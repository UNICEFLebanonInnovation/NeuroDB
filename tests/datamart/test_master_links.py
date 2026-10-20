"""The nightly suggestions and proposals of the eTools master indicators (neurodb.datamart.master_links), with no
AI: which PD indicators are suggested for which master (a replacement, the same structure, a similar title), when
an indicator is ambiguous, which masters are proposed, the roles they start with, what a run again keeps, and the
run's record. Nothing counts until someone confirms it."""

from __future__ import annotations

import json
from collections import Counter
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from neurodb.core import jobs
from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import curation, master_links, masters, masters_tab, monitoring
from neurodb.datamart import models as dm
from neurodb.integrations import background

from . import masters_world as w
from .masters_world import day

pytestmark = pytest.mark.django_db

JOB = SyncRun.Job.ETOOLS_MASTERS
OUTPUT = "1.1 Children access quality learning"
OTHER_OUTPUT = "2.3 Children are protected from violence"
ENROLLED = "# of children enrolled in NFE"
TRAINED = "# of caregivers trained on positive parenting"


@pytest.fixture
def pds(db):
    """Four running programme documents of four partners: PD/2026/001 to PD/2026/004."""
    return {letter: w.programme(letter, n) for n, letter in enumerate("ABCD", start=1)}


def run(year: int = w.YEAR) -> SyncRun:
    found = master_links.run(year, "test")
    assert isinstance(found, SyncRun), found
    assert found.status == SyncRun.Status.SUCCEEDED, found.error
    return found


def links(**filters) -> dict[tuple[str, str], dm.EtoolsMasterLink]:
    """The links by (PD number, ident)."""
    found = dm.EtoolsMasterLink.objects.filter(**filters).select_related("master")
    return {(link.pd_number, link.ident): link for link in found}


def suggested() -> dict[tuple[str, str], dm.EtoolsMasterLink]:
    return links(status="suggested", master__status="active")


def proposals() -> list[dm.EtoolsMaster]:
    return list(dm.EtoolsMaster.objects.filter(status="proposed").order_by("id"))


def state() -> tuple[list, list]:
    """Everything a run writes or keeps, without ids or times: two runs over the same data compare equal."""
    found = sorted(
        (m.section, m.name, m.method, m.status, m.origin, m.proposal_key, m.cp_output, m.updated_by)
        for m in dm.EtoolsMaster.objects.all()
    )
    rows = sorted(
        (x.master.name, x.pd_id, x.ident, x.role, x.status, x.score, x.reason, x.title, x.unit_class)
        for x in dm.EtoolsMasterLink.objects.select_related("master")
    )
    return found, rows


def family(*pairs, title=ENROLLED, cp_output=OUTPUT, **extra) -> None:
    """The same PD indicator in several programme documents: (pd, source id) pairs."""
    for pd, source_id in pairs:
        w.indicator(pd, source_id, title, cp_output=cp_output, **extra)


def collected(pd, source_id):
    """A PD indicator as the job reads it (no Reached)."""
    return monitoring.collect(monitoring.Filters(pd_ids=[pd.id], scope="all"))[(pd.id, f"s{source_id}")]


# ------------------------------------------------------------------------------------------------ proposals
def test_a_family_spanning_two_programme_documents_is_proposed_and_one_alone_is_not(pds):
    a, b, c, d = pds["A"], pds["B"], pds["C"], pds["D"]
    w.indicator(a, 101, ENROLLED, cp_output=OUTPUT)
    w.indicator(b, 201, "Number of girls and boys enrolled in NFE (6-14)", cp_output=OUTPUT)
    w.indicator(d, 401, "# of boys enrolled in NFE")  # no CP output known: joins the one family of its title
    w.indicator(c, 301, TRAINED, cp_output=OUTPUT)  # one programme document only
    w.indicator(c, 302, ENROLLED, section=w.CP, cp_output=OUTPUT)  # another section: alone there
    sync = run()

    (proposal,) = proposals()
    assert (proposal.section, proposal.name, proposal.method, proposal.origin, proposal.cp_output) == (
        w.EDUCATION,
        ENROLLED,  # the shortest title without gender, nationality or disability
        "sum",
        "proposed",
        OUTPUT,
    )
    assert proposal.proposal_key == masters.structure_key(collected(a, 101))
    found = {
        (x.pd_number, x.ident): (x.role, x.status, x.score, x.reason, x.unit_class)
        for x in proposal.links.all()
    }
    reason = "Same CP output, type and title in 3 programme documents"
    assert found == {
        ("PD/2026/001", "s101"): ("total", "suggested", 0.95, reason, ""),
        ("PD/2026/002", "s201"): ("total", "suggested", 0.95, reason, ""),
        ("PD/2026/004", "s401"): ("total", "suggested", 0.95, reason, ""),  # its type is taken when confirmed
    }
    counts = sync.details["sections"]
    assert counts["Education"] == {
        "proposed": 1,
        "in_proposals": 3,
        "suggested": 0,
        "replacements": 0,
        "ambiguous": 0,
        "no_longer_in_etools": 0,
        "type_changed": 0,
        "unattached": 1,  # the caregivers indicator
    }
    assert counts["Child Protection"]["unattached"] == 1 and counts["Child Protection"]["proposed"] == 0


@pytest.mark.parametrize("display_type, method", [("percentage", "percent"), ("ratio", "ratio")])
def test_a_proposal_combines_its_indicators_by_their_type(pds, display_type, method):
    w.indicator(pds["A"], 101, "% of children passing the exam", display_type=display_type, cp_output=OUTPUT)
    w.indicator(pds["B"], 201, "Percentage of children passing the exam", display_type=display_type)
    w.indicator(pds["C"], 301, "# of children passing the exam", cp_output=OUTPUT)  # a number: another family
    run()
    (proposal,) = proposals()
    assert proposal.method == method
    assert {x.pd_number for x in proposal.links.all()} == {"PD/2026/001", "PD/2026/002"}


def test_indicators_that_name_no_section_are_never_proposed_nor_suggested(pds):
    family((pds["A"], 101), (pds["B"], 201), section="")
    sync = run()
    assert not dm.EtoolsMaster.objects.exists() and not dm.EtoolsMasterLink.objects.exists()
    assert sync.rows_in == 0 and sync.details["sections"] == {}


def test_the_candidates_are_this_years_and_last_years_programme_documents_and_no_reached_is_computed(
    pds, monkeypatch
):
    last = w.programme("E", 5, start=day(1, 1, 2025), end=day(12, 31, 2025))
    both = w.programme("F", 6, start=day(1, 1, 2025), end=day(12, 31, 2026))  # read once
    older = w.programme("G", 7, start=day(1, 1, 2024), end=day(12, 31, 2024))
    draft = w.programme("H", 8, status="draft")
    family((pds["A"], 101), (last, 501), (both, 601), (older, 701), (draft, 801))
    monkeypatch.setattr(monitoring, "_attach_reached", lambda *args: pytest.fail("the job computed Reached"))
    sync = run()
    (proposal,) = proposals()
    assert {x.pd_number for x in proposal.links.all()} == {"PD/2026/001", "PD/2026/005", "PD/2026/006"}
    assert sync.rows_in == 3


def test_a_dismissed_proposal_is_never_proposed_again(pds):
    family((pds["A"], 101), (pds["B"], 201))
    run()
    (proposal,) = proposals()
    assert master_links.dismiss([proposal], "staff") == 1
    proposal.refresh_from_db()
    assert (proposal.status, proposal.updated_by) == ("dismissed", "staff")
    assert not proposal.links.exists()  # its suggestions went with it
    stamp = proposal.updated_at

    family((pds["C"], 301))  # another programme document joins the family later
    sync = run()
    assert not proposals() and not dm.EtoolsMasterLink.objects.exists()
    proposal.refresh_from_db()
    assert (proposal.status, proposal.updated_at) == ("dismissed", stamp)  # never touched
    assert sync.details["totals"]["unattached"] == 3
    assert master_links.dismiss([proposal], "staff") == 0  # dismissed already


def test_accept_makes_the_proposal_active_and_confirms_its_links_which_then_count(pds):
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    w.report(a, 101, ENROLLED, "QPR1", day(3, 31), {"Akkar": 120})
    w.report(b, 201, ENROLLED, "QPR1", day(3, 31), {"Akkar": 80})
    run()
    (proposal,) = proposals()
    assert w.figure(proposal).value is None  # suggestions are never counted

    done = master_links.accept([proposal], "staff")
    assert (done.accepted, done.refused, done.links.confirmed, done.links.left) == ([proposal], [], 2, [])
    proposal.refresh_from_db()
    assert (proposal.status, proposal.updated_by) == ("active", "staff")
    assert {(x.status, x.confirmed_by, x.unit_class, x.title) for x in proposal.links.all()} == {
        ("confirmed", "staff", "number", ENROLLED)
    }
    fig = w.figure(proposal)
    assert (fig.value, fig.coverage) == (200.0, "counted 2 of 2 PDs")

    run()  # the next night: its key is held and its indicators have their home
    assert not proposals() and not links(status="suggested")
    assert master_links.accept([proposal], "staff").refused == [(proposal, master_links.NOT_PROPOSED)]


def test_accept_refuses_a_name_the_section_has_already_and_changes_nothing(pds):
    family((pds["A"], 101), (pds["B"], 201))
    run()
    (proposal,) = proposals()
    w.master(ENROLLED)  # made by hand since
    done = master_links.accept([proposal], "staff")
    assert done.accepted == [] and done.refused == [
        (
            proposal,
            f"Education already has a master indicator named {ENROLLED}: rename the proposal first, or merge "
            "it into that one.",
        )
    ]
    proposal.refresh_from_db()
    assert proposal.status == "proposed"
    assert {x.status for x in proposal.links.all()} == {"suggested"}


def test_a_new_proposal_avoids_a_name_its_section_has(pds):
    w.master(ENROLLED, "percent")  # a master of percentages: no number is suggested for it
    family((pds["A"], 101), (pds["B"], 201))
    w.indicator(pds["C"], 301, "Number of children enrolled in NFE", cp_output=OUTPUT)
    run()
    assert [p.name for p in proposals()] == ["Number of children enrolled in NFE"]  # the next shortest


# ----------------------------------------------------------------------------------------------- the rules
def test_the_structure_of_a_confirmed_total_suggests_its_master_and_a_similar_title_never_scores_as_strong(
    pds,
):
    a, b, c = pds["A"], pds["B"], pds["C"]
    w.indicator(a, 101, ENROLLED, cp_output=OUTPUT)
    w.indicator(b, 201, "# of girls and boys enrolled in NFE", cp_output=OUTPUT)
    w.indicator(c, 301, ENROLLED, cp_output=OTHER_OUTPUT)  # another CP output: another structure
    m = w.master("Children enrolled in non-formal education")
    w.link(m, a, 101, title=ENROLLED)
    sync = run()

    found = suggested()
    same = found[("PD/2026/002", "s201")]
    assert (same.master, same.role, same.score, same.reason) == (
        m,
        "total",
        0.95,
        "Same CP output, type and title as 1 confirmed indicator",
    )
    similar = found[("PD/2026/003", "s301")]  # every word of a confirmed total's title
    assert (similar.master, similar.score, similar.reason) == (m, 0.89, "Similar title")
    assert similar.score < master_links.STRONG <= same.score  # "confirm 0.9 or more" takes structures only
    assert not proposals()
    assert sync.details["sections"]["Education"]["suggested"] == 2


def test_a_similar_title_scores_the_masters_words_with_a_bonus_for_its_cp_output(pds):
    w.indicator(pds["A"], 101, ENROLLED, cp_output=OUTPUT)
    m = w.master("Children enrolled in NFE programmes", cp_output=OUTPUT)
    run()
    link = suggested()[("PD/2026/001", "s101")]
    assert (link.master, link.score, link.reason) == (m, 0.85, "Similar title")  # 3 of its 4 words, and 0.1
    m.cp_output = OTHER_OUTPUT
    m.save()
    run()
    assert suggested()[("PD/2026/001", "s101")].score == 0.75


def test_the_keywords_help_a_type_the_method_does_not_take_never_and_an_unknown_age_group_never(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    w.indicator(a, 101, ENROLLED)
    w.indicator(b, 201, "# of youth enrolled in NFE")
    w.indicator(c, 301, "% of children enrolled in NFE", display_type="percentage")  # a sum adds numbers
    m = w.master("Out-of-school learners", keywords="NFE enrolled")
    run()
    found = suggested()
    assert {key: (x.master, x.score) for key, x in found.items()} == {
        ("PD/2026/001", "s101"): (m, 0.89),
        ("PD/2026/002", "s201"): (m, 0.89),  # no confirmed total yet: any age group
    }
    assert master_links.confirm([found[("PD/2026/001", "s101")]], "staff").confirmed == 1
    sync = run()
    assert not suggested()  # the youth are none of its confirmed totals' age group (children)
    assert sync.details["sections"]["Education"]["unattached"] == 2


def test_an_indicator_no_longer_in_etools_is_flagged_and_its_replacement_suggested_never_moved(pds):
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    m = w.master("Children enrolled in NFE")
    old = w.link(m, a, 101, title=ENROLLED)
    w.link(m, b, 201, title=ENROLLED)
    # eTools replaces PD A's indicator by another: a new id, its title reworded
    dm.PDIndicator.objects.filter(source_id=101).delete()
    w.indicator(a, 105, "Number of children (6-14) enrolled in NFE", cp_output=OUTPUT)
    stamp = old.updated_at
    sync = run()

    link = suggested()[("PD/2026/001", "s105")]
    assert (link.master, link.role, link.score, link.reason) == (
        m,
        "total",
        1.0,
        "Replaces an indicator no longer in eTools",
    )
    old.refresh_from_db()
    assert (old.ident, old.status, old.updated_at) == ("s101", "confirmed", stamp)  # never moved
    counts = sync.details["sections"]["Education"]
    assert (counts["no_longer_in_etools"], counts["replacements"], counts["suggested"]) == (1, 1, 1)
    sub = w.subs(w.figure(m))["s101"]
    assert (sub.reason, sub.counted) == (masters.GONE, False)


def test_a_replacement_is_suggested_in_every_master_whose_link_it_replaces_with_that_role(pds):
    """PD A's indicator is in one master's total and another's denominator. eTools replaces it (an amendment:
    the same title, a new id): the replacement is suggested in both, as each had it. They are no rivals: only
    one of them holds it in its total."""
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    completing = "# of learners completing the NFE cycle"
    w.indicator(a, 105, completing, cp_output=OUTPUT)
    enrolled = w.master("Children enrolled in NFE")
    w.link(enrolled, a, 101, title=ENROLLED)
    w.link(enrolled, b, 201, title=ENROLLED)
    retention = w.master("Retention in NFE", "percent")
    w.link(retention, a, 105, title=completing, role="numerator")
    w.link(retention, a, 101, title=ENROLLED, role="denominator")
    dm.PDIndicator.objects.filter(source_id=101).delete()
    w.indicator(a, 102, ENROLLED, cp_output=OUTPUT)
    sync = run()

    found = {
        x.master.name: (x.role, x.score, x.reason)
        for x in dm.EtoolsMasterLink.objects.filter(status="suggested", ident="s102").select_related("master")
    }
    assert found == {
        "Children enrolled in NFE": ("total", 1.0, master_links.REPLACES),
        "Retention in NFE": ("denominator", 1.0, master_links.REPLACES),
    }
    counts = sync.details["sections"]["Education"]
    assert (counts["replacements"], counts["ambiguous"], counts["no_longer_in_etools"]) == (2, 0, 2)
    rows = master_links.pool(w.YEAR)
    assert (
        master_links.ambiguous(rows, [enrolled, retention], list(dm.EtoolsMasterLink.objects.all()), rows)
        == {}
    )

    # two totals it would replace are rivals: neither gets it, the denominator still does
    other = w.master("Children in non-formal education")
    w.indicator(a, 103, "# of children enrolled in NFE (6-14)", cp_output=OTHER_OUTPUT)
    w.link(other, a, 103, title="# of children enrolled in NFE (6-14)")
    dm.PDIndicator.objects.filter(source_id=103).delete()
    sync = run()
    found = {
        x.master.name: x.role
        for x in dm.EtoolsMasterLink.objects.filter(status="suggested", ident="s102").select_related("master")
    }
    assert found == {"Retention in NFE": "denominator"}
    assert master_links.plan(master_links.pool(w.YEAR), w.YEAR).ambiguous == {
        (a.id, "s102"): [enrolled, other]
    }
    assert sync.details["sections"]["Education"]["ambiguous"] == 1
    # the tab says both: where it is suggested, and that two masters want it in their total
    tab = masters_tab.read(monitoring.Filters(year=w.YEAR, sections=[w.EDUCATION]), w.TODAY)
    notes = {(x.row.pd.number, x.row.ident[1]): x.note for x in tab.unattached}
    assert notes[("PD/2026/001", "s102")] == (
        "Suggested for Retention in NFE (1.00); Ambiguous: Children enrolled in NFE or Children in non-formal "
        "education"
    )


def test_a_replacement_confirmed_in_one_masters_total_stays_suggested_in_the_others(pds):
    """The replacement is suggested in one master's total and another's denominator. A person confirms the
    total first: the next nights still suggest it as the denominator (the same link), until it is confirmed
    there too."""
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    completing = "# of learners completing the NFE cycle"
    w.indicator(a, 105, completing, cp_output=OUTPUT)
    enrolled = w.master("Children enrolled in NFE")
    w.link(enrolled, a, 101, title=ENROLLED)
    w.link(enrolled, b, 201, title=ENROLLED)
    retention = w.master("Retention in NFE", "percent")
    w.link(retention, a, 105, title=completing, role="numerator")
    w.link(retention, a, 101, title=ENROLLED, role="denominator")
    dm.PDIndicator.objects.filter(source_id=101).delete()
    w.indicator(a, 102, ENROLLED, cp_output=OUTPUT)
    run()
    total = dm.EtoolsMasterLink.objects.get(master=enrolled, ident="s102")
    denominator = dm.EtoolsMasterLink.objects.get(master=retention, ident="s102")
    assert master_links.confirm([total], "staff").confirmed == 1

    for _night in range(2):
        sync = run()
        found = {
            x.master.name: (x.role, x.status, x.pk)
            for x in dm.EtoolsMasterLink.objects.filter(ident="s102").select_related("master")
        }
        assert found == {
            "Children enrolled in NFE": ("total", "confirmed", total.pk),
            "Retention in NFE": ("denominator", "suggested", denominator.pk),
        }
        counts = sync.details["sections"]["Education"]
        assert (counts["suggested"], counts["replacements"], counts["ambiguous"]) == (1, 1, 0)
    assert master_links.confirm([denominator], "staff").confirmed == 1
    assert w.subs(w.figure(retention))["s102"].eligible
    assert run().details["totals"]["suggested"] == 0


def test_an_indicator_two_masters_fit_as_well_gets_no_suggestion_and_says_which(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    family((a, 101), (b, 201), (c, 301))
    one = w.master("Children enrolled in NFE")
    two = w.master("Children in non-formal education")
    w.link(one, a, 101, title=ENROLLED)
    w.link(two, b, 201, title=ENROLLED)
    sync = run()
    assert not suggested() and not proposals()
    found = master_links.plan(master_links.pool(w.YEAR), w.YEAR)
    assert found.ambiguous == {(c.id, "s301"): [one, two]}
    assert master_links.ambiguous_note([one, two]) == (
        "Ambiguous: Children enrolled in NFE or Children in non-formal education"
    )
    assert sync.details["sections"]["Education"]["ambiguous"] == 1


def test_a_similar_title_goes_to_the_clearly_closest_master_only(pds):
    w.indicator(pds["A"], 101, "# of children enrolled in NFE classes")
    near = w.master("Children enrolled in NFE")  # every word: 0.89
    w.master("Children enrolled in learning programmes")  # 2 of its 4 words: 0.5, under 0.6
    w.master("Children enrolled in NFE learning programmes")  # 3 of its 5 words: 0.6
    run()
    assert suggested()[("PD/2026/001", "s101")].master == near
    w.master("NFE children enrolled classes")  # every word too: as close as the first
    run()
    assert not suggested()


def test_each_indicator_gets_one_suggestion_and_counts_in_one_total_at_most(pds):
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    one = w.master("Children enrolled in NFE")
    w.link(one, a, 101, title=ENROLLED)
    w.master("Children enrolled", keywords="children enrolled NFE")  # a similar title, but rule b comes first
    run()
    assert {key: x.master for key, x in suggested().items()} == {("PD/2026/002", "s201"): one}
    homes = Counter(
        (x.pd_id, x.ident)
        for x in dm.EtoolsMasterLink.objects.filter(role="total", status__in=["suggested", "confirmed"])
    )
    assert set(homes.values()) == {1}


# --------------------------------------------------------------------------------------------- default roles
def test_the_default_roles_a_total_and_its_breakdown_girls_and_boys_and_the_largest(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    w.indicator(a, 101, ENROLLED, cp_output=OUTPUT, target=(900, None))
    w.indicator(a, 102, "# of girls enrolled in NFE", cp_output=OUTPUT, target=(450, None))
    w.indicator(b, 201, "# of girls enrolled in NFE", cp_output=OUTPUT)
    w.indicator(b, 202, "# of boys enrolled in NFE", cp_output=OUTPUT)
    w.indicator(c, 301, "# of girls enrolled in NFE", cp_output=OUTPUT, target=(100, None))
    w.indicator(c, 302, "# of girls with disabilities enrolled in NFE", cp_output=OUTPUT, target=(30, None))
    run()
    (proposal,) = proposals()
    assert {(x.pd_number, x.ident): x.role for x in proposal.links.all()} == {
        ("PD/2026/001", "s101"): "total",  # the total...
        ("PD/2026/001", "s102"): "shown",  # ...and its breakdown
        ("PD/2026/002", "s201"): "total",  # girls and boys cannot count the same child
        ("PD/2026/002", "s202"): "total",
        ("PD/2026/003", "s301"): "total",  # a disability subset may: the largest target counts
        ("PD/2026/003", "s302"): "shown",
    }
    assert proposal.name == ENROLLED


def test_a_breakdown_suggested_next_to_a_confirmed_total_is_shown_and_a_new_indicator_the_next_night(pds):
    a, b = pds["A"], pds["B"]
    w.indicator(a, 101, ENROLLED, cp_output=OUTPUT)
    m = w.master("Children enrolled in NFE")
    w.link(m, a, 101, title=ENROLLED)
    run()
    assert not suggested()
    # an amendment adds a breakdown to PD A, and PD B gets the indicator too
    w.indicator(a, 102, "# of girls enrolled in NFE", cp_output=OUTPUT)
    w.indicator(b, 201, ENROLLED, cp_output=OUTPUT)
    run()
    assert {key: (x.master, x.role) for key, x in suggested().items()} == {
        ("PD/2026/001", "s102"): (m, "shown"),
        ("PD/2026/002", "s201"): (m, "total"),
    }


def test_the_roles_of_one_programme_documents_indicators(pds):
    a = pds["A"]
    for source_id, title, target in (
        (1, ENROLLED, 100),
        (2, "# of children enrolled in NFE classes", 300),
        (3, "# of girls enrolled in NFE", 50),
        (4, "# of boys enrolled in NFE", 40),
        (5, "# of Syrian children enrolled in NFE", 20),
    ):
        w.indicator(a, source_id, title, target=(target, None))
    row = {n: collected(a, n) for n in range(1, 6)}
    key = {n: masters.key_of(row[n]) for n in row}
    assert master_links.roles([row[1]]) == {key[1]: "total"}
    assert master_links.roles([row[1], row[2]]) == {key[1]: "shown", key[2]: "total"}  # the largest target
    assert master_links.roles([row[3], row[4]]) == {key[3]: "total", key[4]: "total"}
    # girls and Syrian children may be the same children: the largest target counts
    assert master_links.roles([row[3], row[5]]) == {key[3]: "total", key[5]: "shown"}
    # next to a confirmed total the rule keeps, a new one may count; else every new one is shown
    assert master_links.roles([row[4]], kept=[row[3]]) == {key[4]: "total"}
    assert master_links.roles([row[3]], kept=[row[1]]) == {key[3]: "shown"}
    assert master_links.roles([row[1]], kept=[row[3]]) == {key[1]: "shown"}


# --------------------------------------------------------------------------------------------- a run again
def test_a_run_again_replaces_only_suggestions_and_proposals_and_keeps_what_people_decided(pds):
    a, b, c, d = pds["A"], pds["B"], pds["C"], pds["D"]
    family((a, 101), (b, 201))
    family((b, 202), (c, 301), (d, 401), title=TRAINED)
    one = w.master("Children enrolled in NFE")
    confirmed = w.link(one, a, 101, title=ENROLLED)
    run()
    before = state()
    (proposal,) = proposals()
    run()
    assert state() == before  # the same data, the same suggestions
    assert [p.pk for p in proposals()] == [proposal.pk]  # the proposal is rebuilt, not made anew

    # what people decided: a suggestion rejected, a proposal renamed, one of its links rejected
    assert master_links.reject([suggested()[("PD/2026/002", "s201")]], "staff") == 1
    proposal.name = "Caregivers trained"
    proposal.save()
    master_links.reject(proposal.links.filter(pd_number="PD/2026/004"), "staff")
    stamps = {x.pk: x.updated_at for x in dm.EtoolsMasterLink.objects.exclude(status="suggested")}
    sync = run()

    assert not suggested()  # never suggested again where it was rejected
    proposal.refresh_from_db()
    assert (proposal.status, proposal.name) == ("proposed", "Caregivers trained")
    assert {(x.pd_number, x.status, x.reason) for x in proposal.links.all()} == {
        ("PD/2026/002", "suggested", "Same CP output, type and title in 2 programme documents"),
        ("PD/2026/003", "suggested", "Same CP output, type and title in 2 programme documents"),
        ("PD/2026/004", "rejected", "Same CP output, type and title in 3 programme documents"),
    }
    assert {x.pk: x.updated_at for x in dm.EtoolsMasterLink.objects.exclude(status="suggested")} == stamps
    confirmed.refresh_from_db()
    assert confirmed.status == "confirmed"
    # in no master: the one rejected for its master, and the one rejected from the proposal
    assert sync.details["sections"]["Education"]["unattached"] == 2


def test_a_proposal_whose_family_is_gone_goes_unless_someone_decided_a_link_of_it(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    family((a, 101), (b, 201))
    family((a, 102), (c, 301), title=TRAINED)
    run()
    by_name = {p.name: p for p in proposals()}
    gone, kept = by_name[ENROLLED], by_name[TRAINED]
    master_links.reject(kept.links.filter(pd_number="PD/2026/003"), "staff")
    dm.PDIndicator.objects.filter(source_id__in=[201, 102]).delete()  # neither family spans two PDs now
    run()
    assert proposals() == [kept]  # kept for its rejected link, which stays
    assert not dm.EtoolsMaster.objects.filter(pk=gone.pk).exists()
    assert {x.status for x in kept.links.all()} == {"rejected"}


def test_a_suggestion_made_again_keeps_its_id_and_the_role_a_person_gave_it(pds):
    """A person sets a suggested breakdown aside ("Shown only"): the night keeps it so, and keeps its id (a
    page left open still finds it), so a one-click confirm of the strong matches never counts it."""
    a, b, c = pds["A"], pds["B"], pds["C"]
    family((a, 101), (b, 201), (c, 301))
    m = w.master("Children enrolled in NFE")
    w.link(m, a, 101, title=ENROLLED)
    run()
    first = suggested()
    aside, other = first[("PD/2026/002", "s201")], first[("PD/2026/003", "s301")]
    assert (aside.role, aside.score, other.role) == ("total", 0.95, "total")
    assert curation.set_role(aside, "shown", "staff") == ""
    run()
    again = suggested()
    assert {key: (x.pk, x.role, x.score) for key, x in again.items()} == {
        ("PD/2026/002", "s201"): (aside.pk, "shown", 0.95),  # the person's role, its id
        ("PD/2026/003", "s301"): (other.pk, "total", 0.95),  # made again: the same link
    }
    strong = dm.EtoolsMasterLink.objects.filter(status="suggested", score__gte=master_links.STRONG)
    assert master_links.confirm(list(strong.select_related("master")), "staff").confirmed == 2
    sub = w.subs(w.figure(m))["s201"]
    assert (sub.status, sub.role, sub.counted, sub.reason) == ("confirmed", "shown", False, masters.SHOWN)

    # a suggestion the night no longer makes goes; one it makes anew is written
    w.indicator(pds["D"], 401, ENROLLED, cp_output=OUTPUT)
    dm.PDIndicator.objects.filter(source_id=301).delete()
    dm.EtoolsMasterLink.objects.filter(ident="s301").update(status="suggested")
    run()
    assert set(suggested()) == {("PD/2026/004", "s401")}


def test_a_proposal_a_person_edited_is_kept_and_found_again_when_its_key_changes(pds):
    """Staff renamed a proposal and typed its code and target; then a report names its CP output, so its
    family's structure key changes: it is the same proposal, under its new key, with what staff typed."""
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201), cp_output="")
    run()
    (proposal,) = proposals()
    before = proposal.proposal_key
    errors = curation.save_settings(
        proposal, w.YEAR, name="Children in NFE", code="AWP-1", target="500", by="staff"
    )
    assert errors == {}
    dm.PDIndicator.objects.filter(source_id=101).update(cp_output=OUTPUT)  # its first report names it
    run()
    (again,) = proposals()
    settings = again.years.get(year=w.YEAR)
    assert (again.pk, again.name, settings.code, settings.target) == (
        proposal.pk,
        "Children in NFE",
        "AWP-1",
        500.0,
    )
    assert again.proposal_key and again.proposal_key != before
    assert {(x.pd_number, x.status) for x in again.links.all()} == {
        ("PD/2026/001", "suggested"),
        ("PD/2026/002", "suggested"),
    }
    # its family is gone: what staff typed is kept, never deleted
    dm.PDIndicator.objects.filter(source_id=201).delete()
    run()
    assert proposals() == [again] and again.years.filter(year=w.YEAR, target=500).exists()
    # one no person edited still goes with its family
    family((a, 102), (b, 202), title=TRAINED)
    run()
    untouched = next(p for p in proposals() if p.pk != again.pk)
    dm.PDIndicator.objects.filter(source_id=202).delete()
    run()
    assert not dm.EtoolsMaster.objects.filter(pk=untouched.pk).exists()


@pytest.mark.parametrize("decision", ["rejected", "role"])
def test_a_proposal_a_person_decided_a_link_of_is_the_same_proposal_when_its_key_changes(pds, decision):
    """Staff rejected one of a proposal's PD indicators, or set one to Shown only; then a report names the
    family's CP output, so its structure key changes: it is the same proposal under its new key, the
    rejected indicator stays out of it and the role stays the person's. Never a second proposal of the same
    name holding the indicator staff rejected."""
    a, b, c = pds["A"], pds["B"], pds["C"]
    family((a, 101), (b, 201), (c, 301), cp_output="")
    run()
    (proposal,) = proposals()
    before = proposal.proposal_key
    link = proposal.links.get(pd_number="PD/2026/003")
    if decision == "rejected":
        assert master_links.reject([link], "staff") == 1
    else:
        assert curation.set_role(link, "shown", "staff") == ""
    dm.PDIndicator.objects.filter(source_id=101).update(cp_output=OUTPUT)  # its first report names it
    run()

    (again,) = proposals()
    assert again.pk == proposal.pk and again.proposal_key and again.proposal_key != before
    expected = ("rejected", "total", "staff") if decision == "rejected" else ("suggested", "shown", "staff")
    assert {(x.pd_number, x.status, x.role, x.confirmed_by) for x in again.links.all()} == {
        ("PD/2026/001", "suggested", "total", ""),
        ("PD/2026/002", "suggested", "total", ""),
        ("PD/2026/003", *expected),
    }
    done = master_links.accept([again], "staff")
    assert done.links.confirmed == (2 if decision == "rejected" else 3)


def test_a_proposal_merged_into_an_active_master_is_confirmed_there_and_stays_after_the_night(pds):
    """Merging a proposal into an active master of other words is accepting it there: its PD indicators are
    confirmed in the master kept (each read again, as Accept does), so the night keeps them, and the family's
    new programme documents are suggested there by their structure."""
    a, b, c, d = pds["A"], pds["B"], pds["C"], pds["D"]
    family((a, 101), (b, 201))
    learners = "# of out-of-school learners in NFE"
    w.indicator(c, 301, learners, cp_output=OTHER_OUTPUT)
    keep = w.master("Out-of-school learners in non-formal education")
    w.link(keep, c, 301, title=learners)
    run()
    (proposal,) = proposals()
    done = curation.merge(keep, [proposal], "staff")
    run()

    assert {(x.pd_number, x.status, x.role, x.confirmed_by) for x in keep.links.all()} == {
        ("PD/2026/001", "confirmed", "total", "staff"),
        ("PD/2026/002", "confirmed", "total", "staff"),
        ("PD/2026/003", "confirmed", "total", ""),
    }
    assert (done.moved, done.confirmed, done.left) == (2, 2, [])
    proposal.refresh_from_db()
    assert proposal.status == "dismissed" and not proposals()
    family((d, 401))  # a new programme document of the family
    run()
    link = suggested()[("PD/2026/004", "s401")]
    assert (link.master, link.score) == (keep, master_links.STRUCTURE_SCORE)


def test_a_proposal_merged_keeps_as_suggestions_what_cannot_count_in_the_master_kept_and_says_why(pds):
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    run()
    (proposal,) = proposals()
    keep = w.master("Out-of-school learners in non-formal education")
    # PD B's indicator is a percentage in eTools since the proposal was made: a sum cannot count it
    dm.PDIndicator.objects.filter(source_id=201).update(display_type="percentage", unit="percentage")
    done = curation.merge(keep, [proposal], "staff")
    assert (done.moved, done.confirmed) == (2, 1)
    assert [(x.ident, why) for x, why in done.left] == [
        ("s201", masters.misfit("sum", "total", "percentage"))
    ]
    assert {(x.ident, x.status) for x in keep.links.all()} == {("s101", "confirmed"), ("s201", "suggested")}


def test_a_proposal_dismissed_or_deleted_or_a_master_archived_while_the_run_works_gets_nothing_back(
    pds, monkeypatch
):
    """The run works out what to write first, then writes it: a person may dismiss a proposal on the tab,
    or delete one or archive a master in the administration, meanwhile. Nothing is written back into
    them, and the run never fails for it."""
    from django.db import connection

    a, b, c, d = pds["A"], pds["B"], pds["C"], pds["D"]
    family((a, 101), (b, 201))
    family((a, 102), (b, 202), title=TRAINED)
    w.indicator(c, 301, "# of children reached with PSS", cp_output=OUTPUT)
    w.indicator(d, 401, "# of children reached with PSS", cp_output=OUTPUT)
    m = w.master("Children reached with PSS")
    w.link(m, c, 301, title="# of children reached with PSS")
    run()
    by_name = {p.name: p for p in proposals()}
    dismissed, deleted = by_name[ENROLLED], by_name[TRAINED]
    assert set(suggested()) == {("PD/2026/004", "s401")}
    plan = master_links.plan

    def meanwhile(*args):
        found = plan(*args)
        assert master_links.dismiss([dismissed], "staff") == 1  # on the tab
        dm.EtoolsMaster.objects.filter(pk=deleted.pk).delete()  # in the administration
        dm.EtoolsMaster.objects.filter(pk=m.pk).update(status="archived")
        return found

    monkeypatch.setattr(master_links, "plan", meanwhile)
    sync = run()
    connection.check_constraints()  # no link to a master that is gone
    dismissed.refresh_from_db()
    assert dismissed.status == "dismissed" and not dismissed.links.exists()
    assert not dm.EtoolsMaster.objects.filter(pk=deleted.pk).exists()
    assert not m.links.filter(status="suggested").exists()
    assert sync.rows_written == 0
    assert (sync.details["totals"]["proposed"], sync.details["totals"]["suggested"]) == (0, 0)


def test_a_family_that_is_a_ratio_by_its_prp_methods_is_proposed_and_suggested_as_a_ratio(pds, monkeypatch):
    """Its display type and unit say number, its reports' PRP methods say ratio: the pages read a ratio, so
    it is proposed as a ratio, accepted in one click, and a new programme document of it is suggested there.
    Only the programme documents whose reports name the ratio method are read with their Reached."""
    a, b, c, d = pds["A"], pds["B"], pds["C"], pds["D"]
    assessed = "# of children assessed"

    def ratio(pd, source_id):
        w.indicator(pd, source_id, assessed, cp_output=OUTPUT)
        w.report(
            pd,
            source_id,
            assessed,
            "QPR1",
            day(3, 31),
            {"Akkar": w.pct(3, 4)},
            calculation_across_periods="ratio",
        )

    ratio(a, 101)
    ratio(b, 201)
    family((c, 301), (d, 401), title=TRAINED)  # numbers: read without Reached
    w.report(c, 301, TRAINED, "QPR1", day(3, 31), {"Akkar": 12})
    read = []
    attach = monitoring._attach_reached
    monkeypatch.setattr(
        monitoring,
        "_attach_reached",
        lambda found, *args: read.append({k[0] for k in found}) or attach(found, *args),
    )
    run()
    assert read == [{a.id, b.id}]
    by_name = {p.name: p for p in proposals()}
    proposal = by_name[assessed]
    assert (proposal.method, by_name[TRAINED].method) == ("ratio", "sum")
    assert {x.role for x in proposal.links.all()} == {"total"}
    done = master_links.accept([proposal], "staff")
    assert (done.links.confirmed, done.links.left) == (2, [])
    assert w.figure(proposal).value == 0.75

    e = w.programme("E", 5)
    ratio(e, 501)
    run()
    link = suggested()[("PD/2026/005", "s501")]
    assert (link.master, link.score) == (proposal, master_links.STRUCTURE_SCORE)


def test_a_programme_document_not_reported_yet_does_not_decide_a_ratio_familys_type(pds):
    """A PD indicator with no report has no PRP method yet: it reads as a number, but its family's type is
    read from those that reported. Two report the ratio method and a new one has not reported: a Ratio with
    all three in the total. Accept confirms the two and leaves the new one a suggestion, said why; once it
    reports the ratio method it is suggested there by its structure. One ratio and one not reported: a
    Ratio, never a Sum that would count nothing."""
    a, b, c, d = pds["A"], pds["B"], pds["C"], pds["D"]
    assessed, screened = "# of children assessed", "# of learners screened"

    def ratio(pd, source_id, title=assessed, value=(3, 4)):
        w.report(
            pd,
            source_id,
            title,
            "QPR1",
            day(3, 31),
            {"Akkar": w.pct(*value)},
            calculation_across_periods="ratio",
        )

    for pd, source_id in ((a, 101), (b, 201), (c, 301), (d, 401), (a, 102)):
        w.indicator(pd, source_id, screened if source_id in (401, 102) else assessed, cp_output=OUTPUT)
    ratio(a, 101)
    ratio(b, 201)
    ratio(d, 401, screened)
    run()
    by_name = {p.name: p for p in proposals()}
    proposal, other = by_name[assessed], by_name[screened]
    assert (proposal.method, other.method) == ("ratio", "ratio")
    assert {(x.pd_number, x.role) for x in proposal.links.all()} == {
        ("PD/2026/001", "total"),
        ("PD/2026/002", "total"),
        ("PD/2026/003", "total"),
    }
    assert {(x.pd_number, x.role) for x in other.links.all()} == {
        ("PD/2026/001", "total"),
        ("PD/2026/004", "total"),
    }
    done = master_links.accept([proposal], "staff")
    assert done.links.confirmed == 2
    assert [(x.pd_number, why) for x, why in done.links.left] == [
        ("PD/2026/003", masters.misfit("ratio", "total", "number"))
    ]
    run()
    assert ("PD/2026/003", "s301") not in suggested()  # still a number: never counted as a ratio's total
    ratio(c, 301, value=(1, 2))
    run()
    link = suggested()[("PD/2026/003", "s301")]
    assert (link.master, link.score, link.role) == (proposal, master_links.STRUCTURE_SCORE, "total")
    assert master_links.confirm([link], "staff").confirmed == 1
    assert w.figure(proposal).value == pytest.approx(7 / 10)


def test_a_number_confirmed_in_a_sum_whose_reports_then_name_the_ratio_method_is_flagged(pds):
    """Confirmed as a number, its reports now combine it as a ratio (PRP's methods): the page reads "Type
    changed in eTools", and the job counts it so (reporting check 12 reads that count)."""
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201), title=TRAINED)
    m = w.master("Caregivers trained")
    w.link(m, a, 101, title=TRAINED)
    w.link(m, b, 201, title=TRAINED)
    w.report(a, 101, TRAINED, "QPR1", day(3, 31), {"Akkar": 12})
    assert run().details["totals"]["type_changed"] == 0
    w.report(b, 201, TRAINED, "QPR1", day(3, 31), {"Akkar": w.pct(3, 4)}, calculation_across_periods="ratio")
    sync = run()
    assert sync.details["sections"]["Education"]["type_changed"] == 1
    sub = w.subs(w.figure(m))["s201"]
    assert (sub.reason, sub.counted) == (masters.TYPE_CHANGED, False)


def test_a_proposals_links_count_only_once_it_is_accepted(pds):
    """Confirming a proposal's links one by one would count them nowhere (a proposal is listed nowhere) and
    keep them from every other master: it is refused, and accepting confirms them."""
    a, b = pds["A"], pds["B"]
    family((a, 101), (b, 201))
    run()
    (proposal,) = proposals()
    done = master_links.confirm(list(proposal.links.select_related("master")), "staff")
    assert done.confirmed == 0
    assert {why for _link, why in done.left} == {
        "Accept this proposed master indicator first: its PD indicators count once it is active."
    }
    assert {x.status for x in proposal.links.all()} == {"suggested"}
    archived = w.master("Children enrolled (old)", status="archived")
    (left,) = curation.add(archived, [(a.id, "s101")], "total", "staff").left
    assert left == (
        f"PD/2026/001: {ENROLLED}",
        "This master indicator is archived: make it active first, or choose another one.",
    )
    assert master_links.accept([proposal], "staff").links.confirmed == 2


def test_a_similar_title_reads_each_text_once_a_run(pds, monkeypatch):
    """Rule c scores with the words of every master and indicator read once a run, never again for each pair
    (thousands of indicators against a section's masters)."""
    a = pds["A"]
    for n in range(1, 13):
        w.indicator(a, 100 + n, f"# of children enrolled in NFE classes in area {n}")
    for n in range(1, 11):
        target = w.master(
            f"Children enrolled in learning programme {n}", keywords="children enrolled classes"
        )
        w.indicator(pds["B"], 200 + n, f"# of children in learning programme {n}")
        w.link(target, pds["B"], 200 + n, title=f"# of children in learning programme {n}")
    calls = []
    words = master_links.linking.words
    monkeypatch.setattr(master_links.linking, "words", lambda text: calls.append(text) or words(text))
    found = master_links.plan(master_links.pool(w.YEAR), w.YEAR)
    # the indicators' titles and outputs, and each master's name, keywords and confirmed title: once each
    assert len(calls) <= 2 * 12 + 3 * 10
    assert found.ambiguous  # they are scored all the same: every master fits as well


def test_a_type_changed_in_etools_is_flagged_and_confirming_it_again_saves_the_new_type(pds):
    a, b = pds["A"], pds["B"]
    title = "% of children passing the exam"
    w.indicator(a, 101, title, display_type="percentage")
    w.report(a, 101, title, "QPR1", day(3, 31), {"Akkar": w.pct(40, 100)})
    w.indicator(b, 201, "# of children assessed", display_type="number")  # a ratio by PRP's methods alone
    w.report(
        b,
        201,
        "# of children assessed",
        "QPR1",
        day(3, 31),
        {"Akkar": w.pct(3, 4)},
        calculation_across_periods="ratio",
    )
    m = w.master("Children passing the exam", "percent")
    link = w.link(
        m, a, 101, title=title, unit_class="ratio"
    )  # confirmed as a ratio: eTools says percentage now
    ratio = w.master("Children assessed", "ratio")
    w.link(ratio, b, 201, title="# of children assessed", unit_class="ratio")
    sync = run()
    assert sync.details["sections"]["Education"]["type_changed"] == 1  # not the ratio by its methods
    sub = w.subs(w.figure(m))["s101"]
    assert (sub.reason, sub.counted) == (masters.TYPE_CHANGED, False)

    done = master_links.confirm([link], "staff")
    assert (done.confirmed, done.left) == (1, [])
    link.refresh_from_db()
    assert (link.unit_class, link.confirmed_by) == ("percentage", "staff")
    assert w.figure(m).value == 40.0
    assert run().details["totals"]["type_changed"] == 0


def test_confirm_reads_the_indicator_again_and_leaves_what_cannot_count(pds):
    a, b = pds["A"], pds["B"]
    w.indicator(a, 101, ENROLLED)
    w.indicator(a, 102, "% of children passing", display_type="percentage")
    w.indicator(b, 201, ENROLLED)
    one = w.master("Children enrolled")
    two = w.master("Children enrolled in NFE")
    w.link(one, a, 101, title=ENROLLED)
    again = w.link(two, a, 101, title=ENROLLED, status="rejected")
    wrong = w.link(two, a, 102, status="suggested", unit_class="")
    gone = w.link(two, a, 109, title="# of children reached", status="suggested", unit_class="")
    done = master_links.confirm([again, wrong, gone], "staff")
    assert done.confirmed == 0
    assert {x.ident: why for x, why in done.left} == {
        "s101": "It already counts in the total of Children enrolled.",
        "s102": masters.misfit("sum", "total", "percentage"),
        "s109": masters.GONE,
    }
    assert {x.status for x in (again, wrong, gone)} == {"rejected", "suggested"}

    other = w.link(one, b, 201, status="suggested", unit_class="")  # one master suggests it for its total
    mine = w.link(two, b, 201, status="rejected")
    assert master_links.confirm([mine], "staff").confirmed == 1  # a person's choice: the suggestion gives way
    assert not dm.EtoolsMasterLink.objects.filter(pk=other.pk).exists()
    mine.refresh_from_db()
    assert (mine.status, mine.title, mine.pd_number, mine.unit_class) == (
        "confirmed",
        ENROLLED,
        "PD/2026/002",
        "number",
    )


# ------------------------------------------------------------------------------------------- the run's record
def test_the_run_is_recorded_with_counts_for_each_section_and_never_a_title_or_a_name(pds):
    a, b, c = pds["A"], pds["B"], pds["C"]
    family((a, 101), (b, 201))
    w.indicator(c, 301, "# of children attending NFE classes", cp_output=OTHER_OUTPUT)
    w.indicator(c, 302, TRAINED, section=w.CP)
    m = w.master("Children attending NFE classes")  # 2 of its 4 words in the family's title: not similar
    sync = run()
    assert (sync.job, sync.triggered_by, sync.target, sync.status) == (JOB, "test", "", "succeeded")
    assert (sync.rows_in, sync.rows_written, sync.rows_failed) == (4, 3, 0)
    assert sync.details["year"] == w.YEAR
    assert sync.details["totals"] == {
        "proposed": 1,
        "in_proposals": 2,
        "suggested": 1,
        "replacements": 0,
        "ambiguous": 0,
        "no_longer_in_etools": 0,
        "type_changed": 0,
        "unattached": 1,
    }
    assert list(sync.details["sections"]) == ["Child Protection", "Education"]
    text = json.dumps(sync.details)
    for word in (ENROLLED, TRAINED, m.name, "Partner A", "PD/2026/001", "s101"):
        assert word not in text


def test_a_run_while_another_holds_the_lock_records_nothing(db, monkeypatch):
    monkeypatch.setattr(master_links, "_lock", lambda: False)
    assert master_links.run(w.YEAR, "test") == master_links.BUSY
    assert not SyncRun.objects.filter(job=JOB).exists()


def test_the_run_holds_its_lock_while_it_works_and_releases_it(pds, monkeypatch):
    held = []
    plan = master_links.plan
    monkeypatch.setattr(
        master_links,
        "plan",
        lambda *args: held.append(background.lock_is_held(master_links.LOCK_ID)) or plan(*args),
    )
    run()
    assert held == [True] and background.lock_is_held(master_links.LOCK_ID) is False


def test_a_failed_run_is_recorded_and_leaves_everything_as_it_was(pds, monkeypatch):
    family((pds["A"], 101), (pds["B"], 201))
    run()
    before = state()
    family((pds["C"], 301))

    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(master_links, "_link", broken)  # after the suggestions were deleted
    sync = master_links.run(w.YEAR, "test")
    assert sync.status == SyncRun.Status.FAILED and "boom" in sync.error
    assert sync.details["year"] == w.YEAR
    assert state() == before  # one transaction: nothing was deleted either


def test_the_command_runs_the_job_and_prints_the_counts(pds):
    family((pds["A"], 101), (pds["B"], 201))
    out = StringIO()
    call_command("suggest_master_indicators", "--year", "2026", "--triggered-by", "staff", stdout=out)
    text = out.getvalue()
    assert "Education: proposed 1, in proposals 2" in text
    assert "eTools master indicators [] succeeded: in=2 written=2 failed=0" in text
    assert SyncRun.objects.get(job=JOB).triggered_by == "staff"
    for wrong in ("26", "2031", "twenty"):
        with pytest.raises(CommandError, match="a calendar year"):
            call_command("suggest_master_indicators", "--year", wrong, stdout=StringIO())


def test_the_schedule_run_a_job_and_the_locks_know_the_job(db, monkeypatch):
    job = ScheduledJob.objects.get(key="etools-masters")
    command = jobs.COMMANDS[job.command]
    assert (command.args, command.sync_job, command.triggered_by) == (
        ("suggest_master_indicators",),
        JOB,
        True,
    )
    (spec,) = [s for s in BACKGROUND_JOBS if s.name == "run_suggest_master_indicators"]
    assert (spec.command, spec.job) == (command.args, JOB)
    assert "Nothing counts until someone confirms it" in str(spec.description)
    assert background.LOCK_IDS[JOB] == master_links.LOCK_ID == background.ETOOLS_MASTERS_LOCK_ID
    assert len(set(background.LOCK_IDS.values())) == len(background.LOCK_IDS)
    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    assert jobs.start(job.command, "schedule") == "started"
    assert started == [("suggest_master_indicators", "--triggered-by", "schedule")]
