"""
Custom validators for stapel-cdn service.
"""

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image as PILImage

# Decompression-bomb cap: a tiny compressed file must not expand into
# gigabytes of pixels. Pillow raises DecompressionBombError above 2x this.
PILImage.MAX_IMAGE_PIXELS = getattr(settings, "CDN_MAX_IMAGE_PIXELS", 50_000_000)


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

    # Check file extension
    file_extension = os.path.splitext(file.name)[1].lower()
    if file_extension not in settings.CDN_ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            f"Invalid file extension. Allowed: {', '.join(settings.CDN_ALLOWED_IMAGE_EXTENSIONS)}"
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
