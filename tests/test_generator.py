"""Tests for generated launcher files, including real shim execution.

The shim tests install a fake Claude Code binary through the real install
transaction and then execute the generated shims for real: on Windows
through `cmd /d /s /c`, `pwsh -NoProfile`, `powershell.exe -NoProfile`, and
Git Bash when present, from a checkout path containing spaces, non-ASCII
characters, `&`, `%`, and parentheses; on POSIX through `sh`. On Windows the
fake binary is a small C# console program compiled at test time with the
.NET Framework compiler (the tests skip when csc.exe is absent).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Any, Dict, List, Optional

import pytest

from claude_wrangler import launcher_core, platforms
from claude_wrangler.constants import METADATA_FILENAME
from claude_wrangler.generator import (
    SettingsFileError,
    check_config_settings,
    generated_file_status,
    merge_config_settings,
    planned_files,
    remove_generated_files,
    write_generated_files,
)
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.operations import install_like_operation
from claude_wrangler.rendering import (
    build_cmd_shim,
    build_launch_config,
    build_path_shim_cmd,
    build_path_shim_sh,
    build_posix_shim,
    cmd_safe_python_path,
    launcher_core_source,
)

from native_fakes import (
    CSC_PATH,
    FakeRelease,
    FakeReleaseSource,
    clean_launch_environment,
    compile_fake_claude,
    git_bash,
    host_key,
    make_config,
    make_services,
    posix_fake_claude,
)

WINDOWS = os.name == "nt"
HOSTILE_NAME = (
    "co (x) & y %cwt% \u00e9 \u5f20"
    if WINDOWS
    else "co (x) & y %cwt% \u00e9 \u5f20 'q'"
)


# ---------------------------------------------------------------------------
# Rendered content
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path):
    """Return a config for a project at tmp_path/project."""

    project = tmp_path / "project"
    project.mkdir()
    return make_config(project)


def test_cmd_shim_is_ascii_crlf_label_free_and_forwards_args_last(config):
    item = next(i for i in planned_files(config) if i.key == "launcher_cmd")
    data = item.data
    data.decode("ascii")
    assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n")
    lines = data.decode("ascii").split("\r\n")
    assert not [line for line in lines if line.lstrip().startswith(":")]
    assert "goto" not in data.decode("ascii").lower()
    with_args = [index for index, line in enumerate(lines) if "%*" in line]
    assert len(with_args) == 1
    final = lines[with_args[0]]
    assert final.startswith('"%CLAUDE_LOCAL_PY%" %CLAUDE_LOCAL_PYARG% -I ')
    assert lines[with_args[0] + 1 :] == [
        'set "CMD_EXIT=%ERRORLEVEL%"',
        "exit /b %CMD_EXIT%",
        "",
    ]
    assert "managed by claude-wrangler" in lines[1]


def test_posix_shim_is_lf_sh_with_probed_fallbacks(config):
    text = build_posix_shim(config)
    assert text.startswith("#!/bin/sh\n")
    assert "\r" not in text
    assert 'exec "$claude_local_recorded" -I "$claude_local_core" "$@"' in text
    assert "exec py -3 -I" in text
    assert "for claude_local_python in python3 python; do" in text
    assert "cygpath -w" in text
    assert "MSYS_NO_PATHCONV=1" in text
    assert ".local/claude/wrangler/launch.py" in text


def test_shims_for_a_nested_launcher_climb_to_the_repository(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(project, "--launcher", "tools/x/claude-local")
    assert '$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)' in build_posix_shim(
        config
    )
    assert 'for %%I in ("%~dp0..\\..") do' in build_cmd_shim(config)
    assert (
        '"$(dirname -- "$0")"/\'../../../../tools/x/claude-local\''
        in build_path_shim_sh(config)
    )
    assert (
        '"%~dp0..\\..\\..\\..\\tools\\x\\claude-local.cmd" %*'
        in build_path_shim_cmd(config)
    )


def test_launch_json_follows_the_launcher_contract(config):
    data = build_launch_config(config)
    parsed = launcher_core.parse_launch_config(data)
    assert parsed.repo_root_from_core == ("..", "..", "..")
    assert parsed.runtime_root == (".local", "claude", "wrangler")
    assert parsed.settings_override["disableDeepLinkRegistration"] == "disable"
    assert parsed.release_base_url == "https://downloads.claude.ai/claude-code-releases"
    assert data["shared_home_default"] is False


def test_recorded_python_spelling_for_cmd():
    assert cmd_safe_python_path(r"C:\Py 3%\python.exe") == r"C:\Py 3%%\python.exe"
    assert cmd_safe_python_path("/usr/bin/python3") is None
    assert cmd_safe_python_path("python.exe") is None
    assert cmd_safe_python_path(None) is None
    if not WINDOWS:
        assert cmd_safe_python_path("C:\\Jos\u00e9\\python.exe") is None


def test_launch_py_is_a_byte_for_byte_copy(config):
    item = next(i for i in planned_files(config) if i.key == "launch_py")
    assert item.data == Path(launcher_core.__file__).read_bytes()
    assert item.data == launcher_core_source()


def test_write_generated_files_is_atomic_idempotent_and_reports_freshness(config):
    write_generated_files(config)
    status = generated_file_status(config)
    assert all(entry["exists"] and entry["fresh"] for entry in status.values())
    launcher = config.layout.launcher_path
    if not WINDOWS:
        assert launcher.stat().st_mode & stat.S_IXUSR
        assert (config.layout.shim_dir / "claude").stat().st_mode & stat.S_IXUSR
    before = {
        key: Path(entry["path"]).stat().st_mtime_ns for key, entry in status.items()
    }
    write_generated_files(config)
    after = {
        key: Path(entry["path"]).stat().st_mtime_ns
        for key, entry in generated_file_status(config).items()
    }
    assert before == after
    assert not [p for p in launcher.parent.iterdir() if p.suffix == ".tmp"]


def test_generated_files_refuse_to_replace_foreign_content(config):
    launcher = config.layout.launcher_path
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    with pytest.raises(ClaudeWranglerError, match="--force"):
        write_generated_files(config)
    assert launcher.read_text("utf-8") == "#!/bin/sh\necho mine\n"


def test_remove_generated_files_proves_ownership(config):
    write_generated_files(config)
    removed = remove_generated_files(config)
    assert str(config.layout.launcher_path) in removed
    assert str(config.layout.launcher_cmd_path) in removed
    assert not config.layout.readme_path.exists()
    # Runtime-owned files go away with the runtime root, not here.
    assert config.layout.launch_core_path.exists()


def test_settings_merge_preserves_keys_and_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"model": "sonnet", "env": {"KEEP": "1"}, "permissions": {"x": 1}}),
        encoding="utf-8",
    )
    assert merge_config_settings(path) is True
    merged = json.loads(path.read_text("utf-8"))
    assert list(merged) == [
        "model",
        "env",
        "permissions",
        "disableDeepLinkRegistration",
    ]
    assert merged["env"] == {
        "KEEP": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_INSTALLATION_CHECKS": "1",
    }
    assert merge_config_settings(path) is False


@pytest.mark.parametrize("content", ["{oops", "[1, 2]", '{"env": "nope"}'])
def test_settings_merge_refuses_malformed_files(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SettingsFileError):
        check_config_settings(path)
    with pytest.raises(SettingsFileError):
        merge_config_settings(path)
    assert path.read_text("utf-8") == content


def test_settings_merge_creates_a_missing_file(tmp_path):
    path = tmp_path / "cfg" / "settings.json"
    assert merge_config_settings(path) is True
    assert json.loads(path.read_text("utf-8"))["env"]["DISABLE_UPDATES"] == "1"


# ---------------------------------------------------------------------------
# Executing the generated shims
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fake_claude_bytes(tmp_path_factory) -> bytes:
    """Return the fake Claude binary for this host."""

    if WINDOWS:
        if not CSC_PATH.is_file():
            pytest.skip("csc.exe (.NET Framework 4) is not installed")
        exe = compile_fake_claude(
            tmp_path_factory.mktemp("csc") / "claude.exe",
            pad_to=platforms.MIN_EXECUTABLE_SIZE + 4096,
        )
        return exe.read_bytes()
    return posix_fake_claude()


@pytest.fixture
def launched(tmp_path: Path, fake_claude_bytes: bytes):
    """Install the fake binary into a hostile checkout path; return helpers."""

    project = tmp_path / HOSTILE_NAME
    (project / ".git").mkdir(parents=True)
    key = host_key()
    release = FakeRelease("2.1.296", {key: fake_claude_bytes})
    source = FakeReleaseSource([release], {"stable": "2.1.296", "latest": "2.1.296"})
    services = make_services(
        source,
        real_runner=True,
        real_structure=WINDOWS,
        environ=clean_launch_environment(),
    )
    config = make_config(
        project, "--set-update-checks", "off", "--set-windows-sandbox-warning", "off"
    )
    assert install_like_operation(config, services) == 0
    return Launched(project, tmp_path / "report.json")


class Launched:
    """A checkout with the fake binary installed, plus run helpers."""

    def __init__(self, project: Path, report: Path) -> None:
        """Remember the checkout and the report path."""

        self.project = project
        self.report = report

    def env(self, **extra: str) -> Dict[str, str]:
        """Return a clean launch environment that writes the fake's report."""

        values = {"FAKE_CLAUDE_REPORT": str(self.report), "FAKE_CLAUDE_EXIT": "7"}
        values.update(extra)
        return clean_launch_environment(values)

    def run(self, command: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        """Run a command in the checkout with captured output."""

        if self.report.exists():
            self.report.unlink()
        kwargs.setdefault("env", self.env())
        return subprocess.run(
            command,
            cwd=str(self.project),
            capture_output=True,
            timeout=120,
            check=False,
            **kwargs,
        )

    def child(self) -> Dict[str, Any]:
        """Return what the fake binary saw."""

        return json.loads(self.report.read_text(encoding="utf-8"))

    @property
    def config_dir(self) -> str:
        """Return the native CLAUDE_CONFIG_DIR spelling."""

        return str(self.project / ".local" / "claude")


def assert_ring1_launch(
    launched: Launched, completed, args: List[str]
) -> Dict[str, Any]:
    """Assert the shim reached the fake binary with the ring-1 environment."""

    assert completed.returncode == 7, completed.stderr.decode("utf-8", "replace")
    child = launched.child()
    assert child["argv"][0] == "--settings"
    assert json.loads(child["argv"][1])["env"]["DISABLE_UPDATES"] == "1"
    assert child["argv"][2:] == args
    env = child["env"]
    assert env["CLAUDE_CONFIG_DIR"] == launched.config_dir
    home = os.path.join(launched.config_dir, "HOME")
    assert env["HOME"] == home
    assert env["CLAUDE_LOCAL_SHARED_HOME"] == "0"
    assert env["DISABLE_UPDATES"] == "1"
    assert env["CLAUDE_LOCAL_MANAGED_BY"] == "claude-wrangler"
    if WINDOWS:
        assert env["USERPROFILE"] == home
        assert env["APPDATA"] == os.path.join(home, "AppData", "Roaming")
        assert env["LOCALAPPDATA"] == os.path.join(home, "AppData", "Local")
        path_value = env.get("PATH") or env.get("Path")
    else:
        path_value = env["PATH"]
    shim_dir = os.path.join(launched.config_dir, "wrangler", "shim")
    assert path_value.split(os.pathsep)[0] == shim_dir
    assert "MSYS_NO_PATHCONV" not in env
    assert "CLAUDE_LOCAL_MSYS_ARGS" not in env
    return child


windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows shells")
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX sh")


@windows_only
@pytest.mark.parametrize("relative", [False, True])
def test_cmd_runs_the_cmd_shim(launched, relative):
    shim = (
        "bin\\claude-local.cmd"
        if relative
        else str(launched.project / "bin" / "claude-local.cmd")
    )
    completed = launched.run('cmd /d /s /c ""{}" "a b" c"'.format(shim))
    assert_ring1_launch(launched, completed, ["a b", "c"])


def test_probed_subcommands_still_get_the_settings_override(launched):
    """The install-time probe ran the real binary; mcp kept --settings."""

    from claude_wrangler.slots import RuntimeStore

    record = RuntimeStore(
        launched.project / ".local" / "claude" / "wrangler"
    ).slot_record("a")
    assert record is not None
    assert record.subcommands == [
        "doctor",
        "mcp",
        "plugin",
        "plugins",
        "update",
        "upgrade",
    ]
    assert record.settings_rejected == []
    if WINDOWS:
        completed = launched.run('cmd /d /s /c ""bin\\claude-local.cmd" mcp list"')
    else:
        completed = launched.run(["sh", "./bin/claude-local", "mcp", "list"])
    assert completed.returncode == 7, completed.stderr
    child = launched.child()
    assert child["argv"][0] == "--settings"
    assert json.loads(child["argv"][1])["env"]["DISABLE_UPDATES"] == "1"
    assert child["argv"][2:] == ["mcp", "list"]


@windows_only
@pytest.mark.parametrize("shell", ["pwsh", "powershell.exe"])
def test_powershell_resolves_the_cmd_shim(launched, shell):
    if shutil.which(shell) is None:
        pytest.skip("{} is not installed".format(shell))
    completed = launched.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "& '.\\bin\\claude-local' 'a b' c; exit $LASTEXITCODE",
        ]
    )
    assert_ring1_launch(launched, completed, ["a b", "c"])


@windows_only
def test_git_bash_runs_the_sh_shim_with_a_native_python(launched):
    bash = git_bash()
    if bash is None:
        pytest.skip("Git for Windows bash is not installed")
    completed = launched.run(
        [str(bash), "-c", '"$0" "$@"', "./bin/claude-local", "a b", "/review"]
    )
    assert_ring1_launch(launched, completed, ["a b", "/review"])


@windows_only
def test_path_shims_reenter_the_launcher(launched):
    shim_dir = launched.project / ".local" / "claude" / "wrangler" / "shim"
    completed = launched.run(
        'cmd /d /s /c ""{}" nested"'.format(shim_dir / "claude.cmd")
    )
    assert_ring1_launch(launched, completed, ["nested"])
    bash = git_bash()
    if bash is not None:
        completed = launched.run(
            [str(bash), "-c", '"$0" "$@"', (shim_dir / "claude").as_posix(), "nested"]
        )
        assert_ring1_launch(launched, completed, ["nested"])


@windows_only
def test_cmd_shim_ring0_keeps_home_but_pins_config_dir(launched):
    env = launched.env()
    completed = launched.run(
        'cmd /d /s /c ""bin\\claude-local.cmd" --shared-home x"', env=env
    )
    assert completed.returncode == 7, completed.stderr
    child = launched.child()
    assert child["argv"][2:] == ["x"]
    assert child["env"]["CLAUDE_CONFIG_DIR"] == launched.config_dir
    assert child["env"]["USERPROFILE"] == env["USERPROFILE"]
    assert child["env"]["CLAUDE_LOCAL_SHARED_HOME"] == "1"


@windows_only
def test_cmd_shim_intercepts_update_with_exit_2(launched):
    completed = launched.run('cmd /d /s /c ""bin\\claude-local.cmd" update"')
    assert completed.returncode == 2
    assert b"claude-wrangler --upgrade --channel stable" in completed.stderr
    assert not launched.report.exists()


@windows_only
def test_cmd_shim_falls_back_when_the_recorded_python_is_gone(launched):
    if shutil.which("py") is None and shutil.which("python") is None:
        pytest.skip("no fallback interpreter on PATH")
    meta_path = launched.project / ".local" / "claude" / "wrangler" / METADATA_FILENAME
    config = make_config(launched.project, "--set-update-checks", "off")
    from dataclasses import replace

    config = replace(config, launcher_python="C:\\missing\\python.exe")
    write_generated_files(config)
    assert "C:\\missing\\python.exe" in (
        launched.project / "bin" / "claude-local.cmd"
    ).read_text("ascii")
    completed = launched.run('cmd /d /s /c ""bin\\claude-local.cmd" fallback"')
    assert_ring1_launch(launched, completed, ["fallback"])
    assert meta_path.exists()


@posix_only
def test_sh_runs_the_posix_shim(launched):
    completed = launched.run(["sh", "./bin/claude-local", "a b", "c"])
    assert_ring1_launch(launched, completed, ["a b", "c"])
    completed = launched.run(["./bin/claude-local", "d"])
    assert_ring1_launch(launched, completed, ["d"])


@posix_only
def test_posix_path_shim_reenters_the_launcher(launched):
    shim = launched.project / ".local" / "claude" / "wrangler" / "shim" / "claude"
    completed = launched.run([str(shim), "nested"])
    assert_ring1_launch(launched, completed, ["nested"])


@posix_only
def test_posix_shim_falls_back_when_the_recorded_python_is_gone(launched):
    if shutil.which("python3") is None and shutil.which("python") is None:
        pytest.skip("no fallback interpreter on PATH")
    from dataclasses import replace

    config = replace(
        make_config(launched.project, "--set-update-checks", "off"),
        launcher_python="/nonexistent/python3",
    )
    write_generated_files(config)
    completed = launched.run(["sh", "./bin/claude-local", "fallback"])
    assert_ring1_launch(launched, completed, ["fallback"])


def posix_sh() -> Optional[str]:
    """Return a POSIX sh to syntax-check with (Git Bash's on Windows)."""

    if not WINDOWS:
        return shutil.which("sh")
    bash = git_bash()
    if bash is None:
        return None
    candidate = bash.parent / "sh.exe"
    return str(candidate) if candidate.is_file() else str(bash)


@pytest.mark.parametrize("launcher", ["bin/claude-local", "tools/x/claude-local"])
def test_generated_sh_shims_parse_in_posix_sh(tmp_path, launcher):
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh available")
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(project, "--launcher", launcher)
    for name, text in (
        ("claude-local", build_posix_shim(config)),
        ("claude", build_path_shim_sh(config)),
    ):
        path = tmp_path / name
        path.write_bytes(text.encode("utf-8"))
        completed = subprocess.run(
            [sh, "-n", path.as_posix() if WINDOWS else str(path)],
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")


def test_posix_shim_quotes_a_recorded_python_with_quotes(tmp_path):
    from dataclasses import replace

    project = tmp_path / "project"
    project.mkdir()
    config = replace(make_config(project), launcher_python="/opt/it's here/python3")
    text = build_posix_shim(config)
    expected = "claude_local_recorded='/opt/it'\\''s here/python3'"
    assert expected in text


# ---------------------------------------------------------------------------
# Shim robustness: CDPATH, quoting, rewrites during a session, modes
# ---------------------------------------------------------------------------


def sh_path(path: Path) -> str:
    """Return a path spelled for Git Bash's sh on Windows, or as-is."""

    if not WINDOWS:
        return str(path)
    text = str(path)
    drive, rest = os.path.splitdrive(text)
    return "/" + drive.rstrip(":").lower() + rest.replace("\\", "/")


NEWLINE = "\n"
STUB_PYTHON = """#!/bin/sh
if [ "$1" = "-I" ] && [ "$2" = "-c" ]; then
  exit 0
fi
printf 'ROOT=[%s]\\n' "$CLAUDE_LOCAL_REPO_ROOT"
printf 'CORE=[%s]\\n' "$2"
if [ -f "$2" ]; then
  echo CORE-EXISTS
fi
"""


def run_shim_with_stubs(
    tmp_path: Path,
    config,
    command: str,
    extra_env: Optional[Dict[str, str]] = None,
):
    """Generate the sh shims with a stub interpreter and run ``command``.

    The recorded interpreter is a stub that prints the repository root and
    core path it was given. On POSIX ``uname`` is stubbed to report Linux;
    Git for Windows' sh puts its own tools first, so there the shim takes
    its Git Bash branch and prints Windows-form paths (``cygpath -w``).
    """

    from dataclasses import replace

    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh available")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "uname").write_bytes(b"#!/bin/sh\necho Linux\n")
    stub_python = stubs / "python-stub"
    stub_python.write_bytes(STUB_PYTHON.encode("ascii"))
    for stub in (stubs / "uname", stub_python):
        stub.chmod(0o755)
    config = replace(config, launcher_python=sh_path(stub_python))
    project = config.project_root
    layout = config.layout
    layout.launch_core_path.parent.mkdir(parents=True, exist_ok=True)
    layout.launch_core_path.write_bytes(b"# stub core\n")
    layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)
    layout.launcher_path.write_bytes(build_posix_shim(config).encode("utf-8"))
    layout.shim_dir.mkdir(parents=True, exist_ok=True)
    (layout.shim_dir / "claude").write_bytes(build_path_shim_sh(config).encode("utf-8"))
    for path in (layout.launcher_path, layout.shim_dir / "claude"):
        path.chmod(0o755)
        syntax = subprocess.run(
            [sh, "-n", sh_path(path)], capture_output=True, timeout=60, check=False
        )
        assert syntax.returncode == 0, syntax.stderr.decode("utf-8", "replace")
    env = dict(os.environ)
    env.update(extra_env or {})
    # Git Bash converts a Windows-style PATH itself; the stubs go first.
    env["PATH"] = str(stubs) + os.pathsep + env.get("PATH", "")
    completed = subprocess.run(
        [sh, "-c", command],
        cwd=str(project),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


@pytest.mark.parametrize("cdpath", [".", ".:{other}"])
def test_posix_shim_ignores_an_exported_cdpath(tmp_path, cdpath):
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    (other / "bin").mkdir(parents=True)
    config = make_config(project)

    code, out, err = run_shim_with_stubs(
        tmp_path,
        config,
        "sh bin/claude-local hello",
        {"CDPATH": cdpath.format(other=sh_path(other))},
    )

    assert code == 0, err
    assert "ROOT=[{}]{}".format(project, NEWLINE) in out, out
    assert "CORE-EXISTS" in out, out


def test_sh_shims_keep_special_characters_in_managed_paths_literal(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    local_dir = "rt/q`echo INJECTED`$HOME z"
    config = make_config(project, "--local-dir", local_dir)
    assert "'{}/launch.py'".format(local_dir) in build_posix_shim(config)

    code, out, err = run_shim_with_stubs(tmp_path, config, "sh bin/claude-local hi")

    assert code == 0, err
    assert "INJECTED" not in err
    assert "CORE-EXISTS" in out, out
    assert "{}/launch.py".format(local_dir) in out.replace("\\", "/")


def test_path_shim_reaches_a_launcher_with_special_characters(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(project, "--launcher", "b$x y/claude-local")

    code, out, err = run_shim_with_stubs(
        tmp_path, config, "sh .local/claude/wrangler/shim/claude hello"
    )

    assert code == 0, err
    assert "ROOT=[{}]{}".format(project, NEWLINE) in out, out
    assert "CORE-EXISTS" in out, out


@pytest.mark.skipif(not WINDOWS, reason="cmd.exe batch semantics")
@pytest.mark.parametrize("change", ["same", "longer", "shorter"])
def test_cmd_shim_survives_being_rewritten_during_a_session(tmp_path, change):
    # cmd.exe re-reads a batch file by byte offset after each command; an
    # upgrade that rewrites the shim while Claude Code runs must not make it
    # resume in the middle of the new content or lose the exit code.
    from dataclasses import replace

    project = tmp_path / "project"
    project.mkdir()
    base = make_config(project)
    variants = {
        "same": base.launcher_python,
        "longer": base.launcher_python + "-" + "x" * 40,
        "shorter": "C:\\py.exe",
    }
    core = base.layout.launch_core_path
    core.parent.mkdir(parents=True)
    started = tmp_path / "core-started"
    core.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path({!r}).write_text('x')\n"
        "time.sleep(2)\n"
        "sys.exit(7)\n".format(str(started)),
        encoding="utf-8",
    )
    shim = base.layout.launcher_cmd_path
    shim.parent.mkdir(parents=True)
    shim.write_bytes(build_cmd_shim(base).replace("\n", "\r\n").encode("ascii"))
    replacement = build_cmd_shim(replace(base, launcher_python=variants[change]))

    process = subprocess.Popen(
        ["cmd", "/d", "/c", str(shim)],
        cwd=str(project),
        env=clean_launch_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Rewrite only once the launch line runs, as an upgrade during a
    # session would.
    deadline = time.monotonic() + 60
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists(), "the stub core never started"
    incoming = shim.with_name("incoming.cmd")
    incoming.write_bytes(replacement.replace("\n", "\r\n").encode("ascii"))
    os.replace(str(incoming), str(shim))
    stdout, stderr = process.communicate(timeout=60)

    assert process.returncode == 7, (stdout, stderr)
    assert b"is not recognized" not in stderr
    assert b"cannot find" not in stderr


@pytest.mark.skipif(WINDOWS, reason="POSIX execute permission")
def test_rewrite_restores_a_lost_execute_bit_on_the_path_shim(config):
    write_generated_files(config)
    shim = config.layout.shim_dir / "claude"
    shim.chmod(0o644)
    assert generated_file_status(config)["path_shim_sh"]["fresh"] is False

    write_generated_files(config)

    assert os.access(str(shim), os.X_OK)
    assert generated_file_status(config)["path_shim_sh"]["fresh"] is True


def test_unrenderable_cmd_files_raise_only_when_strict(tmp_path):
    from claude_wrangler.generator import planned_files

    project = tmp_path / "project"
    project.mkdir()
    config = make_config(project, "--local-dir", ".local/claude/wr\u00e4ngl\u00e9r")

    with pytest.raises(ClaudeWranglerError, match="not ASCII"):
        planned_files(config)
    keys = [item.key for item in planned_files(config, strict=False)]

    assert "launcher_cmd" not in keys
    assert "launcher_sh" in keys and "readme" in keys


def test_cmd_shims_double_percent_signs_in_managed_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(
        project, "--local-dir", "rt/100%done", "--launcher", "b%x%/claude-local"
    )

    launcher = build_cmd_shim(config)
    path_shim = build_path_shim_cmd(config)

    assert r"rt\100%%done\launch.py" in launcher
    assert r"b%%x%%\claude-local.cmd" in path_shim


@pytest.mark.skipif(WINDOWS, reason="Windows file names cannot contain quotes")
def test_cmd_shim_refuses_a_double_quote_in_a_managed_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = make_config(project, "--local-dir", 'rt/a"b')

    with pytest.raises(ClaudeWranglerError, match="double quote"):
        build_cmd_shim(config)
