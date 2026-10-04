"""NeuroDB Watch's checks framework and memory: what opens, changes, closes and goes, with fake checks."""

import datetime

import pytest

from neurodb.accounts.models import Section
from neurodb.core.models import SyncRun
from neurodb.watch import detectors, memory
from neurodb.watch.detectors import (
    ADMINS,
    DAILY,
    DEADLINE,
    INFO,
    QUICK,
    STALE_DETECTOR,
    SYSTEM,
    WARNING,
    Attach,
    Candidate,
    Close,
    Context,
    Detector,
    mark_of,
    milestone,
    milestones_of,
    record,
)
from neurodb.watch.models import KEY_MAX, STORY_MAX, DetectorSetting, SectionMatch, WatchItem

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut
TODAY = datetime.date(2026, 10, 5)
KEY = "due:report:LEB/PCA2026001/PD2026001:7"
DATAMART = SyncRun.Job.ETOOLS_DATAMART


def ctx(day: int = 0, mode: str = DAILY) -> Context:
    return Context.make(mode, now=NOW + datetime.timedelta(days=day))


def mark(day: int) -> str:
    return mark_of(NOW + datetime.timedelta(days=day))


def candidate(key: str = KEY, value: str = "due", **fields) -> Candidate:
    values = {
        "key": key,
        "kind": DEADLINE,
        "severity": INFO,
        "title": "Progress report due 15 Oct 2026: LEB/PCA2026001/PD2026001 (Partner A)",
        "due_date": datetime.date(2026, 10, 15),
        "evidence": {
            "source": "eTools progress reports",
            "source_job": DATAMART,
            "synced_at": "2026-10-04T20:41:00+03:00",
            "records": [record("QPR 7", datetime.date(2026, 10, 15), value, "/programme/1/")],
            "numbers": {"indicators": 3},
        },
    }
    values.update(fields)
    return Candidate(**values)


class Fake:
    """A check whose findings, positive closes and source mark each test sets."""

    def __init__(self, check_id: str = "fake_check", jobs: tuple[str, ...] = (), **options):
        self.found: dict[str, dict] = {}  # key -> candidate fields
        self.attach: list[Attach] = []
        self.closes: dict = {}
        self.mark = mark(0)
        self.raises: Exception | None = None
        self.detector = Detector(
            id=check_id,
            label="A fake check",
            run=self.run,
            resolved=lambda ctx, items: {k: v for k, v in self.closes.items() if k in {i.key for i in items}},
            source_mark=lambda ctx: self.mark,
            source_jobs=jobs,
            **options,
        )

    def run(self, ctx):
        if self.raises:
            raise self.raises
        return [candidate(key, **fields) for key, fields in self.found.items()] + self.attach

    def pass_(self, context: Context, *others: "Fake", **kwargs) -> memory.Outcome:
        return memory.run(context, [self.detector, *(o.detector for o in others)], **kwargs)


def item(key: str = KEY) -> WatchItem:
    return WatchItem.objects.get(key=key)


def stories(key: str = KEY) -> list[str]:
    return [line["text"] for line in item(key).story]


@pytest.fixture
def check():
    fake = Fake()
    fake.found[KEY] = {}
    return fake


def _succeeded(job: str, at: datetime.datetime) -> SyncRun:
    return SyncRun.objects.create(
        job=job,
        status=SyncRun.Status.SUCCEEDED,
        started_at=at - datetime.timedelta(minutes=5),
        finished_at=at,
    )


# ---------------------------------------------------------------------------- the day and the pass
def test_at_21_30_utc_on_4_october_today_is_5_october_in_beirut():
    context = Context.make(now=datetime.datetime(2026, 10, 4, 21, 30, tzinfo=UTC))
    assert context.today == datetime.date(2026, 10, 5)
    assert context.daily
    assert not Context.make(QUICK).daily
    with pytest.raises(ValueError):
        Context.make("weekly")


# ---------------------------------------------------------------------------- first sight and story
def test_a_new_item_is_remembered_with_its_evidence_and_first_line(check):
    outcome = check.pass_(ctx())
    found = item()
    assert outcome.new == [KEY]
    assert found.state == WatchItem.State.OPEN
    assert found.detector == "fake_check"
    assert found.first_seen_on == found.last_seen_on == found.changed_on == TODAY
    assert found.source_mark == mark(0)
    assert found.evidence["records"][0]["label"] == "QPR 7"
    assert found.evidence_hash
    assert stories() == ["First noticed"]
    assert outcome.details()["detectors"]["fake_check"]["new"] == 1


def test_a_second_pass_on_the_same_day_changes_nothing(check):
    check.pass_(ctx())
    again = check.pass_(ctx())
    assert (again.new, again.reopened, again.worse, again.changed, again.closed, again.missed) == (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    assert again.seen == 1
    assert stories() == ["First noticed"]
    assert WatchItem.objects.count() == 1


def test_changed_on_and_the_story_move_only_with_severity_due_date_or_state(check):
    check.pass_(ctx(0))
    check.found[KEY] = {"title": "New wording, same thing"}
    check.pass_(ctx(1))
    assert item().changed_on == TODAY and item().last_seen_on == TODAY + datetime.timedelta(days=1)
    assert item().title == "New wording, same thing"

    check.found[KEY] = {"severity": WARNING}
    outcome = check.pass_(ctx(2))
    assert outcome.worse == [KEY] and outcome.changed == []
    assert item().changed_on == TODAY + datetime.timedelta(days=2)
    assert stories()[-1] == "Got worse: was to note, now warning"

    check.found[KEY] = {"severity": WARNING, "due_date": datetime.date(2026, 10, 20)}
    outcome = check.pass_(ctx(3))
    assert outcome.changed == [KEY] and outcome.worse == []
    assert stories()[-1] == "Due date moved to 20 Oct 2026"

    check.found[KEY] = {"severity": INFO, "due_date": datetime.date(2026, 10, 20)}
    check.pass_(ctx(4))
    assert stories()[-1] == "Less urgent: was warning, now to note"
    assert len(stories()) == 4


def test_the_story_keeps_the_latest_30_lines(check):
    for day in range(40):
        check.found[KEY] = {"severity": WARNING if day % 2 else INFO}
        check.pass_(ctx(day))
    story = item().story
    assert len(story) == STORY_MAX == 30
    assert story[-1]["on"] == (TODAY + datetime.timedelta(days=39)).isoformat()
    assert item().first_seen_on == TODAY


def test_a_key_longer_than_320_characters_is_one_item_across_passes():
    fake = Fake()
    long_key = "due:action_points:" + "Section " * 50
    fake.found[long_key] = {}
    fake.pass_(ctx(0))
    fake.pass_(ctx(1))
    assert WatchItem.objects.count() == 1
    assert len(WatchItem.objects.get().key) <= KEY_MAX


# ---------------------------------------------------------------------------- evidence
def test_an_item_without_evidence_is_refused():
    with pytest.raises(ValueError, match="evidence"):
        candidate(evidence={})
    with pytest.raises(ValueError, match="evidence"):
        candidate(evidence={"source": "eTools", "records": []})
    with pytest.raises(ValueError):
        candidate(severity="urgent")


def test_a_check_giving_an_item_without_evidence_fails_and_writes_nothing(check):
    def bad(ctx):
        return [candidate(evidence={"source": "eTools"})]

    outcome = memory.run(ctx(), [Detector(id="bad_check", label="Bad", run=bad), check.detector])
    assert "evidence" in outcome.errors["bad_check"]
    assert not WatchItem.objects.filter(detector="bad_check").exists()
    assert item().detector == "fake_check"  # the other check still ran


# ---------------------------------------------------------------------------- closing
def test_positive_evidence_closes_at_once(check):
    check.pass_(ctx(0))
    check.found = {}
    check.closes = {KEY: "report submitted on 6 Oct 2026"}
    outcome = check.pass_(ctx(1, QUICK))  # also in a quick pass
    closed = item()
    assert outcome.closed == [KEY]
    assert closed.state == WatchItem.State.CLOSED
    assert closed.close_reason == "report submitted on 6 Oct 2026"
    assert closed.closed_on == TODAY + datetime.timedelta(days=1)
    assert stories()[-1] == "Closed: report submitted on 6 Oct 2026"
    assert outcome.details()["detectors"]["fake_check"]["closed"] == 1


def test_a_close_can_hand_the_item_over_to_the_daily_review(check):
    check.pass_(ctx(0))
    check.found = {}
    check.closes = {KEY: Close("now overdue: see the daily review", review_key="reports_overdue:PD2026001")}
    check.pass_(ctx(1))
    assert item().review_key == "reports_overdue:PD2026001"
    assert item().close_reason == "now overdue: see the daily review"


def test_one_miss_keeps_it_open_and_two_make_it_gone(check):
    check.pass_(ctx(0))
    check.found = {}
    check.mark = mark(1)
    outcome = check.pass_(ctx(1))
    assert outcome.missed == [KEY] and outcome.gone == []
    assert item().state == WatchItem.State.OPEN and item().missed_runs == 1

    check.pass_(ctx(1))  # the same data again: not a second miss
    assert item().missed_runs == 1

    check.mark = mark(2)
    outcome = check.pass_(ctx(2))
    gone = item()
    assert outcome.gone == [KEY]
    assert gone.state == WatchItem.State.GONE
    assert gone.close_reason == "No longer in eTools progress reports"
    assert gone.closed_on == TODAY + datetime.timedelta(days=2)
    assert stories()[-1] == "No longer in eTools progress reports"
    assert outcome.details()["detectors"]["fake_check"]["gone"] == 1


def test_found_again_after_one_miss_starts_counting_again(check):
    check.pass_(ctx(0))
    check.found, check.mark = {}, mark(1)
    check.pass_(ctx(1))
    check.found, check.mark = {KEY: {}}, mark(2)
    check.pass_(ctx(2))
    check.found, check.mark = {}, mark(3)
    check.pass_(ctx(3))
    assert item().state == WatchItem.State.OPEN and item().missed_runs == 1


def test_a_quick_pass_never_counts_a_miss(check):
    check.pass_(ctx(0))
    check.found = {}
    for day in (1, 2, 3):
        check.mark = mark(day)
        outcome = check.pass_(ctx(day, QUICK))
        assert outcome.missed == []
    assert item().state == WatchItem.State.OPEN and item().missed_runs == 0


def test_an_item_seen_on_newer_data_than_the_check_read_is_not_missed(check):
    check.mark = mark(3)  # a quick pass saw it on newer data
    check.pass_(ctx(0, QUICK))
    check.found = {}
    check.mark = mark(2)  # the morning pass reads older data
    outcome = check.pass_(ctx(1))
    assert outcome.missed == [] and item().missed_runs == 0
    check.mark = mark(4)
    check.pass_(ctx(2))
    assert item().missed_runs == 1


def test_a_check_that_cannot_tell_how_fresh_its_data_is_never_misses(check):
    check.pass_(ctx(0))
    check.found, check.mark = {}, ""
    check.pass_(ctx(1))
    assert item().missed_runs == 0


# ---------------------------------------------------------------------------- coming back
def _make_gone(check: Fake) -> None:
    check.pass_(ctx(0))
    check.found = {}
    for day in (1, 2):
        check.mark = mark(day)
        check.pass_(ctx(day))
    assert item().state == WatchItem.State.GONE


def test_back_within_14_days_reopens_the_same_row_and_is_not_new(check):
    _make_gone(check)
    pk = item().pk
    check.found, check.mark = {KEY: {}}, mark(10)
    outcome = check.pass_(ctx(10))
    back = item()
    assert back.pk == pk
    assert outcome.reopened == [KEY] and outcome.new == []
    assert back.state == WatchItem.State.OPEN
    assert back.first_seen_on == TODAY
    assert back.closed_on is None and back.close_reason == "" and back.missed_runs == 0
    assert stories()[-1] == "Back again (it went on 7 Oct 2026)"


def test_back_worse_is_reopened_and_worse(check):
    _make_gone(check)
    check.found, check.mark = {KEY: {"severity": WARNING}}, mark(5)
    outcome = check.pass_(ctx(5))
    assert outcome.reopened == [KEY] and outcome.worse == [KEY] and outcome.changed == []


def test_back_after_14_days_starts_a_new_episode(check):
    _make_gone(check)
    check.found, check.mark = {KEY: {}}, mark(20)
    outcome = check.pass_(ctx(20))
    assert outcome.new == [KEY] and outcome.reopened == []
    assert item().first_seen_on == TODAY + datetime.timedelta(days=20)
    assert stories()[-1] == "Noticed again"
    assert WatchItem.objects.count() == 1


# ---------------------------------------------------------------------------- marked wrong
def test_an_item_marked_wrong_comes_back_only_when_its_evidence_changes(check):
    check.pass_(ctx(0))
    WatchItem.objects.filter(key=KEY).update(state=WatchItem.State.WRONG)  # Something's wrong, by an editor

    # The same records and numbers, synced again and one day nearer: still hidden
    check.found[KEY] = {
        "evidence": {
            **candidate().evidence,
            "synced_at": "2026-10-05T20:41:00+03:00",
            "numbers": {"indicators": 3, "days_left": 9},
        }
    }
    check.mark = mark(1)
    outcome = check.pass_(ctx(1))
    assert item().state == WatchItem.State.WRONG
    assert (outcome.new, outcome.reopened, outcome.worse, outcome.changed) == ([], [], [], [])

    check.found = {}  # not missed while it is marked wrong
    check.mark = mark(2)
    check.pass_(ctx(2))
    check.mark = mark(3)
    check.pass_(ctx(3))
    assert item().state == WatchItem.State.WRONG and item().missed_runs == 0

    check.found = {KEY: {"value": "sent back to the partner"}}
    check.mark = mark(4)
    outcome = check.pass_(ctx(4))
    assert item().state == WatchItem.State.OPEN
    assert outcome.reopened == [KEY]
    assert stories()[-1] == "The evidence changed since it was marked wrong: open again"


# ---------------------------------------------------------------------------- fresh and stale sources
def test_a_check_runs_on_a_fresh_source_and_marks_items_with_its_last_success():
    synced = _succeeded(DATAMART, NOW - datetime.timedelta(hours=10))

    def found(ctx):
        return [candidate()]

    fresh = Detector(id="fresh_check", label="Fresh", run=found, source_jobs=(DATAMART,))
    outcome = memory.run(ctx(), [fresh])
    assert outcome.details()["detectors"]["fresh_check"]["skipped"] == ""
    assert item().source_mark == mark_of(synced.finished_at)


def test_a_stale_source_closes_nothing_and_raises_one_system_item(settings):
    settings.SYNC_STALENESS_HOURS = 30
    _succeeded(DATAMART, NOW - datetime.timedelta(hours=1))
    first, second = Fake("first_check", jobs=(DATAMART,)), Fake("second_check", jobs=(DATAMART,))
    first.found = {"due:a": {}}
    second.found = {"due:b": {}}
    first.pass_(ctx(0), second)

    # Three days later the Datamart has not synced again
    first.found, second.found = {}, {}
    first.closes = {"due:a": "report submitted"}
    first.mark = second.mark = mark(3)
    outcome = first.pass_(ctx(3), second)
    for key in ("due:a", "due:b"):
        assert item(key).state == WatchItem.State.OPEN and item(key).missed_runs == 0
    stale = WatchItem.objects.filter(detector=STALE_DETECTOR)
    assert [s.key for s in stale] == ["system:stale:etools_datamart"]
    system = stale.get()
    assert (system.kind, system.scope, system.severity) == (SYSTEM, ADMINS, WARNING)
    assert system.evidence["records"] and system.evidence["source_job"] == DATAMART
    assert "eTools Datamart sync" in system.title
    assert outcome.stale_sources == [DATAMART]
    assert outcome.details()["detectors"]["first_check"]["skipped"] == "stale: etools_datamart"
    assert DetectorSetting.objects.get(detector=STALE_DETECTOR).mode == DetectorSetting.Mode.ON

    # Once it has synced again, the system item closes and the checks run again
    _succeeded(DATAMART, NOW + datetime.timedelta(days=3, hours=-1))
    outcome = first.pass_(ctx(3), second)
    assert item("system:stale:etools_datamart").state == WatchItem.State.CLOSED
    assert item("system:stale:etools_datamart").close_reason.startswith("eTools Datamart sync ran again on")
    assert item("due:a").state == WatchItem.State.CLOSED
    assert outcome.stale_sources == []


def test_a_source_that_never_ran_is_stale():
    never = Fake("never_check", jobs=(SyncRun.Job.FORECAST,))
    never.found = {KEY: {}}
    outcome = never.pass_(ctx())
    assert not WatchItem.objects.filter(key=KEY).exists()
    assert item("system:stale:forecast").title == "Year-end indicator forecast: no successful run yet"
    assert outcome.stale_sources == [SyncRun.Job.FORECAST]


def test_a_weekly_source_can_be_fresh_for_longer(settings):
    settings.SYNC_STALENESS_HOURS = 30
    _succeeded(SyncRun.Job.FORECAST, NOW - datetime.timedelta(days=4))
    weekly = Fake("weekly_check", jobs=(SyncRun.Job.FORECAST,), max_age_hours=8 * 24)
    weekly.found = {KEY: {}}
    outcome = weekly.pass_(ctx())
    assert outcome.new == [KEY] and outcome.stale_sources == []


# ---------------------------------------------------------------------------- checks that fail or are off
def test_one_check_raising_leaves_the_others_running_and_the_error_is_recorded(check):
    broken = Fake("broken_check")
    broken.found = {"due:broken": {}}
    broken.pass_(ctx(0))
    broken.raises = RuntimeError("the table is missing")
    broken.mark = mark(1)
    broken.found = {}
    run = SyncRun.objects.create(job=SyncRun.Job.WATCH)

    outcome = memory.run(ctx(1), [broken.detector, check.detector], sync_run=run)
    assert outcome.new == [KEY]
    assert outcome.errors == {"broken_check": "RuntimeError: the table is missing"}
    assert item("due:broken").missed_runs == 0  # it could not look, so nothing is missed
    run.refresh_from_db()
    assert run.details["detectors"]["broken_check"]["error"] == "RuntimeError: the table is missing"
    assert run.details["detectors"]["broken_check"]["skipped"] == "error"
    assert run.details["detectors"]["fake_check"]["new"] == 1
    assert run.details["items"]["new"] == 1


def test_a_check_must_give_its_own_items():
    def other(ctx):
        return [candidate(detector="someone_else")]

    outcome = memory.run(ctx(), [Detector(id="mine", label="Mine", run=other)])
    assert "someone_else" in outcome.errors["mine"]
    assert not WatchItem.objects.exists()


def test_settings_are_created_on_first_sight_in_trial_or_on_and_kept():
    trial, review = Fake("new_check"), Fake("review_import", default_mode=DetectorSetting.Mode.ON)
    trial.pass_(ctx(), review)
    assert DetectorSetting.objects.get(detector="new_check").mode == DetectorSetting.Mode.TRIAL
    assert DetectorSetting.objects.get(detector="review_import").mode == DetectorSetting.Mode.ON
    DetectorSetting.objects.filter(detector="new_check").update(mode=DetectorSetting.Mode.ON)
    trial.pass_(ctx())
    assert DetectorSetting.objects.get(detector="new_check").mode == DetectorSetting.Mode.ON


def test_a_check_switched_off_does_not_run_and_changes_nothing(check):
    check.pass_(ctx(0))
    DetectorSetting.objects.filter(detector="fake_check").update(mode=DetectorSetting.Mode.OFF)
    check.found, check.mark = {}, mark(1)
    check.raises = AssertionError("an off check must not run")
    outcome = check.pass_(ctx(1))
    assert outcome.details()["detectors"]["fake_check"]["skipped"] == "off"
    assert outcome.errors == {}
    assert item().missed_runs == 0


def test_a_stopped_pass_runs_no_more_checks(check):
    outcome = memory.run(Context.make(DAILY, now=NOW, stop=lambda: True), [check.detector])
    assert outcome.details()["detectors"]["fake_check"]["skipped"] == "stopped"
    assert not WatchItem.objects.filter(key=KEY).exists()


def test_two_checks_finding_the_same_key_make_one_item(check):
    twin = Fake("twin_check")
    twin.found = {KEY: {"detector": "twin_check"}}
    outcome = check.pass_(ctx(), twin)
    assert WatchItem.objects.filter(key=KEY).count() == 1
    assert item().detector == "fake_check" and outcome.seen == 1


# ---------------------------------------------------------------------------- hand-overs and sections
def test_a_hand_over_to_the_daily_review_is_noted_once_and_kept(check):
    check.pass_(ctx(0))
    review = Fake("review_import")
    review.attach = [Attach(KEY, "reports_overdue:LEB/PCA2026001/PD2026001")]
    outcome = review.pass_(ctx(1), check)
    assert outcome.attached == [KEY]
    review.pass_(ctx(2), check)
    assert item().review_key == "reports_overdue:LEB/PCA2026001/PD2026001"
    assert stories().count("The daily review now flags it too") == 1


def test_sections_come_from_confirmed_etools_names_only():
    education = Section.objects.create(name="Education", code="EDU")
    wash = Section.objects.create(name="WASH", code="WSH")
    SectionMatch.objects.create(etools_name="Education", section=education, how="exact", confirmed=True)
    SectionMatch.objects.create(
        etools_name="WASH / Water, Sanitation and Hygiene", section=wash, how="contains", confirmed=False
    )
    fake = Fake()
    fake.found = {
        "due:a": {"etools_sections": ["Education", "WASH / Water, Sanitation and Hygiene"]},
        "due:b": {"etools_sections": ["Education"], "section_ids": [wash.pk]},  # given by the check
        "due:c": {"etools_sections": ["Unknown section"]},
    }
    fake.pass_(ctx())
    assert item("due:a").section_ids == [education.pk]
    assert item("due:a").etools_sections == ["Education", "WASH / Water, Sanitation and Hygiene"]
    assert item("due:b").section_ids == [wash.pk]
    assert item("due:c").section_ids == []


# ---------------------------------------------------------------------------- milestones and the registry
def test_the_milestone_reached_is_the_nearest_at_or_above_the_days_left():
    due = datetime.date(2026, 10, 20)
    steps = (14, 7, 3, 1, 0)
    assert milestone(due, steps, datetime.date(2026, 10, 1)) is None  # 19 days left
    assert milestone(due, steps, datetime.date(2026, 10, 6)) == 14
    assert milestone(due, steps, datetime.date(2026, 10, 15)) == 7
    assert milestone(due, steps, datetime.date(2026, 10, 20)) == 0
    assert milestone(due, steps, datetime.date(2026, 10, 21)) is None  # past due
    assert milestone(None, steps, TODAY) is None


def test_an_items_milestones_are_kept_with_it():
    fake = Fake(milestones=(7, 14, 3))
    fake.found = {"due:a": {}, "due:b": {"milestones": (30, 14, 7)}}
    fake.pass_(ctx())
    assert milestones_of(item("due:a")) == (14, 7, 3)
    assert milestones_of(item("due:b")) == (30, 14, 7)


def test_a_check_is_registered_once_by_its_id(monkeypatch):
    monkeypatch.setattr(detectors, "REGISTRY", {})
    monkeypatch.setattr(detectors, "_discovered", True)
    first = detectors.register(Detector(id="report_due_soon", label="Reports due soon", run=lambda ctx: ()))
    assert detectors.registered() == [first]
    again = detectors.register(Detector(id="report_due_soon", label="Reports due", run=lambda ctx: ()))
    assert detectors.registered() == [again] and detectors.get("report_due_soon") is again
    with pytest.raises(ValueError):
        Detector(id="x" * 41, label="Too long", run=lambda ctx: ())
    with pytest.raises(ValueError):
        Detector(id="unknown_job", label="Unknown", run=lambda ctx: (), source_jobs=("no_such_job",))
