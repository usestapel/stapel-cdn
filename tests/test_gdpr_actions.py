"""
Tests for the GDPR provider (export/export_to_staging/delete/anonymize)
and the user.deleted action subscription.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from django.core.files.uploadedfile import SimpleUploadedFile
from stapel_cdn.actions import handle_user_deleted, handle_user_deletion_initiated
from stapel_cdn.gdpr import CDNGDPRProvider, _serialize_dates
from stapel_cdn.models import File, Image, Video
from stapel_core.django.users.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username='gdpruser', email='gdpr@example.com', password='testpass123'
    )


def _make_image(user, file_hash='11' * 32, refs=None, original=None):
    with patch('stapel_cdn.tasks.process_image_async'):
        return Image.objects.create(
            file_hash=file_hash,
            original_filename='pic.jpg',
            file_extension='.jpg',
            original_width=10,
            original_height=10,
            original_size=100,
            uploaded_by=user,
            refs=refs or [],
            **({'original': original} if original else {}),
        )


def _make_video(user, file_hash='22' * 32, refs=None):
    return Video.objects.create(
        file_hash=file_hash,
        original_filename='clip.mp4',
        file_extension='.mp4',
        original_size=200,
        uploaded_by=user,
        refs=refs or [],
    )


def _make_file(user, file_hash='33' * 32, refs=None):
    return File.objects.create(
        file_hash=file_hash,
        original_filename='doc.pdf',
        file_extension='.pdf',
        mime_type='application/pdf',
        original_size=300,
        uploaded_by=user,
        refs=refs or [],
    )


@pytest.mark.django_db
class TestExport:
    def test_export_returns_all_sections(self, user):
        _make_image(user)
        _make_video(user)
        _make_file(user)
        data = CDNGDPRProvider().export(user.id)
        assert len(data['images']) == 1
        assert len(data['videos']) == 1
        assert len(data['files']) == 1
        assert data['images'][0]['original_filename'] == 'pic.jpg'
        # Dates serialized to ISO strings
        assert isinstance(data['images'][0]['created_at'], str)

    def test_export_ignores_other_users(self, user):
        other = User.objects.create_user(
            username='someone', email='someone@example.com', password='x'
        )
        _make_image(other)
        data = CDNGDPRProvider().export(user.id)
        assert data == {'images': [], 'videos': [], 'files': []}


@pytest.mark.django_db
class TestExportToStaging:
    def test_copies_files_and_writes_index(self, user, tmp_path):
        upload = SimpleUploadedFile('mine.jpg', b'jpegbytes', content_type='image/jpeg')
        _make_image(user, original=upload)
        _make_video(user)  # no original file on disk -> skipped

        staging = tmp_path / 'staging'
        written = CDNGDPRProvider().export_to_staging(user.id, staging)

        assert (staging / 'pic.jpg').exists()
        index = staging / 'media_index.json'
        assert index in written
        metadata = json.loads(index.read_text())
        assert metadata['images'][0]['filename'] == 'pic.jpg'
        assert metadata['videos'] == []
        assert metadata['files'] == []

    def test_missing_binary_skipped(self, user, tmp_path):
        image = _make_image(user)
        image.original.name = 'product/nonexistent/gone.jpg'
        Image.objects.filter(pk=image.pk).update(original='product/nonexistent/gone.jpg')

        staging = tmp_path / 'staging2'
        written = CDNGDPRProvider().export_to_staging(user.id, staging)
        # Only the index file is written
        assert [p.name for p in written] == ['media_index.json']


@pytest.mark.django_db
class TestDelete:
    def test_unreferenced_media_deleted(self, user):
        _make_image(user, refs=[])
        _make_file(user, refs=[])
        CDNGDPRProvider().delete(user.id)
        assert Image.objects.count() == 0
        assert File.objects.count() == 0

    def test_referenced_media_anonymized(self, user):
        video = _make_video(user, refs=['shop/product/1'])
        CDNGDPRProvider().delete(user.id)
        video.refresh_from_db()
        assert video.uploaded_by is None
        assert Video.objects.count() == 1

    def test_anonymize_is_noop(self, user):
        CDNGDPRProvider().anonymize(user.id)


class TestSerializeDates:
    def test_passthrough_and_isoformat(self):
        from datetime import datetime

        rows = [{'a': 1, 'b': datetime(2026, 1, 2, 3, 4, 5)}]
        result = _serialize_dates(rows)
        assert result == [{'a': 1, 'b': '2026-01-02T03:04:05'}]


@pytest.mark.django_db
class TestHandleUserDeleted:
    def test_erases_user_media(self, user):
        _make_image(user, refs=[])
        event = MagicMock()
        event.payload = {'user_id': user.id}
        handle_user_deleted(event)
        assert Image.objects.count() == 0

    def test_missing_user_id_logged(self, user):
        _make_image(user, refs=[])
        event = MagicMock()
        event.payload = {}
        event.event_id = 'evt-1'
        handle_user_deleted(event)
        # Nothing deleted without a user_id
        assert Image.objects.count() == 1


@pytest.mark.django_db
class TestHandleUserDeletedConfirmation:
    """user.deleted with a correlation_id (the gdpr orchestrator's
    remote-deletion protocol) must be confirmed with gdpr.section.erased —
    without it the closure's 'media' AccountDeletionPart never completes."""

    def test_confirms_section_erased_with_correlation_id(self, user):
        _make_image(user, refs=[])
        event = MagicMock()
        event.payload = {'user_id': user.id, 'correlation_id': 'corr-42'}
        with patch('stapel_core.comm.emit') as m_emit:
            handle_user_deleted(event)
        assert Image.objects.count() == 0
        m_emit.assert_called_once()
        args, kwargs = m_emit.call_args
        assert args[0] == 'gdpr.section.erased'
        assert args[1] == {
            'user_id': str(user.id),
            'correlation_id': 'corr-42',
            'service': 'media',
        }

    def test_no_confirmation_without_correlation_id(self, user):
        """Monolith path (in-process provider run, no remote parts): the
        payload carries no correlation_id and no confirmation is emitted."""
        _make_image(user, refs=[])
        event = MagicMock()
        event.payload = {'user_id': user.id}
        with patch('stapel_core.comm.emit') as m_emit:
            handle_user_deleted(event)
        assert Image.objects.count() == 0
        m_emit.assert_not_called()


@pytest.mark.django_db
class TestHandleDeletionInitiated:
    """user.deletion_initiated: the consume schema existed with no handler
    (2026-07-16 audit). Grace-safe subset: orphans purged, referenced media
    untouched until user.deleted (grace is cancellable)."""

    def test_purges_unreferenced_media(self, user):
        _make_image(user, refs=[])
        _make_file(user, refs=[])
        event = MagicMock()
        event.payload = {'user_id': user.id}
        handle_user_deletion_initiated(event)
        assert Image.objects.count() == 0
        assert File.objects.count() == 0

    def test_referenced_media_untouched_and_still_owned(self, user):
        video = _make_video(user, refs=['shop/product/1'])
        event = MagicMock()
        event.payload = {'user_id': user.id}
        handle_user_deletion_initiated(event)
        video.refresh_from_db()
        # Grace is cancellable: ownership link must survive for a cancel.
        assert video.uploaded_by_id == user.id
        assert Video.objects.count() == 1

    def test_idempotent_on_redelivery(self, user):
        _make_image(user, refs=[])
        event = MagicMock()
        event.payload = {'user_id': user.id}
        handle_user_deletion_initiated(event)
        handle_user_deletion_initiated(event)  # at-least-once redelivery
        assert Image.objects.count() == 0

    def test_missing_user_id_logged_and_nothing_removed(self, user):
        _make_image(user, refs=[])
        event = MagicMock()
        event.payload = {}
        event.event_id = 'evt-2'
        handle_user_deletion_initiated(event)
        assert Image.objects.count() == 1


@pytest.mark.django_db
class TestErasureFailureIsNotAReceipt:
    """The one path whose whole contract is provable erasure.

    ``delete()`` returning normally is what stapel-gdpr's orchestrator treats
    as a receipt, and what lets a closure flip to DELETED. The unlink — the
    only step that removes the actual bytes — used to sit under
    ``except Exception: pass``, after which the row was deleted anyway and the
    object counted as removed. That is unrecoverable rather than merely wrong:
    the row is the only record of where the file is, so a failed erasure
    became personal data nobody can ever locate again.
    """

    @staticmethod
    def _image_with_bytes(user):
        return _make_image(
            user,
            original=SimpleUploadedFile('pic.jpg', b'personal bytes', 'image/jpeg'),
        )

    @staticmethod
    def _unlink_fails():
        """Storage that refuses to remove the blob (read-only mount, S3 denial)."""
        return patch(
            'stapel_cdn.storage.OverwriteStorage.delete',
            side_effect=OSError('read-only file system'),
        )

    def test_a_failed_unlink_raises_instead_of_reporting_success(self, user):
        from stapel_cdn.gdpr import MediaErasureIncomplete

        image = self._image_with_bytes(user)

        with self._unlink_fails(), pytest.raises(MediaErasureIncomplete):
            CDNGDPRProvider().delete(user.id)

        # The row survives, which is what keeps the stranded file findable.
        assert Image.objects.filter(pk=image.pk).exists()

    def test_the_row_is_kept_so_a_retry_can_find_the_file(self, user):
        from stapel_cdn.gdpr import MediaErasureIncomplete

        image = self._image_with_bytes(user)

        with self._unlink_fails(), pytest.raises(MediaErasureIncomplete):
            CDNGDPRProvider().purge_unreferenced(user.id)

        image.refresh_from_db()
        assert image.uploaded_by_id == user.id, (
            'ownership must survive a failed erasure, or the file can no '
            'longer be attributed to the person it belongs to'
        )

    def test_no_section_erased_confirmation_when_the_bytes_survive(self, user):
        """Without this, the closure flips to DELETED over data still on disk."""
        self._image_with_bytes(user)
        event = MagicMock()
        event.payload = {'user_id': user.id, 'correlation_id': 'corr-43'}

        with patch('stapel_core.comm.emit') as m_emit:
            with self._unlink_fails(), pytest.raises(Exception):
                handle_user_deleted(event)

        m_emit.assert_not_called()
        assert Image.objects.count() == 1

    def test_a_successful_erasure_still_reports_success(self, user):
        image = self._image_with_bytes(user)
        stored_path = image.original.path

        removed = CDNGDPRProvider().purge_unreferenced(user.id)

        assert removed == 1
        assert not Image.objects.filter(pk=image.pk).exists()
        assert not Path(stored_path).exists()

    def test_an_already_missing_blob_is_erased_not_stranded(self, user):
        """Idempotence: a retry meets files the first attempt already removed."""
        image = self._image_with_bytes(user)
        Path(image.original.path).unlink()

        assert CDNGDPRProvider().purge_unreferenced(user.id) == 1
        assert not Image.objects.filter(pk=image.pk).exists()
