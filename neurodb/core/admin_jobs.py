"""Every operator command as a button: the "Run a job" menu of *Import and sync runs*.

On App Service there is no shell into the container, so anything an operator would otherwise run
with ``manage.py`` starts here. Long commands run in the background (``background.start_command``,
the same command the schedule runs, recorded as a ``SyncRun``); quick ones run in the request and
report their result as a message. Only administrators see the menu, and every button asks for a
confirmation first (the dialog makes the action a POST).

Not here on purpose: ``migrate_locked`` and ``ensure_legacy_tables`` (run by every deployment),
``seed_demo`` (local databases only) and ``record_datamart_samples`` (writes test fixtures into the
source tree).
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO

from django.contrib import messages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.decorators import action
from unfold.forms import BaseDialogForm

from .models import SyncRun


class ConfirmJobForm(BaseDialogForm):
    """The confirmation step of a job button."""


@dataclass(frozen=True)
class BackgroundJob:
    name: str  # the admin action's name
    command: tuple[str, ...]  # manage.py arguments
    job: str  # the SyncRun job it records, to refuse a second start
    menu: str  # the menu entry (the menu is narrow)
    title: str
    description: str
    icon: str


BACKGROUND_JOBS = [
    BackgroundJob(
        "run_sync_etools",
        ("sync_etools",),
        SyncRun.Job.ETOOLS,
        _("eTools REST sync"),
        _("Sync eTools (REST API)"),
        _(
            "Reads partners, agreements, programme documents and the other eTools records through the "
            "eTools REST API (the eTools token), in the background. The Datamart sync is the one "
            "scheduled every night; use this one when a record is needed before then."
        ),
        "sync",
    ),
    BackgroundJob(
        "run_sync_locations",
        ("sync_locations",),
        SyncRun.Job.LOCATIONS,
        _("Locations sync"),
        _("Sync locations"),
        _(
            "Reads the eTools locations (P-codes, admin levels, parents) from the eTools Datamart, as the "
            "nightly Datamart sync does, in the background."
        ),
        "location_on",
    ),
    BackgroundJob(
        "run_import_ai_structure",
        ("import_activityinfo_structure", "--all"),
        SyncRun.Job.ACTIVITYINFO_STRUCTURE,
        _("ActivityInfo structure"),
        _("Import ActivityInfo structure (current year)"),
        _(
            "Reads the forms, activities and indicators of every database of the current reporting "
            "year from ActivityInfo, in the background. One database at a time: select it in "
            "Databases and use its action instead."
        ),
        "account_tree",
    ),
    BackgroundJob(
        "run_import_ai_data",
        ("import_activityinfo_data", "--current-year"),
        SyncRun.Job.ACTIVITYINFO_DATA,
        _("ActivityInfo data"),
        _("Import ActivityInfo data (current year)"),
        _(
            "Runs the ActivityInfo export of every database of the current reporting year and replaces "
            "its activity records, in the background. It can take several minutes per database."
        ),
        "download",
    ),
    BackgroundJob(
        "run_link_partners",
        ("link_partners",),
        SyncRun.Job.PARTNER_LINKS,
        _("Link partners"),
        _("Link ActivityInfo partners to eTools"),
        _(
            "Links every ActivityInfo partner name to an eTools partner (same name, then programme "
            "document), in the background. It runs after every import; run it after renaming partners."
        ),
        "link",
    ),
    BackgroundJob(
        "run_sync_compiler_youth",
        ("sync_compiler_youth",),
        SyncRun.Job.COMPILER_YOUTH,
        _("Compiler youth figures"),
        _("Read the youth figures from Compiler"),
        _(
            "Reads from Compiler how many young people each youth indicator reached, per partner, "
            "donor and place (counts only, no personal data), in the background, then suggests links "
            "to the eTools indicators of the same programme documents."
        ),
        "groups",
    ),
    BackgroundJob(
        "run_sync_compiler_education",
        ("sync_compiler_education",),
        SyncRun.Job.COMPILER_EDUCATION,
        _("Compiler education figures"),
        _("Read the education figures from Compiler"),
        _(
            "Reads the Makani and Bridging counts Compiler prepared at night (children by partner, place, "
            "sex, age, services, attendance; no personal data), in the background. A year Compiler has "
            "not counted yet is counted there and arrives at the next run."
        ),
        "school",
    ),
    BackgroundJob(
        "run_sync_compiler_wellbeing",
        ("sync_compiler_wellbeing",),
        SyncRun.Job.COMPILER_WELLBEING,
        _("Makani wellbeing flags"),
        _("Read the Makani wellbeing flags from Compiler"),
        _(
            "Reads from Compiler the flags it worked out at night on Makani children who may need a "
            "follow-up (children by registration number only, no names) and the centre summaries, in "
            "the background. Only the flags changed since the last run are read."
        ),
        "favorite",
    ),
    BackgroundJob(
        "run_build_knowledge_hub",
        ("build_knowledge_hub",),
        SyncRun.Job.KNOWLEDGE_HUB,
        _("Knowledge hub"),
        _("Rebuild the knowledge hub"),
        _(
            "Reads new or changed library publications and CPD documents into the knowledge base, then "
            "links every partner, programme document, donor, grant, place, indicator, CPD result, Compiler "
            "programme and centre, document and review finding across sources, and records what changed "
            "since the previous build, in the background. It also runs on its own after every sync."
        ),
        "hub",
    ),
    BackgroundJob(
        "run_forecast_indicators",
        ("forecast_indicators",),
        SyncRun.Job.FORECAST,
        _("Year-end forecast"),
        _("Forecast the indicators' year-end values"),
        _(
            "Learns each ActivityInfo indicator's monthly pattern from the past years, checks how well "
            "it would have forecast them, then forecasts the current year: the likely year-end value, "
            "a range, and whether the target is likely to be reached. In the background."
        ),
        "trending_up",
    ),
    BackgroundJob(
        "run_ml_readiness",
        ("ml_readiness",),
        SyncRun.Job.ML_READINESS,
        _("Machine learning readiness"),
        _("Check the data's readiness for machine learning"),
        _(
            "Measures, for each source, how much history there is, how complete it is and how places, "
            "indicators and outcomes are recorded, and says for each programme decision whether a model "
            "could support it yet. Shown on the Data health page. Counts only; nothing is predicted."
        ),
        "query_stats",
    ),
    BackgroundJob(
        "run_whats_new_digest",
        ("whats_new_digest",),
        SyncRun.Job.WHATS_NEW,
        _("What's new note"),
        _("Write the what's new note"),
        _(
            "Writes today's what's new notes (one for everyone, one per section concerned) from the notable "
            "changes of the last 24 hours, and emails them to the people who asked, in the background."
        ),
        "campaign",
    ),
    BackgroundJob(
        "run_daily_review",
        ("daily_review",),
        SyncRun.Job.DAILY_REVIEW,
        _("Daily review"),
        _("Run the daily review"),
        _(
            "Runs the fourteen checks in the background and replaces today's review when it succeeds. "
            "It takes about a minute."
        ),
        "fact_check",
    ),
    BackgroundJob(
        "run_watch",
        ("run_watch", "--daily"),
        SyncRun.Job.WATCH,
        _("NeuroDB Watch"),
        _("Run NeuroDB Watch now"),
        _(
            "Runs the morning pass of NeuroDB Watch in the background: it checks what is due soon and what "
            "needs someone, updates each person's For you page and writes the morning notes. Running it "
            "again the same day tells nobody twice."
        ),
        "notifications",
    ),
    BackgroundJob(
        "run_fmm_refresh",
        ("fmm_refresh",),
        SyncRun.Job.FMM_REFRESH,
        _("Monitoring insights"),
        _("Refresh monitoring insights now"),
        _(
            "Rebuilds the visits, links and quality scores of Monitoring insights from the synced eTools "
            "data. It runs by itself after every eTools Datamart sync and each morning."
        ),
        "monitoring",
    ),
    BackgroundJob(
        "run_fmm_insights",
        ("fmm_insights",),
        SyncRun.Job.FMM_INSIGHTS,
        _("Monitoring insights (AI)"),
        _("Write the AI monitoring briefs now"),
        _(
            "Writes the AI briefs for the country and each section, as the morning run does. Briefs whose "
            "data has not changed are reused at no cost."
        ),
        "auto_awesome",
    ),
    BackgroundJob(
        "run_fmm_ai_checks",
        ("fmm_ai_checks",),
        SyncRun.Job.FMM_AI_CHECKS,
        _("Monitoring insights (AI checks)"),
        _("Run the AI checks of the quality rules now"),
        _(
            "Checks the records not checked yet against the AI quality rules (R3, R5, R6, R7, R8, R32...): "
            "this year's first, those of visits with several records first, within the day's budget for "
            "these checks, then recomputes the scores. What is left is checked on the following nights. It "
            "runs by itself each morning, after the refresh."
        ),
        "fact_check",
    ),
    BackgroundJob(
        "run_fmm_ap_review",
        ("fmm_ap_review",),
        SyncRun.Job.FMM_AP_REVIEW,
        _("Action points (AI review)"),
        _("Run the AI review of completed action points now"),
        _(
            "Asks the AI whether the action taken on each completed eTools action point resolves the issue "
            "raised (Adequately addressed, Partially addressed, Not addressed or Generic/vague), most "
            "recently completed first, within the day's budget for this review. Action points already "
            "reviewed are skipped unless their texts changed. It runs by itself each morning."
        ),
        "playlist_add_check",
    ),
    BackgroundJob(
        "run_doc_review",
        ("review_documents", "--pending"),
        SyncRun.Job.DOC_REVIEW,
        _("Review documents (pending)"),
        _("Review documents (pending)"),
        _(
            "Analyses the knowledge base documents put in a review batch that are waiting, failed or "
            "partly analysed: their findings, key statements and action points, within the day's budget "
            "for the document review. What the budget leaves waits for the next run. It runs by itself "
            "each night; nothing is analysed while the review is switched off (Document review settings)."
        ),
        "plagiarism",
    ),
    BackgroundJob(
        "run_doc_review_full",
        ("review_documents", "--full"),
        SyncRun.Job.DOC_REVIEW,
        _("Review documents (full)"),
        _("Review documents (full)"),
        _(
            "Analyses every document of the review batches again, as after a change to the prompts or the "
            "topics. People's verdicts, the findings they added and the action points' statuses are kept. "
            "It costs as much as the first analysis; what the day's budget leaves waits for the next run."
        ),
        "restart_alt",
    ),
    BackgroundJob(
        "run_doc_review_locate",
        ("review_documents", "--locate"),
        SyncRun.Job.DOC_REVIEW,
        _("Locate document findings"),
        _("Locate document findings again"),
        _(
            "Finds again, without AI, the page of each document finding, its place on the map and its "
            "evidence score, as after a change to the governorates or districts. It takes a few seconds."
        ),
        "pin_drop",
    ),
]


def _done(request) -> HttpResponse:
    url = reverse("admin:core_syncrun_changelist")
    if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _background_action(spec: BackgroundJob):
    @action(
        description=spec.menu,
        url_path=spec.name.replace("_", "-"),
        permissions=["run_sync"],
        icon=spec.icon,
        dialog={
            "title": spec.title,
            "description": spec.description,
            "form_class": ConfirmJobForm,
            "form_submit_text": _("Start"),
        },
    )
    def run(self, request, form):
        from neurodb.integrations import background

        if background.is_running(spec.job):
            messages.warning(request, _("%(job)s is already running.") % {"job": spec.title})
        else:
            background.start_command(*spec.command, "--triggered-by", request.user.get_username())
            messages.success(
                request, _("%(job)s started. Refresh this page to follow it.") % {"job": spec.title}
            )
        return _done(request)

    run.__name__ = spec.name
    return run


@action(
    description=_("Population figures"),
    url_path="run-reload-population",
    permissions=["run_sync"],
    icon="groups",
    dialog={
        "title": _("Reload population figures"),
        "description": _(
            "Reloads every year from the files shipped with NeuroDB, replacing their total and children "
            "figures. Vulnerable population figures entered in the admin are kept. It takes a few seconds."
        ),
        "form_class": ConfirmJobForm,
        "form_submit_text": _("Reload"),
    },
)
def run_reload_population(self, request, form):
    from neurodb.core.management.commands.load_population_figures import reload_bundled

    try:
        lines = reload_bundled(triggered_by=request.user.get_username())
    except Exception as exc:  # recorded as a failed run too
        messages.error(request, _("The reload failed: %(error)s") % {"error": exc})
    else:
        messages.success(request, "; ".join(lines) or _("No population file is shipped with NeuroDB."))
    return _done(request)


@action(
    description=_("Check freshness"),
    url_path="run-check-freshness",
    permissions=["run_sync"],
    icon="schedule",
    dialog={
        "title": _("Check data freshness"),
        "description": _(
            "Checks that the scheduled syncs (ActivityInfo data, eTools Datamart, locations) succeeded "
            "within SYNC_STALENESS_HOURS, as the hourly check does. It changes nothing."
        ),
        "form_class": ConfirmJobForm,
        "form_submit_text": _("Check"),
    },
)
def run_check_freshness(self, request, form):
    out = StringIO()
    try:
        call_command("check_sync_freshness", stdout=out)
    except CommandError as exc:
        messages.warning(request, _("Out of date: %(detail)s") % {"detail": exc})
    else:
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        messages.success(request, lines[-1] if lines else _("Every scheduled sync is fresh."))
    return _done(request)


@action(
    description=_("Repair roles"),
    url_path="run-bootstrap-roles",
    permissions=["run_sync"],
    icon="admin_panel_settings",
    dialog={
        "title": _("Repair user roles"),
        "description": _(
            "Creates the Viewer, Section editor and Administrator groups with their permissions if one "
            "is missing or was changed, and the Management group (who also gets the whole-country view "
            "on For you). Users keep their groups. Every deployment does this too."
        ),
        "form_class": ConfirmJobForm,
        "form_submit_text": _("Repair"),
    },
)
def run_bootstrap_roles(self, request, form):
    out = StringIO()
    call_command("bootstrap_roles", stdout=out)
    messages.success(request, out.getvalue().strip() or _("Roles ready."))
    return _done(request)


class JobActionsMixin:
    """Adds the "Run a job" menu (and its actions) to a ModelAdmin that defines has_run_sync_permission."""

    JOB_MENU = {
        "title": _("Run a job"),
        "icon": "play_arrow",
        "items": [
            *(spec.name for spec in BACKGROUND_JOBS),
            "run_reload_population",
            "run_check_freshness",
            "run_bootstrap_roles",
        ],
    }

    run_reload_population = run_reload_population
    run_check_freshness = run_check_freshness
    run_bootstrap_roles = run_bootstrap_roles


for _spec in BACKGROUND_JOBS:
    setattr(JobActionsMixin, _spec.name, _background_action(_spec))
