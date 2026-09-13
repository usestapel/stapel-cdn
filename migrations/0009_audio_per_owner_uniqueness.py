"""Audio joins the per-owner uniqueness 0005 gave every other stored model.

0005 took the GLOBAL unique on ``file_hash`` off Image, Video and File and
replaced it with a partial pair — unique per (hash, owner) for user-owned
rows, unique per hash for the service pool — because owner-scoped dedup means
two principals legitimately hold the same bytes. Audio was not in that
migration: it had no HTTP intake, so nothing could reach the constraint.

0.21.0 gave it one, which turned "two members send the same voice clip" into
an IntegrityError on an ordinary request. Expand-only: a unique index is
dropped and two narrower ones are added, so this widens what the table
accepts and nothing existing violates it.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cdn', '0008_media_unreferenced_since'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='audio',
            name='file_hash',
            field=models.CharField(db_index=True, help_text='SHA-256 hash of the original file', max_length=64),
        ),
        migrations.AddConstraint(
            model_name='audio',
            constraint=models.UniqueConstraint(condition=models.Q(('uploaded_by__isnull', False)), fields=('file_hash', 'uploaded_by'), name='cdn_audio_hash_owner_unique'),
        ),
        migrations.AddConstraint(
            model_name='audio',
            constraint=models.UniqueConstraint(condition=models.Q(('uploaded_by__isnull', True)), fields=('file_hash',), name='cdn_audio_hash_service_unique'),
        ),
    ]
