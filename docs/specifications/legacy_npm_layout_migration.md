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
   `package-lock.json`, then `package.json` (the existing inference order)
   for the selector, channel, exact version, `shared_home`, and
   `reasonable_permissions_enabled`. A custom legacy `--local-dir`
   recorded in metadata is honored. Discovery must not write anything.

3. **Version continuity**

   Migration targets the same exact version the legacy install pinned.
   When that version is below the native floor (2.1.207), migration must
   stop and tell the operator to run an explicit
   `--upgrade --channel <channel>`; it must never upgrade silently. A
   legacy `next` channel becomes `latest` with a deprecation notice.

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
      `.npm-cache/` from the legacy tree. On Windows, locked files are
      renamed to `.retired-<txid>` or left in place and reported; the next
      install, upgrade, or repair retries the cleanup.

   A failure before step 4 leaves the legacy launcher and npm tree fully
   working.

6. **Launcher during migration**

   `--update` and the permission toggles run against a legacy layout
   before migration has completed. They must render a Node-free launcher
   that executes the legacy platform binary directly, exports the
   self-update suppression variables, and reports "migration pending";
   they must never render a launcher that points at a runtime that does
   not exist yet.

7. **Config-dir spelling**

   The `CLAUDE_CONFIG_DIR` value the new launcher exports must be
   byte-identical to the value the legacy launcher exported, because
   Claude Code keys some per-config-dir state (including a gated Windows
   Credential Manager backend) on that exact string.

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
- Launcher rendering for `--update` against an unmigrated layout.
