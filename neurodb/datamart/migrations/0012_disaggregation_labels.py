"""The labels of PRP's disaggregations and their values ("Sex": "female"), read by the eTools Datamart sync
for the ids the partner reports key their disaggregation values by."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0011_indicator_flag_unique_beneficiaries"),
    ]

    operations = [
        migrations.CreateModel(
            name="DisaggregationLabel",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("value", "disaggregation value"),
                            ("type", "disaggregation"),
                        ],
                        max_length=8,
                    ),
                ),
                (
                    "prp_id",
                    models.BigIntegerField(help_text="the id in the Partner Reporting Portal"),
                ),
                ("label", models.CharField(blank=True, max_length=255)),
                (
                    "disaggregation_id",
                    models.BigIntegerField(
                        blank=True,
                        help_text="a value's disaggregation (its type), as a PRP id",
                        null=True,
                    ),
                ),
                ("active", models.BooleanField(default=True)),
                (
                    "not_found",
                    models.BooleanField(
                        default=False,
                        help_text="PRP answered that the id does not exist",
                    ),
                ),
                ("synced_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "disaggregation label",
                "ordering": ("kind", "label"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "prp_id"),
                        name="datamart_disaggregationlabel_key",
                    )
                ],
            },
        ),
    ]
