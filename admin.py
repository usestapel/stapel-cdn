"""
Admin configuration for stapel-cdn service.
"""

import logging
import os

from django.conf import settings
from django.contrib import admin, messages
from django.utils.html import format_html

from .forms import ImageAdminForm
from .models import File, Image, Video

logger = logging.getLogger(__name__)


def format_file_size(size_bytes):
    """Format file size in human-readable format."""
    if size_bytes is None:
        return ""
    for unit in ["B", "KB", "MB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}GB"


class OrphanFilter(admin.SimpleListFilter):
    """Filter for orphan media (empty refs)."""

    title = "orphan status"
    parameter_name = "orphan"

    def lookups(self, request, model_admin):
        return [("yes", "Orphans (no refs)"), ("no", "Referenced")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(refs=[])
        if self.value() == "no":
            return queryset.exclude(refs=[])
        return queryset


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    """Admin interface for Image model."""

    form = ImageAdminForm
    list_display = [
        "preview_thumbnail",
        "file_hash_short",
        "type",
        "original_filename",
        "dimensions",
        "file_size_display",
        "refs_count",
        "is_processed",
        "uploaded_by",
        "created_at",
    ]
    list_filter = ["type", "is_processed", OrphanFilter, "created_at"]
    search_fields = ["file_hash", "original_filename", "uploaded_by__username"]
    actions = ["regenerate_variants_async", "regenerate_sync"]
    readonly_fields = [
        "file_hash",
        "original_filename",
        "file_extension",
        "original_width",
        "original_height",
        "original_size",
        "original_link",
        "variant_16_link",
        "variant_32_link",
        "variant_64_link",
        "variant_120_link",
        "variant_160_link",
        "variant_240_link",
        "variant_480_link",
        "variant_560_link",
        "variant_720_link",
        "variant_1080_link",
        "is_processed",
        "processing_log",
        "refs",
        "uploaded_by",
        "created_at",
        "updated_at",
    ]

    fieldsets = (
        ("Original File", {"fields": ("original", "original_link")}),
        (
            "File Information",
            {
                "fields": (
                    "file_hash",
                    "original_filename",
                    "file_extension",
                    "original_width",
                    "original_height",
                    "original_size",
                )
            },
        ),
        (
            "Variant URLs",
            {
                "fields": (
                    "variant_1080_link",
                    "variant_720_link",
                    "variant_560_link",
                    "variant_480_link",
                    "variant_240_link",
                    "variant_160_link",
                    "variant_120_link",
                    "variant_64_link",
                    "variant_32_link",
                    "variant_16_link",
                )
            },
        ),
        ("References", {"fields": ("refs",)}),
        ("Processing Status", {"fields": ("is_processed", "processing_log")}),
        ("User Information", {"fields": ("uploaded_by",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        """Automatically set uploaded_by to current user if not set."""
        if not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="")
    def preview_thumbnail(self, obj):
        """Display small preview thumbnail."""
        if obj.is_processed:
            return format_html(
                '<img src="{}" style="max-height: 40px; max-width: 60px;" />',
                obj.variant_64_url,
            )
        return "-"

    @admin.display(description="Hash")
    def file_hash_short(self, obj):
        """Display shortened file hash."""
        return f"{obj.file_hash[:8]}..."

    @admin.display(description="Dimensions")
    def dimensions(self, obj):
        """Display image dimensions."""
        return f"{obj.original_width}x{obj.original_height}"

    @admin.display(description="Size")
    def file_size_display(self, obj):
        """Display file size in human-readable format."""
        size = obj.original_size
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    @admin.display(description="Refs")
    def refs_count(self, obj):
        count = len(obj.refs) if obj.refs else 0
        return count or "-"

    def _get_variant_file_size(self, obj, variant_name):
        """Get file size of a variant (filename derived from the URL —
        thumbnails are ``{tier}.webp``, previews ``{tier}w.webp``)."""
        try:
            filename = os.path.basename(obj.get_variant_url(variant_name))
            file_path = os.path.join(
                settings.MEDIA_ROOT, obj.type, obj.file_hash, filename
            )
            if os.path.exists(file_path):
                return os.path.getsize(file_path)
        except Exception:
            pass
        return None

    def _variant_link_with_size(self, obj, variant_name):
        """Display variant URL with file size."""
        url = obj.get_variant_url(variant_name)
        size = self._get_variant_file_size(obj, variant_name)
        size_str = f" ({format_file_size(size)})" if size else ""
        return format_html('<a href="{}" target="_blank">{}</a>{}', url, url, size_str)

    @admin.display(description="Original URL")
    def original_link(self, obj):
        """Display original file URL with size."""
        if obj.original:
            url = obj.original.url
            size_str = f" ({format_file_size(obj.original_size)})"
            return format_html(
                '<a href="{}" target="_blank">{}</a>{}', url, url, size_str
            )
        return "-"

    @admin.display(description="16px")
    def variant_16_link(self, obj):
        return self._variant_link_with_size(obj, "16")

    @admin.display(description="32px")
    def variant_32_link(self, obj):
        return self._variant_link_with_size(obj, "32")

    @admin.display(description="64px")
    def variant_64_link(self, obj):
        return self._variant_link_with_size(obj, "64")

    @admin.display(description="120px")
    def variant_120_link(self, obj):
        return self._variant_link_with_size(obj, "120")

    @admin.display(description="160px")
    def variant_160_link(self, obj):
        return self._variant_link_with_size(obj, "160")

    @admin.display(description="240px")
    def variant_240_link(self, obj):
        return self._variant_link_with_size(obj, "240")

    @admin.display(description="480px")
    def variant_480_link(self, obj):
        return self._variant_link_with_size(obj, "480")

    @admin.display(description="560px")
    def variant_560_link(self, obj):
        return self._variant_link_with_size(obj, "560")

    @admin.display(description="720px")
    def variant_720_link(self, obj):
        return self._variant_link_with_size(obj, "720")

    @admin.display(description="1080px")
    def variant_1080_link(self, obj):
        return self._variant_link_with_size(obj, "1080")

    @admin.action(description="Regenerate variants (Async via Celery)")
    def regenerate_variants_async(self, request, queryset):
        """Regenerate image variants asynchronously using Celery."""
        from .tasks import process_image_async

        success_count = 0
        error_count = 0

        for image in queryset:
            try:
                # Reset processed flag to allow reprocessing
                image.is_processed = False
                image.processing_log = ""
                image.save(update_fields=["is_processed", "processing_log"])

                # Queue async task
                process_image_async.delay(image.id)
                success_count += 1
            except Exception as e:
                error_count += 1
                self.message_user(
                    request,
                    f"Error queuing image {image.file_hash[:8]}: {str(e)}",
                    level=messages.ERROR,
                )

        if success_count > 0:
            self.message_user(
                request,
                f"Successfully queued {success_count} image(s) for regeneration.",
                level=messages.SUCCESS,
            )
        if error_count > 0:
            self.message_user(
                request,
                f"Failed to queue {error_count} image(s).",
                level=messages.WARNING,
            )

    @admin.action(description="Regenerate sync (with timing)")
    def regenerate_sync(self, request, queryset):
        """Regenerate image variants synchronously with detailed timing."""
        import time

        from .services import ImageProcessingService

        for image in queryset:
            try:
                start = time.perf_counter()
                ImageProcessingService.process_image(image)
                total_time = int((time.perf_counter() - start) * 1000)

                # Reload to get processing_log
                image.refresh_from_db()

                # Show summary with breakdown
                self.message_user(
                    request,
                    f"{image.file_hash[:8]} ({image.original_width}x{image.original_height}): {total_time}ms\n{image.processing_log}",
                    level=messages.SUCCESS,
                )
            except Exception as e:
                import traceback

                self.message_user(
                    request,
                    f"{image.file_hash[:8]}: Error - {e}\n{traceback.format_exc()}",
                    level=messages.ERROR,
                )


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    """Admin interface for Video model."""

    list_display = [
        "file_hash_short",
        "original_filename",
        "dimensions",
        "duration_display",
        "file_size_display",
        "refs_count",
        "is_processed",
        "uploaded_by",
        "created_at",
    ]
    list_filter = ["is_processed", OrphanFilter, "created_at"]
    search_fields = ["file_hash", "original_filename", "uploaded_by__username"]
    readonly_fields = [
        "file_hash",
        "original_filename",
        "file_extension",
        "original_width",
        "original_height",
        "original_size",
        "duration",
        "variant_16",
        "variant_32",
        "variant_64",
        "variant_160",
        "variant_240",
        "variant_480",
        "variant_720",
        "variant_720_jpg",
        "variant_1080",
        "variant_1440",
        "variant_2160",
        "is_processed",
        "refs",
        "uploaded_by",
        "created_at",
        "updated_at",
    ]

    fieldsets = (
        ("Original File", {"fields": ("original",)}),
        (
            "File Information",
            {
                "fields": (
                    "file_hash",
                    "original_filename",
                    "file_extension",
                    "original_width",
                    "original_height",
                    "original_size",
                    "duration",
                )
            },
        ),
        (
            "Variants",
            {
                "fields": (
                    "variant_16",
                    "variant_32",
                    "variant_64",
                    "variant_160",
                    "variant_240",
                    "variant_480",
                    "variant_720",
                    "variant_720_jpg",
                    "variant_1080",
                    "variant_1440",
                    "variant_2160",
                )
            },
        ),
        ("References", {"fields": ("refs",)}),
        ("Processing Status", {"fields": ("is_processed",)}),
        ("User Information", {"fields": ("uploaded_by",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        """Automatically set uploaded_by to current user if not set."""
        if not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Hash")
    def file_hash_short(self, obj):
        """Display shortened file hash."""
        return f"{obj.file_hash[:8]}..."

    @admin.display(description="Dimensions")
    def dimensions(self, obj):
        """Display video dimensions."""
        if obj.original_width and obj.original_height:
            return f"{obj.original_width}x{obj.original_height}"
        return "N/A"

    @admin.display(description="Refs")
    def refs_count(self, obj):
        count = len(obj.refs) if obj.refs else 0
        return count or "-"

    @admin.display(description="Duration")
    def duration_display(self, obj):
        """Display duration in human-readable format."""
        if obj.duration:
            minutes, seconds = divmod(int(obj.duration), 60)
            return f"{minutes}:{seconds:02d}"
        return "N/A"

    @admin.display(description="Size")
    def file_size_display(self, obj):
        """Display file size in human-readable format."""
        size = obj.original_size
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    """Admin interface for File model."""

    list_display = [
        "file_hash_short",
        "original_filename",
        "file_extension",
        "file_size_display",
        "refs_count",
        "uploaded_by",
        "created_at",
    ]
    list_filter = [OrphanFilter, "created_at"]
    search_fields = ["file_hash", "original_filename", "uploaded_by__username"]
    readonly_fields = [
        "file_hash",
        "original_filename",
        "file_extension",
        "mime_type",
        "original_size",
        "original_link",
        "refs",
        "uploaded_by",
        "created_at",
        "updated_at",
    ]

    fieldsets = (
        ("Original File", {"fields": ("original", "original_link")}),
        (
            "File Information",
            {
                "fields": (
                    "file_hash",
                    "original_filename",
                    "file_extension",
                    "mime_type",
                    "original_size",
                )
            },
        ),
        ("References", {"fields": ("refs",)}),
        ("User Information", {"fields": ("uploaded_by",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        if not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Hash")
    def file_hash_short(self, obj):
        return f"{obj.file_hash[:8]}..."

    @admin.display(description="Size")
    def file_size_display(self, obj):
        size = obj.original_size
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    @admin.display(description="Refs")
    def refs_count(self, obj):
        count = len(obj.refs) if obj.refs else 0
        return count or "-"

    @admin.display(description="Original URL")
    def original_link(self, obj):
        if obj.original:
            url = obj.original.url
            size_str = f" ({format_file_size(obj.original_size)})"
            return format_html(
                '<a href="{}" target="_blank">{}</a>{}', url, url, size_str
            )
        return "-"
