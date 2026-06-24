"""
Tests for CDN serializers.
"""
import pytest
from stapel_cdn.serializers import (
    ImageSerializer,
    VideoSerializer,
    FileUploadSerializer,
    FileExistsSerializer,
    FileExistsResponseSerializer,
)
from stapel_cdn.models import Image, Video
from stapel_core.django.users.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from io import BytesIO
from PIL import Image as PILImage


@pytest.fixture
def user(db):
    """Create a test user."""
    return User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )


@pytest.fixture
def sample_image(user):
    """Create a sample image instance."""
    return Image(
        file_hash='a' * 64,
        original_filename='test.jpg',
        file_extension='.jpg',
        original_width=1920,
        original_height=1080,
        original_size=2048576,
        is_processed=True,
        uploaded_by=user,
    )


@pytest.fixture
def sample_video(user):
    """Create a sample video instance."""
    return Video(
        file_hash='b' * 64,
        original_filename='test.mp4',
        file_extension='.mp4',
        original_width=1920,
        original_height=1080,
        original_size=10485760,
        duration=120.5,
        is_processed=False,
        uploaded_by=user,
    )


@pytest.mark.django_db
class TestImageSerializer:
    """Tests for ImageSerializer."""

    def test_image_serializer_fields(self, user):
        """Test that ImageSerializer contains expected fields."""
        image = Image.objects.create(
            file_hash='c' * 64,
            original_filename='photo.jpg',
            file_extension='.jpg',
            original_width=800,
            original_height=600,
            original_size=12345,
            uploaded_by=user,
        )
        serializer = ImageSerializer(image)
        data = serializer.data

        assert 'id' in data
        assert 'file_hash' in data
        assert 'original_filename' in data
        assert 'file_extension' in data
        assert 'original_width' in data
        assert 'original_height' in data
        assert 'original_size' in data
        assert 'original_url' in data
        assert 'variant_16_url' in data
        assert 'variant_32_url' in data
        assert 'variant_64_url' in data
        assert 'variant_120_url' in data
        assert 'variant_160_url' in data
        assert 'variant_240_url' in data
        assert 'variant_480_url' in data
        assert 'variant_720_url' in data
        assert 'variant_720_jpg_url' in data
        assert 'variant_1080_url' in data
        assert 'is_processed' in data
        assert 'uploaded_by' in data
        assert 'uploaded_by_username' in data
        assert 'created_at' in data
        assert 'updated_at' in data

    def test_image_serializer_uploaded_by_username(self, user):
        """Test that uploaded_by_username is correctly resolved."""
        image = Image.objects.create(
            file_hash='d' * 64,
            original_filename='photo.png',
            file_extension='.png',
            original_width=1000,
            original_height=800,
            original_size=54321,
            uploaded_by=user,
        )
        serializer = ImageSerializer(image)
        assert serializer.data['uploaded_by_username'] == 'testuser'

    def test_image_serializer_variant_urls(self, user):
        """Test that variant URLs are correctly generated."""
        image = Image.objects.create(
            file_hash='e' * 64,
            original_filename='img.webp',
            file_extension='.webp',
            original_width=500,
            original_height=400,
            original_size=10000,
            uploaded_by=user,
        )
        serializer = ImageSerializer(image)
        data = serializer.data

        assert data['variant_16_url'].endswith('/16.webp')
        assert data['variant_720_url'].endswith('/720.webp')
        assert data['variant_720_jpg_url'].endswith('/720.jpg')
        assert 'e' * 8 in data['variant_16_url']


@pytest.mark.django_db
class TestVideoSerializer:
    """Tests for VideoSerializer."""

    def test_video_serializer_fields(self, user):
        """Test that VideoSerializer contains expected fields."""
        video = Video.objects.create(
            file_hash='f' * 64,
            original_filename='movie.mp4',
            file_extension='.mp4',
            original_size=99999999,
            uploaded_by=user,
        )
        serializer = VideoSerializer(video)
        data = serializer.data

        assert 'id' in data
        assert 'file_hash' in data
        assert 'original_filename' in data
        assert 'file_extension' in data
        assert 'original_width' in data
        assert 'original_height' in data
        assert 'original_size' in data
        assert 'duration' in data
        assert 'original_url' in data
        assert 'variant_16p_url' in data
        assert 'variant_32p_url' in data
        assert 'variant_240p_url' in data
        assert 'variant_480p_url' in data
        assert 'variant_720p_url' in data
        assert 'variant_1080p_url' in data
        assert 'variant_2160p_url' in data
        assert 'is_processed' in data
        assert 'uploaded_by' in data
        assert 'uploaded_by_username' in data

    def test_video_serializer_uploaded_by_username(self, user):
        """Test that uploaded_by_username is correctly resolved."""
        video = Video.objects.create(
            file_hash='g' * 64,
            original_filename='clip.webm',
            file_extension='.webm',
            original_size=12345678,
            uploaded_by=user,
        )
        serializer = VideoSerializer(video)
        assert serializer.data['uploaded_by_username'] == 'testuser'

    def test_video_serializer_variant_urls_none(self, user):
        """Test that variant URLs are None when variants don't exist."""
        video = Video.objects.create(
            file_hash='h' * 64,
            original_filename='video.avi',
            file_extension='.avi',
            original_size=5000000,
            uploaded_by=user,
        )
        serializer = VideoSerializer(video)
        data = serializer.data

        assert data['variant_16p_url'] is None
        assert data['variant_720p_url'] is None


class TestFileUploadSerializer:
    """Tests for FileUploadSerializer."""

    def test_valid_jpeg_file(self):
        """Test that valid JPEG file passes validation."""
        img = PILImage.new('RGB', (100, 100), color='red')
        buffer = BytesIO()
        img.save(buffer, format='JPEG')
        buffer.seek(0)
        file = SimpleUploadedFile(
            name='test.jpg',
            content=buffer.read(),
            content_type='image/jpeg'
        )

        serializer = FileUploadSerializer(data={'file': file})
        assert serializer.is_valid()

    def test_valid_png_file(self):
        """Test that valid PNG file passes validation."""
        img = PILImage.new('RGBA', (50, 50), color='blue')
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        file = SimpleUploadedFile(
            name='test.png',
            content=buffer.read(),
            content_type='image/png'
        )

        serializer = FileUploadSerializer(data={'file': file})
        assert serializer.is_valid()

    def test_valid_mp4_file(self):
        """Test that valid MP4 file passes validation."""
        mp4_content = bytes([
            0x00, 0x00, 0x00, 0x1C, 0x66, 0x74, 0x79, 0x70,
        ])
        file = SimpleUploadedFile(
            name='video.mp4',
            content=mp4_content,
            content_type='video/mp4'
        )

        serializer = FileUploadSerializer(data={'file': file})
        assert serializer.is_valid()

    def test_invalid_file_type(self):
        """Test that invalid file type fails validation."""
        file = SimpleUploadedFile(
            name='malware.exe',
            content=b'bad content',
            content_type='application/octet-stream'
        )

        serializer = FileUploadSerializer(data={'file': file})
        assert not serializer.is_valid()
        assert 'file' in serializer.errors

    def test_missing_file(self):
        """Test that missing file fails validation."""
        serializer = FileUploadSerializer(data={})
        assert not serializer.is_valid()
        assert 'file' in serializer.errors


class TestFileExistsSerializer:
    """Tests for FileExistsSerializer."""

    def test_valid_hash(self):
        """Test that valid file_hash passes validation."""
        serializer = FileExistsSerializer(data={'file_hash': 'a' * 64})
        assert serializer.is_valid()
        assert serializer.validated_data['file_hash'] == 'a' * 64

    def test_missing_hash(self):
        """Test that missing file_hash fails validation."""
        serializer = FileExistsSerializer(data={})
        assert not serializer.is_valid()
        assert 'file_hash' in serializer.errors

    def test_empty_hash(self):
        """Test that empty file_hash fails validation."""
        serializer = FileExistsSerializer(data={'file_hash': ''})
        assert not serializer.is_valid()

    def test_hash_too_long(self):
        """Test that hash exceeding max_length fails validation."""
        serializer = FileExistsSerializer(data={'file_hash': 'a' * 65})
        assert not serializer.is_valid()


class TestResponseSerializers:
    """Tests for response serializers."""

    def test_file_exists_response_serializer_found(self):
        """Test FileExistsResponseSerializer when file exists."""
        data = {
            'exists': True,
            'type': 'image',
            'file': {'id': '123', 'file_hash': 'abc'}
        }
        serializer = FileExistsResponseSerializer(data=data)
        assert serializer.is_valid()

    def test_file_exists_response_serializer_not_found(self):
        """Test FileExistsResponseSerializer when file doesn't exist."""
        data = {
            'exists': False,
            'type': None,
            'file': None
        }
        serializer = FileExistsResponseSerializer(data=data)
        assert serializer.is_valid()
