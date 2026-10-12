# Changelog

All notable changes to this repository will be documented in this
file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this repository aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
where practical.

## [Unreleased]

### Added

- Initial spirit-of-the-law port of codex-wrangler to manage
  project-local `@anthropic-ai/claude-code` installs, with channels
  mapped one-to-one onto the `stable`, `latest`, and `next` npm
  dist-tags. (The native runtime below replaces the npm install; see
  Changed and Removed.)
- Generated `bin/claude-local` launcher that pins `CLAUDE_CONFIG_DIR`
  to `.local/claude` at every security ring, redirects `HOME` to
  `.local/claude/HOME` by default (ring 1), and supports per-invocation
  ring-0 escape via `--shared-home` or `CLAUDE_LOCAL_SHARED_HOME=1`.
  `--isolated-home` forces ring 1 for one invocation, and nested
  launches inherit the ring the session chose.
- Four-level security ring model specification and AGENTS.md guidance
  (CPU-convention numbering; ring 3 sandbox default, ring 0 real HOME
  last resort), intended to be shared with codex-wrangler. It now also
  covers native-Windows home redirection, the operator-confirmed ring-1
  default on native Windows, self-update suppression at every ring, and
  recorded ring requirements for native-Windows tests and ring-0
  housekeeping.
- Managed reasonable-permissions launcher default mapped to
  `--permission-mode acceptEdits`, shipped disabled.
- Managed `.gitignore` block covering `.local/`, `bin/claude-local`,
  `bin/claude-local.cmd`, and `README-LOCAL-Start-Claude.md` while
  never ignoring the trackable project-root `.claude/` directory or
  touching ignore blocks owned by TheKnowledge or codex-wrangler.
- Installer trust boundary that recognizes both `.local/claude/HOME`
  and a coexisting `.codex-home` as repo-local isolated homes, and now
  also codex-wrangler's current `.local/codex-home`; refusals name the
  home variable that pointed there.
- TheKnowledge submodule, starter bootstrap files, project-management
  trees, behavior specifications, and an open ECR requesting
  claude-wrangler awareness in TheKnowledge's managed ignore baseline.
- Foundation for a Node-free native Claude Code runtime:
  standard-library verification of both Anthropic manifest signatures
  against pinned trust anchors (`signing.py`), hardware-accurate
  platform selection and executable structure checks (`platforms.py`),
  HTTPS-only bounded downloads with resume, host allowlisting, and
  corporate CA support (`download.py`, `distribution.py`), an A/B
  runtime slot store with maintenance locking, atomic promotion, and
  in-use detection (`slots.py`, `atomicio.py`), and a standalone
  cross-platform launcher core (`launcher_core.py`). Recorded release
  manifests and signatures serve as offline test fixtures.
- Native Claude Code runtime for every operation: install, upgrade,
  update, repair, rollback, inspect, selftest, and uninstall now work on
  Anthropic's signed native binaries from
  `https://downloads.claude.ai/claude-code-releases` on Windows, macOS,
  and Linux, with no Node.js or npm. Each candidate is downloaded into
  staging, checked for size, SHA-256, and executable structure,
  smoke-tested with `--version` in a scratch environment, and promoted
  with its manifest and both signatures retained for offline
  re-verification (`transaction.py`, `services.py`, `operations.py`).
- A/B runtime slots under `.local/claude/wrangler/`: at most two
  releases per project, an `active-slot` pointer, a maintenance lock
  that makes a second maintainer fail fast, launch-time fallback to the
  other slot with a prominent warning and a `fallback.json` record, and
  refusal (never a global `claude`) when neither slot is usable.
- `--rollback`, which makes the previous release active after an
  in-use check and a full offline re-verification.
- In-use protection: a slot is never displaced, rolled back to, or
  removed while a process runs from it, and the refusal names the
  process IDs. `--stop-background-sessions` allows
  `claude daemon stop --any` with the project's launcher environment
  first.
- Cross-platform launchers: a generated standard-library Python
  launcher core (`<runtime root>/launch.py` with `launch.json`) behind
  `bin/claude-local` (POSIX `sh`, LF) and `bin/claude-local.cmd` (cmd,
  CRLF, ASCII), which work from bash, zsh, Git Bash, WSL, cmd.exe,
  Windows PowerShell 5.1, and PowerShell 7 (`generator.py`). The shims
  use the recorded interpreter, then `py -3`, `python3`, and `python`;
  Git Bash gets Windows-form paths and unconverted arguments.
- Nested-`claude` PATH shims (`<runtime root>/shim/claude` and
  `claude.cmd`) so hooks, scripts, `claude -p`, and background respawns
  run the project's pinned binary with the project's environment.
- Notify-only update checks: `--update` verifies each channel's signed
  manifest and records version, build date, and check time; the
  launcher prints a one-line notice for a newer release on the installed
  channel, refreshing at most once per 24 hours within a 3-second
  budget; `claude-local --check-updates` checks on demand; and
  `--set-update-checks on|off` controls the notices per project.
  `update-check.json` records a failed refresh for `--inspect`.
- A once-per-session native-Windows note that Claude Code runs commands
  unsandboxed there, with `--set-windows-sandbox-warning on|off` as the
  per-project opt-out.
- Automatic migration of npm-era projects (`migration.py`) on install,
  upgrade, or repair: read-only discovery, the same pinned version
  (refusing below 2.1.207 instead of upgrading silently), adopt-in-place
  of an npm-installed binary whose hash matches the signed manifest, a
  commit point at the new metadata, a schema-1 compatibility
  projection with a `migrated_to` pointer, deferred cleanup of locked
  Windows files, a Node-free migration-pending launcher for `--update`
  and toggles, and a byte-identical `CLAUDE_CONFIG_DIR` spelling.
- New options: `--platform`, `--release-base-url` (a mirror whose host
  joins the download allowlist and is recorded for launcher update
  checks), `--ca-bundle`, `--download-timeout-seconds`, `--rollback`,
  `--stop-background-sessions`, `--set-update-checks`, and
  `--set-windows-sandbox-warning`.
- Richer `--inspect` and `--selftest`: both slots and the pointer,
  staging and retired entries, fallback and update-check records,
  legacy and migration status, generated-file freshness, the config-dir
  suppression settings, project settings warnings, processes running
  from the runtime, and the launcher environment for each ring on the
  host operating system. The self-test re-verifies both slots offline,
  runs the active binary's `--version` in scratch, and checks that
  `launch.py` matches the packaged launcher core (`inspection.py`).
- Native Windows entry point `install.cmd` with a policy helper
  (`scripts/install-stage-1-windows.py`), and a project-owned
  `scripts/install_prerequisites.{py,sh,cmd}` family that installs only
  missing prerequisites through winget, brew, apt, or dnf, plus a
  `--dev` path for contributors on Windows.
- `scripts/run_tests_isolated.py`, which runs pytest with every
  home-like variable redirected, and an autouse test fixture that does
  the same per test.
- Tests for the new runtime: offline operation fakes, real shim
  execution through cmd, PowerShell 5.1 and 7, Git Bash, and `sh` with
  a fake Claude Code program, cross-module contract tests for the
  launcher core's duplicated constants, a Python 3.9 syntax guard, an
  `AGENTS.md` guidance tripwire, and an opt-in end-to-end install of a
  real local binary.
- Documentation for the native runtime: a Windows quick start,
  launcher usage per shell, rings per operating system, slots,
  rollback and repair, update notices, the misleading "Updates are
  disabled by your administrator" message, migration and the
  claude-wrangler rollback caveat, TLS and proxies, and known
  limitations.
- Windows and macOS operating-system classifiers in `pyproject.toml`.
- `--allow-tracked-state`, which lets install, upgrade, repair,
  `--update`, `--rollback`, the settings toggles, and `--skip-install`
  proceed when git tracks managed runtime, session-state, or launcher
  files.

### Changed

- On Windows, the user-level `claude-wrangler` launcher is a copied
  `claude-wrangler.exe` (renamed aside when in use) instead of a
  symlink, and installers print a PATH hint instead of failing.
- Repo-relative layout paths are stored in POSIX form on every OS;
  `.gitignore` block edits never produce `\r\r\n`; the user home
  resolves from `USERPROFILE` on Windows; codex-wrangler's current
  `.local/codex-home` is recognized as an isolated home.
- Managed installs now prune npm's orphaned ".<name>-<token>"
  rename-staging directories from the local node_modules tree before
  running `npm install`, so an interrupted or crashed install
  self-heals instead of failing every retry with "ENOTEMPTY: directory
  not empty, rename". Only npm scratch state is removed; installed
  packages and project-local Claude session state are never touched.
  (npm-era behavior, superseded by the native runtime.)
- The generated launcher is now the Python launcher core with thin
  `sh` and `cmd` shims instead of a bash script. On native Windows,
  ring 1 also redirects `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`,
  `APPDATA`, and `LOCALAPPDATA`, because native programs ignore `HOME`.
  On Windows the launcher runs Claude Code as a child process and maps
  a Ctrl+C exit to 130.
- The default runtime root (`--local-dir`) moved from
  `.local/claude/npm` to `.local/claude/wrangler`, with schema-2
  metadata at `.local/claude/wrangler/.claude-wrangler.json`. A
  `--local-dir` that names an npm-era directory is treated as the
  layout to migrate from.
- Channels resolve from Anthropic's `stable` and `latest` channel
  pointers, which only choose which signed manifest to fetch, instead
  of npm dist-tags. `--version` accepts `latest` or an exact
  `MAJOR.MINOR.PATCH` of 2.1.207 or newer.
- `--repair-install` now clears staging, deletes retired slots that are
  no longer in use, re-verifies both slots offline, repairs an invalid
  pointer, re-downloads the pinned version when the active slot is
  missing or corrupt, and regenerates managed files.
- `--update` verifies signed manifests before it records a version and
  never changes the installed runtime.
- The launcher's Node.js parity check is warn-only: a missing `node`
  never refuses a launch, `CLAUDE_LOCAL_PREFLIGHT=strict` refuses only
  on an explicit selector mismatch, and literal selectors match by
  leading version components. `--inspect` and `--selftest` report
  `node`, `npm`, and `npx` as information only.
- Uninstall takes the maintenance lock, proves ownership and that
  nothing runs from the runtime, removes the runtime first, generated
  files next, and session state last, and keeps the `.gitignore` block
  when anything cannot be deleted.
- Every generated or managed file is written atomically through a
  same-directory temporary file, with LF for files POSIX shells and
  Python read and CRLF for `.cmd` files; renames and deletes in the
  runtime root retry transient Windows sharing violations.
- Specifications updated for the native runtime: launcher preflight
  (`runtime_fidelity_and_startup_preflight.md`), the managed layout and
  binary execution (`session_state_isolation_and_config_dir_pinning.md`),
  metadata location and `launch.json`
  (`reasonable_permissions_for_claude_local_launchers.md`), Windows home
  variables and codex-wrangler home layouts
  (`explicit_user_home_targeting_for_installers.md`), the `.cmd`
  launcher and line endings (`managed_gitignore_enforcement.md`),
  explicit-only background-service stops (`ab_runtime_slots.md`), and
  recorded release base URLs and refresh errors
  (`native_runtime_distribution_and_verification.md`).
- README, DEVELOPMENT, and the `AGENTS.md` project section describe the
  native runtime layout, Windows workflows, and the isolated test
  runner.
- Black and Ruff target Python 3.9 through `pyproject.toml`.

### Deprecated

- `--channel next`: accepted as an alias of `latest` with a warning,
  and an npm-era `next` channel becomes `latest` on migration.
- `--npm-timeout-seconds` and `--npm-install-loglevel`: accepted so
  existing scripts keep working, but ignored with a warning; use
  `--download-timeout-seconds`.
- The npm-era `.local/claude/npm/` layout and its schema-1 metadata:
  read only as a migration source and kept as a compatibility
  projection for older claude-wrangler releases' `--uninstall`.
- `docs/specifications/bounded_managed_npm_operations.md`, superseded
  by `native_runtime_distribution_and_verification.md` and kept for
  history.

### Removed

- Managed npm installs: claude-wrangler no longer runs `npm`, `npx`, or
  `node`, and no longer writes `package.json`, `package-lock.json`,
  `node_modules/`, or `.npm-cache/` under `.local/claude/npm/`.
- npm dist-tag resolution and the separate `next` channel.
- The bash launcher, including its requirement that `node` be on
  `PATH`.

### Fixed

- `--selftest` no longer says "the npm-era layout still needs
  migration" on the passing `migration_not_pending` line of a project
  with nothing to migrate.
- On a newly installed Windows, a download that fails with "unable to
  get local issuer certificate" now explains the cause (Python trusts
  only roots Windows has already fetched) and names the one PowerShell
  request that makes Windows fetch the missing root, instead of only
  suggesting a TLS-inspecting proxy. Found in Windows Sandbox, where
  `downloads.claude.ai`'s GlobalSign root was absent until such a
  request.
- The generated `bin/claude-local` launcher now exports
  `DISABLE_UPDATES=1`, `DISABLE_AUTOUPDATER=1`, and
  `DISABLE_INSTALLATION_CHECKS=1`. Claude Code's self-updater targets
  `HOME`-relative paths rather than the running binary, so a ring-0
  session could previously rewrite or delete the operator's global
  Claude Code install. Rerun `--update` to regenerate existing
  launchers.
- A literal Node.js selector such as `20` no longer counts as a
  mismatch against `v20.11.0`, which made strict preflight refuse to
  launch.
- The managed `.gitignore` block now also ignores
  `bin/claude-local.cmd`.
- On Windows, the pyenv-win interpreter path is
  `versions\<name>\python.exe`, a failing launcher publication reports
  a clean `[install-project] FAIL`, and every test runs with the home
  variables redirected so none can write into the real user profile.
- `scripts/git_commit_with_changelog.py` no longer falls back to
  `sys.argv` when given an explicit empty argument list.
- `--dry-run` with `--stop-background-sessions` no longer runs
  `claude daemon stop --any` for `--uninstall` or `--rollback`; it
  reports the stop it would run.
- A partly failed uninstall keeps the ownership metadata (binaries go
  first, metadata last), so a plain `--uninstall` rerun finishes
  instead of demanding `--force`; uninstall also accepts the
  migration-pending README that `--update` or a toggle rendered.
- The Windows in-use check expands an 8.3 short image path, and the
  launcher starts the binary by its long path, so a session started
  through a short path can no longer have its slot displaced.
- A non-ASCII `--local-dir` is refused before anything is downloaded,
  and such a layout can always be uninstalled, inspected, and
  self-tested; a `--local-dir` naming another project's directory is
  refused.
- The stale-state guard now digests the metadata the command actually
  parsed, so a publish that lands while a command reads its state can
  no longer be reverted by that command.
- After an interrupted publish, a plain install or repair activates the
  release the inactive slot already holds instead of downloading it
  again, and the failure message says to rerun the same command.
- npm-era discovery no longer follows a link or junction and requires
  claude-wrangler's own metadata or managed `package.json`, so install
  and uninstall can no longer delete another directory's `package.json`
  or `node_modules`.
- A symbolically linked `<config dir>/settings.json` that lacks the
  suppression keys is refused before any download instead of failing
  after promotion.
- `bin/claude-local.cmd` leaves cmd.exe on its launch line, so an
  upgrade that rewrites it during a session no longer makes cmd run
  fragments of the new file or lose Claude Code's exit code.
- Uninstall never deletes a maintenance lock another maintainer holds,
  and on POSIX a maintainer refuses a lock file replaced while it was
  being taken; a filesystem without file locks is reported as such
  instead of as "another maintenance operation is already running".
- macOS in-use detection uses `libproc`'s `proc_pidpath`, which follows
  directory renames and symbolic links; `ps` is only a fallback.
- The generated `sh` shims ignore an exported `CDPATH` and quote every
  managed path, so `$`, backticks, and quotes in `--local-dir` stay
  literal; `scripts/install_prerequisites.sh` ignores `CDPATH` too.
- A byte-identical PATH shim that lost its execute bit is repaired, and
  inspect and selftest report it.
- `scripts/install_prerequisites.py` no longer counts the macOS Command
  Line Tools stub `/usr/bin/git` as Git, and `--dev` refuses an
  interpreter older than Python 3.12 instead of failing inside pip.
- Python 3.9 on Windows: owned dangling launcher links are recognized
  and removed, and a dangling link pointing out of the project is
  refused by the containment checks (`Path.resolve()` does not follow
  such links there).
- On Windows a nested `claude` inside a session (a Python list call,
  Node or Bun spawn, PowerShell, cmd.exe, or Git Bash) now starts the
  active slot's `claude.exe` directly: the launcher puts the slot's
  directory ahead of the PATH shim. Before, `.exe`-only lookups skipped
  the `.cmd` shim and ran the operator's global `claude.exe` against
  the project's config dir. Such nested calls now skip the launcher's
  per-invocation layers (`--settings`, interception, the permission
  default, and its checks); the redirected homes, the pinned config dir,
  and the update-suppression variables still apply. When the slot's
  directory cannot be used (the npm-era layout, or extra files beside
  `claude.exe`), the launcher warns that nested calls fall back to the
  `.cmd` shim.
- `bin\claude-local.cmd` and the nested `claude.cmd` find their own
  directory through a called label, so a quoted bare name found through
  `PATH` (from `shell=True` strings, Node `exec`, or npm scripts) no
  longer makes them run a launcher or `launch.py` located relative to
  the caller's current directory.
- The cmd launcher probes `py.exe`, `python3.exe`, and `python.exe`
  instead of bare names, so a pyenv-win `.bat` shim can no longer end
  it with exit 0 before anything launched; it exits 127 with the
  no-Python message instead. Its probes, and the `sh` launcher's, run
  with `PYTHON_MANAGER_AUTOMATIC_INSTALL=false` (the caller's value is
  restored for Claude Code), so the Windows Python install manager
  never installs a runtime during a launch.
- `install.cmd` and `scripts\install_prerequisites.cmd` no longer abort
  with "was unexpected at this time" when `PATH` holds a quoted entry
  with parentheses (such as one under `Program Files (x86)`), find a
  Python in a quoted `PATH` entry, and accept an interpreter under a
  directory containing `^` or `%`, through `PATH` or
  `THEKNOWLEDGE_BOOTSTRAP_PYTHON`. They read `PATH` only through
  delayed expansion and measure it first, so a `PATH` too long for cmd
  (roughly 8,000 characters once quoted) is skipped with a message
  instead of aborting the script or crashing cmd, and
  `THEKNOWLEDGE_BOOTSTRAP_PYTHON` and the default locations still work
  there. Entries containing `!` keep their spelling.
- `scripts/install_prerequisites.py --dry-run` no longer reports
  "Prerequisites ready." while prerequisites are missing; it says what
  it would install. After a real install it checks again and asks for a
  new terminal when the new tools are not visible yet.
- `update`, `upgrade`, and `install` are intercepted behind leading
  root options and `--` too (Claude Code dispatches them there), while
  `plugin install <name>` and multi-word prompts still pass, including
  in the npm-era layout, which records no subcommands.
- On Windows, a lower-case drive letter or an 8.3 short spelling of the
  checkout no longer forks Claude Code's path-keyed state: the launcher
  canonicalizes the root before it exports `CLAUDE_CONFIG_DIR` and the
  home variables. A project launched before through a lower-case drive
  spelling (as some editor terminals provide) gets its per-project
  Claude Code entry once more under the canonical spelling, so expect
  one more folder-trust prompt there.
- The Windows install gate (`install-stage-1-windows.py`) now checks
  `HOMEDRIVE`/`HOMEPATH`, the home stage 2 falls back to, refuses an
  empty `USERPROFILE`, and names the effective home in its elevated
  prompt; installers also recognize another checkout's isolated home
  and, inside a launcher session, a home that differs from the
  account's own.
- An uninstall stopped by a filesystem without file locks no longer
  leaves the lock file and runtime-root scaffolding it created.
- The tracked-state refusal ignores case on case-insensitive
  filesystems, so a tracked `.LOCAL/Claude/...` path no longer slips
  past it, and a caller's `GIT_LITERAL_PATHSPECS` cannot empty the
  query.
- A checkout whose runtime root contains the `PATH` separator is
  refused before anything changes, and the launcher refuses to start
  from one, instead of letting nested `claude` calls fall through to a
  global binary. This applies on POSIX too, where the separator is `:`:
  an existing install under such a path no longer launches and must be
  moved.
- The macOS prerequisites specification now matches the code (only
  `/usr/bin/git` is gated on `xcode-select -p`), and
  `install_prerequisites.sh` suggests `xcode-select --install` on macOS
  when no Python 3.9+ exists.

### Security

- The launcher removes inherited credential, identity, and provider
  variables before starting Claude Code, because Claude Code prefers
  them to the project's own login and a key set in a shell would
  otherwise log every project into the same account. Removed: every
  `ANTHROPIC_*` variable, `CLAUDE_CODE_OAUTH_*`,
  `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `AWS_BEARER_TOKEN_BEDROCK`,
  the `CLAUDE_CODE_CLIENT_CERT`/`_KEY`/`_KEY_PASSPHRASE` client
  certificate, the Bedrock, Vertex, Foundry, Mantle, Claude Platform,
  and gateway provider switches, and the account variables a Claude
  host app or remote session hands the CLI, at every ring. The removed
  names (never their values) are listed once per session.
  `--set-credential-passthrough on` keeps them for a project whose
  shell variables belong to it; `--inspect` and the local README show
  the setting. Behavior change: a project that relied on a key in the
  launching shell needs that switch.
- Release manifests are verified before they are parsed: the OpenPGP
  and raw RSA signatures must both verify against trust anchors
  compiled into the package, never read from the project, the network,
  or a GnuPG keyring, and `gpg` is never run. Checks use explicit
  `raise` so `python -O` cannot disable them. There is no unsigned
  mode, and versions older than 2.1.207 are refused.
- Downloads are HTTPS-only (TLS 1.2 or newer), every redirect must stay
  on an allowlisted host, and binaries must match the signed size and
  SHA-256 exactly. Candidates run only in a scratch environment that
  never sees the operator's home or the project's config dir.
- The launcher never runs a binary outside the runtime root or a
  global `claude`, starts its core with `python -I`, never takes `node`
  from the current directory, and the PATH shim keeps nested `claude`
  calls from running a global binary against project state.
- Self-update suppression now has three layers (environment,
  `--settings`, and the config-dir `settings.json`), sets
  `disableDeepLinkRegistration`, intercepts `claude update`, `upgrade`,
  and `install`, refuses a caller `--settings`, and warns when project
  settings switch suppression off.
- A running binary is never displaced, so Claude Code's background
  service cannot lose its executable and fall back to the operator's
  global binary; the runtime root may not overlap the redirected home,
  any `claude/versions` directory, or Claude Code's own config-dir
  children.
- On native Windows, ring 1 no longer leaves `USERPROFILE`, `APPDATA`,
  and `LOCALAPPDATA` pointing at the real profile, and installers check
  every advertised home variable before writing user-scoped state.
- A repository-supplied runtime is never trusted as verified. Install,
  migration, the toggles, `--skip-install`, `--update`, rollback, and
  repair re-verify a slot fully offline (both signatures, the manifest
  hash, every binary's SHA-256, and the slot's exact file set) before
  they keep or reuse it, and a slot that fails is never reused. The
  launcher now runs a binary only while it matches the verification
  stamp written after its last full verification
  (`slots/<x>/.verified.json`: size, modification time, and on POSIX
  inode), so a binary planted by a git clone or replaced by a
  `git pull` is refused. A copied checkout needs one `--repair-install`.
  On Windows the stamp has no inode, so an archive that preserves
  modification times could carry a matching one.
- Install, upgrade, repair, `--update`, `--rollback`, the settings
  toggles, and `--skip-install` refuse when git tracks files under the
  runtime root, the config dir, the redirected home, the npm-era
  layout, or the launchers (`--allow-tracked-state` overrides); inspect
  and selftest report such files.
- A slot that holds anything beyond its record, stamp, evidence, and
  binaries (for example a DLL beside `claude.exe`) fails verification,
  selftest, and repair, and loses its stamp.
- `--update` renders a migration-pending launcher only for an npm-era
  binary that matches the signed manifest; the offline toggles and
  `--skip-install` render one only where a claude-wrangler launcher
  already exists.
- A non-default release host recorded in metadata is named before it is
  contacted and in the install summary.
- On POSIX the config dir and redirected home are kept at mode 0700.
- Nested `claude` calls on Windows no longer pass through cmd.exe,
  which re-parsed every argument: text from a prompt, system prompt, or
  JSON configuration could run a second command outside any permission
  check, expand `%` variables, or be truncated. Programs that call the
  top-level launcher should run `launch.py` with an argument list; the
  README documents cmd's re-parsing of `bin\claude-local.cmd`.
- A project session started from another Claude Code session no longer
  inherits that session's identity, messaging pipe and token, or
  secure-storage key. The IDE port, entry
  point, and effort variables are dropped only when `CLAUDECODE` shows
  that another Claude Code session started the launch, because IDE
  terminals and Agent SDK hosts set them too.
- On Windows the config dir and redirected home get a private ACL
  (you, SYSTEM, Administrators) when created and at every maintenance
  operation, so other local accounts cannot read session tokens or
  transcripts in a checkout outside the profile; install and repair
  warn about a checkout writable by Users, Authenticated Users, or
  Everyone.
- Killing the launcher on Windows now ends Claude Code too (a
  kill-on-close job), instead of leaving it running unattended.
- `--uninstall` on Windows notes that Claude Code may keep a login in
  Windows Credential Manager that uninstall cannot remove, and the docs
  list the stores ring 1 shares on Windows (OpenSSH, known folders, the
  PowerShell module path, managed settings, and environment
  credentials).
