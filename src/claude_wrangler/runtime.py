"""Small runtime helpers for process execution and operator output."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import shutil
import subprocess
import sys
from typing import Mapping, Optional, Sequence, Tuple

from .models import ClaudeWranglerError


def utc_now_iso() -> str:
    """Return a UTC timestamp suitable for JSON metadata and reports."""

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def eprint(message: str) -> None:
    """Send operator-facing status output to stderr."""

    print(message, file=sys.stderr, flush=True)


def detect_npm_binaries() -> Tuple[str, str]:
    """Return platform-correct executable names for npm and npx."""

    if os.name == "nt":
        return "npm.cmd", "npx.cmd"
    return "npm", "npx"


def ensure_command_exists(name: str) -> None:
    """Raise a clear error when a required external command is missing."""

    if shutil.which(name) is None:
        raise ClaudeWranglerError(
            "Required command {!r} was not found in PATH. Install Node.js/npm "
            "first and rerun this script.".format(name)
        )


def run_command(
    command: Sequence[str],
    cwd: str,
    capture_output: bool = False,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with explicit error reporting."""

    if env is not None and "NPM_CONFIG_CACHE" in env:
        eprint(
            "[claude-wrangler] Environment: NPM_CONFIG_CACHE={}".format(
                env["NPM_CONFIG_CACHE"]
            )
        )
    eprint("[claude-wrangler] Running: {}".format(" ".join(command)))
    if timeout_seconds is not None:
        eprint("[claude-wrangler] Timeout: {}s".format(timeout_seconds))
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=capture_output,
            env=dict(env) if env is not None else None,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeWranglerError(
            "Command timed out after {}s: {}".format(
                timeout_seconds,
                " ".join(command),
            )
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip() if completed.stderr else ""
        detail = "\n{}".format(stderr) if stderr else ""
        raise ClaudeWranglerError(
            "Command failed with exit code {}: {}{}".format(
                completed.returncode,
                " ".join(command),
                detail,
            )
        )
    return completed
