"""Offline tests for install, upgrade, update, rollback, repair, and uninstall.

Every test drives the real operations, slot store, promotion, metadata, and
generated files; only the network, signature verification of fake
manifests, binary execution, the process table, and the clock are faked
(see tests/native_fakes.py).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Dict

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
    FakeRelease,
    FakeReleaseSource,
    Recorder,
    fake_binary,
    host_key,
    make_config,
    make_services,
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

    assert source.pointer_requests == [] and source.manifest_requests == []
    launch = json.loads(
        (runtime(installed) / "launch.json").read_text(encoding="utf-8")
    )
    assert launch["reasonable_permissions"] is True
    assert launch["update_checks"] is False
    assert launch["windows_sandbox_warning"] is False
    meta = metadata(installed)
    assert meta["reasonable_permissions_enabled"] is True
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
