"""
Tests for conf-driven settings (STAPEL_CDN namespace, stapel_cdn.conf).
"""
from django.test import override_settings

from stapel_cdn.conf import DEFAULT_VARIANT_SIZES, cdn_settings
from stapel_cdn.models import Image, get_image_type_choices
from stapel_cdn.services import ImageProcessingService


class TestDefaults:
    """With no overrides, conf must match the historical hardcoded values."""

    def test_variant_sizes_default(self):
        assert list(cdn_settings.VARIANT_SIZES) == [16, 32, 64, 120, 160, 240, 480, 720, 1080]

    def test_image_types_default(self):
        assert get_image_type_choices() == [("product", "Product"), ("avatar", "Avatar")]

    def test_max_image_size_default(self):
        assert cdn_settings.MAX_IMAGE_SIZE == 20 * 1024 * 1024

    def test_max_image_pixels_default(self):
        assert cdn_settings.MAX_IMAGE_PIXELS == 50_000_000

    def test_allowed_image_extensions(self):
        # conftest sets the legacy flat CDN_ALLOWED_IMAGE_EXTENSIONS
        assert ".jpg" in cdn_settings.ALLOWED_IMAGE_EXTENSIONS
        assert ".heic" in cdn_settings.ALLOWED_IMAGE_EXTENSIONS

    def test_pipeline_sizes_default_match_class_attributes(self):
        assert ImageProcessingService.get_thumbnail_sizes() == ImageProcessingService.THUMBNAIL_SIZES
        assert ImageProcessingService.get_preview_sizes() == ImageProcessingService.PREVIEW_SIZES

    def test_default_variant_properties_exist(self):
        image = Image(file_hash="a" * 64, type="product")
        for size in DEFAULT_VARIANT_SIZES:
            url = getattr(image, f"variant_{size}_url")
            assert url.endswith(f"product/{'a' * 64}/{size}.webp")
        assert image.variant_720_jpg_url.endswith(f"product/{'a' * 64}/720.jpg")

    def test_get_variant_url_accepts_int_and_str(self):
        image = Image(file_hash="a" * 64, type="avatar")
        assert image.get_variant_url(64) == image.get_variant_url("64")


class TestOverrides:
    """STAPEL_CDN dict overrides are honored (and cache-invalidated)."""

    def test_pipeline_honors_variant_sizes_override(self):
        with override_settings(STAPEL_CDN={"VARIANT_SIZES": [32, 300, 600]}):
            assert ImageProcessingService.get_variant_sizes() == [32, 300, 600]
            assert ImageProcessingService.get_thumbnail_sizes() == [("32", 32)]
            assert ImageProcessingService.get_preview_sizes() == [("600", 600), ("300", 300)]
        # back to defaults after the override is removed
        assert ImageProcessingService.get_thumbnail_sizes() == ImageProcessingService.THUMBNAIL_SIZES

    def test_model_variant_urls_honor_override(self):
        image = Image(file_hash="b" * 64, type="product")
        with override_settings(STAPEL_CDN={"VARIANT_SIZES": [32, 300]}):
            urls = image.variant_urls
            assert set(urls) == {32, 300}
            assert urls[300].endswith(f"product/{'b' * 64}/300.webp")
            # the named default-size properties still exist and work
            assert image.variant_16_url.endswith("/16.webp")
            assert image.variant_720_url.endswith("/720.webp")
        assert set(image.variant_urls) == set(DEFAULT_VARIANT_SIZES)

    def test_max_image_size_override_via_namespace(self):
        with override_settings(STAPEL_CDN={"MAX_IMAGE_SIZE": 1234}):
            assert cdn_settings.MAX_IMAGE_SIZE == 1234
        assert cdn_settings.MAX_IMAGE_SIZE == 20 * 1024 * 1024

    def test_legacy_flat_setting_alias(self):
        with override_settings(CDN_MAX_IMAGE_SIZE=4321):
            assert cdn_settings.MAX_IMAGE_SIZE == 4321
        assert cdn_settings.MAX_IMAGE_SIZE == 20 * 1024 * 1024

    def test_image_types_override(self):
        with override_settings(
            STAPEL_CDN={"IMAGE_TYPES": [("product", "Product"), ("banner", "Banner")]}
        ):
            values = [choice[0] for choice in get_image_type_choices()]
            assert values == ["product", "banner"]
        assert [c[0] for c in get_image_type_choices()] == ["product", "avatar"]
