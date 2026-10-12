"""Atomic file replacement and Windows lock-tolerant retries.

Every generated or managed file that claude-wrangler publishes goes through a
same-directory temporary file and ``os.replace`` so a reader never observes a
partially written file and a failed write leaves the previous file intact
(``ab_runtime_slots.md`` requirement 14). Text files are written with an
explicit newline convention: LF for files read by POSIX shells and Python,
CRLF for cmd batch files.

On Windows a running executable can be renamed but not overwritten or
deleted, a reader holding a file open without ``FILE_SHARE_DELETE`` blocks
replacement, and antivirus scanners briefly hold freshly written
executables. ``retry_on_windows_lock`` retries those transient sharing and
access failures with backoff (requirement 13). On other operating systems it
calls the function exactly once.

This module is standard-library-only and Python 3.9 compatible.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import time
from typing import Callable, Optional, TypeVar
import uuid

from .models import ClaudeWranglerError

T = TypeVar("T")

# Evaluated once; tests monkeypatch this to exercise the Windows retry path on
# any host.
_WINDOWS = os.name == "nt"

# ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION, the two codes Windows
# reports while another process (a running image, a reader without
# FILE_SHARE_DELETE, or an antivirus scan) holds a file.
WINDOWS_LOCK_ERRORS = (5, 32)

ALLOWED_NEWLINES = ("\n", "\r\n")

_INITIAL_BACKOFF_SECONDS = 0.05
_MAX_BACKOFF_SECONDS = 1.0


class AtomicWriteError(ClaudeWranglerError):
    """Raised when a destination cannot be replaced safely."""


def is_windows_lock_error(exc: BaseException) -> bool:
    """Return whether an exception is a transient Windows file-lock failure.

    ``winerror`` is set for Win32 API calls such as ``os.replace``. CRT-backed
    calls such as ``open`` report sharing violations as a bare ``EACCES``
    with ``winerror`` unset, so a ``PermissionError`` without a ``winerror``
    is treated as a lock failure too.
    """

    if not isinstance(exc, PermissionError):
        return False
    winerror = getattr(exc, "winerror", None)
    return winerror is None or winerror in WINDOWS_LOCK_ERRORS


def retry_on_windows_lock(fn: Callable[[], T], timeout_seconds: float = 60.0) -> T:
    """Call ``fn`` and, on Windows only, retry transient lock failures.

    A ``PermissionError`` carrying WinError 5 or 32 (or a CRT ``EACCES``
    without a Windows code) is retried with exponential backoff until
    ``timeout_seconds`` have elapsed, after which the last error is raised.
    Every other exception propagates immediately. On non-Windows hosts the
    function is called exactly once.
    """

    if not _WINDOWS:
        return fn()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    delay = _INITIAL_BACKOFF_SECONDS
    while True:
        try:
            return fn()
        except PermissionError as exc:
            if not is_windows_lock_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)


def fsync_directory(path: Path) -> None:
    """Flush a directory entry table to stable storage where supported.

    Windows cannot open directories for flushing and some POSIX filesystems
    reject directory ``fsync``; both are treated as best effort because the
    rename that preceded the flush has already taken effect.
    """

    if _WINDOWS or os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _temporary_sibling(path: Path) -> Path:
    """Return a unique temporary path in the same directory as ``path``."""

    return path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))


def _existing_mode(path: Path) -> Optional[int]:
    """Return the permission bits of an existing regular file, if any."""

    try:
        status = os.stat(str(path))
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return stat.S_IMODE(status.st_mode)


def atomic_write_bytes(path: Path, data: bytes, *, mode: Optional[int] = None) -> None:
    """Replace ``path`` with ``data`` through a flushed same-directory temp file.

    Missing parent directories are created. ``mode`` sets the new file's
    permission bits on POSIX; when omitted, an existing file's bits are kept
    and a new file gets the process default. Either the complete new content
    or the previous file is visible at every moment; on failure the
    temporary file is removed and the previous file is left untouched.
    """

    path = Path(path)
    if path.is_symlink():
        # Replacing a link would silently retarget a published path; callers
        # that manage links must remove them deliberately.
        raise AtomicWriteError(
            "Refusing to atomically replace a symbolic link: {}".format(path)
        )
    if path.is_dir():
        raise AtomicWriteError(
            "Refusing to atomically replace a directory: {}".format(path)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = mode if mode is not None else _existing_mode(path)
    temporary = _temporary_sibling(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(temporary), flags, 0o666)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if permissions is not None and not _WINDOWS and os.name != "nt":
            os.chmod(str(temporary), permissions)
        retry_on_windows_lock(lambda: os.replace(str(temporary), str(path)))
    except BaseException:
        # KeyboardInterrupt included: the temporary file is transaction
        # scratch and must not outlive a failed publication.
        try:
            os.unlink(str(temporary))
        except OSError:
            pass
        raise
    fsync_directory(path.parent)


def normalize_newlines(text: str, newline: str = "\n") -> str:
    """Return ``text`` with every LF or CRLF line ending set to ``newline``.

    Existing CRLF pairs are folded to LF first so CRLF input never becomes
    CR CR LF. A lone CR is not a line ending here and is left unchanged.
    """

    if newline not in ALLOWED_NEWLINES:
        raise ValueError(
            "newline must be one of {!r}, not {!r}".format(ALLOWED_NEWLINES, newline)
        )
    folded = text.replace("\r\n", "\n")
    if newline == "\n":
        return folded
    return folded.replace("\n", newline)


def atomic_write_text(
    path: Path,
    text: str,
    newline: str = "\n",
    *,
    encoding: str = "utf-8",
    mode: Optional[int] = None,
) -> None:
    """Atomically write ``text`` with exactly the given newline convention.

    ``newline`` must be ``"\\n"`` or ``"\\r\\n"``. Line endings in ``text``
    (LF or CRLF) are normalized to it, so the bytes on disk are identical
    on every operating system. ``encoding="ascii"`` makes a non-ASCII
    character fail before anything is written, which cmd batch files need.
    """

    data = normalize_newlines(text, newline).encode(encoding)
    atomic_write_bytes(path, data, mode=mode)
