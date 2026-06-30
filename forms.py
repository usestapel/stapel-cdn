"""
Forms for stapel-cdn service.
"""

import os

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image as PILImage

from .models import Image

# Import pillow_heif for direct usage
try:
    import pillow_heif
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False


class ImageAdminForm(forms.ModelForm):
    """
    Custom form for Image admin to restrict file picker to image files.
    """

    class Meta:
        model = Image
        fields = "__all__"
        widgets = {
            "original": forms.FileInput(
                attrs={
                    "accept": "image/jpeg,image/png,image/gif,image/webp,image/bmp,image/heic,image/heif,.heic,.heif"
                }
            )
        }

    def clean_original(self):
        """Validate the uploaded image file, including HEIC/HEIF support."""
        original = self.cleaned_data.get("original")

        # If no file was uploaded (e.g., editing existing record), skip validation
        if not original:
            return original

        # Check file extension first
        file_extension = os.path.splitext(original.name)[1].lower()
        if file_extension not in settings.CDN_ALLOWED_IMAGE_EXTENSIONS:
            raise ValidationError(
                f"Invalid file extension '{file_extension}'. Allowed: {', '.join(settings.CDN_ALLOWED_IMAGE_EXTENSIONS)}"
            )

        # Handle HEIC/HEIF files - use a lenient approach
        if file_extension in [".heic", ".heif"]:
            # For HEIC files, just do basic validation (non-empty file)
            # Dimensions will be extracted during image processing
            if original.size == 0:
                raise ValidationError("The uploaded file is empty")
            # Set placeholder dimensions - will be updated during processing
            self._image_dimensions = (
                1,
                1,
            )  # Placeholder to satisfy NOT NULL constraint
            self._is_heic = True
        else:
            # For other image formats, use PIL
            try:
                img = PILImage.open(original)
                # Store dimensions for use in save()
                self._image_dimensions = img.size
                # Reset file pointer
                original.seek(0)
                self._is_heic = False
            except Exception as e:
                raise ValidationError(f"Invalid image file: {str(e)}")

        return original

    def save(self, commit=True):
        """Override save to set image dimensions."""
        instance = super().save(commit=False)

        # Set dimensions if we extracted them during validation
        if hasattr(self, "_image_dimensions"):
            instance.original_width, instance.original_height = self._image_dimensions

            # For HEIC files, mark for dimension update during processing
            if hasattr(self, "_is_heic") and self._is_heic:
                # The processing service will update the actual dimensions
                instance.is_processed = False

        if commit:
            instance.save()

        return instance
