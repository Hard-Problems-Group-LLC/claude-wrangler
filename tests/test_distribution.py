"""Offline tests for release channels, the version floor, and manifest bundles.

Unit tests replace signature verification and manifest parsing with fakes
to isolate distribution's own logic. Integration tests run
``ReleaseSource`` and offline re-verification against recorded real release
metadata (served by an in-memory transport) with the real signing module,
which is imported normally so that an import break fails loudly here too.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from claude_wrangler import distribution
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.distribution import (
    CHANNEL_POINTER_MAX_BYTES,
    DEFAULT_RELEASE_BASE_URL,
    MANIFEST_MAX_BYTES,
    SIGNATURE_MAX_BYTES,
    SUPPORTED_CHANNELS,
    VERSION_FLOOR,
    DistributionError,
    ReleaseSource,
    channel_deprecation_message,
    check_version_floor,
    is_exact_version,
    is_newer_version,
    load_manifest_bundle,
    normalize_channel,
    parse_channel_pointer,
    release_hosts,
    version_key,
)
from claude_wrangler.download import DownloadError, HttpsFetcher, TransportResponse

BASE = DEFAULT_RELEASE_BASE_URL
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "releases"


class FakeTransport:
    """In-memory transport serving fixed bodies by URL with HTTP 200."""

    def __init__(self, bodies: Optional[Dict[str, bytes]] = None) -> None:
        """Serve ``bodies``; any other URL answers 404."""

        self.bodies: Dict[str, bytes] = dict(bodies or {})
        self.calls: List[str] = []

    def open(self, url, headers, timeout_seconds):
        """Return the stored body for ``url`` or a 404."""

        self.calls.append(url)
        if url not in self.bodies:
            return TransportResponse(404, {"Content-Length": "0"}, io.BytesIO(b""))
        body = self.bodies[url]
        return TransportResponse(
            200, {"Content-Length": str(len(body))}, io.BytesIO(body)
        )


def make_source(bodies: Dict[str, bytes]) -> Tuple[ReleaseSource, FakeTransport]:
    """Return a ReleaseSource reading from an in-memory transport."""

    transport = FakeTransport(bodies)
    fetcher = HttpsFetcher(
        release_hosts(BASE), retries=0, transport=transport, sleep=lambda s: None
    )
    return ReleaseSource(BASE, fetcher=fetcher), transport


@dataclass
class FakeVerified:
    """Minimal stand-in for VerifiedManifest returned by the fake parser."""

    version: str
    schemes: Tuple[str, ...]


@pytest.fixture
def fake_signing(monkeypatch):
    """Replace signature verification and parsing with recording fakes."""

    events: List[Tuple[str, tuple]] = []
    state = {"verify_error": None, "parsed_version": None}

    def verify(manifest_bytes, pgp_signature, raw_signature_json):
        events.append(("verify", (manifest_bytes, pgp_signature, raw_signature_json)))
        if state["verify_error"] is not None:
            raise state["verify_error"]
        return ("openpgp", "raw-rsa")

    def parse(manifest_bytes, expected_version, schemes):
        events.append(("parse", (manifest_bytes, expected_version, schemes)))
        return FakeVerified(state["parsed_version"] or expected_version, schemes)

    monkeypatch.setattr(distribution, "verify_manifest_signatures", verify)
    monkeypatch.setattr(distribution, "parse_verified_manifest", parse)
    return events, state


def bundle_urls(version: str) -> Tuple[str, str, str]:
    """Return the manifest, OpenPGP, and raw-signature URLs for a version."""

    root = "{}/{}/".format(BASE, version)
    return (
        root + "manifest.json",
        root + "manifest.json.sig",
        root + "manifest.json.raw-sig.json",
    )


# -- channels ----------------------------------------------------------------


def test_supported_channels_and_constants():
    assert SUPPORTED_CHANNELS == ("stable", "latest")
    assert VERSION_FLOOR == "2.1.207"
    assert (
        DEFAULT_RELEASE_BASE_URL == "https://downloads.claude.ai/claude-code-releases"
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("stable", ("stable", False)),
        ("latest", ("latest", False)),
        ("next", ("latest", True)),
        (" Stable ", ("stable", False)),
        ("NEXT", ("latest", True)),
    ],
)
def test_normalize_channel_accepts_supported_and_deprecated(raw, expected):
    assert normalize_channel(raw) == expected


@pytest.mark.parametrize("raw", ["beta", "", "2.1.300", "nightly", "stable/../x"])
def test_normalize_channel_rejects_others(raw):
    with pytest.raises(DistributionError, match="stable, latest"):
        normalize_channel(raw)


def test_channel_deprecation_message_names_replacement():
    message = channel_deprecation_message("next")
    assert "'next'" in message and "'latest'" in message
    assert "deprecated" in message


# -- versions and the floor --------------------------------------------------


@pytest.mark.parametrize(
    "version", ["2.1.207", "2.1.208", "2.2.0", "2.1.1000", "10.0.0"]
)
def test_version_floor_allows_floor_and_newer(version):
    check_version_floor(version)


@pytest.mark.parametrize("version", ["2.1.206", "2.0.999", "1.9.300", "0.0.1"])
def test_version_floor_refuses_older_and_names_floor(version):
    with pytest.raises(DistributionError) as caught:
        check_version_floor(version)
    assert "2.1.207" in str(caught.value)
    assert version in str(caught.value)


@pytest.mark.parametrize(
    "version",
    ["2.1", "v2.1.300", "2.1.300-beta", "２.1.300", "2.1.300\n", " 2.1.300", ""],
)
def test_inexact_versions_are_refused(version):
    assert is_exact_version(version) is False
    with pytest.raises(DistributionError):
        check_version_floor(version)


def test_version_ordering_is_numeric():
    assert version_key("2.1.1000") > version_key("2.1.999")
    assert is_newer_version("2.1.300", "2.1.299")
    assert not is_newer_version("2.1.300", "2.1.300")
    assert not is_newer_version("2.0.999", "2.1.0")


# -- channel pointers ----------------------------------------------------------


@pytest.mark.parametrize(
    "payload", [b"2.1.287", b"2.1.287\n", b" \t2.1.287\r\n", b"\n2.1.287 "]
)
def test_parse_channel_pointer_strips_whitespace(payload):
    assert parse_channel_pointer(payload) == "2.1.287"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"   \n",
        b"latest",
        b"2.1",
        b"2.1.287-beta",
        b"<html><body>Not Found</body></html>",
        b"2.1.287\n2.1.288\n",
        b"\xff\xfe2.1.287",
        "２.1.287".encode("utf-8"),
        b"2.1.287\x00",
    ],
)
def test_parse_channel_pointer_rejects_invalid(payload):
    with pytest.raises(DistributionError):
        parse_channel_pointer(payload)


def test_parse_channel_pointer_error_preview_is_sanitized():
    with pytest.raises(DistributionError) as caught:
        parse_channel_pointer(b"\x1b[31mevil")
    assert "\x1b" not in str(caught.value)


def test_channel_version_reads_pointer_and_maps_next_to_latest():
    source, transport = make_source(
        {BASE + "/stable": b"2.1.287\n", BASE + "/latest": b"2.1.296"}
    )
    assert source.channel_version("stable") == "2.1.287"
    assert source.channel_version("next") == "2.1.296"
    assert transport.calls == [BASE + "/stable", BASE + "/latest"]


def test_channel_version_rejects_invalid_and_oversize_pointers():
    source, _ = make_source(
        {
            BASE + "/stable": b"<!doctype html>",
            BASE + "/latest": b"2.1.296" + b" " * CHANNEL_POINTER_MAX_BYTES,
        }
    )
    with pytest.raises(DistributionError, match="exact version"):
        source.channel_version("stable")
    with pytest.raises(DownloadError, match="limit"):
        source.channel_version("latest")
    with pytest.raises(DistributionError):
        source.channel_version("beta")


# -- release source construction --------------------------------------------


def test_release_hosts_adds_mirror_host():
    assert release_hosts() == ("downloads.claude.ai",)
    assert release_hosts("https://Mirror.Example.com/cc/") == (
        "downloads.claude.ai",
        "mirror.example.com",
    )


def test_release_source_default_fetcher_allowlists_base_host():
    source = ReleaseSource("https://mirror.example.com/claude-code-releases/")
    assert source.base_url == "https://mirror.example.com/claude-code-releases"
    assert source.fetcher.allowed_hosts == frozenset(
        {"downloads.claude.ai", "mirror.example.com"}
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://downloads.claude.ai/claude-code-releases",
        "https://user:pw@downloads.claude.ai/claude-code-releases",
        "https:///claude-code-releases",
        "https://downloads.claude.ai/x?y=1",
        "downloads.claude.ai/claude-code-releases",
    ],
)
def test_release_source_rejects_unsafe_base_urls(base_url):
    with pytest.raises(DistributionError):
        ReleaseSource(base_url, fetcher=None)


def test_artifact_url_uses_platform_binary_name():
    source, _ = make_source({})
    assert source.artifact_url("2.1.287", "win32-x64") == (
        BASE + "/2.1.287/win32-x64/claude.exe"
    )
    assert source.artifact_url("2.1.287", "linux-arm64-musl") == (
        BASE + "/2.1.287/linux-arm64-musl/claude"
    )
    with pytest.raises(ClaudeWranglerError):
        source.artifact_url("2.1.287", "../../etc")
    with pytest.raises(DistributionError):
        source.artifact_url("latest", "linux-x64")


# -- manifest bundles ----------------------------------------------------------


def test_fetch_manifest_bundle_verifies_before_parsing(fake_signing):
    events, _ = fake_signing
    manifest_url, sig_url, raw_url = bundle_urls("2.1.300")
    source, transport = make_source(
        {manifest_url: b"{manifest}", sig_url: b"pgp-sig", raw_url: b"{raw}"}
    )
    bundle = source.fetch_manifest_bundle("2.1.300")
    assert transport.calls == [manifest_url, sig_url, raw_url]
    assert [name for name, _ in events] == ["verify", "parse"]
    assert events[0][1] == (b"{manifest}", b"pgp-sig", b"{raw}")
    assert events[1][1] == (b"{manifest}", "2.1.300", ("openpgp", "raw-rsa"))
    assert bundle.version == "2.1.300"
    assert bundle.evidence_files() == {
        "manifest.json": b"{manifest}",
        "manifest.json.sig": b"pgp-sig",
        "manifest.json.raw-sig.json": b"{raw}",
    }


def test_signature_failure_stops_before_parsing(fake_signing):
    events, state = fake_signing
    state["verify_error"] = distribution.SignatureError("bad signature")
    manifest_url, sig_url, raw_url = bundle_urls("2.1.300")
    source, _ = make_source(
        {manifest_url: b"not json {", sig_url: b"pgp", raw_url: b"raw"}
    )
    with pytest.raises(distribution.SignatureError) as caught:
        source.fetch_manifest_bundle("2.1.300")
    assert "bad signature" in str(caught.value)
    assert manifest_url in str(caught.value)
    assert [name for name, _ in events] == ["verify"]


def test_fetch_manifest_bundle_refuses_below_floor_without_requests(fake_signing):
    source, transport = make_source({})
    with pytest.raises(DistributionError, match="2.1.207"):
        source.fetch_manifest_bundle("2.1.206")
    assert transport.calls == []


def test_fetch_manifest_bundle_caps_manifest_and_signature_sizes(fake_signing):
    events, _ = fake_signing
    manifest_url, sig_url, raw_url = bundle_urls("2.1.300")
    big_manifest, _ = make_source(
        {manifest_url: b"x" * (MANIFEST_MAX_BYTES + 1), sig_url: b"s", raw_url: b"r"}
    )
    with pytest.raises(DownloadError, match="limit"):
        big_manifest.fetch_manifest_bundle("2.1.300")
    big_signature, _ = make_source(
        {manifest_url: b"{}", sig_url: b"s" * (SIGNATURE_MAX_BYTES + 1), raw_url: b"r"}
    )
    with pytest.raises(DownloadError, match="limit"):
        big_signature.fetch_manifest_bundle("2.1.300")
    big_raw, _ = make_source(
        {manifest_url: b"{}", sig_url: b"s", raw_url: b"r" * (SIGNATURE_MAX_BYTES + 1)}
    )
    with pytest.raises(DownloadError, match="limit"):
        big_raw.fetch_manifest_bundle("2.1.300")
    assert events == []


def test_missing_signature_file_is_a_download_error(fake_signing):
    manifest_url, sig_url, _raw_url = bundle_urls("2.1.300")
    source, _ = make_source({manifest_url: b"{}", sig_url: b"s"})
    with pytest.raises(DownloadError, match="HTTP 404"):
        source.fetch_manifest_bundle("2.1.300")


def test_parsed_version_mismatch_is_refused(fake_signing):
    _, state = fake_signing
    state["parsed_version"] = "2.1.299"
    manifest_url, sig_url, raw_url = bundle_urls("2.1.300")
    source, _ = make_source({manifest_url: b"{}", sig_url: b"s", raw_url: b"r"})
    with pytest.raises(DistributionError, match="2.1.299"):
        source.fetch_manifest_bundle("2.1.300")


def test_load_manifest_bundle_reverifies_retained_evidence(tmp_path, fake_signing):
    events, _ = fake_signing
    (tmp_path / "manifest.json").write_bytes(b"{m}")
    (tmp_path / "manifest.json.sig").write_bytes(b"sig")
    (tmp_path / "manifest.json.raw-sig.json").write_bytes(b"{r}")
    bundle = load_manifest_bundle(tmp_path, "2.1.300")
    assert bundle.version == "2.1.300"
    assert events[0] == ("verify", (b"{m}", b"sig", b"{r}"))


def test_load_manifest_bundle_refuses_missing_or_oversize_evidence(
    tmp_path, fake_signing
):
    (tmp_path / "manifest.json").write_bytes(b"{m}")
    (tmp_path / "manifest.json.sig").write_bytes(b"sig")
    with pytest.raises(DistributionError, match="missing"):
        load_manifest_bundle(tmp_path, "2.1.300")
    (tmp_path / "manifest.json.raw-sig.json").write_bytes(
        b"r" * (SIGNATURE_MAX_BYTES + 1)
    )
    with pytest.raises(DistributionError, match="larger than"):
        load_manifest_bundle(tmp_path, "2.1.300")


# -- integration with real signatures (no network) ----------------------------


def _recorded_versions() -> List[str]:
    """Return fixture versions that carry all three evidence files."""

    if not FIXTURES.is_dir():
        return []
    versions = []
    for manifest in sorted(FIXTURES.rglob("manifest.json")):
        directory = manifest.parent
        if is_exact_version(directory.name) and all(
            (directory / name).is_file()
            for name in ("manifest.json.sig", "manifest.json.raw-sig.json")
        ):
            versions.append(directory.name)
    return versions


RECORDED_VERSIONS = _recorded_versions()


@pytest.mark.skipif(
    not RECORDED_VERSIONS,
    reason="needs tests/fixtures/releases",
)
def test_release_source_verifies_recorded_releases_end_to_end():
    from claude_wrangler.platforms import PLATFORM_KEYS

    bodies: Dict[str, bytes] = {}
    for version in RECORDED_VERSIONS:
        directory = next(
            path.parent
            for path in FIXTURES.rglob("manifest.json")
            if path.parent.name == version
        )
        for url, name in zip(
            bundle_urls(version),
            ("manifest.json", "manifest.json.sig", "manifest.json.raw-sig.json"),
        ):
            bodies[url] = (directory / name).read_bytes()
    newest = max(RECORDED_VERSIONS, key=version_key)
    bodies[BASE + "/stable"] = newest.encode("ascii") + b"\n"
    source, _ = make_source(bodies)

    assert source.channel_version("stable") == newest
    for version in RECORDED_VERSIONS:
        bundle = source.fetch_manifest_bundle(version)
        assert bundle.verified.version == version
        assert tuple(bundle.verified.schemes) == ("openpgp", "raw-rsa")
        assert set(bundle.verified.artifacts) <= set(PLATFORM_KEYS)
        assert "win32-x64" in bundle.verified.artifacts

    # A single flipped byte in a served manifest must fail verification.
    manifest_url = bundle_urls(newest)[0]
    tampered = bytearray(bodies[manifest_url])
    tampered[len(tampered) // 2] ^= 0x01
    bodies[manifest_url] = bytes(tampered)
    tampered_source, _ = make_source(bodies)
    with pytest.raises(distribution.SignatureError):
        tampered_source.fetch_manifest_bundle(newest)


@pytest.mark.skipif(
    not RECORDED_VERSIONS,
    reason="needs tests/fixtures/releases",
)
def test_load_manifest_bundle_verifies_recorded_evidence_offline():
    version = RECORDED_VERSIONS[0]
    directory = next(
        path.parent
        for path in FIXTURES.rglob("manifest.json")
        if path.parent.name == version
    )
    bundle = load_manifest_bundle(directory, version)
    assert bundle.verified.version == version
    with pytest.raises(ClaudeWranglerError):
        load_manifest_bundle(directory, "9.9.9")
