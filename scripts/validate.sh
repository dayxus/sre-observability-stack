#!/usr/bin/env bash
# Validates everything that can be validated without a cluster.
#
#   scripts/validate.sh          run every check
#   PYTHON=python3.13 scripts/validate.sh
#
#   1. promtool check config + check rules        (official prometheus release binary)
#   2. amtool check-config                        (official alertmanager release binary)
#   3. loki -verify-config + promtail -check-syntax
#   4. python -m pytest tests/ -q                 (dashboards, compose, configs, target)
#   5. docker compose config -q                   (skipped with a notice when docker is
#                                                  missing, which is the case on macOS)
#
# The binaries are downloaded from the projects' own GitHub releases into .tools/ at the
# exact versions pinned in versions.env: the validation runs with the same tooling the
# stack runs with. Nothing is installed globally and nothing needs sudo.

set -euo pipefail

# shellcheck source=lib/env.sh
. "$(dirname "$0")/lib/env.sh"

TOOLS_DIR="${LAB_ROOT}/.tools"
PYTHON_BIN="${PYTHON:-python3}"
PYTEST_ARGS=("tests/" "-q")

mkdir -p "${TOOLS_DIR}"
PLATFORM="$(lab_platform)"
STEP=0
TOTAL_STEPS=5

step() {
  STEP=$((STEP + 1))
  printf '\n[%s/%s] %s\n' "${STEP}" "${TOTAL_STEPS}" "$1"
}

ok() { printf '  ok: %s\n' "$1"; }
skip() { printf '  skipped: %s\n' "$1"; }

require_command curl
require_command tar

# --- official binaries ---------------------------------------------------------------

fetch_prometheus_binary() {
  local tool="$1" version="${PROMETHEUS_VERSION}" bare="${PROMETHEUS_VERSION#v}"
  local path="${TOOLS_DIR}/${tool}-${bare}"
  if [ -x "${path}" ]; then
    printf '%s\n' "${path}"
    return 0
  fi
  local archive="prometheus-${bare}.${PLATFORM}.tar.gz"
  echo "  downloading prometheus ${version} (${PLATFORM})" >&2
  curl -fsSL -o "${TOOLS_DIR}/${archive}" \
    "https://github.com/prometheus/prometheus/releases/download/${version}/${archive}"
  tar -xzf "${TOOLS_DIR}/${archive}" -C "${TOOLS_DIR}" \
    "prometheus-${bare}.${PLATFORM}/${tool}"
  mv "${TOOLS_DIR}/${tool}" "${path}"
  chmod +x "${path}"
  rm -f "${TOOLS_DIR}/${archive}"
  printf '%s\n' "${path}"
}

fetch_alertmanager_binary() {
  local tool="$1" version="${ALERTMANAGER_VERSION}" bare="${ALERTMANAGER_VERSION#v}"
  local path="${TOOLS_DIR}/${tool}-${bare}"
  if [ -x "${path}" ]; then
    printf '%s\n' "${path}"
    return 0
  fi
  local archive="alertmanager-${bare}.${PLATFORM}.tar.gz"
  echo "  downloading alertmanager ${version} (${PLATFORM})" >&2
  curl -fsSL -o "${TOOLS_DIR}/${archive}" \
    "https://github.com/prometheus/alertmanager/releases/download/${version}/${archive}"
  tar -xzf "${TOOLS_DIR}/${archive}" -C "${TOOLS_DIR}" \
    "alertmanager-${bare}.${PLATFORM}/${tool}"
  mv "${TOOLS_DIR}/${tool}" "${path}"
  chmod +x "${path}"
  rm -f "${TOOLS_DIR}/${archive}"
  printf '%s\n' "${path}"
}

fetch_loki_binary() {
  local version="${LOKI_VERSION}"
  local binary="${TOOLS_DIR}/loki-${version}"
  if [ -x "${binary}" ]; then
    printf '%s\n' "${binary}"
    return 0
  fi
  require_command unzip "needed to unpack the loki release archive"
  local archive="loki-${PLATFORM}.zip"
  echo "  downloading loki ${version} (${PLATFORM})" >&2
  curl -fsSL -o "${TOOLS_DIR}/${archive}" \
    "https://github.com/grafana/loki/releases/download/v${version}/${archive}"
  ( cd "${TOOLS_DIR}" && unzip -o -q "${archive}" )
  mv "${TOOLS_DIR}/loki-${PLATFORM}" "${binary}"
  chmod +x "${binary}"
  rm -f "${TOOLS_DIR}/${archive}"
  printf '%s\n' "${binary}"
}

fetch_promtail_binary() {
  local version="${PROMTAIL_VERSION}"
  local binary="${TOOLS_DIR}/promtail-${version}"
  if [ -x "${binary}" ]; then
    printf '%s\n' "${binary}"
    return 0
  fi
  require_command unzip "needed to unpack the promtail release archive"
  local archive="promtail-${PLATFORM}.zip"
  echo "  downloading promtail ${version} (${PLATFORM})" >&2
  curl -fsSL -o "${TOOLS_DIR}/${archive}" \
    "https://github.com/grafana/loki/releases/download/v${version}/${archive}"
  ( cd "${TOOLS_DIR}" && unzip -o -q "${archive}" )
  mv "${TOOLS_DIR}/promtail-${PLATFORM}" "${binary}"
  chmod +x "${binary}"
  rm -f "${TOOLS_DIR}/${archive}"
  printf '%s\n' "${binary}"
}

run_pytest() {
  local candidate
  for candidate in "${LAB_ROOT}/.venv/bin/python" "${PYTHON_BIN}"; do
    if [ -x "${candidate}" ] || command -v "${candidate}" >/dev/null 2>&1; then
      if "${candidate}" -c 'import pytest, jsonschema, yaml' >/dev/null 2>&1; then
        echo "  using ${candidate}"
        ( cd "${LAB_ROOT}" && "${candidate}" -m pytest "${PYTEST_ARGS[@]}" )
        return $?
      fi
    fi
  done

  if [ ! -x "${LAB_ROOT}/.venv/bin/python" ]; then
    echo "  creating .venv with the test dependencies"
    "${PYTHON_BIN}" -m venv "${LAB_ROOT}/.venv"
    "${LAB_ROOT}/.venv/bin/python" -m pip install -q -U pip
    "${LAB_ROOT}/.venv/bin/python" -m pip install -q -r "${LAB_ROOT}/requirements-dev.txt"
  fi
  ( cd "${LAB_ROOT}" && "${LAB_ROOT}/.venv/bin/python" -m pytest "${PYTEST_ARGS[@]}" )
}

# --- checks ---------------------------------------------------------------------------

printf 'validating sre-observability-stack\n'
printf 'platform: %s   python: %s\n' "${PLATFORM}" "$("${PYTHON_BIN}" -V 2>&1)"

step "prometheus: promtool check config + check rules"
PROMTOOL="$(fetch_prometheus_binary promtool)"
"${PROMTOOL}" --version | head -1 | sed 's/^/  /'
"${PROMTOOL}" check config "${LAB_ROOT}/prometheus/prometheus.yml"
"${PROMTOOL}" check rules "${LAB_ROOT}"/prometheus/rules/*.rules.yml
ok "scrape config, alerting config and 3 rule files are valid"

step "alertmanager: amtool check-config"
AMTOOL="$(fetch_alertmanager_binary amtool)"
"${AMTOOL}" --version | head -1 | sed 's/^/  /'
"${AMTOOL}" check-config "${LAB_ROOT}/alertmanager/alertmanager.yml"
ok "route tree, receivers and inhibit rules are valid"

step "loki and promtail: config validation with the release binaries"
LOKI_BIN="$(fetch_loki_binary)"
PROMTAIL_BIN="$(fetch_promtail_binary)"
"${LOKI_BIN}" -verify-config -config.file="${LAB_ROOT}/loki/loki-config.yml" 2>&1 | tail -3 | sed 's/^/  /'
ok "loki config verified by loki ${LOKI_VERSION}"
"${PROMTAIL_BIN}" -check-syntax -config.file="${LAB_ROOT}/promtail/promtail-config.yml"
ok "promtail config syntax checked by promtail ${PROMTAIL_VERSION}"

step "python tests: pytest (dashboards, compose, prometheus configs, synthetic target)"
run_pytest

step "docker compose config (needs docker; this step is skipped without it)"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ( cd "${LAB_ROOT}" && docker compose config -q )
  ok "docker compose rendered the stack without errors"
else
  skip "docker not found, skipping compose validation"
fi

printf '\nvalidation finished: promtool, amtool, loki, promtail and pytest all green\n'
