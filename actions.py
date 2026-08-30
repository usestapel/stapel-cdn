"""Action subscriptions of the cdn module.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery).

The GDPR half is one module on purpose: ``gdpr.erasure.requested`` (erase
one subject, receipt what was removed) and ``gdpr.owner.probe`` (answer
``gdpr.owner.alive``) are handled side by side, so an ``alive`` answer is
evidence that the erasure path is *consumed* — not that a container is
deployed. ``user.deleted`` is the pre-0.5.0 account path, now routed
through the same :func:`stapel_cdn.erasure.erase`.

``user.merged`` is the other half of that account life cycle: a merge
re-parents rows instead of erasing them, and a module that answers only the
deletion strands what the merge was supposed to carry over.
"""
import logging

from django.core.exceptions import ValidationError

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has media to carry over but there is no
    local user row to point ``uploaded_by`` at yet. Raising is the comm
    layer's retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


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


# ── Merge: the other half of the account life cycle ───────────────────


def _owner_dedup_fields(model) -> tuple:
    """Columns that, together with ``uploaded_by``, may not repeat.

    Read off the model's own ``UniqueConstraint`` list rather than restated
    here, so a media model added later cannot get its per-owner constraint
    silently ignored by this handler.
    """
    fields: set = set()
    for constraint in model._meta.constraints:
        names = tuple(getattr(constraint, "fields", ()) or ())
        if "uploaded_by" in names:
            fields.update(name for name in names if name != "uploaded_by")
    return tuple(sorted(fields))


def _fold_refs(keeper, duplicate) -> None:
    """Union the duplicate row's reference keys onto the row that stays.

    The two rows are one blob under two owners, so the references the guest
    accumulated name content the survivor is about to own; dropping them
    would make the object look unreferenced and eligible for the next purge.
    """
    kept = keeper.refs if isinstance(keeper.refs, list) else []
    extra = [
        ref
        for ref in (duplicate.refs if isinstance(duplicate.refs, list) else [])
        if ref not in kept
    ]
    if extra:
        keeper.refs = kept + extra
        keeper.save(update_fields=["refs", "updated_at"])


def _carry_media(model, from_user_id, into_user_id) -> tuple:
    """Re-point one media model from the guest to the survivor.

    Returns ``(moved, folded)``. Dedup here is owner-scoped
    (:mod:`stapel_cdn.ownership`), so the same bytes may be held twice — one
    row each over one content-addressed blob. A blind update would break the
    per-owner uniqueness constraint, so a guest row the survivor already
    matches is folded: its refs move onto the survivor's row and the
    duplicate row is dropped. The blob is never unlinked — the survivor's row
    still points at the same ``<type>/<hash>/`` path.
    """
    folded = 0
    dedup_fields = _owner_dedup_fields(model)
    if dedup_fields:
        held = {
            tuple(values)
            for values in model.objects.filter(
                uploaded_by_id=into_user_id
            ).values_list(*dedup_fields)
        }
        if held:
            for row in model.objects.filter(uploaded_by_id=from_user_id):
                key = tuple(getattr(row, name) for name in dedup_fields)
                if key not in held:
                    continue
                keeper = model.objects.filter(
                    uploaded_by_id=into_user_id, **dict(zip(dedup_fields, key))
                ).first()
                if keeper is None:  # pragma: no cover - read under one transaction
                    continue
                _fold_refs(keeper, row)
                row.delete()
                folded += 1
    moved = model.objects.filter(uploaded_by_id=from_user_id).update(
        uploaded_by_id=into_user_id
    )
    return moved, folded


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away account's stored media over to the survivor.

    Re-points ``uploaded_by`` on every media model this module owns —
    :class:`~stapel_cdn.models.Image`, :class:`~stapel_cdn.models.Video`,
    :class:`~stapel_cdn.models.File` and :class:`~stapel_cdn.models.Audio` —
    from the guest that ceased to exist onto the account that absorbed it.
    Nothing is erased: a merge is the opposite instruction to a deletion.

    Without this handler the loss is silent rather than loud. ``uploaded_by``
    is ``SET_NULL``, so a guest's uploads survive auth's deletion as
    *service-owned* objects: they keep serving, they stop belonging to
    anybody, they never appear in the survivor's listing or quota, and no
    erasure is ever requested for them.

    Two different "unknown id" situations, and conflating them loses data:

    * the guest owns no media here (never uploaded, or a previous delivery
      already moved it all) — a genuine no-op, returned quietly;
    * the guest owns media but the survivor has no user row here yet — NOT a
      no-op. :class:`MergeTargetNotReady` is raised so the event is
      redelivered, because returning success would let the outbox mark it
      delivered and leave the uploads behind an id nobody can sign in as.

    Quota is deliberately not consulted: refusing to carry media over would
    strand it, and a survivor pushed over ``MAX_BYTES_PER_OWNER`` by a merge
    is stopped at their next upload, which is where the ceiling belongs.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .erasure import media_models

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    with transaction.atomic():
        # Both reads and the decision they feed happen inside the transaction
        # and before the first write, so the "not yet" path below can never
        # leave half the media moved.
        try:
            owns_something = any(
                model.objects.filter(uploaded_by_id=from_user_id).exists()
                for model in media_models()
            )
            # The survivor probe is read here, under the same guard, because a
            # malformed *into* id must not escape as a poison pill either.
            survivor_exists = (
                get_user_model().objects.filter(pk=into_user_id).exists()
            )
        except (ValidationError, ValueError, TypeError):
            # Django raises ValidationError (not ValueError) for a malformed
            # UUID; an id that cannot address a row here names nothing, and an
            # escaping exception is a poison pill no redelivery repairs.
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not owns_something:
            # Nothing to carry: the guest never uploaded here, or a previous
            # delivery already moved everything. Quiet by design — this is
            # also the at-least-once idempotency path.
            return
        if not survivor_exists:
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-cdn yet; redeliver once "
                f"its projection has landed"
            )

        moved = folded = 0
        for model in media_models():
            model_moved, model_folded = _carry_media(
                model, from_user_id, into_user_id
            )
            moved += model_moved
            folded += model_folded

    logger.info(
        "user.merged %s -> %s: %s media object(s) carried over, %s folded into "
        "media the survivor already held",
        from_user_id, into_user_id, moved, folded,
    )
