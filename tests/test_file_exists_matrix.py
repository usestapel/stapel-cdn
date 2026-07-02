"""
FileExistsView GET+POST matrix: own uploads vs foreign (another user's)
uploads across image, video and generic file types.
"""
import pytest
from rest_framework import status
from rest_framework.test import APIClient
from stapel_cdn.models import File, Image, Video
from stapel_core.django.users.models import User

IMAGE_HASH = 'a1' * 32
VIDEO_HASH = 'b2' * 32
FILE_HASH = 'c3' * 32


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        username='owner', email='owner@example.com', password='testpass123'
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username='other', email='other@example.com', password='testpass123'
    )


@pytest.fixture
def owner_client(owner):
    client = APIClient()
    client.force_authenticate(user=owner)
    return client


@pytest.fixture
def other_client(other_user):
    client = APIClient()
    client.force_authenticate(user=other_user)
    return client


@pytest.fixture
def media(owner):
    """One image, one video and one generic file, all owned by ``owner``."""
    image = Image.objects.create(
        file_hash=IMAGE_HASH,
        original_filename='pic.jpg',
        file_extension='.jpg',
        original_width=100,
        original_height=100,
        original_size=1000,
        uploaded_by=owner,
    )
    video = Video.objects.create(
        file_hash=VIDEO_HASH,
        original_filename='clip.mp4',
        file_extension='.mp4',
        original_size=2000,
        uploaded_by=owner,
    )
    file_obj = File.objects.create(
        file_hash=FILE_HASH,
        original_filename='doc.pdf',
        file_extension='.pdf',
        mime_type='application/pdf',
        original_size=3000,
        uploaded_by=owner,
    )
    return {'image': image, 'video': video, 'file': file_obj}


def _get(client, file_hash):
    return client.get('/cdn/api/file/exists/', {'file_hash': file_hash})


def _post(client, file_hash):
    return client.post('/cdn/api/file/exists/', {'file_hash': file_hash}, format='json')


@pytest.mark.django_db
class TestFileExistsOwnUploads:
    """Owner sees their own uploads via both GET and POST."""

    @pytest.mark.parametrize('method', [_get, _post])
    def test_own_image_found(self, method, owner_client, media):
        response = method(owner_client, IMAGE_HASH)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'image'
        assert response.data['file']['id'] == media['image'].id
        assert response.data['file']['file_hash'] == IMAGE_HASH

    @pytest.mark.parametrize('method', [_get, _post])
    def test_own_video_found(self, method, owner_client, media):
        response = method(owner_client, VIDEO_HASH)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'video'
        assert response.data['file']['id'] == media['video'].id

    @pytest.mark.parametrize('method', [_get, _post])
    def test_own_generic_file_found(self, method, owner_client, media):
        response = method(owner_client, FILE_HASH)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is True
        assert response.data['type'] == 'file'
        assert response.data['file']['id'] == media['file'].id
        assert response.data['file']['prefix'] == f'file/{FILE_HASH}'


@pytest.mark.django_db
class TestFileExistsForeignUploads:
    """Another user's uploads are invisible: exists=False for all types."""

    @pytest.mark.parametrize('method', [_get, _post])
    @pytest.mark.parametrize('file_hash', [IMAGE_HASH, VIDEO_HASH, FILE_HASH])
    def test_foreign_upload_not_found(self, method, file_hash, other_client, media):
        response = method(other_client, file_hash)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['exists'] is False
        assert response.data['type'] is None
        assert response.data['file'] is None
