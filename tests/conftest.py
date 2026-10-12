# ruff: noqa: E402
"""Shared fixtures for claude-wrangler tests.

Every test runs with its home-like environment variables redirected into
a fresh per-test directory (see `redirected_home`). This is a safety net
under `scripts/run_tests_isolated.py`: on Windows, `Path.home()` and
native programs read `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA` rather
than `HOME`, so a test that redirected only `HOME` could still write into
the operator's real profile. Mark a test with `@pytest.mark.real_home_env`
to opt out when it must observe the unmodified process environment.
"""

import os
from pathlib import Path
import sys
from typing import Optional

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

REAL_HOME_ENV_MARKER = "real_home_env"

# XDG base directories redirected under the per-test home, relative to it.
XDG_BASE_DIRECTORIES = (
    ("XDG_CONFIG_HOME", (".config",)),
    ("XDG_CACHE_HOME", (".cache",)),
    ("XDG_STATE_HOME", (".local", "state")),
    ("XDG_DATA_HOME", (".local", "share")),
)


def pytest_configure(config):
    """Register the opt-out marker so `--strict-markers` accepts it."""

    config.addinivalue_line(
        "markers",
        "{}: run without the autouse per-test home redirection".format(
            REAL_HOME_ENV_MARKER
        ),
    )


def redirect_home_environment(monkeypatch, home: Path) -> None:
    """Point every home-like environment variable at one directory.

    Sets `HOME`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH` (split from the same
    path so they agree with `USERPROFILE`), `APPDATA`, `LOCALAPPDATA`, and
    the XDG base directories, creating each directory so code that expects
    them to exist behaves as it would in a real profile.
    """

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    drive, tail = os.path.splitdrive(str(home))
    if drive:
        monkeypatch.setenv("HOMEDRIVE", drive)
        monkeypatch.setenv("HOMEPATH", tail or "\\")
    else:
        # POSIX paths have no drive; stale Windows-style values must not
        # survive and name some other home.
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)

    appdata = home / "AppData" / "Roaming"
    localappdata = home / "AppData" / "Local"
    appdata.mkdir(parents=True, exist_ok=True)
    localappdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    for name, parts in XDG_BASE_DIRECTORIES:
        directory = home.joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(directory))


@pytest.fixture(autouse=True)
def redirected_home(request, monkeypatch, tmp_path_factory) -> Optional[Path]:
    """Redirect home-like variables to a per-test directory; return it.

    The directory comes from `tmp_path_factory`, not `tmp_path`, so a
    test's own `tmp_path` stays empty. Returns None for tests marked
    `real_home_env`.
    """

    if request.node.get_closest_marker(REAL_HOME_ENV_MARKER) is not None:
        return None
    home = tmp_path_factory.mktemp("home")
    redirect_home_environment(monkeypatch, home)
    return home


@pytest.fixture
def config_factory():
    """Return a factory that builds a Config for a project root."""

    def factory(project_root: Path, **overrides) -> Config:
        """Build a Config with test defaults, applying keyword overrides."""

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
