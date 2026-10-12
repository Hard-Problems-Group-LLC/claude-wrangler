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

### Security

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
