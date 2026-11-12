"""Who may receive a URL for a stored object — one decision for every door.

A private object (``File`` / ``Audio`` originals under
``PRIVATE_MEDIA_PREFIX``) and a protected one (a watermarked image's original
under ``PROTECTED_MEDIA_PREFIX``) are fetchable only through a signed link
(:mod:`stapel_cdn.protected`). Minting that link is the capability; this
module decides who gets it, and every path of this library that turns a stored
object into a URL for a caller asks :func:`may_receive_urls` first:

* ``POST /describe/`` (:func:`stapel_cdn.services.describe_refs` with the
  request's :class:`Viewer`);
* every model serializer's ``original_url`` and ``render_meta`` (upload
  responses, ``file/exists``, ``images/<type>/random``), through
  :func:`viewer_from_context`;
* the admin's original link (:func:`viewer_from_request`).

The comm Functions (``cdn.describe`` / ``cdn.describe_many``) answer as
:data:`SERVICE`: another service asks over the internal channel, and the
consuming module owns the access decision for its own readers (a chat answers
participants only).

Who passes, for a private or protected object:

* a service caller (``request.is_service_request``, or the comm Function);
* the object's owner — a principal holding a row for the same ref (identical
  bytes uploaded by two principals are two rows over one blob, and each of
  them owns the ref);
* staff, only when ``STAPEL_CDN["PRIVATE_URL_AUDIENCE"]`` is
  ``"owner_and_staff"``;
* anyone the endpoint's guard admits, only when it is ``"any"`` — the
  behaviour before this decision existed, an explicit opt-out
  (``checks.W017``).

Everyone else gets the object's non-sensitive metadata and no URL to its
bytes: ``meta_reason: "not_authorized"`` (:func:`withhold`). A public object
is unchanged for everyone.

What withholding does NOT cover: a watermarked image's marked renditions are
public by design — they live on the public media route at paths derived from
the ref — so they stay in the snapshot for every caller; the watermark is
their protection. A protected original and a clean copy are never in an HTTP
snapshot at all (only ``cdn.describe`` with ``clean`` mints those).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .conf import cdn_settings
from .metadata import REASON_NOT_AUTHORIZED

#: ``PRIVATE_URL_AUDIENCE`` values.
AUDIENCE_OWNER = "owner"
AUDIENCE_OWNER_AND_STAFF = "owner_and_staff"
AUDIENCE_ANY = "any"
AUDIENCES = (AUDIENCE_OWNER, AUDIENCE_OWNER_AND_STAFF, AUDIENCE_ANY)

#: Visibility of a stored object, as ``cdn.ownership`` reports it.
VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_PROTECTED = "protected"
VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_PRIVATE, VISIBILITY_PROTECTED)



@dataclass(frozen=True)
class Viewer:
    """The caller a URL would be handed to."""

    user: Any = None
    service: bool = False


#: A service-to-service caller (the comm Functions).
SERVICE = Viewer(service=True)
#: Nobody in particular: gets public URLs only.
NOBODY = Viewer()


def audience() -> str:
    """Configured audience, normalised. Anything unknown is ``"owner"``."""
    value = str(cdn_settings.PRIVATE_URL_AUDIENCE or "").strip().lower()
    return value if value in AUDIENCES else AUDIENCE_OWNER


def viewer_from_request(request) -> Viewer:
    if request is None:
        return NOBODY
    if bool(getattr(request, "is_service_request", False)):
        return SERVICE
    return Viewer(user=getattr(request, "user", None))


def viewer_from_context(context) -> Viewer:
    """The viewer a serializer renders for.

    An explicit ``context["cdn_viewer"]`` wins, then ``context["request"]``.
    A serializer rendered with neither is rendering for nobody in particular
    and gets public URLs only — fail closed, so a host that serializes a
    private object without saying for whom never leaks its link.
    """
    context = context or {}
    viewer = context.get("cdn_viewer")
    if isinstance(viewer, Viewer):
        return viewer
    return viewer_from_request(context.get("request"))


def visibility(obj) -> str:
    """``private`` / ``protected`` / ``public`` for a stored object, read off
    where its original is stored (a prefix denied on the public route)."""
    from .protected import is_private_name, is_protected_name

    try:
        name = str(obj.original.name or "") if obj.original else ""
    except Exception:  # noqa: BLE001 - no file
        name = ""
    if is_protected_name(name):
        return VISIBILITY_PROTECTED
    if is_private_name(name):
        return VISIBILITY_PRIVATE
    return VISIBILITY_PUBLIC


def is_restricted(obj) -> bool:
    return visibility(obj) != VISIBILITY_PUBLIC


def _user_pk(user):
    from .ownership import owner_id

    return owner_id(user)


def may_receive_urls(obj, viewer: Viewer, *, holds: bool | None = None) -> bool:
    """Whether ``viewer`` may be handed a URL to ``obj``'s restricted bytes.

    ``holds`` short-cuts the ownership lookup when the caller already batched
    it (:func:`held_refs`); ``None`` asks the database for this one object.
    """
    if not is_restricted(obj):
        return True
    if viewer.service:
        return True
    mode = audience()
    if mode == AUDIENCE_ANY:
        return True
    user = viewer.user
    pk = _user_pk(user)
    if pk is None:
        return False
    if mode == AUDIENCE_OWNER_AND_STAFF and bool(getattr(user, "is_staff", False)):
        return True
    if getattr(obj, "uploaded_by_id", None) == pk:
        return True
    if holds is None:
        from .metadata import media_ref

        holds = media_ref(obj) in held_refs([media_ref(obj)], user)
    return bool(holds)


def held_refs(refs: Iterable[str], user) -> set[str]:
    """The subset of ``refs`` for which ``user`` holds a row — one query per
    model. Content addressing makes a ref name bytes, not a row: two
    principals who uploaded identical bytes each hold the ref."""
    pk = _user_pk(user)
    if pk is None:
        return set()
    return {ref for ref, owners in owners_by_ref(refs).items() if pk in owners}


def owners_by_ref(refs: Iterable[str]) -> dict[str, set]:
    """``{ref: {uploaded_by_id, ...}}`` for every ref that resolves; the
    service-owned pool contributes ``None``. One query per model."""
    from django.db.models import Q

    from .models import Audio, File, Image, Video
    from .services import image_ref_prefixes

    image_prefixes = image_ref_prefixes()
    images: dict[tuple, str] = {}
    by_model: dict[Any, dict[str, str]] = {Video: {}, File: {}, Audio: {}}
    plain = {"video": Video, "file": File, "audio": Audio}
    for ref in dict.fromkeys(refs or []):
        parts = str(ref).split("/")
        if len(parts) != 2 or not parts[1]:
            continue
        prefix, file_hash = parts
        if prefix in image_prefixes:
            images[(prefix, file_hash)] = ref
        elif prefix in plain:
            by_model[plain[prefix]][file_hash] = ref

    out: dict[str, set] = {}
    if images:
        q = Q()
        for image_type, file_hash in images:
            q |= Q(type=image_type, file_hash=file_hash)
        for image_type, file_hash, owner in Image.objects.filter(q).values_list(
            "type", "file_hash", "uploaded_by_id"
        ):
            ref = images.get((image_type, file_hash))
            if ref is not None:
                out.setdefault(ref, set()).add(owner)
    for model, lookups in by_model.items():
        if not lookups:
            continue
        for file_hash, owner in model.objects.filter(
            file_hash__in=list(lookups)
        ).values_list("file_hash", "uploaded_by_id"):
            out.setdefault(lookups[file_hash], set()).add(owner)
    return out


#: Snapshot keys that carry a URL to the object's bytes.
_SNAPSHOT_URL_KEYS = ("poster_url",)


def withhold(snapshot: dict, obj) -> dict:
    """``snapshot`` with every URL to ``obj``'s restricted bytes removed.

    For a private object that is every URL it has (its original is its only
    stored file). For a protected image only the original entry and clean
    copies go — the marked renditions are public by design (module doc).
    The metadata stays, ``meta_reason`` says why the URLs are absent and
    ``meta_status`` is never ``ok`` without them.
    """
    out = dict(snapshot)
    if visibility(obj) == VISIBILITY_PROTECTED:
        variants = []
        for entry in out.get("variants") or []:
            if entry.get("tier") == "original":
                continue
            entry = dict(entry)
            entry.pop("clean_url", None)
            variants.append(entry)
        out["variants"] = variants
    else:
        out["variants"] = []
        for key in _SNAPSHOT_URL_KEYS:
            if key in out:
                out[key] = None
    out["meta_reason"] = REASON_NOT_AUTHORIZED
    if out.get("meta_status") == "ok":
        out["meta_status"] = "partial"
    return out


def snapshot_for(obj, viewer: Viewer, *, holds: bool | None = None, clean: bool = False) -> dict:
    """The render-metadata snapshot of ``obj`` as ``viewer`` may see it."""
    from .metadata import build_render_metadata

    snapshot = build_render_metadata(obj, clean=clean and viewer.service)
    if may_receive_urls(obj, viewer, holds=holds):
        return snapshot
    return withhold(snapshot, obj)


def original_url_for(obj, viewer: Viewer, url_of):
    """``url_of(obj)`` when ``viewer`` may have it, else ``None``."""
    if not may_receive_urls(obj, viewer):
        return None
    return url_of(obj)


__all__ = [
    "AUDIENCES",
    "AUDIENCE_ANY",
    "AUDIENCE_OWNER",
    "AUDIENCE_OWNER_AND_STAFF",
    "NOBODY",
    "REASON_NOT_AUTHORIZED",
    "SERVICE",
    "VISIBILITIES",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_PROTECTED",
    "VISIBILITY_PUBLIC",
    "Viewer",
    "audience",
    "held_refs",
    "is_restricted",
    "may_receive_urls",
    "original_url_for",
    "owners_by_ref",
    "snapshot_for",
    "viewer_from_context",
    "viewer_from_request",
    "visibility",
    "withhold",
]
