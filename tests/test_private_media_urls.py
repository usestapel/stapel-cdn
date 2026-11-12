"""Private media (documents, archives, voice notes) never gets a public URL.

Every ``<field>.url`` of a stored object goes through
``storage.OverwriteStorage.url`` -> ``protected.stored_url``. With the default
``PRIVATE_MEDIA_URLS = "signed"`` a private object's URL is a short-lived
signed link served by the app, so the operator can deny the private prefix on
the public media route without breaking playback. ``cdn_probe_private_media``
measures that denial over real HTTP.
"""
import http.server
import os
import threading
from functools import partial
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.checks import run_checks
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework.test import APIClient
from stapel_core.django.users.models import User

from stapel_cdn.checks import W016_PRIVATE_MEDIA_PUBLIC
from stapel_cdn.metadata import build_render_metadata
from stapel_cdn.models import Audio, File
from stapel_cdn.protected import signed_url
from stapel_cdn.serializers import AudioSerializer, FileModelSerializer

pytestmark = pytest.mark.django_db

VOICE = b"OggS-not-really-a-voice-note"
DOC = b"%PDF-1.4 private document"


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL = "/media/"
    return tmp_path


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="private", email="private@example.com", password="pw-123456"
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


def _file(user):
    return File.objects.create(
        file_hash="b2" * 32,
        original_filename="contract.pdf",
        file_extension=".pdf",
        mime_type="application/pdf",
        original_size=len(DOC),
        uploaded_by=user,
        original=SimpleUploadedFile("contract.pdf", DOC, content_type="application/pdf"),
    )


def _urls_handed_out(row):
    """Every URL this library gives the row's owner for ``row``."""
    from stapel_cdn.access import Viewer

    serializer = AudioSerializer if isinstance(row, Audio) else FileModelSerializer
    data = serializer(row, context={"cdn_viewer": Viewer(user=row.uploaded_by)}).data
    urls = [row.original.url, data["original_url"]]
    snapshot = build_render_metadata(row)
    urls += [v["url"] for v in snapshot.get("variants") or [] if v.get("url")]
    return urls


def _body(response):
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content)
    return response.content


class TestSignedByDefault:
    @pytest.mark.parametrize("factory, payload", [(_audio, VOICE), (_file, DOC)])
    def test_no_public_url_for_a_private_object(self, media_root, user, factory, payload):
        row = factory(user)
        assert row.original.name.startswith("private/")
        urls = _urls_handed_out(row)
        assert urls, "nothing was handed out — the test saw no subject"
        for url in urls:
            assert not url.startswith("/media/"), url
            assert "/media/signed/" in url
            response = APIClient().get(url)
            assert response.status_code == 200
            assert _body(response) == payload
            assert response["Cache-Control"] == "private, no-store"

    @pytest.mark.parametrize("value", ["signed", "", None, "Public-ish", "PUBLIC "])
    def test_anything_but_public_fails_closed(self, media_root, user, value):
        row = _audio(user)
        with override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"), "PRIVATE_MEDIA_URLS": value}):
            if value == "PUBLIC ":
                # Normalised: an explicit opt-out with stray case/space is still one.
                assert row.original.url.startswith("/media/private/")
            else:
                assert "/media/signed/" in row.original.url

    def test_a_token_still_cannot_name_a_public_or_outside_path(self, media_root, user):
        row = _audio(user)
        assert APIClient().get(signed_url("avatar/" + "c" * 64 + "/120.webp")).status_code == 404
        assert APIClient().get(signed_url("private/../../etc/passwd")).status_code == 404
        assert APIClient().get(signed_url(row.original.name)).status_code == 200


class TestPublicOptOut:
    def test_public_mode_hands_out_the_media_path_and_warns(self, media_root, user):
        row = _audio(user)
        conf = {"ASSET_TYPES": ("avatar", "product"), "PRIVATE_MEDIA_URLS": "public"}
        with override_settings(STAPEL_CDN=conf):
            assert row.original.url == f"/media/{row.original.name}"
            ids = {m.id for m in run_checks() if m.id == W016_PRIVATE_MEDIA_PUBLIC}
            assert ids == {W016_PRIVATE_MEDIA_PUBLIC}

    def test_default_raises_no_warning(self, media_root):
        assert not [m for m in run_checks() if m.id == W016_PRIVATE_MEDIA_PUBLIC]


# ── the probe: a real HTTP server in front of MEDIA_ROOT ────────────────


class _Handler(http.server.SimpleHTTPRequestHandler):
    deny = ()

    def log_message(self, *args):
        pass

    def translate_path(self, path):
        rel = path.split("?", 1)[0]
        assert rel.startswith("/media/")
        return os.path.join(self.directory, rel[len("/media/"):])

    def do_GET(self):
        rel = self.path[len("/media/"):]
        if any(rel.startswith(prefix) for prefix in self.deny):
            self.send_error(403)
            return
        super().do_GET()


@pytest.fixture
def media_server(media_root):
    servers = []

    def start(deny):
        handler = type("H", (_Handler,), {"deny": deny})
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(handler, directory=str(media_root))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


class TestProbe:
    def test_a_server_that_serves_everything_is_reported_exposed(self, media_server, media_root):
        base = media_server(deny=())
        out = StringIO()
        with pytest.raises(CommandError):
            call_command("cdn_probe_private_media", "--base-url", base, stdout=out, stderr=StringIO())
        text = out.getvalue()
        assert "private/: EXPOSED" in text
        assert "protected/: EXPOSED" in text
        assert "checked 2 prefix(es)" in text
        # The canaries are cleaned up.
        assert not any(".stapel-probe-" in name for _r, _d, files in os.walk(media_root) for name in files)

    def test_a_server_that_denies_both_trees_passes(self, media_server):
        base = media_server(deny=("private/", "protected/"))
        out = StringIO()
        call_command("cdn_probe_private_media", "--base-url", base, stdout=out)
        assert "private/: denied (403)" in out.getvalue()
        assert "protected/: denied (403)" in out.getvalue()

    def test_denying_only_protected_is_not_enough(self, media_server):
        base = media_server(deny=("protected/",))
        out = StringIO()
        with pytest.raises(CommandError):
            call_command("cdn_probe_private_media", "--base-url", base, stdout=out, stderr=StringIO())
        assert "private/: EXPOSED" in out.getvalue()

    def test_an_unreachable_route_is_not_a_pass(self, media_root):
        out = StringIO()
        with pytest.raises(CommandError):
            call_command(
                "cdn_probe_private_media", "--base-url", "http://127.0.0.1:9",
                "--timeout", "2", stdout=out, stderr=StringIO(),
            )
        assert "UNMEASURED" in out.getvalue()
