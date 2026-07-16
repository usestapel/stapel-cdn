"""
Endpoint smoke tests for upload views: generic files, avatars, typed images,
random image, plus the image upload validation matrix (size cap, extension
allowlist, spoofed content, decompression bomb).
"""
import pytest
from io import BytesIO
from unittest.mock import patch
from PIL import Image as PILImage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from stapel_cdn.models import File, Image, ImageType
from stapel_core.django.users.models import User


@pytest.fixture
def api_client():
    """Return an API client."""
    return APIClient()


@pytest.fixture
def user(db):
    """Create a test user."""
    return User.objects.create_user(
        username='uploader',
        email='uploader@example.com',
        password='testpass123'
    )


@pytest.fixture
def staff_user(db):
    """Create a staff user."""
    staff = User.objects.create_user(
        username='staffer',
        email='staffer@example.com',
        password='testpass123'
    )
    staff.is_staff = True
    staff.save(update_fields=['is_staff'])
    return staff


@pytest.fixture
def authenticated_client(api_client, user):
    """Return an authenticated API client."""
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def staff_client(user, staff_user):
    """Return a staff-authenticated API client."""
    client = APIClient()
    client.force_authenticate(user=staff_user)
    return client


def make_image_bytes(width=100, height=100, fmt='JPEG', color='red'):
    """Build raw image bytes."""
    mode = 'RGBA' if fmt == 'PNG' else 'RGB'
    img = PILImage.new(mode, (width, height), color=color)
    buffer = BytesIO()
    img.save(buffer, format=fmt)
    return buffer.getvalue()


def make_image_upload(name='photo.jpg', fmt='JPEG', width=100, height=100, color='red'):
    """Build a SimpleUploadedFile with real image content."""
    content_type = 'image/jpeg' if fmt == 'JPEG' else 'image/png'
    return SimpleUploadedFile(
        name=name,
        content=make_image_bytes(width, height, fmt, color),
        content_type=content_type,
    )


@pytest.mark.django_db
class TestGenericFileUploadView:
    """Tests for GenericFileUploadView (documents/archives)."""

    url = '/cdn/api/v1/upload/file/'

    def test_unauthenticated(self, api_client):
        response = api_client.post(self.url, {}, format='multipart')
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_no_file(self, authenticated_client):
        response = authenticated_client.post(self.url, {}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'No file provided'
        assert File.objects.count() == 0

    def test_oversize_rejected(self, authenticated_client):
        big = SimpleUploadedFile(
            name='big.pdf',
            content=b'0' * (50 * 1024 * 1024 + 1),
            content_type='application/pdf',
        )
        response = authenticated_client.post(self.url, {'file': big}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert File.objects.count() == 0

    def test_extension_rejected(self, authenticated_client):
        exe = SimpleUploadedFile(
            name='malware.exe',
            content=b'MZ binary',
            content_type='application/octet-stream',
        )
        response = authenticated_client.post(self.url, {'file': exe}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert File.objects.count() == 0

    def test_mime_type_rejected(self, authenticated_client):
        spoofed = SimpleUploadedFile(
            name='page.pdf',
            content=b'<html></html>',
            content_type='text/html',
        )
        response = authenticated_client.post(self.url, {'file': spoofed}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert File.objects.count() == 0

    def test_upload_success(self, authenticated_client, user):
        doc = SimpleUploadedFile(
            name='report.pdf',
            content=b'%PDF-1.4 test document',
            content_type='application/pdf',
        )
        response = authenticated_client.post(self.url, {'file': doc}, format='multipart')
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['message'] == 'File uploaded successfully'
        payload = response.data['file']
        assert payload['original_filename'] == 'report.pdf'
        assert payload['file_extension'] == '.pdf'
        assert payload['mime_type'] == 'application/pdf'
        assert payload['prefix'] == f"file/{payload['file_hash']}"

        file_obj = File.objects.get(id=payload['id'])
        assert file_obj.uploaded_by == user
        assert file_obj.original_size == len(b'%PDF-1.4 test document')
        # Binary really stored on disk
        with file_obj.original.open('rb') as fh:
            assert fh.read() == b'%PDF-1.4 test document'

    def test_dedup_same_content_twice(self, authenticated_client):
        content = b'%PDF-1.4 duplicate check'
        first = SimpleUploadedFile('a.pdf', content, content_type='application/pdf')
        second = SimpleUploadedFile('b.pdf', content, content_type='application/pdf')

        response1 = authenticated_client.post(self.url, {'file': first}, format='multipart')
        assert response1.status_code == status.HTTP_201_CREATED

        response2 = authenticated_client.post(self.url, {'file': second}, format='multipart')
        assert response2.status_code == status.HTTP_200_OK
        assert response2.data['message'] == 'File already exists'
        assert response2.data['file']['id'] == response1.data['file']['id']
        # Dedup means the second body was never persisted
        assert File.objects.count() == 1
        assert File.objects.get().original_filename == 'a.pdf'

    def test_create_failure_returns_500(self, authenticated_client):
        doc = SimpleUploadedFile('x.txt', b'text', content_type='text/plain')
        with patch('stapel_cdn.views.File.objects.create', side_effect=RuntimeError('db down')):
            response = authenticated_client.post(self.url, {'file': doc}, format='multipart')
        assert response.status_code == 500
        assert 'error' in response.data


@pytest.mark.django_db
class TestAvatarUploadView:
    """Tests for AvatarUploadView."""

    url = '/cdn/api/v1/upload/avatar/'

    def test_upload_success_sets_avatar_type(self, authenticated_client, user):
        response = authenticated_client.post(
            self.url, {'file': make_image_upload('me.jpg')}, format='multipart'
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['message'] == 'Avatar uploaded successfully'
        assert response.data['image']['type'] == ImageType.AVATAR

        image = Image.objects.get(id=response.data['image']['id'])
        assert image.type == ImageType.AVATAR
        assert image.uploaded_by == user
        assert image.original_filename == 'me.jpg'
        assert image.original_size == len(make_image_bytes())

    def test_dedup_same_content_twice(self, authenticated_client):
        content = make_image_bytes(color='purple')
        f1 = SimpleUploadedFile('a.jpg', content, content_type='image/jpeg')
        f2 = SimpleUploadedFile('b.jpg', content, content_type='image/jpeg')

        response1 = authenticated_client.post(self.url, {'file': f1}, format='multipart')
        assert response1.status_code == status.HTTP_201_CREATED

        response2 = authenticated_client.post(self.url, {'file': f2}, format='multipart')
        assert response2.status_code == status.HTTP_200_OK
        assert response2.data['message'] == 'Avatar already exists'
        assert response2.data['image']['id'] == response1.data['image']['id']
        assert Image.objects.filter(type=ImageType.AVATAR).count() == 1

    def test_spoofed_content_rejected(self, authenticated_client):
        spoofed = SimpleUploadedFile(
            'evil.jpg', b'<script>alert(1)</script>', content_type='image/jpeg'
        )
        response = authenticated_client.post(self.url, {'file': spoofed}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert Image.objects.count() == 0

    def test_create_failure_returns_500(self, authenticated_client):
        with patch('stapel_cdn.views.Image.objects.create', side_effect=RuntimeError('db down')):
            response = authenticated_client.post(
                self.url, {'file': make_image_upload('c.jpg', color='cyan')}, format='multipart'
            )
        assert response.status_code == 500
        assert 'error' in response.data


@pytest.mark.django_db
class TestTypedImageUploadView:
    """Tests for TypedImageUploadView."""

    def test_invalid_type_rejected(self, authenticated_client):
        response = authenticated_client.post(
            '/cdn/api/v1/images/banner/upload/',
            {'file': make_image_upload()},
            format='multipart',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Invalid image type'
        assert Image.objects.count() == 0

    def test_upload_success_with_type(self, authenticated_client, user):
        response = authenticated_client.post(
            '/cdn/api/v1/images/avatar/upload/',
            {'file': make_image_upload('typed.png', fmt='PNG', color='blue')},
            format='multipart',
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['message'] == 'Image uploaded successfully'
        assert response.data['image']['type'] == 'avatar'
        image = Image.objects.get(id=response.data['image']['id'])
        assert image.type == 'avatar'
        assert image.uploaded_by == user

    def test_dedup_same_content_and_type(self, authenticated_client):
        content = make_image_bytes(color='orange')
        f1 = SimpleUploadedFile('t1.jpg', content, content_type='image/jpeg')
        f2 = SimpleUploadedFile('t2.jpg', content, content_type='image/jpeg')

        response1 = authenticated_client.post(
            '/cdn/api/v1/images/product/upload/', {'file': f1}, format='multipart'
        )
        assert response1.status_code == status.HTTP_201_CREATED

        response2 = authenticated_client.post(
            '/cdn/api/v1/images/product/upload/', {'file': f2}, format='multipart'
        )
        assert response2.status_code == status.HTTP_200_OK
        assert response2.data['message'] == 'Image already exists'
        assert response2.data['image']['id'] == response1.data['image']['id']
        assert Image.objects.count() == 1

    def test_spoofed_content_rejected(self, authenticated_client):
        spoofed = SimpleUploadedFile('fake.jpg', b'plain text', content_type='image/jpeg')
        response = authenticated_client.post(
            '/cdn/api/v1/images/product/upload/', {'file': spoofed}, format='multipart'
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert Image.objects.count() == 0

    def test_create_failure_returns_500(self, authenticated_client):
        with patch('stapel_cdn.views.Image.objects.create', side_effect=RuntimeError('db down')):
            response = authenticated_client.post(
                '/cdn/api/v1/images/product/upload/',
                {'file': make_image_upload('d.jpg', color='pink')},
                format='multipart',
            )
        assert response.status_code == 500


@pytest.mark.django_db
class TestImageUploadValidationMatrix:
    """Validation matrix for image uploads: size, extension, content, bombs."""

    url = '/cdn/api/v1/upload/image/'

    def test_oversize_rejected_with_413(self, authenticated_client):
        with override_settings(STAPEL_CDN={'MAX_IMAGE_SIZE': 10}):
            response = authenticated_client.post(
                self.url, {'file': make_image_upload('big.jpg')}, format='multipart'
            )
        assert response.status_code == 413
        assert response.data['error'] == 'File is too large'
        assert Image.objects.count() == 0

    def test_wrong_extension_rejected(self, authenticated_client):
        bad = SimpleUploadedFile('image.exe', make_image_bytes(), content_type='image/jpeg')
        response = authenticated_client.post(self.url, {'file': bad}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert Image.objects.count() == 0

    def test_video_extension_rejected_by_image_check(self, authenticated_client):
        # .mp4 passes the shared upload serializer but must fail the image allowlist
        fake = SimpleUploadedFile('clip.mp4', b'\x00\x00\x00\x1cftypisom', content_type='video/mp4')
        response = authenticated_client.post(self.url, {'file': fake}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert Image.objects.count() == 0

    def test_spoofed_content_rejected(self, authenticated_client):
        spoofed = SimpleUploadedFile(
            'page.jpg', b'<html><body>not an image</body></html>', content_type='image/jpeg'
        )
        response = authenticated_client.post(self.url, {'file': spoofed}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert Image.objects.count() == 0

    def test_decompression_bomb_rejected(self, authenticated_client):
        # 100x100 = 10_000 px > 2 * 1_000 cap -> Pillow DecompressionBombError
        bomb = SimpleUploadedFile(
            'bomb.png', make_image_bytes(100, 100, fmt='PNG'), content_type='image/png'
        )
        with override_settings(STAPEL_CDN={'MAX_IMAGE_PIXELS': 1000}):
            response = authenticated_client.post(self.url, {'file': bomb}, format='multipart')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Unsupported file format'
        assert Image.objects.count() == 0

    def test_create_failure_returns_500(self, authenticated_client):
        with patch('stapel_cdn.views.Image.objects.create', side_effect=RuntimeError('db down')):
            response = authenticated_client.post(
                self.url, {'file': make_image_upload('e.jpg', color='navy')}, format='multipart'
            )
        assert response.status_code == 500

    def test_upload_response_envelope(self, authenticated_client):
        response = authenticated_client.post(
            self.url, {'file': make_image_upload('env.jpg', color='teal')}, format='multipart'
        )
        assert response.status_code == status.HTTP_201_CREATED
        image_payload = response.data['image']
        for key in ('id', 'file_hash', 'prefix', 'original_url', 'variant_720_url', 'is_processed'):
            assert key in image_payload
        assert image_payload['prefix'] == f"product/{image_payload['file_hash']}"
        assert image_payload['is_processed'] is False


@pytest.mark.django_db
class TestVideoUploadExtras:
    """Extra tests for VideoUploadView: DB effects, envelope, 500 path."""

    url = '/cdn/api/v1/upload/video/'

    def test_upload_persists_and_returns_envelope(self, authenticated_client, user):
        content = b'\x00\x00\x00\x1cftypisom-unique-video-1'
        video_file = SimpleUploadedFile('movie.mov', content, content_type='video/quicktime')
        response = authenticated_client.post(self.url, {'file': video_file}, format='multipart')
        assert response.status_code == status.HTTP_201_CREATED
        payload = response.data['video']
        assert payload['original_filename'] == 'movie.mov'
        assert payload['file_extension'] == '.mov'

        from stapel_cdn.models import Video
        video = Video.objects.get(id=payload['id'])
        assert video.uploaded_by == user
        assert video.original_size == len(content)

    def test_create_failure_returns_500(self, authenticated_client):
        video_file = SimpleUploadedFile(
            'boom.mp4', b'\x00\x00\x00\x1cftypisom-unique-video-2', content_type='video/mp4'
        )
        with patch('stapel_cdn.views.Video.objects.create', side_effect=RuntimeError('db down')):
            response = authenticated_client.post(self.url, {'file': video_file}, format='multipart')
        assert response.status_code == 500


@pytest.mark.django_db
class TestRandomImageView:
    """Tests for RandomImageView."""

    def test_requires_staff(self, authenticated_client):
        response = authenticated_client.get('/cdn/api/v1/images/product/random/')
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_invalid_type(self, staff_client):
        response = staff_client.get('/cdn/api/v1/images/banner/random/')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error'] == 'Invalid image type'

    def test_no_processed_images_404(self, staff_client):
        Image.objects.create(
            file_hash='0' * 64,
            original_filename='raw.jpg',
            file_extension='.jpg',
            original_width=10,
            original_height=10,
            original_size=100,
            is_processed=False,
        )
        response = staff_client.get('/cdn/api/v1/images/product/random/')
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data['error'] == 'No processed images found'

    def test_returns_processed_image(self, staff_client):
        image = Image.objects.create(
            file_hash='1' * 64,
            original_filename='done.jpg',
            file_extension='.jpg',
            original_width=800,
            original_height=600,
            original_size=1000,
            is_processed=True,
        )
        response = staff_client.get('/cdn/api/v1/images/product/random/')
        assert response.status_code == status.HTTP_200_OK
        assert response.data['id'] == image.id
        assert response.data['file_hash'] == '1' * 64
        assert response.data['prefix'] == f'product/{"1" * 64}'
        assert response.data['is_processed'] is True
