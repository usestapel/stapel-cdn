"""``cdn.ownership`` — who holds each ref, and how it is stored.

The write-side check a consumer (stapel-chat) runs before it accepts a ref
from a sender: one batched call, identifiers compared as strings, identical
bytes held by two principals owned by both.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from stapel_core.django.users.models import User

from stapel_cdn.models import Audio, File, Image

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


# ── cdn.ownership ────────────────────────────────────────────────────────


class TestOwnershipFunction:
    def _ask(self, refs, owner_id=None):
        from stapel_core.comm import call

        payload = {"refs": refs}
        if owner_id is not None:
            payload["owner_id"] = str(owner_id)
        return {item["ref"]: item for item in call("cdn.ownership", payload)["items"]}

    def test_the_owner_owns_and_a_stranger_does_not(self, private, owner, stranger):
        ref = f"file/{private['file'].file_hash}"
        mine = self._ask([ref], owner.pk)[ref]
        assert mine == {"ref": ref, "exists": True, "owner_id": str(owner.pk),
                        "owned": True, "visibility": "private"}
        assert self._ask([ref], str(stranger.pk))[ref]["owned"] is False

    def test_identifiers_compare_as_strings(self, private, owner):
        ref = f"audio/{private['audio'].file_hash}"
        assert self._ask([ref], str(owner.pk))[ref]["owned"] is True
        assert self._ask([ref], str(owner.pk))[ref]["owner_id"] == str(owner.pk)

    def test_unknown_public_and_unasked(self, private, owner):
        ghost = "file/" + "f" * 64
        items = self._ask([ghost, PUBLIC, "nonsense"], owner.pk)
        assert items[ghost] == {"ref": ghost, "exists": False, "owner_id": None,
                                "owned": False, "visibility": None}
        assert items[PUBLIC]["visibility"] == "public" and items[PUBLIC]["owned"] is True
        assert items["nonsense"]["exists"] is False
        assert self._ask([ghost])[ghost]["owned"] is None

    def test_a_second_holder_of_the_same_bytes_owns_it_too(self, private, owner, stranger):
        _file(stranger)
        ref = f"file/{private['file'].file_hash}"
        assert self._ask([ref], stranger.pk)[ref]["owned"] is True
        assert self._ask([ref], stranger.pk)[ref]["owner_id"] == str(owner.pk)

    def test_order_is_kept_and_duplicates_collapse(self, private, owner):
        from stapel_core.comm import call

        refs = [PUBLIC, f"file/{private['file'].file_hash}", PUBLIC]
        items = call("cdn.ownership", {"refs": refs, "owner_id": str(owner.pk)})["items"]
        assert [i["ref"] for i in items] == refs[:2]

    def test_bounded(self):
        from stapel_cdn.functions import OWNERSHIP_LIMIT, ownership

        with pytest.raises(ValueError):
            ownership({"refs": [f"file/{i:064x}" for i in range(OWNERSHIP_LIMIT + 1)]})

    def test_one_query_per_model(self, private, owner, django_assert_max_num_queries):
        refs = [*_refs(private), PUBLIC]
        with django_assert_max_num_queries(6):
            self._ask(refs, owner.pk)
