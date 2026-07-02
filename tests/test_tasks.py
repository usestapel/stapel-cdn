"""
Tests for Celery tasks (called synchronously; pyvips/processing mocked).
"""
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from django.utils import timezone
from stapel_cdn import tasks
from stapel_cdn.models import Image, Video


def _make_image(file_hash='ab' * 32, width=100, height=100, processed=False):
    return Image.objects.create(
        file_hash=file_hash,
        original_filename='pic.jpg',
        file_extension='.jpg',
        original_width=width,
        original_height=height,
        original_size=100,
        is_processed=processed,
    )


def _make_video(file_hash='cd' * 32, processed=False):
    return Video.objects.create(
        file_hash=file_hash,
        original_filename='clip.mp4',
        file_extension='.mp4',
        original_size=100,
        is_processed=processed,
    )


@pytest.mark.django_db
class TestAppendLog:
    def test_appends_to_empty_log(self):
        image = _make_image()
        tasks._append_log(image, 'first line')
        image.refresh_from_db()
        assert image.processing_log == 'first line'

    def test_appends_to_existing_log(self):
        image = _make_image()
        image.processing_log = 'first line'
        image.save(update_fields=['processing_log'])
        tasks._append_log(image, 'second line')
        image.refresh_from_db()
        assert image.processing_log == 'first line\nsecond line'


@pytest.mark.django_db
class TestGenerateThumbnails:
    def test_success_appends_log(self):
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_thumbnails_only',
            return_value='THUMB LOG',
        ) as mock_gen:
            tasks.generate_thumbnails(image.id)
        mock_gen.assert_called_once()
        image.refresh_from_db()
        assert 'THUMB LOG' in image.processing_log

    def test_missing_image_logs_error(self):
        # Must not raise for a nonexistent id
        tasks.generate_thumbnails(999999)

    def test_failure_logs_and_reraises(self):
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_thumbnails_only',
            side_effect=RuntimeError('vips exploded'),
        ):
            with pytest.raises(RuntimeError):
                tasks.generate_thumbnails(image.id)
        image.refresh_from_db()
        assert 'THUMBNAIL ERROR: vips exploded' in image.processing_log


@pytest.mark.django_db
class TestGeneratePreviews:
    def test_success_marks_processed(self):
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_previews_only',
            return_value='PREVIEW LOG',
        ) as mock_gen:
            tasks.generate_previews(image.id, watermark=False)
        mock_gen.assert_called_once_with(image, False)
        image.refresh_from_db()
        assert image.is_processed is True
        assert 'PREVIEW LOG' in image.processing_log

    def test_missing_image_logs_error(self):
        tasks.generate_previews(999999)

    def test_failure_logs_and_reraises(self):
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_previews_only',
            side_effect=RuntimeError('preview exploded'),
        ):
            with pytest.raises(RuntimeError):
                tasks.generate_previews(image.id)
        image.refresh_from_db()
        assert image.is_processed is False
        assert 'PREVIEW ERROR: preview exploded' in image.processing_log


@pytest.mark.django_db
class TestProcessImageAsync:
    def test_schedules_both_tasks(self):
        image = _make_image(width=100, height=100)
        with patch.object(tasks.generate_thumbnails, 'delay') as mock_thumb, \
                patch.object(tasks.generate_previews, 'delay') as mock_prev:
            tasks.process_image_async(image.id)
        mock_thumb.assert_called_once_with(image.id)
        mock_prev.assert_called_once_with(image.id, watermark=False)
        image.refresh_from_db()
        assert 'Processing started' in image.processing_log

    def test_updates_dimensions_via_pyvips(self):
        image = _make_image(width=0, height=0)
        fake_img = MagicMock(width=640, height=480)
        with patch('pyvips.Image.new_from_file', return_value=fake_img), \
                patch.object(type(image.original), 'path', '/tmp/fake.jpg'), \
                patch.object(tasks.generate_thumbnails, 'delay'), \
                patch.object(tasks.generate_previews, 'delay'):
            tasks.process_image_async(image.id)
        image.refresh_from_db()
        assert image.original_width == 640
        assert image.original_height == 480
        assert 'Updated dimensions: 640x480' in image.processing_log

    def test_dimension_error_still_schedules(self):
        image = _make_image(width=0, height=0)
        with patch('pyvips.Image.new_from_file', side_effect=RuntimeError('no file')), \
                patch.object(type(image.original), 'path', '/tmp/fake.jpg'), \
                patch.object(tasks.generate_thumbnails, 'delay') as mock_thumb, \
                patch.object(tasks.generate_previews, 'delay') as mock_prev:
            tasks.process_image_async(image.id)
        mock_thumb.assert_called_once()
        mock_prev.assert_called_once()

    def test_missing_image_returns_early(self):
        with patch.object(tasks.generate_thumbnails, 'delay') as mock_thumb:
            tasks.process_image_async(999999)
        mock_thumb.assert_not_called()


@pytest.mark.django_db
class TestProcessVideoAsync:
    def test_processes_unprocessed_video(self):
        video = _make_video()
        Video.objects.filter(pk=video.pk).update(is_processed=False)
        tasks.process_video_async(video.id)
        video.refresh_from_db()
        assert video.is_processed is True

    def test_skips_processed_video(self):
        video = _make_video(processed=True)
        with patch(
            'stapel_cdn.services.VideoProcessingService.process_video'
        ) as mock_proc:
            tasks.process_video_async(video.id)
        mock_proc.assert_not_called()

    def test_missing_video_logs_error(self):
        tasks.process_video_async(999999)

    def test_failure_reraises(self):
        video = _make_video()
        Video.objects.filter(pk=video.pk).update(is_processed=False)
        with patch(
            'stapel_cdn.services.VideoProcessingService.process_video',
            side_effect=RuntimeError('ffmpeg exploded'),
        ):
            with pytest.raises(RuntimeError):
                tasks.process_video_async(video.id)


@pytest.mark.django_db
class TestRetryUnprocessed:
    def test_requeues_stuck_images(self):
        image = _make_image()
        Image.objects.filter(pk=image.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        with patch.object(tasks.generate_thumbnails, 'delay') as mock_thumb, \
                patch.object(tasks.generate_previews, 'delay') as mock_prev:
            retried = tasks.retry_unprocessed()
        assert retried == 1
        mock_thumb.assert_called_once_with(image.id)
        mock_prev.assert_called_once_with(image.id, watermark=False)
        image.refresh_from_db()
        assert 'RETRY: re-queued by periodic task' in image.processing_log

    def test_ignores_fresh_and_processed_images(self):
        _make_image(file_hash='11' * 32)  # fresh, not stuck yet
        done = _make_image(file_hash='22' * 32, processed=True)
        Image.objects.filter(pk=done.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        with patch.object(tasks.generate_thumbnails, 'delay') as mock_thumb:
            retried = tasks.retry_unprocessed()
        assert retried == 0
        mock_thumb.assert_not_called()
