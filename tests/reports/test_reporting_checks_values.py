"""The eTools reporting checks 1 to 5 and 7 measure what NeuroDB reads: PRP's values parsed as Reached parses
them (a number, its text or PRP's {v, d, c}), a progress report told apart by its programme document, type,
number and period whatever PRP's ids hold, PRP's cumulative compared through Partner monitoring's own
figures, and the report rows matched to the PD indicators by Partner monitoring's own rule. Every figure is
a count: no title, name or value of a record is in a check's result."""

from __future__ import annotations

import datetime
import itertools
import json
import re
import time
from decimal import Decimal

import pytest
from django.db import connection, transaction

from neurodb.datamart import checks, monitoring
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db
D = datetime.date
NONE = object()  # a row without a disaggregation total "()"
JUNE = D(2026, 6, 30)
_ids = itertools.count(910_000)


def one(key: str) -> dict:
    """One check as the page runs it: in its own transaction, with a minute for its statements."""
    check = next(c for c in checks.CHECKS if c.key == key)
    with transaction.atomic(), connection.cursor() as cursor:
        return check.run(checks._Timed(cursor, time.monotonic() + 60))


def figures(found: dict) -> dict:
    return dict(found["figures"])


def report_row(
    pd,
    number="QPR1",
    *,
    end=JUNE,
    start=None,
    title="Children reached",
    location="Akkar",
    status="Accepted",
    total=NONE,
    **extra,
):
    """One location of one indicator in one progress report of ``pd`` (None: a PD NeuroDB does not hold),
    its "()" given as ``total`` (as stored: a dict, its text, a number, or None)."""
    kind = extra.pop("report_type", re.sub(r"\d", "", number))
    extra.setdefault("disaggregation", {} if total is NONE else {"()": total})
    return dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner=pd.partner if pd else None,
        pd_reference_number=extra.pop("reference", pd.number if pd else ""),
        report_number=number,
        report_type=kind,
        report_status=status,
        period_start=start or end - datetime.timedelta(days=89 if kind == "QPR" else 29),
        period_end=end,
        due_date=end + datetime.timedelta(days=15),
        submission_date=end + datetime.timedelta(days=10) if status in ("Accepted", "Submitted") else None,
        indicator=title,
        location=location,
        **extra,
    )


@pytest.fixture
def pd(db):
    partner = PartnerOrganization.objects.create(etl_id="901", name="Partner A", vendor_number="2500000901")
    found = PCA.objects.create(
        etl_id="9101",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PD/CHK1",
        title="Running PD",
        status="active",
        start=D(2026, 1, 1),
        end=D(2027, 12, 31),
    )
    dm.PDIndicator.objects.create(
        datamart_id=9101,
        source_id=11,
        intervention=found,
        title="Children reached",
        target_numerator=Decimal("1000"),
        display_type="number",
    )
    return found


def other_pd(number="LEB/PD/CHK2", status="active"):
    partner = PartnerOrganization.objects.first()
    return PCA.objects.create(
        etl_id=f"x{number}",
        partner=partner,
        partner_name=partner.name,
        number=number,
        title="Another PD",
        status=status,
        start=D(2026, 1, 1),
        end=D(2027, 12, 31),
    )


# --------------------------------------------------------------------------------- the value parser
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{'c': 100.0, 'd': 1, 'v': 100}", 100.0),
        ('{"v": 0, "d": 1, "c": 0}', 0.0),
        ({"v": 30, "d": 40}, 0.75),
        ("{'c': 0, 'd': 0, 'v': 0}", 0.0),
        ("0.0", 0.0),
        (9, 9.0),
        ("30", 30.0),
        ({"c": None, "v": None, "d": 1}, None),
        ("n/a", None),
        ("", None),
        (None, None),
    ],
)
def test_values_are_read_as_reached_reads_them(raw, expected):
    assert checks._value(raw) == expected


# -------------------------------------------------------------------------------------------- check 1
def test_check_1_tells_reports_apart_by_their_period_not_by_prps_empty_id(pd):
    # QPR1 and QPR2 of one indicator at one place, no progress report id: two reports, no duplicate
    report_row(pd, "QPR1", end=D(2026, 3, 31), total={"c": 10})
    report_row(pd, "QPR2", end=D(2026, 6, 30), total={"c": 20})
    found = one("1")
    assert figures(found)["Locations on several rows of one indicator in one report"] == 0
    assert figures(found)["Locations Reached reads as one report across several reports"] == 0
    assert found["tone"] == "ok" and "Each location appears on one row" in found["verdict"]
    # one place written two ways (" lb11" and "LB11") in one report is one location: two rows
    report_row(pd, "QPR2", end=D(2026, 6, 30), p_code=" lb11", location="", total={"c": 5})
    report_row(pd, "QPR2", end=D(2026, 6, 30), p_code="LB11", location="", total={"c": 5})
    found = one("1")
    assert figures(found)["Locations on several rows of one indicator in one report"] == 1
    assert figures(found)["… with different values"] == 0 and found["tone"] == "ok"


def test_check_1_counts_different_values_in_counted_reports_only_as_a_risk(pd):
    for value in (200, 7):  # two PRP location records of one indicator report, Accepted
        report_row(pd, "QPR1", indicator_report="501", total={"c": value})
    found = one("1")
    assert figures(found)["… with different values"] == 1
    assert figures(found)["… of which in reports that count (Reached takes the largest)"] == 1
    assert found["tone"] == "warning" and "Reached uses the largest, never their sum" in found["verdict"]
    dm.ReportedIndicator.objects.update(report_status="Due", submission_date=None)
    found = one("1")
    assert figures(found)["… with different values"] == 1
    assert figures(found)["… of which in reports that count (Reached takes the largest)"] == 0
    assert found["tone"] == "ok"


SHARED = "Locations two or more indicator reports of one title give in one report"
SHARED_DIFFER = "… carrying different values (Reached keeps the one under its own output)"
KEPT = (
    "Reached keeps the one under the PD indicator's own output, else leaves that report out with a note, "
    "never the largest of them nor their sum"
)


def test_check_1_two_indicators_of_a_report_with_one_title_are_not_a_duplicate(pd):
    report_row(pd, indicator_report="501", total={"c": 3})
    report_row(pd, indicator_report="502", total={"c": 9})
    found = one("1")
    assert (figures(found)[SHARED], figures(found)[SHARED_DIFFER]) == (1, 1)
    assert figures(found)["… with different values"] == 0 and found["tone"] == "ok"
    # the values are 3 and 9: never "the same value", and Reached keeps one, never "both out"
    assert "same value, which Reached reads once" not in found["verdict"]
    assert "leaves both out" not in found["verdict"] and KEPT in found["verdict"]
    assert "(1 with different values, 0 with the same value)" in found["verdict"]


@pytest.mark.parametrize("ids", [("", ""), ("501", "502")])
def test_check_1_words_two_indicator_reports_of_one_title_by_reacheds_rule(pd, ids):
    """PD 7's QPR1 (1 Jan-31 Mar 2026, Accepted): "Children reached" at LB11 under output A (40) and output B
    (7), with no PRP indicator report id or with 501 and 502. Neither "the largest" (the rows are two
    indicator reports, not duplicates) nor "the same value" nor "both left out": Reached keeps the one
    under the PD indicator's own output."""
    for ir, output, value in zip(ids, ("Output A", "Output B"), (40, 7), strict=True):
        report_row(pd, "QPR1", end=D(2026, 3, 31), start=D(2026, 1, 1), p_code="LB11", location="",
                   indicator_report=ir, pd_output=output, total={"c": value})  # fmt: skip
    found = one("1")
    numbers = figures(found)
    assert found["tone"] == "ok"
    assert numbers["Locations on several rows of one indicator in one report"] == 0
    assert (numbers["… with different values"], numbers[SHARED], numbers[SHARED_DIFFER]) == (0, 1, 1)
    assert found["verdict"] == (
        "Each location appears on one row per indicator in each report (a report told apart by its programme "
        "document, type, number and period; an indicator by PRP's indicator report id, else its title and PD "
        "output). At 1 location, two or more indicator reports of one report carry the same title (1 with "
        f"different values, 0 with the same value): {KEPT} (check 7 counts the rows Partner monitoring "
        "places)."
    )
    # two rows of one of them at that place still say what Reached does with them: the largest
    report_row(pd, "QPR1", end=D(2026, 3, 31), start=D(2026, 1, 1), p_code="LB11", location="",
               indicator_report=ids[0], pd_output="Output A", total={"c": 12})  # fmt: skip
    found = one("1")
    assert figures(found)["… of which in reports that count (Reached takes the largest)"] == 1
    assert "Reached uses the largest, never their sum" in found["verdict"] and found["tone"] == "warning"


def test_checks_1_and_2_tell_two_indicators_of_one_title_apart_by_their_pd_output(pd):
    """Two PD indicators titled "Children reached" under "A. Protection" and "B. Education", rows with no PRP
    indicator report id: Akkar under A (3) and under B (9) are two indicator reports, never a location
    given twice; with Bekaa under each, check 2 counts two indicator reports with several locations, as the
    tiles count them."""
    from neurodb.datamart import services

    dm.PDIndicator.objects.filter(intervention=pd).update(lower_result_name="A. Protection")
    dm.PDIndicator.objects.create(datamart_id=9102, source_id=12, intervention=pd, title="Children reached",
                                  lower_result_name="B. Education")  # fmt: skip
    report_row(pd, pd_output="A. Protection", total={"c": 3})
    report_row(pd, pd_output="B. Education", total={"c": 9})
    found = one("1")
    assert figures(found)["Locations on several rows of one indicator in one report"] == 0
    assert figures(found)["… with different values"] == 0 and found["tone"] == "ok"
    assert (
        figures(one("7"))["… matched by PD output and title (a title several indicators of the PD share)"]
        == 2
    )
    report_row(pd, pd_output="A. Protection", location="Bekaa", total={"c": 4})
    report_row(pd, pd_output="B. Education", location="Bekaa", total={"c": 6})
    assert figures(one("2"))["Indicator reports with several locations"] == 2
    assert services.partner_reporting({}, datetime.date.today())["counts"]["indicator_reports"] == 2


def test_check_2_never_reads_two_outputs_of_one_title_as_one_report_total(pd):
    # one Accepted QPR1, no PRP id: output A at LB11 and output B at LB22, each "()" 5: two indicator
    # reports of one location each, never "the same value on every location" of one
    report_row(pd, p_code="LB11", location="", pd_output="Output A", total={"c": 5})
    report_row(pd, p_code="LB22", location="", pd_output="Output B", total={"c": 5})
    found = one("2")
    assert found["verdict"].startswith("No indicator report has more than one location")
    assert found["tone"] != "warning"


def test_check_1_two_programme_documents_never_pool(pd):
    second = other_pd()
    report_row(pd, total={"c": 3})
    report_row(second, total={"c": 9})  # same title, place, number and period
    assert figures(one("1"))["Locations on several rows of one indicator in one report"] == 0


def test_check_1_a_progress_report_id_that_stands_for_several_reports_is_read_as_several(pd):
    # PRP's id repeated on QPR1 and QPR2 (finding ID-6): Reached reads two reports, their PD, type, number and
    # period, never one report keeping the largest; check 7 counts the id
    report_row(pd, "QPR1", end=D(2026, 3, 31), progress_report="PR-PD", total={"c": 200})
    report_row(pd, "QPR2", end=D(2026, 6, 30), progress_report="PR-PD", total={"c": 7})
    found = one("1")
    assert figures(found)["Locations on several rows of one indicator in one report"] == 0
    assert figures(found)["Locations Reached reads as one report across several reports"] == 0
    assert found["tone"] == "ok"
    assert figures(one("7"))["PRP progress report ids standing for more than one report"] == 1


# -------------------------------------------------------------------------------------------- check 2
def test_check_2_reads_the_locations_disaggregation_total(pd):
    # a due report, every location 0: the same value everywhere, harmless
    for place in ("Akkar", "Bekaa", "Beirut"):
        report_row(
            pd, "QPR3", end=D(2026, 12, 31), status="Due", location=place, total={"c": 0, "v": 0, "d": 0}
        )
    found = one("2")
    assert figures(found)['… with the same "()" on every location'] == 1
    assert figures(found)["… of which all zero"] == 1 and found["tone"] == "ok"
    # an accepted report with the same value above zero on every location: the risk the check is for
    for place in ("Akkar", "Bekaa", "Beirut"):
        report_row(pd, "QPR1", location=place, total={"c": 1}, achievement_in_period=str(place))
    found = one("2")
    assert figures(found)["… of which above zero, in reports that count and add up their locations"] == 1
    assert found["tone"] == "warning"


def test_check_2_finds_the_report_total_repeated_in_the_value_in_the_period(pd):
    for place, value in (("Akkar", 10), ("Bekaa", 15), ("Beirut", 5)):
        report_row(pd, location=place, total={"c": value}, achievement_in_period="30")
    found = one("2")
    assert figures(found)['… where the value in the period is the locations\' "()" combined'] == 1
    assert figures(found)['… with the same "()" on every location'] == 0
    assert "decides nothing in Reached" in found["verdict"]


def test_check_2_never_pools_two_reports(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31), location="Akkar", total={"c": 4})
    report_row(pd, "QPR2", end=D(2026, 6, 30), location="Bekaa", total={"c": 4})
    found = one("2")
    assert found["verdict"].startswith("No indicator report has more than one location")


def test_check_2_counts_the_rows_whose_total_holds_no_number(pd):
    for n, total in enumerate(({"c": None, "v": None, "d": None}, {"c": None, "v": None, "d": 1}, None)):
        report_row(pd, f"QPR{n + 1}", end=D(2026, 3 + n, 28), total=total)
    report_row(pd, "QPR4", end=D(2026, 7, 31))  # no "()" key at all
    for n, total in enumerate(("30", 9, {"v": 5, "d": 1})):  # numbers, as Reached reads them
        report_row(pd, f"QPR{n + 5}", end=D(2026, 8 + n, 28), total=total)
    assert figures(one("2"))['Rows whose "()" gives no number'] == 4


# -------------------------------------------------------------------------------------------- check 3
def test_check_3_compares_prps_structured_values_as_numbers(pd):
    report_row(pd, total={"c": 100, "d": 1, "v": 100}, achievement_in_period="{'c': 100.0, 'd': 1, 'v': 100}")
    found = one("3")
    numbers = figures(found)
    assert numbers['Rows compared ("()" and the value in the period both numbers)'] == 1
    assert numbers["… equal"] == 1 and numbers["… written as PRP's {v, d, c}"] == 1
    assert "on 1 of 1 rows (100 %)" in found["verdict"] and found["tone"] == "ok"


@pytest.mark.parametrize(
    ("total", "achieved"),
    [
        ("{'c': 5, 'd': 1, 'v': 5}", "5"),  # "()" stored as text
        (30, "30"),  # "()" as a plain number
        ({"v": 30, "d": 40}, "0.75"),  # no c: v over d
        ({"v": 45, "d": 100, "c": 0.45}, "{'v': 45, 'd': 100, 'c': 0.45}"),  # a ratio, read alike
    ],
)
def test_check_3_reads_every_shape_alike(pd, total, achieved):
    report_row(pd, total=total, achievement_in_period=achieved)
    assert figures(one("3"))["… equal"] == 1


def test_check_3_says_when_nothing_could_be_compared(pd):
    report_row(pd, total={"c": 4}, achievement_in_period="n/a")
    found = one("3")
    assert figures(found)['Rows compared ("()" and the value in the period both numbers)'] == 0
    assert "could not be compared" in found["verdict"] and "0 %" not in found["verdict"]


def test_check_3_tells_a_locations_own_value_from_the_report_total_repeated(pd):
    for place, value in (("Akkar", 100), ("Bekaa", 200)):
        report_row(pd, location=place, indicator_report="77", total={"c": value}, achievement_in_period="300")
    numbers = figures(one("3"))
    assert numbers["… it is the report total, repeated on every location"] == 1
    assert numbers["… the value in the period is each location's own"] == 0 and numbers["… equal"] == 0
    dm.ReportedIndicator.objects.all().delete()
    for place, value in (("Akkar", 100), ("Bekaa", 200)):
        report_row(
            pd, location=place, indicator_report="77", total={"c": value}, achievement_in_period=str(value)
        )
    numbers = figures(one("3"))
    assert numbers["… the value in the period is each location's own"] == 1 and numbers["… equal"] == 2


def test_check_3_counts_the_rows_reached_reads_another_way(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31), total={"c": None, "v": None, "d": None})
    report_row(pd, "QPR2", end=D(2026, 6, 30), total={"c": 3})
    found = one("3")
    assert figures(found)["… holding none (Reached reads them another way)"] == 1
    assert "1 of 2 rows give no number" in found["verdict"] and found["tone"] == "info"


# -------------------------------------------------------------------------------------------- check 4
def test_check_4_reads_prps_empty_value_as_no_value(pd):
    empty = "{'c': 0, 'd': 0, 'v': 0}"
    report_row(pd, "QPR3", end=D(2026, 12, 31), status="Due", achievement_in_period=empty,
               total_cumulative_progress=empty)  # fmt: skip
    report_row(pd, "QPR4", end=D(2027, 3, 31), status="Due", achievement_in_period="0.0")
    report_row(pd, "QPR5", end=D(2027, 6, 30), status="Due", achievement_in_period='{"v": 0, "d": 1, "c": 0}')
    assert checks._value("{'c': 0, 'd': 0, 'v': 0}") == 0.0  # Reached reads it as 0: only check 4 says none
    check = next(c for c in checks.CHECKS if c.key == "4")
    assert "is no value" not in check.neurodb and "here a zero" in check.neurodb
    found = one("4")
    assert figures(found)["Rows of reports that do not count, with a value"] == 0
    assert found["verdict"] == "Reports that do not count carry no value."
    (due,) = [r for r in found["table"]["rows"] if r[0] == "Due"]
    assert due[3:] == (3, 1, 2, 0)  # rows, cumulative 0, cumulative empty, value given
    report_row(pd, "QPR6", end=D(2027, 9, 30), status="Due", achievement_in_period="{'c': 5, 'd': 1, 'v': 5}")
    found = one("4")
    assert figures(found)["Rows of reports that do not count, with a value"] == 1
    assert "1 row of reports that do not count" in found["verdict"]


# -------------------------------------------------------------------------------------------- check 5
def test_check_5_counts_reports_as_reached_does(pd, monkeypatch):
    # no progress report id, PRP's indicator report ids, the title in another case and spacing: two
    # reports of one indicator, with PRP's cumulative written two ways
    report_row(pd, "QPR1", end=D(2026, 3, 31), title="  children  REACHED", indicator_report="11",
               total={"c": 50}, total_cumulative_progress="50")  # fmt: skip
    report_row(pd, "QPR2", end=D(2026, 6, 30), title="  children  REACHED", indicator_report="12",
               total={"c": 0}, total_cumulative_progress="{'v': 50, 'd': 1, 'c': 50.0}")  # fmt: skip
    # a due report never makes a second report
    report_row(pd, "QPR3", end=D(2026, 9, 30), title="Children reached", status="Due", indicator_report="13",
               total={"c": 5}, total_cumulative_progress="55")  # fmt: skip
    found = one("5")
    numbers = figures(found)
    assert numbers["Indicators of the running programme documents with several counted reports"] == 1
    assert numbers["… with the same PRP cumulative on every report"] == 1
    assert numbers["… whose PRP cumulative never goes down from one report to the next"] == 1
    assert numbers["Indicators compared (running PDs, Reached and PRP's cumulative both given)"] == 1
    assert numbers["Indicators where Reached differs from PRP"] == 0 and found["tone"] == "ok"
    # an ended PD's indicator is not read
    ended = other_pd("LEB/PD/ENDED", status="ended")
    dm.PDIndicator.objects.create(
        datamart_id=9199, source_id=99, intervention=ended, title="Children reached"
    )
    for n, end in ((1, D(2025, 3, 31)), (2, D(2025, 6, 30))):
        report_row(ended, f"QPR{n}", end=end, total={"c": 1}, total_cumulative_progress="9")
    assert (
        figures(one("5"))["Indicators of the running programme documents with several counted reports"] == 1
    )


def test_check_5_shows_the_mismatches_with_their_denominator(pd):
    # two counted reports adding up to 150, PRP's cumulative 450 on the latest: PRP is above Reached;
    # 450 is the two plus a due report's 300
    report_row(pd, "QPR1", end=D(2026, 3, 31), total={"c": 100}, total_cumulative_progress="100")
    report_row(pd, "QPR2", end=D(2026, 6, 30), total={"c": 50}, total_cumulative_progress="450")
    report_row(
        pd, "QPR3", end=D(2026, 9, 30), status="Due", total={"c": 300}, total_cumulative_progress="450"
    )
    found = one("5")
    numbers = figures(found)
    assert numbers["Indicators where Reached differs from PRP"] == 1
    assert "on 1 of the 1 indicators where both are given" in found["verdict"] and found["tone"] == "warning"
    why = dict(found["table"]["rows"])
    assert why["PRP's cumulative is above Reached"] == 1
    assert why["PRP's cumulative equals every report combined, also those that do not count"] == 1
    # the same figure as the Data quality tab of Partner monitoring
    filters = monitoring.Filters(scope="active", year=None)
    quality = monitoring.data_quality(monitoring.indicators(filters), filters)
    assert quality["mismatch_count"] == numbers["Indicators where Reached differs from PRP"]


def test_check_5_names_the_mismatches_even_with_no_indicator_of_several_reports(db, monkeypatch):
    monkeypatch.setattr(checks, "_reached_against_prp", lambda: checks._Comparison(compared=300, differ=22))
    found = one("5")
    assert "22 of the 300" in found["verdict"] and found["tone"] == "warning"
    assert figures(found)["Indicators compared (running PDs, Reached and PRP's cumulative both given)"] == 300
    monkeypatch.setattr(checks, "PRP_COMPARE_MIN_S", 1000.0)  # too little time left: nothing is compared
    found = one("5")
    assert found["verdict"].startswith("Not compared") and found["tone"] == "info"
    assert {value for _, value in found["figures"]} == {"—"}


# -------------------------------------------------------------------------------------------- check 7
@pytest.fixture
def indicators(pd):
    for n, (source_id, title) in enumerate(((12, "Kits distributed"), (13, "Kits distributed"),
                                            (14, "Teachers trained"))):  # fmt: skip
        dm.PDIndicator.objects.create(datamart_id=9110 + n, source_id=source_id, intervention=pd, title=title)
    return pd


def test_check_7_counts_the_rows_partner_monitoring_uses_and_leaves_out(indicators):
    pd = indicators
    data = {"etools_cp_output_indicators_id": None, "partner_name": "Partner A", "performance_indicator": "x"}
    report_row(pd, title="children reached ", data=data)  # by title
    report_row(pd, title="Kits distributed", data=data)  # two indicators of the PD have the title
    report_row(pd, title="Unknown title", data={"partner_name": "Partner A"})  # no indicator has it
    report_row(pd, title="something else", etools_indicator_id="14", data=data)  # by id
    report_row(pd, title="Teachers trained", etools_indicator_id="999", data=data)  # unknown id: by title
    found = one("7")
    numbers = figures(found)
    assert numbers["… matched to a PD indicator by its id"] == 1
    assert numbers["… matched by title (one indicator of the PD has it)"] == 2
    assert numbers["… matched by PD output and title (a title several indicators of the PD share)"] == 0
    assert numbers["… left out: two or more indicators of the PD have the title and the output"] == 1
    assert numbers[UNMATCHED] == 0
    assert numbers["… left out: no indicator of the PD has the title"] == 1
    assert numbers["Indicator reports left out"] == 2 and found["tone"] == "warning"
    assert (
        "Left out: 1 row whose title and PD output two or more indicators of their PD share"
        in (found["verdict"])
    )
    census = {line[0]: line for line in found["table"]["rows"]}
    # the key NeuroDB reads the eTools indicator id from: on 4 rows, never with a value
    assert census["etools_cp_output_indicators_id"][1:4] == ("yes", 4, 0)
    assert census["etools_cp_output_indicators_id"][-1] == 4  # empty (null)
    assert census["progress_report"][1:4] == ("yes", 0, 0)  # read by the sync, sent by no record
    assert census["partner_name"][1:4] == ("yes", 5, 5)
    # counts only: no title, name or value of a record
    text = json.dumps(found)
    for secret in ("Partner A", "Unknown title", "Kits distributed", "something else", "LEB/PD/CHK1"):
        assert secret not in text


UNMATCHED = "… left out: a title several indicators of the PD share, and a PD output none of them has"


def test_check_7_and_data_quality_name_a_pd_output_that_matches_none(pd):
    """PD indicators "# of youth trained" under "2.1 Skills" and "2.2 Jobs": a row whose PD output is
    "Output 2.1: Skills" (PRP's spelling) and one with none are left out because their output matches
    neither, never because the indicators share it; Data quality counts both indicators left out."""
    for n, output in enumerate(("2.1 Skills", "2.2 Jobs")):
        dm.PDIndicator.objects.create(datamart_id=9120 + n, source_id=20 + n, intervention=pd,
                                      title="# of youth trained", lower_result_name=output)  # fmt: skip
    report_row(pd, title="# of youth trained", pd_output="Output 2.1: Skills", total={"c": 4})
    report_row(pd, title="# of youth trained", pd_output="", location="Bekaa", total={"c": 6})
    found = one("7")
    numbers = figures(found)
    assert numbers[UNMATCHED] == 2
    assert numbers["… left out: two or more indicators of the PD have the title and the output"] == 0
    assert numbers["Indicator reports left out"] == 2  # each its title and PD output
    assert "2 rows whose title several indicators of their PD share and whose PD output" in found["verdict"]
    assert "correct the output name in PRP or eTools so the two match" in found["verdict"]
    assert "# of youth trained" not in json.dumps(found)
    filters = monitoring.Filters(scope="all", year=None)
    quality = monitoring.data_quality(monitoring.indicators(filters), filters)
    (group,) = quality["same_title"]
    assert (group["count"], group["left_out"], group["rows_unmatched"]) == (2, 2, 2)
    assert (quality["same_title_left_out"], quality["same_title_rows_unmatched"]) == (2, 2)


def test_check_7_says_whether_prps_ids_tell_the_reports_apart(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    report_row(pd, "HR3", end=D(2026, 6, 30))
    found = one("7")
    assert figures(found)["Rows with no PRP progress report id"] == 2
    assert "progress report id is empty on every row" in found["verdict"]
    assert figures(found)["PD indicators in monthly (HR) reports"] == 1
    assert figures(found)["… also in quarterly (QPR) reports"] == 1
    # one id for the PD's reports (a PD-level value): it stands for two reports
    dm.ReportedIndicator.objects.update(progress_report="PR-PD")
    found = one("7")
    assert figures(found)["PRP progress report ids standing for more than one report"] == 1
    assert figures(found)["Reports with more than one PRP progress report id"] == 0
    assert "1 progress report id of PRP stands for more than one report" in found["verdict"]
    assert "reads them as the separate reports they are" in found["verdict"]
    assert found["tone"] == "ok"  # every page tells the two reports apart by their number and period
    # two ids on one report (a report PRP re-created): counted, the report read once
    dm.ReportedIndicator.objects.filter(report_number="HR3").update(progress_report="PR-A")
    report_row(pd, "HR3", end=D(2026, 6, 30), progress_report="PR-B", location="Bekaa")
    assert figures(one("7"))["Reports with more than one PRP progress report id"] == 1


def test_check_7_tables_say_what_tells_shared_indicators_apart(pd):
    """Two PD indicators "Kits distributed" share the title and the output (none given), one high frequency:
    Table A says high frequency alone would place their two rows (not used on the pages: no sync has proved
    it), Table B gives the value pairs of the row placed by title, Table C the PD indicator key that tells the
    two apart, Table D the record key that differs between the two rows of one place. Counts and key names
    only: no title, name or text of a record."""
    for n, hf in enumerate((True, False)):
        dm.PDIndicator.objects.create(
            datamart_id=9130 + n,
            source_id=30 + n,
            intervention=pd,
            title="Kits distributed",
            is_high_frequency=hf,
            location_pcode="LB11",
            data={"is_high_frequency": hf, "title": "Kits distributed"},
        )
    for text in ("Lists of Partner A", "Photos of Partner A"):
        report_row(pd, title="Kits distributed", high_frequency=True, p_code="LB11",
                   data={"means_of_verification": text})  # fmt: skip
    report_row(pd, data={"cluster_indicator": "false", "indicator_type": "quantity"})  # placed by its title
    found = one("7")
    numbers = figures(found)
    assert numbers["… left out: two or more indicators of the PD have the title and the output"] == 2
    assert numbers["… at a location where the report has one row of that title and output"] == 0
    assert numbers["… where it has two or more"] == 2
    assert (numbers["… placed on the pages by a tie-breaker"], numbers["… still left out"]) == (0, 2)
    assert numbers["Titles and outputs two or more indicators of a running PD share"] == 1
    assert numbers["… PD indicators in them"] == 2
    assert numbers["… whose indicators agree on every field compared"] == 0
    assert "tie-breakers place 0 of them on the pages and 2 stay left out" in found["verdict"]
    tables = {t["title"]: t for t in found["tables"]}
    lines = {
        line[0]: line
        for line in tables["What tells apart indicators that share a title and an output"]["rows"]
    }
    assert lines["High frequency"][1:5] == ("high_frequency", "is_high_frequency", "no", 1)
    assert lines["High frequency"][7:] == (1, 2, 0, 0)  # the shared pair differs; it alone would place both
    assert lines["Target"][8:] == (0, 0, 0)  # neither the rows nor the copies give one: it does not apply
    assert lines["All, in this order (on the pages)"][8:] == (0, 2, 0)
    pairs = tables["Value pairs on the rows already placed"]["rows"]
    assert ("high_frequency", "false", "is_high_frequency no", 1) in pairs
    assert ("cluster_indicator", "false", "no cluster", 1) in pairs
    assert ("indicator_type", "quantity", "number / (empty)", 1) in pairs
    assert tables["PD indicator keys that differ inside a shared group"]["rows"] == [("is_high_frequency", 1)]
    assert tables["Report row keys that differ between the rows of one location"]["rows"] == [
        ("means_of_verification", 1)
    ]
    text = json.dumps(found)
    for secret in ("Kits distributed", "Children reached", "Partner A", "Lists of"):
        assert secret not in text
