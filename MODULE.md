# stapel-cdn — MODULE.md

Agent-facing map of this module: what it provides, its fork-free extension points, and
anti-patterns. Use it to classify a desired change as **app-layer override** (do it in
the host project via an extension point below) vs **upstream contribution** (change this
package via the contribution pipeline — see `docs/stdlib-contribution-pipeline.md` and
system-design §8.6 in the platform docs). Stapel modules never import each other; all
customization must be possible **without forking**.

Package: `stapel-cdn` (PyPI) · Django app: `stapel_cdn` (app label `cdn`) ·
Depends on `stapel-core` only · Optional extras: `images` (pyvips — the one image decoder,
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
  (`services.AudioProcessingService.compress_audio`, a documented stub — never silently
  marks a recording compressed). `AudioProcessingService.extract_metadata` is real:
  duration + the base64 waveform strip a voice message renders with.
  `Image`/`Video`/`Audio` each carry `preview_b64` (the inline placeholder) and
  `meta_reason` (the named reason one is missing); `Video` adds `has_poster`.
- **HTTP API** (`stapel_cdn.urls` → v1 canon `/cdn/api/v1/...`, api-versioning.md §2;
  the URL set itself lives in `stapel_cdn.urls_v1`): `upload/image/`, `upload/avatar/`,
  `upload/video/`, `upload/file/`, `images/<type>/upload/`, `images/<type>/random/`,
  `file/exists/` (GET and POST), `describe/` (batch render metadata for refs the caller
  holds but did not necessarily upload — the browser's half of `cdn.describe_many`;
  settings-guarded and throttled, see **The render-metadata contract** below),
  `refs/sync/` (service-to-service, `IsServiceRequest`).
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
  `retry_unprocessed` task re-queues stuck images, videos and recordings;
  `sweep_unclaimed` reaps media left zero-ref past its TTL (see *Background work*).
  `manage.py regenerate_media` wipes generated variants and re-runs the pipeline
  (the operational relaunch step — no compatibility file layouts are kept);
  `manage.py cdn_backfill_media_meta` stamps render metadata on objects stored
  before the metadata pipeline existed (idempotent, resumable — see
  **Backfilling render metadata** below).
- **Upload safety** (`stapel_cdn.validators`, `stapel_cdn.upload_handlers`):
  `validate_image_file` (extension allowlist → libvips decode check → decompression-bomb
  cap); `SpeedLimitUploadHandler` (5-min absolute timeout, 2 KB/s sliding-window minimum
  speed) — opt-in via Django `FILE_UPLOAD_HANDLERS`.
- **Render metadata** (`stapel_cdn.metadata`, `stapel_cdn.kinds`,
  `stapel_cdn.probes`): every attachment carries what a UI needs to draw it
  with no second round trip and no layout jump — aspect box, byte size, an
  inline placeholder, and duration for time-based media. Images get a 16px
  WebP blur-up produced in the **same** libvips pass that writes `16.webp`
  (the encoded buffer is reused, not re-read); video gets ffprobe
  dimensions/duration plus one extracted frame that feeds both
  `MEDIA_ROOT/video/<hash>/poster.webp` and the inline micro poster; voice
  messages get ffprobe duration plus a waveform strip rendered by ffmpeg's
  `showwavespic`; documents get mime + extension. Every inline preview is
  bounded by `STAPEL_CDN["MICRO_PREVIEW_MAX_BYTES"]` and every gap is named
  (`meta_status`/`meta_reason`). See **The render-metadata contract** below.
- **Comm surface**: provides functions `cdn.media_exists`, `cdn.describe`,
  `cdn.describe_many` (the render-metadata snapshot for one ref / a page of
  refs — see the contract table below; consumers denormalize the snapshot once
  when resolving a ref; `describe_many` and `POST /describe/` are one function
  behind two transports) and `cdn.refs_sync`
  (`stapel_cdn.functions`, called via `stapel_core.comm.call` — no import of this package
  needed); subscribes to actions `user.deleted` / `user.deletion_initiated` / `user.merged` (`stapel_cdn.actions`); Kafka consumer
  `manage.py consume_cdn_events` for `cdn.ref.sync` events (topic
  `stapel.cdn.ref-sync`, overridable via `STAPEL_TOPIC_CDN_REF_SYNC` in stapel-core).
- **GDPR** (`stapel_cdn.gdpr.CDNGDPRProvider`, section `media`): export / staged export /
  delete (ref-counted: unreferenced files deleted, referenced files anonymized),
  registered in `CdnConfig.ready()`. A blob that cannot be unlinked raises
  `MediaErasureIncomplete` and keeps its row — returning normally is the
  receipt the gdpr orchestrator acts on, so it may only mean the bytes are
  gone.
- **Public API** (`stapel_cdn.__all__`, lazily exported, Django-free import):
  `cdn_settings`, `media_exists`, `refs_sync`, `validate_image_file`.

## The render-metadata contract

One snapshot, five ways to reach it: `cdn.describe` (one ref),
`cdn.describe_many` (a page of refs), **`POST /cdn/api/v1/describe/`** (a page
of refs, over HTTP, for callers that have no comm bus — i.e. browsers),
`render_meta` on the upload/read serializers, and
`stapel_cdn.metadata.build_render_metadata(obj)` in-process. They return the
**same object**, so an HTTP client and a service caller never build against two
shapes that drift. The two batch forms are literally one function
(`services.describe_refs`) behind two transports.

| Field | Type | Meaning |
|---|---|---|
| `ref` | `str` | `<prefix>/<hash>` — what resolves back to this object. |
| `kind` | `str \| null` | From the open registry (`STAPEL_CDN["MEDIA_KINDS"]`): `image`, `gif`, `video`, `audio`, `file`, or a host kind. `null` only if a host removed the builtin for that model. |
| `mime` | `str` | Stored `mime_type` when there is one, else guessed from the extension. |
| `ext` | `str` | Lowercase, dot-prefixed (`".pdf"`); `""` when unknown. |
| `bytes` | `int` | Size of the original. |
| `width` / `height` | `int \| null` | Pixels. `null` for audio and documents, and for video before ffprobe has run. |
| `aspect` | `float \| null` | `width / height`, rounded to 6dp so the same image always serializes to the same number. |
| `square` | `bool` | Within the 1px epsilon (images-and-cdn.md §3.3): the preview branches are equivalent. |
| `animated` | `bool` | Property of the kind — `true` for `gif`, `video`, `audio` and any host kind that says so. |
| `duration_ms` | `int \| null` | Measured. **Never 0 for "unknown"** — an unmeasured duration is `null` with a reason. |
| `preview_b64` | `str \| null` | `data:image/webp;base64,...` (`image/png` only on the named no-libvips downgrade), at most `MICRO_PREVIEW_MAX_BYTES` bytes. `null` when refused or not yet generated. |
| `preview_kind` | `"blur" \| "poster" \| "waveform" \| null` | What `preview_b64` depicts. `null` = this kind has no inline preview (documents). Declared by the kind, so it is known even while `preview_b64` is still `null`. |
| `poster_url` | `str \| null` | Video only, and only once the poster file exists (`Video.has_poster`) — never a URL derived from the hash alone. |
| `meta_status` | `"ok" \| "partial" \| "missing"` | Whether everything this kind promises is present. |
| `meta_reason` | `str \| null` | Named reason when not `ok` — `stapel_cdn.metadata.REASONS`. |
| `variants` | `list[{tier, branch, url, width, height}]` | Thumbnail tiers (`branch: null`, min-side), preview branches (`"w"`/`"h"`), plus the `"original"` entry. |

**What each media type carries** (the owner's list, and where it comes from):

| Kind | Guaranteed once `meta_status == "ok"` | Produced by |
|---|---|---|
| `image` | `width`, `height`, `aspect`, `bytes`, `preview_b64` (16px WebP blur-up) | one libvips pass — the micro tier's encoded buffer *is* the preview |
| `gif` | the same, plus `animated: true` | same pass; the kind is decided by extension |
| `video` | `width`, `height`, `aspect`, `bytes`, `duration_ms`, `preview_b64` (micro poster), `poster_url` | one `ffprobe` call + one `ffmpeg` frame extraction |
| `audio` (voice) | `duration_ms`, `preview_b64` (waveform strip), `bytes` | `ffprobe` + `ffmpeg showwavespic` |
| `file` | `mime`, `ext`, `bytes` (`preview_kind: null` — nothing is pending) | read off the row |

**Named reasons** (`meta_reason`): `not_generated`, `decoder_missing`,
`preview_over_budget`, `source_missing`, `encode_failed`, `ffprobe_missing`,
`ffmpeg_missing`, `probe_failed`, `render_failed`, `tool_timeout`. There is no
path that returns an unexplained null: if a consumer sees `duration_ms: null`,
this field says whether it is "still coming", "this deployment has no ffmpeg"
or "the blob is gone".

**The byte budget.** `MICRO_PREVIEW_MAX_BYTES` (default **4096 bytes**,
measured on the finished `data:` URI) is enforced by *downgrade-then-refuse* —
WebP quality ladder (85 → 60 → 40), then a smaller waveform strip, then no
preview at all with `preview_over_budget`. Never truncation: a truncated base64
string is a broken image in every consumer, a `null` with a reason is a
placeholder every consumer already draws. The ceiling is re-applied on **read**,
so lowering it stops shipping older, larger payloads immediately.
`cdn.describe_many` is capped at 50 refs per call, because batch size is
response size.

### `POST /cdn/api/v1/describe/` — the browser's describe

A comm Function is unreachable from a browser. Without this endpoint a client
saw `render_meta` only for something it had **just uploaded itself** (inline in
the upload response), or for whatever a consuming module chose to denormalize
into its own serializer — so a chat bubble holding somebody else's
`<prefix>/<hash>` had nothing to draw, and an attachment renderer was not
expressible on the front end at all.

```http
POST /cdn/api/v1/describe/
{"refs": ["avatar/<hash>", "video/<hash>", "product/<hash>"]}

200
{"items": {"avatar/<hash>": { …the table above… }, "video/<hash>": {…}},
 "missing": ["product/<hash>"]}
```

| Rule | Behaviour |
|---|---|
| Ceiling | **50 refs**, `metadata.DESCRIBE_MANY_LIMIT` — the same constant `cdn.describe_many` enforces, applied by the same function. |
| Duplicates | Collapse **before** the ceiling: fifty-one mentions of one attachment cost one slot. |
| Unknown ref | **Data, not an error** — in `missing`, with 200. A page with one deleted attachment still renders the other thirty-nine. |
| Malformed ref | Also `missing`, not a 400: one bad entry must not cost the caller the other forty-nine snapshots. |
| Over the ceiling | `400 error.400.too_many_refs`, `params: {count, max}` — page the batch. |
| Over the rate | `429 error.429.too_many_requests`, `params: {retry_after}`, plus a `Retry-After` header. Not DRF's bare English `detail`. |
| Guard | `STAPEL_CDN["DESCRIBE_PERMISSIONS"]`, read at request time; default `stapel_cdn.permissions.IsAuthenticatedOrService` — the seam `FileExistsView` uses. Pinning `permission_classes` on a subclass still wins. |
| Throttle | `STAPEL_CDN["DESCRIBE_THROTTLE"]` (60/min) and `DESCRIBE_ANON_THROTTLE` (10/min, dormant until the guard is opened). Batch size is response size, so the rate bounds bytes, not just queries. |

**What it discloses, and why the guard can be that wide.** Describe answers
for refs the caller did **not** upload — that is the case it exists for. A ref
is `<prefix>/<sha256>`, so naming one is already evidence the caller was given
it, and the snapshot is geometry, duration and a ≤4 KB inline preview: it
carries **no uploader identity, no filename, no `refs[]`**. That is the whole
difference from `FileExistsView`, which returns the entire row and therefore
stays scoped to `uploaded_by=request.user`. A deployment that will not accept
even that sets `DESCRIBE_PERMISSIONS` to `IsServiceRequest` and keeps describe
service-side; one with public media opens it to `AllowAny`, and
`DESCRIBE_ANON_THROTTLE` is then the only brake.

Both settings are resolved **per request**, so both are checked at boot rather
than discovered from an error rate: `checks.E005` for a permission path that
does not import or a rate DRF cannot parse (either would 500 every describe
call), `checks.W012` for an *empty* `DESCRIBE_PERMISSIONS` — DRF reads no
permission classes as "everyone passes", so a blank publishes the endpoint.
`AllowAny` says the same thing deliberately and is silent.

**Denormalize the result once**, when the ref is resolved — it is an immutable
snapshot, not something to recompute per render.

### Media kinds — an open registry, not an enum

`STAPEL_CDN["MEDIA_KINDS"]`, merged over `stapel_cdn.kinds.BUILTIN_MEDIA_KINDS`
with the same semantics as every other Stapel registry (an entry replaces a
builtin, `None` removes it). A kind decides two things only: what
`preview_b64` holds, and whether the object moves. Adding stickers is a dict
literal, not a release:

```python
STAPEL_CDN = {
    "ASSET_TYPES": ("avatar", "sticker"),
    "MEDIA_KINDS": {
        "sticker": {"model": "image", "asset_types": ("sticker",),
                    "preview": "blur", "animated": True},
    },
}
```

Resolution is by model, narrowed by extension and (for images) `Image.type`;
most specific wins (`asset_types` +2, `extensions` +1), ties broken by name so
the answer never depends on dict ordering. A malformed entry is `checks.W009`,
not a 500 on somebody's attachment.

### Backfilling render metadata

```
manage.py cdn_backfill_media_meta [--kind image|video|audio] [--batch-size N]
                                  [--limit N] [--dry-run] [--retry-degraded]
```

Idempotent and resumable **by construction**: the candidate query is the resume
token — only rows still missing a preview are selected, paged by a primary-key
cursor. A crash halfway is resumed by re-running the same command; a second run
over a finished table writes nothing; `--limit` drains a large table in bounded
slices. A row that cannot be completed records its named reason and is counted
(with a per-reason tally in the output), never retried in a loop and never
reported as success. `--retry-degraded` re-attempts those rows — the normal
sequence after installing ffmpeg on a deployment that stored a month of voice
messages without it. Documents are not a population: their metadata is read off
the row.

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
| `MEDIA_KINDS` | `{}` | Open media-kind registry, merged over the builtins (`image`/`gif`/`video`/`audio`/`file`). Adds stickers or any later kind without a release; `None` removes a builtin. See **Media kinds** above. |
| `MICRO_PREVIEW_MAX_BYTES` | `4096` | Byte ceiling for ONE inline preview, measured on the finished `data:` URI. Downgrade-then-refuse, never truncation; applied on read as well as at ingest. |
| `DESCRIBE_PERMISSIONS` | `["stapel_cdn.permissions.IsAuthenticatedOrService"]` | **REPLACE** — guard of `POST /describe/`, dotted paths, ALL must pass. Default is the read-endpoint seam (signed in, guest sessions included, or an internal service call). Tighten to `IsServiceRequest` to keep describe service-side; open to `AllowAny` for public media. Read at request time, so a subclass pinning `permission_classes` still wins. |
| `DESCRIBE_THROTTLE` / `DESCRIBE_ANON_THROTTLE` | `"60/min"` / `"10/min"` | Rate of `POST /describe/` (DRF scope `cdn_describe`). Batch size is response size, so this bounds bytes, not just queries. The anon rate is dormant under the default guard and becomes the only brake once it is opened. |
| `WAVEFORM_SIZES` / `WAVEFORM_COLOR` | `((240, 40), (120, 32))` / `"#3f7fbf"` | Waveform strip geometry ladder and ink colour for `ffmpeg showwavespic`. |
| `POSTER_FRAME_AT` / `POSTER_MAX_WIDTH` | `1.0` / `720` | Which second the video poster is lifted from, and the width of the derived `poster.webp`. |
| `MEDIA_TOOL_TIMEOUT` | `30.0` | Ceiling on any single ffprobe/ffmpeg call; a hung tool degrades with `tool_timeout` instead of pinning a worker. |
| `THUMBNAIL_SIZES` | `(16, 32, 64, 120)` | Thumbnail tiers: min-side resize, no branches, no watermark. 16 is the micro tier inlined as `preview_b64` by `cdn.describe`. |
| `PREVIEW_SIZES` | `(160, 240, 480, 560, 720, 1080)` | Preview tiers: two branches per tier (`{T}w.webp` / `{T}h.webp`), watermark-capable. |
| `THUMBNAILS_QUEUE` / `PREVIEWS_QUEUE` | `None` | Celery queue each variant-generation task is sent on. `None` sends **no** `queue` option, so the task lands on the app's own default queue and a vanilla single-queue worker drains it. Set them only in a fleet that shards queues per service. See *Background work* below. |
| `RETRY_UNPROCESSED_SCHEDULE` | `{"minute": "*/5"}` | Crontab kwargs for the `retry_unprocessed` safety net, consumed by `tasks.get_cdn_beat_schedule()`. |
| `UNCLAIMED_TTL_HOURS` | `48` | Hours an upload may stay claimed by nothing before `services.sweep_unclaimed` reaps it (bytes + row). The clock is `unreferenced_since` (stamped at upload, cleared by a claim, restamped when the last ref detaches) — never `created_at`; referenced media is never swept. |
| `SWEEP_UNCLAIMED_SCHEDULE` | `{"minute": "0"}` | Crontab kwargs for the unclaimed-media sweep, consumed by `tasks.get_cdn_beat_schedule()`. See *Background work* below. |
| `MAX_IMAGE_SIZE` | `20 * 1024 * 1024` (20 MiB) | Upload size cap, checked before hashing. |
| `ALLOWED_IMAGE_EXTENSIONS` | `.jpg .jpeg .png .gif .webp .avif .heic .heif` | Image extension allowlist in views, serializers and `validate_image_file`. The default is exactly what a stock `pip install stapel-cdn[images]` decodes (the pyvips[binary] wheel carries jpeg/png/gif/webp/libheif), so it never trips `E004` out of the box — `.bmp` was in it until 0.17.1 and libvips has no native BMP reader at all. Widening it (`.bmp` via ImageMagick, `.tif`, `.svg`, `.jxl`, `.jp2`) is exactly what `E004` probes. |
| `ALLOWED_VIDEO_EXTENSIONS` | `.mp4 .webm .mov .avi .mkv` | Video extension allowlist (`FileUploadSerializer`, `VideoUploadView`). |
| `ALLOWED_AUDIO_EXTENSIONS` | `.mp3 .wav .m4a .ogg .opus .flac .aac` | Audio extension allowlist (`recordings` submodule — passthrough storage always accepts these regardless of `ENABLED_SUBMODULES`). |
| `MAX_IMAGE_PIXELS` | `50_000_000` | Decompression-bomb cap: an upload above this many pixels is refused. Exact since 0.10 — Pillow used to only raise above *2x* the configured number. |
| `MAX_AUDIO_SIZE` | `50 * 1024 * 1024` (50 MiB) | Upload size cap for audio recordings. |
| `WATERMARK` | `""` (**off**) | Watermark engine: dotted path to (or directly a) callable `(pyvips.Image) -> pyvips.Image` applied to preview variants. Empty disables watermarking. Built-in reference engine: `stapel_cdn.watermarks.text_watermark`. |
| `WATERMARK_TEXT` | `""` | Label rendered by the built-in `text_watermark` engine (bottom-right corner). Ignored by custom engines unless they read it. |

### Media submodules — extras, opt-in, and system checks (tag `stapel_cdn`)

cdn-modularity.md §2.2/§3. `checks.check_submodule_binaries` runs at `manage.py check` /
boot-smoke time, not at first use:

| Submodule | Model | Binary/library | Opt-in via `ENABLED_SUBMODULES`? | System check |
|---|---|---|---|---|
| `images` | `Image` | `libvips` (system, apt `libvips-dev`) + `pyvips` (pip, extra `images`) | Yes — on by default | `stapel_cdn.images.E001` if `"images"` is enabled and `pyvips` isn't importable: libvips is the **only** image decoder the library has (Pillow left in 0.10), so nothing on the image path works without it. `stapel_cdn.images.E004` if libvips is present but this build cannot read a format `ALLOWED_IMAGE_EXTENSIONS` declares allowed — the setting advertising what the deployment cannot honour. |
| `recordings` | `Audio` | none for storage (always on); `ffmpeg`/`ffprobe` (system) for duration + waveform, and later for compression | Yes — gates the metadata/compression checks only, storage is unconditional | `stapel_cdn.recordings.E003` if `"recordings"` is enabled and `ffmpeg` is missing (a voice message then has neither duration nor waveform) |
| `video` | `Video` | `ffmpeg`/`ffprobe` (system) — VPS/prod-only, never the stapel-studio devcontainer | Yes | `stapel_cdn.video.E002` if `"video"` is enabled and `ffmpeg` is missing (no dimensions, no duration, no poster) |
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

Every view in `stapel_cdn.views` mixes in `SerializerSeamMixin` — imported from
`stapel_core.django.api.views` since 0.16.0 (core 0.41.0 hoisted it into the
canon; this module carried a byte-identical local copy until then) — with two
class attributes
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
| `cdn.describe` | provides (function) | `call("cdn.describe", {"ref": "<type>/<hash>"})` → the render-metadata snapshot (table above). `LookupError` (surfaced as `FunctionCallError`) for an unknown ref — a missing asset is the caller's placeholder case, not an empty snapshot. |
| `cdn.describe_many` | provides (function) | `call("cdn.describe_many", {"refs": [...]})` → `{"items": {ref: snapshot}, "missing": [ref, ...]}`. One query per model for a whole page of attachments; an unknown ref is data, not an error. Max 50 refs per call. |
| `cdn.refs_sync` | provides (function) | `call("cdn.refs_sync", {"service", "entity_type", "entity_id", "old_hashes", "new_hashes"})` → `{"added", "removed", "errors"}`. Same logic as `RefSyncView` / `services.apply_ref_sync`. |
| `gdpr.erasure.requested` | subscribes (action) | Subject-scoped erasure — `account` \| `workspace` \| `file` \| `recording` (see **Erasure** below), confirmed with `gdpr.section.erased` carrying `{owner: "media", subject_type, subject_key, receipt_id, counts}` in the same transaction. Schema: `schemas/consumes/gdpr.erasure.requested.json`. Idempotent. |
| `gdpr.owner.probe` | subscribes (action) | Answered with `gdpr.owner.alive {owner: "media", subject_types}` from the *same* subscriber that erases. Schema: `schemas/consumes/gdpr.owner.probe.json`. |
| `gdpr.section.erased` / `gdpr.owner.alive` | emits (action) | The receipt and the probe answer above. Schemas: `schemas/emits/`. |
| `user.deleted` | subscribes (action) | The pre-0.5.0 account path, now routed through `erasure.erase("account", …)`; when the payload carries a `correlation_id` it confirms with `gdpr.section.erased` in its 0.4.x shape (`service: "media"`) so a host on the older orchestrator still completes. Deprecated in stapel-gdpr 0.5.0, removed there in 0.6.0. Schema: `schemas/consumes/user.deleted.json`. Idempotent. |
| `user.deletion_initiated` | subscribes (action) | Grace period started: purges the user's *unreferenced* media (`refs == []`) via `CDNGDPRProvider.purge_unreferenced()`; referenced media keeps serving (and its ownership link) until `user.deleted` — grace is cancellable. Schema: `schemas/consumes/user.deletion_initiated.json`. Idempotent. |
| `user.merged` | subscribes (action) | The opposite instruction to `user.deleted`: a guest account was folded into an existing one, so `uploaded_by` is re-pointed on `Image`, `Video`, `File` and `Audio` — nothing is erased. Dedup is owner-scoped, so a guest object whose bytes the survivor already holds is folded into the survivor's row (refs unioned, duplicate row dropped, blob never unlinked). A survivor with no user row here yet raises `MergeTargetNotReady` so the outbox redelivers rather than marking the uploads delivered-and-lost. Schema: `schemas/consumes/user.merged.json`. Idempotent. |
| `cdn.ref.sync` | consumes (bus) | `manage.py consume_cdn_events` (Kafka topic `stapel.cdn.ref-sync`); the producer-side helper `sync_cdn_refs()` lives in `stapel_core.django.cdn.ref_sync`, so other modules publish without importing this package. |

Registration happens in `CdnConfig.ready()`; transport (in-process vs bus) is chosen by
`STAPEL_COMM` in stapel-core — the same handlers serve monolith and microservices.

### Erasure

This module is the **`media`** data owner in stapel-gdpr's erasure protocol
(deletion-lifecycle §1.3/§2). One subscriber (`actions.py`) handles both
`gdpr.erasure.requested` and `gdpr.owner.probe`; the erasing itself lives in
`erasure.py` (`erase(subject_type, subject_key) -> counts`), callable
in-process too. Declare it exactly as it claims itself
(`stapel_cdn.erasure.OWNER` / `SUBJECT_TYPES`):

```python
STAPEL_GDPR = {"DATA_OWNERS": {"media": ["account", "workspace", "file", "recording"]}}
```

**How a subject is located.** This module holds no foreign keys to anything:
an object is named by its media ref `<prefix>/<hash>` (the string
`cdn.import_from_url` returns and `cdn.media_exists` takes), and the entities
using it are recorded on the row's `refs` list as
`<service>/<entity_type>/<entity_id>` — the format `cdn.refs_sync` /
`services.apply_ref_sync` write. **That list is the reverse index**, so an
entity subject needs nothing extra in the request: `subject_key` is the
entity id, `subject_type` is the entity type, and every service's reference
to it matches.

| Subject | `subject_key` | What is erased | Counts |
|---|---|---|---|
| `file` | a media ref, `<prefix>/<hash>` | **the bytes**: every row over that content (identical bytes held by two principals are two rows over one blob) and the blob itself. References still attached are counted as `refs_stranded` and logged — the host is expected to have released its own first; leaving the bytes because a stale reference exists would be the wrong failure | `objects_removed`, `blobs_unlinked`, `refs_stranded` |
| `recording` | the recording id | **the entity's reference**: dropped from every object carrying `*/recording/<id>`; an object left with no references at all is destroyed with its blob, an object another entity still uses keeps serving | `refs_removed`, `objects_removed`, `blobs_unlinked`, `objects_kept_referenced` |
| `workspace` | the workspace id | the same, for `*/workspace/<id>`. Media attached to *entities inside* the workspace is erased through those entities' own subjects — each owner library requests its own | same as `recording` |
| `account` | the user id | the policy of record (`CDNGDPRProvider.delete`): the user's unreferenced media is destroyed (rows + bytes), media other content still references keeps serving with `uploaded_by` nulled | `objects_removed`, `objects_anonymized` |

Rules the subscriber keeps:

- **Idempotent.** Delivery is at-least-once; a redelivery finds nothing left
  and receipts zeros. `receipt_id` is `media:<correlation_id>` — stable, so a
  redelivery does not invent a second erasure in the audit trail.
- **One transaction.** Erasure and receipt commit together (outbox canon).
- **Never certify what did not happen.** A blob that cannot be unlinked keeps
  its row and raises `MediaErasureIncomplete` (the row is the only record of
  where the file is); an unparseable or unknown media ref raises; a subject
  type this owner does not claim is ignored, because gdpr opens no part for
  it.
- **Co-location.** The probe is answered from this same module, which is what
  makes gdpr's `W006` evidence that the erasure path is *consumed* rather
  than that a container is deployed.

### Background work — queues, the beat entry, and `variants_status`

`tasks.py` sends two messages per upload (`generate_thumbnails`,
`generate_previews`). Through 0.14 both were pinned to literal queue names
(`thumbnails`, `previews`) inside the task decorator. That is the whole story of
a live incident: a deployment that shards work per service by setting
`CELERY_TASK_DEFAULT_QUEUE` ran **zero** consumers on those two names, and
because every `variant_<size>_url` is derived from `<type>/<hash>` rather than
from a file on disk, the upload still answered `201` with the full ladder — a
201 whose URLs 404 forever.

| Seam | What a host does |
|---|---|
| `STAPEL_CDN["THUMBNAILS_QUEUE"]` / `["PREVIEWS_QUEUE"]` | Leave unset for a single-queue worker (the tasks then carry no `queue` option at all). Set them to the names your workers consume (`-Q`) in a sharded fleet. Resolved per send, so `override_settings` works in tests. |
| `tasks.get_cdn_beat_schedule()` | `CELERY_BEAT_SCHEDULE = {**get_cdn_beat_schedule(), ...}` — schedules `retry_unprocessed` (re-queues images stuck at `is_processed=False`; cadence `RETRY_UNPROCESSED_SCHEDULE`) and `sweep_unclaimed` (reaps zero-ref media past `UNCLAIMED_TTL_HOURS`; cadence `SWEEP_UNCLAIMED_SCHEDULE`). Nothing schedules itself. |
| `checks.W008` (`stapel_cdn.tasks.W008`) | Warns when those settings name a queue that no Django-visible setting (`CELERY_TASK_QUEUES`, `CELERY_TASK_DEFAULT_QUEUE`, `CELERY_TASK_ROUTES`) mentions. It cannot read your compose file — proving a worker exists in the deploy is ADO-class and lives in stapel-tools (`stapel-adoption-lint`). |
| `checks.W013` (`stapel_cdn.tasks.W013`) | Warns when this process runs a `CELERY_BEAT_SCHEDULE` for other work and it carries no `stapel_cdn.tasks.sweep_unclaimed` entry — unclaimed media then accumulates forever behind a TTL setting that says otherwise. Silent when no beat schedule exists at all (a host may sweep from cron via `manage.py cdn_sweep_unclaimed`). |
| `services.sweep_unclaimed()` / `manage.py cdn_sweep_unclaimed [--dry-run]` | The unclaimed-media reaper: deletes bytes + rows for media with `refs == []` whose `unreferenced_since` is older than `UNCLAIMED_TTL_HOURS`. Upload starts unclaimed (stamp set); `cdn.refs_sync` claiming it clears the stamp; detaching the last ref restamps it, so the TTL always counts from *becoming* unreferenced. Deletion is `erasure._destroy` — the same shared-blob-safe, fail-closed machinery erasure and the GDPR purge run. |
| `Image.variants_status` / `variants_ready_at` | Read-only on `ImageSerializer`: `"pending"` until `generate_previews` succeeds, `"ready"` after. A consumer must gate on this before rendering a `variant_*_url`. `variants_status` is derived from `is_processed` (one fact, one owner); `variants_ready_at` is the stamped moment, null while pending. |

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
  check_submodule_binaries` (`stapel_cdn.images.E001`/`E004`) catches the missing-decoder case
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
- **Adding a media type by widening an enum.** `MEDIA_KINDS` is a
  merge-over-builtins registry: stickers, and whatever comes after them, are a
  dict literal in the host's settings. Do not add a `TextChoices` of kinds, and
  do not branch on `Image.type` string literals in a consumer — read `kind` off
  the snapshot.
- **Inlining an unbounded preview.** `preview_b64` is page weight multiplied by
  the number of attachments on a screen. Encode through
  `metadata.encode_preview`, which enforces `MICRO_PREVIEW_MAX_BYTES` by
  downgrading and then refusing. Never truncate a data URI, and never widen the
  budget to fit a bigger image — a bigger image belongs behind a URL.
- **Reporting an unmeasured fact as a measured one.** A duration nobody could
  measure is `null` with a `meta_reason`, never `0`; a video the pass could not
  probe is not `is_processed`; a poster URL is only emitted once
  `Video.has_poster` says the file exists. Every one of those was, or would be,
  the same defect as a 201 whose variant URLs 404 forever.
- **Non-idempotent action handlers.** Anything subscribed via `on_action` must tolerate
  redelivery (outbox retries, at-least-once broker semantics).
- **Reading flat `CDN_*` settings.** The legacy flat aliases are gone; code reads
  `cdn_settings.<KEY>` so the namespace dict, env vars and test overrides all work.
- **Expecting a public "read media by ref" HTTP endpoint.** `file/exists/`
  filters `uploaded_by=request.user` unconditionally (`views.FileExistsView.
  _exists_response`) — it answers "does *my* upload have these bytes?" for
  dedup-checking, not "describe this ref" for an arbitrary owner, and
  `refs/sync/` is `IsServiceRequest`, unreachable from a browser. This is
  deliberate, not a missing route: a consumer that needs another principal's
  media metadata resolves it **server-side**, at write time, via the comm
  functions (`cdn.describe` / `cdn.media_exists`) and denormalizes what it
  needs (`variants_meta`, `prefix`, dimensions) into its own API response —
  the same pattern `RefSyncView` already uses for reference bookkeeping. The
  original bytes/variants are still fetched directly off the public media
  route by URL (`Image.variant_urls` / `get_variant_url`), subject only to
  `PRIVATE_MEDIA_PREFIX`; nothing about that requires an authenticated
  lookup. Do not invent a `GET /media/<ref>/` metadata endpoint or widen
  `file/exists/`'s scope to work around this — route the resolution through
  the owning service's own API instead.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change is expressible as:
a `STAPEL_CDN` key from the table above; a view subclass swapping serializer seams plus a
URL re-route; a `media_processed` / `post_save` receiver; a `FILE_UPLOAD_HANDLERS` entry;
a comm call or a consumer of the events above; an additional GDPR provider registered in
your own app.

**Upstream contribution** (this repo, via `contrib_open` → review origin → PyPI release)
if it needs: a new settings key or a dotted-path `import_strings` seam (e.g. making the
storage backend or processing service class configurable); S3/presigned
uploads (`s3` extra is declared but unwired); the video **rendition ladder**
(`Video.variant_240`…`variant_2160` are still empty FileFields — metadata and the
poster frame ship, transcoding does not); ffmpeg-audio
compression for `Audio` (`AudioProcessingService.compress_audio`, still a documented
stub — `recordings` storage and the duration/waveform pass are already usable
without it); emitting `media_processed` from
the Celery task path; new endpoints, model fields, or migrations; changing WebP/JPEG
quality or upload-handler thresholds (currently hardcoded constants).

Litmus test: if you'd have to monkeypatch, copy a module file, or edit code inside
`stapel_cdn/` to get the behavior — it's upstream. If a setting, subclass, receiver, or
comm call gets you there — it's app-layer.
