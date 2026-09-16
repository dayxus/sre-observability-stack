"""Behaviour tests for the synthetic target.

The target is the workload the whole stack observes, so it gets the same treatment as the
configuration: its handlers are exercised in-process, on an ephemeral port, without
Docker. What is asserted here is exactly what the stack depends on — a 200 on /healthz,
Prometheus text on /metrics, a real 500 on /api/error, the alert webhook sink, and JSON
log lines that the promtail pipeline can parse.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
import urllib.error
import urllib.request

import pytest
from conftest import REPO_ROOT

MODULE_PATH = REPO_ROOT / "synthetic" / "target" / "target_server.py"
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
# Prometheus text exposition format: metric name, an optional label set and a numeric
# sample. Quoted label values may contain braces (the target labels /api/orders/{id}), so
# the label set is matched quote-aware instead of "anything up to the first }".
SAMPLE_LINE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*"
    r"(?:\{(?:[^\"\\}]|\\.|\"(?:[^\"\\]|\\.)*\")*\})?"
    r"\s+-?\d"
)


@pytest.fixture(scope="module")
def target(tmp_path_factory):
    log_file = tmp_path_factory.mktemp("lab") / "synthetic-target.log"
    os.environ["TARGET_LOG_FILE"] = str(log_file)
    spec = importlib.util.spec_from_file_location("synthetic_target_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE["metrics"].reset()
    return module, log_file


@pytest.fixture(scope="module")
def server(target):
    module, _ = target
    httpd = module.build_http_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def base_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def fetch_json(url: str):
    status, body = get(url)
    return status, json.loads(body)


def post_json(url: str, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_classify_counts_only_known_paths(target):
    module, _ = target
    assert module.classify("/healthz") == "/healthz"
    assert module.classify("/api/orders") == "/api/orders"
    assert module.classify("/api/orders/1001") == "/api/orders/{id}"
    assert module.classify("/api/slow") == "/api/slow"
    assert module.classify("/api/error") == "/api/error"
    assert module.classify("/some/random/path/12345") is None
    assert module.classify("/api/orders/not-a-number") is None


def test_healthz_reports_ok(base_url):
    status, payload = fetch_json(f"{base_url}/healthz")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["uptime_seconds"] >= 0


def test_orders_endpoints(base_url):
    status, payload = fetch_json(f"{base_url}/api/orders")
    assert status == 200
    assert payload["count"] == len(payload["orders"]) > 0

    status, order = fetch_json(f"{base_url}/api/orders/1001")
    assert status == 200
    assert order["id"] == 1001

    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{base_url}/api/orders/9999")
    assert error.value.code == 404


def test_error_endpoint_injects_a_real_failure(base_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{base_url}/api/error")
    assert error.value.code == 500
    payload = json.loads(error.value.read())
    assert payload["error"] == "injected_fault"


def test_slow_endpoint_is_inside_the_latency_objective(base_url, target):
    module, _ = target
    import time

    started = time.perf_counter()
    status, payload = fetch_json(f"{base_url}/api/slow")
    elapsed = time.perf_counter() - started
    assert status == 200
    assert payload["endpoint"] == "slow"
    assert module.SLOW_ENDPOINT_MAX_SECONDS <= elapsed < 2.0, (
        "the slow endpoint must stay close to the latency objective: slow enough to be visible, "
        "fast enough not to burn the budget"
    )


def test_unknown_path_never_becomes_a_metric_label(base_url, target):
    module, _ = target
    probe_path = "/unlabelled-path-check"
    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{base_url}{probe_path}")
    assert error.value.code == 404
    status, body = get(f"{base_url}/metrics")
    assert status == 200
    assert probe_path not in body.decode()


def test_metrics_endpoint_exposes_prometheus_text(base_url):
    get(f"{base_url}/api/orders")
    get(f"{base_url}/healthz")
    status, body = get(f"{base_url}/metrics")
    text = body.decode()
    assert status == 200
    assert "# TYPE synthetic_http_requests_total counter" in text
    assert 'synthetic_http_requests_total{path="/healthz",code="200"}' in text
    assert "# TYPE synthetic_http_request_duration_seconds histogram" in text
    assert 'le="0.3"' in text, "the latency objective boundary must be a histogram bucket"
    assert 'le="+Inf"' in text
    assert 'synthetic_http_request_duration_seconds_count{path="/healthz"}' in text
    assert "# TYPE synthetic_http_inflight_requests gauge" in text
    assert "# TYPE synthetic_uptime_seconds gauge" in text
    assert "synthetic_build_info{version=" in text
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert SAMPLE_LINE.match(line), f"malformed line: {line}"


def test_alert_webhook_stores_notifications(base_url, target):
    module, _ = target
    before = module.STATE["sink"].snapshot()["count"]
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "UnitTestAlert",
                    "severity": "critical",
                    "service": "synthetic-target",
                },
                "annotations": {"summary": "in-process sink check"},
            }
        ],
    }
    status, response = post_json(f"{base_url}/alerts", payload)
    assert status == 200
    assert response["received"] == before + 1

    status, snapshot = fetch_json(f"{base_url}/alerts/received")
    assert status == 200
    assert snapshot["count"] == before + 1
    stored = snapshot["alerts"][-1]
    assert stored["alertname"] == "UnitTestAlert"
    assert stored["severity"] == "critical"
    assert stored["status"] == "firing"


def test_alert_webhook_rejects_malformed_payloads(base_url):
    request = urllib.request.Request(
        f"{base_url}/alerts",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 400


def test_log_lines_are_json_with_a_parseable_timestamp(base_url, target):
    _, log_file = target
    get(f"{base_url}/healthz")
    assert log_file.exists(), "the target did not write its log file"
    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "the log file is empty"
    for line in lines:
        record = json.loads(line)
        assert RFC3339.match(record["time"]), f"timestamp is not RFC3339: {record['time']}"
        assert record["level"] in {"debug", "info", "warn", "error"}
        assert record["logger"]
        assert record["msg"]
    served = [json.loads(line) for line in lines if json.loads(line)["msg"] == "request served"]
    assert served, "no request log line was written"
    assert {"path", "status", "duration_ms", "client"} <= set(served[-1])
