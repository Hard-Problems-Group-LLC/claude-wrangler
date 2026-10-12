"""Tests for argument parsing and configuration normalization."""

import argparse
import json
import os
from pathlib import Path

import pytest

from claude_wrangler.config import (
    MISSING_DASH_FLAG_HINTS,
    build_parser,
    config_from_args,
    determine_shared_home,
    determine_target_selection,
    normalize_requested_version,
    parse_args,
    parse_positive_int,
)
from claude_wrangler.constants import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_INSTALL_CLAUDE_CHANNEL,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    DEFAULT_LOCAL_DIR,
    LEGACY_LOCAL_DIR,
    METADATA_FILENAME,
)
from claude_wrangler.distribution import DEFAULT_RELEASE_BASE_URL
from claude_wrangler.layout import resolve_relative_within_root
from claude_wrangler.models import ClaudeWranglerError, ExistingState


def write_native_metadata(project, **fields):
    """Write schema-2 metadata at the default runtime root."""

    payload = {
        "schema_version": 2,
        "claude_selector": "latest",
        "claude_channel": "stable",
        "claude_version": "2.1.287",
        "shared_home": False,
        "reasonable_permissions_enabled": False,
        "generated_at": "2026-10-01T00:00:00Z",
    }
    payload.update(fields)
    path = project / DEFAULT_LOCAL_DIR / METADATA_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def parsed(*argv):
    """Parse argv with a default project root."""

    return parse_args(list(argv))


def test_normalize_requested_version_accepts_case_insensitive_latest():
    assert normalize_requested_version("LATEST") == "latest"
    assert normalize_requested_version("  latest  ") == "latest"
    assert normalize_requested_version("2.1.287") == "2.1.287"
    assert normalize_requested_version("  ") is None


def test_parse_positive_int_rejects_non_positive_values():
    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        parse_positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        parse_positive_int("x")


def test_default_local_dir_is_the_native_runtime_root():
    assert DEFAULT_LOCAL_DIR == ".local/claude/wrangler"
    assert LEGACY_LOCAL_DIR == ".local/claude/npm"


def test_channel_next_is_a_deprecated_alias_of_latest(capsys):
    args = parsed("--upgrade", "--channel", "next")
    assert args.channel == "latest"
    assert args.channel_deprecated is True
    assert "deprecated" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["2.1", "2.1.x", "v2.1.300", "stable", "2.1.300-beta"])
def test_version_must_be_exact_or_latest(bad):
    with pytest.raises(SystemExit):
        parsed("--version", bad)


def test_version_below_the_floor_is_refused_at_parse_time(capsys):
    with pytest.raises(SystemExit):
        parsed("--upgrade", "--channel", "stable", "--version", "2.1.206")
    assert "2.1.207" in capsys.readouterr().err
    assert parsed("--version", "2.1.207").requested_version == "2.1.207"


def test_parse_args_requires_upgrade_channel():
    with pytest.raises(SystemExit):
        parsed("--upgrade")


def test_parse_args_rejects_update_version_and_channel_requests():
    with pytest.raises(SystemExit):
        parsed("--update", "--version", "2.1.287")
    with pytest.raises(SystemExit):
        parsed("--update", "--channel", "stable")


def test_parse_args_keeps_bare_update_as_project_root():
    args = parsed("update")
    assert args.project_root == "update"
    assert args.update is False


def test_config_from_args_suggests_missing_dashes_for_bare_update():
    with pytest.raises(ClaudeWranglerError, match="Did you perhaps mean '--update'"):
        config_from_args(parsed("update"))


@pytest.mark.parametrize("flag", ["rollback", "stop-background-sessions", "platform"])
def test_config_from_args_suggests_missing_dashes_for_new_flags(flag):
    with pytest.raises(ClaudeWranglerError, match="--{}".format(flag)):
        config_from_args(parsed(flag))


def test_missing_dash_hints_cover_every_long_flag():
    parser = build_parser()
    long_flags = {
        option[2:]
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }
    assert long_flags <= set(MISSING_DASH_FLAG_HINTS)


def test_config_from_args_allows_existing_project_named_update(tmp_path, monkeypatch):
    project = tmp_path / "update"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    config = config_from_args(parsed("update"))
    assert config.project_root == project.resolve()
    assert config.operation == "install"


def test_rollback_is_an_operation_and_rejects_install_flags():
    assert parsed("--rollback").rollback is True
    for extra in (
        ["--channel", "stable"],
        ["--version", "2.1.287"],
        ["--skip-install"],
    ):
        with pytest.raises(SystemExit):
            parsed("--rollback", *extra)
    with pytest.raises(SystemExit):
        parsed("--rollback", "--platform", "linux-x64")


@pytest.mark.parametrize(
    "operation",
    ["--inspect", "--selftest", "--uninstall", "--update", "--rollback"],
)
@pytest.mark.parametrize(
    "setting",
    [
        ["--set-reasonable-permissions"],
        ["--clear-reasonable-permissions"],
        ["--set-update-checks", "off"],
        ["--set-windows-sandbox-warning", "on"],
    ],
)
def test_settings_flags_are_only_valid_for_install_like_operations(operation, setting):
    with pytest.raises(SystemExit):
        parsed(operation, *setting)


def test_network_flags_are_refused_for_offline_operations(tmp_path):
    for operation in ("--inspect", "--selftest", "--uninstall", "--rollback"):
        with pytest.raises(SystemExit):
            parsed(operation, "--download-timeout-seconds", "5")


def test_release_base_url_must_be_https(capsys):
    with pytest.raises(SystemExit):
        parsed("--release-base-url", "http://mirror.example.com/x")
    args = parsed("--release-base-url", "https://mirror.example.com/x/")
    assert args.release_base_url == "https://mirror.example.com/x"


def test_ca_bundle_must_exist(tmp_path):
    with pytest.raises(SystemExit):
        parsed("--ca-bundle", str(tmp_path / "missing.pem"))
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x", encoding="utf-8")
    assert parsed("--ca-bundle", str(bundle)).ca_bundle == os.path.abspath(str(bundle))


def test_platform_choices_are_release_keys():
    assert parsed("--platform", "win32-x64").platform == "win32-x64"
    with pytest.raises(SystemExit):
        parsed("--platform", "win32-x86")


def test_stop_background_sessions_only_for_mutating_operations():
    assert parsed("--uninstall", "--stop-background-sessions").stop_background_sessions
    for operation in ("--inspect", "--selftest", "--update"):
        with pytest.raises(SystemExit):
            parsed(operation, "--stop-background-sessions")


def test_retired_npm_flags_are_accepted_with_a_warning(capsys):
    args = parsed("--npm-timeout-seconds", "30", "--npm-install-loglevel", "silly")
    err = capsys.readouterr().err
    assert "--npm-timeout-seconds is ignored" in err
    assert "--npm-install-loglevel is ignored" in err
    assert args.npm_timeout_seconds == 30


def test_help_documents_native_flags(capsys):
    with pytest.raises(SystemExit):
        parsed("--help")
    out = capsys.readouterr().out
    for flag in (
        "--rollback",
        "--platform",
        "--release-base-url",
        "--ca-bundle",
        "--download-timeout-seconds",
        "--stop-background-sessions",
        "--set-update-checks",
        "--set-windows-sandbox-warning",
        "--channel {stable,latest,next}",
    ):
        assert flag in out
    assert "--npm-timeout-seconds" not in out
    assert "Node.js and npm are not required" in " ".join(out.split())


def test_resolve_relative_within_root_rejects_escape(tmp_path):
    with pytest.raises(ClaudeWranglerError, match="escapes the project root"):
        resolve_relative_within_root(tmp_path, "../outside", "--local-dir")


def test_resolve_relative_within_root_returns_posix_spelling(tmp_path):
    relative, resolved = resolve_relative_within_root(tmp_path, "a/b/c", "--launcher")
    assert relative == "a/b/c"
    assert resolved == (tmp_path / "a" / "b" / "c").resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
@pytest.mark.parametrize("raw", ["\\escape", "C:escape", "/escape"])
def test_resolve_relative_within_root_rejects_windows_anchored_paths(tmp_path, raw):
    with pytest.raises(ClaudeWranglerError):
        resolve_relative_within_root(tmp_path, raw, "--local-dir")


def make_dangling_outward_link(project: Path, outside: Path) -> Path:
    """Create `.local` as a link to ``outside``, then delete ``outside``.

    Windows uses a junction (no privilege needed); POSIX a symbolic link.
    """

    outside.mkdir(parents=True)
    link = project / ".local"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        os.symlink(str(outside), str(link), target_is_directory=True)
    outside.rmdir()
    return link


def test_a_dangling_link_out_of_the_project_is_refused(tmp_path):
    # Python 3.9's Path.resolve() does not follow a dangling link on
    # Windows; the containment check must follow it anyway.
    from claude_wrangler.filesystem import require_safe_managed_path

    project = tmp_path / "project"
    project.mkdir()
    make_dangling_outward_link(project, tmp_path / "outside")

    with pytest.raises(ClaudeWranglerError, match="escapes the project root"):
        resolve_relative_within_root(project, ".local/claude", "--local-dir")
    with pytest.raises(ClaudeWranglerError, match="outside the project root"):
        require_safe_managed_path(
            project / ".local" / "claude", project, "managed directory"
        )


@pytest.mark.parametrize("raw", ["rt\nx", "rt\rx", "rt\x00x", "rt\x1bx"])
def test_layout_paths_refuse_control_characters(tmp_path, raw):
    # A newline would end a line of a generated shell script.
    with pytest.raises(ClaudeWranglerError, match="control characters"):
        resolve_relative_within_root(tmp_path, raw, "--local-dir")


def test_new_install_defaults(tmp_path):
    config = config_from_args(parsed(str(tmp_path)))
    assert config.operation == "install"
    assert config.claude_selector == DEFAULT_INSTALL_CLAUDE_SELECTOR
    assert config.claude_channel == DEFAULT_INSTALL_CLAUDE_CHANNEL
    assert config.claude_version is None
    assert config.layout.local_dir_relative == DEFAULT_LOCAL_DIR
    assert config.layout.launcher_cmd_relative == "bin/claude-local.cmd"
    assert config.release_base_url == DEFAULT_RELEASE_BASE_URL
    assert config.download_timeout_seconds == DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    assert config.update_checks is True
    assert config.windows_sandbox_warning is True
    assert config.credential_passthrough is False
    assert config.shared_home is False
    assert config.legacy is None


def test_plain_install_keeps_the_recorded_pin(tmp_path):
    write_native_metadata(tmp_path, claude_version="2.1.287", claude_channel="latest")
    config = config_from_args(parsed(str(tmp_path)))
    assert config.claude_version == "2.1.287"
    assert config.claude_channel == "latest"


def test_explicit_channel_resolves_live(tmp_path):
    write_native_metadata(tmp_path)
    config = config_from_args(parsed("--channel", "latest", str(tmp_path)))
    assert config.claude_selector == "latest"
    assert config.claude_version is None
    assert config.claude_channel == "latest"
    assert config.channel_explicit is True


def test_upgrade_with_exact_version(tmp_path):
    write_native_metadata(tmp_path)
    config = config_from_args(
        parsed(
            "--upgrade", "--channel", "stable", "--version", "2.1.296", str(tmp_path)
        )
    )
    assert config.claude_selector == "2.1.296"
    assert config.claude_version == "2.1.296"


def test_toggle_only_preserves_existing_selection(tmp_path):
    write_native_metadata(tmp_path, claude_version="2.1.287")
    config = config_from_args(parsed("--set-reasonable-permissions", str(tmp_path)))
    assert config.reconfigure_only is True
    assert config.reasonable_permissions_enabled is True
    assert config.claude_version == "2.1.287"
    config = config_from_args(parsed("--set-update-checks", "off", str(tmp_path)))
    assert config.reconfigure_only is True
    assert config.update_checks is False
    config = config_from_args(
        parsed("--set-credential-passthrough", "on", str(tmp_path))
    )
    assert config.reconfigure_only is True
    assert config.credential_passthrough is True
    assert config.claude_version == "2.1.287"


def test_recorded_settings_and_paths_are_honored(tmp_path):
    write_native_metadata(
        tmp_path,
        update_checks=False,
        windows_sandbox_warning=False,
        credential_passthrough=True,
        release_base_url="https://mirror.example.com/r",
        shared_home=True,
        paths={"claude_home_dir": "state/home", "launcher": "tools/claude-local"},
    )
    config = config_from_args(parsed(str(tmp_path)))
    assert config.update_checks is False
    assert config.windows_sandbox_warning is False
    assert config.credential_passthrough is True
    assert config.release_base_url == "https://mirror.example.com/r"
    assert config.shared_home is True
    assert config.layout.claude_home_relative == "state/home"
    assert config.layout.launcher_relative == "tools/claude-local"
    explicit = config_from_args(parsed("--launcher", "x/y", str(tmp_path)))
    assert explicit.layout.launcher_relative == "x/y"


def test_foreign_schema_metadata_is_not_native_state(tmp_path):
    write_native_metadata(tmp_path, schema_version=1)
    config = config_from_args(parsed("--inspect", str(tmp_path)))
    assert config.existing_metadata is None
    assert "schema_version 1" in config.existing_metadata_error


def test_inspect_tolerates_corrupt_metadata(tmp_path):
    path = tmp_path / DEFAULT_LOCAL_DIR / METADATA_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    config = config_from_args(parsed("--inspect", str(tmp_path)))
    assert config.existing_metadata is None
    assert "Failed to parse JSON" in config.existing_metadata_error


def test_inspect_uses_the_recorded_launcher_python(tmp_path):
    write_native_metadata(tmp_path, launcher_python="/recorded/python3")
    assert config_from_args(parsed("--inspect", str(tmp_path))).launcher_python == (
        "/recorded/python3"
    )
    assert (
        config_from_args(parsed(str(tmp_path))).launcher_python != "/recorded/python3"
    )


def test_determine_shared_home_preference_order():
    args = argparse.Namespace(shared_home=None)
    assert determine_shared_home(args, ExistingState(shared_home=True)) is True
    args = argparse.Namespace(shared_home=False)
    assert determine_shared_home(args, ExistingState(shared_home=True)) is False


def test_determine_target_selection_for_reports_uses_existing_state():
    existing = ExistingState(
        metadata={"x": 1},
        requested_claude_selector="2.1.287",
        claude_channel="stable",
        pinned_claude_version="2.1.287",
    )
    args = argparse.Namespace(
        requested_version=None,
        channel=None,
        set_reasonable_permissions=False,
        clear_reasonable_permissions=False,
        set_update_checks=None,
        set_windows_sandbox_warning=None,
        set_credential_passthrough=None,
        repair_install=False,
    )
    assert determine_target_selection(args, existing, "inspect")[:3] == (
        "2.1.287",
        "stable",
        "2.1.287",
    )


def test_legacy_local_dir_is_discovered_automatically(tmp_path):
    legacy = tmp_path / LEGACY_LOCAL_DIR
    legacy.mkdir(parents=True)
    (legacy / "package.json").write_text(
        json.dumps({"devDependencies": {"@anthropic-ai/claude-code": "2.1.287"}}),
        encoding="utf-8",
    )
    (legacy / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claude_channel": "stable",
                "claude_version": "2.1.287",
                "shared_home": True,
                "paths": {"claude_config_dir": ".local/claude"},
            }
        ),
        encoding="utf-8",
    )
    config = config_from_args(parsed(str(tmp_path)))
    assert config.legacy is not None
    assert config.legacy.dir_relative == LEGACY_LOCAL_DIR
    assert config.claude_version == "2.1.287"
    assert config.shared_home is True
    assert config.layout.local_dir_relative == DEFAULT_LOCAL_DIR


def test_explicit_legacy_local_dir_is_treated_as_the_legacy_layout(tmp_path):
    legacy = tmp_path / "tools" / "npm-claude"
    (legacy / "node_modules").mkdir(parents=True)
    (legacy / "package.json").write_text(
        json.dumps({"name": "claude-local-managed-install"}), encoding="utf-8"
    )
    config = config_from_args(parsed("--local-dir", "tools/npm-claude", str(tmp_path)))
    assert config.legacy is not None
    assert config.legacy.dir_relative == "tools/npm-claude"
    assert config.layout.local_dir_relative == DEFAULT_LOCAL_DIR


def test_local_dir_without_ownership_evidence_is_not_a_legacy_layout(tmp_path):
    # An unrelated web project's package.json and node_modules never make a
    # directory the npm-era layout whose npm tree install and uninstall delete.
    web = tmp_path / "web"
    (web / "node_modules" / "left-pad").mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"left-pad": "1.3.0"}}),
        encoding="utf-8",
    )
    config = config_from_args(parsed("--local-dir", "web", str(tmp_path)))
    assert config.legacy is None
    assert config.layout.local_dir_relative == "web"


def test_custom_native_local_dir(tmp_path):
    config = config_from_args(parsed("--local-dir", "rt/claude", str(tmp_path)))
    assert config.layout.local_dir_relative == "rt/claude"
    assert config.legacy is None
