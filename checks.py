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
* **tasks** (``W008``) — the variant queues are named by settings and
  nothing this process can read corroborates that anybody consumes them. The
  live incident: two hardcoded queue names, a fleet that shards queues per
  service, zero consumers, and 201s whose ``variant_*_url``s 404 forever. A
  library cannot see a compose file, so this reports what it *can* see and
  names stapel-tools for the deployment-level (ADO-class) half.
* **recordings** (``E003``) — audio storage is always available
  (passthrough, no extra needed); ffmpeg is what a voice message's duration
  and waveform strip come from, so this fires once a host project opts in
  via ``"recordings"`` in ``STAPEL_CDN["ENABLED_SUBMODULES"]``.
* **kinds** (``W009``) — a ``STAPEL_CDN["MEDIA_KINDS"]`` overlay entry that
  does not parse. The registry is read on every ``cdn.describe``, so a
  malformed entry would otherwise surface as a 500 on an attachment.
* **media** (``W010``) — ffmpeg without ffprobe, or ffprobe without ffmpeg.
  E002/E003 probe for the pair; a split install passes them and then
  produces attachments with a waveform and no duration.
* **media** (``W011``) — an inline-preview byte budget that is not a
  positive number, so the shipped default is what actually bounds the
  payload.
* **describe** (``E005``) — a ``DESCRIBE_PERMISSIONS`` entry that does not
  import, or a ``DESCRIBE_THROTTLE``/``DESCRIBE_ANON_THROTTLE`` rate DRF
  cannot parse. Both are resolved per request, so either one turns every
  ``POST /describe/`` into a 500 — an operator's problem that must be heard
  at boot, not read off an error rate.
* **describe** (``W012``) — an EMPTY ``DESCRIBE_PERMISSIONS``. DRF reads no
  permission classes as "everyone passes", so the accident of an empty list
  publishes the endpoint. ``AllowAny`` says the same thing on purpose and is
  not reported; a blank does not.
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
W007_QUOTA_CEILING_INVALID = "stapel_cdn.ownership.W007"
W008_VARIANT_QUEUE_UNPROVEN = "stapel_cdn.tasks.W008"
W009_MEDIA_KINDS_INVALID = "stapel_cdn.kinds.W009"
W010_MEDIA_TOOL_MISSING = "stapel_cdn.media.W010"
W011_PREVIEW_BUDGET_INVALID = "stapel_cdn.media.W011"
E005_DESCRIBE_SEAM_UNUSABLE = "stapel_cdn.describe.E005"
W012_DESCRIBE_GUARD_EMPTY = "stapel_cdn.describe.W012"


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
                "'ffmpeg' binary is not on PATH — VideoProcessingService "
                "cannot measure a video's dimensions or duration and cannot "
                "extract a poster frame. Every video this deployment stores "
                "answers cdn.describe with a null aspect, a null duration "
                "and no preview, so a UI has nothing to reserve space with.",
                hint="Install ffmpeg on this VPS/prod image (never the "
                     "stapel-studio devcontainer — cdn-modularity.md §3) "
                     "or remove 'video' from ENABLED_SUBMODULES. Rows "
                     "stored while it was missing carry meta_reason "
                     "'ffmpeg_missing'/'ffprobe_missing' — run "
                     "`manage.py cdn_backfill_media_meta --retry-degraded` "
                     "once the binary is there.",
                id=E002_VIDEO_BINARY_MISSING,
            )
        )

    # recordings: storage is always available; this only gates the
    # optional ffmpeg-audio compression pass.
    if "recordings" in enabled and shutil.which("ffmpeg") is None:
        findings.append(
            checks.Error(
                "'recordings' is in STAPEL_CDN['ENABLED_SUBMODULES'] but "
                "the 'ffmpeg' binary is not on PATH — a voice message gets "
                "neither its duration (ffprobe) nor its waveform strip "
                "(ffmpeg showwavespic), so a chat bubble has nothing to "
                "render but a filename. Audio STORAGE is unaffected "
                "(passthrough, no binary required), and so is the still-"
                "unimplemented compression pass.",
                hint="Install ffmpeg on this VPS/prod image or remove "
                     "'recordings' from ENABLED_SUBMODULES to keep "
                     "passthrough-only storage with no waveforms. Rows "
                     "stored while it was missing carry meta_reason "
                     "'ffmpeg_missing'/'ffprobe_missing' — run "
                     "`manage.py cdn_backfill_media_meta --retry-degraded` "
                     "once the binary is there.",
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


@checks.register("stapel_cdn")
def check_owner_quotas(app_configs=None, **kwargs):
    """W007 — a per-owner ceiling nobody can act on.

    A warning rather than an error because the module keeps working: an
    unusable value falls back to the shipped default (``ownership.
    quota_ceiling``), so the deployment is bounded either way. It is reported
    because the failure mode is otherwise invisible — the operator who wrote
    ``MAX_BYTES_PER_OWNER = ""`` believes they configured something, and used
    to get "no ceiling at all" for it.
    """
    from .conf import cdn_settings
    from .ownership import QUOTA_CEILING_KEYS, QUOTA_UNLIMITED, quota_ceiling

    findings = []
    for key in QUOTA_CEILING_KEYS:
        raw = getattr(cdn_settings, key)
        if isinstance(raw, str) and raw.strip().lower() == QUOTA_UNLIMITED:
            continue
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            continue
        findings.append(
            checks.Warning(
                f"STAPEL_CDN['{key}'] is {raw!r}, which is neither "
                f"{QUOTA_UNLIMITED!r} nor a positive number of "
                f"{'objects' if 'OBJECTS' in key else 'bytes'}. The module "
                f"falls back to its shipped default "
                f"({quota_ceiling(key)}) rather than treating an unusable "
                f"value as 'no ceiling', so storage stays bounded — but the "
                f"setting is not doing what it was written to do.",
                hint=f"Set a positive number, or {QUOTA_UNLIMITED!r} to "
                     f"remove the ceiling on purpose. 0 no longer means "
                     f"unlimited: opting out is an explicit act.",
                id=W007_QUOTA_CEILING_INVALID,
            )
        )
    return findings


def _declared_queue_names() -> set[str]:
    """Queue names this *process* can see, from Django settings alone.

    Three sources, and they are the only three a library gets:
    ``CELERY_TASK_QUEUES`` (kombu ``Queue`` objects, dicts or bare strings),
    ``CELERY_TASK_DEFAULT_QUEUE``, and any ``queue`` option pinned in
    ``CELERY_TASK_ROUTES``. A worker's ``-Q`` lives on a command line in a
    compose file; nothing here can read it.
    """
    from django.conf import settings

    names: set[str] = set()

    default_queue = getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", None)
    if default_queue:
        names.add(str(default_queue))

    for entry in getattr(settings, "CELERY_TASK_QUEUES", None) or ():
        name = getattr(entry, "name", None)
        if name is None and isinstance(entry, dict):
            name = entry.get("name") or entry.get("queue")
        if name is None and isinstance(entry, str):
            name = entry
        if name:
            names.add(str(name))

    routes = getattr(settings, "CELERY_TASK_ROUTES", None) or {}
    if isinstance(routes, dict):
        for route in routes.values():
            if isinstance(route, dict) and route.get("queue"):
                names.add(str(route["queue"]))

    return names


@checks.register("stapel_cdn")
def check_variant_queues(app_configs=None, **kwargs):
    """W008 — a variant queue this deployment names and nothing here consumes.

    The defect this exists for was live: ``generate_thumbnails`` and
    ``generate_previews`` were pinned to the literal queues ``thumbnails``
    and ``previews`` inside ``tasks.py``. A deployment that shards per
    service by setting ``CELERY_TASK_DEFAULT_QUEUE`` ran **no** consumer on
    either, so uploads answered 201 with a full ladder of ``variant_*_url``s
    that 404'd forever, and no log line anywhere said so. The queue names are
    settings now (``THUMBNAILS_QUEUE`` / ``PREVIEWS_QUEUE``, default ``None``
    = the app's own default queue, which a vanilla worker already drains).

    **This check is deliberately narrow, and honest about why.** It fires
    only when the settings name an explicit queue that does not appear in
    anything this process can read — ``CELERY_TASK_QUEUES``,
    ``CELERY_TASK_DEFAULT_QUEUE``, ``CELERY_TASK_ROUTES``. A library cannot
    see the fleet's compose file, its systemd units or the ``-Q`` on a worker
    command line, so it cannot *prove* a consumer exists; it can only report
    that nothing in this process's own configuration corroborates the name.
    Deployment-level verification (does a worker for this queue actually
    exist in the deploy?) is ADO-class and lives in stapel-tools, next to
    ADO005 — the same family of "installed, configured, and nobody running
    the process that makes it true".

    Silent by construction in the default single-queue posture: both
    settings unset means both sends carry no ``queue`` option at all, and
    there is nothing to corroborate.
    """
    from .conf import cdn_settings
    from .tasks import QUEUE_SETTINGS

    named = []
    for setting_key in sorted(set(QUEUE_SETTINGS.values())):
        raw = getattr(cdn_settings, setting_key, None)
        value = str(raw).strip() if raw else ""
        if value:
            named.append((setting_key, value))
    if not named:
        return []

    declared = _declared_queue_names()
    unproven = [(key, value) for key, value in named if value not in declared]
    if not unproven:
        return []

    listed = ", ".join(f"STAPEL_CDN['{key}']={value!r}" for key, value in unproven)
    return [
        checks.Warning(
            f"{listed} — this deployment routes variant generation to a "
            f"queue no Django-visible setting mentions "
            f"(CELERY_TASK_QUEUES, CELERY_TASK_DEFAULT_QUEUE, "
            f"CELERY_TASK_ROUTES). If no worker consumes it, uploads still "
            f"answer 201 with every variant_<size>_url filled in and every "
            f"one of them 404s forever — the failure is invisible from both "
            f"ends.",
            hint=(
                "Confirm a worker drains it (`celery -A <app> worker -Q "
                f"{unproven[0][1]}`), declare it in CELERY_TASK_QUEUES so "
                "this check can see it, or drop the setting to send on the "
                "app's default queue. This library cannot read your compose "
                "file or worker command line, so it cannot verify the "
                "consumer — that is an ADO-class check over the deployment "
                "in stapel-tools (stapel-adoption-lint), the same family as "
                "ADO005. Either way, schedule "
                "stapel_cdn.tasks.get_cdn_beat_schedule() so "
                "retry_unprocessed picks up what a lost message stranded."
            ),
            id=W008_VARIANT_QUEUE_UNPROVEN,
        )
    ]


@checks.register("stapel_cdn")
def check_media_kinds(app_configs=None, **kwargs):
    """W009 — a ``MEDIA_KINDS`` overlay entry that does not parse.

    The registry is read on every ``cdn.describe``, so a malformed entry
    would otherwise surface as a 500 on somebody's attachment. A warning
    rather than an error because the failure is contained: nothing renders
    against a kind that could not be built, and the shipped kinds still
    resolve — but the host wrote an entry that is doing nothing.
    """
    from .kinds import MediaKindConfigError, get_media_kinds

    try:
        get_media_kinds()
    except MediaKindConfigError as exc:
        return [
            checks.Warning(
                str(exc),
                hint="An entry is {'model': 'image'|'video'|'audio'|'file', "
                     "'extensions': (...), 'asset_types': (...), 'preview': "
                     "'blur'|'poster'|'waveform'|None, 'animated': bool}, or "
                     "None to remove a builtin kind.",
                id=W009_MEDIA_KINDS_INVALID,
            )
        ]
    return []


@checks.register("stapel_cdn")
def check_media_tools(app_configs=None, **kwargs):
    """W010 — half of ffmpeg is installed, which is worse than neither.

    ffmpeg and ffprobe normally ship together, and E002/E003 above probe
    for ``ffmpeg``. A deployment that has one without the other (a slim
    image that copied a single binary, a distro that splits the package)
    passes those checks and then silently produces attachments with a
    waveform and no duration, or a duration and no poster — a partial
    result that looks like a partial *file* rather than a partial install.
    Only reported once a submodule that uses them is enabled.
    """
    from . import probes
    from .conf import cdn_settings

    enabled = set(cdn_settings.ENABLED_SUBMODULES)
    if not enabled & {"video", "recordings"}:
        return []

    missing = []
    if not probes.ffmpeg_available():
        missing.append("ffmpeg")
    if not probes.ffprobe_available():
        missing.append("ffprobe")
    # Both missing is E002/E003's subject; this is about the split install.
    if len(missing) != 1:
        return []

    absent = missing[0]
    present = "ffprobe" if absent == "ffmpeg" else "ffmpeg"
    lost = (
        "poster frames and waveform strips"
        if absent == "ffmpeg"
        else "durations and video dimensions"
    )
    return [
        checks.Warning(
            f"'{present}' is on PATH but '{absent}' is not. The media "
            f"metadata pass will produce {lost} for nothing it stores, and "
            f"every affected row records meta_reason "
            f"'{absent}_missing' — the attachment looks half-broken to a UI "
            f"while the deployment looks correctly configured.",
            hint=f"Install the full ffmpeg suite (it ships '{absent}' too) "
                 f"on this image, then run `manage.py "
                 f"cdn_backfill_media_meta --retry-degraded`.",
            id=W010_MEDIA_TOOL_MISSING,
        )
    ]


@checks.register("stapel_cdn")
def check_preview_budget(app_configs=None, **kwargs):
    """W011 — an inline-preview budget nobody can act on.

    A warning, not an error, for the reason W007 is: the module stays
    bounded either way (``metadata.preview_budget`` falls back to the
    shipped default rather than treating an unusable value as "no limit").
    It is reported because the operator who wrote
    ``MICRO_PREVIEW_MAX_BYTES = "8kb"`` believes they raised the ceiling.
    """
    from .conf import cdn_settings
    from .metadata import DEFAULT_PREVIEW_BUDGET

    raw = cdn_settings.MICRO_PREVIEW_MAX_BYTES
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return []
    return [
        checks.Warning(
            f"STAPEL_CDN['MICRO_PREVIEW_MAX_BYTES'] is {raw!r}, which is not "
            f"a positive number of bytes. The module falls back to "
            f"{DEFAULT_PREVIEW_BUDGET} rather than inlining unbounded base64 "
            f"into every attachment payload — but the setting is not doing "
            f"what it was written to do.",
            hint="Set a positive byte count measured on the finished data: "
                 "URI (a 16px WebP LQIP is ~300-800 B, a waveform strip "
                 "~1.5-3 KB). There is no 'unlimited': base64 in a JSON "
                 "payload is page weight, and page weight has a ceiling.",
            id=W011_PREVIEW_BUDGET_INVALID,
        )
    ]


@checks.register("stapel_cdn")
def check_describe_seam(app_configs=None, **kwargs):
    """E005/W012 — the guard and the rate of ``POST /describe/``.

    Both settings are resolved per request rather than pinned at import, which
    is what lets a host swap them without subclassing a view — and also what
    would otherwise turn an authoring mistake into a 500 on every attachment a
    page tries to draw, discovered from an error rate. The same argument as
    W009: a registry read inside a request is checked at boot.

    E005 for a dotted path that will not import or a rate DRF cannot parse:
    the endpoint cannot be served at all in this deployment. W012 for an
    empty guard: DRF reads no permission classes as "everyone passes", so a
    blank list publishes the endpoint to anonymous callers — which is a
    legitimate deployment, but one that says so with ``AllowAny``.
    """
    from django.utils.module_loading import import_string
    from rest_framework.throttling import SimpleRateThrottle

    from .conf import cdn_settings

    errors = []

    guard = cdn_settings.DESCRIBE_PERMISSIONS
    for dotted_path in guard or []:
        try:
            import_string(dotted_path)
        except ImportError as exc:
            errors.append(
                checks.Error(
                    f"STAPEL_CDN['DESCRIBE_PERMISSIONS'] names "
                    f"{dotted_path!r}, which does not import ({exc}). The "
                    f"guard is resolved per request, so every POST "
                    f"/cdn/api/v1/describe/ would answer 500 — no attachment "
                    f"on any page could be described.",
                    hint="Name an importable DRF permission class. The "
                         "default is stapel_cdn.permissions."
                         "IsAuthenticatedOrService (the seam the read "
                         "endpoints use); see CONFIG.MD.",
                    id=E005_DESCRIBE_SEAM_UNUSABLE,
                )
            )

    if not guard:
        errors.append(
            checks.Warning(
                "STAPEL_CDN['DESCRIBE_PERMISSIONS'] is empty, so POST "
                "/cdn/api/v1/describe/ has NO guard: DRF reads no permission "
                "classes as 'everyone passes'. Any anonymous caller holding a "
                "media ref can resolve its render metadata.",
                hint="If that is intended, say it: "
                     "['rest_framework.permissions.AllowAny'] — and set "
                     "DESCRIBE_ANON_THROTTLE, which is then the only brake. "
                     "Otherwise restore the default, "
                     "['stapel_cdn.permissions.IsAuthenticatedOrService'].",
                id=W012_DESCRIBE_GUARD_EMPTY,
            )
        )

    for key in ("DESCRIBE_THROTTLE", "DESCRIBE_ANON_THROTTLE"):
        rate = getattr(cdn_settings, key)
        if not rate:
            # Falsy is "no throttle for this class of caller", not a mistake.
            continue
        try:
            SimpleRateThrottle.parse_rate(None, rate)
        except (ValueError, TypeError, IndexError, AttributeError):
            errors.append(
                checks.Error(
                    f"STAPEL_CDN[{key!r}] is {rate!r}, which DRF cannot parse "
                    f"as a throttle rate. The rate is read per request, so "
                    f"every POST /cdn/api/v1/describe/ would answer 500.",
                    hint="Use '<number>/<second|minute|hour|day>', e.g. "
                         "'60/min'. Leave it empty to run that class of "
                         "caller unthrottled.",
                    id=E005_DESCRIBE_SEAM_UNUSABLE,
                )
            )

    return errors


__all__ = [
    "E001_IMAGES_LIBRARY_MISSING",
    "E004_IMAGE_FORMAT_UNDECODABLE",
    "E002_VIDEO_BINARY_MISSING",
    "E003_RECORDINGS_BINARY_MISSING",
    "W005_DEDUP_SCOPE_INVALID",
    "W006_DEDUP_SCOPE_GLOBAL",
    "W007_QUOTA_CEILING_INVALID",
    "W008_VARIANT_QUEUE_UNPROVEN",
    "W009_MEDIA_KINDS_INVALID",
    "W010_MEDIA_TOOL_MISSING",
    "W011_PREVIEW_BUDGET_INVALID",
    "E005_DESCRIBE_SEAM_UNUSABLE",
    "W012_DESCRIBE_GUARD_EMPTY",
    "check_describe_seam",
    "check_dedup_scope",
    "check_media_kinds",
    "check_media_tools",
    "check_owner_quotas",
    "check_preview_budget",
    "check_submodule_binaries",
    "check_variant_queues",
]
