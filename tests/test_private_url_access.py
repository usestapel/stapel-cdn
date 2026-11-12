"""Who receives a URL to a private or protected object — one decision.

A signed link to a private object (a voice note, a document) is the
capability. ``stapel_cdn.access`` decides who gets one, and every path of
this library that turns a stored object into a URL asks it. These tests hold
the decision at each door and walk every mounted route as a non-owner, so a
new route is covered the day it is added.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.checks import run_checks
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient
from stapel_core.django.users.models import User

from stapel_cdn import access
from stapel_cdn.checks import W017_PRIVATE_URL_AUDIENCE
from stapel_cdn.models import Audio, File, Image
from stapel_cdn.serializers import AudioSerializer, FileModelSerializer
from stapel_cdn.urls_v1 import urlpatterns

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


class TestDescribeOverHttp:
    def test_a_stranger_gets_metadata_and_no_url(self, private, stranger):
        body = _describe(stranger, _refs(private))
        for ref in _refs(private):
            snapshot = body["items"][ref]
            assert _urls(snapshot) == []
            assert snapshot["meta_reason"] == access.REASON_NOT_AUTHORIZED
            assert snapshot["meta_status"] != "ok"
            assert snapshot["bytes"] and snapshot["mime"]
        assert SIGNED not in str(body)

    def test_a_mixed_batch_still_describes_the_public_item(self, private, stranger):
        body = _describe(stranger, [*_refs(private), PUBLIC])
        assert set(body["items"]) == {*_refs(private), PUBLIC}
        assert _urls(body["items"][PUBLIC])
        assert body["items"][PUBLIC]["meta_reason"] != access.REASON_NOT_AUTHORIZED

    def test_the_owner_gets_signed_urls(self, private, owner):
        body = _describe(owner, _refs(private))
        for ref in _refs(private):
            [url] = _urls(body["items"][ref])
            assert SIGNED in url

    def test_another_holder_of_the_same_bytes_owns_the_ref(self, private, stranger):
        _file(stranger)  # identical bytes, the stranger's own row
        body = _describe(stranger, [f"file/{private['file'].file_hash}"])
        [url] = _urls(body["items"][f"file/{private['file'].file_hash}"])
        assert SIGNED in url

    @override_settings(SERVICE_API_KEY="svc-key", MIDDLEWARE=[
        "stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware"
    ])
    def test_a_service_caller_gets_signed_urls(self, private):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY="svc-key")
        body = client.post(V1 + "describe/", {"refs": _refs(private)}, format="json").json()
        for ref in _refs(private):
            assert SIGNED in _urls(body["items"][ref])[0]

    def test_staff_are_not_owners_by_default(self, private, staff):
        body = _describe(staff, _refs(private))
        assert all(_urls(body["items"][r]) == [] for r in _refs(private))

    def test_owner_and_staff_admits_staff_and_only_staff(self, private, staff, stranger, settings):
        settings.STAPEL_CDN = {"PRIVATE_URL_AUDIENCE": "owner_and_staff"}
        assert all(_urls(_describe(staff, _refs(private))["items"][r]) for r in _refs(private))
        assert all(
            _urls(_describe(stranger, _refs(private))["items"][r]) == []
            for r in _refs(private)
        )

    def test_any_is_the_explicit_opt_out(self, private, stranger, settings):
        settings.STAPEL_CDN = {"PRIVATE_URL_AUDIENCE": "any"}
        assert all(_urls(_describe(stranger, _refs(private))["items"][r]) for r in _refs(private))

    def test_an_unknown_audience_fails_closed(self, private, stranger, settings):
        settings.STAPEL_CDN = {"PRIVATE_URL_AUDIENCE": "everyone"}
        assert all(
            _urls(_describe(stranger, _refs(private))["items"][r]) == []
            for r in _refs(private)
        )

    def test_the_comm_function_answers_services(self, private):
        from stapel_core.comm import call

        body = call("cdn.describe_many", {"refs": _refs(private)})
        assert all(SIGNED in _urls(body["items"][r])[0] for r in _refs(private))


class TestSerializers:
    def test_no_viewer_means_no_private_url(self, private):
        for row, serializer in ((private["file"], FileModelSerializer),
                                (private["audio"], AudioSerializer)):
            data = serializer(row).data
            assert data["original_url"] is None
            assert _urls(data["render_meta"]) == []

    def test_the_owner_as_viewer_gets_the_link(self, private, owner):
        viewer = access.Viewer(user=owner)
        data = FileModelSerializer(private["file"], context={"cdn_viewer": viewer}).data
        assert SIGNED in data["original_url"]
        assert SIGNED in _urls(data["render_meta"])[0]

    def test_a_public_object_is_unchanged(self, private):
        from stapel_cdn.serializers import ImageSerializer

        assert ImageSerializer(private["image"]).data["original_url"].startswith("/media/")


class TestAdmin:
    def _render(self, user, row):
        from stapel_cdn.admin import _admin_request, original_link_html

        request = RequestFactory().get("/admin/")
        request.user = user
        token = _admin_request.set(request)
        try:
            return str(original_link_html(row))
        finally:
            _admin_request.reset(token)

    def test_staff_see_no_link_to_someone_elses_private_file(self, private, staff):
        html = self._render(staff, private["file"])
        assert SIGNED not in html and "withheld" in html

    def test_owner_and_staff_links_it(self, private, staff, settings):
        settings.STAPEL_CDN = {"PRIVATE_URL_AUDIENCE": "owner_and_staff"}
        assert SIGNED in self._render(staff, private["file"])


# ── the route walk ───────────────────────────────────────────────────────


def _concrete(pattern, victim) -> str:
    route = str(pattern.pattern)
    for token, value in (
        ("<str:image_type>", victim["type"]),
        ("<str:file_hash>", victim["hash"]),
        ("<str:token>", "not-a-token"),
    ):
        route = route.replace(token, value)
    assert "<" not in route, f"unmapped converter in {route}; extend _concrete"
    return V1 + route


def _victims(private):
    return [
        {"ref": f"file/{private['file'].file_hash}", "type": "avatar",
         "hash": private["file"].file_hash, "bytes": DOC},
        {"ref": f"audio/{private['audio'].file_hash}", "type": "avatar",
         "hash": private["audio"].file_hash, "bytes": VOICE},
    ]


def _body_of(response) -> bytes:
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content)
    return response.content


def _walk(user, private):
    """Every (method, route, body) the user's walk produced."""
    client = _client(user)
    seen = []
    for victim in _victims(private):
        payload = {"refs": [victim["ref"]], "file_hash": victim["hash"]}
        for pattern in urlpatterns:
            route = _concrete(pattern, victim)
            for method in ("get", "post", "put", "patch", "delete"):
                fmt = {} if method == "get" else {"format": "json"}
                response = getattr(client, method)(route, payload, **fmt)
                seen.append((method, route, victim, _body_of(response)))
    return seen


def _leaks(seen, private):
    out = []
    for method, route, victim, body in seen:
        if SIGNED.encode() in body or b"/media/private/" in body or victim["bytes"] in body:
            out.append((method, route))
    return out


def test_the_walk_sees_every_door(private):
    names = {p.name for p in urlpatterns}
    assert len(urlpatterns) >= 12
    assert {"describe-media", "file-exists", "cdn-signed-media",
            "cdn-image-original", "random-image"} <= names
    routes = {_concrete(p, _victims(private)[0]) for p in urlpatterns}
    assert len(routes) == len(urlpatterns)


@pytest.mark.parametrize("who", ["stranger", "staff"])
def test_no_route_hands_a_non_owner_a_private_url(private, who, request):
    user = request.getfixturevalue(who)
    assert _leaks(_walk(user, private), private) == []


def test_the_same_walk_does_see_a_url_for_the_owner(private, owner):
    """Inversion: the walk above can see a signed URL when one is handed out."""
    leaks = _leaks(_walk(owner, private), private)
    routes = {route for _, route in leaks}
    assert any(r.endswith("/describe/") for r in routes)
    assert any(r.endswith("/file/exists/") for r in routes)


def test_the_walk_sees_a_url_handed_to_a_stranger_under_any(private, stranger, settings):
    """Inversion: with the decision switched off, the stranger's walk fires."""
    settings.STAPEL_CDN = {"PRIVATE_URL_AUDIENCE": "any"}
    routes = {route for _, route in _leaks(_walk(stranger, private), private)}
    assert any(r.endswith("/describe/") for r in routes)


# ── boot ─────────────────────────────────────────────────────────────────


class TestChecks:
    def _ids(self):
        return {m.id for m in run_checks(tags=["stapel_cdn"])}

    def test_silent_by_default(self, settings):
        settings.STAPEL_CDN = {}
        assert W017_PRIVATE_URL_AUDIENCE not in self._ids()

    @pytest.mark.parametrize("value", ["any", "everyone"])
    def test_any_and_unknown_warn(self, settings, value):
        settings.STAPEL_CDN = {"PRIVATE_URL_AUDIENCE": value}
        assert W017_PRIVATE_URL_AUDIENCE in self._ids()
