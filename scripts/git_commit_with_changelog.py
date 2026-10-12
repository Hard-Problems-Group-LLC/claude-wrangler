#!/usr/bin/env python3
"""Sync the changelog from the pending queue, then delegate to the standard commit helper."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from claude_wrangler.changelog import sync_changelog_from_pending_queue  # noqa: E402

PENDING_QUEUE = (
    REPO_ROOT / "project-management" / "state" / "pending-commit-changes.txt"
)
STANDARD_HELPER = REPO_ROOT / "TheKnowledge" / "scripts" / "git_standard_commit_push.py"


def helper_command(arguments: Sequence[str]) -> list[str]:
    """Return the delegated standard-helper command line.

    The helper path uses native separators because it is passed straight
    to `subprocess.run`, never embedded in generated text.
    """

    return [sys.executable, str(STANDARD_HELPER), *arguments]


def main(argv: Sequence[str] | None = None) -> int:
    """Sync the changelog before delegating to TheKnowledge's commit helper."""

    # An explicit empty list means "no arguments", not "read sys.argv".
    arguments = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in arguments
    if "-h" not in arguments and "--help" not in arguments and PENDING_QUEUE.is_file():
        changelog_file, entry_count, changed = sync_changelog_from_pending_queue(
            REPO_ROOT,
            PENDING_QUEUE,
            dry_run=dry_run,
        )
        if entry_count == 0:
            print(
                "[git-commit-with-changelog] No pending queue entries to mirror "
                "into {}.".format(changelog_file.name),
                flush=True,
            )
        elif changed:
            action = "Would sync" if dry_run else "Synced"
            print(
                "[git-commit-with-changelog] {} {} pending queue entr{} into "
                "{}.".format(
                    action,
                    entry_count,
                    "y" if entry_count == 1 else "ies",
                    changelog_file.name,
                ),
                flush=True,
            )
        else:
            print(
                "[git-commit-with-changelog] {} already contains the {} "
                "pending queue entr{}.".format(
                    changelog_file.name,
                    entry_count,
                    "y" if entry_count == 1 else "ies",
                ),
                flush=True,
            )

    result = subprocess.run(
        helper_command(arguments),
        cwd=str(REPO_ROOT),
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
