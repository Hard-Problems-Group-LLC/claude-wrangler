# Runtime Fidelity and Startup Preflight

## Purpose

Define how `claude-wrangler` verifies runtime-related invariants and
exposes runtime-resolution diagnostics without taking ownership of the
target project's broader Python or Node.js bootstrap.

Claude Code now runs as a pinned native binary
(`native_runtime_distribution_and_verification.md`), so Node.js is no
longer part of the runtime. The Node.js checks below remain only because
project tooling that Claude Code starts (hooks, test commands, package
scripts) may still depend on the project's selected Node.js release.

## Scope

This specification covers:

- the generated launcher core (`<runtime root>/launch.py`) that
  `bin/claude-local` and `bin/claude-local.cmd` run;
- `claude-wrangler --inspect`;
- `claude-wrangler --selftest`; and
- the documented ownership boundary between `claude-wrangler` and the
  target project's own runtime-selection machinery.

## Requirements

1. Ownership boundary
   `claude-wrangler` must continue to own only its namespaced
   artifacts: `.local/claude/` (the runtime root, the redirected home,
   and the pinned config dir, including the suppression keys it merges
   into `<config dir>/settings.json`), `bin/claude-local`,
   `bin/claude-local.cmd`, `README-LOCAL-Start-Claude.md`, and its
   marked `.gitignore` block. It must not rewrite the target project's
   own runtime-selector files such as `.nvmrc`, `.node-version`,
   `.tool-versions`, `package.json`, `pyproject.toml`, or `.envrc`.
2. Launcher preflight
   The launcher core must run a cheap startup preflight before it
   starts Claude Code:
   - Runtime: the cheap active-slot check, fallback, and refusal of
     `ab_runtime_slots.md` requirement 8. A missing or damaged runtime
     is handled there, in every preflight mode, and never by running a
     global `claude`.
   - Node.js selector: the first checked-in selector file present, in
     the order `.nvmrc`, `.node-version`, `.tool-versions` (its
     `nodejs` line), is read. With no selector, the Node.js check does
     nothing and `node` is never looked up.
   - Node.js lookup: when a selector exists, `node` is looked up on
     `PATH` without searching the current directory or relative `PATH`
     entries (on Windows, through `PATHEXT`), and `node --version` runs
     with a 10-second timeout.
   - Comparison: a literal selector (`20`, `v20.11`, `20.11.0`) matches
     by leading version components, so `20` matches `v20.11.0`, as nvm
     and similar managers resolve it. A non-literal selector (an alias
     such as `lts/*`, or a range) only produces an advisory note.
   - Output: diagnostics go to stderr with the `[claude-local]
     preflight:` prefix before Claude Code starts.
   Node.js is never required. A missing `node` produces a warning that
   says Claude Code does not need it and project tooling may.
3. Launcher modes
   The Node.js check must support three operator-selectable modes
   through `CLAUDE_LOCAL_PREFLIGHT`:
   - `warn`: default mode; report mismatches and non-literal selectors,
     then continue;
   - `strict`: exit 1 without starting Claude Code when a literal
     selector does not match the `node` found on `PATH`, or when a
     `node` is found but `node --version` cannot be resolved;
   - `off`: skip the Node.js check entirely.
   A missing `node` only warns, in every mode. Unknown mode values must
   degrade to `warn` with a visible warning.
4. Inspect diagnostics
   `claude-wrangler --inspect` must report:
   - resolved `node`, `npm`, and `npx` paths and versions when
     available, as information only (`node_required: false`);
   - detected checked-in Node.js selector files, their raw values, and
     whether each literal selector matches the resolved `node`;
   - any `package.json` package-manager declaration in the target
     project;
   - whether the launcher defaults to shared or isolated `HOME`; and
   - the effective `CLAUDE_CONFIG_DIR`, `HOME`, `XDG_*`, and `PATH`
     shim prefix the launcher will expose at rings 1 and 0, plus
     `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`, and
     `LOCALAPPDATA` on native Windows, computed with the launcher
     core's own functions.
   A selector mismatch, an unresolvable `node --version`, and a
   selector value that is not a literal version are reported as
   warnings; a missing `node` is reported without a warning.
5. Self-test diagnostics
   `claude-wrangler --selftest` must include the deeper runtime
   diagnostics and fail when an explicit literal Node.js selector
   disagrees with the resolved Node.js version. A missing `node`, an
   unresolvable `node --version`, and non-literal selector values pass,
   because Claude Code does not need Node.js; inspection still reports
   them.
6. Security stance
   Project-local isolation (`.local/claude/HOME` plus the pinned
   config dir) remains the default. No runtime diagnostic or preflight
   feature may silently broaden the writable surface or bypass the
   target project's own selector policies.
7. Submodule behavior
   Documentation must state that submodules such as `TheKnowledge`
   should use the parent project's local interpreters and package
   environments as if they were part of the same project tree.

## Validation

- Unit tests must cover the inspection and self-test report fields,
  including `node_required: false` and a passing self-test without
  `node`.
- Launcher-core tests must cover each preflight mode, leading-component
  selector matching, warn-only behavior when `node` is missing, unknown
  mode values, and that `node` is never taken from the current
  directory.
- The launcher core must run with no `node` on `PATH`.
- A repeatable smoke check should compare runtime resolution between a
  normal project shell and a `claude-local`-launched session.
