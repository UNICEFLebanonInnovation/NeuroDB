"""The HACT programmatic visit plan per programme document (``neurodb.datamart.hact_plan``): what counts as
a completed visit, the year and quarter rules, the remarks, the headline (the team's e-mail rebuilt with
synthetic partners and references), the eTools partner minimum beside the PD figures, the section
comments and who may write them, and that no staff name ever enters the plain rows.

Synthetic data only: "Partner A", references like TEST/PCA2026001/PD2026001, focal point "Focal Person
Test"."""

import datetime
import itertools
import json
from decimal import Decimal

import pytest
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.models import Group
from django.http import QueryDict
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER
from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, fm, hact_plan
from neurodb.datamart import models as dm
from neurodb.donors.models import DonorAccount
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.watch.models import SectionMatch

pytestmark = pytest.mark.django_db

D = datetime.date
YEAR = 2026
TODAY = D(2026, 10, 13)
FOCAL = "Focal Person Test"
# the defaults of the helpers below: a PD running all year, a visit in May, a TPM activity in June, a trip
JAN_1, DEC_31, MAY_10, JUNE_1, APRIL_2 = (
    D(2026, 1, 1),
    D(2026, 12, 31),
    D(2026, 5, 10),
    D(2026, 6, 1),
    D(2026, 4, 2),
)
_ids = itertools.count(1)


# ---------------------------------------------------------------------------------- the world
def partner(name: str, rating: str = "Low") -> PartnerOrganization:
    n = next(_ids)
    return PartnerOrganization.objects.create(
        etl_id=f"p{n}",
        name=name,
        partner_type="Civil Society Organization",
        vendor_number=f"V{n:05d}",
        rating=rating,
    )


def programme(
    owner,
    *,
    status="active",
    start=JAN_1,
    end=DEC_31,
    sections=("Education",),
    document_type="PD",
    budget="120,000.50",
    focal=(FOCAL, "focal.person@example.org"),
) -> PCA:
    n = next(_ids)
    return PCA.objects.create(
        etl_id=f"pd{n}",
        partner=owner,
        partner_name=owner.name if owner else "",
        number=f"TEST/PCA2026{n:03d}/PD2026{n:03d}",
        title=f"Test programme {n}",
        status=status,
        document_type=document_type,
        start=start,
        end=end,
        section_names=list(sections),
        total_budget=budget,
        budget_currency="USD",
        unicef_focal_points=list(focal),
    )


def plan(pd: PCA, quarters=(1, 1, 1, 1), year: int = YEAR) -> dm.PlannedVisits:
    q1, q2, q3, q4 = quarters
    return dm.PlannedVisits.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner_id=pd.partner_id,
        pd_reference_number=pd.number,
        year=year,
        q1=q1,
        q2=q2,
        q3=q3,
        q4=q4,
    )


def fm_row(pd, activity, *, end=MAY_10, status="completed", programmatic=True, partner_row=None, **extra):
    """One finding row of field monitoring visit ``activity`` (an eTools activity id) about ``pd``."""
    owner = partner_row or (pd.partner if pd else None)
    return dm.MonitoringFinding(
        datamart_id=next(_ids),
        partner=owner,
        vendor_number=owner.vendor_number if owner else "",
        entity=pd.number if pd else (owner.name if owner else ""),
        entity_type="PD/SSFA" if pd else "Partner",
        intervention=pd,
        monitoring_activity=f"FM-TEST-{activity}",
        monitoring_activity_id=activity,
        status=status,
        is_programmatic_visit=programmatic,
        start_date=end - datetime.timedelta(days=1) if end else None,
        end_date=end,
        **extra,
    )


def fm_visit(pd, **kwargs) -> int:
    """A one-row field monitoring visit about ``pd``; returns its activity id."""
    activity = next(_ids)
    fm_row(pd, activity, **kwargs).save()
    return activity


def tpm(pd, visit_ref, *, status="unicef_approved", programmatic=True, date=JUNE_1, task="", visit=None):
    return dm.TPMActivity.objects.create(
        datamart_id=next(_ids),
        partner=pd.partner,
        intervention=pd,
        visit=visit,
        visit_reference_number=visit_ref,
        task_reference_number=task,
        visit_status=status,
        is_programmatic_visit=programmatic,
        pd_reference_number=pd.number,
        date=date,
    )


def trip(pd, ref, *, date=APRIL_2, travel_type="Programmatic Visit"):
    return dm.ProgrammaticVisit.objects.create(
        datamart_id=next(_ids),
        partner=pd.partner,
        intervention=pd,
        travel_reference_number=ref,
        travel_type=travel_type,
        date=date,
        partnership_number=pd.number,
    )


def hact_year(owner, *, required, completed, risk="Medium", year=YEAR, modified=None):
    return dm.PartnerHACTYear.objects.create(
        datamart_id=next(_ids),
        partner=owner,
        partner_name=owner.name,
        vendor_number=owner.vendor_number,
        year=year,
        risk_rating=risk,
        pv_required=required,
        pv_completed=completed,
        last_modify_date=modified,
    )


def row_of(rows, pd) -> dict:
    found = [r for r in rows if r["pd_id"] == pd.pk]
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------------------------- counting
def test_required_is_the_quarterly_plan_and_completed_counts_approved_programmatic_visits_only():
    a = partner("Partner A")
    pd = programme(a)
    plan(pd, (1, 2, 0, 3))
    plan(programme(a), (1, 1, 1, 1), year=2025)  # another year's plan: never read for 2026
    fm_visit(pd, end=D(2026, 12, 31))  # programmatic, completed, ends on 31 Dec
    fm_visit(pd, status="Completed", end=D(2026, 2, 1))  # any case
    fm_visit(pd, status="submitted")  # report awaiting approval: not counted
    fm_visit(pd, programmatic=False)  # completed, not marked programmatic: not counted
    fm_visit(pd, end=D(2027, 1, 1))  # ends on 1 January of the next year: next year's
    fm_visit(pd, end=D(2025, 12, 31))  # ends on 31 December of the year before
    fm_visit(pd, status="data_collection")  # under way: nothing
    tpm(pd, "TPM/2026/1")  # programmatic, approved: counted
    tpm(pd, "TPM/2026/2", status="tpm_reported")  # awaiting approval
    tpm(pd, "TPM/2026/3", status="draft")  # neither counted nor a TPM visit of the PD
    tpm(pd, "TPM/2026/4", programmatic=False)  # approved but not programmatic: not counted
    tpm(pd, "TPM/2025/9", date=D(2025, 12, 31))  # last year's
    trip(pd, "TRIP-1")
    trip(pd, "TRIP-1")  # a second activity of the same trip
    trip(pd, "TRIP-2", travel_type="Spot check")  # not a programmatic trip
    trip(pd, "TRIP-3", date=D(2025, 6, 1))  # last year's

    row = row_of(hact_plan.build(YEAR, TODAY), pd)

    assert (row["q1"], row["q2"], row["q3"], row["q4"], row["required"]) == (1, 2, 0, 3, 6)
    assert (row["completed_fm"], row["completed_tpm"], row["completed"]) == (2, 1, 3)
    assert row["awaiting"] == 2  # the FM report submitted and the TPM report
    assert row["fm_not_programmatic"] == 1
    assert row["staff_trips"] == 1  # shown, never counted
    assert row["tpm"] is True  # approved, reported and the non-programmatic one; the draft alone would not
    assert (row["counted"], row["still_needed"]) == (3, 3)
    next_year = row_of(hact_plan.build(2027, TODAY), pd)
    assert next_year["completed_fm"] == 1 and next_year["required"] == 0  # the 1 January visit


def test_a_visit_counts_once_per_programme_document_and_once_per_source():
    a = partner("Partner A")
    first, second = programme(a), programme(a)
    plan(first), plan(second)
    shared = next(_ids)
    dm.MonitoringFinding.objects.bulk_create(
        [
            fm_row(first, shared),  # two rows of one visit about the same PD: once
            fm_row(first, shared),
            fm_row(second, shared),  # the same visit about another PD: once for that PD too
            fm_row(None, shared, partner_row=a),  # its partner row names no PD
        ]
    )
    tpm(first, "TPM/2026/7", task="TPM/2026/7/1")  # two activities of one TPM visit: once
    tpm(first, "TPM/2026/7", task="TPM/2026/7/2")
    tpm(first, "", task="TASK-ONLY")  # no visit reference: its task reference
    tpm(first, "", task="")  # neither: the row itself
    rows = hact_plan.build(YEAR, TODAY)
    assert row_of(rows, first)["completed_fm"] == 1
    assert row_of(rows, second)["completed_fm"] == 1
    assert row_of(rows, first)["completed_tpm"] == 3
    assert fm.visits_by_pd_for_hact(YEAR) == {
        first.pk: {"completed": 1, "submitted": 0, "not_programmatic": 0},
        second.pk: {"completed": 1, "submitted": 0, "not_programmatic": 0},
    }


def test_the_tpm_status_is_the_visits_else_the_linked_tpm_visits():
    a = partner("Partner A")
    pd = programme(a)
    plan(pd)
    approved = dm.TPMVisit.objects.create(
        datamart_id=next(_ids), reference_number="TPM/2026/20", status="unicef_approved"
    )
    reported = dm.TPMVisit.objects.create(
        datamart_id=next(_ids), reference_number="TPM/2026/21", status="tpm_reported"
    )
    tpm(pd, "TPM/2026/20", status="", visit=approved)
    tpm(pd, "TPM/2026/21", status=" ", visit=reported)
    tpm(pd, "TPM/2026/22", status="Unicef_Approved", visit=reported)  # its own status wins, any case
    row = row_of(hact_plan.build(YEAR, TODAY), pd)
    assert (row["completed_tpm"], row["awaiting"]) == (2, 1)


def test_counted_is_capped_by_the_requirement_and_still_needed_is_never_negative():
    a = partner("Partner A")
    over, under = programme(a), programme(a)
    plan(over, (1, 1, 1, 0))
    plan(under, (1, 1, 1, 0))
    for _ in range(4):
        fm_visit(over)
    fm_visit(under)
    rows = hact_plan.build(YEAR, TODAY)
    assert {k: row_of(rows, over)[k] for k in ("required", "completed", "counted", "still_needed")} == {
        "required": 3,
        "completed": 4,
        "counted": 3,
        "still_needed": 0,  # never -1, as in the workbook's two rows
    }
    assert (row_of(rows, under)["counted"], row_of(rows, under)["still_needed"]) == (1, 2)


# ---------------------------------------------------------------------------------- remarks
def test_remarks_and_the_not_visited_flag():
    a = partner("Partner A")
    no_plan = programme(a)
    fm_visit(no_plan)
    done = programme(a)
    plan(done, (1, 0, 0, 0))
    fm_visit(done)
    ended_status = programme(a, status="ended", end=D(2026, 3, 31))
    plan(ended_status, (1, 1, 0, 0))
    closed = programme(a, status="Closed", end=D(2026, 12, 31))  # an ended status, any case
    plan(closed, (1, 0, 0, 0))
    past_end = programme(a, status="active", end=D(2026, 4, 30))  # active, but its end date passed
    plan(past_end, (0, 0, 1, 1))
    fm_visit(past_end)
    needed = programme(a, end=D(2026, 12, 31))
    plan(needed, (0, 0, 1, 1))
    rows = hact_plan.build(YEAR, TODAY)
    remarks = {
        pd.pk: (row_of(rows, pd)["remark"], row_of(rows, pd)["zero"])
        for pd in (no_plan, done, ended_status, closed, past_end, needed)
    }
    assert remarks == {
        no_plan.pk: ("no_plan", False),
        done.pk: ("completed", False),
        ended_status.pk: ("ended", True),
        closed.pk: ("ended", True),
        past_end.pk: ("ended", False),
        needed.pk: ("needed", True),
    }
    assert (
        row_of(rows, no_plan)["completed"] == 1
    )  # the page says "1 completed visit" under "No visit planned"
    assert row_of(rows, closed)["status"] == "closed"
    assert set(hact_plan.REMARKS) == set(hact_plan.REMARK_ORDER)


# ---------------------------------------------------------------------------------- years and quarters
@pytest.mark.parametrize(
    ("year", "today", "quarter", "due"),
    [
        (2026, D(2026, 5, 10), 2, 1),  # Q2: q1 + q2 = 2 planned, 1 completed
        (2026, D(2026, 11, 2), 4, 4),  # Q4: the whole year's 5, 1 completed
        (2026, D(2027, 2, 1), 4, 4),  # a past year: the whole year
        (2026, D(2025, 11, 2), 0, 0),  # a future year: nothing due yet
    ],
)
def test_due_by_the_end_of_the_quarter(year, today, quarter, due):
    a = partner("Partner A")
    pd = programme(a, end=D(2027, 12, 31))
    plan(pd, (1, 1, 2, 1))
    fm_visit(pd, end=D(2026, 3, 1))
    row = row_of(hact_plan.build(year, today), pd)
    assert (row["due_quarter"], row["due_now"]) == (quarter, due)
    assert hact_plan.due_quarter(year, today) == quarter


def test_the_programme_documents_of_the_year():
    a = partner("Partner A")
    running = programme(a)
    draft = programme(a, status="draft")
    plan(draft)
    cancelled = programme(a, status="Cancelled")
    plan(cancelled)
    ended_last_year = programme(a, status="ended", start=D(2024, 1, 1), end=D(2025, 12, 31))
    plan(ended_last_year)  # planned this year: in
    visited_only = programme(a, start=D(2023, 1, 1), end=D(2024, 6, 30))
    fm_visit(visited_only)  # a counted visit this year: in
    tpm_only = programme(a, start=D(2023, 1, 1), end=D(2024, 6, 30))
    tpm(tpm_only, "TPM/2026/30")
    no_dates = programme(a, start=None, end=None)  # no dates and no plan: out
    open_ended = programme(a, start=D(2025, 3, 1), end=None)  # no end: runs
    future = programme(a, start=D(2027, 1, 1), end=D(2027, 12, 31))  # starts next year: out
    old = programme(a, start=D(2020, 1, 1), end=D(2025, 12, 31))  # ended last year, nothing this year: out
    ids = {r["pd_id"] for r in hact_plan.build(YEAR, TODAY)}
    assert ids == {running.pk, ended_last_year.pk, visited_only.pk, tpm_only.pk, open_ended.pk}
    assert not ids & {draft.pk, cancelled.pk, no_dates.pk, future.pk, old.pk}
    assert hact_plan.build(YEAR, TODAY, pd_ids=[running.pk, draft.pk])[0]["pd_id"] == running.pk


def test_rows_are_sorted_by_partner_then_reference_and_carry_the_pd_fields():
    b, a = partner("partner B", rating="High"), partner("Partner A", rating="Low")
    second, first = (
        programme(b),
        programme(a, document_type="SPD", sections=("Education ", "child  protection")),
    )
    PCA.objects.filter(pk=second.pk).update(total_budget="n/a", budget_currency=None)
    rows = hact_plan.build(YEAR, TODAY)
    assert [r["pd_id"] for r in rows] == [first.pk, second.pk]
    row = rows[0]
    assert (
        row["reference"] == first.number
        and row["partner"] == "Partner A"
        and row["vendor_number"] == a.vendor_number
    )
    assert row["document_type"] == "SPD" and row["sections"] == ["Education", "child protection"]
    assert row["total_budget"] == Decimal("120000.50") and row["currency"] == "USD"
    assert rows[1]["total_budget"] is None and rows[1]["currency"] == ""
    assert (row["risk_rating"], row["risk_source"]) == ("Low", "partner")
    assert row["focal_points_count"] == 1  # the e-mail address is never one
    assert row["plan_row"] is False and row["remark"] == "no_plan"


def test_the_risk_rating_is_the_partners_latest_hact_year_else_its_rating():
    a = partner("Partner A", rating="Low")
    pd = programme(a)
    hact_year(
        a, required=2, completed=0, risk="High", modified=timezone.make_aware(datetime.datetime(2026, 3, 1))
    )
    hact_year(
        a,
        required=3,
        completed=1,
        risk="Significant",
        modified=timezone.make_aware(datetime.datetime(2026, 9, 1)),
    )
    hact_year(a, required=9, completed=9, risk="Moderate", year=2025)
    row = row_of(hact_plan.build(YEAR, TODAY), pd)
    assert (row["risk_rating"], row["risk_source"]) == ("Significant", "hact")
    assert (row["etools_pv_required"], row["etools_pv_completed"]) == (3, 1)


# ---------------------------------------------------------------------------------- the e-mail, rebuilt
def _visits(pd, n: int, *, tpm_visits: bool = False) -> None:
    """``n`` completed programmatic visits to ``pd`` (field monitoring, or TPM)."""
    for _ in range(n):
        if tpm_visits:
            tpm(pd, f"TPM/2026/{next(_ids)}")
        else:
            fm_visit(pd)


def _quarters(required: int) -> tuple[int, int, int, int]:
    return {1: (1, 0, 0, 0), 2: (0, 0, 1, 1), 3: (1, 1, 1, 0), 4: (1, 1, 1, 1)}[required]


@pytest.fixture
def email_world():
    """79 programme documents of 45 partners, shaped like the team's October workbook (counts only):

    * 12 PDs planned and never visited, over 11 partners (Z01 has two); 4 of those partners have no
      visit on any PD (Z01-Z04), the other 7 have a PD with more visits than planned (no netting);
      required 2 (9 PDs), 3 (2), 1 (1); 3 ended, 1 active past its end date, 1 awaiting signature;
    * 14 PDs visited but short, over 13 partners (G01 has two): completed 1 or 2, gap 1 (6) or 2 (8);
      2 of them active past their end date, 1 awaiting signature;
    * 51 PDs whose plan is met (some with a surplus);
    * 2 ended PDs eTools plans no visit for this year (one visited in January).
    """
    zero_partners = [partner(f"Partner Z{n:02d}") for n in range(1, 12)]
    gap_partners = [partner(f"Partner G{n:02d}") for n in range(1, 14)]
    done_partners = [partner(f"Partner C{n:02d}") for n in range(1, 22)]
    pds = {"zero": [], "short": [], "done": [], "no_plan": []}

    # 12 never visited: (partner, required, status, end)
    zero = [(zero_partners[0], 2), (zero_partners[0], 2)] + [
        (p, r) for p, r in zip(zero_partners[1:], [2, 2, 2, 2, 2, 2, 2, 3, 3, 1], strict=True)
    ]
    zero_states = [("ended", D(2026, 3, 31))] * 3 + [
        ("active", D(2026, 4, 30)),
        ("signature", D(2027, 6, 30)),
    ]
    zero_states += [("active", D(2026, 12, 31))] * 7
    for (owner, required), (status, end) in zip(zero, zero_states, strict=True):
        pd = programme(owner, status=status, end=end, document_type="SPD")
        plan(pd, _quarters(required))
        pds["zero"].append(pd)

    # 14 visited but short: (completed, required)
    short = [(1, 2)] * 4 + [(1, 3)] * 4 + [(2, 3)] * 2 + [(2, 4)] * 4
    owners = [gap_partners[0]] + gap_partners
    short_states = [("active", D(2026, 5, 31)), ("active", D(2026, 8, 31)), ("signature", D(2027, 3, 31))]
    short_states += [("active", D(2026, 12, 31))] * 11
    for owner, (completed, required), (status, end) in zip(owners, short, short_states, strict=True):
        pd = programme(owner, status=status, end=end, sections=("Health",))
        plan(pd, _quarters(required))
        _visits(pd, completed, tpm_visits=completed == 2)
        pds["short"].append(pd)

    # 51 met: 7 surpluses of the partners with an unvisited PD, two PDs per completed-only partner, and
    # one more PD for two of the short partners
    for owner in zero_partners[4:]:
        pd = programme(owner, sections=("Child Protection",))
        plan(pd, _quarters(2))
        _visits(pd, 5)
        pds["done"].append(pd)
    for owner in done_partners:
        exact, surplus = programme(owner), programme(owner, document_type="SPD")
        plan(exact, _quarters(2))
        _visits(exact, 2)
        plan(surplus, _quarters(1))
        _visits(surplus, 3, tpm_visits=True)
        pds["done"] += [exact, surplus]
    for owner in gap_partners[1:3]:
        pd = programme(owner)
        plan(pd, _quarters(1))
        _visits(pd, 1)
        pds["done"].append(pd)

    # 2 ended, no plan this year
    for owner, visited in ((done_partners[0], True), (done_partners[1], False)):
        pd = programme(owner, status="ended", start=D(2025, 1, 1), end=D(2026, 2, 28))
        if visited:
            fm_visit(pd, end=D(2026, 1, 20))
        pds["no_plan"].append(pd)
    return pds


def test_the_emails_figures_rebuilt_exactly(email_world):
    rows = hact_plan.build(YEAR, TODAY)
    assert len(rows) == 79 and len({r["partner_id"] for r in rows}) == 45
    figures = hact_plan.headline(rows, YEAR)

    # "12 PDs for 11 partners have not been visited since the beginning of the year"
    assert figures["not_visited"] == {"pds": 12, "partners": 11, "partners_no_visit": 4, "ended": 4}
    # "47 FMM/HACT visits across 24 different partners are still needed", over 26 PDs, 6 of them ended
    still = figures["still_needed"]
    assert (still["visits"], still["pds"], still["partners"], still["pds_ended"]) == (47, 26, 24, 6)
    assert (
        still["visits_ended"] == 2 + 2 + 2 + 2 + 1 + 1
    )  # 3 ended and 1 past its end (unvisited), 2 short ones
    gaps = sorted(r["still_needed"] for r in rows if r["still_needed"])
    assert (gaps.count(1), gaps.count(2), gaps.count(3)) == (7, 17, 2)
    assert figures["by_remark"] == {"needed": 20, "ended": 6, "completed": 51, "no_plan": 2}
    assert (figures["pds"], figures["pds_with_plan"]) == (79, 77)
    required = (
        25
        + sum(r for _c, r in [(1, 2)] * 4 + [(1, 3)] * 4 + [(2, 3)] * 2 + [(2, 4)] * 4)
        + 7 * 2
        + 21 * 3
        + 2
    )
    assert figures["completed"]["required"] == required
    assert figures["completed"]["counted"] == required - 47
    # the 12 are the zero rows; the PDs with no plan never enter a figure, even the one visited
    assert {r["pd_id"] for r in rows if r["zero"]} == {pd.pk for pd in email_world["zero"]}
    assert all(row_of(rows, pd)["remark"] == "no_plan" for pd in email_world["no_plan"])
    assert row_of(rows, email_world["no_plan"][0])["completed"] == 1


def test_a_surplus_on_one_pd_never_makes_up_for_a_gap_on_another(email_world):
    rows = hact_plan.build(YEAR, TODAY)
    owner = email_world["zero"][6].partner_id  # a partner with an unvisited PD and a PD visited 5 times
    mine = [r for r in rows if r["partner_id"] == owner]
    assert sum(r["completed"] for r in mine) > sum(r["required"] for r in mine)  # the partner is "ahead"...
    figures = hact_plan.headline(mine, YEAR)
    assert (
        figures["still_needed"]["visits"] == 2 and figures["not_visited"]["partners"] == 1
    )  # ...and still counted
    assert figures["not_visited"]["partners_no_visit"] == 0
    (summary,) = hact_plan.by_partner(mine, YEAR)
    assert (summary["pds"], summary["pds_not_visited"], summary["still_needed"]) == (2, 1, 2)
    assert hact_plan.partner_counts(YEAR, TODAY)[owner] == {"pds": 1, "visits": 2}


def test_partner_counts_for_the_watch(email_world):
    counts = hact_plan.partner_counts(YEAR, TODAY)
    assert len(counts) == 24 and sum(c["visits"] for c in counts.values()) == 47
    assert sum(c["pds"] for c in counts.values()) == 26
    assert counts[email_world["zero"][0].partner_id] == {"pds": 2, "visits": 4}  # its two unvisited PDs


def test_build_takes_a_fixed_number_of_queries(django_assert_max_num_queries):
    owners = [partner(f"Partner {n}") for n in range(3)]
    for n in range(30):
        pd = programme(owners[n % 3])
        plan(pd)
        fm_visit(pd)
        tpm(pd, f"TPM/2026/{n}")
        trip(pd, f"TRIP-{n}")
    hact_year(owners[0], required=2, completed=1)
    with django_assert_max_num_queries(7):
        rows = hact_plan.build(YEAR, TODAY)
    assert len(rows) == 30
    with django_assert_max_num_queries(7):
        assert (
            len(hact_plan.build(YEAR, TODAY, pd_ids=[rows[0]["pd_id"], rows[1]["pd_id"], rows[2]["pd_id"]]))
            == 3
        )


# ---------------------------------------------------------------------------------- partner level
def test_the_etools_partner_minimum_beside_the_pd_figures():
    met = partner("Partner A")  # eTools minimum met, a PD still short
    hact_year(met, required=2, completed=3)
    met_pd = programme(met)
    plan(met_pd, (1, 1, 1, 0))
    fm_visit(met_pd)
    below = partner("Partner B")  # its PDs plan fewer visits than the partner minimum
    hact_year(below, required=4, completed=1)
    below_pd = programme(below)
    plan(below_pd, (1, 0, 0, 0))
    fm_visit(below_pd)
    none = partner("Partner C")  # no HACT row
    plan(programme(none), (1, 0, 0, 0))
    rows = hact_plan.build(YEAR, TODAY)
    summary = {p["partner"]: p for p in hact_plan.by_partner(rows, YEAR)}
    assert [p for p in summary] == ["Partner A", "Partner B", "Partner C"]
    a, b, c = summary["Partner A"], summary["Partner B"], summary["Partner C"]
    assert (a["etools_required"], a["etools_completed"], a["etools_gap"]) == (2, 3, 0)
    assert (a["still_needed"], a["minimum_met_pd_needed"], a["plans_below_minimum"]) == (2, True, False)
    assert (b["etools_gap"], b["required"], b["minimum_met_pd_needed"], b["plans_below_minimum"]) == (
        3,
        1,
        False,
        True,
    )
    assert (c["etools_required"], c["etools_gap"], c["minimum_met_pd_needed"], c["plans_below_minimum"]) == (
        None,
        None,
        False,
        False,
    )
    assert hact_plan.headline(rows, YEAR)["etools_behind"] == {"partners": 1, "with_row": 2}


def test_the_plans_below_minimum_flag_reads_every_pd_of_the_partner_whatever_the_filter():
    met = partner("Partner A")  # its PDs together plan 2 + 3 = 5 visits, above its minimum of 4
    hact_year(met, required=4, completed=3)
    unvisited = programme(met, sections=("Education",))
    plan(unvisited, (1, 1, 0, 0))
    visited = programme(met, sections=("Health",))
    plan(visited, (1, 1, 1, 0))
    for _visit in range(3):
        fm_visit(visited)
    below = partner("Partner B")  # 1 + 1 = 2 planned against a minimum of 3: below whatever the filter
    hact_year(below, required=3, completed=0)
    plan(programme(below, sections=("Education",)), (1, 0, 0, 0))
    plan(programme(below, sections=("Health",)), (0, 1, 0, 0))
    rows = hact_plan.build(YEAR, TODAY)

    def flags(**fields) -> dict[str, tuple[bool, bool]]:
        shown = hact_plan.select(rows, hact_plan.PlanFilters(year=YEAR, **fields))
        return {
            p["partner"]: (p["plans_below_minimum"], p["minimum_met_pd_needed"])
            for p in hact_plan.by_partner(shown, YEAR, rows)
        }

    expected = {"Partner A": (False, False), "Partner B": (True, False)}
    assert flags() == expected
    # a section's own PDs, the quick views, or both: the partner minimum is still the partner's
    for fields in (
        {"sections": ("Education",)},
        {"sections": ("Health",)},
        {"zero": True},
        {"remarks": ("needed", "ended")},
        {"sections": ("Education",), "remarks": ("needed", "ended")},
        {"sections": ("Health",), "zero": True},
    ):
        found = flags(**fields)
        assert found and found == {name: expected[name] for name in found}, fields
    # what the filter shows is still the filter's: Partner A's Required under Education is 2
    education = hact_plan.select(rows, hact_plan.PlanFilters(year=YEAR, sections=("Education",)))
    a = next(p for p in hact_plan.by_partner(education, YEAR, rows) if p["partner"] == "Partner A")
    assert (a["pds"], a["required"], a["still_needed"], a["etools_required"]) == (1, 2, 2, 4)


def test_no_visit_on_any_pd_counts_the_visits_on_pds_the_filter_leaves_out():
    a = partner("Partner A")  # not visited in Education, visited three times in Health
    plan(programme(a, sections=("Education",)), (1, 1, 0, 0))
    health = programme(a, sections=("Health",))
    plan(health, (1, 1, 1, 0))
    for _visit in range(3):
        fm_visit(health)
    b = partner("Partner B")  # never visited anywhere
    plan(programme(b, sections=("Education",)), (1, 0, 0, 0))
    rows = hact_plan.build(YEAR, TODAY)
    education = hact_plan.select(rows, hact_plan.PlanFilters(year=YEAR, sections=("Education",)))
    figures = hact_plan.headline(education, YEAR, rows)
    assert figures["not_visited"] == {"pds": 2, "partners": 2, "partners_no_visit": 1, "ended": 0}
    assert hact_plan.headline(rows, YEAR)["not_visited"]["partners_no_visit"] == 1


def test_pds_without_a_linked_partner_count_for_no_partner():
    for _pd in range(2):  # the sync could not link their partner
        plan(programme(None), (1, 1, 1, 1))
    plan(programme(partner("Partner A")), (1, 1, 1, 1))
    rows = hact_plan.build(YEAR, TODAY)
    figures = hact_plan.headline(rows, YEAR)
    assert figures["not_visited"] == {"pds": 3, "partners": 1, "partners_no_visit": 1, "ended": 0}
    assert (figures["still_needed"]["visits"], figures["still_needed"]["pds"]) == (12, 3)
    assert figures["still_needed"]["partners"] == 1
    assert hact_plan.partner_counts(YEAR, TODAY) == {rows[-1]["partner_id"]: {"pds": 1, "visits": 4}}


def test_programmatic_visits_by_partner_is_unchanged_and_agrees_with_the_pd_count():
    a, b = partner("Partner A"), partner("Partner B")
    only = programme(a)
    plan(only)
    fm_visit(only)
    fm_visit(only, status="COMPLETED")
    fm_visit(only, status="submitted")
    activity = next(_ids)
    dm.MonitoringFinding.objects.bulk_create([fm_row(only, activity), fm_row(None, activity, partner_row=a)])
    fm_visit(None, partner_row=b)  # a visit whose rows never name a PD: the partner's only
    by_partner = fm.programmatic_visits_by_partner(YEAR)
    assert by_partner == {a.pk: 3, b.pk: 1}
    assert by_partner[a.pk] == row_of(hact_plan.build(YEAR, TODAY), only)["completed_fm"]
    assert set(fm.completed_programmatic_rows(YEAR).values_list("monitoring_activity_id", flat=True)) == set(
        dm.MonitoringFinding.objects.filter(
            is_programmatic_visit=True, status__iexact="completed", end_date__year=YEAR
        ).values_list("monitoring_activity_id", flat=True)
    )


def test_a_visit_naming_only_the_partner_is_shown_beside_its_pds_not_counted():
    """A completed programmatic field monitoring visit whose rows name only the partner or a CP output
    counts for the partner on the Assurance page and for none of its PDs: every PD row of the partner
    carries it as ``fm_partner_level`` (shown, not counted), and so does its By partner line. A visit
    with a row naming a PD is that PD's; one awaiting approval, not programmatic or of another year is
    none; a visit with two partners counts for each."""
    a, b, c = partner("Partner A"), partner("Partner B"), partner("Partner C")
    a1, a2, b1, c1 = programme(a), programme(a), programme(b), programme(c)
    for pd in (a1, a2, b1, c1):
        plan(pd)

    def partner_visit(*owners, cp_output=False, **kwargs) -> int:
        activity = next(_ids)
        rows = [fm_row(None, activity, partner_row=owner, **kwargs) for owner in owners]
        if cp_output:
            row = fm_row(None, activity, partner_row=owners[0], **kwargs)
            row.entity, row.entity_type = "1.1 Test output", "CP Output"
            rows.append(row)
        dm.MonitoringFinding.objects.bulk_create(rows)
        return activity

    partner_visit(a, cp_output=True)  # the partner and a CP output: Partner A's
    partner_visit(a, c)  # two partners, no PD: each one's
    partner_visit(a, status="submitted")  # awaiting approval: none
    partner_visit(a, programmatic=False)  # not programmatic: none
    partner_visit(a, end=D(2027, 1, 1))  # next year's
    named = partner_visit(b)
    fm_row(b1, named).save()  # its partner row and a row naming B1: B1's visit
    assert fm.programmatic_visits_by_partner(YEAR) == {a.pk: 2, b.pk: 1, c.pk: 1}  # the Assurance page's
    assert fm.partner_visits_naming_no_pd(YEAR) == {a.pk: 2, c.pk: 1}
    assert fm.partner_visits_naming_no_pd(YEAR, [c.pk, b.pk]) == {c.pk: 1}
    assert fm.partner_visits_naming_no_pd(2027) == {a.pk: 1}

    rows = hact_plan.build(YEAR, TODAY)
    for pd, level, completed in ((a1, 2, 0), (a2, 2, 0), (b1, 0, 1), (c1, 1, 0)):
        row = row_of(rows, pd)
        assert (row["fm_partner_level"], row["completed_fm"]) == (level, completed), pd.number
    assert row_of(rows, a1)["zero"] and row_of(rows, a1)["remark"] == "needed"  # never counted
    assert row_of(hact_plan.build(YEAR, TODAY, pd_ids=[a2.pk]), a2)["fm_partner_level"] == 2  # a redrawn row
    by_partner = {p["partner"]: p["fm_partner_level"] for p in hact_plan.by_partner(rows, YEAR)}
    assert by_partner == {"Partner A": 2, "Partner B": 0, "Partner C": 1}


def test_planned_visit_rows_matching_no_pd_are_counted_apart():
    """eTools' planned-visit rows the sync could not link to a PD (by its reference) are in no figure of
    the plan; :func:`hact_plan.unlinked_plans` counts those of the year and the visits they plan."""
    a = partner("Partner A")
    pd = programme(a)
    plan(pd, (1, 0, 0, 0))
    for year, quarters in ((YEAR, (2, 1, 0, 0)), (YEAR, (0, 0, 0, 1)), (2025, (5, 0, 0, 0))):
        dm.PlannedVisits.objects.create(
            datamart_id=next(_ids),
            pd_reference_number="TEST/PCA2026999/PD2026999-1",
            year=year,
            **dict(zip(("q1", "q2", "q3", "q4"), quarters, strict=True)),
        )
    assert hact_plan.unlinked_plans(YEAR) == {"rows": 2, "visits": 4}
    assert hact_plan.unlinked_plans(2024) == {"rows": 0, "visits": 0}
    figures = hact_plan.headline(hact_plan.build(YEAR, TODAY), YEAR)
    assert (figures["still_needed"]["visits"], figures["completed"]["required"]) == (1, 1)


@pytest.mark.parametrize(
    "number, search",
    [
        ("TEST/PCA2026001/PD2026001-2", "PCA2026001/PD2026001"),
        ("TESTA/PCA2026001/SPD2026002", "PCA2026001/SPD2026002"),
        ("test/pca2026001/pd2026003", "PCA2026001/PD2026003"),
        ("TEST/SSFA2026004-1", "SSFA2026004"),
        ("", ""),
        (None, ""),
    ],
)
def test_the_search_reference_is_what_every_spelling_of_a_reference_holds(number, search):
    """The findings tab is searched by the PCA/PD pair (else the number without its prefix and
    amendment): a finding naming the PD under another prefix or amendment holds it too."""
    assert fm.search_reference(number) == search


# ---------------------------------------------------------------------------------- filters and summaries
def test_filters_narrow_the_rows_and_bad_values_are_dropped():
    params = QueryDict(
        "year=abc&q=%20test%20&section=Health&partner=x&partner=5&partner=5&risk=low&status=Active"
        "&type=spd&remark=bogus&remark=needed&zero=1",
        mutable=False,
    )
    f = hact_plan.PlanFilters.from_params(params, TODAY)
    assert f == hact_plan.PlanFilters(
        year=YEAR,
        q="test",
        sections=("Health",),
        partners=(5,),
        risks=("low",),
        statuses=("active",),
        types=("spd",),
        remarks=("needed",),
        zero=True,
    )
    assert hact_plan.PlanFilters.from_params({"year": "2025"}, TODAY).year == 2025
    assert hact_plan.PlanFilters.from_params({"year": "99999"}, TODAY).year == YEAR
    assert hact_plan.PlanFilters.from_params({}, TODAY) == hact_plan.PlanFilters(year=YEAR)

    a, b = partner("Partner A", rating="Low"), partner("Partner B", rating="High")
    edu = programme(a, sections=("Education",))
    plan(edu, (1, 0, 0, 0))
    health = programme(b, sections=("Health", "Education"), status="signed", document_type="SPD")
    plan(health, (1, 1, 0, 0))
    fm_visit(health)
    done = programme(b, sections=("WASH",))
    plan(done, (1, 0, 0, 0))
    fm_visit(done)
    rows = hact_plan.build(YEAR, TODAY)

    def kept(**fields) -> set[int]:
        return {r["pd_id"] for r in hact_plan.select(rows, hact_plan.PlanFilters(year=YEAR, **fields))}

    assert kept() == {edu.pk, health.pk, done.pk}
    assert kept(q="partner a") == {edu.pk}
    assert kept(q=a.vendor_number.lower()) == {edu.pk}
    assert kept(q=health.number[-6:]) == {health.pk}
    assert kept(sections=("education",)) == {edu.pk, health.pk}
    assert kept(partners=(b.pk,)) == {health.pk, done.pk}
    assert kept(risks=("HIGH",)) == {health.pk, done.pk}
    assert kept(statuses=("signed",)) == {health.pk}
    assert kept(types=("spd",)) == {health.pk}
    assert kept(remarks=("needed",)) == {edu.pk, health.pk}
    assert kept(remarks=("completed",)) == {done.pk}
    assert kept(zero=True) == {edu.pk}
    presets = hact_plan.PlanFilters(year=YEAR, zero=True, remarks=("completed",))
    assert len(hact_plan.select(rows, presets, presets=False)) == 3  # the headline leaves the presets out

    options = hact_plan.options(rows)
    assert options["sections"] == ["Education", "Health", "WASH"]
    assert options["partners"] == [[a.pk, "Partner A"], [b.pk, "Partner B"]]
    assert options["risks"] == ["High", "Low"] and options["types"] == ["PD", "SPD"]
    assert options["statuses"] == ["active", "signed"]
    assert [code for code, _label in options["remarks"]] == list(hact_plan.REMARK_ORDER)


def test_the_lists_keep_every_value_the_filter_applies_even_one_the_year_lacks(
    django_assert_num_queries,
):
    """After a change of year the filter can hold a partner or a section the new year has no PD of: the
    lists keep it, chosen, so the bar shows what the list is filtered on and it can be taken off."""
    a, d = partner("Partner A", rating="Low"), partner("Partner D")
    plan(programme(a, sections=("Education",)))
    last_year = programme(
        d, start=datetime.date(YEAR - 1, 1, 1), end=datetime.date(YEAR - 1, 12, 31), sections=("Nutrition",)
    )
    plan(last_year, year=YEAR - 1)
    rows = hact_plan.build(YEAR - 1, TODAY)
    assert [r["pd_id"] for r in rows] == [last_year.pk]
    f = hact_plan.PlanFilters(
        year=YEAR - 1,
        partners=(a.pk, 999999),
        sections=("Education", "nutrition"),
        risks=("High",),
        statuses=("signed",),
        types=("SPD",),
    )
    assert hact_plan.select(rows, f) == []  # the list is filtered on all of them
    with django_assert_num_queries(1):  # the partners the year lacks are named from their records
        options = hact_plan.options(rows, f)
    assert options["partners"] == [[999999, "#999999"], [a.pk, "Partner A"], [d.pk, "Partner D"]]
    assert options["sections"] == ["Education", "Nutrition"]
    assert options["risks"] == ["High", "Low"] and options["types"] == ["PD", "SPD"]
    assert options["statuses"] == ["active", "signed"]
    # each chosen value as its list spells it ("nutrition" is the year's "Nutrition", not a second line)
    assert options["chosen"] == {
        "sections": ["Education", "Nutrition"],
        "partners": [a.pk, 999999],
        "risks": ["High"],
        "statuses": ["signed"],
        "types": ["SPD"],
    }
    # every chosen value in the year's lists: nothing is added and nothing is read
    with django_assert_num_queries(0):
        same = hact_plan.options(rows, hact_plan.PlanFilters(year=YEAR - 1, partners=(d.pk,)))
    assert same["partners"] == [[d.pk, "Partner D"]] and same["chosen"]["partners"] == [d.pk]
    assert hact_plan.options(rows)["chosen"] == {
        "sections": [],
        "partners": [],
        "risks": [],
        "statuses": [],
        "types": [],
    }


def test_by_section_counts_a_pd_under_each_of_its_sections_and_once_in_the_total():
    a = partner("Partner A")
    both = programme(a, sections=("Education", "Health"))
    plan(both, (1, 1, 0, 0))
    edu = programme(a, sections=("education",))
    plan(edu, (1, 0, 0, 0))
    fm_visit(edu)
    nothing = programme(a, sections=())
    plan(nothing, (1, 0, 0, 0))
    sections = hact_plan.by_section(hact_plan.build(YEAR, TODAY))
    assert sections == [
        {
            "section": "Education",
            "total": False,
            "pds": 2,
            "pds_with_plan": 2,
            "not_visited": 1,
            "still_needed": 2,
        },
        {
            "section": "Health",
            "total": False,
            "pds": 1,
            "pds_with_plan": 1,
            "not_visited": 1,
            "still_needed": 2,
        },
        {"section": "", "total": False, "pds": 1, "pds_with_plan": 1, "not_visited": 1, "still_needed": 1},
        {"section": "", "total": True, "pds": 3, "pds_with_plan": 3, "not_visited": 2, "still_needed": 3},
    ]


def test_by_section_under_a_section_filter_keeps_the_chosen_sections_only():
    both = programme(partner("Partner A"), sections=("Education", "Health"))
    plan(both, (1, 1, 0, 0))
    health = programme(partner("Partner B"), sections=("Health",))
    plan(health, (1, 1, 0, 0))
    rows = hact_plan.build(YEAR, TODAY)
    every = {s["section"]: s for s in hact_plan.by_section(rows)}
    assert (every["Health"]["pds"], every["Health"]["still_needed"]) == (2, 4)

    def lines(*sections) -> list[dict]:
        shown = hact_plan.select(rows, hact_plan.PlanFilters(year=YEAR, sections=sections))
        return hact_plan.by_section(shown, sections)

    # Health's line would hold only the PD it shares with Education (1 PD, 2 visits): it is left out
    education = lines("education")
    assert [(s["section"], s["total"]) for s in education] == [("Education", False), ("", True)]
    assert education[0] == every["Education"] and education[1]["pds"] == 1
    # the section chosen keeps its whole line, and two sections chosen keep both
    assert lines("Health")[0] == every["Health"]
    assert [s["section"] for s in lines("Health", "Education")] == ["Education", "Health", ""]


def test_years_and_the_sync_the_figures_are_as_of():
    a = partner("Partner A")
    plan(programme(a), year=2025)
    hact_year(a, required=1, completed=0, year=2027)
    assert hact_plan.years(TODAY) == [2027, 2026, 2025]
    assert hact_plan.synced_on() is None

    def run(target, day, status=SyncRun.Status.SUCCEEDED):
        at = timezone.make_aware(datetime.datetime(day.year, day.month, day.day, 21))
        SyncRun.objects.create(
            job=SyncRun.Job.ETOOLS_DATAMART, target=target, status=status, started_at=at, finished_at=at
        )

    run("planned_visits", D(2026, 10, 12))
    run("field_monitoring", D(2026, 10, 12), SyncRun.Status.PARTIAL)
    run("tpm_activities", D(2026, 10, 10))
    run("tpm_activities", D(2026, 10, 12), SyncRun.Status.FAILED)  # failed: not a sync the figures are from
    run("partners", D(2026, 10, 13))  # not one of the plan's datasets
    assert hact_plan.synced_on() == D(2026, 10, 10)  # the oldest of the three latest successful runs


# ---------------------------------------------------------------------------------- privacy
def test_no_staff_name_ever_enters_the_plain_rows(email_world, admin_user):
    pd = email_world["zero"][0]
    hact_plan.set_comment(pd, YEAR, "Visit planned for November.", admin_user)
    rows = hact_plan.build(YEAR, TODAY)
    plain = json.dumps(
        {
            "rows": rows,
            "headline": hact_plan.headline(rows, YEAR),
            "partners": hact_plan.by_partner(rows, YEAR),
            "sections": hact_plan.by_section(rows),
            "options": hact_plan.options(rows),
            "watch": hact_plan.partner_counts(YEAR, TODAY),
        },
        default=str,
    )
    assert FOCAL not in plain and "Focal Person" not in plain and "example.org" not in plain
    assert (
        admin_user.username not in plain and "November" not in plain
    )  # comments are the page's, not the rows'
    assert all(row["focal_points_count"] == 1 for row in rows)
    # the page alone reads the names, and never the e-mail address
    shown = hact_plan.for_page([row_of(rows, pd)], YEAR, admin_user)[0]
    assert shown["focal_points"] == [FOCAL] and shown["comment"]["text"] == "Visit planned for November."
    assert set(shown["comment"]) == {"text", "updated_at"}
    assert hact_plan.focal_names([pd.pk]) == {pd.pk: [FOCAL]}


def test_the_comments_are_not_a_datamart_dataset():
    models = {model for model, *_rest in catalogue.TYPED.values()}
    assert "VisitPlanComment" not in models and "IndicatorFlag" not in models
    assert "visit_plan_comment" not in catalogue.TYPED and "visit_plan_comments" not in catalogue.DOCUMENTS


# ---------------------------------------------------------------------------------- comments
@pytest.fixture
def sections(db):
    education = Section.objects.create(name="Education", code="EDU")
    health = Section.objects.create(name="Health", code="HLT")
    SectionMatch.objects.create(etools_name="Education", section=education, how="exact", confirmed=True)
    SectionMatch.objects.create(etools_name="Health", section=health, how="exact", confirmed=True)
    # an unconfirmed guess never gives a right
    SectionMatch.objects.create(
        etools_name="Education and Youth", section=health, how="contains", confirmed=False
    )
    return {"education": education, "health": health}


def _person(username, role, section=None) -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="pass-123456-abc"
    )
    if role:
        user.groups.add(Group.objects.get(name=role))
    if section is not None:
        user.section = section
        user.save(update_fields=["section"])
    return User.objects.get(pk=user.pk)


@pytest.fixture
def people(roles, sections):
    donor = _person("donor", None)
    DonorAccount.objects.create(user=donor, name="Donor Test", donors=["EU"], must_change_password=False)
    superuser = _person("root", None)
    User.objects.filter(pk=superuser.pk).update(is_superuser=True)
    return {
        "admin": _person("admin-test", ADMIN),
        "superuser": User.objects.get(pk=superuser.pk),
        "editor": _person("edu-editor", SECTION_EDITOR, sections["education"]),
        "other_editor": _person("hlt-editor", SECTION_EDITOR, sections["health"]),
        "editor_without_section": _person("lost-editor", SECTION_EDITOR),
        "viewer": _person("edu-viewer", VIEWER, sections["education"]),
        "donor": User.objects.get(pk=donor.pk),
    }


@pytest.fixture
def commented_pd(db):
    pd = programme(partner("Partner A"), sections=(" education ", "Education and Youth"))
    plan(pd)
    return pd


ALLOWED = {"admin": True, "superuser": True, "editor": True}


@pytest.mark.parametrize(
    "who", ["admin", "superuser", "editor", "other_editor", "editor_without_section", "viewer", "donor"]
)
def test_who_may_comment(people, commented_pd, who):
    from django.contrib.auth.models import AnonymousUser

    from neurodb.watch.sections import confirmed_map

    user = people[who]
    expected = ALLOWED.get(who, False)
    assert hact_plan.can_comment(user, commented_pd.section_names) is expected
    assert hact_plan.can_comment(user, commented_pd.section_names, confirmed_map()) is expected
    assert hact_plan.may_comment(hact_plan.editable_names(user), commented_pd.section_names) is expected
    rows = hact_plan.for_page(hact_plan.build(YEAR, TODAY), YEAR, user)
    assert row_of(rows, commented_pd)["can_comment"] is expected
    assert hact_plan.can_comment(AnonymousUser(), commented_pd.section_names) is False
    assert hact_plan.editable_names(AnonymousUser()) == set()
    assert hact_plan.can_comment(user, []) is (who in ("admin", "superuser"))


def _url(pd) -> str:
    return reverse("reports:visit_plan_comment", args=[pd.pk])


@pytest.mark.parametrize("who", ["other_editor", "editor_without_section", "viewer"])
def test_staff_who_may_not_comment_get_403(client, people, commented_pd, who):
    client.force_login(people[who])
    assert client.post(_url(commented_pd), {"text": "Not allowed", "year": YEAR}).status_code == 403
    assert client.get(_url(commented_pd), {"year": YEAR}, HTTP_HX_REQUEST="true").status_code == 403
    assert not dm.VisitPlanComment.objects.exists()


def test_a_donor_and_an_anonymous_user_never_reach_the_comment(client, people, commented_pd):
    response = client.post(_url(commented_pd), {"text": "Not allowed", "year": YEAR})
    assert response.status_code == 302 and response["Location"].startswith(
        reverse("account_login")
    )  # sign in
    client.force_login(people["donor"])
    response = client.post(_url(commented_pd), {"text": "Not allowed", "year": YEAR})
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
    response = client.post(_url(commented_pd), {"text": "Not allowed", "year": YEAR}, HTTP_HX_REQUEST="true")
    assert response.status_code == 403
    assert not dm.VisitPlanComment.objects.exists()


def test_a_section_editor_writes_edits_and_removes_the_comment(client, people, commented_pd):
    client.force_login(people["editor"])
    back = f"{reverse('reports:monitoring')}?tab=plan&year={YEAR}#pd-{commented_pd.pk}"

    form = client.get(_url(commented_pd), {"year": YEAR}, HTTP_HX_REQUEST="true")
    assert form.status_code == 200
    html = form.content.decode()
    assert 'maxlength="500"' in html and f'hx-target="#pd-{commented_pd.pk}"' in html
    assert (
        "Do not write people&#x27;s names or contact details" in html or "Do not write people's names" in html
    )
    assert ">Remove<" not in html  # nothing to remove yet

    response = client.post(_url(commented_pd), {"text": "  Visit\x00 planned\nin November. ", "year": YEAR})
    assert response.status_code == 302 and response["Location"] == back
    comment = dm.VisitPlanComment.objects.get()
    assert (comment.intervention_id, comment.year, comment.text) == (
        commented_pd.pk,
        YEAR,
        "Visit planned in November.",
    )
    assert (comment.pd_reference, comment.updated_by) == (commented_pd.number, "edu-editor")

    client.post(_url(commented_pd), {"text": "x" * 600, "year": YEAR})
    comment.refresh_from_db()
    assert (
        comment.text == "x" * 500 and dm.VisitPlanComment.objects.count() == 1
    )  # capped, one per PD and year
    page = client.get(_url(commented_pd), {"year": YEAR})  # without JavaScript: a page of its own
    assert (
        page.status_code == 200 and "x" * 500 in page.content.decode() and ">Remove<" in page.content.decode()
    )

    plan(commented_pd, year=YEAR - 1)  # the PD is on last year's plan too
    client.post(_url(commented_pd), {"text": "Kept", "year": YEAR - 1})  # another year: another comment
    assert dm.VisitPlanComment.objects.count() == 2
    response = client.post(_url(commented_pd), {"text": "x", "remove": "1", "year": YEAR})
    assert response.status_code == 302
    assert list(dm.VisitPlanComment.objects.values_list("year", "text")) == [(YEAR - 1, "Kept")]
    client.post(_url(commented_pd), {"text": "   ", "year": YEAR - 1})  # an empty text removes it too
    assert not dm.VisitPlanComment.objects.exists()

    # the admin keeps the history: every change, page edits included, with who and when, never the text
    entries = list(
        LogEntry.objects.filter(content_type__model="visitplancomment")
        .order_by("pk")
        .values_list("action_flag", "change_message", "user__username")
    )
    assert entries == [
        (ADDITION, "Written on the HACT visit plan (26 characters).", "edu-editor"),
        (CHANGE, "Written on the HACT visit plan (500 characters).", "edu-editor"),
        (ADDITION, "Written on the HACT visit plan (4 characters).", "edu-editor"),
        (DELETION, "Removed on the HACT visit plan.", "edu-editor"),
        (DELETION, "Removed on the HACT visit plan.", "edu-editor"),
    ]
    for text in ("November", "x" * 20, "Kept"):
        assert not LogEntry.objects.filter(change_message__contains=text).exists(), text
        assert not LogEntry.objects.filter(object_repr__contains=text).exists(), text


def test_an_administrator_comments_on_any_pd_and_htmx_gets_the_row_back(client, people, monkeypatch):
    from neurodb.reports import views

    pd = programme(partner("Partner A"), sections=("Nutrition",))  # a section no editor holds
    plan(pd, (1, 1, 0, 0))
    seen = {}

    def render(request, template, context):  # the row template belongs to the page
        seen.update(template=template, context=context)
        from django.http import HttpResponse

        return HttpResponse(f'<tr id="pd-{context["row"]["pd_id"]}"></tr>')

    monkeypatch.setattr(views, "render", render)
    client.force_login(people["admin"])
    response = client.post(_url(pd), {"text": "Moved to Q4.", "year": YEAR}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and response.content.decode() == f'<tr id="pd-{pd.pk}"></tr>'
    assert seen["template"] == "reports/partials/_plan_row.html"
    context = seen["context"]
    assert context["row"]["pd_id"] == pd.pk and context["row"]["comment"]["text"] == "Moved to Q4."
    assert context["row"]["can_comment"] is True and context["row"]["focal_points"] == [FOCAL]
    assert context["year"] == YEAR and set(context) >= {"row", "year", "today", "due_quarter", "fmm_on"}
    assert dm.VisitPlanComment.objects.get().updated_by == "admin-test"


def test_the_comment_needs_the_csrf_token(people, commented_pd):
    client = Client(enforce_csrf_checks=True)
    client.force_login(people["editor"])
    assert client.post(_url(commented_pd), {"text": "No token", "year": YEAR}).status_code == 403
    assert not dm.VisitPlanComment.objects.exists()


def test_comments_of_a_year(admin_user):
    a = partner("Partner A")
    first, second = programme(a), programme(a)
    hact_plan.set_comment(first, YEAR, "This year", admin_user)
    hact_plan.set_comment(second, YEAR - 1, "Last year", admin_user)
    found = hact_plan.comments(YEAR, [first.pk, second.pk])
    assert list(found) == [first.pk] and found[first.pk]["text"] == "This year"
    assert (
        hact_plan.set_comment(first, YEAR, "This year", admin_user).text == "This year"
    )  # unchanged: no new entry
    assert LogEntry.objects.filter(content_type__model="visitplancomment").count() == 2
    assert hact_plan.clean_comment("\tA\x07 b​  c ") == "A b​ c"


def test_administrators_find_the_comments_and_their_history_in_the_admin(client, admin_user):
    pd = programme(partner("Partner A"))
    comment = hact_plan.set_comment(pd, YEAR, "Visit planned for November.", admin_user)
    client.force_login(admin_user)  # an Administrator, not a superuser: the Datamart tables are hidden
    changelist = reverse("admin:datamart_visitplancomment_changelist")
    assert changelist in client.get(reverse("admin:index")).content.decode()
    listing = client.get(changelist, {"q": pd.number})
    assert listing.status_code == 200 and pd.number in listing.content.decode()
    change = reverse("admin:datamart_visitplancomment_change", args=[comment.pk])
    assert client.get(change).status_code == 200
    history = client.get(reverse("admin:datamart_visitplancomment_history", args=[comment.pk]))
    assert history.status_code == 200 and "Written on the HACT visit plan" in history.content.decode()
    data = {"intervention": pd.pk, "pd_reference": pd.number, "year": YEAR, "text": "Corrected in the admin"}
    assert client.post(change, data).status_code == 302
    comment.refresh_from_db()
    assert (comment.text, comment.updated_by) == ("Corrected in the admin", admin_user.username)
    # a correction made in the admin is in the history too: who, when and which field, never the text
    entries = LogEntry.objects.filter(content_type__model="visitplancomment").order_by("pk")
    assert [(e.action_flag, e.user_id) for e in entries] == [
        (ADDITION, admin_user.pk),
        (CHANGE, admin_user.pk),
    ]
    assert entries.last().get_change_message() == "Changed Text."
    trail = client.get(reverse("admin:admin_logentry_changelist"))
    assert trail.status_code == 200 and "Written on the HACT visit plan" in trail.content.decode()
    for text in ("November", "Corrected"):
        assert not LogEntry.objects.filter(change_message__contains=text).exists(), text
        assert text not in trail.content.decode(), text


def test_a_name_typed_by_mistake_leaves_no_trace_once_the_comment_is_removed(
    client, people, commented_pd, admin_user
):
    client.force_login(people["editor"])
    client.post(_url(commented_pd), {"text": "Call Focal Person Test on +00-0-123456", "year": YEAR})
    client.post(_url(commented_pd), {"text": "Call the partner's office", "year": YEAR})
    client.post(_url(commented_pd), {"remove": "1", "year": YEAR})
    assert not dm.VisitPlanComment.objects.exists()
    # the audit trail still says who changed the comment, and when
    entries = LogEntry.objects.filter(content_type__model="visitplancomment")
    assert [e.action_flag for e in entries.order_by("pk")] == [ADDITION, CHANGE, DELETION]
    assert set(entries.values_list("user__username", flat=True)) == {"edu-editor"}
    # the administrators' audit trail (read-only: nothing there can be erased) never held the text
    client.force_login(admin_user)
    trail = reverse("admin:admin_logentry_changelist")
    listing = client.get(trail)
    assert listing.status_code == 200 and "Removed on the HACT visit plan" in listing.content.decode()
    for text in ("Focal Person", "123456", "partner&#x27;s office", "partner's office"):
        assert not LogEntry.objects.filter(change_message__contains=text).exists(), text
        assert text not in listing.content.decode(), text
        found = client.get(trail, {"q": text})  # the search box echoes the text: count what it finds
        assert found.status_code == 200 and found.context_data["cl"].result_count == 0, text
    assert client.get(trail, {"q": "HACT visit plan"}).context_data["cl"].result_count == 3


def test_a_comment_only_on_a_programme_document_the_plan_lists_for_that_year(client, people, commented_pd):
    owner = commented_pd.partner
    draft = programme(owner, status="draft")
    plan(draft)
    cancelled = programme(owner, status="cancelled")
    old = programme(owner, start=D(YEAR - 3, 1, 1), end=D(YEAR - 2, 12, 31))  # ran before, no plan now
    refused = [(draft, YEAR), (cancelled, YEAR), (old, YEAR), (commented_pd, YEAR - 5)]
    refused += [(commented_pd, YEAR + 10), (commented_pd, 1990)]  # years the PD never ran in
    for who in ("admin", "editor"):
        client.force_login(people[who])
        for pd, year in refused:
            for hx in ({}, {"HTTP_HX_REQUEST": "true"}):
                assert client.get(_url(pd), {"year": year}, **hx).status_code == 404, (who, pd.status, year)
                response = client.post(_url(pd), {"text": "Visit planned.", "year": year}, **hx)
                assert response.status_code == 404, (who, pd.status, year)
    assert not dm.VisitPlanComment.objects.exists() and not LogEntry.objects.exists()
    # the old PD in a year it ran: listed, so commented on
    assert client.post(_url(old), {"text": "Visit missed.", "year": YEAR - 2}).status_code == 302
    assert list(dm.VisitPlanComment.objects.values_list("intervention_id", "year")) == [(old.pk, YEAR - 2)]
    # a section editor of another section still gets 403 first, whatever the year
    client.force_login(people["other_editor"])
    assert client.post(_url(commented_pd), {"text": "x", "year": 1990}).status_code == 403
