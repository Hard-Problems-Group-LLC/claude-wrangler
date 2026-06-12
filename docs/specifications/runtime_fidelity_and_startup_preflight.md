# Runtime Fidelity and Startup Preflight

## Purpose

Define how `claude-wrangler` verifies runtime-related invariants and
exposes runtime-resolution diagnostics without taking ownership of the
target project's broader Python or Node.js bootstrap.

## Scope

This specification covers:

- the generated `bin/claude-local` launcher;
- `claude-wrangler --inspect`;
- `claude-wrangler --selftest`; and
- the documented ownership boundary between `claude-wrangler` and the
  target project's own runtime-selection machinery.

## Requirements

1. Ownership boundary
   `claude-wrangler` must continue to own only its namespaced
   artifacts: `.local/claude/`, `bin/claude-local`,
   `README-LOCAL-Start-Claude.md`, and its marked `.gitignore` block.
   It must not rewrite the target project's own runtime-selector files
   such as `.nvmrc`, `.node-version`, `.tool-versions`, `package.json`,
   `pyproject.toml`, or `.envrc`.
2. Launcher preflight
   The generated launcher must run a cheap startup preflight before
   invoking Claude Code. The preflight must:
   - verify whether `node` is resolvable from the active shell `PATH`;
   - verify the managed local `claude` binary exists and is executable;
   - detect checked-in Node.js selector files `.nvmrc`,
     `.node-version`, and `.tool-versions`;
   - compare explicit selector versions with the resolved
     `node --version` when the selector value is a literal version
     string; and
   - emit operator-facing diagnostics on stderr before launching
     Claude Code.
3. Launcher modes
   The launcher preflight must support three operator-selectable modes
   through `CLAUDE_LOCAL_PREFLIGHT`:
   - `warn`: default mode; surface mismatches and ambiguous selector
     signals, then continue;
   - `strict`: fail when an explicit checked-in Node.js selector does
     not match the resolved Node.js version;
   - `off`: skip the selector portion of the preflight.
   Missing `node` or a missing managed local binary must always refuse
   to launch, regardless of mode. Unknown mode values must degrade to
   `warn` with a visible warning.
4. Inspect diagnostics
   `claude-wrangler --inspect` must report:
   - resolved `node`, `npm`, and `npx` paths and versions when
     available;
   - detected checked-in Node.js selector files and their raw values;
   - any `package.json` package-manager declaration in the target
     project;
   - whether the launcher uses shared or isolated `HOME`; and
   - the effective `CLAUDE_CONFIG_DIR`, `HOME`, and `XDG_*` paths the
     launcher will expose.
5. Self-test diagnostics
   `claude-wrangler --selftest` must include the deeper runtime
   diagnostics and fail when an explicit checked-in Node.js selector
   disagrees with the resolved Node.js version. Ambiguous selector
   signals may warn without failing until a stricter reviewed policy
   is adopted.
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

- Unit tests must cover the inspection and self-test report fields.
- Unit tests must cover the generated launcher content for preflight
  behavior.
- A repeatable smoke check should compare runtime resolution between a
  normal project shell and a `bin/claude-local`-launched session.
