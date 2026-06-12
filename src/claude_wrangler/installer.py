"""Helpers for installing claude-wrangler into a dedicated user venv."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence

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
            "Directory that will receive the user-facing launcher symlink. "
            "Defaults to <user-home>/.local/bin."
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


def venv_bin_dir(venv_path: Path) -> Path:
    """Return the platform-specific venv executable directory."""

    return venv_path / ("Scripts" if os.name == "nt" else "bin")


def pyenv_python_path(pyenv_root: Path, environment_name: str) -> Path:
    """Return the Python path for one installed pyenv environment name."""

    executable = "python.exe" if os.name == "nt" else "python"
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    return pyenv_root / "versions" / environment_name / bin_dir / executable


def venv_python_path(venv_path: Path) -> Path:
    """Return the virtual environment Python executable path."""

    executable = "python.exe" if os.name == "nt" else "python"
    return venv_bin_dir(venv_path) / executable


def installed_command_path(venv_path: Path) -> Path:
    """Return the venv-installed claude-wrangler command path."""

    executable = "claude-wrangler.exe" if os.name == "nt" else "claude-wrangler"
    return venv_bin_dir(venv_path) / executable


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
    candidate = pyenv_python_path(default_pyenv_root(user_home), selection)
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


def ensure_launcher(bin_dir: Path, launcher_name: str, target: Path) -> Path:
    """Create or refresh the stable user-facing launcher symlink."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = bin_dir / launcher_name
    if launcher_path.is_symlink() or launcher_path.exists():
        launcher_path.unlink()
    launcher_path.symlink_to(target)
    return launcher_path


def remove_launcher_if_owned(bin_dir: Path, launcher_name: str, target: Path) -> bool:
    """Remove one launcher only when it points at the expected owned target."""

    launcher_path = bin_dir / launcher_name
    if not launcher_path.is_symlink():
        return False
    if launcher_path.resolve(strict=False) != target.resolve(strict=False):
        return False
    launcher_path.unlink()
    return True


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
        launcher_path = ensure_launcher(
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
    print("Launcher: {}".format(launcher_path))
    return 0


__all__ = [
    "DEFAULT_BIN_DIR",
    "DEFAULT_LAUNCHER_NAME",
    "DEFAULT_VENV",
    "build_install_command",
    "build_bootstrap_command",
    "build_package_spec",
    "default_pyenv_root",
    "ensure_launcher",
    "installed_command_path",
    "load_managed_runtime_name",
    "main",
    "managed_runtime_python",
    "parse_args",
    "pyenv_python_path",
    "remove_launcher_if_owned",
    "select_install_python",
    "venv_bin_dir",
    "venv_python_path",
]
