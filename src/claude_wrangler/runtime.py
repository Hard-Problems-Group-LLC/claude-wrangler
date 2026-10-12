"""Small runtime helpers for bounded process execution and operator output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
from typing import Mapping, Optional, Sequence

from .models import ClaudeWranglerError

# Upper bound on captured output per stream; `--help` is a few kilobytes.
MAX_CAPTURED_BYTES = 1024 * 1024


def format_utc(moment: datetime) -> str:
    """Render an aware datetime as an ISO 8601 UTC timestamp ending in Z."""

    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def utc_now_iso() -> str:
    """Return a UTC timestamp suitable for JSON metadata and reports."""

    return format_utc(datetime.now(timezone.utc))


def eprint(message: str) -> None:
    """Send operator-facing status output to stderr."""

    print(message, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class CompletedRun:
    """Outcome of one bounded child process."""

    command: Sequence[str]
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False


def run_bounded(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    timeout_seconds: float,
) -> CompletedRun:
    """Run a command with stdin closed, captured output, and a hard timeout.

    Output goes to temporary files rather than pipes so a detached
    grandchild that inherits the handles cannot stall the wait. On timeout
    the process is killed and `timed_out` is set. A command that cannot
    start raises `ClaudeWranglerError`.
    """

    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
            )
        except OSError as exc:
            raise ClaudeWranglerError(
                "Cannot run {}: {}".format(command[0] if command else "?", exc)
            ) from exc
        timed_out = False
        try:
            returncode: Optional[int] = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                returncode = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                returncode = None
        out_file.seek(0)
        err_file.seek(0)
        stdout = out_file.read(MAX_CAPTURED_BYTES).decode("utf-8", "replace")
        stderr = err_file.read(MAX_CAPTURED_BYTES).decode("utf-8", "replace")
    return CompletedRun(list(command), returncode, stdout, stderr, timed_out)
