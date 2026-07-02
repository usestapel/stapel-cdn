"""CDN app for stapel-cdn service.

Public API (lazily exported, PEP 562 — importing this package never pulls
in Django or requires configured settings):

- ``cdn_settings`` — resolved app settings (``stapel_cdn.conf``).
- ``media_exists`` — comm function: check whether a media ref resolves to a
  stored CDN asset (``stapel_cdn.functions``).
- ``refs_sync`` — comm function: sync entity → media reference tracking
  (``stapel_cdn.functions``).
- ``validate_image_file`` — upload validator: extension allowlist, Pillow
  decode check, decompression-bomb cap (``stapel_cdn.validators``).
"""

__all__ = [
    "cdn_settings",
    "media_exists",
    "refs_sync",
    "validate_image_file",
]

# name → submodule that defines it. Resolution is deferred until first
# attribute access so that `import stapel_cdn` stays Django-free.
_LAZY_EXPORTS = {
    "cdn_settings": ".conf",
    "media_exists": ".functions",
    "refs_sync": ".functions",
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
