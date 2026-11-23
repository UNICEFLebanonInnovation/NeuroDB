"""A programme document's or a partner's follow-up, on its page (the part "follow-up"): its open action
points and one dated list of what happened to it, from the tables NeuroDB already holds.

**Open points** (:func:`open_points`): how many of its action points are open, how many are overdue and how
many are open and high priority. Each count is the number of rows the Action points page lists at the address
its link opens: the count is that page's own query (``services.filtered_action_points``) over the same
filter, the programme document (``pd=<id>``) or the partner (``partner=<id>``), with ``status=open``,
``overdue=1`` or ``status=open&priority=1``. That page is where they are checked: neither page has a figure
trail.

**The timeline** (:func:`follow_up`): one dated list, newest first, of

* the programme document's signatures, start and end (a date still to come is marked planned); its status in
  eTools now heads a programme document's list. Amendments are not listed: NeuroDB holds no dated amendment;
* its progress reports, read as its page reads them (``services.pd_progress_reports`` and
  ``partner_progress_reports``): submitted (and sent back), accepted, or not submitted by their due date;
* field monitoring visits (one per visit, however many entities it rated: ``fm.visit_key``) on the date
  Monitoring insights gives a visit (its start, else its end), with their worst rating;
* third-party monitoring: a programme document's activities, a partner's visits (drafts and cancelled ones are
  not planned visits, as the partner page counts them);
* audits, special audits, spot checks and micro-assessments on their final report (else their field visit),
  with their financial findings counted as the engagement's page counts them
  (``services.financial_findings_count``);
* for a partner: its HACT assessments and PSEA assessments, and each HACT year once it is over (on 31
  December);
* action points raised (their eTools creation date) and completed, and the changes of status, priority and due
  date the action point history (``ActionPointStatus``) recorded since it started.

A partner's list covers its programme documents (every one but drafts, as its page lists them) and names the
programme document of each event that has one by its reference. Counts, dates, references, statuses and
ratings only: never a person (assignee, author, auditor, visit lead, focal point), never a firm or a partner's
name, never a text a partner or a monitor wrote (description, narrative, comment). Each entry links to the
page where its detail lives, which opens in the quick view as every link of a page does.

The list shows the last :data:`MONTHS` months, at most :data:`LIMIT` entries; *Show older* opens every event,
at most :data:`ALL_LIMIT`. What is left out is always said: the older events, the entries past the limit, and
the records without a date.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Min, Q, QuerySet, Value
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, NullIf, Substr
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.http import urlencode

from neurodb.partnerships.models import PCA, PartnerOrganization

from . import fm, reached, services
from . import models as dm

MONTHS = 24  # the window shown by default
LIMIT = 100  # entries shown at most in that window
ALL_LIMIT = 500  # entries shown at most with Show older
PARAM, ALL = "timeline", "all"  # ?timeline=all: every event
ANCHOR = "follow-up"  # the block's id on both pages

KINDS = {  # the kinds of event, in the order they come on the same day
    "pd": "Programme document",
    "report": "Progress report",
    "fm": "Field monitoring",
    "tpm": "Third-party monitoring",
    "assurance": "Assurance",
    "assessment": "Assessment",
    "hact": "HACT year",
    "action_point": "Action point",
    "history": "Action point history",
}
_ORDER = {kind: n for n, kind in enumerate(KINDS)}
OPEN_POINTS = (  # (key, label, the Action points page's filter)
    ("open", "Open", {"status": "open"}),
    ("overdue", "Overdue", {"overdue": "1"}),
    ("high_priority", "Open, high priority", {"status": "open", "priority": "1"}),
)
SUBMITTED = {  # a submitted report's label by its status now
    reached.SUBMITTED: "Submitted, awaiting acceptance",
    reached.SENT_BACK: "Submitted, then sent back to the partner",
    reached.DUE: "Submitted, then opened again in PRP",
}
RATINGS = {  # fm.normalize_rating's codes, in words
    "on_track": "On track",
    "constrained": "Constrained",
    "off_track": "Off track",
    "not_monitored": "Not rated",
    "other": "Rated",
}


@dataclass(frozen=True)
class Event:
    day: datetime.date
    kind: str
    label: str
    reference: str = ""
    url: str = ""
    pd: str = ""  # the programme document's reference, on a partner's list
    status: str = ""  # a status pill's key
    status_label: str = ""
    planned: bool = False  # a date still to come

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]


class _Events:
    """The events read so far, and the records that would give one but carry no date."""

    def __init__(self, *, named: bool):
        self.named = named  # a partner's list: each event names its programme document
        self.events: list[Event] = []
        self.undated = 0

    def add(self, day: datetime.date | None, kind: str, label: str, *, pd: str = "", **fields: Any) -> None:
        if day is None:
            self.undated += 1
            return
        self.events.append(Event(day, kind, label, pd=(pd or "") if self.named else "", **fields))


def _day(value: datetime.date | datetime.datetime | None) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def _words(day: datetime.date) -> str:
    return date_format(day, "j M Y")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _label(code: str | None) -> str:
    return services.code_label(code or "")


def _link(name: str, *args: Any, **query: Any) -> str:
    url = reverse(name, args=args)
    return f"{url}?{urlencode(query)}" if query else url


# ----------------------------------------------------------------------------------------------- open points
def open_points(
    *, pd: PCA | None = None, partner: PartnerOrganization | None = None, today: datetime.date | None = None
) -> list[dict[str, Any]]:
    """The open, overdue and open high-priority action points of a programme document (or of a partner), each
    with the address of the Action points page that lists exactly them: counted by that page's own query
    over that address's filter, so each count is the number of rows the page lists."""
    scope = {"pd": str(pd.pk)} if pd is not None else {"partner": str(partner.pk)}
    out = []
    for key, label, wanted in OPEN_POINTS:
        params = QueryDict(mutable=True)
        for name, value in {**scope, **wanted}.items():
            params[name] = value
        points = services.filtered_action_points(params, today)[0]
        out.append(
            {
                "key": key,
                "label": label,
                "count": points.count(),
                "url": f"{reverse('reports:action_points')}?{params.urlencode()}",
            }
        )
    return out


# ---------------------------------------------------------------------------------------------- the events
def _pd_events(found: _Events, pd: PCA, today: datetime.date) -> None:
    url = reverse("reports:programme_detail", args=[pd.pk])
    common = {"reference": pd.number or "", "url": url, "pd": pd.number or ""}
    found_any = False
    for day, label in (
        (pd.signed_by_partner_date, "Signed by the partner"),
        (pd.signed_by_unicef_date, "Signed by UNICEF"),
    ):
        if day:
            found.add(day, "pd", label, **common)
    for day, done, planned in ((pd.start, "Started", "Starts"), (pd.end, "Ended", "Ends")):
        if day:
            found_any = True
            ahead = day > today
            found.add(day, "pd", f"{planned} (planned)" if ahead else done, planned=ahead, **common)
    if not found_any:
        found.undated += 1  # a programme document with neither start nor end date


def _report_events(found: _Events, reports: Iterable[dict[str, Any]]) -> None:
    """Each progress report: submitted (then sent back), accepted, or not submitted by its due date. Reports
    still to come (due, not yet due) are not events yet."""
    for r in reports:
        pd = r.get("pd_reference_number") or ""
        if r.get("source"):  # PRP's own list of reports still owed: no pop-up, its programme document's list
            url = _link("reports:partner_reporting", pd=pd, pd_status="")
        else:
            url = _link("reports:progress_report", report=r["key"])
        common = {"reference": r.get("report_number") or "", "url": url, "pd": pd}
        pill = {"status": r["status"], "status_label": r["status_label"]}
        status, given = r["status"], False
        if r.get("submission_date"):
            if status == reached.ACCEPTED:  # the day it was accepted follows
                found.add(r["submission_date"], "report", "Submitted", **common)
            else:
                found.add(
                    r["submission_date"], "report", SUBMITTED.get(status, "Submitted"), **pill, **common
                )
            given = True
        if status == reached.ACCEPTED and r.get("acceptance_date"):
            found.add(r["acceptance_date"], "report", "Accepted", **common)
            given = True
        if status == reached.OVERDUE:
            found.add(r.get("due_date"), "report", "Not submitted by its due date", **pill, **common)
            given = True
        if status == reached.SENT_BACK and not given:
            found.add(r.get("due_date"), "report", "Sent back to the partner", **pill, **common)
            given = True
        if not given and status in (reached.ACCEPTED, reached.SUBMITTED):
            found.undated += 1  # submitted or accepted, but no date says when


def _fm_events(found: _Events, findings: QuerySet[dm.MonitoringFinding]) -> None:
    """One event per field monitoring visit (``fm.visit_key``): its date as Monitoring insights dates a visit
    (the earliest start date of its rows, else their latest end date) and its worst rating."""
    visits: dict[str, dict[str, Any]] = {}
    rows = findings.order_by("pk").values_list(
        "pk",
        "monitoring_activity_id",
        "monitoring_activity",
        "reference_number",
        "start_date",
        "end_date",
        "overall_finding_rating",
        "intervention__number",
    )
    for pk, activity_id, activity, number, start, end, rating, pd in rows:
        key = fm.visit_key(activity_id, activity, pk)
        visit = visits.setdefault(key, {"first": None, "last": None, "rating": "", "reference": "", "pd": ""})
        if start and (visit["first"] is None or start < visit["first"]):
            visit["first"] = start
        if end and (visit["last"] is None or end > visit["last"]):
            visit["last"] = end
        code = fm.normalize_rating(rating)
        if not visit["rating"] or fm.RATING_ORDER[code] > fm.RATING_ORDER[visit["rating"]]:
            visit["rating"] = code
        visit["reference"] = visit["reference"] or (activity or "").strip() or (number or "").strip()
        visit["pd"] = visit["pd"] or (pd or "")
    held = _fmm_visits(set(visits))
    for key, visit in visits.items():
        if key in held:  # its page in Monitoring insights
            url = reverse("fmm:visit", args=[key])
        elif visit["reference"]:  # the Field monitoring page lists its findings by its reference
            url = _link("reports:monitoring", q=visit["reference"])
        else:
            url = reverse("reports:monitoring")
        found.add(
            visit["first"] or visit["last"],
            "fm",
            "Visit",
            reference=visit["reference"],
            url=url,
            pd=visit["pd"],
            status=visit["rating"],
            status_label=RATINGS[visit["rating"]],
        )


def _fmm_visits(keys: set[str]) -> set[str]:
    """The visits of ``keys`` Monitoring insights holds (its visit page), when that app is installed and
    on."""
    from django.apps import apps
    from django.conf import settings

    if not keys or not apps.is_installed("neurodb.fmm") or not getattr(settings, "FMM_ENABLED", False):
        return set()
    from neurodb.fmm.models import Visit

    return set(Visit.objects.filter(key__in=keys).values_list("key", flat=True))


def _tpm_activity_events(found: _Events, pd: PCA) -> None:
    """A programme document's third-party monitoring activities; the Field monitoring page lists them by its
    reference."""
    url = _link("reports:monitoring", q=pd.number) if pd.number else reverse("reports:monitoring")
    for day, task, visit, status in pd.tpm_activities.order_by("pk").values_list(
        "date", "task_reference_number", "visit_reference_number", "status"
    ):
        found.add(
            day,
            "tpm",
            "Visit",
            reference=task or visit or "",
            url=url,
            status=(status or "").lower(),
            status_label=_label(status),
        )


def _tpm_visit_events(found: _Events, partner: PartnerOrganization) -> None:
    """A partner's planned third-party monitoring visits; the Field monitoring page lists them by its vendor
    number."""
    query = partner.vendor_number or partner.name
    url = _link("reports:monitoring", q=query) if query else reverse("reports:monitoring")
    visits = dm.TPMVisit.objects.filter(partner=partner).exclude(status__in=services.TPM_NOT_PLANNED)
    for start, end, reference, status in visits.order_by("pk").values_list(
        "start_date", "end_date", "reference_number", "status"
    ):
        found.add(
            start or end,
            "tpm",
            "Visit",
            reference=reference or "",
            url=url,
            status=(status or "").lower(),
            status_label=_label(status),
        )


def _engagement_events(found: _Events, engagements: QuerySet[dm.AuditEngagement]) -> None:
    """Each audit, special audit, spot check and micro-assessment, on its final report (else its field visit),
    with its financial findings as its page counts them (a micro-assessment: its risk rating)."""
    for e in engagements.annotate(listed=Count("findings")).order_by("pk"):
        kind = services.engagement_type_label(e.engagement_type)
        if e.date_of_final_report:
            day, label = e.date_of_final_report, f"{kind}: final report"
        else:
            day, label = e.date_of_field_visit, f"{kind}: field visit"
        if e.engagement_type == "ma":
            rating = (e.risk_rating or e.rating or "").strip()
            label += f" · risk rating {rating}" if rating else ""
        else:
            n = services.financial_findings_count(e, e.listed)
            label += f" · {_plural(n, 'financial finding', 'financial findings')}"
            if e.high_priority_findings:
                label += f", {e.high_priority_findings} high priority"
        found.add(
            day,
            "assurance",
            label,
            reference=e.reference_number or "",
            url=reverse("reports:engagement_detail", args=[e.pk]),
            status=e.status or "",
            status_label=_label(e.status),
        )


def _assessment_events(found: _Events, partner: PartnerOrganization) -> None:
    """A partner's HACT assessments and PSEA assessments, which the partner page lists under Assessments."""
    url = "#partner-assessments"
    assessments = dm.PartnerAssessment.objects.filter(partner=partner).order_by("pk")
    for kind, rating, completed in assessments.values_list("type", "rating", "completed_date"):
        label = f"{(kind or '').strip() or 'HACT assessment'} completed"
        label += f" · rating {rating.strip()}" if (rating or "").strip() else ""
        found.add(completed, "assessment", label, url=url)
    psea = dm.PSEAAssessment.objects.filter(partner=partner).order_by("pk")
    for reference, day, score, status in psea.values_list(
        "reference_number", "assessment_date", "overall_rating", "status"
    ):
        label = "PSEA assessment" + (f" · score {score}" if score is not None else "")
        found.add(
            day,
            "assessment",
            label,
            reference=reference or "",
            url=url,
            status=status or "",
            status_label=_label(status),
        )


def _hact_events(found: _Events, partner: PartnerOrganization, today: datetime.date) -> None:
    """Each HACT year once it is over (on 31 December): the assurance done against what was required, as the
    partner page's HACT by year shows it."""
    for h in partner.hact_years.filter(year__lt=today.year).exclude(year=None).order_by("year"):
        label = (
            f"{h.year} ended: {h.pv_completed or 0} of {h.pv_required or 0} programmatic visits, "
            f"{h.sc_completed or 0} of {h.sc_required or 0} spot checks, "
            f"{h.audits_completed or 0} of {h.audits_required or 0} audits done"
        )
        found.add(datetime.date(h.year, 12, 31), "hact", label, reference=str(h.year), url="#partner-hact")


def _created_day():
    """The day an action point was raised in eTools ("2026-05-02"), read from its record where the Action
    points page reads the month it was raised (``services.AP_CREATED_KEYS``)."""
    keys = services.AP_CREATED_KEYS
    return Substr(Coalesce(*(NullIf(KT(f"data__{key}"), Value("")) for key in keys), Value("")), 1, 10)


def _iso_day(text: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _action_point_events(found: _Events, points: QuerySet[dm.ActionPoint]) -> None:
    """Each action point raised (its status now on it) and, once completed, the day it was."""
    rows = (
        points.annotate(created_day=_created_day())
        .order_by("pk")
        .values_list(
            "pk",
            "reference_number",
            "status",
            "high_priority",
            "date_of_completion",
            "created_day",
            "intervention__number",
            "intervention_number",
        )
    )
    completed = set(dm.ActionPoint.COMPLETED_STATUSES)
    for pk, reference, status, high, done, created, pd_number, pd_text in rows:
        common = {
            "reference": reference or "",
            "url": reverse("reports:action_point", args=[pk]),
            "pd": pd_number or pd_text or "",
        }
        found.add(
            _iso_day(created),
            "action_point",
            "Raised, high priority" if high else "Raised",
            status=(status or "").lower(),
            status_label=_label(status),
            **common,
        )
        if (status or "").strip().lower() in completed and done:
            found.add(_day(done), "action_point", "Completed", **common)


HISTORY_FIELDS = (
    "source_id",
    "day",
    "in_datamart",
    "status",
    "high_priority",
    "due_date",
    "date_of_completion",
)


def _change(before: dict[str, Any], after: dict[str, Any]) -> str:
    """What changed from one recorded state of an action point to the next, in words."""
    if not after["in_datamart"]:
        return "No longer in the eTools Datamart"
    said = []
    if not before["in_datamart"]:
        said.append("Back in the eTools Datamart")
    if (before["status"] or "") != (after["status"] or ""):
        said.append(
            f"Status changed from {_label(before['status']).lower() or 'none'} to "
            f"{_label(after['status']).lower() or 'none'}"
        )
    if before["high_priority"] != after["high_priority"]:
        said.append("Marked high priority" if after["high_priority"] else "No longer high priority")
    if before["due_date"] != after["due_date"]:
        said.append(
            f"Due date changed to {_words(after['due_date'])}" if after["due_date"] else "Due date removed"
        )
    if not said and before["date_of_completion"] != after["date_of_completion"]:
        said.append("Completion date changed" if after["date_of_completion"] else "Completion date removed")
    return "; ".join(said) or "Recorded again"


def _history_events(found: _Events, scope: Q) -> None:
    """The changes the action point history recorded for the action points this programme document or partner
    ever held (``scope`` over ``ActionPointStatus``): every row but each point's first, the day NeuroDB first
    saw it."""
    ids = dm.ActionPointStatus.objects.filter(scope).values("source_id")
    by_point: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in (
        dm.ActionPointStatus.objects.filter(source_id__in=ids)
        .order_by("source_id", "day")
        .values(*HISTORY_FIELDS)
    ):
        by_point[row["source_id"]].append(row)
    if not by_point:
        return
    points = {
        source_id: (pk, reference, pd_number or pd_text)
        for source_id, pk, reference, pd_number, pd_text in dm.ActionPoint.objects.filter(
            source_id__in=list(by_point)
        )
        .order_by("pk")
        .values_list("source_id", "pk", "reference_number", "intervention__number", "intervention_number")
    }
    for source_id, states in by_point.items():
        pk, reference, pd = points.get(source_id, (None, "", ""))
        url = reverse("reports:action_point", args=[pk]) if pk else ""
        for before, after in zip(states, states[1:], strict=False):
            found.add(
                after["day"],
                "history",
                _change(before, after),
                reference=reference or f"eTools {source_id}",
                url=url,
                pd=pd or "",
            )


def history_since() -> datetime.date | None:
    """The first day the action point history recorded: changes are listed from that day."""
    return dm.ActionPointStatus.objects.aggregate(first=Min("day"))["first"]


def pd_events(pd: PCA, today: datetime.date) -> tuple[list[Event], int]:
    """A programme document's events and the records without a date (not listed)."""
    found = _Events(named=False)
    _pd_events(found, pd, today)
    _report_events(found, services.pd_progress_reports(pd))
    _fm_events(found, dm.MonitoringFinding.objects.filter(intervention=pd))
    _tpm_activity_events(found, pd)
    _engagement_events(found, pd.datamart_engagements.all())
    _action_point_events(found, dm.ActionPoint.objects.filter(intervention=pd))
    _history_events(found, Q(intervention=pd))
    return found.events, found.undated


def partner_events(partner: PartnerOrganization, today: datetime.date) -> tuple[list[Event], int]:
    """A partner's events (its programme documents' and its own) and the records without a date."""
    from neurodb.partnerships.services import EXCLUDED_STATUSES  # the partner page's list of its PDs

    found = _Events(named=True)
    for pd in PCA.objects.filter(partner=partner).exclude(status__in=EXCLUDED_STATUSES).order_by("pk"):
        _pd_events(found, pd, today)
    _report_events(found, services.partner_progress_reports(partner))
    _fm_events(found, dm.MonitoringFinding.objects.filter(partner=partner))
    _tpm_visit_events(found, partner)
    _engagement_events(found, dm.AuditEngagement.objects.filter(partner=partner))
    _assessment_events(found, partner)
    _hact_events(found, partner, today)
    _action_point_events(found, dm.ActionPoint.objects.filter(partner=partner))
    _history_events(found, Q(partner=partner))
    return found.events, found.undated


# ---------------------------------------------------------------------------------------------- the list
def months_before(day: datetime.date, months: int) -> datetime.date:
    """The same day ``months`` months earlier (the month's last day when it has no such day)."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    for last in (day.day, 30, 29, 28):
        try:
            return datetime.date(year, month + 1, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)  # pragma: no cover


def newest_first(events: Iterable[Event]) -> list[Event]:
    return sorted(events, key=lambda e: (-e.day.toordinal(), _ORDER[e.kind], e.pd, e.reference, e.label))


def window(
    events: list[Event], undated: int, today: datetime.date, *, show_all: bool, page: str
) -> dict[str, Any]:
    """What the list shows: the last :data:`MONTHS` months (or every event), newest first, at most
    :data:`LIMIT` (or :data:`ALL_LIMIT`) entries, grouped by year, with what it leaves out."""
    ordered = newest_first(events)
    since = months_before(today, MONTHS)
    recent = [e for e in ordered if e.day >= since]
    chosen = ordered if show_all else recent
    shown = chosen[: ALL_LIMIT if show_all else LIMIT]
    groups: list[dict[str, Any]] = []
    for event in shown:
        if not groups or groups[-1]["year"] != event.day.year:
            groups.append({"year": event.day.year, "entries": []})
        groups[-1]["entries"].append(event)
    return {
        "groups": groups,
        "show_all": show_all,
        "since": since,
        "months": MONTHS,
        "total": len(ordered),
        "chosen": len(chosen),
        "shown": len(shown),
        "cut": len(chosen) - len(shown),
        "older": 0 if show_all else len(ordered) - len(recent),
        "undated": undated,
        "all_url": f"{page}?{PARAM}={ALL}#{ANCHOR}",
        "recent_url": f"{page}#{ANCHOR}",
    }


def follow_up(
    *, pd: PCA | None = None, partner: PartnerOrganization | None = None, show_all: bool = False
) -> dict[str, Any]:
    """The part's figures for a programme document's page (``pd``) or a partner's (``partner``): its open
    points and its timeline."""
    today = timezone.localdate()
    if pd is not None:
        events, undated = pd_events(pd, today)
        page = reverse("reports:programme_detail", args=[pd.pk])
        points = open_points(pd=pd)
    else:
        events, undated = partner_events(partner, today)
        page = reverse("reports:partner_profile", args=[partner.pk])
        points = open_points(partner=partner)
    return {
        "subject": "pd" if pd is not None else "partner",
        "pd": pd,
        "partner": partner,
        "heading": 3 if pd is not None else 2,  # the block's heading level on its page
        "points": points,
        "timeline": window(events, undated, today, show_all=show_all, page=page),
        "history_since": history_since(),
    }
