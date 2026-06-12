"""Release-channel helpers for resolving Claude Code package versions."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Dict, Mapping, Optional

from .constants import CLAUDE_CHANNELS, CLAUDE_PACKAGE_NAME
from .models import ClaudeWranglerError, Config
from .runtime import run_command, utc_now_iso

# Claude Code's npm dist-tags map one-to-one onto the supported channels.
CHANNEL_TO_SELECTOR = {
    "stable": "stable",
    "latest": "latest",
    "next": "next",
}
EXACT_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$")


def is_exact_version(version_or_selector: str) -> bool:
    """Return True when one selector string looks like an exact package version."""

    return EXACT_VERSION_PATTERN.fullmatch(version_or_selector.strip()) is not None


def infer_claude_channel(version_or_selector: Optional[str]) -> Optional[str]:
    """Infer the Claude release channel from one selector string.

    Claude Code versions carry no channel marker (`next` builds are plain
    semver too), so exact versions never imply a channel here. Callers fall
    back to the default channel or an explicit `--channel` instead. The
    generic selector word `latest` is likewise channel-agnostic; only the
    channel-specific words `stable` and `next` identify a channel.
    """

    if not version_or_selector:
        return None
    normalized = version_or_selector.strip().lower()
    if normalized in ("stable", "next"):
        return normalized
    return None


def selector_for_channel(channel: str) -> str:
    """Return the npm dist-tag associated with one supported channel."""

    try:
        return CHANNEL_TO_SELECTOR[channel]
    except KeyError as exc:
        raise ClaudeWranglerError(
            "Unsupported Claude channel: {}".format(channel)
        ) from exc


def parse_dist_tags(payload: str) -> Dict[str, str]:
    """Parse and validate one `npm view ... dist-tags --json` payload."""

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ClaudeWranglerError(
            "Failed to parse npm dist-tags for {}.".format(CLAUDE_PACKAGE_NAME)
        ) from exc
    if not isinstance(data, dict):
        raise ClaudeWranglerError(
            "npm dist-tags for {} did not return a JSON object.".format(
                CLAUDE_PACKAGE_NAME
            )
        )
    tags: Dict[str, str] = {}
    for raw_tag, raw_version in data.items():
        if isinstance(raw_tag, str) and isinstance(raw_version, str):
            tags[raw_tag] = raw_version
    if not tags:
        raise ClaudeWranglerError(
            "npm dist-tags for {} did not include any usable selectors.".format(
                CLAUDE_PACKAGE_NAME
            )
        )
    return tags


def fetch_claude_dist_tags(
    npm_name: str,
    project_root: Path,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, str]:
    """Query npm for the published dist-tags of the Claude Code package."""

    completed = run_command(
        [npm_name, "view", CLAUDE_PACKAGE_NAME, "dist-tags", "--json"],
        cwd=str(project_root),
        capture_output=True,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    return parse_dist_tags(completed.stdout or "")


def fetch_available_claude_versions(
    npm_name: str,
    project_root: Path,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Optional[str]]:
    """Return the locally tracked latest versions for the supported channels."""

    dist_tags = fetch_claude_dist_tags(
        npm_name,
        project_root,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    return {
        channel: dist_tags.get(CHANNEL_TO_SELECTOR[channel])
        for channel in CLAUDE_CHANNELS
    }


def resolve_explicit_selector_version(
    selector: str,
    channel: Optional[str],
    available_versions: Dict[str, Optional[str]],
) -> str:
    """Resolve one explicit install selector against the available-version map."""

    if is_exact_version(selector):
        inferred_channel = infer_claude_channel(selector)
        if (
            channel is not None
            and inferred_channel is not None
            and inferred_channel != channel
        ):
            raise ClaudeWranglerError(
                "Exact version {} does not match the {} channel.".format(
                    selector,
                    channel,
                )
            )
        return selector
    inferred_channel = channel or infer_claude_channel(selector)
    if inferred_channel is None:
        raise ClaudeWranglerError(
            "Claude selector {!r} is not an exact version or a supported "
            "channel/dist-tag.".format(selector)
        )
    resolved_version = available_versions.get(inferred_channel)
    if not resolved_version:
        raise ClaudeWranglerError(
            "No published version is currently known for the {} channel. Run "
            "`claude-wrangler --update` again later.".format(inferred_channel)
        )
    return resolved_version


def resolve_install_version(
    config: Config,
    npm_name: str,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> Config:
    """Resolve an install-time selector into an exact version and catalog snapshot."""

    available_versions = fetch_available_claude_versions(
        npm_name,
        config.project_root,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    resolved_version = resolve_explicit_selector_version(
        config.claude_selector,
        config.claude_channel,
        available_versions,
    )
    return replace(
        config,
        claude_version=resolved_version,
        claude_channel=config.claude_channel
        or infer_claude_channel(config.claude_selector),
        available_versions=available_versions,
        available_versions_updated_at=utc_now_iso(),
    )


def resolve_upgrade_version(config: Config) -> Config:
    """Resolve an upgrade request from the locally known version catalog."""

    channel = config.claude_channel
    if channel is None:
        raise ClaudeWranglerError(
            "Upgrade requires a Claude channel. Use `--channel <stable|latest|next>`."
        )
    if channel not in CLAUDE_CHANNELS:
        raise ClaudeWranglerError("Unsupported Claude channel: {}".format(channel))
    if config.claude_selector == "latest":
        resolved_version = config.available_versions.get(channel)
        if not resolved_version:
            raise ClaudeWranglerError(
                "No known version is recorded for the {} channel. Run "
                "`claude-wrangler --update` first.".format(channel)
            )
    else:
        resolved_version = resolve_explicit_selector_version(
            config.claude_selector,
            channel,
            config.available_versions,
        )
    return replace(
        config,
        claude_version=resolved_version,
        version_source=(
            "known {} channel from local metadata".format(channel)
            if config.claude_selector == "latest"
            else "explicit --version for {} channel".format(channel)
        ),
    )
