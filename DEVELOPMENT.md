# Development

## Project Layout

Command line, configuration, and reporting:

- `src/claude_wrangler/cli.py`
  Thin entry point and top-level dispatch.
- `src/claude_wrangler/config.py`
  Argument parsing and runtime configuration selection, including the
  deprecated `next` channel alias, the version floor, network flags, and
  the retired npm flags (accepted with a warning).
- `src/claude_wrangler/layout.py`
  Path validation and native (schema 2) metadata discovery. Repo-relative
  paths are stored in POSIX form on every operating system.
- `src/claude_wrangler/environment.py`
  Runtime-resolution diagnostics, Node.js selector detection
  (information only), and the launcher environment for each ring,
  computed with the launcher core's own functions.
- `src/claude_wrangler/inspection.py`
  `--inspect` and `--selftest`, including offline re-verification of
  both runtime slots. Never reads the maintenance lock.
- `src/claude_wrangler/rendering.py`
  Deterministic content: schema-2 metadata, the `.gitignore` block,
  `launch.json`, the `sh` and `cmd` launcher shims, the nested-`claude`
  PATH shims, the local README, and operator summaries.
- `src/claude_wrangler/changelog.py`
  Keep a Changelog style parsing and queue-to-changelog sync helpers.

Native runtime (Python 3.9 and standard library only where noted):

- `src/claude_wrangler/signing.py`
  Pinned-key verification of both manifest signatures (OpenPGP v4 and
  raw RSASSA-PKCS1-v1_5-SHA512). Python 3.9, standard library only.
- `src/claude_wrangler/distribution.py`
  Channels, the version floor, and signed manifest bundles from the
  release distribution. Python 3.9, standard library only.
- `src/claude_wrangler/download.py`
  HTTPS-only, allowlisted, size- and hash-bounded downloads with resume,
  proxy, and CA-bundle support. Python 3.9, standard library only.
- `src/claude_wrangler/platforms.py`
  Hardware-accurate platform keys (Rosetta 2, Windows on ARM, musl) and
  PE, Mach-O, and ELF structure checks.
- `src/claude_wrangler/slots.py`
  The A/B slot store: layout, the `active-slot` pointer, completion
  records, verification stamps and the exact-slot-contents check, the
  maintenance lock, promotion, cleanup, and in-use detection through the
  process table (Toolhelp on Windows, `/proc` on Linux, `libproc` on
  macOS).
- `src/claude_wrangler/atomicio.py`
  Atomic same-directory writes with explicit newlines and retries for
  transient Windows sharing violations.
- `src/claude_wrangler/transaction.py`
  One install transaction: platform selection, staging (download or
  adopt), structure check, smoke test, evidence, and promotion behind
  the in-use check; offline slot re-verification and `verify_and_stamp`,
  which every operation that keeps or reuses a slot calls.
- `src/claude_wrangler/services.py`
  Injectable side effects (network, smoke test, `--help` reader,
  process table, the `git ls-files` query for tracked managed files,
  clock) and the scratch environment for candidate binaries.
- `src/claude_wrangler/operations.py`
  Install, upgrade, repair, migrate, update, rollback, and uninstall,
  each holding the maintenance lock.
- `src/claude_wrangler/migration.py`
  Read-only discovery of npm-era layouts, the rolling-forward rule,
  adopt-in-place, the schema-1 compatibility projection, and npm-tree
  cleanup.
- `src/claude_wrangler/generator.py`
  Atomic publication of `launch.py`, `launch.json`, the shims, and the
  README; the config-dir `settings.json` merge; freshness reports.
- `src/claude_wrangler/launcher_core.py`
  The standalone launcher core, copied byte for byte to
  `<runtime root>/launch.py` in each target project. It must stay one
  file, import nothing from `claude_wrangler`, and run on Python 3.9 with
  the standard library alone. It repeats a few constants and the host
  platform detector; `tests/test_cross_module_contracts.py` keeps those
  identical to the package.
- `src/claude_wrangler/releases.py`
  Notify-only channel catalog refresh for `--update` and the launcher's
  `update-check.json` cache.

Install and support code:

- `src/claude_wrangler/filesystem.py`
  Conservative filesystem mutation helpers with byte-exact line
  endings; `.gitignore` edits never produce `\r\r\n`.
- `src/claude_wrangler/install_scope.py`
  Explicit user-home targeting helpers for installer trust boundaries.
  Reads `USERPROFILE` (then `HOMEDRIVE`/`HOMEPATH`, then `HOME`) on
  Windows and `HOME` elsewhere, and recognizes this tool's
  `.local/claude/HOME` and a coexisting codex-wrangler
  `.local/codex-home` or legacy `.codex-home` as repo-local isolated
  homes.
- `src/claude_wrangler/runtime.py`
  Small process and stderr helpers.
- `src/claude_wrangler/installer.py`
  Dedicated user-venv installer helpers, the user launcher (a symlink on
  POSIX, a copied `claude-wrangler.exe` on Windows), and the PATH hint.
- `src/claude_wrangler/project_install.py`
  Repo-specific install-hook logic for dev, user-local standard, and
  system-standard installs plus launcher management.
- `src/claude_wrangler/devtools.py`
  Local developer tooling: the safe serial Black wrapper and the
  repository quality gate.
- `src/claude_wrangler/constants.py`
  Stable script names, markers, managed-path defaults, and pinned
  Claude defaults.
- `src/claude_wrangler/_version.py`
  Single-source package version.
- `src/claude_wrangler/models.py`
  Internal dataclasses and shared error types.
- `src/claude_wrangler/__main__.py`
  Module entry point so `python -m claude_wrangler` works.

Repository entry points and scripts:

- `claude-wrangler.py`
  Thin repository-root wrapper for direct execution from the checkout.
- `install.sh`
  Minimal top-level bootstrap that finds Python 3.9+ and launches
  stage 2. `bootstrap.sh` and `bootstrap-stage2.py` remain
  compatibility wrappers.
- `install.cmd` and `scripts/install-stage-1-windows.py`
  The native-Windows entry point and its policy helper, which then runs
  the unchanged stage 2. ASCII and CRLF, no labels, and `%*` kept out
  of parenthesized blocks and `for` lists.
- `scripts/install_prerequisites.py`, `.sh`, and `.cmd`
  Project-owned installer for missing prerequisites (Python, Git, Git
  for Windows' bash) through winget, brew, apt, or dnf, plus the `--dev`
  virtual-environment path.
- `scripts/install-stage-2.py`
  Managed stage-2 installer/bootstrap from the TheKnowledge starter.
- `scripts/python_environment_bootstrap.py`
  Shared Python 3.9-safe helper layer for managed stage 1 and stage 2.
- `scripts/install_user_tool.py`
  Repository-root helper that installs the user command into a
  dedicated venv.
- `scripts/install_project.py`
  Repository-root install hook that keeps developer installs bound to
  this checkout through a managed launcher in `~/.local/bin`.
- `scripts/run_tests_isolated.py`
  Runs pytest with every home-like variable redirected into a temporary
  directory.
- `scripts/git_commit_with_changelog.py`
  Repo-local wrapper that syncs `CHANGELOG.md` from the pending-commit
  queue before delegating to TheKnowledge's standard commit helper.
- `scripts/run_local_quality_gate.py` and `scripts/run_black_safe.py`
  Repo wrappers over `claude_wrangler.devtools`.
- `scripts/run_tool_with_timeout.py`, `scripts/run_quality_gate_cached.py`,
  and `scripts/validate_knacks.py`
  Thin repo-root delegators to the TheKnowledge helpers.

Specifications and tests:

- `docs/specifications/`
  Behavior specifications. Start with
  `native_runtime_distribution_and_verification.md`,
  `ab_runtime_slots.md`, `cross_platform_launcher.md`,
  `legacy_npm_layout_migration.md`,
  `prerequisites_and_platform_entry_points.md`, and
  `security_ring_model.md`. `bounded_managed_npm_operations.md` is
  superseded and kept for history.
- `tests/`
  Focused tests per module, plus real shim execution through cmd,
  PowerShell, Git Bash, and `sh`; Windows entry-point tests that run
  `install.cmd` from copied fake checkouts; cross-module contract and
  Python 3.9 syntax guards; and tripwires for `.gitignore` and
  `AGENTS.md`.
- `tests/fixtures/releases/`
  Byte-exact recorded release manifests and signatures (2.1.207,
  2.1.287, 2.1.296) for offline signature tests. Its own
  `.gitattributes` disables text conversion; never add binaries there.
- `tests/native_fakes.py` and `tests/fixtures/fake_claude/`
  Offline fakes for operation tests and the source of a small fake
  Claude Code console program compiled at test time on Windows.

## Developer Bootstrap

Use this when you are setting up the repository itself for active
development on Linux, macOS, or WSL2:

```bash
./install.sh --mode dev
```

It ensures Python 3.9+ is available, initializes `TheKnowledge`,
installs the pyenv-managed Python line from
`python-environments.json`, rebuilds `.venv`, installs the package in
editable mode, and makes `direnv` mandatory for automatic activation
inside the repo. Open a new shell in this repository afterward so the
managed `direnv` hook can activate `.venv`.

On native Windows the managed development mode (pyenv and direnv) is
not available. Use the project's prerequisite installer instead, from
cmd or PowerShell:

```bat
scripts\install_prerequisites.cmd --dev
```

It installs whatever is missing (Python 3.9+, Git, Git for Windows'
bash) through `winget --scope user`, then creates `.venv` with
`.venv\Scripts\python.exe` and installs the package in editable mode
with its development extras. Add `--dry-run` to see the commands first,
`--yes` to skip the confirmation, and `--venv PATH` for another
location. Initialize the submodule yourself with
`git submodule update --init --recursive`. The same `--dev` path works
on POSIX through `scripts/install_prerequisites.sh --dev`.

When the current shell is a repo-local `claude-local` session with the
home redirected into `.local/claude/HOME`, user-scoped installer
helpers refuse to mutate that isolated home silently; pass
`--user-home /real/home` or `--allow-isolated-home` deliberately.

## Install Modes

The managed installer supports these outcomes:

- `./install.sh`
  Standard user-local mode with a `~/.local/bin/claude-wrangler`
  launcher.
- `./install.sh --mode dev`
  Development mode with editable install, mandatory `direnv`,
  shell-hook integration, and a project-bound
  `~/.local/bin/claude-wrangler` launcher.
- `./install.sh --mode venv-only`
  Toolchain-only mode that prepares `.venv` without installing the
  package.
- `sudo ./install.sh --system`
  Standard system mode publishing `/usr/local/bin/` launchers. Only
  valid under root; `sudo ./install.sh` without `--system` is rejected.
- `install.cmd` (native Windows)
  Standard user-local mode only, with a copied
  `%USERPROFILE%\.local\bin\claude-wrangler.exe`. It refuses `--mode
  dev`, `--mode venv-only`, and `--system`, isolated homes without
  `--allow-isolated-home`, and unconfirmed elevated runs (set
  `THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED=1` for CI). Where `.cmd` files
  are blocked, run `py -3 scripts\install-stage-1-windows.py`.

## Low-Level User Installer Helper

`./install.sh` (or `install.cmd`) is the preferred normal-install entry
point. Use the repository installer helper only when you explicitly
want to bypass the managed stage-1 and stage-2 flow:

```bash
python3 scripts/install_user_tool.py
```

That creates a dedicated virtual environment under
`<user-home>/.local/share/claude-wrangler/venv`, installs
`claude-wrangler` there, and links
`<user-home>/.local/bin/claude-wrangler` to the venv-managed command.
On Windows the launcher is a copied `claude-wrangler.exe` instead of a
link; a copy that is running is renamed aside and removed by a later
refresh. Neither installer edits `PATH`; both print the command that
adds the launcher directory when it is missing. Use
`--user-home /path/to/home` when the current process home is not the
user scope you intend to manage, and `--allow-isolated-home` only when
an isolated repo-local home should receive the install on purpose.

## Managed Validation Toolchain

After `./install.sh --mode dev` or `./install.sh --mode venv-only`, use
the hardened local quality-gate script for the default repository
validation sequence:

```bash
.venv/bin/python scripts/run_local_quality_gate.py
```

That script runs Black through `scripts/run_black_safe.py`, which
formats one file at a time with timeout protection and probes whether
the installed Black supports flags such as `--no-cache`.

`pyproject.toml` sets Black's and Ruff's target version to Python 3.9,
so both tools enforce the project's syntax floor. On Windows, run the
checks directly until the test suite is Windows-clean under the gate:

```bat
.venv\Scripts\python.exe -m black --check src tests scripts
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe scripts\run_tests_isolated.py -q
```

## Running The Utility From Source

After `./install.sh --mode dev`, entering the repository should
auto-activate `.venv` through `direnv`, so the user-facing command
works directly:

```bash
claude-wrangler --help
```

To execute code from this checkout without the installed command, use
the repository wrapper:

```bash
python3 ./claude-wrangler.py --help
```

Module-style execution from the repo-local venv also works:

```bash
.venv/bin/python -m claude_wrangler --help
```

On Windows, use `.venv\Scripts\python.exe -m claude_wrangler --help`.

## Tests

Run the suite through the isolated runner, which points `HOME`,
`USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, and
`XDG_*` at a temporary directory before pytest starts:

```bash
.venv/bin/python scripts/run_tests_isolated.py -q
```

On Windows this is required, not optional: some tests run installers
that resolve the user home from `USERPROFILE`, and a real-profile
install is ring 0 (see `AGENTS.md`). Use
`.venv\Scripts\python.exe scripts\run_tests_isolated.py -q`, and pass
test paths to run a subset. An autouse fixture in `tests/conftest.py`
also redirects every home variable per test; a test that truly needs
the unmodified environment must say so with
`@pytest.mark.real_home_env`.

The tests stay offline and light on external side effects:

- Signature tests verify recorded real releases; operation tests use
  the fakes in `tests/native_fakes.py`, while slot promotion, metadata,
  generated files, and the launcher core stay real.
- No test downloads a binary or contacts the release server. The
  opt-in `tests/test_real_binary_install.py` promotes a copy of an
  already-installed native `claude` named by
  `CLAUDE_WRANGLER_TEST_REAL_BINARY` (its release must be one of the
  recorded fixtures) through the real install transaction, serving it
  from memory. The binary only ever runs with `--version` and `--help`,
  with every home-like variable redirected into scratch.
- On Windows the shim tests compile a fake Claude Code console program
  with the .NET Framework `csc.exe` and skip when it is absent; Git Bash
  and PowerShell cases skip when those shells are missing.
- POSIX-only tests (`install.sh`, execute bits, file permissions) skip
  on Windows, and Windows-only tests skip elsewhere.
- `tests/test_python39_compatibility.py` parses the shipped code with
  the Python 3.9 grammar; a real Python 3.9 run still belongs in CI. The
  whole suite passes on CPython 3.9 on Windows when the temporary
  directory is short: a temp path of about 140 characters pushes some
  shim tests past `MAX_PATH`, so run from a short `TEMP` or pass a short
  `--basetemp`.
- `tests/native_fakes.py` also provides `UnreachableSource` (a release
  source that fails like a blocked network and records each attempt) and
  `plant_unverified_slot` (a slot whose record and size match but whose
  bytes and evidence do not), for tests of state a repository could
  supply. Fake services report no git-tracked files unless a test passes
  `tracked_files=services.list_tracked_files` to use real git.

## Line Endings

`.gitattributes` pins `*.cmd` and `*.bat` to CRLF; shell and Python
files are not pinned, because TheKnowledge's drift report compares
managed files byte for byte. Files that claude-wrangler generates are
written with explicit newlines (LF for anything POSIX shells or Python
read, CRLF for `.cmd`). `tests/fixtures/releases/.gitattributes`
disables text conversion so recorded signatures keep verifying under
`core.autocrlf`. With `core.autocrlf=true`, a fresh Windows checkout
gets a CRLF `scripts/install_prerequisites.sh`, which Git Bash cannot
run; use the `.cmd` wrapper there. Stage
`scripts/install_prerequisites.sh` with `git add --chmod=+x` so it stays
executable.

## Packaging Notes

This project uses a conventional `src/` layout and a `pyproject.toml`
configuration with a console script entry point:

- command name: `claude-wrangler`
- entry point: `claude_wrangler.cli:main`
- dynamic version source: `claude_wrangler.__version__`
- runtime dependencies: none (standard library only)

That means both normal and editable installs place the same command
into `~/.local/bin` (`%USERPROFILE%\.local\bin` on Windows), which is
the crucial detail for fresh-shell usability. `launcher_core.py` ships
inside the package because every install copies it into the target
project as `launch.py`.
