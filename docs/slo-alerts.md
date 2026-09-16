# SLOs, burn rates and why these windows

Two objectives are measured on the synthetic target that this stack scrapes and probes.
Both are recorded in `prometheus/rules/recording.rules.yml` and alerted on in
`prometheus/rules/slo-burnrate.rules.yml`.

## The objectives

| SLO | Indicator | Objective | Window | Where it is measured |
| --- | --- | --- | --- | --- |
| Availability | `probe_success` of the blackbox http probe on `/healthz` | 99.5% | 30 days | blackbox exporter, scraped by the `blackbox-http` job |
| Latency | share of application requests served in under 300 ms | 99.0% | 30 days | the target's own duration histogram, `synthetic_http_request_duration_seconds` |

Two deliberate decisions:

- **Availability is measured from outside the process.** `probe_success` answers "did an
  independent client get a healthy answer", which is what a user experiences. An
  in-process "am I alive" flag would have been easier and useless.
- **`/api/error` is excluded from the latency SLI.** It is fault injection: a deliberate
  500 that keeps the error panels honest. Excluding it is written into the recording rule
  with `path!="/api/error"`, not left implicit.

## Error budget

| SLO | Budget | Consumed as |
| --- | --- | --- |
| Availability 99.5% / 30d | 0.5% of the time, ≈ 3 h 36 min per 30 days | `slo:availability:budget_remaining_ratio` |
| Latency 99.0% / 30d | 1% of requests may exceed 300 ms | derived from `slo:latency:ratio_rate1d` |

A burn rate of 1 means "the budget will be exactly exhausted at the end of the window". A
burn rate of 14.4 means "2% of the budget is gone in an hour". Alerts fire on a *pair* of
windows: a short one that catches the change and a long one that proves it is not a blip.

## The burn-rate table

Detectable time is the minimum time at that burn rate before the alert fires, given the
`for:` timer of each rule.

| Alert | Burn rate threshold | Short window | Long window | Budget burned | Severity | `for:` | Detectable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `SLOAvailabilityBurnRateFast` | 14.4 | 5m | 1h | 2% in 1h | critical | 2m | ~2m |
| `SLOAvailabilityBurnRateMedium` | 6 | 30m | 6h | 5% in 6h | warning | 15m | ~15m |
| `SLOAvailabilityBurnRateSlow` | 3 | 2h | 1d | 10% in 3d | warning | 1h | ~1h |
| `SLOAvailabilityBurnRateVerySlow` | 1 | 6h | 3d | 10% in 30d | info | 3h | ~3h |
| `SLOAvailabilityBudgetExhausted` | ≤ 0 remaining | 30d | – | 100% | critical | 10m | immediate |
| `SLOLatencyBurnRateFast` | 14.4 | 5m | 1h | 2% in 1h | critical | 2m | ~2m |
| `SLOLatencyBurnRateSlow` | 3 | 2h | 1d | 10% in 3d | warning | 1h | ~1h |

Why these numbers and not others:

- **14.4 = 2% of the budget in one hour** for a 30 day window. It is the "page now" tier:
  if it continues, the budget is gone in a day and a half.
- **6 = 5% in six hours.** Worth waking someone for a service with an on-call rotation,
  worth a ticket in this lab.
- **3 = 10% in three days.** The slow leak that never crosses a short-window threshold,
  and the reason a single-window alert is not enough.
- **1 = spend it all in the window.** Not a page: a planning signal, hence `info`.
- The pairs are asymmetric on purpose (short window ≈ 1/12 of the long one): the short
  window reacts within minutes, the long one cannot be triggered by a single bad scrape.

## Routing and inhibition

`alertmanager/alertmanager.yml` routes `critical` to the webhook receiver with a 1 second
group wait and a 30 minute repeat, `warning` with a 10 second group wait and a 12 hour
repeat, and `info`/`debug` to a terminal receiver. Two inhibit rules keep the noise down:
a critical for a service mutes its own warnings, and `TargetDown` mutes derived SLO
warnings for the same service — if the scrape target is gone, the SLO numbers are not
"bad", they are unknown.

There is no pager in this lab: the receiver is the synthetic target's `/alerts` endpoint,
and `scripts/smoke.sh` asserts that an alert posted through the API really arrives there.
Swapping in a real receiver is a two-line change in the receivers block.

## Runbooks

Every alert carries a `runbook_url` pointing at
[dayxus/sre-runbooks-postmortem](https://github.com/dayxus/sre-runbooks-postmortem):

| Alert | Runbook |
| --- | --- |
| `SLOAvailabilityBurnRate*`, `SLOLatencyBurnRateFast`, `ProbeFailing` | `runbooks/observability/silent-failure.md` |
| `TargetDown`, `PrometheusRuleEvaluationFailures` | `runbooks/observability/missing-metrics.md` |
| `CertificateExpiringSoon`, `CertificateExpiryWithin30Days` | `runbooks/network/tls-cert-expiry.md` |
| `ContainerRestartLoop` | `runbooks/kubernetes/crashloopbackoff.md` |
| `HostHighCpuSaturation` | `runbooks/kubernetes/node-notready.md` |
| `HostHighMemoryUsage` | `runbooks/kubernetes/oomkilled.md` |
| `SLOAvailabilityBudgetExhausted`, `AlertmanagerNotificationsFailing` | `runbooks/observability/alert-fatigue.md` |
| `HostDiskSpaceLow`, `SLOAvailabilityBurnRateVerySlow`, `SLOLatencyBurnRateSlow` | `runbooks/README.md` (index: no dedicated runbook yet) |

## What is not honest to claim here

- The 30 day budget and the `1d`/`3d`/`30d` windows are **only meaningful after 30 days of
  retention**. In CI the stack lives for minutes, so the smoke test asserts the short
  windows and that the rules are evaluated, not the long-window values.
- The availability objective is measured on a synthetic workload with a healthy baseline.
  Nothing here has been tuned against real traffic; a real service would need the
  objectives set from a historical baseline, not from a wish.
- Alerts are not paged anywhere. Delivery is proven end to end (route → receiver → HTTP
  POST) but there is no human on the other side of it.
