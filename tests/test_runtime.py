"""Tests for the bounded process runner and timestamp helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys

import pytest

from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.runtime import format_utc, run_bounded, utc_now_iso


def test_run_bounded_captures_output_and_exit_code(tmp_path):
    completed = run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        env={"PATH": ""} if sys.platform != "win32" else {"SYSTEMROOT": "C:\\Windows"},
        cwd=str(tmp_path),
        timeout_seconds=60,
    )
    assert completed.returncode == 3
    assert completed.stdout.strip() == "out"
    assert completed.stderr.strip() == "err"
    assert completed.timed_out is False


def test_run_bounded_kills_on_timeout(tmp_path):
    completed = run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=dict(__import__("os").environ),
        cwd=str(tmp_path),
        timeout_seconds=0.5,
    )
    assert completed.timed_out is True


def test_run_bounded_reports_a_missing_program(tmp_path):
    with pytest.raises(ClaudeWranglerError, match="Cannot run"):
        run_bounded(
            [str(tmp_path / "missing-program")],
            env={},
            cwd=str(tmp_path),
            timeout_seconds=5,
        )


def test_run_bounded_closes_stdin(tmp_path):
    completed = run_bounded(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        env=dict(__import__("os").environ),
        cwd=str(tmp_path),
        timeout_seconds=60,
    )
    assert completed.stdout.strip() == "''"


def test_utc_timestamps():
    moment = datetime(2026, 10, 12, 5, 6, 7, 999, tzinfo=timezone(timedelta(hours=-7)))
    assert format_utc(moment) == "2026-10-12T12:06:07Z"
    assert utc_now_iso().endswith("Z")
