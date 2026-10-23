"""Page-shaping helpers owned by the web layer: search, data health, saved views, serialisation.

Domain services live in the other apps (facts, partnerships, library, core); this module only
combines them for the reports pages and the internal JSON API.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Count, F, Q, QuerySet, Window
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.core.models import SavedView, SyncRun
from neurodb.indicators.models import (
    Database,
    IndicatorNew,
    MasterIndicator,
    NeuroReport,
    ReportingYear,
    SubIndicator,
)
from neurodb.indicators.services.navigation import current_year
from neurodb.partnerships.models import PCA

MAP_LEVELS = ("governorate", "district", "cadaster", "site")
MAP_FILTERS = ("partner", "pd", "month", "governorate", "district", "indicator")
EMERGENCY_VALUES = ("yes", "no")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
POPULATION_VIEWS = ("total", "children", "vulnerable")
PD_SCOPES = ("active", "all")
STALE_DATABASE_DAYS = 40


# ------------------------------------------------------------------------- query-string parsing


def parse_month(raw: str | None) -> int | None:
    """'1'..'12' -> int; anything else -> None (the caller decides whether that is a 400)."""
    if raw in (None, ""):
        return None
    try:
        month = int(raw)
    except (TypeError, ValueError):
        return None
    return month if 1 <= month <= 12 else None


def parse_quarter(raw: str | None) -> str | None:
    return raw.upper() if raw and raw.upper() in QUARTERS else None


def resolve_year(raw: str | None) -> ReportingYear | None:
    if raw:
        found = ReportingYear.objects.filter(name=raw).first()
        if found:
            return found
    return current_year()


def map_filters(params) -> dict[str, str]:
    return {key: params.get(key, "").strip() for key in MAP_FILTERS if params.get(key, "").strip()}


# ------------------------------------------------------------------------- global search


SEARCH_GROUP_LIMIT = 200  # the items of one group listed by its "See all" link


def humanize_ai_name(name: str) -> str:
    """``"Children reached_Male_Lebanese"`` -> ``"Children reached · Male · Lebanese"``: ActivityInfo
    names join their breakdowns with underscores."""
    return " · ".join(part.strip() for part in (name or "").split("_") if part.strip()) or (name or "")


def _highlight(label: str, q: str) -> dict[str, str]:
    """The label split around the first match of ``q`` (the template marks the match)."""
    at = label.casefold().find(q.casefold())
    if at < 0:
        return {"before": label, "match": "", "after": ""}
    return {"before": label[:at], "match": label[at : at + len(q)], "after": label[at + len(q) :]}


def _join(*parts: Any) -> str:
    return " · ".join(str(part) for part in parts if part not in (None, ""))


def search(
    q: str,
    year: ReportingYear | None = None,
    limit: int = 8,
    group: str | None = None,
    user: Any = None,
) -> list[dict[str, Any]]:
    """What ``q`` names, by group: the pages of the menu ``user`` sees (*Pages*, the menu's own conditions),
    partners (name or short name), programme documents (number or title), country programme indicators (code
    or title), knowledge base documents (title), then the ActivityInfo databases, reports and indicators of
    every year, each with its year in the hint (``year``: of that reporting year alone). While ActivityInfo
    is an archive its groups say so ("ActivityInfo archive: Databases"). Partner staff are never searched, nor
    who added a document.

    Each group lists up to ``limit`` items with its ``total``; ``group`` lists that group alone, up to
    ``SEARCH_GROUP_LIMIT`` items (the "See all" link of a capped group). One query per group: its first
    ``limit`` rows with the number of all its matches.
    """
    from neurodb.cpd.models import Indicator as ProgrammeIndicator
    from neurodb.indicators.services import activityinfo, navigation
    from neurodb.knowledge.models import Document
    from neurodb.partnerships.models import PartnerOrganization
    from neurodb.web.templatetags.ui import day

    q = (q or "").strip()
    if len(q) < 2:
        return []
    state = activityinfo.state()
    every_year = year is None
    dbs = Database.objects.filter(display=True)
    if year is not None:
        dbs = dbs.filter(reporting_year=year)
    name_or_code = Q(name__icontains=q) | Q(awp_code__icontains=q)
    reports = NeuroReport.objects.filter(is_active=True, name__icontains=q)
    if year is not None:
        reports = reports.filter(ryear=year)

    def ai(label):
        """An ActivityInfo group's title: "ActivityInfo archive: …" while ActivityInfo is no longer in use."""
        return label if state.in_use else _("ActivityInfo archive: %(group)s") % {"group": label}

    def year_of(reporting_year) -> str:
        return reporting_year.name if every_year and reporting_year is not None else ""

    def pages() -> list[dict[str, Any]]:
        wanted = q.casefold()
        return [
            {"label": str(page["label"]), "hint": str(page["hint"]), "url": page["url"]}
            for page in navigation.pages(user, state)
            if wanted in str(page["label"]).casefold()
        ]

    # key, label, rows (a queryset, or a list already made), item builder
    sources: list[tuple[str, Any, Any, Any]] = [
        ("pages", _("Pages"), pages, None),
        (
            "partners",
            _("Partners"),
            PartnerOrganization.objects.filter(deleted_flag=False, hidden=False)
            .filter(Q(name__icontains=q) | Q(short_name__icontains=q))
            .only("id", "name", "short_name")
            .order_by("name", "id"),
            lambda p: {
                "label": p.name,
                "hint": p.short_name if p.short_name and p.short_name != p.name else "",
                "url": reverse("reports:partner_profile", args=[p.id]),
            },
        ),
        (
            "programmes",
            _("Programme documents"),
            PCA.objects.filter(Q(number__icontains=q) | Q(title__icontains=q))
            .only("id", "number", "title", "partner_name", "start")
            .order_by(F("start").desc(nulls_last=True), "number", "id"),
            lambda pd: {
                "label": _join(pd.number, pd.title),
                "hint": pd.partner_name or "",
                "url": reverse("reports:programme_detail", args=[pd.id]),
            },
        ),
        (
            "cpd",
            _("Country programme indicators"),
            ProgrammeIndicator.objects.filter(Q(code__icontains=q) | Q(title__icontains=q))
            .select_related("programme")
            .only("id", "code", "title", "programme__name", "programme__start_year")
            .order_by("-programme__start_year", "code", "id"),
            lambda i: {
                "label": _join(i.code, i.title),
                "hint": i.programme.name,
                "url": reverse("cpd:indicator", args=[i.id]),
            },
        ),
        (
            "documents",
            _("Knowledge base"),
            Document.objects.filter(title__icontains=q)
            .only("id", "title", "document_date", "created_at", "origin")
            .order_by(F("document_date").desc(nulls_last=True), "-created_at", "-id"),
            lambda d: {
                "label": d.title,
                # the date and the kind of document, never who added it
                "hint": _join(
                    day(d.document_date or timezone.localtime(d.created_at)), d.get_origin_display()
                ),
                "url": reverse("knowledge:detail", args=[d.id]),
            },
        ),
        (
            "databases",
            ai(_("Databases")),
            dbs.filter(Q(name__icontains=q) | Q(label__icontains=q))
            .select_related("section", "reporting_year")
            .order_by("-reporting_year__name", "name", "id"),
            lambda d: {
                "label": d.label or d.name,
                "hint": _join(
                    d.section.name if d.section and d.section.name != (d.label or d.name) else "",
                    year_of(d.reporting_year),
                ),
                "url": reverse("reports:database_dashboard", args=[d.id]),
            },
        ),
        (
            "reports",
            ai(_("Reports")),
            reports.select_related("ryear").order_by("-ryear__name", "name", "id"),
            lambda r: {
                "label": r.name,
                "hint": _join(_("HPM report") if r.is_hpm else _("Neuro report"), year_of(r.ryear)),
                "url": reverse("reports:report_hpm" if r.is_hpm else "reports:report_dashboard", args=[r.id]),
            },
        ),
        (
            "masters",
            ai(_("Master indicators")),
            MasterIndicator.objects.filter(database__in=dbs, is_active=True)
            .filter(name_or_code)
            .select_related("database", "database__reporting_year")
            .order_by("-database__reporting_year__name", "database__name", "sequence", "id"),
            lambda m: {
                "label": m.name,
                "hint": _join(
                    m.awp_code, m.database.label or m.database.name, year_of(m.database.reporting_year)
                ),
                "url": reverse("reports:database_dashboard", args=[m.database_id]) + f"#indicator-{m.id}",
            },
        ),
        (
            "subs",
            ai(_("Sub-indicators")),
            SubIndicator.objects.filter(database__in=dbs)
            .filter(name_or_code)
            .select_related("database", "database__reporting_year")
            .order_by("-database__reporting_year__name", "database__name", "awp_code", "id"),
            lambda s: {
                "label": humanize_ai_name(s.name),
                "hint": _join(
                    s.awp_code, s.database.label or s.database.name, year_of(s.database.reporting_year)
                ),
                "url": reverse("reports:database_analytical", args=[s.database_id]),
            },
        ),
        (
            "indicators",
            _("ActivityInfo indicators"),
            IndicatorNew.objects.filter(database__in=dbs)
            .filter(name_or_code)
            .select_related("database", "database__reporting_year")
            .order_by("-database__reporting_year__name", "database__name", "awp_code", "id"),
            lambda i: {
                "label": humanize_ai_name(i.name),
                "hint": _join(
                    i.awp_code, i.database.label or i.database.name, year_of(i.database.reporting_year)
                ),
                "url": reverse("reports:database_analytical", args=[i.database_id]),
            },
        ),
    ]
    groups: list[dict[str, Any]] = []
    for key, label, rows, build in sources:
        if group and key != group:
            continue
        cap = SEARCH_GROUP_LIMIT if group else limit
        if callable(rows) and build is None:  # made in Python (the menu's pages)
            found = rows()
            items, total = found[:cap], len(found)
        else:
            # the group's first rows and how many match in all, in one query (COUNT(*) OVER ())
            fetched = list(rows.annotate(found_in_all=Window(Count("pk")))[:cap])
            items = [build(obj) for obj in fetched]
            total = fetched[0].found_in_all if fetched else 0
        if not items:
            continue
        for item in items:
            item.update(_highlight(str(item["label"]), q))
        groups.append({"key": key, "label": label, "items": items, "total": total})
    return groups


# ------------------------------------------------------------------------- data health


def data_health() -> dict[str, Any]:
    """Freshness of every job and of every displayed ActivityInfo database of :func:`health_year`, the latest
    runs, the eTools Datamart datasets and, per Compiler programme, how its latest period compares with the
    view Power BI reads (``power_bi``: counts only). The Data health page asks for each block in a part of its
    own (:func:`health_jobs`, :func:`health_databases`, :func:`recent_runs`, :func:`datamart_quality`,
    ``education.power_bi.health``); the assistant, the daily review and the internal API read them all at
    once here."""
    from neurodb.education import power_bi
    from neurodb.indicators.services import activityinfo

    now = timezone.now()
    state = activityinfo.state()
    archived = not state.watched
    jobs = health_jobs(now, archived=archived)
    year = health_year()
    databases = health_databases(year, now, archived=archived)
    datasets = datamart_quality()
    return {
        "datasets": datasets,
        "jobs": jobs,
        "runs": recent_runs(),
        "databases": databases,
        "staleness_hours": settings.SYNC_STALENESS_HOURS,
        "stale_database_days": STALE_DATABASE_DAYS,
        "year": year,
        # ActivityInfo's import is no longer followed: its jobs and databases are an archive, never stale
        "activityinfo_archived": archived,
        # its scheduled job is still switched on: an archive does not stop the import, it still runs
        "activityinfo_import_on": state.schedule_on,
        # with the registrations in rounds that belong to no year (Makani, Dirasa): why a figure may differ
        "power_bi": power_bi.summary(with_no_year=True),
    }


# Jobs worked out from the synced data, not syncs that go stale: weekly analyses, NeuroDB Watch and
# Monitoring insights, shown on their own pages, and the clean-up run once after a deployment.
HEALTH_SKIPPED_JOBS = (
    SyncRun.Job.ML_READINESS,
    SyncRun.Job.FORECAST,
    SyncRun.Job.WATCH,
    SyncRun.Job.FMM_REFRESH,
    SyncRun.Job.FMM_INSIGHTS,
    SyncRun.Job.FMM_AI_CHECKS,
    SyncRun.Job.FMM_AP_REVIEW,
    SyncRun.Job.DOC_REVIEW,
    SyncRun.Job.DATAMART_BACKFILL,  # once after a deployment: it never goes stale
    SyncRun.Job.REPORTING_CHECKS,  # the eTools reporting checks: their own page says when they ran
    SyncRun.Job.DATAMART_PROBE,  # a probe of one Datamart dataset, started by hand: never stale
    SyncRun.Job.ETOOLS_MASTERS,  # the master indicators' suggestions: worked out from the synced data
)


def health_jobs(now: datetime.datetime | None = None, archived: bool | None = None) -> list[dict[str, Any]]:
    """Per sync job: its last success, its latest run, and whether it is fresh, stale or never
    succeeded (stale after ``SYNC_STALENESS_HOURS``). While ActivityInfo's import is not followed
    (``archived``, default: :func:`neurodb.indicators.services.activityinfo.watched` is false), its jobs are
    "Not in use (archive)", never stale nor failing: nobody runs them for a year partners report in. The
    eTools Datamart sync also has ``datasets`` (:func:`datamart_sync_state`): it writes one run per dataset,
    so its latest run is only the last dataset of the order."""
    from neurodb.indicators.services import activityinfo

    now = now or timezone.now()
    if archived is None:
        archived = not activityinfo.watched()
    staleness = datetime.timedelta(hours=settings.SYNC_STALENESS_HOURS)
    jobs = []
    for job, label in SyncRun.Job.choices:
        if job in HEALTH_SKIPPED_JOBS:
            continue
        last_ok = SyncRun.last_success(job)
        last_run = SyncRun.objects.filter(job=job).order_by("-started_at").first()
        age = (now - last_ok.finished_at) if last_ok and last_ok.finished_at else None
        if archived and job in activityinfo.AI_JOBS:
            state, state_label = "archived", _("Not in use (archive)")
        elif last_ok is None:
            state, state_label = "unknown", _("Never succeeded")
        elif age is not None and age > staleness:
            state, state_label = "stale", _("Stale")
        else:
            state, state_label = "fresh", _("Fresh")
        entry = {
            "job": job,
            "label": label,
            "last_success": last_ok,
            "last_run": last_run,
            "age_hours": round(age.total_seconds() / 3600, 1) if age else None,
            "state": state,
            "state_label": state_label,
        }
        if job == SyncRun.Job.ETOOLS_DATAMART:
            entry["datasets"] = datamart_sync_state()
        jobs.append(entry)
    return jobs


def health_year() -> ReportingYear | None:
    """The reporting year whose ActivityInfo databases Data health lists: while the import's alarms are on
    (:func:`neurodb.indicators.services.activityinfo.watched`), the year the import serves, or the year ticked
    current while its new databases wait for their first records (the import reads that one); otherwise the
    last ActivityInfo year, the archive's newest. The year ticked current when neither has one."""
    from neurodb.indicators.services import activityinfo

    state = activityinfo.state()
    if state.watched:
        if state.setup_pending:
            return current_year()
        return activityinfo.reporting_year_for(state.serving) or current_year()
    return activityinfo.reporting_year_for(state.last) or current_year()


def health_databases(
    year: ReportingYear | None, now: datetime.datetime | None = None, archived: bool | None = None
) -> list[dict[str, Any]]:
    """Per displayed ActivityInfo database of ``year``: its last import, and whether it is fresh, stale
    (older than ``STALE_DATABASE_DAYS``) or never imported. While ActivityInfo's import is not followed
    (``archived``, default: :func:`neurodb.indicators.services.activityinfo.watched` is false) every database
    is "Archive": kept as it was, no longer followed (its import may still run), so never stale."""
    from neurodb.indicators.services import activityinfo

    now = now or timezone.now()
    if archived is None:
        archived = not activityinfo.watched()
    databases = []
    for db in (
        Database.objects.filter(reporting_year=year, display=True)
        .select_related("section")
        .order_by("section__name", "name")
    ):
        last = db.last_monthly_update_date
        stale = bool(last and (now - last).days > STALE_DATABASE_DAYS)
        if archived:
            state, state_label = "archive", _("Archive")
        elif last is None:
            state, state_label = "unknown", _("Never imported")
        else:
            state, state_label = ("stale", _("Stale")) if stale else ("fresh", _("Fresh"))
        databases.append({"database": db, "last_import": last, "state": state, "state_label": state_label})
    return databases


def recent_runs(limit: int = 50) -> list[SyncRun]:
    """The latest runs of every job, newest first."""
    return list(SyncRun.objects.order_by("-started_at")[:limit])


LEGACY_TABLES = {
    "partners": "etools.PartnerOrganization",
    "interventions": "etools.PCA",
    "intervention_budgets": "etools.PCA",
    "agreements": "etools.Agreement",
    "locations": "locations.Location",
    "location_sites": "datamart.MonitoringSite",
}
# What a sync could not link, as the data health table names it (other codes are shown as written)
NOT_LINKED_LABELS = {"programme_document_fm": "programme document (field monitoring)"}
# Where the business area code of a PRP dataset's run came from (details.business_area_from), in words;
# "workspaces (held)" is how the probe runs made before the sync read the workspace held named it
BUSINESS_AREA_FROM = {
    "environment": _("set by the hosting team"),
    "admin": _("entered in the admin"),
    "workspaces": _("found in the Datamart's workspaces"),
    "workspace_held": _("found in the workspace NeuroDB holds"),
    "workspaces (held)": _("found in the workspace NeuroDB holds"),
    "partner_reports": _("the code most partner report rows carry"),
}
# Where a value set aside as not a business area code came from (details.business_area_set_aside)
SET_ASIDE_FROM = {"environment": "ETOOLS_DATAMART_BUSINESS_AREA", "admin": _("the code entered in the admin")}
# Why a read was made in windows (details.windows_why, windows.Reading): a round count, or a cut sign of the
# dataset's previous finished run (datamart_sync.cut_sign), in words (Data health and check 10)
WINDOWS_WHY = {
    "round_count": _("the Datamart announced a round count, a limit of its answer"),
    "cap_suspected": _("the last read looked cut"),
    "whole_count_capped": _("the last read's windows held more than the Datamart's count"),
    "pages_cut": _("a page of the last read came back shorter than the first"),
    "windows_cut": _("a window of the last read held fewer records than it announced"),
    "read_short": _("the last read returned fewer records than announced"),
}
# The rows each dataset holds are counted once an hour at most, and again as soon as one of the jobs that
# write those tables starts or ends a run (the eTools Datamart sync above all). The gazetteer is the one
# table staff also change by hand (Admin → Locations), outside any job: it is counted at each visit.
DATASET_ROWS_TTL = 60 * 60
DATASET_ROWS_JOBS = (
    SyncRun.Job.ETOOLS_DATAMART,
    SyncRun.Job.ETOOLS,
    SyncRun.Job.LOCATIONS,
    SyncRun.Job.DATAMART_BACKFILL,
)
DATASET_ROWS_LIVE = ("locations",)


def latest_datamart_runs(names, *, finished: bool = False) -> dict[str, SyncRun]:
    """The latest eTools Datamart run of each dataset in ``names`` (by start, then id), in one query: for
    each name, its first run on the index ``core_syncrun_job_target`` (job, target, newest start first). A
    ``DISTINCT ON (target)`` would read and sort every Datamart run (on a year of runs: 30 ms, not 1).
    ``finished``: the latest run that is not running (a dataset being synced keeps its last state)."""
    table = connection.ops.quote_name(SyncRun._meta.db_table)
    params = [list(names), SyncRun.Job.ETOOLS_DATAMART.value]
    not_running = ""
    if finished:
        not_running = "AND s.status <> %s "
        params.append(SyncRun.Status.RUNNING.value)
    latest = (
        "SELECT r.* FROM unnest(%s::text[]) AS t(target) CROSS JOIN LATERAL ("  # noqa: S608 - the model's table
        f"SELECT s.* FROM {table} s WHERE s.job = %s AND s.target = t.target {not_running}"
        "ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r"
    )
    runs = SyncRun.objects.raw(latest, params)
    return {run.target: run for run in runs}


@dataclass(frozen=True)
class DatamartSyncState:
    """The eTools Datamart sync dataset by dataset: ``sync_all`` writes one run per dataset and goes on after
    a failed one, so the job's latest run is only the last dataset of the order (``datamart_etl_status``).
    Every reader of the job as a whole (Needs attention and NeuroDB Watch, the admin home's Data jobs, the
    Scheduled jobs list, the help chat, Data health's job card) reads this instead."""

    runs: dict[str, SyncRun]  # each dataset of ENTITY_SYNCS -> its latest finished run, in sync order
    failed: tuple[str, ...] = ()  # datasets whose latest finished run failed, the diagnostic ones left out
    diagnostic_failed: tuple[str, ...] = ()  # the same for the diagnostic ones (catalogue.DIAGNOSTIC)
    partial: tuple[str, ...] = ()  # like failed, for a latest finished run that wrote with errors
    counted: int = 0  # the datasets with a finished run, the diagnostic ones left out

    @property
    def status(self) -> str:
        """Failed (every dataset counted failed), partial (some failed, or none failed and some wrote with
        errors) or succeeded; "" while no dataset counted has a finished run (only a diagnostic one, or none):
        a reader then shows the job's latest run as it is."""
        if not self.counted:
            return ""
        if self.failed:
            return SyncRun.Status.FAILED if len(self.failed) == self.counted else SyncRun.Status.PARTIAL
        return SyncRun.Status.PARTIAL if self.partial else SyncRun.Status.SUCCEEDED

    @property
    def label(self) -> str:
        """The status in words: Succeeded, Succeeded with errors, "2 datasets failed" or Failed."""
        if self.status == SyncRun.Status.PARTIAL and self.failed:
            n = len(self.failed)
            return ngettext("%(n)s dataset failed", "%(n)s datasets failed", n) % {"n": n}
        return str(SyncRun.Status(self.status).label) if self.status else ""

    @property
    def named(self) -> tuple[str, ...]:
        """The datasets the status is about: those that failed, else those that wrote with errors."""
        return self.failed or self.partial

    def runs_url(self) -> str:
        """The list of eTools Datamart runs, of the datasets the status names only when it names some (each
        one's latest run on top): never every failed run of the job, most of them long since followed by a
        success."""
        url = reverse("admin:core_syncrun_changelist") + "?job__exact=etools_datamart"
        return url + "&target__in=" + ",".join(self.named) if self.named else url


def datamart_sync_state() -> DatamartSyncState:
    """The eTools Datamart sync as its datasets: the latest finished run of each (one query), which failed or
    wrote with errors, and which of the failed ones are diagnostic. A dataset not synced again keeps its last
    state; a dataset dropped from the sync is left out."""
    from neurodb.datamart.catalogue import DIAGNOSTIC
    from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

    latest = latest_datamart_runs(ENTITY_SYNCS, finished=True)
    runs = {name: latest[name] for name in ENTITY_SYNCS if name in latest}
    failed = [name for name, run in runs.items() if run.status == SyncRun.Status.FAILED]
    partial = [name for name, run in runs.items() if run.status == SyncRun.Status.PARTIAL]
    return DatamartSyncState(
        runs=runs,
        failed=tuple(name for name in failed if name not in DIAGNOSTIC),
        diagnostic_failed=tuple(name for name in failed if name in DIAGNOSTIC),
        partial=tuple(name for name in partial if name not in DIAGNOSTIC),
        counted=sum(1 for name in runs if name not in DIAGNOSTIC),
    )


def _dataset_rows(name: str) -> int | None:
    """How many rows NeuroDB holds for one dataset; None for a dataset without a table of its own."""
    from django.apps import apps

    from neurodb.datamart import query as dmq

    if name in LEGACY_TABLES:
        return apps.get_model(LEGACY_TABLES[name]).objects.count()
    try:
        return dmq.dataset(name).base.count()
    except Exception:  # a dataset without a table of its own
        return None


def _dataset_rows_key() -> str:
    """The stored row counts' key: the latest run of each job that writes the counted tables, with its
    status and end, so a run that starts or ends gives a new key.

    It is read at every visit of the datasets part, so through the index (job, status, newest start
    first): the latest run of each job and status, then the latest of those per job. Per job alone, the
    index would be walked to its end for a job without recent runs (the clean-up job); sorting every run of
    these jobs takes 30 ms on a year of runs."""
    table = connection.ops.quote_name(SyncRun._meta.db_table)
    query = (
        "SELECT DISTINCT ON (r.job) r.job, r.id, r.status, r.finished_at "  # noqa: S608 - the model's table
        "FROM unnest(%s::text[]) AS j(job) CROSS JOIN unnest(%s::text[]) AS st(status) CROSS JOIN LATERAL ("
        f"SELECT s.job, s.id, s.status, s.finished_at, s.started_at FROM {table} s "
        "WHERE s.job = j.job AND s.status = st.status ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r "
        "ORDER BY r.job, r.started_at DESC, r.id DESC"
    )
    with connection.cursor() as cursor:
        cursor.execute(query, [[job.value for job in DATASET_ROWS_JOBS], list(SyncRun.Status.values)])
        latest = cursor.fetchall()
    stamp = ";".join(
        f"{job}={pk}.{status}.{finished.timestamp() if finished else '-'}"
        for job, pk, status, finished in latest
    )
    return f"health:dataset-rows:{stamp or '-'}"


def dataset_row_counts() -> dict[str, int | None]:
    """``{dataset: rows NeuroDB holds}`` for every eTools Datamart dataset: stored for every worker
    (``compute.once``, an hour at most, keyed on the jobs that write the tables), the gazetteer counted at
    each visit. Plain numbers, the same in every language and for every user."""
    from neurodb.core import compute
    from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

    names = list(ENTITY_SYNCS)

    def count() -> dict[str, int | None]:
        return {name: _dataset_rows(name) for name in names if name not in DATASET_ROWS_LIVE}

    # inside a transaction (NeuroDB Watch's AI look-ups run in a read-only one) once() reads the stored counts
    # and, when there are none, counts without storing
    stored = compute.once(_dataset_rows_key(), count, DATASET_ROWS_TTL, lang_neutral=True)
    return {name: stored[name] if name in stored else _dataset_rows(name) for name in names}


def _window_bound(text: Any) -> str:
    """A window's bound as Data health shows it: "1 Jan 2026" for a date, "open" for the open end, an id (or a
    day and an id) as it is."""
    text = str(text or "")
    try:
        day = datetime.date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        day = None
    return f"{day.day} {day:%b %Y}" if day else text or "open"


def datamart_quality() -> list[dict[str, Any]]:
    """Per eTools Datamart dataset: the last run, what it read, wrote, failed to link, and how many
    rows NeuroDB holds — the table to read before believing a number on an eTools page. A failed run's
    error is shown when it lists no row errors, and a diagnostic dataset (``catalogue.DIAGNOSTIC``, used by
    no page, figure or check) is marked as such. A PRP dataset's run names the business area code it read
    with and where the code came from (a value that is not a code, a code NeuroDB found, the values set
    aside), and the records that named no business area; a run that read fewer records than the Datamart
    announced, some twice, or a round count (a limit of the Datamart's answer), says that it removed
    nothing. A large dataset read in windows says how many, why, what they announced and read, those not read
    whole (the first three), a count that changed during the read, and a removal held one night; a page cut
    short says so. The partner reports and PRP's progress reports say which business area filter their read
    took, or why none, and the records of another business area a filter let through. Records that came twice
    say whether the pages shifted (and as many others did not come) or, in a stable order, the Datamart likely
    holds them twice, and the stable order asked or why none."""
    from neurodb.datamart.catalogue import DIAGNOSTIC
    from neurodb.integrations.etools.datamart_sync import (
        ENTITY_SYNCS,
        FOUND,
        area_words,
        is_area_code,
        not_a_code,
        shift_words,
    )

    last_by_target = latest_datamart_runs(ENTITY_SYNCS)
    counts = dataset_row_counts()
    rows = []
    for name in ENTITY_SYNCS:
        run = last_by_target.get(name)
        details = (run.details or {}) if run else {}
        count = counts[name]
        failed = run is not None and run.status == SyncRun.Status.FAILED
        not_linked = {NOT_LINKED_LABELS.get(k, k): v for k, v in (details.get("not_linked") or {}).items()}
        code = str(details.get("business_area") or "")
        aside = details.get("business_area_set_aside")
        aside = aside if isinstance(aside, dict) else {}
        shifted = details.get("pages_shifted") if isinstance(details.get("pages_shifted"), dict) else {}
        ordering = details.get("ordering") if isinstance(details.get("ordering"), dict) else {}
        rows.append(
            {
                "name": name,
                "run": run,
                "rows": count,
                "not_linked": not_linked,
                "not_linked_total": sum(int(v) for v in not_linked.values()),
                "removed": details.get("removed"),
                "empty_response": bool(details.get("empty_response")),
                # records of another country the sync left out, and rows of another country it deleted
                "other_country_skipped": details.get("other_country_skipped"),
                "other_country_removed": details.get("other_country_removed"),
                "errors": (details.get("errors") or [])[:3],
                "relinked": details.get("relinked"),
                # the run's own error, when it stopped before listing row errors (an HTTP 500 of the Datamart)
                "error": run.error if failed and not details.get("errors") else "",
                "diagnostic": name in DIAGNOSTIC,  # no page, figure or check uses it: never an alarm
                # a PRP dataset's code and its source, in words (it carries no country name)
                "business_area": details.get("business_area") or "",
                "business_area_from": BUSINESS_AREA_FROM.get(details.get("business_area_from"), ""),
                # a code asked that is not one (a country's name, before the sync set such a value aside), in
                # words; the values a later sync set aside; a code NeuroDB found rather than one a person set
                "business_area_not_code": not_a_code(code) if code and not is_area_code(code) else "",
                "business_area_set_aside": "; ".join(
                    f"{SET_ASIDE_FROM.get(k, k)} “{v}”" for k, v in aside.items()
                ),
                "business_area_found": details.get("business_area_from") in FOUND,
                # the records read (kept and skipped, each once) and the count the Datamart announced; fewer
                # read than announced, or some returned twice: nothing was removed
                "read": details.get("read"),
                "datamart_count": details.get("datamart_count"),
                "removal_skipped": bool(details.get("removal_skipped")),
                "duplicates_read": details.get("duplicates_read"),
                # records that came twice: pages that shifted (others then missing) or, in a stable order,
                # records the Datamart likely holds twice; the stable order asked or why none; in words
                "pages_shifted_twice": shifted.get("twice"),
                "pages_shifted_missing": shifted.get("missing"),
                "held_twice": details.get("held_twice"),
                "ordering_key": ordering.get("key") or "",
                "ordering_why": ordering.get("why") or "",
                "shift_words": shift_words(details),
                # why the answer looks cut (a round count read in one answer, a cut the ends test found...)
                "cap_suspected": details.get("cap_suspected") or "",
                # read in windows: how many, what they announced (the Datamart's own count may be cut), those
                # not read whole (the first three: from, to, announced, read), and a count that moved
                "windows_read": details.get("windows_read"),
                "windows_announced": details.get("windows_announced"),
                "windows_not_whole": details.get("windows_not_whole"),
                "windows_not_whole_first": [
                    {
                        "from": _window_bound(w.get("from")),
                        "to": _window_bound(w.get("to")),
                        "announced": w.get("announced"),
                        "read": w.get("read"),
                    }
                    for w in (details.get("windows_not_whole_first") or [])[:3]
                    if isinstance(w, dict)
                ],
                "whole_count_capped": bool(details.get("whole_count_capped")),
                "whole_count_end": details.get("whole_count_end"),
                "changed": details.get("whole_count_end") is not None
                and details.get("whole_count_end") != details.get("datamart_count"),
                # why the read was made in windows (a round count, a cut sign of the last read), in words,
                # and the pages that came back shorter than the first (the answer was cut)
                "windows_why": WINDOWS_WHY.get(details.get("windows_why"), ""),
                "pages_cut": details.get("pages_cut"),
                # the business area filter the read took, or why none (catalogue.AREA_FILTERS), and the
                # records of another business area it let through
                "area_filter": details.get("area_filter")
                if isinstance(details.get("area_filter"), dict)
                else {},
                "area_words": area_words(details.get("area_filter")),
                "area_strays": details.get("area_strays"),
                # a whole read that would remove more than half of the rows held waits one night
                "removal_held": details.get("removal_held"),
                # an empty read the Datamart announced records for: its answer was cut short
                "cut_short": bool(details.get("empty_response"))
                and isinstance(details.get("datamart_count"), int)
                and details["datamart_count"] > (details.get("read") or 0),
                # PRP records naming no business area: kept unconfirmed, or left out as no PRP programme
                # document NeuroDB holds names them
                "country_unnamed": details.get("country_unnamed"),
                "unnamed_skipped": details.get("unnamed_skipped"),
            }
        )
    return rows


def _last_status(job: dict[str, Any]) -> str | None:
    """The job's latest status; the eTools Datamart sync's read from every dataset's latest finished run."""
    if job.get("datasets") and job["datasets"].status:
        return str(job["datasets"].status)
    return job["last_run"].status if job["last_run"] else None


def health_as_dict(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobs": [
            {
                "job": j["job"],
                "label": j["label"],
                "state": j["state"],
                "last_success": j["last_success"].finished_at.isoformat() if j["last_success"] else None,
                "last_status": _last_status(j),
                # the eTools Datamart datasets whose latest finished run failed, the diagnostic ones left out
                "failed_datasets": list(j["datasets"].failed) if j.get("datasets") else [],
                "age_hours": j["age_hours"],
            }
            for j in health["jobs"]
        ],
        "databases": [
            {
                "id": d["database"].id,
                "name": d["database"].label or d["database"].name,
                "state": d["state"],
                "last_import": d["last_import"].isoformat() if d["last_import"] else None,
            }
            for d in health["databases"]
        ],
        # the reporting year whose databases are listed, and whether ActivityInfo is an archive (its jobs
        # "archived", its databases "archive": no longer followed, never stale) and whether its import's
        # scheduled job is still switched on (an archive does not stop it: it still runs)
        "databases_year": health["year"].name if health.get("year") else None,
        "activityinfo_archived": bool(health.get("activityinfo_archived")),
        "activityinfo_import_on": bool(health.get("activityinfo_import_on")),
        "staleness_hours": health["staleness_hours"],
        # per Compiler programme, its latest period compared with the view Power BI reads (counts only)
        "power_bi": [
            {**row, "checked_at": row["checked_at"].isoformat() if row.get("checked_at") else None}
            for row in health.get("power_bi", [])
        ],
    }


# ------------------------------------------------------------------------- saved views


def saved_views_for(user, page: str, object_id: int | None = None) -> QuerySet[SavedView]:
    qs = SavedView.objects.filter(Q(owner=user) | Q(is_shared=True), page=page).select_related("owner")
    if object_id is not None:
        qs = qs.filter(object_id=object_id)
    return qs.order_by("name")


def can_delete_saved_view(user, view: SavedView) -> bool:
    return view.owner_id == user.id or role_of(user) == ADMIN


def saved_view_as_dict(view: SavedView, user=None) -> dict[str, Any]:
    return {
        "id": view.id,
        "name": view.name,
        "page": view.page,
        "object_id": view.object_id,
        "query": view.query,
        "layout": view.layout,
        "is_shared": view.is_shared,
        "owner": view.owner.get_username(),
        "mine": user is not None and view.owner_id == user.id,
        "updated_at": view.updated_at.isoformat() if view.updated_at else None,
    }


# ------------------------------------------------------------------------- programme documents


def pd_as_dict(pd: PCA, interventions: dict[str, int] | None = None) -> dict[str, Any]:
    prefix = (pd.number or "").split("-")[0]
    return {
        "id": pd.id,
        "number": pd.number,
        "title": pd.title,
        "partner_name": pd.partner_name,
        "document_type": pd.document_type,
        "status": pd.status,
        "start": pd.start.isoformat() if pd.start else None,
        "end": pd.end.isoformat() if pd.end else None,
        "sections": pd.section_names or [],
        "offices": pd.offices_set or [],
        "donors": pd.donors or [],
        "grants": pd.grants or [],
        "total_budget": pd.total_budget,
        "planned_locations": len(pd.location_p_codes or []),
        "interventions": (interventions or {}).get(prefix, 0),
    }


def report_databases(report: NeuroReport) -> QuerySet[Database]:
    return (
        Database.objects.filter(masterindicator__neuroreportmasterindicator__report=report)
        .distinct()
        .select_related("section")
        .order_by("section__name", "name")
    )
