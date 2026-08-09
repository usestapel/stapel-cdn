"""The deployment's image decoder — ONE decoder, for the whole image path.

Why this module exists
----------------------
Until 0.10 stapel-cdn carried two decoders that answered different questions
about the same file. The *processing* pipeline decoded with libvips
(``services.ImageProcessingService``, ``models.Image.save``, ``tasks``); the
*guards* in front of it decoded with Pillow (``validators.validate_image_file``,
``fetch.detect_image_extension``, ``forms.ImageAdminForm``). Two decoders means
two capability sets, maintained in two places, and they drifted:

    STAPEL_CDN["ALLOWED_IMAGE_EXTENSIONS"] declares .heic/.heif
    libvips reads HEIC natively (heifload) — the pipeline would have coped
    Pillow reads HEIC only via the optional `pillow_heif` package
    `pillow_heif` was not installed, and its absence was swallowed:
        try: from pillow_heif import register_heif_opener
        except ImportError: pass

so a valid HEIC avatar was refused at the door with "Invalid image file" — the
guard was stricter than the system it guarded, and it blamed the user's file
for a gap in the deployment. The two decoders also disagreed in the other
direction (Pillow reads .avif, this libvips build reads .bmp only through
ImageMagick), which is the general shape of the defect rather than one missing
package.

The fix is structural, not a second package: **the guard decodes with the same
decoder that will process**. "Declared allowed" and "actually decodable" cannot
diverge when there is only one thing to ask. libvips is also the right engine on
the merits — it streams instead of materialising a decoded bitmap in Python
memory, it is markedly faster, and it reads HEIC/AVIF natively with no extra pip
package.

The one honest gap: no decoder at all
-------------------------------------
``pyvips`` is an optional extra (``stapel-cdn[images]``) because libvips is a
SYSTEM library (apt: ``libvips-dev``), not a pip wheel. A deployment that skips
it is passthrough storage with no processing at all — and, now, no decoder. That
state is real and this module does not pretend otherwise:

* with libvips — validation decodes for real: dimensions, the pixel cap, and a
  genuine "these bytes are an image of a format this deployment can process";
* without libvips — validation degrades to what is knowable WITHOUT a decoder:
  the extension allowlist plus a magic-byte signature check (:func:`sniff`).
  That still keeps an HTML/script payload named ``.jpg`` out of storage, which
  is the property the guard exists for; it cannot confirm the pixels decode.

The two states are distinguishable at boot, not postmortem — ``checks.py``
E001 fires when there is no decoder at all, E004 when the decoder is present but
this libvips build cannot read a format the settings declare allowed. Neither is
a silent ``pass``.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError

from .conf import cdn_settings

#: Extension -> libvips load operations, ANY of which is enough to read it.
#: Probed against the operation registry of the libvips build actually present
#: (:func:`loadable_extensions`), because libvips is modular: the same version
#: compiled without libheif has no ``heifload``, and there is no native BMP
#: reader at all — ``.bmp`` is read through ImageMagick, when that module is
#: compiled in. An extension absent from this table is *unknown*, not *unreadable*:
#: the boot check stays silent about it rather than inventing a false positive.
VIPS_LOADERS: dict[str, tuple[str, ...]] = {
    ".jpg": ("jpegload",),
    ".jpeg": ("jpegload",),
    ".png": ("pngload",),
    ".gif": ("gifload", "magickload"),
    ".webp": ("webpload",),
    ".bmp": ("bmpload", "magickload"),  # libvips has no native BMP reader
    ".heic": ("heifload",),
    ".heif": ("heifload",),
    ".avif": ("heifload",),
    ".tif": ("tiffload",),
    ".tiff": ("tiffload",),
    ".jxl": ("jxlload",),
    ".jp2": ("jp2kload",),
    ".svg": ("svgload",),
}

#: Magic-byte signatures — the decoder-FREE half of the gate. Used to verify a
#: file's real format when there is no decoder to ask, and to detect the format
#: of fetched bytes (``fetch.detect_image_extension``) without trusting the URL
#: suffix or the Content-Type header.
#: ``(offset, signature, extension)``.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", ".jpg"),
    (0, b"\x89PNG\r\n\x1a\n", ".png"),
    (0, b"GIF87a", ".gif"),
    (0, b"GIF89a", ".gif"),
    (0, b"BM", ".bmp"),
    (0, b"II*\x00", ".tif"),
    (0, b"MM\x00*", ".tif"),
    (0, b"\x00\x00\x00\x0cJXL \r\n\x87\n", ".jxl"),
    (0, b"\xff\x0a", ".jxl"),
)

#: ISO-BMFF brand (bytes 8..12, after the ``ftyp`` box type) -> extension. HEIC,
#: HEIF and AVIF share one container and are told apart only by this brand.
_FTYP_BRANDS: dict[bytes, str] = {
    b"heic": ".heic", b"heix": ".heic", b"hevc": ".heic", b"hevx": ".heic",
    b"heim": ".heic", b"heis": ".heic", b"hevm": ".heic", b"hevs": ".heic",
    b"mif1": ".heif", b"msf1": ".heif",
    b"avif": ".avif", b"avis": ".avif",
}

#: Bytes needed to decide every signature above.
SNIFF_BYTES = 32


class ImageDecoderUnavailable(ValidationError):
    """This deployment cannot decode *extension* — an operator's problem.

    Deliberately distinct from a plain ``ValidationError``. A rejected upload has
    two entirely different causes that used to share one message: the file is
    broken (the user can fix it by sending a different file) or the deployment
    lacks the decoder for a format its own settings advertise as allowed (the
    user can do nothing; only an operator can). Collapsing the second into the
    first is how "Invalid image file" came to be told to somebody whose file was
    fine. Callers map this to ``error.503.image_decoder_unavailable``; the plain
    ``ValidationError`` keeps meaning "your file".
    """

    def __init__(self, extension: str, message: str | None = None) -> None:
        self.extension = extension
        super().__init__(
            message
            or (
                f"This deployment cannot decode {extension} images: no libvips "
                f"loader is available for that format. The file itself was not "
                f"rejected — install the decoder (stapel-cdn[images] plus a "
                f"libvips build supporting {extension}) or remove {extension} "
                f"from STAPEL_CDN['ALLOWED_IMAGE_EXTENSIONS']."
            )
        )


def _pyvips():
    """The pyvips module, or ``None`` when this deployment has no decoder."""
    try:
        import pyvips
    except ImportError:
        return None
    # `sys.modules["pyvips"] = None` (the checks/validator tests' poison
    # fixture, and a real half-installed environment) yields a successful
    # import of None rather than an ImportError.
    return pyvips or None


def available() -> bool:
    """Whether this deployment has an image decoder at all."""
    return _pyvips() is not None


def _operation_exists(pyvips, name: str) -> bool:
    """Whether *name* is a registered operation in THIS libvips build.

    libvips is modular — ``pyvips.Image.heifload`` resolves through
    ``__getattr__`` and so exists as an attribute whether or not libheif was
    compiled in. Constructing the operation is what actually answers the
    question.
    """
    try:
        pyvips.Operation.new_from_name(name)
    except Exception:
        return False
    return True


def loadable_extensions() -> frozenset[str]:
    """Extensions the libvips build actually present can read.

    Empty when there is no decoder. Restricted to :data:`VIPS_LOADERS` — an
    extension this table does not know is reported by nobody rather than
    guessed at.
    """
    pyvips = _pyvips()
    if pyvips is None:
        return frozenset()
    available_ops = {
        op
        for op in {op for ops in VIPS_LOADERS.values() for op in ops}
        if _operation_exists(pyvips, op)
    }
    return frozenset(
        ext for ext, ops in VIPS_LOADERS.items() if available_ops.intersection(ops)
    )


def undecodable_allowed_extensions() -> tuple[str, ...]:
    """Configured-allowed extensions this deployment provably cannot decode.

    The predicate behind both the boot check (checks.E004) and the runtime
    refusal (:class:`ImageDecoderUnavailable`) — deliberately the same function,
    so an upload can never be refused for a reason ``manage.py check`` stayed
    quiet about. Empty when there is no decoder at all: that is E001's subject,
    and reporting every configured extension a second time would bury it.
    """
    if not available():
        return ()
    loadable = loadable_extensions()
    return tuple(
        sorted(
            ext.lower()
            for ext in cdn_settings.ALLOWED_IMAGE_EXTENSIONS
            if ext.lower() in VIPS_LOADERS and ext.lower() not in loadable
        )
    )


def sniff(head: bytes) -> str | None:
    """Real format of *head* from its magic bytes, or ``None`` if it is not an
    image. Never consults a filename — a ``.jpg`` that is really HTML is exactly
    what this catches, and it needs no decoder to do it.
    """
    for offset, signature, ext in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return ext
    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12])
    if head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def read_head(file) -> bytes:
    """First :data:`SNIFF_BYTES` of *file*, rewinding it afterwards."""
    if hasattr(file, "seek"):
        file.seek(0)
    head = file.read(SNIFF_BYTES)
    if hasattr(file, "seek"):
        file.seek(0)
    return head or b""


def _open(pyvips, file):
    """Open *file* with libvips without materialising it in Python memory.

    A large upload is already spooled to disk by Django
    (``TemporaryUploadedFile``); handing libvips the path lets it stream from
    there instead of the caller reading 20 MB into a bytes object first. Small
    uploads live in memory anyway and go through the buffer form.
    """
    temp_path = getattr(file, "temporary_file_path", None)
    if callable(temp_path):
        return pyvips.Image.new_from_file(temp_path(), access="sequential")
    if hasattr(file, "seek"):
        file.seek(0)
    return pyvips.Image.new_from_buffer(file.read(), "", access="sequential")


def decode_dimensions(file, extension: str, verify: bool = False) -> tuple[int, int] | None:
    """Verify *file* decodes and return ``(width, height)``.

    ``verify`` forces a full pixel pass instead of stopping at the header.
    libvips, like Pillow before it, reads dimensions lazily — enough to reject a
    file that is not an image at all, not enough to catch one that is truncated
    or hostile past its header. The upload path leaves it off (header-only, as
    it always was: the bytes came from an authenticated request and the
    processing pass will read them again anyway); the import-from-URL path turns
    it on, because there the bytes came from an attacker-influenced address and
    that gate's whole job is to be the last word before storage.

    ``None`` means "not verifiable here": this deployment has no decoder, and
    the caller has already established (via :func:`sniff`) that the bytes carry
    a real image signature. That is the honest passthrough-storage answer, not a
    silent success — E001 is red in that deployment.

    Raises :class:`ImageDecoderUnavailable` when the format is one this build
    cannot read, and ``ValidationError`` when the decoder is present and the
    bytes are genuinely not a decodable image. The file is never closed — callers
    keep using it for hashing and storage — only rewound.
    """
    pyvips = _pyvips()
    if pyvips is None:
        return None

    if extension in undecodable_allowed_extensions():
        raise ImageDecoderUnavailable(extension)

    try:
        img = _open(pyvips, file)
        width, height = img.width, img.height
    except Exception as exc:
        raise ValidationError(f"Invalid image file: {exc}")
    finally:
        if hasattr(file, "seek") and not getattr(file, "closed", False):
            file.seek(0)

    # Decompression-bomb cap, applied BEFORE any pixel pass: libvips reads
    # dimensions from the header without decoding, so the bomb is refused
    # before it costs anything — where Pillow had to be talked out of expanding
    # it after the fact via MAX_IMAGE_PIXELS.
    max_pixels = int(cdn_settings.MAX_IMAGE_PIXELS)
    if width * height > max_pixels:
        raise ValidationError(
            f"Image is too large to process: {width}x{height} exceeds the "
            f"{max_pixels} pixel cap (STAPEL_CDN['MAX_IMAGE_PIXELS'])."
        )

    if verify:
        try:
            img.avg()  # streams every pixel; a truncated file fails here
        except Exception as exc:
            raise ValidationError(f"Invalid image file: {exc}")
        finally:
            if hasattr(file, "seek") and not getattr(file, "closed", False):
                file.seek(0)

    return width, height


__all__ = [
    "ImageDecoderUnavailable",
    "SNIFF_BYTES",
    "VIPS_LOADERS",
    "available",
    "decode_dimensions",
    "loadable_extensions",
    "sniff",
    "undecodable_allowed_extensions",
]
