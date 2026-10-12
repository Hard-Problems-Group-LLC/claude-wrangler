"""Injectable side-effect services for maintenance operations.

Every operation that touches the network, runs a candidate binary, looks at
the process table, asks git which files it tracks, or reads the clock goes
through one `Services` object, so tests can replace each effect with an
offline fake while the slot store, promotion, metadata, and generated files
stay real.

The default implementations here also own two pieces of policy from
`native_runtime_distribution_and_verification.md`:

* the transaction scratch environment (requirement 10): every home-like
  variable, `CLAUDE_CONFIG_DIR`, `XDG_*`, and `TMP`/`TEMP`/`TMPDIR` point
  into scratch, the self-update suppression variables are set, and nothing
  that names the operator's home or the project's config dir survives; and
* the smoke test (`--version` must print exactly `<version> (Claude Code)`
  within 60 seconds), the `--help` subcommand reader the completion
  record's `subcommands` list comes from, and the probe that records which
  of those subcommands refuse a leading `--settings`
  (`cross_platform_launcher.md` requirement 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .constants import (
    DEEP_LINK_SETTING_KEY,
    DEEP_LINK_SETTING_VALUE,
    HELP_READ_TIMEOUT_SECONDS,
    SELF_UPDATE_SUPPRESSION_ENV,
    SMOKE_TEST_TIMEOUT_SECONDS,
)
from .distribution import (
    ManifestBundle,
    ReleaseSource,
    load_manifest_bundle,
    release_hosts,
)
from .download import HttpsFetcher, stderr_progress
from .models import ClaudeWranglerError
from .platforms import check_executable_structure, resolve_platform_key
from .runtime import run_bounded
from .slots import (
    RunningProcess,
    ServiceStopResult,
    find_processes_under,
    stop_background_service,
)

# Variables that name a home, a per-user store, the Claude config dir, or a
# temporary directory; all of them are replaced in the scratch environment.
SCRATCH_REPLACED_VARIABLES = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOMESHARE",
    "APPDATA",
    "LOCALAPPDATA",
    "CLAUDE_CONFIG_DIR",
    "TMP",
    "TEMP",
    "TMPDIR",
)
# Name prefixes dropped entirely: XDG base directories are re-pointed, and
# claude-local / Claude Code session markers (which carry the project's
# config-dir spelling when claude-wrangler runs inside a session) never
# reach a candidate binary.
SCRATCH_DROPPED_PREFIXES = ("XDG_", "CLAUDE", "ANTHROPIC")

_SUBCOMMAND_RE = re.compile(r"[a-z][a-z0-9-]*")
# The `--settings` acceptance probe (cross_platform_launcher.md requirement
# 4) gets one overall budget; the subcommands the launcher refuses outright
# are never run.
SETTINGS_PROBE_BUDGET_SECONDS = 120.0
NEVER_PROBED_SUBCOMMANDS = ("update", "upgrade", "install")


class SmokeTestError(ClaudeWranglerError):
    """Raised when a candidate binary fails its smoke test or help read."""


def host_os_family(platform_name: Optional[str] = None) -> str:
    """Return the platform-key prefix for this host: win32, darwin, or linux."""

    name = sys.platform if platform_name is None else platform_name
    if name == "win32":
        return "win32"
    if name == "darwin":
        return "darwin"
    if name.startswith("linux"):
        return "linux"
    return name


def _is_under(value: str, roots: Sequence[str], nt: bool) -> bool:
    """Return True when a path spelling is one of ``roots`` or inside one."""

    def comparable(text: str) -> str:
        """Normalize a spelling for a separator-bounded prefix comparison."""

        text = text.strip().strip('"')
        if nt:
            return ntpath.normcase(ntpath.normpath(text.replace("/", "\\")))
        return os.path.normpath(text) if text else text

    candidate = comparable(value)
    if not candidate:
        return False
    separator = "\\" if nt else "/"
    for root in roots:
        base = comparable(root)
        if not base:
            continue
        if candidate == base or candidate.startswith(
            base.rstrip(separator) + separator
        ):
            return True
    return False


def scratch_environment(
    scratch: Path,
    base: Mapping[str, str],
    *,
    forbidden_roots: Iterable[str] = (),
    os_name: Optional[str] = None,
) -> Dict[str, str]:
    """Return the environment for running a candidate binary in scratch.

    Every home-like variable, `CLAUDE_CONFIG_DIR`, the XDG base and runtime
    directories, and `TMP`/`TEMP`/`TMPDIR` point into ``scratch`` (whose
    directories are created). Claude and claude-local session variables are
    dropped, `PATH` entries under ``forbidden_roots`` (the operator's home
    and the project's config dir) are removed, and any other variable whose
    value lies under a forbidden root is dropped. The self-update
    suppression variables are set.
    """

    nt = (os.name if os_name is None else os_name) == "nt"
    roots = [str(root) for root in forbidden_roots if str(root)]
    replaced = {name.upper() for name in SCRATCH_REPLACED_VARIABLES}
    env: Dict[str, str] = {}
    for key, value in base.items():
        upper = key.upper()
        if upper in replaced or upper.startswith(SCRATCH_DROPPED_PREFIXES):
            continue
        if upper == "PATH":
            separator = ";" if nt else ":"
            kept = [
                entry
                for entry in value.split(separator)
                if entry and not _is_under(entry, roots, nt)
            ]
            env[key] = separator.join(kept)
            continue
        if roots and _is_under(value, roots, nt):
            continue
        env[key] = value

    home = scratch / "home"
    directories = {
        "home": home,
        "config": scratch / "config",
        "tmp": scratch / "tmp",
        "appdata": home / "AppData" / "Roaming",
        "localappdata": home / "AppData" / "Local",
        "xdg_config": home / ".config",
        "xdg_cache": home / ".cache",
        "xdg_state": home / ".local" / "state",
        "xdg_data": home / ".local" / "share",
        "xdg_runtime": scratch / "xdg-runtime",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    if not nt:
        os.chmod(str(directories["xdg_runtime"]), 0o700)

    env["HOME"] = str(home)
    if nt:
        drive, tail = ntpath.splitdrive(str(home))
        env["USERPROFILE"] = str(home)
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
        env["APPDATA"] = str(directories["appdata"])
        env["LOCALAPPDATA"] = str(directories["localappdata"])
    env["XDG_CONFIG_HOME"] = str(directories["xdg_config"])
    env["XDG_CACHE_HOME"] = str(directories["xdg_cache"])
    env["XDG_STATE_HOME"] = str(directories["xdg_state"])
    env["XDG_DATA_HOME"] = str(directories["xdg_data"])
    env["XDG_RUNTIME_DIR"] = str(directories["xdg_runtime"])
    env["CLAUDE_CONFIG_DIR"] = str(directories["config"])
    for name in ("TMP", "TEMP", "TMPDIR"):
        env[name] = str(directories["tmp"])
    for name, value in SELF_UPDATE_SUPPRESSION_ENV:
        env[name] = value
    return env


def expected_version_output(version: str) -> str:
    """Return the exact `--version` line a release must print."""

    return "{} (Claude Code)".format(version)


def run_smoke_test(
    binary: Path,
    version: str,
    env: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float = SMOKE_TEST_TIMEOUT_SECONDS,
) -> None:
    """Run `<binary> --version` and require exactly `<version> (Claude Code)`."""

    completed = run_bounded(
        [str(binary), "--version"],
        env=env,
        cwd=str(cwd),
        timeout_seconds=timeout_seconds,
    )
    if completed.timed_out:
        raise SmokeTestError(
            "{} --version did not finish within {:g} seconds (first-run antivirus "
            "scans can be slow; retry).".format(binary, timeout_seconds)
        )
    output = completed.stdout.rstrip("\r\n")
    expected = expected_version_output(version)
    if completed.returncode != 0 or output != expected:
        detail = " ".join((completed.stdout + " " + completed.stderr).split())[:300]
        raise SmokeTestError(
            "Smoke test failed: {} --version exited {} and printed {!r}; expected "
            "exactly {!r}.".format(
                binary, completed.returncode, detail or "<nothing>", expected
            )
        )


def parse_help_subcommands(text: str) -> List[str]:
    """Return subcommand names and aliases from Claude Code's `--help` text.

    Only the `Commands:` section counts. Each entry starts two spaces in;
    its first word is the name, with aliases joined by `|` (for example
    `plugin|plugins`). Indented continuation lines are skipped, and the
    section ends at the next unindented line.
    """

    names: List[str] = []
    in_commands = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_commands:
            if stripped == "Commands:" and not line.startswith(" "):
                in_commands = True
            continue
        if not stripped:
            continue
        if not line.startswith(" "):
            break
        indent = len(line) - len(line.lstrip(" "))
        if indent != 2:
            continue
        first = stripped.split()[0]
        for name in first.split("|"):
            if _SUBCOMMAND_RE.fullmatch(name) and name not in names:
                names.append(name)
    return names


def read_help_subcommands(
    binary: Path,
    env: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float = HELP_READ_TIMEOUT_SECONDS,
) -> List[str]:
    """Run `<binary> --help` in scratch and return its subcommand names."""

    completed = run_bounded(
        [str(binary), "--help"],
        env=env,
        cwd=str(cwd),
        timeout_seconds=timeout_seconds,
    )
    if completed.timed_out or completed.returncode != 0:
        raise SmokeTestError(
            "{} --help {} ; cannot record its subcommands.".format(
                binary,
                (
                    "timed out"
                    if completed.timed_out
                    else "exited {}".format(completed.returncode)
                ),
            )
        )
    return parse_help_subcommands(completed.stdout)


def settings_probe_document() -> str:
    """Return the compact `--settings` JSON the launcher passes to Claude Code.

    It matches `rendering.settings_override()` serialized the way the
    launcher core does, so the install-time probe tests the exact argument
    a launch will use.
    """

    document = {
        "env": {name: value for name, value in SELF_UPDATE_SUPPRESSION_ENV},
        DEEP_LINK_SETTING_KEY: DEEP_LINK_SETTING_VALUE,
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def _help_names_subcommand(text: str, name: str) -> bool:
    """Return whether help output is ``name``'s own usage, not the root's."""

    for line in text.splitlines():
        words = line.split()
        if words and words[0] == "Usage:":
            return len(words) >= 3 and name in words[2].split("|")
    return False


def probe_settings_rejections(
    binary: Path,
    env: Mapping[str, str],
    cwd: Path,
    subcommands: Sequence[str],
    timeout_seconds: float = HELP_READ_TIMEOUT_SECONDS,
    budget_seconds: float = SETTINGS_PROBE_BUDGET_SECONDS,
) -> List[str]:
    """Return the recorded subcommands that refuse a leading `--settings`.

    The launcher puts `--settings <json>` ahead of every invocation and
    omits it only for subcommands that reject it (cross_platform_launcher.md
    requirement 4). Each subcommand is probed in transaction scratch as
    `<binary> --settings <json> <subcommand> --help`; it accepts the flag
    when the probe exits 0 and prints that subcommand's own usage line.
    An option error, the root help, a timeout, or an exhausted overall
    budget counts as a rejection, so the launcher omits `--settings` and
    the subcommand keeps working; the environment and config-dir layers
    still apply. `update`, `upgrade`, and `install` are never run here:
    the launcher refuses them before any argument is built.
    """

    document = settings_probe_document()
    deadline = time.monotonic() + budget_seconds
    rejected: List[str] = []
    for name in subcommands:
        if name in NEVER_PROBED_SUBCOMMANDS:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            rejected.append(name)
            continue
        completed = run_bounded(
            [str(binary), "--settings", document, name, "--help"],
            env=env,
            cwd=str(cwd),
            timeout_seconds=min(timeout_seconds, remaining),
        )
        if (
            completed.timed_out
            or completed.returncode != 0
            or not _help_names_subcommand(completed.stdout, name)
        ):
            rejected.append(name)
    return rejected


GIT_LS_FILES_TIMEOUT_SECONDS = 30.0
# Environment switches that change how git reads every pathspec.
GIT_PATHSPEC_VARIABLES = (
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)


def filesystem_ignores_case(project_root: Path) -> bool:
    """Return True when the project's filesystem treats names case-insensitively.

    Windows and macOS default to case-insensitive volumes. Elsewhere the
    project root's `.git` entry is looked up under a case-swapped spelling;
    finding the same entry means names differing only in case collide.
    """

    if os.name == "nt" or sys.platform == "darwin":
        return True
    marker = Path(project_root) / ".git"
    swapped = Path(project_root) / ".GIT"
    try:
        return os.path.samefile(str(marker), str(swapped))
    except OSError:
        return False


def list_tracked_files(
    project_root: Path,
    paths: Sequence[Path],
    *,
    ignore_case: Optional[bool] = None,
) -> List[str]:
    """Return the files git tracks under ``paths`` (relative to the project).

    Runs `git -C <project root> ls-files -z -- <pathspecs>` with
    `core.fsmonitor` switched off, so a `.git/config` that came with an
    untrusted checkout cannot make the query run a command. Each pathspec
    carries `literal` magic, so wildcards in managed paths match only
    themselves. On a case-insensitive filesystem (``ignore_case``, detected
    by `filesystem_ignores_case` when None) the pathspecs also carry
    `icase`: there a tracked `.LOCAL/claude/...` is the same file as the
    managed `.local/claude/...`, while git compares pathspecs case-sensitively
    even with `core.ignorecase`. When git is missing, the project is not
    inside a work tree, or git fails, nothing can be tracked there and the
    result is empty; a timeout is reported and treated the same way.
    """

    relative: List[str] = []
    for path in paths:
        try:
            spelled = os.path.relpath(str(path), str(project_root))
        except ValueError:
            continue
        if spelled == os.curdir or spelled.split(os.sep)[0] == os.pardir:
            continue
        relative.append(spelled.replace(os.sep, "/"))
    if not relative:
        return []
    if ignore_case is None:
        ignore_case = filesystem_ignores_case(project_root)
    magic = ":(literal,icase)" if ignore_case else ":(literal)"
    # Per-pathspec magic instead of --literal-pathspecs, which would read the
    # magic prefix itself as part of a literal path. The global pathspec
    # variables are dropped for the same reason: GIT_LITERAL_PATHSPECS would
    # make every pathspec match nothing and silently pass the refusal.
    command = ["git", "-c", "core.fsmonitor=false"]
    command += ["-C", str(project_root), "ls-files", "-z", "--"]
    command += [magic + item for item in relative]
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in GIT_PATHSPEC_VARIABLES
    }
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=GIT_LS_FILES_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            "[claude-wrangler] Warning: `git ls-files` did not finish within {:g} "
            "seconds; could not check whether git tracks managed files.".format(
                GIT_LS_FILES_TIMEOUT_SECONDS
            ),
            file=sys.stderr,
            flush=True,
        )
        return []
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    names = completed.stdout.decode("utf-8", "replace").split("\0")
    return sorted({name for name in names if name})


def default_release_source(
    base_url: str, timeout_seconds: float, ca_bundle: Optional[str]
) -> ReleaseSource:
    """Build the real release source with a fetcher allowlisted for base_url.

    Constructing the fetcher validates `--ca-bundle`/`SSL_CERT_FILE`, so
    callers build the source before taking any irreversible step.
    """

    fetcher = HttpsFetcher(
        release_hosts(base_url),
        timeout_seconds=float(timeout_seconds),
        ca_bundle=ca_bundle,
    )
    return ReleaseSource(base_url, fetcher=fetcher)


def _utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""

    return datetime.now(timezone.utc)


@dataclass
class Services:
    """Side-effect services used by maintenance operations.

    * ``release_source(base_url, timeout_seconds, ca_bundle)`` returns an
      object with ``channel_version``, ``fetch_manifest_bundle``,
      ``artifact_url``, and a ``fetcher`` that has ``download_to``.
    * ``verify_evidence(slot_dir, version)`` re-verifies retained evidence
      offline and returns a ``ManifestBundle``.
    * ``check_structure(path, platform_key)`` raises on a malformed binary.
    * ``smoke_runner(binary, version, env, cwd)`` raises unless
      ``--version`` prints exactly ``<version> (Claude Code)``.
    * ``help_reader(binary, env, cwd)`` returns the subcommand names.
    * ``settings_probe(binary, env, cwd, subcommands)`` returns the
      subcommands that refuse a leading ``--settings``.
    * ``find_processes(path)`` lists processes whose image is under path,
      raising ``ProcessQueryError`` when the table cannot be read.
    * ``stop_service(binary, env)`` runs ``daemon stop --any``.
    * ``clock()`` returns an aware UTC datetime.
    * ``resolve_platform(override, available_keys)`` returns a platform key.
    * ``host_os`` is the host's platform-key prefix.
    * ``progress(label)`` returns a ``download_to`` progress callback.
    * ``environ`` is the base environment for scratch and launcher runs.
    * ``tracked_files(project_root, paths)`` lists files git tracks under
      ``paths``.
    """

    release_source: Callable[[str, float, Optional[str]], ReleaseSource] = (
        default_release_source
    )
    verify_evidence: Callable[[Path, str], ManifestBundle] = load_manifest_bundle
    check_structure: Callable[[Path, str], None] = check_executable_structure
    smoke_runner: Callable[[Path, str, Mapping[str, str], Path], None] = run_smoke_test
    help_reader: Callable[[Path, Mapping[str, str], Path], List[str]] = (
        read_help_subcommands
    )
    settings_probe: Callable[
        [Path, Mapping[str, str], Path, Sequence[str]], List[str]
    ] = probe_settings_rejections
    find_processes: Callable[[Path], List[RunningProcess]] = find_processes_under
    stop_service: Callable[[Path, Mapping[str, str]], ServiceStopResult] = (
        stop_background_service
    )
    clock: Callable[[], datetime] = _utc_now
    resolve_platform: Callable[[Optional[str], Optional[Sequence[str]]], str] = (
        lambda override, keys: resolve_platform_key(override, keys)
    )
    host_os: str = field(default_factory=host_os_family)
    progress: Callable[[str], Optional[Callable[[int, int], None]]] = (
        lambda label: stderr_progress(label)
    )
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    tracked_files: Callable[[Path, Sequence[Path]], List[str]] = list_tracked_files


def default_services() -> Services:
    """Return the production services."""

    return Services()
