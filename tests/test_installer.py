from pathlib import Path

from claude_wrangler.installer import (
    DEFAULT_LAUNCHER_NAME,
    build_bootstrap_command,
    build_install_command,
    build_package_spec,
    default_pyenv_root,
    ensure_launcher,
    managed_runtime_python,
    pyenv_python_path,
    remove_launcher_if_owned,
    select_install_python,
)


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


def test_ensure_launcher_replaces_existing_file(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher_path = bin_dir / "claude-wrangler"
    launcher_path.write_text("old\n", encoding="utf-8")
    target = tmp_path / "venv" / "bin" / "claude-wrangler"

    created = ensure_launcher(bin_dir, "claude-wrangler", target)

    assert created == launcher_path
    assert launcher_path.is_symlink()
    assert launcher_path.resolve() == target.resolve()


def test_remove_launcher_if_owned_only_unlinks_matching_symlink(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = tmp_path / "venv" / "bin" / "claude-wrangler"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    launcher_path = bin_dir / "claude-wrangler"
    launcher_path.symlink_to(target)

    removed = remove_launcher_if_owned(bin_dir, "claude-wrangler", target)

    assert removed is True
    assert not launcher_path.exists()


def test_remove_launcher_if_owned_keeps_unrelated_target(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    owned_target = tmp_path / "venv" / "bin" / "claude-wrangler"
    other_target = tmp_path / "other" / "bin" / "claude-wrangler"
    owned_target.parent.mkdir(parents=True)
    other_target.parent.mkdir(parents=True)
    owned_target.write_text("", encoding="utf-8")
    other_target.write_text("", encoding="utf-8")
    launcher_path = bin_dir / "claude-wrangler"
    launcher_path.symlink_to(other_target)

    removed = remove_launcher_if_owned(bin_dir, "claude-wrangler", owned_target)

    assert removed is False
    assert launcher_path.exists()
    assert launcher_path.resolve() == other_target.resolve()


def test_pyenv_python_path_uses_expected_layout(tmp_path):
    expected = tmp_path / "versions" / "3.12.12" / "bin" / "python"

    assert pyenv_python_path(tmp_path, "3.12.12") == expected


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
    runtime_python = tmp_path / ".pyenv" / "versions" / "3.12.12" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PYENV_ROOT", str(tmp_path / ".pyenv"))

    assert managed_runtime_python(tmp_path) == runtime_python


def test_select_install_python_prefers_explicit_override(tmp_path):
    explicit = "/tmp/custom-python"

    assert select_install_python(tmp_path, explicit) == explicit


def test_default_pyenv_root_uses_explicit_user_home(tmp_path):
    user_home = tmp_path / "operator"

    assert default_pyenv_root(user_home) == user_home / ".pyenv"


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
    runtime_python = user_home / ".pyenv" / "versions" / "3.12.12" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")

    assert select_install_python(tmp_path, None, user_home) == str(runtime_python)


def test_installer_main_rejects_repo_local_codex_home_without_override(
    tmp_path,
    monkeypatch,
    capsys,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / ".codex-home"
    repo_root.mkdir()
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))

    result = __import__("claude_wrangler.installer", fromlist=["main"]).main(
        ["--repo-root", str(repo_root)]
    )

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
    target_venv = operator_home / ".local" / "share" / DEFAULT_LAUNCHER_NAME / "venv"
    target_bin = operator_home / ".local" / "bin"
    repo_root.mkdir()
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))

    created_commands: list[list[str]] = []

    def fake_ensure_virtualenv(python_executable: str, venv_path: Path) -> Path:
        assert venv_path == target_venv
        venv_python = venv_path / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")
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

    monkeypatch.setattr(
        "claude_wrangler.installer.ensure_virtualenv", fake_ensure_virtualenv
    )
    monkeypatch.setattr(
        "claude_wrangler.installer.install_build_bootstrap",
        fake_install_build_bootstrap,
    )
    monkeypatch.setattr(
        "claude_wrangler.installer.build_install_command",
        fake_build_install_command,
    )
    monkeypatch.setattr("claude_wrangler.installer.subprocess.run", fake_subprocess_run)

    result = __import__("claude_wrangler.installer", fromlist=["main"]).main(
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
    assert "Launcher: {}".format(target_bin / DEFAULT_LAUNCHER_NAME) in captured.out
    assert created_commands == [
        [str(target_venv / "bin" / "python"), str(repo_root.resolve())]
    ]
