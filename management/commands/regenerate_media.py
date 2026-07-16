"""
regenerate_media — wipe generated variants and re-process every Image under
the current tier semantics (images-and-cdn.md §6 п.5).

The operational launch step of the tier/branch redesign (alpha policy: no
backward-compatibility file layouts, no data migrations): old variant files
(single-ladder ``{size}.webp`` and legacy ``720.jpg``) are deleted and the
full pipeline runs again, producing min-side thumbnails, w/h preview
branches and the persisted ``variants_meta`` geometry.

Synchronous by design — this is an operator command, not the upload path.
"""
import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand

# Anything the pipeline has ever generated: "<digits>.webp", "<digits>w.webp",
# "<digits>h.webp", legacy "<digits>.jpg".
_VARIANT_FILE_RE = re.compile(r"^\d+[wh]?\.(webp|jpg)$")


class Command(BaseCommand):
    help = (
        "Delete generated image variants and re-run the processing pipeline "
        "under the current tier semantics (min-side thumbnails, w/h preview "
        "branches, variants_meta)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            dest="image_type",
            default=None,
            help="Only regenerate images of this type (e.g. product, avatar).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be regenerated without touching anything.",
        )

    def handle(self, *args, **options):
        from stapel_cdn.models import Image
        from stapel_cdn.services import ImageProcessingService

        qs = Image.objects.all().order_by("pk")
        if options["image_type"]:
            qs = qs.filter(type=options["image_type"])

        total = qs.count()
        self.stdout.write(f"regenerate_media: {total} image(s) to process")

        done = 0
        failed = 0
        for image in qs.iterator():
            label = f"{image.type}/{image.file_hash[:12]} (id={image.pk})"
            if options["dry_run"]:
                self.stdout.write(f"  would regenerate {label}")
                continue
            try:
                removed = self._remove_variant_files(image)
                image.variants_meta = []
                image.is_processed = False
                image.save(update_fields=["variants_meta", "is_processed"])
                ImageProcessingService.process_image(image)
                done += 1
                self.stdout.write(f"  regenerated {label} (removed {removed} old file(s))")
            except Exception as exc:  # keep going: one broken file must not stop the sweep
                failed += 1
                self.stderr.write(f"  FAILED {label}: {exc}")

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"dry-run complete: {total} image(s)"))
            return

        summary = f"regenerate_media: {done} regenerated, {failed} failed of {total}"
        if failed:
            self.stderr.write(self.style.ERROR(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    def _remove_variant_files(self, image) -> int:
        """Delete generated variant files, keeping the original upload."""
        output_dir = os.path.join(settings.MEDIA_ROOT, image.type, image.file_hash)
        if not os.path.isdir(output_dir):
            return 0
        try:
            original_name = os.path.basename(image.original.name or "")
        except Exception:
            original_name = ""
        removed = 0
        for name in os.listdir(output_dir):
            if name == original_name:
                continue
            if _VARIANT_FILE_RE.match(name):
                os.unlink(os.path.join(output_dir, name))
                removed += 1
        return removed
