"""Erasure removes every file derived from the erased bytes, not only the original.

The pipeline is run for real (libvips), so what is checked is the set of files
it actually wrote. That set is held against ``renditions.possible_renditions``
in both directions: everything the pipeline wrote must be a name the module
knows (a new rendition kind cannot appear unnoticed), and after erasure none of
it may remain on disk — including a tier added to the configuration only for
this test.
"""
import io
import os
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PILImage
from stapel_core.django.users.models import User

from stapel_cdn import renditions
from stapel_cdn.erasure import erase
from stapel_cdn.gdpr import MediaErasureIncomplete
from stapel_cdn.models import Image, Video
from stapel_cdn.services import ImageProcessingService, VideoProcessingService

pyvips = pytest.importorskip("pyvips")

pytestmark = pytest.mark.django_db

HASH = "e" * 64


def _jpeg(width=1300, height=900) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 120, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _png(width=640, height=360) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), (10, 10, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def mark_png(tmp_path):
    path = tmp_path / "mark.png"
    PILImage.new("RGBA", (200, 50), (255, 255, 255, 128)).save(path)
    return str(path)


def _files_under(root) -> set[str]:
    found = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _processed_image(user):
    data = _jpeg()
    with patch("stapel_cdn.tasks.process_image_async"):
        row = Image.objects.create(
            file_hash=HASH,
            original_filename="pic.jpg",
            file_extension=".jpg",
            type="product",
            original_width=1300,
            original_height=900,
            original_size=len(data),
            uploaded_by=user,
            site_key="acme",
            original=SimpleUploadedFile("pic.jpg", data, content_type="image/jpeg"),
        )
    ImageProcessingService.process_image(row)
    return row


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="renditions", email="renditions@example.com", password="pw-123456"
    )


def _conf(mark, **extra):
    conf = {
        "ASSET_TYPES": ("avatar", "product"),
        "WATERMARKS": {"acme": {"PATH": mark}},
        "WATERMARK_ASSET_TYPES": ("product",),
    }
    conf.update(extra)
    return conf


class TestImageRenditionsGoWithTheOriginal:
    @pytest.mark.parametrize(
        "extra",
        [
            {},
            # A size nobody ships today: erasure must not depend on a list.
            {"THUMBNAIL_SIZES": (120, 64, 32, 16, 100), "PREVIEW_SIZES": (1080, 720, 560, 480, 240, 160, 300)},
        ],
        ids=["shipped-ladder", "with-an-added-tier"],
    )
    @pytest.mark.parametrize("subject", ["file", "account"])
    def test_every_written_rendition_is_gone_after_erasure(
        self, media_root, mark_png, user, extra, subject
    ):
        with override_settings(STAPEL_CDN=_conf(mark_png, **extra)):
            row = _processed_image(user)
            original = row.original.name
            written = _files_under(media_root) - {"mark.png"}
            derived = written - {original}

            # The pipeline really ran: public thumbnails, both preview
            # branches and the protected clean copies are on disk.
            public = renditions.image_public_dir(row)
            assert f"{public}/120.webp" in derived
            assert f"{public}/720w.webp" in derived
            assert f"{public}/720h.webp" in derived
            assert any("/clean/" in path for path in derived)
            if extra:
                assert f"{public}/100.webp" in derived
                assert f"{public}/300w.webp" in derived

            # Everything it wrote is a rendition the module can name.
            assert derived <= set(renditions.possible_renditions(row)), (
                derived - set(renditions.possible_renditions(row))
            )

            if subject == "file":
                counts = erase("file", f"product/{HASH}")
            else:
                counts = erase("account", user.pk)

        left = {p for p in written if (media_root / p).exists()}
        assert left == set()
        for rel in renditions.derived_dirs(row):
            assert not (media_root / rel).exists()
        assert not Image.objects.filter(pk=row.pk).exists()
        if subject == "file":
            assert counts["renditions_removed"] == len(derived)

    def test_a_sibling_row_over_the_same_bytes_keeps_its_renditions(
        self, media_root, mark_png, user
    ):
        other = User.objects.create_user(
            username="sibling", email="sibling@example.com", password="pw-123456"
        )
        with override_settings(STAPEL_CDN=_conf(mark_png)):
            row = _processed_image(user)
            with patch("stapel_cdn.tasks.process_image_async"):
                Image.objects.create(
                    file_hash=HASH,
                    original_filename="pic.jpg",
                    file_extension=".jpg",
                    type="product",
                    original_width=1300,
                    original_height=900,
                    original_size=row.original_size,
                    uploaded_by=other,
                    original=row.original.name,
                )
            erase("account", user.pk)
        public = renditions.image_public_dir(row)
        assert (media_root / public / "120.webp").exists()

    def test_a_rendition_that_cannot_be_removed_keeps_the_row(
        self, media_root, mark_png, user
    ):
        with override_settings(STAPEL_CDN=_conf(mark_png)):
            row = _processed_image(user)
            with patch(
                "stapel_cdn.renditions.shutil.rmtree", lambda *a, **k: None
            ), pytest.raises(MediaErasureIncomplete):
                erase("file", f"product/{HASH}")
        assert Image.objects.filter(pk=row.pk).exists()


class TestVideoPosterGoesWithTheOriginal:
    def test_poster_is_gone_after_erasure(self, media_root, user):
        video = Video.objects.create(
            file_hash="f" * 64,
            original_filename="clip.mp4",
            file_extension=".mp4",
            original_size=4,
            uploaded_by=user,
            original=SimpleUploadedFile("clip.mp4", b"mp4!", content_type="video/mp4"),
        )
        VideoProcessingService._write_poster(video, _png())
        poster = media_root / renditions.video_dir(video.file_hash) / renditions.POSTER_FILENAME
        assert poster.exists()
        assert {
            os.path.relpath(poster, media_root)
        } <= set(renditions.possible_renditions(video))

        erase("file", f"video/{video.file_hash}")

        assert not poster.exists()
        assert not (media_root / renditions.video_dir(video.file_hash)).exists()
