"""
Kafka consumer for CDN ref-sync events.

Receives cdn.ref.sync events published by other services via sync_cdn_refs()
and persists the ref changes to CDN's Image/Video/File tables.

If CDN is temporarily down, events accumulate in Kafka and are processed
in order when the consumer restarts — no data loss.
"""

import logging

from stapel_core.bus import BaseBusConsumerCommand, Event
from stapel_core.django.cdn.ref_sync import TOPIC_CDN_REF_SYNC

logger = logging.getLogger(__name__)


class Command(BaseBusConsumerCommand):
    help = "Consume CDN ref-sync events from Kafka and persist ref changes"

    topics = [TOPIC_CDN_REF_SYNC]
    consumer_group = "cdn-ref-sync"

    def handle_event(self, event: Event) -> None:
        if event.event_type != "cdn.ref.sync":
            return

        p = event.payload
        service = p.get("service")
        entity_type = p.get("entity_type")
        entity_id = p.get("entity_id")

        if not service or not entity_type or not entity_id:
            logger.warning("cdn.ref.sync: missing fields in payload: %s", p)
            return

        from stapel_cdn.services import apply_ref_sync

        result = apply_ref_sync(
            service=service,
            entity_type=entity_type,
            entity_id=entity_id,
            old_hashes=p.get("old_hashes", []),
            new_hashes=p.get("new_hashes", []),
        )

        self.stdout.write(
            f"cdn.ref.sync {service}/{entity_type}/{entity_id}: "
            f"+{result['added']} -{result['removed']}"
            + (f" errors={result['errors']}" if result["errors"] else "")
        )
