"""The unclaimed-media lifecycle: upload starts UNCLAIMED with a TTL, a ref
claims it, losing the last ref restarts the clock, and ``sweep_unclaimed``
reaps what is zero-ref AND expired — bytes and row.

The clock the sweeper reads is ``unreferenced_since``, never ``created_at``:
media that spent a year attached to a product and was detached this morning
has this morning's stamp, so it gets the full TTL grace again. ``created_at``
ages media that was *never* claimed only because the stamp is set at upload.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from stapel_cdn.conf import cdn_settings
from stapel_cdn.models import Audio, File, Image, Video
from stapel_cdn.services import apply_ref_sync, sweep_unclaimed

pytestmark = pytest.mark.django_db

TTL_HOURS = cdn_settings.UNCLAIMED_TTL_HOURS


def _upload(name="pic.jpg", data=b"bytes-of-a-file"):
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def _image(file_hash="a" * 64, refs=None, type="avatar", original=None):
    with patch("stapel_cdn.tasks.process_image_async"):
        return Image.objects.create(
            file_hash=file_hash,
            original_filename="pic.jpg",
            file_extension=".jpg",
            original_width=10,
            original_height=10,
            original_size=100,
            type=type,
            refs=refs or [],
            **({"original": original} if original else {}),
        )


def _video(file_hash="b" * 64, refs=None):
    return Video.objects.create(
        file_hash=file_hash,
        original_filename="clip.mp4",
        file_extension=".mp4",
        original_size=200,
        refs=refs or [],
    )


def _file(file_hash="c" * 64, refs=None):
    return File.objects.create(
        file_hash=file_hash,
        original_filename="doc.pdf",
        file_extension=".pdf",
        mime_type="application/pdf",
        original_size=300,
        refs=refs or [],
    )


def _audio(file_hash="d" * 64, refs=None):
    return Audio.objects.create(
        file_hash=file_hash,
        original_filename="take.m4a",
        file_extension=".m4a",
        original_size=400,
        refs=refs or [],
    )


def _age(obj, hours):
    """Backdate the unclaimed clock as if the object went unreferenced
    ``hours`` ago (bypasses save() so nothing restamps)."""
    type(obj).objects.filter(pk=obj.pk).update(
        unreferenced_since=timezone.now() - timedelta(hours=hours)
    )
    obj.refresh_from_db()


class TestUnreferencedSinceStamp:
    """The field itself: stamped at upload, cleared by a claim, restamped
    when the last ref goes."""

    def test_upload_starts_unclaimed_with_a_stamp(self):
        for obj in (_image(), _video(), _file(), _audio()):
            assert obj.unreferenced_since is not None
            assert timezone.now() - obj.unreferenced_since < timedelta(minutes=1)

    def test_claim_clears_the_stamp(self):
        image = _image()
        apply_ref_sync("shop", "product", "1", [], [f"avatar/{image.file_hash}"])
        image.refresh_from_db()
        assert image.refs == ["shop/product/1"]
        assert image.unreferenced_since is None

    def test_losing_the_last_ref_restamps_now_not_created_at(self):
        """The mandated draft scenario at lib level: ref added then removed
        via apply_ref_sync — the clock restarts at the detach, so an old
        upload that was briefly claimed gets the full TTL again."""
        image = _image()
        ref = f"avatar/{image.file_hash}"
        # Pretend the upload (and its first stamp) happened long ago.
        Image.objects.filter(pk=image.pk).update(
            created_at=timezone.now() - timedelta(hours=TTL_HOURS * 10),
            unreferenced_since=timezone.now() - timedelta(hours=TTL_HOURS * 10),
        )
        apply_ref_sync("shop", "product", "1", [], [ref])
        apply_ref_sync("shop", "product", "1", [ref], [])
        image.refresh_from_db()
        assert image.refs == []
        assert image.unreferenced_since is not None
        assert timezone.now() - image.unreferenced_since < timedelta(minutes=1)

    def test_keeping_other_refs_does_not_restamp(self):
        image = _image()
        ref = f"avatar/{image.file_hash}"
        apply_ref_sync("shop", "product", "1", [], [ref])
        apply_ref_sync("site", "page", "7", [], [ref])
        apply_ref_sync("shop", "product", "1", [ref], [])
        image.refresh_from_db()
        assert image.refs == ["site/page/7"]
        assert image.unreferenced_since is None


class TestSweepUnclaimed:
    def test_fresh_unclaimed_upload_inside_ttl_is_kept(self):
        image = _image(original=_upload())
        report = sweep_unclaimed()
        assert Image.objects.filter(pk=image.pk).exists()
        assert report["objects_removed"] == 0

    def test_expired_unclaimed_upload_is_reaped_bytes_and_row(self):
        image = _image(original=_upload())
        stored = image.original.name
        assert image.original.storage.exists(stored)
        _age(image, TTL_HOURS + 1)

        report = sweep_unclaimed()

        assert not Image.objects.filter(pk=image.pk).exists()
        assert not image.original.storage.exists(stored)
        assert report["objects_removed"] == 1
        assert report["blobs_unlinked"] == 1

    @pytest.mark.parametrize("factory", [_image, _video, _file, _audio])
    def test_every_media_model_is_swept(self, factory):
        obj = factory()
        _age(obj, TTL_HOURS + 1)
        sweep_unclaimed()
        assert not type(obj).objects.filter(pk=obj.pk).exists()

    def test_claimed_media_is_never_reaped_regardless_of_age(self):
        image = _image(refs=["shop/product/1"])
        # Ancient by every clock, and even carrying a (stale, impossible)
        # stamp: refs are non-empty, so the sweeper must not touch it.
        Image.objects.filter(pk=image.pk).update(
            created_at=timezone.now() - timedelta(hours=TTL_HOURS * 100),
            unreferenced_since=timezone.now() - timedelta(hours=TTL_HOURS * 100),
        )
        report = sweep_unclaimed()
        assert Image.objects.filter(pk=image.pk).exists()
        assert report["objects_removed"] == 0

    def test_two_claims_detach_one_survives_detach_both_reaps_after_ttl(self):
        """The mandated two-claims scenario: with one of two refs gone the
        object survives any sweep; with both gone it is reaped only after
        TTL counted from the second detach — NOT from created_at."""
        image = _image()
        ref = f"avatar/{image.file_hash}"
        Image.objects.filter(pk=image.pk).update(
            created_at=timezone.now() - timedelta(hours=TTL_HOURS * 10)
        )
        apply_ref_sync("shop", "product", "1", [], [ref])
        apply_ref_sync("chat", "message", "2", [], [ref])

        apply_ref_sync("shop", "product", "1", [ref], [])
        sweep_unclaimed()
        assert Image.objects.filter(pk=image.pk).exists()

        apply_ref_sync("chat", "message", "2", [ref], [])
        # created_at is TTL*10 old — a sweep right after the detach keeps it.
        sweep_unclaimed()
        image.refresh_from_db()
        assert image.refs == []

        _age(image, TTL_HOURS + 1)
        sweep_unclaimed()
        assert not Image.objects.filter(pk=image.pk).exists()

    def test_draft_upload_claimed_then_detached_is_reaped_after_ttl(self):
        """Draft flow end to end: upload, attach to a draft, discard the
        draft — the object is kept for a fresh TTL, then reaped."""
        image = _image(original=_upload())
        ref = f"avatar/{image.file_hash}"
        apply_ref_sync("shop", "draft", "9", [], [ref])
        apply_ref_sync("shop", "draft", "9", [ref], [])

        sweep_unclaimed()
        assert Image.objects.filter(pk=image.pk).exists()

        _age(image, TTL_HOURS + 1)
        sweep_unclaimed()
        assert not Image.objects.filter(pk=image.pk).exists()

    def test_shared_blob_survives_reaping_one_holder(self):
        """Content-addressed storage: reaping an expired row must not unlink
        bytes another row still serves (same rule as erasure)."""
        from stapel_core.django.users.models import User

        holder = User.objects.create_user(
            username="holder", email="holder@example.com", password="x"
        )
        data = b"the-same-bytes"
        expired = _image(original=_upload(data=data))
        with patch("stapel_cdn.tasks.process_image_async"):
            keeper = Image.objects.create(
                file_hash="a" * 64,
                original_filename="pic.jpg",
                file_extension=".jpg",
                original_width=10,
                original_height=10,
                original_size=100,
                type="avatar",
                refs=["shop/product/1"],
                uploaded_by=holder,
                original=_upload(data=data),
            )
        stored = keeper.original.name
        _age(expired, TTL_HOURS + 1)

        report = sweep_unclaimed()

        assert not Image.objects.filter(pk=expired.pk).exists()
        assert Image.objects.filter(pk=keeper.pk).exists()
        assert keeper.original.storage.exists(stored)
        assert report["objects_removed"] == 1
        assert report["blobs_unlinked"] == 0

    def test_dry_run_counts_and_deletes_nothing(self):
        image = _image(original=_upload())
        stored = image.original.name
        _age(image, TTL_HOURS + 1)

        report = sweep_unclaimed(dry_run=True)

        assert report["candidates"] == 1
        assert report["objects_removed"] == 0
        assert Image.objects.filter(pk=image.pk).exists()
        assert image.original.storage.exists(stored)

    def test_ttl_default_is_48_hours(self):
        assert cdn_settings.UNCLAIMED_TTL_HOURS == 48


class TestSweepWiring:
    """The task, the beat entry and the management command all run THE
    service function, not private copies of the loop."""

    def test_celery_task_runs_the_sweep(self):
        from stapel_cdn.tasks import sweep_unclaimed as sweep_task

        image = _image()
        _age(image, TTL_HOURS + 1)
        report = sweep_task()
        assert not Image.objects.filter(pk=image.pk).exists()
        assert report["objects_removed"] == 1

    def test_beat_schedule_carries_the_sweep_entry(self):
        from stapel_cdn.tasks import SWEEP_TASK_NAME, get_cdn_beat_schedule

        schedule = get_cdn_beat_schedule()
        tasks = {entry["task"] for entry in schedule.values()}
        assert SWEEP_TASK_NAME in tasks

    def test_management_command_sweeps(self):
        image = _image()
        _age(image, TTL_HOURS + 1)
        call_command("cdn_sweep_unclaimed")
        assert not Image.objects.filter(pk=image.pk).exists()

    def test_management_command_dry_run_keeps_everything(self):
        image = _image()
        _age(image, TTL_HOURS + 1)
        call_command("cdn_sweep_unclaimed", "--dry-run")
        assert Image.objects.filter(pk=image.pk).exists()


class TestSweepBeatCheck:
    """W013 — this process runs beat for other work and nothing sweeps."""

    def _findings(self):
        from stapel_cdn.checks import check_sweep_beat_schedule

        return check_sweep_beat_schedule(None)

    @override_settings(CELERY_BEAT_SCHEDULE={
        "other": {"task": "somebody.else", "schedule": 60},
    })
    def test_warns_when_beat_runs_and_sweep_is_absent(self):
        from stapel_cdn.checks import W013_SWEEP_NOT_SCHEDULED

        findings = self._findings()
        assert [f.id for f in findings] == [W013_SWEEP_NOT_SCHEDULED]

    @override_settings()
    def test_silent_when_no_beat_schedule_exists(self):
        from django.conf import settings

        if hasattr(settings, "CELERY_BEAT_SCHEDULE"):
            del settings.CELERY_BEAT_SCHEDULE
        assert self._findings() == []

    def test_silent_when_the_sweep_is_scheduled(self):
        from stapel_cdn.tasks import get_cdn_beat_schedule

        with override_settings(CELERY_BEAT_SCHEDULE=get_cdn_beat_schedule()):
            assert self._findings() == []
