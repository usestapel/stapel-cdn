"""retype_images: consumer-claimed images move to the right asset type."""
import io
import os
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from stapel_cdn.models import Image

pytestmark = pytest.mark.django_db

LISTING = "listings/listing"


def _jpeg(color):
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (64, 48), color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _user(name):
    from stapel_core.django.users.models import User

    return User.objects.create_user(username=name, email=f"{name}@example.com", password="x-pass-123")


def _row(owner, color, refs, image_type="avatar", site_key=""):
    data = _jpeg(color)
    with patch("stapel_cdn.tasks.process_image_async"):
        return Image.objects.create(
            file_hash=Image.calculate_file_hash(SimpleUploadedFile("p.jpg", data)),
            original=SimpleUploadedFile("p.jpg", data, content_type="image/jpeg"),
            original_filename="p.jpg",
            file_extension=".jpg",
            original_size=len(data),
            type=image_type,
            refs=refs,
            uploaded_by=owner,
            site_key=site_key,
        )


def _run(*args):
    out = StringIO()
    with patch("stapel_cdn.services.ImageProcessingService.process_image") as render:
        call_command("retype_images", *args, stdout=out, stderr=StringIO())
    return out.getvalue(), render


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def test_dry_run_counts_and_changes_nothing():
    owner = _user("seller-a")
    _row(owner, "red", [f"{LISTING}/1"])
    _row(owner, "blue", ["profiles/profile/x", f"{LISTING}/2"])
    _row(owner, "green", ["profiles/profile/x"])  # a real avatar: not selected

    out, render = _run("--from", "avatar", "--to", "product", "--claimed-by", LISTING, "--dry-run")

    assert "2 avatar image(s)" in out
    assert "1 retype in place, 1 copy" in out
    assert Image.objects.filter(type="avatar").count() == 3
    render.assert_not_called()


def test_exclusive_listing_photo_is_retyped_in_place():
    owner = _user("seller-b")
    row = _row(owner, "red", [f"{LISTING}/7"])
    old_original = row.original.path

    _run("--from", "avatar", "--to", "product", "--claimed-by", LISTING, "--site-key", "brand")

    row.refresh_from_db()
    assert row.type == "product"
    assert row.site_key == "brand"
    assert row.refs == [f"{LISTING}/7"]
    assert row.original.name.startswith(f"product/{row.file_hash}/")
    assert os.path.exists(row.original.path)
    assert not os.path.exists(old_original)


def test_a_picture_that_is_also_a_profile_avatar_keeps_the_avatar():
    owner = _user("seller-c")
    row = _row(owner, "blue", ["profiles/profile/p1", f"{LISTING}/8"], site_key="brand")

    _run("--from", "avatar", "--to", "product", "--claimed-by", LISTING)

    row.refresh_from_db()
    assert row.type == "avatar"
    assert row.refs == ["profiles/profile/p1"]
    assert os.path.exists(row.original.path)
    copy = Image.objects.get(type="product", file_hash=row.file_hash)
    assert copy.refs == [f"{LISTING}/8"]
    assert copy.uploaded_by_id == owner.pk
    assert copy.site_key == "brand"


def test_an_existing_target_row_absorbs_the_refs():
    owner = _user("seller-d")
    source = _row(owner, "gray", [f"{LISTING}/9"])
    target = _row(owner, "gray", [], image_type="product")

    _run("--from", "avatar", "--to", "product", "--claimed-by", LISTING)

    source.refresh_from_db()
    target.refresh_from_db()
    assert target.refs == [f"{LISTING}/9"]
    assert target.unreferenced_since is None
    assert source.refs == []
    assert source.unreferenced_since is not None


@override_settings(
    STAPEL_CDN={
        "ASSET_TYPES": ("avatar", "product"),
        "WATERMARKS": {"brand": {"PATH": "/nonexistent/mark.png"}},
        "WATERMARK_ASSET_TYPES": ("product",),
    }
)
def test_a_watermarked_type_lands_in_the_protected_tree():
    owner = _user("seller-e")
    row = _row(owner, "navy", [f"{LISTING}/10"])

    _run("--from", "avatar", "--to", "product", "--claimed-by", LISTING, "--site-key", "brand")

    row.refresh_from_db()
    assert row.original.name.startswith(f"protected/product/{row.file_hash}/")


def test_unknown_type_is_refused():
    with pytest.raises(CommandError):
        _run("--from", "avatar", "--to", "nope", "--claimed-by", LISTING)
