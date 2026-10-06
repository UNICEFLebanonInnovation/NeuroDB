"""The independent check of Release 2's stage A: what it found and fixed.

- The monitored entities' "Not monitored" counted only the rows of reported visits (planned, not
  conducted, as for the visits): a blank rating on a planned or in-progress visit is "not rated yet".
- The recurring issues' mean urgency left out the visits without an urgency (not scored) instead of
  counting them as 0, and the Quality tab shows "—" for an issue none of whose visits has one.
"""

from __future__ import annotations

import datetime

import pytest
from django.http import QueryDict

from neurodb.fmm import metrics
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import Visit, VisitRuleResult
from neurodb.fmm.scope import Scope

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def test_entities_not_monitored_are_those_of_reported_visits_only(built):
    k = metrics.kpis(_scope())
    # 1723 (completed): a blank and a "Not Monitored" row; 1724 (data collection): a blank row, not
    # rated yet; 1725 (cancelled): a blank row, in neither count
    assert k["entities_not_monitored"] == 2
    assert k["entities_not_rated_yet"] == 1
    total = k["entities_rated"] + k["entities_not_monitored"] + k["entities_not_rated_yet"]
    assert k["entities_other"] == k["entities"] - total


def test_the_entities_tile_says_how_many_are_not_rated_yet(built, client_viewer):
    html = client_viewer.get("/fmm/", {"year": "2026", "section": ""}).content.decode()
    assert "2 not monitored · 1 not rated yet" in html


def test_an_issues_mean_urgency_leaves_out_the_visits_without_one(built):
    flagged = list(
        VisitRuleResult.objects.filter(status="fail").values_list("rule", "detail_key", "visit__key")
    )
    groups: dict = {}
    for rule, key, visit in flagged:
        groups.setdefault((rule, key), []).append(visit)
    (rule, key), visits = max(groups.items(), key=lambda kv: len(kv[1]))
    assert len(visits) >= 2  # counted as 0, the unscored visit would pull the mean below 40
    Visit.objects.filter(key__in=visits).update(urgency=40)
    Visit.objects.filter(key=visits[0]).update(urgency=None)
    row = next(r for r in metrics.top_issues(_scope(), 50) if r["drill"] == f"{rule}:{key}")
    assert row["urgency"] == 40
    Visit.objects.filter(key__in=visits).update(urgency=None)
    metrics.cache.clear()
    row = next(r for r in metrics.top_issues(_scope(), 50) if r["drill"] == f"{rule}:{key}")
    assert row["urgency"] is None
