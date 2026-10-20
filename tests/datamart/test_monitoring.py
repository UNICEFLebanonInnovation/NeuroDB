"""Partner monitoring's plumbing (neurodb.datamart.monitoring): :func:`collect` then :func:`_attach_reached` is
what :func:`indicators` gives, and each indicator (and its stored :class:`Row`) carries Reached in the year with
the numerator and denominator it was combined from, Reached from 1 January to the end of each quarter, whether it
is a percentage or a ratio, and its target's numerator and denominator, without any figure the pages already show
moving. The master indicators (``neurodb.datamart.masters``) read these fields from the stored rows."""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

import pytest

from neurodb.core import compute
from neurodb.datamart import models as dm
from neurodb.datamart import monitoring
from neurodb.datamart import reached as rc
from neurodb.indicators.services.tracking import tracking_between
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 9)
CP, EDUCATION = "Child Protection", "Education"
CHILDREN = "# of children reached with psychosocial support"
CAREGIVERS = "% of caregivers reporting improved wellbeing"
CASE_PLANS = "% of children with a case plan"
LEARNING = "# of children in learning spaces"
GIRLS_TO_BOYS = "Ratio of girls to boys enrolled"
HOTLINE = "# of children reached through the hotline"
NONE = (None, None, None)
_ids = itertools.count(1)


def indicator(
    pd, source_id, title, section, *, display_type="number", target=(None, None), places=("Akkar",), **extra
):
    """A PD indicator, one Datamart row per planned location."""
    for place in places:
        dm.PDIndicator.objects.create(
            datamart_id=next(_ids),
            source_id=source_id,
            intervention=pd,
            pd_reference_number=pd.number,
            title=title,
            section_name=section,
            lower_result_name="1.1 Services",
            display_type=display_type,
            unit="percentage" if display_type == "percentage" else "number",
            target_numerator=None if target[0] is None else Decimal(str(target[0])),
            target_denominator=None if target[1] is None else Decimal(str(target[1])),
            location_name=place,
            **extra,
        )


def report(pd, source_id, title, number, end, values, *, kind="QPR", status="Accepted", **extra):
    """One report of one indicator, a row per location; ``values``: location -> PRP's "()" value, a number
    or its ``{"v", "d", "c"}``."""
    for place, value in values.items():
        parts = value if isinstance(value, dict) else {"v": value, "d": 1, "c": value}
        dm.ReportedIndicator.objects.create(
            datamart_id=next(_ids),
            partner=pd.partner,
            intervention=pd,
            pd_reference_number=pd.number,
            progress_report=f"{pd.number}-{number}",
            indicator_report=f"{pd.number}-{number}-{source_id}",
            report_number=number,
            report_type=kind,
            report_status=status,
            period_start=end - datetime.timedelta(days=89 if kind == "QPR" else 29),
            period_end=end,
            due_date=end + datetime.timedelta(days=15),
            submission_date=end + datetime.timedelta(days=10)
            if status in ("Accepted", "Submitted")
            else None,
            indicator=title,
            etools_indicator_id=str(source_id),
            location=place,
            disaggregation={"()": parts},
            **extra,
        )


def pct(v, d):
    return {"v": v, "d": d, "c": v / d}


@pytest.fixture
def world(db):
    """Two partners' programme documents: counts summed and by their maximum, percentages with and without
    their numerators and denominators, a ratio, and an indicator reported only monthly (humanitarian)."""
    partner_a = PartnerOrganization.objects.create(
        etl_id="1", name="Partner A", partner_type="CSO", vendor_number="V1"
    )
    partner_b = PartnerOrganization.objects.create(
        etl_id="2", name="Partner B", partner_type="CSO", vendor_number="V2"
    )
    first = PCA.objects.create(
        etl_id="11",
        partner=partner_a,
        partner_name=partner_a.name,
        number="PD/2026/001",
        title="Programme document 1",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    second = PCA.objects.create(
        etl_id="12",
        partner=partner_b,
        partner_name=partner_b.name,
        number="PD/2025/002",
        title="Programme document 2",
        status="active",
        start=datetime.date(2025, 7, 1),
        end=datetime.date(2027, 6, 30),
    )
    # a count summed over locations and periods: 150 in Q1, 150 in Q2, 60 in Q3 (awaiting acceptance)
    indicator(first, 101, CHILDREN, CP, target=(1000, 1), places=("Akkar", "Bekaa"))
    report(first, 101, CHILDREN, "QPR1", datetime.date(2026, 3, 31), {"Akkar": 100, "Bekaa": 50})
    report(first, 101, CHILDREN, "QPR2", datetime.date(2026, 6, 30), {"Akkar": 120, "Bekaa": 30})
    report(first, 101, CHILDREN, "QPR3", datetime.date(2026, 9, 30), {"Akkar": 60}, status="Submitted")
    # a percentage with its numerators and denominators: (40 + 10 + 90) / (100 + 50 + 100) = 56 %
    indicator(
        first, 102, CAREGIVERS, CP, display_type="percentage", target=(80, 100), places=("Akkar", "Bekaa")
    )
    report(
        first,
        102,
        CAREGIVERS,
        "QPR1",
        datetime.date(2026, 3, 31),
        {"Akkar": pct(40, 100), "Bekaa": pct(10, 50)},
    )
    report(first, 102, CAREGIVERS, "QPR2", datetime.date(2026, 6, 30), {"Akkar": pct(90, 100)})
    # a percentage PRP sends without its numerator and denominator: the latest report's
    indicator(first, 103, CASE_PLANS, CP, display_type="percentage", target=(90, 100))
    report(first, 103, CASE_PLANS, "QPR1", datetime.date(2026, 3, 31), {"Akkar": {"c": 45}})
    report(first, 103, CASE_PLANS, "QPR2", datetime.date(2026, 6, 30), {"Akkar": {"c": 55}})
    # a count by its maximum over the periods, reported last year too
    indicator(second, 201, LEARNING, EDUCATION, target=(500, 1))
    for end, value in (
        ((2025, 12, 31), 300),
        ((2026, 3, 31), 200),
        ((2026, 6, 30), 260),
        ((2026, 9, 30), 240),
    ):
        day = datetime.date(*end)
        report(
            second, 201, LEARNING, f"QPR-{day:%Y%m}", day, {"Akkar": value}, calculation_across_periods="max"
        )
    # a ratio: (45 + 55) / (50 + 50) = 1, never × 100
    indicator(second, 202, GIRLS_TO_BOYS, EDUCATION, display_type="ratio", target=(1, 1))
    report(second, 202, GIRLS_TO_BOYS, "QPR-202603", datetime.date(2026, 3, 31), {"Akkar": pct(45, 50)})
    report(second, 202, GIRLS_TO_BOYS, "QPR-202606", datetime.date(2026, 6, 30), {"Akkar": pct(55, 50)})
    # reported only monthly: read from the humanitarian reports; April's report counts in the second quarter
    indicator(second, 203, HOTLINE, CP, target=(100, 1), is_high_frequency=True)
    report(second, 203, HOTLINE, "HR1", datetime.date(2026, 1, 31), {"Akkar": 10}, kind="HR")
    report(second, 203, HOTLINE, "HR4", datetime.date(2026, 4, 30), {"Akkar": 20}, kind="HR")
    return {"first": first, "second": second}


def by_title(rows):
    return {r.title: r for r in rows}


def quarters(row) -> dict[int, float | None]:
    """Reached from 1 January to the end of each quarter, without its parts."""
    return {q: value for q, (value, _v, _d) in row.to_quarter.items()}


# ---------------------------------------------------------------------------------- collect + _attach_reached
FILTERS = {
    "year": monitoring.Filters(year=2026),
    "every year": monitoring.Filters(year=None),
    "last year": monitoring.Filters(year=2025),
    "section": monitoring.Filters(year=2026, sections=[CP]),
    "section and status": monitoring.Filters(year=2026, sections=[EDUCATION], status="off_track"),
    "not reported": monitoring.Filters(year=2026, status="not_reported"),
    "location": monitoring.Filters(year=2026, locations=["Bekaa"]),
    "words": monitoring.Filters(year=2026, q="children"),
    "humanitarian": monitoring.Filters(year=2026, report_type="HR"),
    "programme document": monitoring.Filters(year=2026, pds=["PD/2025/002"], scope="all"),
}


@pytest.mark.parametrize("filters", FILTERS.values(), ids=FILTERS.keys())
def test_collect_then_reached_is_what_indicators_gives(world, filters):
    found = monitoring.collect(filters)
    monitoring._attach_reached(found, filters, TODAY)
    sections = set(filters.sections)
    by_hand = [
        e
        for e in found.values()
        if (not sections or (e.section or monitoring.NO_SECTION) in sections)
        and (not filters.status or e.tracking == filters.status)
    ]
    assert monitoring.as_rows(by_hand) == monitoring.as_rows(monitoring.indicators(filters, TODAY))


def test_collect_reads_which_indicators_there_are_and_computes_nothing(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("collect computed Reached")

    monkeypatch.setattr(rc, "compute", refuse)
    monkeypatch.setattr(monitoring, "_attach_reached", refuse)
    found = monitoring.collect(monitoring.Filters(year=2026, sections=[CP]))
    # every section: the sections, the status and Reached are left to indicators()
    assert {e.title for e in found.values()} == {
        CHILDREN,
        CAREGIVERS,
        CASE_PLANS,
        LEARNING,
        GIRLS_TO_BOYS,
        HOTLINE,
    }
    assert all(e.result is None and e.reached is None and e.to_quarter == {} for e in found.values())
    assert set(found) == {e.ident for e in found.values()}
    assert monitoring.collect(monitoring.Filters(year=2026, locations=["Bekaa"])).keys() == {
        (world["first"].id, "s101"),
        (world["first"].id, "s102"),
    }
    assert monitoring.collect(monitoring.Filters(pds=["PD/9999/999"])) == {}


# ------------------------------------------------------------------------------------------------ new fields
def test_rows_carry_the_year_with_its_parts_the_quarters_and_the_target_parts(world):
    rows = by_title(monitoring.as_rows(monitoring.indicators(monitoring.Filters(year=2026), TODAY)))
    children = rows[CHILDREN]
    # a count's parts are PRP's v and d (1 at each location) added up: only a percentage or a ratio reads them
    assert children.in_year_item == (360.0, 360.0, 5.0)
    assert children.in_year_item[0] == children.reached_in_year
    assert quarters(children) == {1: 150.0, 2: 300.0, 3: 360.0, 4: 360.0}
    assert (children.ratio, children.percentage, children.target_parts) == (False, False, (1000.0, 1.0))

    caregivers = rows[CAREGIVERS]
    assert caregivers.in_year_item == pytest.approx((56.0, 140.0, 250.0))  # Σv / Σd, never the average
    assert caregivers.to_quarter[1] == pytest.approx((50 / 150 * 100, 50.0, 150.0))
    assert all(caregivers.to_quarter[q] == caregivers.in_year_item for q in (2, 3, 4))
    assert (caregivers.ratio, caregivers.percentage) == (True, True)
    assert (caregivers.target, caregivers.target_parts) == (80.0, (80.0, 100.0))

    case_plans = rows[CASE_PLANS]  # no numerator or denominator: the latest report's, and no parts
    assert case_plans.in_year_item == (55.0, None, None) == case_plans.to_quarter[4]
    assert case_plans.to_quarter[1] == (45.0, None, None)
    assert (case_plans.ratio, case_plans.percentage, case_plans.target_parts) == (True, True, (90.0, 100.0))

    learning = rows[LEARNING]  # the maximum of the year's reports (last year's 300 is not in 2026): no parts
    assert (learning.reached, learning.reached_in_year) == (300.0, 260.0)
    assert learning.in_year_item == (260.0, None, None)
    assert learning.to_quarter == {1: (200.0, None, None), **dict.fromkeys((2, 3, 4), (260.0, None, None))}

    ratio = rows[GIRLS_TO_BOYS]
    assert ratio.in_year_item == (1.0, 100.0, 100.0) and ratio.to_quarter[1] == (0.9, 45.0, 50.0)
    assert (ratio.ratio, ratio.percentage, ratio.target, ratio.target_parts) == (True, False, 1.0, (1.0, 1.0))

    hotline = rows[HOTLINE]  # read from the monthly reports; April's counts in the second quarter
    assert hotline.report_type == "HR"
    assert quarters(hotline) == {1: 10.0, 2: 30.0, 3: 30.0, 4: 30.0}

    # the Indicators the rows are made of carry the same values
    for i in monitoring.indicators(monitoring.Filters(year=2026), TODAY):
        row = rows[i.title]
        assert (i.in_year_item, i.to_quarter, i.ratio, i.percentage, i.target_parts) == (
            row.in_year_item,
            row.to_quarter,
            row.ratio,
            row.percentage,
            row.target_parts,
        )
        assert (i.in_year_item, i.to_quarter) == (i.result.in_year_item, i.result.to_quarter)


def test_without_a_year_there_is_no_year_item_and_no_quarters(world):
    rows = by_title(monitoring.as_rows(monitoring.indicators(monitoring.Filters(year=None), TODAY)))
    assert all(r.in_year_item == NONE and r.to_quarter == {} for r in rows.values())
    assert rows[CAREGIVERS].ratio and rows[CAREGIVERS].target_parts == (80.0, 100.0)
    # a year without a counted report: four empty quarters
    rows = by_title(monitoring.as_rows(monitoring.indicators(monitoring.Filters(year=2025), TODAY)))
    assert rows[CHILDREN].in_year_item == NONE
    assert rows[CHILDREN].to_quarter == dict.fromkeys((1, 2, 3, 4), NONE)
    assert rows[LEARNING].in_year_item == (300.0, None, None) == rows[LEARNING].to_quarter[4]


def test_a_location_filter_reads_the_locations_own_year_and_quarters(world):
    rows = by_title(
        monitoring.as_rows(monitoring.indicators(monitoring.Filters(year=2026, locations=["Bekaa"]), TODAY))
    )
    assert set(rows) == {CHILDREN, CAREGIVERS}
    assert rows[CHILDREN].reached_in_year == rows[CHILDREN].in_year_item[0] == 80.0  # Bekaa: 50 + 30
    assert rows[CHILDREN].to_quarter[1][0] == 50.0
    assert rows[CAREGIVERS].in_year_item == pytest.approx((20.0, 10.0, 50.0))


# ------------------------------------------------------------------------------------- no figure has moved
def test_the_figures_the_pages_show_are_unchanged(world):
    """The values every page read before the new fields: Reached to date and in the year, by year and by month,
    the counted reports, the status and the method (the regression of this change)."""
    rows = by_title(monitoring.indicators(monitoring.Filters(year=2026), TODAY))
    expected = {
        CHILDREN: (360.0, 360.0, {2026: 360.0}, {3: 150.0, 6: 150.0, 9: 60.0}, 3, 1000.0),
        CAREGIVERS: (pytest.approx(56.0), pytest.approx(56.0), {2026: pytest.approx(56.0)},
                     {3: pytest.approx(50 / 150 * 100), 6: 90.0}, 2, 80.0),
        CASE_PLANS: (55.0, 55.0, {2026: 55.0}, {3: 45.0, 6: 55.0}, 2, 90.0),
        LEARNING: (300.0, 260.0, {2025: 300.0, 2026: 260.0}, {3: 200.0, 6: 260.0, 9: 240.0}, 4, 500.0),
        GIRLS_TO_BOYS: (1.0, 1.0, {2026: 1.0}, {3: 0.9, 6: 1.1}, 2, 1.0),
        HOTLINE: (30.0, 30.0, {2026: 30.0}, {1: 10.0, 4: 20.0}, 2, 100.0),
    }  # fmt: skip
    assert set(rows) == set(expected)
    for title, (reached, in_year, years, months, reports, target) in expected.items():
        row = rows[title]
        assert (row.reached, row.reached_in_year, row.years, row.months, row.reports, row.target) == (
            reached,
            in_year,
            years,
            months,
            reports,
            target,
        ), title
        status = tracking_between(row.reached, row.target, row.pd.start, row.pd.end, TODAY)
        assert (row.tracking, row.achieved) == (status.status, status.achieved), title
    assert rows[CHILDREN].awaiting == 60.0 and rows[CHILDREN].as_of == datetime.date(2026, 9, 30)
    assert rows[CHILDREN].method_label == "sum of locations and periods" and rows[CHILDREN].additive
    assert rows[CAREGIVERS].method_label == "numerators over denominators" and not rows[CAREGIVERS].additive
    assert rows[LEARNING].method_label == "sum of locations, max of periods" and not rows[LEARNING].additive


# ------------------------------------------------------------------------------------------- what is stored
def test_rows_stored_before_the_new_fields_are_never_read(world, monkeypatch, settings):
    """A Row changed shape: the stored rows' key carries the cache version, so rows a previous release stored
    (without the new fields) are computed again, never read. Version 3 follows two versions 2 (the rows placed
    and left out; the year's parts and the quarters): rows stored by either are never read."""
    settings.DEBUG = False
    filters = monitoring.Filters(year=2026)
    assert monitoring.CACHE_VERSION == 3
    key = monitoring._rows_key(filters, TODAY, "stamp")
    assert key.startswith("pm-rows:v3:")
    for previous in (1, 2):  # what a previous version stored for the same filter, day and data
        with monkeypatch.context() as patch:
            patch.setattr(monitoring, "CACHE_VERSION", previous)
            patch.setattr(monitoring, "indicators", lambda *args, **kwargs: [])
            assert monitoring._rows_key(filters, TODAY, "stamp") != key
            assert monitoring.stored_indicators(filters, TODAY) == []
        compute.reset_memo()
    rows = monitoring.stored_indicators(filters, TODAY)
    assert len(rows) == 6 and by_title(rows)[CHILDREN].to_quarter[2][0] == 300.0
