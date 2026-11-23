"""The action point history (``datamart.ActionPointStatus``, kept by ``datamart.ap_history``): a small record of
each eTools action point's status, kept from now on. A first row the day a point is first seen, then a row only on a
day its status, priority, due date or completion date changed, or it left the Datamart, or came back. Recorded
after every Datamart sync that refreshed the action points, and by its own job (a Run a job button). It holds no
text and no names: counts, dates, references and statuses only."""

from __future__ import annotations

import datetime
from io import StringIO

import pytest
from django.contrib import admin
from django.core.management import CommandError, call_command
from django.db import connection
from django.http import QueryDict
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.core.models import SyncRun
from neurodb.datamart import ap_history
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

day = datetime.date
DAY1 = day(2026, 10, 15)  # the suite's day (tests/conftest.py)
DAY2, DAY3, DAY4 = day(2026, 10, 16), day(2026, 10, 17), day(2026, 10, 19)
SUCCEEDED, PARTIAL, FAILED = SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL, SyncRun.Status.FAILED
# made-up texts and names an action point holds: none may reach the history or its run
PLANTED = (
    "Plantedperson",
    "Planted Partner Org",
    "Plantedtown",
    "fence of the planted site",
    "planted.invalid",
)
ALLOWED = {
    "id",
    "source_id",
    "day",
    "in_datamart",
    "status",
    "high_priority",
    "due_date",
    "date_of_completion",
    "related_module",
    "section",
    "engagement_source_id",
    "tpm_activity_source_id",
    "intervention",
    "partner",
}


def point(source_id, **extra) -> dm.ActionPoint:
    fields = {
        "datamart_id": 5000 + (source_id or 0),
        "source_id": source_id,
        "reference_number": f"LEB/2026/{source_id}/APD",
        "description": "Mend the fence of the planted site",
        "assigned_to_name": "Plantedperson Example",
        "partner_name": "Planted Partner Org",
        "location_name": "Plantedtown",
        "category": "Write to someone@planted.invalid",
        "office": "Beirut",
        "status": "open",
        "due_date": day(2026, 11, 30),
        "related_module": "audit",
        "section": "Education",
    }
    fields.update(extra)
    return dm.ActionPoint.objects.create(**fields)


def change(source_id, **fields) -> None:
    dm.ActionPoint.objects.filter(source_id=source_id).update(**fields)


def rows(source_id=None, *fields):
    found = dm.ActionPointStatus.objects.order_by("source_id", "day")
    if source_id is not None:
        found = found.filter(source_id=source_id)
    return list(found.values_list(*(fields or ("source_id", "day", "in_datamart", "status"))))


def history_runs():
    return SyncRun.objects.filter(job=SyncRun.Job.AP_HISTORY)


def _no_planted_text(*values) -> None:
    text = repr(values)
    for planted in PLANTED:
        assert planted not in text


# ------------------------------------------------------------------------------------------------- the model
def test_the_model_keeps_only_the_allowed_fields_and_no_text_or_name():
    fields = {f.name for f in dm.ActionPointStatus._meta.get_fields()}
    assert fields == ALLOWED
    for name in fields:
        for word in (
            "name",
            "description",
            "assign",
            "email",
            "phone",
            "location",
            "comment",
            "author",
            "office",
        ):
            assert word not in name, name
    # one row per action point and day, the action point by its lasting eTools id
    assert any(set(c.fields) == {"source_id", "day"} for c in dm.ActionPointStatus._meta.constraints), (
        "a point has one row a day"
    )
    assert type(dm.ActionPointStatus._meta.get_field("day")).__name__ == "DateField"  # what readiness reads


# ------------------------------------------------------------------------------------------- what is recorded
def test_the_first_record_keeps_each_point_once_with_its_status_and_no_text(site_today):
    partner = PartnerOrganization.objects.create(
        etl_id="9001", name="Planted Partner Org", vendor_number="EX1"
    )
    pd = PCA.objects.create(etl_id="9101", number="EX/PCA2026001/PD2026001", title="Example", partner=partner)
    done = datetime.datetime(2026, 10, 2, 8, 30, tzinfo=datetime.UTC)
    point(501, high_priority=True, intervention=pd, partner=partner, engagement_source_id=77)
    point(502, status="completed", date_of_completion=done, related_module="tpm", tpm_activity_source_id=88)
    dm.ActionPoint.objects.create(datamart_id=9, source_id=None, status="open")  # no eTools id: not kept

    run = ap_history.run("test")

    found = list(dm.ActionPointStatus.objects.order_by("source_id").values())
    assert [r["source_id"] for r in found] == [501, 502]
    first, second = ({k: v for k, v in r.items() if k != "id"} for r in found)
    assert first == {
        "source_id": 501,
        "day": DAY1,
        "in_datamart": True,
        "status": "open",
        "high_priority": True,
        "due_date": day(2026, 11, 30),
        "date_of_completion": None,
        "related_module": "audit",
        "section": "Education",
        "engagement_source_id": 77,
        "tpm_activity_source_id": None,
        "intervention_id": pd.pk,
        "partner_id": partner.pk,
    }
    assert second["status"] == "completed" and second["date_of_completion"] == done
    assert second["tpm_activity_source_id"] == 88 and second["intervention_id"] is None
    assert run.job == SyncRun.Job.AP_HISTORY and run.status == SUCCEEDED and run.triggered_by == "test"
    assert run.rows_in == 3 and run.rows_written == 2
    assert run.details["points"] == 3 and run.details["without_etools_id"] == 1
    assert run.details["first_seen"] == 2 and run.details["added"] == 2 and run.details["day"] == "2026-10-15"
    _no_planted_text(found, run.details, run.error)


def test_a_day_with_no_change_writes_no_row(site_today):
    point(501)
    ap_history.run("test")
    site_today.set(DAY2)
    run = ap_history.run("test")
    assert rows() == [(501, DAY1, True, "open")]
    assert run.rows_written == 0 and run.details["unchanged"] == 1 and run.details["added"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "completed"),
        ("high_priority", True),
        ("due_date", day(2026, 12, 31)),
        ("date_of_completion", datetime.datetime(2026, 10, 16, 10, 0, tzinfo=datetime.UTC)),
    ],
)
def test_each_kind_of_change_writes_a_row_that_day(site_today, field, value):
    point(501)
    ap_history.run("test")
    site_today.set(DAY2)
    change(501, **{field: value})
    run = ap_history.run("test")
    kept = list(dm.ActionPointStatus.objects.filter(source_id=501).order_by("day"))
    assert [r.day for r in kept] == [DAY1, DAY2]
    assert getattr(kept[0], field) != value and getattr(kept[1], field) == value
    assert run.details["changed"] == 1 and run.rows_written == 1


def test_a_change_to_the_section_the_module_the_links_or_the_text_alone_writes_no_row(site_today):
    point(501)
    ap_history.run("test")
    site_today.set(DAY2)
    change(
        501,
        section="Health",
        related_module="tpm",
        engagement_source_id=5,
        description="Another text",
        assigned_to_name="Another Plantedperson",
    )
    assert ap_history.run("test").rows_written == 0
    assert rows() == [(501, DAY1, True, "open")]


def test_a_point_that_leaves_the_datamart_and_comes_back(site_today):
    point(501, status="in_progress")
    point(502)
    ap_history.run("test")

    site_today.set(DAY2)
    dm.ActionPoint.objects.filter(
        source_id=501
    ).delete()  # the sync removes a point the Datamart no longer returns
    run = ap_history.run("test")
    gone = dm.ActionPointStatus.objects.get(source_id=501, day=DAY2)
    assert gone.in_datamart is False and gone.status == "in_progress"  # with its last known status
    assert run.details["gone"] == 1 and run.rows_written == 1

    site_today.set(DAY3)
    assert ap_history.run("test").rows_written == 0  # still gone: nothing new

    site_today.set(DAY4)
    point(501, datamart_id=9501, status="in_progress")  # back, under another Datamart id
    run = ap_history.run("test")
    assert run.details["returned"] == 1 and run.rows_written == 1
    assert rows(501) == [
        (501, DAY1, True, "in_progress"),
        (501, DAY2, False, "in_progress"),
        (501, DAY4, True, "in_progress"),
    ]
    assert rows(502) == [(502, DAY1, True, "open")]


def test_running_twice_on_one_day_writes_nothing_new(site_today):
    point(501)
    point(502)
    ap_history.run("test")
    site_today.set(DAY2)
    change(501, status="completed")
    dm.ActionPoint.objects.filter(source_id=502).delete()
    ap_history.run("test")
    kept = list(dm.ActionPointStatus.objects.order_by("pk").values())
    again = ap_history.run("test")
    assert list(dm.ActionPointStatus.objects.order_by("pk").values()) == kept
    assert again.rows_written == 0 and again.status == SUCCEEDED


def test_a_second_change_the_same_day_updates_that_days_row_and_a_change_undone_leaves_none(site_today):
    point(501)
    ap_history.run("test")
    site_today.set(DAY2)
    change(501, status="in_progress")
    ap_history.run("test")
    change(501, status="completed")  # later the same day (a sync after a press of the button)
    run = ap_history.run("test")
    assert rows(501) == [(501, DAY1, True, "open"), (501, DAY2, True, "completed")]
    assert run.details["updated"] == 1 and run.details["added"] == 0 and run.rows_written == 1
    change(501, status="open")  # back where it was the day before, the same day: the day holds no change
    run = ap_history.run("test")
    assert rows(501) == [(501, DAY1, True, "open")]
    assert run.details["removed"] == 1 and run.rows_written == 1


def test_an_empty_table_never_marks_every_point_gone(site_today):
    point(501)
    ap_history.run("test")
    site_today.set(DAY2)
    dm.ActionPoint.objects.all().delete()
    run = ap_history.run("test")
    assert rows() == [(501, DAY1, True, "open")]
    assert run.details["nothing_held"] is True and run.rows_written == 0 and run.status == SUCCEEDED


def test_a_point_the_datamart_returns_twice_is_kept_once_as_last_changed(site_today):
    earlier = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
    later = datetime.datetime(2026, 10, 10, tzinfo=datetime.UTC)
    point(501, datamart_id=1, status="open", last_modify_date=earlier)
    point(501, datamart_id=2, status="completed", last_modify_date=later)
    run = ap_history.run("test")
    assert rows() == [(501, DAY1, True, "completed")]
    assert run.details["repeated"] == 1 and run.details["points"] == 2


def test_the_points_counted_are_those_the_action_points_page_lists(site_today):
    """The run's count of points is the action points page's, with no filter: the same query."""
    from neurodb.datamart import services

    point(501)
    point(502, status="completed")
    dm.ActionPoint.objects.create(datamart_id=9, source_id=None, status="open")
    run = ap_history.run("test")
    listed, _visit, _filters = services.filtered_action_points(QueryDict(""))
    assert run.details["points"] == listed.count() == 3


def test_the_record_reads_in_a_constant_number_of_queries_whatever_the_number_of_points(site_today):
    def queries(new: int, offset: int) -> int:
        dm.ActionPoint.objects.bulk_create(
            [dm.ActionPoint(datamart_id=offset + n, source_id=offset + n, status="open") for n in range(new)]
        )
        with CaptureQueriesContext(connection) as ctx:
            ap_history.record(timezone.localdate())
        return len(ctx.captured_queries)

    few = queries(3, 1000)
    site_today.set(DAY2)
    many = queries(120, 2000)  # 3 unchanged and 120 first seen
    site_today.set(DAY3)
    change_all = dm.ActionPoint.objects.update(status="completed")
    with CaptureQueriesContext(connection) as ctx:
        counts = ap_history.record(timezone.localdate())
    assert few == many == len(ctx.captured_queries)
    assert change_all == counts["changed"] == 123


def test_a_second_run_while_one_is_going_does_nothing(site_today, monkeypatch):
    point(501)
    monkeypatch.setattr(ap_history, "_try_lock", lambda: False)
    assert ap_history.run("test") == ap_history.ANOTHER_RUN
    assert not history_runs().exists() and not dm.ActionPointStatus.objects.exists()
    out = StringIO()
    call_command("record_action_point_status", stdout=out)
    assert ap_history.ANOTHER_RUN in out.getvalue()


# ------------------------------------------------------------------------------- when it runs: after the sync
@pytest.fixture
def datamart_sync(monkeypatch, settings):
    """``sync_etools_datamart`` with its sync replaced: ``targets`` are the datasets it says it synced, each with
    its outcome."""
    from neurodb.integrations.management.commands import sync_etools_datamart as sync_command

    settings.FMM_REFRESH_AFTER_SYNC = "off"
    outcome: dict[str, str] = {}

    def fake_sync_all(only=None, triggered_by="command"):
        runs = []
        for target, status in outcome.items():
            run = SyncRun.objects.create(
                job=SyncRun.Job.ETOOLS_DATAMART, target=target, triggered_by=triggered_by
            )
            run.finish(status)
            runs.append(run)
        return runs

    monkeypatch.setattr(sync_command, "sync_all", fake_sync_all)

    def run(**targets) -> str:
        outcome.clear()
        outcome.update(targets)
        out = StringIO()
        call_command("sync_etools_datamart", stdout=out)
        return out.getvalue()

    return run


def test_the_status_is_recorded_after_each_sync_that_refreshed_the_action_points(
    datamart_sync, site_today, monkeypatch
):
    from neurodb.integrations import background

    point(501)
    inner, held = ap_history.record, []

    def watched(today=None):
        held.append(background.datamart_lock_is_held())
        return inner(today)

    monkeypatch.setattr(ap_history, "record", watched)
    datamart_sync(grants=SUCCEEDED)  # the action points were not synced
    assert not history_runs().exists()
    with pytest.raises(CommandError):
        datamart_sync(action_points=FAILED)  # nothing was refreshed
    assert not history_runs().exists()

    out = datamart_sync(grants=SUCCEEDED, action_points=SUCCEEDED)
    run = history_runs().get()
    assert held == [False]  # once the sync's lock is released
    assert run.status == SUCCEEDED and run.triggered_by == "command" and rows() == [(501, DAY1, True, "open")]
    assert "Action point status record [] succeeded" in out

    site_today.set(DAY2)
    change(501, status="completed")
    datamart_sync(action_points=PARTIAL)  # some records failed, the others were written: recorded too
    assert history_runs().count() == 2 and rows(501)[-1] == (501, DAY2, True, "completed")


def test_a_failed_record_never_fails_the_sync(datamart_sync, site_today, monkeypatch):
    point(501)
    monkeypatch.setattr(ap_history, "record", lambda today=None: 1 / 0)
    out = datamart_sync(action_points=SUCCEEDED)  # no CommandError
    run = history_runs().get()
    assert run.status == FAILED and "ZeroDivisionError" in run.error
    assert "Action point status record [] failed" in out


def _night(site_today, monkeypatch, night: datetime.date, ends: datetime.datetime) -> None:
    """The nightly sync of ``night``: it starts at 20:30 that evening, refreshes the action points and ends at
    ``ends`` (after midnight, the next day), when the status is recorded."""
    from neurodb.integrations.management.commands import sync_etools_datamart as sync_command

    started = timezone.make_aware(datetime.datetime.combine(night, datetime.time(20, 30)))

    def fake_sync_all(only=None, triggered_by="command"):
        runs = []
        for target in ("partners", "action_points"):
            run = SyncRun.objects.create(
                job=SyncRun.Job.ETOOLS_DATAMART, target=target, triggered_by=triggered_by, started_at=started
            )
            run.finish(SUCCEEDED)
            runs.append(run)
        return runs

    monkeypatch.setattr(sync_command, "sync_all", fake_sync_all)
    site_today.set(ends)
    call_command("sync_etools_datamart", stdout=StringIO())


def test_a_sync_that_ends_after_midnight_records_under_the_night_it_read(site_today, monkeypatch, settings):
    """Each night's read is filed under the day its sync started: two nightly reads never share a day, so a point
    completed one night and reopened the next keeps both changes, even when a sync ends after midnight."""
    settings.FMM_REFRESH_AFTER_SYNC = "off"
    point(501)
    _night(site_today, monkeypatch, day(2026, 10, 14), datetime.datetime(2026, 10, 14, 23, 0))
    change(501, status="completed")
    _night(
        site_today, monkeypatch, day(2026, 10, 15), datetime.datetime(2026, 10, 16, 0, 20)
    )  # after midnight
    change(501, status="open")
    _night(site_today, monkeypatch, day(2026, 10, 16), datetime.datetime(2026, 10, 16, 23, 0))
    assert rows(501) == [
        (501, day(2026, 10, 14), True, "open"),
        (501, day(2026, 10, 15), True, "completed"),
        (501, day(2026, 10, 16), True, "open"),
    ]
    assert [r.details["day"] for r in history_runs().order_by("pk")] == [
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
    ]


def test_the_button_files_the_record_under_the_day_the_action_points_were_read(site_today):
    """Run a job → Record action point status, pressed the morning after a sync that read the action points late
    the night before, files them under that night; with no read on record, under today."""
    point(501)
    call_command("record_action_point_status", stdout=StringIO())
    assert rows(501) == [(501, DAY1, True, "open")]  # no read on record: today
    night = timezone.make_aware(datetime.datetime(2026, 10, 16, 23, 50))
    read = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="action_points", started_at=night)
    read.finish(SUCCEEDED)
    failed = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="action_points",
        started_at=night + datetime.timedelta(hours=8),
    )
    failed.finish(FAILED)  # a read that failed wrote nothing: not the day of the data
    change(501, status="completed")
    site_today.set(datetime.datetime(2026, 10, 17, 8, 0))
    call_command("record_action_point_status", stdout=StringIO())
    assert rows(501) == [(501, DAY1, True, "open"), (501, DAY2, True, "completed")]
    assert history_runs().order_by("-pk").first().details["day"] == "2026-10-16"


def test_the_guides_say_what_the_history_keeps_and_removes():
    """Only the same day's row is ever removed, and nothing measures closing times yet: the guides say so."""
    from pathlib import Path

    from neurodb.help import guide

    root = Path(__file__).resolve().parents[2]
    help_text = " ".join((guide.GUIDE_DIR / "action-points.md").read_text().split())
    assert "it will show" not in help_text
    assert (
        "It is kept so that, later, NeuroDB could measure how long action points take to close" in help_text
    )
    operations = " ".join((root / "docs" / "OPERATIONS.md").read_text().split())
    assert "Rows are never deleted." not in operations
    assert (
        "Rows of earlier days are never deleted: only the same day's row goes, when its change is undone that day."
        in operations
    )
    assert "never deleted" not in (dm.ActionPointStatus.__doc__ or "").replace("is never deleted", "")


# ------------------------------------------------------------------------------------- its job and its button
def test_the_job_command_records_its_run_with_counts_only(site_today):
    point(501)
    out = StringIO()
    call_command("record_action_point_status", "--triggered-by", "someone", stdout=out)
    run = history_runs().get()
    assert run.triggered_by == "someone" and run.status == SUCCEEDED
    assert run.rows_in == 1 and run.rows_written == 1
    assert set(run.details) == {
        "day",
        "points",
        "without_etools_id",
        "repeated",
        "first_seen",
        "changed",
        "gone",
        "returned",
        "unchanged",
        "added",
        "updated",
        "removed",
    }
    assert all(isinstance(v, int) for k, v in run.details.items() if k != "day")
    assert "Action point status record [] succeeded" in out.getvalue()
    call_command("record_action_point_status", stdout=StringIO())
    assert history_runs().order_by("-pk").first().rows_written == 0


def test_the_job_is_a_scheduler_command_and_a_run_a_job_button():
    from neurodb.core.admin_jobs import BACKGROUND_JOBS
    from neurodb.core.jobs import COMMANDS
    from neurodb.integrations import background

    command = COMMANDS["ap_history"]
    assert command.args == ("record_action_point_status",) and command.sync_job == SyncRun.Job.AP_HISTORY
    assert str(command.label) == "Record action point status"
    (button,) = [spec for spec in BACKGROUND_JOBS if spec.command == command.args]
    assert str(button.menu) == str(button.title) == "Record action point status"
    assert button.job == SyncRun.Job.AP_HISTORY
    assert background.LOCK_IDS[SyncRun.Job.AP_HISTORY] == ap_history.LOCK_ID
    assert len(set(background.LOCK_IDS.values())) == len(background.LOCK_IDS)


def test_the_run_asks_no_hub_rebuild_and_is_no_data_health_card(settings, site_today):
    from neurodb.graph.models import RefreshRequest
    from neurodb.reports import services

    settings.KNOWLEDGE_HUB_ON_NEW_DATA = True
    point(501)
    ap_history.run("test")
    assert not RefreshRequest.objects.exists()  # it brings no new data
    assert SyncRun.Job.AP_HISTORY in services.HEALTH_SKIPPED_JOBS  # worked out from the synced data


# --------------------------------------------------------------------------------------- the admin list
def test_the_admin_lists_the_history_read_only(client, roles, site_today):
    point(501, status="in_progress")
    ap_history.run("test")
    superuser = User.objects.create_superuser(
        username="owner", password="pass-123456-x", email="owner@example.org"
    )
    client.force_login(superuser)
    url = reverse("admin:datamart_actionpointstatus_changelist")
    html = client.get(url).content.decode()
    assert "501" in html and "in_progress" in html
    _no_planted_text(html.split("<main", 1)[-1])
    model_admin = admin.site._registry[dm.ActionPointStatus]
    assert {"day", "status", "related_module"} <= {
        f if isinstance(f, str) else f[0] for f in model_admin.list_filter
    }
    request = type("R", (), {"user": superuser})()
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request) and not model_admin.has_delete_permission(request)
    assert client.get(reverse("admin:datamart_actionpointstatus_add")).status_code == 403
    for filtered in ("?status=in_progress", "?related_module=audit", f"?day={DAY1.isoformat()}", "?q=abc"):
        assert client.get(url + filtered).status_code == 200
    assert "501" in client.get(url + "?q=501").content.decode()  # found by its eTools id


# ----------------------------------------------------------------------------------------------- migrations
def test_the_migrations_are_quick_and_listed():
    from django.db.migrations.loader import MigrationLoader

    from tests.datamart.test_backfill import QUICK_MIGRATIONS

    assert ("datamart", "0022_action_point_status", "CreateModel") in QUICK_MIGRATIONS
    assert ("core", "0035_ap_history_job", "AlterField") in QUICK_MIGRATIONS
    loader = MigrationLoader(None, ignore_no_migrations=True)
    table = loader.disk_migrations[("datamart", "0022_action_point_status")].operations
    choice = loader.disk_migrations[("core", "0035_ap_history_job")].operations
    assert [type(op).__name__ for op in table] == ["CreateModel"]
    assert [type(op).__name__ for op in choice] == ["AlterField"]


# ------------------------------------------------------------------------------------ what readiness reads
def test_readiness_counts_the_days_kept_from_the_first_day(site_today):
    from neurodb.insights import readiness

    site_today.set(day(2026, 10, 1))
    point(501)
    ap_history.run("test")
    site_today.set(DAY1)  # nothing changed since: no row, the history is kept all the same
    assert ap_history.run("test").rows_written == 0
    result = readiness.measure(DAY1)
    metrics = {f"{s['key']}.{m['key']}": m["value"] for s in result["sections"] for m in s["metrics"]}
    assert metrics["arrivals.ap_history_days"] == 15 and metrics["arrivals.ap_history_first"] == "2026-10-01"
