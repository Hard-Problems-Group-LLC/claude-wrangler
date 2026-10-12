"""Tripwire for the repository's own `.gitignore`.

A forced TheKnowledge starter refresh run from a CRLF Windows checkout
rewrites `.gitignore` with `\\r\\r\\n` line endings. Git strips only one
trailing `\\r` per line, so every pattern keeps a stray `\\r` and silently
stops matching, which would let local operator state such as `.local/`
and codex-wrangler's homes be staged. See
docs/specifications/prerequisites_and_platform_entry_points.md.
"""

from pathlib import Path
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = REPO_ROOT / ".gitignore"

# Paths that must stay ignored: TheKnowledge's local-state and
# restricted-name entries plus codex-wrangler's artifacts, which this
# project must never collide with or expose.
MUST_BE_IGNORED = (
    ".codex-home/x",
    ".codex-local/x",
    ".local/x",
    ".theknowledge-restricted-names.local",
)

# Claude Code keeps trackable project settings under `.claude/`.
MUST_STAY_TRACKABLE = (".claude/settings.json",)


def _git_check_ignore(relative_path: str) -> int:
    """Return `git check-ignore` exit status for one repo-relative path.

    `--no-index` evaluates the ignore rules alone, so the answer does not
    depend on whether a file at that path happens to be tracked.
    """

    completed = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", relative_path],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
    )
    return completed.returncode


@pytest.fixture(scope="module")
def git_work_tree() -> Path:
    """Skip unless git is installed and the checkout is a git work tree."""

    if shutil.which("git") is None:
        pytest.skip("git is not available on PATH")
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip("repository checkout is not a git work tree")
    return REPO_ROOT


def test_gitignore_has_no_doubled_carriage_returns():
    data = GITIGNORE.read_bytes()

    assert b"\r\r\n" not in data, (
        ".gitignore contains \\r\\r\\n line endings; Git will stop matching "
        "those patterns. Restore LF or CRLF endings."
    )


@pytest.mark.parametrize("relative_path", MUST_BE_IGNORED)
def test_local_artifact_paths_stay_ignored(git_work_tree, relative_path):
    status = _git_check_ignore(relative_path)

    assert status == 0, "{} is no longer ignored (git check-ignore exit {})".format(
        relative_path, status
    )


@pytest.mark.parametrize("relative_path", MUST_STAY_TRACKABLE)
def test_project_claude_settings_stay_trackable(git_work_tree, relative_path):
    status = _git_check_ignore(relative_path)

    assert status == 1, "{} must not be ignored (git check-ignore exit {})".format(
        relative_path, status
    )
