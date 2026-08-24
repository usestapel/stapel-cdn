"""
Tests for Celery tasks (called synchronously; pyvips/processing mocked).
"""
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from django.test import override_settings
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
        with patch.object(tasks.generate_thumbnails, 'apply_async') as mock_thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as mock_prev:
            tasks.process_image_async(image.id)
        mock_thumb.assert_called_once_with(args=[image.id], kwargs={})
        mock_prev.assert_called_once_with(args=[image.id], kwargs={'watermark': False})
        image.refresh_from_db()
        assert 'Processing started' in image.processing_log

    def test_updates_dimensions_via_pyvips(self):
        image = _make_image(width=0, height=0)
        fake_img = MagicMock(width=640, height=480)
        with patch('pyvips.Image.new_from_file', return_value=fake_img), \
                patch.object(type(image.original), 'path', '/tmp/fake.jpg'), \
                patch.object(tasks.generate_thumbnails, 'apply_async'), \
                patch.object(tasks.generate_previews, 'apply_async'):
            tasks.process_image_async(image.id)
        image.refresh_from_db()
        assert image.original_width == 640
        assert image.original_height == 480
        assert 'Updated dimensions: 640x480' in image.processing_log

    def test_dimension_error_still_schedules(self):
        image = _make_image(width=0, height=0)
        with patch('pyvips.Image.new_from_file', side_effect=RuntimeError('no file')), \
                patch.object(type(image.original), 'path', '/tmp/fake.jpg'), \
                patch.object(tasks.generate_thumbnails, 'apply_async') as mock_thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as mock_prev:
            tasks.process_image_async(image.id)
        mock_thumb.assert_called_once()
        mock_prev.assert_called_once()

    def test_missing_image_returns_early(self):
        with patch.object(tasks.generate_thumbnails, 'apply_async') as mock_thumb:
            tasks.process_image_async(999999)
        mock_thumb.assert_not_called()


@pytest.mark.django_db
class TestProcessVideoAsync:
    def test_processes_unprocessed_video(self):
        video = _make_video()
        Video.objects.filter(pk=video.pk).update(is_processed=False)
        with patch(
            'stapel_cdn.services.VideoProcessingService.process_video'
        ) as mock_proc:
            tasks.process_video_async(video.id)
        mock_proc.assert_called_once()

    def test_unprocessed_video_records_a_named_reason(self):
        """The task runs the real pass; with no readable blob it degrades.

        ``is_processed`` stays False on purpose (0.16.0: the flag means
        measured facts exist) and ``meta_reason`` says which of the named
        reasons applied — never an empty snapshot with no explanation.
        """
        video = _make_video()
        Video.objects.filter(pk=video.pk).update(is_processed=False)
        tasks.process_video_async(video.id)
        video.refresh_from_db()
        assert video.is_processed is False
        assert video.meta_reason in ('source_missing', 'ffprobe_missing')

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
        with patch.object(tasks.generate_thumbnails, 'apply_async') as mock_thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as mock_prev:
            retried = tasks.retry_unprocessed()
        assert retried == 1
        mock_thumb.assert_called_once_with(args=[image.id], kwargs={})
        mock_prev.assert_called_once_with(args=[image.id], kwargs={'watermark': False})
        image.refresh_from_db()
        assert 'RETRY: re-queued by periodic task' in image.processing_log

    def test_ignores_fresh_and_processed_images(self):
        _make_image(file_hash='11' * 32)  # fresh, not stuck yet
        done = _make_image(file_hash='22' * 32, processed=True)
        Image.objects.filter(pk=done.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        with patch.object(tasks.generate_thumbnails, 'apply_async') as mock_thumb:
            retried = tasks.retry_unprocessed()
        assert retried == 0
        mock_thumb.assert_not_called()


@pytest.mark.django_db
class TestQueueRouting:
    """The queue names are configuration, and the default is "say nothing".

    Through 0.14 these two tasks were decorated ``@shared_task(queue=
    "thumbnails")`` / ``queue="previews"``. A fleet that shards work by
    setting ``CELERY_TASK_DEFAULT_QUEUE`` per service had no worker on either
    name, so the messages were never consumed and every ``variant_*_url`` in
    the 201 pointed at a file that would never be written.
    """

    def test_tasks_carry_no_hardcoded_queue(self):
        # The literal is gone from the task itself: routing is a send-time
        # decision, so a host's setting can still reach it.
        assert getattr(tasks.generate_thumbnails, 'queue', None) is None
        assert getattr(tasks.generate_previews, 'queue', None) is None

    def test_default_sends_without_a_queue_option(self):
        # No `queue` kwarg at all — NOT `queue=None`, which is a different
        # instruction to celery. This is what lets a vanilla single-queue
        # worker consume the work with zero configuration.
        assert tasks.queue_options('THUMBNAILS_QUEUE') == {}
        assert tasks.queue_options('PREVIEWS_QUEUE') == {}

    @override_settings(
        STAPEL_CDN={"THUMBNAILS_QUEUE": "thumbs", "PREVIEWS_QUEUE": "prev"}
    )
    def test_settings_name_the_queue(self):
        assert tasks.queue_options('THUMBNAILS_QUEUE') == {"queue": "thumbs"}
        assert tasks.queue_options('PREVIEWS_QUEUE') == {"queue": "prev"}

    @override_settings(STAPEL_CDN={"THUMBNAILS_QUEUE": "  "})
    def test_blank_setting_falls_back_to_the_default_queue(self):
        assert tasks.queue_options('THUMBNAILS_QUEUE') == {}

    def test_process_image_async_sends_on_the_default_queue(self):
        image = _make_image()
        with patch.object(tasks.generate_thumbnails, 'apply_async') as thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as prev:
            tasks.process_image_async(image.id)
        assert thumb.call_args.kwargs == {"args": [image.id], "kwargs": {}}
        assert prev.call_args.kwargs == {
            "args": [image.id], "kwargs": {"watermark": False}
        }

    @override_settings(
        STAPEL_CDN={"THUMBNAILS_QUEUE": "thumbs", "PREVIEWS_QUEUE": "prev"}
    )
    def test_process_image_async_honours_the_settings(self):
        image = _make_image()
        with patch.object(tasks.generate_thumbnails, 'apply_async') as thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as prev:
            tasks.process_image_async(image.id)
        assert thumb.call_args.kwargs["queue"] == "thumbs"
        assert prev.call_args.kwargs["queue"] == "prev"

    @override_settings(
        STAPEL_CDN={"THUMBNAILS_QUEUE": "thumbs", "PREVIEWS_QUEUE": "prev"}
    )
    def test_retry_unprocessed_honours_the_settings(self):
        image = _make_image()
        Image.objects.filter(pk=image.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        with patch.object(tasks.generate_thumbnails, 'apply_async') as thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as prev:
            tasks.retry_unprocessed()
        assert thumb.call_args.kwargs["queue"] == "thumbs"
        assert prev.call_args.kwargs["queue"] == "prev"

    def test_retry_unprocessed_sends_on_the_default_queue(self):
        image = _make_image()
        Image.objects.filter(pk=image.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        with patch.object(tasks.generate_thumbnails, 'apply_async') as thumb, \
                patch.object(tasks.generate_previews, 'apply_async') as prev:
            tasks.retry_unprocessed()
        assert "queue" not in thumb.call_args.kwargs
        assert "queue" not in prev.call_args.kwargs


class TestBeatSchedule:
    """``retry_unprocessed`` is the safety net, and it schedules nothing."""

    def test_names_the_stable_task_path(self):
        entry = tasks.get_cdn_beat_schedule()["cdn-retry-unprocessed-images"]
        assert entry["task"] == tasks.RETRY_TASK_NAME
        assert tasks.retry_unprocessed.name == tasks.RETRY_TASK_NAME

    @override_settings(STAPEL_CDN={"RETRY_UNPROCESSED_SCHEDULE": {"minute": "*/30"}})
    def test_cadence_is_configuration(self):
        entry = tasks.get_cdn_beat_schedule()["cdn-retry-unprocessed-images"]
        assert entry["schedule"].minute == {0, 30}


@pytest.mark.django_db
class TestVariantsStatus:
    """A 201 must not claim a variant ladder that does not exist yet."""

    def test_pending_at_creation(self):
        image = _make_image()
        assert image.variants_status == "pending"
        assert image.variants_ready_at is None

    def test_flips_to_ready_on_task_success(self):
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_previews_only',
            return_value='PREVIEW LOG',
        ):
            tasks.generate_previews(image.id, watermark=False)
        image.refresh_from_db()
        assert image.variants_status == "ready"
        assert image.variants_ready_at is not None

    def test_stays_pending_on_task_failure(self):
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_previews_only',
            side_effect=RuntimeError('vips exploded'),
        ):
            with pytest.raises(RuntimeError):
                tasks.generate_previews(image.id, watermark=False)
        image.refresh_from_db()
        assert image.variants_status == "pending"
        assert image.variants_ready_at is None

    def test_thumbnails_alone_do_not_make_the_ladder_ready(self):
        # The ladder is ready when the *preview* branches exist; a deployment
        # whose preview worker is dead must not read as "ready".
        image = _make_image()
        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_thumbnails_only',
            return_value='THUMB LOG',
        ):
            tasks.generate_thumbnails(image.id)
        image.refresh_from_db()
        assert image.variants_status == "pending"

    def test_serializer_reports_it(self):
        from stapel_cdn.serializers import ImageSerializer

        image = _make_image()
        data = ImageSerializer(image).data
        assert data["variants_status"] == "pending"
        assert data["variants_ready_at"] is None
        # ...and the URLs it is qualifying are already all there.
        assert data["variant_720_url"]

        with patch(
            'stapel_cdn.services.ImageProcessingService.generate_previews_only',
            return_value='PREVIEW LOG',
        ):
            tasks.generate_previews(image.id, watermark=False)
        image.refresh_from_db()
        data = ImageSerializer(image).data
        assert data["variants_status"] == "ready"
        assert data["variants_ready_at"] is not None
