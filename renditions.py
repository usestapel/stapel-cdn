"""Derived files: where the pipelines write them, and how erasure removes them.

One module names every file this library derives from a stored original, so
the writers (``services.ImageProcessingService``, ``VideoProcessingService``)
and the removers (``erasure._destroy``, ``regenerate_media``) cannot disagree
about what exists:

* an image's thumbnails ``<type>/<hash>/<tier>.webp`` and preview branches
  ``<type>/<hash>/<tier>{w,h}.webp``;
* the clean (unwatermarked) copy of a marked preview, under
  ``<protected>/<type>/<hash>/clean/`` (:mod:`stapel_cdn.protected`);
* a video's poster frame ``video/<hash>/poster.webp``.

All of them live on the local filesystem under ``MEDIA_ROOT``: the derivation
pipelines write with libvips/ffmpeg to local paths, and the library ships no
other storage backend (``storage.cdn_storage`` is a ``FileSystemStorage``).

Erasure removes the whole content-addressed directory of an object rather than
a list of names it remembers: the directory is keyed by the object's own hash,
so everything in it was derived from those bytes, and a size or format added
tomorrow is gone with it without anyone having to update a list here.
"""
from __future__ import annotations

import os
import shutil
import sys

from django.conf import settings

#: Filename of a video's derived poster frame, under its hash directory.
POSTER_FILENAME = "poster.webp"


def thumbnail_name(tier) -> str:
    """``<tier>.webp`` — a min-side thumbnail tier (no branches)."""
    return f"{tier}.webp"


def preview_name(tier, axis: str) -> str:
    """``<tier><axis>.webp`` — one branch (``w``/``h``) of a preview tier."""
    return f"{tier}{axis}.webp"


def image_public_dir(image) -> str:
    """``<type>/<hash>`` relative to MEDIA_ROOT — the public variant directory."""
    return f"{image.type}/{image.file_hash}"


def video_dir(file_hash: str) -> str:
    """``video/<hash>`` relative to MEDIA_ROOT — where a video's poster goes."""
    return f"video/{file_hash}"


def absolute(rel_path: str) -> str:
    return os.path.join(settings.MEDIA_ROOT, rel_path)


def derived_dirs(row) -> list[str]:
    """Directories (relative to MEDIA_ROOT) holding files derived from ``row``.

    Every directory here is keyed by the object's content hash, so it belongs
    to the rows over those bytes and nothing else. ``File`` and ``Audio`` have
    no derived files today; their hash directory is listed anyway, so the day
    one is written there erasure already covers it.
    """
    from .models import Audio, File, Image, Video, _private_prefix
    from .protected import protected_rel_dir

    if isinstance(row, Image):
        return [image_public_dir(row), protected_rel_dir(row)]
    if isinstance(row, Video):
        return [video_dir(row.file_hash)]
    if isinstance(row, File):
        return [f"{_private_prefix()}file/{row.file_hash}"]
    if isinstance(row, Audio):
        return [f"{_private_prefix()}audio/{row.file_hash}"]
    return []


def possible_renditions(row) -> list[str]:
    """Every rendition path (relative to MEDIA_ROOT) the pipelines may write
    for ``row`` under the current configuration.

    "May": a square image writes no ``h`` branch and an unmarked tier writes
    no clean copy, so a real object holds a subset of this list. Built from
    the same name helpers the writers call, which is what lets a test hold
    the pipeline's actual output against it.
    """
    from .conf import cdn_settings
    from .models import Image, Video
    from .protected import clean_rel_path

    if isinstance(row, Image):
        public = image_public_dir(row)
        names = [thumbnail_name(int(t)) for t in cdn_settings.THUMBNAIL_SIZES]
        previews = [
            preview_name(int(t), axis)
            for t in cdn_settings.PREVIEW_SIZES
            for axis in ("w", "h")
        ]
        return (
            [f"{public}/{name}" for name in names + previews]
            + [clean_rel_path(row, name) for name in previews]
        )
    if isinstance(row, Video):
        return [f"{video_dir(row.file_hash)}/{POSTER_FILENAME}"]
    return []


class RenditionRemovalFailed(OSError):
    """A derived file of an object being destroyed is still on disk."""


def remove_derived(row) -> int:
    """Delete every file derived from ``row``; return how many were removed.

    The caller decides that no other row holds these bytes (``_destroy`` asks
    ``ownership.shared_binary_exists`` first). Raises
    :class:`RenditionRemovalFailed` when anything survives — a public
    rendition left behind after an erasure is the same personal data the
    original was.
    """
    removed = 0
    failures: list[str] = []
    for rel in derived_dirs(row):
        path = absolute(rel)
        if not os.path.lexists(path):
            continue
        for _root, _dirs, files in os.walk(path):
            removed += len(files)

        def _record(_func, failed_path, _exc):
            failures.append(str(failed_path))

        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_record)
        else:  # pragma: no cover - 3.11
            shutil.rmtree(path, onerror=_record)
        if os.path.lexists(path):
            failures.append(path)
    if failures:
        raise RenditionRemovalFailed(
            f"could not remove derived files of {type(row).__name__} "
            f"{row.pk}: {', '.join(sorted(set(failures)))}"
        )
    return removed


__all__ = [
    "POSTER_FILENAME",
    "RenditionRemovalFailed",
    "derived_dirs",
    "image_public_dir",
    "possible_renditions",
    "preview_name",
    "remove_derived",
    "thumbnail_name",
    "video_dir",
]
