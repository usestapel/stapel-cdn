"""
Service layer for stapel-cdn - handles file processing and variant generation.
Uses pyvips for fast image processing with ladder downscaling optimization.
"""

from __future__ import annotations

import base64
import logging
import math
import mimetypes
import os
import time
from datetime import datetime
from typing import List

try:
    import pyvips
except ImportError:  # pragma: no cover
    pyvips = None  # type: ignore[assignment]

from django.conf import settings
from django.db import transaction
from stapel_core.signals import media_processed

from .conf import cdn_settings

logger = logging.getLogger(__name__)


def _image_ref_prefixes() -> set[str]:
    """Ref prefixes that route to ``Image`` — every configured
    ``STAPEL_CDN["ASSET_TYPES"]`` value, read fresh (not a frozen module
    constant) so overriding the config takes effect immediately.

    cdn-modularity.md §2.1/(a) parity: this used to be a second,
    independently hardcoded ``{"product", "avatar"}`` set — the exact same
    "half the stack is modular, half isn't" gap the spec calls out, just
    living in ``services.py`` instead of a client-side field. ``video``/
    ``file``/``audio`` are reserved prefixes for their own models, never
    valid ``ASSET_TYPES`` entries.
    """
    types = set()
    for entry in cdn_settings.ASSET_TYPES:
        types.add(entry if isinstance(entry, str) else entry[0])
    return types


class ImageProcessingService:
    """Service for processing and resizing images with watermarks using pyvips."""

    # Thumbnail/preview boundary of the historical single ladder; still used
    # as the fallback split when only a combined size list is available.
    THUMBNAIL_MAX_HEIGHT = 120

    # Default tier lists, kept as class attributes for introspection/tests.
    # The pipeline itself reads the (overridable) conf-driven lists via
    # get_thumbnail_sizes() / get_preview_sizes().
    # Thumbnail tiers (min-side resize, no watermark, high priority) —
    # sorted large to small for ladder downscaling.
    THUMBNAIL_SIZES = [
        ("120", 120),
        ("64", 64),
        ("32", 32),
        ("16", 16),
    ]

    # Preview tiers — two branches per tier ({T}w.webp / {T}h.webp), max
    # 1080, all WebP. Sorted large to small for ladder downscaling.
    PREVIEW_SIZES = [
        ("1080", 1080),
        ("720", 720),
        ("560", 560),
        ("480", 480),
        ("240", 240),
        ("160", 160),
    ]

    # Square-dedup epsilon (images-and-cdn.md §3.3): |w - h| <= 1px counts as
    # square (JPEG decode parity rounding) — only the w-branch is generated.
    SQUARE_EPSILON = 1

    WEBP_QUALITY = 85
    JPEG_QUALITY = 85

    @classmethod
    def get_thumbnail_sizes(cls) -> List[tuple]:
        """(name, size) thumbnail pairs from conf, sorted large to small."""
        sizes = [int(s) for s in cdn_settings.THUMBNAIL_SIZES]
        return [(str(s), s) for s in sorted(sizes, reverse=True)]

    @classmethod
    def get_preview_sizes(cls) -> List[tuple]:
        """(name, size) preview pairs from conf, sorted large to small."""
        sizes = [int(s) for s in cdn_settings.PREVIEW_SIZES]
        return [(str(s), s) for s in sorted(sizes, reverse=True)]

    @classmethod
    def _resize(cls, img: pyvips.Image, target: int, axis: str = "h") -> pyvips.Image:
        """Aspect-preserving downscale along one axis (images-and-cdn.md §3.2).

        ``axis``:
          - ``"w"``  — resize so that width == target;
          - ``"h"``  — resize so that height == target;
          - ``"min"`` — resize so that min(width, height) == target
            (thumbnail-class tiers, §3.4).

        No upscaling: returns the same image if the native side is already
        <= target.
        """
        if axis == "w":
            native = img.width
        elif axis == "min":
            native = min(img.width, img.height)
        else:
            native = img.height
        if native <= target:
            return img
        scale = target / native
        return img.resize(scale)

    @staticmethod
    def _merge_variants_meta(image_model, entries: List[dict], branches: tuple) -> None:
        """Replace this generation pass's slice of ``variants_meta``.

        Thumbnails own the ``branch is None`` entries, previews own the
        ``"w"``/``"h"`` ones — each pass replaces its own class wholesale
        (a re-run of previews on a now-square image also drops stale
        h-branch entries).
        """
        kept = [
            e
            for e in (image_model.variants_meta or [])
            if e.get("branch") not in branches
        ]
        image_model.variants_meta = kept + entries
        image_model.save(update_fields=["variants_meta", "updated_at"])

    @classmethod
    def _extract_embedded_thumbnail(cls, file_path: str) -> pyvips.Image | None:
        """
        Extract embedded thumbnail from image files.
        - HEIF/HEIC: ~512px embedded thumbnail
        - JPEG: EXIF thumbnail via shrink=8
        Returns None if extraction fails.
        """
        ext = os.path.splitext(file_path)[1].lower()

        # HEIF/HEIC - has larger embedded thumbnail (~512px)
        if ext in (".heif", ".heic"):
            try:
                return pyvips.Image.heifload(file_path, thumbnail=True)
            except Exception:
                return None

        # JPEG - try shrink=8 for fast decode
        if ext in (".jpg", ".jpeg"):
            try:
                return pyvips.Image.jpegload(file_path, shrink=8)
            except Exception:
                return None

        return None

    @classmethod
    def _add_watermark(cls, img: pyvips.Image) -> pyvips.Image:
        """Apply the configured watermark engine, if any.

        Off by default: ``STAPEL_CDN["WATERMARK"]`` is empty unless the
        host project points it at a callable (see stapel_cdn.watermarks).
        """
        engine = cdn_settings.WATERMARK
        return engine(img) if engine else img

    @classmethod
    def generate_thumbnails_only(cls, image_model) -> str:
        """
        Generate thumbnail tiers with MIN-SIDE resize (images-and-cdn.md §3.4):
        the smaller side of every thumbnail equals the tier, so a square
        avatar/grid slot is never upscaled regardless of orientation.
        Uses embedded thumbnail or shrink-on-load. Returns log string.
        """
        log_lines = []
        log_lines.append(
            f"[{datetime.now().isoformat()}] Starting thumbnail generation"
        )

        file_path = image_model.original.path
        output_dir = os.path.join(
            settings.MEDIA_ROOT, image_model.type, image_model.file_hash
        )
        os.makedirs(output_dir, exist_ok=True)

        thumbnail_sizes = cls.get_thumbnail_sizes()
        if not thumbnail_sizes:
            log_lines.append("  No thumbnail sizes configured, skipping")
            return "\n".join(log_lines)
        max_size = thumbnail_sizes[0][1]

        total_start = time.perf_counter()

        # Try embedded thumbnail first (HEIF ~512px, JPEG shrink=8)
        start = time.perf_counter()
        thumb = cls._extract_embedded_thumbnail(file_path)
        embed_time = int((time.perf_counter() - start) * 1000)

        if thumb and min(thumb.width, thumb.height) >= max_size:
            log_lines.append(
                f"  Embedded thumbnail: {thumb.width}x{thumb.height} ({embed_time}ms)"
            )
            current = cls._resize(thumb, max_size, axis="min").copy_memory()
        else:
            log_lines.append("  No embedded thumbnail, using shrink-on-load")
            start = time.perf_counter()
            probe = pyvips.Image.new_from_file(file_path, access="sequential")
            min_side = min(probe.width, probe.height)
            if min_side > max_size:
                # Shrink-on-load so the MIN side lands on the top tier:
                # constrain the width to width * (max_size / min_side).
                # NB: vips_thumbnail defaults `height` to `width` (square
                # bounding box) — pass an unbounded height so only the
                # width constrains the scale.
                target_width = math.ceil(probe.width * max_size / min_side)
                current = pyvips.Image.thumbnail(
                    file_path, target_width, height=10_000_000, size="down"
                )
            else:
                current = pyvips.Image.new_from_file(file_path)
            current = current.copy_memory()
            log_lines.append(
                f"  Load min-side {max_size}px: {int((time.perf_counter() - start) * 1000)}ms"
            )

        # Ladder by min side: e.g. 120 -> 64 -> 32 -> 16
        sizes_generated = []
        meta_entries = []
        for name, size in thumbnail_sizes:
            start = time.perf_counter()
            current = cls._resize(current, size, axis="min")
            current.webpsave(
                os.path.join(output_dir, f"{name}.webp"), Q=cls.WEBP_QUALITY
            )
            current = current.copy_memory()
            meta_entries.append(
                {
                    "tier": size,
                    "branch": None,
                    "url": image_model.get_variant_url(size),
                    "width": current.width,
                    "height": current.height,
                }
            )
            elapsed = int((time.perf_counter() - start) * 1000)
            sizes_generated.append(f"{name}={elapsed}ms")

        cls._merge_variants_meta(image_model, meta_entries, branches=(None,))

        total_time = int((time.perf_counter() - total_start) * 1000)
        log_lines.append(f"  Thumbnails: {', '.join(sizes_generated)}")
        log_lines.append(f"  Total thumbnail time: {total_time}ms")

        return "\n".join(log_lines)

    @classmethod
    def generate_previews_only(cls, image_model, apply_watermark: bool = True) -> str:
        """
        Generate preview tiers in TWO branches per tier (images-and-cdn.md
        §3.2): ``{T}w.webp`` (width == T) and ``{T}h.webp`` (height == T),
        each with its own ladder downscale. Square images (§3.3, 1px epsilon)
        generate only the w-branch — the metadata ``square`` flag tells the
        client any branch is equivalent. No upscaling anywhere: a branch
        whose native side is already <= T is saved as-is under the tier name.
        Returns log string.
        """
        log_lines = []
        log_lines.append(f"[{datetime.now().isoformat()}] Starting preview generation")

        file_path = image_model.original.path
        output_dir = os.path.join(
            settings.MEDIA_ROOT, image_model.type, image_model.file_hash
        )
        os.makedirs(output_dir, exist_ok=True)

        preview_sizes = cls.get_preview_sizes()
        if not preview_sizes:
            log_lines.append("  No preview sizes configured, skipping")
            return "\n".join(log_lines)
        max_size = preview_sizes[0][1]

        total_start = time.perf_counter()

        img_info = pyvips.Image.new_from_file(file_path, access="sequential")
        square = abs(img_info.width - img_info.height) <= cls.SQUARE_EPSILON
        branches = ("w",) if square else ("w", "h")
        if square:
            log_lines.append("  Square image: w-branch only (h is an alias, §3.3)")

        sizes_generated = []
        meta_entries = []
        for axis in branches:
            # Load per branch — shrink-on-load to the top tier along this axis.
            start = time.perf_counter()
            native = img_info.width if axis == "w" else img_info.height
            if native > max_size:
                # NB: vips_thumbnail defaults `height` to `width` (square
                # bounding box) — always pass BOTH, with the free axis
                # unbounded, so only the branch axis constrains the scale.
                if axis == "w":
                    current = pyvips.Image.thumbnail(
                        file_path, max_size, height=10_000_000, size="down"
                    )
                else:
                    # Unbounded width, height constrained to the top tier.
                    current = pyvips.Image.thumbnail(
                        file_path, 10_000_000, height=max_size, size="down"
                    )
                log_lines.append(
                    f"  [{axis}] {img_info.width}x{img_info.height} shrunk to "
                    f"{current.width}x{current.height} "
                    f"({int((time.perf_counter() - start) * 1000)}ms)"
                )
            else:
                current = pyvips.Image.new_from_file(file_path)
                log_lines.append(
                    f"  [{axis}] loaded as-is: {current.width}x{current.height} "
                    f"({int((time.perf_counter() - start) * 1000)}ms)"
                )
            current = current.copy_memory()

            # Ladder downscale along this branch's axis.
            for name, target in preview_sizes:
                start = time.perf_counter()
                before = (current.width, current.height)
                current = cls._resize(current, target, axis=axis)
                resized = (current.width, current.height) != before

                output = cls._add_watermark(current) if apply_watermark else current
                output.webpsave(
                    os.path.join(output_dir, f"{name}{axis}.webp"), Q=cls.WEBP_QUALITY
                )

                current = current.copy_memory()
                meta_entries.append(
                    {
                        "tier": target,
                        "branch": axis,
                        "url": image_model.get_variant_url(target, branch=axis),
                        "width": current.width,
                        "height": current.height,
                    }
                )

                elapsed = int((time.perf_counter() - start) * 1000)
                resize_info = (
                    f"resize to {target}{axis}"
                    if resized
                    else f"as-is {current.width}x{current.height}"
                )
                sizes_generated.append(f"{name}{axis}({resize_info})={elapsed}ms")

        cls._merge_variants_meta(image_model, meta_entries, branches=("w", "h"))

        total_time = int((time.perf_counter() - total_start) * 1000)
        log_lines.append(f"  Previews: {', '.join(sizes_generated)}")
        log_lines.append(f"  Total preview time: {total_time}ms")

        return "\n".join(log_lines)

    @classmethod
    def process_image(cls, image_model) -> str:
        """
        Process an image - extract metadata and generate all variants.
        Returns combined log string.
        """
        log_lines = []
        log_lines.append(f"=== Processing {image_model.file_hash[:8]} ===")

        file_path = image_model.original.path

        # Update dimensions if needed
        if image_model.original_width <= 1 or image_model.original_height <= 1:
            img = pyvips.Image.new_from_file(file_path, access="sequential")
            image_model.original_width = img.width
            image_model.original_height = img.height
            image_model.save(update_fields=["original_width", "original_height"])
            log_lines.append(f"Updated dimensions: {img.width}x{img.height}")

        # Generate thumbnails
        thumb_log = cls.generate_thumbnails_only(image_model)
        log_lines.append(thumb_log)

        # Generate previews
        preview_log = cls.generate_previews_only(image_model)
        log_lines.append(preview_log)

        # Mark as processed and save log
        combined_log = "\n".join(log_lines)
        image_model.is_processed = True
        image_model.processing_log = combined_log
        image_model.save(update_fields=["is_processed", "processing_log"])

        # Business milestone: variants generated — in-process extension point
        # for the host project (cache warm-up, denormalization, ...).
        media_processed.send(sender=type(image_model), instance=image_model)

        return combined_log


class VideoProcessingService:
    """Video variant/poster pipeline — a documented stub, not a promise.

    Same posture as ``stapel_geo.search.elasticsearch.
    ElasticsearchGeoSearchBackend`` (geo-v2-redesign.md §2.2): the real
    interface — extract a representative poster frame (images-and-cdn.md
    §5 render-metadata canon: same ``preview_b64``/``variants[]`` shape a
    poster would need to fill) plus the resolution ladder — needs an
    ffmpeg pipeline that does not exist yet (cdn-modularity.md §2.3 notes
    this gap is carried forward, not opened, by that spec).

    ``video`` is a VPS/prod-only submodule (cdn-modularity.md §3): it is
    never installed into the stapel-studio devcontainer, and turning it on
    via ``STAPEL_CDN["ENABLED_SUBMODULES"]`` is what makes
    ``checks.check_submodule_binaries``'s ffmpeg probe (tag ``stapel_cdn``)
    active for this deployment — the ffmpeg-gate this class itself does not
    yet act on, because there is no pipeline behind it to gate.
    """

    @classmethod
    def process_video(cls, video_model):
        """Process a video file - extract metadata and generate variants."""
        # TODO: Implement video processing with ffmpeg (poster frame +
        # resolution ladder, images-and-cdn.md §5 canon). Until then this
        # only marks bookkeeping — no variants, no poster are produced.
        video_model.is_processed = True
        video_model.save()
        return video_model


class AudioProcessingService:
    """Audio ("recordings" submodule) pipeline — storage now, compression later.

    cdn-modularity.md §7.2 (coordinator decision): recordings storage is
    unconditional passthrough — ``Audio.save()`` needs no processing to be
    usable. Compression is the opt-in half: ffmpeg-audio, gated by
    ``"recordings"`` in ``STAPEL_CDN["ENABLED_SUBMODULES"]`` (and, once
    implemented, by the presence of the ``ffmpeg`` binary — see
    ``checks.check_submodule_binaries``). Until a real compression pass
    exists, this stays a documented stub (same pattern as
    ``VideoProcessingService`` / ``stapel_geo.search.elasticsearch.
    ElasticsearchGeoSearchBackend``) — it never silently claims a recording
    was compressed when it wasn't.
    """

    @classmethod
    def compress_audio(cls, audio_model):
        """No-op stub: ``is_compressed`` stays False — nothing pretends to
        have compressed anything. Implement against ffmpeg-audio, then set
        ``is_compressed = True`` only once real output has been written."""
        logger.info(
            "AudioProcessingService.compress_audio: no-op stub (ffmpeg-audio "
            "pipeline not implemented) — audio %s left uncompressed "
            "(passthrough storage is already usable).",
            audio_model.file_hash,
        )
        return audio_model


def build_render_metadata(obj) -> dict:
    """Render-metadata snapshot (images-and-cdn.md §5) for Image/Video/File/Audio.

    The immutable form consumers denormalize once when they resolve a ref:
    ``{mime, bytes, width, height, aspect, duration_ms, preview_b64, square,
    variants[]}``. ``preview_b64`` is the 16px micro tier inlined as a data
    URI (blur-up placeholder, chat-design.md contract); ``variants`` is the
    persisted ``variants_meta`` plus the original file.
    """
    from .models import Audio, File, Image, Video

    if isinstance(obj, Image):
        return _image_render_metadata(obj)
    if isinstance(obj, Video):
        return _video_render_metadata(obj)
    if isinstance(obj, File):
        return _file_render_metadata(obj)
    if isinstance(obj, Audio):
        return _audio_render_metadata(obj)
    raise TypeError(f"build_render_metadata: unsupported object {type(obj)!r}")


def _guess_mime(filename_or_ext: str, fallback: str = "application/octet-stream") -> str:
    name = filename_or_ext
    if name.startswith("."):
        name = f"file{name}"
    mime, _ = mimetypes.guess_type(name)
    return mime or fallback


def _original_variant_entry(obj, width, height):
    try:
        url = obj.original.url if obj.original else None
    except Exception:  # storage without a URL — omit the entry
        url = None
    if url is None:
        return None
    return {
        "tier": "original",
        "branch": None,
        "url": url,
        "width": width,
        "height": height,
    }


def _image_render_metadata(image) -> dict:
    width = image.original_width or None
    height = image.original_height or None
    aspect = (width / height) if width and height else None
    square = (
        width is not None
        and height is not None
        and abs(width - height) <= ImageProcessingService.SQUARE_EPSILON
    )

    preview_b64 = None
    micro_path = os.path.join(
        settings.MEDIA_ROOT, image.type, image.file_hash, "16.webp"
    )
    if os.path.exists(micro_path):
        with open(micro_path, "rb") as fh:
            preview_b64 = "data:image/webp;base64," + base64.b64encode(
                fh.read()
            ).decode("ascii")

    variants = list(image.variants_meta or [])
    original_entry = _original_variant_entry(image, width, height)
    if original_entry is not None:
        variants.append(original_entry)

    return {
        "mime": _guess_mime(image.file_extension or image.original_filename),
        "bytes": image.original_size,
        "width": width,
        "height": height,
        "aspect": aspect,
        "duration_ms": None,
        "preview_b64": preview_b64,
        "square": square,
        "variants": variants,
    }


def _video_render_metadata(video) -> dict:
    width = video.original_width or None
    height = video.original_height or None
    return {
        "mime": _guess_mime(video.file_extension or video.original_filename),
        "bytes": video.original_size,
        "width": width,
        "height": height,
        "aspect": (width / height) if width and height else None,
        "duration_ms": int(video.duration * 1000) if video.duration else None,
        "preview_b64": None,
        "square": False,
        # Poster-frame variants follow the image rules once the ffmpeg
        # pipeline exists (images-and-cdn.md §5); until then — original only.
        "variants": [
            e
            for e in [_original_variant_entry(video, width, height)]
            if e is not None
        ],
    }


def _file_render_metadata(file_obj) -> dict:
    return {
        "mime": file_obj.mime_type
        or _guess_mime(file_obj.file_extension or file_obj.original_filename),
        "bytes": file_obj.original_size,
        "width": None,
        "height": None,
        "aspect": None,
        "duration_ms": None,
        "preview_b64": None,
        "square": False,
        "variants": [
            e for e in [_original_variant_entry(file_obj, None, None)] if e is not None
        ],
    }


def _audio_render_metadata(audio) -> dict:
    return {
        "mime": _guess_mime(audio.file_extension or audio.original_filename),
        "bytes": audio.original_size,
        "width": None,
        "height": None,
        "aspect": None,
        "duration_ms": int(audio.duration * 1000) if audio.duration else None,
        "preview_b64": None,
        "square": False,
        "variants": [
            e for e in [_original_variant_entry(audio, None, None)] if e is not None
        ],
    }


def _batch_resolve_media(ref_strings, for_update=False):
    """
    Batch-resolve ref strings to Image/Video/File/Audio instances.

    Ref format: <prefix>/<hash>
      - <any STAPEL_CDN["ASSET_TYPES"] entry>/<hash> → Image (default: avatar/<hash>)
      - video/<hash>                                 → Video
      - file/<hash>                                  → File
      - audio/<hash>                                 → Audio
    """
    from django.db.models import Q
    from .models import Audio, File, Image, Video

    image_prefixes = _image_ref_prefixes()
    image_lookups = {}
    video_lookups = {}
    file_lookups = {}
    audio_lookups = {}

    for ref_str in ref_strings:
        parts = ref_str.split("/")
        if len(parts) != 2:
            continue
        prefix, file_hash = parts
        if prefix in image_prefixes:
            image_lookups[(prefix, file_hash)] = ref_str
        elif prefix == "video":
            video_lookups[file_hash] = ref_str
        elif prefix == "file":
            file_lookups[file_hash] = ref_str
        elif prefix == "audio":
            audio_lookups[file_hash] = ref_str

    result = {}

    if image_lookups:
        q = Q()
        for img_type, file_hash in image_lookups:
            q |= Q(type=img_type, file_hash=file_hash)
        qs = Image.objects.filter(q)
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            key = (obj.type, obj.file_hash)
            if key in image_lookups:
                result[image_lookups[key]] = obj

    if video_lookups:
        qs = Video.objects.filter(file_hash__in=video_lookups.keys())
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in video_lookups:
                result[video_lookups[obj.file_hash]] = obj

    if file_lookups:
        qs = File.objects.filter(file_hash__in=file_lookups.keys())
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in file_lookups:
                result[file_lookups[obj.file_hash]] = obj

    if audio_lookups:
        qs = Audio.objects.filter(file_hash__in=audio_lookups.keys())
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in audio_lookups:
                result[audio_lookups[obj.file_hash]] = obj

    return result


def apply_ref_sync(
    service: str,
    entity_type: str,
    entity_id: str,
    old_hashes: List[str],
    new_hashes: List[str],
) -> dict:
    """
    Update refs JSONField on Image/Video/File objects.

    Called from RefSyncView (HTTP) and consume_cdn_events (Kafka consumer).
    Returns {"added": int, "removed": int, "errors": list[str]}.
    Errors contain ref strings that could not be resolved (asset not found in CDN).
    """
    ref_key = f"{service}/{entity_type}/{entity_id}"
    to_remove = set(old_hashes) - set(new_hashes)
    to_add = set(new_hashes) - set(old_hashes)

    if not to_remove and not to_add:
        return {"added": 0, "removed": 0, "errors": []}

    added = 0
    removed = 0
    errors: List[str] = []

    with transaction.atomic():
        resolved = _batch_resolve_media(to_remove | to_add, for_update=True)

        for ref_str in to_remove:
            obj = resolved.get(ref_str)
            if obj is None:
                errors.append(ref_str)
                continue
            if ref_key in obj.refs:
                obj.refs = [r for r in obj.refs if r != ref_key]
                obj.save(update_fields=["refs", "updated_at"])
                removed += 1

        for ref_str in to_add:
            obj = resolved.get(ref_str)
            if obj is None:
                errors.append(ref_str)
                continue
            if ref_key not in obj.refs:
                obj.refs = obj.refs + [ref_key]
                obj.save(update_fields=["refs", "updated_at"])
                added += 1

    if errors:
        logger.warning(
            "apply_ref_sync: unresolved refs for %s/%s/%s: %s",
            service,
            entity_type,
            entity_id,
            errors,
        )

    return {"added": added, "removed": removed, "errors": errors}
