"""
Settings for stapel-cdn, resolved through ``stapel_core.conf.AppSettings``.

Configure via a ``STAPEL_CDN`` dict in Django settings::

    STAPEL_CDN = {
        "ASSET_TYPES": ("avatar", "banner"),
        "THUMBNAIL_SIZES": [16, 32, 64, 120],
        "PREVIEW_SIZES": [160, 240, 480, 560, 720, 1080],
        "MAX_IMAGE_SIZE": 20 * 1024 * 1024,
        "ENABLED_SUBMODULES": ("images", "video", "recordings"),
    }

Resolution order per key: ``settings.STAPEL_CDN`` dict → flat Django
setting of the same name → environment variable → built-in default.

``ASSET_TYPES`` is the **same** ``STAPEL_CDN`` namespace and key that
``stapel_core.django.cdn.conf`` reads on the client side (cdn-modularity.md
§2.1/§5) — a host project adds a type once, in one dict, and both the
client-side ``CdnImageField`` validation and this server's ``Image.type``
choices agree on what's legal. Default ``("avatar",)`` — the
zero-infrastructure default, the one CDN type every project plausibly has;
marketplace-specific types (``product``/``chat``/``review``) are not baked
in, a host project adds them explicitly. (The legacy ``IMAGE_TYPES`` key —
0.7.x and earlier — is gone; it accepted ``(value, label)`` pairs, but
those were never anything but ``str(value).capitalize()``, so the plain
string form carries the same information with one less shape to support.)
"""
from stapel_core.conf import AppSettings

#: Single source of truth for ``Image.type`` — shared with
#: ``stapel_core.django.cdn.conf.DEFAULT_ASSET_TYPES`` (same key, same
#: default, same namespace: ``STAPEL_CDN["ASSET_TYPES"]``).
DEFAULT_ASSET_TYPES = ("avatar",)

#: Media submodules with an optional system-binary dependency, enabled by
#: default. ``images`` is core (every Image save needs pyvips — the
#: E-check fires unconditionally, cdn-modularity.md §3), so it needs no
#: opt-in; ``video``/``recordings`` are VPS/prod submodules a host project
#: turns on explicitly once it actually stores that media kind — turning
#: one on is what makes the corresponding ffmpeg-missing system check
#: (tag ``stapel_cdn``) fire instead of silently staying green.
DEFAULT_ENABLED_SUBMODULES = ("images",)

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
    "ASSET_TYPES": DEFAULT_ASSET_TYPES,
    "ENABLED_SUBMODULES": DEFAULT_ENABLED_SUBMODULES,
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
    # Upload extensions for the recordings (audio) submodule — passthrough
    # storage always accepts these; ffmpeg-audio compression (once
    # implemented) is gated by "recordings" in ENABLED_SUBMODULES.
    "ALLOWED_AUDIO_EXTENSIONS": (
        ".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".aac",
    ),
    # Decompression-bomb cap: Pillow raises DecompressionBombError above
    # 2x this pixel count.
    "MAX_IMAGE_PIXELS": 50_000_000,
    # Upload size cap for audio recordings (bytes) — 50 MB.
    "MAX_AUDIO_SIZE": 50 * 1024 * 1024,
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
    "DEFAULT_ASSET_TYPES",
    "DEFAULT_ENABLED_SUBMODULES",
    "DEFAULT_THUMBNAIL_SIZES",
    "DEFAULT_PREVIEW_SIZES",
    "DEFAULT_VARIANT_SIZES",
]
