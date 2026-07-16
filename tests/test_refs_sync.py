"""
Tests for the refs/sync internal endpoint (RefSyncView), the apply_ref_sync
service function and the batch media-ref resolver.
"""
import pytest
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory
from stapel_cdn.models import File, Image, Video
from stapel_cdn.services import apply_ref_sync
from stapel_cdn.views import RefSyncView, _batch_resolve_media

IMG_HASH = 'd4' * 32
VID_HASH = 'e5' * 32
FIL_HASH = 'f6' * 32


def _make_image(file_hash=IMG_HASH, image_type='product', refs=None):
    return Image.objects.create(
        file_hash=file_hash,
        original_filename='pic.jpg',
        file_extension='.jpg',
        type=image_type,
        original_width=10,
        original_height=10,
        original_size=100,
        refs=refs or [],
    )


def _make_video(file_hash=VID_HASH, refs=None):
    return Video.objects.create(
        file_hash=file_hash,
        original_filename='clip.mp4',
        file_extension='.mp4',
        original_size=100,
        refs=refs or [],
    )


def _make_file(file_hash=FIL_HASH, refs=None):
    return File.objects.create(
        file_hash=file_hash,
        original_filename='doc.pdf',
        file_extension='.pdf',
        original_size=100,
        refs=refs or [],
    )


def _service_post(payload):
    """POST to RefSyncView as an internal service request."""
    factory = APIRequestFactory()
    request = factory.post('/cdn/api/v1/refs/sync/', payload, format='json')
    request.is_service_request = True
    return RefSyncView.as_view()(request)


@pytest.mark.django_db
class TestRefSyncView:
    """Tests for the refs/sync internal HTTP endpoint."""

    def test_rejected_without_service_marker(self):
        client = APIClient()
        response = client.post(
            '/cdn/api/v1/refs/sync/',
            {
                'service': 'shop', 'entity_type': 'product', 'entity_id': '1',
                'old_hashes': [], 'new_hashes': [],
            },
            format='json',
        )
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN
        )

    def test_add_ref_via_endpoint(self):
        image = _make_image()
        response = _service_post({
            'service': 'shop',
            'entity_type': 'product',
            'entity_id': '42',
            'old_hashes': [],
            'new_hashes': [f'product/{IMG_HASH}'],
        })
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {'added': 1, 'removed': 0, 'errors': []}
        image.refresh_from_db()
        assert image.refs == ['shop/product/42']

    def test_remove_ref_via_endpoint(self):
        video = _make_video(refs=['shop/product/7'])
        response = _service_post({
            'service': 'shop',
            'entity_type': 'product',
            'entity_id': '7',
            'old_hashes': [f'video/{VID_HASH}'],
            'new_hashes': [],
        })
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {'added': 0, 'removed': 1, 'errors': []}
        video.refresh_from_db()
        assert video.refs == []

    def test_unresolved_ref_reported(self):
        missing = f'product/{"9" * 64}'
        response = _service_post({
            'service': 'shop',
            'entity_type': 'product',
            'entity_id': '3',
            'old_hashes': [],
            'new_hashes': [missing],
        })
        assert response.status_code == status.HTTP_200_OK
        assert response.data['added'] == 0
        assert response.data['errors'] == [missing]

    def test_invalid_payload_rejected(self):
        response = _service_post({'service': 'shop'})
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestApplyRefSync:
    """Direct tests for the apply_ref_sync service function."""

    def test_noop_when_hashes_unchanged(self):
        image = _make_image(refs=['shop/product/1'])
        ref = f'product/{IMG_HASH}'
        result = apply_ref_sync(
            service='shop', entity_type='product', entity_id='1',
            old_hashes=[ref], new_hashes=[ref],
        )
        assert result == {'added': 0, 'removed': 0, 'errors': []}
        image.refresh_from_db()
        assert image.refs == ['shop/product/1']

    def test_add_across_all_media_types(self):
        image = _make_image()
        video = _make_video()
        file_obj = _make_file()
        result = apply_ref_sync(
            service='blog', entity_type='post', entity_id='5',
            old_hashes=[],
            new_hashes=[f'product/{IMG_HASH}', f'video/{VID_HASH}', f'file/{FIL_HASH}'],
        )
        assert result == {'added': 3, 'removed': 0, 'errors': []}
        for obj in (image, video, file_obj):
            obj.refresh_from_db()
            assert obj.refs == ['blog/post/5']

    def test_add_is_idempotent(self):
        image = _make_image(refs=['blog/post/5'])
        result = apply_ref_sync(
            service='blog', entity_type='post', entity_id='5',
            old_hashes=[], new_hashes=[f'product/{IMG_HASH}'],
        )
        assert result['added'] == 0
        image.refresh_from_db()
        assert image.refs == ['blog/post/5']

    def test_malformed_ref_reported_as_error(self):
        result = apply_ref_sync(
            service='blog', entity_type='post', entity_id='5',
            old_hashes=[], new_hashes=['garbage-without-slash'],
        )
        assert result == {'added': 0, 'removed': 0, 'errors': ['garbage-without-slash']}

    def test_remove_missing_ref_is_noop(self):
        image = _make_image(refs=[])
        result = apply_ref_sync(
            service='blog', entity_type='post', entity_id='5',
            old_hashes=[f'product/{IMG_HASH}'], new_hashes=[],
        )
        assert result['removed'] == 0
        image.refresh_from_db()
        assert image.refs == []


@pytest.mark.django_db
class TestBatchResolveMedia:
    """Tests for the views-level batch media reference resolver."""

    def test_resolves_all_types(self):
        image = _make_image()
        video = _make_video()
        file_obj = _make_file()
        refs = [f'product/{IMG_HASH}', f'video/{VID_HASH}', f'file/{FIL_HASH}']
        resolved = _batch_resolve_media(refs)
        assert resolved[refs[0]] == image
        assert resolved[refs[1]] == video
        assert resolved[refs[2]] == file_obj

    def test_skips_malformed_and_unknown_prefix(self):
        _make_image()
        resolved = _batch_resolve_media([
            'no-slash-here',
            'too/many/parts',
            f'banner/{IMG_HASH}',
        ])
        assert resolved == {}

    def test_missing_hashes_absent_from_result(self):
        resolved = _batch_resolve_media([
            f'product/{"0" * 64}', f'video/{"0" * 64}', f'file/{"0" * 64}'
        ])
        assert resolved == {}

    def test_type_mismatch_not_resolved(self):
        _make_image(image_type='product')
        resolved = _batch_resolve_media([f'avatar/{IMG_HASH}'])
        assert resolved == {}

    def test_for_update_locks_rows(self):
        from django.db import transaction

        image = _make_image()
        video = _make_video()
        file_obj = _make_file()
        refs = [f'product/{IMG_HASH}', f'video/{VID_HASH}', f'file/{FIL_HASH}']
        with transaction.atomic():
            resolved = _batch_resolve_media(refs, for_update=True)
        assert set(resolved.values()) == {image, video, file_obj}
