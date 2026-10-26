"""A Datamart read is windowed only when a cut is suspected (A): a round count, or a sign of a cut in the dataset's
previous finished run (``datamart_sync.cut_sign``). Any other read is today's, whatever its size, with the ends
test once it is large; and the history is counted from where the rows held really start (C). Pages that shift
during a read are said and remove nothing, and the next read asks a stable order when the Datamart takes one
(G). The Datamart is the fake of ``fake_datamart`` with the small constants of ``test_datamart_windows`` (a
read of 20 records or more is large, a round count of 20 or more a multiple of 10, pages of 5, a stable order
proven on pages of 5); nothing patches ``cut_sign``. A whole read keeps the rows it no longer returns, marked no
longer sent (``test_datamart_unsent``)."""

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

from .fake_datamart import FakeDatamart, report

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
REPORTS = f"{BASE}/api/latest/prp/datareport/"
KEY = "reporting_period_start_date"
ASKED = {"page_size": "5", "country_name": "Lebanon", f"{KEY}__gte": "2025-01-01"}  # the sync's own query


@pytest.fixture(autouse=True)
def small(settings, monkeypatch):
    """Large from 20 records, a round count of 20 or more a multiple of 10, pages of 5 (and of the stable order's
    proof), and the partner reports read from 1 Jan 2025."""
    monkeypatch.setattr(windows, "WINDOW_RECORDS", 20)
    monkeypatch.setattr(windows, "ORDER_PAGE", 5)
    monkeypatch.setattr(sync, "CAP_FLOOR", 20)
    monkeypatch.setattr(sync, "CAP_ROUND", 10)
    settings.ETOOLS_DATAMART_PAGE_SIZE = 5
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def run(name: str = "partner_reports") -> SyncRun:
    (found,) = sync.sync_all(only=[name], triggered_by="test", client=client())
    return found


def spread(
    n: int, first: dt.date = dt.date(2025, 1, 3), step: int = 5, start: int = 1, **extra
) -> list[dict]:
    """``n`` partner report rows, one every ``step`` days from ``first``, ids from ``start``."""
    return [report(start + i, (first + dt.timedelta(days=i * step)).isoformat(), **extra) for i in range(n)]


def held(pk: int, start: str = "2025-06-01") -> dm.ReportedIndicator:
    """A partner report row an earlier read wrote, in the window the sync reads."""
    return dm.ReportedIndicator.objects.create(datamart_id=pk, period_start=start, indicator="held")


def ids() -> set[int]:
    return set(dm.ReportedIndicator.objects.values_list("datamart_id", flat=True))


def _dry(records: list[dict]) -> list[dict[str, str]]:
    """The requests tonight's read of ``records`` makes, in order (the same read gives the same requests)."""
    fake = FakeDatamart(records)
    with responses.RequestsMock() as mock:
        fake.url = REPORTS
        mock.add_callback(responses.GET, re.compile(rf"{REPORTS}(\?.*)?$"), callback=fake._answer)
        reading = windows.Reading(
            client(), "prp/datareport", {f"{KEY}__gte": "2025-01-01"}, date_key=KEY, name="partner_reports"
        )
        list(reading)
    return fake.requests


@responses.activate
def test_a_large_country_blind_answer_is_read_in_one_answer():
    """The Datamart ignores country_name on prp/datareport (20-21 Oct 2026: every country's rows, whole): 47 rows,
    23 of Lebanon and 24 of another country, are read in one answer, with no proof or plan request. The ends
    test asks the first and the last record by id before the rest of the pages; no business area filter is
    asked, as no business area code is set or found."""
    held(900)
    rows = [
        report(n, "2025-03-01", **({} if n % 2 == 0 else {"country_name": "Syria"})) for n in range(1, 48)
    ]
    fake = FakeDatamart(rows, ignore=("country_name",))
    fake.register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert fake.requests == [
        ASKED,
        {**ASKED, "ordering": "id", "page_size": "1"},
        {**ASKED, "ordering": "-id", "page_size": "1"},
        *({**ASKED, "page": str(n)} for n in range(2, 11)),
    ]
    assert "window_bound" not in details and "windows_why" not in details and "ends_missing" not in details
    assert (details["datamart_count"], details["other_country_skipped"], details["removed"]) == (47, 24, 0)
    assert ids() == {*range(2, 48, 2), 900}  # the row of the window no longer returned: kept, marked
    assert details["no_longer_sent_new"] == 1
    assert details["area_filter"]["why"] == "no_code"


@responses.activate
def test_a_round_count_is_read_in_windows_and_says_why():
    held(900)
    FakeDatamart(spread(120), cap=50).register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert (details["windows_why"], details["window_bound"]) == ("round_count", KEY)
    assert ids() == {*range(1, 121), 900} and (details["removed"], details["no_longer_sent"]) == (0, 1)


@responses.activate
def test_a_page_shorter_than_the_first_removes_nothing_and_windows_the_next_night():
    """Page 3 of 10 comes back with 4 records of 5: the answer was cut, nothing is removed. The next night (no
    page cut) is read in windows because of it, and whole; the night after reads plain again."""
    held(900)
    rows = spread(47)
    FakeDatamart(rows, cut_pages=(3,)).register(REPORTS)
    first = run()
    details = first.details
    assert first.status == SyncRun.Status.PARTIAL
    assert (details["pages_cut"], details["removal_skipped"], details["removed"]) == (1, True, 0)
    assert "windows_why" not in details and 900 in ids() and 15 not in ids()

    responses.reset()
    FakeDatamart(rows).register(REPORTS)
    second = run()
    details = second.details
    assert second.status == SyncRun.Status.SUCCEEDED, details
    assert (details["windows_why"], details["window_bound"], details["removed"]) == ("pages_cut", KEY, 0)
    assert ids() == {*range(1, 48), 900} and "pages_cut" not in details and details["no_longer_sent"] == 1

    responses.reset()
    FakeDatamart(rows).register(REPORTS)
    third = run()
    assert third.status == SyncRun.Status.SUCCEEDED and "window_bound" not in third.details


@responses.activate
def test_a_plain_read_that_came_back_short_windows_the_next_night():
    """The Datamart announces 47 and returns 40, the 7 missing in the middle of the ids (the ends test cannot see
    them): fewer records than announced, nothing removed; the next night is read in windows and whole."""
    rows = spread(47)
    rows = rows[:20] + rows[27:] + rows[20:27]  # the first 40 it returns hold the first and the last id
    FakeDatamart(rows, cap=40, cut_count=False).register(REPORTS)
    first = run()
    details = first.details
    assert first.status == SyncRun.Status.PARTIAL and "ends_missing" not in details
    assert (details["read"], details["datamart_count"], details["removal_skipped"]) == (40, 47, True)
    assert sync.cut_sign(details) == "read_short"

    responses.reset()
    FakeDatamart(rows, cap=40, cut_count=False).register(REPORTS)
    second = run()
    assert second.details["windows_why"] == "read_short"
    assert second.status == SyncRun.Status.SUCCEEDED and ids() == set(range(1, 48))


@responses.activate
def test_a_windowed_night_not_whole_without_a_cut_is_followed_by_a_plain_night():
    """The last read's page came back short: tonight is windowed, and the Datamart's count changes during the
    read (a row added before the end check). Not whole, nothing removed, and no sign of a cut: the next night
    reads plain."""
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="partner_reports",
                           status=SyncRun.Status.PARTIAL, details={"pages_cut": 1})  # fmt: skip
    rows = spread(47)
    before_end = len(_dry(rows)) - 1  # the end check is the last request
    held(900)
    FakeDatamart(rows, changes=(before_end, [report(500, "2025-02-01")])).register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert (details["windows_why"], details["changed"], details["removal_skipped"]) == (
        "pages_cut",
        True,
        True,
    )
    assert sync.cut_sign(details) == "" and 900 in ids()

    responses.reset()
    FakeDatamart([*rows, report(500, "2025-02-01")]).register(REPORTS)
    following = run()
    assert following.status == SyncRun.Status.SUCCEEDED and "window_bound" not in following.details
    assert 900 in ids() and following.details["no_longer_sent_new"] == 1  # kept, marked no longer sent


@pytest.mark.parametrize(
    ("details", "sign"),
    [
        ({"cap_suspected": "ends_missing"}, "cap_suspected"),
        ({"whole_count_capped": True, "window_bound": "id"}, "whole_count_capped"),
        ({"pages_cut": 1}, "pages_cut"),
        ({"windows_cut": 2, "window_bound": "id"}, "windows_cut"),
        ({"read": 40, "datamart_count": 47}, "read_short"),
        ({"read": 40, "datamart_count": 47, "window_bound": ""}, "read_short"),  # windowed, no filter proven
        # the order: an answer that looked cut first, then the windows' count, a cut page, a window cut, short
        ({"cap_suspected": "no_count", "pages_cut": 2, "read": 1, "datamart_count": 9}, "cap_suspected"),
        ({"whole_count_capped": True, "pages_cut": 1, "windows_cut": 1}, "whole_count_capped"),
        ({"windows_cut": 1, "pages_cut": 2}, "pages_cut"),
        ({"windows_cut": 1, "read": 1, "datamart_count": 9, "window_bound": "id"}, "windows_cut"),
        # read in windows: their own counts tell, not the read against the first count
        ({"read": 40, "datamart_count": 47, "window_bound": "id"}, ""),
        # records twice and as many others missing: pages shifting (the 21 Oct progress reports), not a cut
        ({"read": 150_221, "duplicates_read": 74, "datamart_count": 150_295, "removal_skipped": True}, ""),
        ({"datamart_count": 780_905}, ""),  # the 21 Oct partner reports: whole, no read recorded
        (
            {"read": 780_975, "datamart_count": 780_905, "window_bound": ""},
            "",
        ),  # records added during the read
        ({"changed": True, "duplicates_read": 3, "plan_incomplete": True, "window_bound": "id"}, ""),
        ({"pages_cut": 0, "windows_cut": 0, "cap_suspected": ""}, ""),
        ({}, ""),
    ],
)
def test_cut_sign(details, sign):
    assert sync.cut_sign(details) == sign


@responses.activate
def test_a_whole_read_records_where_its_feed_starts():
    """Read from 1 Jan 2025, the Datamart holds no report starting before 5 Jan 2026: the history is counted from
    the feed's first report (more than 100 days after the date asked)."""
    FakeDatamart(spread(10, dt.date(2026, 1, 5))).register(REPORTS)
    found = run()
    assert found.status == SyncRun.Status.SUCCEEDED
    assert (found.details["read_from"], found.details["feed_from"]) == ("2025-01-01", "2026-01-05")
    assert reached.reports_read_from() == dt.date(2026, 1, 5)
    assert reached.history_from() == reached.HistoryFrom(
        dt.date(2026, 1, 5), reached.FEED, dt.date(2025, 1, 1)
    )


@responses.activate
def test_rows_no_longer_sent_keep_the_history_of_the_rows_held():
    """The rows held start on 1 Feb 2025; tonight's whole read returns two reports from 5 Jan 2026: the rows it no
    longer returns are kept and marked, they still count, so the history stays counted from the date asked, the
    next night too (the Datamart dropping old reports never moves it forward)."""
    for pk in range(900, 905):
        held(pk, "2025-02-01")
    FakeDatamart(spread(2, dt.date(2026, 1, 5))).register(REPORTS)
    first = run()
    assert (first.details["no_longer_sent_new"], first.details["feed_from"]) == (5, "2025-02-01")
    assert "removal_held" not in first.details and first.details["removed"] == 0
    assert reached.history_from() == reached.HistoryFrom(
        dt.date(2025, 1, 1), reached.READ_FROM, dt.date(2025, 1, 1)
    )
    SyncRun.objects.filter(pk=first.pk).update(started_at=first.started_at - dt.timedelta(hours=21))
    second = run()
    assert (second.details["removed"], second.details["no_longer_sent"], second.details["feed_from"]) == (
        0,
        5,
        "2025-02-01",
    )
    assert reached.history_from().source == reached.READ_FROM


# ------------------------------------------------------------------------------- pages that shift during a read
def shifting() -> list[dict]:
    """47 partner report rows whose default order ends with ids 45 and 46: pages that shift by two lose those two,
    never the first or the last id, so the ends test finds nothing missing."""
    rows = spread(47)
    return rows[:20] + [rows[46]] + rows[20:46]


@responses.activate
def test_shifting_pages_are_said_and_remove_nothing():
    """From page 2 on, the Datamart's default order starts two records early: two come twice and two never come.
    The read is not whole, nothing is removed, and the run says so plainly."""
    held(900)
    FakeDatamart(shifting(), shift=2).register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert details["pages_shifted"] == {"twice": 2, "missing": 2} and "held_twice" not in details
    assert (details["removal_skipped"], details["removed"], details["duplicates_read"]) == (True, 0, 2)
    assert 900 in ids() and not {45, 46} & ids() and "ordering" not in details
    assert (
        sync.cut_sign(details) == ""
    )  # records twice and as many missing: no cut, the next night reads plain
    assert sync.shift_words(details) == (
        "2 records came twice while the Datamart's pages were read: rows moved between pages, and then as many "
        "others did not come, or the Datamart holds them twice. Nothing was removed; the next read asks a stable "
        "order."
    )


@responses.activate
def test_after_a_shift_the_read_asks_a_stable_order():
    """The next night proves ordering=id first (two pages in order, the reverse on another) and asks every page
    in it: no page shifts, the read is whole, and what the Datamart no longer returns is removed."""
    held(900)
    FakeDatamart(shifting(), shift=2).register(REPORTS)
    run()
    responses.reset()
    fake = FakeDatamart(shifting(), shift=2)
    fake.register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    order = {**ASKED, "ordering": "id"}
    assert fake.requests[:3] == [order, {**order, "page": "2"}, {**order, "ordering": "-id"}]
    assert details["ordering"] == {"key": "id"}
    assert all(r["ordering"] == "id" for r in fake.requests[3:] if r["page_size"] == "5")
    assert "pages_shifted" not in details and "duplicates_read" not in details
    assert ids() == {*range(1, 48), 900} and (details["removed"], details["no_longer_sent"]) == (0, 1)


@responses.activate
def test_an_ordering_the_datamart_ignores_is_never_asked():
    held(900)
    FakeDatamart(shifting(), shift=2, ignore=("ordering",)).register(REPORTS)
    run()
    responses.reset()
    fake = FakeDatamart(shifting(), shift=2, ignore=("ordering",))
    fake.register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert details["ordering"] == {"key": "", "why": "ignored", "tried": ["id"]}
    pages = [r for r in fake.requests[3:] if r["page_size"] == "5"]
    assert pages and not any("ordering" in r for r in pages)  # the read's pages: in the Datamart's own order
    assert details["pages_shifted"] == {"twice": 2, "missing": 2} and 900 in ids()
    assert sync.shift_words(details).endswith(
        "Nothing was removed; the Datamart takes no stable order (ordering by id is ignored)."
    )


@responses.activate
def test_records_still_twice_in_a_stable_order_are_said_to_be_held_twice():
    """The Datamart holds three rows twice: they come twice in its own order, and again under ordering=id, the
    next night. In a stable order no row moves between pages: it likely holds them twice. The read stays not
    whole, and nothing is removed."""
    held(900)
    rows = spread(47)
    rows = rows[:20] + rows[10:13] + rows[20:]  # ids 11, 12 and 13 held twice
    FakeDatamart(rows).register(REPORTS)
    first = run()
    assert first.details["pages_shifted"] == {"twice": 3, "missing": 3}
    responses.reset()
    FakeDatamart(rows).register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL and details["ordering"] == {"key": "id"}
    assert details["held_twice"] == 3 and "pages_shifted" not in details
    assert (details["removal_skipped"], details["removed"]) == (True, 0) and 900 in ids()
    assert sync.shift_words(details) == (
        "The Datamart returned 3 records twice in a stable order (by id): it likely holds them twice; tell the "
        "eTools Datamart team. Nothing was removed."
    )


# ------------------------------------------------- the other readings: locations, sites, partners, engagements...
AUDIT_RESULTS = f"{BASE}/api/latest/datamart/audit/results/"


def audit(pk: int) -> dict:
    """An audit result (``audit/results``) of an engagement NeuroDB does not hold: only its raw copy is written."""
    return {"id": pk, "reference_number": f"LEB/AUD/{pk}", "country_name": "Lebanon"}


@responses.activate
def test_an_engagement_dataset_whose_page_came_back_cut_is_windowed_the_next_night():
    """The locations, sites, partners, programme documents and the engagements' details (audit_results here) are
    read as every other dataset: a page cut one night is recorded with the Datamart's count, so the next night
    is read in windows, by id, and whole."""
    rows = [audit(n) for n in range(1, 48)]
    FakeDatamart(rows, cut_pages=(3,)).register(AUDIT_RESULTS)
    first = run("audit_results")
    details = first.details
    assert first.status == SyncRun.Status.PARTIAL
    assert (details["pages_cut"], details["datamart_count"], details["removal_skipped"]) == (1, 47, True)
    assert sync.cut_sign(details) == "pages_cut"

    responses.reset()
    FakeDatamart(rows).register(AUDIT_RESULTS)
    second = run("audit_results")
    details = second.details
    assert second.status == SyncRun.Status.SUCCEEDED, details
    assert (details["windows_why"], details["window_bound"], details["documents"]) == ("pages_cut", "id", 47)
    assert "pages_cut" not in details


@responses.activate
def test_an_engagement_dataset_asks_the_stable_order_every_night_once_proven():
    """Records came twice on audit_results: the next night proves ordering=id, reads whole and records the order
    asked, so the night after asks it again (the nights no longer alternate between records twice and whole)."""
    rows = [audit(n) for n in range(1, 48)]
    rows = rows[:20] + [rows[46]] + rows[20:46]  # the shift loses ids 45 and 46, never the first or the last
    nights = []
    for _ in range(3):
        responses.reset()
        fake = FakeDatamart(rows, shift=2)
        fake.register(AUDIT_RESULTS)
        nights.append((run("audit_results"), fake.requests))
    (first, _), (second, _), (third, asked) = nights
    assert first.status == SyncRun.Status.PARTIAL
    assert (first.details["pages_shifted"], first.details["duplicates_read"]) == (
        {"twice": 2, "missing": 2},
        2,
    )
    for night in (second, third):
        assert night.status == SyncRun.Status.SUCCEEDED, night.details
        assert night.details["ordering"] == {"key": "id"} and "duplicates_read" not in night.details
    assert all(r["ordering"] == "id" for r in asked[3:] if r["page_size"] == "5")  # after the three proofs
