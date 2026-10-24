"""The KM Database's corrections of how its downloads show an author entry: a new, empty table (CreateModel
only, no code runs). An administrator fills it in the admin."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("km", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="KMOrganisation",
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
                    "name",
                    models.CharField(
                        help_text="Corrects how downloads show an author entry. Write it as the workbook does. Changes downloads at once.",
                        max_length=200,
                        verbose_name="name",
                    ),
                ),
                (
                    "folded",
                    models.CharField(editable=False, max_length=200, unique=True),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("organisation", "An organisation: name it in downloads"),
                            ("person", "A person: count, never name"),
                            (
                                "words",
                                "Not a name or an organisation: leave it out of downloads and keep it in the texts",
                            ),
                        ],
                        max_length=12,
                        verbose_name="shown in downloads as",
                    ),
                ),
                (
                    "added_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="added at"),
                ),
                (
                    "changed_at",
                    models.DateTimeField(auto_now=True, verbose_name="changed at"),
                ),
                (
                    "added_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="added by",
                    ),
                ),
            ],
            options={
                "verbose_name": "organisation named in downloads",
                "verbose_name_plural": "organisations named in downloads",
                "db_table": "km_organisation",
                "ordering": ("name",),
            },
        ),
    ]
