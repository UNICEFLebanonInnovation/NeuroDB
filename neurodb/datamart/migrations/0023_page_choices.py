"""The filter choices the jobs store for a page's frame (``PageChoices``): a new, empty table of a row per page.
CreateModel only, no code runs; the next eTools job (or *Run a job → Store the partner monitoring filter choices*)
fills Partner monitoring's row, and until then its first frame does."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("datamart", "0022_action_point_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="PageChoices",
            fields=[
                (
                    "name",
                    models.CharField(max_length=64, primary_key=True, serialize=False),
                ),
                ("value", models.TextField(help_text="the choices, as JSON text")),
                (
                    "version",
                    models.PositiveSmallIntegerField(
                        default=0, help_text="the shape of the value its reader expects"
                    ),
                ),
                (
                    "fingerprint",
                    models.CharField(
                        blank=True,
                        help_text="the moment of the eTools figures they were read at",
                        max_length=16,
                    ),
                ),
                ("computed_at", models.DateTimeField()),
            ],
            options={
                "verbose_name": "stored page choices",
                "verbose_name_plural": "stored page choices",
            },
        ),
    ]
