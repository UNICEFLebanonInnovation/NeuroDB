"""Records the eTools Datamart sends twice (25 Oct 2026: reports/indicators announced 8,899 and sent 6,960
distinct records, night after night; PRP's progress reports, 74 short). One answer of the Datamart, one page,
lists each of its rows once: a record whose every copy came identical in one answer is held that many times by
the Datamart, proven (``copies_proven``), and a plain read whose records twice are all proven is whole when the
records it received, copies included, are every one it announced, so what it no longer returns is removed.
Copies on two pages side by side are asked again in one small request covering both places. A record twice
that no answer proves (a page shift, copies too far apart, copies that differ) leaves the read not whole, and
nothing is removed: a record missing is never removed. The Datamart is the fake of ``fake_datamart``, pages of
5."""

import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import raw_admin, runfacts
from neurodb.datamart.models import DatamartDocument
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools import probe, windows
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session

from .fake_datamart import FakeDatamart

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
INDICATORS = f"{BASE}/api/latest/datamart/reports/indicators/"
GRANTS = f"{BASE}/api/latest/datamart/funds/grants/"
NAME = "cp_indicators"  # reports/indicators: kept whole, its records carry no id (held under their hash)


@pytest.fixture(autouse=True)
def small(settings):
    settings.ETOOLS_DATAMART_PAGE_SIZE = 5


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def run(name: str = NAME) -> SyncRun:
    (found,) = sync.sync_all(only=[name], triggered_by="test", client=client())
    return found


def indicator(n: int, **extra) -> dict:
    """An indicator with its baseline and target, as reports/indicators sends it: no id."""
    return {"pd_sffa_reference_number": "LEB/PD1", "label": f"Indicator {n}", "baseline": "0",
            "target_numerator": str(10 * n), "country_name": "Lebanon", **extra}  # fmt: skip


def holds(*records: dict) -> None:
    """Rows an earlier read wrote, under their hash."""
    for record in records:
        DatamartDocument.objects.create(dataset=NAME, record_key=sync.content_key(record), data=record)


def keys() -> set[str]:
    return set(DatamartDocument.objects.filter(dataset=NAME).values_list("record_key", flat=True))


ROWS = [indicator(n) for n in range(1, 13)]
CHANGED = indicator(
    4, target_numerator="35"
)  # indicator 4 as an earlier read held it, before its target changed


@responses.activate
def test_records_the_datamart_sends_twice_on_one_page_are_its_own_and_the_read_is_whole():
    """Indicators 2 and 7 come twice, each pair on one page: the Datamart holds them twice. 12 distinct records
    and 2 copies are the 14 it announced: the read is whole, and the old version of indicator 4 is removed."""
    holds(CHANGED, ROWS[0])
    listed = ROWS[:2] + [ROWS[1]] + ROWS[2:7] + [ROWS[6]] + ROWS[7:]  # pages: 1 2 2 3 4 | 5 6 7 7 8 | ...
    fake = FakeDatamart(listed)
    fake.register(INDICATORS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert details["copies_proven"] == {"twice": 2, "same_page": 2, "asked_again": 0}
    assert (details["duplicates_read"], details["documents"], details["documents_removed"]) == (2, 12, 1)
    assert (
        "removal_skipped" not in details
        and "pages_shifted" not in details
        and "confirm_requests" not in details
    )
    assert keys() == {sync.content_key(r) for r in ROWS}
    assert all(r["page_size"] == "5" for r in fake.requests)  # nothing asked again
    assert sync.shift_words(details) == (
        "The Datamart sent 2 records twice (identical), every copy in one of its answers (on one page): it holds "
        "them twice, as one answer never lists a row twice. NeuroDB keeps one of each."
    )
    # Raw eTools data: what eTools said it had is what NeuroDB received and the copies it sent
    facts = runfacts.facts(NAME)
    assert (facts.said_it_had, facts.received, facts.sent_twice, facts.copies_proven) == (14, 12, 2, 2)
    assert (facts.never_came, facts.twice_unproven, facts.complete) == (0, 0, (True, "Yes"))
    assert raw_admin._not_kept(facts).endswith("; 2 the Datamart sent twice (identical)")


@responses.activate
def test_copies_on_two_pages_side_by_side_are_asked_again_in_one_request():
    """Indicator 5 ends page 1 and starts page 2: one request of 2 records (page 3) covers both places; it holds
    both copies, identical, so they are proven and the read is whole."""
    holds(CHANGED)
    fake = FakeDatamart(ROWS[:5] + [ROWS[4]] + ROWS[5:])
    fake.register(INDICATORS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    asked = [r for r in fake.requests if r["page_size"] != "5"]
    assert asked == [{"page_size": "2", "page": "3", "country_name": "Lebanon"}]
    assert details["copies_proven"] == {"twice": 1, "same_page": 0, "asked_again": 1}
    assert (details["confirm_requests"], details["documents_removed"]) == (1, 1)
    assert sync.shift_words(details).startswith(
        "The Datamart sent 1 record twice (identical), every copy in one of its answers (found together again "
        "in one more request)"
    )


@responses.activate
def test_a_page_shift_is_never_taken_for_copies_and_removes_nothing():
    """From page 2 on, the Datamart's default order starts one record early: indicator 5 comes twice and
    indicator 12 never comes. The request asked again holds indicator 5 once (one answer lists a row once): not
    the Datamart's own copy, so the read is not whole and indicator 12, still held, is not removed."""
    holds(CHANGED, ROWS[11])
    fake = FakeDatamart(ROWS, shift=1)
    fake.register(INDICATORS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.PARTIAL
    assert (details["removal_skipped"], details["documents_removed"], details["confirm_requests"]) == (
        True,
        0,
        1,
    )
    assert details["pages_shifted"] == {"twice": 1, "missing": 1} and "copies_proven" not in details
    assert {sync.content_key(ROWS[11]), sync.content_key(CHANGED)} <= keys()
    facts = runfacts.facts(NAME)
    assert (facts.said_it_had, facts.received, facts.never_came, facts.twice_unproven) == (12, 11, 1, 1)
    assert facts.gap_by_repeats
    assert facts.complete == (
        False,
        "No: 1 were sent twice and may stand for others that did not come; nothing was removed",
    )
    assert raw_admin._not_kept(facts).endswith("; 1 sent twice (counted once)")


@responses.activate
def test_copies_too_far_apart_stay_not_proven_and_nothing_is_asked_again():
    """Indicator 1 comes first and again after indicator 12, three pages later: no answer of one page can hold
    both, so nothing is asked again; the Datamart may hold it twice, or a shift may have put it in another's
    place: the read is not whole, and the source page says why."""
    holds(CHANGED)
    fake = FakeDatamart([*ROWS, ROWS[0]])
    fake.register(INDICATORS)
    found = run()
    assert found.status == SyncRun.Status.PARTIAL and found.details["removal_skipped"]
    assert all(r["page_size"] == "5" for r in fake.requests) and "confirm_requests" not in found.details
    assert sync.content_key(CHANGED) in keys()
    # Raw eTools data: the record twice, not proven, may stand for the one that did not come; the source page
    # then says NeuroDB counts a copy as eTools' own only when every copy comes identical in one answer
    facts = runfacts.facts(NAME)
    assert (facts.said_it_had, facts.received, facts.never_came, facts.twice_unproven) == (13, 12, 1, 1)
    assert (facts.sent_twice, facts.copies_proven, facts.gap_by_repeats) == (1, 0, True)
    assert facts.complete == (
        False,
        "No: 1 were sent twice and may stand for others that did not come; nothing was removed",
    )
    assert raw_admin._not_kept(facts).endswith("; 1 sent twice (counted once)")


@responses.activate
def test_a_record_twice_whose_copies_differ_is_not_proven():
    """Grant 2 comes twice on one page with two names: one answer holds two rows of id 2, but not one record
    twice; the read is not whole and grant 3 stays. Sent twice identical, the copies are the Datamart's own, and
    grant 3, which it no longer holds, is removed."""

    def grant(pk: int, **extra) -> dict:
        return {"id": pk, "name": f"SC{pk}", "donor": "EU", "country_name": "Lebanon", **extra}

    for pk in (1, 2, 3):
        dm.Grant.objects.create(datamart_id=pk, name=f"SC{pk}", donor="EU")
    FakeDatamart([grant(1), grant(2), grant(2, name="SC2 renamed")]).register(GRANTS)
    found = run("grants")
    assert found.status == SyncRun.Status.PARTIAL and found.details["removal_skipped"]
    assert "copies_proven" not in found.details and dm.Grant.objects.filter(datamart_id=3).exists()

    responses.reset()
    FakeDatamart([grant(1), grant(2), grant(2)]).register(GRANTS)
    found = run("grants")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert found.details["copies_proven"] == {"twice": 1, "same_page": 1, "asked_again": 0}
    assert sorted(dm.Grant.objects.values_list("datamart_id", flat=True)) == [1, 2]


# ------------------------------------------------------------- an order the records do not carry the key of
class _Unkeyed:
    """A Datamart that orders 200 records by an id it does not send: pages share no record, so ordering=id is
    taken, but nothing in the records shows the order."""

    def first_page(self, dataset, params, country=True):
        rows = [{"label": f"Indicator {n}"} for n in range(200)]
        if str(params.get("ordering", "")).startswith("-"):
            rows.reverse()
        size, page = int(params["page_size"]), int(params.get("page", 1))
        return 200, rows[(page - 1) * size : page * size]


def test_an_order_proven_on_records_without_its_key_is_marked_unchecked():
    found, tried = windows.prove_ordering(_Unkeyed(), "reports/indicators", {}, keys=("id",))
    assert found == {"key": "id", "carried": False}
    assert (tried[0]["verdict"], tried[0]["carried"]) == ("proven", False)


@responses.activate
def test_records_twice_under_an_order_not_checked_are_never_said_held_twice(monkeypatch):
    """The last read had records twice, so the next one asks ordering=id, taken on records that carry no id:
    the order is not checked, so records that still come twice are said as pages that shift, not as held twice."""
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target=NAME, status=SyncRun.Status.PARTIAL,
                           details={"removal_skipped": True, "read": 12, "datamart_count": 13,
                                    "duplicates_read": 1})  # fmt: skip
    monkeypatch.setattr(windows, "prove_ordering", lambda *a, **k: ({"key": "id", "carried": False}, []))
    FakeDatamart([*ROWS, ROWS[0]]).register(INDICATORS)
    found = run()
    details = found.details
    assert details["ordering"] == {"key": "id", "carried": False} and "held_twice" not in details
    assert details["pages_shifted"] == {"twice": 1, "missing": 1}
    assert sync.shift_words(details).endswith(
        "Nothing was removed; the records do not carry id, so the order asked by id cannot be checked, and the "
        "next read is in the Datamart's own order again."
    )


# ------------------------------------------------------------------ the order a read with copies is asked in
HIDDEN = [
    indicator(n, id=n) for n in range(1, 31)
]  # 30 indicators the Datamart orders by an id it never sends
HIDDEN.insert(
    7, indicator(7, id=999)
)  # indicator 7 held twice: its second row, written later, right after it


@pytest.mark.parametrize(
    ("before", "wanted"),
    [
        ({}, False),
        ({"duplicates_read": 2, "pages_shifted": {"twice": 2, "missing": 2}}, True),  # not proven: ask an order
        ({"duplicates_read": 2, "copies_proven": {"twice": 2}}, False),  # every copy proven: whole as it was
        ({"duplicates_read": 3, "copies_proven": {"twice": 2}}, True),
        ({"ordering": {"key": "id"}}, True),  # an order taken stays asked
        ({"ordering": {"key": "id"}, "duplicates_read": 1, "held_twice": 1}, True),
        ({"ordering": {"key": "id", "carried": False}, "duplicates_read": 1, "copies_proven": {"twice": 1}}, True),
        # an order the records do not carry, that left copies not proven: back to the Datamart's own order
        ({"ordering": {"key": "id", "carried": False}, "duplicates_read": 1,
          "pages_shifted": {"twice": 1, "missing": 1}}, False),
        ({"duplicates_read": 2, "window_bound": "id"}, True),  # a windowed read proves no copy
    ],
)  # fmt: skip
def test_an_order_is_asked_after_records_twice_not_proven(before, wanted):
    assert windows.order_wanted(before) is wanted


@responses.activate
def test_a_read_whose_copies_were_proven_stays_in_the_default_order(monkeypatch):
    """Indicator 7 is held twice: side by side in the Datamart's own order, 24 places apart by the id it does not
    send. The first night proves both copies on one page and is whole; the nights after ask no order (asked by id,
    the copies would be too far apart to prove, night after night) and stay whole."""
    monkeypatch.setattr(windows, "ORDER_PAGE", 5)
    fake = FakeDatamart(HIDDEN, hidden="id")
    fake.register(INDICATORS)
    for _ in range(3):
        found = run()
        assert found.status == SyncRun.Status.SUCCEEDED, found.details
        assert found.details["copies_proven"] == {"twice": 1, "same_page": 1, "asked_again": 0}
        assert "ordering" not in found.details
    assert not any("ordering" in r for r in fake.requests)
    assert runfacts.facts(NAME).complete == (True, "Yes")


@responses.activate
def test_an_order_the_records_do_not_carry_is_dropped_once_it_left_copies_not_proven(monkeypatch):
    """The last read asked ordering=id on records that carry no id, and indicator 7's copies came too far apart
    to prove: the next read is in the Datamart's own order again, where they come side by side, and is whole."""
    monkeypatch.setattr(windows, "ORDER_PAGE", 5)
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target=NAME, status=SyncRun.Status.PARTIAL,
                           details={"removal_skipped": True, "read": 30, "datamart_count": 31, "duplicates_read": 1,
                                    "ordering": {"key": "id", "carried": False},
                                    "pages_shifted": {"twice": 1, "missing": 1}})  # fmt: skip
    fake = FakeDatamart(HIDDEN, hidden="id")
    fake.register(INDICATORS)
    found = run()
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert not any("ordering" in r for r in fake.requests) and "ordering" not in found.details


class _ById:
    """A Datamart that takes ordering=id on 200 reports with ids. ``held``: it holds report 50 twice, so in id
    order its copies end page 1 and start page 2 of the order proof; else page 2 starts one record early (rows
    that moved between the two requests)."""

    def __init__(self, held: bool) -> None:
        rows = [{"id": n, "report_type": "QPR"} for n in range(1, 201)]
        self.rows = [*rows[:50], dict(rows[49]), *rows[50:]] if held else rows
        self.shift, self.asked = not held, []

    def first_page(self, dataset, params, country=True):
        self.asked.append(dict(params))
        rows = sorted(
            self.rows, key=lambda r: r["id"], reverse=str(params.get("ordering", "")).startswith("-")
        )
        size, page = int(params["page_size"]), int(params.get("page", 1))
        start = (page - 1) * size - (1 if self.shift and page > 1 else 0)
        return len(rows), rows[start : start + size]


def test_copies_the_datamart_holds_across_the_order_proof_s_pages_never_hide_the_order():
    """Report 50's two copies end page 1 and start page 2 of ordering=id: one answer of both places lists it twice
    as well, so the Datamart holds it twice and takes the order. A page that starts one record early shares a
    record too, but one answer of both places lists it once: a shift, and the order is not proven."""
    held = _ById(held=True)
    found, tried = windows.prove_ordering(held, "prp/datareport", {}, keys=("id",))
    assert found == {"key": "id"}
    assert (tried[0]["verdict"], tried[0]["shared"], tried[0]["shared_held"]) == ("proven", 1, 1)
    assert held.asked[-1] == {"ordering": "id", "page_size": 2 * windows.ORDER_PAGE}
    assert "the 1 record they share it holds twice" in probe.order_verdict("prp/datareport", tried)

    shifted = _ById(held=False)
    found, tried = windows.prove_ordering(shifted, "prp/datareport", {}, keys=("id",))
    assert found == {"key": "", "why": "ignored", "tried": ["id"]}
    assert (tried[0]["shared"], "shared_held" in tried[0]) == (1, False)


# --------------------------------------------------------------- what NeuroDB received, a record sent twice
def grant(pk: int, **extra) -> dict:
    return {"id": pk, "name": f"SC{pk}", "donor": "EU", "country_name": "Lebanon", **extra}


@responses.activate
def test_a_whole_read_counts_each_record_received_once():
    """Grant 2 comes twice on one page, the Datamart's own copy: NeuroDB received 2 records, both updated by the
    sync, and Raw eTools data's Complete? is Yes, with nothing unexplained. Grant 3, of Syria, sent twice through a
    country filter the Datamart ignores, is one record left out."""
    FakeDatamart([grant(1), grant(2), grant(2)]).register(GRANTS)
    found = run("grants")
    assert found.status == SyncRun.Status.SUCCEEDED and found.details["read"] == 2
    facts = runfacts.facts("grants")
    assert (facts.said_it_had, facts.received, facts.copies_proven, facts.updated_by_sync) == (3, 2, 1, 2)
    assert (facts.never_came, facts.unexplained, facts.complete) == (0, 0, (True, "Yes"))

    responses.reset()
    syria = grant(3, country_name="Syria")
    FakeDatamart([grant(1), syria, syria], ignore=("country_name",)).register(GRANTS)
    found = run("grants")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert (found.details["read"], found.details["other_country_skipped"]) == (2, 1)
    facts = runfacts.facts("grants")
    assert (facts.received, facts.other_country, facts.updated_by_sync, facts.unexplained) == (2, 1, 1, 0)
    assert facts.complete == (True, "Yes")
