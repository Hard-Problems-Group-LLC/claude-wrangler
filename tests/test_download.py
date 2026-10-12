"""Offline tests for the HTTPS-only, bounded, verified downloader.

Every test drives ``HttpsFetcher`` through an in-memory fake transport, so
nothing here opens a socket.
"""

from __future__ import annotations

import hashlib
import io
import socket
import ssl
from typing import Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

import pytest

from claude_wrangler import download
from claude_wrangler.download import (
    USER_AGENT,
    DownloadError,
    HttpsFetcher,
    TransportResponse,
    UrllibTransport,
    describe_tls_failure,
    issuer_from_der,
    resolve_ca_bundle,
    stderr_progress,
)
from claude_wrangler.models import ClaudeWranglerError

HOST = "downloads.claude.ai"
BASE = "https://downloads.claude.ai/claude-code-releases"
BINARY_URL = BASE + "/2.1.300/linux-x64/claude"
PAYLOAD = bytes(range(256)) * 1024  # 256 KiB: several 64 KiB chunks
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()

Responder = Callable[[str, Dict[str, str]], TransportResponse]


class ScriptedBody:
    """Body that yields ``data`` and then either EOF or a raised exception."""

    def __init__(self, data: bytes, error: Optional[BaseException] = None) -> None:
        """Hold the bytes to serve and the optional failure at the end."""

        self.data = data
        self.position = 0
        self.error = error
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        """Serve the next chunk, then raise ``error`` (if any) at the end."""

        if self.position < len(self.data):
            end = len(self.data) if size < 0 else self.position + size
            chunk = self.data[self.position : end]
            self.position += len(chunk)
            return chunk
        if self.error is not None:
            raise self.error
        return b""

    def close(self) -> None:
        """Record that the fetcher released the body."""

        self.closed = True


class FakeTransport:
    """In-memory transport: each URL maps to a queue of responders.

    The last responder for a URL repeats once the queue drains.
    """

    def __init__(self, issuer: Optional[str] = None) -> None:
        """Start with no routes and an optional issuer for TLS diagnostics."""

        self.routes: Dict[str, List[Responder]] = {}
        self.calls: List[Tuple[str, Dict[str, str], float]] = []
        self.issuer = issuer

    def add(self, url: str, *responders: Responder) -> "FakeTransport":
        """Queue responders for ``url``."""

        self.routes.setdefault(url, []).extend(responders)
        return self

    def open(self, url, headers, timeout_seconds):
        """Record the request and run the next responder for ``url``."""

        self.calls.append((url, dict(headers), timeout_seconds))
        queue = self.routes.get(url)
        if not queue:
            raise AssertionError("unexpected request for {}".format(url))
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(url, dict(headers))

    def presented_issuer(self, url):
        """Return the configured issuer (the real transport probes TLS)."""

        return self.issuer

    def urls(self) -> List[str]:
        """Return the requested URLs in order."""

        return [call[0] for call in self.calls]


def ok(
    data: bytes,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None,
    content_length: bool = True,
    error: Optional[BaseException] = None,
    declared: Optional[int] = None,
) -> Responder:
    """Return a responder serving ``data`` (optionally failing at the end)."""

    def respond(url, request_headers):
        merged = {}
        if content_length:
            merged["Content-Length"] = str(len(data) if declared is None else declared)
        merged.update(headers or {})
        return TransportResponse(status, merged, ScriptedBody(data, error))

    return respond


def redirect(location: str, status: int = 302) -> Responder:
    """Return a responder answering with a redirect to ``location``."""

    def respond(url, request_headers):
        return TransportResponse(status, {"Location": location}, ScriptedBody(b""))

    return respond


def fail(error: BaseException) -> Responder:
    """Return a responder that raises ``error`` from ``open``."""

    def respond(url, request_headers):
        raise error

    return respond


def ranged(data: bytes) -> Responder:
    """Return a responder that honors ``Range: bytes=N-`` with a 206."""

    def respond(url, request_headers):
        header = request_headers.get("Range")
        if header is None:
            return ok(data)(url, request_headers)
        start = int(header[len("bytes=") : -1])
        body = data[start:]
        return TransportResponse(
            206,
            {
                "Content-Length": str(len(body)),
                "Content-Range": "bytes {}-{}/{}".format(
                    start, len(data) - 1, len(data)
                ),
            },
            ScriptedBody(body),
        )

    return respond


def make_fetcher(
    transport: FakeTransport, retries: int = 3, hosts=(HOST,)
) -> Tuple[HttpsFetcher, List[float]]:
    """Return a fetcher over ``transport`` plus the list its sleeps go to."""

    delays: List[float] = []
    fetcher = HttpsFetcher(
        hosts,
        timeout_seconds=7.5,
        retries=retries,
        transport=transport,
        sleep=delays.append,
    )
    return fetcher, delays


@pytest.fixture(autouse=True)
def no_operator_ca_bundle(monkeypatch):
    """Keep an operator's SSL_CERT_FILE from leaking into transport tests."""

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


# -- construction and URL policy -------------------------------------------


def test_download_error_is_a_wrangler_error():
    assert issubclass(DownloadError, ClaudeWranglerError)


def test_fetcher_rejects_bad_settings():
    transport = FakeTransport()
    with pytest.raises(DownloadError):
        HttpsFetcher([], transport=transport)
    with pytest.raises(DownloadError):
        HttpsFetcher([HOST], retries=-1, transport=transport)
    with pytest.raises(DownloadError):
        HttpsFetcher([HOST], timeout_seconds=0, transport=transport)
    with pytest.raises(DownloadError):
        HttpsFetcher(["https://downloads.claude.ai/"], transport=transport)


def test_fetcher_treats_a_single_string_as_one_host():
    fetcher = HttpsFetcher("Downloads.Claude.AI.", transport=FakeTransport())
    assert fetcher.allowed_hosts == frozenset({HOST})


def test_default_transport_is_urllib_and_needs_no_network():
    fetcher = HttpsFetcher([HOST])
    assert isinstance(fetcher.transport, UrllibTransport)


@pytest.mark.parametrize(
    "url",
    [
        "http://downloads.claude.ai/claude-code-releases/stable",
        "https://evil.example/claude-code-releases/stable",
        "https://user:pw@downloads.claude.ai/claude-code-releases/stable",
        "https://downloads.claude.ai/a b",
        "ftp://downloads.claude.ai/stable",
        "https://downloads.claude.ai:notaport/stable",
    ],
)
def test_disallowed_urls_are_refused_before_any_request(url):
    transport = FakeTransport()
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="Refusing"):
        fetcher.get_bytes(url, 100)
    assert transport.calls == []


def test_get_bytes_sends_identity_user_agent_and_timeout():
    transport = FakeTransport().add(BASE + "/stable", ok(b"2.1.300\n"))
    fetcher, _ = make_fetcher(transport)
    assert fetcher.get_bytes(BASE + "/stable", 64) == b"2.1.300\n"
    url, headers, timeout = transport.calls[0]
    assert headers["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith("claude-wrangler/")
    assert headers["Accept-Encoding"] == "identity"
    assert timeout == 7.5


# -- redirects ---------------------------------------------------------------


def test_redirect_to_http_is_refused():
    transport = FakeTransport().add(
        BASE + "/stable", redirect("http://downloads.claude.ai/claude-code-releases/x")
    )
    fetcher, delays = make_fetcher(transport)
    with pytest.raises(DownloadError, match="Refusing redirect.*HTTPS"):
        fetcher.get_bytes(BASE + "/stable", 64)
    assert transport.urls() == [BASE + "/stable"]
    assert delays == []


def test_redirect_to_other_host_is_refused():
    transport = FakeTransport().add(
        BASE + "/stable", redirect("https://mirror.evil.example/stable", status=307)
    )
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="mirror.evil.example.*allowlist"):
        fetcher.get_bytes(BASE + "/stable", 64)
    assert len(transport.calls) == 1


def test_redirect_within_allowlist_is_followed_including_relative_location():
    second = "https://mirror.example.com/cc/stable"
    third = "https://mirror.example.com/cc/v/stable"
    transport = (
        FakeTransport()
        .add(BASE + "/stable", redirect(second, status=301))
        .add(second, redirect("v/stable", status=308))
        .add(third, ok(b"2.1.300"))
    )
    fetcher, _ = make_fetcher(transport, hosts=(HOST, "mirror.example.com"))
    assert fetcher.get_bytes(BASE + "/stable", 64) == b"2.1.300"
    assert transport.urls() == [BASE + "/stable", second, third]


def test_redirect_without_location_is_an_error():
    def respond(url, headers):
        return TransportResponse(302, {}, ScriptedBody(b""))

    transport = FakeTransport().add(BASE + "/stable", respond)
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="no Location"):
        fetcher.get_bytes(BASE + "/stable", 64)


def test_redirect_loop_is_bounded():
    transport = FakeTransport().add(BASE + "/stable", redirect(BASE + "/stable"))
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="Too many redirects"):
        fetcher.get_bytes(BASE + "/stable", 64)
    assert len(transport.calls) == download.MAX_REDIRECTS + 1


# -- retries -----------------------------------------------------------------


def test_connection_errors_and_5xx_are_retried_with_backoff():
    transport = FakeTransport().add(
        BASE + "/stable",
        ok(b"", status=503),
        fail(ConnectionResetError("reset by peer")),
        fail(urllib.error.URLError(socket.gaierror("name lookup failed"))),
        ok(b"2.1.300"),
    )
    fetcher, delays = make_fetcher(transport, retries=3)
    assert fetcher.get_bytes(BASE + "/stable", 64) == b"2.1.300"
    assert len(transport.calls) == 4
    assert delays == [1.0, 2.0, 4.0]


def test_retries_are_bounded():
    transport = FakeTransport().add(BASE + "/stable", ok(b"", status=502))
    fetcher, delays = make_fetcher(transport, retries=2)
    with pytest.raises(DownloadError, match="after 3 attempt"):
        fetcher.get_bytes(BASE + "/stable", 64)
    assert len(transport.calls) == 3
    assert len(delays) == 2


@pytest.mark.parametrize("status", [400, 403, 404, 410, 429])
def test_4xx_is_not_retried(status):
    transport = FakeTransport().add(BASE + "/stable", ok(b"nope", status=status))
    fetcher, delays = make_fetcher(transport)
    with pytest.raises(DownloadError, match="HTTP {}".format(status)):
        fetcher.get_bytes(BASE + "/stable", 64)
    assert len(transport.calls) == 1
    assert delays == []


@pytest.mark.parametrize(
    "error",
    [socket.timeout("timed out"), urllib.error.URLError(socket.timeout("timed out"))],
)
def test_timeout_becomes_download_error_after_retries(error):
    transport = FakeTransport().add(BASE + "/stable", fail(error))
    fetcher, _ = make_fetcher(transport, retries=1)
    with pytest.raises(
        DownloadError, match="timed out after 7.5 s waiting for " + HOST
    ):
        fetcher.get_bytes(BASE + "/stable", 64)
    assert len(transport.calls) == 2


def test_timeout_mid_body_is_retried():
    transport = FakeTransport().add(
        BASE + "/stable",
        ok(b"2.1", declared=7, error=socket.timeout("timed out")),
        ok(b"2.1.300"),
    )
    fetcher, _ = make_fetcher(transport)
    assert fetcher.get_bytes(BASE + "/stable", 64) == b"2.1.300"


# -- TLS ---------------------------------------------------------------------


def test_tls_verification_failure_names_host_and_issuer_without_retry():
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_message = "unable to get local issuer certificate"
    transport = FakeTransport(issuer="O=Corp, CN=Corp Inspection CA").add(
        BASE + "/stable", fail(urllib.error.URLError(error))
    )
    fetcher, delays = make_fetcher(transport)
    with pytest.raises(DownloadError) as caught:
        fetcher.get_bytes(BASE + "/stable", 64)
    message = str(caught.value)
    assert HOST in message
    assert "unable to get local issuer certificate" in message
    assert "CN=Corp Inspection CA" in message
    assert "--ca-bundle" in message
    assert len(transport.calls) == 1
    assert delays == []


def test_tls_failure_survives_a_failing_issuer_probe():
    class ProbeFails(FakeTransport):
        def presented_issuer(self, url):
            raise RuntimeError("probe exploded")

    transport = ProbeFails().add(
        BASE + "/stable", fail(ssl.SSLCertVerificationError(1, "verify failed"))
    )
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="TLS certificate verification failed"):
        fetcher.get_bytes(BASE + "/stable", 64)


def test_describe_tls_failure_adds_macos_hint_only_on_darwin():
    darwin = describe_tls_failure(HOST, "self-signed", "CN=X", platform="darwin")
    windows = describe_tls_failure(HOST, "self-signed", None, platform="win32")
    assert "Install Certificates" in darwin
    assert "CN=X" in darwin
    assert "Install Certificates" not in windows
    assert HOST in windows
    # A self-signed certificate is not the fresh-Windows missing-root case.
    assert "Invoke-WebRequest" not in windows


def test_describe_tls_failure_explains_a_missing_root_on_windows():
    # Seen on a fresh Windows (Windows Sandbox): Python failed until one
    # PowerShell request made Windows fetch GlobalSign Root CA.
    url = BASE + "/stable"
    windows = describe_tls_failure(
        HOST, "unable to get local issuer certificate", "CN=WR3", "win32", url=url
    )
    assert "newly installed Windows" in windows
    assert "Invoke-WebRequest -UseBasicParsing {} | Out-Null".format(url) in windows
    linux = describe_tls_failure(
        HOST, "unable to get local issuer certificate", "CN=WR3", "linux", url=url
    )
    assert "Invoke-WebRequest" not in linux
    fallback = describe_tls_failure(
        HOST, "unable to get local issuer certificate", None, "win32"
    )
    assert "https://{}/".format(HOST) in fallback


def _der(tag: int, content: bytes) -> bytes:
    """Encode one DER element (test helper)."""

    size = len(content)
    if size < 0x80:
        length = bytes([size])
    else:
        raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + content


def _name(*pairs: Tuple[bytes, int, bytes]) -> bytes:
    """Encode an X.509 Name from ``(oid, string_tag, value)`` triples."""

    return _der(
        0x30,
        b"".join(
            _der(0x31, _der(0x30, _der(0x06, oid) + _der(tag, value)))
            for oid, tag, value in pairs
        ),
    )


def _certificate(issuer: bytes, with_version: bool = True) -> bytes:
    """Build a structurally plausible certificate around ``issuer``."""

    algorithm = _der(0x30, _der(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x01\x0b"))
    tbs = b""
    if with_version:
        tbs += _der(0xA0, _der(0x02, b"\x02"))
    tbs += _der(0x02, b"\x01\x23")
    tbs += algorithm
    tbs += issuer
    tbs += _der(0x30, b"")  # validity (contents irrelevant here)
    tbs += _name((b"\x55\x04\x03", 0x0C, b"downloads.claude.ai"))
    return _der(0x30, _der(0x30, tbs) + algorithm + _der(0x03, b"\x00" + b"\x01" * 32))


def test_issuer_from_der_reads_country_org_and_common_name():
    long_org = b"Example Corp " + b"x" * 200  # forces long-form lengths
    issuer = _name(
        (b"\x55\x04\x06", 0x13, b"US"),
        (b"\x55\x04\x0a", 0x0C, long_org),
        (b"\x55\x04\x07", 0x0C, b"Ignored Locality"),
        (b"\x55\x04\x03", 0x0C, b"Example Proxy CA"),
    )
    text = issuer_from_der(_certificate(issuer))
    assert text is not None
    assert text.startswith("C=US, O=Example Corp ")
    assert "Locality" not in text
    assert issuer_from_der(_certificate(issuer, with_version=False)).startswith("C=US")


def test_issuer_from_der_sanitizes_and_rejects_garbage():
    issuer = _name((b"\x55\x04\x03", 0x0C, b"Evil\x1b[31mCA"))
    assert issuer_from_der(_certificate(issuer)) == "CN=Evil?[31mCA"
    assert issuer_from_der(b"") is None
    assert issuer_from_der(b"\x30\x05\x30") is None
    # Arbitrary bytes must never raise.
    assert issuer_from_der(bytes(range(64))) is None


def test_resolve_ca_bundle_precedence(tmp_path):
    explicit = tmp_path / "explicit.pem"
    explicit.write_text("x", encoding="ascii")
    env_file = tmp_path / "env.pem"
    env_file.write_text("x", encoding="ascii")
    environ = {"SSL_CERT_FILE": str(env_file)}
    assert resolve_ca_bundle(str(explicit), environ) == str(explicit)
    assert resolve_ca_bundle(None, environ) == str(env_file)
    assert resolve_ca_bundle(None, {}) is None
    with pytest.raises(DownloadError, match="--ca-bundle"):
        resolve_ca_bundle(str(tmp_path / "missing.pem"), environ)
    with pytest.raises(DownloadError, match="SSL_CERT_FILE"):
        resolve_ca_bundle(None, {"SSL_CERT_FILE": str(tmp_path / "missing.pem")})


def test_urllib_transport_uses_ca_bundle_and_ssl_cert_file(tmp_path, monkeypatch):
    seen: List[Optional[str]] = []
    real_create = ssl.create_default_context

    def recording_create(*args, cafile=None, **kwargs):
        seen.append(cafile)
        return real_create()

    monkeypatch.setattr(download.ssl, "create_default_context", recording_create)
    bundle = tmp_path / "corp.pem"
    bundle.write_text("x", encoding="ascii")
    env_bundle = tmp_path / "env.pem"
    env_bundle.write_text("x", encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(env_bundle))
    HttpsFetcher([HOST], ca_bundle=str(bundle))
    HttpsFetcher([HOST])
    monkeypatch.delenv("SSL_CERT_FILE")
    HttpsFetcher([HOST])
    assert seen == [str(bundle), str(env_bundle), None]


def test_unloadable_ca_bundle_is_a_download_error(tmp_path):
    bundle = tmp_path / "broken.pem"
    bundle.write_text("not a certificate", encoding="ascii")
    with pytest.raises(DownloadError, match="CA bundle"):
        HttpsFetcher([HOST], ca_bundle=str(bundle))


def test_urllib_transport_honors_https_proxy_environment(monkeypatch):
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    transport = UrllibTransport()
    proxies = [
        handler
        for handler in transport._opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert proxies and proxies[0].proxies.get("https") == "http://proxy.invalid:3128"


def test_urllib_transport_never_follows_redirects(monkeypatch):
    handler = download._NoFollowRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://x/") is None
    transport = UrllibTransport()
    assert any(
        isinstance(item, download._NoFollowRedirectHandler)
        for item in transport._opener.handlers
    )
    headers = {"Location": "http://downloads.claude.ai/x"}
    error = urllib.error.HTTPError(
        BASE + "/stable", 302, "Found", headers, io.BytesIO(b"")
    )

    def raise_redirect(request, timeout=None):
        raise error

    monkeypatch.setattr(transport._opener, "open", raise_redirect)
    response = transport.open(BASE + "/stable", {"User-Agent": USER_AGENT}, 5.0)
    assert response.status == 302
    assert response.header("location") == "http://downloads.claude.ai/x"
    response.close()


# -- capped small reads ------------------------------------------------------


def test_get_bytes_refuses_declared_oversize_without_retry():
    transport = FakeTransport().add(BASE + "/x", ok(b"x" * 10, declared=10_000_000))
    fetcher, delays = make_fetcher(transport)
    with pytest.raises(DownloadError, match="over the 1024-byte limit"):
        fetcher.get_bytes(BASE + "/x", 1024)
    assert len(transport.calls) == 1
    assert delays == []


def test_get_bytes_aborts_streamed_oversize():
    transport = FakeTransport().add(BASE + "/x", ok(b"x" * 5000, content_length=False))
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="exceeded the 1024-byte limit"):
        fetcher.get_bytes(BASE + "/x", 1024)
    assert len(transport.calls) == 1


def test_get_bytes_retries_truncated_body():
    transport = FakeTransport().add(
        BASE + "/x", ok(b"abc", declared=10), ok(b"abcdefghij")
    )
    fetcher, _ = make_fetcher(transport)
    assert fetcher.get_bytes(BASE + "/x", 64) == b"abcdefghij"
    assert len(transport.calls) == 2


def test_get_bytes_refuses_compressed_and_bad_lengths():
    transport = (
        FakeTransport()
        .add(BASE + "/gz", ok(b"x", headers={"Content-Encoding": "gzip"}))
        .add(BASE + "/len", ok(b"x", headers={"Content-Length": "12abc"}))
    )
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="Content-Encoding"):
        fetcher.get_bytes(BASE + "/gz", 64)
    with pytest.raises(DownloadError, match="Content-Length"):
        fetcher.get_bytes(BASE + "/len", 64)


# -- verified downloads ------------------------------------------------------


def test_download_to_happy_path_reports_progress(tmp_path):
    transport = FakeTransport().add(BINARY_URL, ok(PAYLOAD))
    fetcher, _ = make_fetcher(transport)
    dest = tmp_path / "claude"
    seen: List[Tuple[int, int]] = []
    fetcher.download_to(
        BINARY_URL,
        dest,
        len(PAYLOAD),
        PAYLOAD_SHA.upper(),
        progress=lambda d, t: seen.append((d, t)),
    )
    assert dest.read_bytes() == PAYLOAD
    assert seen[0] == (0, len(PAYLOAD))
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert "Range" not in transport.calls[0][1]


def test_download_to_validates_arguments(tmp_path):
    fetcher, _ = make_fetcher(FakeTransport())
    dest = tmp_path / "claude"
    with pytest.raises(DownloadError, match="SHA-256"):
        fetcher.download_to(BINARY_URL, dest, 10, "abc")
    with pytest.raises(DownloadError, match="positive"):
        fetcher.download_to(BINARY_URL, dest, 0, PAYLOAD_SHA)
    with pytest.raises(DownloadError, match="does not exist"):
        fetcher.download_to(
            BINARY_URL, tmp_path / "missing" / "claude", 10, PAYLOAD_SHA
        )
    (tmp_path / "adir").mkdir()
    with pytest.raises(DownloadError, match="not a regular file"):
        fetcher.download_to(BINARY_URL, tmp_path / "adir", 10, PAYLOAD_SHA)
    with pytest.raises(DownloadError, match="Refusing"):
        fetcher.download_to("http://downloads.claude.ai/x", dest, 10, PAYLOAD_SHA)


def test_download_to_aborts_and_discards_on_oversize(tmp_path):
    transport = FakeTransport().add(
        BINARY_URL, ok(PAYLOAD + b"extra", content_length=False)
    )
    fetcher, delays = make_fetcher(transport)
    dest = tmp_path / "claude"
    with pytest.raises(DownloadError, match="more than the manifest size"):
        fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert not dest.exists()
    assert len(transport.calls) == 1
    assert delays == []


def test_download_to_refuses_mismatched_content_length(tmp_path):
    transport = FakeTransport().add(BINARY_URL, ok(PAYLOAD + b"x"))
    fetcher, _ = make_fetcher(transport)
    with pytest.raises(DownloadError, match="manifest says"):
        fetcher.download_to(BINARY_URL, tmp_path / "claude", len(PAYLOAD), PAYLOAD_SHA)
    assert len(transport.calls) == 1


def test_download_to_hash_mismatch_discards_without_retry(tmp_path):
    tampered = b"\xff" + PAYLOAD[1:]  # PAYLOAD itself starts with 0x00
    transport = FakeTransport().add(BINARY_URL, ok(tampered))
    fetcher, delays = make_fetcher(transport)
    dest = tmp_path / "claude"
    with pytest.raises(DownloadError, match="SHA-256 mismatch"):
        fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert not dest.exists()
    assert len(transport.calls) == 1
    assert delays == []


def test_download_to_resumes_after_truncation(tmp_path):
    half = len(PAYLOAD) // 2
    transport = FakeTransport().add(
        BINARY_URL,
        ok(PAYLOAD[:half], declared=len(PAYLOAD)),
        ranged(PAYLOAD),
    )
    fetcher, delays = make_fetcher(transport)
    dest = tmp_path / "claude"
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert transport.calls[1][1]["Range"] == "bytes={}-".format(half)
    assert delays == [1.0]


def test_download_to_resumes_after_connection_reset_mid_body(tmp_path):
    cut = 100_000
    transport = FakeTransport().add(
        BINARY_URL,
        ok(PAYLOAD[:cut], declared=len(PAYLOAD), error=ConnectionResetError("reset")),
        ranged(PAYLOAD),
    )
    fetcher, _ = make_fetcher(transport)
    dest = tmp_path / "claude"
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert transport.calls[1][1]["Range"] == "bytes={}-".format(cut)


def test_download_to_resumes_existing_partial_file(tmp_path):
    dest = tmp_path / "claude"
    dest.write_bytes(PAYLOAD[:12345])
    transport = FakeTransport().add(BINARY_URL, ranged(PAYLOAD))
    fetcher, _ = make_fetcher(transport)
    seen: List[Tuple[int, int]] = []
    fetcher.download_to(
        BINARY_URL,
        dest,
        len(PAYLOAD),
        PAYLOAD_SHA,
        progress=lambda d, t: seen.append((d, t)),
    )
    assert dest.read_bytes() == PAYLOAD
    assert len(transport.calls) == 1
    assert transport.calls[0][1]["Range"] == "bytes=12345-"
    assert seen[0] == (12345, len(PAYLOAD))


def test_download_to_restarts_when_server_ignores_range(tmp_path):
    dest = tmp_path / "claude"
    dest.write_bytes(PAYLOAD[:5000])
    transport = FakeTransport().add(BINARY_URL, ok(PAYLOAD))
    fetcher, _ = make_fetcher(transport)
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert len(transport.calls) == 1
    assert transport.calls[0][1]["Range"] == "bytes=5000-"


@pytest.mark.parametrize(
    "bad_reply",
    [
        ok(PAYLOAD[4000:], status=206, headers={"Content-Range": "bytes 4000-99/100"}),
        ok(PAYLOAD[4000:], status=206, headers={}),
        ok(b"", status=416),
    ],
    ids=["wrong-content-range", "missing-content-range", "416"],
)
def test_download_to_restarts_from_zero_on_inconsistent_range_reply(
    tmp_path, bad_reply
):
    dest = tmp_path / "claude"
    dest.write_bytes(PAYLOAD[:5000])
    transport = FakeTransport().add(BINARY_URL, bad_reply, ok(PAYLOAD))
    fetcher, delays = make_fetcher(transport)
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert transport.calls[0][1]["Range"] == "bytes=5000-"
    assert "Range" not in transport.calls[1][1]
    assert delays == []


def test_download_to_restarts_once_when_resumed_prefix_was_corrupt(tmp_path):
    dest = tmp_path / "claude"
    dest.write_bytes(b"\xff" * 5000)  # wrong prefix bytes
    transport = FakeTransport().add(BINARY_URL, ranged(PAYLOAD), ok(PAYLOAD))
    fetcher, _ = make_fetcher(transport)
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert transport.calls[0][1]["Range"] == "bytes=5000-"
    assert "Range" not in transport.calls[1][1]


def test_download_to_accepts_complete_existing_file_without_request(tmp_path):
    dest = tmp_path / "claude"
    dest.write_bytes(PAYLOAD)
    transport = FakeTransport()
    fetcher, _ = make_fetcher(transport)
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert transport.calls == []


@pytest.mark.parametrize(
    "existing",
    [b"\xff" * len(PAYLOAD), PAYLOAD + b"tail"],
    ids=["wrong-bytes", "oversize"],
)
def test_download_to_replaces_wrong_full_or_oversize_file(tmp_path, existing):
    dest = tmp_path / "claude"
    dest.write_bytes(existing)
    transport = FakeTransport().add(BINARY_URL, ok(PAYLOAD))
    fetcher, _ = make_fetcher(transport)
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert "Range" not in transport.calls[0][1]


def test_download_to_keeps_partial_when_retries_run_out(tmp_path):
    transport = FakeTransport().add(
        BINARY_URL, ok(PAYLOAD[:1000], declared=len(PAYLOAD))
    )
    fetcher, _ = make_fetcher(transport, retries=0)
    dest = tmp_path / "claude"
    with pytest.raises(DownloadError, match="connection closed after 1000"):
        fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD[:1000]


def test_download_to_follows_allowed_redirect_and_keeps_range(tmp_path):
    cdn = "https://downloads.claude.ai/cdn/claude"
    dest = tmp_path / "claude"
    dest.write_bytes(PAYLOAD[:777])
    transport = FakeTransport().add(BINARY_URL, redirect(cdn)).add(cdn, ranged(PAYLOAD))
    fetcher, _ = make_fetcher(transport)
    fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert dest.read_bytes() == PAYLOAD
    assert transport.calls[1][1]["Range"] == "bytes=777-"


def test_download_to_refuses_redirect_off_https(tmp_path):
    transport = FakeTransport().add(
        BINARY_URL, redirect("http://downloads.claude.ai/x")
    )
    fetcher, _ = make_fetcher(transport)
    dest = tmp_path / "claude"
    with pytest.raises(DownloadError, match="Refusing redirect"):
        fetcher.download_to(BINARY_URL, dest, len(PAYLOAD), PAYLOAD_SHA)
    assert not dest.exists()


def test_stderr_progress_prints_coarse_steps():
    stream = io.StringIO()
    report = stderr_progress("claude 2.1.300", stream=stream, step_percent=25)
    for done in (0, 10, 30, 49, 50, 99, 100, 100):
        report(done, 100)
    lines = stream.getvalue().splitlines()
    assert [line.split(":")[1].split("%")[0].strip() for line in lines] == [
        "0",
        "25",
        "50",
        "75",
        "100",
    ]
