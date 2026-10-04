"""NeuroDB Watch's runner: the morning pass step by step, the quick pass after new data, the events that
ask for it, and running again safely (cut off, failing, stopped, out of time, one run at a time)."""

import datetime
import json
import re
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.models import Max
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VIEWER
from neurodb.assistant import agent
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import models as dm
from neurodb.graph import refresh
from neurodb.graph.models import Change, RefreshRequest
from neurodb.integrations import background
from neurodb.integrations.runs import new_run
from neurodb.partnerships.models import PCA
from neurodb.review.models import FindingAssignment
from neurodb.watch import connect, explain, investigate, lock, memory, routing, services, signals
from neurodb.watch.models import WatchItem, WatchNote, WatchReceipt, WatchRequest, WatchState
from tests.graph.conftest import build_hub

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut, after the 07:45 morning pass
TODAY = datetime.date(2026, 10, 5)
BEFORE_SIX = datetime.datetime(2026, 10, 5, 2, 30, tzinfo=UTC)  # 05:30 in Beirut
SEVEN = datetime.datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 07:00 in Beirut: the morning pass is near
Job, Status = SyncRun.Job, SyncRun.Status
STEPS = [
    "sections",
    "inputs",
    "usefulness",
    "checks",
    "connect",
    "announce",
    "notes",
    "email",
    "look_ups",
    "watermark",
    "retention",
]
AI_ON = {
    "AI_ASSISTANT_ENABLED": True,
    "OPENAI_API_KEY": "test-key-not-real",
    "WATCH_AI": True,
    "WATCH_MODEL": "watch-test-model",
    "WATCH_INVESTIGATE_ENABLED": True,
    "WATCH_INVESTIGATE_PER_DAY": 3,
    "WATCH_DAILY_TOKEN_CAP": 300_000,
    "WATCH_MAX_MODEL_CALLS_PER_DAY": 24,
    "AI_DAILY_TOKEN_SOFT_CAP": 3_000_000,
}
WRITE = re.compile(r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"(\w+)"', re.I)


@pytest.fixture(autouse=True)
def slept(settings, monkeypatch):
    """The watch switched on, no settling time, and waiting only counted (the seconds asked for)."""
    settings.WATCH_ENABLED = True
    settings.WATCH_SETTLE_SECONDS = 0
    seconds = []
    monkeypatch.setattr(services, "_sleep", seconds.append)
    return seconds


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def ai_on(settings):
    for name, value in AI_ON.items():
        setattr(settings, name, value)
    return settings


LOOK_UP_CALLS, LOOK_UP_TOKENS = 2, (3_000, 500, 200)  # what one faked look-up uses: in, cached, out


@pytest.fixture
def model(monkeypatch):
    """The OpenAI client and Ask NeuroDB's loop, faked: every use is counted. A morning note gets an
    answer with no sentence (the plain note is used); a look-up makes 2 model calls, runs the real
    tools listed in ``tool_calls`` (read-only, through the look-up's filter: what the AI would read
    goes in ``tool_results``) and finds nothing. ``questions`` holds what each look-up was asked."""
    used = SimpleNamespace(clients=0, requests=[], look_ups=0, questions=[], tool_calls=[], tool_results=[])

    def create(**params):
        used.requests.append(params)
        return SimpleNamespace(
            output_text='{"sentences": []}',
            usage=SimpleNamespace(
                input_tokens=1_000, input_tokens_details=SimpleNamespace(cached_tokens=0), output_tokens=100
            ),
        )

    def client():
        used.clients += 1
        api = SimpleNamespace(responses=SimpleNamespace(create=create))
        api.with_options = lambda **options: api
        return api

    def answer(question, history, outcome, **kwargs):
        used.look_ups += 1
        used.questions.append(question)
        for name, args in used.tool_calls:
            used.tool_results.append(agent._lookup(name, args, kwargs["options"]))
        outcome.calls += LOOK_UP_CALLS
        outcome.input_tokens, outcome.cache_read_tokens, outcome.output_tokens = LOOK_UP_TOKENS
        outcome.answer = '{"what_i_found": "", "numbers": []}'
        return iter(())

    monkeypatch.setattr(agent, "client", client)
    monkeypatch.setattr(agent, "answer", answer)
    return used


def synced(job: str, at: datetime.datetime = NOW - datetime.timedelta(hours=1)) -> SyncRun:
    """A job that succeeded at ``at`` (default: an hour before the pass)."""
    return SyncRun.objects.create(
        job=job, status=Status.SUCCEEDED, started_at=at - datetime.timedelta(minutes=20), finished_at=at
    )


def person(username: str, role: str, section=None) -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.org")
    User.objects.filter(pk=user.pk).update(date_joined=datetime.datetime(2020, 1, 1, tzinfo=UTC))
    user.groups.add(Group.objects.get(name=role))
    if section is not None:
        user.section = section
        user.save(update_fields=["section"])
    return user


@pytest.fixture
def morning(hub, roles):
    """The hub tests' world on the morning of 5 Oct 2026: the night's sync, the daily review and the
    hub build done an hour ago (What's new has a change about the PD); a progress report of its PD due
    in two days; an Administrator and a viewer, both in Education."""
    PCA.objects.filter(pk=hub.pd.pk).update(title="Education and protection support in the north")
    build_hub()  # the night's build: What's new has a change about the PD
    for job in (Job.ETOOLS_DATAMART, Job.DAILY_REVIEW):
        synced(job)
    hour_ago = NOW - datetime.timedelta(hours=1)
    SyncRun.objects.filter(job=Job.KNOWLEDGE_HUB).update(started_at=hour_ago, finished_at=hour_ago)
    for n in range(2):
        dm.ReportedIndicator.objects.create(
            datamart_id=n + 1,
            partner_id=hub.pd.partner_id,
            intervention=hub.pd,
            pd_reference_number=hub.pd.number,
            progress_report="PR-7",
            report_number="QPR3",
            report_type="QPR",
            report_status="Due",
            period_start=datetime.date(2026, 7, 1),
            period_end=datetime.date(2026, 9, 30),
            due_date=TODAY + datetime.timedelta(days=2),
            indicator=f"# of children reached {n}",
            etools_indicator_id=str(100 + n),
            location=f"Place {n}",
        )
    WatchRequest.objects.all().delete()  # the fixture's hub build asked for a quick pass
    return SimpleNamespace(
        world=hub,
        admin=person("boss", ADMIN, hub.education),
        viewer=person("val", VIEWER, hub.education),
    )


def snapshot():
    """What the watch remembers: the items with their stories, who was told what, the notes."""
    items = sorted(
        (item.key, item.state, item.severity, tuple(line["text"] for line in item.story))
        for item in WatchItem.objects.all()
    )
    receipts = sorted(
        (r.user.username, r.item.key, r.told_step, r.level, r.milestone_count)
        for r in WatchReceipt.objects.select_related("user", "item")
    )
    notes = sorted((note.audience_key, note.text) for note in WatchNote.objects.all())
    return items, receipts, notes


def _without(found, key: str):
    """A snapshot without one item and what was told about it."""
    items, receipts, notes = found
    return [i for i in items if i[0] != key], [r for r in receipts if r[1] != key], notes


def morning_pass(**kwargs) -> SyncRun:
    done = services.daily(now=NOW, **kwargs)
    assert len(done.runs) == 1, done.note
    return done.runs[0]


def requested(reason: str = "Knowledge hub") -> WatchRequest:
    return WatchRequest.objects.create(reason=reason)


def ran_today(day: datetime.date = TODAY) -> None:
    """The morning pass of ``day`` is done (no catch-up due)."""
    state = WatchState.get()
    state.last_daily_on = day
    state.save()


# ---------------------------------------------------------------------------- the morning pass
def test_a_morning_pass_end_to_end(morning):
    run = morning_pass(triggered_by="schedule")
    assert (run.job, run.target, run.status, run.triggered_by) == (
        Job.WATCH,
        "daily",
        Status.SUCCEEDED,
        "schedule",
    )
    details = run.details
    assert list(details["steps"]) == STEPS and details["step_errors"] == {} and details["not_run"] == []
    assert details["mode"] == "daily" and details["date"] == "2026-10-05" and details["catch_up"] is False
    assert details["inputs_not_ready"] == {} and details["waited_seconds"] == 0
    assert details["sections"]["confirmed"] >= 1  # "Education" matched the NeuroDB section by its name
    assert details["detectors"]["report_due_soon"]["candidates"] == 1
    assert details["detectors"]["daily_review"]["new"] == 2
    assert details["receipts"]["needs_you"] >= 2 and run.rows_written == details["receipts"]["created"]
    assert details["notes"]["written"] == 2 and details["ai_skipped_reason"] == "off"  # AI off here
    assert details["look_ups"]["skipped"] == "off" and details["model_calls"] == 0 and details["tokens"] == 0
    assert details["emails"]["sent"] == 0
    top = Change.objects.aggregate(top=Max("pk"))["top"]
    assert top and details["changes_until"] == top and details["connect"]["changes_read"] >= 1
    state = WatchState.get()
    assert (state.last_change_id, state.last_daily_on) == (top, TODAY)
    assert run.rows_in == details["items"]["seen"] > 0 and run.rows_failed == 0

    report = WatchItem.objects.get(key=f"due:report:{morning.world.pd.number}:PR-7")
    told = WatchReceipt.objects.get(user=morning.admin, item=report)  # a trial check: the country view
    assert (told.told_step, told.level) == ("new", "needs_you")
    assert any(line["text"].startswith("What's new: ") for line in report.story)  # the night's PD change
    assert not WatchReceipt.objects.filter(user=morning.viewer, item=report).exists()
    assert set(WatchNote.objects.values_list("audience_key", flat=True)) == {
        "country",
        f"section:{morning.world.education.pk}",
    }


def _spy(order: list, label: str, real):
    def spy(*args, **kwargs):
        order.append(label)
        return real(*args, **kwargs)

    return spy


def test_the_morning_pass_writes_the_notes_before_it_looks_things_up(db, monkeypatch):
    """The look-ups come last: they use what the notes left of the day's AI budget and time."""
    order = []
    for module, name, label in (
        (memory, "run", "checks"),
        (routing, "announce", "announce"),
        (investigate, "run", "look_ups"),
        (explain, "write_all", "notes"),
    ):
        monkeypatch.setattr(module, name, _spy(order, label, getattr(module, name)))
    morning_pass()
    assert order == ["checks", "announce", "notes", "look_ups"]
    order.clear()
    requested()
    assert [run.target for run in services.when_requested(now=NOW).runs] == ["quick"]
    assert order == ["checks", "announce"]  # a quick pass: no AI, no note


def test_with_the_ai_on_the_morning_pass_looks_up_and_writes_notes_within_one_ledger(morning, ai_on, model):
    run = morning_pass()
    assert model.look_ups >= 1 and model.requests  # the notes, then a look-up on the critical item
    assert run.details["look_ups"]["tried"] == model.look_ups
    looked = run.details["look_ups"]
    assert (looked["calls"], looked["tokens"]) == (LOOK_UP_CALLS * model.look_ups, 3_700 * model.look_ups)
    # the run's account holds both: the notes' calls and tokens, and the look-ups'
    assert run.details["model_calls"] == len(model.requests) + LOOK_UP_CALLS * model.look_ups
    assert run.details["tokens"] == 1_100 * len(model.requests) + 3_700 * model.look_ups
    assert run.details["ai_use_today"]["watch_calls"] == run.details["model_calls"]  # the ledger agrees
    assert all(request["store"] is False and not request.get("tools") for request in model.requests)


def test_a_look_up_starts_only_while_there_is_time_for_it(morning, ai_on, model, settings):
    settings.WATCH_TIME_LIMIT_SECONDS = investigate.TIME_LIMIT + services.AFTER_ROOM_SECONDS - 30
    run = morning_pass()
    assert model.look_ups == 0 and run.details["look_ups"]["skipped"] == investigate.STOPPED
    assert run.details["notes"]["written"] == 2 and run.status == Status.SUCCEEDED


def _every_look_up_tool(world) -> list[tuple[str, dict]]:
    """Each tool a look-up may use, with arguments that find something in the hub tests' world."""
    from neurodb.graph.models import Entity

    partner = Entity.objects.get(kind="partner", name=world.amel.name)
    calls = [
        ("find_anything", {"text": "Amel"}),
        ("entity_profile", {"kind": "partner", "key": partner.key}),
        ("connected", {"kind": "partner", "key": partner.key, "to_kind": "programme_document"}),
        ("programme_details", {"number": world.pd.number}),
        ("partner_details", {"partner_id": world.amel.pk}),
        ("partner_reporting", {"partner": "Amel"}),
        ("pd_indicator_progress", {"partner": "Amel"}),
        ("funds_overview", {"partner": "Amel"}),
        ("assurance_overview", {"partner": "Amel"}),
        ("indicator_forecasts", {}),
        ("whats_new", {}),
        ("daily_review", {}),
        ("data_freshness", {}),
    ]
    assert {name for name, _args in calls} == set(investigate.TOOLS)
    return calls


def test_a_morning_pass_writes_only_its_own_tables(morning, ai_on, model):
    """The whole morning pass, with an assignment on a finding (read row by row by the checks) and a
    look-up running every tool it may use: only the watch's tables, the AI ledger and its run."""
    FindingAssignment.objects.create(
        key="ap_overdue:CARE", owner="Education team", note="Follow up", status="assigned"
    )
    model.tool_calls = _every_look_up_tool(morning.world)
    with CaptureQueriesContext(connection) as queries:
        run = morning_pass()
    assert run.status == Status.SUCCEEDED and model.requests  # the AI wrote (tried) the notes
    assert model.look_ups >= 1 and len(model.tool_results) == len(investigate.TOOLS) * model.look_ups
    written = {found[1] for query in queries.captured_queries if (found := WRITE.match(query["sql"]))}
    assert {"watch_watchitem", "watch_watchreceipt", "watch_watchnote", "assistant_aiusage"} <= written
    assert all(
        table.startswith("watch_") or table in {"assistant_aiusage", "core_syncrun"} for table in written
    ), written


PLANTED = (
    "Zebulon",
    "Quartermaine",
    "zebulon.q@example.org",
    "Philippa Thistlewood",
    "Octavia Fennimore",
    "octavia@example.org",
    "Call her before Friday",
    "Ignatius Brambleworth",
)


def test_no_planted_person_reaches_the_ai_in_a_whole_morning_pass(morning, ai_on, model):
    """Names and emails planted in every source of the morning pass: a user, a PD's focal points and
    its title (so What's new tells it), a finding's assignment (owner and note), a knowledge base
    document and an action point's assignee. None is in what the AI is sent: the notes' input and
    instructions, the look-ups' questions and what their tools return."""
    world = morning.world
    User.objects.create(
        username="zq", first_name="Zebulon", last_name="Quartermaine", email="zebulon.q@example.org"
    )
    PCA.objects.filter(pk=world.pd.pk).update(
        title="Education support with Zebulon Quartermaine", unicef_focal_points=["Philippa Thistlewood"]
    )
    world.note.title = "Field visit minutes with Zebulon Quartermaine"
    world.note.save(update_fields=["title"])
    FindingAssignment.objects.create(
        key="ap_overdue:CARE",
        owner="Octavia Fennimore",
        note="Call her before Friday: octavia@example.org",
        status="assigned",
    )
    dm.ActionPoint.objects.create(
        datamart_id=900, assigned_to_name="Ignatius Brambleworth", status="open", description="Visit"
    )
    build_hub()  # What's new tells the PD's new title
    WatchRequest.objects.all().delete()
    model.tool_calls = _every_look_up_tool(world)

    run = morning_pass()

    assert run.status == Status.SUCCEEDED and model.requests and model.questions and model.tool_results
    sent = [request["input"] + request["instructions"] for request in model.requests]
    sent += model.questions + [json.dumps(result, ensure_ascii=False) for result in model.tool_results]
    assert any("Education support with" in text for text in sent)  # the PD's change did reach the AI
    for text in sent:
        for planted in PLANTED:
            assert planted.casefold() not in text.casefold(), planted


# ---------------------------------------------------------------------------- waiting for its inputs
@pytest.fixture(
    params=[(Job.DAILY_REVIEW, background.DAILY_REVIEW_LOCK_ID), (Job.KNOWLEDGE_HUB, refresh.LOCK_ID)]
)
def input_running(request, db):
    """Today's daily review, or a knowledge hub build, running in another process (holding its lock)."""
    job, lock_id = request.param
    other = connections.create_connection("default")
    with other.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
    run = SyncRun.objects.create(job=job, status=Status.RUNNING)
    yield SimpleNamespace(job=job, run=run, other=other)
    other.close()


def test_the_morning_pass_waits_at_most_20_minutes_for_the_review_or_the_hub(input_running, slept):
    run = morning_pass()
    assert slept == [60] * 20  # it looked every minute
    assert run.details["waited_seconds"] == 1200 and run.status == Status.SUCCEEDED  # then went ahead
    assert run.details["inputs_not_ready"][input_running.job] == services.RUNNING


def test_it_goes_ahead_as_soon_as_its_input_is_ready(input_running, monkeypatch):
    seconds = []

    def sleep(n):
        seconds.append(n)
        if len(seconds) == 2:  # the review (or hub build) finishes during the second minute
            input_running.run.finish(Status.SUCCEEDED)
            input_running.other.close()

    monkeypatch.setattr(services, "_sleep", sleep)
    run = services.daily().runs[0]  # on the clock: the input finished today
    assert seconds == [60, 60] and run.details["waited_seconds"] == 120
    assert input_running.job not in run.details["inputs_not_ready"]


def test_what_did_not_run_today_is_recorded_as_not_ready(db):
    synced(Job.DAILY_REVIEW, NOW - datetime.timedelta(days=1))
    synced(Job.KNOWLEDGE_HUB, NOW - datetime.timedelta(minutes=30))
    run = morning_pass()
    assert run.details["inputs_not_ready"] == {Job.DAILY_REVIEW: services.NOT_TODAY}


# ---------------------------------------------------------------------------- running again safely
class Crash(BaseException):
    """The process dies (a container restart), whatever it was doing."""


def test_cut_off_after_the_checks_then_run_again_nothing_twice(morning, monkeypatch):
    with transaction.atomic():  # what one clean pass leaves, then undone
        morning_pass()
        clean = snapshot()
        transaction.set_rollback(True)
    assert not WatchItem.objects.exists() and clean[0] and clean[1] and clean[2]

    real_link = connect.link

    def dies(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(connect, "link", dies)
    with pytest.raises(Crash):
        services.daily(now=NOW)
    cut = SyncRun.objects.get(job=Job.WATCH)
    assert cut.status == Status.RUNNING and "checks" in cut.details["steps"]  # it got past the checks
    assert background.lock_is_held(lock.LOCK_ID) is False  # the lock went with the "process"

    monkeypatch.setattr(connect, "link", real_link)
    again = morning_pass()
    cut.refresh_from_db()
    assert (cut.status, cut.error) == (Status.FAILED, background.CUT_OFF)  # closed by the next run
    # the cut-off run is the watch's last finished run: the administrators hear it failed
    failed = WatchItem.objects.get(key="system:job_failed:watch")
    assert failed.state == WatchItem.State.OPEN
    assert again.status == Status.SUCCEEDED and _without(snapshot(), failed.key) == clean
    assert (
        morning_pass().status == Status.SUCCEEDED and _without(snapshot(), failed.key) == clean
    )  # once more
    failed.refresh_from_db()
    assert failed.state == WatchItem.State.CLOSED  # a run of the watch succeeded since


def test_a_step_that_fails_is_recorded_the_others_still_run_and_a_rerun_mends_it(morning, monkeypatch):
    with transaction.atomic():
        morning_pass()
        clean = snapshot()
        transaction.set_rollback(True)

    def broken(*args, **kwargs):
        raise RuntimeError("routing broke")

    real = routing.announce
    monkeypatch.setattr(routing, "announce", broken)
    run = morning_pass()
    assert run.status == Status.PARTIAL and run.rows_failed == 1
    assert run.details["step_errors"] == {"announce": "RuntimeError: routing broke"}
    assert "announce: RuntimeError: routing broke" in run.error
    assert list(run.details["steps"]) == STEPS  # the next steps still ran
    assert not WatchReceipt.objects.exists() and WatchNote.objects.exists()

    monkeypatch.setattr(routing, "announce", real)
    assert morning_pass().status == Status.SUCCEEDED
    # the run with errors is the watch's last finished run: the administrators hear of it
    items, receipts, notes = _without(snapshot(), "system:job_partial:watch")
    assert (items, receipts) == clean[:2] and [n[0] for n in notes] == [n[0] for n in clean[2]]


def test_an_administrator_stopping_the_run_ends_it_between_steps(morning, monkeypatch):
    real = memory.run

    def checks_then_stopped(ctx, *args, sync_run=None, **kwargs):
        outcome = real(ctx, *args, sync_run=sync_run, **kwargs)
        sync_run.stop("boss")  # "Stop" in the admin while the checks ran
        return outcome

    monkeypatch.setattr(memory, "run", checks_then_stopped)
    run = morning_pass()
    run.refresh_from_db()
    assert run.status == Status.FAILED and run.error.startswith("Stopped by boss")
    assert run.details["stopped"] == services.BY_ADMIN
    assert run.details["not_run"] == STEPS[4:]
    assert WatchItem.objects.exists() and not WatchReceipt.objects.exists()
    assert WatchState.get().last_daily_on is None
    SyncRun.objects.filter(pk=run.pk).update(started_at=NOW - datetime.timedelta(minutes=5))
    requested()
    monkeypatch.setattr(memory, "run", real)
    assert not services.catch_up_due(NOW)  # stopped on purpose today: not run again by itself
    assert [again.target for again in services.when_requested(now=NOW).runs] == ["quick"]


def test_out_of_time_it_stops_between_steps_and_says_so(db, settings):
    settings.WATCH_TIME_LIMIT_SECONDS = 0
    run = morning_pass()
    assert run.status == Status.PARTIAL and run.details["stopped"] == services.TIME_UP
    assert list(run.details["steps"]) == ["sections", "inputs"] and run.details["not_run"] == STEPS[2:]
    assert "Stopped at the time limit (0 s) before: usefulness, checks" in run.error
    assert WatchState.get().last_daily_on is None


@pytest.fixture
def held_elsewhere(db):
    """Another process holds the watch's lock (a run in progress)."""
    other = connections.create_connection("default")
    with other.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [lock.LOCK_ID])
    yield
    other.close()


def test_a_second_start_while_one_runs_does_nothing(held_elsewhere):
    requested()
    assert services.daily(now=NOW).note == services.BUSY
    assert services.when_requested(now=NOW).note == services.BUSY
    assert not SyncRun.objects.filter(job=Job.WATCH).exists() and WatchRequest.objects.count() == 1
    assert call_command("run_watch", "--when-requested") is None  # exits without an error


def test_the_morning_pass_deletes_old_receipts_old_items_and_the_requests_it_answered(db, viewer):
    def item(key, state, closed_on=None):
        return WatchItem.objects.create(
            key=key,
            detector="fr_expiring",
            kind=WatchItem.Kind.DEADLINE,
            severity=WatchItem.Severity.INFO,
            title=key,
            state=state,
            first_seen_on=datetime.date(2024, 1, 1),
            last_seen_on=TODAY,
            changed_on=TODAY,
            closed_on=closed_on,
            evidence={"source": "eTools", "records": [{"label": key, "date": "", "value": "", "url": ""}]},
        )

    item("due:fr:old", WatchItem.State.GONE, TODAY - datetime.timedelta(days=731))
    recent = item("due:fr:recent", WatchItem.State.CLOSED, TODAY - datetime.timedelta(days=100))
    still_open = item("due:fr:open", WatchItem.State.OPEN)
    for target, told in ((still_open, 400), (recent, 10)):
        WatchReceipt.objects.create(
            user=viewer,
            item=target,
            first_told_on=TODAY - datetime.timedelta(days=told),
            last_told_on=TODAY - datetime.timedelta(days=told),
            told_step="new",
        )
    requested()
    requested("eTools Datamart sync failed")
    run = morning_pass()
    assert run.details["retention"] == {"receipts": 1, "items": 1, "requests": 2}
    assert set(WatchItem.objects.filter(key__startswith="due:fr:").values_list("key", flat=True)) == {
        "due:fr:recent",
        "due:fr:open",
    }
    assert list(WatchReceipt.objects.values_list("item__key", flat=True)) == ["due:fr:recent"]
    assert not WatchRequest.objects.exists()


# ---------------------------------------------------------------------------- the quick pass
def test_a_quick_pass_makes_no_model_call_and_sends_no_email(morning, ai_on, model, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    ran_today()
    requested()
    done = services.when_requested(now=NOW)
    assert [run.target for run in done.runs] == ["quick"] and done.runs[0].status == Status.SUCCEEDED
    assert (model.clients, model.requests, model.look_ups) == (0, [], 0)
    assert mail.outbox == [] and not WatchNote.objects.exists()
    details = done.runs[0].details
    assert details["mode"] == "quick" and "notes" not in details and "look_ups" not in details
    assert details["requests"] == 1 and not WatchRequest.objects.exists()
    assert details["receipts"]["good_to_know"] == 0 and details["receipts"]["needs_you"] >= 1
    assert done.runs[0].triggered_by == "new data: Knowledge hub"
    assert WatchState.get().last_change_id == Change.objects.aggregate(top=Max("pk"))["top"]


def test_the_seventh_quick_pass_of_a_day_waits_for_the_morning(db):
    WatchState(last_daily_on=TODAY, quick_passes_on=TODAY, quick_passes_count=5).save()
    requested()
    assert [run.target for run in services.when_requested(now=NOW).runs] == ["quick"]
    assert services.quick_passes_used(NOW) == 6
    requested()
    done = services.when_requested(now=NOW)
    assert done.runs == [] and done.note == services.CAPPED
    assert WatchRequest.objects.count() == 1  # it waits for the morning pass
    next_day = BEFORE_SIX + datetime.timedelta(days=1)  # 05:30 the next day: a new day's count
    assert [run.target for run in services.when_requested(now=next_day).runs] == ["quick"]
    assert services.quick_passes_used(next_day) == 1


def test_a_quick_pass_at_nine_with_no_morning_pass_today_runs_the_morning_pass(db):
    ran_today(TODAY - datetime.timedelta(days=1))
    requested()
    done = services.when_requested(now=NOW)
    assert [(run.target, run.details["catch_up"]) for run in done.runs] == [("daily", True)]
    assert done.runs[0].triggered_by == "new data (catch-up)" and done.runs[0].status == Status.SUCCEEDED
    assert WatchState.get().last_daily_on == TODAY and services.quick_passes_used(NOW) == 1
    assert not WatchRequest.objects.exists()  # the morning pass answered the request
    requested("eTools Datamart sync failed")
    assert [run.target for run in services.when_requested(now=NOW).runs] == ["quick"]  # done today


def test_before_the_morning_pass_it_is_not_caught_up_and_near_it_the_data_waits(db):
    ran_today(TODAY - datetime.timedelta(days=1))
    requested()
    done = services.when_requested(now=SEVEN)  # 07:00: the morning pass starts within 45 minutes
    assert done.runs == [] and done.note == services.DEFERRED and WatchRequest.objects.count() == 1
    assert [run.target for run in services.when_requested(now=BEFORE_SIX).runs] == ["quick"]
    ScheduledJob.objects.filter(command="watch").update(enabled=False)  # no morning pass at all
    requested()
    assert [run.target for run in services.when_requested(now=NOW).runs] == ["quick"]


def test_requests_arriving_during_a_pass_get_trailing_passes_three_at_most(db, monkeypatch):
    ran_today()
    real = memory.run

    def more_data(ctx, *args, **kwargs):
        requested()  # a hub build finished meanwhile
        return real(ctx, *args, **kwargs)

    monkeypatch.setattr(memory, "run", more_data)
    requested()
    done = services.when_requested(now=NOW)
    assert [run.target for run in done.runs] == ["quick"] * services.MAX_PASSES
    assert WatchRequest.objects.count() == 1  # the last one waits for the next process
    assert services.quick_passes_used(NOW) == 3


def test_nothing_waiting_nothing_runs_and_the_settling_time_is_waited_first(db, settings, slept):
    settings.WATCH_SETTLE_SECONDS = 600
    done = services.when_requested(now=NOW)
    assert slept == [600] and done.runs == [] and done.note == services.NOTHING_WAITING


def test_switched_off_a_pass_records_only_that(db, settings):
    settings.WATCH_ENABLED = False
    requested()
    done = services.when_requested(now=NOW)
    assert done.note == services.SWITCHED_OFF and done.runs[0].details["note"] == services.SWITCHED_OFF
    assert not WatchItem.objects.exists()


def test_new_data_landing_while_the_morning_pass_runs_gets_a_quick_pass_right_after(db, monkeypatch):
    """The hub build finished while the morning pass ran: its request is answered by a trailing quick
    pass under the same lock (the quick process it started would find the lock taken and give up)."""
    requested()  # waiting when the morning pass starts: answered by it
    real = explain.write_all

    def notes_while_data_lands(*args, **kwargs):
        requested("Knowledge hub")  # the late hub build finished meanwhile
        return real(*args, **kwargs)

    monkeypatch.setattr(explain, "write_all", notes_while_data_lands)
    done = services.daily(now=NOW)
    assert [run.target for run in done.runs] == ["daily", "quick"] and done.note == ""
    assert done.runs[1].triggered_by == "new data: Knowledge hub" and done.runs[1].details["requests"] == 1
    assert not WatchRequest.objects.exists() and services.quick_passes_used(NOW) == 1


def test_no_trailing_pass_after_a_morning_pass_an_administrator_stopped(db, monkeypatch):
    real = memory.run

    def checks_then_stopped(ctx, *args, sync_run=None, **kwargs):
        requested()  # new data meanwhile
        outcome = real(ctx, *args, sync_run=sync_run, **kwargs)
        sync_run.stop("boss")
        return outcome

    monkeypatch.setattr(memory, "run", checks_then_stopped)
    done = services.daily(now=NOW)
    assert [run.target for run in done.runs] == ["daily"] and WatchRequest.objects.count() == 1


def test_a_morning_pass_whose_announcements_failed_is_caught_up_the_same_day(db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("routing broke")

    real = routing.announce
    monkeypatch.setattr(routing, "announce", broken)
    run = morning_pass()
    assert run.status == Status.PARTIAL  # a success with errors, for the admin home
    assert WatchState.get().last_daily_on is None and services.catch_up_due(NOW)
    monkeypatch.setattr(routing, "announce", real)
    requested()
    done = services.when_requested(now=NOW)
    assert [(again.target, again.details["catch_up"]) for again in done.runs] == [("daily", True)]
    assert WatchState.get().last_daily_on == TODAY and not services.catch_up_due(NOW)


@pytest.fixture
def held_then_freed(db, monkeypatch, slept):
    """Another run holds the watch's lock, and finishes after the first minute of waiting."""
    other = connections.create_connection("default")
    with other.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [lock.LOCK_ID])

    def wait(seconds):
        slept.append(seconds)
        other.close()  # the other run ends: its lock goes with its session

    monkeypatch.setattr(services, "_sleep", wait)
    yield slept
    other.close()


def test_the_morning_pass_waits_for_the_run_holding_the_lock(held_then_freed):
    done = services.daily(now=NOW)
    assert held_then_freed == [services.LOCK_POLL_SECONDS]
    assert [run.target for run in done.runs] == ["daily"] and done.runs[0].status == Status.SUCCEEDED


def test_after_waiting_it_does_not_run_again_when_a_morning_pass_finished_meanwhile(held_then_freed):
    ran_today()  # e.g. the quick process holding the lock caught the morning pass up
    done = services.daily(now=NOW)
    assert done.runs == [] and done.note == services.ALREADY_RAN


# ---------------------------------------------------------------------------- what asks for a quick pass
def test_three_hub_builds_within_a_minute_start_one_quick_pass(
    db, started, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(3):
            new_run(Job.KNOWLEDGE_HUB, "", "test").finish(Status.SUCCEEDED)
    assert list(WatchRequest.objects.values_list("reason", flat=True)) == ["Knowledge hub"] * 3
    assert started == [("run_watch", "--when-requested", "--triggered-by", "new data")]
    WatchRequest.objects.update(requested_at=timezone.now() - datetime.timedelta(minutes=20))
    with django_capture_on_commit_callbacks(execute=True):  # waiting that long, its process was lost
        new_run(Job.KNOWLEDGE_HUB, "", "test").finish(Status.PARTIAL)
    assert len(started) == 2


def test_a_finished_watch_run_asks_for_no_pass_and_no_rebuild(
    db, started, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        for status in (Status.SUCCEEDED, Status.PARTIAL, Status.FAILED):
            new_run(Job.WATCH, "daily", "test").finish(status)
    assert not WatchRequest.objects.exists() and not RefreshRequest.objects.exists() and started == []


def test_a_failed_job_asks_for_a_quick_pass_and_a_good_sync_waits_for_its_hub_build(
    db, started, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        new_run(Job.ACTIVITYINFO_DATA, "", "test").finish(Status.FAILED)
    assert list(WatchRequest.objects.values_list("reason", flat=True)) == ["ActivityInfo data import failed"]
    assert started == [("run_watch", "--when-requested", "--triggered-by", "new data")]
    WatchRequest.objects.all().delete()
    with django_capture_on_commit_callbacks(execute=True):
        new_run(Job.ACTIVITYINFO_DATA, "", "test").finish(Status.SUCCEEDED)
        SyncRun.objects.create(job=Job.ETOOLS, status=Status.FAILED)  # not the moment a run finished
    assert not WatchRequest.objects.exists()  # the hub build it causes asks instead
    assert started[1:] == [("build_knowledge_hub", "--when-requested")]


def test_editing_a_findings_assignment_asks_for_a_quick_pass(db, started, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        FindingAssignment.objects.create(key="reports_overdue:LEB/PCA2026001/PD2026001", owner="Education")
    assert list(WatchRequest.objects.values_list("reason", flat=True)) == [signals.ASSIGNMENT_EDITED]
    assert started == [("run_watch", "--when-requested", "--triggered-by", "new data")]


def test_nothing_is_asked_when_switched_off_and_no_process_when_the_day_is_used_up(
    db, settings, started, django_capture_on_commit_callbacks
):
    settings.WATCH_ENABLED = False
    with django_capture_on_commit_callbacks(execute=True):
        new_run(Job.ETOOLS, "", "test").finish(Status.FAILED)
    assert not WatchRequest.objects.exists() and started == []
    settings.WATCH_ENABLED = True
    WatchState(quick_passes_on=timezone.localdate(), quick_passes_count=6).save()
    with django_capture_on_commit_callbacks(execute=True):
        new_run(Job.ETOOLS, "", "test").finish(Status.FAILED)
    assert WatchRequest.objects.count() == 1 and started == []  # it waits for the morning pass
