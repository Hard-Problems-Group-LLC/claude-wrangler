"""Tests for explicit user-home targeting and isolated-home rejection."""

from claude_wrangler.constants import DEFAULT_HOME_DIR
from claude_wrangler.install_scope import (
    CODEX_HOME_DIRNAME,
    default_pyenv_root,
    default_tool_venv,
    default_user_bin_dir,
    describe_user_home_source,
    resolve_user_home,
)


def test_default_user_paths_follow_selected_home(tmp_path):
    user_home = tmp_path / "operator"

    assert default_user_bin_dir(user_home) == user_home / ".local" / "bin"
    assert default_tool_venv(user_home, "claude-wrangler") == (
        user_home / ".local" / "share" / "claude-wrangler" / "venv"
    )
    assert default_pyenv_root(user_home) == user_home / ".pyenv"


def _expect_isolated_home_rejection(repo_root):
    try:
        resolve_user_home(repo_root)
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("Expected isolated-home resolution to fail.")

    assert "--user-home" in message
    assert "--allow-isolated-home" in message


def test_resolve_user_home_rejects_repo_local_claude_home_by_default(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / DEFAULT_HOME_DIR
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))

    _expect_isolated_home_rejection(repo_root)


def test_resolve_user_home_rejects_repo_local_codex_home_by_default(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / CODEX_HOME_DIRNAME
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))

    _expect_isolated_home_rejection(repo_root)


def test_resolve_user_home_accepts_explicit_override_from_isolated_home(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / DEFAULT_HOME_DIR
    operator_home = tmp_path / "operator"
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))

    resolved = resolve_user_home(repo_root, operator_home)

    assert resolved == operator_home.resolve()
    assert (
        describe_user_home_source(repo_root, resolved, operator_home)
        == "explicit --user-home target"
    )


def test_resolve_user_home_allows_explicit_isolated_home_opt_in(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = repo_root / DEFAULT_HOME_DIR
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))

    resolved = resolve_user_home(repo_root, allow_isolated_home=True)

    assert resolved == isolated_home.resolve()
    assert (
        describe_user_home_source(repo_root, resolved, None)
        == "current repo-local isolated home"
    )
