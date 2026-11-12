"""regenerate_media selection: --ids, --uploaded-by, --ref, and a dry-run that
prints exactly the rows the real run would process."""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from stapel_core.django.users.models import User

from stapel_cdn.models import Image

pytestmark = pytest.mark.django_db


def _image(n, *, user=None, refs=None, type="product"):
    with patch("stapel_cdn.tasks.process_image_async"):
        return Image.objects.create(
            file_hash=f"{n:x}".rjust(64, "0"),
            original_filename="pic.jpg",
            file_extension=".jpg",
            type=type,
            original_width=10,
            original_height=10,
            original_size=100,
            uploaded_by=user,
            refs=refs or [],
        )


@pytest.fixture
def rows(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    alice = User.objects.create_user(username="alice", email="a@example.com", password="pw-123456")
    bob = User.objects.create_user(username="bob", email="b@example.com", password="pw-123456")
    return {
        "a1": _image(1, user=alice, refs=["listings/listing/7"]),
        "a2": _image(2, user=alice, refs=["profiles/profile/3"]),
        "b1": _image(3, user=bob, refs=["listings/listing/8", "chat/message/1"]),
        "svc": _image(4, refs=[]),
        "alice": alice,
        "bob": bob,
    }


def _run(*args):
    """Run for real with the pipeline stubbed; return (stdout, processed pks)."""
    processed = []
    out = StringIO()
    with patch(
        "stapel_cdn.services.ImageProcessingService.process_image",
        side_effect=lambda image: processed.append(image.pk),
    ):
        call_command("regenerate_media", *args, stdout=out, stderr=StringIO())
    return out.getvalue(), processed


def _dry(*args):
    out = StringIO()
    call_command("regenerate_media", "--dry-run", *args, stdout=out)
    text = out.getvalue()
    return text, sorted(
        int(line.rsplit("id=", 1)[1].rstrip(")"))
        for line in text.splitlines()
        if "would regenerate" in line
    )


@pytest.mark.parametrize(
    "args, expected",
    [
        ((), ["a1", "a2", "b1", "svc"]),
        (("--ids", "1"), ["a1"]),
        (("--ids", "1,3"), ["a1", "b1"]),
        (("--ids", "1", "--ids", "4"), ["a1", "svc"]),
        (("--ref", "listings/listing/8"), ["b1"]),
        (("--ref", "listings/listing/"), ["a1", "b1"]),
        (("--ref", "profiles/profile/3", "--ref", "chat/message/1"), ["a2", "b1"]),
        (("--ref", "listings/listing/7x"), []),
    ],
)
def test_selection_and_dry_run_agree(rows, args, expected):
    pk = {name: rows[name].pk for name in ("a1", "a2", "b1", "svc")}
    # --ids in the table above are positions; map them to real pks.
    mapped = []
    it = iter(args)
    order = ["a1", "a2", "b1", "svc"]
    for arg in it:
        mapped.append(arg)
        if arg == "--ids":
            value = next(it)
            mapped.append(",".join(str(pk[order[int(i) - 1]]) for i in value.split(",")))
    want = sorted(pk[name] for name in expected)

    text, listed = _dry(*mapped)
    assert listed == want
    assert f"{len(want)} image(s) to process" in text

    _out, processed = _run(*mapped)
    assert sorted(processed) == want


def test_uploaded_by_filter(rows):
    alice = rows["alice"]
    _text, listed = _dry("--uploaded-by", str(alice.pk))
    assert listed == sorted([rows["a1"].pk, rows["a2"].pk])
    _out, processed = _run("--uploaded-by", str(alice.pk), "--ref", "listings/listing/")
    assert processed == [rows["a1"].pk]


def test_filters_combine_with_type(rows):
    _image(5, user=rows["alice"], refs=["listings/listing/7"], type="avatar")
    _text, listed = _dry("--type", "product", "--ref", "listings/listing/7")
    assert listed == [rows["a1"].pk]


@pytest.mark.parametrize("args", [("--ids", "x"), ("--ids", ","), ("--ref", " ")])
def test_a_filter_that_names_nothing_is_refused(rows, args):
    with pytest.raises(CommandError):
        call_command("regenerate_media", "--dry-run", *args, stdout=StringIO())
