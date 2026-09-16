"""Contract tests for the provisioned dashboards.

A dashboard that renders an empty panel is worse than no dashboard, so the JSON is
validated against the repository's own subset schema (schemas/grafana-dashboard.schema.json)
and cross-checked against the provisioning files and the documentation: datasource uids
must be the provisioned ones, every panel must have a description and a documented
operational question, and no expression may hardcode an endpoint.
"""

from __future__ import annotations

import json
import re

import jsonschema
import pytest
import yaml
from conftest import DASHBOARDS_DIR, PROVISIONING_DIR, REPO_ROOT, dashboard_files, read_text

EXPECTED_UIDS = {"sre-slo-overview", "sre-golden-signals", "sre-blackbox-probes"}
DASHBOARD_DOC = REPO_ROOT / "docs" / "dashboards.md"
PROVIDER_PATH = "/var/lib/grafana/dashboards"


@pytest.fixture(scope="session")
def schema() -> dict:
    return json.loads(read_text(REPO_ROOT / "schemas" / "grafana-dashboard.schema.json"))


@pytest.fixture(scope="session")
def dashboard_provider() -> dict:
    provisioning = yaml.safe_load(read_text(PROVISIONING_DIR / "dashboards" / "dashboards.yml"))
    return provisioning["providers"][0]


@pytest.fixture(scope="session")
def datasources() -> dict:
    provisioning = yaml.safe_load(read_text(PROVISIONING_DIR / "datasources" / "datasources.yml"))
    return {datasource["uid"]: datasource for datasource in provisioning["datasources"]}


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda path: path.name)
def test_dashboard_validates_against_the_local_schema(path, schema):
    dashboard = json.loads(read_text(path))
    jsonschema.Draft202012Validator(schema).validate(dashboard)


def test_dashboard_identity_is_unique_and_expected(dashboards):
    uids = [dashboard["uid"] for dashboard in dashboards.values()]
    assert len(uids) == len(set(uids)), DASHBOARDS_DIR
    assert set(uids) == EXPECTED_UIDS


def test_dashboards_reference_only_provisioned_datasources(dashboards, datasources):
    for filename, dashboard in dashboards.items():
        for panel in dashboard["panels"]:
            references = [panel["datasource"]] + [target["datasource"] for target in panel["targets"]]
            for reference in references:
                assert reference["uid"] in datasources, (
                    f"{filename}: panel '{panel['title']}' references unprovisioned datasource {reference['uid']}"
                )
                assert reference["type"] == datasources[reference["uid"]]["type"], (
                    f"{filename}: panel '{panel['title']}' declares type {reference['type']} "
                    f"for the datasource {reference['uid']} of type {datasources[reference['uid']]['type']}"
                )


def test_panel_ids_and_ref_ids_are_unique_per_dashboard(dashboards):
    for filename, dashboard in dashboards.items():
        panel_ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(panel_ids) == len(set(panel_ids)), f"{filename} reuses a panel id"
        titles = [panel["title"] for panel in dashboard["panels"]]
        assert len(titles) == len(set(titles)), f"{filename} reuses a panel title"
        for panel in dashboard["panels"]:
            ref_ids = [target["refId"] for target in panel["targets"]]
            assert len(ref_ids) == len(set(ref_ids)), f"{filename}: panel '{panel['title']}' reuses a refId"


def test_every_panel_states_the_question_it_answers(dashboards):
    for filename, dashboard in dashboards.items():
        for panel in dashboard["panels"]:
            assert len(panel["description"]) >= 15, (
                f"{filename}: panel '{panel['title']}' has no operational description"
            )


def test_every_panel_is_documented(dashboards):
    documentation = read_text(DASHBOARD_DOC)
    for filename, dashboard in dashboards.items():
        assert dashboard["title"] in documentation, f"{filename} title is missing from docs/dashboards.md"
        for panel in dashboard["panels"]:
            assert panel["title"] in documentation, (
                f"{filename}: panel '{panel['title']}' is not documented in docs/dashboards.md"
            )


def test_no_expression_hardcodes_an_endpoint(dashboards):
    for filename, dashboard in dashboards.items():
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                expr = target["expr"]
                assert "http://" not in expr and "localhost" not in expr, (
                    f"{filename}: panel '{panel['title']}' hardcodes an endpoint in its query"
                )


def test_templating_variables_are_well_formed(dashboards):
    for filename, dashboard in dashboards.items():
        for variable in dashboard["templating"]["list"]:
            assert variable["type"] == "query", (
                f"{filename}: variable {variable['name']} is not a query variable"
            )
            assert isinstance(variable["query"], dict) and variable["query"].get("query"), (
                f"{filename}: variable {variable['name']} has no query"
            )
            if variable.get("includeAll"):
                assert variable.get("allValue"), f"{filename}: variable {variable['name']} has no allValue"
            assert isinstance(variable["current"]["value"], list), (
                f"{filename}: variable {variable['name']} current value must be a list"
            )


def test_queries_do_not_use_the_builder_query_shape(dashboards):
    """Expressions are written as code; the builder shape hides them from review."""
    for filename, dashboard in dashboards.items():
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                assert re.match(r"^[a-zA-Z0-9_:{\[\(]", target["expr"]), (
                    f"{filename}: unexpected expression shape: {target['expr']}"
                )
                assert len(target["expr"]) >= 8, f"{filename}: expression is too short to be meaningful"


def test_provider_disables_ui_edits_and_points_at_the_mounted_path(dashboard_provider):
    assert dashboard_provider["allowUiUpdates"] is False, (
        "dashboards could be edited in the UI and lost on restart"
    )
    assert dashboard_provider["disableDeletion"] is True
    assert dashboard_provider["options"]["path"] == PROVIDER_PATH


def test_provider_path_is_the_path_mounted_by_compose(compose_config, dashboard_provider):
    mounts = [
        volume["target"] if isinstance(volume, dict) else volume.split(":")[1]
        for volume in compose_config["services"]["grafana"]["volumes"]
    ]
    assert dashboard_provider["options"]["path"] in mounts, (
        f"the provider reads {dashboard_provider['options']['path']}, grafana mounts {mounts}"
    )
    assert f"{DASHBOARDS_DIR.relative_to(REPO_ROOT)}" in " ".join(
        volume["source"] if isinstance(volume, dict) else volume.split(":")[0]
        for volume in compose_config["services"]["grafana"]["volumes"]
    )


def test_dashboards_cover_the_three_documented_views(dashboards):
    panel_types = {panel["type"] for dashboard in dashboards.values() for panel in dashboard["panels"]}
    assert "timeseries" in panel_types, "no time series panel in any dashboard"
    assert {"stat", "gauge"} & panel_types, "no single value panel in any dashboard"
    assert "table" in panel_types, "no table panel in any dashboard"
