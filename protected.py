"""Protected media: bytes that are stored but never on the public media route.

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
    """A URL that serves ``MEDIA_ROOT/<rel_path>`` for SIGNED_MEDIA_TTL_SECONDS."""
    token = signing.dumps({"p": rel_path}, salt=SIGNING_SALT, compress=True)
    try:
        return reverse("cdn-signed-media", args=[token])
    except NoReverseMatch:  # pragma: no cover - app urls not mounted
        return ""


def resolve_token(token: str) -> str | None:
    """Absolute path a valid, unexpired token names, or ``None``.

    The path comes only from the signed payload and must stay inside the
    protected tree under MEDIA_ROOT.
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
    if not is_protected_name(rel):
        return None
    root = os.path.realpath(os.path.join(settings.MEDIA_ROOT, protected_prefix()))
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
    "clean_rel_path",
    "is_protected_name",
    "original_is_protected",
    "protect_original",
    "protected_prefix",
    "protected_rel_dir",
    "resolve_token",
    "signed_url",
]
