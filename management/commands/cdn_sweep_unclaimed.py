"""Reap media that is zero-ref AND past its unclaimed TTL — bytes and row.

    python manage.py cdn_sweep_unclaimed [--dry-run]

The operator's handle on the same mechanism the beat entry runs
(``stapel_cdn.tasks.sweep_unclaimed``): both wrap
``services.sweep_unclaimed``, so cron-driven deployments and celery-beat
deployments reap with one implementation. The TTL clock is
``unreferenced_since`` — stamped at upload, cleared by a claim, restamped
when the last ref is detached — never ``created_at``, so nothing that is
referenced is ever touched and nothing recently detached goes before its
grace window (``STAPEL_CDN["UNCLAIMED_TTL_HOURS"]``) has passed.

Idempotent: a second run over a swept table removes nothing.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Delete media that nothing references and whose unclaimed TTL "
        "(STAPEL_CDN['UNCLAIMED_TTL_HOURS']) has expired — bytes and row."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be reaped and delete nothing.",
        )

    def handle(self, *args, **options):
        from ...services import sweep_unclaimed

        report = sweep_unclaimed(dry_run=options["dry_run"])
        verb = "would reap" if options["dry_run"] else "reaped"
        self.stdout.write(
            f"{verb} {report['candidates'] if options['dry_run'] else report['objects_removed']} "
            f"object(s), {report['blobs_unlinked']} blob(s) unlinked, "
            f"{report['stranded']} stranded"
        )
