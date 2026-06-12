# Reasonable Permissions for claude-local Launchers

## Purpose

Define how `claude-wrangler` persists and renders an opt-in managed
launcher default that keeps normal in-repository Claude Code edits
flowing while still requiring approval prompts for commands and for
maintenance operations that need escalation, such as commit and push
workflows.

## Scope

This specification covers:

- `claude-wrangler --set-reasonable-permissions`;
- `claude-wrangler --clear-reasonable-permissions`;
- managed metadata under `.local/claude/npm/.claude-wrangler.json`;
- the generated `bin/claude-local` launcher;
- the generated `README-LOCAL-Start-Claude.md`; and
- `claude-wrangler --inspect`.

## Requirements

1. Managed state
   `claude-wrangler` must persist one explicit boolean managed state
   recording whether reasonable permissions are enabled for the target
   project. New installs default to disabled. Managed installs that
   lack the field must be treated as disabled until the operator
   explicitly enables it.
2. CLI controls
   The tool must accept two mutually exclusive flags:
   `--set-reasonable-permissions` and `--clear-reasonable-permissions`.
   Those flags are valid only for install-like operations, not for
   inspect, self-test, uninstall, or update.
3. Toggle-only behavior
   When one of the flags is used on an existing managed install without
   an explicit install-changing request such as `--upgrade`,
   `--channel`, or `--version`, the command must preserve the current
   managed selector, channel, and resolved package version, rewriting
   managed files from stored state without silently upgrading or
   reinstalling Claude Code.
4. Launcher defaults
   When reasonable permissions are enabled, the generated launcher must
   add `--permission-mode acceptEdits` only when the caller did not
   already choose permission behavior explicitly. That keeps ordinary
   workspace file edits flowing while Bash commands, git commit, and
   git push still prompt. The managed default must never inject
   `--dangerously-skip-permissions`.
5. Explicit caller intent
   The launcher must treat the following inputs as explicit caller
   intent that suppresses the managed default:
   - `--permission-mode ...`
   - `--permission-mode=...`
   - `--dangerously-skip-permissions`
6. Generated launcher shape
   The launcher must render one explicit shell boolean that controls
   the reasonable-permissions behavior, so clearing the feature later
   regenerates a clean launcher from stored state rather than editing
   old launcher text.
7. Operator visibility
   The generated local README and `--inspect` output must state whether
   reasonable permissions are enabled so operators understand why the
   wrapper may be adding a default permission-mode flag.

## Validation

- Unit tests must cover CLI parsing and config normalization for the
  set and clear flags.
- Unit tests must cover persisted metadata and inspection output for
  the boolean state.
- Launcher tests must cover enabled and disabled states, explicit
  caller overrides, and clean clear-path regeneration from stored
  false or unset state.
