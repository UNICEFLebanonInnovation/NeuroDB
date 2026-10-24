"""The KM Database: its versions, documents and findings, new and empty tables with their search indexes
(CreateModel only, no code runs). The uploaded workbook is read by the background load, never by a migration."""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
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
            name="KMDocument",
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
                ("position", models.PositiveIntegerField(verbose_name="row")),
                (
                    "record_id",
                    models.CharField(
                        max_length=40, unique=True, verbose_name="Document ID"
                    ),
                ),
                (
                    "item_title",
                    models.TextField(blank=True, verbose_name="short title"),
                ),
                ("title", models.TextField(blank=True, verbose_name="title")),
                (
                    "entity",
                    models.TextField(
                        blank=True, verbose_name="publishing entity / authors"
                    ),
                ),
                (
                    "year",
                    models.PositiveSmallIntegerField(
                        blank=True, db_index=True, null=True, verbose_name="year"
                    ),
                ),
                (
                    "year_text",
                    models.CharField(
                        blank=True, max_length=40, verbose_name="year (as given)"
                    ),
                ),
                (
                    "study_type",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=200,
                        verbose_name="document or study type",
                    ),
                ),
                (
                    "text_used",
                    models.CharField(
                        blank=True, max_length=100, verbose_name="text used"
                    ),
                ),
                (
                    "confidence",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("A", "Level A"),
                            ("B", "Level B"),
                            ("C", "Level C"),
                            ("D", "Level D"),
                        ],
                        db_index=True,
                        max_length=1,
                        verbose_name="confidence level",
                    ),
                ),
                (
                    "confidence_text",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="confidence level (as given)",
                    ),
                ),
                (
                    "findings_stated",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="number of findings (as given)",
                    ),
                ),
                ("link", models.TextField(blank=True, verbose_name="link")),
                ("quality", models.TextField(blank=True, verbose_name="data quality")),
                (
                    "needs_review",
                    models.BooleanField(
                        db_index=True, default=False, verbose_name="marked for review"
                    ),
                ),
                (
                    "checks",
                    models.JSONField(
                        blank=True, default=dict, verbose_name="values to check"
                    ),
                ),
                (
                    "search_vector",
                    django.contrib.postgres.search.SearchVectorField(
                        editable=False, null=True
                    ),
                ),
            ],
            options={
                "verbose_name": "document",
                "verbose_name_plural": "documents",
                "db_table": "km_document",
                "ordering": ("position",),
                "indexes": [
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["search_vector"], name="km_document_search"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="KMFinding",
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
                ("position", models.PositiveIntegerField(verbose_name="row")),
                (
                    "finding_id",
                    models.CharField(
                        max_length=40, unique=True, verbose_name="Finding ID"
                    ),
                ),
                (
                    "record_id",
                    models.CharField(max_length=40, verbose_name="Document ID"),
                ),
                (
                    "item_title",
                    models.TextField(blank=True, verbose_name="short title"),
                ),
                (
                    "document_title",
                    models.TextField(blank=True, verbose_name="document title"),
                ),
                (
                    "pillar_code",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=8,
                        verbose_name="pillar code",
                    ),
                ),
                ("pillar", models.TextField(blank=True, verbose_name="pillar")),
                (
                    "domain_code",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=8,
                        verbose_name="analytical domain code",
                    ),
                ),
                (
                    "domain",
                    models.TextField(blank=True, verbose_name="analytical domain"),
                ),
                (
                    "rq_code",
                    models.CharField(
                        blank=True, max_length=8, verbose_name="research question code"
                    ),
                ),
                (
                    "research_question",
                    models.TextField(blank=True, verbose_name="research question"),
                ),
                (
                    "question_key",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=12,
                        verbose_name="research question key",
                    ),
                ),
                (
                    "sub_question",
                    models.TextField(blank=True, verbose_name="sub-question"),
                ),
                (
                    "sub_sub_question",
                    models.TextField(blank=True, verbose_name="sub-sub-question"),
                ),
                (
                    "indicator",
                    models.TextField(
                        blank=True, verbose_name="indicator or evidence asked for"
                    ),
                ),
                (
                    "indicator_component",
                    models.TextField(
                        blank=True, verbose_name="part of the indicator answered"
                    ),
                ),
                (
                    "also_relevant",
                    models.TextField(blank=True, verbose_name="also relevant to"),
                ),
                ("measure", models.TextField(blank=True, verbose_name="measure")),
                (
                    "key_finding",
                    models.TextField(blank=True, verbose_name="key finding"),
                ),
                (
                    "analysis",
                    models.TextField(blank=True, verbose_name="finding analysis"),
                ),
                (
                    "age_text",
                    models.TextField(blank=True, verbose_name="age group (as given)"),
                ),
                (
                    "prenatal",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="before birth (pregnancy)",
                    ),
                ),
                (
                    "early_childhood",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="ages 0-4",
                    ),
                ),
                (
                    "middle_childhood",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="ages 5-9",
                    ),
                ),
                (
                    "early_adolescence",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="ages 10-14",
                    ),
                ),
                (
                    "late_adolescence",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="ages 15-19",
                    ),
                ),
                (
                    "youth",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="youth (15-24)",
                    ),
                ),
                (
                    "young_people",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="young people (10-24)",
                    ),
                ),
                (
                    "adult_caregiver",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="adults and caregivers (20+)",
                    ),
                ),
                (
                    "female",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="girls and women",
                    ),
                ),
                (
                    "male",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="boys and men",
                    ),
                ),
                (
                    "sem_individual",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="individual",
                    ),
                ),
                (
                    "sem_household",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="household and family",
                    ),
                ),
                (
                    "sem_community",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="community",
                    ),
                ),
                (
                    "sem_institutional",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="institutions and services",
                    ),
                ),
                (
                    "sem_policy",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="policies and laws",
                    ),
                ),
                (
                    "disability",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="disability inclusion",
                    ),
                ),
                (
                    "poverty",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="poverty and economic vulnerability",
                    ),
                ),
                (
                    "geographic",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="differences between areas",
                    ),
                ),
                (
                    "nationality",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="nationality and legal status",
                    ),
                ),
                (
                    "mhpss",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="mental health and psychosocial support",
                    ),
                ),
                (
                    "sbc",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="social and behaviour change",
                    ),
                ),
                (
                    "climate",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="climate and the environment",
                    ),
                ),
                (
                    "systems",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="strengthening systems",
                    ),
                ),
                (
                    "foresight",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="looking ahead (future scenarios)",
                    ),
                ),
                (
                    "information_access",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="access to information, digital rights and accountability",
                    ),
                ),
                (
                    "youth_participation",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="youth participation",
                    ),
                ),
                (
                    "palestinian_programme",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Y", "Yes"),
                            ("N", "No"),
                            ("R", "Not reported"),
                            ("A", "Not applicable"),
                        ],
                        max_length=1,
                        verbose_name="Palestinian programme in Lebanon",
                    ),
                ),
                (
                    "limitations",
                    models.TextField(blank=True, verbose_name="limitations"),
                ),
                (
                    "evidence_origin",
                    models.TextField(blank=True, verbose_name="evidence origin"),
                ),
                (
                    "origin_kind",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=1,
                        verbose_name="evidence origin (kind)",
                    ),
                ),
                (
                    "confidence",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("A", "Level A"),
                            ("B", "Level B"),
                            ("C", "Level C"),
                            ("D", "Level D"),
                        ],
                        db_index=True,
                        max_length=1,
                        verbose_name="confidence level",
                    ),
                ),
                (
                    "confidence_text",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="confidence level (as given)",
                    ),
                ),
                (
                    "entity",
                    models.TextField(
                        blank=True, verbose_name="publishing entity / author"
                    ),
                ),
                (
                    "study_type",
                    models.TextField(blank=True, verbose_name="document or study type"),
                ),
                (
                    "document_type",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=200,
                        verbose_name="document type",
                    ),
                ),
                (
                    "year",
                    models.PositiveSmallIntegerField(
                        blank=True, db_index=True, null=True, verbose_name="year"
                    ),
                ),
                (
                    "year_text",
                    models.CharField(
                        blank=True, max_length=40, verbose_name="year (as given)"
                    ),
                ),
                (
                    "period",
                    models.TextField(
                        blank=True, verbose_name="study or data collection period"
                    ),
                ),
                ("population", models.TextField(blank=True, verbose_name="population")),
                (
                    "methods",
                    models.TextField(blank=True, verbose_name="methods and sample"),
                ),
                ("link", models.TextField(blank=True, verbose_name="link")),
                ("quality", models.TextField(blank=True, verbose_name="data quality")),
                (
                    "needs_review",
                    models.BooleanField(
                        db_index=True, default=False, verbose_name="marked for review"
                    ),
                ),
                (
                    "checks",
                    models.JSONField(
                        blank=True, default=dict, verbose_name="values to check"
                    ),
                ),
                (
                    "search_vector",
                    django.contrib.postgres.search.SearchVectorField(
                        editable=False, null=True
                    ),
                ),
                (
                    "own_vector",
                    django.contrib.postgres.search.SearchVectorField(
                        editable=False, null=True
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="findings",
                        to="km.kmdocument",
                    ),
                ),
            ],
            options={
                "verbose_name": "finding",
                "verbose_name_plural": "findings",
                "db_table": "km_finding",
                "ordering": ("position",),
                "indexes": [
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["search_vector"], name="km_finding_search"
                    ),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["own_vector"], name="km_finding_own"
                    ),
                    models.Index(
                        fields=["pillar_code", "question_key"],
                        name="km_finding_question",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="KMVersion",
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
                            ("queued", "Waiting to load"),
                            ("loading", "Loading"),
                            ("live", "Shown now"),
                            ("replaced", "Replaced by a newer version"),
                            ("failed", "Not loaded"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=10,
                        verbose_name="status",
                    ),
                ),
                ("file_name", models.CharField(max_length=255, verbose_name="file")),
                (
                    "file_size",
                    models.PositiveIntegerField(default=0, verbose_name="size (bytes)"),
                ),
                (
                    "file_sha256",
                    models.CharField(
                        max_length=64, verbose_name="file fingerprint (SHA-256)"
                    ),
                ),
                ("content", models.BinaryField(null=True)),
                (
                    "fewer_ok",
                    models.BooleanField(
                        default=False, verbose_name="fewer findings on purpose"
                    ),
                ),
                (
                    "prepared_on",
                    models.DateField(blank=True, null=True, verbose_name="prepared on"),
                ),
                (
                    "source_name",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        verbose_name="source workbook (its name only)",
                    ),
                ),
                (
                    "stated_documents",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="documents the Read Me states",
                    ),
                ),
                (
                    "stated_findings",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="findings the Read Me states",
                    ),
                ),
                (
                    "documents_count",
                    models.PositiveIntegerField(default=0, verbose_name="documents"),
                ),
                (
                    "findings_count",
                    models.PositiveIntegerField(default=0, verbose_name="findings"),
                ),
                (
                    "review_count",
                    models.PositiveIntegerField(
                        default=0, verbose_name="marked for review"
                    ),
                ),
                (
                    "check_count",
                    models.PositiveIntegerField(
                        default=0, verbose_name="rows with a value to check"
                    ),
                ),
                (
                    "problems",
                    models.JSONField(blank=True, default=list, verbose_name="problems"),
                ),
                (
                    "warnings",
                    models.JSONField(blank=True, default=list, verbose_name="warnings"),
                ),
                (
                    "download_summary",
                    models.JSONField(
                        blank=True, default=dict, verbose_name="downloads summary"
                    ),
                ),
                (
                    "peak_memory_mb",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="peak memory of the load (MB)",
                    ),
                ),
                (
                    "load_seconds",
                    models.PositiveIntegerField(
                        blank=True, null=True, verbose_name="load time (seconds)"
                    ),
                ),
                (
                    "uploaded_at",
                    models.DateTimeField(
                        auto_now_add=True, db_index=True, verbose_name="uploaded at"
                    ),
                ),
                (
                    "started_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="load started at"
                    ),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="load finished at"
                    ),
                ),
                ("error", models.TextField(blank=True, verbose_name="error")),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="uploaded by",
                    ),
                ),
            ],
            options={
                "verbose_name": "version",
                "verbose_name_plural": "versions",
                "db_table": "km_version",
                "ordering": ("-uploaded_at",),
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "live")),
                        fields=("status",),
                        name="km_one_live_version",
                    )
                ],
            },
        ),
    ]
