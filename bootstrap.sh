#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[bootstrap.sh] Delegating to ${SCRIPT_DIR}/install.sh"
exec "${SCRIPT_DIR}/install.sh" "$@"
