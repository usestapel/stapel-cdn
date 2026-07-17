"""
Tests for conf-driven settings (STAPEL_CDN namespace, stapel_cdn.conf).

Note: this test module runs under the package's own test settings, which
override ``STAPEL_CDN["ASSET_TYPES"]`` to ``("avatar", "product")``
(conftest.py) so the wider fixture suite can keep using "product" as a
second generic image type. ``test_asset_types_shipped_default`` below
asserts the *shipped* default directly (bypassing the override) since
that's what cdn-modularity.md §2.1/§5 actually pins down.
"""
from django.test import override_settings

from stapel_cdn.conf import (
    DEFAULT_ASSET_TYPES,
    DEFAULT_PREVIEW_SIZES,
    DEFAULT_THUMBNAIL_SIZES,
    DEFAULT_VARIANT_SIZES,
    cdn_settings,
)
from stapel_cdn.models import Image, get_image_type_choices
from stapel_cdn.services import ImageProcessingService


class TestDefaults:
    """With no overrides, conf must match the historical hardcoded values."""

    def test_tier_lists_default(self):
        # images-and-cdn.md §2.1: 560 inserted between 480 and 720; the
        # ladder splits into thumbnail (min-side) and preview (w/h) classes.
        assert list(cdn_settings.THUMBNAIL_SIZES) == [16, 32, 64, 120]
        assert list(cdn_settings.PREVIEW_SIZES) == [160, 240, 480, 560, 720, 1080]
        assert list(DEFAULT_VARIANT_SIZES) == [16, 32, 64, 120, 160, 240, 480, 560, 720, 1080]

    def test_image_types_default(self):
        # Package-level test override (conftest.py) adds "product" back —
        # see test_asset_types_shipped_default for the real shipped default.
        assert set(get_image_type_choices()) == {("avatar", "Avatar"), ("product", "Product")}

    def test_asset_types_shipped_default(self):
        # cdn-modularity.md §2.1/§5 — the zero-infrastructure default, no
        # marketplace-specific types baked in. Shared key/namespace with
        # stapel_core.django.cdn.conf.DEFAULT_ASSET_TYPES on the client side.
        assert DEFAULT_ASSET_TYPES == ("avatar",)

    def test_max_image_size_default(self):
        assert cdn_settings.MAX_IMAGE_SIZE == 20 * 1024 * 1024

    def test_max_image_pixels_default(self):
        assert cdn_settings.MAX_IMAGE_PIXELS == 50_000_000

    def test_allowed_image_extensions(self):
        assert ".jpg" in cdn_settings.ALLOWED_IMAGE_EXTENSIONS
        assert ".heic" in cdn_settings.ALLOWED_IMAGE_EXTENSIONS

    def test_pipeline_sizes_default_match_class_attributes(self):
        assert ImageProcessingService.get_thumbnail_sizes() == ImageProcessingService.THUMBNAIL_SIZES
        assert ImageProcessingService.get_preview_sizes() == ImageProcessingService.PREVIEW_SIZES

    def test_default_variant_properties_exist(self):
        image = Image(file_hash="a" * 64, type="product")
        for size in DEFAULT_THUMBNAIL_SIZES:
            url = getattr(image, f"variant_{size}_url")
            assert url.endswith(f"product/{'a' * 64}/{size}.webp")
        for size in DEFAULT_PREVIEW_SIZES:
            url = getattr(image, f"variant_{size}_url")
            assert url.endswith(f"product/{'a' * 64}/{size}w.webp")

    def test_get_variant_url_accepts_int_and_str(self):
        image = Image(file_hash="a" * 64, type="avatar")
        assert image.get_variant_url(64) == image.get_variant_url("64")


class TestOverrides:
    """STAPEL_CDN dict overrides are honored (and cache-invalidated)."""

    def test_pipeline_honors_tier_list_overrides(self):
        with override_settings(
            STAPEL_CDN={"THUMBNAIL_SIZES": [32], "PREVIEW_SIZES": [300, 600]}
        ):
            assert ImageProcessingService.get_thumbnail_sizes() == [("32", 32)]
            assert ImageProcessingService.get_preview_sizes() == [("600", 600), ("300", 300)]
        # back to defaults after the override is removed
        assert ImageProcessingService.get_thumbnail_sizes() == ImageProcessingService.THUMBNAIL_SIZES

    def test_model_variant_urls_honor_override(self):
        image = Image(file_hash="b" * 64, type="product")
        with override_settings(
            STAPEL_CDN={"THUMBNAIL_SIZES": [32], "PREVIEW_SIZES": [300]}
        ):
            urls = image.variant_urls
            assert set(urls) == {32, 300}
            assert urls[32].endswith(f"product/{'b' * 64}/32.webp")
            # preview tiers map to the w-branch file
            assert urls[300].endswith(f"product/{'b' * 64}/300w.webp")
            # thumbnail/preview class membership follows the override
            assert image.variant_32_url.endswith("/32.webp")
        # the named default-size properties work against the defaults
        assert image.variant_16_url.endswith("/16.webp")
        assert image.variant_720_url.endswith("/720w.webp")
        assert set(image.variant_urls) == set(DEFAULT_VARIANT_SIZES)

    def test_max_image_size_override_via_namespace(self):
        with override_settings(STAPEL_CDN={"MAX_IMAGE_SIZE": 1234}):
            assert cdn_settings.MAX_IMAGE_SIZE == 1234
        assert cdn_settings.MAX_IMAGE_SIZE == 20 * 1024 * 1024

    def test_image_types_override(self):
        with override_settings(
            STAPEL_CDN={"ASSET_TYPES": [("product", "Product"), ("banner", "Banner")]}
        ):
            values = [choice[0] for choice in get_image_type_choices()]
            assert values == ["product", "banner"]
        # back to the package's own test-settings override (conftest.py)
        assert set(c[0] for c in get_image_type_choices()) == {"avatar", "product"}
