# Windows Support and a Native Claude Code Runtime

Title: Engineering Change Request: Windows support, Node-free native
Claude Code runtime with A/B slots, and a cross-platform launcher
Author: Claude (AI) with the operator
Date: 2026-10-12T01:00:00-07:00
Status: Approved (operator decisions recorded 2026-10-11/12)
Reviewers: operator (repository maintainer)
Related Work: `feature/windows-platform-support`;
`docs/specifications/native_runtime_distribution_and_verification.md`,
`ab_runtime_slots.md`, `legacy_npm_layout_migration.md`,
`cross_platform_launcher.md`, `prerequisites_and_platform_entry_points.md`,
`security_ring_model.md` (requirements 7-9);
`closed/2026-10-12-launcher-self-update-escapes-project.md`;
codex-wrangler `atomic_ab_managed_installation.md`.

## Problem Statement

claude-wrangler came from Linux. It installs Claude Code through a managed
npm tree, launches it through a bash script, and redirects only `HOME` for
ring 1. On Windows none of that works: there is no Node.js by default, the
launcher cannot run from cmd or PowerShell, native programs ignore `HOME`,
and Claude Code has no sandbox. Separately, research found that Claude
Code's self-updater can rewrite the operator's global install from a
project session, and that npm is the weaker distribution channel.

## Goals

- Install, update, repair, roll back, inspect, and uninstall project-local
  Claude Code on Windows, macOS, and Linux with one design.
- Remove Node.js and npm from the runtime path.
- Never let a project session update, replace, or delete anything outside
  the project.
- Survive failed or interrupted maintenance and accidental deletion with
  an A/B slot set, configuration flips, and launch-time fallback.
- Make partial progress toward macOS support along the way.

## Operator Decisions

1. Fetch Claude Code's native binary directly from Anthropic's signed
   release distribution instead of npm (2026-10-11). Verify both manifest
   signatures with the Python standard library.
2. Wrangled instances check for updates and notify; they never update
   automatically (2026-10-11).
3. Use an A/B install set with configuration flips and fallbacks
   (2026-10-11).
4. Every project carries its own `scripts/install_prerequisites.*` that
   installs whatever is missing; follow and extend that pattern
   (2026-10-11).
5. Native Windows defaults to ring 1 with a per-session warning and a
   per-project opt-out for personal work (2026-10-12).
6. The launcher is a generated standard-library Python core with thin
   `sh` and `cmd` shims (2026-10-12).
7. Verify code-derived behavior empirically in Windows Sandbox; the
   operator enables the feature when that work is reached (2026-10-12).
8. No remote CI yet; write a CI proposal (2026-10-12).

## Defaults Adopted Without Objection

- Download raw binaries from `downloads.claude.ai`; GitHub release
  archives are a possible later transport.
- `next` becomes a deprecated alias of `latest`.
- Existing npm-based projects migrate automatically on their next install,
  upgrade, or repair, adopting the binary they already have.
- The self-update hotfix ships first so it can reach `trunk` early.
- No new TheKnowledge ECRs; TheKnowledge is being replaced by FieldManual.

## Non-Goals

- Managed `--mode dev` and `--mode venv-only` on native Windows (pyenv and
  direnv); contributors use `install_prerequisites --dev` or WSL2.
- Sandboxing on native Windows.
- Editing TheKnowledge-managed starter files.

## Risks and Mitigations

See the five specifications above. The main residual risks are
code-derived behavior not yet confirmed empirically (planned Windows
Sandbox experiments) and per-user Windows stores that ring 1 cannot
isolate (documented, with the WSL2 recommendation).

## Testing and Validation

Each specification lists its validation. The full suite must pass on
Windows with home variables redirected, and on Linux.
