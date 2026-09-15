"""``Video.original`` gets the 500 characters its siblings already had.

``video_upload_path`` builds ``video/<64-character sha256>/<filename>``: 71
characters before the filename is considered, against a column that was
``FileField(max_length=100)`` — Django's default, never revisited. That left
**29 characters for a client-chosen name**, and
``holiday-video-from-summer.mp4`` is exactly 29.

What happened past 29 was MEASURED rather than assumed, because the answer
turned out not to be the obvious one. ``Video.original`` is the one stored
field here with no ``storage=`` argument, so it gets Django's
``FileSystemStorage``, whose ``get_available_name`` trims an over-long path to
the column and appends a random suffix. A 34-character name was therefore not
a 500 — it was stored as ``holiday-video-fro_bMgrNZQ.mp4``. Silent, and worse
in one respect than an error: the name a person uploaded stopped being the
name the system held, on ordinary filenames, with nothing logged.

(Its siblings use ``OverwriteStorage``, which overrode ``get_available_name``
to return the name unchanged and so dropped that trim. For them an over-long
path really did reach Postgres and really was a 500. Both halves are fixed:
the storage honours ``max_length`` again, and ``upload_to`` fits the path
before it gets there.)

``Image``, ``File`` and ``Audio`` all declare 500 for the same shape. This is
the one that was missed, not a new decision.

Expand-only: widening a ``varchar`` rewrites no rows and nothing existing can
violate the wider bound. Reversing it would fail on any row already storing a
path longer than 100 characters — exactly the rows this migration exists to
allow — so the reverse is left to Django's default column swap and should not
be run once real uploads have landed.
"""

import stapel_cdn.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stapel_cdn", "0009_audio_per_owner_uniqueness"),
    ]

    operations = [
        migrations.AlterField(
            model_name="video",
            name="original",
            field=models.FileField(
                help_text="Original uploaded video",
                max_length=500,
                upload_to=stapel_cdn.models.video_upload_path,
            ),
        ),
    ]
