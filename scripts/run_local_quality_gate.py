#!/usr/bin/env python3
"""Run the repository's local quality gate."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from claude_wrangler.devtools import main_run_quality_gate  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main_run_quality_gate(REPO_ROOT))
