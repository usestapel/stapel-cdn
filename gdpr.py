import json
import logging
from pathlib import Path

from stapel_core.gdpr import GDPRProvider

logger = logging.getLogger(__name__)


class MediaErasureIncomplete(RuntimeError):
    """Bytes this module was asked to erase are still on disk.

    Raised instead of returning normally, because a normal return from
    :meth:`CDNGDPRProvider.delete` IS the receipt: stapel-gdpr's orchestrator
    reads it as "this section is erased" and lets the closure flip to
    ``DELETED``. Reporting an erasure that did not happen is worse than
    failing it — the row is what makes the file findable, and once the row is
    gone nobody can ever locate the personal data left behind.

    The caller (``actions.handle_user_deleted``) runs erasure and confirmation
    in one transaction, so this rolls the confirmation back with it and the
    at-least-once delivery retries. Erasure is idempotent: a retry removes
    whatever the previous attempt managed to remove.
    """


class CDNGDPRProvider(GDPRProvider):
    section = 'media'

    def export(self, user_id: int) -> dict:
        from .models import File, Image, Video

        images = list(Image.objects.filter(uploaded_by_id=user_id).values(
            'original_filename', 'file_extension', 'type',
            'original_width', 'original_height', 'original_size', 'created_at',
        ))
        videos = list(Video.objects.filter(uploaded_by_id=user_id).values(
            'original_filename', 'file_extension',
            'original_width', 'original_height', 'original_size', 'duration', 'created_at',
        ))
        files = list(File.objects.filter(uploaded_by_id=user_id).values(
            'original_filename', 'file_extension', 'mime_type', 'original_size', 'created_at',
        ))
        return {
            'images': _serialize_dates(images),
            'videos': _serialize_dates(videos),
            'files':  _serialize_dates(files),
        }

    def export_to_staging(self, user_id: int, staging_dir: Path) -> list[Path]:
        """Export metadata JSON + copy original binary files."""
        from .models import File, Image, Video

        import shutil

        staging_dir.mkdir(parents=True, exist_ok=True)

        metadata: dict = {'images': [], 'videos': [], 'files': []}
        written: list[Path] = []

        for qs, key in [
            (Image.objects.filter(uploaded_by_id=user_id), 'images'),
            (Video.objects.filter(uploaded_by_id=user_id), 'videos'),
            (File.objects.filter(uploaded_by_id=user_id), 'files'),
        ]:
            for obj in qs:
                try:
                    if obj.original and obj.original.name:
                        dest = staging_dir / obj.original_filename
                        shutil.copy2(obj.original.path, dest)
                        written.append(dest)
                        metadata[key].append({
                            'filename': obj.original_filename,
                            'size':     obj.original_size,
                            'created_at': obj.created_at.isoformat(),
                        })
                except (FileNotFoundError, ValueError):
                    pass

        meta_file = staging_dir / 'media_index.json'
        meta_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8',
        )
        written.append(meta_file)
        return written

    def purge_unreferenced(self, user_id: int) -> int:
        """Delete the user's media that nothing references (``refs == []``):
        binary + row. The grace-safe subset of :meth:`delete` — orphan
        uploads are served by nothing, so removing them is invisible to the
        rest of the platform. Idempotent. Returns the number of objects
        removed.

        Raises :class:`MediaErasureIncomplete` when a blob could not be
        unlinked. The unlink used to sit under ``except Exception: pass``,
        after which the row was deleted anyway and the object counted as
        removed — a fail-open in the one path whose entire contract is
        provable erasure, and an unrecoverable one: the row is the only
        record of where the file is, so deleting it turns a failed erasure
        into personal data nobody can ever find again. A blob that survives
        now keeps its row, and the failure is raised rather than counted.
        """
        from .models import File, Image, Video
        from .ownership import shared_binary_exists

        removed = 0
        stranded: list[str] = []
        for model in (Image, Video, File):
            # Only delete files that have no refs from other content
            for obj in model.objects.filter(uploaded_by_id=user_id):
                refs = obj.refs if isinstance(obj.refs, list) else []
                if not refs:
                    # Storage is content-addressed: `<type>/<hash>/` is shared
                    # by every principal holding the same bytes. The row is
                    # this user's and always goes; the blob is only unlinked
                    # once nothing else points at it, or erasing one holder
                    # would blank an object another one is still serving.
                    if not shared_binary_exists(obj):
                        try:
                            obj.original.delete(save=False)
                        except Exception as exc:
                            logger.error(
                                "erasure incomplete: could not unlink %s %s "
                                "(%s) for user %s — the row is kept so the "
                                "file stays findable: %s",
                                model.__name__, obj.pk, obj.original.name,
                                user_id, exc,
                            )
                            stranded.append(f"{model.__name__}:{obj.pk}")
                            continue
                    obj.delete()
                    removed += 1

        if stranded:
            raise MediaErasureIncomplete(
                f"{len(stranded)} media object(s) for user {user_id} still "
                f"hold their bytes on disk ({', '.join(stranded)}); their "
                f"rows were kept so the files remain findable. This section "
                f"is NOT erased."
            )
        return removed

    def delete(self, user_id: int) -> None:
        """Erase this user's media, or raise.

        Returning normally is the receipt stapel-gdpr's orchestrator acts on,
        so it must mean the bytes are gone. If :meth:`purge_unreferenced`
        could not unlink something, that exception propagates *before* the
        anonymisation pass below — which would otherwise strip
        ``uploaded_by`` off objects whose bytes are still on disk, destroying
        the last link between the file and the person it belongs to.
        """
        from .models import File, Image, Video

        self.purge_unreferenced(user_id)
        for model in (Image, Video, File):
            # Files still referenced by other content — anonymise ownership only
            for obj in model.objects.filter(uploaded_by_id=user_id):
                obj.uploaded_by = None
                obj.save(update_fields=['uploaded_by'])

    def anonymize(self, user_id: int) -> None:
        # Handled in delete() — files still referenced lose uploaded_by link.
        pass


def _serialize_dates(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in row.items()}
        for row in rows
    ]
