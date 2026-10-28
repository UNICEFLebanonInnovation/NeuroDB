"""PRP's own reports still owed for a period that has ended (datamart.owed, SPEC_pbi_findings §3): shown on Home,
Progress reports and its Partners tab only while the sync's recorded verdict says their link to the programme
documents is proven (the gate); counted once against the partner reports; filtered as the page's rows are; and
every reader (tiles, Partners tab, follow-up list, Home, Partner monitoring's Overdue reports, the PD and partner
pages, the daily review, Ask, the exports and the figure trail) says the same.

The world, on the suite's day (15 October 2026): Partner A's LEB/PD1 (Education) and Partner B's SPD LEB/SPD2
(Health). The partner reports hold LEB/PD1's QPR1 and QPR2 (Accepted). PRP's own list holds, linked to them:
LEB/PD1 QPR3 (ended 30 Sep, due 10 Oct: Overdue), LEB/SPD2 HR9 (ended 30 Sep, due 20 Oct: Due), LEB/PD1 QPR2
(Due in PRP, but the partner reports have it: the rows decide) and LEB/PD1 QPR4 (period still running: not owed).
Fake names only."""

from __future__ import annotations

import csv
import datetime as dt
import io
import itertools
import json

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.assistant import tools
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import monitoring, owed, reporting, trail
from neurodb.datamart import reached as rc
from neurodb.datamart import services as datamart
from neurodb.integrations.etools import datamart_sync
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.web.parts import get_part, text
from tests.web.test_plain_words import NEVER

pytestmark = pytest.mark.django_db

D = dt.date
TODAY = D(2026, 10, 15)  # the suite's day (tests/conftest.py)
_ids = itertools.count(50_000)
PERSON, EMAIL = "Focal Alpha", "focal.alpha@example.invalid"


# ----------------------------------------------------------------------------------------------- the world
def _row(pd, number, start, end, word="Accepted", *, kind="QPR", ref=None, section="", **extra):
    """One partner report row (prp/datareport) of ``pd``'s report ``number``."""
    return dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner=pd.partner if pd else None,
        partner_name=pd.partner.name if pd else "",
        pd_reference_number=pd.number if ref is None else ref,
        progress_report=f"PR-{next(_ids)}",
        report_type=kind,
        report_number=number,
        report_status=word,
        period_start=start,
        period_end=end,
        due_date=end + dt.timedelta(days=15),
        submission_date=end + dt.timedelta(days=10) if word in ("Accepted", "Submitted") else None,
        indicator="Children reached",
        etools_indicator_id="10",
        location="Akkar",
        section=section,
        **extra,
    )


def _record(pd, number, start, end, word, *, kind="QPR", due=None, sub=None, key=None, linked=True, **extra):
    """One record of PRP's own progress reports (sources/prp/unicefprogressreport), as the sync keeps it: no id
    (held under a hash), its dates as text, its narrative and the people who wrote it beside the fields NeuroDB
    reads; linked to ``pd`` (the link step's work) unless ``linked`` is False."""
    data = {
        "business_area_code": "2490",
        "programme_document": 77,
        "report_type": kind,
        "report_number": number,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "due_date": due.isoformat() if due else (end + dt.timedelta(days=10)).isoformat() if end else None,
        "submission_date": sub.isoformat() if sub else None,
        "status": word,
        "narrative": "Fake narrative of the period",
        "submitted_by": PERSON,
        "reviewed_by_email": EMAIL,
        **extra,
    }
    return dm.DatamartDocument.objects.create(
        dataset="prp_progress_reports",
        record_key=key or f"h{next(_ids)}",
        intervention=pd if linked else None,
        partner=pd.partner if linked else None,
        date=end,
        data=data,
    )


def _verdict(
    shown=True, why="", *, day=TODAY - dt.timedelta(days=1), target="prp_progress_reports", **details
):
    """A finished run of one of PRP's two datasets on ``day`` evening, with the verdict it recorded."""
    started = timezone.make_aware(dt.datetime.combine(day, dt.time(20, 30)))
    link = {
        "passes": shown or why == "one_day",
        "shown": shown,
        "shown_why": why,
        "shown_since": "2026-10-13" if shown else "",
        "days": 3 if shown else 0,
        "proven": why != "not_proven",
        "agrees": {"compared": 12, "same": 12, "elsewhere": 0, "dated": 12, "same_day": 12},
        "linked": 4,
        "stale": 2,
        "twins": 1,
    }
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target=target,
        status=SyncRun.Status.SUCCEEDED,
        started_at=started,
        finished_at=started + dt.timedelta(minutes=5),
        details={"progress_reports_pd_link": link, "read": 10, **details},
    )


@pytest.fixture
def world(db):
    a = PartnerOrganization.objects.create(etl_id="1", name="Partner A", vendor_number="2500000001")
    b = PartnerOrganization.objects.create(
        etl_id="2", name="Partner B", vendor_number="2500000002", partner_type="Civil Society Organization",
        cso_type="National",
    )  # fmt: skip
    pd = PCA.objects.create(
        etl_id="11", partner=a, partner_name=a.name, number="LEB/PD1", title="PD", status="active",
        document_type="PD", section_names=["Education"], start=D(2026, 1, 1), end=D(2026, 12, 31),
    )  # fmt: skip
    spd = PCA.objects.create(
        etl_id="12", partner=b, partner_name=b.name, number="LEB/SPD2", title="SPD", status="active",
        document_type="SPD", section_names=["Health"], start=D(2026, 1, 1), end=D(2026, 12, 31),
    )  # fmt: skip
    _row(pd, "QPR1", D(2026, 1, 1), D(2026, 3, 31))
    _row(pd, "QPR2", D(2026, 4, 1), D(2026, 6, 30))
    records = {
        "qpr3": _record(pd, "QPR3", D(2026, 7, 1), D(2026, 9, 30), "Overdue", due=D(2026, 10, 10)),
        "hr9": _record(spd, "HR9", D(2026, 9, 1), D(2026, 9, 30), "Due", kind="HR", due=D(2026, 10, 20)),
        "qpr2": _record(pd, "QPR2", D(2026, 4, 1), D(2026, 6, 30), "Due", due=D(2026, 7, 15)),
        "qpr4": _record(pd, "QPR4", D(2026, 10, 1), D(2026, 12, 31), "Due", due=D(2027, 1, 15)),
    }
    return {"a": a, "b": b, "pd": pd, "spd": spd, "records": records}


def hub(params=None):
    return datamart.partner_reporting(params or {}, TODAY, options=False)


def owed_keys(data) -> set[tuple[str, str]]:
    return {(r["pd_reference_number"], r["report_number"]) for r in data["reports"] if r.get("source")}


def tile(data, key: str) -> tuple[int, int]:
    found = next(s for s in data["counts"]["statuses"] if s["key"] == key)
    return found["progress_reports"], found["indicator_reports"]


@pytest.fixture
def checker(client, roles):
    user = User.objects.create_user(username="checker", password="pass-123456-x")
    user.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    client.force_login(user)
    return client


# ------------------------------------------------------------------------------------------------- the gate
def test_gate_closed_shows_no_prp_figure_and_one_line(world):
    """A proven pair whose reports agree on fewer than 9 in 10 period ends: no figure from PRP's own list, and
    one line saying why reports still owed may be missing."""
    before = hub()
    _verdict(shown=False, why="periods")
    data = hub()
    assert data["counts"] == before["counts"]
    assert data["partners"] == before["partners"]
    assert reporting.pd_follow_up(data["reports"]) == reporting.pd_follow_up(before["reports"])
    assert not owed_keys(data) and tile(data, rc.OVERDUE) == (0, 0)
    assert data["owed"]["shown"] is False and data["owed"]["n"] == 0
    assert data["owed"]["line"] == owed.HIDDEN[""]
    assert owed.every_owed(TODAY) == [] and owed.overdue_count({world["pd"].pk}, "QPR", TODAY) == 0


def test_nothing_is_said_while_neurodb_holds_none_of_prp_s_progress_reports(world):
    dm.DatamartDocument.objects.filter(dataset="prp_progress_reports").delete()
    _verdict(shown=False, why="not_proven")
    assert hub()["owed"] == {"shown": False, "n": 0, "held": False, "why": "not_read", "line": ""}


@pytest.mark.parametrize(
    "why, said",
    [
        ("not_proven", "and cannot tie PRP's own list of progress reports to their programme documents yet."),
        ("few", "and cannot tie PRP's own list"),
        ("days", "and cannot tie PRP's own list"),
        ("few_days", "and cannot tie PRP's own list"),
        (
            "one_day",
            "PRP's own list was tied to its programme documents on 14 Oct 2026: NeuroDB shows it once a read on "
            "the next day agrees too.",
        ),
    ],
)
def test_the_closed_line_follows_why(world, why, said):
    _verdict(shown=False, why=why)
    line = owed.link_state().line
    assert line.startswith(owed.HIDDEN_START) and said in line


@pytest.mark.parametrize(
    "why, link, said",
    [
        ("not_checked", {"proven": True, "linked": 4}, "and has not yet checked whether PRP's own list"),
        (
            "check_failed",
            {"proven": True, "linked": 4, "proof_error": "x"},
            "and the last sync could not check",
        ),
        ("not_synced", {}, "and has not yet checked whether PRP's own list"),
    ],
)
def test_the_closed_line_says_when_the_tie_was_not_checked(world, why, link, said):
    """A run made before the verdict was recorded (not checked), one whose check failed, or one that found no
    progress report to check (the first sync of PRP's list still running): the line says the tie was not
    checked, as check 11 does, never that NeuroDB cannot tie the list."""
    started = timezone.make_aware(dt.datetime.combine(TODAY - dt.timedelta(days=1), dt.time(20, 30)))
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="prp_programme_documents", status=SyncRun.Status.SUCCEEDED,
        started_at=started, finished_at=started + dt.timedelta(minutes=5),
        details={"progress_reports_pd_link": link, "read": 3},
    )  # fmt: skip
    state = owed.link_state()
    assert (state.shown, state.why) == (False, why)
    assert state.line == owed.HIDDEN[why] and said in state.line
    assert "cannot tie" not in state.line
    assert hub()["owed"]["line"] == state.line


def test_an_old_read_closes_the_gate_and_says_its_day(world):
    """A verdict that showed them, but the last read of PRP's progress reports ended more than 7 days ago."""
    _verdict(day=TODAY - dt.timedelta(days=9))
    state = owed.link_state()
    assert (state.shown, state.why) == (False, "old")
    assert state.line.endswith("PRP's own list of progress reports was last read on 6 Oct 2026.")
    assert not owed_keys(hub())


def test_an_empty_last_read_closes_the_gate(world):
    _verdict(day=TODAY - dt.timedelta(days=2))
    _verdict(shown=False, why="", empty_response=True).details.pop("progress_reports_pd_link")
    SyncRun.objects.filter(details__empty_response=True).update(details={"empty_response": True})
    state = owed.link_state()
    assert (state.shown, state.why) == (False, "not_read")
    assert state.line == owed.HIDDEN["not_read"]
    assert not owed_keys(hub())


def test_a_newer_programme_documents_run_without_a_verdict_closes_the_gate(world):
    _verdict(day=TODAY - dt.timedelta(days=2))
    run = _verdict(target="prp_programme_documents")
    run.details = {"read": 3}
    run.save(update_fields=["details"])
    assert owed.link_state().shown is False
    assert owed.link_state().line.endswith("or of its programme documents failed or received nothing.")
    assert not owed_keys(hub())


def test_an_unproven_link_linked_by_today_s_lookup_is_not_shown(world):
    """The records are linked to their PDs (the sync links "by today's lookup" with no proven pair), but the
    verdict says the pair is not proven: the pages never gate on the link."""
    _verdict(shown=False, why="not_proven")
    assert dm.DatamartDocument.objects.filter(
        dataset="prp_progress_reports", intervention__isnull=False
    ).count()
    assert not owed_keys(hub()) and owed.every_owed(TODAY) == []


def test_a_failed_read_of_prp_s_progress_reports_closes_the_gate_and_says_so(world):
    """A shown verdict two days old, then a read of PRP's progress reports that failed (no verdict): the line
    says the last sync failed or received nothing, as check 11 does, never that it received nothing."""
    _verdict(day=TODAY - dt.timedelta(days=2))
    started = timezone.make_aware(dt.datetime.combine(TODAY - dt.timedelta(days=1), dt.time(20, 30)))
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="prp_progress_reports", status=SyncRun.Status.FAILED,
        started_at=started, finished_at=started + dt.timedelta(minutes=5),
        details={"read": 600, "error": "timeout"},
    )  # fmt: skip
    state = owed.link_state()
    assert (state.shown, state.why) == (False, "not_read")
    assert state.line == owed.HIDDEN["not_read"]
    assert state.line.endswith(
        "and the last sync of PRP's own list of progress reports or of its programme documents failed or "
        "received nothing."
    )
    assert "failed or received nothing" in datamart_sync.shown_words(datamart_sync.recorded_pd_link())
    assert not owed_keys(hub())


def test_only_administrators_and_staff_validators_get_the_check_11_link_and_it_opens(viewer, checker, world):
    """The link opens the eTools reporting checks, which need staff status: a validator without it reads the
    line only, as on Data health."""
    _verdict(shown=False, why="periods")
    checks = reverse("admin:datamart_reporting_checks")
    plain = Client()
    plain.force_login(viewer)
    staff = User.objects.create_user(username="staff-checker", password="pass-123456-x", is_staff=True)
    staff.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    staff_client = Client()
    staff_client.force_login(staff)
    for client, linked in ((plain, False), (checker, False), (staff_client, True)):
        for address in ("/partner-reporting/", "/partner-reporting/?tab=partners"):
            page = text(client.get(address))
            assert owed.HIDDEN_START in page, address
            assert (checks in page) is linked, address
        home = text(get_part(client, "/?section=", "reporting"))
        assert owed.HIDDEN_START in home
        assert (checks in home) is linked
    assert staff_client.get(checks).status_code == 200


# ------------------------------------------------------------------------------------------- the gate open
def test_gate_open_counts_overdue_and_due_reports_from_prp(client_viewer, world):
    """LEB/PD1's QPR3 (Overdue) and LEB/SPD2's HR9 (Due) join the reports: the tiles (progress reports), the
    Partners tab, the follow-up list and Home's rows count them; their indicator reports are none."""
    before = hub()
    _verdict()
    data = hub()
    assert owed_keys(data) == {("LEB/PD1", "QPR3"), ("LEB/SPD2", "HR9")}
    assert tile(before, rc.OVERDUE) == (0, 0) and tile(data, rc.OVERDUE) == (1, 0)
    assert tile(before, rc.DUE) == (0, 0) and tile(data, rc.DUE) == (1, 0)
    assert data["counts"]["progress_reports"] == before["counts"]["progress_reports"] + 2
    assert data["counts"]["indicator_reports"] == before["counts"]["indicator_reports"]
    by_status = {s["key"]: s["partners"] for s in data["partners"]["by_status"]}
    assert (by_status[rc.OVERDUE], by_status[rc.DUE]) == (1, 1)
    assert data["partners"]["partners"] == 2 and data["partners"]["not_reported"] == 1  # Partner B
    assert [(r["pd_reference_number"], r["status"]) for r in data["partners"]["follow_up"]] == [
        ("LEB/PD1", rc.OVERDUE),
        ("LEB/SPD2", rc.DUE),
    ]
    by_section = {row["section"]: row["total"] for row in data["by_section"]["rows"]}
    assert by_section["Health"] == 1  # counted under its PD's sections
    rows = {r["pd_reference_number"]: r for r in reporting.pd_follow_up(data["reports"])["rows"]}
    assert rows["LEB/PD1"]["counts"][rc.OVERDUE] == 1 and rows["LEB/PD1"]["oldest_due"] == D(2026, 10, 10)
    assert rows["LEB/SPD2"]["sections"] == "Health"
    assert data["owed"] == {"shown": True, "n": 2, "held": True, "why": "", "line": owed.SHOWN.format(n=2)}
    home = text(get_part(client_viewer, "/?section=", "reporting"))
    assert escape(owed.SHOWN.format(n=2)) in home
    assert "LEB/SPD2" in home
    tab = text(client_viewer.get("/partner-reporting/?tab=partners"))
    assert escape(owed.SHOWN.format(n=2)) in tab


def test_the_status_filter_keeps_the_line_s_count_to_the_reports_shown(world):
    _verdict()
    data = hub({"status": "overdue"})
    assert owed_keys(data) == {("LEB/PD1", "QPR3")}
    assert data["owed"]["line"] == owed.SHOWN_ONE


# ---------------------------------------------------------------------------------------- counted once
def test_a_report_the_partner_reports_hold_is_counted_once(world):
    """LEB/PD1's QPR2 is in both feeds: the rows decide (Accepted), PRP's copy (Due) is dropped."""
    _verdict()
    reports = [r for r in hub()["reports"] if r["report_number"] == "QPR2"]
    assert [(r["status"], r.get("source")) for r in reports] == [(rc.ACCEPTED, None)]


def test_a_report_the_partner_reports_hold_is_counted_once_also_under_a_section_filter(world):
    """The rows' QPR2 sits in Child Protection, its PD in Education: the Education filter leaves the rows' report
    out, and never lets PRP's copy in."""
    dm.ReportedIndicator.objects.filter(report_number="QPR2").update(section="Child Protection")
    _verdict()
    assert owed_keys(hub({"section": "Education"})) == {("LEB/PD1", "QPR3")}


def test_a_report_the_partner_reports_hold_is_counted_once_when_the_row_is_linked_to_another_amendment(world):
    """A row of QPR3 that names LEB/PD1-2 (another amendment) and is linked to no PD still holds the report."""
    _row(None, "QPR3", D(2026, 7, 1), D(2026, 9, 30), "Submitted", ref="LEB/PD1-2")
    _verdict()
    assert owed_keys(hub({"pd_status": ""})) == {("LEB/SPD2", "HR9")}


def test_a_report_the_partner_reports_hold_is_counted_once_when_the_row_has_no_type(world):
    _row(world["pd"], "QPR3", D(2026, 7, 1), D(2026, 9, 30), "Submitted", kind="")
    _verdict()
    assert owed_keys(hub()) == {("LEB/SPD2", "HR9")}


def test_a_bare_number_and_qpr3_are_one_report(world):
    """PRP writes the number bare ("3"), the rows "QPR3": one report."""
    record = world["records"]["qpr3"]
    record.data = {**record.data, "report_number": "3"}
    record.save()
    _verdict()
    assert ("LEB/PD1", "QPR3") in owed_keys(hub())
    _row(world["pd"], "QPR3", D(2026, 7, 1), D(2026, 9, 30), "Submitted")
    assert ("LEB/PD1", "QPR3") not in owed_keys(hub())


def test_an_older_version_is_not_counted(world):
    """An older version of a report is linked to none by the link step: never counted."""
    _record(world["spd"], "HR8", D(2026, 8, 1), D(2026, 8, 31), "Overdue", kind="HR", linked=False)
    _verdict()
    assert ("LEB/SPD2", "HR8") not in owed_keys(hub())


def test_two_versions_from_one_read_with_other_period_ends_count_once(world):
    """A shift of the Datamart's pages brings two versions of HR9 in one read: counted once, the later period
    end."""
    _record(world["spd"], "HR9", D(2026, 8, 1), D(2026, 8, 31), "Overdue", kind="HR", due=D(2026, 9, 15))
    _verdict()
    found = [r for r in hub()["reports"] if r["report_number"] == "HR9"]
    assert [(r["period_end"], r["status"]) for r in found] == [(D(2026, 9, 30), rc.DUE)]


def test_a_linked_overdue_record_no_read_returned_for_8_days_is_not_owed(world):
    """PRP may have deleted it: its last record would read Overdue for ever."""
    _verdict()
    dm.DatamartDocument.objects.filter(pk=world["records"]["qpr3"].pk).update(
        synced_at=timezone.now() - dt.timedelta(days=8, hours=12)
    )
    assert owed_keys(hub()) == {("LEB/SPD2", "HR9")}


def test_a_record_without_dates_or_sent_back_is_not_owed(world):
    _record(world["pd"], "QPR5", None, None, "Due")
    _record(world["pd"], "QPR6", D(2026, 4, 1), D(2026, 6, 30), "Sent back")
    _verdict()
    assert owed_keys(hub()) == {("LEB/PD1", "QPR3"), ("LEB/SPD2", "HR9")}


def test_not_yet_due_is_not_owed(world):
    """QPR4's period still runs: PRP's list has it, but it is not owed (the follow-up list's not yet due reports
    come from the partner reports only)."""
    _verdict()
    assert ("LEB/PD1", "QPR4") not in owed_keys(hub())


# ------------------------------------------------------------------------------------------------- filters
@pytest.mark.parametrize(
    "params, kept",
    [
        ({"report_type": "HR"}, {"HR9"}),
        ({"year": "2026"}, {"QPR3", "HR9"}),
        ({"year": "2025"}, set()),
        ({"q": "leb/spd"}, {"HR9"}),
        ({"q": "partner a"}, {"QPR3"}),
        ({"q": "2500000002"}, {"HR9"}),
        ({"q": "children reached"}, set()),  # no indicator
        ({"section": "health"}, {"HR9"}),  # its PD's sections, any case
        ({"report_number": "QPR3"}, {"QPR3"}),
        ({"report_number": "3"}, {"QPR3"}),  # a bare number of the type
        ({"pd": "LEB/PD1"}, {"QPR3"}),
        ({"partner": "{b}"}, {"HR9"}),
        ({"partner_type": "National"}, {"HR9"}),
        ({"emergency": "yes"}, {"HR9"}),  # the PD is an SPD (the Emergency indicator)
        ({"emergency": "no"}, {"QPR3"}),
        ({"start_from": "2026-08-01"}, {"HR9"}),
        ({"end_to": "2026-09-29"}, set()),
        ({"overdue": "1"}, {"QPR3"}),
        ({"pd_status": "closed"}, set()),
        ({"pd_status": ""}, {"QPR3", "HR9"}),
    ],
)
def test_filters_apply_to_owed_reports(world, params, kept):
    _verdict()
    params = {k: str(world["b"].pk) if v == "{b}" else v for k, v in params.items()}
    assert {number for _pd, number in owed_keys(hub(params))} == kept


def test_a_draft_or_cancelled_pd_s_reports_are_never_owed(world):
    PCA.objects.filter(pk=world["spd"].pk).update(status="cancelled")
    _verdict()
    assert owed_keys(hub({"pd_status": ""})) == {("LEB/PD1", "QPR3")}


def test_check_11_counts_the_reports_still_owed_the_pages_can_show(world):
    """LEB/SPD2 is cancelled: the pages never show its HR9, whatever the PD status filter, so neither do the
    sync's counts of reports still owed, which check 11 says NeuroDB shows."""
    from neurodb.datamart import checks

    PCA.objects.filter(pk=world["spd"].pk).update(status="cancelled")
    _verdict()
    proof = datamart_sync.pd_link_proof(True, TODAY)
    widest = owed.owed_reports(reporting.ReportFilters(pd_statuses=[]), TODAY)
    assert proof["owed"] == len(widest) == 1 and proof["stale"] == 0
    assert "NeuroDB shows the 1 report still owed." in checks._shown_sentence({**proof, "shown": True})
    PCA.objects.filter(pk=world["spd"].pk).update(status="active")
    assert datamart_sync.pd_link_proof(True, TODAY)["owed"] == 2


def test_a_programme_document_object_never_shows_a_person(world, roles):
    """The link step reads only an id from a progress report's programme_document: that key is counted on every
    read (keys_seen), never shown as a field NeuroDB reads, so an object holding a person NeuroDB does not know
    is withheld by the people rule in the CSV of reports still owed, Raw eTools data's CSV and its record page;
    the report is still owed and counted."""
    from neurodb.datamart import catalogue, raw

    stranger = "Zorba Quintel"
    record = world["records"]["qpr3"]
    record.data = {**record.data, "programme_document": {"id": 77, "title": f"Support for {stranger}",
                                                         "name": stranger}}  # fmt: skip
    record.save(update_fields=["data"])
    _verdict()
    assert ("LEB/PD1", "QPR3") in owed_keys(hub())
    assert "programme_document" not in raw.reviewed_keys("prp_progress_reports")
    assert "programme_document" in datamart_sync.Census(catalogue.DOCUMENTS["prp_progress_reports"]).watched
    staff = User.objects.create_user(username="staff-validator", password="pass-123456-x", is_staff=True)
    staff.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    client = Client()
    client.force_login(staff)
    owed_csv = client.get(reverse("reports:check_rows", args=["progress-reports"]), {"what": "owed"})
    raw_csv = client.get(reverse("admin:datamart_raw_csv", args=["prp_progress_reports"]))
    for response in (owed_csv, raw_csv):
        assert response.status_code == 200
        content = b"".join(response.streaming_content).decode("utf-8-sig")
        assert "QPR3" in content and stranger not in content and "Quintel" not in content
    page = client.get(reverse("admin:datamart_raw_record", args=["prp_progress_reports", record.pk]))
    assert page.status_code == 200 and "Quintel" not in page.content.decode()


def test_the_filter_steps_record_the_reports_each_filter_leaves_out(world):
    _verdict()
    steps: list = []
    found = owed.owed_reports(reporting.ReportFilters.from_params({"report_type": "QPR"}), TODAY, steps=steps)
    assert [r["report_number"] for r in found] == ["QPR3"]
    assert [(name, len(kept)) for name, kept in steps] == [
        ("start", 2), ("report_type", 1), ("not_a_partnership", 1), ("pd_status", 1),
    ]  # fmt: skip


# ------------------------------------------------------------------------------------------------- readers
def test_the_pop_up_is_not_linked_for_a_prp_only_report_and_the_exports_say_their_source(
    client_viewer, world
):
    _verdict()
    page = text(client_viewer.get("/partner-reporting/"))
    report = next(r for r in hub()["reports"] if r.get("source"))
    assert f"report={report['key']}" not in page and "from PRP&#x27;s own list" in page
    for tab in ("reports", "partners"):
        response = client_viewer.get("/partner-reporting/", {"tab": tab, "export": "csv"})
        rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        sources = {(r["programme_document"], r["report"]): r["source"] for r in rows}
        assert sources[("LEB/PD1", "QPR3")] == "PRP progress reports", tab
        assert sources[("LEB/SPD2", "HR9")] == "PRP progress reports", tab
        if tab == "reports":
            assert sources[("LEB/PD1", "QPR1")] == "Partner reports"


def test_the_pd_and_partner_pages_list_them(world):
    _verdict()
    pd_reports = datamart.programme_datamart(world["pd"], TODAY)["reports"]
    assert ("QPR3", rc.OVERDUE) in {(r["report_number"], r["status"]) for r in pd_reports}
    partner = datamart.partner_datamart(world["b"])
    assert partner["reporting"]["due"] == 1 and partner["reporting"]["reports"] == 1


def test_the_pd_page_counts_every_report_by_status_beyond_the_12_it_lists(client_viewer, world):
    """The browser check: an SPD's 12 latest reports were all Not yet due, so the hundreds of reports it still
    owes from PRP's own list were neither listed nor counted on its page, where the reports to follow up link.
    The page now counts every report of the PD by status, as the partner page does, says that the 12 latest are
    listed, and links to all of them on Progress reports. Gate closed, the owed report is counted nowhere."""
    pd = world["pd"]
    for n in range(1, 13):  # 12 later monthly reports, their periods still running: Not yet due
        _row(pd, f"HR{n}", D(2026, 10, 1), D(2026, 11, n), "", kind="HR")
    url = reverse("reports:programme_detail", args=[pd.pk])

    def said() -> str:
        html = client_viewer.get(url).content.decode()
        return html.split("data-pd-reporting>", 1)[1].split("</p>", 1)[0]

    closed = said()
    assert closed.startswith(
        "14 reports · 2 accepted · 0 awaiting acceptance · 0 sent back · 0 due · 0 overdue"
    )
    _verdict()
    shown = said()
    assert shown.startswith(
        "15 reports · 2 accepted · 0 awaiting acceptance · 0 sent back · 0 due · 1 overdue"
    )
    assert "the 12 latest below" in shown
    assert f'href="{reverse("reports:partner_reporting")}?pd=LEB/PD1&amp;pd_status="' in shown
    listed = datamart.programme_datamart(pd, TODAY)
    assert len(listed["reports"]) == 12 and "QPR3" not in {r["report_number"] for r in listed["reports"]}
    assert listed["reporting"]["overdue"] == 1 and listed["reporting"]["not_yet_due"] == 12


def test_partner_monitoring_overdue_counts_owed_overdue_reports_once(world):
    """Partner monitoring's Overdue reports: the rows' (none) and PRP's QPR3, once; HR9 is Due."""
    filters = monitoring.Filters(pd_ids=[world["pd"].pk, world["spd"].pk], scope="all")
    assert monitoring.summary([], filters, TODAY)["overdue_reports"] == 0  # no indicator rows: no PD
    _verdict()
    assert owed.overdue_count({world["pd"].pk, world["spd"].pk}, "QPR", TODAY) == 1
    assert owed.overdue_count({world["pd"].pk, world["spd"].pk}, "HR", TODAY) == 0
    row = _row(world["pd"], "QPR3", D(2026, 7, 1), D(2026, 9, 30), "")  # the rows have it: overdue there
    dm.ReportedIndicator.objects.filter(pk=row.pk).update(due_date=D(2026, 10, 10))
    assert owed.overdue_count({world["pd"].pk}, "QPR", TODAY) == 0
    rows = monitoring.overdue_rows({world["pd"].pk}, "QPR", TODAY)
    assert reporting.report_count(rows) == 1


def test_a_report_every_row_of_which_is_no_longer_sent_keeps_its_rows_word_gate_open_or_closed(world):
    """QPR3's rows were last sent Submitted, then no longer sent; PRP's own list says Overdue. The rows decide:
    Submitted on the hub, never overdue on Partner monitoring, gate open or closed."""
    row = _row(world["pd"], "QPR3", D(2026, 7, 1), D(2026, 9, 30), "Submitted")
    dm.ReportedIndicator.objects.filter(pk=row.pk).update(not_sent_since=timezone.now())
    for shown in (False, True):
        SyncRun.objects.all().delete()
        _verdict(shown=shown, why="" if shown else "periods")
        found = [r for r in hub()["reports"] if r["report_number"] == "QPR3"]
        assert [(r["status"], r.get("source")) for r in found] == [(rc.SUBMITTED, None)], shown
        assert owed.overdue_count({world["pd"].pk}, "QPR", TODAY) == 0


def test_the_daily_review_adds_owed_overdue_quarterly_reports(world):
    from neurodb.review import checks as review

    _verdict()
    drafts = review.check_reports_overdue(review.Context(today=TODAY, rows=[]))
    assert [d.key for d in drafts] == ["reports_overdue:LEB/PD1"]  # HR9 is Due, and of an HR
    found = drafts[0]
    assert found.title.startswith("1 progress report overdue")
    assert "1 of them is from PRP's progress reports." in found.detail
    assert found.evidence["records"] == ["QPR3: due 2026-10-10 (from PRP's progress reports)"]


def test_ask_says_whether_they_are_shown(world):
    _verdict(shown=False, why="periods")
    closed = tools.partner_reporting()
    assert closed["owed_shown"] is False and closed["owed_note"] == owed.HIDDEN[""]
    SyncRun.objects.all().delete()
    _verdict()
    found = tools.partner_reporting()
    assert found["owed_shown"] is True and found["owed_note"] == owed.SHOWN.format(n=2)
    sources = {r["report"]: r["source"] for r in found["progress_reports"]}
    assert sources["QPR3"] == "PRP progress reports" and sources["QPR1"] == "Partner reports"


def test_no_person_in_any_owed_dict_or_csv(checker, world):
    """PRP's records hold the people who wrote and reviewed them: none reaches a dict of the hub, Ask's tool, the
    trail or its CSV of the reports still owed."""
    _verdict()
    said = json.dumps([hub()["reports"], tools.partner_reporting(), owed.every_owed(TODAY)], default=str)
    assert PERSON not in said and EMAIL not in said and "narrative" not in said
    response = checker.get(reverse("reports:check_rows", args=["progress-reports"]), {"what": "owed"})
    assert response.status_code == 200
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    assert PERSON not in content and EMAIL not in content
    rows = list(csv.DictReader(io.StringIO(content)))
    assert {(r["kept_by_the_filter"], r["report_status_neurodb"]) for r in rows} == {
        ("yes", "Overdue"), ("yes", "Due"),
    }  # fmt: skip


# ----------------------------------------------------------------------------------------------- the trail
@pytest.mark.parametrize("params", [{}, {"report_type": "QPR"}, {"status": "overdue"}, {"emergency": "yes"}])
def test_owed_figure_trail_equals_the_page(checker, world, params):
    """With the gate open, each figure of the trail is the page's, rebuilt from the rows and PRP's reports still
    owed; the trail says where they come from, and what each filter leaves out of them."""
    _verdict()
    for page in ("progress-reports", "home"):
        found = trail.progress_trail(page, params, TODAY)
        assert not found.differences, [(f.key, f.page, f.rebuilt) for f in found.differences]
        step = next(s for s in found.steps if s.key == "P")
        assert not [line.words for line in step.lines if NEVER.findall(line.words)]
        assert "Shown since 13 Oct 2026" in step.lines[0].words
        assert step.lines[1].words.startswith("Still owed") and step.lines[1].what == "owed"
    found = trail.progress_trail("progress-reports", {"report_type": "QPR"}, TODAY)
    out = next(s for s in found.steps if s.key == "report_type")
    assert "Of PRP's own reports still owed: 1 progress report left out." in [x.words for x in out.lines]
    assert found.figure("pr.status.overdue").page == 1


def _csv(client, page: str, what: str, params: dict | None = None) -> list[dict[str, str]]:
    response = client.get(reverse("reports:check_rows", args=[page]), {**(params or {}), "what": what})
    assert response.status_code == 200, (what, response.content[:300])
    return list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))


@pytest.mark.parametrize("page", ["progress-reports", "home"])
def test_gate_open_each_figure_s_csv_gives_the_figure_with_prp_s_reports_still_owed(checker, world, page):
    """A figure's rows as CSV list the reports still owed it counts beside the partner report rows (their record
    through the people rule, "PRP progress reports" in their source column), so its counts_once adds up to the
    figure the page shows."""
    _verdict()
    params = {"block": "reporting"} if page == "home" else {}
    found = trail.progress_trail(page, params, TODAY)
    with_owed = set()
    for f in found.figures:
        if not f.what or f.page is None or dict(f.select).get("set", "kept") not in trail.DOCUMENT_SETS:
            continue
        rows = _csv(checker, page, f.what, params)
        assert sum(int(r["counts_once"]) for r in rows) == f.page, f.key
        assert len({r["unit_key"] for r in rows if r["counts_once"] == "1"}) == f.page, f.key
        owed_rows = (
            [r for r in rows if r["source"] == owed.SOURCE_WORDS] if rows and "source" in rows[0] else []
        )
        if owed_rows:
            with_owed.add(f.key)
            assert {r["report_status_neurodb"] for r in owed_rows} <= {"Overdue", "Due"}, f.key
            assert all(
                r["indicator_report_unit"] == "" and r["counts_once_indicator_report"] == "0"
                for r in owed_rows
            )
        said = json.dumps(rows)
        assert PERSON not in said and EMAIL not in said, f.key
    assert {"pr.progress_reports", "pr.status.overdue", "pr.status.due"} <= with_owed
    assert found.figure("pr.status.overdue").page == 1 and found.figure("pr.status.due").page == 1


def test_gate_open_the_kept_and_status_csvs_list_prp_s_reports_still_owed(checker, world):
    """K's CSV (the rows kept) lists the reports still owed the filter keeps, so a sum of counts_once_* over the
    statuses chosen gives the tiles again; S's CSV those of a status not chosen."""
    _verdict()
    found = trail.progress_trail("progress-reports", {}, TODAY)
    kept = _csv(checker, "progress-reports", "kept")
    chosen = [r for r in kept if r["report_status_chosen"] == "yes"]
    assert (
        sum(int(r["counts_once_progress_report"]) for r in chosen) == found.figure("pr.progress_reports").page
    )
    assert sum(int(r["counts_once_partner"]) for r in chosen) == found.figure("pr.partners").page
    assert {(r["progress_report_key"].split("|")[2], r["source"]) for r in kept if r["source"] != owed.ROWS_WORDS} == {
        ("QPR3", owed.SOURCE_WORDS), ("HR9", owed.SOURCE_WORDS),
    }  # fmt: skip
    k = next(s for s in found.steps if s.key == "K")
    assert "2 progress reports still owed from PRP's own list" in " ".join(x.words for x in k.lines)
    found = trail.progress_trail("progress-reports", {"status": "overdue"}, TODAY)
    s = next(s for s in found.steps if s.key == "S")
    assert s.what == "step:S"
    left = _csv(checker, "progress-reports", "step:S", {"status": "overdue"})
    assert [r["progress_report_key"].split("|")[2] for r in left if r["source"] == owed.SOURCE_WORDS] == [
        "HR9"
    ]


def test_the_trail_says_the_closed_line_and_counts_none(world):
    _verdict(shown=False, why="periods")
    found = trail.progress_trail("progress-reports", {}, TODAY)
    step = next(s for s in found.steps if s.key == "P")
    assert step.lines[0].words.startswith("Not counted: " + owed.HIDDEN_START)
    assert not NEVER.findall(step.lines[0].words)
    assert not found.rows.documents and found.figure("pr.status.overdue").page == 0


def test_a_section_spelt_with_a_stray_space_is_one_row_of_the_table_by_section(world):
    """eTools writes a PD's section "PSEA " (a stray space): PRP's report still owed takes its PD's sections, the
    partner reports' rows "Child Protection, PSEA". The table by section counts them under one Section, as
    Power BI's Reporting Progress by Sector does, and the figure trail says the same."""
    PCA.objects.filter(pk=world["pd"].pk).update(section_names=["Child Protection", "PSEA "])
    dm.ReportedIndicator.objects.filter(intervention=world["pd"]).update(section="Child Protection, PSEA")
    _verdict()
    rows = {row["section"]: row for row in hub({"pd_status": ""})["by_section"]["rows"]}
    assert set(rows) == {"Child Protection, PSEA", "Health"}
    counts = dict(zip(reporting.STATUS_KEYS, rows["Child Protection, PSEA"]["counts"], strict=True))
    assert (counts["accepted"], counts["overdue"]) == (2, 1)
    assert reporting.report_section({"section": "Child Protection, PSEA , Social & Behavioral Change "}) == (
        "Child Protection, PSEA, Social & Behavioral Change"
    )
