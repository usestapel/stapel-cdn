"""
retype_images — move images a consumer claims from one asset type to another.

For images stored under the wrong type (e.g. listing photos uploaded through
the generic intake when it still guessed the first ``ASSET_TYPES`` entry)::

    manage.py retype_images --from avatar --to product \\
        --claimed-by listings/listing [--site-key KEY] [--dry-run]

Only rows of type ``--from`` that carry at least one ref starting with
``--claimed-by`` are touched. Per row:

* claimed by nothing else, and no ``--to`` row for the same owner and bytes:
  the row itself is retyped. Its original is copied to the new type's path
  (the protected tree when the new type is watermarked), every rendition is
  rendered again, and the old files are removed once no other row of the old
  type holds those bytes;
* also claimed by someone else (a profile using the same picture): the
  claimed-by refs move to a ``--to`` copy of the row, and the row keeps its
  other refs untouched;
* a ``--to`` row for the same owner and bytes already exists: the claimed-by
  refs move onto it.

``--site-key`` is stamped on rows (or copies) with no recorded site, so they
get that site's watermark. The consumer then rewrites its own references from
``<from>/<hash>`` to ``<to>/<hash>``; until it does, those references resolve
to nothing on the new rows' ref sync (logged, harmless).
"""
import os
import shutil

from django.conf import settings
from django.core.files import File as DjangoFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Move images claimed by one consumer from one asset type to another."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="from_type", required=True)
        parser.add_argument("--to", dest="to_type", required=True)
        parser.add_argument(
            "--claimed-by",
            required=True,
            help="Ref prefix of the consumer, e.g. 'listings/listing'.",
        )
        parser.add_argument(
            "--site-key",
            default=None,
            help="Site key for rows that have none (selects the watermark).",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from stapel_cdn.models import Image, get_image_type_choices

        src, dst = options["from_type"], options["to_type"]
        valid = {value for value, _ in get_image_type_choices()}
        for name in (src, dst):
            if name not in valid:
                raise CommandError(f"{name!r} is not in STAPEL_CDN['ASSET_TYPES']")
        if src == dst:
            raise CommandError("--from and --to are the same type")
        prefix = options["claimed_by"].rstrip("/") + "/"

        rows = [
            image
            for image in Image.objects.filter(type=src).order_by("pk")
            if any(str(ref).startswith(prefix) for ref in (image.refs or []))
        ]
        plan = {"retype": 0, "copy": 0, "merge": 0}
        for image in rows:
            plan[self._mode(image, dst, prefix)] += 1
        self.stdout.write(
            f"retype_images: {len(rows)} {src} image(s) claimed by {prefix}* "
            f"-> {dst}: {plan['retype']} retype in place, {plan['copy']} copy "
            f"(also claimed elsewhere), {plan['merge']} merge into an existing {dst} row"
        )
        if options["dry_run"]:
            for image in rows:
                self.stdout.write(
                    f"  would {self._mode(image, dst, prefix)} {src}/{image.file_hash} "
                    f"(id={image.pk}, site={image.site_key or '-'}, refs={image.refs})"
                )
            self.stdout.write(self.style.SUCCESS("dry-run: nothing changed"))
            return

        done = failed = 0
        for image in rows:
            try:
                mode = self._mode(image, dst, prefix)
                target = self._apply(image, mode, src, dst, prefix, options["site_key"])
                done += 1
                self.stdout.write(
                    f"  {mode} {src}/{image.file_hash[:12]} -> {dst} (id={target.pk}, "
                    f"site={target.site_key or '-'}, original={target.original.name})"
                )
            except Exception as exc:  # one broken file must not stop the sweep
                failed += 1
                self.stderr.write(f"  FAILED {src}/{image.file_hash[:12]} (id={image.pk}): {exc}")
        summary = f"retype_images: {done} moved, {failed} failed of {len(rows)}"
        if failed:
            self.stderr.write(self.style.ERROR(summary))
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))

    @staticmethod
    def _existing(image, dst):
        from stapel_cdn.models import Image

        return (
            Image.objects.filter(
                type=dst, file_hash=image.file_hash, uploaded_by=image.uploaded_by_id
            )
            .exclude(pk=image.pk)
            .first()
            if image.uploaded_by_id is not None
            else Image.objects.filter(
                type=dst, file_hash=image.file_hash, uploaded_by__isnull=True
            ).first()
        )

    def _mode(self, image, dst, prefix):
        if self._existing(image, dst) is not None:
            return "merge"
        others = [ref for ref in (image.refs or []) if not str(ref).startswith(prefix)]
        return "copy" if others else "retype"

    def _apply(self, image, mode, src, dst, prefix, site_key):
        from stapel_cdn.services import ImageProcessingService

        claimed = [ref for ref in image.refs if str(ref).startswith(prefix)]
        kept = [ref for ref in image.refs if not str(ref).startswith(prefix)]

        if mode == "merge":
            target = self._existing(image, dst)
            with transaction.atomic():
                target.refs = list(dict.fromkeys([*(target.refs or []), *claimed]))
                target.unreferenced_since = None
                target.save(update_fields=["refs", "unreferenced_since", "updated_at"])
                self._drop_refs(image, kept)
            return target

        old_dir = os.path.join(settings.MEDIA_ROOT, src, image.file_hash)
        source_path = image.original.path
        basename = os.path.basename(image.original.name)

        if mode == "copy":
            target = type(image)(
                file_hash=image.file_hash,
                original_filename=image.original_filename,
                file_extension=image.file_extension,
                type=dst,
                original_size=image.original_size,
                original_width=image.original_width,
                original_height=image.original_height,
                site_key=image.site_key or site_key or "",
                refs=claimed,
                unreferenced_since=None,
                uploaded_by_id=image.uploaded_by_id,
                is_processed=True,  # rendered synchronously below
            )
            with open(source_path, "rb") as handle:
                target.original.save(basename, DjangoFile(handle), save=False)
            with transaction.atomic():
                target.save()
                self._drop_refs(image, kept)
        else:
            target = image
            target.type = dst
            if not target.site_key and site_key:
                target.site_key = site_key
            with open(source_path, "rb") as handle:
                # upload_to reads the new type (and its watermark) for the path.
                target.original.save(basename, DjangoFile(handle), save=False)
            target.variants_meta = []
            target.is_processed = False
            target.save()

        ImageProcessingService.process_image(target)

        if mode == "retype" and not type(image).objects.filter(
            type=src, file_hash=image.file_hash
        ).exists():
            # No row of the old type holds these bytes any more.
            if os.path.isdir(old_dir):
                shutil.rmtree(old_dir, ignore_errors=True)
            from stapel_cdn.protected import protected_prefix

            old_protected = os.path.join(
                settings.MEDIA_ROOT, f"{protected_prefix()}{src}", image.file_hash
            )
            if os.path.isdir(old_protected):
                shutil.rmtree(old_protected, ignore_errors=True)
        return target

    @staticmethod
    def _drop_refs(image, kept):
        image.refs = kept
        fields = ["refs", "updated_at"]
        if not kept:
            image.unreferenced_since = timezone.now()
            fields.append("unreferenced_since")
        image.save(update_fields=fields)
