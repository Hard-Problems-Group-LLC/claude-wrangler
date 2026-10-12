"""Maintenance operations: install, upgrade, repair, migrate, update, rollback,
and uninstall of the project-local native Claude Code runtime.

Every mutating operation holds the runtime root's maintenance lock from
state selection through publication of generated files
(`ab_runtime_slots.md` requirement 4). The configuration is read before the
lock, so each operation re-checks a digest of the metadata it was derived
from once the lock is held and stops, changing nothing, when another
maintainer published in between; the CLI then re-reads the state and runs
the command again. The managed `.gitignore` block is written under the
lock too. Side effects that leave the machine
or run binaries go through a `services.Services` object so tests run fully
offline. Inspection and self-test live in `inspection.py`.

Nothing a repository can supply is trusted as verified: every operation
that keeps, reuses, or activates a slot re-verifies it fully offline and
records the verification stamp the launcher checks, and install-like
operations refuse when the runtime, session state, or launchers are
tracked by git (`--allow-tracked-state` overrides).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple
import uuid

from .atomicio import atomic_write_text, retry_on_windows_lock
from .constants import (
    FALLBACK_FILENAME,
    LAUNCH_CONFIG_FILENAME,
    LAUNCH_CORE_FILENAME,
    LEGACY_LOCAL_DIR,
    LEGACY_SCHEMA_VERSION,
    METADATA_FILENAME,
    PATH_SHIM_DIRNAME,
    SCRIPT_MARKER,
    SCRIPT_NAME,
    SCRIPT_VERSION,
    UPDATE_CHECK_FILENAME,
)
from .distribution import (
    DEFAULT_RELEASE_BASE_URL,
    VERSION_FLOOR,
    DistributionError,
    channel_deprecation_message,
    check_version_floor,
    is_exact_version,
)
from .filesystem import (
    maybe_remove_empty_parent,
    normalize_newlines,
    remove_gitignore_block,
    remove_tree,
    remove_tree_except,
    upsert_gitignore_block,
    write_metadata,
)
from .generator import (
    LegacyLaunch,
    check_config_settings,
    generated_file_status,
    launcher_session_environment,
    legacy_pending_alternatives,
    planned_files,
    remove_generated_files,
    write_generated_files,
)
from .migration import (
    build_projection,
    cleanup_legacy_tree,
    discover_legacy,
    find_adoptable_binary,
    legacy_binary_candidates,
    legacy_dir_candidates,
    legacy_dir_from_launcher,
    legacy_is_authoritative,
    projection_text,
    select_legacy_launch_binary,
)
from .layout import managed_state_fingerprint
from .models import (
    ClaudeWranglerError,
    Config,
    LegacyState,
    ManagedStateChangedError,
)
from .releases import (
    apply_catalog,
    newer_release_note,
    refresh_catalog,
    update_check_cache,
)
from .rendering import (
    build_available_versions_table,
    build_gitignore_block,
    build_install_summary,
    build_metadata,
)
from .runtime import eprint, format_utc
from .services import Services, default_services
from .slots import (
    LOCK_NAME,
    POINTER_NAME,
    RETIRED_DIR_NAME,
    SLOT_NAMES,
    SLOTS_DIR_NAME,
    STAGING_DIR_NAME,
    MaintenanceLockError,
    PointerError,
    RuntimeStore,
    check_runtime_root_placement,
    is_link_like,
)
from .slots import remove_tree as remove_runtime_tree
from .transaction import (
    RuntimeInUseError,
    active_slot_for,
    artifact_for,
    in_use_fail_closed,
    native_runtime_complete,
    other_slot,
    prepare_candidate,
    processes_under,
    promote_candidate,
    read_pointer_quietly,
    require_idle,
    select_platform_key,
    slot_binaries,
    verify_and_stamp,
)

# Re-exported for the CLI and tests.
__all__ = [
    "ManagedStateChangedError",
    "RuntimeInUseError",
    "ensure_gitignore_block",
    "install_like_operation",
    "rollback_operation",
    "uninstall_operation",
    "update_operation",
]

# How many tracked paths a refusal names before summarizing the rest.
TRACKED_PATHS_SHOWN = 10


def require_unchanged_state(config: Config) -> None:
    """Refuse to act on metadata another maintainer changed since it was read.

    Call with the maintenance lock held. A config built without a digest
    (for example, constructed directly) skips the check.
    """

    if config.state_fingerprint is None:
        return
    if managed_state_fingerprint(config.state_paths) != config.state_fingerprint:
        raise ManagedStateChangedError(
            "Another claude-wrangler run changed this project's managed state "
            "after this command read it; nothing was changed."
        )


def ensure_gitignore_block(config: Config) -> None:
    """Ensure the managed `.gitignore` block exists for local artifacts."""

    upsert_gitignore_block(
        config.layout.gitignore_path,
        build_gitignore_block(config.layout),
        dry_run=config.dry_run,
    )


def runtime_store(config: Config) -> RuntimeStore:
    """Return the slot store for the configured runtime root."""

    return RuntimeStore(config.layout.local_dir, project_root=config.project_root)


def check_runtime_placement(config: Config, environ: Mapping[str, str]) -> None:
    """Refuse a runtime root Claude Code owns or prunes, before creating it."""

    layout = config.layout
    home = layout.claude_home_dir
    extra = [
        home / ".config",
        home / ".cache",
        home / ".local" / "state",
        home / ".local" / "share",
    ]
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
    ):
        value = environ.get(name)
        if value:
            extra.append(Path(value))
    real_home = environ.get("HOME") or environ.get("USERPROFILE")
    if real_home:
        base = Path(real_home)
        extra.extend([base, base / ".local" / "share", base / ".local" / "state"])
    check_runtime_root_placement(
        layout.local_dir,
        home_dir=home,
        config_dir=layout.config_dir,
        extra_bases=extra,
    )
    _refuse_foreign_runtime_root(layout.local_dir)


# Entry names a claude-wrangler runtime root can hold; a non-empty directory
# with none of them is someone else's (uninstall empties the runtime root).
RUNTIME_ROOT_ENTRY_NAMES = (
    METADATA_FILENAME,
    LOCK_NAME,
    POINTER_NAME,
    SLOTS_DIR_NAME,
    STAGING_DIR_NAME,
    RETIRED_DIR_NAME,
    LAUNCH_CORE_FILENAME,
    LAUNCH_CONFIG_FILENAME,
    PATH_SHIM_DIRNAME,
    FALLBACK_FILENAME,
    UPDATE_CHECK_FILENAME,
)


def _refuse_foreign_runtime_root(root: Path) -> None:
    """Refuse a `--local-dir` that names an existing directory of other files.

    Uninstall empties the runtime root, so installing into a directory that
    already holds unrelated files (for example `--local-dir web`) would put
    those files at risk later.
    """

    try:
        if is_link_like(root) or not root.is_dir():
            return
        names = [name for name in os.listdir(str(root)) if name != ".DS_Store"]
    except OSError:
        return
    if names and not any(name in RUNTIME_ROOT_ENTRY_NAMES for name in names):
        raise ClaudeWranglerError(
            "The runtime root {} already holds files that are not claude-wrangler's "
            "({}). Choose a new or empty --local-dir.".format(
                root, ", ".join(sorted(names)[:TRACKED_PATHS_SHOWN])
            )
        )


def tracked_state_paths(config: Config) -> List[Path]:
    """Return the managed paths a repository must not track.

    The runtime root, the pinned config dir, the redirected home, the
    generated launchers, and the npm-era legacy directory. A tracked file
    there is one a clone or pull can replace with content claude-wrangler
    never verified.
    """

    layout = config.layout
    paths = [
        layout.local_dir,
        layout.config_dir,
        layout.claude_home_dir,
        layout.launcher_path,
        layout.launcher_cmd_path,
    ]
    if config.legacy is not None:
        paths.append(config.legacy.dir)
    unique: List[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def require_untracked_state(config: Config, services: Services) -> None:
    """Refuse to act on runtime or session state that git tracks.

    A repository that commits `.local/claude/...` (even past the managed
    `.gitignore` block, with `git add -f`) supplies the binary, its
    completion record, the settings, and the metadata pin a later run would
    otherwise trust. `--allow-tracked-state` overrides the refusal for an
    operator who tracks these files deliberately; the paths are still named.
    """

    tracked = services.tracked_files(config.project_root, tracked_state_paths(config))
    if not tracked:
        return
    shown = ", ".join(tracked[:TRACKED_PATHS_SHOWN])
    if len(tracked) > TRACKED_PATHS_SHOWN:
        shown += ", and {} more".format(len(tracked) - TRACKED_PATHS_SHOWN)
    if config.allow_tracked_state:
        eprint(
            "[claude-wrangler] Warning: git tracks managed runtime or session "
            "files ({}); continuing because --allow-tracked-state was given.".format(
                shown
            )
        )
        return
    raise ClaudeWranglerError(
        "Refusing to continue: git tracks managed runtime, session-state, or "
        "launcher files in this project: {}. A clone or pull can replace them "
        "with content claude-wrangler never verified. Remove them from the "
        "repository (for example `git rm -r --cached <path>`) or, if you track "
        "them on purpose, rerun with --allow-tracked-state.".format(shown)
    )


def announce_release_source(config: Config) -> None:
    """Name a non-default release host taken from recorded metadata.

    Printed before any network call, so a host that came from a planted or
    stale metadata file is visible before it is contacted. Signatures still
    bind every byte to Anthropic's compiled-in keys.
    """

    url = config.release_base_url
    if not url or url == DEFAULT_RELEASE_BASE_URL or config.release_base_url_explicit:
        return
    eprint(
        "[claude-wrangler] Note: this project's metadata records the release "
        "host {} instead of the default {}; pass --release-base-url to change "
        "it.".format(url, DEFAULT_RELEASE_BASE_URL)
    )


def check_renderable(config: Config) -> None:
    """Refuse a layout whose generated files cannot be written, before any change.

    The cmd launcher is ASCII-only, so a non-ASCII `--local-dir` (or other
    managed path) would otherwise fail after download and promotion.
    """

    planned_files(config)


def _managed_launcher_exists(config: Config) -> bool:
    """Return True when this checkout already has a claude-wrangler launcher."""

    for path in (config.layout.launcher_path, config.layout.launcher_cmd_path):
        try:
            if is_link_like(path) or not path.is_file():
                continue
            if SCRIPT_MARKER in path.read_bytes().decode("utf-8", "replace"):
                return True
        except OSError:
            continue
    return False


def _launcher_env_factory(
    config: Config, services: Services
) -> Callable[[], Mapping[str, str]]:
    """Return a callable building the project's launcher environment lazily."""

    def build() -> Mapping[str, str]:
        """Build the ring environment the launcher would hand Claude Code."""

        return launcher_session_environment(config, services.environ)

    return build


def _rediscover_legacy(config: Config) -> Optional[LegacyState]:
    """Re-read the legacy layout under the lock (discovery never writes)."""

    candidates = [
        config.legacy.dir_relative if config.legacy is not None else None,
        config.legacy_migrated_from,
        legacy_dir_from_launcher(
            config.layout.launcher_path, config.layout.launch_config_path
        ),
        LEGACY_LOCAL_DIR,
    ]
    return discover_legacy(config.project_root, legacy_dir_candidates(*candidates))


def _legacy_metadata_document(
    config: Config, legacy: LegacyState, generated_at: str
) -> Dict[str, object]:
    """Return updated schema-1 metadata for a migration-pending legacy layout.

    `--update` and the settings toggles persist their results here (the
    format npm-era releases read) until migration writes schema 2.
    """

    payload: Dict[str, object] = dict(legacy.metadata or {})
    payload.update(
        {
            "schema_version": LEGACY_SCHEMA_VERSION,
            "script_name": SCRIPT_NAME,
            "script_version": SCRIPT_VERSION,
            "generated_at": generated_at,
            "project_root": str(config.project_root),
            "claude_selector": config.claude_selector,
            "claude_channel": config.claude_channel,
            "claude_version": config.claude_version,
            "shared_home": config.shared_home,
            "reasonable_permissions_enabled": config.reasonable_permissions_enabled,
            "update_checks": config.update_checks,
            "windows_sandbox_warning": config.windows_sandbox_warning,
            "release_base_url": config.release_base_url,
            "version_source": config.version_source,
            "available_versions": dict(config.available_versions),
            "available_versions_updated_at": config.available_versions_updated_at,
            "paths": {
                "local_dir": legacy.dir_relative,
                "claude_home_dir": config.layout.claude_home_relative,
                "claude_config_dir": config.layout.config_dir_relative,
                "launcher": config.layout.launcher_relative,
                "readme_local": config.layout.readme_relative,
            },
        }
    )
    return payload


def _publish_native(
    config: Config, services: Services, generated_at: Optional[str] = None
) -> Config:
    """Write schema-2 metadata, then every generated file, atomically."""

    stamp = generated_at or format_utc(services.clock())
    write_metadata(
        config.layout.metadata_path,
        build_metadata(config, generated_at=stamp),
        dry_run=config.dry_run,
    )
    write_generated_files(config)
    return config


def _host_key_or_none(config: Config, services: Services) -> Optional[str]:
    """Return the host platform key, warning and returning None when unknown."""

    try:
        return select_platform_key(services, config.platform_override)
    except ClaudeWranglerError as exc:
        eprint("[claude-wrangler] Warning: {}".format(exc))
        return None


def _structural_legacy_binary(
    config: Config, services: Services, legacy: LegacyState
) -> Optional[str]:
    """Return the npm-era binary that passes the structural check, if any.

    Offline operations cannot hash it against a signed manifest, so they use
    it only to keep an existing npm-era checkout's launcher running
    (`legacy_npm_layout_migration.md` requirement 6).
    """

    key = _host_key_or_none(config, services)
    if key is None:
        return None
    return select_legacy_launch_binary(
        config.project_root, legacy, key, services.check_structure
    )


def _verified_legacy_binary(
    config: Config, services: Services, legacy: LegacyState, source: object
) -> Tuple[Optional[str], str]:
    """Return the npm-era binary whose size and SHA-256 match the signed manifest.

    `--update` contacts the release host anyway, so it fetches and verifies
    the manifest for the version the npm-era layout pins and hashes each
    candidate binary against it, as migration's adopt-in-place does; the
    match must also pass the structural check. Returns the repo-relative
    POSIX path, or None and the reason.
    """

    key = _host_key_or_none(config, services)
    if key is None:
        return None, "this host's platform could not be determined"
    if legacy.version is None:
        return None, "the npm-era layout does not record an exact version"
    try:
        check_version_floor(legacy.version)
        bundle = source.fetch_manifest_bundle(legacy.version)
        artifact = artifact_for(bundle, key)
    except ClaudeWranglerError as exc:
        return None, "its signed manifest could not be verified ({})".format(exc)
    adoption = find_adoptable_binary(
        legacy_binary_candidates(legacy.dir, key), artifact.size, artifact.checksum
    )
    for note in adoption.notes:
        eprint("[claude-wrangler] {}".format(note))
    if adoption.source is None:
        return (
            None,
            "no runnable Claude Code binary in {} matches the signed manifest "
            "for {}".format(legacy.dir, legacy.version),
        )
    try:
        services.check_structure(adoption.source, key)
    except (ClaudeWranglerError, OSError) as exc:
        return (
            None,
            "no runnable Claude Code binary in {} passes the structural check "
            "({})".format(legacy.dir, exc),
        )
    try:
        relative = Path(
            os.path.relpath(str(adoption.source), str(config.project_root))
        ).as_posix()
    except ValueError:
        return None, "the npm-era binary is not inside the project"
    return relative, ""


def _publish_legacy_mode(
    config: Config,
    services: Services,
    legacy: LegacyState,
    binary: Optional[str],
    reason: str = "",
) -> bool:
    """Render the Node-free legacy launcher and persist legacy metadata.

    ``binary`` is the repo-relative npm-era binary the launcher runs; the
    caller decides how it was vetted (hashed against the signed manifest by
    `--update`, structurally checked by offline toggles on a checkout that
    already has a claude-wrangler launcher). Returns False, and writes no
    launcher, when ``binary`` is None, so a launcher never points at a
    runtime that does not exist or was not vetted.
    """

    stamp = format_utc(services.clock())
    write_metadata(
        legacy.dir / METADATA_FILENAME,
        _legacy_metadata_document(config, legacy, stamp),
        dry_run=config.dry_run,
    )
    if binary is None:
        eprint(
            "[claude-wrangler] Warning: migration pending and {}; the launcher "
            "was not rewritten. Run `{} .` to install the native runtime.".format(
                reason
                or "no runnable Claude Code binary was found in {}".format(legacy.dir),
                SCRIPT_NAME,
            )
        )
        return False
    write_generated_files(config, LegacyLaunch(binary, legacy.version))
    eprint(
        "[claude-wrangler] Migration pending: the launcher runs {} directly (no "
        "Node.js) until install, upgrade, or repair migrates the project.".format(
            binary
        )
    )
    return True


def _report_cleanup(result, legacy_dir: Path) -> None:
    """Print what a legacy cleanup removed, renamed aside, or left."""

    for name in result.removed:
        eprint("[claude-wrangler] Removed legacy {}".format(legacy_dir / name))
    for name in result.retired:
        eprint(
            "[claude-wrangler] Legacy entry in use; renamed to {} for a later "
            "install, upgrade, or repair to delete.".format(legacy_dir / name)
        )
    for name, reason in result.left:
        eprint(
            "[claude-wrangler] Could not remove legacy {} ({}); a later install, "
            "upgrade, or repair retries.".format(legacy_dir / name, reason)
        )


def _cleanup_runtime(store: RuntimeStore, services: Services) -> None:
    """Delete staging and retired entries nothing runs from (fail closed)."""

    def report(path: Path, exc: BaseException) -> None:
        """Report a cleanup failure without failing the operation."""

        eprint(
            "[claude-wrangler] Could not remove {} ({}); --repair-install retries "
            "once nothing runs from it.".format(path, exc)
        )

    removed = store.cleanup_staging_and_retired(
        in_use_fail_closed(services), on_error=report
    )
    for entry in removed:
        eprint("[claude-wrangler] Cleaned up {}".format(entry))


def _retry_legacy_cleanup(
    config: Config, services: Services, legacy: Optional[LegacyState], stamp: str
) -> None:
    """Finish a deferred legacy cleanup when the native runtime is in charge."""

    if legacy is None or not legacy.cleanup_residue:
        return
    if not legacy.is_projection and not config.dry_run:
        version = config.claude_version or legacy.version or ""
        atomic_write_text(
            legacy.dir / METADATA_FILENAME,
            projection_text(
                build_projection(
                    config,
                    legacy,
                    version=version,
                    channel=config.claude_channel or "stable",
                    generated_at=stamp,
                )
            ),
        )
    result = cleanup_legacy_tree(
        legacy.dir,
        in_use=in_use_fail_closed(services),
        txid=uuid.uuid4().hex[:12],
        dry_run=config.dry_run,
    )
    _report_cleanup(result, legacy.dir)


@dataclass
class InstallOutcome:
    """What an install-like transaction did."""

    config: Config
    summary: str
    interrupted: bool = False
    notes: List[str] = field(default_factory=list)


def _resolve_target(
    config: Config, source_factory: Callable[[], object]
) -> Tuple[str, Optional[object]]:
    """Return the exact target version and the release source if one was built."""

    if config.claude_version and is_exact_version(config.claude_version):
        return config.claude_version, None
    source = source_factory()
    version = source.channel_version(config.claude_channel or "stable")
    eprint(
        "[claude-wrangler] The {} channel currently points at {}".format(
            config.claude_channel, version
        )
    )
    return version, source


def _install_release(
    config: Config,
    services: Services,
    store: RuntimeStore,
    *,
    version: str,
    channel: str,
    source: Optional[object],
    platform_key: str,
    adopt_from: Optional[LegacyState] = None,
    allow_invalid_pointer: bool = False,
    target_slot: Optional[str] = None,
) -> Tuple[str, bool, str]:
    """Download (or adopt), validate, and promote one release.

    Returns `(slot, interrupted, platform_key)`. The active slot is never
    touched; the release goes into the inactive (or given) slot.
    """

    check_version_floor(version)
    if source is None:
        source = services.release_source(
            config.release_base_url, config.download_timeout_seconds, config.ca_bundle
        )
    bundle = source.fetch_manifest_bundle(version)
    key = select_platform_key(
        services, config.platform_override, bundle.verified.artifacts.keys()
    )
    artifact_for(bundle, key)
    active = read_pointer_quietly(store)
    if target_slot is None:
        target_slot = other_slot(active) if active is not None else SLOT_NAMES[0]
    if target_slot == active:
        raise ClaudeWranglerError(
            "Refusing to prepare slot {} because it is active.".format(target_slot)
        )
    prepared = prepare_candidate(
        config,
        services,
        store,
        source=source,
        bundle=bundle,
        platform_key=key,
        slot=target_slot,
        channel=channel,
        adopt_candidates=(
            legacy_binary_candidates(adopt_from.dir, key)
            if adopt_from is not None
            else ()
        ),
        carry_from_slots=[name for name in (active, target_slot) if name is not None],
    )
    outcome = promote_candidate(
        config,
        services,
        store,
        prepared,
        active_slot=active,
        launcher_env=_launcher_env_factory(config, services),
        allow_invalid_pointer=allow_invalid_pointer,
    )
    eprint(
        "[claude-wrangler] Slot {} now holds Claude Code {} and is active".format(
            outcome.slot, version
        )
    )
    return outcome.slot, outcome.interrupted, key


def _migrate(
    config: Config,
    services: Services,
    store: RuntimeStore,
    legacy: LegacyState,
) -> InstallOutcome:
    """Move an npm-era layout onto the native runtime (migration spec 5).

    Order: discover (done), prepare and validate the candidate (adopted or
    downloaded), promote it, write schema-2 metadata (the commit point),
    write the launcher, shims, and README, write the schema-1 projection,
    then clean the npm tree. Before the metadata write the legacy launcher
    and npm tree are untouched and keep working.
    """

    # An explicit target (--upgrade, or --channel/--version on install) wins;
    # otherwise migration keeps the version the npm-era install pinned.
    explicit = config.operation == "upgrade" or (
        config.channel_explicit or config.version_explicit
    )
    if explicit:
        version = config.claude_version
        channel = config.claude_channel or "stable"
    else:
        version = legacy.version
        channel = legacy.channel or config.claude_channel or "stable"
        if legacy.channel_deprecated:
            eprint(channel_deprecation_message("next"))
    eprint(
        "[claude-wrangler] Migrating the npm-era layout at {} to the native "
        "runtime".format(legacy.dir)
    )
    source: Optional[object] = None
    if version is None:
        if not explicit:
            raise ClaudeWranglerError(
                "Cannot tell which Claude Code version the npm-era layout in {} "
                "pins. Run `{} --upgrade --channel <stable|latest> .` to choose "
                "one explicitly.".format(legacy.dir, SCRIPT_NAME)
            )
        version, source = _resolve_target(
            config,
            lambda: services.release_source(
                config.release_base_url,
                config.download_timeout_seconds,
                config.ca_bundle,
            ),
        )
    try:
        check_version_floor(version)
    except DistributionError as exc:
        raise ClaudeWranglerError(
            "The npm-era install pins Claude Code {}, older than {}, the oldest "
            "native release claude-wrangler installs. It will not upgrade "
            "silently: run `{} --upgrade --channel {} .` to move to a supported "
            "release explicitly.".format(version, VERSION_FLOOR, SCRIPT_NAME, channel)
        ) from exc

    host_key = select_platform_key(services, config.platform_override)
    active = active_slot_for(store, host_key)
    interrupted = False
    key = host_key
    active_record = store.slot_record(active) if active is not None else None
    reuse = False
    if active is not None and active_record is not None:
        if active_record.version == version:
            # A slot a repository supplied is never trusted on its record
            # alone: reuse only what verifies fully offline.
            verification = verify_and_stamp(store, active, services, host_key)
            reuse = verification.ok
            if not reuse:
                eprint(
                    "[claude-wrangler] Slot {} records Claude Code {} but failed "
                    "full offline verification ({}); not reusing it".format(
                        active, version, verification.problem
                    )
                )
    if reuse:
        eprint(
            "[claude-wrangler] Slot {} already holds verified Claude Code {}; "
            "reusing it".format(active, version)
        )
    else:
        _slot, interrupted, key = _install_release(
            config,
            services,
            store,
            version=version,
            channel=channel,
            source=source,
            platform_key=host_key,
            adopt_from=legacy,
        )

    stamp = format_utc(services.clock())
    migrated = replace(
        config,
        claude_version=version,
        claude_channel=channel,
        claude_selector=(
            config.claude_selector if explicit else (legacy.selector or version)
        ),
        version_source=(
            config.version_source
            if explicit
            else "migrated from the npm-era layout ({})".format(
                legacy.version_source or "legacy"
            )
        ),
        platform_key=key,
        legacy_migrated_from=legacy.dir_relative,
        legacy_migrated_at=stamp,
    )
    if migrated.claude_selector not in ("latest",) and not is_exact_version(
        migrated.claude_selector
    ):
        migrated = replace(migrated, claude_selector=version)
    _publish_native(migrated, services, generated_at=stamp)
    atomic_write_text(
        legacy.dir / METADATA_FILENAME,
        projection_text(
            build_projection(
                migrated, legacy, version=version, channel=channel, generated_at=stamp
            )
        ),
    )
    eprint(
        "[claude-wrangler] Wrote the schema-1 compatibility projection {}".format(
            legacy.dir / METADATA_FILENAME
        )
    )
    result = cleanup_legacy_tree(
        legacy.dir,
        in_use=in_use_fail_closed(services),
        txid=uuid.uuid4().hex[:12],
    )
    _report_cleanup(result, legacy.dir)
    return InstallOutcome(
        migrated,
        "migrated from {} (Claude Code {})".format(legacy.dir_relative, version),
        interrupted,
    )


@dataclass
class RepairReport:
    """What `--repair-install` found and changed before any download."""

    cleaned: List[str] = field(default_factory=list)
    verified: Dict[str, Dict[str, object]] = field(default_factory=dict)
    pointer_before: Optional[str] = None
    pointer_after: Optional[str] = None
    fallback: Optional[Dict[str, object]] = None
    notes: List[str] = field(default_factory=list)


def _read_fallback(config: Config) -> Optional[Dict[str, object]]:
    """Return the launcher's fallback.json record, if any."""

    path = config.layout.fallback_path
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _repair_runtime(
    config: Config, services: Services, store: RuntimeStore, host_key: str
) -> Tuple[RepairReport, Optional[str]]:
    """Run repair requirement 10 up to (not including) a re-download.

    Clears `.staging/`, deletes `.retired/` entries no longer in use,
    re-verifies both slots offline, and repairs the pointer by choosing the
    verified slot whose version matches metadata. When the active slot is
    corrupt but the other slot verifies, the pointer moves to it first so
    the corrupt slot becomes the inactive one a re-download replaces.
    Each slot that verifies gets a fresh verification stamp (so a moved or
    restored checkout launches again without a download), and each slot
    that fails loses its stamp, so no launcher falls back to it.
    Returns the report and the slot that should be active, if any.
    """

    report = RepairReport()
    report.fallback = _read_fallback(config)
    if report.fallback is not None:
        eprint(
            "[claude-wrangler] The launcher recorded a fallback: {}".format(
                json.dumps(report.fallback, sort_keys=True)
            )
        )
    if store.root.is_dir():
        _cleanup_runtime(store, services)
    verifications = {
        name: verify_and_stamp(store, name, services, host_key, dry_run=config.dry_run)
        for name in SLOT_NAMES
    }
    for name, result in verifications.items():
        report.verified[name] = result.as_dict()
        if result.present and not result.ok:
            eprint("[claude-wrangler] Repair: {}".format(result.problem))
    pointer = read_pointer_quietly(store)
    report.pointer_before = pointer
    pinned = config.claude_version
    good = [name for name in SLOT_NAMES if verifications[name].ok]
    chosen: Optional[str] = None
    if pointer is not None and verifications[pointer].ok:
        chosen = pointer
    else:
        matching = [name for name in good if verifications[name].version == pinned]
        if matching:
            chosen = matching[0]
        elif good:
            chosen = good[0]
        if chosen is not None and not config.dry_run:
            store.set_active(chosen)
            eprint(
                "[claude-wrangler] Repaired the active-slot pointer: slot {} "
                "(Claude Code {})".format(chosen, verifications[chosen].version)
            )
            if pinned is not None and verifications[chosen].version != pinned:
                # The launcher's own fallback already ran this slot; say so,
                # because a failed re-download leaves it active.
                eprint(
                    "[claude-wrangler] Repair: slot {} runs Claude Code {} until "
                    "the pinned {} is re-downloaded into slot {}; if that "
                    "download fails, the project keeps running {} and a later "
                    "--repair-install retries.".format(
                        chosen,
                        verifications[chosen].version,
                        pinned,
                        other_slot(chosen),
                        verifications[chosen].version,
                    )
                )
    report.pointer_after = chosen if chosen is not None else pointer
    return report, chosen


def _needs_release(
    store: RuntimeStore, active: Optional[str], version: str, key: str
) -> bool:
    """Return True unless the active slot already holds ``version`` for ``key``."""

    if active is None:
        return True
    record = store.slot_record(active)
    if record is None or record.version != version:
        return True
    return store.slot_problem(active, key) is not None


def _activate_verified_slot(
    config: Config,
    services: Services,
    store: RuntimeStore,
    *,
    version: str,
    host_key: str,
    slot: Optional[str],
) -> bool:
    """Make ``slot`` active when it already holds a verified ``version``.

    A release the inactive slot already holds (for example after an
    interrupted upgrade, or a plain install returning to the pin) is
    re-verified offline and activated by a pointer flip, which displaces
    nothing, instead of being downloaded again. Returns True when the
    pointer moved.
    """

    pointer = read_pointer_quietly(store)
    if slot is None or slot == pointer:
        return False
    record = store.slot_record(slot)
    if record is None or record.version != version:
        return False
    if host_key not in record.platforms:
        return False
    verification = verify_and_stamp(store, slot, services, host_key)
    if not verification.ok:
        eprint(
            "[claude-wrangler] Slot {} records Claude Code {} but failed full "
            "offline verification ({}); downloading instead".format(
                slot, version, verification.problem
            )
        )
        return False
    store.set_active(slot)
    eprint(
        "[claude-wrangler] Slot {} already holds verified Claude Code {}; made it "
        "active without downloading".format(slot, version)
    )
    return True


def _install_locked(
    config: Config, services: Services, store: RuntimeStore
) -> InstallOutcome:
    """Install, upgrade, or repair with the maintenance lock held.

    A plain install keeps the pinned version (downloading it only when no
    verified slot holds it); `--channel`, `--version`, and `--upgrade`
    choose a new target. A new release always goes into the inactive slot.
    The active slot is reused only after a full offline verification, so a
    slot a repository supplied is never trusted on its record alone.
    """

    store.validate()
    legacy = _rediscover_legacy(config)
    host_key = select_platform_key(services, config.platform_override)
    native_generated_at = (
        config.existing_metadata.get("generated_at")
        if isinstance(config.existing_metadata, dict)
        else None
    )
    pointer_before = read_pointer_quietly(store)

    repair: Optional[RepairReport] = None
    verified_active: Optional[str] = None
    if config.repair_install:
        repair, verified_active = _repair_runtime(config, services, store, host_key)
    native_complete = native_runtime_complete(store, host_key)
    if legacy_is_authoritative(legacy, native_complete, native_generated_at):
        return _migrate(config, services, store, legacy)

    active_verified = False
    if config.repair_install:
        # _repair_runtime verified and stamped it already.
        active = verified_active
        active_verified = active is not None
    else:
        try:
            active = store.active_slot()
        except PointerError as exc:
            raise ClaudeWranglerError(
                "{} Run `{} --repair-install .`.".format(exc, SCRIPT_NAME)
            ) from exc
        if active is not None:
            # Integrity only: a slot that merely lacks this host's platform
            # is intact, and _needs_release below adds the platform.
            verification = verify_and_stamp(store, active, services)
            active_verified = verification.ok
            if not active_verified:
                eprint(
                    "[claude-wrangler] Warning: the active slot {} failed full "
                    "offline verification ({}); it will not be reused or run. A "
                    "verified release goes into the other slot.".format(
                        active, verification.problem
                    )
                )

    working = config
    keeps_pin = config.operation == "install" and not (
        config.channel_explicit or config.version_explicit
    )
    if keeps_pin and not config.claude_version and active_verified:
        # No recorded pin (for example, lost metadata): keep what is active,
        # but only a version a verified slot holds.
        record = store.slot_record(active)
        if record is not None:
            working = replace(working, claude_version=record.version)
    if keeps_pin and working.claude_version:
        eprint(
            "[claude-wrangler] Keeping the pinned Claude Code {} (version source: "
            "{})".format(working.claude_version, working.version_source)
        )

    version, source = _resolve_target(
        working,
        lambda: services.release_source(
            config.release_base_url, config.download_timeout_seconds, config.ca_bundle
        ),
    )
    channel = working.claude_channel or "stable"
    interrupted = False
    key = host_key
    if not active_verified or _needs_release(store, active, version, host_key):
        target_slot: Optional[str] = None
        pointer = read_pointer_quietly(store)
        if config.repair_install:
            target_slot = other_slot(pointer) if pointer is not None else SLOT_NAMES[0]
        if _activate_verified_slot(
            working,
            services,
            store,
            version=version,
            host_key=host_key,
            slot=(
                target_slot
                if target_slot is not None
                else (other_slot(pointer) if pointer is not None else None)
            ),
        ):
            summary = (
                "activated the verified Claude Code {} already installed for {}".format(
                    version, key
                )
            )
        else:
            _slot, interrupted, key = _install_release(
                working,
                services,
                store,
                version=version,
                channel=channel,
                source=source,
                platform_key=host_key,
                allow_invalid_pointer=config.repair_install,
                target_slot=target_slot,
            )
            summary = "installed Claude Code {} for {}".format(version, key)
    else:
        summary = "Claude Code {} already installed for {} (slot {})".format(
            version, key, active
        )
        eprint("[claude-wrangler] {}".format(summary))

    final = replace(
        working,
        claude_version=version,
        claude_channel=channel,
        platform_key=key,
    )
    stamp = format_utc(services.clock())
    try:
        _publish_native(final, services, generated_at=stamp)
    except (OSError, ClaudeWranglerError) as exc:
        pointer_now = read_pointer_quietly(store)
        if pointer_now is None or pointer_now == pointer_before:
            raise
        # The pointer flip already committed the new runtime; say how to
        # finish rather than leaving the operator to guess.
        raise ClaudeWranglerError(
            "Slot {} (Claude Code {}) is now active, but publishing the metadata "
            "and launchers failed: {}. Rerun the same command to finish "
            "publishing; a plain `{} .` instead returns to the version the "
            "metadata still pins.".format(pointer_now, version, exc, SCRIPT_NAME)
        ) from exc
    _retry_legacy_cleanup(final, services, legacy, stamp)
    _cleanup_runtime(store, services)
    notes: List[str] = []
    if repair is not None:
        if config.layout.fallback_path.exists():
            try:
                config.layout.fallback_path.unlink()
                notes.append("cleared the resolved launcher fallback record")
            except OSError as exc:
                notes.append("could not clear fallback.json: {}".format(exc))
        notes.append(
            "repair: active-slot pointer {} -> {}".format(
                repair.pointer_before, read_pointer_quietly(store)
            )
        )
    return InstallOutcome(final, summary, interrupted, notes)


def _print_plan(config: Config, services: Services) -> int:
    """Describe what an install-like operation would do (no network, no writes)."""

    store = runtime_store(config)
    host_key: Optional[str] = None
    try:
        host_key = select_platform_key(services, config.platform_override)
    except ClaudeWranglerError as exc:
        eprint("[claude-wrangler] Would fail: {}".format(exc))
    native_complete = host_key is not None and native_runtime_complete(store, host_key)
    generated_at = (
        config.existing_metadata.get("generated_at")
        if isinstance(config.existing_metadata, dict)
        else None
    )
    legacy = config.legacy
    eprint("[claude-wrangler] Dry run: nothing is downloaded, promoted, or written")
    if config.reconfigure_only or config.skip_install:
        eprint("[claude-wrangler] Would only rewrite managed files from stored state")
    elif legacy is not None and legacy_is_authoritative(
        legacy, native_complete, generated_at
    ):
        eprint(
            "[claude-wrangler] Would migrate the npm-era layout at {} (pinned "
            "version {})".format(legacy.dir, legacy.version or "unknown")
        )
    else:
        if config.repair_install:
            eprint(
                "[claude-wrangler] Would clear staging, delete idle retired slots, "
                "re-verify both slots offline, and repair the pointer"
            )
        active = active_slot_for(store, host_key) if host_key else None
        record = store.slot_record(active) if active is not None else None
        if (
            record is not None
            and config.claude_version is not None
            and record.version == config.claude_version
        ):
            eprint(
                "[claude-wrangler] Claude Code {} is already installed (slot {})".format(
                    record.version, active
                )
            )
        else:
            eprint(
                "[claude-wrangler] Would install Claude Code {} ({} channel) for {} "
                "into the inactive slot".format(
                    config.claude_version
                    or "the current {} release".format(config.claude_channel),
                    config.claude_channel,
                    host_key or "this host",
                )
            )
    write_generated_files(config)
    return 0


def _reconfigure(
    config: Config, services: Services, store: RuntimeStore
) -> InstallOutcome:
    """Rewrite managed files from stored state (toggles and --skip-install).

    Nothing is downloaded. The active slot is re-verified offline (and
    re-stamped) before a launcher for it is written. In a migration-pending
    checkout the npm-era binary cannot be hashed offline, so its launcher is
    rendered only where a claude-wrangler launcher already exists: legacy
    mode keeps an existing npm-era checkout running and is never offered to
    a fresh clone.
    """

    store.validate()
    legacy = _rediscover_legacy(config)
    host_key = None
    try:
        host_key = select_platform_key(services, config.platform_override)
    except ClaudeWranglerError:
        host_key = None
    native_complete = native_runtime_complete(store, host_key)
    generated_at = (
        config.existing_metadata.get("generated_at")
        if isinstance(config.existing_metadata, dict)
        else None
    )
    if legacy_is_authoritative(legacy, native_complete, generated_at):
        if not _managed_launcher_exists(config):
            raise ClaudeWranglerError(
                "Migration pending: {} holds an npm-era layout but this checkout "
                "has no claude-wrangler launcher yet, so no launcher is rendered "
                "for its unverified binary. Run `{} .` to migrate; it hashes the "
                "binary against the signed manifest or downloads a verified "
                "one.".format(legacy.dir, SCRIPT_NAME)
            )
        _publish_legacy_mode(
            config,
            services,
            legacy,
            _structural_legacy_binary(config, services, legacy),
        )
        return InstallOutcome(config, "migration pending; managed files rewritten")
    working = config
    active = active_slot_for(store, host_key) if host_key else None
    if active is not None:
        verification = verify_and_stamp(store, active, services, host_key)
        if not verification.ok:
            raise ClaudeWranglerError(
                "The active runtime slot {} failed full offline verification ({}); "
                "no managed file was rewritten, and its verification stamp was "
                "removed so no launcher runs it. Run `{} --repair-install .`.".format(
                    active, verification.problem, SCRIPT_NAME
                )
            )
        record = store.slot_record(active)
        if record is not None:
            working = replace(
                working, claude_version=record.version, platform_key=host_key
            )
    _publish_native(working, services)
    return InstallOutcome(working, "managed files rewritten; runtime unchanged")


def install_like_operation(config: Config, services: Optional[Services] = None) -> int:
    """Install, upgrade, or repair the native runtime, or rewrite managed files."""

    services = services or default_services()
    eprint("[claude-wrangler] Target project root: {}".format(config.project_root))
    eprint("[claude-wrangler] Operation: {}".format(config.operation))
    check_renderable(config)
    require_untracked_state(config, services)
    announce_release_source(config)
    if config.dry_run:
        ensure_gitignore_block(config)
        return _print_plan(config, services)
    check_config_settings(config.layout.config_settings_path)
    check_runtime_placement(config, services.environ)
    if config.platform_override is not None:
        # Refuse another operating system's build before creating anything.
        select_platform_key(services, config.platform_override)
    store = runtime_store(config)
    with store.lock():
        require_unchanged_state(config)
        ensure_gitignore_block(config)
        if config.reconfigure_only or config.skip_install:
            outcome = _reconfigure(config, services, store)
        else:
            outcome = _install_locked(config, services, store)
    print(build_install_summary(outcome.config, outcome.summary))
    for note in outcome.notes:
        print(note)
    if outcome.interrupted:
        raise KeyboardInterrupt
    return 0


def update_operation(config: Config, services: Optional[Services] = None) -> int:
    """Refresh the known channel versions from signed manifests; never upgrade."""

    services = services or default_services()
    if config.existing_metadata is None and config.legacy is None:
        raise ClaudeWranglerError(
            "No managed Claude install was found under {}. Run `{} .` first, then "
            "rerun `{} --update`.".format(config.project_root, SCRIPT_NAME, SCRIPT_NAME)
        )
    eprint("[claude-wrangler] Target project root: {}".format(config.project_root))
    eprint("[claude-wrangler] Operation: update")
    check_renderable(config)
    require_untracked_state(config, services)
    announce_release_source(config)
    eprint("[claude-wrangler] Refreshing stable and latest from signed manifests")
    if config.dry_run:
        ensure_gitignore_block(config)
        eprint("[claude-wrangler] Dry run: the release server is not contacted")
        return 0
    check_config_settings(config.layout.config_settings_path)
    check_runtime_placement(config, services.environ)
    store = runtime_store(config)
    with store.lock():
        require_unchanged_state(config)
        ensure_gitignore_block(config)
        legacy = _rediscover_legacy(config)
        host_key = None
        try:
            host_key = select_platform_key(services, config.platform_override)
        except ClaudeWranglerError:
            host_key = None
        generated_at = (
            config.existing_metadata.get("generated_at")
            if isinstance(config.existing_metadata, dict)
            else None
        )
        legacy_mode = legacy_is_authoritative(
            legacy, native_runtime_complete(store, host_key), generated_at
        )
        active = (
            None if legacy_mode or not host_key else active_slot_for(store, host_key)
        )
        if active is not None:
            # Verified before anything is written, so a launcher is only
            # ever republished for a slot that verifies.
            verification = verify_and_stamp(store, active, services, host_key)
            if not verification.ok:
                raise ClaudeWranglerError(
                    "The active runtime slot {} failed full offline verification "
                    "({}); nothing was refreshed, and its verification stamp was "
                    "removed so no launcher runs it. Run `{} --repair-install "
                    ".`.".format(active, verification.problem, SCRIPT_NAME)
                )
        source = services.release_source(
            config.release_base_url, config.download_timeout_seconds, config.ca_bundle
        )
        results = refresh_catalog(source)
        checked_at = format_utc(services.clock())
        updated = apply_catalog(config, results, checked_at)
        previous = None
        try:
            previous = json.loads(
                config.layout.update_check_path.read_bytes().decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, ValueError):
            previous = None
        cache = update_check_cache(
            updated,
            results,
            checked_at,
            previous if isinstance(previous, dict) else None,
        )
        atomic_write_text(
            config.layout.update_check_path,
            json.dumps(cache, indent=2, sort_keys=True) + "\n",
        )
        if legacy_mode and legacy is not None:
            # The npm-era binary runs only after it hashes to the signed
            # manifest for the version the layout pins.
            binary, reason = _verified_legacy_binary(updated, services, legacy, source)
            _publish_legacy_mode(updated, services, legacy, binary, reason)
        else:
            if active is not None:
                record = store.slot_record(active)
                if record is not None:
                    updated = replace(updated, claude_version=record.version)
            _publish_native(updated, services)

    print(
        "NOTE: This command updates available version information, but does not "
        'perform an upgrade; use "claude-wrangler --upgrade --channel '
        '<stable|latest> [--version <latest|x.y.z>]" for that.'
    )
    print("")
    print(
        build_available_versions_table(
            updated.available_versions, updated.available_version_details
        )
    )
    print("")
    print("Checked at: {}".format(checked_at))
    errors = {
        channel: result.error for channel, result in results.items() if not result.ok
    }
    for channel, error in sorted(errors.items()):
        print("ERROR: {} channel: {}".format(channel, error))
    note = newer_release_note(updated)
    if note:
        print(note)
    return 1 if errors else 0


def rollback_operation(config: Config, services: Optional[Services] = None) -> int:
    """Make the other slot active after an in-use check and offline re-verification."""

    services = services or default_services()
    eprint("[claude-wrangler] Target project root: {}".format(config.project_root))
    eprint("[claude-wrangler] Operation: rollback")
    store = runtime_store(config)
    if not store.root.is_dir():
        raise ClaudeWranglerError(
            "No native runtime exists under {}; nothing to roll back.".format(
                store.root
            )
        )
    check_renderable(config)
    require_untracked_state(config, services)
    check_config_settings(config.layout.config_settings_path)
    host_key = select_platform_key(services, config.platform_override)
    with store.lock():
        require_unchanged_state(config)
        ensure_gitignore_block(config)
        store.validate()
        try:
            active = store.active_slot()
        except PointerError as exc:
            raise ClaudeWranglerError(
                "{} Run `{} --repair-install .` instead of rolling back.".format(
                    exc, SCRIPT_NAME
                )
            ) from exc
        if active is None:
            raise ClaudeWranglerError(
                "No active runtime slot exists; nothing to roll back."
            )
        target = other_slot(active)
        record = store.slot_record(target)
        problem = store.slot_problem(target, host_key)
        if record is None or problem is not None:
            raise ClaudeWranglerError(
                "There is no complete rollback slot ({}).".format(
                    problem or "slot {} is empty".format(target)
                )
            )
        require_idle(
            config,
            services,
            store.slot_dir(target),
            purpose="Refusing to roll back to slot {}".format(target),
            stop_binaries=slot_binaries(store, active) + slot_binaries(store, target),
            launcher_env=_launcher_env_factory(config, services),
        )
        verification = verify_and_stamp(
            store, target, services, host_key, dry_run=config.dry_run
        )
        if not verification.ok:
            raise ClaudeWranglerError(
                "Refusing to roll back: {}".format(verification.problem)
            )
        if config.dry_run:
            eprint(
                "[claude-wrangler] Would make slot {} (Claude Code {}) active".format(
                    target, record.version
                )
            )
            return 0
        store.set_active(target)
        channel = (
            record.channel
            if record.channel in ("stable", "latest")
            else (config.claude_channel)
        )
        rolled = replace(
            config,
            claude_version=record.version,
            claude_selector=record.version,
            claude_channel=channel,
            version_source="rollback to slot {}".format(target),
            platform_key=host_key,
            launcher_python=config.launcher_python,
        )
        _publish_native(rolled, services)
    print(
        "Rolled back: slot {} (Claude Code {}) is active; slot {} is kept as the "
        "rollback target.".format(target, record.version, active)
    )
    return 0


def _managed_residue(config: Config, legacy: Optional[LegacyState]) -> List[str]:
    """Return managed paths that still exist after an uninstall attempt."""

    layout = config.layout
    candidates = [
        layout.local_dir,
        layout.launcher_path,
        layout.launcher_cmd_path,
        layout.readme_path,
        layout.config_dir,
    ]
    if not config.shared_home:
        candidates.append(layout.claude_home_dir)
    if legacy is not None:
        candidates.append(legacy.dir)
    return [str(path) for path in candidates if os.path.lexists(str(path))]


def _outermost_missing_dir(path: Path, stop_at: Path) -> Optional[Path]:
    """Return the outermost missing directory from ``stop_at`` down to ``path``."""

    missing: Optional[Path] = None
    current = path
    while current != stop_at and current != current.parent:
        if os.path.lexists(str(current)):
            break
        missing = current
        current = current.parent
    return missing


def _discard_lock_scaffolding(store: RuntimeStore, created: Optional[Path]) -> None:
    """Remove the empty directories that only the uninstall's lock created.

    Taking the lock creates the runtime root when it is absent (an npm-era
    layout has none). When uninstall then stops, that empty scaffolding is
    removed again so a refused uninstall leaves nothing behind. The lock
    file itself is never deleted here: the caller deletes its own lock
    through `MaintenanceLock.release(delete=True)` while it still owns it,
    so a lock another maintainer holds is never removed. A directory that
    is not empty (another maintainer's lock or staging) stops the cleanup.
    """

    if created is None:
        return
    try:
        current = store.root
        while True:
            current.rmdir()
            if current == created:
                break
            current = current.parent
    except OSError:
        return


def _uninstall_preflight(
    config: Config, services: Services, store: RuntimeStore
) -> List[Path]:
    """Prove ownership and that nothing runs; return the roots to remove.

    A file claude-wrangler itself rendered for a migration-pending layout
    (the README carrying the migration note) proves ownership as well as
    the native rendering does.
    """

    status = generated_file_status(config, strict=False)
    alternatives = legacy_pending_alternatives(config)
    for item in planned_files(config, strict=False):
        if item.owned_by_runtime or not item.path.exists():
            continue
        if status[item.key]["fresh"]:
            continue
        content = item.path.read_bytes().decode("utf-8", "replace")
        accepted = [item.data] + (
            [alternatives[item.key]] if item.key in alternatives else []
        )
        if any(
            normalize_newlines(content, "\n")
            == normalize_newlines(expected.decode("utf-8"), "\n")
            for expected in accepted
        ):
            continue
        marked = any(marker in content for marker in item.markers)
        if not (marked and config.force):
            raise ClaudeWranglerError(
                "Refusing to uninstall: {} does not match the expected generated "
                "content. Use --force to override.".format(item.path)
            )

    legacy = config.legacy
    active = read_pointer_quietly(store) if store.root.is_dir() else None
    stop_binaries = slot_binaries(store, active) + [
        binary for name in SLOT_NAMES for binary in slot_binaries(store, name)
    ]
    roots = [path for path in (store.root,) if path.is_dir()]
    if legacy is not None and legacy.dir.is_dir():
        roots.append(legacy.dir)
    for root in roots:
        require_idle(
            config,
            services,
            root,
            purpose="Refusing to uninstall",
            stop_binaries=stop_binaries,
            launcher_env=_launcher_env_factory(config, services),
        )
    return roots


def _runtime_removal_order(entry: Path) -> Tuple[int, str]:
    """Sort key for uninstall: binaries first, the ownership proof last.

    Staging, retired, and slot trees hold the binaries most likely to be
    locked, so they go first; while they cannot be deleted the launcher
    core and the metadata survive, the project still launches, and a rerun
    of `--uninstall` still proves ownership. The metadata goes last.
    """

    if entry.name in (STAGING_DIR_NAME, RETIRED_DIR_NAME):
        return 0, entry.name
    if entry.name == SLOTS_DIR_NAME:
        return 1, entry.name
    if entry.name == METADATA_FILENAME:
        return 3, entry.name
    return 2, entry.name


def _remove_legacy_tree(directory: Path) -> None:
    """Remove an npm-era directory, keeping its ownership proof until last.

    The npm tree goes first, then `package.json`, then the schema-1
    metadata, so a partial failure still leaves what a rerun of
    `--uninstall` needs to recognize the directory as claude-wrangler's.
    """

    if is_link_like(directory):
        remove_runtime_tree(directory)
        return

    def order(entry: Path) -> Tuple[int, str]:
        """Sort the ownership evidence after everything else."""

        if entry.name == METADATA_FILENAME:
            return 2, entry.name
        if entry.name == "package.json":
            return 1, entry.name
        return 0, entry.name

    for entry in sorted(directory.iterdir(), key=order):
        remove_runtime_tree(entry)
    retry_on_windows_lock(lambda: os.rmdir(str(directory)))


def _uninstall_locked(config: Config, store: RuntimeStore) -> int:
    """Remove the runtime, legacy tree, generated files, and session state.

    Runs with the maintenance lock held, so everything goes except the lock
    file and the directories that lead to it. Binaries go first and the
    metadata (the ownership proof a rerun needs) last. If a binary cannot be
    deleted the operation stops before session state (requirement 12).
    """

    layout = config.layout
    legacy = config.legacy
    metadata: Optional[Path] = None
    try:
        if store.root.is_dir() and not is_link_like(store.root):
            for entry in sorted(store.root.iterdir(), key=_runtime_removal_order):
                if entry.name == LOCK_NAME:
                    continue
                if entry.name == METADATA_FILENAME:
                    metadata = entry
                    continue
                remove_runtime_tree(entry)
        if legacy is not None and legacy.dir.is_dir():
            _remove_legacy_tree(legacy.dir)
            eprint("[claude-wrangler] Removed legacy npm layout {}".format(legacy.dir))
        if metadata is not None:
            remove_runtime_tree(metadata)
    except OSError as exc:
        eprint(
            "[claude-wrangler] ERROR: could not delete part of the runtime ({}). "
            "Stopping before session state; the .gitignore block and the "
            "ownership metadata are kept, so rerunning --uninstall finishes the "
            "removal once nothing holds the file (close any running Claude "
            "Code first).".format(exc)
        )
        return 1

    try:
        remove_generated_files(config, strict=False)
        if not config.shared_home:
            remove_tree(
                layout.claude_home_dir,
                label="managed Claude home directory",
                project_root=config.project_root,
                dry_run=False,
            )
        remove_tree_except(
            layout.config_dir,
            store.root,
            label="managed Claude config directory",
            project_root=config.project_root,
        )
    except OSError as exc:
        eprint(
            "[claude-wrangler] ERROR: could not remove managed files ({}). The "
            ".gitignore block is kept; rerun --uninstall once nothing uses "
            "them.".format(exc)
        )
        return 1
    return 0


def _remove_emptied_runtime_root(store: RuntimeStore) -> Optional[str]:
    """Remove the runtime root once only its (deleted) lock remained.

    Returns a problem description when the root still holds anything: then
    another maintainer created its own lock or staging after this uninstall
    released the lock, and nothing of theirs is touched. A lock file left
    behind (Windows refused to delete it) is deleted only after taking the
    lock once more, so a lock another run holds is never removed.
    """

    root = store.root
    if not root.is_dir() or is_link_like(root):
        return None
    names = sorted(os.listdir(str(root)))
    if names == [LOCK_NAME]:
        probe = store.lock()
        try:
            probe.acquire()
        except MaintenanceLockError:
            return "another claude-wrangler run holds the maintenance lock {}".format(
                root / LOCK_NAME
            )
        probe.release(delete=True)
        names = sorted(os.listdir(str(root)))
    if names:
        return "{} still holds {}; another claude-wrangler run may have started".format(
            root, ", ".join(names)
        )
    retry_on_windows_lock(lambda: os.rmdir(str(root)))
    eprint("[claude-wrangler] Removed runtime root {}".format(root))
    return None


def _uninstall_finish(config: Config, store: RuntimeStore) -> int:
    """After the lock file is deleted: remove the runtime root, then empty dirs."""

    layout = config.layout
    try:
        problem = _remove_emptied_runtime_root(store)
        if problem is not None:
            eprint(
                "[claude-wrangler] ERROR: {}. The .gitignore block is kept; rerun "
                "--uninstall once that run has finished.".format(problem)
            )
            return 1
        remove_tree(
            layout.config_dir,
            label="managed Claude config directory",
            project_root=config.project_root,
            dry_run=False,
        )
        maybe_remove_empty_parent(
            layout.config_dir, stop_at=config.project_root, dry_run=False
        )
        maybe_remove_empty_parent(
            layout.launcher_path, stop_at=config.project_root, dry_run=False
        )
    except OSError as exc:
        eprint(
            "[claude-wrangler] ERROR: could not remove the emptied runtime root "
            "({}). The .gitignore block is kept; rerun --uninstall.".format(exc)
        )
        return 1
    residue = _managed_residue(config, config.legacy)
    if residue:
        eprint(
            "[claude-wrangler] Keeping the .gitignore block because managed files "
            "remain: {}".format(", ".join(residue))
        )
        return 1
    remove_gitignore_block(layout.gitignore_path, dry_run=False)
    print("Uninstall complete for {}.".format(config.project_root))
    return 0


def uninstall_operation(config: Config, services: Optional[Services] = None) -> int:
    """Conservatively remove the managed installation (ab_runtime_slots.md 12).

    The maintenance lock is taken first and held through the ownership and
    in-use checks and every removal; the lock file itself goes last, after
    release (requirement 4). Nothing is removed until no process runs from
    the runtime root or the legacy tree (the background service is stopped
    only with `--stop-background-sessions`). Then the runtime goes first,
    generated files next, and session state last; if a binary cannot be
    deleted the operation stops before session state and keeps the
    `.gitignore` block.
    """

    services = services or default_services()
    layout = config.layout
    store = runtime_store(config)
    if config.existing_metadata is None and config.legacy is None and not config.force:
        raise ClaudeWranglerError(
            "Managed metadata was not found at {}. Refusing to uninstall without "
            "--force because ownership cannot be proven.".format(layout.metadata_path)
        )

    if config.dry_run:
        roots = _uninstall_preflight(config, services, store)
        for root in roots:
            eprint("[claude-wrangler] Would remove {}".format(root))
        remove_generated_files(config, strict=False)
        if not config.shared_home:
            remove_tree(
                layout.claude_home_dir,
                label="managed Claude home directory",
                project_root=config.project_root,
                dry_run=True,
            )
        remove_tree(
            layout.config_dir,
            label="managed Claude config directory",
            project_root=config.project_root,
            dry_run=True,
        )
        remove_gitignore_block(layout.gitignore_path, dry_run=True)
        print("Uninstall complete for {} (dry-run).".format(config.project_root))
        return 0

    created = _outermost_missing_dir(store.root, config.project_root)
    lock = store.lock()
    try:
        lock.acquire()
    except BaseException:
        # The lock may be another maintainer's: only empty directories this
        # run created are removed, never the lock file.
        _discard_lock_scaffolding(store, created)
        raise
    try:
        require_unchanged_state(config)
        _uninstall_preflight(config, services, store)
        code = _uninstall_locked(config, store)
    except BaseException:
        # A lock file this run created is deleted while still held.
        lock.release(delete=created is not None)
        _discard_lock_scaffolding(store, created)
        raise
    if code != 0:
        lock.release(delete=created is not None)
        _discard_lock_scaffolding(store, created)
        return code
    lock.release(delete=True)
    return _uninstall_finish(config, store)


def describe_in_use(config: Config, services: Services) -> Dict[str, object]:
    """Report which processes run from the runtime root (for inspection)."""

    root = config.layout.local_dir
    if not root.is_dir():
        return {"checked": False, "processes": []}
    try:
        running = processes_under(services, root)
    except ClaudeWranglerError as exc:
        return {"checked": False, "error": str(exc), "processes": []}
    return {
        "checked": True,
        "processes": [{"pid": item.pid, "image": item.image} for item in running],
    }
