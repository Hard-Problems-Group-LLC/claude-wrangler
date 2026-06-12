#!/usr/bin/env python3
"""Run TheKnowledge knack validation from this repository root."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "TheKnowledge" / "scripts" / "validate_knacks.py"
HELPER_DIR = HELPER_PATH.parent


def load_helper_main():
    """Load the TheKnowledge knack validator as a module."""

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if str(HELPER_DIR) not in sys.path:
        sys.path.insert(0, str(HELPER_DIR))

    spec = importlib.util.spec_from_file_location(
        "theknowledge_validate_knacks",
        HELPER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load knack validation helper.")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.main


if __name__ == "__main__":
    main = load_helper_main()
    raise SystemExit(main(sys.argv[1:]))
