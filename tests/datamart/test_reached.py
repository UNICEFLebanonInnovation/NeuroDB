"""The one rule for eTools "Reached" (neurodb.datamart.reached): which reports count, a location's value
in a report, a report's value, and Reached to date, in a year, per month, per location and per
disaggregation key. Most tests feed report rows straight to ``reached.compute``; the worked example and
the property tests pin what the user complained about: the numbers never add what PRP repeats, and
they add up wherever they should."""

from __future__ import annotations

import datetime
import random

import pytest

from neurodb.datamart import reached

TODAY = datetime.date(2026, 10, 9)
_ids = iter(range(1, 10**9))


def row(report, end, location, value=None, *, status="Accepted", submitted=True, p_code="", **extra):
    """One partner report row (a ``ReportedIndicator.values()`` dict), its value in the "()" total."""
    out = dict.fromkeys(reached.ROW_FIELDS, "")
    out.update(
        datamart_id=next(_ids),
        intervention_id=1,
        indicator="5.2.1 # adolescents and youth reached with outreach info sessions",
        etools_indicator_id="521",
        progress_report=report,
        indicator_report=f"ir-{report}",
        report_number=report,
        report_type="QPR",
        report_status=status,
        period_start=end - datetime.timedelta(days=89),
        period_end=end,
        due_date=end + datetime.timedelta(days=30),
        submission_date=end + datetime.timedelta(days=20) if submitted else None,
        disaggregation={"()": {"v": value, "d": 1, "c": value}} if value is not None else {},
        location=location,
        p_code=p_code,
        location_ref_id=None,
    )
    out.update(extra)
    return out


def compute(rows, **kwargs):
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("year", 2026)
    return reached.compute(rows, **kwargs)


# --------------------------------------------------------------------------------- worked example
def worked_example_rows():
    """synthesis §1: target 3,000, sum across locations and periods, PD Jul 2025 – Dec 2027, two
    locations; QPR3's Akkar total is 20 with female 5 + male 15 (sex) and Lebanese 20 (nationality)."""
    q = [
        ("QPR1", datetime.date(2025, 9, 30), "Accepted", 100, 50),
        ("QPR2", datetime.date(2025, 12, 31), "Accepted", 120, 80),
        ("QPR3", datetime.date(2026, 3, 31), "Accepted", 20, 30),
        ("QPR4", datetime.date(2026, 6, 30), "Submitted", 60, 40),
    ]
    rows = []
    for report, end, status, akkar, bekaa in q:
        rows.append(row(report, end, "Akkar", akkar, status=status, p_code="LB1"))
        rows.append(row(report, end, "Bekaa", bekaa, status=status, p_code="LB2"))
    rows[4]["disaggregation"] = {
        "()": {"v": 20, "d": 1, "c": 20},
        "(1,)": {"v": 5, "d": 1, "c": 5},  # female
        "(2,)": {"v": 15, "d": 1, "c": 15},  # male
        "(3,)": {"v": 20, "d": 1, "c": 20},  # Lebanese
    }
    # QPR5 due on 30 October (period ended), QPR6 not yet due: they carry nothing that counts
    for report, end, carried in (
        ("QPR5", datetime.date(2026, 9, 30), 0),
        ("QPR6", datetime.date(2026, 12, 31), 0),
    ):
        for place, code in (("Akkar", "LB1"), ("Bekaa", "LB2")):
            rows.append(
                row(
                    report,
                    end,
                    place,
                    carried,
                    status="Due",
                    submitted=False,
                    p_code=code,
                    total_cumulative_progress="0",
                )
            )
    return rows


def test_the_worked_example():
    out = compute(worked_example_rows())
    assert out.reached == 500.0  # 150 + 200 + 50 + 100: Accepted and Submitted reports count
    assert (out.awaiting, out.awaiting_reports) == (100.0, 1)  # QPR4 awaits acceptance
    assert out.reached_accepted == 400.0
    assert out.reached_in_year == 150.0 and out.months == {3: 50.0, 6: 100.0}
    assert out.years == {2025: 350.0, 2026: 150.0} and sum(out.years.values()) == out.reached
    assert {k: v["reached"] for k, v in out.by_location.items()} == {"LB1": 300.0, "LB2": 200.0}
    assert sum(v["reached"] for v in out.by_location.values()) == out.reached
    assert [r.status for r in out.reports] == [
        "accepted",
        "accepted",
        "accepted",
        "submitted",
        "due",
        "not_yet_due",
    ]
    assert out.latest.number == "QPR4" and out.value_status == reached.REPORTED
    # Akkar in QPR3: sex 5 + 15 = 20, nationality 20, location total 20 (never 40)
    cell = next(r for r in out.reports if r.number == "QPR3").cells["LB1"]
    assert cell.value == 20.0
    assert cell.parts["(1,)"] + cell.parts["(2,)"] == cell.parts["(3,)"] == cell.parts["()"] == cell.value
    # the zero a due or future report carries never becomes "the latest"
    assert out.prp_cumulative is None and not out.mismatch


# ------------------------------------------------------------------------------ report statuses
@pytest.mark.parametrize(
    ("text", "submitted", "end", "due", "expected"),
    [
        ("Accepted", None, datetime.date(2026, 6, 30), None, "accepted"),
        (" ACC ", None, datetime.date(2026, 6, 30), None, "accepted"),
        ("Submitted", None, datetime.date(2026, 6, 30), None, "submitted"),
        ("sub", None, datetime.date(2026, 6, 30), None, "submitted"),
        ("Sent back", datetime.date(2026, 7, 2), datetime.date(2026, 6, 30), None, "sent_back"),
        ("Sen", None, datetime.date(2026, 6, 30), None, "sent_back"),
        ("Due", None, datetime.date(2026, 9, 30), datetime.date(2026, 10, 30), "due"),
        ("Due", None, datetime.date(2026, 6, 30), datetime.date(2026, 7, 30), "overdue"),
        ("Overdue", None, datetime.date(2026, 6, 30), datetime.date(2026, 7, 30), "overdue"),
        ("Due", None, datetime.date(2026, 12, 31), datetime.date(2027, 1, 30), "not_yet_due"),
        ("", None, datetime.date(2026, 12, 31), None, "not_yet_due"),
        ("", datetime.date(2026, 7, 2), datetime.date(2026, 6, 30), None, "submitted"),  # a date settles it
        ("Accepted later", None, datetime.date(2026, 6, 30), datetime.date(2026, 7, 30), "overdue"),
        # PRP's own Due or Overdue never counts, even with a submission date (a reopened report); it is
        # then Due, never Overdue, as monitoring.pending_q leaves it out of every overdue figure
        ("Due", datetime.date(2026, 7, 2), datetime.date(2026, 6, 30), datetime.date(2026, 7, 30), "due"),
        ("Ove", datetime.date(2026, 7, 2), datetime.date(2026, 6, 30), datetime.date(2026, 7, 30), "due"),
        ("overdue", datetime.date(2026, 7, 2), datetime.date(2026, 6, 30), None, "due"),
    ],
)
def test_six_statuses(text, submitted, end, due, expected):
    status = reached.status_of(text, period_end=end, due_date=due, submission_date=submitted, today=TODAY)
    assert status == expected
    assert (status in reached.COUNTED) is (expected in ("accepted", "submitted"))


def test_not_reported_and_reported_zero():
    due_only = [row("QPR1", datetime.date(2026, 6, 30), "Akkar", 0, status="Due", submitted=False)]
    out = compute(due_only)
    assert out.reached is None and out.value_status == reached.NOT_REPORTED and out.counted == []
    zero = [row("QPR1", datetime.date(2026, 6, 30), "Akkar", 0)]
    out = compute(zero)
    assert out.reached == 0.0 and out.value_status == reached.REPORTED_ZERO


# ----------------------------------------------------------------------------- property tests
def _random_case(seed: int):
    rng = random.Random(seed)
    places = [(f"Place {n}", f"LB{n}") for n in range(1, rng.randint(1, 4) + 1)]
    rows, ends = [], []
    year = rng.choice([2024, 2025])
    for q in range(rng.randint(1, 8)):
        month = 3 * (q % 4) + 3
        end = datetime.date(year + q // 4, month, 28)
        status = rng.choice(["Accepted", "Accepted", "Submitted", "Sent back", "Due", "Overdue"])
        report = f"QPR{q + 1}"
        for place, code in places:
            if rng.random() < 0.15:
                continue  # a location not reported in this report
            value = rng.randint(0, 400)
            female = rng.randint(0, value)
            refugees = rng.randint(0, value)
            rows.append(
                row(
                    report,
                    end,
                    place,
                    value,
                    status=status,
                    submitted=status in ("Accepted", "Submitted", "Sent back"),
                    p_code=code,
                    disaggregation={
                        "()": {"v": value, "d": 1, "c": value},
                        "(1,)": {"v": female, "d": 1, "c": female},
                        "(2,)": {"v": value - female, "d": 1, "c": value - female},
                        "(3,)": {"v": refugees, "d": 1, "c": refugees},
                        "(4,)": {"v": value - refugees, "d": 1, "c": value - refugees},
                    },
                )
            )
        ends.append(end)
    return rows


@pytest.mark.parametrize("seed", range(40))
def test_sum_indicators_add_up_everywhere(seed):
    rows = _random_case(seed)
    out = compute(rows, year=None)
    counted = [r for r in rows if r["report_status"] in ("Accepted", "Submitted")]
    expected = sum(r["disaggregation"]["()"]["c"] for r in counted) if counted else None
    assert out.reached == expected
    if out.reached is None:
        return
    # the years add up to Reached, and each year's months to Reached in that year
    assert sum(out.years.values()) == out.reached
    for year, value in out.years.items():
        in_year = compute(rows, year=year)
        assert in_year.reached_in_year == value and sum(in_year.months.values()) == value
    # the locations add up to Reached
    assert sum(loc["reached"] or 0 for loc in out.by_location.values()) == out.reached
    # each disaggregation type adds up to the location total; types are never added together
    for place, keys in out.disaggregation.items():
        assert keys["(1,)"] + keys["(2,)"] == keys["()"] == out.by_location[place]["reached"]
        assert keys["(3,)"] + keys["(4,)"] == keys["()"]
    # what awaits acceptance is exactly what the Submitted reports add
    submitted = [r for r in counted if r["report_status"] == "Submitted"]
    assert (out.awaiting or 0) == sum(r["disaggregation"]["()"]["c"] for r in submitted)


@pytest.mark.parametrize("seed", range(20))
def test_reports_that_do_not_count_change_nothing(seed):
    rows = _random_case(seed)
    base = compute(rows, year=None)
    extra = list(rows)
    for n, status in enumerate(("Due", "Sent back", "Overdue")):
        end = datetime.date(2026, 3 * n + 3, 28)
        extra.append(
            row(
                f"X{n}",
                end,
                "Place 1",
                9999,
                status=status,
                submitted=status == "Sent back",
                p_code="LB1",
                total_cumulative_progress="0",
            )
        )
    extra.append(
        row(
            "FUTURE", datetime.date(2027, 3, 31), "Place 1", 9999, status="Due", submitted=False, p_code="LB1"
        )
    )
    out = compute(extra, year=None)
    assert (out.reached, out.years, out.awaiting) == (base.reached, base.years, base.awaiting)
    assert {k: v["reached"] for k, v in out.by_location.items()} == {
        k: v["reached"] for k, v in base.by_location.items()
    }
    assert out.prp_cumulative == base.prp_cumulative


def test_submitted_counts_and_shows_as_awaiting_acceptance():
    rows = [
        row("QPR1", datetime.date(2026, 3, 31), "Akkar", 100),
        row("QPR2", datetime.date(2026, 6, 30), "Akkar", 40, status="Submitted"),
    ]
    out = compute(rows)
    assert (out.reached, out.reached_accepted, out.awaiting, out.awaiting_reports) == (140.0, 100.0, 40.0, 1)
    # sent back later: it no longer counts
    rows[1]["report_status"] = "Sent back"
    out = compute(rows)
    assert (out.reached, out.awaiting, out.awaiting_reports) == (100.0, None, 0)


# --------------------------------------------------------------------------- a location's value
def test_a_value_repeated_on_every_location_is_the_report_total():
    rows = [
        row("QPR1", datetime.date(2026, 3, 31), place, None, achievement_in_period="120")
        for place in ("Akkar", "Bekaa", "Tyre")
    ]
    out = compute(rows)
    assert out.reached == 120.0  # never 360
    assert all(v["reached"] is None for v in out.by_location.values())
    assert any("report total" in note for note in out.notes)
    # different values per location are each location's own and add up
    for r, value in zip(rows, ("100", "15", "5"), strict=True):
        r["achievement_in_period"] = value
    assert compute(rows).reached == 120.0
    # a single location is never "repeated"
    assert compute(rows[:1]).reached == 100.0


def test_location_running_totals_are_the_fallback():
    rows = [
        row(
            "QPR2",
            datetime.date(2026, 6, 30),
            "Akkar",
            None,
            total_cumulative_progress_in_location="350",
            previous_location_progress="200",
            achievement_in_period="999",
        )
    ]
    assert compute(rows).reached == 150.0  # 350 − 200, before the value in the period
    rows[0]["previous_location_progress"] = ""
    assert compute(rows).reached == 999.0


def test_two_rows_of_one_location_are_never_added():
    rows = [
        row("QPR1", datetime.date(2026, 3, 31), "Akkar", 200),
        row("QPR1", datetime.date(2026, 3, 31), "akkar ", 7),  # the same place, by its name
        row("QPR1", datetime.date(2026, 3, 31), "Bekaa", 100),
    ]
    out = compute(rows)
    assert out.reached == 300.0 and out.by_location["name:akkar"]["reached"] == 200.0
    rows[1]["disaggregation"]["()"] = {"v": 200, "d": 1, "c": 200}
    out = compute(rows)
    assert out.reached == 300.0 and not out.notes  # the same value twice: nothing to say


@pytest.mark.parametrize(
    ("progress_report", "indicator_report"),
    [("LEB/PD1", "ir-1"), ("", "Children reached"), ("", "")],
)
def test_a_report_is_its_pd_type_number_and_period_whatever_prps_ids_say(progress_report, indicator_report):
    """QPR1 (March, 100) and QPR2 (June, 50) at one place under one PRP id (a PD-level progress report id, an
    indicator report id that does not vary, or none): two counted reports, 150, never the largest (100)."""
    rows = [
        row(r, end, "Akkar", value, progress_report=progress_report, indicator_report=indicator_report)
        for r, end, value in (
            ("QPR1", datetime.date(2026, 3, 31), 100),
            ("QPR2", datetime.date(2026, 6, 30), 50),
        )
    ]
    out = compute(rows)
    assert (out.reached, len(out.counted), out.months) == (150.0, 2, {3: 100.0, 6: 50.0})
    assert [r.key for r in out.reports] == [
        "pd1|QPR|QPR1|2026-01-01|2026-03-31",
        "pd1|QPR|QPR2|2026-04-02|2026-06-30",
    ]
    # a report whose rows disagree on its status reads the word of its rows changed last
    rows.append(row("QPR2", datetime.date(2026, 6, 30), "Bekaa", 10, status="Due", submitted=False))
    rows[-1]["last_modify_date"], rows[1]["last_modify_date"] = "2026-07-01", "2026-07-09"
    out = compute(rows)
    assert [r.status for r in out.reports] == ["accepted", "accepted"] and out.reached == 160.0


# ------------------------------------------------------------------------------------- methods
def test_percentages_are_numerators_over_denominators():
    rows = [
        row(
            "QPR1",
            datetime.date(2026, 3, 31),
            place,
            None,
            disaggregation={"()": {"v": v, "d": d, "c": v / d}},
        )
        for place, v, d in (("Akkar", 8, 10), ("Bekaa", 40, 50), ("Tyre", 2, 40))
    ]
    out = compute(rows, display_type="percentage")
    assert out.reached == pytest.approx(50.0)  # (8 + 40 + 2) / (10 + 50 + 40), never 80 + 80 + 5
    rows += [
        row(
            "QPR2",
            datetime.date(2026, 6, 30),
            "Akkar",
            None,
            disaggregation={"()": {"v": 30, "d": 100, "c": 0.3}},
        )
    ]
    out = compute(rows, display_type="percentage")
    assert out.reached == pytest.approx(80 / 200 * 100)  # across periods too
    assert out.method_label == "numerators over denominators" and not out.additive


@pytest.mark.parametrize(
    ("locations", "periods", "expected", "in_2026"),
    [
        ("max", "sum", 60 + 70, 70.0),
        ("avg", "sum", (60 + 20) / 2 + (70 + 10) / 2, 40.0),
        ("sum", "max", 80.0, 80.0),
        ("sum", "latest", 80.0, 80.0),
        ("sum", "avg", 80.0, 80.0),
        ("", "", 160.0, 80.0),
    ],
)
def test_location_and_period_methods(locations, periods, expected, in_2026):
    rows = []
    for report, end, akkar, bekaa in (
        ("QPR1", datetime.date(2025, 12, 31), 60, 20),
        ("QPR2", datetime.date(2026, 3, 31), 70, 10),
    ):
        for place, value in (("Akkar", akkar), ("Bekaa", bekaa)):
            rows.append(
                row(
                    report,
                    end,
                    place,
                    value,
                    calculation_across_locations=locations,
                    calculation_across_periods=periods,
                )
            )
    out = compute(rows)
    assert out.reached == pytest.approx(expected) and out.reached_in_year == pytest.approx(in_2026)
    assert out.additive is (locations in ("", "sum") and periods in ("", "sum"))


def test_a_max_across_periods_is_not_three_times_the_children():
    rows = [
        row(f"QPR{n}", datetime.date(2026, 3 * n, 28), "Akkar", 1000, calculation_across_periods="max")
        for n in (1, 2, 3)
    ]
    out = compute(rows)
    assert out.reached == 1000.0 and out.method_label == "sum of locations, max of periods"


# ------------------------------------------------ the year with its numerator and denominator, the quarters
NONE = (None, None, None)
QUARTER_ENDS = {1: datetime.date(2026, 3, 31), 2: datetime.date(2026, 6, 30), 3: datetime.date(2026, 9, 30)}


def pct_row(report, end, location, v, d, **extra):
    """A percentage or ratio row: numerator ``v`` over denominator ``d`` in the "()" total."""
    return row(report, end, location, None, disaggregation={"()": {"v": v, "d": d, "c": v / d}}, **extra)


def in_year_items(out, year=2026):
    """The value of each counted report of ``year``, oldest first: what Reached in the year combines."""
    return [out.items[r.key] for r in out.counted if r.period_end.year == year]


def percentage_rows(**extra):
    return [
        pct_row("QPR0", datetime.date(2025, 12, 31), "Akkar", 99, 100, **extra),  # last year: not in 2026
        pct_row("QPR1", datetime.date(2026, 3, 31), "Akkar", 40, 100, **extra),
        pct_row("QPR1", datetime.date(2026, 3, 31), "Bekaa", 10, 50, **extra),
        pct_row("QPR2", datetime.date(2026, 6, 30), "Akkar", 30, 50, **extra),
    ]


def test_the_worked_example_by_quarter():
    out = compute(worked_example_rows())
    # the figures every page reads, unchanged
    assert (out.reached, out.reached_in_year, out.years, out.months) == (
        500.0,
        150.0,
        {2025: 350.0, 2026: 150.0},
        {3: 50.0, 6: 100.0},
    )
    # Reached in 2026 with PRP's v and d of each location added (d is 1 for a count: only a percentage or a
    # ratio reads them), and from 1 January to the end of each quarter
    assert out.in_year_item == (150.0, 150.0, 4.0)
    assert {q: value for q, (value, _v, _d) in out.to_quarter.items()} == {
        1: 50.0,
        2: 150.0,
        3: 150.0,
        4: 150.0,
    }


def test_the_year_keeps_the_numerators_and_denominators_of_a_sum_over_periods():
    out = compute(percentage_rows(), display_type="percentage")
    assert out.in_year_item == pytest.approx((40.0, 80.0, 200.0))  # (40 + 10 + 30) / (100 + 50 + 50)
    assert out.in_year_item[0] == out.reached_in_year
    assert out.in_year_item == reached.combine_items(
        in_year_items(out), "sum", ratio=True, percentage=True, fallback="latest"
    )
    out = compute(percentage_rows(), display_type="ratio")  # a ratio: the same parts, never × 100
    assert out.in_year_item == pytest.approx((0.4, 80.0, 200.0))


def test_the_year_of_latest_is_the_latest_reports_numerator_and_denominator():
    out = compute(percentage_rows(calculation_across_periods="latest"), display_type="percentage")
    assert out.in_year_item == pytest.approx((60.0, 30.0, 50.0)) and out.in_year_item == out.counted[-1].item
    assert out.in_year_item[0] == out.reached_in_year


@pytest.mark.parametrize(("method", "expected"), [("max", 60.0), ("avg", (50 / 150 * 100 + 60) / 2)])
def test_the_year_of_an_average_or_maximum_has_no_numerator_or_denominator(method, expected):
    out = compute(percentage_rows(calculation_across_periods=method), display_type="percentage")
    assert out.in_year_item[1:] == (None, None)
    assert out.in_year_item[0] == out.reached_in_year == pytest.approx(expected)


def test_the_quarters_are_cumulative_and_a_report_counts_in_the_quarter_its_period_ends():
    rows = [
        row("R0", datetime.date(2025, 12, 31), "Akkar", 999),  # last year
        row("R1", datetime.date(2026, 1, 31), "Akkar", 10),
        row("R2", datetime.date(2026, 3, 31), "Akkar", 20),
        # began in March, ended in April: the second quarter
        row("R3", datetime.date(2026, 4, 30), "Akkar", 40, period_start=datetime.date(2026, 3, 1)),
        row("R4", datetime.date(2026, 9, 30), "Akkar", 80, status="Submitted"),
        row("R5", datetime.date(2026, 12, 31), "Akkar", 1000, status="Due", submitted=False),  # not yet due
    ]
    out = compute(rows)
    assert {q: item[0] for q, item in out.to_quarter.items()} == {1: 30.0, 2: 70.0, 3: 150.0, 4: 150.0}
    assert out.to_quarter[4] == out.in_year_item and out.reached_in_year == 150.0
    for q, (value, _v, _d) in out.to_quarter.items():  # a sum: the months of the quarters so far add up to it
        assert value == sum(v for m, v in out.months.items() if m <= 3 * q)
    # no year chosen: neither; a year without a counted report: four empty quarters
    every = compute(rows, year=None)
    assert (every.in_year_item, every.to_quarter, every.reached) == (NONE, {}, 1149.0)
    assert compute(rows, year=2027).to_quarter == dict.fromkeys((1, 2, 3, 4), NONE)


@pytest.mark.parametrize("display_type", ["number", "percentage", "ratio"])
@pytest.mark.parametrize("period_method", ["", "sum", "max", "avg", "latest"])
def test_each_quarter_is_the_year_cut_at_its_end_for_every_method(display_type, period_method):
    later = datetime.date(2027, 1, 15)  # the fourth quarter's report is in
    rows = []
    for report, end, places in (
        ("R0", datetime.date(2025, 12, 31), {"Akkar": (5, 10)}),
        ("R1", datetime.date(2026, 2, 28), {"Akkar": (30, 60), "Bekaa": (10, 40)}),
        ("R2", datetime.date(2026, 5, 31), {"Akkar": (45, 50)}),
        ("R3", datetime.date(2026, 6, 30), {"Bekaa": (8, 40)}),
        ("R4", datetime.date(2026, 11, 30), {"Akkar": (20, 25), "Bekaa": (1, 4)}),
    ):
        for place, (v, d) in places.items():
            if display_type == "number":
                rows.append(row(report, end, place, v, calculation_across_periods=period_method))
            else:
                rows.append(pct_row(report, end, place, v, d, calculation_across_periods=period_method))
    out = compute(rows, display_type=display_type, today=later)
    ratio, percentage = display_type != "number", display_type == "percentage"
    assert out.in_year_item == reached.combine_items(
        in_year_items(out), period_method or "sum", ratio=ratio, percentage=percentage, fallback="latest"
    )
    assert out.in_year_item[0] == out.reached_in_year and out.to_quarter[4] == out.in_year_item
    for q, end in QUARTER_ENDS.items():  # Reached in the year of the reports that ended by then
        cut = [r for r in rows if r["period_end"] <= end]
        assert out.to_quarter[q] == compute(cut, display_type=display_type, today=later).in_year_item, q
    assert out.to_quarter[3] == out.to_quarter[2]  # no report ends in the third quarter
    if period_method in ("max", "avg"):
        assert all(item[1:] == (None, None) for item in out.to_quarter.values())
    # under a location filter, the location's own
    only = compute(rows, display_type=display_type, today=later, only={"bekaa"})
    assert only.to_quarter[4] == only.in_year_item and only.in_year_item[0] == only.reached_in_year
    assert (
        only.to_quarter[1][0] == compute(rows[:3], display_type=display_type, only={"bekaa"}).reached_in_year
    )


@pytest.mark.parametrize("seed", range(40))
def test_the_quarters_of_a_sum_add_up_to_its_months(seed):
    rows = _random_case(seed)
    for year in sorted({r["period_end"].year for r in rows}):
        out = compute(rows, year=year)
        assert out.in_year_item[0] == out.reached_in_year == out.years.get(year)
        assert out.to_quarter[4] == out.in_year_item
        for q, (value, _v, _d) in out.to_quarter.items():
            months = [v for m, v in out.months.items() if m <= 3 * q]
            assert value == (sum(months) if months else None)


def test_the_year_and_quarters_keep_only_the_indicators_own_output_of_two_indicator_reports():
    """Two PRP indicators of one title under two PD outputs in QPR1 (March) and QPR2 (June): the year's
    parts and the quarters, which the master indicators combine, are those of the rows under the indicator's
    own output (what Reached keeps, reached.one_indicator); an output that tells neither apart leaves both
    reports out of the year and every quarter, never the largest nor the sum."""
    rows = []
    for number, end, a, b in (
        ("QPR1", datetime.date(2026, 3, 31), 40, 7),
        ("QPR2", datetime.date(2026, 6, 30), 30, 9),
    ):
        rows.append(row(number, end, "Akkar", a, pd_output="Output A", indicator_report=f"a-{number}"))
        rows.append(row(number, end, "Akkar", b, pd_output="Output B", indicator_report=f"b-{number}"))
    for output, year, quarters in (
        ("Output A", (70.0, 70.0, 2.0), {1: 40.0, 2: 70.0, 3: 70.0, 4: 70.0}),
        ("output b ", (16.0, 16.0, 2.0), {1: 7.0, 2: 16.0, 3: 16.0, 4: 16.0}),
    ):
        out = compute(rows, output=output)
        assert (out.reached_in_year, out.in_year_item) == (year[0], year), output
        assert {q: value for q, (value, _v, _d) in out.to_quarter.items()} == quarters, output
    neither = compute(rows, output="Output C")
    assert neither.reached_in_year is None and neither.in_year_item == NONE
    assert all(item == NONE for item in neither.to_quarter.values())


# ----------------------------------------------------------------- PRP's own totals and history
def test_prp_report_total_stands_in_when_the_locations_cannot_say():
    rows = [row("QPR1", datetime.date(2026, 3, 31), p, None, achievement_in_period="50") for p in ("A", "B")]
    out = compute(rows, progress_values={"ir-QPR1": "{'v': 50, 'd': 1, 'c': 50.0}"})
    assert out.reached == 50.0 and out.reports[0].source == "prp"
    rows = [row("QPR1", datetime.date(2026, 3, 31), p, v) for p, v in (("A", 30), ("B", 20))]
    out = compute(rows, progress_values={"ir-QPR1": "50"})
    assert out.reached == 50.0 and not out.notes
    out = compute(rows, progress_values={"ir-QPR1": "75"})
    assert out.reached == 50.0 and any("differs" in n for n in out.notes)


def test_people_are_whole_a_small_share_is_still_a_difference():
    """For a count, "agrees" means the same number of people: 10,040 is not 10,000 (0.4 % apart), so
    PRP's report total never replaces the locations' sum, and the cumulative is flagged."""
    rows = [
        row("QPR1", datetime.date(2026, 3, 31), p, v, total_cumulative_progress="10040")
        for p, v in (("Akkar", 6000), ("Bekaa", 4000))
    ]
    out = compute(rows, progress_values={"ir-QPR1": "10040"})
    assert out.reached == 10000.0 and out.mismatch and out.prp_cumulative == 10040.0
    assert sum(loc["reached"] for loc in out.by_location.values()) == out.reached
    assert any("differs from its locations" in n for n in out.notes)
    # the same number: the locations' sum stays, and they add up to the report exactly
    out = compute(rows, progress_values={"ir-QPR1": "10000"})
    assert out.reached == 10000.0 and out.reports[0].source == "locations" and not out.notes
    # a percentage agrees within half a point: the locations' numerators over denominators stay
    pct = [
        row(
            "QPR1",
            datetime.date(2026, 3, 31),
            p,
            None,
            disaggregation={"()": {"v": v, "d": d, "c": v / d}},
            total_cumulative_progress="{'v': 503, 'd': 1000, 'c': 0.503}",
        )
        for p, v, d in (("Akkar", 30, 60), ("Bekaa", 20, 40))
    ]
    out = compute(pct, display_type="percentage", progress_values={"ir-QPR1": "{'c': 50.3}"})
    assert out.reached == pytest.approx(50.0) and not out.mismatch and out.prp_cumulative == 50.3
    assert (out.reports[0].v, out.reports[0].d, out.reports[0].source) == (50.0, 100.0, "locations")
    assert reached.close(80.0, 80.4, ratio=True, percentage=True)
    assert not reached.close(0.80, 0.85, ratio=True) and reached.close(0.800, 0.803, ratio=True)


def test_prp_cumulative_beside_reached_and_partial_history():
    rows = [
        row("QPR1", datetime.date(2026, 3, 31), "Akkar", 100, total_cumulative_progress="400"),
        row("QPR2", datetime.date(2026, 6, 30), "Akkar", 50, total_cumulative_progress="450"),
    ]
    out = compute(rows)
    assert (out.reached, out.prp_cumulative, out.mismatch) == (150.0, 450.0, True)
    out = compute(rows, partial=True)  # the PD started before the reports NeuroDB reads
    assert (out.reached, out.source, out.mismatch) == (450.0, "prp_cumulative", False)
    rows[1]["total_cumulative_progress"] = "150"
    assert compute(rows).mismatch is False


def test_location_filter_reads_the_locations_own_reached():
    rows = [
        row("QPR1", datetime.date(2026, 3, 31), p, v, total_cumulative_progress="300")
        for p, v in (("Akkar", 200), ("Bekaa", 100))
    ]
    out = compute(rows, only={"bekaa"}, progress_values={"ir-QPR1": "300"})
    assert out.reached == 100.0 and out.prp_cumulative is None and list(out.by_location) == ["name:bekaa"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"v": 1200, "d": 1, "c": 1200}, 1200.0),
        ("{'c': 1200.0, 'd': 1, 'v': 1200}", 1200.0),
        ('{"v": 45, "d": 100}', 0.45),
        ("1,234", 1234.0),
        ("45/100", None),
        ("{'v': None}", None),
    ],
)
def test_numbers(raw, expected):
    assert reached.number(raw) == expected


def test_combine_never_adds_a_ratio():
    assert reached.combine([80, 80, 80], "sum") == 240
    assert reached.combine_items([(80, None, None)] * 3, "sum", ratio=True)[0] == 80
    assert reached.combine([1, 5, 3], "latest") == 3 and reached.combine([1, 5, 3], "max") == 5
    assert reached.combine([], "sum") is None


# ------------------------------------------------------------------------------- the sync window
@pytest.mark.django_db
def test_history_start_reaches_back_to_running_pds_never_too_far(settings):
    from neurodb.partnerships.models import PCA

    settings.ETOOLS_DATAMART_REPORTING_YEARS = 3
    assert reached.history_start(TODAY) == datetime.date(2024, 1, 1)
    PCA.objects.create(etl_id="1", number="A", status="active", start=datetime.date(2021, 7, 1))
    PCA.objects.create(
        etl_id="2",
        number="B",
        status="closed",
        start=datetime.date(2019, 1, 1),
        end=datetime.date(2022, 12, 31),
    )  # ended: not running
    PCA.objects.create(etl_id="3", number="C", status="draft", start=datetime.date(2018, 1, 1))
    assert reached.history_start(TODAY) == datetime.date(2021, 7, 1)
    PCA.objects.create(etl_id="4", number="D", status="active", start=datetime.date(1999, 1, 1))  # a typo
    assert reached.history_start(TODAY) == datetime.date(2021, 7, 1)  # never more than 10 years back
    settings.ETOOLS_DATAMART_REPORTING_FROM_PD_START = False
    assert reached.history_start(TODAY) == datetime.date(2024, 1, 1)


@pytest.mark.django_db
def test_partial_history_follows_the_window_the_latest_sync_read(settings):
    """A PD has a partial history when it started before the reports NeuroDB holds: the window the
    latest finished partner reports sync read from, not the one the next sync will read (a widened
    window counts once a sync has read it)."""
    from django.utils import timezone

    from neurodb.core.models import SyncRun
    from neurodb.partnerships.models import PCA

    settings.ETOOLS_DATAMART_REPORTING_YEARS = 3
    PCA.objects.create(etl_id="1", number="A", status="active", start=datetime.date(2021, 7, 1))
    # no sync yet: the window the sync will read
    assert reached.reports_read_from(TODAY) == reached.history_start(TODAY) == datetime.date(2021, 7, 1)

    def run(status=SyncRun.Status.SUCCEEDED, target="partner_reports", day=1, **details):
        return SyncRun.objects.create(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=target,
            status=status,
            started_at=timezone.make_aware(datetime.datetime(2026, 10, day, 3, 0)),
            details=details,
        )

    # a run of the code before the window was recorded read the last three years of its day
    run(day=1, removed=0)
    assert reached.reports_read_from(TODAY) == datetime.date(2024, 1, 1)
    # a later run that failed, read nothing, or synced another dataset changes nothing
    run(SyncRun.Status.FAILED, day=2, read_from="2021-07-01")
    run(day=3, read_from="2021-07-01", empty_response=True)
    run(target="pd_indicators", day=4, read_from="2021-07-01")
    assert reached.reports_read_from(TODAY) == datetime.date(2024, 1, 1)
    # the next sync reads from the PD's start (a run with errors still wrote its rows)
    run(SyncRun.Status.PARTIAL, day=5, read_from="2021-07-01")
    assert reached.reports_read_from(TODAY) == datetime.date(2021, 7, 1)


@pytest.mark.django_db
def test_history_counts_from_the_feed_when_the_datamart_holds_no_earlier_report():
    """The sync asked from 1 Jan 2025 and the rows held start on 5 Jan 2026, more than 100 days later: the
    Datamart holds no earlier report, so the history is counted from the feed's first report, whether the read
    was whole or not, and a running PD that started in 2025 has a partial history. A feed that starts within
    100 days of the date asked changes nothing, and neither does a run made before feed_from was recorded."""
    from django.utils import timezone

    from neurodb.core.models import SyncRun
    from neurodb.datamart.models import ReportedIndicator

    asked, feed, started = datetime.date(2025, 1, 1), datetime.date(2026, 1, 5), datetime.date(2025, 6, 1)
    assert reached.history_from(TODAY).source == reached.NEXT_SYNC  # no sync yet

    def latest(**details) -> reached.HistoryFrom:
        SyncRun.objects.all().delete()
        SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="partner_reports",
                               status=SyncRun.Status.SUCCEEDED, started_at=timezone.now(),
                               details={"read_from": "2025-01-01", **details})  # fmt: skip
        return reached.history_from(TODAY)

    found = latest(feed_from="2026-01-05")
    assert found == reached.HistoryFrom(feed, reached.FEED, asked) and found.date > started  # partial
    assert reached.reports_read_from(TODAY) == feed
    # a feed 59 days after the date asked: the reports of that time are held
    assert latest(feed_from="2025-03-01") == reached.HistoryFrom(asked, reached.READ_FROM, asked)
    # a read not whole removed nothing: what is held still counts, never the earliest row with no margin
    assert (
        latest(feed_from="2026-01-05", removal_skipped=True, read=10, datamart_count=12).source
        == reached.FEED
    )
    on_a_shift = latest(feed_from="2025-03-01", removal_skipped=True, duplicates_read=3)
    assert on_a_shift.source == reached.READ_FROM and on_a_shift.date <= started  # the PD stays whole
    # read in date windows, every one whole from the date asked: the Datamart still holds nothing earlier
    assert latest(window_bound="reporting_period_start_date", whole_from="2025-01-01",
                  feed_from="2026-01-05").source == reached.FEED  # fmt: skip
    assert latest(window_bound="reporting_period_start_date", whole_from="2025-01-01",
                  feed_from="2025-02-01") == reached.HistoryFrom(asked, reached.WHOLE_FROM, asked)  # fmt: skip
    # an answer cut at a round count: the earliest row held, as before
    ReportedIndicator.objects.create(datamart_id=1, period_start=datetime.date(2025, 4, 1))
    found = latest(datamart_count=10_000, removal_skipped=True, feed_from="2026-01-05")
    assert found == reached.HistoryFrom(datetime.date(2025, 4, 1), reached.CUT, asked)
    # a run made before feed_from was recorded: nothing changes until the next sync
    assert latest() == reached.HistoryFrom(asked, reached.READ_FROM, asked)
