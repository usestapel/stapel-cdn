"""Render metadata — everything a UI needs to draw an attachment, once.

The requirement this module exists for
--------------------------------------
A chat page renders an attachment with no second round trip and no layout
jump. That means the moment a consumer resolves a media ref it must already
hold: the aspect box to reserve, the byte size to display, something to show
*while* the real bytes load, and — for time-based media — how long it runs.
Anything missing from that list becomes either a request the renderer makes
mid-scroll or a box that resizes under the reader's thumb.

    images   → width, height, aspect, bytes, a 16px WebP LQIP (blur-up)
    GIFs     → the same, plus ``animated: true``
    video    → the same, plus duration and a poster frame
    voice    → duration and a rendered waveform strip
    documents→ mime type and extension

One pass, not a second decode
-----------------------------
The 16px micro tier is not decoded a second time to produce ``preview_b64``:
``ImageProcessingService.generate_thumbnails_only`` already encodes that
exact file at the bottom of its ladder, and now keeps the encoded buffer —
the same bytes are written to disk *and* base64'd onto the row. Video pays
one ffmpeg frame extraction that produces the poster and its micro preview
together; audio pays one ffmpeg render for the waveform. Nothing in the
snapshot is recomputed per render — a consumer denormalizes it once when it
resolves the ref (SERVICE-BACKLOG §35 item 9а).

The byte budget
---------------
Base64 in a JSON payload is a page-weight decision, not a cosmetic one: a
chat page with 40 attachments multiplies whatever this module allows by 40.
``STAPEL_CDN["MICRO_PREVIEW_MAX_BYTES"]`` (default **4096 bytes**, measured
on the finished ``data:`` URI — what actually lands in the JSON, base64
expansion included) is the ceiling, and it is enforced by
**downgrade-then-refuse**, never by truncation:

1. re-encode down the quality ladder (WebP Q85 → Q60 → Q40);
2. for waveforms, re-render at the smaller strip size;
3. still over → **no preview at all**, with ``meta_reason:
   "preview_over_budget"``.

A truncated base64 string is a broken image in every consumer; a null with a
named reason is a placeholder the consumer already knows how to draw. The
ceiling is also applied on *read* (``build_render_metadata``), so lowering
the setting takes effect for rows stamped before the change instead of
quietly shipping the old, larger payloads.

Degradation is always named
---------------------------
Every snapshot carries ``meta_status`` (``"ok"`` / ``"partial"`` /
``"missing"``) and ``meta_reason`` — one of :data:`REASONS`. There is no
path that returns an empty field with no explanation: a voice message
without a waveform says ``ffmpeg_missing``, an image whose variants have not
run yet says ``not_generated``, and a deployment that is missing a tool
altogether is told at boot by ``checks`` E001/E002/E003/W010.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os

from django.conf import settings

from .conf import cdn_settings
from .kinds import (
    PREVIEW_BLUR,
    PREVIEW_POSTER,
    PREVIEW_WAVEFORM,
    classify_object,
)
from .probes import (
    REASON_FFMPEG_MISSING,
    REASON_FFPROBE_MISSING,
    REASON_PROBE_FAILED,
    REASON_RENDER_FAILED,
    REASON_TOOL_TIMEOUT,
)

logger = logging.getLogger(__name__)

# --- named reasons ---------------------------------------------------------

#: The processing pass has not run yet (fresh upload, queued variants).
REASON_NOT_GENERATED = "not_generated"
#: pyvips/libvips is not importable — nothing can encode a preview.
REASON_DECODER_MISSING = "decoder_missing"
#: A preview was produced but no encoding of it fits the byte budget.
REASON_PREVIEW_OVER_BUDGET = "preview_over_budget"
#: The stored original is gone or unreadable.
REASON_SOURCE_MISSING = "source_missing"
#: The preview encoder itself failed on readable input.
REASON_ENCODE_FAILED = "encode_failed"

#: Every value ``meta_reason`` can take. Part of the ``cdn.describe``
#: contract — consumers may branch on these strings.
REASONS = (
    REASON_NOT_GENERATED,
    REASON_DECODER_MISSING,
    REASON_PREVIEW_OVER_BUDGET,
    REASON_SOURCE_MISSING,
    REASON_ENCODE_FAILED,
    REASON_FFPROBE_MISSING,
    REASON_FFMPEG_MISSING,
    REASON_PROBE_FAILED,
    REASON_RENDER_FAILED,
    REASON_TOOL_TIMEOUT,
)

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
META_STATUSES = (STATUS_OK, STATUS_PARTIAL, STATUS_MISSING)

#: Quality ladder tried when an encoding is over budget, best first.
WEBP_QUALITY_LADDER = (85, 60, 40)

#: Fallback default, used when the setting is unusable (checks.W011 reports
#: the setting; the module stays bounded either way).
DEFAULT_PREVIEW_BUDGET = 4096


def preview_budget() -> int:
    """Effective ``data:`` URI byte ceiling for an inline preview."""
    raw = cdn_settings.MICRO_PREVIEW_MAX_BYTES
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return DEFAULT_PREVIEW_BUDGET


def _data_uri(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def encode_preview(
    raw: bytes | None = None,
    *,
    vips_image=None,
    encoded_webp: bytes | None = None,
    budget: int | None = None,
) -> tuple[str | None, str]:
    """Produce a budget-bounded ``data:`` URI for an inline preview.

    Exactly one source is used, in this order of preference:

    * ``encoded_webp`` — bytes this pipeline *already* encoded (the micro
      thumbnail tier). Reused as-is when it fits: no second encode, which is
      the whole point of the single-pass rule;
    * ``vips_image`` — a live pyvips image, re-encoded down the quality
      ladder until it fits;
    * ``raw`` — encoded bytes from another tool (ffmpeg's PNG output),
      decoded once by libvips and treated like ``vips_image``.

    Returns ``(data_uri, reason)``. ``reason`` is ``""`` on a clean result,
    otherwise one of :data:`REASONS`; ``data_uri`` is ``None`` whenever the
    reason is a refusal. Never truncates.
    """
    limit = budget if budget is not None else preview_budget()

    if encoded_webp:
        uri = _data_uri(encoded_webp, "image/webp")
        if len(uri) <= limit:
            return uri, ""
        # Fall through: re-encode from pixels if we can, else refuse.

    try:
        import pyvips
    except ImportError:
        pyvips = None

    if pyvips is None:
        # No decoder at all. Raw PNG straight from ffmpeg is still a usable
        # preview if it happens to fit — a named downgrade (PNG, not WebP),
        # never a silent one.
        if raw:
            uri = _data_uri(raw, "image/png")
            if len(uri) <= limit:
                return uri, REASON_DECODER_MISSING
            return None, REASON_PREVIEW_OVER_BUDGET
        return None, REASON_DECODER_MISSING

    image = vips_image
    if image is None and raw:
        try:
            image = pyvips.Image.new_from_buffer(raw, "")
        except Exception as exc:  # unreadable bytes from the other tool
            logger.warning("encode_preview: cannot decode source bytes: %s", exc)
            return None, REASON_ENCODE_FAILED
    if image is None:
        if encoded_webp:
            return None, REASON_PREVIEW_OVER_BUDGET
        return None, REASON_NOT_GENERATED

    for quality in WEBP_QUALITY_LADDER:
        try:
            buffer = image.webpsave_buffer(Q=quality)
        except Exception as exc:
            logger.warning("encode_preview: webp encode failed (Q=%s): %s", quality, exc)
            return None, REASON_ENCODE_FAILED
        uri = _data_uri(buffer, "image/webp")
        if len(uri) <= limit:
            return uri, ""
    return None, REASON_PREVIEW_OVER_BUDGET


def within_budget(data_uri: str | None) -> bool:
    """Whether a stored ``data:`` URI still fits the *current* budget."""
    return bool(data_uri) and len(data_uri) <= preview_budget()


# --- snapshot --------------------------------------------------------------


def guess_mime(filename_or_ext: str, fallback: str = "application/octet-stream") -> str:
    name = filename_or_ext or ""
    if name.startswith("."):
        name = f"file{name}"
    mime, _ = mimetypes.guess_type(name)
    return mime or fallback


def _normalized_ext(obj) -> str:
    ext = (obj.file_extension or "").lower()
    if not ext:
        ext = os.path.splitext(obj.original_filename or "")[1].lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return ext


def _aspect(width, height):
    if not width or not height:
        return None
    return round(width / height, 6)


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


def media_ref(obj) -> str:
    """The ``<prefix>/<hash>`` ref that resolves back to *obj*."""
    from .models import Audio, File, Image, Video

    if isinstance(obj, Image):
        return f"{obj.type}/{obj.file_hash}"
    if isinstance(obj, Video):
        return f"video/{obj.file_hash}"
    if isinstance(obj, Audio):
        return f"audio/{obj.file_hash}"
    if isinstance(obj, File):
        return f"file/{obj.file_hash}"
    raise TypeError(f"media_ref: unsupported object {type(obj)!r}")


def _preview_for(obj, kind):
    """``(preview_b64, preview_kind, reason)`` for a stored object.

    Reads the value stamped at ingest, re-checked against the *current*
    budget: a deployment that lowers ``MICRO_PREVIEW_MAX_BYTES`` stops
    shipping the older, larger payloads immediately instead of after a
    backfill.
    """
    preview_kind = kind.preview if kind else None
    if preview_kind is None:
        return None, None, ""
    stored = getattr(obj, "preview_b64", "") or ""
    if not stored:
        reason = getattr(obj, "meta_reason", "") or REASON_NOT_GENERATED
        return None, preview_kind, reason
    if not within_budget(stored):
        return None, preview_kind, REASON_PREVIEW_OVER_BUDGET
    return stored, preview_kind, getattr(obj, "meta_reason", "") or ""


def _status(kind, *, width, height, duration_ms, preview_b64, reason) -> str:
    """``ok`` only when everything this kind promises is actually present."""
    required = []
    preview_kind = kind.preview if kind else None
    if preview_kind == PREVIEW_BLUR:
        required = [width, height, preview_b64]
    elif preview_kind == PREVIEW_POSTER:
        required = [width, height, duration_ms, preview_b64]
    elif preview_kind == PREVIEW_WAVEFORM:
        required = [duration_ms, preview_b64]

    if not required:
        # Documents: mime + extension are read off the row itself and are
        # always known, so there is nothing that can be partially there.
        return STATUS_MISSING if reason else STATUS_OK
    present = [value for value in required if value not in (None, "", 0)]
    if len(present) == len(required) and not reason:
        return STATUS_OK
    return STATUS_PARTIAL if present else STATUS_MISSING


def _snapshot(
    obj,
    *,
    kind,
    mime,
    width=None,
    height=None,
    duration_ms=None,
    square=False,
    variants=None,
    poster_url=None,
    extra_reason="",
) -> dict:
    preview_b64, preview_kind, preview_reason = _preview_for(obj, kind)
    reason = extra_reason or preview_reason
    return {
        "ref": media_ref(obj),
        "kind": kind.name if kind else None,
        "mime": mime,
        "ext": _normalized_ext(obj),
        "bytes": obj.original_size,
        "width": width,
        "height": height,
        "aspect": _aspect(width, height),
        "square": bool(square),
        "animated": bool(kind.animated) if kind else False,
        "duration_ms": duration_ms,
        "preview_b64": preview_b64,
        "preview_kind": preview_kind,
        "poster_url": poster_url,
        "meta_status": _status(
            kind,
            width=width,
            height=height,
            duration_ms=duration_ms,
            preview_b64=preview_b64,
            reason=reason,
        ),
        "meta_reason": reason or None,
        "variants": variants or [],
    }


def _image_snapshot(image) -> dict:
    from .services import ImageProcessingService

    width = image.original_width or None
    height = image.original_height or None
    square = (
        width is not None
        and height is not None
        and abs(width - height) <= ImageProcessingService.SQUARE_EPSILON
    )
    variants = list(image.variants_meta or [])
    original_entry = _original_variant_entry(image, width, height)
    if original_entry is not None:
        variants.append(original_entry)

    return _snapshot(
        image,
        kind=classify_object(image),
        mime=guess_mime(image.file_extension or image.original_filename),
        width=width,
        height=height,
        square=square,
        variants=variants,
    )


def _video_snapshot(video) -> dict:
    width = video.original_width or None
    height = video.original_height or None
    variants = [
        entry
        for entry in [_original_variant_entry(video, width, height)]
        if entry is not None
    ]
    return _snapshot(
        video,
        kind=classify_object(video),
        mime=guess_mime(video.file_extension or video.original_filename),
        width=width,
        height=height,
        duration_ms=int(video.duration * 1000) if video.duration else None,
        variants=variants,
        poster_url=video.poster_url,
    )


def _audio_snapshot(audio) -> dict:
    variants = [
        entry
        for entry in [_original_variant_entry(audio, None, None)]
        if entry is not None
    ]
    return _snapshot(
        audio,
        kind=classify_object(audio),
        mime=audio.mime_type
        or guess_mime(audio.file_extension or audio.original_filename),
        duration_ms=int(audio.duration * 1000) if audio.duration else None,
        variants=variants,
    )


def _file_snapshot(file_obj) -> dict:
    variants = [
        entry
        for entry in [_original_variant_entry(file_obj, None, None)]
        if entry is not None
    ]
    return _snapshot(
        file_obj,
        kind=classify_object(file_obj),
        mime=file_obj.mime_type
        or guess_mime(file_obj.file_extension or file_obj.original_filename),
        variants=variants,
    )


#: Ceiling on one describe batch, shared by every caller of it: the
#: ``cdn.describe_many`` comm Function and the ``POST /describe/`` HTTP
#: endpoint. Each snapshot can carry an inline preview up to
#: ``STAPEL_CDN["MICRO_PREVIEW_MAX_BYTES"]``, so an unbounded batch is an
#: unbounded response — this keeps the worst case at (limit x budget),
#: 200 KB with both defaults.
DESCRIBE_MANY_LIMIT = 50


def build_render_metadata(obj) -> dict:
    """The render-metadata snapshot for one stored object.

    Shape (images-and-cdn.md §5, extended by the metadata pipeline)::

        {
          "ref": "avatar/<hash>",       # what resolves back to this object
          "kind": "image",              # media-kind registry (kinds.py)
          "mime": "image/jpeg",
          "ext": ".jpg",
          "bytes": 51234,
          "width": 1600, "height": 900,
          "aspect": 1.777778,           # width / height, 6dp
          "square": false,
          "animated": false,
          "duration_ms": null,          # video/audio only
          "preview_b64": "data:image/webp;base64,...",
          "preview_kind": "blur",       # blur | poster | waveform | null
          "poster_url": null,           # video only, once a poster exists
          "meta_status": "ok",          # ok | partial | missing
          "meta_reason": null,          # named reason when not "ok"
          "variants": [{tier, branch, url, width, height}, ...]
        }

    Consumers denormalize this ONCE when they resolve a ref (a chat
    attachment, a catalog card) — it is not meant to be recomputed per
    render.
    """
    from .models import Audio, File, Image, Video

    if isinstance(obj, Image):
        return _image_snapshot(obj)
    if isinstance(obj, Video):
        return _video_snapshot(obj)
    if isinstance(obj, File):
        return _file_snapshot(obj)
    if isinstance(obj, Audio):
        return _audio_snapshot(obj)
    raise TypeError(f"build_render_metadata: unsupported object {type(obj)!r}")


# --- derived-file locations ------------------------------------------------


def derived_dir(prefix: str, file_hash: str) -> str:
    """``MEDIA_ROOT/<prefix>/<hash>/`` — where derived files for an object go."""
    return os.path.join(settings.MEDIA_ROOT, prefix, file_hash)


def derived_url(prefix: str, file_hash: str, filename: str) -> str:
    return f"{settings.MEDIA_URL}{prefix}/{file_hash}/{filename}"


__all__ = [
    "DEFAULT_PREVIEW_BUDGET",
    "DESCRIBE_MANY_LIMIT",
    "META_STATUSES",
    "REASONS",
    "REASON_DECODER_MISSING",
    "REASON_ENCODE_FAILED",
    "REASON_NOT_GENERATED",
    "REASON_PREVIEW_OVER_BUDGET",
    "REASON_SOURCE_MISSING",
    "STATUS_MISSING",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "WEBP_QUALITY_LADDER",
    "build_render_metadata",
    "derived_dir",
    "derived_url",
    "encode_preview",
    "guess_mime",
    "media_ref",
    "preview_budget",
    "within_budget",
]
