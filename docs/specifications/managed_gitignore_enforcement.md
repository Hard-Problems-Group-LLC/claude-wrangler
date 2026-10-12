# Managed Gitignore Enforcement

## Purpose

Ensure `claude-wrangler` keeps generated local Claude artifacts
untracked even when a target project has not already configured local
ignore rules, without disturbing ignore blocks owned by other tools.

## Scope

This specification covers the managed `.gitignore` block that
`claude-wrangler` writes to a target project root for local Claude
artifacts.

## Requirements

1. Startup enforcement
   Every normal `claude-wrangler` operation that is not uninstall
   cleanup must ensure the target project's `.gitignore` contains the
   managed block before running operation-specific behavior.
2. Missing file
   When the target project has no `.gitignore`, `claude-wrangler` must
   create one containing only the managed block.
3. Missing managed block
   When a `.gitignore` exists without the managed block,
   `claude-wrangler` must append the managed block without rewriting
   unrelated entries.
4. Existing unmanaged entries
   If managed entries already appear outside the managed block,
   `claude-wrangler` must still add those entries inside the managed
   block. Redundant ignore rules are acceptable and safer than moving
   or deleting user-owned rules.
5. Required entries
   The managed block must contain an explanatory comment plus ignore
   rules for `.local/`, `bin/claude-local`, `bin/claude-local.cmd`, and
   `README-LOCAL-Start-Claude.md`.
5a. Line endings
   Block edits are byte-exact outside the managed block. The block uses
   LF for new, LF, or mixed files on every operating system, and CRLF
   only when the existing file is consistently CRLF. An edit must never
   produce `\r\r\n`.
6. Paths that must stay trackable
   The managed block must not ignore the project-root `.claude/`
   directory. Claude Code uses `.claude/` for tracked project
   configuration such as `settings.json`, commands, and skills, which
   projects may deliberately commit.
7. Foreign managed blocks
   The block must use `claude-wrangler`-specific begin and end markers.
   Enforcement and uninstall cleanup must never modify blocks owned by
   other tools, including TheKnowledge's managed-ignores block and
   `codex-wrangler`'s managed block, even when their entries overlap
   with ours (for example, `.local/`).
8. Uninstall cleanup
   `--uninstall` remains responsible for removing the managed block as
   part of conservative cleanup. It must not recreate the block before
   removing it.

## Validation

- Unit tests must cover creating `.gitignore` with the managed block.
- Unit tests must cover appending the managed block even when managed
  entries already exist outside it.
- Unit tests must cover leaving TheKnowledge-style and
  codex-wrangler-style blocks untouched while adding ours.
- Entry-point tests must cover enforcing the managed block before an
  operation such as `--inspect` delegates to operation-specific
  behavior.
