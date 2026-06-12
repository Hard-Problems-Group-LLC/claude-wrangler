"""Tests for install, update, inspect, uninstall, and self-test operations."""

import json

import pytest

from claude_wrangler.filesystem import upsert_gitignore_block, write_metadata
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.operations import (
    gather_inspection_report,
    install_like_operation,
    selftest_operation,
    update_operation,
    uninstall_operation,
)
from claude_wrangler.rendering import (
    build_gitignore_block,
    build_launcher_content,
    build_local_package_json,
    build_local_readme_content,
    build_metadata,
)


def materialize_managed_install(config) -> None:
    layout = config.layout
    layout.local_dir.mkdir(parents=True, exist_ok=True)
    layout.claude_home_dir.mkdir(parents=True, exist_ok=True)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)
    layout.local_package_json_path.write_text(
        build_local_package_json(config.claude_version),
        encoding="utf-8",
    )
    layout.local_package_lock_path.write_text(
        json.dumps(
            {
                "packages": {
                    "node_modules/@anthropic-ai/claude-code": {
                        "version": config.claude_version
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    layout.launcher_path.write_text(
        build_launcher_content(config),
        encoding="utf-8",
    )
    layout.readme_path.write_text(
        build_local_readme_content(config),
        encoding="utf-8",
    )
    layout.local_node_modules_dir.mkdir(parents=True, exist_ok=True)
    upsert_gitignore_block(
        layout.gitignore_path,
        build_gitignore_block(layout),
        dry_run=False,
    )
    write_metadata(layout.metadata_path, build_metadata(config), dry_run=False)


FAKE_RUNTIME_DIAGNOSTICS = {
    "resolved_commands": {
        "node": {
            "requested_name": "node",
            "resolved_path": "/usr/bin/node",
            "version": "v20.11.1",
            "found": True,
        },
        "npm": {
            "requested_name": "npm",
            "resolved_path": "/usr/bin/npm",
            "version": "10.5.0",
            "found": True,
        },
        "npx": {
            "requested_name": "npx",
            "resolved_path": "/usr/bin/npx",
            "version": "10.5.0",
            "found": True,
        },
    },
    "node_selector_signals": [],
    "selector_alignment": {
        "status": "not_applicable",
        "summary": "No checked-in Node.js selector files were detected.",
        "signal_count": 0,
        "explicit_signal_count": 0,
        "signals_present": False,
    },
    "package_manager_declaration": None,
    "home_configuration": {
        "mode": "project-local isolated HOME",
        "inherits_from_parent_process": False,
        "launcher_environment": {},
    },
}


def test_install_like_operation_overwrites_managed_files_without_force(
    monkeypatch,
    tmp_path,
    config_factory,
):
    initial = config_factory(
        tmp_path,
        claude_selector="latest",
        claude_channel="stable",
        claude_version="2.1.150",
        version_source="default stable channel request",
    )
    target = config_factory(
        tmp_path,
        operation="install",
        claude_selector="latest",
        claude_channel="stable",
        claude_version="latest",
        skip_install=True,
        version_source="default stable channel request",
    )
    materialize_managed_install(initial)

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("python3", "python3"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.ensure_command_exists",
        lambda name: None,
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.resolve_install_version",
        lambda config, npm_name, env=None, timeout_seconds=None: config_factory(
            tmp_path,
            operation=config.operation,
            claude_selector="latest",
            claude_channel="stable",
            claude_version="2.1.153",
            skip_install=config.skip_install,
            shared_home=config.shared_home,
            force=config.force,
            dry_run=config.dry_run,
            version_source="default stable channel request",
            available_versions={
                "stable": "2.1.153",
                "latest": "2.1.172",
                "next": "2.1.173",
            },
        ),
    )

    exit_code = install_like_operation(target)

    assert exit_code == 0
    package_json = target.layout.local_package_json_path.read_text(encoding="utf-8")
    assert "2.1.153" in package_json
    readme = target.layout.readme_path.read_text(encoding="utf-8")
    assert "claude-wrangler --inspect ." in readme
    assert "Known stable version" in readme
    assert "~/bin" not in readme


def test_update_operation_refreshes_known_versions_without_rewriting_package_json(
    monkeypatch,
    tmp_path,
    capsys,
    config_factory,
):
    config = config_factory(
        tmp_path,
        operation="update",
        claude_selector="latest",
        claude_channel="stable",
        claude_version="2.1.150",
        available_versions={
            "stable": "2.1.150",
            "latest": "2.1.160",
            "next": None,
        },
    )
    materialize_managed_install(config)
    original_package_json = config.layout.local_package_json_path.read_text(
        encoding="utf-8"
    )

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.ensure_command_exists",
        lambda name: None,
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.fetch_available_claude_versions",
        lambda npm_name, project_root, env=None, timeout_seconds=None: {
            "stable": "2.1.153",
            "latest": "2.1.172",
            "next": "2.1.173",
        },
    )

    exit_code = update_operation(config)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "does not perform an upgrade" in output
    assert "Known version" in output
    assert (
        config.layout.local_package_json_path.read_text(encoding="utf-8")
        == original_package_json
    )
    metadata = json.loads(config.layout.metadata_path.read_text(encoding="utf-8"))
    assert metadata["available_versions"]["stable"] == "2.1.153"
    assert metadata["available_versions"]["next"] == "2.1.173"


def test_install_like_operation_toggle_only_skips_package_install(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(
        tmp_path,
        claude_selector="latest",
        claude_channel="stable",
        claude_version="2.1.150",
        version_source="existing metadata",
        reasonable_permissions_enabled=True,
        reconfigure_only=True,
    )
    materialize_managed_install(config_factory(tmp_path))
    called = {"resolve_install_version": False, "run_command": False}

    def fail_resolve_install_version(_config, _npm_name):
        called["resolve_install_version"] = True
        raise AssertionError("resolve_install_version should not run")

    def fail_run_command(*args, **kwargs):
        called["run_command"] = True
        raise AssertionError("run_command should not run")

    monkeypatch.setattr(
        "claude_wrangler.operations.resolve_install_version",
        fail_resolve_install_version,
    )
    monkeypatch.setattr("claude_wrangler.operations.run_command", fail_run_command)

    exit_code = install_like_operation(config)

    assert exit_code == 0
    metadata = json.loads(config.layout.metadata_path.read_text(encoding="utf-8"))
    assert metadata["reasonable_permissions_enabled"] is True
    assert called["resolve_install_version"] is False
    assert called["run_command"] is False


def test_install_like_operation_clear_regenerates_disabled_launcher(
    tmp_path,
    config_factory,
):
    enabled_config = config_factory(tmp_path, reasonable_permissions_enabled=True)
    materialize_managed_install(enabled_config)
    clear_config = config_factory(
        tmp_path,
        reasonable_permissions_enabled=False,
        reconfigure_only=True,
    )

    exit_code = install_like_operation(clear_config)

    assert exit_code == 0
    launcher = clear_config.layout.launcher_path.read_text(encoding="utf-8")
    assert 'reasonable_permissions_enabled="0"' in launcher
    assert 'reasonable_permissions_enabled="1"' not in launcher


def test_install_like_operation_repair_install_cleans_managed_npm_artifacts(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, repair_install=True)
    layout = config.layout
    layout.local_node_modules_dir.mkdir(parents=True)
    (layout.local_node_modules_dir / "stale.txt").write_text(
        "stale",
        encoding="utf-8",
    )
    layout.local_package_lock_path.parent.mkdir(parents=True, exist_ok=True)
    layout.local_package_lock_path.write_text("{}", encoding="utf-8")
    npx_cache = layout.local_dir / ".npm-cache" / "_npx"
    npx_cache.mkdir(parents=True)
    (npx_cache / "legacy-claude.txt").write_text("stale", encoding="utf-8")
    cache_tmp = layout.local_dir / ".npm-cache" / "_cacache" / "tmp"
    cache_tmp.mkdir(parents=True)
    (cache_tmp / "truncated-tarball").write_text("stale", encoding="utf-8")
    called = {"npm_install": False, "verify": False}

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.ensure_command_exists",
        lambda name: None,
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.resolve_install_version",
        lambda config, npm_name, env=None, timeout_seconds=None: config,
    )

    def fake_run(
        command,
        cwd,
        capture_output=False,
        env=None,
        timeout_seconds=None,
    ):
        called["npm_install"] = True
        assert command == [
            "npm",
            "install",
            "--prefix",
            str(layout.local_dir),
            "--no-audit",
            "--no-fund",
            "--foreground-scripts",
            "--loglevel=http",
            "--progress=false",
        ]
        assert env["NPM_CONFIG_CACHE"] == str(layout.local_dir / ".npm-cache")
        assert env["NPM_CONFIG_AUDIT"] == "false"
        assert env["NPM_CONFIG_FOREGROUND_SCRIPTS"] == "true"
        assert env["NPM_CONFIG_FUND"] == "false"
        assert env["NPM_CONFIG_LOGLEVEL"] == "http"
        assert env["NPM_CONFIG_PROGRESS"] == "false"
        assert env["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"
        assert timeout_seconds == config.npm_timeout_seconds
        assert not layout.local_node_modules_dir.exists()
        assert not layout.local_package_lock_path.exists()
        assert not npx_cache.exists()
        assert not cache_tmp.exists()

    def fake_verify(_config):
        called["verify"] = True

    monkeypatch.setattr("claude_wrangler.operations.run_command", fake_run)
    monkeypatch.setattr(
        "claude_wrangler.operations.verify_local_claude_binary",
        fake_verify,
    )

    exit_code = install_like_operation(config)

    assert exit_code == 0
    assert called == {"npm_install": True, "verify": True}


def test_gather_inspection_report_surfaces_warnings_and_metadata_mismatch(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, reasonable_permissions_enabled=True)
    config.layout.local_dir.mkdir(parents=True, exist_ok=True)
    config.layout.local_package_json_path.write_text(
        build_local_package_json(config.claude_version),
        encoding="utf-8",
    )
    mismatched = build_metadata(config)
    mismatched["project_root"] = "/tmp/elsewhere"
    write_metadata(config.layout.metadata_path, mismatched, dry_run=False)

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("python3", "python3"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.collect_runtime_diagnostics",
        lambda *args, **kwargs: FAKE_RUNTIME_DIAGNOSTICS,
    )

    report = gather_inspection_report(config)

    assert report["requested_configuration"]["reasonable_permissions_enabled"] is True
    assert report["state"]["reasonable_permissions_enabled"] is True
    assert any(
        "does not appear to be a git repository" in item for item in report["warnings"]
    )
    assert any("node_modules is missing" in item for item in report["issues"])
    assert any(
        "does not match the inspected project root" in item for item in report["issues"]
    )


def test_gather_inspection_report_reports_corrupt_package_lock(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    config.layout.local_dir.mkdir(parents=True, exist_ok=True)
    config.layout.local_package_json_path.write_text(
        build_local_package_json(config.claude_version),
        encoding="utf-8",
    )
    config.layout.local_package_lock_path.write_text("{not json\n", encoding="utf-8")
    write_metadata(config.layout.metadata_path, build_metadata(config), dry_run=False)

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.collect_runtime_diagnostics",
        lambda *args, **kwargs: FAKE_RUNTIME_DIAGNOSTICS,
    )

    report = gather_inspection_report(config)

    assert report["state"]["installed_claude_version_in_lockfile"] is None
    assert report["state"]["local_package_lock_parse_error"]
    assert any(
        "package-lock.json could not be parsed" in item for item in report["issues"]
    )


def test_gather_inspection_report_reports_corrupt_package_json(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    config.layout.local_dir.mkdir(parents=True, exist_ok=True)
    config.layout.local_package_json_path.write_text("{not json\n", encoding="utf-8")
    write_metadata(config.layout.metadata_path, build_metadata(config), dry_run=False)

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.collect_runtime_diagnostics",
        lambda *args, **kwargs: FAKE_RUNTIME_DIAGNOSTICS,
    )

    report = gather_inspection_report(config)

    assert report["state"]["requested_claude_version_in_package_json"] is None
    assert report["state"]["local_package_json_parse_error"]
    assert any("package.json could not be parsed" in item for item in report["issues"])


def test_gather_inspection_report_reports_corrupt_metadata(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    config.layout.local_dir.mkdir(parents=True, exist_ok=True)
    config.layout.metadata_path.write_text("{not json\n", encoding="utf-8")

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.collect_runtime_diagnostics",
        lambda *args, **kwargs: FAKE_RUNTIME_DIAGNOSTICS,
    )

    report = gather_inspection_report(config)

    assert report["state"]["metadata_exists"] is False
    assert report["state"]["metadata_parse_error"]
    assert any("metadata could not be parsed" in item for item in report["issues"])


def test_gather_inspection_report_includes_runtime_selector_warning(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    diagnostics = dict(FAKE_RUNTIME_DIAGNOSTICS)
    diagnostics["node_selector_signals"] = [
        {
            "kind": ".nvmrc",
            "path": str(tmp_path / ".nvmrc"),
            "raw_value": "20.11.1",
            "explicit_version": "20.11.1",
            "matches_resolved_node_version": False,
        }
    ]
    diagnostics["selector_alignment"] = {
        "status": "mismatch",
        "summary": "Resolved node version v18.19.0 does not match selectors.",
        "signal_count": 1,
        "explicit_signal_count": 1,
        "signals_present": True,
    }

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.collect_runtime_diagnostics",
        lambda *args, **kwargs: diagnostics,
    )

    report = gather_inspection_report(config)

    assert report["runtime_environment"]["selector_alignment"]["status"] == "mismatch"
    assert any(
        "Resolved node version v18.19.0 does not match selectors." in item
        for item in report["warnings"]
    )


def test_uninstall_operation_requires_metadata_without_force(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)

    with pytest.raises(ClaudeWranglerError):
        uninstall_operation(config)


def test_uninstall_operation_removes_managed_artifacts(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    materialize_managed_install(config)

    exit_code = uninstall_operation(config)

    assert exit_code == 0
    assert not config.layout.local_dir.exists()
    assert not config.layout.claude_home_dir.exists()
    assert not config.layout.config_dir.exists()
    assert not config.layout.readme_path.exists()
    assert not config.layout.launcher_path.exists()


SELFTEST_STATE = {
    "metadata_exists": True,
    "launcher_matches_expected": True,
    "readme_matches_expected": True,
    "gitignore_managed_block_present": True,
    "installed_claude_version_in_lockfile": "2.1.172",
    "launcher_exists": True,
    "local_claude_bin_exists": True,
    "local_claude_bin_executable": True,
    "node_found": True,
    "npm_found": True,
    "npx_found": True,
}

SELFTEST_RUNTIME = {
    "resolved_commands": {
        "node": {"found": True, "version": "v20.11.1"},
        "npm": {"found": True, "version": "10.5.0"},
        "npx": {"found": True, "version": "10.5.0"},
    },
    "selector_alignment": {
        "status": "match",
        "summary": (
            "Resolved node version v20.11.1 matches all explicit Node.js " "selectors."
        ),
        "signal_count": 1,
        "explicit_signal_count": 1,
        "signals_present": True,
    },
}


class Completed:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


def test_selftest_reports_audit_command_failure(
    monkeypatch,
    tmp_path,
    capsys,
    config_factory,
):
    config = config_factory(tmp_path, operation="selftest")

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.ensure_command_exists",
        lambda name: None,
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.gather_inspection_report",
        lambda config: {
            "runtime_environment": SELFTEST_RUNTIME,
            "state": SELFTEST_STATE,
        },
    )

    def fake_run(
        command,
        cwd,
        capture_output=False,
        env=None,
        timeout_seconds=None,
    ):
        if command[-1] == "--version":
            return Completed(stdout="2.1.172 (Claude Code)\n")
        if command[-1] == "--help":
            return Completed(stdout="Usage: claude [options]\n")
        if command[:2] == ["npm", "audit"]:
            assert env["NPM_CONFIG_CACHE"] == str(
                config.layout.local_dir / ".npm-cache"
            )
            assert "NPM_CONFIG_AUDIT" not in env
            assert timeout_seconds == config.npm_timeout_seconds
            raise ClaudeWranglerError("network unavailable")
        raise AssertionError(command)

    monkeypatch.setattr("claude_wrangler.operations.run_command", fake_run)

    exit_code = selftest_operation(config)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "[PASS] runtime_selector_alignment -" in output
    assert "[PASS] launcher_version_runs -" in output
    assert "[PASS] launcher_help_runs -" in output
    assert "[FAIL] npm_audit_clean - network unavailable" in output
    assert "Self-test failed." in output


def test_selftest_reports_invalid_audit_json(
    monkeypatch,
    tmp_path,
    capsys,
    config_factory,
):
    config = config_factory(tmp_path, operation="selftest")

    monkeypatch.setattr(
        "claude_wrangler.operations.detect_npm_binaries",
        lambda: ("npm", "npx"),
    )
    monkeypatch.setattr(
        "claude_wrangler.operations.gather_inspection_report",
        lambda config: {
            "runtime_environment": SELFTEST_RUNTIME,
            "state": SELFTEST_STATE,
        },
    )

    def fake_run(
        command,
        cwd,
        capture_output=False,
        env=None,
        timeout_seconds=None,
    ):
        if command[-1] == "--version":
            return Completed(stdout="2.1.172 (Claude Code)\n")
        if command[-1] == "--help":
            return Completed(stdout="Usage: claude [options]\n")
        if command[:2] == ["npm", "audit"]:
            return Completed(stdout="not json")
        raise AssertionError(command)

    monkeypatch.setattr("claude_wrangler.operations.run_command", fake_run)

    exit_code = selftest_operation(config)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "[FAIL] npm_audit_clean - npm audit returned invalid JSON:" in output
