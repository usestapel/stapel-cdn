# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
