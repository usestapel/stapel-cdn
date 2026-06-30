"""
Views for stapel-cdn service.
"""

import logging
import os

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from stapel_core.django.api.errors import (
    IronErrorResponse,
    IronErrorSerializer,
    IronResponse,
    error_500_internal,
)
from stapel_core.django.api.permissions import IsServiceRequest, IsStaffUser

from stapel_cdn.errors import (
    ERR_400_FILE_HASH_REQUIRED,
    ERR_400_INVALID_FORMAT,
    ERR_400_INVALID_IMAGE_TYPE,
    ERR_400_MISSING_FIELDS,
    ERR_400_NO_FILE,
    ERR_404_NO_IMAGES,
)

from .dto import (
    FileExistsResponse,
    ImageUploadResponse,
    RefSyncResponse,
    VideoUploadResponse,
)
from .dto import (
    FileUploadResponse as FileUploadResponseDTO,
)
from .models import File, Image, ImageType, Video
from .serializers import (
    FileExistsResponseSerializer,
    FileExistsSerializer,
    FileModelSerializer,
    FileUploadResponseSerializer,
    FileUploadSerializer,
    ImageSerializer,
    ImageUploadResponseSerializer,
    RefSyncRequestSerializer,
    RefSyncResponseSerializer,
    VideoSerializer,
    VideoUploadResponseSerializer,
)

logger = logging.getLogger(__name__)


@extend_schema(tags=["Images"])
class ImageUploadView(APIView):
    """API endpoint for uploading images."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="upload_image",
        summary="Upload an image",
        description="""Upload an image file for processing.

**Supported formats:** JPEG, PNG, GIF, WebP, BMP, HEIC, HEIF

**What happens on upload:**
1. File hash (SHA-256) is calculated for deduplication
2. If file already exists, returns existing image data (200 OK)
3. If new file, creates image record and generates variants via background task
4. Variants are generated in WebP format at multiple resolutions

**Generated variants:**
- 16px, 32px, 64px - thumbnails
- 160px, 240px - previews
- 480px, 720px - medium
- 1080px, 1440px, 2160px - high resolution
- 720px JPEG - fallback for browsers without WebP support

**Request format:** `multipart/form-data` with `file` field

**Maximum file size:** 100MB
""",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "Image file to upload (jpg, png, gif, webp, bmp, heic, heif)",
                    }
                },
                "required": ["file"],
            }
        },
        responses={
            201: OpenApiExample(
                name="Image uploaded",
                response_only=True,
                value={
                    "message": "Image uploaded successfully",
                    "image": {
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "file_hash": "a1b2c3d4e5f6...",
                        "original_filename": "photo.jpg",
                        "file_extension": ".jpg",
                        "original_width": 1920,
                        "original_height": 1080,
                        "original_size": 2048576,
                        "original_url": "/media/cdn/images/original/a1b2c3d4.jpg",
                        "variant_720_url": "/media/cdn/images/720/a1b2c3d4.webp",
                        "is_processed": False,
                    },
                },
            ),
            200: OpenApiExample(
                name="Image already exists",
                response_only=True,
                value={
                    "message": "Image already exists",
                    "image": {"id": "...", "file_hash": "..."},
                },
            ),
            400: IronErrorSerializer,
            401: IronErrorSerializer,
            500: IronErrorSerializer,
        },
        examples=[
            OpenApiExample(
                name="Image uploaded successfully",
                response_only=True,
                status_codes=["201"],
                value={
                    "message": "Image uploaded successfully",
                    "image": {
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "file_hash": "a1b2c3d4e5f6789...",
                        "original_filename": "photo.jpg",
                        "original_width": 1920,
                        "original_height": 1080,
                        "variant_720_url": "/media/cdn/images/720/a1b2c3d4.webp",
                    },
                },
            ),
        ],
    )
    def post(self, request):
        """
        Upload an image file.
        Variants are automatically generated via Django signals.
        """
        serializer = FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]

        # Calculate file hash
        file_hash = Image.calculate_file_hash(uploaded_file)

        # Check if file already exists (default type is 'product')
        existing_image = Image.objects.filter(
            file_hash=file_hash, type=ImageType.PRODUCT
        ).first()
        if existing_image:
            return IronResponse(
                ImageUploadResponseSerializer(
                    ImageUploadResponse(
                        message="Image already exists", image=existing_image
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Get file extension
        file_extension = os.path.splitext(uploaded_file.name)[1].lower()

        # Check if it's an image
        if file_extension not in settings.CDN_ALLOWED_IMAGE_EXTENSIONS:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        # Create Image record
        # Dimensions are calculated in model.save() via pyvips
        # Variants will be automatically generated via post_save signal
        try:
            image = Image.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                type=ImageType.PRODUCT,
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return IronResponse(
            ImageUploadResponseSerializer(
                ImageUploadResponse(message="Image uploaded successfully", image=image)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Videos"])
class VideoUploadView(APIView):
    """API endpoint for uploading videos."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="upload_video",
        summary="Upload a video",
        description="""Upload a video file for processing.

**Supported formats:** MP4, WebM, MOV, AVI, MKV

**What happens on upload:**
1. File hash (SHA-256) is calculated for deduplication
2. If file already exists, returns existing video data (200 OK)
3. If new file, creates video record
4. Variant generation via FFmpeg (TODO: not yet implemented)

**Planned variants:**
- 16p, 32p - animated thumbnails
- 240p, 480p, 720p, 1080p, 2160p - video resolutions

**Request format:** `multipart/form-data` with `file` field

**Maximum file size:** 100MB
""",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "Video file to upload (mp4, webm, mov, avi, mkv)",
                    }
                },
                "required": ["file"],
            }
        },
        responses={
            201: VideoUploadResponseSerializer,
            200: VideoUploadResponseSerializer,
            400: IronErrorSerializer,
            401: IronErrorSerializer,
            500: IronErrorSerializer,
        },
    )
    def post(self, request):
        """
        Upload a video file.
        Variants will be automatically generated via Django signals (TODO: implement ffmpeg processing).
        """
        serializer = FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]

        # Calculate file hash
        file_hash = Video.calculate_file_hash(uploaded_file)

        # Check if file already exists
        existing_video = Video.objects.filter(file_hash=file_hash).first()
        if existing_video:
            return IronResponse(
                VideoUploadResponseSerializer(
                    VideoUploadResponse(
                        message="Video already exists", video=existing_video
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Get file extension
        file_extension = os.path.splitext(uploaded_file.name)[1].lower()

        # Check if it's a video
        if file_extension not in settings.CDN_ALLOWED_VIDEO_EXTENSIONS:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        # Create Video record
        # Variants will be automatically generated via post_save signal (TODO: implement)
        try:
            video = Video.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return IronResponse(
            VideoUploadResponseSerializer(
                VideoUploadResponse(
                    message="Video uploaded successfully (variant generation not yet implemented)",
                    video=video,
                )
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Files"])
class FileExistsView(APIView):
    """API endpoint for checking if a file exists by hash."""

    permission_classes = [IsAuthenticated | IsServiceRequest]

    @extend_schema(
        operation_id="check_file_exists_get",
        summary="Check if file exists (GET)",
        description="""Check if a file with the given hash already exists in the CDN.

Use this before uploading to avoid duplicate uploads.

**How to calculate hash:**
```python
import hashlib

def calculate_file_hash(file_content: bytes) -> str:
    return hashlib.sha256(file_content).hexdigest()
```

**Response:**
- `exists: true` - file found, returns file details
- `exists: false` - file not found, `type` and `file` are null
""",
        parameters=[
            OpenApiParameter(
                name="file_hash",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="SHA-256 hash of the file (64 hex characters)",
                examples=[
                    OpenApiExample(
                        name="Example hash",
                        value="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    )
                ],
            )
        ],
        responses={
            200: FileExistsResponseSerializer,
            400: IronErrorSerializer,
            401: IronErrorSerializer,
        },
        examples=[
            OpenApiExample(
                name="File found (image)",
                response_only=True,
                status_codes=["200"],
                value={
                    "exists": True,
                    "type": "image",
                    "file": {
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "file_hash": "e3b0c44298fc1c14...",
                        "original_filename": "photo.jpg",
                        "variant_720_url": "/media/cdn/images/720/e3b0c442.webp",
                    },
                },
            ),
            OpenApiExample(
                name="File not found",
                response_only=True,
                status_codes=["200"],
                value={"exists": False, "type": None, "file": None},
            ),
        ],
    )
    def get(self, request):
        """
        Check if a file exists by its hash.
        Query parameter: file_hash
        """
        file_hash = request.query_params.get("file_hash")

        if not file_hash:
            return IronErrorResponse(400, ERR_400_FILE_HASH_REQUIRED)

        # Check if image exists
        image = Image.objects.filter(file_hash=file_hash).first()
        if image:
            return IronResponse(
                FileExistsResponseSerializer(
                    FileExistsResponse(
                        exists=True, type="image", file=ImageSerializer(image).data
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Check if video exists
        video = Video.objects.filter(file_hash=file_hash).first()
        if video:
            return IronResponse(
                FileExistsResponseSerializer(
                    FileExistsResponse(
                        exists=True, type="video", file=VideoSerializer(video).data
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Check if generic file exists
        file_obj = File.objects.filter(file_hash=file_hash).first()
        if file_obj:
            return IronResponse(
                FileExistsResponseSerializer(
                    FileExistsResponse(
                        exists=True,
                        type="file",
                        file=FileModelSerializer(file_obj).data,
                    )
                ),
                status=status.HTTP_200_OK,
            )

        return IronResponse(
            FileExistsResponseSerializer(
                FileExistsResponse(exists=False, type=None, file=None)
            ),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        operation_id="check_file_exists_post",
        summary="Check if file exists (POST)",
        description="""Check if a file with the given hash already exists in the CDN.

Same as GET method but accepts hash in request body.
Useful when hash is very long or contains special characters.
""",
        request=FileExistsSerializer,
        responses={
            200: FileExistsResponseSerializer,
            400: IronErrorSerializer,
            401: IronErrorSerializer,
        },
    )
    def post(self, request):
        """
        Check if a file exists by its hash (POST method).
        Body parameter: file_hash
        """
        serializer = FileExistsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        file_hash = serializer.validated_data["file_hash"]

        # Check if image exists
        image = Image.objects.filter(file_hash=file_hash).first()
        if image:
            return IronResponse(
                FileExistsResponseSerializer(
                    FileExistsResponse(
                        exists=True, type="image", file=ImageSerializer(image).data
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Check if video exists
        video = Video.objects.filter(file_hash=file_hash).first()
        if video:
            return IronResponse(
                FileExistsResponseSerializer(
                    FileExistsResponse(
                        exists=True, type="video", file=VideoSerializer(video).data
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Check if generic file exists
        file_obj = File.objects.filter(file_hash=file_hash).first()
        if file_obj:
            return IronResponse(
                FileExistsResponseSerializer(
                    FileExistsResponse(
                        exists=True,
                        type="file",
                        file=FileModelSerializer(file_obj).data,
                    )
                ),
                status=status.HTTP_200_OK,
            )

        return IronResponse(
            FileExistsResponseSerializer(
                FileExistsResponse(exists=False, type=None, file=None)
            ),
            status=status.HTTP_200_OK,
        )


@extend_schema(tags=["Images"])
class AvatarUploadView(APIView):
    """API endpoint for uploading avatar images."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="upload_avatar",
        summary="Upload an avatar image",
        description="""Upload an avatar image file for processing.

**Same as image upload but sets type to 'avatar'.**

**Supported formats:** JPEG, PNG, GIF, WebP, BMP, HEIC, HEIF

**Maximum file size:** 100MB
""",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "Avatar image file to upload",
                    }
                },
                "required": ["file"],
            }
        },
        responses={
            201: ImageUploadResponseSerializer,
            200: ImageUploadResponseSerializer,
            400: IronErrorSerializer,
            401: IronErrorSerializer,
            500: IronErrorSerializer,
        },
    )
    def post(self, request):
        """Upload an avatar image file. Sets type to 'avatar'."""
        serializer = FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]
        file_hash = Image.calculate_file_hash(uploaded_file)

        # Check for existing avatar with same hash
        existing_image = Image.objects.filter(
            file_hash=file_hash, type=ImageType.AVATAR
        ).first()
        if existing_image:
            return IronResponse(
                ImageUploadResponseSerializer(
                    ImageUploadResponse(
                        message="Avatar already exists", image=existing_image
                    )
                ),
                status=status.HTTP_200_OK,
            )

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()
        if file_extension not in settings.CDN_ALLOWED_IMAGE_EXTENSIONS:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        try:
            image = Image.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                type=ImageType.AVATAR,
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return IronResponse(
            ImageUploadResponseSerializer(
                ImageUploadResponse(message="Avatar uploaded successfully", image=image)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Images"])
class TypedImageUploadView(APIView):
    """API endpoint for uploading images with a specific type."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="upload_typed_image",
        summary="Upload an image with specific type",
        description="""Upload an image file with a specific type (product, avatar).

**Supported formats:** JPEG, PNG, GIF, WebP, BMP, HEIC, HEIF

**Available types:** product, avatar

**Maximum file size:** 100MB
""",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "Image file to upload",
                    }
                },
                "required": ["file"],
            }
        },
        responses={
            201: ImageUploadResponseSerializer,
            200: ImageUploadResponseSerializer,
            400: IronErrorSerializer,
            401: IronErrorSerializer,
            500: IronErrorSerializer,
        },
    )
    def post(self, request, image_type):
        """Upload an image file with the specified type."""
        # Validate image type
        valid_types = [choice[0] for choice in ImageType.choices]
        if image_type not in valid_types:
            return IronErrorResponse(400, ERR_400_INVALID_IMAGE_TYPE)

        serializer = FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]
        file_hash = Image.calculate_file_hash(uploaded_file)

        # Check for existing image with same hash AND type
        existing_image = Image.objects.filter(
            file_hash=file_hash, type=image_type
        ).first()
        if existing_image:
            return IronResponse(
                ImageUploadResponseSerializer(
                    ImageUploadResponse(
                        message="Image already exists", image=existing_image
                    )
                ),
                status=status.HTTP_200_OK,
            )

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()
        if file_extension not in settings.CDN_ALLOWED_IMAGE_EXTENSIONS:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        try:
            image = Image.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                type=image_type,
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return IronResponse(
            ImageUploadResponseSerializer(
                ImageUploadResponse(message="Image uploaded successfully", image=image)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Images"])
class RandomImageView(APIView):
    """API endpoint for getting a random image of a specific type."""

    permission_classes = [IsStaffUser]

    @extend_schema(
        operation_id="random_image",
        summary="Get random image by type",
        description="""Get a random image of the specified type.

**Available types:** product, avatar

**Requires:** Staff user or API key authentication.

**Use case:** Admin UI for quickly selecting test images.
""",
        responses={
            200: ImageSerializer,
            400: IronErrorSerializer,
            404: IronErrorSerializer,
            401: IronErrorSerializer,
            403: IronErrorSerializer,
        },
    )
    def get(self, request, image_type):
        """Get a random image of the given type."""
        # Validate image type
        valid_types = [choice[0] for choice in ImageType.choices]
        if image_type not in valid_types:
            return IronErrorResponse(400, ERR_400_INVALID_IMAGE_TYPE)

        # Get random image of this type
        image = (
            Image.objects.filter(type=image_type, is_processed=True)
            .order_by("?")
            .first()
        )
        if not image:
            return IronErrorResponse(404, ERR_404_NO_IMAGES)

        return IronResponse(ImageSerializer(image), status=status.HTTP_200_OK)


MAX_GENERIC_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_FILE_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".csv",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
}
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/csv",
    "application/zip",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/gzip",
    "application/octet-stream",
}


IMAGE_PREFIXES = {"product", "avatar"}


def _batch_resolve_media(ref_strings, for_update=False):
    """
    Batch-resolve media reference strings to model instances.

    Ref format: <prefix>/<hash>
      - product/<hash>, avatar/<hash> → Image (prefix = ImageType)
      - video/<hash>                  → Video
      - file/<hash>                   → File

    Returns dict: ref_str → instance (missing refs are absent).
    """
    image_lookups = {}  # (type, hash) → ref_str
    video_lookups = {}  # hash → ref_str
    file_lookups = {}  # hash → ref_str

    for ref_str in ref_strings:
        parts = ref_str.split("/")
        if len(parts) != 2:
            continue
        prefix, file_hash = parts
        if prefix in IMAGE_PREFIXES:
            image_lookups[(prefix, file_hash)] = ref_str
        elif prefix == "video":
            video_lookups[file_hash] = ref_str
        elif prefix == "file":
            file_lookups[file_hash] = ref_str

    from django.db.models import Q

    result = {}

    # Batch-fetch images
    if image_lookups:
        q = Q()
        for img_type, file_hash in image_lookups:
            q |= Q(type=img_type, file_hash=file_hash)
        qs = Image.objects.filter(q)
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            key = (obj.type, obj.file_hash)
            if key in image_lookups:
                result[image_lookups[key]] = obj

    # Batch-fetch videos
    if video_lookups:
        qs = Video.objects.filter(file_hash__in=video_lookups.keys())
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in video_lookups:
                result[video_lookups[obj.file_hash]] = obj

    # Batch-fetch files
    if file_lookups:
        qs = File.objects.filter(file_hash__in=file_lookups.keys())
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in file_lookups:
                result[file_lookups[obj.file_hash]] = obj

    return result


@extend_schema(tags=["Refs"])
class RefSyncView(APIView):
    """Sync CDN references for media files."""

    permission_classes = [IsServiceRequest]

    @extend_schema(
        operation_id="sync_refs",
        summary="Sync media references",
        description="Add/remove reference tracking for media files. Used by other services to track which entities reference which media.",
        request=RefSyncRequestSerializer,
        responses={200: RefSyncResponseSerializer},
    )
    def post(self, request):
        serializer = RefSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not data.service or not data.entity_type or not data.entity_id:
            return IronErrorResponse(400, ERR_400_MISSING_FIELDS)

        from .services import apply_ref_sync

        result = apply_ref_sync(
            service=data.service,
            entity_type=data.entity_type,
            entity_id=str(data.entity_id),
            old_hashes=list(data.old_hashes or []),
            new_hashes=list(data.new_hashes or []),
        )
        dto = RefSyncResponse(
            added=result["added"],
            removed=result["removed"],
            errors=result["errors"],
        )
        return IronResponse(RefSyncResponseSerializer(dto), status=status.HTTP_200_OK)


@extend_schema(tags=["Files"])
class GenericFileUploadView(APIView):
    """API endpoint for uploading generic files (documents, archives, etc.)."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="upload_file",
        summary="Upload a file",
        description="Upload a generic file (document, archive, etc.).",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "File to upload",
                    }
                },
                "required": ["file"],
            }
        },
        responses={
            201: FileUploadResponseSerializer,
            200: FileUploadResponseSerializer,
            400: IronErrorSerializer,
        },
    )
    def post(self, request):
        if "file" not in request.FILES:
            return IronErrorResponse(400, ERR_400_NO_FILE)

        uploaded_file = request.FILES["file"]

        if uploaded_file.size > MAX_GENERIC_FILE_SIZE:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()
        if file_extension not in ALLOWED_FILE_EXTENSIONS:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        content_type = (uploaded_file.content_type or "").lower()
        if content_type and content_type not in ALLOWED_MIME_TYPES:
            return IronErrorResponse(400, ERR_400_INVALID_FORMAT)

        file_hash = File.calculate_file_hash(uploaded_file)

        existing = File.objects.filter(file_hash=file_hash).first()
        if existing:
            return IronResponse(
                FileUploadResponseSerializer(
                    FileUploadResponseDTO(message="File already exists", file=existing)
                ),
                status=status.HTTP_200_OK,
            )

        try:
            file_obj = File.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                mime_type=uploaded_file.content_type or "",
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return IronResponse(
            FileUploadResponseSerializer(
                FileUploadResponseDTO(
                    message="File uploaded successfully", file=file_obj
                )
            ),
            status=status.HTTP_201_CREATED,
        )
