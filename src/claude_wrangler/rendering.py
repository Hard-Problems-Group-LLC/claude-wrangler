"""Deterministic rendered content for managed files and reports.

Everything here is a pure function of the configuration: the schema-2
metadata document, the `.gitignore` block, `launch.json` for the launcher
core, the POSIX and cmd launcher shims, the nested-`claude` PATH shims,
the local README, and operator summaries. `generator.py` publishes the
files; this module never touches the filesystem except to read the
launcher core it copies and to look up a Windows short path.
"""

from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import posixpath
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    CLAUDE_CHANNELS,
    DEEP_LINK_SETTING_KEY,
    DEEP_LINK_SETTING_VALUE,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    LAUNCH_CONFIG_SCHEMA,
    LAUNCH_CORE_FILENAME,
    PATH_SHIM_DIRNAME,
    PATH_SHIM_NAME,
    README_MARKER,
    SCHEMA_VERSION,
    SCRIPT_MARKER,
    SCRIPT_NAME,
    SCRIPT_VERSION,
    SELF_UPDATE_SUPPRESSION_ENV,
)
from .distribution import DEFAULT_RELEASE_BASE_URL, VERSION_FLOOR
from .models import ClaudeWranglerError, Config, Layout
from .runtime import utc_now_iso

PYTHON_PROBE_POSIX = (
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
)
# Git Bash and MSYS2 ship POSIX-emulation Pythons; the launcher core needs
# a native Windows interpreter there.
PYTHON_PROBE_MSYS = (
    "import sys; raise SystemExit("
    "0 if sys.version_info >= (3, 9) and sys.platform == 'win32' else 1)"
)
# No redirection or escape characters, so a dropped quote can never turn
# the probe into a cmd redirection (the install.cmd convention).
PYTHON_PROBE_CMD = (
    "import sys; raise SystemExit(not (sys.hexversion // 0x03090000 and "
    "sys.platform == 'win32'))"
)


def suppression_env() -> Dict[str, str]:
    """Return the self-update suppression variables as a mapping."""

    return {name: value for name, value in SELF_UPDATE_SUPPRESSION_ENV}


def settings_override() -> Dict[str, Any]:
    """Return the `--settings` document the launcher passes to Claude Code."""

    return {
        "env": suppression_env(),
        DEEP_LINK_SETTING_KEY: DEEP_LINK_SETTING_VALUE,
    }


def build_metadata(
    config: Config, *, generated_at: Optional[str] = None
) -> Dict[str, Any]:
    """Build the schema-2 metadata document stored at the runtime root."""

    return {
        "schema_version": SCHEMA_VERSION,
        "script_name": SCRIPT_NAME,
        "script_version": SCRIPT_VERSION,
        "generated_at": generated_at or utc_now_iso(),
        "project_root": str(config.project_root),
        "claude_selector": config.claude_selector,
        "claude_channel": config.claude_channel,
        "claude_version": config.claude_version,
        "shared_home": config.shared_home,
        "reasonable_permissions_enabled": config.reasonable_permissions_enabled,
        "update_checks": config.update_checks,
        "windows_sandbox_warning": config.windows_sandbox_warning,
        "release_base_url": config.release_base_url or DEFAULT_RELEASE_BASE_URL,
        "platform": config.platform_key,
        "launcher_python": config.launcher_python,
        "config_dir_spelling": config.config_dir_spelling,
        "version_source": config.version_source,
        "available_versions": {
            channel: config.available_versions.get(channel)
            for channel in CLAUDE_CHANNELS
        },
        "available_versions_updated_at": config.available_versions_updated_at,
        "available_version_details": {
            channel: dict(details)
            for channel, details in sorted(config.available_version_details.items())
        },
        "last_update_check": config.last_update_check,
        "legacy_migrated_from": config.legacy_migrated_from,
        "legacy_migrated_at": config.legacy_migrated_at,
        "paths": {
            "local_dir": config.layout.local_dir_relative,
            "claude_home_dir": config.layout.claude_home_relative,
            "claude_config_dir": config.layout.config_dir_relative,
            "launcher": config.layout.launcher_relative,
            "launcher_cmd": config.layout.launcher_cmd_relative,
            "readme_local": config.layout.readme_relative,
        },
    }


def build_gitignore_block(layout: Layout) -> str:
    """Return the managed `.gitignore` block.

    The default layout keeps every managed directory under `.local/`, so
    one umbrella rule covers them; explicit directory rules are added only
    when an operator relocated a managed directory outside `.local/`.
    Both launcher shims are listed. The project-root `.claude/` directory
    is deliberately not ignored: Claude Code keeps trackable project
    settings there.
    """

    directory_entries: List[str] = [".local/"]
    for relative in (
        layout.local_dir_relative,
        layout.claude_home_relative,
        layout.config_dir_relative,
    ):
        normalized = relative.rstrip("/")
        if normalized == ".local" or normalized.startswith(".local/"):
            continue
        entry = "{}/".format(normalized)
        if entry not in directory_entries:
            directory_entries.append(entry)

    lines = [
        GITIGNORE_BEGIN,
        "# Local Claude Code runtime, session state, launchers, and sentinel",
        "# artifacts.",
    ]
    lines.extend(directory_entries)
    lines.extend(
        [
            layout.launcher_relative,
            layout.launcher_cmd_relative,
            layout.readme_relative,
            GITIGNORE_END,
            "",
        ]
    )
    return "\n".join(lines)


def _parent_depth(relative: str) -> int:
    """Return how many directories a POSIX relative path sits below the root."""

    return len(PurePosixPath(relative).parent.parts)


def build_launch_config(
    config: Config,
    *,
    legacy_binary: Optional[str] = None,
    legacy_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the `launch.json` document the launcher core reads.

    Keys follow the launcher contract (schema 1); `release_base_url` and
    `config_dir_spelling` are optional extensions, and `legacy` switches the
    core to the Node-free npm-layout launcher while migration is pending.
    """

    layout = config.layout
    depth = len(PurePosixPath(layout.local_dir_relative).parts)
    channel = (
        config.claude_channel
        if config.claude_channel in CLAUDE_CHANNELS
        else ("stable")
    )
    data: Dict[str, Any] = {
        "schema": LAUNCH_CONFIG_SCHEMA,
        "repo_root_from_core": "/".join([".."] * depth),
        "config_dir": layout.config_dir_relative,
        "home_dir": layout.claude_home_relative,
        "runtime_root": layout.local_dir_relative,
        "shared_home_default": bool(config.shared_home),
        "reasonable_permissions": bool(config.reasonable_permissions_enabled),
        "windows_sandbox_warning": bool(config.windows_sandbox_warning),
        "update_checks": bool(config.update_checks),
        "channel": channel,
        "suppression_env": suppression_env(),
        "settings_override": settings_override(),
        "wrangler_command": SCRIPT_NAME,
        "release_base_url": config.release_base_url or DEFAULT_RELEASE_BASE_URL,
        "config_dir_spelling": config.config_dir_spelling,
    }
    if legacy_binary is not None:
        data["legacy"] = {"binary": legacy_binary, "version": legacy_version}
    return data


def launch_config_text(data: Dict[str, Any]) -> str:
    """Return `launch.json` text: sorted keys, two-space indent, LF."""

    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def launcher_core_source() -> bytes:
    """Return the packaged launcher core's exact bytes (copied to launch.py)."""

    from . import launcher_core

    path = Path(launcher_core.__file__)
    if path.suffix != ".py":
        path = path.with_suffix(".py")
    return path.read_bytes()


def _sh_quote(value: str) -> str:
    """Quote a string for POSIX sh with single quotes."""

    return "'" + value.replace("'", "'\\''") + "'"


def _windows_short_path(path: str) -> Optional[str]:
    """Return the 8.3 short spelling of an existing path, or None."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(path, buffer, len(buffer))
    except (AttributeError, OSError, ValueError):
        return None
    if not length or length >= len(buffer):
        return None
    return buffer.value


def cmd_safe_python_path(python: Optional[str]) -> Optional[str]:
    """Return a spelling of the recorded interpreter safe to embed in a .cmd.

    cmd shims are ASCII-only, so a non-ASCII path is replaced by its 8.3
    short name when one exists and otherwise left out (the shim then falls
    back to `py -3`, `python3`, and `python`). `%` is doubled for cmd.
    Only absolute Windows paths qualify.
    """

    if not python:
        return None
    drive, tail = ntpath.splitdrive(python)
    if not drive or not tail.startswith(("\\", "/")):
        return None
    candidate = python
    if not candidate.isascii():
        short = _windows_short_path(candidate)
        if short is None or not short.isascii():
            return None
        candidate = short
    if any(char in candidate for char in '"\r\n'):
        return None
    return candidate.replace("%", "%%")


def _require_ascii(value: str, label: str) -> str:
    """Refuse a non-ASCII value that must go into an ASCII-only cmd file."""

    if not value.isascii():
        raise ClaudeWranglerError(
            "{} {!r} is not ASCII, so it cannot be written into the ASCII-only "
            "cmd launcher. Choose an ASCII path.".format(label, value)
        )
    return value


def _cmd_path_literal(value: str, label: str) -> str:
    """Return a managed relative path ready to embed in a quoted cmd value.

    cmd files are ASCII-only and a double quote would end the quoted value,
    so both are refused; ``%`` is doubled so cmd does not expand it.
    """

    _require_ascii(value, label)
    if '"' in value:
        raise ClaudeWranglerError(
            "{} {!r} contains a double quote, which the cmd launcher cannot "
            "quote. Choose another path.".format(label, value)
        )
    return value.replace("%", "%%")


def build_posix_shim(config: Config) -> str:
    """Return `bin/claude-local`: a POSIX `sh` shim (LF) for the launcher core.

    The shim derives the repository root from its own location, exports it
    as an absolute `CLAUDE_LOCAL_REPO_ROOT`, and runs the recorded
    interpreter with `-I`, falling back to `py -3`, `python3`, and `python`
    after an exit-code probe. Under Git Bash, MSYS2, or Cygwin it requires a
    native Windows Python, passes Windows-form paths (`cygpath -w`), and
    disables MSYS argument conversion so prompts such as `/review` reach
    Claude Code unchanged.

    `cd` runs with `CDPATH` cleared and `--`, so an exported `CDPATH`
    cannot redirect it or make it print the directory into the captured
    root. Every managed path is embedded single-quoted, so a `$`, backtick,
    or double quote in `--local-dir` stays literal. Each probe runs with
    `PYTHON_MANAGER_AUTOMATIC_INSTALL=false` for that command only, so the
    Windows Python install manager never installs a runtime during a
    launch.
    """

    layout = config.layout
    ups = "/".join([".."] * _parent_depth(layout.launcher_relative)) or "."
    core_relative = posixpath.join(layout.local_dir_relative, LAUNCH_CORE_FILENAME)
    recorded = config.launcher_python or ""
    lines = [
        "#!/bin/sh",
        "# {}".format(SCRIPT_MARKER),
        "# Generated by {}: starts this project's pinned native Claude Code".format(
            SCRIPT_NAME
        ),
        "# through the launcher core. Do not edit; rerun {} to regenerate.".format(
            SCRIPT_NAME
        ),
        "#",
        "# All launch logic (the security-ring environment, the pinned",
        "# CLAUDE_CONFIG_DIR, self-update suppression, and A/B slot selection)",
        "# lives in the launcher core (launch.py under the runtime root). This",
        "# shim only finds a Python 3.9+ interpreter. It never falls back to a",
        "# global claude.",
        "CLAUDE_LOCAL_REPO_ROOT=$(CDPATH= cd -- "
        '"$(dirname -- "$0")/{}" && pwd) || exit 1'.format(ups),
        'claude_local_core="$CLAUDE_LOCAL_REPO_ROOT"/{}'.format(
            _sh_quote(core_relative)
        ),
        "claude_local_recorded={}".format(_sh_quote(recorded)),
        "claude_local_probe={}".format(_sh_quote(PYTHON_PROBE_POSIX)),
        'case "$(uname -s 2>/dev/null)" in',
        "  MINGW*|MSYS*|CYGWIN*)",
        "    # The launcher core needs a native Windows Python and Windows-form",
        "    # paths, and MSYS must not rewrite arguments such as /review.",
        "    claude_local_probe={}".format(_sh_quote(PYTHON_PROBE_MSYS)),
        '    CLAUDE_LOCAL_REPO_ROOT=$(cygpath -w "$CLAUDE_LOCAL_REPO_ROOT") || exit 1',
        '    claude_local_core=$(cygpath -w "$claude_local_core") || exit 1',
        '    if [ -z "${MSYS_NO_PATHCONV+x}" ] && [ -z "${MSYS2_ARG_CONV_EXCL+x}" ]; then',
        "      CLAUDE_LOCAL_MSYS_ARGS=1",
        "      export CLAUDE_LOCAL_MSYS_ARGS",
        "    fi",
        "    MSYS_NO_PATHCONV=1",
        "    MSYS2_ARG_CONV_EXCL='*'",
        "    export MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL",
        "    ;;",
        "esac",
        "export CLAUDE_LOCAL_REPO_ROOT",
        "# The probes never let the Windows Python install manager install a",
        "# runtime; the launch itself keeps the caller's setting.",
        'if [ -n "$claude_local_recorded" ] && PYTHON_MANAGER_AUTOMATIC_INSTALL=false '
        '"$claude_local_recorded" -I -c "$claude_local_probe" >/dev/null 2>&1; then',
        '  exec "$claude_local_recorded" -I "$claude_local_core" "$@"',
        "fi",
        "if command -v py >/dev/null 2>&1 && PYTHON_MANAGER_AUTOMATIC_INSTALL=false "
        'py -3 -I -c "$claude_local_probe" >/dev/null 2>&1; then',
        '  exec py -3 -I "$claude_local_core" "$@"',
        "fi",
        "for claude_local_python in python3 python; do",
        '  if command -v "$claude_local_python" >/dev/null 2>&1 && '
        "PYTHON_MANAGER_AUTOMATIC_INSTALL=false "
        '"$claude_local_python" -I -c "$claude_local_probe" >/dev/null 2>&1; then',
        '    exec "$claude_local_python" -I "$claude_local_core" "$@"',
        "  fi",
        "done",
        'echo "claude-local: no Python 3.9+ interpreter was found (tried the recorded '
        'interpreter, py -3, python3, and python). Rerun {} to record one." >&2'.format(
            SCRIPT_NAME
        ),
        "exit 127",
        "",
    ]
    return "\n".join(lines)


CMD_SELF_LABEL = ":claude_local_self"


def _cmd_self_label(variable: str) -> List[str]:
    """Return the trailing subroutine that records the batch file's directory.

    Outside a called label, ``%~dp0`` describes the batch file as it was
    invoked: when cmd found the file through PATH by a quoted bare name
    (``"claude"`` from a ``shell=True`` string, a Node ``exec`` string, or
    an npm script), it names the caller's current directory instead, which
    would make the shim run whatever launcher or core sits at that
    relative location. Inside a called label ``%~dp0`` always names the
    file's real directory. The label sits after the exit lines and is
    reached only by ``call`` before Claude Code starts, and the files are
    always written with CRLF and content-checked, so cmd's LF label-scan
    hazard does not apply.
    """

    return [
        CMD_SELF_LABEL,
        'set "{}=%~dp0"'.format(variable),
        "exit /b 0",
    ]


def _cmd_self_location(variable: str, name: str) -> List[str]:
    """Return the lines that call the self-location label and check its result.

    The variable is cleared first, and a shim whose label could not be
    found (a file someone saved with LF endings, or edited) refuses instead
    of resolving paths against the caller's current directory.
    """

    return [
        'set "{}="'.format(variable),
        "call {}".format(CMD_SELF_LABEL),
        "if not defined {} (".format(variable),
        "  >&2 echo {}: cannot find its own directory. Rerun {} --repair-install "
        "to regenerate it.".format(name, SCRIPT_NAME),
        "  exit /b 1",
        ")",
    ]


def build_cmd_shim(config: Config) -> str:
    """Return `bin/claude-local.cmd`: an ASCII cmd shim (CRLF when written).

    It keeps `%*` on the launch line only. The launch line ends with
    `& call exit /b %%ERRORLEVEL%%`, so cmd leaves the batch file on that
    same line: cmd re-reads a batch file by byte offset after each command,
    and an upgrade that rewrites this file while a session runs would
    otherwise make it resume in the middle of the new content. `call`
    re-expands ERRORLEVEL after Claude Code exits, so its exit code passes
    through. The `CMD_EXIT` lines after it (the TheKnowledge shim
    convention) run only if an unbalanced quote in the arguments swallowed
    that exit. The file's own directory comes from the one label after
    them (`_cmd_self_label`), never from a top-level `%~dp0`.

    The interpreter is chosen by exit-code probe: the recorded interpreter,
    then `py.exe -3`, `python3.exe`, and `python.exe`. The explicit `.exe`
    keeps cmd from running a `.bat` shim such as pyenv-win's, which would
    take over this batch file and exit 0 without launching anything. The
    probes run in an inner `setlocal` with
    `PYTHON_MANAGER_AUTOMATIC_INSTALL=false`, so the Python install manager
    never installs a runtime during an ordinary launch; `endlocal` restores
    the caller's value, unexpanded, before Claude Code starts.

    cmd re-parses the arguments on the launch line (double quotes, `%`,
    `^`, `&`, and newlines), as it does for every batch file; programs
    should run `launch.py` with a Python interpreter and list arguments.
    """

    layout = config.layout
    depth = _parent_depth(layout.launcher_relative)
    ups = "\\".join([".."] * depth) or "."
    core_relative = _cmd_path_literal(
        ntpath.join(
            *PurePosixPath(layout.local_dir_relative).parts, LAUNCH_CORE_FILENAME
        ),
        "Runtime root",
    )
    recorded = cmd_safe_python_path(config.launcher_python)
    lines = [
        "@echo off",
        "rem {}".format(SCRIPT_MARKER),
        "rem Generated by {}: starts this project's pinned native Claude Code".format(
            SCRIPT_NAME
        ),
        "rem through the launcher core. Do not edit; rerun {} to regenerate.".format(
            SCRIPT_NAME
        ),
        "rem",
        "rem ASCII only, and the argument list appears only on the launch line,",
        "rem so arguments such as x)y never break parsing. That line also exits,",
        "rem so a rewrite of this file during a session is never read; the exit",
        "rem code passes through. The one label, after the exit lines, finds this",
        "rem file's directory: a quoted bare name found through PATH makes the",
        "rem top-level batch path name the caller's directory instead. cmd",
        "rem re-parses the arguments (quotes, percent signs, carets, ampersands);",
        "rem programs should run launch.py with a Python and list arguments.",
        "setlocal EnableExtensions DisableDelayedExpansion",
        'set "NoDefaultCurrentDirectoryInExePath=1"',
    ]
    lines.extend(_cmd_self_location("CLAUDE_LOCAL_SELF", "claude-local"))
    lines += [
        'for %%I in ("%CLAUDE_LOCAL_SELF%{}") do set "CLAUDE_LOCAL_REPO_ROOT=%%~fI"'.format(
            ups
        ),
        'set "CLAUDE_LOCAL_SELF="',
        'set "CLAUDE_LOCAL_CORE=%CLAUDE_LOCAL_REPO_ROOT%\\{}"'.format(core_relative),
        'set "CLAUDE_LOCAL_PROBE={}"'.format(PYTHON_PROBE_CMD),
        'set "CLAUDE_LOCAL_PY="',
        'set "CLAUDE_LOCAL_PYARG="',
        "setlocal",
        'set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"',
    ]
    if recorded is not None:
        lines.extend(
            [
                'if exist "{}" ('.format(recorded),
                '  "{0}" -I -c "%CLAUDE_LOCAL_PROBE%" 1>nul 2>nul && '
                'set "CLAUDE_LOCAL_PY={0}"'.format(recorded),
                ")",
            ]
        )
    lines.extend(
        [
            "if not defined CLAUDE_LOCAL_PY (",
            '  py.exe -3 -I -c "%CLAUDE_LOCAL_PROBE%" 1>nul 2>nul && '
            'set "CLAUDE_LOCAL_PY=py.exe" && set "CLAUDE_LOCAL_PYARG=-3"',
            ")",
            "if not defined CLAUDE_LOCAL_PY (",
            '  python3.exe -I -c "%CLAUDE_LOCAL_PROBE%" 1>nul 2>nul && '
            'set "CLAUDE_LOCAL_PY=python3.exe"',
            ")",
            "if not defined CLAUDE_LOCAL_PY (",
            '  python.exe -I -c "%CLAUDE_LOCAL_PROBE%" 1>nul 2>nul && '
            'set "CLAUDE_LOCAL_PY=python.exe"',
            ")",
            # The inner scope ends here; the chosen interpreter is carried out
            # on the same line, expanded before endlocal runs.
            'endlocal & set "CLAUDE_LOCAL_PY=%CLAUDE_LOCAL_PY%" & '
            'set "CLAUDE_LOCAL_PYARG=%CLAUDE_LOCAL_PYARG%"',
            "if not defined CLAUDE_LOCAL_PY (",
            "  >&2 echo claude-local: no Python 3.9+ interpreter was found. Tried the "
            "recorded interpreter, py.exe -3, python3.exe, and python.exe.",
            "  >&2 echo claude-local: Rerun {} to record one.".format(SCRIPT_NAME),
            "  exit /b 127",
            ")",
            '"%CLAUDE_LOCAL_PY%" %CLAUDE_LOCAL_PYARG% -I "%CLAUDE_LOCAL_CORE%" %* '
            "& call exit /b %%ERRORLEVEL%%",
            'set "CMD_EXIT=%ERRORLEVEL%"',
            "exit /b %CMD_EXIT%",
        ]
    )
    lines.extend(_cmd_self_label("CLAUDE_LOCAL_SELF"))
    lines.append("")
    text = "\n".join(lines)
    return _require_ascii(text, "cmd launcher content")


def path_shim_relative_target(layout: Layout, cmd: bool) -> str:
    """Return the launcher path relative to the PATH shim directory."""

    shim_dir = posixpath.join(layout.local_dir_relative, PATH_SHIM_DIRNAME)
    target = layout.launcher_cmd_relative if cmd else layout.launcher_relative
    return posixpath.relpath(target, shim_dir)


def build_path_shim_sh(config: Config) -> str:
    """Return `<runtime root>/shim/claude`, a sh shim that re-enters the launcher.

    The launcher core puts the shim directory first on PATH, so hooks,
    scripts, `claude -p`, and background respawns run this project's pinned
    binary with this project's environment instead of a global `claude`.
    """

    relative = path_shim_relative_target(config.layout, cmd=False)
    return "\n".join(
        [
            "#!/bin/sh",
            "# {}".format(SCRIPT_MARKER),
            "# Nested `{}` calls re-enter this project's launcher; generated by".format(
                PATH_SHIM_NAME
            ),
            "# {}. Do not edit.".format(SCRIPT_NAME),
            'exec "$(dirname -- "$0")"/{} "$@"'.format(_sh_quote(relative)),
            "",
        ]
    )


def build_path_shim_cmd(config: Config) -> str:
    """Return `<runtime root>/shim/claude.cmd`, re-entering the cmd launcher.

    The launch line hands control to the launcher shim without `call`, so
    the arguments are not re-parsed a second time and the launcher's own
    CMD_EXIT sets the exit code. The shim's directory comes from a called
    label (`_cmd_self_label`), because a top-level `%~dp0` names the
    caller's current directory when cmd found this file through PATH by a
    quoted bare name. On native Windows the launcher puts the selected
    slot's `claude.exe` ahead of this directory on PATH, so this file
    serves `claude.cmd` callers and the fallback when the slot directory
    is not used.
    """

    relative = _cmd_path_literal(
        path_shim_relative_target(config.layout, cmd=True).replace("/", "\\"),
        "Launcher path",
    )
    lines = [
        "@echo off",
        "rem {}".format(SCRIPT_MARKER),
        "rem Nested {} calls re-enter this project's launcher; generated by".format(
            PATH_SHIM_NAME
        ),
        "rem {}. Do not edit.".format(SCRIPT_NAME),
        "setlocal EnableExtensions DisableDelayedExpansion",
    ]
    lines.extend(_cmd_self_location("CLAUDE_LOCAL_SHIM_SELF", PATH_SHIM_NAME))
    lines += [
        '"%CLAUDE_LOCAL_SHIM_SELF%{}" %*'.format(relative),
        "exit /b",
    ]
    lines.extend(_cmd_self_label("CLAUDE_LOCAL_SHIM_SELF"))
    lines.append("")
    return _require_ascii("\n".join(lines), "cmd PATH shim content")


def _ring_lines(config: Config) -> List[str]:
    """Return the README section describing the security rings per OS."""

    layout = config.layout
    home = layout.claude_home_relative
    launcher = layout.launcher_relative
    default_ring = "ring 0 (shared HOME)" if config.shared_home else "ring 1"
    return [
        "## Security Rings",
        "",
        "Agent work follows the project security ring model (see",
        "`docs/specifications/security_ring_model.md` when present). This",
        "project's launcher default is {}.".format(default_ring),
        "",
        "- Ring 3: agent-managed sandbox with an ephemeral HOME (default for",
        "  build and test work; not available on native Windows).",
        "- Ring 2: sandboxed, HOME=`{}` (not available on native".format(home),
        "  Windows).",
        "- Ring 1: unsandboxed, HOME=`{}`. On native Windows".format(home),
        "  the launcher also points USERPROFILE, HOMEDRIVE/HOMEPATH, APPDATA,",
        "  and LOCALAPPDATA there, because native programs ignore HOME.",
        "- Ring 0: unsandboxed, real HOME. Last resort; run",
        "  `{} --shared-home` or set `CLAUDE_LOCAL_SHARED_HOME=1` for".format(launcher),
        "  one ring-0 session. `--isolated-home` forces ring 1.",
        "",
        "`CLAUDE_CONFIG_DIR` stays pinned to `{}` at every".format(
            layout.config_dir_relative
        ),
        "ring, so escalation never forks Claude auth or session history.",
        "",
        "Native Windows: Claude Code has no sandbox there, so the commands it",
        "runs are unsandboxed. Isolation covers Claude's state and the",
        "redirected-home tools, not per-user Windows stores (Credential",
        "Manager, the registry, `HKCU\\SOFTWARE\\Policies`). Use WSL2 or a",
        "separate Windows account for work that needs ring 3. The launcher",
        "prints this note once per session; it is currently {}.".format(
            "on" if config.windows_sandbox_warning else "off"
        ),
        "Change it with `{} --set-windows-sandbox-warning on|off .`.".format(
            SCRIPT_NAME
        ),
        "",
        "Shared at ring 1 on every OS, as on Linux: OpenSSH keys, config, and",
        "agent (Windows OpenSSH finds `~\\.ssh` from the account profile, not",
        "`USERPROFILE`), programs that ask Windows for known folders, the",
        "PowerShell module path, machine-wide managed settings, and",
        "credentials passed in environment variables. The launcher names any",
        "`ANTHROPIC_API_KEY`-style credential variables it inherits, because",
        "Claude Code prefers them to this project's own login.",
        "",
    ]


def build_local_readme_content(config: Config, *, legacy_pending: bool = False) -> str:
    """Build the generated project-local operator README."""

    layout = config.layout
    launcher = layout.launcher_relative
    cmd_launcher = layout.launcher_cmd_relative.replace("/", "\\")
    lines = [
        README_MARKER,
        "# Local Claude Code Start Guide",
        "",
        "This file was generated by `{}` and is intended for local operator use.".format(
            SCRIPT_NAME
        ),
        "It is ignored on purpose and should not be committed.",
        "",
        "This project runs a pinned native Claude Code release that",
        "`{}` downloaded from Anthropic's release distribution and".format(SCRIPT_NAME),
        "verified against signed manifests. Node.js and npm are not used.",
        "",
    ]
    if legacy_pending:
        lines.extend(
            [
                "**Migration pending:** this project still runs the Claude Code",
                "binary from its npm-era layout. Run `{} .` (or".format(SCRIPT_NAME),
                "`--upgrade` / `--repair-install`) to move it to the native runtime.",
                "",
            ]
        )
    lines.extend(
        [
            "## Start Claude Code",
            "",
            "macOS, Linux, WSL, and Git Bash:",
            "",
            "```sh",
            "./{}".format(launcher),
            "./{} --continue".format(launcher),
            "./{} --resume".format(launcher),
            "```",
            "",
            "Windows cmd.exe and PowerShell (both run `{}`):".format(cmd_launcher),
            "",
            "```bat",
            ".\\{}".format(launcher.replace("/", "\\")),
            "```",
            "",
            "The launchers need a Python 3.9+ interpreter (the one that ran",
            "`{}` is recorded; `py -3`, `python3`, and `python` are".format(
                SCRIPT_NAME
            ),
            "fallbacks). They never fall back to a global `claude`.",
            "",
            "cmd.exe re-parses the arguments of every `.cmd` file, so a prompt",
            'passed to `{}` that contains `"`, `%`, `^`, or `&`'.format(cmd_launcher),
            "can change. Programs that start Claude Code with arbitrary",
            "arguments should run `{}` with a Python 3.9+".format(
                posixpath.join(config.layout.local_dir_relative, LAUNCH_CORE_FILENAME)
            ),
            "interpreter (`python -I <path> <args...>`) and an argument list.",
            "Inside a session on Windows, a nested `claude` runs this project's",
            "`claude.exe` directly.",
            "",
            "## Managed Paths",
            "",
            "- Runtime root (A/B slots, launcher core): `{}`".format(
                layout.local_dir_relative
            ),
            "- Local Claude HOME (rings 1-2): `{}`".format(layout.claude_home_relative),
            "- Pinned CLAUDE_CONFIG_DIR: `{}`".format(layout.config_dir_relative),
            "- Launchers: `{}` and `{}`".format(launcher, layout.launcher_cmd_relative),
            "- This README: `{}`".format(layout.readme_relative),
            "",
            "## Installed Version",
            "",
            "- Channel: `{}`".format(config.claude_channel or "unknown"),
            "- Requested version: `{}`".format(config.claude_selector),
            "- Installed Claude Code version: `{}`".format(
                config.claude_version or "not installed"
            ),
            "- Version source: `{}`".format(config.version_source),
            "- HOME mode: `{}`".format(
                "shared user HOME"
                if config.shared_home
                else "project-local isolated HOME"
            ),
            "- Reasonable permissions default: `{}`".format(
                "enabled" if config.reasonable_permissions_enabled else "disabled"
            ),
            "- Update checks: `{}`".format("on" if config.update_checks else "off"),
            "- Known stable version: `{}`".format(
                config.available_versions.get("stable") or "unavailable"
            ),
            "- Known latest version: `{}`".format(
                config.available_versions.get("latest") or "unavailable"
            ),
            "",
        ]
    )
    lines.extend(_ring_lines(config))
    lines.extend(
        [
            "## Updates Are Managed Here",
            "",
            "Claude Code's own updater is disabled in three layers (environment,",
            "`--settings`, and `{}/settings.json`).".format(layout.config_dir_relative),
            "`claude update`, `claude upgrade`, and `claude install` are",
            "intercepted by the launcher. If Claude Code itself says updates are",
            'disabled and to "contact your administrator", that message is',
            "misleading here: this project's runtime is managed by",
            "`{}`; use the commands below instead.".format(SCRIPT_NAME),
            "",
            "```sh",
            "{} --update .                       # refresh known versions".format(
                SCRIPT_NAME
            ),
            "{} --upgrade --channel stable .     # install current stable".format(
                SCRIPT_NAME
            ),
            "{} --upgrade --channel latest .     # install current latest".format(
                SCRIPT_NAME
            ),
            "{} --upgrade --channel stable --version {} .".format(
                SCRIPT_NAME, VERSION_FLOOR
            ),
            "{} --rollback .                     # switch to the previous slot".format(
                SCRIPT_NAME
            ),
            "{} --repair-install .               # re-verify and repair".format(
                SCRIPT_NAME
            ),
            "./{} --check-updates              # check now and print the result".format(
                launcher
            ),
            "```",
            "",
            "The launcher prints a one-line notice when a newer release exists on",
            "the installed channel; nothing installs automatically. A release that",
            "only the unsigned channel pointer names is marked unverified until",
            "`{} --update` or `--upgrade` checks its signed manifest. Turn".format(
                SCRIPT_NAME
            ),
            "the checks off with `{} --set-update-checks off .`.".format(SCRIPT_NAME),
            "",
            "Each project keeps at most two releases: the active slot and the",
            "rollback slot. If the active slot is damaged, the launcher warns and",
            "runs the other slot; `--repair-install` fixes the runtime.",
            "",
            "## Managed Permission Default",
            "",
            "The launcher can store one opt-in reasonable-permissions default.",
            "Current state: `{}`.".format(
                "enabled" if config.reasonable_permissions_enabled else "disabled"
            ),
            "",
            "When enabled, the wrapper adds `--permission-mode acceptEdits` only",
            "when you did not already provide an explicit permission choice.",
            "This keeps ordinary workspace edits flowing while Bash commands and",
            "ACP operations such as commit and push still prompt for approval.",
            "",
            "Explicit `--permission-mode` and `--dangerously-skip-permissions`",
            "always win. The wrapper never injects",
            "`--dangerously-skip-permissions`.",
            "",
            "```sh",
            "{} --set-reasonable-permissions .".format(SCRIPT_NAME),
            "{} --clear-reasonable-permissions .".format(SCRIPT_NAME),
            "```",
            "",
            "## Launcher Preflight",
            "",
            "Claude Code does not need Node.js. When the project declares a",
            "checked-in Node.js selector (`.nvmrc`, `.node-version`, or",
            "`.tool-versions`), the launcher warns if the `node` on PATH does not",
            "match it, because project tooling started from Claude Code may.",
            "",
            "- `CLAUDE_LOCAL_PREFLIGHT=warn` (default)",
            "- `CLAUDE_LOCAL_PREFLIGHT=strict` (refuse on an explicit mismatch)",
            "- `CLAUDE_LOCAL_PREFLIGHT=off`",
            "",
            "## Inspect and Self-Test",
            "",
            "```sh",
            "{} --inspect .".format(SCRIPT_NAME),
            "{} --selftest .".format(SCRIPT_NAME),
            "```",
            "",
            "## Uninstall Managed Setup",
            "",
            "```sh",
            "{} --uninstall .".format(SCRIPT_NAME),
            "```",
            "",
            "Uninstall refuses while anything runs from the runtime;",
            "`--stop-background-sessions` stops the project's background service",
            "first (this also ends its background sessions).",
            "",
            "## Older claude-wrangler Releases",
            "",
            "After a project moves to the native runtime, use an npm-era",
            "claude-wrangler release only for `--uninstall` (it may need",
            "`--force`). Its other operations would rewrite this launcher into",
            "the old Node-based form.",
            "",
        ]
    )
    return "\n".join(lines)


def build_install_summary(config: Config, runtime_summary: str) -> str:
    """Return the operator summary shown after install-like operations."""

    layout = config.layout
    lines = [
        "Operation complete.",
        "",
        "Project root: {}".format(config.project_root),
        "Operation: {}".format(config.operation),
        "Channel: {}".format(config.claude_channel or "unknown"),
        "Requested version: {}".format(config.claude_selector),
        "Installed Claude Code version: {}".format(
            config.claude_version or "not installed"
        ),
        "Version source: {}".format(config.version_source),
    ]
    if config.release_base_url and config.release_base_url != DEFAULT_RELEASE_BASE_URL:
        lines.append(
            "Release host: {} (not the default)".format(config.release_base_url)
        )
    lines += [
        "Runtime: {}".format(runtime_summary),
        "Runtime root: {}".format(layout.local_dir),
        "Metadata file: {}".format(layout.metadata_path),
        "Launchers: {} and {}".format(layout.launcher_path, layout.launcher_cmd_path),
        "Local README: {}".format(layout.readme_path),
        "Reasonable permissions default: {}".format(
            "enabled" if config.reasonable_permissions_enabled else "disabled"
        ),
        "HOME mode: {}".format(
            "shared user HOME" if config.shared_home else "project-local isolated HOME"
        ),
        "Pinned CLAUDE_CONFIG_DIR: {}".format(layout.config_dir),
        "",
        "Recommended usage:",
        "  {}".format(layout.launcher_path),
        "  {} --continue".format(layout.launcher_path),
        "  {} --resume".format(layout.launcher_path),
    ]
    return "\n".join(lines)


def build_available_versions_table(
    available_versions: Dict[str, Optional[str]],
    details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Render the known channel versions as a simple fixed-width table."""

    details = details or {}
    rows: List[Tuple[str, str, str]] = [("Channel", "Known version", "Build date")]
    for channel in CLAUDE_CHANNELS:
        entry = details.get(channel) or {}
        rows.append(
            (
                channel,
                available_versions.get(channel) or "unavailable",
                str(entry.get("build_date") or "-"),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(3)]
    rendered = []
    for row_index, row in enumerate(rows):
        rendered.append(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        )
        if row_index == 0:
            rendered.append("  ".join("-" * width for width in widths))
    return "\n".join(line.rstrip() for line in rendered)


def build_known_versions(config: Config) -> Dict[str, Any]:
    """Return the install-channel defaults and locally known versions."""

    return {
        "default_install_selector": DEFAULT_INSTALL_CLAUDE_SELECTOR,
        "channels": list(CLAUDE_CHANNELS),
        "available_versions": dict(config.available_versions),
        "available_versions_updated_at": config.available_versions_updated_at,
        "available_version_details": dict(config.available_version_details),
        "last_update_check": config.last_update_check,
    }
