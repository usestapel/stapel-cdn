"""
Custom validators for stapel-cdn service.
"""

import os

from django.core.exceptions import ValidationError
from PIL import Image as PILImage

from .conf import cdn_settings

# Decompression-bomb cap: a tiny compressed file must not expand into
# gigabytes of pixels. Pillow raises DecompressionBombError above 2x this.
PILImage.MAX_IMAGE_PIXELS = cdn_settings.MAX_IMAGE_PIXELS


def validate_image_file(file):
    """
    Validate that the uploaded file is a valid image.
    Supports standard formats and HEIC/HEIF.
    """
    # Register HEIF support for this validation
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass

    # Refresh the decompression-bomb cap (conf may have changed since import)
    PILImage.MAX_IMAGE_PIXELS = cdn_settings.MAX_IMAGE_PIXELS

    # Check file extension
    allowed_extensions = cdn_settings.ALLOWED_IMAGE_EXTENSIONS
    file_extension = os.path.splitext(file.name)[1].lower()
    if file_extension not in allowed_extensions:
        raise ValidationError(
            f"Invalid file extension. Allowed: {', '.join(allowed_extensions)}"
        )

    # Try to open with Pillow to verify it's a valid image.
    # Never close the file: callers keep using it (hashing, storage) —
    # only rewind the pointer.
    try:
        if hasattr(file, "seek"):
            file.seek(0)
        img = PILImage.open(file)
        _ = img.size  # Access size to ensure it's valid
    except Exception as e:
        raise ValidationError(f"Invalid image file: {str(e)}")
    finally:
        if hasattr(file, "seek") and not getattr(file, "closed", False):
            file.seek(0)

    return file
