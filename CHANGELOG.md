# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## 0.17.1 — 2026-08-26

**The shipped default failed our own boot check.** `ALLOWED_IMAGE_EXTENSIONS`
had carried `.bmp` since the setting existed, and libvips has **no native BMP
reader** — `bmpload` does not exist; BMP is read only through `magickload`,
from an ImageMagick module that the `pyvips[binary]` wheels do not carry. So a
stock `pip install stapel-cdn[images]` host that had configured *nothing* got
`stapel_cdn.images.E004` from `manage.py check` and could not boot until it
overrode the setting. E004 was right — a setting advertising a format the
deployment cannot decode is exactly the defect it was added for in 0.10. The
default was wrong: it advertised, on every host, a format the library's own
install instructions do not produce a decoder for.

- **`ALLOWED_IMAGE_EXTENSIONS` default is now what a stock install decodes**:
  `.jpg .jpeg .png .gif .webp .avif .heic .heif`. `.bmp` is out; `.avif` is in,
  because it comes through the same `heifload` that already backed the
  `.heic`/`.heif` the default advertised. Measured on `python:3.12-slim` +
  `pyvips[binary]` (libvips 8.18.6): `jpegload`, `pngload`, `gifload`,
  `webpload`, `heifload`, `tiffload`, `svgload` registered — `bmpload`,
  `magickload`, `jxlload`, `jp2kload` not.
- **E004 is unchanged and still probes `.bmp`.** Nothing was loosened: `.bmp`
  keeps its entry in `decoders.VIPS_LOADERS`, so a deployment that *widens*
  the list (BMP via ImageMagick, or `.tif`/`.svg`/`.jxl`/`.jp2`) is still told
  at boot whether its own libvips build can honour what it just declared,
  instead of an uploader being told their file is invalid. The check was doing
  its job; the value it was checking is what changed.
- **A test pins the property, not the incident**: the *shipped default* is run
  through the real `VIPS_LOADERS` table against a simulated stock
  `pyvips[binary]` operation registry, so a future extension added to the
  default without a decoder behind it fails here rather than on a user's first
  boot. Skip-safe where libvips is absent.
- Extension lists in the upload serializer's `help_text` and the admin form's
  `accept` attribute follow the default (`bmp` -> `avif`).

### Also

- **`stapel-tools` is declared in the `test` extra**, and CI runs
  `stapel-sibling-lint . --strict` with `STAPEL_TEST_STRICT_SIBLINGS=1`. The
  suite imports `stapel_tools` (the llms.txt and capabilities emitters are
  driven against this repo's own docs in `tests/test_contract.py` and
  `tests/test_capabilities_surface.py`) while `pyproject.toml` declared it
  nowhere — collection succeeded here only because a sibling checkout happened
  to be installed on the box. SIB001, fixed and now gated.

## 0.17.0 — 2026-08-24

**Describe was unreachable from a browser.** 0.16.0 shipped the whole
render-metadata contract as `cdn.describe` / `cdn.describe_many` — comm
Functions, dispatched over the bus. A browser has no bus. So a client could
see `render_meta` only for something it had *just uploaded itself* (inline in
the upload response), or for whatever a consuming module chose to denormalize
into its own serializer. A chat bubble holding somebody else's
`<prefix>/<hash>` had nothing to draw, and an attachment renderer was not
expressible on the front end at all — which the cdn-react audit named as the
structural reason `chat-react` ships none. This release is the missing
transport, and nothing else: the contract itself is unchanged.

### Added — `POST /cdn/api/v1/describe/`

```
POST /cdn/api/v1/describe/   {"refs": ["avatar/<hash>", "video/<hash>", ...]}
200                          {"items": {"<ref>": {…snapshot…}}, "missing": ["<ref>"]}
```

The same snapshot, the same ceiling, the same posture as the comm Function,
because it is literally the same function: the batch body moved into
`services.describe_refs`, and `cdn.describe_many` and this endpoint are two
transports over it. The OpenAPI response schema is built from the same
`render_meta` emitter the upload serializers use (`serializers.
render_meta_schema`), so the two cannot describe different objects either.

- **50 refs** per call (`metadata.DESCRIBE_MANY_LIMIT`, moved there from
  `functions.py` so the endpoint and the serializer can read it without
  importing the comm module). Duplicates collapse **before** the ceiling.
- **Unknown refs are data**: deleted, never stored, or malformed — all of them
  come back in `missing` with a `200`. A page with one dead attachment still
  renders the other thirty-nine. A malformed ref is not a `400` either; one
  bad entry must not cost the caller its other forty-nine snapshots.
- Over the ceiling: **`error.400.too_many_refs`** (new key) with `count` and
  `max` in the params — the fix is mechanical, page the batch.
- Over the rate: `error.429.too_many_requests` with `retry_after` **and** a
  `Retry-After` header. DRF's bare English `detail` was the one refusal shape
  this module did not otherwise emit; every refusal here is a registered,
  localizable key.

### Added — the guard and the rate are settings, not literals

- **`DESCRIBE_PERMISSIONS`** — dotted paths, resolved at request time,
  defaulting to `stapel_cdn.permissions.IsAuthenticatedOrService`: the seam
  `FileExistsView` already uses (signed in, guest sessions included, or an
  internal service call), named as a single importable class because a DRF
  permission *list* means "all must pass" and cannot spell an OR. Pinning
  `permission_classes` on a subclass still wins — the setting is the default,
  not a ceiling.
- **`DESCRIBE_THROTTLE`** (`60/min`) and **`DESCRIBE_ANON_THROTTLE`**
  (`10/min`, dormant under the default guard, the only brake once it is
  opened). Batch size is response size here, so the rate bounds bytes.

**Why that guard is wide enough to answer for refs the caller did not
upload** — which is the whole case the endpoint exists for: a ref is
`<prefix>/<sha256>`, so naming one is already evidence the caller was given
it, and the snapshot is geometry, duration and a ≤4 KB inline preview. It
carries **no uploader identity, no filename, no `refs[]`** — that is the
difference from `FileExistsView`, which returns the entire row and therefore
stays scoped to `uploaded_by`. A deployment that will not accept even that
sets `DESCRIBE_PERMISSIONS` to `IsServiceRequest` and keeps describe
service-side; one with public media opens it to `AllowAny`.

### Added — boot checks for the new seam (`checks.E005`, `checks.W012`)

Both settings are read per request, which is what lets a host swap them
without subclassing a view and also what would turn an authoring mistake into
a 500 on every attachment a page tries to draw. So, exactly as `W009` does for
the media-kind registry, they are checked at boot:

- **`E005`** — a `DESCRIBE_PERMISSIONS` entry that does not import, or a rate
  DRF cannot parse. Either one means this deployment cannot serve `/describe/`
  at all, which is an operator's problem and must be heard at boot.
- **`W012`** — an **empty** `DESCRIBE_PERMISSIONS`. DRF reads no permission
  classes as "everyone passes", so a blank list publishes the endpoint to
  anonymous callers. `AllowAny` says the same thing on purpose and is silent;
  a blank does not.

### Contract

`docs/schema.json` grows one operation (`describe_media`, 9 paths / 10
operations) and `docs/errors.json` one key (54). Nothing existing changed:
the `render_meta` schema is byte-identical after the emitter refactor.

**For the react pair:** `@stapel/cdn-react` can now resolve `render_meta` for
any ref it holds, which unblocks the attachment renderer in `chat-react` and
retires the "stapel-cdn generates no inline blur placeholder" claim in
`src/model/refs.ts`. Regenerate against 0.17 and bump the pinned contract.

## 0.16.0 — 2026-08-24

**An attachment a UI can draw without asking twice.** Reviewing a live
product, the owner named the gap precisely: every media attachment must carry
what a renderer needs *in the payload it already has* — the aspect box to
reserve, the byte size, something to show while the real bytes load, and, for
time-based media, how long it runs. Anything missing became either a request
made mid-scroll or a box that resized under the reader's thumb. This release
is that metadata, generated at ingest, stored, and served through one contract
(SERVICE-BACKLOG §35 item 9а).

### Added — the render-metadata contract

`cdn.describe` grew from a five-field sketch into the full snapshot, and
`cdn.describe_many` batches a whole page of attachments into one call:

```
{ref, kind, mime, ext, bytes, width, height, aspect, square, animated,
 duration_ms, preview_b64, preview_kind, poster_url,
 meta_status, meta_reason, variants[]}
```

The same object is returned by `cdn.describe`, `cdn.describe_many`, the new
`render_meta` field on the image/video/file serializers, and
`stapel_cdn.metadata.build_render_metadata()` in-process — one shape, so an
HTTP client and a service caller cannot drift apart. Existing keys are
unchanged; everything here is additive.

Per media type:

- **images** — `width`/`height`/`aspect`/`bytes` plus `preview_b64`: a 16px
  WebP blur-up (LQIP), produced in the **same libvips pass** that writes
  `16.webp`. The thumbnail ladder now encodes into memory once and writes
  those same bytes to disk, so the placeholder costs no second decode and no
  re-read of the file just written;
- **GIFs** — the same, plus `animated: true`, decided by the kind registry;
- **video** — `VideoProcessingService.process_video` is no longer a stub: one
  `ffprobe` call for dimensions and duration, one `ffmpeg` frame extraction
  that feeds both `MEDIA_ROOT/video/<hash>/poster.webp` and the inline micro
  poster. (The rendition ladder — `variant_240`…`variant_2160` — is still not
  implemented, and the module still says so rather than implying otherwise.);
- **voice messages** — `AudioProcessingService.extract_metadata`: duration
  from `ffprobe` and a waveform strip rendered by `ffmpeg`'s `showwavespic`,
  inlined as base64. Queued from `post_save` like the image pipeline, never
  run on a request thread;
- **documents** — `mime` and `ext`, with `preview_kind: null` so a client
  knows nothing is pending rather than guessing.

### Added — media kinds are an open registry, not an enum

`STAPEL_CDN["MEDIA_KINDS"]` (`stapel_cdn.kinds`), merged over the builtins
`image`/`gif`/`video`/`audio`/`file` with the house semantics: an entry
replaces a builtin, `None` removes one. A kind decides what `preview_b64`
holds (`blur`/`poster`/`waveform`/none) and whether the object moves —
stickers, and whatever comes after them, are a dict literal in a host's
settings rather than a release here. Resolution is by model, narrowed by
extension and (for images) `Image.type`, most specific first with ties broken
by name so the answer never depends on dict ordering. A malformed entry is
`checks.W009`, not a 500 on somebody's attachment.

### Added — a byte budget that downgrades, then refuses

`STAPEL_CDN["MICRO_PREVIEW_MAX_BYTES"]`, default **4096 bytes**, measured on
the finished `data:` URI — base64 expansion included, i.e. exactly what lands
in a JSON payload. A 16px WebP LQIP is ~300-800 B and a 240x40 waveform strip
~1.5-3 KB, so the default is roughly 5x headroom and bounds a 40-attachment
chat page at ~160 KB.

Enforcement is **downgrade-then-refuse**, never truncation: re-encode down the
WebP quality ladder (85 → 60 → 40), re-render the waveform at the smaller
strip size, and if it still does not fit — no preview at all, with
`meta_reason: "preview_over_budget"`. A truncated base64 string is a broken
image in every consumer; a `null` with a named reason is a placeholder every
consumer already draws. The ceiling is applied on **read** as well as at
ingest, so lowering the setting stops shipping older, larger payloads
immediately instead of after a backfill. `cdn.describe_many` is capped at 50
refs per call, because batch size is response size.

### Added — degradation always has a name

Every gap in the snapshot is accompanied by `meta_status`
(`ok`/`partial`/`missing`) and `meta_reason`, one of `not_generated`,
`decoder_missing`, `preview_over_budget`, `source_missing`, `encode_failed`,
`ffprobe_missing`, `ffmpeg_missing`, `probe_failed`, `render_failed`,
`tool_timeout`. There is no path that returns an unexplained null, and no path
that fabricates a measurement: an unmeasured duration is `null`, never `0`.
All ffprobe/ffmpeg use goes through one seam (`stapel_cdn.probes`) with a
timeout on every call (`MEDIA_TOOL_TIMEOUT`), fixed argv and never a shell.

### Added — `manage.py cdn_backfill_media_meta`

The pass over everything stored before this release. Idempotent and resumable
by construction — the candidate query *is* the resume token (only rows still
missing a preview, paged by a primary-key cursor), so a crash halfway is
resumed by re-running the same command and a second run over a finished table
writes nothing. `--limit` drains a large table in bounded slices; `--dry-run`
counts; a row that cannot be completed records its named reason and is counted
with a per-reason tally rather than retried in a loop or reported as success.
`--retry-degraded` re-attempts those rows — the normal sequence after
installing ffmpeg on a deployment that stored a month of voice messages
without it. `retry_unprocessed` now covers videos and recordings too, for the
same reason.

### Added — boot checks for a missing media tool

- **`checks.W009`** (`stapel_cdn.kinds.W009`) — a `MEDIA_KINDS` entry that
  does not parse;
- **`checks.W010`** (`stapel_cdn.media.W010`) — ffmpeg without ffprobe, or
  ffprobe without ffmpeg. E002/E003 probe for the pair; a split install passes
  them and then produces attachments with a waveform and no duration;
- **`checks.W011`** (`stapel_cdn.media.W011`) — an inline-preview budget that
  is not a positive number, so the shipped default is what actually bounds the
  payload;
- **E002/E003** now say what is actually lost when ffmpeg is absent (no
  aspect, no duration, no poster; no waveform) and point at
  `--retry-degraded`.

### Changed

- **`Video.is_processed` now means measured facts exist**, not "the pass was
  called". Through 0.15 `process_video` set it unconditionally while doing no
  work — the same class of untruth as a 201 whose URLs 404. A video the pass
  could not probe stays `is_processed=False` with a named `meta_reason`, so
  the backfill and `retry_unprocessed` can pick it up once the deployment has
  the binary.
- **`Video.poster_url` is `None` until the poster file exists**
  (`Video.has_poster`) — never a URL derived from the hash alone.
- **`aspect` is rounded to 6dp**, so the same image always serializes to the
  same JSON number.
- **`SerializerSeamMixin` is imported from `stapel_core.django.api.views`**
  (core 0.41.0 hoisted it into the canon); the byte-identical local copy is
  gone. Minimum `stapel-core` is now **0.41.0**.
- **`build_render_metadata` moved to `stapel_cdn.metadata`** and is still
  re-exported from `stapel_cdn.services` — every existing import keeps
  working.

### Migration

`0007_media_render_metadata` — pure expand: `preview_b64` + `meta_reason` on
`Image`/`Video`/`Audio`, `has_poster` on `Video`, all with defaults. Existing
rows read as "never generated", which is exactly the candidate set the
backfill walks, so no marker column and no data migration were needed.

## 0.15.0 — 2026-08-24

**A 201 whose URLs 404 forever.** Two defects, one live incident: a fleet's
image pipeline was dead and nothing — not a log line, not a status field, not
a boot check — said so. The upload endpoint answered `201 Created` with the
complete ladder of `variant_*_url`s, because those URLs are computed from
`<type>/<hash>` and exist as strings the instant the row does. The worker that
was supposed to write the files behind them was never listening.

### Fixed — the variant queues were literals nobody could see

`tasks.py` decorated its two generation tasks `@shared_task(queue="thumbnails")`
and `@shared_task(queue="previews")`. Those strings were the entire routing
policy of this library, unreachable from any setting. A deployment that shards
work per service by setting `CELERY_TASK_DEFAULT_QUEUE` — the ordinary way to
shard, one worker per queue — had **zero consumers** on either name. Messages
were published and never delivered; the images stayed unprocessed forever.

- **`STAPEL_CDN["THUMBNAILS_QUEUE"]` / `["PREVIEWS_QUEUE"]`** (default `None`).
  `None` is not "queue named None": the send carries **no** `queue` option at
  all, so the task lands on the app's own default queue and a vanilla
  `celery -A app worker` drains it with zero configuration. A fleet that shards
  sets the names its workers consume. Resolved per send, not at import — a
  value frozen at decoration time could neither see a host's final settings nor
  be overridden in a test.
- Sends now go through `apply_async` (`.delay` cannot carry a queue option).
  The registered task names are unchanged and pinned explicitly
  (`THUMBNAILS_TASK_NAME`, `PREVIEWS_TASK_NAME`, `RETRY_TASK_NAME`), so a
  deployment's existing `task_routes` keeps matching.
- **`checks.W008`** (`stapel_cdn.tasks.W008`) warns when those settings name a
  queue that nothing Django-visible corroborates — `CELERY_TASK_QUEUES`,
  `CELERY_TASK_DEFAULT_QUEUE`, `CELERY_TASK_ROUTES`. It is deliberately narrow
  and says so: a library cannot read a compose file, a systemd unit or a
  worker's `-Q`, so it reports what it can see and names where the other half
  lives — ADO-class verification over the deployment, in stapel-tools
  (`stapel-adoption-lint`), the same family as ADO005. Silent in the default
  single-queue posture.
- **`tasks.get_cdn_beat_schedule()`** — the beat entry for `retry_unprocessed`,
  the safety net that re-queues images stuck at `is_processed=False`. It existed
  as a periodic task with nothing to schedule it; the cadence is now
  configuration (`STAPEL_CDN["RETRY_UNPROCESSED_SCHEDULE"]`, default every 5
  minutes) and a host wires one dict into `CELERY_BEAT_SCHEDULE`.

### Added — `variants_status`, so a response stops claiming what it cannot know

The other half of the same incident: nothing in the 201 distinguished "these
variants are being generated" from "these variants will never exist". Both
states serialized identically, down to the byte.

- **`variants_status`** (`"pending"` | `"ready"`) and **`variants_ready_at`**
  (nullable) on `ImageSerializer` — so they appear on the upload response, on
  the `already exists` 200, and on every later read of the media ref
  (`/file-exists/` serializes through the same DTO). `variants_status` is
  derived from `is_processed` rather than stored beside it: two columns for one
  fact drift, and the fact already had an owner.
- `tasks.generate_previews` stamps `variants_ready_at` in the same save that
  flips `is_processed`. A failed generation leaves both alone — the row stays
  `pending` rather than advertising a ladder that is not there.
- New migration `0006_image_variants_ready_at` (additive, nullable, indexed).
- The endpoint description now tells a consumer to gate on it before rendering
  a variant URL, and the schema carries the enum. Frontend pairs read it from
  the contract; nothing in stapel-react changed in this release.

**Widening a shipped DTO is a minor bump under pre-1.0 semver**, hence 0.15.0
rather than 0.14.1.

**Hosts must:** nothing, if they run a single default-queue worker — that case
now works where it silently did not. A fleet that shards queues sets
`STAPEL_CDN["THUMBNAILS_QUEUE"]` / `["PREVIEWS_QUEUE"]` to the names its workers
consume, and every fleet should wire
`**stapel_cdn.tasks.get_cdn_beat_schedule()` into `CELERY_BEAT_SCHEDULE`.

## 0.14.0 — 2026-08-23

**Erasure stops being account-only.** This module was the one data owner in
the fleet that already receipted an account erasure — and that was all it
could answer. A deleted recording, a deleted workspace or a single deleted
file left their objects in the bucket, because `user.deleted` is the only
subject that event can name. stapel-gdpr 0.5.0 keys erasure by a subject
(`{subject_type, subject_key}`) and names owners that stop answering
(`gdpr.W006`), so this release answers for all four subjects the spec
assigns to `media` and proves it is listening.

### Added — subject-scoped erasure (`account`, `workspace`, `file`, `recording`)

- New `erasure.py`: `erase(subject_type, subject_key) -> counts`.
  - **`file`** — `subject_key` is a media ref `<prefix>/<hash>` (what
    `cdn.import_from_url` returns and `cdn.media_exists` takes). A ref names
    *content*, so every row over those bytes is deleted and the blob with
    them; references still attached are counted (`refs_stranded`) and logged
    rather than treated as a reason to keep the bytes.
  - **`recording`** / **`workspace`** — `subject_key` is the entity id, and
    **no refs have to be passed in the request**: the row's own `refs` list
    (`<service>/<entity_type>/<entity_id>`, the format `cdn.refs_sync`
    writes) is the reverse index. The entity's reference is dropped wherever
    it appears, whichever service wrote it; an object left with no reference
    at all is destroyed with its blob, an object another entity still uses
    keeps serving — the refcount discipline account erasure already had.
  - **`account`** — the existing `CDNGDPRProvider.delete()` policy, now
    reached through the same entry point.
- New consumers in `actions.py`, both in one module — that co-location is
  what makes an `alive` answer evidence the erasure path is *consumed*
  rather than that a container is deployed: `gdpr.erasure.requested` → the
  erase above plus a `gdpr.section.erased` receipt `{owner: "media",
  subject_type, subject_key, receipt_id, counts}` emitted in the same
  transaction; `gdpr.owner.probe` → `gdpr.owner.alive {owner,
  subject_types}`.
- Contracts committed: `schemas/emits/{gdpr.section.erased,
  gdpr.owner.alive}.json` (this module had no `schemas/emits/` at all) and
  `schemas/consumes/{gdpr.erasure.requested,gdpr.owner.probe}.json`.
- `CDNGDPRProvider.delete()` returns `{objects_removed, objects_anonymized}`
  (it returned `None`) — the counts an `account` receipt carries. The base
  `GDPRProvider` ignores a return value, so the orchestrator's in-process
  path is unaffected.

Host wiring (one line, no new setting here):

```python
STAPEL_GDPR = {"DATA_OWNERS": {"media": ["account", "workspace", "file", "recording"]}}
```

**Minor, not patch**: new public module (`stapel_cdn.erasure`), two new
consumed actions, two new emitted ones, and a provider method that now
returns a value.

### Fixed — account erasure skipped `Audio` entirely

`purge_unreferenced()` and `delete()` looped over a hardcoded `(Image,
Video, File)`. `Audio` was added to this module after that loop was written,
so a user's audio recordings — rows *and* bytes — survived their own account
erasure, and the closure was certified anyway. Both now iterate
`erasure.media_models()`, the one place the media model set is named.

### Deprecated — `user.deleted`

Still consumed and still receipted in its 0.4.x shape (`{user_id,
correlation_id, service}`), so a host on the older orchestrator keeps
completing; the erasure itself is now routed through `erase("account", …)`.
stapel-gdpr emits it alongside `gdpr.erasure.requested` for one minor and
removes it in its 0.6.0; this handler goes with it. Two receipts for one
correlation_id are idempotent on the gdpr side — they land on the same part.

## 0.13.0 — 2026-08-22

**`POST /upload/image/` and `POST /images/product/upload/` now agree on
"product."** The generic endpoint stored `type="product"` unconditionally,
never checking it against `STAPEL_CDN["ASSET_TYPES"]` the way
`TypedImageUploadView` already does for its caller-chosen type. On the
shipped default (`ASSET_TYPES = ("avatar",)` only), that meant a POST to
`/upload/image/` created an `Image` row whose `type` wasn't a member of its
own model's choices — and wasn't a member of `docs/schema.json`'s
`TypeEnum` either, which is generated from the same setting — while a POST
of the identical string to `/images/product/upload/` correctly 400'd. One
endpoint silently accepted what the other, correctly, refused.

### Fixed

- `ImageUploadView.post` now validates `"product"` against
  `STAPEL_CDN["ASSET_TYPES"]` before touching the upload, the same guard
  `TypedImageUploadView` runs for its `image_type` path parameter —
  `error.400.invalid_image_type` on a deployment that never added
  `"product"`. This package's own default library, config-driven `ASSET_TYPES`
  (conf.py) was always the intended single source of truth for what
  `Image.type` may hold (MODULE.md: "`TypedImageUploadView` and
  `RandomImageView` validate against it"); the generic endpoint was the one
  path that didn't. The enum wasn't stale — the view was.
- `413` was returned by the three image upload endpoints
  (`ImageUploadView`, `AvatarUploadView`, `TypedImageUploadView`) whenever
  `STAPEL_CDN["MAX_IMAGE_SIZE"]` was exceeded, but only `VideoUploadView`
  declared it in `responses`. All three now declare `413` alongside `400`/
  `401`/`500`, matching what they already return.

### Docs

- MODULE.md anti-patterns: documented, explicitly, that `file/exists/`'s
  owner-scoping (`uploaded_by=request.user`, unconditional) and
  `refs/sync/`'s `IsServiceRequest` are deliberate — there is no public
  "read media by ref" HTTP surface, by design. A consumer that needs
  another principal's media metadata resolves it server-side via
  `cdn.describe`/`cdn.media_exists` and denormalizes into its own response
  (the pattern `@stapel/cdn-react` already had to work around by treating
  refs as opaque strings — the storefront spec §13.6 item 10). Raw
  bytes/variants stay reachable over the public media route by URL
  regardless, subject only to `PRIVATE_MEDIA_PREFIX`.

**Upgrade note.** A deployment that has been relying on `/upload/image/`
silently storing `type="product"` **without** `"product"` in
`STAPEL_CDN["ASSET_TYPES"]` now gets `400 error.400.invalid_image_type`
from that endpoint instead. Add `"product"` (or whatever value that
deployment uses) to `ASSET_TYPES` to keep the endpoint working — the
existing rows are unaffected, and any deployment that already configured
`ASSET_TYPES` to include it (as this package's own test suite always has)
sees no behavior change at all.

## 0.12.0 — 2026-08-22

**This module now emits its own contract triad.** `docs/schema.json`,
`docs/flows.json` and `docs/errors.json` did not exist before this release —
the Makefile said so out loud, and `tests/test_capabilities_contract.py`
called stapel-cdn "the one module with no schema/flows/errors emitter" —
which blocked the react codegen pipeline (`gen:api`/`gen:errors`/
`gen:manifest`) for any `-react` pair generated against this module
(the storefront spec §1.8, §3.10, A1).

### Added

- `_codegen.py` + `_codegen_settings.py` + `codegen_urls.py`: a
  single-module `{cdn + core}` Django harness that emits
  `docs/{schema,flows,errors}.json` at the canonical `/cdn/api/v1` prefix,
  the same mechanism stapel-search/-chat/-forms already use. `make
  contract` / `make contract-check` now cover the triad in addition to the
  existing `surface`/`docs/llms.txt` gates. `docs/capabilities.json`'s
  `provides`/`axes`/`extension_points`/`requires` stay hand-authored — a
  full generator for that document is a separate, tracked project.
- `docs/schema.json` (8 paths — `error-keys/` stays undescribed on purpose,
  the stapel-translate collector's internal listing, not a product route),
  `docs/flows.json` (`[]` — no `@flow` is declared yet, same state as every
  other contract-complete module today), `docs/errors.json` (53 keys: 11
  owned by this module, the rest inherited from stapel-core).
- `tests/test_contract.py`: every mounted route is described in
  `docs/schema.json`; every `CDN_ERRORS` code is declared with the
  correct `owner`.

### Changed

- **Dependency floor:** `stapel-core>=0.24.0` → `>=0.26.0`. Emission needs
  `generate_error_keys`'s `owner` field on every entry (0.26.0+); on 0.24.0
  the key was missing outright rather than carrying `owner: null`, and the
  new drift gate requires it.
- Three fields that used to fall back to an untyped `JSONField` now declare
  their real shape: `Image.variants_meta` is `array[{tier, branch, url,
  width, height}]` (one fixed shape, not a polymorphic union — models.py
  already documented it precisely); `FileExistsResponse.file` is a `oneOf`
  of `Image`/`Video`/`FileModel` (the concrete type is a sibling `type`
  field in the same envelope, so it can't carry an OpenAPI
  `discriminator`, but `oneOf` alone is still real typing); `FileModel.refs`
  is `array[string]` (opaque `service/entity_type/entity_id` ref keys).
  Response bytes are unchanged; only the declared OpenAPI type is.

## 0.11.0 — 2026-08-14

### Security — the video intake gets the bounds every other intake already had

`POST /cdn/api/v1/upload/video/` had **no size cap at all**, and no setting to
give it one. The whole request body was read and SHA-256'd before anything was
checked; the extension allowlist ran after that, and the per-owner byte quota —
which a deployment may switch off — was the only ceiling underneath. Meanwhile
the endpoint's own OpenAPI description told callers **"Maximum file size:
100MB"**: documentation asserting a limit nothing enforced.

It was also the one intake path that never looked at the bytes. The image path
decodes, the generic path sniffs; a `.mp4` whose leading bytes are HTML or a
script was stored under the media root and served from the media origin, where
a browser runs what it is handed regardless of the name.

- **New `STAPEL_CDN["MAX_VIDEO_SIZE"]`, default `100 * 1024 * 1024`** — the
  number the endpoint has been claiming all along. Enforced *before* hashing;
  over it the answer is `413`.
- The video path now runs `sniff_is_active_content()` like the generic path,
  and its extension allowlist moved ahead of the hash.
- A size cap no longer skips a file that cannot state its size. The old form
  (`if uploaded_file.size and uploaded_file.size > cap`) treated a `size` of
  `None` as "small enough"; an unknown size is now refused on both the image
  and video paths. `size == 0` is still not over any ceiling.

**Upgrade note.** A deployment that accepts videos larger than 100MB now gets
`413` for them. Raise `STAPEL_CDN["MAX_VIDEO_SIZE"]` to whatever that
deployment actually intends to store — the point of the change is that the
number is now stated somewhere and enforced, not that 100MB is right for
everyone.

The `**Maximum file size:** 100MB` line in the image/avatar/typed-image
endpoint descriptions was wrong in the other direction (`MAX_IMAGE_SIZE` is
20MB) and now names the setting instead of a stale literal.

### Security — the generic intake's MIME allowlist is no longer opt-out-able

Two halves of the same hole, both in `GenericFileUploadView`:

```python
if content_type and content_type not in set(ALLOWED_FILE_MIME_TYPES):
```

The allowlist ran **only when the caller volunteered a Content-Type**. Omit
the part header and the check was skipped entirely — a gate any client could
decline by saying nothing. And the shipped list ended with
`application/octet-stream`, the universal "some bytes" type any client may
declare for anything, which reduced the gate to a no-op by construction: every
payload the list excludes passes it by naming that.

- **An upload that declares no Content-Type is now refused.** Absent is not
  allowed.
- **`application/octet-stream` is out of the default
  `STAPEL_CDN["ALLOWED_FILE_MIME_TYPES"]`.**

**Upgrade note.** Clients that upload documents without a Content-Type, or
that label everything `application/octet-stream`, now get `400`. The fix in
almost every case is the client sending the real type. A deployment that
genuinely intakes opaque binaries opts back in explicitly:

```python
STAPEL_CDN = {
    "ALLOWED_FILE_MIME_TYPES": (*DEFAULTS["ALLOWED_FILE_MIME_TYPES"],
                                "application/octet-stream"),
}
```

Note what that does *not* buy back: a declared type is still not evidence
about the bytes, and `sniff_is_active_content()` refuses executable content
under any declared type.

`CONFIG.MD` also gains the rows 0.10's ownership/generic-intake work never
added: `DEDUP_SCOPE`, `MAX_OBJECTS_PER_OWNER`, `MAX_BYTES_PER_OWNER`,
`MAX_FILE_SIZE`, `ALLOWED_FILE_EXTENSIONS`, `ALLOWED_FILE_MIME_TYPES`,
`PRIVATE_MEDIA_PREFIX`.

### Security — removing a per-owner ceiling is now something you have to say

`quota_exceeded()` read its ceilings as `int(cdn_settings.X or 0)` and treated
0 as "unbounded", so `0`, `None`, `""` and a missing key **all** meant "no
ceiling" — three of the four by accident rather than by intent. An empty
environment variable, or a refactor that drops a key, silently removed the
storage ceiling of a module whose identities cost one POST to mint. (A
non-numeric value was worse still: `int("lots")` raised `ValueError` out of
the upload path, i.e. a 500 on every upload.)

It also exempted the one principal it could not measure: `if
owner_id(principal) is None: return None`. No owner means no usage to count
against, which is exactly why that caller needs refusing rather than waving
through — its effective ceiling was infinite.

- **`STAPEL_CDN["MAX_OBJECTS_PER_OWNER"]` / `["MAX_BYTES_PER_OWNER"]` accept
  the string `"unlimited"` to remove a ceiling.** That is the only thing that
  removes one.
- Any other unusable value (`0`, `None`, `""`, a word) **falls back to the
  shipped default** — 1000 objects / 2 GiB — instead of to "no ceiling", and
  **`checks.W007`** names it at boot.
- A principal with no primary key is refused with
  `error.403.storage_quota_exceeded` and `params.limit == "owner"`.

**Upgrade note.** A deployment that switched its quotas off with `0` is now
back on the shipped default ceilings and will start refusing uploads from
owners past them. Set the value to `"unlimited"` to restore the previous
behaviour deliberately:

```python
STAPEL_CDN = {
    "MAX_OBJECTS_PER_OWNER": "unlimited",
    "MAX_BYTES_PER_OWNER": "unlimited",
}
```

### Security — a failed erasure is no longer reported as an erasure

`purge_unreferenced()` wrapped the only step that removes the actual bytes in
`except Exception: pass`, then deleted the row anyway and counted the object
as removed:

```python
try:
    obj.original.delete(save=False)
except Exception:
    pass          # <- the bytes stayed; the row went
obj.delete()
removed += 1
```

`CDNGDPRProvider.delete()` then returned normally, which stapel-gdpr's
orchestrator treats as a receipt and which lets a closure flip to `DELETED`.
A fail-open in the one path whose whole contract is provable erasure — and an
unrecoverable one, because the row is the only record of where the file is, so
a failed erasure became personal data nobody can ever locate again.

- A blob that cannot be unlinked **keeps its row** (and its `uploaded_by`, so
  it stays attributable) and is logged at ERROR.
- `purge_unreferenced()` / `delete()` raise the new
  **`stapel_cdn.gdpr.MediaErasureIncomplete`** instead of returning. In
  `handle_user_deleted` that rolls the `gdpr.section.erased` confirmation back
  with it, so the closure stays `DELETING` and at-least-once delivery retries.
  Erasure is idempotent, and an already-missing blob still counts as erased.
- `delete()` raises *before* the anonymisation pass, which would otherwise
  strip `uploaded_by` off objects whose bytes are still on disk.

**Upgrade note.** A deployment with a media root the app cannot write to (a
read-only mount, an S3 policy without `DeleteObject`) now sees account
deletions fail loudly and retry instead of completing. That is the point: they
were completing over data that was never erased. **No opt-out is offered** —
a switch restoring the old behaviour would be a switch for reporting erasures
that did not happen.

### Security — a deployment with no decoder stops storing unverified images

With no libvips installed, `decoders.decode_dimensions()` returns `None` and
`validate_image_file()` degraded to a magic-byte signature check. So
`MAX_IMAGE_PIXELS` — the decompression-bomb cap — was **never reached**, and
nothing confirmed the bytes decode as the image they claim to be. The
degradation was deliberate and documented, and `checks.E001` is red about it,
which is the honest posture; what it was not is a decision anybody made per
deployment.

- **New `STAPEL_CDN["REQUIRE_DECODER"]`, default `True`.** With no decoder,
  image uploads are refused with `error.503.image_decoder_unavailable` — the
  same answer as "this build cannot read that format", because from the
  uploader's side it is the same situation: their file is fine and this
  deployment cannot handle it.
- **The passthrough stays available as the explicit opt-out:**
  `STAPEL_CDN = {"REQUIRE_DECODER": False}` restores the signature-only gate.

**Upgrade note.** A deployment running without libvips (`checks.E001` already
failing) now answers `503` on image uploads instead of storing them. Install
libvips (`apt: libvips-dev` plus `pip install stapel-cdn[images]`), or drop
`"images"` from `ENABLED_SUBMODULES` if it is really file-only storage, or set
`REQUIRE_DECODER` to `False` to keep storing images that nothing verifies.

## 0.10.0 — 2026-08-10

### Changed (BREAKING) — one decoder on the image path; Pillow is gone (#233)

A HEIC avatar was refused on a live deployment with **400 "Invalid image
file"** — and the file was fine. `ALLOWED_IMAGE_EXTENSIONS` declared `.heic`,
the deployment's libvips read HEIC natively (`heifload`), and
`ImageProcessingService` would have processed it. The refusal came from the
*guard*, which decoded with Pillow, which reads HEIF only via the optional
`pillow_heif` package, which was not installed — and whose absence was
swallowed:

```python
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass          # <- the deployment's gap, silenced
```

Two decoders, two capability sets, maintained in two places. **The guard was
stricter than the system it guarded**, and it reported the divergence as the
uploader's fault. Adding `pillow_heif` would have fixed this one format and
left the mechanism — they had already drifted in both directions (Pillow read
`.avif`, which libvips builds without libheif do not; libvips reads `.bmp`
only through ImageMagick, which Pillow does natively).

So the fix is the topology, not the package. **libvips is now the only image
decoder in the library, and Pillow is no longer a dependency at all.** The
fast, format-complete decoder had been the optional extra while the slow,
format-incomplete one was mandatory *and* held the gate deciding what ever
reached the fast one. That is inverted, and it is the whole defect.

- **`Pillow>=9.0` removed from `[project] dependencies`.** Every call site
  moved to libvips: `validators.validate_image_file`, `fetch.detect_image_extension`,
  `forms.ImageAdminForm.clean_original`, and the `pillow_heif` registration in
  `apps.CdnConfig.ready` (deleted — libvips needs no registration).
  `stapel-core`'s `PilRenderMetadataProvider` is a separate zero-infrastructure
  read path and is untouched. Pillow remains a **test-only** extra
  (`stapel-cdn[test]`): fixtures are generated with a codec independent of the
  one under test.
- **New `stapel_cdn.decoders`** — the single answer to "what can this
  deployment decode", shared by the validator, the URL-import gate, the admin
  form, `Image.save()` and the boot checks.
- **`ImageAdminForm` HEIC branch deleted.** It skipped decoding entirely
  (Pillow could not read HEIF) and accepted *any* payload named `.heic` on
  trust, storing 1x1 placeholder dimensions. An arbitrary file reached storage
  through the admin as long as it was named right. Now HEIC is validated like
  every other format and reports its real size.
- **`MAX_IMAGE_PIXELS` now means exactly what it says.** Pillow only raised
  above *2x* the configured value, so the effective ceiling was quietly double
  what an operator set. A deployment relying on the old slack must double its
  configured number.
- **Formats follow the libvips build**, not Pillow's table: JPEG, PNG, GIF,
  WebP, TIFF and HEIC/HEIF/AVIF natively; BMP through ImageMagick.

### Added — the deployment's gap is now stated at boot and named at runtime

- **`checks.E001` is tied to `ENABLED_SUBMODULES`** and reworded: `"images"`
  enabled with no importable pyvips is an error naming the system package
  (`apt libvips-dev`), the pip extra (`stapel-cdn[images]`) and the third
  remedy (turn `images` off). Previously unconditional — a deployment running
  stapel-cdn as passthrough file storage was told to install a decoder it has
  no use for. **This is the behaviour change to check if you run cdn without
  images.**
- **`checks.E004`** — libvips is present but *this build* cannot read a format
  `ALLOWED_IMAGE_EXTENSIONS` declares allowed. Names the extension, the missing
  loader, and both remedies (install a libvips that reads it, or stop
  advertising it). Same family as CFG006: a setting the library offers that the
  deployment cannot honour, detectable statically instead of as a 503 on
  somebody's avatar.
- **`error.503.image_decoder_unavailable`** (remediation `contact_support`,
  params `{extension}`) — "this deployment cannot read that format" is an
  operator's problem and no longer wears the uploader's error. `400
  error.400.invalid_format` keeps meaning "your file is broken". Same split as
  stapel-workspaces' `error.503.profiles_not_configured` (env-address-class v2
  §2).

### Fixed — every uploaded image was stored with 1x1 dimensions

`Image.save()` read dimensions from `self.original.path` *before*
`super().save()` wrote the file to storage, so it opened a filename that did
not exist yet and fell into the "unreadable file" branch — for **every format**,
not just HEIC. Verified against a live deployment: the path does not exist at
that point. The §0.3 honest-logging split worked exactly as designed, logging a
per-upload `ERROR`; nobody was reading the logs, so the placeholder shipped
anyway. Dimensions are now read from the open file object the validator just
decoded. Deployments where `process_image` runs had this corrected
asynchronously and saw only the spurious error log; deployments where it does
not kept 1x1 permanently.

### Security

- `detect_image_extension` (the SSRF-hardened URL-import gate) verifies the
  magic-byte signature *and* forces a full libvips pixel pass, replacing
  Pillow's `verify()`. The decompression-bomb cap is applied from the header
  before any pixel is touched.

## 0.9.1 — 2026-08-02

### Added
- `docs/llms.txt` — the fifth contract artifact, an agent-sized slice of
  `docs/capabilities.json`, wired into `make contract` / `make contract-check`
  (badge-canon §3). `docs/capabilities.json` itself stays hand-authored here
  (only its `version` field bumped alongside the package).
- Badge canon in README, classifier 3.14, `migration-lint` enabled in CI.

### Fixed
- `docs/capabilities.json`, `docs/flows.json`, `docs/errors.json`,
  `docs/llms.txt` and `CONFIG.MD` now ship in the wheel via `package-data`
  (#184); previously repo-only, invisible to `--from-installed` tooling.

## 0.9.0 — 2026-07-30

### Changed (BREAKING for anonymous callers) — an upload endpoint stops being open file hosting (#168)

`stapel-core` 0.16 turns the `AUTH_ANONYMOUS` axis into a question this
module never answered, and for a *storage* module it is the sharpest version
of that question. A guest session is `is_authenticated`, so a bare
`IsAuthenticated` gate lets it through — and the anonymous axis removes the
only thing that made an upload endpoint self-limiting, namely an account. A
session costs one unauthenticated POST to mint, so "authenticated upload"
and "open file hosting" become the same sentence. All five upload views were
gated on exactly that bare `IsAuthenticated` (`stapel_core.adoption` W002
reported all five against a real deployment).

The rule they now state:

> **a guest may upload the one artifact it legitimately owns — its own
> avatar — and nothing else.**

**Closed** — `IsNotAnonymousUser`, so an anonymous session gets **403** where
it previously got 201:

- `POST /upload/image/` — general-purpose image intake, bound to nothing the
  caller owns.
- `POST /upload/video/` — the most expensive intake here, with transcoding
  still to come.
- `POST /upload/file/` — 50 MB of arbitrary bytes, no type restriction: the
  plainest open-file-hosting shape in the module.
- `POST /images/{image_type}/upload/` — caller-chosen type is the same
  general-purpose intake wearing a label; a guest that needs an avatar has
  the dedicated route, which is the bounded one.

**Left open, deliberately** — `POST /upload/avatar/`
(`stapel_anonymous_access = ANONYMOUS_ALLOWED`). It is the picture on the
guest's own profile, which `stapel-profiles` already lets a guest have, and
it is a **live surface in a real consumer** (meettoday's settings screen is
reachable from the header a guest sees, and its profile tab uploads here) —
closing it would have broken a working flow rather than an abuse vector. It
is bounded three ways: the image validator, `MAX_IMAGE_SIZE` /
`MAX_IMAGE_PIXELS`, and SHA-256 deduplication, so re-uploading the same
bytes costs no new storage at all.

Minor, not patch: for a deployment with `AUTH_ANONYMOUS` on this is a
behaviour change on a live surface. Deployments without guest sessions are
unaffected — an ordinary authenticated user passes `IsNotAnonymousUser`
exactly as before.

New `tests/test_guest_surface.py` pins both halves.

### Changed

- Minimum `stapel-core` raised to `>=0.16` (the release that added
  `ANONYMOUS_ALLOWED` / `ANONYMOUS_DENIED`).

## 0.8.2 — 2026-07-26

### Added — `error-keys/` is finally mounted

`CdnErrorKeysView` has existed since the port but no `urls*.py` ever mounted it — in
*any* stapel library. stapel-translate's `error_collector` polls
`/{prefix}/api/v1/error-keys/` on every service, so the whole endpoint class
answered 404 from Django's URL resolver and the collector harvested nothing
while reporting a plain `HTTP 404`. It is now mounted in `urls_v1.py` at
`error-keys/` (v1 canon), service/staff-gated as the base view declares.

Deliberately **not** in the contract triad: `ErrorKeysView` sets
`schema = None` and `/error-keys` is on the flows allowlist, so `make
contract` is a no-op diff — this is infrastructure, not product surface.

### Fixed — `docs/capabilities.json` version drift

The hand-authored `capabilities.json` still said `0.8.0` while `pyproject.toml`
was already at `0.8.1` (this repo has no `make contract` target to regenerate
it). Realigned to the released version.

## 0.8.0 — 2026-07-17

cdn-modularity.md (owner GO, §67): client/server config parity, media
submodule extras (`images`/`video`/`recordings`/`files`) with per-submodule
system checks, and an honest pyvips failure path. Fleet follow-up to
stapel-core 0.12.4 (CdnImageField unfreeze).

### Changed — breaking (pre-1.0: minor = breaking)
- **`STAPEL_CDN["IMAGE_TYPES"]` → `STAPEL_CDN["ASSET_TYPES"]`.** Same
  namespace/semantics (`Image.type` choices, `models.get_image_type_choices`
  callable, accepts `(value, label)` pairs or plain strings) but now the
  **same key** the client-side `stapel_core.django.cdn.CdnImageField`
  reads (core 0.12.4) — a host project sets asset types once, in one dict,
  for both sides of the stack.
- **Default asset types: `("product", "avatar")` → `("avatar",)`.** The
  zero-infrastructure default (cdn-modularity.md §2.1/§5) — no
  marketplace-specific type baked in; a host project adds its own via
  `ASSET_TYPES`. `Image.type`'s field-level `default="product"` is
  unchanged (a static fallback value, not a validated choice) but its
  `help_text` now points at the new config key.
- **`services._IMAGE_PREFIXES` hardcoded `{"product", "avatar"}` set** →
  `_image_ref_prefixes()`, read fresh from `STAPEL_CDN["ASSET_TYPES"]` every
  call. This was a second, independently frozen copy of the exact "half the
  stack is modular, half isn't" gap the spec calls out — just living in the
  ref-resolution service layer instead of a client-side field.
- **`cdn.import_from_url`'s `image_type` validation** now reads
  `ASSET_TYPES` instead of the removed `IMAGE_TYPES` key.

### Added
- **`Audio` model** (`stapel_cdn.models`) — the "recordings" submodule
  (cdn-modularity.md §7.2, coordinator decision): passthrough storage is
  **always** available, no extra required; `is_compressed` tracks the
  separate, still-unimplemented ffmpeg-audio compression pass
  (`services.AudioProcessingService.compress_audio` — a documented stub,
  never silently marks a recording compressed). `AudioAdmin` registered;
  `build_render_metadata`/`_batch_resolve_media` extended for the `audio/`
  ref prefix.
- **`checks.py`** (tag `stapel_cdn`, same pattern as `stapel_core.bus.
  checks` E001): `stapel_cdn.images.E001` — pyvips not importable (fires
  unconditionally; `images` is core, not opt-in). `stapel_cdn.video.E002` /
  `stapel_cdn.recordings.E003` — `ffmpeg` missing while `"video"` /
  `"recordings"` is in the new `STAPEL_CDN["ENABLED_SUBMODULES"]` (default
  `("images",)`).
- **`pyproject.toml` extras**: `video`, `recordings` (both empty — `ffmpeg`
  is a system binary, not a pip package; these extras exist as
  deployment-intent markers, paired with `ENABLED_SUBMODULES`), `files`
  (empty, listed for submodule-table symmetry — no processing, no extra
  needed).
- `STAPEL_CDN["ALLOWED_AUDIO_EXTENSIONS"]` (default `.mp3 .wav .m4a .ogg
  .opus .flac .aac`), `STAPEL_CDN["MAX_AUDIO_SIZE"]` (default 50 MiB).
- `CONFIG.MD` — full `STAPEL_CDN` settings registry (new for this
  package).
- `VideoProcessingService` docstring now documents the ffmpeg-gate/
  VPS-only/poster-canon contract explicitly (same "documented stub, not a
  promise" posture as `stapel_geo.search.elasticsearch.
  ElasticsearchGeoSearchBackend`) — no runtime behavior change.

### Fixed
- **Silent 1x1 image-dimension degradation** (cdn-modularity.md §0.3):
  `Image.save()`'s pyvips dimension extraction was one broad
  `except Exception: pass` — indistinguishable, from the outside, from a
  deliberately tiny image. Now split into two paths, both still falling
  back to 1x1 (`process_image` can retry later) but each logging a loud
  `ERROR` naming the image and the cause: pyvips not installed (a
  deploy/config problem — see `checks.check_submodule_binaries` E001) vs. a
  genuinely unreadable file (corrupt upload, unsupported format).

### Migration
- `0004_alter_image_type_audio` — `Image.type` help_text update (no data
  change) + `Audio` model creation.

## 0.7.1 — 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## 0.7.0 — 2026-07-17

Legacy purge (pre-1.0: minor = breaking). Only the current mechanisms
remain; no compatibility shims.

### Removed
- **Legacy flat `CDN_*` settings aliases** (`CDN_MAX_IMAGE_SIZE`,
  `CDN_ALLOWED_IMAGE_EXTENSIONS`, `CDN_MAX_IMAGE_PIXELS`, `CDN_WATERMARK`,
  `CDN_WATERMARK_TEXT`): `CdnAppSettings` is gone, `cdn_settings` is a plain
  `stapel_core.conf.AppSettings`. Configure via the `STAPEL_CDN` dict (or an
  unprefixed flat setting / env var of the same key name).
- **`CDN_ALLOWED_VIDEO_EXTENSIONS` flat setting** replaced by
  `STAPEL_CDN["ALLOWED_VIDEO_EXTENSIONS"]` (default
  `.mp4 .webm .mov .avi .mkv`) — previously required with no default.
- **`models.ImageType` TextChoices** — the authoritative, overridable list
  is `STAPEL_CDN["IMAGE_TYPES"]` via `models.get_image_type_choices`; use
  plain `"product"` / `"avatar"` strings.
- **`Video.variant_720_jpg` field** (migration `0003`, contract-phase) —
  never populated; variants are WebP-only. Dropped from admin too.
- **`ImageProcessingService.generate_image_variants`** backwards-compat
  alias — call `process_image`.
- Stale OpenAPI upload description (720px-JPEG fallback, wrong tier list)
  now documents the real WebP thumbnail/preview ladder.

## 0.6.1 — 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules). Suite green against core 0.11.2 (incl. the
  `images`/`s3`/`celery` extras), no code changes needed.

## 0.6.0 — 2026-07-16

Breaking tier semantics (pre-1.0: minor = breaking). Implements the
images-and-cdn.md (§61) aspect-friendly ladder. Alpha policy: **no
compatibility file layouts, no data migrations** — after upgrading run
`manage.py regenerate_media` to rebuild every image's variants under the
new semantics.

### Changed — variant ladder is now aspect-friendly
- **Thumbnail tiers (16/32/64/120) are min-side resized** (`_resize(...,
  axis="min")`): the *smaller* side of the file equals the tier, so square
  avatar/grid slots never upscale regardless of orientation. Previously the
  single ladder resized by height only — a portrait 600×3000 produced a
  24×120 "120px" thumbnail (×5 upscale in a 120×120 slot).
- **Preview tiers (160/240/480/560/720/1080) generate two branches per
  tier**: `{T}w.webp` (width == T) and `{T}h.webp` (height == T), each with
  its own ladder pass — the client picks the branch matching the slot's
  limiting axis (cover/contain × aspect), never upscaling. **560 added** to
  the default ladder between 480 and 720.
- **Square dedup (±1px)**: square images generate only the w-branch; the
  render metadata carries `square: true` (any branch equivalent) instead of
  a duplicate file.
- **`STAPEL_CDN["VARIANT_SIZES"]` replaced** by `THUMBNAIL_SIZES`
  (`(16, 32, 64, 120)`) and `PREVIEW_SIZES` (`(160, 240, 480, 560, 720,
  1080)`). `ImageProcessingService.get_variant_sizes()` removed;
  `get_thumbnail_sizes()` / `get_preview_sizes()` read the new keys.
- **`Image.get_variant_url(size, branch=None)`**: thumbnails resolve to
  `{tier}.webp`, preview tiers to `{tier}{branch}.webp` (default `w`).
  `variant_<size>_url` properties cover the new default ladder (incl. 560).
- **Legacy `720.jpg` fallback removed** (file, `variant_720_jpg_url`
  property, serializer field, admin link). WebP-incapable browsers are not
  a supported target.

### Added
- **`Image.variants_meta` JSONField** (migration `0002`, expand-only):
  per-variant geometry `[{tier, branch, url, width, height}]`, filled by
  the pipeline (branch `null` = min-side thumbnail; previews `"w"`/`"h"`).
  Exposed in `ImageSerializer` as `variants_meta`.
- **`cdn.describe` comm Function** — render-metadata snapshot
  (images-and-cdn.md §5): `{mime, bytes, width, height, aspect,
  duration_ms, preview_b64, square, variants[]}`; `preview_b64` inlines the
  16px micro tier as a `data:image/webp;base64,...` URI (blur-up
  placeholder). `variants[]` = `variants_meta` + the original file. Videos
  report `duration_ms`; generic files report mime/bytes only. Unknown ref
  raises (`LookupError` → `FunctionCallError`).
- **`manage.py regenerate_media`** (`--type`, `--dry-run`) — deletes
  generated variants (old single-ladder files and `720.jpg` included) and
  re-runs the pipeline for every image. The operational launch step of this
  release.

### Changed — HTTP surface (v1 canon, api-versioning.md §2/§6)
- URL set moved to `stapel_cdn.urls_v1` (paths inside unchanged); the root
  `stapel_cdn.urls` now mounts it under the mandatory `v1/` sub-prefix.
  Hosts keep `include('stapel_cdn.urls')` under `.../cdn/api/` — the
  surface becomes `/cdn/api/v1/...`. Bare `/cdn/api/...` no longer exists
  (one-off pre-gate sweep, no deprecation window: the bare path was never a
  published stable contract).

### Fixed
- Shrink-on-load calls now pass an explicit unbounded free axis —
  `vips_thumbnail` defaults `height` to `width` (square bounding box),
  which silently made ladder loads max-side-bound instead of
  axis-bound.
- Admin variant-size display resolved files under the wrong directory
  (`images/` instead of `<type>/`) — file sizes now show for existing
  variants.

## 0.5.3 — 2026-07-16

### Fixed
- **`user.deletion_initiated` is now actually handled.** The consume schema
  (`schemas/consumes/user.deletion_initiated.json`) was declared with no
  `@on_action` handler — a silent contract lie (2026-07-16 audit). The new
  handler purges the user's *unreferenced* media (`refs == []`, binaries +
  rows) at grace start via `CDNGDPRProvider.purge_unreferenced()`; media
  referenced by live content keeps serving and keeps its ownership link
  until `user.deleted` — the closure grace period is cancellable
  (platform precedent: stapel-notifications' soft grace actions, "full
  erasure stays on `user.deleted`"). Idempotent.
- **`user.deleted` now confirms erasure to the gdpr orchestrator.** In the
  remote-deletion protocol the payload carries a `correlation_id` and the
  orchestrator waits for a `gdpr.section.erased` confirmation per service —
  the cdn handler never sent one, so the closure's `media` part stayed
  incomplete and the closure hung in DELETING forever. The handler now
  emits `gdpr.section.erased` (`service: "media"`) in one transaction with
  the erasure; without a `correlation_id` (monolith in-process path)
  nothing is emitted, as before.

### Changed
- `CDNGDPRProvider.delete()` refactored: the unreferenced-purge half is the
  new public `purge_unreferenced()` (shared with the grace handler);
  behavior of `delete()` unchanged (purge orphans + anonymize referenced).

## 0.5.2 — 2026-07-16

### Fixed
- Dependency pin: `stapel-core` requirement was still `>=0.8,<0.9` — three
  releases behind every other stapel-* module (`>=0.10,<0.11`, matching
  stapel-auth / stapel-profiles) and behind the 0.10.1 production fix
  (`users_user.avatar` URLField widening). Bumped to `>=0.10,<0.11`. Full
  suite (275 tests) passes unchanged against core 0.10.1 — no code changes
  were needed.

## 0.5.1 — 2026-07-06

### Security
- `cdn.import_from_url` SSRF hardening: `_ip_is_forbidden` was **not**
  unwrapping the NAT64 well-known prefix (`64:ff9b::/96`, RFC 6052) before
  checking `is_global`, so a forbidden IPv4 address (loopback, RFC1918, the
  `169.254.169.254` cloud-metadata address, or CGNAT) smuggled in as e.g.
  `64:ff9b::a9fe:a9fe` read as an ordinary global IPv6 address and sailed
  past the DNS/IP allowlist — only the unrelated `::ffff:0:0/96`
  (`ipv4_mapped`) and `2002::/16` (`sixtofour`) IPv6 forms were unwrapped.
  `_ip_is_forbidden` now also unwraps the NAT64 prefix to its embedded IPv4
  address and validates that. Also added an explicit `100.64.0.0/10` (RFC
  6598 CGNAT shared address space) check rather than relying solely on
  `ipaddress.is_global` for it, matching the existing "spell the ranges out
  for auditability" approach used for the other forbidden ranges.
- New adversarial tests: NAT64-encoded loopback/RFC1918/metadata/CGNAT
  addresses; plain CGNAT addresses and their range boundaries; and tests
  that exercise the real (non-mocked) `_open()` to confirm `conn.sock`
  pinning stops `http.client` from ever re-resolving/re-connecting via its
  own `auto_open` path (per redirect hop too), and that `HTTP(S)_PROXY`
  environment variables have no effect — the connection always goes
  directly to the pre-validated, pinned IP.


## 0.5.0 — 2026-07-06

### Added
- **`cdn.import_from_url` comm Function** — SSRF-hardened server-side image
  import. Input `{url, image_type, caller?}`, output `{ref: "<type>/<hash>"}`
  pointing at a stored asset with resize variants generated exactly like a
  normal upload. Deliberately a comm Function, **not** an HTTP endpoint, so it
  cannot be driven as an open proxy from outside.
- `stapel_cdn/fetch.py` — the hardened egress fetcher. Controls (each with a
  dedicated adversarial test in `tests/test_import_from_url.py`): https-only
  (enforced on every redirect hop); DNS resolution with allowlisting of **all**
  returned IPs against private RFC1918/ULA, loopback, link-local (incl. the
  `169.254.169.254` cloud-metadata endpoint), multicast, reserved and
  unspecified ranges, plus IPv4-mapped-IPv6 unwrapping; **anti-DNS-rebinding**
  via IP pinning — resolve once, validate, connect to that exact IP while
  presenting the hostname for TLS SNI/`Host`; redirects driven manually with
  per-hop re-validation and a hop cap; streaming body read with a hard size cap
  that aborts before buffering; connect/read timeout; magic-byte content check
  (Pillow decode) routed through the existing
  `validate_image_file`/`ALLOWED_IMAGE_EXTENSIONS`; per-caller fixed-window
  rate limit (Django cache) as an open-proxy/amplification defence. Fails
  closed — no path returns a ref for an unvalidated source.
- New `STAPEL_CDN` settings: `IMPORT_FROM_URL_MAX_BYTES` (10 MB),
  `IMPORT_FROM_URL_TIMEOUT` (5 s), `IMPORT_FROM_URL_MAX_REDIRECTS` (3),
  `IMPORT_FROM_URL_RATE` (`"10/h"`).

Consumed by stapel-profiles' `user.registered` handler to re-host OAuth
provider avatars onto the CDN.


## 0.4.4 — 2026-07-06

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.


## 0.4.3 — 2026-07-06

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_cdn.tests`
  subpackage is no longer listed in `[tool.setuptools] packages`). Added
  `[project.urls]`, completed the trove classifiers (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section (single source shared with the git hooks/CI).


## 0.4.2 — 2026-07-05

### Fixed
- OpenAPI: type hints on Image/Video/FileModel serializer URL fields +
  request schema for `ImageUploadView`. `ImageSerializer`,
  `VideoSerializer` and `FileModelSerializer` URL fields now carry explicit
  `string`/`uri` types (via `URLField(read_only=True)` for the
  property-backed image variants and `@extend_schema_field` on the method
  getters), silencing drf-spectacular "unable to resolve type hint"
  warnings. `ImageSerializer.variant_1440_url` / `variant_2160_url` — which
  have no backing `variant_<size>_url` model property (not in
  `DEFAULT_VARIANT_SIZES`) and were silently dropped from responses while
  making drf-spectacular error resolving them against the model — are now
  `SerializerMethodField`s computed from `Image.get_variant_url`.
  `ImageUploadView`'s `@extend_schema` no longer passes `OpenApiExample`
  objects as `responses` values (which drf-spectacular could not resolve);
  201/200 now point at `ImageUploadResponseSerializer` (what the view
  returns) with the example bodies moved into `examples`, and `request` is
  the real `FileUploadSerializer`.


## 0.4.1 — 2026-07-05

### Fixed
- `user_id` in comm schemas typed uuid, was integer — rejected valid
  `user.deleted` events. `schemas/consumes/user.deleted.json` and
  `schemas/consumes/user.deletion_initiated.json` now type `user_id` as
  `{"type": "string", "format": "uuid"}`, matching the UUID-pk canonical
  user and the auth/gdpr producers.


## 0.4.0 — 2026-07-04
### Changed
- **Watermarking is now a pluggable engine, off by default.**
  `STAPEL_CDN["WATERMARK"]` (legacy alias `CDN_WATERMARK`) names a callable
  `(pyvips.Image) -> pyvips.Image` via dotted path; empty (the default)
  disables watermarking. The previous behavior — a hardcoded "Iron" text
  label rendered by pyvips — is gone; the text renderer survives as the
  reference engine `stapel_cdn.watermarks.text_watermark`, configured via
  `STAPEL_CDN["WATERMARK_TEXT"]` (`CDN_WATERMARK_TEXT`). To restore a text
  watermark: `STAPEL_CDN = {"WATERMARK": "stapel_cdn.watermarks.text_watermark",
  "WATERMARK_TEXT": "..."}`.
- `ImageProcessingService._add_watermark` now dispatches to the configured
  engine and takes no `text` argument.

## 0.3.0 — 2026-07-03

No functional changes — version alignment with the Stapel 0.3
release train; stapel-core dependency now `>=0.3.0,<0.4`.


## [0.2.0] - 2026-07-02

### Added
- `stapel_cdn.conf.cdn_settings` — `AppSettings("STAPEL_CDN")` namespace with
  defaults matching the previously hardcoded values:
  - `IMAGE_TYPES` (default: `product`, `avatar`)
  - `VARIANT_SIZES` (default: `16, 32, 64, 120, 160, 240, 480, 720, 1080`)
  - `MAX_IMAGE_SIZE` (default: 20 MB)
  - `ALLOWED_IMAGE_EXTENSIONS` (default: `.jpg .jpeg .png .gif .webp .bmp .heic .heif`)
  - `MAX_IMAGE_PIXELS` (default: 50,000,000 — Pillow decompression-bomb cap)

  Legacy flat settings `CDN_MAX_IMAGE_SIZE`, `CDN_ALLOWED_IMAGE_EXTENSIONS`
  and `CDN_MAX_IMAGE_PIXELS` keep working as aliases.
- comm Function providers in `stapel_cdn.functions`, registered from
  `CdnConfig.ready()`:
  - `cdn.media_exists` — payload `{"ref": "<type>/<id>"}` →
    `{"exists": bool}` (same resolution logic as the refs sync service).
  - `cdn.refs_sync` — comm equivalent of the `RefSyncView` HTTP endpoint,
    delegating to `services.apply_ref_sync`.
- `stapel_core.signals.media_processed` is now sent (with `instance=`) after
  successful variant generation at pipeline completion
  (`ImageProcessingService.process_image`).
- `Image.variant_urls` property — `{size: url}` mapping honoring
  `STAPEL_CDN["VARIANT_SIZES"]` overrides.
- `ImageProcessingService.get_variant_sizes()/get_thumbnail_sizes()/get_preview_sizes()`
  — conf-driven pipeline size lists (split at `THUMBNAIL_MAX_HEIGHT` = 120).
- `py.typed` marker (PEP 561) shipped in the package.

### Changed
- Upload views, validators and the upload serializer read
  `MAX_IMAGE_SIZE` / `ALLOWED_IMAGE_EXTENSIONS` / `MAX_IMAGE_PIXELS` from
  `cdn_settings` instead of hardcoded constants and raw Django settings.
- `Image.type` choices come from the `get_image_type_choices()` callable
  (conf-driven); view-level image type validation uses it too.
- The `variant_16_url` ... `variant_1080_url` properties are now generated
  dynamically from the default size list and delegate to
  `Image.get_variant_url(size)`; names and values are unchanged.
- Image/Video/File `uploaded_by` foreign keys reference
  `settings.AUTH_USER_MODEL` instead of the concrete
  `stapel_core.django.users.models.User` class; migration `0001_initial`
  uses `migrations.swappable_dependency(settings.AUTH_USER_MODEL)`.

### Fixed
- Nothing.
