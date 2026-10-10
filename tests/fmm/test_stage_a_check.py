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
from neurodb.fmm.models import RecordRuleResult, VisitEntity
from neurodb.fmm.scope import Scope

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def test_records_not_monitored_are_those_of_reported_visits_only(built):
    k = metrics.kpis(_scope())
    # 1723 (completed): a blank and a "Not Monitored" record; 1724 (data collection): a blank record, not
    # rated yet; 1725 (cancelled): a blank record, in neither count
    assert k["records_not_monitored"] == 2
    assert k["records_not_rated_yet"] == 1
    total = k["records_rated"] + k["records_not_monitored"] + k["records_not_rated_yet"]
    assert k["records_other"] == k["records"] - total


def test_the_entities_tile_says_how_many_are_not_rated_yet(built, client_viewer):
    html = client_viewer.get("/fmm/", {"year": "2026", "section": ""}).content.decode()
    assert "2 not monitored · 1 not rated yet" in html


def test_an_issues_mean_urgency_leaves_out_the_records_without_one(built):
    # an issue's mean urgency is its records' (each record has its own urgency)
    flagged = list(
        RecordRuleResult.objects.filter(status="fail").values_list("rule", "detail_key", "entity_id")
    )
    groups: dict = {}
    for rule, key, record in flagged:
        groups.setdefault((rule, key), []).append(record)
    (rule, key), records = max(groups.items(), key=lambda kv: len(kv[1]))
    assert len(records) >= 2  # counted as 0, the record without urgency would pull the mean below 40
    VisitEntity.objects.filter(pk__in=records).update(urgency=40)
    VisitEntity.objects.filter(pk=records[0]).update(urgency=None)
    metrics.cache.clear()
    row = next(r for r in metrics.top_issues(_scope(), 50) if r["drill"] == f"{rule}:{key}")
    assert row["urgency"] == 40
    VisitEntity.objects.filter(pk__in=records).update(urgency=None)
    metrics.cache.clear()
    row = next(r for r in metrics.top_issues(_scope(), 50) if r["drill"] == f"{rule}:{key}")
    assert row["urgency"] is None
