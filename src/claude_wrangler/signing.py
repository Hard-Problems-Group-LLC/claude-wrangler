"""Pinned-key verification of signed Claude Code release manifests.

Anthropic publishes two independent signatures over every release manifest
under `https://downloads.claude.ai/claude-code-releases/<version>/`:

- `manifest.json.sig`: an OpenPGP v4 detached signature (RFC 4880), and
- `manifest.json.raw-sig.json`: an RSASSA-PKCS1-v1_5-SHA512 signature over
  `b"claude-code-manifest-v1\\x00" + manifest bytes`.

This module verifies both against a trust anchor compiled into the package
(`native_runtime_distribution_and_verification.md`, requirements 4-6). It
never reads keys from the consuming repository, the network, or a GnuPG
keyring, never invokes `gpg`, and uses explicit `raise` statements so that
`python -O` cannot disable a check. Only the standard library is used, and
the code stays Python 3.9 compatible because the bootstrap path imports it.

Trust-anchor provenance (recorded 2026-10-12):

- `https://downloads.claude.ai/keys/claude-code.rsa.pub` is an 800-byte PEM
  SubjectPublicKeyInfo whose file SHA-256 is
  `395759c1f7449ef4cdef305a42e820f3c766d6090d142634ebdb049f113168b6`,
  exactly the value Anthropic's setup documentation
  (`https://code.claude.com/docs/en/setup`, "Install with Linux package
  managers", apk tab) tells operators to expect. The RSA-4096 modulus and
  exponent below were decoded from that file. The SHA-256 of its DER SPKI
  is the `publicKeySha256` carried by every recorded raw-signature sidecar.
- `https://downloads.claude.ai/keys/claude-code.asc` is the OpenPGP key the
  same documentation names by fingerprint
  `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE` ("Anthropic Claude
  Code Release Signing <security@anthropic.com>"). It is a single RSA-4096
  primary key created 2026-03-30T21:47:28Z with no subkeys, and its RSA
  material is identical to `claude-code.rsa.pub`. The recorded `.sig` files
  name that primary key in their issuer-fingerprint subpacket, so one pinned
  RSA key serves both schemes and no subkey material needs pinning.

`openpgp_v4_fingerprint()` and `spki_sha256()` recompute the pinned
fingerprint and SPKI hash from the pinned modulus, exponent, and key
creation time; every verification re-checks that agreement so a partial
edit of the anchor fails closed instead of silently trusting another key.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type

from .models import ClaudeWranglerError

# --- Pinned trust anchor --------------------------------------------------
# Decoded from claude-code.rsa.pub (see the module docstring for provenance).
PINNED_RSA_MODULUS = int(
    "a76f2b495e48f0798af022bd1a2c4151947f82d27178e0ac4573b810989e8f8d"
    "23cc19900377165c6a2c54ef3665f16c44a553b4c0b9881fc71fdb318e772b99"
    "83561aba0935bc76f66fc58184018240974f801f165eab9e75ea4462d939eae3"
    "ff802a138c90d4665b841811e7967bae8d2278a2bc447614b2904a0051f25918"
    "420b3e931d6e9845b7f4a7f846ff4f13398e928f7709eff004a1345ff5999535"
    "7d64d0abdb5128951a9198f4e2075a0c51254176a40ad4e28fe193eb2a180804"
    "521f018ef8f0bf3ebeaae2d73e0fb2d18f1481bd4583a054a696a6ff3c9d618f"
    "d0ffec8d8e7b85d79e200f58c479e8abf11e5cceceb350749ba36c9752902c21"
    "d598a3912777cbc6b25b959851319d45a6fdf4d5306a0ce569b7bf55517a9332"
    "7449c9f9ab73e2841d98f41c28d022a791893b6bb293efb116bf42a4abe54798"
    "eb6a5f76b68615fe6fa6babdef6bcc9d6a6581a3fa86effc2dd2c752f352301f"
    "a95cbb4490dfdae0283f9b7683cbbf4e44a953184c5448d90644b161984ccb34"
    "0ceadf0882451f9d2ac83d31e2c5b0d60116786de30e063c723d92b59cfece3a"
    "b351dd390a26dc0b31001a6d44420e72f43e34b48b00535388edba6043d57494"
    "2f25aea13bcc963ae942bd67575a03c8eb7438ba9a280d8c716b5df4f757842f"
    "32d84220ed39ab4e524b7c761b7669220988f8278f50f075a437fe9613e45353",
    16,
)
PINNED_RSA_EXPONENT = 65537
# SHA-256 of the DER SubjectPublicKeyInfo; equals `publicKeySha256` in the
# recorded raw-signature sidecars for 2.1.207, 2.1.287, and 2.1.296.
PINNED_SPKI_SHA256 = "89a24e2e18d2f8fa4627a0a47cf76c3682aef237b60d2cd115d6f5202e2e9896"
# OpenPGP v4 fingerprint documented in Anthropic's setup guide.
PINNED_PGP_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
# Creation time of the OpenPGP primary key packet in claude-code.asc
# (2026-03-30T21:47:28Z); it is an input to the v4 fingerprint.
PINNED_PGP_KEY_CREATED = 1774907248
# Documented SHA-256 of the claude-code.rsa.pub file itself. Not used for
# verification; kept so tests can prove the PEM-to-modulus derivation.
PUBLISHED_KEY_FILE_SHA256 = (
    "395759c1f7449ef4cdef305a42e820f3c766d6090d142634ebdb049f113168b6"
)

# --- Scheme and format constants -----------------------------------------
SCHEME_OPENPGP = "openpgp"
SCHEME_RAW_RSA = "raw-rsa"
REQUIRED_SCHEMES = (SCHEME_OPENPGP, SCHEME_RAW_RSA)
RAW_SIGNATURE_PREFIX = b"claude-code-manifest-v1\x00"
RAW_SIGNATURE_ALGORITHM = "RSASSA-PKCS1-v1_5-SHA512"
RAW_SIGNATURE_SCHEMA = 1
# Upper bound for either signature input; real files are about 835 bytes.
# Callers fetching signatures should use it as their download limit.
MAX_SIGNATURE_BYTES = 64 * 1024

_DIGESTINFO_SHA512_PREFIX = bytes.fromhex("3051300d060960864801650304020305000440")
_SHA512_DIGEST_SIZE = 64
_RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")

_PGP_SIGNATURE_TAG = 2
_PGP_SIG_VERSION = 4
_PGP_SIG_TYPE_BINARY = 0x00
_PGP_PUBKEY_RSA = 1
_PGP_HASH_SHA512 = 10
_SUBPACKET_CREATION_TIME = 2
_SUBPACKET_ISSUER_KEY_ID = 16
_SUBPACKET_ISSUER_FINGERPRINT = 33
_KNOWN_SUBPACKETS = frozenset(
    (_SUBPACKET_CREATION_TIME, _SUBPACKET_ISSUER_KEY_ID, _SUBPACKET_ISSUER_FINGERPRINT)
)

_ARMOR_BEGIN = "-----BEGIN PGP SIGNATURE-----"
_ARMOR_END = "-----END PGP SIGNATURE-----"
_ARMOR_HEADER_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*:(?: .*)?")
_ARMOR_BODY_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_ARMOR_CRC_RE = re.compile(r"=([A-Za-z0-9+/]{4})")
_CRC24_INIT = 0xB704CE
_CRC24_POLY = 0x1864CFB

_HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")
_PLATFORM_KEY_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A bare file name: it later becomes a URL segment and a slot path.
_BINARY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SignatureError(ClaudeWranglerError):
    """Raised when a release signature or its trust anchor fails a check."""


class ManifestError(SignatureError):
    """Raised when verified manifest bytes violate the content policy.

    It derives from `SignatureError` so callers that treat any verification
    failure as fatal can catch one type.
    """


@dataclass(frozen=True)
class PlatformArtifact:
    """One platform's binary entry from a verified release manifest."""

    platform: str
    binary: str
    checksum: str
    size: int


@dataclass(frozen=True)
class VerifiedManifest:
    """Parsed content of a manifest whose signatures already verified."""

    version: str
    build_date: Optional[str]
    commit: Optional[str]
    manifest_sha256: str
    artifacts: Dict[str, PlatformArtifact]
    schemes: Tuple[str, ...]

    def artifact_for(self, platform_key: str) -> PlatformArtifact:
        """Return the artifact for one platform key or raise ManifestError."""

        artifact = self.artifacts.get(platform_key)
        if artifact is None:
            raise ManifestError(
                "Claude Code {} manifest has no {!r} platform; available: {}".format(
                    self.version, platform_key, ", ".join(sorted(self.artifacts))
                )
            )
        return artifact


@dataclass(frozen=True)
class _RsaPublicKey:
    """An RSA public key reduced to the two integers verification needs."""

    modulus: int
    exponent: int

    @property
    def size_bytes(self) -> int:
        """Return k, the modulus length in octets (RFC 8017 notation)."""

        return (self.modulus.bit_length() + 7) // 8


@dataclass(frozen=True)
class _TrustAnchor:
    """The key plus the identifiers both signature schemes must name."""

    key: _RsaPublicKey
    pgp_fingerprint: str
    pgp_created: int
    spki_sha256: str


@dataclass(frozen=True)
class _Subpacket:
    """One OpenPGP signature subpacket."""

    kind: int
    critical: bool
    body: bytes


@dataclass(frozen=True)
class _PgpSignature:
    """The fields of one parsed v4 RSA OpenPGP signature packet."""

    sig_type: int
    pubkey_algorithm: int
    hash_algorithm: int
    hashed_section: bytes
    hashed_subpackets: Tuple[_Subpacket, ...]
    unhashed_subpackets: Tuple[_Subpacket, ...]
    hash_prefix: bytes
    signature_value: bytes


class _ByteReader:
    """Bounds-checked sequential reader over one byte string."""

    def __init__(self, data: bytes, what: str) -> None:
        """Start reading `data`; `what` names it in error messages."""

        self._data = data
        self._what = what
        self.position = 0

    def remaining(self) -> int:
        """Return how many unread octets are left."""

        return len(self._data) - self.position

    def take(self, count: int, field: str) -> bytes:
        """Consume exactly `count` octets or raise on truncation."""

        if count < 0 or count > self.remaining():
            raise SignatureError(
                "{} is truncated while reading {}".format(self._what, field)
            )
        chunk = self._data[self.position : self.position + count]
        self.position += count
        return chunk

    def uint(self, width: int, field: str) -> int:
        """Consume a big-endian unsigned integer of `width` octets."""

        return int.from_bytes(self.take(width, field), "big")


_PINNED_ANCHOR = _TrustAnchor(
    key=_RsaPublicKey(PINNED_RSA_MODULUS, PINNED_RSA_EXPONENT),
    pgp_fingerprint=PINNED_PGP_FINGERPRINT,
    pgp_created=PINNED_PGP_KEY_CREATED,
    spki_sha256=PINNED_SPKI_SHA256,
)


# --- Key identity helpers --------------------------------------------------


def _der_length(length: int) -> bytes:
    """Encode one DER definite length."""

    if length < 0x80:
        return bytes((length,))
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(encoded),)) + encoded


def _der_tlv(tag: int, content: bytes) -> bytes:
    """Encode one DER tag-length-value triple."""

    return bytes((tag,)) + _der_length(len(content)) + content


def _der_unsigned_integer(value: int) -> bytes:
    """Encode a non-negative DER INTEGER, adding a sign octet when needed."""

    return _der_tlv(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def rsa_spki_der(modulus: int, exponent: int) -> bytes:
    """Return the DER SubjectPublicKeyInfo for an RSA public key."""

    rsa_public_key = _der_tlv(
        0x30, _der_unsigned_integer(modulus) + _der_unsigned_integer(exponent)
    )
    algorithm = _der_tlv(0x30, _der_tlv(0x06, _RSA_ENCRYPTION_OID) + b"\x05\x00")
    return _der_tlv(0x30, algorithm + _der_tlv(0x03, b"\x00" + rsa_public_key))


def spki_sha256(modulus: int, exponent: int) -> str:
    """Return the lowercase hex SHA-256 of an RSA key's DER SPKI."""

    return hashlib.sha256(rsa_spki_der(modulus, exponent)).hexdigest()


def _encode_mpi(value: int) -> bytes:
    """Encode one OpenPGP multiprecision integer."""

    bits = value.bit_length()
    return bits.to_bytes(2, "big") + value.to_bytes((bits + 7) // 8, "big")


def openpgp_v4_fingerprint(modulus: int, exponent: int, created: int) -> str:
    """Return the uppercase hex OpenPGP v4 fingerprint of an RSA key.

    RFC 4880 section 12.2 defines it as SHA-1 over 0x99, a two-octet length,
    and the public-key packet body. SHA-1 is the format's definition here,
    not a security decision: verification always uses the pinned modulus.
    """

    body = (
        bytes((4,))
        + created.to_bytes(4, "big")
        + bytes((_PGP_PUBKEY_RSA,))
        + _encode_mpi(modulus)
        + _encode_mpi(exponent)
    )
    framed = b"\x99" + len(body).to_bytes(2, "big") + body
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest().upper()


def _check_trust_anchor(anchor: _TrustAnchor) -> None:
    """Fail closed unless every anchor identifier derives from its key."""

    key = anchor.key
    if key.modulus <= 0 or key.exponent <= 1 or key.exponent % 2 == 0:
        raise SignatureError("Pinned release-signing key is malformed.")
    if spki_sha256(key.modulus, key.exponent) != anchor.spki_sha256:
        raise SignatureError(
            "Pinned release-signing key does not match its pinned SPKI SHA-256."
        )
    derived = openpgp_v4_fingerprint(key.modulus, key.exponent, anchor.pgp_created)
    if derived != anchor.pgp_fingerprint:
        raise SignatureError(
            "Pinned release-signing key does not match its pinned OpenPGP "
            "fingerprint."
        )


# --- RSA PKCS #1 v1.5 --------------------------------------------------------


def _emsa_pkcs1_v15_sha512(digest: bytes, length: int) -> bytes:
    """Return the RFC 8017 EMSA-PKCS1-v1_5 encoding of a SHA-512 digest."""

    if len(digest) != _SHA512_DIGEST_SIZE:
        raise SignatureError("SHA-512 digest has the wrong length.")
    digest_info = _DIGESTINFO_SHA512_PREFIX + digest
    if length < len(digest_info) + 11:
        raise SignatureError("RSA modulus is too short for SHA-512 signatures.")
    padding = b"\xff" * (length - len(digest_info) - 3)
    return b"\x00\x01" + padding + b"\x00" + digest_info


def _rsa_pkcs1_v15_sha512_verify(
    key: _RsaPublicKey, digest: bytes, signature: bytes, scheme: str
) -> None:
    """Verify an RSASSA-PKCS1-v1_5 SHA-512 signature or raise.

    The signature must be exactly k octets and less than the modulus, and
    the complete recovered block is compared with a freshly built encoding
    rather than parsed, which rules out padding and DigestInfo malleability.
    """

    length = key.size_bytes
    if len(signature) != length:
        raise SignatureError(
            "{} signature is {} octets; the pinned key requires {}.".format(
                scheme, len(signature), length
            )
        )
    value = int.from_bytes(signature, "big")
    if value >= key.modulus:
        raise SignatureError(
            "{} signature value is not less than the modulus.".format(scheme)
        )
    recovered = pow(value, key.exponent, key.modulus).to_bytes(length, "big")
    expected = _emsa_pkcs1_v15_sha512(digest, length)
    if not hmac.compare_digest(recovered, expected):
        raise SignatureError(
            "{} signature does not verify against the pinned key.".format(scheme)
        )


# --- Input hygiene -----------------------------------------------------------


def _require_bytes(
    value: object,
    what: str,
    limit: Optional[int] = None,
    error: Type[SignatureError] = SignatureError,
) -> bytes:
    """Return `value` as bytes, refusing text, empty, and oversized input."""

    if not isinstance(value, (bytes, bytearray)):
        raise error("{} must be bytes, not {}.".format(what, type(value).__name__))
    data = bytes(value)
    if not data:
        raise error("{} is empty.".format(what))
    if limit is not None and len(data) > limit:
        raise error("{} is {} bytes; the limit is {}.".format(what, len(data), limit))
    return data


def _strict_b64decode(text: str, what: str) -> bytes:
    """Decode canonical padded base64 or raise SignatureError."""

    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise SignatureError("{} is not valid base64.".format(what)) from exc
    # Re-encoding rejects stray padding or bits that a lenient decoder drops.
    if base64.b64encode(data).decode("ascii") != text:
        raise SignatureError("{} is not canonical base64.".format(what))
    return data


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """Build a JSON object, refusing duplicate member names."""

    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member {!r}".format(key))
        result[key] = value
    return result


def _reject_json_constant(name: str) -> Any:
    """Refuse the non-standard NaN and Infinity JSON literals."""

    raise ValueError("non-standard JSON constant {}".format(name))


def _load_json_object(
    data: bytes, what: str, error: Type[SignatureError]
) -> Dict[str, Any]:
    """Parse strict UTF-8 JSON whose top level must be an object."""

    try:
        text = data.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise error("{} is not valid JSON: {}".format(what, exc)) from exc
    if not isinstance(payload, dict):
        raise error("{} must be a JSON object.".format(what))
    return payload


# --- OpenPGP -----------------------------------------------------------------


def _crc24(data: bytes) -> int:
    """Return the RFC 4880 section 6.1 armor checksum of `data`."""

    crc = _CRC24_INIT
    for octet in data:
        crc ^= octet << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= _CRC24_POLY
    return crc & 0xFFFFFF


def _dearmor_signature(signature: bytes) -> bytes:
    """Return the packet bytes inside one ASCII-armored PGP SIGNATURE block.

    The armor must be the whole input apart from surrounding blank lines.
    A CRC-24 line, when present, must match; RFC 9580 section 6.1 made it
    optional, and it carries no security weight because the RSA signature
    authenticates the content.
    """

    try:
        text = signature.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SignatureError("Armored OpenPGP signature is not ASCII.") from exc
    lines = [line.rstrip(" \t\r") for line in text.split("\n")]
    # Surrounding blank lines are tolerated. Dropping trailing ones here lets
    # a missing END line surface as "ends inside" rather than a body error.
    while lines and lines[-1] == "":
        lines.pop()
    index = 0
    while index < len(lines) and lines[index] == "":
        index += 1
    if index >= len(lines) or lines[index] != _ARMOR_BEGIN:
        raise SignatureError("Armored OpenPGP data is not a PGP SIGNATURE block.")
    index += 1

    def next_line(context: str) -> str:
        """Return the next armor line or raise if the armor ends early."""

        nonlocal index
        if index >= len(lines):
            raise SignatureError(
                "Armored OpenPGP signature ends inside the {}.".format(context)
            )
        line = lines[index]
        index += 1
        return line

    while True:
        line = next_line("armor headers")
        if line == "":
            break
        if _ARMOR_HEADER_RE.fullmatch(line) is None:
            raise SignatureError("Armored OpenPGP signature has a malformed header.")

    body_lines: List[str] = []
    checksum: Optional[str] = None
    while True:
        line = next_line("base64 body")
        if line == _ARMOR_END:
            break
        if line.startswith("="):
            match = _ARMOR_CRC_RE.fullmatch(line)
            if match is None:
                raise SignatureError(
                    "Armored OpenPGP signature has a malformed checksum line."
                )
            checksum = match.group(1)
            if next_line("armor tail") != _ARMOR_END:
                raise SignatureError(
                    "Armored OpenPGP checksum is not followed by the END line."
                )
            break
        if _ARMOR_BODY_RE.fullmatch(line) is None:
            raise SignatureError("Armored OpenPGP signature body is not base64.")
        body_lines.append(line)
    if index < len(lines):
        raise SignatureError("Armored OpenPGP signature has trailing content.")
    if not body_lines:
        raise SignatureError("Armored OpenPGP signature has an empty body.")

    data = _strict_b64decode("".join(body_lines), "Armored OpenPGP signature body")
    if checksum is not None:
        expected = int.from_bytes(_strict_b64decode(checksum, "Armor checksum"), "big")
        if _crc24(data) != expected:
            raise SignatureError("Armored OpenPGP signature fails its CRC-24 check.")
    return data


def _signature_packet_body(data: bytes) -> bytes:
    """Return the body of the single signature packet that `data` must be.

    Old-format headers with one-, two-, or four-octet lengths and new-format
    headers with one-, two-, or five-octet lengths are accepted. Partial and
    indeterminate lengths are refused, as is any byte after the packet.
    """

    reader = _ByteReader(data, "OpenPGP signature packet")
    ctb = reader.uint(1, "packet tag")
    if not ctb & 0x80:
        raise SignatureError("OpenPGP data does not start with a packet header.")
    if ctb & 0x40:
        tag = ctb & 0x3F
        first = reader.uint(1, "packet length")
        if first < 192:
            length = first
        elif first < 224:
            length = ((first - 192) << 8) + reader.uint(1, "packet length") + 192
        elif first == 255:
            length = reader.uint(4, "packet length")
        else:
            raise SignatureError("OpenPGP signature packet uses a partial length.")
    else:
        tag = (ctb >> 2) & 0x0F
        length_type = ctb & 0x03
        if length_type == 3:
            raise SignatureError(
                "OpenPGP signature packet uses an indeterminate length."
            )
        length = reader.uint((1, 2, 4)[length_type], "packet length")
    if tag != _PGP_SIGNATURE_TAG:
        raise SignatureError(
            "OpenPGP packet has tag {}, not a signature packet.".format(tag)
        )
    body = reader.take(length, "packet body")
    if reader.remaining():
        raise SignatureError(
            "OpenPGP signature input has {} bytes after the signature "
            "packet; exactly one packet is accepted.".format(reader.remaining())
        )
    return body


def _parse_subpackets(area: bytes, area_name: str) -> Tuple[_Subpacket, ...]:
    """Split one subpacket area into subpackets (RFC 4880 section 5.2.3.1)."""

    reader = _ByteReader(area, "OpenPGP {} subpacket area".format(area_name))
    subpackets: List[_Subpacket] = []
    while reader.remaining():
        first = reader.uint(1, "subpacket length")
        if first < 192:
            length = first
        elif first < 255:
            length = ((first - 192) << 8) + reader.uint(1, "subpacket length") + 192
        else:
            length = reader.uint(4, "subpacket length")
        if length == 0:
            raise SignatureError(
                "OpenPGP {} subpacket has zero length.".format(area_name)
            )
        raw = reader.take(length, "subpacket body")
        subpackets.append(_Subpacket(raw[0] & 0x7F, bool(raw[0] & 0x80), raw[1:]))
    return tuple(subpackets)


def _parse_signature(body: bytes) -> _PgpSignature:
    """Parse a v4 RSA SHA-512 binary-document signature packet body.

    Version, signature type, and algorithms are checked before the rest of
    the body is parsed so that a wrong value produces a precise error.
    """

    reader = _ByteReader(body, "OpenPGP signature packet")
    version = reader.uint(1, "version")
    if version != _PGP_SIG_VERSION:
        raise SignatureError(
            "OpenPGP signature is version {}; only v4 is accepted.".format(version)
        )
    sig_type = reader.uint(1, "signature type")
    if sig_type != _PGP_SIG_TYPE_BINARY:
        raise SignatureError(
            "OpenPGP signature type is 0x{:02x}; only binary-document "
            "signatures (0x00) are accepted.".format(sig_type)
        )
    pubkey_algorithm = reader.uint(1, "public-key algorithm")
    if pubkey_algorithm != _PGP_PUBKEY_RSA:
        raise SignatureError(
            "OpenPGP signature public-key algorithm is {}; only RSA (1) is "
            "accepted.".format(pubkey_algorithm)
        )
    hash_algorithm = reader.uint(1, "hash algorithm")
    if hash_algorithm != _PGP_HASH_SHA512:
        raise SignatureError(
            "OpenPGP signature hash algorithm is {}; only SHA-512 (10) is "
            "accepted.".format(hash_algorithm)
        )
    hashed_area = reader.take(reader.uint(2, "hashed length"), "hashed subpackets")
    hashed_section = body[: reader.position]
    unhashed_area = reader.take(
        reader.uint(2, "unhashed length"), "unhashed subpackets"
    )
    hash_prefix = reader.take(2, "hash prefix")
    bits = reader.uint(2, "signature MPI length")
    value = reader.take((bits + 7) // 8, "signature MPI")
    if reader.remaining():
        raise SignatureError(
            "OpenPGP RSA signature packet has data after its single MPI."
        )
    if int.from_bytes(value, "big").bit_length() != bits:
        raise SignatureError("OpenPGP signature MPI is not minimally encoded.")
    return _PgpSignature(
        sig_type=sig_type,
        pubkey_algorithm=pubkey_algorithm,
        hash_algorithm=hash_algorithm,
        hashed_section=hashed_section,
        hashed_subpackets=_parse_subpackets(hashed_area, "hashed"),
        unhashed_subpackets=_parse_subpackets(unhashed_area, "unhashed"),
        hash_prefix=hash_prefix,
        signature_value=value,
    )


def _check_issuer_subpackets(
    hashed: Sequence[_Subpacket], unhashed: Sequence[_Subpacket], anchor: _TrustAnchor
) -> None:
    """Enforce the subpacket policy against the trust anchor.

    A hashed v4 issuer fingerprint naming the pinned key is mandatory.
    Every issuer fingerprint or key ID anywhere must also name it; a key ID
    is only ever an additional check. Unknown critical subpackets are
    refused (RFC 4880 section 5.2.3.1), and the hashed creation time may not
    predate the pinned key.
    """

    fingerprint = bytes.fromhex(anchor.pgp_fingerprint)
    key_id = fingerprint[-8:]
    for area_name, subpackets in (("hashed", hashed), ("unhashed", unhashed)):
        for subpacket in subpackets:
            if subpacket.critical and subpacket.kind not in _KNOWN_SUBPACKETS:
                raise SignatureError(
                    "OpenPGP signature has unsupported critical {} subpacket "
                    "type {}.".format(area_name, subpacket.kind)
                )
            if subpacket.kind == _SUBPACKET_ISSUER_FINGERPRINT:
                if subpacket.body[:1] != b"\x04" or len(subpacket.body) != 21:
                    raise SignatureError(
                        "OpenPGP issuer fingerprint is not a v4 fingerprint."
                    )
                if not hmac.compare_digest(subpacket.body[1:], fingerprint):
                    raise SignatureError(
                        "OpenPGP issuer fingerprint {} is not the pinned "
                        "fingerprint {}.".format(
                            subpacket.body[1:].hex().upper(), anchor.pgp_fingerprint
                        )
                    )
            elif subpacket.kind == _SUBPACKET_ISSUER_KEY_ID:
                if not hmac.compare_digest(subpacket.body, key_id):
                    raise SignatureError(
                        "OpenPGP issuer key ID {} does not belong to the pinned "
                        "key.".format(subpacket.body.hex().upper())
                    )

    fingerprints = [s for s in hashed if s.kind == _SUBPACKET_ISSUER_FINGERPRINT]
    if len(fingerprints) != 1:
        raise SignatureError(
            "OpenPGP signature must carry exactly one hashed issuer-fingerprint "
            "subpacket; found {}.".format(len(fingerprints))
        )
    creation = [s for s in hashed if s.kind == _SUBPACKET_CREATION_TIME]
    if len(creation) != 1 or len(creation[0].body) != 4:
        raise SignatureError(
            "OpenPGP signature must carry exactly one hashed four-octet "
            "creation-time subpacket."
        )
    if int.from_bytes(creation[0].body, "big") < anchor.pgp_created:
        raise SignatureError("OpenPGP signature predates the pinned signing key.")


def _verify_pgp_detached_with(
    anchor: _TrustAnchor, data: bytes, signature: bytes
) -> None:
    """Verify one OpenPGP detached signature over `data` against `anchor`."""

    raw = signature.lstrip(b" \t\r\n")
    if raw.startswith(b"-----BEGIN PGP"):
        raw = _dearmor_signature(signature)
    else:
        raw = signature
    parsed = _parse_signature(_signature_packet_body(raw))
    _check_issuer_subpackets(
        parsed.hashed_subpackets, parsed.unhashed_subpackets, anchor
    )

    # RFC 4880 section 5.2.4: hash the document, the hashed part of the
    # packet, then a v4 trailer of 0x04 0xFF and its four-octet length.
    hasher = hashlib.sha512()
    hasher.update(data)
    hasher.update(parsed.hashed_section)
    hasher.update(b"\x04\xff" + len(parsed.hashed_section).to_bytes(4, "big"))
    digest = hasher.digest()
    if digest[:2] != parsed.hash_prefix:
        raise SignatureError(
            "OpenPGP signature hash prefix does not match the signed data."
        )

    # MPIs drop leading zero octets, so a valid value may be shorter than
    # the modulus; I2OSP restores the k-octet form before the RSA check.
    length = anchor.key.size_bytes
    if len(parsed.signature_value) > length:
        raise SignatureError(
            "OpenPGP signature value is {} octets; the pinned key allows at "
            "most {}.".format(len(parsed.signature_value), length)
        )
    padded = parsed.signature_value.rjust(length, b"\x00")
    _rsa_pkcs1_v15_sha512_verify(anchor.key, digest, padded, "OpenPGP")


def _verify_raw_signature_with(
    anchor: _TrustAnchor, data: bytes, sidecar_json: bytes
) -> None:
    """Verify one raw-signature sidecar over `data` against `anchor`."""

    payload = _load_json_object(sidecar_json, "Raw signature sidecar", SignatureError)
    schema = payload.get("schema")
    if type(schema) is not int or schema != RAW_SIGNATURE_SCHEMA:
        raise SignatureError(
            "Raw signature sidecar schema is {!r}; expected {}.".format(
                schema, RAW_SIGNATURE_SCHEMA
            )
        )
    algorithm = payload.get("algorithm")
    if algorithm != RAW_SIGNATURE_ALGORITHM:
        raise SignatureError(
            "Raw signature algorithm is {!r}; only {} is accepted.".format(
                algorithm, RAW_SIGNATURE_ALGORITHM
            )
        )
    key_hash = payload.get("publicKeySha256")
    if not isinstance(key_hash, str) or _HEX64_RE.fullmatch(key_hash) is None:
        raise SignatureError("Raw signature publicKeySha256 is not a SHA-256 value.")
    if not hmac.compare_digest(key_hash.lower(), anchor.spki_sha256):
        raise SignatureError(
            "Raw signature names key {}, not the pinned key {}.".format(
                key_hash.lower(), anchor.spki_sha256
            )
        )
    encoded = payload.get("signature")
    if not isinstance(encoded, str):
        raise SignatureError("Raw signature sidecar has no signature string.")
    signature = _strict_b64decode(encoded, "Raw signature value")
    digest = hashlib.sha512(RAW_SIGNATURE_PREFIX + data).digest()
    _rsa_pkcs1_v15_sha512_verify(anchor.key, digest, signature, "Raw RSA")


# --- Public API --------------------------------------------------------------


def verify_pgp_detached(data: bytes, signature: bytes) -> None:
    """Verify an OpenPGP detached signature over `data` with the pinned key.

    `signature` may be binary or ASCII-armored. Raises SignatureError on any
    structural, policy, or cryptographic failure.
    """

    payload = _require_bytes(data, "Signed data")
    sig = _require_bytes(signature, "OpenPGP signature", MAX_SIGNATURE_BYTES)
    _check_trust_anchor(_PINNED_ANCHOR)
    _verify_pgp_detached_with(_PINNED_ANCHOR, payload, sig)


def verify_raw_signature(data: bytes, sidecar_json: bytes) -> None:
    """Verify a `manifest.json.raw-sig.json` sidecar over `data`.

    The signed message is `RAW_SIGNATURE_PREFIX + data`. Raises
    SignatureError on any structural, policy, or cryptographic failure.
    """

    payload = _require_bytes(data, "Signed data")
    sidecar = _require_bytes(sidecar_json, "Raw signature sidecar", MAX_SIGNATURE_BYTES)
    _check_trust_anchor(_PINNED_ANCHOR)
    _verify_raw_signature_with(_PINNED_ANCHOR, payload, sidecar)


def verify_manifest_signatures(
    manifest_bytes: bytes, pgp_signature: bytes, raw_signature_json: bytes
) -> Tuple[str, ...]:
    """Require both signature schemes to verify over the manifest bytes.

    Returns the verified scheme names in canonical order, which is the value
    `parse_verified_manifest()` expects.
    """

    verify_pgp_detached(manifest_bytes, pgp_signature)
    verify_raw_signature(manifest_bytes, raw_signature_json)
    return REQUIRED_SCHEMES


def _require_schemes(schemes: Iterable[str]) -> Tuple[str, ...]:
    """Refuse to parse unless both signature schemes are claimed verified."""

    if isinstance(schemes, (str, bytes)):
        raise ManifestError("Verified schemes must be a sequence of names.")
    claimed = tuple(schemes)
    if len(set(claimed)) != len(claimed) or set(claimed) != set(REQUIRED_SCHEMES):
        raise ManifestError(
            "Manifest parsing requires verified schemes {}; got {}.".format(
                REQUIRED_SCHEMES, claimed
            )
        )
    return REQUIRED_SCHEMES


def _optional_string(payload: Dict[str, Any], key: str) -> Optional[str]:
    """Return an optional string member, refusing other JSON types."""

    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError("Manifest {} must be a string.".format(key))
    return value


def _parse_artifact(platform_key: object, entry: object) -> PlatformArtifact:
    """Validate one `platforms` member of a manifest."""

    if (
        not isinstance(platform_key, str)
        or _PLATFORM_KEY_RE.fullmatch(platform_key) is None
    ):
        raise ManifestError(
            "Manifest platform key {!r} is invalid.".format(platform_key)
        )
    if not isinstance(entry, dict):
        raise ManifestError(
            "Manifest platform {} must be a JSON object.".format(platform_key)
        )
    binary = entry.get("binary")
    if not isinstance(binary, str) or _BINARY_NAME_RE.fullmatch(binary) is None:
        raise ManifestError(
            "Manifest platform {} binary {!r} is not a plain file name.".format(
                platform_key, binary
            )
        )
    checksum = entry.get("checksum")
    if not isinstance(checksum, str) or _HEX64_RE.fullmatch(checksum) is None:
        raise ManifestError(
            "Manifest platform {} checksum is not 64 hex digits.".format(platform_key)
        )
    size = entry.get("size")
    # `type(...) is int` also refuses JSON booleans, which are ints in Python.
    if type(size) is not int or size <= 0:
        raise ManifestError(
            "Manifest platform {} size {!r} is not a positive integer.".format(
                platform_key, size
            )
        )
    return PlatformArtifact(platform_key, binary, checksum.lower(), size)


def parse_verified_manifest(
    manifest_bytes: bytes, expected_version: str, schemes: Tuple[str, ...]
) -> VerifiedManifest:
    """Parse manifest bytes whose signatures `verify_manifest_signatures` passed.

    Every platform entry must be well formed: a bare `binary` file name, a
    64-hex-digit `checksum`, and a positive integer `size`. No manifest
    field, including `manifestSignatureEnforcement`, relaxes this policy.
    Raises ManifestError on any violation.
    """

    verified_schemes = _require_schemes(schemes)
    data = _require_bytes(manifest_bytes, "Manifest", error=ManifestError)
    if not isinstance(expected_version, str) or not expected_version:
        raise ManifestError("Expected manifest version must be a non-empty string.")
    payload = _load_json_object(data, "Manifest", ManifestError)
    version = payload.get("version")
    if not isinstance(version, str) or version != expected_version:
        raise ManifestError(
            "Manifest version {!r} does not match requested version {}.".format(
                version, expected_version
            )
        )
    platforms = payload.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ManifestError("Manifest platforms must be a non-empty JSON object.")
    artifacts = {key: _parse_artifact(key, entry) for key, entry in platforms.items()}
    return VerifiedManifest(
        version=version,
        build_date=_optional_string(payload, "buildDate"),
        commit=_optional_string(payload, "commit"),
        manifest_sha256=hashlib.sha256(data).hexdigest(),
        artifacts=artifacts,
        schemes=verified_schemes,
    )


def verify_and_parse_manifest(
    manifest_bytes: bytes,
    pgp_signature: bytes,
    raw_signature_json: bytes,
    expected_version: str,
) -> VerifiedManifest:
    """Verify both signatures, then parse; the one-call path for callers."""

    schemes = verify_manifest_signatures(
        manifest_bytes, pgp_signature, raw_signature_json
    )
    return parse_verified_manifest(manifest_bytes, expected_version, schemes)
