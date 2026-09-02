"""
Models for stapel-cdn service.
Uses pyvips for all image processing (supports JPEG, PNG, HEIC, etc.)
"""

import hashlib
import logging
import os
import re

from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from . import decoders
from .conf import DEFAULT_VARIANT_SIZES, cdn_settings
from .storage import cdn_storage

logger = logging.getLogger(__name__)


#: ``Image.variants_status`` values — the two answers the API gives to "do
#: the ``variant_*_url``s in this payload point at anything yet?".
VARIANTS_PENDING = "pending"
VARIANTS_READY = "ready"
VARIANTS_STATUSES = (VARIANTS_PENDING, VARIANTS_READY)

#: Names the preview/thumbnail pipeline writes into ``<type>/<hash>/``:
#: ``120.webp`` (thumbnail tiers) and ``720w.webp`` / ``720h.webp`` (preview
#: branches). See ``services.ImageProcessingService``.
_VARIANT_NAME_RE = re.compile(r"^\d+[wh]?\.webp$", re.IGNORECASE)


def _safe_original_name(filename: str) -> str:
    """Keep a client-supplied name out of the generated-variant namespace.

    The original and every generated variant share one content-addressed
    directory. A caller that names its upload ``720w.webp`` would land exactly
    where the preview pipeline writes, and ``OverwriteStorage`` lets the last
    writer win — so anyone holding the bytes of an object (its hash *is* those
    bytes) could replace a variant that is being served for it with content of
    their own choosing. Reserved names get a prefix; nothing else is touched.
    """
    base = os.path.basename(filename or "")
    return f"original_{base}" if _VARIANT_NAME_RE.match(base) else base


def _private_prefix() -> str:
    """``STAPEL_CDN["PRIVATE_MEDIA_PREFIX"]``, normalised to ``""`` or ``"x/"``.

    One prefix an operator can deny on the public media route, instead of
    having to enumerate every non-image type that must not be world-readable.
    """
    prefix = str(cdn_settings.PRIVATE_MEDIA_PREFIX or "").strip().strip("/")
    return f"{prefix}/" if prefix else ""


def image_upload_path(instance, filename):
    """Generate upload path for images: <type>/<hash>/<filename>"""
    return f"{instance.type}/{instance.file_hash}/{_safe_original_name(filename)}"


def video_upload_path(instance, filename):
    """Generate upload path for videos: video/<hash>/<filename>"""
    return f"video/{instance.file_hash}/{os.path.basename(filename or '')}"


def file_upload_path(instance, filename):
    """Generate upload path for files: <private>/file/<hash>/<filename>

    Documents and archives are the payloads the audit calls out as "may not be
    intended public", so they carry the private prefix.
    """
    return f"{_private_prefix()}file/{instance.file_hash}/{os.path.basename(filename or '')}"


def audio_upload_path(instance, filename):
    """Generate upload path for audio recordings: <private>/audio/<hash>/<filename>"""
    return f"{_private_prefix()}audio/{instance.file_hash}/{os.path.basename(filename or '')}"


def get_image_type_choices():
    """Image type choices from conf (``STAPEL_CDN["ASSET_TYPES"]``).

    Accepts either (value, label) pairs or plain strings — kept for hosts
    that still pass pairs, though the canonical form (cdn-modularity.md
    §2.1) is a flat tuple of strings shared with the client-side
    ``stapel_core.django.cdn`` config under the same key.
    Referenced as a callable by the ``Image.type`` field so overriding
    ASSET_TYPES never produces a model/migration change.
    """
    choices = []
    for entry in cdn_settings.ASSET_TYPES:
        if isinstance(entry, str):
            choices.append((entry, entry.capitalize()))
        else:
            value, label = entry
            choices.append((value, label))
    return choices


class Image(models.Model):
    """Model for storing images with multiple resolution variants."""

    # File identification
    file_hash = models.CharField(
        max_length=64, db_index=True, help_text="SHA-256 hash of the original file"
    )
    original_filename = models.CharField(max_length=255)
    file_extension = models.CharField(max_length=10)
    type = models.CharField(
        max_length=10,
        choices=get_image_type_choices,
        default="product",
        help_text=(
            "Image type — validated against STAPEL_CDN['ASSET_TYPES'] "
            "(default ('avatar',); the historical 'product' default value "
            "here is only in scope for host projects that still add it)."
        ),
    )

    # Original file and metadata
    original = models.FileField(
        upload_to=image_upload_path,
        max_length=500,
        storage=cdn_storage,
        help_text="Original uploaded image",
    )
    original_width = models.IntegerField(default=0)
    original_height = models.IntegerField(default=0)
    original_size = models.BigIntegerField(help_text="File size in bytes")

    # Processing status
    is_processed = models.BooleanField(
        default=False, help_text="Whether variants have been generated"
    )
    #: When the variant ladder finished generating — stamped by
    #: ``tasks.generate_previews`` in the same save that flips
    #: ``is_processed``. NULL while the row is still pending (or if the
    #: generation task failed), which is what makes ``variants_status``
    #: answerable at all: a row created a millisecond ago and a row whose
    #: worker died are otherwise identical from the API.
    variants_ready_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When variant generation completed; null while pending",
    )
    processing_log = models.TextField(
        blank=True, default="", help_text="Log of processing operations"
    )

    # Per-variant geometry, filled by the processing pipeline
    # (images-and-cdn.md §5/§6 item 3): list of
    # {"tier": int, "branch": "w"|"h"|None, "url": str,
    #  "width": int, "height": int}. branch is None for thumbnail-class
    # (min-side) tiers; square images carry only the w-branch (§3.3).
    variants_meta = models.JSONField(
        default=list,
        blank=True,
        help_text="Generated variants: [{tier, branch, url, width, height}]",
    )

    # Inline blur-up placeholder — the micro thumbnail tier as a data URI,
    # stamped by the SAME libvips pass that writes 16.webp (services.
    # ImageProcessingService.generate_thumbnails_only keeps the encoded
    # buffer rather than re-reading the file). Bounded by
    # STAPEL_CDN["MICRO_PREVIEW_MAX_BYTES"]; empty means "not generated" or
    # "refused", and meta_reason below says which.
    preview_b64 = models.TextField(
        blank=True,
        default="",
        help_text="Inline blur-up placeholder: data:image/webp;base64,...",
    )
    meta_reason = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "Named reason the render metadata is incomplete "
            "(stapel_cdn.metadata.REASONS); empty when nothing degraded."
        ),
    )

    # Reference tracking
    refs = models.JSONField(
        default=list,
        blank=True,
        help_text="List of references: service/entity_type/entity_id",
    )

    # User tracking
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_images",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The unclaimed-media clock. Stamped at upload (an upload starts claimed
    # by nobody), cleared while ``refs`` is non-empty, restamped the moment
    # the LAST ref is detached (``services.apply_ref_sync``) — so the TTL in
    # ``STAPEL_CDN["UNCLAIMED_TTL_HOURS"]`` counts from when the object
    # BECAME unreferenced, never from ``created_at``. ``services.
    # sweep_unclaimed`` reaps rows past it: zero-ref AND expired, bytes+row.
    unreferenced_since = models.DateTimeField(
        null=True,
        blank=True,
        default=timezone.now,
        db_index=True,
        help_text=(
            "When this object last became zero-ref; NULL while anything "
            "references it. The sweep TTL counts from here."
        ),
    )

    class Meta:
        db_table = "cdn_image"
        verbose_name = "Image"
        verbose_name_plural = "Images"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["file_hash"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["is_processed"]),
        ]
        constraints = [
            # One row per (content, type, owner). Identical bytes held by two
            # principals are two rows over one content-addressed blob: dedup is
            # scoped to the owner (``ownership.dedup_scope_q``), so a second
            # owner has to be able to record its own object rather than inherit
            # the first one's identity, filename and reference list.
            #
            # Split in two because SQL counts NULLs as distinct: the partial
            # constraint keeps the service-owned pool (``uploaded_by IS NULL``,
            # written by ``cdn.import_from_url``) at exactly one row per
            # (content, type), which a plain three-column constraint would not.
            models.UniqueConstraint(
                fields=["file_hash", "type", "uploaded_by"],
                name="cdn_image_hash_type_owner_unique",
                condition=models.Q(uploaded_by__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["file_hash", "type"],
                name="cdn_image_hash_type_service_unique",
                condition=models.Q(uploaded_by__isnull=True),
            ),
        ]

    def __str__(self):
        return f"Image: {self.file_hash[:8]}... ({self.original_filename})"

    @property
    def variants_status(self):
        """``"pending"`` until the variant ladder exists, then ``"ready"``.

        Derived from ``is_processed`` rather than stored beside it: two
        columns for one fact drift, and the fact already has an owner. This
        is the *name* the API answers under — ``variant_*_url`` is computed
        from the hash and is therefore returned in full at row creation,
        long before any of those files exist. Without this field a 201 and a
        finished image are byte-identical to a caller, which is how a fleet
        served 404s from a successful upload for weeks.
        """
        return VARIANTS_READY if self.is_processed else VARIANTS_PENDING

    def get_variant_url(self, size, branch=None):
        """URL for a variant tier (int or str). All WebP.

        Thumbnail-class tiers (``STAPEL_CDN["THUMBNAIL_SIZES"]``, min-side
        resize) have no branches: ``{tier}.webp``. Preview-class tiers are
        branched (images-and-cdn.md §3.2): ``{tier}w.webp`` / ``{tier}h.webp``
        — ``branch`` defaults to ``"w"`` (square images store only the
        w-branch, §3.3).
        """
        tier = int(size)
        thumbnails = {int(s) for s in cdn_settings.THUMBNAIL_SIZES}
        suffix = "" if tier in thumbnails else (branch or "w")
        return f"{settings.MEDIA_URL}{self.type}/{self.file_hash}/{tier}{suffix}.webp"

    @property
    def variant_urls(self):
        """Mapping ``size -> URL`` for all configured tiers.

        Thumbnail tiers map to their min-side file, preview tiers to the
        w-branch. Full per-branch geometry lives in ``variants_meta``.
        """
        sizes = list(cdn_settings.THUMBNAIL_SIZES) + list(cdn_settings.PREVIEW_SIZES)
        return {int(size): self.get_variant_url(size) for size in sizes}

    @staticmethod
    def calculate_file_hash(file):
        """Calculate SHA-256 hash of a file."""
        hash_sha256 = hashlib.sha256()
        for chunk in file.chunks():
            hash_sha256.update(chunk)
        file.seek(0)  # Reset file pointer after reading
        return hash_sha256.hexdigest()

    def save(self, *args, **kwargs):
        """Override save to automatically extract metadata from uploaded file."""
        # Only process if this is a new object and we have an original file
        if not self.pk and self.original:
            # Calculate file hash
            if not self.file_hash:
                self.file_hash = self.calculate_file_hash(self.original)

            # Get filename and extension
            if not self.original_filename:
                self.original_filename = self.original.name

            if not self.file_extension:
                self.file_extension = os.path.splitext(self.original.name)[1].lower()

            # Get file size
            if not self.original_size:
                self.original_size = self.original.size

            # Get image dimensions using pyvips (supports HEIC/HEIF).
            #
            # cdn-modularity.md §0.3: this used to be a single broad
            # `except Exception: pass` that silently substituted 1x1
            # placeholder dimensions — indistinguishable, from the outside,
            # from a deliberately tiny image. Without libvips installed
            # (the "images" extra's system dependency), EVERY image in the
            # deployment silently got 1x1 dimensions, discovered only
            # postmortem in production. Split into two honest, loud paths:
            # missing library (a deploy/config problem — see also
            # checks.check_submodule_binaries E001) vs. a genuinely
            # unreadable file (corrupt upload, unsupported format) — both
            # still fall back to the 1x1 placeholder (process_image can
            # retry later), but now always with an ERROR log naming which
            # image and why, instead of a quiet pass.
            # Read from the FILE OBJECT, not self.original.path.
            #
            # The path is where the file is ABOUT to live: FileField writes it
            # to storage during super().save() below, which has not run yet. So
            # `new_from_file(self.original.path)` opened a filename that did not
            # exist and every image uploaded through the API — every format, not
            # just the HEIC that got this looked at — fell into the "unreadable
            # file" branch and was stored 1x1. The honest-logging split
            # (§0.3) landed and worked exactly as designed: it logged an ERROR
            # per upload naming the image and the reason. Nothing was reading
            # the logs, so the placeholder shipped anyway. The file object is
            # open right here and is what the validator already decoded.
            if not self.original_width or not self.original_height:
                extension = self.file_extension or os.path.splitext(
                    self.original.name
                )[1].lower()
                try:
                    dimensions = decoders.decode_dimensions(self.original, extension)
                except Exception as exc:
                    logger.error(
                        "libvips failed to read dimensions for image %r "
                        "(type=%s): %r. Falling back to 1x1 placeholder "
                        "dimensions; process_image may retry later.",
                        self.original_filename or (self.original and self.original.name),
                        self.type,
                        exc,
                    )
                    dimensions = None
                else:
                    if dimensions is None:
                        logger.error(
                            "pyvips is not installed — cannot read dimensions "
                            "for image %r (type=%s). Install the system libvips "
                            "library and `pip install stapel-cdn[images]`. "
                            "Falling back to 1x1 placeholder dimensions.",
                            self.original_filename
                            or (self.original and self.original.name),
                            self.type,
                        )
                if dimensions:
                    self.original_width, self.original_height = dimensions
                else:
                    self.original_width = self.original_width or 1
                    self.original_height = self.original_height or 1

        super().save(*args, **kwargs)


def _variant_url_property(size):
    """Build a ``variant_<size>_url`` property delegating to get_variant_url."""

    def getter(self):
        return self.get_variant_url(size)

    getter.__name__ = f"variant_{size}_url"
    getter.__doc__ = f"URL of the {size}px WebP variant."
    return property(getter)


# Keep the historical `variant_16_url` ... `variant_1080_url` property names
# working for the default sizes (generated from conf defaults).
for _size in DEFAULT_VARIANT_SIZES:
    setattr(Image, f"variant_{_size}_url", _variant_url_property(_size))
del _size


class Video(models.Model):
    """Model for storing videos with multiple resolution variants."""

    # File identification
    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of the original file",
    )
    original_filename = models.CharField(max_length=255)
    file_extension = models.CharField(max_length=10)

    # Original file and metadata
    original = models.FileField(
        upload_to=video_upload_path, max_length=100, help_text="Original uploaded video"
    )
    original_width = models.IntegerField(null=True, blank=True)
    original_height = models.IntegerField(null=True, blank=True)
    original_size = models.BigIntegerField(help_text="File size in bytes")
    duration = models.FloatField(null=True, blank=True, help_text="Duration in seconds")

    # Resolution variants (auto-generated with watermark)
    # Note: Video processing will be implemented later with ffmpeg
    variant_16 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="16px height variant (no watermark)",
    )
    variant_32 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="32px height variant (no watermark)",
    )
    variant_64 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="64px height variant (no watermark)",
    )
    variant_160 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="160px height variant with watermark",
    )
    variant_240 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="240px height variant with watermark",
    )
    variant_480 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="480px height variant with watermark",
    )
    variant_720 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="720px height variant with watermark",
    )
    variant_1080 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="1080px height variant with watermark",
    )
    variant_1440 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="1440px height variant with watermark",
    )
    variant_2160 = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="2160px height variant with watermark",
    )

    # Processing status. Since 0.16.0 this means the metadata pass actually
    # produced measured facts (ffprobe dimensions/duration, and a poster
    # frame when ffmpeg could extract one) — NOT merely "the pass was
    # called". A deployment with no ffmpeg leaves it False and names the
    # reason in meta_reason, so the backfill command and retry_unprocessed
    # can pick the row up once the binary exists.
    is_processed = models.BooleanField(
        default=False, help_text="Whether metadata/variants have been generated"
    )

    # Inline poster placeholder (micro frame) + the reason metadata is
    # incomplete, same contract as Image above.
    preview_b64 = models.TextField(
        blank=True,
        default="",
        help_text="Inline poster placeholder: data:image/webp;base64,...",
    )
    meta_reason = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "Named reason the render metadata is incomplete "
            "(stapel_cdn.metadata.REASONS); empty when nothing degraded."
        ),
    )
    # Whether a full-size poster frame exists on disk. The URL is derived
    # from <hash>, so it is well-formed long before the file is — the same
    # trap variants_status exists for on Image (a 201 whose URLs 404).
    has_poster = models.BooleanField(
        default=False,
        help_text="Whether the poster frame file has been written",
    )

    # Reference tracking
    refs = models.JSONField(
        default=list,
        blank=True,
        help_text="List of references: service/entity_type/entity_id",
    )

    # User tracking
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_videos",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The unclaimed-media clock. Stamped at upload (an upload starts claimed
    # by nobody), cleared while ``refs`` is non-empty, restamped the moment
    # the LAST ref is detached (``services.apply_ref_sync``) — so the TTL in
    # ``STAPEL_CDN["UNCLAIMED_TTL_HOURS"]`` counts from when the object
    # BECAME unreferenced, never from ``created_at``. ``services.
    # sweep_unclaimed`` reaps rows past it: zero-ref AND expired, bytes+row.
    unreferenced_since = models.DateTimeField(
        null=True,
        blank=True,
        default=timezone.now,
        db_index=True,
        help_text=(
            "When this object last became zero-ref; NULL while anything "
            "references it. The sweep TTL counts from here."
        ),
    )

    class Meta:
        db_table = "cdn_video"
        verbose_name = "Video"
        verbose_name_plural = "Videos"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["file_hash"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["is_processed"]),
        ]
        # Per-owner uniqueness, for the reason spelled out on Image.Meta:
        # owner-scoped dedup means two principals may hold the same bytes.
        constraints = [
            models.UniqueConstraint(
                fields=["file_hash", "uploaded_by"],
                name="cdn_video_hash_owner_unique",
                condition=models.Q(uploaded_by__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["file_hash"],
                name="cdn_video_hash_service_unique",
                condition=models.Q(uploaded_by__isnull=True),
            ),
        ]

    def __str__(self):
        return f"Video: {self.file_hash[:8]}... ({self.original_filename})"

    #: Filename of the derived poster frame, under the video's hash dir.
    POSTER_FILENAME = "poster.webp"

    @property
    def poster_path(self):
        """Filesystem path of the derived poster frame (may not exist yet)."""
        from .metadata import derived_dir

        return os.path.join(
            derived_dir("video", self.file_hash), self.POSTER_FILENAME
        )

    @property
    def poster_url(self):
        """URL of the poster frame, or ``None`` while none has been written.

        Deliberately not a bare derived string: every URL in this package
        that was computed from ``<hash>`` alone shipped well-formed in a 201
        and 404'd until a worker caught up. ``has_poster`` is the fact; this
        property refuses to name a file that fact says does not exist.
        """
        from .metadata import derived_url

        if not self.has_poster:
            return None
        return derived_url("video", self.file_hash, self.POSTER_FILENAME)

    @staticmethod
    def calculate_file_hash(file):
        """Calculate SHA-256 hash of a file."""
        hash_sha256 = hashlib.sha256()
        for chunk in file.chunks():
            hash_sha256.update(chunk)
        file.seek(0)  # Reset file pointer after reading
        return hash_sha256.hexdigest()

    def save(self, *args, **kwargs):
        """Override save to automatically extract metadata from uploaded file."""
        import os

        # Only process if this is a new object and we have an original file
        if not self.pk and self.original:
            # Calculate file hash
            if not self.file_hash:
                self.file_hash = self.calculate_file_hash(self.original)

            # Get filename and extension
            if not self.original_filename:
                self.original_filename = self.original.name

            if not self.file_extension:
                self.file_extension = os.path.splitext(self.original.name)[1].lower()

            # Get file size
            if not self.original_size:
                self.original_size = self.original.size

            # TODO: Extract video dimensions and duration with ffmpeg

        super().save(*args, **kwargs)


class File(models.Model):
    """Model for storing generic files (documents, archives, etc.)."""

    # File identification
    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of the original file",
    )
    original_filename = models.CharField(max_length=255)
    file_extension = models.CharField(max_length=10)
    mime_type = models.CharField(max_length=100, blank=True, default="")

    # Original file and metadata
    original = models.FileField(
        upload_to=file_upload_path,
        max_length=500,
        storage=cdn_storage,
        help_text="Original uploaded file",
    )
    original_size = models.BigIntegerField(help_text="File size in bytes")

    # Reference tracking
    refs = models.JSONField(
        default=list,
        blank=True,
        help_text="List of references: service/entity_type/entity_id",
    )

    # User tracking
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_files",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The unclaimed-media clock. Stamped at upload (an upload starts claimed
    # by nobody), cleared while ``refs`` is non-empty, restamped the moment
    # the LAST ref is detached (``services.apply_ref_sync``) — so the TTL in
    # ``STAPEL_CDN["UNCLAIMED_TTL_HOURS"]`` counts from when the object
    # BECAME unreferenced, never from ``created_at``. ``services.
    # sweep_unclaimed`` reaps rows past it: zero-ref AND expired, bytes+row.
    unreferenced_since = models.DateTimeField(
        null=True,
        blank=True,
        default=timezone.now,
        db_index=True,
        help_text=(
            "When this object last became zero-ref; NULL while anything "
            "references it. The sweep TTL counts from here."
        ),
    )

    class Meta:
        db_table = "cdn_file"
        verbose_name = "File"
        verbose_name_plural = "Files"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["file_hash"]),
            models.Index(fields=["created_at"]),
        ]
        # Per-owner uniqueness, for the reason spelled out on Image.Meta.
        constraints = [
            models.UniqueConstraint(
                fields=["file_hash", "uploaded_by"],
                name="cdn_file_hash_owner_unique",
                condition=models.Q(uploaded_by__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["file_hash"],
                name="cdn_file_hash_service_unique",
                condition=models.Q(uploaded_by__isnull=True),
            ),
        ]

    def __str__(self):
        return f"File: {self.file_hash[:8]}... ({self.original_filename})"

    @staticmethod
    def calculate_file_hash(file):
        """Calculate SHA-256 hash of a file."""
        hash_sha256 = hashlib.sha256()
        for chunk in file.chunks():
            hash_sha256.update(chunk)
        file.seek(0)
        return hash_sha256.hexdigest()

    def save(self, *args, **kwargs):
        """Override save to automatically extract metadata."""
        if not self.pk and self.original:
            if not self.file_hash:
                self.file_hash = self.calculate_file_hash(self.original)
            if not self.original_filename:
                self.original_filename = self.original.name
            if not self.file_extension:
                self.file_extension = os.path.splitext(self.original.name)[1].lower()
            if self.original_size is None:
                self.original_size = self.original.size
        super().save(*args, **kwargs)


class Audio(models.Model):
    """Model for storing audio recordings ("recordings" submodule).

    cdn-modularity.md §7.2/coordinator decision: storage is always
    available (passthrough, like ``File`` — no extra required), separate
    from compression. ``is_compressed`` stays ``False`` until a real
    ffmpeg-audio pipeline replaces the current no-op ``AudioProcessingService``
    stub (see ``services.AudioProcessingService`` — same "documented
    stub, not a promise" pattern as ``VideoProcessingService``); enabling
    compression is an explicit opt-in (``"recordings"`` in
    ``STAPEL_CDN["ENABLED_SUBMODULES"]``) that turns on
    ``checks.check_submodule_binaries``'s ffmpeg probe for this submodule.
    """

    # File identification
    file_hash = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="SHA-256 hash of the original file",
    )
    original_filename = models.CharField(max_length=255)
    file_extension = models.CharField(max_length=10)
    mime_type = models.CharField(max_length=100, blank=True, default="")

    # Original file and metadata
    original = models.FileField(
        upload_to=audio_upload_path,
        max_length=500,
        storage=cdn_storage,
        help_text="Original uploaded audio recording (always stored as-is)",
    )
    original_size = models.BigIntegerField(help_text="File size in bytes")
    duration = models.FloatField(null=True, blank=True, help_text="Duration in seconds")

    # Compression status — distinct from Image/Video's is_processed: audio
    # is immediately usable (passthrough) the moment it's stored, so this
    # tracks whether the (optional, ffmpeg-gated) compression pass has run,
    # not whether the recording is "ready".
    is_compressed = models.BooleanField(
        default=False,
        help_text="Whether ffmpeg-audio compression has run (opt-in submodule)",
    )

    # The rendered waveform strip as a data URI — what a voice message shows
    # in a chat bubble, alongside `duration`. Produced by ffmpeg's
    # showwavespic filter and re-encoded to WebP within
    # STAPEL_CDN["MICRO_PREVIEW_MAX_BYTES"]; empty means "not generated" or
    # "refused", with meta_reason naming which.
    preview_b64 = models.TextField(
        blank=True,
        default="",
        help_text="Inline waveform strip: data:image/webp;base64,...",
    )
    meta_reason = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "Named reason the render metadata is incomplete "
            "(stapel_cdn.metadata.REASONS); empty when nothing degraded."
        ),
    )

    # Reference tracking
    refs = models.JSONField(
        default=list,
        blank=True,
        help_text="List of references: service/entity_type/entity_id",
    )

    # User tracking
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_audio",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The unclaimed-media clock. Stamped at upload (an upload starts claimed
    # by nobody), cleared while ``refs`` is non-empty, restamped the moment
    # the LAST ref is detached (``services.apply_ref_sync``) — so the TTL in
    # ``STAPEL_CDN["UNCLAIMED_TTL_HOURS"]`` counts from when the object
    # BECAME unreferenced, never from ``created_at``. ``services.
    # sweep_unclaimed`` reaps rows past it: zero-ref AND expired, bytes+row.
    unreferenced_since = models.DateTimeField(
        null=True,
        blank=True,
        default=timezone.now,
        db_index=True,
        help_text=(
            "When this object last became zero-ref; NULL while anything "
            "references it. The sweep TTL counts from here."
        ),
    )

    class Meta:
        db_table = "cdn_audio"
        verbose_name = "Audio"
        verbose_name_plural = "Audio"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["file_hash"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"Audio: {self.file_hash[:8]}... ({self.original_filename})"

    @staticmethod
    def calculate_file_hash(file):
        """Calculate SHA-256 hash of a file."""
        hash_sha256 = hashlib.sha256()
        for chunk in file.chunks():
            hash_sha256.update(chunk)
        file.seek(0)
        return hash_sha256.hexdigest()

    def save(self, *args, **kwargs):
        """Override save to automatically extract metadata. Passthrough —
        no processing is required for storage to be usable (cdn-modularity.md
        §7.2: "recordings: always storable")."""
        if not self.pk and self.original:
            if not self.file_hash:
                self.file_hash = self.calculate_file_hash(self.original)
            if not self.original_filename:
                self.original_filename = self.original.name
            if not self.file_extension:
                self.file_extension = os.path.splitext(self.original.name)[1].lower()
            if self.original_size is None:
                self.original_size = self.original.size
        super().save(*args, **kwargs)


# Signals for automatic variant generation
@receiver(post_save, sender=Image)
def generate_image_variants_on_save(sender, instance, created, **kwargs):
    """
    Trigger image variant generation when an Image is created.

    Async only: falling back to synchronous processing would run the full
    pyvips pipeline inside the upload request whenever the broker is down —
    a trivial CPU DoS. Unprocessed images are picked up later by the
    ``retry_unprocessed`` management command.
    """
    if created and instance.original and not instance.is_processed:
        try:
            from .tasks import process_image_async

            process_image_async.delay(instance.id)
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                "Could not enqueue processing for image %s (broker down?): %s. "
                "Run `manage.py retry_unprocessed` to pick it up.",
                instance.id, e,
            )


@receiver(post_save, sender=Video)
def generate_video_variants_on_save(sender, instance, created, **kwargs):
    """
    Automatically generate video variants when a Video is created.
    Uses post_save signal to ensure the original file is saved first.
    """
    # Only process if:
    # 1. It's a new instance (created=True)
    # 2. The original file exists
    # 3. Variants haven't been generated yet
    if created and instance.original and not instance.is_processed:
        from .services import VideoProcessingService

        # Process in a try-except to avoid breaking the save operation
        try:
            VideoProcessingService.process_video(instance)
        except Exception as e:
            # Log the error but don't break the save
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                f"Failed to generate variants for video {instance.file_hash}: {str(e)}"
            )


@receiver(post_save, sender=Audio)
def extract_audio_metadata_on_save(sender, instance, created, **kwargs):
    """Queue the duration + waveform pass for a new recording.

    Async only, exactly like the image path: ffmpeg inside the upload
    request would transcode on a request thread, and a recording is already
    usable without this (passthrough storage, cdn-modularity.md §7.2). A row
    the broker never accepted is picked up by ``retry_unprocessed``.
    """
    if created and instance.original and not instance.preview_b64:
        try:
            from .tasks import process_audio_async

            process_audio_async.delay(instance.id)
        except Exception as exc:
            logger.error(
                "Could not enqueue metadata extraction for audio %s (broker "
                "down?): %s. `manage.py cdn_backfill_media_meta` picks it up.",
                instance.id, exc,
            )
