"""
Tests that stapel_core.signals.media_processed is sent at pipeline
completion (variant generation is mocked — no libvips needed).
"""
from unittest import mock

import pytest
from stapel_core.signals import media_processed

from stapel_cdn.models import Image
from stapel_cdn.services import ImageProcessingService


def _make_image(file_hash):
    # original is a stored-name reference; width/height > 1 so
    # process_image never touches pyvips for metadata.
    return Image.objects.create(
        file_hash=file_hash,
        original_filename="test.jpg",
        file_extension=".jpg",
        original=f"product/{file_hash}/test.jpg",
        original_width=800,
        original_height=600,
        original_size=1234,
    )


@pytest.mark.django_db
class TestMediaProcessedSignal:
    def test_signal_sent_on_pipeline_success(self):
        image = _make_image("1" * 64)
        received = []

        def receiver(sender, instance, **kwargs):
            received.append((sender, instance))

        media_processed.connect(receiver)
        try:
            with mock.patch.object(
                ImageProcessingService, "generate_thumbnails_only", return_value="thumbs ok"
            ), mock.patch.object(
                ImageProcessingService, "generate_previews_only", return_value="previews ok"
            ):
                ImageProcessingService.process_image(image)
        finally:
            media_processed.disconnect(receiver)

        assert len(received) == 1
        sender, instance = received[0]
        assert sender is Image
        assert instance.pk == image.pk
        assert instance.is_processed is True

    def test_signal_not_sent_on_failure(self):
        image = _make_image("2" * 64)
        received = []

        def receiver(sender, instance, **kwargs):
            received.append(instance)

        media_processed.connect(receiver)
        try:
            with mock.patch.object(
                ImageProcessingService,
                "generate_thumbnails_only",
                side_effect=RuntimeError("boom"),
            ):
                with pytest.raises(RuntimeError):
                    ImageProcessingService.process_image(image)
        finally:
            media_processed.disconnect(receiver)

        assert received == []
        image.refresh_from_db()
        assert image.is_processed is False
