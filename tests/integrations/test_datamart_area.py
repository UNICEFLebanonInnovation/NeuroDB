"""A business area filter proven before it is used (B): the partner reports (``prp/datareport``) and PRP's progress
reports (``sources/prp/unicefprogressreport``) are read by business area only once the Datamart proves, that night,
that it applies the filter (one count with none, one page with each key in turn), and never on a night that must
read every record (no code, a row held naming none, the weekly read). A record of another business area that
comes through a filter is never written and the read removes nothing. The Datamart is the fake of
``fake_datamart``; its rows are of Lebanon (business area 2490) and of another country (0990)."""

import datetime as dt

import pytest
import responses
from django.utils import timezone

from neurodb.core.models import DatamartSetting, SyncRun
from neurodb.datamart import models as dm
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools import probe
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session

from .fake_datamart import FakeDatamart, report

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
REPORTS = f"{BASE}/api/latest/prp/datareport/"
PROGRESS = f"{BASE}/api/latest/sources/prp/unicefprogressreport/"
KEY = "reporting_period_start_date"
ASKED = {"country_name": "Lebanon", f"{KEY}__gte": "2025-01-01"}  # the sync's own query
AREA_KEYS = ("business_area", "business_area_code")


@pytest.fixture(autouse=True)
def window(settings):
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2  # the partner reports read from 1 Jan 2025


@pytest.fixture
def code(db):
    return DatamartSetting.objects.create(pk=1, prp_business_area="2490")


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def run(name: str = "partner_reports") -> SyncRun:
    (found,) = sync.sync_all(only=[name], triggered_by="test", client=client())
    return found


def lebanon(pk: int, **extra) -> dict:
    return report(pk, "2025-03-01", business_area="2490", **extra)


def other(pk: int, **extra) -> dict:
    return report(pk, "2025-03-01", business_area="0990", country_name="Syria", **extra)


def mixed() -> list[dict]:
    """Six rows of Lebanon and six of another country, one after the other."""
    return [row for n in range(1, 7) for row in (lebanon(n), other(100 + n))]


def held(pk: int, area: str | None = "2490") -> dm.ReportedIndicator:
    """A partner report row an earlier read wrote, in the window the sync reads, naming ``area``."""
    data = {"business_area": area} if area is not None else {}
    return dm.ReportedIndicator.objects.create(datamart_id=pk, period_start="2025-06-01", data=data)


def earlier(
    name: str = "partner_reports", days: float = 2, status=SyncRun.Status.SUCCEEDED, **details
) -> SyncRun:
    """A finished run of ``name`` ``days`` ago (with no ``area_filter``: a read with no business area filter)."""
    return SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target=name, status=status,
                                  started_at=timezone.now() - dt.timedelta(days=days), details=details)  # fmt: skip


def ago(found: SyncRun, days: int) -> None:
    SyncRun.objects.filter(pk=found.pk).update(started_at=found.started_at - dt.timedelta(days=days))


def ids() -> set[int]:
    return set(dm.ReportedIndicator.objects.values_list("datamart_id", flat=True))


def area_keys(fake: FakeDatamart) -> list[str]:
    """The business area key of each request made, "" for none."""
    return [next((k for k in AREA_KEYS if k in q), "") for q in fake.requests]


@responses.activate
def test_a_business_area_filter_the_datamart_applies_is_proven_then_used(code):
    held(900)  # a row held, of 2490, the Datamart no longer returns
    earlier()  # a read with no filter two days ago: no weekly read tonight
    fake = FakeDatamart(mixed(), ignore=("country_name",))
    fake.register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert fake.requests == [
        {**ASKED, "page_size": "1"},  # the count with no business area filter
        {**ASKED, "business_area": "2490", "page_size": "50"},  # its proof: one page
        {**ASKED, "business_area": "2490", "page_size": "500"},  # the read by it
    ]
    area = details["area_filter"]
    assert (area["key"], area["why"], area["unfiltered"], area["held"], area["strays"]) == (
        "business_area", "proven", 12, 1, 0
    )  # fmt: skip
    assert area["tried"] == [
        {
            "key": "business_area",
            "verdict": "proven",
            "count": 6,
            "page": 6,
            "named": 6,
            "other": 0,
            "none": 0,
        }
    ]
    assert "other_country_skipped" not in details  # no record of another country was asked
    assert (details["removed"], details["no_longer_sent_new"]) == (0, 1) and ids() == {*range(1, 7), 900}
    assert (
        sync.area_words(area)
        == "read by business area 2490 (filter business_area: 6 records with it, 12 without)"
    )


@responses.activate
def test_a_filter_the_datamart_ignores_falls_back_to_today_s_read(code):
    earlier()
    fake = FakeDatamart(mixed(), ignore=("country_name", *AREA_KEYS))
    fake.register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED
    area = details["area_filter"]
    assert (area["key"], area["why"]) == ("", "none_proven")
    assert [(t["key"], t["verdict"], t["count"]) for t in area["tried"]] == [
        ("business_area", "ignored", 12),
        ("business_area_code", "ignored", 12),
    ]
    assert area_keys(fake) == ["", "business_area", "business_area_code", ""]  # no area key in the read
    assert details["other_country_skipped"] == 6 and ids() == set(range(1, 7))
    assert sync.area_words(area) == (
        "read with no business area filter: business_area is ignored (as many records with it as without), "
        "business_area_code is ignored (as many records with it as without)"
    )


@responses.activate
def test_a_filter_on_another_field_or_refused_is_never_used(code):
    """business_area is compared with another field (its page names other business areas), business_area_code
    refused (HTTP 400): the read takes no filter."""
    earlier()
    rows = [lebanon(n, office_code="2490") for n in range(1, 7)]
    rows += [other(100 + n, office_code="2490" if n < 3 else "0990") for n in range(1, 7)]
    fake = FakeDatamart(
        rows,
        ignore=("country_name",),
        matches={"business_area": "office_code"},
        refuse=("business_area_code",),
    )
    fake.register(REPORTS)
    found = run()
    area = found.details["area_filter"]
    assert found.status == SyncRun.Status.SUCCEEDED and (area["key"], area["why"]) == ("", "none_proven")
    assert area["tried"] == [
        {
            "key": "business_area",
            "verdict": "other_codes",
            "count": 8,
            "page": 8,
            "named": 6,
            "other": 2,
            "none": 0,
        },
        {"key": "business_area_code", "verdict": "refused", "status": 400},
    ]
    assert area_keys(fake)[-1] == "" and ids() == set(range(1, 7))
    assert "business_area_code is refused (HTTP 400)" in sync.area_words(area)


@responses.activate
def test_no_code_or_rows_held_without_it_read_with_no_filter(settings, code):
    earlier()
    held(900, area=None)  # a row held naming no business area: a filter could never return it
    held(901)
    fake = FakeDatamart(mixed(), ignore=("country_name",))
    fake.register(REPORTS)
    area = run().details["area_filter"]
    assert (area["why"], area["held_not_named"], area["tried"]) == ("held_not_named", 1, [])
    assert area_keys(fake) == [""] and len(fake.requests) == 1  # no proof request
    assert sync.area_words(area) == (
        "read with no business area filter: 1 row held names no business area code or one other than 2490"
    )

    responses.reset()
    code.delete()  # no code set, and none found: nothing held names one
    dm.ReportedIndicator.objects.all().delete()
    fake = FakeDatamart(mixed(), ignore=("country_name",))
    fake.register(REPORTS)
    area = run().details["area_filter"]
    assert (area["why"], area["code"]) == ("no_code", "") and len(fake.requests) == 1
    assert sync.area_words(area) == "read with no business area filter: no business area code is set or found"


@responses.activate
def test_a_filter_finding_fewer_than_held_is_not_used(code):
    """NeuroDB holds 8 rows of 2490; the filter finds 6: a whole read by it would remove two that the Datamart may
    still hold under no code or another one."""
    earlier()
    for pk in range(900, 908):
        held(pk)
    FakeDatamart(mixed(), ignore=("country_name",)).register(REPORTS)
    area = run().details["area_filter"]
    assert [(t["key"], t["verdict"]) for t in area["tried"]] == [
        ("business_area", "fewer_than_held"),
        ("business_area_code", "nothing"),  # no record carries that key
    ]
    assert (area["key"], area["why"]) == ("", "none_proven")


@responses.activate
def test_once_a_week_the_read_takes_no_filter(code):
    """The latest read with no filter was 8 days ago (a filtered read since, and a failed one that never reached
    the end): tonight every record is read."""
    earlier(days=8)
    earlier(days=1, area_filter={"key": "business_area", "why": "proven", "strays": 0})
    earlier(days=1, status=SyncRun.Status.FAILED)
    fake = FakeDatamart(mixed(), ignore=("country_name",))
    fake.register(REPORTS)
    area = run().details["area_filter"]
    assert (area["key"], area["why"], area["tried"]) == ("", "weekly", [])
    assert len(fake.requests) == 1 and area_keys(fake) == [""]
    assert sync.area_words(area) == (
        "read with no business area filter: once a week every record is read, so none of Lebanon's that names "
        "no code is missed"
    )


A_WEEK_LESS_A_MINUTE = 7 - 35 / 86_400  # days: the run a week ago started 35 seconds later than tonight's


@responses.activate
def test_the_weekly_read_comes_on_the_seventh_night_whatever_minute_its_run_starts(code):
    """sync_all runs the datasets one after the other, so the partner reports' run starts minutes earlier or later
    from night to night: the read with no filter of a week ago, started 35 seconds later in its night, still makes
    tonight the weekly read (the six nights between read by the proven filter)."""
    earlier(days=A_WEEK_LESS_A_MINUTE)
    for days in range(1, 7):
        earlier(days=days, area_filter={"key": "business_area", "why": "proven", "strays": 0})
    FakeDatamart(mixed(), ignore=("country_name",)).register(REPORTS)
    area = run().details["area_filter"]
    assert (area["key"], area["why"]) == ("", "weekly")


@responses.activate
def test_a_key_that_let_a_stray_through_a_week_ago_is_tried_again_whatever_minute_its_run_starts(code):
    earlier()
    earlier(days=A_WEEK_LESS_A_MINUTE, area_filter={"key": "business_area", "why": "proven", "strays": 1})
    FakeDatamart(mixed(), ignore=("country_name",)).register(REPORTS)
    area = run().details["area_filter"]
    assert (area["tried"][0]["key"], area["tried"][0]["verdict"], area["key"]) == (
        "business_area", "proven", "business_area"
    )  # fmt: skip


@responses.activate
def test_a_stray_is_never_written_and_the_key_is_skipped_the_next_night(code, monkeypatch):
    """business_area is compared with another field: its proof page of 2 names 2490 only, and the read lets one
    record of another country through. It is never written, the read removes nothing, and the key is not tried
    again for a week, whatever the nights between; then it is tried again."""
    monkeypatch.setattr(sync, "AREA_PAGE", 2)
    earlier()
    held(900)
    rows = [lebanon(n, office_code="2490") for n in range(1, 7)]
    rows += [other(101, office_code="2490"), *(other(100 + n, office_code="0990") for n in range(2, 7))]
    FakeDatamart(rows, ignore=("country_name",), matches={"business_area": "office_code"}).register(REPORTS)
    first = run()
    details = first.details
    assert first.status == SyncRun.Status.PARTIAL
    area = details["area_filter"]
    assert (area["key"], area["strays"], details["area_strays"]) == ("business_area", 1, 1)
    assert (details["removal_skipped"], details["removed"], details["other_country_skipped"]) == (True, 0, 1)
    assert 101 not in ids() and 900 in ids()

    second = run()  # the next night: business_area is skipped, business_area_code finds nothing
    area = second.details["area_filter"]
    assert area["tried"][0] == {"key": "business_area", "verdict": "strays_this_week"}
    assert area["tried"][1]["key"] == "business_area_code" and area["why"] == "none_proven"
    assert second.status == SyncRun.Status.SUCCEEDED and second.details["no_longer_sent_new"] == 1
    assert 900 in ids()  # kept, marked no longer sent
    third = run()  # a clean night between: still skipped
    assert third.details["area_filter"]["tried"][0]["verdict"] == "strays_this_week"

    ago(first, 8)  # the strays are more than a week old: the key is tried again
    eighth = run()
    assert eighth.details["area_filter"]["tried"][0] == {
        "key": "business_area", "verdict": "proven", "count": 7, "page": 2, "named": 2, "other": 0, "none": 0
    }  # fmt: skip


@responses.activate
def test_an_unnamed_record_of_the_country_is_written_and_counted_under_a_filter(code, monkeypatch):
    monkeypatch.setattr(sync, "AREA_PAGE", 2)
    earlier()
    rows = [lebanon(n, office_code="2490") for n in range(1, 7)] + [other(100 + n) for n in range(1, 7)]
    rows.append(report(7, "2025-03-01", office_code="2490"))  # Lebanon's, naming no business area
    FakeDatamart(rows, ignore=("country_name",), matches={"business_area": "office_code"}).register(REPORTS)
    found = run()
    area = found.details["area_filter"]
    assert found.status == SyncRun.Status.SUCCEEDED
    assert (area["key"], area["unnamed"], area["strays"]) == ("business_area", 1, 0) and 7 in ids()
    area = run().details["area_filter"]  # the next night: a row held names no code, every record is read
    assert (area["why"], area["held_not_named"]) == ("held_not_named", 1)


def progress(n: int, area: str) -> dict:
    """A PRP progress report as sources/prp/unicefprogressreport sends it: no id, no source_id."""
    return {
        "business_area_code": area,
        "report_type": "QPR",
        "report_number": f"QPR{n}",
        "end_date": "2026-03-31",
    }


@responses.activate
def test_progress_reports_whose_filter_is_ignored_are_read_with_none(code):
    """The 21 Oct 2026 probe: sources/prp/unicefprogressreport ignores business_area_code, and here refuses
    business_area: no area key is sent with the read, and the records are kept by the code they name."""
    earlier("prp_progress_reports")
    records = [progress(n, "2490") for n in range(1, 5)] + [progress(n, "0990") for n in range(5, 11)]
    fake = FakeDatamart(records, ignore=("business_area_code",), refuse=("business_area",))
    fake.register(PROGRESS)
    found = run("prp_progress_reports")
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    area = details["area_filter"]
    assert (area["key"], area["why"], area["unfiltered"]) == ("", "none_proven", 10)
    assert [(t["key"], t["verdict"]) for t in area["tried"]] == [
        ("business_area_code", "ignored"),
        ("business_area", "refused"),
    ]
    assert fake.requests[-1] == {"page_size": "500"}  # the read: no area key
    assert (details["documents"], details["other_country_skipped"]) == (4, 6)
    assert sync.area_words(area) == (
        "read with no business area filter: business_area_code is ignored (as many records with it as "
        "without), business_area is refused (HTTP 400)"
    )


# ----------------------------------------------------------------------------------------------- the probe
@responses.activate
def test_the_probe_says_which_business_area_filter_applies(code):
    FakeDatamart(mixed(), ignore=("country_name",)).register(REPORTS)
    found = probe.run_job("partner_reports", client=client())
    assert found.status == SyncRun.Status.SUCCEEDED
    asked = {a["key"]: a for a in found.details["area_filters"]}
    assert (asked["business_area"]["verdict"], asked["business_area"]["business_areas"]) == (
        "proven",
        {"2490": 6},
    )
    assert asked["business_area_code"]["verdict"] == "nothing"
    assert (
        "The Datamart applies business_area to prp/datareport (6 records with it, 12 without; every record on its "
        "page names 2490): the sync reads by it." in found.details["verdict"]
    )
    assert "windows" not in found.details  # 12 records: the sync reads them in one answer


@responses.activate
def test_the_probe_says_when_no_business_area_filter_applies(code):
    records = [progress(n, "2490") for n in range(1, 5)] + [progress(n, "0990") for n in range(5, 11)]
    FakeDatamart(records, ignore=("business_area_code",), refuse=("business_area",)).register(PROGRESS)
    found = probe.run_job("prp_progress_reports", client=client())
    assert found.status == SyncRun.Status.SUCCEEDED  # a key refused is an answer, not the probe's failure
    assert [(a["key"], a["verdict"]) for a in found.details["area_filters"]] == [
        ("business_area_code", "ignored"),
        ("business_area", "refused"),
    ]
    assert (
        "The Datamart applies none of the business area filters tried for sources/prp/unicefprogressreport: "
        "business_area_code is ignored (as many records with it as without), business_area is refused (HTTP "
        "400). The sync reads every country's records and keeps 2490's." in found.details["verdict"]
    )
