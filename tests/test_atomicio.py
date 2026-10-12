"""Tests for atomic file replacement and Windows lock-tolerant retries."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import threading
import time

import pytest

from claude_wrangler import atomicio
from claude_wrangler.atomicio import (
    AtomicWriteError,
    atomic_write_bytes,
    atomic_write_text,
    normalize_newlines,
    retry_on_windows_lock,
)
from claude_wrangler.models import ClaudeWranglerError


def temp_leftovers(directory: Path) -> list:
    """Return any temporary siblings an atomic write left behind."""

    return sorted(path.name for path in directory.iterdir() if path.suffix == ".tmp")


def windows_lock_error(code: int) -> PermissionError:
    """Build a PermissionError carrying a Windows error code on any host."""

    exc = PermissionError(errno.EACCES, "simulated Windows lock")
    exc.winerror = code
    return exc


def test_atomic_write_text_lf_writes_exact_bytes(tmp_path):
    target = tmp_path / "claude-local"

    atomic_write_text(target, "#!/bin/sh\r\necho one\necho two\n", newline="\n")

    assert target.read_bytes() == b"#!/bin/sh\necho one\necho two\n"
    assert temp_leftovers(tmp_path) == []


def test_atomic_write_text_crlf_writes_exact_bytes_without_doubling(tmp_path):
    target = tmp_path / "claude-local.cmd"

    atomic_write_text(target, "@echo off\r\nset A=1\nexit /b 0\n", newline="\r\n")

    assert target.read_bytes() == b"@echo off\r\nset A=1\r\nexit /b 0\r\n"
    assert b"\r\r\n" not in target.read_bytes()


def test_normalize_newlines_rejects_unknown_convention():
    with pytest.raises(ValueError):
        normalize_newlines("x\n", newline="\r")


def test_atomic_write_text_ascii_failure_leaves_previous_file(tmp_path):
    target = tmp_path / "shim.cmd"
    target.write_bytes(b"old\r\n")

    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(target, "café\n", newline="\r\n", encoding="ascii")

    assert target.read_bytes() == b"old\r\n"
    assert temp_leftovers(tmp_path) == []


def test_atomic_write_bytes_replaces_and_creates_parents(tmp_path):
    target = tmp_path / "nested" / "dir" / "file.bin"

    atomic_write_bytes(target, b"first")
    atomic_write_bytes(target, b"second")

    assert target.read_bytes() == b"second"
    assert temp_leftovers(target.parent) == []


def test_failed_replace_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "launch.json"
    target.write_bytes(b"old")

    def broken_replace(src, dst, *args, **kwargs):
        raise OSError(errno.EIO, "injected replace failure")

    monkeypatch.setattr(os, "replace", broken_replace)

    with pytest.raises(OSError, match="injected"):
        atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"old"
    assert temp_leftovers(tmp_path) == []


def test_atomic_write_refuses_directory_destination(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()

    with pytest.raises(AtomicWriteError):
        atomic_write_bytes(target, b"x")
    assert issubclass(AtomicWriteError, ClaudeWranglerError)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_preserves_or_sets_mode(tmp_path):
    target = tmp_path / "claude-local"
    atomic_write_bytes(target, b"#!/bin/sh\n", mode=0o755)
    assert target.stat().st_mode & 0o777 == 0o755

    atomic_write_bytes(target, b"#!/bin/sh\necho\n")
    assert target.stat().st_mode & 0o777 == 0o755


def test_retry_on_windows_lock_retries_sharing_violation(monkeypatch):
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    sleeps = []
    monkeypatch.setattr(atomicio.time, "sleep", sleeps.append)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise windows_lock_error(32)
        return "done"

    assert retry_on_windows_lock(flaky) == "done"
    assert len(attempts) == 3
    assert len(sleeps) == 2
    assert sleeps[1] >= sleeps[0]


def test_retry_on_windows_lock_retries_access_denied(monkeypatch):
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    monkeypatch.setattr(atomicio.time, "sleep", lambda seconds: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise windows_lock_error(5)
        return 7

    assert retry_on_windows_lock(flaky) == 7


def test_retry_on_windows_lock_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    clock = [1000.0]
    monkeypatch.setattr(atomicio.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(atomicio.time, "sleep", advance)
    attempts = []

    def always_locked():
        attempts.append(1)
        raise windows_lock_error(32)

    with pytest.raises(PermissionError):
        retry_on_windows_lock(always_locked, timeout_seconds=5.0)
    assert len(attempts) > 2
    assert clock[0] <= 1005.0 + 1e-9


def test_retry_on_windows_lock_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    monkeypatch.setattr(atomicio.time, "sleep", lambda seconds: None)
    attempts = []

    def not_a_lock():
        attempts.append(1)
        raise windows_lock_error(1314)

    with pytest.raises(PermissionError):
        retry_on_windows_lock(not_a_lock)
    assert len(attempts) == 1

    def missing():
        attempts.append(1)
        raise FileNotFoundError(errno.ENOENT, "gone")

    with pytest.raises(FileNotFoundError):
        retry_on_windows_lock(missing)
    assert len(attempts) == 2


def test_retry_on_windows_lock_is_single_shot_elsewhere(monkeypatch):
    monkeypatch.setattr(atomicio, "_WINDOWS", False)
    attempts = []

    def locked():
        attempts.append(1)
        raise windows_lock_error(32)

    with pytest.raises(PermissionError):
        retry_on_windows_lock(locked)
    assert len(attempts) == 1


@pytest.mark.skipif(os.name != "nt", reason="real Windows sharing semantics")
def test_atomic_write_waits_for_reader_holding_destination(tmp_path):
    target = tmp_path / "active-slot"
    target.write_bytes(b"a\n")
    reader = open(target, "rb")
    released_at = []

    def release_later():
        time.sleep(0.4)
        released_at.append(time.monotonic())
        reader.close()

    thread = threading.Thread(target=release_later)
    thread.start()
    try:
        atomic_write_bytes(target, b"b\n")
        written_at = time.monotonic()
    finally:
        thread.join()
        if not reader.closed:
            reader.close()

    # Python's open() does not share delete access, so the replacement can
    # only land after the reader lets go; the retry loop bridges the gap.
    assert written_at >= released_at[0]
    assert target.read_bytes() == b"b\n"
    assert temp_leftovers(tmp_path) == []
