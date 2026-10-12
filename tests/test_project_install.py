"""Tests for the repo-specific install hook that stage 2 runs.

Launcher targets are built with `installed_command_path`, so the tests
follow the host's venv layout (`bin/claude-wrangler` on POSIX,
`Scripts\\claude-wrangler.exe` on Windows), and launcher expectations
follow `ensure_launcher`: a symlink on POSIX and a copied `.exe` on
Windows. Symlink-based tests skip where the host cannot create symlinks.
"""

import os
from pathlib import Path

import pytest

import claude_wrangler.project_install as project_install_module
from claude_wrangler.installer import (
    ensure_launcher,
    installed_command_path,
    launcher_path,
)
from claude_wrangler.project_install import (
    build_project_install_command,
    manage_project_launcher,
    repo_venv_path,
    resolve_target_venv,
)

LAUNCHER_BYTES = b"MZ fake console launcher\n"


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


def write_command_target(venv_path: Path) -> Path:
    """Create the venv's console launcher at the host's layout."""

    target = installed_command_path(venv_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(LAUNCHER_BYTES)
    return target


def assert_launcher_targets(launcher: Path, target: Path) -> None:
    """Assert the published launcher runs `target` on this host's terms."""

    if os.name == "nt":
        assert not launcher.is_symlink()
        assert launcher.read_bytes() == target.read_bytes()
    else:
        assert launcher.is_symlink()
        assert launcher.resolve() == target.resolve()


def test_repo_venv_path_uses_python_parent_directories(tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    assert repo_venv_path(venv_python) == tmp_path / ".venv"


def test_repo_venv_path_uses_windows_scripts_layout(tmp_path):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"

    assert repo_venv_path(venv_python) == tmp_path / ".venv"


def test_repo_venv_path_preserves_symlinked_python_location(tmp_path, require_symlinks):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    base_python = tmp_path / "system" / "python3.13"
    venv_python.parent.mkdir(parents=True)
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")
    venv_python.symlink_to(base_python)

    assert repo_venv_path(venv_python) == tmp_path / ".venv"


def test_build_project_install_command_for_dev_mode(tmp_path):
    command = build_project_install_command(
        tmp_path / ".venv" / "bin" / "python",
        tmp_path,
        "dev",
    )

    assert "--editable" in command
    assert command[-1] == "{}[dev]".format(tmp_path.resolve())


def test_build_project_install_command_for_standard_mode(tmp_path):
    command = build_project_install_command(
        tmp_path / ".venv" / "bin" / "python",
        tmp_path,
        "standard",
    )

    assert "--editable" not in command
    assert command[-1] == str(tmp_path.resolve())


def test_resolve_target_venv_prefers_explicit_arg(tmp_path):
    class Args:
        venv = tmp_path / "custom" / "venv"

    venv_python = tmp_path / ".venv" / "bin" / "python"

    assert resolve_target_venv(Args(), venv_python) == Args.venv.resolve()


def test_manage_project_launcher_installs_dev_override(tmp_path):
    venv_path = tmp_path / ".venv"
    command_target = write_command_target(venv_path)
    bin_dir = tmp_path / "bin"

    launcher = manage_project_launcher(
        venv_path,
        mode="dev",
        scope="repo",
        bin_dir=bin_dir,
        launcher_name="claude-wrangler",
    )

    assert launcher == launcher_path(bin_dir, "claude-wrangler")
    assert_launcher_targets(launcher, command_target)


def test_manage_project_launcher_installs_user_standard_launcher(tmp_path):
    venv_path = tmp_path / "user" / "venv"
    command_target = write_command_target(venv_path)
    bin_dir = tmp_path / "bin"

    launcher = manage_project_launcher(
        venv_path,
        mode="standard",
        scope="user",
        bin_dir=bin_dir,
        launcher_name="claude-wrangler",
    )

    assert launcher == launcher_path(bin_dir, "claude-wrangler")
    assert_launcher_targets(launcher, command_target)


def test_manage_project_launcher_rejects_missing_target(tmp_path):
    with pytest.raises(RuntimeError, match="Missing installed launcher target"):
        manage_project_launcher(
            tmp_path / "user" / "venv",
            mode="standard",
            scope="user",
            bin_dir=tmp_path / "bin",
            launcher_name="claude-wrangler",
        )


def test_manage_project_launcher_removes_owned_dev_override(tmp_path):
    venv_path = tmp_path / ".venv"
    command_target = write_command_target(venv_path)
    bin_dir = tmp_path / "bin"
    launcher = ensure_launcher(bin_dir, "claude-wrangler", command_target)
    assert launcher.exists()

    result = manage_project_launcher(
        venv_path,
        mode="venv-only",
        scope="repo",
        bin_dir=bin_dir,
        launcher_name="claude-wrangler",
    )

    assert result is None
    assert not launcher.exists()
    assert not launcher.is_symlink()


def test_main_preserves_symlinked_venv_python_argument(
    tmp_path, monkeypatch, require_symlinks
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    venv_path = repo_root / ".venv"
    venv_python = venv_path / "bin" / "python"
    base_python = tmp_path / "system" / "python3.13"
    bin_dir = tmp_path / "bin"
    captured: dict[str, object] = {}

    venv_python.parent.mkdir(parents=True)
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")
    venv_python.symlink_to(base_python)

    def fake_install_project_package(
        candidate_python: Path,
        candidate_repo_root: Path,
        mode: str,
    ) -> None:
        captured["python"] = candidate_python
        captured["repo_root"] = candidate_repo_root
        captured["mode"] = mode

    def fake_manage_project_launcher(
        candidate_venv_path: Path,
        *,
        mode: str,
        scope: str,
        bin_dir: Path,
        launcher_name: str,
    ) -> Path:
        captured["venv"] = candidate_venv_path
        captured["bin_dir"] = bin_dir
        captured["launcher_name"] = launcher_name
        captured["scope"] = scope
        captured["launcher_mode"] = mode
        return bin_dir / launcher_name

    monkeypatch.setattr(
        project_install_module,
        "install_project_package",
        fake_install_project_package,
    )
    monkeypatch.setattr(
        project_install_module,
        "manage_project_launcher",
        fake_manage_project_launcher,
    )

    result = project_install_module.main(
        [
            "--mode",
            "standard",
            "--scope",
            "user",
            "--python",
            str(venv_python),
            "--venv",
            str(venv_path),
            "--repo-root",
            str(repo_root),
            "--bin-dir",
            str(bin_dir),
        ]
    )

    assert result == 0
    assert captured["python"] == venv_python
    assert captured["python"] != base_python
    assert captured["repo_root"] == repo_root.resolve()
    assert captured["venv"] == venv_path.resolve()
    assert captured["bin_dir"] == bin_dir.resolve()
    assert captured["mode"] == "standard"
    assert captured["scope"] == "user"


def _run_main_with_launcher(monkeypatch, tmp_path, launcher_step):
    """Run main() for a standard user install with a fake launcher step."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    venv_path = tmp_path / "venv"
    monkeypatch.setattr(
        project_install_module,
        "install_project_package",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        project_install_module, "manage_project_launcher", launcher_step
    )
    return project_install_module.main(
        [
            "--mode",
            "standard",
            "--scope",
            "user",
            "--python",
            str(venv_path / "Scripts" / "python.exe"),
            "--venv",
            str(venv_path),
            "--repo-root",
            str(repo_root),
            "--bin-dir",
            str(tmp_path / "bin"),
        ]
    )


def test_main_reports_launcher_os_error_as_failure(tmp_path, monkeypatch, capsys):
    def locked_launcher(*args, **kwargs):
        raise PermissionError(13, "launcher is locked", "claude-wrangler.exe")

    result = _run_main_with_launcher(monkeypatch, tmp_path, locked_launcher)

    assert result == 1
    captured = capsys.readouterr()
    assert "[install-project] FAIL:" in captured.err
    assert "launcher is locked" in captured.err


def test_main_prints_path_hint_when_bin_dir_is_not_on_path(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))

    def published(venv_path, *, mode, scope, bin_dir, launcher_name):
        return launcher_path(bin_dir, launcher_name)

    result = _run_main_with_launcher(monkeypatch, tmp_path, published)

    assert result == 0
    output = capsys.readouterr().out
    assert "PATH: {} is not on PATH".format((tmp_path / "bin").resolve()) in output


def test_main_prints_no_path_hint_when_bin_dir_is_on_path(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PATH", str((tmp_path / "bin").resolve()))

    def published(venv_path, *, mode, scope, bin_dir, launcher_name):
        return launcher_path(bin_dir, launcher_name)

    result = _run_main_with_launcher(monkeypatch, tmp_path, published)

    assert result == 0
    assert "is not on PATH" not in capsys.readouterr().out
