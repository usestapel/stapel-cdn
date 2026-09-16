"""``ASSET_TYPES`` is a setting, and every seam must read it — not copy it.

Three times now this library has carried a frozen copy of
``STAPEL_CDN["ASSET_TYPES"]`` beside code that reads it fresh:

* ``services._image_ref_prefixes`` was ``{"product", "avatar"}`` until the
  release whose docstring calls it "the exact same 'half the stack is
  modular, half isn't' gap the spec calls out";
* ``views.IMAGE_PREFIXES`` was the identical literal, in the identical shape,
  and survived that fix — so a deployment with a third asset type resolved
  ``<third>/<hash>`` one way through ``services`` and another through the
  view;
* ``views.ImageUploadView.post`` stored the literal ``"product"``, which the
  shipped default does not contain — so the endpoint had no reachable 2xx at
  all out of the box, while the contract it emits declared a 201.

A test that asserts "the code does not contain the string ``product``" would
catch none of the three reliably and would fire on every docstring that
mentions one. So this gate is behavioural: it configures an asset type that
**no literal anywhere could contain**, and then requires every seam to
honour it. A frozen copy cannot pass, by construction, whatever it is frozen
to and wherever it is written.

The type is deliberately a nonsense word: if this file ever goes green
because someone added it to a default, that is visible in the diff.
"""
import io
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient

#: A value no hardcoded set, tuple or literal in this package contains, and
#: that nothing would plausibly add to a default. The whole gate rests on it.
UNGUESSABLE = "quokkagram"

pytestmark = pytest.mark.django_db


def _user():
    from stapel_core.django.users.models import User

    tag = uuid.uuid4().hex[:10]
    return User.objects.create_user(
        username=f"frozen-{tag}",
        email=f"frozen-{tag}@example.com",
        password="frozen-types-password-7",
    )


def _client():
    client = APIClient()
    client.force_authenticate(user=_user())
    return client


def _image():
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (48, 32), color=(10, 120, 200)).save(buffer, format="JPEG")
    return SimpleUploadedFile(
        f"frozen-{uuid.uuid4().hex[:8]}.jpg",
        buffer.getvalue(),
        content_type="image/jpeg",
    )


def test_the_unguessable_type_is_not_in_any_default():
    """Guard the gate itself: the whole file is meaningless if it leaked in."""
    from stapel_cdn.conf import DEFAULT_ASSET_TYPES, DEFAULTS

    assert UNGUESSABLE not in DEFAULT_ASSET_TYPES
    assert UNGUESSABLE not in DEFAULTS["ASSET_TYPES"]
    assert DEFAULTS["DEFAULT_UPLOAD_TYPE"] is None


@override_settings(STAPEL_CDN={"ASSET_TYPES": (UNGUESSABLE,)})
def test_the_model_choices_honour_the_setting():
    from stapel_cdn.models import get_image_type_choices

    assert [value for value, _ in get_image_type_choices()] == [UNGUESSABLE]


@override_settings(STAPEL_CDN={"ASSET_TYPES": (UNGUESSABLE,)})
def test_the_fixed_upload_type_honours_the_setting():
    from stapel_cdn.models import get_default_upload_type

    assert get_default_upload_type() == UNGUESSABLE


@override_settings(STAPEL_CDN={"ASSET_TYPES": (UNGUESSABLE,)})
def test_the_upload_endpoint_stores_the_configured_type():
    """The defect this release closes, stated as the behaviour it wanted.

    Before 0.23.0 this answered 400 — the view checked a literal "product"
    against the configured types and refused when it was absent, which on the
    shipped default is always.
    """
    response = _client().post(
        "/cdn/api/v1/upload/image/", {"file": _image()}, format="multipart"
    )

    assert response.status_code == 201, response.content
    assert response.json()["image"]["type"] == UNGUESSABLE


@override_settings(STAPEL_CDN={"ASSET_TYPES": (UNGUESSABLE,)})
def test_the_stored_type_is_a_member_of_the_declared_enum():
    """The point of reading the setting: the two can no longer disagree.

    ``TypeEnum`` in the emitted contract is generated from ``ASSET_TYPES``,
    and so is the stored type. Asserting them against each other here is what
    makes "true under every configuration" a checked claim rather than an
    argument.
    """
    from stapel_cdn.models import get_default_upload_type, get_image_type_choices

    declared = [value for value, _ in get_image_type_choices()]
    assert get_default_upload_type() in declared


@override_settings(STAPEL_CDN={"ASSET_TYPES": (UNGUESSABLE,)})
def test_both_ref_resolvers_route_the_configured_prefix_to_image():
    """The second frozen copy, driven through BOTH seams.

    ``services.image_ref_prefixes`` read the setting; ``views.IMAGE_PREFIXES``
    was a literal beside it. A ref built from a configured type must resolve
    the same way through either, and before this release it did not.
    """
    from stapel_cdn.services import image_ref_prefixes
    from stapel_cdn.views import _batch_resolve_media

    assert image_ref_prefixes() == {UNGUESSABLE}

    upload = _client().post(
        "/cdn/api/v1/upload/image/", {"file": _image()}, format="multipart"
    )
    assert upload.status_code == 201, upload.content
    file_hash = upload.json()["image"]["file_hash"]

    ref = f"{UNGUESSABLE}/{file_hash}"
    resolved = _batch_resolve_media([ref])
    assert ref in resolved, (
        "the view's ref resolver did not route a CONFIGURED asset type to "
        "Image — it is reading a frozen copy of ASSET_TYPES again"
    )


@override_settings(
    STAPEL_CDN={"ASSET_TYPES": ("avatar", UNGUESSABLE), "DEFAULT_UPLOAD_TYPE": UNGUESSABLE}
)
def test_an_explicit_upload_type_wins_over_the_first_entry():
    """The knob a deployment that relied on the old literal reaches for."""
    from stapel_cdn.models import get_default_upload_type

    assert get_default_upload_type() == UNGUESSABLE

    response = _client().post(
        "/cdn/api/v1/upload/image/", {"file": _image()}, format="multipart"
    )
    assert response.status_code == 201, response.content
    assert response.json()["image"]["type"] == UNGUESSABLE


@override_settings(
    STAPEL_CDN={"ASSET_TYPES": ("avatar",), "DEFAULT_UPLOAD_TYPE": UNGUESSABLE}
)
def test_a_misconfigured_upload_type_is_refused_and_reported():
    """Named but not in ASSET_TYPES: refuse the upload, and say so at boot."""
    from stapel_cdn.checks import (
        W014_UPLOAD_TYPE_NOT_CONFIGURED,
        check_default_upload_type,
    )

    response = _client().post(
        "/cdn/api/v1/upload/image/", {"file": _image()}, format="multipart"
    )
    assert response.status_code == 400, response.content

    ids = [issue.id for issue in check_default_upload_type()]
    assert ids == [W014_UPLOAD_TYPE_NOT_CONFIGURED]


@override_settings(STAPEL_CDN={"ASSET_TYPES": ()})
def test_an_empty_asset_types_is_reported_rather_than_stored():
    from stapel_cdn.checks import (
        W014_UPLOAD_TYPE_NOT_CONFIGURED,
        check_default_upload_type,
    )
    from stapel_cdn.models import get_default_upload_type

    assert get_default_upload_type() is None
    ids = [issue.id for issue in check_default_upload_type()]
    assert ids == [W014_UPLOAD_TYPE_NOT_CONFIGURED]


def test_the_shipped_default_is_green():
    """No warning on an out-of-the-box install — the point of the release."""
    from stapel_cdn.checks import check_default_upload_type

    with override_settings(STAPEL_CDN={}):
        assert check_default_upload_type() == []


def test_the_check_is_not_blind():
    """A canary: the check must actually fire on a configuration it forbids.

    Every assertion above that expects ``[]`` is satisfied by a check that
    returns ``[]`` unconditionally. This one is not.
    """
    from stapel_cdn.checks import check_default_upload_type

    with override_settings(
        STAPEL_CDN={"ASSET_TYPES": ("avatar",), "DEFAULT_UPLOAD_TYPE": "not-a-type"}
    ):
        assert check_default_upload_type() != []
