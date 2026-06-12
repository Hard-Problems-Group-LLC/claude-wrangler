"""Tests for conservative managed-filesystem mutation helpers."""

import pytest

from claude_wrangler.filesystem import (
    remove_file_if_managed,
    remove_gitignore_block,
    remove_tree,
    upsert_gitignore_block,
)
from claude_wrangler.layout import build_layout
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.rendering import build_gitignore_block


def default_layout(tmp_path):
    return build_layout(
        tmp_path,
        ".local/claude/npm",
        ".local/claude/HOME",
        ".local/claude",
        "bin/claude-local",
        "README-LOCAL-Start-Claude.md",
    )


def test_upsert_gitignore_block_replaces_managed_block_without_trailing_newline(
    tmp_path,
):
    layout = default_layout(tmp_path)
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(
        "keep-me\n# BEGIN managed by claude-wrangler\nold\n# END managed by claude-wrangler",
        encoding="utf-8",
    )

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(layout),
        dry_run=False,
    )

    text = gitignore_path.read_text(encoding="utf-8")
    assert text.count("# BEGIN managed by claude-wrangler") == 1
    assert "README-LOCAL-Start-Claude.md" in text
    assert ".local/" in text
    assert "bin/claude-local" in text


def test_upsert_gitignore_block_creates_missing_file(tmp_path):
    layout = default_layout(tmp_path)
    gitignore_path = tmp_path / ".gitignore"

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(layout),
        dry_run=False,
    )

    text = gitignore_path.read_text(encoding="utf-8")
    assert text.startswith("# BEGIN managed by claude-wrangler\n")
    assert ".local/\n" in text
    assert "bin/claude-local\n" in text


def test_upsert_gitignore_block_appends_rules_even_when_rules_exist_elsewhere(
    tmp_path,
):
    layout = default_layout(tmp_path)
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(".local/\nbin/claude-local\n", encoding="utf-8")

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(layout),
        dry_run=False,
    )

    text = gitignore_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.count("# BEGIN managed by claude-wrangler") == 1
    assert lines.count(".local/") == 2
    assert lines.count("bin/claude-local") == 2


def test_upsert_gitignore_block_leaves_foreign_managed_blocks_alone(tmp_path):
    layout = default_layout(tmp_path)
    gitignore_path = tmp_path / ".gitignore"
    foreign_blocks = "\n".join(
        [
            "# >>> THEKNOWLEDGE_MANAGED_IGNORES_START >>>",
            ".local/",
            "# <<< THEKNOWLEDGE_MANAGED_IGNORES_END <<<",
            "",
            "# BEGIN managed by codex-wrangler",
            ".codex-local/",
            "# END managed by codex-wrangler",
            "",
        ]
    )
    gitignore_path.write_text(foreign_blocks, encoding="utf-8")

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(layout),
        dry_run=False,
    )

    text = gitignore_path.read_text(encoding="utf-8")
    assert "# >>> THEKNOWLEDGE_MANAGED_IGNORES_START >>>" in text
    assert "# BEGIN managed by codex-wrangler" in text
    assert ".codex-local/" in text
    assert text.count("# BEGIN managed by claude-wrangler") == 1


def test_remove_gitignore_block_handles_eof_without_trailing_newline(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(
        "keep-me\n# BEGIN managed by claude-wrangler\nold\n# END managed by claude-wrangler",
        encoding="utf-8",
    )

    removed = remove_gitignore_block(gitignore_path, dry_run=False)

    assert removed is True
    assert gitignore_path.read_text(encoding="utf-8") == "keep-me\n"


def test_remove_gitignore_block_does_not_touch_foreign_blocks(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text(
        "\n".join(
            [
                "# BEGIN managed by codex-wrangler",
                ".codex-local/",
                "# END managed by codex-wrangler",
                "",
            ]
        ),
        encoding="utf-8",
    )

    removed = remove_gitignore_block(gitignore_path, dry_run=False)

    assert removed is False
    assert ".codex-local/" in gitignore_path.read_text(encoding="utf-8")


def test_remove_file_if_managed_refuses_directory_even_with_force(tmp_path):
    managed_path = tmp_path / "bin" / "claude-local"
    managed_path.mkdir(parents=True)

    with pytest.raises(ClaudeWranglerError):
        remove_file_if_managed(
            managed_path,
            expected_content="ignored",
            force=True,
            dry_run=False,
            label="launcher",
        )


def test_remove_file_if_managed_refuses_unmanaged_content_without_force(tmp_path):
    managed_path = tmp_path / "README-LOCAL-Start-Claude.md"
    managed_path.write_text("user content\n", encoding="utf-8")

    with pytest.raises(ClaudeWranglerError):
        remove_file_if_managed(
            managed_path,
            expected_content="generated content\n",
            force=False,
            dry_run=False,
            label="local README",
        )


def test_remove_tree_refuses_file_path(tmp_path):
    managed_path = tmp_path / ".local" / "claude" / "npm" / "node_modules"
    managed_path.parent.mkdir(parents=True)
    managed_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ClaudeWranglerError, match="not a directory"):
        remove_tree(
            managed_path,
            label="managed local node_modules",
            project_root=tmp_path,
            dry_run=False,
        )


def test_remove_tree_refuses_symlink_path(tmp_path):
    target_path = tmp_path / "target"
    target_path.mkdir()
    managed_path = tmp_path / ".local" / "claude" / "npm" / "node_modules"
    managed_path.parent.mkdir(parents=True)
    managed_path.symlink_to(target_path, target_is_directory=True)

    with pytest.raises(ClaudeWranglerError, match="symbolic link"):
        remove_tree(
            managed_path,
            label="managed local node_modules",
            project_root=tmp_path,
            dry_run=False,
        )


def test_remove_tree_refuses_broken_symlink_path(tmp_path):
    managed_path = tmp_path / ".local" / "claude" / "npm" / "node_modules"
    managed_path.parent.mkdir(parents=True)
    managed_path.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(ClaudeWranglerError, match="symbolic link"):
        remove_tree(
            managed_path,
            label="managed local node_modules",
            project_root=tmp_path,
            dry_run=False,
        )
