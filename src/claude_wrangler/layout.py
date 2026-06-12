"""Filesystem layout and state-discovery helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .constants import CLAUDE_PACKAGE_NAME, METADATA_FILENAME
from .models import ClaudeWranglerError, ExistingState, Layout


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
    """Resolve a relative path and prove it stays beneath project_root."""

    relative_path = Path(raw_relative_path)
    if relative_path.is_absolute():
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

    return str(normalized_relative), resolved


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
        local_package_json_path=local_dir / "package.json",
        local_package_lock_path=local_dir / "package-lock.json",
        local_node_modules_dir=local_dir / "node_modules",
        metadata_path=local_dir / METADATA_FILENAME,
        gitignore_path=project_root / ".gitignore",
    )


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON object file or return None when the file is absent."""

    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
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


def read_existing_state(layout: Layout) -> ExistingState:
    """Read any previously managed state available at the target paths."""

    try:
        metadata = read_json_file(layout.metadata_path)
    except ClaudeWranglerError:
        metadata = None
    requested_claude_selector = None
    claude_channel = None
    pinned_claude_version = None
    available_versions: Dict[str, Optional[str]] = {}
    available_versions_updated_at = None
    reasonable_permissions_enabled = None
    shared_home = None

    if metadata:
        requested_claude_selector = metadata.get("claude_selector")
        claude_channel = metadata.get("claude_channel")
        pinned_claude_version = metadata.get("claude_version")
        raw_available_versions = metadata.get("available_versions")
        if isinstance(raw_available_versions, dict):
            for raw_channel, raw_version in raw_available_versions.items():
                if not isinstance(raw_channel, str):
                    continue
                if isinstance(raw_version, str):
                    available_versions[raw_channel] = raw_version
                elif raw_version is None:
                    available_versions[raw_channel] = None
        raw_updated_at = metadata.get("available_versions_updated_at")
        if isinstance(raw_updated_at, str):
            available_versions_updated_at = raw_updated_at
        raw_reasonable_permissions = metadata.get("reasonable_permissions_enabled")
        if isinstance(raw_reasonable_permissions, bool):
            reasonable_permissions_enabled = raw_reasonable_permissions
        raw_shared_home = metadata.get("shared_home")
        if isinstance(raw_shared_home, bool):
            shared_home = raw_shared_home

    # Metadata is authoritative when present, but conservative fallback
    # inference keeps update/inspect useful for older managed installs.
    if requested_claude_selector is None:
        try:
            requested_claude_selector = infer_requested_version_from_package_json(
                layout.local_package_json_path
            )
        except ClaudeWranglerError:
            requested_claude_selector = None
    if pinned_claude_version is None:
        try:
            pinned_claude_version = infer_installed_version_from_lockfile(
                layout.local_package_lock_path
            )
        except ClaudeWranglerError:
            pinned_claude_version = None
    if pinned_claude_version is None:
        pinned_claude_version = requested_claude_selector

    return ExistingState(
        metadata=metadata,
        requested_claude_selector=(
            requested_claude_selector
            if isinstance(requested_claude_selector, str)
            else None
        ),
        claude_channel=claude_channel if isinstance(claude_channel, str) else None,
        pinned_claude_version=(
            pinned_claude_version if isinstance(pinned_claude_version, str) else None
        ),
        reasonable_permissions_enabled=reasonable_permissions_enabled,
        available_versions=available_versions,
        available_versions_updated_at=available_versions_updated_at,
        shared_home=shared_home,
    )


def infer_requested_version_from_package_json(
    package_json_path: Path,
) -> Optional[str]:
    """Return the pinned Claude version from the managed local package manifest."""

    payload = read_json_file(package_json_path)
    if not payload:
        return None
    dev_dependencies = payload.get("devDependencies")
    if not isinstance(dev_dependencies, dict):
        return None
    version = dev_dependencies.get(CLAUDE_PACKAGE_NAME)
    return version if isinstance(version, str) else None


def infer_installed_version_from_lockfile(lockfile_path: Path) -> Optional[str]:
    """Return the installed Claude version from package-lock.json when present."""

    payload = read_json_file(lockfile_path)
    if not payload:
        return None

    packages = payload.get("packages")
    if not isinstance(packages, dict):
        return None

    claude_entry = packages.get("node_modules/{}".format(CLAUDE_PACKAGE_NAME))
    if not isinstance(claude_entry, dict):
        return None
    version = claude_entry.get("version")
    return version if isinstance(version, str) else None
