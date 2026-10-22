"""The ref-sync consumer, on both transports.

``stapel_core.django.cdn.ref_sync.sync_cdn_refs`` publishes ``cdn.ref.sync`` on
the bus. On a broker the ``consume_cdn_events`` command reads it; on an
in-process bus no command can run, so ``apps.ready()`` subscribes the same
function with core's ``subscribe_in_process`` (it survives ``reset_bus()`` and
``MemoryBus.clear()``, and a broker ignores it). One function, so the two
paths cannot drift.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def apply_ref_sync_event(event) -> dict | None:
    """Apply one ``cdn.ref.sync`` envelope; None when it is not one."""
    if event.event_type != "cdn.ref.sync":
        return None
    p = event.payload or {}
    service = p.get("service")
    entity_type = p.get("entity_type")
    entity_id = p.get("entity_id")
    if not service or not entity_type or not entity_id:
        logger.warning("cdn.ref.sync: missing fields in payload: %s", p)
        return None

    from .services import apply_ref_sync

    result = apply_ref_sync(
        service=service,
        entity_type=entity_type,
        entity_id=entity_id,
        old_hashes=p.get("old_hashes", []),
        new_hashes=p.get("new_hashes", []),
    )
    if result["errors"]:
        logger.warning(
            "cdn.ref.sync %s/%s/%s: unresolved refs %s",
            service, entity_type, entity_id, result["errors"],
        )
    return result


def ensure_in_process_consumer(bus=None) -> bool:
    """Subscribe :func:`apply_ref_sync_event` for an in-process bus.

    Idempotent. Returns True when the topic is delivered in this process.
    """
    from stapel_core.bus import carries_in_process, subscribe_in_process
    from stapel_core.django.cdn.ref_sync import get_ref_sync_topic

    topic = get_ref_sync_topic()
    subscribe_in_process(topic, apply_ref_sync_event)
    return carries_in_process(topic, bus)
