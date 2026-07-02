"""
Tests for management commands: consume_cdn_events and benchmark_imgproc.
"""
import os
import pytest
from io import StringIO
from unittest.mock import MagicMock, patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from stapel_cdn.management.commands.consume_cdn_events import Command as ConsumeCommand
from stapel_cdn.models import Image
from stapel_core.bus import Event

IMG_HASH = 'ab' * 32


def _make_image(**kwargs):
    defaults = dict(
        file_hash=IMG_HASH,
        original_filename='pic.jpg',
        file_extension='.jpg',
        original_width=10,
        original_height=10,
        original_size=100,
        refs=[],
    )
    defaults.update(kwargs)
    with patch('stapel_cdn.tasks.process_image_async'):
        return Image.objects.create(**defaults)


@pytest.mark.django_db
class TestConsumeCdnEvents:
    def _command(self):
        return ConsumeCommand(stdout=StringIO())

    def test_ignores_other_event_types(self):
        cmd = self._command()
        with patch('stapel_cdn.services.apply_ref_sync') as mock_sync:
            cmd.handle_event(Event(event_type='user.created', service='auth'))
        mock_sync.assert_not_called()

    def test_missing_fields_skipped(self):
        cmd = self._command()
        with patch('stapel_cdn.services.apply_ref_sync') as mock_sync:
            cmd.handle_event(Event(
                event_type='cdn.ref.sync',
                service='shop',
                payload={'service': 'shop'},  # entity_type/entity_id missing
            ))
        mock_sync.assert_not_called()

    def test_valid_event_persists_refs(self):
        image = _make_image()
        cmd = self._command()
        cmd.handle_event(Event(
            event_type='cdn.ref.sync',
            service='shop',
            payload={
                'service': 'shop',
                'entity_type': 'product',
                'entity_id': '42',
                'old_hashes': [],
                'new_hashes': [f'product/{IMG_HASH}'],
            },
        ))
        image.refresh_from_db()
        assert image.refs == ['shop/product/42']
        output = cmd.stdout.getvalue() if hasattr(cmd.stdout, 'getvalue') else ''
        assert 'cdn.ref.sync shop/product/42: +1 -0' in output

    def test_event_with_errors_reported(self):
        cmd = self._command()
        cmd.handle_event(Event(
            event_type='cdn.ref.sync',
            service='shop',
            payload={
                'service': 'shop',
                'entity_type': 'product',
                'entity_id': '9',
                'new_hashes': [f'product/{"0" * 64}'],
            },
        ))
        output = cmd.stdout.getvalue() if hasattr(cmd.stdout, 'getvalue') else ''
        assert 'errors=' in output

    def test_topic_configuration(self):
        assert ConsumeCommand.consumer_group == 'cdn-ref-sync'
        assert ConsumeCommand.topics


@pytest.mark.django_db
class TestBenchmarkImgproc:
    def test_no_images(self):
        err = StringIO()
        call_command('benchmark_imgproc', stderr=err)
        assert 'No images found' in err.getvalue()

    def test_python_benchmark_without_cpp_binary(self):
        image = _make_image(
            original=SimpleUploadedFile('bench.jpg', b'jpeg', content_type='image/jpeg'),
            is_processed=True,
        )
        out = StringIO()
        with patch(
            'stapel_cdn.management.commands.benchmark_imgproc.ImageProcessingService'
        ) as mock_service:
            mock_service.generate_thumbnails_only.return_value = ''
            mock_service.generate_previews_only.return_value = ''
            call_command('benchmark_imgproc', '--iterations=1', stdout=out)
        output = out.getvalue()
        assert f'Image: {image.file_hash[:8]}' in output
        assert 'Python (pyvips)' in output
        assert 'Average:' in output
        assert 'C++ binary not found' in output

    def test_cpp_benchmark_with_mocked_binary(self):
        image = _make_image(
            file_hash='cd' * 32,
            original=SimpleUploadedFile('bench2.jpg', b'jpeg2', content_type='image/jpeg'),
        )
        out = StringIO()
        real_exists = os.path.exists

        def fake_exists(path):
            if path == '/usr/local/bin/imgproc':
                return True
            return real_exists(path)

        run_result = MagicMock(returncode=0, stdout='42ms', stderr='')
        with patch(
            'stapel_cdn.management.commands.benchmark_imgproc.ImageProcessingService'
        ) as mock_service, patch(
            'stapel_cdn.management.commands.benchmark_imgproc.os.path.exists',
            side_effect=fake_exists,
        ), patch(
            'stapel_cdn.management.commands.benchmark_imgproc.subprocess.run',
            return_value=run_result,
        ):
            mock_service.generate_thumbnails_only.return_value = ''
            mock_service.generate_previews_only.return_value = ''
            call_command('benchmark_imgproc', f'--image-id={image.id}', '--iterations=1', stdout=out)
        output = out.getvalue()
        assert 'C++ (imgproc)' in output
        assert 'Speedup:' in output
