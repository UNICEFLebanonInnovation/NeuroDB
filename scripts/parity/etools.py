"""Step 3's group B: the eTools monitoring pages (SPEC7 §3.5, STAGE3 group B).

**Pages.** The triage of step 3 (the x20 world given a year of eTools Datamart runs, on the base commit
``0bd5a64``, cold, as an administrator and as a viewer with a section) converts:

- partner monitoring (``/partner-monitoring/``): the grid, Program Performance (``view=table``) and Data
  quality (``view=quality``), 0.55-0.8 s for every section (medians of 3; 0.3 s for the viewer's section);
- partner reporting's Report/Location (``tab=locations``, 0.37-0.43 s) and Report/Disaggregation
  (``tab=disaggregations``, 0.46-0.76 s).

It also records the pages whose code the step changes without giving them parts, so the same recording
proves them unchanged: the partner monitoring map page (its filter options, read through ``compute.once``)
and its API (``monitoring.map_points`` through ``once``), partner reporting's other two tabs (they read the
same ``datamart.partner_reporting`` and filter options), and the funds page (the quick win: the filter options
without the whole ``datamart.funds``). Action points (0.11-0.13 s) keep their code: not in this group.

**Users.** The overview group's: an administrator (the first active superuser, no section) and a viewer with a
section (the first active Viewer with one: Education in the parity world). On a first load (no query at all)
partner monitoring and its map show the viewer's own section; the administrator sees every section.

**Matrix** (computed when recording, stored with the recording): for each page, its first load, every section,
the viewer's section, another one, two, each other filter of its filter bar that changes what is computed (a
partner, a programme document, a location, a status, the report type, a tag, the words searched, the CP
output, every PD status, the PDs running in the year), another year and every year, a filter with no result,
each view or tab, its sorts, and pagination (page 2, and a page past the end, which Django answers with the
last page). See :func:`matrix`.

**Figures.** One list per page (:data:`FIGURES`): the context values the page draws, by their names in the old
view's ``render`` context, each made comparable by a projection (:func:`indicator`: an indicator as the fields
the grid, Program Performance and Data quality draw, its programme document as the fields they draw of it, so a
plain copy stored by ``compute.once`` and the old dataclass holding a model instance compare as one; rows of
dicts as they are; the pages a paginator shows, not the whole filter, whose count and order are kept as the
indicators' identities ``"<pd id>:<key>"``). A figure is recorded only for the views or tabs that draw it.

The map API's figures are its JSON, with its places and their rows put in one order first
(:func:`map_points_in_order`: the old code orders ties by the iteration of a set of strings, which changes
from one process to the next).

``record`` reads each from the old page's one response. ``check`` reads each where the page in parts keeps it:
by default the same name in the frame or in any part (every holder that has it must agree, otherwise a
"consistency" problem), unless :data:`MOVED` names another place for that page; :data:`NOT_DRAWN` lists the
values neither page draws. Both start empty: the step that converts the pages fills them, with
:data:`JSON_IDS` (the old ``json_script`` ids renamed by the rule ``<page>-<part>-data``) and, if needed, a
``comparable(observation, side)`` for a deliberate change (``core.compare`` calls it).

**The world** is ``s0/world.dump`` restored, migrated with the code under test, then given a year of eTools
Datamart runs by ``scratchpad/async/s3/b/seed_world.py`` (the same rows as step 2's ``reports_core gaps``,
without its partner links). No figure of these pages reads a sync run: the runs are there for the keys of the
stored figures (STAGE3: the eTools jobs' latest runs), whose reads ``timing.py`` must meet at production's
size. Record and check each on a fresh restore: some GET requests write (the signed-in test users' sessions).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from scripts.parity import core
from scripts.parity import overview as _overview

NO_SUCH_SECTION = "No such section"
PAGE_PAST_THE_END = "9999"  # Paginator.get_page answers the last page
MONITORING_VIEWS = ("grid", "table", "quality")  # reports.views.MONITORING_VIEWS
REPORTING_TABS = ("reports", "partners", "locations", "disaggregations")  # reports.views.REPORTING_TABS
FUNDS_TABS = ("reservations", "contributions", "programmes")  # reports.views.FUNDS_TABS
_ABSENT = object()
ABSENT = "<absent>"  # a projected field the value does not have: shows as a difference

# ------------------------------------------------------------------------------------------ projections
# what the grid, Program Performance and Data quality draw of an indicator (``monitoring.Indicator``) and of its
# programme document; read by name (an attribute or a dict key), so the old dataclass and a plain copy compare
INDICATOR_FIELDS = (
    "key",
    "url",
    "title",
    "output",
    "section",
    "cp_output",
    "unit",
    "display_type",
    "baseline",
    "target",
    "high_frequency",
    "cluster",
    "tags",
    "locations",
    "reports",
    "reached",
    "reached_in_year",
    "year",
    "awaiting",
    "awaiting_reports",
    "as_of",
    "prp_cumulative",
    "mismatch",
    "value_status",
    "partial",
    "report_type",
    "partner_status",
    "report_status",
    "tracking",
    "tracking_label",
    "achieved",
    "achieved_in_year",
    "additive",
    "method_label",
    "months",
    "years",
    "notes",
)
# what Data quality's lists draw of an indicator
QUALITY_FIELDS = (
    "key",
    "url",
    "title",
    "output",
    "section",
    "cp_output",
    "target",
    "reached",
    "reached_in_year",
    "prp_cumulative",
    "as_of",
)
PD_FIELDS = ("id", "number", "partner_id", "partner_name", "status", "start", "end", "document_type")
QUALITY_COUNTS = (
    "programme_documents",
    "active_pds",
    "running_pds",
    "ended_pds",
    "indicators",
    "with_reached",
    "without_reached",
    "zero_reached",
    "with_target",
    "without_target",
    "no_reached_count",
    "mismatch_count",
    "same_title_count",
    "partial_count",
    "read_from",
    "limit",
)
# a Report/Disaggregation block's working values (its cell, the lines' objects): the page draws its
# ``table_lines`` and ``unlabelled``, made from them
BLOCK_INTERNALS = ("_cell", "groups", "untyped")


def attr(value: Any, name: str) -> Any:
    """``value.name`` as a template reads it: a dict key, else an attribute; ``_ABSENT`` when neither."""
    if value is None or value is _ABSENT:
        return _ABSENT
    if isinstance(value, Mapping):
        return value[name] if name in value else _ABSENT
    try:
        return getattr(value, name)
    except (AttributeError, KeyError, TypeError):
        return _ABSENT


def _plain(value: Any) -> Any:
    return ABSENT if value is _ABSENT else core.plain(value)


def pd_projection(pd: Any) -> Any:
    """A programme document as the pages draw it (its partner by id and name)."""
    if pd is None or pd is _ABSENT:
        return _plain(pd)
    out = {name: _plain(attr(pd, name)) for name in PD_FIELDS}
    partner = attr(pd, "partner")
    out["partner"] = (
        _plain(partner)
        if partner is None or partner is _ABSENT
        else {"id": _plain(attr(partner, "id")), "name": _plain(attr(partner, "name"))}
    )
    return out


def ident(indicator: Any) -> str:
    """An indicator's identity on the page: its programme document's id and its key."""
    return f"{_plain(attr(attr(indicator, 'pd'), 'id'))}:{_plain(attr(indicator, 'key'))}"


def indicator(value: Any, fields: tuple[str, ...] = INDICATOR_FIELDS) -> dict[str, Any]:
    out = {"ident": ident(value)}
    out.update({name: _plain(attr(value, name)) for name in fields})
    out["pd"] = pd_projection(attr(value, "pd"))
    return out


def indicators(rows: Any) -> dict[str, Any]:
    """Every indicator of the filter the page holds (its first 2,000): how many, and which in which order."""
    rows = list(rows or [])
    return {"count": len(rows), "idents": [ident(r) for r in rows]}


def paginated(page: Any, row: Callable[[Any], Any]) -> Any:
    """A paginator's page: its number and bounds, the whole count, and the rows it shows, projected."""
    if page is None or page is _ABSENT:
        return _plain(page)
    paginator = page.paginator
    return {
        "number": page.number,
        "num_pages": paginator.num_pages,
        "count": paginator.count,
        "start_index": page.start_index(),
        "end_index": page.end_index(),
        "rows": [row(item) for item in page.object_list],
    }


def grid_groups(groups: Any) -> Any:
    """The grid's sections, programme documents and outputs (``monitoring.grouped``) of the page shown."""
    if groups is _ABSENT:
        return ABSENT
    return [
        {
            "section": _plain(attr(group, "section")),
            "count": _plain(attr(group, "count")),
            "pds": [
                {
                    "pd": pd_projection(attr(pd, "pd")),
                    "count": _plain(attr(pd, "count")),
                    "outputs": [
                        {
                            "output": _plain(attr(output, "output")),
                            "indicators": [ident(i) for i in attr(output, "indicators")],
                        }
                        for output in attr(pd, "outputs")
                    ],
                }
                for pd in attr(group, "pds")
            ],
        }
        for group in groups
    ]


def quality(value: Any) -> Any:
    """Data quality (``monitoring.data_quality``): its counts, and its lists as the page draws them."""
    if value is _ABSENT or value is None:
        return _plain(value)
    out = {name: _plain(attr(value, name)) for name in QUALITY_COUNTS}
    for name in ("missing_target", "no_reached", "mismatch"):
        out[name] = [indicator(i, QUALITY_FIELDS) for i in attr(value, name)]
    out["same_title"] = [
        {
            "pd": pd_projection(attr(group, "pd")),
            "title": _plain(attr(group, "title")),
            "count": _plain(attr(group, "count")),
            "rows": [ident(i) for i in attr(group, "rows")],
        }
        for group in attr(value, "same_title")
    ]
    out["partial"] = [pd_projection(pd) for pd in attr(value, "partial")]
    return out


def block(value: Any) -> Any:
    """A Report/Disaggregation block as the page draws it (its lines made, its working values left out)."""
    return core.plain({key: item for key, item in value.items() if key not in BLOCK_INTERNALS})


def without_pd(value: Any) -> Any:
    """A funds row (or group) without its programme document instance: the page draws the flat fields."""
    if isinstance(value, Mapping):
        return {key: without_pd(item) for key, item in value.items() if key != "pd"}
    if isinstance(value, list | tuple):
        return [without_pd(item) for item in value]
    return core.plain(value)


def model_row(value: Any) -> Any:
    return core.plain(value)


# --------------------------------------------------------------------------------------- the figures
def _always(_kind: str) -> bool:
    return True


def _only(*kinds: str) -> Callable[[str], bool]:
    return lambda kind: kind in kinds


def _same(value: Any) -> Any:
    return _plain(value)


# A figure: (its name, its path in the old ``render`` context, its projection, the views or tabs that draw it)
Figure = tuple[str, str, Callable[[Any], Any], Callable[[str], bool]]
_MONITORING_FRAME: list[Figure] = [
    (name, name, _same, _always)
    for name in (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "filters",
        "labels",
        "options",
        "selected",
        "own_section",
        "view",
        "tab_urls",
        "year_all_url",
        "year_all_active",
    )
]
FIGURES: dict[str, list[Figure]] = {
    # /partner-monitoring/ (reports.views.pd_monitoring); the kind is the view
    "monitoring": [
        *_MONITORING_FRAME,
        ("truncated", "truncated", _same, _always),
        ("data", "data", _same, _always),  # monitoring.summary: the tiles, the donut, the section bars
        ("rows", "rows", indicators, _always),
        ("page", "page_obj", lambda p: paginated(p, indicator), _only("grid", "table")),
        ("groups", "groups", grid_groups, _only("grid")),
        ("months", "months", _same, _only("grid")),
        ("year_columns", "year_columns", _same, _only("grid")),
        ("chart_data", "chart_data", _same, _only("grid")),
        ("sort", "sort", _same, _only("table")),
        ("sort_key", "sort_key", _same, _only("table")),
        ("sort_desc", "sort_desc", _same, _only("table")),
        ("table_headings", "table_headings", _same, _only("table")),
        ("sort_urls", "sort_urls", _same, _only("table")),
        ("section_tiles", "section_tiles", _same, _only("table")),
        ("all_sections_url", "all_sections_url", _same, _only("table")),
        ("downloads", "downloads", _same, _only("table")),
        ("quality", "quality", quality, _only("quality")),
    ],
    # /partner-monitoring/map/ (reports.views.pd_monitoring_map): the frame; its figures come from the API
    "map": [*_MONITORING_FRAME, ("map_config", "map_config", _same, _always)],
    # /api/internal/partner-monitoring/map/ (reports.api.PDMapAPI): monitoring.map_points as JSON
    "map-api": [],
    # /partner-reporting/ (reports.views.partner_reporting); the kind is the tab
    "reporting": [
        *[
            (name, name, _same, _always)
            for name in (
                "page_title",
                "page_subtitle",
                "breadcrumbs",
                "f",
                "tab",
                "selected",
                "year",
                "q",
                "overdue",
                "tab_urls",
                "downloads",
                "year_all_url",
                "year_all_active",
                # the JSON at the bottom of every tab of the old page (reporting-chart-data); only the reports
                # tab draws its chart, and the tabs in parts carry it no more (comparable)
                "chart_data",
            )
        ],
        ("options", "data.options", _same, _always),
        ("counts", "data.counts", _same, _only("reports")),
        ("summary", "data.summary", _same, _only("reports")),
        ("by_section", "data.by_section", _same, _only("reports")),
        ("partners", "data.partners", _same, _only("partners")),
        ("page", "page_obj", lambda p: paginated(p, core.plain), _only("reports", "partners", "locations")),
        ("page", "page_obj", lambda p: paginated(p, block), _only("disaggregations")),
        ("locations.too_many", "locations.too_many", _same, _only("locations")),
        ("locations.limit", "locations.limit", _same, _only("locations")),
        ("disaggregations.too_many", "disaggregations.too_many", _same, _only("disaggregations")),
        ("disaggregations.limit", "disaggregations.limit", _same, _only("disaggregations")),
        # the tab's own filters, which the filter bar shows
        ("disaggregations.filters", "disaggregations.filters", _same, _only("disaggregations")),
    ],
    # /funds/ (reports.views.funds); the kind is the tab
    "funds": [
        *[
            (name, name, _same, _always)
            for name in (
                "page_title",
                "page_subtitle",
                "breadcrumbs",
                "f",
                "tab",
                "selected",
                "year",
                "q",
                "tab_urls",
                "downloads",
                # the JSON at the bottom of every tab (funds-chart-data); only the reservations tab draws its charts
                "chart_data",
            )
        ],
        ("options", "data.options", _same, _always),
        ("totals", "data.totals", _same, _only("reservations")),
        ("fr_count", "data.fr_count", _same, _only("reservations")),
        ("pds", "data.pds", _same, _only("reservations")),
        ("by_grant", "data.by_grant", _same, _only("reservations")),
        ("page", "page_obj", lambda p: paginated(p, model_row), _only("reservations")),
        ("contributions", "contributions", without_pd, _only("contributions")),
        ("page", "page_obj", lambda p: paginated(p, without_pd), _only("contributions", "programmes")),
        ("programmes", "programmes", without_pd, _only("programmes")),
    ],
}

# the address of each page (the path, without the query)
PAGES: dict[str, re.Pattern[str]] = {
    "monitoring": re.compile(r"/partner-monitoring/"),
    "map": re.compile(r"/partner-monitoring/map/"),
    "map-api": re.compile(r"/api/internal/partner-monitoring/map/"),
    "reporting": re.compile(r"/partner-reporting/"),
    "funds": re.compile(r"/funds/"),
}
# Where the page in parts keeps a figure that moved, per page: ``{figure name: (its path there, (holder, ...))}``,
# "shell" naming the frame (it also holds the parts it drew inline). None moved: each figure keeps its name in the
# frame (the filter bar, the tabs) or in the part that draws it.
MOVED: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
# Figures neither page draws, per page: ``{figure name: the reason}`` (dropped from both sides).
NOT_DRAWN: dict[str, dict[str, str]] = {}
# Figures neither page draws on one view or tab, per (page, view or tab): the old view put them in every view's
# context; the page in parts computes them only where they are drawn (dropped from both sides, see comparable)
NOT_DRAWN_IN: dict[tuple[str, str], dict[str, str]] = {
    ("monitoring", "table"): {
        "data": "the grid's tiles and charts (monitoring.summary): not on Program Performance"
    },
    ("monitoring", "quality"): {
        "data": "the grid's tiles and charts (monitoring.summary): not on Data quality",
        "rows": "the grid's and Program Performance's list: Data quality draws its own lists (compared)",
    },
}
# The old pages' json_script ids renamed by the rule <page>-<part>-data (the same payload)
JSON_IDS: dict[str, str] = {"monitoring-chart-data": "pm-summary-data"}
SUMMARY_JSON = (
    "pm-summary-data"  # the grid's summary chart JSON (it carries the status labels: see comparable)
)
# The Funds tabs that read only the filter choices (the quick win): no chart of theirs read funds-chart-data
FUNDS_WITHOUT_CHARTS = ("contributions", "programmes")
# The Progress reports tabs whose table is a part: no chart of theirs read reporting-chart-data (review R1-B-2)
REPORTING_WITHOUT_CHARTS = ("locations", "disaggregations")
# The keys partner monitoring's parts write into their query when the page's own address has none (every section as
# ``section=``, the user's own section on a first load, the year shown): the links a part draws carry them
RESOLVED_KEYS = ("section", "year")
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*")
# timing.py times these rows: the converted pages' busiest filters, and the viewer's first load
TIMED = (
    "grid: first load",
    "grid: every section",
    "grid: one section",
    "grid: every year",
    "table: first",
    "table: one year",
    "quality: first",
    "locations: first",
    "disaggregations: first",
)
# The part whose arrival is a page's first figure (timing.py), per page and view or tab
FIRST_FIGURES = {
    ("monitoring", "grid"): "summary",
    ("monitoring", "table"): "sections",
    ("monitoring", "quality"): "quality-tiles",
    ("reporting", "locations"): "table",
    ("reporting", "disaggregations"): "table",
}


def page_of(url: str) -> str:
    """The name of the page ``url`` opens (a key of :data:`PAGES`)."""
    path = urlsplit(url).path
    for name, pattern in PAGES.items():
        if pattern.fullmatch(path):
            return name
    raise ValueError(f"{url} is not a page of this group")


def kind_of(url: str) -> str:
    """The view (partner monitoring) or tab (partner reporting, funds) ``url`` shows, as its view chooses it."""
    name = page_of(url)
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if name == "monitoring":
        view = (query.get("view") or [""])[-1]
        return view if view in MONITORING_VIEWS else "grid"
    if name == "reporting":
        tab = (query.get("tab") or [""])[-1]
        return tab if tab in REPORTING_TABS else "reports"
    if name == "funds":
        tab = (query.get("tab") or [""])[-1]
        return tab if tab in FUNDS_TABS else "reservations"
    return name


def first_figure(url: str) -> str | None:
    """The part whose arrival is ``url``'s first figure (timing.py); None for a page without parts."""
    return FIRST_FIGURES.get((page_of(url), kind_of(url)))


# ------------------------------------------------------------------------------------- users and matrix
def users() -> dict[str, Any]:
    return _overview.users()


def _world(people: dict[str, Any]) -> dict[str, Any]:
    """The values of the world the matrix filters on, chosen by rule (so a rebuilt world gives the same)."""
    from django.db.models import Count

    from neurodb.datamart import models as dm
    from neurodb.datamart import monitoring, reporting
    from neurodb.partnerships.models import PCA

    options = monitoring.filter_options(monitoring.Filters())
    sections = options["sections"]
    own = monitoring.default_sections(people["viewer"], sections)
    if not own:
        raise SystemExit("the viewer's section matches no eTools section: the first load would not differ")
    other = [s for s in sections if s not in own]
    if not other:
        raise SystemExit("the world needs a second eTools section")
    # the partner with the most programme documents that have indicators; its first such PD
    with_indicators = PCA.objects.filter(pd_indicators__isnull=False).distinct()
    busiest = (
        with_indicators.exclude(partner=None)
        .values("partner_id")
        .annotate(n=Count("pk", distinct=True))
        .order_by("-n", "partner_id")
        .first()
    )
    if busiest is None:
        raise SystemExit("the world has no programme document with indicators")
    partner = busiest["partner_id"]
    pd = with_indicators.filter(partner_id=partner).exclude(number="").order_by("pk").first()
    # the indicator location with the most indicator rows (its name, and the start of its P-code)
    place = (
        dm.PDIndicator.objects.exclude(location_name="")
        .values("location_name", "location_pcode")
        .annotate(n=Count("pk"))
        .order_by("-n", "location_name")
        .first()
    )
    gender = (options.get("gender") or [None])[0]
    years = options["years"]
    if not years:
        raise SystemExit("the world has no partner report with a period end")
    report_options = reporting.filter_options()
    # a word of the most common indicator title, and of the most common CP output
    title = (
        dm.PDIndicator.objects.values("title")
        .annotate(n=Count("pk"))
        .order_by("-n", "title")
        .first()["title"]
    )
    cp_output = (
        dm.PDIndicator.objects.exclude(cp_output="")
        .values("cp_output")
        .annotate(n=Count("pk"))
        .order_by("-n", "cp_output")
        .first()
    )
    labels = list(
        dm.DisaggregationLabel.objects.filter(kind="value").order_by("pk").values_list("label", flat=True)
    )
    return {
        "own": own,
        "other": other[0],
        "partner": str(partner),
        "pd": pd.number if pd else "",
        "location": place["location_name"] if place else "",
        "p_code": (place["location_pcode"] or "")[:4] if place else "",
        "gender": gender,
        "year": years[0],
        "q": _longest_word(title),
        "cp_output": _longest_word(cp_output["cp_output"]) if cp_output else "",
        "report_year": (report_options["years"] or [years[0]])[0],
        "statuses": [s["value"] for s in report_options["statuses"]],
        "disaggregation": labels[0] if labels else "",
        "indicator_word": _longest_word(title),
    }


def _longest_word(text: str) -> str:
    words = re.findall(r"[A-Za-z]{4,}", text or "")
    return max(words, key=lambda w: (len(w), w)) if words else ""


def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The pages and their filters, as (label, URL). Computed when recording and stored with it."""
    world = _world(people)
    own, other, year = world["own"], world["other"], world["year"]

    def url(path: str, *pairs: tuple[str, Any]) -> str:
        return path + ("?" + urlencode([(k, str(v)) for k, v in pairs]) if pairs else "")

    pm, pm_map, api = (
        "/partner-monitoring/",
        "/partner-monitoring/map/",
        "/api/internal/partner-monitoring/map/",
    )
    pr, funds = "/partner-reporting/", "/funds/"
    every = ("section", "")
    rows = [
        # partner monitoring, the grid (this year by default)
        ("grid: first load", pm),
        ("grid: every section", url(pm, every)),
        ("grid: one section", url(pm, *[("section", s) for s in own])),
        ("grid: another section", url(pm, ("section", other))),
        ("grid: two sections", url(pm, ("section", other), *[("section", s) for s in own])),
        ("grid: every year", url(pm, ("year", "all"))),
        ("grid: previous year", url(pm, every, ("year", year - 1))),
        ("grid: PDs running in the year", url(pm, every, ("scope", "year"), ("year", year))),
        ("grid: a partner", url(pm, ("partner", world["partner"]))),
        ("grid: a programme document", url(pm, ("pd", world["pd"]))),
        ("grid: a location", url(pm, ("location", world["location"]))),
        ("grid: off track", url(pm, every, ("status", "off_track"))),
        ("grid: not reported", url(pm, every, ("status", "not_reported"))),
        ("grid: humanitarian reports", url(pm, every, ("report_type", "HR"))),
        ("grid: not emergency", url(pm, every, ("emergency", "no"))),
        ("grid: words", url(pm, every, ("q", world["q"]))),
        ("grid: CP output", url(pm, every, ("cp_output", world["cp_output"]))),
        ("grid: every PD status", url(pm, every, ("pd_status", ""))),
        ("grid: page 2", url(pm, every, ("page", 2))),
        ("grid: past the last page", url(pm, every, ("page", PAGE_PAST_THE_END))),
        ("grid: no result", url(pm, ("section", NO_SUCH_SECTION))),
    ]
    if world["gender"]:
        rows.append(("grid: a tag", url(pm, every, ("gender", world["gender"]))))
    rows += [
        # Program Performance (every year by default)
        ("table: first", url(pm, ("view", "table"))),
        ("table: one year", url(pm, ("view", "table"), ("year", year))),
        ("table: one section", url(pm, ("view", "table"), *[("section", s) for s in own])),
        ("table: sorted by partner", url(pm, ("view", "table"), ("sort", "partner"))),
        ("table: sorted by target, lowest first", url(pm, ("view", "table"), ("sort", "target"))),
        (
            "table: one year, sorted by Reached in it",
            url(pm, ("view", "table"), ("year", year), ("sort", "reached_in_year")),
        ),
        ("table: page 2", url(pm, ("view", "table"), ("page", 2))),
        ("table: no result", url(pm, ("view", "table"), ("section", NO_SUCH_SECTION))),
        # Data quality (every year by default)
        ("quality: first", url(pm, ("view", "quality"))),
        ("quality: one year", url(pm, ("view", "quality"), ("year", year))),
        ("quality: one section", url(pm, ("view", "quality"), *[("section", s) for s in own])),
        ("quality: no result", url(pm, ("view", "quality"), ("section", NO_SUCH_SECTION))),
        # the map: its page, and its API as the page asks it (every section, or the viewer's own)
        ("map: first load", pm_map),
        ("map: every section", url(pm_map, every)),
        ("map api: every section", api),
        ("map api: one section", url(api, *[("section", s) for s in own])),
        ("map api: a location", url(api, ("location", world["location"]))),
        ("map api: every year", url(api, ("year", "all"))),
        # partner reporting: Report/Location
        ("locations: first", url(pr, ("tab", "locations"))),
        ("locations: one year", url(pr, ("tab", "locations"), ("year", world["report_year"]))),
        ("locations: one section", url(pr, ("tab", "locations"), *[("section", s) for s in own])),
        ("locations: one status", url(pr, ("tab", "locations"), ("status", "submitted"))),
        ("locations: overdue", url(pr, ("tab", "locations"), ("overdue", "1"))),
        ("locations: a partner", url(pr, ("tab", "locations"), ("partner", world["partner"]))),
        ("locations: page 2", url(pr, ("tab", "locations"), ("page", 2))),
        ("locations: past the last page", url(pr, ("tab", "locations"), ("page", PAGE_PAST_THE_END))),
        ("locations: no result", url(pr, ("tab", "locations"), ("section", NO_SUCH_SECTION))),
        # partner reporting: Report/Disaggregation
        ("disaggregations: first", url(pr, ("tab", "disaggregations"))),
        ("disaggregations: one year", url(pr, ("tab", "disaggregations"), ("year", world["report_year"]))),
        ("disaggregations: one section", url(pr, ("tab", "disaggregations"), *[("section", s) for s in own])),
        ("disaggregations: a location", url(pr, ("tab", "disaggregations"), ("location", world["location"]))),
        ("disaggregations: a P-code", url(pr, ("tab", "disaggregations"), ("p_code", world["p_code"]))),
        (
            "disaggregations: a disaggregation value",
            url(pr, ("tab", "disaggregations"), ("disaggregation", world["disaggregation"])),
        ),
        (
            "disaggregations: an indicator",
            url(pr, ("tab", "disaggregations"), ("indicator", world["indicator_word"])),
        ),
        ("disaggregations: one status", url(pr, ("tab", "disaggregations"), ("status", "accepted"))),
        ("disaggregations: page 2", url(pr, ("tab", "disaggregations"), ("page", 2))),
        ("disaggregations: no result", url(pr, ("tab", "disaggregations"), ("section", NO_SUCH_SECTION))),
        # partner reporting's other tabs (the same build and options; no parts)
        ("reports: first", pr),
        ("reports: one year, one status", url(pr, ("year", world["report_year"]), ("status", "submitted"))),
        ("reports: page 2", url(pr, ("page", 2))),
        ("partners: first", url(pr, ("tab", "partners"))),
        # funds (the quick win: the options; no parts)
        ("funds: reservations", funds),
        ("funds: contributions", url(funds, ("tab", "contributions"))),
        ("funds: contributions, page 2", url(funds, ("tab", "contributions"), ("page", 2))),
        (
            "funds: contributions, a partner",
            url(funds, ("tab", "contributions"), ("partner", world["partner"])),
        ),
        ("funds: programmes", url(funds, ("tab", "programmes"))),
    ]
    return rows


# --------------------------------------------------------------------------------------------- the figures
def _read(context: Any, path: str) -> Any:
    """The value at ``path`` (dotted: a context key, then keys or attributes inside it) in a template context,
    or ``_ABSENT``."""
    if context is None:
        return _ABSENT
    first, *rest = path.split(".")
    try:
        if first not in context:
            return _ABSENT
        value = context[first]
    except (KeyError, TypeError):
        return _ABSENT
    for key in rest:
        value = attr(value, key)
        if value is _ABSENT:
            return _ABSENT
    return value


def _api_json(response: Any) -> Any:
    """The map API's answer, parsed (its figures are the JSON itself), its places in a fixed order
    (:func:`map_points_in_order`)."""
    if getattr(response, "status_code", 0) != 200:
        return None
    try:
        found = json.loads(response.content.decode(response.charset or "utf-8"))
    except ValueError:
        return {"__unparsed__": response.content[:200].decode("utf-8", "replace")}
    return map_points_in_order(found)


def map_points_in_order(found: Any) -> Any:
    """``monitoring.map_points`` with its places (``points``, ``unlocated``) by key and each place's rows in
    one order. The old code sorts places by their number of indicators and rows by partner, PD and indicator;
    places with the same number of indicators come in the order a set of location keys gave them, which
    changes from one process to the next (string hashing), and so does the order of their rows. Two runs of
    the old code on one world differed in 20,776 values for that alone; neither order is a figure."""
    if not isinstance(found, dict):
        return found
    out = dict(found)
    for name in ("points", "unlocated"):
        places = out.get(name)
        if not isinstance(places, list):
            continue
        ordered = []
        for place in places:
            if isinstance(place, dict) and isinstance(place.get("rows"), list):
                rows = sorted(place["rows"], key=lambda row: json.dumps(row, sort_keys=True, default=str))
                place = {**place, "rows": rows}
            ordered.append(place)
        out[name] = sorted(ordered, key=lambda place: json.dumps(place.get("key"), default=str))
    return out


# a part's placeholder and its grey shapes: the page in parts holds the part in their place, so their contexts (name,
# kind, rows, url...) are not the page's figures (tests/web/parts.py leaves them out of an assembled page alike)
PLACEHOLDER_TEMPLATES = frozenset({"components/part.html", "components/part_skeleton.html"})


def _drawn_context(response: Any) -> Any:
    """The template contexts of ``response`` without its parts' placeholders'."""
    from django.test.utils import ContextList

    context = getattr(response, "context", None)
    if context is None:
        return None
    contexts = list(context) if isinstance(context, ContextList) else [context]
    templates = list(getattr(response, "templates", []))
    if len(templates) != len(contexts):
        return context
    kept = ContextList(
        c for c, t in zip(contexts, templates, strict=True) if t.name not in PLACEHOLDER_TEMPLATES
    )
    return kept


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The page's figures under the old page's names, and the problems found on the way (a listed path the
    old page does not have; parts that should agree and do not)."""
    name, kind = page_of(page.url), kind_of(page.url)
    problems: list[str] = []
    out: dict[str, Any] = {}
    if name == "map-api":
        out["map_points"] = _api_json(page.shell)
        return out, problems
    shell = getattr(page.shell, "context", None)
    drawn = [figure for figure in FIGURES[name] if figure[3](kind)]
    if mode == "record":
        for label, path, project, _when in drawn:
            value = _read(shell, path)
            if value is _ABSENT and page.shell.status_code == 200:
                problems.append(f"{label}: not in the old page's context ({path})")
            out[label] = project(value) if value is not _ABSENT else None
        return out, problems

    contexts = {"shell": _drawn_context(page.shell)} | {
        part: getattr(response, "context", None)
        for part, response in page.parts.items()
        if response.status_code == 200
    }
    skipped = {**NOT_DRAWN.get(name, {}), **NOT_DRAWN_IN.get((name, kind), {})}
    if name == "funds" and kind in FUNDS_WITHOUT_CHARTS:
        skipped["chart_data"] = "the quick win (comparable)"
    if name == "reporting" and kind in REPORTING_WITHOUT_CHARTS:
        skipped["chart_data"] = "no chart of the tab reads it (comparable)"
    for label, path, project, _when in drawn:
        if label in skipped:
            continue
        where, holders = MOVED.get(name, {}).get(label, (path, tuple(contexts)))
        found = [
            (holder, project(value))
            for holder in holders
            if (value := _read(contexts.get(holder), where)) is not _ABSENT
        ]
        if not found:
            out[label] = None
            if page.shell.status_code == 200:
                problems.append(f"{label}: in no part and not in the shell ({where})")
            continue
        first = found[0][1]
        for holder, value in found[1:]:
            if value != first:
                problems.append(f"{label}: {found[0][0]} and {holder} disagree")
        out[label] = first
    return out, problems


# ------------------------------------------------------------------------------------------ deliberate changes
def comparable(observation: dict[str, Any], side: str) -> dict[str, Any]:
    """One observation as it is compared (``side`` is "record" or "check"), with the deliberate changes of the
    pages in parts taken out:

    - the figures neither page draws (:data:`NOT_DRAWN`, :data:`NOT_DRAWN_IN`), on both sides;
    - partner monitoring's links (in the blocks of the page and in the figures that hold addresses: the sorts, the
      section tiles, the downloads): a part's query writes out the filter the frame resolved (every section as
      ``section=``, the user's own section on a first load, the year shown), so the links a part draws carry
      those keys where the page's own address had none (the old first load's page links even lost the viewer's
      own section). On both sides, a link's ``section`` and ``year`` are left out when the page's own address has
      no such key, and its keys are put in one order; every other key, and every key of an address that has them,
      is compared;
    - the Funds page's Partner Fund Monitoring and Funded Programs tabs carry no chart data any more (the quick
      win: no chart of theirs read it): on the check side it must be absent (otherwise a "consistency" problem),
      and it is dropped from the recording;
    - nor do the Progress reports page's Report/Location and Report/Disaggregation tabs (review R1-B-2: the old
      page carried ``reporting-chart-data`` on every tab, which only the Reporting progress tab's chart reads):
      the same check and the same drop;
    - partner monitoring's summary chart JSON (``pm-summary-data``, the grid's ``chart_data``) carries the status
      labels now, so the donut names its slices by them (SPEC7_review item 16, review R1-B-3): on the check side
      they must be ``monitoring.LABELS`` (otherwise a problem), and they are taken out before the comparison.
    """
    name, kind = page_of(observation["url"]), kind_of(observation["url"])
    out = {**observation, "problems": list(observation["problems"])}
    drop = {**NOT_DRAWN.get(name, {}), **NOT_DRAWN_IN.get((name, kind), {})}
    out["figures"] = {label: value for label, value in observation["figures"].items() if label not in drop}
    if name == "monitoring" and observation["status"] == 200:
        own = parse_qs(urlsplit(observation["url"]).query, keep_blank_values=True)
        out["figures"] = _resolved_links(out["figures"], own)
        out["blocks"] = {
            heading: {**block, "links": [_without_resolved(link, own) for link in block["links"]]}
            for heading, block in observation["blocks"].items()
        }
    if name == "monitoring" and side == "check":
        _without_summary_labels(out)
    if name == "funds" and kind in FUNDS_WITHOUT_CHARTS:
        if side == "check" and (
            "funds-chart-data" in observation["json"] or "chart_data" in observation["figures"]
        ):
            out["problems"].append("funds-chart-data: still on a tab whose charts never read it")
        out["figures"].pop("chart_data", None)
        out["json"] = {key: value for key, value in observation["json"].items() if key != "funds-chart-data"}
        out["ids"] = [element_id for element_id in observation["ids"] if element_id != "funds-chart-data"]
    if name == "reporting" and kind in REPORTING_WITHOUT_CHARTS:
        if side == "check" and (
            "reporting-chart-data" in observation["json"] or "chart_data" in observation["figures"]
        ):
            out["problems"].append("reporting-chart-data: still on a tab whose table no chart reads")
        out["figures"].pop("chart_data", None)
        dropped = "reporting-chart-data"
        out["json"] = {key: value for key, value in observation["json"].items() if key != dropped}
        out["ids"] = [element_id for element_id in observation["ids"] if element_id != dropped]
    return out


def _without_summary_labels(out: dict[str, Any]) -> None:
    """Take the status labels out of partner monitoring's summary chart JSON and its ``chart_data`` (one
    observation of the page in parts, changed in place), after checking they are ``monitoring.LABELS``: the one
    deliberate addition to that JSON (see :func:`comparable`)."""
    from neurodb.datamart.monitoring import LABELS

    labels = json.loads(json.dumps(LABELS))  # as the page's JSON reads them
    holders = (("json", SUMMARY_JSON), ("figures", "chart_data"))
    for where, key in holders:
        payload = out[where].get(key)
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        if payload.pop("labels", None) != labels:
            out["problems"].append(f"{key}: its status labels are missing or not monitoring.LABELS")
        out[where] = {**out[where], key: payload}


def _without_resolved(link: str, own: dict[str, list[str]]) -> str:
    """A link of the page (a query, ``?...``) without the keys the page in parts writes out where the page's own
    address has none (:data:`RESOLVED_KEYS`), its keys in one order. Other links are left as they are."""
    if not isinstance(link, str) or not link.startswith("?"):
        return link
    pairs = parse_qsl(link[1:], keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key in own or key not in RESOLVED_KEYS]
    return "?" + urlencode(sorted(kept))


def _resolved_links(figures: dict[str, Any], own: dict[str, list[str]]) -> dict[str, Any]:
    """The figures that hold the links a part draws, through :func:`_without_resolved`."""
    out = dict(figures)
    if isinstance(out.get("sort_urls"), dict):
        out["sort_urls"] = {key: _without_resolved(url, own) for key, url in out["sort_urls"].items()}
    if isinstance(out.get("all_sections_url"), str):
        out["all_sections_url"] = _without_resolved(out["all_sections_url"], own)
    for label in ("section_tiles", "downloads"):
        if isinstance(out.get(label), list):
            out[label] = [
                {**item, "url": _without_resolved(item.get("url"), own)} if isinstance(item, dict) else item
                for item in out[label]
            ]
    return out
