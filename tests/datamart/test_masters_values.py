"""The figures of the eTools master indicators (neurodb.datamart.masters.figures), read through real partner
monitoring rows: each sub's figure is its own Reached in the year, never its report rows added again.

* A sum adds numbers: up to N (partners may reach the same people), at least the largest programme document's
  figure; within one programme document only its largest sub, unless they cannot count the same people or the
  section said its parts add up; a value too large to be true is set aside.
* A percentage or a ratio is numerators added up over denominators added up (× 100 for a percentage only); a
  sub reported without them is left out and said so ("counted 1 of 2 PDs"), never averaged.
* An average, a maximum or a minimum combine the programme documents' figures, all of one type.
* Only confirmed links in a counting role count; the status is measured against the year's typed target alone.
"""

from __future__ import annotations

import pytest

from neurodb.datamart import masters, monitoring
from neurodb.indicators.services.tracking import tracking

from . import masters_world as w
from .masters_world import day

pytestmark = pytest.mark.django_db

ENROLLED = "# of children enrolled in NFE"


@pytest.fixture
def pds(db):
    """Three running programme documents of three partners."""
    return {letter: w.programme(letter, n) for n, letter in enumerate("ABC", start=1)}


def quarterly(pd, source_id, title, values, **extra):
    """Quarterly reports of one indicator at Akkar: ``values`` is quarter -> value (Q1 ends 31 March)."""
    for quarter, value in values.items():
        end = {1: day(3, 31), 2: day(6, 30), 3: day(9, 30), 4: day(12, 31)}[quarter]
        w.report(pd, source_id, title, f"QPR{quarter}", end, {"Akkar": value}, **extra)


# ------------------------------------------------------------------------------------------------------- sum
def test_a_sum_adds_the_programme_documents_figures_up_to_and_at_least(pds):
    a, b = pds["A"], pds["B"]
    w.indicator(a, 101, ENROLLED, target=(1000, 1), places=("Akkar", "Bekaa"))
    w.report(a, 101, ENROLLED, "QPR1", day(3, 31), {"Akkar": 100, "Bekaa": 50})
    quarterly(a, 101, ENROLLED, {2: 200, 3: 150})
    w.indicator(b, 201, "# of girls and boys enrolled in NFE", target=(600, 1))
    quarterly(b, 201, "# of girls and boys enrolled in NFE", {1: 80, 2: 120})
    m = w.master("Children enrolled in non-formal education", target=1000, code="ED-1.1")
    w.link(m, a, 101)
    w.link(m, b, 201)

    fig = w.figure(m)
    assert (fig.value, fig.at_least) == (700.0, 500.0)
    assert fig.quarters == {1: 230.0, 2: 550.0, 3: 700.0, 4: 700.0}  # cumulative from 1 January
    assert all(s.counted and s.reason == "" for s in fig.subs)
    assert [s.value for s in fig.subs] == [500.0, 200.0]
    assert fig.how == (
        "Up to 700: the sum over the programme documents; partners may reach the same people. "
        "At least 500 (the largest programme document)."
    )
    # every sub adds over its locations and periods: the year's share of the target is expected
    assert fig.prorate and fig.as_of == day(9, 30)
    expected = tracking(700, 1000, w.YEAR, day(9, 30), prorate=True)
    assert (fig.status, fig.achieved) == (expected.status, expected.achieved) == ("on_track", 70.0)
    assert (fig.code, fig.target, fig.status_label) == ("ED-1.1", 1000, "On track")
    assert (fig.counted_pds, fig.pds, fig.coverage, fig.partners) == (2, 2, "counted 2 of 2 PDs", 2)
    assert (fig.reporting, fig.eligible) == (2, 2)
    assert fig.pd_target == 1600.0  # for reference only


@pytest.mark.parametrize(
    "method, in_year, quarters",
    [("max", 260.0, {1: 300.0, 2: 460.0, 3: 560.0}), ("latest", 240.0, {1: 300.0, 2: 460.0, 3: 540.0})],
)
def test_a_sum_with_a_level_takes_its_one_figure_and_is_not_prorated(pds, method, in_year, quarters):
    a, c = pds["A"], pds["C"]
    w.indicator(a, 102, "# of children receiving learning kits", target=(800, 1))
    quarterly(a, 102, "# of children receiving learning kits", {1: 100, 2: 100, 3: 100})
    w.indicator(c, 301, "# of children in learning spaces", target=(500, 1))
    quarterly(
        c,
        301,
        "# of children in learning spaces",
        {1: 200, 2: 260, 3: 240},
        calculation_across_periods=method,
    )
    m = w.master("Children supported to learn", target=700)
    w.link(m, a, 102)
    w.link(m, c, 301)

    fig = w.figure(m)
    sub = w.subs(fig)["s301"]
    assert sub.value == in_year and sub.counted  # its own year figure, nothing that repeats added
    assert fig.value == 300.0 + in_year
    assert {q: fig.quarters[q] for q in (1, 2, 3)} == quarters and fig.quarters[4] == fig.value
    # a level is not expected to grow during the year: the whole target
    assert not fig.prorate
    whole = tracking(fig.value, 700, w.YEAR, day(9, 30), prorate=False)
    assert (fig.status, fig.achieved) == (whole.status, whole.achieved)
    assert fig.status == "off_track"
    assert tracking(fig.value, 700, w.YEAR, day(9, 30), prorate=True).status == "on_track"


def test_one_programme_documents_subs_are_added_only_when_they_cannot_count_the_same_people(pds):
    a, b = pds["A"], pds["B"]
    for source_id, title, value in (
        (111, "# of girls reached with PSS", 300),
        (112, "# of boys reached with PSS", 200),
        (113, "# of children reached with PSS sessions", 450),
        (114, "# of children with disabilities reached with PSS sessions", 40),
        (115, "# of Syrian children reached with recreational activities", 100),
        (116, "# of Lebanese children reached with recreational activities", 60),
    ):
        w.indicator(a, source_id, title, section=w.CP)
        quarterly(a, source_id, title, {1: value})
    w.indicator(b, 211, "# of children reached with PSS", section=w.CP)
    quarterly(b, 211, "# of children reached with PSS", {1: 350})

    girls_and_boys = w.master("Children reached with PSS", section=w.CP)
    for pd, source_id in ((a, 111), (a, 112), (b, 211)):
        w.link(girls_and_boys, pd, source_id)
    fig = w.figure(girls_and_boys)
    assert (fig.value, fig.at_least) == (850.0, 500.0)  # girls and boys of one PD add up: its figure is 500
    assert all(s.counted for s in fig.subs)

    nations = w.master("Children in recreational activities", section=w.CP)
    w.link(nations, a, 115)
    w.link(nations, a, 116)
    assert w.figure(nations).value == 160.0  # two nationalities never count the same child

    overlapping = w.master("Children in PSS sessions", section=w.CP)
    w.link(overlapping, a, 113)
    w.link(overlapping, a, 114)  # a disability subset: may count the same children
    fig = w.figure(overlapping)
    found = w.subs(fig)
    assert fig.value == fig.at_least == 450.0  # only the PD's largest
    assert found["s113"].counted and not found["s114"].counted
    assert (
        found["s114"].reason
        == masters.SAME_PD
        == ("Same programme document: may count the same people; not added")
    )
    assert (fig.counted_pds, fig.pds) == (1, 1)  # the PD is counted, once
    overlapping.pd_parts_add_up = True  # the section says they count different people
    fig = w.figure(overlapping)
    assert fig.value == 490.0 and all(s.counted for s in fig.subs)


def test_the_disjoint_rule_reads_the_title_tags(pds):
    a = pds["A"]
    titles = {
        1: "# of girls enrolled",
        2: "# of boys enrolled",
        3: "# of women trained",
        4: "# of female caregivers trained",
        5: "# of Syrian children enrolled",
        6: "# of Palestinian children enrolled",
        7: "# of refugee children enrolled",
        8: "# of girls with disabilities enrolled",
    }
    for source_id, title in titles.items():
        w.indicator(a, source_id, title)
    found = {int(r.key): r for r in w.rows()}
    assert masters.disjoint(found[1], found[2]) and masters.disjoint(found[3], found[2])
    assert not masters.disjoint(found[3], found[4])  # women and female: the same sex
    assert masters.disjoint(found[5], found[6])
    assert not masters.disjoint(found[5], found[7])  # refugees may be Syrian
    assert not masters.disjoint(found[1], found[8])  # a disability subset of the girls


def test_a_value_too_large_to_be_true_is_set_aside(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    w.indicator(a, 121, "# of children vaccinated", target=(10, 1))
    quarterly(a, 121, "# of children vaccinated", {1: 5000})  # 500 times its target
    w.indicator(b, 221, "# of children vaccinated", target=(500, 1))
    quarterly(b, 221, "# of children vaccinated", {1: 300})
    m = w.master("Children vaccinated")
    w.link(m, a, 121)
    w.link(m, b, 221)
    fig = w.figure(m)
    found = w.subs(fig)
    assert fig.value == fig.at_least == 300.0
    assert not found["s121"].counted and found["s121"].value == 5000.0  # still shown on its row
    assert found["s121"].reason == "Set aside: more than 20 times its target"
    assert fig.coverage == "counted 1 of 2 PDs"

    # above the country's child population: only for a sub that counts children
    w.indicator(c, 321, "# of children reached with hygiene kits")
    quarterly(c, 321, "# of children reached with hygiene kits", {1: 1500})
    w.indicator(b, 222, "# of teachers trained on hygiene")
    quarterly(b, 222, "# of teachers trained on hygiene", {1: 1500})
    kits = w.master("Hygiene")
    w.link(kits, c, 321)
    w.link(kits, b, 222)
    assert w.figure(kits).value == 3000.0
    fig = w.figure(kits, national=1000, is_children=lambda row: row.tags["age_group"] == "Children")
    found = w.subs(fig)
    assert fig.value == 1500.0 and found["s222"].counted
    assert found["s321"].reason == "Set aside: above the child population of Lebanon"


# ------------------------------------------------------------------------------------- percentage and ratio
@pytest.mark.parametrize(
    "first, second, expected, average",
    [((40, 100), (10, 50), 50 / 150 * 100, 30.0), ((90, 100), (1, 10), 91 / 110 * 100, 50.0)],
    ids=["33.3 % not 30 %", "82.7 % not 50 %"],
)
def test_a_percentage_is_the_numerators_over_the_denominators(pds, first, second, expected, average):
    a, b = pds["A"], pds["B"]
    title = "% of children passing the end-of-year exam"
    w.indicator(a, 131, title, display_type="percentage", target=(80, 100))
    quarterly(a, 131, title, {1: w.pct(*first)})
    w.indicator(b, 231, title, display_type="percentage", target=(60, 100))
    quarterly(b, 231, title, {1: w.pct(*second)})
    m = w.master("Children passing the exam", "percent", target=50)
    w.link(m, a, 131, unit_class="percentage")
    w.link(m, b, 231, unit_class="percentage")

    fig = w.figure(m)
    assert fig.value == pytest.approx(expected)
    assert sum(s.value for s in fig.subs) / 2 == pytest.approx(average)  # what averaging would have said
    assert (fig.numerator, fig.denominator) == (first[0] + second[0], first[1] + second[1])
    assert all(q == pytest.approx(expected) for q in fig.quarters.values())
    assert fig.at_least is None and not fig.prorate  # a level: the whole target
    assert fig.achieved == pytest.approx(expected * 100 / 50)
    assert fig.pd_target == pytest.approx(70.0)  # (80 + 60) / (100 + 100), for reference only


def test_a_percentage_reported_without_its_parts_is_left_out_and_said_so(pds):
    a, b = pds["A"], pds["B"]
    title = "% of children with a case plan"
    w.indicator(a, 133, title, display_type="percentage")
    quarterly(a, 133, title, {1: w.pct(30, 60)})
    w.indicator(b, 233, title, display_type="percentage")
    quarterly(b, 233, title, {1: {"c": 45}})  # PRP sent the percentage only
    m = w.master("Children with a case plan", "percent")
    w.link(m, a, 133, unit_class="percentage")
    w.link(m, b, 233, unit_class="percentage")

    fig = w.figure(m)
    found = w.subs(fig)
    assert fig.value == 50.0  # never (50 + 45) / 2
    assert found["s233"].value == 45.0 and not found["s233"].counted
    assert found["s233"].reason == masters.NO_PARTS == "Reported without numerator and denominator"
    assert (fig.counted_pds, fig.pds, fig.coverage) == (1, 2, "counted 1 of 2 PDs")
    assert fig.how == "The numerators added up over the denominators added up, × 100 (counted 1 of 2 PDs)."


def test_numerators_and_denominators_pair_within_each_programme_document(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    passed, sat = "# of children who passed the exam", "# of children who sat the exam"
    for pd, source_id, title, value in (
        (a, 141, passed, 30),
        (a, 142, sat, 60),
        (b, 241, passed, 20),
        (c, 341, sat, 10),
    ):
        w.indicator(pd, source_id, title)
        quarterly(pd, source_id, title, {1: value})
    w.indicator(b, 242, sat)  # linked, never reported
    m = w.master("Exam pass rate", "percent")
    for pd, source_id, role in (
        (a, 141, "numerator"),
        (a, 142, "denominator"),
        (b, 241, "numerator"),
        (b, 242, "denominator"),
        (c, 341, "denominator"),
    ):
        w.link(m, pd, source_id, role=role)

    fig = w.figure(m)
    found = w.subs(fig)
    assert fig.value == 50.0 and (fig.numerator, fig.denominator) == (30.0, 60.0)
    assert found["s141"].counted and found["s142"].counted
    assert found["s241"].reason == masters.NUMERATOR_ALONE == "Numerator without its denominator"
    assert found["s242"].reason == masters.NO_VALUE and found["s242"].eligible
    assert found["s341"].reason == masters.DENOMINATOR_ALONE
    assert (fig.counted_pds, fig.pds) == (1, 3)
    assert (fig.reporting, fig.eligible) == (4, 5)


def test_denominators_adding_up_to_zero_give_no_value(pds):
    a = pds["A"]
    for source_id, title in (
        (151, "# of children who passed the test"),
        (152, "# of children who took the test"),
    ):
        w.indicator(a, source_id, title)
        quarterly(a, source_id, title, {1: 0})
    m = w.master("Test pass rate", "percent", target=50)
    w.link(m, a, 151, role="numerator")
    w.link(m, a, 152, role="denominator")
    fig = w.figure(m)
    assert all(s.counted for s in fig.subs)
    assert fig.value is None and fig.quarters == dict.fromkeys(masters.QUARTERS)
    assert (fig.status, fig.status_label) == ("not_reported", "Not reported")


def test_totals_and_numerators_mix_in_one_master_and_the_quarters_follow_the_same_rule(pds):
    a, b = pds["A"], pds["B"]
    regular = "% of children attending regularly"
    w.indicator(a, 161, regular, display_type="percentage")
    quarterly(a, 161, regular, {1: w.pct(20, 40), 2: w.pct(10, 60)})  # 30 %
    for source_id, title, values in (
        (261, "# of children attending regularly", {1: 30, 2: 30}),
        (262, "# of children enrolled", {1: 60, 2: 40}),
    ):
        w.indicator(b, source_id, title)
        quarterly(b, source_id, title, values)
    m = w.master("Children attending regularly", "percent")
    w.link(m, a, 161, unit_class="percentage")
    w.link(m, b, 261, role="numerator")
    w.link(m, b, 262, role="denominator")

    fig = w.figure(m)
    assert fig.value == pytest.approx(90 / 200 * 100)  # (30 + 60) / (100 + 100)
    assert fig.quarters[1] == pytest.approx(50 / 100 * 100)  # (20 + 30) / (40 + 60)
    assert all(fig.quarters[q] == pytest.approx(45.0) for q in (2, 3, 4))
    assert fig.coverage == "counted 2 of 2 PDs"


def test_a_ratio_is_never_multiplied_by_100_and_never_mixes_with_a_percentage(pds):
    a, b = pds["A"], pds["B"]
    title = "Ratio of girls to boys enrolled"
    w.indicator(a, 171, title, display_type="ratio")
    quarterly(a, 171, title, {1: w.pct(45, 50)})
    w.indicator(b, 271, title, display_type="ratio")
    quarterly(b, 271, title, {1: w.pct(55, 50)})
    m = w.master("Gender parity in enrolment", "ratio")
    w.link(m, a, 171, unit_class="ratio")
    w.link(m, b, 271, unit_class="ratio")
    fig = w.figure(m)
    assert fig.value == 1.0 and (fig.numerator, fig.denominator) == (100.0, 100.0)

    m.method = "percent"  # a percentage master never takes a ratio as its total
    fig = w.figure(m)
    assert fig.value is None and {s.reason for s in fig.subs} == {masters.WRONG_TYPE}


# ------------------------------------------------------------------------------- average, maximum, minimum
def test_average_maximum_and_minimum_take_the_programme_documents_figures_of_one_type(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    rehabilitated = "# of schools rehabilitated"
    w.indicator(a, 181, rehabilitated, target=(12, 1))
    quarterly(a, 181, rehabilitated, {1: 4, 2: 6})  # 10
    w.indicator(b, 281, rehabilitated)
    quarterly(b, 281, rehabilitated, {1: 30})
    w.indicator(c, 381, rehabilitated)
    quarterly(c, 381, rehabilitated, {2: 20})
    w.indicator(c, 382, "% of schools rehabilitated", display_type="percentage")
    quarterly(c, 382, "% of schools rehabilitated", {1: w.pct(1, 4)})
    m = w.master("Schools rehabilitated", "average", target=25)
    for pd, source_id in ((a, 181), (b, 281), (c, 381)):
        w.link(m, pd, source_id)
    w.link(m, c, 382, unit_class="percentage")  # another type: never in an average of numbers

    expected = {
        "average": (20.0, {1: 17.0, 2: 20.0}),
        "maximum": (30.0, {1: 30.0, 2: 30.0}),
        "minimum": (10.0, {1: 4.0, 2: 10.0}),
    }
    for method, (value, quarters) in expected.items():
        m.method = method
        fig = w.figure(m)
        assert fig.value == value, method
        assert {q: fig.quarters[q] for q in (1, 2)} == quarters, method
        assert fig.quarters[4] == value and not fig.prorate and fig.pd_target is None
        assert w.subs(fig)["s382"].reason == masters.WRONG_TYPE and not w.subs(fig)["s382"].counted
        assert fig.coverage == "counted 3 of 3 PDs"
    assert masters.METHODS["average"]["help"] == "The unweighted average of the programme documents' figures."


# --------------------------------------------------------------------------------------- which links count
def test_only_confirmed_links_in_a_counting_role_count(pds):
    a, b = pds["A"], pds["B"]
    ended = w.programme("D", 4, start=day(1, 1, 2024), end=day(12, 31, 2025))
    for pd, source_id, title, value, section in (
        (a, 101, ENROLLED, 100, w.EDUCATION),
        (a, 105, "# of girls enrolled in NFE", 70, w.EDUCATION),
        (b, 201, ENROLLED, 50, w.EDUCATION),
        (b, 202, "# of adolescents enrolled in NFE", 40, w.EDUCATION),
        (b, 203, "# of youth enrolled in NFE", 30, w.EDUCATION),
        (pds["C"], 204, "# of children enrolled in catch-up classes", 20, w.CP),
        (b, 205, "% of children enrolled in NFE", None, w.EDUCATION),
    ):
        w.indicator(
            pd, source_id, title, section=section, display_type="percentage" if source_id == 205 else "number"
        )
        if value is not None:
            quarterly(pd, source_id, title, {1: value})
    w.indicator(ended, 401, ENROLLED)
    m = w.master("Children enrolled", target=500)
    w.link(m, a, 101)
    w.link(m, a, 105, role="shown")  # a breakdown of 101
    w.link(m, b, 201)
    w.link(m, b, 202, status="suggested")
    w.link(m, b, 203, status="rejected")
    w.link(m, pds["C"], 204)  # moved to another section in eTools: still read
    w.link(m, b, 205, unit_class="number")  # a number when confirmed, a percentage now
    w.link(m, a, 999)  # its PD runs, the indicator is gone
    w.link(m, ended, 401)  # its PD does not run in the year

    fig = w.figure(m)
    found = w.subs(fig)
    assert set(found) == {
        "s101",
        "s105",
        "s201",
        "s202",
        "s204",
        "s205",
        "s999",
    }  # rejected and not running: not listed
    assert fig.value == 170.0  # 100 + 50 + 20
    assert {ident for ident, s in found.items() if s.counted} == {"s101", "s201", "s204"}
    assert found["s105"].reason == "Shown only" and found["s105"].value == 70.0
    assert found["s202"].reason == "Suggested, not confirmed"
    assert found["s204"].note == "Now in section Child Protection" and found["s204"].reason == ""
    assert found["s205"].reason == "Type changed in eTools" and found["s205"].unit_class == "percentage"
    assert found["s999"].reason == "No longer in eTools" and found["s999"].row is None
    assert found["s105"].counts_as == "Shown only" and found["s101"].counts_as == "In the total"
    assert (fig.reporting, fig.eligible) == (3, 3)


# ------------------------------------------------------------------------------------------- reading rules
def test_a_monthly_only_sub_next_to_quarterly_ones_is_never_added_twice(pds):
    a, b = pds["A"], pds["B"]
    outreach, hotline = "# of children reached through outreach", "# of children reached through the hotline"
    w.indicator(a, 191, outreach, is_high_frequency=True)
    quarterly(a, 191, outreach, {1: 100})
    w.report(a, 191, outreach, "HR1", day(1, 31), {"Akkar": 500}, kind="HR")  # the same work, monthly
    w.indicator(b, 291, hotline, is_high_frequency=True)
    w.report(b, 291, hotline, "HR1", day(1, 31), {"Akkar": 10}, kind="HR")
    w.report(b, 291, hotline, "HR4", day(4, 30), {"Akkar": 20}, kind="HR")
    m = w.master("Children reached remotely")
    w.link(m, a, 191)
    w.link(m, b, 291)
    fig = w.figure(m)
    found = w.subs(fig)
    assert (found["s191"].row.report_type, found["s291"].row.report_type) == ("QPR", "HR")
    assert fig.value == 130.0
    assert fig.quarters == {1: 110.0, 2: 130.0, 3: 130.0, 4: 130.0}  # April's report in the second quarter


def test_a_report_total_repeated_on_every_location_row_is_read_once(pds):
    c = pds["C"]
    title = "# of children reached with summer schools"
    w.indicator(c, 391, title, places=("Akkar", "Bekaa", "Tyre"))
    w.report(
        c, 391, title, "QPR1", day(3, 31), places=("Akkar", "Bekaa", "Tyre"), achievement_in_period="120"
    )
    m = w.master("Children in summer schools")
    w.link(m, c, 391)
    fig = w.figure(m)
    assert fig.value == fig.subs[0].row.reached_in_year == 120.0  # never 360


# ---------------------------------------------------------------------------------------------- the status
def test_the_status_is_measured_against_the_typed_target_alone(pds):
    a = pds["A"]
    w.indicator(a, 101, ENROLLED, target=(1600, 1))
    quarterly(a, 101, ENROLLED, {1: 400, 2: 300})
    m = w.master("Children enrolled")
    w.link(m, a, 101)

    fig = w.figure(m)  # no target typed for the year: the PD's own target never stands in
    assert (fig.value, fig.pd_target) == (700.0, 1600.0)
    assert (fig.status, fig.status_label, fig.achieved) == ("no_target", "No target", None)
    other_year = w.master("Children enrolled last year", target=100, year=2025)
    assert w.figure(other_year).status == "no_target"  # a target of another year is not this year's

    m.years.create(year=w.YEAR, target=500)
    fig = w.figure(m)
    assert (fig.status, fig.status_label) == ("over_target", "Over target")
    assert fig.achieved == 140.0 and fig.pd_target == 1600.0


def test_the_status_date_is_the_latest_counted_report_at_most_the_year_end_and_today(pds, site_today):
    a, b = pds["A"], pds["B"]
    w.indicator(a, 101, ENROLLED)
    quarterly(a, 101, ENROLLED, {1: 200, 2: 250})
    m = w.master("Children enrolled", target=1000)
    w.link(m, a, 101)
    fig = w.figure(m)
    # 45 % on 30 June, about half the year gone: on track (measured today, 15 October, it would be off)
    assert fig.as_of == day(6, 30) and fig.status == "on_track"
    assert tracking(450, 1000, w.YEAR, w.TODAY).status == "off_track"

    # last year read now: its reports after 31 December do not move its date
    w.indicator(b, 201, ENROLLED, section=w.EDUCATION)
    b.start = day(7, 1, 2025)
    b.save()
    for end, value in ((day(12, 31, 2025), 100), (day(3, 31), 50)):
        w.report(b, 201, ENROLLED, f"QPR-{end:%Y%m}", end, {"Akkar": value})
    last_year = w.master("Children enrolled in 2025", target=100, year=2025)
    w.link(last_year, b, 201)
    fig = w.figure(last_year, year=2025)
    assert fig.value == 100.0 and fig.as_of == day(12, 31, 2025)

    # a report whose period ends after today: the status is as of today
    w.indicator(pds["C"], 301, ENROLLED)
    w.report(pds["C"], 301, ENROLLED, "QPR4", day(12, 31), {"Akkar": 10})
    early = w.master("Children enrolled early", target=10)
    w.link(early, pds["C"], 301)
    assert w.figure(early).as_of == w.TODAY


def test_a_master_appears_when_active_with_the_year_or_a_running_confirmed_link(pds):
    a = pds["A"]
    w.indicator(a, 101, ENROLLED)
    with_year = w.master("With its year", target=10)
    switched_off = w.master("Switched off this year")
    switched_off.years.create(year=w.YEAR, target=10, is_active=False)
    linked = w.master("Linked")
    w.link(linked, a, 101)
    suggested_only = w.master("Suggested only")
    w.link(suggested_only, a, 102, status="suggested", role="shown")
    proposed = w.master("Proposed", status="proposed", target=10)
    running = {a.id}
    assert masters.appears(with_year, w.YEAR, running)
    assert not masters.appears(switched_off, w.YEAR, running)
    assert masters.appears(linked, w.YEAR, running) and not masters.appears(linked, w.YEAR, set())
    assert not masters.appears(suggested_only, w.YEAR, running)
    assert not masters.appears(proposed, w.YEAR, running)


# ---------------------------------------------------------------------------------------- count agreement
def test_each_subs_figure_is_program_performances(pds, settings):
    """The master tab and Program Performance read the same stored rows: each sub's Reached in the year and
    quarters are the indicator's own, for the same year."""
    settings.DEBUG = False
    a, b = pds["A"], pds["B"]
    w.indicator(a, 101, ENROLLED, places=("Akkar", "Bekaa"))
    w.report(a, 101, ENROLLED, "QPR1", day(3, 31), {"Akkar": 100, "Bekaa": 50})
    title = "% of children passing the end-of-year exam"
    w.indicator(b, 231, title, display_type="percentage")
    quarterly(b, 231, title, {1: w.pct(10, 50), 2: w.pct(30, 50)})
    w.indicator(b, 201, ENROLLED)
    quarterly(b, 201, ENROLLED, {2: 80})
    numbers = w.master("Children enrolled")
    w.link(numbers, a, 101)
    w.link(numbers, b, 201)
    share = w.master("Children passing", "percent")
    w.link(share, b, 231, unit_class="percentage")

    tab = monitoring.stored_indicators(
        monitoring.Filters(sections=[w.EDUCATION], year=w.YEAR, scope="year"), w.TODAY
    )
    performance = monitoring.stored_indicators(
        monitoring.Filters.from_params({"year": str(w.YEAR)}, today=w.TODAY, default_year=False), w.TODAY
    )
    by_key = {masters.key_of(r): r for r in performance}
    for m in (numbers, share):
        for sub in w.figure(m, tab).subs:
            row = by_key[(sub.link.pd_id, sub.link.ident)]
            assert sub.value == row.reached_in_year
            assert sub.quarters == {q: row.to_quarter[q][0] for q in masters.QUARTERS}
            assert (sub.numerator, sub.denominator) == row.in_year_item[1:]
