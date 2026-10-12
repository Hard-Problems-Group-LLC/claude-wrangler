"""Tests for scripts/install_prerequisites.py and its POSIX wrapper.

Every test injects host facts and a recording command runner, so no
package manager (apt-get, dnf, brew, winget) and no sudo ever runs. The
tests cover command construction for each package manager, missing-only
detection, the consent rules, the privilege rules, `--dry-run`, and the
`--dev` virtual-environment steps.
"""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "install_prerequisites.py"
WRAPPER_PATH = REPO_ROOT / "scripts" / "install_prerequisites.sh"

GIT_EXE = r"C:\Program Files\Git\cmd\git.exe"
GIT_BASH_EXE = r"C:\Program Files\Git\bin\bash.exe"


def load_prerequisites():
    """Import the script by path, registered like a real import."""

    name = "install_prerequisites_under_test"
    spec = importlib.util.spec_from_file_location(name, str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def prereq():
    """Return the install_prerequisites module."""

    return load_prerequisites()


class RecordingRunner:
    """CommandRunner stand-in that records commands instead of running them."""

    def __init__(self, dry_run: bool = False, fail: bool = False) -> None:
        """Configure dry-run reporting and optional failure."""

        self.dry_run = dry_run
        self.fail = fail
        self.commands: List[List[str]] = []
        self.cwds: List[Optional[Path]] = []

    def run(self, command: Sequence[str], cwd: Optional[Path] = None) -> None:
        """Record one command; raise like a failing command when asked."""

        self.commands.append(list(command))
        self.cwds.append(cwd)
        if self.fail:
            raise subprocess.CalledProcessError(1, list(command))


def fake_which(available: Dict[str, str]):
    """Return a `which` that only knows the given program paths."""

    def which(name: str) -> Optional[str]:
        return available.get(name)

    return which


def make_facts(
    prereq,
    *,
    platform_id: str = "ubuntu",
    python_version=(3, 12, 4),
    venv_available: bool = True,
    is_root: bool = False,
    interactive: bool = False,
    programs: Optional[Dict[str, str]] = None,
    files: Sequence[str] = (),
    env: Optional[Dict[str, str]] = None,
    succeeds=None,
):
    """Build injected host facts; `programs` maps names to PATH hits.

    ``succeeds`` answers command probes (such as `xcode-select -p`); by
    default every probe succeeds and nothing runs.
    """

    pretty = {"windows": "Windows", "macos": "macOS"}.get(platform_id, platform_id)
    existing = set(files)
    return prereq.HostFacts(
        os_info=prereq.OSInfo(platform_id, "1", pretty),
        python_version=tuple(python_version),
        venv_available=venv_available,
        is_root=is_root,
        interactive=interactive,
        env=env if env is not None else {},
        which=fake_which(programs or {}),
        is_file=lambda path: path in existing,
        python_executable="/usr/bin/python3",
        succeeds=succeeds if succeeds is not None else (lambda command: True),
    )


def windows_facts(prereq, *, git: bool = True, bash: bool = True, **overrides):
    """Build Windows host facts with winget and optional Git for Windows."""

    programs = {"winget": r"C:\Users\op\AppData\Local\Microsoft\WindowsApps\winget"}
    if git:
        programs["git"] = GIT_EXE
    files = [GIT_BASH_EXE] if bash else []
    overrides.setdefault("env", {"ProgramFiles": r"C:\Program Files"})
    return make_facts(
        prereq, platform_id="windows", programs=programs, files=files, **overrides
    )


def refuse_ask(prompt: str) -> str:
    """Fail the test if the script prompts when it must not."""

    raise AssertionError("unexpected prompt: {}".format(prompt))


# Command construction ---------------------------------------------------------


def test_apt_get_commands_with_and_without_sudo(prereq):
    assert prereq.build_install_commands("apt-get", ["git"], use_sudo=True) == [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "-y", "install", "git"],
    ]
    assert prereq.build_install_commands(
        "apt-get", ["git"], use_sudo=False, assume_yes=False
    ) == [["apt-get", "update"], ["apt-get", "install", "git"]]


@pytest.mark.parametrize("manager", ["dnf", "yum"])
def test_dnf_and_yum_commands(prereq, manager):
    assert prereq.build_install_commands(
        manager, ["python3", "git"], use_sudo=True
    ) == [["sudo", manager, "-y", "install", "python3", "git"]]


def test_brew_commands_never_use_sudo(prereq):
    assert prereq.build_install_commands("brew", ["git"], use_sudo=False) == [
        ["brew", "update"],
        ["brew", "install", "git"],
    ]


def test_winget_commands_use_user_scope_and_silent_only_when_asked(prereq):
    quiet = prereq.build_install_commands(
        "winget", ["Git.Git"], use_sudo=False, silent=True
    )
    prompted = prereq.build_install_commands("winget", ["Git.Git"], use_sudo=False)

    assert quiet == [
        [
            "winget",
            "install",
            "--id",
            "Git.Git",
            "--exact",
            "--source",
            "winget",
            "--scope",
            "user",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--silent",
        ]
    ]
    assert prompted == [quiet[0][:-1]]


def test_winget_command_omits_user_scope_for_machine_only_packages(prereq):
    command = prereq.winget_install_command(
        "Vendor.MachineOnly", user_scope=False, silent=False
    )

    assert "--scope" not in command
    assert command[:4] == ["winget", "install", "--id", "Vendor.MachineOnly"]


def test_winget_commands_are_never_elevated(prereq):
    with pytest.raises(ValueError):
        prereq.build_install_commands("winget", ["Git.Git"], use_sudo=True)


def test_unknown_package_manager_is_rejected(prereq):
    with pytest.raises(ValueError):
        prereq.build_install_commands("pacman", ["git"], use_sudo=False)


def test_system_packages_for_missing_prerequisites_only(prereq):
    assert prereq.system_packages_for("apt-get", ["git"]) == ["git"]
    assert prereq.system_packages_for("apt-get", ["python"]) == [
        "python3",
        "python3-venv",
        "python3-pip",
    ]
    assert prereq.system_packages_for("apt-get", ["python-venv"]) == ["python3-venv"]
    assert prereq.system_packages_for("winget", ["git", "git-bash"]) == ["Git.Git"]
    assert prereq.system_packages_for("winget") == [
        "Python.Python.3.12",
        "Git.Git",
    ]
    assert prereq.system_packages_for("unknown", ["git"]) == []


def test_format_command_quotes_for_the_host_shell(prereq):
    rendered = prereq.format_command(["winget", "install", "a b"])

    if os.name == "nt":
        assert rendered == 'winget install "a b"'
    else:
        assert rendered == "winget install 'a b'"


# Detection ------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform_id, available, expected",
    [
        ("windows", ["winget"], "winget"),
        ("windows", [], None),
        ("ubuntu", ["brew", "apt-get"], "apt-get"),
        ("fedora", ["yum", "dnf"], "dnf"),
        ("rocky", ["yum"], "yum"),
        ("macos", ["brew"], "brew"),
        ("debian", ["dnf"], "dnf"),
        ("ubuntu", [], None),
    ],
)
def test_select_package_manager(prereq, platform_id, available, expected):
    os_info = prereq.OSInfo(platform_id, "", platform_id)

    assert prereq.select_package_manager(os_info, available) == expected


def test_linux_os_info_maps_derivatives_through_id_like(prereq):
    pop = prereq.linux_os_info(
        prereq.parse_os_release('ID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="22.04"')
    )
    alma = prereq.linux_os_info({"ID": "almalinux", "ID_LIKE": "rhel centos fedora"})
    arch = prereq.linux_os_info({"ID": "arch"})

    assert (pop.platform_id, pop.version_id) == ("ubuntu", "22.04")
    assert alma.platform_id == "fedora"
    assert arch.platform_id == "arch"


def test_detection_reports_nothing_missing_on_a_complete_host(prereq):
    facts = make_facts(prereq, programs={"git": "/usr/bin/git"})

    assert (
        prereq.detect_missing_prerequisites(facts, include_git_bash=False, dev=True)
        == []
    )


def test_detection_finds_old_python_and_missing_git(prereq):
    facts = make_facts(prereq, python_version=(3, 8, 10))

    assert prereq.detect_missing_prerequisites(
        facts, include_git_bash=False, dev=True
    ) == ["python", "git"]


def test_detection_checks_venv_support_only_for_dev(prereq):
    facts = make_facts(prereq, venv_available=False, programs={"git": "/usr/bin/git"})

    assert (
        prereq.detect_missing_prerequisites(facts, include_git_bash=False, dev=False)
        == []
    )
    assert prereq.detect_missing_prerequisites(
        facts, include_git_bash=False, dev=True
    ) == ["python-venv"]


def test_detection_finds_git_bash_next_to_git_for_windows(prereq):
    facts = windows_facts(prereq, env={})

    assert prereq.find_git_bash(facts.env, facts.which, facts.is_file) == (GIT_BASH_EXE)
    assert (
        prereq.detect_missing_prerequisites(facts, include_git_bash=True, dev=False)
        == []
    )


def test_detection_derives_git_bash_from_mingw64_git(prereq):
    facts = make_facts(
        prereq,
        platform_id="windows",
        programs={"git": r"C:\Program Files\Git\mingw64\bin\git.exe"},
        files=[GIT_BASH_EXE],
    )

    assert prereq.find_git_bash(facts.env, facts.which, facts.is_file) == (GIT_BASH_EXE)


def test_detection_honors_claude_code_git_bash_override(prereq):
    custom = r"D:\tools\bash.exe"
    facts = make_facts(
        prereq,
        platform_id="windows",
        programs={"git": r"D:\tools\git.exe"},
        files=[custom],
        env={"CLAUDE_CODE_GIT_BASH_PATH": custom},
    )

    assert prereq.find_git_bash(facts.env, facts.which, facts.is_file) == custom


def test_detection_reports_missing_git_bash_unless_skipped(prereq):
    facts = windows_facts(prereq, bash=False)

    assert prereq.detect_missing_prerequisites(
        facts, include_git_bash=True, dev=False
    ) == ["git-bash"]
    assert (
        prereq.detect_missing_prerequisites(facts, include_git_bash=False, dev=False)
        == []
    )


def test_detection_ignores_git_bash_outside_windows(prereq):
    facts = make_facts(prereq, programs={"git": "/usr/bin/git"})

    assert (
        prereq.detect_missing_prerequisites(facts, include_git_bash=True, dev=False)
        == []
    )


def test_macos_command_line_tools_stub_is_not_git(prereq, capsys):
    # Without the Command Line Tools, /usr/bin/git exists but only opens the
    # installer dialog; `xcode-select -p` fails until the tools are present.
    probes: List[List[str]] = []

    def succeeds(command):
        probes.append(list(command))
        return False

    facts = make_facts(
        prereq,
        platform_id="macos",
        programs={"git": "/usr/bin/git"},
        succeeds=succeeds,
    )

    missing = prereq.detect_missing_prerequisites(
        facts, include_git_bash=False, dev=False
    )
    lines = prereq.describe_prerequisites(
        facts, missing, include_git_bash=False, dev=False
    )
    prereq.report_unresolved(missing, facts.os_info)

    assert missing == ["git"]
    assert ["xcode-select", "-p"] in probes
    assert any("Command Line Tools stub" in line for line in lines)
    assert "xcode-select --install" in capsys.readouterr().err


def test_macos_git_counts_once_the_command_line_tools_are_installed(prereq):
    for path in ("/usr/bin/git", "/opt/homebrew/bin/git"):
        facts = make_facts(
            prereq,
            platform_id="macos",
            programs={"git": path},
            succeeds=lambda command: command == ["xcode-select", "-p"],
        )
        assert (
            prereq.detect_missing_prerequisites(
                facts, include_git_bash=False, dev=False
            )
            == []
        )
    # A git outside /usr/bin is never a stub, even without the tools.
    facts = make_facts(
        prereq,
        platform_id="macos",
        programs={"git": "/opt/homebrew/bin/git"},
        succeeds=lambda command: False,
    )
    assert prereq.find_git(facts) == "/opt/homebrew/bin/git"


# Privilege and planning -------------------------------------------------------


@pytest.mark.parametrize(
    "system_packages, is_root, sudo_available, expected",
    [
        (False, False, True, False),
        (True, False, True, True),
        (True, False, False, False),
        (True, True, True, False),
        (False, True, True, False),
    ],
)
def test_should_use_sudo_requires_explicit_permission(
    prereq, system_packages, is_root, sudo_available, expected
):
    assert (
        prereq.should_use_sudo(
            system_packages, is_root=is_root, sudo_available=sudo_available
        )
        is expected
    )


def test_plan_prints_root_commands_without_system_packages(prereq):
    facts = make_facts(prereq, programs={"sudo": "/usr/bin/sudo"})

    plan = prereq.plan_installation(
        ["git"], "apt-get", facts, system_packages=False, assume_yes=True
    )

    assert plan.commands == []
    assert plan.operator_commands == [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "git"],
    ]
    assert "--system-packages" in plan.operator_reason
    assert plan.operator_prerequisites == ["git"]


def test_plan_uses_sudo_only_with_system_packages(prereq):
    facts = make_facts(prereq, programs={"sudo": "/usr/bin/sudo"})

    plan = prereq.plan_installation(
        ["git"], "dnf", facts, system_packages=True, assume_yes=False
    )

    assert plan.commands == [["sudo", "dnf", "-y", "install", "git"]]
    assert plan.operator_commands == []


def test_plan_without_sudo_asks_for_root(prereq):
    facts = make_facts(prereq)

    plan = prereq.plan_installation(
        ["git"], "apt-get", facts, system_packages=True, assume_yes=True
    )

    assert plan.commands == []
    assert "sudo is not available" in plan.operator_reason


def test_plan_runs_directly_as_root(prereq):
    facts = make_facts(prereq, is_root=True)

    plan = prereq.plan_installation(
        ["git"], "apt-get", facts, system_packages=False, assume_yes=True
    )

    assert plan.commands == [
        ["apt-get", "update"],
        ["apt-get", "-y", "install", "git"],
    ]


def test_plan_runs_brew_as_the_user(prereq):
    facts = make_facts(prereq, platform_id="macos")

    plan = prereq.plan_installation(
        ["python", "git"], "brew", facts, system_packages=False, assume_yes=False
    )

    assert plan.commands == [["brew", "update"], ["brew", "install", "python", "git"]]


def test_plan_winget_dedupes_git_and_never_elevates(prereq):
    facts = windows_facts(prereq, git=False, bash=False)

    plan = prereq.plan_installation(
        ["git", "git-bash"], "winget", facts, system_packages=False, assume_yes=False
    )

    assert len(plan.commands) == 1
    assert plan.commands[0][:4] == ["winget", "install", "--id", "Git.Git"]
    assert "--scope" in plan.commands[0]
    assert "--silent" not in plan.commands[0]
    assert all(command[0] != "sudo" for command in plan.commands)


def test_plan_prints_machine_scope_winget_packages_for_the_operator(
    prereq, monkeypatch
):
    monkeypatch.setattr(prereq, "WINGET_USER_SCOPE_PACKAGES", frozenset())
    facts = windows_facts(prereq, git=False, bash=False)

    plan = prereq.plan_installation(
        ["git"], "winget", facts, system_packages=True, assume_yes=True
    )

    assert plan.commands == []
    assert len(plan.operator_commands) == 1
    assert "--scope" not in plan.operator_commands[0]
    assert "elevated terminal" in plan.operator_reason
    assert plan.operator_prerequisites == ["git"]


def test_plan_without_manager_leaves_everything_unresolved(prereq):
    plan = prereq.plan_installation(
        ["git"], None, make_facts(prereq), system_packages=True, assume_yes=True
    )

    assert plan.commands == []
    assert plan.unresolved == ["git"]


def test_plan_reports_prerequisites_a_manager_cannot_provide(prereq):
    plan = prereq.plan_installation(
        ["python-venv", "git"],
        "dnf",
        make_facts(prereq, is_root=True),
        system_packages=False,
        assume_yes=True,
    )

    assert plan.unresolved == ["python-venv"]
    assert plan.packages == ["git"]


# Consent ----------------------------------------------------------------------


def _plan_with_commands(prereq):
    """Return a plan with one command to run."""

    return prereq.InstallPlan(manager="brew", commands=[["brew", "install", "git"]])


def test_consent_yes_skips_the_prompt(prereq):
    assert prereq.confirm_installation(
        _plan_with_commands(prereq), assume_yes=True, interactive=False, ask=refuse_ask
    )


def test_consent_refused_without_console(prereq, capsys):
    assert not prereq.confirm_installation(
        _plan_with_commands(prereq), assume_yes=False, interactive=False, ask=refuse_ask
    )
    assert "--yes" in capsys.readouterr().err


@pytest.mark.parametrize("answer, expected", [("y", True), ("Yes", True), ("", False)])
def test_consent_interactive_asks_once(prereq, answer, expected):
    prompts: List[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    assert (
        prereq.confirm_installation(
            _plan_with_commands(prereq), assume_yes=False, interactive=True, ask=ask
        )
        is expected
    )
    assert len(prompts) == 1


def test_consent_end_of_input_refuses(prereq):
    def end_of_input(prompt: str) -> str:
        raise EOFError

    assert not prereq.confirm_installation(
        _plan_with_commands(prereq),
        assume_yes=False,
        interactive=True,
        ask=end_of_input,
    )


def test_consent_not_needed_when_nothing_runs(prereq):
    assert prereq.confirm_installation(
        prereq.InstallPlan(manager=None),
        assume_yes=False,
        interactive=False,
        ask=refuse_ask,
    )


# main() ------------------------------------------------------------------------


def test_main_does_nothing_when_nothing_is_missing(prereq, capsys):
    runner = RecordingRunner()
    facts = make_facts(prereq, programs={"git": "/usr/bin/git"})

    assert prereq.main([], facts=facts, runner=runner, ask=refuse_ask) == 0
    assert runner.commands == []
    assert "Prerequisites ready." in capsys.readouterr().out


def test_main_prints_sudo_commands_without_running_them(prereq, capsys):
    runner = RecordingRunner()
    facts = make_facts(
        prereq, programs={"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"}
    )

    assert prereq.main([], facts=facts, runner=runner, ask=refuse_ask) == 1
    assert runner.commands == []
    output = capsys.readouterr().out
    assert "sudo apt-get install git" in output


def test_main_installs_with_sudo_when_allowed(prereq):
    runner = RecordingRunner()
    facts = make_facts(
        prereq, programs={"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"}
    )

    result = prereq.main(
        ["--system-packages", "--yes"], facts=facts, runner=runner, ask=refuse_ask
    )

    assert result == 0
    assert runner.commands == [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "-y", "install", "git"],
    ]


def test_main_refuses_without_consent_when_not_interactive(prereq):
    runner = RecordingRunner()
    facts = windows_facts(prereq, git=False, bash=False)

    assert prereq.main([], facts=facts, runner=runner, ask=refuse_ask) == 1
    assert runner.commands == []


def test_main_windows_yes_installs_git_for_windows_silently(prereq):
    runner = RecordingRunner()
    facts = windows_facts(prereq, git=False, bash=False)

    assert prereq.main(["--yes"], facts=facts, runner=runner, ask=refuse_ask) == 0
    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert command[:4] == ["winget", "install", "--id", "Git.Git"]
    assert command[command.index("--scope") + 1] == "user"
    assert "--silent" in command


def test_main_windows_interactive_consent_runs_without_silent(prereq):
    runner = RecordingRunner()
    facts = windows_facts(prereq, git=True, bash=False, interactive=True)
    prompts: List[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    assert prereq.main([], facts=facts, runner=runner, ask=ask) == 0
    assert len(prompts) == 1
    assert len(runner.commands) == 1
    assert "--silent" not in runner.commands[0]


def test_main_no_git_bash_skips_the_bash_requirement(prereq):
    runner = RecordingRunner()
    facts = windows_facts(prereq, git=True, bash=False)

    assert prereq.main(["--no-git-bash"], facts=facts, runner=runner) == 0
    assert runner.commands == []


def test_main_dry_run_prints_without_prompting_or_running(prereq, capsys, monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("dry run must not run commands")

    monkeypatch.setattr(prereq.subprocess, "run", no_subprocess)
    facts = windows_facts(prereq, git=False, bash=False)

    result = prereq.main(["--dry-run"], facts=facts, ask=refuse_ask)

    assert result == 0
    output = capsys.readouterr().out
    assert "-> " in output
    assert "Git.Git" in output
    assert "--silent" not in output


def test_main_reports_failed_install(prereq, capsys):
    runner = RecordingRunner(fail=True)
    facts = windows_facts(prereq, git=False, bash=False)

    assert prereq.main(["--yes"], facts=facts, runner=runner) == 1
    assert "FAIL" in capsys.readouterr().err


def test_main_without_package_manager_gives_manual_hints(prereq, capsys):
    facts = windows_facts(prereq, git=False, bash=False)
    facts.which = fake_which({})

    assert prereq.main(["--yes"], facts=facts, runner=RecordingRunner()) == 1
    error = capsys.readouterr().err
    assert "no supported package manager" in error
    assert "https://git-scm.com/download/win" in error


def test_main_rejects_unsupported_platform_only_when_installs_are_needed(
    prereq, capsys
):
    complete = make_facts(prereq, platform_id="arch", programs={"git": "/usr/bin/git"})
    incomplete = make_facts(prereq, platform_id="arch")

    assert prereq.main([], facts=complete, runner=RecordingRunner()) == 0
    assert prereq.main([], facts=incomplete, runner=RecordingRunner()) == 1
    assert "Unsupported platform" in capsys.readouterr().err


@pytest.mark.parametrize("windows, layout", [(True, "Scripts"), (False, "bin")])
def test_venv_python_path_follows_platform(prereq, tmp_path, windows, layout):
    executable = "python.exe" if windows else "python"

    assert prereq.venv_python_path(tmp_path, windows=windows) == (
        tmp_path / layout / executable
    )


def test_main_dev_creates_venv_and_installs_dev_extras(prereq, tmp_path, monkeypatch):
    monkeypatch.setattr(prereq, "select_tool_python", lambda override: "py312")
    monkeypatch.setattr(prereq, "GIT_HOOK_INSTALLER", tmp_path / "missing.py")
    runner = RecordingRunner()
    venv = tmp_path / "dev venv"
    facts = make_facts(prereq, programs={"git": "/usr/bin/git"})

    result = prereq.main(
        ["--dev", "--venv", str(venv)], facts=facts, runner=runner, ask=refuse_ask
    )

    assert result == 0
    venv_python = str(prereq.venv_python_path(venv.resolve()))
    assert runner.commands == [
        ["py312", "-m", "venv", str(venv.resolve())],
        [
            venv_python,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel",
        ],
        [venv_python, "-m", "pip", "install", "-e", ".[dev]"],
    ]
    assert runner.cwds[-1] == prereq.REPO_ROOT


def test_main_dev_runs_git_hook_installer_when_present(prereq, tmp_path, monkeypatch):
    hook = tmp_path / "install_git_hooks.py"
    hook.write_text("", encoding="utf-8")
    monkeypatch.setattr(prereq, "select_tool_python", lambda override: "py312")
    monkeypatch.setattr(prereq, "GIT_HOOK_INSTALLER", hook)
    runner = RecordingRunner()
    facts = make_facts(prereq, programs={"git": "/usr/bin/git"})

    assert (
        prereq.main(
            ["--dev", "--venv", str(tmp_path / "v")], facts=facts, runner=runner
        )
        == 0
    )
    assert runner.commands[-1][1:] == [str(hook)]


def test_main_dev_waits_for_a_supported_python(prereq, monkeypatch, capsys):
    def no_dev_setup(*args, **kwargs):
        raise AssertionError("--dev must wait for Python 3.9+")

    monkeypatch.setattr(prereq, "run_dev_setup", no_dev_setup)
    facts = make_facts(
        prereq, python_version=(3, 8, 18), programs={"git": "/usr/bin/git"}
    )

    assert prereq.main(["--dev"], facts=facts, runner=RecordingRunner()) == 1
    assert "skipping --dev" in capsys.readouterr().err


def test_select_tool_python_falls_back_to_running_interpreter(prereq, monkeypatch):
    def no_runtime(*args, **kwargs):
        raise RuntimeError("No Python runtime matched policy")

    monkeypatch.setattr(prereq, "resolve_runtime_policy_executable", no_runtime)

    assert prereq.select_tool_python(None, running_version=(3, 12, 4)) == (
        sys.executable
    )
    with pytest.raises(RuntimeError):
        prereq.select_tool_python("/nonexistent/python")


def test_select_tool_python_without_profiles_helper(prereq, monkeypatch):
    monkeypatch.setattr(prereq, "resolve_runtime_policy_executable", None)

    assert prereq.select_tool_python(None, running_version=(3, 12, 4)) == (
        sys.executable
    )
    assert prereq.select_tool_python("custom-python") == "custom-python"


@pytest.mark.parametrize("helper", ["missing", "no-match"])
def test_select_tool_python_refuses_a_running_interpreter_below_the_dev_floor(
    prereq, monkeypatch, helper
):
    # The [dev] extras pin pytest 9 and black 26, which need Python 3.12; a
    # 3.9 fallback would only fail later inside pip's resolver.
    def no_runtime(*args, **kwargs):
        raise RuntimeError("No Python runtime matched policy")

    monkeypatch.setattr(
        prereq,
        "resolve_runtime_policy_executable",
        None if helper == "missing" else no_runtime,
    )

    with pytest.raises(RuntimeError, match=r"--dev needs Python 3\.12\+"):
        prereq.select_tool_python(None, running_version=(3, 9, 13))


def test_main_dev_refuses_before_creating_a_venv_on_a_too_old_interpreter(
    prereq, monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(prereq, "resolve_runtime_policy_executable", None)
    original = prereq.select_tool_python
    monkeypatch.setattr(
        prereq,
        "select_tool_python",
        lambda override: original(override, running_version=(3, 9, 13)),
    )
    runner = RecordingRunner()
    facts = make_facts(
        prereq, python_version=(3, 9, 13), programs={"git": "/usr/bin/git"}
    )

    code = prereq.main(
        ["--dev", "--venv", str(tmp_path / "venv")], facts=facts, runner=runner
    )

    assert code == 1
    assert runner.commands == []
    assert "--dev needs Python 3.12+" in capsys.readouterr().err


def test_command_runner_dry_run_only_prints(prereq, capsys, monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("dry run must not run commands")

    monkeypatch.setattr(prereq.subprocess, "run", no_subprocess)

    prereq.CommandRunner(dry_run=True).run(["winget", "install", "Git.Git"])

    assert "-> winget install Git.Git" in capsys.readouterr().out


# POSIX wrapper ---------------------------------------------------------------


def posix_bash() -> Optional[str]:
    """Return a bash that runs this checkout's scripts natively.

    On Windows only Git for Windows' bash qualifies; the System32 bash
    would start WSL instead.
    """

    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git is None:
        return None
    git_dir = Path(git).resolve().parent
    for root in (git_dir.parent, git_dir.parent.parent):
        candidate = root / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


@pytest.fixture
def wrapper_bash():
    """Return bash for the wrapper tests, skipping when unusable."""

    bash = posix_bash()
    if bash is None:
        pytest.skip("no suitable bash")
    if b"\r" in WRAPPER_PATH.read_bytes():
        pytest.skip("wrapper has CRLF line endings in this checkout")
    return bash


def test_wrapper_delegates_to_the_python_script(wrapper_bash):
    env = dict(os.environ, THEKNOWLEDGE_BOOTSTRAP_PYTHON=sys.executable)

    completed = subprocess.run(
        [wrapper_bash, str(WRAPPER_PATH), "--help"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--dev" in completed.stdout
    assert "--system-packages" in completed.stdout


def test_wrapper_does_not_fall_back_from_a_bad_override(wrapper_bash, tmp_path):
    env = dict(
        os.environ, THEKNOWLEDGE_BOOTSTRAP_PYTHON=str(tmp_path / "missing-python")
    )

    completed = subprocess.run(
        [wrapper_bash, str(WRAPPER_PATH), "--help"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1
    assert "not falling back" in completed.stderr


def test_wrapper_ignores_an_exported_cdpath(wrapper_bash):
    # Run as `bash scripts/install_prerequisites.sh` from the repository
    # root, `cd scripts` would consult CDPATH, print the directory, and
    # double the captured script_dir; `CDPATH= cd --` prevents both.
    env = dict(os.environ, THEKNOWLEDGE_BOOTSTRAP_PYTHON=sys.executable, CDPATH=".")

    completed = subprocess.run(
        [wrapper_bash, "scripts/install_prerequisites.sh", "--help"],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--dev" in completed.stdout
