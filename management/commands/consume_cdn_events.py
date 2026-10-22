"""
Kafka consumer for CDN ref-sync events.

Receives cdn.ref.sync events published by other services via sync_cdn_refs()
and persists the ref changes to CDN's Image/Video/File tables.

If CDN is temporarily down, events accumulate in Kafka and are processed
in order when the consumer restarts — no data loss.
"""

from stapel_core.bus import BaseBusConsumerCommand, Event
from stapel_core.django.cdn.ref_sync import TOPIC_CDN_REF_SYNC, get_ref_sync_topic


class Command(BaseBusConsumerCommand):
    help = "Consume CDN ref-sync events from Kafka and persist ref changes"

    topics = [TOPIC_CDN_REF_SYNC]
    consumer_group = "cdn-ref-sync"

    def get_topics(self) -> list[str]:
        # The producer reads STAPEL_TOPIC_CDN_REF_SYNC; so does the consumer.
        return [get_ref_sync_topic()]

    def handle_event(self, event: Event) -> None:
        from stapel_cdn.intake import apply_ref_sync_event

        result = apply_ref_sync_event(event)
        if result is None:
            return
        p = event.payload
        self.stdout.write(
            f"cdn.ref.sync {p['service']}/{p['entity_type']}/{p['entity_id']}: "
            f"+{result['added']} -{result['removed']}"
            + (f" errors={result['errors']}" if result["errors"] else "")
        )
