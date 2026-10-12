"""`--inspect` and `--selftest` for the native runtime.

Both are read-only and never migrate (legacy_npm_layout_migration.md 1).
They never read `<runtime root>/.lock`: on Windows a held byte-range lock
makes reads fail (ab_runtime_slots.md requirement 6). Node.js and npm are
reported only as information.

The inspection report covers the active-slot pointer, both slots (version,
completeness, retained evidence), `fallback.json`, `update-check.json`
(including a recorded refresh error), legacy and migration status,
generated-file freshness, the config-dir `settings.json` suppression keys,
the project's `.claude/settings*.json` warnings, and the launcher
environment for each ring on this host's operating system.

The self-test re-verifies both slots offline (signatures, SHA-256, and each
slot's exact file set), checks that the active binary still matches its
verification stamp, runs the active binary's `--version` in a scratch
environment, checks that `launch.py` is a byte-for-byte copy of the
packaged launcher core, and fails when the project's
`.claude/settings*.json` sets a suppression variable to a falsy value.
Both report managed files that git tracks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from . import launcher_core
from .constants import (
    SCHEMA_VERSION,
    SCRIPT_NAME,
    SCRIPT_VERSION,
    SELF_UPDATE_SUPPRESSION_ENV,
)
from .environment import collect_runtime_diagnostics
from .filesystem import gitignore_block_present
from .generator import (
    LegacyLaunch,
    generated_file_status,
    parsed_launch_config,
    settings_suppression_status,
)
from .migration import (
    has_npm_runtime,
    legacy_is_authoritative,
    select_legacy_launch_binary,
)
from .models import ClaudeWranglerError, Config, SelfTestResult
from .operations import describe_in_use, tracked_state_paths
from .rendering import build_known_versions, launcher_core_source
from .runtime import utc_now_iso
from .services import Services, default_services, scratch_environment
from .slots import (
    EVIDENCE_FILE_NAMES,
    RETIRED_DIR_NAME,
    SLOT_NAMES,
    STAGING_DIR_NAME,
    PointerError,
    RuntimeStore,
)
from .transaction import (
    native_runtime_complete,
    scratch_forbidden_roots,
    select_platform_key,
    verify_slot_offline,
)


def _read_json(path: Path) -> Dict[str, Any]:
    """Return `{"exists", "data", "error"}` for a small JSON file."""

    if not path.exists():
        return {"exists": False, "data": None, "error": None}
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"exists": True, "data": None, "error": str(exc)}
    if not isinstance(data, dict):
        return {"exists": True, "data": None, "error": "not a JSON object"}
    return {"exists": True, "data": data, "error": None}


def _entries(path: Path) -> List[str]:
    """List a directory's entry names, or [] when it is absent."""

    try:
        return sorted(os.listdir(str(path)))
    except OSError:
        return []


def _host_key(config: Config, services: Services) -> Optional[str]:
    """Return the host platform key or None when it cannot be determined."""

    try:
        return select_platform_key(services, config.platform_override)
    except ClaudeWranglerError:
        return None


def _legacy_launch(
    config: Config, services: Services, host_key: Optional[str]
) -> Optional[LegacyLaunch]:
    """Return the legacy launch the generator would render, if migration pends."""

    legacy = config.legacy
    if legacy is None or host_key is None:
        return None
    binary = select_legacy_launch_binary(
        config.project_root, legacy, host_key, services.check_structure
    )
    if binary is None:
        return None
    return LegacyLaunch(binary, legacy.version)


def _slot_report(
    store: RuntimeStore, name: str, host_key: Optional[str]
) -> Dict[str, Any]:
    """Describe one slot from its record without hashing binaries."""

    directory = store.slot_dir(name)
    record = store.slot_record(name)
    problem = store.slot_problem(name, host_key) if directory.is_dir() else None
    stamp_problem = (
        store.stamp_problem(name, host_key)
        if record is not None and host_key is not None
        else None
    )
    report: Dict[str, Any] = {
        "path": str(directory),
        "present": directory.is_dir(),
        "complete": directory.is_dir() and problem is None,
        "problem": (
            problem if directory.is_dir() else "slot {} is not installed".format(name)
        ),
        "version": record.version if record else None,
        "channel": record.channel if record else None,
        "platforms": sorted(record.platforms) if record else [],
        "build_date": record.build_date if record else None,
        "commit": record.commit if record else None,
        "schemes": list(record.schemes) if record else [],
        "subcommands": list(record.subcommands) if record else [],
        "settings_rejected": (
            list(record.settings_rejected)
            if record is not None and record.settings_rejected is not None
            else None
        ),
        "verified_at": record.verified_at if record else None,
        "promoted_by": record.promoted_by if record else None,
        # The launcher runs a slot only while its binary matches the stamp
        # maintenance wrote after the last full verification.
        "stamp_ok": record is not None and host_key is not None and not stamp_problem,
        "stamp_problem": stamp_problem,
        "evidence": {
            evidence: (directory / evidence).is_file()
            for evidence in EVIDENCE_FILE_NAMES
        },
    }
    report["evidence_complete"] = all(report["evidence"].values())
    return report


def gather_inspection_report(
    config: Config, services: Optional[Services] = None
) -> Dict[str, Any]:
    """Collect a conservative JSON inspection report for the target project."""

    services = services or default_services()
    layout = config.layout
    store = RuntimeStore(layout.local_dir, project_root=config.project_root)
    host_key = _host_key(config, services)
    launch = parsed_launch_config(config)
    runtime_environment = collect_runtime_diagnostics(
        config.project_root, launch, config.shared_home
    )

    pointer_value: Optional[str] = None
    pointer_problem: Optional[str] = None
    try:
        pointer_value = store.read_pointer()
        if pointer_value is None and store.root.is_dir():
            pointer_problem = "the active-slot pointer is missing"
    except PointerError as exc:
        pointer_problem = str(exc)
    slots = {name: _slot_report(store, name, host_key) for name in SLOT_NAMES}
    active_ok = pointer_value is not None and slots[pointer_value]["complete"]
    native_complete = host_key is not None and native_runtime_complete(store, host_key)
    native_generated_at = (
        config.existing_metadata.get("generated_at")
        if isinstance(config.existing_metadata, dict)
        else None
    )

    legacy = config.legacy
    migration_pending = legacy_is_authoritative(
        legacy, native_complete, native_generated_at
    )
    legacy_launch = (
        _legacy_launch(config, services, host_key) if migration_pending else None
    )
    generated = generated_file_status(config, legacy_launch)
    tracked = services.tracked_files(config.project_root, tracked_state_paths(config))
    fallback = _read_json(layout.fallback_path)
    update_check = _read_json(layout.update_check_path)
    settings = settings_suppression_status(layout.config_settings_path)
    suppression_keys = [name for name, _value in SELF_UPDATE_SUPPRESSION_ENV]
    project_settings_warnings = launcher_core.scan_project_settings(
        str(config.project_root), suppression_keys
    )

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "script_name": SCRIPT_NAME,
        "script_version": SCRIPT_VERSION,
        "generated_at": utc_now_iso(),
        "project_root": str(config.project_root),
        "operation": "inspect",
        "known_versions": build_known_versions(config),
        "requested_configuration": {
            "claude_selector": config.claude_selector,
            "claude_channel": config.claude_channel,
            "claude_version": config.claude_version,
            "shared_home": config.shared_home,
            "reasonable_permissions_enabled": config.reasonable_permissions_enabled,
            "update_checks": config.update_checks,
            "windows_sandbox_warning": config.windows_sandbox_warning,
            "release_base_url": config.release_base_url,
            "config_dir_spelling": config.config_dir_spelling,
            "version_source": config.version_source,
        },
        "runtime": {
            "root": str(store.root),
            "exists": store.root.is_dir(),
            "host_platform": host_key,
            "pointer": {"value": pointer_value, "problem": pointer_problem},
            "active_slot": pointer_value if active_ok else None,
            "inactive_slot": (
                ("b" if pointer_value == "a" else "a") if pointer_value else None
            ),
            "slots": slots,
            "complete": native_complete,
            "staging_entries": _entries(store.root / STAGING_DIR_NAME),
            "retired_entries": _entries(store.root / RETIRED_DIR_NAME),
            "fallback": fallback,
            "update_check": update_check,
            "processes": describe_in_use(config, services),
        },
        "legacy": {
            "detected": legacy is not None,
            "dir": str(legacy.dir) if legacy is not None else None,
            "version": legacy.version if legacy is not None else None,
            "channel": legacy.channel if legacy is not None else None,
            "npm_runtime_present": has_npm_runtime(legacy),
            "is_projection": legacy.is_projection if legacy is not None else False,
            "migration_pending": migration_pending,
            "migrated_from": config.legacy_migrated_from,
            "migrated_at": config.legacy_migrated_at,
            "cleanup_pending": (
                legacy.cleanup_residue
                if legacy is not None and not migration_pending
                else []
            ),
            "metadata_error": legacy.metadata_error if legacy is not None else None,
            "lockfile_error": legacy.lockfile_error if legacy is not None else None,
            "package_json_error": (
                legacy.package_json_error if legacy is not None else None
            ),
        },
        "generated_files": generated,
        "config_settings": settings,
        "project_settings_warnings": project_settings_warnings,
        "runtime_environment": runtime_environment,
        "paths": {
            "runtime_root": str(layout.local_dir),
            "claude_home_dir": str(layout.claude_home_dir),
            "claude_config_dir": str(layout.config_dir),
            "launcher": str(layout.launcher_path),
            "launcher_cmd": str(layout.launcher_cmd_path),
            "readme_local": str(layout.readme_path),
            "metadata": str(layout.metadata_path),
            "launch_core": str(layout.launch_core_path),
            "launch_config": str(layout.launch_config_path),
            "shim_dir": str(layout.shim_dir),
            "gitignore": str(layout.gitignore_path),
        },
        "state": {
            "git_repository": (config.project_root / ".git").exists(),
            "tracked_managed_files": tracked,
            "metadata_exists": config.existing_metadata is not None,
            "metadata_parse_error": config.existing_metadata_error,
            "metadata": config.existing_metadata,
            "gitignore_managed_block_present": gitignore_block_present(
                layout.gitignore_path
            ),
            "claude_home_exists": layout.claude_home_dir.exists(),
            "claude_config_dir_exists": layout.config_dir.exists(),
            "reasonable_permissions_enabled": config.reasonable_permissions_enabled,
        },
        "warnings": [],
        "issues": [],
    }

    warnings: List[str] = report["warnings"]
    issues: List[str] = report["issues"]
    state = report["state"]
    if not state["git_repository"]:
        warnings.append("Target project root does not appear to be a git repository.")
    if tracked:
        issues.append(
            "git tracks managed runtime, session-state, or launcher files ({}); a "
            "clone or pull can replace them with unverified content. Remove them "
            "from the repository.".format(", ".join(tracked[:10]))
        )
    if config.existing_metadata_error:
        issues.append(
            "Managed metadata could not be used: {}".format(
                config.existing_metadata_error
            )
        )
    if migration_pending:
        warnings.append(
            "Migration pending: the npm-era layout in {} is still in charge. Run "
            "`{} .`, `--upgrade`, or `--repair-install` to migrate.".format(
                legacy.dir if legacy is not None else "?", SCRIPT_NAME
            )
        )
    elif legacy is not None and legacy.cleanup_residue:
        warnings.append(
            "Legacy npm leftovers remain in {} ({}); the next install, upgrade, or "
            "repair retries the cleanup.".format(
                legacy.dir, ", ".join(legacy.cleanup_residue)
            )
        )
    if host_key is None:
        issues.append("This host's Claude Code platform could not be determined.")
    if store.root.is_dir() and not migration_pending:
        if pointer_problem:
            issues.append(
                "Active-slot pointer problem: {}. Run `{} --repair-install .`.".format(
                    pointer_problem, SCRIPT_NAME
                )
            )
        elif pointer_value is not None and not active_ok:
            issues.append(
                "Active slot {} is not usable: {}".format(
                    pointer_value, slots[pointer_value]["problem"]
                )
            )
        elif pointer_value is not None and slots[pointer_value]["stamp_problem"]:
            issues.append(
                "Active slot {} will not launch: {}. Run `{} --repair-install .` "
                "to verify it again offline.".format(
                    pointer_value, slots[pointer_value]["stamp_problem"], SCRIPT_NAME
                )
            )
    if config.existing_metadata is not None and not store.root.is_dir():
        issues.append("Metadata exists but the runtime root is missing.")
    if fallback["exists"]:
        warnings.append(
            "The launcher fell back to the other slot: {}. Run `{} --repair-install "
            ".`.".format(json.dumps(fallback["data"], sort_keys=True), SCRIPT_NAME)
        )
    if (
        update_check["exists"]
        and update_check["data"]
        and update_check["data"].get("error")
    ):
        warnings.append(
            "The last update check failed: {}".format(update_check["data"]["error"])
        )
    for key, entry in sorted(generated.items()):
        if entry["exists"] and not entry["fresh"]:
            warnings.append(
                "Generated file {} is stale; rerun `{} .` or `--repair-install` to "
                "regenerate it.".format(entry["path"], SCRIPT_NAME)
            )
        elif not entry["exists"] and (config.existing_metadata or migration_pending):
            warnings.append("Generated file {} is missing.".format(entry["path"]))
    if settings.get("parse_error"):
        issues.append(settings["parse_error"])
    elif settings["exists"] and settings.get("missing_or_wrong"):
        warnings.append(
            "{} lacks the suppression settings: {}".format(
                settings["path"], ", ".join(settings["missing_or_wrong"])
            )
        )
    for message in project_settings_warnings:
        warnings.append("Project settings: {}".format(message))
    if not state["gitignore_managed_block_present"]:
        warnings.append("Managed .gitignore block is not present.")
    selector = runtime_environment["selector_alignment"]
    if selector["status"] in ("mismatch", "unknown") and selector["signals_present"]:
        warnings.append(selector["summary"])
    package_manager = runtime_environment["package_manager_declaration"]
    if package_manager and package_manager.get("read_error"):
        warnings.append(
            "Target project package.json could not be parsed for packageManager: "
            "{}".format(package_manager["read_error"])
        )
    metadata = config.existing_metadata
    if metadata and metadata.get("project_root") != str(config.project_root):
        warnings.append(
            "Managed metadata project_root does not match the inspected project "
            "root (the checkout may have moved; `--repair-install` regenerates the "
            "launchers)."
        )
    return report


def inspect_operation(config: Config, services: Optional[Services] = None) -> int:
    """Emit a JSON inspection report to stdout; exit 1 when issues exist."""

    report = gather_inspection_report(config, services)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["issues"] else 1


def _falsy_suppression_findings(project_root: Path) -> List[str]:
    """Return project settings that set a suppression variable to a falsy value."""

    findings: List[str] = []
    for name in launcher_core.PROJECT_SETTINGS_FILES:
        path = project_root / ".claude" / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        env = data.get("env") if isinstance(data, dict) else None
        if not isinstance(env, dict):
            continue
        for key, _value in SELF_UPDATE_SUPPRESSION_ENV:
            if key in env and not launcher_core._setting_is_truthy(env[key]):
                findings.append(
                    ".claude/{} sets env.{} to {}".format(
                        name, key, json.dumps(env[key])
                    )
                )
    return findings


def format_launcher_environment(home_configuration: Dict[str, Any]) -> List[str]:
    """Return the selftest's informational block for the default-ring launch.

    `session_state_isolation_and_config_dir_pinning.md` requirement 7 asks
    selftest, like inspect, to show the effective `CLAUDE_CONFIG_DIR`,
    `HOME`, and `XDG_*` values and whether the home is isolated or shared.
    A variable the ring leaves alone is shown as inherited.
    """

    lines = [
        "Launcher environment at the default ring (ring {}, {}):".format(
            home_configuration["default_ring"], home_configuration["mode"]
        )
    ]
    environment = home_configuration["launcher_environment"]
    for name, value in environment.items():
        shown = value if value is not None else "(inherited from the caller)"
        lines.append("  {} = {}".format(name, shown))
    lines.append(
        "  Per session: {}".format("; ".join(home_configuration["escape_hatches"]))
    )
    return lines


def selftest_operation(config: Config, services: Optional[Services] = None) -> int:
    """Run a pessimistic verification suite against the managed install."""

    services = services or default_services()
    report = gather_inspection_report(config, services)
    results: List[SelfTestResult] = []

    def record(name: str, ok: bool, detail: str, warning: bool = False) -> None:
        """Append one check result."""

        results.append(SelfTestResult(name=name, ok=ok, detail=detail, warning=warning))

    store = RuntimeStore(config.layout.local_dir, project_root=config.project_root)
    host_key = report["runtime"]["host_platform"]
    legacy_state = report["legacy"]
    record(
        "metadata_exists",
        bool(report["state"]["metadata_exists"]),
        "schema-2 metadata at {}".format(config.layout.metadata_path),
    )
    record(
        "migration_not_pending",
        not legacy_state["migration_pending"],
        "the npm-era layout still needs migration; run `{} .`".format(SCRIPT_NAME),
        warning=bool(legacy_state["migration_pending"]),
    )

    pointer = report["runtime"]["pointer"]
    active = report["runtime"]["active_slot"]
    record(
        "active_slot_pointer_valid",
        active is not None,
        pointer["problem"] or "active slot {}".format(pointer["value"]),
    )
    verifications = {}
    for name in SLOT_NAMES:
        verification = verify_slot_offline(store, name, services, None)
        verifications[name] = verification
        if not verification.present:
            record(
                "slot_{}_verified".format(name),
                name != active,
                "slot {} is not installed".format(name),
                warning=name != active,
            )
            continue
        record(
            "slot_{}_verified".format(name),
            verification.ok,
            verification.problem
            or "slot {} (Claude Code {}) re-verified offline: signatures and "
            "SHA-256".format(name, verification.version),
        )

    if active is not None and host_key is not None and verifications[active].ok:
        stamp_problem = store.stamp_problem(active, host_key)
        record(
            "active_slot_stamp_matches",
            stamp_problem is None,
            stamp_problem
            or "slot {} binary matches the stamp of its last full verification".format(
                active
            ),
        )
        record_entry = verifications[active].record
        relative = record_entry.binary_relpath(host_key) if record_entry else None
        if relative is None or host_key not in (
            record_entry.platforms if record_entry else {}
        ):
            record(
                "active_binary_version_runs",
                False,
                "slot {} has no {} binary".format(active, host_key),
            )
        else:
            binary = store.slot_dir(active).joinpath(*relative.split("/"))
            scratch = Path(tempfile.mkdtemp(prefix="claude-wrangler-selftest-"))
            try:
                env = scratch_environment(
                    scratch,
                    services.environ,
                    forbidden_roots=scratch_forbidden_roots(config, services.environ),
                )
                services.smoke_runner(binary, record_entry.version, env, scratch)
            except ClaudeWranglerError as exc:
                record("active_binary_version_runs", False, str(exc))
            else:
                record(
                    "active_binary_version_runs",
                    True,
                    "{} --version printed {} (Claude Code) in a scratch "
                    "environment".format(binary, record_entry.version),
                )
            finally:
                shutil.rmtree(str(scratch), ignore_errors=True)
    else:
        record(
            "active_binary_version_runs",
            False,
            "skipped: no verified active slot for this host",
        )

    core_path = config.layout.launch_core_path
    try:
        core_matches = core_path.read_bytes() == launcher_core_source()
    except OSError:
        core_matches = False
    record(
        "launch_core_matches_package",
        core_matches,
        "{} is a byte-for-byte copy of the packaged launcher core".format(core_path),
    )
    for key, entry in sorted(report["generated_files"].items()):
        if key == "launch_py":
            continue
        record(
            "generated_{}_fresh".format(key),
            bool(entry["exists"] and entry["fresh"]),
            "{} matches the expected generated content".format(entry["path"]),
        )
    record(
        "gitignore_managed_block_present",
        bool(report["state"]["gitignore_managed_block_present"]),
        "managed .gitignore block",
    )
    tracked = report["state"]["tracked_managed_files"]
    record(
        "managed_files_untracked",
        not tracked,
        (
            "git tracks managed files: {}".format(", ".join(tracked[:10]))
            if tracked
            else "git tracks no runtime, session-state, or launcher files"
        ),
    )
    settings = report["config_settings"]
    record(
        "config_settings_suppress_updates",
        bool(settings.get("ok")),
        settings.get("parse_error")
        or (
            "{} lacks: {}".format(
                settings["path"], ", ".join(settings["missing_or_wrong"])
            )
            if settings.get("missing_or_wrong")
            else "{} carries the suppression settings".format(settings["path"])
        ),
    )
    falsy = _falsy_suppression_findings(config.project_root)
    record(
        "project_settings_keep_updates_disabled",
        not falsy,
        "; ".join(falsy)
        or "no .claude/settings*.json switches self-update suppression off",
    )
    # Claude Code itself needs no Node.js, so a missing `node` passes; an
    # explicit checked-in selector that the resolved `node` contradicts
    # still fails (runtime_fidelity_and_startup_preflight.md requirement 5).
    selector = report["runtime_environment"]["selector_alignment"]
    record(
        "runtime_selector_alignment",
        selector["status"] != "mismatch",
        selector["summary"],
    )

    failures = [result for result in results if not result.ok and not result.warning]
    for result in results:
        status = "PASS" if result.ok else ("WARN" if result.warning else "FAIL")
        print("[{}] {} - {}".format(status, result.name, result.detail))
    print("")
    for line in format_launcher_environment(
        report["runtime_environment"]["home_configuration"]
    ):
        print(line)
    if failures:
        print("")
        print("Self-test failed.")
        for result in failures:
            print(" - {}: {}".format(result.name, result.detail))
        return 1
    print("")
    print("Self-test passed.")
    return 0
