#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "source ./set-context.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="$SCRIPT_DIR/python-environments.json"

context_fail() {
  echo "[set-context] $*" >&2
  return 1 2>/dev/null || exit 1
}

select_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(select_python)" || context_fail "python3 or python is required."
RUNTIME_ENV="$("$PYTHON_BIN" -c 'import json, sys; from pathlib import Path; print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["runtime"]["environment_name"])' "$CONFIG_PATH")"
export PYENV_VERSION="$RUNTIME_ENV"
export THEKNOWLEDGE_ACTIVE_PYTHON_CONTEXT="runtime"
echo "[set-context] PYENV_VERSION=$PYENV_VERSION"
