"""
Custom validators for stapel-cdn service.
"""

import os

from django.core.exceptions import ValidationError

from . import decoders
from .conf import cdn_settings


def validate_image_file(file):
    """
    Validate that the uploaded file is a valid image.

    Two checks, cheapest first: the configured extension allowlist, then a real
    decode with **libvips** — the same decoder that will process the file, so
    this gate can never be stricter than the pipeline behind it. Formats follow
    the libvips build: JPEG, PNG, GIF, WebP, TIFF and HEIC/HEIF/AVIF natively,
    BMP through ImageMagick. A ``.jpg`` that is really an HTML/script payload
    fails the decode, which is the property this gate exists for.

    In a deployment with no libvips at all (passthrough storage; checks.E001 is
    red) there is nothing to decode with, and the gate degrades to what is
    knowable without a decoder: the allowlist plus a magic-byte signature. It
    still keeps a script payload out of storage; it cannot confirm the pixels.

    Raises :class:`decoders.ImageDecoderUnavailable` (a ``ValidationError``
    subclass) when the extension is allowed by configuration but no decoder in
    this deployment can read it. That is an operator's problem, not the
    uploader's, and it is reported separately for exactly that reason — see
    ``decoders`` for why the two used to be one message.
    """
    allowed_extensions = cdn_settings.ALLOWED_IMAGE_EXTENSIONS
    file_extension = os.path.splitext(file.name)[1].lower()
    if file_extension not in allowed_extensions:
        raise ValidationError(
            f"Invalid file extension. Allowed: {', '.join(allowed_extensions)}"
        )

    # The decoder is the authority whenever there is one: it, not a signature
    # table maintained here, decides what this deployment can read. `None` means
    # there is no decoder at all — only then does the magic-byte fallback stand
    # in, so a format libvips reads but the table does not list is never refused.
    if decoders.decode_dimensions(file, file_extension) is None:
        if decoders.sniff(decoders.read_head(file)) is None:
            raise ValidationError(
                "Invalid image file: the content does not carry a known image "
                "signature."
            )

    return file
