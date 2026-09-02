# CONFIG.MD — stapel-cdn

Config registry for **stapel-cdn** (`static-scaffold-and-config.md` §2). One
row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_CDN` settings dict), what it is for,
whether it is required, and its default.

Every key is read through `cdn_settings` (`stapel_cdn.conf.AppSettings`,
namespace `STAPEL_CDN`). Resolution order per key: `settings.STAPEL_CDN`
dict → a flat Django setting of the same name → environment variable → the
default below.

`ASSET_TYPES` is shared with the client-side `stapel_core.django.cdn`
package under the **same** `STAPEL_CDN["ASSET_TYPES"]` key
(cdn-modularity.md §2.1/§5) — a host project sets it once and both the
`CdnImageField` validation (client) and `Image.type` choices (this server)
agree on what's legal. `ENABLED_SUBMODULES` is the opt-in registry for the
`video`/`recordings` submodules (cdn-modularity.md §2.2/§3) — turning one on
is what makes `checks.check_submodule_binaries`'s ffmpeg probe for that
submodule active. `images` is enabled by default and gates two probes: `E001` (no libvips at
all — it is the only image decoder the library has since 0.10) and `E004` (libvips present but
blind to a format `ALLOWED_IMAGE_EXTENSIONS` declares allowed). A deployment running stapel-cdn
as passthrough file storage drops `images` from the tuple and is not asked to install libvips.

## stapel-cdn

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| ASSET_TYPES | env | axis: `Image.type` choices — open, host-extendable (shared with client-side `stapel_core.django.cdn`). | no | `("avatar",)` |
| ENABLED_SUBMODULES | env | axis: which of `images`/`video`/`recordings` this deployment turns on — gates the ffmpeg system-check probes for `video`/`recordings`. | no | `("images",)` |
| MEDIA_KINDS | env | axis: open media-kind registry, merged over `stapel_cdn.kinds.BUILTIN_MEDIA_KINDS` (`image`, `gif`, `video`, `audio`, `file`). An entry replaces a builtin, `None` removes one. Entry shape: `{"model": "image"\|"video"\|"audio"\|"file", "extensions": (...), "asset_types": (...), "preview": "blur"\|"poster"\|"waveform"\|None, "animated": bool}`. Stickers or any later kind are a dict literal here, not a release. Malformed entries → `checks.W009`. | no | `{}` |
| MICRO_PREVIEW_MAX_BYTES | env | Byte ceiling for ONE inline preview, measured on the finished `data:` URI (base64 expansion included — exactly what lands in a JSON payload). A 16px WebP LQIP is ~300-800 B, a 240x40 waveform strip ~1.5-3 KB, so 4 KiB is ~5x headroom and bounds a 40-attachment chat page at ~160 KB. Enforced by **downgrade-then-refuse**, never truncation: WebP quality ladder → smaller waveform strip → no preview at all with `meta_reason: "preview_over_budget"`. Applied on **read** too, so lowering it takes effect for rows stamped earlier. An unusable value falls back to this default (`checks.W011`). | no | `4096` |
| DESCRIBE_PERMISSIONS | env | permission classes (dotted paths, ALL must pass) of `POST /cdn/api/v1/describe/`. Default is the seam the read endpoints use: signed in (guest sessions included) **or** an internal service call. The endpoint answers for refs the caller did not upload — that is the case it exists for — and the snapshot it returns carries no uploader identity, filename or `refs[]`, which is why it is not owner-scoped the way `FileExistsView` is. Tighten to `["stapel_core.django.api.permissions.IsServiceRequest"]` to keep describe service-side; open to `["rest_framework.permissions.AllowAny"]` for public media, and then `DESCRIBE_ANON_THROTTLE` is the only brake. An entry that does not import → `checks.E005`; an EMPTY list means no guard at all (DRF reads no permission classes as "everyone passes") → `checks.W012`. | no | `["stapel_cdn.permissions.IsAuthenticatedOrService"]` |
| DESCRIBE_THROTTLE | env | throttle rate of `POST /describe/` (DRF `ScopedRateThrottle`, scope `cdn_describe`). Batch size IS response size here — 50 snapshots x `MICRO_PREVIEW_MAX_BYTES` at the ceiling — so this rate bounds bytes, not just queries. Unparseable → `checks.E005` (the rate is read per request, so it would 500 every call). | no | `60/min` |
| DESCRIBE_ANON_THROTTLE | env | throttle rate applied to anonymous callers of `POST /describe/`; dormant under the default `DESCRIBE_PERMISSIONS` (which refuses them outright) and the only brake once it is opened. Empty means that class of caller is unthrottled, not broken; unparseable → `checks.E005`. | no | `10/min` |
| WAVEFORM_SIZES | env | Waveform strip geometry (pixels), best first; each is rendered and encoded until one fits `MICRO_PREVIEW_MAX_BYTES`. | no | `((240, 40), (120, 32))` |
| WAVEFORM_COLOR | env | Ink colour passed to ffmpeg's `showwavespic` filter. | no | `"#3f7fbf"` |
| POSTER_FRAME_AT | env | Second the video poster frame is lifted from. 0 takes the first frame, which on real footage is often a black fade-in. | no | `1.0` |
| POSTER_MAX_WIDTH | env | Max width of the derived poster written to `MEDIA_ROOT/video/<hash>/poster.webp`. The inline micro poster is separate. | no | `720` |
| MEDIA_TOOL_TIMEOUT | env | Wall-clock ceiling (seconds) on any single ffprobe/ffmpeg call. A tool that hangs degrades with `meta_reason: "tool_timeout"` instead of pinning a worker. | no | `30.0` |
| THUMBNAIL_SIZES | env | min-side thumbnail tiers (no w/h branches). | no | `(16, 32, 64, 120)` |
| PREVIEW_SIZES | env | preview tiers (w/h branches each). | no | `(160, 240, 480, 560, 720, 1080)` |
| MAX_IMAGE_SIZE | env | upload size cap for images, bytes. | no | `20 * 1024 * 1024` |
| ALLOWED_IMAGE_EXTENSIONS | env | accepted image file extensions. Must be decodable by the deployment's libvips — `checks.E004` fails the boot when this advertises a format that build cannot read, rather than letting an uploader be told their file is invalid. The default is what a stock `stapel-cdn[images]` install decodes, so it passes E004 with nothing configured; widen it only alongside a libvips build that reads the added format (`.bmp` needs the ImageMagick module — libvips has no native BMP reader). | no | `.jpg .jpeg .png .gif .webp .avif .heic .heif` |
| ALLOWED_VIDEO_EXTENSIONS | env | accepted video file extensions. | no | `.mp4 .webm .mov .avi .mkv` |
| MAX_VIDEO_SIZE | env | upload size cap for videos, bytes — enforced before the body is hashed. | no | `100 * 1024 * 1024` |
| ALLOWED_AUDIO_EXTENSIONS | env | accepted audio file extensions (`recordings` submodule — passthrough storage always accepts these). | no | `.mp3 .wav .m4a .ogg .opus .flac .aac` |
| MAX_IMAGE_PIXELS | env | decompression-bomb cap — an upload wider*taller than this is refused. Exact since 0.10 (Pillow used to only raise above *2x* the configured number). | no | `50_000_000` |
| MAX_AUDIO_SIZE | env | upload size cap for audio recordings, bytes. | no | `50 * 1024 * 1024` |
| REQUIRE_DECODER | env | whether image storage requires a working decoder. With no libvips the pixel cap and the decode never run, so `True` refuses the upload (503) instead; `False` keeps the signature-only passthrough. | no | `True` |
| WATERMARK | env | dotted path to (or a) callable `(pyvips.Image) -> pyvips.Image` applied to preview variants; empty disables watermarking. | no | `""` |
| WATERMARK_TEXT | env | text rendered by the built-in `stapel_cdn.watermarks.text_watermark` engine. | no | `""` |
| IMPORT_FROM_URL_MAX_BYTES | env | `cdn.import_from_url` body size cap, bytes. | no | `10 * 1024 * 1024` |
| IMPORT_FROM_URL_TIMEOUT | env | `cdn.import_from_url` connect/read timeout, seconds. | no | `5.0` |
| IMPORT_FROM_URL_MAX_REDIRECTS | env | `cdn.import_from_url` max redirect hops (each re-validated: scheme + DNS + IP). | no | `3` |
| IMPORT_FROM_URL_RATE | env | `cdn.import_from_url` per-caller fixed-window quota (`N/s\|m\|h\|d`). | no | `10/h` |
| DEDUP_SCOPE | env | axis: who a content-hash lookup may match — `owner` (only the calling principal's rows) or `global` (deployment-wide pool; opt-in, warned about by `checks.W006`). | no | `owner` |
| MAX_OBJECTS_PER_OWNER | env | per-owner object ceiling across images + videos + files. `"unlimited"` opts out explicitly; any other unusable value fails closed to this default (`checks.W007`). | no | `1000` |
| MAX_BYTES_PER_OWNER | env | per-owner byte ceiling across images + videos + files. Same `"unlimited"` opt-out and same fail-closed rule. | no | `2 * 1024 * 1024 * 1024` |
| MAX_FILE_SIZE | env | upload size cap for the generic (non-image, non-video) intake, bytes. | no | `50 * 1024 * 1024` |
| ALLOWED_FILE_EXTENSIONS | env | accepted generic-file extensions. | no | `.pdf .doc .docx .xls .xlsx .ppt .pptx .txt .csv .zip .rar .7z .gz` |
| ALLOWED_FILE_MIME_TYPES | env | accepted generic-file `Content-Type` values. An upload that declares none is refused; `application/octet-stream` is deliberately NOT shipped (it is any client's word for "anything") — a host intaking opaque binaries adds it back. | no | the document/archive list in `conf.py` |
| THUMBNAILS_QUEUE | env | celery queue for `tasks.generate_thumbnails`. `None` sends with **no** `queue` option, so the task lands on the app's own default queue and a vanilla single-queue worker drains it. Set it only in a fleet that shards queues per service, to the name that fleet's worker consumes (`-Q`). Was the hardcoded literal `"thumbnails"` through 0.14 — a deployment setting `CELERY_TASK_DEFAULT_QUEUE` had zero consumers on it and every generated variant URL 404'd forever. `checks.W008` reports a value nothing Django-visible corroborates. | no | `None` |
| PREVIEWS_QUEUE | env | celery queue for `tasks.generate_previews`. Same rule, same history (the literal was `"previews"`), same `checks.W008`. | no | `None` |
| RETRY_UNPROCESSED_SCHEDULE | env | crontab kwargs for the `retry_unprocessed` safety net, consumed by `tasks.get_cdn_beat_schedule()`. Nothing schedules itself: a host that never wires the returned dict into `CELERY_BEAT_SCHEDULE` never runs it. | no | `{"minute": "*/5"}` |
| UNCLAIMED_TTL_HOURS | env | hours an upload may stay claimed by nothing before `services.sweep_unclaimed` reaps it, bytes and row. The clock is `unreferenced_since` — stamped at upload, cleared while `refs` is non-empty, restamped when the LAST ref is detached — so the TTL counts from when the object became unreferenced, never from `created_at`, and referenced media is never swept regardless of age. 48 h survives a weekend-long draft. An unusable value fails closed to this default. | no | `48` |
| SWEEP_UNCLAIMED_SCHEDULE | env | crontab kwargs for the unclaimed-media sweep, consumed by `tasks.get_cdn_beat_schedule()` alongside `RETRY_UNPROCESSED_SCHEDULE`. Nothing schedules itself; `checks.W013` warns when this process runs beat for other work and the sweep entry is absent (a host may sweep from cron via `manage.py cdn_sweep_unclaimed` instead). | no | `{"minute": "0"}` |
| PRIVATE_MEDIA_PREFIX | env | path prefix for non-image originals, so an operator has one prefix to deny on the public media route. Empty keeps the flat layout. | no | `private` |
