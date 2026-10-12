"""Tests for conservative managed-filesystem mutation helpers."""

import pytest

from claude_wrangler.constants import GITIGNORE_BEGIN, GITIGNORE_END
from claude_wrangler.filesystem import (
    detect_newline_convention,
    normalize_newlines,
    remove_file_if_managed,
    remove_gitignore_block,
    remove_tree,
    upsert_gitignore_block,
    write_metadata,
    write_text_file,
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


# Line-ending policy: `.gitignore` edits are byte-exact. New files and
# LF or mixed files get an LF block on every platform; a consistently
# CRLF file keeps CRLF. No path may ever produce `\r\r\n`.


def _assert_consistent_crlf(data: bytes) -> None:
    assert b"\r\r\n" not in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_detect_newline_convention_requires_consistent_crlf():
    assert detect_newline_convention("") == "\n"
    assert detect_newline_convention("a\nb\n") == "\n"
    assert detect_newline_convention("a\r\nb\r\n") == "\r\n"
    assert detect_newline_convention("a\r\nb\n") == "\n"


def test_normalize_newlines_rewrites_every_break_style():
    assert normalize_newlines("a\r\nb\rc\n", "\n") == "a\nb\nc\n"
    assert normalize_newlines("a\r\nb\rc\n", "\r\n") == "a\r\nb\r\nc\r\n"
    with pytest.raises(ClaudeWranglerError):
        normalize_newlines("a\n", "\r")


def test_upsert_gitignore_block_creates_lf_file_on_every_platform(tmp_path):
    gitignore_path = tmp_path / ".gitignore"

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(default_layout(tmp_path)),
        dry_run=False,
    )

    data = gitignore_path.read_bytes()
    assert b"\r" not in data
    assert data.startswith(GITIGNORE_BEGIN.encode("utf-8") + b"\n")
    assert data.endswith(GITIGNORE_END.encode("utf-8") + b"\n")


def test_upsert_gitignore_block_keeps_lf_file_lf(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_bytes(b"keep-me\n")

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(default_layout(tmp_path)),
        dry_run=False,
    )

    data = gitignore_path.read_bytes()
    assert b"\r" not in data
    assert data.startswith(b"keep-me\n\n" + GITIGNORE_BEGIN.encode("utf-8"))


def test_upsert_gitignore_block_follows_consistent_crlf(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_bytes(b"keep-me\r\n.cache/\r\n")

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(default_layout(tmp_path)),
        dry_run=False,
    )

    data = gitignore_path.read_bytes()
    _assert_consistent_crlf(data)
    assert data.startswith(b"keep-me\r\n.cache/\r\n\r\n")
    assert b"bin/claude-local\r\n" in data


def test_upsert_gitignore_block_is_idempotent_for_crlf_files(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_bytes(b"keep-me\r\n")
    block = build_gitignore_block(default_layout(tmp_path))

    upsert_gitignore_block(gitignore_path, block, dry_run=False)
    first = gitignore_path.read_bytes()
    upsert_gitignore_block(gitignore_path, block, dry_run=False)

    assert gitignore_path.read_bytes() == first
    _assert_consistent_crlf(first)


def test_upsert_gitignore_block_writes_lf_block_into_mixed_file(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    original = b"crlf-entry\r\nlf-entry\n"
    gitignore_path.write_bytes(original)

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(default_layout(tmp_path)),
        dry_run=False,
    )

    data = gitignore_path.read_bytes()
    assert data.startswith(original)
    assert b"\r" not in data[len(original) :]


def test_upsert_gitignore_block_repairs_damaged_managed_block(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_bytes(
        b"keep-me\r\r\n"
        + GITIGNORE_BEGIN.encode("utf-8")
        + b"\r\r\nold\r\r\n"
        + GITIGNORE_END.encode("utf-8")
        + b"\r\r\nafter\r\r\n"
    )

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(default_layout(tmp_path)),
        dry_run=False,
    )

    data = gitignore_path.read_bytes()
    begin = data.index(GITIGNORE_BEGIN.encode("utf-8"))
    end = data.index(GITIGNORE_END.encode("utf-8")) + len(GITIGNORE_END)
    managed = data[begin : end + 2]
    assert b"\r\r\n" not in managed
    assert managed.endswith(GITIGNORE_END.encode("utf-8") + b"\r\n")
    # Foreign bytes are never rewritten, even when they are damaged.
    assert data.startswith(b"keep-me\r\r\n")
    assert data.endswith(b"after\r\r\n")


def test_upsert_gitignore_block_completes_dangling_carriage_return(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_bytes(b"first\r\nsecond\r")

    upsert_gitignore_block(
        gitignore_path,
        build_gitignore_block(default_layout(tmp_path)),
        dry_run=False,
    )

    data = gitignore_path.read_bytes()
    _assert_consistent_crlf(data)
    assert data.startswith(b"first\r\nsecond\r\n\r\n")


def test_upsert_gitignore_block_normalizes_crlf_block_text_for_lf_file(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    block = build_gitignore_block(default_layout(tmp_path)).replace("\n", "\r\n")

    upsert_gitignore_block(gitignore_path, block, dry_run=False)

    assert b"\r" not in gitignore_path.read_bytes()


def test_remove_gitignore_block_preserves_crlf_convention(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_bytes(
        b"keep-me\r\n\r\n"
        + GITIGNORE_BEGIN.encode("utf-8")
        + b"\r\nold\r\n"
        + GITIGNORE_END.encode("utf-8")
        + b"\r\n"
    )

    assert remove_gitignore_block(gitignore_path, dry_run=False) is True
    assert gitignore_path.read_bytes() == b"keep-me\r\n"


def test_write_text_file_writes_exact_lf_bytes(tmp_path):
    path = tmp_path / "bin" / "claude-local"

    write_text_file(path, "#!/bin/sh\necho ok\n", force=False, dry_run=False)

    assert path.read_bytes() == b"#!/bin/sh\necho ok\n"


def test_write_text_file_can_normalize_to_crlf(tmp_path):
    path = tmp_path / "bin" / "claude-local.cmd"

    write_text_file(
        path,
        "@echo off\nexit /b 0\n",
        force=False,
        dry_run=False,
        newline="\r\n",
    )

    assert path.read_bytes() == b"@echo off\r\nexit /b 0\r\n"


def test_write_text_file_rewrites_line_ending_drift_of_managed_file(tmp_path):
    path = tmp_path / "bin" / "claude-local"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# managed by claude-wrangler\r\necho ok\r\n")

    write_text_file(
        path,
        "# managed by claude-wrangler\necho ok\n",
        force=False,
        dry_run=False,
        managed_markers=("managed by claude-wrangler",),
    )

    assert path.read_bytes() == b"# managed by claude-wrangler\necho ok\n"


def test_write_metadata_writes_lf_json(tmp_path):
    path = tmp_path / ".local" / "claude" / ".claude-wrangler.json"

    write_metadata(path, {"b": 1, "a": "x"}, dry_run=False)

    assert path.read_bytes() == b'{\n  "a": "x",\n  "b": 1\n}\n'


def test_remove_file_if_managed_accepts_line_ending_converted_copy(tmp_path):
    managed_path = tmp_path / "README-LOCAL-Start-Claude.md"
    managed_path.write_bytes(b"generated\r\ncontent\r\n")

    removed = remove_file_if_managed(
        managed_path,
        expected_content="generated\ncontent\n",
        force=False,
        dry_run=False,
        label="local README",
    )

    assert removed is True
    assert not managed_path.exists()
