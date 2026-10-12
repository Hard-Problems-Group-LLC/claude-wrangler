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
- managed metadata at `<runtime root>/.claude-wrangler.json`
  (`.local/claude/wrangler/.claude-wrangler.json` by default), and the
  npm-era `.local/claude/npm/.claude-wrangler.json` that migration
  reads and keeps as a schema-1 compatibility projection;
- the generated launcher core (`<runtime root>/launch.py`) and its
  `launch.json`, which `bin/claude-local` and `bin/claude-local.cmd`
  run;
- the generated `README-LOCAL-Start-Claude.md`; and
- `claude-wrangler --inspect`.

## Requirements

1. Managed state
   `claude-wrangler` must persist one explicit boolean managed state,
   `reasonable_permissions_enabled`, recording whether reasonable
   permissions are enabled for the target project. New installs
   default to disabled. Managed installs that lack the field must be
   treated as disabled until the operator explicitly enables it.
   Migration from the npm-era layout must carry the recorded value
   over, and the schema-1 projection must keep it.
2. CLI controls
   The tool must accept two mutually exclusive flags:
   `--set-reasonable-permissions` and `--clear-reasonable-permissions`.
   Those flags are valid only for install-like operations, not for
   inspect, self-test, uninstall, update, or rollback.
3. Toggle-only behavior
   When one of the flags is used on an existing managed install without
   an explicit install-changing request such as `--upgrade`,
   `--channel`, `--version`, or `--repair-install`, the command must
   preserve the current managed selector, channel, and installed
   version, rewriting managed files from stored state without
   downloading, promoting, or migrating anything. On a layout whose
   migration is still pending, it renders the Node-free
   migration-pending launcher (`legacy_npm_layout_migration.md`
   requirement 6).
4. Launcher defaults
   When reasonable permissions are enabled, the launcher core must add
   `--permission-mode acceptEdits` only when the caller did not
   already choose permission behavior explicitly. That keeps ordinary
   workspace file edits flowing while Bash commands, git commit, and
   git push still prompt. The flag follows the launcher's own
   `--settings` argument and precedes the caller's arguments. The
   managed default must never inject `--dangerously-skip-permissions`.
5. Explicit caller intent
   The launcher must treat the following inputs as explicit caller
   intent that suppresses the managed default:
   - `--permission-mode ...`
   - `--permission-mode=...`
   - `--dangerously-skip-permissions`
6. Generated launcher shape
   The launcher must carry one explicit boolean,
   `reasonable_permissions` in `launch.json`, that controls the
   reasonable-permissions behavior. The shell shims carry no permission
   logic, so clearing the feature later regenerates `launch.json` from
   stored state rather than editing old launcher text.
7. Operator visibility
   The generated local README, the install summary, and `--inspect`
   output must state whether reasonable permissions are enabled so
   operators understand why the launcher may be adding a default
   permission-mode flag.

## Validation

- Unit tests must cover CLI parsing and config normalization for the
  set and clear flags.
- Unit tests must cover persisted metadata, migration carry-over, and
  inspection output for the boolean state.
- Launcher-core tests must cover enabled and disabled states, explicit
  caller overrides, argument order, and clean clear-path regeneration
  of `launch.json` from stored false or unset state.
