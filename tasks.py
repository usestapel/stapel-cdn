"""
Celery tasks for image processing.

Two queues with different priorities:
- thumbnails: high priority (16, 32, 64, 120px)
- previews: normal priority (160-1080px with watermark)

Periodic task:
- retry_unprocessed: picks up images stuck with is_processed=False
"""
from celery import shared_task
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _append_log(image, log_text: str):
    """Append log text to image's processing_log field."""
    if image.processing_log:
        image.processing_log += '\n' + log_text
    else:
        image.processing_log = log_text
    image.save(update_fields=['processing_log'])


@shared_task(queue='thumbnails')
def generate_thumbnails(image_id: int):
    """Generate thumbnails (16, 32, 64, 120px). High priority queue."""
    from .models import Image
    from .services import ImageProcessingService

    try:
        image = Image.objects.get(id=image_id)
        log = ImageProcessingService.generate_thumbnails_only(image)
        _append_log(image, log)
        logger.info(f"Thumbnails generated for {image.file_hash}")
    except Image.DoesNotExist:
        logger.error(f"Image {image_id} not found")
    except Exception as e:
        logger.error(f"Failed to generate thumbnails for {image_id}: {e}")
        # Log error to processing_log if image exists
        try:
            image = Image.objects.get(id=image_id)
            _append_log(image, f"[{datetime.now().isoformat()}] THUMBNAIL ERROR: {e}")
        except Image.DoesNotExist:
            pass
        raise


@shared_task(queue='previews')
def generate_previews(image_id: int, watermark: bool = True):
    """Generate previews (160-1080px). Normal priority queue."""
    from .models import Image
    from .services import ImageProcessingService

    try:
        image = Image.objects.get(id=image_id)
        log = ImageProcessingService.generate_previews_only(image, watermark)
        _append_log(image, log)
        image.is_processed = True
        image.save(update_fields=['is_processed'])
        logger.info(f"Previews generated for {image.file_hash}")
    except Image.DoesNotExist:
        logger.error(f"Image {image_id} not found")
    except Exception as e:
        logger.error(f"Failed to generate previews for {image_id}: {e}")
        # Log error to processing_log if image exists
        try:
            image = Image.objects.get(id=image_id)
            _append_log(image, f"[{datetime.now().isoformat()}] PREVIEW ERROR: {e}")
        except Image.DoesNotExist:
            pass
        raise


@shared_task
def process_image_async(image_id: int):
    """Schedule both thumbnail and preview generation."""
    from .models import Image
    from datetime import datetime
    import pyvips

    # Initialize processing log and update dimensions if needed
    try:
        image = Image.objects.get(id=image_id)
        image.processing_log = f"=== Processing started {datetime.now().isoformat()} ==="

        # Update dimensions if not set (1x1 means not yet read)
        if image.original_width <= 1 or image.original_height <= 1:
            img = pyvips.Image.new_from_file(image.original.path, access='sequential')
            image.original_width = img.width
            image.original_height = img.height
            image.processing_log += f"\nUpdated dimensions: {img.width}x{img.height}"

        image.save(update_fields=['processing_log', 'original_width', 'original_height'])
    except Image.DoesNotExist:
        logger.error(f"Image {image_id} not found")
        return
    except Exception as e:
        logger.error(f"Error updating image dimensions: {e}")

    # Schedule tasks: thumbnails first (high priority), then previews
    generate_thumbnails.delay(image_id)
    # for now disable watermarks since design and letterboxing are not ready
    generate_previews.delay(image_id, watermark=False)


@shared_task
def process_video_async(video_id: int):
    """Process video variants."""
    from .models import Video
    from .services import VideoProcessingService

    try:
        video = Video.objects.get(id=video_id)
        if not video.is_processed:
            VideoProcessingService.process_video(video)
            logger.info(f"Processed video {video.file_hash}")
    except Video.DoesNotExist:
        logger.error(f"Video {video_id} not found")
    except Exception as e:
        logger.error(f"Failed to process video {video_id}: {e}")
        raise


@shared_task
def retry_unprocessed():
    """
    Periodic task: find images that are stuck with is_processed=False
    for more than 5 minutes and re-queue them for processing.
    """
    from .models import Image
    from django.utils import timezone

    cutoff = timezone.now() - timedelta(minutes=5)
    retried = 0

    stuck_images = Image.objects.filter(
        is_processed=False,
        created_at__lt=cutoff,
    )
    for image in stuck_images:
        logger.info(f"Retrying unprocessed image {image.id} ({image.file_hash[:8]})")
        _append_log(image, f"[{datetime.now().isoformat()}] RETRY: re-queued by periodic task")
        generate_thumbnails.delay(image.id)
        generate_previews.delay(image.id, watermark=False)
        retried += 1

    if retried:
        logger.info(f"Retried {retried} unprocessed images")
    return retried
