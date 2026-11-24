"""NeuroDB Watch after a check was removed from NeuroDB (NeuroDB's own grouping of PD indicators: its targets line,
its nightly job's failed, partial, overdue and never-run lines). An item a previous release opened for one of its
lines closes as changed, "no longer followed: this check was removed from NeuroDB", never as fixed: nobody fixed
anything. Before the change such an item closed as fixed. That holds for the overdue and never-run lines of a
copy of the job's schedule under another key too (core 0036 deleted every schedule of the job), and the closed
card the administrators are then shown still opens: its address, the grouping's admin list the previous release
stored, leads to Partner monitoring, never to a 404."""

# ruff: noqa: F401, F811 (the fixture "healthy" comes from the module that defines it)
from __future__ import annotations

import datetime
import html
import re

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN
from neurodb.watch import routing
from neurodb.watch.detectors import ADMINS, QUICK, SYSTEM, WARNING, record, system
from neurodb.watch.models import WatchItem, WatchReceipt
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


# the overdue and never-run lines of the job's schedule, as the previous release wrote them, under the default key
# and under the key of a copy (core 0036 deleted every schedule of the job, whatever its key)
SCHEDULE_LINES = {
    "job_overdue:{key}": "Scheduled job “{label}” is overdue: it was due 5 Oct 2026, 08:00.",
    "job_never_run:{key}": "Scheduled job “{label}” has never run: it was due 5 Oct 2026, 08:00.",
}
# the removed job's name, in the previous release
OLD_LABEL = "Suggest PD indicators for the eTools master indicators"


@pytest.mark.parametrize("line", list(SCHEDULE_LINES))
@pytest.mark.parametrize("key", ["etools-masters", "etools-masters-copy"])
def test_an_item_of_any_schedule_of_the_removed_job_closes_as_no_longer_followed(
    healthy, monkeypatch, line, key
):
    listed = health.warnings
    removed = health.Warning(key=line.format(key=key), text=SCHEDULE_LINES[line].format(label=OLD_LABEL))
    monkeypatch.setattr(health, "warnings", lambda now=None: [*listed(now), removed])
    check()
    monkeypatch.setattr(health, "warnings", listed)

    check(minutes=2, mode=QUICK)

    item = items()[f"system:{removed.key}"]
    assert (item.close_reason, item.close_kind) == (system.REMOVED, WatchItem.CloseKind.CHANGED)


@pytest.mark.parametrize("line", list(SCHEDULE_LINES))
def test_the_line_of_another_schedule_that_goes_still_closes_as_fixed(healthy, monkeypatch, line):
    """Only the removed job's schedule: another job's line, under any key, closes as fixed when it goes."""
    listed = health.warnings
    other = health.Warning(
        key=line.format(key="etools-masters-copy"), text=SCHEDULE_LINES[line].format(label="eTools sync")
    )
    monkeypatch.setattr(health, "warnings", lambda now=None: [*listed(now), other])
    check()
    monkeypatch.setattr(health, "warnings", listed)

    check(minutes=2, mode=QUICK)

    item = items()[f"system:{other.key}"]
    assert (item.close_reason, item.close_kind) == (system.FIXED, WatchItem.CloseKind.RESOLVED)


# the address the previous release stored on the targets line's item and in its evidence: the grouping's admin
# list, filtered to the groupings without a target, which this release no longer has
OLD_LIST = f"/{settings.ADMIN_URL_PATH}datamart/etoolsmaster/?target=listed"
PARTNER_MONITORING = "/partner-monitoring/"


def test_the_closed_card_of_the_removed_line_opens_partner_monitoring_not_a_404(roles, reporting_year):
    """After the deployment the item of the targets line closes, and the administrators told about it are told
    so (Good to know). The card and its evidence still link to the address the previous release stored. Before
    the fix that address answered 404."""
    admin = User.objects.create_user(
        username="admin", email="admin@example.org", password="x-pass-123456", is_staff=True
    )
    admin.groups.add(Group.objects.get(name=ADMIN))
    today = timezone.localdate()
    item = WatchItem.objects.create(
        key="system:etools_masters_without_target",
        detector=system.ID,
        kind=SYSTEM,
        severity=WARNING,
        scope=ADMINS,
        title="3 groupings have no target for 2026.",
        url=OLD_LIST,
        evidence={
            "source": system.SOURCE,
            "source_job": "",
            "synced_at": "",
            "records": [record("Groupings without a target for 2026", None, 3, OLD_LIST)],
            "numbers": {},
        },
        first_seen_on=today - datetime.timedelta(days=3),
        last_seen_on=today,
        changed_on=today,
        state="closed",
        closed_on=today,
        close_reason=system.REMOVED,
        close_kind=WatchItem.CloseKind.CHANGED,
        story=[{"on": today.isoformat(), "text": f"Closed: {system.REMOVED}"}],
    )
    WatchReceipt.objects.create(
        user=admin,
        item=item,
        first_told_on=today,
        last_told_on=today,
        told_step=routing.CLOSED,
        level=WatchReceipt.Level.GOOD_TO_KNOW,
    )
    client = Client()
    client.force_login(admin)

    page = client.get("/for-you/")

    assert page.status_code == 200
    links = set(re.findall(r'href="([^"]*etoolsmaster[^"]*)"', html.unescape(page.content.decode())))
    assert OLD_LIST in links  # the card links to the stored address
    for link in links:
        response = client.get(link)
        assert response.status_code == 302 and response["Location"] == PARTNER_MONITORING, link
    assert client.get(OLD_LIST, follow=True).status_code == 200  # Partner monitoring itself
