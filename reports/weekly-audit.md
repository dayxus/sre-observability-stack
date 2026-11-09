# Weekly audit

_Generated: 2026-11-09 16:08 UTC_

## Pinned components

| Component | Pinned | Latest stable | Drift |
| --- | --- | --- | --- |
| Prometheus | `v3.14.0` | `v3.16.0` | minor |
| Alertmanager | `v0.34.0` | `v0.34.2` | patch |
| Blackbox exporter | `v0.28.0` | `v0.29.1` | minor |
| Node exporter | `v1.12.1` | `v1.12.1` | up_to_date |
| Grafana | `13.2.2` | `v13.3.0` | minor |
| Loki | `3.7.7` | `v3.7.9` | patch |
| Promtail | `3.6.11` | `3.6.11` | up_to_date |
| Python (synthetic target image) | `3.13.15-slim` | `3.13.18-slim` | patch |

## Drift

- **Prometheus**: pinned `v3.14.0`, upstream `v3.16.0` (minor drift) — https://github.com/prometheus/prometheus/blob/main/CHANGELOG.md
- **Alertmanager**: pinned `v0.34.0`, upstream `v0.34.2` (patch drift) — https://github.com/prometheus/alertmanager/blob/main/CHANGELOG.md
- **Blackbox exporter**: pinned `v0.28.0`, upstream `v0.29.1` (minor drift) — https://github.com/prometheus/blackbox_exporter/blob/master/CHANGELOG.md
- **Grafana**: pinned `13.2.2`, upstream `v13.3.0` (minor drift) — https://github.com/grafana/grafana/blob/main/CHANGELOG.md
- **Loki**: pinned `3.7.7`, upstream `v3.7.9` (patch drift) — https://github.com/grafana/loki/blob/main/CHANGELOG.md
- **Python (synthetic target image)**: pinned `3.13.15-slim`, upstream `3.13.18-slim` (patch drift) — https://www.python.org/downloads/

Patch and minor drift is applied by editing `versions.env` and letting CI run `scripts/validate.sh` plus the smoke job. A major drift is opened as an issue instead.

## What is configured

- rule files: 3
- recording rules: 27
- alerting rules: 17
- dashboards: 3 (24 panels)

## Checks run by this job

- upstream release APIs for every pinned component (GitHub Releases / Docker Hub tags)
- `promtool check rules` with the newest published promtool against this repository's rules
