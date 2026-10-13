"""Partner monitoring in parts (SPEC7 §3.3-3.4; STAGE3 group B).

The page answers its frame at once (the view tabs, the filter bar with its options stored for every worker, the
"your section" note) with, in ``#monitoring-grid``, a placeholder for each data block of the view: the grid's
``summary`` (tiles and charts) and ``grid`` (the page of indicators), Program Performance's ``sections`` (the
section tiles) and ``table``, Data quality's ``quality-tiles`` and ``quality-lists``. Every part reads the
indicators of its filter computed once for every worker (``monitoring.stored_indicators``: plain rows, keyed on
the filter, the day and the eTools jobs' latest runs); the map's API stores its places the same way. These tests
check the frame without any figure work, each part, the part queries, the results shell of a filter change, that
the page put together from its parts is the page drawn at once (``_eager=1``), that a stored value never shows
one person's view to another, and that a sync or an edit in the admin shows at once.

On the world of tests/reports/test_pd_monitoring.py (``data``, with ``more`` of test_powerbi_views.py: an
Education indicator beside the two Child Protection ones), today frozen at 1 July 2026.
"""

# ruff: noqa: F401, F811 (the fixtures "data" and "more" come from the modules that define them)
from __future__ import annotations

import gettext
import html as html_module
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import caches
from django.http import QueryDict
from django.test import Client
from django.urls import reverse
from django.utils import translation

from neurodb.accounts.models import Section, User
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.datamart import keys, monitoring
from neurodb.datamart import models as dm
from neurodb.donors.models import DonorAccount
from neurodb.geo.models import Location, LocationType
from neurodb.reports import views
from tests.reports.test_pd_monitoring import TODAY, data, datamart_synced
from tests.reports.test_powerbi_views import CLASSROOMS, frozen, more
from tests.web.parts import (
    HX,
    assemble,
    eager,
    executable,
    get_part,
    normalise,
    placeholder_attributes,
    placeholders,
    raw_get,
    text,
)

pytestmark = pytest.mark.django_db

PAGE = "/partner-monitoring/"  # reports:pd_monitoring
VIEWS = {  # the placeholders of each view, in page order
    "grid": ("summary", "grid"),
    "table": ("sections", "table"),
    "quality": ("quality-tiles", "quality-lists"),
}
GROUPS = {"summary": "pm", "grid": "pm", "sections": "pm", "table": "pm", "quality-tiles": "pm",
          "quality-lists": "pm"}  # fmt: skip
SUMMARY_JSON = "pm-summary-data"  # the one chart JSON of the page, in the grid's summary part
RESULTS = "monitoring-grid"  # the results area a filter, sort or section change reloads
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame warm (its filter options and
# navigation stored, as after the first visit of a page) is 9: the request's floor (session, user, donor check, user
# role, the sidebar's sections and years) and the moment of the eTools figures (the latest run of each eTools job,
# the indicator flags); cold it also computes the filter options (13 queries) and the navigation: 23. The results
# shell of a filter change is 5 (no page around it). Drawn inline (the indicators stored), the grid's frame adds the
# summary's overdue reports: 10. Each part with nothing stored computes the indicators of its filter (the PDs, the
# indicator rows, their identities, the report rows, PRP's totals, the first period held): 11, the summary 12 with
# its overdue reports, the section tiles 12 (the filter without its sections); a part of the group reading what the
# first one stored: 5, the summary 6.
FRAME_QUERIES_COLD = 25
FRAME_QUERIES_WARM = 11
FRAME_QUERIES_INLINE = 12
RESULTS_QUERIES = 7
PART_QUERIES = {
    "summary": 14,
    "grid": 13,
    "sections": 14,
    "table": 13,
    "quality-tiles": 13,
    "quality-lists": 13,
}
PART_QUERIES_STORED = 8


@pytest.fixture
def world(request, frozen):
    """``more`` (the partner monitoring fixture with an Education indicator), today frozen at 1 July 2026."""
    return request.getfixturevalue("more")


@pytest.fixture
def sectioned(viewer):
    """The viewer works in Child Protection: the page's first load shows that section."""
    viewer.section = Section.objects.create(name="Child protection", code="CP")
    viewer.save()
    return viewer


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _url(view: str, **params) -> str:
    query = ({"view": view} if view != "grid" else {}) | params
    return PAGE + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def _json_blocks(html: str) -> dict[str, object]:
    """The page's JSON blocks, its frame's asset addresses aside (base.html)."""
    found = [(name, body) for name, body in JSON_SCRIPT.findall(html) if name != "asset-urls"]
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a chart JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the page raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    for name in ("indicators", "_attach_reached", "as_rows", "summary", "data_quality", "grouped", "sorted_rows",
                 "map_points"):  # fmt: skip
        monkeypatch.setattr(monitoring, name, refuse)
    monkeypatch.setattr(views, "_paginate", refuse)


def _builds(monkeypatch) -> Counter:
    """Count the computations of the stored indicators (``monitoring.as_rows`` runs once per computation)."""
    calls: Counter = Counter()
    real = monitoring.as_rows

    def counted(found):
        calls["rows"] += 1
        return real(found)

    monkeypatch.setattr(monitoring, "as_rows", counted)
    return calls


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("reports:pd_monitoring") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
@pytest.mark.parametrize("view", VIEWS)
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, world, monkeypatch, view):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(_url(view))
    assert response.status_code == 200
    html = text(response)
    assert "<title>Partner monitoring" in html and 'aria-label="Partner monitoring filters"' in html
    assert 'aria-label="Partner monitoring views"' in html  # the view tabs
    assert f'<div id="{RESULTS}">' in html  # the results area, not aria-live (it receives parts)
    assert list(placeholders(html)) == list(VIEWS[view])
    assert f'id="{SUMMARY_JSON}"' not in html and 'id="monitoring-chart-data"' not in html
    for figure in (
        "Tracking status",
        'id="pd-indicators"',
        'id="program-performance"',
        "What the counts mean",
    ):
        assert figure not in html, figure
    # the filter bar's choices are there (the frame's own, stored for every worker)
    assert '<option value="Child Protection"' in html and '<option value="Education"' in html


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_filter(admin_user, sectioned, world):
    viewer, admin = _signed_in(sectioned), _signed_in(admin_user)
    stamp = keys.fingerprint()
    # a first load: the viewer's own section written out, the grid's year (this year) always
    found = placeholder_attributes(text(viewer.get(PAGE)))
    assert list(found) == ["summary", "grid"]
    for name, attrs in found.items():
        assert urlsplit(attrs["hx-get"]).path == PAGE
        assert _query(attrs["hx-get"]) == {
            "section": ["Child Protection"],
            "year": ["2026"],
            "as_of": [stamp],
            "_part": [name],
        }
        assert attrs["data-part-group"] == GROUPS[name] and attrs["data-part-priority"] == "high"
    assert found["summary"]["class"] == "part part--tiles" and found["grid"]["class"] == "part part--table"
    # every section for an administrator (no section of their own), written as "section="
    found = placeholders(text(admin.get(PAGE)))
    assert _query(found["grid"]) == {"section": [""], "year": ["2026"], "as_of": [stamp], "_part": ["grid"]}
    # Program Performance and Data quality open on every year; a filter travels, a key the page reads not does not
    # matter (it travels as typed)
    found = placeholders(text(viewer.get(_url("table", section="Education", sort="partner"))))
    assert _query(found["table"]) == {
        "view": ["table"],
        "section": ["Education"],
        "sort": ["partner"],
        "year": ["all"],
        "as_of": [stamp],
        "_part": ["table"],
    }
    found = placeholder_attributes(text(viewer.get(_url("quality", year="2026"))))
    assert _query(found["quality-lists"]["hx-get"])["year"] == ["2026"]
    assert found["quality-tiles"]["data-part-priority"] == "high"
    assert found["quality-lists"]["data-part-priority"] == "normal"


# Each placeholder's room, as measured on the x20 parity world (reports/partials/_monitoring_results.html): --part-h
# at 1366 px, --part-h-md at 900 px (the grids stacked), --part-h-sm at 390 px; and its grey blocks (the tiles a row
# draws, or the lines of a table).
ROOM = {
    "summary": ({"--part-h": "584px", "--part-h-md": "882px", "--part-h-sm": "1212px"}, "tile", 7),
    "grid": (
        {"--part-h": "876px", "--part-h-md": "896px", "--part-h-sm": "1153px", "--part-rows": "20"},
        "row",
        20,
    ),
    "sections": ({"--part-h": "71px", "--part-h-md": "71px", "--part-h-sm": "202px"}, "tile", 5),
    "table": (
        {"--part-h": "789px", "--part-h-md": "809px", "--part-h-sm": "1033px", "--part-rows": "20"},
        "row",
        20,
    ),
    "quality-tiles": ({"--part-h": "292px", "--part-h-sm": "517px"}, "tile", 8),
    "quality-lists": (
        {"--part-h": "1286px", "--part-h-md": "1506px", "--part-h-sm": "2058px", "--part-rows": "20"},
        "row",
        20,
    ),
}


def _style(attrs: dict[str, str]) -> dict[str, str]:
    return dict(item.split(": ", 1) for item in attrs.get("style", "").split("; ") if item)


def _grey_blocks(html: str, name: str, block: str) -> int:
    """The grey blocks of a kind (tile, row) in part ``name``'s placeholder."""
    start = html.index(f'id="part-{name}"')
    return html[start : html.index('class="part__slow', start)].count(f"skeleton__block--{block}")


@pytest.mark.shell
def test_each_placeholder_keeps_the_room_of_its_part(admin_user, viewer, world):
    """The placeholders hold the height measured for their part, at three widths (the same for both users), and
    grey blocks shaped like it: the grid's seven tiles, the sections' tiles, the quality tiles, a table's lines."""
    for client in (_signed_in(viewer), _signed_in(admin_user)):
        for view, names in VIEWS.items():
            html = text(client.get(_url(view)))
            found = placeholder_attributes(html)
            assert list(found) == list(names)
            for name in names:
                room, block, count = ROOM[name]
                assert _style(found[name]) == room, name
                assert _grey_blocks(html, name, block) == count, name
            # the results shell of a filter change draws the same placeholders
            shell = placeholder_attributes(
                text(
                    raw_get(
                        client,
                        _url(view, section="Education"),
                        HTTP_HX_REQUEST="true",
                        HTTP_HX_TARGET=RESULTS,
                    )
                )
            )
            assert shell and all(_style(attrs) == ROOM[name][0] for name, attrs in shell.items())


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(
    client_viewer, sectioned, world, django_assert_max_num_queries
):
    with django_assert_max_num_queries(FRAME_QUERIES_COLD):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and list(placeholders(text(response))) == list(VIEWS["grid"])
    for view in VIEWS:
        with django_assert_max_num_queries(FRAME_QUERIES_WARM):
            response = client_viewer.get(_url(view, section="Education"))
        assert response.status_code == 200 and list(placeholders(text(response))) == list(VIEWS[view])


@pytest.mark.shell
def test_the_frame_reads_its_filter_options_stored(client_viewer, world, monkeypatch):
    """The filter options are computed once for every worker (ten minutes), and again as soon as an eTools sync
    run ends."""
    calls: Counter = Counter()
    real = monitoring.filter_options

    def counted(filters):
        calls["options"] += 1
        return real(filters)

    monkeypatch.setattr(monitoring, "filter_options", counted)
    for _ in range(2):
        assert client_viewer.get(_url("table")).status_code == 200
        compute.reset_memo()
    assert calls["options"] == 1
    datamart_synced()
    assert client_viewer.get(_url("table")).status_code == 200 and calls["options"] == 2


# ----------------------------------------------------------------------------------------- the results shell
@pytest.mark.parametrize("view", VIEWS)
def test_a_filter_change_answers_the_results_area_with_its_placeholders(
    client_viewer, world, view, monkeypatch, django_assert_max_num_queries
):
    """A filter, sort or section change (aimed at #monitoring-grid) answers the area with new placeholders, no
    figure, at once; the parts then come with the new filter."""
    get_part(client_viewer, _url(view), VIEWS[view][0])  # the filter options and the navigation stored
    drawing = {"sorted_rows": monitoring.sorted_rows, "_paginate": views._paginate}
    _heavy_services_refuse(monkeypatch)
    # Program Performance's two parts read the filter without its sections, which the first request stored: both
    # drawn inline (inline when ready), sorting and paging the stored rows, with nothing computed
    inline = list(VIEWS["table"]) if view == "table" else []
    if inline:
        monkeypatch.setattr(monitoring, "sorted_rows", drawing["sorted_rows"])
        monkeypatch.setattr(views, "_paginate", drawing["_paginate"])
    with django_assert_max_num_queries(RESULTS_QUERIES):
        response = raw_get(
            client_viewer, _url(view, section="Education"), HTTP_HX_REQUEST="true", HTTP_HX_TARGET=RESULTS
        )
    html = text(response)
    assert response.status_code == 200 and "<html" not in html and 'id="sidebar"' not in html
    found = placeholders(html)
    assert list(found) == [name for name in VIEWS[view] if name not in inline]
    assert all(f'data-part-loaded="{name}"' in html for name in inline)
    assert all(_query(url)["section"] == ["Education"] for url in found.values())
    monkeypatch.undo()
    if found:  # the parts then come with the new filter
        response = client_viewer.get(found[VIEWS[view][-1]], **HX)
        assert response.status_code == 200
        html = text(response)
    assert CLASSROOMS in html and world["girls"] not in html


def test_any_other_htmx_request_answers_the_results_area_with_its_figures(client_viewer, world):
    """Not one of the page's own requests (none sends one): the results area with its figures, as before the
    page loaded in parts."""
    response = raw_get(client_viewer, _url("grid", year="2026", status="off_track"), HTTP_HX_REQUEST="true")
    html = text(response)
    assert response.status_code == 200 and "<html" not in html and placeholders(html) == {}
    assert world["boys"] in html and world["girls"] not in html and f'id="{SUMMARY_JSON}"' in html


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize(("view", "name"), [(view, name) for view, names in VIEWS.items() for name in names])
def test_each_part_answers_its_wrapper(client_viewer, world, view, name):
    response = get_part(client_viewer, _url(view), name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "summary": ("Tracking status", "Indicators by section", "Overdue reports"),
        "grid": ('id="pd-indicators"', world["girls"], "The months are on the right"),
        "sections": ("Every section", "Child Protection", "Education"),
        "table": ('id="program-performance"', CLASSROOMS, "Excel of the filter"),
        "quality-tiles": ("Active PDs", "Without a target"),
        "quality-lists": ("Indicators with missing targets", "What the counts mean", "Partial history"),
    }[name]
    for figure in expected:
        assert figure in html, figure
    assert (f'id="{SUMMARY_JSON}"' in html) == (name == "summary")  # the one chart JSON, in its own part
    assert all(
        f'data-source="{SUMMARY_JSON}"' in chart for chart in re.findall(r"<div[^>]*data-chart=[^>]*>", html)
    )
    assert executable(html) == []  # nothing that would run once swapped in (SPEC7 §1.3 rule 3)


@pytest.mark.parametrize(("view", "name"), [(view, name) for view, names in VIEWS.items() for name in names])
def test_each_part_stays_within_its_query_ceiling(
    client_viewer, world, view, name, django_assert_max_num_queries
):
    address = placeholders(text(raw_get(client_viewer, _url(view))))[name]
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)
    others = [n for n in VIEWS[view] if n != name and GROUPS[n] == GROUPS[name]]
    if not others:
        return
    # the group's other part, after this one stored the indicators, only reads them
    other = others[0]
    with django_assert_max_num_queries(PART_QUERIES_STORED):
        response = client_viewer.get(address.replace(f"_part={name}", f"_part={other}"), **HX)
    assert response.status_code == 200


@pytest.mark.parametrize("name", [name for names in VIEWS.values() for name in names])
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    view = next(view for view, names in VIEWS.items() if name in names)
    response = client.get(_url(view, section="", year="2026") + f"&_part={name}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?section=&year=2026&_part=grid", **HX)
    assert response.status_code == 200 and world["girls"] not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


def test_an_unknown_part_is_a_404(client_viewer, world):
    response = client_viewer.get(f"{PAGE}?section=&year=2026&_part=table", **HX)  # the grid has no table
    assert response.status_code == 404


def test_the_parts_draw_the_figures_of_the_filter(client_viewer, world):
    """The assembled page and the eager page draw the same part templates, so their equivalence cannot catch a
    wrong figure: the figures themselves are checked here, against monitoring.indicators."""
    summary = get_part(client_viewer, _url("grid", year="2026", section=""), "summary").context["data"]
    assert (summary["indicators"], summary["status_counts"]["off_track"], summary["programme_documents"]) == (
        3,
        1,
        1,
    )
    grid = get_part(client_viewer, _url("grid", year="2026", section=""), "grid").context
    girls = next(r for r in grid["rows"] if r.title == world["girls"])
    assert (girls.reached, girls.months, girls.tracking) == (550.0, {3: 300.0, 6: 250.0}, "on_track")
    assert girls.pd.number == "LEB/PD1" and girls.partner.name == "Amel Association"
    tiles = get_part(client_viewer, _url("table"), "sections").context["section_tiles"]
    assert {t["name"]: t["count"] for t in tiles} == {"Child Protection": 2, "Education": 1}
    quality = get_part(client_viewer, _url("quality"), "quality-lists").context["quality"]
    assert (quality["indicators"], quality["without_target"]) == (3, 1)
    assert [i.title for i in quality["missing_target"]] == [CLASSROOMS]


# ------------------------------------------------------------------------------------- one build, stored
def test_the_stored_rows_are_the_indicators_as_plain_values(world):
    """What is stored for every worker: each indicator's fields as the pages draw them, its programme document
    as plain values (a PCA instance is never stored), the same figures as monitoring.indicators."""
    filters = monitoring.Filters(year=2026)
    found = monitoring.indicators(filters, TODAY)
    stored = monitoring.stored_indicators(filters, TODAY)
    assert [r.ident for r in stored] == [i.ident for i in found]
    for row, indicator in zip(stored, found, strict=True):
        for name in ("key", "title", "reached", "reached_in_year", "tracking", "tracking_label", "achieved",
                     "achieved_in_year", "additive", "method_label", "notes", "months", "years", "url",
                     "awaiting", "mismatch", "partial", "as_of", "report_status", "partner_status"):  # fmt: skip
            assert getattr(row, name) == getattr(indicator, name), name
        assert (row.pd.id, row.pd.number, row.pd.status, row.pd.start) == (
            indicator.pd.id,
            indicator.pd.number,
            indicator.pd.status,
            indicator.pd.start,
        )
        assert row.partner.name == indicator.partner.name
    compute._check_plain(stored)  # plain data: no model instance, no QuerySet


def test_the_parts_of_a_view_read_one_build(client_viewer, world, monkeypatch):
    calls = _builds(monkeypatch)
    page = client_viewer.get(_url("grid", year="2026"))
    assert set(page.parts) == {"summary", "grid"} and calls["rows"] == 1
    calls.clear()
    client_viewer.get(
        _url("quality", year="2026", section="")
    )  # Data quality reads the grid's filter: stored
    assert calls["rows"] == 0


def test_program_performance_reads_the_computation_of_its_section_tiles(client_viewer, world, monkeypatch):
    """Program Performance's two parts read one computation, in one group: the filter without its sections (the
    tiles count every section), the table keeping the chosen ones, as the page read them before it loaded in
    parts. Under a section filter the indicators are computed once, not once per part, and the table holds the
    same indicators with the same figures as the filter's own computation; another section reads what is
    stored."""
    calls = _builds(monkeypatch)
    page = client_viewer.get(_url("table", section="Child+Protection"))
    assert set(page.parts) == {"sections", "table"} and calls["rows"] == 1
    tiles = {t["name"]: (t["count"], t["current"]) for t in page.parts["sections"].context["section_tiles"]}
    assert tiles == {"Child Protection": (2, True), "Education": (1, False)}
    rows = page.parts["table"].context["rows"]
    own = monitoring.indicators(
        monitoring.Filters.from_params(QueryDict("section=Child+Protection"), default_year=False), TODAY
    )
    assert len(rows) == len(own) == 2 and {r.section for r in rows} == {"Child Protection"}
    figures = ("reached", "reached_in_year", "achieved", "tracking", "target", "awaiting")
    assert {r.ident: tuple(getattr(r, f) for f in figures) for r in rows} == {
        i.ident: tuple(getattr(i, f) for f in figures) for i in own
    }
    calls.clear()
    response = client_viewer.get(_url("table", section="Education"))  # both parts drawn inline: stored
    assert placeholders(text(response)) == {} and calls["rows"] == 0
    assert [r.title for r in response.context["rows"]] == [CLASSROOMS]


def test_the_parts_read_the_build_of_the_frame_s_moment_though_a_sync_ended_between(
    client, admin_user, world
):
    """The browser asks for ``grid`` once ``summary`` is in. An eTools sync run that ends between the two moves
    the key: ``grid`` still reads the build ``summary`` read, found from the moment its address carries
    (``as_of``), so the page never shows the figures of two moments side by side. The next page shows the
    figures of the moment."""
    client.force_login(admin_user)
    address = placeholders(text(raw_get(client, _url("grid", year="2026"))))
    first = client.get(address["summary"], **HX)
    assert first.context["data"]["indicators"] == 3
    dm.PDIndicator.objects.filter(title=CLASSROOMS).delete()
    datamart_synced()
    second = client.get(address["grid"], **HX)
    assert len(second.context["rows"]) == 3  # the build of the frame's moment
    assert len(get_part(client, _url("grid", year="2026"), "grid").context["rows"]) == 2  # the next page


def test_a_stored_build_is_drawn_in_the_frame(
    client_viewer, sectioned, world, monkeypatch, django_assert_max_num_queries
):
    """Inline when ready: once the group's leader has stored the indicators, the frame draws both parts itself,
    so a warm page is one request; another filter is asked for."""
    get_part(client_viewer, PAGE, "summary")  # the leader computes and stores the viewer's section

    def refuse(found):
        raise AssertionError("the stored build was computed again")

    monkeypatch.setattr(monitoring, "as_rows", refuse)
    with django_assert_max_num_queries(FRAME_QUERIES_INLINE):
        html = text(raw_get(client_viewer, PAGE))
    assert placeholders(html) == {}
    for name in VIEWS["grid"]:
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert world["girls"] in html and f'id="{SUMMARY_JSON}"' in html
    assert list(placeholders(text(raw_get(client_viewer, _url("grid", section=""))))) == list(VIEWS["grid"])


def test_a_stored_build_never_answers_another_filter(client_viewer, world):
    def count(**params) -> int:
        return get_part(client_viewer, _url("grid", **params), "summary").context["data"]["indicators"]

    assert count(year="2026", section="") == 3
    assert count(year="2026", section="Education") == 1
    assert count(year="2026", section="Child+Protection") == 2
    assert count(year="2026", section="Child_Protection") == 0  # spelt another way: its own key
    assert count(year="2026", section="", status="off_track") == 1
    assert count(year="2026", section="", report_type="HR") == 1  # the girls' monthly reports


def test_two_filters_never_share_a_key():
    base = monitoring.Filters()
    changed = [
        monitoring.Filters(sections=["Education"]),
        monitoring.Filters(sections=["Child_Protection"]),
        monitoring.Filters(sections=["Child Protection", "Education"]),
        monitoring.Filters(sections=["Child Protection,Education"]),
        monitoring.Filters(partners=["7"]),
        monitoring.Filters(pds=["LEB/PD1"]),
        monitoring.Filters(pd_ids=[1]),
        monitoring.Filters(locations=["Akkar"]),
        monitoring.Filters(report_type="HR"),
        monitoring.Filters(year=2026),
        monitoring.Filters(scope="year"),
        monitoring.Filters(status="off_track"),
        monitoring.Filters(q="girls"),
        monitoring.Filters(tags={"gender": ["Girls"]}),
        monitoring.Filters(pd_statuses=[]),
        monitoring.Filters(pd_statuses=["closed"]),
        monitoring.Filters(document_types=["SSFA"]),
        monitoring.Filters(offices=["Zahle"]),
        monitoring.Filters(partner_types=["CSO"]),
        monitoring.Filters(emergency="yes"),
        monitoring.Filters(cp_output="psychosocial"),
    ]
    keys_seen = {f.cache_key() for f in [base, *changed]}
    assert len(keys_seen) == len(changed) + 1
    # the order of a filter's choices never decides its figures: one key
    assert (
        monitoring.Filters(sections=["B", "A"]).cache_key()
        == monitoring.Filters(sections=["A", "B"]).cache_key()
    )


def test_a_stored_build_never_shows_one_person_s_view_to_another(admin_user, sectioned, world):
    """Nothing in a stored build depends on the person asking: the viewer's own section reaches it only as the
    filter's sections, which are in the key. A build stored by an administrator's request answers the viewer for
    the same filter, with the same figures; the viewer's first load is the Child Protection filter."""
    admin, viewer = _signed_in(admin_user), _signed_in(sectioned)
    staff = get_part(admin, _url("grid", section="Child+Protection"), "grid").context["rows"]
    read = get_part(viewer, PAGE, "grid").context["rows"]  # the viewer's first load: their section
    assert read == staff and {r.section for r in read} == {"Child Protection"}
    caches["shared"].clear()
    assert (
        get_part(viewer, PAGE, "grid").context["rows"] == staff
    )  # computed by the viewer's request this time


class _EveryStringTranslated(gettext.NullTranslations):
    """A language in which every translatable string reads differently."""

    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_the_stored_values_read_the_same_in_every_language(world):
    """The indicators, the filter options and the map's places are stored once for every language
    (``lang_neutral``): nothing in them is translated (the templates translate the labels)."""
    filters = monitoring.Filters(year=2026)

    def values():
        return (
            monitoring.as_rows(monitoring.indicators(filters, TODAY)),
            monitoring.filter_options(filters),
            monitoring.map_points(filters, TODAY),
        )

    with translation.override("en"):
        english = values()
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        assert translation.gettext("Reached") == "[Reached]"  # the language is on
        other = values()
    finally:
        translation.deactivate()
    assert other == english, "a stored value holds a translated string"


# ------------------------------------------------------------------------------------------- fresh after edit
def test_a_finished_etools_sync_shows_at_once(client_viewer, world, monkeypatch):
    """The stored indicators are keyed on the eTools jobs' latest runs: a Datamart sync that changes a report and
    ends shows at once (the figures were stored a moment before)."""
    calls = _builds(monkeypatch)

    def girls() -> float:
        rows = get_part(client_viewer, _url("grid", year="2026", section=""), "grid").context["rows"]
        return next(r for r in rows if r.title == world["girls"]).reached

    assert girls() == 550.0 and calls["rows"] == 1
    assert girls() == 550.0 and calls["rows"] == 1  # stored
    # the sync rewrites a report's rows, then ends its run
    dm.ReportedIndicator.objects.filter(
        indicator=world["girls"], progress_report="PR-2", location="Akkar"
    ).update(
        disaggregation={"()": {"v": 250, "d": 1, "c": 250}},
        achievement_in_period="250",
        total_cumulative_progress="650",
    )
    assert girls() == 550.0  # not seen until the run ends
    datamart_synced()
    assert girls() == 650.0 and calls["rows"] == 2


def test_a_run_of_another_job_keeps_the_stored_figures(client_viewer, world, monkeypatch):
    """Only the eTools jobs move the key (SPEC7_review item 23d): a NeuroDB Watch run or an ActivityInfo import
    changes no figure of these pages, and nothing is computed again."""
    calls = _builds(monkeypatch)
    get_part(client_viewer, _url("grid", year="2026"), "grid")
    for job in (SyncRun.Job.WATCH, SyncRun.Job.ACTIVITYINFO_DATA, SyncRun.Job.DAILY_REVIEW):
        SyncRun.objects.create(job=job, target="all", status=SyncRun.Status.RUNNING).finish(
            SyncRun.Status.SUCCEEDED
        )
        compute.reset_memo()
        get_part(client_viewer, _url("grid", year="2026"), "grid")
    assert calls["rows"] == 1
    for job in keys.ETOOLS_JOBS:  # each eTools job does
        SyncRun.objects.create(job=job, target="all", status=SyncRun.Status.RUNNING).finish(
            SyncRun.Status.SUCCEEDED
        )
        compute.reset_memo()
        get_part(client_viewer, _url("grid", year="2026"), "grid")
    assert calls["rows"] == 1 + len(keys.ETOOLS_JOBS)


def test_emptying_the_stored_figures_in_the_admin_shows_any_change(client, admin_user, world):
    """*Empty the stored page figures* (Run a job, no shell needed) drops what every worker stored: a change made
    outside any job shows at once."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    url = _url("grid", year="2026", section="")
    assert get_part(client, url, "summary").context["data"]["indicators"] == 3
    dm.PDIndicator.objects.filter(title=CLASSROOMS).delete()  # no sync run: the stored figures stay
    assert get_part(client, url, "summary").context["data"]["indicators"] == 3
    compute.clear()
    assert get_part(client, url, "summary").context["data"]["indicators"] == 2


# ------------------------------------------------------------------------------------------------ the links
def test_the_pages_of_the_grid_reload_their_part_and_keep_the_filter(
    client_viewer, sectioned, world, monkeypatch
):
    """A page link of the grid reloads the grid part (its address carries the resolved filter: the viewer's own
    section, which the old link lost) and shows the page's own address, never the part's."""
    monkeypatch.setattr(monitoring, "PAGE_SIZE", 1)
    grid = text(get_part(client_viewer, PAGE, "grid"))
    link = re.search(r'<a class="page-link" href="([^"]+)" hx-get="([^"]+)"[^>]*hx-target="([^"]+)"[^>]*'
                     r'hx-push-url="([^"]+)"[^>]*>2</a>', grid)  # fmt: skip
    assert link, "the grid has a second page"
    href, hx_get, target, pushed = (html_module.unescape(g) for g in link.groups())
    assert target == "#part-grid" and _query(hx_get)["_part"] == ["grid"] and _query(hx_get)["page"] == ["2"]
    for address in (href, hx_get, pushed):
        assert _query(address)["section"] == ["Child Protection"] and "as_of" not in _query(address)
    assert "_part" not in href and "_part" not in pushed and urlsplit(pushed).path == PAGE
    second = client_viewer.get(hx_get, **HX)
    assert "Showing 2–2 of 2" in text(second) and second.context["page_obj"].number == 2


def test_the_sorts_and_section_tiles_reload_the_results_area(client_viewer, world):
    table = text(get_part(client_viewer, _url("table"), "table"))
    sorts = re.findall(r'<a class="sort-link" href="([^"]+)" hx-get="([^"]+)" hx-target="([^"]+)"', table)
    assert sorts and all(target == f"#{RESULTS}" and "_part" not in href + hx for href, hx, target in sorts)
    tiles = text(get_part(client_viewer, _url("table"), "sections"))
    found = re.findall(r'<a class="section-tile" href="([^"]+)" hx-get="([^"]+)" hx-target="([^"]+)"', tiles)
    assert found and all(target == f"#{RESULTS}" and "_part" not in href + hx for href, hx, target in found)


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    ("grid", ""),  # first load (the viewer's own section)
    ("grid", "section="),
    ("grid", "section=Education"),
    ("grid", "year=all"),
    ("grid", "year=2025&section="),
    ("grid", "section=&status=off_track"),
    ("grid", "section=No+such+section"),  # a filter with no result
    ("grid", "section=&page=2"),
    ("table", ""),
    ("table", "year=2026&sort=reached_in_year"),
    ("table", "section=Child+Protection&sort=partner"),
    ("quality", ""),
    ("quality", "year=2026&section=Education"),
]


@pytest.mark.parametrize("role", ["viewer", "staff"])
@pytest.mark.parametrize(("view", "query"), MATRIX)
def test_the_assembled_page_is_the_eager_page(admin_user, sectioned, world, role, view, query, monkeypatch):
    monkeypatch.setattr(monitoring, "PAGE_SIZE", 2)  # pages, on this world
    client = _signed_in(sectioned if role == "viewer" else admin_user)
    url = _url(view) + ("&" if "?" in _url(view) else "?") + query if query else _url(view)
    assembled = assemble(client, url)
    assert list(assembled.parts) == list(VIEWS[view])
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client, url)
    assert drawn.status_code == 200
    # under every filter: no link of a part (nor of the eager page) carries _part, and no part sends anything that
    # would run once swapped in
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link and "as_of" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link and "_eager" not in link for link in LINK.findall(text(drawn)))
    assert placeholders(text(drawn)) == {}  # every part drawn
    assert normalise(assembled.html) == normalise(text(drawn))
    # the chart JSON, unique in the page, the same as the eager page's, in the summary part only
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone and set(together) <= {SUMMARY_JSON}
    if view == "grid":
        sent = _json_blocks(text(assembled.parts["summary"]))
        assert sent == {SUMMARY_JSON: alone[SUMMARY_JSON]} and set(sent[SUMMARY_JSON]) == {
            "status_counts",
            "by_section",
        }


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world):
    """*Show the figures* (no JavaScript) leads to the page drawn at once: every block, no grey shapes left and no
    link to the page already shown."""
    for view, figures in (
        ("grid", ("Tracking status", 'id="pd-indicators"', f'id="{SUMMARY_JSON}"')),
        ("table", ("Every section", 'id="program-performance"')),
        ("quality", ("Active PDs", "What the counts mean")),
    ):
        caches["shared"].clear()  # the page drawn at once just stored its figures: they would be drawn inline
        frame = text(client_viewer.get(_url(view)))
        shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
        assert len(shown) == 2 and len(set(shown)) == 1
        address = PAGE + html_module.unescape(shown[0])
        assert "_eager=1" in address and "as_of" not in address
        page = text(client_viewer.get(address))
        assert placeholders(page) == {} and "part__skeleton" not in page and "Show the figures" not in page
        for figure in figures:
            assert figure in page, (view, figure)
        assert "_eager=1" not in "".join(LINK.findall(page))


def test_the_exports_stay_whole_files(client_viewer, world):
    """The Excel and CSV files are answered first, as before: every indicator of the filter."""
    response = client_viewer.get(PAGE, {"view": "table", "export": "csv"})
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert b"".join(response.streaming_content).count(b"\n") == 4  # the heading and three indicators


# ------------------------------------------------------------------------------------------------- the map
@pytest.fixture
def located(world):
    """The fixture's Akkar rows placed at a gazetteer location with coordinates."""
    governorate = LocationType.objects.create(name="Governorate", admin_level=1)
    akkar = Location.objects.create(
        name="Akkar", p_code="LB1", type=governorate, latitude=34.55, longitude=36.1, lft=1, rght=2, level=0,
        tree_id=1,
    )  # fmt: skip
    dm.PDIndicator.objects.filter(location_name="Akkar").update(location_pcode="LB1", location=akkar)
    dm.ReportedIndicator.objects.filter(location="Akkar").update(p_code="LB1", location_ref=akkar)
    return akkar


def test_the_map_api_is_stored_for_every_worker(client_viewer, located, monkeypatch):
    calls: Counter = Counter()
    real = monitoring.map_points

    def counted(filters, day=None):
        calls["map"] += 1
        return real(filters, day)

    monkeypatch.setattr(monitoring, "map_points", counted)
    api = reverse("api:pd_map")
    first = client_viewer.get(api, {"year": "2026"}).json()
    compute.reset_memo()
    assert client_viewer.get(api, {"year": "2026"}).json() == first and calls["map"] == 1
    assert {p["key"] for p in first["points"]} == {"LB1"}
    client_viewer.get(api, {"year": "2026", "section": "Education"})
    assert calls["map"] == 2  # another filter: its own places


def test_a_location_edited_in_the_admin_shows_at_once_on_the_map(client, admin_user, located):
    """The gazetteer is the one table the map reads that staff change by hand (Admin → Locations): its last
    change is in the key, so a place given other coordinates moves at once, though the places were stored a moment
    before."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    api = reverse("api:pd_map")

    def akkar() -> float:
        return next(p for p in client.get(api, {"year": "2026"}).json()["points"] if p["key"] == "LB1")[
            "latitude"
        ]

    assert akkar() == 34.55
    response = client.post(
        reverse("admin:locations_location_change", args=[located.pk]),
        {"name": "Akkar", "p_code": "LB1", "cas_code": "", "type": located.type_id, "parent": "",
         "latitude": "34.6", "longitude": "36.1", "is_active": "on", "_save": "Save"},
    )  # fmt: skip
    assert response.status_code == 302, text(response)[:2000]
    assert akkar() == 34.6


def test_the_map_places_come_in_one_order(located, world):
    """Places with as many indicators come by their key, so every computation (every worker) gives one order."""
    found = monitoring.map_points(monitoring.Filters(year=2026), TODAY)
    order = [(-p["indicators"], p["key"]) for p in found["points"] + found["unlocated"]]
    assert [(-p["indicators"], p["key"]) for p in found["points"]] == sorted(
        (-p["indicators"], p["key"]) for p in found["points"]
    )
    assert order


@pytest.mark.shell
def test_the_map_page_reads_its_filter_options_stored(client_viewer, world, monkeypatch):
    calls: Counter = Counter()
    real = monitoring.filter_options

    def counted(filters):
        calls["options"] += 1
        return real(filters)

    monkeypatch.setattr(monitoring, "filter_options", counted)
    url = reverse("reports:pd_monitoring_map")
    assert client_viewer.get(url).status_code == 200
    compute.reset_memo()
    assert client_viewer.get(PAGE).status_code == 200  # the grid's frame reads the same stored options
    assert calls["options"] == 1
