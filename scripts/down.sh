#!/usr/bin/env bash
# Stops the stack and removes its volumes, so the next `scripts/up.sh` starts from the
# same empty state the CI smoke job starts from.

set -euo pipefail

# shellcheck source=lib/env.sh
. "$(dirname "$0")/lib/env.sh"

require_command docker "docker compose v2 is required"

compose down -v --remove-orphans
echo "stack stopped and volumes removed"
