"""The signed media link: one object, unforgeable, expiring — a bearer token.

Whoever holds an unexpired link can fetch it; these tests pin what the link
does NOT allow: another object, a forged or re-keyed signature, an expired
stamp.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from stapel_core.django.users.models import User

from stapel_cdn.models import Audio, File, Image
from stapel_cdn.protected import SIGNING_SALT, resolve_token, signed_url

pytestmark = pytest.mark.django_db

V1 = "/cdn/api/v1/"
SIGNED = "/media/signed/"
VOICE = b"OggS-a-private-voice-note"
DOC = b"%PDF-1.4 a private document"
PUBLIC = "avatar/" + "c3" * 32


@pytest.fixture
def media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL = "/media/"
    return tmp_path


def _user(name, **extra):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="pw-123456", **extra
    )


@pytest.fixture
def owner(db):
    return _user("owner")


@pytest.fixture
def stranger(db):
    return _user("stranger")


@pytest.fixture
def staff(db):
    return _user("operator", is_staff=True)


def _file(user, data=DOC, file_hash="b2" * 32):
    return File.objects.create(
        file_hash=file_hash,
        original_filename="contract.pdf",
        file_extension=".pdf",
        mime_type="application/pdf",
        original_size=len(data),
        uploaded_by=user,
        original=SimpleUploadedFile("contract.pdf", data, content_type="application/pdf"),
    )


def _audio(user):
    with patch("stapel_cdn.services.AudioProcessingService.extract_metadata"):
        return Audio.objects.create(
            file_hash="a1" * 32,
            original_filename="voice.ogg",
            file_extension=".ogg",
            original_size=len(VOICE),
            uploaded_by=user,
            original=SimpleUploadedFile("voice.ogg", VOICE, content_type="audio/ogg"),
        )


def _public_image(user):
    with patch("stapel_cdn.tasks.process_image_async.delay"):
        return Image.objects.create(
            file_hash="c3" * 32,
            original_filename="face.jpg",
            file_extension=".jpg",
            type="avatar",
            original_size=10,
            original_width=10,
            original_height=10,
            uploaded_by=user,
            original=SimpleUploadedFile("face.jpg", b"\xff\xd8\xff" + b"0" * 7),
        )


@pytest.fixture
def private(media, owner):
    """The owner's document and voice note, and a public avatar."""
    return {"file": _file(owner), "audio": _audio(owner), "image": _public_image(owner)}


def _client(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _describe(user, refs):
    response = _client(user).post(V1 + "describe/", {"refs": refs}, format="json")
    assert response.status_code == 200, response.content
    return response.json()


def _urls(snapshot) -> list[str]:
    urls = [v.get("url") for v in snapshot.get("variants") or []]
    urls += [v.get("clean_url") for v in snapshot.get("variants") or []]
    urls.append(snapshot.get("poster_url"))
    return [u for u in urls if u]


def _refs(private):
    return [f"file/{private['file'].file_hash}", f"audio/{private['audio'].file_hash}"]


def _body_of(response) -> bytes:
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content)
    return response.content


# ── the signed link itself ───────────────────────────────────────────────


class TestSignedTokens:
    def test_a_token_serves_the_object_it_names_and_nothing_else(self, private):
        doc = _client().get(signed_url(private["file"].original.name))
        voice = _client().get(signed_url(private["audio"].original.name))
        assert _body_of(doc) == DOC and _body_of(voice) == VOICE

    def test_a_tampered_token_is_refused(self, private):
        token = signed_url(private["file"].original.name).rstrip("/").split("/")[-1]
        payload, stamp, signature = token.rsplit(":", 2)
        other = signed_url(private["audio"].original.name).rstrip("/").split("/")[-1]
        other_payload = other.rsplit(":", 2)[0]
        for forged in (
            f"{other_payload}:{stamp}:{signature}",  # another object, old signature
            f"{payload}:{stamp}:{'A' * len(signature)}",
            f"{payload}:{stamp}:",
            payload,
        ):
            assert resolve_token(forged) is None
            assert _client().get(f"{V1}media/signed/{forged}/").status_code == 404

    def test_a_token_signed_with_another_key_or_salt_is_refused(self, private):
        name = private["file"].original.name
        other_key = signing.dumps({"p": name}, key="not-the-secret", salt=SIGNING_SALT)
        other_salt = signing.dumps({"p": name}, salt="another.purpose")
        assert resolve_token(other_key) is None
        assert resolve_token(other_salt) is None

    def test_a_token_expires(self, private, settings):
        settings.STAPEL_CDN = {"SIGNED_MEDIA_TTL_SECONDS": 60}
        link = signed_url(private["file"].original.name)
        assert _client().get(link).status_code == 200
        later = time.time() + 61
        with patch("django.core.signing.time.time", return_value=later):
            assert _client().get(link).status_code == 404

    def test_a_token_carries_no_readable_secret_and_differs_per_object(self, private):
        a = signed_url(private["file"].original.name)
        b = signed_url(private["audio"].original.name)
        assert a != b
        assert private["file"].original.name not in a
