"""Stamp render metadata on objects stored before the pipeline existed.

The one-time (or, in practice, repeatedly re-run) pass behind
``manage.py cdn_backfill_media_meta``. Everything a fresh upload gets at
ingest — the inline blur-up/poster/waveform, measured dimensions and
duration — this walks the existing tables and produces for rows that predate
it.

Idempotent and resumable by construction
----------------------------------------
The candidate query is the resume token. Only rows **missing** the thing
this pass writes are selected (``preview_b64 == ""``), and a row is skipped
the moment it has one, so:

* a crash halfway leaves the finished rows finished — the re-run picks up
  exactly what the crash left, with no bookmark file, no cursor table and no
  ``--start-from``;
* a second full run over a finished table is a no-op that writes nothing;
* two operators running it at once do redundant work, not wrong work.

``--retry-degraded`` widens the candidate set to rows that were attempted
and came back with a named reason (``meta_reason != ""``) — which is the
normal path after installing ffmpeg on a deployment that stored a month of
voice messages without it. It is opt-in because re-attempting a row that
failed for a permanent reason (``source_missing``) costs a subprocess per
object and changes nothing.

Bounded by construction too: ``--limit`` stops after N candidates per
population and ``--batch-size`` chunks the queryset, so a table with a
million rows is drained in slices instead of one transaction.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Populations this pass can walk, in the order it walks them.
POPULATIONS = ("image", "video", "audio")


def _candidates(population: str, retry_degraded: bool):
    from .models import Audio, Image, Video

    model = {"image": Image, "video": Video, "audio": Audio}[population]
    queryset = model.objects.all().order_by("pk")
    if retry_degraded:
        # Everything without a usable preview, whether or not it was tried.
        return queryset.filter(preview_b64="")
    # Never attempted: no preview AND no recorded reason for not having one.
    return queryset.filter(preview_b64="", meta_reason="")


def _stamp(population: str, obj) -> tuple[bool, str]:
    """Run the ingest pass for one object. Returns ``(stamped, reason)``."""
    from .services import (
        AudioProcessingService,
        ImageProcessingService,
        VideoProcessingService,
    )

    if population == "image":
        # The full thumbnail pass: it is what produces the micro tier, and
        # the micro tier is what preview_b64 is. Re-running it also repairs
        # a row whose variant files were lost.
        ImageProcessingService.generate_thumbnails_only(obj)
        obj.refresh_from_db(fields=["preview_b64", "meta_reason"])
    elif population == "video":
        VideoProcessingService.process_video(obj)
    else:
        AudioProcessingService.extract_metadata(obj)
    return bool(obj.preview_b64), (obj.meta_reason or "")


def backfill_media_metadata(
    populations=POPULATIONS,
    batch_size: int = 200,
    limit: int | None = None,
    dry_run: bool = False,
    retry_degraded: bool = False,
) -> dict:
    """Stamp missing render metadata. Returns per-population statistics.

    ``{population: {"candidates": int, "stamped": int, "degraded": int,
    "failed": int, "reasons": {reason: count}}}``. A row that degrades with
    a named reason is **not** an error: it is recorded, counted and left
    for a later run (the reason is usually a binary the deployment does not
    have yet). Only an unexpected exception counts as ``failed``, and it
    never stops the pass — one unreadable file must not strand the other
    999 999 rows.
    """
    stats = {}
    for population in populations:
        queryset = _candidates(population, retry_degraded)
        total = queryset.count()
        result = {
            "candidates": total if limit is None else min(total, limit),
            "stamped": 0,
            "degraded": 0,
            "failed": 0,
            "reasons": {},
        }
        stats[population] = result
        if dry_run or not result["candidates"]:
            continue

        seen = 0
        cursor = 0
        # Paged by a primary-key cursor, not by OFFSET: rows this pass
        # stamps drop out of the candidate query as it runs, so an offset
        # would skip whatever slid into the window. The cursor also
        # guarantees a row degrading for a permanent reason is visited once
        # per run instead of being handed back forever under
        # --retry-degraded.
        while seen < result["candidates"]:
            remaining = result["candidates"] - seen
            chunk = list(
                _candidates(population, retry_degraded).filter(pk__gt=cursor)[
                    : min(batch_size, remaining)
                ]
            )
            if not chunk:
                break
            cursor = chunk[-1].pk
            for obj in chunk:
                seen += 1
                try:
                    stamped, reason = _stamp(population, obj)
                except Exception as exc:  # one bad file must not stop the pass
                    result["failed"] += 1
                    logger.warning(
                        "cdn_backfill_media_meta: %s %s failed: %s",
                        population, getattr(obj, "file_hash", obj.pk), exc,
                    )
                    continue
                if stamped:
                    result["stamped"] += 1
                else:
                    result["degraded"] += 1
                    key = reason or "unknown"
                    result["reasons"][key] = result["reasons"].get(key, 0) + 1
    return stats


__all__ = ["POPULATIONS", "backfill_media_metadata"]
