#!/usr/bin/env python3
"""Command-line entry point for claude-wrangler."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from .config import config_from_args, parse_args
from .inspection import inspect_operation, selftest_operation
from .models import ClaudeWranglerError
from .operations import (
    ManagedStateChangedError,
    ensure_gitignore_block,
    install_like_operation,
    rollback_operation,
    uninstall_operation,
    update_operation,
)
from .runtime import eprint

# How often a command re-reads the project's state after another maintainer
# published between this command's read and its taking the maintenance lock.
STATE_CHANGE_ATTEMPTS = 3


def tolerate_unencodable_output() -> None:
    """Never crash on a path the console's code page cannot encode.

    A checkout path with characters outside a Windows console's code page
    (for example cp1252) would otherwise raise UnicodeEncodeError while
    printing the final summary of an operation that already succeeded.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            continue


def run_operation(args: argparse.Namespace) -> int:
    """Derive the configuration from ``args`` and run the requested operation.

    Maintenance operations write the managed `.gitignore` block themselves
    while they hold the maintenance lock, so a second maintainer changes
    nothing (`ab_runtime_slots.md` requirement 4). Inspect and selftest
    take no lock and keep writing the block here.
    """

    config = config_from_args(args)
    if config.operation == "inspect":
        ensure_gitignore_block(config)
        return inspect_operation(config)
    if config.operation == "selftest":
        ensure_gitignore_block(config)
        return selftest_operation(config)
    if config.operation == "uninstall":
        return uninstall_operation(config)
    if config.operation == "update":
        return update_operation(config)
    if config.operation == "rollback":
        return rollback_operation(config)
    return install_like_operation(config)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Program entry point."""

    tolerate_unencodable_output()
    try:
        args = parse_args(argv)
        attempt = 1
        while True:
            try:
                return run_operation(args)
            except ManagedStateChangedError as exc:
                # Nothing was changed; the state is re-read from scratch.
                if attempt >= STATE_CHANGE_ATTEMPTS:
                    raise ClaudeWranglerError(
                        "{} Rerun the command once the other run has "
                        "finished.".format(exc)
                    ) from exc
                eprint(
                    "[claude-wrangler] {} Re-reading the state and running the "
                    "command again.".format(exc)
                )
                attempt += 1
    except ClaudeWranglerError as exc:
        eprint("ERROR: {}".format(exc))
        return 1
    except OSError as exc:
        # Filesystem failures (permissions, a full disk, a file Windows holds
        # open) are reported plainly; atomic writes left the previous state.
        eprint("ERROR: {}".format(exc))
        return 1
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
