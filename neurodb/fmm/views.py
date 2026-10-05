"""The Monitoring insights page (``/fmm/``): its tabs (Insights, Quality, Analysis, Visits), the
visits table and its CSV, the visit page, the visit look-up, the reviews and the drill-down window
that lists the visits behind a chart cell or a count.

Every view reads the stored visits through a :class:`~neurodb.fmm.scope.Scope` built from the query
string; an HTMX request gets the partial it swaps in, a plain request the full page. With
``FMM_ENABLED`` off every view answers 404. The visit page reads its narratives from the findings and
its checklist answers from their records (``fmm.parse``), for staff only; nothing here sends them
anywhere.
"""

from __future__ import annotations

import csv
import datetime
import re
from collections import OrderedDict
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import F, Func, IntegerField, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from django.views.decorators.http import require_GET, require_POST

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.web.templatetags.ui import code_label

from . import access, metrics, status
from .models import RuleSetting, ScoreSetting, Visit, VisitActionPoint, VisitReview
from .scope import (
    DRILL_KEYS,
    KIND_LABELS,
    RATING_LABELS,
    RATINGS,
    STATUS_GROUPS,
    STATUS_LABELS,
    Scope,
    _drill_ok,
    options,
    quarter_of,
)

# The tab bar: each stage appends its own tab
TABS = [
    ("insights", gettext_lazy("Insights")),
    ("quality", gettext_lazy("Quality")),
    ("analysis", gettext_lazy("Analysis")),
    ("visits", gettext_lazy("Visits")),
    ("map", gettext_lazy("Map")),
]
PAGE_SIZE = 50
SORTS = {
    "-urgency": (F("urgency").desc(), F("end_date").desc(nulls_last=True)),
    "urgency": (F("urgency").asc(), F("end_date").desc(nulls_last=True)),
    "-date": (F("end_date").desc(nulls_last=True),),
    "date": (F("end_date").asc(nulls_last=True),),
    "-quality": (F("quality_score").desc(nulls_last=True), F("urgency").desc()),
    "quality": (F("quality_score").asc(nulls_last=True), F("urgency").desc()),
    "partner": (F("partner__short_name").asc(nulls_last=True), F("end_date").desc(nulls_last=True)),
}
DEFAULT_SORT = "-urgency"
CSV_HEADER = (
    "Visit",
    "Key",
    "eTools activity id",
    "Reference",
    "Reference number",
    "Start date",
    "End date",
    "Status",
    "Status group",
    "Partner",
    "Programme documents",
    "CP outputs",
    "Place",
    "Governorate",
    "District",
    "Sections",
    "Field offices",
    "Rating",
    "HACT Q1",
    "Quality score",
    "Score band",
    "Flags",
    "Urgency",
    "Action points",
    "Open action points",
    "Overdue action points",
    "Review",
    "Reviewed on",
)
URGENCY_PARTS = {
    "rating": gettext_lazy("rating"),
    "quality": gettext_lazy("quality"),
    "flags": gettext_lazy("flags"),
    "follow_up": gettext_lazy("follow-up"),
    "report_late": gettext_lazy("report late"),
}
RULE_STATES = {
    "pass": gettext_lazy("Passed"),
    "fail": gettext_lazy("Flagged"),
    "na": gettext_lazy("Not available"),
    "nap": gettext_lazy("Does not apply"),
    "off": gettext_lazy("Switched off"),
}
PD_MATCH = {
    "exact": gettext_lazy("matched by its number"),
    "token": gettext_lazy("matched by PCA/PD number"),
    "base": gettext_lazy("matched by its base number"),
    "title": gettext_lazy("matched by its title"),
}
AP_MATCH = {
    "related_id": gettext_lazy("linked to the activity in eTools"),
    "reference": gettext_lazy("matched by the visit reference"),
    "reference_number": gettext_lazy("matched by the reference number"),
}
SOURCES = {
    "activity": gettext_lazy("from the visit's eTools record"),
    "pd": gettext_lazy("from the programme document"),
    "action_point": gettext_lazy("from its action points"),
    "partner": gettext_lazy("inferred from the partner"),
}
LOCATED = {
    "site": gettext_lazy("placed by its monitoring site"),
    "location": gettext_lazy("placed by its location"),
}
ROLE_LABELS = {"q1": "HACT Q1", "q2": "Q2", "q3": "Q3", "psea": "PSEA"}
Q1_FROM = {
    "partner": gettext_lazy("given for the partner"),
    "visit": gettext_lazy("given for the whole visit"),
}
BANDS = {"high": gettext_lazy("High"), "medium": gettext_lazy("Medium"), "low": gettext_lazy("Low")}
APPLIES = {"partner": gettext_lazy("for the partner"), "visit": gettext_lazy("for the whole visit")}
DRILL_LABELS = {
    "month": gettext_lazy("Month"),
    "hact_q1": gettext_lazy("HACT Q1"),
    "bucket": gettext_lazy("Quality score"),
    "flag": gettext_lazy("Flag"),
    "flags": gettext_lazy("Flags per visit"),
    "urgency": gettext_lazy("Urgency"),
    "location": gettext_lazy("Location"),
    "issue": gettext_lazy("Issue"),
    "rule": gettext_lazy("Rule"),
    "rule_state": gettext_lazy("Rule result"),
    "review": gettext_lazy("Review"),
    "q": gettext_lazy("Search"),
}
_LOOKUP_PREFIX = re.compile(r"^(?:#|visit\s*#?\s*)", re.IGNORECASE)


def _enabled() -> None:
    if not getattr(settings, "FMM_ENABLED", False):
        raise Http404("Monitoring insights is switched off")


def _crumbs(*extra: dict[str, str | None]) -> list[dict[str, str | None]]:
    return [
        {"label": _("Partnerships"), "url": reverse("reports:partners")},
        {"label": _("Monitoring insights"), "url": reverse("fmm:dashboard") if extra else None},
        *extra,
    ]


# ------------------------------------------------------------------------------------------ helpers
def _day(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def _last_fm_sync() -> datetime.datetime | None:
    """When the eTools field monitoring rows were last synced (the Datamart's field_monitoring run)."""
    return (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART, target="field_monitoring", status__in=status.DONE
        )
        .exclude(finished_at=None)
        .order_by("-finished_at")
        .values_list("finished_at", flat=True)
        .first()
    )


def _reference(scope: Scope, snap: status.Snapshot) -> dict[str, Any]:
    """The reference line: the scope, when the rows were synced and the scores computed, with which
    rules version, a rescore under way, and a failed last refresh."""
    refresh = snap.last_refresh
    return {
        "label": scope.label(),
        "synced": snap.fm_synced,
        "refresh": refresh,
        "rules_version": (refresh.details or {}).get("rules_version") if refresh else None,
        "pending_version": snap.rules_version if snap.pending else None,
        "failed": snap.failed,
    }


def _how(setting: ScoreSetting, rules: list[RuleSetting] | None = None) -> dict[str, Any]:
    """What the "How scores work" window lists: each rule as administrators set it and the thresholds."""
    return {
        "rules": rules if rules is not None else list(RuleSetting.objects.order_by("code")),
        "setting": setting,
    }


def urgency_text(parts: dict[str, Any] | None) -> str:
    """ "rating 40 · quality 8 · flags 5 · follow-up 20": the parts of an urgency that count."""
    out = [f"{URGENCY_PARTS.get(k, k)} {v}" for k, v in (parts or {}).items() if v]
    return " · ".join(out) or _("nothing adds to it")


def _not_rated_yet(code: str, status_group: str) -> bool:
    """A planned or in-progress visit without a rating is "not rated yet", never a monitoring gap."""
    return code == "not_monitored" and status_group in ("planned", "in_progress")


def rating_label(code: str, status_group: str = "") -> str:
    if _not_rated_yet(code, status_group):
        return _("Not rated yet")
    return _(RATING_LABELS.get(code, "Other"))


def _latest_reviews(keys: list[str]) -> dict[str, VisitReview]:
    """The latest review of each visit key."""
    out: dict[str, VisitReview] = {}
    for review in VisitReview.objects.filter(visit_key__in=keys).order_by("visit_key", "-created_at", "-pk"):
        out.setdefault(review.visit_key, review)
    return out


def _decorate(visits: list[Visit], limits: dict[str, int]) -> None:
    """What the table shows of each visit, worked out once."""
    reviews = _latest_reviews([v.key for v in visits])
    for v in visits:
        v.review = reviews.get(v.key)
        v.urgency_title = urgency_text(v.urgency_parts)
        v.row_band = "red" if v.urgency >= limits["red"] else "amber" if v.urgency >= limits["amber"] else ""
        v.rating_label = rating_label(v.rating, v.status_group)
        v.not_rated_yet = _not_rated_yet(v.rating, v.status_group)
        v.team_shown = v.team[:2]
        v.team_more = max(len(v.team) - 2, 0) + v.team_unnamed  # the members known by e-mail only too


def _sort(request: HttpRequest) -> str:
    wanted = request.GET.get("sort", "")
    return wanted if wanted in SORTS else DEFAULT_SORT


SORT_COLUMNS = [
    ("", gettext_lazy("Visit")),
    ("date", gettext_lazy("Date")),
    ("partner", gettext_lazy("Partner / PD ref")),
    ("", gettext_lazy("Location")),
    ("", gettext_lazy("Section")),
    ("quality", gettext_lazy("Quality")),
    ("urgency", gettext_lazy("Urgency")),
]


def _sort_next(sort: str) -> dict[str, str]:
    """The order each sortable column's header asks for: the other direction when it is the current
    one, else its first direction (newest, lowest quality, most urgent first)."""
    return {
        "date": "date" if sort == "-date" else "-date",
        "partner": "partner",
        "quality": "-quality" if sort == "quality" else "quality",
        "urgency": "urgency" if sort == "-urgency" else "-urgency",
    }


def _this_year_query(scope: Scope) -> str:
    """The scope's filters over this calendar year ("Try this year" of an empty filter)."""
    from dataclasses import replace

    from .scope import period

    start, end = period("this_year", timezone.localdate())
    return replace(scope, preset="this_year", start=start, end=end, year=None, drill=()).query


def _table(
    request: HttpRequest, scope: Scope, limits: dict[str, int], count: int | None = None
) -> dict[str, Any]:
    """The visits table: one page of the scope's visits in the chosen order."""
    sort = _sort(request)
    qs = scope.visits().select_related("partner").order_by(*SORTS[sort], "key")
    paginator = Paginator(qs, PAGE_SIZE)
    if count is not None:
        paginator.count = count  # the key figures counted the same visits already
    page = paginator.get_page(request.GET.get("page"))
    visits = list(page.object_list)
    _decorate(visits, limits)
    return {
        "page_obj": page,
        "visits": visits,
        "sort": sort,
        "limits": limits,
        "sort_columns": SORT_COLUMNS,
        "sort_next": _sort_next(sort),
        "this_year_query": _this_year_query(scope),
        "downloads": [
            {"label": _("All rows (CSV)"), "url": f"{reverse('fmm:visits')}?{scope.query}&export=csv"}
        ],
    }


def _kpi_tiles(scope: Scope, k: dict[str, Any]) -> list[dict[str, Any]]:
    """The four key figures as tiles, each linking to the Visits tab with the matching filter."""
    from dataclasses import replace

    from neurodb.web.templatetags.ui import number, percent

    breakdown = " · ".join(f"{number(row['n'])} {_(row['label'])}" for row in k["by_status"])
    rated = _("%(rated)s rated · %(nm)s not monitored") % {
        "rated": number(k["entities_rated"]),
        "nm": number(k["entities_not_monitored"]),
    }
    if k["entities_other"]:
        rated += " · " + _("%(n)s other") % {"n": number(k["entities_other"])}
    if k["avg_quality"] is not None:
        quality_hint = ngettext(
            "on %(n)s scored visit · rules v%(v)s", "on %(n)s scored visits · rules v%(v)s", k["scored"]
        ) % {"n": number(k["scored"]), "v": k["rules_version"]}
    else:
        quality_hint = _("no visit could be scored")
    urgent = replace(scope, drill=tuple(d for d in scope.drill if d[0] != "urgency") + (("urgency", "red"),))
    return [
        {
            "label": _("Monitoring visits"),
            "value": number(k["visits"]),
            "hint": breakdown,
            "href": f"{reverse('fmm:dashboard')}?{_page_query(scope, tab='visits')}",
        },
        {
            "label": _("Monitored entities"),
            "value": number(k["entities"]),
            "hint": rated,
            "href": f"{reverse('fmm:dashboard')}?{_page_query(scope, tab='visits')}",
        },
        {
            "label": _("Average quality score"),
            "value": percent(k["avg_quality"]) if k["avg_quality"] is not None else "—",
            "hint": quality_hint,
            "href": f"{reverse('fmm:dashboard')}?{_page_query(scope, tab='visits', sort='quality')}",
        },
        {
            "label": _("High urgency"),
            "value": number(k["high_urgency"]),
            "hint": _("≥ %(red)s · %(amber)s amber (%(low)s–%(high)s)")
            % {
                "red": k["red_at"],
                "amber": number(k["amber"]),
                "low": k["amber_at"],
                "high": k["red_at"] - 1,
            },
            "href": f"{reverse('fmm:dashboard')}?{_page_query(urgent, tab='visits')}",
            "status": "off_track" if k["high_urgency"] else "",
        },
    ]


def _page_query(scope: Scope, **extra: Any) -> str:
    """The dashboard's query string for ``scope`` plus ``extra`` (tab, sort, page)."""
    from urllib.parse import urlencode

    pairs = scope.pairs() + [(k, str(v)) for k, v in extra.items() if v not in (None, "")]
    return urlencode(pairs)


def _results_context(
    request: HttpRequest, scope: Scope, tab: str, snap: status.Snapshot, when: str
) -> dict[str, Any]:
    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    rules = list(RuleSetting.objects.order_by("code"))
    context: dict[str, Any] = {
        "scope": scope,
        "tab": tab,
        "tabs": [{"key": k, "label": label, "query": _page_query(scope, tab=k)} for k, label in TABS],
        "has_visits": bool(snap.visits),
        "reference": _reference(scope, snap),
        "how": _how(setting, rules),
    }
    if not snap.visits:
        from neurodb.datamart.models import MonitoringFinding

        context["findings_waiting"] = MonitoringFinding.objects.exists()
        return context
    limits = metrics.thresholds(setting)
    kpis = metrics.kpis(scope, when, limits)
    context.update(
        {
            "kpis": kpis,
            "kpi_tiles": _kpi_tiles(scope, kpis),
            "notes": metrics.notes(scope, when),
            "chips": _chips(scope),
            "visits_query": _page_query(scope, tab="visits"),
            "this_year_query": _this_year_query(scope),
            "limits": limits,
        }
    )
    if tab == "visits":
        context.update(_table(request, scope, limits, count=kpis["visits"]))
    elif tab == "quality":
        context.update(_quality_tab(scope, when, limits, rules))
    elif tab == "analysis":
        context.update(_analysis_tab(request, scope, when, limits, rules))
    elif tab == "map":
        context.update(_map_tab(request, scope, when))
    return context


def _chips(scope: Scope) -> list[dict[str, str]]:
    """The removable chips above the results: the default section, the programme document and each
    drill-down, each with the address of the scope without it."""
    from dataclasses import replace

    chips = []
    if scope.default_section:
        chips.append(
            {
                "label": _("Your section: %(names)s") % {"names": ", ".join(scope.sections)},
                "remove": _page_query(replace(scope, sections=())),
                "remove_label": _("show all"),
                "remove_in_words": True,  # "Your section: Education · show all"
            }
        )
    if scope.pd is not None:
        from neurodb.partnerships.models import PCA

        number = PCA.objects.filter(pk=scope.pd).values_list("number", flat=True).first()
        chips.append(
            {
                "label": _("Programme document %(number)s") % {"number": number or scope.pd},
                "remove": _page_query(replace(scope, pd=None)),
                "remove_label": _("remove"),
            }
        )
    for key, value in scope.drill:
        chips.append(
            {
                "label": f"{DRILL_LABELS.get(key, key)}: {_drill_value(key, value)}",
                "remove": _page_query(
                    replace(scope, drill=tuple(d for d in scope.drill if d != (key, value)))
                ),
                "remove_label": _("remove"),
            }
        )
    return chips


def _drill_value(key: str, value: str) -> str:
    """A drill-down's value in words where it is a code ("constrained" -> "Constrained")."""
    if value == "none":  # what "none" means for each drill-down
        none_words = {
            "bucket": _("not scored"),
            "hact_q1": _("not answered"),
            "review": _("not reviewed"),
            "urgency": _("below amber"),
        }
        return str(none_words.get(key, _("none")))
    if key == "hact_q1":
        return _(RATING_LABELS.get(value, value))
    if key == "review":
        return str(dict(VisitReview.Status.choices).get(value, value))
    if key == "rule_state":
        return str(RULE_STATES.get(value, value))
    if key == "bucket":
        return f"{value.replace('-', '–')}%"
    if key == "month":
        try:
            return datetime.date.fromisoformat(f"{value}-01").strftime("%b %Y")
        except ValueError:
            return value
    if key == "location":  # the place, not its id
        from neurodb.geo.models import Location

        return Location.objects.filter(pk=int(value)).values_list("name", flat=True).first() or value
    if key == "issue":  # the issue in words, as the recurring issues list writes it
        rule, detail_key = value.split(":", 1)
        label = metrics.issue_label(rule, detail_key, [], RuleSetting.objects.filter(code=rule).first())
        return label.split(": ", 1)[1] if label.startswith(f"{rule}: ") else label
    if key == "flags":
        return _("%(n)s or more") % {"n": value[:-1]} if value.endswith("+") else value
    return value


def _tab(request: HttpRequest) -> str:
    wanted = request.GET.get("tab", "")
    return wanted if wanted in dict(TABS) else "insights"


# ------------------------------------------------------------------------------------------ drill-downs
# Filters a drill-down may narrow (a block offers only values the filter already keeps, so replacing
# the filter's values by one of them narrows the scope)
DRILL_FILTERS = {
    "rating": "ratings",
    "status": "statuses",
    "office": "offices",
    "section": "sections",
    "entity_type": "entity_types",
}


def _narrowed(scope: Scope, params: dict[str, str]) -> Scope:
    from dataclasses import replace

    drill = [d for d in scope.drill if d[0] not in params]
    changes: dict[str, Any] = {}
    for key, value in params.items():
        if key in DRILL_FILTERS:
            changes[DRILL_FILTERS[key]] = (value,)
        else:
            drill.append((key, value))
    return replace(scope, drill=tuple(drill), default_section=False, **changes)


def drill_url(scope: Scope, **params: str) -> str:
    """The drill-down window of the visits of ``scope`` narrowed by ``params`` (drill codes, or one
    value of a filter: rating, status, office, section, entity type)."""
    return f"{reverse('fmm:drill')}?{_narrowed(scope, params).query}"


def _drill_template(scope: Scope, *keys: str) -> str:
    """The address a chart fills in per point: the scope without ``keys`` (the chart's own drill-downs
    and filters, which every bar already lies within), then ``key={drill}``-style placeholders."""
    from dataclasses import replace

    clear = {DRILL_FILTERS[k]: () for k in keys if k in DRILL_FILTERS}
    base = replace(
        scope, drill=tuple(d for d in scope.drill if d[0] not in keys), default_section=False, **clear
    )
    return f"{reverse('fmm:drill')}?{base.query}"


def _with_urls(
    rows: list[dict[str, Any]], scope: Scope, key: str, field: str = "drill"
) -> list[dict[str, Any]]:
    """Copies of ``rows`` (kept in the cache: never changed in place), each with the drill-down window
    of its ``field`` value as ``url`` ("" when it has none)."""
    return [
        {**row, "url": drill_url(scope, **{key: str(row[field])}) if row.get(field) not in (None, "") else ""}
        for row in rows
    ]


PLACES_TOP = 10  # places shown before "Show all"


def _quality_tab(scope: Scope, when: str, limits: dict[str, int], rules: list) -> dict[str, Any]:
    """The Quality tab: quality and visits by month, HACT Q1 by month (or the overall rating when no
    visit has a Q1 answer), the score distribution, recurring issues, places, rule analysis, the
    issues summary and the flags per visit."""
    q1 = metrics.hact_q1_by_month(scope, when, limits)
    q1_question = metrics.q1_question(when)
    if q1 is None:
        q1_key, q1 = "rating", metrics.rating_by_month(scope, when, limits)
    else:
        q1_key = "hact_q1"
    q1_template = _drill_template(scope, "month", q1_key) + f"&month={{drill}}&{q1_key}={{series_drill}}"
    buckets = metrics.score_buckets(scope, when, limits)
    issues = metrics.issues_summary(scope, when, limits)
    places = metrics.locations(scope, when, limits)
    place_rows = _with_urls(places["rows"], scope, "location")
    rule_rows = [
        {**r, "url": drill_url(scope, flag=r["code"]) if r["flagged"] else ""}
        for r in metrics.rule_analysis(scope, rules, when)
    ]
    flags = metrics.flag_distribution(scope, when, limits)
    narrow = _narrowed(scope, {"rating": "not_monitored", "status": "reported"})
    return {
        "chart_data": {
            "monthly_quality": metrics.monthly_quality(scope, when, limits),
            "monthly_volume": metrics.monthly_volume(scope, when, limits),
            "q1": {k: v for k, v in (q1 or {}).items() if k != "totals"},
            "buckets": buckets["items"],
        },
        "month_template": _drill_template(scope, "month") + "&month={drill}",
        "q1_key": q1_key,
        "q1_template": q1_template,
        "q1_question": q1_question,
        "q1_totals": [
            {**t, "url": f"{_drill_template(scope, q1_key)}&{q1_key}={t['code']}" if t["n"] else ""}
            for t in (q1 or {}).get("totals", ())
        ],
        "bucket_template": _drill_template(scope, "bucket") + "&bucket={drill}",
        "not_scored": buckets["not_scored"],
        "not_scored_url": drill_url(scope, bucket="none") if buckets["not_scored"] else "",
        "issues": [
            {
                **row,
                "url": drill_url(scope, issue=row["drill"]) if row["drill"] else "",
                "chips": [{**c, "url": reverse("fmm:visit", args=[c["key"]])} for c in row["chips"]],
            }
            for row in metrics.top_issues(scope, 10, when, rules)
        ],
        "places": {**places, "top": place_rows[:PLACES_TOP], "rest": place_rows[PLACES_TOP:]},
        "rule_rows": rule_rows,
        "issues_summary": {
            **issues,
            "r6_url": drill_url(scope, flag="R6") if issues["r6"]["n"] else "",
            "gaps_url": f"{reverse('fmm:drill')}?{narrow.query}" if issues["gaps"]["n"] else "",
            "high_flag_url": drill_url(scope, flags=f"{issues['high_flag']['at']}+")
            if issues["high_flag"]["n"]
            else "",
        },
        "flag_rows": [
            {**r, "url": drill_url(scope, flags=r["drill"]) if r["n"] else ""} for r in flags["rows"]
        ],
        "flags_scored": flags["scored"],
        "fields_found_url": reverse("admin:fmm_fieldmapping_changelist"),
    }


ENTITY_ROWS = 25


def _entity_kind(request: HttpRequest) -> str:
    wanted = request.GET.get("entity_kind", "")
    return wanted if wanted in KIND_LABELS else "pd"


def _entity_link(link: tuple[str, int] | None) -> str:
    if not link:
        return ""
    kind, pk = link
    return (
        reverse("reports:programme_detail", args=[pk])
        if kind == "pd"
        else reverse("reports:partner_profile", args=[pk])
    )


def _analysis_tab(
    request: HttpRequest, scope: Scope, when: str, limits: dict[str, int], rules: list
) -> dict[str, Any]:
    """The Analysis tab: highlights, governorates not visited, field offices, entity performance,
    quality by field office, sections, visit frequency by place, quality by rating, flags by rule,
    points by rule, programmatic visits and HACT, and follow-up."""
    highlights = metrics.highlights(scope, when, limits)
    kind = _entity_kind(request)
    show_all = request.GET.get("entity_all") == "1"
    performance = metrics.entities_performance(scope, kind, when)
    entity_rows = [
        {
            **row,
            "url": _entity_link(row["link"]),
            "last_label": rating_label(row["last"]["rating"])
            if not row["last"]["not_rated_yet"]
            else _("Not rated yet"),
        }
        for row in (performance["rows"] if show_all else performance["rows"][:ENTITY_ROWS])
    ]
    offices = metrics.offices(scope, when, limits)
    flag_frequency = metrics.flag_frequency(scope, rules, when)
    follow_up = metrics.action_points(scope, when, limits)
    hact = metrics.hact_programmatic(scope, when, limits)
    places = metrics.locations(scope, when, limits)
    place_rows = _with_urls(places["rows"], scope, "location")
    section_rows = []
    for row in metrics.sections(scope, when, limits):
        lines = [
            {
                **line,
                "url": reverse("fmm:visit", args=[line["key"]]),
                "rating_label": _("Not rated yet") if line["not_rated_yet"] else rating_label(line["rating"]),
            }
            for line in row["lines"]
        ]
        section_rows.append({**row, "lines": lines, "url": drill_url(scope, section=row["drill"])})
    return {
        "chart_data": {
            "bands": highlights["bands"],
            "flags": flag_frequency["pairs"],
        },
        "highlights": {
            **highlights,
            "reported_url": drill_url(scope, status="reported") if highlights["reported"] else "",
            "off_track_url": drill_url(scope, rating="off_track") if highlights["off_track"] else "",
            "kinds": [
                {
                    **k,
                    "url": f"{reverse('fmm:dashboard')}?"
                    + _page_query(_narrowed(scope, {"entity_type": k["kind"]}), tab="analysis"),
                }
                for k in highlights["kinds"]
            ],
        },
        "gaps": metrics.governorate_gaps(scope, when, limits),
        "offices": {
            **offices,
            "rows": _with_urls(offices["rows"], scope, "office"),
            "unknown": _with_urls([offices["unknown"]], scope, "office")[0] if offices["unknown"] else None,
        },
        "entity_kind": kind,
        "entity_kinds": [
            {
                "key": k,
                "label": label,
                "n": performance["kinds"].get(k, 0),
                "query": _page_query(scope, tab="analysis", entity_kind=k),
            }
            for k, label in KIND_LABELS.items()
        ],
        "entity_rows": entity_rows,
        "entity_total": len(performance["rows"]),
        "entity_all_query": _page_query(scope, tab="analysis", entity_kind=kind, entity_all="1"),
        "entity_year": performance["year"],
        "office_badges": _with_urls(metrics.office_rule_badges(scope, rules, when, limits), scope, "office"),
        "section_rows": section_rows,
        "places": {**places, "top": place_rows[:PLACES_TOP], "rest": place_rows[PLACES_TOP:]},
        "rating_rows": _with_urls(metrics.quality_by_rating(scope, when, limits), scope, "rating", "code"),
        "flag_frequency": flag_frequency,
        "flag_template": _drill_template(scope, "flag") + "&flag={drill}",
        "dimensions": metrics.dimension_breakdown(scope, rules, when),
        "hact": hact,
        "follow_up": {
            **follow_up,
            "overdue_list": [
                {**p, "visits": [{**v, "url": reverse("fmm:visit", args=[v["key"]])} for v in p["visits"]]}
                for p in follow_up["overdue_list"]
            ],
            "without_visits": [
                {"key": key, "name": name, "url": reverse("fmm:visit", args=[key])}
                for key, name in follow_up["without"]["visits"]
            ],
        },
        "assurance_url": f"{reverse('reports:assurance')}?hact_year={hact['year']}",
        "action_points_url": f"{reverse('reports:action_points')}?module=fm",
    }


def _map_tab(request: HttpRequest, scope: Scope, when: str) -> dict[str, Any]:
    """The Map tab: the visits against the planned locations of their programme documents (or of
    every active one, ``?pd_scope=active``), with ``?visit=<key>`` centred and opened."""
    from . import geo

    pd_scope = request.GET.get("pd_scope", "")
    pd_scope = pd_scope if pd_scope in geo.PD_SCOPES else "visited"
    data = geo.map_points(scope, pd_scope, when=when)
    config = dict(data["config"])  # the cached one is never changed
    focus_key = request.GET.get("visit", "").strip()[:40]
    focus = {"key": focus_key, "label": "", "reason": ""}
    if focus_key:
        row = next((r for r in data["visit_rows"] if r["key"] == focus_key), None)
        if row is not None:
            config["focus"] = row["label"]
            focus["label"] = row["label"]
        else:
            # not drawn: unknown, outside the filter, without coordinates, or past the points drawn
            found = Visit.objects.filter(key=focus_key).values_list("label", "latitude", "longitude").first()
            if found is None:
                reason = "unknown"
            elif not scope.visits().filter(key=focus_key).exists():
                reason = "outside"
            elif found[1] is None or found[2] is None:
                reason = "unlocated"
            else:
                reason = "capped"
            focus.update({"label": found[0] if found else focus_key, "reason": reason})
    other = "active" if pd_scope == "visited" else "visited"
    return {
        "map": data,
        "map_config": config,
        "map_focus": focus,
        "pd_scope": pd_scope,
        "pd_scope_query": _page_query(scope, tab="map", pd_scope=other),
    }


DRILL_ROWS = 50
DRILL_VALUES = {"rating": RATINGS, "status": STATUS_GROUPS, "entity_type": tuple(KIND_LABELS)}


def _drill_error(params) -> str:
    """Why a drill-down address is refused: a value that is not a code (a label as a chart draws it,
    "May 2026" or "80–100", is never read back)."""
    for key in DRILL_KEYS:
        for value in params.getlist(key):
            if value.strip() and not _drill_ok(key, value.strip()):
                return _("“%(value)s” is not a value of %(key)s.") % {"value": value[:40], "key": key}
    for key, allowed in DRILL_VALUES.items():
        for value in params.getlist(key):
            if value.strip() and value.strip() not in allowed:
                return _("“%(value)s” is not a value of %(key)s.") % {"value": value[:40], "key": key}
    return ""


@require_GET
def drill(request: HttpRequest) -> HttpResponse:
    """The visits behind a chart cell or a count: the scope plus the drill codes of the address, most
    urgent first, ending with a link to the same list in the Visits tab. A value that is not a code
    is refused (400)."""
    _enabled()
    error = _drill_error(request.GET)
    if error:  # plain text: the refused value is echoed back
        return HttpResponseBadRequest(error, content_type="text/plain; charset=utf-8")
    scope = Scope.from_params(request.GET, request.user)
    visits_url = f"{reverse('fmm:dashboard')}?{_page_query(scope, tab='visits')}"
    if not request.htmx:
        return redirect(visits_url)
    limits = metrics.thresholds()
    qs = scope.visits().select_related("partner").order_by(*SORTS[DEFAULT_SORT], "key")
    total = qs.count()
    rows = list(qs[:DRILL_ROWS])
    _decorate(rows, limits)
    context = {
        "scope": scope,
        "visits": rows,
        "total": total,
        "more": max(total - len(rows), 0),
        "chips": [c for c in _chips(scope) if not c.get("remove_in_words")] + _filter_chips(scope),
        "visits_url": visits_url,
        "limits": limits,
    }
    return render(request, "fmm/_drill.html", context)


def _filter_chips(scope: Scope) -> list[dict[str, str]]:
    """The filters a drill-down narrowed, in words (rating, status, office, section, entity type)."""
    out = []
    for value in scope.ratings:
        out.append({"label": f"{_('Rating')}: {rating_label(value)}"})
    for value in scope.statuses:
        out.append({"label": f"{_('Status')}: {_(STATUS_LABELS.get(value, value))}"})
    for value in scope.offices:
        out.append({"label": f"{_('Field office')}: {_('Office not known') if value == 'none' else value}"})
    for value in scope.sections:
        out.append({"label": f"{_('Section')}: {_('No section') if value == 'none' else value}"})
    for value in scope.entity_types:
        out.append({"label": f"{_('Entity type')}: {KIND_LABELS.get(value, value)}"})
    return out


# ------------------------------------------------------------------------------------------ pages
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """The page: filters, reference line, data notes, key figures and the tab chosen. An HTMX request
    (filter bar, tab bar) gets the results only."""
    _enabled()
    snap = status.snapshot()
    when = metrics.stamp(snap.last_refresh)
    scope = Scope.from_params(request.GET, request.user, when=when)
    tab = _tab(request)
    context = _results_context(request, scope, tab, snap, when)
    if request.htmx:
        return render(request, "fmm/_results.html", context)
    context.update(
        {
            "page_title": _("Monitoring insights"),
            "page_subtitle": _("eTools field monitoring: visits, report quality, findings and follow-up"),
            "breadcrumbs": _crumbs(),
            "options": options(when),
            "presets": [(k, _(v)) for k, v in _period_choices()],
            "kind_labels": KIND_LABELS,
            "rating_labels": RATING_LABELS,
            "status_labels": STATUS_LABELS,
        }
    )
    return render(request, "fmm/dashboard.html", context)


def _period_choices() -> list[tuple[str, str]]:
    from .scope import PRESET_LABELS

    return [(k, v) for k, v in PRESET_LABELS.items() if k != "year"]


@require_GET
def visits(request: HttpRequest) -> HttpResponse:
    """The visits table (sorting, pages); ``?export=csv`` gives every row of the filter as CSV, without
    the team, the visit lead or any narrative."""
    _enabled()
    scope = Scope.from_params(request.GET, request.user)
    if request.GET.get("export") == "csv":
        return _csv(scope)
    limits = metrics.thresholds()
    context = {"scope": scope, **_table(request, scope, limits)}
    return render(request, "fmm/_visits_table.html", context)


def _csv(scope: Scope) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="monitoring-visits-{stamp}.csv"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(CSV_HEADER)
    rows = list(scope.visits().select_related("partner").order_by(*SORTS[DEFAULT_SORT], "key"))
    reviews = _latest_reviews([v.key for v in rows])
    for v in rows:
        review = reviews.get(v.key)
        partner = (v.partner.short_name or v.partner.name) if v.partner else ""
        writer.writerow(
            [
                v.label,
                v.key,
                v.activity_id or "",
                v.reference,
                v.reference_number,
                v.start_date or "",
                v.end_date or "",
                v.status,
                v.status_group,
                partner,
                "; ".join(v.pd_numbers),
                "; ".join(v.cp_outputs),
                v.place_name,
                v.governorate_name,
                v.district_name,
                "; ".join(v.section_names),
                "; ".join(v.offices),
                v.rating,
                v.hact_q1,
                v.quality_score if v.quality_score is not None else "",
                v.score_band,
                " ".join(v.flags),
                v.urgency,
                v.action_points,
                v.action_points_open,
                v.action_points_overdue,
                review.status if review else "",
                timezone.localtime(review.created_at).date() if review else "",
            ]
        )
    return response


# ------------------------------------------------------------------------------------------ visit page
@require_GET
def visit(request: HttpRequest, key: str) -> HttpResponse:
    """One visit: header, links, place, sections, team, entities and narratives, quality, answers,
    programme activities, action points, HACT context, review and data notes. The citation target."""
    _enabled()
    found = get_object_or_404(Visit.objects.select_related("partner", "pd", "location", "site"), key=key)
    context = _visit_context(request, found)
    if request.htmx:
        return render(request, "fmm/_visit_body.html", {**context, "modal": True})
    context.update(
        {
            "page_title": found.label,
            "page_subtitle": _("eTools field monitoring visit"),
            "breadcrumbs": _crumbs({"label": found.label, "url": None}),
        }
    )
    return render(request, "fmm/visit.html", context)


def _visit_context(request: HttpRequest, v: Visit) -> dict[str, Any]:
    from neurodb.datamart.models import MonitoringFinding
    from neurodb.partnerships.models import PCA, PartnerOrganization
    from neurodb.watch import people

    from . import fields

    limits = metrics.thresholds()
    v.rating_label = rating_label(v.rating, v.status_group)
    v.not_rated_yet = _not_rated_yet(v.rating, v.status_group)
    v.urgency_title = urgency_text(v.urgency_parts)
    v.row_band = "red" if v.urgency >= limits["red"] else "amber" if v.urgency >= limits["amber"] else ""
    status_as_of = _day(v.last_modified) or _day(_last_fm_sync())

    entities = list(v.entity_rows.select_related("pd", "partner").order_by("datamart_id"))
    texts = dict(
        MonitoringFinding.objects.filter(pk__in=[e.finding_id for e in entities if e.finding_id]).values_list(
            "pk", "narrative_finding"
        )
    )
    for e in entities:
        e.kind_label = KIND_LABELS.get(e.kind, e.kind)
        e.rating_label = rating_label(e.rating, v.status_group)
        e.not_rated_yet = _not_rated_yet(e.rating, v.status_group)
        e.hact_q1_label = _(RATING_LABELS.get(e.hact_q1, "Other")) if e.hact_q1 else ""
        e.hact_q1_from_label = Q1_FROM.get(e.hact_q1_from, "")
        e.match_label = PD_MATCH.get(e.pd_match, "")
        narrative = (texts.get(e.finding_id) or "").strip()
        e.narrative = people.EMAIL.sub(people.EMAIL_WITHHELD, narrative)

    rules = {r.code: r for r in RuleSetting.objects.all()}
    results = list(v.rule_results.order_by("rule"))
    for r in results:
        setting = rules.get(r.rule)
        r.label = setting.label if setting else r.rule
        r.threshold = setting.threshold if setting else None
        r.state_label = RULE_STATES.get(r.status, r.status)
    total = sum(r.points for r in rules.values() if r.enabled)

    partners = list(PartnerOrganization.objects.filter(pk__in=v.partner_ids).only("pk", "name", "short_name"))
    pds = list(PCA.objects.filter(pk__in=v.pd_ids).only("pk", "number", "title"))
    links = list(VisitActionPoint.objects.filter(visit=v).select_related("action_point").order_by("pk"))
    for link in links:
        link.match_label = AP_MATCH.get(link.matched_by, link.matched_by)

    answers_available = fields.available("fm_questions", "answer") or fields.available(
        "fm_questions", "answer_label"
    )
    team_known = bool(v.team or v.team_unnamed) or fields.available("field_monitoring", "team")
    reviews = list(
        VisitReview.objects.filter(visit_key=v.key)
        .select_related("reviewed_by")
        .order_by("-created_at", "-pk")[:5]
    )
    return {
        "visit": v,
        "status_label": code_label(v.status) if v.status else _(STATUS_LABELS.get(v.status_group, "")),
        "hact_q1_label": _(RATING_LABELS.get(v.hact_q1, "Other")) if v.hact_q1 else "",
        "band_label": BANDS.get(v.score_band, ""),
        "status_as_of": status_as_of,
        "entities": entities,
        "results": results,
        "total_points": total,
        "partners": partners,
        "pds": pds,
        "action_points": links,
        "answers": _answers(v, entities, people) if answers_available else None,
        "answers_available": answers_available,
        "activities": v.programme_activities,
        "cp_outputs": _cp_outputs(v.cp_outputs),
        "hact": _hact(v, partners, pds),
        "sections_source": SOURCES.get(v.sections_from, ""),
        "offices_source": SOURCES.get(v.offices_from, ""),
        "located": _located(v),
        "team_known": team_known,
        "reviews": reviews,
        "review": reviews[0] if reviews else None,
        "can_review": access.can_review(request.user, v),
        "review_choices": VisitReview.Status.choices,
        "data_notes": _data_notes(v),
        "etools_url": _etools_url(v),
        "action_points_url": f"{reverse('reports:action_points')}?module=fm&visit={v.key}",
        "map_url": _map_url(v),
        "assurance_url": (
            f"{reverse('reports:assurance')}?hact_year={v.end_date.year}"
            if v.end_date
            else reverse("reports:assurance")
        ),
        "limits": limits,
    }


def _map_url(v: Visit) -> str:
    """The Map tab centred on the visit: its year, every section, so the visit is on the map ("" when
    it has no point or no end date, as the map would not show it)."""
    from .scope import link

    if v.latitude is None or v.longitude is None or v.end_date is None:
        return ""
    return link(year=v.end_date.year, tab="map", visit=v.key)


def _located(v: Visit) -> str:
    if v.located_by == "ancestor":
        return _("approximate: placed at %(place)s") % {"place": v.approximate_from or _("a larger area")}
    if v.located_by in LOCATED:
        return str(LOCATED[v.located_by])
    return _("no coordinates")


def _etools_url(v: Visit) -> str:
    template = getattr(settings, "FMM_ETOOLS_ACTIVITY_URL", "") or ""
    if not template or not v.activity_id:
        return ""
    return template.replace("{id}", str(v.activity_id))


def _answers(v: Visit, entities: list, people) -> dict[str, Any]:
    """The checklist answers of the visit grouped by question, Q1/Q2/Q3/PSEA tagged, with the entity
    each one applies to. Read from the answer records on demand; e-mail addresses hidden."""
    from . import parse

    by_entity = {e.pk: e for e in entities}
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row, shown, summary in parse.visit_answer_rows(v):
        group = groups.setdefault(
            row.question_key or row.question_text,
            {"question": row.question_text, "role": ROLE_LABELS.get(row.role, ""), "items": []},
        )
        if row.applies_to == "entity" and row.entity_id in by_entity:
            target = by_entity[row.entity_id].entity
        elif row.applies_to == "partner" and row.partner:
            target = f"{row.partner.short_name or row.partner.name} ({APPLIES['partner']})"
        else:
            target = str(APPLIES["visit"])
        group["items"].append(
            {
                "target": target,
                "answer": people.EMAIL.sub(people.EMAIL_WITHHELD, str(shown or "")),
                "summary": people.EMAIL.sub(people.EMAIL_WITHHELD, str(summary or "")),
                "answered": row.answered,
                "placeholder": row.placeholder,
            }
        )
    return {"groups": list(groups.values()), "asked": v.questions_asked, "answered": v.questions_answered}


def _cp_outputs(names: list[str]) -> list[dict[str, Any]]:
    """Each CP output of the visit, with the country programme output it matches (current cycle)."""
    out = [{"name": name, "code": "", "url": ""} for name in names]
    if not names:
        return out
    try:
        from neurodb.cpd.models import CountryProgramme, Output
        from neurodb.cpd.services import output_matches
    except ImportError:  # pragma: no cover - the app is always installed
        return out
    programme = CountryProgramme.objects.filter(current=True).first() or CountryProgramme.objects.first()
    if programme is None:
        return out
    outputs = list(Output.objects.filter(outcome__programme=programme))
    for item in out:
        match = next((o for o in outputs if output_matches(o, item["name"])), None)
        if match is not None:
            item["code"] = match.code
            item["url"] = f"{reverse('cpd:dashboard')}?cycle={programme.pk}"
    return out


def _hact(v: Visit, partners: list, pds: list) -> dict[str, Any] | None:
    """The partner's programmatic visits of the visit's year (eTools and NeuroDB's count) and, per
    programme document, the visits planned this quarter against FM visits to it in that quarter."""
    from neurodb.datamart.models import PartnerHACTYear, PlannedVisits

    from .models import VisitEntity

    if v.end_date is None:
        return None
    year = v.end_date.year
    counted = fm.programmatic_visits_by_partner(year) if partners else {}
    hact_rows = {
        row.partner_id: row for row in PartnerHACTYear.objects.filter(partner_id__in=v.partner_ids, year=year)
    }
    partner_lines = []
    for p in partners:
        row = hact_rows.get(p.pk)
        partner_lines.append(
            {
                "partner": p.short_name or p.name,
                "required": row.pv_required if row else None,
                "completed": row.pv_completed if row else None,
                "neurodb": counted.get(p.pk, 0),
            }
        )
    quarter = (v.end_date.month - 1) // 3 + 1
    start, end = quarter_of(v.end_date)
    planned = {
        row.intervention_id: row
        for row in PlannedVisits.objects.filter(intervention_id__in=v.pd_ids, year=year)
    }
    pd_lines = []
    for pd in pds:
        row = planned.get(pd.pk)
        done = (
            VisitEntity.objects.filter(pd_id=pd.pk, visit__end_date__gte=start, visit__end_date__lte=end)
            .values("visit_id")
            .distinct()
            .count()
        )
        pd_lines.append(
            {"pd": pd, "planned": getattr(row, f"q{quarter}", None) if row else None, "done": done}
        )
    return {"year": year, "quarter": quarter, "partners": partner_lines, "pds": pd_lines}


def _data_notes(v: Visit) -> list[str]:
    """The visit's data problems in plain words (``Visit.issues``: codes and counts, no free text)."""
    issues = v.issues or {}
    notes = []
    if issues.get("status_conflict"):
        written = ", ".join(f"{k or '—'} ({n})" for k, n in issues["status_conflict"].items())
        notes.append(_("Its finding rows disagree on the status: %(written)s.") % {"written": written})
    if issues.get("location_conflict"):
        notes.append(
            _("Its finding rows name %(n)s different places; the most frequent one is shown.")
            % {"n": issues["location_conflict"]}
        )
    if issues.get("pd_unresolved"):
        n = issues["pd_unresolved"]
        notes.append(
            ngettext(
                "%(n)s programme document reference was not matched to a programme document in NeuroDB.",
                "%(n)s programme document references were not matched to a programme document in NeuroDB.",
                n,
            )
            % {"n": n}
        )
    if issues.get("no_date"):
        notes.append(_("No end date in eTools: the visit is left out of every period."))
    if issues.get("rating_unknown"):
        written = ", ".join(f"{k} ({n})" for k, n in issues["rating_unknown"].items())
        notes.append(_("Ratings NeuroDB does not know: %(written)s.") % {"written": written})
    if issues.get("reference_conflict"):
        notes.append(
            _("Its reference is shared by %(n)s visits in eTools.") % {"n": issues["reference_conflict"]}
        )
    if issues.get("rows_without_partner"):
        n = issues["rows_without_partner"]
        notes.append(
            ngettext(
                "%(n)s finding row is not linked to a partner (unknown vendor number).",
                "%(n)s finding rows are not linked to a partner (unknown vendor number).",
                n,
            )
            % {"n": n}
        )
    return notes


# ------------------------------------------------------------------------------------------ review
@require_POST
def review(request: HttpRequest, key: str) -> HttpResponse:
    """Mark a visit reviewed, needing follow-up or with a data issue (Administrators, or a Section
    editor of one of the visit's sections)."""
    _enabled()
    found = get_object_or_404(Visit, key=key)
    if not access.can_review(request.user, found):
        raise PermissionDenied
    status_code = request.POST.get("status", "")
    note = " ".join(request.POST.get("note", "").split())
    error = ""
    if status_code not in VisitReview.Status.values:
        error = _("Choose reviewed, needs follow-up or data issue.")
    elif len(note) > 500:
        error = _("The note can be at most 500 characters.")
    if not error:
        VisitReview.objects.create(
            visit_key=found.key, status=status_code, note=note, reviewed_by=request.user
        )
    if not request.htmx:
        if error:
            return HttpResponseBadRequest(error)
        return redirect("fmm:visit", key=found.key)
    reviews = list(
        VisitReview.objects.filter(visit_key=found.key)
        .select_related("reviewed_by")
        .order_by("-created_at", "-pk")[:5]
    )
    context = {
        "visit": found,
        "reviews": reviews,
        "review": reviews[0] if reviews else None,
        "can_review": True,
        "review_choices": VisitReview.Status.choices,
        "review_error": error,
        "review_saved": not error,
        "note": note if error else "",
    }
    # htmx swaps no 4xx answer, so the form comes back with its error and a 200 for the user to see it
    return render(request, "fmm/_review_form.html", context)


# ------------------------------------------------------------------------------------------ look-up
def _find(text: str) -> list[Visit]:
    """The visits ``text`` names: "1722", "#1722", "Visit 1722", a key, a reference or a reference
    number (case and spacing ignored)."""
    token = _LOOKUP_PREFIX.sub("", text.strip()).strip()
    if not token:
        return []
    found: dict[int, Visit] = {}
    for v in Visit.objects.filter(key=token[:40]):
        found[v.pk] = v
    if token.isdigit() and len(token) <= 18:
        for v in Visit.objects.filter(activity_id=int(token)):
            found.setdefault(v.pk, v)
    reference = fm.norm_reference(token)
    if reference:
        for v in Visit.objects.filter(reference__iexact=reference) | Visit.objects.filter(
            reference_number__iexact=reference
        ):
            found.setdefault(v.pk, v)
    return sorted(found.values(), key=lambda v: v.end_date or datetime.date.min, reverse=True)[:10]


def _nearest(scope: Scope, number: int, limit: int = 3) -> list[Visit]:
    """The visits of the scope whose activity ids are closest to ``number``."""
    distance = Func(F("activity_id") - number, function="ABS", output_field=IntegerField())
    qs: QuerySet = scope.visits().exclude(activity_id=None).annotate(distance=distance)
    return list(qs.order_by("distance", "activity_id")[:limit])


@require_GET
def lookup(request: HttpRequest) -> HttpResponse:
    """Find a visit by its id, key, reference or reference number. One hit goes to the visit; a miss
    says so and offers the three nearest ids of the filter."""
    _enabled()
    text = " ".join(request.GET.get("id", "").split())[:100]
    found = _find(text) if text else []
    if len(found) == 1:
        url = found[0].get_absolute_url()
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)
    scope = Scope.from_params(request.GET, request.user)
    token = _LOOKUP_PREFIX.sub("", text).strip()
    nearest = _nearest(scope, int(token)) if token.isdigit() and len(token) <= 18 else []
    context = {"text": text, "token": token, "found": found, "nearest": nearest, "scope": scope}
    return render(request, "fmm/_lookup_result.html", context)
