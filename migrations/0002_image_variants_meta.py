# Image.variants_meta — per-variant geometry persisted by the processing
# pipeline (images-and-cdn.md §5/§6 п.3). Expand-only: new nullable-equivalent
# JSON column with a list default; existing rows are backfilled operationally
# by `manage.py regenerate_media` (alpha policy — no data migration).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cdn", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="variants_meta",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Generated variants: [{tier, branch, url, width, height}]",
            ),
        ),
    ]
