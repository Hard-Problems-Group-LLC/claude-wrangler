"""Argument parsing and configuration normalization."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Dict, Optional, Sequence, Tuple

from .constants import (
    CLI_CHANNEL_CHOICES,
    CONFIG_DIR_SPELLING_FORWARD_SLASH,
    CONFIG_DIR_SPELLING_NATIVE,
    DEFAULT_CONFIG_DIR,
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_HOME_DIR,
    DEFAULT_INSTALL_CLAUDE_CHANNEL,
    DEFAULT_INSTALL_CLAUDE_SELECTOR,
    DEFAULT_LAUNCHER_RELATIVE_PATH,
    DEFAULT_LOCAL_DIR,
    DEFAULT_README_FILENAME,
    LEGACY_LOCAL_DIR,
    METADATA_FILENAME,
)
from .distribution import (
    DEFAULT_RELEASE_BASE_URL,
    VERSION_FLOOR,
    DistributionError,
    channel_deprecation_message,
    check_version_floor,
    is_exact_version,
    normalize_channel,
    release_hosts,
)
from .layout import (
    build_layout,
    managed_state_fingerprint,
    read_existing_state,
    resolve_project_root,
    resolve_relative_within_root,
)
from .migration import (
    discover_legacy,
    legacy_dir_candidates,
    legacy_dir_from_launcher,
    looks_like_legacy_dir,
    npm_era_launcher_paths,
    unaliased_dir_within_root,
)
from .models import (
    ClaudeWranglerError,
    Config,
    ExistingState,
    LegacyState,
    ManagedStateChangedError,
)
from .platforms import PLATFORM_KEYS
from .runtime import eprint

MISSING_DASH_FLAG_HINTS = {
    "allow-tracked-state": "--allow-tracked-state",
    "ca-bundle": "--ca-bundle",
    "channel": "--channel",
    "claude-config-dir": "--claude-config-dir",
    "claude-home-dir": "--claude-home-dir",
    "clear-reasonable-permissions": "--clear-reasonable-permissions",
    "download-timeout-seconds": "--download-timeout-seconds",
    "dry-run": "--dry-run",
    "force": "--force",
    "inspect": "--inspect",
    "isolated-home": "--isolated-home",
    "launcher": "--launcher",
    "local-dir": "--local-dir",
    "npm-install-loglevel": "--npm-install-loglevel",
    "npm-timeout-seconds": "--npm-timeout-seconds",
    "platform": "--platform",
    "readme-local": "--readme-local",
    "release-base-url": "--release-base-url",
    "repair-install": "--repair-install",
    "rollback": "--rollback",
    "selftest": "--selftest",
    "set-reasonable-permissions": "--set-reasonable-permissions",
    "set-update-checks": "--set-update-checks",
    "set-windows-sandbox-warning": "--set-windows-sandbox-warning",
    "shared-home": "--shared-home",
    "skip-install": "--skip-install",
    "stop-background-sessions": "--stop-background-sessions",
    "uninstall": "--uninstall",
    "update": "--update",
    "upgrade": "--upgrade",
    "version": "--version",
}

# Operations that change nothing but report, and operations that never
# accept install-time settings.
REPORT_OPERATIONS = ("inspect", "selftest")
NON_INSTALL_OPERATIONS = ("inspect", "selftest", "uninstall", "update", "rollback")
ON_OFF = ("on", "off")


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


def build_parser() -> argparse.ArgumentParser:
    """Return the claude-wrangler argument parser."""

    parser = argparse.ArgumentParser(
        prog="claude-wrangler",
        description=(
            "Manage a project-local native Claude Code runtime: signed, "
            "verified releases in A/B slots, a generated cross-platform "
            "launcher, JSON inspection, and conservative uninstall "
            "semantics. Node.js and npm are not required."
        ),
        epilog=(
            "Examples:\n"
            "  install current stable:     claude-wrangler .\n"
            "  install current latest:     claude-wrangler --channel latest .\n"
            "  refresh known versions:     claude-wrangler --update .\n"
            "  upgrade to current stable:  claude-wrangler --upgrade --channel stable .\n"
            "  upgrade to exact version:   claude-wrangler --upgrade --channel stable "
            "--version 2.1.287 .\n"
            "  roll back one release:      claude-wrangler --rollback .\n"
            "  repair the runtime:         claude-wrangler --repair-install .\n"
            "  enable reasonable perms:    claude-wrangler --set-reasonable-permissions .\n"
            "  inspect current state:      claude-wrangler --inspect .\n"
            "  uninstall managed setup:    claude-wrangler --uninstall .\n"
            "\n"
            "Releases come from https://downloads.claude.ai/claude-code-releases;\n"
            "each manifest's OpenPGP and raw RSA signatures are verified against\n"
            "keys compiled into claude-wrangler. The oldest installable version is\n"
            "{}. Projects installed by npm-based releases (.local/claude/npm) are\n"
            "migrated automatically by install, upgrade, and repair.\n".format(
                VERSION_FLOOR
            )
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
            "Perform pessimistic verification of the managed setup: offline "
            "signature and SHA-256 re-verification of both runtime slots, a "
            "scratch-environment version check, and generated-file checks."
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
            "Refresh the locally known stable and latest Claude Code versions "
            "from signed manifests without changing the installed runtime."
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
    operation_group.add_argument(
        "--rollback",
        action="store_true",
        help=(
            "Make the other runtime slot (the previous release) active after "
            "an in-use check and a full offline re-verification."
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
        choices=CLI_CHANNEL_CHOICES,
        help=(
            "Claude release channel for install or upgrade. Required for "
            "--upgrade. Defaults to stable for a new install. `next` is a "
            "deprecated alias of `latest`."
        ),
    )
    parser.add_argument(
        "--version",
        dest="requested_version",
        default=None,
        help=(
            "Exact Claude Code version (MAJOR.MINOR.PATCH, {} or newer) to "
            "install or upgrade to, or `latest` (case-insensitive) for the "
            "current version of the selected channel. Default: latest for a "
            "new install; an existing install keeps its pinned version."
            "".format(VERSION_FLOOR)
        ),
    )
    parser.add_argument(
        "--local-dir",
        default=None,
        help=(
            "Directory, relative to the project root, for the native runtime "
            "root (slots, launcher core, metadata). Default: {}. A directory "
            "holding an npm-era layout is treated as the legacy layout to "
            "migrate from.".format(DEFAULT_LOCAL_DIR)
        ),
    )
    parser.add_argument(
        "--claude-home-dir",
        default=None,
        help=(
            "Directory, relative to the project root, used as the redirected "
            "HOME when isolation mode is enabled. Default: the recorded value "
            "or {}".format(DEFAULT_HOME_DIR)
        ),
    )
    parser.add_argument(
        "--claude-config-dir",
        default=None,
        help=(
            "Directory, relative to the project root, pinned as "
            "CLAUDE_CONFIG_DIR for all launcher invocations. Default: the "
            "recorded value or {}".format(DEFAULT_CONFIG_DIR)
        ),
    )
    parser.add_argument(
        "--launcher",
        default=None,
        help=(
            "Path, relative to the project root, for the generated POSIX "
            "launcher; the cmd launcher gets the same path plus .cmd. "
            "Default: the recorded value or {}".format(
                DEFAULT_LAUNCHER_RELATIVE_PATH.as_posix()
            )
        ),
    )
    parser.add_argument(
        "--readme-local",
        default=None,
        help=(
            "Filename, relative to the project root, for the generated local "
            "operator README. Default: the recorded value or {}".format(
                DEFAULT_README_FILENAME
            )
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
        "--platform",
        choices=PLATFORM_KEYS,
        default=None,
        help=(
            "Override platform detection with a release platform key for "
            "this operating system (for example win32-x64 under emulation "
            "on ARM64). Another operating system's build is refused."
        ),
    )
    parser.add_argument(
        "--release-base-url",
        default=None,
        help=(
            "HTTPS base URL of the release distribution (a mirror of {}). "
            "Its host is added to the download allowlist and the value is "
            "recorded for update checks.".format(DEFAULT_RELEASE_BASE_URL)
        ),
    )
    parser.add_argument(
        "--ca-bundle",
        default=None,
        help=(
            "PEM file of trusted certificate authorities for HTTPS; replaces "
            "the system store. SSL_CERT_FILE is honored when this is absent."
        ),
    )
    parser.add_argument(
        "--download-timeout-seconds",
        type=parse_positive_int,
        default=None,
        help=(
            "Connect/read timeout for each HTTPS request in seconds. "
            "Default: {}".format(DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)
        ),
    )
    parser.add_argument(
        "--stop-background-sessions",
        action="store_true",
        help=(
            "Allow `claude daemon stop --any` with the project's launcher "
            "environment when a runtime slot that must be displaced or "
            "removed is in use. This also terminates the project's "
            "background sessions."
        ),
    )
    parser.add_argument(
        "--allow-tracked-state",
        action="store_true",
        help=(
            "Proceed even when git tracks files under the runtime root, the "
            "config dir, the redirected HOME, the npm-era layout, or the "
            "launchers. By default install, upgrade, repair, --update, "
            "--rollback, the settings toggles, and --skip-install refuse, "
            "because a clone or pull can replace such files with content "
            "claude-wrangler never verified."
        ),
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help=(
            "Write managed files (launcher, README, metadata) but do not "
            "download, verify, or promote a runtime, and never migrate."
        ),
    )
    parser.add_argument(
        "--repair-install",
        action="store_true",
        help=(
            "Clear staging, delete retired slots no longer in use, re-verify "
            "both slots offline, repair an invalid active-slot pointer, "
            "re-download the pinned version when the active slot is missing "
            "or corrupt, and regenerate managed files. Claude session state "
            "is never removed."
        ),
    )
    parser.add_argument(
        "--npm-timeout-seconds",
        type=parse_positive_int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--npm-install-loglevel",
        default=None,
        help=argparse.SUPPRESS,
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
            "filesystem, downloading, or contacting the release server."
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
    parser.add_argument(
        "--set-update-checks",
        choices=ON_OFF,
        default=None,
        help=(
            "Turn the launcher's notify-only update checks on or off for this "
            "project. `claude-local --check-updates` still works when off."
        ),
    )
    parser.add_argument(
        "--set-windows-sandbox-warning",
        choices=ON_OFF,
        default=None,
        help=(
            "Turn the native-Windows note that Claude Code runs commands "
            "unsandboxed on or off for this project."
        ),
    )
    return parser


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
    if getattr(args, "rollback", False):
        return "rollback"
    return "install"


def _install_setting_flags(args: argparse.Namespace) -> Tuple[str, ...]:
    """Return the install-time setting flags given on the command line."""

    flags = []
    if args.set_reasonable_permissions:
        flags.append("--set-reasonable-permissions")
    if args.clear_reasonable_permissions:
        flags.append("--clear-reasonable-permissions")
    if args.set_update_checks is not None:
        flags.append("--set-update-checks")
    if args.set_windows_sandbox_warning is not None:
        flags.append("--set-windows-sandbox-warning")
    return tuple(flags)


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject incoherent flag combinations with argparse errors (exit 2)."""

    operation = determine_operation(args)

    if args.requested_version not in (None, "latest"):
        if not is_exact_version(args.requested_version):
            parser.error(
                "--version must be an exact MAJOR.MINOR.PATCH version or "
                "`latest`, not {!r}.".format(args.requested_version)
            )
        try:
            check_version_floor(args.requested_version)
        except DistributionError as exc:
            parser.error(str(exc))

    if args.update and args.channel:
        parser.error("--channel is not valid with --update.")
    if args.update and args.requested_version is not None:
        parser.error("--version is not valid with --update.")
    if args.upgrade and not args.channel:
        parser.error("--upgrade requires --channel <stable|latest>.")
    if args.upgrade and args.skip_install:
        parser.error("--skip-install is not valid with --upgrade.")
    if args.repair_install and args.skip_install:
        parser.error("--repair-install is not valid with --skip-install.")
    if args.repair_install and operation in NON_INSTALL_OPERATIONS:
        parser.error(
            "--repair-install is only valid for install and upgrade operations."
        )
    if operation == "rollback":
        for flag, given in (
            ("--channel", args.channel is not None),
            ("--version", args.requested_version is not None),
            ("--skip-install", args.skip_install),
            ("--platform", args.platform is not None),
        ):
            if given:
                parser.error("{} is not valid with --rollback.".format(flag))
    settings = _install_setting_flags(args)
    if settings and operation in NON_INSTALL_OPERATIONS:
        parser.error(
            "{} {} only valid for install-like operations.".format(
                " and ".join(settings), "is" if len(settings) == 1 else "are"
            )
        )
    if args.platform is not None and operation in NON_INSTALL_OPERATIONS:
        parser.error("--platform is only valid for install-like operations.")
    network_flags = [
        flag
        for flag, given in (
            ("--release-base-url", args.release_base_url is not None),
            ("--ca-bundle", args.ca_bundle is not None),
            ("--download-timeout-seconds", args.download_timeout_seconds is not None),
        )
        if given
    ]
    if network_flags and operation in ("inspect", "selftest", "uninstall", "rollback"):
        parser.error(
            "{} only applies to operations that contact the release server.".format(
                ", ".join(network_flags)
            )
        )
    if args.stop_background_sessions and operation in ("inspect", "selftest", "update"):
        parser.error(
            "--stop-background-sessions is only valid for install, upgrade, "
            "repair, rollback, and uninstall."
        )
    if getattr(args, "allow_tracked_state", False) and operation in (
        "inspect",
        "selftest",
        "uninstall",
    ):
        parser.error(
            "--allow-tracked-state is only valid for install, upgrade, repair, "
            "--update, --rollback, the settings toggles, and --skip-install."
        )
    if args.release_base_url is not None:
        try:
            release_hosts(args.release_base_url)
        except DistributionError as exc:
            parser.error(str(exc))
        args.release_base_url = args.release_base_url.strip().rstrip("/")
    if args.ca_bundle is not None:
        candidate = os.path.abspath(os.path.expanduser(args.ca_bundle))
        if not os.path.isfile(candidate):
            parser.error("--ca-bundle file does not exist: {}".format(args.ca_bundle))
        args.ca_bundle = candidate


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse and validate command-line arguments.

    `--channel next` is normalized to `latest` here with a deprecation
    warning on stderr. The retired npm flags are still accepted so old
    scripts keep working, but they only print a one-line warning.
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    args.requested_version = normalize_requested_version(args.requested_version)
    args.channel_deprecated = False
    if args.channel is not None:
        normalized, deprecated = normalize_channel(args.channel)
        if deprecated:
            eprint(channel_deprecation_message(args.channel.strip().lower()))
        args.channel = normalized
        args.channel_deprecated = deprecated
    if args.npm_timeout_seconds is not None:
        eprint(
            "Warning: --npm-timeout-seconds is ignored; claude-wrangler no longer "
            "runs npm. Use --download-timeout-seconds for release downloads."
        )
    if args.npm_install_loglevel is not None:
        eprint(
            "Warning: --npm-install-loglevel is ignored; claude-wrangler no "
            "longer runs npm."
        )
    validate_args(parser, args)
    return args


def determine_shared_home(
    args: argparse.Namespace,
    existing: ExistingState,
    legacy: Optional[LegacyState] = None,
) -> bool:
    """Decide whether the launcher should default to shared or isolated HOME."""

    if args.shared_home is not None:
        return bool(args.shared_home)
    if existing.shared_home is not None:
        return existing.shared_home
    if legacy is not None and legacy.shared_home is not None:
        return legacy.shared_home
    return False


def determine_reasonable_permissions(
    args: argparse.Namespace,
    existing: ExistingState,
    legacy: Optional[LegacyState] = None,
) -> bool:
    """Decide whether the managed launcher should default permission flags."""

    if args.set_reasonable_permissions:
        return True
    if args.clear_reasonable_permissions:
        return False
    if existing.reasonable_permissions_enabled is not None:
        return existing.reasonable_permissions_enabled
    if legacy is not None and legacy.reasonable_permissions_enabled is not None:
        return legacy.reasonable_permissions_enabled
    return False


def _on_off_setting(
    flag_value: Optional[str],
    recorded: Optional[bool],
    legacy_value: Optional[bool],
) -> bool:
    """Resolve an on/off project setting: flag, then recorded, then on."""

    if flag_value is not None:
        return flag_value == "on"
    if recorded is not None:
        return recorded
    if legacy_value is not None:
        return legacy_value
    return True


def has_existing_managed_selection(
    existing: ExistingState, legacy: Optional[LegacyState] = None
) -> bool:
    """Return True when one prior managed install can be inferred safely."""

    if any(
        (
            existing.metadata is not None,
            existing.requested_claude_selector is not None,
            existing.claude_channel is not None,
            existing.pinned_claude_version is not None,
        )
    ):
        return True
    return legacy is not None and (
        legacy.metadata is not None
        or legacy.selector is not None
        or legacy.version is not None
    )


def is_reasonable_permissions_reconfigure(
    args: argparse.Namespace,
    existing: ExistingState,
    operation: str,
    legacy: Optional[LegacyState] = None,
) -> bool:
    """Return True when the command should only rewrite managed files.

    A settings toggle (`--set-/--clear-reasonable-permissions`,
    `--set-update-checks`, `--set-windows-sandbox-warning`) on an existing
    install, without an install-changing request, preserves the recorded
    selection and never downloads, promotes, or migrates.
    """

    if operation != "install":
        return False
    if getattr(args, "repair_install", False):
        return False
    if not _install_setting_flags(args):
        return False
    if args.channel is not None or args.requested_version is not None:
        return False
    return has_existing_managed_selection(existing, legacy)


def existing_claude_state(
    existing: ExistingState,
    legacy: Optional[LegacyState] = None,
) -> Tuple[str, Optional[str], Optional[str], str]:
    """Return the recorded Claude selector, channel, version, and source.

    Native metadata wins; an npm-era layout is used only when no native
    metadata exists.
    """

    if existing.metadata is not None or legacy is None:
        selector = existing.requested_claude_selector or existing.pinned_claude_version
        channel = existing.claude_channel
        version = existing.pinned_claude_version
        source = existing.version_source or "existing metadata"
    else:
        selector = legacy.selector or legacy.version
        channel = legacy.channel
        version = legacy.version
        source = legacy.version_source or "legacy npm layout"
    if selector is None:
        selector = DEFAULT_INSTALL_CLAUDE_SELECTOR
    if selector != "latest" and not is_exact_version(selector):
        selector = version if version and is_exact_version(version) else "latest"
    if version is not None and not is_exact_version(version):
        version = None
    return selector, channel, version, source


def determine_target_selection(
    args: argparse.Namespace,
    existing: ExistingState,
    operation: str,
    legacy: Optional[LegacyState] = None,
) -> Tuple[str, Optional[str], Optional[str], str]:
    """Resolve the requested selector, channel, exact version, and source.

    The returned version is exact when it is already known (an explicit
    `--version` or a recorded pin) and None when the channel pointer must
    be resolved at install time. A plain install of an existing project
    keeps its pinned version; only `--channel`, `--version`, or
    `--upgrade` change it.
    """

    if operation in NON_INSTALL_OPERATIONS:
        return existing_claude_state(existing, legacy)
    if is_reasonable_permissions_reconfigure(args, existing, operation, legacy):
        return existing_claude_state(existing, legacy)

    requested_version = args.requested_version
    recorded_selector, recorded_channel, recorded_version, recorded_source = (
        existing_claude_state(existing, legacy)
    )
    has_existing = has_existing_managed_selection(existing, legacy)

    if operation == "install":
        channel = args.channel or recorded_channel or DEFAULT_INSTALL_CLAUDE_CHANNEL
        if requested_version is not None and requested_version != "latest":
            return requested_version, channel, requested_version, "explicit --version"
        if args.channel is not None or requested_version == "latest":
            source = "explicit --channel {} request".format(channel)
            if args.channel is None:
                source = "explicit --version latest for {} channel".format(channel)
            return "latest", channel, None, source
        if has_existing and recorded_version is not None:
            return recorded_selector, channel, recorded_version, recorded_source
        return "latest", channel, None, "default {} channel request".format(channel)

    if operation == "upgrade":
        channel = args.channel
        if channel is None:
            raise ClaudeWranglerError("Upgrade requires --channel <stable|latest>.")
        if requested_version is None or requested_version == "latest":
            return (
                "latest",
                channel,
                None,
                "latest request for {} channel".format(channel),
            )
        return (
            requested_version,
            channel,
            requested_version,
            "explicit --version for {} channel".format(channel),
        )

    raise ClaudeWranglerError("Unknown operation: {}".format(operation))


def _relative_or_default(
    explicit: Optional[str],
    recorded: Dict[str, str],
    legacy_recorded: Dict[str, str],
    key: str,
    default: str,
) -> str:
    """Pick a managed path: CLI flag, then native, then legacy, then default."""

    if explicit is not None:
        return explicit
    if key in recorded:
        return recorded[key]
    if key in legacy_recorded:
        return legacy_recorded[key]
    return default


def _safe_relative(project_root: Path, raw: str, label: str) -> Optional[Path]:
    """Return the resolved path for a relative spelling, or None if unsafe."""

    try:
        return resolve_relative_within_root(project_root, raw, label)[1]
    except ClaudeWranglerError:
        return None


def determine_config_dir_spelling(
    existing: ExistingState,
    legacy: Optional[LegacyState],
    os_name: Optional[str] = None,
) -> str:
    """Choose how the launcher spells CLAUDE_CONFIG_DIR.

    The recorded value wins. A project that still carries (or came from)
    an npm-era layout on native Windows keeps the forward-slash spelling
    its Git Bash launcher exported, so the value stays byte-identical
    (legacy_npm_layout_migration.md requirement 7).
    """

    if existing.config_dir_spelling is not None:
        return existing.config_dir_spelling
    effective_os = os.name if os_name is None else os_name
    if effective_os == "nt" and legacy is not None and existing.metadata is None:
        return CONFIG_DIR_SPELLING_FORWARD_SLASH
    return CONFIG_DIR_SPELLING_NATIVE


def config_from_args(args: argparse.Namespace) -> Config:
    """Normalize parsed arguments into the internal configuration model."""

    project_root = resolve_config_project_root(args.project_root)
    operation = determine_operation(args)

    # A --local-dir that names an npm-era directory is the legacy layout to
    # migrate from; the native runtime then lives at the default root. It
    # counts only when reached without links and carrying ownership
    # evidence, because install and uninstall delete a legacy npm tree.
    local_dir_raw = args.local_dir if args.local_dir is not None else DEFAULT_LOCAL_DIR
    explicit_legacy_dir: Optional[str] = None
    if args.local_dir is not None:
        found = unaliased_dir_within_root(project_root, args.local_dir, "--local-dir")
        if found is not None and looks_like_legacy_dir(found[1]):
            explicit_legacy_dir = args.local_dir
            local_dir_raw = DEFAULT_LOCAL_DIR

    probe_layout = build_layout(
        project_root=project_root,
        local_dir_raw=local_dir_raw,
        claude_home_raw=DEFAULT_HOME_DIR,
        config_dir_raw=DEFAULT_CONFIG_DIR,
        launcher_raw=DEFAULT_LAUNCHER_RELATIVE_PATH.as_posix(),
        readme_raw=DEFAULT_README_FILENAME,
    )
    existing = read_existing_state(probe_layout)
    # A custom npm-era --local-dir is recorded only inside that directory;
    # the project's generated launcher (or a migration-pending launch.json)
    # still names it (legacy_npm_layout_migration.md requirement 2).
    launcher_probe = _safe_relative(
        project_root,
        _relative_or_default(
            args.launcher,
            existing.recorded_paths,
            {},
            "launcher",
            DEFAULT_LAUNCHER_RELATIVE_PATH.as_posix(),
        ),
        "--launcher",
    )
    launcher_legacy_dir = (
        legacy_dir_from_launcher(launcher_probe, probe_layout.launch_config_path)
        if launcher_probe is not None
        else None
    )
    legacy_candidates = legacy_dir_candidates(
        explicit_legacy_dir,
        existing.legacy_migrated_from,
        launcher_legacy_dir,
        LEGACY_LOCAL_DIR,
    )
    legacy = discover_legacy(project_root, legacy_candidates)
    state_paths = [probe_layout.metadata_path]
    for candidate_dir in legacy_candidates:
        resolved = _safe_relative(project_root, candidate_dir, "legacy local dir")
        if resolved is not None:
            state_paths.append(resolved / METADATA_FILENAME)
    # The digest must describe the bytes this configuration was derived
    # from (ab_runtime_slots.md requirement 4). It is computed first and the
    # state read again afterwards: when another maintainer published after
    # the first read, the second read differs and the command reruns from a
    # fresh read instead of carrying a digest of state it never parsed.
    state_fingerprint = managed_state_fingerprint(state_paths)
    if _state_signature(
        read_existing_state(probe_layout),
        discover_legacy(project_root, legacy_candidates),
    ) != _state_signature(existing, legacy):
        raise ManagedStateChangedError(
            "Another claude-wrangler run changed this project's managed state "
            "while this command read it; nothing was changed."
        )
    legacy_paths: Dict[str, str] = {}
    if legacy is not None:
        # The npm-era launcher built CLAUDE_CONFIG_DIR and HOME from these
        # suffixes; they fill in what legacy metadata does not record, so
        # the migrated launcher exports the same value (requirement 7).
        launcher_paths = (
            npm_era_launcher_paths(launcher_probe) if launcher_probe is not None else {}
        )
        if launcher_paths.get("local_dir") == legacy.dir_relative:
            legacy_paths.update(launcher_paths)
        legacy_paths.update(legacy.recorded_paths)

    layout = build_layout(
        project_root=project_root,
        local_dir_raw=local_dir_raw,
        claude_home_raw=_relative_or_default(
            args.claude_home_dir,
            existing.recorded_paths,
            legacy_paths,
            "claude_home_dir",
            DEFAULT_HOME_DIR,
        ),
        config_dir_raw=_relative_or_default(
            args.claude_config_dir,
            existing.recorded_paths,
            legacy_paths,
            "claude_config_dir",
            DEFAULT_CONFIG_DIR,
        ),
        launcher_raw=_relative_or_default(
            args.launcher,
            existing.recorded_paths,
            legacy_paths,
            "launcher",
            DEFAULT_LAUNCHER_RELATIVE_PATH.as_posix(),
        ),
        readme_raw=_relative_or_default(
            args.readme_local,
            existing.recorded_paths,
            legacy_paths,
            "readme_local",
            DEFAULT_README_FILENAME,
        ),
    )

    shared_home = determine_shared_home(args, existing, legacy)
    reasonable_permissions_enabled = determine_reasonable_permissions(
        args, existing, legacy
    )
    reconfigure_only = is_reasonable_permissions_reconfigure(
        args, existing, operation, legacy
    )
    claude_selector, claude_channel, claude_version, version_source = (
        determine_target_selection(args, existing, operation, legacy)
    )
    if claude_channel is None:
        claude_channel = DEFAULT_INSTALL_CLAUDE_CHANNEL

    release_base_url = (
        args.release_base_url
        or existing.release_base_url
        or (legacy.release_base_url if legacy is not None else None)
        or DEFAULT_RELEASE_BASE_URL
    )
    # Report and mutate with the same launcher interpreter the shims carry;
    # only operations that rewrite the shims record the current one.
    launcher_python = sys.executable
    if operation in ("inspect", "selftest", "uninstall") and existing.launcher_python:
        launcher_python = existing.launcher_python

    available_versions = dict(existing.available_versions)
    available_versions_updated_at = existing.available_versions_updated_at
    if existing.metadata is None and legacy is not None:
        available_versions = {
            key: value
            for key, value in legacy.available_versions.items()
            if key in ("stable", "latest")
        }
        available_versions_updated_at = legacy.available_versions_updated_at

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
        update_checks=_on_off_setting(
            args.set_update_checks,
            existing.update_checks,
            legacy.update_checks if legacy is not None else None,
        ),
        windows_sandbox_warning=_on_off_setting(
            args.set_windows_sandbox_warning,
            existing.windows_sandbox_warning,
            legacy.windows_sandbox_warning if legacy is not None else None,
        ),
        release_base_url=release_base_url,
        ca_bundle=args.ca_bundle,
        download_timeout_seconds=(
            args.download_timeout_seconds or DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
        ),
        platform_override=args.platform,
        platform_key=existing.platform,
        stop_background_sessions=bool(args.stop_background_sessions),
        channel_explicit=args.channel is not None,
        version_explicit=args.requested_version is not None,
        launcher_python=launcher_python,
        config_dir_spelling=determine_config_dir_spelling(existing, legacy),
        available_versions=available_versions,
        available_versions_updated_at=available_versions_updated_at,
        available_version_details=dict(existing.available_version_details),
        last_update_check=existing.last_update_check,
        legacy_migrated_from=existing.legacy_migrated_from,
        legacy_migrated_at=existing.legacy_migrated_at,
        existing_metadata=existing.metadata,
        existing_metadata_error=existing.metadata_error,
        legacy=legacy,
        state_paths=state_paths,
        state_fingerprint=state_fingerprint,
        release_base_url_explicit=args.release_base_url is not None,
        allow_tracked_state=bool(getattr(args, "allow_tracked_state", False)),
    )


def _state_signature(
    existing: ExistingState, legacy: Optional[LegacyState]
) -> Tuple[object, ...]:
    """Return what a configuration takes from the metadata files it read."""

    return (
        existing.metadata,
        existing.metadata_error,
        legacy.dir_relative if legacy is not None else None,
        legacy.metadata if legacy is not None else None,
        legacy.metadata_error if legacy is not None else None,
    )
