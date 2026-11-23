"""The action point history (``ActionPointStatus``): a new, empty table, with its index on the day and one row per
action point and day. CreateModel only, no code runs; the first record after a Datamart sync, or *Run a job →
Record action point status*, fills it."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0021_pd_indicator_listed"),
        ("etools", "0003_partnerlink"),
    ]

    operations = [
        migrations.CreateModel(
            name="ActionPointStatus",
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
                    "source_id",
                    models.BigIntegerField(help_text="the action point's id in eTools, which lasts"),
                ),
                (
                    "day",
                    models.DateField(db_index=True, help_text="the day this state was recorded"),
                ),
                (
                    "in_datamart",
                    models.BooleanField(
                        default=True,
                        help_text="no on the day the Datamart stopped returning the action point",
                    ),
                ),
                ("status", models.CharField(blank=True, max_length=30)),
                ("high_priority", models.BooleanField(default=False)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("date_of_completion", models.DateTimeField(blank=True, null=True)),
                (
                    "related_module",
                    models.CharField(blank=True, help_text="audit, tpm, fm, t2f, ...", max_length=64),
                ),
                ("section", models.CharField(blank=True, max_length=64)),
                (
                    "engagement_source_id",
                    models.BigIntegerField(blank=True, help_text="the audit's eTools id", null=True),
                ),
                (
                    "tpm_activity_source_id",
                    models.BigIntegerField(blank=True, help_text="the TPM activity's eTools id", null=True),
                ),
                (
                    "intervention",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="etools.pca",
                        verbose_name="programme document",
                    ),
                ),
                (
                    "partner",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="etools.partnerorganization",
                    ),
                ),
            ],
            options={
                "verbose_name": "action point status",
                "verbose_name_plural": "action point history",
                "ordering": ("-day", "source_id"),
                "constraints": [
                    models.UniqueConstraint(fields=("source_id", "day"), name="datamart_apstatus_point_day")
                ],
            },
        ),
    ]
