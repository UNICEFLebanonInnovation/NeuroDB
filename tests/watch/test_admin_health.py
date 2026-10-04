"""NeuroDB Watch in the admin: the usefulness score of each check, a check going back to trial by itself,
"Not mine" per eTools section name and section, the usefulness gate, and the watch's own lines under
"Needs attention"."""

import datetime

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import VIEWER
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.watch import memory, precision, services
from neurodb.watch.detectors import (
    ADMINS,
    DAILY,
    DEADLINE,
    ON,
    SECTION,
    STALE_DETECTOR,
    SYSTEM,
    TRIAL,
    WARNING,
    Context,
    system,
)
from neurodb.watch.models import DetectorSetting, SectionMatch, WatchItem, WatchReceipt, WatchState
from neurodb.web import health

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut
TODAY = datetime.date(2026, 10, 5)
CHECK = "report_due_soon"  # "Progress reports due soon"
OTHER = "action_point_due"  # "Action points due soon"
Reaction = WatchReceipt.Reaction
NEEDS_YOU, GOOD_TO_KNOW = WatchReceipt.Level.NEEDS_YOU, WatchReceipt.Level.GOOD_TO_KNOW


def day(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


# ---------------------------------------------------------------------------- the world
_numbers = iter(range(1, 100_000))


def point(detector=CHECK, sections=("Education",), kind=DEADLINE, scope=SECTION) -> WatchItem:
    n = next(_numbers)
    return WatchItem.objects.create(
        key=f"due:test:{n}",
        detector=detector,
        kind=kind,
        severity=WARNING,
        title=f"Thing {n}",
        etools_sections=list(sections),
        scope=scope,
        evidence={
            "source": "test",
            "records": [{"label": f"record {n}", "date": "", "value": "", "url": ""}],
        },
        first_seen_on=day(-40),
        last_seen_on=TODAY,
        changed_on=day(-40),
    )


def told(user, item, reaction="", on=TODAY, step="new", level=NEEDS_YOU, told_on=None) -> WatchReceipt:
    """A receipt: the person was told about the point on ``told_on`` (default: the day of the
    reaction) and reacted on ``on``."""
    when = datetime.datetime.combine(on, datetime.time(8), tzinfo=UTC)
    return WatchReceipt.objects.create(
        user=user,
        item=item,
        first_told_on=told_on or on,
        last_told_on=told_on or on,
        told_step=step,
        level=level,
        reaction=reaction,
        reacted_at=when if reaction else None,
    )


def rate(user, detector=CHECK, on=TODAY, level=NEEDS_YOU, kind=DEADLINE, scope=SECTION, **counts) -> None:
    """``counts`` reactions (useful=12, not_useful=8...) by ``user``, each on its own point of the check."""
    for reaction, n in counts.items():
        for _ in range(n):
            told(user, point(detector, kind=kind, scope=scope), reaction, on=on, level=level)


def setting(detector=CHECK, mode=ON, on_since=None) -> DetectorSetting:
    row = DetectorSetting.objects.create(detector=detector, mode=mode, on_since=on_since or day(-60))
    return DetectorSetting.objects.get(pk=row.pk)


@pytest.fixture
def education(db):
    section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(etools_name="Education", section=section, how="exact", confirmed=True)
    return section


@pytest.fixture
def health_section(db):
    return Section.objects.create(name="Health", code="HLT")


def person(username, section=None) -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.org")
    user.groups.add(Group.objects.get(name=VIEWER))
    if section is not None:
        user.section = section
        user.save(update_fields=["section"])
    return user


@pytest.fixture
def staff(roles, education):
    return person("edu", education)


@pytest.fixture
def superuser(db, roles):
    return User.objects.create_superuser(username="root", email="root@example.org", password="root-pass-1234")


@pytest.fixture
def client_super(client, superuser):
    client.force_login(superuser)
    return client


# ---------------------------------------------------------------------------- the score
def test_the_score_counts_the_last_30_days_and_leaves_done_and_not_mine_out(staff):
    rate(staff, useful=6, not_useful=2, wrong=1, not_mine=3, done=4)
    rate(staff, useful=9, on=day(-31))  # before the window: not counted
    rate(staff, OTHER, not_useful=1)
    item = point()
    told(staff, item, step=WatchReceipt.KNOWN)  # known, never told: not counted as told
    told(staff, point(), told_on=day(-31))  # told before the window

    scores = precision.scores(TODAY)

    score = scores[CHECK]
    assert (score.told, score.useful, score.not_useful, score.wrong, score.not_mine) == (16, 6, 2, 1, 3)
    assert score.rated == 9 and score.score == pytest.approx(6 / 9)
    assert score.label == "Progress reports due soon"
    assert scores[OTHER].score == 0 and scores[OTHER].rated == 1


def test_a_check_with_a_setting_and_no_reaction_has_no_score(db):
    setting(OTHER, TRIAL)
    score = precision.scores(TODAY)[OTHER]
    assert (score.told, score.rated, score.score) == (0, 0, None)


def test_percent_rounds_half_up():
    assert precision.percent(9, 20) == 45
    assert precision.percent(1, 8) == 13  # 12.5
    assert precision.percent(2, 3) == 67
    assert precision.percent(0, 0) == 0


# ---------------------------------------------------------------------------- back to trial
def test_a_check_goes_back_to_trial_above_40_percent_of_20_ratings(staff):
    setting(CHECK)
    rate(staff, useful=11, not_useful=6, wrong=3)  # 9 of 20: 45%

    result = precision.demote(TODAY, now=NOW)

    row = DetectorSetting.objects.get(detector=CHECK)
    assert row.mode == TRIAL and row.demoted_at == NOW and row.updated_by == precision.DEMOTED_BY
    assert row.demoted_reason == "45% of 20 reactions said not useful or something's wrong"
    assert result == {"checks": 1, "demoted": {CHECK: row.demoted_reason}}
    assert precision.demote(TODAY, now=NOW) == {"checks": 0, "demoted": {}}  # in trial now: left alone


def test_exactly_40_percent_stays_on(staff):
    setting(CHECK)
    rate(staff, useful=12, not_useful=8)  # 8 of 20: not more than 40%
    assert precision.demote(TODAY, now=NOW)["demoted"] == {}
    assert DetectorSetting.objects.get(detector=CHECK).mode == ON


def test_never_with_fewer_than_20_ratings(staff):
    setting(CHECK)
    rate(staff, not_useful=15, wrong=4, done=10, not_mine=10)  # 19 ratings, all unhelpful
    assert precision.demote(TODAY, now=NOW)["demoted"] == {}
    assert DetectorSetting.objects.get(detector=CHECK).mode == ON


def test_only_ratings_since_it_was_switched_on_count(staff):
    setting(CHECK, on_since=day(-5))
    rate(staff, not_useful=20, on=day(-10))  # in trial then: the whole-country view's ratings
    rate(staff, useful=5, not_useful=5, on=day(-1))
    assert precision.demotion(DetectorSetting.objects.get(detector=CHECK), TODAY).rated == 10
    assert precision.demote(TODAY, now=NOW)["demoted"] == {}


def test_points_told_to_the_administrators_only_do_not_count(staff):
    setting(CHECK)
    setting(system.ID)
    rate(staff, useful=5, not_useful=5)
    rate(staff, not_useful=20, scope=ADMINS)  # donor accounts, the rollover: a trial changes nothing
    rate(staff, system.ID, kind=SYSTEM, scope=ADMINS, not_useful=30)

    assert precision.demote(TODAY, now=NOW) == {
        "checks": 1,
        "demoted": {},
    }  # the system check is not looked at
    assert set(DetectorSetting.objects.values_list("mode", flat=True)) == {ON}


def test_a_check_in_trial_or_off_is_never_moved(staff):
    setting(CHECK, TRIAL)
    setting(OTHER, DetectorSetting.Mode.OFF)
    rate(staff, not_useful=25)
    rate(staff, OTHER, not_useful=25)
    assert precision.demote(TODAY, now=NOW) == {"checks": 0, "demoted": {}}


def test_just_above_the_threshold_the_reason_does_not_round_down_to_it(staff):
    setting(CHECK)
    rate(staff, useful=120, not_useful=81)  # 81 of 201: 40.3%
    assert precision.demote(TODAY, now=NOW)["demoted"] == {
        CHECK: "40.3% of 201 reactions said not useful or something's wrong"
    }


def test_demoting_writes_only_the_checks_settings(staff):
    setting(CHECK)
    rate(staff, not_useful=20)
    with CaptureQueriesContext(connection) as queries:
        precision.demote(TODAY, now=NOW)
    writes = [
        q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes and all('"watch_detectorsetting"' in sql for sql in writes)


def test_the_morning_pass_runs_it_before_the_checks(staff):
    setting(CHECK)
    rate(staff, not_useful=20)
    run = SyncRun.objects.create(job=SyncRun.Job.WATCH, target=DAILY, status=SyncRun.Status.RUNNING)

    services._usefulness(run, TODAY)

    assert run.details["usefulness"]["demoted"] == {
        CHECK: "100% of 20 reactions said not useful or something's wrong"
    }
    assert DetectorSetting.objects.get(detector=CHECK).mode == TRIAL


# ---------------------------------------------------------------------------- Needs attention
def lines(now=NOW) -> dict[str, health.Warning]:
    return {line.key: line for line in health.warnings(now)}


def test_a_check_back_in_trial_is_a_line_until_an_administrator_decides(staff, client_super, settings):
    setting(CHECK)
    rate(staff, useful=11, not_useful=9)
    precision.demote(TODAY, now=NOW)
    key = f"watch_check_demoted:{CHECK}"

    line = lines()[key]
    assert line.text == (
        "The check “Progress reports due soon” went back to trial: "
        "45% of 20 reactions said not useful or something's wrong."
    )
    assert line.level == WARNING and line.on == TODAY
    row = DetectorSetting.objects.get(detector=CHECK)
    assert line.url == reverse("admin:watch_detectorsetting_change", args=[row.pk])

    # the system check tells the administrators, as an item of their own
    memory.run(Context.make(DAILY, now=NOW), [system.SYSTEM_HEALTH])
    item = WatchItem.objects.get(key=f"system:{key}")
    assert (item.scope, item.kind, item.state) == (ADMINS, SYSTEM, "open")

    settings.WATCH_ENABLED = False
    assert key not in lines()
    settings.WATCH_ENABLED = True

    # switched on again in the admin: the notice goes and the ratings start again from that day
    response = client_super.post(
        reverse("admin:watch_detectorsetting_change", args=[row.pk]), {"mode": ON}, follow=True
    )
    assert response.status_code == 200
    row.refresh_from_db()
    assert (row.mode, row.demoted_at, row.demoted_reason) == (ON, None, "")
    assert row.on_since == timezone.localdate()
    assert key not in lines()


def test_keep_in_trial_clears_the_notice(staff, client_super):
    setting(CHECK)
    rate(staff, not_useful=20)
    precision.demote(TODAY, now=NOW)
    row = DetectorSetting.objects.get(detector=CHECK)

    response = client_super.post(
        reverse("admin:watch_detectorsetting_changelist"),
        {"action": "keep_in_trial", "_selected_action": [row.pk]},
        follow=True,
    )

    assert response.status_code == 200 and "1 check(s) kept in trial." in response.content.decode()
    row.refresh_from_db()
    assert (row.mode, row.demoted_at, row.demoted_reason) == (TRIAL, None, "")
    assert f"watch_check_demoted:{CHECK}" not in lines()


def test_each_watch_line_appears_under_its_condition(staff, client_super):
    now = timezone.now()
    keys = ("watch_not_run", "watch_ai_paused", "watch_sections_unconfirmed", f"watch_check_demoted:{CHECK}")
    SyncRun.objects.create(
        job=SyncRun.Job.WATCH,
        target=DAILY,
        status=SyncRun.Status.SUCCEEDED,
        started_at=now - datetime.timedelta(hours=2),
        finished_at=now - datetime.timedelta(hours=2),
    )
    assert ScheduledJob.objects.get(command="watch").enabled
    assert not set(keys) & set(lines(now))  # all well: none of them

    SyncRun.objects.filter(job=SyncRun.Job.WATCH).update(
        started_at=now - datetime.timedelta(hours=27), finished_at=now - datetime.timedelta(hours=27)
    )
    WatchState.objects.create(
        pk=1, ai_paused_until=now + datetime.timedelta(hours=3), ai_pause_reason="the OpenAI credit ran out"
    )
    SectionMatch.objects.create(etools_name="Water, Sanitation and Hygiene", how=SectionMatch.How.NONE)
    DetectorSetting.objects.create(
        detector=CHECK, mode=TRIAL, demoted_at=now, demoted_reason="45% of 20 reactions said not useful"
    )

    assert set(keys) <= set(lines(now))
    html = client_super.get(reverse("admin:index")).content.decode()
    assert "NeuroDB Watch has not run for more than 26 hours" in html
    assert "NeuroDB Watch stopped using AI until" in html and "the OpenAI credit ran out" in html
    assert "1 eTools section name has no confirmed NeuroDB section (Water, Sanitation and Hygiene)" in html
    assert (
        "The check “Progress reports due soon” went back to trial: 45% of 20 reactions said not useful."
        in html
    )


# ---------------------------------------------------------------------------- not mine
def test_not_mine_per_eTools_name_section_and_check(roles, education, health_section):
    SectionMatch.objects.create(etools_name="WASH", section=health_section, how="manual", confirmed=True)
    nurse, doctor, teacher = (
        person("nurse", health_section),
        person("doctor", health_section),
        person("t", education),
    )
    told(nurse, point(CHECK, ("WASH",)), Reaction.NOT_MINE)
    told(doctor, point(CHECK, ("wash ",)), Reaction.NOT_MINE)  # the same name, spelled otherwise
    told(nurse, point(OTHER, ("WASH", "Health")), Reaction.NOT_MINE)
    told(teacher, point(CHECK, ("WASH",)), Reaction.NOT_MINE)  # another section's staff: not this match
    told(nurse, point(CHECK, ("WASH",)), Reaction.NOT_MINE, on=day(-31))  # too long ago
    told(nurse, point(CHECK, ("WASH",)), Reaction.USEFUL)

    counts = precision.not_mine(TODAY)

    assert precision.not_mine_for("Wash", health_section.pk, counts) == {CHECK: 2, OTHER: 1}
    assert precision.not_mine_for("WASH", education.pk, counts) == {CHECK: 1}
    assert precision.not_mine_for("WASH", None, counts) == {}
    assert counts[(OTHER, health_section.pk, "health")] == 1


def test_the_section_names_list_shows_the_not_mine_beside_each_name(
    roles, client_super, education, health_section
):
    SectionMatch.objects.create(etools_name="WASH", section=health_section, how="manual", confirmed=True)
    nurse = person("nurse", health_section)
    for _ in range(2):
        told(nurse, point(CHECK, ("WASH",)), Reaction.NOT_MINE, on=timezone.localdate())
    told(nurse, point(OTHER, ("WASH",)), Reaction.NOT_MINE, on=timezone.localdate())

    response = client_super.get(reverse("admin:watch_sectionmatch_changelist"))

    html = response.content.decode()
    assert response.status_code == 200 and "Not mine (30 days)" in html
    assert "Progress reports due soon: 2 · Action points due soon: 1" in html


# ---------------------------------------------------------------------------- the checks' list
def test_the_checks_list_shows_each_score_and_the_gate(staff, client_super):
    setting(CHECK)
    setting(STALE_DETECTOR)
    rate(staff, useful=3, not_useful=1, on=timezone.localdate())

    response = client_super.get(reverse("admin:watch_detectorsetting_changelist"))

    html = response.content.decode()
    assert response.status_code == 200
    assert "Progress reports due soon" in html and "Data sources not refreshed" in html
    assert "75%" in html  # 3 of 4 useful
    assert "Usefulness gate, for information:" in html
    assert "75% of the 4 “Needs you” points people rated in the last 4 weeks were useful" in html

    row = DetectorSetting.objects.get(detector=CHECK)
    change = client_super.get(reverse("admin:watch_detectorsetting_change", args=[row.pk])).content.decode()
    assert "4 people told · 3 useful · 1 not useful · 0 something" in change


# ---------------------------------------------------------------------------- the gate
def test_the_gate_is_false_below_50_percent_useful(staff):
    setting(CHECK, on_since=day(-40))
    rate(staff, useful=10, not_useful=8, wrong=3)  # 10 of 21

    gate = precision.gate(TODAY)

    assert not gate and gate.rated == 21 and gate.useful == 10
    assert gate.text.endswith("the gate is not met (below 50%).")


def test_the_gate_is_met_at_half_with_20_ratings_after_4_weeks_on(staff):
    setting(CHECK, on_since=day(-40))
    rate(staff, useful=10, not_useful=10)
    rate(staff, not_useful=5, level=GOOD_TO_KNOW)  # good to know: not counted
    rate(staff, system.ID, kind=SYSTEM, scope=ADMINS, not_useful=9)  # system points: not counted
    rate(staff, not_useful=9, on=day(-29))  # before the 4 weeks
    rate(staff, done=4, not_mine=4)  # not ratings

    gate = precision.gate(TODAY)

    assert gate and (gate.useful, gate.rated) == (10, 20)
    assert (
        gate.text
        == "50% of the 20 “Needs you” points people rated in the last 4 weeks were useful: the gate is met."
    )


def test_the_gate_needs_20_ratings_and_a_check_on_for_4_weeks(staff):
    setting(CHECK, on_since=day(-40))
    rate(staff, useful=19)
    assert not precision.gate(TODAY)
    assert precision.gate(TODAY).text.endswith("(fewer than 20 ratings).")

    DetectorSetting.objects.filter(detector=CHECK).update(on_since=day(-10))
    setting(system.ID, on_since=day(-90))  # the administrators' own check does not count
    rate(staff, useful=5)
    gate = precision.gate(TODAY)
    assert not gate and gate.rated == 24
    assert gate.text.endswith("(no check has been on for staff for 4 weeks yet).")
