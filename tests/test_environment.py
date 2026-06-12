"""Tests for runtime-environment detection and launcher-environment reports."""

from claude_wrangler.environment import (
    assess_node_selector_alignment,
    build_home_configuration,
    collect_runtime_diagnostics,
    detect_node_selector_signals,
    detect_package_manager_declaration,
)


def test_detect_node_selector_signals_reads_known_files(tmp_path):
    (tmp_path / ".nvmrc").write_text("v20.11.1\n", encoding="utf-8")
    (tmp_path / ".tool-versions").write_text(
        "# comment\nnodejs 18.19.0\npython 3.12.1\n",
        encoding="utf-8",
    )

    signals = detect_node_selector_signals(tmp_path)

    assert [signal["kind"] for signal in signals] == [
        ".nvmrc",
        ".tool-versions:nodejs",
    ]
    assert signals[0]["explicit_version"] == "20.11.1"
    assert signals[1]["explicit_version"] == "18.19.0"


def test_detect_package_manager_declaration_reads_root_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name":"example","packageManager":"pnpm@9.1.0"}\n',
        encoding="utf-8",
    )

    declaration = detect_package_manager_declaration(tmp_path)

    assert declaration == {
        "path": str(tmp_path / "package.json"),
        "raw_value": "pnpm@9.1.0",
        "family": "pnpm",
        "version": "9.1.0",
        "read_error": None,
    }


def test_assess_node_selector_alignment_reports_mismatch():
    signals = [
        {
            "kind": ".nvmrc",
            "path": "/tmp/example/.nvmrc",
            "raw_value": "20.11.1",
            "explicit_version": "20.11.1",
        }
    ]

    assessment = assess_node_selector_alignment(
        signals,
        {
            "requested_name": "node",
            "resolved_path": "/usr/bin/node",
            "version": "v18.19.0",
            "found": True,
        },
    )

    assert assessment["status"] == "mismatch"
    assert ".nvmrc expects Node.js 20.11.1." in assessment["summary"]
    assert assessment["signals"][0]["matches_resolved_node_version"] is False


def test_build_home_configuration_reports_isolated_paths(tmp_path):
    configuration = build_home_configuration(
        False,
        tmp_path / ".local" / "claude" / "HOME",
        tmp_path / ".local" / "claude",
    )

    assert configuration["mode"] == "project-local isolated HOME"
    assert configuration["inherits_from_parent_process"] is False
    environment = configuration["launcher_environment"]
    assert environment["HOME"].endswith("HOME")
    assert environment["CLAUDE_CONFIG_DIR"].endswith("claude")


def test_build_home_configuration_pins_config_dir_in_shared_mode(tmp_path):
    configuration = build_home_configuration(
        True,
        tmp_path / ".local" / "claude" / "HOME",
        tmp_path / ".local" / "claude",
    )

    assert configuration["mode"] == "shared user HOME"
    assert configuration["inherits_from_parent_process"] is True
    environment = configuration["launcher_environment"]
    assert environment["HOME"] is None
    assert environment["CLAUDE_CONFIG_DIR"].endswith("claude")


def test_collect_runtime_diagnostics_includes_signals_and_home_configuration(
    monkeypatch,
    tmp_path,
):
    (tmp_path / ".node-version").write_text("20.11.1\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name":"example","packageManager":"npm@10.5.0"}\n',
        encoding="utf-8",
    )

    versions = {
        "node": {
            "requested_name": "node",
            "resolved_path": "/tool/node",
            "version": "v20.11.1",
            "found": True,
        },
        "npm": {
            "requested_name": "npm",
            "resolved_path": "/tool/npm",
            "version": "10.5.0",
            "found": True,
        },
        "npx": {
            "requested_name": "npx",
            "resolved_path": "/tool/npx",
            "version": "10.5.0",
            "found": True,
        },
    }
    monkeypatch.setattr(
        "claude_wrangler.environment.resolve_command_metadata",
        lambda command_name: versions[command_name],
    )

    diagnostics = collect_runtime_diagnostics(
        tmp_path,
        False,
        tmp_path / ".local" / "claude" / "HOME",
        tmp_path / ".local" / "claude",
        "npm",
        "npx",
    )

    assert diagnostics["selector_alignment"]["status"] == "match"
    assert diagnostics["package_manager_declaration"]["raw_value"] == "npm@10.5.0"
    assert diagnostics["home_configuration"]["mode"] == "project-local isolated HOME"
