"""
Tests for CDN models.
"""
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
