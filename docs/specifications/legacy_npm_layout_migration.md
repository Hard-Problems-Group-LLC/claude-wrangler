# Legacy npm Layout Migration

## Purpose

Move projects installed by npm-based claude-wrangler releases
(`.local/claude/npm/`) onto the native A/B runtime without losing auth,
sessions, memories, home mode, permission defaults, or the pinned version,
and without a window in which the project has no working launcher.

## Scope

Discovery of legacy state, the migration transaction, the launcher during
migration, cleanup of the npm tree, and claude-wrangler rollback. Session
state under `CLAUDE_CONFIG_DIR` and the redirected `HOME` is never part of
the migration.

## Required Behavior

1. **When migration runs**

   Install, upgrade, and repair migrate a project automatically when they
   find a legacy layout and no complete native runtime. Inspect, selftest,
   uninstall, `--dry-run`, and `--skip-install` never migrate; inspect and
   selftest report "migration pending" instead.

2. **Discovery is read-only**

   Legacy discovery reads `.local/claude/npm/.claude-wrangler.json`, then
   `package-lock.json`, then `package.json` (the existing inference
   order), then the installed wrapper's `package.json`, for the
   selector, channel, exact version, `shared_home`, and
   `reasonable_permissions_enabled`. Discovery must not write anything.

   A custom npm-era `--local-dir` is recorded only inside that
   directory, so candidate directories are tried in this order:
   1. an explicit `--local-dir` that names an npm-era directory (the
      native runtime then goes to the default `.local/claude/wrangler`);
   2. the native metadata's record of the directory it migrated from;
   3. the directory the project's generated launcher still names: an
      npm-era bash launcher's `local_prefix="$repo_root/<dir>"` line,
      counted only when the launcher carries claude-wrangler's managed
      marker and runs `node_modules/.bin/claude`, or the legacy binary
      path in a migration-pending `launch.json` (requirement 6);
   4. the default `.local/claude/npm`.

   A candidate counts only when it resolves inside the project and holds
   an npm-era layout, so `claude-wrangler .` migrates a custom tree
   without the operator repeating `--local-dir`.

3. **Version continuity**

   Without an explicit target, migration targets the same exact version
   the legacy install pinned. When that version is below the native
   floor (2.1.207), migration must stop and tell the operator to run an
   explicit `--upgrade --channel <channel>`; it must never upgrade
   silently. An explicit target (`--upgrade`, or `--channel` or
   `--version` on a plain install) migrates straight to that target,
   which is still subject to the floor. A legacy `next` channel becomes
   `latest` with a deprecation notice.

4. **Adopt in place**

   When the legacy tree contains the platform binary
   (`node_modules/@anthropic-ai/claude-code-<platform>/claude[.exe]` or
   the wrapper's `bin/claude.exe` copy), migration hashes it against the
   signed manifest for the pinned version and, on a match, copies it into
   the staging candidate instead of downloading. A mismatch is reported
   and the binary is downloaded.

5. **Transaction order**

   With the maintenance lock held:
   1. discover legacy state;
   2. prepare and validate the candidate (adopted or downloaded) as for a
      fresh install;
   3. promote it into slot `a`;
   4. write schema-2 metadata atomically (commit point);
   5. write the new launcher, shims, and README atomically;
   6. write a schema-1 compatibility projection at
      `.local/claude/npm/.claude-wrangler.json` that keeps the selector,
      channel, version, `shared_home`, `reasonable_permissions_enabled`,
      and a `migrated_to` pointer;
   7. remove `node_modules/`, `package.json`, `package-lock.json`, and
      `.npm-cache/` from the legacy tree. On Windows, locked entries are
      renamed to `.retired-<txid>-<name>` or left in place and reported;
      the next install, upgrade, or repair retries the cleanup.

   A failure before step 4 leaves the legacy launcher and npm tree fully
   working.

6. **Launcher during migration**

   `--update` and the permission toggles run against a legacy layout
   before migration has completed. They must render a Node-free launcher
   that executes the legacy platform binary directly, exports the
   self-update suppression variables, and reports "migration pending";
   they must never render a launcher that points at a runtime that does
   not exist yet. The launcher is the native core and shims, with a
   `legacy` block (binary path and version) in `launch.json`; the core,
   `launch.json`, shims, and `update-check.json` already live in the
   native runtime root. The binary must pass the structural check of
   `native_runtime_distribution_and_verification.md` requirement 9; when
   no binary passes, the existing launcher is left untouched and a
   warning names the fix. Until migration writes schema-2 metadata, the
   state these operations change is kept in the schema-1 legacy
   metadata. No subcommand list is recorded for a legacy binary, so the
   launcher always passes `--settings`.

7. **Config-dir spelling**

   Claude Code keys some per-config-dir state (including a gated Windows
   Credential Manager backend) on the exact `CLAUDE_CONFIG_DIR` string,
   so the new launcher must export the value the legacy launcher
   exported for the same invocation. Byte identity cannot be guaranteed
   in general: the npm-era bash launcher built the value from `pwd`, so
   it already varied with how the operator reached the checkout. The
   achievable guarantee is:
   - the new launcher derives the value the same way, from its own
     invocation path, joined with the same repo-relative config-dir
     suffix. That suffix, and the home suffix, come from the legacy
     metadata, or from the npm-era launcher's `export CLAUDE_CONFIG_DIR=`
     and `export HOME=` lines when the metadata does not record them;
   - `launch.json` records `config_dir_spelling`. `native` uses the
     host's own separators. `forward-slash` is chosen automatically on
     native Windows when an npm-era layout is migrated without native
     metadata, because Git Bash handed native programs `C:/...`; it
     converts backslashes to `/` and upper-cases the drive letter.
     The recorded spelling then persists.

   The value matches for the same invocation path, up to drive-letter
   case. UNC, `subst`, and WSL (`/mnt/c/...`) spellings of the same
   checkout produce different strings, as they did before migration.

8. **Rolling claude-wrangler back**

   Older releases read the schema-1 projection, so their inspect and
   uninstall keep working. The README must state that after migration an
   older claude-wrangler should be used only for `--uninstall`; an older
   release's reconfigure-only operations would rewrite the launcher to a
   Node-based form.

9. **Rolling forward again**

   If both a legacy npm runtime and a complete native runtime exist, the
   native runtime is authoritative when the native metadata's
   `generated_at` is newer; otherwise migration runs again. Retained
   slots are never deleted to resolve the ambiguity.

## Validation

- Migration from default and custom legacy local dirs, with and without
  metadata, with a corrupt lockfile, and with `next`.
- Adopt-in-place with a matching binary and fallback to download on a
  mismatch.
- Failure injection before and after the commit point.
- Below-floor refusal.
- Deferred cleanup when a legacy file is locked.
- Launcher rendering for `--update` against an unmigrated layout, and
  no rendering when the legacy binary is missing or fails the
  structural check.
- Discovery of a custom npm-era directory from the old launcher and
  from a migration-pending `launch.json`, without `--local-dir`.
- Selftest reporting "migration pending" as a warning, and uninstall
  never migrating.
