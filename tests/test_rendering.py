"""Tests for rendered managed content, including live launcher behavior."""

import os
import subprocess

import pytest

from claude_wrangler import __version__
from claude_wrangler.rendering import (
    build_available_versions_table,
    build_gitignore_block,
    build_install_summary,
    build_launcher_content,
    build_local_readme_content,
    build_metadata,
)

STUB_CLAUDE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'printf \'%s\\n\' "$@" > "$CAPTURED_ARGS"\n'
    'printf \'HOME=%s\\n\' "${HOME:-}" > "$CAPTURED_ENV"\n'
    'printf \'CLAUDE_CONFIG_DIR=%s\\n\' "${CLAUDE_CONFIG_DIR:-}" >> "$CAPTURED_ENV"\n'
)


def write_launcher_fixture(config):
    """Render the launcher plus stub node and claude binaries for execution."""

    tools_dir = config.project_root / "tools"
    local_bin_dir = config.layout.local_node_modules_dir / ".bin"
    tools_dir.mkdir()
    local_bin_dir.mkdir(parents=True)
    config.layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)
    config.layout.launcher_path.write_text(
        build_launcher_content(config),
        encoding="utf-8",
    )
    config.layout.launcher_path.chmod(0o755)
    (tools_dir / "node").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (tools_dir / "node").chmod(0o755)
    (local_bin_dir / "claude").write_text(STUB_CLAUDE, encoding="utf-8")
    (local_bin_dir / "claude").chmod(0o755)
    return tools_dir


def launcher_environment(config, tools_dir):
    env = os.environ.copy()
    env["PATH"] = "{}{}{}".format(tools_dir, os.pathsep, env.get("PATH", ""))
    env["CLAUDE_LOCAL_PREFLIGHT"] = "off"
    env["CAPTURED_ARGS"] = str(config.project_root / "captured-args.txt")
    env["CAPTURED_ENV"] = str(config.project_root / "captured-env.txt")
    env.pop("CLAUDE_LOCAL_SHARED_HOME", None)
    return env


def run_launcher(config, caller_args, extra_env=None):
    """Run one rendered launcher against the stub local Claude binary."""

    tools_dir = write_launcher_fixture(config)
    env = launcher_environment(config, tools_dir)
    if extra_env:
        env.update(extra_env)

    subprocess.run(
        [str(config.layout.launcher_path), *caller_args],
        cwd=str(config.project_root),
        env=env,
        check=True,
    )
    captured_args = (
        (config.project_root / "captured-args.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    captured_env = dict(
        line.split("=", 1)
        for line in (config.project_root / "captured-env.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    return captured_args, captured_env


def test_build_gitignore_block_covers_managed_artifacts(tmp_path, config_factory):
    config = config_factory(tmp_path)
    block = build_gitignore_block(config.layout)
    lines = block.splitlines()
    assert ".local/" in lines
    assert "bin/claude-local" in lines
    assert "README-LOCAL-Start-Claude.md" in lines
    # Claude Code keeps trackable project settings in .claude/; the managed
    # block must never ignore it.
    assert ".claude" not in lines
    assert ".claude/" not in lines


def test_build_gitignore_block_adds_relocated_directories(tmp_path, config_factory):
    config = config_factory(
        tmp_path,
        local_dir_raw="vendor/claude-npm",
    )
    block = build_gitignore_block(config.layout)
    lines = block.splitlines()
    assert ".local/" in lines
    assert "vendor/claude-npm/" in lines


def test_build_local_readme_references_console_command(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    readme = build_local_readme_content(config)
    assert "claude-wrangler --inspect ." in readme
    assert "claude-wrangler --selftest ." in readme
    assert "Reasonable permissions default: `disabled`" in readme
    assert "claude-wrangler --set-reasonable-permissions ." in readme
    assert "~/bin" not in readme


def test_build_local_readme_documents_security_rings(tmp_path, config_factory):
    config = config_factory(tmp_path)
    readme = build_local_readme_content(config)
    assert "## Security Rings" in readme
    assert "Ring 3" in readme
    assert "Ring 0" in readme
    assert "CLAUDE_LOCAL_SHARED_HOME=1" in readme
    assert "never forks Claude auth or session history" in readme


def test_build_local_readme_describes_accept_edits_reasonable_permissions(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, reasonable_permissions_enabled=True)

    readme = build_local_readme_content(config)

    assert "Reasonable permissions default: `enabled`" in readme
    assert "`--permission-mode acceptEdits`" in readme
    assert "commit and push" in readme
    assert "never injects" in readme


def test_build_metadata_uses_single_source_script_version(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)
    metadata = build_metadata(config)
    assert metadata["script_version"] == __version__
    assert metadata["reasonable_permissions_enabled"] is False
    assert metadata["paths"]["claude_config_dir"] == ".local/claude"


def test_build_install_summary_distinguishes_completed_and_dry_run_install(
    tmp_path,
    config_factory,
):
    completed = build_install_summary(config_factory(tmp_path))
    dry_run = build_install_summary(config_factory(tmp_path, dry_run=True))
    repaired = build_install_summary(config_factory(tmp_path, repair_install=True))

    assert "Package install: npm install completed" in completed
    assert "Package install: npm install required" in dry_run
    assert "Package install: repair cleanup plus npm install completed" in repaired


def test_build_launcher_content_includes_runtime_preflight(tmp_path, config_factory):
    config = config_factory(tmp_path)

    launcher = build_launcher_content(config)

    assert "CLAUDE_LOCAL_PREFLIGHT:-warn" in launcher
    assert "command -v node" in launcher
    assert ".nvmrc" in launcher
    assert "handle_preflight_mismatch" in launcher
    assert "emit_update_notice" in launcher
    assert "available update on ${channel}" in launcher
    assert 'reasonable_permissions_enabled="0"' in launcher
    assert 'local_claude_bin="$local_prefix/node_modules/.bin/claude"' in launcher
    assert (
        'exec "$local_claude_bin" "${default_claude_args[@]}" "${launcher_args[@]}"'
        in launcher
    )
    assert "exec npx" not in launcher


def test_build_launcher_content_pins_config_dir_and_ring_selection(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)

    launcher = build_launcher_content(config)

    assert 'export CLAUDE_CONFIG_DIR="$repo_root/.local/claude"' in launcher
    assert 'shared_home="${CLAUDE_LOCAL_SHARED_HOME:-0}"' in launcher
    assert "--shared-home)" in launcher
    assert "--isolated-home)" in launcher
    assert 'export HOME="$repo_root/.local/claude/HOME"' in launcher


def test_build_launcher_content_disables_claude_self_update(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)

    launcher = build_launcher_content(config)

    assert 'export DISABLE_UPDATES="1"' in launcher
    assert 'export DISABLE_AUTOUPDATER="1"' in launcher
    assert 'export DISABLE_INSTALLATION_CHECKS="1"' in launcher
    # The exports must precede the exec so every session inherits them.
    assert launcher.index('export DISABLE_UPDATES="1"') < launcher.index(
        'exec "$local_claude_bin"'
    )


def test_build_launcher_content_includes_reasonable_permissions_override_scan(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, reasonable_permissions_enabled=True)

    launcher = build_launcher_content(config)

    assert 'reasonable_permissions_enabled="1"' in launcher
    assert "--permission-mode|--dangerously-skip-permissions" in launcher
    assert "--permission-mode=*" in launcher
    assert "--dangerously-skip-permissions" in launcher


def test_enabled_launcher_injects_reasonable_permissions_defaults(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, reasonable_permissions_enabled=True)

    captured_args, _ = run_launcher(config, ["--resume"])

    assert captured_args == [
        "--permission-mode",
        "acceptEdits",
        "--resume",
    ]


@pytest.mark.parametrize(
    "caller_args",
    [
        ["--permission-mode", "plan"],
        ["--permission-mode=plan"],
        ["--dangerously-skip-permissions"],
    ],
)
def test_enabled_launcher_preserves_explicit_permission_choices(
    tmp_path,
    config_factory,
    caller_args,
):
    config = config_factory(tmp_path, reasonable_permissions_enabled=True)

    captured_args, _ = run_launcher(config, caller_args)

    assert captured_args == caller_args


def test_disabled_launcher_does_not_inject_reasonable_permissions_defaults(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, reasonable_permissions_enabled=False)

    captured_args, _ = run_launcher(config, ["--resume"])

    assert captured_args == ["--resume"]


def test_launcher_redirects_home_and_pins_config_dir_by_default(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)

    _, captured_env = run_launcher(config, ["--version"])

    assert captured_env["HOME"] == str(tmp_path / ".local" / "claude" / "HOME")
    assert captured_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".local" / "claude")


def test_launcher_shared_home_flag_keeps_real_home_and_config_pin(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)

    captured_args, captured_env = run_launcher(config, ["--shared-home", "--version"])

    assert captured_args == ["--version"]
    assert captured_env["HOME"] == os.environ.get("HOME", "")
    assert captured_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".local" / "claude")


def test_launcher_shared_home_environment_variable_reaches_ring_zero(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path)

    _, captured_env = run_launcher(
        config,
        ["--version"],
        extra_env={"CLAUDE_LOCAL_SHARED_HOME": "1"},
    )

    assert captured_env["HOME"] == os.environ.get("HOME", "")
    assert captured_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".local" / "claude")


def test_launcher_isolated_home_flag_overrides_shared_default(
    tmp_path,
    config_factory,
):
    config = config_factory(tmp_path, shared_home=True)

    captured_args, captured_env = run_launcher(
        config,
        ["--isolated-home", "--version"],
    )

    assert captured_args == ["--version"]
    assert captured_env["HOME"] == str(tmp_path / ".local" / "claude" / "HOME")


def test_launcher_refuses_missing_local_claude_binary(tmp_path, config_factory):
    config = config_factory(tmp_path)
    tools_dir = config.project_root / "tools"
    tools_dir.mkdir()
    config.layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)
    config.layout.launcher_path.write_text(
        build_launcher_content(config),
        encoding="utf-8",
    )
    config.layout.launcher_path.chmod(0o755)
    (tools_dir / "node").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (tools_dir / "node").chmod(0o755)
    (tools_dir / "npx").write_text(
        "#!/usr/bin/env bash\necho should-not-run >&2\nexit 99\n",
        encoding="utf-8",
    )
    (tools_dir / "npx").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = "{}{}{}".format(tools_dir, os.pathsep, env.get("PATH", ""))
    env["CLAUDE_LOCAL_PREFLIGHT"] = "off"
    completed = subprocess.run(
        [str(config.layout.launcher_path), "--version"],
        cwd=str(config.project_root),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert (
        "Local Claude Code executable is missing or not executable" in completed.stderr
    )
    assert "should-not-run" not in completed.stderr


def test_build_available_versions_table_lists_supported_channels():
    table = build_available_versions_table(
        {
            "stable": "2.1.153",
            "latest": "2.1.172",
            "next": None,
        }
    )

    assert "Channel" in table
    assert "stable" in table
    assert "latest" in table
    assert "next" in table
    assert "unavailable" in table
