"""Discovery of npm-era layouts and the pieces of their migration.

Implements the legacy half of ``legacy_npm_layout_migration.md``:

* read-only discovery of a ``.local/claude/npm`` (or custom) layout from its
  schema-1 metadata, then ``package-lock.json``, then ``package.json``, then
  the installed wrapper package (requirement 2);
* the rolling-forward rule that decides whether the legacy layout or the
  native runtime is authoritative (requirement 9);
* adopt-in-place: hashing the legacy platform binary against the signed
  manifest and copying it into a staging candidate (requirement 4);
* the schema-1 compatibility projection with a ``migrated_to`` pointer
  (requirements 5.6 and 8); and
* cleanup of the npm tree, renaming locked entries to ``.retired-<txid>``
  or leaving and reporting them for the next install, upgrade, or repair
  (requirement 5.7).

The migration transaction itself is driven by ``operations.py`` while it
holds the maintenance lock. Nothing in this module that discovers state
ever writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .atomicio import retry_on_windows_lock
from .constants import (
    CLAUDE_PACKAGE_NAME,
    LEGACY_SCHEMA_VERSION,
    METADATA_FILENAME,
    SCHEMA_VERSION,
    SCRIPT_MARKER,
    SCRIPT_NAME,
    SCRIPT_VERSION,
)
from .distribution import (
    DistributionError,
    is_exact_version,
    normalize_channel,
)
from .layout import (
    parse_available_versions,
    parse_recorded_paths,
    read_json_file,
    resolve_relative_within_root,
)
from .models import ClaudeWranglerError, Config, LegacyState
from .platforms import PayloadError, PlatformError, binary_filename
from .slots import is_link_like, remove_tree

# Entries an npm install leaves in the legacy directory. The first three
# make it a runnable npm runtime; `.npm-cache` alone is only residue.
LEGACY_NPM_ARTIFACTS = (
    "node_modules",
    "package.json",
    "package-lock.json",
    ".npm-cache",
)
LEGACY_RUNTIME_ARTIFACTS = ("node_modules", "package.json", "package-lock.json")
LEGACY_RETIRED_PREFIX = ".retired-"
# The name npm-era claude-wrangler gave the package.json it managed.
LEGACY_PACKAGE_NAME = "claude-local-managed-install"
NPM_SCOPE = "@anthropic-ai"
WRAPPER_PACKAGE_DIR = "claude-code"
_HASH_CHUNK_BYTES = 1024 * 1024
# npm-era generated bash launcher lines naming repo-relative paths, keyed
# like recorded metadata `paths`; each value is `"$repo_root/<path>"`
# (requirement 2's custom-directory discovery, requirement 7's spelling).
_NPM_LAUNCHER_PATH_RES = {
    "local_dir": re.compile(
        r'^local_prefix="\$repo_root/([^"$`\r\n]+)"\r?$', re.MULTILINE
    ),
    "claude_config_dir": re.compile(
        r'^export CLAUDE_CONFIG_DIR="\$repo_root/([^"$`\r\n]+)"\r?$', re.MULTILINE
    ),
    "claude_home_dir": re.compile(
        r'^\s*export HOME="\$repo_root/([^"$`\r\n]+)"\r?$', re.MULTILINE
    ),
}
_NPM_LAUNCHER_BINARY = "node_modules/.bin/claude"
_NODE_MODULES_SEGMENT = "/node_modules/"
_MAX_LAUNCHER_BYTES = 256 * 1024


class MigrationError(ClaudeWranglerError):
    """Raised when a legacy layout cannot be migrated safely."""


def _lexists(path: Path) -> bool:
    """Return whether a directory entry exists, including a dangling link."""

    return os.path.lexists(str(path))


def _read_metadata(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read legacy metadata, returning `(payload, error)` and never raising."""

    try:
        return read_json_file(path), None
    except ClaudeWranglerError as exc:
        return None, str(exc)


def _managed_package_json(path: Path) -> bool:
    """Return True for the package.json an npm-era claude-wrangler wrote.

    That file is named `claude-local-managed-install` and depends on
    nothing but Claude Code's npm package; an unrelated project's
    package.json is never mistaken for it.
    """

    try:
        payload = read_json_file(path)
    except ClaudeWranglerError:
        return False
    if not payload:
        return False
    if payload.get("name") == LEGACY_PACKAGE_NAME:
        return True
    names = set()
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        dependencies = payload.get(section)
        if isinstance(dependencies, dict):
            names.update(dependencies)
    return names == {CLAUDE_PACKAGE_NAME}


def has_legacy_ownership_evidence(path: Path) -> bool:
    """Return True when a directory provably belongs to npm-era claude-wrangler.

    Evidence is schema-1 metadata written by claude-wrangler (including the
    compatibility projection a migration leaves) or the managed
    package.json. A bare `package.json` or `node_modules` proves nothing:
    install cleanup and uninstall delete the npm tree of a legacy directory,
    so a directory without evidence must never count as one.
    """

    metadata, _error = _read_metadata(path / METADATA_FILENAME)
    if (
        metadata is not None
        and metadata.get("schema_version") == LEGACY_SCHEMA_VERSION
        and metadata.get("script_name") == SCRIPT_NAME
    ):
        return True
    return _managed_package_json(path / "package.json")


def looks_like_legacy_dir(path: Path) -> bool:
    """Return True when a directory holds npm-era claude-wrangler state.

    The directory must be real (not a link or junction) and carry
    ownership evidence (`has_legacy_ownership_evidence`): schema-1
    claude-wrangler metadata or the managed package.json. A schema-2
    (native) runtime root is never mistaken for a legacy directory.
    """

    try:
        if is_link_like(path) or not path.is_dir():
            return False
    except OSError:
        return False
    return has_legacy_ownership_evidence(path)


def unaliased_dir_within_root(
    project_root: Path, raw: str, label: str
) -> Optional[Tuple[str, Path]]:
    """Resolve a relative directory only when no link or junction leads to it.

    Returns ``(relative, path)`` when ``raw`` stays inside the project and
    every existing component between the project root and the directory is
    a real directory, so a link at `.local/claude/npm` can never make
    another directory (inside or outside the project) the legacy layout
    whose npm tree gets deleted. Returns None otherwise.
    """

    try:
        relative, _resolved = resolve_relative_within_root(project_root, raw, label)
    except ClaudeWranglerError:
        return None
    cursor = project_root
    for part in Path(relative).parts:
        cursor = cursor / part
        try:
            if not _lexists(cursor):
                return None
            if is_link_like(cursor):
                return None
        except OSError:
            return None
    return relative, cursor


def _read_small_regular_file(path: Path, limit: int) -> Optional[bytes]:
    """Return a regular file's bytes, or None when absent, aliased, or too big."""

    try:
        if is_link_like(path) or not path.is_file():
            return None
        with open(str(path), "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    return data if len(data) <= limit else None


def _safe_relative_dir(raw: str) -> Optional[str]:
    """Return a plain relative POSIX directory spelling, or None if unusable."""

    text = raw.strip().replace("\\", "/").strip("/")
    if not text or text.startswith("~") or ":" in text:
        return None
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return text


def npm_era_launcher_paths(launcher_path: Path) -> Dict[str, str]:
    """Return the repo-relative paths an npm-era bash launcher records.

    Keys follow recorded metadata `paths`: `local_dir` (from
    `local_prefix="$repo_root/<dir>"`), `claude_config_dir` (from
    `export CLAUDE_CONFIG_DIR="$repo_root/<dir>"`), and `claude_home_dir`
    (from the ring-1 `export HOME=...`). The launcher built
    `CLAUDE_CONFIG_DIR` from that suffix, so reusing it keeps the migrated
    launcher's value identical (requirement 7). Only a launcher carrying
    the managed marker and the npm-era binary path counts; anything else
    yields an empty mapping. Never writes.
    """

    data = _read_small_regular_file(launcher_path, _MAX_LAUNCHER_BYTES)
    if data is None:
        return {}
    text = data.decode("utf-8", "replace")
    if SCRIPT_MARKER not in text or _NPM_LAUNCHER_BINARY not in text:
        return {}
    found: Dict[str, str] = {}
    for key, pattern in _NPM_LAUNCHER_PATH_RES.items():
        match = pattern.search(text)
        if match is None:
            continue
        relative = _safe_relative_dir(match.group(1))
        if relative is not None:
            found[key] = relative
    return found


def legacy_dir_from_launcher(
    launcher_path: Path, launch_config_path: Optional[Path] = None
) -> Optional[str]:
    """Return the npm-era local dir that the project's launcher still names.

    A custom npm-era `--local-dir` is recorded only inside that directory,
    so it cannot be found from the metadata alone (requirement 2). Two
    generated files remember it:

    * an npm-era bash launcher embeds `local_prefix="$repo_root/<dir>"`
      and runs `$local_prefix/node_modules/.bin/claude`; and
    * a migration-pending `launch.json` (requirement 6) records the legacy
      binary as `<dir>/node_modules/...`.

    Only a launcher carrying the managed marker counts. Discovery never
    writes; `discover_legacy` still resolves the result inside the project
    and checks that it really holds an npm-era layout.
    """

    found = npm_era_launcher_paths(launcher_path).get("local_dir")
    if found is not None:
        return found
    if launch_config_path is None:
        return None
    raw = _read_small_regular_file(launch_config_path, _MAX_LAUNCHER_BYTES)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    legacy = payload.get("legacy") if isinstance(payload, dict) else None
    binary = legacy.get("binary") if isinstance(legacy, dict) else None
    if not isinstance(binary, str) or _NODE_MODULES_SEGMENT not in binary:
        return None
    return _safe_relative_dir(binary.split(_NODE_MODULES_SEGMENT, 1)[0])


def legacy_dir_candidates(*raw_candidates: Optional[str]) -> List[str]:
    """Return de-duplicated legacy directory spellings in priority order."""

    seen: List[str] = []
    for raw in raw_candidates:
        if not raw:
            continue
        normalized = raw.replace("\\", "/").strip().rstrip("/")
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def _package_json_selector(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return the Claude Code dependency spec from the managed package.json."""

    try:
        payload = read_json_file(path)
    except ClaudeWranglerError as exc:
        return None, str(exc)
    if not payload:
        return None, None
    for section in ("devDependencies", "dependencies"):
        dependencies = payload.get(section)
        if isinstance(dependencies, dict):
            value = dependencies.get(CLAUDE_PACKAGE_NAME)
            if isinstance(value, str) and value.strip():
                return value.strip(), None
    return None, None


def _lockfile_version(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return the Claude Code version recorded in package-lock.json."""

    try:
        payload = read_json_file(path)
    except ClaudeWranglerError as exc:
        return None, str(exc)
    if not payload:
        return None, None
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        return None, None
    entry = packages.get("node_modules/{}".format(CLAUDE_PACKAGE_NAME))
    if not isinstance(entry, dict):
        return None, None
    version = entry.get("version")
    return (version if isinstance(version, str) else None), None


def _installed_wrapper_version(legacy_dir: Path) -> Optional[str]:
    """Return the version of the installed npm wrapper package, if readable."""

    path = (
        legacy_dir / "node_modules" / NPM_SCOPE / WRAPPER_PACKAGE_DIR / "package.json"
    )
    try:
        payload = read_json_file(path)
    except ClaudeWranglerError:
        return None
    if not payload:
        return None
    version = payload.get("version")
    return version if isinstance(version, str) else None


def _clean_channel(raw: Any) -> Tuple[Optional[str], bool]:
    """Normalize a recorded legacy channel; unknown values become None."""

    if not isinstance(raw, str) or not raw.strip():
        return None, False
    try:
        return normalize_channel(raw)
    except DistributionError:
        return None, False


def read_legacy_state(
    project_root: Path, dir_relative: str, directory: Path
) -> LegacyState:
    """Read everything known about one legacy directory without writing.

    Inference order follows the npm-era tool: metadata first, then
    `package-lock.json`, then `package.json`, then the installed wrapper
    package's own `package.json`.
    """

    metadata, metadata_error = _read_metadata(directory / METADATA_FILENAME)
    if metadata is not None and metadata.get("schema_version") not in (
        None,
        LEGACY_SCHEMA_VERSION,
    ):
        metadata_error = "{} has schema_version {!r}; ignoring it".format(
            directory / METADATA_FILENAME, metadata.get("schema_version")
        )
        metadata = None

    lock_version, lockfile_error = _lockfile_version(directory / "package-lock.json")
    package_selector, package_json_error = _package_json_selector(
        directory / "package.json"
    )
    meta = metadata or {}

    selector = meta.get("claude_selector")
    selector = (
        selector.strip() if isinstance(selector, str) and selector.strip() else None
    )
    if selector is None:
        selector = package_selector

    version = meta.get("claude_version")
    version_source = "legacy metadata"
    if not (isinstance(version, str) and is_exact_version(version)):
        version = None
    if version is None and lock_version and is_exact_version(lock_version):
        version, version_source = lock_version, "legacy package-lock.json"
    if version is None and selector and is_exact_version(selector):
        version, version_source = selector, "legacy package.json"
    if version is None:
        installed = _installed_wrapper_version(directory)
        if installed and is_exact_version(installed):
            version, version_source = installed, "legacy installed npm package"
    if version is None:
        version_source = "legacy npm layout (version unknown)"

    raw_channel = meta.get("claude_channel")
    if raw_channel is None and selector in ("stable", "latest", "next"):
        raw_channel = selector
    channel, deprecated = _clean_channel(raw_channel)
    if selector is not None and selector.lower() in ("stable", "next"):
        # npm dist-tag selectors name a channel, not a version selector.
        selector = "latest"
    if selector is not None and selector != "latest" and not is_exact_version(selector):
        selector = None

    migrated_to = meta.get("migrated_to")
    generated_at = meta.get("generated_at")
    try:
        names = sorted(os.listdir(str(directory)))
    except OSError:
        names = []

    def bool_field(key: str) -> Optional[bool]:
        """Return a boolean metadata field or None."""

        value = meta.get(key)
        return value if isinstance(value, bool) else None

    base_url = meta.get("release_base_url")
    return LegacyState(
        dir_relative=dir_relative,
        dir=directory,
        metadata=metadata,
        metadata_error=metadata_error,
        lockfile_error=lockfile_error,
        package_json_error=package_json_error,
        selector=selector,
        channel=channel,
        channel_deprecated=deprecated,
        version=version,
        version_source=version_source,
        shared_home=bool_field("shared_home"),
        reasonable_permissions_enabled=bool_field("reasonable_permissions_enabled"),
        update_checks=bool_field("update_checks"),
        windows_sandbox_warning=bool_field("windows_sandbox_warning"),
        release_base_url=base_url if isinstance(base_url, str) and base_url else None,
        generated_at=generated_at if isinstance(generated_at, str) else None,
        recorded_paths=parse_recorded_paths(meta.get("paths")),
        available_versions=parse_available_versions(meta.get("available_versions")),
        available_versions_updated_at=(
            meta.get("available_versions_updated_at")
            if isinstance(meta.get("available_versions_updated_at"), str)
            else None
        ),
        migrated_to=migrated_to if isinstance(migrated_to, dict) else None,
        npm_artifacts=[
            name for name in LEGACY_NPM_ARTIFACTS if _lexists(directory / name)
        ],
        retired_entries=[
            name for name in names if name.startswith(LEGACY_RETIRED_PREFIX)
        ],
    )


def discover_legacy(
    project_root: Path, candidates: Sequence[str]
) -> Optional[LegacyState]:
    """Return the first candidate directory that holds an npm-era layout.

    Candidates that are absolute, escape the project root, are reached
    through a link or junction, or lack ownership evidence are skipped.
    Discovery never writes.
    """

    for raw in candidates:
        found = unaliased_dir_within_root(project_root, raw, "legacy local dir")
        if found is None:
            continue
        relative, directory = found
        if looks_like_legacy_dir(directory):
            return read_legacy_state(project_root, relative, directory)
    return None


def has_npm_runtime(legacy: Optional[LegacyState]) -> bool:
    """Return True when the legacy tree still holds a runnable npm install."""

    if legacy is None:
        return False
    return any(name in legacy.npm_artifacts for name in LEGACY_RUNTIME_ARTIFACTS)


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp (`Z` or offset form); None when invalid."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def legacy_is_authoritative(
    legacy: Optional[LegacyState],
    native_complete: bool,
    native_generated_at: Optional[str],
) -> bool:
    """Decide whether the legacy layout, not the native runtime, is in charge.

    A projection (metadata with `migrated_to`) is never authoritative. With
    no complete native runtime, a legacy npm runtime is. When both exist,
    the native runtime wins only when its metadata's `generated_at` is
    newer than the legacy metadata's (requirement 9); a legacy layout with
    no timestamp at all (no metadata) is treated as the older of the two.
    """

    if legacy is None or legacy.is_projection or not has_npm_runtime(legacy):
        return False
    if not native_complete:
        return True
    native_moment = parse_iso_timestamp(native_generated_at)
    if native_moment is None:
        return True
    legacy_moment = parse_iso_timestamp(legacy.generated_at)
    if legacy_moment is None:
        return False
    return not native_moment > legacy_moment


def legacy_binary_candidates(legacy_dir: Path, platform_key: str) -> List[Path]:
    """Return where an npm install keeps the native binary for a platform.

    The per-platform optional dependency holds `claude[.exe]`; the wrapper
    package's postinstall copies (or hard-links) it over `bin/claude.exe`
    on every operating system.
    """

    scope = legacy_dir / "node_modules" / NPM_SCOPE
    return [
        scope
        / "{}-{}".format(WRAPPER_PACKAGE_DIR, platform_key)
        / binary_filename(platform_key),
        scope / WRAPPER_PACKAGE_DIR / "bin" / "claude.exe",
    ]


def _regular_file(path: Path) -> bool:
    """Return True for a regular file that is not a link or junction."""

    try:
        status = os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and not is_link_like(path)


def sha256_file(path: Path) -> str:
    """Return a file's SHA-256 hex digest, streamed in chunks."""

    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class AdoptionResult:
    """Outcome of looking for an adoptable legacy binary."""

    source: Optional[Path]
    notes: List[str] = field(default_factory=list)


def find_adoptable_binary(
    candidates: Iterable[Path],
    expected_size: int,
    expected_sha256: str,
) -> AdoptionResult:
    """Return the first legacy binary whose size and SHA-256 match the manifest.

    Every non-matching candidate is reported in `notes` so the operator
    learns why the binary is downloaded instead (requirement 4).
    """

    notes: List[str] = []
    wanted = expected_sha256.lower()
    for path in candidates:
        if not _regular_file(path):
            continue
        try:
            size = os.lstat(str(path)).st_size
        except OSError as exc:
            notes.append("cannot inspect legacy binary {}: {}".format(path, exc))
            continue
        if size != expected_size:
            notes.append(
                "legacy binary {} is {} bytes; the signed manifest says {}".format(
                    path, size, expected_size
                )
            )
            continue
        try:
            digest = sha256_file(path)
        except OSError as exc:
            notes.append("cannot hash legacy binary {}: {}".format(path, exc))
            continue
        if digest != wanted:
            notes.append(
                "legacy binary {} has SHA-256 {}; the signed manifest says {}".format(
                    path, digest, wanted
                )
            )
            continue
        return AdoptionResult(path, notes)
    return AdoptionResult(None, notes)


def copy_adopted_binary(source: Path, destination: Path, expected_sha256: str) -> None:
    """Copy (never link) an adopted binary into the candidate and re-hash it."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source), str(destination))
    digest = sha256_file(destination)
    if digest != expected_sha256.lower():
        try:
            destination.unlink()
        except OSError:
            pass
        raise MigrationError(
            "The adopted copy of {} does not match the signed manifest "
            "(SHA-256 {}).".format(source, digest)
        )


def select_legacy_launch_binary(
    project_root: Path,
    legacy: LegacyState,
    platform_key: str,
    check_structure: Callable[[Path, str], None],
) -> Optional[str]:
    """Return the repo-relative POSIX path of a runnable legacy binary.

    Used only by offline operations (the settings toggles and
    `--skip-install`) to keep an existing npm-era checkout's launcher
    running (requirement 6): the first candidate that passes the
    structural check for this host's platform key wins. No signature check
    is possible without the network, so callers use this only where a
    claude-wrangler launcher already exists and git tracks none of the
    layout; `--update` hashes the binary against the signed manifest
    instead.
    """

    for path in legacy_binary_candidates(legacy.dir, platform_key):
        if not _regular_file(path):
            continue
        try:
            check_structure(path, platform_key)
        except (PayloadError, PlatformError, OSError):
            continue
        try:
            return Path(os.path.relpath(str(path), str(project_root))).as_posix()
        except ValueError:
            continue
    return None


def build_projection(
    config: Config,
    legacy: LegacyState,
    *,
    version: str,
    channel: str,
    generated_at: str,
) -> Dict[str, Any]:
    """Return the schema-1 compatibility projection for the legacy directory.

    Older claude-wrangler releases read it, so their inspect and uninstall
    keep working (requirement 8). It keeps the selector, channel, version,
    `shared_home`, `reasonable_permissions_enabled`, and points to the
    native runtime through `migrated_to`. Its `generated_at` equals the
    native metadata's, and the projection is never itself authoritative.
    """

    payload: Dict[str, Any] = dict(legacy.metadata or {})
    payload.update(
        {
            "schema_version": LEGACY_SCHEMA_VERSION,
            "script_name": SCRIPT_NAME,
            "script_version": SCRIPT_VERSION,
            "generated_at": generated_at,
            "project_root": str(config.project_root),
            "claude_selector": config.claude_selector,
            "claude_channel": channel,
            "claude_version": version,
            "shared_home": config.shared_home,
            "reasonable_permissions_enabled": config.reasonable_permissions_enabled,
            "version_source": "migrated to the native runtime",
            "available_versions": {
                key: value
                for key, value in config.available_versions.items()
                if key in ("stable", "latest")
            },
            "available_versions_updated_at": config.available_versions_updated_at,
            "paths": {
                "local_dir": legacy.dir_relative,
                "claude_home_dir": config.layout.claude_home_relative,
                "claude_config_dir": config.layout.config_dir_relative,
                "launcher": config.layout.launcher_relative,
                "readme_local": config.layout.readme_relative,
            },
            "migrated_to": {
                "schema_version": SCHEMA_VERSION,
                "runtime_root": config.layout.local_dir_relative,
                "metadata": "{}/{}".format(
                    config.layout.local_dir_relative, METADATA_FILENAME
                ),
                "migrated_at": generated_at,
            },
        }
    )
    return payload


def projection_text(payload: Dict[str, Any]) -> str:
    """Return the projection as sorted, LF-terminated JSON text."""

    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@dataclass
class LegacyCleanupResult:
    """What a legacy-tree cleanup removed, renamed aside, or had to leave."""

    removed: List[str] = field(default_factory=list)
    retired: List[str] = field(default_factory=list)
    left: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Return True when nothing had to be renamed aside or left behind."""

        return not self.retired and not self.left


def cleanup_legacy_tree(
    legacy_dir: Path,
    *,
    in_use: Callable[[Path], bool],
    txid: str,
    dry_run: bool = False,
) -> LegacyCleanupResult:
    """Remove the npm tree, deferring anything that is locked or running.

    `node_modules/`, `package.json`, `package-lock.json`, `.npm-cache/`,
    and earlier `.retired-*` leftovers are removed when nothing runs from
    them. An entry that is in use, or whose removal fails (Windows refuses
    to delete a running image), is renamed to `.retired-<txid>-<name>` when
    possible and otherwise left in place; both are reported so the next
    install, upgrade, or repair retries. Links are removed, never followed.
    The metadata file (the projection) is never touched.
    """

    result = LegacyCleanupResult()
    if not legacy_dir.is_dir() or is_link_like(legacy_dir):
        return result
    entries = [legacy_dir / name for name in LEGACY_NPM_ARTIFACTS]
    try:
        entries.extend(
            legacy_dir / name
            for name in sorted(os.listdir(str(legacy_dir)))
            if name.startswith(LEGACY_RETIRED_PREFIX)
        )
    except OSError:
        pass
    for entry in entries:
        if not _lexists(entry):
            continue
        name = entry.name
        if dry_run:
            result.removed.append(name)
            continue
        reason = ""
        busy = in_use(entry)
        if busy:
            reason = "a running process uses it"
        else:
            try:
                remove_tree(entry)
            except OSError as exc:
                reason = str(exc)
            else:
                result.removed.append(name)
                continue
        if name.startswith(LEGACY_RETIRED_PREFIX):
            result.left.append((name, reason))
            continue
        aside = legacy_dir / "{}{}-{}".format(LEGACY_RETIRED_PREFIX, txid, name)
        try:
            retry_on_windows_lock(lambda: os.rename(str(entry), str(aside)), 5.0)
        except OSError as exc:
            result.left.append(
                (name, "{}; rename aside failed: {}".format(reason, exc))
            )
        else:
            result.retired.append(aside.name)
    return result
