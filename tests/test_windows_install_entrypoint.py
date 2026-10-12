"""Tests for the native-Windows entry points: install.cmd and its policy helper.

Three layers:

- Policy tests run on every OS. They load
  `scripts/install-stage-1-windows.py` by path and drive it with injected
  host facts, using the real stage-2 parser so option abbreviations behave
  exactly as stage 2 reads them.
- Static tests read `install.cmd` and `scripts/install_prerequisites.cmd`
  as bytes and check the batch-file rules: ASCII only, CRLF, no labels or
  GOTO, no output capture, the argument list only on the final top-level
  line, and the CMD_EXIT exit pattern.
- Windows-only tests copy the entry points into a fake checkout whose path
  has spaces and non-ASCII characters, next to a fake stage 2 that records
  how it was started, and run them through cmd, PowerShell 7, Windows
  PowerShell 5.1, and Git Bash. These never install anything: every home
  variable points into the test's temporary directory, and the fake
  stage 2 and fake winget only write JSON or text records.
"""

import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE1_PATH = REPO_ROOT / "scripts" / "install-stage-1-windows.py"
STAGE2_PATH = REPO_ROOT / "scripts" / "install-stage-2.py"
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_CMD = REPO_ROOT / "install.cmd"
PREREQ_CMD = REPO_ROOT / "scripts" / "install_prerequisites.cmd"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"
BATCH_FILES = (INSTALL_CMD, PREREQ_CMD)

# Help text that install.sh, the Windows helper, and install.cmd's
# no-Python help all promise.
HELP_CONTRACT = (
    "Install or bootstrap from this checkout.",
    "normal user: user-local non-development install",
    "All remaining options are passed through",
)

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="runs Windows shells")
# Variables that would steer interpreter discovery or the policy gate.
SCRUBBED_VARIABLES = (
    "THEKNOWLEDGE_BOOTSTRAP_PYTHON",
    "THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED",
    "THEKNOWLEDGE_MANAGED_INSTALL_STAGE1",
    "CLAUDE_LOCAL_MANAGED_BY",
    "CODEX_LOCAL_MANAGED_BY",
    "PYENV",
    "PYENV_ROOT",
    "PYENV_HOME",
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "HOME",
    "CMD_EXIT",
)


def load_stage1():
    """Import the stage-1 helper by path, registered like a real import."""

    name = "install_stage_1_windows_under_test"
    spec = importlib.util.spec_from_file_location(name, str(STAGE1_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stage1():
    """Return the stage-1 helper module."""

    return load_stage1()


@pytest.fixture(scope="module")
def stage2(stage1):
    """Return the real stage-2 module, loaded the way the helper loads it."""

    return stage1.load_stage2(STAGE2_PATH)


def make_host(
    stage1,
    *,
    env: Optional[Dict[str, str]] = None,
    elevated: bool = False,
    interactive: bool = False,
    windows: bool = True,
    profile: str = r"C:\Users\Operator",
):
    """Build injected host facts for a Windows operator profile."""

    host_env = {"USERPROFILE": profile}
    host_env.update(env or {})
    return stage1.Host(
        windows=windows,
        elevated=elevated,
        interactive=interactive,
        env=host_env,
        profile=Path(profile),
    )


class RecordingRun:
    """Stand-in for run_stage2 that records the forwarded arguments."""

    def __init__(self, code: int = 0) -> None:
        """Remember the exit code to report."""

        self.code = code
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str], stage2: Path) -> int:
        """Record one stage-2 run and return the configured code."""

        self.calls.append(list(argv))
        return self.code


def refuse_ask(prompt: str) -> str:
    """Fail the test if the helper prompts when it must not."""

    raise AssertionError("unexpected prompt: {}".format(prompt))


# Policy, any OS ------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--system"], ["--sys"], ["--sy"]])
def test_policy_refuses_system_in_any_spelling(stage1, stage2, argv):
    refusal = stage1.policy_refusal(
        stage2.parse_args(argv),
        stage1.supported_native_modes(stage2),
        make_host(stage1),
    )

    assert refusal is not None
    assert "--system is not supported on native Windows" in refusal
    assert "install_prerequisites.cmd --dev" in refusal
    assert "WSL2" in refusal


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "dev"],
        ["--mode=venv-only"],
        ["--mo=venv-only"],
        ["--m", "dev"],
    ],
)
def test_policy_refuses_posix_only_modes(stage1, stage2, argv):
    refusal = stage1.policy_refusal(
        stage2.parse_args(argv),
        stage1.supported_native_modes(stage2),
        make_host(stage1),
    )

    assert refusal is not None
    assert "is not supported on native Windows" in refusal
    assert "install_prerequisites.cmd --dev" in refusal
    assert "WSL2" in refusal


@pytest.mark.parametrize(
    "argv", [[], ["--mode", "standard"], ["--skip-submodule-init"]]
)
def test_policy_allows_standard_mode(stage1, stage2, argv):
    assert (
        stage1.policy_refusal(
            stage2.parse_args(argv),
            stage1.supported_native_modes(stage2),
            make_host(stage1),
        )
        is None
    )


@pytest.mark.parametrize(
    "variable, value",
    [
        ("HOME", r"C:\work\repo\.local\claude\HOME"),
        ("USERPROFILE", r"C:\work\repo\.local\claude\HOME"),
        ("USERPROFILE", r"C:\work\repo\.LOCAL\Claude\home\\"),
        ("USERPROFILE", r"C:\work\repo\.codex-home"),
        ("HOME", r"C:\work\repo\.local\codex-home"),
        ("HOME", "/c/work/repo/.local/claude/HOME"),
    ],
)
def test_policy_refuses_isolated_homes(stage1, stage2, variable, value):
    host = make_host(stage1, env={variable: value})
    args = stage2.parse_args([])
    modes = stage1.supported_native_modes(stage2)

    refusal = stage1.policy_refusal(args, modes, host)

    assert refusal is not None
    assert "{}={}".format(variable, value) in refusal
    assert "--allow-isolated-home" in refusal
    assert stage1.policy_refusal(args, modes, host, allow_isolated_home=True) is None


def test_policy_refuses_redirected_launcher_session(stage1, stage2):
    host = make_host(
        stage1,
        env={
            "CLAUDE_LOCAL_MANAGED_BY": "claude-local",
            "USERPROFILE": r"C:\work\repo\.local\custom-home",
            "HOME": r"C:\work\repo\.local\custom-home",
        },
    )

    refusal = stage1.policy_refusal(
        stage2.parse_args([]), stage1.supported_native_modes(stage2), host
    )

    assert refusal is not None
    assert "CLAUDE_LOCAL_MANAGED_BY" in refusal
    assert "differs from the Windows profile" in refusal


def test_policy_allows_ring0_launcher_session_with_real_profile(stage1, stage2):
    host = make_host(
        stage1,
        env={
            "CODEX_LOCAL_MANAGED_BY": "codex-local",
            "USERPROFILE": r"c:\users\OPERATOR\\",
            "HOME": "/c/Users/Operator",
        },
    )

    assert (
        stage1.policy_refusal(
            stage2.parse_args([]), stage1.supported_native_modes(stage2), host
        )
        is None
    )


def test_policy_allows_deliberate_home_outside_a_launcher_session(stage1, stage2):
    host = make_host(stage1, env={"HOME": r"D:\home\operator"})

    assert (
        stage1.policy_refusal(
            stage2.parse_args([]), stage1.supported_native_modes(stage2), host
        )
        is None
    )


def bare_host(stage1, env: Dict[str, str], profile: str = r"C:\Users\Operator"):
    """Build host facts whose environment is exactly ``env`` (no USERPROFILE)."""

    return stage1.Host(
        windows=True,
        elevated=False,
        interactive=False,
        env=dict(env),
        profile=Path(profile),
    )


@pytest.mark.parametrize(
    "homepath",
    [r"\work\repo\.local\claude\HOME", r"\work\repo\.codex-home"],
)
def test_policy_refuses_an_isolated_homedrive_homepath(stage1, stage2, homepath):
    # With USERPROFILE unset, stage 2's Path.home() uses HOMEDRIVE/HOMEPATH.
    host = bare_host(stage1, {"HOMEDRIVE": "C:", "HOMEPATH": homepath})

    refusal = stage1.policy_refusal(
        stage2.parse_args([]), stage1.supported_native_modes(stage2), host
    )

    assert refusal is not None
    assert "HOMEDRIVE/HOMEPATH=C:" + homepath in refusal
    assert stage1.install_target_home(host) == "C:" + homepath


def test_policy_refuses_a_redirected_homedrive_homepath_in_a_session(stage1, stage2):
    host = bare_host(
        stage1,
        {
            "HOMEDRIVE": "D:",
            "HOMEPATH": r"\custom\home",
            "CLAUDE_LOCAL_MANAGED_BY": "claude-wrangler",
        },
    )

    refusal = stage1.policy_refusal(
        stage2.parse_args([]), stage1.supported_native_modes(stage2), host
    )

    assert refusal is not None
    assert "HOMEDRIVE/HOMEPATH" in refusal


def test_policy_ignores_a_network_homedrive_when_userprofile_is_set(stage1, stage2):
    host = make_host(
        stage1,
        env={
            "HOMEDRIVE": "H:",
            "HOMEPATH": "\\",
            "CLAUDE_LOCAL_MANAGED_BY": "claude-wrangler",
        },
    )

    assert (
        stage1.policy_refusal(
            stage2.parse_args([]), stage1.supported_native_modes(stage2), host
        )
        is None
    )
    assert stage1.install_target_home(host) == r"C:\Users\Operator"


@pytest.mark.parametrize("allow", [False, True])
def test_policy_refuses_an_empty_userprofile(stage1, stage2, allow):
    # Path.home() would turn an empty USERPROFILE into the current directory.
    host = bare_host(stage1, {"USERPROFILE": "", "HOME": r"C:\Users\Operator"})

    refusal = stage1.policy_refusal(
        stage2.parse_args([]),
        stage1.supported_native_modes(stage2),
        host,
        allow_isolated_home=allow,
    )

    assert refusal is not None
    assert "USERPROFILE is set but empty" in refusal


def test_supported_modes_follow_stage2_declaration(stage1):
    class Declared:
        """A stage 2 that declares two native modes."""

        NATIVE_WINDOWS_SUPPORTED_MODES = ("standard", "dev")

    class Malformed:
        """A stage 2 whose declaration is not a sequence of names."""

        NATIVE_WINDOWS_SUPPORTED_MODES = "standard"

    assert stage1.supported_native_modes(Declared) == ("standard", "dev")
    assert stage1.supported_native_modes(Malformed) == ("standard",)


def test_main_forwards_arguments_and_consumes_allow_flag(stage1):
    run = RecordingRun(code=7)
    host = make_host(stage1, env={"HOME": r"C:\work\repo\.local\claude\HOME"})

    result = stage1.main(
        ["--skip-submodule-init", "--allow-isolated-home", "--mode", "standard"],
        host=host,
        stage2=STAGE2_PATH,
        run=run,
        ask=refuse_ask,
    )

    assert result == 7
    assert run.calls == [["--skip-submodule-init", "--mode", "standard"]]


@pytest.mark.parametrize(
    "argv", [["--mode", "dev"], ["--sys"], ["--allow-isolated-home", "--system"]]
)
def test_main_refusals_never_start_stage2(stage1, argv, capsys):
    run = RecordingRun()

    result = stage1.main(
        argv, host=make_host(stage1), stage2=STAGE2_PATH, run=run, ask=refuse_ask
    )

    assert result == 1
    assert run.calls == []
    assert "not supported on native Windows" in capsys.readouterr().err


def test_main_keeps_stage2_usage_errors(stage1, capsys):
    run = RecordingRun()

    result = stage1.main(
        ["--bogus"], host=make_host(stage1), stage2=STAGE2_PATH, run=run
    )

    assert result == 2
    assert run.calls == []
    assert "install-stage-2.py" in capsys.readouterr().err


def test_main_refuses_outside_windows(stage1, capsys):
    run = RecordingRun()

    result = stage1.main(
        [], host=make_host(stage1, windows=False), stage2=STAGE2_PATH, run=run
    )

    assert result == 1
    assert run.calls == []
    assert "use ./install.sh" in capsys.readouterr().err


@pytest.mark.parametrize("token", ["--help", "-h", "/?"])
def test_main_help_prints_usage_then_stage2_help(stage1, token, capsys):
    run = RecordingRun()
    shown: List[Path] = []

    def show_help(stage2_path: Path) -> int:
        """Record which stage 2 the helper asked for help."""

        shown.append(stage2_path)
        return 0

    result = stage1.main(
        ["--mode", "dev", token],
        host=make_host(stage1),
        stage2=STAGE2_PATH,
        run=run,
        show_help=show_help,
    )

    assert result == 0
    assert run.calls == []
    assert shown == [STAGE2_PATH]
    output = capsys.readouterr().out
    for text in HELP_CONTRACT:
        assert text in output
    assert "Stage-2 options:" in output


def test_main_help_runs_real_stage2_help_without_marker(stage1, capfd, monkeypatch):
    monkeypatch.delenv(stage1.MARKER, raising=False)

    result = stage1.main(["--help"], host=make_host(stage1), stage2=STAGE2_PATH)

    assert result == 0
    output = capfd.readouterr().out
    assert "--mode {standard,dev,venv-only}" in output
    assert "--system" in output


def test_help_contract_is_shared_by_every_entry_point(stage1):
    install_sh = INSTALL_SH.read_text(encoding="utf-8")
    install_cmd = INSTALL_CMD.read_text(encoding="ascii")

    for text in HELP_CONTRACT:
        assert text in install_sh
        assert text in stage1.USAGE
        assert text in install_cmd


@pytest.mark.parametrize(
    "source, message",
    [
        (None, "does not exist"),
        ("raise RuntimeError('broken stage 2')\n", "broken stage 2"),
        ("import sys\nsys.exit(0)\n", "SystemExit"),
        ("VALUE = 1\n", "has no parse_args"),
    ],
)
def test_main_fails_closed_when_stage2_cannot_be_evaluated(
    stage1, tmp_path, capsys, source, message
):
    stage2_path = tmp_path / "install-stage-2.py"
    if source is not None:
        stage2_path.write_text(source, encoding="utf-8")
    run = RecordingRun()

    result = stage1.main([], host=make_host(stage1), stage2=stage2_path, run=run)

    assert result == 1
    assert run.calls == []
    error = capsys.readouterr().err
    assert "refusing" in error
    assert message in error


def test_load_stage2_registers_module_before_executing(stage1, tmp_path):
    stage2_path = tmp_path / "install-stage-2.py"
    stage2_path.write_text(
        "from __future__ import annotations\n"
        "import argparse\n"
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass\n"
        "class Options:\n"
        "    mode: str = 'standard'\n"
        "\n"
        "def parse_args(argv):\n"
        "    return argparse.Namespace(mode='standard', system=False)\n",
        encoding="utf-8",
    )

    module = stage1.load_stage2(stage2_path)

    assert sys.modules[stage1.STAGE2_MODULE_NAME] is module
    assert module.Options().mode == "standard"


def test_stage2_interface_tripwires(stage1, stage2):
    assert callable(stage2.parse_args)
    assert stage2.STAGE1_MARKER == stage1.MARKER


def test_elevated_run_without_console_is_refused(stage1, capsys):
    run = RecordingRun()
    host = make_host(stage1, elevated=True, interactive=False)

    result = stage1.main([], host=host, stage2=STAGE2_PATH, run=run, ask=refuse_ask)

    assert result == 1
    assert run.calls == []
    assert stage1.ALLOW_ELEVATED_ENV in capsys.readouterr().err


@pytest.mark.parametrize("answer, expected", [("y", 0), ("YES", 0), ("n", 1), ("", 1)])
def test_elevated_run_with_console_asks_once(stage1, answer, expected):
    prompts: List[str] = []

    def ask(prompt: str) -> str:
        """Record the prompt and give the parametrized answer."""

        prompts.append(prompt)
        return answer

    run = RecordingRun()
    host = make_host(stage1, elevated=True, interactive=True)

    result = stage1.main([], host=host, stage2=STAGE2_PATH, run=run, ask=ask)

    assert result == expected
    assert len(prompts) == 1
    assert r"C:\Users\Operator" in prompts[0]
    assert len(run.calls) == (1 if expected == 0 else 0)


def test_elevated_run_override_skips_prompt(stage1):
    run = RecordingRun()
    host = make_host(
        stage1,
        elevated=True,
        interactive=False,
        env={stage1.ALLOW_ELEVATED_ENV: "1"},
    )

    assert stage1.main([], host=host, stage2=STAGE2_PATH, run=run, ask=refuse_ask) == 0
    assert run.calls == [[]]


def test_elevated_prompt_end_of_input_and_ctrl_c(stage1):
    host = make_host(stage1, elevated=True, interactive=True)

    def end_of_input(prompt: str) -> str:
        """Behave like input() at the end of standard input."""

        raise EOFError

    def interrupted(prompt: str) -> str:
        """Behave like input() when Ctrl+C is pressed."""

        raise KeyboardInterrupt

    assert stage1.confirm_elevated(host, end_of_input) is False
    assert stage1.main([], host=host, stage2=STAGE2_PATH, ask=interrupted) == 130


def test_map_exit_code_translates_console_ctrl_c(stage1):
    assert stage1.map_exit_code(0xC000013A) == 130
    assert stage1.map_exit_code(-1073741510) == 130
    assert stage1.map_exit_code(0) == 0
    assert stage1.map_exit_code(7) == 7


def test_run_stage2_sets_marker_only_for_the_child(stage1, tmp_path, monkeypatch):
    record = tmp_path / "record.json"
    child = tmp_path / "child.py"
    child.write_text(
        "import json, os, sys\n"
        "with open({!r}, 'w', encoding='utf-8') as handle:\n"
        "    json.dump({{'argv': sys.argv[1:], 'marker': os.environ.get({!r})}},"
        " handle)\n"
        "raise SystemExit(5)\n".format(str(record), stage1.MARKER),
        encoding="utf-8",
    )
    monkeypatch.delenv(stage1.MARKER, raising=False)

    result = stage1.run_stage2(["--skip-submodule-init", "a b", "x)y"], child)

    assert result == 5
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "argv": ["--skip-submodule-init", "a b", "x)y"],
        "marker": "1",
    }
    assert stage1.MARKER not in os.environ


@WINDOWS_ONLY
def test_run_stage2_maps_child_ctrl_c_status_to_130(stage1, tmp_path):
    child = tmp_path / "child.py"
    child.write_text("raise SystemExit(-1073741510)\n", encoding="utf-8")

    assert stage1.run_stage2([], child) == 130


def test_windows_path_helpers(stage1):
    assert stage1.windows_path_text("/c/Users/x") == r"C:\Users\x"
    assert stage1.windows_path_text(r"D:\x") == r"D:\x"
    assert stage1.comparable_windows_path(r"C:\Users\X\\") == (
        stage1.comparable_windows_path("/c/users/x")
    )


# Static batch-file checks, any OS -------------------------------------------


def batch_lines(path: Path) -> List[str]:
    """Return the file's lines without line endings."""

    return path.read_bytes().decode("ascii").split("\r\n")


def code_lines(path: Path) -> List[str]:
    """Return the non-comment, non-blank lines of a batch file."""

    lines = []
    for line in batch_lines(path):
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("rem"):
            continue
        lines.append(stripped)
    return lines


def unquoted(text: str) -> str:
    """Return `text` with double-quoted spans removed."""

    return re.sub(r'"[^"]*"', '""', text)


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_file_is_ascii_with_crlf_line_endings(path):
    data = path.read_bytes()

    data.decode("ascii")
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n")
    assert b"\r\r" not in data


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_file_has_no_labels_goto_or_output_capture(path):
    for line in code_lines(path):
        assert not line.startswith(":"), line
        assert not re.search(r"\bgoto\b", line, re.IGNORECASE), line
        assert not re.search(r"\bfor\s+/f\b", line, re.IGNORECASE), line


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_file_keeps_arguments_out_of_blocks(path):
    depth = 0
    argument_lines = []
    for line in code_lines(path):
        bare = unquoted(line).replace("^(", "").replace("^)", "")
        if "%*" in line:
            argument_lines.append((depth, line))
        depth += bare.count("(") - bare.count(")")
        assert depth >= 0, line
    assert depth == 0
    assert len(argument_lines) == 1
    depth_at_use, line = argument_lines[0]
    assert depth_at_use == 0
    assert line.startswith('"%TK_PY%" %TK_PYARG% ')
    assert line.endswith(" %*")


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_file_exits_through_cmd_exit(path):
    lines = code_lines(path)

    assert lines[-2:] == ['set "CMD_EXIT=%ERRORLEVEL%"', "exit /b %CMD_EXIT%"]
    assert lines[-3].endswith(" %*")


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_probe_has_no_redirection_or_escape_characters(path):
    probes = [line for line in code_lines(path) if line.startswith('set "TK_PROBE=')]

    assert len(probes) == 1
    body = probes[0][len('set "TK_PROBE=') : -1]
    assert "0x03090000" in body
    assert "win32" in body
    assert not set(body) & set("<>|&^%")


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_file_checks_its_helper_exists(path):
    text = path.read_text(encoding="ascii")

    assert 'if not exist "%TK_' in text
    assert "NoDefaultCurrentDirectoryInExePath=1" in text
    assert "PYTHON_MANAGER_AUTOMATIC_INSTALL=false" in text


def test_install_cmd_and_helper_are_project_neutral():
    assert "claude-wrangler" not in INSTALL_CMD.read_text(encoding="ascii")
    assert "claude-wrangler" not in STAGE1_PATH.read_text(encoding="utf-8")


def test_install_prerequisites_cmd_installs_python_per_user_only():
    text = PREREQ_CMD.read_text(encoding="ascii")

    assert "--scope user" in text
    assert "Python.Python.3.12" in text
    assert "runas" not in text.lower()
    assert 'if defined TK_YES set "TK_WINGET_CMD=%TK_WINGET_CMD% --silent"' in text


def test_gitattributes_pins_only_batch_files_to_crlf():
    rules = [
        line.strip()
        for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert rules == ["*.cmd text eol=crlf", "*.bat text eol=crlf"]


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_git_reports_crlf_attribute_for_batch_files(path):
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("git checkout not available")
    relative = path.relative_to(REPO_ROOT).as_posix()
    completed = subprocess.run(
        [git, "check-attr", "eol", "--", relative],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "{}: eol: crlf".format(relative)


# Windows subprocess tests ---------------------------------------------------

FAKE_STAGE2 = '''"""Fake stage 2 for install.cmd tests: records how it was started."""

import argparse
import importlib.util
import json
import os
import sys

STAGE1_MARKER = "THEKNOWLEDGE_MANAGED_INSTALL_STAGE1"
REAL_STAGE2 = {real!r}


def parse_args(argv):
    """Accept stage 2's mode options plus free-form extra arguments."""

    if "-h" in argv or "--help" in argv:
        spec = importlib.util.spec_from_file_location("_real_stage_2", REAL_STAGE2)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.parse_args(argv)
    parser = argparse.ArgumentParser(prog="install-stage-2.py")
    parser.add_argument(
        "--mode", choices=("standard", "dev", "venv-only"), default="standard"
    )
    parser.add_argument("--system", action="store_true")
    parser.add_argument("--skip-submodule-init", action="store_true")
    parser.add_argument("extra", nargs="*")
    return parser.parse_args(argv)


if __name__ == "__main__":
    parse_args(sys.argv[1:])
    with open(os.environ["FAKE_STAGE2_RECORD"], "w", encoding="utf-8") as handle:
        json.dump(
            {{
                "argv": sys.argv[1:],
                "marker": os.environ.get(STAGE1_MARKER),
                "exe": sys.executable,
            }},
            handle,
        )
    raise SystemExit(int(os.environ.get("FAKE_STAGE2_CODE", "0")))
'''

FAKE_PREREQ_HELPER = '''"""Fake install_prerequisites.py: records how it was started."""

import json
import os
import sys

with open(os.environ["FAKE_HELPER_RECORD"], "w", encoding="utf-8") as handle:
    json.dump({"argv": sys.argv[1:], "exe": sys.executable}, handle)
raise SystemExit(int(os.environ.get("FAKE_HELPER_CODE", "0")))
'''

# winget stand-in. With FAKE_WINGET_PYTHON_HOME set it "installs" Python by
# linking the per-user python.org location to an existing interpreter.
FAKE_WINGET_CMD = "\r\n".join(
    [
        "@echo off",
        '>"%FAKE_WINGET_RECORD%" echo %*',
        "if defined FAKE_WINGET_PYTHON_HOME mkdir"
        ' "%LOCALAPPDATA%\\Programs\\Python" 1>nul 2>nul',
        "if defined FAKE_WINGET_PYTHON_HOME mklink /J"
        ' "%LOCALAPPDATA%\\Programs\\Python\\Python312"'
        ' "%FAKE_WINGET_PYTHON_HOME%" 1>nul',
        "exit /b %FAKE_WINGET_CODE%",
        "",
    ]
)


def system32() -> Path:
    """Return the Windows System32 directory."""

    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def base_python() -> Optional[Path]:
    """Return the base interpreter behind this test run, when it has one."""

    candidate = Path(sys.base_prefix) / "python.exe"
    return candidate if candidate.is_file() else None


def git_bash() -> Optional[Path]:
    """Return Git for Windows' bash.exe, never the WSL bash in System32."""

    candidates = []
    git = shutil.which("git")
    if git is not None:
        # git.exe sits in cmd\, bin\, or mingw64\bin\ under the Git root.
        git_dir = Path(git).resolve().parent
        candidates.extend(
            root / "bin" / "bash.exe"
            for root in (git_dir.parent, git_dir.parent.parent)
        )
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")
    return next((path for path in candidates if path.is_file()), None)


class Checkout:
    """A copied fake checkout plus the environment to run it in."""

    def __init__(self, root: Path, work: Path) -> None:
        """Remember the checkout root and the scratch directory."""

        self.root = root
        self.work = work
        self.stage2_record = work / "stage2-record.json"
        self.helper_record = work / "helper-record.json"
        self.winget_record = work / "winget-record.txt"
        self.stub_dir = work / "stub bin"
        self.home = work / "home"

    @property
    def install_cmd(self) -> Path:
        """Return the copied install.cmd."""

        return self.root / "install.cmd"

    @property
    def prereq_cmd(self) -> Path:
        """Return the copied scripts/install_prerequisites.cmd."""

        return self.root / "scripts" / "install_prerequisites.cmd"

    def environment(
        self,
        *,
        python: Optional[str] = None,
        path: Optional[Sequence[Path]] = None,
        code: int = 0,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Return a scrubbed environment with every home in the scratch tree."""

        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in SCRUBBED_VARIABLES
        }
        env.update(
            {
                "USERPROFILE": str(self.home),
                "APPDATA": str(self.home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHON_MANAGER_AUTOMATIC_INSTALL": "false",
                "THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED": "1",
                "FAKE_STAGE2_RECORD": str(self.stage2_record),
                "FAKE_STAGE2_CODE": str(code),
                "FAKE_HELPER_RECORD": str(self.helper_record),
                "FAKE_HELPER_CODE": str(code),
                "FAKE_WINGET_RECORD": str(self.winget_record),
                "FAKE_WINGET_CODE": "0",
            }
        )
        if python is not None:
            env["THEKNOWLEDGE_BOOTSTRAP_PYTHON"] = python
        if path is not None:
            env["PATH"] = ";".join(str(entry) for entry in path)
            # Keep python.org all-users installs out of the default-location
            # fallback so the PATH under test is the only source.
            env["ProgramFiles"] = str(self.work / "Program Files")
        env.update(extra or {})
        return env

    def read_stage2_record(self) -> Optional[dict]:
        """Return the fake stage 2's record, or None when it never ran."""

        if not self.stage2_record.exists():
            return None
        return json.loads(self.stage2_record.read_text(encoding="utf-8"))

    def read_helper_record(self) -> Optional[dict]:
        """Return the fake prerequisite helper's record, or None."""

        if not self.helper_record.exists():
            return None
        return json.loads(self.helper_record.read_text(encoding="utf-8"))


@pytest.fixture
def checkout(tmp_path):
    """Copy the entry points into a checkout path with spaces and non-ASCII."""

    root = tmp_path / "co with space \u00e9 \u5f20"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(str(INSTALL_CMD), str(root / "install.cmd"))
    shutil.copy2(str(STAGE1_PATH), str(scripts / "install-stage-1-windows.py"))
    shutil.copy2(str(PREREQ_CMD), str(scripts / "install_prerequisites.cmd"))
    (scripts / "install-stage-2.py").write_text(
        FAKE_STAGE2.format(real=str(STAGE2_PATH)), encoding="utf-8"
    )
    (scripts / "install_prerequisites.py").write_text(
        FAKE_PREREQ_HELPER, encoding="utf-8"
    )
    fake = Checkout(root, tmp_path)
    for directory in (
        fake.home / "AppData" / "Roaming",
        fake.home / "AppData" / "Local",
        fake.stub_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # A python.exe that fails every probe, like the Microsoft Store alias.
    shutil.copy2(str(system32() / "where.exe"), str(fake.stub_dir / "python.exe"))
    (fake.stub_dir / "winget.cmd").write_bytes(FAKE_WINGET_CMD.encode("ascii"))
    return fake


def run_cmd(
    script: Path,
    args: str,
    env: Dict[str, str],
    *,
    new_console: bool = False,
) -> subprocess.CompletedProcess:
    """Run a batch file through `cmd /d /s /c` with a raw argument string.

    With `new_console` the run gets its own hidden console, which uses the
    system's OEM code page instead of the agent's UTF-8 console.
    """

    kwargs = {}
    if new_console:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        kwargs["startupinfo"] = startupinfo
    command_line = 'cmd.exe /d /s /c ""{}" {}"'.format(script, args)
    return subprocess.run(
        command_line,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
        **kwargs,
    )


def run_powershell(
    shell: str, script: Path, args: Sequence[str], env: Dict[str, str]
) -> subprocess.CompletedProcess:
    """Run a batch file from PowerShell and return its exit code."""

    def quote(text: str) -> str:
        """Quote text as a PowerShell single-quoted string."""

        return "'{}'".format(text.replace("'", "''"))

    command = "& {} {}; exit $LASTEXITCODE".format(
        quote(str(script)), " ".join(quote(arg) for arg in args)
    )
    return subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )


def text(stream: bytes) -> str:
    """Decode captured output leniently."""

    return stream.decode("utf-8", "replace")


@WINDOWS_ONLY
def test_install_cmd_forwards_arguments_and_marker(checkout):
    env = checkout.environment(python=sys.executable, code=0)

    completed = run_cmd(
        checkout.install_cmd, '--skip-submodule-init "a b" "x)y" "a^b" "a&b"', env
    )

    assert completed.returncode == 0, text(completed.stderr)
    record = checkout.read_stage2_record()
    assert record["argv"] == ["--skip-submodule-init", "a b", "x)y", "a^b", "a&b"]
    assert record["marker"] == "1"


@WINDOWS_ONLY
def test_install_cmd_survives_unquoted_parenthesis_argument(checkout):
    env = checkout.environment(python=sys.executable, code=4)

    completed = run_cmd(checkout.install_cmd, "--skip-submodule-init x)y", env)

    assert completed.returncode == 4, text(completed.stderr)
    assert checkout.read_stage2_record()["argv"] == ["--skip-submodule-init", "x)y"]


@WINDOWS_ONLY
@pytest.mark.parametrize("code", [0, 7])
def test_install_cmd_exit_code_through_cmd(checkout, code):
    env = checkout.environment(python=sys.executable, code=code)

    completed = run_cmd(checkout.install_cmd, "--skip-submodule-init", env)

    assert completed.returncode == code, text(completed.stderr)
    assert checkout.read_stage2_record() is not None


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "shell",
    [
        "pwsh.exe",
        str(system32() / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
    ],
    ids=["pwsh", "powershell"],
)
def test_install_cmd_exit_code_and_hostile_args_through_powershell(checkout, shell):
    resolved = shutil.which(shell) if not os.path.isabs(shell) else shell
    if resolved is None or not os.path.isfile(resolved):
        pytest.skip("{} is not installed".format(shell))
    env = checkout.environment(python=sys.executable, code=7)

    completed = run_powershell(
        resolved,
        checkout.install_cmd,
        ["--skip-submodule-init", "x)y", "a^b", "a b"],
        env,
    )

    assert completed.returncode == 7, text(completed.stderr)
    argv = checkout.read_stage2_record()["argv"]
    assert argv[0] == "--skip-submodule-init"
    assert argv[1] == "x)y"
    # PowerShell passes a^b unquoted, so cmd's own parse of the batch
    # command line consumes the caret before install.cmd starts.
    assert argv[2] in ("a^b", "ab")
    assert argv[3] == "a b"


@WINDOWS_ONLY
def test_install_cmd_exit_code_through_git_bash(checkout):
    bash = git_bash()
    if bash is None:
        pytest.skip("Git for Windows bash is not installed")
    env = checkout.environment(python=sys.executable, code=7)

    completed = subprocess.run(
        [
            str(bash),
            "--noprofile",
            "--norc",
            "-c",
            './install.cmd "$@"; exit $?',
            "bash",
            "--skip-submodule-init",
            "x)y",
        ],
        cwd=str(checkout.root),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 7, text(completed.stderr)
    assert checkout.read_stage2_record()["argv"] == ["--skip-submodule-init", "x)y"]


@WINDOWS_ONLY
def test_install_cmd_refusals_write_no_record(checkout):
    env = checkout.environment(python=sys.executable)

    for args in ("--mode dev", "--sys", "--mode venv-only"):
        completed = run_cmd(checkout.install_cmd, args, env)
        assert completed.returncode == 1, args
        assert "not supported on native Windows" in text(completed.stderr)
        assert checkout.read_stage2_record() is None


@WINDOWS_ONLY
@pytest.mark.parametrize("token", ["--help", "-h", "/?"])
def test_install_cmd_help_with_python(checkout, token):
    env = checkout.environment(python=sys.executable)

    completed = run_cmd(checkout.install_cmd, token, env)

    assert completed.returncode == 0, text(completed.stderr)
    output = text(completed.stdout)
    for line in HELP_CONTRACT:
        assert line in output
    assert "--mode {standard,dev,venv-only}" in output
    assert checkout.read_stage2_record() is None


@WINDOWS_ONLY
def test_install_cmd_help_without_python(checkout):
    env = checkout.environment(path=[checkout.stub_dir, system32()])

    completed = run_cmd(checkout.install_cmd, "--help", env)

    assert completed.returncode == 0, text(completed.stderr)
    output = text(completed.stdout)
    for line in HELP_CONTRACT:
        assert line in output
    assert "Stage-2 options are unavailable" in output


@WINDOWS_ONLY
def test_install_cmd_stub_only_path_fails_with_prerequisites_hint(checkout):
    env = checkout.environment(path=[checkout.stub_dir, system32()])

    completed = run_cmd(checkout.install_cmd, "--skip-submodule-init", env)

    assert completed.returncode == 1
    error = text(completed.stderr)
    assert "no native Python 3.9+ found" in error
    assert "install_prerequisites.cmd" in error
    assert checkout.read_stage2_record() is None


@WINDOWS_ONLY
def test_install_cmd_stub_first_path_finds_real_python(checkout):
    real = base_python()
    if real is None:
        pytest.skip("no base python.exe next to this interpreter")
    env = checkout.environment(path=[checkout.stub_dir, real.parent, system32()])

    completed = run_cmd(checkout.install_cmd, "--skip-submodule-init", env)

    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.samefile(checkout.read_stage2_record()["exe"], str(real))


@WINDOWS_ONLY
def test_install_cmd_invalid_override_does_not_fall_back(checkout, tmp_path):
    env = checkout.environment(python=str(tmp_path / "missing" / "python.exe"))

    completed = run_cmd(checkout.install_cmd, "--skip-submodule-init", env)

    assert completed.returncode == 1
    assert "not falling back" in text(completed.stderr)
    assert checkout.read_stage2_record() is None


@WINDOWS_ONLY
def test_install_cmd_missing_helper_exits_1(checkout):
    (checkout.root / "scripts" / "install-stage-1-windows.py").unlink()
    env = checkout.environment(python=sys.executable)

    completed = run_cmd(checkout.install_cmd, "--skip-submodule-init", env)

    assert completed.returncode == 1
    assert "this checkout is incomplete" in text(completed.stderr)


@pytest.fixture
def non_ascii_venv(tmp_path):
    """Create a pip-less venv under a directory with non-ASCII characters."""

    if os.name != "nt":
        pytest.skip("runs Windows shells")
    base = base_python() or Path(sys.executable)
    venv = tmp_path / "Jos\u00e9 M\u00fcller \u5f20" / "venv"
    subprocess.run(
        [str(base), "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return venv / "Scripts" / "python.exe"


@WINDOWS_ONLY
@pytest.mark.parametrize("source", ["override", "path"])
def test_install_cmd_non_ascii_interpreter_path_in_oem_console(
    checkout, non_ascii_venv, source
):
    if source == "override":
        env = checkout.environment(python=str(non_ascii_venv))
    else:
        env = checkout.environment(path=[non_ascii_venv.parent, system32()])

    completed = run_cmd(
        checkout.install_cmd, "--skip-submodule-init", env, new_console=True
    )

    assert completed.returncode == 0, text(completed.stderr)
    record = checkout.read_stage2_record()
    assert os.path.normcase(record["exe"]) == os.path.normcase(str(non_ascii_venv))
    assert record["marker"] == "1"


# install_prerequisites.cmd, Windows ----------------------------------------


@WINDOWS_ONLY
def test_prerequisites_cmd_forwards_to_helper_with_python(checkout):
    env = checkout.environment(python=sys.executable, code=3)

    completed = run_cmd(checkout.prereq_cmd, '--dev --yes "x)y"', env)

    assert completed.returncode == 3, text(completed.stderr)
    assert checkout.read_helper_record()["argv"] == ["--dev", "--yes", "x)y"]
    assert not checkout.winget_record.exists()


@WINDOWS_ONLY
def test_prerequisites_cmd_help_without_python(checkout):
    env = checkout.environment(path=[checkout.stub_dir, system32()])

    completed = run_cmd(checkout.prereq_cmd, "--help", env)

    assert completed.returncode == 0, text(completed.stderr)
    assert "--dry-run" in text(completed.stdout)
    assert not checkout.winget_record.exists()


@WINDOWS_ONLY
def test_prerequisites_cmd_without_winget_points_to_python_org(checkout, tmp_path):
    empty = tmp_path / "empty bin"
    empty.mkdir()
    env = checkout.environment(path=[empty, system32()])

    completed = run_cmd(checkout.prereq_cmd, "--yes", env)

    assert completed.returncode == 1
    assert "winget is not available" in text(completed.stderr)


@WINDOWS_ONLY
def test_prerequisites_cmd_refuses_without_consent(checkout):
    env = checkout.environment(path=[checkout.stub_dir, system32()])

    completed = run_cmd(checkout.prereq_cmd, "--dev", env)

    assert completed.returncode == 1
    assert "Rerun with --yes" in text(completed.stderr)
    assert not checkout.winget_record.exists()
    assert checkout.read_helper_record() is None


@WINDOWS_ONLY
def test_prerequisites_cmd_dry_run_prints_user_scope_command(checkout):
    env = checkout.environment(path=[checkout.stub_dir, system32()])

    completed = run_cmd(checkout.prereq_cmd, "--dev --dry-run", env)

    assert completed.returncode == 0, text(completed.stderr)
    output = text(completed.stdout)
    assert "winget install --id Python.Python.3.12" in output
    assert "--scope user" in output
    assert "--silent" not in output
    assert not checkout.winget_record.exists()


@WINDOWS_ONLY
def test_prerequisites_cmd_yes_installs_then_asks_for_new_terminal(checkout):
    env = checkout.environment(path=[checkout.stub_dir, system32()])

    completed = run_cmd(checkout.prereq_cmd, "--dev --yes", env)

    assert completed.returncode == 1
    assert "Open a new terminal" in text(completed.stdout)
    winget_args = checkout.winget_record.read_text(encoding="ascii").split()
    assert winget_args[:3] == ["install", "--id", "Python.Python.3.12"]
    assert "--exact" in winget_args
    assert winget_args[winget_args.index("--scope") + 1] == "user"
    assert "--accept-package-agreements" in winget_args
    assert "--accept-source-agreements" in winget_args
    assert "--silent" in winget_args
    assert checkout.read_helper_record() is None


@WINDOWS_ONLY
def test_prerequisites_cmd_reports_winget_failure(checkout):
    env = checkout.environment(
        path=[checkout.stub_dir, system32()], extra={"FAKE_WINGET_CODE": "5"}
    )

    completed = run_cmd(checkout.prereq_cmd, "--yes", env)

    assert completed.returncode == 1
    assert "winget could not install Python" in text(completed.stderr)
    assert checkout.read_helper_record() is None


@WINDOWS_ONLY
def test_prerequisites_cmd_continues_with_newly_installed_python(checkout):
    real = base_python()
    if real is None:
        pytest.skip("no base python.exe next to this interpreter")
    junction = checkout.home / "AppData" / "Local" / "Programs" / "Python"
    junction = junction / "Python312"
    env = checkout.environment(
        path=[checkout.stub_dir, system32()],
        code=0,
        extra={"FAKE_WINGET_PYTHON_HOME": str(real.parent)},
    )
    try:
        completed = run_cmd(checkout.prereq_cmd, "--dev --yes x)y", env)

        assert completed.returncode == 0, text(completed.stderr)
        assert "continuing with it" in text(completed.stdout)
        record = checkout.read_helper_record()
        assert record["argv"] == ["--dev", "--yes", "x)y"]
        assert PureWindowsPath(record["exe"]).parent == PureWindowsPath(junction)
    finally:
        # Remove the junction itself, never what it points to.
        if os.path.lexists(str(junction)):
            os.rmdir(str(junction))


# PATH quoting and interpreter paths with cmd metacharacters -----------------


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_probes_start_python_exe_without_call(path):
    # call re-parses its line: it doubles carets and expands percent signs a
    # second time, so a python.exe under such a directory would be rejected.
    for line in code_lines(path):
        if 'python.exe" -c' in line:
            assert not line.startswith("call "), line


@pytest.mark.parametrize("path", BATCH_FILES, ids=lambda path: path.name)
def test_batch_file_reads_path_only_through_delayed_expansion(path):
    # cmd aborts the whole file when a parsed line passes 8191 characters,
    # so PATH and its copy must never be expanded with percent signs.
    lines = code_lines(path)
    for line in lines:
        assert not re.search(r"%(TK_)?PATH[:%]", line, re.IGNORECASE), line
    assert "if not defined TK_PY setlocal EnableDelayedExpansion" in lines
    assert any('set "TK_PATH=!PATH:"=!"' in line for line in lines)
    # The quoted list is length-checked before cmd builds it.
    assert any("!TK_LISTLEN! gtr 8000" in line for line in lines)
    # Each entry is tested with delayed expansion off, and a hit leaves both
    # scopes at once.
    assert "setlocal DisableDelayedExpansion" in lines
    assert any("(endlocal & endlocal & set " in line for line in lines)


def run_entry(checkout, which: str, env: Dict[str, str]):
    """Run install.cmd or install_prerequisites.cmd; return (result, record)."""

    script = checkout.install_cmd if which == "install" else checkout.prereq_cmd
    completed = run_cmd(script, "--skip-submodule-init", env)
    record = (
        checkout.read_stage2_record()
        if which == "install"
        else checkout.read_helper_record()
    )
    return completed, record


QUOTED_PAREN_ENTRY = r'"C:\Quoted Tools (x86)\bin"'


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
def test_entry_points_survive_a_quoted_path_entry_with_parentheses(checkout, which):
    real = base_python()
    if real is None:
        pytest.skip("no base python.exe next to this interpreter")
    # With an override, the PATH scan line is still parsed.
    env = checkout.environment(python=sys.executable)
    env["PATH"] = ";".join([QUOTED_PAREN_ENTRY, str(system32())])
    completed, record = run_entry(checkout, which, env)
    assert completed.returncode == 0, text(completed.stderr)
    assert record is not None
    # Without one, the scan continues past the quoted entry.
    env = checkout.environment(
        path=[checkout.stub_dir, QUOTED_PAREN_ENTRY, real.parent, system32()]
    )
    completed, record = run_entry(checkout, which, env)
    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.samefile(record["exe"], str(real))


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
def test_entry_points_find_python_in_a_quoted_path_entry(checkout, which):
    real = base_python()
    if real is None:
        pytest.skip("no base python.exe next to this interpreter")
    env = checkout.environment(
        path=[checkout.stub_dir, '"{}"'.format(real.parent), system32()]
    )
    completed, record = run_entry(checkout, which, env)
    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.samefile(record["exe"], str(real))


@pytest.fixture(scope="module")
def venv_python_files(tmp_path_factory):
    """Return a pip-less venv's python.exe and pyvenv.cfg to copy elsewhere."""

    if os.name != "nt":
        pytest.skip("runs Windows shells")
    base = base_python() or Path(sys.executable)
    venv = tmp_path_factory.mktemp("venvsrc") / "venv"
    subprocess.run(
        [str(base), "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return venv / "Scripts" / "python.exe", venv / "pyvenv.cfg"


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
@pytest.mark.parametrize("source", ["path", "override"])
@pytest.mark.parametrize("label", ["caret", "percent"])
def test_entry_points_accept_interpreters_under_cmd_metacharacters(
    checkout, tmp_path, venv_python_files, which, source, label
):
    # A venv launcher finds pyvenv.cfg beside itself, so a copy of both is a
    # working interpreter at any path.
    name = {"caret": "car^et dir", "percent": "pct %" + "OS" + "% dir"}[label]
    directory = tmp_path / name
    directory.mkdir()
    for item in venv_python_files:
        shutil.copy2(str(item), str(directory / item.name))
    python = directory / "python.exe"
    if source == "override":
        env = checkout.environment(python=str(python))
    else:
        env = checkout.environment(path=[checkout.stub_dir, directory, system32()])

    completed, record = run_entry(checkout, which, env)

    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.normcase(record["exe"]) == os.path.normcase(str(python))


# Long PATH values and delayed-expansion scope -------------------------------


def junk_path_entries(total: int, width: int = 40) -> List[str]:
    """Return absent directories whose joined length reaches `total`."""

    entries: List[str] = []
    while len(";".join(entries)) < total:
        entries.append(r"C:\nonexistent\d{:05d}".format(len(entries)).ljust(width, "a"))
    return entries


def copy_venv_python(venv_python_files, directory: Path) -> Path:
    """Copy a venv launcher and its pyvenv.cfg into `directory`."""

    directory.mkdir(parents=True, exist_ok=True)
    for item in venv_python_files:
        shutil.copy2(str(item), str(directory / item.name))
    return directory / "python.exe"


# 8150 sits in the band where the quoted entry list passes 8191 characters
# while PATH itself does not: cmd crashed there on a delayed replacement.
LONG_PATH_LENGTHS = (8150, 8500, 20000)


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
@pytest.mark.parametrize("length", LONG_PATH_LENGTHS)
def test_entry_points_survive_a_path_too_long_for_cmd(
    checkout, tmp_path, venv_python_files, which, length
):
    override = copy_venv_python(venv_python_files, tmp_path / "override")
    default = copy_venv_python(
        venv_python_files,
        checkout.home / "AppData" / "Local" / "Programs" / "Python" / "Python399",
    )
    # System32 comes first: cmd cannot search a PATH this long for commands.
    entries = [str(system32())] + junk_path_entries(length)

    env = checkout.environment(path=entries, extra={})
    env["THEKNOWLEDGE_BOOTSTRAP_PYTHON"] = str(override)
    completed, record = run_entry(checkout, which, env)
    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.normcase(record["exe"]) == os.path.normcase(str(override))

    env = checkout.environment(path=entries)
    completed, record = run_entry(checkout, which, env)
    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.normcase(record["exe"]) == os.path.normcase(str(default))


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
@pytest.mark.parametrize("length", (8150, 20000))
def test_entry_points_say_when_a_long_path_was_not_scanned(
    checkout, tmp_path, which, length
):
    empty = tmp_path / "empty bin"
    empty.mkdir()
    env = checkout.environment(
        path=[str(system32()), str(empty)] + junk_path_entries(length)
    )

    completed, record = run_entry(checkout, which, env)

    assert completed.returncode == 1
    assert record is None
    assert "PATH was not scanned" in text(completed.stderr)


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
def test_entry_points_scan_a_long_path_that_still_fits(
    checkout, tmp_path, venv_python_files, which
):
    python = copy_venv_python(venv_python_files, tmp_path / "late dir")
    entries = [checkout.stub_dir] + junk_path_entries(7000)
    env = checkout.environment(path=entries + [str(python.parent), str(system32())])

    completed, record = run_entry(checkout, which, env)

    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.normcase(record["exe"]) == os.path.normcase(str(python))


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
@pytest.mark.parametrize("name", ["bang! dir", "b!PATH!c", "a !b! & (c)"])
def test_entry_points_keep_path_entries_with_exclamation_marks(
    checkout, tmp_path, venv_python_files, which, name
):
    python = copy_venv_python(venv_python_files, tmp_path / name)
    env = checkout.environment(path=[checkout.stub_dir, python.parent, system32()])

    completed, record = run_entry(checkout, which, env)

    assert completed.returncode == 0, text(completed.stderr)
    assert os.path.normcase(record["exe"]) == os.path.normcase(str(python))


@WINDOWS_ONLY
@pytest.mark.parametrize("which", ["install", "prereq"])
@pytest.mark.parametrize("found", ["py-path", "default", "none"])
def test_entry_points_leave_the_callers_console_unchanged(
    checkout, tmp_path, venv_python_files, which, found
):
    # A caller that uses "call" shares the batch file's environment, so an
    # unbalanced setlocal would leak the TK_ helpers and settings into it.
    empty = tmp_path / "empty bin"
    empty.mkdir()
    path = [str(empty), str(system32())]
    if found == "py-path":
        path.insert(0, str(copy_venv_python(venv_python_files, tmp_path / "py").parent))
    elif found == "default":
        copy_venv_python(
            venv_python_files,
            checkout.home / "AppData" / "Local" / "Programs" / "Python" / "Python399",
        )
    env = checkout.environment(path=path)
    for name in (
        "NoDefaultCurrentDirectoryInExePath",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    ):
        env.pop(name, None)
        env.pop(name.upper(), None)
    script = checkout.install_cmd if which == "install" else checkout.prereq_cmd
    command_line = (
        'cmd.exe /d /s /c "call "{}" --skip-submodule-init 1>nul 2>nul'
        ' & set TK_ & set NoDefaultCurrentDirectoryInExePath & set PYTHONUTF8"'
    ).format(script)

    completed = subprocess.run(
        command_line,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert text(completed.stdout).strip() == ""
