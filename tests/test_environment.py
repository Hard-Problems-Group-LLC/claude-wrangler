"""Tests for runtime-environment detection and launcher-environment reports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_wrangler.environment import (
    assess_node_selector_alignment,
    build_home_configuration,
    collect_runtime_diagnostics,
    command_name,
    detect_node_selector_signals,
    detect_package_manager_declaration,
    find_command,
    resolve_command_metadata,
)
from claude_wrangler.generator import parsed_launch_config

from native_fakes import make_config


def launch_for(tmp_path: Path, *args: str):
    """Return the parsed launch config for a fresh project."""

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = make_config(project, *args)
    return config, parsed_launch_config(config)


def test_detect_node_selector_signals_reads_known_files(tmp_path):
    (tmp_path / ".nvmrc").write_text("v20.11.1\n", encoding="utf-8")
    (tmp_path / ".tool-versions").write_text(
        "# comment\nnodejs 18.19.0\npython 3.12.1\n",
        encoding="utf-8",
    )

    signals = detect_node_selector_signals(tmp_path)

    assert [signal["kind"] for signal in signals] == [".nvmrc", ".tool-versions:nodejs"]
    assert signals[0]["explicit_version"] == "20.11.1"
    assert signals[1]["explicit_version"] == "18.19.0"


def test_detect_package_manager_declaration_reads_root_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"packageManager": "pnpm@9.1.0"}', encoding="utf-8"
    )
    declaration = detect_package_manager_declaration(tmp_path)
    assert declaration["family"] == "pnpm"
    assert declaration["version"] == "9.1.0"
    (tmp_path / "package.json").write_text("{broken", encoding="utf-8")
    assert detect_package_manager_declaration(tmp_path)["read_error"]


def test_node_alignment_is_advisory_and_matches_partial_selectors():
    signals = [{"kind": ".nvmrc", "raw_value": "20", "explicit_version": "20"}]
    match = assess_node_selector_alignment(
        signals, {"found": True, "version": "v20.11.1"}
    )
    assert match["status"] == "match"
    mismatch = assess_node_selector_alignment(
        signals, {"found": True, "version": "v18.19.0"}
    )
    assert mismatch["status"] == "mismatch"
    missing = assess_node_selector_alignment(signals, {"found": False, "version": None})
    assert missing["status"] == "node_not_found"
    assert "does not need it" in missing["summary"]


def test_command_names_are_platform_spelled():
    assert command_name("npm", "nt") == "npm.cmd"
    assert command_name("npx", "nt") == "npx.cmd"
    assert command_name("node", "nt") == "node"
    assert command_name("npm", "posix") == "npm"


@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_home_configuration_reports_both_rings_for_the_host_os(tmp_path, os_name):
    config, launch = launch_for(tmp_path)
    report = build_home_configuration(
        launch, config.project_root, shared_home=False, os_name=os_name
    )
    ring1 = report["rings"]["ring1"]
    ring0 = report["rings"]["ring0"]
    assert report["default_ring"] == 1
    assert report["launcher_environment"] == ring1
    sep = "\\" if os_name == "nt" else "/"
    root = str(config.project_root)
    assert ring1["CLAUDE_CONFIG_DIR"] == root + sep + ".local" + sep + "claude"
    assert ring1["HOME"] == ring1["CLAUDE_CONFIG_DIR"] + sep + "HOME"
    assert ring1["CLAUDE_LOCAL_SHARED_HOME"] == "0"
    assert ring0["HOME"] is None
    assert ring0["CLAUDE_CONFIG_DIR"] == ring1["CLAUDE_CONFIG_DIR"]
    assert ring0["CLAUDE_LOCAL_SHARED_HOME"] == "1"
    assert ring0["DISABLE_UPDATES"] == "1"
    assert ring1["PATH_PREFIX"].endswith(sep.join(["", "wrangler", "shim"]))
    if os_name == "nt":
        assert ring1["USERPROFILE"] == ring1["HOME"]
        assert ring1["HOMEDRIVE"] + ring1["HOMEPATH"] == ring1["HOME"] or (
            not ring1["HOMEDRIVE"]
        )
        assert ring1["APPDATA"] == ring1["HOME"] + "\\AppData\\Roaming"
        assert ring1["LOCALAPPDATA"] == ring1["HOME"] + "\\AppData\\Local"
        assert ring0["USERPROFILE"] is None
    else:
        assert "USERPROFILE" not in ring1


def test_shared_home_default_reports_ring_zero(tmp_path):
    config, launch = launch_for(tmp_path, "--shared-home")
    report = build_home_configuration(launch, config.project_root, shared_home=True)
    assert report["default_ring"] == 0
    assert report["inherits_from_parent_process"] is True
    assert report["launcher_environment"]["HOME"] is None


def test_collect_runtime_diagnostics_marks_node_informational(tmp_path):
    config, launch = launch_for(tmp_path)
    (config.project_root / ".nvmrc").write_text("v20.11.1\n", encoding="utf-8")
    diagnostics = collect_runtime_diagnostics(config.project_root, launch, False)
    assert diagnostics["node_required"] is False
    for name in ("node", "npm", "npx"):
        assert diagnostics["resolved_commands"][name]["required"] is False
    assert diagnostics["selector_alignment"]["signal_count"] == 1
    assert "rings" in diagnostics["home_configuration"]


def _plant_node(directory: Path, marker: Path, version: str) -> Path:
    """Write a `node` stand-in that records being run and prints a version."""

    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        script = directory / "node.cmd"
        script.write_text(
            '@echo off\r\necho ran> "{}"\r\necho {}\r\n'.format(marker, version),
            encoding="ascii",
        )
    else:
        script = directory / "node"
        script.write_text(
            "#!/bin/sh\necho ran > '{}'\necho {}\n".format(marker, version),
            encoding="ascii",
        )
        script.chmod(0o755)
    return script


def test_diagnostics_never_run_a_node_planted_in_the_current_directory(
    tmp_path, monkeypatch
):
    clone = tmp_path / "untrusted-clone"
    marker = tmp_path / "planted-node-ran"
    _plant_node(clone, marker, "v99.0.0")
    monkeypatch.chdir(str(clone))
    # Python's shutil.which searches the current directory first on Windows
    # unless this variable is set; the Claude Code harness sets it.
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    # A non-empty PATH without node: shutil.which returns None outright for
    # an empty PATH, which would hide the current-directory search.
    elsewhere = tmp_path / "no-node-here"
    elsewhere.mkdir()
    monkeypatch.setenv("PATH", str(elsewhere))

    metadata = resolve_command_metadata("node")

    assert metadata["found"] is False
    assert metadata["resolved_path"] is None
    assert not marker.exists()
    # A relative PATH entry naming the clone is skipped as well.
    monkeypatch.setenv("PATH", ".")
    assert find_command("node") is None
    assert resolve_command_metadata("node")["found"] is False
    assert not marker.exists()


def test_diagnostics_resolve_commands_from_absolute_path_entries(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    marker = tmp_path / "trusted-node-ran"
    script = _plant_node(tools, marker, "v20.11.1")
    monkeypatch.chdir(str(tmp_path))
    monkeypatch.setenv("PATH", str(tools))

    metadata = resolve_command_metadata("node")

    assert metadata["found"] is True
    assert os.path.normcase(metadata["resolved_path"]) == os.path.normcase(str(script))
    assert metadata["version"] == "v20.11.1"
    assert marker.exists()
    if os.name == "nt":
        # A name that already carries a PATHEXT extension is found as given.
        (tools / "npm.cmd").write_text("@echo off\r\necho 10.0.0\r\n", "ascii")
        assert find_command("npm.cmd") == str(tools / "npm.cmd")
