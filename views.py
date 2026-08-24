"""
Views for stapel-cdn service.

Guest (anonymous session) stance
--------------------------------
With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a bare
``IsAuthenticated`` says nothing about whether guests belong on a view
(``stapel_core.adoption`` E001/W002). For a *storage* module that question is
sharper than elsewhere, because the anonymous axis removes the only thing
that made an upload endpoint self-limiting: an account. A session costs one
unauthenticated POST to mint, so "authenticated upload" and "open file
hosting" become the same sentence. The five upload views here state their
answer, and the rule is:

    **a guest may upload the one artifact it legitimately owns — its own
    avatar — and nothing else.**

``AvatarUploadView`` is ``ANONYMOUS_ALLOWED``: it is the picture on the
guest's own profile (``stapel-profiles`` lets a guest own one, for the same
reason — a guest who types a display name before joining a call may
reasonably attach a face to it), it is a live surface in a real consumer,
and it is bounded three ways: the image validator, ``MAX_IMAGE_SIZE`` /
``MAX_IMAGE_PIXELS``, and SHA-256 deduplication, which makes a re-upload of
the same bytes cost no new storage at all.

The general-purpose intake — arbitrary images, videos, and
``GenericFileUploadView``'s 50 MB of *anything* — carries
:class:`~stapel_core.django.api.permissions.IsNotAnonymousUser`. None of it
is bounded to something a guest owns, and none of it is a guest flow in any
consumer; leaving it open would hand unmetered storage to an identity that
costs nothing to create.
"""

import logging
import os

from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from stapel_core.django.api.errors import (
    ERR_429_TOO_MANY_REQUESTS,
    StapelErrorResponse,
    StapelErrorSerializer,
    StapelResponse,
    error_500_internal,
)
from stapel_core.django.api.views import SerializerSeamMixin
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    IsNotAnonymousUser,
    IsServiceRequest,
    IsStaffUser,
)

from django.core.exceptions import ValidationError

from stapel_cdn.decoders import ImageDecoderUnavailable
from stapel_cdn.errors import (
    ERR_400_FILE_HASH_REQUIRED,
    ERR_400_FILE_TYPE_NOT_ALLOWED,
    ERR_400_INVALID_FORMAT,
    ERR_400_INVALID_IMAGE_TYPE,
    ERR_400_MISSING_FIELDS,
    ERR_400_NO_FILE,
    ERR_400_TOO_MANY_REFS,
    ERR_403_QUOTA_EXCEEDED,
    ERR_404_NO_IMAGES,
    ERR_413_FILE_TOO_LARGE,
    ERR_503_IMAGE_DECODER_UNAVAILABLE,
)
from stapel_cdn.ownership import dedup_scope_q, quota_exceeded
from stapel_cdn.validators import sniff_is_active_content, validate_image_file

from .dto import (
    DescribeManyResponse,
    FileExistsResponse,
    ImageUploadResponse,
    RefSyncResponse,
    VideoUploadResponse,
)
from .conf import DEFAULTS, cdn_settings
from .dto import (
    FileUploadResponse as FileUploadResponseDTO,
)
from .metadata import DESCRIBE_MANY_LIMIT
from .models import File, Image, Video, get_image_type_choices
from .serializers import (
    DescribeManyRequestSerializer,
    DescribeManyResponseSerializer,
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


# ``SerializerSeamMixin`` is imported from ``stapel_core.django.api.views``
# (core 0.41.0 hoisted it into the canon). This module carried a byte-identical
# local copy until 0.16.0 — a seam every module reimplements is a seam that
# drifts, which is the whole reason it moved upstream.


def _over_size_cap(uploaded_file, cap) -> bool:
    """Whether *uploaded_file* fails the byte ceiling *cap*.

    A file that cannot state its size fails, rather than skipping the check:
    the previous form (``if uploaded_file.size and uploaded_file.size > cap``)
    let a ``size`` of ``None`` walk past the one gate that exists to keep an
    unbounded body from being read, hashed and stored. An unknown size is not
    evidence of a small file, and nothing downstream bounds the read either.
    A genuinely empty upload (``size == 0``) is not over any ceiling and is
    left to the format checks below.
    """
    size = getattr(uploaded_file, "size", None)
    return size is None or size > cap


def _validate_image_upload(uploaded_file):
    """Run cheap-to-expensive upload checks BEFORE hashing or storing.

    Order matters: size cap first (hashing an unbounded body is a DoS),
    then extension allowlist, then content verification (a libvips decode —
    the same decoder that will process the file) — a .jpg containing
    HTML/scripts must never reach storage.

    Returns an error response, or None when the file is acceptable.
    """
    if _over_size_cap(uploaded_file, cdn_settings.MAX_IMAGE_SIZE):
        return StapelErrorResponse(413, ERR_413_FILE_TOO_LARGE)

    file_extension = os.path.splitext(uploaded_file.name)[1].lower()
    if file_extension not in cdn_settings.ALLOWED_IMAGE_EXTENSIONS:
        return StapelErrorResponse(400, ERR_400_INVALID_FORMAT)

    try:
        validate_image_file(uploaded_file)
    except ImageDecoderUnavailable as exc:
        # NOT the caller's fault, so not a 4xx and not "invalid format": this
        # deployment advertises an extension it has no decoder for. Logged at
        # ERROR because the only party who can fix it is an operator, and
        # checks.E004 already says the same thing at boot.
        logger.error(
            "image upload refused: no decoder for %s in this deployment "
            "(STAPEL_CDN['ALLOWED_IMAGE_EXTENSIONS'] advertises it) — %s",
            exc.extension,
            exc,
        )
        return StapelErrorResponse(
            503,
            ERR_503_IMAGE_DECODER_UNAVAILABLE,
            {"extension": exc.extension},
        )
    except ValidationError:
        return StapelErrorResponse(400, ERR_400_INVALID_FORMAT)
    return None


def _validate_video_upload(uploaded_file):
    """Run cheap-to-expensive upload checks BEFORE hashing or storing.

    Same order and the same reasoning as :func:`_validate_image_upload`; the
    video path simply had none of it. It read the whole body and SHA-256'd it
    before consulting any bound at all, and the only ceiling behind that was
    the per-owner byte quota — which a deployment may switch off.

    There is no decode step here (no ffmpeg probe on the upload path yet), so
    the byte-level question is the one :func:`sniff_is_active_content` answers:
    every other intake asks it, and video was the single one that never did.
    Extension and Content-Type are both written by the caller, so a `.mp4`
    carrying HTML/script otherwise lands under the media root and is served
    from the media origin — see :class:`GenericFileUploadView` for the full
    version of that argument.

    Returns an error response, or None when the file is acceptable.
    """
    if _over_size_cap(uploaded_file, cdn_settings.MAX_VIDEO_SIZE):
        return StapelErrorResponse(413, ERR_413_FILE_TOO_LARGE)

    file_extension = os.path.splitext(uploaded_file.name)[1].lower()
    if file_extension not in cdn_settings.ALLOWED_VIDEO_EXTENSIONS:
        return StapelErrorResponse(400, ERR_400_INVALID_FORMAT)

    if sniff_is_active_content(uploaded_file):
        return StapelErrorResponse(400, ERR_400_FILE_TYPE_NOT_ALLOWED)

    return None


@extend_schema(tags=["Images"])
class ImageUploadView(SerializerSeamMixin, APIView):
    """API endpoint for uploading images."""

    # General-purpose image intake, bound to nothing the caller owns. With a
    # free-to-mint anonymous identity this is open image hosting.
    permission_classes = [IsNotAnonymousUser]
    parser_classes = [MultiPartParser, FormParser]
    request_serializer_class = FileUploadSerializer
    response_serializer_class = ImageUploadResponseSerializer

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

**Generated variants (all WebP):**
- 16px, 32px, 64px, 120px - min-side thumbnails
- 160px, 240px, 480px, 560px, 720px, 1080px - w/h preview branches

**`variants_status` — read it before you render a variant URL.** Every
`variant_<size>_url` in this response is derived from `<type>/<hash>`, so
all of them are present and well-formed in the 201 that creates the row,
*before* the background task has written a single file. `variants_status`
is `"pending"` until generation succeeds and `"ready"` afterwards
(`variants_ready_at` carries the moment, null while pending). A `pending`
payload's variant URLs are a prediction, not a resource; poll the media ref
(`/file-exists/`) or fall back to `original_url` until it reads `ready`.
A row that stays `pending` is a broken pipeline, not a slow one — see
`checks.W008`.

**Request format:** `multipart/form-data` with `file` field

**Maximum file size:** `STAPEL_CDN["MAX_IMAGE_SIZE"]`, 20MB by default.
Enforced before the body is hashed; over it the answer is 413.

**Stored type:** `"product"` — one value from `STAPEL_CDN["ASSET_TYPES"]`,
same as any type `TypedImageUploadView` accepts. The zero-infra default is
`("avatar",)` only (see `ASSET_TYPES` in CONFIG.MD), so a deployment that
never added `"product"` gets a 400 here, exactly as
`/images/product/upload/` already does for that string.
""",
        request=FileUploadSerializer,
        responses={
            201: ImageUploadResponseSerializer,
            200: ImageUploadResponseSerializer,
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
            413: StapelErrorSerializer,
            500: StapelErrorSerializer,
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
                        "file_hash": "a1b2c3d4e5f6...",
                        "original_filename": "photo.jpg",
                        "file_extension": ".jpg",
                        "original_width": 1920,
                        "original_height": 1080,
                        "original_size": 2048576,
                        "original_url": "/media/cdn/images/original/a1b2c3d4.jpg",
                        "variant_720_url": "/media/cdn/images/720/a1b2c3d4.webp",
                        "variants_status": "pending",
                        "variants_ready_at": None,
                        "is_processed": False,
                    },
                },
            ),
            OpenApiExample(
                name="Image already exists",
                response_only=True,
                status_codes=["200"],
                value={
                    "message": "Image already exists",
                    "image": {"id": "...", "file_hash": "..."},
                },
            ),
        ],
    )
    def post(self, request):  # noqa: R007
        """
        Upload an image file.
        Variants are automatically generated via Django signals.
        """
        # This endpoint's type is fixed rather than caller-chosen (see
        # TypedImageUploadView for that), but "product" is still one value
        # from STAPEL_CDN["ASSET_TYPES"] like any other choice on the model —
        # it must be validated the same way, or a zero-infra deployment
        # (ASSET_TYPES defaults to ("avatar",) only) would silently store an
        # image whose type isn't in its own choices, while
        # /images/product/upload/ 400s for that identical string.
        valid_types = [choice[0] for choice in get_image_type_choices()]
        if "product" not in valid_types:
            return StapelErrorResponse(400, ERR_400_INVALID_IMAGE_TYPE)

        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]

        error = _validate_image_upload(uploaded_file)
        if error:
            return error

        # Calculate file hash
        file_hash = Image.calculate_file_hash(uploaded_file)

        response_serializer_class = self.get_response_serializer_class()

        # Owner-scoped dedup (CDN-02): "have these bytes been seen before?"
        # is answered only about objects this caller already owns.
        existing_image = Image.objects.filter(
            dedup_scope_q(request.user), file_hash=file_hash, type="product"
        ).first()
        if existing_image:
            return StapelResponse(
                response_serializer_class(
                    ImageUploadResponse(
                        message="Image already exists", image=existing_image
                    )
                ),
                status=status.HTTP_200_OK,
            )

        over = quota_exceeded(request.user, uploaded_file.size)
        if over:
            return StapelErrorResponse(403, ERR_403_QUOTA_EXCEEDED, over)

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()

        # Create Image record
        # Dimensions are calculated in model.save() via pyvips
        # Variants will be automatically generated via post_save signal
        try:
            image = Image.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                type="product",
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return StapelResponse(
            response_serializer_class(
                ImageUploadResponse(message="Image uploaded successfully", image=image)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Videos"])
class VideoUploadView(SerializerSeamMixin, APIView):
    """API endpoint for uploading videos."""

    # The most expensive intake in the module (and transcoding is still to
    # come). Closed to a session that costs nothing to mint.
    permission_classes = [IsNotAnonymousUser]
    parser_classes = [MultiPartParser, FormParser]
    request_serializer_class = FileUploadSerializer
    response_serializer_class = VideoUploadResponseSerializer

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

**Maximum file size:** `STAPEL_CDN["MAX_VIDEO_SIZE"]`, 100MB by default.
Enforced before the body is hashed; over it the answer is 413.
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
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
            413: StapelErrorSerializer,
            500: StapelErrorSerializer,
        },
    )
    def post(self, request):  # noqa: R007
        """
        Upload a video file.
        Variants will be automatically generated via Django signals (TODO: implement ffmpeg processing).
        """
        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]

        # Size cap, extension allowlist and byte sniff run BEFORE the body is
        # read for hashing — hashing first is what made the cap unenforceable.
        error = _validate_video_upload(uploaded_file)
        if error:
            return error

        file_hash = Video.calculate_file_hash(uploaded_file)
        file_extension = os.path.splitext(uploaded_file.name)[1].lower()

        response_serializer_class = self.get_response_serializer_class()

        # Owner-scoped dedup (CDN-02) — see ImageUploadView.
        existing_video = Video.objects.filter(
            dedup_scope_q(request.user), file_hash=file_hash
        ).first()
        if existing_video:
            return StapelResponse(
                response_serializer_class(
                    VideoUploadResponse(
                        message="Video already exists", video=existing_video
                    )
                ),
                status=status.HTTP_200_OK,
            )

        over = quota_exceeded(request.user, uploaded_file.size)
        if over:
            return StapelErrorResponse(403, ERR_403_QUOTA_EXCEEDED, over)

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

        return StapelResponse(
            response_serializer_class(
                VideoUploadResponse(
                    message="Video uploaded successfully (variant generation not yet implemented)",
                    video=video,
                )
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Files"])
class FileExistsView(SerializerSeamMixin, APIView):
    """API endpoint for checking if a file exists by hash."""

    permission_classes = [IsAuthenticated | IsServiceRequest]
    # Request serializer applies to the POST body variant; GET reads query params.
    request_serializer_class = FileExistsSerializer
    response_serializer_class = FileExistsResponseSerializer

    def _exists_response(self, request, file_hash):
        """Resolve ``file_hash`` for the requesting user and build the response."""
        response_serializer_class = self.get_response_serializer_class()

        # Check if image exists
        image = Image.objects.filter(file_hash=file_hash, uploaded_by=request.user).first()
        if image:
            return StapelResponse(
                response_serializer_class(
                    FileExistsResponse(
                        exists=True, type="image", file=ImageSerializer(image).data
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Check if video exists
        video = Video.objects.filter(file_hash=file_hash, uploaded_by=request.user).first()
        if video:
            return StapelResponse(
                response_serializer_class(
                    FileExistsResponse(
                        exists=True, type="video", file=VideoSerializer(video).data
                    )
                ),
                status=status.HTTP_200_OK,
            )

        # Check if generic file exists
        file_obj = File.objects.filter(file_hash=file_hash, uploaded_by=request.user).first()
        if file_obj:
            return StapelResponse(
                response_serializer_class(
                    FileExistsResponse(
                        exists=True,
                        type="file",
                        file=FileModelSerializer(file_obj).data,
                    )
                ),
                status=status.HTTP_200_OK,
            )

        return StapelResponse(
            response_serializer_class(
                FileExistsResponse(exists=False, type=None, file=None)
            ),
            status=status.HTTP_200_OK,
        )

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
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
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
    def get(self, request):  # noqa: R007
        """
        Check if a file exists by its hash.
        Query parameter: file_hash
        """
        file_hash = request.query_params.get("file_hash")

        if not file_hash:
            return StapelErrorResponse(400, ERR_400_FILE_HASH_REQUIRED)

        return self._exists_response(request, file_hash)

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
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
        },
    )
    def post(self, request):  # noqa: R007
        """
        Check if a file exists by its hash (POST method).
        Body parameter: file_hash
        """
        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)

        file_hash = serializer.validated_data["file_hash"]

        return self._exists_response(request, file_hash)


@extend_schema(tags=["Images"])
class AvatarUploadView(SerializerSeamMixin, APIView):
    """API endpoint for uploading avatar images."""

    permission_classes = [IsAuthenticated]
    # The one upload a guest legitimately owns: the picture on its own
    # profile, which stapel-profiles already lets a guest have. Live in a real
    # consumer — meettoday's settings screen is reachable from the header a
    # guest sees. Bounded by the image validator, MAX_IMAGE_SIZE /
    # MAX_IMAGE_PIXELS, and SHA-256 dedup (re-uploading the same bytes costs
    # no new storage), so "free to mint a session" does not mean "free to
    # fill the disk".
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    parser_classes = [MultiPartParser, FormParser]
    request_serializer_class = FileUploadSerializer
    response_serializer_class = ImageUploadResponseSerializer

    @extend_schema(
        operation_id="upload_avatar",
        summary="Upload an avatar image",
        description="""Upload an avatar image file for processing.

**Same as image upload but sets type to 'avatar'.**

**Supported formats:** JPEG, PNG, GIF, WebP, BMP, HEIC, HEIF

**Maximum file size:** `STAPEL_CDN["MAX_IMAGE_SIZE"]`, 20MB by default.
Enforced before the body is hashed; over it the answer is 413.
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
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
            413: StapelErrorSerializer,
            500: StapelErrorSerializer,
        },
    )
    def post(self, request):  # noqa: R007
        """Upload an avatar image file. Sets type to 'avatar'."""
        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]

        error = _validate_image_upload(uploaded_file)
        if error:
            return error

        file_hash = Image.calculate_file_hash(uploaded_file)

        response_serializer_class = self.get_response_serializer_class()

        # Owner-scoped dedup (CDN-02) — see ImageUploadView.
        existing_image = Image.objects.filter(
            dedup_scope_q(request.user), file_hash=file_hash, type="avatar"
        ).first()
        if existing_image:
            return StapelResponse(
                response_serializer_class(
                    ImageUploadResponse(
                        message="Avatar already exists", image=existing_image
                    )
                ),
                status=status.HTTP_200_OK,
            )

        over = quota_exceeded(request.user, uploaded_file.size)
        if over:
            return StapelErrorResponse(403, ERR_403_QUOTA_EXCEEDED, over)

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()

        try:
            image = Image.objects.create(
                file_hash=file_hash,
                original_filename=uploaded_file.name,
                file_extension=file_extension,
                type="avatar",
                original=uploaded_file,
                original_size=uploaded_file.size,
                uploaded_by=request.user,
            )
        except Exception:
            return error_500_internal()

        return StapelResponse(
            response_serializer_class(
                ImageUploadResponse(message="Avatar uploaded successfully", image=image)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Images"])
class TypedImageUploadView(SerializerSeamMixin, APIView):
    """API endpoint for uploading images with a specific type."""

    # Caller-chosen `image_type` — general-purpose intake wearing a label, so
    # it follows ImageUploadView, not AvatarUploadView. A guest that needs an
    # avatar has the dedicated route above, which is the bounded one.
    permission_classes = [IsNotAnonymousUser]
    parser_classes = [MultiPartParser, FormParser]
    request_serializer_class = FileUploadSerializer
    response_serializer_class = ImageUploadResponseSerializer

    @extend_schema(
        operation_id="upload_typed_image",
        summary="Upload an image with specific type",
        description="""Upload an image file with a specific type (product, avatar).

**Supported formats:** JPEG, PNG, GIF, WebP, BMP, HEIC, HEIF

**Available types:** product, avatar

**Maximum file size:** `STAPEL_CDN["MAX_IMAGE_SIZE"]`, 20MB by default.
Enforced before the body is hashed; over it the answer is 413.
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
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
            413: StapelErrorSerializer,
            500: StapelErrorSerializer,
        },
    )
    def post(self, request, image_type):  # noqa: R007
        """Upload an image file with the specified type."""
        # Validate image type
        valid_types = [choice[0] for choice in get_image_type_choices()]
        if image_type not in valid_types:
            return StapelErrorResponse(400, ERR_400_INVALID_IMAGE_TYPE)

        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_file = serializer.validated_data["file"]

        error = _validate_image_upload(uploaded_file)
        if error:
            return error

        file_hash = Image.calculate_file_hash(uploaded_file)

        response_serializer_class = self.get_response_serializer_class()

        # Owner-scoped dedup (CDN-02) — see ImageUploadView.
        existing_image = Image.objects.filter(
            dedup_scope_q(request.user), file_hash=file_hash, type=image_type
        ).first()
        if existing_image:
            return StapelResponse(
                response_serializer_class(
                    ImageUploadResponse(
                        message="Image already exists", image=existing_image
                    )
                ),
                status=status.HTTP_200_OK,
            )

        over = quota_exceeded(request.user, uploaded_file.size)
        if over:
            return StapelErrorResponse(403, ERR_403_QUOTA_EXCEEDED, over)

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()

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

        return StapelResponse(
            response_serializer_class(
                ImageUploadResponse(message="Image uploaded successfully", image=image)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Images"])
class RandomImageView(SerializerSeamMixin, APIView):
    """API endpoint for getting a random image of a specific type."""

    permission_classes = [IsStaffUser]
    request_serializer_class = None  # GET only — no request body
    response_serializer_class = ImageSerializer

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
            400: StapelErrorSerializer,
            404: StapelErrorSerializer,
            401: StapelErrorSerializer,
            403: StapelErrorSerializer,
        },
    )
    def get(self, request, image_type):  # noqa: R007
        """Get a random image of the given type."""
        # Validate image type
        valid_types = [choice[0] for choice in get_image_type_choices()]
        if image_type not in valid_types:
            return StapelErrorResponse(400, ERR_400_INVALID_IMAGE_TYPE)

        # Get random image of this type
        image = (
            Image.objects.filter(type=image_type, is_processed=True)
            .order_by("?")
            .first()
        )
        if not image:
            return StapelErrorResponse(404, ERR_404_NO_IMAGES)

        return StapelResponse(
            self.get_response_serializer_class()(image), status=status.HTTP_200_OK
        )


#: Legacy module-level aliases for the generic-file intake limits. The values
#: now live in the ``STAPEL_CDN`` namespace (``MAX_FILE_SIZE``,
#: ``ALLOWED_FILE_EXTENSIONS``, ``ALLOWED_FILE_MIME_TYPES``) so an operator can
#: shrink "50 MB of anything" without forking the view; these names are kept
#: for host projects importing them and are resolved at call time, not here.
MAX_GENERIC_FILE_SIZE = DEFAULTS["MAX_FILE_SIZE"]
ALLOWED_FILE_EXTENSIONS = frozenset(DEFAULTS["ALLOWED_FILE_EXTENSIONS"])
ALLOWED_MIME_TYPES = frozenset(DEFAULTS["ALLOWED_FILE_MIME_TYPES"])


IMAGE_PREFIXES = {"product", "avatar"}


def _batch_resolve_media(ref_strings, for_update=False):
    """
    Batch-resolve media reference strings to model instances.

    Ref format: <prefix>/<hash>
      - product/<hash>, avatar/<hash> → Image (prefix = image type)
      - video/<hash>                  → Video
      - file/<hash>                   → File

    Returns dict: ref_str → instance (missing refs are absent).

    A ref names *content*, and owner-scoped dedup (see ``ownership``) means the
    same content can be held by more than one principal. Resolution is pinned
    to the oldest row for the content — one canonical carrier per ref — so that
    reference tracking cannot drift onto a different row between two calls and
    leave an entity's reference recorded against an object nobody looks up.
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
        qs = Image.objects.filter(q).order_by("created_at", "pk")
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            key = (obj.type, obj.file_hash)
            if key in image_lookups and image_lookups[key] not in result:
                result[image_lookups[key]] = obj

    # Batch-fetch videos
    if video_lookups:
        qs = Video.objects.filter(file_hash__in=video_lookups.keys()).order_by(
            "created_at", "pk"
        )
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in video_lookups and video_lookups[obj.file_hash] not in result:
                result[video_lookups[obj.file_hash]] = obj

    # Batch-fetch files
    if file_lookups:
        qs = File.objects.filter(file_hash__in=file_lookups.keys()).order_by(
            "created_at", "pk"
        )
        if for_update:
            qs = qs.select_for_update()
        for obj in qs:
            if obj.file_hash in file_lookups and file_lookups[obj.file_hash] not in result:
                result[file_lookups[obj.file_hash]] = obj

    return result


@extend_schema(tags=["Refs"])
class RefSyncView(SerializerSeamMixin, APIView):
    """Sync CDN references for media files."""

    permission_classes = [IsServiceRequest]
    request_serializer_class = RefSyncRequestSerializer
    response_serializer_class = RefSyncResponseSerializer

    @extend_schema(
        operation_id="sync_refs",
        summary="Sync media references",
        description="Add/remove reference tracking for media files. Used by other services to track which entities reference which media.",
        request=RefSyncRequestSerializer,
        responses={200: RefSyncResponseSerializer},
    )
    def post(self, request):  # noqa: R007
        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not data.service or not data.entity_type or not data.entity_id:
            return StapelErrorResponse(400, ERR_400_MISSING_FIELDS)

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
        return StapelResponse(
            self.get_response_serializer_class()(dto), status=status.HTTP_200_OK
        )


@extend_schema(tags=["Files"])
class GenericFileUploadView(SerializerSeamMixin, APIView):
    """API endpoint for uploading generic files (documents, archives, etc.)."""

    # 50 MB of arbitrary bytes with no type restriction — the plainest "open
    # file hosting" shape in the module.
    permission_classes = [IsNotAnonymousUser]
    parser_classes = [MultiPartParser, FormParser]
    request_serializer_class = None  # reads request.FILES["file"] directly
    response_serializer_class = FileUploadResponseSerializer

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
            400: StapelErrorSerializer,
        },
    )
    def post(self, request):  # noqa: R007
        if "file" not in request.FILES:
            return StapelErrorResponse(400, ERR_400_NO_FILE)

        uploaded_file = request.FILES["file"]

        if uploaded_file.size > cdn_settings.MAX_FILE_SIZE:
            return StapelErrorResponse(400, ERR_400_INVALID_FORMAT)

        file_extension = os.path.splitext(uploaded_file.name)[1].lower()
        if file_extension not in set(cdn_settings.ALLOWED_FILE_EXTENSIONS):
            return StapelErrorResponse(400, ERR_400_INVALID_FORMAT)

        # No `if content_type and ...`: the allowlist used to run ONLY when the
        # caller volunteered a value, so omitting the part header opted out of
        # it entirely — a gate any client could decline. An absent type is not
        # an allowed type; a caller that will not say what it is sending is
        # refused like one that names something off the list.
        content_type = (uploaded_file.content_type or "").strip().lower()
        if content_type not in set(cdn_settings.ALLOWED_FILE_MIME_TYPES):
            return StapelErrorResponse(400, ERR_400_INVALID_FORMAT)

        # Extension and Content-Type are both caller-supplied, so neither says
        # anything about the bytes. Everything under the media root is served
        # by path, and a browser that is handed markup will run it in the media
        # origin regardless of the name it was stored under — so the actual
        # leading bytes get the last word.
        if sniff_is_active_content(uploaded_file):
            return StapelErrorResponse(400, ERR_400_FILE_TYPE_NOT_ALLOWED)

        file_hash = File.calculate_file_hash(uploaded_file)

        response_serializer_class = self.get_response_serializer_class()

        # Owner-scoped dedup (CDN-02) — see ImageUploadView.
        existing = File.objects.filter(
            dedup_scope_q(request.user), file_hash=file_hash
        ).first()
        if existing:
            return StapelResponse(
                response_serializer_class(
                    FileUploadResponseDTO(message="File already exists", file=existing)
                ),
                status=status.HTTP_200_OK,
            )

        over = quota_exceeded(request.user, uploaded_file.size)
        if over:
            return StapelErrorResponse(403, ERR_403_QUOTA_EXCEEDED, over)

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

        return StapelResponse(
            response_serializer_class(
                FileUploadResponseDTO(
                    message="File uploaded successfully", file=file_obj
                )
            ),
            status=status.HTTP_201_CREATED,
        )


class DescribeThrottle(ScopedRateThrottle):
    """``ScopedRateThrottle`` whose rate comes from ``STAPEL_CDN`` (lazily).

    DRF resolves scoped rates from the global ``DEFAULT_THROTTLE_RATES``
    setting, which a library module cannot own, so the rate is read from this
    module's own namespace instead (``DESCRIBE_THROTTLE``).

    A caller with no identity gets ``DESCRIBE_ANON_THROTTLE``. That rate is
    dormant under the default permission — anonymous callers are refused
    outright — and becomes the only brake the moment a deployment opens
    ``DESCRIBE_PERMISSIONS`` for public media.
    """

    scope = "cdn_describe"

    def allow_request(self, request, view):
        self._request = request
        return super().allow_request(request, view)

    def get_rate(self):
        user = getattr(getattr(self, "_request", None), "user", None)
        if user is not None and not user.is_authenticated:
            anon_rate = cdn_settings.DESCRIBE_ANON_THROTTLE
            if anon_rate:
                return anon_rate
        return cdn_settings.DESCRIBE_THROTTLE


@extend_schema(tags=["Media"])
class DescribeMediaView(SerializerSeamMixin, APIView):
    """Batch render-metadata for refs the caller holds — the browser's describe.

    ``cdn.describe`` / ``cdn.describe_many`` are comm Functions, and a browser
    cannot reach the comm bus. Without this endpoint a client could only ever
    see ``render_meta`` for something it had just uploaded itself (inline in
    the upload response) or for whatever a consuming module chose to
    denormalize into its own serializer — which is why a chat bubble holding
    somebody else's ``<prefix>/<hash>`` had nothing to draw. Same batch body
    (:func:`stapel_cdn.services.describe_refs`), same ceiling, same
    ``missing``-as-data posture as the comm Function; the transport is the
    only difference.

    **Guard.** ``STAPEL_CDN["DESCRIBE_PERMISSIONS"]`` at request time rather
    than pinned at import, defaulting to the seam the read endpoints use
    (signed in, guest sessions included, or an internal service call). Setting
    ``permission_classes`` on a subclass still wins — the setting is the
    default, not a ceiling. What the snapshot discloses and why that seam
    fits it is written down beside the setting in ``conf.py``.

    **Throttle.** Batch size is response size, so the rate bounds bytes, not
    just queries. A refusal is ``error.429.too_many_requests`` with
    ``retry_after`` — the same localizable envelope as every other refusal
    here, rather than DRF's bare ``detail`` string.
    """

    #: ``None`` means "ask the settings"; a list pins the view.
    permission_classes = None
    throttle_classes = [DescribeThrottle]
    throttle_scope = "cdn_describe"
    request_serializer_class = DescribeManyRequestSerializer
    response_serializer_class = DescribeManyResponseSerializer

    def get_permissions(self):
        if self.permission_classes is not None:
            return super().get_permissions()
        from django.utils.module_loading import import_string

        return [
            import_string(dotted_path)()
            for dotted_path in (cdn_settings.DESCRIBE_PERMISSIONS or [])
        ]

    def handle_exception(self, exc):
        """Answer a throttle refusal in the module's own error envelope.

        DRF's own answer is a bare ``{"detail": "..."}`` in English, which is
        the one refusal shape this module does not otherwise emit — every
        other one carries a registered, localizable key. Converted here rather
        than by raising through the exception handler so the answer does not
        depend on a host having wired ``EXCEPTION_HANDLER``, and so
        ``Retry-After`` (the header a client actually schedules its retry
        from) survives the conversion.
        """
        if isinstance(exc, Throttled):
            params = {}
            if exc.wait is not None:
                params["retry_after"] = int(exc.wait) + 1
            response = StapelErrorResponse(429, ERR_429_TOO_MANY_REQUESTS, params)
            if exc.wait is not None:
                response["Retry-After"] = str(int(exc.wait) + 1)
            return response
        return super().handle_exception(exc)

    @extend_schema(
        operation_id="describe_media",
        summary="Render metadata for a batch of media refs",
        description=f"""Resolve up to {DESCRIBE_MANY_LIMIT} media refs to the
render-metadata snapshot a UI needs to draw them with no second round trip and
no layout jump: aspect box, byte size, an inline `preview_b64` placeholder,
`preview_kind` (known even while `preview_b64` is still null, so the box can be
reserved in the right shape), and `duration_ms` for time-based media.

This is the HTTP form of the `cdn.describe_many` comm Function and returns the
identical object — the same snapshot the upload endpoints inline as
`render_meta`.

**Unknown refs are data, not an error.** A ref that was deleted, never stored,
or is malformed comes back in `missing`; the call still succeeds and the other
snapshots still arrive, so one dead attachment does not cost a page its other
thirty-nine.

**Duplicates collapse** before the {DESCRIBE_MANY_LIMIT}-ref ceiling is
applied. Over the ceiling the answer is `error.400.too_many_refs` with `count`
and `max` in the params — page the batch. The ceiling exists because every
snapshot may inline a preview, so batch size is response size.

**Denormalize the result once**, when the ref is resolved. It is an immutable
snapshot, not something to recompute per render.
""",
        request=DescribeManyRequestSerializer,
        responses={
            200: DescribeManyResponseSerializer,
            400: StapelErrorSerializer,
            401: StapelErrorSerializer,
            403: StapelErrorSerializer,
            429: StapelErrorSerializer,
        },
        examples=[
            OpenApiExample(
                name="Describe three attachments, one of them gone",
                request_only=True,
                value={
                    "refs": [
                        "avatar/" + "a1" * 32,
                        "video/" + "b2" * 32,
                        "product/" + "c3" * 32,
                    ]
                },
            ),
            OpenApiExample(
                name="One resolved, one missing",
                response_only=True,
                status_codes=["200"],
                value={
                    "items": {
                        "avatar/" + "a1" * 32: {
                            "ref": "avatar/" + "a1" * 32,
                            "kind": "image",
                            "mime": "image/jpeg",
                            "ext": ".jpg",
                            "bytes": 51234,
                            "width": 1600,
                            "height": 900,
                            "aspect": 1.777778,
                            "square": False,
                            "animated": False,
                            "duration_ms": None,
                            "preview_b64": "data:image/webp;base64,UklGRh...",
                            "preview_kind": "blur",
                            "poster_url": None,
                            "meta_status": "ok",
                            "meta_reason": None,
                            "variants": [],
                        }
                    },
                    "missing": ["product/" + "c3" * 32],
                },
            ),
        ],
    )
    def post(self, request):
        from .services import DescribeBatchTooLarge, describe_refs

        serializer = self.get_request_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = describe_refs(serializer.validated_data["refs"])
        except DescribeBatchTooLarge as exc:
            return StapelErrorResponse(
                400,
                ERR_400_TOO_MANY_REFS,
                {"count": exc.count, "max": exc.limit},
            )

        return StapelResponse(
            self.get_response_serializer_class()(
                DescribeManyResponse(
                    items=result["items"], missing=result["missing"]
                )
            ),
            status=status.HTTP_200_OK,
        )
