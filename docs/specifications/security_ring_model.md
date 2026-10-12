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
7. Home redirection on native Windows
   Native Windows programs resolve the user's home from `USERPROFILE`
   (and `HOMEDRIVE`/`HOMEPATH`, `APPDATA`, `LOCALAPPDATA`), not from
   `HOME`. On native Windows, "`$HOME` redirected" in the ring
   definitions means all of `HOME`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`,
   `APPDATA`, `LOCALAPPDATA`, and `XDG_*` point into
   `.local/claude/HOME`. Redirecting `HOME` alone is effectively ring 0
   for native processes and must never be presented as ring 1. See
   `cross_platform_launcher.md`.
8. Native-Windows default ring
   Claude Code has no sandbox on native Windows, so rings 3 and 2 do not
   exist there; WSL2 provides them. With operator confirmation recorded
   in `AGENTS.md` on 2026-10-12, native Windows defaults to ring 1. The
   launcher warns once per session that commands run unsandboxed and
   that isolation covers Claude's state and redirected-home tools but not
   per-user Windows stores (Credential Manager, the registry,
   `HKCU\SOFTWARE\Policies`, and OpenSSH keys and agent). A project may
   turn the warning off for personal work. Client work that needs ring 3
   should use WSL2 or a separate Windows account.

   Ring 1 also shares these, as it does on Linux, where OpenSSH reads
   the password-database home and agents and keyrings are per user:
   Windows OpenSSH keys, config, and the `ssh-agent` service (OpenSSH
   finds `~\.ssh` from the account profile in the registry, not from
   `USERPROFILE`); programs that ask Windows for known folders
   (`SHGetKnownFolderPath`, .NET `Environment.GetFolderPath`), which
   return the real profile; the PowerShell module path; machine-wide
   managed settings (`C:\Program Files\ClaudeCode`); and credentials
   passed in environment variables, which the launcher names once per
   session (`cross_platform_launcher.md` requirement 3). Claude Code may
   also keep a login in Windows Credential Manager under a name derived
   from the config-dir path, which outlives `--uninstall` and would be
   found by a later checkout at the same path
   (`ab_runtime_slots.md` requirement 12).
9. Self-update suppression
   At every ring the launcher disables Claude Code's self-update and
   installer, because they write to `HOME`-relative locations and at
   ring 0 would modify the operator's global install. See
   `session_state_isolation_and_config_dir_pinning.md` requirement 5a.

## Validation

- Launcher tests must cover the default ring 1 environment (redirected
  `HOME`, XDG paths, pinned `CLAUDE_CONFIG_DIR`).
- Launcher tests must cover the ring 0 escape hatch, including that the
  config-dir pin survives it.
- Launcher tests must cover the native-Windows ring 1 variable set and
  the native-Windows sandbox warning.
- `AGENTS.md` must document the ring definitions and the escalation
  policy for agents.
