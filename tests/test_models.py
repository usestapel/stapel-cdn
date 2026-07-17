"""
Tests for CDN models.
"""
import logging
import sys

import pytest
from stapel_cdn.models import Image, Video
from stapel_core.django.users.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )


@pytest.mark.django_db
class TestImageModel:
    """Tests for Image model."""

    def test_create_image(self, user):
        """Test creating an image record."""
        image = Image.objects.create(
            file_hash='a' * 64,
            original_filename='test.jpg',
            file_extension='.jpg',
            original_width=800,
            original_height=600,
            original_size=12345,
            uploaded_by=user,
        )
        assert image.file_hash == 'a' * 64
        assert image.original_filename == 'test.jpg'
        assert not image.is_processed

    def test_image_str_method(self, user):
        """Test Image string representation."""
        image = Image.objects.create(
            file_hash='b' * 64,
            original_filename='photo.png',
            file_extension='.png',
            original_width=1920,
            original_height=1080,
            original_size=54321,
            uploaded_by=user,
        )
        assert 'photo.png' in str(image)
        assert 'bbbbbbbb' in str(image)

    def test_image_unique_hash(self, user):
        """Test that file_hash is unique."""
        Image.objects.create(
            file_hash='c' * 64,
            original_filename='first.jpg',
            file_extension='.jpg',
            original_width=100,
            original_height=100,
            original_size=1000,
            uploaded_by=user,
        )
        with pytest.raises(Exception):
            Image.objects.create(
                file_hash='c' * 64,
                original_filename='second.jpg',
                file_extension='.jpg',
                original_width=200,
                original_height=200,
                original_size=2000,
                uploaded_by=user,
            )


@pytest.mark.django_db
class TestImageDimensionFallbackLogging:
    """cdn-modularity.md §0.3/(б): the pyvips dimension-extraction fallback
    used to be a single broad ``except Exception: pass`` — indistinguishable
    from a deliberately tiny 1x1 image. Both failure modes must now log an
    honest ERROR naming the image and the cause."""

    def test_missing_pyvips_logs_error_and_falls_back_to_1x1(self, user, caplog, monkeypatch):
        monkeypatch.setitem(sys.modules, "pyvips", None)  # forces ImportError
        with caplog.at_level(logging.ERROR, logger="stapel_cdn.models"):
            image = Image.objects.create(
                file_hash="f" * 64,
                original_filename="nopyvips.jpg",
                file_extension=".jpg",
                original="product/" + "f" * 64 + "/nopyvips.jpg",
                original_size=111,
                uploaded_by=user,
            )
        assert image.original_width == 1
        assert image.original_height == 1
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "pyvips" in r.getMessage()]
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert "pyvips is not installed" in message
        assert "nopyvips.jpg" in message

    def test_unreadable_file_logs_error_and_falls_back_to_1x1(self, user, caplog):
        # No forced ImportError — pyvips is real, but the referenced file
        # doesn't exist on disk, so Image.new_from_file raises.
        with caplog.at_level(logging.ERROR, logger="stapel_cdn.models"):
            image = Image.objects.create(
                file_hash="0" * 64,
                original_filename="missing.jpg",
                file_extension=".jpg",
                original="product/" + "0" * 64 + "/missing.jpg",
                original_size=222,
                uploaded_by=user,
            )
        assert image.original_width == 1
        assert image.original_height == 1
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "pyvips" in r.getMessage()]
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert "pyvips failed to read dimensions" in message
        assert "missing.jpg" in message


@pytest.mark.django_db
class TestAudioModel:
    """Tests for the Audio model ("recordings" submodule) — passthrough
    storage always available (cdn-modularity.md §7.2)."""

    def test_create_audio(self, user):
        from stapel_cdn.models import Audio

        audio = Audio.objects.create(
            file_hash="a1" * 32,
            original_filename="voice.mp3",
            file_extension=".mp3",
            original_size=4096,
            uploaded_by=user,
        )
        assert audio.file_hash == "a1" * 32
        assert audio.is_compressed is False

    def test_audio_str_method(self, user):
        from stapel_cdn.models import Audio

        audio = Audio.objects.create(
            file_hash="b2" * 32,
            original_filename="memo.wav",
            file_extension=".wav",
            original_size=2048,
            uploaded_by=user,
        )
        assert "memo.wav" in str(audio)
        assert "b2b2b2b2" in str(audio)

    def test_audio_save_extracts_metadata_from_file(self, user):
        from django.core.files.base import ContentFile
        from stapel_cdn.models import Audio

        audio = Audio(
            original=ContentFile(b"fake-audio-bytes", name="clip.mp3"),
            uploaded_by=user,
        )
        audio.save()
        assert len(audio.file_hash) == 64
        assert audio.original_filename == "clip.mp3"
        assert audio.file_extension == ".mp3"
        assert audio.original_size == len(b"fake-audio-bytes")


@pytest.mark.django_db
class TestVideoModel:
    """Tests for Video model."""

    def test_create_video(self, user):
        """Test creating a video record."""
        video = Video.objects.create(
            file_hash='d' * 64,
            original_filename='test.mp4',
            file_extension='.mp4',
            original_size=1234567,
            uploaded_by=user,
        )
        assert video.file_hash == 'd' * 64
        assert video.original_filename == 'test.mp4'

    def test_video_str_method(self, user):
        """Test Video string representation."""
        video = Video.objects.create(
            file_hash='e' * 64,
            original_filename='clip.mp4',
            file_extension='.mp4',
            original_size=9999999,
            uploaded_by=user,
        )
        assert 'clip.mp4' in str(video)
