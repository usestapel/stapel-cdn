"""
Forms for stapel-cdn service.
"""

import os

from django import forms

from . import decoders
from .models import Image
from .validators import validate_image_file


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
                    "accept": "image/jpeg,image/png,image/gif,image/webp,image/avif,image/heic,image/heif,.avif,.heic,.heif"
                }
            )
        }

    def clean_original(self):
        """Validate the uploaded image file through the module's own validator.

        This used to hand-roll a ``PIL.Image.open()`` check beside
        ``validate_image_file`` — the drift ``docs/capabilities.json`` was
        written to stop — and the copy was both weaker (no decompression-bomb
        cap) and, for HEIC/HEIF, not a check at all: Pillow could not read those
        without ``pillow_heif``, so the branch skipped verification entirely and
        stored 1x1 placeholder dimensions, indistinguishable afterwards from a
        genuinely tiny image. With libvips as the one decoder there is nothing
        left for that special case to work around: HEIC decodes like any other
        format and reports its real size.
        """
        original = self.cleaned_data.get("original")

        # If no file was uploaded (e.g., editing existing record), skip validation
        if not original:
            return original

        validate_image_file(original)

        extension = os.path.splitext(original.name)[1].lower()
        # `None` only in a deployment with no decoder at all (checks.E001):
        # fall back to the placeholder, which process_image will correct.
        self._image_dimensions = (
            decoders.decode_dimensions(original, extension) or (1, 1)
        )
        return original

    def save(self, commit=True):
        """Override save to set image dimensions."""
        instance = super().save(commit=False)

        # Set dimensions if we extracted them during validation
        if hasattr(self, "_image_dimensions"):
            instance.original_width, instance.original_height = self._image_dimensions
            # Placeholder dimensions mean nothing decoded them; let the
            # processing pass have another go rather than freezing 1x1.
            if self._image_dimensions == (1, 1):
                instance.is_processed = False

        if commit:
            instance.save()

        return instance
