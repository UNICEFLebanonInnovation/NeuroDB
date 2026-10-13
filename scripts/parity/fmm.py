"""Stage C's group: Monitoring insights (``/fmm/``), every tab (SPEC7 §3.5, STAGE3 group C).

**World.** The x20 world (``s0/world.dump``) holds the demo's 75 field monitoring visits only. Group C's
world adds Monitoring insights at production size (5,000 visits of 3 records, 100,000 checklist answers,
2,500 action points, after one full refresh 5,075 visits and 18,696 records) with the generator of
``tests/fmm/test_performance.py`` adapted to the world's gazetteer, sections and offices, the clock frozen
while it runs: ``scratchpad/async/s3/c/seed_fmm.py``, run by ``build_world.sh <db>`` (restore, migrate,
seed, analyse). Two builds print the same fingerprint of every Monitoring insights table. Rebuild it
before ``record`` and before ``check`` alike (some GETs write: the sessions, the AI card's expiry pass).

**Triage** (step 3, base ``0bd5a64``, that world, production caches, the full page cold, the median of
15 runs, administrator / viewer): only the **Map** tab is over 300 ms (every section 347 / 362 ms, first
load 327 / 273, every active programme document 446 / 358); Insights (141-152 / 138-150), Quality
(227-244 / 150-235), Analysis (265-269 / 235-270; one round of three had the viewer at 312) and Records
(193-199 / 190-195) stay one request. Every tab is recorded all the same: the tabs share the view, the
frame and ``fmm/_results.html``, which the conversion of the Map tab changes.

**Commands** (step 2's settings: ``DJANGO_ENV=test``, ``config.settings``; the clock frozen at
``core.FROZEN_AT``), from this worktree's root, each on a fresh build of the world::

    build_world.sh neurodb_parity_ps_c_<name> [migrate-checkout]
    python -m scripts.parity.core record fmm ps_c_base.json --checkout /home/user/nd-ps-base
    python -m scripts.parity.core check fmm ps_c_base.json [--report diff.json]

The recording checked against the base itself (``--checkout /home/user/nd-ps-base``): 64 filters x 2 users,
0 differences, cold and warm.

**Users.** The overview group's: an administrator (the first active superuser) and a viewer with a
section (the first active viewer with one: the demo viewer, Education), whose first load applies its
own section.

**Rows** (computed when recording, stored with the recording; see :func:`matrix`):

- a page: the address a person opens (``record``: the old page in one response; ``check``: the frame and
  every part);
- ``hx:<target>:<address>``: an HTMX request as the tab bar and the filter bar make it (target
  ``fmm-results``), or as the records list's pages and sorts make it (``fmm:visits``, target
  ``fmm-visits``). Its answer is read as the page's ``<main>``; placeholders inside are filled as on a page.

**What a person sees once everything has arrived.** ``core.get_page`` fills the placeholders and the
elements that load themselves (the AI card, ``hx-trigger="load"``). This group also reveals the panels a
person reaches by scrolling (``hx-trigger="revealed"``: the Analysis tab's rule score trends and entity
table, each an HTMX request whose answer the panel selects with ``hx-select``), on both sides
(:func:`get_page`, which ``core`` calls once this module is loaded): the page in parts makes them strict
parts, which ``check`` asks for like any part.

**Context paths.** The old view's render context, by name (:func:`paths`): the frame's keys (full pages),
the results' keys (every answer of ``/fmm/``) and the tab's own keys; ``page_obj`` is read as its numbers
(:func:`_page_numbers`), ``map`` without its ``config`` (the payload ``fmm-map-config`` carries it, and
``map_config`` is that payload: not recorded twice). Besides the answer's own context:

- ``ai_card.<key>``: the AI card's context (``fmm:insights``, the ``hx-trigger="load"`` answer of the
  Insights tab), every key but the two prompt versions (model rows of several KB each);
- ``revealed.<target>.<key>``: the context of a revealed panel's answer (``rule_trends`` of the rule score
  trends; the entity table's keys of the entity table).

``check`` reads each path where the page in parts keeps it: by default the same name in the frame or any
part (every holder that has it must agree, otherwise a "consistency" problem), ``ai_card.*`` in the AI
card's answer (a part or a load), ``revealed.*`` in the revealed answer, unless :data:`MOVED` names
another place (the conversion fills it, with :data:`JSON_IDS`, as reports_core.py did).

**The conversion** (step 3): the Map tab answers its frame and one part, ``map``; its HTMX swaps answer the
map drawn in; the Analysis and Quality tabs' revealed panels and "Show all" lists are answered alone. No path
moved and no JSON id changed (:data:`MOVED`, :data:`JSON_IDS`). One change is deliberate (:func:`comparable`):
a Map tab whose filter has no visit (both pages draw the tab's empty state) no longer computes the map's
figures it never drew.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.parity import core
from scripts.parity import overview as _overview

NO_SUCH_SECTION = "No such section"
NO_SUCH_VISIT = "no-such-visit"
TABS = ("insights", "quality", "analysis", "visits", "map")
HX_PREFIX = "hx:"
RESULTS_TARGET = "fmm-results"
TABLE_TARGET = "fmm-visits"
CARD_PATH = "/fmm/insights/"

# --------------------------------------------------------------------------------------- the context paths
# the full page's own keys (``fmm.views.dashboard``, not in an HTMX answer)
FRAME_PATHS = (
    "page_title",
    "page_subtitle",
    "breadcrumbs",
    "actions",
    "options",
    "presets",
    "kind_labels",
    "rating_labels",
    "status_labels",
    "quality_labels",
    "urgency_labels",
)
# every answer of /fmm/ (``fmm.views._results_context``)
RESULTS_PATHS = (
    "scope",
    "tab",
    "tabs",
    "has_visits",
    "reference",
    "how",
    "glossary",
    "kpis",
    "kpi_tiles",
    "notes",
    "chips",
    "visits_query",
    "this_year_query",
    "limits",
)
# the records list (``fmm.views._table``): the Records tab, and fmm:visits
TABLE_PATHS = (
    "page_obj",
    "visits",
    "records",
    "view",
    "sort",
    "size_query",
    "page_size",
    "page_sizes",
    "limits",
    "sort_columns",
    "team_after",
    "sort_next",
    "this_year_query",
    "glossary",
    "view_queries",
    "downloads",
)
TAB_PATHS: dict[str, tuple[str, ...]] = {
    "insights": ("briefing", "critical", "chat"),
    "quality": (
        "chart_data",
        "month_template",
        "q1_key",
        "q1_template",
        "q1_question",
        "q1_totals",
        "issues",
        "places",
        "issues_summary",
        "fields_found_url",
    ),
    "analysis": (
        "chart_data",
        "bucket_template",
        "not_scored",
        "not_scored_url",
        "rule_trends",
        "rule_trends_query",
        "rule_trend_template",
        "rule_rows",
        "sections_all_query",
        "rules_all",
        "rules_all_query",
        "rule_checked",
        "fields_found_url",
        "highlights",
        "gaps",
        "offices",
        "entity_kind",
        "entity_kinds",
        "entity_rows",
        "entity_total",
        "entity_all_query",
        "entity_lazy_query",
        "entity_year",
        "office_badges",
        "section_rows",
        "places",
        "rating_rows",
        "flag_frequency",
        "flag_frequency_rows",
        "flag_count_rows",
        "flags_scored",
        "dimensions",
        "hact",
        "follow_up",
        "assurance_url",
        "action_points_url",
    ),
    "visits": TABLE_PATHS,
    "map": ("map", "map_focus", "pd_scope", "pd_scope_query"),
}
# the AI card (``fmm.views._insight_context``), without ``version`` and ``shown_version`` (the published
# prompt version's row: what it shows of it, the card's own keys hold)
CARD_PATHS = (
    "scope",
    "brief",
    "row",
    "sections",
    "actions",
    "actions_title",
    "rules_version",
    "chips",
    "quota",
    "ai_on",
    "disabled",
    "message",
    "is_admin",
    "generation",
    "fallback_note",
)
# the panels the Analysis tab reveals (their target id): the keys their answer draws
REVEALED_PATHS: dict[str, tuple[str, ...]] = {
    "fmm-rule-trends-panel": ("rule_trends", "rule_trend_template"),
    "fmm-entities": (
        "entity_kind",
        "entity_kinds",
        "entity_rows",
        "entity_total",
        "entity_all_query",
        "entity_lazy_query",
        "entity_year",
    ),
}
# Where the page in parts keeps a path that moved: ``{path: (its name there, (holder, ...))}``, a holder being
# "shell", a part's name, "ai_card" or "revealed:<target>". The conversion (step 3) moved none: the Map tab's
# part "map" holds the old tab's names (``map``, ``map_focus``, ``pd_scope``, ``pd_scope_query``; the frame
# keeps ``pd_scope`` too, for the filter bar, and both must agree), read from the frame or any part as every
# path; an HTMX swap of the results draws the map in its answer; and the panels a tab reveals are answered alone
# with the names the whole tab gave them (``revealed.<target>.*``, from the revealed answer as before).
MOVED: dict[str, tuple[str, tuple[str, ...]]] = {}
# The old page's json_script ids renamed by the conversion (``<page>-<part>-data``): none, the map's
# configuration keeps ``fmm-map-config`` (edumap.js reads it by that name; it is in the map part only).
JSON_IDS: dict[str, str] = {}
# Element ids only the page in parts has: the parts' own wrappers
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*")
# timing.py: the rows it times (the converted tab's pages) and the part whose arrival is a page's first
# figure: none, the key figures are in the Map tab's frame (its first figure is the frame); the map part is its
# last part
TIMED = ("map: first load", "map: every section", "map: every active programme document")
FIRST_FIGURE = None


def first_figure(url: str) -> str | None:
    return FIRST_FIGURE


# The Map tab's figures only its part draws (the frame keeps ``pd_scope``, for the filter bar)
MAP_PART_PATHS = ("map", "map_focus", "pd_scope_query")


def comparable(observation: dict[str, Any], side: str) -> dict[str, Any]:
    """One observation as it is compared (``side`` is "record" or "check"), with the one deliberate change of
    the conversion taken out: a Map tab whose filter has no visit draws the tab's empty state, not the map, on
    both pages; the old view computed the map's figures all the same (never drawn), the page in parts asks for
    no part there. For such a row the map's figures (:data:`MAP_PART_PATHS`) are left out on both sides, and
    on the check side the page must have asked for no part (otherwise a "consistency" problem)."""
    figures = observation["figures"]
    kpis = figures.get("kpis") if isinstance(figures.get("kpis"), dict) else {}
    if tab_of(observation["url"]) != "map" or observation["status"] != 200 or kpis.get("visits"):
        return observation
    out = {**observation, "figures": {k: v for k, v in figures.items() if k not in MAP_PART_PATHS}}
    out["problems"] = [p for p in observation["problems"] if p.split(":", 1)[0] not in MAP_PART_PATHS]
    if side == "check" and observation["parts"]:
        out["problems"].append(f"a Map tab without visits asked for parts: {sorted(observation['parts'])}")
    return out


# ------------------------------------------------------------------------------------------------ rows
def hx_row(target: str, address: str) -> str:
    return f"{HX_PREFIX}{target}:{address}"


def split_row(url: str) -> tuple[str | None, str]:
    """``(target, address)`` of a row: the HTMX target of an ``hx:`` row (None for a page)."""
    if url.startswith(HX_PREFIX):
        target, address = url[len(HX_PREFIX) :].split(":", 1)
        return target, address
    return None, url


def tab_of(url: str) -> str:
    """The tab a row shows (``fmm.views._tab``: Insights unless a known tab is asked for)."""
    _target, address = split_row(url)
    wanted = (parse_qs(urlsplit(address).query).get("tab") or [""])[-1]
    return wanted if wanted in TABS else "insights"


def paths(url: str) -> tuple[str, ...]:
    """The context paths of a row's answer (its own context; the AI card's and the revealed panels' are
    read apart)."""
    target, address = split_row(url)
    if urlsplit(address).path == "/fmm/visits/":
        return ("scope", *TABLE_PATHS)
    frame = () if target else FRAME_PATHS
    return frame + RESULTS_PATHS + tuple(p for p in TAB_PATHS[tab_of(url)] if p not in RESULTS_PATHS)


# ------------------------------------------------------------------------------------- users and matrix
def users() -> dict[str, Any]:
    return _overview.users()


def _choices(year: int) -> dict[str, Any]:
    """The values the matrix names, from the visits of ``year`` (every section), each the busiest of its
    kind (ties: the first by name or id): a governorate, a partner, a programme document, a field office,
    a section other than the viewer's, the most urgent visit on the map and a visit without coordinates."""
    from neurodb.fmm.models import Visit

    visits = Visit.objects.filter(visit_date__year=year)

    def busiest(values) -> Any:
        counted = Counter(v for v in values if v not in (None, ""))
        if not counted:
            raise SystemExit("the world's visits of this year have no value to filter on")
        return min(counted.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]

    offices = [o for row in visits.values_list("offices", flat=True) for o in row or ()]
    sections = [s for row in visits.values_list("section_names", flat=True) for s in row or ()]
    located = visits.exclude(latitude=None).exclude(longitude=None).exclude(urgency=None)
    focus = located.order_by("-urgency", "key").values_list("key", flat=True).first()
    unlocated = visits.filter(latitude=None).order_by("key").values_list("key", flat=True).first()
    return {
        "governorate": busiest(visits.values_list("governorate_name", flat=True)),
        "partner": busiest(visits.values_list("partner_id", flat=True)),
        "pd": busiest(visits.values_list("pd_id", flat=True)),
        "office": busiest(offices),
        "sections": Counter(sections),
        "focus": focus,
        "unlocated": unlocated,
    }


def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The rows, as (label, URL). Each tab: its first load (the viewer's own section), every section, the
    viewer's section, filters (another section, two sections, a governorate, a partner, a programme
    document, a field office, a rating, a period), the previous year (no visit in this world), a filter with
    no result, and what the tab asks for itself or on a click (the entity chips, every entity, the rule
    trends, the "Show all" lists; pages, page size, sorts and the grouping by visit; every active programme
    document, a visit centred, an unknown one); then the HTMX swaps of the tab bar and the filter bar."""
    from django.utils import timezone

    from neurodb.fmm import metrics
    from neurodb.fmm.scope import options

    year = timezone.localdate().year
    when = metrics.stamp()
    sections = options(when)["sections"]
    own = people["viewer"].section.name
    if own not in sections:
        raise SystemExit(f"the viewer's section {own!r} has no visits: the first load would not apply it")
    found = _choices(year)
    other = min((s for s in sections if s != own), key=lambda s: (-found["sections"][s], s))

    def page(*pairs: tuple[str, str]) -> str:
        return "/fmm/" + ("?" + urlencode(pairs) if pairs else "")

    every = ("section", "")
    rows: list[tuple[str, str]] = []
    for tab in TABS:
        name = "records" if tab == "visits" else tab
        tab_pair = () if tab == "insights" else (("tab", tab),)
        rows += [
            (f"{name}: first load", page(*tab_pair)),
            (f"{name}: every section", page(every, *tab_pair)),
            (f"{name}: one section", page(("section", own), *tab_pair)),
            (f"{name}: previous year", page(("year", str(year - 1)), *tab_pair)),
            (f"{name}: no result", page(("section", NO_SUCH_SECTION), *tab_pair)),
        ]
        if tab == "insights":
            rows += [
                ("insights: another section", page(("section", other))),
                ("insights: two sections", page(("section", other), ("section", own))),
                ("insights: one governorate", page(every, ("governorate", found["governorate"]))),
                ("insights: one partner", page(every, ("partner", str(found["partner"])))),
                ("insights: a programme document", page(every, ("pd", str(found["pd"])))),
                ("insights: one field office", page(every, ("office", found["office"]))),
                ("insights: off-track records", page(every, ("rating", "off_track"))),
                ("insights: last 90 days", page(("period", "last_90"), every)),
            ]
        elif tab == "quality":
            rows += [
                ("quality: one governorate", page(every, ("governorate", found["governorate"]), *tab_pair)),
                ("quality: every place", page(every, *tab_pair, ("places", "all"))),
            ]
        elif tab == "analysis":
            rows += [
                ("analysis: one governorate", page(every, ("governorate", found["governorate"]), *tab_pair)),
                ("analysis: PD/SSFA entities", page(every, *tab_pair, ("entity_kind", "pd"))),
                ("analysis: partner entities", page(every, *tab_pair, ("entity_kind", "partner"))),
                (
                    "analysis: every entity",
                    page(every, *tab_pair, ("entity_kind", "all"), ("entity_all", "1")),
                ),
                ("analysis: rule trends asked", page(every, *tab_pair, ("rule_trends", "1"))),
                ("analysis: every section line", page(every, *tab_pair, ("sections", "all"))),
                ("analysis: every rule", page(every, *tab_pair, ("rules", "all"))),
                ("analysis: every place", page(every, *tab_pair, ("places", "all"))),
            ]
        elif tab == "visits":
            rows += [
                ("records: page 2", page(every, *tab_pair, ("page", "2"))),
                ("records: 100 a page, page 3", page(every, *tab_pair, ("page_size", "100"), ("page", "3"))),
                ("records: lowest quality first", page(every, *tab_pair, ("sort", "quality"))),
                ("records: oldest first", page(every, *tab_pair, ("sort", "date"))),
                ("records: by partner", page(every, *tab_pair, ("sort", "partner"))),
                ("records: grouped by visit", page(every, *tab_pair, ("view", "visits"))),
                (
                    "records: grouped by visit, page 2",
                    page(every, *tab_pair, ("view", "visits"), ("page", "2")),
                ),
                (
                    "records: grouped by visit, least urgent first",
                    page(every, *tab_pair, ("view", "visits"), ("sort", "urgency")),
                ),
            ]
        else:
            rows += [
                ("map: one governorate", page(every, ("governorate", found["governorate"]), *tab_pair)),
                ("map: every active programme document", page(every, *tab_pair, ("pd_scope", "active"))),
                ("map: a visit centred", page(every, *tab_pair, ("visit", found["focus"]))),
                ("map: an unknown visit", page(every, *tab_pair, ("visit", NO_SUCH_VISIT))),
            ]
            if found["unlocated"]:
                rows.append(
                    (
                        "map: a visit without coordinates",
                        page(every, *tab_pair, ("visit", found["unlocated"])),
                    )
                )
    for tab in TABS:
        name = "records" if tab == "visits" else tab
        rows.append((f"HX {name}: every section", hx_row(RESULTS_TARGET, page(every, ("tab", tab)))))
    rows += [
        ("HX insights: first load (own section)", hx_row(RESULTS_TARGET, page(("tab", "insights")))),
        ("HX map: one section", hx_row(RESULTS_TARGET, page(("section", own), ("tab", "map")))),
        (
            "HX map: every active programme document",
            hx_row(RESULTS_TARGET, page(every, ("tab", "map"), ("pd_scope", "active"))),
        ),
        (
            "HX records list: page 2",
            hx_row(TABLE_TARGET, "/fmm/visits/?" + urlencode([every, ("page", "2")])),
        ),
    ]
    return rows


# --------------------------------------------------------------------------------- asking for a row's page
_core_get_page = core.get_page
REVEALED = re.compile(r'<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*?\shx-trigger="revealed"[^>]*>')


class _InMain:
    """An HTMX answer read as a page: its body inside ``<main>``, where the swap puts it."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.content = b"<main>" + response.content + b"</main>"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class _HxFirst:
    """The signed-in client, whose first GET is a row's HTMX request (its target); the rest as asked."""

    def __init__(self, client: Any, target: str) -> None:
        self._client, self._target, self._first = client, target, True

    def get(self, path: str, *args: Any, **extra: Any) -> Any:
        if not self._first:
            return self._client.get(path, *args, **extra)
        self._first = False
        headers = {
            "HTTP_HX_REQUEST": "true",
            "HTTP_HX_TARGET": self._target,
            "HTTP_HX_CURRENT_URL": core.SERVER + "/fmm/",
        }
        return _InMain(self._client.get(path, *args, **headers, **extra))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _Missing:
    """What a revealed panel gets when its answer holds no element to put in place (status 0)."""

    status_code = 0
    content = b""
    charset = "utf-8"
    context = None


def _element(html: str, element_id: str) -> tuple[int, int, str] | None:
    """Where the element of id ``element_id`` starts and ends in ``html``, and its tag."""
    match = re.search(rf'<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*?\sid="{re.escape(element_id)}"[^>]*>', html)
    if match is None:
        return None
    return match.start(), core.element_end(html, match.start(), match["tag"]), match["tag"]


def _reveal(client: Any, page: core.Page, address: str) -> None:
    """Each element of ``<main>`` that loads when scrolled into view (``hx-trigger="revealed"``) asked for
    as htmx asks (its target in ``HX-Target``), and the element its ``hx-select`` names put in place of its
    target (``hx-swap``, outerHTML or innerHTML). The answers go to ``page.revealed`` by target."""
    page.revealed = {}
    done: set[str] = set()
    while True:
        main = core.MAIN.search(page.html)
        if main is None:
            return
        match = next((m for m in REVEALED.finditer(main["body"]) if m.group(0) not in done), None)
        if match is None:
            return
        done.add(match.group(0))
        found = core.attributes(match.group(0))
        target = found.get("hx-target", "").removeprefix("#")
        chosen = found.get("hx-select", "").removeprefix("#") or target
        response, pending = core.hx_get(client, found.get("hx-get", ""), target, address)
        page.pending += pending
        page.loads.append(response)
        page.revealed[target] = response
        if response.status_code != 200:
            continue
        body = response.content.decode(response.charset or "utf-8")
        picked, where = _element(body, chosen), _element(page.html, target)
        if picked is None or where is None:
            page.loads.append(_Missing())
            continue
        start, end, tag = where
        new = body[picked[0] : picked[1]]
        if found.get("hx-swap", "innerHTML").startswith("outerHTML"):
            page.html = page.html[:start] + new + page.html[end:]
        else:
            opening = page.html[start : page.html.index(">", start) + 1]
            page.html = page.html[:start] + opening + new + f"</{tag}>" + page.html[end:]


def get_page(client: Any, url: str) -> core.Page:
    """A row's page as a person sees it once everything has arrived: ``core.get_page`` (for an ``hx:`` row,
    with its HTMX request first), then the panels revealed by scrolling (:func:`_reveal`)."""
    target, address = split_row(url)
    page = _core_get_page(_HxFirst(client, target) if target else client, address)
    page.url = url
    if page.shell.status_code == 200:
        _reveal(client, page, address)
    else:
        page.revealed = {}
    return page


# the group's pages are asked for through get_page (core's _pages calls core.get_page)
core.get_page = get_page


# --------------------------------------------------------------------------------------------- the figures
_ABSENT = object()


def _read(context: Any, path: str) -> Any:
    """The value at ``path`` (dotted: a key, then keys inside it) in a template context, or ``_ABSENT``."""
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
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return _ABSENT
    return value


def _page_numbers(page_obj: Any) -> Any:
    """A records page as its numbers (its rows are the ``visits`` or ``records`` path): the Paginator holds
    the whole filter's queryset, which is not a figure."""
    if page_obj is None:
        return None
    paginator = page_obj.paginator
    return {
        "number": page_obj.number,
        "count": paginator.count,
        "num_pages": paginator.num_pages,
        "per_page": paginator.per_page,
        "start_index": page_obj.start_index(),
        "end_index": page_obj.end_index(),
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
        "page_range": list(paginator.get_elided_page_range(page_obj.number)),
    }


def _without_config(data: Any) -> Any:
    """The Map tab's data without its ``config`` (the payload ``fmm-map-config`` carries it)."""
    return {k: v for k, v in data.items() if k != "config"} if isinstance(data, dict) else data


READERS = {"page_obj": _page_numbers, "map": _without_config}


def _plain(path: str, value: Any) -> Any:
    reader = READERS.get(path.rsplit(".", 1)[-1])
    return core.plain(reader(value) if reader else value)


def _card(page: core.Page) -> Any:
    """The AI card's answer (a part or an element that loads itself), or None."""
    for response in [*page.parts.values(), *page.loads]:
        request = getattr(response, "wsgi_request", None)
        if request is not None and request.path == CARD_PATH and response.status_code == 200:
            return response
    return None


def holders(page: core.Page) -> dict[str, Any]:
    """Every context the page's figures may be read from: the answer's own ("shell"), each part's, the AI
    card's ("ai_card") and each revealed panel's ("revealed:<target>")."""
    out: dict[str, Any] = {"shell": getattr(page.shell, "context", None)}
    for name, response in page.parts.items():
        if response.status_code == 200:
            out[name] = getattr(response, "context", None)
    card = _card(page)
    if card is not None:
        out["ai_card"] = card.context
    for target, response in getattr(page, "revealed", {}).items():
        if response.status_code == 200:
            out[f"revealed:{target}"] = getattr(response, "context", None)
    return out


def _wanted(page: core.Page, mode: str) -> list[tuple[str, str, tuple[str, ...], bool]]:
    """``(path, its name in its holder, the holders it is read from, required)`` of a row. Its own paths
    (from the answer on the old page; on the page in parts from the frame or any part: no holder named),
    the AI card's on the Insights tab, and the revealed panels': those the old page revealed (``record``),
    or, on the page in parts, wherever :data:`MOVED` (else the revealed answer) has them, when it does
    (a row whose tab is empty reveals nothing on either side)."""
    own = ("shell",) if mode == "record" else ()
    out = [(path, path, own, True) for path in paths(page.url)]
    if tab_of(page.url) == "insights" and urlsplit(split_row(page.url)[1]).path == "/fmm/":
        out += [(f"ai_card.{key}", key, ("ai_card",), True) for key in CARD_PATHS]
    targets = getattr(page, "revealed", {}) if mode == "record" else REVEALED_PATHS
    for target in targets:
        out += [
            (f"revealed.{target}.{key}", key, (f"revealed:{target}",), mode == "record")
            for key in REVEALED_PATHS[target]
        ]
    return out


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The row's figures under the old page's context paths, and the problems found on the way (a listed
    path the old answer does not have; holders that should agree and do not)."""
    contexts = holders(page)
    every = tuple(h for h in contexts if h != "ai_card" and not h.startswith("revealed:"))
    problems: list[str] = []
    out: dict[str, Any] = {}
    ok = page.shell.status_code == 200
    for path, name, where, required in _wanted(page, mode):
        if mode != "record":
            name, where = MOVED.get(path, (name, where or every))
        found = [(h, v) for h in where if (v := _read(contexts.get(h), name)) is not _ABSENT]
        if not found:
            if required:
                out[path] = None
                if ok:
                    problems.append(
                        f"{path}: not in the old page's context"
                        if mode == "record"
                        else f"{path}: in no part and not in the shell"
                    )
            continue
        first = _plain(name, found[0][1])
        for holder, value in found[1:]:
            if _plain(name, value) != first:
                problems.append(f"{path}: {found[0][0]} and {holder} disagree")
        out[path] = first
    return out, problems
