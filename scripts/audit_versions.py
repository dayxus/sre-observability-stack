#!/usr/bin/env python3
"""Weekly component audit for the observability stack.

Reads the pinned versions from versions.env, asks the upstream release APIs what the
current stable version is, and writes:

    docs/versions.md        the version table that ships with the repository
    reports/weekly-audit.md the audit itself: pins, drift, counts of what is configured
    .audit/summary.json     machine-readable summary the workflow acts on

The workflow commits the two documents only when they change, and opens an issue instead
of bumping a major version on its own: a major upgrade of Prometheus, Grafana or Loki
changes configuration semantics, which is a reviewed change, not a scheduled one.

Only the standard library is used, so the job runs on a bare runner with no dependencies.
PyYAML is used when it happens to be installed; without it the counters fall back to a
regex over the rule files, and that is stated in the report.

    python3 scripts/audit_versions.py                # write the reports
    python3 scripts/audit_versions.py --check        # exit 1 when anything is out of date
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_FILE = REPO_ROOT / "versions.env"
VERSIONS_DOC = REPO_ROOT / "docs" / "versions.md"
REPORT_FILE = REPO_ROOT / "reports" / "weekly-audit.md"
SUMMARY_FILE = REPO_ROOT / ".audit" / "summary.json"
USER_AGENT = "dayxus-sre-observability-stack-maintenance (+https://github.com/dayxus/sre-observability-stack)"


class Component:
    """One pinned component and where its current version can be read from."""

    def __init__(
        self,
        name: str,
        env_key: str,
        image: str,
        source: str,
        repository: str,
        changelog: str,
        tag_filter: Optional[str] = None,
        note: str = "",
    ) -> None:
        self.name = name
        self.env_key = env_key
        self.image = image
        self.source = source  # "github" or "dockerhub"
        self.repository = repository
        self.changelog = changelog
        self.tag_filter = tag_filter
        self.note = note


COMPONENTS: List[Component] = [
    Component(
        "Prometheus",
        "PROMETHEUS_VERSION",
        "prom/prometheus",
        "github",
        "prometheus/prometheus",
        "https://github.com/prometheus/prometheus/blob/main/CHANGELOG.md",
    ),
    Component(
        "Alertmanager",
        "ALERTMANAGER_VERSION",
        "prom/alertmanager",
        "github",
        "prometheus/alertmanager",
        "https://github.com/prometheus/alertmanager/blob/main/CHANGELOG.md",
    ),
    Component(
        "Blackbox exporter",
        "BLACKBOX_EXPORTER_VERSION",
        "prom/blackbox-exporter",
        "github",
        "prometheus/blackbox_exporter",
        "https://github.com/prometheus/blackbox_exporter/blob/master/CHANGELOG.md",
    ),
    Component(
        "Node exporter",
        "NODE_EXPORTER_VERSION",
        "prom/node-exporter",
        "github",
        "prometheus/node_exporter",
        "https://github.com/prometheus/node_exporter/blob/master/CHANGELOG.md",
    ),
    Component(
        "Grafana",
        "GRAFANA_VERSION",
        "grafana/grafana",
        "github",
        "grafana/grafana",
        "https://github.com/grafana/grafana/blob/main/CHANGELOG.md",
    ),
    Component(
        "Loki",
        "LOKI_VERSION",
        "grafana/loki",
        "github",
        "grafana/loki",
        "https://github.com/grafana/loki/blob/main/CHANGELOG.md",
    ),
    Component(
        "Promtail",
        "PROMTAIL_VERSION",
        "grafana/promtail",
        "dockerhub",
        "grafana/promtail",
        "https://grafana.com/docs/loki/latest/send-data/promtail/",
        tag_filter=r"^3\.6\.\d+$",
        note="frozen upstream: Promtail is superseded by Grafana Alloy (see README limitations)",
    ),
    Component(
        "Python (synthetic target image)",
        "PYTHON_VERSION",
        "library/python",
        "dockerhub",
        "library/python",
        "https://www.python.org/downloads/",
        tag_filter=r"^3\.13\.\d+-slim$",
    ),
]


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_github(repository: str) -> Tuple[str, str]:
    payload = http_json(f"https://api.github.com/repos/{repository}/releases/latest")
    return payload["tag_name"], payload.get("html_url", f"https://github.com/{repository}/releases")


def latest_dockerhub(repository: str, tag_filter: Optional[str]) -> Tuple[str, str]:
    payload = http_json(
        f"https://hub.docker.com/v2/repositories/{repository}/tags/?page_size=100&ordering=last_updated"
    )
    pattern = re.compile(tag_filter) if tag_filter else None
    for entry in payload.get("results", []):
        name = entry["name"]
        if pattern is None or pattern.match(name):
            return name, f"https://hub.docker.com/r/{repository}/tags?name={name}"
    raise RuntimeError(f"no tag of {repository} matches {tag_filter}")


def parse_version(tag: str) -> Tuple[int, int, int]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise ValueError(f"cannot parse a semantic version out of {tag}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def drift_level(pinned: str, latest: str) -> str:
    """up_to_date | patch | minor | major, based on the semantic version triple."""
    if pinned == latest:
        return "up_to_date"
    try:
        pinned_version = parse_version(pinned)
        latest_version = parse_version(latest)
    except ValueError:
        return "unknown"
    if latest_version < pinned_version:
        return "up_to_date"
    for index, label in enumerate(("major", "minor", "patch")):
        if latest_version[index] > pinned_version[index]:
            return label
    return "up_to_date"


def read_pins() -> Dict[str, str]:
    pins: Dict[str, str] = {}
    for line in VERSIONS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        pins[key.strip()] = value.strip()
    return pins


def count_configuration() -> Dict[str, int]:
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None  # noqa: N813 - optional dependency, see module docstring

    counts = {"rule_files": 0, "alert_rules": 0, "recording_rules": 0, "dashboards": 0, "panels": 0}
    for path in sorted((REPO_ROOT / "prometheus" / "rules").glob("*.rules.yml")):
        counts["rule_files"] += 1
        text = path.read_text(encoding="utf-8")
        if yaml is not None:
            content = yaml.safe_load(text)
            for group in content.get("groups", []):
                for rule in group.get("rules", []):
                    counts["alert_rules"] += 1 if "alert" in rule else 0
                    counts["recording_rules"] += 1 if "record" in rule else 0
        else:
            counts["alert_rules"] += len(re.findall(r"^\s*-\s*alert:", text, re.MULTILINE))
            counts["recording_rules"] += len(re.findall(r"^\s*-\s*record:", text, re.MULTILINE))
    for path in sorted((REPO_ROOT / "grafana" / "dashboards").glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        counts["dashboards"] += 1
        counts["panels"] += len(dashboard.get("panels", []))
    return counts


def render_versions_doc(rows: List[dict], generated_at: str) -> str:
    lines = [
        "# Component versions",
        "",
        "Pinned in [`versions.env`](../versions.env) — the single source of truth for every image the",
        "stack runs. This table is regenerated weekly by `.github/workflows/maintenance.yml`, which",
        "queries the upstream release APIs; it is not edited by hand.",
        "",
        f"_Last checked: {generated_at}_",
        "",
        "| Component | Pinned | Latest stable upstream | Status | Image | Changelog |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        status = {
            "up_to_date": "up to date",
            "patch": "patch behind",
            "minor": "minor behind",
            "major": "**major behind**",
            "unknown": "unknown",
            "unreachable": "upstream unreachable",
        }[row["drift"]]
        note = f" — {row['note']}" if row.get("note") else ""
        lines.append(
            f"| {row['name']} | `{row['pinned']}` | `{row['latest']}` | {status}{note} | "
            f"`{row['image']}` | [changelog]({row['changelog']}) |"
        )
    lines += [
        "",
        "The weekly job never bumps a version by itself: patch and minor drift is reported here,",
        "while a major bump opens an issue with the changelog and the validation steps, because a",
        "major release of Prometheus, Grafana or Loki changes configuration semantics.",
        "",
    ]
    return "\n".join(lines)


def render_report(rows: List[dict], counts: Dict[str, int], generated_at: str, warnings: List[str]) -> str:
    drifted = [row for row in rows if row["drift"] not in {"up_to_date", "unreachable"}]
    majors = [row for row in rows if row["drift"] == "major"]
    lines = [
        "# Weekly audit",
        "",
        f"_Generated: {generated_at}_",
        "",
        "## Pinned components",
        "",
        "| Component | Pinned | Latest stable | Drift |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(f"| {row['name']} | `{row['pinned']}` | `{row['latest']}` | {row['drift']} |")

    lines += [
        "",
        "## Drift",
        "",
    ]
    if not drifted:
        lines.append("No drift: every pinned version is the current stable release upstream.")
    else:
        for row in drifted:
            lines.append(
                f"- **{row['name']}**: pinned `{row['pinned']}`, upstream `{row['latest']}` "
                f"({row['drift']} drift) — {row['changelog']}"
            )
        lines += [
            "",
            "Patch and minor drift is applied by editing `versions.env` and letting CI run "
            "`scripts/validate.sh` plus the smoke job. A major drift is opened as an issue instead.",
        ]

    lines += [
        "",
        "## What is configured",
        "",
        f"- rule files: {counts['rule_files']}",
        f"- recording rules: {counts['recording_rules']}",
        f"- alerting rules: {counts['alert_rules']}",
        f"- dashboards: {counts['dashboards']} ({counts['panels']} panels)",
        "",
        "## Checks run by this job",
        "",
        "- upstream release APIs for every pinned component (GitHub Releases / Docker Hub tags)",
        "- `promtool check rules` with the newest published promtool against this repository's rules",
        "",
    ]
    if warnings:
        lines += ["## Warnings", ""] + [f"- {warning}" for warning in warnings] + [""]
    if majors:
        lines += [
            "## Issues to open",
            "",
        ]
        for row in majors:
            lines.append(
                f"- {row['name']} `{row['pinned']}` → `{row['latest']}`: read {row['changelog']}, "
                "then run scripts/validate.sh and the CI smoke job before merging."
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit pinned component versions against upstream releases.")
    parser.add_argument("--check", action="store_true", help="exit 1 when any component is out of date")
    args = parser.parse_args(argv)

    pins = read_pins()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    warnings: List[str] = []
    rows: List[dict] = []

    for component in COMPONENTS:
        pinned = pins.get(component.env_key, "")
        if not pinned:
            warnings.append(f"{component.env_key} is not defined in versions.env")
            continue
        try:
            if component.source == "github":
                latest, link = latest_github(component.repository)
            else:
                latest, link = latest_dockerhub(component.repository, component.tag_filter)
            drift = drift_level(pinned, latest)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, KeyError, ValueError) as exc:
            latest, link, drift = "unknown", component.changelog, "unreachable"
            warnings.append(f"{component.name}: could not read upstream releases ({exc})")
        rows.append(
            {
                "name": component.name,
                "env_key": component.env_key,
                "pinned": pinned,
                "latest": latest,
                "drift": drift,
                "image": f"{component.image}:{pinned}",
                "changelog": component.changelog,
                "release": link,
                "note": component.note,
            }
        )

    counts = count_configuration()
    VERSIONS_DOC.parent.mkdir(parents=True, exist_ok=True)
    VERSIONS_DOC.write_text(render_versions_doc(rows, generated_at), encoding="utf-8")
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(render_report(rows, counts, generated_at, warnings), encoding="utf-8")

    summary = {
        "generated_at": generated_at,
        "components": rows,
        "counts": counts,
        "warnings": warnings,
        "drift": {
            level: [row["name"] for row in rows if row["drift"] == level]
            for level in ("major", "minor", "patch", "unreachable")
        },
    }
    SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"audited {len(rows)} components at {generated_at}")
    for row in rows:
        print(
            f"  {row['name']:<38} pinned={row['pinned']:<16} latest={row['latest']:<16} drift={row['drift']}"
        )
    print(f"configuration: {counts}")
    for warning in warnings:
        print(f"warning: {warning}")
    print(
        f"wrote {VERSIONS_DOC.relative_to(REPO_ROOT)}, {REPORT_FILE.relative_to(REPO_ROOT)}, "
        f"{SUMMARY_FILE.relative_to(REPO_ROOT)}"
    )

    if not rows:
        print(
            "error: no component could be checked; the workflow should treat this as a failure",
            file=sys.stderr,
        )
        return 1

    if args.check:
        drifted = [row for row in rows if row["drift"] not in {"up_to_date", "unreachable"}]
        return 1 if drifted else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
