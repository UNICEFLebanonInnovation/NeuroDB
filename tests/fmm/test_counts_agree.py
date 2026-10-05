"""Monitoring insights counts as the overview and the field monitoring page do (§0.4, invariants 1-2):
the visits of a calendar year with no filter equal the overview's field monitoring visits and the
field monitoring page's "Monitoring activities"; the monitored entities equal its "Findings". A row
without an activity reference is the one known difference, and the page's data note says so."""

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
