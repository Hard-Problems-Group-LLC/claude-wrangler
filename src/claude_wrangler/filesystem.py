"""Managed filesystem mutation helpers.

Line-ending policy: every helper here reads and writes text byte-exactly
(UTF-8, no platform newline translation), so a file generated on Windows
is identical to one generated on POSIX. Python's default text mode would
silently turn `\\n` into `\\r\\n` on Windows, which breaks generated POSIX
shell scripts, and re-writing already-CRLF text in that mode is how
`\\r\\r\\n` sequences appear in `.gitignore` files, where Git then keeps a
stray `\\r` in each pattern and stops matching it.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Callable, Dict, Optional, Sequence

from .constants import GITIGNORE_BEGIN, GITIGNORE_END, README_MARKER, SCRIPT_MARKER
from .models import ClaudeWranglerError, Config
from .runtime import eprint

LF = "\n"
CRLF = "\r\n"


def read_text_exact(path: Path) -> str:
    """Return a UTF-8 file's text without any newline translation."""

    return path.read_bytes().decode("utf-8")


def write_text_exact(path: Path, text: str) -> None:
    """Write text as UTF-8 bytes exactly as given, with no newline translation."""

    path.write_bytes(text.encode("utf-8"))


def normalize_newlines(text: str, newline: str) -> str:
    """Return text with every CRLF, CR, or LF line break spelled as newline."""

    if newline not in (LF, CRLF):
        raise ClaudeWranglerError(
            "Unsupported newline convention: {!r}".format(newline)
        )
    unified = text.replace(CRLF, LF).replace("\r", LF)
    if newline == LF:
        return unified
    return unified.replace(LF, CRLF)


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

    gitignore_path.parent.mkdir(parents=True, exist_ok=True)
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


def write_text_file(
    path: Path,
    content: str,
    force: bool,
    dry_run: bool,
    executable: bool = False,
    managed_markers: Sequence[str] = (),
    managed_content_predicate: Optional[Callable[[str], bool]] = None,
    newline: Optional[str] = None,
) -> None:
    """Write a deterministic text file with conservative overwrite rules.

    The content is written byte-exactly on every platform. Pass `newline`
    ("\\n" or "\\r\\n") to normalize every line break first, for example
    CRLF for `.cmd` files; leave it as None to keep content as given.
    """

    if newline is not None:
        content = normalize_newlines(content, newline)

    if path.exists():
        existing = read_text_exact(path)
        if existing == content:
            eprint("[claude-wrangler] Unchanged: {}".format(path))
            if executable and not dry_run:
                path.chmod(path.stat().st_mode | 0o111)
            return
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
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_exact(path, content)
    if executable:
        path.chmod(path.stat().st_mode | 0o111)


def write_metadata(path: Path, payload: Dict[str, object], dry_run: bool) -> None:
    """Write the managed metadata file as pretty JSON."""

    action = "Would write" if dry_run else "Writing"
    eprint("[claude-wrangler] {} metadata: {}".format(action, path))
    if dry_run:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_exact(path, json.dumps(payload, indent=2, sort_keys=True) + LF)


def ensure_managed_directories(config: Config) -> None:
    """Create the managed directories unless this is a dry-run.

    The pinned config dir is created in both home modes because the
    launcher exports `CLAUDE_CONFIG_DIR` unconditionally; only the
    redirected HOME directory is conditional on isolation mode.
    """

    if config.dry_run:
        eprint(
            "[claude-wrangler] Would ensure directory exists: {}".format(
                config.layout.local_dir
            )
        )
        eprint(
            "[claude-wrangler] Would ensure directory exists: {}".format(
                config.layout.config_dir
            )
        )
        if not config.shared_home:
            eprint(
                "[claude-wrangler] Would ensure directory exists: {}".format(
                    config.layout.claude_home_dir
                )
            )
        if config.layout.launcher_path.parent != config.project_root:
            eprint(
                "[claude-wrangler] Would ensure directory exists: {}".format(
                    config.layout.launcher_path.parent
                )
            )
        return

    config.layout.local_dir.mkdir(parents=True, exist_ok=True)
    config.layout.config_dir.mkdir(parents=True, exist_ok=True)
    if not config.shared_home:
        config.layout.claude_home_dir.mkdir(parents=True, exist_ok=True)
    config.layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)


def require_safe_managed_path(path: Path, project_root: Path, label: str) -> None:
    """Assert that a path is within the project root and not equal to it."""

    resolved = path.resolve()
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

    actual_content = read_text_exact(path)
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
    path.unlink()
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

    shutil.rmtree(path)
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
