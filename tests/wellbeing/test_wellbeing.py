"""Makani wellbeing: reading BMA's flags and summaries, the pages, and follow-ups sent to BMA."""

import copy

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.core.models import SyncRun
from neurodb.integrations.http import IntegrationError
from neurodb.wellbeing import sync
from neurodb.wellbeing.models import CenterSummary, Flag, SyncState


def flag(id_, kind="A1", status="open", **kw):
    return {
        "id": id_,
        "registration": 5000 + id_,
        "kind": kind,
        "kind_label": {
            "A1": "Absent several class days in a row",
            "P1": "Protection concern recorded, not referred",
        }[kind],
        "status": status,
        "urgent": kind == "P1",
        "priority": False,
        "reason": "Absent 3 class days in a row",
        "evidence": {},
        "as_of": "2026-09-25",
        "opened_on": "2026-09-26",
        "last_seen_on": "2026-09-30",
        "resolved_on": None,
        "follow_up": None,
        "center": {"id": 7, "name": "Center A"},
        "partner": {"id": 3, "name": "Makani Partner"},
        "round": {"id": 1, "name": "Round 1 2026"},
        "child": {"gender": "Female", "age_band": "10-14", "nationality": "Syrian"},
        "bma_path": f"/mscc/child-profile/{5000 + id_}/",
        "modified": f"2026-09-30T02:00:0{id_}",
        **kw,
    }


SUMMARY = {
    "center": {"id": 7, "name": "Center A", "governorate": "Akkar"},
    "partner": {"id": 3, "name": "Makani Partner"},
    "round": {"id": 1, "name": "Round 1 2026"},
    "computed_at": "2026-09-30T02:10:00",
    "figures": {
        "children": 120,
        "dropouts": 4,
        "attendance": {"rate_28_days": 81.5, "sheets": 40, "sheets_all_present": 6},
        "flags": {
            "children_flagged": 9,
            "open": 11,
            "urgent_open": 1,
            "open_by_kind": {"A1": 6, "P1": 1},
            "opened_this_month": 12,
            "followed_up_this_month": 5,
            "followed_up_on_time": 4,
            "median_days_to_follow_up": 3,
            "open_longer_than_target": 2,
        },
        "services": {"required": 300, "completed": 240, "core_children_without_checklist": 2},
        "learning": {"assessed": 50, "no_progress": 7},
    },
}


class FakeBMA:
    def __init__(self, flags):
        self.flags = flags
        self.calls, self.posted = [], []
        self.answer = None

    def start_run(self, kind, payload=None):
        self.calls.append(("calculate", kind))
        return {"id": 1, "status": "succeeded"}

    def wellbeing_flags(self, modified_since=None, after=None, limit=500):
        self.calls.append((modified_since, after))
        rows = [
            f
            for f in self.flags
            if (after is None or f["id"] > after)
            and (modified_since is None or f["modified"] >= modified_since)
        ][:2]
        more = [f for f in self.flags if f["id"] > rows[-1]["id"]] if rows else []
        return {
            "flags": rows,
            "next_after": rows[-1]["id"] if more else None,
            "kinds": {
                "A1": "Absent several class days in a row",
                "P1": "Protection concern recorded, not referred",
            },
            "results": {"referred": "Referred"},
            "settings": {"followup_days": 7, "absence_streak": 3},
        }

    def wellbeing_summaries(self, month=None):
        return {
            "month": month or "2026-09-01",
            "months": ["2026-09-01", "2026-08-01"],
            "summaries": [SUMMARY],
        }

    def wellbeing_follow_up(self, flag_id, values):
        self.posted.append((flag_id, values))
        if self.answer:
            return self.answer
        done = copy.deepcopy(next(f for f in self.flags if f["id"] == flag_id))
        done.update(
            status="followed_up",
            follow_up={
                "on": values["followed_up_on"],
                "type": values["follow_up_type"],
                "result": values["result"],
                "result_label": "Referred",
                "note": values["note"],
                "by": values["by"],
            },
        )
        return 200, {"flag": done}


@pytest.fixture
def bma(db):
    return FakeBMA([flag(1), flag(2, "P1"), flag(3)])


def test_sync_reads_every_page_then_only_changes(bma):
    run = sync.sync(client=bma)
    assert run.status == SyncRun.Status.SUCCEEDED and Flag.objects.count() == 3
    assert bma.calls[0] == ("calculate", "wellbeing")  # BMA works the flags out first
    assert [c[1] for c in bma.calls[1:]] == [None, 2]  # two pages
    f = Flag.objects.get(bma_id=2)
    assert (f.registration, f.urgent, f.child_age_band, f.center_name) == (5002, True, "10-14", "Center A")
    assert CenterSummary.objects.filter(month="2026-09-01").get().figures["children"] == 120
    assert CenterSummary.objects.count() == 2  # both months
    state = SyncState.current()
    assert state.flags_modified_since == "2026-09-30T02:00:03" and state.settings["followup_days"] == 7
    bma.calls.clear()
    sync.sync(client=bma, calculate_first=False)
    assert bma.calls[0][0] == "2026-09-30T02:00:03"


def test_a_failing_bma_fails_the_run(db):
    class Down(FakeBMA):
        def wellbeing_flags(self, **kw):
            raise IntegrationError("down")

    run = sync.sync(client=Down([]))
    assert run.status == SyncRun.Status.FAILED and "down" in run.error


@pytest.fixture
def editor(db, roles):
    user = User.objects.create_user(
        username="editor",
        email="e@example.org",
        password="editor-pass-123456",
        first_name="Ana",
        last_name="Officer",
    )
    user.groups.add(Group.objects.get(name=SECTION_EDITOR))
    return user


def test_everyone_sees_the_centre_summaries_but_only_editors_the_flags(client_viewer, bma):
    sync.sync(client=bma)
    page = client_viewer.get(reverse("wellbeing:summaries"))
    assert page.status_code == 200 and b"Center A" in page.content
    assert page.context["total"]["flagged"] == 9
    assert client_viewer.get(reverse("wellbeing:flags")).status_code == 403
    flag_id = Flag.objects.first().pk
    assert client_viewer.get(reverse("wellbeing:follow_up", args=[flag_id])).status_code == 403


def test_editors_see_flags_by_registration_number_and_open_them_in_bma(client, editor, bma, settings):
    settings.COMPILER_API_URL = "https://bma.example.org"
    sync.sync(client=bma)
    client.force_login(editor)
    page = client.get(reverse("wellbeing:flags"))
    assert page.status_code == 200 and page.context["counts"]["open"] == 3
    assert b"5002" in page.content and b"https://bma.example.org/mscc/child-profile/5002/" in page.content
    urgent = client.get(reverse("wellbeing:flags"), {"urgent": "1"})
    assert [f.bma_id for f in urgent.context["page"]] == [2]


def test_a_follow_up_is_saved_in_bma_and_here(client, editor, bma, monkeypatch):
    sync.sync(client=bma)
    monkeypatch.setattr("neurodb.youth.compiler.CompilerClient", lambda: bma)
    client.force_login(editor)
    local = Flag.objects.get(bma_id=1)
    url = reverse("wellbeing:follow_up", args=[local.pk])
    assert client.get(url).status_code == 200
    today = timezone.localdate().isoformat()
    response = client.post(
        url, {"follow_up_type": "Phone call", "result": "referred", "followed_up_on": today, "note": "Called"}
    )
    assert response.status_code == 302
    assert bma.posted == [
        (
            1,
            {
                "follow_up_type": "Phone call",
                "result": "referred",
                "followed_up_on": today,
                "note": "Called",
                "by": "Ana Officer",
            },
        )
    ]
    local.refresh_from_db()
    assert (
        local.status == "followed_up"
        and local.followed_up_by == editor
        and local.followed_up_by_name == "Ana Officer"
    )


def test_bma_refusals_and_outages_are_shown(client, editor, bma, monkeypatch):
    sync.sync(client=bma)
    monkeypatch.setattr("neurodb.youth.compiler.CompilerClient", lambda: bma)
    client.force_login(editor)
    local = Flag.objects.get(bma_id=3)
    url = reverse("wellbeing:follow_up", args=[local.pk])
    data = {
        "follow_up_type": "Phone call",
        "result": "referred",
        "followed_up_on": timezone.localdate().isoformat(),
    }
    bma.answer = (400, {"result": ["Not a valid choice."]})
    response = client.post(url, data)
    assert response.status_code == 200 and b"Not a valid choice." in response.content
    closed = dict(flag(3), status="resolved", resolved_on="2026-09-30")
    bma.answer = (409, {"detail": "no longer open", "flag": closed})
    client.post(url, data)
    local.refresh_from_db()
    assert local.status == "resolved"

    def down(*a):
        raise IntegrationError("down")

    other = Flag.objects.get(bma_id=1)
    bma.wellbeing_follow_up = down
    response = client.post(reverse("wellbeing:follow_up", args=[other.pk]), data, follow=True)
    assert b"BMA could not be reached" in response.content
    other.refresh_from_db()
    assert other.status == "open"


def test_a_date_before_the_flag_or_in_the_future_is_refused(client, editor, bma, monkeypatch):
    sync.sync(client=bma)
    monkeypatch.setattr("neurodb.youth.compiler.CompilerClient", lambda: bma)
    client.force_login(editor)
    url = reverse("wellbeing:follow_up", args=[Flag.objects.get(bma_id=1).pk])
    response = client.post(
        url, {"follow_up_type": "Phone call", "result": "referred", "followed_up_on": "2026-09-01"}
    )
    assert response.status_code == 200 and bma.posted == []
