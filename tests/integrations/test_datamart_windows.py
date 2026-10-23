"""A large eTools Datamart read in windows (``windows.Reading``): today's requests while small; above the limit,
windows proven to filter, each read whole, before the sync removes anything. The Datamart is the fake of
``fake_datamart`` with small constants (a window below 20 records, a round count of 20 or more a multiple of
10, pages of 5), and every large read here is one the sync suspects cut, A (``datamart_sync.cut_sign`` says
the last read's page came back short): ``test_datamart_reads`` reads when it is not."""

import datetime as dt
import re

import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import reached
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools import windows
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, PartnerOrganization

from .fake_datamart import FakeDatamart, report, v2

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
REPORTS = f"{BASE}/api/latest/prp/datareport/"
V2 = f"{BASE}/api/latest/prp/indicator-report-v2/"
INDICATORS = f"{BASE}/api/latest/datamart/pd-indicators/"
KEY = "reporting_period_start_date"
DATE_FILTERS = (f"{KEY}__gte", f"{KEY}__lt", f"{KEY}__lte")
ID_FILTERS = ("id__gte", "id__lt", "id__lte")


@pytest.fixture(autouse=True)
def small(settings, monkeypatch):
    """Windows below 20 records, a round count of 20 or more a multiple of 10, pages of 5, and the partner
    reports read from 1 Jan 2025 (two years, no running PD reaching further back)."""
    monkeypatch.setattr(windows, "WINDOW_RECORDS", 20)
    monkeypatch.setattr(sync, "CAP_FLOOR", 20)
    monkeypatch.setattr(sync, "CAP_ROUND", 10)
    settings.ETOOLS_DATAMART_PAGE_SIZE = 5
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2


@pytest.fixture(autouse=True)
def suspected(monkeypatch):
    """Every large read here is one the sync suspects cut: it is windowed from 20 records, as before A."""
    monkeypatch.setattr(sync, "cut_sign", lambda details: "pages_cut")


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def run(name: str) -> SyncRun:
    (found,) = sync.sync_all(only=[name], triggered_by="test", client=client())
    return found


def spread(n: int, first: dt.date = dt.date(2025, 1, 3), step: int = 5, start: int = 1) -> list[dict]:
    """``n`` partner report rows, one every ``step`` days from ``first``, ids from ``start``."""
    return [report(start + i, (first + dt.timedelta(days=i * step)).isoformat()) for i in range(n)]


def held(pk: int, start: str = "2025-06-01") -> dm.ReportedIndicator:
    """A partner report row an earlier read wrote, in the window the sync reads."""
    return dm.ReportedIndicator.objects.create(datamart_id=pk, period_start=start, indicator="held")


def counts(fake: FakeDatamart) -> list[dict[str, str]]:
    """The one-record requests the read made (proofs, plan, splits, end check)."""
    return [q for q in fake.requests if q.get("page_size") == "1"]


def ids() -> set[int]:
    return set(dm.ReportedIndicator.objects.values_list("datamart_id", flat=True))


@responses.activate
def test_a_small_read_makes_today_s_requests():
    fake = FakeDatamart(spread(7))
    fake.register(REPORTS)
    found = run("partner_reports")
    assert found.status == SyncRun.Status.SUCCEEDED
    asked = {"page_size": "5", "country_name": "Lebanon", f"{KEY}__gte": "2025-01-01"}
    assert fake.requests == [asked, {**asked, "page": "2"}]  # no count request, no window
    assert "windows" not in found.details and found.details["datamart_count"] == 7


@responses.activate
def test_a_cut_count_and_pages_on_one_read_remove_nothing():
    """The Datamart holds 120 rows, answers 50 with a count of 50 (round) and ignores every window filter: the
    read is today's, it is not whole, and no row held is removed."""
    held(900)
    fake = FakeDatamart(spread(120), cap=50, ignore=(*DATE_FILTERS, *ID_FILTERS))
    fake.register(REPORTS)
    found = run("partner_reports")
    assert found.status == SyncRun.Status.PARTIAL
    details = found.details
    assert (details["cap_suspected"], details["removal_skipped"], details["removed"]) == (
        "windows_unproven",
        True,
        0,
    )
    assert details["window_bound"] == "" and details["ends_missing"] == 1  # the last id was never returned
    assert 900 in ids() and len(ids()) == 51


@responses.activate
def test_a_cut_read_holds_its_history_from_its_earliest_row_only():
    """The Datamart keeps the latest 50 of 120 rows and ignores every window filter: the read proves no history
    from the date it asked, so a running PD that started before the earliest row held (19 Dec 2025) has a
    partial history (its Reached is PRP's cumulative)."""
    FakeDatamart(list(reversed(spread(120))), cap=50, ignore=(*DATE_FILTERS, *ID_FILTERS)).register(REPORTS)
    found = run("partner_reports")
    assert (found.status, found.details["cap_suspected"]) == (SyncRun.Status.PARTIAL, "windows_unproven")
    assert found.details["read_from"] == "2025-01-01" and "whole_from" not in found.details
    assert reached.reports_read_from() == dt.date(2025, 12, 19)


@responses.activate
def test_a_capped_dataset_is_read_whole_in_date_windows():
    """120 rows, at most 50 to one query and a count cut to 50: read in date windows split below 20, every row
    arrives, the stale row goes, and the run says the Datamart's own count was cut."""
    held(900)
    fake = FakeDatamart(spread(120), cap=50)
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert ids() == set(range(1, 121))
    assert details["removed"] == 1 and "removal_skipped" not in details
    assert (details["datamart_count"], details["read"], details["windows_announced"]) == (50, 120, 120)
    assert details["window_bound"] == KEY and details["whole_count_capped"] is True
    assert details["windows_not_whole"] == 0 and all(w["announced"] < 20 for w in details["windows"])
    assert details["whole_from"] == "2025-01-01" and details["change_checks"] == ["window_counts"]
    windows_asked = [q for q in fake.requests if q.get("page_size") == "5"][1:]
    assert all(f"{KEY}__lt" in q for q in windows_asked[:-1])  # the open last window has no end


@responses.activate
def test_an_ignored_date_filter_falls_back_to_id_windows():
    fake = FakeDatamart(spread(120), cap=50, ignore=(f"{KEY}__lt", f"{KEY}__lte"))
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert details["window_bound"] == "id" and ids() == set(range(1, 121))
    assert "whole_from" not in details  # id windows say nothing of the history by date
    assert any("id__lt" in q for q in fake.requests)
    assert reached.reports_read_from() == dt.date(2025, 1, 1)  # every id window whole: all it asked for


@responses.activate
def test_with_no_window_filter_the_ends_test_finds_a_cut():
    """A count cut to 45 (not round) with every record of it returned looks whole; the Datamart takes the
    ordering, and its last record by id was never returned: the answer was cut, nothing is removed."""
    held(900)
    fake = FakeDatamart(spread(60), cap=45, ignore=(*DATE_FILTERS, *ID_FILTERS))
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert (found.status, details["cap_suspected"], details["ends_missing"]) == (
        SyncRun.Status.PARTIAL,
        "ends_missing",
        1,
    )
    assert details["removal_skipped"] is True and 900 in ids()
    orders = [q["ordering"] for q in fake.requests if "ordering" in q]
    assert orders == ["id", "-id"]


@responses.activate
def test_a_next_link_that_drops_the_window_is_never_whole():
    held(900)
    fake = FakeDatamart(spread(60), ignore_on_next=(f"{KEY}__lt",))
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert details["outside_window"] > 0 and details["removal_skipped"] is True
    assert details["windows_not_whole"] > 0 and 900 in ids()


def _register(fake: FakeDatamart, answer=None) -> None:
    fake.url = REPORTS
    responses.add_callback(responses.GET, re.compile(rf"{REPORTS}(\?.*)?$"), callback=answer or fake._answer)


def _dry(records: list[dict], since: str = "2025-01-01", **options) -> list[dict[str, str]]:
    """The requests a read of ``records`` from ``since`` makes, in order (the same read gives the same
    requests)."""
    fake = FakeDatamart(records, **options)
    with responses.RequestsMock() as mock:
        fake.url = REPORTS
        mock.add_callback(responses.GET, re.compile(rf"{REPORTS}(\?.*)?$"), callback=fake._answer)
        list(windows.Reading(client(), "prp/datareport", {f"{KEY}__gte": since}, date_key=KEY))
    return fake.requests


@responses.activate
def test_a_count_that_changes_during_the_read_removes_nothing():
    rows = spread(61)
    before_end = len(_dry(rows)) - 1  # the end check is the last request
    held(900)
    FakeDatamart(rows, changes=(before_end, [report(500, "2025-02-01")])).register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert (details["changed"], details["datamart_count"], details["whole_count_end"]) == (True, 61, 62)
    assert details["windows_not_whole"] == 0 and details["removal_skipped"] is True and 900 in ids()


@responses.activate
def test_a_window_whose_count_changes_during_the_read_is_not_whole():
    """The whole count is a limit (50 of 120): asked again it says 50 whatever happened, so a row added to
    a window between its count and its read is caught by the window's own count."""
    rows = spread(120)
    asked = _dry(rows, cap=50)
    reads_from = next(i for i, q in enumerate(asked) if i and q.get("page_size") == "5")  # after every count
    held(900)
    FakeDatamart(rows, cap=50, changes=(reads_from, [report(500, "2026-09-30")])).register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL and "changed" not in details
    assert details["windows_not_whole"] == 1 and details["removal_skipped"] is True and 900 in ids()
    assert details["change_checks"] == ["window_counts"]


@responses.activate
def test_windows_adding_up_to_less_than_the_count_are_not_whole():
    """The first request counts rows no window holds (it ignores the date the sync asks from, the others do
    not): the windows add up to less than that count, and the read is not whole."""
    rows = spread(40) + [report(800 + i, "2001-01-01") for i in range(5)]
    held(900)
    fake = FakeDatamart(rows, ignore=(f"{KEY}__gte",))

    def answer(request):
        found = fake._answer(request)
        fake.ignore = set()  # from the second request on, the date filter is applied
        return found

    _register(fake, answer)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert (details["windows_short"], details["datamart_count"], details["windows_announced"]) == (
        True,
        45,
        40,
    )
    assert details["removal_skipped"] is True and 900 in ids()


@responses.activate
def test_a_one_day_window_too_large_is_split_by_id_or_said():
    rows = [report(pk, "2026-03-01") for pk in range(1, 31)]
    fake = FakeDatamart(rows)
    fake.register(REPORTS)
    found = run("partner_reports")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert ids() == set(range(1, 31))
    split = [w for w in found.details["windows"] if " id " in w["from"]]
    assert split and all(w["whole"] for w in split)

    responses.reset()
    held(900, "2026-03-01")
    fake = FakeDatamart(rows, ignore=ID_FILTERS)
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    (too_large,) = details["windows_not_whole_first"]
    assert (too_large["from"], too_large["to"], too_large["announced"]) == ("2026-03-01", "2026-03-02", 30)
    assert details["removal_skipped"] is True and 900 in ids()


def _history(settings) -> list[dict]:
    """Rows from 2019 on: 15 in 2019, then 8 a year; read from 1 Jan 2019."""
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 8
    rows = spread(15, dt.date(2019, 1, 10), step=10)
    for year in range(2020, 2027):
        rows += spread(8, dt.date(year, 1, 10), step=20, start=year * 100)
    return rows


@responses.activate
def test_whole_from_sets_the_partial_history(settings):
    """At most 12 rows to one query, with the true count: the 2019 window (15) is not read whole. History is
    held whole from 1 Jan 2020 on: a running PD that started in 2019 has a partial history."""
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Partner A", partner_type="NGO", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PCA-1/PD1", title="PD", partner=partner, status="active",
                            start=dt.date(2019, 3, 1))  # fmt: skip
    fake = FakeDatamart(_history(settings), cap=12, cut_count=False)
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL and details["windows_not_whole"] == 1
    assert (details["read_from"], details["whole_from"]) == ("2019-01-01", "2020-01-01")
    assert reached.reports_read_from() == dt.date(2020, 1, 1) > pd.start  # its history is partial


@responses.activate
def test_an_open_window_not_whole_keeps_the_gap_of_an_earlier_window():
    """At most 12 rows to one query, with the true count: 2025 (16 rows) is not read whole, and neither is the
    open last window (15 reports starting in 2027). The open window says nothing of the history: it is held
    whole from 1 Jan 2026, after the 2025 gap."""
    rows = spread(16, dt.date(2025, 1, 10), step=10) + spread(15, dt.date(2027, 1, 10), step=5, start=500)
    FakeDatamart(rows, cap=12, cut_count=False).register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL and details["windows_not_whole"] == 2
    assert details["whole_from"] == "2026-01-01" and reached.reports_read_from() == dt.date(2026, 1, 1)


def _running_pd(start: dt.date) -> PCA:
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Partner A", partner_type="NGO", vendor_number="V1"
    )
    return PCA.objects.create(etl_id="11", number="LEB/PCA-1/PD1", title="PD", partner=partner, status="active",
                              start=start)  # fmt: skip


@responses.activate
def test_a_past_window_not_whole_counts_whatever_the_open_window_gives(settings):
    """The 2019 window is not read whole and the open window (15 reports starting in 2027) neither: history is
    held whole from 1 Jan 2020, and a running PD that started in 2019 has a partial history."""
    pd = _running_pd(dt.date(2019, 3, 1))
    rows = _history(settings) + spread(15, dt.date(2027, 1, 10), step=5, start=5000)
    FakeDatamart(rows, cap=12, cut_count=False).register(REPORTS)
    found = run("partner_reports")
    assert found.details["read_from"] == "2019-01-01" and found.details["whole_from"] == "2020-01-01"
    assert reached.reports_read_from() == dt.date(2020, 1, 1) > pd.start


@responses.activate
def test_a_changed_read_after_a_cut_run_still_counts_its_own_windows(settings):
    """The first windowed night after a run whose count was a limit (it covers nothing): the Datamart's count
    changes during the read, and the 2019 window is not read whole. Tonight's windows decide: history is held
    whole from 1 Jan 2020, never from the date the sync asked (1 Jan 2019)."""
    pd = _running_pd(dt.date(2019, 3, 1))
    rows = _history(settings)
    before_end = (
        len(_dry(rows, "2019-01-01", cap=12, cut_count=False)) - 1
    )  # the end check is the last request
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="partner_reports", status=SyncRun.Status.PARTIAL,
                           details={"read_from": "2019-01-01", "datamart_count": 10_000})  # fmt: skip
    FakeDatamart(rows, cap=12, cut_count=False, changes=(before_end, [report(9000, "2025-02-01")])).register(
        REPORTS
    )
    found = run("partner_reports")
    details = found.details
    assert details["changed"] is True and details["windows_not_whole"] == 1
    assert details["whole_from"] == "2020-01-01"
    assert reached.reports_read_from() == dt.date(2020, 1, 1) > pd.start


@responses.activate
def test_whole_from_keeps_a_window_an_earlier_run_read_whole(settings):
    rows = _history(settings)
    fake = FakeDatamart(rows)
    fake.register(REPORTS)
    assert run("partner_reports").details["whole_from"] == "2019-01-01"  # every window whole

    responses.reset()
    FakeDatamart(rows, cap=12, cut_count=False).register(REPORTS)
    found = run("partner_reports")  # 2019 not whole tonight: its rows of last night are still held
    assert (found.status, found.details["whole_from"]) == (SyncRun.Status.PARTIAL, "2019-01-01")

    # a run read before windows whose count was a limit covers nothing: tonight's windows decide
    SyncRun.objects.all().delete()
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="partner_reports", status=SyncRun.Status.PARTIAL,
                           details={"read_from": "2019-01-01", "datamart_count": 10_000})  # fmt: skip
    found = run("partner_reports")
    assert found.details["whole_from"] == "2020-01-01"


@responses.activate
def test_a_window_not_whole_never_deletes_through_the_last_window_count(settings):
    """2025 announces 15 and returns 12; 2026 is read whole and is the last window read, so the client's last
    count is 2026's: the read is still not whole, nothing is removed, and the run keeps the whole count."""
    rows = spread(15, dt.date(2025, 1, 10), step=10) + spread(10, dt.date(2026, 1, 10), step=10, start=100)
    held(900)
    fake = FakeDatamart(rows, cap=12, cut_count=False)
    fake.register(REPORTS)
    the_client = client()
    (found,) = sync.sync_all(only=["partner_reports"], triggered_by="test", client=the_client)
    assert the_client.last_count == 10  # the 2026 window's
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL and details["removal_skipped"] is True
    assert details["datamart_count"] == 25 and details["removed"] == 0 and 900 in ids()


@responses.activate
def test_lte_windows_when_lt_is_ignored():
    fake = FakeDatamart(spread(60), ignore=(f"{KEY}__lt",))
    fake.register(REPORTS)
    found = run("partner_reports")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert found.details["window_bound"] == KEY and ids() == set(range(1, 61))
    firsts = [q for q in fake.requests if q.get("page_size") == "5" and "page" not in q][1:]
    assert firsts and all(f"{KEY}__lt" not in q and f"{KEY}__lte" in q for q in firsts)
    # half-open windows end to end: each ends the day before the next one starts
    for window, following in zip(firsts, firsts[1:], strict=False):
        end = dt.date.fromisoformat(window[f"{KEY}__lte"])
        assert (end + dt.timedelta(days=1)).isoformat() == following[f"{KEY}__gte"]


@responses.activate
def test_an_empty_window_is_not_read():
    """Nothing starts in 2026 or later: those windows answer 0 to their count and are never read."""
    fake = FakeDatamart(spread(30, step=7))  # 3 Jan to 12 Jul 2025
    fake.register(REPORTS)
    found = run("partner_reports")
    assert found.status == SyncRun.Status.SUCCEEDED
    reads = [q for q in fake.requests if q.get("page_size") == "5"][1:]
    assert reads and all(q[f"{KEY}__gte"] < "2026-01-01" for q in reads)
    empty = [w for w in found.details["windows"] if w["announced"] == 0]
    assert empty and all(w["pages"] == 0 and w["whole"] for w in empty)


@responses.activate
def test_a_record_on_the_window_boundary_by_a_timezone_is_not_outside():
    """The Datamart filters in UTC: a row starting 1 Jan 2026 at 01:00 in Beirut is 2025's for it. Read as 1
    Jan 2026, it is one day past its window's end: a time zone, not a row outside."""
    rows = spread(30) + [report(700, "2026-01-01T01:00:00+03:00")]
    fake = FakeDatamart(rows)
    fake.register(REPORTS)
    found = run("partner_reports")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert found.details["outside_window"] == 0 and 700 in ids()


@responses.activate
def test_a_large_removal_waits_one_night(monkeypatch):
    monkeypatch.setattr(sync, "HOLD_FLOOR", 3)
    for pk in range(900, 905):
        held(pk)
    fake = FakeDatamart(spread(2))
    fake.register(REPORTS)
    first = run("partner_reports")
    assert first.status == SyncRun.Status.PARTIAL
    assert (first.details["removal_held"], first.details["removed"]) == (5, 0) and len(ids()) == 7
    _hours_ago(first, 21)
    second = run("partner_reports")  # the next night's whole read removes them
    assert second.status == SyncRun.Status.SUCCEEDED
    assert second.details["removed"] == 5 and ids() == {1, 2}


def _hours_ago(found: SyncRun, hours: int) -> None:
    """Move a run's start ``hours`` back (the run of the night before)."""
    SyncRun.objects.filter(pk=found.pk).update(started_at=found.started_at - dt.timedelta(hours=hours))


@responses.activate
def test_a_held_removal_is_held_again_by_a_sync_minutes_later(monkeypatch):
    """The sync pressed again minutes after the night's run (the Datamart's table still reloading) reads the
    same smaller set: it removes nothing either. The run a night after the first hold removes the rows."""
    monkeypatch.setattr(sync, "HOLD_FLOOR", 3)
    for pk in range(900, 905):
        held(pk)
    FakeDatamart(spread(2)).register(REPORTS)
    first = run("partner_reports")
    second = run("partner_reports")  # minutes later
    assert second.status == SyncRun.Status.PARTIAL
    assert (second.details["removal_held"], second.details["removed"]) == (5, 0) and len(ids()) == 7
    _hours_ago(first, 21)  # the first hold was a night ago: its chain of holds has waited long enough
    third = run("partner_reports")
    assert third.status == SyncRun.Status.SUCCEEDED
    assert third.details["removed"] == 5 and ids() == {1, 2}


@responses.activate
def test_the_plan_stops_at_max_counts(monkeypatch):
    monkeypatch.setattr(windows, "MAX_COUNTS", 6)
    held(900)
    fake = FakeDatamart(spread(120), cap=50)
    fake.register(REPORTS)
    found = run("partner_reports")
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert details["plan_incomplete"] is True and details["count_requests"] <= 6
    assert details["removal_skipped"] is True and 900 in ids()


@responses.activate
def test_a_large_dataset_kept_whole_is_read_in_id_windows_and_a_cut_removes_none(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    dm.DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="999")
    records = [v2(pk, "2490") for pk in range(1, 61)]
    FakeDatamart(records, cap=30).register(V2)
    found = run("prp_indicator_reports")
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert (details["window_bound"], details["documents"], details["documents_removed"]) == ("id", 60, 1)
    assert details["datamart_count"] == 30 and details["whole_count_capped"] is True

    responses.reset()
    dm.DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="999")
    FakeDatamart(records, cap=30, ignore=ID_FILTERS).register(V2)
    found = run("prp_indicator_reports")
    assert found.status == SyncRun.Status.PARTIAL
    assert (found.details["cap_suspected"], found.details["documents_removed"]) == ("windows_unproven", 0)
    assert dm.DatamartDocument.objects.filter(dataset="prp_indicator_reports", record_key="999").exists()


@responses.activate
def test_pd_indicators_large_are_read_in_id_windows():
    dm.PDIndicator.objects.create(datamart_id=999, title="gone")
    records = [{"id": pk, "title": f"Indicator {pk}", "country_name": "Lebanon"} for pk in range(1, 46)]
    fake = FakeDatamart(records, cap=30)
    fake.register(INDICATORS)
    found = run("pd_indicators")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert found.details["window_bound"] == "id" and found.details["removed"] == 1
    assert set(dm.PDIndicator.objects.values_list("datamart_id", flat=True)) == set(range(1, 46))
