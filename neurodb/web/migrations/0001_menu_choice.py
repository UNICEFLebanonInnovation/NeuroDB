"""The menu each role sees, as a superuser ticks it in the administration's Menu page: a new, empty table of at
most three rows (CreateModel only, no code runs)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MenuChoice",
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
                    "role",
                    models.CharField(
                        choices=[
                            ("Viewer", "Viewer"),
                            ("Section editor", "Section editor"),
                            ("Administrator", "Administrator"),
                        ],
                        max_length=32,
                        unique=True,
                        verbose_name="role",
                    ),
                ),
                (
                    "shown",
                    models.JSONField(
                        blank=True, default=list, verbose_name="entries shown"
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
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
                "verbose_name": "menu",
                "verbose_name_plural": "menu",
                "ordering": ["role"],
            },
        ),
    ]
