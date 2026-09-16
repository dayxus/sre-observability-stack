#!/usr/bin/env bash
# Generates the TLS material used by the synthetic target and inspected by the blackbox
# ssl probe.
#
# The certificate is self-signed and valid for TLS_CERT_VALIDITY_DAYS days. It is written
# to runtime/ (git-ignored) and regenerated per run, so no private key ever enters the
# repository: the lab proves it can terminate TLS without committing a credential.

set -euo pipefail

# shellcheck source=lib/env.sh
. "$(dirname "$0")/lib/env.sh"

require_command openssl "install openssl, or use the one shipped with the OS"

CERT_DIR="${LAB_ROOT}/runtime/tls"
CERT_FILE="${CERT_DIR}/tls.crt"
KEY_FILE="${CERT_DIR}/tls.key"
VALIDITY_DAYS="${TLS_CERT_VALIDITY_DAYS:-30}"

mkdir -p "${CERT_DIR}"

if [ -f "${CERT_FILE}" ] && [ -f "${KEY_FILE}" ] && openssl x509 -checkend 86400 -noout -in "${CERT_FILE}" >/dev/null 2>&1; then
  echo "tls: reusing ${CERT_FILE} (valid for more than a day)"
  openssl x509 -in "${CERT_FILE}" -noout -subject -issuer -dates
  exit 0
fi

CONFIG_FILE="$(mktemp -t sre-lab-openssl.XXXXXX)"
cleanup() { rm -f "${CONFIG_FILE}"; }
trap cleanup EXIT

# Written as a config file rather than -addext so the script works on both OpenSSL 3.x
# (Linux CI) and the LibreSSL that ships with macOS.
cat >"${CONFIG_FILE}" <<'OPENSSL_CONFIG'
[req]
distinguished_name = distinguished_name
prompt = no
x509_extensions = v3_extensions

[distinguished_name]
CN = synthetic-target
O = observability-lab

[v3_extensions]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = synthetic-target
DNS.2 = localhost
IP.1 = 127.0.0.1
OPENSSL_CONFIG

openssl req -x509 -newkey rsa:2048 -nodes \
  -days "${VALIDITY_DAYS}" \
  -config "${CONFIG_FILE}" \
  -keyout "${KEY_FILE}" \
  -out "${CERT_FILE}" 2>/dev/null

chmod 600 "${KEY_FILE}"
chmod 644 "${CERT_FILE}"

echo "tls: generated ${CERT_FILE} (${VALIDITY_DAYS} days) and ${KEY_FILE}"
openssl x509 -in "${CERT_FILE}" -noout -subject -issuer -dates -ext subjectAltName
