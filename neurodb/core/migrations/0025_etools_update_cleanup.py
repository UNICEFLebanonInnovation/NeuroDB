"""The ``datamart_backfill`` job (the eTools update clean-up) and its schedule ``etools-update-cleanup``.

The schedule is due at once, so the scheduler starts the job on its first pass after the deployment
(usually within a minute of the site starting; at most 15 minutes when the previous container's
scheduler, which does not know the command, sees it first and plans it for its next quarter-hour),
then every 15 minutes until a run has gone through everything: the job then switches the schedule
off. This does the work datamart 0010 and 0013 did
before the site started, which on the real tables did not fit in App Service's start-up limit."""

from django.db import migrations, models
from django.utils import timezone


def schedule(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    job, created = ScheduledJob.objects.get_or_create(
        key="etools-update-cleanup",
        defaults={"command": "datamart_backfill", "schedule": "*/15 * * * *", "enabled": True},
    )
    if created:
        job.next_run_at = timezone.now()  # due now: the scheduler's next pass starts it
        job.save(update_fields=["next_run_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_doc_review"),
        ("datamart", "0013_scrub_contact_details"),
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
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(schedule, migrations.RunPython.noop),
    ]
