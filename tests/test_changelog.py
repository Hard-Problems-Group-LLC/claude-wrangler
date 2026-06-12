"""Tests for the changelog sync helpers."""

from pathlib import Path

from claude_wrangler.changelog import (
    changelog_path,
    default_changelog_text,
    existing_unreleased_entries,
    extract_pending_entries,
    sync_changelog_from_pending_queue,
    sync_changelog_text,
)


def test_extract_pending_entries_splits_wrapped_sentences() -> None:
    queue_text = "\n".join(
        [
            "Add approved runtime-fidelity design records plus a compact",
            "startup-preflight specification.",
            "Introduce runtime diagnostics for inspect and self-test.",
            "Harden isolated repo-local home handling.",
            "",
        ]
    )

    assert extract_pending_entries(queue_text) == [
        "Add approved runtime-fidelity design records plus a compact startup-preflight specification.",
        "Introduce runtime diagnostics for inspect and self-test.",
        "Harden isolated repo-local home handling.",
    ]


def test_sync_changelog_text_adds_missing_changed_entries() -> None:
    updated, changed = sync_changelog_text(
        default_changelog_text(),
        "- Add startup preflight.\n- Harden isolated home handling.\n",
    )

    assert changed is True
    assert "- Add startup preflight." in updated
    assert "- Harden isolated home handling." in updated


def test_sync_changelog_text_is_idempotent() -> None:
    first, _ = sync_changelog_text(
        default_changelog_text(),
        "- Add startup preflight.\n",
    )
    second, changed = sync_changelog_text(first, "- Add startup preflight.\n")

    assert changed is False
    assert second == first


def test_existing_unreleased_entries_sees_added_and_changed_bullets() -> None:
    entries = existing_unreleased_entries(
        "\n".join(
            [
                "# Changelog",
                "",
                "## [Unreleased]",
                "",
                "### Added",
                "",
                "- Added changelog helper that preserves queue history",
                "  in the changelog.",
                "",
                "### Changed",
                "",
                "- Added startup preflight and selector-aware diagnostics",
                "  for the generated launcher.",
                "",
            ]
        )
        + "\n"
    )

    assert (
        "Added changelog helper that preserves queue history in the changelog."
        in entries
    )
    assert (
        "Added startup preflight and selector-aware diagnostics for the generated launcher."
        in entries
    )


def test_sync_changelog_from_pending_queue_updates_file(tmp_path: Path) -> None:
    repo_root = tmp_path
    queue_path = (
        repo_root / "project-management" / "state" / "pending-commit-changes.txt"
    )
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text("- Add startup preflight.\n", encoding="utf-8")

    changelog_file, entry_count, changed = sync_changelog_from_pending_queue(
        repo_root,
        queue_path,
    )

    assert changelog_file == changelog_path(repo_root)
    assert entry_count == 1
    assert changed is True
    assert "- Add startup preflight." in changelog_file.read_text(encoding="utf-8")
