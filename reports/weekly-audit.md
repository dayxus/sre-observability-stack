# Weekly audit

_Generated: 2026-09-16 00:53 UTC_

## Pinned components

| Component | Pinned | Latest stable | Drift |
| --- | --- | --- | --- |
| Prometheus | `v3.14.0` | `v3.14.0` | up_to_date |
| Alertmanager | `v0.34.0` | `v0.34.0` | up_to_date |
| Blackbox exporter | `v0.28.0` | `v0.28.0` | up_to_date |
| Node exporter | `v1.12.1` | `v1.12.1` | up_to_date |
| Grafana | `13.2.2` | `v13.2.2` | up_to_date |
| Loki | `3.7.7` | `v3.7.7` | up_to_date |
| Promtail | `3.6.11` | `3.6.11` | up_to_date |
| Python (synthetic target image) | `3.13.15-slim` | `3.13.15-slim` | up_to_date |

## Drift

No drift: every pinned version is the current stable release upstream.

## What is configured

- rule files: 3
- recording rules: 27
- alerting rules: 17
- dashboards: 3 (24 panels)

## Checks run by this job

- upstream release APIs for every pinned component (GitHub Releases / Docker Hub tags)
- `promtool check rules` with the newest published promtool against this repository's rules
