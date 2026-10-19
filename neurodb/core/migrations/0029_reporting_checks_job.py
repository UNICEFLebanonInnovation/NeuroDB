"""The eTools reporting checks job and its schedule ``reporting-checks``: every night at 02:15, after the
eTools Datamart sync (20:30), and due at once, so the page has a result to show soon after the deployment
that adds it. The scheduler starts it on its first pass; when the previous container's scheduler, which
does not know the command, sees the row first (it then plans it for the next 02:15), the new release's
scheduler starts it as soon as it takes over (``neurodb.core.scheduler.tick``). The checks only read
counts; the page shows the latest result."""

from django.db import migrations, models
from django.utils import timezone


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    job, created = ScheduledJob.objects.get_or_create(
        key="reporting-checks",
        defaults={"command": "reporting_checks", "schedule": "15 2 * * *", "enabled": True},
    )
    if created:
        job.next_run_at = timezone.now()  # due now: the scheduler's next pass starts it
        job.save(update_fields=["next_run_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0028_activityinfo_setting"),
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
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
