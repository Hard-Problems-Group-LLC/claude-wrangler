"""The A/B install transaction: prepare, validate, and promote a release.

This module drives `slots.RuntimeStore` for one maintenance operation while
the caller holds the maintenance lock, following `ab_runtime_slots.md`
(requirements 5-7) and `native_runtime_distribution_and_verification.md`
(requirements 7-11):

1. resolve the platform key (refusing another operating system's build);
2. stage the binary under `.staging/<txid>/<platform>/claude[.exe]`, either
   downloaded (size- and SHA-256-checked while streaming) or adopted from
   an npm-era layout after hashing it against the signed manifest;
3. check its executable structure, smoke-test `--version` and read the
   subcommands from `--help` in transaction scratch;
4. write the retained evidence and build the completion record; and
5. promote into the inactive slot behind an in-use check that refuses,
   fails closed when the process table cannot be read, and stops the
   background service only with `--stop-background-sessions`.

It also re-verifies a slot fully offline (signatures from the retained
evidence, every binary's SHA-256, and the slot's exact file set) for
rollback, repair, selftest, and every maintenance operation that keeps or
reuses a slot, and records the verification stamp the launcher checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .atomicio import atomic_write_bytes
from .constants import SCRIPT_VERSION
from .distribution import ManifestBundle
from .migration import copy_adopted_binary, find_adoptable_binary, sha256_file
from .models import ClaudeWranglerError, Config
from .platforms import PlatformError, binary_filename
from .runtime import eprint, format_utc
from .services import Services, scratch_environment
from .signing import PlatformArtifact
from .slots import (
    SLOT_NAMES,
    CompletionRecord,
    PointerCommittedError,
    PointerCommittedInterrupt,
    PointerError,
    ProcessQueryError,
    RunningProcess,
    RuntimeStore,
    SlotError,
    remove_tree,
    slot_layout_problem,
)


class TransactionError(ClaudeWranglerError):
    """Raised when a candidate cannot be prepared or promoted safely."""


class RuntimeInUseError(ClaudeWranglerError):
    """Raised when processes still run from a tree that must change."""

    def __init__(self, path: Path, processes: Sequence[RunningProcess], purpose: str):
        """Name the blocked path, the processes, and the way forward."""

        self.path = path
        self.processes = list(processes)
        details = ", ".join(
            "pid {} ({})".format(process.pid, process.image) for process in processes
        )
        super().__init__(
            "{}: processes still run from {}: {}. Close those Claude Code "
            "sessions and retry, or pass --stop-background-sessions to run "
            "`claude daemon stop --any` with this project's launcher "
            "environment first (it also terminates this project's background "
            "sessions).".format(purpose, path, details)
        )


def other_slot(name: str) -> str:
    """Return the slot that is not ``name``."""

    return SLOT_NAMES[1] if name == SLOT_NAMES[0] else SLOT_NAMES[0]


def select_platform_key(
    services: Services,
    override: Optional[str],
    available_keys: Optional[Iterable[str]] = None,
) -> str:
    """Return the platform key to install for this host.

    A `--platform` override must name a key for the host operating system:
    installing another system's build is refused, so the structural
    interpreter check and the smoke test always run (requirement 7).
    """

    host_os = services.host_os
    if override is not None and override.split("-", 1)[0] != host_os:
        raise PlatformError(
            "--platform {} is a build for another operating system; this host "
            "is {}. Run claude-wrangler on that system instead (for example, "
            "inside WSL for linux-*), so the binary can be checked and "
            "smoke-tested where it runs.".format(override, host_os)
        )
    keys = sorted(available_keys) if available_keys is not None else None
    key = services.resolve_platform(override, keys)
    if key.split("-", 1)[0] != host_os:
        raise PlatformError(
            "Platform {} does not belong to this host operating system ({}).".format(
                key, host_os
            )
        )
    return key


def artifact_for(bundle: ManifestBundle, key: str) -> PlatformArtifact:
    """Return the manifest artifact for a key, cross-checking its file name."""

    artifact = bundle.verified.artifact_for(key)
    expected = binary_filename(key)
    if artifact.binary != expected:
        raise TransactionError(
            "The signed manifest names binary {!r} for {}, expected {!r}.".format(
                artifact.binary, key, expected
            )
        )
    return artifact


def scratch_forbidden_roots(config: Config, environ: Mapping[str, str]) -> List[str]:
    """Return paths no scratch-run binary may see: real homes and project state."""

    roots = [str(config.layout.config_dir), str(config.layout.claude_home_dir)]
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        value = environ.get(name)
        if value:
            roots.append(value)
    try:
        roots.append(str(Path.home()))
    except (OSError, RuntimeError, KeyError):
        pass
    return sorted(set(roots))


@dataclass
class PreparedCandidate:
    """A validated staging candidate and the completion record to promote."""

    path: Path
    record: CompletionRecord
    adopted_from: Optional[Path] = None
    notes: List[str] = field(default_factory=list)


LONG_WINDOWS_PATH_WARNING = 240


def make_executable(path: Path) -> None:
    """Add execute permission wherever read is granted (POSIX only).

    A downloaded or adopted binary is written without the execute bit; the
    smoke test runs it from staging before promotion sets the bits too.
    """

    if os.name == "nt":
        return
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    wanted = mode | ((mode & 0o444) >> 2) | stat.S_IXUSR
    if wanted != mode:
        os.chmod(str(path), wanted)


def warn_long_windows_path(path: Path, os_name: Optional[str] = None) -> bool:
    """Warn when a Windows binary path approaches MAX_PATH (260 characters).

    Programs without long-path support can fail to start from such a path,
    so a deeply nested checkout is called out before the smoke test runs.
    """

    if (os.name if os_name is None else os_name) != "nt":
        return False
    length = len(str(path))
    if length < LONG_WINDOWS_PATH_WARNING:
        return False
    eprint(
        "[claude-wrangler] Warning: the staged binary path is {} characters long; "
        "Windows programs without long-path support fail at 260. If the smoke "
        "test fails, move the checkout to a shorter path.".format(length)
    )
    return True


def _best_effort_remove(path: Path) -> None:
    """Remove a staging tree, reporting (never raising) a failure."""

    try:
        remove_tree(path)
    except OSError as exc:
        eprint(
            "[claude-wrangler] Warning: could not remove {} ({}); a later "
            "--repair-install clears it.".format(path, exc)
        )


def _carry_over_platforms(
    store: RuntimeStore,
    source_slots: Sequence[str],
    bundle: ManifestBundle,
    candidate: Path,
    skip_key: str,
) -> Dict[str, Dict[str, Any]]:
    """Copy same-release binaries for other platforms from existing slots.

    A checkout shared with WSL may hold several platform keys for one
    release in one slot (ab_runtime_slots.md requirement 11). The active
    slot and the slot being replaced are both consulted, so a repair that
    replaces a damaged slot keeps its other platforms. Each carried binary
    is re-hashed against the signed manifest; one that does not match is
    dropped.
    """

    carried: Dict[str, Dict[str, Any]] = {}
    for source_slot in source_slots:
        _carry_from_slot(store, source_slot, bundle, candidate, skip_key, carried)
    return carried


def _carry_from_slot(
    store: RuntimeStore,
    source_slot: str,
    bundle: ManifestBundle,
    candidate: Path,
    skip_key: str,
    carried: Dict[str, Dict[str, Any]],
) -> None:
    """Carry one slot's verified same-release binaries into ``carried``."""

    record = store.slot_record(source_slot)
    if record is None or record.version != bundle.version:
        return
    slot_dir = store.slot_dir(source_slot)
    for key, entry in sorted(record.platforms.items()):
        if key == skip_key or key in carried:
            continue
        try:
            artifact = artifact_for(bundle, key)
        except ClaudeWranglerError:
            continue
        source = slot_dir.joinpath(*PurePosixPath(entry["binary"]).parts)
        destination = candidate / key / binary_filename(key)
        try:
            if sha256_file(source) != artifact.checksum.lower():
                eprint(
                    "[claude-wrangler] Not carrying {} binary for {}: its SHA-256 "
                    "no longer matches the signed manifest.".format(key, record.version)
                )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(source), str(destination))
        except OSError as exc:
            eprint("[claude-wrangler] Not carrying {} binary: {}".format(key, exc))
            continue
        carried[key] = {
            "binary": "{}/{}".format(key, binary_filename(key)),
            "sha256": artifact.checksum.lower(),
            "size": artifact.size,
        }


def prepare_candidate(
    config: Config,
    services: Services,
    store: RuntimeStore,
    *,
    source: Any,
    bundle: ManifestBundle,
    platform_key: str,
    slot: str,
    channel: str,
    adopt_candidates: Sequence[Path] = (),
    carry_from_slots: Sequence[str] = (),
) -> PreparedCandidate:
    """Stage, validate, and record one release for promotion into ``slot``.

    Nothing outside `.staging/` changes. On any failure the candidate and
    its scratch directory are removed and the error propagates.
    """

    artifact = artifact_for(bundle, platform_key)
    version = bundle.version
    candidate = store.new_staging_dir()
    warn_long_windows_path(candidate / platform_key / binary_filename(platform_key))
    notes: List[str] = []
    adopted_from: Optional[Path] = None
    try:
        destination = candidate / platform_key / binary_filename(platform_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if adopt_candidates:
            adoption = find_adoptable_binary(
                adopt_candidates, artifact.size, artifact.checksum
            )
            for note in adoption.notes:
                notes.append(note)
                eprint("[claude-wrangler] Adopt-in-place: {}".format(note))
            if adoption.source is not None:
                eprint(
                    "[claude-wrangler] Adopting the legacy binary {} (it matches "
                    "the signed manifest for {}).".format(adoption.source, version)
                )
                copy_adopted_binary(adoption.source, destination, artifact.checksum)
                adopted_from = adoption.source
            else:
                eprint(
                    "[claude-wrangler] No legacy binary matches the signed manifest "
                    "for {}; downloading it.".format(version)
                )
        if adopted_from is None:
            url = source.artifact_url(version, platform_key)
            eprint(
                "[claude-wrangler] Downloading Claude Code {} for {} ({} bytes)".format(
                    version, platform_key, artifact.size
                )
            )
            source.fetcher.download_to(
                url,
                destination,
                artifact.size,
                artifact.checksum,
                progress=services.progress(
                    "claude-wrangler: {} {}".format(version, platform_key)
                ),
            )
        services.check_structure(destination, platform_key)
        make_executable(destination)

        scratch = store.new_staging_dir()
        try:
            env = scratch_environment(
                scratch,
                services.environ,
                forbidden_roots=scratch_forbidden_roots(config, services.environ),
            )
            services.smoke_runner(destination, version, env, scratch)
            subcommands = list(services.help_reader(destination, env, scratch))
            settings_rejected = list(
                services.settings_probe(destination, env, scratch, subcommands)
            )
        finally:
            _best_effort_remove(scratch)

        platforms: Dict[str, Dict[str, Any]] = {
            platform_key: {
                "binary": "{}/{}".format(platform_key, binary_filename(platform_key)),
                "sha256": artifact.checksum.lower(),
                "size": artifact.size,
            }
        }
        platforms.update(
            _carry_over_platforms(
                store, carry_from_slots, bundle, candidate, platform_key
            )
        )
        for name, data in bundle.evidence_files().items():
            atomic_write_bytes(candidate / name, data)
        record = CompletionRecord(
            slot=slot,
            version=version,
            channel=channel,
            platforms=platforms,
            manifest_sha256=bundle.verified.manifest_sha256,
            build_date=bundle.verified.build_date,
            commit=bundle.verified.commit,
            schemes=list(bundle.verified.schemes),
            subcommands=subcommands,
            verified_at=format_utc(services.clock()),
            promoted_by=SCRIPT_VERSION,
            settings_rejected=settings_rejected,
        ).validated()
    except BaseException:
        _best_effort_remove(candidate)
        raise
    return PreparedCandidate(candidate, record, adopted_from, notes)


def processes_under(services: Services, path: Path) -> List[RunningProcess]:
    """List processes running from ``path``, failing closed on query errors."""

    try:
        return list(services.find_processes(path))
    except ProcessQueryError as exc:
        raise ClaudeWranglerError(
            "Cannot tell whether anything runs from {} ({}); refusing to continue "
            "rather than guess.".format(path, exc)
        ) from exc


def in_use_fail_closed(services: Services) -> Callable[[Path], bool]:
    """Return a cleanup `in_use` callback that treats query errors as in use."""

    def in_use(path: Path) -> bool:
        """Return True when a process may be running from ``path``."""

        try:
            return bool(services.find_processes(path))
        except ProcessQueryError:
            return True

    return in_use


def require_idle(
    config: Config,
    services: Services,
    path: Path,
    *,
    purpose: str,
    stop_binaries: Sequence[Path],
    launcher_env: Callable[[], Mapping[str, str]],
) -> None:
    """Refuse unless nothing runs from ``path``.

    Claude Code's background service is never stopped implicitly, because
    `daemon stop --any` also ends the operator's background sessions. Only
    with `--stop-background-sessions` is it stopped (with the project's
    launcher environment) and the check repeated.
    """

    running = processes_under(services, path)
    if not running:
        return
    if not config.stop_background_sessions:
        raise RuntimeInUseError(path, running, purpose)
    binary = next((item for item in stop_binaries if item.is_file()), None)
    if binary is None:
        raise RuntimeInUseError(path, running, purpose)
    if config.dry_run:
        # A dry run must not end the operator's background sessions; it
        # only says what the real run would do.
        eprint(
            "[claude-wrangler] Would run {} daemon stop --any (dry run; not "
            "stopped) because processes run from {}: {}".format(
                binary,
                path,
                ", ".join(
                    "pid {} ({})".format(item.pid, item.image) for item in running
                ),
            )
        )
        return
    eprint(
        "[claude-wrangler] Stopping this project's Claude Code background service "
        "(--stop-background-sessions): {} daemon stop --any".format(binary)
    )
    services.stop_service(binary, launcher_env())
    running = processes_under(services, path)
    if running:
        raise RuntimeInUseError(path, running, purpose)


def slot_binaries(store: RuntimeStore, name: Optional[str]) -> List[Path]:
    """Return the recorded binary paths of a slot (empty when unusable)."""

    if name is None:
        return []
    record = store.slot_record(name)
    if record is None:
        return []
    directory = store.slot_dir(name)
    return [
        directory.joinpath(*PurePosixPath(entry["binary"]).parts)
        for _key, entry in sorted(record.platforms.items())
    ]


@dataclass
class PromotionOutcome:
    """Result of promoting a candidate."""

    slot: str
    interrupted: bool = False
    late_error: Optional[str] = None


def promote_candidate(
    config: Config,
    services: Services,
    store: RuntimeStore,
    prepared: PreparedCandidate,
    *,
    active_slot: Optional[str],
    launcher_env: Callable[[], Mapping[str, str]],
    allow_invalid_pointer: bool = False,
) -> PromotionOutcome:
    """Promote a prepared candidate; treat a post-commit failure as committed.

    The in-use check runs only when the target slot is occupied. A
    `PointerCommittedError` or `PointerCommittedInterrupt` means the new
    slot is already live: the caller still publishes metadata and
    generated files, and re-raises the interrupt afterwards.
    """

    target = prepared.record.slot
    stop_binaries = slot_binaries(store, active_slot) + slot_binaries(store, target)

    def ensure_slot_free(slot_path: Path) -> None:
        """Refuse to displace a slot that anything still runs from."""

        require_idle(
            config,
            services,
            slot_path,
            purpose="Refusing to displace runtime slot {}".format(target),
            stop_binaries=stop_binaries,
            launcher_env=launcher_env,
        )

    try:
        slot = store.promote(
            prepared.path,
            prepared.record,
            ensure_slot_free=ensure_slot_free,
            allow_invalid_pointer=allow_invalid_pointer,
            stamp_keys=host_platform_keys(services, prepared.record.platforms),
        )
    except PointerCommittedError as exc:
        eprint("[claude-wrangler] Warning: {}".format(exc))
        return PromotionOutcome(target, late_error=str(exc))
    except PointerCommittedInterrupt as exc:
        eprint("[claude-wrangler] Interrupted after the commit point: {}".format(exc))
        return PromotionOutcome(target, interrupted=True, late_error=str(exc))
    except BaseException:
        if prepared.path.exists():
            _best_effort_remove(prepared.path)
        raise
    return PromotionOutcome(slot)


@dataclass
class SlotVerification:
    """Full offline re-verification result for one slot."""

    name: str
    present: bool
    ok: bool
    problem: Optional[str]
    version: Optional[str] = None
    record: Optional[CompletionRecord] = None
    platforms: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-ready summary."""

        return {
            "present": self.present,
            "verified": self.ok,
            "problem": self.problem,
            "version": self.version,
            "platforms": list(self.platforms),
        }


def verify_slot_offline(
    store: RuntimeStore,
    name: str,
    services: Services,
    platform_key: Optional[str] = None,
) -> SlotVerification:
    """Re-verify a slot from its retained evidence, without the network.

    Both manifest signatures are verified again against the compiled-in
    keys, the manifest hash must equal the record's, every recorded
    binary's size and SHA-256 must equal the signed manifest's, and the
    slot must hold nothing beyond its expected files
    (``slots.slot_layout_problem``). With ``platform_key`` the slot must
    also hold that platform.
    """

    directory = store.slot_dir(name)
    if not directory.is_dir():
        return SlotVerification(
            name, False, False, "slot {} is not installed".format(name)
        )
    problem = store.slot_problem(name)
    record = store.slot_record(name)
    if record is None:
        return SlotVerification(
            name, True, False, problem or "slot {} has no valid record".format(name)
        )
    result = SlotVerification(
        name, True, False, problem, record.version, record, sorted(record.platforms)
    )
    if problem is not None:
        return result
    if platform_key is not None and platform_key not in record.platforms:
        result.problem = "slot {} ({}) has no {} binary".format(
            name, record.version, platform_key
        )
        return result
    layout_problem = slot_layout_problem(directory, record)
    if layout_problem is not None:
        result.problem = "slot {}: {}".format(name, layout_problem)
        return result
    try:
        bundle = services.verify_evidence(directory, record.version)
    except ClaudeWranglerError as exc:
        result.problem = "slot {} retained evidence: {}".format(name, exc)
        return result
    if bundle.verified.manifest_sha256.lower() != record.manifest_sha256.lower():
        result.problem = "slot {} manifest hash differs from its record".format(name)
        return result
    for key, entry in sorted(record.platforms.items()):
        try:
            artifact = bundle.verified.artifact_for(key)
        except ClaudeWranglerError as exc:
            result.problem = "slot {}: {}".format(name, exc)
            return result
        if artifact.checksum.lower() != entry["sha256"].lower() or (
            artifact.size != entry["size"]
        ):
            result.problem = (
                "slot {} record for {} differs from the signed manifest".format(
                    name, key
                )
            )
            return result
        binary = directory.joinpath(*PurePosixPath(entry["binary"]).parts)
        try:
            digest = sha256_file(binary)
        except OSError as exc:
            result.problem = "slot {} cannot hash {}: {}".format(name, binary, exc)
            return result
        if digest != artifact.checksum.lower():
            result.problem = (
                "slot {} binary {} SHA-256 does not match the signed manifest".format(
                    name, entry["binary"]
                )
            )
            return result
    result.ok = True
    result.problem = None
    return result


def host_platform_keys(services: Services, platforms: Iterable[str]) -> List[str]:
    """Return the platform keys this host's launcher runs, for stamping.

    Only binaries for the host's operating system are stamped; a checkout
    shared with WSL keeps the other system's stamps (``slots.write_stamp``).
    """

    return sorted(key for key in platforms if key.split("-", 1)[0] == services.host_os)


def verify_and_stamp(
    store: RuntimeStore,
    name: str,
    services: Services,
    platform_key: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> SlotVerification:
    """Verify a slot fully offline and, when it passes, record its stamp.

    Every maintenance operation that keeps, reuses, or activates a slot
    calls this, so the launcher only ever runs a binary whose identity was
    recorded right after a full verification. The binaries' identities are
    read before hashing and compared afterwards; a binary that changed in
    between fails. A slot whose integrity fails loses its whole stamp, so
    no launcher on any system runs it until a later verification passes.
    A slot that is intact but lacks ``platform_key`` (a checkout shared
    with WSL before this system's binary was added) fails without losing
    its stamp, so the other system keeps launching it. ``dry_run``
    verifies without writing or removing a stamp.
    """

    keys: List[str] = []
    before: Dict[str, Dict[str, int]] = {}
    record = store.slot_record(name)
    if record is not None:
        keys = host_platform_keys(services, record.platforms)
        try:
            before = store.binary_identities(name, keys)
        except (OSError, SlotError):
            before = {}
    result = verify_slot_offline(store, name, services)
    if result.ok:
        try:
            after = store.binary_identities(name, keys)
        except (OSError, SlotError) as exc:
            after = {}
            result.problem = "slot {} changed while it was verified: {}".format(
                name, exc
            )
        if result.problem is None and after != before:
            result.problem = "slot {} changed while it was verified".format(name)
        if result.problem is None:
            if not dry_run:
                store.write_stamp(name, after)
            if platform_key is not None and platform_key not in result.platforms:
                result.ok = False
                result.problem = "slot {} ({}) has no {} binary".format(
                    name, result.version, platform_key
                )
            return result
        result.ok = False
    if result.present and not dry_run:
        store.remove_stamp(name)
    return result


def read_pointer_quietly(store: RuntimeStore) -> Optional[str]:
    """Return the pointer's slot, or None when it is absent or malformed."""

    try:
        return store.read_pointer()
    except PointerError:
        return None


def active_slot_for(store: RuntimeStore, platform_key: str) -> Optional[str]:
    """Return the active slot when it is complete for ``platform_key``."""

    name = read_pointer_quietly(store)
    if name is None:
        return None
    if store.slot_problem(name, platform_key) is not None:
        return None
    return name


def native_runtime_complete(store: RuntimeStore, platform_key: Optional[str]) -> bool:
    """Return True when the pointer names a slot complete for the host key.

    Read-only and lock-free; it never reads `<runtime root>/.lock`.
    """

    if not store.root.is_dir():
        return False
    name = read_pointer_quietly(store)
    if name is None:
        return False
    try:
        return store.slot_problem(name, platform_key) is None
    except OSError:
        return False
