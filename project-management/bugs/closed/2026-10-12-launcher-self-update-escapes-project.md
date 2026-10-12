# Launcher lets Claude Code self-update outside the project

- Status: closed
- Severity: high
- Reported: 2026-10-12T00:20:00-07:00
- Resolved: 2026-10-12T00:35:00-07:00
- Branch: `feature/windows-platform-support` (first commit, intended to be
  cherry-picked to `trunk` ahead of the branch)

## Symptom

The generated `bin/claude-local` launcher never disabled Claude Code's
self-update machinery. A session started through the launcher could
update or reinstall Claude Code in locations outside the project.

## Root cause

Claude Code's updater and installer choose their targets from `HOME`
(and `XDG_*`), not from the location of the running binary. A binary
under `node_modules/@anthropic-ai/` classifies itself as an npm-global
install, so its updater runs `npm install -g` against the operator's
global prefix and, on Windows, first removes `HOME/.local/bin/claude.exe`.
At ring 0, where `HOME` is the operator's real home, that rewrites or
deletes the operator's global Claude Code install. The README's claim
that the launcher "emits update notices rather than auto-updating" was
therefore untrue. Evidence came from reading the code bundled in Claude
Code 2.1.296 and the documented `DISABLE_UPDATES` /
`DISABLE_AUTOUPDATER` / `DISABLE_INSTALLATION_CHECKS` variables.

## Resolution

The launcher now exports `DISABLE_UPDATES=1`, `DISABLE_AUTOUPDATER=1`,
and `DISABLE_INSTALLATION_CHECKS=1` before it executes Claude Code
(`SELF_UPDATE_SUPPRESSION_ENV` in `src/claude_wrangler/constants.py`).
`session_state_isolation_and_config_dir_pinning.md` gained requirement
5a, and `test_build_launcher_content_disables_claude_self_update` covers
the exports. Existing installs pick up the fix the next time
claude-wrangler rewrites the launcher (`--update`, `--upgrade`, or
`--repair-install`).

## Known residual risk

A repository's own `.claude/settings.json` can override these
environment variables through its `env` block. Closing that gap
(`--settings` layering and a preflight scan) is part of the native
runtime work on the same branch.
