"""Which PD indicator a partner report row belongs to (neurodb.datamart.identity): today's order (the eTools
id, the title, the PD output and title) first, then, for a title and an output two indicators of a PD share,
the tie-breakers the data proves, per placement group and never per row. Each world is one programme document
with two such indicators (X and Y, told apart by one key) and, for the proof, twenty indicators of another
one placed by their title; nothing names a person."""

from __future__ import annotations

import datetime
import itertools
import json
import time
from decimal import Decimal

import pytest
import responses
from django.db import connection, transaction
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import checks, identity, monitoring, reached, reporting, services
from neurodb.datamart import models as dm
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = D(2026, 10, 15)  # the suite's day (tests/conftest.py SITE_NOW)
CHILDREN = "# of children reached"
OUTPUT = "1.1 Protection"
PLACES = {"Akkar": "LB11", "Bekaa": "LB21", "Tripoli": "LB31"}
QUARTERS = {"QPR1": (D(2026, 1, 1), D(2026, 3, 31)), "QPR2": (D(2026, 4, 1), D(2026, 6, 30))}
MONTHS = {"HR1": (D(2026, 1, 1), D(2026, 1, 31)), "HR2": (D(2026, 2, 1), D(2026, 2, 28))}
SEX, NATIONALITY, MALE, FEMALE, LEBANESE = 10, 20, 2264, 2265, 2266
X, Y = 501, 502  # the two indicators of PD B sharing CHILDREN and OUTPUT
_ids = itertools.count(840_000)


def make_pd(letter: str) -> PCA:
    n = next(_ids)
    partner = PartnerOrganization.objects.create(
        etl_id=f"p{n}", name=f"Partner {letter}", vendor_number=f"V{n}"
    )
    return PCA.objects.create(
        etl_id=f"pd{n}",
        partner=partner,
        partner_name=partner.name,
        number=f"LEB/ID/PD{letter}",
        title=f"Programme {letter}",
        status="active",
        start=D(2026, 1, 1),
        end=D(2026, 12, 31),
    )


def indicator(
    pd: PCA,
    source_id: int,
    title: str = CHILDREN,
    output: str = OUTPUT,
    *,
    places: tuple[str, ...] = ("Akkar", "Bekaa"),
    disaggregations: tuple[str, ...] = ("",),
    active: bool = True,
    hf: bool = False,
    cluster: str = "",
    target: int = 1000,
    display_type: str = "number",
    unit: str = "number",
) -> None:
    """A PD indicator, one row per planned place and disaggregation (as the Datamart lists it)."""
    for place in places:
        for name in disaggregations:
            dm.PDIndicator.objects.create(
                datamart_id=next(_ids),
                source_id=source_id,
                intervention=pd,
                pd_reference_number=pd.number,
                title=title,
                lower_result_name=output,
                section_name="Child Protection",
                location_name=place,
                location_pcode=PLACES[place],
                disaggregation_name=name,
                is_active=active,
                is_high_frequency=hf,
                cluster_name=cluster,
                target_numerator=Decimal(target),
                display_type=display_type,
                unit=unit,
            )


def row(
    pd: PCA,
    value: float = 40,
    *,
    title: str = CHILDREN,
    output: str = OUTPUT,
    number: str = "QPR1",
    place: str = "Akkar",
    hf: bool = False,
    cluster: str | None = None,
    kind: str | None = None,
    target: str = "1000",
    values: dict[str, float] | None = None,
    total: dict | None = None,
) -> dm.ReportedIndicator:
    """One location of one indicator in one progress report, as the Lebanon rows come (no PRP id, no eTools
    indicator id); ``cluster`` and ``kind``: PRP's raw cluster_indicator and indicator_type."""
    start, end = (MONTHS if number.startswith("HR") else QUARTERS)[number]
    data = {"performance_indicator": title, "pd_output_title": output}
    if cluster is not None:
        data["cluster_indicator"] = cluster
    if kind is not None:
        data["indicator_type"] = kind
    return dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner=pd.partner,
        partner_name=pd.partner_name,
        pd_reference_number=pd.number,
        report_number=number,
        report_type="HR" if number.startswith("HR") else "QPR",
        report_status="Accepted",
        period_start=start,
        period_end=end,
        due_date=end + datetime.timedelta(days=15),
        submission_date=end + datetime.timedelta(days=10),
        indicator=title,
        pd_output=output,
        location=place,
        p_code=PLACES[place],
        high_frequency=hf,
        target=target,
        disaggregation={"()": total or {"v": value, "d": 1, "c": value}, **(values or {})},
        calculation_across_locations="sum",
        calculation_across_periods="sum",
        data=data,
    )


def gate(
    n: int = identity.CALIBRATION_MIN, *, indicators: dict | None = None, rows: dict | None = None
) -> PCA:
    """``n`` indicators of another PD, each with its own title and one report row: the groups placed by title
    the proof compares each tie-breaker on (the indicators and the rows say what ``indicators`` and ``rows``
    give, so they agree)."""
    pd = make_pd("G")
    for i in range(n):
        indicator(pd, 9000 + i, f"Indicator {i}", "9.1 Other", **(indicators or {}))
        row(pd, 5, title=f"Indicator {i}", output="9.1 Other", **(rows or {}))
    return pd


def labels() -> None:
    for pk, label, type_id in (
        (MALE, "Male", SEX),
        (FEMALE, "Female", SEX),
        (LEBANESE, "Lebanese", NATIONALITY),
    ):
        dm.DisaggregationLabel.objects.create(kind="value", prp_id=pk, label=label, disaggregation_id=type_id)
    dm.DisaggregationLabel.objects.create(kind="type", prp_id=SEX, label="Sex")
    dm.DisaggregationLabel.objects.create(kind="type", prp_id=NATIONALITY, label="Nationality")


def whole(count: int = 40, **details) -> SyncRun:
    """The latest PD indicators sync: read whole unless ``details`` say otherwise. Made before :func:`proof`
    (the newest partner reports or PD indicators run carries the proof the pages read)."""
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="pd_indicators",
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
        details={"datamart_count": count, **details},
    )


def proof(stored: dict | None = None) -> dict:
    """The proof of the tie-breakers, stored as the partner reports sync stores it in its run."""
    found = identity.calibrate() if stored is None else stored
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
        details={"identity": found},
    )
    return found


def grid(pd: PCA, report_type: str = "QPR") -> dict[int, monitoring.Indicator]:
    found = monitoring.indicators(
        monitoring.Filters(pd_ids=[pd.id], scope="all", year=None, report_type=report_type), TODAY
    )
    return {i.source_id: i for i in found}


def ways(pd: PCA) -> list[str]:
    """Where the rows of PD B's shared title and output go, as the pages place them."""
    rows = list(reached.report_rows([pd.id], ["QPR", "HR"]))
    return sorted({p.way for p in identity.Idents([pd.id]).place(rows)})


def one(key: str) -> dict:
    check = next(c for c in checks.CHECKS if c.key == key)
    with transaction.atomic(), connection.cursor() as cursor:
        return check.run(checks._Timed(cursor, time.monotonic() + 60))


# ------------------------------------------------------------------------------------- today's rule
def test_today_s_order_is_unchanged_without_a_calibration():
    """By id, by title, by output and title, and the rows left out (a shared title and output, an output that
    matches none, an unknown title): each row as today's rule reads it, with or without a proof of another
    version stored."""
    pd = make_pd("B")
    indicator(pd, 401, "# of kits distributed", "2.1 Kits")
    indicator(pd, 402, "# of youth trained", "3.1 Skills")
    indicator(pd, 403, "# of youth trained", "3.2 Jobs")
    indicator(pd, X)
    indicator(pd, Y)
    gate()
    rows = [
        row(pd, title="# of kits distributed", output="2.1 Kits"),
        row(pd, title="# of youth trained", output="3.1 skills "),
        row(pd, title="# of youth trained", output="Output 3: Jobs"),
        row(pd),
        row(pd, title="# of tents", output="4.1 Shelter"),
    ]
    dm.ReportedIndicator.objects.filter(pk=rows[0].pk).update(etools_indicator_id="401", indicator="other")
    for stored in (None, {"version": identity.CALIBRATION_VERSION + 1, "enabled": list(identity.BREAKERS)}):
        if stored is not None:
            proof(stored)
        idents = identity.Idents([pd.id])
        found = list(reached.report_rows([pd.id], ["QPR"]))
        placed = idents.place(found)
        assert [(p.ident and p.ident[1], p.way) for p in placed] == [
            ("s401", "id"),
            ("s402", "output"),
            (None, "output_unmatched"),
            (None, "shared_output"),
            (None, "unknown"),
        ]
        assert [p.way for p in placed] == [
            idents.how(r["intervention_id"], r["etools_indicator_id"], r["indicator"], r["pd_output"])
            for r in found
        ]
        assert idents.calibration.enabled == frozenset()
    entries = grid(pd)
    assert (entries[X].reached, entries[Y].reached, entries[402].reached) == (None, None, 40.0)


# ---------------------------------------------------------------------------------- each tie-breaker
# per tie-breaker: X's and Y's own keys, the shared rows' keys, the proof's indicators and rows, the winner
SETUPS = {
    "high_frequency": ({"hf": True}, {}, {"hf": True}, {}, {}, X),
    "cluster": (
        {"cluster": "Child Protection cluster"},
        {},
        {"cluster": "true"},
        {},
        {"cluster": "false"},
        X,
    ),
    "target": ({}, {"target": 500}, {"target": "500"}, {}, {}, Y),
    "indicator_type": (
        {},
        {"display_type": "percentage", "unit": "percentage"},
        {"kind": "Quantity"},
        {},
        {"kind": "quantity"},
        X,
    ),
    "disaggregations": (
        {"disaggregations": ("Sex",)},
        {"disaggregations": ("Sex", "Nationality")},
        {"values": {f"({MALE},)": 10, f"({FEMALE},)": 30}},
        {"disaggregations": ("Sex",)},
        {"values": {f"({MALE},)": 2, f"({FEMALE},)": 3}},
        X,
    ),
    "locations": ({"places": ("Akkar",)}, {"places": ("Bekaa",)}, {}, {"places": ("Akkar",)}, {}, X),
    "active": ({}, {"active": False}, {}, {}, {}, X),
}


def world(name: str) -> PCA:
    mine, theirs, shared, proved, proved_rows, _ = SETUPS[name]
    pd = make_pd("B")
    indicator(pd, X, **mine)
    indicator(pd, Y, **theirs)
    row(pd, **shared)
    gate(indicators=proved, rows=proved_rows)
    labels()
    if name == "locations":
        whole()
    return pd


@pytest.mark.parametrize("name", identity.BREAKERS)
def test_each_tie_breaker_places_the_rows_on_the_indicator_it_names(name):
    """Two indicators sharing a title and an output, apart by one key only: with no proof the rows are left
    out (as today); once twenty groups placed by title agree on that key, they go to the indicator it names,
    which says so (``placed_by``)."""
    pd = world(name)
    winner = SETUPS[name][-1]
    other = Y if winner == X else X
    before = grid(pd)
    assert (before[X].reached, before[Y].reached) == (None, None)
    stored = proof()
    assert name in stored["enabled"] and stored["stats"][name]["compared"] >= identity.CALIBRATION_MIN
    after = grid(pd)
    assert (after[winner].reached, after[other].reached) == (40.0, None)
    assert name in after[winner].placed_by.split(", ") and after[other].placed_by == ""
    assert ways(pd) == [name]


def test_a_breaker_that_disagrees_once_on_placed_rows_is_not_used():
    """Twenty placed groups agree on high frequency, one does not: high frequency is never used."""
    pd = world("high_frequency")
    other = make_pd("H")
    indicator(other, 7001, "# of teachers trained", "5.1 Teachers")
    row(other, title="# of teachers trained", output="5.1 Teachers", hf=True)  # the indicator is not
    stored = proof()
    assert stored["stats"]["high_frequency"]["differ"] == 1 and "high_frequency" not in stored["enabled"]
    assert (grid(pd)[X].reached, grid(pd)[Y].reached) == (None, None)
    assert ways(pd) == ["shared_output"]


def test_two_keys_naming_different_indicators_leave_the_rows_out():
    """High frequency names X, the target names Y: the rows go to neither."""
    pd = make_pd("B")
    indicator(pd, X, hf=True)
    indicator(pd, Y, target=500)
    row(pd, hf=True, target="500")
    gate()
    proof()
    entries = grid(pd)
    assert (entries[X].reached, entries[Y].reached) == (None, None)
    assert ways(pd) == ["keys_disagree"]


def test_an_enabled_breaker_that_contradicts_keeps_the_group_out():
    """High frequency names X; the target in use matches neither (an amended target, or another indicator):
    the group is unlike every placed one and stays out."""
    pd = make_pd("B")
    indicator(pd, X, hf=True)
    indicator(pd, Y)
    row(pd, hf=True, target="700")
    gate()
    assert {"high_frequency", "target"} <= set(proof()["enabled"])
    assert (grid(pd)[X].reached, grid(pd)[Y].reached) == (None, None)
    assert ways(pd) == ["keys_disagree"]


def test_two_indicator_reports_never_go_to_one_indicator():
    """PRP's two copies side by side in one report, one high frequency (40) and one not (30), both saying they
    are a cluster's; only X is: the cluster names X for both groups, so neither goes (Reached would take the
    larger of the two)."""
    pd = make_pd("B")
    indicator(pd, X, cluster="Child Protection cluster")
    indicator(pd, Y)
    row(pd, 40, hf=True, cluster="true")
    row(pd, 30, hf=False, cluster="true")
    gate(rows={"cluster": "false"})
    off = make_pd("H")  # high frequency is not proved: one placed group differs
    indicator(off, 7001, "# of teachers trained", "5.1 Teachers")
    row(off, title="# of teachers trained", output="5.1 Teachers", hf=True)
    stored = proof()
    assert "cluster" in stored["enabled"] and "high_frequency" not in stored["enabled"]
    entries = grid(pd)
    assert (entries[X].reached, entries[Y].reached) == (None, None)
    assert ways(pd) == ["two_for_one"]


def test_a_group_is_placed_whole():
    """The target tells X from Y on the Akkar row, the Bekaa row gives none: the group goes nowhere, never
    half of it."""
    pd = make_pd("B")
    indicator(pd, X)
    indicator(pd, Y, target=500)
    row(pd, 40, target="500")
    row(pd, 30, place="Bekaa", target="")
    gate()
    assert "target" in proof()["enabled"]
    assert (grid(pd)[X].reached, grid(pd)[Y].reached) == (None, None)
    assert ways(pd) == ["shared_output"]


def test_a_group_with_two_values_at_one_location_is_never_placed_by_a_tie_breaker():
    """High frequency names X, but the group has 10 and 30 at Akkar: both copies may be in it, and X would
    take 30 (the larger, as one location of one report). Neither gets it."""
    pd = make_pd("B")
    indicator(pd, X, hf=True)
    indicator(pd, Y)
    row(pd, 10, hf=True)
    row(pd, 30, hf=True)
    gate()
    proof()
    entries = grid(pd)
    assert (entries[X].reached, entries[Y].reached) == (None, None)
    assert ways(pd) == ["side_by_side"]


def test_a_group_holding_both_copies_at_different_places_is_not_given_to_the_active_one():
    """X (active) plans Akkar, Y (inactive) Bekaa; one report has Akkar 10 and Bekaa 30 under the shared title:
    the Bekaa row may be Y's, so the group is not given to X."""
    pd = make_pd("B")
    indicator(pd, X, places=("Akkar",))
    indicator(pd, Y, places=("Bekaa",), active=False)
    row(pd, 10)
    row(pd, 30, place="Bekaa")
    gate(indicators={"places": ("Akkar",)})
    whole()
    assert "active" in proof()["enabled"]
    entries = grid(pd)
    assert (entries[X].reached, entries[Y].reached) == (None, None)
    assert ways(pd) == ["keys_disagree"]


def test_an_hr_group_excludes_indicators_neither_high_frequency_nor_cluster():
    """A monthly report under the shared title: Y is neither high frequency nor a cluster's, so the monthly
    rows are X's (a cluster's); X has no quarterly report, so its Reached reads them."""
    pd = make_pd("B")
    indicator(pd, X, cluster="Child Protection cluster")
    indicator(pd, Y)
    row(pd, number="HR1")
    gate()
    assert "high_frequency" in proof()["enabled"]
    entries = grid(pd)
    assert (entries[X].reached, entries[X].report_type, entries[Y].reached) == (40.0, "HR", None)
    assert entries[X].placed_by == "high_frequency"


def test_planned_locations_only_after_a_whole_pd_indicators_read():
    """X plans Akkar, Y Bekaa: a row at Akkar is X's only once the latest PD indicators sync read them whole
    (a read that skipped its removal may hold Y's Akkar row no longer, or miss it)."""
    pd = make_pd("B")
    indicator(pd, X, places=("Akkar",))
    indicator(pd, Y, places=("Bekaa",))
    row(pd)
    gate(indicators={"places": ("Akkar",)})
    proof()
    assert grid(pd)[X].reached is None  # no PD indicators sync yet
    whole(removal_skipped=True)
    proof()
    assert grid(pd)[X].reached is None
    whole()
    proof()
    assert (grid(pd)[X].reached, grid(pd)[X].placed_by) == (40.0, "locations")


def test_planned_locations_are_off_after_a_round_pd_indicators_count():
    """A PD indicators read that announced 5,000 (a limit of the Datamart's answer, not what it holds) did not
    read every planned place: the planned locations decide nothing."""
    pd = world("locations")
    whole(count=5_000)
    proof()
    assert not identity.indicators_read_whole()
    assert (grid(pd)[X].reached, grid(pd)[Y].reached) == (None, None)


def test_the_active_tie_breaker_adds_its_note():
    pd = world("active")
    proof()
    entry = grid(pd)[X]
    assert entry.reached == 40.0 and identity.ACTIVE_NOTE in entry.notes
    assert identity.ACTIVE_NOTE in entry.as_dict()["data_notes"]
    assert identity.ACTIVE_NOTE in monitoring.as_rows([entry])[0].notes


def test_disaggregations_decide_only_when_every_row_names_the_same_types():
    """X has Sex, Y Sex and Nationality. QPR1's Akkar row names Sex, its Bekaa row Sex and Nationality (the
    group may hold both copies): it stays out. QPR2's rows all name Sex: X's."""
    pd = make_pd("B")
    indicator(pd, X, disaggregations=("Sex",))
    indicator(pd, Y, disaggregations=("Sex", "Nationality"))
    row(pd, 40, values={f"({MALE},)": 10, f"({FEMALE},)": 30})
    row(pd, 30, place="Bekaa", values={f"({MALE},)": 10, f"({LEBANESE},)": 30})
    for place in ("Akkar", "Bekaa"):
        row(pd, 20, number="QPR2", place=place, values={f"({MALE},)": 5, f"({FEMALE},)": 15})
    gate(indicators={"disaggregations": ("Sex",)}, rows={"values": {f"({MALE},)": 2, f"({FEMALE},)": 3}})
    labels()
    assert "disaggregations" in proof()["enabled"]
    entries = grid(pd)
    assert (entries[X].reached, entries[X].reports, entries[Y].reached) == (40.0, 1, None)
    assert ways(pd) == ["disaggregations", "shared_output"]


def test_a_calibration_of_another_version_is_ignored():
    """A proof stored under another rule is never applied: no tie-breaker until the next sync stores one."""
    pd = world("high_frequency")
    stored = identity.calibrate()
    proof({**stored, "version": identity.CALIBRATION_VERSION - 1})
    assert identity.calibration().enabled == frozenset()
    assert (grid(pd)[X].reached, grid(pd)[Y].reached) == (None, None)
    proof(stored)
    assert "high_frequency" in identity.calibration().enabled
    assert grid(pd)[X].reached == 40.0


def test_data_quality_counts_an_indicator_left_out_only_when_rows_of_its_title_were():
    """The same-title list: X and Y share their title and output. With their rows left out both are left out;
    once high frequency gives X its rows, Y (PRP reports only X) is not; a pair whose title has no row at all
    is not left out either (it is simply not reported: Without Reached counts it)."""
    pd = world("high_frequency")
    indicator(pd, 601, "# of tents", "4.1 Shelter")
    indicator(pd, 602, "# of tents", "4.1 Shelter")
    filters = monitoring.Filters(pd_ids=[pd.id], scope="all", year=None)

    def left_out() -> dict[str, int]:
        quality = monitoring.data_quality(monitoring.indicators(filters, TODAY), filters, TODAY)
        return {g["title"]: g["left_out"] for g in quality["same_title"]}

    assert left_out() == {CHILDREN: 2, "# of tents": 0}
    proof()
    assert left_out() == {CHILDREN: 0, "# of tents": 0}


# ----------------------------------------------------------------------------- stored, read everywhere
def make_client() -> DatamartClient:
    return DatamartClient(
        "https://datamart.test",
        "svc-user",
        "not-a-real-secret",
        country="Lebanon",
        session=make_session("", backoff=0),
    )


@responses.activate
def test_the_calibration_is_stored_by_the_sync_and_read_by_every_page():
    """The partner reports sync stores the proof in its run (``details.identity``, counts and words only);
    Partner monitoring, Report/Location, the report pop-up and check 7 then place the shared rows alike: X (a
    percentage, high frequency) gets them, and its type is the one its report's lines read."""
    pd = make_pd("B")
    indicator(pd, X, hf=True, display_type="percentage", unit="percentage")
    indicator(pd, Y)
    shared = row(pd, hf=True, total={"v": 40, "d": 100, "c": 40})
    gate()
    responses.get(
        "https://datamart.test/api/latest/prp/datareport/",
        json={"count": 0, "next": None, "results": []},
    )
    (run,) = sync.sync_all(only=["partner_reports"], client=make_client())
    stored = run.details["identity"]
    assert stored["version"] == identity.CALIBRATION_VERSION and "high_frequency" in stored["enabled"]
    assert CHILDREN not in json.dumps(stored) and "Partner" not in json.dumps(stored)
    assert identity.calibration().run_id == run.pk
    entries = grid(pd)
    assert (entries[X].reached, entries[X].placed_by, entries[Y].reached) == (40.0, "high_frequency", None)
    data = services.partner_reporting({}, TODAY)
    lines = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    (line,) = [r for r in lines if r["indicator"] == CHILDREN]
    assert line["percentage"] is True and line["value_in_period"] == 40.0
    detail = services.report_detail(
        reached.report_key(dm.ReportedIndicator.objects.values().get(pk=shared.pk))
    )
    (item,) = [i for i in detail["indicators"] if i["indicator"] == CHILDREN]
    assert item["percentage"] is True and item["total"] == 40.0
    numbers = dict(one("7")["figures"])
    assert numbers["… placed on the pages by a tie-breaker"] == 1 and numbers["… still left out"] == 0


def test_the_lines_of_shared_copies_read_their_common_type_or_none():
    """With no proof, a report line under a shared title and output reads the type its copies share, and none
    (a number) when they differ: never the one the database returns first."""
    pd = make_pd("B")
    indicator(pd, X, display_type="percentage", unit="percentage")
    indicator(pd, Y, display_type="percentage", unit="percentage")
    row(pd, total={"v": 40, "d": 100, "c": 40})
    data = services.partner_reporting({}, TODAY)
    (line,) = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert line["percentage"] is True
    dm.PDIndicator.objects.filter(source_id=Y).update(display_type="number", unit="number")
    (line,) = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert line["percentage"] is False
