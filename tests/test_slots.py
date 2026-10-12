"""Tests for A/B runtime slots, promotion, locking, and in-use detection."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.slots import (
    COMPLETION_RECORD_NAME,
    POINTER_NAME,
    SLOT_NAMES,
    CompletionRecord,
    CompletionRecordError,
    MaintenanceLock,
    MaintenanceLockError,
    PointerCommittedError,
    PointerCommittedInterrupt,
    PointerError,
    PromotionError,
    RunningProcess,
    RuntimeStore,
    ServiceStopError,
    SlotError,
    SlotInUseError,
    UnsafePathError,
    check_runtime_root_placement,
    find_processes_under,
    is_link_like,
    list_process_images,
    make_slot_free_check,
    processes_under,
    remove_tree,
    stop_background_service,
)

SRC = Path(__file__).resolve().parents[1] / "src"
PLATFORM = "linux-x64"


def sha256(data: bytes) -> str:
    """Return the hex SHA-256 of some bytes."""

    return hashlib.sha256(data).hexdigest()


def make_store(tmp_path: Path) -> RuntimeStore:
    """Return a store at the default runtime-root spelling inside a project."""

    project = tmp_path / "project"
    project.mkdir()
    return RuntimeStore(
        project / ".local" / "claude" / "wrangler", project_root=project
    )


def build_candidate(store, slot, version, payload=None, platform=PLATFORM):
    """Stage a candidate with a binary and evidence; return it and its record."""

    candidate = store.new_staging_dir()
    binary_name = "claude.exe" if platform.startswith("win32") else "claude"
    relpath = "{}/{}".format(platform, binary_name)
    if payload is None:
        payload = ("binary for " + version + "\n").encode("ascii") * 64
    (candidate / platform).mkdir()
    (candidate / platform / binary_name).write_bytes(payload)
    manifest = json.dumps({"version": version}).encode("utf-8")
    (candidate / "manifest.json").write_bytes(manifest)
    (candidate / "manifest.json.sig").write_bytes(b"pgp-signature")
    (candidate / "manifest.json.raw-sig.json").write_bytes(b"{}")
    record = CompletionRecord(
        slot=slot,
        version=version,
        channel="stable",
        platforms={
            platform: {
                "binary": relpath,
                "sha256": sha256(payload),
                "size": len(payload),
            }
        },
        manifest_sha256=sha256(manifest),
        build_date="2026-10-01T00:00:00Z",
        commit="0123456789abcdef",
        schemes=["openpgp", "raw-rsa"],
        subcommands=["daemon", "mcp"],
        verified_at="2026-10-12T00:00:00Z",
        promoted_by="claude-wrangler test",
    )
    return candidate, record


def no_op_free(path: Path) -> None:
    """An ensure_slot_free check that always passes."""


def install(store: RuntimeStore, version: str) -> str:
    """Promote one release into the inactive slot and return that slot."""

    slot = store.inactive_slot()
    candidate, record = build_candidate(store, slot, version)
    assert store.promote(candidate, record, ensure_slot_free=no_op_free) == slot
    return slot


def slot_version(store: RuntimeStore, name: str):
    """Return the recorded version in a slot, or None."""

    record = store.slot_record(name)
    return None if record is None else record.version


def snapshot(root: Path) -> dict:
    """Return every directory and file (with bytes) under a tree."""

    result = {}
    for current, dirnames, filenames in os.walk(root):
        base = Path(current)
        for dirname in dirnames:
            result[(base / dirname).relative_to(root).as_posix() + "/"] = None
        for filename in filenames:
            path = base / filename
            if filename == ".lock":
                # A held Windows byte-range lock makes the file unreadable;
                # its existence is what matters.
                result[path.relative_to(root).as_posix()] = None
                continue
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def make_dir_link(target: Path, link: Path) -> None:
    """Create a directory junction (Windows) or symbolic link (POSIX)."""

    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def binary_path(store: RuntimeStore, slot: str) -> Path:
    """Return the test platform binary path inside a slot."""

    return store.slot_dir(slot) / PLATFORM / "claude"


# Pointer -------------------------------------------------------------------


def test_read_pointer_absent_and_strict_values(tmp_path):
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    assert store.read_pointer() is None
    assert store.active_slot() is None
    assert store.inactive_slot() == "a"

    for name in SLOT_NAMES:
        store.pointer_path.write_bytes((name + "\n").encode("ascii"))
        assert store.read_pointer() == name


@pytest.mark.parametrize(
    "content",
    [b"", b"a", b"b", b"a\r\n", b"c\n", b" a\n", b"a\n\n", b"A\n", b"slots/a\n"],
)
def test_read_pointer_rejects_anything_but_exact_form(tmp_path, content):
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    store.pointer_path.write_bytes(content)

    with pytest.raises(PointerError):
        store.read_pointer()


def test_read_pointer_rejects_directory(tmp_path):
    store = make_store(tmp_path)
    store.pointer_path.mkdir(parents=True)

    with pytest.raises(PointerError):
        store.read_pointer()


def test_pointer_naming_incomplete_slot_is_invalid(tmp_path):
    store = make_store(tmp_path)
    store.slot_dir("a").mkdir(parents=True)
    store.pointer_path.write_bytes(b"a\n")

    assert store.read_pointer() == "a"
    with pytest.raises(PointerError, match="repair-install"):
        store.active_slot()
    with pytest.raises(PointerError):
        store.inactive_slot()


# Completion record ---------------------------------------------------------


def sample_record(**overrides) -> CompletionRecord:
    """Return a valid record with optional field overrides."""

    fields = dict(
        slot="a",
        version="2.1.300",
        channel="stable",
        platforms={
            "win32-x64": {
                "binary": "win32-x64/claude.exe",
                "sha256": "A" * 64,
                "size": 123,
            }
        },
        manifest_sha256="b" * 64,
        build_date=None,
        commit="abc",
        schemes=["openpgp", "raw-rsa"],
        subcommands=[],
        verified_at="2026-10-12T00:00:00Z",
        promoted_by="claude-wrangler 2026.10.12",
    )
    fields.update(overrides)
    return CompletionRecord(**fields)


def test_completion_record_round_trip_ignores_unknown_keys():
    record = sample_record()
    text = record.to_json()
    assert text.endswith("\n")
    payload = json.loads(text)
    assert payload["schema"] == 1
    assert payload["platforms"]["win32-x64"]["sha256"] == "a" * 64

    payload["future_field"] = {"anything": [1, 2]}
    payload["platforms"]["win32-x64"]["future_entry_field"] = True
    loaded = CompletionRecord.from_json(json.dumps(payload).encode("utf-8"))

    assert loaded.to_json() == text
    assert loaded.version == "2.1.300"
    assert loaded.build_date is None
    assert loaded.binary_relpath("win32-x64") == "win32-x64/claude.exe"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(schema=2),
        lambda p: p.update(schema=True),
        lambda p: p.update(slot="c"),
        lambda p: p.update(version="2.1"),
        lambda p: p.update(channel=""),
        lambda p: p.update(platforms={}),
        lambda p: p["platforms"]["win32-x64"].update(size=True),
        lambda p: p["platforms"]["win32-x64"].update(size=0),
        lambda p: p["platforms"]["win32-x64"].update(sha256="xyz"),
        lambda p: p["platforms"]["win32-x64"].update(binary="../claude.exe"),
        lambda p: p["platforms"]["win32-x64"].update(binary="/abs/claude"),
        lambda p: p["platforms"]["win32-x64"].update(binary="win32-x64\\claude.exe"),
        lambda p: p["platforms"]["win32-x64"].update(binary="C:claude.exe"),
        lambda p: p["platforms"]["win32-x64"].update(binary="a//b"),
        lambda p: p.update(schemes="openpgp"),
        lambda p: p.update(commit=5),
        lambda p: p.pop("verified_at"),
    ],
)
def test_completion_record_rejects_malformed_fields(mutate):
    payload = json.loads(sample_record().to_json())
    mutate(payload)

    with pytest.raises(CompletionRecordError):
        CompletionRecord.from_json(json.dumps(payload))


def test_completion_record_rejects_non_json():
    with pytest.raises(CompletionRecordError):
        CompletionRecord.from_json(b"\xff not json")
    with pytest.raises(CompletionRecordError):
        CompletionRecord.from_json("[1, 2]")


# Promotion -----------------------------------------------------------------


def test_first_promotion_fills_slot_a_and_activates_it(tmp_path):
    store = make_store(tmp_path)

    slot = install(store, "2.1.300")

    assert slot == "a"
    assert store.pointer_path.read_bytes() == b"a\n"
    assert store.active_slot() == "a"
    assert store.slot_is_complete("a", PLATFORM)
    assert not store.slot_is_complete("a", "win32-x64")
    assert (store.slot_dir("a") / COMPLETION_RECORD_NAME).is_file()
    assert list(store.staging_dir.iterdir()) == []
    assert not store.slot_dir("b").exists()


def test_promote_a_to_b_and_b_to_a_keeps_rollback_slot(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")

    assert install(store, "2.1.301") == "b"
    assert store.active_slot() == "b"
    assert slot_version(store, "a") == "2.1.300"
    assert slot_version(store, "b") == "2.1.301"
    assert not store.retired_dir.exists()

    formerly_active = snapshot(store.slot_dir("b"))
    assert install(store, "2.1.302") == "a"
    assert store.pointer_path.read_bytes() == b"a\n"
    assert slot_version(store, "a") == "2.1.302"
    # The formerly active slot is untouched and stays the rollback target.
    assert snapshot(store.slot_dir("b")) == formerly_active
    retired = list(store.retired_dir.iterdir())
    assert len(retired) == 1
    displaced = CompletionRecord.from_json(
        (retired[0] / COMPLETION_RECORD_NAME).read_bytes()
    )
    assert displaced.version == "2.1.300"


def test_promote_refuses_the_active_slot(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    candidate, record = build_candidate(store, "a", "2.1.301")
    before = snapshot(store.root)

    with pytest.raises(PromotionError, match="active-slot pointer names it"):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    assert snapshot(store.root) == before


def test_promote_requires_candidate_inside_staging(tmp_path):
    store = make_store(tmp_path)
    candidate, record = build_candidate(store, "a", "2.1.300")
    outside = tmp_path / "elsewhere"
    shutil.move(str(candidate), str(outside))

    with pytest.raises(PromotionError, match="direct child"):
        store.promote(outside, record, ensure_slot_free=no_op_free)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda c: (c / PLATFORM / "claude").write_bytes(b"short"),
        lambda c: (c / PLATFORM / "claude").write_bytes(
            (c / PLATFORM / "claude").read_bytes()[:-1] + b"X"
        ),
        lambda c: (c / "manifest.json.sig").unlink(),
        lambda c: (c / "manifest.json").write_bytes(b'{"version": "9.9.9"}'),
    ],
    ids=["size", "sha256", "missing-evidence", "manifest-hash"],
)
def test_promote_rejects_candidate_that_disagrees_with_record(tmp_path, corrupt):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    candidate, record = build_candidate(store, "b", "2.1.301")
    corrupt(candidate)
    before = snapshot(store.root)

    with pytest.raises(PromotionError, match="failed validation"):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    assert snapshot(store.root) == before


def install_two(store: RuntimeStore) -> None:
    """Leave slot b active (2.1.301) with slot a as rollback (2.1.300)."""

    install(store, "2.1.300")
    install(store, "2.1.301")


def assert_previous_state_intact(store, before_slots, candidate, candidate_bytes):
    """Check a failed promotion restored both slots, pointer, and candidate."""

    assert store.pointer_path.read_bytes() == b"b\n"
    assert store.active_slot() == "b"
    assert snapshot(store.slots_dir) == before_slots
    assert not store.retired_dir.exists() or not any(store.retired_dir.iterdir())
    assert snapshot(candidate) == candidate_bytes
    assert not [p for p in store.root.iterdir() if p.name.endswith(".tmp")]


def test_pointer_failure_before_commit_restores_retired_slot(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    install_two(store)
    candidate, record = build_candidate(store, "a", "2.1.302")
    before_slots = snapshot(store.slots_dir)
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.basename(str(dst)) == POINTER_NAME:
            raise OSError(errno.EIO, "injected pointer failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(PromotionError, match="before the commit point") as info:
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    monkeypatch.undo()

    assert "unchanged" in str(info.value)
    candidate_after = snapshot(candidate)
    assert candidate_after[COMPLETION_RECORD_NAME] == record.to_json().encode()
    assert_previous_state_intact(store, before_slots, candidate, candidate_after)


def test_candidate_rename_failure_restores_retired_slot(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    install_two(store)
    candidate, record = build_candidate(store, "a", "2.1.302")
    before_slots = snapshot(store.slots_dir)
    real_rename = os.rename

    def failing_rename(src, dst, *args, **kwargs):
        if Path(src) == candidate:
            raise OSError(errno.EIO, "injected candidate move failure")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(PromotionError):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    monkeypatch.undo()

    assert_previous_state_intact(store, before_slots, candidate, snapshot(candidate))


def test_revalidation_failure_after_move_restores_everything(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    install_two(store)
    candidate, record = build_candidate(store, "a", "2.1.302")
    before_slots = snapshot(store.slots_dir)
    real_rename = os.rename

    def truncating_rename(src, dst, *args, **kwargs):
        result = real_rename(src, dst, *args, **kwargs)
        if Path(src) == candidate:
            # Simulate a scanner or disk fault truncating the moved binary.
            with open(Path(dst) / PLATFORM / "claude", "r+b") as handle:
                handle.truncate(10)
        return result

    monkeypatch.setattr(os, "rename", truncating_rename)
    with pytest.raises(PromotionError, match="re-validation"):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    monkeypatch.undo()

    assert_previous_state_intact(store, before_slots, candidate, snapshot(candidate))


def test_interrupt_before_commit_restores_and_reraises(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    install_two(store)
    candidate, record = build_candidate(store, "a", "2.1.302")
    before_slots = snapshot(store.slots_dir)
    real_replace = os.replace

    def interrupted_replace(src, dst, *args, **kwargs):
        if os.path.basename(str(dst)) == POINTER_NAME:
            raise KeyboardInterrupt
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    monkeypatch.undo()

    assert_previous_state_intact(store, before_slots, candidate, snapshot(candidate))


def test_failure_after_commit_keeps_new_slot_active(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    install_two(store)
    formerly_active = snapshot(store.slot_dir("b"))
    candidate, record = build_candidate(store, "a", "2.1.302")
    real_replace = os.replace

    def replace_then_fail(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        if os.path.basename(str(dst)) == POINTER_NAME:
            raise OSError(errno.EIO, "injected failure after commit")
        return result

    monkeypatch.setattr(os, "replace", replace_then_fail)
    with pytest.raises(PointerCommittedError, match="now selects verified slot a"):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    monkeypatch.undo()

    assert store.pointer_path.read_bytes() == b"a\n"
    assert store.active_slot() == "a"
    assert slot_version(store, "a") == "2.1.302"
    assert snapshot(store.slot_dir("b")) == formerly_active
    assert not candidate.exists()
    assert len(list(store.retired_dir.iterdir())) == 1


def test_interrupt_after_commit_is_flagged_as_committed(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    install_two(store)
    candidate, record = build_candidate(store, "a", "2.1.302")
    real_replace = os.replace

    def replace_then_interrupt(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        if os.path.basename(str(dst)) == POINTER_NAME:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(os, "replace", replace_then_interrupt)
    with pytest.raises(PointerCommittedInterrupt):
        store.promote(candidate, record, ensure_slot_free=no_op_free)
    monkeypatch.undo()

    assert store.active_slot() == "a"
    assert slot_version(store, "b") == "2.1.301"


def test_ensure_slot_free_refusal_changes_nothing(tmp_path):
    store = make_store(tmp_path)
    install_two(store)
    candidate, record = build_candidate(store, "a", "2.1.302")
    before = snapshot(store.root)
    seen = []

    def fake_lister(path):
        seen.append(path)
        return [RunningProcess(4242, str(path / PLATFORM / "claude"))]

    stops = []
    check = make_slot_free_check(lambda: stops.append(1), lister=fake_lister)

    with pytest.raises(SlotInUseError, match="pid 4242") as info:
        store.promote(candidate, record, ensure_slot_free=check)

    assert info.value.processes[0].pid == 4242
    assert seen == [store.slot_dir("a")]
    assert stops == [1]
    assert snapshot(store.root) == before


def test_ensure_slot_free_skipped_for_empty_target(tmp_path):
    store = make_store(tmp_path)
    candidate, record = build_candidate(store, "a", "2.1.300")

    def must_not_run(path):
        raise AssertionError("no occupied slot to check")

    assert store.promote(candidate, record, ensure_slot_free=must_not_run) == "a"


def test_promote_with_invalid_pointer_needs_repair_flag(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    store.pointer_path.write_bytes(b"garbage")
    candidate, record = build_candidate(store, "b", "2.1.301")

    with pytest.raises(PointerError):
        store.promote(candidate, record, ensure_slot_free=no_op_free)

    store.promote(
        candidate, record, ensure_slot_free=no_op_free, allow_invalid_pointer=True
    )
    assert store.active_slot() == "b"
    assert slot_version(store, "a") == "2.1.300"


def test_promote_refuses_when_active_slot_is_corrupt_unless_repairing(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    binary_path(store, "a").write_bytes(b"truncated")
    candidate, record = build_candidate(store, "b", "2.1.300")

    with pytest.raises(PointerError, match="incomplete"):
        store.promote(candidate, record, ensure_slot_free=no_op_free)

    store.promote(
        candidate, record, ensure_slot_free=no_op_free, allow_invalid_pointer=True
    )
    assert store.active_slot() == "b"


# Completeness, rollback, cleanup -------------------------------------------


def test_slot_completeness_checks(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    assert store.slot_problem("a") is None

    record_path = store.slot_dir("a") / COMPLETION_RECORD_NAME
    original_record = record_path.read_bytes()
    binary = binary_path(store, "a")
    original_binary = binary.read_bytes()

    binary.write_bytes(original_binary[:-1])
    assert not store.slot_is_complete("a", PLATFORM)
    assert "bytes" in store.slot_problem("a", PLATFORM)
    binary.unlink()
    assert "missing" in store.slot_problem("a", PLATFORM)
    binary.write_bytes(original_binary)
    assert store.slot_is_complete("a", PLATFORM)

    record_path.write_bytes(b"{corrupt")
    assert store.slot_record("a") is None
    assert not store.slot_is_complete("a", PLATFORM)

    payload = json.loads(original_record)
    payload["slot"] = "b"
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.slot_record("a") is None
    assert "names slot b" in store.slot_problem("a")

    record_path.write_bytes(original_record)
    assert store.slot_is_complete("a", PLATFORM)
    assert not store.slot_is_complete("b", PLATFORM)
    with pytest.raises(SlotError):
        store.slot_dir("c")


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute permission")
def test_promote_marks_binaries_executable_and_completeness_requires_it(tmp_path):
    store = make_store(tmp_path)
    candidate, record = build_candidate(store, "a", "2.1.300")
    os.chmod(candidate / PLATFORM / "claude", 0o644)

    store.promote(candidate, record, ensure_slot_free=no_op_free)

    binary = binary_path(store, "a")
    assert binary.stat().st_mode & 0o777 == 0o755
    assert store.slot_is_complete("a", PLATFORM)
    os.chmod(binary, 0o644)
    assert "not executable" in store.slot_problem("a", PLATFORM)


def test_set_active_rolls_back_and_refuses_incomplete(tmp_path):
    store = make_store(tmp_path)
    install_two(store)

    store.set_active("a")
    assert store.pointer_path.read_bytes() == b"a\n"
    assert store.active_slot() == "a"

    binary_path(store, "b").write_bytes(b"x")
    with pytest.raises(SlotError, match="incomplete"):
        store.set_active("b")
    assert store.pointer_path.read_bytes() == b"a\n"


def test_set_active_repairs_malformed_pointer_but_not_a_directory(tmp_path):
    store = make_store(tmp_path)
    install_two(store)

    store.pointer_path.write_bytes(b"b\r\n")
    with pytest.raises(PointerError):
        store.active_slot()
    store.set_active("a")
    assert store.pointer_path.read_bytes() == b"a\n"

    store.pointer_path.unlink()
    store.pointer_path.mkdir()
    with pytest.raises(PointerError, match="directory"):
        store.set_active("b")
    assert store.pointer_path.is_dir()


def test_set_active_replaces_linked_pointer_without_following(tmp_path):
    store = make_store(tmp_path)
    install_two(store)
    outside = tmp_path / "outside-pointer"
    outside.write_bytes(b"b\n")
    store.pointer_path.unlink()
    try:
        os.symlink(str(outside), str(store.pointer_path))
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot create a file symbolic link here: {}".format(exc))

    with pytest.raises(PointerError, match="regular file"):
        store.read_pointer()
    store.set_active("a")

    assert not is_link_like(store.pointer_path)
    assert store.pointer_path.read_bytes() == b"a\n"
    assert outside.read_bytes() == b"b\n"


def test_cleanup_staging_and_retired_respects_in_use(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    install(store, "2.1.301")
    install(store, "2.1.302")
    busy_retired = next(store.retired_dir.iterdir())
    stale = store.new_staging_dir()
    (stale / "partial.bin").write_bytes(b"partial")
    busy_staging = store.new_staging_dir()
    slots_before = snapshot(store.slots_dir)
    asked = []

    def in_use(path):
        asked.append(path)
        return path in (busy_retired, busy_staging)

    removed = store.cleanup_staging_and_retired(in_use)

    assert removed == [".staging/" + stale.name]
    assert busy_retired.exists() and busy_staging.exists()
    assert not stale.exists()
    assert snapshot(store.slots_dir) == slots_before
    assert store.pointer_path.read_bytes() == b"a\n"
    assert sorted(asked) == sorted([stale, busy_staging, busy_retired])

    removed = store.cleanup_staging_and_retired(lambda path: False)
    assert removed == [
        ".staging/" + busy_staging.name,
        ".retired/" + busy_retired.name,
    ]
    assert list(store.retired_dir.iterdir()) == []
    assert snapshot(store.slots_dir) == slots_before


def test_cleanup_never_follows_links(tmp_path):
    store = make_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"keep me")
    retired = store.root / ".retired" / "20260101T000000Z-a-abc"
    retired.mkdir(parents=True)
    (retired / "file.bin").write_bytes(b"x")
    try:
        make_dir_link(outside, retired / "escape")
    except OSError as exc:
        pytest.skip("cannot create a directory link here: {}".format(exc))

    removed = store.cleanup_staging_and_retired(lambda path: False)

    assert removed == [".retired/" + retired.name]
    assert not retired.exists()
    assert (outside / "sentinel.txt").read_bytes() == b"keep me"


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attribute")
def test_remove_tree_clears_windows_read_only(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    locked = victim / "readonly.bin"
    locked.write_bytes(b"x")
    os.chmod(locked, 0o444)

    remove_tree(victim)

    assert not victim.exists()


# Root safety and locking ---------------------------------------------------


def test_runtime_root_must_stay_inside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = RuntimeStore(tmp_path / "elsewhere", project_root=project)

    with pytest.raises(UnsafePathError):
        store.new_staging_dir()


def test_linked_runtime_ancestor_is_refused_without_writing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    real_local = tmp_path / "real-local"
    real_local.mkdir()
    try:
        make_dir_link(real_local, project / ".local")
    except OSError as exc:
        pytest.skip("cannot create a directory link here: {}".format(exc))
    assert is_link_like(project / ".local")
    root = project / ".local" / "claude" / "wrangler"

    with pytest.raises(MaintenanceLockError):
        with MaintenanceLock(root, project_root=project):
            pass
    with pytest.raises(UnsafePathError):
        RuntimeStore(root, project_root=project).new_staging_dir()
    assert list(real_local.iterdir()) == []


def test_linked_runtime_root_is_refused(tmp_path):
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked = tmp_path / "wrangler"
    try:
        make_dir_link(real_root, linked)
    except OSError as exc:
        pytest.skip("cannot create a directory link here: {}".format(exc))

    with pytest.raises(MaintenanceLockError):
        MaintenanceLock(linked).acquire()
    assert list(real_root.iterdir()) == []


def test_runtime_root_placement_rules(tmp_path):
    project = tmp_path / "project"
    config = project / ".local" / "claude"
    home = config / "HOME"
    xdg_data = home / ".local" / "share"

    check_runtime_root_placement(
        config / "wrangler", home_dir=home, config_dir=config, extra_bases=[xdg_data]
    )
    bad_roots = [
        home,
        home / "runtime",
        config,
        config / "local" / "wrangler",
        config / "Downloads",
        config / "cache",
        xdg_data / "claude" / "versions" / "x",
        project / "tools" / "claude" / "versions",
    ]
    for root in bad_roots:
        with pytest.raises(UnsafePathError):
            check_runtime_root_placement(
                root, home_dir=home, config_dir=config, extra_bases=[xdg_data]
            )


def test_lock_refuses_non_regular_lock_path(tmp_path):
    store = make_store(tmp_path)
    store.lock_path.mkdir(parents=True)

    with pytest.raises(MaintenanceLockError, match="regular file"):
        store.lock().acquire()


def test_lock_is_exclusive_in_process_and_file_is_kept(tmp_path):
    store = make_store(tmp_path)
    with store.lock() as held:
        assert held.held
        with pytest.raises(MaintenanceLockError, match="already running"):
            store.lock().acquire()
    assert store.lock_path.is_file()
    with store.lock():
        pass
    assert store.lock_path.is_file()


def test_second_maintainer_fails_fast_while_another_process_holds_lock(tmp_path):
    store = make_store(tmp_path)
    install(store, "2.1.300")
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from claude_wrangler.slots import MaintenanceLock\n"
        "with MaintenanceLock(Path(sys.argv[2])):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(SRC), str(store.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline().strip() == b"locked"
        before = snapshot(store.root)
        started = time.monotonic()
        with pytest.raises(MaintenanceLockError, match="already running"):
            with store.lock():
                pytest.fail("lock must not be acquired")
        assert time.monotonic() - started < 5
        assert snapshot(store.root) == before
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    assert holder.returncode == 0
    with store.lock():
        pass


# In-use detection ------------------------------------------------------------


def test_processes_under_uses_separator_boundary(tmp_path):
    root = tmp_path / "slots" / "b"
    inside = RunningProcess(10, str(root / "linux-x64" / "claude"))
    sibling = RunningProcess(11, str(tmp_path / "slots" / "bb" / "claude"))
    parent = RunningProcess(12, str(tmp_path / "slots" / "claude"))
    deleted = RunningProcess(9, str(root / "claude") + " (deleted)")

    found = processes_under(root, [inside, sibling, parent, deleted])

    assert [process.pid for process in found] == [9, 10]


@pytest.mark.skipif(os.name != "nt", reason="Windows path case rules")
def test_processes_under_is_case_insensitive_on_windows(tmp_path):
    root = tmp_path / "Slots" / "B"
    image = str(root / "win32-x64" / "claude.exe").upper().replace("\\", "/")

    found = processes_under(root, [RunningProcess(3, "\\\\?\\" + image)])

    assert [process.pid for process in found] == [3]


def test_find_processes_under_sees_current_interpreter():
    processes = list_process_images()
    mine = [process for process in processes if process.pid == os.getpid()]
    if not mine or not os.path.isabs(mine[0].image):
        pytest.skip("this platform does not report a full image path for us")

    image_dir = Path(mine[0].image).parent
    found = find_processes_under(image_dir)

    assert os.getpid() in [process.pid for process in found]


def long_running_copy(directory: Path) -> list:
    """Copy a harmless long-running system tool into a slot-like directory."""

    if os.name == "nt":
        source = shutil.which("ping")
        name = "ping.exe"
        arguments = ["-n", "60", "127.0.0.1"]
    else:
        source = shutil.which("sleep")
        name = "sleep"
        arguments = ["60"]
    if source is None:
        pytest.skip("no ping/sleep tool to copy")
    directory.mkdir(parents=True)
    target = directory / name
    shutil.copy2(os.path.realpath(source), target)
    return [str(target)] + arguments


def test_real_process_running_from_slot_blocks_displacement(tmp_path):
    slot = tmp_path / "wrangler" / "slots" / "b"
    command = long_running_copy(slot / "tool")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        found = []
        while time.monotonic() < deadline and not found:
            found = find_processes_under(slot)
            if not found:
                time.sleep(0.1)
        if not found and sys.platform == "darwin":
            pytest.skip("ps did not report a full path for the copied tool")
        assert process.pid in [entry.pid for entry in found]
        assert find_processes_under(tmp_path / "wrangler" / "slots" / "a") == []

        with pytest.raises(SlotInUseError, match=str(process.pid)):
            make_slot_free_check()(slot)
    finally:
        process.kill()
        process.wait(timeout=30)
    assert find_processes_under(slot) == []


# Background service stop -----------------------------------------------------


def fake_claude(tmp_path: Path, monkeypatch, body: str) -> Path:
    """Make ``python daemon stop --any`` run ``body`` as the daemon script.

    The interpreter stands in for the binary: it runs the file named
    ``daemon`` from the working directory with ``stop --any`` as arguments.
    """

    workdir = tmp_path / "fake-claude"
    workdir.mkdir()
    (workdir / "daemon").write_text(body, encoding="utf-8")
    monkeypatch.chdir(workdir)
    return Path(sys.executable)


def test_stop_background_service_reports_nonzero_exit(tmp_path, monkeypatch, capsys):
    binary = fake_claude(
        tmp_path,
        monkeypatch,
        "import os, sys\n"
        "data = sys.stdin.read()\n"
        "print('args', sys.argv[1:], 'marker', os.environ.get('CW_MARKER'))\n"
        "print('stdin', repr(data))\n"
        "sys.exit(3)\n",
    )
    env = dict(os.environ, CW_MARKER="project-env")

    result = stop_background_service(binary, env, timeout_seconds=60)

    assert result.returncode == 3
    assert not result.ok
    assert result.command[1:] == ("daemon", "stop", "--any")
    assert "['stop', '--any']" in result.output
    assert "marker project-env" in result.output
    assert "stdin ''" in result.output
    assert "exited 3" in capsys.readouterr().err


def test_stop_background_service_success(tmp_path, monkeypatch, capsys):
    binary = fake_claude(tmp_path, monkeypatch, "print('stopped')\n")

    result = stop_background_service(binary, dict(os.environ))

    assert result.ok
    assert "stopped" in result.output
    assert capsys.readouterr().err == ""


def test_stop_background_service_times_out(tmp_path, monkeypatch):
    binary = fake_claude(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    started = time.monotonic()

    with pytest.raises(ServiceStopError, match="did not finish"):
        stop_background_service(binary, dict(os.environ), timeout_seconds=1.0)
    assert time.monotonic() - started < 30
    assert issubclass(ServiceStopError, ClaudeWranglerError)


def test_stop_background_service_missing_binary_is_reported(tmp_path, capsys):
    result = stop_background_service(tmp_path / "missing-claude", dict(os.environ))

    assert result.returncode is None
    assert not result.ok
    assert "could not start" in capsys.readouterr().err
