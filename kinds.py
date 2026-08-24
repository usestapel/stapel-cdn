"""The media-kind registry — what an attachment *is*, as an open registry.

Why this is not an enum
-----------------------
A chat attachment renders differently per kind: an image gets a blur-up
placeholder and an aspect box, a voice message gets a waveform and a
duration, a document gets an icon plus its extension. Every product that
ships media grows one more kind sooner or later — GIF today, stickers next,
whatever after that. A closed ``TextChoices`` would make each of those an
upstream release, which is the exact defect cdn-modularity.md §2.1 names
(the server took an open ``ASSET_TYPES`` while the client-side field was
pinned to a marketplace enum).

So kinds are a **merge-over-builtins registry**, the same semantics as every
other Stapel registry (``stapel_core.django.mounts``, ``stapel_core.i18n.
catalogs``): builtins here, an overlay in ``STAPEL_CDN["MEDIA_KINDS"]``
replacing per key, ``None`` removing a key. A host adds stickers with a dict
literal and no fork::

    STAPEL_CDN = {
        "ASSET_TYPES": ("avatar", "sticker"),
        "MEDIA_KINDS": {
            "sticker": {
                "model": "image",
                "asset_types": ("sticker",),
                "preview": "blur",
                "animated": True,
            },
        },
    }

What a kind decides
-------------------
Exactly two things, both of them things the *renderer* needs and cannot
guess:

* ``preview`` — what ``preview_b64`` in the render-metadata snapshot holds
  for this kind: ``"blur"`` (a 16px LQIP of the image itself), ``"poster"``
  (a micro frame lifted out of the video), ``"waveform"`` (a rendered
  amplitude strip for audio) or ``None`` (documents — there is nothing to
  show but an icon);
* ``animated`` — whether the object moves, so a UI knows to offer a
  play/pause affordance instead of treating it as a still.

Everything else about an object (dimensions, duration, byte size) is a
*fact* read off the file, not a property of its kind, and lives in the
snapshot.

Resolution
----------
An object is classified by the model it is stored in, narrowed by extension
and — for images — by ``Image.type``. Most specific wins, deterministically:

    asset_types match: +2   extensions match: +1   base entry: 0

A narrowing that does not match excludes the entry entirely; equal scores
are broken by kind name so the answer never depends on dict ordering. A
kind whose model has no other candidate is the model's fallback (``image``,
``video``, ``audio``, ``file`` are all shipped as such), and a model left
without any candidate at all — a host that removed the builtin without
replacing it — resolves to ``None``, which the snapshot reports as
``kind: null`` rather than inventing one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

#: The four stored models a kind can be drawn from. A kind naming anything
#: else cannot be resolved by ``classify`` and is a configuration error.
MODELS = ("image", "video", "audio", "file")

#: What ``preview_b64`` carries for a kind. ``None`` = this kind has no
#: inline preview at all.
PREVIEW_BLUR = "blur"
PREVIEW_POSTER = "poster"
PREVIEW_WAVEFORM = "waveform"
PREVIEW_KINDS = (PREVIEW_BLUR, PREVIEW_POSTER, PREVIEW_WAVEFORM)

ENTRY_KEYS = {"model", "extensions", "asset_types", "preview", "animated"}


class MediaKindConfigError(Exception):
    """A ``STAPEL_CDN["MEDIA_KINDS"]`` entry does not parse.

    Raised by :func:`get_media_kinds` and reported as a system-check
    Warning (``checks.W009``) — a malformed overlay entry must be visible
    at ``manage.py check`` time, not as a 500 on somebody's attachment.
    """


@dataclass(frozen=True)
class MediaKind:
    """One media kind of the current deployment."""

    name: str
    model: str
    preview: Optional[str] = None
    animated: bool = False
    #: Lowercase, dot-prefixed (".gif"). Empty = no extension narrowing.
    extensions: Tuple[str, ...] = field(default_factory=tuple)
    #: ``Image.type`` values this kind narrows to. Empty = no narrowing.
    asset_types: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def specificity(self) -> int:
        return (2 if self.asset_types else 0) + (1 if self.extensions else 0)


#: Shipped kinds. Deliberately small: the four storage models plus GIF,
#: which is the one kind a chat UI must already tell apart from a still
#: image (it autoplays, and it is an *image* file, so nothing else in the
#: snapshot distinguishes it).
BUILTIN_MEDIA_KINDS: Dict[str, dict] = {
    "image": {"model": "image", "preview": PREVIEW_BLUR, "animated": False},
    "gif": {
        "model": "image",
        "extensions": (".gif",),
        "preview": PREVIEW_BLUR,
        "animated": True,
    },
    "video": {"model": "video", "preview": PREVIEW_POSTER, "animated": True},
    "audio": {"model": "audio", "preview": PREVIEW_WAVEFORM, "animated": True},
    "file": {"model": "file", "preview": None, "animated": False},
}


def _coerce(name: str, entry: Any) -> MediaKind:
    if isinstance(entry, MediaKind):
        return entry
    if not isinstance(entry, dict):
        raise MediaKindConfigError(
            f"STAPEL_CDN['MEDIA_KINDS'][{name!r}]: expected a dict or None, "
            f"got {type(entry).__name__}"
        )
    unknown = set(entry) - ENTRY_KEYS
    if unknown:
        raise MediaKindConfigError(
            f"STAPEL_CDN['MEDIA_KINDS'][{name!r}]: unknown keys "
            f"{sorted(unknown)} (allowed: {sorted(ENTRY_KEYS)})"
        )
    model = entry.get("model")
    if model not in MODELS:
        raise MediaKindConfigError(
            f"STAPEL_CDN['MEDIA_KINDS'][{name!r}]: 'model' must be one of "
            f"{list(MODELS)}, got {model!r}"
        )
    preview = entry.get("preview")
    if preview is not None and preview not in PREVIEW_KINDS:
        raise MediaKindConfigError(
            f"STAPEL_CDN['MEDIA_KINDS'][{name!r}]: 'preview' must be one of "
            f"{list(PREVIEW_KINDS)} or None, got {preview!r}"
        )
    extensions = tuple(
        str(e).lower() if str(e).startswith(".") else f".{str(e).lower()}"
        for e in (entry.get("extensions") or ())
    )
    asset_types = tuple(str(t) for t in (entry.get("asset_types") or ()))
    return MediaKind(
        name=name,
        model=model,
        preview=preview,
        animated=bool(entry.get("animated", False)),
        extensions=extensions,
        asset_types=asset_types,
    )


def get_media_kinds() -> Dict[str, MediaKind]:
    """Effective kinds: builtins merged with ``STAPEL_CDN["MEDIA_KINDS"]``.

    Merge-over-builtins: an overlay entry replaces the builtin of the same
    name, ``None`` removes it. Raises :class:`MediaKindConfigError` on a
    malformed entry — surfaced by ``checks.W009``.
    """
    from .conf import cdn_settings

    merged = {name: _coerce(name, entry) for name, entry in BUILTIN_MEDIA_KINDS.items()}
    overlay = cdn_settings.MEDIA_KINDS or {}
    if not isinstance(overlay, dict):
        raise MediaKindConfigError(
            f"STAPEL_CDN['MEDIA_KINDS'] must be a dict, got "
            f"{type(overlay).__name__}"
        )
    for name, entry in overlay.items():
        if entry is None:
            merged.pop(name, None)
            continue
        merged[name] = _coerce(str(name), entry)
    return merged


def classify(
    model: str, extension: str = "", asset_type: str = ""
) -> Optional[MediaKind]:
    """The kind of an object stored in *model*, or None if nothing claims it.

    ``model`` is one of :data:`MODELS`; ``extension`` is the stored
    ``file_extension`` (with or without the dot); ``asset_type`` is
    ``Image.type`` for images and ignored otherwise. See the module
    docstring for the specificity rule.
    """
    ext = (extension or "").lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"

    candidates = []
    for kind in get_media_kinds().values():
        if kind.model != model:
            continue
        if kind.extensions and ext not in kind.extensions:
            continue
        if kind.asset_types and asset_type not in kind.asset_types:
            continue
        candidates.append(kind)
    if not candidates:
        return None
    candidates.sort(key=lambda k: (-k.specificity, k.name))
    return candidates[0]


def classify_object(obj) -> Optional[MediaKind]:
    """:func:`classify` for a stored ``Image``/``Video``/``File``/``Audio``."""
    from .models import Audio, File, Image, Video

    if isinstance(obj, Image):
        return classify("image", obj.file_extension, obj.type)
    if isinstance(obj, Video):
        return classify("video", obj.file_extension)
    if isinstance(obj, Audio):
        return classify("audio", obj.file_extension)
    if isinstance(obj, File):
        return classify("file", obj.file_extension)
    raise TypeError(f"classify_object: unsupported object {type(obj)!r}")


__all__ = [
    "BUILTIN_MEDIA_KINDS",
    "ENTRY_KEYS",
    "MODELS",
    "MediaKind",
    "MediaKindConfigError",
    "PREVIEW_BLUR",
    "PREVIEW_KINDS",
    "PREVIEW_POSTER",
    "PREVIEW_WAVEFORM",
    "classify",
    "classify_object",
    "get_media_kinds",
]
