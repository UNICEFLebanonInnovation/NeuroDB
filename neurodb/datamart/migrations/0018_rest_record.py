"""The records of the eTools REST API kept whole as they came, for Raw eTools data (Admin → Data and sync). A new,
empty table with its key and its index."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0017_raw_field_review"),
    ]

    operations = [
        migrations.CreateModel(
            name="RestRecord",
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
                ("entity", models.CharField(db_index=True, max_length=32)),
                ("record_key", models.CharField(max_length=64)),
                ("source_id", models.CharField(blank=True, help_text="the eTools id", max_length=64)),
                ("reference", models.CharField(blank=True, help_text="its number: never a person", max_length=255)),
                (
                    "title",
                    models.CharField(
                        blank=True, help_text="its name or title: never a trip's purpose", max_length=255
                    ),
                ),
                (
                    "parent_key",
                    models.CharField(
                        blank=True, help_text="a detail: the list record it details", max_length=64
                    ),
                ),
                ("data", models.JSONField(blank=True, default=dict)),
                ("synced_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "eTools REST record",
                "constraints": [
                    models.UniqueConstraint(fields=("entity", "record_key"), name="datamart_restrecord_key")
                ],
                "indexes": [models.Index(fields=["entity", "-synced_at"], name="datamart_restrecord_synced")],
            },
        ),
    ]
