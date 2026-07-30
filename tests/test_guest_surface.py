"""The guest (anonymous session) surface of stapel-cdn.

With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a
bare ``IsAuthenticated`` gate lets it through — and for a *storage* module
that is the sharpest version of the question, because the anonymous axis
removes the only thing that made an upload endpoint self-limiting. A session
costs one unauthenticated POST to mint, so "authenticated upload" and "open
file hosting" become the same sentence.

``views.py`` now states the answer per view; this module keeps it true:

    a guest may upload its own avatar, and nothing else.

The avatar half is the one that would hurt in production if it regressed —
it is a live surface in a real consumer (meettoday's settings screen is
reachable from the header a guest sees, and its profile tab uploads here).
"""

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image as PILImage
from rest_framework import status
from rest_framework.test import APIClient
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    IsNotAnonymousUser,
)
from stapel_core.django.users.models import User

from stapel_cdn import views
from stapel_cdn.models import File, Image, Video


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints:
    authenticated, ``is_anonymous=True``."""
    return User.create_anonymous_user()


@pytest.fixture
def guest_client(api_client, guest):
    api_client.force_authenticate(user=guest)
    return api_client


@pytest.fixture
def registered_client(api_client, db):
    api_client.force_authenticate(
        user=User.objects.create_user(
            username="uploader", email="uploader@example.com", password="testpass123"
        )
    )
    return api_client


def make_image_upload(name="photo.jpg"):
    img = PILImage.new("RGB", (100, 100), color="red")
    buffer = BytesIO()
    img.save(buffer, format="JPEG")
    return SimpleUploadedFile(
        name=name, content=buffer.getvalue(), content_type="image/jpeg"
    )


# ---------------------------------------------------------------------------
# The one upload a guest owns
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestMayUploadItsOwnAvatar:
    url = "/cdn/api/v1/upload/avatar/"

    def test_guest_avatar_upload_succeeds(self, guest_client, guest):
        resp = guest_client.post(
            self.url, {"file": make_image_upload()}, format="multipart"
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.content
        image = Image.objects.get(type="avatar")
        assert image.uploaded_by_id == guest.id

    def test_declared_allowed_in_the_source(self):
        assert views.AvatarUploadView.stapel_anonymous_access == ANONYMOUS_ALLOWED

    def test_dedup_still_applies_to_a_guest(self, guest_client):
        """Re-uploading the same bytes costs no new storage — which is what
        keeps "a session is free to mint" from meaning "the disk is free"."""
        upload = make_image_upload()
        assert (
            guest_client.post(self.url, {"file": upload}, format="multipart").status_code
            == status.HTTP_201_CREATED
        )
        again = guest_client.post(
            self.url, {"file": make_image_upload()}, format="multipart"
        )
        assert again.status_code == status.HTTP_200_OK, again.content
        assert Image.objects.filter(type="avatar").count() == 1


# ---------------------------------------------------------------------------
# The general-purpose intake is closed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestMayNotUploadAnythingElse:
    def test_generic_file(self, guest_client):
        resp = guest_client.post(
            "/cdn/api/v1/upload/file/",
            {
                "file": SimpleUploadedFile(
                    "notes.pdf", b"%PDF-1.4 hello", content_type="application/pdf"
                )
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.content
        assert File.objects.count() == 0

    def test_image(self, guest_client):
        resp = guest_client.post(
            "/cdn/api/v1/upload/image/",
            {"file": make_image_upload()},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.content
        assert Image.objects.count() == 0

    def test_video(self, guest_client):
        resp = guest_client.post(
            "/cdn/api/v1/upload/video/",
            {
                "file": SimpleUploadedFile(
                    "clip.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"
                )
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.content
        assert Video.objects.count() == 0

    def test_typed_image(self, guest_client):
        """Including `avatar` as the type: the bounded avatar route is the one
        a guest gets, not this general-purpose one wearing its label."""
        resp = guest_client.post(
            "/cdn/api/v1/images/avatar/upload/",
            {"file": make_image_upload()},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.content
        assert Image.objects.count() == 0


@pytest.mark.django_db
class TestRegisteredUsersAreUnaffected:
    """The gate is about *anonymous*, not about *authenticated*."""

    def test_image(self, registered_client):
        resp = registered_client.post(
            "/cdn/api/v1/upload/image/",
            {"file": make_image_upload()},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.content

    def test_generic_file(self, registered_client):
        resp = registered_client.post(
            "/cdn/api/v1/upload/file/",
            {
                "file": SimpleUploadedFile(
                    "notes.pdf", b"%PDF-1.4 hello", content_type="application/pdf"
                )
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.content


def test_closed_views_carry_the_permission_class():
    for view in (
        views.ImageUploadView,
        views.VideoUploadView,
        views.TypedImageUploadView,
        views.GenericFileUploadView,
    ):
        assert IsNotAnonymousUser in view.permission_classes, view.__name__


def test_no_view_is_left_silent():
    """The question ``stapel_core.adoption`` E001/W002 asks a consumer's
    deployment, asked here — where it can be answered."""
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.views import APIView
    from stapel_core.django.api.permissions import ANONYMOUS_DECLARATIONS

    silent = [
        name
        for name, obj in vars(views).items()
        if isinstance(obj, type)
        and issubclass(obj, APIView)
        and set(getattr(obj, "permission_classes", ()) or ()) == {IsAuthenticated}
        and getattr(obj, "stapel_anonymous_access", None) not in ANONYMOUS_DECLARATIONS
    ]
    assert silent == []
