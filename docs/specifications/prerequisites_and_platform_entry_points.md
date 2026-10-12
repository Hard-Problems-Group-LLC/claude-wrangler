# Prerequisites and Platform Entry Points

## Purpose

Give every supported operating system a working path from a fresh checkout
to an installed claude-wrangler, including installing whatever is missing,
while keeping TheKnowledge-managed starter files unmodified.

## Scope

The project-owned `scripts/install_prerequisites.*` family, the native
Windows `install.cmd` entry point and its policy helper, line-ending
attributes, and the user-facing claude-wrangler launcher on Windows.

## Ownership

`install.sh`, `bootstrap.sh`, `bootstrap-stage2.py`,
`scripts/install-stage-2.py`, `scripts/python_environment_bootstrap.py`,
`scripts/dev_setup.py`, and TheKnowledge's managed `.gitignore` block
are TheKnowledge starter files and are not edited for Windows support.
claude-wrangler's own marked `.gitignore` block is separate: it is owned
by claude-wrangler and governed by `managed_gitignore_enforcement.md`.
The files below are owned by this project and are written to be portable
to TheKnowledge's successor without depending on TheKnowledge internals.

## Required Behavior

1. **Prerequisite installer family**

   `scripts/install_prerequisites.py` follows the structure and function
   names of TheKnowledge's installer of the same name (`detect_os_info`,
   `select_package_manager`, `system_packages_for`,
   `build_install_commands`, `CommandRunner`, `ensure_virtualenv`, …) and
   extends it orthogonally:
   - it installs only prerequisites that are missing, after detecting
     each one;
   - prerequisites are Python 3.9+, Git, and, on Windows, Git for
     Windows' bash (which Claude Code's Bash tool uses; optional, on by
     default, `--no-git-bash` skips it); Node.js is not a prerequisite;
   - on macOS, `/usr/bin/git` is a Command Line Tools stub that exists
     but only opens the installer dialog, so it counts as installed only
     when `xcode-select -p` succeeds; otherwise Git is reported missing
     with `xcode-select --install` as the fix. Python is judged by
     running it, not by `xcode-select`: the wrappers' version probe
     rejects the `/usr/bin/python3` stub, and on macOS the shell
     wrapper's no-Python hint names `xcode-select --install` (which
     provides Apple's Python 3.9) before the package-manager command;
   - package managers: `apt-get`, `dnf`/`yum`, `brew`, and on Windows
     `winget` with `--scope user` where the package supports it;
   - `--dry-run` prints commands, says how many prerequisites it would
     install and that nothing was changed, never prints "Prerequisites
     ready." while something is missing, and exits 0 for the dry run
     itself; `--yes` runs without prompting; without `--yes` an
     interactive run asks once before installing and a non-interactive
     run refuses;
   - after a real install the prerequisites are detected again; winget
     changes only the registry `PATH`, so a tool the running process
     still cannot see gets "open a new terminal ... then rerun to
     confirm" instead of "ready";
   - `--dev` additionally creates the repo-local `.venv` (using
     `Scripts\python.exe` on Windows) and installs the project with
     development extras, so contributors on Windows have a development
     path while the managed `--mode dev` (pyenv and direnv) stays
     POSIX-only. The development extras need Python 3.12, so `--dev`
     uses the steady-state interpreter from the tool-validation profile
     and falls back to the running interpreter only when it is 3.12 or
     newer; otherwise it refuses before creating anything and names
     `--python`;
   - it never uses `sudo` unless it is already root or the operator passed
     `--system-packages`, and never elevates on Windows; when a package
     needs elevation it prints the exact command for the operator to run.

   Thin wrappers find an interpreter and delegate:
   - `scripts/install_prerequisites.sh` (POSIX), which finds its own
     directory with `CDPATH= cd --` so an exported `CDPATH` cannot
     redirect it;
   - `scripts/install_prerequisites.cmd` (Windows), which, when no Python
     3.9+ exists, offers to run `winget install --scope user` for Python
     itself and then re-runs in a new environment.

2. **Windows entry point**

   `install.cmd` at the repository root is the native-Windows peer of
   `install.sh`:
   - it finds Python 3.9+ by exit-code probes with cmd-native paths
     (`THEKNOWLEDGE_BOOTSTRAP_PYTHON`, then `py -3`, then each
     `python.exe` on `PATH`, then the default python.org locations),
     skips the Microsoft Store alias, and never captures an interpreter
     path as text;
   - it starts each `python.exe` candidate without `call`, which would
     re-parse the line, doubling carets and expanding `%` a second
     time, and so reject an interpreter under such a directory; only
     `py` and an override that is not a `.exe` use `call`;
   - it reads `PATH` only through delayed expansion, never with `%`,
     because cmd parses a whole line before evaluating its `if` and
     aborts the script when a parsed line passes 8,191 characters, even
     when an earlier candidate already won. It removes the double quotes
     first, so a quoted entry such as one under `Program Files (x86)`
     cannot end the `for` list, and it measures the quoted entry list
     before building it, because cmd crashes when a delayed replacement
     passes 8,191 characters. A `PATH` whose list would pass 8,000
     characters, or that is itself over 8,191 characters (delayed
     expansion yields nothing for it), is not scanned, and the error
     says so; `py -3`, the default locations, and
     `THEKNOWLEDGE_BOOTSTRAP_PYTHON` still work. Each entry is tested
     with delayed expansion off again, so entries containing `!` keep
     their spelling, and the setlocal scopes stay balanced for callers
     that use `call`;
   - when none is found it points to `scripts\install_prerequisites.cmd`;
   - it runs `scripts/install-stage-1-windows.py`, which applies policy
     and then runs the unchanged `scripts/install-stage-2.py` as a child,
     forwarding every argument and the exit code;
   - it keeps `%*` out of parenthesized blocks and `for` lists, uses no
     labels, is ASCII-only, and exits through
     `set "CMD_EXIT=%ERRORLEVEL%"` / `exit /b %CMD_EXIT%`.

3. **Windows policy helper**

   `scripts/install-stage-1-windows.py` (Python 3.9, standard library):
   - parses arguments with stage 2's own parser, loaded by path and
     registered in `sys.modules`, failing closed if the parser is missing;
   - supports `--mode standard` only; `--system` and other modes exit 1
     with a message pointing to `install_prerequisites.cmd --dev` for
     development or WSL2 for the managed POSIX modes;
   - refuses when `HOME`, `USERPROFILE`, or `HOMEDRIVE`/`HOMEPATH` is a
     repo-local isolated home or when `CLAUDE_LOCAL_MANAGED_BY` /
     `CODEX_LOCAL_MANAGED_BY` is set with a home that differs from the
     Windows profile, unless `--allow-isolated-home` is given (consumed,
     not forwarded). It evaluates the home stage 2's `Path.home()` will
     use (`USERPROFILE`, then `HOMEDRIVE` plus `HOMEPATH`), compares
     `HOMEDRIVE`/`HOMEPATH` with the profile only when it is that
     effective home, and refuses an empty `USERPROFILE` outright
     (`explicit_user_home_targeting_for_installers.md`);
   - on an elevated token, prompts on a console and refuses without one
     unless `THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED=1`;
   - sets `THEKNOWLEDGE_MANAGED_INSTALL_STAGE1=1` only in the child's
     environment, waits for the child, and maps `0xC000013A` to 130.

4. **Help parity**

   `install.cmd --help`, `-h`, and `/?` print a Windows usage block plus
   stage 2's options and exit 0; with no Python they print a short usage
   block and exit 0.

5. **User launcher on Windows**

   Standard mode installs the claude-wrangler console launcher into the
   user bin directory as a copied `claude-wrangler.exe` on Windows (pip
   console launchers embed an absolute interpreter path), and as a
   symlink elsewhere. Replacing a running launcher renames it aside
   first. If the user bin directory is not on `PATH`, the installer prints
   the command to add it and never edits `PATH` itself.

6. **Line endings**

   `.gitattributes` pins `*.cmd` and `*.bat` to CRLF. Shell scripts and
   Python files are not pinned locally, because TheKnowledge's drift
   report compares managed files byte for byte; the generated files that
   POSIX shells read are written with explicit LF by claude-wrangler.

7. **TheKnowledge maintenance on Windows checkouts**

   `AGENTS.md` must warn that `update_theknowledge_submodule.py` and
   `initial-setup --force` must not run from a CRLF Windows checkout,
   because the forced refresh rewrites `.gitignore` with `\r\r\n` line
   endings and stops ignoring `.codex-home/`, `.codex-local/`, and the
   restricted-names file. A tripwire test asserts the `.gitignore` has no
   `\r\r\n` sequences and that those paths stay ignored.

## Support Matrix

| Mode | Linux | macOS | Windows |
|---|---|---|---|
| `install.sh` standard | yes | yes | Git Bash only, unguarded |
| `install.sh --mode dev` / `venv-only` | yes | yes | no (use WSL2) |
| `install.cmd` standard | — | — | yes |
| `install_prerequisites --dev` | yes | yes | yes |
| Project-local Claude runtime and launcher | yes | yes | yes |

## Validation

- Policy-helper tests on any OS with injected host facts: refusals,
  isolated-home detection, elevation handling, help, forwarding.
- Windows-only tests that run `install.cmd` from a copied fake checkout
  under paths with spaces and non-ASCII characters, through cmd,
  PowerShell 5.1, PowerShell 7, and Git Bash, with a stub-only `PATH`,
  a stub-first `PATH`, and an invalid override; with a quoted `PATH`
  entry containing parentheses (with and without an override) and a
  quoted entry holding Python; and with interpreters under directories
  containing a caret or a percent-variable token, found through `PATH`
  and through the override. The same tests cover `PATH` values past
  cmd's limits (in the band where only the quoted list passes 8,191
  characters, and well beyond it) with the override, the default
  locations, and no Python at all; a 7,000-character `PATH` that is
  still scanned to its end; entries containing `!`; and a caller using
  `call` that must see none of the scripts' variables afterwards.
- Prerequisite-installer tests for command construction on every package
  manager, missing-only detection (including the macOS Command Line
  Tools stubs), `--dry-run`, the consent rules, `--dev`
  virtual-environment paths, and the `--dev` interpreter floor.
- The `.gitignore` tripwire test.
