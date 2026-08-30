"""The ``user.merged`` half of the account life cycle (stapel-core 0.52.1).

A merge is the opposite instruction to a deletion: the guest account ceased
to exist and its rows belong to the survivor. ``uploaded_by`` is
``SET_NULL``, so a module that answered only ``user.deleted`` would lose the
uploads *silently* — they keep serving, they stop belonging to anybody, and
no erasure is ever requested for them. These tests pin the four properties a
bus consumer has to have: it moves the rows, a redelivery moves nothing more,
a malformed payload is ACKed rather than redelivered forever, and an event
about users this deployment never saw does nothing.
"""
from unittest.mock import MagicMock, patch

import pytest
from stapel_core.django.users.models import User

from stapel_cdn.actions import MergeTargetNotReady, handle_user_merged
from stapel_cdn.models import Audio, File, Image, Video

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "", "  ", "['x']"]


@pytest.fixture
def guest(db):
    return User.objects.create_user(
        username="guest", email="guest@example.com", password="x"
    )


@pytest.fixture
def survivor(db):
    return User.objects.create_user(
        username="survivor", email="survivor@example.com", password="x"
    )


def _event(**payload):
    event = MagicMock()
    event.payload = payload
    event.event_id = "evt-merge"
    return event


def _image(user, file_hash="11" * 32, type="avatar", refs=None):
    with patch("stapel_cdn.tasks.process_image_async"):
        return Image.objects.create(
            file_hash=file_hash,
            type=type,
            original_filename="pic.jpg",
            file_extension=".jpg",
            original_width=10,
            original_height=10,
            original_size=100,
            uploaded_by=user,
            refs=refs or [],
        )


def _video(user, file_hash="22" * 32, refs=None):
    return Video.objects.create(
        file_hash=file_hash,
        original_filename="clip.mp4",
        file_extension=".mp4",
        original_size=200,
        uploaded_by=user,
        refs=refs or [],
    )


def _file(user, file_hash="33" * 32, refs=None):
    return File.objects.create(
        file_hash=file_hash,
        original_filename="doc.pdf",
        file_extension=".pdf",
        mime_type="application/pdf",
        original_size=300,
        uploaded_by=user,
        refs=refs or [],
    )


def _audio(user, file_hash="44" * 32, refs=None):
    return Audio.objects.create(
        file_hash=file_hash,
        original_filename="voice.m4a",
        file_extension=".m4a",
        mime_type="audio/mp4",
        original_size=400,
        uploaded_by=user,
        refs=refs or [],
    )


class TestHappyPath:
    def test_every_media_model_is_re_parented(self, guest, survivor):
        rows = [_image(guest), _video(guest), _file(guest), _audio(guest)]

        handle_user_merged(_event(from_user_id=str(guest.pk), into_user_id=str(survivor.pk)))

        for row in rows:
            row.refresh_from_db()
            assert row.uploaded_by_id == survivor.pk
        for model in (Image, Video, File, Audio):
            assert not model.objects.filter(uploaded_by_id=guest.pk).exists()

    def test_the_survivors_own_media_is_untouched(self, guest, survivor):
        theirs = _image(survivor, file_hash="aa" * 32)
        _image(guest, file_hash="bb" * 32)

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        theirs.refresh_from_db()
        assert theirs.uploaded_by_id == survivor.pk
        assert Image.objects.filter(uploaded_by_id=survivor.pk).count() == 2

    def test_media_of_a_third_party_is_untouched(self, guest, survivor):
        third = User.objects.create_user(
            username="third", email="third@example.com", password="x"
        )
        theirs = _video(third, file_hash="cc" * 32)
        _video(guest)

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        theirs.refresh_from_db()
        assert theirs.uploaded_by_id == third.pk


class TestDuplicateBytes:
    """Dedup is owner-scoped, so both accounts may hold the same bytes — one
    row each over one blob. The per-owner uniqueness constraint forbids a
    blind update, so the guest's duplicate is folded into the survivor's row.
    """

    def test_a_duplicate_image_is_folded_and_its_refs_carried(self, guest, survivor):
        keeper = _image(survivor, file_hash="dd" * 32, refs=["shop/product/1"])
        _image(guest, file_hash="dd" * 32, refs=["shop/product/2"])

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        assert Image.objects.count() == 1
        keeper.refresh_from_db()
        assert keeper.uploaded_by_id == survivor.pk
        assert sorted(keeper.refs) == ["shop/product/1", "shop/product/2"]

    def test_the_same_bytes_under_a_different_image_type_still_move(
        self, guest, survivor
    ):
        """The constraint is (file_hash, type, uploaded_by): an avatar and a
        product image over one blob are two legitimate rows."""
        _image(survivor, file_hash="ee" * 32, type="avatar")
        guest_row = _image(guest, file_hash="ee" * 32, type="product")

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        guest_row.refresh_from_db()
        assert guest_row.uploaded_by_id == survivor.pk
        assert Image.objects.count() == 2

    def test_a_duplicate_video_is_folded(self, guest, survivor):
        keeper = _video(survivor, file_hash="ff" * 32, refs=["shop/product/1"])
        _video(guest, file_hash="ff" * 32, refs=[])

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        assert Video.objects.count() == 1
        keeper.refresh_from_db()
        assert keeper.refs == ["shop/product/1"]

    def test_audio_is_globally_unique_so_its_rows_only_move(self, guest, survivor):
        """``Audio.file_hash`` is ``unique=True`` across the table — one row
        per blob, whoever uploaded it first — so there is no per-owner
        duplicate to fold and every row simply moves."""
        assert not Audio._meta.constraints
        _audio(survivor, file_hash="88" * 32)
        guest_row = _audio(guest, file_hash="99" * 32)

        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=survivor.pk))

        guest_row.refresh_from_db()
        assert guest_row.uploaded_by_id == survivor.pk
        assert Audio.objects.filter(uploaded_by_id=survivor.pk).count() == 2


class TestIdempotency:
    def test_a_redelivery_changes_nothing_further(self, guest, survivor):
        _image(guest)
        _video(guest)
        event = _event(from_user_id=guest.pk, into_user_id=survivor.pk)

        handle_user_merged(event)
        snapshot = sorted(
            Image.objects.values_list("id", "uploaded_by_id")
        ) + sorted(Video.objects.values_list("id", "uploaded_by_id"))

        handle_user_merged(event)
        handle_user_merged(event)

        again = sorted(
            Image.objects.values_list("id", "uploaded_by_id")
        ) + sorted(Video.objects.values_list("id", "uploaded_by_id"))
        assert again == snapshot


class TestPoisonPayloads:
    """A raise here is a poison pill: the bus redelivers a payload no retry
    can repair. Every malformed shape must be ACKed and touch no rows.

    ``not-a-uuid`` is the one that bites: Django answers an uncoercible UUID
    with ``django.core.exceptions.ValidationError``, which is NOT a
    ``ValueError``, so a ``(ValueError, TypeError)``-only guard lets it out.
    """

    def _snapshot(self):
        return [
            sorted(model.objects.values_list("id", "uploaded_by_id"))
            for model in (Image, Video, File, Audio)
        ]

    def test_a_malformed_from_id_acks_and_moves_nothing(self, guest, survivor):
        _image(guest)
        before = self._snapshot()
        for bad in BAD_IDS:
            handle_user_merged(_event(from_user_id=bad, into_user_id=str(survivor.pk)))
        assert self._snapshot() == before

    def test_a_malformed_into_id_acks_and_moves_nothing(self, guest, survivor):
        _image(guest)
        before = self._snapshot()
        for bad in BAD_IDS:
            handle_user_merged(_event(from_user_id=str(guest.pk), into_user_id=bad))
        assert self._snapshot() == before

    def test_a_missing_id_acks_and_moves_nothing(self, guest, survivor):
        _image(guest)
        before = self._snapshot()
        handle_user_merged(_event())
        handle_user_merged(_event(from_user_id=str(guest.pk)))
        handle_user_merged(_event(into_user_id=str(survivor.pk)))
        assert self._snapshot() == before

    def test_an_empty_payload_object_acks(self, guest):
        event = MagicMock()
        event.payload = None
        event.event_id = "evt-empty"
        handle_user_merged(event)

    def test_a_self_merge_is_a_no_op(self, guest):
        row = _image(guest)
        handle_user_merged(_event(from_user_id=guest.pk, into_user_id=guest.pk))
        row.refresh_from_db()
        assert row.uploaded_by_id == guest.pk


class TestUnknownUsers:
    def test_an_event_about_users_with_no_media_here_does_nothing(self, survivor):
        stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        theirs = _image(survivor, file_hash="77" * 32)

        handle_user_merged(_event(from_user_id=stranger.pk, into_user_id=survivor.pk))

        theirs.refresh_from_db()
        assert theirs.uploaded_by_id == survivor.pk
        assert Image.objects.count() == 1

    def test_a_survivor_with_no_user_row_yet_is_retried_not_dropped(self, guest):
        """The guest HAS media and nothing can point a FK at a user that does
        not exist here yet. Returning success would let the outbox mark the
        event delivered and lose the uploads, so the handler raises instead —
        the comm layer's retry signal."""
        import uuid

        _image(guest)
        with pytest.raises(MergeTargetNotReady):
            handle_user_merged(
                _event(from_user_id=guest.pk, into_user_id=str(uuid.uuid4()))
            )
        assert Image.objects.filter(uploaded_by_id=guest.pk).count() == 1


class TestLifecycleCheck:
    """stapel_core.lifecycle.E001 — an app that answers ``user.deleted`` and
    not ``user.merged`` is a system-check ERROR. Registered here so the pair
    cannot be broken by a later refactor without a red test."""

    def test_the_lifecycle_pair_check_is_green(self):
        from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs

        assert check_lifecycle_pairs() == []
