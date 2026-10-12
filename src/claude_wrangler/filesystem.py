"""Managed filesystem mutation helpers.

Line-ending policy: every helper here reads and writes text byte-exactly
(UTF-8, no platform newline translation), so a file generated on Windows
is identical to one generated on POSIX. Python's default text mode would
silently turn `\\n` into `\\r\\n` on Windows, which breaks generated POSIX
shell scripts, and re-writing already-CRLF text in that mode is how
`\\r\\r\\n` sequences appear in `.gitignore` files, where Git then keeps a
stray `\\r` in each pattern and stops matching it.

Newline normalization has one policy for the whole package, implemented in
`atomicio.normalize_newlines`: CRLF, a lone CR, and LF are all line breaks
and are rewritten to the requested convention.

Every write goes through `atomicio.atomic_write_bytes` (a flushed
same-directory temporary file plus `os.replace`), so a reader never sees a
partially written file and a failed write leaves the previous file intact
(`ab_runtime_slots.md` requirement 14).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Callable, Dict, Optional, Sequence, Tuple

from . import atomicio
from .constants import GITIGNORE_BEGIN, GITIGNORE_END, README_MARKER, SCRIPT_MARKER
from .layout import real_path
from .models import ClaudeWranglerError, Config
from .runtime import eprint
from .slots import is_link_like
from .slots import remove_tree as slots_remove_tree

LF = "\n"
CRLF = "\r\n"


def read_text_exact(path: Path) -> str:
    """Return a UTF-8 file's text without any newline translation."""

    return path.read_bytes().decode("utf-8")


def write_text_exact(
    path: Path, text: str, *, encoding: str = "utf-8", mode: Optional[int] = None
) -> None:
    """Atomically write text exactly as given, with no newline translation."""

    atomicio.atomic_write_bytes(path, text.encode(encoding), mode=mode)


def normalize_newlines(text: str, newline: str) -> str:
    """Return text with every CRLF, CR, or LF line break spelled as newline.

    This delegates to `atomicio.normalize_newlines`, the package's single
    newline policy, and reports an unsupported convention as a
    `ClaudeWranglerError`.
    """

    if newline not in (LF, CRLF):
        raise ClaudeWranglerError(
            "Unsupported newline convention: {!r}".format(newline)
        )
    return atomicio.normalize_newlines(text, newline)


def detect_newline_convention(text: str) -> str:
    """Return CRLF only when every line break in text is CRLF, else LF.

    LF is the default for empty, LF-only, and mixed files. A file that is
    consistently CRLF (typical for a Windows checkout with
    `core.autocrlf=true`) keeps CRLF so an edit does not flip every line.
    """

    lf_count = text.count(LF)
    if lf_count and text.count(CRLF) == lf_count:
        return CRLF
    return LF


def _block_end_index(text: str, marker: str) -> int:
    """Return the index just after the marker line, even at EOF.

    The search stops at LF, so a CRLF (or a damaged `\\r\\r\\n`) line
    ending on the marker line is consumed together with the marker.
    """

    start = text.index(marker)
    newline_index = text.find(LF, start)
    if newline_index == -1:
        return len(text)
    return newline_index + 1


def gitignore_block_present(gitignore_path: Path) -> bool:
    """Return True when the `.gitignore` carries this tool's managed block."""

    if not gitignore_path.is_file():
        return False
    try:
        text = read_text_exact(gitignore_path)
    except (OSError, UnicodeDecodeError):
        return False
    return GITIGNORE_BEGIN in text and GITIGNORE_END in text


def upsert_gitignore_block(
    gitignore_path: Path,
    block_text: str,
    dry_run: bool,
) -> None:
    """Insert or replace the managed `.gitignore` block idempotently.

    Only this tool's own marked block is ever touched. Blocks owned by
    other tools, such as TheKnowledge's managed-ignores block or
    codex-wrangler's block, are left exactly as found even when their
    entries overlap with ours.

    The block is written with LF line endings on every platform, unless
    the existing file is consistently CRLF, in which case the block
    follows that convention. Bytes outside the block are never rewritten,
    and the block itself can never contain `\\r\\r\\n`.
    """

    existing_text = ""
    if gitignore_path.exists():
        existing_text = read_text_exact(gitignore_path)

    if GITIGNORE_BEGIN in existing_text and GITIGNORE_END not in existing_text:
        raise ClaudeWranglerError(
            "Found a managed .gitignore begin marker without its end marker in "
            "{}. Refusing to guess.".format(gitignore_path)
        )

    newline = detect_newline_convention(existing_text)
    managed_block = normalize_newlines(block_text, newline)

    if GITIGNORE_BEGIN in existing_text and GITIGNORE_END in existing_text:
        begin_index = existing_text.index(GITIGNORE_BEGIN)
        end_index = _block_end_index(existing_text, GITIGNORE_END)
        replacement = (
            existing_text[:begin_index] + managed_block + existing_text[end_index:]
        )
    else:
        prefix = existing_text
        if prefix.endswith("\r"):
            # Complete a dangling CR instead of appending a second one,
            # which would create `\r\r\n`.
            prefix += LF
        elif prefix and not prefix.endswith(LF):
            prefix += newline
        # Separate the appended block from user entries by one blank line.
        if prefix and not prefix.endswith(newline + newline):
            prefix += newline
        replacement = prefix + managed_block

    if replacement == existing_text:
        eprint("[claude-wrangler] .gitignore block already up to date")
        return

    action = "Would update" if dry_run else "Updating"
    eprint("[claude-wrangler] {} {}".format(action, gitignore_path))
    if dry_run:
        return

    write_text_exact(gitignore_path, replacement)


def remove_gitignore_block(gitignore_path: Path, dry_run: bool) -> bool:
    """Remove the managed `.gitignore` block if it exists.

    Bytes outside the block are preserved; only trailing blank lines are
    trimmed, and the final line break follows the file's own convention.
    """

    if not gitignore_path.exists():
        return False

    existing_text = read_text_exact(gitignore_path)
    if GITIGNORE_BEGIN not in existing_text:
        return False
    if GITIGNORE_END not in existing_text:
        raise ClaudeWranglerError(
            "Found a managed .gitignore begin marker without its end marker in "
            "{}. Refusing to guess.".format(gitignore_path)
        )

    newline = detect_newline_convention(existing_text)
    begin_index = existing_text.index(GITIGNORE_BEGIN)
    end_index = _block_end_index(existing_text, GITIGNORE_END)
    replacement = existing_text[:begin_index] + existing_text[end_index:]
    replacement = replacement.rstrip() + (newline if replacement.strip() else "")

    action = "Would remove" if dry_run else "Removing"
    eprint("[claude-wrangler] {} managed block from {}".format(action, gitignore_path))
    if dry_run:
        return True

    write_text_exact(gitignore_path, replacement)
    return True


def _executable_mode(path: Path) -> int:
    """Return permission bits that add execute wherever read is granted."""

    try:
        current = stat.S_IMODE(os.stat(str(path)).st_mode)
    except OSError:
        current = 0o644
    return current | ((current & 0o444) >> 2) | stat.S_IXUSR


def write_text_file(
    path: Path,
    content: str,
    force: bool,
    dry_run: bool,
    executable: bool = False,
    managed_markers: Sequence[str] = (),
    managed_content_predicate: Optional[Callable[[str], bool]] = None,
    newline: Optional[str] = None,
    encoding: str = "utf-8",
) -> bool:
    """Atomically write a deterministic text file with conservative overwrite rules.

    The content is written byte-exactly on every platform. Pass `newline`
    ("\\n" or "\\r\\n") to normalize every line break first, for example
    CRLF for `.cmd` files; leave it as None to keep content as given.
    `encoding="ascii"` refuses non-ASCII content before anything is written.
    An existing file is replaced only when it already matches, carries one
    of `managed_markers`, satisfies `managed_content_predicate`, or `force`
    is set. Returns True when the file was (or, in a dry run, would be)
    written and False when it was already up to date.
    """

    if newline is not None:
        content = normalize_newlines(content, newline)
    data = content.encode(encoding)

    if path.exists():
        if not path.is_file():
            raise ClaudeWranglerError(
                "Refusing to replace something that is not a regular file: "
                "{}".format(path)
            )
        existing_bytes = path.read_bytes()
        if existing_bytes == data:
            eprint("[claude-wrangler] Unchanged: {}".format(path))
            if executable and not dry_run and os.name != "nt":
                mode = _executable_mode(path)
                if mode != stat.S_IMODE(os.stat(str(path)).st_mode):
                    os.chmod(str(path), mode)
            return False
        existing = existing_bytes.decode("utf-8", "replace")
        marker_present = any(marker in existing for marker in managed_markers)
        predicate_matches = (
            managed_content_predicate(existing)
            if managed_content_predicate is not None
            else False
        )
        if not force and not marker_present and not predicate_matches:
            raise ClaudeWranglerError(
                "Refusing to overwrite existing file without --force: {}".format(path)
            )

    action = "Would write" if dry_run else "Writing"
    eprint("[claude-wrangler] {}: {}".format(action, path))
    if dry_run:
        return True

    mode = _executable_mode(path) if executable else None
    atomicio.atomic_write_bytes(path, data, mode=mode)
    return True


def write_metadata(path: Path, payload: Dict[str, object], dry_run: bool) -> None:
    """Atomically write a managed metadata file as pretty, sorted, LF JSON."""

    action = "Would write" if dry_run else "Writing"
    eprint("[claude-wrangler] {} metadata: {}".format(action, path))
    if dry_run:
        return

    atomicio.atomic_write_text(
        path, json.dumps(payload, indent=2, sort_keys=True) + LF, newline=LF
    )


def ensure_managed_directories(config: Config) -> None:
    """Create the session directories unless this is a dry-run.

    The pinned config dir is created in both home modes because the
    launcher exports `CLAUDE_CONFIG_DIR` unconditionally; only the
    redirected HOME directory is conditional on isolation mode. On Windows
    the redirected home also gets `AppData\\Roaming` and `AppData\\Local`,
    because the ring-1 launcher points APPDATA and LOCALAPPDATA there.

    On POSIX the config dir and the redirected HOME hold transcripts,
    history, and credentials, so they are made private to the operator
    (mode 0700), including when they already exist with looser modes.
    """

    targets = [(config.layout.config_dir, True)]
    if not config.shared_home:
        targets.append((config.layout.claude_home_dir, True))
        if os.name == "nt":
            targets.append(
                (config.layout.claude_home_dir / "AppData" / "Roaming", False)
            )
            targets.append((config.layout.claude_home_dir / "AppData" / "Local", False))
    if config.layout.launcher_path.parent != config.project_root:
        targets.append((config.layout.launcher_path.parent, False))

    for target, private in targets:
        if config.dry_run:
            eprint("[claude-wrangler] Would ensure directory exists: {}".format(target))
            continue
        target.mkdir(parents=True, exist_ok=True)
        if private and os.name != "nt":
            _make_private_directory(target)


def _make_private_directory(path: Path) -> None:
    """Restrict a managed directory to its owner (mode 0700) on POSIX."""

    if is_link_like(path) or not path.is_dir():
        return
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    if mode & 0o077:
        eprint(
            "[claude-wrangler] Restricting {} to its owner (mode {:o} -> 700): it "
            "holds Claude Code session state.".format(path, mode)
        )
        os.chmod(str(path), 0o700)


def require_safe_managed_path(path: Path, project_root: Path, label: str) -> None:
    """Refuse a path outside the project root, or the project root itself.

    Links are resolved with `layout.real_path`, which also follows a
    dangling link (Python 3.9's `Path.resolve()` does not on Windows).
    """

    resolved = real_path(path)
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ClaudeWranglerError(
            "{} is outside the project root and will not be touched: {}".format(
                label, path
            )
        ) from exc
    if resolved == project_root:
        raise ClaudeWranglerError(
            "{} resolves to the project root itself and will not be touched.".format(
                label
            )
        )


def remove_file_if_managed(
    path: Path,
    expected_content: str,
    force: bool,
    dry_run: bool,
    label: str,
) -> bool:
    """Remove a generated file only when ownership can be proven.

    Content is compared without regard to line-ending convention, so a
    generated file that Git or an editor converted between LF and CRLF
    still proves ownership.
    """

    if not path.exists() and not path.is_symlink():
        return False
    if not path.is_file():
        raise ClaudeWranglerError(
            "Expected a file at {}, but found something else. Refusing to "
            "remove it automatically{}.".format(
                path,
                " even with --force" if force else " without --force",
            )
        )

    actual_content = path.read_bytes().decode("utf-8", "replace")
    marker_present = SCRIPT_MARKER in actual_content or README_MARKER in actual_content
    content_matches = normalize_newlines(actual_content, LF) == normalize_newlines(
        expected_content, LF
    )
    if not content_matches and not (marker_present and force):
        raise ClaudeWranglerError(
            "Refusing to remove {} because it does not exactly match the "
            "expected generated content. Use --force to override.".format(path)
        )

    action = "Would remove" if dry_run else "Removing"
    eprint("[claude-wrangler] {} {}: {}".format(action, label, path))
    if dry_run:
        return True
    atomicio.retry_on_windows_lock(path.unlink)
    return True


def remove_tree(path: Path, label: str, project_root: Path, dry_run: bool) -> bool:
    """Remove a managed directory tree after path safety checks."""

    if not path.exists() and not path.is_symlink():
        return False

    require_safe_managed_path(path, project_root, label)
    if path.is_symlink():
        raise ClaudeWranglerError(
            "{} is a symbolic link and will not be removed automatically: {}".format(
                label,
                path,
            )
        )
    if not path.is_dir():
        raise ClaudeWranglerError(
            "{} is not a directory and will not be removed automatically: {}".format(
                label,
                path,
            )
        )
    action = "Would remove" if dry_run else "Removing"
    eprint("[claude-wrangler] {} {}: {}".format(action, label, path))
    if dry_run:
        return True

    # slots.remove_tree never follows links or junctions inside the tree,
    # clears the Windows read-only attribute, and retries transient Windows
    # sharing violations.
    slots_remove_tree(path)
    return True


def _path_parts(path: Path) -> Tuple[str, ...]:
    """Return normalized, case-folded absolute path components."""

    absolute = os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
    return Path(absolute).parts


def is_strictly_inside(child: Path, parent: Path) -> bool:
    """Return whether ``child`` lies below ``parent`` (not equal to it)."""

    child_parts = _path_parts(child)
    parent_parts = _path_parts(parent)
    return (
        len(child_parts) > len(parent_parts)
        and child_parts[: len(parent_parts)] == parent_parts
    )


def _remove_children_except(directory: Path, keep: Path) -> None:
    """Delete everything under ``directory`` except ``keep`` and its ancestors."""

    for entry in sorted(directory.iterdir()):
        if _path_parts(entry) == _path_parts(keep):
            continue
        if (
            is_strictly_inside(keep, entry)
            and not is_link_like(entry)
            and entry.is_dir()
        ):
            _remove_children_except(entry, keep)
            continue
        # slots.remove_tree never follows links or junctions.
        slots_remove_tree(entry)


def remove_tree_except(path: Path, keep: Path, label: str, project_root: Path) -> bool:
    """Remove a managed directory tree but keep the subtree at ``keep``.

    Uninstall empties the config dir while it still holds the maintenance
    lock, whose file lives in the runtime root inside that dir; the runtime
    root and the emptied directories go afterwards, so the lock file is the
    last thing deleted (`ab_runtime_slots.md` requirement 4). When ``keep``
    is not inside ``path`` this is `remove_tree`.
    """

    if not is_strictly_inside(keep, path):
        return remove_tree(path, label, project_root, dry_run=False)
    if not path.exists() and not path.is_symlink():
        return False
    require_safe_managed_path(path, project_root, label)
    if path.is_symlink() or is_link_like(path) or not path.is_dir():
        raise ClaudeWranglerError(
            "{} is a link or not a directory and will not be removed "
            "automatically: {}".format(label, path)
        )
    eprint(
        "[claude-wrangler] Removing {} (the runtime root {} goes last): {}".format(
            label, keep, path
        )
    )
    _remove_children_except(path, keep)
    return True


def maybe_remove_empty_parent(path: Path, stop_at: Path, dry_run: bool) -> None:
    """Remove empty parent directories up to, but not including, stop_at."""

    current = path.parent
    while current != stop_at and current.exists():
        try:
            next(current.iterdir())
            break
        except StopIteration:
            action = "Would remove" if dry_run else "Removing"
            eprint("[claude-wrangler] {} empty directory: {}".format(action, current))
            if not dry_run:
                current.rmdir()
            current = current.parent
