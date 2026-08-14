"""What every intake path refuses before it stores anything.

Each upload view is supposed to answer three questions before the bytes reach
storage: is it small enough, is it a shape this deployment accepts, and do the
actual leading bytes agree. This module pins the answers that were missing.

The video path had none of the three: the whole body was read and SHA-256'd
first, the only ceiling behind that was the per-owner byte quota (which a
deployment may switch off), and it was the single intake that never sniffed —
while the endpoint's own OpenAPI description told callers there was a 100MB
limit. The generic path had a MIME allowlist a caller could opt out of by
simply not sending a Content-Type. And with no decoder installed the image
path degraded to a magic-byte signature, storing bytes it could not verify.
"""
from io import BytesIO

import pytest
from PIL import Image as PILImage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from stapel_core.django.users.models import User

from stapel_cdn.conf import cdn_settings
from stapel_cdn.models import File, Image, Video

MP4_HEADER = b"\x00\x00\x00\x1cftypisom"


@pytest.fixture
def uploader(db):
    return User.objects.create_user(
        username="intake-uploader",
        email="intake@example.com",
        password="pw-not-for-production",
    )


@pytest.fixture
def client(uploader):
    api = APIClient()
    api.force_authenticate(user=uploader)
    return api


def video_of(content, name="clip.mp4", content_type="video/mp4"):
    return SimpleUploadedFile(name=name, content=content, content_type=content_type)


def image_bytes(color="red"):
    buffer = BytesIO()
    PILImage.new("RGB", (32, 24), color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The video path: a size cap that exists, and a byte sniff
# ---------------------------------------------------------------------------


class TestVideoSizeCapIsConfigured:
    def test_shipped_default_matches_the_documented_limit(self):
        """The endpoint description has always claimed 100MB. Now so does the gate."""
        assert cdn_settings.MAX_VIDEO_SIZE == 100 * 1024 * 1024

    def test_cap_is_overridable_through_the_namespace(self):
        with override_settings(STAPEL_CDN={"MAX_VIDEO_SIZE": 4096}):
            assert cdn_settings.MAX_VIDEO_SIZE == 4096
        assert cdn_settings.MAX_VIDEO_SIZE == 100 * 1024 * 1024


@pytest.mark.django_db
class TestVideoUploadIsBounded:
    url = "/cdn/api/v1/upload/video/"

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_VIDEO_SIZE": 1024})
    def test_oversized_video_is_refused(self, client):
        oversized = video_of(MP4_HEADER + b"\x00" * 4096)
        response = client.post(self.url, {"file": oversized}, format="multipart")

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        assert not Video.objects.exists()

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_VIDEO_SIZE": 1024})
    def test_oversized_video_is_refused_before_it_is_hashed(self, client, monkeypatch):
        """Hashing reads the whole body; a cap consulted after it bounds nothing."""
        calls = []
        monkeypatch.setattr(
            Video, "calculate_file_hash",
            classmethod(lambda cls, f: calls.append(f) or "0" * 64),
        )

        response = client.post(
            self.url, {"file": video_of(MP4_HEADER + b"\x00" * 4096)},
            format="multipart",
        )

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        assert calls == [], "the body was hashed before the size cap was consulted"

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_VIDEO_SIZE": 1024 * 1024})
    def test_a_video_under_the_cap_still_uploads(self, client):
        response = client.post(
            self.url, {"file": video_of(MP4_HEADER + b"real enough")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.django_db
class TestVideoUploadSniffsTheBytes:
    """A `.mp4` name says nothing; the media origin runs what it is handed."""

    url = "/cdn/api/v1/upload/video/"

    @pytest.mark.parametrize(
        "payload",
        [
            b"<html><body><script>fetch('/steal')</script></body></html>",
            b"<!DOCTYPE html>\n<html></html>",
            b"  \n\t<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>",
            b"\xef\xbb\xbf<script>alert(1)</script>",
            b"#!/bin/sh\nrm -rf /\n",
        ],
    )
    def test_active_content_under_a_video_name_is_refused(self, client, payload):
        response = client.post(
            self.url, {"file": video_of(payload, "payload.mp4")}, format="multipart"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not Video.objects.exists()


class TestUnknownSizeDoesNotSkipTheCap:
    """``size`` of ``None`` is not evidence of a small file."""

    def test_image_upload_with_no_declared_size_is_refused(self):
        from stapel_cdn.views import _validate_image_upload

        upload = SimpleUploadedFile(
            name="photo.jpg", content=image_bytes(), content_type="image/jpeg"
        )
        # A handle that cannot state its size: the old cap read
        # `if uploaded_file.size and ...`, so this walked straight past it.
        upload.size = None

        error = _validate_image_upload(upload)
        assert error is not None
        assert error.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE

    def test_video_upload_with_no_declared_size_is_refused(self):
        from stapel_cdn.views import _validate_video_upload

        upload = video_of(MP4_HEADER + b"payload")
        upload.size = None

        error = _validate_video_upload(upload)
        assert error is not None
        assert error.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


# ---------------------------------------------------------------------------
# The generic path: the MIME allowlist is not opt-out-able
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGenericUploadRequiresAContentType:
    url = "/cdn/api/v1/upload/file/"

    def test_missing_content_type_is_refused(self, client):
        """The allowlist only ran when the caller volunteered a value."""
        # An empty Content-Type is what a client that omits the part header
        # produces; the check used to be `if content_type and ...`, so this
        # skipped the allowlist entirely.
        upload = SimpleUploadedFile(
            name="notes.pdf", content=b"%PDF-1.4 hello", content_type=""
        )
        response = client.post(self.url, {"file": upload}, format="multipart")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not File.objects.exists()

    def test_a_declared_allowed_type_still_uploads(self, client):
        upload = SimpleUploadedFile(
            name="notes.pdf", content=b"%PDF-1.4 hello", content_type="application/pdf"
        )
        response = client.post(self.url, {"file": upload}, format="multipart")
        assert response.status_code == status.HTTP_201_CREATED


class TestOctetStreamIsNotShippedAsAllowed:
    def test_default_mime_allowlist_has_no_universal_type(self):
        """`application/octet-stream` is any client's word for "anything"."""
        assert "application/octet-stream" not in cdn_settings.ALLOWED_FILE_MIME_TYPES

    def test_a_host_can_opt_back_in(self):
        with override_settings(
            STAPEL_CDN={"ALLOWED_FILE_MIME_TYPES": ("application/octet-stream",)}
        ):
            assert "application/octet-stream" in cdn_settings.ALLOWED_FILE_MIME_TYPES


@pytest.mark.django_db
class TestOctetStreamUploadIsRefusedByDefault:
    url = "/cdn/api/v1/upload/file/"

    def test_declaring_the_universal_type_no_longer_passes_the_gate(self, client):
        upload = SimpleUploadedFile(
            name="archive.zip",
            content=b"PK\x03\x04 not really",
            content_type="application/octet-stream",
        )
        response = client.post(self.url, {"file": upload}, format="multipart")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not File.objects.exists()

    @override_settings(STAPEL_CDN={
        "ASSET_TYPES": ("avatar", "product"),
        "ALLOWED_FILE_MIME_TYPES": ("application/zip", "application/octet-stream"),
    })
    def test_the_opt_in_is_honoured(self, client):
        upload = SimpleUploadedFile(
            name="archive.zip",
            content=b"PK\x03\x04 not really",
            content_type="application/octet-stream",
        )
        response = client.post(self.url, {"file": upload}, format="multipart")
        assert response.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# The image path: no decoder means no storage, unless a host says otherwise
# ---------------------------------------------------------------------------


@pytest.fixture
def no_decoder(monkeypatch):
    """A deployment with no libvips at all — checks.E001 is red in it."""
    from stapel_cdn import decoders

    monkeypatch.setattr(decoders, "_pyvips", lambda: None)


@pytest.mark.django_db
class TestImageStorageRequiresADecoder:
    url = "/cdn/api/v1/upload/image/"

    def test_shipped_default_requires_one(self):
        assert cdn_settings.REQUIRE_DECODER is True

    def test_upload_is_refused_when_nothing_can_decode(self, client, no_decoder):
        """Without a decoder the pixel-bomb cap and the decode never run at all."""
        response = client.post(
            self.url,
            {"file": SimpleUploadedFile("photo.jpg", image_bytes(), "image/jpeg")},
            format="multipart",
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert not Image.objects.exists()

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "REQUIRE_DECODER": False})
    def test_a_host_can_opt_into_passthrough_storage(self, client, no_decoder):
        """The documented passthrough posture stays reachable — explicitly."""
        response = client.post(
            self.url,
            {"file": SimpleUploadedFile("photo.jpg", image_bytes(), "image/jpeg")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED

    def test_a_decoder_present_is_unaffected(self, client):
        response = client.post(
            self.url,
            {"file": SimpleUploadedFile("photo.jpg", image_bytes("blue"), "image/jpeg")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED
