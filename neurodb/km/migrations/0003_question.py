"""The questions put to the KM Database's assistant (its own log, kept 90 days): a new, empty table
(CreateModel only, no code runs)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("km", "0002_organisation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="KMQuestion",
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
                    "conversation",
                    models.UUIDField(
                        blank=True,
                        help_text="groups a question with its follow-ups",
                        null=True,
                    ),
                ),
                (
                    "question",
                    models.TextField(
                        help_text="as sent: names, e-mail addresses, links and phone numbers removed",
                        verbose_name="question",
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        blank=True, max_length=500, verbose_name="filters"
                    ),
                ),
                (
                    "terms",
                    models.JSONField(
                        blank=True, default=list, verbose_name="search words suggested"
                    ),
                ),
                (
                    "read",
                    models.JSONField(
                        blank=True, default=list, verbose_name="findings read"
                    ),
                ),
                (
                    "cited",
                    models.JSONField(
                        blank=True, default=list, verbose_name="findings cited"
                    ),
                ),
                ("answer", models.TextField(blank=True, verbose_name="answer")),
                (
                    "covered",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("yes", "Answered"),
                            ("partly", "In part"),
                            ("no", "Not answered"),
                        ],
                        max_length=8,
                        verbose_name="answers the question",
                    ),
                ),
                (
                    "uncited",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="sentences citing no finding"
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("in_progress", "Being answered"),
                            ("answered", "Answered"),
                            ("no_match", "No matching findings"),
                            ("refused", "Declined"),
                            ("failed", "Failed"),
                            ("limited", "Over a limit or AI off"),
                        ],
                        db_index=True,
                        default="in_progress",
                        max_length=12,
                        verbose_name="status",
                    ),
                ),
                (
                    "model",
                    models.CharField(blank=True, max_length=64, verbose_name="model"),
                ),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("cache_read_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True, verbose_name="error")),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, db_index=True, verbose_name="asked at"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="asked by",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="km.kmversion",
                        verbose_name="version read",
                    ),
                ),
            ],
            options={
                "verbose_name": "question to the KM Database",
                "verbose_name_plural": "questions to the KM Database",
                "db_table": "km_question",
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(
                        fields=["user", "-created_at"], name="km_question_user"
                    ),
                    models.Index(
                        fields=["user", "conversation", "created_at"],
                        name="km_question_turns",
                    ),
                ],
            },
        ),
    ]
