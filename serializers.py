"""
Serializers for stapel-cdn service.
"""

from rest_framework import serializers
from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    FileExistsResponse,
    FileUploadResponse,
    ImageUploadResponse,
    RefSyncRequest,
    RefSyncResponse,
    VideoUploadResponse,
)
from .errors import ERR_400_FILE_TYPE_NOT_ALLOWED
from .models import File, Image, Video


class ImageSerializer(serializers.ModelSerializer):
    """
    Serializer for Image model.

    Returns complete image information including all generated variants.
    Variants are auto-generated in WebP format for optimal compression.
    """

    prefix = serializers.SerializerMethodField(help_text="URL prefix: <type>/<hash>")
    original_url = serializers.SerializerMethodField(
        help_text="URL to original uploaded image"
    )
    variant_16_url = serializers.ReadOnlyField(help_text="16px thumbnail (WebP)")
    variant_32_url = serializers.ReadOnlyField(help_text="32px thumbnail (WebP)")
    variant_64_url = serializers.ReadOnlyField(help_text="64px thumbnail (WebP)")
    variant_120_url = serializers.ReadOnlyField(help_text="120px thumbnail (WebP)")
    variant_160_url = serializers.ReadOnlyField(help_text="160px preview (WebP)")
    variant_240_url = serializers.ReadOnlyField(help_text="240px preview (WebP)")
    variant_480_url = serializers.ReadOnlyField(help_text="480px medium (WebP)")
    variant_720_url = serializers.ReadOnlyField(help_text="720px HD (WebP)")
    variant_720_jpg_url = serializers.ReadOnlyField(
        help_text="720px HD (JPEG fallback)"
    )
    variant_1080_url = serializers.ReadOnlyField(help_text="1080px Full HD (WebP)")
    variant_1440_url = serializers.ReadOnlyField(help_text="1440px 2K (WebP)")
    variant_2160_url = serializers.ReadOnlyField(help_text="2160px 4K (WebP)")
    uploaded_by_username = serializers.CharField(
        source="uploaded_by.username", read_only=True
    )

    class Meta:
        model = Image
        fields = [
            "id",
            "file_hash",
            "original_filename",
            "file_extension",
            "type",
            "prefix",
            "original_width",
            "original_height",
            "original_size",
            "original_url",
            "variant_16_url",
            "variant_32_url",
            "variant_64_url",
            "variant_120_url",
            "variant_160_url",
            "variant_240_url",
            "variant_480_url",
            "variant_720_url",
            "variant_720_jpg_url",
            "variant_1080_url",
            "variant_1440_url",
            "variant_2160_url",
            "is_processed",
            "uploaded_by",
            "uploaded_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "file_hash",
            "original_width",
            "original_height",
            "original_size",
            "type",
            "is_processed",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    def get_prefix(self, obj):
        return f"{obj.type}/{obj.file_hash}"

    def get_original_url(self, obj):
        return obj.original.url if obj.original else None


class VideoSerializer(serializers.ModelSerializer):
    """Serializer for Video model."""

    original_url = serializers.SerializerMethodField()
    variant_16p_url = serializers.SerializerMethodField()
    variant_32p_url = serializers.SerializerMethodField()
    variant_240p_url = serializers.SerializerMethodField()
    variant_480p_url = serializers.SerializerMethodField()
    variant_720p_url = serializers.SerializerMethodField()
    variant_1080p_url = serializers.SerializerMethodField()
    variant_2160p_url = serializers.SerializerMethodField()
    uploaded_by_username = serializers.CharField(
        source="uploaded_by.username", read_only=True
    )

    class Meta:
        model = Video
        fields = [
            "id",
            "file_hash",
            "original_filename",
            "file_extension",
            "original_width",
            "original_height",
            "original_size",
            "duration",
            "original_url",
            "variant_16p_url",
            "variant_32p_url",
            "variant_240p_url",
            "variant_480p_url",
            "variant_720p_url",
            "variant_1080p_url",
            "variant_2160p_url",
            "is_processed",
            "uploaded_by",
            "uploaded_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "file_hash",
            "original_width",
            "original_height",
            "original_size",
            "duration",
            "is_processed",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    def get_original_url(self, obj):
        return obj.original.url if obj.original else None

    def get_variant_16p_url(self, obj):
        return obj.variant_16.url if obj.variant_16 else None

    def get_variant_32p_url(self, obj):
        return obj.variant_32.url if obj.variant_32 else None

    def get_variant_240p_url(self, obj):
        return obj.variant_240.url if obj.variant_240 else None

    def get_variant_480p_url(self, obj):
        return obj.variant_480.url if obj.variant_480 else None

    def get_variant_720p_url(self, obj):
        return obj.variant_720.url if obj.variant_720 else None

    def get_variant_1080p_url(self, obj):
        return obj.variant_1080.url if obj.variant_1080 else None

    def get_variant_2160p_url(self, obj):
        return obj.variant_2160.url if obj.variant_2160 else None


class FileUploadSerializer(serializers.Serializer):
    """
    Serializer for file upload requests.

    Upload a file using multipart/form-data with the 'file' field.
    """

    file = serializers.FileField(
        help_text="The file to upload. Images: jpg, jpeg, png, gif, webp, bmp, heic, heif. Videos: mp4, webm, mov, avi, mkv."
    )

    def validate_file(self, value):
        """Validate the uploaded file."""
        from django.conf import settings

        # Get file extension
        file_extension = value.name.split(".")[-1].lower()

        # Check if it's an allowed extension
        allowed_extensions = (
            settings.CDN_ALLOWED_IMAGE_EXTENSIONS
            + settings.CDN_ALLOWED_VIDEO_EXTENSIONS
        )

        if f".{file_extension}" not in allowed_extensions:
            raise StapelValidationError(ERR_400_FILE_TYPE_NOT_ALLOWED)

        return value


class FileExistsSerializer(serializers.Serializer):
    """Serializer for file existence check by hash."""

    file_hash = serializers.CharField(
        max_length=64,
        required=True,
        help_text="SHA-256 hash of the file content (64 hex characters)",
    )


# =============================================================================
# Response Serializers for OpenAPI Documentation
# =============================================================================


class ImageUploadResponseSerializer(StapelDataclassSerializer):
    """Response for successful image upload."""

    image = ImageSerializer(help_text="Uploaded image details with variant URLs")

    class Meta:
        dataclass = ImageUploadResponse


class VideoUploadResponseSerializer(StapelDataclassSerializer):
    """Response for successful video upload."""

    video = VideoSerializer(help_text="Uploaded video details with variant URLs")

    class Meta:
        dataclass = VideoUploadResponse


class FileExistsResponseSerializer(StapelDataclassSerializer):
    """Response for file existence check."""

    file = serializers.JSONField(
        allow_null=True,
        help_text="File details (ImageSerializer or VideoSerializer) if found, null otherwise",
    )

    class Meta:
        dataclass = FileExistsResponse


class FileModelSerializer(serializers.ModelSerializer):
    """Serializer for File model."""

    prefix = serializers.SerializerMethodField(help_text="URL prefix: file/<hash>")
    original_url = serializers.SerializerMethodField(
        help_text="URL to original uploaded file"
    )
    uploaded_by_username = serializers.CharField(
        source="uploaded_by.username", read_only=True
    )

    class Meta:
        model = File
        fields = [
            "id",
            "file_hash",
            "original_filename",
            "file_extension",
            "mime_type",
            "original_size",
            "prefix",
            "original_url",
            "refs",
            "uploaded_by",
            "uploaded_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "file_hash",
            "original_size",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    def get_prefix(self, obj):
        return f"file/{obj.file_hash}"

    def get_original_url(self, obj):
        return obj.original.url if obj.original else None


class RefSyncRequestSerializer(StapelDataclassSerializer):
    """Serializer for ref sync request."""

    class Meta:
        dataclass = RefSyncRequest


class RefSyncResponseSerializer(StapelDataclassSerializer):
    """Serializer for ref sync response."""

    class Meta:
        dataclass = RefSyncResponse


class FileUploadResponseSerializer(StapelDataclassSerializer):
    """Response for successful file upload."""

    file = FileModelSerializer(help_text="Uploaded file details")

    class Meta:
        dataclass = FileUploadResponse
