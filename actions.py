"""Action subscriptions of the cdn module.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery).

The GDPR half is one module on purpose: ``gdpr.erasure.requested`` (erase
one subject, receipt what was removed) and ``gdpr.owner.probe`` (answer
``gdpr.owner.alive``) are handled side by side, so an ``alive`` answer is
evidence that the erasure path is *consumed* — not that a container is
deployed. ``user.deleted`` is the pre-0.5.0 account path, now routed
through the same :func:`stapel_cdn.erasure.erase`.
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("gdpr.erasure.requested")
def handle_erasure_requested(event):
    """Erase this module's slice of one subject and receipt what was removed.

    Subjects this owner does not claim are ignored in silence: gdpr opens an
    ``ErasurePart`` only for owners that declared the subject type, so
    answering for a foreign subject would certify an erasure nobody asked
    for. Erasure and receipt are one transaction (outbox discipline) — the
    receipt leaves iff the erasure committed, so a partial purge can never
    complete the request.
    """
    from django.db import transaction

    from stapel_core.comm import emit

    from .erasure import OWNER, SUBJECT_TYPES, erase

    payload = event.payload or {}
    subject_type = payload.get("subject_type")
    subject_key = payload.get("subject_key")
    correlation_id = payload.get("correlation_id")
    if not subject_type or not subject_key or not correlation_id:
        logger.error(
            "malformed gdpr.erasure.requested event: %s",
            getattr(event, "event_id", "?"),
        )
        return
    if subject_type not in SUBJECT_TYPES:
        return

    with transaction.atomic():
        counts = erase(subject_type, subject_key)
        emit(
            "gdpr.section.erased",
            {
                "correlation_id": str(correlation_id),
                "owner": OWNER,
                "subject_type": str(subject_type),
                "subject_key": str(subject_key),
                # Deterministic proof: a redelivery receipts the same id
                # instead of inventing a second erasure in the audit trail.
                "receipt_id": f"{OWNER}:{correlation_id}",
                "counts": counts,
            },
            key=str(subject_key),
        )
    logger.info(
        "cdn erased %s %s for erasure %s: %s",
        subject_type, subject_key, correlation_id, counts,
    )


@on_action("gdpr.owner.probe")
def handle_owner_probe(event):
    """Answer the liveness probe with the subjects this owner claims.

    Deliberately in the same module and process as the erasure handler
    above: ``gdpr.W006`` reads these answers to name owners whose consumer
    was never deployed, and an answer from anywhere else would make that
    check lie.
    """
    from stapel_core.comm import emit

    from .erasure import OWNER, SUBJECT_TYPES

    answer = {"owner": OWNER, "subject_types": list(SUBJECT_TYPES)}
    correlation_id = (event.payload or {}).get("correlation_id")
    if correlation_id:
        answer["correlation_id"] = str(correlation_id)
    emit("gdpr.owner.alive", answer, key=OWNER)


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

    Deprecated by stapel-gdpr 0.5.0 (removed there in 0.6.0): an account
    erasure now also arrives as ``gdpr.erasure.requested``, which carries the
    subject pair this payload cannot express. Kept working for one minor,
    routed through the same :func:`stapel_cdn.erasure.erase`; the receipt
    keeps its 0.4.x shape so a host on the older orchestrator still completes
    (a second receipt for the same correlation_id is idempotent on the gdpr
    side — it lands on the same part).
    """
    from django.db import transaction

    from stapel_core.comm import emit

    from .erasure import OWNER, erase

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    correlation_id = event.payload.get("correlation_id")
    with transaction.atomic():
        erase("account", user_id)
        if correlation_id:
            emit(
                "gdpr.section.erased",
                {
                    "user_id": str(user_id),
                    "correlation_id": str(correlation_id),
                    "service": OWNER,
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
