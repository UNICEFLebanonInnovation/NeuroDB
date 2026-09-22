import datetime as dt

import pytest
from django.db.models.query import QuerySet

from neurodb.core.models import SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.integrations.activityinfo import data as data_module
from neurodb.integrations.activityinfo.data import import_data
from tests.integrations.extracts import make_extract, make_row

pytestmark = pytest.mark.django_db

TODAY = dt.date(2025, 6, 15)


def make_run(database) -> SyncRun:
    return SyncRun.objects.create(
        job=SyncRun.Job.ACTIVITYINFO_DATA, target=str(database.ai_id), triggered_by="test"
    )


def seed_existing(database, other_database) -> None:
    ActivityReportNew.objects.create(dbase=database, database_ai_id=str(database.ai_id), indicator_id="old")
    ActivityReportNew.objects.create(
        dbase=other_database, database_ai_id=str(other_database.ai_id), indicator_id="keep"
    )


def test_import_data_replaces_rows_in_batches_and_records_counts(
    database, nutrition_database, monkeypatch, tmp_path
):
    seed_existing(database, nutrition_database)
    monkeypatch.setattr(data_module, "BATCH_SIZE", 2)
    calls: list[int | None] = []
    original = QuerySet.bulk_create

    def spy(self, objs, batch_size=None, **kwargs):
        calls.append(batch_size)
        return original(self, objs, batch_size=batch_size, **kwargs)

    monkeypatch.setattr(QuerySet, "bulk_create", spy)
    monkeypatch.setattr(data_module.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    extract = make_extract(
        [
            make_row(RecordId="r1"),
            make_row(RecordId="r2", Value="7.5"),
            make_row(RecordId="r3", month="2025-02"),
            make_row(RecordId="zero", Value="0"),  # skipped like v2
            make_row(RecordId="bad-year", month="2019-01"),  # rejected -> rows_failed
            make_row(RecordId="too-long", **{"partner.partner_full_name": "x" * 300}),  # rejected
        ]
    )
    run = make_run(database)
    stats = import_data(database, run=run, extract_bytes=extract, keep_copy=False, today=TODAY)

    run.refresh_from_db()
    assert run.status == SyncRun.Status.PARTIAL
    assert (run.rows_in, run.rows_written, run.rows_failed) == (6, 3, 2)
    assert run.details["rows_skipped"] == 1
    assert run.details["rows_deleted"] == 1
    assert stats["status"] == SyncRun.Status.PARTIAL
    assert calls == [2]
    assert (tmp_path / "202516_ai_data.txt").read_bytes() == extract

    rows = ActivityReportNew.objects.filter(dbase=database).order_by("indicator_value")
    assert list(rows.values_list("indicator_value", flat=True)) == [7.5, 18.0, 18.0]
    assert set(rows.values_list("month_name", flat=True)) == {"2024-10", "2025-02"}
    assert rows.filter(indicator_id="old").count() == 0
    assert ActivityReportNew.objects.filter(indicator_id="keep").count() == 1
    database.refresh_from_db()
    assert database.last_monthly_update_date is not None


def test_import_data_succeeds_when_no_row_fails(database):
    run = make_run(database)
    import_data(database, run=run, extract_bytes=make_extract([make_row()]), keep_copy=False, today=TODAY)
    run.refresh_from_db()
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.rows_written == 1


def test_failed_insert_rolls_back_and_does_not_stamp_database(database, nutrition_database, monkeypatch):
    seed_existing(database, nutrition_database)

    def boom(self, objs, batch_size=None, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(QuerySet, "bulk_create", boom)
    run = make_run(database)
    with pytest.raises(RuntimeError):
        import_data(database, run=run, extract_bytes=make_extract([make_row()]), keep_copy=False, today=TODAY)
    run.refresh_from_db()
    assert run.status == SyncRun.Status.FAILED
    assert "db down" in run.error
    assert ActivityReportNew.objects.filter(indicator_id="old").count() == 1  # delete rolled back
    database.refresh_from_db()
    assert database.last_monthly_update_date is None


def test_export_job_is_used_when_no_extract_given(database, monkeypatch):
    class FakeClient:
        def export_database(self, db):
            assert db == database
            return make_extract([make_row()])

    run = make_run(database)
    import_data(database, client=FakeClient(), run=run, keep_copy=False, today=TODAY)
    assert run.rows_written == 1


def test_database_without_reporting_year_fails(database):
    database.reporting_year = None
    run = make_run(database)
    with pytest.raises(ValueError):
        import_data(database, run=run, extract_bytes=b"", keep_copy=False)
    assert run.status == SyncRun.Status.FAILED
