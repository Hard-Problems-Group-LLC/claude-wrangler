# Cross-Platform Launcher

## Purpose

Start project-local Claude Code sessions the same way from bash, zsh,
Git Bash, cmd.exe, Windows PowerShell 5.1, PowerShell 7, and WSL, with one
implementation of the security-relevant launch logic.

## Scope

The generated launcher core, its shell shims, environment construction
for each security ring on each operating system, update-suppression
layering, subcommand interception, update notices, the nested-`claude`
PATH shim, the native-Windows sandbox warning, and process semantics.

## Design

- **Core:** `.local/claude/wrangler/launch.py`, a generated,
  standard-library-only Python 3.9+ script. It contains all launch logic
  and imports nothing from claude-wrangler, so a project keeps launching
  if claude-wrangler is later uninstalled.
- **Shims:**
  - `bin/claude-local`: POSIX `sh`, LF line endings.
  - `bin/claude-local.cmd`: cmd batch, CRLF line endings, ASCII only.

  PowerShell resolves `.\bin\claude-local` to the `.cmd` shim, so no
  `.ps1` is generated and execution policy never applies.
- **Interpreter:** each shim first runs the absolute interpreter path
  recorded at generation time (the Python running claude-wrangler). If
  that interpreter is gone it falls back to `py -3`, then `python3`, then
  `python`, accepting only Python 3.9+ by exit-code probe, never by
  parsing interpreter output.
- **Git Bash and MSYS:** when `uname -s` reports MINGW, MSYS, or
  CYGWIN, the `sh` shim accepts only a native Windows Python and hands
  the core Windows-form paths from `cygpath -w`. It also exports
  `MSYS_NO_PATHCONV=1` and `MSYS2_ARG_CONV_EXCL='*'`, because Git Bash
  otherwise rewrites arguments such as the prompt `/review` into
  `C:/Program Files/Git/review` before Python sees them. When the
  caller had set neither variable, the shim also sets the marker
  `CLAUDE_LOCAL_MSYS_ARGS=1`, and the core then removes all three from
  Claude Code's environment, so the conversion override does not leak
  into the commands Claude Code runs. Values the caller set are kept.

## Required Behavior

1. **Repository root**

   The core derives the repository root from its own location, so a
   moved or renamed checkout keeps working after `--repair-install`
   regenerates the shims.

2. **Config-dir pinning**

   `CLAUDE_CONFIG_DIR` is set to the managed config dir at every ring,
   using the exact spelling recorded in metadata.

3. **Ring environment**

   Ring selection keeps today's interface: the install-time default,
   `CLAUDE_LOCAL_SHARED_HOME=1`, `--shared-home`, and `--isolated-home`.
   At ring 1 the core sets:
   - every OS: `HOME` and `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
     `XDG_STATE_HOME`, `XDG_DATA_HOME` under the redirected home;
   - Windows additionally: `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH` (split
     from the same path), `APPDATA`, and `LOCALAPPDATA` under the
     redirected home, because native Windows programs (Node/Bun, Python,
     npm, Git Credential Manager) resolve home from these and ignore
     `HOME`.

   At ring 0 the core leaves the operator's home variables alone. It
   never sets `HOME` without the Windows variables on Windows, because a
   mismatch splits Claude Code's paths between two homes.

4. **Self-update suppression in three layers**

   - Environment: `DISABLE_UPDATES=1`, `DISABLE_AUTOUPDATER=1`,
     `DISABLE_INSTALLATION_CHECKS=1`.
   - Command line: `--settings` with an `env` block repeating those
     three keys and `"disableDeepLinkRegistration": "disable"`, because
     command-line settings outrank project and local settings, and a
     cloned repository's `.claude/settings.json` can otherwise switch
     updates back on before the trust prompt. The core puts `--settings`
     first and omits it only when the first caller argument is a
     subcommand that refused it during an install-time probe:
     - for each subcommand recorded from the candidate's `--help`, except
       `update`, `upgrade`, and `install` (which the core intercepts),
       claude-wrangler runs `<candidate> --settings <json> <subcommand>
       --help` in transaction scratch within a 120-second total budget;
     - a subcommand accepts the flag only when the probe exits 0 and
       prints that subcommand's own usage line; an option error, the
       root command's help, a timeout, or an exhausted budget counts as
       a refusal;
     - the refusals are stored as `settings_rejected` in the slot's
       completion record and shown by `--inspect`. A slot promoted
       before the probe existed has no such field, and the core then
       omits `--settings` for every recorded subcommand.

     Claude Code 2.1.296 does not report an error for `attach`, `logs`,
     `respawn`, `rm`, `stop`, or `kill` behind a leading `--settings`;
     it routes the invocation to the root command instead, so the probe
     records those six as refusals. For refused subcommands only the
     environment and config-dir layers apply.
   - Config dir: claude-wrangler merges the same keys into
     `<config dir>/settings.json` at install and upgrade, preserving
     every other key, so detached processes that bypass the launcher
     inherit them.

   If the caller passes its own `--settings` (or `--settings=...`), the
   core exits 2 with a hint to put settings in
   `<config dir>/settings.json`. Whether several `--settings` flags
   merge safely has not been established, so they are never combined.

5. **Settings scan**

   Before launching, the core reads the project's `.claude/settings.json`
   and `.claude/settings.local.json` and warns when either sets any of
   the suppression variables to a falsy value or sets
   `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. Malformed files are
   reported, not fatal.

6. **Subcommand interception**

   When the first non-launcher argument is `update`, `upgrade`, or
   `install`, the core prints that the runtime is managed by
   claude-wrangler, names `claude-wrangler --upgrade --channel
   <channel>`, and exits 2 without running Claude Code. Claude Code's own
   refusal says "contact your administrator", which is misleading here.

7. **Slot selection and fallback**

   The core reads `active-slot`, validates the slot cheaply, and falls
   back to the other slot as `ab_runtime_slots.md` requirement 8
   describes. It never executes a binary outside the runtime root, with
   one exception: while a migration is pending
   (`legacy_npm_layout_migration.md` requirement 6), `launch.json`
   carries a `legacy` block and the core runs the npm-era platform
   binary it names. That binary must be a regular file inside the
   repository, and claude-wrangler structurally checks it before
   rendering such a launcher. It is not checked against a signed
   manifest, and there is no slot to fall back to.

8. **Nested `claude` calls**

   The core prepends `.local/claude/wrangler/shim/` to `PATH`. That
   directory holds `claude` (sh) and `claude.cmd` shims that re-enter the
   launcher core, so hooks, scripts, `claude -p`, and background respawns
   run the project's pinned binary with the project's environment rather
   than the operator's global `claude`.

9. **Update notices**

   The core prints at most one line to stderr when the cached update
   check names a newer release on the installed channel, following
   `native_runtime_distribution_and_verification.md` requirement 12.
   The core's own refresh records the unsigned channel pointer in
   `pointer_version` only and never writes `latest_version`, which holds
   manifest-verified data from `claude-wrangler --update`. A release that
   only the pointer names is announced as unverified. `--check-updates`
   is consumed by the core and never forwarded.

10. **Native-Windows sandbox warning**

    On native Windows (not WSL) the core prints one line per session
    stating that Claude Code runs commands unsandboxed on native Windows
    and that isolation covers Claude's state and redirected-home tools
    but not per-user Windows stores. The warning is suppressed when the
    project sets `--set-windows-sandbox-warning off` at install time.

11. **Reasonable permissions**

    The existing `--permission-mode acceptEdits` default and its
    caller-override detection carry over unchanged. The managed state is
    the JSON boolean `reasonable_permissions` in `launch.json`, read by
    the core, instead of a shell variable in the launcher.

12. **Node.js parity check**

    The existing `.nvmrc`/`.node-version`/`.tool-versions` check becomes
    warn-only and never requires `node`; `CLAUDE_LOCAL_PREFLIGHT`
    (`warn`, `strict`, `off`) keeps its meaning.

13. **Process semantics**

    On POSIX the core replaces itself with `os.execve`. On Windows it
    starts Claude Code as a child that inherits the console and standard
    handles, ignores Ctrl+C in the parent while waiting, and exits with
    the child's exit code, mapping `0xC000013A` to 130.

14. **Generated-file hygiene**

    The core, shims, and PATH shims are regenerated by install, upgrade,
    update, repair, and reconfiguration, and written atomically. The
    `.gitignore` block covers `bin/claude-local` and
    `bin/claude-local.cmd`.

## Validation

- Ring environment tables for Windows and POSIX at rings 0 and 1, with
  injected `os.name` and environment.
- Settings layering: the emitted `--settings` JSON, the config-dir merge
  preserving unrelated keys, and the settings-scan warnings.
- The install-time `--settings` probe and the launcher's per-subcommand
  omission, including records without `settings_rejected`.
- Interception of `update`, `upgrade`, and `install`.
- Slot fallback and refusal.
- Shim tests on Windows through cmd, PowerShell 5.1, PowerShell 7, and
  Git Bash, including paths with spaces, non-ASCII characters, `&`, and
  `%`, and exit-code propagation; POSIX shim tests through `sh`.
- The core runs with no `node` on `PATH`.
