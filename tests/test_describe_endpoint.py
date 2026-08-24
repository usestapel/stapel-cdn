"""``POST /cdn/api/v1/describe/`` — the browser's half of cdn.describe_many.

The gap this endpoint closes: ``cdn.describe`` / ``cdn.describe_many`` are
comm Functions, and a browser cannot reach the comm bus. A client could only
ever see ``render_meta`` for something it had just uploaded itself, so a chat
bubble holding somebody else's ``<prefix>/<hash>`` had nothing to draw. The
tests that matter here are therefore:

- a caller gets snapshots for refs it did NOT upload (the whole point);
- unresolvable refs — deleted, never stored, malformed — come back as DATA in
  ``missing``, and the call still succeeds;
- the ceiling refuses the 51st ref, AFTER duplicates collapse, with the
  numbers in the params;
- HTTP and comm return the SAME snapshot, because they share one body;
- the guard is the settings seam, and the throttle answers in this module's
  own error envelope.
"""
import pytest
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APIClient
from stapel_core.comm import call
from stapel_core.django.users.models import User

from stapel_cdn.metadata import DESCRIBE_MANY_LIMIT
from stapel_cdn.models import File, Image, Video

URL = "/cdn/api/v1/describe/"

IMAGE_HASH = "a1" * 32
VIDEO_HASH = "b2" * 32
FILE_HASH = "c3" * 32
GONE_HASH = "d4" * 32


@pytest.fixture
def uploader(db):
    return User.objects.create_user(
        username="uploader", email="uploader@example.com", password="testpass123"
    )


@pytest.fixture
def reader(db):
    """Someone who never uploaded anything — a chat participant."""
    return User.objects.create_user(
        username="reader", email="reader@example.com", password="testpass123"
    )


@pytest.fixture
def reader_client(reader):
    client = APIClient()
    client.force_authenticate(user=reader)
    return client


@pytest.fixture
def media(uploader):
    """One image, one video and one document, ALL owned by someone else."""
    image = Image.objects.create(
        file_hash=IMAGE_HASH,
        original_filename="pic.jpg",
        file_extension=".jpg",
        type="avatar",
        original_width=1600,
        original_height=900,
        original_size=51234,
        uploaded_by=uploader,
    )
    video = Video.objects.create(
        file_hash=VIDEO_HASH,
        original_filename="clip.mp4",
        file_extension=".mp4",
        original_size=2000,
        uploaded_by=uploader,
    )
    document = File.objects.create(
        file_hash=FILE_HASH,
        original_filename="doc.pdf",
        file_extension=".pdf",
        mime_type="application/pdf",
        original_size=3000,
        uploaded_by=uploader,
    )
    return {"image": image, "video": video, "file": document}


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    """Throttle history lives in the cache; a leaked bucket fails the next test."""
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestDescribeReachesTheBrowser:
    def test_snapshots_for_refs_the_caller_never_uploaded(self, reader_client, media):
        """The gap D-2 names: a ref from somebody else's upload is describable."""
        response = reader_client.post(
            URL,
            {"refs": [f"avatar/{IMAGE_HASH}", f"video/{VIDEO_HASH}", f"file/{FILE_HASH}"]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        items = response.data["items"]
        assert set(items) == {
            f"avatar/{IMAGE_HASH}",
            f"video/{VIDEO_HASH}",
            f"file/{FILE_HASH}",
        }
        assert response.data["missing"] == []

    def test_snapshot_carries_the_render_contract(self, reader_client, media):
        response = reader_client.post(
            URL, {"refs": [f"avatar/{IMAGE_HASH}"]}, format="json"
        )
        snapshot = response.data["items"][f"avatar/{IMAGE_HASH}"]

        assert set(snapshot) >= {
            "ref", "kind", "mime", "ext", "bytes", "width", "height", "aspect",
            "square", "animated", "duration_ms", "preview_b64", "preview_kind",
            "poster_url", "meta_status", "meta_reason", "variants",
        }
        assert snapshot["ref"] == f"avatar/{IMAGE_HASH}"
        assert snapshot["width"] == 1600
        assert snapshot["height"] == 900
        assert snapshot["aspect"] == pytest.approx(1.777778)

    def test_snapshot_names_nobody(self, reader_client, media):
        """No uploader, filename or refs[] — that is why the seam can be this wide."""
        response = reader_client.post(
            URL, {"refs": [f"avatar/{IMAGE_HASH}"]}, format="json"
        )
        snapshot = response.data["items"][f"avatar/{IMAGE_HASH}"]

        assert not {"uploaded_by", "original_filename", "refs"} & set(snapshot)

    def test_http_and_comm_return_the_same_object(self, reader_client, media):
        """One body, two transports — the drift this endpoint must not introduce."""
        refs = [f"avatar/{IMAGE_HASH}", f"video/{VIDEO_HASH}"]

        over_http = reader_client.post(URL, {"refs": refs}, format="json").data
        over_comm = call("cdn.describe_many", {"refs": refs})

        assert dict(over_http["items"]) == over_comm["items"]
        assert list(over_http["missing"]) == over_comm["missing"]


@pytest.mark.django_db
class TestMissingIsData:
    def test_unknown_ref_is_reported_not_raised(self, reader_client, media):
        gone = f"avatar/{GONE_HASH}"

        response = reader_client.post(
            URL, {"refs": [f"avatar/{IMAGE_HASH}", gone]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert list(response.data["missing"]) == [gone]
        assert f"avatar/{IMAGE_HASH}" in response.data["items"]

    def test_malformed_ref_lands_in_missing_too(self, reader_client, media):
        """No <prefix>/<hash> shape is not a 400 — it is one unresolvable ref."""
        response = reader_client.post(
            URL,
            {"refs": ["not-a-ref", "", f"avatar/{IMAGE_HASH}"]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert set(response.data["missing"]) == {"not-a-ref", ""}
        assert list(response.data["items"]) == [f"avatar/{IMAGE_HASH}"]

    def test_all_missing_still_succeeds(self, reader_client, media):
        response = reader_client.post(
            URL, {"refs": [f"avatar/{GONE_HASH}"]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["items"] == {}
        assert list(response.data["missing"]) == [f"avatar/{GONE_HASH}"]

    def test_empty_batch_is_an_empty_answer(self, reader_client, media):
        response = reader_client.post(URL, {"refs": []}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["items"] == {}
        assert list(response.data["missing"]) == []

    def test_refs_is_required(self, reader_client, media):
        response = reader_client.post(URL, {}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestBatchCeiling:
    def test_the_limit_is_fifty(self):
        assert DESCRIBE_MANY_LIMIT == 50

    def test_at_the_ceiling_is_accepted(self, reader_client, media):
        refs = [f"avatar/{i:064d}" for i in range(DESCRIBE_MANY_LIMIT)]

        response = reader_client.post(URL, {"refs": refs}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["missing"]) == DESCRIBE_MANY_LIMIT

    def test_one_over_the_ceiling_is_refused_with_the_numbers(self, reader_client, media):
        refs = [f"avatar/{i:064d}" for i in range(DESCRIBE_MANY_LIMIT + 1)]

        response = reader_client.post(URL, {"refs": refs}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["localizable_error"] == "error.400.too_many_refs"
        assert response.data["params"]["count"] == DESCRIBE_MANY_LIMIT + 1
        assert response.data["params"]["max"] == DESCRIBE_MANY_LIMIT

    def test_duplicates_collapse_before_the_ceiling(self, reader_client, media):
        """51 mentions of one attachment cost one slot, not fifty-one."""
        refs = [f"avatar/{IMAGE_HASH}"] * (DESCRIBE_MANY_LIMIT + 1)

        response = reader_client.post(URL, {"refs": refs}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert list(response.data["items"]) == [f"avatar/{IMAGE_HASH}"]

    def test_the_ceiling_is_the_comm_functions_ceiling(self, reader_client, media):
        """Same rule, one implementation: services.describe_refs."""
        refs = [f"avatar/{i:064d}" for i in range(DESCRIBE_MANY_LIMIT + 1)]

        assert (
            reader_client.post(URL, {"refs": refs}, format="json").status_code
            == status.HTTP_400_BAD_REQUEST
        )
        with pytest.raises(Exception):
            call("cdn.describe_many", {"refs": refs})


@pytest.mark.django_db
class TestGuard:
    def test_anonymous_is_refused_by_default(self, media):
        response = APIClient().post(
            URL, {"refs": [f"avatar/{IMAGE_HASH}"]}, format="json"
        )

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_settings_can_open_the_guard(self, settings, media):
        settings.STAPEL_CDN = {
            **settings.STAPEL_CDN,
            "DESCRIBE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
        }

        response = APIClient().post(
            URL, {"refs": [f"avatar/{IMAGE_HASH}"]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK

    def test_settings_can_tighten_the_guard_to_service_only(
        self, settings, reader_client, media
    ):
        settings.STAPEL_CDN = {
            **settings.STAPEL_CDN,
            "DESCRIBE_PERMISSIONS": [
                "stapel_core.django.api.permissions.IsServiceRequest"
            ],
        }

        response = reader_client.post(
            URL, {"refs": [f"avatar/{IMAGE_HASH}"]}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestThrottle:
    def test_over_the_rate_is_refused_in_the_module_envelope(
        self, settings, reader_client, media
    ):
        settings.STAPEL_CDN = {**settings.STAPEL_CDN, "DESCRIBE_THROTTLE": "1/min"}
        body = {"refs": [f"avatar/{IMAGE_HASH}"]}

        assert reader_client.post(URL, body, format="json").status_code == 200
        refused = reader_client.post(URL, body, format="json")

        assert refused.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert refused.data["localizable_error"] == "error.429.too_many_requests"
        assert refused["Retry-After"]
        assert refused.data["params"]["retry_after"] >= 1

    def test_anonymous_callers_get_their_own_rate(self, settings, media):
        """Dormant under the default guard; the only brake once it is opened."""
        settings.STAPEL_CDN = {
            **settings.STAPEL_CDN,
            "DESCRIBE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
            "DESCRIBE_THROTTLE": "1000/min",
            "DESCRIBE_ANON_THROTTLE": "1/min",
        }
        client = APIClient()
        body = {"refs": [f"avatar/{IMAGE_HASH}"]}

        assert client.post(URL, body, format="json").status_code == 200
        assert (
            client.post(URL, body, format="json").status_code
            == status.HTTP_429_TOO_MANY_REQUESTS
        )


class TestSeamBootChecks:
    """The guard and the rate are read PER REQUEST — so they are checked at boot.

    Otherwise an authoring mistake in either is discovered from a 500 rate on
    every attachment a page tries to draw, which is the shape ``checks.W009``
    was written for and the same argument applies here.
    """

    def test_silent_on_the_shipped_defaults(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {}
        assert checks.check_describe_seam() == []

    def test_unimportable_permission_is_an_error(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"DESCRIBE_PERMISSIONS": ["nope.NotAClass"]}
        findings = checks.check_describe_seam()

        assert [f.id for f in findings] == [checks.E005_DESCRIBE_SEAM_UNUSABLE]

    def test_empty_guard_is_reported_as_publishing_the_endpoint(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"DESCRIBE_PERMISSIONS": []}
        findings = checks.check_describe_seam()

        assert [f.id for f in findings] == [checks.W012_DESCRIBE_GUARD_EMPTY]

    def test_allow_any_says_it_on_purpose_and_is_silent(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {
            "DESCRIBE_PERMISSIONS": ["rest_framework.permissions.AllowAny"]
        }
        assert checks.check_describe_seam() == []

    @pytest.mark.parametrize("rate", ["sixty/min", "60", "/min"])
    def test_unparseable_rate_is_an_error(self, settings, rate):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"DESCRIBE_THROTTLE": rate}
        findings = checks.check_describe_seam()

        assert [f.id for f in findings] == [checks.E005_DESCRIBE_SEAM_UNUSABLE]

    def test_an_empty_rate_means_unthrottled_not_broken(self, settings):
        from stapel_cdn import checks

        settings.STAPEL_CDN = {"DESCRIBE_ANON_THROTTLE": ""}
        assert checks.check_describe_seam() == []
