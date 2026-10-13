"""Page-shaping helpers owned by the web layer: search, data health, saved views, serialisation.

Domain services live in the other apps (facts, partnerships, library, core); this module only
combines them for the reports pages and the internal JSON API.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

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


def search(
    q: str, year: ReportingYear | None, limit: int = 8, group: str | None = None
) -> list[dict[str, Any]]:
    """Indicators, databases and reports of ``year`` whose name or code contains ``q``.

    Each group lists up to ``limit`` items with its ``total``; ``group`` lists that group alone, up to
    ``SEARCH_GROUP_LIMIT`` items (the "See all" link of a capped group).
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    dbs = Database.objects.filter(reporting_year=year, display=True) if year else Database.objects.none()
    db_ids = list(dbs.values_list("id", flat=True))
    name_or_code = Q(name__icontains=q) | Q(awp_code__icontains=q)
    reports = NeuroReport.objects.filter(ryear=year, is_active=True, name__icontains=q) if year else None

    # key, label, queryset, item builder
    sources = [
        (
            "databases",
            _("Databases"),
            dbs.filter(Q(name__icontains=q) | Q(label__icontains=q))
            .select_related("section")
            .order_by("name"),
            lambda d: {
                "label": d.label or d.name,
                "hint": d.section.name if d.section and d.section.name != (d.label or d.name) else "",
                "url": reverse("reports:database_dashboard", args=[d.id]),
            },
        ),
        (
            "reports",
            _("Reports"),
            reports.order_by("name") if reports is not None else NeuroReport.objects.none(),
            lambda r: {
                "label": r.name,
                "hint": _("HPM report") if r.is_hpm else _("Neuro report"),
                "url": reverse("reports:report_hpm" if r.is_hpm else "reports:report_dashboard", args=[r.id]),
            },
        ),
        (
            "masters",
            _("Master indicators"),
            MasterIndicator.objects.filter(database_id__in=db_ids, is_active=True)
            .filter(name_or_code)
            .select_related("database")
            .order_by("database__name", "sequence", "id"),
            lambda m: {
                "label": m.name,
                "hint": f"{m.awp_code} · {m.database.label or m.database.name}",
                "url": reverse("reports:database_dashboard", args=[m.database_id]) + f"#indicator-{m.id}",
            },
        ),
        (
            "subs",
            _("Sub-indicators"),
            SubIndicator.objects.filter(database_id__in=db_ids)
            .filter(name_or_code)
            .select_related("database")
            .order_by("database__name", "awp_code", "id"),
            lambda s: {
                "label": humanize_ai_name(s.name),
                "hint": f"{s.awp_code} · {s.database.label or s.database.name}",
                "url": reverse("reports:database_analytical", args=[s.database_id]),
            },
        ),
        (
            "indicators",
            _("ActivityInfo indicators"),
            IndicatorNew.objects.filter(database_id__in=db_ids)
            .filter(name_or_code)
            .select_related("database")
            .order_by("database__name", "awp_code", "id"),
            lambda i: {
                "label": humanize_ai_name(i.name),
                "hint": f"{i.awp_code or ''} · {i.database.label or i.database.name}",
                "url": reverse("reports:database_analytical", args=[i.database_id]),
            },
        ),
    ]
    groups: list[dict[str, Any]] = []
    for key, label, queryset, build in sources:
        if group and key != group:
            continue
        cap = SEARCH_GROUP_LIMIT if group else limit
        items = [build(obj) for obj in queryset[:cap]]
        if not items:
            continue
        for item in items:
            item.update(_highlight(item["label"], q))
        total = len(items) if len(items) < cap else queryset.count()
        groups.append({"key": key, "label": label, "items": items, "total": total})
    return groups


# ------------------------------------------------------------------------- data health


def data_health() -> dict[str, Any]:
    """Freshness of every job and of every displayed database of the current year, the latest runs and
    the eTools Datamart datasets. The Data health page asks for each block in a part of its own
    (:func:`health_jobs`, :func:`health_databases`, :func:`recent_runs`, :func:`datamart_quality`); the
    assistant, the daily review and the internal API read them all at once here."""
    now = timezone.now()
    jobs = health_jobs(now)
    year = current_year()
    databases = health_databases(year, now)
    datasets = datamart_quality()
    return {
        "datasets": datasets,
        "jobs": jobs,
        "runs": recent_runs(),
        "databases": databases,
        "staleness_hours": settings.SYNC_STALENESS_HOURS,
        "stale_database_days": STALE_DATABASE_DAYS,
        "year": year,
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
)


def health_jobs(now: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Per sync job: its last success, its latest run, and whether it is fresh, stale or never
    succeeded (stale after ``SYNC_STALENESS_HOURS``)."""
    now = now or timezone.now()
    staleness = datetime.timedelta(hours=settings.SYNC_STALENESS_HOURS)
    jobs = []
    for job, label in SyncRun.Job.choices:
        if job in HEALTH_SKIPPED_JOBS:
            continue
        last_ok = SyncRun.last_success(job)
        last_run = SyncRun.objects.filter(job=job).order_by("-started_at").first()
        age = (now - last_ok.finished_at) if last_ok and last_ok.finished_at else None
        if last_ok is None:
            state, state_label = "unknown", _("Never succeeded")
        elif age is not None and age > staleness:
            state, state_label = "stale", _("Stale")
        else:
            state, state_label = "fresh", _("Fresh")
        jobs.append(
            {
                "job": job,
                "label": label,
                "last_success": last_ok,
                "last_run": last_run,
                "age_hours": round(age.total_seconds() / 3600, 1) if age else None,
                "state": state,
                "state_label": state_label,
            }
        )
    return jobs


def health_databases(
    year: ReportingYear | None, now: datetime.datetime | None = None
) -> list[dict[str, Any]]:
    """Per displayed ActivityInfo database of ``year``: its last import, and whether it is fresh, stale
    (older than ``STALE_DATABASE_DAYS``) or never imported."""
    now = now or timezone.now()
    databases = []
    for db in (
        Database.objects.filter(reporting_year=year, display=True)
        .select_related("section")
        .order_by("section__name", "name")
    ):
        last = db.last_monthly_update_date
        stale = bool(last and (now - last).days > STALE_DATABASE_DAYS)
        databases.append(
            {
                "database": db,
                "last_import": last,
                "state": "unknown" if last is None else ("stale" if stale else "fresh"),
                "state_label": _("Never imported") if last is None else (_("Stale") if stale else _("Fresh")),
            }
        )
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


def latest_datamart_runs(names) -> dict[str, SyncRun]:
    """The latest eTools Datamart run of each dataset in ``names`` (by start, then id), in one query: for
    each name, its first run on the index ``core_syncrun_job_target`` (job, target, newest start first). A
    ``DISTINCT ON (target)`` would read and sort every Datamart run (on a year of runs: 30 ms, not 1)."""
    table = connection.ops.quote_name(SyncRun._meta.db_table)
    latest = (
        "SELECT r.* FROM unnest(%s::text[]) AS t(target) CROSS JOIN LATERAL ("  # noqa: S608 - the model's table
        f"SELECT s.* FROM {table} s WHERE s.job = %s AND s.target = t.target "
        "ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r"
    )
    runs = SyncRun.objects.raw(latest, [list(names), SyncRun.Job.ETOOLS_DATAMART.value])
    return {run.target: run for run in runs}


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


def datamart_quality() -> list[dict[str, Any]]:
    """Per eTools Datamart dataset: the last run, what it read, wrote, failed to link, and how many
    rows NeuroDB holds — the table to read before believing a number on an eTools page."""
    from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

    last_by_target = latest_datamart_runs(ENTITY_SYNCS)
    counts = dataset_row_counts()
    rows = []
    for name in ENTITY_SYNCS:
        run = last_by_target.get(name)
        details = (run.details or {}) if run else {}
        count = counts[name]
        not_linked = {NOT_LINKED_LABELS.get(k, k): v for k, v in (details.get("not_linked") or {}).items()}
        rows.append(
            {
                "name": name,
                "run": run,
                "rows": count,
                "not_linked": not_linked,
                "not_linked_total": sum(int(v) for v in not_linked.values()),
                "removed": details.get("removed"),
                "empty_response": bool(details.get("empty_response")),
                "errors": (details.get("errors") or [])[:3],
                "relinked": details.get("relinked"),
            }
        )
    return rows


def health_as_dict(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobs": [
            {
                "job": j["job"],
                "label": j["label"],
                "state": j["state"],
                "last_success": j["last_success"].finished_at.isoformat() if j["last_success"] else None,
                "last_status": j["last_run"].status if j["last_run"] else None,
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
        "staleness_hours": health["staleness_hours"],
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
