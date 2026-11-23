"""Which daily review checks are in trial: a new, empty table of one row per check (a check with no row takes
its default, ``review.checks.default_mode``), and whether a finding was found by a check in trial, a column
with a constant default (the table's description only on PostgreSQL, whatever its rows). CreateModel and
AddField only, no code runs."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("review", "0003_ai_decisions"),
    ]

    operations = [
        migrations.CreateModel(
            name="CheckSetting",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "check_id",
                    models.CharField(max_length=40, unique=True, verbose_name="check"),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("trial", "Trial: Daily review page only"),
                            ("on", "On: also sent to NeuroDB Watch"),
                        ],
                        default="trial",
                        max_length=8,
                    ),
                ),
                (
                    "on_since",
                    models.DateField(
                        blank=True, help_text="the day it was switched on", null=True
                    ),
                ),
                (
                    "trial_since",
                    models.DateField(
                        blank=True, help_text="the day it went into trial", null=True
                    ),
                ),
                ("updated_by", models.CharField(blank=True, max_length=150)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "daily review check",
                "verbose_name_plural": "daily review checks",
                "ordering": ("check_id",),
            },
        ),
        migrations.AddField(
            model_name="reviewfinding",
            name="trial",
            field=models.BooleanField(
                default=False,
                help_text="found by a check in trial: on the Daily review page only, never sent to NeuroDB Watch",
            ),
        ),
    ]
