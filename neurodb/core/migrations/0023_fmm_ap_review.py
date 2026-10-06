"""The AI review of completed action points (does the action taken resolve the issue raised?): its job,
and its schedule, daily 06:10, after the 05:25 refresh, the 05:40 AI briefs and the 05:50 AI checks, so
that it reviews the points completed since the last run within its own daily cap
(``FMM_AP_REVIEW_DAILY_TOKEN_CAP``). With the AI switched off the run reviews nothing."""

from django.db import migrations, models


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="fmm-ap-review", defaults={"command": "fmm_ap_review", "schedule": "10 6 * * *", "enabled": True}
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0022_fmm_ai_checks")]

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
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
