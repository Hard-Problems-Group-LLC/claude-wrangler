"""Standalone launcher core for claude-wrangler's project-local Claude Code.

claude-wrangler copies this file verbatim to ``<runtime root>/launch.py``
next to a generated ``launch.json``. The generated ``bin/claude-local``
(POSIX ``sh``) and ``bin/claude-local.cmd`` shims, and the nested-``claude``
PATH shims under ``<runtime root>/shim/``, run it with a Python 3.9+
interpreter. A project must keep launching after claude-wrangler itself is
uninstalled, so this module imports only the standard library and nothing
from the ``claude_wrangler`` package, and it must stay a single file.

It implements the launch-time half of
``docs/specifications/cross_platform_launcher.md``: ring selection and the
per-OS ring environment (``security_ring_model.md``), config-dir pinning,
three-layer self-update suppression, the project settings scan, interception
of ``update``/``upgrade``/``install``, caller ``--settings`` refusal, cheap
A/B slot validation, including the verification stamp, with launch-time
fallback (``ab_runtime_slots.md`` requirements 8 and 15), notify-only
update checks
(``native_runtime_distribution_and_verification.md`` requirement 12), the
reasonable-permissions default, the warn-only Node.js parity check, the
native-Windows sandbox warning, the nested-``claude`` PATH order, the
handling of another Claude Code session's variables, the removal of
inherited credentials (unless the project turned credential passthrough
on), private session directories (mode 0700, or a private DACL on
Windows), and process semantics (including the Windows kill-on-close
job).

Several constants and the host platform detector intentionally duplicate
package code (``slots.py``, ``platforms.py``, ``distribution.py``). Keep
both sides in sync when either changes; the duplication is the price of the
launcher's independence from the installed package.

Everything is written as small functions plus ``main()`` with injectable
OS facts, environment, process hooks, network fetch, and clock, so tests
can drive every branch on any operating system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import glob
import json
import ntpath
import os
import platform
import posixpath
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

LAUNCH_CONFIG_NAME = "launch.json"
LAUNCH_CONFIG_SCHEMA = 1
MAX_LAUNCH_CONFIG_BYTES = 1024 * 1024

# Mirrors claude_wrangler.slots: SLOT_NAMES, POINTER_NAME,
# COMPLETION_RECORD_NAME, and the verification stamp, plus the slots/
# directory under the runtime root.
SLOT_NAMES = ("a", "b")
POINTER_NAME = "active-slot"
COMPLETION_RECORD_NAME = ".complete.json"
VERIFICATION_STAMP_NAME = ".verified.json"
VERIFICATION_STAMP_SCHEMA = 1
SLOTS_DIR_NAME = "slots"
MAX_COMPLETION_RECORD_BYTES = 1024 * 1024
MAX_VERIFICATION_STAMP_BYTES = 64 * 1024

SHIM_DIR_NAME = "shim"
FALLBACK_RECORD_NAME = "fallback.json"
UPDATE_CHECK_NAME = "update-check.json"
MAX_UPDATE_CHECK_BYTES = 64 * 1024
MAX_SETTINGS_BYTES = 4 * 1024 * 1024

# Mirrors claude_wrangler.distribution.DEFAULT_RELEASE_BASE_URL and
# SUPPORTED_CHANNELS. The launcher only ever reads the unsigned channel
# pointer, and only to word an advisory notice.
DEFAULT_RELEASE_BASE_URL = "https://downloads.claude.ai/claude-code-releases"
UPDATE_CHECK_ALLOWED_HOST = "downloads.claude.ai"
SUPPORTED_CHANNELS = ("stable", "latest")
UPDATE_CHECK_TTL_SECONDS = 24 * 60 * 60
UPDATE_CHECK_BUDGET_SECONDS = 3.0
# A cached check stamped this far in the future is treated as stale, so a
# clock that jumped backward cannot suppress checks for days.
UPDATE_CHECK_CLOCK_SKEW_SECONDS = 300
MAX_CHANNEL_POINTER_BYTES = 64

# The floor every ring must export, whatever launch.json adds on top.
REQUIRED_SUPPRESSION_ENV = (
    ("DISABLE_UPDATES", "1"),
    ("DISABLE_AUTOUPDATER", "1"),
    ("DISABLE_INSTALLATION_CHECKS", "1"),
)
NONESSENTIAL_TRAFFIC_VAR = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
INTERCEPTED_SUBCOMMANDS = ("update", "upgrade", "install")
# Subcommands whose operands can be one of those words (`plugin install
# <name>`). They end the interception scan when the completion record lists
# no subcommands: the npm-era layout, or a record without the probe.
FALLBACK_SCAN_STOPS = ("mcp", "plugin", "plugins", "config")
PROJECT_SETTINGS_FILES = ("settings.json", "settings.local.json")

# How CLAUDE_CONFIG_DIR is spelled. "forward-slash" reproduces, on Windows,
# the value a Git Bash launcher exported (C:/.../.local/claude) so a project
# migrated from the npm-era layout keeps the byte-identical string Claude
# Code keys some per-config-dir state on. Mirrors claude_wrangler.constants.
CONFIG_DIR_SPELLING_NATIVE = "native"
CONFIG_DIR_SPELLING_FORWARD_SLASH = "forward-slash"
CONFIG_DIR_SPELLINGS = (CONFIG_DIR_SPELLING_NATIVE, CONFIG_DIR_SPELLING_FORWARD_SLASH)

MANAGED_BY_VAR = "CLAUDE_LOCAL_MANAGED_BY"
MANAGED_BY_VALUE = "claude-wrangler"
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
SHARED_HOME_VAR = "CLAUDE_LOCAL_SHARED_HOME"
REPO_ROOT_VAR = "CLAUDE_LOCAL_REPO_ROOT"
PREFLIGHT_VAR = "CLAUDE_LOCAL_PREFLIGHT"
# Set in the child environment to the pinned config dir so nested launches
# of the same project (hooks, `claude -p`, respawns through the PATH shim)
# skip once-per-session notices; another project's launcher sees a
# different value and still shows its own.
NOTICES_SHOWN_VAR = "CLAUDE_LOCAL_NOTICES_SHOWN"
# Set by the POSIX shim under Git Bash/MSYS2 when it had to disable MSYS
# argument conversion (so a prompt such as "/review" is not rewritten into
# a Windows path); the core then removes that override from the child
# environment so commands Claude Code runs keep the normal MSYS behavior.
MSYS_ARGS_VAR = "CLAUDE_LOCAL_MSYS_ARGS"
MSYS_ARG_CONVERSION_VARS = ("MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL")
# Helper variables the generated cmd shims set for themselves inside their
# setlocal scope; Claude Code's environment never needs them.
SHIM_INTERNAL_VARS = (
    "CLAUDE_LOCAL_CORE",
    "CLAUDE_LOCAL_PY",
    "CLAUDE_LOCAL_PYARG",
    "CLAUDE_LOCAL_PROBE",
    "CLAUDE_LOCAL_SELF",
    "CLAUDE_LOCAL_SHIM_SELF",
)
# What a running Claude Code session exports to the processes it starts:
# its identity, its IPC endpoint and token, and its secure-storage key. A
# project session started from another Claude Code session (another
# project's, or the operator's global one) must not inherit them, or it
# behaves as that session's child; a nested launch of the same project
# keeps them, as Claude Code intends for its own children.
PARENT_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
)
PARENT_SESSION_PREFIXES = ("CLAUDE_CODE_MESSAGING_",)
# Set by IDE terminals for Claude Code's IDE integration (the SSE port), or
# by Agent SDK and IDE hosts for the CLI they start as well as by a Claude
# Code session for its children (the entry point and effort). They are kept
# unless CLAUDECODE shows the launch came from another Claude Code session,
# whose IDE connection and settings are not this project's.
PARENT_SESSION_GATED_VARS = (
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_EFFORT",
)
# Credentials, account identity, and provider or endpoint selection that
# Claude Code reads from the environment ahead of the project's own login in
# the pinned config dir (code.claude.com/docs/en/env-vars and
# /authentication): every ANTHROPIC_* variable (API keys, auth tokens,
# custom headers, base URLs, profiles, organization, workspace, and
# federation IDs, model choices), Claude Code's OAuth and API-key
# variables, the Bedrock API key, the mTLS client certificate, and the
# provider switches. Matching by prefix also covers variables a later
# Claude Code release adds. Unless the project turned credential
# passthrough on, the launcher removes every inherited one; either way it
# names the ones that were set (never their values) once per session.
CREDENTIAL_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_OAUTH_")
CREDENTIAL_ENV_VARS = (
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    # In 2.1.296's own provider-switch list, though not yet documented.
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_GATEWAY",
    # Credentials and account identity a Claude host app or a remote
    # session hands the CLI (2.1.296's host-auth checks).
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_HOST_CREDS_FILE",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_ACCOUNT_UUID",
)
CREDENTIAL_PASSTHROUGH_FLAG = "--set-credential-passthrough"

SHARED_HOME_FLAG = "--shared-home"
ISOLATED_HOME_FLAG = "--isolated-home"
CHECK_UPDATES_FLAG = "--check-updates"

PREFLIGHT_MODES = ("warn", "strict", "off")
NODE_SELECTOR_FILES = (".nvmrc", ".node-version", ".tool-versions")
NODE_VERSION_TIMEOUT_SECONDS = 10.0

MESSAGE_PREFIX = "[claude-local]"
PREFLIGHT_PREFIX = "[claude-local] preflight:"
WINDOWS_CTRL_C_EXIT = 0xC000013A
SIGINT_EXIT_CODE = 130
FALLBACK_BANNER = "!" * 68
MIGRATION_PENDING_NOTE = (
    "note: migration pending: this project still runs the Claude Code binary "
    "from its npm-era layout. Run `{cmd} .`, `{cmd} --upgrade --channel "
    "{channel} .`, or `{cmd} --repair-install .` to move it to the native "
    "runtime."
)

WINDOWS_SANDBOX_WARNING = (
    "note: Claude Code has no sandbox on native Windows, so the commands it "
    "runs are unsandboxed; this project's isolation covers Claude's state "
    "and redirected-home tools, not per-user Windows stores (Credential "
    "Manager, the registry, HKCU\\SOFTWARE\\Policies, OpenSSH keys and "
    "agent). Use WSL2 or a separate Windows account for work that needs "
    "ring 3."
)
# Shown on native Windows when nested `claude` calls cannot reach the slot's
# claude.exe directly (see nested_claude_slot_dir) and fall back to the cmd
# PATH shim.
NESTED_SHIM_FALLBACK_WARNING = (
    "warning: nested `claude` calls in this session go through "
    "shim\\claude.cmd because {reason}. cmd.exe re-parses their arguments, "
    "and callers that look only for .exe files can reach another claude.exe "
    "on PATH."
)
NESTED_SHIM_REASON_LEGACY = "the npm-era layout is still in place"
NESTED_SHIM_REASON_SLOT = (
    "the active slot's directory holds more than claude.exe; run "
    "`{cmd} --repair-install .`"
)
# Shown when inherited credential variables were removed (the default).
CREDENTIAL_STRIPPED_NOTE = (
    "note: removed {names}, inherited from the environment that started this "
    "launcher, so this project uses its own login in {config}. If {subject} "
    "{belong} to this project, run `{cmd} {flag} on .` to pass {pronoun} "
    "through."
)
# Shown when the project turned credential passthrough on.
CREDENTIAL_PASSTHROUGH_NOTE = (
    "note: {names} {verb} passed through from the environment that started "
    "this launcher (credential passthrough is on). Claude Code uses {pronoun} "
    "ahead of this project's own login in {config}, so every project started "
    "this way shares that account or endpoint. Run `{cmd} {flag} off .` "
    "unless {subject} {belong} to this project."
)

# Windows security identifiers and flags for the private session-directory
# DACL (owner, SYSTEM, Administrators) and the shared-checkout check.
LOCAL_SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
# CREATOR OWNER and OWNER RIGHTS: entries that grant only the owner.
OWNER_PRINCIPAL_SIDS = ("S-1-3-0", "S-1-3-4")
BROAD_GROUP_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
}
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
# Rights that let a principal change a directory's contents or its ACL:
# add file or subdirectory, delete child, delete, write DAC, write owner,
# generic all, and generic write.
WINDOWS_WRITE_RIGHTS = (
    0x00000002  # FILE_ADD_FILE / FILE_WRITE_DATA
    | 0x00000004  # FILE_ADD_SUBDIRECTORY / FILE_APPEND_DATA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_ACL_SIZE_INFORMATION_CLASS = 2

# Windows job object settings for spawn_and_wait: the child dies when the
# launcher's job handle closes (for example when the launcher is killed),
# while processes the child starts itself are never placed in the job.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CHANNEL_POINTER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_NUMERIC_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_LITERAL_NODE_RE = re.compile(r"^v?[0-9]+(?:\.[0-9]+){0,2}$")
_TOOL_VERSIONS_NODE_RE = re.compile(r"^\s*nodejs\s+(\S+)")
_TRUTHY_SETTING_VALUES = ("1", "true", "yes", "on")

# Windows IMAGE_FILE_MACHINE_* values reported by IsWow64Process2.
_WINDOWS_MACHINE_NAMES = {0xAA64: "arm64", 0x8664: "x64", 0x014C: "x86"}
# Same aliases as claude_wrangler.platforms._MACHINE_ALIASES for 64-bit
# machines; 32-bit spellings stay unsupported.
_X64_MACHINES = ("x86_64", "x86-64", "amd64", "x64", "em64t")
_ARM64_MACHINES = ("arm64", "aarch64")
# Same file check as Anthropic's install.sh (and platforms.detect_musl),
# which decides the musl build before falling back to `ldd /bin/ls`.
_MUSL_LIBC_GLOB = "/lib/libc.musl-*.so.1"

ExecFn = Callable[[str, List[str], Dict[str, str]], Any]
SpawnFn = Callable[[List[str], Dict[str, str]], int]
FetchFn = Callable[[str, float], bytes]
NodeProbe = Callable[[Mapping[str, str], str], Tuple[Optional[str], Optional[str]]]


class LauncherError(Exception):
    """A launch-blocking condition reported to the operator with an exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        """Keep the operator-facing message and the process exit code."""

        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class LaunchConfigError(LauncherError):
    """Raised when launch.json is missing, unreadable, or fails its schema."""


class NoUsableRuntimeError(LauncherError):
    """Raised when no runtime slot passes the cheap launch-time check."""


class UnsupportedHostError(LauncherError):
    """Raised when the host has no matching Claude Code platform key."""


class UpdateCheckError(Exception):
    """Raised when the channel pointer cannot be fetched or is invalid."""


@dataclass(frozen=True)
class LaunchConfig:
    """Validated contents of launch.json, the generator-written metadata.

    Path fields hold the POSIX-separated relative path split into parts.
    """

    repo_root_from_core: Tuple[str, ...]
    config_dir: Tuple[str, ...]
    home_dir: Tuple[str, ...]
    runtime_root: Tuple[str, ...]
    shared_home_default: bool
    reasonable_permissions: bool
    windows_sandbox_warning: bool
    update_checks: bool
    channel: str
    suppression_env: Dict[str, str]
    settings_override: Dict[str, Any]
    wrangler_command: str
    release_base_url: str = DEFAULT_RELEASE_BASE_URL
    config_dir_spelling: str = CONFIG_DIR_SPELLING_NATIVE
    legacy_binary: Optional[Tuple[str, ...]] = None
    legacy_version: Optional[str] = None
    # Off unless the project opted in: inherited credential variables are
    # removed from Claude Code's environment.
    credential_passthrough: bool = False


@dataclass(frozen=True)
class RepoPaths:
    """Managed paths for one launch, spelled for the environment and the host.

    The plain fields are joined with the target OS separator onto the chosen
    repository-root spelling and are what the child environment sees. The
    ``*_fs`` fields are joined with the host's ``os.path`` for filesystem
    access; they differ only when tests inject a foreign ``os_name``.
    """

    repo_root: str
    config_dir: str
    home_dir: str
    runtime_root: str
    shim_dir: str
    config_dir_fs: str
    home_dir_fs: str
    runtime_root_fs: str


@dataclass(frozen=True)
class LauncherArguments:
    """Launcher-only flags consumed from the command line, plus the rest."""

    shared_home: Optional[bool]
    check_updates: bool
    forwarded: Tuple[str, ...]


@dataclass(frozen=True)
class SlotCheck:
    """Outcome of the cheap launch-time validation of one runtime slot."""

    name: str
    ok: bool
    reason: str
    version: Optional[str]
    binary: Optional[str]
    record: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class FallbackEvent:
    """Why the launcher is not running the slot the pointer names."""

    from_slot: Optional[str]
    from_version: Optional[str]
    to_slot: str
    reason: str


@dataclass(frozen=True)
class SlotSelection:
    """The slot chosen for this launch and, when relevant, the fallback."""

    slot: str
    version: str
    binary: str
    record: Dict[str, Any]
    fallback: Optional[FallbackEvent]


@dataclass(frozen=True)
class PreflightResult:
    """Messages from the Node.js parity check and whether strict mode failed."""

    messages: Tuple[str, ...]
    failed: bool


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""

    return datetime.now(timezone.utc)


def format_timestamp(moment: datetime) -> str:
    """Render an aware datetime as an ISO 8601 UTC timestamp ending in Z."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp (``Z`` or offset form); None if invalid."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Python 3.9's fromisoformat does not accept a trailing Z.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _emit(stream: Any, message: str) -> None:
    """Write one line to an operator stream without ever failing the launch."""

    if stream is None:
        # pythonw and some service hosts run with sys.stderr set to None.
        return
    try:
        stream.write(message + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        try:
            stream.write(
                (message + "\n")
                .encode(encoding, "backslashreplace")
                .decode(encoding, "replace")
            )
        except (OSError, ValueError, LookupError):
            return
    except (OSError, ValueError):
        return
    try:
        stream.flush()
    except (OSError, ValueError):
        return


def _read_limited(path: str, limit: int) -> bytes:
    """Read a whole file, refusing files larger than ``limit`` bytes."""

    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise OSError("{} is larger than {} bytes".format(path, limit))
    return data


def _write_json_atomic(path: str, payload: Mapping[str, Any]) -> None:
    """Write JSON with LF endings via a same-directory temp file and replace."""

    directory = os.path.dirname(path) or "."
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    handle, temp_path = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def env_lookup(env: Mapping[str, str], name: str, os_name: str) -> Optional[str]:
    """Look up an environment variable, case-insensitively on Windows."""

    if name in env:
        return env[name]
    if os_name == "nt":
        wanted = name.upper()
        for key, value in env.items():
            if key.upper() == wanted:
                return value
    return None


def env_set(env: Dict[str, str], name: str, value: str, os_name: str) -> None:
    """Set an environment variable, dropping case variants on Windows.

    Windows treats ``Path`` and ``PATH`` as one variable; a child
    environment block carrying both is undefined behavior.
    """

    if os_name == "nt":
        wanted = name.upper()
        for key in [key for key in env if key.upper() == wanted and key != name]:
            del env[key]
    env[name] = value


def env_pop(env: Dict[str, str], name: str, os_name: str) -> None:
    """Remove a variable, matching case-insensitively on Windows."""

    if os_name == "nt":
        wanted = name.upper()
        for key in [key for key in env if key.upper() == wanted]:
            del env[key]
    else:
        env.pop(name, None)


def is_absolute_windows_path(value: str) -> bool:
    """Return True for ``X:\\...`` and UNC paths, which never depend on cwd.

    ``C:`` and ``C:foo`` are relative to drive C's current directory, and a
    path starting with a single backslash is relative to the current drive,
    so none of them qualify.
    """

    drive, tail = ntpath.splitdrive(value)
    if not drive:
        return False
    if drive.startswith(("\\\\", "//")):
        return True
    return tail[:1] in ("\\", "/")


def _target_sep(os_name: str) -> str:
    """Return the path separator for the target operating system."""

    return "\\" if os_name == "nt" else "/"


def spelled_join(root: str, parts: Sequence[str], os_name: str) -> str:
    """Join relative parts onto a root spelling with the target separator.

    The root spelling is kept byte-for-byte because Claude Code keys some
    per-config-dir state on the exact ``CLAUDE_CONFIG_DIR`` string.
    """

    sep = _target_sep(os_name)
    if not parts:
        return root
    trailing = ("\\", "/") if os_name == "nt" else ("/",)
    base = root if root.endswith(trailing) else root + sep
    return base + sep.join(parts)


def _split_relative(value: Any, *, allow_parent: bool) -> Optional[Tuple[str, ...]]:
    """Split a POSIX-separated relative path; None when unsafe or malformed.

    Backslashes, NUL, leading slashes, and drive or UNC prefixes are
    rejected so a value cannot change meaning between Windows and POSIX.
    """

    if not isinstance(value, str) or not value:
        return None
    if "\\" in value or "\x00" in value or value.startswith("/"):
        return None
    if ntpath.splitdrive(value)[0]:
        return None
    parts = tuple(part for part in value.split("/") if part not in ("", "."))
    if not allow_parent and ".." in parts:
        return None
    return parts


# ---------------------------------------------------------------------------
# launch.json
# ---------------------------------------------------------------------------


def _config_relative(
    data: Mapping[str, Any], key: str, *, allow_parent: bool, allow_empty: bool
) -> Tuple[str, ...]:
    """Validate one relative-path field of launch.json."""

    parts = _split_relative(data.get(key), allow_parent=allow_parent)
    if parts is None or (not parts and not allow_empty):
        raise LaunchConfigError(
            "launch.json field {} must be a relative path with / separators "
            "{}: {!r}".format(
                key,
                "" if allow_parent else "inside the repository",
                data.get(key),
            )
        )
    return parts


def _config_bool(data: Mapping[str, Any], key: str) -> bool:
    """Validate one boolean field of launch.json (integers are not booleans)."""

    value = data.get(key)
    if type(value) is not bool:
        raise LaunchConfigError(
            "launch.json field {} must be true or false: {!r}".format(key, value)
        )
    return value


def _config_env_mapping(value: Any, label: str) -> Dict[str, str]:
    """Validate an environment mapping that must carry the suppression floor."""

    if not isinstance(value, dict):
        raise LaunchConfigError("launch.json field {} must be an object".format(label))
    result: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _ENV_NAME_RE.match(key):
            raise LaunchConfigError(
                "launch.json field {} has an invalid variable name: {!r}".format(
                    label, key
                )
            )
        if not isinstance(item, str) or "\x00" in item:
            raise LaunchConfigError(
                "launch.json field {}.{} must be a string".format(label, key)
            )
        result[key] = item
    for key, required in REQUIRED_SUPPRESSION_ENV:
        if result.get(key) != required:
            raise LaunchConfigError(
                "launch.json field {} must set {}={}".format(label, key, required)
            )
    return result


def parse_launch_config(data: Any) -> LaunchConfig:
    """Validate parsed launch.json content strictly; unknown keys are ignored."""

    if not isinstance(data, dict):
        raise LaunchConfigError("launch.json must contain a JSON object")
    schema = data.get("schema")
    if type(schema) is not int or schema != LAUNCH_CONFIG_SCHEMA:
        raise LaunchConfigError(
            "launch.json schema {!r} is not supported; this launcher reads "
            "schema {}".format(schema, LAUNCH_CONFIG_SCHEMA)
        )

    channel = data.get("channel")
    if channel not in SUPPORTED_CHANNELS:
        raise LaunchConfigError(
            "launch.json field channel must be one of {}: {!r}".format(
                ", ".join(SUPPORTED_CHANNELS), channel
            )
        )

    wrangler_command = data.get("wrangler_command")
    if (
        not isinstance(wrangler_command, str)
        or not wrangler_command.strip()
        or any(char in wrangler_command for char in "\r\n\x00")
    ):
        raise LaunchConfigError(
            "launch.json field wrangler_command must be a non-empty string"
        )

    settings_override = data.get("settings_override")
    if not isinstance(settings_override, dict):
        raise LaunchConfigError("launch.json field settings_override must be an object")
    _config_env_mapping(settings_override.get("env"), "settings_override.env")

    release_base_url = data.get("release_base_url", DEFAULT_RELEASE_BASE_URL)
    if release_base_url is None:
        release_base_url = DEFAULT_RELEASE_BASE_URL
    if _release_base_host(release_base_url) is None:
        raise LaunchConfigError(
            "launch.json field release_base_url must be an https:// URL with a "
            "host and no credentials, query, or fragment: {!r}".format(release_base_url)
        )

    spelling = data.get("config_dir_spelling", CONFIG_DIR_SPELLING_NATIVE)
    if spelling is None:
        spelling = CONFIG_DIR_SPELLING_NATIVE
    if spelling not in CONFIG_DIR_SPELLINGS:
        raise LaunchConfigError(
            "launch.json field config_dir_spelling must be one of {}: {!r}".format(
                ", ".join(CONFIG_DIR_SPELLINGS), spelling
            )
        )

    legacy_binary: Optional[Tuple[str, ...]] = None
    legacy_version: Optional[str] = None
    legacy = data.get("legacy")
    if legacy is not None:
        if not isinstance(legacy, dict):
            raise LaunchConfigError("launch.json field legacy must be an object")
        legacy_binary = _config_relative(
            legacy, "binary", allow_parent=False, allow_empty=False
        )
        raw_version = legacy.get("version")
        if raw_version is not None and (
            not isinstance(raw_version, str)
            or not _CHANNEL_POINTER_RE.match(raw_version)
        ):
            raise LaunchConfigError(
                "launch.json field legacy.version must be a version or null: "
                "{!r}".format(raw_version)
            )
        legacy_version = raw_version

    return LaunchConfig(
        repo_root_from_core=_config_relative(
            data, "repo_root_from_core", allow_parent=True, allow_empty=True
        ),
        config_dir=_config_relative(
            data, "config_dir", allow_parent=False, allow_empty=False
        ),
        home_dir=_config_relative(
            data, "home_dir", allow_parent=False, allow_empty=False
        ),
        runtime_root=_config_relative(
            data, "runtime_root", allow_parent=False, allow_empty=False
        ),
        shared_home_default=_config_bool(data, "shared_home_default"),
        reasonable_permissions=_config_bool(data, "reasonable_permissions"),
        windows_sandbox_warning=_config_bool(data, "windows_sandbox_warning"),
        update_checks=_config_bool(data, "update_checks"),
        channel=channel,
        suppression_env=_config_env_mapping(
            data.get("suppression_env"), "suppression_env"
        ),
        settings_override=settings_override,
        wrangler_command=wrangler_command.strip(),
        release_base_url=release_base_url.strip().rstrip("/"),
        config_dir_spelling=spelling,
        legacy_binary=legacy_binary,
        legacy_version=legacy_version,
        # A launch.json written before the setting existed lacks the field;
        # its absence keeps the safe default (credentials removed).
        credential_passthrough=(
            _config_bool(data, "credential_passthrough")
            if "credential_passthrough" in data
            else False
        ),
    )


def _release_base_host(value: Any) -> Optional[str]:
    """Return the host of a usable https release base URL, else None."""

    if not isinstance(value, str) or not value.strip():
        return None
    if any(char in value for char in "\r\n\x00 "):
        return None
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(value.strip())
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment:
        return None
    return parts.hostname.lower().rstrip(".")


def update_check_hosts(config: LaunchConfig) -> Tuple[str, ...]:
    """Return the hosts update checks may contact (and be redirected to)."""

    hosts = [UPDATE_CHECK_ALLOWED_HOST]
    host = _release_base_host(config.release_base_url)
    if host is not None and host not in hosts:
        hosts.append(host)
    return tuple(hosts)


def load_launch_config(path: str) -> LaunchConfig:
    """Read and validate launch.json from beside the launcher core."""

    try:
        raw = _read_limited(path, MAX_LAUNCH_CONFIG_BYTES)
    except FileNotFoundError as exc:
        raise LaunchConfigError(
            "launcher metadata is missing: {}\n"
            "Run `claude-wrangler --repair-install` from the project "
            "root.".format(path)
        ) from exc
    except OSError as exc:
        raise LaunchConfigError(
            "cannot read launcher metadata {}: {}".format(path, exc)
        ) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LaunchConfigError(
            "launcher metadata {} is not valid JSON: {}\n"
            "Run `claude-wrangler --repair-install` from the project "
            "root.".format(path, exc)
        ) from exc
    try:
        return parse_launch_config(data)
    except LaunchConfigError as exc:
        raise LaunchConfigError(
            "{} ({})\nRun `claude-wrangler --repair-install` from the project "
            "root.".format(exc.message, path)
        ) from exc


# ---------------------------------------------------------------------------
# Repository root and managed paths
# ---------------------------------------------------------------------------


def derive_repo_root(core_path: str, config: LaunchConfig) -> str:
    """Derive the repository root from the core's own location."""

    core_dir = os.path.dirname(os.path.abspath(core_path))
    return os.path.normpath(os.path.join(core_dir, *config.repo_root_from_core))


def _clean_root_spelling(value: str, os_name: str) -> Optional[str]:
    """Return a shim-supplied root spelling minus trailing separators, or None.

    Only absolute spellings without ``.``/``..`` components are usable,
    because the spelling is copied verbatim into ``CLAUDE_CONFIG_DIR``. On
    Windows a rooted drive (``X:\\``) or UNC prefix is required so the
    meaning cannot depend on a current drive or directory.
    """

    if not value or any(char in value for char in "\r\n\x00"):
        return None
    if os_name == "nt":
        if not is_absolute_windows_path(value):
            return None
        components = re.split(r"[\\/]", ntpath.splitdrive(value)[1])
        trailing = "\\/"
    else:
        if not value.startswith("/"):
            return None
        components = value.split("/")
        trailing = "/"
    if any(part in (".", "..") for part in components):
        return None
    cleaned = value.rstrip(trailing)
    if os_name == "nt":
        drive, tail = ntpath.splitdrive(cleaned)
        if not tail:
            # Keep the separator after a bare drive or share root.
            return value[: len(drive) + 1]
        return cleaned
    return cleaned or "/"


def choose_repo_root(derived: str, environ: Mapping[str, str], os_name: str) -> str:
    """Choose the repository-root spelling for this launch.

    The shim's ``CLAUDE_LOCAL_REPO_ROOT`` spelling wins only when
    ``os.path.samefile`` says it is the derived root; otherwise the derived
    root is used.
    """

    candidate = env_lookup(environ, REPO_ROOT_VAR, os_name)
    if not candidate:
        return derived
    cleaned = _clean_root_spelling(candidate, os_name)
    if cleaned is None:
        return derived
    try:
        same = os.path.samefile(cleaned, derived)
    except (OSError, ValueError):
        return derived
    return cleaned if same else derived


def long_path_spelling(path: str) -> str:
    """Return an existing Windows path with its 8.3 short components expanded.

    ``bin\\claude-local.cmd`` derives the repository root with ``%~f``,
    which keeps a short spelling such as ``LONG-P~1`` when the shim was
    started through one. A binary started by that spelling reports its
    image the same way. Elsewhere, or when expansion fails, ``path`` is
    returned unchanged.
    """

    if os.name != "nt" or "~" not in path:
        return path
    try:
        import ctypes
        from ctypes import wintypes

        function = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(path, buffer, len(buffer))
    except (AttributeError, OSError, ValueError):
        return path
    if not length or length >= len(buffer):
        return path
    return buffer.value


def forward_slash_spelling(value: str) -> str:
    """Return a Windows path spelled the way Git Bash hands it to programs.

    MSYS converts ``/c/repo/.local/claude`` into ``C:/repo/.local/claude``
    for native children: forward slashes and an upper-case drive letter.
    """

    text = value.replace("\\", "/")
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text


def canonical_windows_root(value: str) -> str:
    """Return a Windows root spelling with 8.3 names expanded and drive upper.

    Claude Code keys its per-project directory, its ``.claude.json`` project
    entries, and its secure-storage name on exact path strings, so the same
    checkout reached as ``c:\\...`` or through an 8.3 short name would fork
    that state. Only the drive letter's case and short components change:
    subst drives, mapped drives, and UNC paths keep the caller's spelling,
    and nothing resolves links.
    """

    text = long_path_spelling(value)
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text


def path_entry_problem(entry: str, os_name: str) -> Optional[str]:
    """Return why ``entry`` cannot be one PATH entry, or None when it can.

    A directory whose name contains the PATH separator (``;`` on Windows,
    ``:`` elsewhere) splits into two entries, so a nested ``claude`` would
    fall through to whatever ``claude`` comes later on PATH.
    """

    separator = ";" if os_name == "nt" else ":"
    checked = entry
    if os_name != "nt" and not entry.startswith("/"):
        # Only tests that drive POSIX behavior on a Windows host see a
        # drive-letter root here; its drive colon is not a separator.
        checked = ntpath.splitdrive(entry)[1]
    if separator in checked:
        return (
            "{} contains the PATH separator {!r}, so nested `claude` calls could "
            "fall through to a global claude. Move the checkout to a path "
            "without {!r}.".format(entry, separator, separator)
        )
    return None


def build_repo_paths(repo_root: str, config: LaunchConfig, os_name: str) -> RepoPaths:
    """Compute every managed path for one launch from the chosen root."""

    runtime_root = spelled_join(repo_root, config.runtime_root, os_name)
    config_dir = spelled_join(repo_root, config.config_dir, os_name)
    if (
        os_name == "nt"
        and config.config_dir_spelling == CONFIG_DIR_SPELLING_FORWARD_SLASH
    ):
        config_dir = forward_slash_spelling(config_dir)
    return RepoPaths(
        repo_root=repo_root,
        config_dir=config_dir,
        home_dir=spelled_join(repo_root, config.home_dir, os_name),
        runtime_root=runtime_root,
        shim_dir=spelled_join(runtime_root, (SHIM_DIR_NAME,), os_name),
        config_dir_fs=os.path.join(repo_root, *config.config_dir),
        home_dir_fs=os.path.join(repo_root, *config.home_dir),
        runtime_root_fs=os.path.join(repo_root, *config.runtime_root),
    )


# ---------------------------------------------------------------------------
# Arguments, rings, and the session environment
# ---------------------------------------------------------------------------


def parse_launcher_arguments(args: Sequence[str]) -> LauncherArguments:
    """Split launcher-only flags from the arguments forwarded to Claude Code.

    Like the bash launcher, the flags are consumed wherever they appear and
    the last ring flag wins.
    """

    shared_home: Optional[bool] = None
    check_updates = False
    forwarded: List[str] = []
    for arg in args:
        if arg == SHARED_HOME_FLAG:
            shared_home = True
        elif arg == ISOLATED_HOME_FLAG:
            shared_home = False
        elif arg == CHECK_UPDATES_FLAG:
            check_updates = True
        else:
            forwarded.append(arg)
    return LauncherArguments(shared_home, check_updates, tuple(forwarded))


def select_shared_home(
    default_shared: bool,
    environ: Mapping[str, str],
    override: Optional[bool],
    os_name: str,
) -> bool:
    """Return True for ring 0 (real home) and False for ring 1 (redirected).

    Mirrors the bash ``${CLAUDE_LOCAL_SHARED_HOME:-default}`` semantics: an
    unset or empty variable means the install-time default, ``1`` means
    ring 0, and any other value means ring 1. A command-line flag wins.
    """

    if override is not None:
        return override
    raw = env_lookup(environ, SHARED_HOME_VAR, os_name)
    value = raw if raw else ("1" if default_shared else "0")
    return value == "1"


def redirected_home_environment(home: str, os_name: str) -> Dict[str, str]:
    """Return the ring-1 home variables for the target operating system.

    Native Windows programs resolve home from USERPROFILE, HOMEDRIVE and
    HOMEPATH, APPDATA, and LOCALAPPDATA and ignore HOME, so on Windows all
    of them move together; HOME alone would split Claude Code's paths
    between two homes.
    """

    pathmod = ntpath if os_name == "nt" else posixpath
    values = {"HOME": home}
    if os_name == "nt":
        drive, tail = ntpath.splitdrive(home)
        values["USERPROFILE"] = home
        values["HOMEDRIVE"] = drive
        values["HOMEPATH"] = tail or "\\"
        values["APPDATA"] = ntpath.join(home, "AppData", "Roaming")
        values["LOCALAPPDATA"] = ntpath.join(home, "AppData", "Local")
    values["XDG_CONFIG_HOME"] = pathmod.join(home, ".config")
    values["XDG_CACHE_HOME"] = pathmod.join(home, ".cache")
    values["XDG_STATE_HOME"] = pathmod.join(home, ".local", "state")
    values["XDG_DATA_HOME"] = pathmod.join(home, ".local", "share")
    return values


def prepend_path_entry(env: Dict[str, str], entry: str, os_name: str) -> None:
    """Put ``entry`` first on PATH, dropping earlier copies of it only."""

    nt = os_name == "nt"
    separator = ";" if nt else ":"

    def normalized(item: str) -> str:
        """Compare PATH entries the way the target OS resolves them."""

        if nt:
            return ntpath.normcase(item.strip('"').rstrip("\\/"))
        return item.rstrip("/") or "/"

    current = env_lookup(env, "PATH", os_name)
    kept: List[str] = []
    if current:
        wanted = normalized(entry)
        kept = [item for item in current.split(separator) if normalized(item) != wanted]
    env_set(env, "PATH", separator.join([entry] + kept), os_name)


def is_nested_launch(
    environ: Mapping[str, str], paths: RepoPaths, os_name: str
) -> bool:
    """Return True when a launcher session of this same project started us.

    The launcher exports ``CLAUDE_LOCAL_NOTICES_SHOWN`` as the pinned config
    dir; another project's launcher, or a shell outside any session, has a
    different value or none.
    """

    return env_lookup(environ, NOTICES_SHOWN_VAR, os_name) == paths.config_dir


def parent_session_variables(environ: Mapping[str, str], os_name: str) -> List[str]:
    """Return the inherited names that belong to another Claude Code session.

    These are the identity, IPC, and secure-storage variables of
    ``PARENT_SESSION_VARS`` and ``PARENT_SESSION_PREFIXES``, plus
    ``PARENT_SESSION_GATED_VARS`` when ``CLAUDECODE`` shows the caller is
    itself a Claude Code session (an IDE terminal or an SDK host sets those
    for the CLI too). Callers drop them only for a launch that is not a
    nested launch of the same project.
    """

    # Every listed name is upper case, so on Windows (where names are case
    # insensitive) comparing the upper-cased key is enough.
    exact = set(PARENT_SESSION_VARS)
    if env_lookup(environ, "CLAUDECODE", os_name):
        exact.update(PARENT_SESSION_GATED_VARS)
    names: List[str] = []
    for key in environ:
        comparable = key.upper() if os_name == "nt" else key
        if comparable in exact or comparable.startswith(PARENT_SESSION_PREFIXES):
            names.append(key)
    return names


def is_credential_variable(name: str, os_name: str) -> bool:
    """Return True for a credential, identity, or provider variable name.

    Windows environment names are case insensitive, so they are compared
    upper-cased there; POSIX names must match exactly.
    """

    comparable = name.upper() if os_name == "nt" else name
    return comparable in CREDENTIAL_ENV_VARS or comparable.startswith(
        CREDENTIAL_ENV_PREFIXES
    )


def credential_variables(environ: Mapping[str, str], os_name: str) -> List[str]:
    """Return the credential or provider variables set (non-empty) in environ."""

    return sorted(
        name
        for name, value in environ.items()
        if value and is_credential_variable(name, os_name)
    )


def credential_note(
    names: Sequence[str],
    config_dir: str,
    *,
    passthrough: bool = False,
    command: str = "claude-wrangler",
) -> str:
    """Render the once-per-session note naming inherited credential variables.

    Only the variable names appear, never their values. The note says the
    variables were removed, or, with ``passthrough``, that they were kept.
    """

    single = len(names) == 1
    template = CREDENTIAL_PASSTHROUGH_NOTE if passthrough else CREDENTIAL_STRIPPED_NOTE
    return template.format(
        names=", ".join(names),
        verb="is" if single else "are",
        pronoun="it" if single else "them",
        subject="it" if single else "they",
        belong="belongs" if single else "belong",
        config=config_dir,
        cmd=command,
        flag=CREDENTIAL_PASSTHROUGH_FLAG,
    )


def build_session_environment(
    base: Mapping[str, str],
    *,
    paths: RepoPaths,
    config: LaunchConfig,
    shared_home: bool,
    os_name: str,
    slot_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Build the child environment for the selected ring.

    At ring 0 the operator's home variables are left exactly as inherited.
    The config-dir pin, the managed-by marker, and the self-update
    suppression variables are set at every ring. Unless this is a nested
    launch of the same project, the variables of another Claude Code
    session (``parent_session_variables``) are dropped, and the cmd shims'
    own helper variables never reach Claude Code. Inherited credential,
    identity, and provider variables (``is_credential_variable``) are
    removed at every ring unless ``config.credential_passthrough`` is on,
    so the project uses its own login in the pinned config dir.

    ``PATH`` gets the nested-``claude`` shim directory first. With
    ``slot_dir`` (Windows, see ``nested_claude_slot_dir``) the selected
    slot's platform directory goes in front of it, so a nested ``claude``
    reaches the slot's ``claude.exe`` directly: CreateProcess and libuv
    search only ``.exe`` files, and a ``.cmd`` shim would make cmd.exe
    re-parse every argument.
    """

    env = dict(base)
    if env_lookup(env, MSYS_ARGS_VAR, os_name) == "1":
        for name in (MSYS_ARGS_VAR,) + MSYS_ARG_CONVERSION_VARS:
            env_pop(env, name, os_name)
    else:
        env_pop(env, MSYS_ARGS_VAR, os_name)
    for name in SHIM_INTERNAL_VARS:
        env_pop(env, name, os_name)
    if not is_nested_launch(base, paths, os_name):
        for name in parent_session_variables(env, os_name):
            env.pop(name, None)
    if not config.credential_passthrough:
        # Empty ones too: an empty provider switch is harmless, but the
        # child should see no inherited account settings at all.
        for name in [key for key in env if is_credential_variable(key, os_name)]:
            env.pop(name, None)
    if not shared_home:
        for name, value in redirected_home_environment(paths.home_dir, os_name).items():
            env_set(env, name, value, os_name)
    # Nested launches (hooks, `claude -p`, respawns through the PATH shim)
    # inherit this and keep the ring this session chose.
    env_set(env, SHARED_HOME_VAR, "1" if shared_home else "0", os_name)
    env_set(env, CONFIG_DIR_VAR, paths.config_dir, os_name)
    env_set(env, MANAGED_BY_VAR, MANAGED_BY_VALUE, os_name)
    for name, value in REQUIRED_SUPPRESSION_ENV:
        env_set(env, name, value, os_name)
    for name, value in config.suppression_env.items():
        env_set(env, name, value, os_name)
    env_set(env, NOTICES_SHOWN_VAR, paths.config_dir, os_name)
    prepend_path_entry(env, paths.shim_dir, os_name)
    if slot_dir:
        prepend_path_entry(env, slot_dir, os_name)
    return env


def nested_claude_slot_dir(
    selection: SlotSelection, config: LaunchConfig, os_name: str
) -> Optional[str]:
    """Return the slot directory to put first on PATH on Windows, or None.

    On native Windows a nested ``claude`` must reach the slot's
    ``claude.exe`` itself: CreateProcess and libuv-based spawns (Python
    list calls, Node, Bun) look only for ``.exe`` files and would skip the
    ``.cmd`` shim for the next ``claude.exe`` on PATH, and any caller that
    does reach ``claude.cmd`` has cmd.exe re-parse its arguments. The
    directory qualifies only in slot mode (the npm-era layout's directory
    holds other files) and only while it holds nothing but the binary, so a
    future release's extra executables never shadow tools session-wide.
    ``selection.binary`` is already spelled with long names, so
    maintenance's in-use check matches the image path.
    """

    if os_name != "nt" or config.legacy_binary is not None or not selection.binary:
        return None
    directory = os.path.dirname(selection.binary)
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    binary_name = os.path.normcase(os.path.basename(selection.binary))
    if [os.path.normcase(entry) for entry in entries] != [binary_name]:
        return None
    return directory


def ensure_session_directories(
    paths: RepoPaths, shared_home: bool, os_name: Optional[str] = None
) -> None:
    """Create the config dir, and the redirected home at ring 1, if missing.

    On Windows ring 1 also creates ``HOME\\AppData\\Roaming`` and
    ``HOME\\AppData\\Local``, because APPDATA and LOCALAPPDATA point
    there and native programs expect both to exist. A config dir or home
    created here is private to the operator, because it holds transcripts
    and credentials: mode 0700 on POSIX, and on Windows a protected DACL
    granting only the operator, SYSTEM, and Administrators (best effort; a
    filesystem without ACLs keeps its defaults). Existing directories are
    left as they are (maintenance tightens them).
    """

    # The config dir comes first so a home inside it never creates it with
    # the default mode as an intermediate directory.
    targets = [(paths.config_dir_fs, True)]
    if not shared_home:
        targets.append((paths.home_dir_fs, True))
        if (os.name if os_name is None else os_name) == "nt":
            targets.append(
                (os.path.join(paths.home_dir_fs, "AppData", "Roaming"), False)
            )
            targets.append((os.path.join(paths.home_dir_fs, "AppData", "Local"), False))
    for target, private in targets:
        try:
            created = not os.path.isdir(target)
            # Python 3.13+ turns mode 0700 into a restrictive ACL on Windows
            # and earlier versions do not, so Windows sets its DACL itself.
            if private and os.name != "nt" and created:
                os.makedirs(target, mode=0o700, exist_ok=True)
            else:
                os.makedirs(target, exist_ok=True)
        except OSError as exc:
            raise LauncherError(
                "cannot create managed directory {}: {}".format(target, exc)
            ) from exc
        if private and created and os.name == "nt":
            try:
                windows_set_private_dacl(target)
            except OSError:
                # FAT, exFAT, and some network shares have no ACLs to set.
                pass


# ---------------------------------------------------------------------------
# Windows ACLs (private session directories, shared-checkout detection)
# ---------------------------------------------------------------------------


def _windows_security_api() -> Tuple[Any, Any]:
    """Return prototyped private ``advapi32`` and ``kernel32`` instances.

    Private ``WinDLL`` instances keep these prototypes from leaking into
    other ``ctypes.windll`` users in the same process.
    """

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [pointer]
    kernel32.LocalFree.restype = pointer
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        pointer,
        pointer,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi32.GetAce.restype = wintypes.BOOL
    function = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    function.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.ULONG),
    ]
    function.restype = wintypes.BOOL
    return advapi32, kernel32


def _sid_string(advapi32: Any, kernel32: Any, sid: Any) -> Optional[str]:
    """Return the ``S-1-...`` form of a SID pointer, or None."""

    import ctypes
    from ctypes import wintypes

    if not sid:
        return None
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return None
    try:
        return text.value
    finally:
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def windows_current_user_sid() -> Optional[str]:
    """Return the SID of this process's user as ``S-1-...``, or None."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        advapi32, kernel32 = _windows_security_api()
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD(0)
            # The first call only reports the buffer size it needs.
            advapi32.GetTokenInformation(
                token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size)
            )
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, _TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)
            ):
                return None
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member
            # is the SID pointer.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            return _sid_string(advapi32, kernel32, sid)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, ValueError):
        return None


def windows_security_summary(
    path: str,
) -> Optional[Tuple[Optional[str], List[Tuple[int, int, int, Optional[str]]]]]:
    """Return ``(owner SID, DACL entries)`` for a path, or None when unreadable.

    Each entry is ``(ACE type, ACE flags, access mask, SID)``. A NULL DACL,
    which grants everyone everything, is reported as one allow entry for
    Everyone with every right.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        advapi32, kernel32 = _windows_security_api()
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = advapi32.GetNamedSecurityInfoW(
            path,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            return None
        try:
            owner_sid = _sid_string(advapi32, kernel32, owner.value)
            if not dacl.value:
                return owner_sid, [(ACCESS_ALLOWED_ACE_TYPE, 0, 0xFFFFFFFF, "S-1-1-0")]

            class AclSizeInformation(ctypes.Structure):
                """ACL_SIZE_INFORMATION: the ACE count and byte usage."""

                _fields_ = [
                    ("AceCount", wintypes.DWORD),
                    ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD),
                ]

            info = AclSizeInformation()
            if not advapi32.GetAclInformation(
                dacl.value,
                ctypes.byref(info),
                ctypes.sizeof(info),
                _ACL_SIZE_INFORMATION_CLASS,
            ):
                return None
            entries: List[Tuple[int, int, int, Optional[str]]] = []
            for index in range(info.AceCount):
                ace = ctypes.c_void_p()
                if not advapi32.GetAce(dacl.value, index, ctypes.byref(ace)):
                    return None
                raw = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
                ace_type, ace_flags = raw[0], raw[1]
                mask = ctypes.cast(
                    ace.value + 4, ctypes.POINTER(ctypes.c_uint32)
                ).contents.value
                sid: Optional[str] = None
                # Only the basic allow and deny ACEs keep the SID at offset 8;
                # other ACE types are reported without one.
                if ace_type in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                    sid = _sid_string(advapi32, kernel32, ace.value + 8)
                entries.append((ace_type, ace_flags, mask, sid))
            return owner_sid, entries
        finally:
            kernel32.LocalFree(descriptor)
    except (AttributeError, OSError, ValueError):
        return None


def private_dacl_principals(
    user_sid: Optional[str], owner_sid: Optional[str]
) -> List[str]:
    """Return the SIDs a private session directory grants full control.

    The operator, the directory's owner when that is an account (so another
    administrator running maintenance never locks the owner out; an owner
    can always rewrite its DACL anyway), SYSTEM, and Administrators.
    """

    principals: List[str] = []
    # An owner counts only when it is an account SID (S-1-5-21-...): a
    # directory created elevated is owned by Administrators, which is
    # listed anyway, and a broad group must never be granted.
    owner_account = (
        owner_sid if owner_sid and owner_sid.startswith("S-1-5-21-") else None
    )
    for sid in (user_sid, owner_account, LOCAL_SYSTEM_SID, ADMINISTRATORS_SID):
        if sid and sid not in principals:
            principals.append(sid)
    return principals


def dacl_is_private(
    entries: Sequence[Tuple[int, int, int, Optional[str]]], principals: Sequence[str]
) -> bool:
    """Return True when every allow entry names one of ``principals``.

    CREATOR OWNER and OWNER RIGHTS count as private too: they grant only the
    owner, who can rewrite the DACL anyway (Python 3.13 and pytest create
    private directories that way). Deny entries never widen access. An
    allow entry of another type (object or callback ACEs) cannot be judged
    here and counts as not private.
    """

    accepted = set(principals) | set(OWNER_PRINCIPAL_SIDS)
    for ace_type, _flags, _mask, sid in entries:
        if ace_type == ACCESS_DENIED_ACE_TYPE:
            continue
        if ace_type != ACCESS_ALLOWED_ACE_TYPE or sid not in accepted:
            return False
    return True


def broad_write_grants(
    entries: Sequence[Tuple[int, int, int, Optional[str]]],
) -> List[str]:
    """Return the names of broad groups an ACL lets change content or the ACL.

    Inherit-only entries count too: they grant the same rights on every
    file and directory below.
    """

    names: List[str] = []
    for ace_type, _flags, mask, sid in entries:
        if ace_type != ACCESS_ALLOWED_ACE_TYPE or sid not in BROAD_GROUP_SIDS:
            continue
        if mask & WINDOWS_WRITE_RIGHTS and BROAD_GROUP_SIDS[sid] not in names:
            names.append(BROAD_GROUP_SIDS[sid])
    return names


def windows_set_private_dacl(path: str) -> None:
    """Give ``path`` a protected DACL for the operator, SYSTEM, and Administrators.

    Inheritance from the parent is removed and the entries are inheritable
    by files and subdirectories, which Windows propagates to existing
    children. Raises OSError when the ACL cannot be set (for example on a
    filesystem without ACLs) or the operator's SID is unknown.
    """

    if os.name != "nt":
        raise OSError("Windows ACLs are only available on Windows")
    import ctypes
    from ctypes import wintypes

    user_sid = windows_current_user_sid()
    if user_sid is None:
        raise OSError("cannot determine the current user's SID")
    summary = windows_security_summary(path)
    owner_sid = summary[0] if summary is not None else None
    sddl = "D:P" + "".join(
        "(A;OICI;FA;;;{})".format(sid)
        for sid in private_dacl_principals(user_sid, owner_sid)
    )
    advapi32, kernel32 = _windows_security_api()
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        status = advapi32.SetNamedSecurityInfoW(
            path,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if status != 0:
            raise ctypes.WinError(status)
    finally:
        kernel32.LocalFree(descriptor)


# ---------------------------------------------------------------------------
# Caller arguments: interception, --settings, permissions
# ---------------------------------------------------------------------------


def intercepted_subcommand(
    args: Sequence[str], subcommands: Sequence[str] = ()
) -> Optional[str]:
    """Return the self-update subcommand the arguments could dispatch to.

    Claude Code dispatches a subcommand that follows leading root options
    (``--verbose update``, ``--model x install``) or ``--``, so any argument
    that is exactly ``update``, ``upgrade``, or ``install`` is refused,
    without an option-arity table. The scan stops at the first argument
    that is another recorded subcommand (``subcommands``, or
    ``FALLBACK_SCAN_STOPS`` when none are recorded), whose own operands may
    legitimately be such a word (``plugin install <name>``). A false
    positive (a one-word prompt such as ``-p install``, which Claude Code
    itself would dispatch) is only a clear refusal; a false negative would
    reach Claude Code's own installer. One false negative remains: an
    option value that equals a subcommand name (``--agent mcp update``)
    ends the scan early, and ``DISABLE_UPDATES`` stops the updater there.
    """

    stops = set(subcommands or FALLBACK_SCAN_STOPS) - set(INTERCEPTED_SUBCOMMANDS)
    for arg in args:
        if arg in INTERCEPTED_SUBCOMMANDS:
            return arg
        if arg in stops:
            return None
    return None


def interception_message(subcommand: str, config: LaunchConfig) -> str:
    """Explain why ``claude update``-style commands are blocked here."""

    return (
        "`claude {}` is disabled here: this project's Claude Code runtime is "
        "managed by claude-wrangler.\n"
        "Run `{} --upgrade --channel {}` from the project root to upgrade "
        "it.".format(subcommand, config.wrangler_command, config.channel)
    )


def caller_passed_settings(args: Sequence[str]) -> bool:
    """Return True when the caller supplied its own ``--settings`` flag."""

    return any(arg == "--settings" or arg.startswith("--settings=") for arg in args)


def settings_refusal_message(paths: RepoPaths, os_name: str) -> str:
    """Explain the ``--settings`` refusal and where settings belong instead."""

    return (
        "claude-local refuses a caller --settings flag: it passes its own "
        "--settings to keep Claude Code's self-update disabled.\n"
        "Put persistent settings in {} instead.".format(
            spelled_join(paths.config_dir, ("settings.json",), os_name)
        )
    )


def caller_chose_permissions(args: Sequence[str]) -> bool:
    """Detect explicit permission intent exactly as the bash launcher did."""

    for arg in args:
        if arg in ("--permission-mode", "--dangerously-skip-permissions"):
            return True
        if arg.startswith("--permission-mode="):
            return True
    return False


def record_subcommands(record: Mapping[str, Any]) -> Tuple[str, ...]:
    """Return the completion record's subcommand names; empty when absent."""

    raw = record.get("subcommands")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def record_settings_rejected(record: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    """Return the subcommands that refused ``--settings`` at install time.

    None when the record predates the install-time probe (or the field is
    unusable); the launcher then omits ``--settings`` for every recorded
    subcommand, as before.
    """

    raw = record.get("settings_rejected")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return None
    return tuple(raw)


def build_claude_arguments(
    args: Sequence[str],
    *,
    subcommands: Sequence[str],
    settings_override: Mapping[str, Any],
    reasonable_permissions: bool,
    settings_rejected: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build Claude Code's argument list (without the executable).

    ``--settings`` goes first, so command-line settings outrank a cloned
    repository's project settings for subcommands too. It is omitted only
    when the first caller argument is a subcommand that refused it during
    the install-time probe (``settings_rejected``). A slot promoted before
    that probe existed has no such list; then every recorded subcommand
    omits it. The permission default keeps the bash launcher's behavior.
    """

    prefix: List[str] = []
    first = args[0] if args else None
    if settings_rejected is not None:
        omit_settings = first is not None and first in settings_rejected
    else:
        omit_settings = first is not None and first in subcommands
    if not omit_settings:
        prefix.extend(
            [
                "--settings",
                json.dumps(settings_override, separators=(",", ":"), sort_keys=True),
            ]
        )
    if reasonable_permissions and not caller_chose_permissions(args):
        prefix.extend(["--permission-mode", "acceptEdits"])
    return prefix + list(args)


# ---------------------------------------------------------------------------
# Project settings scan
# ---------------------------------------------------------------------------


def _setting_is_truthy(value: Any) -> bool:
    """Apply Claude Code's env truthiness: 1, true, yes, or on."""

    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, str)):
        text = str(value)
    else:
        return False
    return text.strip().lower() in _TRUTHY_SETTING_VALUES


def scan_project_settings(
    repo_root_fs: str, suppression_keys: Sequence[str]
) -> List[str]:
    """Warn about project settings that fight the update suppression.

    A cloned repository's ``.claude/settings.json`` applies before the trust
    prompt, so these warnings name what it changes. Malformed files are
    reported and never block the launch.
    """

    warnings: List[str] = []
    for name in PROJECT_SETTINGS_FILES:
        label = ".claude/{}".format(name)
        path = os.path.join(repo_root_fs, ".claude", name)
        if not os.path.isfile(path):
            continue
        try:
            raw = _read_limited(path, MAX_SETTINGS_BYTES)
        except OSError as exc:
            warnings.append("cannot read {}: {}".format(label, exc))
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            warnings.append(
                "{} is not valid JSON and was not checked: {}".format(label, exc)
            )
            continue
        if not isinstance(data, dict):
            warnings.append(
                "{} is malformed: the top level is not an object".format(label)
            )
            continue
        env = data.get("env")
        if env is None:
            continue
        if not isinstance(env, dict):
            warnings.append("{} is malformed: env is not an object".format(label))
            continue
        for key in suppression_keys:
            if key in env and not _setting_is_truthy(env[key]):
                warnings.append(
                    "{} sets env.{} to {}, which would re-enable Claude Code's "
                    "self-update; claude-local overrides it on the command "
                    "line, but remove it from the project settings.".format(
                        label, key, json.dumps(env[key])
                    )
                )
        if NONESSENTIAL_TRAFFIC_VAR in env:
            warnings.append(
                "{} sets env.{} to {}; this project-level setting changes Claude "
                "Code's network behavior for every session in this checkout. "
                "Review it.".format(
                    label,
                    NONESSENTIAL_TRAFFIC_VAR,
                    json.dumps(env[NONESSENTIAL_TRAFFIC_VAR]),
                )
            )
    return warnings


# ---------------------------------------------------------------------------
# Runtime slots
# ---------------------------------------------------------------------------


def _is_within(path: str, root: str) -> bool:
    """Return True when ``path`` resolves strictly inside ``root``."""

    real_root = os.path.normcase(os.path.realpath(root))
    real_path = os.path.normcase(os.path.realpath(path))
    try:
        common = os.path.commonpath([real_root, real_path])
    except ValueError:
        return False
    return common == real_root and real_path != real_root


def _slot_failure(name: str, reason: str, version: Optional[str] = None) -> SlotCheck:
    """Build a failed SlotCheck."""

    return SlotCheck(name, False, reason, version, None, None)


def _binary_identity(status: os.stat_result) -> Dict[str, int]:
    """Return a binary's identity as the verification stamp records it.

    Mirrors ``claude_wrangler.slots.binary_identity``: size and modification
    time in nanoseconds, plus the inode on POSIX.
    """

    identity = {"size": int(status.st_size), "mtime_ns": int(status.st_mtime_ns)}
    if os.name != "nt":
        identity["ino"] = int(status.st_ino)
    return identity


def _stamp_entry(slot_dir: str, platform_key: str) -> Optional[Dict[str, Any]]:
    """Return the verification-stamp entry for one platform, or None."""

    try:
        raw = _read_limited(
            os.path.join(slot_dir, VERIFICATION_STAMP_NAME),
            MAX_VERIFICATION_STAMP_BYTES,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if type(payload.get("schema")) is not int or (
        payload.get("schema") != VERIFICATION_STAMP_SCHEMA
    ):
        return None
    binaries = payload.get("binaries")
    entry = binaries.get(platform_key) if isinstance(binaries, dict) else None
    return entry if isinstance(entry, dict) else None


def check_slot(runtime_root_fs: str, name: str, platform_key: str) -> SlotCheck:
    """Validate one slot cheaply, without hashing the binary.

    The completion record must parse and name the slot, the platform entry
    must exist, and the binary must exist inside the runtime root with the
    recorded size. The binary's identity (size, modification time, and on
    POSIX the inode) must also equal the verification stamp maintenance
    wrote right after it last verified the slot in full. A git clone or
    pull cannot carry a matching stamp, because it writes new files with a
    fresh modification time, so a binary it planted or replaced is refused.
    On Windows the stamp has no inode, so an archive or copy tool that
    preserves modification times could restore a same-size binary together
    with a matching stamp; the stamp is a tripwire against clones and
    pulls, not proof against every way files can arrive.
    """

    slot_dir = os.path.join(runtime_root_fs, SLOTS_DIR_NAME, name)
    record_path = os.path.join(slot_dir, COMPLETION_RECORD_NAME)
    try:
        raw = _read_limited(record_path, MAX_COMPLETION_RECORD_BYTES)
    except FileNotFoundError:
        return _slot_failure(name, "completion record is missing")
    except OSError as exc:
        return _slot_failure(name, "completion record is unreadable ({})".format(exc))
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _slot_failure(name, "completion record is not valid JSON")
    if not isinstance(record, dict):
        return _slot_failure(name, "completion record is not a JSON object")

    version = record.get("version")
    version = version if isinstance(version, str) and version else None
    schema = record.get("schema")
    if type(schema) is not int or schema < 1:
        return _slot_failure(name, "completion record has no valid schema", version)
    if record.get("slot") != name:
        return _slot_failure(
            name,
            "completion record names slot {!r}".format(record.get("slot")),
            version,
        )
    if version is None:
        return _slot_failure(name, "completion record has no version")
    platforms = record.get("platforms")
    if not isinstance(platforms, dict):
        return _slot_failure(name, "completion record has no platforms", version)
    entry = platforms.get(platform_key)
    if not isinstance(entry, dict):
        installed = ", ".join(sorted(str(key) for key in platforms)) or "none"
        return _slot_failure(
            name,
            "no {} binary (installed platforms: {})".format(platform_key, installed),
            version,
        )
    parts = _split_relative(entry.get("binary"), allow_parent=False)
    if not parts:
        return _slot_failure(
            name,
            "recorded binary path {!r} is not a safe relative path".format(
                entry.get("binary")
            ),
            version,
        )
    size = entry.get("size")
    if type(size) is not int or size <= 0:
        return _slot_failure(name, "recorded binary size is invalid", version)

    binary = os.path.join(slot_dir, *parts)
    # Refuse before touching the file: a symlink or junction anywhere on the
    # path must not lead execution outside the runtime root.
    if not _is_within(binary, runtime_root_fs):
        return _slot_failure(name, "binary resolves outside the runtime root", version)
    try:
        status = os.stat(binary)
    except FileNotFoundError:
        return _slot_failure(name, "binary is missing", version)
    except OSError as exc:
        return _slot_failure(name, "binary is unreadable ({})".format(exc), version)
    if not stat.S_ISREG(status.st_mode):
        return _slot_failure(name, "binary is not a regular file", version)
    if status.st_size != size:
        return _slot_failure(
            name,
            "binary is {} bytes but the record says {}".format(status.st_size, size),
            version,
        )
    if os.name != "nt" and not os.access(binary, os.X_OK):
        return _slot_failure(name, "binary is not executable", version)
    stamp = _stamp_entry(slot_dir, platform_key)
    if stamp is None:
        return _slot_failure(
            name,
            "the binary has no verification stamp (claude-wrangler has not "
            "verified this slot on this checkout)",
            version,
        )
    if any(
        stamp.get(field) != value for field, value in _binary_identity(status).items()
    ):
        return _slot_failure(
            name,
            "the binary changed after claude-wrangler last verified it",
            version,
        )
    return SlotCheck(name, True, "", version, binary, record)


def read_active_pointer(runtime_root_fs: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(slot, None)`` for a valid pointer or ``(None, problem)``."""

    path = os.path.join(runtime_root_fs, POINTER_NAME)
    try:
        raw = _read_limited(path, 16)
    except FileNotFoundError:
        return None, "the active-slot pointer is missing"
    except OSError as exc:
        return None, "the active-slot pointer is unreadable ({})".format(exc)
    for name in SLOT_NAMES:
        if raw == (name + "\n").encode("ascii"):
            return name, None
    return None, "the active-slot pointer is malformed"


def _other_slot(name: str) -> str:
    """Return the slot that is not ``name``."""

    return SLOT_NAMES[1] if name == SLOT_NAMES[0] else SLOT_NAMES[0]


def _selection(check: SlotCheck, fallback: Optional[FallbackEvent]) -> SlotSelection:
    """Turn a passing SlotCheck into a SlotSelection."""

    return SlotSelection(
        slot=check.name,
        version=check.version or "",
        binary=check.binary or "",
        record=check.record or {},
        fallback=fallback,
    )


def _no_runtime(
    runtime_root: str, details: Sequence[str], wrangler_command: str
) -> NoUsableRuntimeError:
    """Build the refusal raised when no slot can run."""

    lines = ["ERROR: no usable Claude Code runtime in {}:".format(runtime_root)]
    lines.extend("  {}".format(detail) for detail in details)
    lines.append(
        "Run `{} --repair-install` from the project root. claude-local never "
        "falls back to a global claude.".format(wrangler_command)
    )
    return NoUsableRuntimeError("\n".join(lines), exit_code=1)


def select_slot(
    runtime_root_fs: str,
    platform_key: str,
    wrangler_command: str = MANAGED_BY_VALUE,
) -> SlotSelection:
    """Choose the slot to run, falling back to the other slot when needed.

    With a valid pointer the active slot runs if it passes; otherwise the
    other slot runs if it passes. With a missing or malformed pointer the
    single passing slot runs; when both pass, which one was active is
    unknowable, so the launch is refused rather than guessing.
    """

    active, pointer_problem = read_active_pointer(runtime_root_fs)
    if active is not None:
        active_check = check_slot(runtime_root_fs, active, platform_key)
        if active_check.ok:
            return _selection(active_check, None)
        other_check = check_slot(runtime_root_fs, _other_slot(active), platform_key)
        if other_check.ok:
            return _selection(
                other_check,
                FallbackEvent(
                    from_slot=active,
                    from_version=active_check.version,
                    to_slot=other_check.name,
                    reason=active_check.reason,
                ),
            )
        raise _no_runtime(
            runtime_root_fs,
            [
                "slot {} (active): {}".format(active, active_check.reason),
                "slot {}: {}".format(other_check.name, other_check.reason),
            ],
            wrangler_command,
        )

    checks = [check_slot(runtime_root_fs, name, platform_key) for name in SLOT_NAMES]
    passing = [check for check in checks if check.ok]
    if len(passing) == 1:
        return _selection(
            passing[0],
            FallbackEvent(
                from_slot=None,
                from_version=None,
                to_slot=passing[0].name,
                reason=pointer_problem or "the active-slot pointer is invalid",
            ),
        )
    details = [str(pointer_problem)]
    if passing:
        details.append(
            "both slots are complete, so the active one cannot be determined"
        )
    else:
        details.extend(
            "slot {}: {}".format(check.name, check.reason) for check in checks
        )
    raise _no_runtime(runtime_root_fs, details, wrangler_command)


def select_legacy_binary(repo_root_fs: str, config: LaunchConfig) -> SlotSelection:
    """Select the npm-era platform binary while migration is pending.

    ``legacy_npm_layout_migration.md`` requirement 6: until install,
    upgrade, or repair migrates the project, ``--update`` and the settings
    toggles render a launcher that runs the legacy layout's native binary
    directly, never node or npm. The binary must be a regular file inside
    the repository; there is no slot to fall back to.
    """

    parts = config.legacy_binary or ()
    binary = os.path.join(repo_root_fs, *parts)
    if not _is_within(binary, repo_root_fs):
        raise NoUsableRuntimeError(
            "the legacy Claude Code binary resolves outside the repository: "
            "{}".format(binary)
        )
    try:
        status = os.stat(binary)
    except OSError as exc:
        raise NoUsableRuntimeError(
            "the legacy Claude Code binary is missing or unreadable ({}).\n"
            "Run `{} .` from the project root to install the native "
            "runtime.".format(exc, config.wrangler_command)
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        raise NoUsableRuntimeError(
            "the legacy Claude Code binary is not a regular file: {}".format(binary)
        )
    return SlotSelection(
        slot="legacy",
        version=config.legacy_version or "",
        binary=binary,
        record={},
        fallback=None,
    )


def fallback_warning_lines(
    selection: SlotSelection, wrangler_command: str
) -> List[str]:
    """Render the prominent fallback warning naming both versions."""

    event = selection.fallback
    if event is None:
        return []
    lines = [FALLBACK_BANNER]
    if event.from_slot is None:
        lines.append(
            "WARNING: {}; running the only complete runtime slot, {} "
            "(Claude Code {}).".format(event.reason, event.to_slot, selection.version)
        )
    else:
        lines.append(
            "WARNING: active runtime slot {} (Claude Code {}) failed its "
            "launch check: {}.".format(
                event.from_slot, event.from_version or "unknown version", event.reason
            )
        )
        lines.append(
            "Falling back to slot {} (Claude Code {}) for this launch.".format(
                event.to_slot, selection.version
            )
        )
    lines.append(
        "Run `{} --repair-install` from the project root to repair the "
        "runtime.".format(wrangler_command)
    )
    lines.append(FALLBACK_BANNER)
    return lines


def write_fallback_record(
    runtime_root_fs: str, event: FallbackEvent, moment: datetime
) -> None:
    """Record a fallback in ``fallback.json`` for --inspect and repair."""

    _write_json_atomic(
        os.path.join(runtime_root_fs, FALLBACK_RECORD_NAME),
        {
            "at": format_timestamp(moment),
            "from_slot": event.from_slot,
            "to_slot": event.to_slot,
            "reason": event.reason,
        },
    )


# ---------------------------------------------------------------------------
# Host platform key (duplicates claude_wrangler.platforms; keep in sync)
# ---------------------------------------------------------------------------


def _normalize_machine(machine: Optional[str]) -> Optional[str]:
    """Map a machine name onto ``x64`` or ``arm64``; None when unsupported."""

    lowered = (machine or "").strip().lower()
    if lowered in _X64_MACHINES:
        return "x64"
    if lowered in _ARM64_MACHINES:
        return "arm64"
    return None


def platform_key_from_facts(
    system: str,
    machine: str,
    *,
    rosetta_translated: bool = False,
    windows_native_machine: Optional[str] = None,
    musl: bool = False,
) -> str:
    """Map host facts onto a release platform key reflecting the hardware.

    ``windows_native_machine`` comes from IsWow64Process2 or the processor
    environment and wins over ``machine``, which an emulated process
    misreports. Rosetta-translated macOS processes map to darwin-arm64.
    """

    lowered = system.strip().lower()
    if lowered in ("windows", "win32"):
        arch = (
            _normalize_machine(windows_native_machine)
            if windows_native_machine
            else None
        )
        if arch is None and not windows_native_machine:
            arch = _normalize_machine(machine)
        if arch is None:
            raise UnsupportedHostError(
                "Claude Code has no build for this Windows machine type ({}).".format(
                    windows_native_machine or machine
                )
            )
        return "win32-{}".format(arch)
    if lowered == "darwin":
        arch = _normalize_machine(machine)
        if arch == "arm64" or (arch == "x64" and rosetta_translated):
            return "darwin-arm64"
        if arch == "x64":
            return "darwin-x64"
        raise UnsupportedHostError(
            "Claude Code has no build for this macOS machine type ({}).".format(machine)
        )
    if lowered == "linux":
        arch = _normalize_machine(machine)
        if arch is None:
            raise UnsupportedHostError(
                "Claude Code has no build for this Linux machine type ({}).".format(
                    machine
                )
            )
        return "linux-{}{}".format(arch, "-musl" if musl else "")
    raise UnsupportedHostError(
        "Claude Code has no build for this operating system ({}).".format(system)
    )


def _probe_windows_native_machine() -> Optional[str]:
    """Return the native Windows machine (``arm64``/``x64``/``x86``) or None."""

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
        if is_wow64_process2 is not None:
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            is_wow64_process2.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.USHORT),
                ctypes.POINTER(wintypes.USHORT),
            ]
            is_wow64_process2.restype = wintypes.BOOL
            process_machine = wintypes.USHORT(0)
            native_machine = wintypes.USHORT(0)
            if is_wow64_process2(
                kernel32.GetCurrentProcess(),
                ctypes.byref(process_machine),
                ctypes.byref(native_machine),
            ):
                name = _WINDOWS_MACHINE_NAMES.get(native_machine.value)
                if name is not None:
                    return name
    except (AttributeError, OSError, ValueError):
        pass
    # PROCESSOR_ARCHITEW6432 names the native machine inside WOW64; an x64
    # process emulated on ARM64 is only detectable through IsWow64Process2.
    for variable in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"):
        value = os.environ.get(variable, "").strip().lower()
        if value == "arm64":
            return "arm64"
        if value in ("amd64", "x64"):
            return "x64"
        if value == "x86":
            return "x86"
    return None


def _probe_rosetta() -> bool:
    """Return True when this macOS process runs under Rosetta translation."""

    sysctl = "/usr/sbin/sysctl" if os.path.exists("/usr/sbin/sysctl") else "sysctl"
    try:
        completed = subprocess.run(
            [sysctl, "-n", "sysctl.proc_translated"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip() == b"1"


def _probe_musl() -> bool:
    """Return True when the system C library is musl (install.sh's checks)."""

    if any(os.path.isfile(path) for path in glob.glob(_MUSL_LIBC_GLOB)):
        return True
    try:
        completed = subprocess.run(
            ["ldd", "/bin/ls"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b"musl" in completed.stdout


def detect_host_platform_key() -> str:
    """Detect the platform key for the hardware this launcher runs on."""

    machine = platform.machine()
    if sys.platform == "win32":
        return platform_key_from_facts(
            "windows", machine, windows_native_machine=_probe_windows_native_machine()
        )
    if sys.platform == "darwin":
        rosetta = _normalize_machine(machine) == "x64" and _probe_rosetta()
        return platform_key_from_facts("darwin", machine, rosetta_translated=rosetta)
    if sys.platform.startswith("linux"):
        return platform_key_from_facts("linux", machine, musl=_probe_musl())
    if sys.platform in ("cygwin", "msys"):
        # A POSIX-emulation Python would build a POSIX ring environment for a
        # native Windows binary, leaving USERPROFILE and APPDATA unredirected.
        raise UnsupportedHostError(
            "claude-local is running under a {} Python; run it with a native "
            "Windows Python (py -3) so the Windows home variables are "
            "redirected.".format(sys.platform)
        )
    raise UnsupportedHostError(
        "Claude Code has no build for this operating system ({}).".format(sys.platform)
    )


def detect_wsl(os_name: str) -> bool:
    """Return True when this POSIX process runs inside WSL."""

    if os_name == "nt":
        return False
    try:
        with open("/proc/sys/kernel/osrelease", "rb") as handle:
            release = handle.read(4096).lower()
    except OSError:
        return False
    return b"microsoft" in release or b"wsl" in release


# ---------------------------------------------------------------------------
# Update checks
# ---------------------------------------------------------------------------


def parse_version_tuple(value: Any) -> Optional[Tuple[int, ...]]:
    """Parse a dotted numeric version; None for anything else."""

    if not isinstance(value, str) or not _NUMERIC_VERSION_RE.match(value.strip()):
        return None
    return tuple(int(part) for part in value.strip().split("."))


def is_newer_version(candidate: Any, installed: Any) -> bool:
    """Return True when ``candidate`` is a strictly newer numeric version."""

    left = parse_version_tuple(candidate)
    right = parse_version_tuple(installed)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def read_update_cache(runtime_root_fs: str) -> Optional[Dict[str, Any]]:
    """Read update-check.json; None when missing or unusable."""

    try:
        raw = _read_limited(
            os.path.join(runtime_root_fs, UPDATE_CHECK_NAME), MAX_UPDATE_CHECK_BYTES
        )
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update_cache_is_fresh(
    cache: Optional[Mapping[str, Any]], channel: str, now: datetime
) -> bool:
    """Return True when the cache is for ``channel`` and under 24 hours old."""

    if not cache or cache.get("channel") != channel:
        return False
    checked_at = parse_timestamp(cache.get("checked_at"))
    if checked_at is None:
        return False
    age = (now - checked_at).total_seconds()
    return -UPDATE_CHECK_CLOCK_SKEW_SECONDS <= age < UPDATE_CHECK_TTL_SECONDS


def verified_latest_version(
    cache: Optional[Mapping[str, Any]], channel: str
) -> Optional[str]:
    """Return the manifest-verified newest version the cache records, if any.

    Only ``claude-wrangler --update`` writes ``latest_version``, after the
    signed manifest for that version verified. The launcher's own refresh
    carries it forward unchanged and never writes a pointer value there.
    """

    if not cache or cache.get("channel") != channel:
        return None
    value = cache.get("latest_version")
    return value if isinstance(value, str) else None


def unverified_pointer_version(
    cache: Optional[Mapping[str, Any]], channel: str
) -> Optional[str]:
    """Return the unsigned channel pointer the cache records, if any."""

    if not cache or cache.get("channel") != channel:
        return None
    value = cache.get("pointer_version")
    return value if isinstance(value, str) else None


def update_notice(
    cache: Optional[Mapping[str, Any]],
    channel: str,
    installed_version: str,
    wrangler_command: str,
) -> Optional[str]:
    """Return the one-line notice when the cache names a newer release.

    A newer manifest-verified version is announced as available. Failing
    that, a newer unsigned channel pointer is announced as unverified:
    ``native_runtime_distribution_and_verification.md`` requirement 2
    trusts a pointer only to choose which signed manifest to fetch, and
    ``--upgrade`` verifies that manifest before it installs anything.
    """

    verified = verified_latest_version(cache, channel)
    if is_newer_version(verified, installed_version):
        return (
            "Claude Code {} is available on the {} channel (this project runs {}). "
            "Run `{} --upgrade --channel {}` to install it.".format(
                verified, channel, installed_version, wrangler_command, channel
            )
        )
    pointer = unverified_pointer_version(cache, channel)
    if not is_newer_version(pointer, installed_version):
        return None
    return (
        "The {} channel pointer names Claude Code {} (unverified; this project "
        "runs {}). Run `{} --upgrade --channel {}` to verify its signed manifest "
        "and install it.".format(
            channel, pointer, installed_version, wrangler_command, channel
        )
    )


def _call_with_budget(function: Callable[[], Any], budget_seconds: float) -> Any:
    """Run ``function`` in a daemon thread and give up after the budget.

    Socket timeouts bound each operation, not the whole request (DNS,
    connect, TLS, redirects, read), so the total budget needs a thread. An
    abandoned thread dies with the process or, on POSIX, at ``execve``.
    """

    outcome: Dict[str, Any] = {}

    def runner() -> None:
        """Capture the function's result or exception for the caller."""

        try:
            outcome["value"] = function()
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
            outcome["error"] = exc

    worker = threading.Thread(target=runner, name="claude-local-update-check")
    worker.daemon = True
    worker.start()
    worker.join(budget_seconds)
    if worker.is_alive():
        raise UpdateCheckError("no answer within {:g} seconds".format(budget_seconds))
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def fetch_url_bytes(
    url: str,
    timeout_seconds: float,
    allowed_hosts: Sequence[str] = (UPDATE_CHECK_ALLOWED_HOST,),
) -> bytes:
    """GET a small HTTPS resource from a release host with urllib.

    Redirects must stay on HTTPS and on ``allowed_hosts`` (the default
    release host plus a recorded mirror's host). urllib honors
    HTTPS_PROXY/NO_PROXY, and the default TLS context honors SSL_CERT_FILE.
    """

    # Imported lazily: urllib pulls in http.client, ssl, and email, which
    # would slow every launch even though checks run at most once a day.
    import urllib.error
    import urllib.parse
    import urllib.request

    wanted_hosts = tuple(host.lower().rstrip(".") for host in allowed_hosts)

    def allowed(target: str) -> bool:
        """Return True for HTTPS URLs on an allowed release host."""

        parts = urllib.parse.urlsplit(target)
        host = (parts.hostname or "").lower().rstrip(".")
        return parts.scheme == "https" and host in wanted_hosts

    class AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
        """Refuse redirects that leave HTTPS or the release host."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            """Follow a redirect only when its target is allowed."""

            if not allowed(newurl):
                raise urllib.error.URLError("refusing redirect to {}".format(newurl))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    if not allowed(url):
        raise UpdateCheckError("refusing to fetch {}".format(url))
    opener = urllib.request.build_opener(AllowlistedRedirectHandler())
    request = urllib.request.Request(
        url, headers={"User-Agent": "claude-wrangler-launcher"}
    )
    with opener.open(request, timeout=timeout_seconds) as response:
        status = response.getcode()
        if status != 200:
            raise UpdateCheckError("HTTP status {}".format(status))
        data = response.read(MAX_CHANNEL_POINTER_BYTES + 1)
    if len(data) > MAX_CHANNEL_POINTER_BYTES:
        raise UpdateCheckError("channel pointer response is too large")
    return data


def fetch_channel_version(
    channel: str,
    fetch: FetchFn,
    budget_seconds: float,
    base_url: str = DEFAULT_RELEASE_BASE_URL,
) -> str:
    """Fetch and validate the channel pointer within the time budget."""

    url = "{}/{}".format(base_url.rstrip("/"), channel)
    try:
        data = _call_with_budget(lambda: fetch(url, budget_seconds), budget_seconds)
    except UpdateCheckError:
        raise
    except Exception as exc:  # noqa: BLE001 - every network failure is advisory
        raise UpdateCheckError(str(exc) or exc.__class__.__name__) from exc
    if not isinstance(data, bytes):
        raise UpdateCheckError("channel pointer response is not bytes")
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise UpdateCheckError("channel pointer is not ASCII") from exc
    if not _CHANNEL_POINTER_RE.match(text):
        raise UpdateCheckError(
            "channel pointer {!r} is not a version".format(text[:32])
        )
    return text


def refresh_update_cache(
    runtime_root_fs: str,
    channel: str,
    installed_version: str,
    fetch: FetchFn,
    now: datetime,
    budget_seconds: float,
    previous: Optional[Mapping[str, Any]] = None,
    base_url: str = DEFAULT_RELEASE_BASE_URL,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Refresh update-check.json; return the new cache and an error or None.

    The unsigned channel pointer goes into ``pointer_version`` only; the
    manifest-verified ``latest_version`` that ``claude-wrangler --update``
    recorded is carried forward unchanged, so an unverified pointer never
    passes for verified data. A failed attempt is recorded too (keeping any
    earlier pointer for the same channel), so an offline machine retries
    once per day instead of spending the budget on every launch. The
    ``error`` key is null after a successful refresh and names the failure
    otherwise, so ``--inspect`` can tell the two apart. Cache write
    failures are ignored.
    """

    error: Optional[str] = None
    pointer: Optional[str] = None
    try:
        pointer = fetch_channel_version(channel, fetch, budget_seconds, base_url)
    except UpdateCheckError as exc:
        error = str(exc)
        pointer = unverified_pointer_version(previous, channel)
    cache: Dict[str, Any] = {
        "checked_at": format_timestamp(now),
        "channel": channel,
        "latest_version": verified_latest_version(previous, channel),
        "pointer_version": pointer,
        "installed_version": installed_version,
        "error": error,
    }
    try:
        _write_json_atomic(os.path.join(runtime_root_fs, UPDATE_CHECK_NAME), cache)
    except OSError:
        pass
    return cache, error


# ---------------------------------------------------------------------------
# Node.js parity preflight (warn-only; never requires node)
# ---------------------------------------------------------------------------


def find_node_selector(repo_root_fs: str) -> Optional[Tuple[str, str]]:
    """Return ``(file, value)`` from the first Node.js selector file present.

    Like the bash launcher, only the first existing file is consulted, in
    the order .nvmrc, .node-version, .tool-versions.
    """

    for name in NODE_SELECTOR_FILES:
        path = os.path.join(repo_root_fs, name)
        if not os.path.isfile(path):
            continue
        try:
            text = _read_limited(path, 64 * 1024).decode("utf-8", "replace")
        except OSError:
            return name, ""
        lines = text.splitlines()
        if name == ".tool-versions":
            for line in lines:
                match = _TOOL_VERSIONS_NODE_RE.match(line)
                if match:
                    return name, match.group(1).strip()
            return name, ""
        return name, (lines[0].strip() if lines else "")
    return None


def find_executable_on_path(
    name: str, path_value: Optional[str], os_name: str, pathext: Optional[str] = None
) -> Optional[str]:
    """Find ``name`` on PATH without ever searching the current directory.

    ``shutil.which`` searches the current directory first on Windows, and
    the launcher usually runs from the repository root, so a checked-in
    ``node.exe`` there must never be executed. Relative PATH entries are
    skipped for the same reason. On Windows a name that already ends in a
    ``PATHEXT`` extension (``npm.cmd``) is looked up as given.
    """

    nt = os_name == "nt"
    pathmod = ntpath if nt else posixpath
    if nt:
        extensions = [
            ext for ext in (pathext or ".COM;.EXE;.BAT;.CMD").split(";") if ext
        ]
        if any(name.lower().endswith(ext.lower()) for ext in extensions):
            candidates = [name]
        else:
            candidates = [name + ext for ext in extensions]
    else:
        candidates = [name]
    for entry in (path_value or "").split(";" if nt else ":"):
        entry = entry.strip().strip('"') if nt else entry
        if not entry:
            continue
        if nt and not is_absolute_windows_path(entry):
            continue
        if not nt and not entry.startswith("/"):
            continue
        for candidate in candidates:
            full = pathmod.join(entry, candidate)
            if os.path.isfile(full) and (nt or os.access(full, os.X_OK)):
                return full
    return None


def default_node_probe(
    environ: Mapping[str, str], os_name: str
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(node path, node --version output)``; None for each unknown."""

    node = find_executable_on_path(
        "node",
        env_lookup(environ, "PATH", os_name),
        os_name,
        env_lookup(environ, "PATHEXT", os_name),
    )
    if node is None:
        return None, None
    try:
        completed = subprocess.run(
            [node, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environ),
            timeout=NODE_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return node, None
    lines = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    if completed.returncode != 0 or not lines:
        return node, None
    return node, lines[0].strip()


def _node_versions_match(expected: str, actual: str) -> bool:
    """Return True when ``actual`` satisfies a literal selector ``expected``.

    A partial selector such as ``20`` or ``20.11`` matches any release that
    starts with those components, as nvm and similar managers resolve it.
    """

    expected_parts = expected.lstrip("vV").split(".")
    actual_parts = actual.strip().lstrip("vV").split(".")
    if len(actual_parts) < len(expected_parts):
        return False
    return actual_parts[: len(expected_parts)] == expected_parts


def node_parity_preflight(
    repo_root_fs: str,
    mode_raw: Optional[str],
    environ: Mapping[str, str],
    os_name: str,
    node_probe: NodeProbe,
) -> PreflightResult:
    """Compare a checked-in Node.js selector with the node on PATH.

    ``CLAUDE_LOCAL_PREFLIGHT`` keeps its meaning: ``warn`` (default)
    reports, ``strict`` fails on an explicit selector mismatch, ``off``
    skips the check. Claude Code itself does not need node, so a missing
    node only ever warns.
    """

    messages: List[str] = []
    mode = mode_raw or "warn"
    if mode not in PREFLIGHT_MODES:
        messages.append(
            'Unknown {} value "{}"; defaulting to warn.'.format(PREFLIGHT_VAR, mode)
        )
        mode = "warn"
    if mode == "off":
        return PreflightResult(tuple(messages), False)
    selector = find_node_selector(repo_root_fs)
    if selector is None or not selector[1]:
        return PreflightResult(tuple(messages), False)
    label, value = selector

    def mismatch(message: str) -> PreflightResult:
        """Record a parity mismatch, failing only in strict mode."""

        messages.append(message)
        return PreflightResult(tuple(messages), mode == "strict")

    node_path, node_version = node_probe(environ, os_name)
    if node_path is None:
        messages.append(
            "Project declares Node.js selector {}={}, but node is not on PATH. "
            "claude-local does not need node; project tooling started from "
            "Claude Code may.".format(label, value)
        )
        return PreflightResult(tuple(messages), False)
    if not node_version:
        return mismatch(
            "Project declares Node.js selector {}={}, but node --version could "
            "not be resolved.".format(label, value)
        )
    if _LITERAL_NODE_RE.match(value):
        if not _node_versions_match(value, node_version):
            return mismatch(
                "Project declares Node.js {} in {}, but PATH resolves node {} at "
                "{}.".format(value, label, node_version, node_path)
            )
        return PreflightResult(tuple(messages), False)
    messages.append(
        "Project declares Node.js selector {}={}. Launch claude-local from the "
        "project-activated shell for best parity.".format(label, value)
    )
    return PreflightResult(tuple(messages), False)


# ---------------------------------------------------------------------------
# Process semantics
# ---------------------------------------------------------------------------


def map_windows_exit_code(code: Optional[int]) -> int:
    """Map STATUS_CONTROL_C_EXIT (0xC000013A) to 130; pass others through."""

    if code is None:
        return 1
    if code & 0xFFFFFFFF == WINDOWS_CTRL_C_EXIT:
        return SIGINT_EXIT_CODE
    return code


def _windows_job_api() -> Any:
    """Return a private, prototyped ``kernel32`` for the job-object calls."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def attach_kill_on_close_job(pid: int) -> Optional[int]:
    """Put a Windows child into a kill-on-close job; return the job handle.

    Without a job, killing the launcher (a tool timeout, an SDK
    cancellation, or an IDE that terminates only its direct child) leaves
    Claude Code running: still editing files and spending tokens, keeping
    its slot in use, and holding the caller's pipes open. The job has
    ``KILL_ON_JOB_CLOSE``, so Windows ends the child when the last handle,
    held only by this process and never inherited, closes, including when
    this process is killed. ``SILENT_BREAKAWAY_OK`` keeps everything the
    child starts itself (tools, background sessions, the daemon) outside
    the job, as before. Best effort: any failure returns None and the
    launch goes on without a job.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            """JOBOBJECT_BASIC_LIMIT_INFORMATION."""

            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            """IO_COUNTERS."""

            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            """JOBOBJECT_EXTENDED_LIMIT_INFORMATION."""

            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = _windows_job_api()
        # NULL security attributes: the handle is not inheritable, so no
        # child ever holds the job open.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        )
        process = None
        try:
            if not kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise OSError("SetInformationJobObject failed")
            process = kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
            )
            if not process or not kernel32.AssignProcessToJobObject(job, process):
                raise OSError("AssignProcessToJobObject failed")
        except OSError:
            kernel32.CloseHandle(job)
            return None
        finally:
            if process:
                kernel32.CloseHandle(process)
        return int(job)
    except (AttributeError, OSError, ValueError):
        return None


def close_job(job: Optional[int]) -> None:
    """Close a handle from ``attach_kill_on_close_job`` (None is ignored)."""

    if job is None:
        return
    try:
        _windows_job_api().CloseHandle(job)
    except (AttributeError, OSError, ValueError):
        pass


def spawn_and_wait(argv: List[str], env: Dict[str, str]) -> int:
    """Run Claude Code as a child sharing the console; return its exit code.

    The parent ignores Ctrl+C while it waits: the console delivers the
    event to the child too, and the child decides what it means.
    ``close_fds=False`` lets the child inherit the standard handles exactly
    as a shell would hand them over. The child is placed in a
    kill-on-close job right after it starts (``attach_kill_on_close_job``),
    so it does not outlive a killed launcher. Anything it started in that
    instant stays outside the job, as its later children do anyway.
    """

    restore = True
    previous: Any = None
    job: Optional[int] = None
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        # Not on the main thread; KeyboardInterrupt handling below suffices.
        restore = False
    try:
        process = subprocess.Popen(list(argv), env=env, close_fds=False)
        job = attach_kill_on_close_job(process.pid)
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                continue
    finally:
        # The child has exited (or Popen failed), so closing the job kills
        # nothing; its own children were never in the job.
        close_job(job)
        if restore:
            try:
                signal.signal(
                    signal.SIGINT,
                    previous if previous is not None else signal.SIG_DFL,
                )
            except (ValueError, OSError):
                pass


def windows_warning_applies(
    os_name: str, is_wsl: bool, config: LaunchConfig, nested: bool
) -> bool:
    """Return True when the native-Windows sandbox warning should print."""

    return (
        os_name == "nt" and not is_wsl and config.windows_sandbox_warning and not nested
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _installed_version_quietly(
    paths: RepoPaths, config: LaunchConfig, platform_key: Optional[str]
) -> Tuple[str, Optional[str]]:
    """Return ``(version, slot)`` of the slot a launch would run, or ``("", None)``.

    In legacy mode the slot is reported as ``legacy``.
    """

    if config.legacy_binary is not None:
        return (config.legacy_version or ""), "legacy"
    try:
        key = platform_key or detect_host_platform_key()
        selection = select_slot(paths.runtime_root_fs, key, config.wrangler_command)
    except LauncherError:
        return "", None
    return selection.version, selection.slot


def run_check_updates(
    paths: RepoPaths,
    config: LaunchConfig,
    *,
    fetch: FetchFn,
    now: datetime,
    stdout: Any,
    stderr: Any,
    platform_key: Optional[str],
) -> int:
    """Handle ``--check-updates``: refresh the cache and print the result."""

    installed, slot = _installed_version_quietly(paths, config, platform_key)
    previous = read_update_cache(paths.runtime_root_fs)
    cache, error = refresh_update_cache(
        paths.runtime_root_fs,
        config.channel,
        installed,
        fetch,
        now,
        UPDATE_CHECK_BUDGET_SECONDS,
        previous,
        config.release_base_url,
    )
    if error is not None:
        _emit(
            stderr,
            "{} update check failed for the {} channel: {}".format(
                MESSAGE_PREFIX, config.channel, error
            ),
        )
        return 1
    pointer = unverified_pointer_version(cache, config.channel)
    verified = verified_latest_version(cache, config.channel)
    command = config.wrangler_command
    _emit(stdout, "Claude Code update check ({} channel)".format(config.channel))
    _emit(stdout, "  channel pointer: {} (unsigned, not verified)".format(pointer))
    _emit(
        stdout,
        "  verified:        {}".format(
            "{} (signed manifest checked by `{} --update`)".format(verified, command)
            if verified
            else "none recorded; run `{} --update`".format(command)
        ),
    )
    _emit(
        stdout,
        "  installed:       {}".format(
            "{} (slot {})".format(installed, slot) if slot else "none usable"
        ),
    )
    if installed and is_newer_version(verified, installed):
        _emit(
            stdout,
            "Update available. Run `{} --upgrade --channel {}` to install "
            "it.".format(command, config.channel),
        )
    elif installed and is_newer_version(pointer, installed):
        _emit(
            stdout,
            "The channel pointer names a newer release, which is unverified until "
            "its signed manifest is checked. Run `{} --upgrade --channel {}` to "
            "verify and install it, or `{} --update` to verify it without "
            "installing.".format(command, config.channel, command),
        )
    elif installed:
        _emit(stdout, "Up to date.")
    if not config.update_checks:
        _emit(
            stdout,
            "Automatic update checks are off for this project; this check ran "
            "because {} was given.".format(CHECK_UPDATES_FLAG),
        )
    return 0


def _maybe_emit_update_notice(
    paths: RepoPaths,
    config: LaunchConfig,
    installed_version: str,
    fetch: FetchFn,
    now: datetime,
    stderr: Any,
) -> None:
    """Print the cached update notice, refreshing a stale cache first.

    Nothing here may block or fail a launch: every error is swallowed and
    the refresh is held to the 3-second total budget.
    """

    try:
        cache = read_update_cache(paths.runtime_root_fs)
        if not update_cache_is_fresh(cache, config.channel, now):
            cache, _ = refresh_update_cache(
                paths.runtime_root_fs,
                config.channel,
                installed_version,
                fetch,
                now,
                UPDATE_CHECK_BUDGET_SECONDS,
                cache,
                config.release_base_url,
            )
        line = update_notice(
            cache, config.channel, installed_version, config.wrangler_command
        )
    except Exception:  # noqa: BLE001 - update notices are advisory only
        return
    if line:
        _emit(stderr, "{} {}".format(MESSAGE_PREFIX, line))


def _launch(
    args: Sequence[str],
    *,
    environ: Dict[str, str],
    os_name: str,
    platform_is_wsl: Optional[bool],
    core_path: str,
    exec_fn: Optional[ExecFn],
    spawn_fn: Optional[SpawnFn],
    stderr: Any,
    stdout: Any,
    fetch: Optional[FetchFn],
    now_fn: Callable[[], datetime],
    platform_key: Optional[str],
    node_probe: NodeProbe,
) -> int:
    """Run every launch step in order; raise LauncherError to refuse."""

    parsed = parse_launcher_arguments(args)
    config = load_launch_config(
        os.path.join(os.path.dirname(core_path), LAUNCH_CONFIG_NAME)
    )
    repo_root = choose_repo_root(derive_repo_root(core_path, config), environ, os_name)
    if os_name == "nt" and config.config_dir_spelling == CONFIG_DIR_SPELLING_NATIVE:
        # One checkout reached as c:\... or through an 8.3 name must give
        # Claude Code one config-dir and home spelling (forward-slash mode
        # already upper-cases the drive for its migrated spelling).
        repo_root = canonical_windows_root(repo_root)
    paths = build_repo_paths(repo_root, config, os_name)
    if fetch is None:
        hosts = update_check_hosts(config)

        def fetch(url: str, timeout_seconds: float) -> bytes:
            """Fetch from the release host or the recorded mirror only."""

            return fetch_url_bytes(url, timeout_seconds, hosts)

    if parsed.check_updates:
        return run_check_updates(
            paths,
            config,
            fetch=fetch,
            now=now_fn(),
            stdout=stdout,
            stderr=stderr,
            platform_key=platform_key,
        )

    forwarded = parsed.forwarded
    # The plain first-argument form is refused before slot selection, so it
    # gets this message even without a usable runtime; leading options are
    # checked against the selected slot's subcommands below.
    if forwarded and forwarded[0] in INTERCEPTED_SUBCOMMANDS:
        raise LauncherError(interception_message(forwarded[0], config), exit_code=2)
    if caller_passed_settings(forwarded):
        raise LauncherError(settings_refusal_message(paths, os_name), exit_code=2)
    problem = path_entry_problem(paths.shim_dir, os_name)
    if problem is not None:
        raise LauncherError(problem, exit_code=1)

    if config.legacy_binary is not None:
        selection = select_legacy_binary(long_path_spelling(repo_root), config)
        _emit(
            stderr,
            "{} {}".format(
                MESSAGE_PREFIX,
                MIGRATION_PENDING_NOTE.format(
                    cmd=config.wrangler_command, channel=config.channel
                ),
            ),
        )
    else:
        key = platform_key or detect_host_platform_key()
        # The binary is started by its long-name path, so maintenance's
        # in-use check finds it even when the shim ran through an 8.3 path.
        selection = select_slot(
            long_path_spelling(paths.runtime_root_fs), key, config.wrangler_command
        )
    blocked = intercepted_subcommand(forwarded, record_subcommands(selection.record))
    if blocked is not None:
        raise LauncherError(interception_message(blocked, config), exit_code=2)
    if selection.fallback is not None:
        for line in fallback_warning_lines(selection, config.wrangler_command):
            _emit(stderr, "{} {}".format(MESSAGE_PREFIX, line))
        try:
            write_fallback_record(paths.runtime_root_fs, selection.fallback, now_fn())
        except OSError as exc:
            _emit(
                stderr,
                "{} could not record the fallback in {}: {}".format(
                    MESSAGE_PREFIX, FALLBACK_RECORD_NAME, exc
                ),
            )

    suppression_keys = list(
        dict.fromkeys(
            [name for name, _ in REQUIRED_SUPPRESSION_ENV]
            + list(config.suppression_env)
        )
    )
    for warning in scan_project_settings(repo_root, suppression_keys):
        _emit(stderr, "{} warning: {}".format(MESSAGE_PREFIX, warning))

    preflight = node_parity_preflight(
        repo_root,
        env_lookup(environ, PREFLIGHT_VAR, os_name),
        environ,
        os_name,
        node_probe,
    )
    for message in preflight.messages:
        _emit(stderr, "{} {}".format(PREFLIGHT_PREFIX, message))
    if preflight.failed:
        return 1

    shared_home = select_shared_home(
        config.shared_home_default, environ, parsed.shared_home, os_name
    )
    nested = is_nested_launch(environ, paths, os_name)
    is_wsl = detect_wsl(os_name) if platform_is_wsl is None else platform_is_wsl
    if windows_warning_applies(os_name, is_wsl, config, nested):
        _emit(stderr, "{} {}".format(MESSAGE_PREFIX, WINDOWS_SANDBOX_WARNING))
    credentials = credential_variables(environ, os_name)
    if credentials and not nested:
        _emit(
            stderr,
            "{} {}".format(
                MESSAGE_PREFIX,
                credential_note(
                    credentials,
                    paths.config_dir,
                    passthrough=config.credential_passthrough,
                    command=config.wrangler_command,
                ),
            ),
        )
    if config.update_checks and not nested:
        _maybe_emit_update_notice(
            paths, config, selection.version, fetch, now_fn(), stderr
        )

    slot_dir = nested_claude_slot_dir(selection, config, os_name)
    if slot_dir is not None and path_entry_problem(slot_dir, os_name) is not None:
        slot_dir = None
    if os_name == "nt" and slot_dir is None and not nested:
        reason = (
            NESTED_SHIM_REASON_LEGACY
            if config.legacy_binary is not None
            else NESTED_SHIM_REASON_SLOT.format(cmd=config.wrangler_command)
        )
        _emit(
            stderr,
            "{} {}".format(
                MESSAGE_PREFIX, NESTED_SHIM_FALLBACK_WARNING.format(reason=reason)
            ),
        )
    child_env = build_session_environment(
        environ,
        paths=paths,
        config=config,
        shared_home=shared_home,
        os_name=os_name,
        slot_dir=slot_dir,
    )
    ensure_session_directories(paths, shared_home, os_name)
    child_argv = [selection.binary] + build_claude_arguments(
        forwarded,
        subcommands=record_subcommands(selection.record),
        settings_override=config.settings_override,
        reasonable_permissions=config.reasonable_permissions,
        settings_rejected=record_settings_rejected(selection.record),
    )

    try:
        if os_name == "nt":
            return map_windows_exit_code(
                (spawn_fn or spawn_and_wait)(child_argv, child_env)
            )
        result = (exec_fn or os.execve)(selection.binary, child_argv, child_env)
    except OSError as exc:
        raise LauncherError(
            "cannot run {}: {}".format(selection.binary, exc),
            exit_code=127 if isinstance(exc, FileNotFoundError) else 126,
        ) from exc
    # os.execve never returns; injected test doubles may return a code.
    return result if isinstance(result, int) else 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
    platform_is_wsl: Optional[bool] = None,
    core_path: Optional[str] = None,
    exec_fn: Optional[ExecFn] = None,
    spawn_fn: Optional[SpawnFn] = None,
    stderr: Any = None,
    stdout: Any = None,
    fetch_fn: Optional[FetchFn] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    platform_key: Optional[str] = None,
    node_probe: Optional[NodeProbe] = None,
) -> int:
    """Launch the project's pinned Claude Code and return an exit code.

    Every keyword exists so tests can drive each branch on any OS:
    ``os_name`` selects Windows or POSIX behavior, ``exec_fn`` replaces
    ``os.execve`` (POSIX), ``spawn_fn`` replaces the Windows child-process
    wait, ``fetch_fn`` replaces the HTTPS GET used by update checks,
    ``now_fn`` the clock, ``platform_key`` host detection, and
    ``node_probe`` the ``node --version`` lookup.
    """

    error_stream = stderr if stderr is not None else sys.stderr
    try:
        return _launch(
            list(sys.argv[1:] if argv is None else argv),
            environ=dict(os.environ if environ is None else environ),
            os_name=os.name if os_name is None else os_name,
            platform_is_wsl=platform_is_wsl,
            core_path=os.path.abspath(__file__ if core_path is None else core_path),
            exec_fn=exec_fn,
            spawn_fn=spawn_fn,
            stderr=error_stream,
            stdout=stdout if stdout is not None else sys.stdout,
            fetch=fetch_fn,
            now_fn=now_fn or _utc_now,
            platform_key=platform_key,
            node_probe=node_probe or default_node_probe,
        )
    except LauncherError as exc:
        for line in exc.message.splitlines():
            _emit(error_stream, "{} {}".format(MESSAGE_PREFIX, line))
        return exc.exit_code
    except KeyboardInterrupt:
        return SIGINT_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
