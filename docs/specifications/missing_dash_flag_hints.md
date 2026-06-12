# Missing-Dash Flag Hints

## Purpose

Define how `claude-wrangler` reports common flag spelling mistakes
without changing command-line semantics.

## Scope

This specification applies when configuration normalization validates
the target project root. It covers nonexistent project roots that look
like known long-form flags without their required `--` prefix.

## Requirements

1. Preserve command semantics
   Bare words such as `update` must remain positional project-root
   arguments. `claude-wrangler update` must not silently become
   `claude-wrangler --update .`.
2. Respect real project names
   If the positional project root exists, even when it is named
   `update`, the command must proceed using that directory.
3. Hint on missing dashes
   If the positional project root does not exist and the literal token
   is a known long-form flag without its leading dashes, the error must
   include a targeted hint such as `Did you perhaps mean '--update'?`.
4. Friendly configuration errors
   Configuration-time `ClaudeWranglerError` exceptions must be reported
   as `ERROR: ...` without exposing a Python traceback to normal CLI
   users.

## Validation

- Unit tests must cover that `update` remains a positional project
  root.
- Unit tests must cover the missing-dash hint for nonexistent `update`.
- Unit tests must cover the missing-dash hint for at least one other
  known flag.
- Unit tests must cover that an existing project named `update` is
  accepted.
- Entrypoint tests must cover that configuration errors return exit
  code `1` and do not print tracebacks.
