"""The Monitoring insights page (``/fmm/``): its tabs (Insights, Quality, Analysis, Visits, Map), the
visits table and its CSV, the visit page, the visit look-up, the reviews, the drill-down window that
lists the visits behind a chart cell or a count, the AI brief's card (Regenerate starts the brief
in a background process; the card polls it) and what was sent for it, and the exports of the filter
(the header's Export menu: the Excel workbook, the printable report and the Power BI package, built by
:mod:`neurodb.fmm.exports`).

Every view reads the stored visits through a :class:`~neurodb.fmm.scope.Scope` built from the query
string; an HTMX request gets the partial it swaps in, a plain request the full page. With
``FMM_ENABLED`` off every view answers 404. The visit page reads its narratives from the findings and
its checklist answers from their records (``fmm.parse``), for staff only; no view here sends them
anywhere, and no view calls the AI but the chat's (``chat_stream``), which streams the answer of a
question about the filter's visits (``fmm.ai.chat``).
"""

from __future__ import annotations

import csv
import datetime
import functools
import re
from collections import OrderedDict
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import F, Func, IntegerField, QuerySet
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    QueryDict,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.web.templatetags.ui import code_label

from . import access, metrics, status
from . import action_points as ap_module
from .ai import budget, profiles
from .ai import insights as ai_insights
from .models import Insight, LocalActionPoint, RuleSetting, ScoreSetting, Visit, VisitActionPoint, VisitReview
from .scope import (
    DRILL_KEYS,
    KIND_LABELS,
    QUALITY_BANDS,
    QUALITY_LABELS,
    RATING_LABELS,
    RATINGS,
    STATUS_GROUPS,
    STATUS_LABELS,
    URGENCY_LABELS,
    URGENCY_LEVELS,
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
SORTS = {  # a visit without a score has no urgency: it comes after every urgency, either way
    "-urgency": (F("urgency").desc(nulls_last=True), F("end_date").desc(nulls_last=True)),
    "urgency": (F("urgency").asc(nulls_last=True), F("end_date").desc(nulls_last=True)),
    "-date": (F("end_date").desc(nulls_last=True),),
    "date": (F("end_date").asc(nulls_last=True),),
    "-quality": (F("quality_score").desc(nulls_last=True), F("urgency").desc(nulls_last=True)),
    "quality": (F("quality_score").asc(nulls_last=True), F("urgency").desc(nulls_last=True)),
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
    "quality_gap": gettext_lazy("quality gap"),
    "recency": gettext_lazy("recency"),
    "red_flags": gettext_lazy("red flags"),
}
RULE_STATES = {
    "pass": gettext_lazy("Passed"),
    "fail": gettext_lazy("Flagged"),
    "na": gettext_lazy("Not available"),
    "nap": gettext_lazy("Does not apply"),
    "off": gettext_lazy("Switched off"),
    "pending": gettext_lazy("Pending"),
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
# The answers eTools writes on a finding row (its FMM export), shown under each entity of the visit page
ROW_TEXTS = (
    ("q1_answer", gettext_lazy("Q1 – Implementation status")),
    ("q2_answer", gettext_lazy("Q2 – Activities monitored")),
    ("q3_answer", gettext_lazy("Q3 – Observations and action points")),
    ("supplies", gettext_lazy("Supplies")),
    ("psea", gettext_lazy("PSEA")),
)
VISIT_TEXTS = (("visit_goals", gettext_lazy("Visit goals")), ("objective", gettext_lazy("Objective")))
Q1_FROM = {
    "partner": gettext_lazy("given for the partner"),
    "visit": gettext_lazy("given for the whole visit"),
}
BANDS = {
    "high": gettext_lazy("High"),
    "medium": gettext_lazy("Medium"),
    "low": gettext_lazy("Low"),
    "pending": gettext_lazy("Pending"),
}
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
        # "Data available from X to Y": the visits' dates, for all time and custom dates
        "window": metrics.data_window(scope) if scope.preset in ("all_time", "custom") else None,
        "synced": snap.fm_synced,
        "refresh": refresh,
        "rules_version": (refresh.details or {}).get("rules_version") if refresh else None,
        "pending_version": snap.rules_version if snap.pending else None,
        "failed": snap.failed,
    }


def _how(setting: ScoreSetting, rules: list[RuleSetting] | None = None) -> dict[str, Any]:
    """What "What does quality mean for Lebanon?" shows, rendered from the rule set the engine applies:
    the band thresholds, the score categories with their weights, the core rules (FMS's HACT rules) and
    the additional rules switched on (id, name, category, deduction, what it checks), and urgency."""
    from .ai import profiles
    from .rules import TYPE_LABELS, code_order, nominal
    from .score import categories_of, category_labels, scored_statuses_of, weights_of

    rules = sorted(
        rules if rules is not None else RuleSetting.objects.all(), key=lambda r: code_order(r.code)
    )
    labels = category_labels(setting)
    shown = []
    for rule in rules:
        if not rule.enabled:
            continue
        rule.category_label = labels.get(rule.category, rule.category)
        rule.type_label = TYPE_LABELS.get(rule.type, rule.type)
        rule.weight = nominal(rule)
        shown.append(rule)
    version = profiles.published()
    country = version.profile.label if version is not None else "Lebanon"
    return {
        "rules": shown,
        "core": [r for r in shown if r.group == "core"],
        "additional": [r for r in shown if r.group != "core"],
        "off": sum(1 for r in rules if not r.enabled),
        "categories": [
            {"key": k, "label": labels.get(k, k), "weight": w} for k, w in categories_of(setting).items()
        ],
        "setting": setting,
        "country": country,
        "scored": [code_label(code) for code in fm.STATUSES if code in scored_statuses_of(setting)],
        "weights": {name: round(100 * value) for name, value in weights_of(setting).items()},
    }


def urgency_text(parts: dict[str, Any] | None) -> str:
    """ "quality gap 30 · recency 25 · red flags 10": the weighted parts of an urgency that count."""
    from neurodb.web.templatetags.ui import number

    parts = parts or {}
    names = [k for k in URGENCY_PARTS if k in parts] + [k for k in parts if k not in URGENCY_PARTS]
    out = [f"{URGENCY_PARTS.get(k, k)} {number(parts[k], 1)}" for k in names if parts[k]]
    return " · ".join(out) or _("nothing adds to it")


def urgency_band(urgency: int | None, limits: dict[str, int]) -> str:
    """red, amber or "" for an urgency under the thresholds set now; "" for a visit without one."""
    if urgency is None:
        return ""
    return "red" if urgency >= limits["red"] else "amber" if urgency >= limits["amber"] else ""


SIGNALS = {
    "no_follow_up": gettext_lazy("No follow-up action point yet for an off-track or constrained visit"),
    "ap_overdue": gettext_lazy("Overdue action points: %(n)s"),
    "ap_high_overdue": gettext_lazy("of them high priority: %(n)s"),
    "ap_high_open": gettext_lazy("High-priority action points open: %(n)s"),
    "report_late_days": gettext_lazy("Report late: the visit ended %(n)s days ago and is not reported"),
}


def signal_lines(signals: dict[str, Any] | None) -> list[str]:
    """The visit's follow-up and late-report signals in words (they are not part of its urgency)."""
    out = []
    for key, text in SIGNALS.items():
        value = (signals or {}).get(key)
        if value:
            out.append(str(text) % {"n": value} if "%(n)s" in str(text) else str(text))
    return out


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
        v.row_band = urgency_band(v.urgency, limits)
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
    cited = ai_insights.cited_keys(scope)
    for v in visits:
        v.ai_cited = v.key in cited
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
    if k["entities_not_rated_yet"]:
        rated += " · " + _("%(n)s not rated yet") % {"n": number(k["entities_not_rated_yet"])}
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
            "more_href": f"{reverse('fmm:dashboard')}?{_page_query(urgent, tab='visits')}",
            "more_label": _("View urgent visits →"),
        },
    ]


BRIEFING_TILES = (  # (key, label, definition): the morning briefing (FMS §7.1), in its order
    ("critical", gettext_lazy("Critical flags"), gettext_lazy("Visits with urgency %(red)s or more.")),
    (
        "avg_quality",
        gettext_lazy("Avg quality"),
        gettext_lazy("The mean quality score (0–100) of the scored visits."),
    ),
    ("low", gettext_lazy("Low quality visits"), gettext_lazy("Scored visits below %(low)s.")),
    (
        "critical_partners",
        gettext_lazy("Critical partners"),
        gettext_lazy("Partners with at least one visit of urgency %(red)s or more."),
    ),
    ("visits", gettext_lazy("Monitoring visits"), gettext_lazy("Visits, whatever their status.")),
    (
        "review",
        gettext_lazy("Pending report review"),
        gettext_lazy("Visits at review status only: reports awaiting the reviewer's sign-off."),
    ),
    ("submitted", gettext_lazy("Submitted"), gettext_lazy("Visits at submitted status.")),
    (
        "data_collection",
        gettext_lazy("Data collection"),
        gettext_lazy("Visits at data collection status: monitors collecting data."),
    ),
    (
        "assigned",
        gettext_lazy("Assigned"),
        gettext_lazy("Visits at assigned status: data collection not started."),
    ),
    ("completed", gettext_lazy("Completed"), gettext_lazy("Visits at completed status.")),
)


def _briefing(scope: Scope, when: str, limits: dict[str, int]) -> dict[str, Any]:
    """The morning briefing's tiles, each with its definition and the visits behind it, the top
    critical partners (each opening the page filtered on it) and the quality by governorate."""
    from dataclasses import replace

    from neurodb.web.templatetags.ui import number, percent

    data = metrics.briefing(scope, when, limits)
    year = metrics.briefing_scope(scope)
    words = {"red": data["red_at"], "low": data["low_below"]}
    urgent = drill_url(year, urgency_level="high")
    values = {
        "critical": (number(data["critical"]), urgent if data["critical"] else ""),
        "avg_quality": (
            percent(data["avg_quality"]) if data["avg_quality"] is not None else "—",
            f"{reverse('fmm:dashboard')}?{_page_query(year, tab='visits', sort='quality')}",
        ),
        "low": (number(data["low"]), drill_url(year, quality="low") if data["low"] else ""),
        "critical_partners": (number(data["critical_partners"]), urgent if data["critical_partners"] else ""),
        "visits": (number(data["visits"]), drill_url(year) if data["visits"] else ""),
    }
    for code, n in data["statuses"].items():
        values[code] = (number(n), drill_url(year, visit_status=code) if n else "")
    tiles = [
        {
            "key": key,
            "label": label,
            "info": str(info) % words,
            "value": values[key][0],
            "url": values[key][1],
        }
        for key, label, info in BRIEFING_TILES
    ]
    partners = [
        {
            **p,
            "url": f"{reverse('fmm:dashboard')}?"
            + _page_query(replace(year, partners=(p["id"],)), tab="visits"),
        }
        for p in data["top_partners"]
    ]
    governorates = [
        {
            **g,
            "url": f"{reverse('fmm:dashboard')}?"
            + _page_query(replace(year, governorate=g["key"]), tab="visits"),
        }
        for g in data["governorates"]
    ]
    return {
        "tiles": tiles,
        "partners": partners,
        "governorates": governorates,
        "start": data["start"],
        "end": data["end"],
        "filters": _filters_in_words(replace(scope, drill=())),
    }


def _filters_in_words(scope: Scope) -> str:
    """The page's filters besides the period, in words ("" when there is none): the briefing keeps
    them."""
    from .ai.facts import filters_text

    text = filters_text(scope, frozenset())
    return "" if text == "All visits" else text


def _page_query(scope: Scope, **extra: Any) -> str:
    """The dashboard's query string for ``scope`` plus ``extra`` (tab, sort, page)."""
    from urllib.parse import urlencode

    pairs = scope.pairs() + [(k, str(v)) for k, v in extra.items() if v not in (None, "")]
    return urlencode(pairs)


def _results_context(
    request: HttpRequest, scope: Scope, tab: str, snap: status.Snapshot, when: str
) -> dict[str, Any]:
    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    rules = list(RuleSetting.objects.all())
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
    if tab == "insights":
        context["briefing"] = _briefing(scope, when, limits)
        context["chat"] = _chat_context(request, scope)
    elif tab == "visits":
        context.update(_table(request, scope, limits, count=kpis["visits"]))
    elif tab == "quality":
        context.update(
            _quality_tab(
                scope,
                when,
                limits,
                rules,
                places_all=_places_all(request),
                trends="rule_trends" in request.GET,
            )
        )
    elif tab == "analysis":
        context.update(_analysis_tab(request, scope, when, limits, rules, setting))
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
    "modality": "modalities",
    "quality": "quality_bands",
    "urgency_level": "urgency_levels",
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


DRILL_SENTINEL = "fmm-drill-value"


def _with_urls(
    rows: list[dict[str, Any]], scope: Scope, key: str, field: str = "drill"
) -> list[dict[str, Any]]:
    """Copies of ``rows`` (kept in the cache: never changed in place), each with the drill-down window
    of its ``field`` value as ``url`` ("" when it has none). The address is built once and each row's
    value put in it (a table of 500 places built 500 scopes)."""
    from urllib.parse import quote_plus

    template = drill_url(scope, **{key: DRILL_SENTINEL})
    return [
        {
            **row,
            "url": template.replace(DRILL_SENTINEL, quote_plus(str(row[field])))
            if row.get(field) not in (None, "")
            else "",
        }
        for row in rows
    ]


def _day_text(day) -> str:
    """ "12 May 2026", or "—" without a date."""
    return date_format(day, "j M Y") if day else "—"


def _visit_url():
    """``lambda key: reverse("fmm:visit", args=[key])``, reversing once for a list of visits."""
    template = reverse("fmm:visit", args=["visit-key"])
    return lambda key: template.replace("visit-key", str(key))


PLACES_TOP = 10  # places shown before "Show all"


def _places_all(request: HttpRequest) -> bool:
    """``?places=all``: the places table's rows beyond the first ``PLACES_TOP`` are wanted (opening
    "Show all" asks for them; they are not sent with every tab)."""
    return request.GET.get("places") == "all"


def _shown_places(places: dict[str, Any], show_all: bool) -> list[dict[str, Any]]:
    """The rows of a places table that are written out: the first ``PLACES_TOP``, or all of them when
    asked for (each written row costs its links and dates)."""
    return places["rows"] if show_all else places["rows"][:PLACES_TOP]


def _places(scope: Scope, tab: str, places: dict[str, Any], rows: list, show_all: bool) -> dict[str, Any]:
    """A places table: its first ``PLACES_TOP`` rows, and the rest only when asked for (``?places=all``):
    up to 500 rows, most of the tab's weight, that only a reader who opens "Show all" reads. ``rows``
    are the rows written out (:func:`_shown_places`)."""
    return {
        **places,
        "top": rows[:PLACES_TOP],
        "rest": rows[PLACES_TOP:] if show_all else [],
        "rest_count": max(len(places["rows"]) - PLACES_TOP, 0),
        "all_query": _page_query(scope, tab=tab, places="all"),
    }


def _quality_tab(
    scope: Scope,
    when: str,
    limits: dict[str, int],
    rules: list,
    places_all: bool = False,
    trends: bool = False,
) -> dict[str, Any]:
    """The Quality tab: quality and visits by month, HACT Q1 by month (or the overall rating when no
    visit has a Q1 answer), the score distribution, recurring issues, places, rule analysis, the
    issues summary and the flags per visit. The rule score trends (their own query, per rule and
    month) are worked out only when their panel scrolls into view and asks for them (``trends``)."""
    visit_url = _visit_url()
    q1 = metrics.hact_q1_by_month(scope, when, limits)
    q1_question = metrics.q1_question(when)
    if q1 is None:
        q1_key, q1 = "rating", metrics.rating_by_month(scope, when, limits)
    else:
        q1_key = "hact_q1"
    q1_template = _drill_template(scope, "month", q1_key) + f"&month={{drill}}&{q1_key}={{series_drill}}"
    buckets = metrics.score_buckets(scope, when, limits)
    # the trends first, when asked for: the rule figures below are then summed from the same query
    rule_trends = metrics.rule_trends(scope, rules, when) if trends else None
    issues = metrics.issues_summary(scope, when, limits)
    places = metrics.locations(scope, when, limits)
    place_rows = [  # the last visit's date, written once per place shown
        {**p, "last_iso": p["last"].isoformat() if p["last"] else "", "last_text": _day_text(p["last"])}
        for p in _with_urls(_shown_places(places, places_all), scope, "location")
    ]
    rule_rows = [
        {**r, "url": drill_url(scope, flag=r["code"]) if r["flagged"] else ""}
        for r in metrics.rule_analysis(scope, rules, when)
    ]
    flags = metrics.flag_distribution(scope, when, limits)
    # Not monitored: planned, not conducted (a reported visit with nothing rated), a count apart
    narrow = _narrowed(scope, {"rating": "not_monitored"})
    not_monitored = issues["gaps"]["n"]
    not_monitored_url = f"{reverse('fmm:drill')}?{narrow.query}" if not_monitored else ""
    return {
        "chart_data": {
            "monthly_quality": metrics.monthly_quality(scope, when, limits),
            "monthly_volume": metrics.monthly_volume(scope, when, limits),
            "q1": {k: v for k, v in (q1 or {}).items() if k != "totals"},
            "buckets": buckets["items"],
        },
        "rule_trends": None if rule_trends is None else {"rule_trends": rule_trends},
        "rule_trends_query": _page_query(scope, tab="quality", rule_trends="1"),
        "rule_trend_template": _drill_template(scope, "month", "rule") + "&month={drill}&rule={series_drill}",
        "month_template": _drill_template(scope, "month") + "&month={drill}",
        "q1_key": q1_key,
        "q1_template": q1_template,
        "q1_question": q1_question,
        "q1_totals": [
            {**t, "url": f"{_drill_template(scope, q1_key)}&{q1_key}={t['code']}" if t["n"] else ""}
            for t in (q1 or {}).get("totals", ())
            if t["code"] != "not_monitored"
        ]
        + [
            {
                "code": "not_monitored",
                "label": _("Not Monitored"),
                "n": not_monitored,
                "url": not_monitored_url,
            }
        ],
        "bucket_template": _drill_template(scope, "bucket") + "&bucket={drill}",
        "not_scored": buckets["not_scored"],
        "not_scored_url": drill_url(scope, bucket="none") if buckets["not_scored"] else "",
        "issues": [
            {
                **row,
                "url": drill_url(scope, issue=row["drill"]) if row["drill"] else "",
                "chips": [{**c, "url": visit_url(c["key"])} for c in row["chips"]],
            }
            for row in metrics.top_issues(scope, 10, when, rules)
        ],
        "places": _places(scope, "quality", places, place_rows, places_all),
        "rule_rows": rule_rows,
        "issues_summary": {
            **issues,
            "r6_url": drill_url(scope, flag="R6") if issues["r6"]["n"] else "",
            "gaps_url": not_monitored_url,
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
    request: HttpRequest, scope: Scope, when: str, limits: dict[str, int], rules: list, setting=None
) -> dict[str, Any]:
    """The Analysis tab: highlights, governorates not visited, field offices, entity performance,
    quality by field office, sections, visit frequency by place, quality by rating, flags by rule,
    points by category, programmatic visits and HACT, and follow-up."""
    visit_url = _visit_url()
    highlights = metrics.highlights(scope, when, limits)
    kind = _entity_kind(request)
    show_all = request.GET.get("entity_all") == "1"
    # the entity table is worked out when its chips ask for it (``entity_kind``), else when it scrolls
    # into view: it is a whole pass over the entity rows, low on the tab
    eager = "entity_kind" in request.GET or show_all
    performance = (
        metrics.entities_performance(scope, kind, when)
        if eager
        else {"rows": [], "kinds": metrics.entity_kinds(scope, when), "year": None}
    )
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
    place_rows = _with_urls(_shown_places(places, _places_all(request)), scope, "location")
    section_rows = []
    day = functools.lru_cache(maxsize=None)(lambda d: date_format(d, "j M Y") if d else "")
    for row in metrics.sections(scope, when, limits):
        lines = [
            {
                **line,
                "url": visit_url(line["key"]),
                "rating_label": _("Not rated yet") if line["not_rated_yet"] else rating_label(line["rating"]),
                # the date beside the rating, written here once per line (a translated block per line
                # of up to 50 lines per section cost as much as the rest of the block)
                "when": (_("ends %(day)s") if line["not_rated_yet"] else _("rated %(day)s"))
                % {"day": day(line["date"])},
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
        "entity_lazy_query": "" if eager else _page_query(scope, tab="analysis", entity_kind=kind),
        "entity_year": performance["year"],
        "office_badges": _with_urls(metrics.office_rule_badges(scope, rules, when, limits), scope, "office"),
        "section_rows": section_rows,
        "places": _places(scope, "analysis", places, place_rows, _places_all(request)),
        "rating_rows": _rating_rows(scope, when, limits),
        "flag_frequency": flag_frequency,
        "flag_template": _drill_template(scope, "flag") + "&flag={drill}",
        "dimensions": metrics.dimension_breakdown(scope, rules, when, setting),
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


def _rating_rows(scope: Scope, when: str, limits: dict[str, int]) -> list[dict[str, Any]]:
    """Quality by finding rating, each row opening its visits (a row of no visit opens nothing)."""
    rows = _with_urls(metrics.quality_by_rating(scope, when, limits), scope, "rating", "code")
    return [{**row, "url": row["url"] if row["visits"] else ""} for row in rows]


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
DRILL_VALUES = {
    "rating": RATINGS,
    "status": STATUS_GROUPS,
    "entity_type": tuple(KIND_LABELS),
    "quality": QUALITY_BANDS,
    "urgency_level": URGENCY_LEVELS,
}


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
    """The filters a drill-down narrowed, in words (rating, status, office, section, entity type,
    modality, quality band, urgency level)."""
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
    for value in scope.modalities:
        out.append({"label": f"{_('Modality')}: {_('Modality not known') if value == 'none' else value}"})
    for value in scope.quality_bands:
        out.append({"label": f"{_('Quality')}: {_(QUALITY_LABELS.get(value, value))}"})
    for value in scope.urgency_levels:
        out.append({"label": f"{_('Urgency')}: {_(URGENCY_LABELS.get(value, value))}"})
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
            "actions": _export_menu(request, scope),
            "options": options(when),
            "presets": [(k, _(v)) for k, v in _period_choices()],
            "kind_labels": KIND_LABELS,
            "rating_labels": RATING_LABELS,
            "status_labels": STATUS_LABELS,
            "quality_labels": QUALITY_LABELS,
            "urgency_labels": URGENCY_LABELS,
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
    v.row_band = urgency_band(v.urgency, limits)
    v.signal_lines = signal_lines(v.signals)
    status_as_of = _day(v.last_modified) or _day(_last_fm_sync())

    entities = list(v.entity_rows.select_related("pd", "partner").order_by("datamart_id"))
    found_rows = MonitoringFinding.objects.filter(pk__in=[e.finding_id for e in entities if e.finding_id])
    texts, records = {}, {}
    for pk, narrative, data in found_rows.values_list("pk", "narrative_finding", "data"):
        texts[pk], records[pk] = narrative, data
    visit_texts: dict[str, str] = {}
    for e in entities:
        e.row_texts = _row_texts(records.get(e.finding_id), ROW_TEXTS, people)
        for _name, label, text in _row_texts(records.get(e.finding_id), VISIT_TEXTS, people):
            visit_texts.setdefault(str(label), text)
        e.kind_label = KIND_LABELS.get(e.kind, e.kind)
        e.rating_label = rating_label(e.rating, v.status_group)
        e.not_rated_yet = _not_rated_yet(e.rating, v.status_group)
        e.hact_q1_label = _(RATING_LABELS.get(e.hact_q1, "Other")) if e.hact_q1 else ""
        e.hact_q1_from_label = Q1_FROM.get(e.hact_q1_from, "")
        e.match_label = PD_MATCH.get(e.pd_match, "")
        narrative = (texts.get(e.finding_id) or "").strip()
        e.narrative = people.EMAIL.sub(people.EMAIL_WITHHELD, narrative)

    from .rules import TYPE_LABELS, code_order
    from .score import category_labels

    rules = {r.code: r for r in RuleSetting.objects.all()}
    categories = category_labels(ScoreSetting.load())
    results = sorted(v.rule_results.all(), key=lambda r: code_order(r.rule))
    for r in results:
        setting = rules.get(r.rule)
        r.label = setting.label if setting else r.rule
        r.type_label = TYPE_LABELS.get(setting.type, "") if setting else ""
        r.category_label = categories.get(setting.category, setting.category) if setting else ""
        r.state_label = RULE_STATES.get(r.status, r.status)
    # switched off (the rules off, and the AI checks while they are): listed once, after the rules that ran
    shown = [r for r in results if r.status != "off"]
    switched_off = sorted(
        {r.rule for r in results if r.status == "off"} | {code for code, s in rules.items() if not s.enabled},
        key=code_order,
    )

    partners = list(PartnerOrganization.objects.filter(pk__in=v.partner_ids).only("pk", "name", "short_name"))
    pds = list(PCA.objects.filter(pk__in=v.pd_ids).only("pk", "number", "title"))
    links = list(VisitActionPoint.objects.filter(visit=v).select_related("action_point").order_by("pk"))
    reviews = ap_module.current_reviews([link.action_point for link in links])
    checks = ap_module.latest_verifications([link.action_point.datamart_id for link in links])
    for link in links:
        link.match_label = AP_MATCH.get(link.matched_by, link.matched_by)
        link.confidence_label = ap_module.CONFIDENCE_LABELS[
            ap_module.CONFIDENCE.get(link.matched_by, "medium")
        ]
        link.review = reviews.get(link.action_point.datamart_id)
        link.check = checks.get(link.action_point.datamart_id)
    local_points = ap_module.visit_local_points(v.key)
    for point in local_points:
        point.can_change = ap_module.can_change_local(request.user, point, v)
    if any(point.is_follow_up for point in local_points) and "no_follow_up" in (v.signals or {}):
        # a NeuroDB action point follows the visit up: the signal no longer holds
        v.signal_lines = signal_lines({k: val for k, val in v.signals.items() if k != "no_follow_up"})

    answers_available = fields.available("fm_questions", "answer") or fields.available(
        "fm_questions", "answer_label"
    )
    team_known = bool(v.team or v.team_unnamed) or fields.available("field_monitoring", "team")
    landing = Scope.from_params(QueryDict(""), request.user)  # the filter the user lands on
    brief, cited = ai_insights.citing(v.key, landing)
    reviews = list(
        VisitReview.objects.filter(visit_key=v.key)
        .select_related("reviewed_by")
        .order_by("-created_at", "-pk")[:5]
    )
    return {
        "visit": v,
        "visit_texts": list(visit_texts.items()),
        "status_label": code_label(v.status) if v.status else _(STATUS_LABELS.get(v.status_group, "")),
        "hact_q1_label": _(RATING_LABELS.get(v.hact_q1, "Other")) if v.hact_q1 else "",
        "band_label": BANDS.get(v.score_band, ""),
        "status_as_of": status_as_of,
        "entities": entities,
        "results": shown,
        "results_off": switched_off,
        "category_deductions": [
            {"label": categories.get(key, key), "points": points}
            for key, points in sorted((v.category_deductions or {}).items(), key=lambda kv: -kv[1])
        ],
        "partners": partners,
        "pds": pds,
        "action_points": links,
        "local_action_points": local_points,
        "local_statuses": LocalActionPoint.Status.choices,
        "can_add_local": ap_module.can_add_local(request.user),
        "answers": _answers(v, entities, people) if answers_available else None,
        "answers_available": answers_available,
        "activities": v.programme_activities,
        "cp_outputs": _cp_outputs(v.cp_outputs),
        "hact": _hact(v, partners, pds),
        "pd_context": _pd_context(v),
        "sections_source": SOURCES.get(v.sections_from, ""),
        "offices_source": SOURCES.get(v.offices_from, ""),
        "located": _located(v),
        "team_known": team_known,
        "reviews": reviews,
        "review": reviews[0] if reviews else None,
        "can_review": access.can_review(request.user, v),
        "review_choices": VisitReview.Status.choices,
        "data_notes": _data_notes(v),
        "cited": {"lines": cited, "brief": brief, "scope_label": landing.label()} if cited else None,
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


def _row_texts(record: Any, wanted, people) -> list[tuple[str, Any, str]]:
    """(field, label, text) of each answer of ``wanted`` that a finding row's record holds, read under
    the key Fields found chose, e-mail addresses hidden; for staff, on the visit page only."""
    from neurodb.datamart import catalogue

    from . import fields, parse

    if not isinstance(record, dict):
        return []
    data = catalogue.scrub(record)
    out = []
    for name, label in wanted:
        key = fields.key_for("field_monitoring", name)
        text = parse.value(data, key, "text") if key else None
        if text:
            out.append((name, label, people.EMAIL.sub(people.EMAIL_WITHHELD, text)))
    return out


def _pd_context(v: Visit) -> list[dict[str, Any]]:
    """Item 11a: for each programme document of the visit, what the partner reported and the other
    visits to it within 90 days of the visit (``services.pd_context``)."""
    from . import services

    if not v.pd_ids:
        return []
    return services.pd_context(v.pd_ids, around=v.end_date, exclude_key=v.key)


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


# ------------------------------------------------------------------------------------------ AI brief
INSIGHT_MESSAGES = {
    "stopped": gettext_lazy("The last attempt stopped; you can try again."),
    "privacy": gettext_lazy("Insights could not be written safely; administrators have been told."),
    "refused": gettext_lazy("The AI did not write this brief; the brief below is written by NeuroDB."),
    "malformed": gettext_lazy("The AI's answer could not be read; the last brief is shown."),
    "up to date": gettext_lazy("Up to date: the brief below was written from the same data."),
}
PRIORITY_STATUS = {"High": "off_track", "Medium": "constrained", "Low": "not_monitored"}
SAMPLING_NAMES = {"temperature": "temp", "top_p": "top-p"}


def _insight_message(row) -> str:
    """What a brief that was not written says on the card."""
    from neurodb.assistant.views import NO_CREDIT

    reason = row.reason or ""
    if reason == ai_insights.CUT_OFF:
        return _("The brief was cut off at %(n)s tokens; an administrator can raise the limit.") % {
            "n": f"{row.max_output_tokens:,}"
        }
    if reason == ai_insights.QUOTA:
        return NO_CREDIT
    if reason in INSIGHT_MESSAGES:
        return str(INSIGHT_MESSAGES[reason])
    return reason


def _sampling_chips(row) -> list[dict[str, str]]:
    """ "temp 0.30 · applied", "top-p · not set (API default 1.00)": what was asked and what happened."""
    out = []
    for parameter in ("temperature", "top_p"):
        found = (row.sampling or {}).get(parameter) or {}
        name = SAMPLING_NAMES[parameter]
        asked, state = found.get("asked"), found.get("state", "not_set")
        if state == "not_set" or asked is None:
            text = f"{name} · " + (_("not set (API default 1.00)") if parameter == "top_p" else _("not set"))
        elif state == "off":
            text = f"{name} · " + _("off")
        else:
            words = {
                "applied": _("applied"),
                "not_applied": _("not applied"),
                "known_rejected": _("not applied"),
            }
            text = f"{name} {asked:.2f} · {words.get(state, _('sent'))}"
        out.append(
            {"text": text, "title": found.get("why", ""), "warn": state in ("not_applied", "known_rejected")}
        )
    return out


def _visit_names(keys: set[str]) -> dict[str, str]:
    return dict(Visit.objects.filter(key__in=list(keys)).values_list("key", "label")) if keys else {}


def _brief_blocks(
    data: dict[str, Any], actions: list[dict], parts: list[dict[str, Any]]
) -> tuple[list[dict], list[dict]]:
    """The parts of a brief (its version's: label, paragraph or bullets) and its priority action
    points, each sentence and action with its visit chips; an action written out as
    "[PRIORITY: High] Section / Partner — action — responsible — timeframe"."""
    from .ai import sections as sections_module

    wanted: set[str] = set()
    for name, entries in data.items():
        if name == "notes" or not isinstance(entries, list):
            continue
        for entry in entries:
            wanted.update(filter(None, (ai_insights.visit_key_of(k) for k in entry.get("keys") or ())))
    for entry in actions or []:
        wanted.update(filter(None, (ai_insights.visit_key_of(k) for k in entry.get("keys") or ())))
    labels = _visit_names(wanted)

    def chips(keys) -> list[dict[str, str]]:
        out, seen = [], set()
        for key in keys or ():
            visit = ai_insights.visit_key_of(key)
            if visit and visit in labels and visit not in seen:
                seen.add(visit)
                out.append({"key": visit, "label": labels[visit]})
        return out

    notes = data.get("notes") or {}
    sections = [
        {
            "key": part["key"],
            "title": part["label"],
            "format": part["format"],
            "sentences": [
                {"text": s.get("text", ""), "chips": chips(s.get("keys"))}
                for s in data.get(part["key"]) or []
            ],
            "note": notes.get(part["key"], ""),
        }
        for part in sections_module.text_parts(parts)
    ]
    lines = [
        {
            **action,
            "line": sections_module.action_line(action),
            "where": " / ".join(p for p in (action.get("section"), action.get("partner")) if p),
            "status": PRIORITY_STATUS.get(action.get("priority"), "unknown"),
            "chips": chips(action.get("keys")),
        }
        for action in actions or []
    ]
    return sections, lines


def _insight_context(request: HttpRequest, scope: Scope, message: str = "") -> dict[str, Any]:
    """The brief card: the brief shown (kept or code-written), its header, chips, quota and buttons."""
    from .ai import sections as sections_module

    version = profiles.published()
    brief = ai_insights.current(scope, version)
    row = brief.insight
    parts = sections_module.of(row.version if row is not None else version)
    if row is not None:
        sections, actions = _brief_blocks(row.sections or {}, row.actions or [], parts)
    else:
        written = brief.fallback or {}
        sections, actions = _brief_blocks(written.get("sections") or {}, written.get("actions") or [], parts)
    used, allowed = budget.quota("insights", request.user, version)
    on = version is not None and budget.switched_on(version)
    # a brief the AI wrote from the very same input would be reused, so Regenerate has nothing to do;
    # a code-written one (the AI's sentences did not pass) may be tried again
    reused = brief.up_to_date and row is not None and row.status in ai_insights.WRITTEN
    if not on:
        disabled = _("AI switched off")
    elif reused:
        disabled = _("Up to date")
    elif brief.reason and brief.reason != ai_insights.NOT_YET:  # paused, switched off or too few visits
        disabled = brief.reason
    else:
        refused = ai_insights.gate(scope, version, request.user)
        disabled = refused[1] if refused else ""
    rules_version = (row.rules_version if row else None) or ai_insights._refresh_state()[0]
    return {
        "scope": scope,
        "brief": brief,
        "row": row,
        "sections": sections,
        "actions": actions,
        "actions_title": (sections_module.action_part(parts) or {}).get("label", ""),
        "version": version,
        "shown_version": row.version if row else version,
        "rules_version": rules_version,
        "chips": _sampling_chips(row) if row is not None and row.called else [],
        "quota": {"used": used, "allowed": allowed, "office": budget.office_share() if on else None},
        "ai_on": on,
        "disabled": disabled,
        "message": message,
        "is_admin": access.is_admin(request.user),
        "fallback_note": row is not None and row.status == "fallback",
    }


def _insight_running(request: HttpRequest, scope: Scope, row) -> HttpResponse:
    return render(request, "fmm/_insights.html", {"scope": scope, "running": row})


def _running_brief(scope: Scope, version):
    """The brief of ``scope`` being written with ``version`` now, if any."""
    if version is None:
        return None
    return Insight.objects.filter(
        scope_hash=scope.hash(), version=version, status=Insight.Status.RUNNING
    ).first()


@require_http_methods(["GET", "POST"])
def insights(request: HttpRequest) -> HttpResponse:
    """The AI brief card. GET shows it (never calls the AI); ``?running=<pk>`` polls a brief being
    written. POST (Regenerate) runs the cheap gates, then starts the brief in a background process and
    answers at once with the card that polls it; a second click gets the brief already being written."""
    _enabled()
    scope = Scope.from_params(request.GET, request.user)
    version = profiles.published()
    message = ""
    if request.method == "POST":
        busy = _running_brief(scope, version)
        if busy is not None:
            return _insight_running(request, scope, busy)
        refused = ai_insights.gate(scope, version, request.user) if version is not None else None
        if version is None:
            message = _("AI switched off")
        elif refused:
            row = ai_insights.record_refusal(scope, version, request.user, Insight.Trigger.USER, refused)
            message = _insight_message(row)
        else:
            row = ai_insights.start(scope, version, request.user, Insight.Trigger.USER)
            return _insight_running(request, scope, row)
        return render(request, "fmm/_insights.html", _insight_context(request, scope, message))
    ai_insights.expire_stale()
    running = request.GET.get("running", "")
    if running.isdigit():  # only a brief of this filter, and never an administrator's test run
        row = (
            Insight.objects.filter(pk=int(running), scope_hash=scope.hash())
            .exclude(trigger=Insight.Trigger.TEST)
            .first()
        )
        if row is not None and row.status == Insight.Status.RUNNING:
            return _insight_running(request, scope, row)
        if row is not None and row.status not in ai_insights.SHOWN:
            message = _insight_message(row)
    else:
        row = _running_brief(scope, version)
        if row is not None:
            return _insight_running(request, scope, row)
    return render(request, "fmm/_insights.html", _insight_context(request, scope, message))


@require_GET
def insight_sent(request: HttpRequest, pk: int) -> HttpResponse:
    """What was sent for a brief: the facts and the notes exactly as sent (redacted), while they are
    kept (``FMM_PAYLOAD_RETENTION_DAYS``)."""
    import json

    _enabled()
    row = get_object_or_404(Insight.objects.select_related("version"), pk=pk)
    if row.trigger == Insight.Trigger.TEST and not access.is_admin(request.user):
        raise Http404("Test runs are for administrators")
    payload = row.sent_payload
    narratives = list((payload or {}).get("narratives", {}).values()) if payload else []
    facts = {k: v for k, v in (payload or {}).items() if k != "narratives"} if payload else None
    context = {
        "row": row,
        "narratives": narratives,
        "facts_json": json.dumps(facts, indent=2, ensure_ascii=False, sort_keys=True) if facts else "",
        "retention_days": settings.FMM_PAYLOAD_RETENTION_DAYS,
    }
    return render(request, "fmm/_insight_sent.html", context)


# ------------------------------------------------------------------------------------------ chat
# the starter questions are the published prompt version's (an administrator edits them); these defaults
# serve while no version is published
CHAT_OFF = gettext_lazy("Chat is not available: the AI is switched off.")
CHAT_DISABLED = gettext_lazy("Chat is not available: switched off by an administrator.")
CHAT_BUSY = gettext_lazy("The chat is busy; please try again in a minute.")
MAX_RUNNING_PER_USER = 2


def _chat_context(request: HttpRequest, scope: Scope) -> dict[str, Any]:
    """The chat panel of the Insights tab: whether it can be used (and why not), the person's questions
    today against the daily quota, and the starter questions (the published version's)."""
    from .models import default_chat_examples

    version = profiles.published()
    on = version is not None and budget.switched_on(version)
    reason = ""
    if not on:
        reason = str(CHAT_OFF)
    elif not version.chat_enabled:
        reason = str(CHAT_DISABLED)
    used, allowed = budget.quota("chat", request.user, version) if version is not None else (0, 0)
    return {
        "on": not reason,
        "reason": reason,
        "used": used,
        "allowed": allowed,
        "examples": list(version.chat_examples if version is not None else default_chat_examples()),
    }


def _chat_running(version, user=None) -> int:
    """Chat answers being written now (of ``user``, else on the whole site). A row left "in progress" by
    a stopped server stops counting once the version's time limit has long passed."""
    from .models import ChatQuestion

    since = timezone.now() - datetime.timedelta(seconds=version.chat_time_limit + 30)
    rows = ChatQuestion.objects.filter(status=ChatQuestion.Status.IN_PROGRESS, created_at__gte=since)
    return rows.filter(user=user).count() if user is not None else rows.count()


@require_POST
def chat_stream(request: HttpRequest) -> HttpResponse:
    """A question of Chat with Data, answered as Server-Sent Events about the visits of the page's
    filter (``scope``: the page's query string). Checked in order: the question (1-1,000 characters),
    the AI switched on, the person's daily quota (429), the questions being answered (at most 2 per
    person and ``FMM_CHAT_MAX_RUNNING`` on the site: 503), the day's AI budget and the chat switched on
    in the published prompt version. Ask NeuroDB's hourly limit and its log are not touched."""
    import uuid

    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .ai import chat as ai_chat
    from .models import AIState, ChatQuestion

    _enabled()
    question = (request.POST.get("question") or "").replace("\x00", "").strip()
    if not question:
        return JsonResponse({"error": _("Type a question first.")}, status=400)
    if len(question) > ai_chat.QUESTION_CHARS:
        return JsonResponse({"error": _("Please keep questions under 1,000 characters.")}, status=400)
    scope = Scope.from_params(QueryDict(str(request.POST.get("scope") or "")), request.user)
    try:
        conversation = uuid.UUID(str(request.POST.get("conversation") or ""))
    except ValueError:
        conversation = uuid.uuid4()
    version = profiles.published()
    if version is None or not budget.switched_on(version):
        return JsonResponse({"error": str(CHAT_OFF)}, status=503)

    def limited(reason: str) -> None:
        ChatQuestion.objects.create(
            user=request.user,
            conversation=conversation,
            scope_hash=scope.hash(),
            scope=scope.canonical(),
            version=version,
            question=ai_chat.clean_question(question),
            status=ChatQuestion.Status.LIMITED,
            error=reason,
        )

    AIState.load()
    with transaction.atomic():
        # One chat start at a time on the site (the AI state row) and per person: questions sent at the
        # same time are checked one after the other, and the row written here counts at once.
        AIState.objects.select_for_update().get(pk=1)
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        used, allowed = budget.quota("chat", request.user, version)
        if used >= allowed:
            limited("quota")
            message = _("You have asked %(n)s questions today; the count starts again tomorrow.") % {
                "n": allowed
            }
            return JsonResponse({"error": message}, status=429)
        if (
            _chat_running(version, request.user) >= MAX_RUNNING_PER_USER
            or _chat_running(version) >= settings.FMM_CHAT_MAX_RUNNING
        ):
            return JsonResponse({"error": str(CHAT_BUSY)}, status=503)
        ok, why = budget.allowed("chat", request.user, ai_chat.TOKENS, version=version)
        if not ok:
            if why == budget.BUDGET:
                limited("budget")
                return JsonResponse({"error": budget.REASONS[why]}, status=429)
            return JsonResponse(
                {"error": budget.REASONS[why] if why != budget.OFF else str(CHAT_OFF)}, status=503
            )
        if not version.chat_enabled:
            return JsonResponse({"error": str(CHAT_DISABLED)}, status=503)
        turns, seen, numbers = ai_chat.history(request.user, conversation, scope.hash())
        row = ChatQuestion.objects.create(
            user=request.user,
            conversation=conversation,
            scope_hash=scope.hash(),
            scope=scope.canonical(),
            version=version,
            question=ai_chat.clean_question(question),
            status=ChatQuestion.Status.IN_PROGRESS,
            model=profiles.model_of(version),
        )
    ctx = ai_chat.context(scope, version, seen, numbers)
    response = StreamingHttpResponse(
        ai_chat.stream(request, row, ctx, version, turns), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering: tokens reach the browser as they arrive
    return response


# ------------------------------------------------------------------------------------------ exports
def _export_menu(request: HttpRequest, scope: Scope) -> list[dict[str, Any]]:
    """The page header's Export menu, for the filter shown: the visits CSV, the Excel workbook, the PDF
    report and the Power BI package; for Administrators, the Power BI keys of the live connection. Each
    filter link takes the address bar's filter when clicked (``data-current-query``, app.js): the filter
    bar changes the address, not the header."""
    query = scope.query
    items = [
        {
            "label": _("CSV (visits)"),
            "url": f"{reverse('fmm:visits')}?{query}&export=csv",
            "follow": True,
            "extra": "export=csv",
        },
        {"label": _("Excel workbook"), "url": f"{reverse('fmm:export_xlsx')}?{query}", "follow": True},
        {
            "label": _("PDF report"),
            "url": f"{reverse('fmm:report')}?{query}",
            "follow": True,
            "new_tab": True,
        },
        {"label": _("Power BI package"), "url": f"{reverse('fmm:export_powerbi')}?{query}", "follow": True},
    ]
    if access.is_admin(request.user):
        items.append(
            {"label": _("Power BI live connection…"), "url": reverse("admin:fmm_powerbikey_changelist")}
        )
    return [{"label": _("Export"), "icon": "download", "menu": items}]


@require_GET
def export_xlsx(request: HttpRequest) -> HttpResponse:
    """The Excel workbook of the filter (``fmm.exports``): About, Visits (FMS's column names), Rule
    results, Partners, Field offices, Sections, Flags and Action points; no person in it."""
    from . import exports

    _enabled()
    when = metrics.stamp()
    scope = Scope.from_params(request.GET, request.user, when=when)
    return exports.workbook(scope, when)


@require_GET
def export_powerbi(request: HttpRequest) -> HttpResponse:
    """The Power BI package of the filter: the visits, rule results, action points and partners as CSV
    files, the Power Query script that loads them and README.txt, in one ZIP file."""
    from . import exports

    _enabled()
    when = metrics.stamp()
    scope = Scope.from_params(request.GET, request.user, when=when)
    filename, content = exports.powerbi_package(scope, when)
    response = HttpResponse(content, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


REPORT_PARTNERS = 15
REPORT_FLAGS = 10
REPORT_URGENT = 10


@require_GET
def report(request: HttpRequest) -> HttpResponse:
    """The printable report of the filter (A4, laid out like the "FMM Analysis" report): the browser's
    print dialog opens on it, and staff save it as PDF. Every figure is the page's own."""
    _enabled()
    snap = status.snapshot()
    when = metrics.stamp(snap.last_refresh)
    scope = Scope.from_params(request.GET, request.user, when=when)
    context = _report_context(request, scope, snap, when)
    context.update(
        {
            "page_title": _("Monitoring insights report"),
            "page_subtitle": scope.label(),
            "breadcrumbs": _crumbs({"label": _("Report"), "url": None}),
        }
    )
    return render(request, "fmm/report.html", context)


def _report_context(request: HttpRequest, scope: Scope, snap: status.Snapshot, when: str) -> dict[str, Any]:
    from .ai import sections as sections_module

    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    rules = list(RuleSetting.objects.all())
    limits = metrics.thresholds(setting)
    context: dict[str, Any] = {
        "scope": scope,
        "reference": _reference(scope, snap),
        "filters": _filters_in_words(scope),
        "has_visits": bool(snap.visits),
        "how": _how(setting, rules),
        "limits": limits,
        "dashboard_url": f"{reverse('fmm:dashboard')}?{scope.query}",
    }
    context["urgency_weights"] = [
        (URGENCY_PARTS.get(name, name), share) for name, share in context["how"]["weights"].items()
    ]
    if not snap.visits:
        return context
    kpis = metrics.kpis(scope, when, limits)
    version = profiles.published()
    brief = ai_insights.current(scope, version)
    row = brief.insight
    parts = sections_module.of(row.version if row is not None else version)
    written = (row.sections or {}) if row is not None else (brief.fallback or {}).get("sections") or {}
    written_actions = (row.actions or []) if row is not None else (brief.fallback or {}).get("actions") or []
    brief_sections, brief_actions = _brief_blocks(written, written_actions, parts)
    ratings = metrics.quality_by_rating(scope, when, limits)
    highlights = metrics.highlights(scope, when, limits)
    flags = metrics.flag_frequency(scope, rules, when)
    urgent = list(
        scope.visits().select_related("partner").order_by(*SORTS[DEFAULT_SORT], "key")[:REPORT_URGENT]
    )
    _decorate(urgent, limits)
    partners = [r for r in metrics.breakdown(scope, "partner", when, limits) if r["key"] != "none"]
    sections = metrics.breakdown(scope, "section", when, limits)
    context.update(
        {
            "kpi_tiles": _kpi_tiles(scope, kpis),
            "kpis": kpis,
            "briefing": _briefing(scope, when, limits),
            "brief": brief,
            "brief_row": row,
            "brief_by_ai": row is not None and row.status in ai_insights.WRITTEN,
            "brief_sections": brief_sections,
            "brief_actions": brief_actions,
            "actions_title": (sections_module.action_part(parts) or {}).get("label", ""),
            "ratings": ratings,
            "highlights": highlights,
            "by_section": sections,
            "by_office": metrics.breakdown(scope, "office", when, limits),
            "partners": partners[:REPORT_PARTNERS],
            "partners_more": max(len(partners) - REPORT_PARTNERS, 0),
            "flags": [r for r in flags["rows"] if r["n"]][:REPORT_FLAGS],
            "urgent": urgent,
            "hact": metrics.hact_programmatic(scope, when, limits),
            "follow_up": metrics.action_points(scope, when, limits),
            "points_by_section": [r for r in sections if r["open_action_points"]],
            "chart_data": {
                "ratings": [[str(r["label"]), r["visits"], r["code"]] for r in ratings],
                "bands": highlights["bands"],
                "flags": [[r["label"], r["n"], r["code"]] for r in flags["rows"] if r["n"]][:REPORT_FLAGS],
            },
        }
    )
    return context
