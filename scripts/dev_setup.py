#!/usr/bin/env python3
"""Bootstrap a starter Python toolchain from pinned requirements."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = REPO_ROOT / ".venv"
REQUIREMENTS_FILE = REPO_ROOT / "requirements-dev.txt"
STEADY_STATE_RUNTIME_POLICY = "steady_state_python_tools"

try:
    from tool_validation_profiles import resolve_runtime_policy_executable
except ImportError:  # pragma: no cover - the installed starter has the helper.
    helper_dir = REPO_ROOT / "scripts"
    if str(helper_dir) not in sys.path:
        sys.path.insert(0, str(helper_dir))
    from tool_validation_profiles import resolve_runtime_policy_executable


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or refresh the starter Python environment from the pinned "
            "TheKnowledge tool requirements."
        )
    )
    parser.add_argument(
        "--python",
        default=None,
        help=(
            "Override the managed steady-state Python interpreter used to "
            "create the virtual environment."
        ),
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=DEFAULT_VENV,
        help="Virtual environment path.",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip pip installation after ensuring the virtual environment.",
    )
    return parser.parse_args(argv)


def venv_python_path(venv_path: Path) -> Path:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv_path / bin_dir / executable


def run(command: Sequence[object]) -> None:
    printable = " ".join(str(part) for part in command)
    print(f"[dev-setup] -> {printable}")
    subprocess.run([str(part) for part in command], cwd=REPO_ROOT, check=True)


def python_version(executable: object) -> str:
    """Return the interpreter's reported Python version string."""

    completed = subprocess.run(
        [str(executable), "--version"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    version_text = (completed.stdout or completed.stderr).strip()
    prefix = "Python "
    if not version_text.startswith(prefix):
        raise RuntimeError(
            "Unexpected version output from {}: {}".format(executable, version_text)
        )
    return version_text[len(prefix) :]


def ensure_virtualenv(python_executable: str, venv_path: Path) -> Path:
    """Create `.venv`, or rebuild it when the interpreter version changed."""

    python_path = venv_python_path(venv_path)
    if python_path.exists():
        if python_version(python_path) != python_version(python_executable):
            run([python_executable, "-m", "venv", "--clear", venv_path])
        return python_path

    run([python_executable, "-m", "venv", venv_path])
    return python_path


def select_tool_python(python_override: Optional[str]) -> str:
    return resolve_runtime_policy_executable(
        REPO_ROOT,
        STEADY_STATE_RUNTIME_POLICY,
        explicit_candidate=python_override,
        required_modules=[],
    )


def install_requirements(python_executable: Path) -> None:
    if not REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"Missing requirements file: {REQUIREMENTS_FILE}")

    run(
        [
            python_executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel",
        ]
    )
    run(
        [
            python_executable,
            "-m",
            "pip",
            "install",
            "-r",
            REQUIREMENTS_FILE,
        ]
    )
    run([python_executable, "--version"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        tool_python = select_tool_python(args.python)
        print(f"[dev-setup] managed tool runtime: {tool_python}")
        python_executable = ensure_virtualenv(tool_python, args.venv)
        if not args.skip_install:
            install_requirements(python_executable)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[dev-setup] FAIL: {error}", file=sys.stderr)
        return 1

    print("[dev-setup] PASS: pinned starter Python tooling is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
