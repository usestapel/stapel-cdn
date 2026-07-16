"""
Tests for the images-and-cdn.md §61 tier/branch semantics:

- ``_resize`` axes (w / h / min);
- min-side thumbnail ladder (§3.4);
- two-branch preview generation with square dedup (§3.2-3.3);
- persisted ``variants_meta`` geometry (§6 п.3);
- ``cdn.describe`` render-metadata snapshot (§5);
- ``regenerate_media`` management command (§6 п.5);
- the v1 URL canon (api-versioning.md §2).
"""
import os
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from PIL import Image as PILImage

from stapel_core.comm import call, function_registry

from stapel_cdn.models import Image, Video, File
from stapel_cdn.services import ImageProcessingService, build_render_metadata

pyvips = pytest.importorskip("pyvips")


def _write_jpeg(path, width, height, color="blue"):
    img = PILImage.new("RGB", (width, height), color=color)
    img.save(str(path), format="JPEG")


def _make_image_with_file(tmp_path, settings, width, height, hash_char="f",
                          image_type="product"):
    """DB Image + real file on disk, original mocked to the file path."""
    settings.MEDIA_ROOT = str(tmp_path)
    file_hash = hash_char * 64
    img_dir = tmp_path / image_type / file_hash
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / "original.jpg"
    _write_jpeg(img_path, width, height)

    image = Image.objects.create(
        file_hash=file_hash,
        original_filename="original.jpg",
        file_extension=".jpg",
        type=image_type,
        original_width=width,
        original_height=height,
        original_size=os.path.getsize(str(img_path)),
        is_processed=False,
    )
    image.original = MagicMock()
    image.original.path = str(img_path)
    image.original.name = f"{image_type}/{file_hash}/original.jpg"
    image.original.url = f"/media/{image_type}/{file_hash}/original.jpg"
    return image, img_dir


class TestResizeAxes:
    """_resize(img, target, axis) — §3.2/§3.4."""

    def _img(self, width, height):
        img = MagicMock()
        img.width = width
        img.height = height
        img.resize.side_effect = lambda scale: (scale, img)
        return img

    def test_axis_w_downscales_by_width(self):
        img = self._img(1000, 500)
        result = img.resize.side_effect  # keep referenced
        ImageProcessingService._resize(img, 250, axis="w")
        img.resize.assert_called_once_with(0.25)
        assert result is not None

    def test_axis_h_downscales_by_height(self):
        img = self._img(1000, 500)
        ImageProcessingService._resize(img, 250, axis="h")
        img.resize.assert_called_once_with(0.5)

    def test_axis_min_uses_smaller_side(self):
        img = self._img(1000, 500)
        ImageProcessingService._resize(img, 250, axis="min")
        img.resize.assert_called_once_with(0.5)  # min side is height

    def test_default_axis_is_height(self):
        img = self._img(1000, 500)
        ImageProcessingService._resize(img, 250)
        img.resize.assert_called_once_with(0.5)

    def test_no_upscale_on_any_axis(self):
        for axis in ("w", "h", "min"):
            img = self._img(100, 50)
            assert ImageProcessingService._resize(img, 200, axis=axis) is img
            img.resize.assert_not_called()


@pytest.mark.django_db
class TestTwoBranchGeneration:
    """Preview w/h branches + min-side thumbnails on a real pyvips run."""

    def test_portrait_generates_both_branches(self, tmp_path, settings):
        image, img_dir = _make_image_with_file(tmp_path, settings, 600, 1200)

        ImageProcessingService.generate_thumbnails_only(image)
        ImageProcessingService.generate_previews_only(image, apply_watermark=False)

        # Thumbnails: min-side files, no branch suffix.
        for tier in (16, 32, 64, 120):
            path = img_dir / f"{tier}.webp"
            assert path.exists(), f"missing {tier}.webp"
            loaded = pyvips.Image.new_from_file(str(path))
            assert min(loaded.width, loaded.height) == tier

        # Previews: both branches for a non-square image.
        for tier in (160, 240, 480, 560, 720, 1080):
            assert (img_dir / f"{tier}w.webp").exists(), f"missing {tier}w"
            assert (img_dir / f"{tier}h.webp").exists(), f"missing {tier}h"

        # The w-branch pins WIDTH to the tier (no upscale: 600 native).
        w560 = pyvips.Image.new_from_file(str(img_dir / "560w.webp"))
        assert w560.width == 560
        w1080 = pyvips.Image.new_from_file(str(img_dir / "1080w.webp"))
        assert w1080.width == 600  # native width < 1080 — saved as-is

        # The h-branch pins HEIGHT to the tier.
        h720 = pyvips.Image.new_from_file(str(img_dir / "720h.webp"))
        assert h720.height == 720
        h1080 = pyvips.Image.new_from_file(str(img_dir / "1080h.webp"))
        assert h1080.height == 1080

        # No legacy JPEG fallback anymore.
        assert not (img_dir / "720.jpg").exists()

    def test_square_image_generates_w_branch_only(self, tmp_path, settings):
        image, img_dir = _make_image_with_file(
            tmp_path, settings, 500, 500, hash_char="e"
        )

        ImageProcessingService.generate_previews_only(image, apply_watermark=False)

        assert (img_dir / "480w.webp").exists()
        assert not (img_dir / "480h.webp").exists()
        # variants_meta carries only w-branch preview entries
        branches = {e["branch"] for e in image.variants_meta}
        assert branches == {"w"}

    def test_variants_meta_persisted_with_geometry(self, tmp_path, settings):
        image, _ = _make_image_with_file(
            tmp_path, settings, 600, 1200, hash_char="d"
        )

        ImageProcessingService.generate_thumbnails_only(image)
        ImageProcessingService.generate_previews_only(image, apply_watermark=False)

        image.refresh_from_db()
        entries = {(e["tier"], e["branch"]): e for e in image.variants_meta}

        # 4 thumbnail (branch None) + 6×2 preview entries.
        assert len(entries) == 4 + 12

        thumb = entries[(120, None)]
        assert min(thumb["width"], thumb["height"]) == 120
        assert thumb["url"].endswith("/120.webp")

        w = entries[(560, "w")]
        assert w["width"] == 560 and w["url"].endswith("/560w.webp")
        h = entries[(560, "h")]
        assert h["height"] == 560 and h["url"].endswith("/560h.webp")

        # No-upscale cap recorded truthfully.
        assert entries[(1080, "w")]["width"] == 600

    def test_rerun_replaces_not_duplicates(self, tmp_path, settings):
        image, _ = _make_image_with_file(
            tmp_path, settings, 300, 600, hash_char="c"
        )
        ImageProcessingService.generate_previews_only(image, apply_watermark=False)
        first = len(image.variants_meta)
        ImageProcessingService.generate_previews_only(image, apply_watermark=False)
        assert len(image.variants_meta) == first


@pytest.mark.django_db
class TestDescribe:
    """cdn.describe — render-metadata snapshot §5."""

    def test_registered(self):
        assert "cdn.describe" in function_registry.names()

    def test_image_snapshot_shape(self, tmp_path, settings):
        image, img_dir = _make_image_with_file(
            tmp_path, settings, 600, 1200, hash_char="b"
        )
        ImageProcessingService.generate_thumbnails_only(image)
        ImageProcessingService.generate_previews_only(image, apply_watermark=False)
        # NB: no refresh_from_db — it would reset the mocked `original`
        # FieldFile; variants_meta is already up to date on the instance.

        snapshot = build_render_metadata(image)

        assert snapshot["mime"] == "image/jpeg"
        assert snapshot["bytes"] == image.original_size
        assert snapshot["width"] == 600 and snapshot["height"] == 1200
        assert snapshot["aspect"] == 0.5
        assert snapshot["duration_ms"] is None
        assert snapshot["square"] is False
        # 16px micro tier inlined as a data URI
        assert snapshot["preview_b64"].startswith("data:image/webp;base64,")
        tiers = {(v["tier"], v["branch"]) for v in snapshot["variants"]}
        assert (16, None) in tiers
        assert (560, "w") in tiers and (560, "h") in tiers
        # original is part of the ladder (never upscaled past the top tier)
        originals = [v for v in snapshot["variants"] if v["tier"] == "original"]
        assert len(originals) == 1
        assert originals[0]["width"] == 600 and originals[0]["height"] == 1200

    def test_square_flag(self, tmp_path, settings):
        image, _ = _make_image_with_file(
            tmp_path, settings, 500, 500, hash_char="a"
        )
        snapshot = build_render_metadata(image)
        assert snapshot["square"] is True

    def test_describe_via_comm(self, tmp_path, settings):
        image, _ = _make_image_with_file(
            tmp_path, settings, 400, 300, hash_char="9"
        )
        result = call("cdn.describe", {"ref": f"product/{image.file_hash}"})
        assert result["width"] == 400
        assert result["aspect"] == 400 / 300

    def test_describe_unknown_ref_raises(self):
        with pytest.raises(Exception):
            call("cdn.describe", {"ref": f"product/{'0' * 64}"})

    def test_video_snapshot(self):
        video = Video.objects.create(
            file_hash="8" * 64,
            original_filename="clip.mp4",
            file_extension=".mp4",
            original_size=5000,
            duration=12.5,
        )
        snapshot = build_render_metadata(video)
        assert snapshot["mime"] == "video/mp4"
        assert snapshot["duration_ms"] == 12500
        assert snapshot["preview_b64"] is None

    def test_file_snapshot(self):
        file_obj = File.objects.create(
            file_hash="7" * 64,
            original_filename="doc.pdf",
            file_extension=".pdf",
            mime_type="application/pdf",
            original_size=999,
        )
        snapshot = build_render_metadata(file_obj)
        assert snapshot["mime"] == "application/pdf"
        assert snapshot["width"] is None and snapshot["aspect"] is None


@pytest.mark.django_db
class TestRegenerateMedia:
    """regenerate_media — operational relaunch of the pipeline (§6 п.5)."""

    def _upload_image(self, tmp_path, settings, width=600, height=1200):
        from django.core.files.uploadedfile import SimpleUploadedFile

        settings.MEDIA_ROOT = str(tmp_path)
        buf = BytesIO()
        PILImage.new("RGB", (width, height), color="red").save(buf, format="JPEG")
        upload = SimpleUploadedFile("photo.jpg", buf.getvalue(), "image/jpeg")
        return Image.objects.create(type="product", original=upload,
                                    original_size=len(buf.getvalue()))

    def test_regenerates_under_new_semantics(self, tmp_path, settings):
        image = self._upload_image(tmp_path, settings)
        img_dir = tmp_path / "product" / image.file_hash

        # Plant old-semantics leftovers (single ladder + legacy JPEG).
        (img_dir / "720.webp").write_bytes(b"old")
        (img_dir / "720.jpg").write_bytes(b"old")

        call_command("regenerate_media")

        image.refresh_from_db()
        assert image.is_processed is True
        assert not (img_dir / "720.webp").exists()  # old bare preview removed
        assert not (img_dir / "720.jpg").exists()
        assert (img_dir / "720w.webp").exists()
        assert (img_dir / "720h.webp").exists()
        assert (img_dir / "16.webp").exists()
        # original untouched
        assert any(f.name.startswith("photo") for f in img_dir.iterdir())
        assert len(image.variants_meta) == 16

    def test_dry_run_touches_nothing(self, tmp_path, settings):
        image = self._upload_image(tmp_path, settings, 300, 300)
        img_dir = tmp_path / "product" / image.file_hash
        (img_dir / "720.webp").write_bytes(b"old")

        call_command("regenerate_media", "--dry-run")

        image.refresh_from_db()
        assert (img_dir / "720.webp").exists()
        assert image.variants_meta == []

    def test_type_filter(self, tmp_path, settings):
        image = self._upload_image(tmp_path, settings, 200, 400)
        call_command("regenerate_media", "--type", "avatar")
        image.refresh_from_db()
        assert image.variants_meta == []  # product image untouched


@pytest.mark.django_db
class TestV1Canon:
    """api-versioning.md §2: /cdn/api/v1/... is the only mounted surface."""

    def test_bare_path_is_gone(self, client):
        response = client.post("/cdn/api/file/exists/", {"file_hash": "x" * 64})
        assert response.status_code == 404

    def test_v1_path_resolves(self, client):
        response = client.post(
            "/cdn/api/v1/file/exists/", {"file_hash": "x" * 64}
        )
        assert response.status_code != 404
