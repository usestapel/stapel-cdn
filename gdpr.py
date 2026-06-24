import json
from pathlib import Path

from stapel_core.gdpr import GDPRProvider


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

    def delete(self, user_id: int) -> None:
        from .models import File, Image, Video

        for model in (Image, Video, File):
            # Only delete files that have no refs from other content
            for obj in model.objects.filter(uploaded_by_id=user_id):
                refs = obj.refs if isinstance(obj.refs, list) else []
                if not refs:
                    try:
                        obj.original.delete(save=False)
                    except Exception:
                        pass
                    obj.delete()
                else:
                    # File still referenced — anonymise ownership only
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
