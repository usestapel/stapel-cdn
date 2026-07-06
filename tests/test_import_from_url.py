"""SSRF-hardening tests for the ``cdn.import_from_url`` comm Function.

Each defensive control has an adversarial test here: forbidden IPs (private,
loopback, cloud metadata), redirect-to-private, a DNS-rebinding-style second
resolution into a private range, https-only enforcement, oversize-abort,
non-image magic bytes, plus a happy path that really saves to the test
storage and returns an honest ``<type>/<hash>`` ref.

The network is faked at two seams:
  * ``socket.getaddrinfo`` — controls what a hostname resolves to.
  * ``stapel_cdn.fetch._open`` — returns a fake HTTP response instead of a
    real pinned TLS connection, so the redirect/streaming/status logic is
    exercised without egress.
"""
import hashlib
import io
import socket

import pytest
from PIL import Image as PILImage
from stapel_core.comm import call, function_registry
from stapel_core.comm.exceptions import FunctionCallError

from stapel_cdn import fetch
from stapel_cdn.fetch import (
    ImageImportError,
    detect_image_extension,
    fetch_image_bytes,
)
from stapel_cdn.models import Image


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _png_bytes(color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def _addrinfo(ip: str, port: int = 443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]


class FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse."""

    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._buf = io.BytesIO(body)

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


@pytest.fixture
def public_dns(monkeypatch):
    """All hostnames resolve to a single public IP unless overridden."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_function_registered():
    assert "cdn.import_from_url" in function_registry.names()


# --------------------------------------------------------------------------- #
# Scheme enforcement
# --------------------------------------------------------------------------- #
class TestSchemeEnforcement:
    def test_http_rejected(self):
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("http://example.com/a.png")
        assert exc.value.code == "scheme_not_https"

    def test_file_scheme_rejected(self):
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("file:///etc/passwd")
        assert exc.value.code == "scheme_not_https"


# --------------------------------------------------------------------------- #
# Forbidden IP ranges (DNS → IP allowlist)
# --------------------------------------------------------------------------- #
class TestForbiddenIPs:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",        # loopback
            "10.0.0.5",         # RFC1918
            "192.168.1.10",     # RFC1918
            "172.16.9.9",       # RFC1918
            "169.254.169.254",  # cloud metadata / link-local
            "0.0.0.0",          # unspecified
            "::1",              # v6 loopback
            "fd00::1",          # v6 ULA (is_private)
            "fe80::1",          # v6 link-local
        ],
    )
    def test_private_or_special_ip_rejected(self, monkeypatch, ip):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo(ip))
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://evil.example/a.png")
        assert exc.value.code == "blocked_ip"

    def test_ipv4_mapped_metadata_rejected(self, monkeypatch):
        # ::ffff:169.254.169.254 must not slip past by v6-encoding the v4 metadata IP.
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **k: _addrinfo("::ffff:a9fe:a9fe")
        )
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://evil.example/a.png")
        assert exc.value.code == "blocked_ip"

    def test_mixed_public_and_private_answers_rejected(self, monkeypatch):
        # A name answering with BOTH a public and a private record is hostile.
        infos = _addrinfo("93.184.216.34") + _addrinfo("10.1.2.3")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://evil.example/a.png")
        assert exc.value.code == "blocked_ip"


# --------------------------------------------------------------------------- #
# Anti-rebinding: connection is pinned to the validated IP
# --------------------------------------------------------------------------- #
class TestIpPinning:
    def test_connect_targets_validated_ip_not_hostname(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))
        seen = {}

        def fake_open(host, ip, port, path):
            seen["host"] = host
            seen["ip"] = str(ip)
            return FakeResponse(200, {"Content-Type": "image/png"}, _png_bytes())

        monkeypatch.setattr(fetch, "_open", fake_open)
        fetch_image_bytes("https://host.example/a.png")
        # The TCP target is the pre-validated IP; the hostname rides along only
        # for TLS SNI / Host — so a post-check DNS flip cannot redirect us.
        assert seen["ip"] == "93.184.216.34"
        assert seen["host"] == "host.example"

    def test_rebinding_second_resolution_into_private_is_caught(self, monkeypatch):
        # First hop resolves public and 301-redirects; the redirect target
        # re-resolves into a private range — the per-hop re-validation catches it.
        resolutions = iter([_addrinfo("93.184.216.34"), _addrinfo("10.0.0.7")])
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: next(resolutions))

        def fake_open(host, ip, port, path):
            return FakeResponse(
                302, {"Location": "https://internal.example/secret"}, b""
            )

        monkeypatch.setattr(fetch, "_open", fake_open)
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "blocked_ip"


# --------------------------------------------------------------------------- #
# Redirects
# --------------------------------------------------------------------------- #
class TestRedirects:
    def test_redirect_to_private_rejected(self, monkeypatch):
        resolutions = iter([_addrinfo("93.184.216.34"), _addrinfo("127.0.0.1")])
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: next(resolutions))
        monkeypatch.setattr(
            fetch,
            "_open",
            lambda *a: FakeResponse(301, {"Location": "https://localhost/x"}, b""),
        )
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "blocked_ip"

    def test_redirect_to_http_rejected(self, monkeypatch, public_dns):
        monkeypatch.setattr(
            fetch,
            "_open",
            lambda *a: FakeResponse(302, {"Location": "http://public.example/x"}, b""),
        )
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "scheme_not_https"

    def test_redirect_cap_enforced(self, monkeypatch, public_dns):
        monkeypatch.setattr(
            fetch,
            "_open",
            lambda *a: FakeResponse(302, {"Location": "https://public.example/loop"}, b""),
        )
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "too_many_redirects"

    def test_redirect_without_location_rejected(self, monkeypatch, public_dns):
        monkeypatch.setattr(fetch, "_open", lambda *a: FakeResponse(302, {}, b""))
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "redirect_no_location"


# --------------------------------------------------------------------------- #
# Streaming size cap
# --------------------------------------------------------------------------- #
class TestSizeCap:
    def test_oversize_body_aborted(self, monkeypatch, public_dns, settings):
        settings.STAPEL_CDN = {"IMPORT_FROM_URL_MAX_BYTES": 1024}
        big = b"\x89PNG\r\n\x1a\n" + b"A" * 5000
        monkeypatch.setattr(
            fetch,
            "_open",
            lambda *a: FakeResponse(200, {"Content-Type": "image/png"}, big),
        )
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/big.png")
        assert exc.value.code == "too_large"


# --------------------------------------------------------------------------- #
# Content-Type / magic-byte checks
# --------------------------------------------------------------------------- #
class TestContentChecks:
    def test_non_image_content_type_rejected(self, monkeypatch, public_dns):
        monkeypatch.setattr(
            fetch,
            "_open",
            lambda *a: FakeResponse(200, {"Content-Type": "text/html"}, b"<html>"),
        )
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "not_image_content_type"

    def test_bad_status_rejected(self, monkeypatch, public_dns):
        monkeypatch.setattr(fetch, "_open", lambda *a: FakeResponse(404, {}, b""))
        with pytest.raises(ImageImportError) as exc:
            fetch_image_bytes("https://public.example/a.png")
        assert exc.value.code == "bad_status"

    def test_detect_rejects_non_image_bytes(self):
        with pytest.raises(ImageImportError) as exc:
            detect_image_extension(b"this is not an image")
        assert exc.value.code == "not_an_image"

    def test_detect_maps_png(self):
        assert detect_image_extension(_png_bytes()) == ".png"


# --------------------------------------------------------------------------- #
# End-to-end via comm call, with real storage save
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestImportEndToEnd:
    def _wire_happy(self, monkeypatch, body):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))
        monkeypatch.setattr(
            fetch,
            "_open",
            lambda *a: FakeResponse(200, {"Content-Type": "image/png"}, body),
        )

    def test_happy_path_saves_and_returns_ref(self, monkeypatch):
        body = _png_bytes()
        self._wire_happy(monkeypatch, body)

        result = call(
            "cdn.import_from_url",
            {"url": "https://cdn.provider.example/pic.png", "image_type": "avatar", "caller": "u1"},
        )

        expected_hash = hashlib.sha256(body).hexdigest()
        assert result == {"ref": f"avatar/{expected_hash}"}
        img = Image.objects.get(file_hash=expected_hash, type="avatar")
        assert img.file_extension == ".png"
        assert img.original_size == len(body)

    def test_dedup_returns_same_ref(self, monkeypatch):
        body = _png_bytes(color=(1, 2, 3))
        self._wire_happy(monkeypatch, body)
        payload = {
            "url": "https://cdn.provider.example/pic.png",
            "image_type": "avatar",
            "caller": "u2",
        }
        r1 = call("cdn.import_from_url", payload)
        r2 = call("cdn.import_from_url", payload)
        assert r1 == r2
        assert Image.objects.filter(file_hash=hashlib.sha256(body).hexdigest()).count() == 1

    def test_invalid_image_type_rejected(self, monkeypatch):
        self._wire_happy(monkeypatch, _png_bytes())
        with pytest.raises(FunctionCallError):
            call(
                "cdn.import_from_url",
                {"url": "https://x.example/a.png", "image_type": "bogus"},
            )

    def test_private_ip_surfaces_as_function_call_error(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("10.0.0.9"))
        with pytest.raises(FunctionCallError):
            call(
                "cdn.import_from_url",
                {"url": "https://evil.example/a.png", "image_type": "avatar"},
            )


# --------------------------------------------------------------------------- #
# Rate limiting (open-proxy defence)
# --------------------------------------------------------------------------- #
class TestRateLimit:
    def test_caller_exceeding_quota_is_blocked(self, settings):
        settings.STAPEL_CDN = {"IMPORT_FROM_URL_RATE": "2/h"}
        fetch.enforce_rate_limit("caller-a")
        fetch.enforce_rate_limit("caller-a")
        with pytest.raises(ImageImportError) as exc:
            fetch.enforce_rate_limit("caller-a")
        assert exc.value.code == "rate_limited"

    def test_separate_callers_have_separate_buckets(self, settings):
        settings.STAPEL_CDN = {"IMPORT_FROM_URL_RATE": "1/h"}
        fetch.enforce_rate_limit("caller-x")
        fetch.enforce_rate_limit("caller-y")  # different bucket, must not raise
        with pytest.raises(ImageImportError):
            fetch.enforce_rate_limit("caller-x")
