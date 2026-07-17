"""System checks for stapel-cdn's media submodules (tag ``stapel_cdn``).

Same pattern as ``stapel_core.bus.checks`` E001 (a configured backend whose
transport library isn't installed) and ``stapel_core.django.cdn.checks``
(the client-side counterpart to this module — see cdn-modularity.md
§2.2/§3): a submodule that is configured/enabled but missing its system
binary/library fails silently deep inside the processing pipeline (or, for
``images``, silently degrades to 1x1 placeholder dimensions — §0.3) instead
of at ``manage.py check`` / boot-smoke time.

* **images** (``E001``) — core, unconditional: every ``Image.save()`` needs
  pyvips (and the system ``libvips`` library behind it) to read real
  dimensions. This check always runs; there is no "images disabled" state.
* **video** (``E002``) — VPS/prod-only submodule (cdn-modularity.md §3:
  never installed in the stapel-studio devcontainer). Only checked once a
  host project opts in via ``"video"`` in ``STAPEL_CDN["ENABLED_SUBMODULES"]``.
* **recordings** (``E003``) — audio storage is always available
  (passthrough, no extra needed); this check is about the *optional*
  ffmpeg-audio compression pass, so it only fires once a host project opts
  in via ``"recordings"`` in ``STAPEL_CDN["ENABLED_SUBMODULES"]``.
"""
from __future__ import annotations

import importlib.util
import shutil

from django.core import checks

E001_IMAGES_LIBRARY_MISSING = "stapel_cdn.images.E001"
E002_VIDEO_BINARY_MISSING = "stapel_cdn.video.E002"
E003_RECORDINGS_BINARY_MISSING = "stapel_cdn.recordings.E003"


def _pyvips_importable() -> bool:
    return importlib.util.find_spec("pyvips") is not None


@checks.register("stapel_cdn")
def check_submodule_binaries(app_configs=None, **kwargs):
    """E001/E002/E003 — an enabled media submodule is missing its binary/library."""
    from .conf import cdn_settings

    findings = []
    enabled = set(cdn_settings.ENABLED_SUBMODULES)

    # images: unconditional — every Image save needs pyvips, regardless of
    # ENABLED_SUBMODULES (it isn't an opt-in; cdn-modularity.md §3 table).
    if not _pyvips_importable():
        findings.append(
            checks.Error(
                "pyvips is not importable — Image.save() will silently fall "
                "back to 1x1 placeholder dimensions for every uploaded "
                "image (an honest ERROR is now logged per-save, but the "
                "root cause is a missing dependency, not a per-file fluke).",
                hint="Install the system libvips library (apt: "
                     "libvips-dev) and `pip install stapel-cdn[images]`.",
                id=E001_IMAGES_LIBRARY_MISSING,
            )
        )

    # video: VPS/prod-only, opt-in via ENABLED_SUBMODULES.
    if "video" in enabled and shutil.which("ffmpeg") is None:
        findings.append(
            checks.Error(
                "'video' is in STAPEL_CDN['ENABLED_SUBMODULES'] but the "
                "'ffmpeg' binary is not on PATH — video variant/poster "
                "generation (VideoProcessingService) cannot run.",
                hint="Install ffmpeg on this VPS/prod image (never the "
                     "stapel-studio devcontainer — cdn-modularity.md §3) "
                     "or remove 'video' from ENABLED_SUBMODULES.",
                id=E002_VIDEO_BINARY_MISSING,
            )
        )

    # recordings: storage is always available; this only gates the
    # optional ffmpeg-audio compression pass.
    if "recordings" in enabled and shutil.which("ffmpeg") is None:
        findings.append(
            checks.Error(
                "'recordings' is in STAPEL_CDN['ENABLED_SUBMODULES'] but "
                "the 'ffmpeg' binary is not on PATH — ffmpeg-audio "
                "compression (AudioProcessingService) cannot run. Audio "
                "storage itself is unaffected (passthrough, no binary "
                "required) — this only blocks the optional compression "
                "pass.",
                hint="Install ffmpeg on this VPS/prod image or remove "
                     "'recordings' from ENABLED_SUBMODULES to keep "
                     "passthrough-only storage.",
                id=E003_RECORDINGS_BINARY_MISSING,
            )
        )

    return findings


__all__ = [
    "E001_IMAGES_LIBRARY_MISSING",
    "E002_VIDEO_BINARY_MISSING",
    "E003_RECORDINGS_BINARY_MISSING",
    "check_submodule_binaries",
]
