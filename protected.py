"""Protected and private media: bytes that are stored but never on the public
media route.

When a watermark applies to an image, what the public may fetch is the marked
renditions only. The stored original and the clean copy of each rendition live
under ``STAPEL_CDN["PROTECTED_MEDIA_PREFIX"]`` (default ``protected/``), which
the operator denies on the public media route, and are reachable through two
doors only:

* a short-lived signed URL (:func:`signed_url`), minted for internal consumers
  by ``cdn.describe`` with ``{"clean": true}`` — a comm Function, so only a
  service can ask for one; the URL itself is fetchable by whoever holds it
  (a vision provider) until it expires;
* the uploader's own original download (``images/<type>/<hash>/original/``).

Private media (``STAPEL_CDN["PRIVATE_MEDIA_PREFIX"]``, default ``private/``:
documents, archives, voice notes) follows the same rule for the URL this
library hands out. :func:`stored_url` is the one decision, and
``storage.OverwriteStorage.url`` calls it, so every ``<field>.url`` of a
stored object — serializers, describe snapshots, admin — gets a signed link
for a private or protected name and never the public media path. Only
``PRIVATE_MEDIA_URLS = "public"`` (a deliberate opt-out, ``checks.W016``)
hands out the public path for private names; protected names are signed
whatever it says.

This module decides what a URL looks like; :mod:`stapel_cdn.access` decides
who is handed one.
"""
from __future__ import annotations

import os

from django.conf import settings
from django.core import signing
from django.urls import NoReverseMatch, reverse

from .conf import cdn_settings

SIGNING_SALT = "stapel_cdn.protected"
CLEAN_DIR = "clean"


def protected_prefix() -> str:
    """``PROTECTED_MEDIA_PREFIX`` normalised to ``"x/"``; never empty."""
    prefix = str(cdn_settings.PROTECTED_MEDIA_PREFIX or "").strip().strip("/")
    return f"{prefix or 'protected'}/"


def private_prefix() -> str:
    """``PRIVATE_MEDIA_PREFIX`` normalised to ``""`` (flat layout) or ``"x/"``."""
    prefix = str(cdn_settings.PRIVATE_MEDIA_PREFIX or "").strip().strip("/")
    return f"{prefix}/" if prefix else ""


def is_private_name(name: str) -> bool:
    prefix = private_prefix()
    return bool(prefix) and str(name or "").startswith(prefix)


#: ``PRIVATE_MEDIA_URLS`` values. Anything else is treated as ``signed``.
PRIVATE_URLS_SIGNED = "signed"
PRIVATE_URLS_PUBLIC = "public"


def private_urls_mode() -> str:
    """``"public"`` only when said exactly; everything else fails closed."""
    value = str(cdn_settings.PRIVATE_MEDIA_URLS or "").strip().lower()
    return PRIVATE_URLS_PUBLIC if value == PRIVATE_URLS_PUBLIC else PRIVATE_URLS_SIGNED


def stored_url(name: str, public_url: str) -> str:
    """The URL handed out for the stored object ``name``.

    ``public_url`` is what the storage would answer (``MEDIA_URL + name``);
    it is returned only for a name outside the protected and private trees,
    or for a private name when the deployment opted out with
    ``PRIVATE_MEDIA_URLS = "public"``. A signed link is relative to the app
    (``.../media/signed/<token>/``) and expires after
    ``SIGNED_MEDIA_TTL_SECONDS``; with the app's URLs not mounted it is
    ``""`` — no link rather than a public one.
    """
    rel = str(name or "").replace("\\", "/")
    if is_protected_name(rel):
        return signed_url(rel)
    if is_private_name(rel) and private_urls_mode() == PRIVATE_URLS_SIGNED:
        return signed_url(rel)
    return public_url


def protected_rel_dir(image) -> str:
    """``protected/<type>/<hash>`` relative to MEDIA_ROOT."""
    return f"{protected_prefix()}{image.type}/{image.file_hash}"


def clean_rel_path(image, filename: str) -> str:
    return f"{protected_rel_dir(image)}/{CLEAN_DIR}/{filename}"


def is_protected_name(name: str) -> bool:
    return str(name or "").startswith(protected_prefix())


def original_is_protected(image) -> bool:
    try:
        return is_protected_name(image.original.name)
    except Exception:  # noqa: BLE001 - no file, nothing to protect
        return False


def signed_url(rel_path: str) -> str:
    """A URL that serves ``MEDIA_ROOT/<rel_path>`` for SIGNED_MEDIA_TTL_SECONDS.

    Only a protected or private path resolves (:func:`resolve_token`)."""
    token = signing.dumps({"p": rel_path}, salt=SIGNING_SALT, compress=True)
    try:
        return reverse("cdn-signed-media", args=[token])
    except NoReverseMatch:  # pragma: no cover - app urls not mounted
        return ""


def resolve_token(token: str) -> str | None:
    """Absolute path a valid, unexpired token names, or ``None``.

    The path comes only from the signed payload and must stay inside the
    protected or the private tree under MEDIA_ROOT.
    """
    try:
        payload = signing.loads(
            token,
            salt=SIGNING_SALT,
            max_age=int(cdn_settings.SIGNED_MEDIA_TTL_SECONDS),
        )
    except (signing.BadSignature, ValueError, TypeError):
        return None
    rel = str((payload or {}).get("p") or "")
    if is_protected_name(rel):
        tree = protected_prefix()
    elif is_private_name(rel):
        tree = private_prefix()
    else:
        return None
    root = os.path.realpath(os.path.join(settings.MEDIA_ROOT, tree))
    path = os.path.realpath(os.path.join(settings.MEDIA_ROOT, rel))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def protect_original(image) -> bool:
    """Move a public original into the protected tree. True when moved."""
    if original_is_protected(image) or not image.original:
        return False
    src = image.original.path
    name = os.path.basename(image.original.name)
    rel = f"{protected_rel_dir(image)}/{name}"
    dst = os.path.join(settings.MEDIA_ROOT, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    old = image.original.name
    if not os.path.exists(src) and os.path.exists(dst):
        # A sibling row over the same blob moved it already.
        type(image).objects.filter(pk=image.pk).update(original=rel)
        image.original.name = rel
        return False
    os.replace(src, dst)
    # Every row over the same blob (per-owner dedup shares the file) follows;
    # a queryset update so no post_save re-queues processing.
    type(image).objects.filter(original=old).update(original=rel)
    image.original.name = rel
    return True


__all__ = [
    "CLEAN_DIR",
    "PRIVATE_URLS_PUBLIC",
    "PRIVATE_URLS_SIGNED",
    "is_private_name",
    "private_prefix",
    "private_urls_mode",
    "stored_url",
    "clean_rel_path",
    "is_protected_name",
    "original_is_protected",
    "protect_original",
    "protected_prefix",
    "protected_rel_dir",
    "resolve_token",
    "signed_url",
]
