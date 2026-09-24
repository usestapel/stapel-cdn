"""Watermark engines for stapel-cdn.

The engine is a dotted-path seam, not a baked-in renderer: set
``STAPEL_CDN["WATERMARK"]`` to a callable ``(pyvips.Image) -> pyvips.Image``
and the processing pipeline applies it to every preview variant.
Watermarking is **off by default** (the setting is empty).

``text_watermark`` below is the built-in reference implementation — a
plain bottom-right text label. Host projects that want designed
watermarks (logo overlays, opacity, tiling) point the setting at their
own callable instead of forking::

    STAPEL_CDN = {
        "WATERMARK": "stapel_cdn.watermarks.text_watermark",
        "WATERMARK_TEXT": "Acme",
    }
    # or
    STAPEL_CDN = {"WATERMARK": "myproject.media.logo_watermark"}

Per-site logo watermarks (0.26.0): ``STAPEL_CDN["WATERMARKS"]`` maps a site
key to a PNG and its placement (see conf.py). An upload records the site it
arrived on (``Image.site_key``, from the ``stapel_core.sites`` registry), and
its preview renditions get that site's mark. The stored original is never
touched; thumbnails and renditions under ``WATERMARK_MIN_SIDE`` stay clean::

    STAPEL_CDN = {
        "WATERMARKS": {
            "acme": {"PATH": "/etc/acme/watermark.png", "POSITION": "bottom-right"},
        },
        "WATERMARK_ASSET_TYPES": ("product",),
    }
"""
from __future__ import annotations

import logging
import os
import threading

try:
    import pyvips
except ImportError:  # pragma: no cover
    pyvips = None  # type: ignore[assignment]

from .conf import cdn_settings

logger = logging.getLogger(__name__)

POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left", "center")

SPEC_DEFAULTS = {
    "POSITION": "bottom-right",
    "SCALE": 0.24,
    "MAX_HEIGHT": 0.10,
    "MARGIN": 0.028,
    "OPACITY": 1.0,
}


def text_watermark(img: "pyvips.Image", text: str | None = None) -> "pyvips.Image":
    """Render a text label in the bottom-right corner.

    ``text`` defaults to ``STAPEL_CDN["WATERMARK_TEXT"]``; with no text
    configured the image is returned unchanged.
    """
    if text is None:
        text = cdn_settings.WATERMARK_TEXT
    if not text:
        return img

    font_size = max(12, int(img.height * 0.05))
    markup = f'<span foreground="white" background="black">{text}</span>'
    text_img = pyvips.Image.text(
        markup, font=f"DejaVu Sans Bold {font_size}", dpi=72, rgba=True
    )

    padding = max(5, int(img.height * 0.02))
    x = max(padding, img.width - text_img.width - padding)
    y = max(padding, img.height - text_img.height - padding)

    if img.bands == 3:
        img = img.bandjoin(255)

    text_positioned = text_img.embed(x, y, img.width, img.height, extend="black")
    result = img.composite2(text_positioned, "over")
    return result.flatten(background=[255, 255, 255])


def request_site_key(request) -> str:
    """The site key an upload request arrived on, or ``""``.

    The registry entry's brand key, else its host. Only an exact host or alias
    match counts: a request on an internal or unknown host records nothing
    rather than being stamped with the primary site's brand.
    """
    try:
        from stapel_core.django.sites.helpers import request_host, site_registry
    except ImportError:  # pragma: no cover - core without the registry
        return ""
    site = site_registry().for_host(request_host(request))
    if site is None:
        return ""
    return site.brand.key if site.brand else site.host


def watermark_spec_for(image_model) -> dict | None:
    """The per-site spec that applies to *image_model*, or ``None``."""
    specs = cdn_settings.WATERMARKS or {}
    if not specs:
        return None
    types = tuple(cdn_settings.WATERMARK_ASSET_TYPES or ())
    if types and getattr(image_model, "type", None) not in types:
        return None
    key = getattr(image_model, "site_key", "") or cdn_settings.WATERMARK_DEFAULT_SITE
    spec = specs.get(key) if key else None
    if not spec:
        return None
    return {**SPEC_DEFAULTS, **spec}


def _as_srgb(img: "pyvips.Image") -> "pyvips.Image":
    """8-bit sRGB (with or without alpha), whatever the rendition came as."""
    if img.format == "uchar" and img.bands in (3, 4):
        if img.interpretation != "srgb":
            img = img.copy(interpretation="srgb")
        return img
    return img.colourspace("srgb").cast("uchar")


_marks: dict = {}
_marks_lock = threading.Lock()


def _load_mark(path: str) -> "pyvips.Image":
    """The mark as premultiplied-ready RGBA, cached per (path, mtime)."""
    stamp = os.stat(path).st_mtime_ns
    with _marks_lock:
        cached = _marks.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
    mark = _as_srgb(pyvips.Image.new_from_file(path, access="random"))
    if not mark.hasalpha():
        mark = mark.bandjoin(255)
    mark = mark.copy_memory()
    with _marks_lock:
        _marks[path] = (stamp, mark)
    return mark


def overlay_watermark(img: "pyvips.Image", spec: dict) -> "pyvips.Image":
    """Composite the spec's PNG onto *img*, scaled to the rendition.

    Width is ``SCALE`` of the image width, capped so the mark is never taller
    than ``MAX_HEIGHT`` of the image height (panoramas). Returns *img*
    unchanged when the mark cannot be read — the failure is logged at ERROR,
    because an unreadable asset must not stop photos from being published.
    """
    spec = {**SPEC_DEFAULTS, **spec}
    path = str(spec.get("PATH") or "")
    try:
        mark = _load_mark(path)
    except Exception as exc:  # noqa: BLE001 - logged, rendition stays clean
        logger.error("stapel-cdn: watermark %r cannot be read: %s", path, exc)
        return img

    width, height = img.width, img.height
    target = min(
        float(spec["SCALE"]) * width,
        float(spec["MAX_HEIGHT"]) * height * mark.width / mark.height,
    )
    if target < 8:
        return img
    # Premultiplied resize: no dark fringe where the alpha falls off.
    scaled = mark.premultiply().resize(target / mark.width).unpremultiply()
    scaled = scaled.cast("uchar")
    opacity = float(spec["OPACITY"])
    if opacity < 1.0:
        scaled = scaled * [1, 1, 1, max(0.0, opacity)]
        scaled = scaled.cast("uchar")

    inset = round(float(spec["MARGIN"]) * min(width, height))
    position = spec["POSITION"] if spec["POSITION"] in POSITIONS else "bottom-right"
    if position == "center":
        x, y = (width - scaled.width) // 2, (height - scaled.height) // 2
    else:
        vertical, horizontal = position.split("-")
        x = inset if horizontal == "left" else width - scaled.width - inset
        y = inset if vertical == "top" else height - scaled.height - inset
    x, y = max(0, x), max(0, y)

    base = _as_srgb(img)
    had_alpha = base.hasalpha()
    out = base.composite2(scaled, "over", x=x, y=y)
    out = out.cast("uchar")
    if not had_alpha:
        out = out.extract_band(0, n=3)
    return out


__all__ = [
    "overlay_watermark",
    "request_site_key",
    "text_watermark",
    "watermark_spec_for",
]
