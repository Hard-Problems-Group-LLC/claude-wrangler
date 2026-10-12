"""Claude Code release channels, version floor, and signed manifest bundles.

Anthropic publishes each native Claude Code release under one base URL:
unsigned channel pointers (``/stable``, ``/latest``), and per version a
``manifest.json`` with two detached signatures plus one binary per
platform. ``ReleaseSource`` resolves a channel to a version, fetches the
manifest with both signatures, verifies the signatures before the bytes
are ever parsed as JSON, and builds artifact URLs. A channel pointer is
only ever used to choose which signed manifest to fetch.

This module is on the bootstrap path, so it stays standard-library-only
and Python 3.9 compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Dict, Optional, Tuple
import urllib.parse

from .download import HttpsFetcher
from .models import ClaudeWranglerError
from .platforms import binary_filename
from .signing import (
    SignatureError,
    VerifiedManifest,
    parse_verified_manifest,
    verify_manifest_signatures,
)

__all__ = [
    "CHANNEL_POINTER_MAX_BYTES",
    "DEFAULT_RELEASE_BASE_URL",
    "DEFAULT_RELEASE_HOST",
    "DEPRECATED_CHANNEL_ALIASES",
    "DistributionError",
    "EVIDENCE_NAMES",
    "MANIFEST_MAX_BYTES",
    "MANIFEST_NAME",
    "ManifestBundle",
    "PGP_SIGNATURE_NAME",
    "RAW_SIGNATURE_NAME",
    "ReleaseSource",
    "SIGNATURE_MAX_BYTES",
    "SUPPORTED_CHANNELS",
    "SignatureError",
    "VERSION_FLOOR",
    "channel_deprecation_message",
    "check_version_floor",
    "is_exact_version",
    "is_newer_version",
    "load_manifest_bundle",
    "normalize_channel",
    "parse_channel_pointer",
    "release_hosts",
    "verify_manifest_bundle",
    "version_key",
]

DEFAULT_RELEASE_BASE_URL = "https://downloads.claude.ai/claude-code-releases"
DEFAULT_RELEASE_HOST = "downloads.claude.ai"
VERSION_FLOOR = "2.1.207"
SUPPORTED_CHANNELS = ("stable", "latest")
DEPRECATED_CHANNEL_ALIASES: Dict[str, str] = {"next": "latest"}

MANIFEST_NAME = "manifest.json"
PGP_SIGNATURE_NAME = "manifest.json.sig"
RAW_SIGNATURE_NAME = "manifest.json.raw-sig.json"
EVIDENCE_NAMES = (MANIFEST_NAME, PGP_SIGNATURE_NAME, RAW_SIGNATURE_NAME)

MANIFEST_MAX_BYTES = 1024 * 1024
SIGNATURE_MAX_BYTES = 64 * 1024
CHANNEL_POINTER_MAX_BYTES = 256

# ASCII digits only: ``\d`` would also match other Unicode digits, and a
# ``$`` anchor would accept a trailing newline. Callers use ``fullmatch``.
_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_POINTER_PREVIEW_CHARS = 40


class DistributionError(ClaudeWranglerError):
    """Raised for an unusable channel, version, pointer, or release source."""


def channel_deprecation_message(alias: str) -> str:
    """Return the warning to print when a deprecated channel alias is used."""

    target = DEPRECATED_CHANNEL_ALIASES.get(alias, alias)
    return (
        "Warning: the {!r} channel is deprecated; using {!r} instead. Pass "
        "--channel {} to silence this warning.".format(alias, target, target)
    )


def normalize_channel(channel: str) -> Tuple[str, bool]:
    """Return ``(channel, deprecated)`` for a requested release channel.

    ``stable`` and ``latest`` are returned as-is; ``next`` maps to
    ``latest`` with ``deprecated=True`` so the caller prints the warning
    from ``channel_deprecation_message``. Anything else is refused.
    """

    name = str(channel).strip().lower()
    if name in SUPPORTED_CHANNELS:
        return name, False
    if name in DEPRECATED_CHANNEL_ALIASES:
        return DEPRECATED_CHANNEL_ALIASES[name], True
    raise DistributionError(
        "Unsupported Claude Code channel {!r}; choose one of: {}.".format(
            channel, ", ".join(SUPPORTED_CHANNELS)
        )
    )


def is_exact_version(text: str) -> bool:
    """Return True when ``text`` is exactly ``MAJOR.MINOR.PATCH`` in ASCII digits."""

    return isinstance(text, str) and _VERSION_PATTERN.fullmatch(text) is not None


def version_key(version: str) -> Tuple[int, int, int]:
    """Return a numeric sort key for an exact version or raise DistributionError."""

    if not is_exact_version(version):
        raise DistributionError(
            "{!r} is not an exact Claude Code version (MAJOR.MINOR.PATCH).".format(
                version
            )
        )
    try:
        major, minor, patch = (int(part) for part in version.split("."))
    except ValueError as exc:
        # Python 3.11+ refuses absurdly long digit strings.
        raise DistributionError(
            "Claude Code version {!r} is out of range.".format(version[:40])
        ) from exc
    return major, minor, patch


def is_newer_version(candidate: str, installed: str) -> bool:
    """Return True when ``candidate`` sorts after ``installed``."""

    return version_key(candidate) > version_key(installed)


def check_version_floor(version: str) -> None:
    """Refuse an exact version older than ``VERSION_FLOOR``."""

    if version_key(version) < version_key(VERSION_FLOOR):
        raise DistributionError(
            "Claude Code {} is older than {}, the oldest version claude-wrangler "
            "installs ({} is the first release whose manifest carries both "
            "signature forms and that honors DISABLE_UPDATES for custom "
            "launchers). Choose {} or newer.".format(
                version, VERSION_FLOOR, VERSION_FLOOR, VERSION_FLOOR
            )
        )


def parse_channel_pointer(payload: bytes, source: str = "channel pointer") -> str:
    """Return the version named by an unsigned channel-pointer body.

    The body must be ASCII and, after stripping surrounding whitespace,
    exactly ``MAJOR.MINOR.PATCH``. The result only selects which signed
    manifest to fetch; it is never trusted for anything else.
    """

    try:
        text = bytes(payload).decode("ascii")
    except UnicodeDecodeError as exc:
        raise DistributionError(
            "{} is not ASCII text, so it is not a version pointer.".format(source)
        ) from exc
    version = text.strip()
    if not is_exact_version(version):
        preview = version[:_POINTER_PREVIEW_CHARS]
        preview = "".join(char if char.isprintable() else "?" for char in preview)
        raise DistributionError(
            "{} does not name an exact version (got {!r}).".format(source, preview)
        )
    return version


def _normalize_base_url(base_url: str) -> str:
    """Return ``base_url`` without a trailing slash, or raise DistributionError."""

    text = str(base_url).strip()
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise DistributionError(
            "Release base URL must be an https:// URL with a host: {!r}".format(
                base_url
            )
        )
    if parts.username is not None or parts.password is not None:
        raise DistributionError(
            "Release base URL must not embed credentials: {!r}".format(base_url)
        )
    if parts.query or parts.fragment:
        raise DistributionError(
            "Release base URL must not carry a query or fragment: {!r}".format(base_url)
        )
    return text.rstrip("/")


def release_hosts(base_url: str = DEFAULT_RELEASE_BASE_URL) -> Tuple[str, ...]:
    """Return the download host allowlist for a release base URL.

    ``downloads.claude.ai`` is always allowed; an explicit base URL adds
    its own host.
    """

    host = (urllib.parse.urlsplit(_normalize_base_url(base_url)).hostname or "").rstrip(
        "."
    )
    if host == DEFAULT_RELEASE_HOST:
        return (DEFAULT_RELEASE_HOST,)
    return (DEFAULT_RELEASE_HOST, host)


@dataclass(frozen=True)
class ManifestBundle:
    """One release's manifest, both detached signatures, and the verified view."""

    manifest_bytes: bytes
    pgp_signature: bytes
    raw_signature_json: bytes
    verified: VerifiedManifest

    @property
    def version(self) -> str:
        """Return the verified release version."""

        return self.verified.version

    def evidence_files(self) -> Dict[str, bytes]:
        """Return the retained-evidence file names mapped to their bytes."""

        return {
            MANIFEST_NAME: self.manifest_bytes,
            PGP_SIGNATURE_NAME: self.pgp_signature,
            RAW_SIGNATURE_NAME: self.raw_signature_json,
        }


def verify_manifest_bundle(
    manifest_bytes: bytes,
    pgp_signature: bytes,
    raw_signature_json: bytes,
    expected_version: str,
    source: Optional[str] = None,
) -> ManifestBundle:
    """Verify both signatures, then parse the manifest for ``expected_version``.

    The manifest bytes are not parsed until both signatures have passed.
    ``source`` names where the bytes came from in error messages.
    """

    origin = source or "the manifest for {}".format(expected_version)
    try:
        schemes = verify_manifest_signatures(
            manifest_bytes, pgp_signature, raw_signature_json
        )
    except SignatureError as exc:
        # Keep the specific SignatureError subclass; only add the origin.
        raise type(exc)(
            "Signature verification failed for {}: {}".format(origin, exc)
        ) from exc
    verified = parse_verified_manifest(manifest_bytes, expected_version, schemes)
    if verified.version != expected_version:
        raise DistributionError(
            "{} describes version {}, not the requested {}.".format(
                origin, verified.version, expected_version
            )
        )
    return ManifestBundle(
        manifest_bytes=manifest_bytes,
        pgp_signature=pgp_signature,
        raw_signature_json=raw_signature_json,
        verified=verified,
    )


def _read_evidence(path: Path, limit: int) -> bytes:
    """Read one retained-evidence file, refusing a missing or oversize file."""

    try:
        with open(str(path), "rb") as handle:
            if os.fstat(handle.fileno()).st_size > limit:
                raise DistributionError(
                    "Retained evidence {} is larger than {} bytes.".format(path, limit)
                )
            return handle.read(limit + 1)
    except FileNotFoundError as exc:
        raise DistributionError(
            "Retained evidence is missing: {}".format(path)
        ) from exc
    except OSError as exc:
        raise DistributionError(
            "Cannot read retained evidence {}: {}".format(path, exc)
        ) from exc


def load_manifest_bundle(directory: Path, expected_version: str) -> ManifestBundle:
    """Re-verify a slot's retained manifest evidence offline."""

    base = Path(directory)
    manifest_bytes = _read_evidence(base / MANIFEST_NAME, MANIFEST_MAX_BYTES)
    pgp_signature = _read_evidence(base / PGP_SIGNATURE_NAME, SIGNATURE_MAX_BYTES)
    raw_signature = _read_evidence(base / RAW_SIGNATURE_NAME, SIGNATURE_MAX_BYTES)
    return verify_manifest_bundle(
        manifest_bytes,
        pgp_signature,
        raw_signature,
        expected_version,
        source="the retained manifest in {}".format(base),
    )


class ReleaseSource:
    """Anthropic's native Claude Code release distribution (or a mirror)."""

    def __init__(
        self,
        base_url: str = DEFAULT_RELEASE_BASE_URL,
        fetcher: Optional[HttpsFetcher] = None,
    ) -> None:
        """Bind a base URL and a fetcher allowlisted for its host.

        Without an explicit ``fetcher`` a default ``HttpsFetcher`` is built
        whose allowlist is ``release_hosts(base_url)``.
        """

        self.base_url = _normalize_base_url(base_url)
        self.fetcher = (
            fetcher
            if fetcher is not None
            else HttpsFetcher(release_hosts(self.base_url))
        )

    def channel_url(self, channel: str) -> str:
        """Return the pointer URL for a channel (aliases normalized)."""

        name, _deprecated = normalize_channel(channel)
        return "{}/{}".format(self.base_url, name)

    def channel_version(self, channel: str) -> str:
        """Return the version a channel pointer currently names.

        ``next`` is accepted and resolved as ``latest``; printing the
        deprecation warning is the caller's job (see ``normalize_channel``).
        """

        url = self.channel_url(channel)
        payload = self.fetcher.get_bytes(url, CHANNEL_POINTER_MAX_BYTES)
        return parse_channel_pointer(payload, source="Channel pointer {}".format(url))

    def version_url(self, version: str, name: str) -> str:
        """Return the URL of one file directly under a version directory."""

        if not is_exact_version(version):
            raise DistributionError(
                "{!r} is not an exact Claude Code version.".format(version)
            )
        return "{}/{}/{}".format(self.base_url, version, name)

    def fetch_manifest_bundle(self, version: str) -> ManifestBundle:
        """Fetch and verify the signed manifest for an exact ``version``.

        Versions below ``VERSION_FLOOR`` are refused before any request.
        Both signatures are verified before the manifest is parsed.
        """

        check_version_floor(version)
        manifest_url = self.version_url(version, MANIFEST_NAME)
        manifest_bytes = self.fetcher.get_bytes(manifest_url, MANIFEST_MAX_BYTES)
        pgp_signature = self.fetcher.get_bytes(
            self.version_url(version, PGP_SIGNATURE_NAME), SIGNATURE_MAX_BYTES
        )
        raw_signature = self.fetcher.get_bytes(
            self.version_url(version, RAW_SIGNATURE_NAME), SIGNATURE_MAX_BYTES
        )
        return verify_manifest_bundle(
            manifest_bytes, pgp_signature, raw_signature, version, source=manifest_url
        )

    def artifact_url(self, version: str, platform_key: str) -> str:
        """Return the binary URL for ``version`` on ``platform_key``."""

        filename = binary_filename(platform_key)
        return self.version_url(version, "{}/{}".format(platform_key, filename))
