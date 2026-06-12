# claude-wrangler

`claude-wrangler` is a Python utility for setting up per-project,
isolated Claude Code support: it manages project-local
`@anthropic-ai/claude-code` installs without making the repository root
look like a Node project, and keeps all session-specific Claude data
inside the project. It is designed to coexist with instances from
`codex-wrangler` in the same project.

It creates and manages a disciplined local layout under the
TheKnowledge-standard `.local/` operator-state directory:

- `.local/claude/npm/` for the local npm package install, npm cache, and
  managed metadata
- `.local/claude/HOME/` as the project-local `$HOME` used at security
  rings 1 and 2
- `.local/claude/` as the pinned `CLAUDE_CONFIG_DIR`, so Claude auth,
  sessions, history, and todos stay project-local at every ring
- `bin/claude-local` as the generated launcher used inside the target
  project
- `README-LOCAL-Start-Claude.md` as an ignored local operator guide
  inside the target project
- a marked `.gitignore` block so uninstall can remove only what it owns

The utility also supports:

- installation of the current stable Claude Code release by default
- JSON inspection with `--inspect`
- pessimistic verification with `--selftest`
- conservative removal with `--uninstall`
- version-catalog refresh with `--update`
- channel-aware upgrades with `--upgrade --channel stable|latest|next`
- exact-version targeting with `--version latest|x.y.z`
- managed reasonable-permissions launcher defaults with
  `--set-reasonable-permissions` and `--clear-reasonable-permissions`
  (mapped to `--permission-mode acceptEdits`, shipped disabled)

## Why Project-Local Pinning

A bad tool update that lands in a global install can take out
development work on every project at once. `claude-wrangler` pins the
Claude Code CLI per project, the same way a virtualenv pins Python
dependencies: the generated launcher execs the managed local binary
directly, never falls back to a global or registry copy, and emits
update *notices* rather than auto-updating. A bad release affects one
project, and the managed metadata records exactly what to roll back to.

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
  `CLAUDE_LOCAL_SHARED_HOME=1`.

`CLAUDE_CONFIG_DIR` stays pinned to `.local/claude` at every ring, so
escalating `$HOME` fidelity never forks Claude auth or session history.

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
`python TheKnowledge/scripts/git_standard_commit_push.py`.

## Quick Start

Use the top-level installer when you want a normal user-local install
from this checkout:

```bash
./install.sh
```

That is the default supported path. It ensures Python 3.9+ is available
and installs a stable non-development command into a user-local virtual
environment. Use `sudo ./install.sh --system` when you explicitly want
a system-level install; `sudo ./install.sh` without `--system` is
rejected on purpose.

## Developer Quick Start

Use this explicit development mode when you are setting up this
repository for active editing:

```bash
./install.sh --mode dev
```

It ensures Python 3.9+ is available, initializes `TheKnowledge`,
installs a pyenv-managed Python toolchain, builds the repo-local
`.venv`, installs the package in editable mode, and makes `direnv`
mandatory for automatic activation when you enter the repository. After
the script finishes, open a new shell in this repository so the managed
`direnv` hook can activate `.venv` automatically.

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

Refresh the locally known stable, latest, and next versions:

```bash
claude-wrangler --update .
```

Upgrade to the latest known version on one channel:

```bash
claude-wrangler --upgrade --channel stable .
claude-wrangler --upgrade --channel latest .
claude-wrangler --upgrade --channel next .
```

Upgrade to one exact version:

```bash
claude-wrangler --upgrade --channel stable --version 2.1.153 .
```

If a prior npm install was interrupted and left the managed install
inconsistent, use the explicit repair mode. It removes only managed npm
install artifacts under `.local/claude/npm` before reinstalling; it
never touches Claude session state:

```bash
claude-wrangler --upgrade --channel stable --repair-install .
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

`claude-wrangler` resolves the exact release for the stable, latest,
and next channels at install time from npm dist-tags (which map
one-to-one onto the channels), then writes that exact version plus the
known channel catalog into managed metadata. `--update` refreshes that
local catalog without changing the install, and `--upgrade --channel
...` uses the recorded catalog unless you supply an explicit exact
`--version`.

Managed npm operations use a 300-second timeout by default and disable
npm's optional audit, funding, update-notifier, and spinner progress
behavior while keeping HTTP fetch logs and foreground lifecycle-script
output for troubleshooting. Use `--npm-timeout-seconds` and
`--npm-install-loglevel` to adjust.

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

- `.local/claude/` (npm install, ring-1/2 HOME, pinned config dir)
- `bin/claude-local`
- `README-LOCAL-Start-Claude.md`
- a marked `.gitignore` block

The managed `.gitignore` block deliberately does not ignore the
project-root `.claude/` directory, which Claude Code uses for
trackable project settings. The block also never touches ignore blocks
owned by other tools, including TheKnowledge's managed baseline and
codex-wrangler's block, even where entries overlap.

## Runtime Boundary

`claude-wrangler` localizes the `@anthropic-ai/claude-code` package
install under `.local/claude/npm/` and localizes Claude state under
`.local/claude/`. It does not install or pin the `node` runtime used by
`bin/claude-local`, or the `npm` executable used by maintenance
commands. Managed npm operations set `NPM_CONFIG_CACHE` to
`.local/claude/npm/.npm-cache` so package installs and npm scratch
writes stay inside the target project.

The generated launcher executes
`.local/claude/npm/node_modules/.bin/claude` directly. It intentionally
does not use `npx claude` or any global `claude`, so a bad global tool
update or a registry package squatting a bare name cannot affect the
project. The Node.js runtime family still comes from the shell `PATH`
that launched the command.

The launcher performs a cheap startup preflight before it starts Claude
Code. It always refuses to launch when `node` or the managed local
binary is unavailable. By default it also warns when a checked-in
Node.js selector such as `.nvmrc`, `.node-version`, or `.tool-versions`
appears inconsistent with the active shell. Use
`CLAUDE_LOCAL_PREFLIGHT=warn`, `strict`, or `off` to control the
selector-warning behavior.

`claude-wrangler --inspect` and `--selftest` report the resolved
`node`, `npm`, and `npx` paths and versions, detected checked-in
Node.js selectors, any root `package.json` package-manager declaration,
and the effective `CLAUDE_CONFIG_DIR`, `HOME`, and `XDG_*` paths the
launcher will expose. They also report whether the managed local Claude
binary and its host platform package exist and whether the requested,
lockfile, and installed package versions agree.

## Trust and Install Scope

`claude-wrangler` treats user-scoped install targets as an explicit
trust boundary.

- Less-trusted AI or isolated `claude-local` sessions should normally
  stay in repo scope and should not mutate an operator's shell init,
  `~/.local/bin`, or `~/.pyenv` implicitly.
- More-trusted AI can still manage its own user scope, but that should
  be its own explicit home directory, not an accidental by-product of
  whatever `HOME` the current session inherited.
- Operator-user installs remain supported, but when the current `HOME`
  is a repo-local isolated home (this tool's `.local/claude/HOME` or a
  coexisting `.codex-home`), they require `--user-home /path/to/home`
  instead of silently targeting the wrong scope, or
  `--allow-isolated-home` when an AI-local install is deliberate.
- System installs remain separate and still require
  `sudo ./install.sh --system`.

## Safety Notes

The tool is intentionally suspicious.

- Managed paths must stay under the target project root.
- Uninstall removes files only when it can prove ownership, unless
  `--force` is used.
- The generated launcher and generated local README are
  content-checked.
- Inspection emits a JSON report so automation or operators can see
  what the tool believes it owns.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for project layout, test commands,
and packaging notes.
