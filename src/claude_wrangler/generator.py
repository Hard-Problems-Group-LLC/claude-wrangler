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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

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
from .slots import is_link_like

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
    config: Config, legacy: Optional[LegacyLaunch] = None, *, strict: bool = True
) -> List[GeneratedFile]:
    """Return every generated file in publication order.

    Files inside the runtime root come first; the launcher shims and the
    README follow, so a published shim never points at a missing core.
    With ``strict`` (the default) a file that cannot be rendered (a
    non-ASCII path in an ASCII-only cmd file) raises; without it that file
    is left out, so uninstall, inspect, and selftest still work for a
    layout that could never be installed.
    """

    layout = config.layout
    launch = build_launch_config(
        config,
        legacy_binary=legacy.binary if legacy is not None else None,
        legacy_version=legacy.version if legacy is not None else None,
    )
    shim_dir = layout.shim_dir
    specs: List[Tuple[str, Path, Callable[[], bytes], bool, bool, Tuple[str, ...]]] = [
        ("launch_py", layout.launch_core_path, launcher_core_source, False, True, ()),
        (
            "launch_json",
            layout.launch_config_path,
            lambda: _encode(launch_config_text(launch), LF),
            False,
            True,
            (),
        ),
        (
            "path_shim_sh",
            shim_dir / PATH_SHIM_NAME,
            lambda: _encode(build_path_shim_sh(config), LF),
            True,
            True,
            (SCRIPT_MARKER,),
        ),
        (
            "path_shim_cmd",
            shim_dir / (PATH_SHIM_NAME + ".cmd"),
            lambda: _encode(build_path_shim_cmd(config), CRLF, "ascii"),
            False,
            True,
            (SCRIPT_MARKER,),
        ),
        (
            "launcher_sh",
            layout.launcher_path,
            lambda: _encode(build_posix_shim(config), LF),
            True,
            False,
            (SCRIPT_MARKER,),
        ),
        (
            "launcher_cmd",
            layout.launcher_cmd_path,
            lambda: _encode(build_cmd_shim(config), CRLF, "ascii"),
            False,
            False,
            (SCRIPT_MARKER,),
        ),
        (
            "readme",
            layout.readme_path,
            lambda: _encode(
                build_local_readme_content(config, legacy_pending=legacy is not None),
                LF,
            ),
            False,
            False,
            (README_MARKER,),
        ),
    ]
    files: List[GeneratedFile] = []
    for key, path, render, executable, owned, markers in specs:
        try:
            data = render()
        except (ClaudeWranglerError, UnicodeEncodeError) as exc:
            if strict:
                if isinstance(exc, ClaudeWranglerError):
                    raise
                raise ClaudeWranglerError(
                    "{} cannot be written as ASCII: {}".format(path, exc)
                ) from exc
            eprint("[claude-wrangler] Warning: skipping {} ({}).".format(path, exc))
            continue
        files.append(GeneratedFile(key, path, data, executable, owned, markers))
    return files


def legacy_pending_alternatives(config: Config) -> Dict[str, bytes]:
    """Return the migration-pending renderings of files outside the runtime root.

    `--update` and the settings toggles render the README with a
    "migration pending" note while an npm-era layout is in charge. That
    file is claude-wrangler's own output, so uninstall accepts it as proof
    of ownership alongside the native rendering. Empty without a legacy
    layout.
    """

    if config.legacy is None:
        return {}
    pending = LegacyLaunch("", config.legacy.version)
    return {
        item.key: item.data
        for item in planned_files(config, pending, strict=False)
        if not item.owned_by_runtime
    }


def _write_generated(item: GeneratedFile, config: Config) -> None:
    """Write one generated file atomically after the ownership check."""

    if item.owned_by_runtime:
        if item.path.is_file() and item.path.read_bytes() == item.data:
            # Unchanged bytes can still have lost the execute bit (a copy
            # through a tool that drops modes); a non-executable PATH shim
            # would make nested `claude` calls fall through to a global one.
            if (
                item.executable
                and os.name != "nt"
                and not config.dry_run
                and not os.access(str(item.path), os.X_OK)
            ):
                os.chmod(str(item.path), 0o755)
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
    """Refuse early when `<config dir>/settings.json` cannot be merged.

    Called before any runtime change so a malformed file never leaves an
    operation half-published. A symbolic link (or other non-regular entry)
    that still lacks the suppression keys is refused here too: the merge
    never writes through a link, because that would change a file shared
    outside the project, so it would otherwise fail after promotion. A
    linked file that already carries every key is left alone.
    """

    payload = _read_settings(path)
    if not os.path.lexists(str(path)):
        return
    if is_link_like(path) or not path.is_file():
        if _merged_settings(path, payload)[1]:
            raise SettingsFileError(
                "{} is a symbolic link or not a regular file, and it lacks the "
                "self-update suppression settings claude-wrangler must add. "
                "claude-wrangler never writes through a link (that would change "
                "a file shared outside this project). Replace the link with a "
                "regular copy of the file in the config dir, then rerun.".format(path)
            )


def _merged_settings(
    path: Path, payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], bool]:
    """Return the settings with the suppression keys merged and whether they changed."""

    merged = dict(payload)
    env = dict(merged.get("env") or {})
    changed = not os.path.lexists(str(path))
    for name, value in SELF_UPDATE_SUPPRESSION_ENV:
        if env.get(name) != value:
            env[name] = value
            changed = True
    if "env" not in merged or merged.get("env") != env:
        merged["env"] = env
    if merged.get(DEEP_LINK_SETTING_KEY) != DEEP_LINK_SETTING_VALUE:
        merged[DEEP_LINK_SETTING_KEY] = DEEP_LINK_SETTING_VALUE
        changed = True
    return merged, changed


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

    payload, changed = _merged_settings(path, _read_settings(path))
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
    config: Config, legacy: Optional[LegacyLaunch] = None, *, strict: bool = False
) -> Dict[str, Dict[str, Any]]:
    """Report, per generated file, whether it exists and is byte-for-byte fresh.

    On POSIX an executable file (the launcher and the PATH shim) counts as
    fresh only while it keeps its execute permission. A file that cannot be
    rendered for this layout is left out unless ``strict`` is set.
    """

    report: Dict[str, Dict[str, Any]] = {}
    for item in planned_files(config, legacy, strict=strict):
        exists = item.path.is_file()
        fresh = False
        if exists:
            try:
                fresh = item.path.read_bytes() == item.data
            except OSError:
                fresh = False
            if fresh and item.executable and os.name != "nt":
                fresh = os.access(str(item.path), os.X_OK)
        report[item.key] = {"path": str(item.path), "exists": exists, "fresh": fresh}
    return report


def remove_generated_files(
    config: Config, legacy: Optional[LegacyLaunch] = None, *, strict: bool = True
) -> List[str]:
    """Remove the generated files outside the runtime root after ownership checks.

    Files inside the runtime root go away with the runtime root itself. A
    launcher or README that does not match its expected content is kept
    unless it carries the managed marker and `--force` is given. The
    migration-pending rendering (``legacy_pending_alternatives``) proves
    ownership too. Without ``strict`` a file that cannot be rendered for
    this layout is skipped.
    """

    alternatives = legacy_pending_alternatives(config)
    removed: List[str] = []
    for item in planned_files(config, legacy, strict=strict):
        if item.owned_by_runtime:
            continue
        expected = item.data.decode("utf-8")
        alternative = alternatives.get(item.key)
        if alternative is not None and item.path.is_file():
            try:
                current = item.path.read_bytes().decode("utf-8", "replace")
            except OSError:
                current = None
            if current is not None and normalize_newlines(
                current, LF
            ) == normalize_newlines(alternative.decode("utf-8"), LF):
                expected = alternative.decode("utf-8")
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
