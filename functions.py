"""
comm Function providers of the cdn module.

Registered from ``CdnConfig.ready()`` (importing this module is enough:
re-imports are no-ops and re-registering the same handler object is
idempotent). Other modules call these by name via
``stapel_core.comm.call`` — no import of this package needed:

    from stapel_core.comm import call

    call("cdn.media_exists", {"ref": "product/<hash>"})
    call("cdn.refs_sync", {"service": "shop", "entity_type": "product",
                           "entity_id": "42", "new_hashes": [...]})
"""
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
