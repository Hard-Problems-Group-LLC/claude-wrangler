#!/usr/bin/env python3
"""Native-Windows stage 1 for the managed installer: policy gate, then stage 2.

`install.cmd` has already chosen a native Python 3.9+ interpreter and runs
this file with it. This file is the Windows peer of `install.sh`'s policy
layer: it reads the request with stage 2's own argument parser (so option
abbreviations behave exactly as stage 2 reads them), refuses requests that
native Windows cannot honor, and then runs the unchanged
`scripts/install-stage-2.py` as a child process with every argument
forwarded and `THEKNOWLEDGE_MANAGED_INSTALL_STAGE1=1` set in the child's
environment only.

Windows has no `exec()`: `os.execv` starts a new process and the parent
exits 0 at once, so this helper waits for the child and returns its exit
code, mapping the console Ctrl+C status `0xC000013A` to 130.

Candidate TheKnowledge starter file: keep it project-neutral (apart from
the isolated-home suffixes that the installer specs require), standard
library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import importlib.util
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
from types import ModuleType
from typing import Callable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
STAGE2 = SCRIPT_DIR / "install-stage-2.py"
STAGE2_MODULE_NAME = "_managed_install_stage_2"
MARKER = "THEKNOWLEDGE_MANAGED_INSTALL_STAGE1"
ALLOW_ELEVATED_ENV = "THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED"
ALLOW_ISOLATED_HOME_FLAG = "--allow-isolated-home"
# Set by the generated claude-local and codex-local launchers in the
# sessions they start.
MANAGED_BY_VARIABLES = ("CLAUDE_LOCAL_MANAGED_BY", "CODEX_LOCAL_MANAGED_BY")
HELP_TOKENS = ("-h", "--help", "/?")
# Used until stage 2 exports NATIVE_WINDOWS_SUPPORTED_MODES itself.
FALLBACK_NATIVE_WINDOWS_MODES = ("standard",)
# Repo-local isolated homes (lower-cased path tails) that a user-scoped
# install must not silently target: the claude-local redirected home and
# the current and legacy codex-local homes.
ISOLATED_HOME_TAILS = (
    (".local", "claude", "home"),
    (".local", "codex-home"),
    (".codex-home",),
)
STATUS_CONTROL_C_EXIT = 0xC000013A
# The same NTSTATUS seen as a signed 32-bit exit code.
STATUS_CONTROL_C_EXIT_SIGNED = STATUS_CONTROL_C_EXIT - (1 << 32)
CTRL_C_EXIT_CODE = 130
DEVELOPMENT_HINT = (
    "For a development checkout on native Windows, run "
    "scripts\\install_prerequisites.cmd --dev; for the managed POSIX modes "
    "(pyenv and direnv), run ./install.sh from a Linux or WSL2 checkout."
)
USAGE = """\
Usage: install.cmd [stage-2-options]
       py -3 scripts\\install-stage-1-windows.py [stage-2-options]

Install or bootstrap from this checkout.

Default behavior:
  - normal user: user-local non-development install
  - --mode dev, --mode venv-only: not supported on native Windows; use
    scripts\\install_prerequisites.cmd --dev for a development checkout,
    or ./install.sh from a Linux or WSL2 checkout
  - --system: not supported on native Windows

Options:
  -h, --help, /?         Show this help, then the current stage-2 options.
  --allow-isolated-home  Install even though HOME or USERPROFILE is a
                         repo-local isolated home (consumed here, not
                         passed to stage 2).

Environment:
  THEKNOWLEDGE_BOOTSTRAP_PYTHON       Interpreter install.cmd must use.
  THEKNOWLEDGE_INSTALL_ALLOW_ELEVATED=1
                                      Allow an elevated run without a
                                      console prompt, for CI.

All remaining options are passed through to scripts/install-stage-2.py.
"""


class Stage2Error(RuntimeError):
    """Stage 2 is missing or no longer exposes the interface this gate needs."""


class Host(NamedTuple):
    """Host facts the policy reads; tests pass fakes instead of probing."""

    windows: bool
    elevated: bool
    interactive: bool
    env: Mapping[str, str]
    # The Windows profile recorded for this logon token. Unlike
    # Path.home() it ignores a redirected USERPROFILE.
    profile: Path


def windows_profile_directory() -> Optional[Path]:
    """Return the profile directory recorded for the current token, or None.

    `GetUserProfileDirectoryW` reads the profile from the logon token and
    the registry, so it still names the operator's real profile when a
    launcher has redirected `USERPROFILE` into a repository.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        userenv.GetUserProfileDirectoryW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        token_query = 0x0008
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD(0)
            # The first call only reports the buffer size it needs.
            userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
            if size.value == 0:
                return None
            buffer = ctypes.create_unicode_buffer(size.value)
            if not userenv.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, ValueError):
        return None


def detect_elevated() -> bool:
    """Return True when this process runs with an elevated Windows token."""

    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def stdin_is_console() -> bool:
    """Return True when stdin is an interactive console."""

    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def detect_host() -> Host:
    """Probe the real host without side effects."""

    profile = windows_profile_directory()
    if profile is None:
        profile = Path.home()
    return Host(
        windows=os.name == "nt",
        elevated=detect_elevated(),
        interactive=stdin_is_console(),
        env=os.environ,
        profile=profile,
    )


def load_stage2(stage2: Path = STAGE2) -> ModuleType:
    """Import stage 2 by path so policy uses stage 2's own parser.

    The module is registered in `sys.modules` before it executes, as the
    import system would, so decorators such as `dataclass` that look the
    module up keep working if stage 2 grows them. Any failure is reported
    as `Stage2Error` so the caller fails closed.
    """

    if not stage2.is_file():
        raise Stage2Error("{} does not exist".format(stage2))
    spec = importlib.util.spec_from_file_location(STAGE2_MODULE_NAME, str(stage2))
    if spec is None or spec.loader is None:
        raise Stage2Error("cannot load {}".format(stage2))
    module = importlib.util.module_from_spec(spec)
    sys.modules[STAGE2_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as error:
        # A stage 2 that raises, or even exits, while importing has changed
        # its interface; treat both as a load failure.
        sys.modules.pop(STAGE2_MODULE_NAME, None)
        raise Stage2Error("loading {} failed: {!r}".format(stage2, error)) from error
    if not callable(getattr(module, "parse_args", None)):
        raise Stage2Error("{} has no parse_args(); refusing to guess".format(stage2))
    return module


def supported_native_modes(module: ModuleType) -> Tuple[str, ...]:
    """Return the install modes stage 2 declares safe on native Windows."""

    declared = getattr(module, "NATIVE_WINDOWS_SUPPORTED_MODES", None)
    if isinstance(declared, (tuple, list)) and all(
        isinstance(mode, str) for mode in declared
    ):
        return tuple(declared)
    return FALLBACK_NATIVE_WINDOWS_MODES


def split_gate_arguments(argv: Sequence[str]) -> Tuple[List[str], bool]:
    """Remove the helper's own flags; return (stage-2 argv, allow isolated).

    Only the exact `--allow-isolated-home` spelling is consumed, so an
    abbreviation still reaches stage 2's parser and fails there visibly.
    """

    forwarded: List[str] = []
    allow_isolated_home = False
    for token in argv:
        if token == ALLOW_ISOLATED_HOME_FLAG:
            allow_isolated_home = True
            continue
        forwarded.append(token)
    return forwarded, allow_isolated_home


def wants_help(argv: Sequence[str]) -> bool:
    """Return True when any argument asks for help, as install.sh checks."""

    return any(token in HELP_TOKENS for token in argv)


def windows_path_text(value: str) -> str:
    """Return a Windows spelling of a path, converting MSYS `/c/...` forms."""

    match = re.match(r"^/([A-Za-z])(?:/|$)", value)
    if match:
        rest = value[len(match.group(0)) :]
        return "{}:\\{}".format(match.group(1).upper(), rest.replace("/", "\\"))
    return value


def comparable_windows_path(value: str) -> str:
    """Return a case-folded, normalized Windows path for equality checks."""

    normalized = ntpath.normpath(windows_path_text(value))
    return ntpath.normcase(normalized).rstrip("\\")


def isolated_home_hit(env: Mapping[str, str]) -> Optional[Tuple[str, str]]:
    """Return (variable, value) when HOME or USERPROFILE is an isolated home."""

    for name in ("HOME", "USERPROFILE"):
        value = env.get(name)
        if not value:
            continue
        parts = tuple(
            part.lower() for part in PureWindowsPath(windows_path_text(value)).parts
        )
        for tail in ISOLATED_HOME_TAILS:
            if len(parts) > len(tail) and parts[-len(tail) :] == tail:
                return name, value
    return None


def managed_home_mismatch(
    env: Mapping[str, str], profile: Path
) -> Optional[Tuple[str, str, str]]:
    """Return (marker, variable, value) for a launcher-redirected home.

    Inside a claude-local or codex-local session (marker variable set), a
    HOME or USERPROFILE that differs from the Windows profile means the
    session redirected the home, possibly to a custom layout that the tail
    check cannot recognize. Ring-0 sessions keep the real profile and pass.
    """

    marker = next((name for name in MANAGED_BY_VARIABLES if env.get(name)), None)
    if marker is None:
        return None
    expected = comparable_windows_path(str(profile))
    for name in ("HOME", "USERPROFILE"):
        value = env.get(name)
        if value and comparable_windows_path(value) != expected:
            return marker, name, value
    return None


def policy_refusal(
    args: object,
    supported_modes: Sequence[str],
    host: Host,
    *,
    allow_isolated_home: bool = False,
) -> Optional[str]:
    """Return why native Windows must not run this request, or None."""

    if getattr(args, "system", False):
        return (
            "--system is not supported on native Windows; omit it for a "
            "user-local install. " + DEVELOPMENT_HINT
        )
    mode = getattr(args, "mode", None)
    if mode not in supported_modes:
        return (
            "--mode {} is not supported on native Windows (stage 2 provisions "
            "POSIX pyenv and direnv). {}".format(mode, DEVELOPMENT_HINT)
        )
    if allow_isolated_home:
        return None
    hit = isolated_home_hit(host.env)
    if hit is not None:
        return (
            "{}={} is a repo-local isolated home. Rerun from a normal terminal, "
            "or pass {} to install there on purpose.".format(
                hit[0], hit[1], ALLOW_ISOLATED_HOME_FLAG
            )
        )
    mismatch = managed_home_mismatch(host.env, host.profile)
    if mismatch is not None:
        return (
            "{} is set and {}={} differs from the Windows profile {}, so this "
            "looks like a redirected launcher session. Rerun from a normal "
            "terminal, or pass {} to install there on purpose.".format(
                mismatch[0],
                mismatch[1],
                mismatch[2],
                host.profile,
                ALLOW_ISOLATED_HOME_FLAG,
            )
        )
    return None


def install_target_home(host: Host) -> str:
    """Return the home stage 2 will install under (Path.home() on Windows)."""

    return host.env.get("USERPROFILE") or str(host.profile)


def confirm_elevated(host: Host, ask: Callable[[str], str] = input) -> bool:
    """Mirror install.sh's root rule: prompt on a console, refuse without one."""

    if not host.elevated or host.env.get(ALLOW_ELEVATED_ENV) == "1":
        return True
    if not host.interactive:
        print(
            "install: this run is elevated (Administrator) and has no console "
            "to confirm it. Rerun from a normal terminal, or set {}=1 for "
            "unattended CI.".format(ALLOW_ELEVATED_ENV),
            file=sys.stderr,
        )
        return False
    try:
        answer = ask(
            "install: this shell is elevated (Administrator); the user-local "
            "install will target {}. Continue? [y/N] ".format(install_target_home(host))
        )
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    print(
        "install: aborted the elevated install. Rerun from a normal terminal.",
        file=sys.stderr,
    )
    return False


def map_exit_code(code: int) -> int:
    """Map the Windows console Ctrl+C status to the POSIX-style 130."""

    if code in (STATUS_CONTROL_C_EXIT, STATUS_CONTROL_C_EXIT_SIGNED):
        return CTRL_C_EXIT_CODE
    return code


def run_stage2(argv: Sequence[str], stage2: Path = STAGE2) -> int:
    """Run stage 2 as a child with the marker set only there; return its code."""

    env = dict(os.environ)
    env[MARKER] = "1"
    child = subprocess.Popen([sys.executable, str(stage2)] + list(argv), env=env)
    while True:
        try:
            code = child.wait()
            break
        except KeyboardInterrupt:
            # The child received the same console Ctrl+C. Keep waiting so
            # it is never orphaned in the middle of a write.
            continue
    return map_exit_code(code)


def show_stage2_help(stage2: Path = STAGE2) -> int:
    """Print stage 2's own option help; the stage-1 marker stays unset."""

    try:
        return subprocess.call([sys.executable, str(stage2), "--help"])
    except OSError as error:
        print("install: cannot run {}: {}".format(stage2, error), file=sys.stderr)
        return 1


def parse_stage2_arguments(module: ModuleType, argv: Sequence[str], stage2: Path):
    """Parse argv with stage 2's parser so errors name stage 2, as it would."""

    saved_argv0 = sys.argv[0] if sys.argv else ""
    if sys.argv:
        sys.argv[0] = str(stage2)
    else:
        sys.argv.append(str(stage2))
    try:
        return module.parse_args(list(argv))
    finally:
        sys.argv[0] = saved_argv0


def main(
    argv: Optional[Sequence[str]] = None,
    host: Optional[Host] = None,
    stage2: Path = STAGE2,
    run: Callable[[Sequence[str], Path], int] = run_stage2,
    show_help: Callable[[Path], int] = show_stage2_help,
    ask: Callable[[str], str] = input,
) -> int:
    """Apply the native-Windows gate, then hand every argument to stage 2."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    host = host if host is not None else detect_host()
    if not host.windows:
        print(
            "install-stage-1-windows.py: native Windows only; use ./install.sh.",
            file=sys.stderr,
        )
        return 1
    if sys.version_info < (3, 9):
        # Direct `py -3 scripts\install-stage-1-windows.py` runs skip
        # install.cmd's version probe.
        print(
            "install: Python 3.9+ is required, found {}.".format(
                sys.version.split()[0]
            ),
            file=sys.stderr,
        )
        return 1
    if wants_help(raw_argv):
        print(USAGE + "\nStage-2 options:\n", flush=True)
        if show_help(stage2) != 0:
            print(
                "\nStage-2 options are unavailable: {} did not run.".format(stage2),
                flush=True,
            )
        return 0

    forwarded, allow_isolated_home = split_gate_arguments(raw_argv)
    try:
        module = load_stage2(stage2)
    except Exception as error:  # noqa: BLE001 - any failure must fail closed.
        print(
            "install: cannot evaluate the stage-2 options ({}); refusing.".format(
                error
            ),
            file=sys.stderr,
        )
        return 1
    try:
        args = parse_stage2_arguments(module, forwarded, stage2)
    except SystemExit as exit_request:
        # argparse already printed its usage error; keep its exit status.
        code = exit_request.code
        return code if isinstance(code, int) else 2
    except Exception as error:  # noqa: BLE001 - an interface change fails closed.
        print(
            "install: stage 2 could not parse the options ({}); refusing.".format(
                error
            ),
            file=sys.stderr,
        )
        return 1

    refusal = policy_refusal(
        args,
        supported_native_modes(module),
        host,
        allow_isolated_home=allow_isolated_home,
    )
    if refusal is not None:
        print("install: " + refusal, file=sys.stderr)
        return 1
    try:
        if not confirm_elevated(host, ask):
            return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return CTRL_C_EXIT_CODE

    home = host.env.get("HOME")
    if home and comparable_windows_path(home) != comparable_windows_path(
        install_target_home(host)
    ):
        print(
            "install: note: HOME={} differs from USERPROFILE; native Windows "
            "user-local paths go under {}.".format(home, install_target_home(host)),
            file=sys.stderr,
        )
    return run(forwarded, stage2)


if __name__ == "__main__":
    raise SystemExit(main())
