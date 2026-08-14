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
    # Upload size cap for videos (bytes) — 100 MB. The number is not new: the
    # endpoint's own OpenAPI description has always told callers "Maximum file
    # size: 100MB". Nothing enforced it, so the documented limit and the real
    # one disagreed by infinity — the whole body was read and SHA-256'd before
    # any bound was consulted, and the per-owner byte quota was the only
    # ceiling underneath (itself opt-out-able). Now the documentation and the
    # gate are the same value.
    "MAX_VIDEO_SIZE": 100 * 1024 * 1024,
    # Upload extensions for the recordings (audio) submodule — passthrough
    # storage always accepts these; ffmpeg-audio compression (once
    # implemented) is gated by "recordings" in ENABLED_SUBMODULES.
    #
    # RESERVED, NOT AN ACTIVE KNOB. stapel-cdn has no audio upload at all:
    # the `Audio` model is declared but never instantiated by this library —
    # the real upload path lives in stapel-recordings with its own
    # `MAX_UPLOAD_BYTES` in its own namespace. Pulling these keys over there
    # would couple libs through each other's settings. Silenced on purpose:
    # once an audio path lands HERE, drop the noqa and validate both values.
    "ALLOWED_AUDIO_EXTENSIONS": (  # noqa: CFG006
        ".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".aac",
    ),
    # Decompression-bomb cap: an upload whose width*height exceeds this is
    # refused. libvips reads the dimensions from the header without decoding,
    # so the bomb costs nothing to refuse. NB 0.10 made this the exact cap its
    # name always claimed: Pillow used to only raise above *2x* this number, so
    # the effective ceiling was quietly double the configured one.
    "MAX_IMAGE_PIXELS": 50_000_000,
    # Upload size cap for audio recordings (bytes) — 50 MB.
    # Same reserved status as ALLOWED_AUDIO_EXTENSIONS above.
    "MAX_AUDIO_SIZE": 50 * 1024 * 1024,  # noqa: CFG006
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
    # --- ownership, dedup scoping and per-owner quotas -------------------
    # Scope of content-hash deduplication on every intake path.
    #
    #   "owner"  — a hash lookup only ever matches objects the *calling*
    #              principal already owns. The default.
    #   "global" — a hash lookup matches any object with the same bytes,
    #              whoever uploaded it.
    #
    # "global" is a disclosure channel, not just a storage optimisation: an
    # upload that reports "already exists" answers "does somebody in this
    # deployment hold exactly these bytes?" for any file the caller can guess
    # or obtain, and the response carries the other holder's row — id,
    # filename, refs, timestamps. A host that accepts that (single-tenant
    # deployments, or a deliberately shared public asset pool) can opt back
    # in; nothing in this library assumes it.
    "DEDUP_SCOPE": "owner",
    # Per-owner storage ceilings across images + videos + files. The defaults
    # are deliberately generous rather than absent: an identity that costs one
    # POST to mint must not have an unbounded quota, and a real user is
    # nowhere near these numbers.
    #
    # Removing a ceiling takes the literal string "unlimited". It used to take
    # 0 — and, because the code read `int(setting or 0)`, also None, "" and a
    # missing key, so three ways of *saying nothing* meant "no ceiling". A
    # value that is neither "unlimited" nor a positive number falls back to
    # the default below and is reported by checks.W007.
    "MAX_OBJECTS_PER_OWNER": 1000,
    "MAX_BYTES_PER_OWNER": 2 * 1024 * 1024 * 1024,
    # --- generic (non-image, non-video) intake ---------------------------
    # Upload size cap for GenericFileUploadView (bytes).
    "MAX_FILE_SIZE": 50 * 1024 * 1024,
    "ALLOWED_FILE_EXTENSIONS": (
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".zip", ".rar", ".7z", ".gz",
    ),
    # A Content-Type the caller declares is not evidence, so this list is a
    # narrowing device, not a verdict — which is exactly why
    # "application/octet-stream" is NOT in it. It is the universal "some
    # bytes" type any client may declare for anything, so shipping it in the
    # default allowlist reduced the gate to a no-op by construction: every
    # payload the list was written to exclude passes it by naming it. A host
    # that genuinely intakes opaque binaries adds it back explicitly.
    "ALLOWED_FILE_MIME_TYPES": (
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
        "application/zip",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/gzip",
    ),
    # Path prefix under which non-image originals (documents, archives,
    # audio) are stored, so an operator has ONE prefix to deny on the public
    # media route / bucket policy instead of having to enumerate types.
    # Empty keeps the historical flat layout; only NEW uploads move, stored
    # rows keep the path recorded in the database.
    "PRIVATE_MEDIA_PREFIX": "private",
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
