#!/usr/bin/env python3
"""Compatibility wrapper for the managed stage-two installer."""

from __future__ import annotations

from pathlib import Path
import os
import runpy
import sys

REPO_ROOT = Path(__file__).resolve().parent
SCRIPT = REPO_ROOT / "scripts" / "install-stage-2.py"


if __name__ == "__main__":
    os.environ.setdefault("THEKNOWLEDGE_MANAGED_INSTALL_STAGE1", "1")
    sys.argv[0] = str(SCRIPT)
    runpy.run_path(str(SCRIPT), run_name="__main__")
