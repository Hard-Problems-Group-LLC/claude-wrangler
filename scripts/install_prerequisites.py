#!/usr/bin/env python3
"""Install missing prerequisites for this project on Linux, macOS, and Windows.

This is the project-owned peer of TheKnowledge's installer of the same
name. It keeps that file's structure and function names (`detect_os_info`,
`select_package_manager`, `system_packages_for`, `build_install_commands`,
`CommandRunner`, `ensure_virtualenv`, ...) and extends it orthogonally:

- Missing-only installs. Each prerequisite is detected first: Python 3.9+,
  Git, and on Windows the bash that Git for Windows ships (Claude Code's
  Bash tool uses it; on by default, `--no-git-bash` skips it). Only the
  missing ones are installed. Node.js is not a prerequisite.
- Package managers: apt-get, dnf/yum, brew, and on Windows winget, with
  `--scope user` for packages that support a per-user install.
- Consent: `--dry-run` only prints commands (and never reports the host
  ready while something is missing); `--yes` runs them without prompting;
  otherwise an interactive run asks once and a non-interactive run
  refuses.
- Privilege: sudo is used only by an operator who passed
  `--system-packages` (root needs no sudo). Windows is never elevated; a
  command that needs elevation is printed for the operator to run.
- `--dev` additionally creates the repo-local `.venv` (`Scripts\\python.exe`
  on Windows) and installs the project with its development extras, which
  gives Windows contributors a development path while the managed
  `install.sh --mode dev` (pyenv and direnv) stays POSIX-only.

Standard library only and Python 3.9 compatible. TheKnowledge modules are
never imported; the project's own `scripts/tool_validation_profiles.py` is
used to pick the development interpreter when it is importable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import importlib.util
import os
from pathlib import Path, PureWindowsPath
import platform
import shlex
import shutil
import subprocess
import sys
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from tool_validation_profiles import resolve_runtime_policy_executable
except (ImportError, SyntaxError):  # pragma: no cover - depends on entry point.
    try:
        from scripts.tool_validation_profiles import resolve_runtime_policy_executable
    except (ImportError, SyntaxError):
        resolve_runtime_policy_executable = None


@dataclass
class OSInfo:
    """Information parsed from the host operating system."""

    platform_id: str
    version_id: str
    pretty_name: str


REPO_ROOT = Path(__file__).resolve().parent.parent
STEADY_STATE_RUNTIME_POLICY = "steady_state_python_tools"
MINIMUM_PYTHON = (3, 9)
# The development extras (pytest 9, black 26) need the steady-state floor.
DEV_MINIMUM_PYTHON = (3, 12)
# macOS ships these as xcode-select stubs that exist but only open the
# Command Line Tools installer until the tools are installed. Only git is
# looked up through `which`, so only it is gated on `xcode-select -p`;
# Python is judged by running it (the wrappers' probe rejects the stub).
MACOS_CLT_STUBS = frozenset({"/usr/bin/git", "/usr/bin/python3"})
MACOS_CLT_INSTALL_HINT = (
    "run `xcode-select --install` to install the Command Line Tools"
)
PROBE_TIMEOUT_SECONDS = 15
GIT_HOOK_INSTALLER = REPO_ROOT / "scripts" / "install_git_hooks.py"
# Claude Code reads this variable when Git for Windows' bash is not where
# it expects; an operator who set it has chosen a bash explicitly.
GIT_BASH_ENV = "CLAUDE_CODE_GIT_BASH_PATH"

PYTHON = "python"
GIT = "git"
GIT_BASH = "git-bash"
PYTHON_VENV = "python-venv"
PREREQUISITE_LABELS = {
    PYTHON: "Python {}.{}+".format(*MINIMUM_PYTHON),
    GIT: "Git",
    GIT_BASH: "Git for Windows bash",
    PYTHON_VENV: "Python venv support",
}
MANUAL_INSTALL_HINTS = {
    PYTHON: "install Python 3.9+ from https://www.python.org/downloads/",
    GIT: "install Git from https://git-scm.com/downloads",
    GIT_BASH: (
        "install Git for Windows from https://git-scm.com/download/win, or set "
        "{} to an existing bash.exe".format(GIT_BASH_ENV)
    ),
    PYTHON_VENV: (
        "install your distribution's Python venv package (python3-venv on "
        "Debian and Ubuntu)"
    ),
}
SUPPORTED_PLATFORMS = frozenset(
    {
        "ubuntu",
        "debian",
        "linuxmint",
        "mint",
        "rocky",
        "fedora",
        "macos",
        "windows",
    }
)
# Package names per manager and prerequisite. An empty list means the
# prerequisite cannot be missing on its own with that manager.
PACKAGES_BY_MANAGER: Dict[str, Dict[str, List[str]]] = {
    "apt-get": {
        PYTHON: ["python3", "python3-venv", "python3-pip"],
        GIT: ["git"],
        PYTHON_VENV: ["python3-venv"],
    },
    "dnf": {PYTHON: ["python3", "python3-pip"], GIT: ["git"], PYTHON_VENV: []},
    "yum": {PYTHON: ["python3", "python3-pip"], GIT: ["git"], PYTHON_VENV: []},
    "brew": {PYTHON: ["python"], GIT: ["git"], PYTHON_VENV: []},
    "winget": {
        PYTHON: ["Python.Python.3.12"],
        GIT: ["Git.Git"],
        GIT_BASH: ["Git.Git"],
        PYTHON_VENV: [],
    },
}
# winget packages whose manifests offer a per-user installer, so they
# install with `--scope user` and never need elevation. Any other package
# would trigger a UAC prompt and is printed for the operator instead.
WINGET_USER_SCOPE_PACKAGES = frozenset({"Python.Python.3.12", "Git.Git"})
ROOT_MANAGERS = frozenset({"apt-get", "dnf", "yum"})


def path_is_file(path: str) -> bool:
    """Return True when `path` names an existing regular file."""

    return os.path.isfile(path)


def command_succeeds(command: Sequence[str]) -> bool:
    """Return True when `command` runs and exits 0 within the probe timeout."""

    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@dataclass
class HostFacts:
    """Host facts that detection and policy read; tests inject fakes."""

    os_info: OSInfo
    python_version: Tuple[int, ...]
    venv_available: bool
    is_root: bool
    interactive: bool
    env: Mapping[str, str]
    which: Callable[[str], Optional[str]] = shutil.which
    is_file: Callable[[str], bool] = path_is_file
    python_executable: str = sys.executable
    succeeds: Callable[[Sequence[str]], bool] = command_succeeds


@dataclass
class InstallPlan:
    """What to install, split into commands run here and operator commands."""

    manager: Optional[str]
    packages: List[str] = field(default_factory=list)
    commands: List[List[str]] = field(default_factory=list)
    operator_commands: List[List[str]] = field(default_factory=list)
    operator_reason: str = ""
    # Prerequisites covered only by operator commands.
    operator_prerequisites: List[str] = field(default_factory=list)
    # Prerequisites this run cannot install at all.
    unresolved: List[str] = field(default_factory=list)


def format_command(command: Sequence[str]) -> str:
    """Return a copy-pasteable rendering of one command for this host."""

    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    """Execute commands, or only print them in dry-run mode."""

    def __init__(self, dry_run: bool = False) -> None:
        """Remember whether commands should only be printed."""

        self.dry_run = dry_run

    def run(self, command: Sequence[str], cwd: Optional[Path] = None) -> None:
        """Print one command and, unless in dry-run mode, run it to success."""

        print("-> {}".format(format_command(command)), flush=True)
        if self.dry_run:
            return
        subprocess.run(
            list(command), check=True, cwd=str(cwd) if cwd is not None else None
        )


def parse_os_release(content: str) -> Dict[str, str]:
    """Parse the contents of an os-release style string."""

    data: Dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def load_os_release(path: Path = Path("/etc/os-release")) -> Dict[str, str]:
    """Read and parse an os-release file, or return {} when it is absent."""

    if not path.exists():
        return {}
    return parse_os_release(path.read_text())


def linux_os_info(release: Mapping[str, str]) -> OSInfo:
    """Build OSInfo from os-release data, mapping derivatives via ID_LIKE."""

    platform_id = release.get("ID", "linux").lower()
    if platform_id not in SUPPORTED_PLATFORMS:
        # Derivatives such as Pop!_OS (ID_LIKE="ubuntu debian") or AlmaLinux
        # (ID_LIKE="rhel centos fedora") use their parent's package manager.
        for candidate in release.get("ID_LIKE", "").lower().split():
            if candidate in SUPPORTED_PLATFORMS:
                platform_id = candidate
                break
    version_id = release.get("VERSION_ID", "")
    pretty_name = release.get("PRETTY_NAME", platform_id)
    return OSInfo(platform_id, version_id, pretty_name)


def detect_os_info() -> OSInfo:
    """Return OSInfo for the running host."""

    system = platform.system().lower()
    if system == "windows":
        version = platform.version()
        return OSInfo("windows", version, "Windows")
    if system == "darwin":
        version = platform.mac_ver()[0]
        return OSInfo("macos", version, "macOS")
    if system == "linux":
        return linux_os_info(load_os_release())
    return OSInfo(system, "", system)


def detect_is_root() -> bool:
    """Return True when running as root on POSIX; always False on Windows."""

    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def detect_venv_available() -> bool:
    """Return True when this interpreter can create a venv with pip.

    Debian and Ubuntu split `ensurepip` into the python3-venv package, so
    `venv` imports while `python -m venv` still fails.
    """

    return all(
        importlib.util.find_spec(name) is not None for name in ("venv", "ensurepip")
    )


def stdin_is_interactive() -> bool:
    """Return True when stdin is an interactive terminal or console."""

    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def detect_host_facts() -> HostFacts:
    """Probe the running host without side effects."""

    return HostFacts(
        os_info=detect_os_info(),
        python_version=tuple(sys.version_info[:3]),
        venv_available=detect_venv_available(),
        is_root=detect_is_root(),
        interactive=stdin_is_interactive(),
        env=os.environ,
    )


def is_windows(os_info: OSInfo) -> bool:
    """Return True for native Windows hosts."""

    return os_info.platform_id == "windows"


def select_package_manager(
    os_info: OSInfo, available: Optional[Iterable[str]] = None
) -> Optional[str]:
    """Choose the best-fit package manager for the detected platform."""

    if is_windows(os_info):
        if available is None:
            available = [name for name in ("winget",) if shutil.which(name)]
        return "winget" if "winget" in set(available) else None

    preferred: List[str] = []
    if os_info.platform_id in {"ubuntu", "debian", "linuxmint", "mint"}:
        preferred.append("apt-get")
    elif os_info.platform_id in {"rocky", "fedora"}:
        preferred.extend(["dnf", "yum"])
    elif os_info.platform_id == "macos":
        preferred.append("brew")

    search_order = preferred + ["apt-get", "dnf", "yum", "brew"]
    if available is None:
        available = [cmd for cmd in search_order if shutil.which(cmd)]
    available_set = set(available)
    for candidate in search_order:
        if candidate in available_set:
            return candidate
    return None


def git_bash_candidates(
    env: Mapping[str, str], which: Callable[[str], Optional[str]]
) -> List[str]:
    """Return the places Git for Windows' bash.exe may live, in search order.

    Mirrors how Claude Code looks for it: an explicit
    `CLAUDE_CODE_GIT_BASH_PATH`, then `bin\\bash.exe` under the Git for
    Windows root that holds `git.exe` on PATH (git.exe lives in `cmd\\`,
    `bin\\`, or `mingw64\\bin\\` under that root), then the default
    per-machine and per-user install directories.
    """

    candidates: List[str] = []
    override = env.get(GIT_BASH_ENV)
    if override:
        candidates.append(override)
    git = which("git")
    if git:
        git_dir = PureWindowsPath(git).parent
        for git_root in (git_dir.parent, git_dir.parent.parent):
            candidates.append(str(git_root / "bin" / "bash.exe"))
    for variable, parts in (
        ("ProgramFiles", ("Git",)),
        ("ProgramW6432", ("Git",)),
        ("LOCALAPPDATA", ("Programs", "Git")),
    ):
        base = env.get(variable)
        if base:
            candidates.append(str(PureWindowsPath(base, *parts, "bin", "bash.exe")))
    unique: List[str] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def find_git_bash(
    env: Mapping[str, str],
    which: Callable[[str], Optional[str]],
    is_file: Callable[[str], bool] = path_is_file,
) -> Optional[str]:
    """Return Git for Windows' bash.exe, or None when it is not installed."""

    for candidate in git_bash_candidates(env, which):
        if is_file(candidate):
            return candidate
    return None


def python_is_supported(version: Sequence[int]) -> bool:
    """Return True when `version` meets the Python floor."""

    return tuple(version[:2]) >= MINIMUM_PYTHON


def is_macos_clt_stub(facts: HostFacts, path: Optional[str]) -> bool:
    """Return True when `path` is a macOS Command Line Tools stub, not a tool.

    `/usr/bin/git` and `/usr/bin/python3` always exist on macOS; without
    the Command Line Tools they only open the installer dialog and fail.
    `xcode-select -p` exits 0 only when the tools are installed.
    """

    if facts.os_info.platform_id != "macos" or path not in MACOS_CLT_STUBS:
        return False
    return not facts.succeeds(["xcode-select", "-p"])


def find_git(facts: HostFacts) -> Optional[str]:
    """Return the usable git on PATH, or None (a macOS CLT stub is not one)."""

    git = facts.which("git")
    if git and is_macos_clt_stub(facts, git):
        return None
    return git


def manual_install_hint(key: str, os_info: OSInfo) -> str:
    """Return the manual-install hint for one prerequisite on this platform."""

    if key == GIT and os_info.platform_id == "macos":
        return "{}, or {}".format(MACOS_CLT_INSTALL_HINT, MANUAL_INSTALL_HINTS[key])
    return MANUAL_INSTALL_HINTS[key]


def detect_missing_prerequisites(
    facts: HostFacts, *, include_git_bash: bool, dev: bool
) -> List[str]:
    """Return the prerequisites this host lacks, in install order."""

    missing: List[str] = []
    if not python_is_supported(facts.python_version):
        missing.append(PYTHON)
    elif dev and not facts.venv_available:
        missing.append(PYTHON_VENV)
    if not find_git(facts):
        missing.append(GIT)
    if (
        include_git_bash
        and is_windows(facts.os_info)
        and find_git_bash(facts.env, facts.which, facts.is_file) is None
    ):
        missing.append(GIT_BASH)
    return missing


def describe_prerequisites(
    facts: HostFacts, missing: Sequence[str], *, include_git_bash: bool, dev: bool
) -> List[str]:
    """Return one status line per prerequisite checked on this host."""

    version_text = ".".join(str(part) for part in facts.python_version)
    lines = []
    if PYTHON in missing:
        lines.append(
            "{}: missing (running {})".format(PREREQUISITE_LABELS[PYTHON], version_text)
        )
    else:
        lines.append(
            "{}: found {} at {}".format(
                PREREQUISITE_LABELS[PYTHON], version_text, facts.python_executable
            )
        )
    if dev and PYTHON not in missing:
        lines.append(
            "{}: {}".format(
                PREREQUISITE_LABELS[PYTHON_VENV],
                "missing" if PYTHON_VENV in missing else "found",
            )
        )
    git = find_git(facts)
    stub = facts.which("git") if git is None else None
    if git:
        git_status = "found at {}".format(git)
    elif stub and is_macos_clt_stub(facts, stub):
        git_status = "missing ({} is the Command Line Tools stub; {})".format(
            stub, MACOS_CLT_INSTALL_HINT
        )
    else:
        git_status = "missing"
    lines.append("{}: {}".format(PREREQUISITE_LABELS[GIT], git_status))
    if include_git_bash and is_windows(facts.os_info):
        bash = find_git_bash(facts.env, facts.which, facts.is_file)
        lines.append(
            "{}: {}".format(
                PREREQUISITE_LABELS[GIT_BASH],
                "found at {}".format(bash) if bash else "missing",
            )
        )
    return lines


def system_packages_for(
    manager: str, missing: Optional[Iterable[str]] = None
) -> List[str]:
    """Return the packages that provide the given (default: all) prerequisites."""

    mapping = PACKAGES_BY_MANAGER.get(manager)
    if mapping is None:
        return []
    keys = list(mapping) if missing is None else list(missing)
    packages: List[str] = []
    for key in keys:
        for package in mapping.get(key, []):
            if package not in packages:
                packages.append(package)
    return packages


def should_use_sudo(
    system_packages: bool = False,
    *,
    is_root: Optional[bool] = None,
    sudo_available: Optional[bool] = None,
) -> bool:
    """Return True only for a non-root operator who allowed sudo explicitly."""

    if is_root is None:
        is_root = detect_is_root()
    if is_root or not system_packages:
        return False
    if sudo_available is None:
        sudo_available = shutil.which("sudo") is not None
    return sudo_available


def _with_prefix(prefix: List[str], command: List[str]) -> List[str]:
    """Return `command` behind `prefix`, dropping empty parts."""

    return prefix + [part for part in command if part]


def winget_install_command(
    package_id: str, *, user_scope: bool, silent: bool
) -> List[str]:
    """Build one non-elevating winget install command for an exact package id."""

    command = ["winget", "install", "--id", package_id, "--exact", "--source"]
    command.append("winget")
    if user_scope:
        command.extend(["--scope", "user"])
    command.extend(["--accept-package-agreements", "--accept-source-agreements"])
    if silent:
        command.append("--silent")
    return command


def build_install_commands(
    manager: str,
    packages: List[str],
    use_sudo: bool,
    assume_yes: bool = True,
    *,
    silent: bool = False,
) -> List[List[str]]:
    """Build the package-manager commands that install `packages`.

    `assume_yes` adds `-y` for apt-get, dnf, and yum, whose own prompt would
    otherwise follow this script's single consent prompt. `silent` adds
    winget's `--silent`, which hides installer UI and is used only with
    `--yes`.
    """

    prefix: List[str] = ["sudo"] if use_sudo else []
    commands: List[List[str]] = []

    if manager == "apt-get":
        commands.append(_with_prefix(prefix, [manager, "update"]))
        install_cmd = [manager]
        if assume_yes:
            install_cmd.append("-y")
        install_cmd.append("install")
        install_cmd.extend(packages)
        commands.append(_with_prefix(prefix, install_cmd))
    elif manager in {"dnf", "yum"}:
        install_cmd = [manager]
        if assume_yes:
            install_cmd.append("-y")
        install_cmd.append("install")
        install_cmd.extend(packages)
        commands.append(_with_prefix(prefix, install_cmd))
    elif manager == "brew":
        commands.append([manager, "update"])
        commands.append([manager, "install"] + packages)
    elif manager == "winget":
        if use_sudo:
            raise ValueError("winget commands are never elevated")
        for package in packages:
            commands.append(
                winget_install_command(
                    package,
                    user_scope=package in WINGET_USER_SCOPE_PACKAGES,
                    silent=silent,
                )
            )
    else:
        raise ValueError(f"Unsupported package manager: {manager}")

    return commands


def plan_installation(
    missing: Sequence[str],
    manager: Optional[str],
    facts: HostFacts,
    *,
    system_packages: bool,
    assume_yes: bool,
) -> InstallPlan:
    """Decide which install commands run here and which the operator runs."""

    plan = InstallPlan(manager=manager)
    if not missing:
        return plan
    if manager is None:
        plan.unresolved = list(missing)
        return plan
    mapping = PACKAGES_BY_MANAGER[manager]
    installable = [key for key in missing if mapping.get(key)]
    plan.unresolved = [key for key in missing if not mapping.get(key)]
    plan.packages = system_packages_for(manager, installable)
    if not plan.packages:
        return plan

    if manager == "winget":
        user_scope = [p for p in plan.packages if p in WINGET_USER_SCOPE_PACKAGES]
        machine_scope = [
            p for p in plan.packages if p not in WINGET_USER_SCOPE_PACKAGES
        ]
        plan.commands = build_install_commands(
            manager, user_scope, False, silent=assume_yes
        )
        if machine_scope:
            plan.operator_commands = build_install_commands(
                manager, machine_scope, False
            )
            plan.operator_reason = (
                "These packages install machine-wide and need elevation, which "
                "this script never requests. Run them from an elevated terminal:"
            )
            plan.operator_prerequisites = [
                key
                for key in installable
                if set(mapping[key]) & set(machine_scope)
                and not set(mapping[key]) & set(user_scope)
            ]
        return plan

    if manager in ROOT_MANAGERS and not facts.is_root:
        sudo_available = facts.which("sudo") is not None
        if should_use_sudo(
            system_packages, is_root=False, sudo_available=sudo_available
        ):
            plan.commands = build_install_commands(manager, plan.packages, True)
            return plan
        plan.operator_commands = build_install_commands(
            manager, plan.packages, True, assume_yes=False
        )
        if not sudo_available:
            plan.operator_reason = (
                "System packages need root and sudo is not available. Run these "
                "as root:"
            )
        else:
            plan.operator_reason = (
                "System packages need root. Rerun with --system-packages to let "
                "this script use sudo, or run these yourself:"
            )
        plan.operator_prerequisites = list(installable)
        return plan

    plan.commands = build_install_commands(manager, plan.packages, False)
    return plan


def confirm_installation(
    plan: InstallPlan,
    *,
    assume_yes: bool,
    interactive: bool,
    ask: Callable[[str], str] = input,
) -> bool:
    """Apply the consent rules: --yes, one interactive prompt, else refuse."""

    if not plan.commands or assume_yes:
        return True
    if not interactive:
        print(
            "install_prerequisites: refusing to install without consent in a "
            "non-interactive run. Rerun with --yes to install, or with "
            "--dry-run to only print the commands.",
            file=sys.stderr,
        )
        return False
    try:
        answer = ask("Install the missing prerequisites with these commands? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    print("install_prerequisites: nothing installed.", file=sys.stderr)
    return False


def execute_commands(commands: Iterable[Sequence[str]], runner: CommandRunner) -> None:
    """Run each command in order, stopping at the first failure."""

    for command in commands:
        runner.run(list(command))


def venv_python_path(venv_path: Path, windows: Optional[bool] = None) -> Path:
    """Return the interpreter path inside a virtual environment."""

    if windows is None:
        windows = os.name == "nt"
    bin_dir = "Scripts" if windows else "bin"
    executable = "python.exe" if windows else "python"
    return venv_path.joinpath(bin_dir, executable)


def ensure_virtualenv(
    python_executable: str, venv_path: Path, runner: CommandRunner
) -> Path:
    """Create the virtual environment unless its interpreter already exists."""

    python_path = venv_python_path(venv_path)
    if python_path.exists():
        return python_path

    runner.run([python_executable, "-m", "venv", str(venv_path)])
    return python_path


def upgrade_pip_tooling(python_executable: str, runner: CommandRunner) -> None:
    """Upgrade pip, setuptools, and wheel inside the virtual environment."""

    runner.run(
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


def install_project(python_executable: str, dev: bool, runner: CommandRunner) -> None:
    """Install this checkout in editable mode, with dev extras when asked."""

    target = ".[dev]" if dev else "."
    runner.run([python_executable, "-m", "pip", "install", "-e", target], cwd=REPO_ROOT)


def install_git_hooks(python_executable: str, runner: CommandRunner) -> bool:
    """Run the project's git-hook installer when it has one; return True if run."""

    if not GIT_HOOK_INSTALLER.is_file():
        return False
    runner.run([python_executable, str(GIT_HOOK_INSTALLER)], cwd=REPO_ROOT)
    return True


def _running_interpreter_for_dev(running_version: Sequence[int], reason: str) -> str:
    """Return the running interpreter for `--dev`, or refuse one that is too old.

    The development extras pin releases that need Python 3.12, so a 3.9
    to 3.11 interpreter would only fail later inside pip's resolver.
    """

    if tuple(running_version[:2]) < DEV_MINIMUM_PYTHON:
        raise RuntimeError(
            "--dev needs Python {}.{}+ for the development tools, and no such "
            "interpreter was found ({}); this interpreter is {}. Install Python "
            "{}.{}+ or pass --python <path to it>.".format(
                *DEV_MINIMUM_PYTHON,
                reason,
                ".".join(str(part) for part in running_version[:3]),
                *DEV_MINIMUM_PYTHON,
            )
        )
    return sys.executable


def select_tool_python(
    python_override: Optional[str] = None,
    running_version: Optional[Sequence[int]] = None,
) -> str:
    """Choose the interpreter that creates the development virtual environment.

    Prefers the steady-state tool runtime from tool_validation_profiles.json
    when that helper is importable. An explicit `--python` that fails the
    policy is an error; otherwise the running interpreter is the fallback,
    but only when it meets the development floor (Python 3.12).
    ``running_version`` defaults to this interpreter's version.
    """

    version = tuple(
        sys.version_info[:3] if running_version is None else running_version
    )
    if resolve_runtime_policy_executable is None:
        if python_override:
            return python_override
        return _running_interpreter_for_dev(
            version, "the tool-validation profile helper is unavailable"
        )
    try:
        return resolve_runtime_policy_executable(
            REPO_ROOT,
            STEADY_STATE_RUNTIME_POLICY,
            explicit_candidate=python_override,
            required_modules=[],
        )
    except (RuntimeError, ValueError, OSError) as error:
        if python_override:
            raise RuntimeError(str(error)) from error
        selected = _running_interpreter_for_dev(version, str(error))
        print(
            "install_prerequisites: note: {} Using {} instead.".format(error, selected)
        )
        return selected


def validate_platform(os_info: OSInfo) -> None:
    """Raise RuntimeError when the platform has no supported install path."""

    if os_info.platform_id not in SUPPORTED_PLATFORMS:
        raise RuntimeError(
            "Unsupported platform: {platform}. Supported platforms include "
            "Rocky Linux 9.6, Fedora 43, Ubuntu 24.x, Linux Mint, Debian, and "
            "modern macOS releases, plus Windows 11 command-line shells.".format(
                platform=os_info.pretty_name
            )
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line options."""

    parser = argparse.ArgumentParser(
        description=(
            "Install the prerequisites this project needs (Python 3.9+, Git, "
            "and on Windows Git for Windows' bash) when they are missing."
        )
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help=(
            "Also create the repo-local virtual environment and install the "
            "project with its development extras."
        ),
    )
    parser.add_argument(
        "--venv",
        default=str(REPO_ROOT / ".venv"),
        help="Virtual environment path for --dev. Default: .venv in the checkout.",
    )
    parser.add_argument(
        "--python",
        default=None,
        help=(
            "Interpreter that creates the --dev virtual environment. Default: "
            "the steady-state tool runtime, else the running interpreter."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Install missing prerequisites without prompting.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without executing them."
    )
    parser.add_argument(
        "--system-packages",
        action="store_true",
        help=(
            "Allow sudo for apt-get, dnf, or yum when not running as root. "
            "Without it, commands that need root are printed instead."
        ),
    )
    parser.add_argument(
        "--no-git-bash",
        action="store_true",
        help="On Windows, do not require Git for Windows' bash.",
    )
    return parser.parse_args(argv)


def report_unresolved(
    prerequisites: Iterable[str], os_info: Optional[OSInfo] = None
) -> None:
    """Print a manual-install hint for each prerequisite left missing."""

    for key in prerequisites:
        print(
            "install_prerequisites: {} is still missing: {}.".format(
                PREREQUISITE_LABELS[key],
                (
                    manual_install_hint(key, os_info)
                    if os_info
                    else MANUAL_INSTALL_HINTS[key]
                ),
            ),
            file=sys.stderr,
        )


def run_dev_setup(args: argparse.Namespace, runner: CommandRunner) -> None:
    """Create `.venv` and install the project with development extras."""

    venv_path = Path(args.venv).resolve()
    tool_python = select_tool_python(args.python)
    print(f"Development interpreter: {tool_python}")
    venv_python = ensure_virtualenv(tool_python, venv_path, runner)
    upgrade_pip_tooling(str(venv_python), runner)
    install_project(str(venv_python), dev=True, runner=runner)
    install_git_hooks(str(venv_python), runner=runner)
    if getattr(runner, "dry_run", False):
        print(f"Dry run: nothing was created in {venv_path}")
    else:
        print(f"Environment ready in {venv_path}")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    facts: Optional[HostFacts] = None,
    runner: Optional[CommandRunner] = None,
    ask: Callable[[str], str] = input,
) -> int:
    """Install whatever is missing; return 0 when nothing remains missing.

    "Prerequisites ready." is printed only when nothing is missing. A dry
    run that would install something says so, changes nothing, and returns
    0 for the dry run itself. After a real install the prerequisites are
    detected again: winget changes only the registry PATH, so a tool this
    process still cannot see gets an "open a new terminal" note instead.
    """

    args = parse_args(argv)
    if runner is None or (args.dry_run and not getattr(runner, "dry_run", False)):
        # --dry-run must never reach a runner that executes commands.
        runner = CommandRunner(dry_run=args.dry_run)
    dry_run = args.dry_run or bool(getattr(runner, "dry_run", False))
    facts = facts if facts is not None else detect_host_facts()
    os_info = facts.os_info
    print(
        f"Detected platform: {os_info.pretty_name} "
        f"({os_info.platform_id} {os_info.version_id})".rstrip()
    )

    include_git_bash = is_windows(os_info) and not args.no_git_bash
    missing = detect_missing_prerequisites(
        facts, include_git_bash=include_git_bash, dev=args.dev
    )
    for line in describe_prerequisites(
        facts, missing, include_git_bash=include_git_bash, dev=args.dev
    ):
        print(line)

    still_missing: List[str] = []
    # Prerequisites this run installed (or, in a dry run, would install).
    planned: List[str] = []
    if missing:
        try:
            validate_platform(os_info)
        except RuntimeError as error:
            print(f"install_prerequisites: {error}", file=sys.stderr)
            report_unresolved(missing, os_info)
            return 1
        manager_names = (
            ("winget",) if is_windows(os_info) else ("apt-get", "dnf", "yum", "brew")
        )
        manager = select_package_manager(
            os_info, [name for name in manager_names if facts.which(name)]
        )
        if manager is None:
            print(
                "install_prerequisites: no supported package manager found "
                "({}).".format(", ".join(manager_names)),
                file=sys.stderr,
            )
            if is_windows(os_info):
                print(
                    "install_prerequisites: winget ships with App Installer from "
                    "the Microsoft Store.",
                    file=sys.stderr,
                )
        plan = plan_installation(
            missing,
            manager,
            facts,
            system_packages=args.system_packages,
            assume_yes=args.yes,
        )
        if plan.commands:
            if not dry_run:
                print("Commands to install the missing prerequisites:")
                for command in plan.commands:
                    print("  {}".format(format_command(command)))
                if not confirm_installation(
                    plan,
                    assume_yes=args.yes,
                    interactive=facts.interactive,
                    ask=ask,
                ):
                    return 1
            try:
                execute_commands(plan.commands, runner)
            except (subprocess.CalledProcessError, OSError) as error:
                print(f"install_prerequisites: FAIL: {error}", file=sys.stderr)
                return 1
        if plan.operator_commands:
            print(plan.operator_reason)
            for command in plan.operator_commands:
                print("  {}".format(format_command(command)))
        still_missing = plan.operator_prerequisites + plan.unresolved
        if plan.commands:
            planned = [key for key in missing if key not in still_missing]
        report_unresolved(plan.unresolved, os_info)

    if args.dev:
        if PYTHON in still_missing or PYTHON_VENV in still_missing or PYTHON in missing:
            print(
                "install_prerequisites: skipping --dev until Python 3.9+ with venv "
                "support is installed; rerun afterwards.",
                file=sys.stderr,
            )
            return 1
        try:
            run_dev_setup(args, runner)
        except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
            print(f"install_prerequisites: FAIL: {error}", file=sys.stderr)
            return 1

    if still_missing:
        return 1
    if planned and dry_run:
        # Nothing ran, so the host is not ready; exit 0 only means the dry
        # run itself succeeded.
        print(
            "Dry run: {} prerequisite(s) would be installed ({}); nothing was "
            "changed.".format(
                len(planned), ", ".join(PREREQUISITE_LABELS[key] for key in planned)
            )
        )
        return 0
    if planned:
        # The package manager succeeded, but this process keeps the PATH it
        # started with (winget updates the registry PATH only), so check
        # again before calling the host ready. venv support is skipped: the
        # facts hold a startup snapshot of it, and --dev's environment
        # setup, which needs it and has just succeeded, already proved it.
        remaining = [
            key
            for key in detect_missing_prerequisites(
                facts, include_git_bash=include_git_bash, dev=args.dev
            )
            if key != PYTHON_VENV
        ]
        if remaining:
            print(
                "Installed {}, but this process cannot see {} yet. Open a new "
                "terminal so PATH picks up the new tools, then rerun to "
                "confirm.".format(
                    ", ".join(PREREQUISITE_LABELS[key] for key in planned),
                    ", ".join(PREREQUISITE_LABELS[key] for key in remaining),
                )
            )
            return 0
    print("Prerequisites ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
