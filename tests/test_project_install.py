from pathlib import Path

import claude_wrangler.project_install as project_install_module
from claude_wrangler.project_install import (
    build_project_install_command,
    manage_project_launcher,
    repo_venv_path,
    resolve_target_venv,
)


def test_repo_venv_path_uses_python_parent_directories(tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    assert repo_venv_path(venv_python) == tmp_path / ".venv"


def test_repo_venv_path_preserves_symlinked_python_location(tmp_path):
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
    command_target = venv_path / "bin" / "claude-wrangler"
    bin_dir = tmp_path / "bin"
    command_target.parent.mkdir(parents=True)
    command_target.write_text("", encoding="utf-8")

    launcher_path = manage_project_launcher(
        venv_path,
        mode="dev",
        scope="repo",
        bin_dir=bin_dir,
        launcher_name="claude-wrangler",
    )

    assert launcher_path == bin_dir / "claude-wrangler"
    assert launcher_path.is_symlink()
    assert launcher_path.resolve() == command_target.resolve()


def test_manage_project_launcher_installs_user_standard_launcher(tmp_path):
    venv_path = tmp_path / "user" / "venv"
    command_target = venv_path / "bin" / "claude-wrangler"
    bin_dir = tmp_path / "bin"
    command_target.parent.mkdir(parents=True)
    command_target.write_text("", encoding="utf-8")

    launcher_path = manage_project_launcher(
        venv_path,
        mode="standard",
        scope="user",
        bin_dir=bin_dir,
        launcher_name="claude-wrangler",
    )

    assert launcher_path == bin_dir / "claude-wrangler"
    assert launcher_path.is_symlink()
    assert launcher_path.resolve() == command_target.resolve()


def test_manage_project_launcher_removes_owned_dev_override(tmp_path):
    venv_path = tmp_path / ".venv"
    command_target = venv_path / "bin" / "claude-wrangler"
    bin_dir = tmp_path / "bin"
    launcher_path = bin_dir / "claude-wrangler"
    command_target.parent.mkdir(parents=True)
    command_target.write_text("", encoding="utf-8")
    bin_dir.mkdir()
    launcher_path.symlink_to(command_target)

    result = manage_project_launcher(
        venv_path,
        mode="venv-only",
        scope="repo",
        bin_dir=bin_dir,
        launcher_name="claude-wrangler",
    )

    assert result is None
    assert not launcher_path.exists()


def test_main_preserves_symlinked_venv_python_argument(tmp_path, monkeypatch):
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
    assert captured["repo_root"] == repo_root
    assert captured["venv"] == venv_path
    assert captured["bin_dir"] == bin_dir
    assert captured["mode"] == "standard"
    assert captured["scope"] == "user"
