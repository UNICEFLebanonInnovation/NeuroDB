"""The job of Compare with Power BI (datamart.powerbi_compare, command compare_powerbi): started by the upload and
by Compare again, one at a time under its own lock, never beside a Datamart sync or a KM Database load; staged
copies read and emptied; stale comparisons closed (from the pages, busy() and the scheduler); retention of the
newest uploads; the upload kept in memory only; reading a large export without memory growing with its rows.
Generated fake exports only."""

from __future__ import annotations

import datetime
import io
import os
import random
import resource
import time
import tracemalloc
import zipfile

import pytest
from django.core.management import CommandError, call_command
from django.db import connections
from django.urls import reverse
from django.utils import timezone

from neurodb.core import scheduler
from neurodb.datamart import powerbi_compare as pc
from neurodb.datamart import powerbi_exports as px
from neurodb.datamart import powerbi_read as pr
from neurodb.datamart import powerbi_words as words
from neurodb.datamart import powerbi_xlsx
from neurodb.datamart.models import PowerBIDifference, PowerBIExport, PowerBIUpload
from neurodb.integrations import background
from tests.datamart import powerbi_factory as pf

pytestmark = pytest.mark.django_db
Status = PowerBIUpload.Status


@pytest.fixture
def started(monkeypatch):
    """The commands started in the background (none really is)."""
    found = []
    monkeypatch.setattr(background, "start_command", lambda *args: found.append(args) or 4242)
    return found


@pytest.fixture
def checker(client, roles):
    client.force_login(pf.user("checker"))
    return client


@pytest.fixture
def no_limits(monkeypatch):
    """The command's own limits (CPU priority, memory) are never set on the test process."""
    seen = []
    monkeypatch.setattr(pc, "limit_process", lambda: seen.append("limited"))
    return seen


def _upload(client, *files) -> PowerBIUpload:
    response = pf.post(client, *files)
    assert response.status_code == 302, response.content[:300]
    return PowerBIUpload.objects.order_by("-pk").first()


def _content_left(upload: PowerBIUpload) -> int:
    return PowerBIExport.objects.filter(upload=upload).exclude(content=None).count()


# ----------------------------------------------------------------------------------------------- starting
def test_an_upload_starts_the_job_with_its_upload_and_who_started_it(checker, started):
    upload = _upload(
        checker,
        ("cards.xlsx", pf.export("partner_cards")),
        ("list.xlsx", pf.export("report_list", people=True)),
    )
    assert started == [("compare_powerbi", "--upload-id", str(upload.pk), "--triggered-by", "checker")]
    assert upload.status == Status.QUEUED and upload.queued_at is not None
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert states == {
        "partner_cards": "staged",
        "report_list": "read",
    }  # a file naming people is read at once


def test_a_start_that_fails_fails_the_upload_and_empties_its_copies(checker, monkeypatch):
    def broken(*args):
        raise OSError("no process")

    monkeypatch.setattr(background, "start_command", broken)
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    upload.refresh_from_db()
    assert upload.status == Status.FAILED and upload.error == words.START_FAILED
    assert _content_left(upload) == 0


def test_the_job_reads_the_staged_copies_and_empties_them(checker, started, no_limits):
    upload = _upload(checker, *pf.every_export())
    assert _content_left(upload) == 13  # no column names people: each waits in its repacked copy
    out = io.StringIO()
    call_command("compare_powerbi", "--upload-id", str(upload.pk), "--triggered-by", "checker", stdout=out)
    assert no_limits == ["limited"] and "done" in out.getvalue()
    upload.refresh_from_db()
    assert upload.status == Status.DONE and upload.compared_at and upload.seconds is not None
    assert upload.peak_memory_mb and upload.day is not None
    assert _content_left(upload) == 0
    exports = PowerBIExport.objects.filter(upload=upload)
    assert sorted(exports.values_list("kind", flat=True)) == sorted(px.KINDS)
    assert set(exports.values_list("state", flat=True)) == {"read"}
    assert all(e.rows for e in exports) and all(e.rows_read == 3 for e in exports)
    assert upload.summary["exports"] == 13 and upload.summary["rows"] == 39
    # nothing else: no import run, no difference written
    assert PowerBIDifference.objects.count() == 0


def test_an_export_that_cannot_be_read_fails_alone(checker, started):
    upload = _upload(
        checker, ("cards.xlsx", pf.export("partner_cards")), ("sector.xlsx", pf.export("by_sector"))
    )
    PowerBIExport.objects.filter(upload=upload, kind="by_sector").update(content=b"PK broken")
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert upload.status == Status.DONE
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert states == {"partner_cards": "read", "by_sector": "failed"}
    failed = PowerBIExport.objects.get(upload=upload, kind="by_sector")
    assert failed.error == "NeuroDB could not read sector.xlsx (Unreadable)." and failed.content is None


def test_an_export_of_another_country_or_with_no_row_is_said_so(checker, started):
    upload = _upload(
        checker,
        ("other.xlsx", pf.export("by_sector", filters=("country_name is Fakeland",))),
        ("empty.xlsx", pf.export("follow_up", [])),
    )
    pc.run(upload.pk)
    found = {e.kind: e for e in PowerBIExport.objects.filter(upload=upload)}
    assert found["by_sector"].state == "not_compared"
    assert found["by_sector"].notes == ["NeuroDB holds Lebanon's data only."]
    assert "Fakeland" not in repr(found["by_sector"].filters) + repr(found["by_sector"].clauses)
    assert found["follow_up"].state == "empty" and found["follow_up"].rows_read == 0


def test_power_bi_s_refresh_gives_the_day_the_statuses_are_read_for(checker, started, monkeypatch):
    from neurodb.datamart import services

    monkeypatch.setattr(services, "reporting_today", lambda: datetime.date(2026, 10, 25))
    upload = _upload(checker, ("locations.xlsx", pf.export("locations")))
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert timezone.localtime(upload.powerbi_as_of).date() == datetime.date(2026, 10, 24)
    assert upload.day == datetime.date(2026, 10, 24) and upload.summary["notes"] == []
    monkeypatch.setattr(services, "reporting_today", lambda: datetime.date(2026, 12, 1))
    assert pc.again(upload)
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert upload.day == datetime.date(2026, 12, 1)
    assert upload.summary["notes"] == [words.DAY_OLD.format(day="24 Oct 2026")]


def test_power_bi_s_own_fields_are_applied_from_the_records_when_they_carry_them(
    checker, started, monkeypatch
):
    upload = _upload(
        checker, ("pp.xlsx", pf.export("program_performance", filters=("is_period_latest is True",)))
    )
    monkeypatch.setattr(pc, "fields_in_records", lambda run: set())
    pc.run(upload.pk)
    export = PowerBIExport.objects.get(upload=upload)
    assert export.clauses == [{"words": "is_period_latest is True", "state": "as_read"}]
    assert export.notes == [
        "Power BI's field is_period_latest is not in the records NeuroDB reads: NeuroDB reads it as the newest "
        "row of each report and location."
    ]
    monkeypatch.setattr(pc, "fields_in_records", lambda run: {"is_period_latest"})
    assert pc.again(upload)
    pc.run(upload.pk)
    export.refresh_from_db()
    assert (
        export.clauses == [{"words": "is_period_latest is True", "state": "from_records"}]
        and export.notes == []
    )


def test_fields_in_records_reads_raw_etools_data_s_fields(monkeypatch):
    from neurodb.datamart import raw

    monkeypatch.setattr(raw, "keys_kept", lambda run, name: {"id": [10, 10], "is_period_latest": [10, 10]})
    assert pc.fields_in_records(object()) == {"is_period_latest"}
    monkeypatch.setattr(raw, "keys_kept", lambda run, name: {"id": [10, 10], "is_period_latest": [9, 9]})
    assert pc.fields_in_records(object()) == set()


# -------------------------------------------------------------------------------------------- compare again
def test_compare_again_on_an_old_upload_waits_for_the_job(checker, started):
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    pc.run(upload.pk)
    PowerBIUpload.objects.filter(pk=upload.pk).update(uploaded_at=timezone.now() - datetime.timedelta(days=3))
    response = checker.post(reverse("admin:datamart_powerbi_again", args=[upload.pk]))
    assert response.status_code == 302
    assert started[-1] == ("compare_powerbi", "--upload-id", str(upload.pk), "--triggered-by", "checker")
    page = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk]))
    upload.refresh_from_db()
    assert page.status_code == 200 and upload.status == Status.QUEUED  # aged by queued_at, not uploaded_at
    assert pc.run(upload.pk).status == Status.DONE


def test_compare_again_is_refused_for_a_trimmed_or_running_upload(checker, started):
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    assert not pc.can_compare_again(upload)  # waiting for the job
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert pc.can_compare_again(upload)
    PowerBIUpload.objects.filter(pk=upload.pk).update(trimmed=True)
    response = checker.post(reverse("admin:datamart_powerbi_again", args=[upload.pk]))
    assert response.status_code == 200 and words.NOT_AGAIN in response.content.decode()


# ----------------------------------------------------------------------------------------- lock and refusals
@pytest.fixture
def other_session():
    """A second database session: a lock it holds is held by another process for the job."""
    other = connections.create_connection("default")
    yield other
    other.close()


def test_a_comparison_while_another_holds_the_lock_fails_with_its_sentence(checker, started, other_session):
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    with other_session.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [pc.LOCK_ID])
    try:
        ended = pc.run(upload.pk)
        assert pc.close_stale() == 0  # the lock is held: nothing is stale
    finally:
        with other_session.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [pc.LOCK_ID])
    assert ended.status == Status.FAILED and ended.error == words.LOCKED
    assert _content_left(upload) == 0


def test_an_upload_while_a_comparison_runs_is_refused(checker, started):
    running = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    response = pf.post(checker, ("sector.xlsx", pf.export("by_sector")))
    assert response.status_code == 200 and words.BUSY in response.content.decode()
    assert PowerBIUpload.objects.count() == 1 and running.status == Status.QUEUED


def test_an_upload_while_a_datamart_sync_or_a_km_load_runs_is_refused(checker, started, monkeypatch):
    held = {background.KM_LOAD_LOCK_ID}
    monkeypatch.setattr(background, "lock_is_held", lambda lock: lock in held)
    response = pf.post(checker, ("sector.xlsx", pf.export("by_sector")))
    assert response.status_code == 200 and words.OTHER_JOB in response.content.decode()
    assert PowerBIUpload.objects.count() == 0 and started == []


def test_a_heavy_job_started_after_reading_stops_the_comparison_and_keeps_the_rows(
    checker, started, monkeypatch
):
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    monkeypatch.setattr(pc, "heavy_job_running", lambda: True)
    ended = pc.run(upload.pk)
    assert ended.status == Status.FAILED and ended.error == words.OTHER_JOB_LATER
    assert PowerBIExport.objects.get(upload=upload).rows and pc.can_compare_again(ended)


# ------------------------------------------------------------------------------------------------ stale
def test_stale_comparisons_are_closed_and_their_copies_emptied(checker, started):
    reading = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    PowerBIUpload.objects.filter(pk=reading.pk).update(status=Status.READING)
    queued = PowerBIUpload.objects.create(
        status=Status.QUEUED, queued_at=timezone.now() - datetime.timedelta(minutes=3)
    )
    young = PowerBIUpload.objects.create(status=Status.QUEUED, queued_at=timezone.now())
    assert pc.close_stale() == 2
    reading.refresh_from_db(), queued.refresh_from_db(), young.refresh_from_db()
    assert reading.status == Status.FAILED and reading.error == words.STALE and _content_left(reading) == 0
    assert queued.status == Status.FAILED and queued.error == words.STALE_AGAIN  # nothing waits in a copy
    assert young.status == Status.QUEUED


def test_a_stopped_comparison_whose_rows_are_kept_can_be_compared_again(checker, started):
    upload = _upload(checker, ("list.xlsx", pf.export("report_list", people=True)))  # read in the request
    PowerBIUpload.objects.filter(pk=upload.pk).update(status=Status.COMPARING)
    assert pc.close_stale() == 1
    upload.refresh_from_db()
    assert upload.error == words.STALE_AGAIN and pc.can_compare_again(upload)


def test_busy_and_the_scheduler_close_stale_comparisons(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "close_stale", lambda **kw: calls.append("closed") or 0)
    assert pc.busy() is None and calls == ["closed"]
    scheduler.tick()
    assert calls == ["closed", "closed"]


def test_a_broken_tidy_up_never_stops_the_scheduler(monkeypatch, caplog):
    def broken(**kw):
        raise RuntimeError("Jane Doe")

    monkeypatch.setattr(pc, "close_stale", broken)
    assert scheduler.tick() == []
    assert "Jane" not in caplog.text and "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------------------------- the command
def test_the_command_says_nothing_to_do_or_raises_the_stored_sentence(
    checker, started, no_limits, monkeypatch
):
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    pc.run(upload.pk)
    out = io.StringIO()
    call_command("compare_powerbi", "--upload-id", str(upload.pk), stdout=out)
    assert out.getvalue().startswith("Nothing to do")
    failing = _upload(checker, ("sector.xlsx", pf.export("by_sector")))
    PowerBIUpload.objects.filter(pk=failing.pk).update(status=Status.QUEUED)

    def stopping(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(pr, "read", stopping)
    with pytest.raises(CommandError) as raised:
        call_command("compare_powerbi", "--upload-id", str(failing.pk))
    failing.refresh_from_db()
    assert failing.status == Status.FAILED and failing.error == "The comparison stopped: MemoryError."
    assert str(raised.value).endswith("The comparison stopped: MemoryError.")
    assert _content_left(failing) == 0


def test_the_job_lowers_its_priority_and_caps_its_memory(monkeypatch):
    seen = {}
    monkeypatch.setattr(os, "nice", lambda n: seen.setdefault("nice", n))
    monkeypatch.setattr(resource, "getrlimit", lambda what: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda what, limits: seen.setdefault("limit", (what, limits)))
    pc.limit_process()
    assert seen == {
        "nice": 10,
        "limit": (resource.RLIMIT_DATA, (600 * 1024 * 1024, resource.RLIM_INFINITY)),
    }


def test_the_lock_is_the_comparison_s_own():
    ids = [v for k, v in vars(background).items() if k.endswith("_LOCK_ID")]
    assert ids.count(background.POWERBI_COMPARE_LOCK_ID) == 1 and pc.LOCK_ID == 7140443
    assert pc.LOCK_ID not in background.LOCK_IDS.values()


# ---------------------------------------------------------------------------------------------- retention
def test_only_the_newest_uploads_keep_their_rows(checker, started):
    uploads = []
    for n in range(4):
        uploads.append(_upload(checker, (f"{n}.xlsx", pf.export("report_list", people=True))))
        PowerBIUpload.objects.filter(pk=uploads[-1].pk).update(status=Status.DONE)  # not running
    first = uploads[0]
    first.refresh_from_db()
    assert first.trimmed  # trimmed when the fourth was created
    assert PowerBIExport.objects.get(upload=first).rows is None
    assert PowerBIExport.objects.get(upload=first).rows_read == 3  # its counts stay
    assert all(PowerBIExport.objects.get(upload=u).rows for u in uploads[1:])
    assert not pc.can_compare_again(first)


# ----------------------------------------------------------------------------------------- the upload itself
def test_the_upload_never_reaches_the_disk(checker, started, settings, tmp_path):
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path)
    padding = random.Random(1).randbytes(3 * 1024 * 1024)  # more than Django keeps in memory by itself
    data = pf.archive(
        ("list.xlsx", pf.export("report_list", people=True)), ("padding.bin", padding), mac=False, junk=False
    )
    assert len(data) > settings.FILE_UPLOAD_MAX_MEMORY_SIZE
    upload = _upload(checker, ("exports.zip", data))
    assert list(tmp_path.iterdir()) == []
    assert upload.refused == [words.NOT_EXCEL.format(name="padding.bin")]


def test_an_upload_too_large_in_all_is_refused_and_nothing_kept(
    checker, started, monkeypatch, settings, tmp_path
):
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path)
    monkeypatch.setattr(pr, "MAX_UPLOAD_BYTES", 200_000)
    padding = random.Random(2).randbytes(300_000)
    response = pf.post(checker, ("a.xlsx", pf.export("by_sector")), ("b.bin", padding))
    assert response.status_code == 200
    assert (
        words.TOO_BIG_UPLOAD in response.content.decode() and words.NOTHING_KEPT in response.content.decode()
    )
    assert PowerBIUpload.objects.count() == 0 and started == [] and list(tmp_path.iterdir()) == []


def test_a_file_too_large_is_dropped_and_the_others_read(checker, started, monkeypatch):
    monkeypatch.setattr(pr, "MAX_FILE_BYTES", 100_000)
    padding = random.Random(3).randbytes(150_000)
    upload = _upload(checker, ("big one.xlsx", padding), ("sector.xlsx", pf.export("by_sector")))
    assert upload.refused == [words.TOO_BIG.format(name="big one.xlsx")]
    assert list(PowerBIExport.objects.filter(upload=upload).values_list("kind", flat=True)) == ["by_sector"]


def test_the_handler_stops_a_body_announced_too_large():
    handler = pr.UploadHandler()
    handler.handle_raw_input(None, {}, 60 * 1024 * 1024, b"", None)
    with pytest.raises(pr.StopUpload):
        handler.new_file("files", "a.xlsx", "application/octet-stream", None)
    assert handler.too_big_upload


# -------------------------------------------------------------------------------------- reading at scale
@pytest.fixture(scope="module")
def big():
    return pf.big_disaggregations(100_000)


def test_a_large_export_is_read_whole_by_the_job(checker, started, big):
    start = time.monotonic()
    peek = powerbi_xlsx.peek(big)
    assert time.monotonic() - start < 1 and peek.header[0] == "Indicator"
    upload = _upload(checker, ("disaggregations.xlsx", big))
    pc.run(upload.pk)
    export = PowerBIExport.objects.get(upload=upload)
    assert export.state == "read" and export.rows_read == 100_000 and export.content is None
    rows = pr.rows_of(export.rows)
    first = dict(zip(export.columns, next(iter(rows)), strict=True))
    assert first["p_code"] == "LB9_9_0" and first["period_end"] == "2026-01-01"


def test_reading_memory_does_not_grow_with_the_rows():
    def peak(n: int) -> int:
        data = pf.big_disaggregations(n)
        match = px.recognise(powerbi_xlsx.peek(data).header)
        tracemalloc.start()
        try:
            pr.read(data, match, frozenset(), ())
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    small, large = peak(5_000), peak(20_000)  # four times the rows: not four times the memory
    assert large < 1.5 * small + 5 * 1024 * 1024, (small, large)


def test_a_staged_copy_of_a_large_export_is_read_the_same(big):
    content = pr.repack(big, "Export")
    assert sorted(zipfile.ZipFile(io.BytesIO(content)).namelist()) == [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/_rels/workbook.xml.rels",
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
    ]
