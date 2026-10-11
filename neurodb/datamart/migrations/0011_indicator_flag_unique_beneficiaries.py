"""The "unique beneficiaries" role of the indicator flags: the one indicator of a programme document
that is its headline "children / beneficiaries reached" (``neurodb.datamart.headline``), one per PD.
``counts_children`` may now be empty (the title rule decides), so a flag set for the new role alone
says nothing about children. Existing flags keep their value."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0010_fill_reached_columns"),
        ("etools", "0003_partnerlink"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="indicatorflag",
            options={
                "ordering": ("source", "label"),
                "verbose_name": "indicator flag",
                "verbose_name_plural": "indicator flags",
            },
        ),
        migrations.AddField(
            model_name="indicatorflag",
            name="intervention",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="indicator_flags",
                to="etools.pca",
                verbose_name="programme document",
            ),
        ),
        migrations.AddField(
            model_name="indicatorflag",
            name="unique_beneficiaries",
            field=models.BooleanField(
                default=False,
                help_text="the programme document's unique beneficiaries indicator: each child or person counted once, the PD's figure in every children reached total (one per programme document)",
                verbose_name="unique beneficiaries",
            ),
        ),
        migrations.AlterField(
            model_name="indicatorflag",
            name="counts_children",
            field=models.BooleanField(
                blank=True,
                default=True,
                help_text="whether the indicator counts children; empty: the title decides",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="indicatorflag",
            constraint=models.UniqueConstraint(
                condition=models.Q(("unique_beneficiaries", True)),
                fields=("intervention",),
                name="datamart_indicatorflag_one_unique_per_pd",
            ),
        ),
    ]
