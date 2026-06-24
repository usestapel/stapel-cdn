"""
Tests for CDN views.
"""
import pytest
from io import BytesIO
from unittest.mock import patch, MagicMock
from PIL import Image as PILImage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from stapel_cdn.models import Image, Video
from stapel_core.django.users.models import User


@pytest.fixture
def api_client():
    """Return an API client."""
    return APIClient()


@pytest.fixture
def user(db):
    """Create a test user."""
    return User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )


@pytest.fixture
def authenticated_client(api_client, user):
    """Return an authenticated API client."""
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def sample_image_file():
    """Create a sample JPEG image file for testing."""
    img = PILImage.new('RGB', (100, 100), color='red')
    buffer = BytesIO()
    img.save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(
        name='test_image.jpg',
        content=buffer.read(),
        content_type='image/jpeg'
    )


@pytest.fixture
def sample_png_file():
    """Create a sample PNG image file for testing."""
    img = PILImage.new('RGBA', (200, 150), color='blue')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return SimpleUploadedFile(
        name='test_image.png',
        content=buffer.read(),
        content_type='image/png'
    )


@pytest.fixture
def sample_video_file():
    """Create a sample video file for testing (fake MP4 header)."""
    mp4_header = bytes([
        0x00, 0x00, 0x00, 0x1C,
        0x66, 0x74, 0x79, 0x70,
        0x69, 0x73, 0x6F, 0x6D,
        0x00, 0x00, 0x02, 0x00,
        0x69, 0x73, 0x6F, 0x6D,
        0x69, 0x73, 0x6F, 0x32,
        0x6D, 0x70, 0x34, 0x31,
    ])
    return SimpleUploadedFile(
        name='test_video.mp4',
        content=mp4_header,
        content_type='video/mp4'
    )


@pytest.fixture
def invalid_file():
    """Create an invalid file type for testing."""
    return SimpleUploadedFile(
        name='test.exe',
        content=b'not a valid file',
        content_type='application/octet-stream'
    )


@pytest.mark.django_db
class TestImageUploadView:
    """Tests for ImageUploadView."""

    def test_upload_image_unauthenticated(self, api_client, sample_image_file):
        """Test that unauthenticated users cannot upload images."""
        response = api_client.post(
            '/cdn/api/upload/image/',
            {'file': sample_image_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @patch('stapel_cdn.models.generate_image_variants_on_save')
    def test_upload_image_success(self, mock_signal, authenticated_client, sample_image_file):
        """Test successful image upload."""
        response = authenticated_client.post(
            '/cdn/api/upload/image/',
            {'file': sample_image_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert 'image' in response.data
        assert response.data['message'] == 'Image uploaded successfully'
        assert 'id' in response.data['image']
        assert 'file_hash' in response.data['image']

    @patch('stapel_cdn.models.generate_image_variants_on_save')
    def test_upload_duplicate_image(self, mock_signal, authenticated_client, user):
        """Test uploading a duplicate image returns existing record."""
        # Create first image
        img = PILImage.new('RGB', (100, 100), color='green')
        buffer = BytesIO()
        img.save(buffer, format='JPEG')
        buffer.seek(0)
        file1 = SimpleUploadedFile(
            name='first.jpg',
            content=buffer.read(),
            content_type='image/jpeg'
        )

        response1 = authenticated_client.post(
            '/cdn/api/upload/image/',
            {'file': file1},
            format='multipart'
        )
        assert response1.status_code == status.HTTP_201_CREATED

        # Upload same content again
        buffer.seek(0)
        file2 = SimpleUploadedFile(
            name='second.jpg',
            content=buffer.getvalue(),
            content_type='image/jpeg'
        )

        response2 = authenticated_client.post(
            '/cdn/api/upload/image/',
            {'file': file2},
            format='multipart'
        )
        assert response2.status_code == status.HTTP_200_OK
        assert response2.data['message'] == 'Image already exists'
        assert response2.data['image']['id'] == response1.data['image']['id']

    def test_upload_invalid_file_type(self, authenticated_client, invalid_file):
        """Test uploading an invalid file type returns error."""
        response = authenticated_client.post(
            '/cdn/api/upload/image/',
            {'file': invalid_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'error' in response.data or 'file' in response.data

    def test_upload_no_file(self, authenticated_client):
        """Test uploading without a file returns error."""
        response = authenticated_client.post(
            '/cdn/api/upload/image/',
            {},
            format='multipart'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_upload_video_to_image_endpoint(self, authenticated_client, sample_video_file):
        """Test uploading a video to image endpoint returns error."""
        response = authenticated_client.post(
            '/cdn/api/upload/image/',
            {'file': sample_video_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'error' in response.data or 'file' in response.data


@pytest.mark.django_db
class TestVideoUploadView:
    """Tests for VideoUploadView."""

    def test_upload_video_unauthenticated(self, api_client, sample_video_file):
        """Test that unauthenticated users cannot upload videos."""
        response = api_client.post(
            '/cdn/api/upload/video/',
            {'file': sample_video_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @patch('stapel_cdn.models.generate_video_variants_on_save')
    def test_upload_video_success(self, mock_signal, authenticated_client, sample_video_file):
        """Test successful video upload."""
        response = authenticated_client.post(
            '/cdn/api/upload/video/',
            {'file': sample_video_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert 'video' in response.data
        assert 'variant generation not yet implemented' in response.data['message']

    @patch('stapel_cdn.models.generate_video_variants_on_save')
    def test_upload_duplicate_video(self, mock_signal, authenticated_client):
        """Test uploading a duplicate video returns existing record."""
        mp4_content = bytes([
            0x00, 0x00, 0x00, 0x1C, 0x66, 0x74, 0x79, 0x70,
            0x69, 0x73, 0x6F, 0x6D, 0x00, 0x00, 0x02, 0x00,
            0x69, 0x73, 0x6F, 0x6D, 0x69, 0x73, 0x6F, 0x32,
            0x6D, 0x70, 0x34, 0x31,
        ])

        file1 = SimpleUploadedFile(
            name='first.mp4',
            content=mp4_content,
            content_type='video/mp4'
        )

        response1 = authenticated_client.post(
            '/cdn/api/upload/video/',
            {'file': file1},
            format='multipart'
        )
        assert response1.status_code == status.HTTP_201_CREATED

        file2 = SimpleUploadedFile(
            name='second.mp4',
            content=mp4_content,
            content_type='video/mp4'
        )

        response2 = authenticated_client.post(
            '/cdn/api/upload/video/',
            {'file': file2},
            format='multipart'
        )
        assert response2.status_code == status.HTTP_200_OK
        assert response2.data['message'] == 'Video already exists'

    def test_upload_image_to_video_endpoint(self, authenticated_client, sample_image_file):
        """Test uploading an image to video endpoint returns error."""
        response = authenticated_client.post(
            '/cdn/api/upload/video/',
            {'file': sample_image_file},
            format='multipart'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'error' in response.data

    def test_upload_no_file(self, authenticated_client):
        """Test uploading without a file returns error."""
        response = authenticated_client.post(
            '/cdn/api/upload/video/',
            {},
            format='multipart'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestFileExistsView:
    """Tests for FileExistsView."""

    def test_file_exists_unauthenticated(self, api_client):
        """Test that unauthenticated users cannot check file existence."""
        response = api_client.get(
            '/cdn/api/file/exists/',
            {'file_hash': 'a' * 64}
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_file_exists_get_missing_hash(self, authenticated_client):
        """Test GET without file_hash parameter."""
        response = authenticated_client.get('/cdn/api/file/exists/')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'file_hash parameter is required' in response.data['error']

    def test_file_exists_get_not_found(self, authenticated_client):
        """Test GET for non-existent file."""
        response = authenticated_client.get(
            '/cdn/api/file/exists/',
            {'file_hash': 'a' * 64}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is False
        assert response.data['type'] is None
        assert response.data['file'] is None

    def test_file_exists_get_image_found(self, authenticated_client, user):
        """Test GET when image exists."""
        image = Image.objects.create(
            file_hash='b' * 64,
            original_filename='test.jpg',
            file_extension='.jpg',
            original_width=800,
            original_height=600,
            original_size=12345,
            uploaded_by=user,
        )

        response = authenticated_client.get(
            '/cdn/api/file/exists/',
            {'file_hash': 'b' * 64}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'image'
        assert response.data['file']['id'] == image.id

    def test_file_exists_get_video_found(self, authenticated_client, user):
        """Test GET when video exists."""
        video = Video.objects.create(
            file_hash='c' * 64,
            original_filename='test.mp4',
            file_extension='.mp4',
            original_size=123456,
            uploaded_by=user,
        )

        response = authenticated_client.get(
            '/cdn/api/file/exists/',
            {'file_hash': 'c' * 64}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'video'
        assert response.data['file']['id'] == video.id

    def test_file_exists_post_not_found(self, authenticated_client):
        """Test POST for non-existent file."""
        response = authenticated_client.post(
            '/cdn/api/file/exists/',
            {'file_hash': 'd' * 64},
            format='json'
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is False

    def test_file_exists_post_image_found(self, authenticated_client, user):
        """Test POST when image exists."""
        image = Image.objects.create(
            file_hash='e' * 64,
            original_filename='test.png',
            file_extension='.png',
            original_width=1920,
            original_height=1080,
            original_size=54321,
            uploaded_by=user,
        )

        response = authenticated_client.post(
            '/cdn/api/file/exists/',
            {'file_hash': 'e' * 64},
            format='json'
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'image'
        assert response.data['file']['id'] == image.id

    def test_file_exists_post_missing_hash(self, authenticated_client):
        """Test POST without file_hash."""
        response = authenticated_client.post(
            '/cdn/api/file/exists/',
            {},
            format='json'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_file_exists_post_video_found(self, authenticated_client, user):
        """Test POST when video exists."""
        video = Video.objects.create(
            file_hash='f' * 64,
            original_filename='clip.mp4',
            file_extension='.mp4',
            original_size=999999,
            uploaded_by=user,
        )

        response = authenticated_client.post(
            '/cdn/api/file/exists/',
            {'file_hash': 'f' * 64},
            format='json'
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'video'
        assert response.data['file']['id'] == video.id
