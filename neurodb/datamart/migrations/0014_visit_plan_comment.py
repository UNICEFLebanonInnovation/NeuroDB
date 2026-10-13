"""The sections' comments on the HACT visit plan of the Field monitoring page (one per programme
document and year). A new, empty table that people write to: schema only, and the release before
this one never reads it."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0013_scrub_contact_details"),
        ("etools", "0003_partnerlink"),
    ]

    operations = [
        migrations.CreateModel(
            name="VisitPlanComment",
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
                    "pd_reference",
                    models.CharField(
                        blank=True,
                        help_text="the PD's reference number when written",
                        max_length=64,
                        verbose_name="PD reference",
                    ),
                ),
                (
                    "year",
                    models.PositiveSmallIntegerField(help_text="the HACT year (calendar year) of the plan"),
                ),
                ("text", models.CharField(max_length=500)),
                ("updated_by", models.CharField(blank=True, max_length=150)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "intervention",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="visit_plan_comments",
                        to="etools.pca",
                        verbose_name="programme document",
                    ),
                ),
            ],
            options={
                "verbose_name": "visit plan comment",
                "verbose_name_plural": "visit plan comments",
                "ordering": ("-year", "pd_reference"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("intervention", "year"),
                        name="datamart_visitplancomment_pd_year",
                    )
                ],
            },
        ),
    ]
