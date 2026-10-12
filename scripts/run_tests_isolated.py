#!/usr/bin/env python3
"""Run pytest with every home-like environment variable redirected.

Some tests exercise installers that resolve the user's home directory. On
Windows those resolve it from USERPROFILE (and APPDATA/LOCALAPPDATA), not
HOME, so a test that only redirects HOME can install into the operator's
real profile. This runner points HOME, USERPROFILE, HOMEDRIVE/HOMEPATH,
APPDATA, LOCALAPPDATA, and XDG_* at a fresh temporary directory before
pytest starts, as defense in depth on every operating system.

Usage: python scripts/run_tests_isolated.py [pytest arguments...]
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent


def isolated_environment(home: Path, base: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of base with every home-like variable under home."""

    env = dict(base)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # HOMEDRIVE/HOMEPATH must agree with USERPROFILE on Windows; on POSIX
    # they are unused but harmless.
    drive, tail = os.path.splitdrive(str(home))
    if drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
    env["APPDATA"] = str(home / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Create a temporary home, then run pytest inside it."""

    pytest_args: List[str] = list(sys.argv[1:] if argv is None else argv)
    with tempfile.TemporaryDirectory(prefix="claude-wrangler-test-home-") as raw:
        home = Path(raw)
        for relative in ("AppData/Roaming", "AppData/Local", ".config", ".cache"):
            (home / relative).mkdir(parents=True, exist_ok=True)
        env = isolated_environment(home, dict(os.environ))
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
        command.extend(pytest_args)
        print("[run_tests_isolated] HOME/USERPROFILE -> {}".format(home), flush=True)
        completed = subprocess.run(command, cwd=REPO_ROOT, env=env, check=False)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
