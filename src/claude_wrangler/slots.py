"""A/B runtime slots for project-local native Claude Code binaries.

This module implements the storage half of ``ab_runtime_slots.md``: the
runtime-root layout, the strict ``active-slot`` pointer, completion records,
the maintenance lock, reversible promotion of a validated staging candidate,
cleanup of staging and retired trees, and the in-use checks that guard every
displacement. Download, signature verification, and metadata publication
belong to the operations layer, which drives this module while holding
``MaintenanceLock``.

Layout under the runtime root (``.local/claude/wrangler`` by default)::

    .lock                     maintenance lock, never removed
    active-slot               exactly "a\\n" or "b\\n" on every OS
    slots/<a|b>/              one verified release per slot
      <platform>/claude[.exe]
      manifest.json, manifest.json.sig, manifest.json.raw-sig.json
      .complete.json          completion record, written last
      .verified.json          verification stamp (binary identity)
    .staging/<txid>/          candidate being prepared
    .retired/<txid>/          displaced slot awaiting deletion

The pointer is a regular file on every operating system, never a symbolic
link, so one checkout behaves identically on Windows, macOS, Linux, and WSL.
It is replaced only through a same-directory temporary file and
``os.replace``; that replacement is the promotion commit point.

The verification stamp records each binary's size, modification time, and
(on POSIX) inode at the moment maintenance last verified the slot in full.
A git clone or pull cannot carry a matching stamp: it writes new files with
a fresh modification time and inode, so the launcher's cheap check refuses
a binary it planted or replaced after verification. The Windows stamp has
no inode, so an archive or copy tool that preserves modification times
could restore a same-size binary together with a matching stamp.

This module is standard-library-only and Python 3.9 compatible.
"""

from __future__ import annotations

from collections.abc import Mapping as AbcMapping
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)
import uuid

from .atomicio import (
    atomic_write_bytes,
    atomic_write_text,
    fsync_directory,
    retry_on_windows_lock,
)
from .models import ClaudeWranglerError

SLOT_NAMES = ("a", "b")
COMPLETION_RECORD_NAME = ".complete.json"
POINTER_NAME = "active-slot"
LOCK_NAME = ".lock"
SLOTS_DIR_NAME = "slots"
STAGING_DIR_NAME = ".staging"
RETIRED_DIR_NAME = ".retired"
COMPLETION_RECORD_SCHEMA = 1
# Mirrored by launcher_core.VERIFICATION_STAMP_NAME and _SCHEMA.
VERIFICATION_STAMP_NAME = ".verified.json"
VERIFICATION_STAMP_SCHEMA = 1

# Retained signature evidence every promoted slot keeps for offline
# re-verification (native_runtime_distribution_and_verification.md 11).
MANIFEST_NAME = "manifest.json"
EVIDENCE_FILE_NAMES = (
    MANIFEST_NAME,
    "manifest.json.sig",
    "manifest.json.raw-sig.json",
)
# Entry names tolerated anywhere in a slot by the exact-layout check:
# Finder metadata cannot run code and appears when a folder is browsed.
TOLERATED_SLOT_ENTRY_NAMES = (".DS_Store",)

DEFAULT_SERVICE_STOP_TIMEOUT_SECONDS = 30.0

_POINTER_MAX_BYTES = 16
_RECORD_MAX_BYTES = 1024 * 1024
_STAMP_MAX_BYTES = 64 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
# ASCII digits with ``fullmatch``: ``\d`` would accept other Unicode digits
# and ``$`` a trailing newline, both of which distribution.py refuses.
_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
# The temporary sibling ``atomicio.atomic_write_bytes`` uses for the files
# maintenance writes into a slot; a hard kill can leave one behind.
_SLOT_TEMPORARY_RE = re.compile(
    r"\.(?:\.complete\.json|\.verified\.json)\.[0-9a-f]{32}\.tmp"
)
# errno values a non-blocking lock attempt reports when another process
# holds the lock (msvcrt reports EACCES or EDEADLOCK on Windows).
_LOCK_CONTENTION_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EACCES", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)

# Windows reparse-point facts. A reparse tag with the name-surrogate bit set
# (symbolic links, junctions, and similar) makes the entry an alias for
# another location; other tags such as cloud-file placeholders do not.
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REPARSE_TAG_NAME_SURROGATE_BIT = 0x20000000


class SlotError(ClaudeWranglerError):
    """Raised when the runtime slot store is unsafe or inconsistent."""


class UnsafePathError(SlotError):
    """Raised when a managed path is a link, junction, or wrong file type."""


class PointerError(SlotError):
    """Raised when ``active-slot`` is malformed or names an incomplete slot."""


class CompletionRecordError(SlotError):
    """Raised when a completion record is malformed."""


class PromotionError(SlotError):
    """Raised when promotion fails before its commit point.

    When this is raised the previous pointer, both fixed slots, and every
    file outside the staging candidate are unchanged, unless the message
    reports that restoration itself failed.
    """


class PointerCommittedError(SlotError):
    """Raised when a failure is observed after the pointer already switched.

    The new slot is active and verified; the caller should continue with its
    post-commit steps or report the late failure, never roll back by
    deleting anything.
    """


class PointerCommittedInterrupt(KeyboardInterrupt):
    """Raised when an interrupt arrives after the pointer already switched.

    It is still a ``KeyboardInterrupt`` so interrupt handling stays uniform,
    but callers can tell that the new slot is already active.
    """


class SlotInUseError(SlotError):
    """Raised when a process still runs from a tree that must be displaced."""

    def __init__(self, path: Path, processes: List["RunningProcess"]) -> None:
        """Record the blocked path and the processes that block it."""

        self.path = path
        self.processes = list(processes)
        details = ", ".join(
            "pid {} ({})".format(process.pid, process.image)
            for process in self.processes
        )
        super().__init__(
            "Refusing to displace {} because processes still run from it: {}. "
            "Close those Claude Code sessions and retry.".format(path, details)
        )


class MaintenanceLockError(ClaudeWranglerError):
    """Raised when the maintenance lock is held elsewhere or unsafe."""


class MaintenanceLockUnsupportedError(MaintenanceLockError):
    """Raised when the filesystem cannot take file locks at all.

    No other maintainer can hold such a lock either. ``created_lock_file``
    says whether this attempt created the lock file (it did not exist when
    the attempt started), so a caller that also created the runtime root
    can remove what it left behind.
    """

    def __init__(self, message: str, *, created_lock_file: bool) -> None:
        """Keep the message and whether this attempt created the lock file."""

        super().__init__(message)
        self.created_lock_file = created_lock_file


class ProcessQueryError(ClaudeWranglerError):
    """Raised when running processes cannot be enumerated reliably."""


class ServiceStopError(ClaudeWranglerError):
    """Raised when stopping the background service does not finish in time."""


def _new_txid(label: str = "") -> str:
    """Return a sortable, collision-resistant transaction identifier."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    middle = "-{}".format(label) if label else ""
    return "{}{}-{}".format(stamp, middle, uuid.uuid4().hex[:12])


def _lexists(path: Path) -> bool:
    """Return whether a directory entry exists, including a dangling link."""

    return os.path.lexists(str(path))


def _status_is_link_like(status: os.stat_result) -> bool:
    """Return whether an ``lstat`` result describes a link or junction."""

    if stat.S_ISLNK(status.st_mode):
        return True
    attributes = getattr(status, "st_file_attributes", 0)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(status, "st_reparse_tag", 0)
        return bool(tag & _REPARSE_TAG_NAME_SURROGATE_BIT)
    return False


def is_link_like(path: Path) -> bool:
    """Return whether ``path`` is a symbolic link, junction, or other alias.

    A missing path is not link-like. Any other ``lstat`` failure propagates
    so callers fail closed instead of treating an unreadable entry as safe.
    """

    try:
        status = os.lstat(str(path))
    except (FileNotFoundError, NotADirectoryError):
        return False
    return _status_is_link_like(status)


def _same_path(left: Path, right: Path) -> bool:
    """Compare two absolute paths with the platform's case rules."""

    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def validate_runtime_root(root: Path, project_root: Optional[Path] = None) -> Path:
    """Return the absolute runtime root after proving it is not aliased.

    The root itself must not be a symbolic link or junction and, when it
    exists, must be a directory. When ``project_root`` is given, the root
    must sit strictly inside it and no existing component between them may
    be a link or junction; the project root itself may be one.
    """

    root_abs = Path(os.path.abspath(str(root)))
    if project_root is not None:
        project_abs = Path(os.path.abspath(str(project_root)))
        try:
            relative = root_abs.relative_to(project_abs)
        except ValueError as exc:
            raise UnsafePathError(
                "Runtime root {} is not inside the project root {}.".format(
                    root_abs, project_abs
                )
            ) from exc
        if not relative.parts:
            raise UnsafePathError(
                "Runtime root may not be the project root itself: {}".format(root_abs)
            )
        cursor = project_abs
        for part in relative.parts:
            cursor = cursor / part
            if not _lexists(cursor):
                break
            if is_link_like(cursor):
                raise UnsafePathError(
                    "Runtime root or an ancestor under the project is a symbolic "
                    "link or junction: {}".format(cursor)
                )
            if not cursor.is_dir():
                raise UnsafePathError(
                    "Runtime root or an ancestor is not a directory: {}".format(cursor)
                )
        return root_abs
    if is_link_like(root_abs):
        raise UnsafePathError(
            "Runtime root is a symbolic link or junction: {}".format(root_abs)
        )
    if _lexists(root_abs) and not root_abs.is_dir():
        raise UnsafePathError("Runtime root is not a directory: {}".format(root_abs))
    return root_abs


def _path_key(path: Path) -> Tuple[str, ...]:
    """Return case-folded absolute path components for overlap checks.

    Folding on every OS is deliberately conservative: a checkout can move
    between case-sensitive and case-insensitive filesystems.
    """

    absolute = os.path.normpath(os.path.abspath(str(path)))
    return tuple(part.casefold() for part in Path(absolute).parts)


def _overlaps(left: Path, right: Path) -> bool:
    """Return whether two paths are equal or one contains the other."""

    left_key = _path_key(left)
    right_key = _path_key(right)
    shorter = min(len(left_key), len(right_key))
    return left_key[:shorter] == right_key[:shorter]


def check_runtime_root_placement(
    root: Path,
    *,
    home_dir: Path,
    config_dir: Path,
    extra_bases: Iterable[Path] = (),
) -> None:
    """Refuse a runtime root that Claude Code itself might prune or own.

    The root must not be, contain, or sit inside the redirected ``HOME``,
    the config dir's Claude-owned ``local/``, ``downloads/``, or ``cache/``
    children, or any ``claude/versions`` directory (Claude Code housekeeping
    prunes those). ``extra_bases`` names further homes or ``XDG_*``
    directories whose ``claude/versions`` child must also stay disjoint.
    """

    forbidden = [Path(home_dir)]
    forbidden.extend(
        Path(config_dir) / child for child in ("local", "downloads", "cache")
    )
    forbidden.extend(Path(base) / "claude" / "versions" for base in extra_bases)
    for path in forbidden:
        if _overlaps(root, path):
            raise UnsafePathError(
                "Runtime root {} overlaps {}, which Claude Code owns or prunes; "
                "choose a different --local-dir.".format(root, path)
            )
    parts = _path_key(root)
    for index in range(len(parts) - 1):
        if parts[index] == "claude" and parts[index + 1] == "versions":
            raise UnsafePathError(
                "Runtime root {} sits inside a claude/versions directory, which "
                "Claude Code housekeeping prunes.".format(root)
            )


def _require_real_directory_or_absent(path: Path, label: str) -> None:
    """Reject a link, junction, or non-directory at a managed container path."""

    if not _lexists(path):
        return
    if is_link_like(path):
        raise UnsafePathError(
            "{} is a symbolic link or junction: {}".format(label, path)
        )
    if not path.is_dir():
        raise UnsafePathError("{} is not a directory: {}".format(label, path))


def _validate_relpath(value: Any) -> str:
    """Return a slot-relative POSIX path after rejecting escapes."""

    if not isinstance(value, str) or not value:
        raise CompletionRecordError("binary path must be a non-empty string")
    if "\\" in value or ":" in value or "\x00" in value:
        raise CompletionRecordError(
            "binary path must use POSIX separators without drive or stream "
            "syntax: {!r}".format(value)
        )
    pure = PurePosixPath(value)
    if pure.is_absolute() or str(pure) != value:
        raise CompletionRecordError(
            "binary path must be relative and normalized: {!r}".format(value)
        )
    if any(part in ("", ".", "..") for part in pure.parts):
        raise CompletionRecordError(
            "binary path may not contain '.' or '..': {!r}".format(value)
        )
    return value


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    """Return a required non-empty string field."""

    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CompletionRecordError("{} must be a non-empty string".format(key))
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> Optional[str]:
    """Return an optional string field, which may be null or absent."""

    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CompletionRecordError("{} must be a string or null".format(key))
    return value


def _string_list(payload: Mapping[str, Any], key: str) -> List[str]:
    """Return a required list of strings."""

    value = payload.get(key)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise CompletionRecordError("{} must be a list of strings".format(key))
    return list(value)


def _sha256_field(value: Any, label: str) -> str:
    """Return a lowercase 64-hex-digit SHA-256 value."""

    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise CompletionRecordError(
            "{} must be a 64-hex-digit SHA-256 value".format(label)
        )
    return value.lower()


def _positive_int(value: Any, label: str) -> int:
    """Return a positive integer, rejecting booleans."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CompletionRecordError("{} must be a positive integer".format(label))
    return value


@dataclass
class CompletionRecord:
    """The durable statement that one slot holds one verified release.

    ``platforms`` maps a manifest platform key to ``{"binary": <slot-relative
    POSIX path>, "sha256": <hex>, "size": <bytes>}``. The JSON form uses the
    field names below as keys; readers must ignore unknown keys. ``schema``
    is declared last only because dataclass fields with defaults must follow
    those without; construct records with keyword arguments.
    """

    slot: str
    version: str
    channel: str
    platforms: Dict[str, Dict[str, Any]]
    manifest_sha256: str
    build_date: Optional[str]
    commit: Optional[str]
    schemes: List[str]
    subcommands: List[str]
    verified_at: str
    promoted_by: str
    schema: int = COMPLETION_RECORD_SCHEMA
    # Recorded subcommands that refused `--settings` at install time
    # (cross_platform_launcher.md requirement 4). None means the slot was
    # promoted before this probe existed: the launcher then omits
    # `--settings` for every recorded subcommand.
    settings_rejected: Optional[List[str]] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CompletionRecord":
        """Build a validated record from parsed JSON, ignoring unknown keys."""

        if not isinstance(payload, AbcMapping):
            raise CompletionRecordError("completion record must be a JSON object")
        schema = payload.get("schema")
        if (
            isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema != COMPLETION_RECORD_SCHEMA
        ):
            raise CompletionRecordError(
                "unsupported completion record schema: {!r}".format(schema)
            )
        slot = payload.get("slot")
        if slot not in SLOT_NAMES:
            raise CompletionRecordError("slot must be 'a' or 'b': {!r}".format(slot))
        version = _require_string(payload, "version")
        if not _VERSION_PATTERN.fullmatch(version):
            raise CompletionRecordError(
                "version must look like MAJOR.MINOR.PATCH: {!r}".format(version)
            )
        raw_platforms = payload.get("platforms")
        if not isinstance(raw_platforms, AbcMapping) or not raw_platforms:
            raise CompletionRecordError("platforms must be a non-empty object")
        platforms: Dict[str, Dict[str, Any]] = {}
        for key, entry in raw_platforms.items():
            if not isinstance(key, str) or not key:
                raise CompletionRecordError("platform keys must be non-empty strings")
            if not isinstance(entry, AbcMapping):
                raise CompletionRecordError(
                    "platform {} entry must be an object".format(key)
                )
            platforms[key] = {
                "binary": _validate_relpath(entry.get("binary")),
                "sha256": _sha256_field(entry.get("sha256"), key + " sha256"),
                "size": _positive_int(entry.get("size"), key + " size"),
            }
        return cls(
            slot=slot,
            version=version,
            channel=_require_string(payload, "channel"),
            platforms=platforms,
            manifest_sha256=_sha256_field(
                payload.get("manifest_sha256"), "manifest_sha256"
            ),
            build_date=_optional_string(payload, "build_date"),
            commit=_optional_string(payload, "commit"),
            schemes=_string_list(payload, "schemes"),
            subcommands=_string_list(payload, "subcommands"),
            verified_at=_require_string(payload, "verified_at"),
            promoted_by=_require_string(payload, "promoted_by"),
            schema=schema,
            settings_rejected=(
                None
                if payload.get("settings_rejected") is None
                else _string_list(payload, "settings_rejected")
            ),
        )

    @classmethod
    def from_json(cls, text: Union[str, bytes]) -> "CompletionRecord":
        """Parse and validate a record from its JSON text or UTF-8 bytes."""

        try:
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            payload = json.loads(text)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CompletionRecordError(
                "completion record is not valid JSON: {}".format(exc)
            ) from exc
        return cls.from_dict(payload)

    def validated(self) -> "CompletionRecord":
        """Return a normalized, validated copy or raise ``CompletionRecordError``."""

        return CompletionRecord.from_dict(self._raw_dict())

    def _raw_dict(self) -> Dict[str, Any]:
        """Return the fields as a plain dict without validation."""

        return {
            "schema": self.schema,
            "slot": self.slot,
            "version": self.version,
            "channel": self.channel,
            "platforms": self.platforms,
            "manifest_sha256": self.manifest_sha256,
            "build_date": self.build_date,
            "commit": self.commit,
            "schemes": list(self.schemes),
            "subcommands": list(self.subcommands),
            "verified_at": self.verified_at,
            "promoted_by": self.promoted_by,
            "settings_rejected": (
                list(self.settings_rejected)
                if self.settings_rejected is not None
                else None
            ),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return the validated JSON-ready mapping for this record."""

        normalized = self.validated()
        payload = normalized._raw_dict()
        payload["platforms"] = {
            key: dict(normalized.platforms[key]) for key in sorted(normalized.platforms)
        }
        return payload

    def to_json(self) -> str:
        """Return the canonical JSON text (sorted keys, LF, trailing newline)."""

        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def binary_relpath(self, platform_key: str) -> str:
        """Return the slot-relative binary path recorded for one platform."""

        entry = self.platforms.get(platform_key)
        if entry is None:
            raise CompletionRecordError(
                "completion record has no binary for platform {}".format(platform_key)
            )
        return _validate_relpath(entry.get("binary"))


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, streamed in chunks."""

    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _contained_regular_file(base: Path, relpath: str) -> Tuple[Optional[Path], str]:
    """Resolve a slot-relative file without following any link.

    Returns ``(path, "")`` for a regular file reached through real
    directories, or ``(None, reason)`` otherwise.
    """

    cursor = base
    parts = PurePosixPath(relpath).parts
    for index, part in enumerate(parts):
        cursor = cursor / part
        try:
            status = os.lstat(str(cursor))
        except (FileNotFoundError, NotADirectoryError):
            return None, "{} is missing".format(relpath)
        except OSError as exc:
            return None, "cannot inspect {}: {}".format(relpath, exc)
        if _status_is_link_like(status):
            return None, "{} passes through a link or junction".format(relpath)
        last = index == len(parts) - 1
        if last and not stat.S_ISREG(status.st_mode):
            return None, "{} is not a regular file".format(relpath)
        if not last and not stat.S_ISDIR(status.st_mode):
            return None, "{} has a non-directory ancestor".format(relpath)
    return cursor, ""


def _read_record_file(directory: Path) -> Tuple[Optional[CompletionRecord], str]:
    """Read and validate the completion record inside one directory."""

    path, reason = _contained_regular_file(directory, COMPLETION_RECORD_NAME)
    if path is None:
        return None, "completion record: " + reason
    try:
        if os.lstat(str(path)).st_size > _RECORD_MAX_BYTES:
            return None, "completion record is implausibly large"
        data = path.read_bytes()
    except OSError as exc:
        return None, "completion record unreadable: {}".format(exc)
    try:
        return CompletionRecord.from_json(data), ""
    except CompletionRecordError as exc:
        return None, "completion record invalid: {}".format(exc)


def _binaries_problem(
    directory: Path,
    record: CompletionRecord,
    platform_keys: Iterable[str],
    verify_checksums: bool,
    require_executable: bool = True,
) -> Optional[str]:
    """Return why recorded binaries do not match the record, or ``None``.

    ``require_executable`` mirrors the launcher's POSIX execute-permission
    check; Windows has no such bit.
    """

    for key in platform_keys:
        entry = record.platforms.get(key)
        if entry is None:
            return "no binary recorded for platform {}".format(key)
        path, reason = _contained_regular_file(directory, entry["binary"])
        if path is None:
            return "binary " + reason
        try:
            size = os.lstat(str(path)).st_size
        except OSError as exc:
            return "cannot inspect binary {}: {}".format(entry["binary"], exc)
        if size != entry["size"]:
            return "binary {} is {} bytes, record says {}".format(
                entry["binary"], size, entry["size"]
            )
        if require_executable and os.name != "nt" and not os.access(path, os.X_OK):
            return "binary {} is not executable".format(entry["binary"])
        if verify_checksums:
            try:
                digest = _sha256_file(path)
            except OSError as exc:
                return "cannot hash binary {}: {}".format(entry["binary"], exc)
            if digest != entry["sha256"].lower():
                return "binary {} SHA-256 {} does not match record {}".format(
                    entry["binary"], digest, entry["sha256"]
                )
    return None


def _make_binaries_executable(directory: Path, record: CompletionRecord) -> None:
    """Add execute permission wherever read permission exists (POSIX only)."""

    if os.name == "nt":
        return
    for entry in record.platforms.values():
        path = directory.joinpath(*PurePosixPath(entry["binary"]).parts)
        mode = stat.S_IMODE(os.lstat(str(path)).st_mode)
        wanted = mode | ((mode & 0o444) >> 2) | stat.S_IXUSR
        if wanted != mode:
            os.chmod(str(path), wanted)


def local_os_family() -> str:
    """Return this host's platform-key prefix: win32, darwin, or linux.

    Maintenance stamps only binaries whose platform key starts with this
    prefix, because only this operating system's launcher runs them and
    another system (WSL sharing a Windows checkout) may see different
    file identities for the same file.
    """

    if os.name == "nt":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def binary_identity(status: os.stat_result) -> Dict[str, int]:
    """Return the identity a verification stamp records for one binary.

    Size and modification time in nanoseconds everywhere, plus the inode on
    POSIX. Windows inode values differ between Python versions on ReFS, so
    they are left out there; a git checkout or pull still writes a new
    modification time, but an archive that preserves modification times
    could reproduce a Windows identity. Mirrored by
    ``launcher_core._binary_identity``.
    """

    identity = {"size": int(status.st_size), "mtime_ns": int(status.st_mtime_ns)}
    if os.name != "nt":
        identity["ino"] = int(status.st_ino)
    return identity


def _identity_mapping(value: Any) -> Optional[Dict[str, int]]:
    """Return a stamp entry's integer fields, or None when malformed."""

    if not isinstance(value, AbcMapping):
        return None
    result: Dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool):
            return None
        if not isinstance(item, int):
            return None
        result[key] = item
    if "size" not in result or "mtime_ns" not in result:
        return None
    return result


def read_verification_stamp(directory: Path) -> Dict[str, Dict[str, int]]:
    """Return a slot's stamp entries by platform key ({} when absent or bad).

    The stamp is read without following links; a malformed stamp is
    treated as absent, which makes the launcher refuse the slot until
    maintenance verifies it again.
    """

    path, _reason = _contained_regular_file(directory, VERIFICATION_STAMP_NAME)
    if path is None:
        return {}
    try:
        if os.lstat(str(path)).st_size > _STAMP_MAX_BYTES:
            return {}
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(payload, AbcMapping):
        return {}
    if payload.get("schema") != VERIFICATION_STAMP_SCHEMA or isinstance(
        payload.get("schema"), bool
    ):
        return {}
    binaries = payload.get("binaries")
    if not isinstance(binaries, AbcMapping):
        return {}
    entries: Dict[str, Dict[str, int]] = {}
    for key, value in binaries.items():
        identity = _identity_mapping(value)
        if isinstance(key, str) and identity is not None:
            entries[key] = identity
    return entries


def slot_layout_problem(directory: Path, record: CompletionRecord) -> Optional[str]:
    """Return why a slot holds anything beyond its expected files, or None.

    A slot may hold only its completion record, verification stamp, the
    three retained-evidence files, and each recorded binary inside the
    directories that lead to it. Anything else, including a link or
    junction anywhere, fails: on Windows a DLL beside an executable is
    loaded before the system copy, so an extra file can change what a
    verified binary runs. ``.DS_Store`` and the temporary siblings a
    killed atomic write leaves are tolerated.
    """

    top_level = {COMPLETION_RECORD_NAME, VERIFICATION_STAMP_NAME}
    top_level.update(EVIDENCE_FILE_NAMES)
    binaries = set()
    directories = set()
    for entry in record.platforms.values():
        parts = tuple(PurePosixPath(entry["binary"]).parts)
        binaries.add(parts)
        for index in range(1, len(parts)):
            directories.add(parts[:index])

    def walk(current: Path, prefix: Tuple[str, ...]) -> Optional[str]:
        """Check one directory level and recurse into expected directories."""

        try:
            with os.scandir(str(current)) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            return "cannot list {}: {}".format(current, exc)
        for item in entries:
            relative = prefix + (item.name,)
            shown = "/".join(relative)
            try:
                status = os.lstat(item.path)
            except OSError as exc:
                return "cannot inspect {}: {}".format(shown, exc)
            if _status_is_link_like(status):
                return "{} is a link or junction".format(shown)
            if item.name in TOLERATED_SLOT_ENTRY_NAMES and stat.S_ISREG(status.st_mode):
                continue
            if stat.S_ISDIR(status.st_mode):
                if relative not in directories:
                    return "unexpected directory {}".format(shown)
                problem = walk(Path(item.path), relative)
                if problem is not None:
                    return problem
                continue
            if not stat.S_ISREG(status.st_mode):
                return "{} is not a regular file".format(shown)
            if relative in binaries:
                continue
            if not prefix and (
                item.name in top_level or _SLOT_TEMPORARY_RE.fullmatch(item.name)
            ):
                continue
            return "unexpected file {}".format(shown)
        return None

    return walk(directory, ())


def _open_for_flush(path: Path) -> int:
    """Open a file so ``os.fsync`` is permitted on this platform."""

    if os.name == "nt":
        # FlushFileBuffers requires write access on Windows.
        return os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
    return os.open(str(path), os.O_RDONLY)


def _flush_tree(root: Path) -> None:
    """Flush every regular file and directory in a candidate tree."""

    directories: List[Path] = []
    for current, dirnames, filenames in os.walk(str(root), followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for dirname in dirnames:
            if is_link_like(current_path / dirname):
                raise PromotionError(
                    "Candidate contains a link or junction: {}".format(
                        current_path / dirname
                    )
                )
        for filename in filenames:
            path = current_path / filename
            status = os.lstat(str(path))
            if _status_is_link_like(status) or not stat.S_ISREG(status.st_mode):
                raise PromotionError(
                    "Candidate contains a non-regular file: {}".format(path)
                )

            def flush_one(target: Path = path) -> None:
                """Open, flush, and close one candidate file."""

                descriptor = _open_for_flush(target)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

            retry_on_windows_lock(flush_one)
    for directory in reversed(directories):
        fsync_directory(directory)


def _remove_link(path: Path, status: os.stat_result) -> None:
    """Remove a link or junction itself, never its target."""

    attributes = getattr(status, "st_file_attributes", 0)
    if os.name == "nt" and attributes & _FILE_ATTRIBUTE_DIRECTORY:
        retry_on_windows_lock(lambda: os.rmdir(str(path)))
    else:
        retry_on_windows_lock(lambda: os.unlink(str(path)))


def _unlink_file(path: Path) -> None:
    """Delete one file, clearing the Windows read-only attribute if needed."""

    def attempt() -> None:
        """Try one deletion, retrying immediately if read-only was the cause."""

        try:
            os.unlink(str(path))
        except PermissionError:
            if os.name != "nt":
                raise
            attributes = getattr(os.lstat(str(path)), "st_file_attributes", 0)
            if not attributes & _FILE_ATTRIBUTE_READONLY:
                raise
            os.chmod(str(path), stat.S_IWRITE | stat.S_IREAD)
            os.unlink(str(path))

    retry_on_windows_lock(attempt)


def remove_tree(path: Path) -> None:
    """Delete a file or directory tree without following links or junctions.

    Links and junctions found anywhere in the tree are removed as entries,
    never traversed. Windows lock errors are retried per entry.
    """

    try:
        status = os.lstat(str(path))
    except FileNotFoundError:
        return
    if _status_is_link_like(status):
        _remove_link(path, status)
        return
    if stat.S_ISDIR(status.st_mode):
        with os.scandir(str(path)) as iterator:
            children = [Path(entry.path) for entry in iterator]
        for child in children:
            remove_tree(child)
        retry_on_windows_lock(lambda: os.rmdir(str(path)))
        return
    _unlink_file(path)


@dataclass(frozen=True)
class RunningProcess:
    """One running process and the full path of its executable image."""

    pid: int
    image: str


class RuntimeStore:
    """The A/B slot store rooted at one project's runtime directory.

    Mutating methods (``new_staging_dir``, ``promote``, ``set_active``, and
    ``cleanup_staging_and_retired``) must be called while the caller holds
    ``MaintenanceLock`` for the same root. Read methods never write.
    """

    def __init__(self, root: Path, *, project_root: Optional[Path] = None) -> None:
        """Bind the store to a runtime root, optionally confined to a project."""

        self.root = Path(os.path.abspath(str(root)))
        self.project_root = (
            Path(os.path.abspath(str(project_root)))
            if project_root is not None
            else None
        )
        self.slots_dir = self.root / SLOTS_DIR_NAME
        self.staging_dir = self.root / STAGING_DIR_NAME
        self.retired_dir = self.root / RETIRED_DIR_NAME
        self.pointer_path = self.root / POINTER_NAME
        self.lock_path = self.root / LOCK_NAME

    def lock(self) -> "MaintenanceLock":
        """Return an unacquired maintenance lock for this runtime root."""

        return MaintenanceLock(self.root, project_root=self.project_root)

    def validate(self) -> None:
        """Reject an aliased root, container, or fixed slot directory."""

        validate_runtime_root(self.root, self.project_root)
        _require_real_directory_or_absent(self.slots_dir, "Slots directory")
        _require_real_directory_or_absent(self.staging_dir, "Staging directory")
        _require_real_directory_or_absent(self.retired_dir, "Retired directory")
        for name in SLOT_NAMES:
            _require_real_directory_or_absent(
                self.slots_dir / name, "Slot {}".format(name)
            )

    def slot_dir(self, name: str) -> Path:
        """Return the fixed directory for slot ``a`` or ``b``."""

        if name not in SLOT_NAMES:
            raise SlotError("Unknown runtime slot: {!r}".format(name))
        return self.slots_dir / name

    def read_pointer(self) -> Optional[str]:
        """Return the slot named by ``active-slot``, or ``None`` when absent.

        The pointer must be a regular, non-link file whose bytes are exactly
        ``a\\n`` or ``b\\n``; anything else raises ``PointerError``. This
        does not check that the named slot is complete.
        """

        path = self.pointer_path
        try:
            status = os.lstat(str(path))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PointerError(
                "Cannot inspect active-slot pointer {}: {}".format(path, exc)
            ) from exc
        if _status_is_link_like(status) or not stat.S_ISREG(status.st_mode):
            raise PointerError(
                "Active-slot pointer must be a regular file: {}".format(path)
            )
        if status.st_size > _POINTER_MAX_BYTES:
            raise PointerError(
                "Active-slot pointer must contain exactly 'a' or 'b' and one LF: "
                "{}".format(path)
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PointerError(
                "Cannot read active-slot pointer {}: {}".format(path, exc)
            ) from exc
        for name in SLOT_NAMES:
            if content == (name + "\n").encode("ascii"):
                return name
        raise PointerError(
            "Active-slot pointer must contain exactly 'a' or 'b' and one LF, "
            "found {!r}: {}".format(content, path)
        )

    def slot_record(self, name: str) -> Optional[CompletionRecord]:
        """Return the slot's validated completion record, or ``None``.

        ``None`` covers a missing, linked, unreadable, or malformed record
        and a record that names the other slot. Binaries are not checked.
        """

        directory = self.slot_dir(name)
        if is_link_like(self.slots_dir) or is_link_like(directory):
            return None
        if not directory.is_dir():
            return None
        record, _reason = _read_record_file(directory)
        if record is None or record.slot != name:
            return None
        return record

    def slot_problem(
        self, name: str, platform_key: Optional[str] = None
    ) -> Optional[str]:
        """Return why a slot is not complete, or ``None`` when it is.

        A slot is complete when its directory is real, its completion record
        exists, parses, and names the slot, and each checked binary is a
        regular file whose size equals the record (and, on POSIX, is
        executable). With ``platform_key``
        only that platform's binary is checked and it must be recorded;
        without it every recorded platform is checked. This is the same
        cheap check the generated launcher performs on every launch.
        """

        directory = self.slot_dir(name)
        if is_link_like(self.slots_dir) or is_link_like(directory):
            return "slot {} directory is a link or junction".format(name)
        if not directory.is_dir():
            return "slot {} is not installed".format(name)
        record, reason = _read_record_file(directory)
        if record is None:
            return "slot {}: {}".format(name, reason)
        if record.slot != name:
            return "slot {}: completion record names slot {}".format(name, record.slot)
        keys = [platform_key] if platform_key is not None else sorted(record.platforms)
        problem = _binaries_problem(directory, record, keys, verify_checksums=False)
        if problem is not None:
            return "slot {} ({}): {}".format(name, record.version, problem)
        return None

    def slot_is_complete(self, name: str, platform_key: str) -> bool:
        """Return whether a slot is complete for one platform key."""

        return self.slot_problem(name, platform_key) is None

    def binary_identities(
        self, name: str, platform_keys: Iterable[str]
    ) -> Dict[str, Dict[str, int]]:
        """Return the current identity of each named platform's binary.

        Raises ``SlotError`` when a slot has no valid record or a binary is
        missing or reached through a link.
        """

        directory = self.slot_dir(name)
        record = self.slot_record(name)
        if record is None:
            raise SlotError("slot {} has no valid completion record".format(name))
        identities: Dict[str, Dict[str, int]] = {}
        for key in platform_keys:
            entry = record.platforms.get(key)
            if entry is None:
                raise SlotError("slot {} has no {} binary".format(name, key))
            path, reason = _contained_regular_file(directory, entry["binary"])
            if path is None:
                raise SlotError("slot {} binary {}".format(name, reason))
            identities[key] = binary_identity(os.lstat(str(path)))
        return identities

    def write_stamp(self, name: str, identities: Mapping[str, Dict[str, int]]) -> None:
        """Record the identities of binaries maintenance just verified in full.

        Entries for platforms of another operating system are kept from an
        existing stamp so a checkout shared with WSL keeps both sides'
        stamps; every other entry is replaced.
        """

        directory = self.slot_dir(name)
        record = self.slot_record(name)
        if record is None:
            raise SlotError("slot {} has no valid completion record".format(name))
        family = local_os_family()
        entries = {
            key: value
            for key, value in read_verification_stamp(directory).items()
            if key in record.platforms and key.split("-", 1)[0] != family
        }
        entries.update({key: dict(value) for key, value in identities.items()})
        payload = {"schema": VERIFICATION_STAMP_SCHEMA, "binaries": entries}
        atomic_write_text(
            directory / VERIFICATION_STAMP_NAME,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def remove_stamp(self, name: str) -> bool:
        """Delete a slot's verification stamp so no launcher runs the slot.

        Returns True when a stamp was removed. Failures are left to the
        caller's next verification, which finds the slot unverified again.
        """

        path = self.slot_dir(name) / VERIFICATION_STAMP_NAME
        if not _lexists(path):
            return False
        try:
            status = os.lstat(str(path))
            if _status_is_link_like(status):
                _remove_link(path, status)
            else:
                _unlink_file(path)
        except OSError:
            return False
        return True

    def stamp_problem(self, name: str, platform_key: str) -> Optional[str]:
        """Return why a binary does not match its stamp, or None when it does.

        This is the launcher's stamp check (``launcher_core.check_slot``),
        used by inspect and selftest.
        """

        directory = self.slot_dir(name)
        record = self.slot_record(name)
        if record is None or platform_key not in record.platforms:
            return "slot {} has no {} binary".format(name, platform_key)
        entry = read_verification_stamp(directory).get(platform_key)
        if entry is None:
            return "slot {} has no verification stamp for {}".format(name, platform_key)
        try:
            current = self.binary_identities(name, [platform_key])[platform_key]
        except (OSError, SlotError) as exc:
            return str(exc)
        if any(entry.get(field) != value for field, value in current.items()):
            return (
                "slot {} {} binary changed after claude-wrangler last verified "
                "it".format(name, platform_key)
            )
        return None

    def active_slot(self) -> Optional[str]:
        """Return the active slot, or ``None`` when no pointer exists yet.

        Raises ``PointerError`` when the pointer is malformed or names a slot
        that is not complete, because maintenance must not guess which slot
        is in service; ``--repair-install`` resolves that state.
        """

        name = self.read_pointer()
        if name is None:
            return None
        problem = self.slot_problem(name)
        if problem is not None:
            raise PointerError(
                "Active-slot pointer names an incomplete slot ({}). Run "
                "`claude-wrangler --repair-install`.".format(problem)
            )
        return name

    def inactive_slot(self) -> str:
        """Return the slot that maintenance may prepare: the non-active one."""

        return "b" if self.active_slot() == "a" else "a"

    def new_staging_dir(self) -> Path:
        """Create and return an empty, transaction-unique staging directory.

        Staging names are short random identifiers rather than timestamped
        transaction ids: the candidate binary runs from here for its smoke
        test, and on Windows a program path over 260 characters may fail to
        start, so every character saved helps deeply nested checkouts.
        """

        self.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(exist_ok=True)
        self.validate()
        path = self.staging_dir / uuid.uuid4().hex[:12]
        path.mkdir()
        return path

    def _pointer_value_or_none(self) -> Optional[str]:
        """Return the pointer's slot, treating a malformed pointer as ``None``."""

        try:
            return self.read_pointer()
        except PointerError:
            return None

    def _write_pointer(self, name: str) -> None:
        """Atomically replace ``active-slot``; this is the commit point.

        A link or junction at the pointer path is never valid; it is removed
        (not followed) so repair can replace it with a regular file. A
        directory there is refused rather than deleted.
        """

        path = self.pointer_path
        if is_link_like(path):
            _remove_link(path, os.lstat(str(path)))
        elif path.is_dir():
            raise PointerError(
                "Active-slot pointer path is a directory; refusing to replace "
                "it: {}".format(path)
            )
        atomic_write_bytes(path, (name + "\n").encode("ascii"))

    def _require_candidate(self, candidate: Path) -> Path:
        """Return a candidate path after proving it is a real staging child."""

        candidate = Path(os.path.abspath(str(candidate)))
        if not _same_path(candidate.parent, self.staging_dir):
            raise PromotionError(
                "Candidate must be a direct child of {}: {}".format(
                    self.staging_dir, candidate
                )
            )
        if is_link_like(candidate) or not candidate.is_dir():
            raise PromotionError(
                "Candidate is not a real directory: {}".format(candidate)
            )
        return candidate

    def _candidate_problem(
        self, candidate: Path, record: CompletionRecord, verify_checksums: bool
    ) -> Optional[str]:
        """Return why a candidate does not match its record, or ``None``."""

        problem = _binaries_problem(
            candidate,
            record,
            sorted(record.platforms),
            verify_checksums,
            require_executable=False,
        )
        if problem is not None:
            return problem
        for name in EVIDENCE_FILE_NAMES:
            path, reason = _contained_regular_file(candidate, name)
            if path is None:
                return "retained evidence " + reason
        manifest_path = candidate / MANIFEST_NAME
        try:
            manifest_digest = _sha256_file(manifest_path)
        except OSError as exc:
            return "cannot hash {}: {}".format(MANIFEST_NAME, exc)
        if manifest_digest != record.manifest_sha256:
            return "{} SHA-256 {} does not match record {}".format(
                MANIFEST_NAME, manifest_digest, record.manifest_sha256
            )
        return None

    def _restore_after_failure(
        self, candidate: Path, target: Path, retired: Optional[Path]
    ) -> List[str]:
        """Undo a pre-commit promotion from observed filesystem state.

        The candidate path disappears only through this transaction's rename,
        so a missing candidate with a present target means the target holds
        the candidate and must move back before the retired slot returns.
        """

        errors: List[str] = []
        if not _lexists(candidate) and _lexists(target):
            try:
                retry_on_windows_lock(lambda: os.rename(str(target), str(candidate)))
            except (OSError, KeyboardInterrupt) as exc:
                errors.append(
                    "moving candidate back from {} failed: {}".format(target, exc)
                )
        if retired is not None and _lexists(retired) and not _lexists(target):
            try:
                retry_on_windows_lock(lambda: os.rename(str(retired), str(target)))
            except (OSError, KeyboardInterrupt) as exc:
                errors.append(
                    "restoring retired slot {} failed: {}".format(retired, exc)
                )
        fsync_directory(self.slots_dir)
        return errors

    def promote(
        self,
        candidate: Path,
        record: CompletionRecord,
        *,
        ensure_slot_free: Callable[[Path], None],
        allow_invalid_pointer: bool = False,
        verify_checksums: bool = True,
        stamp_keys: Optional[Iterable[str]] = None,
    ) -> str:
        """Make a validated staging candidate the active slot.

        ``record.slot`` names the fixed slot to fill and must not be the slot
        the pointer currently names. Steps, in order:

        1. validate the candidate against the record (binary sizes and, by
           default, SHA-256; retained manifest evidence and its hash);
        2. if the target slot is occupied, call ``ensure_slot_free`` with its
           path (the caller stops the background service and raises when a
           process still runs from it);
        3. mark binaries executable (POSIX), write the completion record
           into the candidate, and flush it;
        4. move an occupied target slot to ``.retired/<txid>``;
        5. rename the candidate to ``slots/<slot>``;
        6. re-check the moved record and binary sizes, then write the
           verification stamp for ``stamp_keys`` (by default every recorded
           platform of this host's operating system);
        7. replace ``active-slot`` (the commit point).

        A failure before step 7 restores the retired slot, leaves the pointer
        and the formerly active slot untouched, and raises
        ``PromotionError``; a refusal from ``ensure_slot_free`` propagates
        before anything changes. The formerly active slot is never moved or
        deleted. ``allow_invalid_pointer`` lets repair proceed when the
        pointer is malformed or names an incomplete slot. Returns the newly
        active slot name. The retired slot stays in ``.retired/`` for
        ``cleanup_staging_and_retired``.
        """

        self.validate()
        candidate = self._require_candidate(candidate)
        record = record.validated()
        target_name = record.slot
        target = self.slot_dir(target_name)
        family = local_os_family()
        keys_to_stamp = (
            sorted(key for key in record.platforms if key.split("-", 1)[0] == family)
            if stamp_keys is None
            else sorted(stamp_keys)
        )

        try:
            current = self.read_pointer()
        except PointerError:
            if not allow_invalid_pointer:
                raise
            current = None
        if current == target_name:
            raise PromotionError(
                "Refusing to promote into slot {} because the active-slot pointer "
                "names it; prepare the inactive slot instead.".format(target_name)
            )
        if current is not None and not allow_invalid_pointer:
            problem = self.slot_problem(current)
            if problem is not None:
                raise PointerError(
                    "Active-slot pointer names an incomplete slot ({}). Run "
                    "`claude-wrangler --repair-install`.".format(problem)
                )

        problem = self._candidate_problem(candidate, record, verify_checksums)
        if problem is not None:
            raise PromotionError(
                "Candidate {} failed validation: {}".format(candidate, problem)
            )

        occupied = _lexists(target)
        if occupied:
            if is_link_like(target) or not target.is_dir():
                raise PromotionError(
                    "Slot {} is not a real directory: {}".format(target_name, target)
                )
            ensure_slot_free(target)

        try:
            _make_binaries_executable(candidate, record)
            atomic_write_text(candidate / COMPLETION_RECORD_NAME, record.to_json())
            _flush_tree(candidate)
        except OSError as exc:
            raise PromotionError(
                "Could not finalize candidate {}: {}".format(candidate, exc)
            ) from exc

        self.slots_dir.mkdir(exist_ok=True)
        retired: Optional[Path] = None
        try:
            if occupied:
                self.retired_dir.mkdir(exist_ok=True)
                self.validate()
                retired = self.retired_dir / _new_txid(target_name)
                retired_path = retired
                retry_on_windows_lock(lambda: os.rename(str(target), str(retired_path)))
                fsync_directory(self.retired_dir)
            retry_on_windows_lock(lambda: os.rename(str(candidate), str(target)))
            fsync_directory(self.slots_dir)
            fsync_directory(self.staging_dir)
            moved_problem = self.slot_problem(target_name)
            if moved_problem is None:
                moved = self.slot_record(target_name)
                if moved is None or moved.to_json() != record.to_json():
                    moved_problem = "moved completion record differs from the record"
            if moved_problem is not None:
                raise PromotionError(
                    "Slot {} failed re-validation after the move: {}".format(
                        target_name, moved_problem
                    )
                )
            # The binaries were hashed while staged and nothing else writes
            # under the lock, so their identity now is the verified one.
            self.write_stamp(
                target_name, self.binary_identities(target_name, keys_to_stamp)
            )
            self._write_pointer(target_name)
        except BaseException as exc:
            if self._pointer_value_or_none() == target_name:
                # The replace landed even though an error surfaced afterwards;
                # the verified slot is live, so rolling back would be wrong.
                message = (
                    "The active-slot pointer now selects verified slot {} "
                    "(version {}), but promotion then reported: {}".format(
                        target_name, record.version, str(exc) or type(exc).__name__
                    )
                )
                if isinstance(exc, Exception):
                    raise PointerCommittedError(message) from exc
                if isinstance(exc, KeyboardInterrupt):
                    raise PointerCommittedInterrupt(message) from exc
                raise
            errors = self._restore_after_failure(candidate, target, retired)
            if isinstance(exc, KeyboardInterrupt) and not errors:
                raise
            detail = (
                " Restoration also failed: {}.".format("; ".join(errors))
                if errors
                else " The previous runtime and pointer are unchanged."
            )
            raise PromotionError(
                "Promotion into slot {} failed before the commit point: {}.{}".format(
                    target_name, str(exc) or type(exc).__name__, detail
                )
            ) from exc
        return target_name

    def set_active(self, name: str) -> None:
        """Point ``active-slot`` at a complete slot (rollback and repair).

        The caller must already have run the in-use check and any full
        offline re-verification the operation requires.
        """

        self.validate()
        problem = self.slot_problem(name)
        if problem is not None:
            raise SlotError(
                "Refusing to activate an incomplete slot: {}".format(problem)
            )
        self._write_pointer(name)

    def cleanup_staging_and_retired(
        self,
        in_use: Callable[[Path], bool],
        *,
        on_error: Optional[Callable[[Path, BaseException], None]] = None,
    ) -> List[str]:
        """Delete staging candidates and retired slots that nothing runs from.

        Each direct child of ``.staging/`` and ``.retired/`` is passed to
        ``in_use``; entries reported in use are left for a later repair.
        Removal never follows links. An entry whose removal fails is left
        partly deleted, reported to ``on_error`` when given, and skipped.
        Returns the removed entries as root-relative POSIX paths. Fixed
        slots and the pointer are never touched.
        """

        self.validate()
        removed: List[str] = []
        for container in (self.staging_dir, self.retired_dir):
            if not container.is_dir():
                continue
            with os.scandir(str(container)) as iterator:
                entries = sorted(Path(entry.path) for entry in iterator)
            for entry in entries:
                if in_use(entry):
                    continue
                try:
                    remove_tree(entry)
                except OSError as exc:
                    if on_error is not None:
                        on_error(entry, exc)
                    continue
                removed.append("{}/{}".format(container.name, entry.name))
        return removed


class MaintenanceLock:
    """Exclusive, non-blocking maintenance lock on ``<root>/.lock``.

    Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` elsewhere. The
    lock file is created when absent and deleted only by uninstall, through
    ``release(delete=True)``. On POSIX ``acquire`` also proves that the path
    still names the inode it locked, so a maintainer racing that deletion
    never proceeds on an orphaned inode. A second maintainer, in this
    process or another, fails fast with ``MaintenanceLockError`` and changes
    nothing; a filesystem that cannot lock at all is reported as such.
    Launchers never take this lock.
    """

    def __init__(self, root: Path, *, project_root: Optional[Path] = None) -> None:
        """Prepare a lock for one runtime root; nothing is opened yet."""

        self.root = Path(os.path.abspath(str(root)))
        self.project_root = (
            Path(os.path.abspath(str(project_root)))
            if project_root is not None
            else None
        )
        self.path = self.root / LOCK_NAME
        self._descriptor: Optional[int] = None

    @property
    def held(self) -> bool:
        """Return whether this instance currently holds the lock."""

        return self._descriptor is not None

    def acquire(self) -> None:
        """Take the lock or raise ``MaintenanceLockError`` immediately."""

        if self._descriptor is not None:
            raise MaintenanceLockError(
                "Maintenance lock is already held by this instance: {}".format(
                    self.path
                )
            )
        try:
            validate_runtime_root(self.root, self.project_root)
            self.root.mkdir(parents=True, exist_ok=True)
            # Re-check after creation so a link swapped in meanwhile is
            # refused before the lock file is opened through it.
            validate_runtime_root(self.root, self.project_root)
        except (UnsafePathError, OSError) as exc:
            raise MaintenanceLockError(
                "Cannot take the maintenance lock under {}: {}".format(self.root, exc)
            ) from exc
        try:
            status: Optional[os.stat_result] = os.lstat(str(self.path))
        except FileNotFoundError:
            status = None
        except OSError as exc:
            raise MaintenanceLockError(
                "Cannot inspect maintenance lock {}: {}".format(self.path, exc)
            ) from exc
        if status is not None and (
            _status_is_link_like(status) or not stat.S_ISREG(status.st_mode)
        ):
            raise MaintenanceLockError(
                "Maintenance lock must be a regular file: {}".format(self.path)
            )
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.path), flags, 0o600)
        except OSError as exc:
            raise MaintenanceLockError(
                "Cannot open maintenance lock {}: {}".format(self.path, exc)
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise MaintenanceLockError(
                    "Maintenance lock must be a regular file: {}".format(self.path)
                )
            _lock_descriptor(descriptor)
        except MaintenanceLockError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                # ENOLCK, EINVAL, ENOTSUP and similar mean the filesystem
                # cannot lock at all; waiting would never help.
                raise MaintenanceLockUnsupportedError(
                    "Cannot lock {} ({}): this filesystem does not support the "
                    "file locks claude-wrangler needs. Keep the checkout on a "
                    "local filesystem or a lock-capable mount.".format(
                        self.path,
                        "errno {} {}".format(
                            exc.errno,
                            exc.strerror or errno.errorcode.get(exc.errno, ""),
                        ).strip(),
                    ),
                    created_lock_file=status is None,
                ) from exc
            raise MaintenanceLockError(
                "Another claude-wrangler maintenance operation is already running "
                "for {} (lock: {}). Wait for it to finish, then retry.".format(
                    self.root, self.path
                )
            ) from exc
        if os.name != "nt" and not _descriptor_is_path(descriptor, self.path):
            # Another maintainer's uninstall deleted the lock file between our
            # open and lock; a lock on that orphaned inode excludes nobody.
            _unlock_and_close(descriptor)
            raise MaintenanceLockError(
                "The maintenance lock {} was replaced while it was being taken; "
                "another claude-wrangler run is changing this project. Retry once "
                "it has finished.".format(self.path)
            )
        self._descriptor = descriptor

    def release(self, *, delete: bool = False) -> None:
        """Release and close the lock; with ``delete`` remove the lock file too.

        Only uninstall deletes the lock file. On POSIX the file is unlinked
        while the lock is still held, and only when the path still names
        the locked inode, so a lock another maintainer holds is never
        removed. Windows refuses to delete a file another process has open,
        so there the file is deleted once, without retrying, after closing.
        """

        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        if delete and os.name != "nt" and _descriptor_is_path(descriptor, self.path):
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
        _unlock_and_close(descriptor)
        if delete and os.name == "nt":
            try:
                os.unlink(str(self.path))
            except OSError:
                pass

    def __enter__(self) -> "MaintenanceLock":
        """Acquire the lock for a ``with`` block."""

        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Release the lock when the ``with`` block ends."""

        self.release()


def _lock_descriptor(descriptor: int) -> None:
    """Take a non-blocking exclusive OS lock on byte 0 of a descriptor."""

    if os.name == "nt":
        import msvcrt

        # Locking a byte beyond end-of-file is permitted on Windows, so the
        # lock file can stay empty.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    """Release the OS lock taken by ``_lock_descriptor``."""

    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _unlock_and_close(descriptor: int) -> None:
    """Release the OS lock on a descriptor, then close it."""

    try:
        _unlock_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _descriptor_is_path(descriptor: int, path: Path) -> bool:
    """Return whether ``path`` still names the file open as ``descriptor``.

    Used on POSIX only: st_dev and st_ino identify the locked inode.
    """

    try:
        opened = os.fstat(descriptor)
        current = os.lstat(str(path))
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


_kernel32_bundle: Optional[Tuple[Any, Any]] = None


def _windows_kernel32() -> Tuple[Any, Any]:
    """Return a private, prototyped kernel32 handle and PROCESSENTRY32W type.

    A private ``WinDLL`` instance keeps these prototypes from leaking into
    other users of ``ctypes.windll.kernel32``.
    """

    global _kernel32_bundle
    if _kernel32_bundle is not None:
        return _kernel32_bundle
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        """The Toolhelp32 process record (tlhelp32.h)."""

        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32_bundle = (kernel32, PROCESSENTRY32W)
    return _kernel32_bundle


def _windows_process_images() -> List[RunningProcess]:
    """Enumerate process images with Toolhelp32 and QueryFullProcessImageNameW.

    Processes that cannot be opened with PROCESS_QUERY_LIMITED_INFORMATION
    (services and other users' processes, when not elevated) are skipped.
    Windows lets a directory holding a running image be renamed but refuses
    to delete or overwrite the image, so a missed process keeps running from
    ``.retired/`` and only makes that entry's later deletion fail.
    """

    import ctypes
    from ctypes import wintypes

    kernel32, process_entry_type = _windows_kernel32()
    th32cs_snapprocess = 0x00000002
    process_query_limited_information = 0x1000
    error_no_more_files = 18
    invalid_handle = ctypes.c_void_p(-1).value

    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if not snapshot or snapshot == invalid_handle:
        raise ProcessQueryError(
            "CreateToolhelp32Snapshot failed: {}".format(
                ctypes.WinError(ctypes.get_last_error())
            )
        )
    pids: List[int] = []
    try:
        entry = process_entry_type()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pids.append(int(entry.th32ProcessID))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error not in (0, error_no_more_files):
            raise ProcessQueryError(
                "Process enumeration stopped early: {}".format(ctypes.WinError(error))
            )
    finally:
        kernel32.CloseHandle(snapshot)

    results: List[RunningProcess] = []
    buffer = ctypes.create_unicode_buffer(32768)
    for pid in pids:
        if pid in (0, 4):
            # System Idle Process and System have no image path.
            continue
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                results.append(RunningProcess(pid, buffer.value[: size.value]))
        finally:
            kernel32.CloseHandle(handle)
    return results


def _proc_process_images(
    proc_root: Path = Path("/proc"),
    readlink: Optional[Callable[[str], str]] = None,
) -> List[RunningProcess]:
    """Enumerate process images through ``/proc/<pid>/exe`` on Linux.

    Without privileges, other users' processes cannot be read and are
    skipped, as are processes that exit during the scan. ``readlink`` is
    injectable for tests.
    """

    read = readlink or os.readlink
    try:
        names = os.listdir(str(proc_root))
    except OSError as exc:
        raise ProcessQueryError(
            "Cannot list {} to find running processes: {}".format(proc_root, exc)
        ) from exc
    results: List[RunningProcess] = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            image = read(str(proc_root / name / "exe"))
        except OSError:
            continue
        results.append(RunningProcess(int(name), image))
    return results


# PROC_PIDPATHINFO_MAXSIZE from <sys/proc_info.h> (4 * MAXPATHLEN).
_DARWIN_PIDPATH_MAX = 4 * 1024


def _load_libproc() -> Any:
    """Return macOS ``libproc`` through ``ctypes`` with prototypes set."""

    import ctypes

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.proc_listallpids.restype = ctypes.c_int
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    return library


def _darwin_process_images(libproc: Any = None) -> List[RunningProcess]:
    """Enumerate process images with ``proc_pidpath`` from macOS ``libproc``.

    ``proc_pidpath`` returns the path of the executable's vnode as the
    kernel sees it now: physical (no symbolic links) and current after a
    directory rename, so a process running from a slot moved to
    ``.retired/`` is still found there. Processes that cannot be queried
    (other users' processes without privileges, or processes that just
    exited) are skipped. A process list that cannot be read raises
    ``ProcessQueryError`` so callers fail closed.
    """

    import ctypes

    library = libproc if libproc is not None else _load_libproc()
    count = library.proc_listallpids(None, 0)
    if count <= 0:
        raise ProcessQueryError(
            "proc_listallpids failed: errno {}".format(ctypes.get_errno())
        )
    # Leave room for processes started between the two calls.
    capacity = count + 256
    pids = (ctypes.c_int * capacity)()
    count = library.proc_listallpids(pids, ctypes.sizeof(pids))
    if count <= 0:
        raise ProcessQueryError(
            "proc_listallpids failed: errno {}".format(ctypes.get_errno())
        )
    buffer = ctypes.create_string_buffer(_DARWIN_PIDPATH_MAX)
    results: List[RunningProcess] = []
    for pid in list(pids)[: min(count, capacity)]:
        if pid <= 0:
            continue
        length = library.proc_pidpath(pid, buffer, _DARWIN_PIDPATH_MAX)
        if length <= 0:
            continue
        results.append(RunningProcess(int(pid), os.fsdecode(buffer.raw[:length])))
    return results


def _run_ps() -> "subprocess.CompletedProcess[bytes]":
    """Run ``ps -axo pid=,comm=`` with a timeout and captured output."""

    return subprocess.run(
        ["ps", "-axo", "pid=,comm="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )


def _ps_process_images(
    runner: Optional[Callable[[], "subprocess.CompletedProcess[bytes]"]] = None,
) -> List[RunningProcess]:
    """Enumerate process images with ``ps -axo pid=,comm=`` (BSD fallback).

    Used where neither ``/proc`` nor macOS ``libproc`` is available. On
    macOS and the BSDs ``comm`` is the process's ``argv[0]`` as it was
    exec'd (a login shell shows ``-zsh``) or, for another user's process,
    only the short command name, so it is spelled however the caller
    spelled it: a symbolic link, a relative path, or a bare name found on
    ``PATH``. ``processes_under`` also compares the resolved spelling of
    absolute images, but a relative or rewritten ``argv[0]`` cannot be
    matched; that is the documented limitation of this fallback. ``runner``
    is injectable for tests.
    """

    try:
        completed = (runner or _run_ps)()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessQueryError("Cannot run ps: {}".format(exc)) from exc
    if completed.returncode != 0:
        raise ProcessQueryError(
            "ps exited with {}: {}".format(
                completed.returncode,
                completed.stderr.decode("utf-8", "replace").strip(),
            )
        )
    results: List[RunningProcess] = []
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        results.append(RunningProcess(int(parts[0]), parts[1].strip()))
    return results


def list_process_images() -> List[RunningProcess]:
    """Return every running process whose image path can be read.

    Raises ``ProcessQueryError`` when the process table itself cannot be
    read, so callers fail closed instead of assuming nothing is running.
    """

    if os.name == "nt":
        return _windows_process_images()
    if sys.platform.startswith("linux"):
        return _proc_process_images()
    if sys.platform == "darwin":
        try:
            library = _load_libproc()
        except (OSError, AttributeError):
            # No usable libproc: fall back to ps rather than refusing every
            # maintenance operation.
            return _ps_process_images()
        return _darwin_process_images(library)
    return _ps_process_images()


def _windows_long_path(path: str) -> Optional[str]:
    """Return the long-name spelling of an existing Windows path, or None.

    A process started through an 8.3 short path (``LONG-P~1``) reports its
    image in that form; ``GetLongPathNameW`` expands every component.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        function = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(path, buffer, len(buffer))
    except (AttributeError, OSError, ValueError):
        return None
    if not length or length >= len(buffer):
        return None
    return buffer.value


def _comparable_path(path: str) -> str:
    """Normalize a path for prefix comparison on this platform."""

    if path.startswith("\\\\?\\"):
        path = path[4:]
    normalized = os.path.normcase(os.path.normpath(path))
    if sys.platform == "darwin":
        # APFS and HFS+ default to case-insensitive names; folding errs
        # toward reporting a process as in use, which is the safe side.
        normalized = normalized.casefold()
    return normalized


def _image_spellings(image: str) -> List[str]:
    """Return the comparable spellings of one process image path.

    The image as reported always counts. On Windows an image with an 8.3
    short component (``~``) is also expanded to its long name; elsewhere an
    absolute image is also resolved, because ``ps`` reports ``argv[0]``,
    which may run through a symbolic link that the resolved runtime root
    never contains.
    """

    spellings = [_comparable_path(image)]
    if os.name == "nt":
        if "~" in image:
            expanded = _windows_long_path(image)
            if expanded is None:
                try:
                    expanded = os.path.realpath(image)
                except (OSError, ValueError):
                    expanded = None
            if expanded:
                spellings.append(_comparable_path(expanded))
    elif image.startswith("/"):
        try:
            spellings.append(_comparable_path(os.path.realpath(image)))
        except (OSError, ValueError):
            pass
    return spellings


def _root_prefixes(root: Path) -> Tuple[str, ...]:
    """Return the comparable spellings of ``root`` with a trailing separator."""

    spellings = {os.path.abspath(str(root)), os.path.realpath(str(root))}
    prefixes = []
    for spelling in sorted(spellings):
        comparable = _comparable_path(spelling)
        if not comparable.endswith(os.sep):
            comparable += os.sep
        prefixes.append(comparable)
    return tuple(prefixes)


def processes_under(
    root: Path, processes: Iterable[RunningProcess]
) -> List[RunningProcess]:
    """Return the processes whose image lies strictly inside ``root``.

    Paths are compared after normalization (case-insensitively on Windows
    and macOS) with a trailing-separator boundary, so ``slots/b`` never
    matches ``slots/bb``. Linux's `` (deleted)`` suffix is ignored so a
    process running a replaced binary still counts. Each image is compared
    in every spelling ``_image_spellings`` gives (8.3 names expanded on
    Windows, symbolic links resolved elsewhere).
    """

    prefixes = _root_prefixes(root)
    matches = []
    for process in processes:
        image = process.image
        if image.endswith(" (deleted)"):
            image = image[: -len(" (deleted)")]
        if any(
            comparable.startswith(prefix)
            for comparable in _image_spellings(image)
            for prefix in prefixes
        ):
            matches.append(process)
    return sorted(matches, key=lambda process: process.pid)


def find_processes_under(root: Path) -> List[RunningProcess]:
    """Return running processes whose executable image lives under ``root``.

    Windows uses Toolhelp32 and ``QueryFullProcessImageNameW`` through
    ``ctypes``; Linux reads ``/proc/<pid>/exe``; macOS asks ``libproc``'s
    ``proc_pidpath`` (falling back to ``ps -axo pid=,comm=`` only when
    ``libproc`` cannot be loaded); other systems parse ``ps``. See the
    enumerators for each platform's blind spots.
    """

    return processes_under(root, list_process_images())


def require_no_processes_under(
    path: Path,
    *,
    lister: Optional[Callable[[Path], List[RunningProcess]]] = None,
) -> None:
    """Raise ``SlotInUseError`` naming every process that runs from ``path``."""

    running = (lister or find_processes_under)(path)
    if running:
        raise SlotInUseError(path, running)


def make_slot_free_check(
    stop_service: Optional[Callable[[], object]] = None,
    *,
    lister: Optional[Callable[[Path], List[RunningProcess]]] = None,
) -> Callable[[Path], None]:
    """Build an ``ensure_slot_free`` callable for ``RuntimeStore.promote``.

    The returned callable first runs ``stop_service`` (typically a closure
    over ``stop_background_service``) and then refuses with
    ``SlotInUseError`` if any process still runs from the slot.
    """

    def ensure_slot_free(slot_path: Path) -> None:
        """Stop the background service, then prove the slot is idle."""

        if stop_service is not None:
            stop_service()
        require_no_processes_under(slot_path, lister=lister)

    return ensure_slot_free


@dataclass(frozen=True)
class ServiceStopResult:
    """Outcome of one ``claude daemon stop --any`` attempt."""

    command: Tuple[str, ...]
    returncode: Optional[int]
    output: str

    @property
    def ok(self) -> bool:
        """Return whether the stop command ran and exited zero."""

        return self.returncode == 0


def stop_background_service(
    binary: Path,
    env: Mapping[str, str],
    timeout_seconds: float = DEFAULT_SERVICE_STOP_TIMEOUT_SECONDS,
) -> ServiceStopResult:
    """Ask the project's Claude Code background service to stop.

    Runs ``<binary> daemon stop --any`` with exactly ``env`` (the caller's
    launcher environment, so the project's config dir and home are used),
    stdin closed, and output captured. A non-zero exit or a binary that
    cannot start is reported on stderr and returned, never raised, because
    the in-use check that follows is the real safety gate. Exceeding
    ``timeout_seconds`` kills the command and raises ``ServiceStopError``.
    Output goes to a temporary file rather than a pipe so a detached
    grandchild that inherits the handle cannot stall the wait.
    """

    command = (str(binary), "daemon", "stop", "--any")
    with tempfile.TemporaryFile() as output_file:
        try:
            process = subprocess.Popen(
                list(command),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=output_file,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            result = ServiceStopResult(command, None, str(exc))
            _report_stop_failure(result)
            return result
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise ServiceStopError(
                "`{} daemon stop --any` did not finish within {} seconds.".format(
                    binary, timeout_seconds
                )
            ) from exc
        output_file.seek(0)
        output = output_file.read().decode("utf-8", "replace")
    result = ServiceStopResult(command, returncode, output)
    if not result.ok:
        _report_stop_failure(result)
    return result


def _report_stop_failure(result: ServiceStopResult) -> None:
    """Print a one-line warning about an unsuccessful service stop."""

    detail = " ".join(result.output.split())
    if len(detail) > 400:
        detail = detail[:400] + "..."
    status = (
        "could not start"
        if result.returncode is None
        else "exited {}".format(result.returncode)
    )
    print(
        "[claude-wrangler] Warning: background service stop {}: {}".format(
            status, detail or "(no output)"
        ),
        file=sys.stderr,
        flush=True,
    )
