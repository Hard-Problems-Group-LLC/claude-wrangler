"""Project-specific install hook for repo-bound dev and standard installs.

Stage 2 of the managed installer runs this hook with the target venv and
launcher directory. Launcher publication follows `installer.ensure_launcher`:
a symlink on POSIX and a copied `claude-wrangler.exe` on Windows. A
filesystem error while publishing the launcher (for example a locked file
or a missing symlink privilege) is reported as a clean FAIL, and a launcher
directory that is not on `PATH` produces a hint, never a `PATH` edit.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence

from claude_wrangler.installer import (
    DEFAULT_BIN_DIR,
    DEFAULT_LAUNCHER_NAME,
    build_install_command,
    ensure_launcher,
    installed_command_path,
    print_path_hint,
    remove_launcher_if_owned,
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments for the repo-specific install hook."""

    parser = argparse.ArgumentParser(
        description=(
            "Install claude-wrangler into the selected managed virtual "
            "environment and publish the matching launcher policy."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("dev", "standard", "venv-only"),
        required=True,
        help="Managed install mode selected by scripts/install-stage-2.py.",
    )
    parser.add_argument(
        "--scope",
        choices=("repo", "user", "system"),
        required=True,
        help="Managed install scope selected by scripts/install-stage-2.py.",
    )
    parser.add_argument(
        "--python",
        required=True,
        help="Python executable inside the managed target virtual environment.",
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=None,
        help="Managed target virtual-environment path. Defaults from --python.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Path to the claude-wrangler repository checkout.",
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=DEFAULT_BIN_DIR,
        help="Directory that should receive the project-bound developer launcher.",
    )
    parser.add_argument(
        "--launcher-name",
        default=DEFAULT_LAUNCHER_NAME,
        help="Filename to manage under the launcher directory.",
    )
    return parser.parse_args(argv)


def repo_venv_path(venv_python: Path) -> Path:
    """Return the virtual-environment root from its Python executable path."""

    return absolute_path_preserving_symlinks(venv_python).parents[1]


def absolute_path_preserving_symlinks(path: Path) -> Path:
    """Return one absolute path without collapsing any symlinked segments."""

    return Path(os.path.abspath(str(path.expanduser())))


def resolve_target_venv(args: argparse.Namespace, venv_python: Path) -> Path:
    """Resolve the managed target virtual environment path."""

    if args.venv is not None:
        return args.venv.expanduser().resolve()
    return repo_venv_path(venv_python)


def build_project_install_command(
    venv_python: Path,
    repo_root: Path,
    mode: str,
) -> Sequence[str]:
    """Build the pip install command required for one managed mode."""

    if mode == "dev":
        return build_install_command(venv_python, repo_root, editable=True, dev=True)
    if mode == "standard":
        return build_install_command(venv_python, repo_root, editable=False, dev=False)
    raise ValueError("No project install command for mode {}".format(mode))


def install_project_package(venv_python: Path, repo_root: Path, mode: str) -> None:
    """Install the package into the repo-local virtual environment when needed."""

    if mode == "venv-only":
        return
    subprocess.run(
        list(build_project_install_command(venv_python, repo_root, mode)),
        cwd=str(repo_root),
        check=True,
    )


def manage_project_launcher(
    venv_path: Path,
    *,
    mode: str,
    scope: str,
    bin_dir: Path,
    launcher_name: str,
) -> Optional[Path]:
    """Install or remove the managed launcher for one mode/scope pair."""

    target = installed_command_path(venv_path)
    if mode == "dev":
        if not target.is_file():
            raise RuntimeError("Missing repo-local launcher target: {}".format(target))
        return ensure_launcher(bin_dir, launcher_name, target)

    if mode == "standard":
        if scope not in ("user", "system"):
            raise RuntimeError(
                "Standard installs require user or system scope, not {}".format(scope)
            )
        if not target.is_file():
            raise RuntimeError("Missing installed launcher target: {}".format(target))
        return ensure_launcher(bin_dir, launcher_name, target)

    if mode == "venv-only":
        removed = remove_launcher_if_owned(bin_dir, launcher_name, target)
        return None if removed else Path()

    raise RuntimeError("Unsupported install mode: {}".format(mode))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Install claude-wrangler into `.venv` and manage the dev launcher."""

    args = parse_args(argv or sys.argv[1:])
    repo_root = args.repo_root.resolve()
    venv_python = absolute_path_preserving_symlinks(Path(args.python))
    venv_path = resolve_target_venv(args, venv_python)
    bin_dir = args.bin_dir.expanduser().resolve()
    try:
        install_project_package(venv_python, repo_root, args.mode)
        launcher_path = manage_project_launcher(
            venv_path,
            mode=args.mode,
            scope=args.scope,
            bin_dir=bin_dir,
            launcher_name=args.launcher_name,
        )
    except (RuntimeError, subprocess.CalledProcessError, ValueError, OSError) as error:
        print("[install-project] FAIL: {}".format(error), file=sys.stderr)
        return 1

    print("[install-project] PASS")
    print("Repository: {}".format(repo_root))
    print("Mode: {}".format(args.mode))
    print("Scope: {}".format(args.scope))
    print("Virtualenv: {}".format(venv_path))
    if launcher_path is None:
        print("Launcher: owned launcher removed.")
    elif launcher_path == Path():
        print("Launcher: no launcher change.")
    else:
        print("Launcher: {}".format(launcher_path))
        print_path_hint(bin_dir)
    return 0


__all__ = [
    "build_project_install_command",
    "install_project_package",
    "main",
    "manage_project_launcher",
    "parse_args",
    "repo_venv_path",
    "resolve_target_venv",
]
