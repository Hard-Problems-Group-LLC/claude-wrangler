"""Helpers for maintaining the repository changelog from commit queue data."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

CHANGELOG_FILE = "CHANGELOG.md"
UNRELEASED_HEADING = "## [Unreleased]"
DEFAULT_UNRELEASED_CATEGORIES = ("### Added", "### Changed", "### Fixed")
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*]\s+")
_QUEUE_SPLIT_RE = re.compile(r"(?<=[.!?])\n(?=[A-Z`])")
_WHITESPACE_RE = re.compile(r"\s+")


def default_changelog_text() -> str:
    """Return the default changelog skeleton for this repository."""

    return "\n".join(
        [
            "# Changelog",
            "",
            "All notable changes to this repository will be documented in this",
            "file.",
            "",
            "The format is based on [Keep a Changelog]"
            "(https://keepachangelog.com/en/1.1.0/), and this repository aims",
            "to follow [Semantic Versioning]"
            "(https://semver.org/spec/v2.0.0.html) where practical.",
            "",
            UNRELEASED_HEADING,
            "",
            "### Added",
            "",
            "### Changed",
            "",
            "### Fixed",
            "",
        ]
    )


def normalize_changelog_entry(text: str) -> str:
    """Collapse one changelog entry to a stable single-line form."""

    stripped = _BULLET_PREFIX_RE.sub("", text.strip(), count=1)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def extract_pending_entries(queue_text: str) -> list[str]:
    """Split pending-commit queue text into normalized changelog entries."""

    stripped = queue_text.strip()
    if not stripped:
        return []
    if any(_BULLET_PREFIX_RE.match(line) for line in stripped.splitlines()):
        return _extract_bullet_entries(stripped.splitlines())

    paragraphs = re.split(r"\n\s*\n", stripped)
    entries: list[str] = []
    for paragraph in paragraphs:
        for part in _QUEUE_SPLIT_RE.split(paragraph.strip()):
            normalized = normalize_changelog_entry(part)
            if normalized:
                entries.append(normalized)
    return entries


def changelog_path(repo_root: Path) -> Path:
    """Return the changelog path for one repository root."""

    return repo_root / CHANGELOG_FILE


def _extract_bullet_entries(lines: list[str]) -> list[str]:
    """Collect wrapped bullet entries from one list of lines."""

    entries: list[str] = []
    current: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            if current:
                entries.append(normalize_changelog_entry(" ".join(current)))
                current = []
            continue
        if _BULLET_PREFIX_RE.match(line):
            if current:
                entries.append(normalize_changelog_entry(" ".join(current)))
            current = [_BULLET_PREFIX_RE.sub("", line, count=1)]
            continue
        if current:
            current.append(line.strip())
    if current:
        entries.append(normalize_changelog_entry(" ".join(current)))
    return [entry for entry in entries if entry]


def _next_heading_index(lines: list[str], start_index: int) -> int:
    """Return the index of the next heading or the end of the file."""

    for index in range(start_index + 1, len(lines)):
        if lines[index].startswith("## "):
            return index
    return len(lines)


def _ensure_unreleased_section(lines: list[str]) -> list[str]:
    """Ensure the changelog contains the standard unreleased headings."""

    if UNRELEASED_HEADING not in lines:
        insertion_index = len(lines)
        for index, line in enumerate(lines):
            if line.startswith("## "):
                insertion_index = index
                break
        unreleased_block = [
            UNRELEASED_HEADING,
            "",
            "### Added",
            "",
            "### Changed",
            "",
            "### Fixed",
            "",
        ]
        if insertion_index > 0 and lines[insertion_index - 1] != "":
            unreleased_block.insert(0, "")
        lines[insertion_index:insertion_index] = unreleased_block

    unreleased_index = lines.index(UNRELEASED_HEADING)
    unreleased_end = _next_heading_index(lines, unreleased_index)
    for heading in DEFAULT_UNRELEASED_CATEGORIES:
        if heading not in lines[unreleased_index:unreleased_end]:
            insertion = unreleased_end
            block = [heading, ""]
            if insertion > 0 and lines[insertion - 1] != "":
                block.insert(0, "")
            lines[insertion:insertion] = block
            unreleased_end += len(block)
    return lines


def _section_body_bounds(lines: list[str], heading: str) -> tuple[int, int]:
    """Return the body start and end indexes for one changelog subheading."""

    heading_index = lines.index(heading)
    body_start = heading_index + 1
    body_end = len(lines)
    for index in range(body_start, len(lines)):
        if lines[index].startswith("### ") or lines[index].startswith("## "):
            body_end = index
            break
    return body_start, body_end


def existing_unreleased_entries(changelog_text: str) -> set[str]:
    """Return normalized unreleased-section entries already present."""

    lines = changelog_text.splitlines()
    if UNRELEASED_HEADING not in lines:
        return set()
    unreleased_index = lines.index(UNRELEASED_HEADING)
    unreleased_end = _next_heading_index(lines, unreleased_index)
    return set(_extract_bullet_entries(lines[unreleased_index + 1 : unreleased_end]))


def sync_changelog_text(changelog_text: str, pending_text: str) -> tuple[str, bool]:
    """Mirror pending queue entries into the changelog changed section."""

    entries = extract_pending_entries(pending_text)
    if not changelog_text.strip():
        changelog_text = default_changelog_text()
    lines = _ensure_unreleased_section(changelog_text.splitlines())
    rendered = "\n".join(lines).rstrip() + "\n"

    if not entries:
        return rendered, changelog_text != rendered

    body_start, body_end = _section_body_bounds(lines, "### Changed")
    existing = existing_unreleased_entries("\n".join(lines))
    missing = [
        entry for entry in entries if normalize_changelog_entry(entry) not in existing
    ]
    if not missing:
        return rendered, changelog_text != rendered

    insertion_index = body_end
    while insertion_index > body_start and not lines[insertion_index - 1].strip():
        insertion_index -= 1

    block = [f"- {entry}" for entry in missing]
    if insertion_index > body_start and lines[insertion_index - 1].strip():
        block.insert(0, "")
    block.append("")
    lines[insertion_index:insertion_index] = block
    return "\n".join(lines).rstrip() + "\n", True


def sync_changelog_from_pending_queue(
    repo_root: Path,
    pending_queue_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[Path, int, bool]:
    """Sync one pending queue into the repo changelog and report the result."""

    changelog_file = changelog_path(repo_root)
    pending_text = pending_queue_path.read_text(encoding="utf-8")
    entry_count = len(extract_pending_entries(pending_text))
    current_text = (
        changelog_file.read_text(encoding="utf-8") if changelog_file.is_file() else ""
    )
    updated_text, changed = sync_changelog_text(current_text, pending_text)
    if changed and not dry_run:
        changelog_file.write_text(updated_text, encoding="utf-8")
    return changelog_file, entry_count, changed


def render_changed_entries(entries: Iterable[str]) -> list[str]:
    """Render normalized entries as single-line changelog bullets."""

    return [f"- {normalize_changelog_entry(entry)}" for entry in entries]


__all__ = [
    "CHANGELOG_FILE",
    "DEFAULT_UNRELEASED_CATEGORIES",
    "UNRELEASED_HEADING",
    "changelog_path",
    "default_changelog_text",
    "existing_unreleased_entries",
    "extract_pending_entries",
    "normalize_changelog_entry",
    "render_changed_entries",
    "sync_changelog_from_pending_queue",
    "sync_changelog_text",
]
