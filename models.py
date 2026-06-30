"""
Models for stapel-cdn service.
Uses pyvips for all image processing (supports JPEG, PNG, HEIC, etc.)
"""

import hashlib
import os

from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from stapel_core.django.users.models import User

from .storage import cdn_storage


def image_upload_path(instance, filename):
    """Generate upload path for images: <type>/<hash>/<filename>"""
    return f"{instance.type}/{instance.file_hash}/{filename}"


def video_upload_path(instance, filename):
    """Generate upload path for videos: video/<hash>/<filename>"""
    return f"video/{instance.file_hash}/{filename}"


def file_upload_path(instance, filename):
    """Generate upload path for files: file/<hash>/<filename>"""
    return f"file/{instance.file_hash}/{filename}"


class ImageType(models.TextChoices):
    """Types of images (user-uploaded content with watermarks)."""

    PRODUCT = "product", "Product"
    AVATAR = "avatar", "Avatar"


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
        choices=ImageType,
        default=ImageType.PRODUCT,
        help_text="Type of image: product or avatar",
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
    processing_log = models.TextField(
        blank=True, default="", help_text="Log of processing operations"
    )

    # Reference tracking
    refs = models.JSONField(
        default=list,
        blank=True,
        help_text="List of references: service/entity_type/entity_id",
    )

    # User tracking
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="uploaded_images"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
            models.UniqueConstraint(
                fields=["file_hash", "type"], name="cdn_image_hash_type_unique"
            ),
        ]

    def __str__(self):
        return f"Image: {self.file_hash[:8]}... ({self.original_filename})"

    def get_variant_url(self, variant_name):
        """Get URL for a specific variant. All WebP."""
        from django.conf import settings

        return f"{settings.MEDIA_URL}{self.type}/{self.file_hash}/{variant_name}.webp"

    @property
    def variant_16_url(self):
        return self.get_variant_url("16")

    @property
    def variant_32_url(self):
        return self.get_variant_url("32")

    @property
    def variant_64_url(self):
        return self.get_variant_url("64")

    @property
    def variant_120_url(self):
        return self.get_variant_url("120")

    @property
    def variant_160_url(self):
        return self.get_variant_url("160")

    @property
    def variant_240_url(self):
        return self.get_variant_url("240")

    @property
    def variant_480_url(self):
        return self.get_variant_url("480")

    @property
    def variant_720_url(self):
        return self.get_variant_url("720")

    @property
    def variant_720_jpg_url(self):
        """Get URL for 720p JPEG variant (legacy browser compatibility)."""
        from django.conf import settings

        return f"{settings.MEDIA_URL}{self.type}/{self.file_hash}/720.jpg"

    @property
    def variant_1080_url(self):
        return self.get_variant_url("1080")

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

            # Get image dimensions using pyvips (supports HEIC/HEIF)
            if not self.original_width or not self.original_height:
                try:
                    import pyvips

                    img = pyvips.Image.new_from_file(
                        self.original.path, access="sequential"
                    )
                    self.original_width = img.width
                    self.original_height = img.height
                except Exception:
                    # Fallback: will be updated by process_image later
                    if not self.original_width:
                        self.original_width = 1
                    if not self.original_height:
                        self.original_height = 1

        super().save(*args, **kwargs)


class Video(models.Model):
    """Model for storing videos with multiple resolution variants."""

    # File identification
    file_hash = models.CharField(
        max_length=64,
        unique=True,
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
    variant_720_jpg = models.FileField(
        upload_to=video_upload_path,
        max_length=100,
        blank=True,
        null=True,
        help_text="720px height variant (legacy compatibility)",
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

    # Processing status
    is_processed = models.BooleanField(
        default=False, help_text="Whether variants have been generated"
    )

    # Reference tracking
    refs = models.JSONField(
        default=list,
        blank=True,
        help_text="List of references: service/entity_type/entity_id",
    )

    # User tracking
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="uploaded_videos"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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

    def __str__(self):
        return f"Video: {self.file_hash[:8]}... ({self.original_filename})"

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
        unique=True,
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
        User, on_delete=models.SET_NULL, null=True, related_name="uploaded_files"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cdn_file"
        verbose_name = "File"
        verbose_name_plural = "Files"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["file_hash"]),
            models.Index(fields=["created_at"]),
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


# Signals for automatic variant generation
@receiver(post_save, sender=Image)
def generate_image_variants_on_save(sender, instance, created, **kwargs):
    """
    Trigger image variant generation when an Image is created.
    Try async first, fallback to sync if Celery is not available.
    """
    if created and instance.original and not instance.is_processed:
        try:
            from .tasks import process_image_async

            process_image_async.delay(instance.id)
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                f"Celery not available, processing image {instance.id} synchronously: {e}"
            )

            from .services import ImageProcessingService

            try:
                ImageProcessingService.process_image(instance)
            except Exception as proc_error:
                logger.error(f"Failed to process image {instance.id}: {proc_error}")


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
