"""Tests for stapel_cdn.decoders — the one decoder on the image path.

These pin the defect that produced this module. A HEIC avatar was uploaded to a
deployment whose ``ALLOWED_IMAGE_EXTENSIONS`` declared ``.heic``, whose libvips
read HEIC natively, and whose pipeline would have processed it — and it came
back 400 "Invalid image file", blaming the uploader for a file that was fine.
The cause was two decoders: the guard asked Pillow (no HEIF support without the
absent ``pillow_heif``), the pipeline used libvips. The guard was stricter than
the system it guarded.

So the properties worth holding are, in order of what actually went wrong:

1. the guard accepts what the pipeline can process (test_guard_is_not_stricter);
2. when it cannot, it says whose problem that is (TestDecoderUnavailable);
3. and it says so at boot, not at upload time (test_checks.py E004).
"""
import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from stapel_cdn import decoders
from stapel_cdn.validators import validate_image_file


class TestSniff:
    """Magic-byte signatures — the decoder-free half of the gate."""

    @pytest.mark.parametrize(
        "head,expected",
        [
            (b"\xff\xd8\xff\xe0", ".jpg"),
            (b"\x89PNG\r\n\x1a\n", ".png"),
            (b"GIF89a", ".gif"),
            (b"BM\x00\x00", ".bmp"),
            (b"RIFF\x00\x00\x00\x00WEBP", ".webp"),
            (b"II*\x00", ".tif"),
            (b"\x00\x00\x00\x18ftypheic", ".heic"),
            (b"\x00\x00\x00\x18ftypmif1", ".heif"),
            (b"\x00\x00\x00\x18ftypavif", ".avif"),
        ],
    )
    def test_known_signatures(self, head, expected):
        assert decoders.sniff(head) == expected

    def test_real_heic_sniffs_as_heic(self, tiny_heic_bytes):
        assert decoders.sniff(tiny_heic_bytes[: decoders.SNIFF_BYTES]) == ".heic"

    @pytest.mark.parametrize(
        "payload",
        [
            b"<html><script>alert(1)</script>",
            b"not an image at all",
            b"",
            b"%PDF-1.4",
        ],
    )
    def test_non_images_are_not_recognised(self, payload):
        assert decoders.sniff(payload) is None

    def test_never_consults_a_filename(self):
        """A .jpg that is really HTML is exactly what this catches."""
        assert decoders.sniff(b"<html><body>hi</body></html>") is None


class TestLoadableExtensions:
    def test_empty_without_a_decoder(self, poisoned_pyvips):
        assert decoders.loadable_extensions() == frozenset()
        assert decoders.available() is False

    def test_reports_the_formats_this_build_has(self):
        pytest.importorskip("pyvips")
        loadable = decoders.loadable_extensions()
        # jpeg/png are in every libvips build worth deploying.
        assert {".jpg", ".png"} <= loadable
        # Never claims a format outside the table it can actually justify.
        assert loadable <= set(decoders.VIPS_LOADERS)


class TestUndecodableAllowedExtensions:
    """The predicate shared by the boot check and the runtime refusal.

    Deliberately one function: an upload can never be refused for a reason
    ``manage.py check`` stayed silent about.
    """

    def test_empty_when_settings_match_the_build(self, settings):
        pytest.importorskip("pyvips")
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".png")}
        assert decoders.undecodable_allowed_extensions() == ()

    def test_names_the_advertised_but_unreadable_extension(self, settings, monkeypatch):
        monkeypatch.setattr(
            decoders, "loadable_extensions", lambda: frozenset({".jpg"})
        )
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".heic")}
        assert decoders.undecodable_allowed_extensions() == (".heic",)

    def test_silent_without_a_decoder(self, settings, poisoned_pyvips):
        """E001 owns "no decoder at all"; this must not bury it."""
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".heic")}
        assert decoders.undecodable_allowed_extensions() == ()

    def test_unknown_extensions_are_not_guessed_at(self, settings):
        pytest.importorskip("pyvips")
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".zzz")}
        # Absent from VIPS_LOADERS means "unknown", not "unreadable" — the
        # check reports nothing rather than inventing a false positive.
        assert decoders.undecodable_allowed_extensions() == ()


class TestGuardMatchesPipeline:
    def test_guard_is_not_stricter_than_the_pipeline(self, tiny_heic_bytes):
        """The regression test for the reported defect.

        Every extension the deployment declares allowed AND that libvips can
        load must pass validation. Before 0.10 .heic satisfied both and was
        refused anyway, because a third party — Pillow — got the deciding vote.
        """
        pytest.importorskip("pyvips")
        if ".heic" not in decoders.loadable_extensions():
            pytest.skip("this libvips build has no heifload (no HEIC decoder)")

        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        assert validate_image_file(upload) is upload

    def test_real_heic_reports_true_dimensions(self, tiny_heic):
        assert decoders.decode_dimensions(tiny_heic, ".heic") == (16, 16)

    def test_file_is_rewound_not_closed(self, tiny_heic, tiny_heic_bytes):
        """Callers keep using the file for hashing and storage."""
        decoders.decode_dimensions(tiny_heic, ".heic")
        assert not tiny_heic.closed
        assert tiny_heic.tell() == 0
        assert tiny_heic.read() == tiny_heic_bytes


class TestDecoderUnavailable:
    """A missing decoder and a broken file are different states.

    They used to be one message. That is the whole reason this exception exists.
    """

    def test_raises_for_an_advertised_but_undecodable_format(
        self, settings, monkeypatch, tiny_heic_bytes
    ):
        monkeypatch.setattr(
            decoders, "loadable_extensions", lambda: frozenset({".jpg"})
        )
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".heic")}
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")

        with pytest.raises(decoders.ImageDecoderUnavailable) as exc:
            validate_image_file(upload)
        assert exc.value.extension == ".heic"
        # The message must not blame the file...
        assert "The file itself was not rejected" in str(exc.value)
        # ...and must carry both remedies.
        assert "stapel-cdn[images]" in str(exc.value)
        assert "ALLOWED_IMAGE_EXTENSIONS" in str(exc.value)

    def test_is_a_validation_error_so_existing_callers_still_catch_it(self):
        assert issubclass(decoders.ImageDecoderUnavailable, ValidationError)

    def test_a_genuinely_broken_file_stays_the_users_problem(self):
        """The other half of the split: this one really IS the file."""
        pytest.importorskip("pyvips")
        upload = SimpleUploadedFile(
            "a.jpg", b"\xff\xd8\xfftruncated garbage", content_type="image/jpeg"
        )
        with pytest.raises(ValidationError) as exc:
            validate_image_file(upload)
        assert not isinstance(exc.value, decoders.ImageDecoderUnavailable)


class TestDecompressionBombCap:
    def test_refuses_above_the_configured_pixel_cap(self, settings, tiny_heic_bytes):
        pytest.importorskip("pyvips")
        settings.STAPEL_CDN = {
            "ALLOWED_IMAGE_EXTENSIONS": (".heic",),
            "MAX_IMAGE_PIXELS": 100,  # the 16x16 fixture is 256 px
        }
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        with pytest.raises(ValidationError, match="pixel cap"):
            validate_image_file(upload)

    def test_cap_is_exact_not_double(self, settings, tiny_heic_bytes):
        """0.10 made MAX_IMAGE_PIXELS mean what its name says.

        Pillow only raised above *2x* the configured number, so the effective
        ceiling was quietly double what an operator set. 256 px against a 300 px
        cap must pass; against a 200 px cap it must not.
        """
        pytest.importorskip("pyvips")
        settings.STAPEL_CDN = {
            "ALLOWED_IMAGE_EXTENSIONS": (".heic",),
            "MAX_IMAGE_PIXELS": 300,
        }
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        assert validate_image_file(upload) is upload

        settings.STAPEL_CDN = {
            "ALLOWED_IMAGE_EXTENSIONS": (".heic",),
            "MAX_IMAGE_PIXELS": 200,
        }
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        with pytest.raises(ValidationError, match="pixel cap"):
            validate_image_file(upload)


class TestNoDecoderAtAll:
    """Passthrough storage: honest degradation, and only when asked for.

    Degrading to a signature check means ``MAX_IMAGE_PIXELS`` is never reached
    and nothing confirms the bytes decode, so it is a posture a deployment
    chooses — ``STAPEL_CDN["REQUIRE_DECODER"] = False`` — rather than what
    happens by default when libvips is missing.
    """

    def test_refuses_storage_by_default(self, poisoned_pyvips, settings, tiny_heic_bytes):
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".heic",)}
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        # An operator's problem, not the uploader's — the view answers 503.
        with pytest.raises(decoders.ImageDecoderUnavailable):
            validate_image_file(upload)

    def test_degrades_to_the_signature_check_when_asked_to(
        self, poisoned_pyvips, settings, tiny_heic_bytes
    ):
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".heic",),
                               "REQUIRE_DECODER": False}
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        # Accepted on its signature — the pixels are simply not verifiable here.
        assert validate_image_file(upload) is upload

    def test_still_keeps_a_script_payload_out_of_storage(
        self, poisoned_pyvips, settings
    ):
        settings.STAPEL_CDN = {"ALLOWED_IMAGE_EXTENSIONS": (".jpg",),
                               "REQUIRE_DECODER": False}
        upload = SimpleUploadedFile(
            "a.jpg", b"<html><script>alert(1)</script>", content_type="image/jpeg"
        )
        with pytest.raises(ValidationError, match="signature"):
            validate_image_file(upload)

    def test_decode_dimensions_reports_none_rather_than_a_guess(
        self, poisoned_pyvips, tiny_heic_bytes
    ):
        upload = SimpleUploadedFile("a.heic", tiny_heic_bytes, content_type="image/heic")
        assert decoders.decode_dimensions(upload, ".heic") is None
