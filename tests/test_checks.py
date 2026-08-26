"""Tests for stapel_cdn.checks (tag ``stapel_cdn``).

Per-submodule system checks (cdn-modularity.md §2.2/§3). All four are now tied
to ``STAPEL_CDN["ENABLED_SUBMODULES"]``: images (E001 no libvips at all, E004
libvips present but blind to a configured format) and video/recordings
(E002/E003, ffmpeg). ``images`` is on by default, so E001 fires out of the box
for a deployment that forgot libvips — but a deployment running stapel-cdn as
passthrough file storage turns images off and is not nagged about a decoder it
has no use for.
"""
import pytest
from django.test import override_settings

from stapel_cdn.checks import (
    E001_IMAGES_LIBRARY_MISSING,
    E002_VIDEO_BINARY_MISSING,
    E003_RECORDINGS_BINARY_MISSING,
    E004_IMAGE_FORMAT_UNDECODABLE,
    W008_VARIANT_QUEUE_UNPROVEN,
    check_submodule_binaries,
    check_variant_queues,
)


class TestImagesDecoderProbe:
    def test_clean_when_pyvips_importable(self):
        pytest.importorskip("pyvips")
        errors = check_submodule_binaries()
        assert not any(e.id == E001_IMAGES_LIBRARY_MISSING for e in errors)

    def test_errors_when_pyvips_missing(self, poisoned_pyvips):
        errors = check_submodule_binaries()
        images_errors = [e for e in errors if e.id == E001_IMAGES_LIBRARY_MISSING]
        assert len(images_errors) == 1
        assert "libvips" in images_errors[0].hint
        assert "stapel-cdn[images]" in images_errors[0].hint

    def test_fires_by_default(self, poisoned_pyvips):
        # "images" is in DEFAULT_ENABLED_SUBMODULES: a deployment that says
        # nothing still gets told its image path has no decoder.
        assert any(
            e.id == E001_IMAGES_LIBRARY_MISSING for e in check_submodule_binaries()
        )

    def test_silent_when_images_not_enabled(self, poisoned_pyvips):
        """Passthrough file storage is not told to install libvips.

        The one axis this check is allowed to be quiet on: a deployment storing
        PDFs with images switched off has no image path to break.
        """
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("files",)}):
            errors = check_submodule_binaries()
        assert not any(e.id == E001_IMAGES_LIBRARY_MISSING for e in errors)

    def test_names_the_system_package_and_the_pip_extra(self, poisoned_pyvips):
        (error,) = [
            e
            for e in check_submodule_binaries()
            if e.id == E001_IMAGES_LIBRARY_MISSING
        ]
        assert "libvips-dev" in error.hint
        assert "stapel-cdn[images]" in error.hint
        # ...and the second remedy: turn the submodule off.
        assert "ENABLED_SUBMODULES" in error.hint


class TestUndecodableFormatProbe:
    """E004 — the setting advertises a format this libvips build cannot read.

    The meettoday defect one level down: ALLOWED_IMAGE_EXTENSIONS declared
    .heic, nothing in the deployment could decode it, and the first anyone
    heard of it was a user being told their file was invalid. Detectable at
    boot, so it is detected at boot.
    """

    def test_clean_when_every_allowed_extension_is_loadable(self):
        pytest.importorskip("pyvips")
        with override_settings(
            STAPEL_CDN={"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".png")}
        ):
            errors = check_submodule_binaries()
        assert not any(e.id == E004_IMAGE_FORMAT_UNDECODABLE for e in errors)

    def test_errors_when_an_allowed_extension_has_no_loader(self, monkeypatch):
        pytest.importorskip("pyvips")
        # Stub the capability rather than the environment: the assertion is
        # about the mechanism, and must hold however CI's libvips was compiled.
        monkeypatch.setattr(
            "stapel_cdn.decoders.loadable_extensions",
            lambda: frozenset({".jpg", ".png"}),
        )
        with override_settings(
            STAPEL_CDN={"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".heic")}
        ):
            errors = check_submodule_binaries()
        (error,) = [e for e in errors if e.id == E004_IMAGE_FORMAT_UNDECODABLE]
        # Names the extension...
        assert ".heic" in error.msg
        # ...the decoder that is missing...
        assert "heifload" in error.msg
        # ...and both remedies: install it, or stop advertising it.
        assert "libvips-dev" in error.hint
        assert "ALLOWED_IMAGE_EXTENSIONS" in error.hint

    def test_silent_when_there_is_no_decoder_at_all(self, poisoned_pyvips):
        """E001 owns that state; repeating every extension would bury it."""
        with override_settings(
            STAPEL_CDN={"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".heic")}
        ):
            errors = check_submodule_binaries()
        assert not any(e.id == E004_IMAGE_FORMAT_UNDECODABLE for e in errors)

    def test_silent_when_images_not_enabled(self, monkeypatch):
        pytest.importorskip("pyvips")
        monkeypatch.setattr(
            "stapel_cdn.decoders.loadable_extensions",
            lambda: frozenset({".jpg"}),
        )
        with override_settings(
            STAPEL_CDN={
                "ENABLED_SUBMODULES": ("files",),
                "ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".heic"),
            }
        ):
            errors = check_submodule_binaries()
        assert not any(e.id == E004_IMAGE_FORMAT_UNDECODABLE for e in errors)


#: The libvips operation registry of a STOCK `pip install stapel-cdn[images]`
#: host — measured on python:3.12-slim with pyvips[binary] (libvips 8.18.6),
#: which is what a host that installs nothing beyond the extra actually gets.
#: The wheel bundles jpeg/png/gif/webp/libheif/tiff/svg and NOT the modular
#: bits: `bmpload` does not exist in libvips at all, and `magickload`,
#: `jxlload`, `jp2kload` need modules the wheel does not carry.
STOCK_PYVIPS_BINARY_OPERATIONS = frozenset(
    {"jpegload", "pngload", "gifload", "webpload", "heifload", "tiffload", "svgload"}
)


class TestShippedDefaultIsDecodable:
    """The defect this class exists for: the *shipped default* failed E004.

    `ALLOWED_IMAGE_EXTENSIONS` carried ``.bmp`` through 0.17.0. libvips has no
    native BMP reader — only `magickload`, from an ImageMagick module absent
    from the pyvips[binary] wheels — so a stock `pip install stapel-cdn[images]`
    host that had configured NOTHING failed `manage.py check` with E004 until
    it overrode the setting. E004 was right; the default was wrong. A default
    must be decodable by the deployment it defaults into, and that is a
    property of this repo, testable here rather than in someone's deploy.
    """

    def test_default_passes_the_probe_on_a_stock_pyvips_binary_build(self, monkeypatch):
        """Deterministic: the ambient CI libvips may be fatter than a wheel's.

        Stubs the operation registry rather than `loadable_extensions`, so
        `VIPS_LOADERS` — the extension -> loader table the default is judged
        against — is exercised for real.
        """
        pytest.importorskip("pyvips")
        monkeypatch.setattr(
            "stapel_cdn.decoders._operation_exists",
            lambda pyvips, name: name in STOCK_PYVIPS_BINARY_OPERATIONS,
        )
        # No override_settings: this is the DEFAULTS tuple as shipped.
        errors = check_submodule_binaries()
        undecodable = [e for e in errors if e.id == E004_IMAGE_FORMAT_UNDECODABLE]
        assert not undecodable, [e.msg for e in undecodable]

    def test_default_passes_the_probe_on_this_machines_build(self):
        """Belt to the braces: whatever libvips is present here reads it too."""
        pytest.importorskip("pyvips")
        errors = check_submodule_binaries()
        undecodable = [e for e in errors if e.id == E004_IMAGE_FORMAT_UNDECODABLE]
        assert not undecodable, [e.msg for e in undecodable]

    def test_bmp_is_still_probed_for_a_deployment_that_widens_the_list(
        self, monkeypatch
    ):
        """Dropping .bmp from the default did not drop it from the table.

        E004 keeps its job in the other direction: a host that adds .bmp back
        on a build without the ImageMagick module hears about it at boot.
        """
        pytest.importorskip("pyvips")
        monkeypatch.setattr(
            "stapel_cdn.decoders._operation_exists",
            lambda pyvips, name: name in STOCK_PYVIPS_BINARY_OPERATIONS,
        )
        with override_settings(
            STAPEL_CDN={"ALLOWED_IMAGE_EXTENSIONS": (".jpg", ".bmp")}
        ):
            errors = check_submodule_binaries()
        (error,) = [e for e in errors if e.id == E004_IMAGE_FORMAT_UNDECODABLE]
        assert ".bmp" in error.msg


class TestVideoProbeOptIn:
    def test_clean_when_video_not_enabled(self):
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("images",)}):
            errors = check_submodule_binaries()
        assert not any(e.id == E002_VIDEO_BINARY_MISSING for e in errors)

    def test_errors_when_enabled_and_ffmpeg_missing(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("images", "video")}):
            errors = check_submodule_binaries()
        video_errors = [e for e in errors if e.id == E002_VIDEO_BINARY_MISSING]
        assert len(video_errors) == 1
        assert "ffmpeg" in video_errors[0].msg
        assert "ENABLED_SUBMODULES" in video_errors[0].msg

    def test_clean_when_enabled_and_ffmpeg_present(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("images", "video")}):
            errors = check_submodule_binaries()
        assert not any(e.id == E002_VIDEO_BINARY_MISSING for e in errors)


class TestRecordingsProbeOptIn:
    def test_clean_when_recordings_not_enabled(self):
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("images",)}):
            errors = check_submodule_binaries()
        assert not any(e.id == E003_RECORDINGS_BINARY_MISSING for e in errors)

    def test_errors_when_enabled_and_ffmpeg_missing(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("images", "recordings")}):
            errors = check_submodule_binaries()
        recordings_errors = [e for e in errors if e.id == E003_RECORDINGS_BINARY_MISSING]
        assert len(recordings_errors) == 1
        assert "passthrough" in recordings_errors[0].msg

    def test_clean_when_enabled_and_ffmpeg_present(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ("images", "recordings")}):
            errors = check_submodule_binaries()
        assert not any(e.id == E003_RECORDINGS_BINARY_MISSING for e in errors)


class TestVariantQueues:
    """W008 — the queue names are settings now, and the check is honest.

    The class this closes was live: ``@shared_task(queue="thumbnails")`` and
    ``queue="previews"`` were literals in ``tasks.py``, a fleet that shards
    per service ran no worker on either, and every upload answered 201 with a
    full ladder of variant URLs that 404'd forever.
    """

    def test_silent_on_the_default_posture(self):
        # Nothing set: both sends carry no `queue` option, so they land on
        # the app's own default queue and there is nothing to corroborate.
        assert check_variant_queues() == []

    @override_settings(STAPEL_CDN={"THUMBNAILS_QUEUE": "thumbnails"})
    def test_warns_on_a_queue_nothing_corroborates(self):
        findings = [w for w in check_variant_queues()
                    if w.id == W008_VARIANT_QUEUE_UNPROVEN]
        assert len(findings) == 1
        assert "THUMBNAILS_QUEUE" in findings[0].msg
        assert "'thumbnails'" in findings[0].msg
        # Honest about the half it cannot see, and where that half lives.
        assert "stapel-adoption-lint" in findings[0].hint

    @override_settings(
        STAPEL_CDN={"THUMBNAILS_QUEUE": "thumbs", "PREVIEWS_QUEUE": "prev"},
    )
    def test_reports_both_settings_in_one_finding(self):
        findings = [w for w in check_variant_queues()
                    if w.id == W008_VARIANT_QUEUE_UNPROVEN]
        assert len(findings) == 1
        assert "'thumbs'" in findings[0].msg and "'prev'" in findings[0].msg

    @override_settings(
        STAPEL_CDN={"THUMBNAILS_QUEUE": "media"},
        CELERY_TASK_DEFAULT_QUEUE="media",
    )
    def test_silent_when_the_name_is_the_default_queue(self):
        assert not [w for w in check_variant_queues()
                    if w.id == W008_VARIANT_QUEUE_UNPROVEN]

    @override_settings(
        STAPEL_CDN={"PREVIEWS_QUEUE": "prev"},
        CELERY_TASK_QUEUES=["prev", "other"],
    )
    def test_silent_when_declared_in_task_queues(self):
        assert not [w for w in check_variant_queues()
                    if w.id == W008_VARIANT_QUEUE_UNPROVEN]

    @override_settings(
        STAPEL_CDN={"PREVIEWS_QUEUE": "prev"},
        CELERY_TASK_ROUTES={"stapel_cdn.tasks.generate_previews": {"queue": "prev"}},
    )
    def test_silent_when_pinned_in_task_routes(self):
        assert not [w for w in check_variant_queues()
                    if w.id == W008_VARIANT_QUEUE_UNPROVEN]

    @override_settings(STAPEL_CDN={"THUMBNAILS_QUEUE": "   "})
    def test_blank_is_not_a_queue_name(self):
        assert not [w for w in check_variant_queues()
                    if w.id == W008_VARIANT_QUEUE_UNPROVEN]

    def test_registered_under_the_module_tag(self):
        from django.core.checks import registry

        assert check_variant_queues in registry.registry.get_checks(
            include_deployment_checks=False
        )
