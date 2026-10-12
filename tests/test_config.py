"""Tests for argument parsing and configuration normalization."""

import argparse
import json
import os

import pytest

from claude_wrangler.config import (
    config_from_args,
    determine_shared_home,
    determine_target_selection,
    normalize_requested_version,
    parse_args,
    parse_positive_int,
)
from claude_wrangler.constants import (
    DEFAULT_INSTALL_CLAUDE_CHANNEL,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    DEFAULT_LOCAL_DIR,
    DEFAULT_NPM_INSTALL_LOGLEVEL,
    DEFAULT_NPM_TIMEOUT_SECONDS,
)
from claude_wrangler.layout import resolve_relative_within_root
from claude_wrangler.models import ClaudeWranglerError, ExistingState
from claude_wrangler.rendering import build_local_package_json


class DummyArgs:
    inspect = False
    selftest = False
    uninstall = False
    update = False
    upgrade = False
    requested_version = None
    channel = None
    shared_home = None
    set_reasonable_permissions = False
    clear_reasonable_permissions = False


def make_local_dir(tmp_path):
    local_dir = tmp_path / DEFAULT_LOCAL_DIR
    local_dir.mkdir(parents=True)
    return local_dir


def test_normalize_requested_version_accepts_case_insensitive_latest():
    assert normalize_requested_version("LATEST") == "latest"
    assert normalize_requested_version("  latest  ") == "latest"
    assert normalize_requested_version("2.1.153") == "2.1.153"


def test_parse_positive_int_rejects_non_positive_values():
    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        parse_positive_int("0")


def test_determine_target_selection_defaults_to_latest_stable_for_install():
    selector, channel, version, source = determine_target_selection(
        DummyArgs(),
        ExistingState(),
        "install",
    )
    assert selector == DEFAULT_INSTALL_CLAUDE_SELECTOR
    assert channel == DEFAULT_INSTALL_CLAUDE_CHANNEL
    assert version == "latest"
    assert source == "default stable channel request"


def test_determine_target_selection_uses_existing_state_for_update():
    existing = ExistingState(
        metadata={"version_source": "previous install"},
        requested_claude_selector="latest",
        claude_channel="next",
        pinned_claude_version="2.1.173",
    )
    selector, channel, version, source = determine_target_selection(
        DummyArgs(),
        existing,
        "update",
    )
    assert selector == "latest"
    assert channel == "next"
    assert version == "2.1.173"
    assert source == "previous install"


def test_determine_target_selection_requires_upgrade_channel():
    with pytest.raises(ClaudeWranglerError):
        determine_target_selection(DummyArgs(), ExistingState(), "upgrade")


def test_determine_shared_home_preserves_existing_choice():
    args = DummyArgs()
    existing = ExistingState(shared_home=True)
    assert determine_shared_home(args, existing) is True


def test_determine_shared_home_honors_explicit_flag():
    args = DummyArgs()
    args.shared_home = False
    existing = ExistingState(shared_home=True)
    assert determine_shared_home(args, existing) is False


def test_resolve_relative_within_root_rejects_escape(tmp_path):
    with pytest.raises(ClaudeWranglerError):
        resolve_relative_within_root(tmp_path, "../escape", "test")


def test_resolve_relative_within_root_rejects_absolute_path(tmp_path):
    with pytest.raises(ClaudeWranglerError, match="must be relative"):
        resolve_relative_within_root(tmp_path, str(tmp_path / "inside"), "test")


def test_resolve_relative_within_root_returns_posix_spelling(tmp_path):
    relative, resolved = resolve_relative_within_root(
        tmp_path, "bin/../bin/./custom-claude", "test"
    )

    assert relative == "bin/custom-claude"
    assert resolved == (tmp_path / "bin" / "custom-claude").resolve()


@pytest.mark.skipif(
    os.name != "nt", reason="backslash is a path separator only on Windows"
)
def test_resolve_relative_within_root_normalizes_windows_separators(tmp_path):
    relative, _ = resolve_relative_within_root(tmp_path, "bin\\custom-claude", "test")

    assert relative == "bin/custom-claude"


@pytest.mark.skipif(
    os.name != "nt", reason="rooted and drive-relative paths exist only on Windows"
)
@pytest.mark.parametrize("raw", ["\\escape", "C:escape", "/escape"])
def test_resolve_relative_within_root_rejects_windows_anchored_paths(tmp_path, raw):
    with pytest.raises(ClaudeWranglerError, match="must be relative"):
        resolve_relative_within_root(tmp_path, raw, "test")


def test_parse_args_requires_upgrade_channel():
    with pytest.raises(SystemExit):
        parse_args(["--upgrade"])


def test_parse_args_rejects_update_version_request():
    with pytest.raises(SystemExit):
        parse_args(["--update", "--version", "latest"])


def test_parse_args_keeps_bare_update_as_project_root():
    args = parse_args(["update"])

    assert args.update is False
    assert args.project_root == "update"


def test_config_from_args_suggests_missing_dashes_for_bare_update():
    with pytest.raises(ClaudeWranglerError, match="Did you perhaps mean '--update'"):
        config_from_args(parse_args(["update"]))


def test_config_from_args_suggests_missing_dashes_for_other_known_flags():
    with pytest.raises(ClaudeWranglerError, match="Did you perhaps mean '--force'"):
        config_from_args(parse_args(["force"]))


def test_config_from_args_allows_existing_project_named_update(tmp_path, monkeypatch):
    project = tmp_path / "update"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    config = config_from_args(parse_args(["update"]))

    assert config.operation == "install"
    assert config.project_root == project.resolve()


def test_parse_args_rejects_set_reasonable_permissions_with_inspect():
    with pytest.raises(SystemExit):
        parse_args(["--inspect", "--set-reasonable-permissions"])


def test_parse_args_rejects_mismatched_upgrade_channel_and_version():
    with pytest.raises(SystemExit):
        parse_args(["--upgrade", "--channel", "stable", "--version", "next"])


def test_parse_args_rejects_mismatched_install_channel_and_version():
    with pytest.raises(SystemExit):
        parse_args(["--channel", "latest", "--version", "stable"])


def test_parse_args_accepts_exact_version_for_any_channel():
    # Claude versions carry no channel marker, so an exact version may be
    # paired with any explicit channel.
    args = parse_args(["--channel", "next", "--version", "2.1.173"])

    assert args.channel == "next"
    assert args.requested_version == "2.1.173"


def test_parse_args_rejects_invalid_npm_timeout():
    with pytest.raises(SystemExit):
        parse_args(["--npm-timeout-seconds", "0"])


def test_config_from_args_builds_expected_configuration(tmp_path):
    args = parse_args(
        [
            "--shared-home",
            "--channel",
            "next",
            "--version",
            "latest",
            "--launcher",
            "bin/custom-claude",
            "--readme-local",
            "LOCAL-README.md",
            str(tmp_path),
        ]
    )
    config = config_from_args(args)

    assert config.operation == "install"
    assert config.claude_selector == "latest"
    assert config.claude_channel == "next"
    assert config.shared_home is True
    assert config.npm_timeout_seconds == DEFAULT_NPM_TIMEOUT_SECONDS
    assert config.npm_install_loglevel == DEFAULT_NPM_INSTALL_LOGLEVEL
    assert config.layout.launcher_relative == "bin/custom-claude"
    assert config.layout.readme_relative == "LOCAL-README.md"


def test_config_from_args_honors_npm_timeout_override(tmp_path):
    config = config_from_args(
        parse_args(["--npm-timeout-seconds", "42", str(tmp_path)])
    )

    assert config.npm_timeout_seconds == 42


def test_config_from_args_honors_npm_install_loglevel_override(tmp_path):
    config = config_from_args(
        parse_args(["--npm-install-loglevel", "notice", str(tmp_path)])
    )

    assert config.npm_install_loglevel == "notice"


def test_config_from_args_update_falls_back_to_existing_managed_files(tmp_path):
    local_dir = make_local_dir(tmp_path)
    (local_dir / "package.json").write_text(
        build_local_package_json("2.1.150"),
        encoding="utf-8",
    )
    (local_dir / "package-lock.json").write_text(
        '{"packages": {"node_modules/@anthropic-ai/claude-code":'
        ' {"version": "2.1.150"}}}',
        encoding="utf-8",
    )

    config = config_from_args(parse_args(["--update", str(tmp_path)]))

    assert config.operation == "update"
    assert config.claude_selector == "2.1.150"
    # Exact Claude versions never imply a channel.
    assert config.claude_channel is None
    assert config.claude_version == "2.1.150"
    assert config.version_source == "existing managed files"


def test_config_from_args_inspect_tolerates_corrupt_managed_json(tmp_path):
    local_dir = make_local_dir(tmp_path)
    (local_dir / ".claude-wrangler.json").write_text("{not json\n", encoding="utf-8")
    (local_dir / "package.json").write_text(
        build_local_package_json("2.1.150"),
        encoding="utf-8",
    )
    (local_dir / "package-lock.json").write_text("{not json\n", encoding="utf-8")

    config = config_from_args(parse_args(["--inspect", str(tmp_path)]))

    assert config.operation == "inspect"
    assert config.claude_selector == "2.1.150"
    assert config.claude_version == "2.1.150"


def test_config_from_args_inspect_tolerates_fully_corrupt_managed_state(tmp_path):
    local_dir = make_local_dir(tmp_path)
    (local_dir / ".claude-wrangler.json").write_text("{not json\n", encoding="utf-8")
    (local_dir / "package.json").write_text("{not json\n", encoding="utf-8")
    (local_dir / "package-lock.json").write_text("{not json\n", encoding="utf-8")

    config = config_from_args(parse_args(["--inspect", str(tmp_path)]))

    assert config.operation == "inspect"
    assert config.claude_selector == DEFAULT_INSTALL_CLAUDE_SELECTOR
    # With fully corrupt managed state nothing can prove a channel, and the
    # generic `latest` selector deliberately implies none.
    assert config.claude_channel is None
    assert config.claude_version == DEFAULT_INSTALL_CLAUDE_SELECTOR


def test_config_from_args_toggle_only_preserves_existing_selection(tmp_path):
    local_dir = make_local_dir(tmp_path)
    (local_dir / "package.json").write_text(
        build_local_package_json("2.1.150"),
        encoding="utf-8",
    )
    (local_dir / "package-lock.json").write_text(
        '{"packages": {"node_modules/@anthropic-ai/claude-code":'
        ' {"version": "2.1.150"}}}',
        encoding="utf-8",
    )
    # json.dumps escapes Windows backslashes; %-formatting the path into a
    # JSON literal produced invalid escapes such as `\U` on Windows.
    (local_dir / ".claude-wrangler.json").write_text(
        json.dumps(
            {
                "project_root": str(tmp_path),
                "claude_selector": "latest",
                "claude_channel": "stable",
                "claude_version": "2.1.150",
                "version_source": "existing metadata",
                "reasonable_permissions_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    config = config_from_args(
        parse_args(["--set-reasonable-permissions", str(tmp_path)])
    )

    assert config.operation == "install"
    assert config.claude_selector == "latest"
    assert config.claude_channel == "stable"
    assert config.claude_version == "2.1.150"
    assert config.version_source == "existing metadata"
    assert config.reasonable_permissions_enabled is True
    assert config.reconfigure_only is True
