"""Subject-scoped erasure (stapel-gdpr 0.5.0): account, workspace, file and
recording, receipted with what was actually removed, plus the liveness probe
answered from the same subscriber.

`emit` is patched rather than delivered: this suite runs without the outbox
app installed (see conftest), and the payload is what these tests are about.
"""
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from stapel_core.django.users.models import User

from stapel_cdn.actions import (
    handle_erasure_requested,
    handle_owner_probe,
    handle_user_deleted,
)
from stapel_cdn.erasure import erase
from stapel_cdn.models import Audio, File, Image, Video

pytestmark = pytest.mark.django_db

RECORDING_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="eraser", email="eraser@example.com", password="testpass123"
    )


def _upload(name="pic.jpg", data=b"bytes-of-a-file"):
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def _image(user=None, file_hash="a" * 64, refs=None, type="avatar", original=None):
    with patch("stapel_cdn.tasks.process_image_async"):
        return Image.objects.create(
            file_hash=file_hash,
            original_filename="pic.jpg",
            file_extension=".jpg",
            original_width=10,
            original_height=10,
            original_size=100,
            uploaded_by=user,
            type=type,
            refs=refs or [],
            **({"original": original} if original else {}),
        )


def _video(user=None, file_hash="b" * 64, refs=None):
    return Video.objects.create(
        file_hash=file_hash,
        original_filename="clip.mp4",
        file_extension=".mp4",
        original_size=200,
        uploaded_by=user,
        refs=refs or [],
    )


def _file(user=None, file_hash="c" * 64, refs=None):
    return File.objects.create(
        file_hash=file_hash,
        original_filename="doc.pdf",
        file_extension=".pdf",
        mime_type="application/pdf",
        original_size=300,
        uploaded_by=user,
        refs=refs or [],
    )


def _audio(user=None, file_hash="d" * 64, refs=None, original=None):
    return Audio.objects.create(
        file_hash=file_hash,
        original_filename="take.m4a",
        file_extension=".m4a",
        original_size=400,
        uploaded_by=user,
        refs=refs or [],
        **({"original": original} if original else {}),
    )


def _request(subject_type, subject_key, correlation_id="corr-1", **extra):
    """Deliver a gdpr.erasure.requested and return the emit mock."""
    event = MagicMock()
    event.payload = {
        "request_id": 7,
        "correlation_id": correlation_id,
        "subject_type": subject_type,
        "subject_key": str(subject_key),
        **extra,
    }
    event.event_id = "evt-erasure"
    with patch("stapel_core.comm.emit") as emit:
        handle_erasure_requested(event)
    return emit


def _receipt(emit):
    emit.assert_called_once()
    args, _ = emit.call_args
    assert args[0] == "gdpr.section.erased"
    return args[1]


class TestFileSubject:
    """`file` names the bytes: the ref is the object's identity."""

    def test_destroys_every_row_over_those_bytes_and_the_blob(self, user):
        image = _image(user, original=_upload())
        stored = image.original.name
        assert image.original.storage.exists(stored)

        emit = _request("file", f"avatar/{image.file_hash}")

        assert not Image.objects.filter(pk=image.pk).exists()
        assert not image.original.storage.exists(stored)
        receipt = _receipt(emit)
        assert receipt["owner"] == "media"
        assert receipt["subject_type"] == "file"
        assert receipt["subject_key"] == f"avatar/{image.file_hash}"
        assert receipt["receipt_id"] == "media:corr-1"
        assert receipt["counts"]["objects_removed"] == 1
        assert receipt["counts"]["blobs_unlinked"] == 1

    def test_every_holder_of_the_content_goes(self, user):
        """Identical bytes held by two principals are two rows over one blob;
        a ref names the content, so both rows are erased."""
        other = User.objects.create_user(username="other", password="x")
        _image(user)
        _image(other)
        assert Image.objects.count() == 2

        emit = _request("file", f"avatar/{'a' * 64}")

        assert Image.objects.count() == 0
        assert _receipt(emit)["counts"]["objects_removed"] == 2

    def test_a_live_reference_is_counted_not_a_reason_to_keep_the_bytes(self, user):
        _image(user, refs=["shop/product/1", "shop/product/2"])

        emit = _request("file", f"avatar/{'a' * 64}")

        assert Image.objects.count() == 0
        assert _receipt(emit)["counts"]["refs_stranded"] == 2

    def test_video_file_and_audio_refs_route_to_their_models(self, user):
        _video(user)
        _file(user)
        _audio(user)

        for ref, model in (
            (f"video/{'b' * 64}", Video),
            (f"file/{'c' * 64}", File),
            (f"audio/{'d' * 64}", Audio),
        ):
            emit = _request("file", ref)
            assert model.objects.count() == 0
            assert _receipt(emit)["counts"]["objects_removed"] == 1

    def test_only_the_named_type_is_touched(self, user):
        _image(user, type="avatar")
        _image(user, type="product", file_hash="a" * 64)

        _request("file", f"avatar/{'a' * 64}")

        assert [i.type for i in Image.objects.all()] == ["product"]

    def test_redelivery_receipts_zeros(self, user):
        _image(user)

        first = _receipt(_request("file", f"avatar/{'a' * 64}"))
        second = _receipt(_request("file", f"avatar/{'a' * 64}"))

        assert first["counts"]["objects_removed"] == 1
        assert second["counts"]["objects_removed"] == 0

    def test_an_unlocatable_ref_refuses_instead_of_receipting(self, user):
        _image(user)

        for bad in ("not-a-ref", "sticker/abc", "avatar/"):
            with pytest.raises(ValueError):
                erase("file", bad)
        assert Image.objects.count() == 1


class TestEntitySubjects:
    """`recording` / `workspace` name an entity; its reference is what goes,
    and an object nothing references any more goes with it."""

    def test_recording_destroys_what_it_was_the_last_holder_of(self, user):
        audio = _audio(
            user,
            refs=[f"recordings/recording/{RECORDING_ID}"],
            original=_upload("take.m4a", b"audio-bytes"),
        )
        stored = audio.original.name

        emit = _request("recording", RECORDING_ID)

        assert not Audio.objects.filter(pk=audio.pk).exists()
        assert not audio.original.storage.exists(stored)
        counts = _receipt(emit)["counts"]
        assert counts["objects_removed"] == 1
        assert counts["refs_removed"] == 1
        assert counts["blobs_unlinked"] == 1

    def test_media_another_entity_still_uses_keeps_serving(self, user):
        image = _image(
            user, refs=[f"recordings/recording/{RECORDING_ID}", "shop/product/9"]
        )

        emit = _request("recording", RECORDING_ID)

        image.refresh_from_db()
        assert image.refs == ["shop/product/9"]
        counts = _receipt(emit)["counts"]
        assert counts["objects_removed"] == 0
        assert counts["objects_kept_referenced"] == 1
        assert counts["refs_removed"] == 1

    def test_every_service_reference_to_the_entity_matches(self, user):
        image = _image(
            user,
            refs=[
                f"recordings/recording/{RECORDING_ID}",
                f"agent/recording/{RECORDING_ID}",
            ],
        )

        emit = _request("recording", RECORDING_ID)

        assert not Image.objects.filter(pk=image.pk).exists()
        assert _receipt(emit)["counts"]["refs_removed"] == 2

    def test_a_different_entity_of_the_same_type_is_untouched(self, user):
        keep = _image(user, refs=["recordings/recording/other-id"])

        _request("recording", RECORDING_ID)

        keep.refresh_from_db()
        assert keep.refs == ["recordings/recording/other-id"]

    def test_workspace_uses_the_same_reverse_index(self, user):
        doomed = _file(user, refs=[f"workspaces/workspace/{WORKSPACE_ID}"])
        unrelated = _video(user, refs=["shop/product/1"])

        emit = _request("workspace", WORKSPACE_ID)

        assert not File.objects.filter(pk=doomed.pk).exists()
        assert Video.objects.filter(pk=unrelated.pk).exists()
        assert _receipt(emit)["counts"]["objects_removed"] == 1

    def test_redelivery_receipts_zeros(self, user):
        _image(user, refs=[f"recordings/recording/{RECORDING_ID}"])

        first = _receipt(_request("recording", RECORDING_ID))
        second = _receipt(_request("recording", RECORDING_ID))

        assert first["counts"]["objects_removed"] == 1
        assert second["counts"] == {
            "objects_removed": 0,
            "blobs_unlinked": 0,
            "refs_removed": 0,
            "refs_stranded": 0,
            "objects_kept_referenced": 0,
        }


class TestAccountSubject:
    def test_erases_orphans_anonymizes_the_rest_and_receipts_counts(self, user):
        _image(user, refs=[])
        referenced = _video(user, refs=["shop/product/1"])

        emit = _request("account", user.id)

        assert Image.objects.count() == 0
        referenced.refresh_from_db()
        assert referenced.uploaded_by is None
        counts = _receipt(emit)["counts"]
        assert counts == {"objects_removed": 1, "objects_anonymized": 1}

    def test_audio_is_erased_too(self, user):
        """Audio was added after the provider's model loop was written, so an
        account erasure used to leave the user's recordings in place."""
        _audio(user, refs=[])

        _request("account", user.id)

        assert Audio.objects.count() == 0

    def test_redelivery_receipts_zeros(self, user):
        _image(user, refs=[])

        first = _receipt(_request("account", user.id))
        second = _receipt(_request("account", user.id))

        assert first["counts"]["objects_removed"] == 1
        assert second["counts"] == {"objects_removed": 0, "objects_anonymized": 0}

    def test_user_deleted_still_erases_and_still_receipts_its_legacy_shape(
        self, user
    ):
        """The deprecated event keeps working for one minor, routed through
        the same erase; its receipt keeps the 0.4.x payload so a host on the
        older orchestrator still completes."""
        _image(user, refs=[])
        event = MagicMock()
        event.payload = {"user_id": user.id, "correlation_id": "corr-42"}

        with patch("stapel_core.comm.emit") as emit:
            handle_user_deleted(event)

        assert Image.objects.count() == 0
        args, _ = emit.call_args
        assert args[0] == "gdpr.section.erased"
        assert args[1] == {
            "user_id": str(user.id),
            "correlation_id": "corr-42",
            "service": "media",
        }


class TestForeignAndMalformedRequests:
    def test_subject_type_this_owner_does_not_claim_is_ignored(self, user):
        _image(user)

        emit = _request("document", "whatever")

        assert Image.objects.count() == 1
        emit.assert_not_called()

    def test_request_without_a_subject_is_not_receipted(self):
        event = MagicMock()
        event.payload = {"correlation_id": "corr-9"}
        event.event_id = "evt-bad"

        with patch("stapel_core.comm.emit") as emit:
            handle_erasure_requested(event)

        emit.assert_not_called()

    def test_erase_refuses_an_unknown_subject_type(self):
        with pytest.raises(ValueError):
            erase("meeting", "abc")


class TestOwnerProbe:
    def test_answers_alive_with_the_subjects_it_claims(self):
        event = MagicMock()
        event.payload = {"correlation_id": "corr-probe"}

        with patch("stapel_core.comm.emit") as emit:
            handle_owner_probe(event)

        args, _ = emit.call_args
        assert args[0] == "gdpr.owner.alive"
        assert args[1] == {
            "owner": "media",
            "subject_types": ["account", "workspace", "file", "recording"],
            "correlation_id": "corr-probe",
        }

    def test_the_probe_is_answered_from_the_erasure_subscriber(self):
        """Co-location is the contract: gdpr's W006 reads these answers as
        evidence that the erasure path is consumed, so an answer from a
        module that does not also erase would make the check lie."""
        assert handle_owner_probe.__module__ == handle_erasure_requested.__module__ == (
            "stapel_cdn.actions"
        )

    def test_the_owner_name_is_the_provider_section(self):
        from stapel_cdn.erasure import OWNER
        from stapel_cdn.gdpr import CDNGDPRProvider

        assert OWNER == CDNGDPRProvider.section

    def test_claimed_subjects_all_have_an_implementation(self):
        from stapel_cdn.erasure import SUBJECT_TYPES

        keys = {
            "file": f"avatar/{'e' * 64}",
            "account": "33333333-3333-3333-3333-333333333333",
        }
        for subject_type in SUBJECT_TYPES:
            erase(subject_type, keys.get(subject_type, "some-id"))  # no ValueError
