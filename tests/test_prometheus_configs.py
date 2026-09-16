"""Contract tests for the Prometheus, Alertmanager, Loki and Promtail configuration.

These files are the reason the stack is reproducible, so they are validated as data:
every rule file referenced exists, every alert carries a severity and a runbook, every
expression only touches metrics documented in tests/metric_inventory.json, and the
probe clients point at services that actually exist in docker-compose.yml.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

import pytest
import yaml
from conftest import PROMETHEUS_DIR, REPO_ROOT, RULES_DIR, read_text

EXPECTED_SCRAPE_JOBS = {
    "prometheus",
    "alertmanager",
    "loki",
    "blackbox-exporter",
    "node-exporter",
    "synthetic-target",
    "blackbox-http",
    "blackbox-tcp",
    "blackbox-icmp",
    "blackbox-ssl",
}
PROBE_JOBS = {"blackbox-http", "blackbox-tcp", "blackbox-icmp", "blackbox-ssl"}
SYNTHETIC_TARGET_HOST = "synthetic-target"
RUNBOOK_URL_PREFIX = "https://github.com/dayxus/sre-runbooks-postmortem/blob/main/runbooks/"
STATEMENT_KEYWORDS = {
    "and",
    "or",
    "unless",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "bool",
    "offset",
    "start",
    "end",
    "and on",
    "le",
    "job",
    "instance",
    "path",
    "code",
    "mode",
    "fstype",
    "mountpoint",
    "phase",
    "alertstate",
    "severity",
    "service",
    "slo",
    "window",
    "env",
}

_FUNCTION_CALL = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_RECORDING_RULE = re.compile(r"\b(?:slo|lab):[a-zA-Z0-9_:]+")
_METRIC_LIKE = re.compile(
    r"\b(?:probe|node|prometheus|alertmanager|synthetic|process|target|go|python)_[a-zA-Z0-9_]+\b"
)
_SPECIAL_METRICS = ("up", "ALERTS")


def rule_files() -> List:
    return sorted(RULES_DIR.glob("*.rules.yml"))


@pytest.fixture(scope="session")
def rules() -> Dict[str, dict]:
    loaded = {}
    for path in rule_files():
        loaded[path.name] = yaml.safe_load(read_text(path))
    return loaded


def iter_rules(rules: Dict[str, dict]) -> List[Tuple[str, str, dict]]:
    collected = []
    for filename, content in rules.items():
        for group in content["groups"]:
            for rule in group["rules"]:
                collected.append((filename, group["name"], rule))
    return collected


def recorded_names(rules: Dict[str, dict]) -> List[str]:
    return [rule["record"] for _, _, rule in iter_rules(rules) if "record" in rule]


def alert_rules(rules: Dict[str, dict]) -> List[Tuple[str, str, dict]]:
    return [entry for entry in iter_rules(rules) if "alert" in entry[2]]


_EXPRESSION_CACHE: List[str] = []


def expressions() -> List[str]:
    """Every PromQL expression in the repository's rules and dashboards (cached)."""
    if _EXPRESSION_CACHE:
        return _EXPRESSION_CACHE
    collected: List[str] = []
    for path in rule_files():
        content = yaml.safe_load(read_text(path))
        for group in content["groups"]:
            for rule in group["rules"]:
                if "expr" in rule:
                    collected.append(rule["expr"])
    for path in sorted((REPO_ROOT / "grafana" / "dashboards").glob("*.json")):
        dashboard = json.loads(read_text(path))
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                collected.append(target["expr"])
    _EXPRESSION_CACHE.extend(collected)
    return _EXPRESSION_CACHE


def metric_references(expr: str) -> set:
    """Metric and recording-rule names used by an expression.

    Function names (anything followed by '(') and label names are filtered out, so what
    remains is exactly the set of series the expression reads.
    """
    functions = set(_FUNCTION_CALL.findall(expr))
    found = set()
    for match in _METRIC_LIKE.finditer(expr):
        name = match.group(0)
        if name in functions and expr[match.end() : match.end() + 1] == "(":
            continue
        found.add(name)
    for match in _RECORDING_RULE.finditer(expr):
        found.add(match.group(0).rstrip(":"))
    for name in _SPECIAL_METRICS:
        if re.search(rf"\b{name}\b", expr):
            found.add(name)
    return {name for name in found if name not in STATEMENT_KEYWORDS}


# --- prometheus.yml -------------------------------------------------------------------


def test_scrape_job_set_is_complete(prometheus_config):
    jobs = {job["job_name"] for job in prometheus_config["scrape_configs"]}
    assert jobs == EXPECTED_SCRAPE_JOBS


def test_scrape_jobs_have_targets_and_unique_names(prometheus_config):
    names = [job["job_name"] for job in prometheus_config["scrape_configs"]]
    assert len(names) == len(set(names)), "duplicated scrape job name"
    for job in prometheus_config["scrape_configs"]:
        assert job.get("static_configs"), f"{job['job_name']} has no static target"
        for static in job["static_configs"]:
            assert static.get("targets"), f"{job['job_name']} has an empty target list"


def test_global_intervals_are_sane(prometheus_config):
    assert prometheus_config["global"]["scrape_interval"] == "15s"
    assert prometheus_config["global"]["evaluation_interval"] == "15s"
    assert prometheus_config["global"]["external_labels"]["environment"] == "lab"


def test_rule_files_exist_on_disk(prometheus_config):
    declared = prometheus_config["rule_files"]
    assert declared, "prometheus.yml declares no rule files"
    for entry in declared:
        assert (PROMETHEUS_DIR / entry).exists(), f"rule file {entry} does not exist"


def test_alertmanager_is_configured_as_the_alert_sink(prometheus_config):
    targets = [
        target
        for manager in prometheus_config["alerting"]["alertmanagers"]
        for static in manager["static_configs"]
        for target in static["targets"]
    ]
    assert targets == ["alertmanager:9093"]


def test_probe_jobs_use_the_blackbox_exporter_and_defined_modules(prometheus_config, blackbox_config):
    modules = set(blackbox_config["modules"])
    for job in prometheus_config["scrape_configs"]:
        if job["job_name"] not in PROBE_JOBS:
            continue
        assert job["metrics_path"] == "/probe"
        module = job["params"]["module"][0]
        assert module in modules, f"{job['job_name']} uses undefined module {module}"
        relabels = {rule.get("target_label"): rule for rule in job["relabel_configs"]}
        assert relabels["__param_target"]["source_labels"] == ["__address__"]
        assert relabels["__address__"]["replacement"] == "blackbox-exporter:9115"
        assert relabels["instance"]["source_labels"] == ["__param_target"]
        for static in job["static_configs"]:
            for target in static["targets"]:
                assert SYNTHETIC_TARGET_HOST in target, (
                    f"{job['job_name']} probes {target}, which is not the repository's synthetic target"
                )


def test_blackbox_modules_cover_every_probe_layer(blackbox_config):
    modules = blackbox_config["modules"]
    probers = {module["prober"] for module in modules.values()}
    assert probers == {"http", "tcp", "icmp"}
    assert modules["http_2xx"]["http"]["valid_status_codes"] == [200]
    assert modules["tcp_connect"]["tcp"]["preferred_ip_protocol"] == "ip4"
    assert modules["icmp_ping"]["icmp"]["preferred_ip_protocol"] == "ip4"
    assert modules["ssl_expiry"]["http"]["tls_config"]["insecure_skip_verify"] is True, (
        "the ssl module must skip verification: the lab certificate is self-signed"
    )


# --- rule files -----------------------------------------------------------------------


def test_rule_files_parse_into_groups_with_rules(rules):
    assert rules, "no rule files found"
    for filename, content in rules.items():
        assert content.get("groups"), f"{filename} has no groups"
        for group in content["groups"]:
            assert group.get("name"), f"{filename} has an unnamed group"
            assert group.get("rules"), f"{group['name']} has no rules"


def test_alert_names_and_recording_names_are_unique(rules):
    alerts = [rule["alert"] for _, _, rule in alert_rules(rules)]
    records = recorded_names(rules)
    assert len(alerts) == len(set(alerts)), "duplicated alert name"
    assert len(records) == len(set(records)), "duplicated recording rule name"


def test_every_recording_rule_has_a_valid_name_and_expression(rules):
    for filename, group, rule in iter_rules(rules):
        if "record" not in rule:
            continue
        assert re.match(r"^[a-z][a-z0-9_]*:[a-zA-Z0-9_:]+$", rule["record"]), (
            f"{filename}:{group} records into an invalid metric name {rule['record']}"
        )
        assert rule["expr"].strip(), f"{rule['record']} has an empty expression"


def test_every_alert_has_severity_timer_annotations_and_runbook(rules):
    for _filename, _group, rule in alert_rules(rules):
        name = rule["alert"]
        assert rule["expr"].strip(), f"{name} has an empty expression"
        assert rule.get("for"), f"{name} has no 'for' timer"
        severity = rule.get("labels", {}).get("severity")
        assert severity in {"critical", "warning", "info"}, f"{name} has severity {severity}"
        annotations = rule.get("annotations", {})
        assert annotations.get("summary"), f"{name} has no summary annotation"
        assert annotations.get("description"), f"{name} has no description annotation"
        runbook = annotations.get("runbook_url", "")
        assert runbook.startswith(RUNBOOK_URL_PREFIX), (
            f"{name} runbook_url does not point at the runbook repository: {runbook}"
        )
        assert not runbook.endswith("#"), f"{name} runbook_url is not a concrete path"
        assert "{{" not in name, f"{name} alert name must be static"


def test_every_alert_has_a_non_zero_timer(rules):
    """An alert without a timer fires on the first flat sample, which is how alert fatigue starts."""
    for _filename, _group, rule in alert_rules(rules):
        timer = str(rule.get("for", "0m"))
        assert timer not in {"0m", "0s"}, f"{rule['alert']} fires without any timer"


# --- metric honesty -------------------------------------------------------------------


def test_every_referenced_metric_is_documented(metric_inventory):
    documented = set(metric_inventory["metrics"]) | set(metric_inventory["recording_rules"])
    unknown = {}
    for expr in expressions():
        for name in metric_references(expr):
            if name in documented:
                continue
            if any(rule.startswith(name + ":") for rule in metric_inventory["recording_rules"]):
                continue
            unknown.setdefault(name, expr.strip().splitlines()[0])
    assert not unknown, (
        f"expressions reference metrics that tests/metric_inventory.json does not document: {unknown}"
    )


def test_inventory_metrics_are_actually_used(metric_inventory):
    used = set()
    for expr in expressions():
        used |= metric_references(expr)
    unused = sorted(set(metric_inventory["metrics"]) - used)
    assert not unused, f"documented but never referenced: {unused}"
    all_exprs = expressions()
    unused_rules = sorted(
        name for name in metric_inventory["recording_rules"] if not any(name in expr for expr in all_exprs)
    )
    assert not unused_rules, f"recording rules that nothing consumes: {unused_rules}"


def test_slo_alerts_use_multi_window_conditions(rules):
    burn_rate = [
        rule
        for _, _, rule in alert_rules(rules)
        if rule["alert"].endswith("BurnRateFast")
        or rule["alert"].endswith("BurnRateMedium")
        or rule["alert"].endswith("BurnRateSlow")
    ]
    assert len(burn_rate) >= 5
    for rule in burn_rate:
        expr = rule["expr"]
        assert " and " in expr, f"{rule['alert']} is not a multi-window condition: {expr}"
        assert len(re.findall(r"slo:[a-z_:]+", expr)) >= 2, f"{rule['alert']} looks at a single window"


def test_burn_rate_alerts_cover_the_documented_windows(rules):
    windows = {
        rule["labels"].get("window") for _, _, rule in alert_rules(rules) if rule["alert"].startswith("SLO")
    }
    assert {"1h", "6h", "3d", "30d"} <= windows, f"burn-rate windows incomplete: {windows}"


# --- alertmanager ---------------------------------------------------------------------


def test_alertmanager_route_tree_is_by_severity(alertmanager_config):
    route = alertmanager_config["route"]
    assert route["receiver"] == "lab-webhook"
    assert set(route["group_by"]) == {"alertname", "severity", "service"}
    assert route["repeat_interval"]
    severities = []
    for child in route["routes"]:
        for matcher in child["matchers"]:
            assert matcher.startswith("severity"), f"route matcher is not severity based: {matcher}"
            severities.append(matcher)
    assert any("critical" in matcher for matcher in severities)
    assert any("warning" in matcher for matcher in severities)


def test_every_route_receiver_exists_and_has_a_config(alertmanager_config):
    receivers = {receiver["name"]: receiver for receiver in alertmanager_config["receivers"]}
    route = alertmanager_config["route"]
    referenced = {route["receiver"]} | {child["receiver"] for child in route.get("routes", [])}
    assert referenced <= set(receivers), f"routes reference unknown receivers: {referenced - set(receivers)}"
    for name in referenced:
        receiver = receivers[name]
        blocks = [key for key in receiver if key != "name"]
        assert blocks or name == "blackhole", f"receiver {name} has no configuration"
        for key in blocks:
            assert key.endswith("_configs"), f"receiver {name} has an unexpected block {key}"


def test_webhook_receiver_points_at_the_synthetic_target(alertmanager_config):
    webhooks = [
        config
        for receiver in alertmanager_config["receivers"]
        for config in receiver.get("webhook_configs", [])
    ]
    assert webhooks, "no webhook receiver configured"
    urls = [config["url"] for config in webhooks]
    assert any(SYNTHETIC_TARGET_HOST in url for url in urls), f"webhook does not target the lab sink: {urls}"
    assert all(config.get("send_resolved") for config in webhooks), "send_resolved is off"


def test_inhibit_rules_are_structured(alertmanager_config):
    rules = alertmanager_config["inhibit_rules"]
    assert rules, "no inhibit rules defined"
    for rule in rules:
        assert rule["source_matchers"], "inhibit rule without source matchers"
        assert rule["target_matchers"], "inhibit rule without target matchers"
        assert rule["equal"], "inhibit rule without equal labels"
        assert "alertname" in rule["equal"] or "service" in rule["equal"]


def test_no_credentials_are_embedded_in_the_alerting_config(alertmanager_config):
    serialized = yaml.safe_dump(alertmanager_config)
    for pattern in ("password", "token", "api_key", "apikey", "secret", "pagerduty", "slack_api"):
        assert pattern not in serialized.lower(), f"alertmanager.yml mentions {pattern}"


# --- loki and promtail ----------------------------------------------------------------


def test_loki_is_a_single_binary_with_filesystem_storage(loki_config):
    assert loki_config["auth_enabled"] is False
    assert loki_config["server"]["http_listen_port"] == 3100
    assert loki_config["common"]["storage"]["filesystem"]["chunks_directory"].startswith("/loki")
    schema = loki_config["schema_config"]["configs"][0]
    assert schema["store"] == "tsdb"
    assert schema["object_store"] == "filesystem"
    assert schema["schema"].startswith("v")


def test_loki_retention_is_configured_and_bounded(loki_config):
    assert loki_config["limits_config"]["retention_period"].endswith("h")
    assert loki_config["compactor"]["retention_enabled"] is True
    assert loki_config["compactor"]["delete_request_store"] == "filesystem"


def test_loki_ruler_points_at_the_alertmanager(loki_config):
    assert loki_config["ruler"]["alertmanager_url"] == "http://alertmanager:9093"


def test_loki_does_not_report_analytics(loki_config):
    assert loki_config["analytics"]["reporting_enabled"] is False


def test_promtail_ships_to_loki_and_reads_positions_from_a_volume(promtail_config):
    urls = [client["url"] for client in promtail_config["clients"]]
    assert urls == ["http://loki:3100/loki/api/v1/push"]
    assert promtail_config["positions"]["filename"].startswith("/var/lib/promtail")


def test_promtail_scrape_configs_cover_containers_and_lab_files(promtail_config):
    jobs = {job["job_name"]: job for job in promtail_config["scrape_configs"]}
    assert set(jobs) == {"docker-containers", "lab-files"}
    docker_job = jobs["docker-containers"]
    assert docker_job["docker_sd_configs"][0]["host"] == "unix:///var/run/docker.sock"
    relabel_targets = {rule.get("target_label") for rule in docker_job["relabel_configs"]}
    assert {"container", "service", "stream", "job"} <= relabel_targets
    assert docker_job["pipeline_stages"] == [{"docker": {}}]

    file_job = jobs["lab-files"]
    paths = [label["__path__"] for static in file_job["static_configs"] for label in [static["labels"]]]
    assert paths == ["/var/log/lab/*.log"]
    stages = file_job["pipeline_stages"]
    assert any("json" in stage for stage in stages), "lab-files does not parse JSON logs"
    assert any("timestamp" in stage for stage in stages), "lab-files does not parse the log timestamp"


def test_promtail_watches_the_same_directory_the_target_writes(compose_config, promtail_config):
    target_env = compose_config["services"]["synthetic-target"]["environment"]
    log_file = target_env["TARGET_LOG_FILE"]
    promtail_mounts = [
        volume["target"] if isinstance(volume, dict) else volume.split(":")[1]
        for volume in compose_config["services"]["promtail"]["volumes"]
        if "lab-logs" in (volume["source"] if isinstance(volume, dict) else volume)
    ]
    assert promtail_mounts, "promtail does not mount the lab log volume"
    directory = "/" + log_file.strip("/").rsplit("/", 1)[0]
    assert directory in promtail_mounts, f"promtail mounts {promtail_mounts}, the target writes {log_file}"
    target_mounts = [
        volume["target"] if isinstance(volume, dict) else volume.split(":")[1]
        for volume in compose_config["services"]["synthetic-target"]["volumes"]
        if "lab-logs" in (volume["source"] if isinstance(volume, dict) else volume)
    ]
    assert directory in target_mounts
