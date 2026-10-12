"""Data models used by claude-wrangler."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

from .constants import (
    CMD_SHIM_SUFFIX,
    CONFIG_DIR_SPELLING_NATIVE,
    CONFIG_SETTINGS_FILENAME,
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    FALLBACK_FILENAME,
    LAUNCH_CONFIG_FILENAME,
    LAUNCH_CORE_FILENAME,
    PATH_SHIM_DIRNAME,
    UPDATE_CHECK_FILENAME,
)


class ClaudeWranglerError(RuntimeError):
    """Raised when an operation cannot be completed safely."""


@dataclass
class Layout:
    """Concrete filesystem layout for one managed installation.

    `local_dir` is the native runtime root (`--local-dir`, by default
    `.local/claude/wrangler`); every `*_relative` field is a POSIX-spelled
    path relative to `project_root`.
    """

    project_root: Path
    local_dir_relative: str
    claude_home_relative: str
    config_dir_relative: str
    launcher_relative: str
    readme_relative: str
    local_dir: Path
    claude_home_dir: Path
    config_dir: Path
    launcher_path: Path
    readme_path: Path
    metadata_path: Path
    gitignore_path: Path

    @property
    def runtime_root(self) -> Path:
        """Return the native runtime root (an alias of `local_dir`)."""

        return self.local_dir

    @property
    def launcher_cmd_relative(self) -> str:
        """Return the POSIX-spelled relative path of the cmd shim."""

        return self.launcher_relative + CMD_SHIM_SUFFIX

    @property
    def launcher_cmd_path(self) -> Path:
        """Return the cmd shim path beside the POSIX shim."""

        return self.launcher_path.with_name(self.launcher_path.name + CMD_SHIM_SUFFIX)

    @property
    def launch_core_path(self) -> Path:
        """Return the generated launcher core (`launch.py`) path."""

        return self.local_dir / LAUNCH_CORE_FILENAME

    @property
    def launch_config_path(self) -> Path:
        """Return the generated launcher configuration (`launch.json`) path."""

        return self.local_dir / LAUNCH_CONFIG_FILENAME

    @property
    def shim_dir(self) -> Path:
        """Return the directory holding the nested-`claude` PATH shims."""

        return self.local_dir / PATH_SHIM_DIRNAME

    @property
    def update_check_path(self) -> Path:
        """Return the cached update-check result path."""

        return self.local_dir / UPDATE_CHECK_FILENAME

    @property
    def fallback_path(self) -> Path:
        """Return the launch-time fallback record path."""

        return self.local_dir / FALLBACK_FILENAME

    @property
    def config_settings_path(self) -> Path:
        """Return `<config dir>/settings.json`."""

        return self.config_dir / CONFIG_SETTINGS_FILENAME


@dataclass
class LegacyState:
    """Read-only facts about an npm-era layout (`.local/claude/npm`).

    Built by `migration.discover_legacy`, which never writes anything.
    `channel` is already normalized (`next` becomes `latest`, with
    `channel_deprecated` set). `migrated_to` is present only on the schema-1
    compatibility projection that a completed migration leaves behind.
    """

    dir_relative: str
    dir: Path
    metadata: Optional[Dict[str, Any]] = None
    metadata_error: Optional[str] = None
    lockfile_error: Optional[str] = None
    package_json_error: Optional[str] = None
    selector: Optional[str] = None
    channel: Optional[str] = None
    channel_deprecated: bool = False
    version: Optional[str] = None
    version_source: Optional[str] = None
    shared_home: Optional[bool] = None
    reasonable_permissions_enabled: Optional[bool] = None
    update_checks: Optional[bool] = None
    windows_sandbox_warning: Optional[bool] = None
    release_base_url: Optional[str] = None
    generated_at: Optional[str] = None
    recorded_paths: Dict[str, str] = field(default_factory=dict)
    available_versions: Dict[str, Optional[str]] = field(default_factory=dict)
    available_versions_updated_at: Optional[str] = None
    migrated_to: Optional[Dict[str, Any]] = None
    npm_artifacts: List[str] = field(default_factory=list)
    retired_entries: List[str] = field(default_factory=list)

    @property
    def is_projection(self) -> bool:
        """Return True when the metadata is a post-migration projection."""

        return self.migrated_to is not None

    @property
    def cleanup_residue(self) -> List[str]:
        """Return legacy entries a migration cleanup still has to remove."""

        return list(self.npm_artifacts) + list(self.retired_entries)


@dataclass
class Config:
    """Normalized runtime configuration used by the implementation."""

    operation: str
    project_root: Path
    claude_selector: str
    claude_channel: Optional[str]
    claude_version: Optional[str]
    shared_home: bool
    skip_install: bool
    force: bool
    dry_run: bool
    layout: Layout
    version_source: str
    reasonable_permissions_enabled: bool = False
    reconfigure_only: bool = False
    repair_install: bool = False
    update_checks: bool = True
    windows_sandbox_warning: bool = True
    release_base_url: Optional[str] = None
    ca_bundle: Optional[str] = None
    download_timeout_seconds: int = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    platform_override: Optional[str] = None
    platform_key: Optional[str] = None
    stop_background_sessions: bool = False
    channel_explicit: bool = False
    version_explicit: bool = False
    launcher_python: str = field(default_factory=lambda: sys.executable)
    config_dir_spelling: str = CONFIG_DIR_SPELLING_NATIVE
    available_versions: Dict[str, Optional[str]] = field(default_factory=dict)
    available_versions_updated_at: Optional[str] = None
    available_version_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_update_check: Optional[Dict[str, Any]] = None
    legacy_migrated_from: Optional[str] = None
    legacy_migrated_at: Optional[str] = None
    existing_metadata: Optional[Dict[str, Any]] = None
    existing_metadata_error: Optional[str] = None
    legacy: Optional[LegacyState] = None
    # The metadata files this config was derived from and their digest
    # (`layout.managed_state_fingerprint`); maintenance operations re-check
    # the digest under the lock. None skips the check.
    state_paths: List[Path] = field(default_factory=list)
    state_fingerprint: Optional[str] = None


@dataclass
class ExistingState:
    """Metadata inferred from a previously managed native install, when present."""

    metadata: Optional[Dict[str, Any]] = None
    metadata_error: Optional[str] = None
    schema_version: Optional[int] = None
    requested_claude_selector: Optional[str] = None
    claude_channel: Optional[str] = None
    pinned_claude_version: Optional[str] = None
    reasonable_permissions_enabled: Optional[bool] = None
    available_versions: Dict[str, Optional[str]] = field(default_factory=dict)
    available_versions_updated_at: Optional[str] = None
    available_version_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_update_check: Optional[Dict[str, Any]] = None
    shared_home: Optional[bool] = None
    update_checks: Optional[bool] = None
    windows_sandbox_warning: Optional[bool] = None
    release_base_url: Optional[str] = None
    platform: Optional[str] = None
    launcher_python: Optional[str] = None
    config_dir_spelling: Optional[str] = None
    generated_at: Optional[str] = None
    legacy_migrated_from: Optional[str] = None
    legacy_migrated_at: Optional[str] = None
    recorded_paths: Dict[str, str] = field(default_factory=dict)
    version_source: Optional[str] = None


@dataclass
class SelfTestResult:
    """One self-test check result; `warning` results never fail the run."""

    name: str
    ok: bool
    detail: str
    warning: bool = False
