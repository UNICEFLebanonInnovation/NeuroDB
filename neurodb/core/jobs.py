"""The commands the scheduler may run: a fixed list, so a schedule can never run arbitrary code.

Shared by the scheduled jobs page (which command a schedule runs) and the scheduler (how to start it).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .models import SyncRun


@dataclass(frozen=True)
class JobCommand:
    label: str
    args: tuple[str, ...]  # manage.py arguments
    sync_job: str | None  # the SyncRun job it records: a second start is refused while one runs
    triggered_by: bool = True  # the command takes --triggered-by


COMMANDS: dict[str, JobCommand] = {
    "etools_datamart": JobCommand(
        _("Sync eTools (Datamart)"), ("sync_etools_datamart",), SyncRun.Job.ETOOLS_DATAMART
    ),
    "etools_rest": JobCommand(_("Sync eTools (REST API)"), ("sync_etools",), SyncRun.Job.ETOOLS),
    "locations": JobCommand(_("Sync locations"), ("sync_locations",), SyncRun.Job.LOCATIONS),
    "ai_structure": JobCommand(
        _("Import ActivityInfo structure (current year)"),
        ("import_activityinfo_structure", "--all"),
        SyncRun.Job.ACTIVITYINFO_STRUCTURE,
    ),
    "ai_data": JobCommand(
        _("Import ActivityInfo data (current year)"),
        ("import_activityinfo_data", "--current-year"),
        SyncRun.Job.ACTIVITYINFO_DATA,
    ),
    "link_partners": JobCommand(
        _("Link ActivityInfo partners to eTools"), ("link_partners",), SyncRun.Job.PARTNER_LINKS
    ),
    "compiler_youth": JobCommand(
        _("Read the youth figures from Compiler"), ("sync_compiler_youth",), SyncRun.Job.COMPILER_YOUTH
    ),
    "compiler_education": JobCommand(
        _("Read the education figures from Compiler"),
        ("sync_compiler_education",),
        SyncRun.Job.COMPILER_EDUCATION,
    ),
    "compiler_wellbeing": JobCommand(
        _("Read the Makani wellbeing flags from Compiler"),
        ("sync_compiler_wellbeing",),
        SyncRun.Job.COMPILER_WELLBEING,
    ),
    "knowledge_hub": JobCommand(
        _("Rebuild the knowledge hub (and read new documents)"),
        ("build_knowledge_hub",),
        SyncRun.Job.KNOWLEDGE_HUB,
    ),
    "daily_review": JobCommand(_("Daily review"), ("daily_review",), SyncRun.Job.DAILY_REVIEW),
    "forecast": JobCommand(
        _("Forecast the year-end value of the indicators"), ("forecast_indicators",), SyncRun.Job.FORECAST
    ),
    "ml_readiness": JobCommand(
        _("Check whether the data is ready for machine learning"), ("ml_readiness",), SyncRun.Job.ML_READINESS
    ),
    "whats_new": JobCommand(
        _("Write the daily what's new note (and email it)"), ("whats_new_digest",), SyncRun.Job.WHATS_NEW
    ),
    "watch": JobCommand(
        _("NeuroDB Watch: deadlines and concerns for each person"),
        ("run_watch", "--daily"),
        SyncRun.Job.WATCH,
    ),
    "fmm_refresh": JobCommand(_("Refresh monitoring insights"), ("fmm_refresh",), SyncRun.Job.FMM_REFRESH),
    "fmm_insights": JobCommand(_("Write AI monitoring briefs"), ("fmm_insights",), SyncRun.Job.FMM_INSIGHTS),
    "fmm_ai_checks": JobCommand(
        _("Run the AI checks of the monitoring quality rules"), ("fmm_ai_checks",), SyncRun.Job.FMM_AI_CHECKS
    ),
    "fmm_ap_review": JobCommand(
        _("Run the AI review of completed action points"), ("fmm_ap_review",), SyncRun.Job.FMM_AP_REVIEW
    ),
    "doc_review": JobCommand(
        _("Review documents (those waiting, failed or partly analysed)"),
        ("review_documents", "--pending"),
        SyncRun.Job.DOC_REVIEW,
    ),
    "datamart_backfill": JobCommand(
        _("Finish the eTools update (fill new columns, remove contact details)"),
        ("datamart_backfill",),
        SyncRun.Job.DATAMART_BACKFILL,
    ),
    "reporting_checks": JobCommand(
        _("Run the eTools reporting checks"), ("reporting_checks",), SyncRun.Job.REPORTING_CHECKS
    ),
    "etools_masters": JobCommand(
        _("Suggest PD indicators for the eTools master indicators"),
        ("suggest_master_indicators",),
        SyncRun.Job.ETOOLS_MASTERS,
    ),
    "freshness": JobCommand(_("Check data freshness"), ("check_sync_freshness",), None, triggered_by=False),
}

COMMAND_CHOICES = [(key, cmd.label) for key, cmd in COMMANDS.items()]

# Jobs whose run "succeeded with errors" is said on their own page, not raised as an alarm (the daily
# review's sync failures, the admin home's Needs attention): a reporting check that could not finish.
# A run of theirs that failed still counts.
PARTIAL_IS_NOT_AN_ALARM = frozenset({SyncRun.Job.REPORTING_CHECKS})

# Jobs whose runs are never an alarm, whatever their status: a probe of the eTools Datamart, started by hand
# from Run a job, whose run says what it found (a refusal of the Datamart included); nothing runs it again.
NEVER_AN_ALARM = frozenset({SyncRun.Job.DATAMART_PROBE})

# What the scheduler writes on a schedule whose command the running release does not know
UNKNOWN_COMMAND = "not started: unknown command"


def last_run(command: JobCommand) -> SyncRun | None:
    """The latest run of a command's own job (none without one), as Scheduled jobs' "Last run" and Ask
    NeuroDB's list of jobs say it. A superuser's removal of the partner report rows eTools no longer sends
    is recorded under the eTools update clean-up's job with a target (``datamart.backfill.UNSENT_TARGET``):
    it is never the clean-up's last run, which reads only the runs with no target."""
    if not command.sync_job:
        return None
    runs = SyncRun.objects.filter(job=command.sync_job)
    if command.sync_job == SyncRun.Job.DATAMART_BACKFILL:
        runs = runs.filter(target="")
    return runs.order_by("-started_at").first()


def start(key: str, triggered_by: str) -> str:
    """Start a job's command in the background. Returns "started" or "running" (not started)."""
    from neurodb.integrations import background

    command = COMMANDS[key]
    if command.sync_job and background.is_running(command.sync_job):
        return "running"
    args = [*command.args, *(("--triggered-by", triggered_by) if command.triggered_by else ())]
    background.start_command(*args)
    return "started"
