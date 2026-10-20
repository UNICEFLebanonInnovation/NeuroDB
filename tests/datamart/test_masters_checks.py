"""Reporting check 12 "Indicator grouping" and Data health's eTools master indicators without a target.

Check 12 counts only (never a PD indicator's title, a partner's or a master indicator's name): the PD indicators
the nightly job reads, with and without an eTools id and a CP output, the keys of their records, the percentage
and ratio report rows that carry a numerator and a denominator, the job's dry run (written nowhere, the same
counts the job then records) and, once master indicators are live, where the grouping stands. The warning
counts the master indicators listed this year (as the Master indicators tab lists them) whose target for the
year is not typed. Generic names only: Partner A, PD/2026/001."""

from __future__ import annotations

import datetime
import json

import pytest
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import checks, master_links, masters
from neurodb.datamart import models as dm
from neurodb.web import health

from . import masters_world as w
from .masters_world import day

pytestmark = pytest.mark.django_db

KEY = "etools_masters_without_target"
OUTPUT = "1.1 Children access quality learning"
ENROLLED = "# of children enrolled in NFE"
ENROLLED_AGAIN = "# of Children enrolled in NFE"  # another partner's wording, with no eTools id
PASSED = "% of children passing the learning assessment"
TEACHERS = "# of teachers trained on inclusive education"
SESSIONS = "# of awareness sessions held"
SHARED = "Share of children attending regularly"  # two indicators of one PD, told apart by their PD output
TITLES = (ENROLLED, ENROLLED_AGAIN, PASSED, TEACHERS, SESSIONS, SHARED)


@pytest.fixture
def world(db):
    """Four programme documents (PD/2026/001 to 004; the fourth ran in 2025 only) of Partners A to D. The
    enrolment indicator is in all four (once without an eTools id, once with its CP output from its PD
    only); a percentage, reported once with its numerator and denominator and once without; an indicator of
    its own; one that names no section."""
    pds = {
        "A": w.programme("A", 1),
        "B": w.programme("B", 2),
        "C": w.programme("C", 3, cp_outputs=[OUTPUT]),
        "D": w.programme("D", 4, start=datetime.date(2025, 1, 1), end=datetime.date(2025, 12, 31)),
    }
    w.indicator(pds["A"], 101, ENROLLED, cp_output=OUTPUT, target=(500, None))
    w.indicator(
        pds["A"],
        102,
        PASSED,
        display_type="percentage",
        cp_output=OUTPUT,
        target=(80, 100),
        data={"id": 102, "cluster_indicator_id": 7, "blueprint": None},
    )
    w.indicator(pds["B"], 201, ENROLLED, cp_output=OUTPUT, target=(300, None))
    w.indicator(pds["B"], 202, TEACHERS)  # no CP output: its PD names none either
    w.indicator(pds["C"], None, ENROLLED_AGAIN, target=(200, None))
    w.indicator(pds["C"], 301, SESSIONS, section="")
    w.indicator(pds["D"], 401, ENROLLED, cp_output=OUTPUT, target=(100, None))
    w.report(pds["A"], 101, ENROLLED, "QPR1", day(3, 31), {"Akkar": 10})
    w.report(pds["A"], 102, PASSED, "QPR1", day(3, 31), {"Akkar": w.pct(40, 100)})
    w.report(pds["A"], 102, PASSED, "QPR2", day(6, 30), {"Akkar": {"c": 45}})  # PRP sent the % alone
    w.report(pds["A"], 102, PASSED, "QPR3", day(9, 30), {"Akkar": None})  # nothing reported
    return pds


def grouping() -> dict:
    """Check 12, as a stored run keeps it (plain JSON)."""
    with connection.cursor() as cursor:
        found = checks._grouping(cursor)
    return json.loads(json.dumps(found, cls=DjangoJSONEncoder))


def figures(found: dict) -> dict:
    return dict(found["figures"])


def section_rows(found: dict) -> dict:
    columns = found["table"]["columns"]
    return {row[0]: dict(zip(columns[1:], row[1:], strict=True)) for row in found["table"]["rows"]}


def no_title_or_name(found: dict, *more: str) -> None:
    text = json.dumps(found)
    for word in (*TITLES, *more, "Partner A", "Partner B", "PD/2026/001", "PD/2026/004"):
        assert word not in text, word


# ---------------------------------------------------------------------------------------- check 12
def test_check_12_is_listed_last_and_runs_with_the_others(world):
    found = checks.run_all()
    assert [c["key"] for c in found] == [str(n) for n in range(1, 13)]
    twelve = found[-1]
    assert twelve["title"] == "Indicator grouping" and "unfinished" not in twelve
    assert "never averaged" in twelve["neurodb"] and "no AI" in twelve["neurodb"]


def test_before_any_master_the_dry_run_counts_what_the_job_would_propose_and_writes_nothing(world):
    found = grouping()
    got = figures(found)
    assert got["PD indicators the nightly job reads (programme documents running in 2025 or 2026)"] == 7
    assert got["… of programme documents running in 2026"] == 6
    assert got["… without an eTools id (grouped by their title)"] == 1
    assert got["… with a CP output (from their reports, or their PD's only one)"] == 6
    assert got["… percentages or ratios"] == 1
    assert got["Master indicators the next run would propose"] == 1
    assert got["… from PD indicators"] == 4  # the enrolment indicator of the four programme documents
    assert got["PD indicators left on their own"] == 2  # the percentage and the teachers' indicator
    assert got["Active master indicators"] == 0 and got["PD indicators in an active master indicator"] == 0
    assert found["tone"] == checks.INFO
    assert (
        "No master indicator is active yet: the nightly job would propose 1 master indicator from 4 PD "
        "indicators; 2 would be left on their own." in found["verdict"]
    )
    assert "1 without an eTools id (14 %), grouped by their title alone" in found["verdict"]
    rows = section_rows(found)
    assert rows["Education"] == {
        "PD indicators": 6,
        "Without an eTools id": 1,
        "With a CP output": 5,
        "In a master indicator": 0,
        "Suggested": 0,
        "Ambiguous": 0,
        "Proposed master indicators": 1,
        "PD indicators in them": 4,
        "On their own": 2,
    }
    assert rows[checks.NO_SECTION_LABEL]["PD indicators"] == 1  # listed last; it never joins a master
    assert [row[0] for row in found["table"]["rows"]][-1] == checks.NO_SECTION_LABEL
    # written nowhere
    assert not dm.EtoolsMaster.objects.exists() and not dm.EtoolsMasterLink.objects.exists()
    no_title_or_name(found)


def test_the_dry_run_gives_the_counts_the_job_then_records(world):
    before = figures(grouping())
    run = master_links.run(w.YEAR, "test")
    assert isinstance(run, SyncRun) and run.status == SyncRun.Status.SUCCEEDED
    totals = run.details["totals"]
    assert totals["proposed"] == before["Master indicators the next run would propose"]
    assert totals["in_proposals"] == before["… from PD indicators"]
    assert totals["unattached"] == before["PD indicators left on their own"]
    assert run.rows_in == 6  # the candidates: every PD indicator that names a section


def test_the_percentage_rows_and_the_record_keys_are_counted(world):
    found = grouping()
    got = figures(found)
    # the "()" total with v and d; the % alone; a row with nothing reported is not counted
    assert got["Percentage and ratio report rows with a value"] == 2
    assert got["… with their numerator and denominator"] == 1
    assert (
        "1 of 2 percentage and ratio report rows with a value (50 %) carry their numerator"
        in found["verdict"]
    )
    keys = {row[0]: row for row in found["tables"][0]["rows"]}
    assert found["tables"][0]["title"] == "Keys of the PD indicator records"
    assert keys["cluster_indicator_id"] == ["cluster_indicator_id", 1, 1, "yes"]
    assert keys["blueprint"] == ["blueprint", 1, 0, "yes"]  # named, but never with a value
    assert keys["id"][3] == ""
    assert got["… that may name one indicator across programme documents"] == 1
    assert (
        "The PD indicator records carry cluster_indicator_id, which may name one indicator"
        in found["verdict"]
    )


def test_a_ratio_by_its_method_alone_counts_with_the_percentages(world):
    dm.ReportedIndicator.objects.all().delete()
    w.report(
        world["B"],
        202,
        TEACHERS,
        "QPR1",
        day(3, 31),
        {"Akkar": w.pct(3, 4)},
        calculation_across_periods="ratio",
    )
    w.report(world["A"], 101, ENROLLED, "QPR1", day(3, 31), {"Akkar": 10})  # a number: never counted here
    got = figures(grouping())
    assert got["Percentage and ratio report rows with a value"] == 1
    assert got["… with their numerator and denominator"] == 1


def test_a_report_row_is_a_percentage_by_the_indicator_partner_monitoring_places_it_under(world):
    """Two indicators of PD/2026/002 share a title (a percentage under one PD output, a number under another)
    and their rows name no eTools id: each row is placed by its PD output and title, as Partner monitoring and
    check 7 place it (monitoring._Idents), so only the row under the percentage counts here; a row whose PD
    output matches neither indicator is placed under none and counts by its methods alone (none here)."""
    dm.ReportedIndicator.objects.all().delete()
    w.indicator(world["B"], 203, SHARED, display_type="percentage")
    w.indicator(world["B"], 204, SHARED)
    dm.PDIndicator.objects.filter(source_id=203).update(lower_result_name="Output A")
    dm.PDIndicator.objects.filter(source_id=204).update(lower_result_name="Output B")
    for output, value in (("Output A", w.pct(3, 4)), ("output b", w.pct(5, 10)), ("Output C", w.pct(1, 2))):
        w.report(world["B"], None, SHARED, "QPR1", day(3, 31), {"Akkar": value}, pd_output=output)
    got = figures(grouping())
    assert got["Percentage and ratio report rows with a value"] == 1
    assert got["… with their numerator and denominator"] == 1


def test_once_live_it_counts_where_the_grouping_stands_and_warns(world):
    master_links.run(w.YEAR, "test")
    (proposal,) = dm.EtoolsMaster.objects.filter(status="proposed")
    assert master_links.accept([proposal], "test").links.confirmed == 4
    master = dm.EtoolsMaster.objects.get(pk=proposal.pk)
    w.link(master, world["A"], 999)  # its PD runs, its indicator is gone
    w.link(master, world["A"], 102, role="shown", unit_class="number")  # a percentage now
    found = grouping()
    got = figures(found)
    assert got["Active master indicators"] == 1 and got["… listed in 2026"] == 1
    assert got["… without a target for 2026"] == 1
    assert got["PD indicators in an active master indicator"] == 5  # the four, and the one shown
    assert got["PD indicators left on their own"] == 1  # the teachers' indicator
    assert got["Confirmed links no longer in eTools"] == 1
    assert got["Confirmed links whose type changed in eTools"] == 1
    assert got["Master indicators the next run would propose"] == 0
    assert found["tone"] == checks.WARN
    assert (
        "5 PD indicators in an active master indicator, 0 suggested for one and 0 that two fit as well; 1 in "
        "none and suggested for none." in found["verdict"]
    )
    assert (
        "1 of the 1 master indicator listed in 2026 has no target for 2026; 1 confirmed link no longer in "
        "eTools, 1 of another type now." in found["verdict"]
    )
    assert section_rows(found)["Education"]["In a master indicator"] == 5
    no_title_or_name(found, master.name)
    # every link right and the year's target typed: nothing to do
    dm.EtoolsMasterLink.objects.filter(ident="s999").delete()
    dm.EtoolsMasterLink.objects.filter(ident="s102").delete()
    dm.EtoolsMasterYear.objects.create(master=master, year=w.YEAR, target=1000)
    found = grouping()
    assert figures(found)["… without a target for 2026"] == 0 and found["tone"] == checks.OK


def test_the_page_shows_check_12_with_its_two_tables(client, admin_user, world):
    client.force_login(admin_user)
    page = client.get(reverse("admin:datamart_reporting_checks"))
    assert page.status_code == 200
    assert "Check 12 · Indicator grouping" in page.text
    assert "By section" in page.text and "Keys of the PD indicator records" in page.text
    assert "cluster_indicator_id" in page.text
    for word in (*TITLES, "Partner A", "PD/2026/001"):
        assert word not in page.text, word


def test_on_an_empty_database_check_12_says_there_is_nothing_yet(db):
    found = grouping()
    assert found["tone"] == checks.INFO
    assert found["verdict"].startswith(
        "No PD indicator of a programme document running in 2025 or 2026 is held"
    )
    assert found["table"]["rows"] == [] and found["tables"][0]["rows"] == []


# -------------------------------------------------------------------------------- the warning
def warning():
    return {line.key: line for line in health.warnings()}.get(KEY)


def test_a_master_listed_this_year_without_a_target_is_a_warning_until_one_is_typed(world):
    master = w.master(ENROLLED)
    w.link(master, world["A"], 101)
    line = warning()
    assert line is not None and line.text == "1 eTools master indicator has no target for 2026."
    assert line.url == reverse("admin:datamart_etoolsmaster_changelist") + "?target=listed"
    assert line.value == 1 and line.read == "eTools master indicators without a target for 2026"
    assert health.about_activityinfo(KEY) is False  # followed whatever the ActivityInfo import does
    year = dm.EtoolsMasterYear.objects.create(master=master, year=w.YEAR, target=0)
    assert warning() is not None  # 0 is no target: the status reads "No target"
    year.target = 500
    year.save()
    assert warning() is None


def test_only_the_masters_listed_this_year_count(world):
    listed = w.master("Listed by its year", target=None, code="AWP-1")  # its year row, no target
    switched_off = w.master("Switched off this year")
    dm.EtoolsMasterYear.objects.create(master=switched_off, year=w.YEAR, is_active=False)
    w.link(switched_off, world["B"], 201)
    w.master("No link and no year")
    last_year = w.master("Linked to last year's programme document")
    w.link(last_year, world["D"], 401)
    proposed = w.master("Proposed", status="proposed")
    w.link(proposed, world["A"], 101, status="suggested")
    assert [m.pk for m in masters.without_target(w.YEAR)] == [listed.pk]
    assert warning().text == "1 eTools master indicator has no target for 2026."
    other = w.master("Another one")
    w.link(other, world["C"], 301)
    assert warning().text == "2 eTools master indicators have no target for 2026."


def test_the_admin_list_the_warning_opens_holds_the_master(client, admin_user, world):
    master = w.master(ENROLLED)
    w.link(master, world["A"], 101)
    client.force_login(admin_user)
    page = client.get(warning().url)
    assert page.status_code == 200 and ENROLLED in page.text


def test_the_list_the_warning_opens_holds_exactly_the_masters_it_counts(client, admin_user, world):
    """The count and the list agree: not a master whose year is switched off, nor one with no link and no
    year (not listed this year), and a target of 0 or less is none on both."""
    linked = w.master(ENROLLED)
    w.link(linked, world["A"], 101)
    unticked = w.master("Switched off this year")
    dm.EtoolsMasterYear.objects.create(master=unticked, year=w.YEAR, is_active=False)
    w.master("No link and no year")
    negative = w.master("Typed below 0")
    dm.EtoolsMasterYear.objects.create(master=negative, year=w.YEAR, target=-5)
    line = warning()
    client.force_login(admin_user)
    found = client.get(line.url).context["cl"]
    assert found.result_count == line.value == 2
    assert {m.pk for m in found.result_list} == {linked.pk, negative.pk}


def test_no_master_no_warning(db):
    assert warning() is None
