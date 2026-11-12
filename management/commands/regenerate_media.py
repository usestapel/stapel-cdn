"""
regenerate_media — wipe generated variants and re-process every Image under
the current tier semantics (images-and-cdn.md §6 item 5).

The operational launch step of the tier/branch redesign (alpha policy: no
backward-compatibility file layouts, no data migrations): old variant files
(single-ladder ``{size}.webp`` and legacy ``720.jpg``) are deleted and the
full pipeline runs again, producing min-side thumbnails, w/h preview
branches and the persisted ``variants_meta`` geometry.

Synchronous by design — this is an operator command, not the upload path.

``--previews-only`` re-renders just the preview tiers (what changes when a
watermark is turned on or redesigned): thumbnails, the original and the
inline placeholder are left as they are. ``--protect-originals`` moves the
original of every watermarked image off the public route
(stapel_cdn.protected). ``--assign-site KEY`` stamps rows
with no recorded site first, so photos uploaded before sites were recorded
get that site's watermark. Counts of watermarked renditions are printed
before and after.

Selection narrows by the model's own fields: ``--ids`` (primary keys,
repeatable or comma-separated), ``--uploaded-by`` (owner user id) and
``--ref`` (an entry of ``Image.refs``, ``<service>/<entity_type>/<entity_id>``;
a value ending in ``/`` matches every entry under that prefix). Filters
combine with AND. ``--dry-run`` prints the selection the run would process,
computed by the same function the run uses.
"""
import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Anything the pipeline has ever generated: "<digits>.webp", "<digits>w.webp",
# "<digits>h.webp", legacy "<digits>.jpg".
_VARIANT_FILE_RE = re.compile(r"^\d+[wh]?\.(webp|jpg)$")
# Preview tiers only: "<digits>w.webp" / "<digits>h.webp".
_PREVIEW_FILE_RE = re.compile(r"^\d+[wh]\.webp$")


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
            "--ids",
            action="append",
            default=[],
            metavar="ID[,ID...]",
            help="Only these Image primary keys (repeatable, or comma-separated).",
        )
        parser.add_argument(
            "--uploaded-by",
            dest="uploaded_by",
            default=None,
            metavar="USER_ID",
            help="Only images uploaded by this user id (Image.uploaded_by).",
        )
        parser.add_argument(
            "--ref",
            action="append",
            default=[],
            metavar="SERVICE/ENTITY_TYPE/ENTITY_ID",
            help=(
                "Only images whose refs carry this key (repeatable; any match). "
                "A value ending in '/' matches every key under that prefix, "
                "e.g. 'listings/listing/'."
            ),
        )
        parser.add_argument(
            "--previews-only",
            action="store_true",
            help="Re-render only the preview tiers; thumbnails stay as they are.",
        )
        parser.add_argument(
            "--assign-site",
            default=None,
            help="Set site_key on rows that have none before re-rendering.",
        )
        parser.add_argument(
            "--protect-originals",
            action="store_true",
            help=(
                "Move the original of every image its site watermarks into the "
                "protected tree (off the public media route)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be regenerated without touching anything.",
        )

    def handle(self, *args, **options):
        from stapel_cdn.protected import protect_original
        from stapel_cdn.services import ImageProcessingService
        from stapel_cdn.watermarks import watermark_spec_for

        try:
            qs = select_images(
                image_type=options["image_type"],
                ids=options["ids"],
                uploaded_by=options["uploaded_by"],
                refs=options["ref"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        total = qs.count()
        self.stdout.write(f"regenerate_media: {total} image(s) to process")
        before = _watermark_counts(qs)
        self.stdout.write(
            f"  before: {before[0]} image(s) with watermarked renditions, "
            f"{before[1]} watermarked rendition(s)"
        )

        if options["assign_site"] and not options["dry_run"]:
            stamped = qs.filter(site_key="").update(site_key=options["assign_site"])
            self.stdout.write(f"  assigned site {options['assign_site']!r} to {stamped} row(s)")

        done = 0
        failed = 0
        moved = 0
        for image in qs.iterator():
            label = f"{image.type}/{image.file_hash[:12]} (id={image.pk})"
            if options["dry_run"]:
                self.stdout.write(f"  would regenerate {label}")
                continue
            try:
                if options["protect_originals"] and watermark_spec_for(image):
                    moved += int(protect_original(image))
                if options["previews_only"]:
                    # Overwritten in place: the public URLs never 404 mid-sweep.
                    removed = 0
                    ImageProcessingService.generate_previews_only(image)
                else:
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

        after = _watermark_counts(qs)
        self.stdout.write(
            f"  after: {after[0]} image(s) with watermarked renditions, "
            f"{after[1]} watermarked rendition(s)"
        )
        if options["protect_originals"]:
            self.stdout.write(f"  protected {moved} original(s)")
        summary = f"regenerate_media: {done} regenerated, {failed} failed of {total}"
        if failed:
            self.stderr.write(self.style.ERROR(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    def _remove_variant_files(self, image) -> int:
        """Delete generated variant files, keeping the original upload."""
        from stapel_cdn.protected import CLEAN_DIR, protected_rel_dir
        from stapel_cdn.renditions import image_public_dir

        output_dir = os.path.join(settings.MEDIA_ROOT, image_public_dir(image))
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
        clean_dir = os.path.join(
            settings.MEDIA_ROOT, protected_rel_dir(image), CLEAN_DIR
        )
        if os.path.isdir(clean_dir):
            for name in os.listdir(clean_dir):
                if _PREVIEW_FILE_RE.match(name):
                    os.unlink(os.path.join(clean_dir, name))
                    removed += 1
        return removed


def _parse_ids(values) -> list[int]:
    ids = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                raise ValueError(f"--ids: {part!r} is not an integer id") from None
    return ids


def _ref_matches(entries, wanted) -> bool:
    for entry in entries if isinstance(entries, list) else ():
        entry = str(entry)
        for key in wanted:
            if entry == key or (key.endswith("/") and entry.startswith(key)):
                return True
    return False


def select_images(*, image_type=None, ids=(), uploaded_by=None, refs=()):
    """The Image queryset a run processes — the one selection dry-run and the
    real run both print and walk.

    ``refs`` is matched in Python over the rows that carry any reference at
    all: JSON containment is not portable across the database backends this
    library supports (the same choice ``erasure.erase_entity`` makes).
    """
    from stapel_cdn.models import Image

    qs = Image.objects.all().order_by("pk")
    if image_type:
        qs = qs.filter(type=image_type)
    pks = _parse_ids(ids)
    if ids and not pks:
        raise ValueError("--ids was given but names no id")
    if pks:
        qs = qs.filter(pk__in=pks)
    if uploaded_by not in (None, ""):
        qs = qs.filter(uploaded_by_id=uploaded_by)
    wanted = [str(r).strip() for r in refs or () if str(r).strip()]
    if refs and not wanted:
        raise ValueError("--ref was given but names no key")
    if wanted:
        matching = [
            pk
            for pk, entries in qs.exclude(refs=[]).values_list("pk", "refs")
            if _ref_matches(entries, wanted)
        ]
        qs = qs.filter(pk__in=matching)
    return qs


def _watermark_counts(qs) -> tuple[int, int]:
    """(images with any watermarked rendition, watermarked renditions)."""
    images = renditions = 0
    for meta in qs.values_list("variants_meta", flat=True).iterator():
        marked = sum(1 for entry in (meta or []) if entry.get("watermarked"))
        if marked:
            images += 1
            renditions += marked
    return images, renditions
