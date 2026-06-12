"""Local developer tooling helpers for this repository."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence

DEFAULT_BLACK_TIMEOUT_SECONDS = 60
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 10
DEFAULT_BLACK_PROBE_TIMEOUT_SECONDS = 10
DEFAULT_TOOL_TIMEOUTS = {
    "ruff": 45,
    "compileall": 60,
    "entropy_check": 90,
    "entropy_tripwire_verify": 120,
    "pytest": 120,
}
FALLBACK_BLACK_PATHS = (
    "claude-wrangler.py",
    "src",
    "tests",
    "scripts",
)
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    # Claude session state and any coexisting codex-wrangler artifacts are
    # local operator state, never formatter or lint targets.
    ".local",
    ".codex-home",
    ".codex-local",
    "__pycache__",
}


def _print(message: str) -> None:
    print(message, flush=True)


def _run_capture(
    command: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: Optional[int] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _iter_python_files(root: Path) -> Iterable[Path]:
    if root.is_file() and root.suffix == ".py":
        yield root
        return

    if not root.is_dir():
        return

    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        yield path


def list_tracked_python_files(repo_root: Path) -> List[Path]:
    """Return tracked Python files under the repository root."""

    completed = None
    try:
        completed = _run_capture(
            ["git", "ls-files"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _print(
            "[black-wrapper] timed out listing tracked files after {}s; "
            "falling back to repository path scan.".format(
                DEFAULT_CAPTURE_TIMEOUT_SECONDS
            )
        )
    if completed and completed.returncode == 0:
        tracked = [
            repo_root / line
            for line in completed.stdout.splitlines()
            if line.endswith(".py")
        ]
        if tracked:
            return tracked

    fallback: List[Path] = []
    for raw_path in FALLBACK_BLACK_PATHS:
        fallback.extend(_iter_python_files(repo_root / raw_path))
    return _dedupe_paths(fallback)


def expand_black_targets(repo_root: Path, raw_targets: Sequence[str]) -> List[Path]:
    """Expand explicit formatter targets into concrete Python files."""

    if not raw_targets:
        return list_tracked_python_files(repo_root)

    expanded: List[Path] = []
    for raw_target in raw_targets:
        target = Path(raw_target)
        if not target.is_absolute():
            target = repo_root / target
        if not target.exists():
            raise FileNotFoundError("Black target does not exist: {}".format(target))
        expanded.extend(_iter_python_files(target))
    return _dedupe_paths(expanded)


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen = set()
    deduped: List[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(path)
    return deduped


def black_supports_flag(
    python_executable: str,
    repo_root: Path,
    flag: str,
) -> bool:
    """Return True when the installed Black advertises a given flag."""

    try:
        completed = _run_capture(
            [python_executable, "-m", "black", "--help"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_BLACK_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _print(
            "[black-wrapper] timed out probing Black help after {}s; "
            "treating {} as unsupported.".format(
                DEFAULT_BLACK_PROBE_TIMEOUT_SECONDS,
                flag,
            )
        )
        return False
    if completed.returncode != 0:
        _print(
            "[black-wrapper] Black help probe failed; treating {} as "
            "unsupported.".format(flag)
        )
        stderr = completed.stderr.strip()
        if stderr:
            _print("[black-wrapper] black --help stderr: {}".format(stderr))
        return False
    return flag in completed.stdout


def build_black_command(
    python_executable: str,
    target: Path,
    *,
    supports_no_cache: bool,
    check: bool,
    diff: bool,
) -> List[str]:
    """Build one Black command for one concrete target file."""

    command = [python_executable, "-m", "black"]
    if supports_no_cache:
        command.append("--no-cache")
    if check:
        command.append("--check")
    if diff:
        command.append("--diff")
    command.append(str(target))
    return command


def run_timed_command(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
    label: str,
) -> int:
    """Run one command with a timeout and operator-friendly logging."""

    pretty = " ".join(shlex.quote(part) for part in command)
    _print("[{}] running: {}".format(label, pretty))
    _print("[{}] timeout: {}s".format(label, timeout_seconds))
    env = os.environ.copy()
    src_dir = str(cwd / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_dir
        if not existing_pythonpath
        else src_dir + os.pathsep + existing_pythonpath
    )
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            check=False,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        print(
            "[{}] timeout exceeded after {}s.".format(label, timeout_seconds),
            file=sys.stderr,
        )
        return 124
    return completed.returncode


def run_black_serial(
    python_executable: str,
    repo_root: Path,
    targets: Sequence[Path],
    *,
    timeout_seconds: int,
    check: bool,
    diff: bool,
) -> int:
    """Run Black one file at a time to avoid sandbox hangs."""

    if not targets:
        print("[black-wrapper] no Python files matched the requested scope.")
        return 0

    supports_no_cache = black_supports_flag(
        python_executable,
        repo_root,
        "--no-cache",
    )
    _print(
        "[black-wrapper] serial mode across {} files; --no-cache {}.".format(
            len(targets),
            "enabled" if supports_no_cache else "unsupported by installed Black",
        )
    )

    first_failure = 0
    for index, target in enumerate(targets, start=1):
        relative = target.relative_to(repo_root)
        _print("[black-wrapper] ({}/{}) {}".format(index, len(targets), relative))
        exit_code = run_timed_command(
            build_black_command(
                python_executable,
                target,
                supports_no_cache=supports_no_cache,
                check=check,
                diff=diff,
            ),
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
            label="black-wrapper",
        )
        if exit_code != 0 and first_failure == 0:
            first_failure = exit_code
    return first_failure


def run_repo_quality_gate(
    python_executable: str,
    repo_root: Path,
    tools: Sequence[str],
    black_timeout_seconds: int,
) -> int:
    """Run the repository's local quality gate with repo-correct paths."""

    commands = {
        "ruff": [python_executable, "-m", "ruff", "check", "."],
        "compileall": [python_executable, "-m", "compileall", "src", "tests"],
        "entropy_check": [
            python_executable,
            "TheKnowledge/standards-and-practices/dev-utils/security/run_entropy_harness.py",
        ],
        "entropy_tripwire_verify": [
            python_executable,
            "TheKnowledge/standards-and-practices/dev-utils/security/verify_entropy_tripwire.py",
        ],
        "pytest": [python_executable, "-m", "pytest"],
    }

    for tool in tools:
        if tool == "black":
            exit_code = run_black_serial(
                python_executable,
                repo_root,
                expand_black_targets(repo_root, ()),
                timeout_seconds=black_timeout_seconds,
                check=False,
                diff=False,
            )
        else:
            exit_code = run_timed_command(
                commands[tool],
                cwd=repo_root,
                timeout_seconds=DEFAULT_TOOL_TIMEOUTS[tool],
                label=tool,
            )
        if exit_code != 0:
            return exit_code
    return 0


def main_run_black(
    repo_root: Path,
    argv: Optional[Sequence[str]] = None,
) -> int:
    """CLI entry point for the Black wrapper."""

    parser = argparse.ArgumentParser(
        description=(
            "Run Black safely in this repository by probing flag support and "
            "formatting one file at a time with timeout protection."
        )
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_BLACK_TIMEOUT_SECONDS,
        help="Per-file timeout in seconds.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run Black in check mode.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Show diffs for Black changes.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Optional files or directories to format. Defaults to tracked Python files.",
    )
    args = parser.parse_args(argv)

    targets = expand_black_targets(repo_root, args.targets)
    return run_black_serial(
        sys.executable,
        repo_root,
        targets,
        timeout_seconds=args.timeout_seconds,
        check=bool(args.check),
        diff=bool(args.diff),
    )


def main_run_quality_gate(
    repo_root: Path,
    argv: Optional[Sequence[str]] = None,
) -> int:
    """CLI entry point for the local repository quality gate."""

    valid_tools = {
        "black",
        "ruff",
        "compileall",
        "entropy_check",
        "entropy_tripwire_verify",
        "pytest",
    }
    parser = argparse.ArgumentParser(
        description=(
            "Run the repository's local quality gate using repo-correct paths "
            "and a Black wrapper that avoids sandbox hangs."
        )
    )
    parser.add_argument(
        "--black-timeout-seconds",
        type=int,
        default=DEFAULT_BLACK_TIMEOUT_SECONDS,
        help="Per-file timeout for the Black wrapper.",
    )
    parser.add_argument(
        "tools",
        nargs="*",
        help="Optional subset of tools to run.",
    )
    args = parser.parse_args(argv)

    if any(tool not in valid_tools for tool in args.tools):
        invalid = [tool for tool in args.tools if tool not in valid_tools]
        parser.error(
            "invalid tool selection: {} (choose from {})".format(
                ", ".join(invalid),
                ", ".join(sorted(valid_tools)),
            )
        )

    tools = list(args.tools) or [
        "black",
        "ruff",
        "compileall",
        "entropy_check",
        "entropy_tripwire_verify",
        "pytest",
    ]
    return run_repo_quality_gate(
        sys.executable,
        repo_root,
        tools,
        black_timeout_seconds=args.black_timeout_seconds,
    )
