"""Page-shaping helpers owned by the web layer: search, data health, saved views, serialisation.

Domain services live in the other apps (facts, partnerships, library, core); this module only
combines them for the reports pages and the internal JSON API.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
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
MAP_FILTERS = ("partner", "pd", "month", "governorate", "district")
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


def search(q: str, year: ReportingYear | None, limit: int = 8) -> list[dict[str, Any]]:
    """Indicators, databases and reports of the current year whose name or code contains ``q``."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    dbs = Database.objects.filter(reporting_year=year, display=True) if year else Database.objects.none()
    db_ids = list(dbs.values_list("id", flat=True))
    name_or_code = Q(name__icontains=q) | Q(awp_code__icontains=q)
    groups: list[dict[str, Any]] = []

    databases = dbs.filter(Q(name__icontains=q) | Q(label__icontains=q)).select_related("section")[:limit]
    groups.append(
        {
            "key": "databases",
            "label": _("Databases"),
            "items": [
                {
                    "label": d.label or d.name,
                    "hint": d.section.name if d.section else "",
                    "url": reverse("reports:database_dashboard", args=[d.id]),
                }
                for d in databases
            ],
        }
    )
    reports = (
        NeuroReport.objects.filter(ryear=year, is_active=True, name__icontains=q)[:limit] if year else []
    )
    groups.append(
        {
            "key": "reports",
            "label": _("Reports"),
            "items": [
                {
                    "label": r.name,
                    "hint": _("HPM report") if r.is_hpm else _("Neuro report"),
                    "url": reverse(
                        "reports:report_hpm" if r.is_hpm else "reports:report_dashboard", args=[r.id]
                    ),
                }
                for r in reports
            ],
        }
    )
    masters = (
        MasterIndicator.objects.filter(database_id__in=db_ids, is_active=True)
        .filter(name_or_code)
        .select_related("database")
        .order_by("database__name", "sequence")[:limit]
    )
    groups.append(
        {
            "key": "masters",
            "label": _("Master indicators"),
            "items": [
                {
                    "label": m.name,
                    "hint": f"{m.awp_code} · {m.database.label or m.database.name}",
                    "url": reverse("reports:database_dashboard", args=[m.database_id]) + f"#indicator-{m.id}",
                }
                for m in masters
            ],
        }
    )
    subs = (
        SubIndicator.objects.filter(database_id__in=db_ids)
        .filter(name_or_code)
        .select_related("database")[:limit]
    )
    groups.append(
        {
            "key": "subs",
            "label": _("Sub-indicators"),
            "items": [
                {
                    "label": s.name,
                    "hint": f"{s.awp_code} · {s.database.label or s.database.name}",
                    "url": reverse("reports:database_analytical", args=[s.database_id]),
                }
                for s in subs
            ],
        }
    )
    leaves = (
        IndicatorNew.objects.filter(database_id__in=db_ids)
        .filter(name_or_code)
        .select_related("database")[:limit]
    )
    groups.append(
        {
            "key": "indicators",
            "label": _("ActivityInfo indicators"),
            "items": [
                {
                    "label": i.name,
                    "hint": f"{i.awp_code or ''} · {i.database.label or i.database.name}",
                    "url": reverse("reports:database_analytical", args=[i.database_id]),
                }
                for i in leaves
            ],
        }
    )
    return [g for g in groups if g["items"]]


# ------------------------------------------------------------------------- data health


def data_health() -> dict[str, Any]:
    """Freshness of every job and of every displayed database of the current year."""
    now = timezone.now()
    staleness = datetime.timedelta(hours=settings.SYNC_STALENESS_HOURS)
    jobs = []
    for job, label in SyncRun.Job.choices:
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
    year = current_year()
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
    return {
        "jobs": jobs,
        "runs": list(SyncRun.objects.order_by("-started_at")[:50]),
        "databases": databases,
        "staleness_hours": settings.SYNC_STALENESS_HOURS,
        "stale_database_days": STALE_DATABASE_DAYS,
        "year": year,
    }


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
