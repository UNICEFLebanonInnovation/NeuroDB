"""Step 3's group D1: the country programme, youth and the donor page (SPEC7 §3.5, STAGE3 group D1).

**Pages.** The triage of step 3 (the x20 world given group D1's additions, on the base commit, as an
administrator and a viewer with a section, cold, medians of 6 to 11 runs) converts three pages: the country
programme (``/country-programme/``, 1.5-2.2 s), youth (``/youth/``, 320-370 ms on its first load, a
governorate or a master indicator) and the donor page (``/donor/``, 1.0-1.6 s). Two more are recorded although
they stay one request, because the quick wins of the stage change the code they run: Makani
(``/education/makani/``, 180-296 ms on every tab; its options and builds may move to the shared cache)
and the country programme's indicator page (``/country-programme/indicators/<pk>/``, SPEC7 §2.2: it reads
its sources with the dashboard's ``link_values``, whose payload reads change). Dirasa (about 100 ms) keeps
its code and is not recorded.

**Users.** The overview group's administrator (the first active superuser) and viewer with a section, and
the donor account with the lowest id (the world's ``demo-donor``, USAID). Every row of the matrix is asked
for by all three, so the recording also pins who may open what: a donor is sent to its own page from every
staff page (302), the donor page answers 404 to the viewer, and to the administrator unless it names an
``account`` (the preview), and a donor's page ignores ``account`` (it is always the donor's own).

**Matrix** (computed when recording, stored with the recording):

- the country programme: first load (the current cycle), the current cycle named, the previous cycle, a
  cycle that does not exist (the current one); the page has no other filter, tab or pagination;
- youth: first load (the latest year), the previous year, one partner, one donor, one governorate (targets
  hidden), one master indicator, all four together, a partner with no young people, a year not stored (the
  latest);
- the donor page: the own page, its "UNICEF Lebanon overall" tab, the previous year, a year without funds
  for the account (the current year then), another account asked for (``?account=`` of the second account:
  the donor still gets its own), and the administrator's preview of each account (with the overall tab and
  the previous year);
- Makani (quick wins only): first load, each tab, the previous year (and its maps tab), one partner, two
  slicers on the education tab, "Not specified", a filter with no result, a slicer value not offered, a year
  not stored;
- the indicator page (quick wins only): an indicator with Makani and Dirasa sources, one with a youth
  source, one with eTools sources (some suggested, not confirmed), one with a value typed in the admin.

**Context paths.** One function per page (:func:`cpd_paths`, :func:`youth_paths`, :func:`donor_paths`,
:func:`makani_paths`, :func:`indicator_paths`) lists the old page's context values by their names in the old
view's ``render`` context; a dotted path reads inside a dict (``data.links``). A few values are not compared
as Django objects but as what the page shows of them (:data:`READ_AS`): the cycle's documents (their file is
a storage object), the donor account (its ``last_seen_at`` changes as the donor visits; only what the page
draws is kept), the youth build's ``figures`` (the whole Compiler payload, 3 MB, which the page never draws:
only its year is kept), and Makani's ``page`` (a dataclass holding functions) and ``record`` (its payload is
deferred: only the fields the page draws). ``record`` reads each from the old page's one response.

``check`` reads each path where the page in parts keeps it: by default the same name in the frame or in any
part (every holder that has it must agree, otherwise a "consistency" problem), unless the page's entry in
:data:`MOVED` names another place (the youth filter's choices, the frame's; the donor's figures, its JSON part),
and drops the paths of :data:`NOT_DRAWN` on both sides (the youth build's payload and its unused
``chart_data``, no longer computed). :data:`JSON_IDS` holds the renamed JSON ids (the country programme's
``chart-data``). :func:`comparable` reads the builds' rows on both sides as the fields the pages draw: the old
builds held model instances, the stored ones hold plain dataclasses with those fields.

**The donor page's JSON.** The old page writes its figures in the ``donor-data`` JSON script. The page in
parts fetches them from ``donor.js`` (``?<query>&_part=data``, a JSON part, STAGE3), with no ``[data-part]``
placeholder for ``core.get_page`` to fill: :func:`prepare` (called by ``core.observe`` before the page is read)
asks for that part itself (with the HTMX headers, as the signed-in user of the page), puts the figures where the
old page wrote them and shows or hides what donor.js shows or hides, so the page is compared as the browser
shows it.

**The world.** ``scratchpad/async/s3/d1/build_world.sh <db> [checkout]``: ``s0/world.dump`` restored,
migrated with the checkout under test, then ``seed_world.py`` (the eTools Datamart, Compiler education and
youth sync history; Makani and Dirasa figures; youth figures and links; two country programme cycles; a second
donor account). Rebuild it before ``record`` and before ``check``: some GET requests write.
"""

from __future__ import annotations

import html as html_module
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

from scripts.parity import core
from scripts.parity import overview as _overview

NO_SUCH_ID = 999999  # an id no cycle, partner or year has


# ------------------------------------------------------------------------------------- the context paths
def cpd_paths() -> tuple[str, ...]:
    """``/country-programme/`` (``cpd.views.dashboard``): everything its ``render`` context holds. ``data`` is
    ``cpd.services.dashboard`` (model instances, compared by their fields: the cycle, outcomes, outputs, each
    indicator's ``Progress``, the programme documents matched to each output); ``chart_data`` is what
    ``chart-data`` carries."""
    return (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "programmes",
        "programme",
        "documents",
        "data",
        "chart_data",
    )


def youth_paths() -> tuple[str, ...]:
    """``/youth/`` (``youth.views.dashboard``): its ``render`` context, with ``data``
    (``youth.services.dashboard``) read key by key: its ``figures`` is the whole Compiler payload, which no
    template reads (only its year is compared, :data:`READ_AS`)."""
    return (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "years",
        "filters",
        "configured",
        "data.figures",
        "data.total",
        "data.female",
        "data.male",
        "data.partners",
        "data.indicators",
        "data.breakdowns",
        "data.programme_documents",
        "data.links",
        "data.linked",
        "data.suggested",
        "data.options",
        "data.fetched_at",
        "data.generated_at",
        "data.targets_hidden",
        "data.chart_data",
    )


def donor_paths() -> tuple[str, ...]:
    """``/donor/`` (``donors.views.page``): everything its ``render`` context holds. ``data`` is
    ``donors.services.build`` (plain data: what ``donor-data`` carries), ``years`` and ``as_of`` stay in the
    frame of the page in parts (STAGE3, SPEC7_review item 24)."""
    return ("account", "preview", "year", "years", "data", "as_of", "tab")


def makani_paths() -> tuple[str, ...]:
    """``/education/makani/`` (``education.views._page``): its ``render`` context, ``services.page_data``'s
    keys included (the slicers with their options, the kept and not applied ones, the filters, and ``data``,
    the tab's build: its ``charts`` are what ``edu-chart-data`` carries)."""
    return (
        "page",
        "page_title",
        "breadcrumbs",
        "periods",
        "period",
        "tab",
        "tab_template",
        "record",
        "configured",
        "old_format",
        "slicers",
        "kept",
        "not_applied",
        "filters",
        "data",
    )


def indicator_paths() -> tuple[str, ...]:
    """``/country-programme/indicators/<pk>/`` (``cpd.views.indicator``): everything its ``render`` context
    holds; ``progress`` is ``services.indicator_detail`` (each year's value and the sources behind it)."""
    return ("page_title", "breadcrumbs", "indicator", "progress", "rows", "suggested")


# A page: its name, the address it answers (the path, without the query) and its context paths.
PAGES: dict[str, tuple[re.Pattern[str], Callable[[], tuple[str, ...]]]] = {
    "cpd": (re.compile(r"/country-programme/"), cpd_paths),
    "youth": (re.compile(r"/youth/"), youth_paths),
    "donor": (re.compile(r"/donor/"), donor_paths),
    "makani": (re.compile(r"/education/makani/"), makani_paths),
    "indicator": (re.compile(r"/country-programme/indicators/\d+/"), indicator_paths),
}
CONVERTED = ("cpd", "youth", "donor")  # the others are recorded for the quick wins only


def _documents(documents: Any) -> Any:
    """The cycle's documents as the page lists them (the file's stored name, not the storage object)."""
    return [
        {
            "pk": d.pk,
            "title": d.title,
            "kind": d.kind,
            "kind_label": str(d.get_kind_display()),
            "note": d.note,
            "file": d.file.name,
        }
        for d in documents or []
    ]


def _account(account: Any) -> Any:
    """The donor account as the page draws it (not ``last_seen_at``, which the donor's own visits move)."""
    if account is None:
        return None
    return {
        "pk": account.pk,
        "name": account.name,
        "donors": list(account.donors),
        "grants": list(account.grants),
        "show_partner_names": account.show_partner_names,
        "contact": account.contact,
    }


def _page(page: Any) -> Any:
    """An education page's definition as the page uses it (not its two functions)."""
    return {
        "programme": page.programme,
        "name": page.name,
        "title": page.title,
        "subtitle": str(page.subtitle),
        "url_name": page.url_name,
        "period_param": page.period_param,
        "period_label": str(page.period_label),
        "tabs": [[key, str(label)] for key, label in page.tabs],
        "slicers": page.slicers,
    }


def _record(record: Any) -> Any:
    """The stored education figures as the page draws them (the row, not its payload)."""
    if record is None:
        return None
    return {
        "pk": record.pk,
        "programme": record.programme,
        "year": record.year,
        "fetched_at": record.fetched_at,
        "counted_at": record.counted_at,
    }


def _figures(figures: Any) -> Any:
    """The youth build's Compiler payload: its year only (the page never draws the payload itself)."""
    return None if figures is None else {"year": figures.year}


# Values compared as what the page shows of them, per page: ``{path: function(value)}`` (both modes).
READ_AS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "cpd": {"documents": _documents},
    "youth": {"data.figures": _figures},
    "donor": {"account": _account},
    "makani": {"page": _page, "record": _record},
}
# Where the page in parts keeps a path that moved, per page: ``{path: (its name there, (part, ...))}``,
# "shell" naming the frame (it also holds the parts it drew inline), "json" the donor page's JSON part. A path not
# listed is read under its own name, from the shell or any part; every holder must agree.
MOVED: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {name: {} for name in PAGES}
MOVED["youth"]["data.options"] = ("options", ("shell",))  # the filter bar's choices: the frame's, read apart
# the donor's figures: the page's JSON part (or the frame's, when stored)
MOVED["donor"]["data"] = ("data", ("json",))
# Paths of the old context that the page in parts no longer computes, with the reason (dropped from both
# sides).
NOT_DRAWN: dict[str, dict[str, str]] = {name: {} for name in PAGES}
NOT_DRAWN["youth"] = {
    "data.figures": "the year's whole Compiler payload, which no template drew; a build is stored plain",
    "data.chart_data": "computed for no template (STAGE3: drop the unused chart_data work)",
}
# The old pages' json_script ids renamed by the pages in parts, by the rule <page>-<part>-data: the country
# programme's ``chart-data`` is its summary part's (SPEC7 §2.1). The donor page's ``donor-data`` keeps its id
# (:func:`prepare` puts the figures its JSON part answers where donor.js reads them).
JSON_IDS: dict[str, str] = {"chart-data": "cpd-summary-data"}
# Element ids only the pages in parts have: the parts' own wrappers, and the donor page's state of its figures
# (the message while they come, the error box with Try again) and the two wrappers donor.js shows or hides
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*|load(?:State|Error|ErrorText|Retry)|mine(?:Empty|Content)")
# The ids of the donor's "Your contribution" content, which the old page left out for a year without funds and
# the page in parts keeps, hidden (the frame cannot know before the figures say so)
DONOR_CONTENT_IDS = frozenset(
    (
        "fGrant fSections fGov scope tiles q1 flow map mapLegend q2 spend grants q3 progress whoCard sexBlock sex "
        "ageBlock age govbars q4 vfm dumbbell q5 prog q6"
    ).split()
)
# timing.py times these rows (the converted pages' slow ones of the triage, and a donor's own page)
TIMED = (
    "cpd: first load",
    "cpd: previous cycle",
    "youth: first load",
    "youth: one governorate",
    "youth: one master indicator",
    "donor: own",
    "donor: preview, account 1",
)
# The part whose arrival is a page's first figure (timing.py): SPEC7 §2.1's leaders
FIRST_FIGURES = {"cpd": "summary", "youth": "summary", "donor": "data"}


def first_figure(url: str) -> str:
    """The part whose arrival is ``url``'s first figure (timing.py)."""
    return FIRST_FIGURES[page_of(url)]


def page_of(url: str) -> str:
    """The name of the page ``url`` opens (a key of :data:`PAGES`)."""
    path = urlsplit(url).path
    for name, (pattern, _paths) in PAGES.items():
        if pattern.fullmatch(path):
            return name
    raise ValueError(f"{url} is not a page of this group")


# ------------------------------------------------------------------------------------- users and matrix
def users() -> dict[str, Any]:
    """The overview group's administrator and viewer, and the donor of the lowest account id."""
    from neurodb.donors.models import DonorAccount

    people = dict(_overview.users())
    account = (
        DonorAccount.objects.filter(active=True, must_change_password=False, user__is_active=True)
        .select_related("user")
        .order_by("pk")
        .first()
    )
    if account is None or not account.is_valid_now():
        raise SystemExit("the world needs an active donor account (build_world.sh adds a second one)")
    people["donor"] = account.user
    return people


def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The pages and their filters, as (label, URL). Computed when recording and stored with it."""
    from django.utils import timezone

    from neurodb.cpd.models import CountryProgramme, Indicator, Link
    from neurodb.donors import services as donors
    from neurodb.donors.models import DonorAccount
    from neurodb.education import makani
    from neurodb.education import services as education
    from neurodb.education.figures import Figures as EducationFigures
    from neurodb.youth import services as youth
    from neurodb.youth.figures import Figures

    def address(path: str, *pairs: tuple[str, Any]) -> str:
        return path + ("?" + urlencode([(k, str(v)) for k, v in pairs]) if pairs else "")

    cycles = list(CountryProgramme.objects.order_by("-start_year", "pk"))
    current = next((c for c in cycles if c.current), None)
    previous = next((c for c in cycles if c is not current), None)
    if current is None or previous is None:
        raise SystemExit("the world needs a current country programme cycle and another one")
    cp = "/country-programme/"
    rows = [
        ("cpd: first load", cp),
        ("cpd: current cycle", address(cp, ("cycle", current.pk))),
        ("cpd: previous cycle", address(cp, ("cycle", previous.pk))),
        ("cpd: no such cycle", address(cp, ("cycle", NO_SUCH_ID))),
    ]

    years = youth.years()
    if len(years) < 2:
        raise SystemExit("the world needs two years of youth figures")
    latest = youth.load(years[0])
    figures = Figures(latest.payload)
    options = youth.options(figures)
    partner, donor = options["partners"][0][0], options["donors"][0][0]
    governorate, master = options["governorates"][0][0], options["masters"][1][0]
    yo = "/youth/"
    rows += [
        ("youth: first load", yo),
        ("youth: previous year", address(yo, ("year", years[1]))),
        ("youth: one partner", address(yo, ("year", years[0]), ("partner", partner))),
        ("youth: one donor", address(yo, ("year", years[0]), ("donor", donor))),
        ("youth: one governorate", address(yo, ("year", years[0]), ("governorate", governorate))),
        ("youth: one master indicator", address(yo, ("year", years[0]), ("master", master))),
        (
            "youth: every filter",
            address(
                yo,
                ("year", years[0]),
                ("master", master),
                ("partner", partner),
                ("donor", donor),
                ("governorate", governorate),
            ),
        ),
        ("youth: a partner without young people", address(yo, ("year", years[0]), ("partner", NO_SUCH_ID))),
        ("youth: a year not stored", address(yo, ("year", "1999"))),
    ]

    accounts = list(DonorAccount.objects.order_by("pk"))
    if len(accounts) < 2:
        raise SystemExit("the world needs two donor accounts (build_world.sh adds the second one)")
    own, other = accounts[0], accounts[1]
    today = timezone.localdate()  # the frozen day (core.freeze patched django.utils.timezone)
    offered = donors.years(own, today)
    earlier = next((y for y in offered if y < today.year), today.year - 1)
    not_offered = min(offered) - 3
    do = "/donor/"
    rows += [
        ("donor: own", do),
        ("donor: own, overall tab", address(do, ("tab", "overall"))),
        ("donor: own, previous year", address(do, ("year", earlier))),
        ("donor: own, a year without funds", address(do, ("year", not_offered))),
        ("donor: another account asked", address(do, ("account", other.pk))),
        ("donor: preview, account 1", address(do, ("account", own.pk))),
        ("donor: preview, account 1, overall tab", address(do, ("account", own.pk), ("tab", "overall"))),
        ("donor: preview, account 2, previous year", address(do, ("account", other.pk), ("year", earlier))),
    ]
    return (
        rows
        + _makani_rows(address, makani, education, EducationFigures)
        + _indicator_rows(current, Indicator, Link)
    )


def _makani_rows(address, makani, education, figures_of) -> list[tuple[str, str]]:
    """Makani's rows (recorded for the quick wins): every tab, the previous year, slicers."""
    periods = education.periods(makani.PAGE.programme)
    if len(periods) < 2:
        raise SystemExit("the world needs two years of Makani figures")
    stored = education.record(makani.PAGE.programme, periods[0])
    figures = figures_of(stored.payload)
    options = makani.PAGE.options(figures)
    partner = options["partner"][0][0]
    governorates = [value for value, _label in options["governorate"] if value != education.NONE]
    center = next(c for c in figures.payload["centers"] if str(c["governorate"]) in governorates)
    elsewhere = next(g for g in governorates if g != str(center["governorate"]))
    mk, year = "/education/makani/", ("year", periods[0])
    return [
        ("makani: first load", mk),
        ("makani: education tab", address(mk, ("tab", "education"))),
        ("makani: health tab", address(mk, ("tab", "health"))),
        ("makani: maps tab", address(mk, ("tab", "maps"))),
        ("makani: previous year", address(mk, ("year", periods[1]))),
        ("makani: previous year, maps tab", address(mk, ("tab", "maps"), ("year", periods[1]))),
        ("makani: one partner", address(mk, ("tab", "overview"), year, ("partner", partner))),
        (
            "makani: two slicers, education tab",
            address(mk, ("tab", "education"), year, ("gender", "Male"), ("governorate", governorates[0])),
        ),
        ("makani: not specified", address(mk, ("tab", "overview"), year, ("gender", education.NONE))),
        (
            "makani: a filter with no result",
            address(mk, ("tab", "overview"), year, ("center", center["id"]), ("governorate", elsewhere)),
        ),
        (
            "makani: a slicer value not offered",
            address(mk, ("tab", "overview"), year, ("partner", NO_SUCH_ID)),
        ),
        ("makani: a year not stored", address(mk, ("year", "1999"))),
    ]


def _indicator_rows(cycle, indicator_model, link_model) -> list[tuple[str, str]]:
    """The indicator page's rows (recorded for the quick wins): one indicator per kind of source."""
    mine = indicator_model.objects.filter(programme=cycle)

    def first(**where) -> int:
        found = mine.filter(**where).order_by("pk").values_list("pk", flat=True).first()
        if found is None:
            raise SystemExit(f"the world needs a current-cycle indicator with {where}")
        return found

    unconfirmed = link_model.objects.filter(indicator__programme=cycle, kind="etools", confirmed=False)
    return [
        (
            "indicator: Makani and Dirasa sources",
            f"/country-programme/indicators/{first(links__kind='education')}/",
        ),
        ("indicator: a youth source", f"/country-programme/indicators/{first(links__kind='youth')}/"),
        (
            "indicator: eTools sources, one suggested",
            f"/country-programme/indicators/{first(pk__in=unconfirmed.values('indicator_id'))}/",
        ),
        ("indicator: a value typed", f"/country-programme/indicators/{first(values__isnull=False)}/"),
    ]


# --------------------------------------------------------------------------------------------- the figures
_ABSENT = object()


def _read(context: Any, path: str) -> Any:
    """The value at ``path`` (dotted: a dict key, then keys inside it) in a template context, or ``_ABSENT``."""
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


def _plain(name: str, path: str, value: Any) -> Any:
    read_as = READ_AS.get(name, {}).get(path)
    return core.plain(read_as(value) if read_as is not None else value)


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The page's figures under the old page's context paths, and the problems found on the way (a listed
    path the old page does not have; parts that should agree and do not). A page that did not answer 200
    (a redirect, a 404) has no figures."""
    name = page_of(page.url)
    paths = PAGES[name][1]()
    shell = getattr(page.shell, "context", None)
    problems: list[str] = []
    out: dict[str, Any] = {}
    if page.shell.status_code != 200:
        return out, problems
    if mode == "record":
        for path in paths:
            value = _read(shell, path)
            if value is _ABSENT:
                if path.split(".", 1)[0] != "data" or _read(shell, "data") not in (None, _ABSENT):
                    problems.append(f"{path}: not in the old page's context")
                out[path] = None
                continue
            out[path] = _plain(name, path, value)
        return out, problems

    contexts = {"shell": shell} | {
        part: getattr(response, "context", None)
        for part, response in page.parts.items()
        if response.status_code == 200
    }
    if getattr(page, "donor_data", None) is not None:  # the donor page's figures, as donor.js reads them
        contexts["json"] = {"data": page.donor_data}
    for path in paths:
        if path in NOT_DRAWN.get(name, {}):
            continue
        where, names = MOVED[name].get(path, (path, tuple(contexts)))
        found = [
            (holder, value)
            for holder in names
            if (value := _read(contexts.get(holder), where)) is not _ABSENT
        ]
        if not found:
            out[path] = None
            if path.split(".", 1)[0] != "data" or _read(shell, "data") not in (None, _ABSENT):
                problems.append(f"{path}: in no part and not in the shell")
            continue
        first = _plain(name, path, found[0][1])
        for holder, value in found[1:]:
            if _plain(name, path, value) != first:
                problems.append(f"{path}: {found[0][0]} and {holder} disagree")
        out[path] = first
    return out, problems


# ------------------------------------------------------------------------------------- one form for both
# The builds of the pages in parts are plain data: where the old ones held model instances (the cycle's
# outcomes, outputs, indicators and programme documents; a youth link and its eTools indicator), they hold
# dataclasses with the fields the pages draw, under the models' names. Both sides are read as those fields.
_INDICATOR = ("pk", "code", "title", "unit", "accumulate", "baseline", "baseline_year", "target", "origin")
_OUTCOME = ("pk", "code", "title", "sections")
_OUTPUT = ("pk", "code", "title")
_PD = ("pk", "number")
_LINK = ("pk", "pd_id", "etools_title", "source")
_ETOOLS = ("key", "target", "reached", "reached_in_year", "tracking", "prp_cumulative", "mismatch")


def _ref(tree: Any, fields: tuple[str, ...]) -> Any:
    """A row as its ``fields``: from a model instance (``{"model", "pk", "fields"}``) or a dataclass."""
    if isinstance(tree, dict) and "model" in tree and isinstance(tree.get("fields"), dict):
        return {f: tree["pk"] if f == "pk" else tree["fields"].get(f) for f in fields}
    if isinstance(tree, dict) and "__dataclass__" in tree:
        return {f: tree.get(f) for f in fields}
    return tree


def _progress(tree: Any) -> Any:
    return {**tree, "indicator": _ref(tree.get("indicator"), _INDICATOR)} if isinstance(tree, dict) else tree


def _cpd_data(data: Any) -> Any:
    """The country programme's build as the page draws it. The old build also held the cycle itself
    (``programme``), which the page draws from its own ``programme`` (compared there)."""
    if not isinstance(data, dict):
        return data
    out = {key: value for key, value in data.items() if key != "programme"}
    out["outcomes"] = [
        {
            **outcome,
            "outcome": _ref(outcome["outcome"], _OUTCOME),
            "indicators": [_progress(p) for p in outcome["indicators"]],
            "outputs": [
                {
                    **row,
                    "output": _ref(row["output"], _OUTPUT),
                    "indicators": [_progress(p) for p in row["indicators"]],
                    "pds": [_ref(pd, _PD) for pd in row["pds"]],
                }
                for row in outcome["outputs"]
            ],
        }
        for outcome in data.get("outcomes") or []
    ]
    return out


def _youth_entry(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    return {**entry, "link": _ref(entry.get("link"), _LINK), "etools": _ref(entry.get("etools"), _ETOOLS)}


def _youth_rows(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [
        {
            **row,
            "links": [_youth_entry(e) for e in row.get("links") or []],
            "subs": [
                {**sub, "links": [_youth_entry(e) for e in sub.get("links") or []]} for sub in row["subs"]
            ],
        }
        for row in rows
    ]


def comparable(observation: dict[str, Any], side: str) -> dict[str, Any]:
    """One observation as it is compared (``side`` is "record" or "check"): the paths of :data:`NOT_DRAWN`
    left out on both sides, the builds' rows read as the fields the pages draw (the old builds held model
    instances), and for a donor's year without funds the content ids the page in parts keeps hidden."""
    if observation["status"] != 200:
        return observation
    page = page_of(observation["url"])
    drop = NOT_DRAWN.get(page, {})
    figures = {path: value for path, value in observation["figures"].items() if path not in drop}
    if page == "cpd" and "data" in figures:
        figures["data"] = _cpd_data(figures["data"])
    if page == "youth":
        if isinstance(figures.get("data.links"), list):
            figures["data.links"] = [_youth_entry(e) for e in figures["data.links"]]
        if "data.indicators" in figures:
            figures["data.indicators"] = _youth_rows(figures["data.indicators"])
    out = {**observation, "figures": figures}
    data = figures.get("data") if page == "donor" else None
    if side == "check" and isinstance(data, dict) and not data.get("pds"):
        out["ids"] = [i for i in observation["ids"] if i not in DONOR_CONTENT_IDS]
    return out


# ------------------------------------------------------------------------------------- the donor's figures
def prepare(page: core.Page, mode: str) -> None:
    """The donor page as the browser shows it once ``donor.js`` has its figures: the page in parts answers its
    frame and the script fetches them from the page's ``data`` part (``main[data-donor-src]``, asked for here
    as donor.js asks, by the page's signed-in client). They are put where the old page wrote them
    (``donor-data``, read for the JSON comparison), the year without funds shows its empty card instead of the
    contribution's content, and the "Loading the figures" line is gone, as donor.js and donor.css leave them.
    When the frame carries the figures already (stored), they are read there. Nothing for the old page, which
    always wrote them."""
    if page_of(page.url) != "donor" or page.shell.status_code != 200:
        return
    found = re.search(r'<script id="donor-data" type="application/json">(.*?)</script>', page.html, re.S)
    source = re.search(r'data-donor-src="([^"]+)"', page.html)
    if found:
        page.donor_data = json.loads(found.group(1))
    elif source is not None:
        from django.utils.html import json_script

        response, pending = core.hx_get(
            page.shell.client, html_module.unescape(source.group(1)), "", page.url
        )
        page.pending += pending
        page.parts["data"] = response
        if response.status_code != 200:
            return
        page.donor_data = json.loads(response.content)
        page.html = page.html.replace(
            '<div id="tip" hidden></div>',
            '<div id="tip" hidden></div>\n' + json_script(page.donor_data, "donor-data"),
        )
    if source is None:
        return
    funded = bool((getattr(page, "donor_data", None) or {}).get("pds"))
    page.html = page.html.replace('<p class="loadstate__wait">', '<p class="loadstate__wait" hidden>')
    page.html = re.sub(
        r'(<div class="card empty" id="mineEmpty")(?: hidden)?',
        r"\1" + ("" if not funded else " hidden"),
        page.html,
    )
    page.html = re.sub(
        r'(<div id="mineContent")(?: hidden)?', r"\1" + (" hidden" if not funded else ""), page.html
    )
