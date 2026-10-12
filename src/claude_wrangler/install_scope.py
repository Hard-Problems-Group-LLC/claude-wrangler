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
from typing import List, Mapping, Optional, Tuple

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


def isolated_home_sources(
    repo_root: Path,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
) -> List[Tuple[str, Path]]:
    """Return each advertised home that is a repo-local isolated home."""

    return [
        (source, path)
        for source, path in home_candidates(environ, os_name)
        if is_repo_local_isolated_home(path, repo_root)
    ]


def describe_user_home_source(
    repo_root: Path,
    user_home: Path,
    explicit_home: Optional[Path],
) -> str:
    """Describe why one target user home was selected."""

    if explicit_home is not None:
        return "explicit --user-home target"
    if is_repo_local_isolated_home(user_home, repo_root):
        return "current repo-local isolated home"
    return "current process HOME"


def resolve_user_home(
    repo_root: Path,
    explicit_home: Optional[Path] = None,
    *,
    allow_isolated_home: bool = False,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
) -> Path:
    """Return the intended home for user-scoped installer side effects.

    `environ` and `os_name` default to the live process and exist so the
    Windows and POSIX rules can be tested on any host.
    """

    if explicit_home is not None:
        return _resolve_lenient(explicit_home)

    isolated = isolated_home_sources(repo_root, environ, os_name)
    if isolated:
        source, isolated_home = isolated[0]
        if not allow_isolated_home:
            raise RuntimeError(
                "Current {} resolves to a repo-local isolated home ({}). Rerun "
                "from a normal terminal, pass --user-home PATH to target a "
                "different user scope explicitly, or pass --allow-isolated-home "
                "to install into the isolated AI home on purpose.".format(
                    source, isolated_home
                )
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
    "current_home",
    "default_pyenv_root",
    "default_tool_venv",
    "default_user_bin_dir",
    "default_user_share_dir",
    "describe_user_home_source",
    "home_candidates",
    "is_repo_local_isolated_home",
    "isolated_home_sources",
    "repo_local_isolated_homes",
    "resolve_user_home",
]
