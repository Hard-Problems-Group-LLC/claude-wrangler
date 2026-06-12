#!/usr/bin/env python3
"""Command-line entry point for claude-wrangler."""

from __future__ import annotations

from typing import Optional, Sequence

from .config import config_from_args, parse_args
from .models import ClaudeWranglerError
from .operations import (
    ensure_gitignore_block,
    inspect_operation,
    install_like_operation,
    selftest_operation,
    update_operation,
    uninstall_operation,
)
from .runtime import eprint


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Program entry point."""

    try:
        args = parse_args(argv)
        config = config_from_args(args)
        if config.operation != "uninstall":
            ensure_gitignore_block(config)
        if config.operation == "inspect":
            return inspect_operation(config)
        if config.operation == "selftest":
            return selftest_operation(config)
        if config.operation == "uninstall":
            return uninstall_operation(config)
        if config.operation == "update":
            return update_operation(config)
        return install_like_operation(config)
    except ClaudeWranglerError as exc:
        eprint("ERROR: {}".format(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
