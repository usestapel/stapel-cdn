"""Action subscriptions of the cdn module.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery).
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed.

    When the ``user.deleted`` payload carries a ``correlation_id`` (the
    gdpr orchestrator's remote-deletion protocol), the erasure is confirmed
    with a ``gdpr.section.erased`` action for the ``media`` section —
    without it the orchestrator's AccountDeletionPart for this service
    never completes and the closure is stuck DELETING forever. Erasure and
    confirmation are one transaction (outbox discipline): the event leaves
    iff the erasure committed.
    """
    from django.db import transaction

    from stapel_core.comm import emit

    from .gdpr import CDNGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    correlation_id = event.payload.get("correlation_id")
    with transaction.atomic():
        CDNGDPRProvider().delete(user_id)
        if correlation_id:
            emit(
                "gdpr.section.erased",
                {
                    "user_id": str(user_id),
                    "correlation_id": str(correlation_id),
                    "service": CDNGDPRProvider.section,
                },
                key=str(user_id),
            )
    logger.info("cdn data erased for deleted user %s", user_id)


@on_action("user.deletion_initiated")
def handle_user_deletion_initiated(event):
    """Account-closure grace period started: purge the user's orphan media.

    Grace is cancellable (gdpr ``cancel_closure``), so — following the
    platform precedent (stapel-notifications: soft actions at grace start,
    "full erasure stays on ``user.deleted``") — this handler touches only
    what the platform provably does not use: media with ``refs == []``,
    which nothing serves. Referenced media keeps serving and keeps its
    ownership link until ``user.deleted``, so a cancelled closure loses
    nothing anyone could see. Idempotent (a redelivery finds no orphans
    left and removes nothing).
    """
    from django.db import transaction

    from .gdpr import CDNGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error(
            "user.deletion_initiated event without user_id: %s", event.event_id
        )
        return
    with transaction.atomic():
        removed = CDNGDPRProvider().purge_unreferenced(user_id)
    logger.info(
        "purged %d unreferenced media object(s) for user %s (deletion grace period)",
        removed, user_id,
    )
