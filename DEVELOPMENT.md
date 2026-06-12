# Development

## Project Layout

- `src/claude_wrangler/cli.py`
  Thin entry point and top-level dispatch.
- `src/claude_wrangler/config.py`
  Argument parsing and runtime configuration selection.
- `src/claude_wrangler/layout.py`
  Path validation and existing-state discovery.
- `src/claude_wrangler/environment.py`
  Runtime-resolution diagnostics, selector detection, and launcher
  environment reporting (including the pinned `CLAUDE_CONFIG_DIR`).
- `src/claude_wrangler/changelog.py`
  Keep a Changelog style parsing and queue-to-changelog sync helpers.
- `src/claude_wrangler/install_scope.py`
  Explicit user-home targeting helpers for installer trust boundaries.
  Recognizes both this tool's `.local/claude/HOME` and a coexisting
  codex-wrangler `.codex-home` as repo-local isolated homes.
- `src/claude_wrangler/rendering.py`
  Deterministic managed file content and summary rendering, including
  the ring-aware launcher.
- `src/claude_wrangler/filesystem.py`
  Conservative filesystem mutation helpers.
- `src/claude_wrangler/operations.py`
  Install, inspect, uninstall, and self-test behaviors.
- `src/claude_wrangler/releases.py`
  Channel and dist-tag resolution for `@anthropic-ai/claude-code`.
  Channels map one-to-one onto the `stable`, `latest`, and `next`
  dist-tags; exact versions never imply a channel.
- `src/claude_wrangler/runtime.py`
  Small process and stderr helpers.
- `src/claude_wrangler/installer.py`
  Dedicated user-venv installer helpers for cross-distro command setup.
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
- `claude-wrangler.py`
  Thin repository-root wrapper for direct execution from the checkout.
- `scripts/install_user_tool.py`
  Repository-root helper that installs the user command into a
  dedicated venv.
- `scripts/git_commit_with_changelog.py`
  Repo-local wrapper that syncs `CHANGELOG.md` from the pending-commit
  queue before delegating to TheKnowledge's standard commit helper.
- `scripts/install_project.py`
  Repository-root install hook that keeps developer installs bound to
  this checkout through a managed launcher in `~/.local/bin`.
- `scripts/install-stage-2.py`
  Managed stage-2 installer/bootstrap from the TheKnowledge starter.
- `scripts/python_environment_bootstrap.py`
  Shared Python 3.9-safe helper layer for managed stage 1 and stage 2.
- `scripts/run_local_quality_gate.py` and `scripts/run_black_safe.py`
  Repo wrappers over `claude_wrangler.devtools`.
- `scripts/run_tool_with_timeout.py`, `scripts/run_quality_gate_cached.py`,
  and `scripts/validate_knacks.py`
  Thin repo-root delegators to the TheKnowledge helpers.
- `install.sh`
  Minimal top-level bootstrap that finds Python 3.9+ and launches
  stage 2. `bootstrap.sh` and `bootstrap-stage2.py` remain
  compatibility wrappers.
- `docs/specifications/`
  Behavior specifications, including the security ring model and the
  session-state isolation contract.
- `tests/`
  Focused tests split by config, rendering (including live launcher
  execution against stub binaries), filesystem, operations, releases,
  bootstrap and installer behavior, entry points, and runtime
  detection.

## Developer Bootstrap

Use this when you are setting up the repository itself for active
development:

```bash
./install.sh --mode dev
```

It ensures Python 3.9+ is available, initializes `TheKnowledge`,
installs the pyenv-managed Python line from
`python-environments.json`, rebuilds `.venv`, installs the package in
editable mode, and makes `direnv` mandatory for automatic activation
inside the repo. Open a new shell in this repository afterward so the
managed `direnv` hook can activate `.venv`.

When the current shell is a repo-local `claude-local` session with
`HOME` redirected into `.local/claude/HOME`, user-scoped installer
helpers refuse to mutate that isolated home silently; pass
`--user-home /real/home` or `--allow-isolated-home` deliberately.

## Install Modes

The managed installer supports four distinct outcomes:

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

## Low-Level User Installer Helper

`./install.sh` is the preferred normal-install entry point. Use the
repository installer helper only when you explicitly want to bypass the
managed stage-1 and stage-2 flow:

```bash
python3 scripts/install_user_tool.py
```

That creates a dedicated virtual environment under
`<user-home>/.local/share/claude-wrangler/venv`, installs
`claude-wrangler` there, and links
`<user-home>/.local/bin/claude-wrangler` to the venv-managed command.
Use `--user-home /path/to/home` when the current process `HOME` is not
the user scope you intend to manage, and `--allow-isolated-home` only
when an isolated repo-local home should receive the install on
purpose.

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

## Tests

For a checkout bootstrapped with `./install.sh --mode dev`, use the
standardized project path:

```bash
.venv/bin/python scripts/run_local_quality_gate.py pytest
```

The repo-local developer virtual environment also works directly:

```bash
.venv/bin/python -m pytest
```

The tests intentionally stay light on external side effects while
covering the version/channel logic, managed content generation
(including executing the rendered launcher against stub `node` and
`claude` binaries to verify ring selection, the `CLAUDE_CONFIG_DIR`
pin, and permission-default injection), conservative uninstall
behavior, and inspection/self-test failure modes.

## Packaging Notes

This project uses a conventional `src/` layout and a `pyproject.toml`
configuration with a console script entry point:

- command name: `claude-wrangler`
- entry point: `claude_wrangler.cli:main`
- dynamic version source: `claude_wrangler.__version__`

That means both normal and editable installs place the same command
into `~/.local/bin`, which is the crucial detail for fresh-shell
usability.
