"""Release 2, stage C: the action points module. The AI review of completed eTools action points
(``ActionPointReview``), their PME verifications (``ActionPointVerification``), the AI content
summaries asked for (``ActionPointSummary``, no text kept), the action points kept in NeuroDB only
(``LocalActionPoint``) and the page's AI settings (``ActionPointSetting``); the prompt versions' help
names the action points' instructions."""

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fmm", "0013_lebanon_rules"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ActionPointReview",
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
                ("datamart_id", models.BigIntegerField(unique=True)),
                ("input_hash", models.CharField(max_length=64)),
                ("prompt_hash", models.CharField(max_length=64)),
                (
                    "verdict",
                    models.CharField(
                        choices=[
                            ("adequate", "Adequately addressed"),
                            ("partial", "Partially addressed"),
                            ("not_addressed", "Not addressed"),
                            ("vague", "Generic/vague"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                ("explanation", models.CharField(blank=True, max_length=400)),
                ("model", models.CharField(blank=True, max_length=64)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("reviewed_at", models.DateTimeField(db_index=True)),
            ],
            options={
                "verbose_name": "AI review of an action point",
                "verbose_name_plural": "AI reviews of action points",
                "ordering": ("-reviewed_at",),
            },
        ),
        migrations.AlterField(
            model_name="promptversion",
            name="rule_prompts",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='The instructions of the AI checks, one per prompt key a narrative quality rule names (for example "evidence_sufficiency"): what the check looks for and when it passes. NeuroDB adds its fixed rules and the answer format (passed or not, and one or two sentences why). The action points page reads two more: "ap_adequacy_review" (does the action taken on a completed action point resolve its issue) and "ap_content_summary" (the dominant themes of the action points on the page).',
            ),
        ),
        migrations.CreateModel(
            name="ActionPointSetting",
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
                ("ai_review", models.BooleanField(default=True)),
                (
                    "summary_points",
                    models.PositiveSmallIntegerField(
                        default=150,
                        validators=[
                            django.core.validators.MinValueValidator(10),
                            django.core.validators.MaxValueValidator(500),
                        ],
                    ),
                ),
                (
                    "summary_per_user_per_day",
                    models.PositiveSmallIntegerField(
                        default=5,
                        validators=[django.core.validators.MaxValueValidator(50)],
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
                "verbose_name": "action point settings",
                "verbose_name_plural": "action point settings",
            },
        ),
        migrations.CreateModel(
            name="ActionPointSummary",
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
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("limited", "Over a limit"),
                        ],
                        max_length=8,
                    ),
                ),
                ("called", models.BooleanField(default=False)),
                ("points", models.PositiveSmallIntegerField(default=0)),
                ("reason", models.CharField(blank=True, max_length=200)),
                ("model", models.CharField(blank=True, max_length=64)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "AI content summary",
                "verbose_name_plural": "AI content summaries",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="LocalActionPoint",
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
                ("title", models.CharField(max_length=200)),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        validators=[django.core.validators.MaxLengthValidator(2000)],
                    ),
                ),
                (
                    "visit_key",
                    models.CharField(blank=True, db_index=True, max_length=40),
                ),
                (
                    "priority",
                    models.CharField(
                        choices=[
                            ("high", "High"),
                            ("medium", "Medium"),
                            ("low", "Low"),
                        ],
                        default="medium",
                        max_length=8,
                    ),
                ),
                ("due_date", models.DateField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("done", "Done"),
                            ("dropped", "Dropped"),
                        ],
                        db_index=True,
                        default="open",
                        max_length=8,
                    ),
                ),
                ("assignee_role", models.CharField(blank=True, max_length=150)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("manual", "Added by hand"),
                            ("auto", "Made by NeuroDB"),
                        ],
                        default="manual",
                        max_length=8,
                    ),
                ),
                ("rule", models.CharField(blank=True, max_length=4)),
                ("created_by_name", models.CharField(blank=True, max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "assignee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
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
                "verbose_name": "NeuroDB action point",
                "verbose_name_plural": "NeuroDB action points",
                "ordering": ("-created_at", "-pk"),
            },
        ),
        migrations.CreateModel(
            name="ActionPointVerification",
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
                ("datamart_id", models.BigIntegerField(db_index=True)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("verified", "Verified"),
                            ("rejected", "Rejected"),
                            ("pending", "Pending"),
                        ],
                        max_length=10,
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=500)),
                ("verified_by_name", models.CharField(max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "verified_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "action point verification",
                "verbose_name_plural": "action point verifications",
                "ordering": ("-created_at", "-pk"),
                "indexes": [
                    models.Index(
                        fields=["datamart_id", "-created_at"],
                        name="fmm_actionp_datamar_951816_idx",
                    )
                ],
            },
        ),
    ]
