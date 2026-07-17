# stapel: contract-phase
# variant_720_jpg was never populated by any pipeline (JPEG fallback removed;
# variants are WebP-only) — no code ever read or wrote it, so the contract
# ships immediately (alpha policy: drop & rebuild, no transition periods).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("cdn", "0002_image_variants_meta"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="video",
            name="variant_720_jpg",
        ),
    ]
