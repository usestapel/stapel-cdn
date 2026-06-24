"""Data Transfer Objects for CDN API."""

from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class ImageUploadResponse:
    """Successful image upload.

    Attributes:
        message: Confirmation message. Example: Image uploaded successfully
        image: Uploaded image object (serialized by ImageSerializer).
    """

    message: str
    image: Any


@dataclass
class VideoUploadResponse:
    """Successful video upload.

    Attributes:
        message: Confirmation message. Example: Video uploaded successfully
        video: Uploaded video object (serialized by VideoSerializer).
    """

    message: str
    video: Any


@dataclass
class FileExistsResponse:
    """File existence check result.

    Attributes:
        exists: Whether the file exists. Example: true
        type: File type if found (image or video). Example: image
        file: File object if found, null otherwise.
    """

    exists: bool
    type: Optional[str]
    file: Any


@dataclass
class RefSyncRequest:
    """CDN reference sync request.

    Attributes:
        service: Service name. Example: auth
        entity_type: Entity type. Example: profile
        entity_id: Entity identifier. Example: 464
        old_hashes: Previous media references. Example: ["product/hash1", "product/hash2"]
        new_hashes: Current media references. Example: ["product/hash2", "product/hash3"]
    """

    service: str
    entity_type: str
    entity_id: str
    old_hashes: List[str]
    new_hashes: List[str]


@dataclass
class RefSyncResponse:
    """CDN reference sync result.

    Attributes:
        added: Number of refs added. Example: 1
        removed: Number of refs removed. Example: 1
        errors: List of unresolved media references. Example: ["product/nonexistent"]
    """

    added: int
    removed: int
    errors: List[str]


@dataclass
class FileUploadResponse:
    """Successful file upload.

    Attributes:
        message: Confirmation message. Example: File uploaded successfully
        file: Uploaded file object.
    """

    message: str
    file: Any
