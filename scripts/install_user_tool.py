#!/usr/bin/env python3
"""Repository-root installer for a user-level claude-wrangler command."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from claude_wrangler.installer import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
