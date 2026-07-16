"""
Settings for stapel-cdn, resolved through ``stapel_core.conf.AppSettings``.

Configure via a ``STAPEL_CDN`` dict in Django settings::

    STAPEL_CDN = {
        "THUMBNAIL_SIZES": [16, 32, 64, 120],
        "PREVIEW_SIZES": [160, 240, 480, 560, 720, 1080],
        "MAX_IMAGE_SIZE": 20 * 1024 * 1024,
    }

Resolution order per key: ``settings.STAPEL_CDN`` dict → legacy flat
``CDN_*`` setting (see ``LEGACY_ALIASES``) → flat Django setting of the
same name → environment variable → built-in default. All defaults match
the previously hardcoded values, so with no overrides behavior is
unchanged.
"""
from stapel_core.conf import AppSettings

#: Image type choices: (value, label) pairs. Mirrors the historical
#: ``ImageType`` TextChoices on the model.
DEFAULT_IMAGE_TYPES = (
    ("product", "Product"),
    ("avatar", "Avatar"),
)

#: Thumbnail tiers (images-and-cdn.md §2.1/§3.4): min-side resize, no w/h
#: branches, no watermark, high-priority queue. 16 is the micro tier — the
#: same file is inlined as ``preview_b64`` in the cdn.describe snapshot.
DEFAULT_THUMBNAIL_SIZES = (16, 32, 64, 120)

#: Preview tiers (images-and-cdn.md §2.1/§3.2): each tier generates TWO
#: branches — ``{T}w.webp`` (width == T) and ``{T}h.webp`` (height == T) —
#: so the limiting axis of any slot is served without upscaling. Square
#: images (within a 1px epsilon) generate only the w-branch and are marked
#: ``square`` in the render metadata (§3.3 dedup).
DEFAULT_PREVIEW_SIZES = (160, 240, 480, 560, 720, 1080)

#: Combined ladder — thumbnail + preview tiers, ascending. Kept for the
#: ``variant_<size>_url`` model property generation and admin display.
DEFAULT_VARIANT_SIZES = DEFAULT_THUMBNAIL_SIZES + DEFAULT_PREVIEW_SIZES

DEFAULTS = {
    "IMAGE_TYPES": DEFAULT_IMAGE_TYPES,
    "THUMBNAIL_SIZES": DEFAULT_THUMBNAIL_SIZES,
    "PREVIEW_SIZES": DEFAULT_PREVIEW_SIZES,
    # Upload size cap for images (bytes) — 20 MB.
    "MAX_IMAGE_SIZE": 20 * 1024 * 1024,
    "ALLOWED_IMAGE_EXTENSIONS": (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
    ),
    # Decompression-bomb cap: Pillow raises DecompressionBombError above
    # 2x this pixel count.
    "MAX_IMAGE_PIXELS": 50_000_000,
    # Watermark engine: dotted path to (or directly a) callable
    # ``(pyvips.Image) -> pyvips.Image`` applied to preview variants.
    # Empty (the default) disables watermarking entirely. The built-in
    # reference engine is "stapel_cdn.watermarks.text_watermark", which
    # renders WATERMARK_TEXT in the bottom-right corner; host projects
    # supply their own callable for designed watermarks.
    "WATERMARK": "",
    "WATERMARK_TEXT": "",
    # --- cdn.import_from_url (SSRF-hardened egress fetcher) ---------------
    # Body size cap for a fetched image (bytes). Aborts the stream mid-flight
    # once crossed — kept below MAX_IMAGE_SIZE since avatars are small and a
    # tighter cap shrinks the DoS surface of the outbound fetch.
    "IMPORT_FROM_URL_MAX_BYTES": 10 * 1024 * 1024,
    # Connect/read timeout (seconds) for the fetch.
    "IMPORT_FROM_URL_TIMEOUT": 5.0,
    # Max redirect hops; each hop is re-validated (scheme + DNS + IP).
    "IMPORT_FROM_URL_MAX_REDIRECTS": 3,
    # Per-caller fixed-window quota ("N/s|m|h|d") — open-proxy defence.
    "IMPORT_FROM_URL_RATE": "10/h",
}

_UNSET = object()


class CdnAppSettings(AppSettings):
    """AppSettings that also honors the legacy flat ``CDN_*`` names."""

    LEGACY_ALIASES = {
        "MAX_IMAGE_SIZE": "CDN_MAX_IMAGE_SIZE",
        "ALLOWED_IMAGE_EXTENSIONS": "CDN_ALLOWED_IMAGE_EXTENSIONS",
        "MAX_IMAGE_PIXELS": "CDN_MAX_IMAGE_PIXELS",
        "WATERMARK": "CDN_WATERMARK",
        "WATERMARK_TEXT": "CDN_WATERMARK_TEXT",
    }

    def _connect_reload(self):
        try:
            from django.test.signals import setting_changed

            def _reload(*, setting, **kwargs):
                if (
                    setting == self.namespace
                    or setting in self.defaults
                    or setting in self.LEGACY_ALIASES.values()
                ):
                    self.reload()

            setting_changed.connect(_reload, weak=False)
        except Exception:  # pragma: no cover — Django not ready
            pass

    def _raw(self, key):
        from django.conf import settings

        overrides = getattr(settings, self.namespace, None) or {}
        if key not in overrides:
            legacy = self.LEGACY_ALIASES.get(key)
            if legacy is not None:
                value = getattr(settings, legacy, _UNSET)
                if value is not _UNSET:
                    return value
        return super()._raw(key)


cdn_settings = CdnAppSettings(
    "STAPEL_CDN", defaults=DEFAULTS, import_strings=("WATERMARK",)
)

__all__ = [
    "cdn_settings",
    "CdnAppSettings",
    "DEFAULTS",
    "DEFAULT_IMAGE_TYPES",
    "DEFAULT_THUMBNAIL_SIZES",
    "DEFAULT_PREVIEW_SIZES",
    "DEFAULT_VARIANT_SIZES",
]
