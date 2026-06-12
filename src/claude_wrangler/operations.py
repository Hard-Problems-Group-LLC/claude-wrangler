"""Operational behaviors for install, inspect, uninstall, and self-test."""

from __future__ import annotations

from dataclasses import replace
import json
import os
import platform
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional

from .constants import (
    CLAUDE_PACKAGE_NAME,
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    README_MARKER,
    SCHEMA_VERSION,
    SCRIPT_NAME,
    SCRIPT_MARKER,
    SCRIPT_VERSION,
)
from .environment import collect_runtime_diagnostics
from .filesystem import (
    ensure_managed_directories,
    maybe_remove_empty_parent,
    require_safe_managed_path,
    remove_file_if_managed,
    remove_gitignore_block,
    remove_tree,
    upsert_gitignore_block,
    write_metadata,
    write_text_file,
)
from .layout import (
    infer_installed_version_from_lockfile,
    infer_requested_version_from_package_json,
    read_json_file,
)
from .models import ClaudeWranglerError, Config, SelfTestResult
from .releases import (
    fetch_available_claude_versions,
    resolve_install_version,
    resolve_upgrade_version,
)
from .rendering import (
    build_available_versions_table,
    build_gitignore_block,
    build_install_summary,
    build_known_versions,
    build_launcher_content,
    build_local_package_json,
    build_local_readme_content,
    build_metadata,
    local_package_json_looks_managed,
)
from .runtime import (
    detect_npm_binaries,
    ensure_command_exists,
    eprint,
    run_command,
    utc_now_iso,
)

CLAUDE_VERSION_OUTPUT_PATTERN = re.compile(r"^\d+\.\d+\.\d+")


def ensure_gitignore_block(config: Config) -> None:
    """Ensure the managed `.gitignore` block exists for local artifacts."""

    upsert_gitignore_block(
        config.layout.gitignore_path,
        build_gitignore_block(config.layout),
        dry_run=config.dry_run,
    )


def write_managed_supporting_files(config: Config) -> None:
    """Write the managed launcher, README, .gitignore block, and metadata."""

    ensure_managed_directories(config)
    write_text_file(
        config.layout.launcher_path,
        build_launcher_content(config),
        force=config.force,
        dry_run=config.dry_run,
        executable=True,
        managed_markers=(SCRIPT_MARKER,),
    )
    write_text_file(
        config.layout.readme_path,
        build_local_readme_content(config),
        force=config.force,
        dry_run=config.dry_run,
        managed_markers=(README_MARKER,),
    )
    ensure_gitignore_block(config)
    write_metadata(
        config.layout.metadata_path,
        build_metadata(config),
        dry_run=config.dry_run,
    )


def require_existing_managed_install(config: Config) -> None:
    """Reject update-like operations when no managed install can be proven."""

    metadata_exists = config.layout.metadata_path.exists()
    package_json_exists = config.layout.local_package_json_path.exists()
    if metadata_exists or package_json_exists:
        return
    raise ClaudeWranglerError(
        "No managed Claude install was found under {}. Run `claude-wrangler .` "
        "first, then rerun `claude-wrangler --update`.".format(config.project_root)
    )


def npm_local_environment(config: Config) -> Dict[str, str]:
    """Return an environment that keeps npm cache writes project-local."""

    env = dict(os.environ)
    env["NPM_CONFIG_CACHE"] = str(config.layout.local_dir / ".npm-cache")
    return env


def npm_install_environment(config: Config) -> Dict[str, str]:
    """Return an environment that avoids optional install-time npm checks."""

    env = npm_local_environment(config)
    env["NPM_CONFIG_AUDIT"] = "false"
    env["NPM_CONFIG_FOREGROUND_SCRIPTS"] = "true"
    env["NPM_CONFIG_FUND"] = "false"
    env["NPM_CONFIG_LOGLEVEL"] = config.npm_install_loglevel
    env["NPM_CONFIG_PROGRESS"] = "false"
    env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    return env


def local_claude_bin_path(config: Config) -> Path:
    """Return the expected local Claude executable path for this platform."""

    binary_name = "claude.cmd" if os.name == "nt" else "claude"
    return config.layout.local_node_modules_dir / ".bin" / binary_name


def managed_npm_install_command(config: Config, npm_name: str) -> List[str]:
    """Return the npm install command for the managed Claude package tree."""

    return [
        npm_name,
        "install",
        "--prefix",
        str(config.layout.local_dir),
        "--no-audit",
        "--no-fund",
        "--foreground-scripts",
        "--loglevel={}".format(config.npm_install_loglevel),
        "--progress=false",
    ]


def local_claude_package_json_path(config: Config) -> Path:
    """Return the managed Claude Code package manifest path."""

    scope, name = CLAUDE_PACKAGE_NAME.split("/", 1)
    return config.layout.local_node_modules_dir / scope / name / "package.json"


def expected_claude_platform_package_names() -> List[str]:
    """Return the acceptable platform packages for this host.

    Claude Code ships per-platform optional dependencies. Linux hosts may
    legitimately receive either the glibc or the musl variant, so both are
    returned as acceptable candidates there.
    """

    raw_machine = platform.machine().lower()
    if raw_machine in ("x86_64", "amd64"):
        arch = "x64"
    elif raw_machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        return []

    if sys.platform.startswith("linux"):
        base = "{}-linux-{}".format(CLAUDE_PACKAGE_NAME, arch)
        return [base, "{}-musl".format(base)]
    if sys.platform == "darwin":
        return ["{}-darwin-{}".format(CLAUDE_PACKAGE_NAME, arch)]
    if sys.platform in ("win32", "cygwin", "msys"):
        return ["{}-win32-{}".format(CLAUDE_PACKAGE_NAME, arch)]
    return []


def local_platform_package_json_path(
    config: Config,
    package_name: str,
) -> Path:
    """Return the expected platform package manifest path for one candidate."""

    scope, name = package_name.split("/", 1)
    return config.layout.local_node_modules_dir / scope / name / "package.json"


def first_installed_platform_package(
    config: Config,
    package_names: List[str],
) -> Optional[Path]:
    """Return the first candidate platform package manifest that exists."""

    for package_name in package_names:
        candidate = local_platform_package_json_path(config, package_name)
        if candidate.exists():
            return candidate
    return None


def read_package_version(package_json_path: Optional[Path]) -> Optional[str]:
    """Read a package.json version string, or return None when unavailable."""

    if package_json_path is None or not package_json_path.exists():
        return None
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) and version else None


def infer_requested_version_with_error(
    package_json_path: Path,
) -> tuple[Optional[str], Optional[str]]:
    """Infer requested Claude version and preserve parse errors for inspection."""

    try:
        return infer_requested_version_from_package_json(package_json_path), None
    except ClaudeWranglerError as exc:
        return None, str(exc)


def infer_lockfile_version_with_error(
    lockfile_path: Path,
) -> tuple[Optional[str], Optional[str]]:
    """Infer installed Claude version and preserve parse errors for inspection."""

    try:
        return infer_installed_version_from_lockfile(lockfile_path), None
    except ClaudeWranglerError as exc:
        return None, str(exc)


def read_metadata_with_error(
    metadata_path: Path,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read managed metadata and preserve parse errors for inspection."""

    try:
        return read_json_file(metadata_path), None
    except ClaudeWranglerError as exc:
        return None, str(exc)


def looks_like_claude_version_output(version_text: str) -> bool:
    """Return True when one --version output looks like Claude Code's."""

    if not version_text:
        return False
    if "claude code" in version_text.lower():
        return True
    return CLAUDE_VERSION_OUTPUT_PATTERN.match(version_text) is not None


def verify_local_claude_binary(config: Config) -> None:
    """Run a cheap smoke test against the managed local Claude executable."""

    local_claude_bin = local_claude_bin_path(config)
    if not local_claude_bin.exists():
        raise ClaudeWranglerError(
            "Local Claude executable was not created: {}. The npm install is "
            "incomplete; rerun the install or upgrade after repairing the "
            "managed local directory.".format(local_claude_bin)
        )
    if os.name != "nt" and not os.access(local_claude_bin, os.X_OK):
        raise ClaudeWranglerError(
            "Local Claude executable is not executable: {}".format(local_claude_bin)
        )

    version_result = run_command(
        [str(local_claude_bin), "--version"],
        cwd=str(config.project_root),
        capture_output=True,
    )
    version_text = (
        (version_result.stdout or "") + (version_result.stderr or "")
    ).strip()
    if not looks_like_claude_version_output(version_text):
        raise ClaudeWranglerError(
            "Local Claude executable did not report a Claude Code version: "
            "{}".format(version_text or "<no output>")
        )


def remove_managed_install_file(config: Config, path: Path, label: str) -> bool:
    """Remove one managed npm artifact file after path safety checks."""

    if not path.exists():
        return False
    if not path.is_file():
        raise ClaudeWranglerError(
            "Expected {} to be a file, but found something else: {}".format(
                label,
                path,
            )
        )
    require_safe_managed_path(path, config.project_root, label)
    action = "Would remove" if config.dry_run else "Removing"
    eprint("[claude-wrangler] {} {}: {}".format(action, label, path))
    if config.dry_run:
        return True
    path.unlink()
    return True


def repair_managed_npm_install(config: Config) -> None:
    """Remove managed npm install artifacts before a clean reinstall."""

    remove_tree(
        config.layout.local_node_modules_dir,
        label="managed local node_modules",
        project_root=config.project_root,
        dry_run=config.dry_run,
    )
    remove_managed_install_file(
        config,
        config.layout.local_package_lock_path,
        label="managed local package-lock",
    )
    remove_tree(
        config.layout.local_dir / ".npm-cache" / "_npx",
        label="managed local npx scratch cache",
        project_root=config.project_root,
        dry_run=config.dry_run,
    )
    remove_tree(
        config.layout.local_dir / ".npm-cache" / "_cacache" / "tmp",
        label="managed local npm cache temp files",
        project_root=config.project_root,
        dry_run=config.dry_run,
    )


def install_like_operation(config: Config) -> int:
    """Shared implementation for install and upgrade-style operations."""

    if config.reconfigure_only:
        resolved_config = config
    else:
        npm_name, _ = detect_npm_binaries()
        ensure_command_exists("node")
        ensure_command_exists(npm_name)

        if config.operation == "upgrade":
            resolved_config = resolve_upgrade_version(config)
        else:
            resolved_config = resolve_install_version(
                config,
                npm_name,
                env=npm_local_environment(config),
                timeout_seconds=config.npm_timeout_seconds,
            )

    eprint(
        "[claude-wrangler] Target project root: {}".format(resolved_config.project_root)
    )
    eprint("[claude-wrangler] Operation: {}".format(resolved_config.operation))
    eprint(
        "[claude-wrangler] Claude selector: {}".format(resolved_config.claude_selector)
    )
    eprint(
        "[claude-wrangler] Claude version: {}".format(resolved_config.claude_version)
    )
    eprint(
        "[claude-wrangler] Version source: {}".format(resolved_config.version_source)
    )
    eprint(
        "[claude-wrangler] HOME isolation: {}".format(
            "shared user HOME"
            if resolved_config.shared_home
            else "project-local isolated HOME"
        )
    )
    eprint(
        "[claude-wrangler] Pinned CLAUDE_CONFIG_DIR: {}".format(
            resolved_config.layout.config_dir
        )
    )
    eprint(
        "[claude-wrangler] Reasonable permissions default: {}".format(
            "enabled" if resolved_config.reasonable_permissions_enabled else "disabled"
        )
    )

    write_text_file(
        resolved_config.layout.local_package_json_path,
        build_local_package_json(resolved_config.claude_version),
        force=resolved_config.force,
        dry_run=resolved_config.dry_run,
        managed_content_predicate=local_package_json_looks_managed,
    )
    write_managed_supporting_files(resolved_config)

    if resolved_config.reconfigure_only:
        eprint(
            "[claude-wrangler] Skipping npm install because this command only "
            "updates managed launcher state."
        )
    elif resolved_config.dry_run:
        if resolved_config.repair_install:
            repair_managed_npm_install(resolved_config)
        eprint(
            "[claude-wrangler] Would set: NPM_CONFIG_CACHE={}".format(
                npm_local_environment(resolved_config)["NPM_CONFIG_CACHE"]
            )
        )
        eprint(
            "[claude-wrangler] Would run: {}".format(
                " ".join(managed_npm_install_command(resolved_config, npm_name))
            )
        )
    elif resolved_config.skip_install:
        eprint("[claude-wrangler] Skipping npm install by request")
    else:
        if resolved_config.repair_install:
            repair_managed_npm_install(resolved_config)
        run_command(
            managed_npm_install_command(resolved_config, npm_name),
            cwd=str(resolved_config.project_root),
            env=npm_install_environment(resolved_config),
            timeout_seconds=resolved_config.npm_timeout_seconds,
        )
        verify_local_claude_binary(resolved_config)

    print(build_install_summary(resolved_config))
    return 0


def update_operation(config: Config) -> int:
    """Refresh the locally known Claude channel versions without upgrading."""

    require_existing_managed_install(config)

    npm_name, _ = detect_npm_binaries()
    ensure_command_exists("node")
    ensure_command_exists(npm_name)

    available_versions = fetch_available_claude_versions(
        npm_name,
        config.project_root,
        env=npm_local_environment(config),
        timeout_seconds=config.npm_timeout_seconds,
    )
    updated_config = replace(
        config,
        available_versions=available_versions,
        available_versions_updated_at=utc_now_iso(),
    )

    eprint(
        "[claude-wrangler] Target project root: {}".format(updated_config.project_root)
    )
    eprint("[claude-wrangler] Operation: update")
    eprint("[claude-wrangler] Refreshing locally known stable/latest/next versions")

    write_managed_supporting_files(updated_config)

    print(
        "NOTE: This command updates available version information, but does not "
        'perform an upgrade; use "claude-wrangler --upgrade --channel '
        '<stable|latest|next> [--version <latest|x.y.z>]" for that.'
    )
    print("")
    print(build_available_versions_table(updated_config.available_versions))
    if updated_config.available_versions_updated_at:
        print("")
        print("Updated at: {}".format(updated_config.available_versions_updated_at))
    return 0


def launcher_matches_expected(config: Config) -> bool:
    """Return True when the launcher exactly matches the expected content."""

    path = config.layout.launcher_path
    if not path.exists() or not path.is_file():
        return False
    actual = path.read_text(encoding="utf-8")
    return actual == build_launcher_content(config)


def readme_matches_expected(config: Config) -> bool:
    """Return True when the generated local README exactly matches expectation."""

    path = config.layout.readme_path
    if not path.exists() or not path.is_file():
        return False
    actual = path.read_text(encoding="utf-8")
    return actual == build_local_readme_content(config)


def gather_inspection_report(config: Config) -> Dict[str, Any]:
    """Collect a conservative JSON inspection report for the target project."""

    npm_name, npx_name = detect_npm_binaries()
    metadata, metadata_parse_error = read_metadata_with_error(
        config.layout.metadata_path
    )
    runtime_environment = collect_runtime_diagnostics(
        config.project_root,
        config.shared_home,
        config.layout.claude_home_dir,
        config.layout.config_dir,
        npm_name,
        npx_name,
    )

    gitignore_text = (
        config.layout.gitignore_path.read_text(encoding="utf-8")
        if config.layout.gitignore_path.exists()
        else ""
    )
    gitignore_managed_block_present = (
        GITIGNORE_BEGIN in gitignore_text and GITIGNORE_END in gitignore_text
    )
    local_claude_bin = local_claude_bin_path(config)
    local_claude_package_json = local_claude_package_json_path(config)
    expected_platform_packages = expected_claude_platform_package_names()
    installed_platform_package_json = first_installed_platform_package(
        config,
        expected_platform_packages,
    )
    requested_claude_version, local_package_json_parse_error = (
        infer_requested_version_with_error(config.layout.local_package_json_path)
    )
    installed_lockfile_version, local_package_lock_parse_error = (
        infer_lockfile_version_with_error(config.layout.local_package_lock_path)
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "script_name": SCRIPT_NAME,
        "script_version": SCRIPT_VERSION,
        "generated_at": utc_now_iso(),
        "project_root": str(config.project_root),
        "operation": "inspect",
        "known_versions": build_known_versions(
            config.available_versions,
            config.available_versions_updated_at,
        ),
        "requested_configuration": {
            "claude_selector": config.claude_selector,
            "claude_channel": config.claude_channel,
            "claude_version": config.claude_version,
            "shared_home": config.shared_home,
            "reasonable_permissions_enabled": (config.reasonable_permissions_enabled),
            "version_source": config.version_source,
        },
        "runtime_environment": runtime_environment,
        "paths": {
            "local_dir": str(config.layout.local_dir),
            "claude_home_dir": str(config.layout.claude_home_dir),
            "claude_config_dir": str(config.layout.config_dir),
            "launcher": str(config.layout.launcher_path),
            "readme_local": str(config.layout.readme_path),
            "local_package_json": str(config.layout.local_package_json_path),
            "local_package_lock": str(config.layout.local_package_lock_path),
            "local_claude_bin": str(local_claude_bin),
            "local_claude_package_json": str(local_claude_package_json),
            "local_claude_platform_package_json": (
                str(installed_platform_package_json)
                if installed_platform_package_json is not None
                else None
            ),
            "metadata": str(config.layout.metadata_path),
            "gitignore": str(config.layout.gitignore_path),
        },
        "state": {
            "git_repository": (config.project_root / ".git").exists(),
            "local_dir_exists": config.layout.local_dir.exists(),
            "local_package_json_exists": config.layout.local_package_json_path.exists(),
            "local_package_lock_exists": config.layout.local_package_lock_path.exists(),
            "node_modules_exists": config.layout.local_node_modules_dir.exists(),
            "local_claude_bin_exists": local_claude_bin.exists(),
            "local_claude_bin_executable": (
                os.access(local_claude_bin, os.X_OK)
                if local_claude_bin.exists()
                else False
            ),
            "expected_claude_platform_packages": expected_platform_packages,
            "local_claude_platform_package_json_exists": (
                installed_platform_package_json is not None
            ),
            "metadata_exists": metadata is not None,
            "metadata_parse_error": metadata_parse_error,
            "metadata": metadata,
            "available_versions": dict(config.available_versions),
            "available_versions_updated_at": config.available_versions_updated_at,
            "reasonable_permissions_enabled": (config.reasonable_permissions_enabled),
            "claude_home_exists": config.layout.claude_home_dir.exists(),
            "claude_config_dir_exists": config.layout.config_dir.exists(),
            "launcher_exists": config.layout.launcher_path.exists(),
            "launcher_executable": (
                os.access(config.layout.launcher_path, os.X_OK)
                if config.layout.launcher_path.exists()
                else False
            ),
            "launcher_matches_expected": launcher_matches_expected(config),
            "readme_exists": config.layout.readme_path.exists(),
            "readme_matches_expected": readme_matches_expected(config),
            "gitignore_exists": config.layout.gitignore_path.exists(),
            "gitignore_managed_block_present": gitignore_managed_block_present,
            "requested_claude_version_in_package_json": requested_claude_version,
            "local_package_json_parse_error": local_package_json_parse_error,
            "installed_claude_version_in_package_dir": (
                read_package_version(local_claude_package_json)
            ),
            "installed_claude_platform_package_version": (
                read_package_version(installed_platform_package_json)
            ),
            "installed_claude_version_in_lockfile": installed_lockfile_version,
            "local_package_lock_parse_error": local_package_lock_parse_error,
            "node_found": runtime_environment["resolved_commands"]["node"]["found"],
            "npm_found": runtime_environment["resolved_commands"]["npm"]["found"],
            "npx_found": runtime_environment["resolved_commands"]["npx"]["found"],
        },
        "warnings": [],
        "issues": [],
    }

    state = report["state"]
    warnings = report["warnings"]
    issues = report["issues"]

    if not state["git_repository"]:
        warnings.append("Target project root does not appear to be a git repository.")
    if state["metadata_parse_error"]:
        issues.append(
            "Managed metadata could not be parsed: {}".format(
                state["metadata_parse_error"]
            )
        )
    if state["local_package_json_parse_error"]:
        issues.append(
            "Managed package.json could not be parsed: {}".format(
                state["local_package_json_parse_error"]
            )
        )
    if state["local_package_lock_parse_error"]:
        issues.append(
            "Managed package-lock.json could not be parsed: {}".format(
                state["local_package_lock_parse_error"]
            )
        )
    if state["local_package_json_exists"] and not state["node_modules_exists"]:
        issues.append(
            "Managed package manifest exists, but node_modules is missing. "
            "The install may be incomplete."
        )
    if state["node_modules_exists"] and not state["local_claude_bin_exists"]:
        issues.append(
            "Managed node_modules exists, but the local Claude executable is "
            "missing."
        )
    if state["local_claude_bin_exists"] and not state["local_claude_bin_executable"]:
        issues.append("Local Claude executable exists but is not executable.")
    if (
        state["expected_claude_platform_packages"]
        and state["node_modules_exists"]
        and not state["local_claude_platform_package_json_exists"]
    ):
        issues.append(
            "No expected platform package ({}) has its package.json under "
            "node_modules.".format(
                ", ".join(state["expected_claude_platform_packages"])
            )
        )
    if state["launcher_exists"] and not state["launcher_matches_expected"]:
        warnings.append(
            "Launcher exists but does not exactly match the expected "
            "generated content."
        )
    if state["readme_exists"] and not state["readme_matches_expected"]:
        warnings.append(
            "Local README exists but does not exactly match the expected "
            "generated content."
        )
    if not state["gitignore_managed_block_present"]:
        warnings.append("Managed .gitignore block is not present.")
    package_manager = runtime_environment["package_manager_declaration"]
    if package_manager and package_manager.get("read_error"):
        warnings.append(
            "Target project package.json could not be parsed for "
            "packageManager: {}".format(package_manager["read_error"])
        )
    selector_alignment = runtime_environment["selector_alignment"]
    if (
        selector_alignment["status"] in ("mismatch", "unknown")
        and selector_alignment["signals_present"]
    ):
        warnings.append(selector_alignment["summary"])
    requested_claude_version = state["requested_claude_version_in_package_json"]
    installed_package_dir_version = state["installed_claude_version_in_package_dir"]
    installed_lockfile_version = state["installed_claude_version_in_lockfile"]
    if (
        requested_claude_version
        and installed_package_dir_version
        and requested_claude_version != installed_package_dir_version
    ):
        issues.append(
            "Managed package.json requests {} {}, but node_modules contains "
            "{}.".format(
                CLAUDE_PACKAGE_NAME,
                requested_claude_version,
                installed_package_dir_version,
            )
        )
    if (
        requested_claude_version
        and installed_lockfile_version
        and requested_claude_version != installed_lockfile_version
    ):
        issues.append(
            "Managed package.json requests {} {}, but package-lock records "
            "{}.".format(
                CLAUDE_PACKAGE_NAME,
                requested_claude_version,
                installed_lockfile_version,
            )
        )
    if not state["node_found"] or not state["npm_found"]:
        issues.append("Required node/npm commands are not available in PATH.")
    if metadata and metadata.get("project_root") != str(config.project_root):
        issues.append(
            "Managed metadata project_root does not match the inspected "
            "project root."
        )

    return report


def inspect_operation(config: Config) -> int:
    """Emit a JSON inspection report to stdout."""

    report = gather_inspection_report(config)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["issues"] else 1


def uninstall_operation(config: Config) -> int:
    """Conservatively remove the managed installation.

    The managed config dir holds Claude auth and session history; like the
    codex-wrangler home-directory removal, it is removed only as part of a
    proven uninstall, after the launcher and README ownership checks.
    """

    report = gather_inspection_report(config)

    if not report["state"]["metadata_exists"] and not config.force:
        raise ClaudeWranglerError(
            "Managed metadata was not found at {}. Refusing to uninstall "
            "without --force because ownership cannot be proven.".format(
                config.layout.metadata_path
            )
        )

    remove_file_if_managed(
        config.layout.launcher_path,
        build_launcher_content(config),
        force=config.force,
        dry_run=config.dry_run,
        label="launcher",
    )
    remove_file_if_managed(
        config.layout.readme_path,
        build_local_readme_content(config),
        force=config.force,
        dry_run=config.dry_run,
        label="local README",
    )
    remove_gitignore_block(config.layout.gitignore_path, dry_run=config.dry_run)
    remove_tree(
        config.layout.local_dir,
        label="managed local directory",
        project_root=config.project_root,
        dry_run=config.dry_run,
    )
    if not config.shared_home:
        remove_tree(
            config.layout.claude_home_dir,
            label="managed Claude home directory",
            project_root=config.project_root,
            dry_run=config.dry_run,
        )
    remove_tree(
        config.layout.config_dir,
        label="managed Claude config directory",
        project_root=config.project_root,
        dry_run=config.dry_run,
    )
    maybe_remove_empty_parent(
        config.layout.config_dir,
        stop_at=config.project_root,
        dry_run=config.dry_run,
    )
    maybe_remove_empty_parent(
        config.layout.launcher_path,
        stop_at=config.project_root,
        dry_run=config.dry_run,
    )

    print(
        "Uninstall complete for {}{}.".format(
            config.project_root,
            " (dry-run)" if config.dry_run else "",
        )
    )
    return 0


def selftest_operation(config: Config) -> int:
    """Run a pessimistic verification suite against the managed install."""

    npm_name, _ = detect_npm_binaries()

    report = gather_inspection_report(config)
    results: List[SelfTestResult] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append(SelfTestResult(name=name, ok=ok, detail=detail))

    state = report["state"]
    runtime_environment = report["runtime_environment"]
    resolved_commands = runtime_environment["resolved_commands"]
    record(
        "metadata_exists", bool(state["metadata_exists"]), "managed metadata missing"
    )
    record(
        "launcher_matches_expected",
        bool(state["launcher_matches_expected"]),
        "launcher missing or not exactly as expected",
    )
    record(
        "readme_matches_expected",
        bool(state["readme_matches_expected"]),
        "local README missing or not exactly as expected",
    )
    record(
        "gitignore_managed_block_present",
        bool(state["gitignore_managed_block_present"]),
        "managed .gitignore block missing",
    )
    record(
        "installed_claude_version_present",
        bool(state["installed_claude_version_in_lockfile"]),
        "installed Claude version could not be inferred from package-lock.json",
    )
    record(
        "node_command_found",
        bool(state["node_found"]),
        "node command is not available in PATH",
    )
    record(
        "npm_command_found",
        bool(state["npm_found"]),
        "npm command is not available in PATH",
    )
    record(
        "local_claude_bin_exists",
        bool(state.get("local_claude_bin_exists")),
        "local Claude executable is missing from node_modules/.bin",
    )
    record(
        "local_claude_bin_executable",
        bool(state.get("local_claude_bin_executable")),
        "local Claude executable is not executable",
    )
    selector_alignment = runtime_environment["selector_alignment"]
    record(
        "runtime_selector_alignment",
        selector_alignment["status"] != "mismatch",
        selector_alignment["summary"],
    )

    if state["launcher_exists"] and state["launcher_matches_expected"]:
        try:
            version_result = run_command(
                [str(config.layout.launcher_path), "--version"],
                cwd=str(config.project_root),
                capture_output=True,
            )
        except ClaudeWranglerError as exc:
            record("launcher_version_runs", False, str(exc))
        else:
            version_text = (version_result.stdout or "").strip()
            record(
                "launcher_version_runs",
                looks_like_claude_version_output(version_text),
                "launcher did not report a Claude Code version",
            )

        try:
            help_result = run_command(
                [str(config.layout.launcher_path), "--help"],
                cwd=str(config.project_root),
                capture_output=True,
            )
        except ClaudeWranglerError as exc:
            record("launcher_help_runs", False, str(exc))
        else:
            help_text = (help_result.stdout or "") + (help_result.stderr or "")
            record(
                "launcher_help_runs",
                "claude" in help_text.lower(),
                "launcher --help did not return the expected usage text",
            )
    else:
        record(
            "launcher_commands_skipped",
            False,
            "launcher verification commands were skipped because the launcher "
            "is not trustworthy",
        )

    if resolved_commands["npm"]["found"]:
        try:
            audit_result = run_command(
                [
                    npm_name,
                    "audit",
                    "--prefix",
                    str(config.layout.local_dir),
                    "--json",
                ],
                cwd=str(config.project_root),
                capture_output=True,
                env=npm_local_environment(config),
                timeout_seconds=config.npm_timeout_seconds,
            )
        except ClaudeWranglerError as exc:
            record("npm_audit_clean", False, str(exc))
        else:
            try:
                audit_payload = json.loads(audit_result.stdout or "")
            except json.JSONDecodeError as exc:
                record(
                    "npm_audit_clean",
                    False,
                    "npm audit returned invalid JSON: {}".format(exc),
                )
            else:
                vulnerabilities = audit_payload.get("metadata", {}).get(
                    "vulnerabilities", {}
                )
                total_vulnerabilities = vulnerabilities.get("total")
                record(
                    "npm_audit_clean",
                    total_vulnerabilities == 0,
                    "npm audit reported {} total vulnerabilities".format(
                        total_vulnerabilities
                    ),
                )
    else:
        record("npm_audit_clean", False, "npm command is not available in PATH")

    failures = [result for result in results if not result.ok]
    for result in results:
        print(
            "[{}] {} - {}".format(
                "PASS" if result.ok else "FAIL",
                result.name,
                result.detail,
            )
        )

    if failures:
        print("")
        print("Self-test failed.")
        for result in failures:
            print(" - {}: {}".format(result.name, result.detail))
        return 1

    print("")
    print("Self-test passed.")
    return 0
