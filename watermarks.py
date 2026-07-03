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
"""
from __future__ import annotations

try:
    import pyvips
except ImportError:  # pragma: no cover
    pyvips = None  # type: ignore[assignment]

from .conf import cdn_settings


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


__all__ = ["text_watermark"]
