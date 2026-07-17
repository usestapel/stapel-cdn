"""
Settings for stapel-cdn, resolved through ``stapel_core.conf.AppSettings``.

Configure via a ``STAPEL_CDN`` dict in Django settings::

    STAPEL_CDN = {
        "THUMBNAIL_SIZES": [16, 32, 64, 120],
        "PREVIEW_SIZES": [160, 240, 480, 560, 720, 1080],
        "MAX_IMAGE_SIZE": 20 * 1024 * 1024,
    }

Resolution order per key: ``settings.STAPEL_CDN`` dict → flat Django
setting of the same name → environment variable → built-in default.
"""
from stapel_core.conf import AppSettings

#: Image type choices: (value, label) pairs.
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
    "ALLOWED_VIDEO_EXTENSIONS": (
        ".mp4", ".webm", ".mov", ".avi", ".mkv",
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

cdn_settings = AppSettings(
    "STAPEL_CDN", defaults=DEFAULTS, import_strings=("WATERMARK",)
)

__all__ = [
    "cdn_settings",
    "DEFAULTS",
    "DEFAULT_IMAGE_TYPES",
    "DEFAULT_THUMBNAIL_SIZES",
    "DEFAULT_PREVIEW_SIZES",
    "DEFAULT_VARIANT_SIZES",
]
