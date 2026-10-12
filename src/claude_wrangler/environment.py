"""Runtime-environment diagnostics for inspection and self-tests.

The native runtime needs neither Node.js nor npm; `node`, `npm`, and `npx`
are reported only as information, and the checked-in Node.js selector check
is advisory (the generated launcher's parity check is warn-only). The
launcher environment for each security ring is computed with the launcher
core's own functions, so the report shows exactly what a launch exports on
this host's operating system.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Mapping, Optional

from . import launcher_core

EXPLICIT_NODE_VERSION_PATTERN = re.compile(r"^v?\d+(?:\.\d+){0,2}$")
# Variables the ring report lists, in display order.
RING_VARIABLES = (
    "CLAUDE_CONFIG_DIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "CLAUDE_LOCAL_SHARED_HOME",
    "CLAUDE_LOCAL_MANAGED_BY",
    "DISABLE_UPDATES",
    "DISABLE_AUTOUPDATER",
    "DISABLE_INSTALLATION_CHECKS",
)


def normalize_node_version(raw_version: str) -> str:
    """Return a normalized Node.js version without a leading `v`."""

    return raw_version.strip().lstrip("v")


def explicit_node_version(raw_value: str) -> Optional[str]:
    """Return a normalized literal Node.js version when one is present."""

    candidate = raw_value.strip()
    if not candidate:
        return None
    if not EXPLICIT_NODE_VERSION_PATTERN.match(candidate):
        return None
    return normalize_node_version(candidate)


def _read_trimmed_text(path: Path) -> Optional[str]:
    """Return trimmed file content when the path exists and is readable."""

    if not path.is_file():
        return None
    try:
        text = path.read_bytes().decode("utf-8", "replace").strip()
    except OSError:
        return None
    return text or None


def detect_node_selector_signals(project_root: Path) -> List[Dict[str, Optional[str]]]:
    """Return checked-in Node.js selector signals present at project root."""

    signals: List[Dict[str, Optional[str]]] = []
    for filename in (".nvmrc", ".node-version"):
        path = project_root / filename
        raw_value = _read_trimmed_text(path)
        if raw_value is None:
            continue
        first_line = raw_value.splitlines()[0].strip()
        signals.append(
            {
                "kind": filename,
                "path": str(path),
                "raw_value": first_line,
                "explicit_version": explicit_node_version(first_line),
            }
        )

    tool_versions_path = project_root / ".tool-versions"
    tool_versions_text = _read_trimmed_text(tool_versions_path)
    if tool_versions_text is None:
        return signals

    for raw_line in tool_versions_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] != "nodejs":
            continue
        raw_value = parts[1]
        signals.append(
            {
                "kind": ".tool-versions:nodejs",
                "path": str(tool_versions_path),
                "raw_value": raw_value,
                "explicit_version": explicit_node_version(raw_value),
            }
        )
        break
    return signals


def detect_package_manager_declaration(
    project_root: Path,
) -> Optional[Dict[str, Optional[str]]]:
    """Return the root `package.json` package-manager declaration when present."""

    package_json_path = project_root / "package.json"
    if not package_json_path.is_file():
        return None

    try:
        payload = json.loads(package_json_path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "path": str(package_json_path),
            "raw_value": None,
            "family": None,
            "version": None,
            "read_error": str(exc),
        }

    if not isinstance(payload, dict):
        return {
            "path": str(package_json_path),
            "raw_value": None,
            "family": None,
            "version": None,
            "read_error": "Expected a JSON object in package.json.",
        }

    raw_value = payload.get("packageManager")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None

    normalized_value = raw_value.strip()
    family, separator, version = normalized_value.partition("@")
    return {
        "path": str(package_json_path),
        "raw_value": normalized_value,
        "family": family or None,
        "version": version if separator and version else None,
        "read_error": None,
    }


def command_name(name: str, os_name: Optional[str] = None) -> str:
    """Return the platform spelling of a Node.js tool (`npm.cmd` on Windows)."""

    nt = (os.name if os_name is None else os_name) == "nt"
    if nt and name in ("npm", "npx"):
        return name + ".cmd"
    return name


def find_command(
    command: str,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve ``command`` from absolute PATH entries only.

    ``shutil.which`` searches the current directory first on Windows
    (unless ``NoDefaultCurrentDirectoryInExePath`` is set), so running
    `--inspect` from the root of an untrusted clone would execute that
    clone's ``node.cmd``. The launcher core's resolver never searches the
    current directory and skips relative PATH entries.
    """

    env = os.environ if environ is None else environ
    effective_os = os.name if os_name is None else os_name
    return launcher_core.find_executable_on_path(
        command,
        launcher_core.env_lookup(env, "PATH", effective_os),
        effective_os,
        launcher_core.env_lookup(env, "PATHEXT", effective_os),
    )


def resolve_command_metadata(command: str) -> Dict[str, Any]:
    """Return resolved-path and version details for one command (informational).

    The command is found with `find_command`, never in the current
    directory, before its `--version` runs.
    """

    resolved_path = find_command(command)
    metadata: Dict[str, Any] = {
        "requested_name": command,
        "resolved_path": resolved_path,
        "version": None,
        "found": resolved_path is not None,
        "required": False,
    }
    if resolved_path is None:
        return metadata

    try:
        completed = subprocess.run(
            [resolved_path, "--version"],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return metadata

    if completed.returncode != 0:
        return metadata

    version_output = (completed.stdout or completed.stderr or "").strip()
    if not version_output:
        return metadata
    metadata["version"] = version_output.splitlines()[0].strip()
    return metadata


def ring_environment(
    launch: launcher_core.LaunchConfig,
    project_root: Path,
    *,
    shared_home: bool,
    os_name: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Return the variables a launch exports at one ring on one OS.

    Variables the ring leaves inherited (the operator's home at ring 0)
    are reported as None.
    """

    effective_os = os.name if os_name is None else os_name
    paths = launcher_core.build_repo_paths(str(project_root), launch, effective_os)
    env = launcher_core.build_session_environment(
        {}, paths=paths, config=launch, shared_home=shared_home, os_name=effective_os
    )
    report: Dict[str, Optional[str]] = {}
    for name in RING_VARIABLES:
        if name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA"):
            if effective_os != "nt":
                continue
        report[name] = env.get(name)
    report["PATH_PREFIX"] = paths.shim_dir
    return report


def build_home_configuration(
    launch: launcher_core.LaunchConfig,
    project_root: Path,
    shared_home: bool,
    *,
    os_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the HOME, XDG, and config-dir values the launcher will expose.

    `CLAUDE_CONFIG_DIR` is pinned at both rings: escalating the `$HOME`
    fidelity ring never forks Claude session state out of the project.
    On native Windows ring 1 also redirects USERPROFILE, HOMEDRIVE and
    HOMEPATH, APPDATA, and LOCALAPPDATA.
    """

    effective_os = os.name if os_name is None else os_name
    rings = {
        "ring1": ring_environment(
            launch, project_root, shared_home=False, os_name=effective_os
        ),
        "ring0": ring_environment(
            launch, project_root, shared_home=True, os_name=effective_os
        ),
    }
    return {
        "mode": "shared user HOME" if shared_home else "project-local isolated HOME",
        "default_ring": 0 if shared_home else 1,
        "host_os": "windows" if effective_os == "nt" else "posix",
        "inherits_from_parent_process": shared_home,
        "launcher_environment": rings["ring0" if shared_home else "ring1"],
        "rings": rings,
        "escape_hatches": [
            "claude-local --shared-home (ring 0 for one session)",
            "CLAUDE_LOCAL_SHARED_HOME=1 (ring 0)",
            "claude-local --isolated-home (ring 1 for one session)",
        ],
    }


def assess_node_selector_alignment(
    selector_signals: List[Dict[str, Optional[str]]],
    node_command: Mapping[str, Any],
) -> Dict[str, Any]:
    """Assess whether resolved Node.js matches explicit checked-in selectors.

    Advisory only: Claude Code does not need Node.js, so a missing `node`
    is `not_required` rather than a failure.
    """

    if not selector_signals:
        return {
            "status": "not_applicable",
            "summary": "No checked-in Node.js selector files were detected.",
            "signal_count": 0,
            "explicit_signal_count": 0,
            "signals_present": False,
            "signals": [],
        }

    resolved_node_version = node_command.get("version")
    normalized_resolved_version = None
    if isinstance(resolved_node_version, str) and resolved_node_version:
        normalized_resolved_version = normalize_node_version(resolved_node_version)

    explicit_signal_count = 0
    mismatch_descriptions: List[str] = []
    enriched_signals: List[Dict[str, Any]] = []

    for signal in selector_signals:
        enriched: Dict[str, Any] = dict(signal)
        explicit_version = signal.get("explicit_version")
        matches_resolved_version = None
        if isinstance(explicit_version, str) and explicit_version:
            explicit_signal_count += 1
            if normalized_resolved_version is not None:
                expected_parts = explicit_version.split(".")
                actual_parts = normalized_resolved_version.split(".")
                matches_resolved_version = (
                    actual_parts[: len(expected_parts)] == expected_parts
                )
                if not matches_resolved_version:
                    mismatch_descriptions.append(
                        "{} expects Node.js {}.".format(
                            signal["kind"],
                            signal["raw_value"],
                        )
                    )
        enriched["matches_resolved_node_version"] = matches_resolved_version
        enriched_signals.append(enriched)

    if not node_command.get("found"):
        summary = (
            "Checked-in Node.js selectors are present, but `node` is not on "
            "PATH. Claude Code does not need it; project tooling may."
        )
        status = "node_not_found"
    elif explicit_signal_count and normalized_resolved_version is None:
        summary = (
            "Checked-in Node.js selectors are present, but `node --version` "
            "could not be resolved."
        )
        status = "unknown"
    elif mismatch_descriptions:
        summary = (
            "Resolved node version {} does not match checked-in Node.js "
            "selectors: {}".format(
                resolved_node_version,
                " ".join(mismatch_descriptions),
            )
        )
        status = "mismatch"
    elif explicit_signal_count:
        summary = (
            "Resolved node version {} matches all explicit Node.js selectors.".format(
                resolved_node_version
            )
        )
        status = "match"
    else:
        summary = (
            "Checked-in Node.js selectors are present, but their values are "
            "not literal versions that claude-wrangler can verify "
            "automatically."
        )
        status = "unknown"

    return {
        "status": status,
        "summary": summary,
        "signal_count": len(selector_signals),
        "explicit_signal_count": explicit_signal_count,
        "signals_present": True,
        "signals": enriched_signals,
    }


def collect_runtime_diagnostics(
    project_root: Path,
    launch: launcher_core.LaunchConfig,
    shared_home: bool,
    *,
    os_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect runtime-resolution details for inspection and self-tests."""

    resolved_commands = {
        "node": resolve_command_metadata(command_name("node", os_name)),
        "npm": resolve_command_metadata(command_name("npm", os_name)),
        "npx": resolve_command_metadata(command_name("npx", os_name)),
    }
    selector_signals = detect_node_selector_signals(project_root)
    selector_alignment = assess_node_selector_alignment(
        selector_signals,
        resolved_commands["node"],
    )
    return {
        "node_required": False,
        "resolved_commands": resolved_commands,
        "node_selector_signals": selector_alignment["signals"],
        "selector_alignment": {
            "status": selector_alignment["status"],
            "summary": selector_alignment["summary"],
            "signal_count": selector_alignment["signal_count"],
            "explicit_signal_count": selector_alignment["explicit_signal_count"],
            "signals_present": selector_alignment["signals_present"],
        },
        "package_manager_declaration": detect_package_manager_declaration(project_root),
        "home_configuration": build_home_configuration(
            launch, project_root, shared_home, os_name=os_name
        ),
    }
