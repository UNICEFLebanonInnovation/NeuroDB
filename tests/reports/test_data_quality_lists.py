"""Data quality's lists on Partner monitoring (the part ``quality-lists``) without the *Partial history* box: the
owner removed it on 25 Nov 2026. What stays: each indicator of a programme document that started before the
reports NeuroDB holds still shows the *Partial history* chip in the grid and in Program Performance, and its
Reached is still PRP's cumulative. The figure trail has no figure for the box.

On the world of tests/reports/test_pd_monitoring.py (``data``, with ``more`` of test_powerbi_views.py), today
frozen at 1 July 2026, the reports held only from 1 March 2026: the active PD, which started on 1 January 2026,
has a partial history."""

# ruff: noqa: F401, F811 (the fixtures "data", "more" and "frozen" come from the modules that define them)
from __future__ import annotations

import datetime
import html as html_module

import pytest

from neurodb.datamart import monitoring
from neurodb.datamart import trail_monitoring as tm
from tests.reports.test_pd_monitoring import TODAY, data
from tests.reports.test_powerbi_views import frozen, more
from tests.web.parts import get_part, text

pytestmark = pytest.mark.django_db

PAGE = "/partner-monitoring/"  # reports:pd_monitoring
MARCH = datetime.date(2026, 3, 1)
# what the box drew: its title, its table, its file name, its count's figure, its sentence and its empty text
BOX = (
    '<h2 class="h6 mb-0">Partial history</h2>',
    "dq-partial",
    "partial-history",
    "dq.list.partial",
    "the first reporting period NeuroDB holds",
    "None: NeuroDB holds every report since each programme document started.",
)
CHIP = ">Partial history</span>"
GONE = ("partial", "partial_count", "read_from", "read_from_source", "read_from_asked")


@pytest.fixture
def partial(more, frozen, monkeypatch):
    """The reports held only from 1 March 2026 (as the Datamart's feed says, asked from 1 Jan 2025): the
    active PD, started 1 January 2026, has a partial history."""
    history = monitoring.rc.HistoryFrom(MARCH, monitoring.rc.FEED, datetime.date(2025, 1, 1))
    monkeypatch.setattr(monitoring.rc, "history_from", lambda today=None: history)
    return more


def _plain(html: str) -> str:
    return " ".join(html_module.unescape(html).split())


def _url(view: str = "", **params: str) -> str:
    query = ({"view": view} if view else {}) | params
    return PAGE + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def test_the_lists_part_has_no_partial_history_box(client_viewer, partial):
    rows = monitoring.indicators(monitoring.Filters(), TODAY)
    assert rows and all(r.partial for r in rows if r.pd == partial["pd"])  # the PD has a partial history
    html = text(get_part(client_viewer, _url("quality"), "quality-lists"))
    for drawn in BOX:
        assert drawn not in html, drawn
    # the counts text names the two checks left: Reached differing from PRP's, the same title twice
    plain = _plain(html)
    assert "The last two lists are NeuroDB's own checks: where its Reached differs" in plain
    assert "two indicators with the same title (they are kept apart, never merged)." in plain
    assert "The last three lists" not in plain and "started before the reports NeuroDB holds" not in plain
    # the same-title list is a full-width panel, as the other single lists: no half-empty row
    assert html.count('class="grid grid--2') == 1  # the missing targets beside what the counts mean
    before = html.split('data-table-target="#dq-titles"', 1)[0]
    assert before[before.rindex("<section") :].startswith('<section class="panel p-0 mb-3">')


def test_the_data_quality_page_has_no_box_and_its_grid_keeps_the_chip(client_viewer, partial):
    for params in ({"view": "quality"}, {"view": "quality", "year": "all"}, {}):
        page = client_viewer.get(PAGE, params)  # the page put together from its parts
        html = text(page)
        assert page.status_code == 200 and "Same title twice in one PD" in html
        for drawn in BOX:
            assert drawn not in html, (params, drawn)
        assert CHIP in html, params  # the grid below the lists: each indicator of the PD keeps its chip
        quality = page.context["quality"]
        assert not set(GONE) & set(quality), set(GONE) & set(quality)


def test_the_quality_figures_keep_their_counts(partial):
    quality = monitoring.data_quality(
        monitoring.indicators(monitoring.Filters(), TODAY), monitoring.Filters()
    )
    assert not set(GONE) & set(quality)
    assert (quality["indicators"], quality["same_title_count"], quality["mismatch_count"]) == (3, 0, 0)


@pytest.mark.parametrize("view", ["", "table"])
def test_the_chip_still_shows_in_the_grid_and_the_table(client_viewer, partial, view):
    name = "table" if view else "grid"
    html = text(get_part(client_viewer, _url(view, year="all"), name))
    shown = [r for r in monitoring.indicators(monitoring.Filters(), TODAY) if r.pd == partial["pd"]]
    assert shown and html.count(CHIP) == len(shown)  # one chip per indicator of the PD
    if not view:
        assert "The PD started before the reports NeuroDB reads: Reached is PRP’s own cumulative" in html


def test_no_chip_without_a_partial_history(client_viewer, more, frozen):
    html = text(get_part(client_viewer, _url(year="all"), "grid"))
    assert CHIP not in html


@pytest.mark.parametrize("params", [{"view": "quality", "year": "2026"}, {"view": "quality", "year": "all"}])
def test_the_figure_trail_has_no_partial_history_figure(partial, params):
    found = tm.monitoring_trail(params, TODAY)
    keys = {f.key for f in found.figures}
    assert "dq.list.partial" not in keys
    assert {"dq.list.missing_target", "dq.list.no_reached", "dq.list.differs", "dq.list.same_title"} <= keys
    assert not found.differences, [(f.key, f.page, f.rebuilt) for f in found.differences]
    assert all(f.label != "Partial history" for f in found.figures)
