# stapel-cdn — MODULE.md

Agent-facing map of this module: what it provides, its fork-free extension points, and
anti-patterns. Use it to classify a desired change as **app-layer override** (do it in
the host project via an extension point below) vs **upstream contribution** (change this
package via the contribution pipeline — see `docs/stdlib-contribution-pipeline.md` and
system-design §8.6 in the platform docs). Stapel modules never import each other; all
customization must be possible **without forking**.

Package: `stapel-cdn` (PyPI) · Django app: `stapel_cdn` (app label `cdn`) ·
Depends on `stapel-core` only · Optional extras: `images` (pyvips — core, unconditional
system check), `video`/`recordings` (ffmpeg — a system binary, not a pip package; these
extras are opt-in markers, paired with `STAPEL_CDN["ENABLED_SUBMODULES"]`), `files` (no
extra needed — passthrough), `s3` (boto3, reserved). See the submodule table below and
`CONFIG.MD` for the full settings registry.

## What this module provides

- **Models** (`stapel_cdn.models`): `Image`, `Video`, `File`, `Audio` — content-addressed
  media, deduplicated by SHA-256 `file_hash`. Each carries a `refs` JSONField tracking
  `service/entity_type/entity_id` back-references from other modules' entities.
  `uploaded_by` FKs `settings.AUTH_USER_MODEL`. `Audio` (the "recordings" submodule,
  cdn-modularity.md §7.2) is passthrough storage always available — no extra required;
  `is_compressed` tracks the separate, still-unimplemented ffmpeg-audio compression pass
  (`services.AudioProcessingService`, a documented stub — never silently marks a
  recording compressed).
- **HTTP API** (`stapel_cdn.urls` → v1 canon `/cdn/api/v1/...`, api-versioning.md §2;
  the URL set itself lives in `stapel_cdn.urls_v1`): `upload/image/`, `upload/avatar/`,
  `upload/video/`, `upload/file/`, `images/<type>/upload/`, `images/<type>/random/`,
  `file/exists/` (GET and POST), `refs/sync/` (service-to-service, `IsServiceRequest`).
- **Image processing pipeline** (`stapel_cdn.services.ImageProcessingService`): libvips
  via `pyvips` — aspect-friendly tier semantics (images-and-cdn.md): thumbnail tiers
  (16/32/64/120) are **min-side** resized (`MEDIA_ROOT/<type>/<hash>/<size>.webp`),
  preview tiers (160/240/480/560/720/1080) generate **two branches** per tier —
  `{T}w.webp` (width == T) and `{T}h.webp` (height == T) — with an independent ladder
  downscale per branch and no upscaling anywhere. Square images (±1px) generate only
  the w-branch (`square` flag in the render metadata marks branches equivalent).
  Per-variant geometry `{tier, branch, url, width, height}` is persisted in
  `Image.variants_meta`. Embedded-thumbnail fast path (HEIC `heifload(thumbnail=True)`,
  JPEG `shrink=8`), optional watermark via a pluggable engine (off by default). Runs
  async on Celery queues `thumbnails` (high priority) and `previews`;
  `retry_unprocessed` task re-queues stuck images. `manage.py regenerate_media` wipes
  generated variants and re-runs the pipeline (the operational relaunch step —
  no compatibility file layouts are kept).
- **Upload safety** (`stapel_cdn.validators`, `stapel_cdn.upload_handlers`):
  `validate_image_file` (extension allowlist → Pillow decode check → decompression-bomb
  cap); `SpeedLimitUploadHandler` (5-min absolute timeout, 2 KB/s sliding-window minimum
  speed) — opt-in via Django `FILE_UPLOAD_HANDLERS`.
- **Comm surface**: provides functions `cdn.media_exists`, `cdn.describe`
  (render-metadata snapshot `{mime, bytes, width, height, aspect, duration_ms,
  preview_b64, square, variants[]}` — `preview_b64` is the 16px micro tier as a
  `data:image/webp;base64,...` URI; consumers denormalize the snapshot once when
  resolving a ref) and `cdn.refs_sync`
  (`stapel_cdn.functions`, called via `stapel_core.comm.call` — no import of this package
  needed); subscribes to actions `user.deleted` / `user.deletion_initiated` (`stapel_cdn.actions`); Kafka consumer
  `manage.py consume_cdn_events` for `cdn.ref.sync` events (topic
  `stapel.cdn.ref-sync`, overridable via `STAPEL_TOPIC_CDN_REF_SYNC` in stapel-core).
- **GDPR** (`stapel_cdn.gdpr.CDNGDPRProvider`, section `media`): export / staged export /
  delete (ref-counted: unreferenced files deleted, referenced files anonymized),
  registered in `CdnConfig.ready()`.
- **Public API** (`stapel_cdn.__all__`, lazily exported, Django-free import):
  `cdn_settings`, `media_exists`, `refs_sync`, `validate_image_file`.

## Extension points (fork-free)

### Settings — `STAPEL_CDN` namespace

`stapel_cdn.conf.cdn_settings` (a `stapel_core.conf.AppSettings`).
Resolution order per key: `settings.STAPEL_CDN` dict → flat Django setting of the same
name → environment variable → built-in default. Test-safe: caches invalidate on
`setting_changed`.

See `CONFIG.MD` for the complete registry (source/required/default per key). Highlights:

| Key | Default | What it customizes |
|---|---|---|
| `ASSET_TYPES` | `("avatar",)` | `Image.type` choices — **same `STAPEL_CDN` key the client-side `stapel_core.django.cdn.CdnImageField` reads** (cdn-modularity.md §2.1/§5, replacing the pre-0.8.0 `IMAGE_TYPES` key). Read through the callable `models.get_image_type_choices`, so adding types never produces a model/migration change. Accepts `(value, label)` pairs or plain strings. `TypedImageUploadView` and `RandomImageView` validate against it. Values must fit `max_length=10`. |
| `ENABLED_SUBMODULES` | `("images",)` | Which of `images`/`video`/`recordings` this deployment turns on. `images` needs no opt-in (its system check always runs); adding `"video"`/`"recordings"` is what activates `checks.check_submodule_binaries`'s ffmpeg probe for that submodule. |
| `THUMBNAIL_SIZES` | `(16, 32, 64, 120)` | Thumbnail tiers: min-side resize, no branches, no watermark, high-priority queue. 16 is the micro tier inlined as `preview_b64` by `cdn.describe`. |
| `PREVIEW_SIZES` | `(160, 240, 480, 560, 720, 1080)` | Preview tiers: two branches per tier (`{T}w.webp` / `{T}h.webp`), watermark-capable, normal-priority queue. |
| `MAX_IMAGE_SIZE` | `20 * 1024 * 1024` (20 MiB) | Upload size cap, checked before hashing. |
| `ALLOWED_IMAGE_EXTENSIONS` | `.jpg .jpeg .png .gif .webp .bmp .heic .heif` | Image extension allowlist in views, serializers and `validate_image_file`. |
| `ALLOWED_VIDEO_EXTENSIONS` | `.mp4 .webm .mov .avi .mkv` | Video extension allowlist (`FileUploadSerializer`, `VideoUploadView`). |
| `ALLOWED_AUDIO_EXTENSIONS` | `.mp3 .wav .m4a .ogg .opus .flac .aac` | Audio extension allowlist (`recordings` submodule — passthrough storage always accepts these regardless of `ENABLED_SUBMODULES`). |
| `MAX_IMAGE_PIXELS` | `50_000_000` | Pillow decompression-bomb cap (`PIL.Image.MAX_IMAGE_PIXELS`). |
| `MAX_AUDIO_SIZE` | `50 * 1024 * 1024` (50 MiB) | Upload size cap for audio recordings. |
| `WATERMARK` | `""` (**off**) | Watermark engine: dotted path to (or directly a) callable `(pyvips.Image) -> pyvips.Image` applied to preview variants. Empty disables watermarking. Built-in reference engine: `stapel_cdn.watermarks.text_watermark`. |
| `WATERMARK_TEXT` | `""` | Label rendered by the built-in `text_watermark` engine (bottom-right corner). Ignored by custom engines unless they read it. |

### Media submodules — extras, opt-in, and system checks (tag `stapel_cdn`)

cdn-modularity.md §2.2/§3. `checks.check_submodule_binaries` runs at `manage.py check` /
boot-smoke time, not at first use:

| Submodule | Model | Binary/library | Opt-in via `ENABLED_SUBMODULES`? | System check |
|---|---|---|---|---|
| `images` | `Image` | `libvips` (system, apt `libvips-dev`) + `pyvips` (pip, extra `images`) | No — core, unconditional | `stapel_cdn.images.E001` if `pyvips` isn't importable. Without it, `Image.save()` falls back to 1x1 placeholder dimensions with a loud `ERROR` log (no longer a silent `except Exception: pass`). |
| `recordings` | `Audio` | none for storage (always on); `ffmpeg` (system) once a real compression pipeline exists | Yes — gates the *compression* check only, storage is unconditional | `stapel_cdn.recordings.E003` if `"recordings"` is enabled and `ffmpeg` is missing |
| `video` | `Video` | `ffmpeg` (system) — VPS/prod-only, never the stapel-studio devcontainer | Yes | `stapel_cdn.video.E002` if `"video"` is enabled and `ffmpeg` is missing |
| `files` | `File` | none — passthrough, no processing | N/A (no extra) | none |

### Storage / processing backends (dotted paths)

| Seam | Current state | Fork-free? |
|---|---|---|
| File storage | `stapel_cdn.storage.cdn_storage` — a module-level `OverwriteStorage(FileSystemStorage)` instance baked into `Image.original` / `File.original` `FileField(storage=...)` | **No dotted-path seam.** Not selectable via `STAPEL_CDN`; S3/remote storage support (the `s3` extra exists in `pyproject.toml` but is unused by code) is an upstream contribution. |
| Watermark engine | `STAPEL_CDN["WATERMARK"]` — dotted path (via `import_strings`) or direct callable `(pyvips.Image) -> pyvips.Image`; off by default | **Yes.** The only dotted-path key in the namespace. Built-in reference: `stapel_cdn.watermarks.text_watermark` (renders `WATERMARK_TEXT`). |
| Image pipeline | `services.ImageProcessingService` classmethods (`process_image`, `generate_thumbnails_only`, `generate_previews_only`, `WEBP_QUALITY=85`, `JPEG_QUALITY=85`) | Subclassable, but call sites (`tasks.py`, `models.py` post_save signal, `admin.py`) import the class directly — a replacement class cannot be injected via settings. Behavior *is* conf-driven through `THUMBNAIL_SIZES`/`PREVIEW_SIZES` and `WATERMARK`. Anything else (quality, formats) is upstream. |
| Upload throttling | `upload_handlers.SpeedLimitUploadHandler` | Yes — plain Django upload handler; enable/replace via `FILE_UPLOAD_HANDLERS` in the host project. Its constants (`UPLOAD_MAX_TIME=300`, `UPLOAD_MIN_SPEED=2048`) are module-level, not conf keys. |

### Swappable models

None. No model in this package is swappable; the only swap honored is Django's
`AUTH_USER_MODEL` (all `uploaded_by` FKs). Changing `Image`/`Video`/`File`/`Audio` schema
is an upstream contribution. `Image.type` values, however, are extendable via
`ASSET_TYPES` (see above) without touching the model.

### Serializer seams

Every view in `stapel_cdn.views` mixes in `SerializerSeamMixin` with two class attributes
and two getters — swap serializers (or add per-request logic) by subclassing the view and
re-routing the URL in the host project, without copying view bodies:

```python
class MyImageUpload(ImageUploadView):
    response_serializer_class = MyImageUploadResponseSerializer  # or override
    # get_request_serializer_class() / get_response_serializer_class()
```

| View | `request_serializer_class` | `response_serializer_class` |
|---|---|---|
| `ImageUploadView` | `FileUploadSerializer` | `ImageUploadResponseSerializer` |
| `AvatarUploadView` | `FileUploadSerializer` | `ImageUploadResponseSerializer` |
| `TypedImageUploadView` | `FileUploadSerializer` | `ImageUploadResponseSerializer` |
| `VideoUploadView` | `FileUploadSerializer` | `VideoUploadResponseSerializer` |
| `GenericFileUploadView` | `None` (raw `request.FILES`) | `FileUploadResponseSerializer` |
| `FileExistsView` | `FileExistsSerializer` (POST body) | `FileExistsResponseSerializer` |
| `RandomImageView` | `None` (GET only) | `ImageSerializer` |
| `RefSyncView` | `RefSyncRequestSerializer` | `RefSyncResponseSerializer` |

### Events & functions (comm surface)

| Name | Direction | Contract |
|---|---|---|
| `cdn.media_exists` | provides (function) | `call("cdn.media_exists", {"ref": "<type>/<hash>"})` → `{"exists": bool}`. Ref prefixes: any configured `STAPEL_CDN["ASSET_TYPES"]` value (default `avatar`), `video`, `file`, `audio`. |
| `cdn.refs_sync` | provides (function) | `call("cdn.refs_sync", {"service", "entity_type", "entity_id", "old_hashes", "new_hashes"})` → `{"added", "removed", "errors"}`. Same logic as `RefSyncView` / `services.apply_ref_sync`. |
| `user.deleted` | subscribes (action) | Erases this module's PII via `CDNGDPRProvider.delete()` and, when the payload carries a `correlation_id`, confirms with `gdpr.section.erased` (`service: "media"`) so the gdpr orchestrator can complete the closure. Schema: `schemas/consumes/user.deleted.json`. Handler is idempotent (at-least-once delivery). |
| `user.deletion_initiated` | subscribes (action) | Grace period started: purges the user's *unreferenced* media (`refs == []`) via `CDNGDPRProvider.purge_unreferenced()`; referenced media keeps serving (and its ownership link) until `user.deleted` — grace is cancellable. Schema: `schemas/consumes/user.deletion_initiated.json`. Idempotent. |
| `cdn.ref.sync` | consumes (bus) | `manage.py consume_cdn_events` (Kafka topic `stapel.cdn.ref-sync`); the producer-side helper `sync_cdn_refs()` lives in `stapel_core.django.cdn.ref_sync`, so other modules publish without importing this package. |

Registration happens in `CdnConfig.ready()`; transport (in-process vs bus) is chosen by
`STAPEL_COMM` in stapel-core — the same handlers serve monolith and microservices.

### Signals

| Signal | Sender / payload | When |
|---|---|---|
| `stapel_core.signals.media_processed` | `sender=Image` class, `instance=<Image>` | Sent by `ImageProcessingService.process_image()` after all variants are generated. In-process extension point for the host project (cache warm-up, denormalization). Caveat: the Celery split path (`generate_thumbnails` + `generate_previews` tasks) does **not** currently emit it — only the combined `process_image()` path (e.g. admin reprocess) does. |
| Django `post_save` on `Image` / `Video` | internal receivers in `models.py` | Enqueue `process_image_async` / run `VideoProcessingService.process_video`. Internal wiring, not a public hook — attach your own `post_save` receivers rather than replacing these. |

Error keys: `errors.py` registers `error.400.*` / `error.413.*` / `error.404.*` keys via
`stapel_core` `register_service_errors`; `CdnErrorKeysView.get_service_errors()` is an
overridable listing seam.

## Admin categories (`stapel_core.access`)

`Image`, `Video`, `File`, `Audio` are left **undecorated** — implicit `@access.standard`
(business). All four are staff-facing moderation tables (the admin exposes preview
thumbnails, orphan filters, variant regeneration actions), not machinery nobody is
meant to open, so `@access.ops` does not apply; `file_hash` is a content-addressing
SHA-256 digest, not a credential, and no model in this package carries a signing key,
upload token, or other secret field, so `@access.secret` does not apply either. The
SSRF-hardened `cdn.import_from_url` fetcher (`fetch.py`) is stateless — it persists no
job/log/audit row of its own (it writes a normal `Image` on success) — so there is no
additional ops-shaped model to classify. Net result of the AS-5 sweep for this package:
zero decorators added.

## Anti-patterns

- **Forking to add an image type or variant size.** Both are settings
  (`STAPEL_CDN["ASSET_TYPES"]`, `STAPEL_CDN["THUMBNAIL_SIZES"]`/`["PREVIEW_SIZES"]`); `Image.type` choices are
  a callable, so no migration is generated. Keep custom type values ≤ 10 chars
  (`max_length=10` on the column). `ASSET_TYPES` is the same key/namespace the
  client-side `stapel_core.django.cdn.CdnImageField` reads — set it once.
- **Assuming a broad `except Exception` around pyvips is harmless.** It isn't — a
  swallowed pyvips failure used to silently produce 1x1 "dimensions" for every image in
  the deployment (cdn-modularity.md §0.3). `Image.save()` now distinguishes "pyvips not
  installed" from "file unreadable" and logs an `ERROR` either way; `checks.
  check_submodule_binaries` (`stapel_cdn.images.E001`) catches the missing-library case
  at boot-smoke time. Don't reintroduce a bare `except: pass` around media processing.
- **Importing `stapel_cdn` from another Stapel module.** Cross-module calls go through
  `stapel_core.comm.call("cdn.media_exists", ...)` / `call("cdn.refs_sync", ...)` or the
  bus (`sync_cdn_refs` in stapel-core). Modules never import each other.
- **Mutating `refs` JSONField directly.** Ref bookkeeping is transactional
  (`select_for_update` in `services.apply_ref_sync`); go through `cdn.refs_sync`,
  `RefSyncView`, or the `cdn.ref.sync` bus event. Direct writes race with concurrent syncs
  and break GDPR ref-counted deletion.
- **Processing images synchronously in the request path.** The `post_save` signal is
  async-only by design — a sync fallback would run the full pyvips pipeline inside the
  upload request whenever the broker is down (trivial CPU DoS). Stuck images are
  re-queued by the `retry_unprocessed` periodic task.
- **Copying a view body to change its serialization.** Subclass the view and swap
  `request_serializer_class` / `response_serializer_class` (or the getters), then point
  your URLconf at the subclass.
- **Expecting image variants to be FileFields.** Image variants are derived files at
  `MEDIA_ROOT/<type>/<hash>/{tier}.webp` (thumbnails) and `{tier}w/h.webp` (preview
  branches), addressed by URL convention (`Image.variant_urls`, `get_variant_url`)
  with per-variant geometry in `Image.variants_meta`. Don't write to those paths
  yourself and don't assume a DB row per variant (only `Video` has variant
  FileFields).
- **Non-idempotent action handlers.** Anything subscribed via `on_action` must tolerate
  redelivery (outbox retries, at-least-once broker semantics).
- **Reading flat `CDN_*` settings.** The legacy flat aliases are gone; code reads
  `cdn_settings.<KEY>` so the namespace dict, env vars and test overrides all work.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change is expressible as:
a `STAPEL_CDN` key from the table above; a view subclass swapping serializer seams plus a
URL re-route; a `media_processed` / `post_save` receiver; a `FILE_UPLOAD_HANDLERS` entry;
a comm call or a consumer of the events above; an additional GDPR provider registered in
your own app.

**Upstream contribution** (this repo, via `contrib_open` → review origin → PyPI release)
if it needs: a new settings key or a dotted-path `import_strings` seam (e.g. making the
storage backend or processing service class configurable); S3/presigned
uploads (`s3` extra is declared but unwired); video variant/poster generation (ffmpeg —
currently `VideoProcessingService.process_video`, a documented stub that only marks
`is_processed`, same pattern as `stapel_geo.search.elasticsearch`); ffmpeg-audio
compression for `Audio` (`AudioProcessingService.compress_audio`, also a stub —
`recordings` storage itself is already usable without it); emitting `media_processed` from
the Celery task path; new endpoints, model fields, or migrations; changing WebP/JPEG
quality or upload-handler thresholds (currently hardcoded constants).

Litmus test: if you'd have to monkeypatch, copy a module file, or edit code inside
`stapel_cdn/` to get the behavior — it's upstream. If a setting, subclass, receiver, or
comm call gets you there — it's app-layer.
