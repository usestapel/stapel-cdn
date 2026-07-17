# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
