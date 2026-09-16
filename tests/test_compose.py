"""Contract tests for docker-compose.yml.

The compose file is the interface of the whole stack, so it is treated as data and
checked like any other interface: pinned images, healthchecks, restart policy, resource
limits, dependency conditions, mounts that exist and variables that are actually defined.
"""

from __future__ import annotations

import re

from conftest import REPO_ROOT, read_text

EXPECTED_SERVICES = {
    "prometheus",
    "alertmanager",
    "blackbox-exporter",
    "node-exporter",
    "loki",
    "promtail",
    "grafana",
    "synthetic-target",
}

# Paths generated at runtime by scripts/gen-certs.sh, so they do not exist in a fresh clone.
GENERATED_PATH_PREFIXES = ("runtime/", "./runtime/")

# Host paths that only exist inside a Linux host or inside the Docker VM on macOS.
HOST_PATH_PREFIXES = (
    "/proc",
    "/sys",
    "/var/run/docker.sock",
    "/var/lib/docker",
)

COMPOSE_PROJECT_NAME = "sre-observability-stack"


def test_project_name_is_fixed(compose_config):
    assert compose_config["name"] == COMPOSE_PROJECT_NAME


def test_expected_services_are_defined(compose_config):
    assert set(compose_config["services"]) == EXPECTED_SERVICES


def test_every_image_is_interpolated_from_versions_env(compose_raw, versions_env):
    """The tag is never typed in the compose file: it is always a versions.env variable.

    The repository part stays literal text, so the version of every component is a one-line
    change in versions.env and a hand-typed tag cannot survive a review.
    """
    images = re.findall(r"^\s+image:\s*(\S+)\s*$", compose_raw, re.MULTILINE)
    assert images, "no image line found in docker-compose.yml"
    for image in images:
        match = re.fullmatch(r"[a-z0-9._/-]+:\$\{([A-Za-z_][A-Za-z0-9_]*)\}", image)
        assert match, f"image '{image}' does not resolve its tag from a versions.env variable"
        assert match.group(1) in versions_env, f"{match.group(1)} is not defined in versions.env"


def test_no_floating_image_tags(compose_config, versions_env):
    pinned = set(versions_env.values())
    for name, service in compose_config["services"].items():
        image = service["image"]
        assert ":" in image.rsplit("/", 1)[-1], f"{name} has no explicit tag ({image})"
        tag = image.rsplit("/", 1)[-1].split(":", 1)[1]
        assert tag != "latest", f"{name} uses :latest"
        assert tag in pinned, f"{name} uses tag {tag}, which versions.env does not pin"


def test_versions_env_has_no_latest_and_uses_concrete_versions(versions_env):
    assert versions_env, "versions.env is empty"
    for key, value in versions_env.items():
        assert value != "latest", f"{key} is pinned to latest"
        assert re.match(r"^v?\d+\.\d+\.\d+([.-][A-Za-z0-9.]+)?$", value), (
            f"{key}={value} is not a concrete version"
        )


def test_every_service_has_a_healthcheck(compose_config):
    for name, service in compose_config["services"].items():
        healthcheck = service.get("healthcheck")
        assert healthcheck, f"{name} has no healthcheck"
        assert healthcheck.get("test"), f"{name} has an empty healthcheck"
        assert int(healthcheck.get("retries", 0)) >= 3, f"{name} healthcheck retries < 3"


def test_every_service_restarts_unless_stopped(compose_config):
    for name, service in compose_config["services"].items():
        assert service.get("restart") == "unless-stopped", f"{name} does not restart unless-stopped"


def test_every_service_has_resource_limits(compose_config):
    for name, service in compose_config["services"].items():
        limits = service.get("deploy", {}).get("resources", {}).get("limits")
        assert limits, f"{name} has no resource limits"
        assert "cpus" in limits and "memory" in limits, f"{name} limits are incomplete: {limits}"


def test_depends_on_uses_healthy_conditions_and_real_services(compose_config):
    services = compose_config["services"]
    for name, service in services.items():
        depends_on = service.get("depends_on") or {}
        for dependency, options in depends_on.items():
            assert dependency in services, f"{name} depends on unknown service {dependency}"
            assert options.get("condition") == "service_healthy", (
                f"{name} -> {dependency} does not wait for service_healthy"
            )
    # the wiring that the smoke test relies on
    upstream = {dependency for name in services.values() for dependency in (name.get("depends_on") or {})}
    assert {"prometheus", "loki", "synthetic-target", "node-exporter", "alertmanager"} <= upstream


def test_bind_mounts_point_at_files_that_exist(compose_config):
    missing = []
    for name, service in compose_config["services"].items():
        for volume in service.get("volumes", []):
            source = volume["source"] if isinstance(volume, dict) else volume.split(":", 1)[0]
            if not (source.startswith("./") or source.startswith("../")):
                continue
            if source.startswith(GENERATED_PATH_PREFIXES):
                continue
            if not (REPO_ROOT / source[2:]).exists():
                missing.append(f"{name}: {source}")
    assert not missing, f"bind mounts point at paths that do not exist: {missing}"


def test_host_mounts_are_expected_host_paths(compose_config):
    for name, service in compose_config["services"].items():
        for volume in service.get("volumes", []):
            source = volume["source"] if isinstance(volume, dict) else volume.split(":", 1)[0]
            if source.startswith("/"):
                # node_exporter deliberately mounts the whole host root read-only.
                assert source == "/" or source.startswith(HOST_PATH_PREFIXES), (
                    f"{name} mounts the unexpected host path {source}"
                )


def test_named_volumes_are_declared(compose_config):
    declared = set(compose_config.get("volumes") or {})
    used = set()
    for service in compose_config["services"].values():
        for volume in service.get("volumes", []):
            source = volume["source"] if isinstance(volume, dict) else volume.split(":", 1)[0]
            if not source.startswith(("/", "./", "../")):
                used.add(source)
    assert used, "no named volume is used, the config mounts nothing persistent"
    assert used <= declared, f"volumes used but not declared: {sorted(used - declared)}"


def test_published_ports_come_from_env_example(compose_raw, env_example):
    mappings = re.findall(r'^\s+-\s*"\$\{([A-Za-z_][A-Za-z0-9_]*)\}:\d+"\s*$', compose_raw, re.MULTILINE)
    assert mappings, "no published port is driven by a variable"
    assert set(mappings) <= set(env_example), (
        f"ports not documented in .env.example: {sorted(set(mappings) - set(env_example))}"
    )


def test_every_interpolated_variable_is_defined(compose_config, undefined_variables):
    assert compose_config["services"], "compose file did not parse into services"
    assert not undefined_variables, f"undefined compose variables: {sorted(set(undefined_variables))}"


def test_no_privileged_or_dangerous_options(compose_config):
    for name, service in compose_config["services"].items():
        assert not service.get("privileged"), f"{name} runs privileged"
        assert service.get("network_mode") != "host", f"{name} uses host networking"
        assert not service.get("pid"), f"{name} shares the host pid namespace"


def test_networks_are_declared_and_used(compose_config):
    declared = set(compose_config.get("networks") or {})
    assert declared, "no network declared"
    for name, service in compose_config["services"].items():
        assert service.get("networks"), f"{name} is not attached to a network"
        assert set(service["networks"]) <= declared


def test_grafana_provisioning_is_mounted_read_only(compose_config):
    grafana = compose_config["services"]["grafana"]
    mounts = {
        (volume["source"] if isinstance(volume, dict) else volume.split(":", 1)[0]): (
            volume.get("read_only") if isinstance(volume, dict) else volume.endswith(":ro")
        )
        for volume in grafana["volumes"]
    }
    assert mounts["./grafana/provisioning"] is True, "grafana provisioning is not mounted read-only"
    assert mounts["./grafana/dashboards"] is True, "dashboards are not mounted read-only"


def test_non_interpolated_source_paths_match_the_repository_layout(compose_config):
    expected = {
        "prometheus": ["./prometheus/prometheus.yml", "./prometheus/rules"],
        "alertmanager": ["./alertmanager/alertmanager.yml"],
        "blackbox-exporter": ["./blackbox/blackbox.yml"],
        "loki": ["./loki/loki-config.yml"],
        "promtail": ["./promtail/promtail-config.yml"],
        "synthetic-target": ["./synthetic/target/target_server.py"],
    }
    for service, paths in expected.items():
        sources = [
            volume["source"] if isinstance(volume, dict) else volume.split(":", 1)[0]
            for volume in compose_config["services"][service]["volumes"]
        ]
        for path in paths:
            assert path in sources, f"{service} does not mount {path}"


def test_runtime_tls_material_is_generated_not_committed(compose_raw):
    assert "./runtime/tls:/certs:ro" in compose_raw, (
        "the synthetic target does not mount the generated certificate"
    )
    gitignore = read_text(REPO_ROOT / ".gitignore")
    assert re.search(r"^runtime/$", gitignore, re.MULTILINE), (
        "runtime/ is not git-ignored, so generated TLS material could be committed"
    )
    generator = read_text(REPO_ROOT / "scripts" / "gen-certs.sh")
    assert "openssl req -x509" in generator, "gen-certs.sh does not create a self-signed certificate"
    up_script = read_text(REPO_ROOT / "scripts" / "up.sh")
    assert "gen-certs.sh" in up_script, "scripts/up.sh does not generate the TLS material before starting"
