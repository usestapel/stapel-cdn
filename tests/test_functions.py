"""
Tests for the comm Function providers (cdn.media_exists, cdn.refs_sync)
called in-process via stapel_core.comm.call.
"""
import pytest
from stapel_core.comm import call, function_registry

from stapel_cdn.models import Image, Video


def _make_image(file_hash, image_type="product"):
    return Image.objects.create(
        file_hash=file_hash,
        original_filename="test.jpg",
        file_extension=".jpg",
        type=image_type,
        original_width=100,
        original_height=100,
        original_size=1000,
    )


class TestRegistration:
    def test_functions_registered(self):
        names = function_registry.names()
        assert "cdn.media_exists" in names
        assert "cdn.refs_sync" in names


@pytest.mark.django_db
class TestMediaExists:
    def test_existing_image_ref(self):
        _make_image("a" * 64)
        result = call("cdn.media_exists", {"ref": f"product/{'a' * 64}"})
        assert result == {"exists": True}

    def test_missing_ref(self):
        result = call("cdn.media_exists", {"ref": f"product/{'0' * 64}"})
        assert result == {"exists": False}

    def test_type_mismatch_does_not_match(self):
        _make_image("b" * 64, image_type="product")
        result = call("cdn.media_exists", {"ref": f"avatar/{'b' * 64}"})
        assert result == {"exists": False}

    def test_video_ref(self):
        Video.objects.create(
            file_hash="c" * 64,
            original_filename="test.mp4",
            file_extension=".mp4",
            original_size=1000,
        )
        result = call("cdn.media_exists", {"ref": f"video/{'c' * 64}"})
        assert result == {"exists": True}


@pytest.mark.django_db
class TestRefsSync:
    def test_add_and_remove_refs(self):
        image = _make_image("d" * 64)
        ref = f"product/{'d' * 64}"

        result = call(
            "cdn.refs_sync",
            {
                "service": "shop",
                "entity_type": "product",
                "entity_id": 42,
                "old_hashes": [],
                "new_hashes": [ref],
            },
        )
        assert result == {"added": 1, "removed": 0, "errors": []}
        image.refresh_from_db()
        assert "shop/product/42" in image.refs

        result = call(
            "cdn.refs_sync",
            {
                "service": "shop",
                "entity_type": "product",
                "entity_id": 42,
                "old_hashes": [ref],
                "new_hashes": [],
            },
        )
        assert result == {"added": 0, "removed": 1, "errors": []}
        image.refresh_from_db()
        assert image.refs == []

    def test_unresolved_ref_reported_as_error(self):
        missing = f"product/{'e' * 64}"
        result = call(
            "cdn.refs_sync",
            {
                "service": "shop",
                "entity_type": "product",
                "entity_id": "7",
                "new_hashes": [missing],
            },
        )
        assert result["added"] == 0
        assert result["errors"] == [missing]
