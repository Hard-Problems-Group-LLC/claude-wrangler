"""Tests for the shared subprocess runtime helpers."""

import subprocess

import pytest

from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.runtime import run_command


def test_run_command_reports_timeout(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr("claude_wrangler.runtime.subprocess.run", fake_run)

    with pytest.raises(ClaudeWranglerError, match="timed out after 5s"):
        run_command(["npm", "install"], cwd=str(tmp_path), timeout_seconds=5)
