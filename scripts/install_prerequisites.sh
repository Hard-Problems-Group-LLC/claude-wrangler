#!/usr/bin/env bash
# install_prerequisites.sh - POSIX wrapper for install_prerequisites.py.
#
# Finds a Python 3.9+ interpreter and runs scripts/install_prerequisites.py
# with every argument forwarded; that script detects and installs whatever
# else is missing. THEKNOWLEDGE_BOOTSTRAP_PYTHON, when set, names the
# interpreter to use, with no fallback. Without any Python 3.9+, this
# wrapper prints the package-manager command that installs one and exits 1;
# it never runs sudo itself.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
helper="${script_dir}/install_prerequisites.py"

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    >/dev/null 2>&1
}

find_python() {
  local candidate
  if [[ -n "${THEKNOWLEDGE_BOOTSTRAP_PYTHON:-}" ]]; then
    if python_is_supported "${THEKNOWLEDGE_BOOTSTRAP_PYTHON}"; then
      printf '%s\n' "${THEKNOWLEDGE_BOOTSTRAP_PYTHON}"
      return 0
    fi
    echo "install_prerequisites.sh: THEKNOWLEDGE_BOOTSTRAP_PYTHON is not a Python 3.9+ interpreter; not falling back." >&2
    return 2
  fi
  for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

python_install_hint() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "  sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null 2>&1; then
    echo "  sudo dnf install -y python3 python3-pip"
  elif command -v yum >/dev/null 2>&1; then
    echo "  sudo yum install -y python3 python3-pip"
  elif command -v brew >/dev/null 2>&1; then
    echo "  brew install python"
  else
    echo "  (no apt-get, dnf, yum, or brew found; see https://www.python.org/downloads/)"
  fi
}

status=0
python_path="$(find_python)" || status=$?
if [[ "$status" -eq 2 ]]; then
  exit 1
fi
if [[ "$status" -ne 0 ]]; then
  for arg in "$@"; do
    if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
      echo "Usage: scripts/install_prerequisites.sh [options]"
      echo
      echo "Installs the prerequisites this project needs when they are missing."
      echo "The full option list, including --dev, is shown once Python 3.9+ exists."
      echo "Install Python 3.9+ first, for example:"
      python_install_hint
      exit 0
    fi
  done
  echo "install_prerequisites.sh: no Python 3.9+ interpreter found." >&2
  echo "install_prerequisites.sh: install one, then rerun this script, for example:" >&2
  python_install_hint >&2
  exit 1
fi

exec "$python_path" "$helper" "$@"
