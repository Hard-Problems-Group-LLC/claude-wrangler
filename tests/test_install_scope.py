"""Tests for explicit user-home targeting and isolated-home rejection."""

import os
from pathlib import Path

import pytest

from claude_wrangler.constants import DEFAULT_HOME_DIR
from claude_wrangler.install_scope import (
    CODEX_HOME_DIRNAME,
    LEGACY_CODEX_HOME_DIRNAME,
    current_home,
    default_pyenv_root,
    default_tool_venv,
    default_user_bin_dir,
    describe_user_home_source,
    home_candidates,
    is_repo_local_isolated_home,
    resolve_user_home,
)


def test_default_user_paths_follow_selected_home(tmp_path):
    user_home = tmp_path / "operator"

    assert default_user_bin_dir(user_home) == user_home / ".local" / "bin"
    assert default_tool_venv(user_home, "claude-wrangler") == (
        user_home / ".local" / "share" / "claude-wrangler" / "venv"
    )
    assert default_pyenv_root(user_home) == user_home / ".pyenv"


def _expect_isolated_home_rejection(repo_root, **kwargs):
    with pytest.raises(RuntimeError) as excinfo:
        resolve_user_home(repo_root, **kwargs)

    message = str(excinfo.value)
    assert "--user-home" in message
    assert "--allow-isolated-home" in message
    return message


def _make_home(repo_root, relative):
    home = repo_root / relative
    home.mkdir(parents=True)
    return home


def _windows_home_env(home, **extra):
    """Return a native-Windows-style environment naming one home."""

    drive, tail = os.path.splitdrive(str(home))
    env = {"USERPROFILE": str(home), "HOMEDRIVE": drive, "HOMEPATH": tail}
    env.update(extra)
    return env


def test_codex_home_layouts_match_codex_wrangler():
    assert CODEX_HOME_DIRNAME == ".local/codex-home"
    assert LEGACY_CODEX_HOME_DIRNAME == ".codex-home"


@pytest.mark.parametrize(
    "relative",
    [DEFAULT_HOME_DIR, CODEX_HOME_DIRNAME, LEGACY_CODEX_HOME_DIRNAME],
)
def test_resolve_user_home_rejects_each_repo_local_home_via_home(
    tmp_path, monkeypatch, relative
):
    repo_root = tmp_path / "repo"
    isolated_home = _make_home(repo_root, relative)
    monkeypatch.setenv("HOME", str(isolated_home))

    message = _expect_isolated_home_rejection(repo_root)

    assert "HOME" in message


def test_resolve_user_home_rejects_repo_local_claude_home_by_default(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)
    monkeypatch.setenv("HOME", str(isolated_home))

    _expect_isolated_home_rejection(repo_root)


def test_resolve_user_home_rejects_repo_local_codex_home_by_default(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = _make_home(repo_root, CODEX_HOME_DIRNAME)
    monkeypatch.setenv("HOME", str(isolated_home))

    _expect_isolated_home_rejection(repo_root)


@pytest.mark.skipif(os.name != "nt", reason="USERPROFILE is a home only on Windows")
def test_resolve_user_home_rejects_isolated_userprofile_on_windows(
    tmp_path, monkeypatch
):
    repo_root = tmp_path / "repo"
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))

    message = _expect_isolated_home_rejection(repo_root)

    assert "USERPROFILE" in message


def test_resolve_user_home_accepts_explicit_override_from_isolated_home(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)
    operator_home = tmp_path / "operator"
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
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)
    monkeypatch.setenv("HOME", str(isolated_home))

    resolved = resolve_user_home(repo_root, allow_isolated_home=True)

    assert resolved == isolated_home.resolve()
    assert (
        describe_user_home_source(repo_root, resolved, None)
        == "current repo-local isolated home"
    )


def test_resolve_user_home_returns_current_home_when_not_isolated(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # The autouse conftest fixture points every home variable at a
    # per-test directory outside the repository.
    assert resolve_user_home(repo_root) == current_home()


def test_windows_rules_reject_isolated_userprofile_without_home(tmp_path):
    repo_root = tmp_path / "repo"
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)

    message = _expect_isolated_home_rejection(
        repo_root, environ=_windows_home_env(isolated_home), os_name="nt"
    )

    assert "USERPROFILE" in message


def test_windows_rules_reject_isolated_homedrive_homepath(tmp_path):
    repo_root = tmp_path / "repo"
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    isolated_home = _make_home(repo_root, CODEX_HOME_DIRNAME)
    drive, tail = os.path.splitdrive(str(isolated_home))
    environ = {
        "USERPROFILE": str(operator_home),
        "HOMEDRIVE": drive,
        "HOMEPATH": tail,
    }

    message = _expect_isolated_home_rejection(repo_root, environ=environ, os_name="nt")

    assert "HOMEDRIVE/HOMEPATH" in message


def test_windows_rules_reject_isolated_home_variable(tmp_path):
    repo_root = tmp_path / "repo"
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    isolated_home = _make_home(repo_root, LEGACY_CODEX_HOME_DIRNAME)
    environ = _windows_home_env(operator_home, HOME=str(isolated_home))

    _expect_isolated_home_rejection(repo_root, environ=environ, os_name="nt")


def test_windows_rules_opt_in_returns_isolated_home_not_real_profile(tmp_path):
    repo_root = tmp_path / "repo"
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)
    environ = _windows_home_env(operator_home, HOME=str(isolated_home))

    resolved = resolve_user_home(
        repo_root, allow_isolated_home=True, environ=environ, os_name="nt"
    )

    assert resolved == isolated_home.resolve()


def test_windows_current_home_prefers_userprofile_over_home(tmp_path):
    profile = tmp_path / "profile"
    git_bash_home = tmp_path / "git-bash-home"
    environ = _windows_home_env(profile, HOME=str(git_bash_home))

    assert current_home(environ, "nt") == profile.resolve()


def test_windows_current_home_falls_back_to_homedrive_homepath(tmp_path):
    profile = tmp_path / "profile"
    drive, tail = os.path.splitdrive(str(profile))
    environ = {"HOMEDRIVE": drive, "HOMEPATH": tail, "HOME": str(tmp_path / "x")}

    assert current_home(environ, "nt") == profile.resolve()


def test_windows_current_home_uses_home_when_no_native_variable(tmp_path):
    home = tmp_path / "home-only"

    assert current_home({"HOME": str(home)}, "nt") == home.resolve()


def test_posix_rules_ignore_windows_home_variables(tmp_path):
    repo_root = tmp_path / "repo"
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    isolated_home = _make_home(repo_root, DEFAULT_HOME_DIR)
    environ = _windows_home_env(isolated_home, HOME=str(operator_home))

    resolved = resolve_user_home(repo_root, environ=environ, os_name="posix")

    assert resolved == operator_home.resolve()


def test_home_candidates_report_each_location_once(tmp_path):
    profile = tmp_path / "profile"
    environ = _windows_home_env(profile, HOME=str(profile))

    candidates = home_candidates(environ, "nt")

    assert candidates == [("USERPROFILE", profile.resolve())]


def test_is_repo_local_isolated_home_matches_each_layout(tmp_path):
    repo_root = tmp_path / "repo"
    for relative in (DEFAULT_HOME_DIR, CODEX_HOME_DIRNAME, LEGACY_CODEX_HOME_DIRNAME):
        assert is_repo_local_isolated_home(repo_root / relative, repo_root)
    assert not is_repo_local_isolated_home(repo_root / ".local", repo_root)
    assert not is_repo_local_isolated_home(tmp_path / "operator", repo_root)


def test_autouse_fixture_redirects_every_home_variable(redirected_home):
    assert redirected_home is not None
    home = str(redirected_home)
    assert os.environ["HOME"] == home
    assert os.environ["USERPROFILE"] == home
    assert Path.home() == redirected_home
    for name in (
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
    ):
        assert Path(os.environ[name]).is_dir()
        assert os.path.commonpath([os.environ[name], home]) == home
    if os.path.splitdrive(home)[0]:
        assert os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"] == home


@pytest.mark.real_home_env
def test_real_home_env_marker_opts_out_of_redirection(redirected_home):
    assert redirected_home is None
