#!/usr/bin/env python3
"""Repository-root install hook for repo-bound dev and standard installs."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from claude_wrangler.project_install import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
