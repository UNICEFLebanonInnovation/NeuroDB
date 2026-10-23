"""The fields of an eTools source a superuser reviewed on Raw eTools data (Admin → Data and sync), shown there
through the people rule. A new, empty table."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0016_etools_masters"),
    ]

    operations = [
        migrations.CreateModel(
            name="RawFieldReview",
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
                ("source", models.CharField(max_length=64)),
                ("key", models.CharField(max_length=64)),
                ("reviewed_at", models.DateTimeField(auto_now_add=True)),
                ("reviewed_by", models.CharField(blank=True, max_length=150)),
            ],
            options={
                "verbose_name": "reviewed field",
                "verbose_name_plural": "reviewed fields",
                "constraints": [
                    models.UniqueConstraint(fields=("source", "key"), name="datamart_rawfieldreview_key")
                ],
            },
        ),
    ]
