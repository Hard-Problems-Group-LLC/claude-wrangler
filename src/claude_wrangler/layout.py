"""Filesystem layout and native-metadata discovery helpers.

Legacy npm-layout discovery lives in `migration.py`; this module only knows
the native (schema 2) layout rooted at `--local-dir`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .constants import (
    CLAUDE_CHANNELS,
    CONFIG_DIR_SPELLINGS,
    METADATA_FILENAME,
    SCHEMA_VERSION,
)
from .models import ClaudeWranglerError, ExistingState, Layout

# Recorded `paths` keys and the CLI flags they mirror.
RECORDED_PATH_KEYS = (
    "local_dir",
    "claude_home_dir",
    "claude_config_dir",
    "launcher",
    "readme_local",
)


def resolve_project_root(raw_path: str) -> Path:
    """Resolve and validate the target project directory."""

    project_root = Path(raw_path).expanduser().resolve()
    if not project_root.exists():
        raise ClaudeWranglerError(
            "Project root does not exist: {}".format(project_root)
        )
    if not project_root.is_dir():
        raise ClaudeWranglerError(
            "Project root is not a directory: {}".format(project_root)
        )
    return project_root


def resolve_relative_within_root(
    project_root: Path,
    raw_relative_path: str,
    label: str,
) -> Tuple[str, Path]:
    """Resolve a relative path and prove it stays beneath project_root.

    The returned relative spelling always uses POSIX `/` separators, even
    on Windows, because it is recorded in metadata and embedded in the
    `.gitignore` block and generated launcher files, all of which must
    read the same on every operating system that shares a checkout.
    """

    relative_path = Path(raw_relative_path)
    # On Windows, `\foo` (rooted, no drive) and `C:foo` (drive-relative)
    # are not "absolute" to pathlib yet would not stay under the project
    # root, so any anchor at all is rejected.
    if relative_path.is_absolute() or relative_path.drive or relative_path.root:
        raise ClaudeWranglerError(
            "{} must be relative to the project root, not absolute: {}".format(
                label, raw_relative_path
            )
        )

    normalized_relative = Path(os.path.normpath(str(relative_path)))
    if str(normalized_relative) in ("", "."):
        raise ClaudeWranglerError(
            "{} may not resolve to the project root itself.".format(label)
        )

    resolved = (project_root / normalized_relative).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ClaudeWranglerError(
            "{} escapes the project root after normalization: {}".format(
                label, raw_relative_path
            )
        ) from exc

    return normalized_relative.as_posix(), resolved


def build_layout(
    project_root: Path,
    local_dir_raw: str,
    claude_home_raw: str,
    config_dir_raw: str,
    launcher_raw: str,
    readme_raw: str,
) -> Layout:
    """Resolve all managed paths into a validated layout structure."""

    local_dir_relative, local_dir = resolve_relative_within_root(
        project_root, local_dir_raw, "--local-dir"
    )
    claude_home_relative, claude_home_dir = resolve_relative_within_root(
        project_root, claude_home_raw, "--claude-home-dir"
    )
    config_dir_relative, config_dir = resolve_relative_within_root(
        project_root, config_dir_raw, "--claude-config-dir"
    )
    launcher_relative, launcher_path = resolve_relative_within_root(
        project_root, launcher_raw, "--launcher"
    )
    readme_relative, readme_path = resolve_relative_within_root(
        project_root, readme_raw, "--readme-local"
    )

    return Layout(
        project_root=project_root,
        local_dir_relative=local_dir_relative,
        claude_home_relative=claude_home_relative,
        config_dir_relative=config_dir_relative,
        launcher_relative=launcher_relative,
        readme_relative=readme_relative,
        local_dir=local_dir,
        claude_home_dir=claude_home_dir,
        config_dir=config_dir,
        launcher_path=launcher_path,
        readme_path=readme_path,
        metadata_path=local_dir / METADATA_FILENAME,
        gitignore_path=project_root / ".gitignore",
    )


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON object file or return None when the file is absent."""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeWranglerError(
            "Failed to parse JSON in {}: {}".format(path, exc)
        ) from exc
    except OSError as exc:
        raise ClaudeWranglerError(
            "Failed to read JSON in {}: {}".format(path, exc)
        ) from exc
    if not isinstance(payload, dict):
        raise ClaudeWranglerError("Expected JSON object in {}".format(path))
    return payload


def _optional_bool(payload: Dict[str, Any], key: str) -> Optional[bool]:
    """Return a boolean field or None when absent or mistyped."""

    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _optional_str(payload: Dict[str, Any], key: str) -> Optional[str]:
    """Return a non-empty string field or None."""

    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def parse_available_versions(raw: Any) -> Dict[str, Optional[str]]:
    """Return the `{channel: version-or-None}` catalog from metadata."""

    versions: Dict[str, Optional[str]] = {}
    if not isinstance(raw, dict):
        return versions
    for raw_channel, raw_version in raw.items():
        if not isinstance(raw_channel, str):
            continue
        if isinstance(raw_version, str) or raw_version is None:
            versions[raw_channel] = raw_version
    return versions


def parse_recorded_paths(raw: Any) -> Dict[str, str]:
    """Return the recorded relative `paths` that still look usable."""

    paths: Dict[str, str] = {}
    if not isinstance(raw, dict):
        return paths
    for key in RECORDED_PATH_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            paths[key] = value.strip()
    return paths


def read_native_metadata(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return `(metadata, error)` for a schema-2 metadata file.

    A missing file is `(None, None)`. A file that cannot be parsed, is not
    an object, or carries another schema yields `(None, reason)`; the
    payload of a foreign schema is never mistaken for native state.
    """

    try:
        payload = read_json_file(path)
    except ClaudeWranglerError as exc:
        return None, str(exc)
    if payload is None:
        return None, None
    schema = payload.get("schema_version")
    if schema != SCHEMA_VERSION or isinstance(schema, bool):
        return None, "Metadata {} has schema_version {!r}, not {}.".format(
            path, schema, SCHEMA_VERSION
        )
    return payload, None


def read_existing_state(layout: Layout) -> ExistingState:
    """Read the native install's schema-2 metadata, tolerating damage."""

    metadata, error = read_native_metadata(layout.metadata_path)
    if metadata is None:
        return ExistingState(metadata=None, metadata_error=error)

    channel = _optional_str(metadata, "claude_channel")
    if channel is not None and channel not in CLAUDE_CHANNELS:
        channel = None
    spelling = _optional_str(metadata, "config_dir_spelling")
    if spelling not in CONFIG_DIR_SPELLINGS:
        spelling = None
    details = metadata.get("available_version_details")
    last_check = metadata.get("last_update_check")
    return ExistingState(
        metadata=metadata,
        metadata_error=None,
        schema_version=SCHEMA_VERSION,
        requested_claude_selector=_optional_str(metadata, "claude_selector"),
        claude_channel=channel,
        pinned_claude_version=_optional_str(metadata, "claude_version"),
        reasonable_permissions_enabled=_optional_bool(
            metadata, "reasonable_permissions_enabled"
        ),
        available_versions=parse_available_versions(metadata.get("available_versions")),
        available_versions_updated_at=_optional_str(
            metadata, "available_versions_updated_at"
        ),
        available_version_details=(
            {
                key: dict(value)
                for key, value in details.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
            if isinstance(details, dict)
            else {}
        ),
        last_update_check=dict(last_check) if isinstance(last_check, dict) else None,
        shared_home=_optional_bool(metadata, "shared_home"),
        update_checks=_optional_bool(metadata, "update_checks"),
        windows_sandbox_warning=_optional_bool(metadata, "windows_sandbox_warning"),
        release_base_url=_optional_str(metadata, "release_base_url"),
        platform=_optional_str(metadata, "platform"),
        launcher_python=_optional_str(metadata, "launcher_python"),
        config_dir_spelling=spelling,
        generated_at=_optional_str(metadata, "generated_at"),
        legacy_migrated_from=_optional_str(metadata, "legacy_migrated_from"),
        legacy_migrated_at=_optional_str(metadata, "legacy_migrated_at"),
        recorded_paths=parse_recorded_paths(metadata.get("paths")),
        version_source=_optional_str(metadata, "version_source"),
    )


def managed_state_fingerprint(paths: Sequence[Path]) -> str:
    """Return a digest of the metadata files a command's decisions rest on.

    Commands read their state (native metadata, npm-era metadata) before
    they take the maintenance lock. Comparing this digest again once the
    lock is held detects another maintainer's publish in between, so a
    stale read can never revert it (`ab_runtime_slots.md` requirement 4).
    A missing file and an unreadable one hash differently from any content.
    """

    digest = hashlib.sha256()
    for path in paths:
        digest.update(os.fsencode(str(path)) + b"\0")
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        except OSError as exc:
            digest.update("unreadable {}\0".format(exc.errno).encode("ascii"))
            continue
        digest.update(str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return digest.hexdigest()
