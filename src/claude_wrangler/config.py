"""Argument parsing and configuration normalization."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .constants import (
    CLAUDE_CHANNELS,
    DEFAULT_CONFIG_DIR,
    DEFAULT_HOME_DIR,
    DEFAULT_INSTALL_CLAUDE_CHANNEL,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    DEFAULT_LAUNCHER_RELATIVE_PATH,
    DEFAULT_LOCAL_DIR,
    DEFAULT_NPM_INSTALL_LOGLEVEL,
    DEFAULT_NPM_TIMEOUT_SECONDS,
    DEFAULT_README_FILENAME,
    NPM_INSTALL_LOGLEVELS,
)
from .layout import build_layout, read_existing_state, resolve_project_root
from .models import ClaudeWranglerError, Config, ExistingState
from .releases import infer_claude_channel

MISSING_DASH_FLAG_HINTS = {
    "channel": "--channel",
    "claude-config-dir": "--claude-config-dir",
    "claude-home-dir": "--claude-home-dir",
    "clear-reasonable-permissions": "--clear-reasonable-permissions",
    "dry-run": "--dry-run",
    "force": "--force",
    "inspect": "--inspect",
    "isolated-home": "--isolated-home",
    "launcher": "--launcher",
    "local-dir": "--local-dir",
    "readme-local": "--readme-local",
    "repair-install": "--repair-install",
    "set-reasonable-permissions": "--set-reasonable-permissions",
    "selftest": "--selftest",
    "shared-home": "--shared-home",
    "skip-install": "--skip-install",
    "uninstall": "--uninstall",
    "update": "--update",
    "upgrade": "--upgrade",
    "version": "--version",
}


def normalize_requested_version(raw_value: Optional[str]) -> Optional[str]:
    """Normalize one optional requested version or selector value."""

    if raw_value is None:
        return None
    stripped = raw_value.strip()
    if not stripped:
        return None
    if stripped.lower() == "latest":
        return "latest"
    return stripped


def parse_positive_int(raw_value: str) -> int:
    """Parse one positive integer CLI value."""

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


def validate_channel_version_pair(
    parser: argparse.ArgumentParser,
    channel: Optional[str],
    requested_version: Optional[str],
) -> None:
    """Reject one explicit channel/version combination when it is incoherent."""

    if channel is None or requested_version is None or requested_version == "latest":
        return
    inferred_channel = infer_claude_channel(requested_version)
    if inferred_channel is None or inferred_channel == channel:
        return
    parser.error(
        "--version {} does not match --channel {}.".format(
            requested_version,
            channel,
        )
    )


def missing_dash_flag_hint(raw_project_root: str) -> Optional[str]:
    """Return a likely flag when one project-root token looks like a flag."""

    return MISSING_DASH_FLAG_HINTS.get(raw_project_root)


def resolve_config_project_root(raw_project_root: str) -> Path:
    """Resolve the project root and add CLI-focused hints for common mistakes."""

    try:
        return resolve_project_root(raw_project_root)
    except ClaudeWranglerError as exc:
        hint = missing_dash_flag_hint(raw_project_root)
        if hint is not None:
            raise ClaudeWranglerError(
                "{} Did you perhaps mean '{}'?".format(exc, hint)
            ) from exc
        raise


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Manage a project-local @anthropic-ai/claude-code installation "
            "with explicit metadata, a generated launcher, JSON inspection, "
            "and conservative uninstall semantics."
        ),
        epilog=(
            "Examples:\n"
            "  install current stable:     claude-wrangler .\n"
            "  install current next:       claude-wrangler --channel next .\n"
            "  refresh known versions:     claude-wrangler --update .\n"
            "  upgrade to current stable:  claude-wrangler --upgrade --channel stable .\n"
            "  upgrade to exact version:   claude-wrangler --upgrade --channel stable --version 2.1.153 .\n"
            "  enable reasonable perms:    claude-wrangler --set-reasonable-permissions .\n"
            "  inspect current state:      claude-wrangler --inspect .\n"
            "  uninstall managed setup:    claude-wrangler --uninstall .\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    operation_group = parser.add_mutually_exclusive_group()
    operation_group.add_argument(
        "--inspect",
        action="store_true",
        help="Emit a JSON report describing the managed Claude state.",
    )
    operation_group.add_argument(
        "--selftest",
        action="store_true",
        help=(
            "Perform pessimistic verification of the managed setup. This runs "
            "local commands such as the launcher version check and npm audit."
        ),
    )
    operation_group.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "Remove managed artifacts when the script can prove ownership. "
            "This mode is intentionally conservative."
        ),
    )
    operation_group.add_argument(
        "--update",
        action="store_true",
        help=(
            "Refresh the locally known stable, latest, and next Claude Code "
            "versions without changing the installed package."
        ),
    )
    operation_group.add_argument(
        "--upgrade",
        action="store_true",
        help=(
            "Upgrade the managed install to one explicitly chosen channel. "
            "Requires --channel."
        ),
    )

    parser.add_argument(
        "project_root",
        nargs="?",
        default=".",
        help="Target project directory. Defaults to the current directory.",
    )
    parser.add_argument(
        "--channel",
        choices=CLAUDE_CHANNELS,
        help=(
            "Claude release channel for install or upgrade. Required for "
            "--upgrade. Defaults to stable for install."
        ),
    )
    parser.add_argument(
        "--version",
        dest="requested_version",
        default=None,
        help=(
            "Exact @anthropic-ai/claude-code version to install or upgrade "
            "to, or `latest` (case-insensitive) for the current version of "
            "the selected channel. Default: latest"
        ),
    )
    parser.add_argument(
        "--local-dir",
        default=DEFAULT_LOCAL_DIR,
        help=(
            "Directory, relative to the project root, for the isolated npm "
            "install. Default: {}".format(DEFAULT_LOCAL_DIR)
        ),
    )
    parser.add_argument(
        "--claude-home-dir",
        default=DEFAULT_HOME_DIR,
        help=(
            "Directory, relative to the project root, used as the redirected "
            "HOME when isolation mode is enabled. Default: {}".format(DEFAULT_HOME_DIR)
        ),
    )
    parser.add_argument(
        "--claude-config-dir",
        default=DEFAULT_CONFIG_DIR,
        help=(
            "Directory, relative to the project root, pinned as "
            "CLAUDE_CONFIG_DIR for all launcher invocations. Default: "
            "{}".format(DEFAULT_CONFIG_DIR)
        ),
    )
    parser.add_argument(
        "--launcher",
        default=str(DEFAULT_LAUNCHER_RELATIVE_PATH),
        help=(
            "Path, relative to the project root, for the generated launcher. "
            "Default: {}".format(DEFAULT_LAUNCHER_RELATIVE_PATH)
        ),
    )
    parser.add_argument(
        "--readme-local",
        default=DEFAULT_README_FILENAME,
        help=(
            "Filename, relative to the project root, for the generated local "
            "operator README. Default: {}".format(DEFAULT_README_FILENAME)
        ),
    )

    home_group = parser.add_mutually_exclusive_group()
    home_group.add_argument(
        "--shared-home",
        action="store_true",
        default=None,
        help=(
            "Default the launcher to the operator's real HOME (security "
            "ring 0) instead of the project-local redirected HOME. Claude "
            "session state stays pinned under the managed config dir either "
            "way."
        ),
    )
    home_group.add_argument(
        "--isolated-home",
        action="store_false",
        dest="shared_home",
        default=None,
        help=(
            "Default the launcher to the project-local redirected HOME "
            "(security ring 1). This is the default when no prior managed "
            "install exists."
        ),
    )

    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Write managed files but skip npm install.",
    )
    parser.add_argument(
        "--repair-install",
        action="store_true",
        help=(
            "Before npm install, remove only managed npm install artifacts "
            "under the managed local dir: node_modules, package-lock.json, "
            "and the npm scratch caches. This does not remove Claude session "
            "state."
        ),
    )
    parser.add_argument(
        "--npm-timeout-seconds",
        type=parse_positive_int,
        default=DEFAULT_NPM_TIMEOUT_SECONDS,
        help=(
            "Timeout for managed npm operations in seconds. Default: {}".format(
                DEFAULT_NPM_TIMEOUT_SECONDS
            )
        ),
    )
    parser.add_argument(
        "--npm-install-loglevel",
        choices=NPM_INSTALL_LOGLEVELS,
        default=DEFAULT_NPM_INSTALL_LOGLEVEL,
        help=(
            "npm log level for managed package installation. Default: {}".format(
                DEFAULT_NPM_INSTALL_LOGLEVEL
            )
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Override conservative safety checks. This is mainly relevant "
            "when replacing existing generated files or forcing removal of "
            "stale managed artifacts."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Describe the actions that would be taken without mutating the "
            "filesystem or running npm install/uninstall commands."
        ),
    )
    reasonable_permissions_group = parser.add_mutually_exclusive_group()
    reasonable_permissions_group.add_argument(
        "--set-reasonable-permissions",
        action="store_true",
        help=(
            "Persist a managed launcher default that adds `--permission-mode "
            "acceptEdits` unless the caller already selected permission "
            "behavior."
        ),
    )
    reasonable_permissions_group.add_argument(
        "--clear-reasonable-permissions",
        action="store_true",
        help="Clear the managed launcher default for permission behavior.",
    )

    args = parser.parse_args(argv)
    args.requested_version = normalize_requested_version(args.requested_version)

    if args.update and args.channel:
        parser.error("--channel is not valid with --update.")
    if args.update and args.requested_version is not None:
        parser.error("--version is not valid with --update.")
    validate_channel_version_pair(parser, args.channel, args.requested_version)
    if args.upgrade and not args.channel:
        parser.error("--upgrade requires --channel <stable|latest|next>.")
    if args.upgrade and args.skip_install:
        parser.error("--skip-install is not valid with --upgrade.")
    if args.repair_install and args.skip_install:
        parser.error("--repair-install is not valid with --skip-install.")
    if args.repair_install and (
        args.inspect or args.selftest or args.uninstall or args.update
    ):
        parser.error(
            "--repair-install is only valid for install and upgrade operations."
        )
    if (args.set_reasonable_permissions or args.clear_reasonable_permissions) and (
        args.inspect or args.selftest or args.uninstall or args.update
    ):
        parser.error(
            "--set-reasonable-permissions and --clear-reasonable-permissions "
            "are only valid for install-like operations."
        )
    return args


def determine_operation(args: argparse.Namespace) -> str:
    """Translate argparse flags into one internal operation string."""

    if args.inspect:
        return "inspect"
    if args.selftest:
        return "selftest"
    if args.uninstall:
        return "uninstall"
    if args.update:
        return "update"
    if args.upgrade:
        return "upgrade"
    return "install"


def determine_shared_home(
    args: argparse.Namespace,
    existing: ExistingState,
) -> bool:
    """Decide whether the launcher should default to shared or isolated HOME."""

    if args.shared_home is not None:
        return bool(args.shared_home)
    if existing.shared_home is not None:
        return existing.shared_home
    return False


def determine_reasonable_permissions(
    args: argparse.Namespace,
    existing: ExistingState,
) -> bool:
    """Decide whether the managed launcher should default permission flags."""

    if args.set_reasonable_permissions:
        return True
    if args.clear_reasonable_permissions:
        return False
    if existing.reasonable_permissions_enabled is not None:
        return existing.reasonable_permissions_enabled
    return False


def has_existing_managed_selection(existing: ExistingState) -> bool:
    """Return True when one prior managed install can be inferred safely."""

    return any(
        (
            existing.metadata is not None,
            existing.requested_claude_selector is not None,
            existing.claude_channel is not None,
            existing.pinned_claude_version is not None,
        )
    )


def is_reasonable_permissions_reconfigure(
    args: argparse.Namespace,
    existing: ExistingState,
    operation: str,
) -> bool:
    """Return True when the command should only rewrite managed files."""

    if operation != "install":
        return False
    if getattr(args, "repair_install", False):
        return False
    if not (args.set_reasonable_permissions or args.clear_reasonable_permissions):
        return False
    if args.channel is not None or args.requested_version is not None:
        return False
    return has_existing_managed_selection(existing)


def existing_claude_state(
    existing: ExistingState,
) -> Tuple[str, Optional[str], str, str]:
    """Return the currently recorded Claude selector, channel, version, and source."""

    selector = existing.requested_claude_selector or existing.pinned_claude_version
    if selector is None:
        selector = DEFAULT_INSTALL_CLAUDE_SELECTOR
    channel = (
        existing.claude_channel
        or infer_claude_channel(selector)
        or infer_claude_channel(existing.pinned_claude_version)
    )
    version = existing.pinned_claude_version or selector
    source = "existing managed files"
    if existing.metadata:
        source = "existing metadata"
        metadata_source = existing.metadata.get("version_source")
        if isinstance(metadata_source, str) and metadata_source:
            source = metadata_source
    return selector, channel, version, source


def determine_target_selection(
    args: argparse.Namespace,
    existing: ExistingState,
    operation: str,
) -> Tuple[str, Optional[str], str, str]:
    """Resolve the requested Claude selector, channel, version, and source."""

    if operation in ("inspect", "selftest", "uninstall", "update"):
        return existing_claude_state(existing)

    if is_reasonable_permissions_reconfigure(args, existing, operation):
        return existing_claude_state(existing)

    requested_version = args.requested_version
    if operation == "install":
        channel = args.channel
        if requested_version is None:
            requested_version = "latest"
        if channel is None:
            channel = (
                infer_claude_channel(requested_version)
                or DEFAULT_INSTALL_CLAUDE_CHANNEL
            )
        if requested_version == "latest":
            source = "default {} channel request".format(channel)
            if args.channel is not None:
                source = "explicit --channel {} request".format(channel)
            return "latest", channel, "latest", source
        return requested_version, channel, requested_version, "explicit --version"

    if operation == "upgrade":
        channel = args.channel
        if channel is None:
            raise ClaudeWranglerError(
                "Upgrade requires --channel <stable|latest|next>."
            )
        if requested_version is None:
            requested_version = "latest"
        if requested_version == "latest":
            return (
                "latest",
                channel,
                "latest",
                "latest request for {} channel".format(channel),
            )
        return (
            requested_version,
            channel,
            requested_version,
            "explicit --version for {} channel".format(channel),
        )

    raise ClaudeWranglerError("Unknown operation: {}".format(operation))


def config_from_args(args: argparse.Namespace) -> Config:
    """Normalize parsed arguments into the internal configuration model."""

    project_root = resolve_config_project_root(args.project_root)
    layout = build_layout(
        project_root=project_root,
        local_dir_raw=args.local_dir,
        claude_home_raw=args.claude_home_dir,
        config_dir_raw=args.claude_config_dir,
        launcher_raw=args.launcher,
        readme_raw=args.readme_local,
    )
    operation = determine_operation(args)
    existing = read_existing_state(layout)
    shared_home = determine_shared_home(args, existing)
    reasonable_permissions_enabled = determine_reasonable_permissions(args, existing)
    reconfigure_only = is_reasonable_permissions_reconfigure(args, existing, operation)
    claude_selector, claude_channel, claude_version, version_source = (
        determine_target_selection(args, existing, operation)
    )

    return Config(
        operation=operation,
        project_root=project_root,
        claude_selector=claude_selector,
        claude_channel=claude_channel,
        claude_version=claude_version,
        shared_home=shared_home,
        skip_install=bool(args.skip_install),
        force=bool(args.force),
        dry_run=bool(args.dry_run),
        layout=layout,
        version_source=version_source,
        reasonable_permissions_enabled=reasonable_permissions_enabled,
        reconfigure_only=reconfigure_only,
        repair_install=bool(args.repair_install),
        npm_timeout_seconds=args.npm_timeout_seconds,
        npm_install_loglevel=args.npm_install_loglevel,
        available_versions=dict(existing.available_versions),
        available_versions_updated_at=existing.available_versions_updated_at,
    )
