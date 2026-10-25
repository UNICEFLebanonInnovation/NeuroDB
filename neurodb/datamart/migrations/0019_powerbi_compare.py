"""Compare with Power BI (Admin → Data and sync): an upload of the PRP Power BI exports, its exports and their
differences. Three new, empty tables (CreateModel only)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("datamart", "0018_rest_record"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PowerBIUpload",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Waiting to start"),
                            ("reading", "Reading the files"),
                            ("comparing", "Comparing"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        default="queued",
                        max_length=10,
                    ),
                ),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("queued_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("compared_at", models.DateTimeField(blank=True, null=True)),
                ("day", models.DateField(blank=True, null=True)),
                ("powerbi_as_of", models.DateTimeField(blank=True, null=True)),
                ("synced_at", models.DateTimeField(blank=True, null=True)),
                ("sync_moved", models.BooleanField(default=False)),
                ("refused", models.JSONField(blank=True, default=list)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("choices", models.JSONField(blank=True, default=list)),
                ("trimmed", models.BooleanField(default=False)),
                ("error", models.CharField(blank=True, max_length=300)),
                ("peak_memory_mb", models.FloatField(blank=True, null=True)),
                ("seconds", models.FloatField(blank=True, null=True)),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Power BI upload",
                "ordering": ("-uploaded_at", "-id"),
            },
        ),
        migrations.CreateModel(
            name="PowerBIExport",
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
                ("order", models.PositiveSmallIntegerField()),
                ("kind", models.CharField(max_length=30)),
                ("file_name", models.CharField(max_length=255)),
                ("state", models.CharField(max_length=12)),
                ("content", models.BinaryField(null=True)),
                ("rows", models.BinaryField(null=True)),
                ("rows_read", models.PositiveIntegerField(default=0)),
                ("set_aside", models.PositiveIntegerField(default=0)),
                ("columns", models.JSONField(blank=True, default=list)),
                ("dropped", models.JSONField(blank=True, default=list)),
                ("not_compared", models.JSONField(blank=True, default=list)),
                ("unknown_columns", models.PositiveSmallIntegerField(default=0)),
                ("filters", models.JSONField(blank=True, default=dict)),
                ("clauses", models.JSONField(blank=True, default=list)),
                ("counts", models.JSONField(blank=True, default=dict)),
                ("fits", models.JSONField(blank=True, default=list)),
                ("verdict", models.CharField(blank=True, max_length=30)),
                ("notes", models.JSONField(blank=True, default=list)),
                ("error", models.CharField(blank=True, max_length=300)),
                (
                    "upload",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="exports",
                        to="datamart.powerbiupload",
                    ),
                ),
            ],
            options={
                "verbose_name": "Power BI export",
                "ordering": ("upload", "order", "id"),
            },
        ),
        migrations.CreateModel(
            name="PowerBIDifference",
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
                ("n", models.PositiveIntegerField()),
                ("group", models.PositiveSmallIntegerField()),
                ("kind", models.CharField(max_length=12)),
                ("reason", models.CharField(max_length=20)),
                ("rule", models.CharField(blank=True, max_length=20)),
                ("detail", models.CharField(blank=True, max_length=300)),
                ("match", models.JSONField(blank=True, default=list)),
                ("found", models.JSONField(blank=True, default=dict)),
                ("link", models.CharField(blank=True, max_length=300)),
                (
                    "export",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="differences",
                        to="datamart.powerbiexport",
                    ),
                ),
            ],
            options={
                "verbose_name": "Power BI difference",
                "indexes": [
                    models.Index(
                        fields=["export", "group", "reason", "n"],
                        name="datamart_powerbidiff_group",
                    )
                ],
            },
        ),
    ]
