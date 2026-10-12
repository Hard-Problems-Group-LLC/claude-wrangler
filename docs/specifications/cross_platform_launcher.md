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
  `.ps1` is generated and execution policy never applies. A
  same-basename `.ps1` must never be generated: PowerShell prefers
  `x.ps1` over `x.cmd`, and under the Restricted policy it then errors
  instead of falling back.

  cmd.exe re-parses the arguments of every batch file (double quotes,
  `%`, `^`, `&`, and newlines), so no `.cmd` shim can forward arbitrary
  arguments verbatim. `bin\claude-local.cmd` is the entry point for
  people typing at a prompt; programs that pass prompts, system prompts,
  or JSON configurations as arguments run
  `<python> -I .local\claude\wrangler\launch.py <args...>` with an
  argument list instead, and nested calls inside a session never reach a
  batch file on Windows (requirement 8).
- **Interpreter:** each shim first runs the absolute interpreter path
  recorded at generation time (the Python running claude-wrangler). If
  that interpreter is gone it falls back to `py -3`, then `python3`, then
  `python`, accepting only Python 3.9+ by exit-code probe, never by
  parsing interpreter output. The cmd shim names the fallbacks
  `py.exe`, `python3.exe`, and `python.exe`, so a `.bat` interpreter
  shim (pyenv-win) can never take over the batch file and end it with
  exit 0 before anything launched. Every probe runs with
  `PYTHON_MANAGER_AUTOMATIC_INSTALL=false`, so the Windows Python
  install manager never installs a runtime during a launch: the cmd
  shim sets it inside an inner `setlocal` whose `endlocal` restores the
  caller's value, unexpanded, before the launch line, and the `sh` shim
  sets it per probe command only.
- **Shim robustness:** the `sh` shim derives the repository root with
  `CDPATH= cd -- "$(dirname -- "$0")/..."`, so an exported `CDPATH`
  cannot redirect `cd` or make it print the directory into the captured
  root, and it embeds every managed path single-quoted, so a `$`,
  backtick, or double quote in `--local-dir` or `--launcher` stays
  literal. The cmd shim doubles `%` in managed paths and refuses a
  double quote. Its launch line ends with `& call exit /b
  %%ERRORLEVEL%%`: cmd re-reads a batch file by byte offset after each
  command, so an upgrade that rewrites the shim while a session runs
  would otherwise make cmd resume in the middle of the new content;
  `call` re-expands `ERRORLEVEL` after Claude Code exits, so its exit
  code passes through. The `CMD_EXIT` lines after it run only when an
  unbalanced quote in the arguments swallowed that exit. Managed paths
  may not contain control characters.
- **Shim self-location:** both `.cmd` shims find their own directory
  through one label, `:claude_local_self`, placed after their exit
  lines and reached only by `call` before Claude Code starts. A
  top-level `%~dp0` describes the batch file as it was invoked: when
  cmd finds the file through `PATH` by a quoted bare name (`"claude"`
  in a `shell=True` string, a Node `exec` string, or an npm script), it
  names the caller's current directory, so the shim would run whatever
  launcher or `launch.py` sits at that relative location. Inside a
  called label `%~dp0` always names the file's real directory. Absolute
  paths are not embedded instead, because the `.cmd` files are
  ASCII-only and checkout roots may be non-ASCII. The label does not
  bring back the LF label-scan hazard that keeps `install.cmd`
  label-free: the shims are always written with CRLF and
  content-checked, and the label is reached before the launch line, so
  a rewrite during a session is still never read. The variable the
  label sets is cleared before the `call`, and a shim whose label cannot
  be found (a copy saved with LF endings, or an edit) exits 1 with a
  repair hint instead of resolving paths against the caller's
  directory. `shim\claude.cmd` runs inside its own `setlocal`.
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
   using the exact spelling recorded in metadata. In the native spelling
   on Windows, the repository-root spelling the shim supplied is first
   canonicalized: the drive letter is upper-cased and 8.3 short
   components are expanded (`GetLongPathNameW`), because Claude Code
   keys its per-project directory, its `.claude.json` project entries,
   and its secure-storage name on exact path strings, and one checkout
   reached as `c:\...` or through a short name would otherwise fork that
   state. Subst drives, mapped drives, and UNC paths keep the caller's
   spelling, links are not resolved, and Claude Code's working directory
   is left as the caller set it, as stock Claude Code would receive it.
   The forward-slash spelling of a migrated project already upper-cases
   the drive and is not changed further. `--inspect` reports the
   resolved project root.

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

   At every ring, unless the launch is a nested launch of the same
   project (`CLAUDE_LOCAL_NOTICES_SHOWN` equals the pinned config dir),
   the core drops the variables another Claude Code session exports to
   its children: `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`,
   `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ATTENDED`,
   `CLAUDE_CODE_EXECPATH`, `CLAUDE_CODE_BRIDGE_SESSION_ID`,
   `CLAUDE_PID`, `CLAUDE_SECURESTORAGE_CONFIG_DIR`, and
   `CLAUDE_CODE_MESSAGING_*`. Without that, a project session started
   from the operator's global session, or from another project's
   session, behaves as that session's child and inherits its messaging
   endpoint and token. `CLAUDE_CODE_SSE_PORT` (IDE integration),
   `CLAUDE_CODE_ENTRYPOINT`, and `CLAUDE_EFFORT` are kept unless
   `CLAUDECODE` shows the caller is itself a Claude Code session,
   because IDE terminals and Agent SDK hosts set them for the CLI they
   start. The cmd shims'
   helper variables (`CLAUDE_LOCAL_CORE`, `CLAUDE_LOCAL_PY`, and the
   like) never reach Claude Code.

   Credentials, identity, and provider selection in the environment
   take precedence over the project's own login in the pinned config
   dir, so a persistent user-level key would authenticate every project
   as one account. The core treats as such every name starting with
   `ANTHROPIC_` or `CLAUDE_CODE_OAUTH_`, plus
   `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `AWS_BEARER_TOKEN_BEDROCK`,
   `CLAUDE_CODE_CLIENT_CERT`, `CLAUDE_CODE_CLIENT_KEY`,
   `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE`, the provider switches
   `CLAUDE_CODE_USE_BEDROCK`, `_VERTEX`, `_FOUNDRY`, `_MANTLE`,
   `_ANTHROPIC_AWS`, `_ANTHROPIC_GOOGLE_CLOUD`, and `_GATEWAY`, and the
   host and remote-session account variables
   `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`,
   `CLAUDE_CODE_HOST_AUTH_ENV_VAR`, `CLAUDE_CODE_HOST_CREDS_FILE`,
   `CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR`,
   `CLAUDE_CODE_SESSION_ACCESS_TOKEN`, `CLAUDE_CODE_API_BASE_URL`,
   `CLAUDE_CODE_ORGANIZATION_UUID`, and `CLAUDE_CODE_ACCOUNT_UUID`
   (case-insensitively on Windows). Matching by prefix covers variables later releases add.
   Unless `launch.json` sets `credential_passthrough` (written by
   `--set-credential-passthrough on`; absent means off), the core
   removes all of them, empty or not, at every ring and for nested
   launches too, and names the non-empty ones, never their values, once
   per session with the command that turns passthrough on. With
   passthrough on, they are kept and the note says so, with the command
   that turns it off. Credential sources outside the environment
   (settings-file `env` blocks, `apiKeyHelper`, cloud SDK credential
   chains) are not affected.

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

   Claude Code dispatches a subcommand that follows leading root options
   (`--verbose update`, `--model x install`) or `--`, so the core
   refuses when any forwarded argument is exactly `update`, `upgrade`,
   or `install`, scanning until the first argument that is another
   subcommand recorded for the selected slot (whose operands may
   legitimately be such a word, as in `plugin install <name>`). When
   the completion record lists no subcommands (the npm-era layout), a
   built-in list (`mcp`, `plugin`, `plugins`, `config`) stops the scan
   instead. An option value equal to a subcommand name
   (`--agent mcp update`) ends the scan early; `DISABLE_UPDATES` still
   stops Claude Code's updater there. It then
   prints that the runtime is managed by claude-wrangler, names
   `claude-wrangler --upgrade --channel <channel>`, and exits 2 without
   running Claude Code. A plain first-argument form is refused before
   slot selection, so it gets this message even without a usable
   runtime. A false positive (a one-word prompt such as `-p install`,
   which Claude Code would itself dispatch to `install`) is only a clear
   refusal. Claude Code's own refusal says "contact your administrator",
   which is misleading here; both of its update and install handlers
   stop when `DISABLE_UPDATES` is set, which covers nested calls that
   bypass the core (requirement 8). The rule for omitting `--settings`
   (requirement 4) still looks only at the first argument, which is
   what Claude Code's own pre-dispatch does.

7. **Slot selection and fallback**

   The core reads `active-slot`, validates the slot cheaply, and falls
   back to the other slot as `ab_runtime_slots.md` requirement 8
   describes. The cheap check includes the verification stamp of
   `ab_runtime_slots.md` requirement 15: a binary whose size,
   modification time, or (on POSIX) inode differs from the stamp
   maintenance wrote after its last full verification, or that has no
   stamp, is refused. On Windows the core expands an 8.3 short spelling
   of the runtime root before it starts the binary, so maintenance's
   in-use check sees the long-name image path. It never executes a
   binary outside the runtime root, with one exception: while a
   migration is pending (`legacy_npm_layout_migration.md` requirement
   6), `launch.json` carries a `legacy` block and the core runs the
   npm-era platform binary it names. That binary must be a regular file
   inside the repository; `--update` renders such a launcher only for a
   binary that matches the signed manifest, and the offline toggles only
   in a checkout that already had a claude-wrangler launcher. There is
   no slot to fall back to.

8. **Nested `claude` calls**

   The core prepends `.local/claude/wrangler/shim/` to `PATH`. That
   directory holds `claude` (sh) and `claude.cmd` shims that re-enter the
   launcher core, so hooks, scripts, `claude -p`, and background respawns
   run the project's pinned binary with the project's environment rather
   than the operator's global `claude`.

   On native Windows, in slot mode, the core also puts the selected
   slot's platform directory in front of the shim directory, spelled
   with long names so maintenance's in-use check matches the image
   path. CreateProcess and libuv-based spawns (Python list calls, Node,
   Bun) look only for `.exe` files and would skip `claude.cmd` for the
   next `claude.exe` on `PATH`, typically the operator's global install;
   and any caller that reaches `claude.cmd` has cmd.exe re-parse the
   arguments, which can run a second command, expand `%` variables, or
   truncate a JSON argument. With the slot directory first, Python,
   PowerShell, cmd.exe, and Git Bash all start the slot's `claude.exe`
   directly with the arguments unchanged (apart from PowerShell 5.1's own
   legacy quoting). The directory is used only while it holds nothing
   but the binary, so a future release's extra executables never shadow
   tools session-wide; otherwise the order falls back to the shim first.
   The npm-era layout's directory holds other files and is never used.
   On that fallback the core prints a one-line warning (not repeated for
   nested launches of the same project) naming the reason, because
   nested calls then keep cmd.exe's re-parsing and `.exe`-only lookups
   can again reach another `claude.exe` on `PATH`; a caller that names
   `claude.cmd` itself gets the re-parsing in every case.

   Such a nested call inherits the redirected homes, `CLAUDE_CONFIG_DIR`,
   and the `DISABLE_*` variables, and `<config dir>/settings.json` still
   applies, but it bypasses the core's per-invocation layers: the
   `--settings` override (so a cloned repository's
   `.claude/settings.json` can blank the suppression variables for
   nested processes; the top-level settings scan still warns), the
   interception of requirement 6 (Claude Code's own `DISABLE_UPDATES`
   refusal applies), the reasonable-permissions default, the stamp
   check (the parent already checked the same image, and an in-use slot
   is never displaced), the settings scan, and the caller `--settings`
   refusal. A slot change during a session is safe: the session keeps
   the slot it launched.

   A runtime root whose spelling contains the `PATH` separator (`;` on
   Windows, `:` elsewhere) would split the entry and let nested calls
   fall through to a global binary; install, upgrade, repair,
   `--update`, `--rollback`, and the toggles refuse such a layout, and
   the core refuses to launch from one.

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
    the child's exit code, mapping `0xC000013A` to 130. Right after the
    child starts, the core puts it into a job object with
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and
    `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`, holding the only,
    non-inheritable handle. Killing the launcher (a tool timeout, an SDK
    cancellation, or an IDE that terminates only its direct child, and a
    venv `python.exe` redirector, whose own job takes the base
    interpreter down) then ends Claude Code too instead of leaving it
    editing files, spending tokens, holding its slot in use, and holding
    the caller's pipes open. Processes Claude Code starts itself (tools,
    background sessions, the daemon) are never in the job and live on as
    before. Job setup is best effort; a failure launches without one.
    Killing the cmd.exe that runs `bin\claude-local.cmd` still leaves
    the launcher running, because cmd never ends its child: programmatic
    callers kill the process tree or run `launch.py` directly.

15. **Private session directories**

    The pinned config dir and the redirected home hold transcripts and
    credentials. When the core creates either one it makes it private:
    mode 0700 on POSIX, and on Windows a protected DACL granting full
    control to the operator, the directory's owner when that is an
    account, SYSTEM, and Administrators, with inheritance from the
    parent removed (best effort: a filesystem without ACLs keeps its
    defaults). Existing directories are left to maintenance
    (`session_state_isolation_and_config_dir_pinning.md`).

14. **Generated-file hygiene**

    The core, shims, and PATH shims are regenerated by install, upgrade,
    update, repair, and reconfiguration, and written atomically. The
    `.gitignore` block covers `bin/claude-local` and
    `bin/claude-local.cmd`. A regeneration that finds a byte-identical
    POSIX launcher or PATH shim without its execute bit restores the
    bit, and inspect and selftest report such a file as stale: a
    non-executable PATH shim would make nested `claude` calls fall
    through to a global binary.

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
- The verification stamp: no stamp, a malformed stamp, and a binary
  replaced after stamping are refused or fall back.
- The `sh` shim with an exported `CDPATH` and with `$`, backtick, and
  space in managed paths; the cmd shim rewritten while a session runs
  keeps its exit code.
- Windows nested calls from a launched session's environment, with a
  stand-in global `claude.exe` later on `PATH`: a Python bare-name list
  call, `shutil.which("claude")` and `shutil.which("claude.exe")`,
  PowerShell 7 and 5.1, cmd.exe, and Git Bash all reach the slot's
  `claude.exe`; arguments carrying a quote break with a benign
  marker-file command, a percent token, a caret, and a JSON document
  arrive verbatim, and no marker appears.
- Both `.cmd` shims invoked by a quoted bare name through `PATH` from a
  directory laid out under a decoy tree run this project's launcher,
  never the decoy's.
- `.bat` interpreter shims on `PATH` with the recorded interpreter gone
  give exit 127 and the no-Python message; the caller's
  `PYTHON_MANAGER_AUTOMATIC_INSTALL` reaches Claude Code unchanged.
- Another session's variables are dropped for a fresh launch and kept
  for a nested launch of the same project; inherited credentials are
  removed by default (kept with passthrough on), named once, and never
  echoed.
- Killing the launcher (base interpreter and venv redirector) ends
  Claude Code.
- Interception behind leading options and `--`, and forwarding of
  `plugin install <name>` and multi-word prompts.
- Canonical spellings for a lower-case drive and an 8.3 root; refusal
  of a root containing the `PATH` separator.
