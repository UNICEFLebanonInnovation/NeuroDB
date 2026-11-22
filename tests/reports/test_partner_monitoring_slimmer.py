"""Partner monitoring, slimmer (the owner's decision of 21 Nov 2026).

* Its tiles have no "Programme documents" tile (with its hint "N indicators with Reached"). The tiles' template
  is Home's indicator block's and the figure trail's too: Home and the trail's page keep the tile.
* Its indicators table (the grid, in the Data quality tab) has no "% Reached", "Reached in <year>", "Awaiting
  acceptance" or "Partner's view" column; the rest of the table is unchanged. Its Copy and CSV buttons read
  the table on the page: they keep those four columns, from cells only the download reads (``hidden``,
  ``data-export="only"``).

On the world of tests/reports/test_progress_reports_active_default.py (programme documents running in 2026, two
indicators each, partner reports in every status).
"""

# ruff: noqa: F401, F811 (the fixtures come from the modules that define them)
from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.datamart import trail_monitoring as tm
from tests.reports.test_progress_reports_active_default import world
from tests.web.parts import get_part, text

pytestmark = pytest.mark.django_db

PAGE = "/partner-monitoring/"
LABEL = re.compile(r'<div class="kpi__label">([^<]+)</div>')
GRID = re.compile(r'<table[^>]*\bid="pd-indicators"[^>]*>(.*?)</table>', re.S)
HEAD = re.compile(r"<thead>(.*?)</thead>", re.S)
CELL = re.compile(r"<(th|td)\b([^>]*)>(.*?)</\1>", re.S)
TAGS = re.compile(r"<[^>]+>")
GONE = ("% Reached", "Reached in 2026", "Awaiting acceptance", "Partner's view")


def _labels(html: str) -> list[str]:
    return [label.strip() for label in LABEL.findall(html)]


def _headings(html: str) -> tuple[list[str], list[str]]:
    """The grid's column headings: those a person sees, and those its Copy and CSV write (every one)."""
    table = GRID.search(html)
    assert table, "the grid is drawn"
    head = HEAD.search(table.group(1)).group(1)
    seen, written = [], []
    for _tag, attrs, inner in CELL.findall(head):
        words = " ".join(TAGS.sub("", inner).replace("&#x27;", "'").split())
        written.append(words)
        if " hidden" not in f" {attrs}":
            seen.append(words)
    return seen, written


def _rows(html: str) -> list[list[tuple[str, str]]]:
    """The grid's indicator rows: each cell's attributes and text."""
    body = GRID.search(html).group(1).split("<tbody>", 1)[1]
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        out.append(
            [(attrs, " ".join(TAGS.sub("", inner).split())) for _tag, attrs, inner in CELL.findall(row)]
        )
    return out


def _validator() -> Client:
    user = User.objects.create_user(username="checker", email="checker@example.org", password="x-pass-123456")
    user.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=VALIDATORS))
    client = Client()
    client.force_login(user)
    return client


# ------------------------------------------------------------------------------------------------ the tiles
def test_partner_monitoring_has_no_programme_documents_tile(client_viewer, world):
    summary = text(get_part(client_viewer, PAGE, "summary"))
    labels = _labels(summary)
    assert "Programme documents" not in labels
    assert "indicators with Reached" not in summary
    assert labels == ["Indicators", "On track", "Off track", "Not reported", "Partners", "Overdue reports"]
    # nor do Data quality's own tiles, which open the page: Active PDs comes first
    quality = text(get_part(client_viewer, PAGE, "quality-tiles"))
    assert (
        "Programme documents" not in _labels(quality)
        and "running (active, signed or suspended)" not in quality
    )
    assert _labels(quality)[0] == "Active PDs"
    assert 'data-figure="dq.pds"' not in quality and 'data-figure="dq.running"' not in quality


def test_home_and_the_figure_trail_keep_the_programme_documents_tile(roles, client_viewer, world):
    home = text(get_part(client_viewer, "/home/?section=", "indicators"))
    assert "Programme documents" in _labels(home) and "indicators with Reached" in home
    trail_page = reverse("reports:check_figures", args=["partner-monitoring"])
    checked = text(get_part(_validator(), trail_page, "figures"))
    assert "Programme documents" in _labels(checked)


def test_the_figure_trail_says_the_page_no_longer_draws_the_tile(world):
    found = tm.monitoring_trail({}, tm.monitoring.today())
    for key in ("pm.pds", "pm.with_reached", "dq.pds", "dq.running"):
        figure = found.figure(key)
        assert not figure.drawn and figure.note, key
    # Home's block draws them
    home = tm.monitoring_trail(
        {"section": [""], "year": "2026", "scope": "year"}, tm.monitoring.today(), home=True
    )
    assert home.figure("pm.pds").drawn and home.figure("pm.with_reached").drawn


# --------------------------------------------------------------------------------------------- the grid
def test_the_grid_has_none_of_the_four_columns_and_its_downloads_keep_them(client_viewer, world):
    html = text(get_part(client_viewer, PAGE, "grid"))
    seen, written = _headings(html)
    for heading in GONE:
        assert heading not in seen, heading
    assert seen[:4] == ["Indicator", "Target", "Reached", "Status"]
    assert seen[4:] == ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    # Copy and CSV read every cell of the table, those for the download only too: the columns as before
    assert written[:8] == ["Indicator", "Target", "Reached", *GONE[:3], "Status", "Partner's view"]
    rows = _rows(html)
    assert rows and all(len(row) == len(written) for row in rows)
    for row in rows:
        for attrs, _words in row:
            if " hidden" in f" {attrs}":
                assert 'data-export="only"' in attrs  # never drawn, only downloaded
        # no meter, tooltip or figure key is left in the cells for the download
        assert not any("meter" in attrs or "title=" in attrs for attrs, _ in row if " hidden" in f" {attrs}")
    assert 'class="meter' not in html


def test_every_year_the_grid_has_no_reached_in_a_year_column(client_viewer, world):
    html = text(get_part(client_viewer, f"{PAGE}?year=all", "grid"))
    seen, written = _headings(html)
    assert seen[:4] == ["Indicator", "Target", "Reached", "Status"]
    assert not any(h.startswith("Reached in") for h in written)
    assert written[:7] == [
        "Indicator",
        "Target",
        "Reached",
        "% Reached",
        "Awaiting acceptance",
        "Status",
        "Partner's view",
    ]


def test_the_grid_still_lists_every_indicator_in_its_order(client_viewer, world):
    grid = get_part(client_viewer, PAGE, "grid")
    html = text(grid)
    assert len(grid.context["rows"]) == 6  # two indicators of each of the three running programme documents
    # section, programme document, output, indicator: in the order the page groups them
    order = [
        (p["pd"].number, r.title)
        for g in grid.context["groups"]
        for p in g["pds"]
        for o in p["outputs"]
        for r in o["indicators"]
    ]
    assert len(order) == 6
    starts = [html.index(f">{p['pd'].number}</a>") for g in grid.context["groups"] for p in g["pds"]]
    assert len(starts) == 3 and starts == sorted(starts)
    for _number, title in order:
        assert title in html


def test_program_performance_still_sorts(client_viewer, world):
    up = get_part(client_viewer, f"{PAGE}?view=table&sort=pd", "table").context["page_obj"]
    down = get_part(client_viewer, f"{PAGE}?view=table&sort=-pd", "table").context["page_obj"]
    numbers = [r.pd.number for r in up]
    assert numbers == sorted(numbers) and [r.pd.number for r in down] == sorted(numbers, reverse=True)
