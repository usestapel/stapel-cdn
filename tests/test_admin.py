"""
Smoke tests for the admin configuration: display helpers, filters, actions.
"""
import os
import pytest
from unittest.mock import MagicMock, patch
from django.conf import settings
from django.contrib import admin as django_admin
from django.test import RequestFactory
from stapel_cdn.admin import (
    FileAdmin,
    ImageAdmin,
    OrphanFilter,
    VideoAdmin,
    format_file_size,
)
from stapel_cdn.models import File, Image, Video
from stapel_core.django.users.models import User


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username='adminuser', email='admin@example.com', password='testpass123'
    )


@pytest.fixture
def image(db):
    return Image.objects.create(
        file_hash='aa' * 32,
        original_filename='pic.jpg',
        file_extension='.jpg',
        original_width=800,
        original_height=600,
        original_size=2048,
        refs=['shop/product/1'],
    )


@pytest.fixture
def video(db):
    return Video.objects.create(
        file_hash='bb' * 32,
        original_filename='clip.mp4',
        file_extension='.mp4',
        original_size=5 * 1024 * 1024,
        duration=125.0,
    )


@pytest.fixture
def file_obj(db):
    return File.objects.create(
        file_hash='cc' * 32,
        original_filename='doc.pdf',
        file_extension='.pdf',
        original_size=100,
    )


@pytest.fixture
def image_admin():
    return ImageAdmin(Image, django_admin.site)


@pytest.fixture
def video_admin():
    return VideoAdmin(Video, django_admin.site)


@pytest.fixture
def file_admin():
    return FileAdmin(File, django_admin.site)


class TestFormatFileSize:
    def test_none(self):
        assert format_file_size(None) == ''

    @pytest.mark.parametrize('size,expected', [
        (512, '512.0B'),
        (2048, '2.0KB'),
        (3 * 1024 * 1024, '3.0MB'),
        (4 * 1024 ** 3, '4.0GB'),
    ])
    def test_units(self, size, expected):
        assert format_file_size(size) == expected


@pytest.mark.django_db
class TestOrphanFilter:
    def _filter(self, rf, image_admin, value):
        params = {'orphan': [value]} if value else {}
        return OrphanFilter(rf.get('/'), dict(params), Image, image_admin)

    def test_lookups(self, rf, image_admin):
        f = self._filter(rf, image_admin, None)
        assert ('yes', 'Orphans (no refs)') in f.lookups(None, image_admin)

    def test_orphans_only(self, rf, image_admin, image):
        orphan = Image.objects.create(
            file_hash='dd' * 32,
            original_filename='o.jpg',
            file_extension='.jpg',
            original_width=1,
            original_height=1,
            original_size=1,
            refs=[],
        )
        f = self._filter(rf, image_admin, 'yes')
        qs = f.queryset(None, Image.objects.all())
        assert list(qs) == [orphan]

    def test_referenced_only(self, rf, image_admin, image):
        f = self._filter(rf, image_admin, 'no')
        qs = f.queryset(None, Image.objects.all())
        assert list(qs) == [image]

    def test_no_value_returns_all(self, rf, image_admin, image):
        f = self._filter(rf, image_admin, None)
        qs = f.queryset(None, Image.objects.all())
        assert image in qs


@pytest.mark.django_db
class TestImageAdmin:
    def test_display_helpers(self, image_admin, image):
        assert image_admin.file_hash_short(image) == f'{"aa" * 32}'[:8] + '...'
        assert image_admin.dimensions(image) == '800x600'
        assert image_admin.file_size_display(image) == '2.0 KB'
        assert image_admin.refs_count(image) == 1
        assert image_admin.preview_thumbnail(image) == '-'
        image.is_processed = True
        assert 'img src=' in image_admin.preview_thumbnail(image)

    def test_refs_count_empty(self, image_admin, image):
        image.refs = []
        assert image_admin.refs_count(image) == '-'

    def test_variant_links(self, image_admin, image):
        for name in ('16', '32', '64', '120', '160', '240', '480', '720', '1080'):
            html = getattr(image_admin, f'variant_{name}_link')(image)
            assert f'/{name}.webp' in html
        assert '720.jpg' in image_admin.variant_720_jpg_link(image)

    def test_variant_link_includes_size_when_file_exists(self, image_admin, image):
        variant_dir = os.path.join(settings.MEDIA_ROOT, 'images', image.file_hash)
        os.makedirs(variant_dir, exist_ok=True)
        webp = os.path.join(variant_dir, '16.webp')
        jpg = os.path.join(variant_dir, '720.jpg')
        try:
            with open(webp, 'wb') as fh:
                fh.write(b'x' * 100)
            with open(jpg, 'wb') as fh:
                fh.write(b'x' * 200)
            assert '(100.0B)' in image_admin.variant_16_link(image)
            assert '(200.0B)' in image_admin.variant_720_jpg_link(image)
        finally:
            os.remove(webp)
            os.remove(jpg)

    def test_original_link_without_file(self, image_admin, image):
        assert image_admin.original_link(image) == '-'

    def test_save_model_sets_uploaded_by(self, image_admin, image, rf, user):
        request = rf.post('/')
        request.user = user
        image_admin.save_model(request, image, form=None, change=True)
        image.refresh_from_db()
        assert image.uploaded_by == user

    def test_regenerate_variants_async(self, image_admin, image, rf, user):
        request = rf.post('/')
        request.user = user
        image_admin.message_user = MagicMock()
        with patch('stapel_cdn.tasks.process_image_async') as mock_task:
            image_admin.regenerate_variants_async(request, Image.objects.all())
        mock_task.delay.assert_called_once_with(image.id)
        image.refresh_from_db()
        assert image.is_processed is False
        assert image.processing_log == ''
        assert 'Successfully queued 1 image(s)' in image_admin.message_user.call_args_list[0].args[1]

    def test_regenerate_variants_async_error(self, image_admin, image, rf, user):
        request = rf.post('/')
        request.user = user
        image_admin.message_user = MagicMock()
        with patch('stapel_cdn.tasks.process_image_async') as mock_task:
            mock_task.delay.side_effect = RuntimeError('broker down')
            image_admin.regenerate_variants_async(request, Image.objects.all())
        messages_sent = [c.args[1] for c in image_admin.message_user.call_args_list]
        assert any('Error queuing image' in m for m in messages_sent)
        assert any('Failed to queue 1 image(s)' in m for m in messages_sent)

    def test_regenerate_sync(self, image_admin, image, rf, user):
        request = rf.post('/')
        request.user = user
        image_admin.message_user = MagicMock()
        with patch(
            'stapel_cdn.services.ImageProcessingService.process_image'
        ) as mock_proc:
            image_admin.regenerate_sync(request, Image.objects.all())
        mock_proc.assert_called_once()
        assert image_admin.message_user.called

    def test_regenerate_sync_error(self, image_admin, image, rf, user):
        request = rf.post('/')
        request.user = user
        image_admin.message_user = MagicMock()
        with patch(
            'stapel_cdn.services.ImageProcessingService.process_image',
            side_effect=RuntimeError('vips exploded'),
        ):
            image_admin.regenerate_sync(request, Image.objects.all())
        message = image_admin.message_user.call_args.args[1]
        assert 'Error - vips exploded' in message


@pytest.mark.django_db
class TestVideoAdmin:
    def test_display_helpers(self, video_admin, video):
        assert video_admin.file_hash_short(video) == f'{"bb" * 32}'[:8] + '...'
        assert video_admin.dimensions(video) == 'N/A'
        video.original_width, video.original_height = 1920, 1080
        assert video_admin.dimensions(video) == '1920x1080'
        assert video_admin.duration_display(video) == '2:05'
        video.duration = None
        assert video_admin.duration_display(video) == 'N/A'
        assert video_admin.file_size_display(video) == '5.0 MB'
        assert video_admin.refs_count(video) == '-'

    def test_save_model_sets_uploaded_by(self, video_admin, video, rf, user):
        request = rf.post('/')
        request.user = user
        video_admin.save_model(request, video, form=None, change=True)
        video.refresh_from_db()
        assert video.uploaded_by == user


@pytest.mark.django_db
class TestFileAdmin:
    def test_display_helpers(self, file_admin, file_obj):
        assert file_admin.file_hash_short(file_obj) == f'{"cc" * 32}'[:8] + '...'
        assert file_admin.file_size_display(file_obj) == '100.0 B'
        assert file_admin.refs_count(file_obj) == '-'
        assert file_admin.original_link(file_obj) == '-'

    def test_save_model_sets_uploaded_by(self, file_admin, file_obj, rf, user):
        request = rf.post('/')
        request.user = user
        file_admin.save_model(request, file_obj, form=None, change=True)
        file_obj.refresh_from_db()
        assert file_obj.uploaded_by == user

    def test_registered_in_admin_site(self):
        assert Image in django_admin.site._registry
        assert Video in django_admin.site._registry
        assert File in django_admin.site._registry
