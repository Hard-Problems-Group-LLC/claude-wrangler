# ECR: Managed install.sh on Stock macOS bash 3.2, and CDPATH in Root Scripts

- Date: 2026-10-12
- Status: open (drafted in a consuming project whose TheKnowledge
  checkout is read-only for upstream maintenance)
- Requestor: consuming-project maintainers (AI pairing session)
- Consuming-project record: backlog entry "`install.sh` aborts on stock
  macOS bash 3.2" (review POSIX-2, added 2026-10-12T05:40:00-07:00)

## Summary

The starter `install.sh` that TheKnowledge installs into consuming
projects stops before stage 2 on a default Mac when it is run with no
arguments, which is the documented Quick Start. The managed root scripts
also derive their own directory with a plain `cd`, which an exported
`CDPATH` can redirect or make print the directory into the captured
value.

## Problem 1: empty array under `set -u` on bash 3.2

`install.sh` runs with `set -euo pipefail`. `main` starts with
`local stage_2_args=("$@")` and later runs stage 2 with
`"${stage_2_args[@]}"`. Bash before 4.4 treats the expansion of an
empty array as an unbound variable under `set -u`, so on macOS's
`/bin/bash` (3.2.57) a run without arguments aborts with
"stage_2_args[@]: unbound variable". Passing any argument, or having a
newer bash first on `PATH`, hides the problem.

### Requested change

Either of these keeps the behavior on every bash:

- Pass `"$@"` straight through to the final `exec`. `main` never
  shifts its arguments, so the copy in `stage_2_args` is not needed.
- Keep the array but expand it with the bash 3.2-safe idiom
  `${stage_2_args[@]+"${stage_2_args[@]}"}`.

The same idiom applies to any other array in the managed shell starters
that can be empty under `set -u`.

## Problem 2: `cd` without `CDPATH= ... --` in the root scripts

`install.sh`, `bootstrap.sh`, `set-context.sh`, and
`set-context-bootstrap.sh` compute their directory with
`"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`. With `CDPATH`
exported (a common interactive setting), `cd` can resolve a relative
`dirname` result against a `CDPATH` entry instead of the current
directory, and it prints the directory it chose, which then becomes part
of the captured value. A path that starts with `-` is also read as an
option.

### Requested change

Use `"$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"` in
every managed root script that derives its own location.

## Validation Requested

- A macOS CI step (or a bash 3.2 container) that runs
  `/bin/bash install.sh` with no arguments against a stub stage 2 that
  records its argument list, asserting it ran with an empty list.
- A step that runs each root script with `CDPATH` exported to a
  directory holding a same-named decoy, asserting the script still
  finds its own checkout.

## Notes

- The consuming project must not edit or refresh the managed
  `install.sh` from its Windows checkout (its AGENTS.md forbids the
  forced starter refresh there), so it carries this as an upstream
  request, together with the other starter items it has queued for
  TheKnowledge's successor.
- The consuming project's own `scripts/install_prerequisites.sh` already
  uses `CDPATH= cd --`; it is not a managed file.
