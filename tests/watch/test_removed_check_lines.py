"""NeuroDB Watch after a check was removed from NeuroDB (NeuroDB's own grouping of PD indicators: its targets line,
its nightly job's failed, partial, overdue and never-run lines). An item a previous release opened for one of its
lines closes as changed, "no longer followed: this check was removed from NeuroDB", never as fixed: nobody fixed
anything. Before the change such an item closed as fixed."""

# ruff: noqa: F401, F811 (the fixture "healthy" comes from the module that defines it)
from __future__ import annotations

import pytest

from neurodb.watch.detectors import QUICK, system
from neurodb.watch.models import WatchItem
from neurodb.web import health
from tests.watch.test_system import check, healthy, items

pytestmark = pytest.mark.django_db

REMOVED = [
    "etools_masters_without_target",
    "job_failed:etools_masters",
    "job_partial:etools_masters",
    "job_overdue:etools-masters",
    "job_never_run:etools-masters",
]


def test_the_removed_lines_and_their_close_reason():
    assert system.REMOVED_LINES == frozenset(REMOVED)
    assert system.REMOVED == "no longer followed: this check was removed from NeuroDB"


@pytest.mark.parametrize("key", REMOVED)
def test_an_item_of_a_removed_check_closes_as_no_longer_followed(healthy, monkeypatch, key):
    listed = health.warnings
    # the line as a previous release listed it (neutral text: the line itself is gone)
    monkeypatch.setattr(
        health, "warnings", lambda now=None: [*listed(now), health.Warning(key=key, text="A line.")]
    )
    check()
    item = items()[f"system:{key}"]
    assert item.state == "open"
    monkeypatch.setattr(health, "warnings", listed)  # this release: the line is gone

    outcome = check(minutes=2, mode=QUICK)

    item = items()[f"system:{key}"]
    assert outcome.closed == [item.key] and item.state == "closed"
    assert (item.close_reason, item.close_kind) == (system.REMOVED, WatchItem.CloseKind.CHANGED)
    assert item.story[-1]["text"] == f"Closed: {system.REMOVED}"


def test_any_other_line_that_goes_still_closes_as_fixed(healthy, monkeypatch):
    listed = health.warnings
    monkeypatch.setattr(
        health,
        "warnings",
        lambda now=None: [*listed(now), health.Warning(key="users_without_role", text="A.")],
    )
    check()
    monkeypatch.setattr(health, "warnings", listed)
    check(minutes=2, mode=QUICK)
    item = items()["system:users_without_role"]
    assert (item.close_reason, item.close_kind) == (system.FIXED, WatchItem.CloseKind.RESOLVED)
