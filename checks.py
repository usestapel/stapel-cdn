"""System checks for stapel-cdn's media submodules (tag ``stapel_cdn``).

Same pattern as ``stapel_core.bus.checks`` E001 (a configured backend whose
transport library isn't installed) and ``stapel_core.django.cdn.checks``
(the client-side counterpart to this module — see cdn-modularity.md
§2.2/§3): a submodule that is configured/enabled but missing its system
binary/library fails silently deep inside the processing pipeline (or, for
``images``, silently degrades to 1x1 placeholder dimensions — §0.3) instead
of at ``manage.py check`` / boot-smoke time.

* **images** (``E001``) — gated on ``"images"`` in
  ``STAPEL_CDN["ENABLED_SUBMODULES"]`` (on by default). libvips is now the
  *only* decoder on the image path — upload validation, admin validation,
  URL-import format detection and every ``Image.save()`` dimension read all go
  through it (Pillow left the library in 0.10). Without it the image path
  cannot work at all, so this is an error, not a degradation. It is gated
  because stapel-cdn also serves as passthrough file storage: a deployment that
  stores PDFs and turns images off has no business being told to install
  libvips.
* **images** (``E004``) — the precise one, one level below E001: libvips is
  present but this *build* of it cannot read a format
  ``ALLOWED_IMAGE_EXTENSIONS`` declares allowed (libvips is modular — compiled
  without libheif it has no ``heifload``, and .heic is then advertised and
  unreadable). This is the defect class E004 exists for: a setting the library
  offers but the deployment cannot honour (same family as CFG006), detectable
  statically at boot rather than as a 503 on somebody's avatar.
* **video** (``E002``) — VPS/prod-only submodule (cdn-modularity.md §3:
  never installed in the stapel-studio devcontainer). Only checked once a
  host project opts in via ``"video"`` in ``STAPEL_CDN["ENABLED_SUBMODULES"]``.
* **recordings** (``E003``) — audio storage is always available
  (passthrough, no extra needed); this check is about the *optional*
  ffmpeg-audio compression pass, so it only fires once a host project opts
  in via ``"recordings"`` in ``STAPEL_CDN["ENABLED_SUBMODULES"]``.
"""
from __future__ import annotations

import shutil

from django.core import checks

E001_IMAGES_LIBRARY_MISSING = "stapel_cdn.images.E001"
E002_VIDEO_BINARY_MISSING = "stapel_cdn.video.E002"
E003_RECORDINGS_BINARY_MISSING = "stapel_cdn.recordings.E003"
E004_IMAGE_FORMAT_UNDECODABLE = "stapel_cdn.images.E004"
W005_DEDUP_SCOPE_INVALID = "stapel_cdn.ownership.W005"
W006_DEDUP_SCOPE_GLOBAL = "stapel_cdn.ownership.W006"


@checks.register("stapel_cdn")
def check_submodule_binaries(app_configs=None, **kwargs):
    """E001/E004 (images), E002/E003 (ffmpeg) — an enabled submodule cannot work."""
    from . import decoders
    from .conf import cdn_settings

    findings = []
    enabled = set(cdn_settings.ENABLED_SUBMODULES)

    # images: libvips is the one decoder on the image path. No libvips, no
    # image path — uploads cannot be validated, dimensions cannot be read,
    # variants cannot be generated.
    if "images" in enabled and not decoders.available():
        findings.append(
            checks.Error(
                "'images' is in STAPEL_CDN['ENABLED_SUBMODULES'] but pyvips "
                "is not importable — libvips is the only image decoder this "
                "library has. Upload validation cannot verify that a file is "
                "an image, Image.save() falls back to 1x1 placeholder "
                "dimensions, and variant generation cannot run at all.",
                hint="Install the system libvips library (apt: "
                     "libvips-dev) and `pip install stapel-cdn[images]`, or "
                     "remove 'images' from ENABLED_SUBMODULES to run this "
                     "deployment as passthrough file storage.",
                id=E001_IMAGES_LIBRARY_MISSING,
            )
        )

    # images: libvips present, but this build cannot read a configured format.
    # Silent when there is no libvips at all — that is E001's subject, and
    # repeating every configured extension underneath it would bury the one
    # finding that matters.
    if "images" in enabled:
        for extension in decoders.undecodable_allowed_extensions():
            loaders = ", ".join(decoders.VIPS_LOADERS[extension])
            findings.append(
                checks.Error(
                    f"STAPEL_CDN['ALLOWED_IMAGE_EXTENSIONS'] declares "
                    f"{extension} allowed, but this libvips build has no "
                    f"loader for it ({loaders} not registered) — every "
                    f"{extension} upload is accepted by the extension "
                    f"allowlist and then refused with "
                    f"error.503.image_decoder_unavailable, which reads to the "
                    f"uploader as their file being rejected.",
                    hint=f"Install a libvips build that can read {extension} "
                         f"(apt: libvips-dev pulls libheif for HEIC/HEIF/AVIF; "
                         f"BMP needs the ImageMagick module), or remove "
                         f"{extension} from "
                         f"STAPEL_CDN['ALLOWED_IMAGE_EXTENSIONS'] so the "
                         f"setting stops advertising what this deployment "
                         f"cannot do.",
                    id=E004_IMAGE_FORMAT_UNDECODABLE,
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


@checks.register("stapel_cdn")
def check_dedup_scope(app_configs=None, **kwargs):
    """W005/W006 — the deployment's answer to "who may a hash lookup see?".

    Both findings are warnings, not errors: the module still runs, and the
    second one is a legitimate (if narrow) configuration. They exist because
    the failure mode of getting this wrong is silent — a global lookup leaks by
    answering correctly, so nothing about it ever looks broken from inside.
    """
    from .conf import cdn_settings
    from .ownership import SCOPE_GLOBAL, SCOPE_OWNER, VALID_DEDUP_SCOPES

    raw = str(cdn_settings.DEDUP_SCOPE or "").strip().lower()
    if raw not in VALID_DEDUP_SCOPES:
        return [
            checks.Warning(
                f"STAPEL_CDN['DEDUP_SCOPE'] is {cdn_settings.DEDUP_SCOPE!r}, "
                f"which is not one of {', '.join(VALID_DEDUP_SCOPES)}. The "
                f"module falls back to {SCOPE_OWNER!r}, so nothing leaks — but "
                f"the setting is not doing what it was written to do.",
                hint=f"Set it to {SCOPE_OWNER!r} or {SCOPE_GLOBAL!r}.",
                id=W005_DEDUP_SCOPE_INVALID,
            )
        ]
    if raw == SCOPE_GLOBAL:
        return [
            checks.Warning(
                "STAPEL_CDN['DEDUP_SCOPE'] is 'global': a content-hash lookup "
                "matches objects uploaded by anyone. Every upload endpoint "
                "then answers 'does this deployment already hold exactly these "
                "bytes?' for any caller, and returns the holder's row — "
                "identifier, original filename and reference list included.",
                hint="Keep 'global' only where every principal is entitled to "
                     "every other principal's media (a single-tenant "
                     "deployment, or a deliberately shared public asset pool). "
                     "Otherwise use 'owner'.",
                id=W006_DEDUP_SCOPE_GLOBAL,
            )
        ]
    return []


__all__ = [
    "E001_IMAGES_LIBRARY_MISSING",
    "E004_IMAGE_FORMAT_UNDECODABLE",
    "E002_VIDEO_BINARY_MISSING",
    "E003_RECORDINGS_BINARY_MISSING",
    "W005_DEDUP_SCOPE_INVALID",
    "W006_DEDUP_SCOPE_GLOBAL",
    "check_dedup_scope",
    "check_submodule_binaries",
]
