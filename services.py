"""
Service layer for stapel-cdn - handles file processing and variant generation.
Uses pyvips for fast image processing with ladder downscaling optimization.
"""

from __future__ import annotations

import logging
import math
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
from .metadata import DESCRIBE_MANY_LIMIT
from .metadata import build_render_metadata as build_render_metadata  # re-export
from .metadata import encode_preview, preview_budget

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
    def _merge_variants_meta(
        image_model,
        entries: List[dict],
        branches: tuple,
        extra_fields: dict | None = None,
    ) -> None:
        """Replace this generation pass's slice of ``variants_meta``.

        Thumbnails own the ``branch is None`` entries, previews own the
        ``"w"``/``"h"`` ones — each pass replaces its own class wholesale
        (a re-run of previews on a now-square image also drops stale
        h-branch entries). ``extra_fields`` is stamped in the SAME write —
        the thumbnail pass uses it for ``preview_b64``/``meta_reason``, so
        the geometry and the placeholder it was derived from can never be
        half-committed.
        """
        kept = [
            e
            for e in (image_model.variants_meta or [])
            if e.get("branch") not in branches
        ]
        image_model.variants_meta = kept + entries
        update_fields = ["variants_meta", "updated_at"]
        for name, value in (extra_fields or {}).items():
            setattr(image_model, name, value)
            update_fields.append(name)
        image_model.save(update_fields=update_fields)

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
        micro_tier = thumbnail_sizes[-1][1]  # smallest configured tier
        micro_buffer = None
        sizes_generated = []
        meta_entries = []
        for name, size in thumbnail_sizes:
            start = time.perf_counter()
            current = cls._resize(current, size, axis="min")
            # Encode ONCE into memory, then write those same bytes to disk.
            # The micro tier's buffer is what becomes ``preview_b64`` — the
            # blur-up placeholder costs no second decode and no re-read of
            # the file just written (SERVICE-BACKLOG §35 item 9а: the
            # micro-preview is generated in the SAME libvips pass).
            buffer = current.webpsave_buffer(Q=cls.WEBP_QUALITY)
            with open(os.path.join(output_dir, f"{name}.webp"), "wb") as handle:
                handle.write(buffer)
            if size == micro_tier:
                micro_buffer = buffer
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

        preview_b64, reason = encode_preview(
            encoded_webp=micro_buffer, vips_image=current
        )
        if preview_b64:
            log_lines.append(
                f"  Inline preview: {len(preview_b64)}B data URI "
                f"(budget {preview_budget()}B)"
            )
        else:
            log_lines.append(f"  Inline preview REFUSED: {reason}")
        cls._merge_variants_meta(
            image_model,
            meta_entries,
            branches=(None,),
            extra_fields={
                "preview_b64": preview_b64 or "",
                "meta_reason": reason or "",
            },
        )

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


def _source_path(media_model):
    """Filesystem path of a stored original, or ``None`` if it is not there.

    A row whose blob is gone is a real state (storage swapped, file pruned
    by hand) and it must degrade with ``source_missing`` rather than be
    reported as a media tool failure — the two ask for completely different
    operator actions.
    """
    try:
        path = media_model.original.path if media_model.original else None
    except Exception:  # storage backend with no local path
        return None
    if not path or not os.path.exists(path):
        return None
    return path


class VideoProcessingService:
    """Video metadata + poster pass (ffmpeg), and nothing it cannot do.

    What this produces, in ONE ffprobe call plus ONE frame extraction:
    ``original_width``/``original_height``/``duration`` (measured, not
    guessed), a derived poster frame at
    ``MEDIA_ROOT/video/<hash>/poster.webp`` for the player, and the inline
    micro poster (``preview_b64``) a chat bubble draws before anything is
    fetched.

    What it still does NOT produce: the transcoded resolution ladder
    (``variant_240``…``variant_2160``). Those FileFields stay empty and this
    class does not pretend otherwise — a rendition pipeline is a different
    piece of work with different operational costs, and claiming it here
    would repeat the exact defect ``variants_status`` exists for.

    ``video`` is a VPS/prod-only submodule (cdn-modularity.md §3), never
    installed into the stapel-studio devcontainer; ``"video"`` in
    ``STAPEL_CDN["ENABLED_SUBMODULES"]`` turns on
    ``checks.check_submodule_binaries``'s ffmpeg probe, which is now the
    boot-time warning that this pass will degrade.
    """

    @classmethod
    def process_video(cls, video_model):
        """Extract measured metadata and a poster frame. Idempotent.

        Sets ``is_processed`` only when real facts were measured. Anything
        missing leaves it False and records the named reason in
        ``meta_reason`` so ``cdn_backfill_media_meta`` / ``retry_unprocessed``
        can pick the row up once the deployment has the binary.
        """
        from .metadata import (
            REASON_SOURCE_MISSING,
        )
        from .probes import MediaToolUnavailable, extract_poster_png, probe_media

        update_fields = ["meta_reason", "updated_at"]
        reason = ""

        path = _source_path(video_model)
        if path is None:
            video_model.meta_reason = REASON_SOURCE_MISSING
            video_model.save(update_fields=update_fields)
            logger.warning(
                "VideoProcessingService: no readable original for video %s — "
                "metadata left unmeasured (%s)",
                video_model.file_hash,
                REASON_SOURCE_MISSING,
            )
            return video_model

        measured = False
        try:
            facts = probe_media(path)
        except MediaToolUnavailable as exc:
            reason = exc.reason
            logger.warning(
                "VideoProcessingService: ffprobe unavailable for video %s (%s: %s)",
                video_model.file_hash, exc.reason, exc.detail,
            )
        else:
            video_model.original_width = facts["width"]
            video_model.original_height = facts["height"]
            if facts["duration_ms"] is not None:
                video_model.duration = facts["duration_ms"] / 1000.0
            update_fields += ["original_width", "original_height", "duration"]
            measured = True

        # Poster frame — the same extraction feeds the full-size poster file
        # and the inline micro preview; the video is decoded once.
        try:
            poster_png = extract_poster_png(
                path, at_seconds=float(cdn_settings.POSTER_FRAME_AT or 0)
            )
        except MediaToolUnavailable as exc:
            reason = reason or exc.reason
            logger.warning(
                "VideoProcessingService: no poster for video %s (%s: %s)",
                video_model.file_hash, exc.reason, exc.detail,
            )
        else:
            poster_reason = cls._write_poster(video_model, poster_png)
            update_fields += ["preview_b64", "has_poster"]
            reason = reason or poster_reason

        video_model.meta_reason = reason
        if measured:
            video_model.is_processed = True
            update_fields.append("is_processed")
        video_model.save(update_fields=sorted(set(update_fields)))
        return video_model

    @classmethod
    def _write_poster(cls, video_model, poster_png: bytes) -> str:
        """Write ``poster.webp`` and stamp the inline micro poster.

        Returns a named reason, or ``""``. The PNG that ffmpeg produced is
        decoded once: the same in-memory image is downscaled for the poster
        file and again for the inline preview.
        """
        from .metadata import REASON_DECODER_MISSING, derived_dir, encode_preview

        # Imported here, not read off the module global, so "this deployment
        # has no decoder" is evaluated at call time — the same way
        # ``decoders``/``encode_preview`` evaluate it.
        try:
            import pyvips  # noqa: F811 - deliberate call-time probe
        except ImportError:
            pyvips = None

        if pyvips is None:
            # No libvips: the poster frame cannot be re-encoded to WebP at
            # all. The inline path can still fall back to the raw PNG if it
            # fits the budget — encode_preview names that downgrade.
            preview_b64, reason = encode_preview(raw=poster_png)
            video_model.preview_b64 = preview_b64 or ""
            video_model.has_poster = False
            return reason or REASON_DECODER_MISSING

        try:
            frame = pyvips.Image.new_from_buffer(poster_png, "")
        except Exception as exc:
            logger.warning(
                "VideoProcessingService: undecodable poster frame for %s: %s",
                video_model.file_hash, exc,
            )
            video_model.has_poster = False
            video_model.preview_b64 = ""
            from .metadata import REASON_ENCODE_FAILED

            return REASON_ENCODE_FAILED

        output_dir = derived_dir("video", video_model.file_hash)
        os.makedirs(output_dir, exist_ok=True)
        poster = ImageProcessingService._resize(
            frame, int(cdn_settings.POSTER_MAX_WIDTH), axis="w"
        )
        poster.webpsave(
            os.path.join(output_dir, video_model.POSTER_FILENAME),
            Q=ImageProcessingService.WEBP_QUALITY,
        )
        video_model.has_poster = True

        micro_tier = min(int(size) for size in cdn_settings.THUMBNAIL_SIZES)
        micro = ImageProcessingService._resize(frame, micro_tier, axis="min")
        preview_b64, reason = encode_preview(vips_image=micro)
        video_model.preview_b64 = preview_b64 or ""
        return reason


class AudioProcessingService:
    """Audio ("recordings" submodule): metadata + waveform now, compression later.

    cdn-modularity.md §7.2: recordings storage is unconditional passthrough
    — ``Audio.save()`` needs no processing to be usable. Two separate
    optional passes sit on top of it:

    * :meth:`extract_metadata` — duration (ffprobe) plus the rendered
      waveform strip a voice message shows in a chat bubble (ffmpeg's
      ``showwavespic``). Degrades with a named reason when ffmpeg is absent;
    * :meth:`compress_audio` — still a documented stub. It never claims a
      recording was compressed when it wasn't.
    """

    @classmethod
    def extract_metadata(cls, audio_model):
        """Measure duration and render the waveform strip. Idempotent.

        Both halves are attempted independently: a file ffprobe can time but
        ffmpeg cannot draw still gets its duration, and the missing half is
        named in ``meta_reason`` rather than left as an unexplained null.
        """
        from .metadata import REASON_SOURCE_MISSING
        from .probes import MediaToolUnavailable, probe_media

        update_fields = ["meta_reason", "updated_at"]
        reason = ""

        path = _source_path(audio_model)
        if path is None:
            audio_model.meta_reason = REASON_SOURCE_MISSING
            audio_model.save(update_fields=update_fields)
            logger.warning(
                "AudioProcessingService: no readable original for audio %s (%s)",
                audio_model.file_hash, REASON_SOURCE_MISSING,
            )
            return audio_model

        try:
            facts = probe_media(path)
        except MediaToolUnavailable as exc:
            reason = exc.reason
            logger.warning(
                "AudioProcessingService: ffprobe unavailable for audio %s (%s: %s)",
                audio_model.file_hash, exc.reason, exc.detail,
            )
        else:
            if facts["duration_ms"] is not None:
                audio_model.duration = facts["duration_ms"] / 1000.0
                update_fields.append("duration")

        preview_b64, waveform_reason = cls._render_waveform(path)
        audio_model.preview_b64 = preview_b64 or ""
        update_fields.append("preview_b64")
        reason = reason or waveform_reason

        audio_model.meta_reason = reason
        audio_model.save(update_fields=sorted(set(update_fields)))
        return audio_model

    @classmethod
    def _render_waveform(cls, path: str) -> tuple[str | None, str]:
        """Render the strip at the first configured size that fits the budget.

        The downgrade ladder is explicit: each ``WAVEFORM_SIZES`` entry is
        rendered and encoded (which itself walks the WebP quality ladder);
        the first result inside ``MICRO_PREVIEW_MAX_BYTES`` wins. Only when
        every size is still too large does this refuse, with
        ``preview_over_budget``.
        """
        from .metadata import REASON_PREVIEW_OVER_BUDGET, encode_preview
        from .probes import MediaToolUnavailable, render_waveform_png

        color = str(cdn_settings.WAVEFORM_COLOR)
        last_reason = REASON_PREVIEW_OVER_BUDGET
        for width, height in cdn_settings.WAVEFORM_SIZES:
            try:
                png = render_waveform_png(path, int(width), int(height), color)
            except MediaToolUnavailable as exc:
                logger.warning(
                    "AudioProcessingService: waveform render failed (%s: %s)",
                    exc.reason, exc.detail,
                )
                return None, exc.reason
            preview_b64, reason = encode_preview(raw=png)
            if preview_b64:
                return preview_b64, reason
            last_reason = reason
            if reason != REASON_PREVIEW_OVER_BUDGET:
                # A decoder/encoder problem is not fixed by drawing smaller.
                return None, reason
        return None, last_reason

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


# ``build_render_metadata`` moved to ``stapel_cdn.metadata`` in 0.16.0, where
# it grew the kind registry, the byte budget and the named degradation
# reasons. It is imported at the top of this module and re-exported here
# because every existing caller (functions.py, host projects) already
# reaches for ``services.build_render_metadata``.


class DescribeBatchTooLarge(ValueError):
    """More refs in one describe batch than :data:`DESCRIBE_MANY_LIMIT`.

    Carries the numbers so each caller can say which limit and by how much:
    the comm Function lets it surface as a ``FunctionCallError``, the HTTP
    endpoint turns it into ``error.400.too_many_refs`` with ``count``/``max``
    in the params.
    """

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(
            f"{count} refs exceeds the per-call limit of {limit} — page the batch"
        )


def describe_refs(refs) -> dict:
    """``{"items": {ref: snapshot}, "missing": [ref, ...]}`` for a page of refs.

    THE batch describe body — the comm Function ``cdn.describe_many`` and the
    ``POST /cdn/api/v1/describe/`` endpoint are both thin wrappers over this,
    so a browser and a service caller get the same snapshots, the same
    deduplication and the same ceiling rather than two implementations that
    drift.

    Duplicates collapse before the ceiling is applied, so asking for the same
    attachment twice costs one slot. A ref that does not resolve — deleted,
    never existed, or malformed (no ``<prefix>/<hash>`` shape) — comes back in
    ``missing``: a page with one dead attachment still renders the other
    thirty-nine. Raises :class:`DescribeBatchTooLarge` above
    :data:`~stapel_cdn.metadata.DESCRIBE_MANY_LIMIT`; every snapshot may
    inline a preview, so batch size IS response size.
    """
    deduped = list(dict.fromkeys(refs or []))
    if len(deduped) > DESCRIBE_MANY_LIMIT:
        raise DescribeBatchTooLarge(len(deduped), DESCRIBE_MANY_LIMIT)
    resolved = _batch_resolve_media(deduped)
    return {
        "items": {
            ref: build_render_metadata(obj) for ref, obj in resolved.items()
        },
        "missing": [ref for ref in deduped if ref not in resolved],
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
        qs = Image.objects.filter(q).order_by("created_at", "pk")
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            key = (obj.type, obj.file_hash)
            if key in image_lookups and image_lookups[key] not in result:
                result[image_lookups[key]] = obj

    if video_lookups:
        qs = Video.objects.filter(file_hash__in=video_lookups.keys()).order_by(
            "created_at", "pk"
        )
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in video_lookups and video_lookups[obj.file_hash] not in result:
                result[video_lookups[obj.file_hash]] = obj

    if file_lookups:
        qs = File.objects.filter(file_hash__in=file_lookups.keys()).order_by(
            "created_at", "pk"
        )
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in file_lookups and file_lookups[obj.file_hash] not in result:
                result[file_lookups[obj.file_hash]] = obj

    if audio_lookups:
        qs = Audio.objects.filter(file_hash__in=audio_lookups.keys()).order_by(
            "created_at", "pk"
        )
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in audio_lookups and audio_lookups[obj.file_hash] not in result:
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
