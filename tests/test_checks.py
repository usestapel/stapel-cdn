"""Tests for stapel_cdn.checks (tag ``stapel_cdn``).

Per-submodule system checks (cdn-modularity.md §2.2/§3): images is core and
unconditional (E001); video/recordings are opt-in via
``STAPEL_CDN["ENABLED_SUBMODULES"]`` (E002/E003) and only probe ffmpeg once
enabled.
"""
import sys

import pytest
from django.test import override_settings

from stapel_cdn.checks import (
    E001_IMAGES_LIBRARY_MISSING,
    E002_VIDEO_BINARY_MISSING,
    E003_RECORDINGS_BINARY_MISSING,
    check_submodule_binaries,
)


@pytest.fixture
def poisoned_pyvips():
    """Force `import pyvips` to fail for the duration of a test."""
    saved = sys.modules.get("pyvips")
    sys.modules["pyvips"] = None
    yield
    sys.modules.pop("pyvips", None)
    if saved is not None:
        sys.modules["pyvips"] = saved


class TestImagesProbeUnconditional:
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

    def test_fires_regardless_of_enabled_submodules(self, poisoned_pyvips):
        # images is core — not gated by ENABLED_SUBMODULES at all.
        with override_settings(STAPEL_CDN={"ENABLED_SUBMODULES": ()}):
            errors = check_submodule_binaries()
        assert any(e.id == E001_IMAGES_LIBRARY_MISSING for e in errors)


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
