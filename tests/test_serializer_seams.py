"""
Tests for the request/response serializer seams on CDN APIViews.
"""
import pytest
from rest_framework import serializers, status
from rest_framework.test import APIRequestFactory, force_authenticate
from stapel_cdn import views
from stapel_cdn.models import Image
from stapel_cdn.serializers import (
    FileExistsResponseSerializer,
    FileExistsSerializer,
    FileUploadResponseSerializer,
    FileUploadSerializer,
    ImageSerializer,
    ImageUploadResponseSerializer,
    RefSyncRequestSerializer,
    RefSyncResponseSerializer,
    VideoUploadResponseSerializer,
)
from stapel_core.django.users.models import User

EXPECTED_SEAMS = {
    views.ImageUploadView: (FileUploadSerializer, ImageUploadResponseSerializer),
    views.VideoUploadView: (FileUploadSerializer, VideoUploadResponseSerializer),
    views.FileExistsView: (FileExistsSerializer, FileExistsResponseSerializer),
    views.AvatarUploadView: (FileUploadSerializer, ImageUploadResponseSerializer),
    views.TypedImageUploadView: (FileUploadSerializer, ImageUploadResponseSerializer),
    views.RandomImageView: (None, ImageSerializer),
    views.RefSyncView: (RefSyncRequestSerializer, RefSyncResponseSerializer),
    views.GenericFileUploadView: (None, FileUploadResponseSerializer),
}


@pytest.mark.parametrize('view_class', list(EXPECTED_SEAMS))
def test_every_view_exposes_serializer_seams(view_class):
    request_class, response_class = EXPECTED_SEAMS[view_class]
    view = view_class()
    assert view.request_serializer_class is request_class
    assert view.response_serializer_class is response_class
    assert view.get_request_serializer_class() is request_class
    assert view.get_response_serializer_class() is response_class


class MinimalImageSerializer(serializers.ModelSerializer):
    """Stripped-down replacement serializer to prove the seam is honored."""

    class Meta:
        model = Image
        fields = ['id', 'file_hash']


class MinimalRandomImageView(views.RandomImageView):
    response_serializer_class = MinimalImageSerializer


@pytest.mark.django_db
def test_subclass_swapping_response_serializer_changes_envelope():
    staff = User.objects.create_user(
        username='seamstaff', email='seam@example.com', password='x'
    )
    staff.is_staff = True
    staff.save(update_fields=['is_staff'])

    image = Image.objects.create(
        file_hash='9a' * 32,
        original_filename='seam.jpg',
        file_extension='.jpg',
        original_width=10,
        original_height=10,
        original_size=100,
        is_processed=True,
    )

    factory = APIRequestFactory()

    # Default view: full ImageSerializer envelope
    request = factory.get('/cdn/api/images/product/random/')
    force_authenticate(request, user=staff)
    default_response = views.RandomImageView.as_view()(request, image_type='product')
    assert default_response.status_code == status.HTTP_200_OK
    assert 'original_url' in default_response.data
    assert 'variant_720_url' in default_response.data

    # Swapped serializer: only the minimal fields remain
    request = factory.get('/cdn/api/images/product/random/')
    force_authenticate(request, user=staff)
    swapped_response = MinimalRandomImageView.as_view()(request, image_type='product')
    assert swapped_response.status_code == status.HTTP_200_OK
    assert dict(swapped_response.data) == {'id': image.id, 'file_hash': '9a' * 32}
