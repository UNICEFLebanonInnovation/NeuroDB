"""The eTools master indicators job and its schedule ``etools-masters``: every night at 02:40, after the eTools
Datamart sync (20:30) and the reporting checks (02:15), and due at once, so the suggestions are there soon
after the deployment that adds it. The scheduler starts it on its first pass; when the previous container's
scheduler, which does not know the command, sees the row first (it then plans it for the next 02:40), the new
release's scheduler starts it as soon as it takes over (``neurodb.core.scheduler.tick``). The job only
suggests PD indicators for the master indicators and proposes new ones: nothing counts until a person
confirms it.

The same release rewrites eTools reporting checks 1 to 11 and adds check 12 (*Indicator grouping*), which is
read before accepting the first proposals; the result the previous release stored has eleven checks worked out
the earlier way. So the ``reporting-checks`` schedule, when switched on, is made due a quarter of an hour after
this migration (``AFTER_RELEASE``) instead of the next 02:15, not at once: the previous container's scheduler,
still running during the deployment, knows the command and would run its own earlier checks; a quarter of an
hour later the new release's has normally taken over. Until a run of this release stores its result (and if the
previous container still ran them), the checks page says the one shown is an earlier release's
(``neurodb.datamart.checks.read_stored``), with *Run the checks now*. A new database keeps the checks due at
once (core 0029)."""

import datetime

from django.db import migrations, models
from django.utils import timezone

AFTER_RELEASE = datetime.timedelta(minutes=15)


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    job, created = ScheduledJob.objects.get_or_create(
        key="etools-masters",
        defaults={"command": "etools_masters", "schedule": "40 2 * * *", "enabled": True},
    )
    if created:
        job.next_run_at = timezone.now()  # due now: the scheduler's next pass starts it
        job.save(update_fields=["next_run_at"])
    soon = timezone.now() + AFTER_RELEASE
    checks = ScheduledJob.objects.filter(key="reporting-checks", enabled=True).first()
    if checks is not None and (checks.next_run_at is None or checks.next_run_at > soon):
        checks.next_run_at = soon  # the checks of this release, soon after its deployment
        checks.save(update_fields=["next_run_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0030_datamart_prp_setting"),
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
                    ("etools_masters", "eTools master indicators"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
