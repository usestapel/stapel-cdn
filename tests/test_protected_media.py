"""With a watermark configured, only marked renditions are public.

The original and the clean copies live in the protected tree and are reached
through a signed link (internal readers) or the uploader's own download.
"""
import os
from io import BytesIO
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image as PILImage
from rest_framework.test import APIClient
from stapel_core.comm import call
from stapel_core.django.users.models import User

from stapel_cdn.models import Image
from stapel_cdn.protected import (
    original_is_protected,
    protect_original,
    protected_prefix,
    signed_url,
)
from stapel_cdn.services import ImageProcessingService

pytest.importorskip("pyvips")

SITES = {"sites": [{"host": "primary.example", "primary": True,
                    "brand": {"key": "acme", "name": "Acme"}}]}


@pytest.fixture
def mark_png(tmp_path):
    path = tmp_path / "mark.png"
    PILImage.new("RGBA", (200, 50), (255, 255, 255, 128)).save(path)
    return str(path)


@pytest.fixture
def conf(mark_png, tmp_path, settings):
    from stapel_core.sites import reset_sites_cache

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.STAPEL_SITES = SITES
    settings.ALLOWED_HOSTS = ["*"]
    settings.STAPEL_CDN = {
        "ASSET_TYPES": ("avatar", "product"),
        "WATERMARKS": {"acme": {"PATH": mark_png}},
        "WATERMARK_ASSET_TYPES": ("product",),
    }
    reset_sites_cache()
    yield settings
    reset_sites_cache()


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="owner", email="o@example.com", password="x")


@pytest.fixture
def stranger(db):
    return User.objects.create_user(username="stranger", email="s@example.com", password="x")


def _jpeg(width=1600, height=1200):
    buf = BytesIO()
    PILImage.new("RGB", (width, height), (0, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def uploaded(conf, owner):
    """An upload on the watermarked site, with its previews rendered."""
    client = APIClient()
    client.force_authenticate(user=owner)
    with patch("stapel_cdn.tasks.process_image_async.delay"):
        response = client.post(
            "/cdn/api/v1/images/product/upload/",
            {"file": SimpleUploadedFile("photo.jpg", _jpeg(), content_type="image/jpeg")},
            format="multipart",
            HTTP_HOST="primary.example",
        )
    assert response.status_code == 201, response.content
    image = Image.objects.get()
    ImageProcessingService.generate_previews_only(image)
    image.refresh_from_db()
    return image


def _path_of(url):
    """The file a /media/ URL would map to on disk."""
    from django.conf import settings

    return os.path.join(settings.MEDIA_ROOT, url[len(settings.MEDIA_URL):])


@pytest.mark.django_db
class TestUploadAndPublicSnapshot:
    def test_upload_records_site_and_protects_the_original(self, uploaded):
        assert uploaded.site_key == "acme"
        assert original_is_protected(uploaded)
        assert uploaded.original.name.startswith(protected_prefix() + "product/")

    def test_public_describe_exposes_only_marked_renditions(self, uploaded):
        snapshot = call("cdn.describe", {"ref": f"product/{uploaded.file_hash}"})
        urls = [v["url"] for v in snapshot["variants"]]
        assert not any(v["tier"] == "original" for v in snapshot["variants"])
        assert not any("clean_url" in v for v in snapshot["variants"])
        assert not any(protected_prefix() in u for u in urls)
        marked = [v for v in snapshot["variants"] if v.get("watermarked")]
        assert marked and all(os.path.isfile(_path_of(v["url"])) for v in marked)

    def test_upload_response_points_the_owner_at_the_download(self, conf, owner):
        client = APIClient()
        client.force_authenticate(user=owner)
        with patch("stapel_cdn.tasks.process_image_async.delay"):
            body = client.post(
                "/cdn/api/v1/images/product/upload/",
                {"file": SimpleUploadedFile("p.jpg", _jpeg(800, 600), content_type="image/jpeg")},
                format="multipart",
                HTTP_HOST="primary.example",
            ).json()
        assert "/original/" in str(body)
        assert protected_prefix() not in str(body)


@pytest.mark.django_db
class TestInternalReaders:
    def test_clean_describe_signs_clean_copies_that_serve(self, uploaded):
        snapshot = call(
            "cdn.describe", {"ref": f"product/{uploaded.file_hash}", "clean": True}
        )
        marked = [v for v in snapshot["variants"] if v.get("watermarked")]
        assert marked and all(v["clean_url"].startswith("/cdn/api/v1/media/signed/")
                              for v in marked)
        original = [v for v in snapshot["variants"] if v["tier"] == "original"]
        assert original and "/media/signed/" in original[0]["url"]

        anonymous = APIClient()
        response = anonymous.get(marked[0]["clean_url"])
        assert response.status_code == 200
        assert response["Cache-Control"] == "private, no-store"
        assert b"".join(response.streaming_content)[:4] == b"RIFF"

    def test_forged_or_expired_token_is_refused(self, uploaded, conf):
        anonymous = APIClient()
        assert anonymous.get("/cdn/api/v1/media/signed/nonsense/").status_code == 404
        link = signed_url(uploaded.original.name)
        conf.STAPEL_CDN = {**conf.STAPEL_CDN, "SIGNED_MEDIA_TTL_SECONDS": -1}
        assert anonymous.get(link).status_code == 404

    def test_a_token_cannot_name_a_path_outside_the_protected_tree(self, uploaded):
        link = signed_url(f"product/{uploaded.file_hash}/1080w.webp")
        assert APIClient().get(link).status_code == 404
        link = signed_url(f"{protected_prefix()}../../etc/passwd")
        assert APIClient().get(link).status_code == 404


@pytest.mark.django_db
class TestOwnerDownload:
    URL = "/cdn/api/v1/images/product/{}/original/"

    def test_owner_gets_the_original_bytes(self, uploaded, owner):
        client = APIClient()
        client.force_authenticate(user=owner)
        response = client.get(self.URL.format(uploaded.file_hash))
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == open(uploaded.original.path, "rb").read()

    def test_stranger_and_anonymous_are_refused(self, uploaded, stranger):
        client = APIClient()
        assert client.get(self.URL.format(uploaded.file_hash)).status_code in (401, 403)
        client.force_authenticate(user=stranger)
        assert client.get(self.URL.format(uploaded.file_hash)).status_code == 404


@pytest.mark.django_db
class TestProtectExisting:
    def test_moves_the_blob_and_every_row_over_it(self, conf, owner, stranger):
        from django.conf import settings

        folder = os.path.join(settings.MEDIA_ROOT, "product", "e" * 64)
        os.makedirs(folder)
        with open(os.path.join(folder, "a.jpg"), "wb") as fh:
            fh.write(_jpeg(400, 300))
        rows = []
        for user in (owner, stranger):
            with patch("stapel_cdn.tasks.process_image_async.delay"):
                row = Image(
                    file_hash="e" * 64, original_filename="a.jpg", file_extension=".jpg",
                    type="product", original_width=400, original_height=300,
                    original_size=1, uploaded_by=user, site_key="acme",
                )
                row.original.name = "product/" + "e" * 64 + "/a.jpg"
                Image.objects.bulk_create([row])
            rows.append(Image.objects.get(uploaded_by=user))
        assert protect_original(rows[0]) is True
        assert protect_original(Image.objects.get(pk=rows[1].pk)) is False
        for row in Image.objects.all():
            assert original_is_protected(row) and os.path.isfile(row.original.path)
        assert not os.path.exists(os.path.join(folder, "a.jpg"))
