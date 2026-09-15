"""A client-chosen name never 500s an upload, and never quietly becomes another name.

Everything about an upload except its bytes is chosen by whoever sends it —
the filename, its extension, the ``Content-Type`` header — and every one of
them lands in a bounded column. Django validates ``max_length`` in forms,
never on a write, so a bound declared on a column is not a bound enforced on
the path that writes it.

Found by ``stapel-bounds-lint`` (BND002) rather than by a traceback, which is
what that gate is for. There were two distinct failures underneath one
finding, and the difference was measured, not assumed:

* **A 500.** ``original_filename`` (255), ``file_extension`` (10) and
  ``mime_type`` (100) are assigned straight from the request in the view and
  never go near storage, so an over-long value reaches Postgres as
  ``StringDataRightTruncation``, the transaction rolls back, and the endpoint
  answers 500. ``file_extension`` is the cheapest: an extension is whatever
  follows the last dot of a client-chosen string, so ``clip.`` + eleven
  characters is enough. The stored PATH of ``Image``/``File``/``Audio`` is a
  500 for a second reason — ``OverwriteStorage`` overrode
  ``get_available_name`` to return the name unchanged, which dropped the trim
  Django performs against the column.
* **A silent rename.** ``Video.original`` was ``max_length=100`` against a
  71-character prefix and is the one field with no custom storage, so
  Django's own ``get_available_name`` trimmed and randomised instead:
  ``holiday-video-from-summer-2026.mp4`` was stored as
  ``holiday-video-fro_bMgrNZQ.mp4``. No error, no log, and the name a person
  uploaded is not the name the system holds.

The assertions below are written against the FIELD's declared ``max_length``
rather than against 255 / 100 / 10. A test that restates the number passes on
the day the column shrinks, which is the failure the repair exists to prevent.
"""
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient
from PIL import Image as PILImage
from django.contrib.auth import get_user_model

from stapel_cdn import bounds
from stapel_cdn.models import Audio, File, Image, Video

User = get_user_model()

IMAGE_URL = "/cdn/api/v1/upload/image/"
VIDEO_URL = "/cdn/api/v1/upload/video/"
AUDIO_URL = "/cdn/api/v1/upload/audio/"

def _png_bytes(width=8, height=8):
    """A real PNG — pyvips decodes the upload, so a hand-rolled header is a
    400 (`invalid_format`) and would test the wrong refusal."""
    buffer = BytesIO()
    PILImage.new("RGB", (width, height), color=(40, 90, 160)).save(buffer, format="PNG")
    return buffer.getvalue()
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 512
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 512


@pytest.fixture
def client(db):
    api = APIClient()
    api.force_authenticate(
        user=User.objects.create_user(
            username="name-uploader",
            email="names@example.com",
            password="pw-not-for-production",
        )
    )
    return api


def _limit(model, field):
    return bounds.max_length_of(model, field)


# ---------------------------------------------------------------------------
# the arithmetic, on its own
# ---------------------------------------------------------------------------


class TestBoundsReadsTheLimitOffTheField:
    def test_fit_marks_the_cut(self):
        long_value = "x" * (_limit(Image, "original_filename") + 50)
        fitted = bounds.fit(Image, "original_filename", long_value)
        assert len(fitted) == _limit(Image, "original_filename")
        assert fitted.endswith(bounds.ELLIPSIS), "a silent cut is a lie about the data"

    def test_fit_leaves_a_value_that_already_fits_alone(self):
        assert bounds.fit(Image, "original_filename", "holiday.png") == "holiday.png"

    def test_none_becomes_empty_not_the_word_none(self):
        assert bounds.fit(Image, "original_filename", None) == ""

    def test_fit_filename_keeps_the_extension(self):
        """The tail is the part that carries meaning for a filename: it is what
        a browser dispatches on and what a person recognises in a list."""
        limit = _limit(Image, "original_filename")
        name = "a" * (limit + 40) + ".png"
        fitted = bounds.fit_filename(Image, "original_filename", name)
        assert len(fitted) <= limit
        assert fitted.endswith(".png")
        assert bounds.ELLIPSIS in fitted

    def test_an_absurd_extension_does_not_push_the_stem_out(self):
        """`x.` + 300 characters is a legal filename. Keeping that 'extension'
        would store a name with no stem at all."""
        limit = _limit(Image, "original_filename")
        name = "photo." + "e" * (limit + 10)
        fitted = bounds.fit_filename(Image, "original_filename", name)
        assert len(fitted) <= limit
        assert fitted.startswith("photo.")

    def test_fit_stored_name_never_cuts_the_prefix(self):
        """A truncated hash directory would put two different files in one
        place, which is worse than the overflow it was trying to avoid."""
        prefix = "video/" + "a" * 64 + "/"
        stored = bounds.fit_stored_name(prefix, "b" * 400 + ".mp4", Video, "original")
        assert stored.startswith(prefix)
        assert len(stored) <= _limit(Video, "original")
        assert stored.endswith(".mp4")

    def test_a_prefix_that_cannot_fit_is_an_error_here_not_a_500_later(self):
        with pytest.raises(ValueError) as caught:
            bounds.fit_stored_name("x" * 600 + "/", "a.mp4", Video, "original")
        assert "widen the column" in str(caught.value)


# ---------------------------------------------------------------------------
# the doors
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestALongNameIsStoredNotRefused:
    def test_an_image_with_a_300_character_name_uploads(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        limit = _limit(Image, "original_filename")
        name = "p" * (limit + 45) + ".png"

        response = client.post(
            IMAGE_URL,
            {"file": SimpleUploadedFile(name, _png_bytes(), content_type="image/png")},
            format="multipart",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        image = Image.objects.get()
        assert len(image.original_filename) <= limit
        assert image.original_filename.endswith(".png")
        assert len(image.original.name) <= _limit(Image, "original")

    def test_the_video_name_that_used_to_overflow_a_100_column(
        self, client, settings, tmp_path
    ):
        """`video/<64-char hash>/` is 71 characters. This name is 29 — the
        length at which the old column ran out, on an unremarkable filename."""
        settings.MEDIA_ROOT = str(tmp_path)
        name = "holiday-video-from-summer.mp4"
        assert len(name) == 29

        response = client.post(
            VIDEO_URL,
            {"file": SimpleUploadedFile(name, MP4, content_type="video/mp4")},
            format="multipart",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        video = Video.objects.get()
        assert len(video.original.name) <= _limit(Video, "original")
        # …and it is stored WHOLE now, not trimmed to fit a mis-declared column
        assert video.original.name.endswith(name)

    def test_an_absurd_extension_fits_the_ten_character_column(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        name = "clip." + "m" * 60

        response = client.post(
            AUDIO_URL,
            {"file": SimpleUploadedFile(name, WEBM, content_type="audio/webm")},
            format="multipart",
        )

        # Either the extension allowlist refuses it (a 400, which is a refusal
        # the client can read) or it is stored — what must NOT happen is a 500.
        assert response.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR
        if response.status_code == status.HTTP_201_CREATED:
            audio = Audio.objects.get()
            assert len(audio.file_extension) <= _limit(Audio, "file_extension")

    def test_a_long_content_type_header_fits_its_column(
        self, client, settings, tmp_path
    ):
        """`Content-Type` is a client header, so its length is the client's."""
        settings.MEDIA_ROOT = str(tmp_path)
        response = client.post(
            AUDIO_URL,
            {
                "file": SimpleUploadedFile(
                    "voice.webm", WEBM, content_type="audio/webm; codecs=" + "o" * 200
                )
            },
            format="multipart",
        )

        assert response.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR
        if response.status_code == status.HTTP_201_CREATED:
            audio = Audio.objects.get()
            assert len(audio.mime_type) <= _limit(Audio, "mime_type")


@pytest.mark.django_db
class TestEveryStoredModelIsCovered:
    """The repair is per-boundary, so the test is per-model: a model whose
    intake was missed is the next 500."""

    @pytest.mark.parametrize("model", [Image, Video, File, Audio])
    def test_the_stored_path_column_can_hold_its_own_prefix(self, model):
        prefix = "x" * 8 + "/" + "a" * 64 + "/"
        stored = bounds.fit_stored_name(prefix, "n" * 400 + ".bin", model, "original")
        assert len(stored) <= _limit(model, "original")

    @pytest.mark.parametrize("model", [Image, Video, File, Audio])
    def test_a_long_name_fits_the_display_column(self, model):
        fitted = bounds.fit_filename(
            model, "original_filename", "z" * 900 + ".bin"
        )
        assert len(fitted) <= _limit(model, "original_filename")
