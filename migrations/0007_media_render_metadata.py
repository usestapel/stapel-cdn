"""Render metadata: inline previews and the named reason one is missing.

Pure expand — seven additive columns and one help_text change, all with
defaults, no data migration and nothing dropped. Existing rows read as
"never generated" (``preview_b64 == ""``, ``meta_reason == ""``), which is
exactly the candidate set ``manage.py cdn_backfill_media_meta`` walks, so
the backfill needs no separate marker column to know where it stands.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cdn', '0006_image_variants_ready_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='audio',
            name='meta_reason',
            field=models.CharField(blank=True, default='', help_text='Named reason the render metadata is incomplete (stapel_cdn.metadata.REASONS); empty when nothing degraded.', max_length=32),
        ),
        migrations.AddField(
            model_name='audio',
            name='preview_b64',
            field=models.TextField(blank=True, default='', help_text='Inline waveform strip: data:image/webp;base64,...'),
        ),
        migrations.AddField(
            model_name='image',
            name='meta_reason',
            field=models.CharField(blank=True, default='', help_text='Named reason the render metadata is incomplete (stapel_cdn.metadata.REASONS); empty when nothing degraded.', max_length=32),
        ),
        migrations.AddField(
            model_name='image',
            name='preview_b64',
            field=models.TextField(blank=True, default='', help_text='Inline blur-up placeholder: data:image/webp;base64,...'),
        ),
        migrations.AddField(
            model_name='video',
            name='has_poster',
            field=models.BooleanField(default=False, help_text='Whether the poster frame file has been written'),
        ),
        migrations.AddField(
            model_name='video',
            name='meta_reason',
            field=models.CharField(blank=True, default='', help_text='Named reason the render metadata is incomplete (stapel_cdn.metadata.REASONS); empty when nothing degraded.', max_length=32),
        ),
        migrations.AddField(
            model_name='video',
            name='preview_b64',
            field=models.TextField(blank=True, default='', help_text='Inline poster placeholder: data:image/webp;base64,...'),
        ),
        migrations.AlterField(
            model_name='video',
            name='is_processed',
            field=models.BooleanField(default=False, help_text='Whether metadata/variants have been generated'),
        ),
    ]
