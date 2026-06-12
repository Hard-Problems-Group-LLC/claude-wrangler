# Security Ring Model for Agent Sessions

## Purpose

Define the shared isolation ladder that `claude-wrangler` projects use
when AI agents build, test, debug, and operate on repository code, so
that agent work defaults to the least-privileged environment that can do
the job and so that ring decisions stay visible to operators.

These definitions are intended to be shared: `codex-wrangler` is
expected to adopt the same ring numbering when it is next upgraded.

## Rationale

Bad tool updates and stray writes into an operator's real home directory
have previously broken development on every project at once. Project-
local pinning limits tool blast radius; the ring model limits state
blast radius by keeping agent-visible `$HOME` as isolated as the task
allows.

## Ring Definitions

Ring numbering follows the CPU protection-ring convention: ring 0 is
the most privileged, and privilege decreases as the number increases.

- Ring 3 (default): the agent runs work inside an agent-managed sandbox
  with its own ephemeral, sandbox-internal `$HOME`. Human developers
  cannot see that home on the real system without effort.
- Ring 2: the agent still runs work inside the sandbox, but `$HOME` is
  the project-local persistent home `.local/claude/HOME`, which humans
  can inspect directly.
- Ring 1: the agent runs work outside the sandbox, with `$HOME` still
  redirected to `.local/claude/HOME`.
- Ring 0 (last resort): the agent runs work outside the sandbox using
  the operator's real `$HOME`.

## Requirements

1. Default posture
   Agents must run build, test, and debug work at ring 3 whenever that
   is reasonable for the task. Escalation toward ring 0 is permitted
   only after the current ring has been established as inadequate for
   the specific task at hand.
2. Recorded escalations
   When a specific task class is found to require a lower ring, the
   project's `AGENTS.md` must record the task class and the required
   ring so future sessions do not rediscover the limitation.
3. Default changes require confirmation
   A project may change its default ring (for example, a difficult
   project that cannot build under the sandbox), but changing the
   default always requires explicit operator confirmation, and the new
   default must be recorded in `AGENTS.md`.
4. Launcher posture
   The generated `bin/claude-local` launcher establishes the session
   environment at ring 1: the Claude Code process itself runs outside
   any sandbox with `HOME` redirected to `.local/claude/HOME`. Rings 3
   and 2 are the agent's responsibility inside the session, using the
   sandboxing tools the session provides.
5. Ring 0 escape hatch
   The launcher must provide a per-invocation escape hatch that runs
   the session with the operator's real `$HOME` (ring 0) without
   reinstalling or regenerating managed files. Claude session state
   must remain pinned to the project (`CLAUDE_CONFIG_DIR`) even at
   ring 0, so escalation does not fork auth or session history.
6. Session-state pinning
   Claude-specific session state (auth, transcripts, history, todos,
   settings) is a separate axis from the `$HOME` rings and must stay
   under `.local/claude` at every ring. See
   `session_state_isolation_and_config_dir_pinning.md`.

## Validation

- Launcher tests must cover the default ring 1 environment (redirected
  `HOME`, XDG paths, pinned `CLAUDE_CONFIG_DIR`).
- Launcher tests must cover the ring 0 escape hatch, including that the
  config-dir pin survives it.
- `AGENTS.md` must document the ring definitions and the escalation
  policy for agents.
