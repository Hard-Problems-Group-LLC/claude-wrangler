"""Tests for the standalone launcher core (cross_platform_launcher.md).

Most tests drive ``launcher_core.main`` with an injected ``os_name``,
environment, clock, network fetch, platform key, Node.js probe, and
exec/spawn doubles, so Windows and POSIX behavior are both covered on any
host. A few tests run the copied core in a real child interpreter.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

from claude_wrangler import launcher_core as core

CORE_SOURCE = Path(core.__file__)
KEY = "linux-x64"
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
SUPPRESSION = {
    "DISABLE_UPDATES": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_INSTALLATION_CHECKS": "1",
}
SETTINGS_OVERRIDE = {
    "env": dict(SUPPRESSION),
    "disableDeepLinkRegistration": "disable",
}
NT_ROOT = "C:\\Work Space\\repo"
POSIX_ROOT = "/work/space/repo"


def config_dict(**overrides: Any) -> Dict[str, Any]:
    """Return a valid launch.json document with optional overrides."""

    data: Dict[str, Any] = {
        "schema": 1,
        "repo_root_from_core": "../../..",
        "config_dir": ".local/claude",
        "home_dir": ".local/claude/HOME",
        "runtime_root": ".local/claude/wrangler",
        "shared_home_default": False,
        "reasonable_permissions": False,
        "windows_sandbox_warning": True,
        "update_checks": True,
        "channel": "stable",
        "suppression_env": dict(SUPPRESSION),
        "settings_override": json.loads(json.dumps(SETTINGS_OVERRIDE)),
        "wrangler_command": "claude-wrangler",
    }
    data.update(overrides)
    return data


class Project:
    """A fake checkout with a copied launcher core and runtime slots."""

    def __init__(self, root: Path) -> None:
        """Create the runtime root, copy the core, and write launch.json."""

        self.root = root
        self.runtime = root / ".local" / "claude" / "wrangler"
        self.runtime.mkdir(parents=True)
        self.core_path = self.runtime / "launch.py"
        shutil.copyfile(CORE_SOURCE, self.core_path)
        self.write_config()

    def write_config(self, **overrides: Any) -> None:
        """Write launch.json beside the core."""

        (self.runtime / "launch.json").write_text(
            json.dumps(config_dict(**overrides)), encoding="utf-8"
        )

    def install_slot(
        self,
        name: str,
        version: str,
        *,
        key: str = KEY,
        content: bytes = b"fake claude binary",
        subcommands: Optional[Sequence[str]] = ("mcp", "doctor", "plugin"),
        binary_name: str = "claude",
        record_overrides: Optional[Dict[str, Any]] = None,
        stamp: bool = True,
    ) -> Path:
        """Write a complete slot and return its binary path.

        With ``stamp`` the slot also gets the verification stamp maintenance
        writes after a full verification, so the launcher runs it.
        """

        slot = self.runtime / "slots" / name
        binary = slot / key / binary_name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(content)
        binary.chmod(0o755)
        record: Dict[str, Any] = {
            "schema": 1,
            "slot": name,
            "version": version,
            "channel": "stable",
            "platforms": {
                key: {
                    "binary": "{}/{}".format(key, binary_name),
                    "sha256": "0" * 64,
                    "size": len(content),
                }
            },
            "manifest_sha256": "0" * 64,
            "build_date": None,
            "commit": None,
            "schemes": ["openpgp", "raw-rsa"],
            "verified_at": "2026-10-01T00:00:00Z",
            "promoted_by": "test",
            "future_field": {"ignored": True},
        }
        if subcommands is not None:
            record["subcommands"] = list(subcommands)
        record.update(record_overrides or {})
        (slot / ".complete.json").write_text(json.dumps(record), encoding="utf-8")
        if stamp:
            self.stamp_slot(name, key, binary)
        return binary

    def stamp_slot(self, name: str, key: str, binary: Path) -> None:
        """Write the verification stamp for ``binary`` as it is now."""

        payload = {
            "schema": core.VERIFICATION_STAMP_SCHEMA,
            "binaries": {key: core._binary_identity(os.stat(str(binary)))},
        }
        (self.runtime / "slots" / name / core.VERIFICATION_STAMP_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def set_pointer(self, value: Any) -> None:
        """Write the active-slot pointer (str gets the LF appended)."""

        data = value if isinstance(value, bytes) else (value + "\n").encode()
        (self.runtime / "active-slot").write_bytes(data)

    def write_cache(self, **fields: Any) -> None:
        """Write update-check.json."""

        payload = {
            "checked_at": core.format_timestamp(NOW - timedelta(hours=1)),
            "channel": "stable",
            "latest_version": "2.1.250",
            "installed_version": "2.1.250",
        }
        payload.update(fields)
        (self.runtime / "update-check.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def read_json(self, name: str) -> Dict[str, Any]:
        """Read a JSON file from the runtime root."""

        return json.loads((self.runtime / name).read_text(encoding="utf-8"))

    def config_dir(self, os_name: str) -> str:
        """Return the CLAUDE_CONFIG_DIR spelling the core should export."""

        sep = "\\" if os_name == "nt" else "/"
        return str(self.root) + sep + ".local" + sep + "claude"


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """Return a project whose slot a (2.1.250) is complete and active."""

    result = Project(tmp_path / "repo")
    result.install_slot("a", "2.1.250")
    result.set_pointer("a")
    return result


@dataclass
class Outcome:
    """Everything observable about one ``main`` invocation."""

    code: int
    stderr: str
    stdout: str
    execs: List[Tuple[Any, ...]]
    spawns: List[Tuple[Any, ...]]
    fetches: List[str]

    @property
    def argv(self) -> List[str]:
        """Return the child argv from the exec or spawn double."""

        if self.execs:
            return list(self.execs[0][1])
        return list(self.spawns[0][0])

    @property
    def env(self) -> Dict[str, str]:
        """Return the child environment from the exec or spawn double."""

        if self.execs:
            return dict(self.execs[0][2])
        return dict(self.spawns[0][1])

    @property
    def launched(self) -> bool:
        """Return True when Claude Code would have been started."""

        return bool(self.execs or self.spawns)


def base_environ(os_name: str) -> Dict[str, str]:
    """Return an operator environment for the target OS."""

    if os_name == "nt":
        return {
            "PATH": "C:\\Windows\\system32;C:\\Tools",
            "USERPROFILE": "C:\\Users\\operator",
            "HOMEDRIVE": "C:",
            "HOMEPATH": "\\Users\\operator",
            "APPDATA": "C:\\Users\\operator\\AppData\\Roaming",
            "LOCALAPPDATA": "C:\\Users\\operator\\AppData\\Local",
        }
    return {"PATH": "/usr/local/bin:/usr/bin", "HOME": "/home/operator"}


def offline_fetch(calls: List[str]) -> Callable[[str, float], bytes]:
    """Return a fetch double that records URLs and fails like no network."""

    def fetch(url: str, timeout: float) -> bytes:
        """Record the URL and raise."""

        calls.append(url)
        raise OSError("network unreachable")

    return fetch


def run(
    project: Project,
    args: Sequence[str],
    *,
    os_name: str = "posix",
    environ: Optional[Dict[str, str]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    fetch: Optional[Callable[[str, float], bytes]] = None,
    now: datetime = NOW,
    node: Tuple[Optional[str], Optional[str]] = (None, None),
    wsl: bool = False,
    platform_key: str = KEY,
    exec_result: Any = 0,
    spawn_result: int = 0,
) -> Outcome:
    """Invoke ``core.main`` against ``project`` with recording doubles."""

    stderr = io.StringIO()
    stdout = io.StringIO()
    execs: List[Tuple[Any, ...]] = []
    spawns: List[Tuple[Any, ...]] = []
    fetches: List[str] = []

    def exec_double(path: str, argv: List[str], env: Dict[str, str]) -> Any:
        """Record an execve call."""

        execs.append((path, argv, env))
        return exec_result

    def spawn_double(argv: List[str], env: Dict[str, str]) -> int:
        """Record a Windows spawn."""

        spawns.append((argv, env))
        return spawn_result

    def fetch_wrapper(url: str, timeout: float) -> bytes:
        """Record the URL, then delegate or fail offline."""

        if fetch is None:
            return offline_fetch(fetches)(url, timeout)
        fetches.append(url)
        return fetch(url, timeout)

    env = dict(base_environ(os_name) if environ is None else environ)
    env.update(extra_env or {})
    code = core.main(
        list(args),
        environ=env,
        os_name=os_name,
        platform_is_wsl=wsl,
        core_path=str(project.core_path),
        exec_fn=exec_double,
        spawn_fn=spawn_double,
        stderr=stderr,
        stdout=stdout,
        fetch_fn=fetch_wrapper,
        now_fn=lambda: now,
        platform_key=platform_key,
        node_probe=lambda environ, os_name: node,
    )
    return Outcome(code, stderr.getvalue(), stdout.getvalue(), execs, spawns, fetches)


def load_config(**overrides: Any) -> core.LaunchConfig:
    """Parse a launch.json document into a LaunchConfig."""

    return core.parse_launch_config(config_dict(**overrides))


# ---------------------------------------------------------------------------
# Independence from the package
# ---------------------------------------------------------------------------


def imported_modules(source: str) -> List[Tuple[str, int]]:
    """Return ``(module, level)`` for every import statement in ``source``."""

    found: List[Tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_core_imports_only_the_standard_library() -> None:
    source = CORE_SOURCE.read_text(encoding="utf-8")
    modules = imported_modules(source)
    assert modules
    stdlib = getattr(sys, "stdlib_module_names", None)
    for module, level in modules:
        root = module.split(".")[0]
        assert level == 0, "relative import in launcher core"
        assert root != "claude_wrangler"
        if stdlib is not None:
            assert root in stdlib or root == "__future__", root


def test_core_source_parses_as_python_39() -> None:
    ast.parse(CORE_SOURCE.read_text(encoding="utf-8"), feature_version=(3, 9))


def test_copied_core_runs_in_an_isolated_interpreter(project: Project) -> None:
    env = dict(os.environ)
    env.pop(core.REPO_ROOT_VAR, None)
    completed = subprocess.run(
        [sys.executable, "-I", str(project.core_path), "upgrade"],
        cwd=str(project.root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    stderr = completed.stderr.decode("utf-8", "replace")
    assert "claude-wrangler --upgrade --channel stable" in stderr
    assert "Traceback" not in stderr


# ---------------------------------------------------------------------------
# launch.json
# ---------------------------------------------------------------------------


def test_parse_launch_config_accepts_contract_and_ignores_unknown_keys() -> None:
    config = core.parse_launch_config(config_dict(added_later={"x": 1}))
    assert config.repo_root_from_core == ("..", "..", "..")
    assert config.config_dir == (".local", "claude")
    assert config.home_dir == (".local", "claude", "HOME")
    assert config.runtime_root == (".local", "claude", "wrangler")
    assert config.channel == "stable"
    assert config.suppression_env == SUPPRESSION
    assert config.settings_override == SETTINGS_OVERRIDE
    assert config.wrangler_command == "claude-wrangler"
    assert config.update_checks is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": 2},
        {"schema": True},
        {"schema": "1"},
        {"config_dir": "/abs/claude"},
        {"config_dir": ".local\\claude"},
        {"config_dir": "../outside"},
        {"config_dir": "C:/claude"},
        {"config_dir": "."},
        {"home_dir": ""},
        {"runtime_root": "//server/share"},
        {"repo_root_from_core": "/"},
        {"shared_home_default": 0},
        {"update_checks": "true"},
        {"channel": "next"},
        {"channel": "stable/../../x"},
        {"wrangler_command": ""},
        {"suppression_env": {"DISABLE_UPDATES": "1"}},
        {"suppression_env": dict(SUPPRESSION, DISABLE_UPDATES="0")},
        {"suppression_env": dict(SUPPRESSION, **{"BAD NAME": "1"})},
        {"settings_override": {"disableDeepLinkRegistration": "disable"}},
        {"settings_override": []},
    ],
)
def test_parse_launch_config_rejects_contract_violations(
    overrides: Dict[str, Any],
) -> None:
    with pytest.raises(core.LaunchConfigError):
        core.parse_launch_config(config_dict(**overrides))


@pytest.mark.parametrize(
    "key", ["channel", "update_checks", "settings_override", "wrangler_command"]
)
def test_parse_launch_config_requires_every_contract_key(key: str) -> None:
    data = config_dict()
    del data[key]
    with pytest.raises(core.LaunchConfigError):
        core.parse_launch_config(data)


def test_main_reports_missing_and_malformed_launch_json(project: Project) -> None:
    (project.runtime / "launch.json").unlink()
    missing = run(project, [])
    assert missing.code == 1
    assert "launcher metadata is missing" in missing.stderr
    assert "--repair-install" in missing.stderr
    assert not missing.launched

    (project.runtime / "launch.json").write_text("{nope", encoding="utf-8")
    malformed = run(project, [])
    assert malformed.code == 1
    assert "not valid JSON" in malformed.stderr
    assert not malformed.launched


# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------


def same_directory_alias(path: Path) -> str:
    """Return a different spelling of ``path`` that names the same directory."""

    if os.name == "nt":
        alias = str(path).upper()
        if alias == str(path):
            pytest.skip("path has no lowercase characters to vary")
        return alias
    link = path.parent / "repo-alias"
    try:
        os.symlink(str(path), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    return str(link)


def test_repo_root_is_derived_from_core_location(project: Project) -> None:
    config = load_config()
    assert core.derive_repo_root(str(project.core_path), config) == str(project.root)


def test_repo_root_env_spelling_used_when_samefile(project: Project) -> None:
    derived = str(project.root)
    alias = same_directory_alias(project.root)
    for spelling in (alias, alias + os.sep):
        chosen = core.choose_repo_root(derived, {core.REPO_ROOT_VAR: spelling}, os.name)
        assert chosen == alias


def test_repo_root_env_spelling_reaches_config_dir(project: Project) -> None:
    alias = same_directory_alias(project.root)
    outcome = run(project, [], os_name=os.name, extra_env={core.REPO_ROOT_VAR: alias})
    assert outcome.launched
    expected = alias + os.sep + ".local" + os.sep + "claude"
    assert outcome.env["CLAUDE_CONFIG_DIR"] == expected


def test_repo_root_env_spelling_ignored_unless_safe_and_same(
    project: Project, tmp_path: Path
) -> None:
    derived = str(project.root)
    other = tmp_path / "other"
    other.mkdir()
    dotted = str(project.root / "sub" / "..")
    (project.root / "sub").mkdir()
    for candidate in (
        str(other),
        str(tmp_path / "missing"),
        "repo",
        dotted,
        "",
    ):
        chosen = core.choose_repo_root(
            derived, {core.REPO_ROOT_VAR: candidate}, os.name
        )
        assert chosen == derived, candidate


def test_clean_root_spelling_requires_drive_on_windows() -> None:
    assert core._clean_root_spelling("\\Users\\op\\repo", "nt") is None
    assert core._clean_root_spelling("C:repo", "nt") is None
    assert core._clean_root_spelling("C:\\Users\\op\\repo\\", "nt") == (
        "C:\\Users\\op\\repo"
    )
    assert core._clean_root_spelling("C:\\", "nt") == "C:\\"
    assert core._clean_root_spelling("\\\\srv\\share\\repo", "nt") == (
        "\\\\srv\\share\\repo"
    )
    assert core._clean_root_spelling("/srv/repo/", "posix") == "/srv/repo"
    assert core._clean_root_spelling("/srv/../repo", "posix") is None
    # A bare drive names drive C's current directory, which may well be the
    # repository; accepting it would pin CLAUDE_CONFIG_DIR to C:\.local.
    assert core._clean_root_spelling("C:", "nt") is None
    assert core._clean_root_spelling("C:/Users/op/repo", "nt") == "C:/Users/op/repo"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("C:\\x", True),
        ("c:/x", True),
        ("\\\\server\\share", True),
        ("//server/share/x", True),
        ("C:", False),
        ("C:x", False),
        ("\\x", False),
        ("x", False),
        ("", False),
    ],
)
def test_is_absolute_windows_path(value: str, expected: bool) -> None:
    assert core.is_absolute_windows_path(value) is expected


# ---------------------------------------------------------------------------
# Ring environments
# ---------------------------------------------------------------------------


def session_env(
    os_name: str, *, shared_home: bool, base: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Build a session environment for a fixed fake repository root."""

    root = NT_ROOT if os_name == "nt" else POSIX_ROOT
    config = load_config()
    paths = core.build_repo_paths(root, config, os_name)
    return core.build_session_environment(
        base_environ(os_name) if base is None else base,
        paths=paths,
        config=config,
        shared_home=shared_home,
        os_name=os_name,
    )


def test_windows_ring1_environment_table() -> None:
    env = session_env("nt", shared_home=False)
    home = NT_ROOT + "\\.local\\claude\\HOME"
    expected = {
        "HOME": home,
        "USERPROFILE": home,
        "HOMEDRIVE": "C:",
        "HOMEPATH": "\\Work Space\\repo\\.local\\claude\\HOME",
        "APPDATA": home + "\\AppData\\Roaming",
        "LOCALAPPDATA": home + "\\AppData\\Local",
        "XDG_CONFIG_HOME": home + "\\.config",
        "XDG_CACHE_HOME": home + "\\.cache",
        "XDG_STATE_HOME": home + "\\.local\\state",
        "XDG_DATA_HOME": home + "\\.local\\share",
        "CLAUDE_CONFIG_DIR": NT_ROOT + "\\.local\\claude",
        "CLAUDE_LOCAL_MANAGED_BY": "claude-wrangler",
        "DISABLE_UPDATES": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_INSTALLATION_CHECKS": "1",
        "PATH": NT_ROOT
        + "\\.local\\claude\\wrangler\\shim;C:\\Windows\\system32;C:\\Tools",
    }
    for name, value in expected.items():
        assert env[name] == value, name
    assert env["HOMEDRIVE"] + env["HOMEPATH"] == env["USERPROFILE"]


def test_windows_ring0_environment_keeps_operator_home() -> None:
    base = base_environ("nt")
    env = session_env("nt", shared_home=True, base=base)
    assert "HOME" not in env
    for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA"):
        assert env[name] == base[name]
    assert not any(name.startswith("XDG_") for name in env)
    assert env["CLAUDE_CONFIG_DIR"] == NT_ROOT + "\\.local\\claude"
    assert env["DISABLE_UPDATES"] == "1"


@pytest.mark.parametrize("shared_home", [False, True])
@pytest.mark.parametrize(
    "base",
    [
        base_environ("nt"),
        dict(base_environ("nt"), HOME="/c/Users/operator"),
        dict(base_environ("nt"), HOME="C:\\Users\\operator"),
        {"Path": "C:\\Windows"},
    ],
)
def test_windows_never_moves_home_without_windows_variables(
    shared_home: bool, base: Dict[str, str]
) -> None:
    env = session_env("nt", shared_home=shared_home, base=dict(base))
    if env.get("HOME") != base.get("HOME"):
        assert env["USERPROFILE"] == env["HOME"]
        assert env["HOMEDRIVE"] + env["HOMEPATH"] == env["HOME"]
        assert env["APPDATA"].startswith(env["HOME"] + "\\")
        assert env["LOCALAPPDATA"].startswith(env["HOME"] + "\\")
    else:
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            assert env.get(name) == base.get(name)


def test_windows_unc_root_splits_homedrive_and_homepath() -> None:
    config = load_config()
    paths = core.build_repo_paths("\\\\server\\share\\repo", config, "nt")
    values = core.redirected_home_environment(paths.home_dir, "nt")
    assert values["HOMEDRIVE"] == "\\\\server\\share"
    assert values["HOMEPATH"] == "\\repo\\.local\\claude\\HOME"


def test_posix_ring1_environment_table() -> None:
    env = session_env("posix", shared_home=False)
    home = POSIX_ROOT + "/.local/claude/HOME"
    assert env["HOME"] == home
    assert env["XDG_CONFIG_HOME"] == home + "/.config"
    assert env["XDG_CACHE_HOME"] == home + "/.cache"
    assert env["XDG_STATE_HOME"] == home + "/.local/state"
    assert env["XDG_DATA_HOME"] == home + "/.local/share"
    assert env["CLAUDE_CONFIG_DIR"] == POSIX_ROOT + "/.local/claude"
    assert (
        env["PATH"]
        == POSIX_ROOT + "/.local/claude/wrangler/shim:/usr/local/bin:/usr/bin"
    )
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"):
        assert name not in env


def test_posix_ring0_environment_keeps_operator_home() -> None:
    env = session_env("posix", shared_home=True)
    assert env["HOME"] == "/home/operator"
    assert not any(name.startswith("XDG_") for name in env)
    assert env["CLAUDE_CONFIG_DIR"] == POSIX_ROOT + "/.local/claude"
    assert env["CLAUDE_LOCAL_MANAGED_BY"] == "claude-wrangler"


def test_windows_path_variable_is_case_insensitive_and_deduplicated() -> None:
    shim = NT_ROOT + "\\.local\\claude\\wrangler\\shim"
    env = session_env(
        "nt", shared_home=False, base={"Path": shim.upper() + "\\;C:\\Windows"}
    )
    assert "Path" not in env
    assert env["PATH"] == shim + ";C:\\Windows"


def test_path_shim_added_when_path_missing_and_not_duplicated() -> None:
    env = session_env("posix", shared_home=False, base={})
    shim = POSIX_ROOT + "/.local/claude/wrangler/shim"
    assert env["PATH"] == shim
    again = session_env(
        "posix", shared_home=False, base={"PATH": env["PATH"] + ":/bin"}
    )
    assert again["PATH"] == shim + ":/bin"


def test_extra_suppression_variables_from_launch_json_are_exported() -> None:
    config = load_config(suppression_env=dict(SUPPRESSION, CLAUDE_EXTRA="1"))
    paths = core.build_repo_paths(POSIX_ROOT, config, "posix")
    env = core.build_session_environment(
        {}, paths=paths, config=config, shared_home=True, os_name="posix"
    )
    assert env["CLAUDE_EXTRA"] == "1"


@pytest.mark.parametrize(
    "default, env_value, override, expected",
    [
        (False, None, None, False),
        (True, None, None, True),
        (False, "1", None, True),
        (True, "0", None, False),
        (True, "", None, True),
        (False, "yes", None, False),
        (False, "0", True, True),
        (True, "1", False, False),
    ],
)
def test_ring_selection_matches_bash_semantics(
    default: bool, env_value: Optional[str], override: Optional[bool], expected: bool
) -> None:
    environ = {} if env_value is None else {core.SHARED_HOME_VAR: env_value}
    assert core.select_shared_home(default, environ, override, "posix") is expected


def test_launcher_flags_are_consumed_anywhere_and_last_ring_flag_wins() -> None:
    parsed = core.parse_launcher_arguments(
        ["--shared-home", "-p", "hi", "--check-updates", "--isolated-home"]
    )
    assert parsed.shared_home is False
    assert parsed.check_updates is True
    assert parsed.forwarded == ("-p", "hi")


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_main_ring1_redirects_home_and_creates_only_managed_dirs(
    project: Project, os_name: str
) -> None:
    outcome = run(project, ["-p", "hello"], os_name=os_name)
    assert outcome.code == 0
    assert "--isolated-home" not in outcome.argv
    sep = "\\" if os_name == "nt" else "/"
    assert outcome.env["HOME"] == project.config_dir(os_name) + sep + "HOME"
    if os_name == "nt":
        assert outcome.env["USERPROFILE"] == outcome.env["HOME"]
    assert outcome.env["CLAUDE_CONFIG_DIR"] == project.config_dir(os_name)
    assert outcome.env[core.SHARED_HOME_VAR] == "0"
    home = project.root / ".local" / "claude" / "HOME"
    assert home.is_dir()
    if os_name == "nt":
        # APPDATA and LOCALAPPDATA point here at ring 1, so both must exist.
        assert (home / "AppData" / "Roaming").is_dir()
        assert (home / "AppData" / "Local").is_dir()
        assert sorted(path.name for path in home.iterdir()) == ["AppData"]
    else:
        assert list(home.iterdir()) == []


@pytest.mark.parametrize("os_name", ["posix", "nt"])
@pytest.mark.parametrize(
    "args, extra_env",
    [(["--shared-home"], {}), ([], {core.SHARED_HOME_VAR: "1"})],
)
def test_main_ring0_keeps_home_but_pins_config_dir(
    project: Project, os_name: str, args: List[str], extra_env: Dict[str, str]
) -> None:
    outcome = run(project, args, os_name=os_name, extra_env=extra_env)
    base = base_environ(os_name)
    assert outcome.code == 0
    assert "--shared-home" not in outcome.argv
    assert outcome.env.get("HOME") == base.get("HOME")
    assert outcome.env.get("USERPROFILE") == base.get("USERPROFILE")
    assert outcome.env["CLAUDE_CONFIG_DIR"] == project.config_dir(os_name)
    assert outcome.env["DISABLE_UPDATES"] == "1"
    # Nested launches inherit the ring this session chose.
    assert outcome.env[core.SHARED_HOME_VAR] == "1"
    assert not (project.root / ".local" / "claude" / "HOME").exists()


# ---------------------------------------------------------------------------
# Project settings scan
# ---------------------------------------------------------------------------


def write_project_settings(root: Path, name: str, content: Any) -> None:
    """Write ``.claude/<name>`` with JSON content (or raw text)."""

    target = root / ".claude" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    target.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("value", ["0", "false", "", "no", False, 0, None, "2"])
def test_settings_scan_warns_on_falsy_suppression(tmp_path: Path, value: Any) -> None:
    write_project_settings(
        tmp_path, "settings.json", {"env": {"DISABLE_UPDATES": value}}
    )
    warnings = core.scan_project_settings(str(tmp_path), list(SUPPRESSION))
    assert len(warnings) == 1
    assert "DISABLE_UPDATES" in warnings[0]
    assert ".claude/settings.json" in warnings[0]


@pytest.mark.parametrize("value", ["1", "true", " YES ", "on", True, 1])
def test_settings_scan_accepts_truthy_suppression(tmp_path: Path, value: Any) -> None:
    write_project_settings(
        tmp_path, "settings.local.json", {"env": {"DISABLE_AUTOUPDATER": value}}
    )
    assert core.scan_project_settings(str(tmp_path), list(SUPPRESSION)) == []


def test_settings_scan_flags_nonessential_traffic_in_local_settings(
    tmp_path: Path,
) -> None:
    write_project_settings(
        tmp_path,
        "settings.local.json",
        {"env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}},
    )
    warnings = core.scan_project_settings(str(tmp_path), list(SUPPRESSION))
    assert len(warnings) == 1
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in warnings[0]
    assert ".claude/settings.local.json" in warnings[0]


@pytest.mark.parametrize(
    "content, fragment",
    [
        ("{not json", "not valid JSON"),
        ("[1, 2]", "top level is not an object"),
        ({"env": ["DISABLE_UPDATES=0"]}, "env is not an object"),
    ],
)
def test_settings_scan_reports_malformed_files(
    tmp_path: Path, content: Any, fragment: str
) -> None:
    write_project_settings(tmp_path, "settings.json", content)
    warnings = core.scan_project_settings(str(tmp_path), list(SUPPRESSION))
    assert len(warnings) == 1
    assert fragment in warnings[0]


def test_settings_scan_is_quiet_without_files(tmp_path: Path) -> None:
    write_project_settings(tmp_path, "settings.json", {"permissions": {}})
    assert core.scan_project_settings(str(tmp_path), list(SUPPRESSION)) == []


def test_main_warns_about_settings_but_still_launches(project: Project) -> None:
    write_project_settings(
        project.root, "settings.json", {"env": {"DISABLE_INSTALLATION_CHECKS": "0"}}
    )
    write_project_settings(project.root, "settings.local.json", "{broken")
    outcome = run(project, [])
    assert outcome.launched
    assert "DISABLE_INSTALLATION_CHECKS" in outcome.stderr
    assert ".claude/settings.local.json is not valid JSON" in outcome.stderr


# ---------------------------------------------------------------------------
# Interception and --settings refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", ["update", "upgrade", "install"])
@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_self_update_subcommands_are_intercepted(
    tmp_path: Path, subcommand: str, os_name: str
) -> None:
    bare = Project(tmp_path / "repo")
    bare.write_config(channel="latest")
    outcome = run(bare, ["--isolated-home", subcommand, "stable"], os_name=os_name)
    assert outcome.code == 2
    assert not outcome.launched
    assert "managed by claude-wrangler" in outcome.stderr
    assert "`claude-wrangler --upgrade --channel latest`" in outcome.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--verbose", "update"],
        ["-d", "install"],
        ["--model", "x", "upgrade"],
        ["--", "update"],
        ["-p", "install"],
        ["--isolated-home", "--verbose", "install", "stable"],
    ],
)
@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_self_update_subcommands_behind_leading_options_are_intercepted(
    project: Project, args: List[str], os_name: str
) -> None:
    # Claude Code dispatches a subcommand that follows root options or "--".
    outcome = run(project, args, os_name=os_name)
    assert outcome.code == 2
    assert not outcome.launched
    assert "managed by claude-wrangler" in outcome.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "please install the linter"],
        ["plugin", "install", "formatter"],
        ["--verbose", "mcp", "add", "update-helper"],
        ["--model", "x", "plugin", "install", "y"],
    ],
)
def test_words_inside_prompts_and_subcommand_operands_are_forwarded(
    project: Project, args: List[str]
) -> None:
    project.install_slot(
        "a", "2.1.250", subcommands=("mcp", "plugin", "plugins", "install", "update")
    )
    outcome = run(project, args)
    assert outcome.code == 0, outcome.stderr
    assert outcome.argv[-len(args) :] == args


def test_intercepted_subcommand_stops_at_another_subcommand() -> None:
    subs = ("mcp", "plugin", "update", "install")
    assert core.intercepted_subcommand(["plugin", "install", "x"], subs) is None
    assert core.intercepted_subcommand(["--verbose", "update"], subs) == "update"
    assert core.intercepted_subcommand(["x", "--", "upgrade"], ()) == "upgrade"


def test_unrecorded_subcommands_fall_back_to_the_built_in_stops() -> None:
    # The npm-era layout records no subcommands; plugin and MCP operands
    # still pass there, and self-update behind options is still refused.
    assert core.intercepted_subcommand(["plugin", "install", "x"], ()) is None
    assert core.intercepted_subcommand(["plugins", "install", "x"], ()) is None
    assert core.intercepted_subcommand(["mcp", "update"], ()) is None
    assert core.intercepted_subcommand(["--verbose", "update"], ()) == "update"
    assert core.intercepted_subcommand(["-d", "install"], ()) == "install"
    # A recorded list replaces the fallback entirely.
    assert core.intercepted_subcommand(["mcp", "update"], ("plugin",)) == "update"


@pytest.mark.parametrize(
    "args",
    [["--settings", "{}"], ["--settings=x.json"], ["-p", "hi", "--settings", "x"]],
)
@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_caller_settings_flag_is_refused(
    project: Project, args: List[str], os_name: str
) -> None:
    outcome = run(project, args, os_name=os_name)
    assert outcome.code == 2
    assert not outcome.launched
    sep = "\\" if os_name == "nt" else "/"
    assert project.config_dir(os_name) + sep + "settings.json" in outcome.stderr


# ---------------------------------------------------------------------------
# Claude Code argument construction
# ---------------------------------------------------------------------------


def test_main_command_gets_compact_settings_override_first(project: Project) -> None:
    outcome = run(project, ["-p", "hi"])
    argv = outcome.argv
    assert argv[0] == outcome.execs[0][0]
    assert argv[1] == "--settings"
    assert argv[2] == json.dumps(
        SETTINGS_OVERRIDE, separators=(",", ":"), sort_keys=True
    )
    assert json.loads(argv[2]) == SETTINGS_OVERRIDE
    assert argv[3:] == ["-p", "hi"]


def test_recorded_subcommand_omits_settings_override(project: Project) -> None:
    # A slot promoted before the install-time --settings probe existed.
    outcome = run(project, ["mcp", "list"])
    assert outcome.argv[1:] == ["mcp", "list"]


def test_probed_subcommands_keep_settings_unless_they_rejected_it(
    tmp_path: Path,
) -> None:
    bare = Project(tmp_path / "repo")
    bare.install_slot(
        "a",
        "2.1.250",
        subcommands=("mcp", "doctor", "plugin"),
        record_overrides={"settings_rejected": ["doctor"]},
    )
    bare.set_pointer("a")
    accepted = run(bare, ["mcp", "list"])
    assert accepted.argv[1] == "--settings"
    assert json.loads(accepted.argv[2]) == SETTINGS_OVERRIDE
    assert accepted.argv[3:] == ["mcp", "list"]
    rejected = run(bare, ["doctor"])
    assert rejected.argv[1:] == ["doctor"]
    main_command = run(bare, ["-p", "hi"])
    assert main_command.argv[1] == "--settings"


@pytest.mark.parametrize("value", [None, "doctor", [1], {"doctor": True}])
def test_unusable_settings_rejected_falls_back_to_omitting(
    tmp_path: Path, value: Any
) -> None:
    bare = Project(tmp_path / "repo")
    overrides = {} if value is None else {"settings_rejected": value}
    bare.install_slot("a", "2.1.250", record_overrides=overrides)
    bare.set_pointer("a")
    assert (
        core.record_settings_rejected(
            json.loads((bare.runtime / "slots" / "a" / ".complete.json").read_text())
        )
        is None
    )
    assert run(bare, ["mcp", "list"]).argv[1:] == ["mcp", "list"]


def test_build_claude_arguments_honors_the_probe_result() -> None:
    def build(args: List[str], rejected: Optional[List[str]]) -> List[str]:
        """Build arguments for a slot with three recorded subcommands."""

        return core.build_claude_arguments(
            args,
            subcommands=("mcp", "doctor", "plugin"),
            settings_override={"env": {}},
            reasonable_permissions=False,
            settings_rejected=rejected,
        )

    assert build(["mcp"], None) == ["mcp"]
    assert build(["mcp"], [])[0] == "--settings"
    assert build(["mcp"], ["mcp"]) == ["mcp"]
    assert build(["plugin", "list"], ["mcp"])[0] == "--settings"
    assert build([], ["mcp"])[0] == "--settings"


def test_missing_subcommand_list_treats_everything_as_main_command(
    tmp_path: Path,
) -> None:
    bare = Project(tmp_path / "repo")
    bare.install_slot("a", "2.1.250", subcommands=None)
    bare.set_pointer("a")
    outcome = run(bare, ["mcp", "list"])
    assert outcome.argv[1] == "--settings"
    assert outcome.argv[3:] == ["mcp", "list"]


def test_no_arguments_still_gets_settings_override(project: Project) -> None:
    assert run(project, []).argv[1:] == [
        "--settings",
        json.dumps(SETTINGS_OVERRIDE, separators=(",", ":"), sort_keys=True),
    ]


def test_reasonable_permissions_default_is_injected(project: Project) -> None:
    project.write_config(reasonable_permissions=True)
    argv = run(project, ["-c"]).argv
    assert argv[3:] == ["--permission-mode", "acceptEdits", "-c"]


@pytest.mark.parametrize(
    "caller",
    [
        ["--permission-mode", "plan"],
        ["--permission-mode=plan"],
        ["--dangerously-skip-permissions"],
        ["-p", "x", "--permission-mode", "default"],
    ],
)
def test_explicit_permission_choice_suppresses_default(
    project: Project, caller: List[str]
) -> None:
    project.write_config(reasonable_permissions=True)
    argv = run(project, caller).argv
    assert "acceptEdits" not in argv
    assert argv[3:] == caller


def test_permissions_default_off_never_injects(project: Project) -> None:
    assert "--permission-mode" not in run(project, ["-c"]).argv


# ---------------------------------------------------------------------------
# Slot selection, fallback, and refusal
# ---------------------------------------------------------------------------


def test_active_slot_runs_without_fallback(project: Project) -> None:
    project.install_slot("b", "2.1.240")
    outcome = run(project, [])
    assert outcome.execs[0][0] == str(project.runtime / "slots" / "a" / KEY / "claude")
    assert "WARNING" not in outcome.stderr
    assert not (project.runtime / "fallback.json").exists()


@pytest.mark.parametrize(
    "damage, reason_fragment, failed_version",
    [
        ("missing", "binary is missing", "2.1.250"),
        ("truncated", "bytes but the record says", "2.1.250"),
        ("corrupt", "not valid JSON", None),
        ("wrong-slot", "names slot 'b'", "2.1.250"),
        ("wrong-platform", "no linux-x64 binary", "2.1.250"),
    ],
)
def test_failed_active_slot_falls_back_with_warning_and_record(
    project: Project, damage: str, reason_fragment: str, failed_version: Optional[str]
) -> None:
    project.install_slot("b", "2.1.240")
    slot_a = project.runtime / "slots" / "a"
    if damage == "missing":
        (slot_a / KEY / "claude").unlink()
    elif damage == "truncated":
        (slot_a / KEY / "claude").write_bytes(b"short")
    elif damage == "corrupt":
        (slot_a / ".complete.json").write_text("{corrupt", encoding="utf-8")
    elif damage == "wrong-slot":
        project.install_slot("a", "2.1.250", record_overrides={"slot": "b"})
    else:
        project.install_slot("a", "2.1.250", key="darwin-arm64")
        shutil.rmtree(str(slot_a / KEY))
        record = json.loads((slot_a / ".complete.json").read_text(encoding="utf-8"))
        assert KEY not in record["platforms"]

    outcome = run(project, ["-p", "x"])

    assert outcome.code == 0
    assert outcome.execs[0][0] == str(project.runtime / "slots" / "b" / KEY / "claude")
    assert "failed its launch check" in outcome.stderr
    assert reason_fragment in outcome.stderr
    assert (
        "Claude Code {}".format(failed_version or "unknown version") in outcome.stderr
    )
    assert "Falling back to slot b (Claude Code 2.1.240)" in outcome.stderr
    assert "claude-wrangler --repair-install" in outcome.stderr
    record = project.read_json("fallback.json")
    assert record["at"] == "2026-10-12T12:00:00Z"
    assert record["from_slot"] == "a"
    assert record["to_slot"] == "b"
    assert reason_fragment in record["reason"]


def test_refuses_when_neither_slot_passes(project: Project) -> None:
    (project.runtime / "slots" / "a" / KEY / "claude").unlink()
    outcome = run(project, [])
    assert outcome.code == 1
    assert not outcome.launched
    assert "no usable Claude Code runtime" in outcome.stderr
    assert "slot a (active): binary is missing" in outcome.stderr
    assert "slot b: completion record is missing" in outcome.stderr
    assert "claude-wrangler --repair-install" in outcome.stderr
    assert "never falls back to a global claude" in outcome.stderr


@pytest.mark.parametrize("pointer", [None, b"b", b"b\r\n", b"c\n"])
def test_invalid_pointer_runs_the_only_complete_slot(
    tmp_path: Path, pointer: Optional[bytes]
) -> None:
    bare = Project(tmp_path / "repo")
    bare.install_slot("b", "2.1.240")
    if pointer is not None:
        bare.set_pointer(pointer)
    outcome = run(bare, [])
    assert outcome.code == 0
    assert outcome.execs[0][0] == str(bare.runtime / "slots" / "b" / KEY / "claude")
    assert "active-slot pointer" in outcome.stderr
    record = bare.read_json("fallback.json")
    assert record["from_slot"] is None
    assert record["to_slot"] == "b"


def test_invalid_pointer_with_two_complete_slots_is_refused(tmp_path: Path) -> None:
    bare = Project(tmp_path / "repo")
    bare.install_slot("a", "2.1.250")
    bare.install_slot("b", "2.1.240")
    outcome = run(bare, [])
    assert outcome.code == 1
    assert not outcome.launched
    assert "cannot be determined" in outcome.stderr


def test_uninstalled_runtime_is_refused(tmp_path: Path) -> None:
    outcome = run(Project(tmp_path / "repo"), [])
    assert outcome.code == 1
    assert "the active-slot pointer is missing" in outcome.stderr
    assert "slot a: completion record is missing" in outcome.stderr


@pytest.mark.parametrize(
    "binary_path", ["../../../outside/claude", "/usr/bin/claude", "C:/claude.exe"]
)
def test_recorded_binary_path_cannot_escape(project: Project, binary_path: str) -> None:
    record_path = project.runtime / "slots" / "a" / ".complete.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["platforms"][KEY]["binary"] = binary_path
    record_path.write_text(json.dumps(record), encoding="utf-8")
    check = core.check_slot(str(project.runtime), "a", KEY)
    assert not check.ok
    assert "not a safe relative path" in check.reason
    assert run(project, []).code == 1


def test_symlinked_binary_outside_runtime_root_is_refused(
    project: Project, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-claude"
    binary = project.runtime / "slots" / "a" / KEY / "claude"
    outside.write_bytes(binary.read_bytes())
    outside.chmod(0o755)
    binary.unlink()
    try:
        os.symlink(str(outside), str(binary))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    check = core.check_slot(str(project.runtime), "a", KEY)
    assert not check.ok
    assert "outside the runtime root" in check.reason
    outcome = run(project, [])
    assert outcome.code == 1
    assert not outcome.launched


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bit")
def test_non_executable_binary_fails_the_check(project: Project) -> None:
    (project.runtime / "slots" / "a" / KEY / "claude").chmod(0o644)
    check = core.check_slot(str(project.runtime), "a", KEY)
    assert not check.ok
    assert "not executable" in check.reason


def test_completion_record_unknown_keys_are_accepted(project: Project) -> None:
    check = core.check_slot(str(project.runtime), "a", KEY)
    assert check.ok
    assert check.version == "2.1.250"
    assert check.record is not None and "future_field" in check.record


# ---------------------------------------------------------------------------
# Host platform key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "system, machine, extra, expected",
    [
        ("Windows", "AMD64", {}, "win32-x64"),
        ("Windows", "AMD64", {"windows_native_machine": "arm64"}, "win32-arm64"),
        ("Windows", "ARM64", {}, "win32-arm64"),
        ("Darwin", "arm64", {}, "darwin-arm64"),
        ("Darwin", "x86_64", {}, "darwin-x64"),
        ("Darwin", "x86_64", {"rosetta_translated": True}, "darwin-arm64"),
        ("Linux", "x86_64", {}, "linux-x64"),
        ("Linux", "aarch64", {}, "linux-arm64"),
        ("Linux", "x86_64", {"musl": True}, "linux-x64-musl"),
        ("Linux", "aarch64", {"musl": True}, "linux-arm64-musl"),
    ],
)
def test_platform_key_from_facts(
    system: str, machine: str, extra: Dict[str, Any], expected: str
) -> None:
    assert core.platform_key_from_facts(system, machine, **extra) == expected


@pytest.mark.parametrize(
    "system, machine, extra",
    [
        ("Windows", "AMD64", {"windows_native_machine": "x86"}),
        ("Linux", "armv7l", {}),
        ("FreeBSD", "amd64", {}),
    ],
)
def test_platform_key_from_facts_rejects_unsupported_hosts(
    system: str, machine: str, extra: Dict[str, Any]
) -> None:
    with pytest.raises(core.UnsupportedHostError):
        core.platform_key_from_facts(system, machine, **extra)


def test_detect_host_platform_key_on_this_host() -> None:
    key = core.detect_host_platform_key()
    expected_prefix = {"win32": "win32-", "darwin": "darwin-"}.get(
        sys.platform, "linux-"
    )
    assert key.startswith(expected_prefix)


# ---------------------------------------------------------------------------
# Update checks
# ---------------------------------------------------------------------------


def pointer_fetch(version: bytes) -> Callable[[str, float], bytes]:
    """Return a fetch double that answers every URL with ``version``."""

    return lambda url, timeout: version


def test_fresh_cache_with_newer_version_prints_one_notice(project: Project) -> None:
    project.write_cache(latest_version="2.1.300")
    outcome = run(project, [])
    notices = [line for line in outcome.stderr.splitlines() if "available" in line]
    assert len(notices) == 1
    assert "2.1.300" in notices[0]
    assert "`claude-wrangler --upgrade --channel stable`" in notices[0]
    assert outcome.fetches == []
    assert outcome.launched


@pytest.mark.parametrize("latest", ["2.1.250", "2.1.9", None, "garbage"])
def test_no_notice_when_cache_is_not_newer(project: Project, latest: Any) -> None:
    project.write_cache(latest_version=latest)
    outcome = run(project, [])
    assert "available" not in outcome.stderr
    assert outcome.fetches == []


def test_stale_cache_is_refreshed_once_then_notice(project: Project) -> None:
    project.write_cache(
        checked_at=core.format_timestamp(NOW - timedelta(hours=25)),
        latest_version="2.1.250",
    )
    outcome = run(project, [], fetch=pointer_fetch(b"2.1.301\n"))
    assert outcome.fetches == [core.DEFAULT_RELEASE_BASE_URL + "/stable"]
    notices = [line for line in outcome.stderr.splitlines() if "2.1.301" in line]
    assert len(notices) == 1
    assert "unverified" in notices[0]
    assert "`claude-wrangler --upgrade --channel stable`" in notices[0]
    assert "is available" not in notices[0]
    cache = project.read_json("update-check.json")
    # The unsigned pointer never replaces the manifest-verified version.
    assert cache == {
        "checked_at": "2026-10-12T12:00:00Z",
        "channel": "stable",
        "latest_version": "2.1.250",
        "pointer_version": "2.1.301",
        "installed_version": "2.1.250",
        "error": None,
    }


def test_cache_for_another_channel_is_stale(project: Project) -> None:
    project.write_config(channel="latest")
    project.write_cache(channel="stable", latest_version="2.1.300")
    outcome = run(project, [], fetch=pointer_fetch(b"2.1.250"))
    assert outcome.fetches == [core.DEFAULT_RELEASE_BASE_URL + "/latest"]
    assert "available" not in outcome.stderr


@pytest.mark.parametrize("body", [None, b"<html>oops</html>", b"2.1", b"\xff\xfe"])
def test_offline_or_bad_pointer_never_blocks_launch(
    project: Project, body: Optional[bytes]
) -> None:
    project.write_cache(checked_at="2026-10-10T00:00:00Z", latest_version="2.1.299")
    fetch = None if body is None else pointer_fetch(body)
    outcome = run(project, [], fetch=fetch)
    assert outcome.code == 0
    assert outcome.launched
    cache = project.read_json("update-check.json")
    assert cache["checked_at"] == "2026-10-12T12:00:00Z"
    assert cache["latest_version"] == "2.1.299"
    # The kept answer still drives the notice; the next attempt waits a day.
    assert "2.1.299" in outcome.stderr
    assert run(project, [], fetch=fetch).fetches == []


def test_missing_cache_offline_records_attempt(project: Project) -> None:
    outcome = run(project, [])
    assert outcome.launched
    assert len(outcome.fetches) == 1
    assert project.read_json("update-check.json")["latest_version"] is None


def test_update_checks_off_disables_refresh_and_notice(project: Project) -> None:
    project.write_config(update_checks=False)
    project.write_cache(checked_at="2026-01-01T00:00:00Z", latest_version="2.1.300")
    outcome = run(project, [])
    assert outcome.fetches == []
    assert "available" not in outcome.stderr


def test_nested_launch_of_same_project_skips_notices(project: Project) -> None:
    project.write_cache(latest_version="2.1.300")
    nested = run(
        project,
        [],
        os_name="nt",
        extra_env={core.NOTICES_SHOWN_VAR: project.config_dir("nt")},
    )
    assert "available" not in nested.stderr
    assert "native Windows" not in nested.stderr
    other = run(project, [], os_name="nt", extra_env={core.NOTICES_SHOWN_VAR: "x"})
    assert "available" in other.stderr
    assert "native Windows" in other.stderr
    assert other.env[core.NOTICES_SHOWN_VAR] == project.config_dir("nt")


def test_slow_network_is_cut_off_by_the_budget(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "UPDATE_CHECK_BUDGET_SECONDS", 0.2)
    release = threading.Event()

    def slow(url: str, timeout: float) -> bytes:
        """Block until the test ends."""

        release.wait(10)
        return b"2.1.999"

    started = time.monotonic()
    try:
        outcome = run(project, [], fetch=slow)
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert outcome.launched
    assert "2.1.999" not in outcome.stderr


def test_check_updates_refreshes_prints_and_exits(project: Project) -> None:
    project.write_cache(latest_version="2.1.250")
    outcome = run(
        project, ["-p", "x", "--check-updates"], fetch=pointer_fetch(b"2.1.300")
    )
    assert outcome.code == 0
    assert not outcome.launched
    assert "channel pointer: 2.1.300 (unsigned, not verified)" in outcome.stdout
    assert "verified:        2.1.250" in outcome.stdout
    assert "installed:       2.1.250 (slot a)" in outcome.stdout
    assert "unverified until its signed manifest is checked" in outcome.stdout
    assert "`claude-wrangler --upgrade --channel stable`" in outcome.stdout
    assert "`claude-wrangler --update`" in outcome.stdout
    cache = project.read_json("update-check.json")
    assert cache["latest_version"] == "2.1.250"
    assert cache["pointer_version"] == "2.1.300"


def test_check_updates_reports_a_newer_verified_release(project: Project) -> None:
    project.write_cache(latest_version="2.1.300")
    outcome = run(project, ["--check-updates"], fetch=pointer_fetch(b"2.1.300"))
    assert outcome.code == 0
    assert "verified:        2.1.300 (signed manifest checked" in outcome.stdout
    assert "Update available." in outcome.stdout


def test_check_updates_without_verified_data_says_how_to_verify(
    project: Project,
) -> None:
    project.write_cache(latest_version=None)
    outcome = run(project, ["--check-updates"], fetch=pointer_fetch(b"2.1.250"))
    assert outcome.code == 0
    assert "verified:        none recorded; run `claude-wrangler --update`" in (
        outcome.stdout
    )
    assert "Up to date." in outcome.stdout


def test_unsigned_pointer_never_overrides_the_verified_notice(
    project: Project,
) -> None:
    project.write_cache(
        checked_at=core.format_timestamp(NOW - timedelta(hours=25)),
        latest_version="2.1.300",
    )
    outcome = run(project, [], fetch=pointer_fetch(b"2.1.999"))
    notices = [line for line in outcome.stderr.splitlines() if "2.1." in line]
    assert len(notices) == 1
    assert "Claude Code 2.1.300 is available" in notices[0]
    assert "2.1.999" not in outcome.stderr
    cache = project.read_json("update-check.json")
    assert cache["latest_version"] == "2.1.300"
    assert cache["pointer_version"] == "2.1.999"


def test_failed_refresh_keeps_the_last_pointer_for_the_channel(
    project: Project,
) -> None:
    project.write_cache(
        checked_at="2026-01-01T00:00:00Z",
        latest_version=None,
        pointer_version="2.1.299",
    )
    outcome = run(project, [])
    assert outcome.launched
    cache = project.read_json("update-check.json")
    assert cache["pointer_version"] == "2.1.299"
    assert cache["latest_version"] is None
    assert cache["error"] == "network unreachable"
    assert "unverified" in outcome.stderr
    # Another channel's pointer and verified version are never carried over.
    project.write_config(channel="latest")
    other = run(project, [])
    cache = project.read_json("update-check.json")
    assert cache["channel"] == "latest"
    assert cache["pointer_version"] is None
    assert cache["latest_version"] is None
    assert "2.1.299" not in other.stderr


def test_check_updates_up_to_date_and_disabled_note(project: Project) -> None:
    project.write_config(update_checks=False)
    outcome = run(project, ["--check-updates"], fetch=pointer_fetch(b"2.1.250"))
    assert outcome.code == 0
    assert "Up to date." in outcome.stdout
    assert "Automatic update checks are off" in outcome.stdout


def test_check_updates_failure_is_reported(project: Project) -> None:
    outcome = run(project, ["--check-updates"])
    assert outcome.code == 1
    assert not outcome.launched
    assert "update check failed for the stable channel" in outcome.stderr


@pytest.mark.parametrize(
    "candidate, installed, expected",
    [
        ("2.1.300", "2.1.250", True),
        ("2.10.0", "2.9.99", True),
        ("2.1.250", "2.1.250", False),
        ("2.1.0", "2.1.250", False),
        ("3", "2.9.9", True),
        ("2.1.300-beta", "2.1.250", False),
        (None, "2.1.250", False),
    ],
)
def test_version_comparison(candidate: Any, installed: str, expected: bool) -> None:
    assert core.is_newer_version(candidate, installed) is expected


def test_cache_freshness_rules() -> None:
    def cache(moment: str) -> Dict[str, Any]:
        """Return a stable-channel cache stamped ``moment``."""

        return {"channel": "stable", "checked_at": moment}

    assert core.update_cache_is_fresh(cache("2026-10-12T00:00:00Z"), "stable", NOW)
    assert core.update_cache_is_fresh(cache("2026-10-12T00:00:00+00:00"), "stable", NOW)
    assert not core.update_cache_is_fresh(cache("2026-10-11T11:59:59Z"), "stable", NOW)
    assert not core.update_cache_is_fresh(cache("2026-10-13T00:00:00Z"), "stable", NOW)
    assert not core.update_cache_is_fresh(cache("yesterday"), "stable", NOW)
    assert not core.update_cache_is_fresh(cache("2026-10-12T00:00:00Z"), "latest", NOW)
    assert not core.update_cache_is_fresh(None, "stable", NOW)


@pytest.mark.parametrize(
    "url",
    ["http://downloads.claude.ai/claude-code-releases/stable", "https://example.com/x"],
)
def test_fetch_refuses_non_release_urls_without_network(url: str) -> None:
    with pytest.raises(core.UpdateCheckError):
        core.fetch_url_bytes(url, 1.0)


# ---------------------------------------------------------------------------
# Node.js parity preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["warn", None, ""])
def test_preflight_warns_on_mismatch_and_launches(project: Project, mode: Any) -> None:
    (project.root / ".nvmrc").write_text("18\n", encoding="utf-8")
    extra = {} if mode is None else {core.PREFLIGHT_VAR: mode}
    outcome = run(project, [], extra_env=extra, node=("/usr/bin/node", "v20.11.0"))
    assert outcome.launched
    assert (
        "[claude-local] preflight: Project declares Node.js 18 in .nvmrc"
        in outcome.stderr
    )


def test_preflight_strict_mismatch_refuses(project: Project) -> None:
    (project.root / ".node-version").write_text("v18.19.0\r\n", encoding="utf-8")
    outcome = run(
        project,
        [],
        extra_env={core.PREFLIGHT_VAR: "strict"},
        node=("/usr/bin/node", "v20.11.0"),
    )
    assert outcome.code == 1
    assert not outcome.launched
    assert "v18.19.0 in .node-version" in outcome.stderr


def test_preflight_off_skips_selector_check(project: Project) -> None:
    (project.root / ".nvmrc").write_text("18\n", encoding="utf-8")
    outcome = run(
        project, [], extra_env={core.PREFLIGHT_VAR: "off"}, node=("/n", "v20.0.0")
    )
    assert "preflight" not in outcome.stderr
    assert outcome.launched


@pytest.mark.parametrize("mode", ["warn", "strict"])
def test_preflight_never_requires_node(project: Project, mode: str) -> None:
    (project.root / ".nvmrc").write_text("20\n", encoding="utf-8")
    outcome = run(project, [], extra_env={core.PREFLIGHT_VAR: mode}, node=(None, None))
    assert outcome.launched
    assert "node is not on PATH" in outcome.stderr


@pytest.mark.parametrize(
    "selector, actual, quiet",
    [
        ("20", "v20.11.0", True),
        ("v20.11", "v20.11.1", True),
        ("20.11.0", "v20.11.0", True),
        ("20.1", "v20.11.0", False),
        ("21", "v20.11.0", False),
    ],
)
def test_preflight_literal_selectors_match_by_leading_components(
    project: Project, selector: str, actual: str, quiet: bool
) -> None:
    (project.root / ".nvmrc").write_text(selector + "\n", encoding="utf-8")
    outcome = run(project, [], node=("/usr/bin/node", actual))
    assert ("preflight" not in outcome.stderr) is quiet


def test_preflight_non_literal_selector_is_informational(project: Project) -> None:
    (project.root / ".nvmrc").write_text("lts/iron\n", encoding="utf-8")
    outcome = run(
        project,
        [],
        extra_env={core.PREFLIGHT_VAR: "strict"},
        node=("/usr/bin/node", "v20.11.0"),
    )
    assert outcome.launched
    assert "project-activated shell" in outcome.stderr


def test_preflight_unknown_mode_degrades_to_warn(project: Project) -> None:
    (project.root / ".nvmrc").write_text("18\n", encoding="utf-8")
    outcome = run(
        project, [], extra_env={core.PREFLIGHT_VAR: "loud"}, node=("/n", "v20.0.0")
    )
    assert 'Unknown CLAUDE_LOCAL_PREFLIGHT value "loud"' in outcome.stderr
    assert outcome.launched


def test_node_selector_file_order_and_parsing(tmp_path: Path) -> None:
    assert core.find_node_selector(str(tmp_path)) is None
    (tmp_path / ".tool-versions").write_text(
        "python 3.12.1\r\n  nodejs   20.11.0\r\n", encoding="utf-8"
    )
    assert core.find_node_selector(str(tmp_path)) == (".tool-versions", "20.11.0")
    (tmp_path / ".node-version").write_text(" 18.0.0 \r\n", encoding="utf-8")
    assert core.find_node_selector(str(tmp_path)) == (".node-version", "18.0.0")
    # Like the bash elif chain, an empty .nvmrc still wins and yields nothing.
    (tmp_path / ".nvmrc").write_text("", encoding="utf-8")
    assert core.find_node_selector(str(tmp_path)) == (".nvmrc", "")


def write_fake_node(directory: Path) -> Path:
    """Create an executable fake ``node`` that prints a version."""

    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        target = directory / "node.cmd"
        target.write_text("@echo v20.11.0\r\n", encoding="ascii")
    else:
        target = directory / "node"
        target.write_text("#!/bin/sh\necho v20.11.0\n", encoding="ascii")
        target.chmod(0o755)
    return target


def test_executable_lookup_skips_relative_entries_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_fake_node(tmp_path / "tools")
    monkeypatch.chdir(str(tmp_path / "tools"))
    pathext = ".COM;.EXE;.BAT;.CMD"
    assert core.find_executable_on_path("node", "", os.name, pathext) is None
    assert core.find_executable_on_path("node", "tools", os.name, pathext) is None
    if os.name == "nt":
        # A bare drive is that drive's current directory, here the tools dir.
        drive = os.path.splitdrive(str(tmp_path))[0]
        assert core.find_executable_on_path("node", drive, "nt", pathext) is None
    found = core.find_executable_on_path(
        "node", str(tmp_path / "tools"), os.name, pathext
    )
    assert found is not None
    assert os.path.normcase(found) == os.path.normcase(str(target))


def test_default_node_probe_runs_node_from_path(tmp_path: Path) -> None:
    write_fake_node(tmp_path / "tools")
    environ = dict(os.environ)
    environ["PATH"] = str(tmp_path / "tools") + os.pathsep + environ.get("PATH", "")
    environ["PATHEXT"] = ".CMD;.EXE"
    path, version = core.default_node_probe(environ, os.name)
    assert path is not None
    assert version == "v20.11.0"


# ---------------------------------------------------------------------------
# Native-Windows sandbox warning
# ---------------------------------------------------------------------------


def windows_warning_lines(stderr: str) -> List[str]:
    """Return the sandbox warning lines found in ``stderr``."""

    return [
        line for line in stderr.splitlines() if "no sandbox on native Windows" in line
    ]


def test_windows_warning_printed_once_on_native_windows(project: Project) -> None:
    lines = windows_warning_lines(run(project, [], os_name="nt").stderr)
    assert len(lines) == 1
    assert "Credential Manager" in lines[0]


@pytest.mark.parametrize(
    "os_name, wsl, enabled",
    [
        ("nt", False, False),
        ("nt", True, True),
        ("posix", False, True),
        ("posix", True, True),
    ],
)
def test_windows_warning_suppressed(
    project: Project, os_name: str, wsl: bool, enabled: bool
) -> None:
    project.write_config(windows_sandbox_warning=enabled)
    assert (
        windows_warning_lines(run(project, [], os_name=os_name, wsl=wsl).stderr) == []
    )


# ---------------------------------------------------------------------------
# Process semantics
# ---------------------------------------------------------------------------


def test_posix_replaces_process_with_execve(project: Project) -> None:
    outcome = run(project, ["-p", "x"], exec_result=None)
    assert outcome.code == 0
    assert not outcome.spawns
    path, argv, env = outcome.execs[0]
    assert argv[0] == path
    assert env["CLAUDE_LOCAL_MANAGED_BY"] == "claude-wrangler"


@pytest.mark.parametrize(
    "child_code, expected",
    [(0, 0), (7, 7), (0xC000013A, 130), (-1073741510, 130), (None, 1)],
)
def test_windows_spawns_child_and_maps_ctrl_c(
    project: Project, child_code: Any, expected: int
) -> None:
    outcome = run(project, ["-p", "x"], os_name="nt", spawn_result=child_code)
    assert outcome.code == expected
    assert not outcome.execs
    assert outcome.spawns[0][0][0] == str(
        project.runtime / "slots" / "a" / KEY / "claude"
    )


@pytest.mark.parametrize(
    "error, expected", [(FileNotFoundError("gone"), 127), (PermissionError("no"), 126)]
)
def test_exec_failure_is_reported(
    project: Project, error: OSError, expected: int
) -> None:
    def failing_exec(path: str, argv: List[str], env: Dict[str, str]) -> None:
        """Raise like a failed execve."""

        raise error

    stderr = io.StringIO()
    code = core.main(
        [],
        environ=base_environ("posix"),
        os_name="posix",
        platform_is_wsl=False,
        core_path=str(project.core_path),
        exec_fn=failing_exec,
        stderr=stderr,
        stdout=io.StringIO(),
        fetch_fn=offline_fetch([]),
        now_fn=lambda: NOW,
        platform_key=KEY,
        node_probe=lambda environ, os_name: (None, None),
    )
    assert code == expected
    assert "cannot run" in stderr.getvalue()


def test_emit_tolerates_missing_and_narrow_streams() -> None:
    core._emit(None, "no stream at all")
    narrow = io.TextIOWrapper(
        io.BytesIO(), encoding="ascii", errors="strict", newline="\n"
    )
    core._emit(narrow, "café — path")
    narrow.flush()
    assert narrow.buffer.getvalue() == b"caf\\xe9 \\u2014 path\n"


def test_spawn_and_wait_returns_child_code_and_restores_sigint() -> None:
    before = signal.getsignal(signal.SIGINT)
    code = core.spawn_and_wait(
        [sys.executable, "-c", "import sys; sys.exit(3)"], dict(os.environ)
    )
    assert code == 3
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.parametrize(
    "code, expected",
    [(0, 0), (1, 1), (0xC000013A, 130), (-1073741510, 130), (255, 255)],
)
def test_map_windows_exit_code(code: int, expected: int) -> None:
    assert core.map_windows_exit_code(code) == expected


# ---------------------------------------------------------------------------
# End to end through a real child process
# ---------------------------------------------------------------------------

CHILD_CODE = (
    "import json, os, sys\n"
    "keys = ['HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA',"
    " 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'CLAUDE_CONFIG_DIR',"
    " 'CLAUDE_LOCAL_MANAGED_BY', 'DISABLE_UPDATES', 'DISABLE_AUTOUPDATER',"
    " 'DISABLE_INSTALLATION_CHECKS', 'PATH']\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
    "    json.dump({'argv': sys.argv[1:],"
    " 'env': {key: os.environ.get(key) for key in keys}}, handle)\n"
    "sys.exit(7)\n"
)


def install_python_as_claude(project: Project, key: str) -> None:
    """Install an interpreter stand-in for Claude Code into slot a."""

    if os.name == "nt":
        if sys.prefix == sys.base_prefix:
            pytest.skip("needs a virtual environment's relocatable launcher")
        content = Path(sys.executable).read_bytes()
        binary = project.install_slot(
            "a",
            "2.1.250",
            key=key,
            content=content,
            subcommands=["-c"],
            binary_name="claude.exe",
        )
        base_home = os.path.dirname(getattr(sys, "_base_executable", sys.executable))
        (binary.parent / "pyvenv.cfg").write_text(
            "home = {}\ninclude-system-site-packages = false\n".format(base_home),
            encoding="utf-8",
        )
    else:
        script = '#!/bin/sh\nexec "{}" "$@"\n'.format(sys.executable).encode()
        project.install_slot(
            "a", "2.1.250", key=key, content=script, subcommands=["-c"]
        )
    project.set_pointer("a")


def test_end_to_end_launch_exports_ring1_environment(tmp_path: Path) -> None:
    project = Project(tmp_path / "repo space")
    project.write_config(update_checks=False, windows_sandbox_warning=False)
    install_python_as_claude(project, core.detect_host_platform_key())
    out_file = tmp_path / "child.json"
    env = dict(os.environ)
    for name in (
        core.REPO_ROOT_VAR,
        core.SHARED_HOME_VAR,
        core.PREFLIGHT_VAR,
        core.NOTICES_SHOWN_VAR,
    ):
        env.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-I", str(project.core_path), "-c", CHILD_CODE, str(out_file)],
        cwd=str(project.root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 7, completed.stderr.decode("utf-8", "replace")
    report = json.loads(out_file.read_text(encoding="utf-8"))
    child_env = report["env"]
    config_dir = str(project.root / ".local" / "claude")
    home = str(project.root / ".local" / "claude" / "HOME")
    assert report["argv"] == [str(out_file)]
    assert child_env["CLAUDE_CONFIG_DIR"] == config_dir
    assert child_env["HOME"] == home
    assert child_env["XDG_CONFIG_HOME"] == os.path.join(home, ".config")
    assert child_env["CLAUDE_LOCAL_MANAGED_BY"] == "claude-wrangler"
    assert child_env["DISABLE_UPDATES"] == "1"
    assert child_env["PATH"].split(os.pathsep)[0] == str(project.runtime / "shim")
    if os.name == "nt":
        assert child_env["USERPROFILE"] == home
        assert child_env["HOMEDRIVE"] + child_env["HOMEPATH"] == home
        assert child_env["APPDATA"] == os.path.join(home, "AppData", "Roaming")
        assert child_env["LOCALAPPDATA"] == os.path.join(home, "AppData", "Local")


# ---------------------------------------------------------------------------
# Phase-2 extensions: mirror URL, update-check errors, config-dir spelling,
# ring export, MSYS argument conversion, and the migration-pending mode
# ---------------------------------------------------------------------------


def test_release_base_url_is_optional_and_validated() -> None:
    assert load_config().release_base_url == core.DEFAULT_RELEASE_BASE_URL
    mirror = load_config(release_base_url="https://mirror.example.com/r/")
    assert mirror.release_base_url == "https://mirror.example.com/r"
    assert core.update_check_hosts(mirror) == (
        "downloads.claude.ai",
        "mirror.example.com",
    )
    for bad in (
        "http://mirror.example.com",
        "https://user:pw@mirror.example.com",
        "https://mirror.example.com/?q=1",
        "https://",
        42,
    ):
        with pytest.raises(core.LaunchConfigError):
            load_config(release_base_url=bad)


def test_update_checks_use_the_recorded_mirror(project: Project) -> None:
    project.write_config(release_base_url="https://mirror.example.com/r")
    project.write_cache(checked_at="2026-01-01T00:00:00Z")
    outcome = run(project, [], fetch=pointer_fetch(b"2.1.301\n"))
    assert outcome.fetches == ["https://mirror.example.com/r/stable"]


def test_fetch_allows_the_mirror_host_only_when_listed() -> None:
    with pytest.raises(core.UpdateCheckError):
        core.fetch_url_bytes("https://mirror.example.com/r/stable", 1.0)


def test_failed_refresh_records_the_error(project: Project) -> None:
    project.write_cache(checked_at="2026-01-01T00:00:00Z", latest_version="2.1.299")
    outcome = run(project, ["--check-updates"])
    assert outcome.code == 1
    cache = project.read_json("update-check.json")
    assert cache["error"] == "network unreachable"
    assert cache["latest_version"] == "2.1.299"
    success = run(project, ["--check-updates"], fetch=pointer_fetch(b"2.1.300\n"))
    assert success.code == 0
    assert project.read_json("update-check.json")["error"] is None


def test_forward_slash_config_dir_spelling_on_windows(project: Project) -> None:
    project.write_config(config_dir_spelling="forward-slash")
    outcome = run(project, [], os_name="nt")
    expected = project.config_dir("nt").replace("\\", "/")
    expected = expected[0].upper() + expected[1:]
    assert outcome.env["CLAUDE_CONFIG_DIR"] == expected
    assert outcome.env[core.NOTICES_SHOWN_VAR] == expected
    posix = run(project, [], os_name="posix")
    assert posix.env["CLAUDE_CONFIG_DIR"] == project.config_dir("posix")


def test_config_dir_spelling_is_validated() -> None:
    assert load_config().config_dir_spelling == "native"
    with pytest.raises(core.LaunchConfigError):
        load_config(config_dir_spelling="backslash")


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_msys_argument_override_is_removed_from_the_child(
    project: Project, os_name: str
) -> None:
    outcome = run(
        project,
        [],
        os_name=os_name,
        extra_env={
            core.MSYS_ARGS_VAR: "1",
            "MSYS_NO_PATHCONV": "1",
            "MSYS2_ARG_CONV_EXCL": "*",
        },
    )
    for name in (core.MSYS_ARGS_VAR, "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL"):
        assert name not in outcome.env
    kept = run(project, [], os_name=os_name, extra_env={"MSYS_NO_PATHCONV": "1"})
    assert kept.env["MSYS_NO_PATHCONV"] == "1"


def test_ring_choice_is_exported_for_nested_launches(project: Project) -> None:
    ring1 = run(project, [])
    assert ring1.env[core.SHARED_HOME_VAR] == "0"
    ring0 = run(project, ["--shared-home"])
    assert ring0.env[core.SHARED_HOME_VAR] == "1"
    nested = run(project, [], extra_env={core.SHARED_HOME_VAR: "1"})
    assert nested.env["HOME"] == "/home/operator"


def write_legacy_binary(project: Project, version: Optional[str] = "2.1.287") -> str:
    """Create an npm-layout binary and switch launch.json to legacy mode."""

    relative = (
        ".local/claude/npm/node_modules/@anthropic-ai/claude-code-{}/claude".format(KEY)
    )
    binary = project.root.joinpath(*relative.split("/"))
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"legacy native binary")
    binary.chmod(0o755)
    project.write_config(legacy={"binary": relative, "version": version})
    return str(binary)


def test_legacy_mode_runs_the_npm_layout_binary_without_node(tmp_path: Path) -> None:
    project = Project(tmp_path / "repo")
    binary = write_legacy_binary(project)
    outcome = run(project, ["-p", "hi"], node=(None, None))
    assert outcome.code == 0
    assert outcome.argv[0] == binary
    assert outcome.argv[1] == "--settings"
    assert outcome.argv[-2:] == ["-p", "hi"]
    assert outcome.env["DISABLE_UPDATES"] == "1"
    assert "migration pending" in outcome.stderr
    assert not (project.runtime / "fallback.json").exists()


def test_legacy_mode_refuses_a_missing_or_escaping_binary(tmp_path: Path) -> None:
    project = Project(tmp_path / "repo")
    project.write_config(
        legacy={"binary": ".local/claude/npm/node_modules/x/claude", "version": None}
    )
    missing = run(project, [])
    assert missing.code == 1 and not missing.launched
    assert "legacy Claude Code binary is missing" in missing.stderr
    with pytest.raises(core.LaunchConfigError):
        load_config(legacy={"binary": "../outside/claude", "version": None})
    with pytest.raises(core.LaunchConfigError):
        load_config(legacy={"binary": "a/claude", "version": "latest"})


def test_legacy_mode_update_notice_uses_the_legacy_version(tmp_path: Path) -> None:
    project = Project(tmp_path / "repo")
    write_legacy_binary(project, version="2.1.287")
    project.write_cache(latest_version="2.1.300")
    outcome = run(project, [])
    assert "Claude Code 2.1.300 is available" in outcome.stderr
    assert "this project runs 2.1.287" in outcome.stderr


def test_ring1_on_windows_creates_appdata_directories(project: Project) -> None:
    paths = core.build_repo_paths(str(project.root), load_config(), "nt")
    core.ensure_session_directories(paths, False, "nt")
    home = Path(paths.home_dir_fs)
    assert (home / "AppData" / "Roaming").is_dir()
    assert (home / "AppData" / "Local").is_dir()


# ---------------------------------------------------------------------------
# Verification stamp (ab_runtime_slots.md requirement 8)
# ---------------------------------------------------------------------------


def test_slot_without_a_verification_stamp_never_runs(project: Project) -> None:
    project.install_slot("a", "2.1.250", stamp=False)
    (project.runtime / "slots" / "a" / core.VERIFICATION_STAMP_NAME).unlink()

    outcome = run(project, [])

    assert outcome.code == 1
    assert not outcome.launched
    assert "no verification stamp" in outcome.stderr
    assert "--repair-install" in outcome.stderr


def test_binary_replaced_after_its_stamp_falls_back_to_the_verified_slot(
    project: Project,
) -> None:
    project.install_slot("b", "2.1.240")
    binary = project.runtime / "slots" / "a" / KEY / "claude"
    data = b"y" * binary.stat().st_size
    time.sleep(0.05)
    # A clone or pull writes a new file with the same size.
    incoming = binary.with_name("incoming")
    incoming.write_bytes(data)
    os.replace(str(incoming), str(binary))

    outcome = run(project, ["-p", "x"])

    assert outcome.code == 0
    assert outcome.execs[0][0] == str(project.runtime / "slots" / "b" / KEY / "claude")
    assert "changed after claude-wrangler last verified it" in outcome.stderr


@pytest.mark.parametrize(
    "stamp",
    [
        "{not json",
        json.dumps({"schema": 2, "binaries": {}}),
        json.dumps({"schema": 1, "binaries": {KEY: "nope"}}),
        json.dumps({"schema": 1, "binaries": {"darwin-arm64": {"size": 1}}}),
    ],
)
def test_a_malformed_or_foreign_stamp_counts_as_missing(
    project: Project, stamp: str
) -> None:
    (project.runtime / "slots" / "a" / core.VERIFICATION_STAMP_NAME).write_text(
        stamp, encoding="utf-8"
    )

    check = core.check_slot(str(project.runtime), "a", KEY)

    assert not check.ok
    assert "verification stamp" in check.reason


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 short names")
def test_long_path_spelling_expands_an_8_3_spelling(tmp_path: Path) -> None:
    from claude_wrangler.rendering import _windows_short_path

    directory = tmp_path / "long-project-name" / "runtime-root"
    directory.mkdir(parents=True)
    short = _windows_short_path(str(directory))
    if short is None or "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume")

    expanded = core.long_path_spelling(short)

    assert os.path.normcase(expanded) == os.path.normcase(str(directory))
    assert core.long_path_spelling(str(directory)) == str(directory)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_session_directories_are_created_private(project: Project) -> None:
    # A config dir and home the launcher creates hold transcripts and
    # credentials, so they are private to the operator whatever the umask.
    project.write_config(config_dir=".local/session", home_dir=".local/session/HOME")
    previous = os.umask(0o022)
    try:
        outcome = run(project, ["-p", "x"])
    finally:
        os.umask(previous)

    assert outcome.code == 0
    for path in (
        project.root / ".local" / "session",
        project.root / ".local" / "session" / "HOME",
    ):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path


# ---------------------------------------------------------------------------
# Another Claude Code session's variables and inherited credentials
# ---------------------------------------------------------------------------

PARENT_SESSION_SAMPLE = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "parent-session",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_SESSION_ATTENDED": "1",
    "CLAUDE_CODE_EXECPATH": "C:\\elsewhere\\claude.exe",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_BRIDGE_SESSION_ID": "bridge",
    "CLAUDE_PID": "4242",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR": "C:\\elsewhere",
    "CLAUDE_CODE_MESSAGING_SOCKET": "parent-pipe",
    "CLAUDE_CODE_MESSAGING_TOKEN": "parent-token",
    "CLAUDE_CODE_SSE_PORT": "4321",
    "CLAUDE_EFFORT": "high",
}


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_a_fresh_launch_drops_another_sessions_variables(os_name: str) -> None:
    base = dict(base_environ(os_name), **PARENT_SESSION_SAMPLE)
    env = session_env(os_name, shared_home=False, base=base)
    for name in PARENT_SESSION_SAMPLE:
        assert name not in env, name


def test_windows_matches_parent_session_names_case_insensitively() -> None:
    base = dict(base_environ("nt"), ClaudeCode="1", Claude_Code_Session_Id="x")
    env = session_env("nt", shared_home=False, base=base)
    assert "ClaudeCode" not in env and "Claude_Code_Session_Id" not in env


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_a_nested_launch_of_the_same_project_keeps_its_session_variables(
    os_name: str,
) -> None:
    root = NT_ROOT if os_name == "nt" else POSIX_ROOT
    config = load_config()
    paths = core.build_repo_paths(root, config, os_name)
    base = dict(
        base_environ(os_name),
        **PARENT_SESSION_SAMPLE,
        **{core.NOTICES_SHOWN_VAR: paths.config_dir},
    )
    env = core.build_session_environment(
        base, paths=paths, config=config, shared_home=False, os_name=os_name
    )
    for name, value in PARENT_SESSION_SAMPLE.items():
        assert env[name] == value, name


def test_ide_port_is_kept_unless_another_claude_session_started_us() -> None:
    from_ide = session_env(
        "posix",
        shared_home=False,
        base=dict(base_environ("posix"), CLAUDE_CODE_SSE_PORT="4321"),
    )
    assert from_ide["CLAUDE_CODE_SSE_PORT"] == "4321"
    from_claude = session_env(
        "posix",
        shared_home=False,
        base=dict(base_environ("posix"), CLAUDE_CODE_SSE_PORT="4321", CLAUDECODE="1"),
    )
    assert "CLAUDE_CODE_SSE_PORT" not in from_claude


@pytest.mark.parametrize("name", ["CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT"])
def test_host_variables_are_kept_unless_another_claude_session_started_us(
    name: str,
) -> None:
    # An Agent SDK host or IDE extension sets these for the CLI it starts,
    # with no CLAUDECODE; only a Claude Code parent session's copies go.
    from_host = session_env(
        "nt", shared_home=False, base=dict(base_environ("nt"), **{name: "sdk-py"})
    )
    assert from_host[name] == "sdk-py"
    from_claude = session_env(
        "nt",
        shared_home=False,
        base=dict(base_environ("nt"), **{name: "cli", "CLAUDECODE": "1"}),
    )
    assert name not in from_claude


def test_cmd_shim_helper_variables_never_reach_claude() -> None:
    base = dict(
        base_environ("nt"),
        CLAUDE_LOCAL_CORE="core",
        CLAUDE_LOCAL_PY="py.exe",
        CLAUDE_LOCAL_PYARG="-3",
        CLAUDE_LOCAL_PROBE="probe",
        CLAUDE_LOCAL_SELF="C:\\x\\",
    )
    env = session_env("nt", shared_home=False, base=base)
    for name in core.SHIM_INTERNAL_VARS:
        assert name not in env, name


INHERITED_CREDENTIALS = {
    "ANTHROPIC_API_KEY": "dummy-value-for-test",
    "ANTHROPIC_AUTH_TOKEN": "dummy-auth-token",
    "ANTHROPIC_BASE_URL": "https://gateway.example.invalid",
    "ANTHROPIC_FOUNDRY_API_KEY": "dummy-foundry-key",
    "CLAUDE_CODE_OAUTH_TOKEN": "dummy-oauth-token",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "dummy-refresh-token",
    "AWS_BEARER_TOKEN_BEDROCK": "dummy-bedrock-key",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "dummy-passphrase",
    "CLAUDE_CODE_USE_BEDROCK": "1",
}


def test_inherited_credentials_are_removed_and_named_once_per_session(
    project: Project,
) -> None:
    fresh = run(project, ["-p", "x"], extra_env=INHERITED_CREDENTIALS)
    assert fresh.code == 0
    for name in INHERITED_CREDENTIALS:
        assert name not in fresh.env, name
    notes = [line for line in fresh.stderr.splitlines() if "ANTHROPIC_API_KEY" in line]
    assert len(notes) == 1
    assert "removed" in notes[0] and "CLAUDE_CODE_USE_BEDROCK" in notes[0]
    assert "--set-credential-passthrough on" in notes[0]
    for value in INHERITED_CREDENTIALS.values():
        if value != "1":
            assert value not in fresh.stderr
    nested = run(
        project,
        ["-p", "x"],
        extra_env=dict(
            INHERITED_CREDENTIALS,
            **{core.NOTICES_SHOWN_VAR: project.config_dir("posix")},
        ),
    )
    assert "ANTHROPIC_API_KEY" not in nested.stderr
    assert "ANTHROPIC_API_KEY" not in nested.env


def test_credential_passthrough_keeps_inherited_credentials(project: Project) -> None:
    project.write_config(credential_passthrough=True)
    fresh = run(project, ["-p", "x"], extra_env=INHERITED_CREDENTIALS)
    assert fresh.code == 0
    for name, value in INHERITED_CREDENTIALS.items():
        assert fresh.env[name] == value, name
    notes = [line for line in fresh.stderr.splitlines() if "ANTHROPIC_API_KEY" in line]
    assert len(notes) == 1
    assert "passed through" in notes[0]
    assert "--set-credential-passthrough off" in notes[0]
    assert "dummy-value-for-test" not in fresh.stderr


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_credentials_are_removed_at_every_ring(os_name: str) -> None:
    base = dict(base_environ(os_name), ANTHROPIC_API_KEY="k", CLAUDE_CODE_USE_VERTEX="")
    for shared_home in (False, True):
        env = session_env(os_name, shared_home=shared_home, base=base)
        # The empty provider switch goes too.
        assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_USE_VERTEX" not in env


def test_credential_names_match_by_prefix_and_list() -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_SOME_FUTURE_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
        "CLAUDE_CODE_ORGANIZATION_UUID",
    ):
        assert core.is_credential_variable(name, "posix"), name
    for name in (
        "CLAUDE_CODE_USE_POWERSHELL_TOOL",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "CLAUDE_CONFIG_DIR",
        "AWS_PROFILE",
        "GH_TOKEN",
    ):
        assert not core.is_credential_variable(name, "posix"), name
    # Windows names are case insensitive; POSIX names are not.
    assert core.is_credential_variable("Anthropic_Api_Key", "nt")
    assert not core.is_credential_variable("anthropic_api_key", "posix")


def test_windows_removes_credentials_spelled_in_any_case() -> None:
    base = dict(base_environ("nt"), Anthropic_Api_Key="k", claude_code_use_bedrock="1")
    env = session_env("nt", shared_home=False, base=base)
    assert "Anthropic_Api_Key" not in env and "claude_code_use_bedrock" not in env


def test_launch_config_without_the_passthrough_field_removes_credentials() -> None:
    assert load_config().credential_passthrough is False
    assert load_config(credential_passthrough=True).credential_passthrough is True
    with pytest.raises(core.LaunchConfigError):
        load_config(credential_passthrough=1)


def test_credential_note_grammar() -> None:
    one = core.credential_note(["ANTHROPIC_API_KEY"], "/r/.local/claude")
    assert "removed ANTHROPIC_API_KEY" in one and "If it belongs" in one
    assert "to pass it through" in one
    two = core.credential_note(["A", "B"], "/r/.local/claude")
    assert "removed A, B" in two and "If they belong" in two
    kept = core.credential_note(["A"], "/r/.local/claude", passthrough=True)
    assert "A is passed through" in kept and "unless it belongs" in kept
    kept_two = core.credential_note(
        ["A", "B"], "/r/.local/claude", passthrough=True, command="cw"
    )
    assert "A, B are passed through" in kept_two
    assert "`cw --set-credential-passthrough off .`" in kept_two


# ---------------------------------------------------------------------------
# Nested calls on Windows: the slot directory on PATH
# ---------------------------------------------------------------------------


def nt_selection(project: Project) -> core.SlotSelection:
    """Return slot a's selection for a Windows platform key."""

    project.install_slot("a", "2.1.250", key="win32-x64", binary_name="claude.exe")
    return core.select_slot(str(project.runtime), "win32-x64")


def test_windows_puts_the_slot_directory_before_the_shim(project: Project) -> None:
    selection = nt_selection(project)
    slot_dir = core.nested_claude_slot_dir(selection, load_config(), "nt")
    assert slot_dir == os.path.dirname(selection.binary)
    outcome = run(project, ["-p", "x"], os_name="nt", platform_key="win32-x64")
    assert outcome.code == 0, outcome.stderr
    entries = outcome.env["PATH"].split(";")
    assert entries[:2] == [slot_dir, project.config_dir("nt") + "\\wrangler\\shim"]
    assert entries[2:] == base_environ("nt")["PATH"].split(";")
    assert "shim\\claude.cmd" not in outcome.stderr


def test_slot_directory_with_other_files_is_not_put_on_path(project: Project) -> None:
    selection = nt_selection(project)
    extra = Path(selection.binary).parent / "helper.exe"
    extra.write_bytes(b"x")
    assert core.nested_claude_slot_dir(selection, load_config(), "nt") is None
    # The launch still runs, but says that nested calls lost the direct route.
    outcome = run(project, ["-p", "x"], os_name="nt", platform_key="win32-x64")
    assert outcome.code == 0, outcome.stderr
    assert outcome.env["PATH"].split(";")[0] == (
        project.config_dir("nt") + "\\wrangler\\shim"
    )
    assert "go through shim\\claude.cmd" in outcome.stderr
    assert "--repair-install" in outcome.stderr
    nested = run(
        project,
        ["-p", "x"],
        os_name="nt",
        platform_key="win32-x64",
        extra_env={core.NOTICES_SHOWN_VAR: project.config_dir("nt")},
    )
    assert "shim\\claude.cmd" not in nested.stderr


def test_legacy_mode_on_windows_says_nested_calls_use_the_cmd_shim(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path / "repo")
    write_legacy_binary(project)
    outcome = run(project, ["-p", "x"], os_name="nt", node=(None, None))
    assert "go through shim\\claude.cmd" in outcome.stderr
    assert "npm-era layout is still in place" in outcome.stderr


def test_slot_directory_is_never_used_on_posix_or_in_legacy_mode(
    project: Project,
) -> None:
    selection = nt_selection(project)
    assert core.nested_claude_slot_dir(selection, load_config(), "posix") is None
    legacy = load_config(legacy={"binary": "a/b/claude.exe", "version": None})
    assert core.nested_claude_slot_dir(selection, legacy, "nt") is None


# ---------------------------------------------------------------------------
# PATH separators and canonical Windows spellings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry, os_name, bad",
    [
        ("C:\\work\\a;b\\shim", "nt", True),
        ("C:\\work\\ab\\shim", "nt", False),
        ("/srv/a:b/shim", "posix", True),
        ("/srv/ab/shim", "posix", False),
    ],
)
def test_path_entry_problem(entry: str, os_name: str, bad: bool) -> None:
    assert (core.path_entry_problem(entry, os_name) is not None) is bad


def test_a_root_containing_the_path_separator_is_refused(tmp_path: Path) -> None:
    separator = ";" if os.name == "nt" else ":"
    bare = Project(tmp_path / "re{}po".format(separator))
    bare.install_slot("a", "2.1.250")
    bare.set_pointer("a")
    outcome = run(bare, ["-p", "x"], os_name=os.name)
    assert outcome.code == 1
    assert not outcome.launched
    assert "PATH separator" in outcome.stderr


@pytest.mark.parametrize(
    "value, expected",
    [
        ("c:\\Work\\repo", "C:\\Work\\repo"),
        ("C:\\Work\\repo", "C:\\Work\\repo"),
        ("\\\\server\\share\\repo", "\\\\server\\share\\repo"),
        ("d:/x", "D:/x"),
    ],
)
def test_canonical_windows_root_upper_cases_the_drive(
    value: str, expected: str
) -> None:
    assert core.canonical_windows_root(value) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows drive letters and 8.3 names")
def test_launch_through_a_lower_case_drive_exports_the_canonical_spelling(
    project: Project,
) -> None:
    spelling = str(project.root)
    lowered = spelling[0].lower() + spelling[1:]
    outcome = run(project, [], os_name="nt", extra_env={core.REPO_ROOT_VAR: lowered})
    assert outcome.code == 0
    assert outcome.env["CLAUDE_CONFIG_DIR"] == project.config_dir("nt")
    assert outcome.env["HOME"].startswith(spelling[0].upper() + ":")


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 short names")
def test_launch_through_an_8_3_spelling_exports_long_names(tmp_path: Path) -> None:
    from claude_wrangler.rendering import _windows_short_path

    bare = Project(tmp_path / "long-project-name-here")
    bare.install_slot("a", "2.1.250")
    bare.set_pointer("a")
    short = _windows_short_path(str(bare.root))
    if short is None or "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume")
    outcome = run(bare, [], os_name="nt", extra_env={core.REPO_ROOT_VAR: short})
    assert outcome.code == 0
    assert "~" not in outcome.env["CLAUDE_CONFIG_DIR"]
    assert os.path.normcase(outcome.env["CLAUDE_CONFIG_DIR"]) == os.path.normcase(
        bare.config_dir("nt")
    )


def test_forward_slash_spelling_is_not_canonicalized_further(project: Project) -> None:
    project.write_config(config_dir_spelling="forward-slash")
    outcome = run(project, [], os_name="nt")
    assert outcome.env["CLAUDE_CONFIG_DIR"] == core.forward_slash_spelling(
        project.config_dir("nt")
    )


# ---------------------------------------------------------------------------
# Windows ACLs and the kill-on-close job
# ---------------------------------------------------------------------------

USER_SID = "S-1-5-21-1-2-3-1001"
OTHER_SID = "S-1-5-21-1-2-3-1002"
ALLOW = core.ACCESS_ALLOWED_ACE_TYPE
DENY = core.ACCESS_DENIED_ACE_TYPE


def test_private_dacl_principals() -> None:
    assert core.private_dacl_principals(USER_SID, USER_SID) == [
        USER_SID,
        core.LOCAL_SYSTEM_SID,
        core.ADMINISTRATORS_SID,
    ]
    assert core.private_dacl_principals(USER_SID, OTHER_SID)[:2] == [
        USER_SID,
        OTHER_SID,
    ]
    # A group owner (Administrators, or anything broad) is never added.
    assert "S-1-1-0" not in core.private_dacl_principals(USER_SID, "S-1-1-0")


@pytest.mark.parametrize(
    "entries, private",
    [
        ([(ALLOW, 3, 0x1F01FF, USER_SID), (ALLOW, 3, 0x1F01FF, "S-1-5-18")], True),
        ([(ALLOW, 3, 0x1F01FF, "S-1-3-4"), (DENY, 0, 0x1, "S-1-1-0")], True),
        ([(ALLOW, 3, 0x1200A9, "S-1-5-32-545")], False),
        ([(ALLOW, 3, 0x1F01FF, OTHER_SID)], False),
        ([(5, 3, 0x1F01FF, USER_SID)], False),
    ],
)
def test_dacl_is_private(entries: Any, private: bool) -> None:
    principals = core.private_dacl_principals(USER_SID, None)
    assert core.dacl_is_private(entries, principals) is private


def test_broad_write_grants_names_groups_that_can_change_a_checkout() -> None:
    drive_root = [
        (ALLOW, 0, 0x001200A9, "S-1-5-32-545"),
        (ALLOW, 11, 0xE0010000, "S-1-5-11"),
        (ALLOW, 0, 0x4, "S-1-5-11"),
        (ALLOW, 3, 0x1F01FF, "S-1-5-18"),
    ]
    assert core.broad_write_grants(drive_root) == ["Authenticated Users"]
    assert core.broad_write_grants([(ALLOW, 3, 0x1301BF, "S-1-1-0")]) == ["Everyone"]
    assert core.broad_write_grants([(DENY, 3, 0x1F01FF, "S-1-1-0")]) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_launcher_creates_private_session_directories_on_windows(
    project: Project,
) -> None:
    project.write_config(config_dir=".local/session", home_dir=".local/session/HOME")
    outcome = run(project, ["-p", "x"], os_name="nt")
    assert outcome.code == 0
    user = core.windows_current_user_sid()
    assert user is not None
    for path in (
        project.root / ".local" / "session",
        project.root / ".local" / "session" / "HOME",
    ):
        summary = core.windows_security_summary(str(path))
        assert summary is not None, path
        sids = {sid for kind, _flags, _mask, sid in summary[1] if kind == ALLOW}
        assert sids == {user, core.LOCAL_SYSTEM_SID, core.ADMINISTRATORS_SID}, path


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_existing_session_directories_keep_their_acl_at_launch(
    project: Project,
) -> None:
    config_dir = project.root / ".local" / "claude"
    before = core.windows_security_summary(str(config_dir))
    outcome = run(project, ["-p", "x"], os_name="nt")
    assert outcome.code == 0
    assert core.windows_security_summary(str(config_dir)) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_attach_kill_on_close_job_ends_the_child_when_closed() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
    )
    try:
        job = core.attach_kill_on_close_job(process.pid)
        assert job is not None
        assert process.poll() is None
        core.close_job(job)
        # Closing the last handle ends the child long before its sleep would.
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
