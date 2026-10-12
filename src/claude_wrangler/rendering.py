"""Deterministic rendered content for managed files and reports."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .constants import (
    CLAUDE_CHANNELS,
    CLAUDE_PACKAGE_NAME,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    METADATA_FILENAME,
    README_MARKER,
    SCHEMA_VERSION,
    SCRIPT_NAME,
    SCRIPT_VERSION,
    SCRIPT_MARKER,
    SELF_UPDATE_SUPPRESSION_ENV,
)
from .models import Config, Layout
from .releases import CHANNEL_TO_SELECTOR
from .runtime import utc_now_iso

LOCAL_PACKAGE_NAME = "claude-local-managed-install"


def build_local_package_json(claude_version: str) -> str:
    """Return the deterministic package.json content for the managed install."""

    payload = {
        "name": LOCAL_PACKAGE_NAME,
        "private": True,
        "devDependencies": {
            CLAUDE_PACKAGE_NAME: claude_version,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def build_metadata(config: Config) -> Dict[str, Any]:
    """Build the metadata document recorded under the managed local directory."""

    return {
        "schema_version": SCHEMA_VERSION,
        "script_name": SCRIPT_NAME,
        "script_version": SCRIPT_VERSION,
        "generated_at": utc_now_iso(),
        "project_root": str(config.project_root),
        "claude_selector": config.claude_selector,
        "claude_channel": config.claude_channel,
        "claude_version": config.claude_version,
        "shared_home": config.shared_home,
        "reasonable_permissions_enabled": config.reasonable_permissions_enabled,
        "version_source": config.version_source,
        "available_versions": dict(config.available_versions),
        "available_versions_updated_at": config.available_versions_updated_at,
        "paths": {
            "local_dir": config.layout.local_dir_relative,
            "claude_home_dir": config.layout.claude_home_relative,
            "claude_config_dir": config.layout.config_dir_relative,
            "launcher": config.layout.launcher_relative,
            "readme_local": config.layout.readme_relative,
        },
    }


def build_gitignore_block(layout: Layout) -> str:
    """Return the managed `.gitignore` block.

    The default layout keeps every managed directory under `.local/`, so
    one umbrella rule covers them; explicit directory rules are added only
    when an operator relocated a managed directory outside `.local/`.
    The project-root `.claude/` directory is deliberately not ignored:
    Claude Code keeps trackable project settings there.
    """

    directory_entries: List[str] = [".local/"]
    for relative in (
        layout.local_dir_relative,
        layout.claude_home_relative,
        layout.config_dir_relative,
    ):
        normalized = relative.rstrip("/")
        if normalized == ".local" or normalized.startswith(".local/"):
            continue
        entry = "{}/".format(normalized)
        if entry not in directory_entries:
            directory_entries.append(entry)

    lines = [
        GITIGNORE_BEGIN,
        "# Local Claude Code package, session state, wrapper, and sentinel",
        "# artifacts.",
    ]
    lines.extend(directory_entries)
    lines.extend(
        [
            layout.launcher_relative,
            layout.readme_relative,
            GITIGNORE_END,
            "",
        ]
    )
    return "\n".join(lines)


def build_launcher_home_lines(config: Config) -> List[str]:
    """Return the shell block that selects the session's security ring.

    The install-time mode only chooses the default; both the
    `CLAUDE_LOCAL_SHARED_HOME` environment variable and the launcher-only
    `--shared-home` / `--isolated-home` arguments switch one invocation
    between ring 1 (redirected HOME) and ring 0 (real HOME) without
    regenerating managed files. `CLAUDE_CONFIG_DIR` stays pinned in both
    modes, so ring escalation never forks Claude session state.
    """

    default_value = "1" if config.shared_home else "0"
    return [
        "# Ring selection: 0 keeps the operator's real HOME (ring 0); any",
        "# other value redirects HOME into the project-local home (ring 1).",
        'shared_home="${{CLAUDE_LOCAL_SHARED_HOME:-{}}}"'.format(default_value),
        "launcher_args=()",
        'for arg in "$@"; do',
        '  case "$arg" in',
        "    --shared-home)",
        '      shared_home="1"',
        "      ;;",
        "    --isolated-home)",
        '      shared_home="0"',
        "      ;;",
        "    *)",
        '      launcher_args+=("$arg")',
        "      ;;",
        "  esac",
        "done",
        "",
        'if [[ "$shared_home" != "1" ]]; then',
        '  export HOME="$repo_root/{}"'.format(config.layout.claude_home_relative),
        '  export XDG_CONFIG_HOME="$HOME/.config"',
        '  export XDG_CACHE_HOME="$HOME/.cache"',
        '  export XDG_STATE_HOME="$HOME/.local/state"',
        '  export XDG_DATA_HOME="$HOME/.local/share"',
        '  mkdir -p "$HOME"',
        "fi",
        'mkdir -p "$CLAUDE_CONFIG_DIR"',
    ]


def build_launcher_preflight_lines() -> List[str]:
    """Return the shell preflight block embedded in the launcher."""

    return [
        'preflight_mode="${CLAUDE_LOCAL_PREFLIGHT:-warn}"',
        'preflight_prefix="[claude-local] preflight"',
        'local_claude_bin="$local_prefix/node_modules/.bin/claude"',
        "",
        "emit_preflight_warning() {",
        '  printf "%s: %s\\n" "$preflight_prefix" "$1" >&2',
        "}",
        "",
        "preflight_fail() {",
        '  emit_preflight_warning "$1"',
        "  exit 1",
        "}",
        "",
        "handle_preflight_mismatch() {",
        '  if [[ "$preflight_mode" == "strict" ]]; then',
        '    preflight_fail "$1"',
        "  fi",
        '  emit_preflight_warning "$1"',
        "}",
        "",
        "normalize_node_version() {",
        '  local raw_value="${1:-}"',
        '  raw_value="${raw_value#v}"',
        '  printf "%s" "$raw_value"',
        "}",
        "",
        "is_literal_node_version() {",
        '  [[ "${1:-}" =~ ^v?[0-9]+(\\.[0-9]+){0,2}$ ]]',
        "}",
        "",
        'case "$preflight_mode" in',
        "  warn|strict|off) ;;",
        "  *)",
        '    emit_preflight_warning "Unknown CLAUDE_LOCAL_PREFLIGHT value \\"$preflight_mode\\"; defaulting to warn."',
        '    preflight_mode="warn"',
        "    ;;",
        "esac",
        "",
        'node_path="$(command -v node || true)"',
        'if [[ -z "$node_path" ]]; then',
        '  preflight_fail "Required node command is not available in PATH."',
        "fi",
        "",
        'if [[ ! -x "$local_claude_bin" ]]; then',
        '  preflight_fail "Local Claude Code executable is missing or not executable: $local_claude_bin. Re-run claude-wrangler install or upgrade for this project."',
        "fi",
        "",
        'if [[ "$preflight_mode" != "off" ]]; then',
        '  selector_path=""',
        '  selector_value=""',
        '  if [[ -f "$repo_root/.nvmrc" ]]; then',
        '    selector_path="$repo_root/.nvmrc"',
        '    selector_value="$(sed -n \'1{s/\\r$//;s/^[[:space:]]*//;s/[[:space:]]*$//;p;}\' "$selector_path" 2>/dev/null)"',
        '  elif [[ -f "$repo_root/.node-version" ]]; then',
        '    selector_path="$repo_root/.node-version"',
        '    selector_value="$(sed -n \'1{s/\\r$//;s/^[[:space:]]*//;s/[[:space:]]*$//;p;}\' "$selector_path" 2>/dev/null)"',
        '  elif [[ -f "$repo_root/.tool-versions" ]]; then',
        '    selector_path="$repo_root/.tool-versions"',
        '    selector_value="$(awk \'/^[[:space:]]*nodejs[[:space:]]+/ { print $2; exit }\' "$selector_path" 2>/dev/null)"',
        "    selector_value=\"${selector_value%$'\\r'}\"",
        "  fi",
        "",
        '  if [[ -n "$selector_path" && -n "$selector_value" ]]; then',
        '    selector_label="${selector_path#"$repo_root/"}"',
        '    actual_node_version="$(node --version 2>/dev/null || true)"',
        '    if [[ -z "$actual_node_version" ]]; then',
        '      handle_preflight_mismatch "Project declares Node.js selector ${selector_label}=${selector_value}, but node --version could not be resolved."',
        '    elif is_literal_node_version "$selector_value"; then',
        '      expected_node_version="$(normalize_node_version "$selector_value")"',
        '      actual_node_version_normalized="$(normalize_node_version "$actual_node_version")"',
        '      if [[ "$expected_node_version" != "$actual_node_version_normalized" ]]; then',
        '        handle_preflight_mismatch "Project declares Node.js ${selector_value} in ${selector_label}, but PATH resolves node ${actual_node_version} at ${node_path}."',
        "      fi",
        "    else",
        '      emit_preflight_warning "Project declares Node.js selector ${selector_label}=${selector_value}. Launch claude-local from the project-activated shell for best parity."',
        "    fi",
        "  fi",
        "fi",
    ]


def build_launcher_update_notice_lines(layout: Layout) -> List[str]:
    """Return the shell block that prints a known-update notice before launch."""

    metadata_path = "{}/{}".format(layout.local_dir_relative, METADATA_FILENAME)
    return [
        "emit_update_notice() {",
        "  node - \"$repo_root/{}\" <<'NODE'".format(metadata_path),
        'const fs = require("fs");',
        "const metadataPath = process.argv[2];",
        "let metadata;",
        "try {",
        '  metadata = JSON.parse(fs.readFileSync(metadataPath, "utf8"));',
        "} catch (error) {",
        "  process.exit(0);",
        "}",
        'const installedVersion = typeof metadata.claude_version === "string" ? metadata.claude_version : null;',
        'const channel = typeof metadata.claude_channel === "string" ? metadata.claude_channel.toLowerCase() : null;',
        'const availableVersions = metadata && typeof metadata.available_versions === "object" && metadata.available_versions !== null ? metadata.available_versions : {};',
        'const availableVersion = typeof availableVersions[channel] === "string" ? availableVersions[channel] : null;',
        "if (!installedVersion || !channel || !availableVersion) {",
        "  process.exit(0);",
        "}",
        "function parseVersion(raw) {",
        '  const normalized = String(raw).trim().replace(/^v/, "");',
        '  const pieces = normalized.split("-", 2);',
        '  const main = pieces[0].split(".").map((part) => Number.parseInt(part, 10) || 0);',
        "  while (main.length < 3) {",
        "    main.push(0);",
        "  }",
        '  const prerelease = pieces.length > 1 ? pieces[1].split(".") : [];',
        "  return { main, prerelease };",
        "}",
        "function compareIdentifier(left, right) {",
        "  const leftIsNumeric = /^[0-9]+$/.test(left);",
        "  const rightIsNumeric = /^[0-9]+$/.test(right);",
        "  if (leftIsNumeric && rightIsNumeric) {",
        "    return Number(left) - Number(right);",
        "  }",
        "  if (leftIsNumeric) {",
        "    return -1;",
        "  }",
        "  if (rightIsNumeric) {",
        "    return 1;",
        "  }",
        "  if (left < right) {",
        "    return -1;",
        "  }",
        "  if (left > right) {",
        "    return 1;",
        "  }",
        "  return 0;",
        "}",
        "function comparePrerelease(left, right) {",
        "  if (left.length === 0 && right.length === 0) {",
        "    return 0;",
        "  }",
        "  if (left.length === 0) {",
        "    return 1;",
        "  }",
        "  if (right.length === 0) {",
        "    return -1;",
        "  }",
        "  const count = Math.max(left.length, right.length);",
        "  for (let index = 0; index < count; index += 1) {",
        "    const leftValue = left[index];",
        "    const rightValue = right[index];",
        "    if (leftValue === undefined) {",
        "      return -1;",
        "    }",
        "    if (rightValue === undefined) {",
        "      return 1;",
        "    }",
        "    const compared = compareIdentifier(leftValue, rightValue);",
        "    if (compared !== 0) {",
        "      return compared;",
        "    }",
        "  }",
        "  return 0;",
        "}",
        "function compareVersions(leftRaw, rightRaw) {",
        "  const left = parseVersion(leftRaw);",
        "  const right = parseVersion(rightRaw);",
        "  for (let index = 0; index < 3; index += 1) {",
        "    const delta = left.main[index] - right.main[index];",
        "    if (delta !== 0) {",
        "      return delta;",
        "    }",
        "  }",
        "  return comparePrerelease(left.prerelease, right.prerelease);",
        "}",
        "if (compareVersions(installedVersion, availableVersion) >= 0) {",
        "  process.exit(0);",
        "}",
        "console.error(`NOTE: {} has an available update on ${{channel}} (installed ${{installedVersion}}; available ${{availableVersion}}). Run \\`{} --update\\` to refresh version information and \\`{} --upgrade --channel ${{channel}}\\` to install it.`);".format(
            CLAUDE_PACKAGE_NAME,
            SCRIPT_NAME,
            SCRIPT_NAME,
        ),
        "NODE",
        "}",
        "",
        "emit_update_notice",
    ]


def build_launcher_reasonable_permissions_lines(config: Config) -> List[str]:
    """Return the shell block that injects the managed permission default."""

    state_value = "1" if config.reasonable_permissions_enabled else "0"
    return [
        'reasonable_permissions_enabled="{}"'.format(state_value),
        "default_claude_args=()",
        "",
        'if [[ "$reasonable_permissions_enabled" == "1" ]]; then',
        "  explicit_permission_choice=0",
        '  for arg in "${launcher_args[@]}"; do',
        '    case "$arg" in',
        "      --permission-mode|--dangerously-skip-permissions)",
        "        explicit_permission_choice=1",
        "        ;;",
        "      --permission-mode=*)",
        "        explicit_permission_choice=1",
        "        ;;",
        "    esac",
        '    if [[ "$explicit_permission_choice" == "1" ]]; then',
        "      break",
        "    fi",
        "  done",
        '  if [[ "$explicit_permission_choice" == "0" ]]; then',
        '    default_claude_args+=("--permission-mode" "acceptEdits")',
        "  fi",
        "fi",
    ]


def build_launcher_content(config: Config) -> str:
    """Build the generated shell launcher."""

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# {}".format(SCRIPT_MARKER),
        "# Generated by {}".format(SCRIPT_NAME),
        "#",
        "# This launcher keeps the Claude Code npm package local to the",
        "# repository by executing the managed local claude binary directly.",
        "# It must not fall back to a registry package or global install named",
        "# claude, so a bad global tool update cannot take out this project.",
        "#",
        "# CLAUDE_CONFIG_DIR is pinned to the project so auth, sessions, and",
        "# other Claude state stay project-local at every security ring. In",
        "# default mode the launcher also redirects HOME into the managed",
        "# project-local home directory (ring 1); pass --shared-home or set",
        "# CLAUDE_LOCAL_SHARED_HOME=1 for one ring-0 session with the real",
        "# HOME.",
        "#",
        "# Claude Code's own updater and installer write wherever HOME points,",
        "# not next to the running binary, so at ring 0 they would replace or",
        "# delete the operator's global install. claude-wrangler owns updates",
        "# for this project, so self-update and install checks stay disabled.",
        "",
        'repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        'local_prefix="$repo_root/{}"'.format(config.layout.local_dir_relative),
        'export NPM_CONFIG_CACHE="$local_prefix/.npm-cache"',
        'export CLAUDE_LOCAL_MANAGED_BY="{}"'.format(SCRIPT_NAME),
        'export CLAUDE_CONFIG_DIR="$repo_root/{}"'.format(
            config.layout.config_dir_relative
        ),
    ]
    lines.extend(
        'export {}="{}"'.format(name, value)
        for name, value in SELF_UPDATE_SUPPRESSION_ENV
    )
    lines.append("")
    lines.extend(build_launcher_home_lines(config))
    lines.extend([""])
    lines.extend(build_launcher_preflight_lines())
    lines.extend([""])
    lines.extend(build_launcher_update_notice_lines(config.layout))
    lines.extend([""])
    lines.extend(build_launcher_reasonable_permissions_lines(config))
    lines.extend(
        [
            "",
            'exec "$local_claude_bin" "${default_claude_args[@]}" "${launcher_args[@]}"',
            "",
        ]
    )
    return "\n".join(lines)


def build_local_readme_content(config: Config) -> str:
    """Build the generated project-local operator README."""

    lines = [
        README_MARKER,
        "# Local Claude Code Start Guide",
        "",
        "This file was generated by `{}` and is intended for local operator use.".format(
            SCRIPT_NAME
        ),
        "It is ignored on purpose and should not be committed.",
        "",
        "## Managed Paths",
        "",
        "- Local install: `{}`".format(config.layout.local_dir_relative),
        "- Local Claude HOME (rings 1-2): `{}`".format(
            config.layout.claude_home_relative
        ),
        "- Pinned CLAUDE_CONFIG_DIR: `{}`".format(config.layout.config_dir_relative),
        "- Launcher: `{}`".format(config.layout.launcher_relative),
        "- This README: `{}`".format(config.layout.readme_relative),
        "",
        "## Installed Version",
        "",
        "- Channel: `{}`".format(config.claude_channel or "unknown"),
        "- Requested version: `{}`".format(config.claude_selector),
        "- Resolved Claude Code version: `{}`".format(config.claude_version),
        "- Version source: `{}`".format(config.version_source),
        "- HOME mode: `{}`".format(
            "shared user HOME" if config.shared_home else "project-local isolated HOME"
        ),
        "- Reasonable permissions default: `{}`".format(
            "enabled" if config.reasonable_permissions_enabled else "disabled"
        ),
        "- Known stable version: `{}`".format(
            config.available_versions.get("stable") or "unavailable"
        ),
        "- Known latest version: `{}`".format(
            config.available_versions.get("latest") or "unavailable"
        ),
        "- Known next version: `{}`".format(
            config.available_versions.get("next") or "unavailable"
        ),
        "",
        "## Start Claude Code",
        "",
        "```bash",
        "./{}".format(config.layout.launcher_relative),
        "```",
        "",
        "## Security Rings",
        "",
        "Agent work follows the project security ring model (see",
        "`docs/specifications/security_ring_model.md` when present):",
        "",
        "- Ring 3: agent-managed sandbox, ephemeral sandbox HOME (default",
        "  for build and test work).",
        "- Ring 2: sandboxed, HOME=`{}`.".format(config.layout.claude_home_relative),
        "- Ring 1: unsandboxed, HOME=`{}`. The launcher".format(
            config.layout.claude_home_relative
        ),
        "  starts sessions here by default.",
        "- Ring 0: unsandboxed, real HOME. Last resort; use",
        "  `./{} --shared-home` or".format(config.layout.launcher_relative),
        "  `CLAUDE_LOCAL_SHARED_HOME=1` for one ring-0 session.",
        "",
        "`CLAUDE_CONFIG_DIR` stays pinned to `{}` at every".format(
            config.layout.config_dir_relative
        ),
        "ring, so escalation never forks Claude auth or session history.",
        "",
        "## Managed Permission Default",
        "",
        "The launcher can store one opt-in reasonable-permissions default.",
        "Current state: `{}`.".format(
            "enabled" if config.reasonable_permissions_enabled else "disabled"
        ),
        "",
        "When enabled, the wrapper adds `--permission-mode acceptEdits` only",
        "when you did not already provide an explicit permission choice.",
        "This keeps ordinary workspace edits flowing while Bash commands and",
        "ACP operations such as commit and push still prompt for approval.",
        "",
        "Explicit `--permission-mode` and `--dangerously-skip-permissions`",
        "always win. The wrapper never injects",
        "`--dangerously-skip-permissions`.",
        "",
        "## Launcher Preflight",
        "",
        "The launcher runs a cheap runtime preflight before it starts Claude",
        "Code. By default it warns when the project declares a checked-in",
        "Node.js selector but the active shell appears inconsistent with it.",
        "",
        "Use one of these modes when needed:",
        "",
        "- `CLAUDE_LOCAL_PREFLIGHT=warn`",
        "- `CLAUDE_LOCAL_PREFLIGHT=strict`",
        "- `CLAUDE_LOCAL_PREFLIGHT=off`",
        "",
        "## Resume Claude Code",
        "",
        "```bash",
        "./{} --continue".format(config.layout.launcher_relative),
        "./{} --resume".format(config.layout.launcher_relative),
        "```",
        "",
        "## Audit",
        "",
        "```bash",
        "NPM_CONFIG_CACHE=./{}/.npm-cache npm audit --prefix ./{}".format(
            config.layout.local_dir_relative,
            config.layout.local_dir_relative,
        ),
        "```",
        "",
        "## Inspect Managed State",
        "",
        "```bash",
        "{} --inspect .".format(SCRIPT_NAME),
        "```",
        "",
        "## Self-Test Managed State",
        "",
        "```bash",
        "{} --selftest .".format(SCRIPT_NAME),
        "```",
        "",
        "## Refresh Known Versions",
        "",
        "```bash",
        "{} --update .".format(SCRIPT_NAME),
        "```",
        "",
        "## Upgrade To Latest Known Channel Version",
        "",
        "```bash",
        "{} --upgrade --channel stable .".format(SCRIPT_NAME),
        "{} --upgrade --channel latest .".format(SCRIPT_NAME),
        "{} --upgrade --channel next .".format(SCRIPT_NAME),
        "```",
        "",
        "## Upgrade To One Explicit Version",
        "",
        "```bash",
        "{} --upgrade --channel stable --version 2.1.153 .".format(SCRIPT_NAME),
        "```",
        "",
        "## Toggle Managed Permission Defaults",
        "",
        "```bash",
        "{} --set-reasonable-permissions .".format(SCRIPT_NAME),
        "{} --clear-reasonable-permissions .".format(SCRIPT_NAME),
        "```",
        "",
        "## Uninstall Managed Setup",
        "",
        "```bash",
        "{} --uninstall .".format(SCRIPT_NAME),
        "```",
        "",
    ]
    return "\n".join(lines)


def local_package_json_looks_managed(content: str) -> bool:
    """Return True when package.json content matches the managed shape."""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False

    if not isinstance(payload, dict):
        return False
    if payload.get("name") != LOCAL_PACKAGE_NAME:
        return False
    if payload.get("private") is not True:
        return False

    dev_dependencies = payload.get("devDependencies")
    if not isinstance(dev_dependencies, dict):
        return False
    version = dev_dependencies.get(CLAUDE_PACKAGE_NAME)
    return isinstance(version, str) and bool(version)


def build_install_summary(config: Config) -> str:
    """Return the operator summary shown after install and upgrade operations."""

    lines = [
        "Operation complete.",
        "",
        "Project root: {}".format(config.project_root),
        "Operation: {}".format(config.operation),
        "Channel: {}".format(config.claude_channel or "unknown"),
        "Requested version: {}".format(config.claude_selector),
        "Resolved Claude Code version: {}".format(config.claude_version),
        "Version source: {}".format(config.version_source),
        "Local package manifest: {}".format(config.layout.local_package_json_path),
        "Metadata file: {}".format(config.layout.metadata_path),
        "Launcher: {}".format(config.layout.launcher_path),
        "Local README: {}".format(config.layout.readme_path),
        "Reasonable permissions default: {}".format(
            "enabled" if config.reasonable_permissions_enabled else "disabled"
        ),
        "HOME mode: {}".format(
            "shared user HOME" if config.shared_home else "project-local isolated HOME"
        ),
        "Pinned CLAUDE_CONFIG_DIR: {}".format(config.layout.config_dir),
        "Package install: {}".format(
            "unchanged existing managed install"
            if config.reconfigure_only
            else (
                "skipped by request"
                if config.skip_install
                else (
                    "repair cleanup plus npm install required"
                    if config.repair_install and config.dry_run
                    else (
                        "repair cleanup plus npm install completed"
                        if config.repair_install
                        else (
                            "npm install required"
                            if config.dry_run
                            else "npm install completed"
                        )
                    )
                )
            )
        ),
        "",
        "Recommended usage:",
        "  {}".format(config.layout.launcher_path),
        "  {} --continue".format(config.layout.launcher_path),
        "  {} --resume".format(config.layout.launcher_path),
        "",
        "Audit command:",
        "  NPM_CONFIG_CACHE={}/.npm-cache npm audit --prefix {}".format(
            config.layout.local_dir,
            config.layout.local_dir,
        ),
    ]
    return "\n".join(lines)


def build_available_versions_table(
    available_versions: Dict[str, Optional[str]],
) -> str:
    """Render the known channel versions as a simple fixed-width table."""

    rows = [("Channel", "Dist-tag", "Known version")]
    rows.extend(
        (
            channel,
            CHANNEL_TO_SELECTOR[channel],
            available_versions.get(channel) or "unavailable",
        )
        for channel in CLAUDE_CHANNELS
    )
    widths = [
        max(len(str(row[column_index])) for row in rows)
        for column_index in range(len(rows[0]))
    ]
    rendered_rows = []
    for row_index, row in enumerate(rows):
        rendered_rows.append(
            "  ".join(
                str(value).ljust(widths[column_index])
                for column_index, value in enumerate(row)
            )
        )
        if row_index == 0:
            rendered_rows.append(
                "  ".join("-" * widths[column_index] for column_index in range(3))
            )
    return "\n".join(rendered_rows)


def build_known_versions(
    available_versions: Dict[str, Optional[str]],
    available_versions_updated_at: Optional[str],
) -> Dict[str, Any]:
    """Return the install-channel defaults and locally known versions."""

    return {
        "default_install_selector": DEFAULT_INSTALL_CLAUDE_SELECTOR,
        "channel_dist_tags": dict(CHANNEL_TO_SELECTOR),
        "available_versions": dict(available_versions),
        "available_versions_updated_at": available_versions_updated_at,
    }
