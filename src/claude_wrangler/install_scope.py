"""Helpers for explicit user-home targeting across install entry points.

Which directory counts as "the user's home" depends on the operating
system. POSIX programs, Git Bash, and Python on POSIX read `HOME`. Native
Windows programs, including Python on Windows, read `USERPROFILE` (then
`HOMEDRIVE`/`HOMEPATH`) and ignore `HOME`. A ring-1 claude-local session
redirects all of them into the repository, but a partially redirected
environment can still advertise different homes through different
variables. The isolated-home guard therefore checks every home variable
the current OS honors, while the default install target follows the
OS's native resolution order.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from .constants import DEFAULT_HOME_DIR

# Recognized repo-local isolated homes. Mixed-wrangler projects may launch
# sessions from either tool, so every layout guards user-scoped installs:
# this tool's redirected HOME, codex-wrangler's current `.local/codex-home`,
# and codex-wrangler's legacy `.codex-home`.
CLAUDE_HOME_RELATIVE = DEFAULT_HOME_DIR
CODEX_HOME_DIRNAME = ".local/codex-home"
LEGACY_CODEX_HOME_DIRNAME = ".codex-home"
ISOLATED_HOME_RELATIVES = (
    CLAUDE_HOME_RELATIVE,
    CODEX_HOME_DIRNAME,
    LEGACY_CODEX_HOME_DIRNAME,
)
# Set by the generated claude-local and codex-local launchers in the
# sessions they start (the same markers the Windows stage-1 gate reads).
MANAGED_BY_VARIABLES = ("CLAUDE_LOCAL_MANAGED_BY", "CODEX_LOCAL_MANAGED_BY")
# Marks "probe the account's own home" for the injectable account_home.
_PROBE_ACCOUNT_HOME = object()


def _environment(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    """Return the injected environment, or the live process environment."""

    return os.environ if environ is None else environ


def _is_windows(os_name: Optional[str]) -> bool:
    """Return True when the injected or actual `os.name` is native Windows."""

    return (os.name if os_name is None else os_name) == "nt"


def _resolve_lenient(path: Path) -> Path:
    """Resolve a path, falling back to an absolute lexical path.

    Python 3.9 on Windows can return a relative result when resolving a
    missing relative path, and resolution can fail outright on symlink
    loops, so both cases fall back to `os.path.abspath`.
    """

    expanded = path.expanduser()
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(str(expanded)))
    if not resolved.is_absolute():
        return Path(os.path.abspath(str(resolved)))
    return resolved


def _same_location(left: Path, right: Path) -> bool:
    """Return True when two resolved paths name the same location.

    Resolved-path equality is case-insensitive on Windows already;
    `os.path.samefile` additionally catches aliases such as hard-linked
    or short-name spellings when both paths exist.
    """

    if left == right:
        return True
    try:
        return os.path.samefile(str(left), str(right))
    except OSError:
        return False


def home_candidates(
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
) -> List[Tuple[str, Path]]:
    """Return `(source, resolved path)` for each home this process advertises.

    The first entry is the home that native programs on this OS use and
    is the default target for user-scoped installs. Later entries exist so
    the isolated-home guard can detect a partially redirected environment.
    Duplicate locations are reported once, under their first source.
    """

    env = _environment(environ)
    raw: List[Tuple[str, Path]] = []
    if _is_windows(os_name):
        userprofile = env.get("USERPROFILE")
        if userprofile:
            raw.append(("USERPROFILE", Path(userprofile)))
        homepath = env.get("HOMEPATH")
        if homepath:
            # Mirrors ntpath.expanduser: a missing HOMEDRIVE means no drive.
            raw.append(
                ("HOMEDRIVE/HOMEPATH", Path(env.get("HOMEDRIVE", "") + homepath))
            )
    home = env.get("HOME")
    if home:
        raw.append(("HOME", Path(home)))
    if not raw:
        try:
            raw.append(("home directory", Path.home()))
        except (KeyError, RuntimeError) as exc:
            raise RuntimeError(
                "Cannot determine the current user home directory. Pass "
                "--user-home PATH to choose one explicitly."
            ) from exc

    candidates: List[Tuple[str, Path]] = []
    for source, path in raw:
        resolved = _resolve_lenient(path)
        if any(_same_location(resolved, seen) for _, seen in candidates):
            continue
        candidates.append((source, resolved))
    return candidates


def current_home(
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
) -> Path:
    """Return the current process home directory as a resolved path.

    On native Windows this is `USERPROFILE` (then `HOMEDRIVE`/`HOMEPATH`),
    matching `Path.home()` and every native Windows program; `HOME` is
    used only when neither is set. Elsewhere it is `HOME`.
    """

    return home_candidates(environ, os_name)[0][1]


def default_user_bin_dir(user_home: Path) -> Path:
    """Return the managed user-local bin directory under one target home."""

    return user_home.expanduser().resolve() / ".local" / "bin"


def default_user_share_dir(user_home: Path) -> Path:
    """Return the managed user-local share directory under one target home."""

    return user_home.expanduser().resolve() / ".local" / "share"


def default_tool_venv(user_home: Path, tool_name: str) -> Path:
    """Return the default managed venv path for one tool under one home."""

    return default_user_share_dir(user_home) / tool_name / "venv"


def default_pyenv_root(user_home: Optional[Path] = None) -> Path:
    """Return the pyenv root for the selected target home."""

    if user_home is not None:
        return user_home.expanduser().resolve() / ".pyenv"

    configured = os.environ.get("PYENV_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return current_home() / ".pyenv"


def repo_local_isolated_homes(repo_root: Path) -> List[Path]:
    """Return the recognized repo-local isolated home paths for one repo."""

    resolved_root = _resolve_lenient(repo_root)
    return [
        _resolve_lenient(resolved_root / relative)
        for relative in ISOLATED_HOME_RELATIVES
    ]


def is_repo_local_isolated_home(user_home: Path, repo_root: Path) -> bool:
    """Return True when one home matches a recognized repo-local isolated home."""

    resolved = _resolve_lenient(user_home)
    return any(
        _same_location(resolved, isolated)
        for isolated in repo_local_isolated_homes(repo_root)
    )


def has_isolated_home_tail(path: Path, os_name: Optional[str] = None) -> bool:
    """Return True when a path ends like any repository's isolated home.

    Inside project A's session, an installer started from checkout B sees
    A's redirected home, which B's own layout check cannot recognize. The
    path tails `.local/claude/HOME`, `.local/codex-home`, and `.codex-home`
    are compared case-insensitively on Windows.
    """

    windows = _is_windows(os_name)
    parts = tuple(part.lower() if windows else part for part in Path(path).parts)
    for relative in ISOLATED_HOME_RELATIVES:
        tail = tuple(part.lower() if windows else part for part in relative.split("/"))
        if len(parts) > len(tail) and parts[-len(tail) :] == tail:
            return True
    return False


def account_home_directory(os_name: Optional[str] = None) -> Optional[Path]:
    """Return the home recorded for this account, ignoring the environment.

    On Windows that is the logon token's profile (`GetUserProfileDirectoryW`,
    which still names the real profile when a launcher redirected
    `USERPROFILE`); on POSIX the password database entry. None when it
    cannot be read.
    """

    if _is_windows(os_name):
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            userenv = ctypes.WinDLL("userenv", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            ]
            userenv.GetUserProfileDirectoryW.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
            ):
                return None
            try:
                size = wintypes.DWORD(0)
                # The first call only reports the buffer size it needs.
                userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
                if size.value == 0:
                    return None
                buffer = ctypes.create_unicode_buffer(size.value)
                if not userenv.GetUserProfileDirectoryW(
                    token, buffer, ctypes.byref(size)
                ):
                    return None
                return Path(buffer.value)
            finally:
                kernel32.CloseHandle(token)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError, OSError):
        return None


def isolated_home_sources(
    repo_root: Path,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
    *,
    account_home: Any = _PROBE_ACCOUNT_HOME,
) -> List[Tuple[str, Path]]:
    """Return each advertised home that is an isolated or launcher home.

    A home counts when it is one of this repository's isolated homes, when
    its path ends like any repository's isolated home, or, inside a
    claude-local or codex-local session (`MANAGED_BY_VARIABLES`), when it
    differs from the account's own home (a custom launcher layout). Ring-0
    sessions keep the real home and pass. On Windows `HOMEDRIVE/HOMEPATH`
    takes part in that last check only when it is the effective home,
    because domain accounts often point it at a network drive.
    `account_home` defaults to `account_home_directory`; tests pass a path
    or None.
    """

    env = _environment(environ)
    marker = any(env.get(name) for name in MANAGED_BY_VARIABLES)
    expected: Optional[Path] = None
    if marker:
        raw = (
            account_home_directory(os_name)
            if account_home is _PROBE_ACCOUNT_HOME
            else account_home
        )
        expected = _resolve_lenient(Path(raw)) if raw is not None else None
    hits: List[Tuple[str, Path]] = []
    for index, (source, path) in enumerate(home_candidates(environ, os_name)):
        if is_repo_local_isolated_home(path, repo_root) or has_isolated_home_tail(
            path, os_name
        ):
            hits.append((source, path))
            continue
        if expected is None:
            continue
        if source == "HOMEDRIVE/HOMEPATH" and index != 0:
            continue
        if not _same_location(path, expected):
            hits.append((source, path))
    return hits


def describe_user_home_source(
    repo_root: Path,
    user_home: Path,
    explicit_home: Optional[Path],
) -> str:
    """Describe why one target user home was selected."""

    if explicit_home is not None:
        return "explicit --user-home target"
    if is_repo_local_isolated_home(user_home, repo_root) or has_isolated_home_tail(
        user_home
    ):
        return "current repo-local isolated home"
    return "current process HOME"


def resolve_user_home(
    repo_root: Path,
    explicit_home: Optional[Path] = None,
    *,
    allow_isolated_home: bool = False,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
    account_home: Any = _PROBE_ACCOUNT_HOME,
) -> Path:
    """Return the intended home for user-scoped installer side effects.

    `environ`, `os_name`, and `account_home` default to the live process
    and exist so the Windows and POSIX rules can be tested on any host.
    """

    if explicit_home is not None:
        return _resolve_lenient(explicit_home)

    isolated = isolated_home_sources(
        repo_root, environ, os_name, account_home=account_home
    )
    if isolated:
        source, isolated_home = isolated[0]
        if not allow_isolated_home:
            raise RuntimeError(
                "Current {} resolves to a repo-local isolated home or a "
                "launcher-redirected home ({}). Rerun from a normal terminal, "
                "pass --user-home PATH to target a different user scope "
                "explicitly, or pass --allow-isolated-home to install into the "
                "isolated AI home on purpose.".format(source, isolated_home)
            )
        # The operator opted into the isolated home. When only some home
        # variables were redirected, never fall through to the real profile
        # that the remaining variables still name.
        return isolated_home
    return current_home(environ, os_name)


__all__ = [
    "CLAUDE_HOME_RELATIVE",
    "CODEX_HOME_DIRNAME",
    "ISOLATED_HOME_RELATIVES",
    "LEGACY_CODEX_HOME_DIRNAME",
    "MANAGED_BY_VARIABLES",
    "account_home_directory",
    "current_home",
    "default_pyenv_root",
    "default_tool_venv",
    "default_user_bin_dir",
    "default_user_share_dir",
    "describe_user_home_source",
    "has_isolated_home_tail",
    "home_candidates",
    "is_repo_local_isolated_home",
    "isolated_home_sources",
    "repo_local_isolated_homes",
    "resolve_user_home",
]
