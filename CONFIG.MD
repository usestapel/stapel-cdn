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
| THUMBNAIL_SIZES | env | min-side thumbnail tiers (no w/h branches). | no | `(16, 32, 64, 120)` |
| PREVIEW_SIZES | env | preview tiers (w/h branches each). | no | `(160, 240, 480, 560, 720, 1080)` |
| MAX_IMAGE_SIZE | env | upload size cap for images, bytes. | no | `20 * 1024 * 1024` |
| ALLOWED_IMAGE_EXTENSIONS | env | accepted image file extensions. Must be decodable by the deployment's libvips — `checks.E004` fails the boot when this advertises a format that build cannot read, rather than letting an uploader be told their file is invalid. | no | `.jpg .jpeg .png .gif .webp .bmp .heic .heif` |
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
| PRIVATE_MEDIA_PREFIX | env | path prefix for non-image originals, so an operator has one prefix to deny on the public media route. Empty keeps the flat layout. | no | `private` |
