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
- the environment that the generated launcher core
  (`<runtime root>/launch.py`, run by `bin/claude-local` and
  `bin/claude-local.cmd`) gives Claude Code;
- `claude-wrangler --inspect` and `--selftest` reporting; and
- coexistence constraints with `codex-wrangler` and TheKnowledge's
  managed `.gitignore` baseline.

## Managed Layout

All session-specific Claude data lives under the project-root
`.local/claude/` umbrella, which TheKnowledge already treats as the
standard home for checkout-local operator state:

- `.local/claude/wrangler/`: the native runtime root (`--local-dir`).
  It holds the schema-2 metadata file `.claude-wrangler.json`, the
  maintenance lock `.lock`, the `active-slot` pointer, the two runtime
  slots `slots/a/` and `slots/b/` (each one verified release with its
  retained manifest evidence and completion record), `.staging/` and
  `.retired/` work areas, the launcher core `launch.py` and its
  `launch.json`, the nested-`claude` PATH shim directory `shim/`, the
  update-check cache `update-check.json`, and `fallback.json` after a
  launch-time fallback. See `ab_runtime_slots.md`.
- `.local/claude/HOME/`: the project-local persistent home used as
  `$HOME` at rings 1 and 2.
- `.local/claude/` itself as the pinned `CLAUDE_CONFIG_DIR`, so Claude
  Code writes auth, transcripts, history, todos, and settings directly
  under the umbrella. claude-wrangler changes only the self-update
  suppression keys in `settings.json` there and preserves every other
  key.
- `.local/claude/npm/`: the npm-era layout written by older releases.
  Migration (`legacy_npm_layout_migration.md`) leaves only a schema-1
  compatibility projection of the metadata there, plus any locked
  leftovers that the next install, upgrade, or repair removes.

Claude Code owns the config dir's `local/`, `downloads/`, and `cache/`
children, so the runtime root must never be placed there.

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
   the `codex-local` isolation design. On native Windows it must also
   redirect `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`, and
   `LOCALAPPDATA` (`security_ring_model.md` requirement 7).
3. Ring 0 escape hatch
   A per-invocation `--shared-home` launcher argument (consumed by the
   launcher, not forwarded to Claude Code) and an equivalent
   `CLAUDE_LOCAL_SHARED_HOME=1` environment variable must skip the
   home-variable redirection while preserving the config-dir pin.
   `--isolated-home` forces ring 1 for one invocation. An install-time
   `--shared-home` default recorded in managed metadata must also
   remain supported for parity with `codex-wrangler`.
4. Local binary execution
   The launcher core must run the binary of the active slot
   (`slots/<active>/<platform>/claude[.exe]`) directly after the cheap
   slot check, and may run the other slot only through the fallback of
   `ab_runtime_slots.md` requirement 8. It must not use `npx`, a `PATH`
   lookup, or any globally installed `claude`, and it must refuse a
   binary whose real path leaves the runtime root, so a bad global tool
   update cannot take out project work and a squatted name cannot be
   substituted. Nested `claude` calls reach the same binary through the
   PATH shim. While a migration is pending, the Node-free launcher runs
   the npm-era platform binary directly instead
   (`legacy_npm_layout_migration.md` requirement 6).
5. Runtime containment
   Every runtime write must stay inside the runtime root: downloads
   stream into `.staging/<txid>/` on the same volume as the slots, and
   promotion only renames within the runtime root. The runtime root
   must not be, contain, or sit inside the redirected `HOME`, any
   `*/claude/versions` directory reachable from `HOME` or `XDG_*`, or
   the config dir's `local/`, `downloads/`, or `cache/` children. A
   candidate binary's smoke test and `--help` read run in transaction
   scratch with every home variable, `CLAUDE_CONFIG_DIR`, `XDG_*`,
   `TMP`, `TEMP`, and `TMPDIR` redirected there, never against the
   operator's home or the project's config dir. claude-wrangler no
   longer runs npm, so the npm-era `NPM_CONFIG_CACHE` containment rule
   applies only to projects that older releases still manage.
5a. Self-update suppression
   The launcher must export `DISABLE_UPDATES=1`,
   `DISABLE_AUTOUPDATER=1`, and `DISABLE_INSTALLATION_CHECKS=1` before
   executing Claude Code at every ring. Claude Code's updater and
   installer write to `HOME`-relative locations rather than beside the
   running binary, so without these exports a ring-0 session can
   replace, downgrade, or delete the operator's global install, and an
   npm-layout session can run a global `npm install -g`. Updates for a
   managed install are claude-wrangler's responsibility. The launcher
   core repeats these keys through `--settings`, and claude-wrangler
   merges them into `<config dir>/settings.json`
   (`cross_platform_launcher.md` requirement 4).
6. Coexistence with codex-wrangler
   `claude-wrangler` must be able to manage a repository that also
   carries a `codex-wrangler` install. Managed paths
   (`.local/claude/`, `bin/claude-local`, `bin/claude-local.cmd`,
   `README-LOCAL-Start-Claude.md`) must not collide with
   `codex-wrangler` artifacts (`.codex-local/`, `.local/codex-home/`,
   the legacy `.codex-home/`, `bin/codex-local`,
   `README-LOCAL-Start-Codex.md`), and each tool's marked `.gitignore`
   block must leave the other's block untouched.
7. Observability
   `--inspect` and `--selftest` must report the effective
   `CLAUDE_CONFIG_DIR`, `HOME`, and `XDG_*` values the launcher will
   expose (plus the Windows home variables on native Windows), and
   whether the managed install uses isolated or shared home mode.
   `--inspect` reports the launcher environment for both rings on the
   host operating system and names the default ring. `--selftest`
   prints a "Launcher environment at the default ring" block after its
   check results: the ring and mode, each variable's value (or
   "inherited from the caller" at ring 0), and the per-session escape
   hatches.

Privacy of the session directories (POSIX): the pinned config dir and
the redirected HOME hold transcripts, history, and credentials. The
launcher creates either one that is missing with mode 0700, and every
maintenance operation that publishes managed files tightens both to
0700 when they are group- or world-accessible, saying so. A checkout in
a shared location (for example `/srv`) therefore never exposes session
state to other local users. On Windows the inherited ACLs apply.

## Validation

- Launcher tests must cover the pinned config dir, the redirected home
  and XDG paths, the native-Windows home variables, both ring 0 escape
  hatches, and the self-update suppression exports.
- Operations tests must cover runtime-root placement refusals, the
  scratch environment for candidate binaries, and slot-only binary
  selection with no global fallback.
- Inspection tests must cover the reported launcher environment for
  `--inspect` and the selftest environment block at rings 1 and 0.
