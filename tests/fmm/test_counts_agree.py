"""Monitoring insights counts as the overview and the field monitoring page do (§0.4, invariants 1-5):
the visits of a calendar year with no filter equal the overview's field monitoring visits and the
field monitoring page's "Monitoring activities"; the monitored entities equal its "Findings". A row
without an activity reference is the one known difference, and the page's data note says so. The
average quality is one figure everywhere, and the partner and PD pages' panels equal the page their
link opens, for every user."""

from __future__ import annotations

import datetime

import pytest

from neurodb.datamart import models as dm
from neurodb.datamart import services
from neurodb.fmm import metrics, refresh
from neurodb.fmm.scope import Scope
from neurodb.reports import overview

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)


def _fmm(year: int = 2026) -> dict:
    return metrics.kpis(Scope.from_params({"year": str(year), "section": ""}))


def _overview_visits(reporting_year, year: int = 2026) -> int:
    data = overview.build(overview.Scope(year=year, reporting_year=reporting_year, today=TODAY), cache=False)
    return data["delivery"]["assurance"]["field_monitoring_visits"]


def test_visits_and_entities_equal_the_overview_and_the_field_monitoring_page(built, reporting_year):
    page = services.monitoring({"year": "2026"})
    kpis = _fmm()
    assert kpis["visits"] == _overview_visits(reporting_year) == page["activities"] == 8
    assert kpis["entities"] == page["findings"].count() == 12
    # another year: nothing on either side
    assert _fmm(2025)["visits"] == services.monitoring({"year": "2025"})["activities"] == 0


def test_a_row_without_a_reference_is_the_difference_and_the_note_says_so(built, reporting_year):
    dm.MonitoringFinding.objects.create(
        datamart_id=999, end_date=datetime.date(2026, 6, 1), overall_finding_rating="On Track"
    )
    refresh.run(triggered_by="test", today=TODAY)
    kpis = _fmm()
    assert (
        kpis["visits"]
        == _overview_visits(reporting_year) + 1
        == services.monitoring({"year": "2026"})["activities"] + 1
    )
    assert kpis["entities"] == services.monitoring({"year": "2026"})["findings"].count() == 13
    notes = metrics.notes(Scope.from_params({"year": "2026", "section": ""}))
    assert {"key": "no_reference", "n": 1} in notes


def test_the_average_quality_is_one_definition(built):
    from neurodb.fmm.models import Visit
    from neurodb.fmm.score import average_quality

    scope = Scope.from_params({"year": "2026", "section": ""})
    scores = list(Visit.objects.values_list("quality_score", flat=True))
    assert _fmm()["avg_quality"] == metrics.avg_quality(scope.visits()) == average_quality(scores)


@pytest.mark.parametrize(
    "params", [{}, {"governorate": "north"}, {"rating": "on_track"}, {"status": "reported"}]
)
def test_the_average_quality_tile_equals_the_analysis_highlight(built, client_viewer, params):
    """Invariant 5: the key figure and the Analysis tab's highlight show one average quality."""
    import re

    from django.urls import reverse

    scope = Scope.from_params({"year": "2026", "section": "", **params})
    html = client_viewer.get(
        reverse("fmm:dashboard"), {"tab": "analysis", "year": "2026", "section": "", **params}
    )
    text = " ".join(re.sub(r"<[^>]+>", " ", html.content.decode()).split())
    tile = re.search(r"Average quality score ([\d.]+%|—)", text).group(1)
    highlight = re.search(r"([\d.]+%|—) Average quality · \d+ scored visits?", text).group(1)
    expected = metrics.kpis(scope)["avg_quality"]
    assert tile == highlight == (f"{expected}%" if expected is not None else "—")
    assert metrics.highlights(scope)["avg_quality"] == expected == metrics.avg_quality(scope.visits())
    # the AI brief's facts, sent and checked against, carry the same figure (stage 6b)
    from neurodb.fmm.ai import facts

    sent = facts.build(scope, narratives=False).payload["kpi"]["avg_quality"]
    assert sent == (float(expected) if expected is not None else None)
    # and so does the chat's fm_summary, bound to the same filter (stage 7)
    from neurodb.fmm.ai import tools

    with tools.bind(tools.ChatContext(scope=scope, texts_left=0, cards_max=15)):
        counted = tools.fm_summary()
    assert counted["avg_quality"] == sent and counted["visits"] == metrics.kpis(scope)["visits"]


# ---------------------------------------------------------------------- invariants 3 and 4 (stage 8a)
@pytest.fixture
def _this_year(monkeypatch):
    """The partner and PD pages read 2026 as this year, whatever day the tests run."""
    monkeypatch.setattr("neurodb.reports.views._this_year", lambda: 2026)


def _users(fm_world):
    from neurodb.accounts.models import User
    from tests.fmm.test_pages import _section_editor

    viewer = User.objects.create_user(username="plain", email="plain@example.org", password="x-pass-123456")
    return [viewer, _section_editor(fm_world.section)]  # one without a section, one with Education


def _page_visits(client, url: str) -> int:
    import re

    from tests.fmm.test_pages import visible

    text = " ".join(visible(client.get(url).content.decode()).split())
    return int(re.search(r"Monitoring visits (\d+)", text).group(1))


def test_the_partner_panel_equals_fmm_for_every_user(built, fm_world, client, _this_year):
    """Invariant 3: the partner page's panel = FMM ?partner=<id>&year=Y&section=, for every user."""
    from neurodb.fmm import services

    for user in _users(fm_world):
        client.force_login(user)
        for partner in fm_world.partners.values():
            panel = services.partner_summary(partner.pk, 2026)
            assert _page_visits(client, panel["url"]) == panel["visits"]
            assert panel["visits"] == _fmm_with(partner=partner.pk)["visits"]
            assert panel["avg_quality"] == _fmm_with(partner=partner.pk)["avg_quality"]


def test_the_pd_panel_equals_fmm_for_every_user(built, fm_world, client, _this_year):
    """Invariant 4: the PD page's FMM row = FMM ?pd=<id>&year=Y&section=, for every user."""
    from neurodb.fmm import services

    for user in _users(fm_world):
        client.force_login(user)
        for pd in fm_world.pds.values():
            panel = services.pd_summary(pd.pk, 2026)
            if panel is None:
                continue
            assert _page_visits(client, panel["url"]) == panel["visits"] == _fmm_with(pd=pd.pk)["visits"]
            assert sum(q["visits"] for q in panel["quarters"]) == panel["visits"]


def _fmm_with(**params) -> dict:
    return metrics.kpis(
        Scope.from_params({"year": "2026", "section": "", **{k: str(v) for k, v in params.items()}})
    )


def test_a_visit_across_the_new_year_counts_in_its_start_year_on_every_page(built, fm_world, reporting_year):
    """Invariants 1-4 with periods read on the start date: a visit from 30 December 2025 to 3 January
    2026 is 2025's on Monitoring insights, the overview, the field monitoring page, and the partner and
    PD panels; 2026's figures stay those of fm_world."""
    from neurodb.fmm import services as fmm_services
    from tests.fmm.conftest import PD_EDU, _finding

    mercy = fm_world.partners["mercy"]
    _finding(
        990,
        partner=mercy,
        vendor_number=mercy.vendor_number,
        entity=PD_EDU,
        entity_type="PD/SSFA",
        monitoring_activity="FM-2025-099",
        monitoring_activity_id=1799,
        reference_number="FM-2025-099",
        status="completed",
        overall_finding_rating="On Track",
        start_date=datetime.date(2025, 12, 30),
        end_date=datetime.date(2026, 1, 3),
    )
    refresh.run(triggered_by="test", today=TODAY)
    for year, visits in ((2025, 1), (2026, 8)):
        page = services.monitoring({"year": str(year)})
        assert _fmm(year)["visits"] == _overview_visits(reporting_year, year) == page["activities"] == visits
        assert _fmm(year)["entities"] == page["findings"].count()
    panel = fmm_services.partner_summary(mercy.pk, 2025)
    scope = Scope.from_params({"year": "2025", "partner": str(mercy.pk), "section": ""})
    assert panel["visits"] == metrics.kpis(scope)["visits"] == 1
    pd = fm_world.pds["education"]
    pd_panel = fmm_services.pd_summary(pd.pk, 2025)
    assert pd_panel["visits"] == sum(q["visits"] for q in pd_panel["quarters"]) == 1
