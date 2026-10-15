"""The left menu (SPEC_HOME §4): one tree for everyone, daily entries first, then the figures management reads,
the eTools pages, money and assurance, Makani, Dirasa and youth while Compiler is live, the resources, and the
ActivityInfo archive at the bottom. Rows of a source that is not live show only to the people who can set it up,
as muted "Set up" rows; the current page is marked on its most specific link; counts say what they count; the
script that remembers which blocks a reader opened stores only the reader's own choices."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VIEWER
from neurodb.cpd.models import CountryProgramme
from neurodb.education.models import EducationFigures
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo, navigation
from neurodb.knowledge.models import DocumentReviewSettings

pytestmark = pytest.mark.django_db

TEMPLATES = Path(django_settings.BASE_DIR) / "neurodb" / "web" / "templates"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Every row a person sees when every source is live, in order, with its icon
FULL_TREE = [
    (None, [("Home", "home"), ("For you", "bell"), ("Daily review", "list"), ("What's new", "inbox"),
            ("Ask NeuroDB", "sparkle")]),
    ("Programme results", [("Country overview", "grid"), ("Management brief", "briefcase"),
                           ("Country programme", "route")]),
    ("Partner progress (eTools)", [("Partner monitoring", "target"), ("Progress reports", "file"),
                                   ("Programme documents", "table"), ("Partners", "users")]),
    ("Funds and assurance", [("Funds", "database"), ("Donors", "gift"), ("Assurance", "shield"),
                             ("Field monitoring", "eye"), ("Monitoring insights", "ruler"),
                             ("Action points", "check")]),
    ("Education and youth", [("Makani (MSCC)", "child"), ("Makani wellbeing", "shield-plus"),
                             ("Dirasa (Bridging)", "school"), ("Youth programmes", "teacher")]),
    ("Resources", [("Knowledge base", "layers"), ("Periodic reports", "trend"), ("Document review", "zap"),
                   ("Library", "book"), ("Maps", "map"), ("Population", "chart"), ("Data health", "pulse")]),
]  # fmt: skip
ROW = re.compile(
    r'<a class="nav-item-link([^"]*)" href="([^"]*)"([^>]*)>'
    r'<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-([\w-]+)"></use></svg>\s*'
    r"<span>([^<]+)</span>(.*?)</a>",
    re.S,
)


def _sidebar(html: str) -> str:
    return html.split('id="sidebar"', 1)[1].split("</nav>", 1)[0]


def _rows(html: str) -> list[dict]:
    """The menu's rows with an icon (the archive's own rows have none), in order."""
    return [
        {
            "css": css.split(),
            "href": href.replace("&amp;", "&"),
            "attrs": attrs,
            "icon": icon,
            "label": label.replace("&#x27;", "'"),
            "rest": rest,
        }
        for css, href, attrs, icon, label, rest in ROW.findall(_sidebar(html))
    ]


def _groups(html: str) -> list[tuple[str, str]]:
    """(key, title) of every titled group, in order."""
    side = _sidebar(html)
    found = re.findall(
        r'<div class="nav-group" role="group" aria-labelledby="nav-title-([\w-]+)">\s*'
        r'<div class="sidebar__section-title" id="nav-title-\1">([^<]+)</div>',
        side,
    )
    return found


def _person(username: str, role: str, *, is_staff=False, is_superuser=False) -> User:
    user = User.objects.create_user(
        username=username, password="pass-123456-x", is_staff=is_staff, is_superuser=is_superuser
    )
    user.groups.add(Group.objects.get(name=role))
    return user


def _page(client, user, url: str) -> str:
    client.force_login(user)
    return client.get(url).content.decode()


@pytest.fixture
def no_compiler(settings):
    settings.COMPILER_API_URL = ""
    settings.COMPILER_API_TOKEN = ""


@pytest.fixture
def everything_live(settings, no_compiler):
    """Every source the menu may leave out has something to show: a cycle, Compiler figures, the document
    review switched on; Ask and Monitoring insights on."""
    settings.AI_ASSISTANT_ENABLED = True
    settings.FMM_ENABLED = True
    CountryProgramme.objects.create(name="Country programme 2026-2028", start_year=2026, end_year=2028)
    EducationFigures.objects.create(programme="mscc", year="2026", fetched_at=timezone.now(), payload={})
    DocumentReviewSettings.objects.create(pk=1, enabled=True)
    navigation.invalidate()


# --------------------------------------------------------------------------------------------- the tree
def test_the_tree_its_order_labels_groups_and_icons(client, roles, everything_live):
    viewer = _person("tree-viewer", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    rows = _rows(html)
    expected = [row for _title, items in FULL_TREE for row in items] + [("Help", "info")]
    assert [(r["label"], r["icon"]) for r in rows] == expected
    assert _groups(html) == [
        ("results", "Programme results"),
        ("etools", "Partner progress (eTools)"),
        ("funds", "Funds and assurance"),
        ("education", "Education and youth"),
        ("resources", "Resources"),
    ]
    # no two rows share an icon, and every icon is in the sprite
    icons = [r["icon"] for r in rows]
    assert len(icons) == len(set(icons))
    sprite = set(re.findall(r'<symbol id="i-([\w-]+)"', render_to_string("components/icons.html")))
    assert set(icons) | {navigation.ARCHIVE_ICON} <= sprite and navigation.ARCHIVE_ICON not in icons
    # the tree in the code is the tree on the page (the search's Pages group reads it too)
    assert [(str(g.title) if g.title else None) for g in navigation.MENU] == [t for t, _ in FULL_TREE]
    assert [(str(i.label), i.icon) for g in navigation.MENU for i in g.items] == expected[:-1]
    # each row links to its page; Help is last, after the ActivityInfo archive when there is one
    by_label = {r["label"]: r["href"] for r in rows}
    assert by_label["Daily review"] == reverse("reports:daily_review")
    assert by_label["Country overview"] == reverse("reports:country_overview")
    assert by_label["Periodic reports"] == reverse("knowledge:series_index")
    assert "nav-item-link--help" in rows[-1]["css"]


def test_the_first_screen_holds_every_etools_and_field_monitoring_page(client, roles, everything_live):
    """At 1440 x 900 (the sidebar shows 844 px; a row is 32.6 px, a group title 41 px) Progress reports and
    Action points are on the first screen, even for an Administrator with Ask and a country programme cycle."""
    admin = _person("first-screen", ADMIN, is_staff=True)
    html = _page(client, admin, reverse("reports:library"))
    labels = [r["label"] for r in _rows(html)]
    through = labels.index("Action points") + 1
    titles = sum(1 for _key, title in _groups(html) if labels.index(_first_of(title)) < through)
    assert labels.index("Progress reports") < labels.index("Action points")
    assert through * 32.6 + titles * 41 + 12 <= 844, (through, titles)


def _first_of(title: str) -> str:
    return next(items[0][0] for t, items in FULL_TREE if t == title)


def test_a_viewer_sees_no_row_of_a_source_that_is_not_live(client, roles, no_compiler, settings):
    settings.AI_ASSISTANT_ENABLED = False
    settings.FMM_ENABLED = True
    viewer = _person("quiet-viewer", VIEWER, is_staff=False)
    html = _page(client, viewer, reverse("reports:library"))
    labels = [r["label"] for r in _rows(html)]
    for hidden in (
        "Ask NeuroDB",
        "Country programme",
        "Makani (MSCC)",
        "Youth programmes",
        "Document review",
    ):
        assert hidden not in labels, hidden
    assert "Education and youth" not in [title for _key, title in _groups(html)]  # no empty group
    assert "Set up" not in _sidebar(html) and "nav-item-link--muted" not in html
    # Ask shows to a member of staff even while the assistant is off (its page says so), as before
    staff_viewer = _person("staff-viewer", VIEWER, is_staff=True)
    assert "Ask NeuroDB" in [
        r["label"] for r in _rows(_page(client, staff_viewer, reverse("reports:library")))
    ]


@pytest.mark.parametrize(
    ("who", "muted"),
    [
        ("superuser", True),
        ("staff_admin", True),
        ("admin_without_staff", False),  # the admin would refuse them
        ("staff_viewer", False),  # a Viewer with staff status has nothing to set up
    ],
)
def test_muted_set_up_rows_only_for_those_who_can_set_up(client, roles, no_compiler, who, muted):
    user = {
        "superuser": lambda: _person("su", VIEWER, is_staff=True, is_superuser=True),
        "staff_admin": lambda: _person("staff-admin", ADMIN, is_staff=True),
        "admin_without_staff": lambda: _person("plain-admin", ADMIN),
        "staff_viewer": lambda: _person("staff-viewer", VIEWER, is_staff=True),
    }[who]()
    html = _page(client, user, reverse("reports:library"))
    rows = {r["label"]: r for r in _rows(html)}
    for label, page in (
        ("Country programme", "cpd:dashboard"),
        ("Makani (MSCC)", "education:makani"),
        ("Makani wellbeing", "wellbeing:summaries"),
        ("Dirasa (Bridging)", "education:dirasa"),
        ("Youth programmes", "youth:dashboard"),
        ("Document review", "knowledge:review"),
    ):
        if not muted:
            assert label not in rows, (who, label)
            continue
        row = rows[label]
        # a quiet row to its own page, which says what is missing; "Set up" instead of any other badge
        assert "nav-item-link--muted" in row["css"] and row["href"] == reverse(page)
        assert '<span class="nav-badge nav-badge--muted">Set up</span>' in row["rest"]
        assert '<span class="nav-badge">AI</span>' not in row["rest"]
    assert ("Education and youth" in dict(_groups(html)).values()) is muted


def test_live_sources_show_their_rows_to_everyone_never_muted(client, roles, everything_live):
    for user in (_person("live-viewer", VIEWER), _person("live-admin", ADMIN, is_staff=True)):
        html = _page(client, user, reverse("reports:library"))
        rows = {r["label"]: r for r in _rows(html)}
        for label in ("Country programme", "Makani (MSCC)", "Document review"):
            assert "nav-item-link--muted" not in rows[label]["css"], label
        assert '<span class="nav-badge">AI</span>' in rows["Document review"]["rest"]
        assert "Set up" not in _sidebar(html)


def test_an_ended_cycle_still_shows_the_country_programme(client, roles, no_compiler):
    CountryProgramme.objects.create(name="Country programme 2021-2025", start_year=2021, end_year=2025)
    html = _page(client, _person("history", VIEWER), reverse("reports:library"))
    assert "Country programme" in [r["label"] for r in _rows(html)]  # history


def test_monitoring_insights_and_its_ai_badge_follow_their_switches(client, roles, settings):
    viewer = _person("fmm-viewer", VIEWER)
    settings.FMM_ENABLED, settings.FMM_AI = True, False
    rows = {r["label"]: r for r in _rows(_page(client, viewer, reverse("reports:library")))}
    assert "nav-badge" not in rows["Monitoring insights"]["rest"]
    settings.FMM_AI = True
    rows = {r["label"]: r for r in _rows(_page(client, viewer, reverse("reports:library")))}
    assert '<span class="nav-badge">AI</span>' in rows["Monitoring insights"]["rest"]
    settings.FMM_ENABLED = False
    assert "Monitoring insights" not in [
        r["label"] for r in _rows(_page(client, viewer, reverse("reports:library")))
    ]


def test_for_you_has_no_count_while_watch_is_switched_off(client, roles, settings):
    viewer = _person("watch-viewer", VIEWER)
    rows = {r["label"]: r for r in _rows(_page(client, viewer, reverse("reports:library")))}
    assert f'hx-get="{reverse("watch:badge")}"' in rows["For you"]["rest"]
    settings.WATCH_ENABLED = False
    rows = {r["label"]: r for r in _rows(_page(client, viewer, reverse("reports:library")))}
    assert "hx-get" not in rows["For you"]["rest"] and rows["For you"]["href"] == reverse("watch:for_you")


# ------------------------------------------------------------------------------------------ aria-current
@pytest.mark.parametrize(
    ("url", "label"),
    [
        (lambda h: reverse("reports:library"), "Library"),
        (lambda h: reverse("reports:pd_monitoring_map"), "Partner monitoring"),
        (lambda h: reverse("reports:programme_summary"), "Programme documents"),
        (lambda h: reverse("help:page", args=["overview"]), "Help"),
        (lambda h: reverse("knowledge:series_index"), "Periodic reports"),
    ],
)
def test_the_current_page_is_marked_on_its_row_sub_pages_included(client_viewer, hierarchy, url, label):
    html = client_viewer.get(url(hierarchy)).content.decode()
    current = [r for r in _rows(html) if 'aria-current="page"' in r["attrs"]]
    assert [r["label"] for r in current] == [label]
    assert "active" in current[0]["css"]
    assert _sidebar(html).count('aria-current="page"') == 1


def test_one_current_link_on_a_year_of_activityinfo_databases(client_viewer, hierarchy):
    """On /databases/?year=2025 the archive's 2025 row is the page's link; "ActivityInfo databases" stays
    active only. On /databases/ alone it is the current link."""
    older = ReportingYear.objects.create(name="2025", year="2025")
    old_db = Database.objects.create(
        ai_id=202501, db_id="old", name="CP 2025", label="CP 2025", username="", password="",
        section=hierarchy["database"].section, reporting_year=older, display=True,
    )  # fmt: skip
    from neurodb.facts.models import ActivityReportNew

    ActivityReportNew.objects.create(
        dbase=old_db, database_ai_id="202501", indicator_id="i_a", indicator_value=5, month_name="2025-11-01"
    )
    activityinfo.invalidate()
    html = client_viewer.get(reverse("reports:databases") + "?year=2025").content.decode()
    side = _sidebar(html)
    assert side.count('aria-current="page"') == 1
    assert re.search(r'<a class="nav-item-link nav-item-link--sub active" href="/databases/\?year=2025" '
                     r'aria-current="page">2025</a>', side)  # fmt: skip
    databases = re.search(
        r'<a class="([^"]*)" href="([^"]*)"([^>]*)><span>ActivityInfo databases</span>', side
    )
    assert "active" in databases.group(1) and "aria-current" not in databases.group(3)
    side = _sidebar(client_viewer.get(reverse("reports:databases")).content.decode())
    assert side.count('aria-current="page"') == 1
    assert re.search(r'aria-current="page"><span>ActivityInfo databases</span>', side)
    # every link of a database's sub-pages is current there (not only its dashboard's)
    page = client_viewer.get(reverse("reports:database_analytical", args=[hierarchy["database"].id]))
    side = _sidebar(page.content.decode())
    assert side.count('aria-current="page"') == 1 and 'title="Child Protection" aria-current="page"' in side


# ----------------------------------------------------------------------------------------- the archive
def _archive(html: str) -> str:
    return re.search(r'<details class="sidebar__archive"[^>]*>.*?</summary>', _sidebar(html), re.S).group(0)


def test_the_archive_closed_at_the_bottom_while_activityinfo_is_archived(client_viewer, hierarchy):
    html = client_viewer.get(reverse("reports:library")).content.decode()  # hierarchy: records to Feb 2026
    archive = _archive(html)
    assert (
        'data-nav-key="archive"' in archive and " open" not in archive and "data-nav-default" not in archive
    )
    assert '<use href="#i-clock">' in archive
    assert "<span>ActivityInfo archive</span>" in archive
    assert '<span class="sidebar__archive-span">2026</span>' in archive
    assert "Monthly results (ActivityInfo)" not in html
    side = _sidebar(html)
    assert side.index('data-nav-key="archive"') > side.index(">Data health<") > side.index(">Action points<")
    assert side.index('data-nav-key="archive"') < side.index(">Help<")
    # its links carry the year (the bare address opens the reporting year ticked current, maybe empty)
    assert 'href="/databases/?year=2026"><span>ActivityInfo databases</span>' in side
    assert 'href="/databases/"' not in side


def test_the_archive_reads_monthly_results_and_opens_by_default_while_in_use(
    client_viewer, hierarchy, activityinfo_in_use
):
    archive = _archive(client_viewer.get(reverse("reports:library")).content.decode())
    assert "<span>Monthly results (ActivityInfo)</span>" in archive
    assert 'data-nav-default="open"' in archive and " open" not in archive.replace(
        'data-nav-default="open"', ""
    )
    assert "sidebar__archive-span" not in archive


@pytest.mark.parametrize(
    "url",
    [
        lambda h: reverse("reports:databases"),
        lambda h: reverse("reports:database_dashboard", args=[h["database"].id]),
        lambda h: reverse("reports:report_hpm", args=[h["report"].id]),
        lambda h: reverse("insights:forecasts"),
    ],
)
def test_the_archive_is_open_on_every_activityinfo_page(client_viewer, hierarchy, url):
    archive = _archive(client_viewer.get(url(hierarchy)).content.decode())
    assert re.search(r'data-nav-key="archive" open data-nav-active>', archive)


def test_the_archive_lists_a_row_per_activityinfo_year(client_viewer, hierarchy):
    older = ReportingYear.objects.create(name="2024", year="2024")
    empty = ReportingYear.objects.create(name="2025", year="2025")  # databases without records: no row
    db = Database.objects.create(
        ai_id=202401, db_id="y24", name="CP 2024", label="CP 2024", username="", password="",
        section=hierarchy["database"].section, reporting_year=older, display=True,
    )  # fmt: skip
    Database.objects.create(
        ai_id=202502, db_id="y25", name="CP 2025", label="CP 2025", username="", password="",
        section=hierarchy["database"].section, reporting_year=empty, display=True,
    )  # fmt: skip
    from neurodb.facts.models import ActivityReportNew

    ActivityReportNew.objects.create(
        dbase=db, database_ai_id="202401", indicator_id="i", month_name="2024-03-01"
    )
    activityinfo.invalidate()
    side = _sidebar(client_viewer.get(reverse("reports:library")).content.decode())
    years = re.findall(
        r'<a class="nav-item-link nav-item-link--sub" href="/databases/\?year=(\d+)">(\d+)</a>', side
    )
    assert years == [("2026", "2026"), ("2024", "2024")]
    assert '<div class="sidebar__subtitle" id="nav-title-ai-years">Years</div>' in side
    assert '<span class="sidebar__archive-span">2024–2026</span>' in side


def test_without_any_activityinfo_year_the_archive_keeps_its_databases_page(client_viewer, reporting_year):
    side = _sidebar(client_viewer.get(reverse("reports:library")).content.decode())
    assert 'href="/databases/"><span>ActivityInfo databases</span>' in side  # no year to name
    assert "sidebar__subtitle" not in side


def test_no_archive_without_any_reporting_year(client_viewer):
    assert "sidebar__archive" not in client_viewer.get(reverse("reports:library")).content.decode()


def test_year_end_forecasts_only_while_some_exist(client_viewer, hierarchy):
    side = _sidebar(client_viewer.get(reverse("reports:library")).content.decode())
    assert "Year-end forecasts" not in side
    from neurodb.insights.models import IndicatorForecast

    IndicatorForecast.objects.create(
        master=hierarchy["master"], database=hierarchy["database"], year=2026, as_of_month=8, value_to_date=1,
        status=IndicatorForecast.Status.values[0], basis="test", computed_at=timezone.now(),
    )  # fmt: skip
    navigation.invalidate()
    side = _sidebar(client_viewer.get(reverse("reports:library")).content.decode())
    assert f'href="{reverse("insights:forecasts")}"><span>Year-end forecasts</span>' in side


def test_count_pills_say_what_they_count(client_viewer, hierarchy, section, reporting_year):
    side = _sidebar(client_viewer.get(reverse("reports:library")).content.decode())
    assert '<span class="count">1<span class="visually-hidden"> database</span></span>' in side
    assert '<span class="count">1<span class="visually-hidden"> report</span></span>' in side
    Database.objects.create(
        ai_id=202619, db_id="cp2", name="CP second", label="CP second", username="", password="",
        section=section, reporting_year=reporting_year, display=True,
    )  # fmt: skip
    navigation.invalidate()
    side = _sidebar(client_viewer.get(reverse("reports:library")).content.decode())
    assert side.count('<span class="count">2<span class="visually-hidden"> databases</span></span>') == 2
    assert not re.search(r'<span class="count">\d+</span>', side)  # never a bare number


# ------------------------------------------------------------------------------------- other audiences
def test_a_visitor_sees_open_resources_only_when_there_are_some(client, db, settings):
    settings.PUBLIC_PAGES = ["reports:library"]
    html = client.get(reverse("reports:library")).content.decode()
    side = _sidebar(html)
    assert "Open resources" in side and 'role="group" aria-labelledby="nav-title-open"' in side
    assert re.search(r'href="/library/" aria-current="page"', side)
    settings.PUBLIC_PAGES = []
    side = _sidebar(client.get("/no-such-page/").content.decode())
    assert "Open resources" not in side and "Sign in for dashboards" in side


def test_phone_names_the_account_and_the_year_buttons(client_viewer, reporting_year):
    html = client_viewer.get(reverse("reports:brief")).content.decode()
    user_menu = html.split('id="user-menu"', 1)[1].split("</button>", 1)[0]
    assert '<span class="visually-hidden d-md-none">Your account</span>' in user_menu
    year_menu = html.split('id="year-menu"', 1)[1].split("</button>", 1)[0]
    assert (
        '<span class="year-menu__label"><span class="visually-hidden">Year: </span>2026</span>' in year_menu
    )
    css = (TEMPLATES.parent / "static" / "css" / "app.css").read_text()
    phone = css.split("@media (max-width: 575.98px) {", 1)[1].split("\n}", 1)[0]
    assert "#year-menu span { display: none; }" not in css
    assert "#year-menu .year-menu__label {" in phone and "clip: rect(0, 0, 0, 0)" in phone


def test_the_menu_rules_are_in_app_css():
    css = (TEMPLATES.parent / "static" / "css" / "app.css").read_text()
    for selector in (
        ".nav-group {",
        ".nav-item-link--muted {",
        ".nav-badge.nav-badge--muted {",
        ".sidebar__archive {",
        ".sidebar__archive > summary {",
        ".sidebar__archive-span {",
        ".sidebar__subtitle {",
        ".kpi__how {",
        ".kpi--wide-sm {",
    ):
        assert selector in css, selector


# ------------------------------------------------------------------------- the remembered-state script
# base.html's sidebar script, run in node on a stand-in page: blocks with a summary, events, a storage.
SCRIPT_RUN = """
const [script, blocksJson, storedJson] = process.argv.slice(1);
const stored = JSON.parse(storedJson);
const writes = [];
const queue = [];  // "toggle" events wait for a task, as in a browser
function element(spec) {
  const listeners = {};
  const attrs = { ...spec.attrs };
  const el = {
    _open: !!spec.open,
    get open() { return this._open; },
    set open(value) {
      if (!!value === this._open) return;
      this._open = !!value;
      queue.push(() => (listeners.toggle || []).forEach((fn) => fn({ type: "toggle" })));
    },
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    hasAttribute: (name) => name in attrs,
    addEventListener: (type, fn) => (listeners[type] ||= []).push(fn),
    fire: (type, detail) => (listeners[type] || []).forEach((fn) => fn({ type, ...detail })),
  };
  const summary = {
    addEventListener: (type, fn) => (summary._l[type] ||= []).push(fn),
    _l: {},
    fire(type, detail) { (this._l[type] || []).forEach((fn) => fn({ type, ...detail })); },
  };
  el.summary = summary;
  el.querySelector = (selector) => (selector === "summary" ? summary : null);
  // a block drawn open fires "toggle" once as the page loads
  if (spec.open) queue.push(() => (listeners.toggle || []).forEach((fn) => fn({ type: "toggle" })));
  return el;
}
const blocks = JSON.parse(blocksJson).map(element);
globalThis.document = {
  querySelectorAll: (selector) => (selector === "#sidebar details[data-nav-key]" ? blocks : []),
};
globalThis.localStorage = {
  getItem: (key) => (key in stored ? stored[key] : null),
  setItem: (key, value) => { stored[key] = value; writes.push([key, value]); },
};
new Function(script)();
const settle = () => { while (queue.length) queue.shift()(); };
settle();
const out = { afterLoad: blocks.map((b) => b.open), writesOnLoad: writes.length };
// what the reader (or a script) then does: [block index, "pointer" | "key" | "script", key]
const act = JSON.parse(process.argv[4] || "[]");
for (const [index, how, key] of act) {
  const block = blocks[index];
  if (how === "pointer") block.summary.fire("pointerdown");
  if (how === "key") block.summary.fire("keydown", { key });
  block.open = !block.open;  // "script": a toggle no reader asked for
  settle();
}
out.after = blocks.map((b) => b.open);
out.writes = writes;
console.log(JSON.stringify(out));
"""


def _script() -> str:
    base = (TEMPLATES / "base.html").read_text()
    after_nav = base.split("</nav>", 1)[1]
    return after_nav[after_nav.index("<script nonce=") :].split(">", 1)[1].split("</script>", 1)[0]


def _run_script(blocks: list[dict], stored: dict, acts: list | None = None) -> dict:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", SCRIPT_RUN, _script(), json.dumps(blocks), json.dumps(stored),
         json.dumps(acts or [])],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


ARCHIVE_IN_USE = {"attrs": {"data-nav-key": "archive", "data-nav-default": "open"}}
ARCHIVE_ON_ITS_PAGE = {"attrs": {"data-nav-key": "archive", "data-nav-active": ""}, "open": True}
DATABASES = {"attrs": {"data-nav-key": "databases"}}


@needs_node
def test_the_sidebar_script_stores_nothing_on_load():
    """A block drawn open fires "toggle" as the page loads, and so does one the script opens by default: the
    script used to store "open" for both, so the archive stayed open for good once a reader had seen it open
    (spec check 3, #20). Now only a toggle after the reader's press or key is stored."""
    out = _run_script([ARCHIVE_ON_ITS_PAGE, ARCHIVE_IN_USE, DATABASES], {})
    assert out["afterLoad"] == [True, True, False]  # its page; open by default; nothing stored
    assert out["writesOnLoad"] == 0 and out["writes"] == []


@needs_node
def test_the_readers_choice_wins_over_the_default_and_is_remembered():
    # nothing stored: the default opens it; the reader closes it with a pointer: stored "closed"
    out = _run_script([ARCHIVE_IN_USE], {}, [[0, "pointer", None]])
    assert out["afterLoad"] == [True] and out["after"] == [False]
    assert out["writes"] == [["neurodb-nav:archive", "closed"]]
    # the next page: "closed" stored, so the default does not open it
    assert _run_script([ARCHIVE_IN_USE], {"neurodb-nav:archive": "closed"})["afterLoad"] == [False]
    # a block the reader opened from the keyboard (Enter or Space) is stored "open" and opens on later pages
    for key in ("Enter", " "):
        out = _run_script([DATABASES], {}, [[0, "key", key]])
        assert out["after"] == [True] and out["writes"] == [["neurodb-nav:databases", "open"]]
    assert _run_script([DATABASES], {"neurodb-nav:databases": "open"})["afterLoad"] == [True]
    # another key, or a toggle no reader asked for, is not stored
    assert _run_script([DATABASES], {}, [[0, "key", "Tab"]])["writes"] == []
    assert _run_script([DATABASES], {}, [[0, "script", None]])["writes"] == []
    # the block of the current page opens whatever is stored
    assert _run_script([ARCHIVE_ON_ITS_PAGE], {"neurodb-nav:archive": "closed"})["afterLoad"] == [True]


@needs_node
def test_once_archived_the_archive_closes_for_everyone_who_did_not_open_it():
    """While in use the archive opens by default; a reader who never touched it stored nothing, so once
    ActivityInfo is archived (no data-nav-default) it is closed for them."""
    first = _run_script([ARCHIVE_IN_USE], {})
    assert first["afterLoad"] == [True] and first["writes"] == []
    assert _run_script([DATABASES | {"attrs": {"data-nav-key": "archive"}}], {})["afterLoad"] == [False]
