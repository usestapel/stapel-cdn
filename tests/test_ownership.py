"""Cross-principal isolation on the content-addressed intake paths (CDN-02).

The property under test is one sentence: **an upload never tells you anything
about somebody else's bytes, and never hands you their object.**

Content addressing makes that non-obvious. The hash of a file is its identity,
so "store this" and "look this up" are the same operation, and a lookup keyed
on content alone answers a question the uploader is not entitled to ask — does
anyone in this deployment hold exactly these bytes? A `200 already exists`
against a `201 created` is that answer, and the body that comes with it is the
other holder's row.

Every test here is written from the attacker's seat: tenant B has obtained (or
guessed) tenant A's file and uploads it. What B may learn is nothing, and what
B may receive is B's own object.
"""
import pytest
from io import BytesIO

from PIL import Image as PILImage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from stapel_core.django.users.models import User

from stapel_cdn.models import File, Image, Video
from stapel_cdn.ownership import (
    SCOPE_GLOBAL,
    SCOPE_OWNER,
    dedup_scope,
    owner_usage,
    quota_ceiling,
    quota_exceeded,
    shared_binary_exists,
)

SECRET_BYTES_NAME = "tenant-a-secret.jpg"


def make_image_bytes(width=64, height=48, fmt="JPEG", color="red"):
    img = PILImage.new("RGB", (width, height), color=color)
    buffer = BytesIO()
    img.save(buffer, format=fmt)
    return buffer.getvalue()


def upload_of(content, name=SECRET_BYTES_NAME, content_type="image/jpeg"):
    """A fresh SimpleUploadedFile over the SAME bytes.

    Fresh per call on purpose: an upload handle is consumed by hashing, and a
    reused one would make the second request look empty rather than colliding.
    """
    return SimpleUploadedFile(name=name, content=content, content_type=content_type)


@pytest.fixture
def tenant_a(db):
    return User.objects.create_user(
        username="tenant-a", email="a@example.com", password="pw-a-not-for-production"
    )


@pytest.fixture
def tenant_b(db):
    return User.objects.create_user(
        username="tenant-b", email="b@example.com", password="pw-b-not-for-production"
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
class TestCollidingUploadRevealsNothing:
    """Tenant B uploads tenant A's exact bytes."""

    @pytest.mark.parametrize(
        "url,image_type",
        [
            ("/cdn/api/v1/upload/image/", "product"),
            ("/cdn/api/v1/upload/avatar/", "avatar"),
            ("/cdn/api/v1/images/product/upload/", "product"),
        ],
    )
    def test_b_gets_its_own_object_not_a(self, tenant_a, tenant_b, url, image_type):
        content = make_image_bytes()

        first = client_for(tenant_a).post(
            url, {"file": upload_of(content)}, format="multipart"
        )
        assert first.status_code == status.HTTP_201_CREATED
        a_image = Image.objects.get(uploaded_by=tenant_a, type=image_type)

        second = client_for(tenant_b).post(
            url, {"file": upload_of(content, name="whatever-b-called-it.jpg")},
            format="multipart",
        )

        # No equality oracle: B's first upload of these bytes is a creation,
        # exactly as it would be for bytes nobody has ever held.
        assert second.status_code == status.HTTP_201_CREATED

        # No inherited object: B's response names B's row, never A's.
        body = second.json()
        assert body["image"]["id"] != a_image.id
        b_image = Image.objects.get(uploaded_by=tenant_b, type=image_type)
        assert body["image"]["id"] == b_image.id

        # No leaked metadata: A's filename is A's business.
        assert SECRET_BYTES_NAME not in second.content.decode()
        assert body["image"]["original_filename"] == "whatever-b-called-it.jpg"

    def test_b_repeating_its_own_upload_still_dedups(self, tenant_b):
        """Owner-scoped does not mean disabled — B's own re-upload is a 200."""
        content = make_image_bytes(color="blue")
        client = client_for(tenant_b)
        url = "/cdn/api/v1/upload/avatar/"

        first = client.post(url, {"file": upload_of(content)}, format="multipart")
        second = client.post(url, {"file": upload_of(content)}, format="multipart")

        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_200_OK
        assert second.json()["image"]["id"] == first.json()["image"]["id"]
        assert Image.objects.filter(uploaded_by=tenant_b, type="avatar").count() == 1

    def test_video_collision_is_scoped(self, tenant_a, tenant_b):
        content = b"\x00\x00\x00\x18ftypmp42" + b"tenant-a video payload" * 8
        url = "/cdn/api/v1/upload/video/"

        first = client_for(tenant_a).post(
            url, {"file": upload_of(content, "a.mp4", "video/mp4")}, format="multipart"
        )
        assert first.status_code == status.HTTP_201_CREATED

        second = client_for(tenant_b).post(
            url, {"file": upload_of(content, "b.mp4", "video/mp4")}, format="multipart"
        )
        assert second.status_code == status.HTTP_201_CREATED
        assert second.json()["video"]["id"] != first.json()["video"]["id"]
        assert Video.objects.filter(uploaded_by=tenant_b).count() == 1

    def test_generic_file_collision_is_scoped(self, tenant_a, tenant_b):
        content = b"%PDF-1.4\n" + b"tenant A confidential contract\n" * 4
        url = "/cdn/api/v1/upload/file/"

        first = client_for(tenant_a).post(
            url,
            {"file": upload_of(content, "contract-tenant-a.pdf", "application/pdf")},
            format="multipart",
        )
        assert first.status_code == status.HTTP_201_CREATED

        second = client_for(tenant_b).post(
            url,
            {"file": upload_of(content, "guess.pdf", "application/pdf")},
            format="multipart",
        )
        assert second.status_code == status.HTTP_201_CREATED
        assert "contract-tenant-a.pdf" not in second.content.decode()
        assert File.objects.filter(uploaded_by=tenant_b).count() == 1


@pytest.mark.django_db
class TestServicePoolIsolation:
    """`cdn.import_from_url` writes rows with no user behind them."""

    def test_import_does_not_adopt_a_users_object(self, tenant_a, monkeypatch):
        from stapel_cdn import fetch, functions

        content = make_image_bytes(color="green")
        client_for(tenant_a).post(
            "/cdn/api/v1/upload/avatar/", {"file": upload_of(content)},
            format="multipart",
        )
        a_image = Image.objects.get(uploaded_by=tenant_a, type="avatar")

        monkeypatch.setattr(fetch, "enforce_rate_limit", lambda caller: None)
        monkeypatch.setattr(fetch, "fetch_image_bytes", lambda url: content)

        result = functions.import_from_url(
            {"url": "https://example.invalid/x.jpg", "image_type": "avatar",
             "caller": "some-service"}
        )

        # The ref is content-addressed, so it necessarily matches — but the
        # service must have created its OWN row rather than pointing the
        # caller at a row a user owns and can delete.
        assert result["ref"] == f"avatar/{a_image.file_hash}"
        service_rows = Image.objects.filter(uploaded_by__isnull=True, type="avatar")
        assert service_rows.count() == 1
        assert service_rows.first().pk != a_image.pk

    def test_service_pool_dedups_against_itself(self, monkeypatch):
        from stapel_cdn import fetch, functions

        content = make_image_bytes(color="yellow")
        monkeypatch.setattr(fetch, "enforce_rate_limit", lambda caller: None)
        monkeypatch.setattr(fetch, "fetch_image_bytes", lambda url: content)

        payload = {"url": "https://example.invalid/y.jpg", "image_type": "avatar",
                   "caller": "some-service"}
        functions.import_from_url(payload)
        functions.import_from_url(payload)

        assert Image.objects.filter(uploaded_by__isnull=True, type="avatar").count() == 1


@pytest.mark.django_db
class TestDedupScopeSetting:
    """The scope is a setting in the STAPEL_CDN namespace, and it fails closed."""

    def test_default_is_owner(self):
        assert dedup_scope() == SCOPE_OWNER

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "DEDUP_SCOPE": "GLOBAL"})
    def test_value_is_normalised(self):
        assert dedup_scope() == SCOPE_GLOBAL

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "DEDUP_SCOPE": "per-tenant-ish"})
    def test_unknown_value_falls_back_to_owner(self):
        """A typo must not silently reopen the cross-principal lookup."""
        from stapel_cdn.checks import W005_DEDUP_SCOPE_INVALID, check_dedup_scope

        assert dedup_scope() == SCOPE_OWNER
        assert [w.id for w in check_dedup_scope()] == [W005_DEDUP_SCOPE_INVALID]

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "DEDUP_SCOPE": "global"})
    def test_global_is_opt_in_and_reported(self, tenant_a, tenant_b):
        from stapel_cdn.checks import W006_DEDUP_SCOPE_GLOBAL, check_dedup_scope

        assert [w.id for w in check_dedup_scope()] == [W006_DEDUP_SCOPE_GLOBAL]

        content = make_image_bytes(color="purple")
        url = "/cdn/api/v1/upload/avatar/"
        client_for(tenant_a).post(url, {"file": upload_of(content)}, format="multipart")
        second = client_for(tenant_b).post(
            url, {"file": upload_of(content)}, format="multipart"
        )
        # The audited legacy behaviour, reachable only by asking for it.
        assert second.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestSharedBinaryRefcount:
    """One blob, two owners: erasing one must not blank the other's object."""

    def test_shared_blob_survives_one_owners_purge(self, tenant_a, tenant_b):
        from stapel_cdn.gdpr import CDNGDPRProvider

        content = make_image_bytes(color="orange")
        url = "/cdn/api/v1/upload/avatar/"
        client_for(tenant_a).post(
            url, {"file": upload_of(content, "same-name.jpg")}, format="multipart"
        )
        client_for(tenant_b).post(
            url, {"file": upload_of(content, "same-name.jpg")}, format="multipart"
        )

        a_image = Image.objects.get(uploaded_by=tenant_a, type="avatar")
        b_image = Image.objects.get(uploaded_by=tenant_b, type="avatar")
        # Identical bytes and identical names land on ONE content-addressed
        # path — which is exactly why an unconditional unlink is a cross-owner
        # defect rather than a storage detail.
        assert a_image.original.name == b_image.original.name
        assert shared_binary_exists(a_image)

        CDNGDPRProvider().purge_unreferenced(tenant_a.id)

        assert not Image.objects.filter(pk=a_image.pk).exists()
        b_image.refresh_from_db()
        assert b_image.original.storage.exists(b_image.original.name)

    def test_last_holder_takes_the_blob_with_it(self, tenant_b):
        from stapel_cdn.gdpr import CDNGDPRProvider

        content = make_image_bytes(color="brown")
        client_for(tenant_b).post(
            "/cdn/api/v1/upload/avatar/", {"file": upload_of(content)},
            format="multipart",
        )
        image = Image.objects.get(uploaded_by=tenant_b, type="avatar")
        storage, name = image.original.storage, image.original.name
        assert not shared_binary_exists(image)

        CDNGDPRProvider().purge_unreferenced(tenant_b.id)

        assert not storage.exists(name)


@pytest.mark.django_db
class TestVariantNamespaceIsNotWritable:
    """A caller-chosen filename must not land on a generated variant's path."""

    def test_upload_named_like_a_variant_is_moved_aside(self, tenant_b):
        content = make_image_bytes(color="pink")
        response = client_for(tenant_b).post(
            "/cdn/api/v1/upload/avatar/",
            {"file": upload_of(content, name="720w.webp", content_type="image/webp")},
            format="multipart",
        )
        # Refused as a webp-named JPEG, or stored — either is fine, but if it
        # is stored it must not be stored AS the variant.
        if response.status_code == status.HTTP_201_CREATED:
            image = Image.objects.get(uploaded_by=tenant_b, type="avatar")
            assert not image.original.name.endswith("/720w.webp")

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("720w.webp", "original_720w.webp"),
            ("120.webp", "original_120.webp"),
            ("480H.WEBP", "original_480H.WEBP"),
            ("holiday.jpg", "holiday.jpg"),
            ("720w.jpg", "720w.jpg"),
        ],
    )
    def test_reserved_names_are_prefixed(self, filename, expected):
        from stapel_cdn.models import _safe_original_name

        assert _safe_original_name(filename) == expected


@pytest.mark.django_db
class TestPerOwnerQuota:
    """An identity that costs one POST to mint does not get unbounded storage."""

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_OBJECTS_PER_OWNER": 1})
    def test_object_ceiling_refuses_the_second_object(self, tenant_b):
        client = client_for(tenant_b)
        url = "/cdn/api/v1/upload/image/"

        first = client.post(
            url, {"file": upload_of(make_image_bytes(color="red"), "1.jpg")},
            format="multipart",
        )
        second = client.post(
            url, {"file": upload_of(make_image_bytes(color="blue"), "2.jpg")},
            format="multipart",
        )

        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_403_FORBIDDEN
        assert second.json()["localizable_error"] == "error.403.storage_quota_exceeded"
        assert Image.objects.filter(uploaded_by=tenant_b).count() == 1

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_BYTES_PER_OWNER": 1})
    def test_byte_ceiling_refuses_the_first_object(self, tenant_b):
        response = client_for(tenant_b).post(
            "/cdn/api/v1/upload/avatar/", {"file": upload_of(make_image_bytes())},
            format="multipart",
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["params"]["limit"] == "bytes"

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_OBJECTS_PER_OWNER": 1})
    def test_ceiling_is_per_owner_not_deployment_wide(self, tenant_a, tenant_b):
        """A's usage must not spend B's quota."""
        url = "/cdn/api/v1/upload/image/"
        client_for(tenant_a).post(
            url, {"file": upload_of(make_image_bytes(color="red"), "a.jpg")},
            format="multipart",
        )
        response = client_for(tenant_b).post(
            url, {"file": upload_of(make_image_bytes(color="blue"), "b.jpg")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "MAX_OBJECTS_PER_OWNER": "unlimited",
                                   "MAX_BYTES_PER_OWNER": "unlimited"})
    def test_the_explicit_opt_out_disables_the_ceiling(self, tenant_b):
        """Removing a ceiling is a thing a deployment SAYS."""
        assert quota_exceeded(tenant_b, 10 ** 12) is None

    @pytest.mark.parametrize("nothing", [0, None, "", "  ", "none", -1, "lots"])
    def test_saying_nothing_no_longer_means_unlimited(self, tenant_b, nothing):
        """0/None/""/garbage used to land on "no ceiling" — three by accident."""
        with override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                           "MAX_OBJECTS_PER_OWNER": nothing,
                                           "MAX_BYTES_PER_OWNER": nothing}):
            assert quota_ceiling("MAX_OBJECTS_PER_OWNER") == 1000
            assert quota_ceiling("MAX_BYTES_PER_OWNER") == 2 * 1024 * 1024 * 1024
            over = quota_exceeded(tenant_b, 10 ** 12)
            assert over is not None and over["limit"] == "bytes"

    def test_shipped_defaults_are_ceilings(self):
        assert quota_ceiling("MAX_OBJECTS_PER_OWNER") == 1000
        assert quota_ceiling("MAX_BYTES_PER_OWNER") == 2 * 1024 * 1024 * 1024

    @pytest.mark.parametrize("nothing", [0, None, "", "lots"])
    def test_an_unusable_ceiling_is_reported_at_boot(self, nothing):
        from stapel_cdn.checks import W007_QUOTA_CEILING_INVALID, check_owner_quotas

        with override_settings(STAPEL_CDN={"MAX_BYTES_PER_OWNER": nothing}):
            findings = check_owner_quotas()
        assert [f.id for f in findings] == [W007_QUOTA_CEILING_INVALID]

    @pytest.mark.parametrize("usable", [1, 4096, "unlimited", "UNLIMITED"])
    def test_a_usable_ceiling_is_silent_at_boot(self, usable):
        from stapel_cdn.checks import check_owner_quotas

        with override_settings(STAPEL_CDN={"MAX_OBJECTS_PER_OWNER": usable,
                                           "MAX_BYTES_PER_OWNER": usable}):
            assert check_owner_quotas() == []

    def test_a_principal_the_quota_cannot_attribute_is_refused(self):
        """No owner, no usage to count against — so no ceiling at all.

        The exemption meant the ONE caller the quota could not measure was
        the one caller it did not bound.
        """
        from django.contrib.auth.models import AnonymousUser

        over = quota_exceeded(AnonymousUser(), 1)
        assert over is not None
        assert over["limit"] == "owner"

    def test_a_principal_without_a_primary_key_is_refused_too(self):
        class Nobody:
            pk = None
            is_authenticated = True

        over = quota_exceeded(Nobody(), 1)
        assert over is not None
        assert over["limit"] == "owner"

    def test_usage_counts_every_media_kind(self, tenant_b):
        client = client_for(tenant_b)
        client.post(
            "/cdn/api/v1/upload/avatar/", {"file": upload_of(make_image_bytes())},
            format="multipart",
        )
        client.post(
            "/cdn/api/v1/upload/file/",
            {"file": upload_of(b"%PDF-1.4\nsmall", "doc.pdf", "application/pdf")},
            format="multipart",
        )
        objects, total = owner_usage(tenant_b)
        assert objects == 2
        assert total > 0


@pytest.mark.django_db
class TestGenericIntakeRefusesActiveContent:
    """Extension and Content-Type are written by the uploader; bytes are not."""

    @pytest.mark.parametrize(
        "payload",
        [
            b"<html><body><script>fetch('/steal')</script></body></html>",
            b"<!DOCTYPE html>\n<html></html>",
            b"  \n\t<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>",
            b"\xef\xbb\xbf<script>alert(1)</script>",
            b"<?php system($_GET['c']); ?>",
            b"#!/bin/sh\nrm -rf /\n",
        ],
    )
    def test_markup_under_a_document_name_is_refused(self, tenant_b, payload):
        response = client_for(tenant_b).post(
            "/cdn/api/v1/upload/file/",
            {"file": upload_of(payload, "notes.txt", "text/plain")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not File.objects.filter(uploaded_by=tenant_b).exists()

    def test_a_real_document_still_passes(self, tenant_b):
        response = client_for(tenant_b).post(
            "/cdn/api/v1/upload/file/",
            {"file": upload_of(b"name,amount\nrent,100\n", "budget.csv", "text/csv")},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.django_db
class TestPrivateMediaPrefix:
    """Documents and archives get one prefix an operator can deny."""

    def test_documents_land_under_the_private_prefix(self, tenant_b):
        client_for(tenant_b).post(
            "/cdn/api/v1/upload/file/",
            {"file": upload_of(b"%PDF-1.4\ncontract", "c.pdf", "application/pdf")},
            format="multipart",
        )
        stored = File.objects.get(uploaded_by=tenant_b)
        assert stored.original.name.startswith("private/file/")

    def test_images_stay_on_the_public_layout(self, tenant_b):
        client_for(tenant_b).post(
            "/cdn/api/v1/upload/avatar/", {"file": upload_of(make_image_bytes())},
            format="multipart",
        )
        image = Image.objects.get(uploaded_by=tenant_b)
        assert image.original.name.startswith("avatar/")

    @override_settings(STAPEL_CDN={"ASSET_TYPES": ("avatar", "product"),
                                   "PRIVATE_MEDIA_PREFIX": ""})
    def test_prefix_is_configurable(self, tenant_b):
        client_for(tenant_b).post(
            "/cdn/api/v1/upload/file/",
            {"file": upload_of(b"%PDF-1.4\nflat", "flat.pdf", "application/pdf")},
            format="multipart",
        )
        stored = File.objects.get(uploaded_by=tenant_b)
        assert stored.original.name.startswith("file/")


@pytest.mark.django_db
class TestRefResolutionIsPinnedToOneRow:
    """A ref names content; content can have several holders."""

    def test_refs_always_resolve_to_the_oldest_holder(self, tenant_a, tenant_b):
        from stapel_cdn.services import _batch_resolve_media

        content = make_image_bytes(color="cyan")
        url = "/cdn/api/v1/upload/avatar/"
        client_for(tenant_a).post(url, {"file": upload_of(content)}, format="multipart")
        client_for(tenant_b).post(url, {"file": upload_of(content)}, format="multipart")

        a_image = Image.objects.get(uploaded_by=tenant_a, type="avatar")
        ref = f"avatar/{a_image.file_hash}"

        # Called twice: the point is not merely "resolves" but "resolves to the
        # SAME row every time", or reference tracking drifts between callers.
        assert _batch_resolve_media([ref])[ref].pk == a_image.pk
        assert _batch_resolve_media([ref])[ref].pk == a_image.pk
