#!/usr/bin/env python3
"""Shared helpers for the managed stage-1 and stage-2 bootstrap flow."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence

CONFIG_FILE_NAME = "python-environments.json"
PYENV_INIT_BEGIN = "# >>> theknowledge pyenv init >>>"
PYENV_INIT_END = "# <<< theknowledge pyenv init <<<"
DIRENV_BEGIN = "# >>> theknowledge direnv >>>"
DIRENV_END = "# <<< theknowledge direnv <<<"
ENVRC_MARKER = "# Managed by scripts/install-stage-2.py"
PYENV_REPO = "https://github.com/pyenv/pyenv.git"
PYENV_PLUGIN_REPO = "https://github.com/pyenv/pyenv-virtualenv.git"


@dataclass(frozen=True)
class PythonContextConfig:
    """Describe one configured bootstrap or runtime Python context."""

    required_version: str
    base_version: str
    environment_name: str


@dataclass(frozen=True)
class PythonEnvironmentConfig:
    """Hold the bootstrap and runtime context declarations."""

    bootstrap: PythonContextConfig
    runtime: PythonContextConfig


def slugify_project_name(name: str) -> str:
    """Convert a project directory name into a stable lowercase slug."""

    lowered = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "project"


def _load_context(data: Dict[str, object], key: str) -> PythonContextConfig:
    """Load one context record from parsed JSON data."""

    entry = data.get(key)
    if not isinstance(entry, dict):
        raise ValueError("python-environments config must define '{}'".format(key))

    required = entry.get("required_version")
    base = entry.get("base_version")
    env_name = entry.get("environment_name")
    if not isinstance(required, str):
        raise ValueError(
            "python-environments.{}.required_version must be a string".format(key)
        )
    if not isinstance(base, str):
        raise ValueError(
            "python-environments.{}.base_version must be a string".format(key)
        )
    if not isinstance(env_name, str):
        raise ValueError(
            "python-environments.{}.environment_name must be a string".format(key)
        )
    return PythonContextConfig(
        required_version=required,
        base_version=base,
        environment_name=env_name,
    )


def load_python_environment_config(
    repo_root: Path, config_name: str = CONFIG_FILE_NAME
) -> PythonEnvironmentConfig:
    """Load and validate the bootstrap/runtime Python configuration."""

    config_path = repo_root / config_name
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("python-environments config must be a JSON object")
    return PythonEnvironmentConfig(
        bootstrap=_load_context(data, "bootstrap"),
        runtime=_load_context(data, "runtime"),
    )


def version_tuple(version_text: str) -> tuple:
    """Parse a dotted Python version string into a comparable tuple."""

    return tuple(int(part) for part in version_text.split("."))


def ensure_minimum_python(
    version_info: Sequence[int], minimum_version: str, label: str
) -> None:
    """Fail when the active interpreter is below the configured minimum."""

    current = tuple(version_info[: len(version_tuple(minimum_version))])
    minimum = version_tuple(minimum_version)
    if current < minimum:
        current_text = ".".join(str(part) for part in current)
        raise RuntimeError(
            "{} requires Python {}+ but is running on {}.".format(
                label,
                minimum_version,
                current_text,
            )
        )


def default_pyenv_root() -> Path:
    """Return the user-scoped pyenv root for the managed bootstrap flow."""

    configured = os.environ.get("PYENV_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".pyenv").resolve()


def pyenv_bin(pyenv_root_path: Path) -> Path:
    """Return the expected `pyenv` executable path."""

    executable = "pyenv.exe" if os.name == "nt" else "pyenv"
    return pyenv_root_path / "bin" / executable


def pyenv_environment(pyenv_root_path: Path) -> Dict[str, str]:
    """Return environment variables suitable for pyenv subprocesses."""

    env = os.environ.copy()
    env["PYENV_ROOT"] = str(pyenv_root_path)
    pyenv_bin_dir = str(pyenv_root_path / "bin")
    current_path = env.get("PATH", "")
    env["PATH"] = (
        pyenv_bin_dir + os.pathsep + current_path if current_path else pyenv_bin_dir
    )
    return env


def pyenv_command(pyenv_root_path: Path, *args: str) -> list:
    """Build one pyenv command line using the managed pyenv executable."""

    return [str(pyenv_bin(pyenv_root_path)), *args]


def context_requires_virtualenv(context: PythonContextConfig) -> bool:
    """Return True when the context still needs `pyenv-virtualenv`."""

    return context.environment_name != context.base_version


def selection_name(context: PythonContextConfig) -> str:
    """Return the value that should be written to `.python-version`."""

    if context_requires_virtualenv(context):
        return context.environment_name
    return context.base_version


def ensure_pyenv_installed(
    pyenv_root_path: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Path:
    """Clone or update the user-scoped pyenv checkout and return its binary."""

    pyenv_executable = pyenv_bin(pyenv_root_path)
    if pyenv_executable.exists():
        runner(["git", "-C", str(pyenv_root_path), "pull", "--ff-only"])
        return pyenv_executable

    runner(["git", "clone", PYENV_REPO, str(pyenv_root_path)])
    return pyenv_executable


def ensure_pyenv_virtualenv_plugin(
    pyenv_root_path: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Path:
    """Clone or update the optional `pyenv-virtualenv` plugin checkout."""

    plugin_root = pyenv_root_path / "plugins" / "pyenv-virtualenv"
    if plugin_root.exists():
        runner(["git", "-C", str(plugin_root), "pull", "--ff-only"])
        return plugin_root

    plugin_root.parent.mkdir(parents=True, exist_ok=True)
    runner(["git", "clone", PYENV_PLUGIN_REPO, str(plugin_root)])
    return plugin_root


def pyenv_python_executable(pyenv_root_path: Path, environment_name: str) -> Path:
    """Return the expected Python executable path for one pyenv selection."""

    executable = "python.exe" if os.name == "nt" else "python"
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    return pyenv_root_path / "versions" / environment_name / bin_dir / executable


def ensure_pyenv_context(
    pyenv_root_path: Path,
    context: PythonContextConfig,
    runner: Callable[..., subprocess.CompletedProcess],
) -> str:
    """Ensure one configured pyenv version or legacy virtualenv exists."""

    env = pyenv_environment(pyenv_root_path)
    runner(
        pyenv_command(pyenv_root_path, "install", "-s", context.base_version), env=env
    )
    if context_requires_virtualenv(context):
        ensure_pyenv_virtualenv_plugin(pyenv_root_path, runner)
        runner(
            pyenv_command(
                pyenv_root_path,
                "virtualenv",
                "--force",
                context.base_version,
                context.environment_name,
            ),
            env=env,
        )
    return selection_name(context)


def write_python_version_file(repo_root: Path, environment_name: str) -> None:
    """Write `.python-version` so pyenv defaults to the runtime context."""

    (repo_root / ".python-version").write_text(
        environment_name + "\n", encoding="utf-8"
    )


def detect_shell_name(shell_path: str) -> str:
    """Return the shell basename used for startup hook selection."""

    if not shell_path:
        return "bash"
    return os.path.basename(shell_path)


def shell_rc_path(home: Path, shell_name: str) -> Optional[Path]:
    """Return the rc file path that should receive managed shell hooks."""

    if shell_name == "bash":
        return home / ".bashrc"
    if shell_name == "zsh":
        return home / ".zshrc"
    return None


def upsert_managed_block(text: str, begin: str, end: str, replacement: str) -> str:
    """Replace or append one managed text block."""

    if begin in text and end in text:
        start = text.index(begin)
        finish = text.index(end, start) + len(end)
        if finish < len(text) and text[finish] == "\n":
            finish += 1
        return text[:start] + replacement + text[finish:]

    if text and not text.endswith("\n"):
        text += "\n"
    return text + replacement


def pyenv_shell_init_snippet() -> str:
    """Return the idempotent shell-init block for pyenv integration."""

    return "\n".join(
        [
            PYENV_INIT_BEGIN,
            'export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"',
            'case ":$PATH:" in',
            '  *":$PYENV_ROOT/bin:"*) ;;',
            '  *) export PATH="$PYENV_ROOT/bin:$PATH" ;;',
            "esac",
            "if command -v pyenv >/dev/null 2>&1; then",
            '  eval "$(pyenv init -)"',
            '  if pyenv commands | grep -q "^virtualenv-init$"; then',
            '    eval "$(pyenv virtualenv-init -)"',
            "  fi",
            "fi",
            PYENV_INIT_END,
            "",
        ]
    )


def default_shell_init_files(home: Optional[Path] = None) -> list:
    """Return the common shell startup files that may need managed hooks."""

    root = home or Path.home()
    return [root / ".bashrc", root / ".zshrc", root / ".profile"]


def append_shell_init_snippet(path: Path, snippet: str) -> None:
    """Append one managed shell-init snippet to a startup file once."""

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = upsert_managed_block(existing, PYENV_INIT_BEGIN, PYENV_INIT_END, snippet)
    path.write_text(updated, encoding="utf-8")


def build_direnv_hook_block(shell_name: str) -> str:
    """Return the managed direnv shell-hook block."""

    return "\n".join(
        [
            DIRENV_BEGIN,
            'case ":$PATH:" in',
            '  *":$HOME/.local/bin:"*) ;;',
            '  *) export PATH="$HOME/.local/bin:$PATH" ;;',
            "esac",
            "if command -v direnv >/dev/null 2>&1; then",
            '  eval "$(direnv hook {})"'.format(shell_name),
            "fi",
            DIRENV_END,
            "",
        ]
    )


def build_envrc_content() -> str:
    """Return the managed `.envrc` content for developer installs."""

    return "\n".join(
        [
            ENVRC_MARKER,
            'if [ ! -f "$PWD/.venv/bin/activate" ]; then',
            '  echo "Missing .venv; rerun ./install.sh." >&2',
            "  exit 1",
            "fi",
            'source "$PWD/.venv/bin/activate"',
            "",
        ]
    )


def direnv_download_name(platform_name: str, machine: str) -> str:
    """Map platform and machine tuples to direnv download artifact names."""

    normalized_machine = machine.lower()
    if platform_name != "linux":
        raise ValueError("Unsupported platform for automatic direnv install")
    if normalized_machine in ("x86_64", "amd64"):
        return "direnv.linux-amd64"
    if normalized_machine in ("aarch64", "arm64"):
        return "direnv.linux-arm64"
    raise ValueError("Unsupported machine for automatic direnv install")
