"""Tests for rendered metadata, the .gitignore block, README, and summaries.

Launcher behavior is exercised through the generated `launch.py` (the
byte-for-byte copy of `launcher_core.py`) with exec/spawn doubles, replacing
the npm-era bash launcher tests.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from claude_wrangler import launcher_core
from claude_wrangler.constants import GITIGNORE_BEGIN, GITIGNORE_END, SCHEMA_VERSION
from claude_wrangler.operations import install_like_operation
from claude_wrangler.rendering import (
    build_available_versions_table,
    build_gitignore_block,
    build_install_summary,
    build_local_readme_content,
    build_metadata,
)

from native_fakes import make_config, make_services, standard_source


@pytest.fixture
def config(tmp_path: Path):
    """Return a default install config for a fresh project."""

    project = tmp_path / "project"
    project.mkdir()
    return make_config(project)


def test_gitignore_block_covers_both_launchers_and_not_dot_claude(config):
    block = build_gitignore_block(config.layout)
    lines = block.splitlines()
    assert lines[0] == GITIGNORE_BEGIN and lines[-1] == GITIGNORE_END
    assert ".local/" in lines
    assert "bin/claude-local" in lines
    assert "bin/claude-local.cmd" in lines
    assert "README-LOCAL-Start-Claude.md" in lines
    assert ".claude/" not in lines and ".claude" not in lines


def test_gitignore_block_adds_relocated_directories(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(
        project, "--local-dir", "tools/claude-rt", "--launcher", "scripts/cl"
    )
    lines = build_gitignore_block(config.layout).splitlines()
    assert "tools/claude-rt/" in lines
    assert "scripts/cl" in lines and "scripts/cl.cmd" in lines


def test_metadata_is_schema_two_with_native_fields(config):
    meta = build_metadata(config, generated_at="2026-10-12T00:00:00Z")
    assert meta["schema_version"] == SCHEMA_VERSION == 2
    for key in (
        "claude_selector",
        "claude_channel",
        "claude_version",
        "shared_home",
        "reasonable_permissions_enabled",
        "update_checks",
        "windows_sandbox_warning",
        "credential_passthrough",
        "release_base_url",
        "platform",
        "launcher_python",
        "config_dir_spelling",
        "generated_at",
        "legacy_migrated_from",
        "legacy_migrated_at",
        "last_update_check",
        "available_versions",
        "available_version_details",
        "paths",
    ):
        assert key in meta, key
    assert meta["available_versions"] == {"stable": None, "latest": None}
    assert meta["paths"]["launcher_cmd"] == "bin/claude-local.cmd"
    assert meta["generated_at"] == "2026-10-12T00:00:00Z"


def test_readme_documents_the_native_runtime(config):
    text = build_local_readme_content(config)
    assert text.startswith("<!-- managed by claude-wrangler -->")
    for snippet in (
        "Node.js and npm are not used",
        "./bin/claude-local",
        ".\\bin\\claude-local",
        "Ring 1: unsandboxed",
        "USERPROFILE, HOMEDRIVE/HOMEPATH, APPDATA",
        "Claude Code has no sandbox there",
        "--set-windows-sandbox-warning",
        "--rollback",
        "--repair-install",
        "--update",
        "--check-updates",
        "contact your administrator",
        "use an npm-era",
        "--uninstall",
        "--stop-background-sessions",
        "CLAUDE_LOCAL_PREFLIGHT=strict",
        "--permission-mode acceptEdits",
    ):
        assert snippet in text, snippet
    assert "npm audit" not in text
    assert "NPM_CONFIG_CACHE" not in text
    assert "next" not in text.split("## Installed Version")[1].split("##")[0]


def test_readme_names_shared_ring1_stores_and_cmd_reparsing(config):
    text = build_local_readme_content(config)
    for snippet in (
        "OpenSSH keys, config, and",
        "PowerShell module path",
        "machine-wide managed settings",
        "launcher therefore removes them",
        "Passthrough is currently off.",
        "--set-credential-passthrough on .",
        "cmd.exe re-parses the arguments of every `.cmd` file",
        "`.local/claude/wrangler/launch.py`",
        "a nested `claude` runs this project's",
    ):
        assert snippet in text, snippet


def test_readme_flags_a_pending_migration(config):
    assert "Migration pending" in build_local_readme_content(
        config, legacy_pending=True
    )
    assert "Migration pending" not in build_local_readme_content(config)


def test_install_summary_and_versions_table(config):
    summary = build_install_summary(config, "installed Claude Code 2.1.287 for x")
    assert "Runtime: installed Claude Code 2.1.287 for x" in summary
    assert "claude-local.cmd" in summary
    table = build_available_versions_table(
        {"stable": "2.1.287", "latest": None},
        {"stable": {"build_date": "2026-10-01T00:00:00Z"}},
    )
    lines = table.splitlines()
    assert lines[0].split() == ["Channel", "Known", "version", "Build", "date"]
    assert lines[2].split() == ["stable", "2.1.287", "2026-10-01T00:00:00Z"]
    assert lines[3].split() == ["latest", "unavailable", "-"]


# ---------------------------------------------------------------------------
# The generated launcher (launch.py) for permission and ring behavior
# ---------------------------------------------------------------------------


class Captured:
    """Captures what the launcher would run."""

    def __init__(self) -> None:
        """Start with no calls."""

        self.calls: List[Tuple[List[str], Dict[str, str]]] = []

    def exec_fn(self, binary: str, argv: List[str], env: Dict[str, str]) -> int:
        """Record a POSIX exec."""

        self.calls.append((list(argv), dict(env)))
        return 0

    def spawn_fn(self, argv: List[str], env: Dict[str, str]) -> int:
        """Record a Windows spawn."""

        self.calls.append((list(argv), dict(env)))
        return 0


def launch(
    project: Path, args: List[str], env: Dict[str, str] = None
) -> Tuple[List[str], Dict[str, str]]:
    """Run the generated launch.py in-process with exec/spawn doubles."""

    captured = Captured()
    code = launcher_core.main(
        args,
        environ=dict(env or {"PATH": ""}),
        os_name=os.name,
        platform_is_wsl=False,
        core_path=str(project / ".local" / "claude" / "wrangler" / "launch.py"),
        exec_fn=captured.exec_fn,
        spawn_fn=captured.spawn_fn,
        stderr=io.StringIO(),
        stdout=io.StringIO(),
        node_probe=lambda env, os_name: (None, None),
    )
    assert code == 0
    return captured.calls[0]


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    """Return a project with a fake release installed."""

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    config = make_config(
        project, "--set-update-checks", "off", "--set-windows-sandbox-warning", "off"
    )
    assert install_like_operation(config, make_services(standard_source())) == 0
    return project


def reconfigure(project: Path, *flags: str) -> None:
    """Toggle a setting through the CLI configuration."""

    assert (
        install_like_operation(
            make_config(project, *flags), make_services(standard_source())
        )
        == 0
    )


def test_enabled_launcher_injects_reasonable_permissions_default(installed):
    reconfigure(installed, "--set-reasonable-permissions")
    argv, _env = launch(installed, ["-p", "hi"])
    assert argv[3:] == ["--permission-mode", "acceptEdits", "-p", "hi"]


@pytest.mark.parametrize(
    "caller_args",
    [
        ["--permission-mode", "plan"],
        ["--permission-mode=default"],
        ["--dangerously-skip-permissions"],
    ],
)
def test_enabled_launcher_preserves_explicit_permission_choices(installed, caller_args):
    reconfigure(installed, "--set-reasonable-permissions")
    argv, _env = launch(installed, caller_args)
    assert argv[3:] == caller_args
    assert "acceptEdits" not in argv


def test_clear_regenerates_a_launcher_without_the_default(installed):
    reconfigure(installed, "--set-reasonable-permissions")
    reconfigure(installed, "--clear-reasonable-permissions")
    argv, _env = launch(installed, ["-p", "hi"])
    assert "--permission-mode" not in argv
    launch_json = json.loads(
        (installed / ".local" / "claude" / "wrangler" / "launch.json").read_text(
            "utf-8"
        )
    )
    assert launch_json["reasonable_permissions"] is False


def test_launcher_redirects_home_and_pins_config_dir_by_default(installed):
    _argv, env = launch(installed, [])
    config_dir = str(installed / ".local" / "claude")
    assert env["CLAUDE_CONFIG_DIR"] == config_dir
    assert env["HOME"] == os.path.join(config_dir, "HOME")
    assert env["XDG_CONFIG_HOME"] == os.path.join(config_dir, "HOME", ".config")
    assert env["DISABLE_UPDATES"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DISABLE_INSTALLATION_CHECKS"] == "1"


def test_launcher_shared_home_flag_keeps_real_home_and_config_pin(installed):
    _argv, env = launch(
        installed, ["--shared-home"], {"PATH": "", "HOME": "/real/home"}
    )
    assert env["HOME"] == "/real/home"
    assert env["CLAUDE_CONFIG_DIR"] == str(installed / ".local" / "claude")


def test_launcher_shared_home_environment_variable_reaches_ring_zero(installed):
    _argv, env = launch(
        installed,
        [],
        {"PATH": "", "HOME": "/real/home", "CLAUDE_LOCAL_SHARED_HOME": "1"},
    )
    assert env["HOME"] == "/real/home"


def test_launcher_isolated_home_flag_overrides_shared_default(installed):
    reconfigure(installed, "--shared-home")
    _argv, env = launch(
        installed, ["--isolated-home"], {"PATH": "", "HOME": "/real/home"}
    )
    assert env["HOME"] == os.path.join(str(installed / ".local" / "claude"), "HOME")


def test_launcher_refuses_when_no_runtime_slot_is_usable(installed):
    record = (
        installed / ".local" / "claude" / "wrangler" / "slots" / "a" / ".complete.json"
    )
    record.unlink()
    stderr = io.StringIO()
    code = launcher_core.main(
        [],
        environ={"PATH": ""},
        os_name=os.name,
        platform_is_wsl=False,
        core_path=str(installed / ".local" / "claude" / "wrangler" / "launch.py"),
        exec_fn=lambda *a: pytest.fail("must not exec"),
        spawn_fn=lambda *a: pytest.fail("must not spawn"),
        stderr=stderr,
        node_probe=lambda env, os_name: (None, None),
    )
    assert code == 1
    assert "--repair-install" in stderr.getvalue()
    assert "never falls back to a global claude" in stderr.getvalue()


def test_launcher_metadata_is_stable_across_regeneration(installed):
    rt = installed / ".local" / "claude" / "wrangler"
    before = (rt / "launch.json").read_bytes()
    reconfigure(installed, "--set-update-checks", "off")
    assert (rt / "launch.json").read_bytes() == before
