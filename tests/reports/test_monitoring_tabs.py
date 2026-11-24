"""Partner monitoring's tab bar: Data quality, Program Performance and Map (the owner's decision of 21 Nov 2026).

* The *Grid* tab is merged into *Data quality*: the first tab, opened by default. It shows Data quality's figures
  and lists first (the parts ``quality-tiles`` and ``quality-lists``), then the grid (``summary`` and ``grid``),
  on one filter and one year. Its address is ``view=quality``; the bare address and the old ``view=grid`` open it
  too. It opens on this calendar year, as the grid did (the page's first view is unchanged); *All years* is in its
  year menu, and the figure trail and the Power BI comparison read the year it shows.
* The old address of the removed *Master indicators* view (``view=masters``: NeuroDB's own grouping of PD
  indicators, removed because the eTools Datamart has none) opens the bare page, and no regular page links to it.
* Program Performance (``view=table``) and the Map are unchanged.

On the world of tests/reports/test_pd_monitoring.py (``data``, with ``more`` of test_powerbi_views.py: an
Education indicator beside the two Child Protection ones), today frozen at 1 July 2026.
"""

# ruff: noqa: F401, F811 (the fixtures "data", "more" and "frozen" come from the modules that define them)
from __future__ import annotations

import html as html_module
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.datamart import comparison, monitoring, powerbi_results
from neurodb.datamart import trail_monitoring as tm
from tests.reports.test_pd_monitoring import TODAY, data
from tests.reports.test_powerbi_views import frozen, more
from tests.web.parts import get_part, placeholders, raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/partner-monitoring/"  # reports:pd_monitoring
SHOWN = ["Data quality", "Program Performance", "Map"]
MERGED = ["quality-tiles", "quality-lists", "summary", "grid"]  # Data quality's parts, then the grid's
TAB_BAR = re.compile(
    r'<div class="segmented[^"]*" role="group" aria-label="Partner monitoring views">(.*?)</div>', re.S
)
TAB_LINK = re.compile(r'<a href="([^"]*)"([^>]*)>(.*?)</a>', re.S)
TAGS = re.compile(r"<[^>]+>")
YEAR_MENU = re.compile(r'<select[^>]*id="f-year"[^>]*>(.*?)</select>', re.S)
SELECTED = re.compile(r'<option value="([^"]*)"\s+selected>')
# any address on a page (a link, a form, an HTMX request or the address it pushes)
ADDRESS = re.compile(r'\s(?:href|action|hx-get|data-hx-get|hx-push-url|data-hx-push-url)="([^"]*)"')
MASTERS_ADDRESS = re.compile(r"[?&]view=masters(?:&|$)")


@pytest.fixture
def world(more, frozen):
    return more


def _tabs(html: str) -> list[tuple[str, str, bool]]:
    """The tab bar's links: (label, address, current)."""
    bars = TAB_BAR.findall(html)
    assert len(bars) == 1, bars
    return [
        (" ".join(TAGS.sub("", label).split()), html_module.unescape(href), "aria-current" in attrs)
        for href, attrs, label in TAB_LINK.findall(bars[0])
    ]


def _query(url: str) -> dict[str, list[str]]:
    """The query of an address already read out of its HTML (by the parser, or by :func:`_tabs`)."""
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _year_shown(html: str) -> list[str]:
    """The value the filter bar's year menu has selected."""
    menus = YEAR_MENU.findall(html)
    assert len(menus) == 1, menus
    return SELECTED.findall(menus[0])


def _validator() -> Client:
    user = User.objects.create_user(username="checker", email="checker@example.org", password="x-pass-123456")
    user.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=VALIDATORS))
    client = Client()
    client.force_login(user)
    return client


# --------------------------------------------------------------------------------------------- the tab bar
@pytest.mark.parametrize(
    ("params", "current"),
    [
        ({}, "Data quality"),
        ({"view": "grid"}, "Data quality"),
        ({"view": "quality"}, "Data quality"),
        ({"view": "table"}, "Program Performance"),
    ],
)
def test_the_tab_bar_shows_data_quality_program_performance_and_map(client_viewer, world, params, current):
    response = raw_get(client_viewer, PAGE, params)
    assert response.status_code == 200
    tabs = _tabs(text(response))
    assert [label for label, _, _ in tabs] == SHOWN
    quality, table, on_map = (href for _, href, _ in tabs)
    assert _query(quality)["view"] == ["quality"] and _query(table)["view"] == ["table"]
    assert urlsplit(on_map).path == reverse("reports:pd_monitoring_map")
    assert [label for label, _, on in tabs if on] == ([current] if current else [])
    assert list(response.context["tab_urls"]) == ["quality", "table", "map"]


def test_the_map_draws_the_same_tab_bar(client_viewer, world):
    response = raw_get(client_viewer, reverse("reports:pd_monitoring_map"))
    tabs = _tabs(text(response))
    assert [label for label, _, _ in tabs] == SHOWN
    assert [label for label, _, on in tabs if on] == ["Map"]
    assert list(response.context["tab_urls"]) == ["quality", "table", "map"]


def test_a_filter_applied_draws_the_same_tab_bar(client_viewer, world):
    applied = raw_get(client_viewer, PAGE, {"section": "Education", "year": "2026"})
    assert [label for label, _, _ in _tabs(text(applied))] == SHOWN


# ------------------------------------------------------------------------------------------- the merged tab
@pytest.mark.parametrize("params", [{}, {"view": "grid"}, {"view": "quality"}])
def test_the_merged_tab_draws_data_quality_then_the_grid(client_viewer, world, params):
    response = raw_get(client_viewer, PAGE, params)
    assert response.status_code == 200 and response.context["view"] == "quality"
    assert list(placeholders(text(response))) == MERGED
    # put together, Data quality's tiles and lists come first, then the grid's tiles, charts and list
    html = text(client_viewer.get(PAGE, params))
    order = [
        html.index("Active PDs"),
        html.index("What the counts mean"),
        html.index('id="pm-grid-heading"'),  # the grid's own heading, between the two
        html.index("Tracking status"),
        html.index('id="pd-indicators"'),
    ]
    assert order == sorted(order), order
    assert html.count('id="pm-summary-data"') == 1  # the one chart JSON, in the grid's summary part
    assert html.count('aria-label="Partner monitoring filters"') == 1  # one filter bar for the tab


def test_each_part_of_the_merged_tab_answers_under_its_own_name(client_viewer, world):
    for params in ("", "?view=grid", "?view=quality"):
        for name in MERGED:
            response = get_part(client_viewer, PAGE + params, name)
            assert response["X-NeuroDB-Part"] == name


def test_a_filter_change_answers_the_four_placeholders(client_viewer, world):
    response = raw_get(
        client_viewer,
        PAGE,
        {"section": "Education"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="monitoring-grid",
    )
    found = placeholders(text(response))
    assert list(found) == MERGED
    assert all(_query(url)["section"] == ["Education"] for url in found.values())


# ------------------------------------------------------------------------------------------------ one year
@pytest.mark.parametrize("params", [{}, {"view": "grid"}, {"view": "quality"}])
def test_the_merged_tab_opens_on_this_year_for_both_parts(client_viewer, world, params):
    """One year for Data quality and the grid: this calendar year by default, as the grid opened (the page's
    first view is the one it had), and the filter bar shows it."""
    response = raw_get(client_viewer, PAGE, params)
    html = text(response)
    assert {name: _query(url)["year"] for name, url in placeholders(html).items()} == {
        name: ["2026"] for name in MERGED
    }
    assert _year_shown(html) == ["2026"]
    address = PAGE + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    years = {name: get_part(client_viewer, address, name).context["filters"].year for name in MERGED}
    assert years == dict.fromkeys(MERGED, 2026)


def test_every_year_shows_both_parts_for_every_year(client_viewer, world):
    response = raw_get(client_viewer, PAGE, {"view": "quality", "year": "all"})
    html = text(response)
    assert {name: _query(url)["year"] for name, url in placeholders(html).items()} == {
        name: ["all"] for name in MERGED
    }
    assert _year_shown(html) == ["all"]
    tiles = get_part(client_viewer, PAGE + "?view=quality&year=all", "quality-tiles")
    grid = get_part(client_viewer, PAGE + "?view=quality&year=all", "grid")
    assert tiles.context["filters"].year is None and grid.context["filters"].year is None
    assert "With Reached in" not in text(tiles) and "One column per year" in text(grid)


def test_a_year_chosen_is_the_year_of_both_parts(client_viewer, world):
    response = raw_get(client_viewer, PAGE, {"year": "2025"})
    assert {name: _query(url)["year"] for name, url in placeholders(text(response)).items()} == {
        name: ["2025"] for name in MERGED
    }
    assert _year_shown(text(response)) == ["2025"]


def test_program_performance_still_opens_on_every_year(client_viewer, world):
    response = raw_get(client_viewer, PAGE, {"view": "table"})
    found = placeholders(text(response))
    assert list(found) == ["sections", "table"]
    assert all(_query(url)["year"] == ["all"] for url in found.values())
    # the tabs keep the year shown both ways
    assert _query(response.context["tab_urls"]["quality"])["year"] == ["all"]
    merged = raw_get(client_viewer, PAGE)
    assert _query(merged.context["tab_urls"]["table"])["year"] == ["2026"]


# ------------------------------------------------------------------- the trail, the comparison, downloads
def test_the_trail_and_the_comparison_read_the_year_the_merged_tab_shows(world, roles):
    validator = _validator()
    page = raw_get(validator, PAGE, {"view": "quality"})
    trail_url = page.context["check_trail"]
    assert _query(trail_url)["year"] == ["2026"]
    trail = validator.get(trail_url)
    assert trail.status_code == 200
    compare = dict(page.context["check_compare"]["query"])
    assert compare["year"] == "2026"
    # without a year, the trail and the comparison sheet read the year the page opens on, whatever the view
    # word of the address
    for params in ({}, {"view": "grid"}, {"view": "quality"}):
        assert tm.filters_of(params).year == TODAY.year, params
        found = comparison.read("partner-monitoring", dict(params), TODAY)
        assert found.filters.year == TODAY.year, params
    assert tm.filters_of({"view": "table"}).year is None
    assert comparison.read("partner-monitoring", {"view": "table"}, TODAY).filters.year is None


def test_the_trail_s_links_open_the_merged_tab(world):
    found = comparison.read("partner-monitoring", {"year": "2026", "section": ""}, TODAY)
    for key in ("dq.pds", "pm.indicators"):
        link = comparison.trail_link(found, "", key)
        assert _query(link)["year"] == ["2026"], key
    assert powerbi_results.NEURODB_PAGE["dq_list"] == ("reports:pd_monitoring", {"view": "quality"})


def test_the_figure_trail_opens_from_each_address_of_the_merged_tab(world, roles):
    validator = _validator()
    url = reverse("reports:check_figures", args=["partner-monitoring"])
    for params in ({}, {"view": "grid"}, {"view": "quality"}):
        response = validator.get(url, {**params, "year": "2026", "section": ""})
        assert response.status_code == 200, params
        back = response.context["page_url"]
        assert urlsplit(back).path == PAGE
        assert raw_get(validator, back).context["view"] == "quality"
        rows = validator.get(
            reverse("reports:check_rows", args=["partner-monitoring"]),
            {**params, "year": "2026", "section": "", "what": "kept"},
        )
        assert rows.status_code == 200, params


def test_the_downloads_of_the_merged_tab_still_work(client_viewer, world):
    html = text(client_viewer.get(PAGE))
    for table in ("pd-indicators", "dq-targets", "dq-reached"):
        assert f'data-table-csv data-table-target="#{table}"' in html, table
    assert 'data-filename="partner-monitoring"' in html
    # the page's export, from its address, is Program Performance's file of the filter, as from the grid
    for params in ({}, {"view": "grid"}, {"view": "quality"}):
        response = client_viewer.get(PAGE, {**params, "export": "csv"})
        assert response.status_code == 200, params
        assert "program-performance-2026" in response["Content-Disposition"], params


# ------------------------------------------------------------------------- the removed Master indicators
def test_the_old_master_indicators_address_opens_the_merged_tab(client_viewer, world):
    """The view was removed (eTools has no master indicators): its address, a part of it and its export open the
    bare page, which is the merged tab with its bar (tests/reports/test_masters_removed.py has the details)."""
    for params in (
        {"view": "masters"},
        {"view": "masters", "year": "2026", "_part": "masters-table"},
        {"view": "masters", "year": "2026", "export": "csv"},
    ):
        response = raw_get(client_viewer, PAGE, params)
        assert response.status_code == 302 and response["Location"] == PAGE, params
        assert not response.has_header("Content-Disposition"), params
    landed = raw_get(client_viewer, PAGE)
    assert landed.context["view"] == "quality" and list(placeholders(text(landed))) == MERGED
    assert [label for label, _, on in _tabs(text(landed)) if on] == ["Data quality"]


def _indicator_url(world) -> str:
    """The page of the girls' indicator, as the grid links it."""
    rows = monitoring.indicators(monitoring.Filters(year=TODAY.year), TODAY)
    return next(r.url for r in rows if r.title == world["girls"])


def _regular_pages(world) -> list[tuple[str, dict]]:
    pd, partner = world["pd"], world["partner"]
    return [
        (reverse("reports:overview"), {}),
        (PAGE, {}),
        (PAGE, {"view": "grid"}),
        (PAGE, {"view": "quality", "year": "all"}),
        (PAGE, {"view": "table"}),
        (reverse("reports:pd_monitoring_map"), {}),
        (_indicator_url(world), {"year": "2026"}),
        (reverse("reports:partner_reporting"), {}),
        (reverse("reports:programmes"), {"tab": "indicators"}),
        (reverse("reports:programme_detail", args=[pd.pk]), {}),
        (reverse("reports:partner_profile", args=[partner.pk]), {}),
        (reverse("reports:data_health"), {}),
        (reverse("reports:brief"), {}),
        (reverse("reports:daily_review"), {}),
        *((reverse("help:page", args=[key]), {}) for key in HELP_PAGES),
    ]


HELP_PAGES = ("partner-progress", "checking-figures", "overview", "home", "glossary", "exports")


def test_no_regular_page_links_to_the_master_indicators(client_viewer, world):
    for url, params in _regular_pages(world):
        response = client_viewer.get(url, params)
        assert response.status_code == 200, (url, params)
        html = text(response)
        found = [
            address
            for address in (html_module.unescape(a) for a in ADDRESS.findall(html))
            if MASTERS_ADDRESS.search(address)
        ]
        assert found == [], (url, params, found)
        assert "view=masters" not in html, url  # nor in a data attribute, a script or the page's text


# ------------------------------------------------------------------------------------------- the guide
def test_the_help_guide_describes_the_three_tabs_and_no_master_indicators():
    from neurodb.help import guide

    folder = Path(guide.__file__).parent / "guide"
    every = {path.name: path.read_text(encoding="utf-8") for path in folder.glob("*.md")}
    for name, words in every.items():
        assert "view=masters" not in words and "Grid tab" not in words, name
    page = every["partner-progress.md"]
    section = page.split("## Partner monitoring", 1)[1].split("\n## ", 1)[0]
    assert "three tabs" in section
    bullets = [line[2:] for line in section.splitlines() if line.startswith("- **")]
    assert [b.split("**")[1] for b in bullets] == ["Data quality", "Program Performance", "Map"]
    data_quality = section.split("- **Data quality**", 1)[1].split("\n- **", 1)[0]
    assert "the grid" in data_quality and "same year" in data_quality  # the grid is below its lists
    assert "**Grid**" not in section and "**Master indicators**" not in section
    # NeuroDB's own grouping of PD indicators was removed (eTools has none): the guide no longer describes it
    assert "## Master indicators" not in page and "#master-indicators" not in "".join(every.values())
