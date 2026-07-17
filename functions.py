"""
comm Function providers of the cdn module.

Registered from ``CdnConfig.ready()`` (importing this module is enough:
re-imports are no-ops and re-registering the same handler object is
idempotent). Other modules call these by name via
``stapel_core.comm.call`` — no import of this package needed:

    from stapel_core.comm import call

    call("cdn.media_exists", {"ref": "product/<hash>"})
    call("cdn.describe", {"ref": "product/<hash>"})
    call("cdn.refs_sync", {"service": "shop", "entity_type": "product",
                           "entity_id": "42", "new_hashes": [...]})
"""
import hashlib
import logging

from stapel_core.comm import function

logger = logging.getLogger(__name__)

MEDIA_EXISTS_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {
            "type": "string",
            "description": "Media reference in <type>/<id> form, e.g. product/<hash>",
        },
    },
    "required": ["ref"],
}

REFS_SYNC_SCHEMA = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "entity_type": {"type": "string"},
        "entity_id": {"type": ["string", "integer"]},
        "old_hashes": {"type": "array", "items": {"type": "string"}},
        "new_hashes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["service", "entity_type", "entity_id"],
}

IMPORT_FROM_URL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "https:// URL of the image to import"},
        "image_type": {
            "type": "string",
            "description": "CDN image type, e.g. 'avatar' or 'product'",
        },
        "caller": {
            "type": ["string", "null"],
            "description": "Opaque caller id for per-caller rate limiting (e.g. user id)",
        },
    },
    "required": ["url", "image_type"],
}


DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {
            "type": "string",
            "description": "Media reference in <type>/<id> form, e.g. product/<hash>",
        },
    },
    "required": ["ref"],
}


@function("cdn.describe", schema=DESCRIBE_SCHEMA)
def describe(payload: dict) -> dict:
    """Render-metadata snapshot for a media ref (images-and-cdn.md §5).

    Payload: ``{"ref": "<type>/<id>"}``. Returns the immutable snapshot
    ``{mime, bytes, width, height, aspect, duration_ms, preview_b64, square,
    variants[]}`` — ``preview_b64`` is the 16px micro tier as a
    ``data:image/webp;base64,...`` URI, ``variants`` carries per-tier
    per-branch geometry (thumbnails: ``branch: null`` min-side; previews:
    ``"w"``/``"h"``; square images only ``"w"`` with ``square: true``) plus
    the original. Consumers denormalize this ONCE when resolving a ref
    (chat attachment, catalog card) — it is not meant to be recomputed per
    render.

    Raises ``LookupError`` for an unknown ref (surfaced to the caller as a
    ``FunctionCallError``) — a missing asset is the caller's placeholder
    case, not an empty snapshot.
    """
    from .services import _batch_resolve_media, build_render_metadata

    ref = payload["ref"]
    resolved = _batch_resolve_media([ref])
    if ref not in resolved:
        raise LookupError(f"cdn.describe: unknown media ref {ref!r}")
    return build_render_metadata(resolved[ref])


@function("cdn.media_exists", schema=MEDIA_EXISTS_SCHEMA)
def media_exists(payload: dict) -> dict:
    """Check whether a media ref resolves to a stored CDN asset.

    Payload: ``{"ref": "<type>/<id>"}`` where <type> is an image type
    (product, avatar), ``video`` or ``file``, and <id> is the file hash.
    Returns ``{"exists": bool}``. Same resolution logic as the refs sync
    service / FileExistsView.
    """
    from .services import _batch_resolve_media

    ref = payload["ref"]
    resolved = _batch_resolve_media([ref])
    return {"exists": ref in resolved}


@function("cdn.import_from_url", schema=IMPORT_FROM_URL_SCHEMA)
def import_from_url(payload: dict) -> dict:
    """Fetch an external image over an SSRF-hardened path and store it on CDN.

    Payload: ``{"url": "https://...", "image_type": "avatar",
    "caller": "<opaque id>"}``. Returns ``{"ref": "<image_type>/<hash>"}``
    pointing at a stored asset with resize variants generated exactly like a
    normal upload.

    Security posture (this is an outbound-egress SSRF sink — the URL is
    attacker-influenced): all hardening lives in :mod:`stapel_cdn.fetch`
    (https-only, DNS→IP allowlisting re-run per redirect, IP-pinned connect
    to defeat rebinding, size/timeout caps, magic-byte content check,
    per-caller rate limit). This function is intentionally a comm Function,
    **not** an HTTP endpoint, so it cannot be driven as an open proxy from
    the outside.

    Fails closed: any validation/fetch/format problem raises
    ``ImageImportError`` (surfaced to the caller as a ``FunctionCallError``);
    there is no code path that returns a ref for an unvalidated source.
    """
    from django.core.files.base import ContentFile

    from .conf import cdn_settings
    from .fetch import (
        ImageImportError,
        detect_image_extension,
        enforce_rate_limit,
        fetch_image_bytes,
    )
    from .models import Image
    from .validators import validate_image_file

    url = payload["url"]
    image_type = payload["image_type"]
    caller = payload.get("caller")

    # image_type must be a configured CDN asset type (avatar, ... — open,
    # cdn-modularity.md §2.1: STAPEL_CDN["ASSET_TYPES"], same key the
    # client-side stapel_core.django.cdn field checks read).
    valid_types = set()
    for entry in cdn_settings.ASSET_TYPES:
        valid_types.add(entry if isinstance(entry, str) else entry[0])
    if image_type not in valid_types:
        raise ImageImportError("invalid_image_type", f"unknown image_type {image_type!r}")

    # Rate-limit BEFORE any DNS/network work — open-proxy / amplification guard.
    enforce_rate_limit(caller)

    data = fetch_image_bytes(url)
    ext = detect_image_extension(data)

    file_hash = hashlib.sha256(data).hexdigest()

    # Content-addressed dedup: the same bytes for the same type reuse the ref.
    existing = Image.objects.filter(file_hash=file_hash, type=image_type).first()
    if existing:
        return {"ref": f"{image_type}/{file_hash}"}

    content = ContentFile(data, name=f"import_{file_hash[:16]}{ext}")

    # Route through the same validator the upload endpoints use
    # (ALLOWED_IMAGE_EXTENSIONS + Pillow decode) for parity/defence-in-depth.
    from django.core.exceptions import ValidationError

    try:
        validate_image_file(content)
    except ValidationError as exc:
        raise ImageImportError("invalid_image_file", str(exc)) from exc

    image = Image.objects.create(
        file_hash=file_hash,
        original_filename=content.name,
        file_extension=ext,
        type=image_type,
        original=content,
        original_size=content.size,
        uploaded_by=None,
    )
    return {"ref": f"{image_type}/{image.file_hash}"}


@function("cdn.refs_sync", schema=REFS_SYNC_SCHEMA)
def refs_sync(payload: dict) -> dict:
    """Sync entity → media reference tracking.

    comm equivalent of the ``RefSyncView`` HTTP endpoint; delegates to
    ``services.apply_ref_sync``. Returns
    ``{"added": int, "removed": int, "errors": [ref, ...]}``.
    """
    from .services import apply_ref_sync

    return apply_ref_sync(
        service=payload["service"],
        entity_type=payload["entity_type"],
        entity_id=str(payload["entity_id"]),
        old_hashes=list(payload.get("old_hashes") or []),
        new_hashes=list(payload.get("new_hashes") or []),
    )
