"""
Celery tasks for image processing.

Queue routing is CONFIGURATION, not a literal
---------------------------------------------
``generate_thumbnails`` and ``generate_previews`` used to be decorated
``@shared_task(queue="thumbnails")`` / ``@shared_task(queue="previews")``.
Those two strings were the whole routing policy of this library, and they
were invisible from the outside: a deployment that shards work per service
by setting ``CELERY_TASK_DEFAULT_QUEUE`` (and running one worker per queue)
had **zero consumers** on ``thumbnails`` and ``previews``. Nothing raised.
The upload answered 201 with the full ladder of ``variant_*_url``s, the
messages sat in queues no process was listening on, and every one of those
URLs 404'd forever — a whole fleet's image pipeline silently dead.

So the queue names are now settings, resolved at send time:

* ``STAPEL_CDN["THUMBNAILS_QUEUE"]`` / ``STAPEL_CDN["PREVIEWS_QUEUE"]``
* default ``None`` — send with **no** ``queue`` option at all, so the task
  lands on the app's own default queue and a vanilla single-queue worker
  consumes it with no configuration;
* an explicit value for a fleet that shards, matched by its worker's ``-Q``.

Resolution happens per send (``_send`` below), not at decoration time: a
value read while the module is imported would be frozen before a host's
settings were necessarily final, and could not be overridden in a test.
``checks.W008`` reports queue names this process has no in-process evidence
anybody consumes — and is explicit about what a library cannot see.

Periodic task
-------------
``retry_unprocessed`` picks up images stuck with ``is_processed=False``. It
is the safety net for exactly the class above, and it only runs if somebody
schedules it: wire ``get_cdn_beat_schedule()`` into ``CELERY_BEAT_SCHEDULE``,
on the cadence in ``STAPEL_CDN["RETRY_UNPROCESSED_SCHEDULE"]``.
"""
from celery import shared_task
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: Stable task names — what a beat schedule, a ``task_routes`` map or a
#: worker's ``-Q`` has to reference, kept out of the callers' reach so a
#: refactor cannot silently rename what a deployment routes on.
THUMBNAILS_TASK_NAME = "stapel_cdn.tasks.generate_thumbnails"
PREVIEWS_TASK_NAME = "stapel_cdn.tasks.generate_previews"
RETRY_TASK_NAME = "stapel_cdn.tasks.retry_unprocessed"

#: Setting key per variant-generation task (checks.W008 reads this map, so
#: the check and the routing can never disagree about which keys exist).
QUEUE_SETTINGS = {
    THUMBNAILS_TASK_NAME: "THUMBNAILS_QUEUE",
    PREVIEWS_TASK_NAME: "PREVIEWS_QUEUE",
}


def queue_options(setting_key: str) -> dict:
    """``{"queue": ...}`` for a configured queue, ``{}`` for the default one.

    An empty dict is the point: passing ``queue=None`` to ``apply_async`` is
    not the same as not passing it, and only the second one means "whatever
    this app's default queue is".
    """
    from .conf import cdn_settings

    name = getattr(cdn_settings, setting_key, None)
    name = str(name).strip() if name else ""
    return {"queue": name} if name else {}


def _send(task, setting_key: str, args=(), kwargs=None):
    """Send ``task`` on the queue ``setting_key`` names (or the app default)."""
    return task.apply_async(
        args=list(args), kwargs=dict(kwargs or {}), **queue_options(setting_key)
    )


def get_cdn_beat_schedule() -> dict:
    """Beat entry for ``retry_unprocessed``, on the configured cadence.

    Wire it into a host's schedule::

        from stapel_cdn.tasks import get_cdn_beat_schedule

        CELERY_BEAT_SCHEDULE = {**get_cdn_beat_schedule(), ...}
    """
    from celery.schedules import crontab

    from .conf import cdn_settings

    schedule = dict(cdn_settings.RETRY_UNPROCESSED_SCHEDULE or {})
    return {
        "cdn-retry-unprocessed-images": {
            "task": RETRY_TASK_NAME,
            "schedule": crontab(**schedule),
        },
    }


def _append_log(image, log_text: str):
    """Append log text to image's processing_log field."""
    if image.processing_log:
        image.processing_log += '\n' + log_text
    else:
        image.processing_log = log_text
    image.save(update_fields=['processing_log'])


@shared_task(name=THUMBNAILS_TASK_NAME)
def generate_thumbnails(image_id: int):
    """Generate thumbnails (16, 32, 64, 120px).

    Carries no ``queue`` option of its own — routing is resolved per send
    from ``STAPEL_CDN["THUMBNAILS_QUEUE"]`` (see the module docstring).
    """
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


@shared_task(name=PREVIEWS_TASK_NAME)
def generate_previews(image_id: int, watermark: bool = True):
    """Generate previews (160-1080px).

    Carries no ``queue`` option of its own — routing is resolved per send
    from ``STAPEL_CDN["PREVIEWS_QUEUE"]`` (see the module docstring).

    Success here is what makes the variant ladder real: it stamps
    ``variants_ready_at`` and flips ``Image.variants_status`` to ``"ready"``,
    which is the field the upload response and every later GET report. A
    failure leaves both alone — the row stays ``"pending"`` rather than
    advertising URLs that do not exist.
    """
    from django.utils import timezone

    from .models import Image
    from .services import ImageProcessingService

    try:
        image = Image.objects.get(id=image_id)
        log = ImageProcessingService.generate_previews_only(image, watermark)
        _append_log(image, log)
        image.is_processed = True
        image.variants_ready_at = timezone.now()
        image.save(update_fields=['is_processed', 'variants_ready_at'])
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

    # Schedule tasks: thumbnails first (high priority), then previews.
    # Sent through _send so the configured queue (or the app's default one)
    # applies — .delay() cannot carry a queue option.
    _send(generate_thumbnails, "THUMBNAILS_QUEUE", args=(image_id,))
    # for now disable watermarks since design and letterboxing are not ready
    _send(generate_previews, "PREVIEWS_QUEUE", args=(image_id,),
          kwargs={"watermark": False})


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
        _send(generate_thumbnails, "THUMBNAILS_QUEUE", args=(image.id,))
        _send(generate_previews, "PREVIEWS_QUEUE", args=(image.id,),
              kwargs={"watermark": False})
        retried += 1

    if retried:
        logger.info(f"Retried {retried} unprocessed images")
    return retried


__all__ = [
    "THUMBNAILS_TASK_NAME",
    "PREVIEWS_TASK_NAME",
    "RETRY_TASK_NAME",
    "QUEUE_SETTINGS",
    "queue_options",
    "get_cdn_beat_schedule",
    "generate_thumbnails",
    "generate_previews",
    "process_image_async",
    "process_video_async",
    "retry_unprocessed",
]
