"""The job of Compare with Power BI (datamart.powerbi_compare, command compare_powerbi): started by the upload and
by Compare again, one at a time under its own lock, never beside a Datamart sync or a KM Database load; staged
copies read and emptied; stale comparisons closed (from the pages, busy() and the scheduler); retention of the
newest uploads; the upload kept in memory only; reading a large export without memory growing with its rows,
and comparing it against an empty and a matching NeuroDB within the job's memory. Generated fake exports only."""

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
    assert set(exports.values_list("state", flat=True)) == {"compared"}
    assert all(e.rows for e in exports) and all(e.rows_read == 3 for e in exports)
    assert upload.summary["exports"] == 13 and upload.summary["rows"] == 39
    # the fake rows against an empty NeuroDB: differences, each with its reason (and nothing else is written)
    assert PowerBIDifference.objects.filter(export__upload=upload).count() > 0
    assert len(upload.summary["pages"]) == 10


def test_an_export_that_cannot_be_read_fails_alone(checker, started):
    upload = _upload(
        checker, ("cards.xlsx", pf.export("partner_cards")), ("sector.xlsx", pf.export("by_sector"))
    )
    PowerBIExport.objects.filter(upload=upload, kind="by_sector").update(content=b"PK broken")
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert upload.status == Status.DONE
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert states == {"partner_cards": "compared", "by_sector": "failed"}
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


def test_power_bi_s_own_fields_are_read_as_neurodb_reads_them_even_when_the_records_carry_them(
    checker, started, monkeypatch
):
    """NeuroDB's pages never read Power BI's own fields: the clause is applied as NeuroDB reads it, and the note
    says whether the records carry the field."""
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
    assert export.clauses == [{"words": "is_period_latest is True", "state": "as_read"}]
    assert export.notes == [
        "Power BI's field is_period_latest is in the records NeuroDB reads, but NeuroDB's pages do not use it: "
        "NeuroDB reads it as the newest row of each report and location."
    ]


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
    stopped = (
        "The comparison stopped while reading the files (MemoryError): upload the files again."  # not read
    )
    assert failing.status == Status.FAILED and failing.error == stopped
    assert str(raised.value).endswith(stopped)
    assert _content_left(failing) == 0


def test_the_job_lowers_its_priority_and_caps_its_memory(monkeypatch):
    seen = {}
    monkeypatch.setattr(os, "nice", lambda n: seen.setdefault("nice", n))
    monkeypatch.setattr(resource, "getrlimit", lambda what: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda what, limits: seen.setdefault("limit", (what, limits)))
    monkeypatch.setattr(pc, "memory_room", lambda: None)  # a container with no memory limit
    assert pc.limit_process() == 600 * 1024 * 1024
    assert seen == {
        "nice": 10,
        "limit": (resource.RLIMIT_DATA, (600 * 1024 * 1024, resource.RLIM_INFINITY)),
    }


@pytest.mark.parametrize(
    ("limit", "current", "inactive", "expected_mb"),
    [
        # the web workers already take 520 MB of a 1 GiB container: the job may grow to what is left
        (1024, 600, 20, 84 + 1024 - 580 - 150),
        (4096, 600, 0, 600),  # plenty of room: 600 MB at most
        (1024, 1000, 0, 250),  # almost none: 250 MB at least, so it can start
    ],
)
def test_the_job_s_memory_cap_follows_the_room_left_in_its_container(
    monkeypatch, tmp_path, limit, current, inactive, expected_mb
):
    mb = 1024 * 1024
    (tmp_path / "memory.max").write_text(f"{limit * mb}\n")
    (tmp_path / "memory.current").write_text(f"{current * mb}\n")
    (tmp_path / "memory.stat").write_text(f"anon 1\ninactive_file {inactive * mb}\nactive_file 2\n")
    monkeypatch.setattr(pc, "CGROUP_V2", str(tmp_path))
    monkeypatch.setattr(pc, "_own_data", lambda: 84 * mb)  # the job's own data as it starts
    seen = {}
    monkeypatch.setattr(os, "nice", lambda n: None)
    monkeypatch.setattr(resource, "getrlimit", lambda what: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda what, limits: seen.setdefault("limit", limits[0]))
    assert pc.limit_process() == seen["limit"] == expected_mb * mb
    (tmp_path / "memory.max").write_text("max\n")  # no limit: the fixed cap
    assert pc.memory_room() is None


def test_the_memory_cap_used_is_kept_beside_the_peak(checker, started):
    upload = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    pc.run(upload.pk, memory_limit=300 * 1024 * 1024)
    upload.refresh_from_db()
    assert upload.status == Status.DONE and upload.summary["memory_limit_mb"] == 300


def test_two_uploads_at_once_queue_one_comparison(checker, started, monkeypatch):
    """Queueing is one step with the check that nothing is busy (under its own lock): an upload queued while
    these files were being checked refuses them, and Compare again too."""
    first = _upload(checker, ("cards.xlsx", pf.export("partner_cards")))
    PowerBIUpload.objects.filter(pk=first.pk).update(status=Status.DONE)
    other = _upload(checker, ("sector.xlsx", pf.export("by_sector")))  # queued, its job about to start
    monkeypatch.setattr(pc, "busy", lambda: None)  # the request checked before the other one was queued
    response = pf.post(checker, ("list.xlsx", pf.export("report_list", people=True)))
    assert response.status_code == 200 and words.BUSY in response.content.decode()
    assert PowerBIUpload.objects.count() == 2 and len(started) == 2
    with pytest.raises(pc.Busy):
        pc.again(first)
    response = checker.post(reverse("admin:datamart_powerbi_again", args=[first.pk]))
    assert response.status_code == 200 and words.BUSY in response.content.decode()
    first.refresh_from_db()
    assert first.status == Status.DONE and len(started) == 2
    other.refresh_from_db()
    assert other.status == Status.QUEUED


def test_a_queued_upload_is_never_trimmed_and_one_trimmed_is_never_compared(checker, started):
    """Retention never trims an upload waiting to be compared (nor counts it among the newest); an upload
    whose rows were removed is never compared against nothing."""
    uploads = []
    for n in range(4):
        uploads.append(_upload(checker, (f"{n}.xlsx", pf.export("report_list", people=True))))
        PowerBIUpload.objects.filter(pk=uploads[-1].pk).update(status=Status.DONE)
    pc.retention()
    oldest, second = uploads[0], uploads[1]
    PowerBIUpload.objects.filter(pk=second.pk).update(status=Status.QUEUED)  # Compare again on it
    assert pc.retention() == 0
    second.refresh_from_db()
    assert not second.trimmed and PowerBIExport.objects.get(upload=second).rows
    # one trimmed meanwhile all the same: it fails, and says so
    PowerBIUpload.objects.filter(pk=oldest.pk).update(status=Status.QUEUED)
    ended = pc.run(oldest.pk)
    assert ended.status == Status.FAILED and ended.error == words.TRIMMED_QUEUED
    # an export whose rows are gone fails alone: never "the same" over no row
    PowerBIExport.objects.filter(upload=second).update(rows=None)
    ended = pc.run(second.pk)
    export = PowerBIExport.objects.get(upload=second)
    assert export.state == "failed" and export.verdict == "failed" and export.counts.get("same") is None


def test_the_lock_is_the_comparison_s_own():
    ids = [v for k, v in vars(background).items() if k.endswith("_LOCK_ID")]
    assert ids.count(background.POWERBI_COMPARE_LOCK_ID) == 1 and pc.LOCK_ID == 7140443
    assert ids.count(background.POWERBI_QUEUE_LOCK_ID) == 1 and pc.QUEUE_LOCK_ID == 7140444
    assert pc.LOCK_ID not in background.LOCK_IDS.values()


# ---------------------------------------------------------------------------------------------- retention
def test_only_the_newest_uploads_keep_their_rows(checker, started):
    uploads = []
    for n in range(4):
        uploads.append(_upload(checker, (f"{n}.xlsx", pf.export("report_list", people=True))))
        first = uploads[0]
        first.refresh_from_db()
        assert not first.trimmed  # an upload waiting to be compared is never counted among the newest
        PowerBIUpload.objects.filter(pk=uploads[-1].pk).update(status=Status.DONE)  # not running
    assert pc.retention() == 1  # as the fourth's comparison ends
    first.refresh_from_db()
    assert first.trimmed
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
    assert export.state == "compared" and export.rows_read == 100_000 and export.content is None
    rows = pr.rows_of(export.rows)
    first = dict(zip(export.columns, next(iter(rows)), strict=True))
    assert first["p_code"] == "LB9_9_0" and first["period_end"] == "2026-01-01"


def test_comparing_a_large_export_stays_within_its_memory_and_line_limits(checker, started, big):
    """100,000 disaggregation rows against a NeuroDB that holds none: compared month by month, every one counted,
    only the export's limit written, and the comparison's memory far below the job's limit."""
    upload = _upload(checker, ("disaggregations.xlsx", big))
    PowerBIExport.objects.filter(upload=upload).update(state="read")
    export = PowerBIExport.objects.get(upload=upload)
    found = pr.read(
        bytes(export.content), px.recognise(powerbi_xlsx.peek(bytes(export.content)).header), frozenset()
    )
    PowerBIExport.objects.filter(pk=export.pk).update(
        rows=found.rows, rows_read=found.rows_read, columns=found.columns, content=None, filters={}
    )
    export.refresh_from_db()
    from neurodb.datamart import powerbi_facts

    context = powerbi_facts.Context(day=datetime.date(2026, 10, 15), history=datetime.date(2024, 1, 1))
    tracemalloc.start()
    try:
        written = pc._write_lines([(export, pc._compare(export, upload, context))])
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    export.refresh_from_db()
    assert export.state == "compared" and export.counts["lines"] == 100_000
    assert written == pc.LINE_LIMIT == PowerBIDifference.objects.filter(export=export).count()
    assert peak < 150 * 1024 * 1024, peak / 1e6


MATCHING_LABELS = [(1, "Female", 10), (2, "Male", 10)] + [(i, f"Age {i}", 20) for i in range(3, 11)]


def _matching_neurodb(indicators: int, places: int, months: int) -> None:
    """NeuroDB's records for :func:`_matching_export`: one PD, monthly reports, ten values of two
    disaggregations at every place."""
    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.create(
        etl_id="fake-big", name="Fakeorg Partner 1", vendor_number="V-B"
    )
    pd = PCA.objects.create(
        etl_id="fake-big-pd", partner=partner, partner_name="Fakeorg Partner 1", number="FAKE/PCA2026001/PD2026001",
        title="Fake programme", status="active", document_type="PD", start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31), section_names=["Section Alpha"],
    )  # fmt: skip
    label = dm.DisaggregationLabel
    label.objects.create(kind=label.Kind.TYPE, prp_id=10, label="Sex")
    label.objects.create(kind=label.Kind.TYPE, prp_id=20, label="Age")
    for prp_id, name, kind in MATCHING_LABELS:
        label.objects.create(kind=label.Kind.VALUE, prp_id=prp_id, label=name, disaggregation_id=kind)
    values = {"()": 100.0, **{f"({prp_id},)": float(prp_id) for prp_id, _n, _k in MATCHING_LABELS}}
    records, n = [], 1_000_000
    for month in range(1, months + 1):
        start, end = _month(month)
        for i in range(indicators):
            for place in range(places):
                n += 1
                records.append(
                    dm.ReportedIndicator(
                        datamart_id=n, partner_id=partner.pk, intervention=pd, partner_name="Fakeorg Partner 1",
                        pd_reference_number=pd.number, progress_report=f"PR-{month}", indicator_report=f"IR-{month}-{i}",
                        report_number=str(month), report_type="HR", report_status="Accepted", period_start=start,
                        period_end=end, due_date=end + datetime.timedelta(days=10),
                        submission_date=end + datetime.timedelta(days=5), indicator=f"Fake indicator {i}",
                        etools_indicator_id=str(100 + i), pd_output="Fake output", section="Section Alpha",
                        location=f"Fake place {place}", p_code=f"LB9_{place}", cp_output="Fake output one",
                        disaggregation=values, target="100",
                    )
                )  # fmt: skip
    dm.ReportedIndicator.objects.bulk_create(records, batch_size=2000)


def _month(month: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(2026, month, 1)
    return start, (start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)


def _matching_export(indicators: int, places: int, months: int) -> bytes:
    """Power BI's Disaggregation export of the same values (reports/xlsx.py)."""
    from neurodb.reports import xlsx

    cols = pf.columns("disaggregations")

    def body():
        for month in range(1, months + 1):
            start, end = _month(month)
            for i in range(indicators):
                for place in range(places):
                    for prp_id, name, _kind in MATCHING_LABELS:
                        value = {
                            "section": "Section Alpha", "indicator_title": f"Fake indicator {i}",
                            "location": f"Fake place {place}", "p_code": f"LB9_{place}", "period_start": start,
                            "period_end": end, "due_date": end + datetime.timedelta(days=10),
                            "report_status": "Accepted", "report_type": "HR", "report": f"HR{month}",
                            "disaggregation": name, "reached": float(prp_id),
                            "refreshed": datetime.datetime(2026, 10, 14, 6, 0),
                        }  # fmt: skip
                        yield {c.caption: value.get(c.name) for c in cols}
        yield {}
        yield {cols[0].caption: pf.filters_line(("country_name is Lebanon", "status is active"))}

    return xlsx.workbook([("Export", [c.caption for c in cols], body())])


def test_comparing_a_large_export_with_a_matching_neurodb_stays_within_its_memory(
    checker, started, monkeypatch
):
    """100,000 disaggregation rows against a NeuroDB that holds the same values (10,000 records): NeuroDB's side
    (disaggregation_rows, fill_block, the sums) is read month by month, every row is the same, and the
    comparison's memory stays far below the job's limit."""
    from neurodb.datamart import powerbi_facts, reached

    monkeypatch.setattr(
        reached,
        "history_from",
        lambda today=None: reached.HistoryFrom(datetime.date(2026, 1, 1), "read_from"),
    )
    _matching_neurodb(20, 50, 10)
    upload = _upload(checker, ("disaggregations.xlsx", _matching_export(20, 50, 10)))
    export = PowerBIExport.objects.get(upload=upload)
    found = pr.read(
        bytes(export.content), px.recognise(powerbi_xlsx.peek(bytes(export.content)).header), frozenset()
    )
    PowerBIExport.objects.filter(pk=export.pk).update(
        rows=found.rows, rows_read=found.rows_read, columns=found.columns, content=None,
        filters=found.filter.to_json(), state="read",
    )  # fmt: skip
    export.refresh_from_db()
    context = powerbi_facts.Context(day=datetime.date(2026, 10, 15), history=datetime.date(2026, 1, 1))
    tracemalloc.start()
    try:
        pc._write_lines([(export, pc._compare(export, upload, context))])
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    export.refresh_from_db()
    assert export.state == "compared" and export.rows_read == 100_000, export.error
    assert export.counts["same"] == 100_000 and export.counts["lines"] == 0, export.counts
    assert peak < 150 * 1024 * 1024, peak / 1e6


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
    """The copy is written by NeuroDB in the request (the compared columns only, every row streamed): quick
    enough for the upload, and read exactly as the file itself."""
    peek = powerbi_xlsx.peek(big)
    match = px.recognise(peek.header, peek.filled)
    start = time.monotonic()
    content = pr.repack(big, "Export", match)
    assert time.monotonic() - start < 15
    assert sorted(zipfile.ZipFile(io.BytesIO(content)).namelist()) == [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/_rels/workbook.xml.rels",
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
    ]
    copy = pr.read(content, px.recognise(powerbi_xlsx.peek(content).header), frozenset(), ())
    whole = pr.read(big, match, frozenset(), ())
    assert copy.rows_read == whole.rows_read == 100_000
    assert list(pr.rows_of(copy.rows)) == list(pr.rows_of(whole.rows))
    assert copy.filter == whole.filter


# ------------------------------------------------------------------------------------- a failure, explained
def _sql(statement, *params):
    """A comparison step that runs one statement (the database refuses it)."""

    def run(*args, **kwargs):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(statement, list(params))

    return run


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "where, target, statement, step, code, meaning",
    [
        ("page", "partner_contributions", "SELECT 1 / (%s - 1)", "at NeuroDB's page", "22012",
         "a division by zero"),
        ("page", "partner_contributions", "SELECT %s::numeric", "at NeuroDB's page", "22P02",
         "a value written in a form the database cannot read"),
        ("facts", "Pds", "SELECT %s::numeric", "while reading the facts behind the reasons", "22P02",
         "a value written in a form the database cannot read"),
    ],
)  # fmt: skip
def test_a_failed_export_says_its_step_and_the_database_s_code_never_a_value(
    checker, started, monkeypatch, caplog, where, target, statement, step, code, meaning
):
    """The next failure is explained without the server's logs: the step and the SQLSTATE code (with its
    meaning), never a value, a query or the database's own message."""
    from neurodb.datamart import funds, powerbi_facts, powerbi_results

    module = funds if where == "page" else powerbi_facts
    value = 1 if "/" in statement else "Jane Doe"
    monkeypatch.setattr(module, target, _sql(statement, value))
    rows = [pf.row("fund_monitoring", pd="FAKE/PCA1/PD1", cash=1)]  # a PD NeuroDB does not hold
    funds_file = pf.export("fund_monitoring", rows, drop=("financial_contribution",))
    upload = _upload(checker, ("funds.xlsx", funds_file), ("cards.xlsx", pf.export("partner_cards")))
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert upload.status == Status.DONE
    export = PowerBIExport.objects.get(upload=upload, kind="fund_monitoring")
    assert export.state == "failed" and export.verdict == "failed"
    assert export.error == f"The comparison stopped {step}: DataError {code}, {meaning}."
    assert export.counts == {"failure": {"step": where, "cls": "DataError", "code": code}}
    verdict, action = powerbi_results.verdict_words(export)
    assert verdict == f"Could not be compared {step}: DataError {code}, {meaning}"
    assert action == words.VERDICT_ACTIONS["failed"]
    html = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    assert verdict.replace("'", "&#x27;") in html
    for seen in (export.error, html, caplog.text):
        assert "Jane" not in seen and "SELECT" not in seen and "numeric" not in seen
    assert f"DataError {code}" in caplog.text
    assert PowerBIExport.objects.get(upload=upload, kind="partner_cards").state == "compared"


@pytest.mark.django_db(transaction=True)
def test_an_export_whose_lines_cannot_be_written_says_so_and_keeps_no_count(checker, started, monkeypatch):
    monkeypatch.setattr(pc, "_write_group", _sql("SELECT 1 / (%s - 1)", 1))
    upload = _upload(checker, ("sector.xlsx", pf.export("by_sector")))
    pc.run(upload.pk)
    export = PowerBIExport.objects.get(upload=upload)
    assert export.state == "failed"
    assert (
        export.error
        == "The comparison stopped while writing its differences: DataError 22012, a division by zero."
    )
    # never "Needs a look" over lines that were removed
    assert (
        export.counts == {"failure": {"step": "lines", "cls": "DataError", "code": "22012"}}
        and not export.fits
    )


def _no_code(*args, **kwargs):
    """The database client's own error (libpq's "out of memory for query result"): no SQLSTATE."""
    import psycopg
    from django.db import DatabaseError

    raise DatabaseError("Jane Doe") from psycopg.DatabaseError("Jane Doe")


@pytest.mark.parametrize("memory", [True, False])
def test_a_database_error_without_a_code_says_whether_the_job_was_at_its_memory_limit(
    checker, started, monkeypatch, memory
):
    from neurodb.datamart import monitoring, powerbi_kinds, powerbi_results

    monkeypatch.setattr(monitoring, "indicators", _no_code)
    monkeypatch.setattr(pc, "at_memory_limit", lambda: memory)
    rows = [pf.row("program_performance", partner="FAKEORG PARTNER 1", indicator_title="Fake 1", target=1)]
    upload = _upload(checker, ("pp.xlsx", pf.export("program_performance", rows)))
    pc.run(upload.pk)
    export = PowerBIExport.objects.get(upload=upload)
    tail = ", at the job's memory limit" if memory else ""
    assert export.error == f"The comparison stopped at NeuroDB's page: DatabaseError with no code{tail}."
    assert powerbi_results.verdict_words(export)[0] == (
        f"Could not be compared at NeuroDB's page: DatabaseError with no code{tail}"
    )
    assert "Jane" not in export.error and powerbi_kinds.COMPARERS  # the comparer itself is untouched


def test_a_comparison_error_names_the_step_and_its_class_only(checker, started, monkeypatch):
    from neurodb.datamart import powerbi_kinds

    def broken(job):
        raise TypeError("Jane Doe")

    monkeypatch.setitem(powerbi_kinds.COMPARERS, "by_sector", broken)
    upload = _upload(checker, ("sector.xlsx", pf.export("by_sector")))
    pc.run(upload.pk)
    export = PowerBIExport.objects.get(upload=upload)
    assert export.error == "The comparison stopped while matching the rows: TypeError."


def test_an_old_failure_without_its_step_still_shows_its_class():
    """Exports that failed before the step was kept (their error alone)."""
    from neurodb.datamart import powerbi_results

    export = PowerBIExport(
        state="failed", verdict="failed", error="The comparison stopped: DataError.", counts={}
    )
    assert powerbi_results.verdict_words(export)[0] == "Could not be compared: DataError"


@pytest.mark.django_db(transaction=True)
def test_a_stopped_upload_says_the_step_it_stopped_at(checker, started, monkeypatch):
    from neurodb.datamart import powerbi_kinds

    def too_much(job):
        raise MemoryError

    monkeypatch.setitem(powerbi_kinds.COMPARERS, "by_sector", too_much)
    upload = _upload(checker, ("sector.xlsx", pf.export("by_sector")))
    ended = pc.run(upload.pk)
    visual = px.KINDS["by_sector"].visual[1]
    assert ended.status == Status.FAILED
    assert (
        ended.error == f"The comparison stopped while comparing {visual} (MemoryError): press Compare again."
    )
    monkeypatch.setattr(pc, "_summary", _sql("SELECT 1 / (%s - 1)", 1))
    monkeypatch.setitem(powerbi_kinds.COMPARERS, "by_sector", lambda job: None)
    assert pc.again(ended)
    ended = pc.run(upload.pk)
    assert ended.error == (
        "The comparison stopped while writing the summary (DataError 22012, a division by zero): press Compare "
        "again."
    )


@pytest.mark.parametrize(
    "kind, named",
    [
        ("program_performance", "Program Performance"),  # the page's only visual, a bare "Table"
        ("pd_indicators", "PD/SPD Indicator"),
        ("funded_programmes", "Funded Programs"),
        ("dq_list", "Indicators without Reached or with Zero Reached"),  # a page of two visuals: the visual
    ],
)
def test_a_stopped_upload_names_the_export_it_stopped_at(checker, started, monkeypatch, kind, named):
    """Seven exports' visual is a bare "Table": the upload names their page ("while comparing Table" said
    nothing of which export)."""
    from neurodb.datamart import powerbi_kinds

    def too_much(job):
        raise MemoryError

    monkeypatch.setitem(powerbi_kinds.COMPARERS, kind, too_much)
    upload = _upload(checker, (f"{kind}.xlsx", pf.export(kind)))
    ended = pc.run(upload.pk)
    assert (
        ended.error == f"The comparison stopped while comparing {named} (MemoryError): press Compare again."
    )


@pytest.mark.parametrize("kind", ["locations", "disaggregations"])
def test_an_error_in_a_page_module_a_comparer_calls_itself_is_at_neurodb_s_page(
    checker, started, monkeypatch, kind
):
    """By location's and By disaggregation's comparers call NeuroDB's page code themselves
    (``reporting.location_rows``, ``disaggregation_rows``): an error there is at NeuroDB's page, never "while
    matching the rows"."""
    from neurodb.datamart import reporting

    def cells(*args, **kwargs):
        raise OverflowError("Jane Doe")

    monkeypatch.setattr(reporting, "_location_cells", cells)
    upload = _upload(checker, (f"{kind}.xlsx", pf.export(kind)))
    pc.run(upload.pk)
    export = PowerBIExport.objects.get(upload=upload)
    assert export.state == "failed"
    assert export.error == "The comparison stopped at NeuroDB's page: OverflowError."
    assert export.counts == {"failure": {"step": "page", "cls": "OverflowError"}}


# ------------------------------------------------------------------------------------- the job's own memory
@pytest.mark.parametrize(
    "status, inherited_mb, at_limit, peak_mb",
    [
        (
            "VmHWM:\t  102400 kB\nVmData:\t   81920 kB\n",
            600,
            False,
            100.0,
        ),  # the web worker's 600 MB: not its own
        ("VmHWM:\t  235520 kB\nVmData:\t   81920 kB\n", 100, True, 230.0),  # its own 230 MB, past 90% of 250
        ("VmHWM:\t  102400 kB\nVmData:\t  235520 kB\n", 100, True, 100.0),  # its data now, past 90%
        (None, 240, True, 240.0),  # /proc cannot be read: ru_maxrss
        (None, 100, False, 100.0),
    ],
)
def test_the_memory_limit_and_the_peak_are_the_job_s_own(
    monkeypatch, tmp_path, status, inherited_mb, at_limit, peak_mb
):
    """The job is started by a web worker, whose memory peak Linux keeps in the job's ``ru_maxrss`` across exec:
    "at the job's memory limit" and ``peak_memory_mb`` read the job's own peak (VmHWM), never the worker's."""
    import types

    mb = 1024 * 1024
    path = tmp_path / "status"
    if status is not None:
        path.write_text(f"Name:\tpython\n{status}VmRSS:\t   90000 kB\n")
    monkeypatch.setattr(pc, "PROC_STATUS", str(path))
    monkeypatch.setattr(resource, "getrlimit", lambda what: (250 * mb, resource.RLIM_INFINITY))
    monkeypatch.setattr(
        resource, "getrusage", lambda who: types.SimpleNamespace(ru_maxrss=inherited_mb * 1024)
    )
    assert pc.at_memory_limit() is at_limit
    upload = PowerBIUpload()
    pc._measure(upload, time.monotonic())
    assert upload.peak_memory_mb == peak_mb


def test_a_job_started_by_a_process_that_held_much_memory_is_not_at_its_limit():
    """The real thing: a process that held 300 MB execs into a job whose data limit is 320 MB. Its
    ``ru_maxrss`` is the 300 MB it inherited (past 90% of the limit); the job itself is far below it."""
    import subprocess
    import sys
    from pathlib import Path

    job = (
        "import resource, django\n"
        "django.setup()\n"
        "from neurodb.datamart import powerbi_compare as pc\n"
        "_soft, hard = resource.getrlimit(resource.RLIMIT_DATA)\n"
        "resource.setrlimit(resource.RLIMIT_DATA, (320 * pc.MB, hard))\n"
        "inherited = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024\n"
        "print(inherited // pc.MB, pc._own_peak() // pc.MB, pc.at_memory_limit())\n"
    )
    worker = (
        "import os, sys\n"
        "held = b'x' * (300 * 1024 * 1024)\n"
        "del held\n"
        f"os.execv(sys.executable, [sys.executable, '-c', {job!r}])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", worker],
        cwd=Path(pc.__file__).resolve().parents[2],
        env={"DJANGO_SETTINGS_MODULE": "config.settings", **os.environ},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    inherited, own, at_limit = done.stdout.split()
    assert int(inherited) >= 300 and int(own) < 200 and at_limit == "False", done.stdout
