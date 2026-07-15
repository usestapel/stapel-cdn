# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
