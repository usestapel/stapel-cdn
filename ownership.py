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

from .conf import DEFAULTS, cdn_settings

#: A hash lookup matches only objects the calling principal owns.
SCOPE_OWNER = "owner"
#: A hash lookup matches any object with the same bytes, whoever uploaded it.
SCOPE_GLOBAL = "global"

VALID_DEDUP_SCOPES = (SCOPE_OWNER, SCOPE_GLOBAL)

#: The one way to switch a per-owner ceiling off, spelled out.
QUOTA_UNLIMITED = "unlimited"

#: Settings keys holding a per-owner ceiling.
QUOTA_CEILING_KEYS = ("MAX_OBJECTS_PER_OWNER", "MAX_BYTES_PER_OWNER")

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

    ``AnonymousUser`` and anything else without a primary key answer None here
    — they are not *an* owner, so they can neither own nor inherit anything,
    and :func:`quota_exceeded` refuses rather than exempts them.
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


def quota_ceiling(key: str) -> int | None:
    """Resolve one per-owner ceiling from settings. ``None`` means no ceiling.

    Switching a ceiling off has to be *said*: the value ``"unlimited"``, and
    nothing else, does it. The previous rule was ``int(setting or 0)`` with 0
    meaning unbounded, so ``0``, ``None``, ``""`` and a missing key all landed
    on "no ceiling" — three of the four by accident rather than by intent. A
    storage ceiling that removes itself on a typo, an empty environment
    variable or a refactor that drops a key is not a ceiling.

    So anything that is neither ``"unlimited"`` nor a positive whole number
    falls back to the shipped default, which is the safe direction, and
    ``checks.W007`` names the value at boot so it is fixed rather than
    silently absorbed.
    """
    raw = getattr(cdn_settings, key)
    if isinstance(raw, str) and raw.strip().lower() == QUOTA_UNLIMITED:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(DEFAULTS[key])
    return value if value > 0 else int(DEFAULTS[key])


def quota_exceeded(principal, incoming_bytes: int = 0) -> dict | None:
    """Report which per-owner ceiling ``incoming_bytes`` would cross, if any.

    Returns ``None`` when the upload fits, otherwise a detail dict naming the
    limit — the intake path turns that into a 403 the caller can act on
    (delete something) instead of an opaque refusal.

    A principal the quota cannot attribute an object to is **refused**, not
    exempted. It used to return ``None`` here on the reasoning that such a
    caller "has no usage to bound" — which is true, and is exactly the
    problem: with nothing to count against it, its ceiling is infinite, so the
    one caller the quota cannot measure was the one caller it did not bound.
    Storing bytes nobody can be billed, quota'd or GDPR-erased for is not a
    thing this module should do quietly, so the ceilings being configured at
    all is enough to require an owner.
    """
    max_objects = quota_ceiling("MAX_OBJECTS_PER_OWNER")
    max_bytes = quota_ceiling("MAX_BYTES_PER_OWNER")
    if max_objects is None and max_bytes is None:
        return None
    if owner_id(principal) is None:
        return {"limit": "owner", "max": 0, "used": 0}

    objects, used = owner_usage(principal)
    if max_objects and objects + 1 > max_objects:
        return {"limit": "objects", "max": max_objects, "used": objects}
    if max_bytes and used + int(incoming_bytes or 0) > max_bytes:
        return {"limit": "bytes", "max": max_bytes, "used": used}
    return None


__all__ = [
    "QUOTA_CEILING_KEYS",
    "QUOTA_UNLIMITED",
    "SCOPE_GLOBAL",
    "SCOPE_OWNER",
    "VALID_DEDUP_SCOPES",
    "dedup_scope",
    "dedup_scope_q",
    "owner_id",
    "owner_usage",
    "quota_ceiling",
    "quota_exceeded",
    "service_scope_q",
    "shared_binary_exists",
]
