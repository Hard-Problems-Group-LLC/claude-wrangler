"""Runtime-environment detection for launchers, inspection, and self-tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

EXPLICIT_NODE_VERSION_PATTERN = re.compile(r"^v?\d+(?:\.\d+){0,2}$")


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
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def detect_node_selector_signals(project_root: Path) -> List[Dict[str, Optional[str]]]:
    """Return checked-in Node.js selector signals present at project root."""

    signals: List[Dict[str, Optional[str]]] = []
    for filename in (".nvmrc", ".node-version"):
        path = project_root / filename
        raw_value = _read_trimmed_text(path)
        if raw_value is None:
            continue
        signals.append(
            {
                "kind": filename,
                "path": str(path),
                "raw_value": raw_value,
                "explicit_version": explicit_node_version(raw_value),
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
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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


def resolve_command_metadata(command_name: str) -> Dict[str, Any]:
    """Return resolved-path and version details for one runtime command."""

    resolved_path = shutil.which(command_name)
    metadata: Dict[str, Any] = {
        "requested_name": command_name,
        "resolved_path": resolved_path,
        "version": None,
        "found": resolved_path is not None,
    }
    if resolved_path is None:
        return metadata

    try:
        completed = subprocess.run(
            [resolved_path, "--version"],
            check=False,
            capture_output=True,
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


def build_home_configuration(
    shared_home: bool,
    claude_home_dir: Path,
    config_dir: Path,
) -> Dict[str, Any]:
    """Return the HOME, XDG, and config-dir paths the launcher will expose.

    `CLAUDE_CONFIG_DIR` is pinned in both modes: escalating the `$HOME`
    fidelity ring never forks Claude session state out of the project.
    """

    config_dir_value = str(config_dir.resolve())
    if shared_home:
        return {
            "mode": "shared user HOME",
            "inherits_from_parent_process": True,
            "launcher_environment": {
                "CLAUDE_CONFIG_DIR": config_dir_value,
                "HOME": None,
                "XDG_CONFIG_HOME": None,
                "XDG_CACHE_HOME": None,
                "XDG_STATE_HOME": None,
                "XDG_DATA_HOME": None,
            },
        }

    home_dir = str(claude_home_dir.resolve())
    return {
        "mode": "project-local isolated HOME",
        "inherits_from_parent_process": False,
        "launcher_environment": {
            "CLAUDE_CONFIG_DIR": config_dir_value,
            "HOME": home_dir,
            "XDG_CONFIG_HOME": "{}/.config".format(home_dir),
            "XDG_CACHE_HOME": "{}/.cache".format(home_dir),
            "XDG_STATE_HOME": "{}/.local/state".format(home_dir),
            "XDG_DATA_HOME": "{}/.local/share".format(home_dir),
        },
    }


def assess_node_selector_alignment(
    selector_signals: List[Dict[str, Optional[str]]],
    node_command: Dict[str, Optional[str]],
) -> Dict[str, Any]:
    """Assess whether resolved Node.js matches explicit checked-in selectors."""

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
    enriched_signals: List[Dict[str, Optional[str]]] = []

    for signal in selector_signals:
        enriched = dict(signal)
        explicit_version = signal.get("explicit_version")
        matches_resolved_version = None
        if isinstance(explicit_version, str) and explicit_version:
            explicit_signal_count += 1
            if normalized_resolved_version is not None:
                matches_resolved_version = (
                    explicit_version == normalized_resolved_version
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
            "Checked-in Node.js selectors are present, but `node` is not "
            "available in PATH."
        )
        status = "unknown"
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
    shared_home: bool,
    claude_home_dir: Path,
    config_dir: Path,
    npm_command_name: str,
    npx_command_name: str,
) -> Dict[str, Any]:
    """Collect runtime-resolution details for inspection and self-tests."""

    resolved_commands = {
        "node": resolve_command_metadata("node"),
        "npm": resolve_command_metadata(npm_command_name),
        "npx": resolve_command_metadata(npx_command_name),
    }
    selector_signals = detect_node_selector_signals(project_root)
    selector_alignment = assess_node_selector_alignment(
        selector_signals,
        resolved_commands["node"],
    )
    return {
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
            shared_home,
            claude_home_dir,
            config_dir,
        ),
    }
