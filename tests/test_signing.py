"""Tests for pinned-key verification of Claude Code release manifests.

Real manifests, signatures, and published keys recorded from
downloads.claude.ai live under tests/fixtures/releases/ (see its README.md).
A deterministic throwaway RSA key, generated from a fixed seed, lets the
policy tests build signatures that are cryptographically valid yet differ
from an acceptable one in exactly one field, which proves each policy check
works on its own rather than only through the final RSA comparison. No test
uses the network.
"""

from __future__ import annotations

import ast
import base64
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import textwrap
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from claude_wrangler import signing
from claude_wrangler.models import ClaudeWranglerError
from claude_wrangler.signing import (
    MAX_SIGNATURE_BYTES,
    PINNED_PGP_FINGERPRINT,
    PINNED_PGP_KEY_CREATED,
    PINNED_RSA_EXPONENT,
    PINNED_RSA_MODULUS,
    PINNED_SPKI_SHA256,
    PUBLISHED_KEY_FILE_SHA256,
    RAW_SIGNATURE_PREFIX,
    ManifestError,
    PlatformArtifact,
    SignatureError,
    VerifiedManifest,
    openpgp_v4_fingerprint,
    parse_verified_manifest,
    rsa_spki_der,
    spki_sha256,
    verify_and_parse_manifest,
    verify_manifest_signatures,
    verify_pgp_detached,
    verify_raw_signature,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "releases"
REAL_VERSIONS = ("2.1.207", "2.1.287", "2.1.296")
LATEST = "2.1.296"
SCHEMES = ("openpgp", "raw-rsa")
EXPECTED_PLATFORMS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "linux-arm64-musl",
    "linux-x64-musl",
    "win32-arm64",
    "win32-x64",
}
# SHA-256 of each recorded fixture as fetched on 2026-10-12. A mismatch
# usually means Git rewrote line endings; .gitattributes must say "* -text".
FIXTURE_SHA256 = {
    "2.1.207/manifest.json": (
        "c083ea6edea8e76c1dd1427d812f2a879a25902d86a931910534fb1fd262b4f6"
    ),
    "2.1.207/manifest.json.raw-sig.json": (
        "4739fbee489f54396baff72c4491d476b15df80a5a56d0ef26602b713075ccb9"
    ),
    "2.1.207/manifest.json.sig": (
        "2e06094aa7e030f7ba5b200482d0099af98fe64e9d7e8076824b0bb64b330498"
    ),
    "2.1.287/manifest.json": (
        "7654db465063ffed4718ac52ad9eaaba489511bb326abdbbbc2203e44b8e61fe"
    ),
    "2.1.287/manifest.json.raw-sig.json": (
        "eeeba8132ed31fb8e9760c6eefe5490c82725955f56eb6e2bfde596a576d1365"
    ),
    "2.1.287/manifest.json.sig": (
        "5de6c29a8932c6c017ba5d8822521910d0672875c04a21046f29ffa439525904"
    ),
    "2.1.296/manifest.json": (
        "e50e872d91f3b7da2fb3d8d38505d709e0a90f48da1951dd51176930c8da8270"
    ),
    "2.1.296/manifest.json.raw-sig.json": (
        "81ffbda87083a0c990b6d9deee59207b8abc251305621b84bae358fa77f2037d"
    ),
    "2.1.296/manifest.json.sig": (
        "ab95ac645a820036734e38a6fc21d41f0535412722a463d91e0b6077082e29c9"
    ),
    "keys/claude-code.asc": (
        "bd70a5e4a268002704024ceba7f8446024114e94f3f0bdd11c23a9e592be81c6"
    ),
    "keys/claude-code.rsa.pub": (
        "395759c1f7449ef4cdef305a42e820f3c766d6090d142634ebdb049f113168b6"
    ),
}
SHA512_DIGESTINFO = bytes.fromhex("3051300d060960864801650304020305000440")
SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
# SHA-512 DigestInfo with the NULL parameters omitted, a non-canonical form.
SHA512_DIGESTINFO_NO_NULL = bytes.fromhex("304f300b0609608648016503040203" "0440")
TEST_KEY_CREATED = 1_700_000_000


# --- Fixture access ----------------------------------------------------------


def _fixture(version: str, name: str) -> bytes:
    """Return one recorded release file's exact bytes."""

    return (FIXTURES / version / name).read_bytes()


def _release(version: str) -> Tuple[bytes, bytes, bytes]:
    """Return (manifest, armored .sig, raw-sig sidecar) for one release."""

    return (
        _fixture(version, "manifest.json"),
        _fixture(version, "manifest.json.sig"),
        _fixture(version, "manifest.json.raw-sig.json"),
    )


def _simple_dearmor(armored: bytes) -> bytes:
    """Decode an armored block's body without checking its CRC."""

    lines = armored.decode("ascii").splitlines()
    start = lines.index("") + 1
    body: List[str] = []
    for line in lines[start:]:
        if line.startswith("=") or line.startswith("-----"):
            break
        body.append(line)
    return base64.b64decode("".join(body))


def _armor(
    packet: bytes,
    *,
    checksum: bool = True,
    headers: Tuple[str, ...] = (),
    newline: str = "\n",
    label: str = "PGP SIGNATURE",
) -> bytes:
    """Armor packet bytes with 64-column lines, as GnuPG does."""

    encoded = base64.b64encode(packet).decode("ascii")
    lines = ["-----BEGIN {}-----".format(label), *headers, ""]
    lines.extend(encoded[i : i + 64] for i in range(0, len(encoded), 64))
    if checksum:
        crc = signing._crc24(packet).to_bytes(3, "big")
        lines.append("=" + base64.b64encode(crc).decode("ascii"))
    lines.append("-----END {}-----".format(label))
    return (newline.join(lines) + newline).encode("ascii")


def _binary_sig(version: str = LATEST) -> bytes:
    """Return one release's OpenPGP signature as a binary packet."""

    return _simple_dearmor(_fixture(version, "manifest.json.sig"))


# --- Signature packet surgery ----------------------------------------------


def _u16(value: int) -> bytes:
    """Encode a big-endian two-octet integer."""

    return value.to_bytes(2, "big")


def _split_packet(packet: bytes) -> Dict[str, Any]:
    """Split an old-format two-octet-length signature packet into fields."""

    if packet[0] != 0x89:
        raise ValueError("test helper expects ctb 0x89")
    body = packet[3 : 3 + int.from_bytes(packet[1:3], "big")]
    hashed_length = int.from_bytes(body[4:6], "big")
    index = 6 + hashed_length
    unhashed_length = int.from_bytes(body[index : index + 2], "big")
    unhashed = body[index + 2 : index + 2 + unhashed_length]
    index += 2 + unhashed_length
    return {
        "version": body[0],
        "sig_type": body[1],
        "pk": body[2],
        "hash": body[3],
        "hashed": body[6 : 6 + hashed_length],
        "unhashed": unhashed,
        "prefix": body[index : index + 2],
        "bits": int.from_bytes(body[index + 2 : index + 4], "big"),
        "mpi": body[index + 4 :],
    }


def _packet_body(parts: Dict[str, Any]) -> bytes:
    """Serialize signature fields into a packet body."""

    return (
        bytes((parts["version"], parts["sig_type"], parts["pk"], parts["hash"]))
        + _u16(len(parts["hashed"]))
        + parts["hashed"]
        + _u16(len(parts["unhashed"]))
        + parts["unhashed"]
        + parts["prefix"]
        + _u16(parts["bits"])
        + parts["mpi"]
    )


def _join_packet(parts: Dict[str, Any]) -> bytes:
    """Serialize signature fields as an old-format packet."""

    body = _packet_body(parts)
    return b"\x89" + _u16(len(body)) + body


def _subpacket(kind: int, body: bytes, critical: bool = False) -> bytes:
    """Encode one subpacket with a one-octet length."""

    return bytes((len(body) + 1, kind | (0x80 if critical else 0))) + body


def _subpacket_list(area: bytes) -> List[Tuple[int, bytes]]:
    """Split a subpacket area that uses one-octet lengths."""

    result: List[Tuple[int, bytes]] = []
    index = 0
    while index < len(area):
        length = area[index]
        result.append((area[index + 1], area[index + 2 : index + 1 + length]))
        index += 1 + length
    return result


def _encode_subpackets(items: List[Tuple[int, bytes]]) -> bytes:
    """Encode (type octet, body) pairs with one-octet lengths."""

    return b"".join(bytes((len(body) + 1, kind)) + body for kind, body in items)


def _replace_subpacket(area: bytes, kind: int, body: bytes) -> bytes:
    """Return `area` with every subpacket of `kind` given a new body."""

    return _encode_subpackets(
        [(k, body if k & 0x7F == kind else b) for k, b in _subpacket_list(area)]
    )


def _drop_subpacket(area: bytes, kind: int) -> bytes:
    """Return `area` without subpackets of `kind`."""

    return _encode_subpackets([(k, b) for k, b in _subpacket_list(area) if k != kind])


def _subpacket_body(area: bytes, kind: int) -> bytes:
    """Return the body of the first subpacket of `kind`."""

    for k, body in _subpacket_list(area):
        if k & 0x7F == kind:
            return body
    raise KeyError(kind)


def _mpi(value: int) -> Tuple[int, bytes]:
    """Return (bit count, minimal octets) for one MPI."""

    bits = value.bit_length()
    return bits, value.to_bytes((bits + 7) // 8, "big")


def _mutated(mutate: Callable[[Dict[str, Any]], Dict[str, Any]]) -> bytes:
    """Apply one field mutation to the latest real signature packet."""

    return _join_packet(mutate(_split_packet(_binary_sig())))


# --- Deterministic throwaway RSA key ---------------------------------------


def _small_primes(limit: int) -> List[int]:
    """Return the primes below `limit`."""

    sieve = bytearray([1]) * limit
    sieve[0:2] = b"\x00\x00"
    for value in range(2, int(limit**0.5) + 1):
        if sieve[value]:
            sieve[value * value :: value] = bytearray(
                len(sieve[value * value :: value])
            )
    return [value for value in range(limit) if sieve[value]]


SMALL_PRIMES = _small_primes(2000)


def _is_probable_prime(candidate: int, rng: random.Random) -> bool:
    """Run trial division and 40 Miller-Rabin rounds."""

    for prime in SMALL_PRIMES:
        if candidate % prime == 0:
            return candidate == prime
    odd, rounds = candidate - 1, 0
    while odd % 2 == 0:
        odd //= 2
        rounds += 1
    for _ in range(40):
        witness = pow(rng.randrange(2, candidate - 2), odd, candidate)
        if witness in (1, candidate - 1):
            continue
        for _ in range(rounds - 1):
            witness = pow(witness, 2, candidate)
            if witness == candidate - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int, exponent: int, rng: random.Random) -> int:
    """Return a prime with its top two bits set and p - 1 coprime to e."""

    while True:
        candidate = rng.getrandbits(bits) | (3 << (bits - 2)) | 1
        if (candidate - 1) % exponent and _is_probable_prime(candidate, rng):
            return candidate


@dataclass(frozen=True)
class ThrowawayKey:
    """A deterministic test-only RSA key with CRT signing parameters."""

    p: int
    q: int
    e: int
    d: int

    @property
    def n(self) -> int:
        """Return the modulus."""

        return self.p * self.q

    @property
    def k(self) -> int:
        """Return the modulus length in octets."""

        return (self.n.bit_length() + 7) // 8

    @property
    def fingerprint(self) -> str:
        """Return the key's OpenPGP v4 fingerprint."""

        return openpgp_v4_fingerprint(self.n, self.e, TEST_KEY_CREATED)

    @property
    def anchor(self) -> Any:
        """Return a trust anchor that pins this key."""

        return signing._TrustAnchor(
            key=signing._RsaPublicKey(self.n, self.e),
            pgp_fingerprint=self.fingerprint,
            pgp_created=TEST_KEY_CREATED,
            spki_sha256=spki_sha256(self.n, self.e),
        )

    def sign_block(self, block: bytes) -> bytes:
        """Apply the raw RSA private operation to an encoded block."""

        value = int.from_bytes(block, "big")
        m1 = pow(value, self.d % (self.p - 1), self.p)
        m2 = pow(value, self.d % (self.q - 1), self.q)
        h = (pow(self.q, -1, self.p) * (m1 - m2)) % self.p
        return (m2 + h * self.q).to_bytes(self.k, "big")


@pytest.fixture(scope="module")
def throwaway_key() -> ThrowawayKey:
    """Generate one 2048-bit key from a fixed seed."""

    rng = random.Random(20261012)
    exponent = 65537
    p = _generate_prime(1024, exponent, rng)
    q = _generate_prime(1024, exponent, rng)
    d = pow(exponent, -1, (p - 1) * (q - 1))
    return ThrowawayKey(p=p, q=q, e=exponent, d=d)


def _pkcs1_block(
    digest: bytes, k: int, digest_info: bytes = SHA512_DIGESTINFO
) -> bytes:
    """Build a correct EMSA-PKCS1-v1_5 block for `digest`."""

    t = digest_info + digest
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def _default_hashed(fingerprint: str, created: int) -> bytes:
    """Return the hashed subpackets an acceptable signature carries."""

    return _subpacket(33, b"\x04" + bytes.fromhex(fingerprint)) + _subpacket(
        2, created.to_bytes(4, "big")
    )


def _pgp_sign(
    key: ThrowawayKey,
    data: bytes,
    *,
    sig_type: int = 0x00,
    hash_algorithm: int = 10,
    hash_factory: Callable[[], Any] = hashlib.sha512,
    digest_info: bytes = SHA512_DIGESTINFO,
    hashed: Optional[bytes] = None,
    unhashed: Optional[bytes] = None,
) -> bytes:
    """Return a binary v4 RSA signature packet over `data` by `key`."""

    if hashed is None:
        hashed = _default_hashed(key.fingerprint, TEST_KEY_CREATED + 1)
    if unhashed is None:
        unhashed = _subpacket(16, bytes.fromhex(key.fingerprint)[-8:])
    section = bytes((4, sig_type, 1, hash_algorithm)) + _u16(len(hashed)) + hashed
    hasher = hash_factory()
    hasher.update(data + section + b"\x04\xff" + len(section).to_bytes(4, "big"))
    digest = hasher.digest()
    block = _pkcs1_block(digest, key.k, digest_info)
    value = int.from_bytes(key.sign_block(block), "big")
    bits, mpi = _mpi(value)
    return _join_packet(
        {
            "version": 4,
            "sig_type": sig_type,
            "pk": 1,
            "hash": hash_algorithm,
            "hashed": hashed,
            "unhashed": unhashed,
            "prefix": digest[:2],
            "bits": bits,
            "mpi": mpi,
        }
    )


def _sidecar(signature: bytes, **overrides: Any) -> bytes:
    """Return a raw-signature sidecar document as bytes."""

    payload: Dict[str, Any] = {
        "schema": 1,
        "algorithm": "RSASSA-PKCS1-v1_5-SHA512",
        "signature": base64.b64encode(signature).decode("ascii"),
        "publicKeySha256": PINNED_SPKI_SHA256,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _raw_sidecar_value(version: str = LATEST) -> bytes:
    """Return one release's decoded raw RSA signature value."""

    sidecar = json.loads(_fixture(version, "manifest.json.raw-sig.json"))
    return base64.b64decode(sidecar["signature"])


# --- Trust-anchor provenance -----------------------------------------------


def test_fixture_files_are_byte_exact():
    attributes = (FIXTURES / ".gitattributes").read_bytes()
    assert attributes.splitlines()[0] == b"* -text"
    for relative, expected in FIXTURE_SHA256.items():
        actual = hashlib.sha256((FIXTURES / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def test_key_file_hash_matches_documented_value():
    pem = (FIXTURES / "keys" / "claude-code.rsa.pub").read_bytes()
    assert hashlib.sha256(pem).hexdigest() == PUBLISHED_KEY_FILE_SHA256


def test_pinned_modulus_and_exponent_reproduce_recorded_pem():
    pem = (FIXTURES / "keys" / "claude-code.rsa.pub").read_bytes()
    der = base64.b64decode(
        b"".join(line for line in pem.splitlines() if not line.startswith(b"-----"))
    )
    assert rsa_spki_der(PINNED_RSA_MODULUS, PINNED_RSA_EXPONENT) == der
    assert hashlib.sha256(der).hexdigest() == PINNED_SPKI_SHA256
    assert spki_sha256(PINNED_RSA_MODULUS, PINNED_RSA_EXPONENT) == PINNED_SPKI_SHA256
    assert PINNED_RSA_MODULUS.bit_length() == 4096
    assert PINNED_RSA_EXPONENT == 65537

    encoded = base64.b64encode(der).decode("ascii")
    rebuilt = "-----BEGIN PUBLIC KEY-----\n"
    rebuilt += "".join(encoded[i : i + 64] + "\n" for i in range(0, len(encoded), 64))
    rebuilt += "-----END PUBLIC KEY-----\n"
    assert rebuilt.encode("ascii") == pem


def _openpgp_packets(data: bytes) -> List[Tuple[int, bytes]]:
    """Split a transferable public key into (tag, body) pairs."""

    packets: List[Tuple[int, bytes]] = []
    index = 0
    while index < len(data):
        ctb = data[index]
        if ctb & 0x40:
            tag, first = ctb & 0x3F, data[index + 1]
            if first < 192:
                length, header = first, 2
            elif first < 224:
                length, header = ((first - 192) << 8) + data[index + 2] + 192, 3
            else:
                length, header = int.from_bytes(data[index + 2 : index + 6], "big"), 6
        else:
            tag, width = (ctb >> 2) & 0x0F, (1, 2, 4)[ctb & 3]
            length = int.from_bytes(data[index + 1 : index + 1 + width], "big")
            header = 1 + width
        packets.append((tag, data[index + header : index + header + length]))
        index += header + length
    return packets


def test_pinned_fingerprint_names_recorded_openpgp_key():
    asc = (FIXTURES / "keys" / "claude-code.asc").read_bytes()
    packets = _openpgp_packets(_simple_dearmor(asc))
    tags = [tag for tag, _ in packets]
    # One primary key, one user ID, one self-signature, and no subkeys, so
    # the release signatures can only come from the primary key.
    assert tags == [6, 13, 2]
    body = packets[0][1]
    assert body[0] == 4 and body[5] == 1
    assert int.from_bytes(body[1:5], "big") == PINNED_PGP_KEY_CREATED
    bits = int.from_bytes(body[6:8], "big")
    modulus_end = 8 + (bits + 7) // 8
    assert int.from_bytes(body[8:modulus_end], "big") == PINNED_RSA_MODULUS
    exponent_bits = int.from_bytes(body[modulus_end : modulus_end + 2], "big")
    exponent = body[modulus_end + 2 : modulus_end + 2 + (exponent_bits + 7) // 8]
    assert int.from_bytes(exponent, "big") == PINNED_RSA_EXPONENT

    framed = b"\x99" + _u16(len(body)) + body
    assert hashlib.sha1(framed).hexdigest().upper() == PINNED_PGP_FINGERPRINT
    assert (
        openpgp_v4_fingerprint(
            PINNED_RSA_MODULUS, PINNED_RSA_EXPONENT, PINNED_PGP_KEY_CREATED
        )
        == PINNED_PGP_FINGERPRINT
    )
    assert b"security@anthropic.com" in packets[1][1]


@pytest.mark.parametrize("version", REAL_VERSIONS)
def test_recorded_signatures_name_the_pinned_key(version):
    sidecar = json.loads(_fixture(version, "manifest.json.raw-sig.json"))
    assert sidecar["publicKeySha256"] == PINNED_SPKI_SHA256
    parts = _split_packet(_binary_sig(version))
    assert _subpacket_body(parts["hashed"], 33) == b"\x04" + bytes.fromhex(
        PINNED_PGP_FINGERPRINT
    )


@pytest.mark.parametrize(
    "change",
    [
        {"pgp_fingerprint": "00" * 20},
        {"spki_sha256": "00" * 32},
        {"pgp_created": PINNED_PGP_KEY_CREATED + 1},
        {"key": signing._RsaPublicKey(PINNED_RSA_MODULUS + 2, PINNED_RSA_EXPONENT)},
    ],
    ids=["fingerprint", "spki", "created", "modulus"],
)
def test_partially_edited_anchor_fails_closed(monkeypatch, change):
    manifest, pgp, raw = _release(LATEST)
    monkeypatch.setattr(
        signing, "_PINNED_ANCHOR", replace(signing._PINNED_ANCHOR, **change)
    )
    with pytest.raises(SignatureError, match="Pinned release-signing key"):
        verify_pgp_detached(manifest, pgp)
    with pytest.raises(SignatureError, match="Pinned release-signing key"):
        verify_raw_signature(manifest, raw)


def test_module_uses_no_assert_and_no_external_key_sources():
    source = Path(signing.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported <= {
        "__future__",
        "base64",
        "binascii",
        "dataclasses",
        "hashlib",
        "hmac",
        "json",
        "re",
        "typing",
        ".models",
    }


def test_error_hierarchy():
    assert issubclass(SignatureError, ClaudeWranglerError)
    assert issubclass(ManifestError, SignatureError)


# --- Real releases -----------------------------------------------------------


@pytest.mark.parametrize("version", REAL_VERSIONS)
def test_real_release_signatures_verify(version):
    manifest, pgp, raw = _release(version)
    verify_pgp_detached(manifest, pgp)
    verify_raw_signature(manifest, raw)
    assert verify_manifest_signatures(manifest, pgp, raw) == SCHEMES


@pytest.mark.parametrize("version", REAL_VERSIONS)
def test_parse_verified_manifest_extracts_all_platforms(version):
    manifest, pgp, raw = _release(version)
    schemes = verify_manifest_signatures(manifest, pgp, raw)
    parsed = parse_verified_manifest(manifest, version, schemes)
    document = json.loads(manifest)

    assert isinstance(parsed, VerifiedManifest)
    assert parsed.version == version
    assert parsed.build_date == document["buildDate"]
    assert parsed.commit == document["commit"]
    assert parsed.manifest_sha256 == hashlib.sha256(manifest).hexdigest()
    assert parsed.schemes == SCHEMES
    assert set(parsed.artifacts) == EXPECTED_PLATFORMS
    for key, artifact in parsed.artifacts.items():
        entry = document["platforms"][key]
        assert artifact == PlatformArtifact(
            key, entry["binary"], entry["checksum"], entry["size"]
        )
        expected_binary = "claude.exe" if key.startswith("win32-") else "claude"
        assert artifact.binary == expected_binary
        assert artifact.size > 0
    assert parsed.artifact_for("win32-x64").binary == "claude.exe"
    with pytest.raises(ManifestError, match="win32-ia64"):
        parsed.artifact_for("win32-ia64")


def test_verify_and_parse_manifest_round_trip():
    manifest, pgp, raw = _release(LATEST)
    parsed = verify_and_parse_manifest(manifest, pgp, raw, LATEST)
    assert parsed.version == LATEST
    with pytest.raises(ManifestError, match="does not match requested version"):
        verify_and_parse_manifest(manifest, pgp, raw, "2.1.287")


def test_signatures_do_not_transfer_between_releases():
    manifest = _fixture(LATEST, "manifest.json")
    with pytest.raises(SignatureError):
        verify_pgp_detached(manifest, _fixture("2.1.287", "manifest.json.sig"))
    with pytest.raises(SignatureError, match="does not verify"):
        verify_raw_signature(
            manifest, _fixture("2.1.287", "manifest.json.raw-sig.json")
        )


def _flip(data: bytes, index: int) -> bytes:
    """Return `data` with one bit flipped at `index`."""

    mutable = bytearray(data)
    mutable[index] ^= 0x01
    return bytes(mutable)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda m: _flip(m, 0),
        lambda m: _flip(m, len(m) // 2),
        lambda m: _flip(m, len(m) - 1),
        lambda m: m + b"\n",
        lambda m: m[:-1],
        lambda m: m.replace(b"\n", b"\r\n"),
        lambda m: m.replace(b'"size": 2', b'"size": 3', 1),
        lambda m: m.replace(b'"manifestSignatureEnforcement": "flag"', b'"x": 1'),
    ],
    ids=["first", "middle", "last", "append", "truncate", "crlf", "size", "field"],
)
def test_tampered_manifest_fails_both_schemes(tamper):
    manifest, pgp, raw = _release(LATEST)
    tampered = tamper(manifest)
    assert tampered != manifest
    with pytest.raises(SignatureError):
        verify_pgp_detached(tampered, pgp)
    with pytest.raises(SignatureError, match="does not verify"):
        verify_raw_signature(tampered, raw)
    with pytest.raises(SignatureError):
        verify_manifest_signatures(tampered, pgp, raw)


def test_manifest_fields_cannot_relax_policy():
    manifest, pgp, raw = _release(LATEST)
    relaxed = manifest.replace(
        b'"manifestSignatureEnforcement": "flag"',
        b'"manifestSignatureEnforcement": "off"',
    )
    with pytest.raises(SignatureError):
        verify_manifest_signatures(relaxed, pgp, raw)
    document = json.loads(manifest)
    document["manifestSignatureEnforcement"] = "off"
    document["platforms"]["linux-x64"]["checksum"] = "not-hex"
    with pytest.raises(ManifestError, match="checksum"):
        parse_verified_manifest(json.dumps(document).encode(), LATEST, SCHEMES)


@pytest.mark.parametrize(
    "call",
    [
        lambda m, pgp, raw: verify_pgp_detached(m.decode("utf-8"), pgp),
        lambda m, pgp, raw: verify_pgp_detached(m, pgp.decode("ascii")),
        lambda m, pgp, raw: verify_raw_signature(m, raw.decode("ascii")),
        lambda m, pgp, raw: verify_raw_signature(b"", raw),
        lambda m, pgp, raw: verify_pgp_detached(m, b""),
        lambda m, pgp, raw: verify_pgp_detached(m, b"\n" * (MAX_SIGNATURE_BYTES + 1)),
        lambda m, pgp, raw: verify_raw_signature(m, b" " * (MAX_SIGNATURE_BYTES + 1)),
    ],
    ids=[
        "text-data",
        "text-pgp",
        "text-raw",
        "empty-data",
        "empty-pgp",
        "huge-pgp",
        "huge-raw",
    ],
)
def test_inputs_must_be_bounded_bytes(call):
    manifest, pgp, raw = _release(LATEST)
    with pytest.raises(SignatureError):
        call(manifest, pgp, raw)


def test_verification_survives_python_optimize_flags(tmp_path):
    script = textwrap.dedent("""
        import sys
        from pathlib import Path

        sys.path.insert(0, sys.argv[1])
        from claude_wrangler import signing

        if __debug__:
            raise SystemExit("assert statements are unexpectedly enabled")
        release = Path(sys.argv[2])
        manifest = (release / "manifest.json").read_bytes()
        pgp = (release / "manifest.json.sig").read_bytes()
        raw = (release / "manifest.json.raw-sig.json").read_bytes()
        signing.verify_manifest_signatures(manifest, pgp, raw)
        tampered = manifest.replace(b'"size": 2', b'"size": 3', 1)
        checks = (
            lambda: signing.verify_pgp_detached(tampered, pgp),
            lambda: signing.verify_raw_signature(tampered, raw),
            lambda: signing.verify_manifest_signatures(tampered, pgp, raw),
        )
        for check in checks:
            try:
                check()
            except signing.SignatureError:
                continue
            raise SystemExit("tampered manifest accepted under -O")
        print("tampered-rejected")
        """)
    for flags in (["-O"], ["-OO"]):
        completed = subprocess.run(
            [
                sys.executable,
                *flags,
                "-c",
                script,
                str(REPO_ROOT / "src"),
                str(FIXTURES / LATEST),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "tampered-rejected"


# --- OpenPGP encodings -------------------------------------------------------


@pytest.mark.parametrize("version", REAL_VERSIONS)
def test_armored_and_binary_signatures_both_verify(version):
    manifest = _fixture(version, "manifest.json")
    armored = _fixture(version, "manifest.json.sig")
    binary = _binary_sig(version)
    assert binary[0] == 0x89
    verify_pgp_detached(manifest, armored)
    verify_pgp_detached(manifest, binary)
    # Re-armoring proves _crc24 agrees with the CRC the real signer wrote.
    assert _armor(binary) == armored


def _new_format(body: bytes, five_octet: bool = False) -> bytes:
    """Wrap a signature packet body in a new-format header."""

    if five_octet:
        return b"\xc2\xff" + len(body).to_bytes(4, "big") + body
    length = len(body) - 192
    return bytes((0xC2, (length >> 8) + 192, length & 0xFF)) + body


@pytest.mark.parametrize(
    "encode",
    [
        lambda body: _new_format(body),
        lambda body: _new_format(body, five_octet=True),
        lambda body: b"\x8a" + len(body).to_bytes(4, "big") + body,
    ],
    ids=["new-two-octet", "new-five-octet", "old-four-octet"],
)
def test_alternate_packet_headers_verify(encode):
    binary = _binary_sig()
    verify_pgp_detached(_fixture(LATEST, "manifest.json"), encode(binary[3:]))


@pytest.mark.parametrize(
    "armor",
    [
        lambda packet: _armor(packet, newline="\r\n"),
        lambda packet: _armor(packet, headers=("Comment: release", "Version: x")),
        lambda packet: _armor(packet, checksum=False),
        lambda packet: b"\n\n" + _armor(packet) + b"\n\n",
    ],
    ids=["crlf", "headers", "no-crc", "blank-lines"],
)
def test_armor_variants_accepted(armor):
    verify_pgp_detached(_fixture(LATEST, "manifest.json"), armor(_binary_sig()))


@pytest.mark.parametrize(
    "corrupt, message",
    [
        (lambda a: a.replace(a.split(b"\n")[-3], b"=AAAA"), "CRC-24"),
        (lambda a: a.replace(a.split(b"\n")[-3], b"=AAA"), "checksum line"),
        (
            lambda a: a.replace(b"Signature\n\n", b"Signature\n", 1),
            "malformed header",
        ),
        (lambda a: a + b"trailing\n", "trailing content"),
        (lambda a: a.replace(b"SIGNATURE", b"MESSAGE"), "not a PGP SIGNATURE"),
        (
            lambda a: a.replace(a.split(b"\n")[3], b"!" + a.split(b"\n")[3][1:]),
            "not base64",
        ),
        (lambda a: a.replace(b"-----END PGP SIGNATURE-----\n", b""), "ends inside"),
        (
            lambda a: _armor(_binary_sig(), checksum=False).replace(
                b"-----END PGP SIGNATURE-----\n", b""
            ),
            "ends inside",
        ),
        (lambda a: a.replace(b"Signature", b"Signatur\xe9"), "not ASCII"),
        (lambda a: b"junk\n" + a, "packet header"),
        (
            lambda a: a.replace(a.split(b"\n")[3], a.split(b"\n")[3][:-2] + b"=="),
            "base64",
        ),
    ],
    ids=[
        "bad-crc",
        "short-crc",
        "no-blank-line",
        "trailing",
        "wrong-label",
        "bad-char",
        "no-end",
        "no-end-no-crc",
        "non-ascii",
        "leading-junk",
        "inner-padding",
    ],
)
def test_armor_corruption_rejected(corrupt, message):
    armored = _armor(_binary_sig(), headers=("Comment: Signature",))
    corrupted = corrupt(armored)
    assert corrupted != armored
    with pytest.raises(SignatureError, match=message):
        verify_pgp_detached(_fixture(LATEST, "manifest.json"), corrupted)


def _fingerprint_subpacket(fingerprint: bytes) -> bytes:
    """Return a hashed issuer-fingerprint subpacket body."""

    return b"\x04" + fingerprint


PINNED_FP_BYTES = bytes.fromhex(PINNED_PGP_FINGERPRINT)
OTHER_FP_BYTES = PINNED_FP_BYTES[:-1] + bytes((PINNED_FP_BYTES[-1] ^ 0x01,))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: {**p, "version": 3}, "only v4"),
        (lambda p: {**p, "version": 5}, "only v4"),
        (lambda p: {**p, "sig_type": 0x01}, "binary-document"),
        (lambda p: {**p, "sig_type": 0x13}, "binary-document"),
        (lambda p: {**p, "pk": 17}, "only RSA"),
        (lambda p: {**p, "pk": 3}, "only RSA"),
        (lambda p: {**p, "hash": 8}, "only SHA-512"),
        (lambda p: {**p, "hash": 2}, "only SHA-512"),
        (
            lambda p: {
                **p,
                "hashed": _replace_subpacket(
                    p["hashed"], 33, _fingerprint_subpacket(OTHER_FP_BYTES)
                ),
            },
            "issuer fingerprint",
        ),
        (
            lambda p: {
                **p,
                "hashed": _replace_subpacket(
                    p["hashed"], 33, b"\x05" + PINNED_FP_BYTES
                ),
            },
            "not a v4 fingerprint",
        ),
        (
            lambda p: {**p, "unhashed": _subpacket(16, OTHER_FP_BYTES[-8:])},
            "key ID",
        ),
        (
            lambda p: {**p, "hashed": _drop_subpacket(p["hashed"], 33)},
            "exactly one hashed issuer-fingerprint",
        ),
        (
            lambda p: {
                **p,
                "hashed": _drop_subpacket(p["hashed"], 33),
                "unhashed": p["unhashed"]
                + _subpacket(33, _fingerprint_subpacket(PINNED_FP_BYTES)),
            },
            "exactly one hashed issuer-fingerprint",
        ),
        (
            lambda p: {
                **p,
                "hashed": p["hashed"]
                + _subpacket(33, _fingerprint_subpacket(PINNED_FP_BYTES)),
            },
            "exactly one hashed issuer-fingerprint",
        ),
        (
            lambda p: {**p, "hashed": p["hashed"] + _subpacket(50, b"x", True)},
            "critical hashed subpacket type 50",
        ),
        (
            lambda p: {**p, "unhashed": p["unhashed"] + _subpacket(50, b"x", True)},
            "critical unhashed subpacket type 50",
        ),
        (
            lambda p: {**p, "hashed": _drop_subpacket(p["hashed"], 2)},
            "creation-time",
        ),
        (
            lambda p: {
                **p,
                "hashed": _replace_subpacket(
                    p["hashed"], 2, (PINNED_PGP_KEY_CREATED - 1).to_bytes(4, "big")
                ),
            },
            "predates",
        ),
        (lambda p: {**p, "hashed": p["hashed"] + b"\x00"}, "zero length"),
        (lambda p: {**p, "hashed": p["hashed"] + b"\x09\x02"}, "truncated"),
        (
            lambda p: {**p, "prefix": bytes((p["prefix"][0] ^ 0x01, p["prefix"][1]))},
            "hash prefix",
        ),
        (
            lambda p: {**p, "mpi": p["mpi"][:-1] + bytes((p["mpi"][-1] ^ 0x01,))},
            "does not verify",
        ),
        (
            lambda p: {
                **p,
                "bits": _mpi(PINNED_RSA_MODULUS)[0],
                "mpi": _mpi(PINNED_RSA_MODULUS)[1],
            },
            "not less than the modulus",
        ),
        (
            lambda p: {**p, "bits": 4096, "mpi": b"\xff" * 512},
            "not less than the modulus",
        ),
        (
            lambda p: {**p, "bits": 4097, "mpi": b"\x01" + b"\x00" * 512},
            "at most 512",
        ),
        (
            lambda p: {**p, "bits": p["bits"] + 8, "mpi": b"\x00" + p["mpi"]},
            "minimally encoded",
        ),
        (
            lambda p: {
                **p,
                "bits": _mpi(int.from_bytes(p["mpi"][:-1], "big"))[0],
                "mpi": _mpi(int.from_bytes(p["mpi"][:-1], "big"))[1],
            },
            "does not verify",
        ),
        (lambda p: {**p, "mpi": p["mpi"] + b"\x00"}, "after its single MPI"),
    ],
    ids=[
        "v3",
        "v5",
        "text-sig",
        "certification",
        "dsa",
        "rsa-sign-only",
        "sha256",
        "sha1",
        "wrong-fingerprint",
        "fingerprint-v5",
        "wrong-key-id",
        "key-id-only",
        "fingerprint-unhashed-only",
        "duplicate-fingerprint",
        "critical-hashed",
        "critical-unhashed",
        "no-creation-time",
        "predates-key",
        "zero-length-subpacket",
        "truncated-subpacket",
        "hash-prefix",
        "tampered-value",
        "value-equals-modulus",
        "value-all-ones",
        "value-too-long",
        "non-minimal-mpi",
        "truncated-mpi",
        "data-after-mpi",
    ],
)
def test_pgp_field_violations_rejected(mutate, message):
    with pytest.raises(SignatureError, match=message):
        verify_pgp_detached(_fixture(LATEST, "manifest.json"), _mutated(mutate))


@pytest.mark.parametrize(
    "rewrite, message",
    [
        (lambda b: b[:-1], "truncated"),
        (lambda b: b + b"\x00", "after the signature packet"),
        (lambda b: b + b, "after the signature packet"),
        (lambda b: b"\x8d" + b[1:], "tag 3"),
        (lambda b: b"\x8b" + b[3:], "indeterminate"),
        (lambda b: b"\xc2\xe5" + b[3:], "partial length"),
        (lambda b: b"\x00" + b[1:], "packet header"),
        (lambda b: b[:2], "truncated"),
    ],
    ids=[
        "truncated",
        "trailing-byte",
        "two-packets",
        "wrong-tag",
        "indeterminate",
        "partial",
        "not-a-packet",
        "header-only",
    ],
)
def test_pgp_packet_framing_violations_rejected(rewrite, message):
    with pytest.raises(SignatureError, match=message):
        verify_pgp_detached(_fixture(LATEST, "manifest.json"), rewrite(_binary_sig()))


# --- Policy checks with a key that can sign anything -------------------------

DATA = b'{"version": "9.9.9"}\n'


def test_throwaway_key_positive_controls(throwaway_key):
    anchor = throwaway_key.anchor
    signing._check_trust_anchor(anchor)
    signature = _pgp_sign(throwaway_key, DATA)
    signing._verify_pgp_detached_with(anchor, DATA, signature)
    signing._verify_pgp_detached_with(anchor, DATA, _armor(signature))
    digest = hashlib.sha512(RAW_SIGNATURE_PREFIX + DATA).digest()
    raw = throwaway_key.sign_block(_pkcs1_block(digest, throwaway_key.k))
    sidecar = _sidecar(raw, publicKeySha256=anchor.spki_sha256)
    signing._verify_raw_signature_with(anchor, DATA, sidecar)
    # Only the pinned key is trusted by the public API.
    with pytest.raises(SignatureError, match="issuer fingerprint"):
        verify_pgp_detached(DATA, signature)
    with pytest.raises(SignatureError, match="not the pinned key"):
        verify_raw_signature(DATA, sidecar)


def test_other_key_claiming_pinned_identity_fails_rsa(throwaway_key):
    hashed = _default_hashed(PINNED_PGP_FINGERPRINT, PINNED_PGP_KEY_CREATED + 1)
    unhashed = _subpacket(16, PINNED_FP_BYTES[-8:])
    forged = _pgp_sign(throwaway_key, DATA, hashed=hashed, unhashed=unhashed)
    with pytest.raises(SignatureError, match="OpenPGP signature does not verify"):
        verify_pgp_detached(DATA, forged)
    digest = hashlib.sha512(RAW_SIGNATURE_PREFIX + DATA).digest()
    raw = throwaway_key.sign_block(_pkcs1_block(digest, throwaway_key.k))
    with pytest.raises(SignatureError, match="256 octets; the pinned key requires 512"):
        verify_raw_signature(DATA, _sidecar(raw))
    with pytest.raises(SignatureError, match="Raw RSA signature does not verify"):
        verify_raw_signature(DATA, _sidecar(raw.rjust(512, b"\x00")))


@pytest.mark.parametrize(
    "options, message",
    [
        (
            {
                "hash_algorithm": 8,
                "hash_factory": hashlib.sha256,
                "digest_info": SHA256_DIGESTINFO,
            },
            "only SHA-512",
        ),
        ({"sig_type": 0x01}, "binary-document"),
        ({"hashed_kind": "key-id-only"}, "exactly one hashed issuer-fingerprint"),
        (
            {"hashed_kind": "fingerprint-unhashed"},
            "exactly one hashed issuer-fingerprint",
        ),
        ({"hashed_kind": "critical-unknown"}, "critical hashed subpacket"),
        ({"hashed_kind": "early"}, "predates"),
        ({"hashed_kind": "no-time"}, "creation-time"),
        ({"digest_info": SHA512_DIGESTINFO_NO_NULL}, "does not verify"),
    ],
    ids=[
        "sha256",
        "text-mode",
        "key-id-only",
        "fingerprint-unhashed",
        "critical-unknown",
        "predates-key",
        "no-creation-time",
        "digestinfo-without-null",
    ],
)
def test_valid_signatures_with_disallowed_parameters_rejected(
    throwaway_key, options, message
):
    options = dict(options)
    kind = options.pop("hashed_kind", None)
    fingerprint = bytes.fromhex(throwaway_key.fingerprint)
    created = (TEST_KEY_CREATED + 1).to_bytes(4, "big")
    if kind == "key-id-only":
        options["hashed"] = _subpacket(2, created) + _subpacket(16, fingerprint[-8:])
    elif kind == "fingerprint-unhashed":
        options["hashed"] = _subpacket(2, created)
        options["unhashed"] = _subpacket(33, b"\x04" + fingerprint)
    elif kind == "critical-unknown":
        options["hashed"] = _default_hashed(
            throwaway_key.fingerprint, TEST_KEY_CREATED + 1
        ) + _subpacket(20, b"\x80\x00\x00\x00\x00\x01\x00\x01nv", critical=True)
    elif kind == "early":
        options["hashed"] = _default_hashed(
            throwaway_key.fingerprint, TEST_KEY_CREATED - 1
        )
    elif kind == "no-time":
        options["hashed"] = _subpacket(33, b"\x04" + fingerprint)
    signature = _pgp_sign(throwaway_key, DATA, **options)
    with pytest.raises(SignatureError, match=message):
        signing._verify_pgp_detached_with(throwaway_key.anchor, DATA, signature)


def test_unknown_non_critical_subpackets_are_ignored(throwaway_key):
    hashed = _default_hashed(throwaway_key.fingerprint, TEST_KEY_CREATED + 1)
    hashed += _subpacket(20, b"\x80\x00\x00\x00\x00\x01\x00\x01nv")
    unhashed = _subpacket(16, bytes.fromhex(throwaway_key.fingerprint)[-8:])
    unhashed += _subpacket(50, b"opaque")
    signature = _pgp_sign(throwaway_key, DATA, hashed=hashed, unhashed=unhashed)
    signing._verify_pgp_detached_with(throwaway_key.anchor, DATA, signature)


def test_short_openpgp_mpi_is_left_padded(throwaway_key):
    # About one signature in 200 has a leading zero octet, which a minimal
    # MPI drops; it must still verify once restored to the modulus length.
    for counter in range(5000):
        data = DATA + str(counter).encode("ascii")
        signature = _pgp_sign(throwaway_key, data)
        if _split_packet(signature)["bits"] <= 8 * (throwaway_key.k - 1):
            break
    else:
        pytest.fail("no short signature found")
    assert len(_split_packet(signature)["mpi"]) < throwaway_key.k
    signing._verify_pgp_detached_with(throwaway_key.anchor, data, signature)


def _malformed_blocks(digest: bytes, k: int) -> Dict[str, bytes]:
    """Return PKCS #1 blocks that a lenient parser might accept."""

    t = SHA512_DIGESTINFO + digest
    good = _pkcs1_block(digest, k)
    return {
        "block-type-2": b"\x00\x02" + good[2:],
        "short-padding-trailing-garbage": (
            b"\x00\x01" + b"\xff" * (k - len(t) - 11) + b"\x00" + t + b"\x00" * 8
        ),
        "padding-not-ff": good[:5] + b"\xfe" + good[6:],
        "no-separator": b"\x00\x01" + b"\xff" * (k - len(t) - 2) + t,
        "leading-nonzero": b"\x01" + good[1:],
        "sha256-digestinfo": _pkcs1_block(
            hashlib.sha256(RAW_SIGNATURE_PREFIX + DATA).digest(), k, SHA256_DIGESTINFO
        ),
        "digestinfo-without-null": _pkcs1_block(digest, k, SHA512_DIGESTINFO_NO_NULL),
    }


@pytest.mark.parametrize(
    "variant",
    [
        "block-type-2",
        "short-padding-trailing-garbage",
        "padding-not-ff",
        "no-separator",
        "leading-nonzero",
        "sha256-digestinfo",
        "digestinfo-without-null",
    ],
)
def test_malformed_pkcs1_blocks_rejected(throwaway_key, variant):
    digest = hashlib.sha512(RAW_SIGNATURE_PREFIX + DATA).digest()
    block = _malformed_blocks(digest, throwaway_key.k)[variant]
    assert int.from_bytes(block, "big") < throwaway_key.n
    sidecar = _sidecar(
        throwaway_key.sign_block(block),
        publicKeySha256=throwaway_key.anchor.spki_sha256,
    )
    with pytest.raises(SignatureError, match="does not verify"):
        signing._verify_raw_signature_with(throwaway_key.anchor, DATA, sidecar)


# --- Raw signature sidecar ---------------------------------------------------


def _real_sidecar_with(**overrides: Any) -> bytes:
    """Return the latest real sidecar with some members replaced."""

    payload = json.loads(_fixture(LATEST, "manifest.json.raw-sig.json"))
    for key, value in overrides.items():
        if value is _MISSING:
            payload.pop(key)
        else:
            payload[key] = value
    return json.dumps(payload).encode("utf-8")


_MISSING = object()
N_BYTES = PINNED_RSA_MODULUS.to_bytes(512, "big")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"schema": 2}, "schema"),
        ({"schema": "1"}, "schema"),
        ({"schema": True}, "schema"),
        ({"schema": 1.0}, "schema"),
        ({"schema": _MISSING}, "schema"),
        ({"algorithm": "RSASSA-PSS-SHA512"}, "algorithm"),
        ({"algorithm": "RSASSA-PKCS1-v1_5-SHA256"}, "algorithm"),
        ({"algorithm": "rsassa-pkcs1-v1_5-sha512"}, "algorithm"),
        ({"algorithm": _MISSING}, "algorithm"),
        ({"publicKeySha256": "00" * 32}, "not the pinned key"),
        ({"publicKeySha256": PINNED_SPKI_SHA256[:-1] + "7"}, "not the pinned key"),
        ({"publicKeySha256": PINNED_SPKI_SHA256[:-2]}, "not a SHA-256"),
        ({"publicKeySha256": PINNED_SPKI_SHA256 + "\n"}, "not a SHA-256"),
        ({"publicKeySha256": _MISSING}, "not a SHA-256"),
        ({"signature": _MISSING}, "no signature string"),
        ({"signature": 7}, "no signature string"),
        ({"signature": "!!!!"}, "not valid base64"),
        ({"signature": "QUJD\nREVG"}, "not valid base64"),
        ({"signature": "QUI="[:-1]}, "base64"),
        ({"signature": "QUJ="}, "not canonical"),
        (
            {"signature": base64.b64encode(_raw_sidecar_value()[1:]).decode()},
            "511 octets",
        ),
        (
            {"signature": base64.b64encode(b"\x00" + _raw_sidecar_value()).decode()},
            "513 octets",
        ),
        (
            {"signature": base64.b64encode(N_BYTES).decode()},
            "not less than the modulus",
        ),
        (
            {"signature": base64.b64encode(b"\xff" * 512).decode()},
            "not less than the modulus",
        ),
        (
            {"signature": base64.b64encode(_flip(_raw_sidecar_value(), 100)).decode()},
            "does not verify",
        ),
    ],
    ids=[
        "schema-2",
        "schema-string",
        "schema-bool",
        "schema-float",
        "schema-missing",
        "pss",
        "sha256",
        "lowercase-algorithm",
        "algorithm-missing",
        "zero-key-hash",
        "other-key-hash",
        "short-key-hash",
        "key-hash-newline",
        "key-hash-missing",
        "signature-missing",
        "signature-number",
        "signature-garbage",
        "signature-newline",
        "signature-bad-padding",
        "signature-noncanonical",
        "signature-511",
        "signature-513",
        "signature-equals-modulus",
        "signature-all-ones",
        "signature-tampered",
    ],
)
def test_raw_sidecar_violations_rejected(overrides, message):
    sidecar = _real_sidecar_with(**overrides)
    with pytest.raises(SignatureError, match=message):
        verify_raw_signature(_fixture(LATEST, "manifest.json"), sidecar)


@pytest.mark.parametrize(
    "sidecar, message",
    [
        (b"not json", "not valid JSON"),
        (b"[1, 2]", "JSON object"),
        (b'"text"', "JSON object"),
        (b'{"schema": 1, "schema": 1}', "duplicate"),
        (b'{"schema": NaN}', "non-standard"),
        (b'{"schema": 1, "x": "\xff"}', "not valid JSON"),
    ],
    ids=["garbage", "array", "string", "duplicate", "nan", "bad-utf8"],
)
def test_raw_sidecar_malformed_json_rejected(sidecar, message):
    with pytest.raises(SignatureError, match=message):
        verify_raw_signature(_fixture(LATEST, "manifest.json"), sidecar)


def test_raw_sidecar_tolerates_unknown_members_and_uppercase_key_hash():
    manifest = _fixture(LATEST, "manifest.json")
    verify_raw_signature(manifest, _real_sidecar_with(comment="extra"))
    verify_raw_signature(
        manifest, _real_sidecar_with(publicKeySha256=PINNED_SPKI_SHA256.upper())
    )


# --- Manifest content checks -------------------------------------------------


def _manifest_with(edit: Callable[[Dict[str, Any]], None]) -> bytes:
    """Return the latest manifest re-serialized after `edit`."""

    document = json.loads(_fixture(LATEST, "manifest.json"))
    edit(document)
    return json.dumps(document, indent=2).encode("utf-8")


def _set_entry(
    platform: str, field: str, value: Any
) -> Callable[[Dict[str, Any]], None]:
    """Return an edit that sets or removes one platform field."""

    def edit(document: Dict[str, Any]) -> None:
        """Apply the edit."""

        if value is _MISSING:
            document["platforms"][platform].pop(field)
        else:
            document["platforms"][platform][field] = value

    return edit


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d.update(version="2.1.287"), "does not match requested version"),
        (lambda d: d.update(version=None), "does not match requested version"),
        (lambda d: d.pop("version"), "does not match requested version"),
        (lambda d: d.update(version="2.1.296 "), "does not match requested version"),
        (_set_entry("linux-x64", "checksum", "z" * 64), "64 hex digits"),
        (_set_entry("linux-x64", "checksum", "a" * 63), "64 hex digits"),
        (_set_entry("linux-x64", "checksum", "a" * 65), "64 hex digits"),
        (_set_entry("linux-x64", "checksum", 12), "64 hex digits"),
        (_set_entry("linux-x64", "checksum", _MISSING), "64 hex digits"),
        (_set_entry("win32-x64", "size", "260489376"), "positive integer"),
        (_set_entry("win32-x64", "size", 0), "positive integer"),
        (_set_entry("win32-x64", "size", -5), "positive integer"),
        (_set_entry("win32-x64", "size", True), "positive integer"),
        (_set_entry("win32-x64", "size", 1.5), "positive integer"),
        (_set_entry("win32-x64", "size", _MISSING), "positive integer"),
        (_set_entry("darwin-x64", "binary", "../claude"), "plain file name"),
        (_set_entry("darwin-x64", "binary", "bin/claude"), "plain file name"),
        (_set_entry("darwin-x64", "binary", "bin\\claude"), "plain file name"),
        (_set_entry("darwin-x64", "binary", ".."), "plain file name"),
        (_set_entry("darwin-x64", "binary", ""), "plain file name"),
        (_set_entry("darwin-x64", "binary", _MISSING), "plain file name"),
        (lambda d: d["platforms"].update({"Win32-X64": {}}), "platform key"),
        (lambda d: d["platforms"].update({"linux/x64": {}}), "platform key"),
        (lambda d: d["platforms"].update({"linux-x64": []}), "JSON object"),
        (lambda d: d.update(platforms=[]), "non-empty JSON object"),
        (lambda d: d.update(platforms={}), "non-empty JSON object"),
        (lambda d: d.pop("platforms"), "non-empty JSON object"),
        (lambda d: d.update(buildDate=20261009), "buildDate"),
        (lambda d: d.update(commit=["abc"]), "commit"),
    ],
)
def test_manifest_content_violations_rejected(edit, message):
    with pytest.raises(ManifestError, match=message):
        parse_verified_manifest(_manifest_with(edit), LATEST, SCHEMES)


@pytest.mark.parametrize(
    "manifest, message",
    [
        (b"[]", "JSON object"),
        (b"\xef\xbb\xbf{}", "not valid JSON"),
        (b'{"version": "\xff"}', "not valid JSON"),
        (b'{"version": "2.1.296", "version": "2.1.296"}', "duplicate"),
        (
            b'{"version": "2.1.296", "platforms": {"x": {"size": Infinity}}}',
            "non-standard",
        ),
        (b"", "empty"),
    ],
    ids=["array", "bom", "bad-utf8", "duplicate", "infinity", "empty"],
)
def test_manifest_malformed_json_rejected(manifest, message):
    with pytest.raises(ManifestError, match=message):
        parse_verified_manifest(manifest, LATEST, SCHEMES)


def test_manifest_optional_fields_and_normalization():
    def edit(document: Dict[str, Any]) -> None:
        """Drop optional members and uppercase one checksum."""

        document.pop("commit")
        document.pop("buildDate")
        entry = document["platforms"]["linux-x64"]
        entry["checksum"] = entry["checksum"].upper()
        entry["future"] = {"ignored": True}

    parsed = parse_verified_manifest(_manifest_with(edit), LATEST, SCHEMES)
    assert parsed.commit is None and parsed.build_date is None
    checksum = parsed.artifacts["linux-x64"].checksum
    assert checksum == checksum.lower() and len(checksum) == 64


@pytest.mark.parametrize(
    "schemes",
    [
        ("openpgp",),
        ("raw-rsa",),
        (),
        ("openpgp", "raw-rsa", "none"),
        ("openpgp", "openpgp", "raw-rsa"),
        "openpgp,raw-rsa",
    ],
)
def test_parse_requires_both_verified_schemes(schemes):
    with pytest.raises(ManifestError, match="schemes"):
        parse_verified_manifest(_fixture(LATEST, "manifest.json"), LATEST, schemes)


def test_parse_normalizes_scheme_order():
    parsed = parse_verified_manifest(
        _fixture(LATEST, "manifest.json"), LATEST, ("raw-rsa", "openpgp")
    )
    assert parsed.schemes == SCHEMES


def test_parse_rejects_bad_expected_version():
    for expected in ("", None):
        with pytest.raises(ManifestError, match="Expected manifest version"):
            parse_verified_manifest(
                _fixture(LATEST, "manifest.json"), expected, SCHEMES
            )


def test_signature_plus_modulus_is_rejected():
    # s + n has the same RSA result as s, so only the explicit s < n check
    # stops this malleability; exercise it wherever s + n still fits.
    exercised = {"openpgp": 0, "raw": 0}
    for version in REAL_VERSIONS:
        manifest, pgp, raw = _release(version)
        raw_value = int.from_bytes(_raw_sidecar_value(version), "big")
        shifted = raw_value + PINNED_RSA_MODULUS
        if shifted.bit_length() <= 4096:
            payload = json.loads(raw)
            payload["signature"] = base64.b64encode(shifted.to_bytes(512, "big"))
            payload["signature"] = payload["signature"].decode("ascii")
            with pytest.raises(SignatureError, match="not less than the modulus"):
                verify_raw_signature(manifest, json.dumps(payload).encode())
            exercised["raw"] += 1

        parts = _split_packet(_simple_dearmor(pgp))
        shifted = int.from_bytes(parts["mpi"], "big") + PINNED_RSA_MODULUS
        if shifted.bit_length() <= 4096:
            parts["bits"], parts["mpi"] = _mpi(shifted)
            with pytest.raises(SignatureError, match="not less than the modulus"):
                verify_pgp_detached(manifest, _join_packet(parts))
            exercised["openpgp"] += 1
    assert exercised["raw"] and exercised["openpgp"], exercised
