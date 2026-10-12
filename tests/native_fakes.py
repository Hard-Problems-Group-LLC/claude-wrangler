"""Offline fakes for native-runtime operation tests.

The fakes replace only what must not run in tests: the network (a
`FakeReleaseSource` whose real `HttpsFetcher` reads from an in-memory
transport), signature verification of fake manifests (a stand-in that
checks a digest-based "signature"), the process table, and the clock. Slot
promotion, metadata, generated files, and the launcher core stay real.

Fake manifests are signed by `fake_signature(manifest_bytes)`; tampering
with retained evidence therefore still fails offline re-verification.
Recorded real releases (tests/fixtures/releases) cover the real signature
path elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from claude_wrangler import platforms
from claude_wrangler.config import config_from_args, parse_args
from claude_wrangler.distribution import (
    DEFAULT_RELEASE_BASE_URL,
    MANIFEST_NAME,
    PGP_SIGNATURE_NAME,
    RAW_SIGNATURE_NAME,
    DistributionError,
    ManifestBundle,
    check_version_floor,
    normalize_channel,
    release_hosts,
)
from claude_wrangler.download import DownloadError, HttpsFetcher, TransportResponse
from claude_wrangler.models import Config
from claude_wrangler.platforms import binary_filename
from claude_wrangler.services import Services
from claude_wrangler.signing import PlatformArtifact, SignatureError, VerifiedManifest
from claude_wrangler.slots import RunningProcess, ServiceStopResult

FIXED_NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
HELP_SUBCOMMANDS = ["doctor", "mcp", "plugin", "plugins", "update", "upgrade"]
CSC_PATH = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
FAKE_SOURCE = (
    Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "FakeClaude.cs"
)


def host_key() -> str:
    """Return this host's real platform key."""

    return platforms.host_platform_key()


def host_os() -> str:
    """Return this host's platform-key prefix."""

    return host_key().split("-", 1)[0]


def fake_binary(version: str, key: str, extra: bytes = b"") -> bytes:
    """Return deterministic fake binary bytes naming a version and platform."""

    return "FAKE-CLAUDE {} {}\n".format(version, key).encode("ascii") + extra


def fake_signature(manifest_bytes: bytes) -> bytes:
    """Return the stand-in detached signature of fake manifest bytes."""

    return b"fake-openpgp:" + hashlib.sha256(manifest_bytes).hexdigest().encode("ascii")


def fake_raw_signature(manifest_bytes: bytes) -> bytes:
    """Return the stand-in raw-signature sidecar of fake manifest bytes."""

    return json.dumps(
        {"fake-raw-rsa": hashlib.sha256(manifest_bytes).hexdigest()}
    ).encode("ascii")


@dataclass
class FakeRelease:
    """One fake release: a manifest, both stand-in signatures, and binaries."""

    version: str
    binaries: Dict[str, bytes]
    build_date: str = "2026-10-09T14:46:35Z"
    commit: str = "f" * 40

    @property
    def manifest_bytes(self) -> bytes:
        """Return the manifest JSON bytes."""

        payload = {
            "version": self.version,
            "commit": self.commit,
            "buildDate": self.build_date,
            "platforms": {
                key: {
                    "binary": binary_filename(key),
                    "checksum": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
                for key, data in sorted(self.binaries.items())
            },
        }
        return (json.dumps(payload, indent=2) + "\n").encode("utf-8")

    def evidence(self) -> Dict[str, bytes]:
        """Return the three retained-evidence files."""

        manifest = self.manifest_bytes
        return {
            MANIFEST_NAME: manifest,
            PGP_SIGNATURE_NAME: fake_signature(manifest),
            RAW_SIGNATURE_NAME: fake_raw_signature(manifest),
        }


def fake_verify(
    manifest_bytes: bytes,
    pgp_signature: bytes,
    raw_signature_json: bytes,
    expected_version: str,
) -> ManifestBundle:
    """Verify stand-in signatures, then parse a fake manifest."""

    if pgp_signature != fake_signature(manifest_bytes):
        raise SignatureError("fake OpenPGP signature does not match the manifest")
    if raw_signature_json != fake_raw_signature(manifest_bytes):
        raise SignatureError("fake raw signature does not match the manifest")
    payload = json.loads(manifest_bytes.decode("utf-8"))
    if payload.get("version") != expected_version:
        raise SignatureError(
            "manifest version {} is not {}".format(
                payload.get("version"), expected_version
            )
        )
    artifacts = {
        key: PlatformArtifact(key, entry["binary"], entry["checksum"], entry["size"])
        for key, entry in payload["platforms"].items()
    }
    verified = VerifiedManifest(
        version=payload["version"],
        build_date=payload.get("buildDate"),
        commit=payload.get("commit"),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifacts=artifacts,
        schemes=("openpgp", "raw-rsa"),
    )
    return ManifestBundle(manifest_bytes, pgp_signature, raw_signature_json, verified)


def fake_load_manifest_bundle(directory: Path, expected_version: str) -> ManifestBundle:
    """Offline re-verification of a slot's fake retained evidence."""

    base = Path(directory)
    try:
        manifest = (base / MANIFEST_NAME).read_bytes()
        pgp = (base / PGP_SIGNATURE_NAME).read_bytes()
        raw = (base / RAW_SIGNATURE_NAME).read_bytes()
    except OSError as exc:
        raise DistributionError("Retained evidence is missing: {}".format(exc)) from exc
    return fake_verify(manifest, pgp, raw, expected_version)


class FakeTransport:
    """In-memory transport serving fixed bodies by URL with HTTP 200."""

    def __init__(self, bodies: Dict[str, bytes]) -> None:
        """Serve ``bodies``; any other URL answers 404."""

        self.bodies = bodies
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


class FakeReleaseSource:
    """A release source over fake releases; binaries download for real."""

    def __init__(
        self,
        releases: Sequence[FakeRelease],
        channels: Mapping[str, str],
        base_url: str = DEFAULT_RELEASE_BASE_URL,
    ) -> None:
        """Index releases by version and serve their binaries in memory."""

        self.base_url = base_url.rstrip("/")
        self.releases = {release.version: release for release in releases}
        self.channels = dict(channels)
        self.manifest_requests: List[str] = []
        self.pointer_requests: List[str] = []
        self.unavailable_channels: Dict[str, str] = {}
        bodies = {}
        for release in releases:
            for key, data in release.binaries.items():
                bodies[self.artifact_url(release.version, key)] = data
        self.transport = FakeTransport(bodies)
        self.fetcher = HttpsFetcher(
            release_hosts(self.base_url),
            retries=0,
            transport=self.transport,
            sleep=lambda seconds: None,
        )

    def channel_version(self, channel: str) -> str:
        """Return the version a fake channel pointer names."""

        name, _deprecated = normalize_channel(channel)
        self.pointer_requests.append(name)
        if name in self.unavailable_channels:
            raise DownloadError(self.unavailable_channels[name])
        if name not in self.channels:
            raise DownloadError("HTTP 404 for channel {}".format(name))
        return self.channels[name]

    def fetch_manifest_bundle(self, version: str) -> ManifestBundle:
        """Return the verified fake bundle for a version."""

        check_version_floor(version)
        self.manifest_requests.append(version)
        release = self.releases.get(version)
        if release is None:
            raise DownloadError("HTTP 404 for {}/{}".format(version, MANIFEST_NAME))
        evidence = release.evidence()
        return fake_verify(
            evidence[MANIFEST_NAME],
            evidence[PGP_SIGNATURE_NAME],
            evidence[RAW_SIGNATURE_NAME],
            version,
        )

    def artifact_url(self, version: str, platform_key: str) -> str:
        """Return the binary URL for a version and platform."""

        return "{}/{}/{}/{}".format(
            self.base_url, version, platform_key, binary_filename(platform_key)
        )

    @property
    def downloads(self) -> List[str]:
        """Return binary URLs the transport served."""

        return list(self.transport.calls)


@dataclass
class FakeProcessTable:
    """A fake process table keyed by image path."""

    processes: List[RunningProcess] = field(default_factory=list)
    fail: bool = False
    queries: List[Path] = field(default_factory=list)

    def find(self, root: Path) -> List[RunningProcess]:
        """Return processes whose image lies inside ``root``."""

        from claude_wrangler.slots import ProcessQueryError, processes_under

        self.queries.append(Path(root))
        if self.fail:
            raise ProcessQueryError("process table unavailable (test)")
        return processes_under(root, self.processes)

    def run_from(self, image: Path, pid: int = 4242) -> None:
        """Pretend a process runs ``image``."""

        self.processes.append(RunningProcess(pid, str(image)))


@dataclass
class Recorder:
    """Records calls to fake services."""

    smoke: List[Tuple[str, str, Dict[str, str]]] = field(default_factory=list)
    structure: List[Tuple[str, str]] = field(default_factory=list)
    stops: List[Tuple[str, Dict[str, str]]] = field(default_factory=list)
    sources: List[Tuple[str, float, Optional[str]]] = field(default_factory=list)


def fake_smoke_runner(recorder: Recorder):
    """Return a smoke runner that checks the fake binary names the version."""

    def run(binary: Path, version: str, env: Mapping[str, str], cwd: Path) -> None:
        """Pretend to run --version: the fake binary bytes carry the version."""

        from claude_wrangler.services import SmokeTestError

        recorder.smoke.append((str(binary), version, dict(env)))
        data = Path(binary).read_bytes()
        if not data.startswith(fake_binary(version, "")[:-2]):
            raise SmokeTestError(
                "Smoke test failed: {} is not Claude Code {}".format(binary, version)
            )

    return run


def make_services(
    source: FakeReleaseSource,
    *,
    recorder: Optional[Recorder] = None,
    processes: Optional[FakeProcessTable] = None,
    key: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    real_runner: bool = False,
    real_structure: bool = False,
    tracked_files=None,
) -> Services:
    """Return offline services over a fake release source.

    ``tracked_files`` replaces the git tracked-file query; by default no
    file is tracked (most test projects only carry an empty `.git` marker).
    Pass ``services.list_tracked_files`` to use real git.
    """

    from claude_wrangler.services import (
        probe_settings_rejections,
        read_help_subcommands,
        run_smoke_test,
    )

    recorder = recorder or Recorder()
    processes = processes or FakeProcessTable()
    platform_key = key or host_key()

    def release_source(base_url, timeout_seconds, ca_bundle):
        """Return the fake source, recording the requested settings."""

        recorder.sources.append((base_url, timeout_seconds, ca_bundle))
        return source

    def check_structure(path: Path, key_name: str) -> None:
        """Record structure checks; optionally run the real checker."""

        recorder.structure.append((str(path), key_name))
        if real_structure:
            platforms.check_executable_structure(path, key_name)

    def resolve(override, available):
        """Resolve the platform like the real resolver, without probing."""

        chosen = override or platform_key
        if available is not None and chosen not in available:
            raise platforms.PlatformError(
                "{} is not published in this release's manifest".format(chosen)
            )
        return chosen

    def stop_service(binary: Path, env: Mapping[str, str]) -> ServiceStopResult:
        """Record a background-service stop and clear the fake table."""

        recorder.stops.append((str(binary), dict(env)))
        processes.processes.clear()
        return ServiceStopResult((str(binary), "daemon", "stop", "--any"), 0, "")

    return Services(
        release_source=release_source,
        verify_evidence=fake_load_manifest_bundle,
        check_structure=check_structure,
        smoke_runner=run_smoke_test if real_runner else fake_smoke_runner(recorder),
        help_reader=(
            read_help_subcommands
            if real_runner
            else (lambda binary, env, cwd: list(HELP_SUBCOMMANDS))
        ),
        settings_probe=(
            probe_settings_rejections
            if real_runner
            else (lambda binary, env, cwd, subcommands: [])
        ),
        find_processes=processes.find,
        stop_service=stop_service,
        clock=lambda: FIXED_NOW,
        resolve_platform=resolve,
        host_os=platform_key.split("-", 1)[0],
        progress=lambda label: None,
        environ=dict(os.environ if environ is None else environ),
        tracked_files=(
            tracked_files
            if tracked_files is not None
            else (lambda project_root, paths: [])
        ),
    )


def make_config(project_root: Path, *args: str) -> Config:
    """Parse CLI arguments for a project root into a Config."""

    return config_from_args(parse_args(list(args) + [str(project_root)]))


def standard_releases(
    key: Optional[str] = None,
) -> Tuple[FakeRelease, FakeRelease, FakeRelease]:
    """Return fake releases at the floor, stable, and latest for one key."""

    key = key or host_key()
    return (
        FakeRelease("2.1.207", {key: fake_binary("2.1.207", key)}),
        FakeRelease("2.1.287", {key: fake_binary("2.1.287", key)}),
        FakeRelease("2.1.296", {key: fake_binary("2.1.296", key)}),
    )


def standard_source(key: Optional[str] = None) -> FakeReleaseSource:
    """Return a fake source: stable 2.1.287, latest 2.1.296."""

    return FakeReleaseSource(
        standard_releases(key), {"stable": "2.1.287", "latest": "2.1.296"}
    )


def compile_fake_claude(destination: Path, pad_to: int = 0) -> Path:
    """Compile tests/fixtures/fake_claude/FakeClaude.cs with csc.exe.

    The executable is padded with trailing zeros to ``pad_to`` bytes; a PE
    loader ignores such overlay data, so the padded file still runs and
    passes the real structural check's minimum-size rule.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = destination.with_name(destination.stem + "-raw.exe")
    completed = subprocess.run(
        [
            str(CSC_PATH),
            "-nologo",
            "-platform:x64",
            "-target:exe",
            "-out:{}".format(raw),
            str(FAKE_SOURCE),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "csc.exe failed: {}".format(completed.stdout.decode("utf-8", "replace"))
        )
    data = raw.read_bytes()
    if pad_to > len(data):
        data += b"\0" * (pad_to - len(data))
    destination.write_bytes(data)
    return destination


def posix_fake_claude() -> bytes:
    """Return a POSIX shell fake Claude binary using this interpreter.

    It mirrors FakeClaude.cs: `--version`, `--help`, the `--settings` probe,
    and otherwise an optional start marker and sleep, then a JSON report of
    its arguments, environment, and own path ("image").
    """

    script = """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "${FAKE_CLAUDE_VERSION:-2.1.296} (Claude Code)"
  exit 0
fi
if [ "$1" = "--help" ]; then
  printf 'Usage: claude [options] [command]\\n\\nCommands:\\n  doctor  Check\\n  mcp  Configure\\n  plugin|plugins  Manage\\n  update|upgrade  Check for updates\\n'
  exit 0
fi
for claude_last; do :; done
if [ "$1" = "--settings" ] && [ "$#" -ge 4 ] && [ "$claude_last" = "--help" ]; then
  case ",${FAKE_CLAUDE_REJECT_SETTINGS:-}," in
    *",$3,"*) echo "error: unknown option '--settings'" >&2; exit 1 ;;
  esac
  echo "Usage: claude $3 [options]"
  exit 0
fi
if [ -n "${FAKE_CLAUDE_STARTED:-}" ]; then
  : > "$FAKE_CLAUDE_STARTED"
fi
FAKE_CLAUDE_SELF=$0 "%s" -c 'import json, os, sys, time
image = os.environ.pop("FAKE_CLAUDE_SELF")
time.sleep(int(os.environ.get("FAKE_CLAUDE_SLEEP_MS") or 0) / 1000.0)
data = {"argv": sys.argv[1:], "env": dict(os.environ), "image": image}
path = os.environ.get("FAKE_CLAUDE_REPORT")
if path:
    open(path, "w", encoding="utf-8").write(json.dumps(data))
print(json.dumps(data, ensure_ascii=True))' "$@"
exit "${FAKE_CLAUDE_EXIT:-0}"
""" % sys.executable
    return script.encode("utf-8")


def git_bash() -> Optional[Path]:
    """Return Git for Windows' bash.exe when present (never WSL's bash)."""

    if os.name != "nt":
        return None
    candidates = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        candidates.extend([root / "bin" / "bash.exe", root.parent / "bin" / "bash.exe"])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def clean_launch_environment(
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Return os.environ minus claude-local and Claude session variables."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("CLAUDE_LOCAL_", "CLAUDECODE", "CLAUDE_CODE_"))
        and key.upper()
        not in ("CLAUDE_CONFIG_DIR", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL")
    }
    env.update(extra or {})
    return env


class UnreachableSource(FakeReleaseSource):
    """A release source that records contact and fails like a blocked network."""

    def __init__(self) -> None:
        """Serve nothing; every network call is recorded and refused."""

        super().__init__([], {})
        self.contacted: List[str] = []

    def channel_version(self, channel: str) -> str:
        """Refuse a channel lookup."""

        self.contacted.append("channel " + channel)
        raise DownloadError("network blocked by test")

    def fetch_manifest_bundle(self, version: str) -> ManifestBundle:
        """Refuse a manifest fetch."""

        self.contacted.append("manifest " + version)
        raise DownloadError("network blocked by test")


def plant_unverified_slot(
    runtime_root: Path, version: str, key: str, *, name: str = "a"
) -> Path:
    """Plant a slot a hostile repository could supply; return its binary.

    The record names a real release and the size of its binary, so the
    cheap record-and-size check passes, but the binary bytes, the recorded
    SHA-256, and the (absent) retained evidence are not the release's. The
    pointer names the slot.
    """

    genuine = fake_binary(version, key)
    slot = runtime_root / "slots" / name
    binary = slot / key / binary_filename(key)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"P" * len(genuine))
    record = {
        "schema": 1,
        "slot": name,
        "version": version,
        "channel": "stable",
        "platforms": {
            key: {
                "binary": "{}/{}".format(key, binary_filename(key)),
                "sha256": "ab" * 32,
                "size": len(genuine),
            }
        },
        "manifest_sha256": "cd" * 32,
        "build_date": None,
        "commit": None,
        "schemes": ["openpgp", "raw-rsa"],
        "subcommands": ["mcp"],
        "verified_at": "2026-10-01T00:00:00Z",
        "promoted_by": "hostile",
    }
    (slot / ".complete.json").write_text(json.dumps(record), encoding="utf-8")
    (runtime_root / "active-slot").write_bytes((name + "\n").encode("ascii"))
    return binary
