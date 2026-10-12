"""Opt-in: promote a copy of a real Claude Code binary end to end.

Set ``CLAUDE_WRANGLER_TEST_REAL_BINARY`` to an installed native ``claude``
(or ``claude.exe``) whose release is recorded under tests/fixtures/releases.
The test serves that file and the recorded signed manifest through an
in-memory transport (no network), then runs the real install transaction:
real signature verification, size and SHA-256 checks while "downloading",
the real structural check, and the real smoke test (`--version`) and help
read (`--help`) in transaction scratch. It finishes with a real offline
self-test. The binary is only ever run with `--version` and `--help`, with
every home-like variable redirected into scratch.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Optional

import pytest

from claude_wrangler.distribution import (
    DEFAULT_RELEASE_BASE_URL,
    EVIDENCE_NAMES,
    ReleaseSource,
    release_hosts,
)
from claude_wrangler.download import HttpsFetcher, TransportResponse
from claude_wrangler.inspection import selftest_operation
from claude_wrangler.operations import ensure_gitignore_block, install_like_operation
from claude_wrangler.platforms import binary_filename, host_platform_key
from claude_wrangler.services import Services
from claude_wrangler.slots import RuntimeStore, ServiceStopResult

from native_fakes import FakeProcessTable, make_config

REAL_BINARY = os.environ.get("CLAUDE_WRANGLER_TEST_REAL_BINARY")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "releases"

pytestmark = pytest.mark.skipif(
    not REAL_BINARY,
    reason="set CLAUDE_WRANGLER_TEST_REAL_BINARY to a real claude binary",
)


def sha256_of(path: Path) -> str:
    """Return a file's SHA-256 hex digest."""

    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matching_release(binary: Path, key: str) -> Optional[str]:
    """Return the recorded release whose manifest lists this exact binary."""

    digest = sha256_of(binary)
    for manifest in sorted(FIXTURES.glob("*/manifest.json")):
        payload = json.loads(manifest.read_bytes().decode("utf-8"))
        entry = payload.get("platforms", {}).get(key)
        if entry and entry.get("checksum", "").lower() == digest:
            return manifest.parent.name
    return None


class FileTransport:
    """Serves recorded metadata from memory and the binary from disk."""

    def __init__(self, bodies: Dict[str, bytes], binary_url: str, binary: Path) -> None:
        """Remember the small bodies and where the binary lives."""

        self.bodies = bodies
        self.binary_url = binary_url
        self.binary = binary

    def open(self, url, headers, timeout_seconds):
        """Return a 200 response (or 404) without any network access."""

        import io

        if url == self.binary_url:
            size = self.binary.stat().st_size
            return TransportResponse(
                200, {"Content-Length": str(size)}, open(str(self.binary), "rb")
            )
        if url not in self.bodies:
            return TransportResponse(404, {"Content-Length": "0"}, io.BytesIO(b""))
        body = self.bodies[url]
        return TransportResponse(
            200, {"Content-Length": str(len(body))}, io.BytesIO(body)
        )


def test_promote_real(tmp_path, capsys):
    """Promote, verify, and self-test a real binary (name kept short for MAX_PATH)."""

    binary = Path(REAL_BINARY)
    key = (
        os.environ.get("CLAUDE_WRANGLER_TEST_REAL_BINARY_PLATFORM")
        or host_platform_key()
    )
    version = matching_release(binary, key)
    if version is None:
        pytest.skip("no recorded release manifest lists this binary for {}".format(key))
    bodies = {
        "{}/{}/{}".format(DEFAULT_RELEASE_BASE_URL, version, name): (
            FIXTURES / version / name
        ).read_bytes()
        for name in EVIDENCE_NAMES
    }
    bodies["{}/stable".format(DEFAULT_RELEASE_BASE_URL)] = (version + "\n").encode()
    binary_url = "{}/{}/{}/{}".format(
        DEFAULT_RELEASE_BASE_URL, version, key, binary_filename(key)
    )
    fetcher = HttpsFetcher(
        release_hosts(DEFAULT_RELEASE_BASE_URL),
        retries=0,
        transport=FileTransport(bodies, binary_url, binary),
        sleep=lambda seconds: None,
    )
    source = ReleaseSource(DEFAULT_RELEASE_BASE_URL, fetcher=fetcher)
    services = Services(
        release_source=lambda base_url, timeout, ca_bundle: source,
        find_processes=FakeProcessTable().find,
        stop_service=lambda binary, env: ServiceStopResult((), 0, ""),
        clock=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc),
        resolve_platform=lambda override, keys: key,
        progress=lambda label: None,
    )
    project = tmp_path / "p"
    (project / ".git").mkdir(parents=True)

    config = make_config(
        project, "--set-update-checks", "off", "--set-windows-sandbox-warning", "off"
    )
    assert install_like_operation(config, services) == 0

    store = RuntimeStore(
        project / ".local" / "claude" / "wrangler", project_root=project
    )
    record = store.slot_record("a")
    assert store.read_pointer() == "a"
    assert record.version == version
    assert record.platforms[key]["sha256"] == sha256_of(binary)
    assert "mcp" in record.subcommands
    assert {"update", "upgrade", "install"} <= set(record.subcommands)

    ensure_gitignore_block(make_config(project))
    capsys.readouterr()
    assert selftest_operation(make_config(project, "--selftest"), services) == 0
    output = capsys.readouterr().out
    assert "[PASS] slot_a_verified" in output
    assert "[PASS] active_binary_version_runs" in output
