"""Tests for the dedicated user-venv installer and its launcher handling.

Layout expectations are explicit per operating system: POSIX venvs and
pyenv keep interpreters under `bin/`, Windows venvs under `Scripts\\`, and
pyenv-win directly under `versions\\<name>`. Launcher tests pass
`windows=True` or `windows=False` so both branches run on every host;
symlink-based tests skip where the host cannot create symlinks (Windows
without Developer Mode).
"""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import time

import pytest

import claude_wrangler.installer as installer_module
from claude_wrangler.installer import (
    DEFAULT_LAUNCHER_NAME,
    build_bootstrap_command,
    build_install_command,
    build_package_spec,
    cleanup_retired_launchers,
    default_pyenv_root,
    directory_on_path,
    ensure_launcher,
    installed_command_path,
    launcher_path,
    managed_runtime_python,
    path_hint,
    pyenv_python_path,
    remove_launcher_if_owned,
    retired_launcher_paths,
    select_install_python,
    venv_python_path,
)

# Interpreter location inside one pyenv version directory on this host.
PYENV_LAYOUT = ("python.exe",) if os.name == "nt" else ("bin", "python")

LAUNCHER_BYTES = b"MZ fake console launcher\n"
OTHER_LAUNCHER_BYTES = b"MZ another console launcher\n"


def symlinks_supported(directory: Path) -> bool:
    """Return True when this host can create a symlink inside `directory`."""

    probe = directory / ".symlink-probe"
    try:
        probe.symlink_to(directory / "missing-target")
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


@pytest.fixture
def require_symlinks(tmp_path):
    """Skip the test when the host cannot create symlinks."""

    if not symlinks_supported(tmp_path):
        pytest.skip("symlinks need Developer Mode or elevation on this host")


def write_launcher(path: Path, data: bytes = LAUNCHER_BYTES) -> Path:
    """Create a fake console launcher file, with parent directories."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_build_package_spec_without_dev_extra(tmp_path):
    assert build_package_spec(tmp_path, dev=False) == str(tmp_path.resolve())


def test_build_package_spec_with_dev_extra(tmp_path):
    expected = "{}[dev]".format(tmp_path.resolve())
    assert build_package_spec(tmp_path, dev=True) == expected


def test_build_install_command_for_normal_install(tmp_path):
    command = build_install_command(
        tmp_path / "venv" / "bin" / "python",
        tmp_path,
        editable=False,
        dev=False,
    )

    assert command[:5] == [
        str(tmp_path / "venv" / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "--upgrade",
    ]
    assert "--no-build-isolation" in command
    assert command[-1] == str(tmp_path.resolve())


def test_build_install_command_for_editable_dev_install(tmp_path):
    command = build_install_command(
        tmp_path / "venv" / "bin" / "python",
        tmp_path,
        editable=True,
        dev=True,
    )

    assert "--editable" in command
    assert command[-1] == "{}[dev]".format(tmp_path.resolve())


def test_build_bootstrap_command_uses_local_build_requirements(tmp_path):
    command = build_bootstrap_command(tmp_path / "venv" / "bin" / "python")

    assert command == [
        str(tmp_path / "venv" / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "setuptools>=69",
        "wheel",
    ]


def test_venv_paths_follow_the_requested_platform(tmp_path):
    venv = tmp_path / "venv"

    assert venv_python_path(venv, windows=True) == venv / "Scripts" / "python.exe"
    assert venv_python_path(venv, windows=False) == venv / "bin" / "python"
    assert installed_command_path(venv, windows=True) == (
        venv / "Scripts" / "claude-wrangler.exe"
    )
    assert installed_command_path(venv, windows=False) == (
        venv / "bin" / "claude-wrangler"
    )


def test_launcher_path_adds_exe_only_on_windows(tmp_path):
    assert launcher_path(tmp_path, "claude-wrangler", windows=False) == (
        tmp_path / "claude-wrangler"
    )
    assert launcher_path(tmp_path, "claude-wrangler", windows=True) == (
        tmp_path / "claude-wrangler.exe"
    )
    assert launcher_path(tmp_path, "tool.EXE", windows=True) == tmp_path / "tool.EXE"


def test_ensure_launcher_replaces_existing_file(tmp_path, require_symlinks):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "claude-wrangler"
    launcher.write_text("old\n", encoding="utf-8")
    target = tmp_path / "venv" / "bin" / "claude-wrangler"

    created = ensure_launcher(bin_dir, "claude-wrangler", target, windows=False)

    assert created == launcher
    assert launcher.is_symlink()
    # os.path.realpath follows the dangling link on every Python version;
    # Python 3.9's Path.resolve() does not on Windows.
    assert Path(os.path.realpath(str(launcher))) == Path(os.path.realpath(str(target)))


def test_ensure_launcher_copies_exe_on_windows(tmp_path):
    bin_dir = tmp_path / "bin"
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")

    created = ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

    assert created == bin_dir / "claude-wrangler.exe"
    assert not created.is_symlink()
    assert created.read_bytes() == LAUNCHER_BYTES
    assert sorted(entry.name for entry in bin_dir.iterdir()) == ["claude-wrangler.exe"]


def test_ensure_launcher_refreshes_changed_copy_on_windows(tmp_path):
    bin_dir = tmp_path / "bin"
    write_launcher(bin_dir / "claude-wrangler.exe", OTHER_LAUNCHER_BYTES)
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")

    created = ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

    assert created.read_bytes() == LAUNCHER_BYTES
    assert retired_launcher_paths(created) == []


def test_ensure_launcher_leaves_identical_copy_alone_on_windows(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    existing = write_launcher(bin_dir / "claude-wrangler.exe")
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")

    def refuse_replace(*args, **kwargs):
        raise AssertionError("an unchanged launcher must not be replaced")

    monkeypatch.setattr(installer_module.os, "replace", refuse_replace)

    assert ensure_launcher(bin_dir, "claude-wrangler", target, windows=True) == (
        existing
    )


def test_ensure_launcher_renames_busy_launcher_aside_on_windows(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    launcher = write_launcher(bin_dir / "claude-wrangler.exe", OTHER_LAUNCHER_BYTES)
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")
    real_replace = os.replace
    calls = []

    def busy_once(source, destination):
        """Refuse the first overwrite of the launcher, as a running .exe would."""

        calls.append((Path(source).name, Path(destination).name))
        if len(calls) == 1 and Path(destination) == launcher:
            raise PermissionError(13, "file is in use", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(installer_module.os, "replace", busy_once)

    ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

    assert launcher.read_bytes() == LAUNCHER_BYTES
    retired = retired_launcher_paths(launcher)
    assert len(retired) == 1
    assert retired[0].read_bytes() == OTHER_LAUNCHER_BYTES
    assert not (bin_dir / "claude-wrangler.exe.new").exists()


def test_ensure_launcher_restores_previous_launcher_when_swap_fails(
    tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    launcher = write_launcher(bin_dir / "claude-wrangler.exe", OTHER_LAUNCHER_BYTES)
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")
    real_replace = os.replace

    def always_busy(source, destination):
        """Refuse every write onto the launcher name except a restore."""

        if Path(destination) == launcher and Path(source).name.endswith(".new"):
            raise PermissionError(13, "file is in use", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(installer_module.os, "replace", always_busy)

    with pytest.raises(PermissionError):
        ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

    assert launcher.read_bytes() == OTHER_LAUNCHER_BYTES
    assert retired_launcher_paths(launcher) == []
    assert not (bin_dir / "claude-wrangler.exe.new").exists()


def test_ensure_launcher_cleans_up_retired_copies_on_windows(tmp_path):
    bin_dir = tmp_path / "bin"
    write_launcher(bin_dir / "claude-wrangler.exe.old-1234abcd")
    write_launcher(bin_dir / "claude-wrangler.exe.old-5678ef01")
    unrelated = write_launcher(bin_dir / "claude.exe.old-1234abcd")
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")

    created = ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

    assert retired_launcher_paths(created) == []
    assert unrelated.exists()


def test_cleanup_retired_launchers_keeps_copies_that_cannot_be_deleted(
    tmp_path, monkeypatch
):
    launcher = tmp_path / "claude-wrangler.exe"
    busy = write_launcher(tmp_path / "claude-wrangler.exe.old-busy")
    idle = write_launcher(tmp_path / "claude-wrangler.exe.old-idle")
    real_unlink = Path.unlink

    def unlink_unless_busy(self, *args, **kwargs):
        """Refuse to delete the busy copy, as Windows does for a running exe."""

        if self == busy:
            raise PermissionError(13, "file is in use", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_unless_busy)

    removed = cleanup_retired_launchers(launcher)

    assert removed == [idle]
    assert busy.exists()


def test_ensure_launcher_removes_legacy_symlink_on_windows(tmp_path, require_symlinks):
    bin_dir = tmp_path / "bin"
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")
    legacy = bin_dir / "claude-wrangler"
    bin_dir.mkdir()
    legacy.symlink_to(target)

    created = ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

    assert not legacy.is_symlink()
    assert created.read_bytes() == LAUNCHER_BYTES


@pytest.mark.skipif(os.name != "nt", reason="exercises a running Windows .exe")
def test_ensure_launcher_replaces_running_launcher_on_windows(tmp_path):
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    ping = system_root / "System32" / "PING.EXE"
    if not ping.is_file():
        pytest.skip("PING.EXE is not available")
    bin_dir = tmp_path / "bin"
    launcher = bin_dir / "claude-wrangler.exe"
    bin_dir.mkdir()
    shutil.copy2(str(ping), str(launcher))
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")
    running = subprocess.Popen(
        [str(launcher), "-n", "30", "127.0.0.1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.5)
        with pytest.raises(PermissionError):
            launcher.unlink()

        ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)

        assert launcher.read_bytes() == LAUNCHER_BYTES
        assert len(retired_launcher_paths(launcher)) == 1
    finally:
        running.kill()
        running.wait(timeout=30)

    # The image can stay mapped for a moment after the process exits.
    deadline = time.monotonic() + 10
    while retired_launcher_paths(launcher) and time.monotonic() < deadline:
        ensure_launcher(bin_dir, "claude-wrangler", target, windows=True)
        time.sleep(0.2)
    assert retired_launcher_paths(launcher) == []


def test_remove_launcher_if_owned_only_unlinks_matching_symlink(
    tmp_path, require_symlinks
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = tmp_path / "venv" / "bin" / "claude-wrangler"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    launcher = bin_dir / "claude-wrangler"
    launcher.symlink_to(target)

    removed = remove_launcher_if_owned(
        bin_dir, "claude-wrangler", target, windows=False
    )

    assert removed is True
    assert not launcher.exists()


def test_remove_launcher_if_owned_keeps_unrelated_target(tmp_path, require_symlinks):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    owned_target = tmp_path / "venv" / "bin" / "claude-wrangler"
    other_target = tmp_path / "other" / "bin" / "claude-wrangler"
    owned_target.parent.mkdir(parents=True)
    other_target.parent.mkdir(parents=True)
    owned_target.write_text("", encoding="utf-8")
    other_target.write_text("", encoding="utf-8")
    launcher = bin_dir / "claude-wrangler"
    launcher.symlink_to(other_target)

    removed = remove_launcher_if_owned(
        bin_dir, "claude-wrangler", owned_target, windows=False
    )

    assert removed is False
    assert launcher.exists()
    assert launcher.resolve() == other_target.resolve()


@pytest.mark.parametrize("windows", [False, True])
def test_remove_launcher_if_owned_removes_a_dangling_owned_symlink(
    tmp_path, require_symlinks, windows
):
    # Venv-only uninstall deletes the venv before the launcher: the owned
    # link then dangles, and it must still be recognized and removed.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = tmp_path / "venv" / "bin" / "claude-wrangler"
    launcher = bin_dir / "claude-wrangler"
    launcher.symlink_to(target)
    assert not target.exists()

    removed = remove_launcher_if_owned(
        bin_dir, "claude-wrangler", target, windows=windows
    )

    assert removed is True
    assert not os.path.lexists(str(launcher))


def test_remove_launcher_if_owned_removes_matching_copy_on_windows(tmp_path):
    bin_dir = tmp_path / "bin"
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")
    launcher = write_launcher(bin_dir / "claude-wrangler.exe")

    removed = remove_launcher_if_owned(bin_dir, "claude-wrangler", target, windows=True)

    assert removed is True
    assert not launcher.exists()


def test_remove_launcher_if_owned_keeps_foreign_copy_on_windows(tmp_path):
    bin_dir = tmp_path / "bin"
    target = write_launcher(tmp_path / "venv" / "Scripts" / "claude-wrangler.exe")
    launcher = write_launcher(bin_dir / "claude-wrangler.exe", OTHER_LAUNCHER_BYTES)

    removed = remove_launcher_if_owned(bin_dir, "claude-wrangler", target, windows=True)

    assert removed is False
    assert launcher.read_bytes() == OTHER_LAUNCHER_BYTES


@pytest.mark.parametrize(
    "windows, layout",
    [(True, ("python.exe",)), (False, ("bin", "python"))],
)
def test_pyenv_python_path_uses_expected_layout(tmp_path, windows, layout):
    expected = tmp_path.joinpath("versions", "3.12.12", *layout)

    assert pyenv_python_path(tmp_path, "3.12.12", windows=windows) == expected


def test_managed_runtime_python_reads_python_environments_json(tmp_path, monkeypatch):
    config = tmp_path / "python-environments.json"
    config.write_text(
        """{
  "runtime": {
    "environment_name": "3.12.12"
  }
}
""",
        encoding="utf-8",
    )
    runtime_python = tmp_path.joinpath(".pyenv", "versions", "3.12.12", *PYENV_LAYOUT)
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PYENV_ROOT", str(tmp_path / ".pyenv"))

    assert managed_runtime_python(tmp_path) == runtime_python.resolve()


@pytest.mark.skipif(os.name != "nt", reason="pyenv-win layout is Windows-only")
def test_managed_runtime_python_finds_nested_pyenv_win_root(tmp_path, monkeypatch):
    (tmp_path / "python-environments.json").write_text(
        '{"runtime": {"environment_name": "3.12.12"}}', encoding="utf-8"
    )
    runtime_python = tmp_path.joinpath(
        ".pyenv", "pyenv-win", "versions", "3.12.12", "python.exe"
    )
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PYENV_ROOT", str(tmp_path / ".pyenv"))

    assert managed_runtime_python(tmp_path) == runtime_python.resolve()


def test_select_install_python_prefers_explicit_override(tmp_path):
    explicit = "/tmp/custom-python"

    assert select_install_python(tmp_path, explicit) == explicit


def test_default_pyenv_root_uses_explicit_user_home(tmp_path):
    user_home = tmp_path / "operator"

    assert default_pyenv_root(user_home) == user_home.resolve() / ".pyenv"


def test_select_install_python_uses_explicit_user_home_for_managed_runtime(
    tmp_path,
):
    config = tmp_path / "python-environments.json"
    user_home = tmp_path / "operator"
    config.write_text(
        """{
  "runtime": {
    "environment_name": "3.12.12"
  }
}
""",
        encoding="utf-8",
    )
    runtime_python = user_home.joinpath(".pyenv", "versions", "3.12.12", *PYENV_LAYOUT)
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")

    assert select_install_python(tmp_path, None, user_home) == str(
        runtime_python.resolve()
    )


def test_installer_main_rejects_repo_local_codex_home_without_override(
    tmp_path,
    monkeypatch,
    capsys,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / ".codex-home"
    repo_root.mkdir()
    isolated_home.mkdir()
    # Windows resolves the home from USERPROFILE, not HOME; redirect both so
    # the guard sees the isolated home and nothing can reach a real profile.
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))

    def no_install(*args, **kwargs):
        raise AssertionError("the isolated-home guard must refuse first")

    monkeypatch.setattr(installer_module, "ensure_virtualenv", no_install)
    monkeypatch.setattr(installer_module, "install_build_bootstrap", no_install)
    monkeypatch.setattr(installer_module, "ensure_launcher", no_install)

    result = installer_module.main(["--repo-root", str(repo_root)])

    assert result == 1
    captured = capsys.readouterr()
    assert "--user-home" in captured.err
    assert "--allow-isolated-home" in captured.err


def test_installer_main_uses_explicit_user_home(
    tmp_path,
    monkeypatch,
    capsys,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / ".codex-home"
    operator_home = tmp_path / "operator"
    target_venv = (
        operator_home.resolve() / ".local" / "share" / DEFAULT_LAUNCHER_NAME / "venv"
    )
    target_bin = operator_home.resolve() / ".local" / "bin"
    repo_root.mkdir()
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))

    created_commands: list[list[str]] = []

    def fake_ensure_virtualenv(python_executable: str, venv_path: Path) -> Path:
        assert venv_path == target_venv
        venv_python = venv_path / "bin" / "python"
        write_launcher(venv_python)
        write_launcher(installed_command_path(venv_path))
        return venv_python

    def fake_install_build_bootstrap(venv_python: Path) -> None:
        assert venv_python == target_venv / "bin" / "python"

    def fake_build_install_command(
        python_executable: Path,
        candidate_repo_root: Path,
        *,
        editable: bool,
        dev: bool,
    ) -> list[str]:
        created_commands.append([str(python_executable), str(candidate_repo_root)])
        return [
            str(python_executable),
            "-m",
            "pip",
            "install",
            str(candidate_repo_root),
        ]

    def fake_subprocess_run(command: list[str], check: bool) -> None:
        assert check is True
        assert command[-1] == str(repo_root.resolve())

    monkeypatch.setattr(installer_module, "ensure_virtualenv", fake_ensure_virtualenv)
    monkeypatch.setattr(
        installer_module, "install_build_bootstrap", fake_install_build_bootstrap
    )
    monkeypatch.setattr(
        installer_module, "build_install_command", fake_build_install_command
    )
    monkeypatch.setattr(installer_module.subprocess, "run", fake_subprocess_run)

    result = installer_module.main(
        [
            "--repo-root",
            str(repo_root),
            "--user-home",
            str(operator_home),
        ]
    )

    assert result == 0
    captured = capsys.readouterr()
    assert "User home: {}".format(operator_home.resolve()) in captured.out
    assert "User home source: explicit --user-home target" in captured.out
    expected_launcher = launcher_path(target_bin, DEFAULT_LAUNCHER_NAME)
    assert "Launcher: {}".format(expected_launcher) in captured.out
    assert expected_launcher.exists()
    assert created_commands == [
        [str(target_venv / "bin" / "python"), str(repo_root.resolve())]
    ]


@pytest.mark.parametrize("windows", [True, False])
def test_path_hint_is_silent_when_bin_dir_is_on_path(windows):
    # Case, trailing separators, and surrounding entries must not matter.
    if windows:
        bin_dir = PureWindowsPath(r"C:\Users\Operator\.local\bin")
        environ = {"PATH": r"C:\Windows;c:\users\OPERATOR\.local\bin\;"}
    else:
        bin_dir = PurePosixPath("/home/operator/.local/bin")
        environ = {"PATH": "/usr/bin:/home/operator/.local/bin/:"}

    assert directory_on_path(bin_dir, environ=environ, windows=windows)
    assert path_hint(bin_dir, environ=environ, windows=windows) is None


def test_path_hint_prints_powershell_command_on_windows():
    bin_dir = PureWindowsPath(r"C:\Users\O'Brien\.local\bin")

    hint = path_hint(bin_dir, environ={"PATH": r"C:\Windows"}, windows=True)

    assert hint is not None
    assert "is not on PATH" in hint
    assert "[Environment]::SetEnvironmentVariable('Path'" in hint
    assert r"';C:\Users\O''Brien\.local\bin', 'User')" in hint
    assert "setx" not in hint


def test_path_hint_prints_export_line_on_posix():
    bin_dir = PurePosixPath("/home/operator/.local/bin")

    hint = path_hint(bin_dir, environ={"PATH": "/usr/bin:/bin"}, windows=False)

    assert hint is not None
    assert 'export PATH=/home/operator/.local/bin:"$PATH"' in hint
