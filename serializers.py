"""
Serializers for stapel-cdn service.
"""

from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    DescribeManyResponse,
    FileExistsResponse,
    FileUploadResponse,
    ImageUploadResponse,
    RefSyncRequest,
    RefSyncResponse,
    VideoUploadResponse,
)
from .errors import ERR_400_FILE_TYPE_NOT_ALLOWED
from .metadata import DESCRIBE_MANY_LIMIT
from .models import VARIANTS_STATUSES, File, Image, Video


class VariantsMetaField(serializers.JSONField):
    """``[{tier, branch, url, width, height}]`` — fixed-shape per-variant geometry.

    Unlike Feature/config-style polymorphic fields elsewhere in the fleet,
    ``Image.variants_meta`` (models.py) has ONE shape, not a `type`-keyed
    union — so it is typed directly as an array of objects rather than a
    discriminated ``oneOf`` (A1 delta: typed instead of the bare `JSONField`
    this endpoint shipped with).
    """


class VariantsMetaFieldExtension(OpenApiSerializerFieldExtension):
    target_class = VariantsMetaField

    def map_serializer_field(self, auto_schema, direction):
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tier": {"type": "integer"},
                    "branch": {
                        "type": "string",
                        "enum": ["w", "h"],
                        "nullable": True,
                        "description": "None for thumbnail-class (min-side) tiers.",
                    },
                    "url": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "required": ["tier", "url", "width", "height"],
            },
        }


class RenderMetaField(serializers.JSONField):
    """The ``cdn.describe`` snapshot, inline in an upload/read response.

    Same object the comm Function returns (``stapel_cdn.metadata.
    build_render_metadata``), so an HTTP caller and a service caller build
    against ONE contract instead of two that drift. Fixed shape, hence a
    typed object rather than the bare ``JSONField`` an "extra metadata" bag
    would have been.
    """


def render_meta_schema() -> dict:
    """OpenAPI schema of ONE render-metadata snapshot.

    A function, not a module constant, so every consumer gets its own dict —
    drf-spectacular post-processes the objects it is handed, and two fields
    sharing one instance would let a mutation for one leak into the other.
    Both the inline ``render_meta`` field and the ``POST /describe/`` map of
    them are built from this, which is what keeps "the same object, however
    you reach it" true in the schema and not only in the prose.
    """
    return {
        "type": "object",
        "description": (
            "Everything needed to render this attachment with no second "
            "round trip: aspect box, byte size, inline placeholder, and "
            "duration for time-based media. Identical to the cdn.describe "
            "comm Function's return value."
        ),
        "properties": {
            "ref": {"type": "string"},
            "kind": {
                "type": "string",
                "nullable": True,
                "description": (
                    "Open media-kind registry (STAPEL_CDN['MEDIA_KINDS']): "
                    "image, gif, video, audio, file, or a host-defined kind."
                ),
            },
            "mime": {"type": "string"},
            "ext": {"type": "string", "description": "Lowercase, dot-prefixed."},
            "bytes": {"type": "integer"},
            "width": {"type": "integer", "nullable": True},
            "height": {"type": "integer", "nullable": True},
            "aspect": {
                "type": "number",
                "format": "float",
                "nullable": True,
                "description": "width / height, 6dp.",
            },
            "square": {"type": "boolean"},
            "animated": {"type": "boolean"},
            "duration_ms": {"type": "integer", "nullable": True},
            "preview_b64": {
                "type": "string",
                "nullable": True,
                "description": (
                    "data:image/webp;base64,... bounded by "
                    "STAPEL_CDN['MICRO_PREVIEW_MAX_BYTES']; null when "
                    "refused or not generated, with meta_reason saying which."
                ),
            },
            "preview_kind": {
                "type": "string",
                "enum": ["blur", "poster", "waveform"],
                "nullable": True,
            },
            "poster_url": {"type": "string", "nullable": True},
            "meta_status": {
                "type": "string",
                "enum": ["ok", "partial", "missing"],
            },
            "meta_reason": {
                "type": "string",
                "nullable": True,
                "description": "stapel_cdn.metadata.REASONS; null when ok.",
            },
            "variants": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
        "required": ["ref", "mime", "ext", "bytes", "meta_status"],
    }


class RenderMetaFieldExtension(OpenApiSerializerFieldExtension):
    target_class = RenderMetaField

    def map_serializer_field(self, auto_schema, direction):
        return render_meta_schema()


class RenderMetaMapField(serializers.JSONField):
    """``{ref: RenderMeta}`` — the describe batch's body, keyed by ref.

    A map rather than a list because the caller asked BY ref and renders by
    ref: an array would make every consumer build this dict itself, and the
    refs that resolved to nothing are already reported separately in
    ``missing`` rather than as null entries here.
    """


class RenderMetaMapFieldExtension(OpenApiSerializerFieldExtension):
    target_class = RenderMetaMapField

    def map_serializer_field(self, auto_schema, direction):
        return {
            "type": "object",
            "description": (
                "Render-metadata snapshot per ref that resolved, keyed by the "
                "ref the caller asked for. Refs that resolved to nothing are "
                "in `missing`, not here."
            ),
            "additionalProperties": render_meta_schema(),
        }


class FileResultField(serializers.JSONField):
    """``ImageSerializer | VideoSerializer | FileModelSerializer`` result.

    The concrete shape is picked by a SIBLING field in the same envelope
    (``FileExistsResponse.type == "image"|"video"|"file"``, views.py
    ``_exists_response``), not by a key inside this object itself — so
    unlike ``FeatureDto``/``FeatureConfig`` elsewhere in the fleet, this
    cannot carry an OpenAPI ``discriminator`` (that requires the
    discriminating property to live inside the oneOf'd object). A plain
    ``oneOf`` of the three concrete serializers is still real typing, not
    the `any` a bare ``JSONField`` described before.
    """


class FileResultFieldExtension(OpenApiSerializerFieldExtension):
    target_class = FileResultField

    def map_serializer_field(self, auto_schema, direction):
        # Referenced by name, resolved when the schema is actually built
        # (well after this module finishes importing) — ImageSerializer is
        # defined above, FileModelSerializer below; forward references are
        # fine here because none of this runs at class-body-eval time.
        refs = []
        for serializer_class in (ImageSerializer, VideoSerializer, FileModelSerializer):
            component = auto_schema.resolve_serializer(serializer_class, direction)
            refs.append({"$ref": f"#/components/schemas/{component.name}"})
        return {"oneOf": refs, "nullable": True}


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
    # Variants 16..1080 (+ 720 JPEG fallback) are backed by ``variant_<size>_url``
    # model properties. A read-only ``URLField`` reads that property and gives
    # drf-spectacular an explicit ``string/uri`` type (a bare ``ReadOnlyField``
    # left the type unresolved and warned).
    variant_16_url = serializers.URLField(read_only=True, help_text="16px micro thumbnail, min-side (WebP)")
    variant_32_url = serializers.URLField(read_only=True, help_text="32px thumbnail, min-side (WebP)")
    variant_64_url = serializers.URLField(read_only=True, help_text="64px thumbnail, min-side (WebP)")
    variant_120_url = serializers.URLField(read_only=True, help_text="120px thumbnail, min-side (WebP)")
    variant_160_url = serializers.URLField(read_only=True, help_text="160px preview, w-branch (WebP)")
    variant_240_url = serializers.URLField(read_only=True, help_text="240px preview, w-branch (WebP)")
    variant_480_url = serializers.URLField(read_only=True, help_text="480px preview, w-branch (WebP)")
    variant_560_url = serializers.URLField(read_only=True, help_text="560px preview, w-branch (WebP)")
    variant_720_url = serializers.URLField(read_only=True, help_text="720px preview, w-branch (WebP)")
    variant_1080_url = serializers.URLField(read_only=True, help_text="1080px preview, w-branch (WebP)")
    variants_meta = VariantsMetaField(
        read_only=True,
        help_text="Generated variants with geometry: [{tier, branch, url, width, height}]",
    )
    # The one field that distinguishes "these URLs will exist shortly" from
    # "these URLs will never exist". Every variant_<size>_url above is derived
    # from <type>/<hash>, so all of them are present and well-formed in the
    # 201 that creates the row — before a worker has touched the file. A
    # consumer that renders them immediately renders 404s.
    variants_status = serializers.ChoiceField(
        choices=VARIANTS_STATUSES,
        read_only=True,
        help_text=(
            "'pending' — variant generation has not completed, every "
            "variant_<size>_url in this payload is a prediction; 'ready' — "
            "the ladder exists and the URLs resolve."
        ),
    )
    variants_ready_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="When variant generation completed; null while pending.",
    )
    # 1440/2160 have no ``variant_<size>_url`` model property (not in
    # DEFAULT_VARIANT_SIZES), so a ReadOnlyField silently dropped them and
    # drf-spectacular errored resolving them against the model. Compute the
    # URL directly via ``Image.get_variant_url``.
    variant_1440_url = serializers.SerializerMethodField(help_text="1440px 2K (WebP)")
    variant_2160_url = serializers.SerializerMethodField(help_text="2160px 4K (WebP)")
    # The whole render contract in one place, so a client never has to
    # reassemble aspect/placeholder/byte size out of five sibling fields.
    render_meta = serializers.SerializerMethodField(
        help_text="cdn.describe snapshot for this image (see RenderMeta)."
    )
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
            "variant_560_url",
            "variant_720_url",
            "variant_1080_url",
            "variant_1440_url",
            "variant_2160_url",
            "variants_meta",
            "variants_status",
            "variants_ready_at",
            "render_meta",
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
            "render_meta",
            "type",
            "variants_status",
            "variants_ready_at",
            "is_processed",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_prefix(self, obj):
        return f"{obj.type}/{obj.file_hash}"

    @extend_schema_field(OpenApiTypes.URI)
    def get_original_url(self, obj):
        return obj.original.url if obj.original else None

    @extend_schema_field(RenderMetaField)
    def get_render_meta(self, obj):
        from .metadata import build_render_metadata

        return build_render_metadata(obj)

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_1440_url(self, obj):
        return obj.get_variant_url(1440)

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_2160_url(self, obj):
        return obj.get_variant_url(2160)


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
    poster_url = serializers.SerializerMethodField(
        help_text="Derived poster frame; null until one has been written."
    )
    render_meta = serializers.SerializerMethodField(
        help_text="cdn.describe snapshot for this video (see RenderMeta)."
    )
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
            "poster_url",
            "render_meta",
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
            "poster_url",
            "render_meta",
            "is_processed",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(OpenApiTypes.URI)
    def get_original_url(self, obj):
        return obj.original.url if obj.original else None

    @extend_schema_field(RenderMetaField)
    def get_render_meta(self, obj):
        from .metadata import build_render_metadata

        return build_render_metadata(obj)

    @extend_schema_field(OpenApiTypes.URI)
    def get_poster_url(self, obj):
        return obj.poster_url

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_16p_url(self, obj):
        return obj.variant_16.url if obj.variant_16 else None

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_32p_url(self, obj):
        return obj.variant_32.url if obj.variant_32 else None

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_240p_url(self, obj):
        return obj.variant_240.url if obj.variant_240 else None

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_480p_url(self, obj):
        return obj.variant_480.url if obj.variant_480 else None

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_720p_url(self, obj):
        return obj.variant_720.url if obj.variant_720 else None

    @extend_schema_field(OpenApiTypes.URI)
    def get_variant_1080p_url(self, obj):
        return obj.variant_1080.url if obj.variant_1080 else None

    @extend_schema_field(OpenApiTypes.URI)
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
        from .conf import cdn_settings

        # Get file extension
        file_extension = value.name.split(".")[-1].lower()

        # Check if it's an allowed extension
        allowed_extensions = list(cdn_settings.ALLOWED_IMAGE_EXTENSIONS) + list(
            cdn_settings.ALLOWED_VIDEO_EXTENSIONS
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


class DescribeManyRequestSerializer(serializers.Serializer):
    """The refs one ``POST /describe/`` call asks about.

    Shape only. The ceiling is NOT checked here: it belongs to
    ``services.describe_refs``, the body the comm Function shares, so that the
    rule (dedup, then compare against ``DESCRIBE_MANY_LIMIT``) has exactly one
    implementation and the two transports cannot drift apart on where the
    fifty-first ref stops being acceptable.

    A ref whose shape is wrong is not a 400 either: it resolves to nothing and
    comes back in ``missing`` like any other unresolvable ref, so one
    malformed entry never costs the caller the other forty-nine snapshots.
    """

    refs = serializers.ListField(
        child=serializers.CharField(max_length=256, allow_blank=True),
        allow_empty=True,
        required=True,
        help_text=(
            "Media refs in <prefix>/<hash> form. Duplicates collapse before "
            f"the {DESCRIBE_MANY_LIMIT}-ref ceiling is applied."
        ),
    )


class DescribeManyResponseSerializer(StapelDataclassSerializer):
    """``{items: {ref: RenderMeta}, missing: [ref]}``."""

    items = RenderMetaMapField(
        help_text="Snapshot per ref that resolved, keyed by ref."
    )
    missing = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "Refs that resolved to nothing — deleted, never stored, or "
            "malformed. Draw a placeholder for these; they are data, not an "
            "error, and they never fail the call."
        ),
    )

    class Meta:
        dataclass = DescribeManyResponse


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

    file = FileResultField(
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
    # ModelSerializer has no source override for `refs` (models.py "List of
    # references: service/entity_type/entity_id"), so without this explicit
    # declaration it falls back to a bare untyped JSONField in the OpenAPI
    # schema (contract-pipeline.md A1 — typed where typeable, no free-form
    # blob for something that is, in fact, `list[str]`). Same
    # required/read-only-ness as the auto-generated field it replaces
    # (blank=True on the model -> required=False, not otherwise read-only).
    refs = serializers.ListField(
        child=serializers.CharField(), required=False,
        help_text="List of references: service/entity_type/entity_id",
    )
    # A document's render contract is mime + extension + byte size. It has
    # no derived preview, and the snapshot says so explicitly
    # (preview_kind: null) rather than leaving a client to guess whether one
    # is still coming.
    render_meta = serializers.SerializerMethodField(
        help_text="cdn.describe snapshot for this file (see RenderMeta)."
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
            "render_meta",
            "refs",
            "uploaded_by",
            "uploaded_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "file_hash",
            "original_size",
            "render_meta",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_prefix(self, obj):
        return f"file/{obj.file_hash}"

    @extend_schema_field(RenderMetaField)
    def get_render_meta(self, obj):
        from .metadata import build_render_metadata

        return build_render_metadata(obj)

    @extend_schema_field(OpenApiTypes.URI)
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
