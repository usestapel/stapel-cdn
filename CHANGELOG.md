# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
