"""A host's ref claim reaches the refs table on the default in-process bus.

Listings, profiles and chat claim media through core's ``sync_cdn_refs``,
which publishes on the bus. The shipped default bus is the in-process
MemoryBus, and nothing in this library subscribes to the topic in-process (the
``consume_cdn_events`` command refuses an in-process bus). The claim was
dropped, ``unreferenced_since`` stayed stamped, and ``sweep_unclaimed`` deleted
the photos of a published listing once ``UNCLAIMED_TTL_HOURS`` passed.
"""
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.bus import get_bus
from stapel_core.django.cdn.ref_sync import sync_cdn_refs
from stapel_cdn.models import Image
from stapel_cdn.services import sweep_unclaimed

HASH = "ab" * 32


def _image():
    return Image.objects.create(
        file_hash=HASH,
        original_filename="pic.jpg",
        file_extension=".jpg",
        type="product",
        original_width=10,
        original_height=10,
        original_size=100,
        refs=[],
    )


@pytest.mark.django_db
def test_the_default_bus_is_in_process():
    assert getattr(get_bus(), "in_process", False) is True


@pytest.mark.django_db
def test_a_listing_claim_lands_in_the_refs_table():
    image = _image()
    assert image.unreferenced_since is not None

    result = sync_cdn_refs("listings", "listing", 42, [], [f"product/{HASH}"])

    assert result.ok is True
    image.refresh_from_db()
    assert image.refs == ["listings/listing/42"]
    assert image.unreferenced_since is None


@pytest.mark.django_db
def test_claimed_media_survives_the_unclaimed_sweep():
    image = _image()
    Image.objects.filter(pk=image.pk).update(
        unreferenced_since=timezone.now() - timedelta(hours=49)
    )

    sync_cdn_refs("listings", "listing", 42, [], [f"product/{HASH}"])
    sweep_unclaimed()

    assert Image.objects.filter(pk=image.pk).exists()


@pytest.mark.django_db
def test_a_detach_restamps_the_row():
    image = _image()
    sync_cdn_refs("listings", "listing", 42, [], [f"product/{HASH}"])
    sync_cdn_refs("listings", "listing", 42, [f"product/{HASH}"], [])

    image.refresh_from_db()
    assert image.refs == []
    assert image.unreferenced_since is not None


@pytest.mark.django_db
@override_settings(STAPEL_TOPIC_CDN_REF_SYNC="acme.cdn.refs")
def test_the_consumer_listens_on_the_configured_topic():
    from stapel_cdn.management.commands.consume_cdn_events import Command

    assert Command().get_topics() == ["acme.cdn.refs"]
