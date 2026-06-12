# ruff: noqa: E402
"""Shared fixtures for claude-wrangler tests."""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_wrangler.constants import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_HOME_DIR,
    DEFAULT_INSTALL_CLAUDE_CHANNEL,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    DEFAULT_LOCAL_DIR,
)
from claude_wrangler.layout import build_layout
from claude_wrangler.models import Config


@pytest.fixture
def config_factory():
    def factory(project_root: Path, **overrides) -> Config:
        layout = build_layout(
            project_root,
            overrides.pop("local_dir_raw", DEFAULT_LOCAL_DIR),
            overrides.pop("claude_home_raw", DEFAULT_HOME_DIR),
            overrides.pop("config_dir_raw", DEFAULT_CONFIG_DIR),
            overrides.pop("launcher_raw", "bin/claude-local"),
            overrides.pop("readme_raw", "README-LOCAL-Start-Claude.md"),
        )
        defaults = {
            "operation": "install",
            "project_root": project_root,
            "claude_selector": DEFAULT_INSTALL_CLAUDE_SELECTOR,
            "claude_channel": DEFAULT_INSTALL_CLAUDE_CHANNEL,
            "claude_version": DEFAULT_INSTALL_CLAUDE_SELECTOR,
            "shared_home": False,
            "skip_install": False,
            "force": False,
            "dry_run": False,
            "layout": layout,
            "version_source": "test",
            "reasonable_permissions_enabled": False,
            "reconfigure_only": False,
        }
        defaults.update(overrides)
        return Config(**defaults)

    return factory
