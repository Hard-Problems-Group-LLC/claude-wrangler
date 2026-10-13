# claude-wrangler

`claude-wrangler` is a Python utility for setting up per-project,
isolated Claude Code support: it installs a pinned native Claude Code
release, verified against Anthropic's signed release manifests, inside
the project, and keeps all session-specific Claude data there too. It
needs neither Node.js nor npm, runs on Windows, macOS, and Linux, and is
designed to coexist with instances from `codex-wrangler` in the same
project.

It creates and manages a disciplined local layout under the
TheKnowledge-standard `.local/` operator-state directory:

- `.local/claude/wrangler/` as the runtime root: two A/B runtime slots
  holding verified releases, the generated launcher core, the
  update-check cache, and managed metadata
- `.local/claude/HOME/` as the project-local `$HOME` used at security
  rings 1 and 2
- `.local/claude/` as the pinned `CLAUDE_CONFIG_DIR`, so Claude auth,
  sessions, history, and todos stay project-local at every ring
- `bin/claude-local` (POSIX `sh`) and `bin/claude-local.cmd` (cmd and
  PowerShell) as the generated launchers used inside the target project
- `README-LOCAL-Start-Claude.md` as an ignored local operator guide
  inside the target project
- a marked `.gitignore` block so uninstall can remove only what it owns

The utility also supports:

- installation of the current stable Claude Code release by default
- JSON inspection with `--inspect`
- pessimistic verification with `--selftest`, including offline
  re-verification of both runtime slots
- conservative removal with `--uninstall`
- notify-only version-catalog refresh with `--update`
- channel-aware upgrades with `--upgrade --channel stable|latest`
  (`next` is a deprecated alias of `latest`)
- exact-version targeting with `--version latest|x.y.z` (2.1.207 or
  newer)
- instant rollback to the previous release with `--rollback`
- runtime repair with `--repair-install`
- managed reasonable-permissions launcher defaults with
  `--set-reasonable-permissions` and `--clear-reasonable-permissions`
  (mapped to `--permission-mode acceptEdits`, shipped disabled)
- per-project switches for launcher update notices
  (`--set-update-checks on|off`) and the native-Windows sandbox note
  (`--set-windows-sandbox-warning on|off`)
- removal of inherited credential variables such as `ANTHROPIC_API_KEY`
  by default, so one shell key never logs every project into the same
  account (`--set-credential-passthrough on|off`)
- automatic migration of projects installed by npm-based releases

## Why Project-Local Pinning

A bad tool update that lands in a global install can take out
development work on every project at once. `claude-wrangler` pins the
Claude Code CLI per project, the same way a virtualenv pins Python
dependencies: the generated launcher runs the binary in the project's
active runtime slot, never falls back to a global or registry copy, and
emits update *notices* rather than auto-updating. A bad release affects
one project, and the previous release stays installed in the other slot
for `--rollback`.

## Security Ring Model

Agent work in projects managed by this tool follows a four-level
security ring ladder, numbered by the CPU protection-ring convention
(ring 0 most privileged). See
`docs/specifications/security_ring_model.md` for the specification.

- Ring 3 (default): agent-managed sandbox, ephemeral sandbox `$HOME`.
- Ring 2: sandboxed, `$HOME` = `.local/claude/HOME`.
- Ring 1: unsandboxed, `$HOME` = `.local/claude/HOME`. The launcher
  starts Claude Code sessions here.
- Ring 0 (last resort): unsandboxed with the real `$HOME`. Reach it
  per-invocation with `bin/claude-local --shared-home` or
  `CLAUDE_LOCAL_SHARED_HOME=1`. `--isolated-home` forces ring 1 for one
  invocation.

`CLAUDE_CONFIG_DIR` stays pinned to `.local/claude` at every ring, so
escalating `$HOME` fidelity never forks Claude auth or session history.

Which rings exist depends on the operating system:

| Host | Rings available | Default for agent work | What ring 1 redirects |
|---|---|---|---|
| Linux, macOS, WSL2 | 3, 2, 1, 0 | ring 3 | `HOME`, `XDG_*` |
| Native Windows | 1, 0 | ring 1 | `HOME`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `XDG_*` |

Claude Code has no sandbox on native Windows, so rings 3 and 2 do not
exist there, and the operator confirmed ring 1 as the native-Windows
default (recorded in `AGENTS.md`). Native Windows programs read their
home from `USERPROFILE` and the related variables rather than `HOME`, so
the launcher moves all of them together; redirecting `HOME` alone would
effectively be ring 0. Once per session the launcher prints a note that
commands run unsandboxed and that isolation covers Claude's state and
the redirected-home tools, not per-user Windows stores. For personal
work you can turn the note off per project:

```bash
claude-wrangler --set-windows-sandbox-warning off .
```

Client work that needs ring 3 should use WSL2 or a separate Windows
account.

## Changelog

This repository keeps a top-level [CHANGELOG.md](CHANGELOG.md) in Keep
a Changelog style. The repo's short-lived
`project-management/state/pending-commit-changes.txt` queue is still
used for commit-body text, but notable changes are mirrored into the
`Unreleased` changelog section before commit.

For normal local commit-and-push flow, use:

```bash
python scripts/git_commit_with_changelog.py -m "Your subject"
```

That helper updates `CHANGELOG.md` first, then delegates to
`python TheKnowledge/scripts/git_standard_commit_push.py`. On Windows,
follow the interim commit procedure in `AGENTS.md` instead: that helper
always pushes, and its quality gate does not redirect the user profile.

## Quick Start

Use the top-level installer when you want a normal user-local install
from this checkout on Linux or macOS:

```bash
./install.sh
```

That is the default supported path. It ensures Python 3.9+ is available
and installs a stable non-development command into a user-local virtual
environment. Use `sudo ./install.sh --system` when you explicitly want
a system-level install; `sudo ./install.sh` without `--system` is
rejected on purpose.

If Python or Git is missing, install only what is missing first:

```bash
scripts/install_prerequisites.sh --dry-run   # show the commands
scripts/install_prerequisites.sh             # ask once, then install
```

It uses `apt-get`, `dnf`/`yum`, or `brew`. It runs `sudo` only when you
pass `--system-packages`; otherwise it prints the commands that need
root for you to run.

## Windows Quick Start

From cmd or PowerShell in the checkout:

```bat
scripts\install_prerequisites.cmd
.\install.cmd
```

- `scripts\install_prerequisites.cmd` installs only what is missing:
  Python 3.9+, Git, and Git for Windows' bash (which Claude Code's Bash
  tool uses; `--no-git-bash` skips it). It uses
  `winget install --scope user` where the package supports it, asks once
  before installing (`--yes` skips the question, `--dry-run` only prints
  the commands and what it would install), and never elevates; a
  package that needs elevation is printed for you to run. When no
  Python exists yet, it offers to install Python through winget and
  then continues, or asks you to open a new terminal and rerun. After
  installing it checks again and says "ready" only when the new tools
  are visible; otherwise it asks you to open a new terminal.
- `install.cmd` is the native-Windows peer of `install.sh`. It finds
  Python 3.9+ (`THEKNOWLEDGE_BOOTSTRAP_PYTHON`, `py -3`, each
  `python.exe` on `PATH`, then the default python.org, Python install
  manager, and pyenv-win locations), applies Windows policy, and runs
  the same stage-2 installer. The result is
  `%USERPROFILE%\.local\bin\claude-wrangler.exe` backed by a virtual
  environment in `%USERPROFILE%\.local\share\claude-wrangler\venv`.
  `.\install.cmd --help` (or `/?`) shows the options. cmd cannot split a
  `PATH` of roughly 8,000 characters or more, so such a `PATH` is not
  scanned (the error says so); `py -3` and the default locations still
  work, or set `THEKNOWLEDGE_BOOTSTRAP_PYTHON` to a `python.exe`.
- PATH: if `%USERPROFILE%\.local\bin` is not on `PATH`, the installer
  prints a PowerShell command that adds it for your user. It never edits
  `PATH` or runs `setx` itself. Run the printed command, then open a new
  terminal.
- Where `.cmd` files are blocked by policy, run the policy helper
  directly: `py -3 scripts\install-stage-1-windows.py`.
- Native Windows supports the standard user-local mode only.
  `--mode dev`, `--mode venv-only`, and `--system` are refused with a
  pointer to `scripts\install_prerequisites.cmd --dev` or WSL2.
- Inside a `claude-local` session (where `USERPROFILE` points into a
  project), the installer refuses so it does not install into the
  isolated home by accident. Run it from a normal terminal, or pass
  `--allow-isolated-home` when that is what you want.
- Run it from a normal, non-elevated terminal. On an elevated token it
  asks for confirmation on a console and refuses without one (Git Bash
  and mintty give it a pipe, not a console). Set
  `THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED=1` only for unattended CI. On
  hosts with UAC disabled, administrators see that question on every
  run.
- Pressing Ctrl+C during `install.cmd` gets cmd's own
  `Terminate batch job (Y/N)?` prompt.
- `./install.sh` under Git Bash still reaches stage 2, but without these
  Windows checks; prefer `install.cmd` on Windows.

## Developer Quick Start

Use this explicit development mode when you are setting up this
repository for active editing on Linux, macOS, or WSL2:

```bash
./install.sh --mode dev
```

It ensures Python 3.9+ is available, initializes `TheKnowledge`,
installs a pyenv-managed Python toolchain, builds the repo-local
`.venv`, installs the package in editable mode, and makes `direnv`
mandatory for automatic activation when you enter the repository. After
the script finishes, open a new shell in this repository so the managed
`direnv` hook can activate `.venv` automatically.

On native Windows, use the project's own prerequisite installer
instead; it installs anything missing, then creates `.venv` with the
development extras:

```bat
scripts\install_prerequisites.cmd --dev
```

The same `--dev` path works on Linux and macOS through
`scripts/install_prerequisites.sh --dev` when you do not want pyenv and
direnv.

Run `./install.sh --help` to see the current stage-1 help plus the live
stage-2 options. `./bootstrap.sh` and `./bootstrap-stage2.py` remain
compatibility wrappers.

## Typical Usage

Initialize the current repository with the current stable release:

```bash
claude-wrangler .
```

Inspect the managed state as JSON:

```bash
claude-wrangler --inspect .
```

Self-test the managed state:

```bash
claude-wrangler --selftest .
```

Refresh the locally known stable and latest versions without changing
the installed runtime:

```bash
claude-wrangler --update .
```

Upgrade to the current version on one channel:

```bash
claude-wrangler --upgrade --channel stable .
claude-wrangler --upgrade --channel latest .
```

Upgrade to one exact version:

```bash
claude-wrangler --upgrade --channel stable --version 2.1.287 .
```

Switch back to the previous release, or repair the runtime:

```bash
claude-wrangler --rollback .
claude-wrangler --repair-install .
```

Enable or clear the managed reasonable-permissions launcher default:

```bash
claude-wrangler --set-reasonable-permissions .
claude-wrangler --clear-reasonable-permissions .
```

When enabled, the generated launcher adds `--permission-mode
acceptEdits` unless the caller already supplied an explicit permission
choice. That keeps ordinary workspace edits flowing while Bash commands
and ACP operations such as commit and push still prompt for approval.
The wrapper never injects `--dangerously-skip-permissions`.

Uninstall the managed setup:

```bash
claude-wrangler --uninstall .
```

`claude-wrangler` resolves the stable and latest channels from
Anthropic's channel pointers at install time, fetches and verifies the
signed manifest for the chosen version, and writes that exact version
plus the known channel catalog into managed metadata. `--update`
refreshes that local catalog without changing the install, and
`--upgrade --channel ...` installs the channel's current version unless
you supply an explicit exact `--version`. Add `--dry-run` to see what
an operation would do without changing anything or contacting the
release server.

## Starting Claude Code

Every launcher form runs the same Python launcher core,
`.local/claude/wrangler/launch.py`, so ring selection, the pinned
config dir, self-update suppression, and slot selection behave the same
everywhere.

bash, zsh, macOS, Linux, and WSL:

```bash
./bin/claude-local
./bin/claude-local --continue
./bin/claude-local --shared-home     # one ring-0 session
```

cmd.exe:

```bat
bin\claude-local
bin\claude-local --resume
```

Windows PowerShell 5.1 and PowerShell 7 resolve `.\bin\claude-local` to
the `.cmd` launcher, so no `.ps1` exists and execution policy never
applies:

```powershell
.\bin\claude-local
.\bin\claude-local --continue
```

Git Bash runs the `sh` launcher with a native Windows Python and
Windows-form paths, and turns off MSYS argument conversion so prompts
such as `/review` reach Claude Code unchanged:

```bash
./bin/claude-local
```

Notes:

- The launchers need a Python 3.9+ interpreter. They use the one that
  ran `claude-wrangler` (recorded at install time), then `py -3`,
  `python3`, and `python`. Under Git Bash an MSYS or Cygwin Python is
  not enough; a native Windows Python is required.
- `--shared-home`, `--isolated-home`, and `--check-updates` are
  launcher flags and are never passed to Claude Code.
- `claude update`, `claude upgrade`, and `claude install` are
  intercepted: the launcher explains that `claude-wrangler` manages the
  runtime and exits 2. Claude Code also dispatches these behind leading
  options (`--verbose update`) or `--`, so any argument that is exactly
  one of the three words is refused, up to the first other subcommand
  (`claude plugin install <name>` still works). A one-word prompt such
  as `-p install` is refused too; Claude Code would run its installer
  for it.
- A caller `--settings` flag is refused, because the launcher passes its
  own; put persistent settings in `.local/claude/settings.json`.
- The launcher puts `.local/claude/wrangler/shim/` first on `PATH`, so
  hooks, scripts, `claude -p`, and background respawns that call
  `claude` run this project's pinned binary with this project's
  environment instead of a global install. On native Windows the
  active slot's own directory goes first, so a nested `claude` starts
  the project's `claude.exe` directly, whether Python, Node, PowerShell,
  cmd.exe, or Git Bash looks it up, and no cmd.exe re-parses its
  arguments (Windows PowerShell 5.1 and Git Bash still apply their own
  quoting rules, as for any native program). Such nested calls keep the
  redirected homes, the pinned config dir, and the update-suppression
  variables, but skip the launcher's own `--settings` layer,
  interception, permission default, and checks; a project
  `.claude/settings.json` whose `env` block clears `DISABLE_UPDATES`
  (the launcher warns about one at startup) therefore re-enables Claude
  Code's updater for them, into the redirected home at ring 1. When the
  slot's directory cannot be used (the npm-era layout, or extra files
  beside `claude.exe`), the launcher says so and nested calls fall back
  to `shim\claude.cmd`; that fallback, and any caller that names
  `claude.cmd` itself, get cmd.exe's re-parsing described below.
- cmd.exe re-parses the arguments of every `.cmd` file (double quotes,
  `%`, `^`, `&`, and line breaks), so a prompt typed after
  `bin\claude-local` can change on its way in. Programs that start
  Claude Code with arbitrary arguments should run
  `python -I .local\claude\wrangler\launch.py <args...>` (any Python
  3.9+) with an argument list instead.
- A session started from inside another Claude Code session (your
  global one, or another project's) does not inherit that session's
  identity, messaging pipe, or token.
- Credentials in the environment are removed by default. Claude Code
  prefers them to this project's own login, so a key set in your shell
  would make every project share one account. The launcher removes
  every `ANTHROPIC_*` variable, `CLAUDE_CODE_OAUTH_*`,
  `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `AWS_BEARER_TOKEN_BEDROCK`, the
  `CLAUDE_CODE_CLIENT_CERT`/`_KEY`/`_KEY_PASSPHRASE` client
  certificate, the provider switches (`CLAUDE_CODE_USE_BEDROCK`,
  `_VERTEX`, `_FOUNDRY`, `_MANTLE`, `_ANTHROPIC_AWS`,
  `_ANTHROPIC_GOOGLE_CLOUD`, `_GATEWAY`), and the account variables a
  Claude host app or remote session hands the CLI
  (`CLAUDE_CODE_HOST_AUTH_ENV_VAR`, `CLAUDE_CODE_SESSION_ACCESS_TOKEN`,
  and the like), at every ring, and names the removed ones (never their
  values) once per session.
  When the variables in your shell do belong to the project (set by
  direnv, say), run `claude-wrangler --set-credential-passthrough on .`;
  the launcher then passes them through and says so each session.
  Credentials in the project's `.claude/settings.json` `env` block are
  unaffected, because Claude Code applies those itself.
- On Windows the launcher waits for Claude Code and returns its exit
  code. Ctrl+C is left to Claude Code, and a session that Ctrl+C ends
  reports exit code 130, as on POSIX. Killing the launcher process
  (for example a tool timeout) ends Claude Code as well; killing the
  cmd.exe that runs `bin\claude-local.cmd` does not, so kill the
  process tree or run `launch.py` directly from programs.

## Runtime Slots, Rollback, and Repair

Each project keeps at most two releases in `.local/claude/wrangler/`:
the active slot and the rollback slot, chosen by the one-line
`active-slot` pointer. An install or upgrade downloads into
`.staging/`, verifies and smoke-tests the candidate there, and only
then moves it into the inactive slot and flips the pointer, so a failed
or interrupted operation leaves the working runtime untouched. Every
maintenance operation holds `.local/claude/wrangler/.lock`; a second
maintainer fails fast and changes nothing.

- **Launch-time fallback.** On every launch the launcher checks the
  active slot cheaply (completion record, binary present, size, and the
  verification stamp below). If it fails and the other slot passes, the
  launcher prints a prominent warning naming both versions, runs the
  other slot, and records the event in `fallback.json` for `--inspect`
  and `--repair-install`. If neither slot passes it refuses and tells
  you to run `claude-wrangler --repair-install`. It never runs a global
  `claude`.
- **Verification stamp.** Every operation that keeps, reuses, or
  activates a slot (install, upgrade, repair, rollback, `--update`, the
  settings toggles, and `--skip-install`) first re-verifies it fully
  offline: both signatures, the manifest hash, each binary's SHA-256,
  and that the slot holds nothing but its expected files. It then
  records each binary's size, modification time, and (on POSIX) inode
  in `slots/<a|b>/.verified.json`. The launcher runs a binary only while
  it still matches that stamp, so a binary that a git clone or
  `git pull` planted or replaced after verification is refused. On
  Windows the stamp has no inode, so an archive or copy tool that keeps
  modification times could restore a same-size binary together with a
  matching stamp; treat runtime state from archives as untrusted and run
  `claude-wrangler --repair-install .`, which re-verifies every byte. A
  copied or restored checkout usually gets new file identities: run
  `--repair-install` once to re-verify and re-stamp it offline, without
  a download.
- **`--rollback`** makes the other slot active after checking that
  nothing runs from it and re-verifying it fully offline (signatures and
  SHA-256 from its retained evidence).
- **`--repair-install`** clears `.staging/`, deletes retired slots that
  are no longer in use, re-verifies both slots offline (stamping the
  ones that pass and un-stamping the ones that fail), repairs an invalid
  pointer, re-downloads the pinned version when the active slot is
  missing, corrupt, or holds unexpected files, and regenerates the
  launchers. A release the inactive slot already holds is activated
  instead of being downloaded again. It never removes Claude session
  state.
- **Tracked files are refused.** Install, upgrade, repair, `--update`,
  `--rollback`, the settings toggles, and `--skip-install` refuse when
  git tracks anything under the runtime root, the config dir, the
  redirected home, the npm-era layout, or the launchers, because a
  clone or pull could then replace them with content `claude-wrangler`
  never verified. On Windows and macOS the check ignores case, so a
  tracked `.LOCAL/Claude/...` counts. Remove such files from the
  repository; `--allow-tracked-state` overrides the refusal for an
  operator who tracks them on purpose. `--inspect` and `--selftest`
  report them.
- **In-use protection.** A slot is never replaced, rolled back to, or
  removed while a process runs from it; the operation stops and names
  the process IDs. Claude Code's background service counts. Pass
  `--stop-background-sessions` to let `claude-wrangler` run
  `claude daemon stop --any` with the project's launcher environment
  first. That also ends the project's background sessions, so it is
  never done without the flag.

## Updates and Notices

Claude Code's own updater is disabled in three layers: environment
variables (`DISABLE_UPDATES`, `DISABLE_AUTOUPDATER`,
`DISABLE_INSTALLATION_CHECKS`), a `--settings` argument that outranks a
cloned repository's `.claude/settings.json`, and the same keys merged
into `.local/claude/settings.json` for processes that bypass the
launcher. The launcher warns when the project's
`.claude/settings*.json` switches any of them off. Updates are
`claude-wrangler`'s job, and it only ever notifies:

- `claude-wrangler --update .` fetches both channel pointers, verifies
  the signed manifest of each version they name, and records the
  version, build date, and check time. It never changes the installed
  binary.
- The launcher prints a one-line notice when the recorded catalog names
  a newer release on the installed channel, for example
  `Run claude-wrangler --upgrade --channel stable to install it.` It
  may refresh the catalog itself at most once per 24 hours within a
  3-second budget, and a network problem never blocks or fails a
  launch. A version that only the unsigned channel pointer names is
  reported as unverified until `--update` or `--upgrade` checks its
  signed manifest.
- `./bin/claude-local --check-updates` checks now and prints the result,
  even when automatic checks are off.
- `claude-wrangler --set-update-checks off .` turns the automatic
  notices off for one project.

Nothing installs automatically; run
`claude-wrangler --upgrade --channel <channel> .` when you are ready.

### "Updates are disabled by your administrator"

Claude Code prints "Updates are disabled by your administrator. Contact
your IT team" when its own update command runs with `DISABLE_UPDATES`
set. In a project managed here there is no administrator or IT policy
involved: `claude-wrangler` set that variable on purpose so the session
cannot replace the project's pinned binary or, at ring 0, your global
install. The launcher intercepts `claude update` before Claude Code sees
it, so you normally meet this text only when a binary is run outside the
launcher. The same applies when Claude Code says the installed version
is too old and asks you to run `claude update`: run
`claude-wrangler --upgrade --channel <channel> .` from the project root
instead.

## Migrating From npm-Based Releases

Earlier releases of `claude-wrangler` installed the
`@anthropic-ai/claude-code` npm package under `.local/claude/npm/` and
launched it through Node.js. Those projects move to the native runtime
automatically the next time you run an install, `--upgrade`, or
`--repair-install`. `--inspect` and `--selftest` report
"migration pending" instead, and `--uninstall`, `--dry-run`, and
`--skip-install` never migrate.

- Migration keeps the exact version the npm install pinned. If that
  version is older than 2.1.207, the oldest release with both signature
  forms, it stops and asks you to run
  `claude-wrangler --upgrade --channel <channel> .`; it never upgrades
  silently. A recorded `next` channel becomes `latest` with a
  deprecation notice.
- When the npm tree already holds the native platform binary, migration
  hashes it against the signed manifest and adopts it on a match instead
  of downloading it again.
- Auth, sessions, memories, the home mode, the permission default, and
  the exact `CLAUDE_CONFIG_DIR` spelling carry over. Session state is
  never part of the migration.
- The commit point is the new metadata in `.local/claude/wrangler/`.
  Until then the old launcher and npm tree keep working. Afterwards the
  new launchers are written, a schema-1 compatibility copy of the
  metadata stays at `.local/claude/npm/.claude-wrangler.json`, and the
  npm tree is removed. On Windows, files still locked by a running
  process are left in place, reported, and removed by the next install,
  upgrade, or repair.
- `--update` and the permission and notice toggles still work before
  migration; they write a Node-free launcher that runs the npm-era
  binary directly and reports "migration pending". `--update` does so
  only after the binary hashes to the signed manifest for the version
  the npm install pins. The offline toggles and `--skip-install` cannot
  check a signature, so they rewrite the launcher only in a checkout
  that already has a `claude-wrangler` launcher; a fresh clone that
  carries an npm tree must run `claude-wrangler .`.
- An npm-era directory counts only when it carries `claude-wrangler`'s
  own metadata or managed `package.json` and is reached without a
  symbolic link or junction, so another project's `node_modules` is
  never cleaned up or uninstalled.
- The retired `--npm-timeout-seconds` and `--npm-install-loglevel`
  flags are still accepted, but only print a warning. Use
  `--download-timeout-seconds` instead.

**Rolling `claude-wrangler` itself back.** After a project has moved to
the native runtime, use an older, npm-based `claude-wrangler` release
only for `--uninstall` (it may need `--force`). Its other operations,
such as `--update` or a permission toggle, would rewrite the launcher
into the old Node-based form that points at the removed npm tree. If
that happens, run `claude-wrangler --repair-install .` with the current
release. The native runtime slots are still in place, so it regenerates
the native launchers, or migrates again if the older release
reinstalled the npm tree.

## Network, TLS, and Proxies

Releases come from `https://downloads.claude.ai/claude-code-releases`.
Every request and every redirect must be HTTPS on an allowlisted host.
Binaries stream into staging while being hashed, stop as soon as they
exceed the signed size, resume interrupted transfers, and must match
the signed size and SHA-256 exactly.

- Proxies: `HTTPS_PROXY` and `NO_PROXY` are honored. On Windows, when
  no proxy variable is set, the system proxy settings apply.
- Corporate TLS inspection: `--ca-bundle <file.pem>` replaces the
  system trust store for one operation; otherwise `SSL_CERT_FILE` is
  honored. A verification failure names the certificate issuer the
  server presented. On macOS with a python.org Python, run
  `Install Certificates.command` from the `/Applications/Python 3.x/`
  folder first.
- Newly installed Windows: Python trusts only the root certificates
  Windows has already downloaded, and a fresh Windows fetches most
  roots on first use. Until some Windows program has reached
  `downloads.claude.ai`, Python's verification fails with "unable to
  get local issuer certificate". On that failure, with the Windows
  store in use (no `--ca-bundle` or `SSL_CERT_FILE`), claude-wrangler
  says so, makes one request through Windows' own TLS (a Windows
  PowerShell `Invoke-WebRequest -Method Head` to the same URL, from the
  system directory, never from `PATH`), reloads the store, and retries
  once per host. If PowerShell is blocked or the root still does not
  arrive, the error says which, and names the manual request:
  `powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing https://downloads.claude.ai/claude-code-releases/stable | Out-Null"`.
- Mirrors: `--release-base-url <https URL>` points at a mirror of the
  release distribution. Its host joins the allowlist, and the launcher's
  update checks use it too. Signatures are still checked against the
  pinned keys, so a mirror cannot alter a release, although it could
  still offer an older signed one.
- Timeouts: `--download-timeout-seconds` (default 60) bounds each
  connect and read; a small number of retries with backoff follows.
- npm settings such as `.npmrc` proxies, registries, and `cafile` no
  longer apply.

The launcher's own update check uses the system trust store or
`SSL_CERT_FILE`; it does not know a `--ca-bundle` given at install time.
Behind TLS inspection, set `SSL_CERT_FILE` for launches too, or turn
the checks off with `--set-update-checks off`.

## Verification and Trust

- Both manifest signatures, the OpenPGP signature and the raw RSA
  signature, must verify against keys compiled into the installed
  `claude-wrangler` package. Keys are never read from the project, the
  network, or a GnuPG keyring, and `gpg` is never run. The manifest is
  verified before it is parsed.
- The oldest installable version is 2.1.207, and there is no unsigned
  mode.
- Platform selection reflects the hardware, not the Python process
  (Rosetta 2, Windows on ARM x64 emulation, and musl Linux are
  detected); `--platform` overrides it within the host's own operating
  system.
- Before promotion every binary passes a size and SHA-256 check, an
  executable-structure check (PE, Mach-O, or ELF for the expected
  machine), and a `--version` smoke test in a scratch environment that
  never sees your home or the project's config dir.
- Each slot keeps its manifest and both signatures, so `--selftest`,
  `--rollback`, and `--repair-install` re-verify it offline. A slot
  that holds anything beyond its record, stamp, evidence, and binaries
  (for example a DLL beside `claude.exe`, which Windows would load
  first) fails that verification.
- Nothing a repository can supply is trusted as verified: a slot is
  reused only after a full offline verification, and the launcher runs
  only binaries stamped right after one (see "Runtime Slots, Rollback,
  and Repair").
- A release host other than Anthropic's that was recorded in the
  metadata is named before it is contacted and in the install summary.

## Runtime Boundary

`claude-wrangler` localizes the Claude Code runtime under
`.local/claude/wrangler/` and Claude state under `.local/claude/`.
Claude Code needs neither Node.js nor npm, and no `claude-wrangler`
operation or launcher requires them. The launchers need only a Python
3.9+ interpreter, and the launcher core uses the standard library
alone, so a project keeps launching even if `claude-wrangler` itself is
later uninstalled.

The launcher core performs a cheap startup preflight before it starts
Claude Code. Besides the slot check, it compares a checked-in Node.js
selector (`.nvmrc`, `.node-version`, or `.tool-versions`) with the
`node` on `PATH`, because project tooling started from Claude Code may
still depend on it. A literal selector matches by leading version
components, so `20` matches `v20.11.0`. A missing `node` only warns.
`CLAUDE_LOCAL_PREFLIGHT` controls the check:

- `warn` (default): report and continue;
- `strict`: refuse to launch when a literal selector does not match the
  `node` on `PATH`, or when that `node` cannot report its version;
- `off`: skip the Node.js check.

`claude-wrangler --inspect` and `--selftest` report `node`, `npm`, and
`npx` paths and versions as information only, detected checked-in
Node.js selectors, any root `package.json` package-manager declaration,
and the effective `CLAUDE_CONFIG_DIR`, `HOME`, `XDG_*` (and, on native
Windows, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`, and
`LOCALAPPDATA`) values the launcher exposes at each ring. They also
report both slots, the pointer, any fallback or failed update check,
generated-file freshness, and whether the self-update suppression
settings are in place. `--selftest` fails when an explicit Node.js
selector disagrees with the resolved `node`.

Submodules such as `TheKnowledge` should use the parent project's local
interpreters and package environments as if they were part of the same
project tree.

## Trust and Install Scope

`claude-wrangler` treats user-scoped install targets as an explicit
trust boundary.

- Less-trusted AI or isolated `claude-local` sessions should normally
  stay in repo scope and should not mutate an operator's shell init,
  `~/.local/bin`, or `~/.pyenv` implicitly.
- More-trusted AI can still manage its own user scope, but that should
  be its own explicit home directory, not an accidental by-product of
  whatever home the current session inherited.
- Operator-user installs remain supported, but when the current home is
  a repo-local isolated home (this tool's `.local/claude/HOME`, or a
  coexisting codex-wrangler `.local/codex-home` or legacy
  `.codex-home`), they require `--user-home /path/to/home` instead of
  silently targeting the wrong scope, or `--allow-isolated-home` when
  an AI-local install is deliberate. On native Windows the home is read
  from `USERPROFILE` (then `HOMEDRIVE`/`HOMEPATH`, then `HOME`), and
  each of those variables is checked. Another checkout's isolated home
  counts too, and inside a `claude-local` or `codex-local` session any
  home that differs from your account's own home counts, so a custom
  launcher layout is caught as well.
- System installs remain separate and still require
  `sudo ./install.sh --system` (not available on native Windows).

## Safety Notes

The tool is intentionally suspicious.

- Managed paths must stay under the target project root, and the
  runtime root must not overlap the redirected home, any
  `claude/versions` directory, or Claude Code's own config-dir
  children.
- Uninstall removes files only when it can prove ownership, unless
  `--force` is used. It refuses while anything runs from the runtime,
  removes the binaries first and the ownership metadata last, removes
  the runtime before session state, and stops before session state if a
  binary cannot be deleted; a plain rerun then finishes the job. A
  `--dry-run` never stops background sessions, even with
  `--stop-background-sessions`.
- `--local-dir` must name a new, empty, or `claude-wrangler`-owned
  directory, and every managed path must be ASCII-safe for the cmd
  launcher; both are checked before anything is downloaded.
- The config dir and the redirected home are kept private to you: mode
  0700 on POSIX, and on Windows an ACL that grants only you, SYSTEM,
  and Administrators (Claude Code keeps its Windows login in
  `.local\claude\.credentials.json`). On Windows, install and repair
  also warn when the checkout itself is writable by Users,
  Authenticated Users, or Everyone, as folders created under a drive
  root such as `C:\src` are by default; keep client work under your own
  profile.
- The generated launchers and the generated local README are
  content-checked; the launcher core must be a byte-for-byte copy of
  the packaged one.
- Every generated file is written atomically, with LF line endings for
  files POSIX shells read and CRLF for `.cmd` files.
- Inspection emits a JSON report so automation or operators can see
  what the tool believes it owns, and never takes the maintenance lock.

## Known Limitations

- **No sandbox on native Windows.** Claude Code runs the commands it
  executes unsandboxed there, so rings 3 and 2 are unavailable. Use
  WSL2 or a separate Windows account for work that needs them.
- **Per-user Windows stores.** Ring 1 redirects the home variables,
  but Windows Credential Manager (including Git Credential Manager),
  the registry, and `HKCU\SOFTWARE\Policies` stay per user and are
  shared by every project. `HOMESHARE`, `TEMP`, and `TMP` are not
  redirected. As on Linux at ring 1, these are shared too: Windows
  OpenSSH keys, config, and the `ssh-agent` service (OpenSSH finds
  `~\.ssh` from the account profile, not `USERPROFILE`); programs that
  ask Windows for known folders, such as .NET tools, which get the real
  profile; the PowerShell module path; and machine-wide managed
  settings under `C:\Program Files\ClaudeCode`.
- **Credentials in the environment.** The launcher removes inherited
  credential and provider variables (see Starting Claude Code) unless
  the project turned `--set-credential-passthrough` on. It cannot
  reach other credential sources: AWS and Google Cloud SDK credentials
  (`AWS_PROFILE`, `GOOGLE_APPLICATION_CREDENTIALS`, and the like) stay
  visible to the commands Claude Code runs, and an `apiKeyHelper` in
  a settings file runs as configured.
- **Windows Credential Manager outlives uninstall.** Claude Code may
  keep a login there under a name derived from the config-dir path.
  `--uninstall` cannot remove it and says so; run `/logout` in a project
  session first, or delete the entry in Credential Manager, so a later
  checkout at the same path does not find it.
- **Shared checkouts on Windows.** A checkout writable by other local
  accounts (any folder created under a drive root inherits that by
  default) is open to them whatever `claude-wrangler` does; the private
  session directories protect only the session state's
  confidentiality. Keep work for different clients in checkouts only
  you can write, or in separate Windows accounts with their own
  checkouts.
- **Ring-0 housekeeping.** Claude Code's daily cleanup prunes old
  versions in `~/.local/share/claude/versions` whatever the update
  settings say. A ring-0 session can therefore prune old versions of
  your global install. Prefer ring 1; the project runtime itself never
  lives under any `claude/versions` directory.
- **WSL needs its own install.** WSL is a separate Linux host:
  `claude-wrangler` refuses to install another operating system's build,
  so install and run it inside WSL to add the Linux binary. A slot can
  carry the same release for several platforms, so a checkout shared
  between Windows and WSL keeps both builds. Each system stamps only
  its own binaries, so after an upgrade on one side, run
  `claude-wrangler .` once on the other before launching there.
- **Pinned trust anchors.** If Anthropic rotates its release signing
  key, a new `claude-wrangler` release is required, and a manifest
  signed by several keys during a rotation is refused.
- **Long Windows paths.** A deeply nested checkout can push the binary
  path past 260 characters, which some programs cannot start from;
  `claude-wrangler` warns before the smoke test.
- **Disk and synced folders.** Each kept release is about 250 MB, and
  two are kept per project. Folders synced by OneDrive or Dropbox
  upload each one.
- **Python 3.13 and newer** verify certificates more strictly by
  default and can reject some TLS-inspection CA certificates that a
  browser accepts; fix the CA certificate or run `claude-wrangler` with
  Python 3.12 or older.
- **Not yet validated everywhere.** The native runtime was developed
  and tested on Windows. Its Linux and macOS code paths, Windows on ARM
  detection, and the Python 3.9 floor still need runs on those systems
  (planned CI); until then they are covered by tests with injected host
  facts and by syntax checks.

## TheKnowledge Submodule

This repository carries `TheKnowledge/` as a git submodule in the
standard project-root location. After a fresh clone, initialize it
with:

```bash
git submodule update --init --recursive
```

The developer bootstrap script runs this automatically.

## Generated Target-Project Artifacts

When you run `claude-wrangler` against another repository, it manages
these artifacts inside that target repository:

- `.local/claude/` (runtime root, ring-1/2 HOME, pinned config dir)
- `bin/claude-local` and `bin/claude-local.cmd`
- `README-LOCAL-Start-Claude.md`
- a marked `.gitignore` block

The managed `.gitignore` block deliberately does not ignore the
project-root `.claude/` directory, which Claude Code uses for
trackable project settings. The block also never touches ignore blocks
owned by other tools, including TheKnowledge's managed baseline and
codex-wrangler's block, even where entries overlap. Edits to
`.gitignore` keep its existing line endings and never produce
`\r\r\n`.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for project layout, test commands,
and packaging notes.
