"""Helpers for installing claude-wrangler into a dedicated user venv.

The user-facing launcher is a symlink to the venv's console script on
POSIX. On Windows it is a copied `claude-wrangler.exe`: pip's console
launcher embeds the absolute path of its venv interpreter, so a copy keeps
working from any directory, while symlinks need Developer Mode or an
elevated token. A running `.exe` cannot be overwritten or deleted on
Windows but can be renamed, so a refresh renames a busy launcher aside and
later refreshes delete those retired copies once they are no longer in use.
The installers never edit `PATH`; they print the command that adds the
launcher directory when it is missing.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import ntpath
import os
from pathlib import Path
import posixpath
import shlex
import shutil
import subprocess
import sys
from typing import List, Mapping, Optional, Sequence
import uuid

from claude_wrangler.install_scope import (
    default_pyenv_root as default_install_pyenv_root,
    default_tool_venv,
    default_user_bin_dir,
    describe_user_home_source,
    resolve_user_home,
)

DEFAULT_LAUNCHER_NAME = "claude-wrangler"
DEFAULT_VENV = default_tool_venv(Path.home(), DEFAULT_LAUNCHER_NAME)
DEFAULT_BIN_DIR = default_user_bin_dir(Path.home())
PYTHON_ENVIRONMENTS_FILE = "python-environments.json"
WINDOWS_LAUNCHER_SUFFIX = ".exe"
# A replaced Windows launcher that was still running is renamed to
# "<launcher>.old-<token>" so the new copy can take its name.
RETIRED_LAUNCHER_INFIX = ".old-"
STAGED_LAUNCHER_SUFFIX = ".new"


def _windows(windows: Optional[bool]) -> bool:
    """Return the injected Windows flag, defaulting to the running OS."""

    return os.name == "nt" if windows is None else windows


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments for the user-tool installer."""

    parser = argparse.ArgumentParser(
        description=(
            "Install claude-wrangler into a dedicated virtual environment and "
            "place a stable launcher in ~/.local/bin."
        )
    )
    parser.add_argument(
        "--user-home",
        type=Path,
        default=None,
        help=(
            "Explicit user home for the managed install paths and pyenv "
            "runtime lookup. Required when the current HOME is an isolated "
            "repo-local isolated home and you intend to target another user "
            "scope."
        ),
    )
    parser.add_argument(
        "--allow-isolated-home",
        action="store_true",
        help=(
            "Allow the installer to target the current repo-local isolated home "
            "when HOME isolation is active."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Path to the claude-wrangler repository checkout.",
    )
    parser.add_argument(
        "--python",
        default=None,
        help=(
            "Python interpreter used to create the dedicated virtual "
            "environment. When omitted, prefer the managed pyenv runtime "
            "under the selected user home if it already exists, otherwise "
            "fall back to the current interpreter."
        ),
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=None,
        help=(
            "Virtual environment path for the installed command. Defaults to "
            "<user-home>/.local/share/claude-wrangler/venv."
        ),
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=None,
        help=(
            "Directory that will receive the user-facing launcher: a symlink, "
            "or a copied .exe on Windows. Defaults to <user-home>/.local/bin."
        ),
    )
    parser.add_argument(
        "--launcher-name",
        default=DEFAULT_LAUNCHER_NAME,
        help="Filename to create under the bin directory.",
    )
    parser.add_argument(
        "--editable",
        action="store_true",
        help="Install the repository in editable mode.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Install the package's dev extra.",
    )
    return parser.parse_args(argv)


def venv_bin_dir(venv_path: Path, *, windows: Optional[bool] = None) -> Path:
    """Return the platform-specific venv executable directory."""

    return venv_path / ("Scripts" if _windows(windows) else "bin")


def pyenv_python_path(
    pyenv_root: Path,
    environment_name: str,
    *,
    windows: Optional[bool] = None,
) -> Path:
    """Return the Python path for one installed pyenv environment name.

    POSIX pyenv keeps the interpreter at `versions/<name>/bin/python`;
    pyenv-win keeps full Windows installs, with `python.exe` directly under
    `versions/<name>`.
    """

    version_dir = pyenv_root / "versions" / environment_name
    if _windows(windows):
        return version_dir / "python.exe"
    return version_dir / "bin" / "python"


def venv_python_path(venv_path: Path, *, windows: Optional[bool] = None) -> Path:
    """Return the virtual environment Python executable path."""

    executable = "python.exe" if _windows(windows) else "python"
    return venv_bin_dir(venv_path, windows=windows) / executable


def installed_command_path(venv_path: Path, *, windows: Optional[bool] = None) -> Path:
    """Return the venv-installed claude-wrangler command path."""

    executable = (
        DEFAULT_LAUNCHER_NAME + WINDOWS_LAUNCHER_SUFFIX
        if _windows(windows)
        else DEFAULT_LAUNCHER_NAME
    )
    return venv_bin_dir(venv_path, windows=windows) / executable


def build_package_spec(repo_root: Path, *, dev: bool) -> str:
    """Return the pip package spec for this checkout."""

    resolved = str(repo_root.resolve())
    if dev:
        return "{}[dev]".format(resolved)
    return resolved


def load_managed_runtime_name(repo_root: Path) -> Optional[str]:
    """Return the configured managed runtime selection when present."""

    config_path = repo_root / PYTHON_ENVIRONMENTS_FILE
    if not config_path.is_file():
        return None

    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return None
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        return None
    selection = runtime.get("environment_name")
    if not isinstance(selection, str) or not selection:
        return None
    return selection


def default_pyenv_root(user_home: Optional[Path] = None) -> Path:
    """Return the preferred pyenv root for one selected user home."""

    return default_install_pyenv_root(user_home)


def managed_runtime_python(
    repo_root: Path,
    user_home: Optional[Path] = None,
) -> Optional[Path]:
    """Return the managed pyenv runtime Python when it already exists."""

    selection = load_managed_runtime_name(repo_root)
    if selection is None:
        return None
    pyenv_root = default_pyenv_root(user_home)
    roots = [pyenv_root]
    if os.name == "nt":
        # pyenv-win installs under <home>/.pyenv/pyenv-win and normally sets
        # PYENV_ROOT to that directory; accept the unset-variable layout too.
        roots.append(pyenv_root / "pyenv-win")
    for root in roots:
        candidate = pyenv_python_path(root, selection)
        if candidate.is_file():
            return candidate
    return None


def select_install_python(
    repo_root: Path,
    explicit_python: Optional[str],
    user_home: Optional[Path] = None,
) -> str:
    """Choose the interpreter used for the dedicated user install."""

    if explicit_python:
        return explicit_python
    managed_python = managed_runtime_python(repo_root, user_home)
    if managed_python is not None:
        return str(managed_python)
    return sys.executable


def build_install_command(
    python_executable: Path,
    repo_root: Path,
    *,
    editable: bool,
    dev: bool,
) -> list[str]:
    """Build the pip install command for the dedicated venv."""

    command = [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-build-isolation",
    ]
    if editable:
        command.append("--editable")
    command.append(build_package_spec(repo_root, dev=dev))
    return command


def build_bootstrap_command(python_executable: Path) -> list[str]:
    """Build the minimal bootstrap command for local package builds."""

    return [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "setuptools>=69",
        "wheel",
    ]


def ensure_virtualenv(python_executable: str, venv_path: Path) -> Path:
    """Create the dedicated virtual environment when it does not exist."""

    venv_python = venv_python_path(venv_path)
    if venv_python.exists():
        return venv_python

    subprocess.run(
        [python_executable, "-m", "venv", str(venv_path)],
        check=True,
    )
    return venv_python


def install_build_bootstrap(venv_python: Path) -> None:
    """Install the build tooling that recent Ubuntu venvs omit by default."""

    subprocess.run(build_bootstrap_command(venv_python), check=True)


def launcher_path(
    bin_dir: Path,
    launcher_name: str,
    *,
    windows: Optional[bool] = None,
) -> Path:
    """Return the launcher path; Windows needs a PATHEXT-visible `.exe` name."""

    if _windows(windows) and not launcher_name.lower().endswith(
        WINDOWS_LAUNCHER_SUFFIX
    ):
        return bin_dir / (launcher_name + WINDOWS_LAUNCHER_SUFFIX)
    return bin_dir / launcher_name


def _real_path(path: Path) -> Path:
    """Return ``path`` with links resolved, following dangling links too.

    On Windows, Python 3.9's `Path.resolve()` does not follow a link whose
    target is missing, so an owned launcher link to a deleted venv would
    not be recognized; `os.path.realpath` follows it on every version.
    """

    return Path(os.path.realpath(str(path)))


def _symlink_points_to(link: Path, target: Path) -> bool:
    """Return True when `link` is a symlink that resolves to `target`."""

    return link.is_symlink() and _real_path(link) == _real_path(target)


def _same_file_contents(left: Path, right: Path) -> bool:
    """Return True when both regular files exist with identical bytes."""

    try:
        return (
            left.is_file()
            and right.is_file()
            and filecmp.cmp(str(left), str(right), shallow=False)
        )
    except OSError:
        return False


def retired_launcher_paths(path: Path) -> List[Path]:
    """Return renamed-aside copies of one launcher, sorted by name."""

    prefix = path.name + RETIRED_LAUNCHER_INFIX
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return []
    return sorted(entry for entry in entries if entry.name.startswith(prefix))


def cleanup_retired_launchers(path: Path) -> List[Path]:
    """Delete retired launcher copies that are no longer running.

    Best effort: a copy that is still running cannot be deleted on Windows
    and stays until a later refresh.
    """

    removed: List[Path] = []
    for retired in retired_launcher_paths(path):
        try:
            retired.unlink()
        except OSError:
            continue
        removed.append(retired)
    return removed


def _retire_launcher(path: Path) -> Path:
    """Rename one in-use launcher aside so a new copy can take its name."""

    retired = path.with_name(
        "{}{}{}".format(path.name, RETIRED_LAUNCHER_INFIX, uuid.uuid4().hex[:12])
    )
    os.replace(str(path), str(retired))
    return retired


def _install_launcher_copy(target: Path, path: Path) -> None:
    """Copy `target` to `path`, renaming a running launcher aside first."""

    staged = path.with_name(path.name + STAGED_LAUNCHER_SUFFIX)
    shutil.copy2(str(target), str(staged))
    try:
        try:
            os.replace(str(staged), str(path))
            return
        except PermissionError:
            # Windows refuses to overwrite a running .exe but allows renaming
            # it, so move the busy launcher aside and retry once.
            if not path.exists():
                raise
        retired = _retire_launcher(path)
        try:
            os.replace(str(staged), str(path))
        except OSError:
            # Put the previous launcher back rather than leave none.
            try:
                os.replace(str(retired), str(path))
            except OSError:
                pass
            raise
    except OSError:
        try:
            staged.unlink()
        except OSError:
            pass
        raise


def ensure_launcher(
    bin_dir: Path,
    launcher_name: str,
    target: Path,
    *,
    windows: Optional[bool] = None,
) -> Path:
    """Create or refresh the stable user-facing launcher.

    POSIX gets a symlink named `launcher_name`. Windows gets a copied
    `<launcher_name>.exe`; an older extensionless symlink to the same target
    is removed, and an unchanged copy is left alone so a running launcher is
    not renamed aside without need.
    """

    bin_dir.mkdir(parents=True, exist_ok=True)
    if not _windows(windows):
        path = bin_dir / launcher_name
        if path.is_symlink() or path.exists():
            path.unlink()
        path.symlink_to(target)
        return path

    path = launcher_path(bin_dir, launcher_name, windows=True)
    legacy = bin_dir / launcher_name
    if legacy != path and _symlink_points_to(legacy, target):
        legacy.unlink()
    cleanup_retired_launchers(path)
    if not path.is_symlink() and _same_file_contents(target, path):
        return path
    _install_launcher_copy(target, path)
    return path


def remove_launcher_if_owned(
    bin_dir: Path,
    launcher_name: str,
    target: Path,
    *,
    windows: Optional[bool] = None,
) -> bool:
    """Remove one launcher only when it is the expected owned launcher.

    A symlink is owned when it resolves to `target`. On Windows a copied
    `.exe` is owned when its bytes match `target`; a copy that is still
    running is renamed aside for a later cleanup instead.
    """

    if not _windows(windows):
        path = bin_dir / launcher_name
        if not _symlink_points_to(path, target):
            return False
        path.unlink()
        return True

    removed = False
    path = launcher_path(bin_dir, launcher_name, windows=True)
    for candidate in dict.fromkeys([path, bin_dir / launcher_name]):
        if _symlink_points_to(candidate, target):
            candidate.unlink()
            removed = True
        elif (
            not candidate.is_symlink()
            and candidate.suffix.lower() == WINDOWS_LAUNCHER_SUFFIX
            and _same_file_contents(target, candidate)
        ):
            try:
                candidate.unlink()
            except PermissionError:
                _retire_launcher(candidate)
            removed = True
    cleanup_retired_launchers(path)
    return removed


def _comparable_path(value: str, windows: bool) -> str:
    """Return a normalized spelling of one directory for PATH comparisons."""

    expanded = os.path.expandvars(os.path.expanduser(value.strip().strip('"')))
    if windows:
        return ntpath.normcase(ntpath.normpath(expanded)).rstrip("\\")
    return posixpath.normpath(expanded).rstrip("/") or "/"


def directory_on_path(
    directory: Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
    windows: Optional[bool] = None,
) -> bool:
    """Return True when `directory` is one of the PATH entries."""

    windows = _windows(windows)
    env = os.environ if environ is None else environ
    raw_path = env.get("PATH")
    if raw_path is None and windows:
        raw_path = env.get("Path", "")
    separator = ";" if windows else ":"
    wanted = _comparable_path(str(directory), windows)
    return any(
        entry.strip() and _comparable_path(entry, windows) == wanted
        for entry in (raw_path or "").split(separator)
    )


def path_hint(
    bin_dir: Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
    windows: Optional[bool] = None,
) -> Optional[str]:
    """Return how to add `bin_dir` to PATH, or None when it is already there.

    The installers only print this; they never edit PATH or run setx,
    which truncates long values.
    """

    windows = _windows(windows)
    if directory_on_path(bin_dir, environ=environ, windows=windows):
        return None
    if windows:
        quoted = str(bin_dir).replace("'", "''")
        return (
            "{} is not on PATH. To add it for your user, run this in "
            "PowerShell, then open a new terminal:\n"
            "  [Environment]::SetEnvironmentVariable('Path', "
            "[Environment]::GetEnvironmentVariable('Path', 'User') + "
            "';{}', 'User')".format(bin_dir, quoted)
        )
    return (
        "{} is not on PATH. Add this line to your shell profile, then open a "
        'new shell:\n  export PATH={}:"$PATH"'.format(
            bin_dir, shlex.quote(str(bin_dir))
        )
    )


def print_path_hint(bin_dir: Path) -> None:
    """Print the PATH hint for `bin_dir` when it is not on PATH."""

    hint = path_hint(bin_dir)
    if hint is not None:
        print("PATH: {}".format(hint))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Install claude-wrangler into a dedicated venv and expose the launcher."""

    args = parse_args(argv or sys.argv[1:])
    repo_root = args.repo_root.resolve()
    try:
        target_user_home = resolve_user_home(
            repo_root,
            args.user_home,
            allow_isolated_home=args.allow_isolated_home,
        )
        venv_path = (
            args.venv.expanduser().resolve()
            if args.venv is not None
            else default_tool_venv(target_user_home, DEFAULT_LAUNCHER_NAME)
        )
        bin_dir = (
            args.bin_dir.expanduser().resolve()
            if args.bin_dir is not None
            else default_user_bin_dir(target_user_home)
        )
        python_executable = select_install_python(
            repo_root,
            args.python,
            target_user_home,
        )
        venv_python = ensure_virtualenv(python_executable, venv_path)
        install_build_bootstrap(venv_python)
        subprocess.run(
            build_install_command(
                venv_python,
                repo_root,
                editable=args.editable,
                dev=args.dev,
            ),
            check=True,
        )
        installed_launcher = ensure_launcher(
            bin_dir,
            args.launcher_name,
            installed_command_path(venv_path),
        )
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print("[install-user-tool] FAIL: {}".format(error), file=sys.stderr)
        return 1

    print("[install-user-tool] PASS")
    print("Repository: {}".format(repo_root))
    print("User home: {}".format(target_user_home))
    print(
        "User home source: {}".format(
            describe_user_home_source(repo_root, target_user_home, args.user_home)
        )
    )
    print("Python: {}".format(python_executable))
    print("Virtualenv: {}".format(venv_path))
    print("Launcher: {}".format(installed_launcher))
    print_path_hint(bin_dir)
    return 0


__all__ = [
    "DEFAULT_BIN_DIR",
    "DEFAULT_LAUNCHER_NAME",
    "DEFAULT_VENV",
    "build_install_command",
    "build_bootstrap_command",
    "build_package_spec",
    "cleanup_retired_launchers",
    "default_pyenv_root",
    "directory_on_path",
    "ensure_launcher",
    "installed_command_path",
    "launcher_path",
    "load_managed_runtime_name",
    "main",
    "managed_runtime_python",
    "parse_args",
    "path_hint",
    "print_path_hint",
    "pyenv_python_path",
    "remove_launcher_if_owned",
    "retired_launcher_paths",
    "select_install_python",
    "venv_bin_dir",
    "venv_python_path",
]
