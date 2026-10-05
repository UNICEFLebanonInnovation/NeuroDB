"""The Monitoring insights refresh: so far its key probe (``fmm_refresh --probe-only``): relink, probe,
choose, one run at a time, recorded as a run with meaningful counts."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.fmm import fields, refresh
from neurodb.fmm.management.commands import fmm_refresh as command
from neurodb.fmm.models import FieldMapping, KeyProbe
from neurodb.integrations import background

pytestmark = pytest.mark.django_db


def _probe(**kw):
    return refresh.run(triggered_by="test", probe_only=True, **kw)


def _command(*args) -> str:
    out = StringIO()
    call_command("fmm_refresh", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def held_elsewhere(db):
    """Another process holds the refresh's lock (a run in progress)."""
    other = connections.create_connection("default")
    with other.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [refresh.LOCK_ID])
    yield
    other.close()


def test_the_probe_relinks_probes_and_chooses(fm_world):
    dm.MonitoringFinding.objects.update(intervention=None, pd_match="")
    run = _probe()
    assert (run.job, run.target, run.status, run.triggered_by) == (
        SyncRun.Job.FMM_REFRESH,
        "probe",
        SyncRun.Status.SUCCEEDED,
        "test",
    )
    # 1: the PD written in the LEBA/ form is linked to the amendment that covers the visit date
    leba = dm.MonitoringFinding.objects.get(datamart_id=101)
    assert (leba.intervention, leba.pd_match) == (fm_world.pds["amended"], "token")
    assert run.details["pd_relinked"]["token"] >= 1
    # 2 and 3
    assert KeyProbe.objects.filter(dataset="field_monitoring").exists()
    assert FieldMapping.objects.count() == sum(len(f) for f in fields.CANDIDATES.values())
    total = sum(run.details["datasets"][d]["records"] for d in fields.DATASETS)
    assert run.rows_in == total and run.rows_failed == 0
    assert run.rows_written == KeyProbe.objects.count()
    assert run.details["duration_ms"] >= 0


def test_the_run_details_hold_the_rates(fm_world):
    details = _probe().details
    findings = dm.MonitoringFinding.objects.count()
    assert details["activity_ids"] == {
        "rows": findings,
        "with_id": findings - 1,
        "share": round((findings - 1) / findings, 4),
    }
    pd = details["pd_resolved"]
    assert pd["pd_kind_rows"] == pd["exact"] + pd["token"] + pd["base"] + pd["title"] + pd["unresolved"]
    assert pd["token"] >= 1  # the LEBA/ form
    questions = details["questions"]
    assert questions["records"] == dm.DatamartDocument.objects.filter(dataset="fm_questions").count()
    assert questions["unanswered_seen"] is True and 0 < questions["answered_share"] < 1
    assert "field_monitoring.sections" in details["fields_not_found"]
    ratings = {raw: (code, n) for raw, code, n in details["ratings_seen"]}
    assert ratings["Off Track"] == ("off_track", 2) and ratings["Not Monitored"][0] == "not_monitored"
    statuses = {raw: code for raw, code, _ in details["statuses_seen"]}
    assert statuses["data_collection"] == "data_collection" and statuses["cancelled"] == "cancelled"
    kinds = {raw: code for raw, code, _ in details["entity_types_seen"]}
    assert kinds["CP Output"] == "cp_output" and kinds["PD/SSFA"] == "pd" and kinds["Partner"] == "partner"


def test_unknown_ratings_and_statuses_are_listed(fm_world):
    dm.MonitoringFinding.objects.filter(datamart_id=103).update(overall_finding_rating="Partially fine")
    dm.MonitoringFinding.objects.filter(datamart_id=141).update(status="in review")
    details = _probe().details
    assert details["ratings_unknown"] == {"Partially fine": 1}
    assert details["statuses_unknown"] == {"in review": 1}


def test_two_probes_give_the_same_rows(fm_world):
    _probe()
    first = list(
        KeyProbe.objects.order_by("dataset", "key").values_list("dataset", "key", "records", "types")
    )
    mappings = list(
        FieldMapping.objects.order_by("pk").values_list("pk", "dataset", "field", "chosen_key", "state")
    )
    _probe()
    assert (
        list(KeyProbe.objects.order_by("dataset", "key").values_list("dataset", "key", "records", "types"))
        == first
    )
    assert (
        list(FieldMapping.objects.order_by("pk").values_list("pk", "dataset", "field", "chosen_key", "state"))
        == mappings
    )


def test_the_probe_builds_no_visit(fm_world):
    from django.apps import apps

    _probe()
    assert {m.__name__ for m in apps.get_app_config("fmm").get_models()} == {"KeyProbe", "FieldMapping"}


def test_a_record_that_cannot_be_read_is_counted_and_the_others_are_read(fm_world, monkeypatch):
    add = fields.Probe.add

    def flaky(self, record):
        if isinstance(record, dict) and record.get("id") == 102:
            raise ValueError("unreadable record")
        add(self, record)

    monkeypatch.setattr(fields.Probe, "add", flaky)
    run = _probe()
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 1
    assert run.details["errors"][0]["error"] == "ValueError: unreadable record"
    assert run.details["datasets"]["field_monitoring"]["failed"] == 1
    assert KeyProbe.objects.filter(dataset="field_monitoring").exists()


def test_a_step_that_fails_keeps_the_previous_keys(fm_world, monkeypatch):
    _probe()
    before = KeyProbe.objects.count()
    dm.DatamartDocument.objects.filter(dataset="offices").delete()

    def broken(probes):
        raise RuntimeError("the choice failed")

    monkeypatch.setattr(fields, "resolve_all", broken)
    run = _probe()
    assert run.status == SyncRun.Status.FAILED and run.error == "RuntimeError: the choice failed"
    assert run.rows_failed == 0 and KeyProbe.objects.count() == before  # nothing half written
    assert KeyProbe.objects.filter(dataset="offices").exists()


def test_one_refresh_at_a_time(fm_world, held_elsewhere):
    assert _probe() is None
    assert not SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).exists()
    assert background.LOCK_IDS[SyncRun.Job.FMM_REFRESH] == refresh.LOCK_ID == 7140432
    assert command.BUSY in _command("--probe-only")


def test_switched_off_it_does_nothing(fm_world, settings):
    settings.FMM_ENABLED = False
    assert _probe() is None and not KeyProbe.objects.exists()
    assert command.SWITCHED_OFF in _command("--probe-only")


def test_only_the_probe_exists_so_far(db):
    with pytest.raises(NotImplementedError):
        refresh.run(triggered_by="test")
    run = refresh.run_safely(triggered_by="test")
    assert (run.status, run.target) == (SyncRun.Status.FAILED, "full") and "NotImplementedError" in run.error
    with pytest.raises(CommandError, match="--probe-only"):
        _command()


def test_the_command_writes_a_summary(fm_world):
    out = _command("--probe-only", "--triggered-by", "admin")
    assert "Monitoring insights refresh [probe] succeeded" in out
    assert SyncRun.objects.get(job=SyncRun.Job.FMM_REFRESH).triggered_by == "admin"


def test_a_failed_probe_fails_the_command(fm_world, monkeypatch):
    monkeypatch.setattr(fields, "resolve_all", lambda probes: 1 / 0)
    with pytest.raises(CommandError, match="fmm_refresh"):
        _command("--probe-only")


def test_the_refresh_is_wanted_after_a_sync_of_its_sources():
    def ran(target, status=SyncRun.Status.SUCCEEDED):
        return SyncRun(job=SyncRun.Job.ETOOLS_DATAMART, target=target, status=status)

    assert refresh.wanted_after([ran("grants"), ran("fm_questions")])
    assert refresh.wanted_after([ran("field_monitoring", SyncRun.Status.PARTIAL)])
    assert not refresh.wanted_after([ran("grants"), ran("field_monitoring", SyncRun.Status.FAILED)])
    assert not refresh.wanted_after([])
