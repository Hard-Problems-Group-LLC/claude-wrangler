"""Tests for the transaction scratch environment, smoke test, and help reader."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from claude_wrangler import platforms
from claude_wrangler.services import (
    SmokeTestError,
    expected_version_output,
    host_os_family,
    parse_help_subcommands,
    probe_settings_rejections,
    read_help_subcommands,
    run_smoke_test,
    scratch_environment,
    settings_probe_document,
)

from native_fakes import CSC_PATH, compile_fake_claude, posix_fake_claude

REAL_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --add-dir <directories...>            Additional directories to allow tool
                                        access to
  -h, --help                            Display help for command

Commands:
  agents [options]                      Manage background agents
  attach <id|name>                      Open a background session in this
                                        terminal. <id> is the short id
  auth                                  Manage authentication
  auto-mode                             Inspect or reset auto mode classifier
  doctor                                Check the health of your Claude Code
  mcp                                   Configure and manage MCP servers
  plugin|plugins                        Manage Claude Code plugins
  setup-token                           Set up a long-lived authentication token
  stop|kill <id>                        Stop a background session.
  update|upgrade                        Check for updates and install if
                                        available
"""


def test_help_parser_reads_names_and_aliases_from_commands_only():
    assert parse_help_subcommands(REAL_HELP) == [
        "agents",
        "attach",
        "auth",
        "auto-mode",
        "doctor",
        "mcp",
        "plugin",
        "plugins",
        "setup-token",
        "stop",
        "kill",
        "update",
        "upgrade",
    ]


def test_help_parser_ignores_text_without_a_commands_section():
    assert parse_help_subcommands("Usage: x\n\nOptions:\n  -h  help\n") == []
    assert parse_help_subcommands("Commands:\n  BAD!name  x\n  ok  y\n") == ["ok"]


def test_help_parser_stops_at_the_next_section():
    text = "Commands:\n  mcp  x\n\nExamples:\n  notacommand  y\n"
    assert parse_help_subcommands(text) == ["mcp"]


def test_scratch_environment_never_names_real_homes_or_project_state(tmp_path):
    real_home = tmp_path / "real-home"
    project_config = tmp_path / "project" / ".local" / "claude"
    scratch = tmp_path / "scratch"
    base = {
        "HOME": str(real_home),
        "USERPROFILE": str(real_home),
        "HOMEDRIVE": "C:",
        "HOMEPATH": "\\Users\\op",
        "HOMESHARE": "\\\\server\\op",
        "APPDATA": str(real_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(real_home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(real_home / ".config"),
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "CLAUDE_CONFIG_DIR": str(project_config),
        "CLAUDE_LOCAL_NOTICES_SHOWN": str(project_config),
        "CLAUDECODE": "1",
        "ANTHROPIC_API_KEY": "secret",
        "TMP": str(real_home / "tmp"),
        "TEMP": str(real_home / "tmp"),
        "TMPDIR": str(real_home / "tmp"),
        "ONEDRIVE": str(real_home / "OneDrive"),
        "PATH": os.pathsep.join(
            [
                str(real_home / "bin"),
                str(project_config / "wrangler" / "shim"),
                "/usr/bin",
            ]
        ),
        "KEEP_ME": "yes",
    }

    env = scratch_environment(
        scratch, base, forbidden_roots=[str(real_home), str(project_config)]
    )

    for key, value in env.items():
        for part in value.split(os.pathsep):
            assert not part.startswith(str(real_home)), (key, value)
            assert not part.startswith(str(project_config)), (key, value)
    assert env["HOME"] == str(scratch / "home")
    assert env["CLAUDE_CONFIG_DIR"] == str(scratch / "config")
    for name in ("TMP", "TEMP", "TMPDIR"):
        assert env[name] == str(scratch / "tmp")
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
    ):
        assert env[name].startswith(str(scratch))
    assert env["XDG_RUNTIME_DIR"].startswith(str(scratch))
    assert "CLAUDECODE" not in env and "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_LOCAL_NOTICES_SHOWN" not in env
    assert "HOMESHARE" not in env
    assert env["KEEP_ME"] == "yes"
    assert env["DISABLE_UPDATES"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DISABLE_INSTALLATION_CHECKS"] == "1"
    assert env["PATH"].split(os.pathsep) == ["/usr/bin"]
    if os.name == "nt":
        assert env["USERPROFILE"] == env["HOME"]
        assert env["APPDATA"].startswith(str(scratch))
        assert env["LOCALAPPDATA"].startswith(str(scratch))
        assert (scratch / "home" / "AppData" / "Roaming").is_dir()


@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_scratch_environment_shape_per_os(tmp_path, os_name):
    env = scratch_environment(tmp_path, {}, os_name=os_name)
    if os_name == "nt":
        assert {
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "APPDATA",
            "LOCALAPPDATA",
        } <= set(env)
    else:
        assert "USERPROFILE" not in env and "APPDATA" not in env


def test_expected_version_output():
    assert expected_version_output("2.1.296") == "2.1.296 (Claude Code)"


def test_host_os_family():
    assert host_os_family("win32") == "win32"
    assert host_os_family("linux") == "linux"
    assert host_os_family("darwin") == "darwin"
    assert platforms.host_platform_key().startswith(host_os_family())


@pytest.fixture(scope="module")
def fake_binary(tmp_path_factory) -> Path:
    """Return an executable fake Claude Code for this host."""

    directory = tmp_path_factory.mktemp("fake")
    if os.name == "nt":
        if not CSC_PATH.is_file():
            pytest.skip("csc.exe is not installed")
        return compile_fake_claude(directory / "claude.exe")
    path = directory / "claude"
    path.write_bytes(posix_fake_claude())
    path.chmod(0o755)
    return path


def test_smoke_test_accepts_the_exact_version_line(fake_binary, tmp_path):
    env = scratch_environment(tmp_path, dict(os.environ))
    run_smoke_test(fake_binary, "2.1.296", env, tmp_path)


def test_smoke_test_rejects_another_version(fake_binary, tmp_path):
    env = scratch_environment(tmp_path, dict(os.environ))
    with pytest.raises(
        SmokeTestError, match="expected exactly '2.1.207 \\(Claude Code\\)'"
    ):
        run_smoke_test(fake_binary, "2.1.207", env, tmp_path)


def test_smoke_test_rejects_a_program_that_is_not_claude(tmp_path):
    env = scratch_environment(tmp_path, dict(os.environ))
    with pytest.raises(SmokeTestError):
        run_smoke_test(Path(sys.executable), "2.1.296", env, tmp_path)


def test_smoke_test_reports_a_timeout(fake_binary, tmp_path, monkeypatch):
    from claude_wrangler import runtime, services

    def slow(command, *, env, cwd, timeout_seconds):
        """Pretend the binary never finished."""

        return runtime.CompletedRun(command, None, "", "", timed_out=True)

    monkeypatch.setattr(services, "run_bounded", slow)
    with pytest.raises(SmokeTestError, match="did not finish within 60 seconds"):
        run_smoke_test(fake_binary, "2.1.296", {}, tmp_path)


def test_help_reader_runs_help_in_scratch(fake_binary, tmp_path):
    env = scratch_environment(tmp_path, dict(os.environ))
    names = read_help_subcommands(fake_binary, env, tmp_path)
    assert names == ["doctor", "mcp", "plugin", "plugins", "update", "upgrade"]


# ---------------------------------------------------------------------------
# --settings acceptance probe (cross_platform_launcher.md requirement 4)
# ---------------------------------------------------------------------------


def test_settings_probe_accepts_subcommands_that_print_their_own_usage(
    fake_binary, tmp_path
):
    env = scratch_environment(tmp_path, dict(os.environ))
    names = ["doctor", "mcp", "plugin", "plugins", "update", "upgrade"]
    assert probe_settings_rejections(fake_binary, env, tmp_path, names) == []


def test_settings_probe_records_subcommands_that_reject_settings(fake_binary, tmp_path):
    env = scratch_environment(tmp_path, dict(os.environ))
    env["FAKE_CLAUDE_REJECT_SETTINGS"] = "mcp,plugins"
    names = ["doctor", "mcp", "plugin", "plugins"]
    assert probe_settings_rejections(fake_binary, env, tmp_path, names) == [
        "mcp",
        "plugins",
    ]


def test_settings_probe_classifies_outcomes_and_skips_intercepted(
    tmp_path, monkeypatch
):
    from claude_wrangler import rendering, runtime, services

    calls = []

    def fake_run(command, *, env, cwd, timeout_seconds):
        """Answer each probe like a different Claude Code behavior."""

        calls.append(list(command))
        name = command[3]
        if name == "mcp":
            return runtime.CompletedRun(command, 0, "Usage: claude mcp [options]\n", "")
        if name == "plugins":
            usage = "Usage: claude plugin|plugins [options] [command]\n"
            return runtime.CompletedRun(command, 0, usage, "")
        if name == "doctor":
            # The root's help means --settings swallowed the subcommand.
            usage = "Usage: claude [options] [command] [prompt]\n"
            return runtime.CompletedRun(command, 0, usage, "")
        if name == "slow":
            return runtime.CompletedRun(command, None, "", "", timed_out=True)
        return runtime.CompletedRun(command, 1, "", "error: unknown option")

    monkeypatch.setattr(services, "run_bounded", fake_run)
    names = ["mcp", "plugins", "doctor", "slow", "auth", "update", "upgrade", "install"]

    rejected = probe_settings_rejections(Path("claude"), {}, tmp_path, names)

    assert rejected == ["doctor", "slow", "auth"]
    assert [call[3] for call in calls] == ["mcp", "plugins", "doctor", "slow", "auth"]
    for call in calls:
        assert call[1] == "--settings" and call[-1] == "--help"
        assert json.loads(call[2]) == rendering.settings_override()
    assert calls[0][2] == settings_probe_document()


def test_settings_probe_budget_rejects_without_running(tmp_path, monkeypatch):
    from claude_wrangler import services

    def never(command, *, env, cwd, timeout_seconds):
        """Fail if a probe runs after the budget is spent."""

        raise AssertionError("probe ran after the budget was spent")

    monkeypatch.setattr(services, "run_bounded", never)
    assert probe_settings_rejections(
        Path("claude"), {}, tmp_path, ["mcp", "doctor"], budget_seconds=0
    ) == ["mcp", "doctor"]


# git tracked-file query ------------------------------------------------------


def tracked_repository(tmp_path: Path) -> Path:
    """Return a git work tree that tracks `.LOCAL/claude/settings.json`."""

    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    tracked = root / ".LOCAL" / "claude" / "settings.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("{}", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-f", ".LOCAL/claude/settings.json"]):
        subprocess.run(
            ["git", "-c", "core.longpaths=true", "-C", str(root)] + args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=True,
        )
    return root


def test_tracked_file_query_matches_case_variants_when_names_ignore_case(
    tmp_path, monkeypatch
):
    from claude_wrangler.services import list_tracked_files

    root = tracked_repository(tmp_path)
    managed = [root / ".local" / "claude"]

    assert list_tracked_files(root, managed, ignore_case=True) == [
        ".LOCAL/claude/settings.json"
    ]
    assert list_tracked_files(root, managed, ignore_case=False) == []
    # A caller's pathspec switches never turn the magic into a literal path.
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    assert list_tracked_files(root, managed, ignore_case=True) == [
        ".LOCAL/claude/settings.json"
    ]
    monkeypatch.delenv("GIT_LITERAL_PATHSPECS")
    monkeypatch.setenv("GIT_GLOB_PATHSPECS", "1")
    assert list_tracked_files(root, managed, ignore_case=True) == [
        ".LOCAL/claude/settings.json"
    ]


def test_tracked_file_query_keeps_wildcards_literal(tmp_path):
    from claude_wrangler.services import list_tracked_files

    root = tracked_repository(tmp_path)

    assert list_tracked_files(root, [root / ".LOCAL" / "c*"], ignore_case=True) == []
    assert list_tracked_files(root, [root / ".LOCAL" / "claude"]) == [
        ".LOCAL/claude/settings.json"
    ]


@pytest.mark.skipif(
    os.name != "nt" and sys.platform != "darwin", reason="default case folding"
)
def test_windows_and_macos_filesystems_ignore_case(tmp_path):
    from claude_wrangler.services import filesystem_ignores_case

    assert filesystem_ignores_case(tmp_path) is True
