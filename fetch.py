"""SSRF-hardened HTTP image fetcher backing ``cdn.import_from_url``.

This is the *only* outbound-HTTP path in the CDN and it fetches
attacker-influenced URLs (e.g. an OAuth provider avatar URL that a
registering user can point anywhere). It is therefore a classic
Server-Side Request Forgery (SSRF) sink and is hardened accordingly — see
``docs/security-programme.md`` for the threat-model row.

Every network step is guarded, and each guard has a dedicated test:

* **https-only** — ``http://``/``file://``/anything else is rejected up front
  and again on every redirect hop.
* **DNS → IP allowlisting** — the hostname is resolved once with
  ``socket.getaddrinfo`` and *all* returned addresses are validated against
  the forbidden ranges (private RFC1918/ULA, loopback, link-local incl. the
  169.254.169.254 cloud-metadata endpoint, multicast, reserved, unspecified,
  CGNAT ``100.64.0.0/10``). IPv4-mapped, 6to4, and NAT64 (``64:ff9b::/96``)
  IPv6 forms are unwrapped to their embedded IPv4 address before validation,
  so a private/metadata address cannot sneak past the allowlist re-encoded
  as IPv6. If *any* resolved address is forbidden the whole fetch is
  refused — a name that answers with a mix of public and private records is
  treated as hostile, not as "pick the good one".
* **Anti-DNS-rebinding via IP pinning** — we resolve once, validate, then
  open the TCP connection to *that exact validated IP* while presenting the
  original hostname for TLS SNI / certificate verification and the ``Host``
  header. There is no second name lookup between the check and the connect,
  so the classic rebinding race (validate a public A record, connect after
  the record flips to 127.0.0.1) cannot happen. See :func:`_open`.
* **Redirects are never trusted** — auto-follow is disabled; we drive the
  redirect loop ourselves, re-running the *full* scheme + DNS + IP
  validation for every ``Location`` before connecting, and cap the hop
  count.
* **size cap while streaming** — the body is read in bounded chunks and the
  transfer is aborted the instant it crosses the cap, *before* the bytes are
  buffered whole in memory (defends against a decompression/oversize DoS and
  a ``Content-Length``-lying server alike).
* **timeout** on connect and read.
* **content verification** — the declared ``Content-Type`` must be
  ``image/*`` and the bytes must decode as a real image whose detected
  format maps to an allowed extension (magic-byte check via Pillow, routed
  through the same ``validate_image_file`` / ``ALLOWED_IMAGE_EXTENSIONS``
  the upload endpoints use). The URL's own extension is never trusted.

Failures raise :class:`ImageImportError` with a stable, machine-readable
``.code``. The fetcher never fails *open*: there is no code path that
returns a ref for an unvalidated address.
"""
from __future__ import annotations

import http.client
import ipaddress
import logging
import socket
import ssl
import time
from io import BytesIO
from urllib.parse import urljoin, urlsplit

from PIL import Image as PILImage

from .conf import cdn_settings

logger = logging.getLogger(__name__)

# Read the body in bounded chunks so the size cap can abort mid-stream.
_CHUNK = 64 * 1024

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Trust the *detected* image format (magic bytes), not the URL/Content-Type,
# to choose the stored extension. Only formats whose extension survives the
# ALLOWED_IMAGE_EXTENSIONS allowlist below are kept.
_PIL_FORMAT_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "HEIF": ".heic",
    "HEIC": ".heic",
    "MPO": ".jpg",  # multi-picture JPEG (some phone cameras)
}


class ImageImportError(Exception):
    """Structured failure of an import-from-URL attempt.

    ``code`` is a stable machine token (``scheme_not_https``, ``blocked_ip``,
    ``too_large``, ...) suitable for logging/metrics; the message adds
    human context. Callers on security paths must treat this as fatal — it
    is deliberately never converted into a fail-open default.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


# --------------------------------------------------------------------------- #
# Configuration accessors
# --------------------------------------------------------------------------- #
def _timeout() -> float:
    return float(cdn_settings.IMPORT_FROM_URL_TIMEOUT)


def _max_bytes() -> int:
    return int(cdn_settings.IMPORT_FROM_URL_MAX_BYTES)


def _max_redirects() -> int:
    return int(cdn_settings.IMPORT_FROM_URL_MAX_REDIRECTS)


# --------------------------------------------------------------------------- #
# IP / DNS validation
# --------------------------------------------------------------------------- #
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


def _nat64_embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract the embedded IPv4 address from a NAT64 (RFC 6052) address.

    Only the *well-known* prefix ``64:ff9b::/96`` is unwrapped — this is the
    prefix a NAT64/DNS64 resolver synthesizes AAAA records under for
    IPv4-only names. ``ipaddress.IPv6Address.ipv4_mapped``/``.sixtofour``
    have no idea this prefix exists, so e.g. ``64:ff9b::a9fe:a9fe`` (NAT64
    encoding of the 169.254.169.254 cloud-metadata address) reads as a
    perfectly ordinary global IPv6 address to ``is_global`` unless it is
    unwrapped here first, same as the mapped/6to4 forms above it.
    """
    if ip in _NAT64_WELL_KNOWN_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _ip_is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    """True if *ip* is anything but a normal, routable public address.

    Unwraps IPv4-mapped/6to4/NAT64 IPv6 forms first so an attacker cannot
    smuggle ``169.254.169.254`` in as ``::ffff:a9fe:a9fe`` or
    ``64:ff9b::a9fe:a9fe``. ``is_global`` alone would reject all of these
    *if* it recognized them as embedded IPv4, but it does not know the
    NAT64 prefix at all, so that one is unwrapped explicitly. The
    RFC1918/loopback/etc. flags below are spelled out for auditability and
    because ``is_global`` semantics have shifted across CPython versions;
    CGNAT (RFC 6598, ``100.64.0.0/10``) is likewise checked explicitly
    rather than trusted to ``is_global`` alone, for the same reason.
    """
    if ip.version == 6:
        mapped = (
            getattr(ip, "ipv4_mapped", None)
            or getattr(ip, "sixtofour", None)
            or _nat64_embedded_ipv4(ip)
        )
        if mapped is not None:
            ip = mapped

    return (
        not ip.is_global
        or ip.is_private          # RFC1918 v4, ULA fc00::/7 v6
        or ip.is_loopback         # 127.0.0.0/8, ::1
        or ip.is_link_local       # 169.254.0.0/16 (incl. metadata), fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified      # 0.0.0.0, ::
        or (ip.version == 4 and ip in _CGNAT_RANGE)  # RFC 6598 CGNAT shared space
    )


def _resolve_validated(host: str, port: int) -> ipaddress._BaseAddress:
    """Resolve *host* and validate every answer; return the first IP to pin.

    Raises :class:`ImageImportError` if resolution fails or *any* returned
    address is forbidden. Returning the validated address (rather than the
    hostname) is what lets the caller pin the connection and defeat DNS
    rebinding.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ImageImportError("dns_resolution_failed", f"cannot resolve {host!r}: {exc}") from exc

    if not infos:
        raise ImageImportError("dns_resolution_failed", f"no addresses for {host!r}")

    first: ipaddress._BaseAddress | None = None
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:  # pragma: no cover - getaddrinfo returns valid literals
            raise ImageImportError("dns_resolution_failed", str(exc)) from exc
        if _ip_is_forbidden(ip):
            raise ImageImportError(
                "blocked_ip",
                f"{host!r} resolves to non-public address {ip}",
            )
        if first is None:
            first = ip
    assert first is not None
    return first


# --------------------------------------------------------------------------- #
# Pinned single-hop request (the anti-rebinding connect)
# --------------------------------------------------------------------------- #
def _open(host: str, ip: ipaddress._BaseAddress, port: int, path: str) -> http.client.HTTPResponse:
    """Open one HTTPS request to the *validated* ``ip`` for ``host``.

    The TCP connection targets ``ip`` directly (no second DNS lookup), while
    TLS SNI + certificate validation and the ``Host`` header use the real
    hostname — so a valid public cert is still required and rebinding gains
    the attacker nothing. Kept as a small seam so tests can substitute the
    network with a fake response.
    """
    timeout = _timeout()
    raw = socket.create_connection((str(ip), port), timeout=timeout)
    try:
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise

    conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    conn.sock = sock
    conn.request(
        "GET",
        path or "/",
        headers={
            "Host": host,
            "User-Agent": "stapel-cdn-import/1.0",
            "Accept": "image/*",
        },
    )
    return conn.getresponse()


# --------------------------------------------------------------------------- #
# Redirect-driving fetch loop
# --------------------------------------------------------------------------- #
def fetch_image_bytes(url: str) -> bytes:
    """Fetch *url* as raw image bytes under the full SSRF hardening.

    Every hop (initial URL and each redirect target) is independently
    validated: https-only, DNS resolved and all IPs allowlisted, connection
    pinned to a validated IP. The body is streamed with a hard size cap.
    Returns the raw bytes; content/format validation is the caller's job.
    """
    current = url
    hops = 0
    cap = _max_bytes()

    while True:
        parsed = urlsplit(current)
        if parsed.scheme != "https":
            raise ImageImportError(
                "scheme_not_https", f"refusing non-https scheme {parsed.scheme!r}"
            )
        host = parsed.hostname
        if not host:
            raise ImageImportError("no_host", "url has no host")
        port = parsed.port or 443

        ip = _resolve_validated(host, port)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        resp = _open(host, ip, port, path)
        try:
            status = resp.status

            if status in _REDIRECT_STATUSES:
                location = resp.getheader("Location")
                if not location:
                    raise ImageImportError("redirect_no_location", f"{status} without Location")
                hops += 1
                if hops > _max_redirects():
                    raise ImageImportError(
                        "too_many_redirects", f"exceeded {_max_redirects()} redirects"
                    )
                # Re-loop: the new URL is fully re-validated (scheme + DNS +
                # IP) before any connection is made to it.
                current = urljoin(current, location)
                continue

            if status != 200:
                raise ImageImportError("bad_status", f"upstream returned HTTP {status}")

            content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                raise ImageImportError(
                    "not_image_content_type", f"Content-Type {content_type!r} is not image/*"
                )

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    # Abort mid-stream — never buffer an oversize body whole.
                    raise ImageImportError(
                        "too_large", f"body exceeds {cap} byte cap"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            resp.close()


# --------------------------------------------------------------------------- #
# Content verification
# --------------------------------------------------------------------------- #
def detect_image_extension(data: bytes) -> str:
    """Decode *data* with Pillow and map the detected format to an allowed
    extension. This is the magic-byte gate: the URL/Content-Type is never
    trusted to name the format.

    Raises :class:`ImageImportError('not_an_image')` if the bytes do not
    decode as an image, or ``('unsupported_image_format')`` if the real
    format is not in ``ALLOWED_IMAGE_EXTENSIONS``.
    """
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:  # pragma: no cover - optional dependency
        pass

    try:
        with PILImage.open(BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            img.verify()  # decode-verify: rejects truncated/hostile payloads
    except Exception as exc:
        raise ImageImportError("not_an_image", f"payload is not a decodable image: {exc}") from exc

    ext = _PIL_FORMAT_EXT.get(fmt)
    allowed = {e.lower() for e in cdn_settings.ALLOWED_IMAGE_EXTENSIONS}
    if ext is None or ext not in allowed:
        raise ImageImportError(
            "unsupported_image_format", f"detected format {fmt!r} is not allowed"
        )
    return ext


# --------------------------------------------------------------------------- #
# Per-caller rate limit (fixed window, Django cache) — open-proxy defence
# --------------------------------------------------------------------------- #
def enforce_rate_limit(caller: str | None) -> None:
    """Fixed-window per-caller quota for import-from-URL.

    Mirrors ``stapel_core.gateway.ratelimit.CacheRateLimiter`` (atomic
    ``add`` + ``incr`` on the Django cache). A comm Function has no ambient
    caller identity, so the caller is passed explicitly (profiles passes the
    registering user's id); calls without one share a single ``-`` bucket.
    This is what stops the function from being turned into an open HTTP proxy
    / amplifier. Checked *before* any DNS or network work.
    """
    from stapel_core.gateway.ratelimit import parse_rate

    limit, window = parse_rate(cdn_settings.IMPORT_FROM_URL_RATE)
    from django.core.cache import cache

    bucket = int(time.time() // window)
    key = f"stapel:cdn:import_from_url:{caller or '-'}:{bucket}"
    cache.add(key, 0, timeout=window * 2)
    try:
        count = cache.incr(key)
    except ValueError:  # add/incr raced with expiry
        cache.add(key, 0, timeout=window * 2)
        count = cache.incr(key)
    if count > limit:
        raise ImageImportError("rate_limited", f"import-from-url quota {limit}/{window}s exceeded")
