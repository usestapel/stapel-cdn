"""Stamp render metadata on media stored before the metadata pipeline.

    python manage.py cdn_backfill_media_meta [--kind image|video|audio]
        [--batch-size N] [--limit N] [--dry-run] [--retry-degraded]

Every upload since 0.16.0 gets its inline preview (blur-up / poster frame /
waveform strip), measured dimensions and duration at ingest. This is the
pass over everything stored before that — and, with ``--retry-degraded``,
over everything a deployment stored while it was still missing ffmpeg.

Idempotent and resumable by construction: only rows still missing a preview
are candidates, paged by a primary-key cursor, so a crash halfway is
resumed by re-running the same command and a second full run over a
finished table writes nothing. A row that cannot be completed records a
NAMED reason (``meta_reason``) and is counted, not retried in a loop and not
reported as success.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Backfill render metadata (inline preview, dimensions, duration) for "
        "media stored before the metadata pipeline existed."
    )

    def add_arguments(self, parser):
        from ...backfill import POPULATIONS

        parser.add_argument(
            "--kind",
            action="append",
            choices=list(POPULATIONS),
            help=(
                "Population to walk; repeatable. Default: all of "
                f"{', '.join(POPULATIONS)}. Documents carry no derived "
                "metadata (mime and extension are read straight off the row) "
                "and are therefore not a population."
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Rows fetched per query (default 200).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Stop after this many candidates per population — for "
                "draining a large table in bounded slices."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count candidates and report, write nothing.",
        )
        parser.add_argument(
            "--retry-degraded",
            action="store_true",
            help=(
                "Also re-attempt rows that already failed with a named "
                "reason — what to run after installing ffmpeg on a "
                "deployment that stored media without it."
            ),
        )

    def handle(self, *args, **options):
        from ...backfill import POPULATIONS, backfill_media_metadata

        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be >= 1")
        if options["limit"] is not None and options["limit"] < 1:
            raise CommandError("--limit must be >= 1")

        populations = tuple(options["kind"] or POPULATIONS)
        result = backfill_media_metadata(
            populations=populations,
            batch_size=options["batch_size"],
            limit=options["limit"],
            dry_run=options["dry_run"],
            retry_degraded=options["retry_degraded"],
        )

        verb = "would stamp" if options["dry_run"] else "stamped"
        degraded_total = 0
        for population, stats in result.items():
            degraded_total += stats["degraded"]
            line = (
                f"cdn_backfill_media_meta [{population}]: "
                f"{stats['candidates']} candidate(s), {verb} "
                f"{stats['stamped']}, {stats['degraded']} degraded, "
                f"{stats['failed']} failed."
            )
            if stats["reasons"]:
                named = ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(stats["reasons"].items())
                )
                line += f" Reasons: {named}."
            self.stdout.write(line)

        if degraded_total and not options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "Some rows could not be completed — the reasons above are "
                    "the reasons, and every one of those rows now carries it "
                    "in meta_reason (cdn.describe reports it as "
                    "meta_status/meta_reason rather than an unexplained "
                    "null). 'ffmpeg_missing'/'ffprobe_missing' means this "
                    "host has no media tool: install ffmpeg and re-run with "
                    "--retry-degraded. 'source_missing' means the stored blob "
                    "is gone — that one no re-run fixes."
                )
            )
