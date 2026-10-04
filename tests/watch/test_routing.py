"""NeuroDB Watch: who is told what, and when (routing and receipts)."""

import datetime

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, MANAGEMENT, SECTION_EDITOR, VIEWER
from neurodb.core.models import SyncRun
from neurodb.donors.models import DonorAccount
from neurodb.watch import memory, page, routing
from neurodb.watch.detectors import (
    ADMINS,
    CONCERN,
    COUNTRY,
    CRITICAL,
    DAILY,
    DEADLINE,
    INFO,
    OFF,
    ON,
    QUICK,
    SECTION,
    SYSTEM,
    TRIAL,
    WARNING,
    Context,
)
from neurodb.watch.models import DetectorSetting, SectionMatch, WatchItem, WatchNote, WatchReceipt
from neurodb.watch.routing import (
    ADMINS_ONLY,
    CLOSED,
    COUNTRY_VIEW,
    ESCALATED,
    ESCALATION,
    GOOD_TO_KNOW,
    KNOWN,
    MISSED,
    NEEDS_YOU,
    NEW,
    OVERDUE,
    REMINDER,
    RESOLVED,
    STAFF,
    STILL_OPEN,
    WORSE,
    Routing,
    announce,
)

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut
TODAY = datetime.date(2026, 10, 5)
LONG_AGO = datetime.datetime(2020, 1, 1, tzinfo=UTC)
CHECK = "report_due_soon"
OTHER_CHECK = "action_point_due"
Reaction = WatchReceipt.Reaction


def day(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


def ctx(n: int = 0, mode: str = DAILY) -> Context:
    return Context.make(mode, now=NOW + datetime.timedelta(days=n))


def run(n: int = 0, mode: str = DAILY, **kwargs) -> routing.Announced:
    return announce(ctx(n, mode), **kwargs)


# ---------------------------------------------------------------------------- the world
@pytest.fixture
def education(db):
    section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(etools_name="Education", section=section, how="exact", confirmed=True)
    return section


@pytest.fixture
def health(db):
    section = Section.objects.create(name="Health", code="HLT")
    SectionMatch.objects.create(etools_name="Health", section=section, how="exact", confirmed=True)
    return section


def person(username, role=VIEWER, section=None, management=False, joined=LONG_AGO, **fields) -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.org", **fields)
    User.objects.filter(pk=user.pk).update(date_joined=joined)
    if role:
        user.groups.add(Group.objects.get(name=role))
    if management:
        user.groups.add(Group.objects.get(name=MANAGEMENT))
    if section is not None:
        user.section = section
        user.save(update_fields=["section"])
    return user


@pytest.fixture
def team(roles, education, health):
    """An Administrator (Education), a Management member (Health), two Education viewers, a Health
    viewer, an Education section editor and a viewer without a section."""

    class Team:
        admin = person("admin", ADMIN, education)
        boss = person("boss", VIEWER, health, management=True)
        edu = person("edu", VIEWER, education)
        edu2 = person("edu2", VIEWER, education)
        hlt = person("hlt", VIEWER, health)
        editor = person("editor", SECTION_EDITOR, education)
        nobody = person("nobody", VIEWER, None)

    return Team


def setting(detector=CHECK, mode=ON, since=None) -> DetectorSetting:
    """The check's setting, on (or in trial) since ``since`` (default: a month ago)."""
    since = since or day(-30)
    row, _ = DetectorSetting.objects.update_or_create(
        detector=detector,
        defaults={
            "mode": mode,
            "on_since": since if mode == ON else None,
            "trial_since": since if mode == TRIAL else None,
        },
    )
    DetectorSetting.objects.filter(pk=row.pk).update(
        updated_at=datetime.datetime.combine(since, datetime.time(5), tzinfo=UTC)
    )
    return DetectorSetting.objects.get(pk=row.pk)


@pytest.fixture
def checks(db):
    """Both test checks on for staff for a month."""
    return setting(CHECK), setting(OTHER_CHECK)


_numbers = iter(range(1, 10_000))


def item(
    sections=("Education",),
    severity=WARNING,
    due=None,
    first_seen=TODAY,
    detector=CHECK,
    scope=SECTION,
    kind=DEADLINE,
    milestones=(),
    key=None,
    **fields,
) -> WatchItem:
    mapping = dict(SectionMatch.objects.filter(confirmed=True).values_list("etools_name", "section_id"))
    n = next(_numbers)
    evidence = {
        "source": "eTools progress reports",
        "records": [{"label": f"record {n}", "date": "", "value": "", "url": ""}],
        "numbers": {},
    }
    if milestones:
        evidence["milestones"] = list(milestones)
    return WatchItem.objects.create(
        key=key or f"due:test:{n}",
        detector=detector,
        kind=kind,
        severity=severity,
        title=f"Thing {n}",
        due_date=due,
        etools_sections=list(sections),
        section_ids=sorted({mapping[s] for s in sections if s in mapping}),
        scope=scope,
        evidence=evidence,
        first_seen_on=first_seen,
        last_seen_on=first_seen,
        changed_on=first_seen,
        **fields,
    )


def receipts(user=None, item_=None):
    qs = WatchReceipt.objects.all()
    if user is not None:
        qs = qs.filter(user=user)
    if item_ is not None:
        qs = qs.filter(item=item_)
    return qs


def step(user, item_) -> str | None:
    found = receipts(user, item_).first()
    return found.told_step if found else None


def react(user, item_, reaction, on=TODAY, **fields):
    WatchReceipt.objects.filter(user=user, item=item_).update(
        reaction=reaction,
        reacted_at=datetime.datetime.combine(on, datetime.time(8), tzinfo=UTC),
        **fields,
    )


# ---------------------------------------------------------------------------- who
def test_people_are_active_users_with_a_role_and_never_a_donor_account(team, education):
    donor = person("donor", VIEWER, education)
    DonorAccount.objects.create(user=donor, name="EU", donors=["EU"], must_change_password=False)
    person("norole", None, education)
    person("gone", VIEWER, education, is_active=False)
    found = routing.people()
    assert donor.pk not in found
    assert {u.username for u in User.objects.filter(pk__in=found)} == {
        "admin",
        "boss",
        "edu",
        "edu2",
        "hlt",
        "editor",
        "nobody",
    }
    assert found[team.admin.pk].admin and found[team.admin.pk].country
    assert found[team.boss.pk].management and found[team.boss.pk].country
    assert found[team.editor.pk].editor and not found[team.editor.pk].country
    assert not found[team.edu.pk].country


def test_audiences_are_each_section_plus_the_country_view(team, education, health):
    found = routing.audiences()
    assert found[WatchNote.section_audience(education.pk)] == sorted(
        [team.admin.pk, team.edu.pk, team.edu2.pk, team.editor.pk]
    )
    assert found[WatchNote.section_audience(health.pk)] == sorted([team.boss.pk, team.hlt.pk])
    assert found[WatchNote.COUNTRY] == sorted([team.admin.pk, team.boss.pk])
    assert routing.Person.of(team.boss).audiences == [WatchNote.section_audience(health.pk), "country"]
    assert routing.Person.of(team.nobody).audiences == []


def test_a_donor_account_with_a_section_gets_no_receipt(team, education, checks):
    donor = person("donor", ADMIN, education)  # even in a role group, by mistake
    DonorAccount.objects.create(user=donor, name="EU", donors=["EU"], must_change_password=False)
    thing = item(severity=CRITICAL, scope=COUNTRY)
    run()
    assert step(team.edu, thing) == NEW
    assert not receipts(donor).exists()
    assert donor.pk not in routing.item_users(thing)
    assert routing.unseen_count(donor, TODAY) == 0


def test_section_items_go_to_their_section_staff_only(team, checks):
    thing = item()
    found = routing.item_users(thing, Routing.load(TODAY))
    assert found == {team.admin.pk: STAFF, team.edu.pk: STAFF, team.edu2.pk: STAFF, team.editor.pk: STAFF}


def test_a_viewer_never_gets_an_item_whose_section_is_unmapped_administrators_do(team, checks):
    SectionMatch.objects.create(etools_name="Education and Youth", how="contains", confirmed=False)
    unmapped = item(sections=("Education and Youth",))
    nothing = item(sections=())
    half = item(sections=("Education", "Education and Youth"))
    found = Routing.load(TODAY)
    assert found.recipients(unmapped) == {team.admin.pk: ADMINS_ONLY}  # not the Management group
    assert found.recipients(nothing) == {team.admin.pk: ADMINS_ONLY}
    assert set(found.recipients(half)) == {team.admin.pk, team.edu.pk, team.edu2.pk, team.editor.pk}
    run()
    assert step(team.admin, unmapped) is not None
    assert not receipts(team.edu, unmapped).exists() and not receipts(team.boss, unmapped).exists()


def test_a_trial_checks_items_reach_only_the_country_view(team, checks):
    setting(CHECK, TRIAL)
    thing = item(severity=CRITICAL)
    assert routing.item_users(thing) == {team.admin.pk: COUNTRY_VIEW, team.boss.pk: COUNTRY_VIEW}
    run()
    assert set(receipts(item_=thing).values_list("user__username", flat=True)) == {"admin", "boss"}


def test_a_check_switched_off_tells_no_one(team, checks):
    setting(CHECK, OFF)
    thing = item(severity=CRITICAL)
    assert routing.item_users(thing) == {}
    run()
    assert not receipts(item_=thing).exists()


def test_system_and_administrators_items_go_to_the_administrators_only(team, checks):
    setting("system_health", ON)
    failed = item(kind=SYSTEM, scope=ADMINS, detector="system_health", sections=())
    donor_expiry = item(scope=ADMINS, sections=("Education",))
    assert routing.item_users(failed) == {team.admin.pk: ADMINS_ONLY}
    assert routing.item_users(donor_expiry) == {team.admin.pk: ADMINS_ONLY}


def test_a_management_member_gets_the_country_items_and_counts_not_every_sections_items(
    team, education, checks
):
    education_item = item(sections=("Education",))
    grant = item(sections=("Education",), scope=COUNTRY, severity=CRITICAL)
    health_item = item(sections=("Health",))
    run()
    assert set(receipts(team.boss).values_list("item_id", flat=True)) == {grant.pk, health_item.pk}
    assert not receipts(team.boss, education_item).exists()
    found = Routing.load(TODAY)
    everything = [education_item, grant, health_item]
    assert found.audience_items("country", everything) == [grant]
    assert found.audience_items(WatchNote.section_audience(education.pk), everything) == [
        education_item,
        grant,
    ]
    counts = found.section_counts(everything)
    assert counts[education.pk]["open"] == 2 and counts[education.pk]["critical"] == 1
    assert counts[team.hlt.section_id]["open"] == 1


def test_a_user_without_a_section_gets_no_receipts(team, checks):
    item(severity=CRITICAL)
    item(sections=("Health",), scope=COUNTRY)
    run()
    assert not receipts(team.nobody).exists()
    assert routing.items_for(team.nobody, TODAY) == []


# ---------------------------------------------------------------------------- first sight
def test_the_first_run_announces_only_critical_items_and_items_due_within_3_days(team):
    setting(CHECK, ON, since=TODAY)  # its first run is today
    critical = item(severity=CRITICAL, due=day(30))
    soon = item(severity=WARNING, due=day(3))
    later = item(severity=WARNING, due=day(10))
    info = item(severity=INFO, due=day(12))
    old = item(severity=WARNING, due=None)
    done = run()
    assert step(team.edu, critical) == NEW
    assert step(team.edu, soon) == NEW
    for quiet in (later, info, old):
        assert step(team.edu, quiet) == KNOWN
    assert receipts(team.edu, later).get().level == GOOD_TO_KNOW
    assert done.known and done.needs_you
    # the next morning, something new is new
    fresh = item(severity=WARNING, due=day(20), first_seen=day(1))
    run(1)
    assert step(team.edu, fresh) == NEW
    assert step(team.edu, later) == KNOWN


def test_a_trial_check_is_quiet_on_its_first_day_too(team):
    setting(CHECK, TRIAL, since=TODAY)
    thing = item(severity=WARNING, due=day(20))
    run()
    assert step(team.admin, thing) == KNOWN


def test_an_item_first_seen_a_week_ago_is_known_to_someone_who_hears_of_it_now(team, checks, education):
    old = item(first_seen=day(-8), due=day(20))
    late = person("late", VIEWER, education)
    run()
    assert step(late, old) == KNOWN


def test_a_person_s_first_day_is_quiet(team, checks, education):
    thing = item(due=day(20))
    newcomer = person("newcomer", VIEWER, education, joined=NOW)
    run()
    assert step(team.edu, thing) == NEW
    assert step(newcomer, thing) == KNOWN


# ---------------------------------------------------------------------------- how much
def test_seven_new_warnings_give_five_needs_you(team, checks):
    things = [item(severity=WARNING, due=day(20 + n)) for n in range(7)]  # no milestone tomorrow
    done = run()
    told = receipts(team.edu).filter(level=NEEDS_YOU)
    assert told.count() == 5
    assert set(told.values_list("item_id", flat=True)) == {t.pk for t in things[:5]}  # soonest due first
    assert done.deferred >= 2
    # the two left are told the next morning
    run(1)
    assert set(receipts(team.edu).filter(last_told_on=day(1)).values_list("item_id", flat=True)) == {
        things[5].pk,
        things[6].pk,
    }


def test_needs_you_ranks_critical_first(team, checks):
    warnings = [item(severity=WARNING, due=day(1 + n)) for n in range(5)]
    critical = item(severity=CRITICAL, due=day(40))
    run()
    told = set(receipts(team.edu).filter(level=NEEDS_YOU).values_list("item_id", flat=True))
    assert critical.pk in told and warnings[-1].pk not in told


@override_settings(WATCH_GOOD_TO_KNOW_PER_DAY=2)
def test_good_to_know_has_its_own_limit(team, checks):
    for n in range(3):
        item(severity=INFO, due=day(20 + n))
    run()
    assert receipts(team.edu).filter(level=GOOD_TO_KNOW).exclude(told_step=KNOWN).count() == 2


def test_a_quick_pass_creates_no_good_to_know_receipt(team, checks):
    critical = item(severity=CRITICAL, due=day(30))
    soon = item(severity=WARNING, due=day(2))
    info_soon = item(severity=INFO, due=day(1))
    later = item(severity=WARNING, due=day(10))
    done = run(mode=QUICK)
    assert done.mode == QUICK
    assert step(team.edu, critical) == NEW and step(team.edu, soon) == NEW
    assert not receipts(team.edu, info_soon).exists()
    assert not receipts(team.edu, later).exists()  # waits for the morning
    assert not WatchReceipt.objects.filter(level=GOOD_TO_KNOW).exists()
    run(1)
    assert step(team.edu, later) == NEW


# ---------------------------------------------------------------------------- milestones and changes
def test_the_14_7_and_3_day_milestones_each_fire_once_and_never_a_fourth(team):
    setting(CHECK, ON, since=day(-20))
    thing = item(severity=WARNING, due=TODAY, first_seen=day(-20), milestones=(14, 7, 3, 1, 0))
    told = []
    for n in range(-20, 1):
        run(n)
        receipt = receipts(team.edu, thing).get()
        if receipt.last_told_on == day(n):
            told.append(receipt.told_step)
    assert told == [KNOWN, "14", "7", "3"]
    assert receipts(team.edu, thing).get().milestone_count == 3


def test_a_milestone_is_told_once_even_with_several_passes_a_day(team, checks):
    thing = item(severity=WARNING, due=day(9), first_seen=day(-1), milestones=(7, 3))
    run(-1)
    assert step(team.edu, thing) == NEW
    run(2)
    run(2, mode=QUICK)
    run(3)
    receipt = receipts(team.edu, thing).get()
    assert (receipt.told_step, receipt.last_told_on, receipt.milestone_count) == ("7", day(2), 1)


def test_worse_is_told_once(team, checks):
    thing = item(severity=WARNING, due=day(40))
    run()
    assert step(team.edu, thing) == NEW
    thing.severity = CRITICAL
    thing.add_story("Got worse: was warning, now critical", day(2))
    thing.save()
    run(2)
    assert step(team.edu, thing) == WORSE
    run(3)
    assert receipts(team.edu, thing).get().last_told_on == day(2)


def test_worse_on_the_day_it_was_told_comes_from_the_pass(team, checks):
    thing = item(severity=WARNING, due=day(40))
    run()
    thing.severity = CRITICAL
    thing.add_story("Got worse: was warning, now critical", TODAY)
    thing.save()
    outcome = memory.Outcome(mode=QUICK, worse=[thing.key])
    run(mode=QUICK, outcome=outcome)
    assert step(team.edu, thing) == WORSE


def test_less_urgent_then_back_is_not_worse(team, checks):
    thing = item(severity=WARNING, due=day(40))
    run()
    thing.add_story("Less urgent: was warning, now to note", day(1))
    thing.add_story("Got worse: was to note, now warning", day(2))
    thing.save()
    run(3)
    assert step(team.edu, thing) == NEW


def test_a_passed_date_is_told_once(team, checks):
    thing = item(severity=WARNING, due=day(1), milestones=(3, 0))
    run()
    run(2)
    assert step(team.edu, thing) == OVERDUE
    run(3)
    assert receipts(team.edu, thing).get().last_told_on == day(2)
    assert routing.reason(receipts(team.edu, thing).get()) == "Date passed"


# ---------------------------------------------------------------------------- reactions
def test_a_snooze_until_tomorrow_suppresses_today(team, checks):
    thing = item(severity=WARNING, due=day(9), milestones=(7,), first_seen=day(-1))
    run(-1)
    WatchReceipt.objects.filter(user=team.edu, item=thing).update(snoozed_until=day(3))
    run(2)  # the 7-day milestone falls while snoozed
    assert receipts(team.edu, thing).get().last_told_on == day(-1)
    run(3)
    receipt = receipts(team.edu, thing).get()
    assert receipt.last_told_on == day(3) and receipt.told_step == "7"
    assert receipt.snoozed_until is None


def test_a_snooze_that_ends_reminds_once(team, checks):
    thing = item(severity=WARNING)
    run()
    WatchReceipt.objects.filter(user=team.edu, item=thing).update(snoozed_until=day(1))
    run(1)
    assert step(team.edu, thing) == REMINDER
    run(2)
    assert receipts(team.edu, thing).get().last_told_on == day(1)


def test_not_mine_is_never_told_again_unless_the_item_turns_critical(team, checks):
    thing = item(severity=WARNING, due=day(9), milestones=(7, 3))
    run()
    react(team.edu, thing, Reaction.NOT_MINE)
    for n in range(1, 8):
        run(n)
    assert receipts(team.edu, thing).get().last_told_on == TODAY
    thing.severity = CRITICAL
    thing.add_story("Got worse: was warning, now critical", day(8))
    thing.save()
    run(8)
    assert step(team.edu, thing) == WORSE
    run(9)
    assert receipts(team.edu, thing).get().last_told_on == day(8)


def test_three_not_useful_mute_only_that_check_for_that_person(team, checks):
    first = [item(severity=WARNING) for _ in range(3)]
    run()
    for thing in first:
        react(team.edu, thing, Reaction.NOT_USEFUL)
    muted_warning = item(severity=WARNING, first_seen=day(1))
    muted_critical = item(severity=CRITICAL, first_seen=day(1))
    other_check = item(severity=WARNING, detector=OTHER_CHECK, first_seen=day(1))
    run(1)
    assert step(team.edu, muted_warning) == KNOWN
    assert step(team.edu, muted_critical) == NEW
    assert step(team.edu, other_check) == NEW
    assert step(team.edu2, muted_warning) == NEW


def test_done_hides_it_then_one_reminder_seven_days_later_if_still_open(team, checks):
    thing = item(severity=WARNING)
    run()
    react(team.edu, thing, Reaction.DONE)
    run(6)
    assert receipts(team.edu, thing).get().last_told_on == TODAY
    run(7)
    assert step(team.edu, thing) == STILL_OPEN
    assert routing.reason(receipts(team.edu, thing).get()) == "Still open after you marked it done"
    run(14)
    assert receipts(team.edu, thing).get().last_told_on == day(7)


def test_done_by_a_section_editor_hides_it_for_the_whole_section(team, checks):
    thing = item(severity=WARNING, due=day(9), milestones=(7,))
    run()
    react(team.editor, thing, Reaction.DONE)
    run(2)  # the 7-day milestone
    assert receipts(team.edu, thing).get().last_told_on == TODAY
    assert routing.done_by_section(thing)
    assert routing.item_stats([thing])[thing.key] == {"times_told": 4, "done_by_section": True}


def test_done_by_a_viewer_hides_it_for_them_only(team, checks):
    thing = item(severity=WARNING, due=day(9), milestones=(7,))
    run()
    react(team.edu, thing, Reaction.DONE)
    run(2)
    assert step(team.edu2, thing) == "7"
    assert receipts(team.edu, thing).get().last_told_on == TODAY
    assert not routing.done_by_section(thing)


def test_an_item_marked_wrong_for_everyone_is_told_to_no_one(team, checks):
    thing = item(severity=CRITICAL, state=WatchItem.State.WRONG)
    run()
    assert not receipts(item_=thing).exists()


# ---------------------------------------------------------------------------- resolved
def test_resolved_goes_only_to_people_told_before(team, education):
    setting(CHECK, ON, since=TODAY)
    told = item(severity=CRITICAL, due=day(20))
    quiet = item(severity=WARNING, due=day(20))
    run()
    assert step(team.edu, told) == NEW and step(team.edu, quiet) == KNOWN
    newcomer = person("newcomer", VIEWER, education)
    for thing in (told, quiet):
        thing.state, thing.closed_on = WatchItem.State.CLOSED, day(1)
        thing.save()
    react(team.edu2, told, Reaction.NOT_MINE)
    run(1)
    assert step(team.edu, told) == RESOLVED
    assert receipts(team.edu, told).get().level == GOOD_TO_KNOW
    assert step(team.edu, quiet) == KNOWN  # only recorded, never told: no "resolved"
    assert step(team.edu2, told) == NEW  # said not mine
    assert not receipts(newcomer).exists()
    run(2)
    assert receipts(team.edu, told).get().last_told_on == day(1)


def test_a_quick_pass_tells_nothing_resolved(team, checks):
    thing = item(severity=CRITICAL)
    run()
    thing.state, thing.closed_on = WatchItem.State.CLOSED, TODAY
    thing.save()
    run(mode=QUICK)
    assert step(team.edu, thing) == NEW


def test_an_item_back_after_two_weeks_is_new_again(team, checks):
    thing = item(severity=WARNING)
    run()
    react(team.edu, thing, Reaction.DONE)
    thing.first_seen_on = day(20)  # memory: a new episode
    thing.save()
    run(20)
    receipt = receipts(team.edu, thing).get()
    assert (receipt.told_step, receipt.first_told_on, receipt.milestone_count) == (NEW, day(20), 0)


# ---------------------------------------------------------------------------- escalation
def test_a_critical_finding_open_a_week_with_no_one_assigned_goes_to_the_country_view(team, checks):
    setting("daily_review", ON)
    finding = item(
        severity=CRITICAL,
        detector="daily_review",
        kind=CONCERN,
        key="review:reports_overdue:LEB/PCA1/PD1",
        first_seen=TODAY,
    )
    run()
    assert step(team.edu, finding) == NEW and step(team.admin, finding) == NEW
    assert not receipts(team.boss, finding).exists()
    run(6)
    assert not receipts(team.boss, finding).exists()
    assert routing.item_users(finding, Routing.load(day(7)))[team.boss.pk] == ESCALATION
    run(7)
    assert step(team.boss, finding) == ESCALATED
    assert step(team.admin, finding) == ESCALATED
    assert receipts(team.edu, finding).get().last_told_on == TODAY  # section staff: not escalated
    assert routing.reason(receipts(team.boss, finding).get()) == "Open 7 days, no one assigned"
    run(8)
    assert receipts(team.boss, finding).get().last_told_on == day(7)


def test_no_escalation_once_someone_is_assigned(team, checks):
    setting("daily_review", ON)
    finding = item(
        severity=CRITICAL,
        detector="daily_review",
        kind=CONCERN,
        key="review:pd_ending_soon:LEB/PCA1/PD1",
        first_seen=day(-10),
        assignment_status="assigned",
        has_owner=True,
    )
    assert team.boss.pk not in routing.item_users(finding, Routing.load(TODAY))


# ---------------------------------------------------------------------------- the run and the page
def test_the_counts_go_into_the_runs_details(team, checks):
    item(severity=CRITICAL)
    sync_run = SyncRun.objects.create(job=SyncRun.Job.WATCH, status=SyncRun.Status.RUNNING)
    done = run(sync_run=sync_run)
    sync_run.refresh_from_db()
    assert sync_run.details["receipts"]["needs_you"] == done.needs_you == 4
    assert sync_run.details["receipts"]["steps"] == {NEW: 4}


def test_the_badge_counts_unseen_needs_you_told_today(team, checks):
    first = item(severity=CRITICAL)
    item(severity=INFO, due=day(1))
    run()
    assert routing.unseen_count(team.edu, TODAY) == 1
    WatchReceipt.objects.filter(user=team.edu, item=first).update(seen_at=NOW)
    assert routing.unseen_count(team.edu, TODAY) == 0
    assert routing.unseen_count(team.nobody, TODAY) == 0
    assert [r.item for r in routing.needs_you_today(team.edu, TODAY)] == [first]


def test_receipts_older_than_twelve_months_are_deleted(team, checks):
    thing = item(severity=CRITICAL)
    run()
    WatchReceipt.objects.filter(user=team.edu).update(last_told_on=day(-366))
    assert routing.prune(TODAY) == 1
    assert not receipts(team.edu).exists()
    assert receipts(team.edu2, thing).exists()


def test_announce_writes_only_receipts(team, checks):
    item(severity=CRITICAL)
    with CaptureQueriesContext(connection) as queries:
        run()
    writes = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].split()[0].upper() in ("INSERT", "UPDATE", "DELETE")
    ]
    assert writes and all('"watch_watchreceipt"' in sql for sql in writes)


# ---------------------------------------------------------------------------- checks changing mode
def test_on_the_day_a_check_goes_on_for_staff_what_exists_is_known(team):
    setting(CHECK, TRIAL, since=day(-10))
    existing = item(severity=WARNING, due=day(20), first_seen=day(-2))
    run(-2)
    assert step(team.admin, existing) == NEW and not receipts(team.edu, existing).exists()
    setting(CHECK, ON, since=TODAY)  # an administrator switched it on today
    run()
    assert step(team.edu, existing) == KNOWN
    assert receipts(team.admin, existing).get().last_told_on == day(-2)
    fresh = item(severity=WARNING, due=day(25), first_seen=day(1))
    run(1)
    assert step(team.edu, fresh) == NEW


def test_a_check_back_in_trial_is_quiet_for_the_country_view_on_that_day(team):
    setting(CHECK, ON, since=day(-10))
    existing = item(sections=("Education",), severity=WARNING, due=day(20), first_seen=day(-2))
    run(-2)
    assert not receipts(team.boss, existing).exists()
    setting(CHECK, TRIAL, since=TODAY)  # back in trial today
    run()
    assert step(team.boss, existing) == KNOWN
    assert receipts(team.edu, existing).get().last_told_on == day(-2)  # staff hear no more of it


def test_a_quick_pass_tells_administrators_of_a_new_system_item(team):
    setting("system_health", ON)
    failed = item(kind=SYSTEM, scope=ADMINS, detector="system_health", sections=(), severity=WARNING)
    run(mode=QUICK)
    assert step(team.admin, failed) == NEW
    assert set(receipts(item_=failed).values_list("user__username", flat=True)) == {"admin"}


def test_an_agreed_date_passed_is_worded_as_such(team, checks):
    setting("assignment_due", ON)
    agreed = item(
        detector="assignment_due",
        key="assignment:reports_overdue:LEB/PCA1/PD1",
        due=day(1),
        scope=COUNTRY,
    )
    run()
    run(2)
    receipt = receipts(team.boss, agreed).get()
    assert receipt.told_step == OVERDUE
    assert routing.reason(receipt) == "Agreed date passed"


def test_an_item_no_longer_seen_is_worded_as_such(team, checks):
    thing = item(severity=CRITICAL)
    run()
    thing.state, thing.closed_on = WatchItem.State.GONE, day(1)
    thing.save()
    run(1)
    assert routing.reason(receipts(team.edu, thing).get()) == "No longer seen"


# ---------------------------------------------------------------------------- how a closed item ended
@pytest.mark.parametrize(
    "kind, told, words",
    [
        (WatchItem.CloseKind.RESOLVED, RESOLVED, "Resolved"),
        (WatchItem.CloseKind.MISSED, MISSED, "Date passed, not done"),
        (WatchItem.CloseKind.CHANGED, CLOSED, "No longer followed here"),
    ],
)
def test_a_closed_item_is_told_as_it_ended_never_resolved_when_its_date_passed(
    team, checks, kind, told, words
):
    thing = item(severity=WARNING, due=day(5))
    run()
    assert step(team.edu, thing) == NEW
    thing.state, thing.closed_on, thing.close_kind = WatchItem.State.CLOSED, day(6), kind
    thing.close_reason = "now overdue: see the daily review"
    thing.save()
    run(6)
    receipt = receipts(team.edu, thing).get()
    assert (receipt.told_step, receipt.level, receipt.last_told_on) == (told, GOOD_TO_KNOW, day(6))
    assert routing.reason(receipt) == words
    run(7)
    assert receipts(team.edu, thing).get().last_told_on == day(6)  # told once


def test_a_report_now_overdue_is_closed_as_missed_by_the_memory(team, checks):
    """The check's own words decide: "now overdue" is a missed date, told as such."""
    from neurodb.watch.detectors import MISSED as MISSED_KIND
    from neurodb.watch.detectors import Close, Detector, Result

    thing = item(severity=WARNING, due=day(-1))
    check = Detector(id=CHECK, label="Reports due soon", run=lambda ctx: ())
    result = Result(detector=check, mode=ON, mark="9999")
    result.closes[thing.key] = Close("now overdue: see the daily review", kind=MISSED_KIND)
    outcome = memory.apply(ctx(), [result])
    thing.refresh_from_db()
    assert outcome.closed == [thing.key]
    assert (thing.state, thing.close_kind) == (WatchItem.State.CLOSED, WatchItem.CloseKind.MISSED)


# ---------------------------------------------------------------------------- got worse, the same day
def test_a_rise_later_the_day_they_were_told_is_told_the_next_morning(team, checks):
    thing = item(severity=INFO, due=day(20))
    run()  # the morning: told as new, to note
    assert step(team.edu, thing) == NEW and receipts(team.edu, thing).get().told_severity == INFO
    # a quick pass that afternoon raises it: not urgent, so the quick pass does not tell it
    thing.severity = WARNING
    thing.add_story("Got worse: was to note, now warning", TODAY)
    thing.save()
    run(0, mode=QUICK)
    assert step(team.edu, thing) == NEW
    run(1)  # the next morning: nothing got worse in that pass, but it is worse than what they were told
    receipt = receipts(team.edu, thing).get()
    assert (receipt.told_step, receipt.told_severity, receipt.level) == (WORSE, WARNING, NEEDS_YOU)


# ---------------------------------------------------------------------------- answers given while it runs
def test_an_answer_or_a_visit_made_while_announcing_is_never_written_over(team, checks, monkeypatch):
    thing = item(severity=WARNING, due=day(9), milestones=(7,))
    run()
    real = routing._within_limits

    def meanwhile(*args, **kwargs):
        kept = real(*args, **kwargs)
        # while the pass runs: edu asks to hear about it later, edu2 opens the page
        WatchReceipt.objects.filter(user=team.edu, item=thing).update(snoozed_until=day(5))
        WatchReceipt.objects.filter(user=team.edu2, item=thing).update(seen_at=NOW)
        return kept

    monkeypatch.setattr(routing, "_within_limits", meanwhile)
    done = run(2)  # the 7-day milestone
    edu = receipts(team.edu, thing).get()
    assert edu.snoozed_until == day(5) and edu.told_step == NEW  # their reminder holds: not told over it
    assert done.deferred >= 1
    edu2 = receipts(team.edu2, thing).get()
    assert edu2.told_step == "7" and edu2.seen_at is None  # told again: unseen until they look


# ---------------------------------------------------------------------------- a section's "Something's wrong"
def test_a_section_editors_wrong_hides_it_for_their_section_only(team, checks, health):
    thing = item(sections=("Education", "Health"), severity=WARNING, due=day(9), milestones=(7,))
    thing.evidence_hash = memory.fingerprint(thing.evidence)
    thing.save()
    run()
    page.react(receipts(team.editor, thing).get(), reaction=Reaction.WRONG, now=NOW)
    thing.refresh_from_db()
    assert thing.state == WatchItem.State.OPEN
    run(2)  # the 7-day milestone
    assert receipts(team.edu, thing).get().last_told_on == TODAY  # Education: hidden
    assert step(team.hlt, thing) == "7"  # Health: still told
    assert routing.wrong_by_section([thing]) == {thing.pk: {thing.section_ids[0]}}
    # its evidence changes: Education hears of it again
    thing.evidence_hash = "the records changed"
    thing.save()
    assert routing.wrong_by_section([thing]) == {}


def test_a_section_editors_wrong_on_an_item_of_their_section_only_is_wrong_for_everyone(team, checks):
    thing = item(severity=WARNING)
    run()
    page.react(receipts(team.editor, thing).get(), reaction=Reaction.WRONG, now=NOW)
    thing.refresh_from_db()
    assert thing.state == WatchItem.State.WRONG


# ---------------------------------------------------------------------------- a check kept in trial
def test_saving_a_check_kept_in_trial_does_not_make_its_new_points_known(team):
    setting(CHECK, TRIAL, since=day(-10))
    # "Keep in trial" in the admin, or a save that changes nothing: its time of change moves
    DetectorSetting.objects.filter(detector=CHECK).update(updated_at=NOW)
    fresh = item(severity=WARNING, due=day(20), first_seen=TODAY)
    run()
    assert step(team.admin, fresh) == NEW


def test_a_check_going_into_trial_records_the_day(db):
    row = DetectorSetting.objects.create(detector="new_check", mode=TRIAL)
    assert row.trial_since is not None
    row.mode = ON
    row.save()
    assert row.on_since is not None


# ---------------------------------------------------------------------------- the morning note's counts
def test_the_morning_notes_counts_read_the_receipts_in_two_queries(team, checks):
    things = [item(severity=WARNING) for _ in range(6)]
    run()
    for thing in things[:3]:
        react(team.editor, thing, Reaction.DONE)
    rules = Routing.load(TODAY)
    with CaptureQueriesContext(connection) as queries:
        stats = routing.item_stats(things, rules)
    assert len(queries.captured_queries) == 2
    assert [stats[t.key]["done_by_section"] for t in things] == [True] * 3 + [False] * 3
