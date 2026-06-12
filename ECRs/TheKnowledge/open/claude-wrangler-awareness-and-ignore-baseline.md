# ECR: claude-wrangler Awareness and Managed Ignore Baseline

- Date: 2026-06-11
- Status: open (drafted in claude-wrangler; TheKnowledge checkout is
  read-only here)
- Requestor: claude-wrangler project (Matt Heck / AI pairing session)

## Summary

TheKnowledge's managed `.gitignore` baseline
(`templates/gitignore-managed.txt`) and the AGENTS footer's local-tooling
guidance currently name Codex and codex-wrangler artifacts explicitly
(`.codex-local/`, `.codex-home/`, `.codex`, `bin/codex-local`,
`README-LOCAL-Start-Codex.md`). The footer instructs consumers to
"extend the managed ignore baseline deliberately when future local Codex
artifact paths appear." That future case now exists: `claude-wrangler`
is the Claude Code analogue of codex-wrangler and is designed to coexist
with it in the same repository.

## Requested Changes

1. Add the claude-wrangler generated artifacts to the managed ignore
   baseline: `bin/claude-local` and `README-LOCAL-Start-Claude.md`.
   The session-state umbrella `.local/` is already covered by the
   existing baseline and needs no new entry.
2. Do not ignore the project-root `.claude/` directory. Claude Code
   keeps trackable project configuration there (settings, commands,
   skills), which consuming projects may deliberately commit.
3. Extend the AGENTS footer's local-CLI guidance to treat
   `.local/claude/` (npm install, redirected HOME, pinned
   `CLAUDE_CONFIG_DIR` state), `bin/claude-local`, and
   `README-LOCAL-Start-Claude.md` as local operator tooling or
   generated artifacts, parallel to the existing Codex guidance, and to
   avoid classifying `.local/claude/npm/package.json` as a tracked
   project dependency manifest.
4. Consider adopting the four-level security ring model documented in
   claude-wrangler's `docs/specifications/security_ring_model.md`
   (CPU-convention numbering; ring 3 sandbox default, ring 0 real HOME
   last resort) as shared cross-wrangler guidance, since the same
   definitions are planned for the next codex-wrangler upgrade.

## Notes

- claude-wrangler's own managed `.gitignore` block already covers its
  artifacts for projects that do not use TheKnowledge; this request is
  about the proactive baseline TheKnowledge installs before those tools
  run.
- See `docs/specifications/managed_gitignore_enforcement.md` and
  `docs/specifications/session_state_isolation_and_config_dir_pinning.md`
  in the claude-wrangler repository for the artifact contract.
