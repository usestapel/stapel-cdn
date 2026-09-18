"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason, asserted exactly current;
* every read, and every write whose declared body carries a nullable field or
  a derived URL, is driven a SECOND time in its emptiest legal state
  (``EMPTY_STATE``): an image before its variants exist and one after, a
  video before ffmpeg has read it and one after, a hash that resolves and one
  that does not. Every null finding in the first wave of this gate was there.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT AND THE CONFIGURATION. ``codegen_urls.py`` mounts ``cdn/api/`` and
the module's own ``urls.py`` contributes ``v1/``, so the document is written
against ``/cdn/api/v1/…``; ``stapel_cdn/tests/urls.py`` mounts the SAME
prefix, so unlike five of the first eight libraries in this wave this
module's suite was already looking where its document points.

The settings are the other half of the mount here, and this module is the
first in the wave where they matter. ``_codegen_settings.py`` sets **no**
``STAPEL_CDN`` block, so the document was emitted with
``ASSET_TYPES == ("avatar",)`` — which is why ``TypeEnum`` in the committed
schema has exactly one member. ``conftest.py`` configures
``ASSET_TYPES = ("avatar", "product")`` for the historical fixtures. This
gate pins the EMISSION's configuration, because a contract check run against
a different deployment than the document describes is checking a different
document.

WHAT IT FOUND on its first run — 11 operations, 17 declared (method, path,
2xx) rows, all driven, 5 red (2 of them the same defect on an operation's two
declared codes), in three families:

1. ``POST /upload/video/`` declared seven ``variant_*p_url`` fields and a
   ``poster_url`` as REQUIRED, non-nullable ``string``/``uri``, and sent
   **null** for all eight on every video it had ever accepted. They are
   ``SerializerMethodField``s returning ``obj.variant_16.url if
   obj.variant_16 else None`` and ``Video.poster_url``, which returns
   ``None`` "while none has been written" (models.py, its own docstring).
   The ``@extend_schema_field(OpenApiTypes.URI)`` decorator above each getter
   was what erased the null from the claim: it pins the field to a bare URI
   and drf-spectacular copies that.

   **CLOSED in 0.24.0**: the eight are produced ASYNCHRONOUSLY — the ladder
   is transcoded and the poster cut after the upload has answered — so they
   are declared ``nullable`` (``NULLABLE_URI`` in serializers.py) and stay in
   the payload, where a client that polls the row finds them filling in.
   The 200 recipe below now drives a row mid-transcode (one rung and the
   poster written, the rest still null), because a gate that only ever sees
   these null has checked one half of a two-valued claim.
2. **CLOSED in 0.23.0**, recorded because the shape recurs. ``POST
   /upload/image/`` declared a 201 and a 200 it could not answer under the
   configuration the contract was emitted from: the view stored a FIXED
   literal ``"product"`` and refused with 400 when that string was not in
   ``ASSET_TYPES`` — and it is not, in the emission's own settings, so the
   endpoint had no reachable 2xx at all out of the box. Adding ``"product"``
   moved the lie rather than fixing it: the 201 then carried ``image.type ==
   "product"`` while the same document's ``TypeEnum`` — generated from the
   same setting — admitted only ``"avatar"``. Wrong under both
   configurations, in opposite directions, which is what told us neither
   configuration was the defect. The stored type is now read from
   ``ASSET_TYPES`` (``DEFAULT_UPLOAD_TYPE``, defaulting to its first entry),
   so the enum and the wire are generated from one setting and cannot
   disagree. ``test_upload_image_stores_a_type_its_own_document_admits``
   drives it here under the emission's own defaults;
   ``tests/test_asset_types_are_never_frozen.py`` is the gate for the class.
3. ``GET /images/{image_type}/random/`` declares ``uploaded_by_username`` as
   a REQUIRED, non-nullable ``string`` and OMITS the key entirely from every
   row whose ``uploaded_by`` is null. The field is
   ``CharField(source="uploaded_by.username")`` in all four model
   serializers, and DRF raises ``SkipField`` when a traversed relation is
   None — so the key is dropped rather than sent as null. The field DIRECTLY
   ABOVE it, ``uploaded_by``, is declared nullable: the same serializer says
   the null uploader is expected one line up and then promises a username
   derived from it. An unowned row is ordinary — ``cdn.import_from_url``
   writes ``uploaded_by=None`` on purpose (ownership.py:100-109) — and this
   operation filters on type and ``is_processed`` only, never on an owner.
   Found on the EMPTY state, like every null finding in this gate's history.

Everything else held — including every nullable field of ``Image``,
``Audio`` and ``FileModel`` in the states this module can reach, and the
four-branch ``FileExistsResponse.file`` union — and
``test_the_gate_is_not_blind`` proves that is a finding rather than a gate
that never looked.

Left exactly as it is: this is a gate, not a fix.
"""
import copy
import hashlib
import io
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``cdn/api/`` + the module's own ``v1/``).
urlpatterns = [
    url_path("cdn/api/", include("stapel_cdn.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/cdn/api/v1"

#: Matches ``SERVICE_API_KEY`` below — what ``ServiceAPIKeyMiddleware``
#: compares ``X-API-KEY`` against, and the only credential ``POST
#: /refs/sync/`` (``IsServiceRequest``) accepts.
SERVICE_KEY = "wire-contract-service-key"


@pytest.fixture(autouse=True)
def _emission_deployment(tmp_path):
    """Pin MEDIA_ROOT, and run under the settings the DOCUMENT was emitted with.

    ``MEDIA_ROOT`` matters more here than anywhere else in this wave: every
    recipe below stores a real file through it, and the harness points it at
    a fixed ``/tmp`` path shared by every run on the machine, so uploads
    accumulate there forever and two runs can see each other's objects.

    ``STAPEL_CDN`` is reset to ``{}`` — i.e. to the library defaults, which
    is what ``_codegen_settings.py`` emits under. The suite's own conftest
    adds ``"product"`` to ``ASSET_TYPES``; the committed ``TypeEnum`` says
    ``["avatar"]``, so the suite's configuration and the committed document
    describe two different deployments and only one of them is the one under
    test.

    ``MIDDLEWARE`` gains ``ServiceAPIKeyMiddleware``: the harness configures
    none at all, and without it ``IsServiceRequest`` can never be satisfied
    through a URL — which is why this module's own refs/sync tests bypass
    routing with ``APIRequestFactory`` and set the attribute by hand. A
    contract gate has to go through the door the document names.
    """
    from django.core.cache import cache

    cache.clear()
    with override_settings(
        MEDIA_ROOT=str(tmp_path / "media"),
        STAPEL_CDN={},
        SERVICE_API_KEY=SERVICE_KEY,
        MIDDLEWARE=["stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware"],
    ):
        yield
    cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _undiscriminated_union(node):
    """A ``oneOf`` whose branches OVERLAP by construction.

    ``FileExistsResponse.file`` is emitted as ``oneOf: [Image, Video, Audio,
    FileModel]``. None of the four forbids the others' properties, and
    ``FileModel``'s required list is a SUBSET of ``Image``'s — so an image
    body satisfies two branches and an exclusive ``oneOf`` rejects exactly
    what the document plainly describes. The union carries no
    ``discriminator`` to tell the branches apart, so it is read as
    alternatives: this is a generator idiom colliding with ``oneOf``
    exclusivity, not a claim the wire breaks.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list) or "discriminator" in node:
        return False
    return len(branches) > 1


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type`` (or
    beside a ``oneOf``, which is how ``FileExistsResponse.file`` is emitted);
    JSON Schema has no such keyword and would refuse the null — which is
    exactly the value the "not found" branch answers. The second conversion
    is the overlapping union above. Everything else drf-spectacular emits
    (``$ref``, ``allOf``, ``enum``, ``format``, ``required``, ``readOnly``)
    is JSON Schema as written, or inert.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if _undiscriminated_union(rebuilt):
        rebuilt["anyOf"] = rebuilt.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def make_user(**kwargs):
    from stapel_core.django.users.models import User

    defaults = dict(
        username=_unique("wire-"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def service_client():
    """The ``X-API-KEY`` door, through the real middleware."""
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=SERVICE_KEY)
    return client


def image_bytes(color=(220, 40, 40), size=(64, 48)):
    """Real JPEG bytes, from an encoder that is NOT the library under test.

    Pillow is a test-only dependency here on purpose (see conftest.py): a
    fixture produced by libvips and then read back by libvips proves the
    round trip and nothing about a real file.
    """
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", size, color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


def image_upload(color=None):
    return SimpleUploadedFile(
        f"{_unique('photo-')}.jpg",
        image_bytes(color or (uuid.uuid4().int % 250, 40, 40)),
        content_type="image/jpeg",
    )


def video_upload():
    """An MP4 container header — enough bytes to pass the sniff, and this
    module never decodes a video anyway (the ffmpeg pass is a documented
    TODO), so no real stream is being stood in for."""
    body = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + _unique("v").encode()
    return SimpleUploadedFile(f"{_unique('clip-')}.mp4", body, content_type="video/mp4")


def audio_upload():
    body = b"\x1aE\xdf\xa3" + b"webm-voice-message-" + _unique("a").encode()
    return SimpleUploadedFile(
        f"{_unique('voice-')}.webm", body, content_type="audio/webm"
    )


def document_upload():
    body = b"plain text, no markup, nothing a browser would execute " + _unique("d").encode()
    return SimpleUploadedFile(f"{_unique('doc-')}.txt", body, content_type="text/plain")


def _content(uploaded):
    uploaded.seek(0)
    body = uploaded.read()
    uploaded.seek(0)
    return body


def again(uploaded):
    """The same bytes, a second time — the dedup (200) path's input."""
    return SimpleUploadedFile(
        uploaded.name, _content(uploaded), content_type=uploaded.content_type
    )


def sha256(uploaded):
    return hashlib.sha256(_content(uploaded)).hexdigest()


def post_file(client, url, uploaded):
    return client.post(url, {"file": uploaded}, format="multipart")


def variants_generated(image):
    """Run the REAL variant pass for one image, inline.

    ``generate_image_variants_on_save`` dispatches ``process_image_async``
    through celery on purpose (a pyvips pipeline on a request thread is a
    trivial CPU DoS), and this harness has no broker — so every uploaded row
    stays ``variants_status: "pending"``. Calling the task's own body is what
    makes the OTHER state reachable: the same code the worker runs, minus the
    queue. ``is_processed`` and ``variants_ready_at`` — the two fields the
    contract's ``variants_status``/``variants_ready_at`` are derived from —
    are written by ``generate_previews`` itself, not by this helper.
    """
    from stapel_cdn.models import Image
    from stapel_cdn.tasks import generate_previews

    generate_previews(image.id, watermark=False)
    return Image.objects.get(pk=image.id)


def stored_image(user, image_type="avatar", processed=False, **kwargs):
    """One Image row created the way the upload views create it."""
    from stapel_cdn.models import Image

    uploaded = image_upload()
    defaults = dict(
        file_hash=sha256(uploaded),
        original_filename=uploaded.name,
        file_extension=".jpg",
        type=image_type,
        original=uploaded,
        original_size=uploaded.size,
        uploaded_by=user,
    )
    defaults.update(kwargs)
    image = Image.objects.create(**defaults)
    if processed:
        image = variants_generated(image)
    return image


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def url(self, params=None):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        return url

    def __call__(self, client, params=None, data=None, query="", fmt="json", **extra):
        url = self.url(params) + query
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url, **extra)
        return send(url, data if data is not None else {}, format=fmt, **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``. Every key here
#: names an explicit code: this module declares TWO 2xx codes on six of its
#: eleven operations (201 created / 200 already-exists), and they are
#: different wire states, not one operation seen twice.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe.
EMPTY_STATE = {}


def recipe(method, path, code, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares is reachable from a test
#: client, including the service-only ``refs/sync`` — that one needs
#: ``ServiceAPIKeyMiddleware``, which the fixture above installs, rather than
#: a stand-in for the permission.
UNDRIVABLE: dict = {}


# ── describe ─────────────────────────────────────────────────────────────────


@recipe("POST", "/describe/", 200)
def _describe(call):
    """One ref that resolves and one that never will — the endpoint's whole
    posture is that the second is DATA, not an error."""
    user = make_user()
    image = stored_image(user, processed=True)
    return call(
        client_for(user),
        data={"refs": [f"avatar/{image.file_hash}", f"avatar/{'d4' * 32}"]},
    )


@empty_state("POST", "/describe/", 200)
def _describe_nothing_resolves(call):
    """Every ref gone: ``items`` is an empty MAP and ``missing`` carries them
    all — the shape a page of dead attachments actually gets."""
    return call(client_for(make_user()), data={"refs": [f"avatar/{'d4' * 32}"]})


# ── file existence ───────────────────────────────────────────────────────────


@recipe("GET", "/file/exists/", 200)
def _exists_get(call):
    user = make_user()
    image = stored_image(user, processed=True)
    return call(client_for(user), query=f"?file_hash={image.file_hash}")


@empty_state("GET", "/file/exists/", 200)
def _exists_get_missing(call):
    """The answer every pre-upload check gets the first time: ``exists``
    false, and the null ``type`` and null ``file`` the declaration marks
    REQUIRED."""
    return call(client_for(make_user()), query=f"?file_hash={'d4' * 32}")


@recipe("POST", "/file/exists/", 200)
def _exists_post(call):
    user = make_user()
    from stapel_cdn.models import File

    uploaded = document_upload()
    File.objects.create(
        file_hash=sha256(uploaded),
        original_filename=uploaded.name,
        file_extension=".txt",
        mime_type="text/plain",
        original=uploaded,
        original_size=uploaded.size,
        uploaded_by=user,
    )
    return call(client_for(user), data={"file_hash": sha256(uploaded)})


@empty_state("POST", "/file/exists/", 200)
def _exists_post_missing(call):
    return call(client_for(make_user()), data={"file_hash": "d4" * 32})


# ── random image ─────────────────────────────────────────────────────────────


@recipe("GET", "/images/{image_type}/random/", 200)
def _random_image(call):
    staff = make_user(is_staff=True)
    stored_image(make_user(), processed=True)
    return call(client_for(staff), params={"image_type": "avatar"})


@empty_state("GET", "/images/{image_type}/random/", 200)
def _random_image_unowned(call):
    """A row nobody owns — a service upload, or one whose uploader was
    erased: the null ``uploaded_by`` the declaration marks REQUIRED, and the
    empty ``uploaded_by_username`` beside it."""
    staff = make_user(is_staff=True)
    stored_image(None, processed=True, uploaded_by=None)
    return call(client_for(staff), params={"image_type": "avatar"})


# ── typed image upload ───────────────────────────────────────────────────────


@recipe("POST", "/images/{image_type}/upload/", 201)
def _typed_image_create(call):
    """A brand-new row: the variant ladder does not exist yet, so this is the
    ``variants_status: "pending"`` / null ``variants_ready_at`` state the
    upload response is documented to carry."""
    return call(
        client_for(make_user()),
        params={"image_type": "avatar"},
        data={"file": image_upload()},
        fmt="multipart",
    )


@recipe("POST", "/images/{image_type}/upload/", 200)
def _typed_image_dedup(call):
    """The same bytes again, AFTER the variant pass ran: the ``ready`` half
    of ``variants_status``, with a real ``variants_ready_at``."""
    from stapel_cdn.models import Image

    user = make_user()
    uploaded = image_upload()
    client = client_for(user)
    first = post_file(client, call.url({"image_type": "avatar"}), uploaded)
    assert first.status_code == 201, first.content
    variants_generated(Image.objects.get(pk=first.json()["image"]["id"]))
    return call(
        client,
        params={"image_type": "avatar"},
        data={"file": again(uploaded)},
        fmt="multipart",
    )


@empty_state("POST", "/images/{image_type}/upload/", 200)
def _typed_image_dedup_pending(call):
    """The same 200, on a row whose variants were never generated."""
    user = make_user()
    uploaded = image_upload()
    client = client_for(user)
    first = post_file(client, call.url({"image_type": "avatar"}), uploaded)
    assert first.status_code == 201, first.content
    return call(
        client,
        params={"image_type": "avatar"},
        data={"file": again(uploaded)},
        fmt="multipart",
    )


# ── refs ─────────────────────────────────────────────────────────────────────


@recipe("POST", "/refs/sync/", 200)
def _refs_sync(call):
    """One ref added, one removed, one that resolves to nothing — all three
    counters non-zero in a single answer."""
    user = make_user()
    keep = stored_image(user)
    drop = stored_image(user)
    entity = str(uuid.uuid4())
    first = call(
        service_client(),
        data={
            "service": "wire",
            "entity_type": "listing",
            "entity_id": entity,
            "old_hashes": [],
            "new_hashes": [f"avatar/{drop.file_hash}"],
        },
    )
    assert first.status_code == 200, first.content
    return call(
        service_client(),
        data={
            "service": "wire",
            "entity_type": "listing",
            "entity_id": entity,
            "old_hashes": [f"avatar/{drop.file_hash}"],
            "new_hashes": [f"avatar/{keep.file_hash}", f"avatar/{'d4' * 32}"],
        },
    )


@empty_state("POST", "/refs/sync/", 200)
def _refs_sync_noop(call):
    """Nothing to add, nothing to remove, nothing unresolved: the zero/zero/[]
    answer a no-op sync sends."""
    return call(
        service_client(),
        data={
            "service": "wire",
            "entity_type": "listing",
            "entity_id": str(uuid.uuid4()),
            "old_hashes": [],
            "new_hashes": [],
        },
    )


# ── audio ────────────────────────────────────────────────────────────────────


@recipe("POST", "/upload/audio/", 201)
def _audio_create(call):
    """A recording the ffprobe pass has not seen yet — null ``duration`` and
    an empty waveform, which is every recording at the moment it lands."""
    return call(
        client_for(make_user()), data={"file": audio_upload()}, fmt="multipart"
    )


@recipe("POST", "/upload/audio/", 200)
def _audio_dedup(call):
    """The same bytes again, after the metadata pass filled the row."""
    from stapel_cdn.models import Audio

    user = make_user()
    uploaded = audio_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    Audio.objects.filter(pk=first.json()["audio"]["id"]).update(
        duration=12.5, preview_b64="data:image/webp;base64,UklGRh"
    )
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


@empty_state("POST", "/upload/audio/", 200)
def _audio_dedup_bare(call):
    user = make_user()
    uploaded = audio_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


# ── avatar ───────────────────────────────────────────────────────────────────


@recipe("POST", "/upload/avatar/", 201)
def _avatar_create(call):
    return call(
        client_for(make_user()), data={"file": image_upload()}, fmt="multipart"
    )


@recipe("POST", "/upload/avatar/", 200)
def _avatar_dedup(call):
    from stapel_cdn.models import Image

    user = make_user()
    uploaded = image_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    variants_generated(Image.objects.get(pk=first.json()["image"]["id"]))
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


@empty_state("POST", "/upload/avatar/", 200)
def _avatar_dedup_pending(call):
    user = make_user()
    uploaded = image_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


# ── generic file ─────────────────────────────────────────────────────────────


@recipe("POST", "/upload/file/", 201)
def _file_create(call):
    return call(
        client_for(make_user()), data={"file": document_upload()}, fmt="multipart"
    )


@recipe("POST", "/upload/file/", 200)
def _file_dedup(call):
    user = make_user()
    uploaded = document_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


# ── the fixed-type image intake ──────────────────────────────────────────────


@recipe("POST", "/upload/image/", 201)
def _image_create(call):
    return call(
        client_for(make_user()), data={"file": image_upload()}, fmt="multipart"
    )


@recipe("POST", "/upload/image/", 200)
def _image_dedup(call):
    user = make_user()
    uploaded = image_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    # Under the emission's own ASSET_TYPES this never reaches 201 — see
    # KNOWN_MISMATCHES. The recipe drives the door the document names and
    # lets the gate report what came back.
    if first.status_code != 201:
        return first
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


# ── video ────────────────────────────────────────────────────────────────────


@recipe("POST", "/upload/video/", 201)
def _video_create(call):
    return call(
        client_for(make_user()), data={"file": video_upload()}, fmt="multipart"
    )


@recipe("POST", "/upload/video/", 200)
def _video_dedup(call):
    """The same bytes again, on a row the pipeline has already worked on.

    Geometry and duration are known, ONE variant of the ladder has been
    written and so has the poster. The eight derived URLs are declared
    nullable because they are produced out of band — but a gate that only
    ever sees them null has checked half of that claim, and the half it
    skipped is the one a player actually renders. Here the 720p rung and the
    poster carry a string while the other six rungs are still null, which is
    the ordinary shape of a ladder mid-transcode.
    """
    from stapel_cdn.models import Video

    user = make_user()
    uploaded = video_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    row = Video.objects.get(pk=first.json()["video"]["id"])
    Video.objects.filter(pk=row.pk).update(
        original_width=1920,
        original_height=1080,
        duration=42.0,
        has_poster=True,
        variant_720=f"video/{row.file_hash}/clip-720p.mp4",
    )
    answer = call(client, data={"file": again(uploaded)}, fmt="multipart")
    body = answer.json()["video"]
    # The nullable half is checked by the empty-state recipe below; this
    # branch exists for the OTHER half, so it fails loudly if the state it
    # was written for stops being reachable.
    assert body["variant_720p_url"], body
    assert body["poster_url"], body
    assert body["variant_1080p_url"] is None, body
    return answer


@empty_state("POST", "/upload/video/", 200)
def _video_dedup_bare(call):
    """The same 200 on a row nothing has probed: null width, null height,
    null duration — three REQUIRED-and-nullable claims at once."""
    user = make_user()
    uploaded = video_upload()
    client = client_for(user)
    first = post_file(client, call.url(), uploaded)
    assert first.status_code == 201, first.content
    return call(client, data={"file": again(uploaded)}, fmt="multipart")


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────



#: Operations whose declared body the wire does not send, keyed by
#: ``(METHOD, path, code)`` — this module declares two 2xx codes on six of
#: its operations, and a defect can live on one of them and not the other.
#: An entry names the defect AND its owner, and ``strict=True`` turns a fixed
#: one into a failure until the entry is deleted.
KNOWN_MISMATCHES: dict = {}

_UNOWNED_USERNAME = (
    "`uploaded_by_username` is declared a REQUIRED, non-nullable string and "
    "is ABSENT from the body of every row whose `uploaded_by` is null — the "
    "field is `CharField(source=\"uploaded_by.username\", read_only=True)` "
    "(serializers.py:272-274 Image, 366-368 Video, and the same two lines in "
    "AudioSerializer and FileModelSerializer), and DRF raises SkipField when a "
    "traversed relation is None, which drops the key rather than sending null. "
    "The state is ordinary, not exotic: `cdn.import_from_url` writes "
    "`uploaded_by=None` on purpose (ownership.py:100-109 calls that the "
    "service-owned pool), and a GDPR erasure leaves the same shape behind. "
    "The field DIRECTLY ABOVE it, `uploaded_by`, is declared nullable — so "
    "the same serializer says the null uploader is expected one line up and "
    "then promises a username derived from it. A generated client reads "
    "`image.uploaded_by_username.trim()` and throws on `undefined`. Reachable "
    "through this operation because it filters on type and `is_processed` "
    "only (views.py:1143-1160), never on an owner. OWNER: this module's four "
    "`uploaded_by_username` declarations — a `default=\"\"` or an "
    "`allow_null` would make the claim true; the schema cannot see the "
    "SkipField."
)

#: The same, for the EMPTY-state pass: a defect can live in one state and not
#: the other, and marking both would hide a claim the wire actually keeps.
KNOWN_MISMATCHES_EMPTY = {
    ("GET", V1 + "/images/{image_type}/random/", 200): _UNOWNED_USERNAME,
}


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe. A missing recipe already
    fails loudly; this fails when the MOUNT is wrong, which no per-operation
    check can see, because when the mount is wrong every operation is equally
    and silently unreachable.
    """
    from django.urls import Resolver404, resolve

    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No declared (method, path, code) row is covered by silence, and no
    entry outlives the row it names."""
    declared_rows = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}

    missing = sorted(
        row for row in declared_rows
        if row not in RECIPES and (row[0], row[1]) not in UNDRIVABLE
    )
    assert not missing, (
        "declared 2xx rows with no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )
    stale = sorted(set(RECIPES) - declared_rows)
    assert not stale, (
        "recipes for rows the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    stale_exclusions = sorted(set(UNDRIVABLE) - declared_ops)
    assert not stale_exclusions, (
        f"exclusions for operations the contract no longer declares: {stale_exclusions}"
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    # RECIPES ∪ UNDRIVABLE is EXACTLY the declared set, in both directions.
    covered = {(m, p) for m, p, _c in RECIPES} | set(UNDRIVABLE)
    assert covered == declared_ops, (
        "the covered set and the declared set differ:\n"
        f"  declared and not covered: {sorted(declared_ops - covered)}\n"
        f"  covered and not declared: {sorted(covered - declared_ops)}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.
    """
    exempt = {
        # A 201 IS the emptiest state of every upload here: the row is created
        # by the request, nothing derived from it exists yet, and the second,
        # fuller state is the operation's own 200. Driving a 201 "emptier"
        # would be driving it twice.
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if code == 201
    } | {
        # The 200 is "these bytes are already yours", which by construction
        # requires a row to exist — there is no emptier state of it than the
        # one its own recipe builds.
        ("POST", V1 + "/upload/image/", 200),
        # FileModel's only nullable field is `uploaded_by`, and owner-scoped
        # dedup means this 200 is only ever answered ABOUT THE CALLER'S OWN
        # row — an unowned row is invisible to the lookup that produces it
        # (ownership.py:83-97). There is no state of this operation in which
        # that field is null, so an "empty" drive would be the populated one
        # again.
        ("POST", V1 + "/upload/file/", 200),
    }
    rows = {(m, p, c) for m, p, c, _ in OPERATIONS}
    missing = sorted(rows - set(EMPTY_STATE) - exempt)
    assert not missing, (
        "rows driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )
    stale = sorted(set(EMPTY_STATE) - rows)
    assert not stale, f"empty-state recipes for undeclared rows: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live row and carry its reason and owner."""
    declared_rows = {(m, p, c) for m, p, c, _ in OPERATIONS}
    for table in (KNOWN_MISMATCHES, KNOWN_MISMATCHES_EMPTY):
        for key, reason in table.items():
            assert key in declared_rows, (
                f"{key} is recorded as a known mismatch but the contract no "
                "longer declares it — delete the entry"
            )
            assert reason and reason.strip(), f"{key} is recorded with no reason"
            assert "OWNER:" in reason, (
                f"{key} names a defect but not who owns it — an unowned "
                "finding is a finding nobody fixes"
            )
    for key in KNOWN_MISMATCHES_EMPTY:
        assert key in EMPTY_STATE, (
            f"{key} is recorded as an empty-state mismatch but has no "
            "empty-state recipe to produce it"
        )


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = table.get((method, path, code))
    assert perform is not None, (
        f"{method} {path} -> {code} declares a response body and has no "
        "recipe — an unchecked operation is a schema nobody proves. Teach "
        "RECIPES, or name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a collection must
    # actually carry a row for the check to have looked at anything.
    if expect_rows and isinstance(body, list):
        assert body, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path, code) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path} {code}: {KNOWN_MISMATCHES[(method, path, code)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if (method, path, code) in EMPTY_STATE
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path, code) in KNOWN_MISMATCHES_EMPTY:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"{method} {path} {code}: "
                    f"{KNOWN_MISMATCHES_EMPTY[(method, path, code)]}"
                ),
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_upload_image_stores_a_type_its_own_document_admits():
    """What the two-horned ``/upload/image/`` finding became.

    It used to read: the endpoint declares a 201 and a 200 it cannot answer
    under the emission's config (the view stored a FIXED literal ``"product"``
    and ``ASSET_TYPES`` defaults to ``("avatar",)``), and configuring
    ``"product"`` only moved the lie — the 201 then carried a ``type`` the
    same document's ``TypeEnum`` did not admit. Wrong under both, in opposite
    directions.

    0.23.0 removed the literal: the stored type is read from the same setting
    ``TypeEnum`` is generated from, so the two cannot disagree. This drives it
    under the configuration the CONTRACT WAS EMITTED FROM — no
    ``override_settings``, the shipped defaults — and validates the received
    body against the committed declaration, the claim the old entry denied.

    ``tests/test_asset_types_are_never_frozen.py`` is the general gate; this
    is the specific one, kept in the file where the finding was recorded so
    the history reads straight.
    """
    response = post_file(
        client_for(make_user()), V1 + "/upload/image/", image_upload()
    )

    assert response.status_code == 201, response.content
    body = response.json()

    declared_enum = SCHEMA["components"]["schemas"]["TypeEnum"]["enum"]
    assert body["image"]["type"] in declared_enum, (
        f"stored {body['image']['type']!r}, TypeEnum admits {declared_enum}"
    )

    schema = SCHEMA["paths"]["/cdn/api/v1/upload/image/"]["post"]["responses"]["201"][
        "content"
    ]["application/json"]["schema"]
    errors = [
        error
        for error in _validator(schema).iter_errors(body)
        if list(error.path)[:2] == ["image", "type"]
    ]
    assert errors == [], [error.message for error in errors]


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object, so every one of them must fail. If any passes, the validation
    in ``_drive`` is not reaching the received body and this whole file proves
    nothing.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path, code) not in KNOWN_MISMATCHES
        and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path} {code}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
