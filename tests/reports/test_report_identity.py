"""A progress report is its programme document, type, number and period (findings ID-1, ID-6), and a title two
PD indicators share is told apart by the PD output (ID-7): on Partner monitoring, Progress reports and its
Report/Location tab, the pop-up, the home page, the brief, the daily review and the eTools reporting checks,
whatever PRP's own report ids hold (the Lebanon rows carry none)."""

from __future__ import annotations

import datetime
import json
import time
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.urls import reverse

from neurodb.datamart import checks, monitoring, reached, reporting, services
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.review import checks as review

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = D(2026, 10, 15)  # the suite's day (tests/conftest.py SITE_NOW)
MARCH, JUNE = D(2026, 3, 31), D(2026, 6, 30)
CHILDREN = "# of children reached"
_ids = iter(range(81_000, 10**9))
HX = {"HX-Request": "true"}


def make_pd(n: int, status: str = "active") -> PCA:
    partner = PartnerOrganization.objects.create(etl_id=f"8{n}", name=f"Partner {n}", vendor_number=f"V8{n}")
    return PCA.objects.create(
        etl_id=f"88{n}",
        partner=partner,
        partner_name=partner.name,
        number=f"LEB/ID/PD{n}",
        title=f"Programme {n}",
        status=status,
        start=D(2026, 1, 1),
        end=D(2026, 12, 31),
    )


def indicator(pd: PCA, source_id: int, title: str = CHILDREN, output: str = "1.1 Protection") -> None:
    dm.PDIndicator.objects.create(
        datamart_id=next(_ids),
        source_id=source_id,
        intervention=pd,
        pd_reference_number=pd.number,
        title=title,
        lower_result_name=output,
        section_name="Child Protection",
        target_numerator=Decimal("1000"),
        display_type="number",
        location_name="Akkar",
    )


def row(
    pd, number, end, value, *, status="Accepted", location="Akkar", title=CHILDREN, changed=None, **extra
):
    """One location of one indicator in one progress report, as the Lebanon rows come: no PRP progress
    report id, no indicator report id, no eTools indicator id (unless given)."""
    kind = "HR" if number.startswith("HR") else "QPR"
    kind = extra.pop("report_type", kind)
    extra.setdefault("pd_output", "1.1 Protection")
    return dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner=pd.partner if pd else None,
        partner_name=pd.partner_name if pd else "",
        pd_reference_number=extra.pop("reference", pd.number if pd else ""),
        report_number=number,
        report_type=kind,
        report_status=status,
        period_start=extra.pop(
            "period_start", end - datetime.timedelta(days=89 if kind.strip() == "QPR" else 29)
        ),
        period_end=extra.pop("period_end", end),
        due_date=extra.pop("due_date", end + datetime.timedelta(days=15)),
        submission_date=end + datetime.timedelta(days=10) if status in ("Accepted", "Submitted") else None,
        last_modify_date=changed,
        indicator=title,
        location=location,
        disaggregation={"()": {"v": value, "d": 1, "c": value}},
        calculation_across_locations="sum",
        calculation_across_periods="sum",
        **extra,
    )


def at(day: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{day}T12:00:00+00:00")


def grid(year: int | None = 2026, **kwargs) -> dict[tuple[str, str], monitoring.Indicator]:
    rows = monitoring.indicators(monitoring.Filters(year=year, scope="all", **kwargs), today=TODAY)
    return {(r.pd.number, r.key): r for r in rows}


def one(key: str) -> dict:
    check = next(c for c in checks.CHECKS if c.key == key)
    with transaction.atomic(), connection.cursor() as cursor:
        return check.run(checks._Timed(cursor, time.monotonic() + 60))


# ---------------------------------------------------------------------- ID-6: periods never merged
@pytest.mark.parametrize(("progress_report", "indicator_report"), [("LEB/PD1", ""), ("", "Children reached")])
def test_two_periods_with_one_prp_id_are_two_reports(progress_report, indicator_report):
    """PRP's id repeated on QPR1 (100) and QPR2 (50) at one place, a PD-level progress report id or an
    indicator report id that does not vary: two counted reports, Reached 150 (it was 100, the largest)."""
    pd = make_pd(1)
    indicator(pd, 501)
    for number, end, value in (("QPR1", MARCH, 100), ("QPR2", JUNE, 50)):
        row(pd, number, end, value, progress_report=progress_report, indicator_report=indicator_report)
    entry = grid()[(pd.number, "501")]
    assert (entry.reached, entry.reports, entry.months) == (150.0, 2, {3: 100.0, 6: 50.0})
    assert not entry.result.notes  # never "several rows for one location: the largest is used"


def test_one_report_whose_rows_disagree_has_the_newest_word_everywhere(client_viewer):
    """One report (PD, QPR2, its period) whose rows say Submitted, and, changed later, Accepted: one report,
    Accepted, on Partner monitoring, Progress reports and its Report/Location tab."""
    pd = make_pd(1)
    indicator(pd, 501)
    indicator(pd, 502, "# of caregivers reached", "1.2 Caregivers")
    row(pd, "QPR2", JUNE, 30, status="Submitted", changed=at("2026-07-05"), title="# of caregivers reached",
        pd_output="1.2 Caregivers")  # fmt: skip
    row(pd, "QPR2", JUNE, 80, status="Accepted", changed=at("2026-07-12"))
    found = grid()
    assert {k: (r.reached, r.awaiting_reports) for k, r in found.items()} == {
        (pd.number, "501"): (80.0, 0),
        (pd.number, "502"): (30.0, 0),
    }
    data = services.partner_reporting({}, TODAY)
    (report,) = data["reports"]
    assert (report["status"], report["indicators"], data["summary"]["reports"]) == ("accepted", 2, 1)
    lines = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert sorted(r["status"] for r in lines) == ["accepted", "accepted"]
    # a section filter keeps one indicator: the report keeps the word of all its rows
    dm.ReportedIndicator.objects.filter(indicator="# of caregivers reached").update(section="Education")
    (report,) = services.partner_reporting({"section": "Education"}, TODAY)["reports"]
    assert report["status"] == "accepted"


def test_two_pds_reports_of_one_number_and_period_never_share_a_status():
    """PD A's QPR1 (Accepted, changed first) and PD B's QPR1 of the same dates (Due, changed later), with
    no PRP id: PD A's Reached counts its report (PD B's word once stopped it counting)."""
    a, b = make_pd(1), make_pd(2)
    indicator(a, 501)
    indicator(b, 601)
    row(a, "QPR1", MARCH, 200, changed=at("2026-04-12"))
    row(b, "QPR1", MARCH, 60, status="Due", changed=at("2026-09-01"))
    found = grid()
    assert found[(a.number, "501")].reached == 200.0
    assert found[(b.number, "601")].reached is None
    statuses = [r.status for r in found[(b.number, "601")].result.reports]
    assert statuses == ["overdue"]


def test_report_location_gives_each_pds_report_its_own_line():
    """Two programme documents' rows of one title, number, dates and place (and a PD NeuroDB does not
    hold): one line each, each with its own value, never the largest of them under one PD."""
    a, b = make_pd(1), make_pd(2)
    row(a, "QPR1", MARCH, 200)
    row(b, "QPR1", MARCH, 60)
    row(None, "QPR1", MARCH, 500, reference="LEB/ID/GONE")
    data = services.partner_reporting({"pd_status": ""}, TODAY)
    lines = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert sorted((r["programme_document"], r["value_in_period"], r["rows"]) for r in lines) == [
        ("LEB/ID/GONE", 500.0, 1),
        ("LEB/ID/PD1", 200.0, 1),
        ("LEB/ID/PD2", 60.0, 1),
    ]


# -------------------------------------------------------------------- ID-1: counting reports
def test_reports_without_prp_ids_are_counted_one_by_one_everywhere(reporting_year):
    """Two overdue reports of one PD with no PRP id: the summary's overdue reports, the home page's
    progress reports, the brief's missing reports and the daily review count two (they counted one, or
    none)."""
    from neurodb.reports import brief
    from neurodb.web import services as web_services

    pd = make_pd(1)
    indicator(pd, 501)
    for number, end in (("QPR1", MARCH), ("QPR2", JUNE)):
        row(pd, number, end, 0, status="Due", location="Akkar")
        row(pd, number, end, 0, status="Due", location="Bekaa")  # two rows of each report
    filters = monitoring.Filters(year=2026, scope="all")
    assert (
        monitoring.summary(monitoring.indicators(filters, today=TODAY), filters, today=TODAY)[
            "overdue_reports"
        ]
        == 2
    )
    assert web_services._etools_counts(2026)["progress_reports"] == 2
    built = brief.build(brief.Scope(year=2026, reporting_year=reporting_year, today=TODAY), cache=False)
    assert built["confidence"]["timeliness"]["counts"]["missing"] == 2  # QPR1 and QPR2, each its quarter
    (finding,) = review.check_reports_overdue(review.Context(today=TODAY))
    assert finding.evidence["numbers"]["reports"] == 2
    assert finding.title.startswith("2 progress reports overdue")


def test_the_pop_up_opens_a_report_by_its_key_or_by_a_prp_id_of_one_report(client_viewer):
    """A report with no PRP id opens from the lists by its key; PRP's id opens it only when all its rows are
    of one report (a PD-level id stands for several: none opens)."""
    pd = make_pd(1)
    indicator(pd, 501)
    row(pd, "QPR1", MARCH, 200)
    row(pd, "QPR1", MARCH, 100, location="Bekaa")
    row(pd, "QPR2", JUNE, 50)
    reports = {r["report_number"]: r for r in services.partner_reporting({}, TODAY)["reports"]}
    key = reports["QPR1"]["key"]
    assert key == f"pd{pd.pk}|QPR|QPR1|2026-01-01|2026-03-31"
    detail = services.report_detail(key, today=TODAY)
    (children,) = detail["indicators"]
    assert (children["total"], detail["status"]) == (300.0, "accepted")
    page = client_viewer.get(reverse("reports:progress_report"), {"report": key}, headers=HX)
    assert page.status_code == 200 and "300" in page.text
    table = client_viewer.get(reverse("reports:partner_reporting"), headers=HX).text
    assert "report=pd" in table  # the report table links each report by its key
    # PRP's id on one report opens it; the same id on both reports opens none
    dm.ReportedIndicator.objects.filter(report_number="QPR1").update(progress_report="PR-1")
    assert services.report_detail("PR-1", today=TODAY)["indicators"][0]["total"] == 300.0
    dm.ReportedIndicator.objects.update(progress_report="PR-PD")
    assert services.report_detail("PR-PD", today=TODAY) is None
    assert services.report_detail("pd0|QPR|QPR1||", today=TODAY) is None


def test_the_database_key_is_the_report_key():
    """reporting.REPORT_KEY (counts in the database) gives each row the text reached.report_key gives it."""
    a = make_pd(1)
    row(a, "QPR1", MARCH, 1, report_type=" QPR ")
    row(None, "QPR1", MARCH, 1, reference="LEB/ID/GONE")
    row(None, "QPR1", MARCH, 1, reference="", progress_report="X-1")  # names no PD: PRP's id tells it apart
    row(a, "", MARCH, 1, progress_report="P-9", period_start=None, period_end=None, due_date=None)
    found = dm.ReportedIndicator.objects.annotate(_key=reporting.REPORT_KEY).values(
        "_key", *reached.IDENTITY_FIELDS, "progress_report"
    )
    keys = [(r.pop("_key"), reached.report_key(r)) for r in found]
    assert all(sql == python for sql, python in keys), keys
    assert {python for _, python in keys} >= {
        f"pd{a.pk}|QPR|QPR1|2026-01-01|2026-03-31",
        "ref:LEB/ID/GONE|QPR|QPR1|2026-01-01|2026-03-31",
        "ref:|QPR|QPR1|2026-01-01|2026-03-31|pr:X-1",
        f"pd{a.pk}|QPR||||pr:P-9",
    }


# --------------------------------------------------------------- ID-7: a title two indicators share
def test_a_title_two_indicators_share_is_told_apart_by_the_pd_output(client_viewer):
    """A PD with two indicators titled '# of children reached' under outputs A and B, rows carrying their
    PD output: each indicator gets its own Reached (both were Not reported), and the Report/Location tab
    gives each its own line; two that share the output too stay left out, and are counted."""
    pd = make_pd(1)
    indicator(pd, 501, output="A. Protection")
    indicator(pd, 502, output="B. Education")
    indicator(pd, 503, "# of households supported", "C. Shelter")
    indicator(pd, 504, "# of households supported", "C. Shelter")
    row(pd, "QPR1", MARCH, 40, pd_output="A. Protection")
    row(pd, "QPR1", MARCH, 7, pd_output="b. education ")
    row(pd, "QPR1", MARCH, 5, title="# of households supported", pd_output="C. Shelter")
    found = grid()
    assert {k[1]: r.reached for k, r in found.items()} == {"501": 40.0, "502": 7.0, "503": None, "504": None}
    data = services.partner_reporting({}, TODAY)
    lines = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert sorted(r["value_in_period"] for r in lines if r["indicator"] == CHILDREN) == [7.0, 40.0]
    assert data["counts"]["indicator_reports"] == 3  # the two children indicators apart, households as one
    quality = monitoring.data_quality(list(found.values()), monitoring.Filters(year=2026, scope="all"), TODAY)
    assert (quality["same_title_count"], quality["same_title_left_out"]) == (2, 2)
    numbers = dict(one("7")["figures"])
    assert numbers["… matched by PD output and title (a title several indicators of the PD share)"] == 2
    assert numbers["… two or more indicators of the PD have the title and the output"] == 1


def _two_indicator_reports(progress_report: str) -> list[dict]:
    """One PD indicator's title on two PRP indicator reports of one progress report: 501 = Akkar 40, Bekaa
    5 (output A); 502 = Akkar 7, Bekaa 30 (output B)."""
    rows = []
    for ir, output, values in (
        ("501", "A", {"Akkar": 40, "Bekaa": 5}),
        ("502", "B", {"Akkar": 7, "Bekaa": 30}),
    ):
        for place, value in values.items():
            out = dict.fromkeys(reached.ROW_FIELDS, "")
            out.update(
                datamart_id=next(_ids),
                intervention_id=1,
                indicator=CHILDREN,
                pd_output=output,
                progress_report=progress_report,
                indicator_report=ir,
                report_number="QPR1",
                report_type="QPR",
                report_status="Accepted",
                period_start=D(2026, 1, 1),
                period_end=MARCH,
                due_date=D(2026, 4, 15),
                submission_date=D(2026, 4, 10),
                disaggregation={"()": {"v": value, "d": 1, "c": value}},
                location=place,
                location_ref_id=None,
            )
            rows.append(out)
    return rows


@pytest.mark.parametrize("progress_report", ["PR-1", ""])
def test_two_indicator_reports_of_one_title_are_never_pooled(progress_report):
    """Pooled, they gave 70 (the largest at each place) or 82 (both added): never. The one under the PD
    indicator's own output is used; with no output to tell them apart, the report is left out, with a note."""
    rows = _two_indicator_reports(progress_report)
    mine = reached.compute(rows, today=TODAY, output="b")
    assert mine.reached == 37.0 and "the one under its own output is used" in mine.notes[-1]
    neither = reached.compute(rows, today=TODAY, output="Another output")
    assert neither.reached is None and "left out of Reached" in neither.notes[-1]
    # one indicator report given per location (a PRP id per row, no place twice): read as before
    for n, r in enumerate(rows):
        r["indicator_report"], r["pd_output"] = f"9{n}", ""
    assert reached.compute(rows[:2], today=TODAY).reached == 45.0


def _row(ir: str, output: str, place: str, value: float) -> dict:
    """One location of one PRP indicator report in PD 7's Accepted QPR1 (1 Jan-31 Mar 2026)."""
    out = dict.fromkeys(reached.ROW_FIELDS, "")
    out.update(
        datamart_id=next(_ids),
        intervention_id=7,
        indicator="Children reached",
        pd_output=output,
        indicator_report=ir,
        report_number="QPR1",
        report_type="QPR",
        report_status="Accepted",
        period_start=D(2026, 1, 1),
        period_end=MARCH,
        due_date=D(2026, 4, 15),
        submission_date=D(2026, 4, 10),
        disaggregation={"()": {"v": value, "d": 1, "c": value}},
        p_code=place,
        location_ref_id=None,
    )
    return out


@pytest.mark.parametrize("ids", [("501", "502"), ("", "")])
def test_two_indicator_reports_of_one_title_at_different_places_are_never_added_up(ids):
    """501 (output A) gives LB11 = 40 and 502 (output B, a second PRP indicator with the title) LB22 = 30: no
    location is given twice, yet they are two indicator reports. Reached is the one under the PD indicator's
    own output (40), never 70; with no PRP id the outputs alone tell them apart."""
    rows = [_row(ids[0], "Output A", "LB11", 40), _row(ids[1], "Output B", "LB22", 30)]
    found = reached.compute(rows, output="Output A", today=D(2026, 10, 20))
    assert found.reached == 40.0 and "the one under its own output is used" in found.notes[-1]
    neither = reached.compute(rows, output="Output C", today=D(2026, 10, 20))
    assert neither.reached is None and "left out of Reached" in neither.notes[-1]


def test_rows_of_one_indicator_report_at_one_place_still_give_the_largest():
    rows = [_row("501", "Output A", "LB11", 40), _row("501", "Output A", "LB11", 12)]
    found = reached.compute(rows, output="Output A", today=D(2026, 10, 20))
    assert found.reached == 40.0 and any("the largest is used, never their sum" in n for n in found.notes)
    assert not any("the one under its own output" in n for n in found.notes)


# ------------------------------------------------------------------- ID-4: the id the rows carry
def test_check_7_says_the_key_read_is_empty_and_never_confirmed():
    pd = make_pd(1)
    indicator(pd, 501)
    row(pd, "QPR1", MARCH, 1, data={"etools_cp_output_indicators_id": None})
    found = one("7")
    assert "etools_cp_output_indicators_id" in found["verdict"] and "never confirmed" in found["verdict"]
    assert "matched by title, and by PD output" in found["verdict"]
    text = json.dumps(found)
    assert CHILDREN not in text and pd.number not in text  # counts only
