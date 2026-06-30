"""
Custom validators for stapel-cdn service.
"""

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image as PILImage


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

    # Try to open with Pillow to verify it's a valid image
    try:
        # Open the file - handle both UploadedFile and FieldFile
        if hasattr(file, "open"):
            # FieldFile object - need to open it first
            file.open("rb")
            img = PILImage.open(file)
            _ = img.size  # Access size to ensure it's valid
            file.close()
        else:
            # UploadedFile object
            img = PILImage.open(file)
            _ = img.size  # Access size to ensure it's valid
            # Reset file pointer if possible
            if hasattr(file, "seek"):
                file.seek(0)
    except Exception as e:
        raise ValidationError(f"Invalid image file: {str(e)}")

    return file
