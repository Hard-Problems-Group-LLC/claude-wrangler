# Explicit User-Home Targeting for Installers

## Purpose

Define how `claude-wrangler` installer entry points distinguish
repo-local effects from user-scoped effects when the active session may
be an isolated AI home rather than the operator's normal login home.

## Scope

This specification covers:

- `./install.sh` and `scripts/install-stage-2.py`;
- `install.cmd` and its policy helper
  `scripts/install-stage-1-windows.py` on native Windows;
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
   When any home the current process advertises resolves to a
   repository's project-local isolated home, user-scoped installer
   flows must fail by default instead of silently writing user-local
   state there, and the refusal must name the variable that pointed
   there. Recognized isolated homes are this tool's
   `.local/claude/HOME`, `codex-wrangler`'s current
   `.local/codex-home`, and its legacy `.codex-home`, so mixed-wrangler
   projects stay guarded from either session type. The advertised homes
   depend on the operating system:
   - POSIX: `HOME`.
   - Native Windows: `USERPROFILE`, then `HOMEDRIVE` plus `HOMEPATH`,
     then `HOME`. Python and native Windows programs resolve the home
     from `USERPROFILE` and ignore `HOME`, so checking `HOME` alone
     would miss a ring-1 session (`security_ring_model.md`
     requirement 7). A partially redirected environment is caught
     through whichever variable still points at an isolated home.
   Paths are compared after resolving them, with a same-file fallback,
   so case, separator, and link differences do not hide a match. Two
   further checks catch homes that belong to another checkout:
   - any advertised home whose path ends like a recognized isolated home
     (`.local/claude/HOME`, `.local/codex-home`, or `.codex-home`,
     case-insensitively on Windows) counts, so an installer started from
     checkout B inside project A's ring-1 session refuses A's home too;
   - inside a launcher session (`CLAUDE_LOCAL_MANAGED_BY` or
     `CODEX_LOCAL_MANAGED_BY` set), a home that differs from the
     account's own home counts, which catches custom layouts. The
     account's home comes from the logon token on Windows
     (`GetUserProfileDirectoryW`) and from the password database on
     POSIX, never from the environment. On Windows `HOMEDRIVE` plus
     `HOMEPATH` takes part only when it is the effective home
     (`USERPROFILE` unset), because domain accounts often point it at a
     network home that legitimately differs. Ring-0 sessions keep the
     real home and pass.
3. Approved escape hatches
   User-scoped installer flows must support:
   - `--user-home PATH` to target some other user scope explicitly;
     and
   - `--allow-isolated-home` to opt into writing user-scoped state
     under the current isolated home on purpose. With this flag the
     isolated home itself is the target; the flow must never fall
     through to a real profile that another, unredirected variable
     still names.
   On native Windows the stage-2 installer has no guard of its own, so
   `install.cmd`'s policy helper applies one before stage 2 runs. It
   evaluates the home stage 2's `Path.home()` will use (`USERPROFILE`,
   then `HOMEDRIVE` plus `HOMEPATH`) and refuses when `HOME`,
   `USERPROFILE`, or `HOMEDRIVE`/`HOMEPATH` ends in one of the
   recognized isolated-home paths, or when `CLAUDE_LOCAL_MANAGED_BY` or
   `CODEX_LOCAL_MANAGED_BY` is set and `HOME`, `USERPROFILE`, or (when
   it is the effective home) `HOMEDRIVE`/`HOMEPATH` differs from the
   logged-on user's Windows profile (read from the logon token, not
   from `USERPROFILE`), which catches custom layouts. A `USERPROFILE`
   that is set but empty is refused outright, because `Path.home()`
   would turn it into the current directory. The elevated-run prompt
   names the same effective home. It consumes `--allow-isolated-home`
   and does not forward it to stage 2.
4. Scope preservation
   Existing install scopes remain:
   - `repo` for repo-local development state;
   - `user` for user-local standard installs; and
   - `system` for explicit system installs (POSIX only; native Windows
     supports the standard user-local mode only).
   This behavior clarifies which home the `user`-scoped portions of
   those flows will touch. It does not create a second
   runtime-selection system.
5. User-scoped path derivation
   Without `--user-home`, the default user scope is the first
   advertised home for the operating system: `HOME` on POSIX and
   `USERPROFILE` (then `HOMEDRIVE`/`HOMEPATH`, then `HOME`) on native
   Windows. When `--user-home PATH` is selected, installer code must
   derive user-local paths such as `~/.local/bin`,
   `~/.local/share/.../venv`, and `~/.pyenv` from that explicit target
   home instead of from the current process environment.
6. Documentation
   User-facing docs must explain the middle-ground trust model:
   repo-local work stays easy, isolated AI homes may be used
   deliberately, and operator-user installs require explicit targeting
   when the current session is already isolated.

## Validation

- Unit tests must cover explicit user-home resolution and
  isolated-home rejection for all three recognized isolated-home
  layouts, with the Windows and POSIX variable orders tested on any
  host through an injected environment and operating-system name.
- Policy-helper tests must cover the `install.cmd` refusals (including
  an isolated or redirected `HOMEDRIVE`/`HOMEPATH` with `USERPROFILE`
  unset, and an empty `USERPROFILE`) and the consumed
  `--allow-isolated-home` flag.
- Unit tests must cover another checkout's isolated home and a
  redirected launcher home inside a session, a network
  `HOMEDRIVE`/`HOMEPATH` beside a set `USERPROFILE`, and the POSIX
  account-home comparison.
- Help text and docs must mention `--user-home` and
  `--allow-isolated-home`.
- Installer success output must identify the selected user home and
  whether it came from the current process or an explicit override.
