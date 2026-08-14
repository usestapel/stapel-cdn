"""
Custom validators for stapel-cdn service.
"""

import os

from django.core.exceptions import ValidationError

from . import decoders
from .conf import cdn_settings


#: Leading tokens that make a stored object *executable by a browser* when it
#: is fetched from the media origin, whatever extension it was stored under.
#: Deliberately short and literal: this is a refusal list for the plainest
#: script-in-the-media-origin shapes, not a sanitiser, and anything it does not
#: recognise is still refused by the extension and MIME allowlists above it.
_ACTIVE_CONTENT_MARKERS: tuple[bytes, ...] = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<svg",
    b"<?xml",
    b"<?php",
    b"#!",
)


def sniff_is_active_content(file) -> bool:
    """Whether *file* begins with markup/script a browser would execute.

    Extension and ``Content-Type`` on an upload are both written by the
    caller, so neither is evidence about the bytes. Everything under the media
    root is served by path, and a document route that hands a browser markup
    runs it in the media origin — which is why a ``.txt``, ``.csv`` or
    ``.pdf``-named upload whose first bytes are ``<script>`` or ``<svg>`` has
    to be refused on the bytes rather than on the name.

    Leading whitespace and a UTF-8 BOM are stripped first: both are ignored by
    the browsers that would run the content, so neither may be used to walk
    past this check.
    """
    from . import decoders

    head = decoders.read_head(file)
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    head = head.lstrip(b" \t\r\n\x0b\x0c\x00").lower()
    return any(head.startswith(marker) for marker in _ACTIVE_CONTENT_MARKERS)


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
    red) there is nothing to decode with. What happens then is a policy
    question, and ``STAPEL_CDN["REQUIRE_DECODER"]`` answers it. The default,
    ``True``, refuses the upload: with no decoder the pixel cap
    (``MAX_IMAGE_PIXELS``) is never reached and nothing confirms the bytes are
    the image they claim to be, so the honest answer is that this deployment
    cannot serve image storage — not that it will store whatever arrives.
    ``False`` restores the historical passthrough, where the gate degrades to
    what is knowable without a decoder: the allowlist plus a magic-byte
    signature. That still keeps a script payload out of storage; it cannot
    confirm the pixels.

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
        if cdn_settings.REQUIRE_DECODER:
            # Same answer as "this build cannot read that format", because from
            # the uploader's side it is the same situation: their file is fine
            # and this deployment cannot handle it. 503 + the operator-facing
            # log, never a 4xx blaming the caller for an unconfigured host.
            raise decoders.ImageDecoderUnavailable(file_extension)
        if decoders.sniff(decoders.read_head(file)) is None:
            raise ValidationError(
                "Invalid image file: the content does not carry a known image "
                "signature."
            )

    return file
