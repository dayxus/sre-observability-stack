#!/usr/bin/env bash
# Smoke test: proves the stack is not merely running but correct.
#
#   scripts/smoke.sh          run against the stack started by scripts/up.sh
#
# It checks, in order:
#   1. every health endpoint answers (prometheus, alertmanager, grafana, loki, promtail,
#      blackbox exporter, node exporter, synthetic target)
#   2. every Prometheus scrape target is UP, and the blackbox probes all succeed
#   3. the SLO recording rules are evaluated and no rule evaluation is failing
#   4. an alert posted through the Alertmanager API reaches the webhook sink
#   5. Grafana has the provisioned datasources and all dashboards
#   6. the log pipeline really ships: Loki returns lines for the target's log file
#
# Evidence is written to runtime/smoke/ and uploaded by CI, so a failed run can be read
# after the fact instead of being re-run.

set -uo pipefail

# shellcheck source=lib/env.sh
. "$(dirname "$0")/lib/env.sh"

require_command curl
require_command jq
require_command docker "the stack only runs through docker compose"

PROM="http://localhost:${PROMETHEUS_PORT}"
AM="http://localhost:${ALERTMANAGER_PORT}"
GRAFANA="http://localhost:${GRAFANA_PORT}"
LOKI="http://localhost:${LOKI_PORT}"
PROMTAIL="http://localhost:${PROMTAIL_PORT}"
BLACKBOX="http://localhost:${BLACKBOX_EXPORTER_PORT}"
NODE_EXPORTER="http://localhost:${NODE_EXPORTER_PORT}"
TARGET="http://localhost:${SYNTHETIC_TARGET_HTTP_PORT}"
GRAFANA_AUTH="${GF_ADMIN_USER}:${GF_ADMIN_PASSWORD}"
HEAD_TIMEOUT="${SMOKE_HEAD_TIMEOUT:-60}"
ALERT_TIMEOUT="${SMOKE_ALERT_TIMEOUT:-90}"

EVIDENCE_DIR="${LAB_ROOT}/runtime/smoke"
mkdir -p "${EVIDENCE_DIR}"

PASSES=0
FAILURES=0
FAILED_CHECKS=()

pass() {
  PASSES=$((PASSES + 1))
  printf '  \033[32mPASS\033[0m %s\n' "$1"
}

fail() {
  FAILURES=$((FAILURES + 1))
  FAILED_CHECKS+=("$1")
  printf '  \033[31mFAIL\033[0m %s\n' "$1"
}

step() {
  printf '\n== %s\n' "$1"
}

# wait_for_http <url> <description> - retries until the endpoint answers 2xx/3xx.
wait_for_http() {
  local url="$1" description="$2" waited=0
  while [ "${waited}" -lt "${HEAD_TIMEOUT}" ]; do
    if curl -fsS -o /dev/null --max-time 5 "${url}"; then
      pass "${description}"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  fail "${description} (no answer from ${url} within ${HEAD_TIMEOUT}s)"
  return 1
}

# query_prometheus <promql> <output file>
query_prometheus() {
  curl -fsS --get "${PROM}/api/v1/query" \
    --data-urlencode "query=$1" \
    --data-urlencode "time=$(date -u +%s)" \
    -o "$2"
}

printf 'sre-observability-stack smoke test\n'
printf 'evidence directory: %s\n' "${EVIDENCE_DIR}"

step "1. health endpoints"
wait_for_http "${PROM}/-/ready" "prometheus /-/ready answers"
wait_for_http "${PROM}/-/healthy" "prometheus /-/healthy answers"
wait_for_http "${AM}/-/ready" "alertmanager /-/ready answers"
wait_for_http "${GRAFANA}/api/health" "grafana /api/health answers"
wait_for_http "${LOKI}/ready" "loki /ready answers"
wait_for_http "${PROMTAIL}/ready" "promtail /ready answers"
wait_for_http "${BLACKBOX}/-/healthy" "blackbox exporter /-/healthy answers"
wait_for_http "${NODE_EXPORTER}/metrics" "node exporter /metrics answers"
wait_for_http "${TARGET}/healthz" "synthetic target /healthz answers"

curl -fsS "${GRAFANA}/api/health" -o "${EVIDENCE_DIR}/grafana-health.json"
GRAFANA_VERSION_REPORTED="$(jq -r '.version' "${EVIDENCE_DIR}/grafana-health.json")"
if [ "${GRAFANA_VERSION_REPORTED}" = "${GRAFANA_VERSION}" ]; then
  pass "grafana runs the pinned version ${GRAFANA_VERSION}"
else
  fail "grafana reports version ${GRAFANA_VERSION_REPORTED}, versions.env pins ${GRAFANA_VERSION}"
fi

if curl -fsS "${NODE_EXPORTER}/metrics" | grep -q '^node_cpu_seconds_total'; then
  pass "node exporter is collecting host CPU metrics"
else
  fail "node exporter answered but exposes no node_cpu_seconds_total"
fi

step "2. prometheus targets and blackbox probes"
if curl -fsS "${PROM}/api/v1/targets" -o "${EVIDENCE_DIR}/prometheus-targets.json"; then
  TARGET_TOTAL="$(jq -r '.data.activeTargets | length' "${EVIDENCE_DIR}/prometheus-targets.json")"
  TARGET_DOWN="$(jq -r '[.data.activeTargets[] | select(.health != "up")] | length' "${EVIDENCE_DIR}/prometheus-targets.json")"
  if [ "${TARGET_TOTAL}" -gt 0 ] && [ "${TARGET_DOWN}" -eq 0 ]; then
    pass "all ${TARGET_TOTAL} scrape targets are up"
  else
    fail "${TARGET_DOWN} of ${TARGET_TOTAL} scrape targets are not up"
    jq -r '.data.activeTargets[] | select(.health != "up") | "      job=\(.labels.job) instance=\(.labels.instance) health=\(.health) lastError=\(.lastError)"' \
      "${EVIDENCE_DIR}/prometheus-targets.json"
  fi
  printf '  job                 health  scrape url\n'
  jq -r '.data.activeTargets[] | [.labels.job, .health, .scrapeUrl] | @tsv' "${EVIDENCE_DIR}/prometheus-targets.json" |
    awk -F'\t' '{printf "  %-20s %-7s %s\n", $1, $2, $3}'
else
  fail "could not read /api/v1/targets from prometheus"
fi

if query_prometheus "probe_success" "${EVIDENCE_DIR}/prometheus-probe-success.json"; then
  PROBE_TOTAL="$(jq -r '.data.result | length' "${EVIDENCE_DIR}/prometheus-probe-success.json")"
  PROBE_DOWN="$(jq -r '[.data.result[] | select(.value[1] != "1")] | length' "${EVIDENCE_DIR}/prometheus-probe-success.json")"
  PROBE_JOBS="$(jq -r '[.data.result[].metric.job] | unique | length' "${EVIDENCE_DIR}/prometheus-probe-success.json")"
  if [ "${PROBE_TOTAL}" -gt 0 ] && [ "${PROBE_DOWN}" -eq 0 ] && [ "${PROBE_JOBS}" -ge 4 ]; then
    pass "${PROBE_TOTAL} probe series across ${PROBE_JOBS} modules report success"
  else
    fail "${PROBE_DOWN} of ${PROBE_TOTAL} probe series are failing (${PROBE_JOBS} modules reporting)"
  fi
  printf '  probe job            target                       success\n'
  jq -r '.data.result[] | [.metric.job, .metric.instance, .value[1]] | @tsv' "${EVIDENCE_DIR}/prometheus-probe-success.json" |
    awk -F'\t' '{printf "  %-20s %-28s %s\n", $1, $2, $3}'
else
  fail "could not query probe_success from prometheus"
fi

step "3. SLO recording rules"
if query_prometheus "slo:availability:ratio_rate5m" "${EVIDENCE_DIR}/slo-availability-ratio.json"; then
  RATIO="$(jq -r '.data.result[0].value[1] // empty' "${EVIDENCE_DIR}/slo-availability-ratio.json")"
  if [ -n "${RATIO}" ]; then
    pass "slo:availability:ratio_rate5m = ${RATIO}"
  else
    fail "slo:availability:ratio_rate5m has no value: the recording rules are not being evaluated"
  fi
else
  fail "could not query slo:availability:ratio_rate5m"
fi

if query_prometheus "slo:burn_rate:5m" "${EVIDENCE_DIR}/slo-burn-rate.json"; then
  BURN="$(jq -r '.data.result[0].value[1] // empty' "${EVIDENCE_DIR}/slo-burn-rate.json")"
  if [ -n "${BURN}" ]; then
    pass "slo:burn_rate:5m = ${BURN}"
  else
    fail "slo:burn_rate:5m has no value"
  fi
fi

if query_prometheus "slo:latency:ratio_rate5m" "${EVIDENCE_DIR}/slo-latency-ratio.json"; then
  LATENCY="$(jq -r '.data.result[0].value[1] // empty' "${EVIDENCE_DIR}/slo-latency-ratio.json")"
  if [ -n "${LATENCY}" ]; then
    pass "slo:latency:ratio_rate5m = ${LATENCY}"
  else
    fail "slo:latency:ratio_rate5m has no value"
  fi
fi

if query_prometheus "sum(rate(prometheus_rule_evaluation_failures_total[5m]))" "${EVIDENCE_DIR}/rule-evaluation-failures.json"; then
  RULE_FAILURES="$(jq -r '.data.result[0].value[1] // "0"' "${EVIDENCE_DIR}/rule-evaluation-failures.json")"
  if [ "${RULE_FAILURES}" = "0" ]; then
    pass "no rule evaluation failures in prometheus"
  else
    fail "prometheus rule evaluation failures: ${RULE_FAILURES}"
  fi
fi

step "4. alert pipeline (prometheus -> alertmanager -> receiver)"
ALERT_START="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
ALERT_END="$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"

if compose exec -T alertmanager /bin/amtool alert add LabSmokeProbe \
  severity=critical \
  service=synthetic-target \
  slo=availability \
  --annotation=summary="smoke test: verify the alert pipeline end to end" \
  --annotation=runbook_url="https://github.com/dayxus/sre-runbooks-postmortem/blob/main/runbooks/observability/silent-failure.md" \
  --start="${ALERT_START}" \
  --end="${ALERT_END}" \
  --alertmanager.url=http://127.0.0.1:9093 \
  >"${EVIDENCE_DIR}/amtool-alert-add.txt" 2>&1; then
  pass "alert posted to the alertmanager API with amtool"
else
  fail "could not post a test alert through amtool"
  cat "${EVIDENCE_DIR}/amtool-alert-add.txt"
fi

SINK_HIT=0
WAITED=0
while [ "${WAITED}" -lt "${ALERT_TIMEOUT}" ]; do
  if curl -fsS "${TARGET}/alerts/received" -o "${EVIDENCE_DIR}/alert-sink.json" 2>/dev/null; then
    if jq -e '[.alerts[] | select(.alertname == "LabSmokeProbe")] | length > 0' "${EVIDENCE_DIR}/alert-sink.json" >/dev/null 2>&1; then
      SINK_HIT=1
      break
    fi
  fi
  sleep 3
  WAITED=$((WAITED + 3))
done

if [ "${SINK_HIT}" -eq 1 ]; then
  pass "the webhook receiver got the notification (route tree -> receiver -> http POST)"
  printf '  received alert:\n'
  jq -c '[.alerts[] | select(.alertname == "LabSmokeProbe")][0]' "${EVIDENCE_DIR}/alert-sink.json" | sed 's/^/    /'
else
  fail "the alert never reached the webhook sink within ${ALERT_TIMEOUT}s"
  printf '  sink content:\n'
  jq '.' "${EVIDENCE_DIR}/alert-sink.json" 2>/dev/null | sed 's/^/    /' || echo "    (no answer from the sink)"
fi

if query_prometheus "sum(rate(alertmanager_notifications_failed_total[5m]))" "${EVIDENCE_DIR}/notification-failures.json"; then
  NOTIFY_FAILURES="$(jq -r '.data.result[0].value[1] // "0"' "${EVIDENCE_DIR}/notification-failures.json")"
  if [ "${NOTIFY_FAILURES}" = "0" ]; then
    pass "alertmanager has no failed notifications"
  else
    fail "alertmanager notification failures: ${NOTIFY_FAILURES}"
  fi
fi

step "5. grafana provisioning"
if curl -fsS -u "${GRAFANA_AUTH}" "${GRAFANA}/api/datasources" -o "${EVIDENCE_DIR}/grafana-datasources.json"; then
  if jq -e '[.[].uid] as $uids | ($uids | index("prometheus")) != null and ($uids | index("loki")) != null' \
    "${EVIDENCE_DIR}/grafana-datasources.json" >/dev/null; then
    pass "datasources provisioned: prometheus and loki"
  else
    fail "provisioned datasources are missing prometheus and/or loki"
    jq -r '.[] | "      \(.name) uid=\(.uid) type=\(.type)"' "${EVIDENCE_DIR}/grafana-datasources.json"
  fi
else
  fail "could not read /api/datasources from grafana"
fi

if curl -fsS -u "${GRAFANA_AUTH}" "${GRAFANA}/api/search?type=dash-db" -o "${EVIDENCE_DIR}/grafana-dashboards.json"; then
  EXPECTED_DASHBOARDS="$(find "${LAB_ROOT}/grafana/dashboards" -name '*.json' | wc -l | tr -d ' ')"
  FOUND_DASHBOARDS="$(jq -r 'length' "${EVIDENCE_DIR}/grafana-dashboards.json")"
  if [ "${FOUND_DASHBOARDS}" = "${EXPECTED_DASHBOARDS}" ]; then
    pass "${FOUND_DASHBOARDS} dashboards provisioned from grafana/dashboards/"
  else
    fail "grafana has ${FOUND_DASHBOARDS} dashboards, the repository ships ${EXPECTED_DASHBOARDS}"
  fi
  while IFS= read -r uid; do
    if jq -e --arg uid "${uid}" '[.[] | select(.uid == $uid)] | length > 0' \
      "${EVIDENCE_DIR}/grafana-dashboards.json" >/dev/null; then
      pass "dashboard uid '${uid}' is provisioned"
    else
      fail "dashboard uid '${uid}' is missing from grafana"
    fi
  done < <(jq -r '.uid' "${LAB_ROOT}"/grafana/dashboards/*.json)
  printf '  dashboards:\n'
  jq -r '.[] | "    \(.uid)  \(.title)  (folder: \(.folderTitle // "-"))"' "${EVIDENCE_DIR}/grafana-dashboards.json"
else
  fail "could not list dashboards through the grafana API"
fi

if curl -fsS -u "${GRAFANA_AUTH}" "${GRAFANA}/api/search?query=SLO" -o "${EVIDENCE_DIR}/grafana-search-slo.json"; then
  if jq -e '[.[] | select(.uid == "sre-slo-overview")] | length == 1' "${EVIDENCE_DIR}/grafana-search-slo.json" >/dev/null; then
    pass "GET /api/search?query=SLO returns the SLO overview dashboard"
  else
    fail "GET /api/search?query=SLO did not return the SLO overview dashboard"
    jq -c '.' "${EVIDENCE_DIR}/grafana-search-slo.json"
  fi
fi

step "6. log pipeline (promtail -> loki)"
LOKI_START_NS="$(python3 -c 'import time; print(int((time.time() - 1800) * 10**9))')"
LOG_HIT=0
WAITED=0
while [ "${WAITED}" -lt "${HEAD_TIMEOUT}" ]; do
  if curl -fsS --get "${LOKI}/loki/api/v1/query_range" \
    --data-urlencode 'query={job="lab-files"}' \
    --data-urlencode "start=${LOKI_START_NS}" \
    --data-urlencode 'limit=5' \
    --data-urlencode 'direction=backward' \
    -o "${EVIDENCE_DIR}/loki-log-query.json"; then
    if jq -e '.data.result | length > 0' "${EVIDENCE_DIR}/loki-log-query.json" >/dev/null 2>&1; then
      LOG_HIT=1
      break
    fi
  fi
  sleep 3
  WAITED=$((WAITED + 3))
done

if [ "${LOG_HIT}" -eq 1 ]; then
  pass "loki returns log lines shipped by promtail from the target's log file"
  printf '  newest line from {job="lab-files"}:\n'
  jq -r '.data.result[0].values[0][1] // empty' "${EVIDENCE_DIR}/loki-log-query.json" | sed 's/^/    /'
else
  fail "loki has no lines for {job=\"lab-files\"} within ${HEAD_TIMEOUT}s"
fi

printf '\n== summary\n'
printf '  checks passed: %s\n' "${PASSES}"
printf '  checks failed: %s\n' "${FAILURES}"
printf '  evidence: %s\n' "${EVIDENCE_DIR}"

if [ "${FAILURES}" -gt 0 ]; then
  printf '\nfailed checks:\n'
  for check in "${FAILED_CHECKS[@]}"; do
    printf '  - %s\n' "${check}"
  done
  exit 1
fi

printf '\nsmoke test passed: the stack is up, scraped, provisioned and alerting.\n'
