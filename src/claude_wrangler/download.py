"""HTTPS-only, bounded, verified downloads for Claude Code release files.

``HttpsFetcher`` is the only network client claude-wrangler uses for the
native runtime. Every request URL, including every redirect hop, must be
HTTPS on an allowlisted host. Small metadata reads are capped. Binary
downloads stream into a staging file while hashing, abort as soon as more
bytes arrive than the manifest promised, can resume with an HTTP Range
request, and finish only when the byte count and SHA-256 equal the
manifest.

The network sits behind the small ``Transport`` protocol so tests run
offline against in-memory fakes. ``UrllibTransport`` is the default: it
verifies TLS against the system store, an explicit CA bundle, or
``SSL_CERT_FILE``, honors ``HTTPS_PROXY``/``NO_PROXY`` through urllib's
``ProxyHandler``, and never follows a redirect on its own.

This module is on the bootstrap path, so it stays standard-library-only
and Python 3.9 compatible.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import time
from typing import (
    Callable,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    TextIO,
    Tuple,
    TypeVar,
)
import urllib.error
import urllib.parse
import urllib.request

from ._version import __version__
from .models import ClaudeWranglerError

T = TypeVar("T")

USER_AGENT = "claude-wrangler/{}".format(__version__)
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_RETRIES = 3
MAX_REDIRECTS = 5
CHUNK_SIZE = 64 * 1024
HASH_CHUNK_SIZE = 1024 * 1024
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 8.0
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ISSUER_PROBE_TIMEOUT_SECONDS = 10.0

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)")
_HOSTNAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
)
_MAX_ISSUER_TEXT = 300

# X.509 Name attribute OIDs (DER content bytes) worth showing an operator.
_NAME_OIDS = {
    b"\x55\x04\x06": "C",
    b"\x55\x04\x0a": "O",
    b"\x55\x04\x0b": "OU",
    b"\x55\x04\x03": "CN",
}


class DownloadError(ClaudeWranglerError):
    """Raised when a fetch or download cannot be completed safely."""


class _RetryableFailure(Exception):
    """Internal: one attempt failed in a way a fresh attempt might fix."""


class _Readable(Protocol):
    """Anything with a ``read(size)`` method returning bytes."""

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes; ``b""`` at end of stream."""
        ...


class _Hasher(Protocol):
    """The subset of a ``hashlib`` hash object the downloader uses."""

    def update(self, data: bytes) -> None:
        """Feed more bytes into the hash."""
        ...

    def hexdigest(self) -> str:
        """Return the digest as lower-case hex."""
        ...


class TransportResponse:
    """One HTTP response as seen by ``HttpsFetcher``.

    Header names are stored in lower case. A transport returns redirect
    responses as-is instead of following them, so the fetcher can apply
    its HTTPS and host policy to every hop.
    """

    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        body: Optional[_Readable] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """Wrap a status code, header mapping, and readable body."""

        self.status = int(status)
        self.headers = {str(key).lower(): str(value) for key, value in headers.items()}
        self._body = body
        self._on_close = on_close

    def header(self, name: str) -> Optional[str]:
        """Return one header value by case-insensitive name, or None."""

        return self.headers.get(name.lower())

    def read(self, size: int) -> bytes:
        """Read up to ``size`` body bytes; ``b""`` means end of body."""

        if self._body is None:
            return b""
        return self._body.read(size)

    def close(self) -> None:
        """Release the underlying connection, ignoring close-time errors."""

        closer = self._on_close
        if closer is None:
            closer = getattr(self._body, "close", None)
        if closer is None:
            return
        try:
            closer()
        except (OSError, http.client.HTTPException):
            pass


class Transport(Protocol):
    """Minimal network interface ``HttpsFetcher`` needs.

    ``open`` sends one GET and returns the response without following
    redirects. Transport failures are raised as ``OSError`` (including
    ``socket.timeout``, ``ConnectionError``, ``ssl.SSLError``, and
    ``urllib.error.URLError``) or ``http.client.HTTPException``; the same
    exception types may come out of ``TransportResponse.read``.

    A transport may also provide ``presented_issuer(url) -> str | None``,
    used only to name the certificate issuer in TLS error messages.
    """

    def open(
        self, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> TransportResponse:
        """Send one GET for ``url`` with ``headers`` and return the response."""
        ...


def resolve_ca_bundle(
    ca_bundle: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Return the CA file to trust, or None for the system trust store.

    An explicit ``--ca-bundle`` wins over ``SSL_CERT_FILE``. Either one
    replaces the system store rather than extending it, matching how
    OpenSSL-based tools treat ``SSL_CERT_FILE``.
    """

    env = os.environ if environ is None else environ
    if ca_bundle:
        candidate, source = ca_bundle, "--ca-bundle"
    else:
        candidate, source = env.get("SSL_CERT_FILE") or "", "SSL_CERT_FILE"
    if not candidate:
        return None
    if not os.path.isfile(candidate):
        raise DownloadError(
            "The CA bundle named by {} does not exist or is not a file: {}".format(
                source, candidate
            )
        )
    return candidate


def build_ssl_context(cafile: Optional[str]) -> ssl.SSLContext:
    """Return a verifying TLS client context, optionally pinned to one CA file."""

    try:
        context = ssl.create_default_context(cafile=cafile)
    except OSError as exc:
        raise DownloadError(
            "Could not load the CA bundle {}: {}".format(cafile, exc)
        ) from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _sanitize_text(text: str) -> str:
    """Replace control characters so untrusted text cannot drive a terminal."""

    cleaned = "".join(
        "?" if (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F) else char
        for char in text
    )
    if len(cleaned) > _MAX_ISSUER_TEXT:
        cleaned = cleaned[: _MAX_ISSUER_TEXT - 3] + "..."
    return cleaned


def _der_element(data: bytes, offset: int, limit: int) -> Tuple[int, int, int]:
    """Return ``(tag, value_start, value_end)`` for the DER element at offset."""

    if offset + 2 > limit:
        raise ValueError("truncated DER header")
    tag = data[offset]
    length = data[offset + 1]
    position = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or position + count > limit:
            raise ValueError("unsupported DER length")
        length = int.from_bytes(data[position : position + count], "big")
        position += count
    end = position + length
    if end > limit:
        raise ValueError("DER element overruns its container")
    return tag, position, end


def _der_children(data: bytes, start: int, end: int) -> List[Tuple[int, int, int]]:
    """Return every DER element directly inside ``data[start:end]``."""

    children = []
    position = start
    while position < end:
        tag, value_start, value_end = _der_element(data, position, end)
        children.append((tag, value_start, value_end))
        position = value_end
    return children


def _decode_der_string(tag: int, raw: bytes) -> Optional[str]:
    """Decode one ASN.1 string value used in X.509 names, or return None."""

    if tag == 0x0C:
        return raw.decode("utf-8", "replace")
    if tag in (0x13, 0x14, 0x16):
        return raw.decode("latin-1")
    if tag == 0x1E:
        return raw.decode("utf-16-be", "replace")
    if tag == 0x1C:
        return raw.decode("utf-32-be", "replace")
    return None


def issuer_from_der(der: bytes) -> Optional[str]:
    """Return a readable issuer name (``C=.., O=.., CN=..``) from a DER cert.

    Only the issuer's country, organization, unit, and common name are
    extracted. Any parse failure returns None: the result only decorates
    an error message and is never trusted.
    """

    try:
        tag, start, end = _der_element(der, 0, len(der))
        if tag != 0x30:
            return None
        tag, start, end = _der_element(der, start, end)
        if tag != 0x30:
            return None
        fields = _der_children(der, start, end)
        # tbsCertificate: [0] version (optional), serialNumber, signature,
        # issuer, ...
        index = 1 if fields and fields[0][0] == 0xA0 else 0
        issuer_tag, issuer_start, issuer_end = fields[index + 2]
        if issuer_tag != 0x30:
            return None
        parts = []
        for set_tag, set_start, set_end in _der_children(der, issuer_start, issuer_end):
            if set_tag != 0x31:
                return None
            for _seq_tag, seq_start, seq_end in _der_children(der, set_start, set_end):
                pair = _der_children(der, seq_start, seq_end)
                if len(pair) < 2 or pair[0][0] != 0x06:
                    continue
                label = _NAME_OIDS.get(der[pair[0][1] : pair[0][2]])
                if label is None:
                    continue
                text = _decode_der_string(pair[1][0], der[pair[1][1] : pair[1][2]])
                if text is not None:
                    parts.append("{}={}".format(label, text))
    except (ValueError, IndexError):
        return None
    if not parts:
        return None
    return _sanitize_text(", ".join(parts))


def describe_tls_failure(
    host: str,
    detail: str,
    issuer: Optional[str] = None,
    platform: Optional[str] = None,
) -> str:
    """Return the operator-facing message for a TLS verification failure."""

    current_platform = sys.platform if platform is None else platform
    lines = [
        "TLS certificate verification failed for {}: {}.".format(host, detail),
    ]
    if issuer:
        lines.append("The server presented a certificate issued by: {}.".format(issuer))
    lines.append(
        "If a TLS-inspecting proxy is in use, pass --ca-bundle <its CA "
        "bundle> or set SSL_CERT_FILE."
    )
    if current_platform == "darwin":
        lines.append(
            "On macOS with a python.org Python, run 'Install "
            "Certificates.command' from the /Applications/Python 3.x/ "
            "folder to install a CA bundle for Python."
        )
    return " ".join(lines)


class _NoFollowRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses to follow any redirect by itself.

    urllib's default handler would follow a redirect to plain HTTP or to
    any host. Declining here hands the 3xx response back to the caller (as
    an ``HTTPError``) so ``HttpsFetcher`` vets each hop against its HTTPS
    and host allowlist before requesting it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Decline to build a follow-up request for any redirect."""

        return None


def _message_headers(message: object) -> Mapping[str, str]:
    """Return a plain header mapping from an ``email.message.Message``."""

    items = getattr(message, "items", None)
    if items is None:
        return {}
    return {str(key): str(value) for key, value in items()}


class UrllibTransport:
    """Default ``Transport``: urllib with verified TLS and environment proxies."""

    def __init__(self, ca_bundle: Optional[str] = None) -> None:
        """Build the opener; raise ``DownloadError`` for an unusable CA bundle."""

        self.cafile = resolve_ca_bundle(ca_bundle)
        self._context = build_ssl_context(self.cafile)
        # ProxyHandler() reads HTTPS_PROXY/NO_PROXY (and the Windows registry
        # when no proxy variables are set).
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=self._context),
            _NoFollowRedirectHandler(),
        )

    def open(
        self, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> TransportResponse:
        """Send one GET; non-2xx responses come back as responses, not errors."""

        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            raw = self._opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as exc:
            return TransportResponse(
                exc.code, _message_headers(exc.headers), exc, on_close=exc.close
            )
        return TransportResponse(raw.getcode(), _message_headers(raw.headers), raw)

    def presented_issuer(self, url: str) -> Optional[str]:
        """Return the issuer of the certificate ``url``'s server presents.

        This opens a separate, unverified TLS handshake (through the same
        HTTPS proxy, when one applies) solely to read the certificate for
        an error message. No request is sent and nothing from it is used
        for trust decisions.
        """

        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if not host:
            return None
        port = parts.port or 443
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = None
        try:
            connection = self._probe_connection(host, port, context)
            if connection is None:
                return None
            connection.connect()
            sock = connection.sock
            der = sock.getpeercert(binary_form=True) if sock is not None else None
        except (OSError, http.client.HTTPException, ValueError):
            return None
        finally:
            if connection is not None:
                connection.close()
        if not der:
            return None
        return issuer_from_der(der)

    def _probe_connection(
        self, host: str, port: int, context: ssl.SSLContext
    ) -> Optional[http.client.HTTPSConnection]:
        """Return an unconnected probe connection honoring the HTTPS proxy."""

        proxy = urllib.request.getproxies().get("https")
        if not proxy or urllib.request.proxy_bypass(host):
            return http.client.HTTPSConnection(
                host, port, timeout=ISSUER_PROBE_TIMEOUT_SECONDS, context=context
            )
        if "://" not in proxy:
            proxy = "http://" + proxy
        proxy_parts = urllib.parse.urlsplit(proxy)
        # Only plain-HTTP proxies are probed; TLS-to-proxy is out of scope for
        # an error-message diagnostic.
        if proxy_parts.scheme != "http" or not proxy_parts.hostname:
            return None
        connection = http.client.HTTPSConnection(
            proxy_parts.hostname,
            proxy_parts.port or 80,
            timeout=ISSUER_PROBE_TIMEOUT_SECONDS,
            context=context,
        )
        tunnel_headers = {}
        if proxy_parts.username is not None:
            credentials = "{}:{}".format(
                urllib.parse.unquote(proxy_parts.username),
                urllib.parse.unquote(proxy_parts.password or ""),
            )
            tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
                credentials.encode("utf-8")
            ).decode("ascii")
        connection.set_tunnel(host, port, headers=tunnel_headers)
        return connection


def _normalize_host(raw: str) -> str:
    """Return one allowlist entry in canonical form or raise DownloadError."""

    host = str(raw).strip().lower().rstrip(".")
    if not _HOSTNAME.fullmatch(host):
        raise DownloadError("Invalid download host in allowlist: {!r}".format(raw))
    return host


def _normalize_sha256(value: str) -> str:
    """Return a lower-case 64-hex-digit SHA-256 or raise DownloadError."""

    digest = str(value).strip().lower()
    if not _SHA256_HEX.fullmatch(digest):
        raise DownloadError("Expected SHA-256 is not 64 hex digits: {!r}".format(value))
    return digest


def _report(
    progress: Optional[Callable[[int, int], None]], done: int, total: int
) -> None:
    """Forward one progress update when a callback was supplied."""

    if progress is not None:
        progress(done, total)


class _DownloadState:
    """Mutable facts that persist across retries of one ``download_to`` call."""

    def __init__(self) -> None:
        """Start with no restart spent."""

        self.restarted_after_mismatch = False


class HttpsFetcher:
    """HTTPS-only GET client with a host allowlist, caps, retries, and resume."""

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        ca_bundle: Optional[str] = None,
        transport: Optional[Transport] = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        backoff_seconds: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        """Validate settings and build the default transport when none is given.

        ``timeout_seconds`` bounds the connect and each socket read.
        ``retries`` is the number of extra attempts after the first for
        connection errors, timeouts, truncated bodies, and HTTP 5xx; HTTP
        4xx and policy, size, and hash failures are never retried.
        ``sleep`` and ``backoff_seconds`` exist so tests need not wait.
        """

        if isinstance(allowed_hosts, str):
            allowed_hosts = [allowed_hosts]
        hosts = frozenset(_normalize_host(host) for host in allowed_hosts)
        if not hosts:
            raise DownloadError("At least one allowed download host is required.")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise DownloadError("retries must be a non-negative integer.")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise DownloadError("timeout_seconds must be a number.") from exc
        if not timeout > 0:
            raise DownloadError("timeout_seconds must be positive.")
        self.allowed_hosts = hosts
        self.timeout_seconds = timeout
        self.retries = retries
        self.ca_bundle = ca_bundle
        self.transport: Transport = (
            transport if transport is not None else UrllibTransport(ca_bundle)
        )
        self._sleep = sleep
        self._backoff_seconds = float(backoff_seconds)

    # -- URL policy -----------------------------------------------------

    def _url_problem(self, url: str) -> Optional[str]:
        """Return why ``url`` may not be fetched, or None when it is allowed."""

        if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
            return "the URL contains whitespace or control characters"
        try:
            parts = urllib.parse.urlsplit(url)
            _ = parts.port  # raises ValueError for a malformed port
        except ValueError:
            return "the URL is malformed"
        if parts.scheme.lower() != "https":
            return "only HTTPS URLs are allowed"
        if parts.username is not None or parts.password is not None:
            return "URLs with embedded credentials are not allowed"
        host = (parts.hostname or "").rstrip(".")
        if host not in self.allowed_hosts:
            return "host {!r} is not in the download allowlist ({})".format(
                host, ", ".join(sorted(self.allowed_hosts))
            )
        return None

    def check_url(self, url: str) -> None:
        """Raise ``DownloadError`` unless ``url`` is HTTPS on an allowed host."""

        problem = self._url_problem(url)
        if problem is not None:
            raise DownloadError("Refusing to fetch {}: {}.".format(url, problem))

    # -- request plumbing -----------------------------------------------

    def _headers(self) -> dict:
        """Return the headers every request carries."""

        # identity keeps sizes and hashes about the bytes on the wire.
        return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}

    def _classify(self, exc: BaseException, url: str) -> Exception:
        """Map a transport exception to a fatal or retryable failure."""

        inner: BaseException = exc
        if (
            isinstance(exc, urllib.error.URLError)
            and not isinstance(exc, urllib.error.HTTPError)
            and isinstance(exc.reason, BaseException)
        ):
            inner = exc.reason
        host = urllib.parse.urlsplit(url).hostname or url
        if isinstance(inner, ssl.SSLCertVerificationError):
            detail = getattr(inner, "verify_message", None) or str(inner)
            return DownloadError(
                describe_tls_failure(host, detail, self._presented_issuer(url))
            )
        if isinstance(inner, (socket.timeout, TimeoutError)):
            return _RetryableFailure(
                "timed out after {:g} s waiting for {}".format(
                    self.timeout_seconds, host
                )
            )
        if isinstance(inner, ssl.SSLError):
            return _RetryableFailure("TLS error talking to {}: {}".format(host, inner))
        return _RetryableFailure(
            "connection error talking to {}: {}".format(host, inner)
        )

    def _presented_issuer(self, url: str) -> Optional[str]:
        """Ask the transport for the presented certificate issuer, if it can."""

        probe = getattr(self.transport, "presented_issuer", None)
        if probe is None:
            return None
        try:
            issuer = probe(url)
        except Exception:  # diagnostics must never mask the TLS error itself
            return None
        return _sanitize_text(issuer) if issuer else None

    def _call_transport(
        self, url: str, headers: Mapping[str, str]
    ) -> TransportResponse:
        """Send one request through the transport, classifying failures."""

        try:
            return self.transport.open(url, headers, self.timeout_seconds)
        except (OSError, http.client.HTTPException) as exc:
            raise self._classify(exc, url) from exc

    def _read(self, response: TransportResponse, url: str, size: int) -> bytes:
        """Read one body chunk, classifying transport failures."""

        try:
            return response.read(size)
        except (OSError, http.client.HTTPException) as exc:
            raise self._classify(exc, url) from exc

    def _open(self, url: str, headers: Mapping[str, str]) -> TransportResponse:
        """GET ``url``, following only redirects that pass the URL policy."""

        self.check_url(url)
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            response = self._call_transport(current, headers)
            if response.status not in REDIRECT_STATUSES:
                return response
            location = response.header("location")
            status = response.status
            response.close()
            if not location:
                raise DownloadError(
                    "HTTP {} redirect from {} has no Location header.".format(
                        status, current
                    )
                )
            target = urllib.parse.urljoin(current, location.strip())
            problem = self._url_problem(target)
            if problem is not None:
                raise DownloadError(
                    "Refusing redirect from {} to {}: {}.".format(
                        current, target, problem
                    )
                )
            current = target
        raise DownloadError(
            "Too many redirects (more than {}) fetching {}.".format(MAX_REDIRECTS, url)
        )

    def _status_failure(self, response: TransportResponse, url: str) -> Exception:
        """Return the failure for an unexpected HTTP status."""

        status = response.status
        if 500 <= status <= 599:
            return _RetryableFailure("HTTP {} from {}".format(status, url))
        return DownloadError("HTTP {} fetching {}.".format(status, url))

    def _check_encoding(self, response: TransportResponse, url: str) -> None:
        """Refuse compressed bodies: sizes and hashes describe raw bytes."""

        encoding = (response.header("content-encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            raise DownloadError(
                "Refusing {}: unexpected Content-Encoding {!r}.".format(url, encoding)
            )

    def _content_length(self, response: TransportResponse, url: str) -> Optional[int]:
        """Return the declared Content-Length, or None when absent."""

        raw = response.header("content-length")
        if raw is None:
            return None
        text = raw.strip()
        if not text.isdigit() or not text.isascii():
            raise DownloadError("Invalid Content-Length {!r} from {}.".format(raw, url))
        return int(text)

    def _with_retries(self, url: str, attempt: Callable[[], T]) -> T:
        """Run ``attempt``, retrying retryable failures with backoff."""

        failures = 0
        while True:
            try:
                return attempt()
            except _RetryableFailure as exc:
                failures += 1
                if failures > self.retries:
                    raise DownloadError(
                        "Could not fetch {} after {} attempt(s): {}.".format(
                            url, failures, exc
                        )
                    ) from exc
                delay = min(
                    self._backoff_seconds * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS
                )
                self._sleep(delay)

    # -- small reads ----------------------------------------------------

    def get_bytes(self, url: str, max_bytes: int) -> bytes:
        """Return the body of ``url``, refusing anything over ``max_bytes``."""

        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise DownloadError("max_bytes must be a positive integer.")
        return self._with_retries(url, lambda: self._get_bytes_once(url, max_bytes))

    def _get_bytes_once(self, url: str, max_bytes: int) -> bytes:
        """Make one capped GET attempt."""

        response = self._open(url, self._headers())
        try:
            if response.status != 200:
                raise self._status_failure(response, url)
            self._check_encoding(response, url)
            declared = self._content_length(response, url)
            if declared is not None and declared > max_bytes:
                raise DownloadError(
                    "Refusing {}: it is {} bytes, over the {}-byte limit.".format(
                        url, declared, max_bytes
                    )
                )
            chunks = []
            total = 0
            while True:
                # Ask for at most one byte past the cap so an oversize body is
                # detected without buffering much beyond it.
                chunk = self._read(
                    response, url, min(CHUNK_SIZE, max_bytes - total + 1)
                )
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError(
                        "Aborted {}: the body exceeded the {}-byte limit.".format(
                            url, max_bytes
                        )
                    )
                chunks.append(chunk)
            if declared is not None and total < declared:
                raise _RetryableFailure(
                    "truncated body: received {} of {} bytes".format(total, declared)
                )
            if declared is not None and total > declared:
                raise DownloadError(
                    "{} sent {} bytes after declaring {}.".format(url, total, declared)
                )
            return b"".join(chunks)
        finally:
            response.close()

    # -- verified downloads ---------------------------------------------

    def download_to(
        self,
        url: str,
        dest: Path,
        expected_size: int,
        expected_sha256: str,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Download ``url`` into ``dest`` and verify its size and SHA-256.

        An existing smaller ``dest`` is treated as a partial transfer: its
        bytes are re-hashed and the rest is requested with ``Range``. A
        server that ignores ``Range`` (200) or answers inconsistently makes
        the transfer restart from zero. A transfer that exceeds
        ``expected_size`` or ends with the wrong hash discards ``dest``.
        Nothing outside ``dest`` is written or deleted. ``progress`` is
        called with ``(bytes_on_disk, expected_size)``.
        """

        digest = _normalize_sha256(expected_sha256)
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            raise DownloadError("expected_size must be a positive integer.")
        target = Path(dest)
        self.check_url(url)
        if not target.parent.is_dir():
            raise DownloadError(
                "Download directory does not exist: {}".format(target.parent)
            )
        self._existing_size(target)
        state = _DownloadState()
        self._with_retries(
            url,
            lambda: self._download_once(
                url, target, expected_size, digest, progress, state
            ),
        )

    def _existing_size(self, dest: Path) -> int:
        """Return the size of a partial ``dest`` (0 if absent); refuse non-files."""

        try:
            info = os.lstat(str(dest))
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise DownloadError("Cannot inspect {}: {}".format(dest, exc)) from exc
        if not stat.S_ISREG(info.st_mode):
            raise DownloadError(
                "Refusing to download into {}: it is not a regular file.".format(dest)
            )
        return info.st_size

    def _truncate(self, dest: Path) -> None:
        """Empty ``dest`` so the next transfer starts from byte zero."""

        try:
            with open(str(dest), "wb"):
                pass
        except OSError as exc:
            raise DownloadError("Cannot reset {}: {}".format(dest, exc)) from exc

    def _discard(self, dest: Path) -> None:
        """Remove a rejected ``dest``, best effort.

        On Windows an antivirus scanner may briefly hold a new file. A copy
        that survives is harmless: it is re-hashed before any reuse.
        """

        try:
            os.remove(str(dest))
        except FileNotFoundError:
            return
        except OSError:
            try:
                self._truncate(dest)
            except DownloadError:
                pass

    def _hash_existing(self, dest: Path, hasher: _Hasher) -> None:
        """Feed the bytes already in ``dest`` into ``hasher``."""

        try:
            with open(str(dest), "rb") as handle:
                while True:
                    block = handle.read(HASH_CHUNK_SIZE)
                    if not block:
                        break
                    hasher.update(block)
        except OSError as exc:
            raise DownloadError("Cannot read {}: {}".format(dest, exc)) from exc

    def _range_accepted(
        self, response: TransportResponse, url: str, offset: int, size: int
    ) -> str:
        """Classify a reply to a Range request: partial, full, or restart."""

        status = response.status
        if status == 200:
            return "full"
        if status == 416:
            return "restart"
        if status != 206:
            raise self._status_failure(response, url)
        match = _CONTENT_RANGE.fullmatch(
            (response.header("content-range") or "").strip()
        )
        if match is None:
            return "restart"
        first, last, total = int(match.group(1)), int(match.group(2)), match.group(3)
        if first != offset or last != size - 1:
            return "restart"
        if total != "*" and int(total) != size:
            return "restart"
        declared = self._content_length(response, url)
        if declared is not None and declared != size - offset:
            return "restart"
        return "partial"

    def _download_once(
        self,
        url: str,
        dest: Path,
        size: int,
        digest: str,
        progress: Optional[Callable[[int, int], None]],
        state: _DownloadState,
    ) -> None:
        """Make one download attempt, resuming from any partial ``dest``."""

        offset = self._existing_size(dest)
        if offset > size:
            self._truncate(dest)
            offset = 0
        hasher = hashlib.sha256()
        if offset:
            self._hash_existing(dest, hasher)
            if offset == size:
                if hasher.hexdigest() == digest:
                    _report(progress, size, size)
                    return
                self._truncate(dest)
                offset = 0
                hasher = hashlib.sha256()
        headers = self._headers()
        if offset:
            headers["Range"] = "bytes={}-".format(offset)
        response = self._open(url, headers)
        restart = False
        try:
            if offset:
                outcome = self._range_accepted(response, url, offset, size)
                if outcome == "restart":
                    restart = True
                elif outcome == "full":
                    # The server ignored Range; this body is the whole file.
                    self._truncate(dest)
                    offset = 0
                    hasher = hashlib.sha256()
            elif response.status != 200:
                raise self._status_failure(response, url)
            if not restart:
                self._check_encoding(response, url)
                if offset == 0:
                    declared = self._content_length(response, url)
                    if declared is not None and declared != size:
                        raise DownloadError(
                            "{} is {} bytes but the manifest says {}.".format(
                                url, declared, size
                            )
                        )
                written = self._stream(
                    response, url, dest, offset, size, hasher, progress
                )
        finally:
            response.close()
        if restart:
            self._truncate(dest)
            self._download_once(url, dest, size, digest, progress, state)
            return
        if written < size:
            # Keep the partial file: the next attempt resumes from it.
            raise _RetryableFailure(
                "connection closed after {} of {} bytes".format(written, size)
            )
        actual = hasher.hexdigest()
        if actual == digest:
            return
        self._discard(dest)
        if offset and not state.restarted_after_mismatch:
            # The resumed prefix may have been the corrupt part; one clean
            # transfer from zero settles it.
            state.restarted_after_mismatch = True
            self._download_once(url, dest, size, digest, progress, state)
            return
        raise DownloadError(
            "SHA-256 mismatch for {}: expected {}, got {}. The file was "
            "discarded.".format(url, digest, actual)
        )

    def _stream(
        self,
        response: TransportResponse,
        url: str,
        dest: Path,
        offset: int,
        size: int,
        hasher: _Hasher,
        progress: Optional[Callable[[int, int], None]],
    ) -> int:
        """Append the body to ``dest`` from ``offset``; return bytes on disk."""

        written = offset
        oversize = False
        _report(progress, written, size)
        try:
            handle = open(str(dest), "ab" if offset else "wb")
        except OSError as exc:
            raise DownloadError("Cannot write {}: {}".format(dest, exc)) from exc
        with handle:
            while True:
                chunk = self._read(response, url, min(CHUNK_SIZE, size - written + 1))
                if not chunk:
                    break
                if written + len(chunk) > size:
                    oversize = True
                    break
                try:
                    handle.write(chunk)
                except OSError as exc:
                    raise DownloadError(
                        "Cannot write {}: {}".format(dest, exc)
                    ) from exc
                hasher.update(chunk)
                written += len(chunk)
                _report(progress, written, size)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as exc:
                raise DownloadError("Cannot write {}: {}".format(dest, exc)) from exc
        if oversize:
            self._discard(dest)
            raise DownloadError(
                "Aborted download of {}: the server sent more than the "
                "manifest size of {} bytes. The file was discarded.".format(url, size)
            )
        return written


def stderr_progress(
    label: str, stream: Optional[TextIO] = None, step_percent: int = 10
) -> Callable[[int, int], None]:
    """Return a ``download_to`` progress callback printing coarse steps to stderr."""

    out = sys.stderr if stream is None else stream
    step = max(1, min(100, int(step_percent)))
    last = -1

    def report(done: int, total: int) -> None:
        """Print one line each time progress crosses another step."""

        nonlocal last
        if total <= 0:
            return
        percent = min(100, done * 100 // total)
        bucket = 100 if percent == 100 else percent - percent % step
        if bucket <= last:
            return
        last = bucket
        print(
            "{}: {:3d}% ({:.1f} of {:.1f} MiB)".format(
                label, bucket, done / 1048576.0, total / 1048576.0
            ),
            file=out,
            flush=True,
        )

    return report
