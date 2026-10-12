"""Native release catalog: channel resolution and notify-only update checks.

`--update` refreshes what claude-wrangler knows about both channels without
touching the installed binary
(`native_runtime_distribution_and_verification.md` requirement 12): each
unsigned channel pointer only selects which signed manifest to fetch, and a
channel counts as known only after that manifest's signatures verify. The
result goes into metadata (`available_versions`,
`available_version_details`, `last_update_check`) and into the launcher's
`update-check.json` cache for the installed channel.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Sequence

from .constants import CLAUDE_CHANNELS
from .distribution import DEFAULT_RELEASE_BASE_URL, is_newer_version
from .models import ClaudeWranglerError, Config


@dataclass(frozen=True)
class ChannelCheck:
    """The verified answer (or the error) for one channel during a refresh."""

    channel: str
    version: Optional[str]
    build_date: Optional[str]
    commit: Optional[str]
    error: Optional[str]

    @property
    def ok(self) -> bool:
        """Return True when the channel's signed manifest verified."""

        return self.error is None and self.version is not None


def check_channel(source: Any, channel: str) -> ChannelCheck:
    """Resolve a channel and verify its signed manifest; never raises."""

    try:
        version = source.channel_version(channel)
        bundle = source.fetch_manifest_bundle(version)
    except ClaudeWranglerError as exc:
        return ChannelCheck(channel, None, None, None, str(exc) or type(exc).__name__)
    verified = bundle.verified
    return ChannelCheck(
        channel, verified.version, verified.build_date, verified.commit, None
    )


def refresh_catalog(
    source: Any,
    channels: Sequence[str] = CLAUDE_CHANNELS,
) -> Dict[str, ChannelCheck]:
    """Check every channel; failures are recorded per channel, not raised."""

    return {channel: check_channel(source, channel) for channel in channels}


def apply_catalog(
    config: Config,
    results: Dict[str, ChannelCheck],
    checked_at: str,
) -> Config:
    """Return a config whose catalog reflects a refresh.

    Verified channels replace their entries; a failed channel keeps its
    previous answer. `last_update_check` records when the refresh ran,
    against which release base URL, and every per-channel error.
    """

    versions = dict(config.available_versions)
    details = {
        key: dict(value) for key, value in config.available_version_details.items()
    }
    errors: Dict[str, str] = {}
    for channel, result in results.items():
        if result.ok:
            versions[channel] = result.version
            details[channel] = {
                "version": result.version,
                "build_date": result.build_date,
                "commit": result.commit,
                "verified_at": checked_at,
            }
        else:
            errors[channel] = result.error or "unknown error"
    any_ok = any(result.ok for result in results.values())
    return replace(
        config,
        available_versions=versions,
        available_version_details=details,
        available_versions_updated_at=(
            checked_at if any_ok else config.available_versions_updated_at
        ),
        last_update_check={
            "checked_at": checked_at,
            "release_base_url": config.release_base_url or DEFAULT_RELEASE_BASE_URL,
            "ok": not errors,
            "errors": errors,
        },
    )


def update_check_cache(
    config: Config,
    results: Dict[str, ChannelCheck],
    checked_at: str,
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the `update-check.json` document for the installed channel.

    It uses the launcher core's format (`checked_at`, `channel`,
    `latest_version`, `pointer_version`, `installed_version`, `error`), so
    the launcher's once-a-day refresh is satisfied by an explicit
    `--update`. `latest_version` only ever holds a version whose signed
    manifest verified; the launcher records its own unsigned pointer reads
    in `pointer_version` and never overwrites `latest_version`
    (`native_runtime_distribution_and_verification.md` requirement 2).
    """

    channel = config.claude_channel or "stable"
    result = results.get(channel)
    same_channel = bool(previous) and previous.get("channel") == channel
    latest: Optional[str] = None
    pointer: Optional[str] = None
    error: Optional[str] = None
    if result is not None and result.ok:
        latest = result.version
        # The pointer named this version and its manifest verified.
        pointer = result.version
    else:
        error = (result.error if result is not None else None) or (
            "channel {} was not checked".format(channel)
        )
        if same_channel:
            kept = previous.get("latest_version")
            latest = kept if isinstance(kept, str) else None
            kept_pointer = previous.get("pointer_version")
            pointer = kept_pointer if isinstance(kept_pointer, str) else None
        if latest is None:
            latest = config.available_versions.get(channel)
    return {
        "checked_at": checked_at,
        "channel": channel,
        "latest_version": latest,
        "pointer_version": pointer,
        "installed_version": config.claude_version or "",
        "error": error,
    }


def newer_release_note(config: Config) -> Optional[str]:
    """Return a one-line upgrade hint when the catalog names a newer release."""

    channel = config.claude_channel
    installed = config.claude_version
    candidate = config.available_versions.get(channel or "")
    if not channel or not installed or not candidate:
        return None
    try:
        newer = is_newer_version(candidate, installed)
    except ClaudeWranglerError:
        return None
    if not newer:
        return None
    return (
        "Claude Code {} is available on the {} channel (installed {}). Run "
        "`claude-wrangler --upgrade --channel {} .` to install it.".format(
            candidate, channel, installed, channel
        )
    )
