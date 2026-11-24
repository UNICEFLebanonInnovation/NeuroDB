"""Removes the job of NeuroDB's own grouping of PD indicators (the owner's decision: eTools has no such grouping):
its choice on the sync runs (a choice of the column, nothing in the database changes), its schedule
``etools-masters`` (and any copy of it under another key) and its runs.

The reporting checks lose check 12 in the same release, and the result the previous release stored still holds
it. So the ``reporting-checks`` schedule, when switched on, is made due a quarter of an hour after this
migration (``AFTER_RELEASE``) instead of the next 02:15, as core 0031 did, not at once: the previous container's
scheduler, still running during the deployment, would run its own checks; a quarter of an hour later the new
release's has normally taken over. Until then the checks page says the result shown is an earlier release's
(``neurodb.datamart.checks.read_stored``), with *Run the checks now*. Quick: a few dozen schedule rows, and the
job's runs (about one a night) through the runs' job index. The grouping's three tables are left alone this
release (datamart 0024)."""

import datetime

from django.db import migrations, models
from django.utils import timezone

AFTER_RELEASE = datetime.timedelta(minutes=15)
COMMAND = "etools_masters"


def drop_rows(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    SyncRun = apps.get_model("core", "SyncRun")
    ScheduledJob.objects.filter(command=COMMAND).delete()  # "etools-masters" and any copy of it
    SyncRun.objects.filter(job=COMMAND).delete()  # its runs (the knowledge hub's changes keep theirs: SET NULL)
    soon = timezone.now() + AFTER_RELEASE
    checks = ScheduledJob.objects.filter(key="reporting-checks", enabled=True).first()
    if checks is not None and (checks.next_run_at is None or checks.next_run_at > soon):
        checks.next_run_at = soon  # the checks of this release, soon after its deployment
        checks.save(update_fields=["next_run_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_ap_history_job"),
        # the knowledge hub's changes point to a run (SET NULL): their model is in the state the runs are
        # deleted with, so a change that names a deleted run keeps its row with no run
        ("graph", "0003_fm_visit_kind"),
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
                    ("etools_rest_raw", "eTools REST API into Raw eTools data"),
                    ("ap_history", "Action point status record"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(drop_rows, migrations.RunPython.noop),
    ]
