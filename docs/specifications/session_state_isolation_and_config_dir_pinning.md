# Session-State Isolation and Config-Dir Pinning

## Purpose

Define how `claude-wrangler` keeps all session-specific Claude Code
state inside the target project, how the generated launcher composes
`CLAUDE_CONFIG_DIR` with the security ring model's `$HOME` handling,
and how the managed layout coexists with `codex-wrangler` in the same
repository.

## Scope

This specification covers:

- the managed layout under `.local/claude/`;
- the generated `bin/claude-local` launcher environment;
- `claude-wrangler --inspect` and `--selftest` reporting; and
- coexistence constraints with `codex-wrangler` and TheKnowledge's
  managed `.gitignore` baseline.

## Managed Layout

All session-specific Claude data lives under the project-root
`.local/claude/` umbrella, which TheKnowledge already treats as the
standard home for checkout-local operator state:

- `.local/claude/npm/`: the managed npm install (package manifest,
  lockfile, `node_modules/`, `.npm-cache/`, and the
  `.claude-wrangler.json` managed metadata file);
- `.local/claude/HOME/`: the project-local persistent home used as
  `$HOME` at rings 1 and 2; and
- `.local/claude/` itself as the pinned `CLAUDE_CONFIG_DIR`, so Claude
  Code writes auth, transcripts, history, todos, and settings directly
  under the umbrella.

## Requirements

1. Config-dir pinning across rings
   The generated launcher must export
   `CLAUDE_CONFIG_DIR=<project>/.local/claude` unconditionally, at
   every ring, including the ring 0 escape hatch. Escalating fidelity
   for `$HOME` must never fork Claude auth or session history into the
   operator's real `~/.claude`.
2. Ring 1 home redirect
   By default the launcher must export `HOME=<project>/.local/claude/
   HOME` and point `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
   `XDG_STATE_HOME`, and `XDG_DATA_HOME` beneath that home, mirroring
   the `codex-local` isolation design.
3. Ring 0 escape hatch
   A per-invocation `--shared-home` launcher argument (consumed by the
   wrapper, not forwarded to Claude Code) and an equivalent
   `CLAUDE_LOCAL_SHARED_HOME=1` environment variable must skip the
   `HOME` and `XDG_*` redirection while preserving the config-dir pin.
   An install-time `--shared-home` default recorded in managed metadata
   must also remain supported for parity with `codex-wrangler`.
4. Local binary execution
   The launcher must exec
   `.local/claude/npm/node_modules/.bin/claude` directly. It must not
   use `npx` or fall back to any globally installed `claude`, so a bad
   global tool update cannot take out project work, and so a registry
   package squatting a bare name cannot be substituted.
5. npm containment
   Managed npm operations must set `NPM_CONFIG_CACHE` to
   `.local/claude/npm/.npm-cache` so package installs and npm scratch
   writes stay inside the project.
6. Coexistence with codex-wrangler
   `claude-wrangler` must be able to manage a repository that also
   carries a `codex-wrangler` install. Managed paths
   (`.local/claude/`, `bin/claude-local`,
   `README-LOCAL-Start-Claude.md`) must not collide with
   `codex-wrangler` artifacts (`.codex-local/`, `.codex-home/`,
   `bin/codex-local`, `README-LOCAL-Start-Codex.md`), and each tool's
   marked `.gitignore` block must leave the other's block untouched.
7. Observability
   `--inspect` and `--selftest` must report the effective
   `CLAUDE_CONFIG_DIR`, `HOME`, and `XDG_*` values the launcher will
   expose, and whether the managed install uses isolated or shared
   home mode.

## Validation

- Launcher tests must cover the pinned config dir, the redirected home
  and XDG paths, and both ring 0 escape hatches.
- Operations tests must cover npm cache containment and direct local
  binary execution.
- Inspection tests must cover the reported launcher environment.
