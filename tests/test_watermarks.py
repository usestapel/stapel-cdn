"""Per-site watermarks: which renditions get a mark, and what stays clean."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory, override_settings
from PIL import Image as PILImage

from stapel_cdn import checks
from stapel_cdn.models import Image
from stapel_cdn.services import CLEAN_DIR, ImageProcessingService
from stapel_cdn.watermarks import (
    overlay_watermark,
    request_site_key,
    watermark_spec_for,
)

pyvips = pytest.importorskip("pyvips")

SITES = {
    "sites": [
        {"host": "primary.example", "primary": True, "brand": {"key": "acme", "name": "Acme"}},
        {"host": "second.example", "aliases": ["www.second.example"]},
    ]
}


@pytest.fixture
def mark_png(tmp_path):
    """A 200x50 half-transparent white bar."""
    path = tmp_path / "mark.png"
    PILImage.new("RGBA", (200, 50), (255, 255, 255, 128)).save(path)
    return str(path)


def _cdn(mark, **extra):
    conf = {
        "ASSET_TYPES": ("product", "avatar"),
        "WATERMARKS": {"acme": {"PATH": mark}},
        "WATERMARK_ASSET_TYPES": ("product",),
    }
    conf.update(extra)
    return conf


def _black(width, height):
    return pyvips.Image.black(width, height, bands=3).cast("uchar")


class TestRequestSiteKey:
    @override_settings(STAPEL_SITES=SITES, ALLOWED_HOSTS=["*"])
    def test_brand_key_then_host_then_nothing(self):
        from stapel_core.sites import reset_sites_cache

        reset_sites_cache()
        rf = RequestFactory()
        try:
            assert request_site_key(rf.get("/", HTTP_HOST="primary.example")) == "acme"
            assert request_site_key(rf.get("/", HTTP_HOST="www.second.example")) == "second.example"
            # An internal host is not stamped with the primary's brand.
            assert request_site_key(rf.get("/", HTTP_HOST="svc-cdn:8000")) == ""
        finally:
            reset_sites_cache()


class TestSpecResolution:
    def test_off_by_default(self):
        assert watermark_spec_for(SimpleNamespace(type="product", site_key="acme")) is None

    def test_site_type_and_default(self, mark_png):
        with override_settings(STAPEL_CDN=_cdn(mark_png)):
            spec = watermark_spec_for(SimpleNamespace(type="product", site_key="acme"))
            assert spec["PATH"] == mark_png and spec["POSITION"] == "bottom-right"
            assert watermark_spec_for(SimpleNamespace(type="avatar", site_key="acme")) is None
            assert watermark_spec_for(SimpleNamespace(type="product", site_key="")) is None
        with override_settings(STAPEL_CDN=_cdn(mark_png, WATERMARK_DEFAULT_SITE="acme")):
            assert watermark_spec_for(SimpleNamespace(type="product", site_key="")) is not None


class TestOverlay:
    def test_bottom_right_scaled_to_the_rendition(self, mark_png):
        out = overlay_watermark(_black(1000, 800), {"PATH": mark_png})
        assert (out.width, out.height, out.bands) == (1000, 800, 3)
        # Mark width 0.24 * 1000 = 240, inset 0.028 * 800 = 22.
        assert out.getpoint(1000 - 22 - 120, 800 - 22 - 20)[0] > 100
        assert out.getpoint(40, 40)[0] == 0
        assert out.getpoint(1000 - 22 - 245, 800 - 22 - 20)[0] == 0

    def test_panorama_caps_the_mark_height(self, mark_png):
        out = overlay_watermark(_black(1000, 200), {"PATH": mark_png})
        # MAX_HEIGHT 0.10 * 200 = 20px tall -> 80px wide, not 240.
        assert out.getpoint(1000 - 6 - 40, 200 - 6 - 10)[0] > 100
        assert out.getpoint(1000 - 6 - 150, 200 - 6 - 10)[0] == 0

    def test_unreadable_mark_leaves_the_image(self, tmp_path):
        img = _black(500, 500)
        assert overlay_watermark(img, {"PATH": str(tmp_path / "missing.png")}) is img

    def test_alpha_rendition_keeps_its_alpha(self, mark_png):
        img = _black(600, 600).bandjoin(255)
        assert overlay_watermark(img, {"PATH": mark_png}).bands == 4


@pytest.mark.django_db
class TestPipeline:
    @pytest.fixture
    def image(self, tmp_path, settings):
        settings.MEDIA_ROOT = str(tmp_path)
        hash_val = "c" * 64
        folder = tmp_path / "product" / hash_val
        folder.mkdir(parents=True)
        path = folder / "original.jpg"
        PILImage.new("RGB", (1600, 1200), (0, 0, 0)).save(path, format="JPEG")
        original_bytes = path.read_bytes()
        row = Image.objects.create(
            file_hash=hash_val,
            original_filename="original.jpg",
            file_extension=".jpg",
            type="product",
            original_width=1600,
            original_height=1200,
            original_size=len(original_bytes),
            site_key="acme",
        )
        row.original = MagicMock()
        row.original.path = str(path)
        return row, folder, original_bytes

    def test_previews_marked_small_tiers_and_original_clean(self, image, mark_png):
        row, folder, original_bytes = image
        with override_settings(STAPEL_CDN=_cdn(mark_png)):
            ImageProcessingService.generate_previews_only(row)
        meta = {(e["tier"], e["branch"]): e for e in row.variants_meta}
        # 1080w is 1080x810: marked, with a clean copy beside it.
        big = meta[(1080, "w")]
        assert big["watermarked"] is True
        assert big["clean_url"].endswith(f"/{CLEAN_DIR}/1080w.webp")
        assert (folder / CLEAN_DIR / "1080w.webp").exists()
        marked = pyvips.Image.new_from_file(str(folder / "1080w.webp"))
        clean = pyvips.Image.new_from_file(str(folder / CLEAN_DIR / "1080w.webp"))
        corner = (marked.width - 30 - 100, marked.height - 23 - 20)
        assert marked.getpoint(*corner)[0] > 60
        assert clean.getpoint(*corner)[0] < 10
        # 240w is 240x180: under WATERMARK_MIN_SIDE, left clean.
        assert "watermarked" not in meta[(240, "w")]
        assert not (folder / CLEAN_DIR / "240w.webp").exists()
        # The stored original is byte-identical.
        assert (folder / "original.jpg").read_bytes() == original_bytes

    def test_no_config_no_mark_no_clean_dir(self, image):
        row, folder, _ = image
        with override_settings(STAPEL_CDN={"ASSET_TYPES": ("product",)}):
            ImageProcessingService.generate_previews_only(row)
        assert not any(e.get("watermarked") for e in row.variants_meta)
        assert not os.path.exists(folder / CLEAN_DIR)

    def test_turning_it_off_removes_stale_clean_copies(self, image, mark_png):
        row, folder, _ = image
        with override_settings(STAPEL_CDN=_cdn(mark_png)):
            ImageProcessingService.generate_previews_only(row)
        with override_settings(STAPEL_CDN={"ASSET_TYPES": ("product",)}):
            ImageProcessingService.generate_previews_only(row)
        assert os.listdir(folder / CLEAN_DIR) == []


class TestCheck:
    def test_unreadable_path_and_unknown_default(self, tmp_path):
        conf = {
            "WATERMARKS": {"acme": {"PATH": str(tmp_path / "nope.png"), "POSITION": "middle"}},
            "WATERMARK_DEFAULT_SITE": "other",
        }
        with override_settings(STAPEL_CDN=conf):
            found = checks.check_watermarks()
        assert len(found) == 3
        assert {w.id for w in found} == {checks.W015_WATERMARK_UNUSABLE}

    def test_quiet_when_valid_or_unset(self, mark_png):
        assert checks.check_watermarks() == []
        with override_settings(STAPEL_CDN=_cdn(mark_png)):
            assert checks.check_watermarks() == []
