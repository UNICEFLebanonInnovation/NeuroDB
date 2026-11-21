"""Progress reports without its grey notes (the owner's decision of 21 Nov 2026).

Every tab of the page (Reporting progress, Partner view, and the hidden By location and By disaggregation) draws
no explanation note: none under the tiles, under a table or under a list. What stays: the subtitle under the
title, the empty states ("No progress reports match."), the warnings (a filter holding too many rows, reports
still owed that may be missing), the line that says the page shows one programme document with its way back,
the tile hints and, for the people who check the figures, the figure trail. The figure trail still gives the
figures the notes held (reports submitted late, what the PD status filter leaves out), marked as not drawn.
Home and Partner monitoring keep their notes.

On the world of tests/reports/test_progress_reports_active_default.py, and for By disaggregation on the world of
tests/reports/test_r4_views.py (``labelled``).
"""

# ruff: noqa: F401, F811 (the fixtures come from the modules that define them)
from __future__ import annotations

import datetime
import re

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VALIDATORS, VIEWER
from neurodb.datamart import trail
from tests.reports.test_progress_reports_active_default import world
from tests.reports.test_r4_views import labelled
from tests.web.parts import text

pytestmark = pytest.mark.django_db

PAGE = "/partner-reporting/"
DAY = datetime.date(2026, 10, 15)
SUBTITLE = "Progress reports submitted by partners in the Partner Reporting Portal, from the eTools Datamart"
# a grey paragraph of the results area (the explanation notes were all of this shape)
MUTED = re.compile(r'<p\b[^>]*class="[^"]*\btext-muted\b[^"]*"[^>]*>(.*?)</p>', re.S)
TAGS = re.compile(r"<[^>]+>")
# the grey sentences that stay: the empty states of a list
EMPTY_STATES = (
    "No progress reports match.",
    "Every partner in the filter has a Submitted or Accepted report.",
    "Nothing to follow up: every report in the filter is Accepted.",
    "No progress report in the filter.",
)
# the notes the page drew before, one phrase of each
NOTES = (
    "Each tile counts progress reports",
    "The PD status filter leaves out",
    "Show every PD status",
    "name no programme document",
    "Administrators:",
    "Each progress report is counted once",
    "Open a report to see each indicator",
    "A partner with reports in several statuses",
    "Overdue first, then Sent back",
    "One row per indicator, report and location",
    "Per indicator, report and location",
    "progress reports were submitted after their due date",
)


def _client(*groups: str, staff: bool = False) -> Client:
    user = User.objects.create_user(
        username="-".join(groups).lower(),
        email="someone@example.org",
        password="x-pass-123456",
        is_staff=staff,
    )
    user.groups.add(*Group.objects.filter(name__in=groups))
    client = Client()
    client.force_login(user)
    return client


def _results(html: str) -> str:
    """The page's results area: everything below the filter bar, to the end of the page's own content."""
    area = html[html.index('id="reporting-results"') :]
    return area[: area.index("</main>")]


def _grey(html: str) -> list[str]:
    return [" ".join(TAGS.sub("", found).split()) for found in MUTED.findall(_results(html))]


def _assert_quiet(html: str, where: str) -> None:
    for sentence in _grey(html):
        assert sentence in EMPTY_STATES, (where, sentence)
    for phrase in NOTES:
        assert phrase not in html, (where, phrase)


@pytest.mark.parametrize("tab", ["", "partners", "locations"])
def test_no_tab_draws_a_grey_note(roles, world, tab):
    """For a viewer, an administrator (who had one note of their own) and a validator."""
    for client in (_client(VIEWER), _client(ADMIN, staff=True), _client(VIEWER, VALIDATORS)):
        response = client.get(PAGE, {"tab": tab} if tab else {})
        assert response.status_code == 200
        html = text(response)
        _assert_quiet(html, tab)
        if tab != "locations":  # the PD status filter leaves three programme documents out: still no note
            assert response.context["data"]["counts"]["progress_reports"] == 2


def test_by_disaggregation_draws_no_grey_note(client_viewer, labelled):
    response = client_viewer.get(PAGE, {"tab": "disaggregations"})
    assert response.status_code == 200 and response.context["disaggregations"]["blocks"]
    _assert_quiet(text(response), "disaggregations")


def test_every_list_s_empty_state_stays(client_viewer, world):
    html = text(client_viewer.get(PAGE, {"q": "no such partner"}))
    assert "No progress reports match" in html
    partners = text(client_viewer.get(PAGE, {"tab": "partners", "q": "no such partner"}))
    assert "No progress report in the filter." in partners
    accepted = text(client_viewer.get(PAGE, {"tab": "partners", "status": "accepted"}))
    assert "Nothing to follow up: every report in the filter is Accepted." in accepted


def test_the_subtitle_the_hints_the_pd_line_and_the_trail_stay(roles, world):
    client = _client(VIEWER, VALIDATORS)
    html = text(client.get(PAGE))
    assert SUBTITLE in html
    assert 'class="kpi__hint"' in html and "Each indicator once, in active programme documents" in html
    assert "Not yet due included" in html  # the chart's own hint, in its header
    assert "Check these figures" in html and "Power BI comparison sheet (Excel)" in html
    one = text(client.get(PAGE, {"pd": "LEB/PD21"}))
    assert 'id="reporting-pd-note"' in one
    assert "Programme document LEB/PD21 only" in one and "Show every programme document" in one


def test_a_warning_stays(client_viewer, world, monkeypatch):
    from neurodb.datamart import reporting

    monkeypatch.setattr(reporting, "LOCATION_LIMIT", 1)
    html = text(client_viewer.get(PAGE, {"tab": "locations"}))
    assert "more than the 1 this view reads at once" in html


def test_the_trail_still_gives_the_figures_the_notes_held(world):
    found = trail.progress_trail("progress-reports", {}, DAY)
    late = found.figure("pr.late")
    assert not late.drawn and late.note
    for name in ("by_pd_status", "other_status", "not_held", "no_pd"):
        figure = found.figure(f"pr.left_out.{name}")
        assert not figure.drawn and figure.note, name
    # the indicator reports of the signed, suspended and ended programme documents: two each
    assert found.figure("pr.left_out.other_status").page == 6
    assert any("leaves out" in note for note in found.notes)


def test_home_keeps_its_notes(client_viewer, world):
    html = text(client_viewer.get("/home/?section="))
    assert "Each tile counts progress reports, as Power BI" in html
    assert "Counts are progress reports. Overdue first" in html
    pm = text(client_viewer.get(reverse("reports:pd_monitoring")))
    assert "never adding a value the reports repeat" in pm  # Partner monitoring keeps its notes
