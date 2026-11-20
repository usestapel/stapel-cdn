"""Contract examples: one complete object per media kind.

Every response example in ``views.py`` is built from these, so an example
validates against the serializer it illustrates (tests/test_contract_profile2.py
checks each one) and a field added to a serializer is added here once.
"""
from __future__ import annotations

import copy

_HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_USER = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
_AT = "2026-03-17T10:30:00Z"


def _render_meta(ref: str, kind: str, mime: str, ext: str, size: int, **extra) -> dict:
    meta = {
        "ref": ref,
        "kind": kind,
        "mime": mime,
        "ext": ext,
        "bytes": size,
        "meta_status": "ok",
    }
    meta.update(extra)
    return meta


def _image_urls(name: str) -> dict:
    return {
        f"variant_{width}_url": f"/media/cdn/images/{width}/{name}.webp"
        for width in (16, 32, 64, 120, 160, 240, 480, 560, 720, 1080, 1440, 2160)
    }


IMAGE = {
    "id": 42,
    "file_hash": _HASH,
    "original_filename": "photo.jpg",
    "file_extension": ".jpg",
    "type": "avatar",
    "prefix": "avatar",
    "original_width": 1920,
    "original_height": 1080,
    "original_size": 2048576,
    "original_url": f"/media/cdn/images/original/{_HASH}.jpg",
    **_image_urls(_HASH),
    "variants_meta": [
        {
            "tier": 720,
            "branch": "w",
            "url": f"/media/cdn/images/720/{_HASH}.webp",
            "width": 720,
            "height": 405,
        },
    ],
    "variants_status": "pending",
    "variants_ready_at": None,
    "render_meta": _render_meta(
        f"avatar/{_HASH}", "image", "image/jpeg", ".jpg", 2048576,
        width=1920, height=1080, aspect=1.7778, square=False, animated=False,
    ),
    "is_processed": False,
    "uploaded_by": _USER,
    "uploaded_by_username": "alice",
    "created_at": _AT,
    "updated_at": _AT,
}

VIDEO = {
    "id": 7,
    "file_hash": _HASH,
    "original_filename": "clip.mp4",
    "file_extension": ".mp4",
    "original_width": 1920,
    "original_height": 1080,
    "original_size": 15728640,
    "duration": 12.5,
    "original_url": f"/media/cdn/videos/original/{_HASH}.mp4",
    "variant_16p_url": None,
    "variant_32p_url": None,
    "variant_240p_url": None,
    "variant_480p_url": None,
    "variant_720p_url": None,
    "variant_1080p_url": None,
    "variant_2160p_url": None,
    "poster_url": None,
    "render_meta": _render_meta(
        f"video/{_HASH}", "video", "video/mp4", ".mp4", 15728640,
        width=1920, height=1080, aspect=1.7778, duration_ms=12500,
    ),
    "is_processed": False,
    "uploaded_by": _USER,
    "uploaded_by_username": "alice",
    "created_at": _AT,
    "updated_at": _AT,
}

FILE = {
    "id": 11,
    "file_hash": _HASH,
    "original_filename": "invoice.pdf",
    "file_extension": ".pdf",
    "mime_type": "application/pdf",
    "original_size": 184320,
    "prefix": "file",
    "original_url": f"/media/cdn/files/{_HASH}.pdf",
    "render_meta": _render_meta(f"file/{_HASH}", "file", "application/pdf", ".pdf", 184320),
    "refs": [],
    "uploaded_by": _USER,
    "uploaded_by_username": "alice",
    "created_at": _AT,
    "updated_at": _AT,
}

AUDIO = {
    "id": 5,
    "ref": f"audio/{_HASH}",
    "file_hash": _HASH,
    "original_filename": "voice.ogg",
    "file_extension": ".ogg",
    "mime_type": "audio/ogg",
    "original_size": 48213,
    "duration": 3.9,
    "preview_b64": "",
    "original_url": f"/media/cdn/audio/{_HASH}.ogg",
    "render_meta": _render_meta(
        f"audio/{_HASH}", "audio", "audio/ogg", ".ogg", 48213,
        duration_ms=3900, preview_kind="waveform",
    ),
    "refs": [],
    "is_compressed": True,
    "uploaded_by": _USER,
    "uploaded_by_username": "alice",
    "created_at": _AT,
    "updated_at": _AT,
}


def media(kind: str) -> dict:
    """A fresh copy of the example object for ``kind`` (image/video/file/audio)."""
    return copy.deepcopy({"image": IMAGE, "video": VIDEO, "file": FILE, "audio": AUDIO}[kind])


def upload_examples(kind: str, noun: str, created_message: str | None = None) -> list:
    """The 201 (stored) and 200 (already stored, same hash) answers of an upload."""
    from drf_spectacular.utils import OpenApiExample

    return [
        OpenApiExample(
            name=f"{noun} uploaded",
            response_only=True,
            status_codes=["201"],
            value={"message": created_message or f"{noun} uploaded successfully", kind: media(kind)},
        ),
        OpenApiExample(
            name=f"{noun} already exists",
            response_only=True,
            status_codes=["200"],
            value={"message": f"{noun} already exists", kind: media(kind)},
        ),
    ]


def exists_examples() -> list:
    """``file/exists`` answers: found (an image here) and not found."""
    from drf_spectacular.utils import OpenApiExample

    return [
        OpenApiExample(
            name="File found (image)",
            response_only=True,
            status_codes=["200"],
            value={"exists": True, "type": "image", "file": media("image")},
        ),
        OpenApiExample(
            name="File not found",
            response_only=True,
            status_codes=["200"],
            value={"exists": False, "type": None, "file": None},
        ),
    ]
