"""Tests for the native release catalog and notify-only update checks."""

from __future__ import annotations

from pathlib import Path

from claude_wrangler.releases import (
    ChannelCheck,
    apply_catalog,
    check_channel,
    newer_release_note,
    refresh_catalog,
    update_check_cache,
)

from native_fakes import make_config, standard_source


def config_for(tmp_path: Path, **overrides):
    """Return an install config with overrides applied."""

    from dataclasses import replace

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return replace(make_config(project), **overrides)


def test_check_channel_verifies_the_signed_manifest():
    source = standard_source()
    result = check_channel(source, "latest")
    assert result.ok
    assert result.version == "2.1.296"
    assert result.build_date == "2026-10-09T14:46:35Z"
    assert source.manifest_requests == ["2.1.296"]


def test_check_channel_records_errors_instead_of_raising():
    source = standard_source()
    source.unavailable_channels["stable"] = "timed out"
    result = check_channel(source, "stable")
    assert not result.ok
    assert "timed out" in result.error


def test_check_channel_refuses_a_pointer_below_the_floor():
    source = standard_source()
    source.channels["stable"] = "2.1.100"
    result = check_channel(source, "stable")
    assert not result.ok
    assert "2.1.207" in result.error


def test_refresh_catalog_checks_both_channels():
    results = refresh_catalog(standard_source())
    assert sorted(results) == ["latest", "stable"]
    assert results["stable"].version == "2.1.287"


def test_apply_catalog_keeps_previous_answers_for_failed_channels(tmp_path):
    config = config_for(
        tmp_path,
        available_versions={"stable": "2.1.250", "latest": "2.1.260"},
    )
    results = {
        "stable": ChannelCheck("stable", None, None, None, "offline"),
        "latest": ChannelCheck("latest", "2.1.296", "2026-10-09T00:00:00Z", "c", None),
    }
    updated = apply_catalog(config, results, "2026-10-12T00:00:00Z")
    assert updated.available_versions == {"stable": "2.1.250", "latest": "2.1.296"}
    assert updated.available_version_details["latest"]["build_date"] == (
        "2026-10-09T00:00:00Z"
    )
    assert updated.last_update_check["ok"] is False
    assert updated.last_update_check["errors"] == {"stable": "offline"}
    assert updated.available_versions_updated_at == "2026-10-12T00:00:00Z"


def test_update_check_cache_matches_the_launcher_format(tmp_path):
    config = config_for(tmp_path, claude_channel="stable", claude_version="2.1.287")
    results = {"stable": ChannelCheck("stable", "2.1.300", None, None, None)}
    cache = update_check_cache(config, results, "2026-10-12T00:00:00Z")
    assert cache == {
        "checked_at": "2026-10-12T00:00:00Z",
        "channel": "stable",
        "latest_version": "2.1.300",
        "pointer_version": "2.1.300",
        "installed_version": "2.1.287",
        "error": None,
    }
    from claude_wrangler import launcher_core
    from datetime import datetime, timezone

    now = datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
    assert launcher_core.update_cache_is_fresh(cache, "stable", now)
    notice = launcher_core.update_notice(cache, "stable", "2.1.287", "claude-wrangler")
    assert "claude-wrangler --upgrade --channel stable" in notice


def test_update_check_cache_records_a_failure_and_keeps_the_last_answer(tmp_path):
    config = config_for(tmp_path, claude_channel="stable", claude_version="2.1.287")
    results = {"stable": ChannelCheck("stable", None, None, None, "offline")}
    previous = {
        "channel": "stable",
        "latest_version": "2.1.290",
        "pointer_version": "2.1.291",
    }
    cache = update_check_cache(config, results, "2026-10-12T00:00:00Z", previous)
    assert cache["error"] == "offline"
    assert cache["latest_version"] == "2.1.290"
    assert cache["pointer_version"] == "2.1.291"
    other = update_check_cache(
        config, results, "2026-10-12T00:00:00Z", dict(previous, channel="latest")
    )
    assert other["latest_version"] is None
    assert other["pointer_version"] is None


def test_launcher_refresh_never_turns_a_pointer_into_verified_data(tmp_path):
    from datetime import datetime, timezone

    from claude_wrangler import launcher_core

    config = config_for(tmp_path, claude_channel="stable", claude_version="2.1.287")
    results = {"stable": ChannelCheck("stable", "2.1.290", None, None, None)}
    verified = update_check_cache(config, results, "2026-10-10T00:00:00Z")
    runtime_root = tmp_path / "rt"
    runtime_root.mkdir()
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    cache, error = launcher_core.refresh_update_cache(
        str(runtime_root),
        "stable",
        "2.1.287",
        lambda url, timeout: b"2.1.300\n",
        now,
        1.0,
        verified,
    )
    assert error is None
    assert cache["latest_version"] == "2.1.290"
    assert cache["pointer_version"] == "2.1.300"
    # A later --update that fails keeps the verified value, not the pointer.
    failed = {"stable": ChannelCheck("stable", None, None, None, "offline")}
    after = update_check_cache(config, failed, "2026-10-12T01:00:00Z", cache)
    assert after["latest_version"] == "2.1.290"
    notice = launcher_core.update_notice(after, "stable", "2.1.287", "claude-wrangler")
    assert notice.startswith("Claude Code 2.1.290 is available")


def test_newer_release_note(tmp_path):
    config = config_for(
        tmp_path,
        claude_channel="stable",
        claude_version="2.1.287",
        available_versions={"stable": "2.1.300"},
    )
    assert "--upgrade --channel stable" in newer_release_note(config)
    current = config_for(
        tmp_path,
        claude_channel="stable",
        claude_version="2.1.300",
        available_versions={"stable": "2.1.300"},
    )
    assert newer_release_note(current) is None
