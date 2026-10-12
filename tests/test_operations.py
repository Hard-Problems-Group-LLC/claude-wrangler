"""Offline tests for install, upgrade, update, rollback, repair, and uninstall.

Every test drives the real operations, slot store, promotion, metadata, and
generated files; only the network, signature verification of fake
manifests, binary execution, the process table, and the clock are faked
(see tests/native_fakes.py).
"""

from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Any, Dict, List

import pytest

from claude_wrangler import launcher_core, operations
from claude_wrangler.config import parse_args
from claude_wrangler.constants import GITIGNORE_BEGIN, METADATA_FILENAME, SCHEMA_VERSION
from claude_wrangler.generator import SettingsFileError
from claude_wrangler.inspection import gather_inspection_report, selftest_operation
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.operations import (
    ensure_gitignore_block,
    install_like_operation,
    rollback_operation,
    uninstall_operation,
    update_operation,
)
from claude_wrangler.platforms import PlatformError, binary_filename
from claude_wrangler.rendering import launcher_core_source
from claude_wrangler.slots import PointerCommittedError, RuntimeStore
from claude_wrangler.transaction import RuntimeInUseError

from native_fakes import (
    FakeProcessTable,
    UnreachableSource,
    FakeRelease,
    FakeReleaseSource,
    Recorder,
    fake_binary,
    host_key,
    make_config,
    make_services,
    plant_unverified_slot,
    standard_releases,
    standard_source,
)

KEY = host_key()
BINARY = binary_filename(KEY)


def runtime(project: Path) -> Path:
    """Return the default runtime root of a project."""

    return project / ".local" / "claude" / "wrangler"


def store(project: Path) -> RuntimeStore:
    """Return the slot store of a project."""

    return RuntimeStore(runtime(project), project_root=project)


def metadata(project: Path) -> Dict[str, Any]:
    """Read the schema-2 metadata."""

    return json.loads(
        (runtime(project) / METADATA_FILENAME).read_text(encoding="utf-8")
    )


def slot_binary(project: Path, slot: str) -> Path:
    """Return a slot's host binary path."""

    return runtime(project) / "slots" / slot / KEY / BINARY


def install(project: Path, source: FakeReleaseSource, *args: str, **kwargs: Any) -> int:
    """Run an install-like operation with fake services."""

    services = kwargs.pop("services", None) or make_services(source, **kwargs)
    return install_like_operation(make_config(project, *args), services)


def snapshot(project: Path) -> Dict[str, Any]:
    """Capture the observable runtime state for no-change assertions."""

    rt = runtime(project)
    files = {}
    for path in sorted(rt.rglob("*")) if rt.exists() else []:
        if path.is_file() and ".lock" not in path.name:
            files[path.relative_to(rt).as_posix()] = path.read_bytes()
    return files


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Return an empty project directory with a .git marker."""

    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def installed(project: Path) -> Path:
    """Return a project with stable 2.1.287 installed in slot a."""

    assert install(project, standard_source()) == 0
    return project


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def test_fresh_install_promotes_stable_into_slot_a_and_publishes_files(project):
    source = standard_source()
    recorder = Recorder()

    assert install(project, source, recorder=recorder) == 0

    slots = store(project)
    assert slots.read_pointer() == "a"
    record = slots.slot_record("a")
    assert record is not None and record.version == "2.1.287"
    assert record.channel == "stable"
    assert record.platforms[KEY]["binary"] == "{}/{}".format(KEY, BINARY)
    assert record.subcommands == [
        "doctor",
        "mcp",
        "plugin",
        "plugins",
        "update",
        "upgrade",
    ]
    assert record.schemes == ["openpgp", "raw-rsa"]
    assert record.settings_rejected == []
    assert record.verified_at == "2026-10-12T12:00:00Z"
    for name in ("manifest.json", "manifest.json.sig", "manifest.json.raw-sig.json"):
        assert (slots.slot_dir("a") / name).is_file()
    assert source.pointer_requests == ["stable"]
    assert source.manifest_requests == ["2.1.287"]
    assert len(source.downloads) == 1

    meta = metadata(project)
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["claude_version"] == "2.1.287"
    assert meta["claude_channel"] == "stable"
    assert meta["claude_selector"] == "latest"
    assert meta["platform"] == KEY
    assert meta["update_checks"] is True
    assert meta["windows_sandbox_warning"] is True
    assert meta["credential_passthrough"] is False
    assert meta["launcher_python"]
    assert meta["config_dir_spelling"] == "native"
    assert meta["paths"]["local_dir"] == ".local/claude/wrangler"
    assert meta["legacy_migrated_from"] is None

    rt = runtime(project)
    assert (rt / "launch.py").read_bytes() == launcher_core_source()
    parsed = launcher_core.parse_launch_config(
        json.loads((rt / "launch.json").read_text(encoding="utf-8"))
    )
    assert parsed.channel == "stable"
    assert parsed.repo_root_from_core == ("..", "..", "..")
    assert parsed.legacy_binary is None
    for path in (
        project / "bin" / "claude-local",
        project / "bin" / "claude-local.cmd",
        rt / "shim" / "claude",
        rt / "shim" / "claude.cmd",
        project / "README-LOCAL-Start-Claude.md",
    ):
        assert path.is_file(), path
    settings = json.loads(
        (project / ".local" / "claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["env"]["DISABLE_UPDATES"] == "1"
    assert settings["disableDeepLinkRegistration"] == "disable"
    assert not list((rt / ".staging").iterdir())
    assert recorder.structure == [(recorder.structure[0][0], KEY)]
    smoke_env = recorder.smoke[0][2]
    assert recorder.smoke[0][1] == "2.1.287"
    assert ".staging" in smoke_env["HOME"]
    assert ".staging" in smoke_env["CLAUDE_CONFIG_DIR"]
    assert smoke_env["DISABLE_UPDATES"] == "1"


def test_reinstall_keeps_the_pinned_version_without_network(installed):
    source = standard_source()
    before = snapshot(installed)

    assert install(installed, source) == 0

    assert source.pointer_requests == []
    assert source.manifest_requests == []
    assert store(installed).read_pointer() == "a"
    after = snapshot(installed)
    assert before["slots/a/.complete.json"] == after["slots/a/.complete.json"]


def test_install_with_explicit_channel_installs_into_inactive_slot(installed):
    source = standard_source()

    assert install(installed, source, "--channel", "latest") == 0

    slots = store(installed)
    assert slots.read_pointer() == "b"
    assert slots.slot_record("b").version == "2.1.296"
    assert slots.slot_record("a").version == "2.1.287"
    assert metadata(installed)["claude_channel"] == "latest"


def test_upgrade_rotates_slots_and_retires_the_displaced_release(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    assert store(installed).read_pointer() == "b"

    assert (
        install(
            installed,
            source,
            "--upgrade",
            "--channel",
            "stable",
            "--version",
            "2.1.207",
        )
        == 0
    )

    slots = store(installed)
    assert slots.read_pointer() == "a"
    assert slots.slot_record("a").version == "2.1.207"
    assert slots.slot_record("b").version == "2.1.296"
    # The displaced 2.1.287 slot went through .retired and was deleted.
    assert not list((runtime(installed) / ".retired").iterdir())
    meta = metadata(installed)
    assert meta["claude_version"] == "2.1.207"
    assert meta["claude_selector"] == "2.1.207"


def test_upgrade_refuses_when_the_slot_to_displace_is_in_use(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    processes = FakeProcessTable()
    processes.run_from(slot_binary(installed, "a"))
    before = snapshot(installed)

    with pytest.raises(RuntimeInUseError, match="pid 4242"):
        install(
            installed,
            source,
            "--upgrade",
            "--channel",
            "stable",
            "--version",
            "2.1.207",
            processes=processes,
        )

    after = snapshot(installed)
    assert {k: v for k, v in after.items() if not k.startswith(".staging")} == {
        k: v for k, v in before.items() if not k.startswith(".staging")
    }
    assert not [k for k in after if k.startswith(".staging")]


def test_upgrade_stops_background_service_only_when_allowed(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    processes = FakeProcessTable()
    processes.run_from(slot_binary(installed, "a"))
    recorder = Recorder()

    assert (
        install(
            installed,
            source,
            "--upgrade",
            "--channel",
            "stable",
            "--version",
            "2.1.207",
            "--stop-background-sessions",
            processes=processes,
            recorder=recorder,
        )
        == 0
    )

    assert len(recorder.stops) == 1
    binary, env = recorder.stops[0]
    assert binary == str(slot_binary(installed, "b"))
    assert env["CLAUDE_CONFIG_DIR"].endswith(os.path.join(".local", "claude"))
    assert env["DISABLE_UPDATES"] == "1"
    assert store(installed).slot_record("a").version == "2.1.207"


def test_upgrade_fails_closed_when_the_process_table_is_unreadable(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0

    with pytest.raises(ClaudeWranglerError, match="refusing to continue"):
        install(
            installed,
            source,
            "--upgrade",
            "--channel",
            "stable",
            "--version",
            "2.1.207",
            processes=FakeProcessTable(fail=True),
        )

    assert store(installed).read_pointer() == "b"
    assert store(installed).slot_record("a").version == "2.1.287"


def test_download_hash_mismatch_changes_nothing(installed):
    key = KEY
    good = FakeRelease("2.1.296", {key: fake_binary("2.1.296", key)})
    source = FakeReleaseSource([good], {"stable": "2.1.287", "latest": "2.1.296"})
    url = source.artifact_url("2.1.296", key)
    source.transport.bodies[url] = fake_binary("2.1.296", key)[:-1] + b"X"
    before = snapshot(installed)
    meta_before = (runtime(installed) / METADATA_FILENAME).read_bytes()

    with pytest.raises(ClaudeWranglerError):
        install(installed, source, "--upgrade", "--channel", "latest")

    assert store(installed).read_pointer() == "a"
    assert (runtime(installed) / METADATA_FILENAME).read_bytes() == meta_before
    after = snapshot(installed)
    assert {k for k in after if not k.startswith(".staging")} == {
        k for k in before if not k.startswith(".staging")
    }
    assert not list((runtime(installed) / ".staging").iterdir())


def test_smoke_test_failure_changes_nothing(installed):
    key = KEY
    wrong = FakeRelease("2.1.296", {key: fake_binary("2.1.000", key)})
    source = FakeReleaseSource([wrong], {"latest": "2.1.296"})

    with pytest.raises(ClaudeWranglerError, match="Smoke test failed"):
        install(installed, source, "--upgrade", "--channel", "latest")

    assert store(installed).read_pointer() == "a"
    assert not (runtime(installed) / "slots" / "b").exists()
    assert metadata(installed)["claude_version"] == "2.1.287"


def test_failure_after_the_commit_point_is_treated_as_committed(installed, monkeypatch):
    original = RuntimeStore.promote

    def promote_then_fail(self, *args, **kwargs):
        """Promote for real, then report a late failure."""

        original(self, *args, **kwargs)
        raise PointerCommittedError("simulated failure after the pointer moved")

    monkeypatch.setattr(RuntimeStore, "promote", promote_then_fail)

    assert (
        install(installed, standard_source(), "--upgrade", "--channel", "latest") == 0
    )

    assert store(installed).read_pointer() == "b"
    assert metadata(installed)["claude_version"] == "2.1.296"


def test_cross_os_platform_override_is_refused(project):
    foreign = "linux-x64" if not KEY.startswith("linux") else "win32-x64"

    with pytest.raises(PlatformError, match="another operating system"):
        install(project, standard_source(), "--platform", foreign)

    assert not runtime(project).exists()


def test_dry_run_install_writes_nothing_and_stays_offline(project, capsys):
    source = standard_source()

    assert install(project, source, "--dry-run") == 0

    assert source.pointer_requests == [] and source.manifest_requests == []
    assert not (project / ".local").exists()
    assert not (project / "bin").exists()
    assert "Would install Claude Code" in capsys.readouterr().err


def test_skip_install_writes_managed_files_without_a_runtime(project):
    source = standard_source()

    assert install(project, source, "--skip-install") == 0

    assert source.manifest_requests == []
    assert (project / "bin" / "claude-local.cmd").is_file()
    assert metadata(project)["claude_version"] is None
    assert not (runtime(project) / "slots").exists()


def test_toggles_only_rewrite_managed_files(installed):
    source = standard_source()

    assert install(installed, source, "--set-reasonable-permissions") == 0
    assert install(installed, source, "--set-update-checks", "off") == 0
    assert install(installed, source, "--set-windows-sandbox-warning", "off") == 0
    launch = json.loads(
        (runtime(installed) / "launch.json").read_text(encoding="utf-8")
    )
    assert launch["credential_passthrough"] is False
    assert install(installed, source, "--set-credential-passthrough", "on") == 0

    assert source.pointer_requests == [] and source.manifest_requests == []
    launch = json.loads(
        (runtime(installed) / "launch.json").read_text(encoding="utf-8")
    )
    assert launch["reasonable_permissions"] is True
    assert launch["update_checks"] is False
    assert launch["windows_sandbox_warning"] is False
    assert launch["credential_passthrough"] is True
    meta = metadata(installed)
    assert meta["reasonable_permissions_enabled"] is True
    assert meta["credential_passthrough"] is True
    assert meta["claude_version"] == "2.1.287"

    assert install(installed, source, "--clear-reasonable-permissions") == 0
    launch = json.loads(
        (runtime(installed) / "launch.json").read_text(encoding="utf-8")
    )
    assert launch["reasonable_permissions"] is False


def test_malformed_config_settings_refuses_before_any_change(project):
    settings = project / ".local" / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")

    with pytest.raises(SettingsFileError, match="refusing to rewrite"):
        install(project, standard_source())

    assert settings.read_text(encoding="utf-8") == "{not json"
    assert not runtime(project).exists()


def test_config_settings_merge_preserves_other_keys(project):
    settings = project / ".local" / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"model": "opus", "env": {"FOO": "bar", "DISABLE_UPDATES": "0"}}),
        encoding="utf-8",
    )

    assert install(project, standard_source()) == 0

    merged = json.loads(settings.read_text(encoding="utf-8"))
    assert merged["model"] == "opus"
    assert merged["env"]["FOO"] == "bar"
    assert merged["env"]["DISABLE_UPDATES"] == "1"
    assert merged["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert merged["disableDeepLinkRegistration"] == "disable"


def test_runtime_root_inside_claude_owned_paths_is_refused(project):
    with pytest.raises(ClaudeWranglerError, match="overlaps"):
        install(project, standard_source(), "--local-dir", ".local/claude/HOME/rt")
    with pytest.raises(ClaudeWranglerError, match="overlaps"):
        install(project, standard_source(), "--local-dir", ".local/claude/local")


def test_second_maintainer_fails_fast_and_changes_nothing(installed):
    from claude_wrangler.slots import MaintenanceLockError

    before = snapshot(installed)
    with store(installed).lock():
        with pytest.raises(MaintenanceLockError):
            install(installed, standard_source(), "--upgrade", "--channel", "latest")
    assert snapshot(installed) == before


def test_install_records_subcommands_that_reject_settings(project):
    services = make_services(standard_source())
    probes = []

    def probe(binary, env, cwd, subcommands):
        """Record the probe call and report one rejecting subcommand."""

        probes.append((Path(binary), dict(env), Path(cwd), list(subcommands)))
        return ["doctor"]

    services.settings_probe = probe
    assert install_like_operation(make_config(project), services) == 0

    record = store(project).slot_record("a")
    assert record.settings_rejected == ["doctor"]
    ((binary, env, cwd, subcommands),) = probes
    assert subcommands == record.subcommands
    # The probe runs the staged candidate in transaction scratch.
    assert ".staging" in binary.parts
    assert os.path.commonpath([env["HOME"], str(cwd)]) == str(cwd)
    assert os.path.commonpath([env["CLAUDE_CONFIG_DIR"], str(cwd)]) == str(cwd)
    spawned = []
    for args in (["mcp", "list"], ["doctor"]):
        code = launcher_core.main(
            args,
            environ={"PATH": ""},
            os_name=os.name,
            platform_is_wsl=False,
            core_path=str(runtime(project) / "launch.py"),
            exec_fn=lambda binary, argv, env: spawned.append(argv) or 0,
            spawn_fn=lambda argv, env: spawned.append(argv) or 0,
            stderr=io.StringIO(),
            node_probe=lambda env, os_name: (None, None),
            fetch_fn=lambda url, timeout: b"2.1.287",
        )
        assert code == 0
    mcp_argv, doctor_argv = spawned
    assert mcp_argv[1] == "--settings" and mcp_argv[3:] == ["mcp", "list"]
    assert doctor_argv[1:] == ["doctor"]


def test_release_base_url_and_download_settings_reach_the_source(project):
    recorder = Recorder()
    source = FakeReleaseSource(
        list(standard_source().releases.values()),
        {"stable": "2.1.287"},
        base_url="https://mirror.example.com/releases",
    )

    assert (
        install(
            project,
            source,
            "--release-base-url",
            "https://mirror.example.com/releases/",
            "--download-timeout-seconds",
            "7",
            recorder=recorder,
        )
        == 0
    )

    assert recorder.sources == [("https://mirror.example.com/releases", 7, None)]
    meta = metadata(project)
    assert meta["release_base_url"] == "https://mirror.example.com/releases"
    launch = json.loads((runtime(project) / "launch.json").read_text(encoding="utf-8"))
    assert launch["release_base_url"] == "https://mirror.example.com/releases"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_refreshes_catalog_without_touching_the_runtime(installed, capsys):
    source = standard_source()
    before = {
        k: v
        for k, v in snapshot(installed).items()
        if k.startswith(("slots/", "active"))
    }

    assert (
        update_operation(make_config(installed, "--update"), make_services(source)) == 0
    )

    assert sorted(source.pointer_requests) == ["latest", "stable"]
    assert sorted(source.manifest_requests) == ["2.1.287", "2.1.296"]
    assert source.downloads == []
    after = {
        k: v
        for k, v in snapshot(installed).items()
        if k.startswith(("slots/", "active"))
    }
    assert after == before
    meta = metadata(installed)
    assert meta["available_versions"] == {"stable": "2.1.287", "latest": "2.1.296"}
    assert meta["available_version_details"]["latest"]["build_date"] == (
        "2026-10-09T14:46:35Z"
    )
    assert meta["last_update_check"]["ok"] is True
    cache = json.loads((runtime(installed) / "update-check.json").read_text("utf-8"))
    assert cache == {
        "channel": "stable",
        "checked_at": "2026-10-12T12:00:00Z",
        "error": None,
        "installed_version": "2.1.287",
        "latest_version": "2.1.287",
        "pointer_version": "2.1.287",
    }
    output = capsys.readouterr().out
    assert (
        "does not perform an upgrade" in output.replace("\n", " ") or "NOTE" in output
    )
    assert "2.1.296" in output


def test_update_records_a_failed_channel_check(installed, capsys):
    source = standard_source()
    source.unavailable_channels["stable"] = "connection refused (test)"

    code = update_operation(make_config(installed, "--update"), make_services(source))

    assert code == 1
    cache = json.loads((runtime(installed) / "update-check.json").read_text("utf-8"))
    assert "connection refused" in cache["error"]
    meta = metadata(installed)
    assert meta["available_versions"]["latest"] == "2.1.296"
    assert "connection refused" in meta["last_update_check"]["errors"]["stable"]
    report = gather_inspection_report(
        make_config(installed, "--inspect"), make_services(source)
    )
    assert any("last update check failed" in item for item in report["warnings"])


def test_update_requires_a_managed_install(project):
    with pytest.raises(ClaudeWranglerError, match="No managed Claude install"):
        update_operation(
            make_config(project, "--update"), make_services(standard_source())
        )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_reactivates_the_previous_release(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0

    assert (
        rollback_operation(make_config(installed, "--rollback"), make_services(source))
        == 0
    )

    assert store(installed).read_pointer() == "a"
    meta = metadata(installed)
    assert meta["claude_version"] == "2.1.287"
    assert meta["version_source"] == "rollback to slot a"


def test_rollback_refuses_tampered_evidence(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    sig = runtime(installed) / "slots" / "a" / "manifest.json.sig"
    sig.write_bytes(b"tampered")

    with pytest.raises(ClaudeWranglerError, match="Refusing to roll back"):
        rollback_operation(make_config(installed, "--rollback"), make_services(source))

    assert store(installed).read_pointer() == "b"


def test_rollback_refuses_corrupt_binary_of_the_same_size(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    binary = slot_binary(installed, "a")
    data = bytearray(binary.read_bytes())
    data[-1] ^= 0xFF
    binary.write_bytes(bytes(data))

    with pytest.raises(ClaudeWranglerError, match="SHA-256"):
        rollback_operation(make_config(installed, "--rollback"), make_services(source))


def test_rollback_refuses_a_slot_in_use(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    processes = FakeProcessTable()
    processes.run_from(slot_binary(installed, "a"), pid=99)

    with pytest.raises(RuntimeInUseError, match="pid 99"):
        rollback_operation(
            make_config(installed, "--rollback"),
            make_services(source, processes=processes),
        )
    assert store(installed).read_pointer() == "b"


def test_rollback_without_a_second_slot_is_refused(installed):
    with pytest.raises(ClaudeWranglerError, match="no complete rollback slot"):
        rollback_operation(
            make_config(installed, "--rollback"), make_services(standard_source())
        )


def test_rollback_rejects_install_flags():
    with pytest.raises(SystemExit):
        parse_args(["--rollback", "--channel", "stable"])


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def test_repair_replaces_a_corrupt_active_slot_and_keeps_the_good_one(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    binary = slot_binary(installed, "b")
    data = bytearray(binary.read_bytes())
    data[0] ^= 0xFF
    binary.write_bytes(bytes(data))

    assert install(installed, source, "--repair-install") == 0

    slots = store(installed)
    pointer = slots.read_pointer()
    assert slots.slot_record(pointer).version == "2.1.296"
    other = "a" if pointer == "b" else "b"
    assert slots.slot_record(other).version == "2.1.287"
    assert slot_binary(installed, pointer).read_bytes() == fake_binary("2.1.296", KEY)


def test_repair_says_when_a_failed_redownload_leaves_the_older_slot_active(
    installed, capsys
):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    binary = slot_binary(installed, "b")
    data = bytearray(binary.read_bytes())
    data[0] ^= 0xFF  # same size, so only the offline hash check catches it
    binary.write_bytes(bytes(data))
    source.transport.bodies.clear()  # the re-download fails
    capsys.readouterr()

    with pytest.raises(ClaudeWranglerError):
        install(installed, source, "--repair-install")

    err = capsys.readouterr().err
    assert "slot a runs Claude Code 2.1.287 until the pinned 2.1.296" in err
    assert "keeps running 2.1.287" in err
    slots = store(installed)
    assert slots.read_pointer() == "a"
    assert metadata(installed)["claude_version"] == "2.1.296"


def test_repair_fixes_a_malformed_pointer_without_downloading(installed):
    source = standard_source()
    (runtime(installed) / "active-slot").write_bytes(b"garbage")

    assert install(installed, source, "--repair-install") == 0

    assert store(installed).read_pointer() == "a"
    assert source.downloads == []


def test_repair_chooses_the_verified_slot_whose_version_matches_metadata(
    installed, capsys
):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    slots = store(installed)
    assert slots.read_pointer() == "b"
    assert slots.slot_record("a").version == "2.1.287"
    assert metadata(installed)["claude_version"] == "2.1.296"
    record_b = (runtime(installed) / "slots" / "b" / ".complete.json").read_bytes()
    downloads = list(source.downloads)
    # Slot a sorts first, so only the version match can pick slot b.
    (runtime(installed) / "active-slot").write_bytes(b"garbage")
    capsys.readouterr()

    assert install(installed, source, "--repair-install") == 0

    err = capsys.readouterr().err
    assert "Repaired the active-slot pointer: slot b (Claude Code 2.1.296)" in err, err
    assert "Claude Code 2.1.296 already installed" in err
    assert slots.read_pointer() == "b"
    assert source.downloads == downloads
    assert slots.slot_record("a").version == "2.1.287"
    assert (
        runtime(installed) / "slots" / "b" / ".complete.json"
    ).read_bytes() == record_b
    assert metadata(installed)["claude_version"] == "2.1.296"


def test_install_without_repair_reports_a_malformed_pointer(installed):
    (runtime(installed) / "active-slot").write_bytes(b"x\r\n")

    with pytest.raises(ClaudeWranglerError, match="--repair-install"):
        install(installed, standard_source(), "--channel", "latest")


def test_repair_cleans_staging_and_idle_retired_entries(installed):
    rt = runtime(installed)
    (rt / ".staging" / "leftover").mkdir(parents=True)
    (rt / ".staging" / "leftover" / "partial").write_bytes(b"x")
    busy = rt / ".retired" / "busy"
    (busy / KEY).mkdir(parents=True)
    (busy / KEY / BINARY).write_bytes(b"running")
    (rt / ".retired" / "idle").mkdir()
    (rt / "fallback.json").write_text(
        json.dumps({"at": "x", "from_slot": "b", "to_slot": "a", "reason": "test"}),
        encoding="utf-8",
    )
    processes = FakeProcessTable()
    processes.run_from(busy / KEY / BINARY)

    assert (
        install(installed, standard_source(), "--repair-install", processes=processes)
        == 0
    )

    assert not (rt / ".staging" / "leftover").exists()
    assert not (rt / ".retired" / "idle").exists()
    assert busy.exists()
    assert not (rt / "fallback.json").exists()


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_runtime_generated_files_and_session_state(installed):
    ensure_gitignore_block(make_config(installed))
    (installed / ".local" / "claude" / "HOME" / "note.txt").write_text("x", "utf-8")

    code = uninstall_operation(
        make_config(installed, "--uninstall"), make_services(standard_source())
    )

    assert code == 0
    assert not (installed / ".local" / "claude").exists()
    assert not (installed / "bin").exists()
    assert not (installed / "README-LOCAL-Start-Claude.md").exists()
    assert GITIGNORE_BEGIN not in (installed / ".gitignore").read_text("utf-8")


def test_uninstall_refuses_while_the_runtime_is_in_use(installed):
    ensure_gitignore_block(make_config(installed))
    processes = FakeProcessTable()
    processes.run_from(slot_binary(installed, "a"))

    with pytest.raises(RuntimeInUseError):
        uninstall_operation(
            make_config(installed, "--uninstall"),
            make_services(standard_source(), processes=processes),
        )

    assert slot_binary(installed, "a").exists()
    assert (installed / "bin" / "claude-local").exists()


def test_uninstall_stops_the_background_service_when_allowed(installed):
    processes = FakeProcessTable()
    processes.run_from(slot_binary(installed, "a"))
    recorder = Recorder()

    code = uninstall_operation(
        make_config(installed, "--uninstall", "--stop-background-sessions"),
        make_services(standard_source(), processes=processes, recorder=recorder),
    )

    assert code == 0
    assert len(recorder.stops) == 1
    assert not runtime(installed).exists()


def test_uninstall_stops_before_session_state_when_a_binary_is_locked(
    installed, monkeypatch
):
    ensure_gitignore_block(make_config(installed))
    home_note = installed / ".local" / "claude" / "HOME" / "note.txt"
    home_note.write_text("keep", encoding="utf-8")
    original = operations.remove_runtime_tree

    def locked(path: Path) -> None:
        """Fail like Windows does for a running image."""

        if path.name == "slots":
            raise PermissionError(5, "Access is denied (simulated running image)")
        original(path)

    monkeypatch.setattr(operations, "remove_runtime_tree", locked)

    code = uninstall_operation(
        make_config(installed, "--uninstall"), make_services(standard_source())
    )

    assert code == 1
    assert home_note.exists()
    assert (installed / ".local" / "claude" / "settings.json").exists()
    assert GITIGNORE_BEGIN in (installed / ".gitignore").read_text("utf-8")


def test_uninstall_requires_ownership_proof(project):
    with pytest.raises(ClaudeWranglerError, match="ownership"):
        uninstall_operation(
            make_config(project, "--uninstall"), make_services(standard_source())
        )


def test_uninstall_refuses_edited_launcher_without_force(installed):
    launcher = installed / "bin" / "claude-local"
    launcher.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    with pytest.raises(ClaudeWranglerError, match="--force"):
        uninstall_operation(
            make_config(installed, "--uninstall"), make_services(standard_source())
        )
    assert runtime(installed).exists()


# ---------------------------------------------------------------------------
# Inspect and selftest
# ---------------------------------------------------------------------------


def test_inspect_reports_slots_pointer_and_generated_files(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    (runtime(installed) / "fallback.json").write_text(
        json.dumps({"at": "t", "from_slot": "b", "to_slot": "a", "reason": "r"}),
        encoding="utf-8",
    )

    report = gather_inspection_report(
        make_config(installed, "--inspect"), make_services(source)
    )

    rt = report["runtime"]
    assert rt["pointer"] == {"value": "b", "problem": None}
    assert rt["active_slot"] == "b" and rt["inactive_slot"] == "a"
    assert rt["slots"]["b"]["version"] == "2.1.296"
    assert rt["slots"]["a"]["version"] == "2.1.287"
    assert rt["slots"]["a"]["evidence_complete"] is True
    assert rt["slots"]["a"]["settings_rejected"] == []
    assert rt["fallback"]["data"]["from_slot"] == "b"
    assert all(entry["fresh"] for entry in report["generated_files"].values())
    assert report["config_settings"]["ok"] is True
    assert report["legacy"]["detected"] is False
    rings = report["runtime_environment"]["home_configuration"]["rings"]
    assert rings["ring1"]["HOME"].endswith(os.path.join(".local", "claude", "HOME"))
    assert rings["ring0"]["HOME"] is None
    assert rings["ring0"]["CLAUDE_CONFIG_DIR"] == rings["ring1"]["CLAUDE_CONFIG_DIR"]
    if os.name == "nt":
        assert rings["ring1"]["APPDATA"].endswith(
            os.path.join("HOME", "AppData", "Roaming")
        )
    assert report["runtime_environment"]["node_required"] is False
    assert report["issues"] == []


def test_inspect_never_reads_the_held_lock(installed):
    with store(installed).lock():
        report = gather_inspection_report(
            make_config(installed, "--inspect"), make_services(standard_source())
        )
    assert report["runtime"]["active_slot"] == "a"


def test_inspect_reports_stale_generated_files(installed):
    (runtime(installed) / "launch.py").write_text("# old\n", encoding="utf-8")

    report = gather_inspection_report(
        make_config(installed, "--inspect"), make_services(standard_source())
    )

    assert report["generated_files"]["launch_py"]["fresh"] is False
    assert any("stale" in item for item in report["warnings"])


def test_selftest_passes_for_a_healthy_install(installed, capsys):
    ensure_gitignore_block(make_config(installed))
    code = selftest_operation(
        make_config(installed, "--selftest"), make_services(standard_source())
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "[PASS] slot_a_verified" in output
    assert "[PASS] launch_core_matches_package" in output
    assert "[PASS] active_binary_version_runs" in output


@pytest.mark.parametrize("shared", [False, True])
def test_selftest_shows_the_default_ring_launcher_environment(
    installed, capsys, shared
):
    args = ("--shared-home",) if shared else ()
    assert install(installed, standard_source(), *args) == 0
    capsys.readouterr()

    selftest_operation(
        make_config(installed, "--selftest"), make_services(standard_source())
    )

    output = capsys.readouterr().out
    lines = output.splitlines()
    if shared:
        assert (
            "Launcher environment at the default ring (ring 0, shared user HOME):"
            in lines
        )
        assert "  HOME = (inherited from the caller)" in lines
    else:
        assert (
            "Launcher environment at the default ring (ring 1, project-local "
            "isolated HOME):" in lines
        )
        home = installed / ".local" / "claude" / "HOME"
        assert "  HOME = {}".format(home) in lines
        assert "  XDG_CONFIG_HOME = {}".format(home / ".config") in lines
    config_lines = [line for line in lines if line.startswith("  CLAUDE_CONFIG_DIR = ")]
    assert config_lines == [
        "  CLAUDE_CONFIG_DIR = {}".format(installed / ".local" / "claude")
    ]
    assert any(line.startswith("  XDG_DATA_HOME = ") for line in lines)
    assert any("claude-local --shared-home" in line for line in lines)


@pytest.mark.parametrize(
    "damage",
    ["evidence", "core", "project_settings"],
)
def test_selftest_fails_on_damage(installed, capsys, damage):
    rt = runtime(installed)
    if damage == "evidence":
        (rt / "slots" / "a" / "manifest.json").write_bytes(b"{}")
    elif damage == "core":
        (rt / "launch.py").write_bytes(launcher_core_source() + b"\n# edited\n")
    else:
        settings = installed / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"env": {"DISABLE_UPDATES": "0"}}), "utf-8")

    code = selftest_operation(
        make_config(installed, "--selftest"), make_services(standard_source())
    )

    output = capsys.readouterr().out
    assert code == 1
    expected = {
        "evidence": "[FAIL] slot_a_verified",
        "core": "[FAIL] launch_core_matches_package",
        "project_settings": "[FAIL] project_settings_keep_updates_disabled",
    }[damage]
    assert expected in output


def test_cli_install_reports_errors_without_traceback(project, capsys, monkeypatch):
    def boom(config, services=None):
        """Fail like an operation that refuses."""

        raise ClaudeWranglerError("simulated failure")

    monkeypatch.setattr("claude_wrangler.cli.install_like_operation", boom)
    from claude_wrangler.cli import main

    assert main([str(project)]) == 1
    captured = capsys.readouterr()
    assert "ERROR: simulated failure" in captured.err
    assert "Traceback" not in captured.err


def test_selftest_fails_on_an_explicit_node_selector_mismatch(
    installed, capsys, monkeypatch
):
    from claude_wrangler import environment

    ensure_gitignore_block(make_config(installed))
    (installed / ".nvmrc").write_text("18\n", encoding="utf-8")

    def fake_node(command):
        """Pretend node 20 is on PATH."""

        return {
            "requested_name": command,
            "resolved_path": "/usr/bin/" + command,
            "version": "v20.11.1" if command == "node" else None,
            "found": True,
            "required": False,
        }

    monkeypatch.setattr(environment, "resolve_command_metadata", fake_node)
    code = selftest_operation(
        make_config(installed, "--selftest"), make_services(standard_source())
    )
    assert code == 1
    assert "[FAIL] runtime_selector_alignment" in capsys.readouterr().out


def test_selftest_passes_without_node_on_path(installed, capsys, monkeypatch):
    from claude_wrangler import environment

    ensure_gitignore_block(make_config(installed))
    (installed / ".nvmrc").write_text("18\n", encoding="utf-8")
    monkeypatch.setattr(
        environment,
        "resolve_command_metadata",
        lambda command: {
            "requested_name": command,
            "resolved_path": None,
            "version": None,
            "found": False,
            "required": False,
        },
    )
    code = selftest_operation(
        make_config(installed, "--selftest"), make_services(standard_source())
    )
    assert code == 0, capsys.readouterr().out


def test_long_windows_paths_are_warned_about(capsys):
    from claude_wrangler.transaction import warn_long_windows_path

    assert warn_long_windows_path(Path("C:\\" + "x" * 250), os_name="nt") is True
    assert "260" in capsys.readouterr().err
    assert warn_long_windows_path(Path(r"C:\short"), os_name="nt") is False
    assert warn_long_windows_path(Path("/" + "x" * 300), os_name="posix") is False


def test_staging_names_are_short(installed):
    candidate = store(installed).new_staging_dir()
    assert len(candidate.name) == 12


def test_uninstall_dry_run_removes_nothing(installed, capsys):
    code = uninstall_operation(
        make_config(installed, "--uninstall", "--dry-run"),
        make_services(standard_source()),
    )
    assert code == 0
    assert runtime(installed).exists()
    assert (installed / "bin" / "claude-local").exists()
    assert "Would remove" in capsys.readouterr().err


def test_rollback_dry_run_keeps_the_pointer(installed):
    source = standard_source()
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    code = rollback_operation(
        make_config(installed, "--rollback", "--dry-run"), make_services(source)
    )
    assert code == 0
    assert store(installed).read_pointer() == "b"


def test_same_release_platforms_share_a_slot(installed):
    """A checkout shared with another OS keeps that OS's binary on upgrade."""

    other = "linux-x64" if not KEY.startswith("linux") else "win32-x64"
    slots = store(installed)
    record = slots.slot_record("a")
    # Pretend another OS already installed 2.1.296 into slot a's release.
    releases = {
        version: FakeRelease(
            version,
            {KEY: fake_binary(version, KEY), other: fake_binary(version, other)},
        )
        for version in ("2.1.287", "2.1.296")
    }
    source = FakeReleaseSource(list(releases.values()), {"latest": "2.1.296"})
    assert record.version == "2.1.287"
    # First move to 2.1.296 for this host only.
    assert install(installed, source, "--upgrade", "--channel", "latest") == 0
    active = slots.read_pointer()
    # Add the other platform's binary to the active slot as the other OS would.
    slot_dir = slots.slot_dir(active)
    binary = slot_dir / other / binary_filename(other)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(fake_binary("2.1.296", other))
    data = json.loads((slot_dir / ".complete.json").read_text("utf-8"))
    import hashlib

    data["platforms"][other] = {
        "binary": "{}/{}".format(other, binary_filename(other)),
        "sha256": hashlib.sha256(fake_binary("2.1.296", other)).hexdigest(),
        "size": len(fake_binary("2.1.296", other)),
    }
    (slot_dir / ".complete.json").write_text(json.dumps(data), "utf-8")
    # Corrupt this host's binary so a reinstall into the other slot happens,
    # carrying the other platform's verified binary along.
    host_binary = slot_dir / KEY / BINARY
    host_binary.write_bytes(b"short")

    assert install(installed, source, "--repair-install") == 0

    new_active = slots.read_pointer()
    new_record = slots.slot_record(new_active)
    assert new_record.version == "2.1.296"
    assert sorted(new_record.platforms) == sorted([KEY, other])
    assert (
        slots.slot_dir(new_active) / other / binary_filename(other)
    ).read_bytes() == (fake_binary("2.1.296", other))


# ---------------------------------------------------------------------------
# Maintenance lock (ab_runtime_slots.md requirement 4)
# ---------------------------------------------------------------------------


def managed_files(project: Path) -> Dict[str, Any]:
    """Capture the runtime plus generated files outside it."""

    state: Dict[str, Any] = dict(snapshot(project))
    for name in (".gitignore", "bin/claude-local", "bin/claude-local.cmd"):
        path = project / name
        state[name] = path.read_bytes() if path.exists() else None
    readme = project / "README-LOCAL-Start-Claude.md"
    state["readme"] = readme.read_bytes() if readme.exists() else None
    settings = project / ".local" / "claude" / "settings.json"
    state["settings"] = settings.read_bytes() if settings.exists() else None
    return state


@pytest.fixture
def upgraded(installed):
    """Return a project with 2.1.287 in slot a and 2.1.296 active in slot b."""

    assert (
        install(installed, standard_source(), "--upgrade", "--channel", "latest") == 0
    )
    return installed


MAINTAINERS = {
    "install": lambda project, services: install_like_operation(
        make_config(project), services
    ),
    "upgrade": lambda project, services: install_like_operation(
        make_config(project, "--upgrade", "--channel", "stable"), services
    ),
    "repair": lambda project, services: install_like_operation(
        make_config(project, "--repair-install"), services
    ),
    "toggle": lambda project, services: install_like_operation(
        make_config(project, "--set-update-checks", "off"), services
    ),
    "skip-install": lambda project, services: install_like_operation(
        make_config(project, "--skip-install"), services
    ),
    "update": lambda project, services: update_operation(
        make_config(project, "--update"), services
    ),
    "rollback": lambda project, services: rollback_operation(
        make_config(project, "--rollback"), services
    ),
    "uninstall": lambda project, services: uninstall_operation(
        make_config(project, "--uninstall"), services
    ),
}


@pytest.mark.parametrize("operation", sorted(MAINTAINERS))
def test_every_maintainer_fails_fast_while_the_lock_is_held(upgraded, operation):
    from claude_wrangler.slots import MaintenanceLockError

    source = standard_source()
    # Without the managed block, a maintainer would normally write it.
    (upgraded / ".gitignore").unlink()
    before = managed_files(upgraded)

    with store(upgraded).lock():
        with pytest.raises(MaintenanceLockError):
            MAINTAINERS[operation](upgraded, make_services(source))

    assert managed_files(upgraded) == before
    assert source.pointer_requests == []
    assert source.downloads == []


@pytest.mark.parametrize("argv", [[], ["--update"], ["--rollback"], ["--uninstall"]])
def test_cli_second_maintainer_leaves_gitignore_untouched(upgraded, argv, capsys):
    from claude_wrangler.cli import main

    (upgraded / ".gitignore").unlink()
    before = managed_files(upgraded)

    with store(upgraded).lock():
        assert main(argv + [str(upgraded)]) == 1

    assert "ERROR:" in capsys.readouterr().err
    assert managed_files(upgraded) == before
    assert not (upgraded / ".gitignore").exists()


def test_stale_state_read_before_the_lock_changes_nothing(installed):
    stale = make_config(installed)  # reads the 2.1.287 pin
    assert (
        install(installed, standard_source(), "--upgrade", "--channel", "latest") == 0
    )
    before = managed_files(installed)
    source = standard_source()

    with pytest.raises(operations.ManagedStateChangedError):
        install_like_operation(stale, make_services(source))

    assert managed_files(installed) == before
    assert store(installed).read_pointer() == "b"
    assert metadata(installed)["claude_version"] == "2.1.296"
    assert source.downloads == []


@pytest.mark.parametrize("operation", ["update", "rollback", "uninstall"])
def test_other_operations_refuse_stale_state(installed, operation):
    stale = make_config(
        installed,
        *{"update": ["--update"], "rollback": ["--rollback"]}.get(
            operation, ["--uninstall"]
        ),
    )
    assert (
        install(installed, standard_source(), "--upgrade", "--channel", "latest") == 0
    )
    before = managed_files(installed)
    runner = {
        "update": update_operation,
        "rollback": rollback_operation,
        "uninstall": uninstall_operation,
    }[operation]

    with pytest.raises(operations.ManagedStateChangedError):
        runner(stale, make_services(standard_source()))

    assert managed_files(installed) == before


def test_cli_rereads_state_another_maintainer_published(installed, monkeypatch, capsys):
    """Maintainer A upgrades between B's state read and B's lock."""

    from claude_wrangler import cli

    source = standard_source()
    monkeypatch.setattr(operations, "default_services", lambda: make_services(source))
    real_config_from_args = cli.config_from_args
    reads = []

    def racing_config_from_args(args):
        """Return B's config, then let A publish before B takes the lock."""

        config = real_config_from_args(args)
        reads.append(config.claude_version)
        if len(reads) == 1:
            assert install(installed, source, "--upgrade", "--channel", "latest") == 0
        return config

    monkeypatch.setattr(cli, "config_from_args", racing_config_from_args)

    assert cli.main([str(installed)]) == 0

    assert reads == ["2.1.287", "2.1.296"]
    assert "Re-reading the state" in capsys.readouterr().err
    # B's plain install kept A's upgrade instead of re-promoting 2.1.287.
    assert store(installed).read_pointer() == "b"
    assert metadata(installed)["claude_version"] == "2.1.296"
    assert GITIGNORE_BEGIN in (installed / ".gitignore").read_text("utf-8")


def test_cli_gives_up_after_repeated_state_changes(installed, monkeypatch, capsys):
    from claude_wrangler import cli

    def always_changed(args):
        """Simulate a state change on every attempt."""

        raise operations.ManagedStateChangedError("state changed (test)")

    monkeypatch.setattr(cli, "run_operation", always_changed)

    assert cli.main([str(installed)]) == 1
    err = capsys.readouterr().err
    assert err.count("Re-reading the state") == cli.STATE_CHANGE_ATTEMPTS - 1
    assert "ERROR: state changed (test) Rerun the command" in err


def test_uninstall_holds_the_lock_from_checks_through_session_state(
    upgraded, monkeypatch
):
    from claude_wrangler.slots import MaintenanceLockError

    ensure_gitignore_block(make_config(upgraded))
    held: Dict[str, bool] = {}

    def lock_is_held() -> bool:
        """Return whether a second maintainer would be refused right now."""

        probe = store(upgraded).lock()
        try:
            probe.acquire()
        except MaintenanceLockError:
            return True
        probe.release()
        return False

    def spy(name, original):
        """Wrap an uninstall step to record whether the lock is held."""

        def wrapper(*args, **kwargs):
            """Record the lock state, then run the real step."""

            held[name] = lock_is_held()
            return original(*args, **kwargs)

        return wrapper

    for name in ("require_idle", "remove_generated_files", "remove_tree_except"):
        monkeypatch.setattr(operations, name, spy(name, getattr(operations, name)))

    code = uninstall_operation(
        make_config(upgraded, "--uninstall"), make_services(standard_source())
    )

    assert code == 0
    assert held == {
        "require_idle": True,
        "remove_generated_files": True,
        "remove_tree_except": True,
    }
    assert not (upgraded / ".local").exists()
    assert GITIGNORE_BEGIN not in (upgraded / ".gitignore").read_text("utf-8")


# ---------------------------------------------------------------------------
# Repository-supplied state is never trusted as verified (SEC-1, SEC-3, SEC-5)
# ---------------------------------------------------------------------------


def plant_slot(project: Path, *, version: str = "2.1.296", name: str = "a") -> Path:
    """Plant a slot a hostile repository could supply (see native_fakes)."""

    return plant_unverified_slot(runtime(project), version, KEY, name=name)


def test_plain_install_never_reuses_a_planted_slot(project, capsys):
    plant_slot(project)
    source = UnreachableSource()

    with pytest.raises(ClaudeWranglerError, match="network blocked"):
        install(project, source)

    err = capsys.readouterr().err
    assert "already installed" not in err
    assert "failed full offline verification" in err
    assert source.contacted  # it had to fetch a verified release instead
    for path in (
        project / "bin" / "claude-local",
        project / "bin" / "claude-local.cmd",
        runtime(project) / "launch.py",
        runtime(project) / "launch.json",
    ):
        assert not path.exists(), path


def test_plain_install_replaces_a_planted_slot_with_a_verified_release(project):
    plant_slot(project)
    source = standard_source()

    assert install(project, source) == 0

    slots = store(project)
    # The planted record's version is not trusted as the pin either: the
    # stable channel decides, and the verified release goes into slot b.
    assert slots.read_pointer() == "b"
    assert slots.slot_record("b").version == "2.1.287"
    assert metadata(project)["claude_version"] == "2.1.287"
    assert len(source.downloads) == 1
    # No launcher ever runs the planted slot: it carries no stamp.
    check = launcher_core.check_slot(str(runtime(project)), "a", KEY)
    assert not check.ok and "verification stamp" in check.reason


def test_launcher_refuses_a_binary_swapped_after_verification(installed):
    binary = slot_binary(installed, "a")
    swapped = b"S" * binary.stat().st_size
    time.sleep(0.05)
    incoming = binary.with_name("incoming")
    incoming.write_bytes(swapped)
    os.replace(str(incoming), str(binary))
    spawns: List[Any] = []
    stderr = io.StringIO()

    code = launcher_core.main(
        ["--version"],
        environ={"PATH": os.environ.get("PATH", "")},
        core_path=str(runtime(installed) / "launch.py"),
        exec_fn=lambda path, argv, env: spawns.append(argv),
        spawn_fn=lambda argv, env: spawns.append(argv) or 0,
        stderr=stderr,
        stdout=io.StringIO(),
        fetch_fn=lambda url, timeout: b"",
        platform_key=KEY,
        node_probe=lambda environ, os_name: (None, None),
    )

    assert code != 0
    assert spawns == []
    assert "changed after claude-wrangler last verified it" in stderr.getvalue()


def test_repair_restamps_a_moved_checkout_without_downloading(installed):
    # A copy of the checkout (backup restore) gets new file identities; the
    # launcher refuses until repair re-verifies offline and stamps again.
    stamp = runtime(installed) / "slots" / "a" / ".verified.json"
    stamp.write_text(
        json.dumps({"schema": 1, "binaries": {KEY: {"size": 1, "mtime_ns": 1}}}),
        encoding="utf-8",
    )
    assert store(installed).stamp_problem("a", KEY) is not None
    source = standard_source()

    assert install(installed, source, "--repair-install") == 0

    assert store(installed).stamp_problem("a", KEY) is None
    assert source.downloads == []


def git(project: Path, *args: str) -> str:
    """Run git in the project and return its output."""

    # core.longpaths lets Git for Windows write objects under a deep
    # temporary directory; it changes nothing elsewhere.
    completed = subprocess.run(
        ["git", "-c", "core.longpaths=true", "-C", str(project)] + list(args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return completed.stdout.decode("utf-8", "replace")


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    """Return a project that is a real git work tree."""

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


def test_install_refuses_when_git_tracks_runtime_files(git_project, capsys):
    from claude_wrangler.services import list_tracked_files

    plant_slot(git_project)
    git(git_project, "add", "-f", ".local/claude/wrangler/active-slot")
    source = standard_source()
    services = make_services(source, tracked_files=list_tracked_files)

    with pytest.raises(ClaudeWranglerError) as raised:
        install_like_operation(make_config(git_project), services)

    assert ".local/claude/wrangler/active-slot" in str(raised.value)
    assert "--allow-tracked-state" in str(raised.value)
    assert source.downloads == [] and source.pointer_requests == []
    assert not (git_project / "bin").exists()


def test_allow_tracked_state_overrides_the_refusal(git_project, capsys):
    from claude_wrangler.services import list_tracked_files

    (git_project / ".local" / "claude").mkdir(parents=True)
    (git_project / ".local" / "claude" / "settings.json").write_text(
        "{}", encoding="utf-8"
    )
    git(git_project, "add", "-f", ".local/claude/settings.json")
    services = make_services(standard_source(), tracked_files=list_tracked_files)

    code = install_like_operation(
        make_config(git_project, "--allow-tracked-state"), services
    )

    assert code == 0
    assert "continuing because --allow-tracked-state" in capsys.readouterr().err


def test_tracked_metadata_pin_and_host_are_refused(git_project):
    # A tracked metadata file could pin an old signed release or add its own
    # release host without the operator choosing either.
    from claude_wrangler.services import list_tracked_files

    root = runtime(git_project)
    root.mkdir(parents=True)
    (root / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "claude_version": "2.1.207",
                "claude_channel": "stable",
                "release_base_url": "https://mirror.example.invalid/releases",
                "version_source": "default stable channel request",
            }
        ),
        encoding="utf-8",
    )
    git(git_project, "add", "-f", ".local/claude/wrangler/.claude-wrangler.json")
    source = standard_source()
    services = make_services(source, tracked_files=list_tracked_files)

    with pytest.raises(ClaudeWranglerError, match="git tracks"):
        install_like_operation(make_config(git_project), services)

    assert source.downloads == [] and source.manifest_requests == []


def test_recorded_release_host_is_named_before_it_is_contacted(
    project, monkeypatch, capsys
):
    mirror = "https://mirror.example.invalid/releases"
    first = FakeReleaseSource(standard_releases(), {"stable": "2.1.287"}, mirror)
    assert install(project, first, "--release-base-url", mirror) == 0
    events: List[str] = []
    source = FakeReleaseSource(
        standard_releases(), {"stable": "2.1.287", "latest": "2.1.296"}, mirror
    )
    services = make_services(source)
    original_factory = services.release_source

    def recording_factory(*args):
        """Record the first network-facing call."""

        events.append("release source")
        return original_factory(*args)

    services.release_source = recording_factory
    monkeypatch.setattr(
        operations, "eprint", lambda message: events.append(str(message))
    )
    capsys.readouterr()

    assert (
        install_like_operation(make_config(project, "--channel", "latest"), services)
        == 0
    )

    notice = next(
        index
        for index, event in enumerate(events)
        if "records the release host" in event
    )
    assert mirror in events[notice]
    assert notice < events.index("release source")
    assert "Release host: {}".format(mirror) in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Exact slot contents (SEC-3)
# ---------------------------------------------------------------------------


def test_offline_verification_rejects_a_planted_dll_beside_the_binary(installed):
    from claude_wrangler.transaction import verify_slot_offline

    (slot_binary(installed, "a").parent / "version.dll").write_bytes(b"MZ planted")

    result = verify_slot_offline(
        store(installed), "a", make_services(standard_source()), KEY
    )

    assert not result.ok
    assert "version.dll" in result.problem


def test_selftest_fails_on_an_extra_file_in_a_slot(installed, capsys):
    (runtime(installed) / "slots" / "a" / "extra.txt").write_text("x", "utf-8")

    code = selftest_operation(
        make_config(installed, "--selftest"), make_services(standard_source())
    )

    assert code == 1
    assert "extra.txt" in capsys.readouterr().out


def test_repair_replaces_a_slot_that_holds_extra_files(installed):
    platform_dir = slot_binary(installed, "a").parent
    (platform_dir / "version.dll").write_bytes(b"MZ planted")
    source = standard_source()

    assert install(installed, source, "--repair-install") == 0

    slots = store(installed)
    active = slots.read_pointer()
    assert active == "b"
    assert sorted(
        path.name for path in slot_binary(installed, active).parent.iterdir()
    ) == [BINARY]
    # The slot that failed lost its stamp, so no launcher falls back to it.
    assert slots.stamp_problem("a", KEY) is not None


# ---------------------------------------------------------------------------
# Lifecycle (LC-1, LC-2, LC-4, LC-5, LC-6, LC-8, LC-11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["--uninstall", "--rollback"])
def test_dry_run_never_stops_background_sessions(upgraded, operation, capsys):
    processes = FakeProcessTable()
    processes.run_from(slot_binary(upgraded, "a"))
    recorder = Recorder()
    services = make_services(standard_source(), recorder=recorder, processes=processes)
    before = managed_files(upgraded)
    config = make_config(upgraded, operation, "--dry-run", "--stop-background-sessions")

    if operation == "--uninstall":
        code = uninstall_operation(config, services)
    else:
        code = rollback_operation(config, services)

    assert code == 0
    assert recorder.stops == []
    assert managed_files(upgraded) == before
    assert "dry run; not stopped" in capsys.readouterr().err


def test_partial_uninstall_keeps_the_ownership_proof_for_a_rerun(
    installed, monkeypatch, capsys
):
    real_remove = operations.remove_runtime_tree

    def locked_slots(path):
        """Fail like Windows refusing to delete a binary something holds."""

        if "slots" in Path(path).parts:
            raise PermissionError(errno.EACCES, "in use", str(path))
        return real_remove(path)

    monkeypatch.setattr(operations, "remove_runtime_tree", locked_slots)

    code = uninstall_operation(
        make_config(installed, "--uninstall"), make_services(standard_source())
    )

    assert code == 1
    assert (runtime(installed) / METADATA_FILENAME).is_file()
    assert (runtime(installed) / "launch.py").is_file()
    assert "rerunning --uninstall finishes" in capsys.readouterr().err

    monkeypatch.setattr(operations, "remove_runtime_tree", real_remove)
    code = uninstall_operation(
        make_config(installed, "--uninstall"), make_services(standard_source())
    )

    assert code == 0
    assert not (installed / ".local").exists()


def test_non_ascii_local_dir_is_refused_before_any_download(project):
    source = standard_source()

    with pytest.raises(ClaudeWranglerError, match="not ASCII"):
        install(project, source, "--local-dir", ".local/claude/wränglér")

    assert source.downloads == [] and source.manifest_requests == []
    assert source.pointer_requests == []
    assert not (project / ".local" / "claude" / "wränglér").exists()


def test_non_ascii_local_dir_state_can_always_be_uninstalled(project):
    local_dir = ".local/claude/wränglér"
    root = project / ".local" / "claude" / "wränglér"
    (root / "slots" / "a").mkdir(parents=True)
    (root / "active-slot").write_bytes(b"a\n")
    (root / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "claude_version": "2.1.287",
                "paths": {"local_dir": local_dir},
            }
        ),
        encoding="utf-8",
    )

    code = uninstall_operation(
        make_config(project, "--uninstall", "--force", "--local-dir", local_dir),
        make_services(standard_source()),
    )

    assert code == 0
    assert not root.exists()


def test_state_changed_while_it_was_read_reruns_from_a_fresh_read(
    installed, monkeypatch
):
    from claude_wrangler import config as config_module
    from claude_wrangler.config import config_from_args

    real_read = config_module.read_existing_state
    fired: List[bool] = []

    def read_then_upgrade(layout):
        """Return the state, then let another maintainer upgrade at once."""

        state = real_read(layout)
        if not fired:
            fired.append(True)
            assert (
                install(
                    installed, standard_source(), "--upgrade", "--channel", "latest"
                )
                == 0
            )
        return state

    monkeypatch.setattr(config_module, "read_existing_state", read_then_upgrade)

    with pytest.raises(operations.ManagedStateChangedError):
        config_from_args(parse_args([str(installed)]))

    assert metadata(installed)["claude_version"] == "2.1.296"
    assert store(installed).read_pointer() == "b"


def test_interrupted_publish_does_not_download_the_pinned_release_again(
    installed, monkeypatch, capsys
):
    real_write = operations.write_metadata
    failures: List[bool] = []

    def full_disk_once(*args, **kwargs):
        """Fail the first metadata write like a full disk."""

        if not failures:
            failures.append(True)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(operations, "write_metadata", full_disk_once)
    upgrade_source = standard_source()
    with pytest.raises(ClaudeWranglerError, match="Rerun the same command"):
        install(installed, upgrade_source, "--upgrade", "--channel", "latest")
    assert store(installed).read_pointer() == "b"
    assert metadata(installed)["claude_version"] == "2.1.287"

    source = standard_source()
    assert install(installed, source) == 0

    assert source.downloads == []
    assert store(installed).read_pointer() == "a"
    assert metadata(installed)["claude_version"] == "2.1.287"
    assert "made it active without downloading" in capsys.readouterr().err


def test_symlinked_config_settings_is_refused_before_any_change(project, tmp_path):
    shared = tmp_path / "shared-settings.json"
    shared.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    link = project / ".local" / "claude" / "settings.json"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(str(shared), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted on this host")
    source = standard_source()

    with pytest.raises(SettingsFileError, match="symbolic link"):
        install(project, source)

    assert source.downloads == [] and source.pointer_requests == []
    assert not runtime(project).exists()
    assert json.loads(shared.read_text("utf-8")) == {"theme": "dark"}


def test_symlinked_config_settings_that_already_has_the_keys_is_accepted(
    project, tmp_path
):
    shared = tmp_path / "shared-settings.json"
    shared.write_text(
        json.dumps(
            {
                "env": {
                    "DISABLE_UPDATES": "1",
                    "DISABLE_AUTOUPDATER": "1",
                    "DISABLE_INSTALLATION_CHECKS": "1",
                },
                "disableDeepLinkRegistration": "disable",
            }
        ),
        encoding="utf-8",
    )
    link = project / ".local" / "claude" / "settings.json"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(str(shared), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted on this host")

    assert install(project, standard_source()) == 0
    assert link.is_symlink()


def test_failed_uninstall_lock_never_touches_another_maintainers_lock(
    project, monkeypatch
):
    # The runtime root does not exist yet (an npm-era layout has none), so
    # uninstall would create it; another maintainer creates it and takes the
    # lock first.
    from claude_wrangler import slots as slots_module
    from claude_wrangler.slots import MaintenanceLockError

    legacy = project / ".local" / "claude" / "npm"
    legacy.mkdir(parents=True)
    (legacy / "package.json").write_text(
        json.dumps({"name": "claude-local-managed-install"}), encoding="utf-8"
    )
    other = store(project).lock()
    real_acquire = slots_module.MaintenanceLock.acquire
    unlinked: List[str] = []
    real_unlink = Path.unlink

    def acquire(self):
        """Let the other maintainer win the race, then try ours."""

        if self is not other and not other.held:
            real_acquire(other)
        return real_acquire(self)

    def unlink(self, *args, **kwargs):
        """Record every Path.unlink."""

        unlinked.append(str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(slots_module.MaintenanceLock, "acquire", acquire)
    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        with pytest.raises(MaintenanceLockError):
            uninstall_operation(
                make_config(project, "--uninstall", "--force"),
                make_services(standard_source()),
            )
        assert other.held
        assert other.path.is_file()
        assert str(other.path) not in unlinked
    finally:
        other.release()


def test_inspect_and_selftest_report_a_missing_stamp_and_tracked_files(
    installed, capsys
):
    (runtime(installed) / "slots" / "a" / ".verified.json").unlink()
    services = make_services(
        standard_source(),
        tracked_files=lambda root, paths: [".local/claude/settings.json"],
    )

    report = gather_inspection_report(make_config(installed, "--inspect"), services)

    assert report["runtime"]["slots"]["a"]["stamp_ok"] is False
    assert any("will not launch" in issue for issue in report["issues"])
    assert report["state"]["tracked_managed_files"] == [".local/claude/settings.json"]
    assert any("git tracks" in issue for issue in report["issues"])

    code = selftest_operation(make_config(installed, "--selftest"), services)

    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] active_slot_stamp_matches" in out
    assert "[FAIL] managed_files_untracked" in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_install_makes_the_session_directories_private(project):
    config_dir = project / ".local" / "claude"
    (config_dir / "HOME").mkdir(parents=True)
    os.chmod(str(config_dir), 0o755)
    os.chmod(str(config_dir / "HOME"), 0o755)

    assert install(project, standard_source()) == 0

    for path in (config_dir, config_dir / "HOME"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_a_slot_lacking_this_hosts_platform_keeps_its_stamp(installed):
    # A checkout shared with WSL: the other system's maintenance finds the
    # slot intact but without its own platform. It must not un-stamp the
    # slot this system launches.
    from claude_wrangler.transaction import verify_and_stamp

    foreign = "darwin-arm64" if not KEY.startswith("darwin") else "linux-x64"

    result = verify_and_stamp(
        store(installed), "a", make_services(standard_source()), foreign
    )

    assert not result.ok
    assert "no {} binary".format(foreign) in result.problem
    assert store(installed).stamp_problem("a", KEY) is None


# Case-variant tracked state, lock-less filesystems, PATH separators, ACLs ------


def case_insensitive_filesystem(root: Path) -> bool:
    """Return True when ``root`` treats names that differ in case as one."""

    probe = root / "Case-Probe"
    probe.mkdir()
    try:
        return (root / "case-probe").exists()
    finally:
        probe.rmdir()


def test_install_refuses_a_case_variant_tracked_runtime_path(git_project):
    # On a case-insensitive filesystem a tracked `.LOCAL/Claude/...` is the
    # managed `.local/claude/...`, while git compares pathspecs by case. A
    # pull writes such an entry into the existing `.local/claude` directory,
    # so the index and the directory on disk disagree in case.
    from claude_wrangler.services import list_tracked_files

    if not case_insensitive_filesystem(git_project):
        pytest.skip("the filesystem distinguishes names that differ in case")
    on_disk = git_project / ".local" / "claude" / "settings.json"
    on_disk.parent.mkdir(parents=True)
    on_disk.write_text("{}", encoding="utf-8")
    blob = git(git_project, "hash-object", "-w", str(on_disk)).strip()
    git(
        git_project,
        "update-index",
        "--add",
        "--cacheinfo",
        "100644,{},.LOCAL/Claude/settings.json".format(blob),
    )
    source = standard_source()
    services = make_services(source, tracked_files=list_tracked_files)

    with pytest.raises(ClaudeWranglerError, match="git tracks") as raised:
        install_like_operation(make_config(git_project), services)

    assert ".LOCAL/Claude/settings.json" in str(raised.value)
    assert source.downloads == [] and source.pointer_requests == []


def test_uninstall_leaves_nothing_behind_on_a_filesystem_without_locks(
    project, monkeypatch
):
    # Nobody can hold a lock on such a filesystem, so the lock file and the
    # runtime-root scaffolding this run created go again.
    from claude_wrangler import slots as slots_module
    from claude_wrangler.slots import MaintenanceLockError

    legacy = project / ".local" / "claude" / "npm"
    legacy.mkdir(parents=True)
    (legacy / "package.json").write_text(
        json.dumps({"name": "claude-local-managed-install"}), encoding="utf-8"
    )

    def unsupported(descriptor):
        """Fail like flock on a filesystem without lock support."""

        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(slots_module, "_lock_descriptor", unsupported)

    with pytest.raises(MaintenanceLockError, match="does not support"):
        uninstall_operation(
            make_config(project, "--uninstall", "--force"),
            make_services(standard_source()),
        )

    assert not runtime(project).exists()
    assert legacy.is_dir()


def test_uninstall_keeps_a_lock_file_it_did_not_create(installed, monkeypatch):
    from claude_wrangler import slots as slots_module
    from claude_wrangler.slots import MaintenanceLockError

    lock_file = runtime(installed) / ".lock"
    assert lock_file.is_file()

    def unsupported(descriptor):
        """Fail like flock on a filesystem without lock support."""

        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(slots_module, "_lock_descriptor", unsupported)

    with pytest.raises(MaintenanceLockError):
        uninstall_operation(
            make_config(installed, "--uninstall"), make_services(standard_source())
        )

    assert lock_file.is_file()


def test_install_refuses_a_runtime_root_with_the_path_separator(tmp_path):
    root = tmp_path / "pro{}ject".format(os.pathsep)
    (root / ".git").mkdir(parents=True)
    source = standard_source()

    with pytest.raises(ClaudeWranglerError, match="PATH separator"):
        install(root, source)

    assert source.downloads == []
    assert not (root / "bin").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_uninstall_on_windows_mentions_credential_manager(installed, capsys, dry_run):
    args = ["--uninstall"] + (["--dry-run"] if dry_run else [])
    services = make_services(standard_source(), key="win32-x64")

    assert uninstall_operation(make_config(installed, *args), services) == 0

    assert "Windows Credential Manager" in capsys.readouterr().out


def test_uninstall_elsewhere_does_not_mention_credential_manager(installed, capsys):
    services = make_services(standard_source(), key="linux-x64")

    assert uninstall_operation(make_config(installed, "--uninstall"), services) == 0

    assert "Credential Manager" not in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_install_makes_the_session_directories_private_on_windows(project, capsys):
    from claude_wrangler import launcher_core as core

    config_dir = project / ".local" / "claude"
    (config_dir / "HOME").mkdir(parents=True)
    # Give the config dir a grant other local accounts would inherit.
    grant_users_modify(config_dir)

    assert install(project, standard_source()) == 0

    assert "Restricting" in capsys.readouterr().err
    user = core.windows_current_user_sid()
    for path in (config_dir, config_dir / "HOME", runtime(project)):
        summary = core.windows_security_summary(str(path))
        assert summary is not None
        principals = core.private_dacl_principals(user, summary[0])
        assert core.dacl_is_private(summary[1], principals), path


def grant_users_modify(path: Path) -> None:
    """Add an inheritable Modify grant for BUILTIN\\Users with icacls."""

    completed = subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_install_warns_about_a_checkout_other_accounts_can_change(project, capsys):
    grant_users_modify(project)

    assert install(project, standard_source()) == 0

    error = capsys.readouterr().err
    assert "grants Users write access" in error
    assert "unsafe for multi-account use" in error


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_a_private_checkout_gets_no_shared_checkout_warning(project, capsys):
    assert install(project, standard_source()) == 0

    assert "unsafe for multi-account use" not in capsys.readouterr().err
