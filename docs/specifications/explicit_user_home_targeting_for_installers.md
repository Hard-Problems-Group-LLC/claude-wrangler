# Explicit User-Home Targeting for Installers

## Purpose

Define how `claude-wrangler` installer entry points distinguish
repo-local effects from user-scoped effects when the active session may
be an isolated AI home rather than the operator's normal login home.

## Scope

This specification covers:

- `./install.sh` and `scripts/install-stage-2.py`;
- `python3 scripts/install_user_tool.py`;
- user-scoped pyenv, direnv, shell-init, launcher, and venv paths
  selected by those entry points; and
- the trust-boundary language used in repository documentation.

## Requirements

1. Explicit user-home targeting
   Installer entry points that manage user-scoped paths must treat the
   target home directory as an explicit policy decision, not as an
   unexamined side effect of the current process environment.
2. Isolated-home safeguard
   When the current `HOME` resolves to a repository's project-local
   isolated home, user-scoped installer flows must fail by default
   instead of silently writing user-local state there. Recognized
   isolated homes include this tool's `.local/claude/HOME` and the
   `codex-wrangler` `.codex-home` layout, so mixed-wrangler projects
   stay guarded from either session type.
3. Approved escape hatches
   User-scoped installer flows must support:
   - `--user-home PATH` to target some other user scope explicitly;
     and
   - `--allow-isolated-home` to opt into writing user-scoped state
     under the current isolated home on purpose.
4. Scope preservation
   Existing install scopes remain:
   - `repo` for repo-local development state;
   - `user` for user-local standard installs; and
   - `system` for explicit system installs.
   This behavior clarifies which home the `user`-scoped portions of
   those flows will touch. It does not create a second
   runtime-selection system.
5. User-scoped path derivation
   When `--user-home PATH` is selected, installer code must derive
   user-local paths such as `~/.local/bin`,
   `~/.local/share/.../venv`, and `~/.pyenv` from that explicit target
   home instead of from the current process `HOME`.
6. Documentation
   User-facing docs must explain the middle-ground trust model:
   repo-local work stays easy, isolated AI homes may be used
   deliberately, and operator-user installs require explicit targeting
   when the current session is already isolated.

## Validation

- Unit tests must cover explicit user-home resolution and
  isolated-home rejection for both recognized isolated-home layouts.
- Help text and docs must mention `--user-home` and
  `--allow-isolated-home`.
- Installer success output must identify the selected user home and
  whether it came from the current process or an explicit override.
