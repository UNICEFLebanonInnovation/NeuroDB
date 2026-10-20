"""The eTools Datamart probe job (a choice of ``SyncRun.job``) and the admin's "eTools Datamart: PRP business area
code", a new, empty table of one row. Schema only: no code runs."""

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_reporting_checks_job"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="syncrun",
            name="job",
            field=models.CharField(
                choices=[
                    ("ai_structure", "ActivityInfo structure import"),
                    ("ai_data", "ActivityInfo data import"),
                    ("etools", "eTools sync"),
                    ("etools_datamart", "eTools Datamart sync"),
                    ("locations", "Locations sync"),
                    ("population", "Population figures load"),
                    ("partner_links", "ActivityInfo partner links"),
                    ("daily_review", "Daily AI review"),
                    ("compiler_youth", "Compiler youth figures"),
                    ("compiler_education", "Compiler education figures"),
                    ("compiler_wellbeing", "Compiler Makani wellbeing flags"),
                    ("knowledge_hub", "Knowledge hub"),
                    ("whats_new", "What's new note"),
                    ("ml_readiness", "Machine learning readiness check"),
                    ("forecast", "Year-end indicator forecast"),
                    ("watch", "NeuroDB Watch"),
                    ("fmm_refresh", "Monitoring insights refresh"),
                    ("fmm_insights", "Monitoring insights (AI briefs)"),
                    ("fmm_ai_checks", "Monitoring insights (AI checks)"),
                    ("fmm_ap_review", "Monitoring insights (action point review)"),
                    ("doc_review", "Document review"),
                    ("datamart_backfill", "eTools update clean-up"),
                    ("reporting_checks", "eTools reporting checks"),
                    ("datamart_probe", "eTools Datamart probe"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="DatamartSetting",
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
                    "prp_business_area",
                    models.CharField(
                        blank=True,
                        help_text="The country office's business area code, which the eTools Datamart sync sends to PRP's indicator reports, programme documents and progress reports (they carry no country name). Leave it empty to let NeuroDB find it in the Datamart's list of workspaces. A code the hosting team set (ETOOLS_DATAMART_BUSINESS_AREA) comes before this one. It applies from the next sync of those datasets; Run a job → Probe a Datamart dataset shows first what the Datamart holds for it.",
                        max_length=20,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[0-9A-Za-z]+$", "Letters and digits only, as in 2490."
                            )
                        ],
                        verbose_name="PRP business area code",
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
                "verbose_name": "eTools Datamart: PRP business area code",
                "verbose_name_plural": "eTools Datamart: PRP business area code",
            },
        ),
    ]
