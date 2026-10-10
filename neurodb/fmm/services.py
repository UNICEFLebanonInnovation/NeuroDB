"""What other NeuroDB pages read from Monitoring insights: the partner page's panel
(:func:`partner_summary`), the programme document page's panel (:func:`pd_summary`), the block "what the
partner reported, and other visits" of the visit page and the PD panel (:func:`pd_context`), and the
visits of the action points page (:func:`visits_for_action_points`, :func:`action_point_ids`).

The other pages import this module lazily, only when the app is installed and ``FMM_ENABLED`` is on
(:func:`enabled`), so ``neurodb.datamart`` and ``neurodb.reports`` never depend on it at module level.
Every figure is read through a :class:`~neurodb.fmm.scope.Scope` built as the panel's own link writes
it (``section=`` empty), so a panel always equals the Monitoring insights page it opens, for every
user (§0.4, invariants 3 and 4). Nothing here reads a narrative, an answer or a person's name.
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from django.apps import apps
from django.conf import settings
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

CONCERN_RATINGS = ("off_track", "constrained")
LATEST = 3  # latest visits listed on the PD panel
AROUND_DAYS = 90  # other visits to a PD within this many days of the visit, either side
SHOWN = 10  # TPM activities, staff trips and FM visits listed per PD
INDICATORS_SHOWN = 5  # partner-reported indicators listed per PD


def enabled() -> bool:
    """Monitoring insights is installed and switched on: the other pages show its panels and links."""
    return apps.is_installed("neurodb.fmm") and bool(getattr(settings, "FMM_ENABLED", False))


def action_point_ids(key: str) -> list[int]:
    """The ids of the eTools action points linked to the visit ``key`` (``VisitActionPoint``): the same
    set the visit page lists, for the action points page's "From Visit 1722" filter."""
    from .models import VisitActionPoint

    return list(
        VisitActionPoint.objects.filter(visit__key=str(key or "")[:40])
        .order_by("action_point_id")
        .values_list("action_point_id", flat=True)
    )


def visit_label(key: str) -> str:
    """The label of the visit ``key`` ("Visit 1722"), or "" when no visit has that key."""
    from .models import Visit

    return Visit.objects.filter(key=str(key or "")[:40]).values_list("label", flat=True).first() or ""


def visits_for_action_points(ids: Iterable[int] | Any) -> dict[int, tuple[str, str]]:
    """``{action point id: (visit key, visit label)}`` for the action points of ``ids`` (a list, or a
    query of ids) that are linked to a visit (``VisitActionPoint``); one linked to two visits gives the
    first by key. The action points page shows "Visit 1722" next to each, opening the visit."""
    from .models import VisitActionPoint

    out: dict[int, tuple[str, str]] = {}
    rows = (
        VisitActionPoint.objects.filter(action_point_id__in=ids)
        .order_by("action_point_id", "visit__key")
        .values_list("action_point_id", "visit__key", "visit__label")
    )
    for pk, key, label in rows:
        out.setdefault(pk, (key, label or key))
    return out


# ------------------------------------------------------------------------------------------ helpers
def _scope(**params: Any):
    """The scope of the link into FMM written with ``params`` (``section=`` empty, as ``scope.link``
    writes it)."""
    from .scope import Scope

    query = {key: str(value) for key, value in params.items()}
    query.setdefault("section", "")
    return Scope.from_params(query)


def _visit_line(v, limits: dict[str, int]) -> dict[str, Any]:
    """A visit as the panels list it: label, dates (the visit date: start, else end; the end date, which
    the rating line reads), rating (or "not rated yet"), quality, urgency."""
    from .views import _not_rated_yet, rating_label, urgency_band

    band = urgency_band(v.urgency, limits)
    return {
        "key": v.key,
        "label": v.label,
        "url": v.get_absolute_url(),
        "visit_date": v.visit_date,
        "end_date": v.end_date,
        "rating": v.rating,
        "rating_label": rating_label(v.rating, v.status_group),
        "not_rated_yet": _not_rated_yet(v.rating, v.status_group),
        "quality": v.quality_score,
        "score_band": v.score_band,
        "urgency": v.urgency,
        "urgency_band": band,
    }


def _action_point_counts(visits, today: datetime.date) -> dict[str, int]:
    """The FM action points linked to ``visits`` (each counted once): open, and open with their due
    date passed (``ActionPoint.OPEN_STATUSES``), as the Follow-up block of the page counts them."""
    from neurodb.datamart.models import ActionPoint

    from .models import VisitActionPoint

    linked = VisitActionPoint.objects.filter(visit__in=visits.values("pk")).values("action_point_id")
    open_ = Q(status__in=ActionPoint.OPEN_STATUSES)
    return ActionPoint.objects.filter(pk__in=linked).aggregate(
        open=Count("pk", filter=open_),
        overdue=Count("pk", filter=open_ & Q(due_date__lt=today)),
    )


# ------------------------------------------------------------------------------------- partner page
def partner_summary(partner_id: int, year: int) -> dict[str, Any] | None:
    """The partner page's "Monitoring insights" panel for ``year``: the partner's FM visits (with the
    status breakdown), the average quality of its own records (each scored on its own), its off-track and
    constrained records, the open and overdue FM action points of those visits, and its last visit (any
    year). None when no visit ever monitored the partner. The figures are those of
    ``/fmm/?partner=<id>&year=<year>&section=``, the panel's own link."""
    from . import metrics
    from .models import Visit
    from .scope import link

    partner_id = int(partner_id)
    every = Visit.objects.filter(partner_ids__contains=[partner_id])
    last = every.exclude(visit_date=None).order_by("-visit_date", "-pk").first()
    if last is None and not every.exists():
        return None
    limits = metrics.thresholds()
    scope = _scope(partner=partner_id, year=year)
    kpis = metrics.kpis(scope, metrics.stamp(), limits)
    ratings = scope.records().aggregate(  # the partner's own records, as the link's rating filter reads
        off_track=Count("pk", filter=Q(rating="off_track")),
        constrained=Count("pk", filter=Q(rating="constrained")),
    )
    points = _action_point_counts(scope.visits(), timezone.localdate())
    return {
        "year": year,
        "visits": kpis["visits"],
        "by_status": kpis["by_status"],
        "avg_quality": kpis["avg_quality"],
        "scored": kpis["scored"],
        "rules_version": kpis["rules_version"],
        "off_track": ratings["off_track"],
        "constrained": ratings["constrained"],
        "action_points_open": points["open"],
        "action_points_overdue": points["overdue"],
        "last": _visit_line(last, limits) if last else None,
        "url": link(partner=partner_id, year=year),
        "concern_url": link(partner=partner_id, year=year, rating=list(CONCERN_RATINGS), tab="visits"),
    }


# ------------------------------------------------------------------------------------------ PD page
def pd_summary(pd_id: int, year: int) -> dict[str, Any] | None:
    """The programme document page's "Field monitoring visits" panel for ``year``: the FM visits to the
    PD per quarter of their visit date, start else end (``VisitEntity.pd``) against the visits eTools plans
    (``PlannedVisits`` q1-q4), its 3 latest visits (any year) and the block of :func:`pd_context` for the
    year. None when no visit ever monitored the PD and eTools plans none for the year. The visits are
    those of ``/fmm/?pd=<id>&year=<year>&section=``, the panel's own link."""
    from django.db.models import Exists, OuterRef

    from neurodb.datamart.models import PlannedVisits

    from . import metrics
    from .models import Visit, VisitEntity
    from .scope import link

    pd_id = int(pd_id)
    # the visits with a record of the programme document, as the page's ``pd`` filter keeps them
    every = Visit.objects.filter(Exists(VisitEntity.objects.filter(visit=OuterRef("pk"), pd_id=pd_id)))
    planned = PlannedVisits.objects.filter(intervention_id=pd_id, year=year).aggregate(
        rows=Count("pk"), q1=Sum("q1"), q2=Sum("q2"), q3=Sum("q3"), q4=Sum("q4")
    )
    has_plan = bool(planned["rows"])
    if not has_plan and not every.exists():
        return None
    limits = metrics.thresholds()
    scope = _scope(pd=pd_id, year=year)
    kpis = metrics.kpis(scope, metrics.stamp(), limits)
    done = Counter((day.month - 1) // 3 + 1 for day in scope.visits().values_list("visit_date", flat=True))
    quarters = [
        {"quarter": q, "planned": planned[f"q{q}"] if has_plan else None, "visits": done.get(q, 0)}
        for q in (1, 2, 3, 4)
    ]
    latest = list(every.exclude(visit_date=None).order_by("-visit_date", "-pk")[:LATEST])
    return {
        "year": year,
        "visits": kpis["visits"],
        "avg_quality": kpis["avg_quality"],
        "scored": kpis["scored"],
        "planned": sum(q["planned"] or 0 for q in quarters) if has_plan else None,
        "quarters": quarters,
        "latest": [_visit_line(v, limits) for v in latest],
        "url": link(pd=pd_id, year=year),
        "context": pd_context([pd_id], year=year, knowledge=False, limits=limits),
    }


def _synced(target: str) -> datetime.date | None:
    """The day the eTools Datamart dataset ``target`` was last synced: the reference date of the
    statuses read from it."""
    from neurodb.core.models import SyncRun

    from .status import DONE

    when = (
        SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART, target=target, status__in=DONE)
        .exclude(finished_at=None)
        .order_by("-finished_at")
        .values_list("finished_at", flat=True)
        .first()
    )
    return timezone.localtime(when).date() if when else None


def _cp_outputs(pds: Iterable[Any]) -> dict[int, list[dict[str, Any]]]:
    """``{PD id: the country programme outputs it contributes to}``: the outputs of the current country
    programme (the latest one when none is marked current) that ``cpd.services.output_matches`` finds
    in the PD's eTools CP outputs, with their code, title and the country programme dashboard's link."""
    from neurodb.cpd.models import CountryProgramme, Output
    from neurodb.cpd.services import output_matches

    out: dict[int, list[dict[str, Any]]] = {pd.pk: [] for pd in pds}
    if not any(pd.cp_outputs for pd in pds):
        return out
    programme = CountryProgramme.objects.filter(current=True).first() or CountryProgramme.objects.first()
    if programme is None:
        return out
    outputs = list(Output.objects.filter(outcome__programme=programme).order_by("code", "pk"))
    url = f"{reverse('cpd:dashboard')}?cycle={programme.pk}"
    for pd in pds:
        names = [name for name in pd.cp_outputs or [] if name]
        out[pd.pk] = [
            {"code": o.code, "title": o.title, "url": url}
            for o in outputs
            if any(output_matches(o, name) for name in names)
        ]
    return out


def pd_context(
    pd_ids: Iterable[int],
    around: datetime.date | None = None,
    *,
    year: int | None = None,
    exclude_key: str = "",
    knowledge: bool = True,
    limits: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """One block per programme document of ``pd_ids``: what the partner reported on it (its indicators
    with the tracking status of the latest period reported, ``datamart.monitoring.indicators``), the
    other visits to it (TPM activities, UNICEF staff programmatic trips, never the traveller's name,
    and FM visits), the country programme outputs it contributes to and, with ``knowledge``, the
    knowledge base documents that mention it (else those that mention its partner).

    The visits are those within ``AROUND_DAYS`` of ``around`` (the visit page, ``exclude_key`` leaving
    the visit itself out), else those of the calendar year ``year`` (the PD page; this year by
    default). The indicators are those of the year of ``around``, else of ``year``."""
    from neurodb.datamart import monitoring
    from neurodb.datamart.models import ProgrammaticVisit, TPMActivity
    from neurodb.partnerships.models import PCA

    from . import metrics
    from .models import Visit

    ids = list(dict.fromkeys(int(pk) for pk in pd_ids))
    if not ids:
        return []
    today = timezone.localdate()
    if around is not None:
        start = around - datetime.timedelta(days=AROUND_DAYS)
        end = around + datetime.timedelta(days=AROUND_DAYS)
        year = around.year
    else:
        year = year or today.year
        start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    pds = {pd.pk: pd for pd in PCA.objects.filter(pk__in=ids).select_related("partner")}
    if not pds:
        return []
    limits = limits or metrics.thresholds()
    found = list(pds)
    outputs = _cp_outputs(pds.values())
    tpm_as_of = _synced("tpm_activities")

    reported: dict[int, list] = {pk: [] for pk in pds}
    for row in monitoring.indicators(monitoring.Filters(pd_ids=found, year=year, scope="all"), today):
        reported.setdefault(row.pd.pk, []).append(row)

    tpm: dict[int, list[dict[str, Any]]] = {pk: [] for pk in pds}
    for a in (
        TPMActivity.objects.filter(intervention_id__in=found, date__gte=start, date__lte=end)
        .order_by("-date", "-datamart_id")
        .values("intervention_id", "date", "visit_reference_number", "task_reference_number", "status")
    ):
        tpm[a["intervention_id"]].append(
            {
                "reference": a["task_reference_number"] or a["visit_reference_number"],
                "date": a["date"],
                "status": a["status"],
            }
        )
    trips: dict[int, list[dict[str, Any]]] = {pk: [] for pk in pds}
    for t in (
        ProgrammaticVisit.objects.filter(
            intervention_id__in=found, travel_type__icontains="programmatic", date__gte=start, date__lte=end
        )
        .order_by("-date", "-datamart_id")
        .values("intervention_id", "travel_reference_number", "date", "location_name")
    ):
        trips[t["intervention_id"]].append(
            {"reference": t["travel_reference_number"], "date": t["date"], "place": t["location_name"]}
        )
    visits: dict[int, list[dict[str, Any]]] = {pk: [] for pk in pds}
    rows = Visit.objects.filter(pd_ids__overlap=found, visit_date__gte=start, visit_date__lte=end)
    if exclude_key:
        rows = rows.exclude(key=exclude_key)
    for v in rows.order_by("-visit_date", "-pk")[:500]:
        for pk in v.pd_ids:
            if pk in visits:
                visits[pk].append(_visit_line(v, limits))

    blocks = []
    for pk in ids:
        pd = pds.get(pk)
        if pd is None:
            continue
        indicators = reported.get(pk, [])
        counts = Counter(i.tracking for i in indicators)
        latest_first = sorted(
            indicators, key=lambda i: (i.cumulative_as_of or datetime.date.min, i.title), reverse=True
        )
        documents: list = []
        if knowledge:
            from neurodb.knowledge.search import linked

            documents = linked("programme_document", pd.pk, limit=5)
            if not documents and pd.partner_id:
                documents = linked("partner", pd.partner_id, limit=5)
        blocks.append(
            {
                "pd": pd,
                "year": year,
                "indicators": [
                    {
                        "title": i.title,
                        "url": i.url,
                        "tracking": i.tracking,
                        "label": i.tracking_label,
                        "as_of": i.cumulative_as_of,
                    }
                    for i in latest_first[:INDICATORS_SHOWN]
                ],
                "indicator_count": len(indicators),
                "status_counts": [
                    {"tracking": key, "label": monitoring.LABELS.get(key, key), "n": n}
                    for key, n in counts.most_common()
                ],
                "reported_for": latest_first[0].cumulative_as_of if latest_first else None,
                "monitoring_url": (
                    f"{reverse('reports:pd_monitoring')}?pd={quote(pd.number or '', safe='')}&scope=all"
                    if pd.number
                    else ""
                ),
                "tpm": tpm[pk][:SHOWN],
                "tpm_count": len(tpm[pk]),
                "tpm_as_of": tpm_as_of,
                "trips": trips[pk][:SHOWN],
                "trips_count": len(trips[pk]),
                "visits": visits[pk][:SHOWN],
                "visits_count": len(visits[pk]),
                "outputs": outputs.get(pk, []),
                "documents": documents,
                "start": start,
                "end": end,
                "around": around,
            }
        )
    return blocks
