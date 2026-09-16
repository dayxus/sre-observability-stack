"""Shared fixtures for the repository's test suite.

The tests run without Docker: they validate the configuration as data (YAML, JSON,
PromQL expressions) and exercise the synthetic target's handlers in-process. Anything
that needs a running container is covered by scripts/smoke.sh in CI, which asserts the
same contract against the live stack.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS_DIR = REPO_ROOT / "prometheus"
RULES_DIR = PROMETHEUS_DIR / "rules"
DASHBOARDS_DIR = REPO_ROOT / "grafana" / "dashboards"
PROVISIONING_DIR = REPO_ROOT / "grafana" / "provisioning"

_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SENTINEL = "\x00DOLLAR\x00"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a KEY=value file, ignoring comments and blank lines."""
    values: Dict[str, str] = {}
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@pytest.fixture(scope="session")
def versions_env() -> Dict[str, str]:
    return parse_env_file(REPO_ROOT / "versions.env")


@pytest.fixture(scope="session")
def env_example() -> Dict[str, str]:
    return parse_env_file(REPO_ROOT / ".env.example")


@pytest.fixture(scope="session")
def env_vars(versions_env: Dict[str, str], env_example: Dict[str, str]) -> Dict[str, str]:
    merged = dict(env_example)
    merged.update(versions_env)
    return merged


@pytest.fixture(scope="session")
def compose_raw() -> str:
    return read_text(REPO_ROOT / "docker-compose.yml")


def interpolate(text: str, variables: Dict[str, str], undefined: list) -> str:
    """Expand ${VAR} / ${VAR:-default} the way docker compose does.

    ``$$`` is compose's escape for a literal ``$`` (used in the node exporter's regex
    arguments), so it is protected before substitution and restored afterwards.
    """
    protected = text.replace("$$", _SENTINEL)

    def replace(match: "re.Match[str]") -> str:
        name, default = match.group(1), match.group(2)
        if name in variables:
            return variables[name]
        if default is not None:
            return default
        undefined.append(name)
        return ""

    return _INTERPOLATION.sub(replace, protected).replace(_SENTINEL, "$")


@pytest.fixture(scope="session")
def undefined_variables() -> list:
    return []


@pytest.fixture(scope="session")
def compose_config(compose_raw: str, env_vars: Dict[str, str], undefined_variables: list) -> Dict[str, Any]:
    return yaml.safe_load(interpolate(compose_raw, env_vars, undefined_variables))


@pytest.fixture(scope="session")
def prometheus_config() -> Dict[str, Any]:
    return yaml.safe_load(read_text(PROMETHEUS_DIR / "prometheus.yml"))


@pytest.fixture(scope="session")
def alertmanager_config() -> Dict[str, Any]:
    return yaml.safe_load(read_text(REPO_ROOT / "alertmanager" / "alertmanager.yml"))


@pytest.fixture(scope="session")
def blackbox_config() -> Dict[str, Any]:
    return yaml.safe_load(read_text(REPO_ROOT / "blackbox" / "blackbox.yml"))


@pytest.fixture(scope="session")
def loki_config() -> Dict[str, Any]:
    return yaml.safe_load(read_text(REPO_ROOT / "loki" / "loki-config.yml"))


@pytest.fixture(scope="session")
def promtail_config() -> Dict[str, Any]:
    return yaml.safe_load(read_text(REPO_ROOT / "promtail" / "promtail-config.yml"))


@pytest.fixture(scope="session")
def datasource_uids() -> set:
    provisioning = yaml.safe_load(read_text(PROVISIONING_DIR / "datasources" / "datasources.yml"))
    return {datasource["uid"] for datasource in provisioning["datasources"]}


def dashboard_files() -> list:
    return sorted(DASHBOARDS_DIR.glob("*.json"))


@pytest.fixture(scope="session")
def dashboards() -> Dict[str, Dict[str, Any]]:
    loaded: Dict[str, Dict[str, Any]] = {}
    for path in dashboard_files():
        loaded[path.name] = json.loads(read_text(path))
    return loaded


@pytest.fixture(scope="session")
def metric_inventory() -> Dict[str, Any]:
    return json.loads(read_text(Path(__file__).resolve().parent / "metric_inventory.json"))
