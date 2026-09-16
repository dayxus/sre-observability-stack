#!/usr/bin/env bash
# Brings the whole stack up and waits until every healthcheck passes.
#
#   scripts/up.sh              start the stack
#   scripts/smoke.sh           prove it is correct afterwards (recommended)

set -euo pipefail

# shellcheck source=lib/env.sh
. "$(dirname "$0")/lib/env.sh"

require_command docker "docker compose v2 is required"
if ! docker compose version >/dev/null 2>&1; then
  echo "error: 'docker compose' (v2) is required; the v1 python client is not supported" >&2
  exit 1
fi

if [ ! -f "${LAB_ROOT}/.env" ]; then
  cp "${LAB_ROOT}/.env.example" "${LAB_ROOT}/.env"
  echo "created ${LAB_ROOT}/.env from .env.example (git-ignored; edit it to change ports or the Grafana password)"
fi

"${LAB_ROOT}/scripts/gen-certs.sh"

echo
echo "starting the stack (image tags from versions.env)"
compose up -d --wait

echo
echo "stack is up and healthy:"
printf '  prometheus        http://localhost:%s\n' "${PROMETHEUS_PORT}"
printf '  alertmanager      http://localhost:%s\n' "${ALERTMANAGER_PORT}"
printf '  grafana           http://localhost:%s  (user %s, password in %s)\n' "${GRAFANA_PORT}" "${GF_ADMIN_USER}" "${LAB_ENV_FILE}"
printf '  loki              http://localhost:%s\n' "${LOKI_PORT}"
printf '  promtail          http://localhost:%s\n' "${PROMTAIL_PORT}"
printf '  blackbox exporter http://localhost:%s\n' "${BLACKBOX_EXPORTER_PORT}"
printf '  node exporter     http://localhost:%s\n' "${NODE_EXPORTER_PORT}"
printf '  synthetic target  http://localhost:%s  (https on %s)\n' "${SYNTHETIC_TARGET_HTTP_PORT}" "${SYNTHETIC_TARGET_HTTPS_PORT}"
echo
echo "next: scripts/smoke.sh   (targets up, dashboards provisioned, alert delivered)"
