from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import scripts.git_commit_with_changelog as git_commit_with_changelog

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "git_commit_with_changelog.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_helper_command_targets_theknowledge_script() -> None:
    command = git_commit_with_changelog.helper_command(["-m", "example"])

    assert command[0] == sys.executable
    assert command[1].endswith("TheKnowledge/scripts/git_standard_commit_push.py")
    assert command[-2:] == ["-m", "example"]


def test_main_reports_dry_run_sync(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        git_commit_with_changelog,
        "sync_changelog_from_pending_queue",
        lambda repo_root, pending_queue_path, dry_run=False: (
            repo_root / "CHANGELOG.md",
            2,
            True,
        ),
    )
    monkeypatch.setattr(
        git_commit_with_changelog.subprocess,
        "run",
        lambda command, cwd, check=False: subprocess.CompletedProcess(command, 0),
    )

    result = git_commit_with_changelog.main(["--dry-run", "-m", "example"])

    assert result == 0
    captured = capsys.readouterr()
    assert "Would sync 2 pending queue entries into CHANGELOG.md." in captured.out


def test_script_help_delegates() -> None:
    result = _run("--help")

    assert result.returncode == 0
    assert "git_standard_commit_push.py" not in result.stderr
    assert "--assume-reviewed" in result.stdout
