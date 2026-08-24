"""CDN app for stapel-cdn service.

Public API (lazily exported, PEP 562 — importing this package never pulls
in Django or requires configured settings):

- ``cdn_settings`` — resolved app settings (``stapel_cdn.conf``).
- ``media_exists`` — comm function: check whether a media ref resolves to a
  stored CDN asset (``stapel_cdn.functions``).
- ``refs_sync`` — comm function: sync entity → media reference tracking
  (``stapel_cdn.functions``).
- ``describe`` / ``describe_many`` — comm functions: the render-metadata
  snapshot a UI needs to draw an attachment with no second round trip
  (``stapel_cdn.functions``).
- ``build_render_metadata`` — that same snapshot for a model instance the
  host already holds (``stapel_cdn.metadata``).
- ``get_media_kinds`` / ``classify`` — the open media-kind registry
  (``stapel_cdn.kinds``).
- ``validate_image_file`` — upload validator: extension allowlist, libvips
  decode check, decompression-bomb cap (``stapel_cdn.validators``).
- ``text_watermark`` — built-in reference watermark engine for the
  ``STAPEL_CDN["WATERMARK"]`` seam (``stapel_cdn.watermarks``).
"""

__all__ = [
    "build_render_metadata",
    "cdn_settings",
    "classify",
    "describe",
    "describe_many",
    "get_media_kinds",
    "media_exists",
    "refs_sync",
    "text_watermark",
    "validate_image_file",
]

# name → submodule that defines it. Resolution is deferred until first
# attribute access so that `import stapel_cdn` stays Django-free.
_LAZY_EXPORTS = {
    "build_render_metadata": ".metadata",
    "cdn_settings": ".conf",
    "classify": ".kinds",
    "describe": ".functions",
    "describe_many": ".functions",
    "get_media_kinds": ".kinds",
    "media_exists": ".functions",
    "refs_sync": ".functions",
    "text_watermark": ".watermarks",
    "validate_image_file": ".validators",
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        value = getattr(import_module(_LAZY_EXPORTS[name], __name__), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
