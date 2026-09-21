# Component versions

Pinned in [`versions.env`](../versions.env) — the single source of truth for every image the
stack runs. This table is regenerated weekly by `.github/workflows/maintenance.yml`, which
queries the upstream release APIs; it is not edited by hand.

_Last checked: 2026-09-21 12:42 UTC_

| Component | Pinned | Latest stable upstream | Status | Image | Changelog |
| --- | --- | --- | --- | --- | --- |
| Prometheus | `v3.14.0` | `v3.14.0` | up to date | `prom/prometheus:v3.14.0` | [changelog](https://github.com/prometheus/prometheus/blob/main/CHANGELOG.md) |
| Alertmanager | `v0.34.0` | `v0.34.1` | patch behind | `prom/alertmanager:v0.34.0` | [changelog](https://github.com/prometheus/alertmanager/blob/main/CHANGELOG.md) |
| Blackbox exporter | `v0.28.0` | `v0.28.0` | up to date | `prom/blackbox-exporter:v0.28.0` | [changelog](https://github.com/prometheus/blackbox_exporter/blob/master/CHANGELOG.md) |
| Node exporter | `v1.12.1` | `v1.12.1` | up to date | `prom/node-exporter:v1.12.1` | [changelog](https://github.com/prometheus/node_exporter/blob/master/CHANGELOG.md) |
| Grafana | `13.2.2` | `v13.2.2` | up to date | `grafana/grafana:13.2.2` | [changelog](https://github.com/grafana/grafana/blob/main/CHANGELOG.md) |
| Loki | `3.7.7` | `v3.7.8` | patch behind | `grafana/loki:3.7.7` | [changelog](https://github.com/grafana/loki/blob/main/CHANGELOG.md) |
| Promtail | `3.6.11` | `3.6.11` | up to date — frozen upstream: Promtail is superseded by Grafana Alloy (see README limitations) | `grafana/promtail:3.6.11` | [changelog](https://grafana.com/docs/loki/latest/send-data/promtail/) |
| Python (synthetic target image) | `3.13.15-slim` | `3.13.15-slim` | up to date | `library/python:3.13.15-slim` | [changelog](https://www.python.org/downloads/) |

The weekly job never bumps a version by itself: patch and minor drift is reported here,
while a major bump opens an issue with the changelog and the validation steps, because a
major release of Prometheus, Grafana or Loki changes configuration semantics.
