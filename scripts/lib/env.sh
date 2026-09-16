#!/usr/bin/env bash
# Shared environment and helpers for the lab scripts.
#
# Every script sources this file first. It exports versions.env (pinned image tags) and
# .env (runtime settings), falling back to .env.example so a fresh clone works without
# copying anything by hand.

set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export LAB_ROOT

lab_env_file() {
  if [ -f "${LAB_ROOT}/.env" ]; then
    printf '%s\n' "${LAB_ROOT}/.env"
  else
    printf '%s\n' "${LAB_ROOT}/.env.example"
  fi
}

# shellcheck disable=SC1091
load_lab_env() {
  local env_file
  env_file="$(lab_env_file)"
  set -a
  . "${LAB_ROOT}/versions.env"
  . "${env_file}"
  set +a
  export LAB_ENV_FILE="${env_file}"
}

load_lab_env

require_command() {
  local name="$1" hint="${2:-}"
  if ! command -v "${name}" >/dev/null 2>&1; then
    if [ -n "${hint}" ]; then
      echo "error: '${name}' is required (${hint})" >&2
    else
      echo "error: '${name}' is required" >&2
    fi
    return 1
  fi
}

# Prints "<os>-<arch>" in the naming used by the official release artifacts.
lab_platform() {
  local os arch
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux) os=linux ;;
    *)
      echo "error: unsupported operating system: $(uname -s)" >&2
      return 1
      ;;
  esac
  case "$(uname -m)" in
    arm64 | aarch64) arch=arm64 ;;
    x86_64 | amd64) arch=amd64 ;;
    *)
      echo "error: unsupported architecture: $(uname -m)" >&2
      return 1
      ;;
  esac
  printf '%s-%s\n' "${os}" "${arch}"
}

compose() {
  ( cd "${LAB_ROOT}" && docker compose "$@" )
}
