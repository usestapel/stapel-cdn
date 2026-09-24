"""``Image.site_key``: the site an upload arrived on, for per-site watermarks.

Expand-only: a new column with an empty default rewrites nothing that exists.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cdn", "0010_video_original_path_width"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="site_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Site (brand key) the upload arrived on; empty when unknown",
                max_length=64,
            ),
        ),
    ]
