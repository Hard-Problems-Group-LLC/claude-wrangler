"""Publication of claude-wrangler's generated launcher files.

The generator writes, atomically and with the right newline convention
(`ab_runtime_slots.md` requirement 14, `cross_platform_launcher.md`
requirement 14):

* `<runtime root>/launch.py`: a byte-for-byte copy of the packaged
  `launcher_core.py`;
* `<runtime root>/launch.json`: the launcher contract (LF JSON);
* `<runtime root>/shim/claude` (sh, LF) and `shim/claude.cmd` (CRLF,
  ASCII): nested-`claude` PATH shims that re-enter the launcher;
* `bin/claude-local` (sh, LF, executable) and `bin/claude-local.cmd`
  (CRLF, ASCII), written after the core so a shim never points at a
  missing core; and
* the local README.

It also merges the self-update suppression keys into
`<config dir>/settings.json`, preserving every other key and refusing to
touch a malformed file, and it reports generated-file freshness for
`--inspect` and `--selftest`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import launcher_core
from .atomicio import atomic_write_bytes, atomic_write_text
from .constants import (
    DEEP_LINK_SETTING_KEY,
    DEEP_LINK_SETTING_VALUE,
    PATH_SHIM_NAME,
    README_MARKER,
    SCRIPT_MARKER,
    SELF_UPDATE_SUPPRESSION_ENV,
)
from .filesystem import (
    ensure_managed_directories,
    normalize_newlines,
    remove_file_if_managed,
    write_text_file,
)
from .models import ClaudeWranglerError, Config
from .rendering import (
    build_cmd_shim,
    build_launch_config,
    build_local_readme_content,
    build_path_shim_cmd,
    build_path_shim_sh,
    build_posix_shim,
    launch_config_text,
    launcher_core_source,
)
from .runtime import eprint

LF = "\n"
CRLF = "\r\n"


class SettingsFileError(ClaudeWranglerError):
    """Raised when `<config dir>/settings.json` cannot be merged safely."""


@dataclass(frozen=True)
class LegacyLaunch:
    """The npm-era binary a migration-pending launcher runs directly."""

    binary: str
    version: Optional[str]


@dataclass(frozen=True)
class GeneratedFile:
    """One generated file: its path, exact bytes, and ownership rules."""

    key: str
    path: Path
    data: bytes
    executable: bool
    owned_by_runtime: bool
    markers: Tuple[str, ...]


def _encode(text: str, newline: str, encoding: str = "utf-8") -> bytes:
    """Normalize line endings and encode text for a generated file."""

    return normalize_newlines(text, newline).encode(encoding)


def planned_files(
    config: Config, legacy: Optional[LegacyLaunch] = None
) -> List[GeneratedFile]:
    """Return every generated file in publication order.

    Files inside the runtime root come first; the launcher shims and the
    README follow, so a published shim never points at a missing core.
    """

    layout = config.layout
    launch = build_launch_config(
        config,
        legacy_binary=legacy.binary if legacy is not None else None,
        legacy_version=legacy.version if legacy is not None else None,
    )
    shim_dir = layout.shim_dir
    return [
        GeneratedFile(
            "launch_py",
            layout.launch_core_path,
            launcher_core_source(),
            False,
            True,
            (),
        ),
        GeneratedFile(
            "launch_json",
            layout.launch_config_path,
            _encode(launch_config_text(launch), LF),
            False,
            True,
            (),
        ),
        GeneratedFile(
            "path_shim_sh",
            shim_dir / PATH_SHIM_NAME,
            _encode(build_path_shim_sh(config), LF),
            True,
            True,
            (SCRIPT_MARKER,),
        ),
        GeneratedFile(
            "path_shim_cmd",
            shim_dir / (PATH_SHIM_NAME + ".cmd"),
            _encode(build_path_shim_cmd(config), CRLF, "ascii"),
            False,
            True,
            (SCRIPT_MARKER,),
        ),
        GeneratedFile(
            "launcher_sh",
            layout.launcher_path,
            _encode(build_posix_shim(config), LF),
            True,
            False,
            (SCRIPT_MARKER,),
        ),
        GeneratedFile(
            "launcher_cmd",
            layout.launcher_cmd_path,
            _encode(build_cmd_shim(config), CRLF, "ascii"),
            False,
            False,
            (SCRIPT_MARKER,),
        ),
        GeneratedFile(
            "readme",
            layout.readme_path,
            _encode(
                build_local_readme_content(config, legacy_pending=legacy is not None),
                LF,
            ),
            False,
            False,
            (README_MARKER,),
        ),
    ]


def _write_generated(item: GeneratedFile, config: Config) -> None:
    """Write one generated file atomically after the ownership check."""

    if item.owned_by_runtime:
        if item.path.is_file() and item.path.read_bytes() == item.data:
            return
        if config.dry_run:
            eprint("[claude-wrangler] Would write: {}".format(item.path))
            return
        eprint("[claude-wrangler] Writing: {}".format(item.path))
        mode = None
        if item.executable and os.name != "nt":
            mode = 0o755
        atomic_write_bytes(item.path, item.data, mode=mode)
        return
    write_text_file(
        item.path,
        item.data.decode("utf-8"),
        force=config.force,
        dry_run=config.dry_run,
        executable=item.executable,
        managed_markers=item.markers,
    )


def check_config_settings(path: Path) -> None:
    """Refuse early when `<config dir>/settings.json` is malformed.

    Called before any runtime change so a malformed file never leaves an
    operation half-published.
    """

    _read_settings(path)


def _read_settings(path: Path) -> Dict[str, Any]:
    """Return the parsed settings object, `{}` when absent, or raise."""

    if not path.exists():
        return {}
    if not path.is_file():
        raise SettingsFileError("{} is not a regular file.".format(path))
    try:
        payload = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SettingsFileError(
            "{} is not valid JSON ({}); refusing to rewrite it. Fix or remove it, "
            "then rerun.".format(path, exc)
        ) from exc
    if not isinstance(payload, dict):
        raise SettingsFileError(
            "{} does not hold a JSON object; refusing to rewrite it.".format(path)
        )
    env = payload.get("env")
    if env is not None and not isinstance(env, dict):
        raise SettingsFileError(
            "{} has an `env` that is not an object; refusing to rewrite it.".format(
                path
            )
        )
    return payload


def merge_config_settings(path: Path, dry_run: bool = False) -> bool:
    """Merge the suppression keys into `<config dir>/settings.json`.

    Sets `env.DISABLE_UPDATES`, `env.DISABLE_AUTOUPDATER`, and
    `env.DISABLE_INSTALLATION_CHECKS` to "1" and
    `disableDeepLinkRegistration` to "disable", preserving every other key
    and the existing key order. Detached processes that bypass the launcher
    inherit these (cross_platform_launcher.md requirement 4). Returns True
    when the file changed.
    """

    payload = _read_settings(path)
    env = dict(payload.get("env") or {})
    changed = not path.exists()
    for name, value in SELF_UPDATE_SUPPRESSION_ENV:
        if env.get(name) != value:
            env[name] = value
            changed = True
    if "env" not in payload or payload.get("env") != env:
        payload["env"] = env
    if payload.get(DEEP_LINK_SETTING_KEY) != DEEP_LINK_SETTING_VALUE:
        payload[DEEP_LINK_SETTING_KEY] = DEEP_LINK_SETTING_VALUE
        changed = True
    if not changed:
        return False
    action = "Would update" if dry_run else "Updating"
    eprint("[claude-wrangler] {} {}".format(action, path))
    if dry_run:
        return True
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + LF)
    return True


def settings_suppression_status(path: Path) -> Dict[str, Any]:
    """Report whether `<config dir>/settings.json` carries the suppression keys."""

    status: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "parse_error": None,
        "missing_or_wrong": [],
    }
    try:
        payload = _read_settings(path)
    except SettingsFileError as exc:
        status["parse_error"] = str(exc)
        return status
    env = payload.get("env") or {}
    for name, value in SELF_UPDATE_SUPPRESSION_ENV:
        if env.get(name) != value:
            status["missing_or_wrong"].append("env.{}".format(name))
    if payload.get(DEEP_LINK_SETTING_KEY) != DEEP_LINK_SETTING_VALUE:
        status["missing_or_wrong"].append(DEEP_LINK_SETTING_KEY)
    status["ok"] = not status["missing_or_wrong"]
    return status


def write_generated_files(
    config: Config, legacy: Optional[LegacyLaunch] = None
) -> None:
    """Publish every generated file, the session directories, and settings."""

    ensure_managed_directories(config)
    merge_config_settings(config.layout.config_settings_path, dry_run=config.dry_run)
    for item in planned_files(config, legacy):
        _write_generated(item, config)


def generated_file_status(
    config: Config, legacy: Optional[LegacyLaunch] = None
) -> Dict[str, Dict[str, Any]]:
    """Report, per generated file, whether it exists and is byte-for-byte fresh."""

    report: Dict[str, Dict[str, Any]] = {}
    for item in planned_files(config, legacy):
        exists = item.path.is_file()
        fresh = False
        if exists:
            try:
                fresh = item.path.read_bytes() == item.data
            except OSError:
                fresh = False
        report[item.key] = {"path": str(item.path), "exists": exists, "fresh": fresh}
    return report


def remove_generated_files(
    config: Config, legacy: Optional[LegacyLaunch] = None
) -> List[str]:
    """Remove the generated files outside the runtime root after ownership checks.

    Files inside the runtime root go away with the runtime root itself. A
    launcher or README that does not match its expected content is kept
    unless it carries the managed marker and `--force` is given.
    """

    removed: List[str] = []
    for item in planned_files(config, legacy):
        if item.owned_by_runtime:
            continue
        expected = item.data.decode("utf-8")
        if remove_file_if_managed(
            item.path,
            expected,
            force=config.force,
            dry_run=config.dry_run,
            label=item.key.replace("_", " "),
        ):
            removed.append(str(item.path))
    return removed


def parsed_launch_config(
    config: Config, legacy: Optional[LegacyLaunch] = None
) -> launcher_core.LaunchConfig:
    """Return the launcher core's own parse of the rendered launch.json."""

    data = build_launch_config(
        config,
        legacy_binary=legacy.binary if legacy is not None else None,
        legacy_version=legacy.version if legacy is not None else None,
    )
    return launcher_core.parse_launch_config(json.loads(launch_config_text(data)))


def launcher_session_environment(
    config: Config,
    base: Mapping[str, str],
    *,
    shared_home: Optional[bool] = None,
    os_name: Optional[str] = None,
) -> Dict[str, str]:
    """Return the environment the generated launcher would give Claude Code.

    Built with the launcher core's own functions from the rendered
    `launch.json`, so `daemon stop --any` and inspection see exactly the
    ring environment a launch would get.
    """

    effective_os = os.name if os_name is None else os_name
    launch = parsed_launch_config(config)
    paths = launcher_core.build_repo_paths(
        str(config.project_root), launch, effective_os
    )
    return launcher_core.build_session_environment(
        base,
        paths=paths,
        config=launch,
        shared_home=config.shared_home if shared_home is None else shared_home,
        os_name=effective_os,
    )
