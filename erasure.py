"""Subject-scoped erasure (GDPR Art. 17) — the slice stapel-cdn owns.

stapel-gdpr 0.5.0 keys an erasure by a *subject* (``{subject_type,
subject_key}``) instead of by an account. This module is the whole answer
for the media owner (``section = "media"``); ``actions.py`` is only the
transport (``gdpr.erasure.requested`` / ``gdpr.owner.probe``).

**How a subject is found here.** Storage is content-addressed and this
module holds no foreign keys: a stored object is named by its *media ref*
``<prefix>/<hash>`` (``avatar/ab12…``, ``video/…``, ``file/…``,
``audio/…``), and the entities that use it are recorded on the row's
``refs`` list as ``<service>/<entity_type>/<entity_id>`` (the format
``services.apply_ref_sync`` writes — the same strings hosts send through
``cdn.refs_sync``). That list IS the reverse index, so a `recording` or a
`workspace` erasure needs nothing extra in the request: the entity type and
id in ``subject_key`` are exactly what the ref keys are built from. Nobody
has to ship a list of refs in metadata.

Two erasure disciplines, matching what the row means:

- **A `file` subject names the bytes** (its ref is the object's identity),
  so every row over those bytes goes and the blob with them. A host that
  asks for this has already dropped its own reference; any reference still
  attached is counted and logged, never silently kept as a reason to
  refuse — the request is about content, and content is what is destroyed.
- **A `recording` / `workspace` subject names an entity**, so the entity's
  reference is dropped from every object that carries it, and an object
  left with no references at all is destroyed. An object another entity is
  still using keeps serving — that is the same refcount discipline account
  erasure has always followed (``purge_unreferenced``), and unlinking a
  content-addressed blob out from under a second holder is the failure it
  exists to prevent.

Every entry point returns a counts mapping and is idempotent: a redelivered
request finds nothing left to remove and receipts zeros.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: This module's name in ``STAPEL_GDPR["DATA_OWNERS"]`` — the same string as
#: ``CDNGDPRProvider.section``, so one owner has one name whichever protocol
#: reaches it.
OWNER = "media"

#: Subject types this owner claims; ``gdpr.owner.alive`` reports this list
#: and gdpr opens a part for this owner only for these.
SUBJECT_TYPES = ("account", "workspace", "file", "recording")

#: entity_type segment of a ref key, per entity subject.
_ENTITY_TYPES = {"recording": "recording", "workspace": "workspace"}


def erase(subject_type: str, subject_key, *, workspace_id=None) -> dict:
    """Erase everything this module owns about one subject; return the counts.

    An unclaimed subject type raises :class:`ValueError` — a typo must not
    receipt as an empty success, which would certify an erasure nobody
    performed.
    """
    if subject_type == "account":
        return erase_account(subject_key)
    if subject_type == "file":
        return erase_file(str(subject_key))
    if subject_type in _ENTITY_TYPES:
        return erase_entity(_ENTITY_TYPES[subject_type], str(subject_key))
    raise ValueError(f"stapel-cdn does not own subject type {subject_type!r}")


def erase_account(user_id) -> dict:
    """Destroy the user's unreferenced media and un-own the rest.

    Delegates to the GDPR provider, which is the account policy of record:
    orphan objects are destroyed (rows + bytes), objects other content still
    references keep serving with ``uploaded_by`` nulled, and a blob that
    could not be unlinked raises ``MediaErasureIncomplete`` rather than
    reporting an erasure that did not happen.
    """
    from .gdpr import CDNGDPRProvider

    counts = CDNGDPRProvider().delete(user_id)
    logger.info("cdn: media erased for account %s (%s)", user_id, counts)
    return counts


def erase_file(ref: str) -> dict:
    """Destroy the object a media ref names: every row over those bytes, and
    the bytes themselves.

    ``ref`` is the string this module hands out (``<prefix>/<hash>``) — the
    same value ``cdn.import_from_url`` returns and hosts store. An
    unparseable or unknown prefix raises: erasing nothing while receipting
    success is how a request completes over data that is still there.
    """
    model, lookup = _resolve_ref(ref)
    counts = _zero_counts()
    rows = list(model.objects.filter(**lookup))
    if not rows:
        logger.info("cdn: media ref %s already absent — erasure is a no-op", ref)
        return counts

    for row in rows:
        stranded = len(row.refs if isinstance(row.refs, list) else [])
        if stranded:
            # Not a refusal: the request names this content, and the host is
            # responsible for having released its own references first. Say
            # loudly which ones are now dangling instead of leaving the bytes.
            logger.warning(
                "cdn: erasing %s with %d live reference(s) still attached: %s",
                ref, stranded, row.refs,
            )
            counts["refs_stranded"] += stranded
        _destroy(row, counts)
    logger.info("cdn: media ref %s erased (%s)", ref, counts)
    return counts


def erase_entity(entity_type: str, entity_id: str) -> dict:
    """Drop an entity's references everywhere, and destroy what it was the
    last holder of.

    The reverse lookup is the ``refs`` list itself: a key is
    ``<service>/<entity_type>/<entity_id>``, so every service's reference to
    this entity matches, whichever module wrote it.
    """
    from django.db import transaction

    counts = _zero_counts()
    for model in media_models():
        # An erasure is rare and correctness beats a scan: rows carrying no
        # reference at all cannot match, so they are excluded in the database
        # and the remainder is matched in Python (JSON containment is not
        # portable across the backends this module supports).
        for row in model.objects.exclude(refs=[]).iterator():
            matching = [r for r in row.refs if _matches(r, entity_type, entity_id)]
            if not matching:
                continue
            remaining = [r for r in row.refs if r not in matching]
            counts["refs_removed"] += len(matching)
            with transaction.atomic():
                if remaining:
                    row.refs = remaining
                    row.save(update_fields=["refs", "updated_at"])
                    counts["objects_kept_referenced"] += 1
                else:
                    _destroy(row, counts)
    logger.info("cdn: %s %s erased (%s)", entity_type, entity_id, counts)
    return counts


# ── internals ────────────────────────────────────────────────────────


def _zero_counts() -> dict:
    return {
        "objects_removed": 0,
        "blobs_unlinked": 0,
        "refs_removed": 0,
        "refs_stranded": 0,
        "objects_kept_referenced": 0,
    }


def media_models() -> tuple:
    from .models import Audio, File, Image, Video

    return (Image, Video, File, Audio)


def _matches(ref_key: str, entity_type: str, entity_id: str) -> bool:
    """Whether a ``<service>/<entity_type>/<entity_id>`` key names this
    entity. The service segment is deliberately not constrained: two modules
    may both reference the same recording, and both references have to go."""
    parts = str(ref_key).split("/")
    return len(parts) == 3 and parts[1] == entity_type and parts[2] == str(entity_id)


def _resolve_ref(ref: str):
    """``(model, filter kwargs)`` for a ``<prefix>/<hash>`` media ref.

    Every row over those bytes matches, not just one: identical bytes held by
    two principals are two rows over one blob (``ownership`` docstring), and
    a ref names the content.
    """
    from .models import Audio, File, Image, Video
    from .services import _image_ref_prefixes

    parts = str(ref).split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"media ref {ref!r} is not '<prefix>/<hash>' — refusing to receipt "
            "an erasure that could not be located"
        )
    prefix, file_hash = parts
    if prefix in _image_ref_prefixes():
        return Image, {"type": prefix, "file_hash": file_hash}
    if prefix == "video":
        return Video, {"file_hash": file_hash}
    if prefix == "file":
        return File, {"file_hash": file_hash}
    if prefix == "audio":
        return Audio, {"file_hash": file_hash}
    raise ValueError(
        f"unknown media ref prefix {prefix!r} (ref {ref!r}) — not an asset type "
        "this deployment serves"
    )


def _destroy(row, counts: dict) -> None:
    """Delete one row, unlinking its blob when nothing else holds those bytes.

    Same fail-closed rule as the account path: a blob that cannot be unlinked
    keeps its row, because the row is the only record of where the file is —
    deleting it would turn a failed erasure into personal data nobody can
    ever find again.
    """
    from .gdpr import MediaErasureIncomplete
    from .ownership import shared_binary_exists

    if not shared_binary_exists(row):
        try:
            row.original.delete(save=False)
        except Exception as exc:
            logger.error(
                "erasure incomplete: could not unlink %s %s (%s) — the row is "
                "kept so the file stays findable: %s",
                type(row).__name__, row.pk, row.original.name, exc,
            )
            raise MediaErasureIncomplete(
                f"{type(row).__name__} {row.pk} still holds its bytes on disk; "
                f"its row was kept so the file remains findable. This subject "
                f"is NOT erased."
            ) from exc
        counts["blobs_unlinked"] += 1
    row.delete()
    counts["objects_removed"] += 1


__all__ = [
    "OWNER",
    "media_models",
    "SUBJECT_TYPES",
    "erase",
    "erase_account",
    "erase_entity",
    "erase_file",
]
