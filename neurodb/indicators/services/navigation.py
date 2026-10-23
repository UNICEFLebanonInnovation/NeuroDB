"""Sidebar and landing-page content, generated from the database (v2 hard-coded it).

The full menu (:data:`MENU`) is one tree: the daily entries first, then the figures management reads, the
eTools pages officers use weekly, money and assurance, Makani, Dirasa and youth while Compiler is live, the
resources, and the ActivityInfo archive at the bottom (``base.html``). A few rows show only while their source
has something to show (:func:`sources`); the people who can set such a source up
(:func:`neurodb.accounts.roles.can_set_up`) see a muted "Set up" row instead, which links to the row's own
page (it says what is missing).

Superusers see the full menu. Everyone else sees the focused menu (:data:`FOCUS`: Home, Partner reporting
with Progress reports and Partner monitoring, Field monitoring, Ask NeuroDB), or the entries a superuser
ticked for their role in the administration's Menu page (:func:`shown`, :func:`sidebar`). Only the menu is
shorter: a page left out of it still opens from links, bookmarks and the search, whose *Pages* group lists
every page of the full menu under the same conditions (:func:`pages`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from neurodb.accounts.models import Section
from neurodb.assistant import llm
from neurodb.indicators.models import Database, NeuroReport, ReportingYear

# a reporting year's id, or CURRENT: the year ticked current (the pages without a year)
CACHE_KEY = "nav:v2:{}"
CURRENT = "current"
CACHE_SECONDS = 120
SOURCES_KEY = "nav:v3:sources"  # what the menu shows of the sources that may be off (sources())


@dataclass
class SectionNav:
    section: Section | None
    databases: list[Database] = field(default_factory=list)


@dataclass
class Navigation:
    year: ReportingYear | None
    sections: list[SectionNav]
    neuro_reports: list[NeuroReport]
    hpm_reports: list[NeuroReport]
    years: list[ReportingYear]
    # The year each block lists: the page's year, or the latest year that has any when it has none
    # (a new reporting year starts empty; the menus stay, labelled with the year they show).
    databases_year: ReportingYear | None = None
    neuro_year: ReportingYear | None = None
    hpm_year: ReportingYear | None = None

    @property
    def database_count(self) -> int:
        """Databases listed (a section may hold several), the figure the Databases page counts."""
        return sum(len(s.databases) for s in self.sections)

    @property
    def sources(self) -> dict:
        """What the menu shows of the sources that may be off (:func:`sources`, kept two minutes)."""
        return sources()


def current_year() -> ReportingYear | None:
    return (
        ReportingYear.objects.filter(current=True).order_by("-name").first()
        or ReportingYear.objects.order_by("-name").first()
    )


def _current_of(years: list[ReportingYear]) -> ReportingYear | None:
    """:func:`current_year` picked from every reporting year, newest name first (no query of its own)."""
    return next((y for y in years if y.current), years[0] if years else None)


def _with_fallback(qs_for, year: ReportingYear | None, years: list[ReportingYear]):
    """``qs_for(y)`` for the given year, else for the latest year (by name) where it is not empty."""
    for candidate in ([year] if year else []) + [y for y in years if not year or y.id != year.id]:
        rows = list(qs_for(candidate))
        if rows:
            return candidate, rows
    return year, []


def build_navigation(year: ReportingYear | None = None) -> Navigation:
    """The sidebar for ``year`` (default: the current reporting year), kept two minutes per worker: a page
    without a year reads it under :data:`CURRENT`, so a kept sidebar costs no query, and building one reads
    the reporting years once (the current one is picked from them)."""
    key = CACHE_KEY.format(year.id if year else CURRENT)
    cached = cache.get(key)
    if cached:
        return cached
    years = list(ReportingYear.objects.order_by("-name"))
    year = year or _current_of(years)
    databases_year, databases = _with_fallback(
        lambda y: (
            Database.objects.filter(reporting_year=y, display=True)
            .select_related("section")
            .order_by("section__name", "label", "name")
        ),
        year,
        years,
    )
    by_section: dict[int | None, SectionNav] = {}
    for db in databases:
        by_section.setdefault(db.section_id, SectionNav(section=db.section)).databases.append(db)

    def reports(hpm: bool):
        return lambda y: NeuroReport.objects.filter(ryear=y, is_active=True, is_hpm=hpm).order_by("name")

    neuro_year, neuro_reports = _with_fallback(reports(False), year, years)
    hpm_year, hpm_reports = _with_fallback(reports(True), year, years)
    nav = Navigation(
        year=year,
        sections=sorted(by_section.values(), key=lambda s: s.section.name if s.section else "zzz"),
        neuro_reports=neuro_reports,
        hpm_reports=hpm_reports,
        years=years,
        databases_year=databases_year,
        neuro_year=neuro_year,
        hpm_year=hpm_year,
    )
    cache.set(key, nav, CACHE_SECONDS)
    return nav


def sources() -> dict:
    """Whether the sources the menu shows only when live have anything to show, the same for every person
    (plain data, kept two minutes per worker):

    - ``cpd_cycle``: ``[start year, end year]`` of the country programme cycle the Country programme page
      opens on (any cycle, an ended one too: it is history), or None;
    - ``cpd_live``: today falls inside that cycle;
    - ``compiler``: Compiler is connected, or Makani, Dirasa or youth figures are stored;
    - ``doc_review``: the document review is switched on, or it has found anything;
    - ``forecasts``: year-end forecasts exist.

    Who sees a muted "Set up" row instead of a missing item is the caller's choice
    (:func:`neurodb.accounts.roles.can_set_up`)."""
    cached = cache.get(SOURCES_KEY)
    if cached is not None:
        return cached
    from neurodb.core import years
    from neurodb.cpd.services import current_cycle
    from neurodb.education.models import EducationFigures
    from neurodb.insights.models import IndicatorForecast
    from neurodb.knowledge.models import DocumentFinding, DocumentReviewSettings
    from neurodb.youth import compiler
    from neurodb.youth.models import YouthFigures

    cycle = current_cycle()
    today = years.today()
    # the rest in one query (a page that finds nothing stored pays two, not six)
    found = _any_rows(
        education=EducationFigures.objects.all(),
        youth=YouthFigures.objects.all(),
        review_on=DocumentReviewSettings.objects.filter(pk=1, enabled=True),
        findings=DocumentFinding.objects.all(),
        forecasts=IndicatorForecast.objects.all(),
    )
    value = {
        "cpd_cycle": [cycle.start_year, cycle.end_year] if cycle else None,
        "cpd_live": bool(cycle and cycle.start_year <= today.year <= cycle.end_year),
        "compiler": bool(compiler.configured() or found["education"] or found["youth"]),
        "doc_review": bool(found["review_on"] or found["findings"]),
        "forecasts": found["forecasts"],
    }
    cache.set(SOURCES_KEY, value, CACHE_SECONDS)
    return value


def _any_rows(**querysets) -> dict[str, bool]:
    """Whether each queryset has a row, asked in one query (``SELECT EXISTS(...), EXISTS(...)``)."""
    from django.db import connection

    columns, params = [], []
    for queryset in querysets.values():
        sql, sub_params = queryset.order_by().values("pk")[:1].query.sql_with_params()
        columns.append(f"EXISTS({sql})")
        params.extend(sub_params)
    with connection.cursor() as cursor:
        cursor.execute("SELECT " + ", ".join(columns), params)  # noqa: S608 - the ORM's own SQL
        row = cursor.fetchone()
    return {name: bool(value) for name, value in zip(querysets, row, strict=True)}


def invalidate() -> None:
    ids = list(ReportingYear.objects.values_list("id", flat=True))
    cache.delete_many([CACHE_KEY.format(i) for i in [*ids, CURRENT, "none"]] + [SOURCES_KEY])


def invalidate_sources() -> None:
    """Drop only :func:`sources` on this worker (no query): for the inputs it reads that the rest of the menu
    does not (the document review's settings, the country programme cycles)."""
    cache.delete(SOURCES_KEY)


# ------------------------------------------------------------------------------------------------- the menu
@dataclass(frozen=True)
class MenuItem:
    """One row of the menu: its label (the page's title), the page it opens and its icon (no two rows share
    one), the other routes of its pages (where it is the current item too), and when it shows."""

    label: Any  # a lazy translation
    route: str  # the URL name the row links to
    icon: str
    also: tuple[str, ...] = ()  # the sub-pages it stands for (its "active" routes, its aria-current routes)
    when: str = ""  # a condition of _flags(); "" = always
    badge: str = ""  # "ai": the "AI" badge; "fmm_ai": the "AI" badge while Monitoring insights' AI is on
    count: bool = False  # For you's count of the points that need the person today (watch:badge)
    css: str = ""

    @property
    def routes(self) -> tuple[str, ...]:
        return (self.route, *self.also)

    @property
    def key(self) -> str:
        """The entry's name in the Menu page's choices (web.MenuChoice): its route, unique in the menu."""
        return self.route


@dataclass(frozen=True)
class MenuGroup:
    """A titled group of rows (``role="group"``, labelled by its title); the first group has no title."""

    key: str
    title: Any
    items: tuple[MenuItem, ...]
    when: str = ""  # the whole group's condition


# The two entries of Partner reporting (PRP): under Partner progress (eTools) in the full menu, a group of
# their own in the focused menu (FOCUS), the same rows in both.
PARTNER_MONITORING = MenuItem(
    _("Partner monitoring"),
    "reports:pd_monitoring",
    "target",
    ("reports:pd_monitoring_map", "reports:pd_indicator"),
)
PROGRESS_REPORTS = MenuItem(
    _("Progress reports"), "reports:partner_reporting", "file", ("reports:progress_report",)
)

MENU: tuple[MenuGroup, ...] = (
    MenuGroup(
        "today",
        None,
        (
            MenuItem(_("Home"), "reports:overview", "home"),
            MenuItem(_("For you"), "watch:for_you", "bell", count=True),
            MenuItem(_("Daily review"), "reports:daily_review", "list"),
            MenuItem(_("What's new"), "graph:whats_new", "inbox"),
            MenuItem(_("Ask NeuroDB"), "assistant:ask", "sparkle", when="ask", badge="ai"),
        ),
    ),
    MenuGroup(
        "results",
        _("Programme results"),
        (
            MenuItem(_("Country overview"), "reports:country_overview", "grid"),
            MenuItem(_("Management brief"), "reports:brief", "briefcase"),
            MenuItem(
                _("Country programme"),
                "cpd:dashboard",
                "route",
                ("cpd:indicator", "cpd:document"),
                when="cpd",
            ),
        ),
    ),
    MenuGroup(
        "etools",
        _("Partner progress (eTools)"),
        (
            PARTNER_MONITORING,
            PROGRESS_REPORTS,
            MenuItem(
                _("Programme documents"),
                "reports:programmes",
                "table",
                ("reports:programme_summary", "reports:programme_detail", "reports:pd_main_indicator"),
            ),
            MenuItem(
                _("Partners"),
                "reports:partners",
                "users",
                ("reports:partner_profile", "reports:partner_activityinfo"),
            ),
        ),
    ),
    MenuGroup(
        "funds",
        _("Funds and assurance"),
        (
            MenuItem(_("Funds"), "reports:funds", "database"),
            MenuItem(_("Donors"), "reports:donors", "gift"),
            MenuItem(_("Assurance"), "reports:assurance", "shield", ("reports:engagement_detail",)),
            MenuItem(_("Field monitoring"), "reports:monitoring", "eye"),
            MenuItem(
                _("Monitoring insights"),
                "fmm:dashboard",
                "ruler",
                ("fmm:visits", "fmm:visit", "fmm:insights", "fmm:report"),
                when="fmm",
                badge="fmm_ai",
            ),
            MenuItem(
                _("Action points"),
                "reports:action_points",
                "check",
                (
                    "reports:action_points_report",
                    "reports:action_point",
                    "reports:action_points_review",
                    "reports:action_points_summary",
                    "reports:local_action_points",
                    "reports:local_action_point_new",
                ),
            ),
        ),
    ),
    MenuGroup(
        "education",
        _("Education and youth"),
        (
            MenuItem(_("Makani (MSCC)"), "education:makani", "child"),
            MenuItem(
                _("Makani wellbeing"),
                "wellbeing:summaries",
                "shield-plus",
                ("wellbeing:flags", "wellbeing:follow_up"),
            ),
            MenuItem(_("Dirasa (Bridging)"), "education:dirasa", "school"),
            MenuItem(_("Youth programmes"), "youth:dashboard", "teacher"),
        ),
        when="compiler",
    ),
    MenuGroup(
        "resources",
        _("Resources"),
        (
            MenuItem(_("Knowledge base"), "knowledge:index", "layers", ("knowledge:detail", "knowledge:add")),
            MenuItem(_("Periodic reports"), "knowledge:series_index", "trend", ("knowledge:series",)),
            MenuItem(
                _("Document review"),
                "knowledge:review",
                "zap",
                (
                    "knowledge:review_verified",
                    "knowledge:review_batch_new",
                    "knowledge:review_batch_edit",
                    "knowledge:review_finding_new",
                    "knowledge:review_finding_edit",
                ),
                when="doc_review",
                badge="ai",
            ),
            MenuItem(_("Library"), "reports:library", "book", ("reports:library_item",)),
            MenuItem(_("Maps"), "reports:maps", "map", ("reports:map_item",)),
            MenuItem(_("Population"), "reports:population", "chart"),
            MenuItem(_("Data health"), "reports:data_health", "pulse"),
        ),
    ),
)
HELP = MenuItem(_("Help"), "help:index", "info", ("help:page",), css="nav-item-link--help")
ARCHIVE_ICON = "clock"  # the ActivityInfo archive's summary row
# The ActivityInfo archive's own rows (sub rows, no icon): its databases page and the year-end forecasts.
ARCHIVE_DATABASES = _("ActivityInfo databases")
ARCHIVE_FORECASTS = _("Year-end forecasts")
# Conditions whose item a person who can set the source up sees as a muted "Set up" row instead of nothing
SETUP_CONDITIONS = frozenset({"cpd", "compiler", "doc_review"})
# The two entries of the menu that are not a row of MENU, by their name in the Menu page's choices
ARCHIVE_KEY = "archive"  # the ActivityInfo archive (or Monthly results) at the bottom of the menu
HELP_KEY = HELP.route

# The focused menu (SPEC_focus_menu §1.2): what every role sees until a superuser ticks otherwise in the
# administration's Menu page, in this order. Superusers always see the full menu (MENU). Help is not in it:
# the "?" panel links to the Help pages and the search finds them (a superuser can tick it for a role).
PRP = MenuGroup("prp", _("Partner reporting (PRP)"), (PROGRESS_REPORTS, PARTNER_MONITORING))
FOCUS: tuple[str | MenuGroup, ...] = ("reports:overview", PRP, "reports:monitoring", "assistant:ask")
FOCUSED = frozenset(
    {"reports:overview", PROGRESS_REPORTS.key, PARTNER_MONITORING.key, "reports:monitoring", "assistant:ask"}
)
# What the Menu page says of an entry that shows only under a condition (MenuItem.when, MenuGroup.when)
NOTES = {
    "ask": _("Shown while the AI is on (to staff always)."),
    "fmm": _("Shown while Monitoring insights is switched on."),
    "cpd": _("Shown once a country programme is uploaded."),
    "compiler": _("Shown once Compiler's figures are there."),
    "doc_review": _("Shown once the document review is on."),
}


def _flags(user) -> dict[str, bool]:
    """The menu's conditions for ``user``: the person's own (Ask) and the sources' (:func:`sources`)."""
    live = sources()
    return {
        "": True,
        "ask": bool(llm.enabled() or getattr(user, "is_staff", False)),
        "fmm": bool(getattr(settings, "FMM_ENABLED", False)),
        "cpd": bool(live["cpd_cycle"]),  # any cycle, an ended one too: it is history
        "compiler": bool(live["compiler"]),
        "doc_review": bool(live["doc_review"]),
    }


def menu(user, can_set_up: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """The full menu's groups under their conditions for ``user``, in order: ``{"key", "title", "items"}``,
    each item a dict (:func:`_entry`) with ``muted`` set on a "Set up" row. The sidebar draws it whole for a
    superuser and the entries of the person's role for anyone else (:func:`sidebar`). ``can_set_up`` (asked
    at most once, and only when a source is not live) defaults to :func:`neurodb.accounts.roles.can_set_up`.
    A group whose items all stay hidden is left out, title and all."""
    from neurodb.accounts.roles import can_set_up as may_set_up

    flags = _flags(user)
    asked: list[bool] = []

    def setup() -> bool:
        if not asked:
            asked.append(bool(can_set_up() if can_set_up is not None else may_set_up(user)))
        return asked[0]

    groups = []
    for group in MENU:
        group_muted = False
        if not flags[group.when]:
            if group.when not in SETUP_CONDITIONS or not setup():
                continue
            group_muted = True
        items = []
        for item in group.items:
            muted = group_muted
            if not flags[item.when]:
                if item.when not in SETUP_CONDITIONS or not setup():
                    continue
                muted = True
            items.append(_entry(item, muted))
        if items:
            groups.append({"key": group.key, "title": group.title, "items": items})
    return groups


def help_entry() -> dict[str, Any]:
    """Help, the menu's last row."""
    return _entry(HELP, muted=False)


def keys() -> list[str]:
    """Every entry of the full menu by its name in the Menu page's choices, in the full menu's order: the
    rows of :data:`MENU` group by group, then the ActivityInfo archive, then Help."""
    return [item.key for group in MENU for item in group.items] + [ARCHIVE_KEY, HELP_KEY]


def entries() -> list[dict[str, Any]]:
    """Every entry as the Menu page lists it, in the order a role's sidebar draws them (:func:`sidebar`): the
    focused part first (:data:`FOCUS`), then each group of :data:`MENU` without the entries already listed,
    then the ActivityInfo archive and Help. Each is ``{"key", "label", "group", "note"}``: ``group`` the
    heading the page lists it under, ``note`` when it shows ("" when always)."""
    rows = {item.key: (item, group) for group in MENU for item in group.items}
    top, below = _("Top of the menu"), _("Below Partner reporting")
    found: list[dict[str, Any]] = []
    placed: set[str] = set()

    def add(item: MenuItem, heading, group: MenuGroup | None = None) -> None:
        when = item.when or (group.when if group else "")
        found.append({"key": item.key, "label": item.label, "group": heading, "note": NOTES.get(when, "")})
        placed.add(item.key)

    for n, part in enumerate(FOCUS):
        if isinstance(part, MenuGroup):
            for item in part.items:
                add(item, part.title, rows[item.key][1])
        elif part in rows:
            add(rows[part][0], top if n == 0 else below, rows[part][1])
    for group in MENU:
        for item in group.items:
            if item.key not in placed:
                # the untitled first group's rows follow the focused part's last rows, untitled too
                add(item, group.title or below, group)
    found.append(
        {
            "key": ARCHIVE_KEY,
            "label": _("ActivityInfo (the monthly results or the archive)"),
            "group": _("ActivityInfo"),
            "note": _(
                "Shown while there is an ActivityInfo year. Holds the only menu links to the Neuro reports "
                "and HPM reports, where section editors write their comments."
            ),
        }
    )
    found.append({"key": HELP_KEY, "label": HELP.label, "group": _("Bottom of the menu"), "note": ""})
    return found


def shown(user) -> frozenset[str] | None:
    """The entries the person's menu shows: None (every entry) for a superuser; else their role's, as a
    superuser ticked them in the Menu page (:mod:`neurodb.web.menu_choices`), or the focused menu
    (:data:`FOCUSED`) while no choice is saved. Never a query of its own: the role is read once per request
    (the top bar names it), the choices once per 10 s."""
    if getattr(user, "is_superuser", False):
        return None
    from neurodb.accounts.roles import role_of
    from neurodb.web import menu_choices

    return menu_choices.for_role(role_of(user))


def sidebar(user, can_set_up: Callable[[], bool] | None = None) -> dict[str, Any]:
    """The menu the sidebar draws for ``user``: ``{"groups", "archive", "help"}``. ``groups`` as
    :func:`menu` gives them (its conditions and muted rows apply to every layout); ``archive`` whether the
    ActivityInfo archive is drawn; ``help`` Help's row, or None.

    A superuser gets the full menu. Anyone else gets the entries :func:`shown` names, laid out focus
    first: the rows of :data:`FOCUS` in its order (an untitled row each, Partner reporting a group of its
    own), then every other entry shown, in :data:`MENU`'s order under its usual title. The layout does not
    change with what is ticked: one more tick adds a row and moves none."""
    full = menu(user, can_set_up)
    chosen = shown(user)
    if chosen is None:
        return {"groups": full, "archive": True, "help": help_entry()}
    drawn = {item["key"]: item for group in full for item in group["items"]}  # under their conditions
    groups: list[dict[str, Any]] = []
    placed: set[str] = set()
    for n, part in enumerate(FOCUS):
        if isinstance(part, MenuGroup):
            items = [drawn[i.key] for i in part.items if i.key in chosen and i.key in drawn]
            placed.update(i.key for i in part.items)
            if items:
                groups.append({"key": part.key, "title": part.title, "items": items})
        else:
            placed.add(part)
            if part in chosen and part in drawn:
                groups.append({"key": f"focus-{n}", "title": None, "items": [drawn[part]]})
    for group in full:
        items = [i for i in group["items"] if i["key"] in chosen and i["key"] not in placed]
        if items:
            groups.append({**group, "items": items})
    return {
        "groups": groups,
        "archive": ARCHIVE_KEY in chosen,
        "help": help_entry() if HELP_KEY in chosen else None,
    }


def _entry(item: MenuItem, muted: bool) -> dict[str, Any]:
    ai_badge = item.badge == "ai" or (item.badge == "fmm_ai" and getattr(settings, "FMM_AI", False))
    return {
        "key": item.key,
        "label": item.label,
        "url": reverse(item.route),
        "icon": item.icon,
        "routes": item.routes,
        "muted": muted,
        "badge": "ai" if ai_badge and not muted else "",
        # no count while NeuroDB Watch is switched off (For you says so itself)
        "count": item.count and bool(getattr(settings, "WATCH_ENABLED", False)),
        "css": item.css,
        "active": False,
        "current": False,
    }


def archive(state) -> dict[str, Any]:
    """The ActivityInfo archive's own links for :class:`neurodb.indicators.services.activityinfo.State`
    ``state``: its databases page on the year the import serves, else on the last ActivityInfo year (never the
    bare address while there is one: it opens the reporting year ticked current, which may be an empty new
    year), and one row per ActivityInfo year (``reports:databases?year=``), newest first."""
    base = reverse("reports:databases")
    names = state.names
    year = state.serving if state.serving in names else state.last

    def url(number: int) -> str:
        return f"{base}?{urlencode({'year': names[number]})}"

    return {
        "databases_url": url(year) if year in names else base,
        "years": [{"year": number, "name": names[number], "url": url(number)} for number in state.years],
    }


def pages(user, state=None, can_set_up: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """Every page of the full menu under its conditions for ``user`` (:func:`menu`; a muted row counts as
    shown), the ActivityInfo archive's own pages and Help, as the search's *Pages* group lists them:
    ``{"label", "url", "hint"}``, the hint being the full menu's group the page sits in. It does not follow
    the person's own menu (:func:`sidebar`): a page left out of it is still found here."""
    from neurodb.indicators.services import activityinfo

    found = []
    for group in menu(user, can_set_up):
        for item in group["items"]:
            found.append({"label": item["label"], "url": item["url"], "hint": group["title"] or ""})
    state = state or activityinfo.state()
    if state.years or state.current_name:  # the archive is drawn (it has years, or reporting years exist)
        hint = _("Monthly results (ActivityInfo)") if state.in_use else _("ActivityInfo archive")
        found.append({"label": ARCHIVE_DATABASES, "url": archive(state)["databases_url"], "hint": hint})
        if sources()["forecasts"]:
            found.append({"label": ARCHIVE_FORECASTS, "url": reverse("insights:forecasts"), "hint": hint})
    found.append({"label": HELP.label, "url": reverse(HELP.route), "hint": ""})
    return found
