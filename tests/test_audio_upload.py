"""``POST /cdn/api/v1/upload/audio/`` — the intake the recordings half lacked.

Everything around this endpoint already shipped: the ``Audio`` model, its
content-addressed storage, the ``post_save`` hook that queues the waveform
pass, ``AudioProcessingService`` (ffprobe duration + ffmpeg ``showwavespic``),
the ``audio/<hash>`` ref that ``resolve_refs``/``cdn.describe`` speak, the
GDPR erasure provider, and ``ALLOWED_AUDIO_EXTENSIONS`` / ``MAX_AUDIO_SIZE``
sitting in ``conf.py`` marked "reserved, not an active knob". The one missing
piece was the HTTP door, so a browser recording a voice message had nowhere
to put it.

What is pinned here:

- the door itself: a recording is stored and comes back with the
  ``audio/<hash>`` ref a chat message keeps;
- ``.webm`` is accepted, because that is what ``MediaRecorder`` produces —
  an audio allowlist without it refuses the only recorder a browser has;
- the three refusals every other intake in this module performs, in the same
  order and *before* the body is hashed: byte ceiling, extension allowlist,
  active-content sniff;
- owner-scoped dedup: the same bytes twice cost one row;
- the quota: ``Audio`` is now in ``ownership._owned_models()``, so a
  recording counts towards the per-owner ceilings like every other stored
  object. A model with an intake and no quota row is a hole in the ceiling,
  not a smaller ceiling;
- ``file/exists/`` can see a recording — a dedup check blind to one of the
  kinds it precedes sends the caller to re-upload bytes it already holds;
- the async seam: the 201 carries no duration and no waveform (ffmpeg on a
  request thread would transcode inline), and after the metadata pass runs,
  ``/describe/`` answers with ``duration_ms`` and the waveform ``preview_b64``.

ffmpeg is not installed in this environment on purpose (see
tests/test_media_metadata.py): the happy path is driven through the
``probes`` seam with a stub returning what a real ffmpeg returns, and the
absence path is the environment's own truth.
"""
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PILImage
from rest_framework import status
from rest_framework.test import APIClient
from stapel_core.django.api.permissions import IsNotAnonymousUser
from stapel_core.django.users.models import User

from stapel_cdn import probes, views
from stapel_cdn.conf import cdn_settings
from stapel_cdn.models import Audio
from stapel_cdn.ownership import _owned_models, owner_usage
from stapel_cdn.services import AudioProcessingService

URL = "/cdn/api/v1/upload/audio/"
EXISTS_URL = "/cdn/api/v1/file/exists/"
DESCRIBE_URL = "/cdn/api/v1/describe/"

#: A WebM/Opus container header — the leading bytes a browser's MediaRecorder
#: writes (EBML magic), padded to something with a size.
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 512
OGG = b"OggS" + b"\x00" * 512


@pytest.fixture
def uploader(db):
    return User.objects.create_user(
        username="voice-uploader",
        email="voice@example.com",
        password="pw-not-for-production",
    )


@pytest.fixture
def client(uploader):
    api = APIClient()
    api.force_authenticate(user=uploader)
    return api


@pytest.fixture
def other_client(db):
    api = APIClient()
    api.force_authenticate(
        user=User.objects.create_user(
            username="voice-other",
            email="other@example.com",
            password="pw-not-for-production",
        )
    )
    return api


def recording(content=WEBM, name="voice.webm", content_type="audio/webm"):
    return SimpleUploadedFile(name=name, content=content, content_type=content_type)


def _png_bytes(width, height):
    """A real PNG, encoded by Pillow — what ffmpeg hands back in production."""
    buffer = BytesIO()
    PILImage.new("RGB", (width, height), color=(40, 90, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """A working ffmpeg/ffprobe, driven through the probes seam."""

    def probe_media(path):
        return {"width": None, "height": None, "duration_ms": 4500}

    def render_waveform_png(path, width, height, color="#3f7fbf"):
        return _png_bytes(width, height)

    monkeypatch.setattr(probes, "probe_media", probe_media)
    monkeypatch.setattr(probes, "render_waveform_png", render_waveform_png)


# ---------------------------------------------------------------------------
# the door
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAudioUploadStoresARecording:
    def test_webm_is_stored_and_answers_with_its_ref(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        response = client.post(URL, {"file": recording()}, format="multipart")

        assert response.status_code == status.HTTP_201_CREATED, response.content
        assert response.data["message"] == "Audio uploaded successfully"

        audio = Audio.objects.get()
        assert response.data["audio"]["ref"] == f"audio/{audio.file_hash}"
        assert response.data["audio"]["file_hash"] == audio.file_hash
        assert response.data["audio"]["file_extension"] == ".webm"
        assert response.data["audio"]["original_size"] == len(WEBM)
        assert audio.uploaded_by_id == client.handler._force_user.pk
        # Passthrough: the bytes are stored as they arrived, under the
        # content-addressed private prefix.
        assert f"audio/{audio.file_hash}/" in audio.original.name
        assert audio.original.storage.exists(audio.original.name)

    def test_the_201_carries_no_duration_and_no_waveform(
        self, client, settings, tmp_path
    ):
        """The metadata pass is async on purpose — ffmpeg on a request thread
        turns every voice message into a synchronous transcode. A null
        duration in the 201 is the contract, not a failure."""
        settings.MEDIA_ROOT = str(tmp_path)
        response = client.post(URL, {"file": recording()}, format="multipart")

        assert response.status_code == status.HTTP_201_CREATED, response.content
        assert response.data["audio"]["duration"] is None
        assert response.data["audio"]["preview_b64"] == ""
        assert response.data["audio"]["is_compressed"] is False

    @pytest.mark.parametrize(
        "name,content_type",
        [
            ("note.ogg", "audio/ogg"),
            ("note.opus", "audio/opus"),
            ("note.m4a", "audio/mp4"),
            ("note.mp3", "audio/mpeg"),
            ("note.wav", "audio/wav"),
        ],
    )
    def test_every_shipped_extension_is_accepted(
        self, client, settings, tmp_path, name, content_type
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        payload = OGG + name.encode()  # distinct bytes per case -> distinct hash
        response = client.post(
            URL,
            {"file": recording(payload, name=name, content_type=content_type)},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_webm_is_in_the_shipped_allowlist(self):
        """The browser recorder's default container. Without it the endpoint
        refuses the only thing a chat client can actually produce."""
        assert ".webm" in cdn_settings.ALLOWED_AUDIO_EXTENSIONS


@pytest.mark.django_db
class TestAudioDedup:
    def test_the_same_bytes_twice_cost_one_row(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        first = client.post(URL, {"file": recording()}, format="multipart")
        assert first.status_code == status.HTTP_201_CREATED, first.content

        second = client.post(
            URL, {"file": recording(name="again.webm")}, format="multipart"
        )
        assert second.status_code == status.HTTP_200_OK, second.content
        assert second.data["message"] == "Audio already exists"
        assert second.data["audio"]["id"] == first.data["audio"]["id"]
        assert Audio.objects.count() == 1

    def test_dedup_is_owner_scoped(self, client, other_client, settings, tmp_path):
        """"Have these bytes been seen before?" is not a question one caller
        may ask about another's storage (ownership.py). Two members sending
        the same voice clip get two rows over one content-addressed blob —
        neither is told about the other, and neither is refused.

        This is also the regression pin for the constraint 0005 never got
        round to on this model: ``Audio.file_hash`` was globally ``unique``
        while Image/Video/File had moved to a per-owner pair, so the second
        owner here used to be an IntegrityError — a 500 on an ordinary
        request, invisible only because the model had no intake to reach it.
        """
        settings.MEDIA_ROOT = str(tmp_path)
        first = client.post(URL, {"file": recording()}, format="multipart")
        assert first.status_code == status.HTTP_201_CREATED, first.content

        second = other_client.post(URL, {"file": recording()}, format="multipart")
        assert second.status_code == status.HTTP_201_CREATED, second.content
        assert second.data["audio"]["id"] != first.data["audio"]["id"]
        assert Audio.objects.count() == 2
        assert (
            Audio.objects.values_list("file_hash", flat=True).distinct().count() == 1
        )


# ---------------------------------------------------------------------------
# what it refuses, before it stores anything
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAudioUploadIsBounded:
    def test_oversized_recording_is_refused_with_413(self, client, settings):
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "product"),
                               "MAX_AUDIO_SIZE": 1024}
        response = client.post(
            URL, {"file": recording(WEBM + b"\x00" * 4096)}, format="multipart"
        )
        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        assert response.json()["localizable_error"] == "error.413.file_too_large"
        assert Audio.objects.count() == 0

    def test_the_shipped_cap_is_the_documented_one(self):
        assert cdn_settings.MAX_AUDIO_SIZE == 50 * 1024 * 1024

    def test_the_cap_is_overridable_through_the_namespace(self):
        with override_settings(STAPEL_CDN={"MAX_AUDIO_SIZE": 4096}):
            assert cdn_settings.MAX_AUDIO_SIZE == 4096
        assert cdn_settings.MAX_AUDIO_SIZE == 50 * 1024 * 1024

    def test_an_extension_off_the_allowlist_is_refused(self, client):
        response = client.post(
            URL,
            {"file": recording(b"hello", name="notes.txt", content_type="text/plain")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json()["localizable_error"] == "error.400.invalid_format"
        assert Audio.objects.count() == 0

    def test_a_video_only_extension_is_refused(self, client):
        """`.mp4` is on the VIDEO allowlist, not this one. `.webm` is
        deliberately on both — the container is shared and the endpoint the
        caller chose decides which model the bytes become — but the audio
        door does not inherit the whole video list."""
        response = client.post(
            URL,
            {"file": recording(b"\x00\x00\x00\x1cftypisom", name="clip.mp4",
                               content_type="video/mp4")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert Audio.objects.count() == 0

    def test_markup_wearing_an_audio_extension_is_refused(self, client):
        """Extension and Content-Type are both written by the caller, so the
        leading bytes get the last word: everything under the media root is
        served from the media origin."""
        response = client.post(
            URL,
            {"file": recording(b"<html><script>alert(1)</script></html>",
                               name="voice.webm")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json()["localizable_error"] == "error.400.file_type_not_allowed"
        assert Audio.objects.count() == 0

    def test_no_file_is_a_400(self, client):
        response = client.post(URL, {}, format="multipart")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestAudioUploadIsMembersOnly:
    def test_unauthenticated_is_refused(self, db):
        response = APIClient().post(URL, {"file": recording()}, format="multipart")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert Audio.objects.count() == 0

    def test_a_guest_session_is_refused(self, db):
        """The one upload a guest owns is its avatar. A voice message is
        arbitrary bytes attached to a conversation it is not in, and a guest
        session costs one unauthenticated POST to mint."""
        api = APIClient()
        api.force_authenticate(user=User.create_anonymous_user())
        response = api.post(URL, {"file": recording()}, format="multipart")
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
        assert Audio.objects.count() == 0

    def test_the_gate_is_spelled_on_the_view(self):
        assert IsNotAnonymousUser in views.AudioUploadView.permission_classes


# ---------------------------------------------------------------------------
# the quota
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordingsCountTowardsTheQuota:
    def test_audio_is_one_of_the_owned_models(self):
        assert Audio in _owned_models()

    def test_a_stored_recording_shows_up_in_owner_usage(
        self, client, uploader, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        assert owner_usage(uploader) == (0, 0)

        response = client.post(URL, {"file": recording()}, format="multipart")
        assert response.status_code == status.HTTP_201_CREATED, response.content

        objects, used = owner_usage(uploader)
        assert objects == 1
        assert used == len(WEBM)

    def test_the_object_ceiling_refuses_the_second_recording(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "product"),
                               "MAX_OBJECTS_PER_OWNER": 1}
        first = client.post(URL, {"file": recording()}, format="multipart")
        assert first.status_code == status.HTTP_201_CREATED, first.content

        second = client.post(
            URL,
            {"file": recording(OGG, name="second.ogg", content_type="audio/ogg")},
            format="multipart",
        )
        assert second.status_code == status.HTTP_403_FORBIDDEN, second.content
        assert second.json()["localizable_error"] == "error.403.storage_quota_exceeded"
        assert Audio.objects.count() == 1

    def test_the_byte_ceiling_counts_the_recording_that_is_arriving(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "product"),
                               "MAX_BYTES_PER_OWNER": 64}
        response = client.post(URL, {"file": recording()}, format="multipart")
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
        assert response.json()["localizable_error"] == "error.403.storage_quota_exceeded"
        assert Audio.objects.count() == 0

    def test_a_recording_fills_the_ceiling_for_the_other_media_too(
        self, client, uploader, settings, tmp_path
    ):
        """The hole this closes: a ceiling counted across images, videos and
        files only. An owner at its limit could keep uploading voice messages
        forever, and an image upload after a recording saw a ceiling one
        object emptier than the storage actually was."""
        settings.MEDIA_ROOT = str(tmp_path)
        settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "product"),
                               "MAX_OBJECTS_PER_OWNER": 1}
        assert client.post(
            URL, {"file": recording()}, format="multipart"
        ).status_code == status.HTTP_201_CREATED

        buffer = BytesIO()
        PILImage.new("RGB", (16, 16), color="red").save(buffer, format="JPEG")
        image_response = client.post(
            "/cdn/api/v1/upload/image/",
            {"file": SimpleUploadedFile("photo.jpg", buffer.getvalue(),
                                        content_type="image/jpeg")},
            format="multipart",
        )
        assert image_response.status_code == status.HTTP_403_FORBIDDEN, (
            image_response.content
        )


# ---------------------------------------------------------------------------
# the dedup check that precedes the upload
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFileExistsSeesRecordings:
    def test_a_stored_recording_is_reported_as_audio(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        upload = client.post(URL, {"file": recording()}, format="multipart")
        assert upload.status_code == status.HTTP_201_CREATED, upload.content
        file_hash = upload.data["audio"]["file_hash"]

        response = client.get(EXISTS_URL, {"file_hash": file_hash})
        assert response.status_code == status.HTTP_200_OK, response.content
        assert response.data["exists"] is True
        assert response.data["type"] == "audio"
        assert response.data["file"]["ref"] == f"audio/{file_hash}"

    def test_another_owners_recording_is_invisible(
        self, client, other_client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        upload = client.post(URL, {"file": recording()}, format="multipart")
        file_hash = upload.data["audio"]["file_hash"]

        response = other_client.get(EXISTS_URL, {"file_hash": file_hash})
        assert response.status_code == status.HTTP_200_OK, response.content
        assert response.data["exists"] is False
        assert response.data["type"] is None


# ---------------------------------------------------------------------------
# the async half: what a chat bubble draws once ffmpeg has run
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDescribeAnswersForAnUploadedRecording:
    def test_duration_and_waveform_arrive_after_the_metadata_pass(
        self, client, settings, tmp_path, fake_ffmpeg
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        upload = client.post(URL, {"file": recording()}, format="multipart")
        assert upload.status_code == status.HTTP_201_CREATED, upload.content
        ref = upload.data["audio"]["ref"]

        # Before: the upload response's own snapshot says nothing is measured.
        assert upload.data["audio"]["render_meta"]["duration_ms"] is None
        assert upload.data["audio"]["render_meta"]["preview_b64"] is None

        # The pass the post_save signal queues, run here directly (no broker
        # in the test environment — tasks.process_audio_async is the same
        # two lines).
        AudioProcessingService.extract_metadata(Audio.objects.get())

        response = client.post(DESCRIBE_URL, {"refs": [ref]}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.content
        snapshot = response.data["items"][ref]
        assert snapshot["kind"] == "audio"
        assert snapshot["preview_kind"] == "waveform"
        assert snapshot["duration_ms"] == 4500
        assert snapshot["preview_b64"].startswith("data:image/webp;base64,")
        assert snapshot["meta_status"] == "ok"
        assert response.data["missing"] == []

    def test_without_ffmpeg_the_recording_is_still_playable(
        self, client, settings, tmp_path, monkeypatch
    ):
        """The degraded path is named, not fabricated: no duration, no
        waveform, a reason — and the stored bytes still serve."""

        def missing(*args, **kwargs):
            raise probes.MediaToolUnavailable(
                probes.REASON_FFPROBE_MISSING, "no 'ffprobe' on PATH"
            )

        monkeypatch.setattr(probes, "probe_media", missing)
        monkeypatch.setattr(probes, "render_waveform_png", missing)

        settings.MEDIA_ROOT = str(tmp_path)
        upload = client.post(URL, {"file": recording()}, format="multipart")
        assert upload.status_code == status.HTTP_201_CREATED, upload.content
        ref = upload.data["audio"]["ref"]

        audio = Audio.objects.get()
        AudioProcessingService.extract_metadata(audio)
        audio.refresh_from_db()
        assert audio.duration is None
        assert audio.preview_b64 == ""
        assert audio.original.storage.exists(audio.original.name)

        response = client.post(DESCRIBE_URL, {"refs": [ref]}, format="json")
        snapshot = response.data["items"][ref]
        assert snapshot["duration_ms"] is None
        assert snapshot["preview_b64"] is None
        assert snapshot["meta_reason"] == probes.REASON_FFPROBE_MISSING
