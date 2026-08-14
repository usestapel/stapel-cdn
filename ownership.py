"""Who may see, reuse and keep a stored object.

Every intake path in this module is content-addressed: the SHA-256 of the
bytes is the identity of the object, and the same bytes uploaded twice are the
same object. That is a good storage property and a bad *authorization*
property, because "have these bytes been seen before?" is a question the
uploader is not entitled to ask about anybody else's bytes. A hash lookup
keyed on content alone turns every upload endpoint into an equality oracle
over the whole deployment — a caller who can guess or obtain a file learns
whether some other principal holds it, and gets that principal's row
(identifier, original filename, reference list, timestamps) in the response.

So dedup here is scoped, not global: a lookup answers only for the objects the
calling principal already owns. Identical bytes held by two principals are two
rows over one content-addressed blob — see :func:`shared_binary_exists` for
the refcount discipline that keeps one owner's deletion from unlinking bytes
the other one is still serving.

The second half of ownership is *how much* of it one principal may accumulate.
With a free-to-mint identity, "authenticated upload" and "open file hosting"
are the same sentence unless something counts, so the intake paths ask
:func:`quota_exceeded` before they create a row.
"""
from __future__ import annotations

from django.db.models import Q, Sum

from .conf import cdn_settings

#: A hash lookup matches only objects the calling principal owns.
SCOPE_OWNER = "owner"
#: A hash lookup matches any object with the same bytes, whoever uploaded it.
SCOPE_GLOBAL = "global"

VALID_DEDUP_SCOPES = (SCOPE_OWNER, SCOPE_GLOBAL)

def _owned_models():
    """Models an owner's quota is counted across."""
    from .models import File, Image, Video

    return (Image, Video, File)


def dedup_scope() -> str:
    """Configured dedup scope, normalised. Unknown values fall back to owner.

    Failing closed on a typo is the whole point: a misspelt scope must not
    silently reopen the cross-principal lookup. ``checks.W005`` reports the
    typo so it is fixed rather than tolerated.
    """
    value = str(cdn_settings.DEDUP_SCOPE or "").strip().lower()
    return value if value in VALID_DEDUP_SCOPES else SCOPE_OWNER


def owner_id(principal) -> int | None:
    """Primary key of a principal, or None when there is no identified one.

    ``AnonymousUser`` and an unsaved instance both answer None here — they are
    not *an* owner, so they can neither own nor inherit anything.
    """
    pk = getattr(principal, "pk", None)
    if pk is None or not getattr(principal, "is_authenticated", True):
        return None
    return pk


def dedup_scope_q(principal) -> Q:
    """Queryset filter confining a content-hash lookup to what ``principal`` may see.

    Returns an empty ``Q`` under ``DEDUP_SCOPE="global"`` (the caller opted
    into the deployment-wide pool). Under the default owner scope it pins the
    lookup to the principal's own rows, and a caller with no identity matches
    nothing at all rather than falling through to the service-owned rows that
    ``cdn.import_from_url`` writes with ``uploaded_by=None``.
    """
    if dedup_scope() == SCOPE_GLOBAL:
        return Q()
    pk = owner_id(principal)
    if pk is None:
        return Q(pk__in=[])
    return Q(uploaded_by_id=pk)


def service_scope_q() -> Q:
    """Dedup scope for the service-owned pool (``uploaded_by IS NULL``).

    ``cdn.import_from_url`` runs on behalf of an opaque caller id with no user
    row behind it. Its pool is its own: it neither reads nor is read by any
    end user's objects.
    """
    if dedup_scope() == SCOPE_GLOBAL:
        return Q()
    return Q(uploaded_by__isnull=True)


def shared_binary_exists(instance) -> bool:
    """Whether another row still points at ``instance``'s content-addressed blob.

    Two principals holding identical bytes share one path under
    ``<type>/<hash>/``. Unlinking on the first delete would silently break the
    object the other one is still serving, so deletion asks this first.
    """
    model = type(instance)
    siblings = model.objects.filter(file_hash=instance.file_hash).exclude(pk=instance.pk)
    if hasattr(instance, "type"):
        siblings = siblings.filter(type=instance.type)
    return siblings.exists()


def owner_usage(principal) -> tuple[int, int]:
    """``(objects, bytes)`` currently stored for ``principal`` across all media."""
    pk = owner_id(principal)
    if pk is None:
        return (0, 0)
    objects = 0
    total = 0
    for model in _owned_models():
        qs = model.objects.filter(uploaded_by_id=pk)
        objects += qs.count()
        total += qs.aggregate(total=Sum("original_size"))["total"] or 0
    return (objects, total)


def quota_exceeded(principal, incoming_bytes: int = 0) -> dict | None:
    """Report which per-owner ceiling ``incoming_bytes`` would cross, if any.

    Returns ``None`` when the upload fits, otherwise a detail dict naming the
    limit — the intake path turns that into a 403 the caller can act on
    (delete something) instead of an opaque refusal.

    An unidentified principal is not quota-checked here: it cannot own a row,
    so it has no usage to bound. Whether such a caller may upload at all is a
    permission decision, made by the view.
    """
    max_objects = int(cdn_settings.MAX_OBJECTS_PER_OWNER or 0)
    max_bytes = int(cdn_settings.MAX_BYTES_PER_OWNER or 0)
    if not max_objects and not max_bytes:
        return None
    if owner_id(principal) is None:
        return None

    objects, used = owner_usage(principal)
    if max_objects and objects + 1 > max_objects:
        return {"limit": "objects", "max": max_objects, "used": objects}
    if max_bytes and used + int(incoming_bytes or 0) > max_bytes:
        return {"limit": "bytes", "max": max_bytes, "used": used}
    return None


__all__ = [
    "SCOPE_GLOBAL",
    "SCOPE_OWNER",
    "VALID_DEDUP_SCOPES",
    "dedup_scope",
    "dedup_scope_q",
    "owner_id",
    "owner_usage",
    "quota_exceeded",
    "service_scope_q",
    "shared_binary_exists",
]
