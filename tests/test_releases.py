"""Tests for release-channel resolution against Claude Code dist-tags."""

import pytest

from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.releases import (
    fetch_available_claude_versions,
    infer_claude_channel,
    is_exact_version,
    parse_dist_tags,
    resolve_install_version,
    resolve_upgrade_version,
    selector_for_channel,
)


def test_infer_claude_channel_only_recognizes_channel_words():
    assert infer_claude_channel("stable") == "stable"
    assert infer_claude_channel("next") == "next"
    # `latest` is the generic selector word, not a channel claim, and exact
    # Claude versions carry no channel marker.
    assert infer_claude_channel("latest") is None
    assert infer_claude_channel("2.1.153") is None
    assert infer_claude_channel(None) is None


def test_selector_for_channel_maps_channels_to_dist_tags():
    assert selector_for_channel("stable") == "stable"
    assert selector_for_channel("latest") == "latest"
    assert selector_for_channel("next") == "next"
    with pytest.raises(ClaudeWranglerError):
        selector_for_channel("beta")


def test_is_exact_version_accepts_semver_and_prerelease_strings():
    assert is_exact_version("2.1.153") is True
    assert is_exact_version("2.2.0-beta.3") is True
    assert is_exact_version("latest") is False


def test_parse_dist_tags_rejects_non_object_payload():
    with pytest.raises(ClaudeWranglerError):
        parse_dist_tags('["latest"]')


def test_fetch_available_claude_versions_maps_dist_tags(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "claude_wrangler.releases.fetch_claude_dist_tags",
        lambda npm_name, project_root, env=None, timeout_seconds=None: {
            "stable": "2.1.153",
            "latest": "2.1.172",
            "next": "2.1.173",
        },
    )

    versions = fetch_available_claude_versions("npm", tmp_path)

    assert versions == {
        "stable": "2.1.153",
        "latest": "2.1.172",
        "next": "2.1.173",
    }


def test_resolve_install_version_uses_selected_channel(
    monkeypatch,
    tmp_path,
    config_factory,
):
    config = config_factory(
        tmp_path,
        claude_selector="latest",
        claude_channel="next",
        claude_version="latest",
        version_source="explicit --channel next request",
    )
    monkeypatch.setattr(
        "claude_wrangler.releases.fetch_available_claude_versions",
        lambda npm_name, project_root, env=None, timeout_seconds=None: {
            "stable": "2.1.153",
            "latest": "2.1.172",
            "next": "2.1.173",
        },
    )

    resolved = resolve_install_version(config, "npm")

    assert resolved.claude_channel == "next"
    assert resolved.claude_version == "2.1.173"
    assert resolved.available_versions["stable"] == "2.1.153"


def test_resolve_upgrade_version_uses_known_latest_for_selected_channel(
    tmp_path,
    config_factory,
):
    config = config_factory(
        tmp_path,
        operation="upgrade",
        claude_selector="latest",
        claude_channel="next",
        claude_version="latest",
        available_versions={
            "stable": "2.1.153",
            "latest": "2.1.172",
            "next": "2.1.173",
        },
    )

    resolved = resolve_upgrade_version(config)

    assert resolved.claude_version == "2.1.173"
    assert resolved.version_source == "known next channel from local metadata"


def test_resolve_upgrade_version_accepts_exact_version_for_selected_channel(
    tmp_path,
    config_factory,
):
    config = config_factory(
        tmp_path,
        operation="upgrade",
        claude_selector="2.1.150",
        claude_channel="stable",
        claude_version="2.1.150",
        available_versions={
            "stable": "2.1.153",
            "latest": "2.1.172",
            "next": "2.1.173",
        },
    )

    resolved = resolve_upgrade_version(config)

    assert resolved.claude_version == "2.1.150"
    assert resolved.version_source == "explicit --version for stable channel"


def test_resolve_upgrade_version_requires_known_catalog_entry(
    tmp_path,
    config_factory,
):
    config = config_factory(
        tmp_path,
        operation="upgrade",
        claude_selector="latest",
        claude_channel="next",
        claude_version="latest",
        available_versions={},
    )

    with pytest.raises(ClaudeWranglerError, match="--update"):
        resolve_upgrade_version(config)
