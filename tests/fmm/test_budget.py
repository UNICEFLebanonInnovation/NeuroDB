"""The budget of Monitoring insights' AI (fmm.ai.budget): whether a call may start (switched on, not
paused, within the office caps and the shared soft cap) and the per-person quotas (stage 6a); how the
brief's Regenerate meets them (stage 6b); the chat's quota, its concurrency limits and the day's budget
(stage 7)."""

import datetime
from types import SimpleNamespace

import httpx2
import openai
import pytest
from django.test import override_settings
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.fmm.ai import budget, profiles
from neurodb.fmm.models import AIState, Insight, PromptVersion

pytestmark = pytest.mark.django_db

AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}


@pytest.fixture
def ai_on():
    with override_settings(**AI_ON):
        yield profiles.published()


def _use(feature: str, tokens: int, calls: int = 1):
    usage.record(feature, "gpt-5.5", SimpleNamespace(input_tokens=tokens, output_tokens=0), calls=calls)


def _brief(
    user, *, trigger=Insight.Trigger.USER, called=True, status=Insight.Status.OK, scope="s", when=None
):
    row = Insight.objects.create(
        scope_hash=scope,
        scope={},
        scope_label="This year",
        trigger=trigger,
        version=profiles.published(),
        rules_version=1,
        input_hash="i",
        status=status,
        called=called,
        created_by=user,
    )
    if when is not None:
        Insight.objects.filter(pk=row.pk).update(created_at=when)
    return row


def _error(body):
    response = httpx2.Response(429, request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    return openai.RateLimitError("Error code: 429", response=response, body=body)


# ------------------------------------------------------------------------------------------ the switch
def test_off_unless_everything_is_switched_on():
    assert budget.allowed("user", None, 1000) == (False, "off")  # FMM_AI is off at deploy
    with override_settings(**AI_ON):
        assert budget.switched_on() and budget.allowed("user", None, 1000) == (True, "")
    for off in ({"FMM_AI": False}, {"FMM_ENABLED": False}, {"AI_ASSISTANT_ENABLED": False}):
        with override_settings(**{**AI_ON, **off}):
            assert budget.allowed("nightly", None, 0) == (False, "off"), off
    PromptVersion.objects.filter(status="published").update(status="retired")
    with override_settings(**AI_ON):
        assert not budget.switched_on() and budget.allowed("user", None, 0) == (False, "off")
    with pytest.raises(ValueError):
        budget.allowed("weekly", None, 0)


def test_the_credit_running_out_pauses_the_ai_for_six_hours(ai_on):
    assert budget.trip(_error({"message": "Slow down", "code": "rate_limit_exceeded"})) is False
    assert budget.allowed("user", None, 0) == (True, "")
    now = timezone.now()
    assert budget.trip(
        _error({"message": "You exceeded your current quota.", "code": "insufficient_quota"}), now
    )
    state = AIState.objects.get()
    assert state.paused_until == now + datetime.timedelta(hours=6) and "credit" in state.reason
    assert budget.allowed("chat", None, 0) == (False, "paused")
    later = now + datetime.timedelta(hours=6, minutes=1)
    assert budget.allowed("chat", None, 0, now=later) == (True, "")


# ------------------------------------------------------------------------------------------ the caps
@override_settings(FMM_DAILY_TOKEN_CAP=10_000, FMM_MAX_CALLS_PER_DAY=5)
def test_the_office_caps_of_monitoring_insights(ai_on):
    _use(usage.FMM, 8_000, calls=2)
    assert budget.allowed("user", None, 2_000) == (True, "")
    assert budget.allowed("user", None, 2_001) == (False, "budget")
    assert budget.office_share() == 80.0
    _use(usage.FMM, 0, calls=3)  # 5 calls made: the call cap is reached
    assert budget.allowed("chat", None, 0) == (False, "budget")
    _use(usage.ASK, 50_000)  # other features do not count against Monitoring insights' own cap
    assert usage.today_total(usage.FMM) == 8_000


@override_settings(AI_DAILY_TOKEN_SOFT_CAP=100_000, FMM_DAILY_TOKEN_CAP=1_000_000)
def test_background_runs_stop_at_80_percent_of_the_shared_cap_people_at_100(ai_on):
    _use(usage.ASK, 85_000)  # 85% of the key's day, used by Ask NeuroDB
    assert budget.allowed("nightly", None, 0) == (False, "budget")
    assert budget.allowed("test", None, 0) == (False, "budget")
    assert budget.allowed("user", None, 0) == (True, "")
    assert budget.allowed("chat", None, 10_000) == (True, "")
    assert budget.allowed("user", None, 15_001) == (False, "budget")
    _use(usage.WATCH, 15_000)  # 100%
    assert budget.allowed("chat", None, 1) == (False, "budget")


# ------------------------------------------------------------------------------------------ quotas
def test_the_brief_quota_per_person_per_day(ai_on, admin_user, viewer):
    assert budget.quota("insights", viewer) == (0, 5)
    for i in range(5):
        _brief(viewer, scope=f"s{i}")
    assert budget.quota("insights", viewer) == (5, 5)  # a 6th is refused by the brief's gate
    assert budget.quota("insights", admin_user) == (0, 5)  # another person is fine


def test_what_counts_against_the_brief_quota(ai_on, viewer):
    _brief(viewer, called=False, status=Insight.Status.OK, scope="hit")  # up to date: no call
    _brief(viewer, called=False, status=Insight.Status.LIMITED, scope="refused")
    _brief(viewer, trigger=Insight.Trigger.TEST, scope="test")  # test runs: the office cap only
    _brief(None, trigger=Insight.Trigger.NIGHTLY, scope="night")  # nightly briefs are nobody's
    assert budget.quota("insights", viewer) == (0, 5)
    _brief(viewer, called=False, status=Insight.Status.RUNNING, scope="running")  # being written counts
    assert budget.quota("insights", viewer) == (1, 5)


def test_the_quota_starts_again_at_local_midnight(ai_on, viewer):
    today = timezone.localdate()
    midnight = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))
    _brief(viewer, scope="a", when=midnight - datetime.timedelta(minutes=1))  # 23:59 yesterday, Beirut
    _brief(viewer, scope="b", when=midnight + datetime.timedelta(minutes=1))
    assert budget.quota("insights", viewer) == (1, 5)


def test_the_quotas_come_from_the_published_version(ai_on, admin_user, viewer):
    draft = profiles.draft_from(
        profiles.published(), admin_user, "More", insights_per_user_per_day=8, chat_per_user_per_day=30
    )
    assert budget.quota("insights", viewer) == (0, 5) and budget.quota("chat", viewer) == (0, 20)
    profiles.publish(draft, admin_user)
    assert budget.quota("insights", viewer) == (0, 8) and budget.quota("chat", viewer) == (0, 30)
    assert budget.quota("insights", None) == (0, 8)
    with pytest.raises(ValueError):
        budget.quota("emails", viewer)


def test_the_chat_quota_is_separate_from_asks_hourly_limit(ai_on, viewer):
    from neurodb.assistant.models import AssistantQuestion

    AssistantQuestion.objects.create(user=viewer, question="How many?", status="answered")
    assert budget.quota("chat", viewer) == (0, 20)  # Ask's questions never count here


def test_the_estimate_and_the_status(ai_on, viewer):
    version = profiles.published()
    facts = SimpleNamespace(payload={"kpi": {"key": "kpi", "visits": 33}})
    tokens = budget.estimate(facts, version)
    assert version.max_output_tokens < tokens < version.max_output_tokens + 3_000
    status = budget.status(viewer)
    assert status["on"] is True and status["insights"] == (0, 5) and status["office_share"] == 0.0


# ------------------------------------------------------------------------------------------ the brief (6b)
YEAR = "year=2026&section="


def _regenerate(client):
    from django.urls import reverse

    return client.post(f"{reverse('fmm:insights')}?{YEAR}").content.decode()


@pytest.fixture
def started(monkeypatch):
    from neurodb.integrations import background

    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


def test_the_sixth_regenerate_of_the_day_is_refused_and_another_person_is_not(
    built, ai_on, client, viewer, admin_user, started, django_capture_on_commit_callbacks
):
    for i in range(5):
        _brief(viewer, scope=f"s{i}")
    client.force_login(viewer)
    html = _regenerate(client)
    assert "5 of 5 today — resets at midnight" in html and started == []
    limited = Insight.objects.get(status=Insight.Status.LIMITED)
    assert (limited.created_by, limited.called) == (viewer, False)
    assert budget.quota("insights", viewer) == (5, 5)  # the refusal does not count
    client.force_login(admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        html = _regenerate(client)
    assert "every 3s" in html and len(started) == 1
    assert budget.quota("insights", admin_user) == (1, 5)  # the brief being written counts


def test_two_regenerates_at_once_write_one_brief_with_one_call(built, ai_on, viewer, admin_user, monkeypatch):
    import json

    from neurodb.assistant import agent
    from neurodb.fmm.ai import facts, insights
    from tests.fmm.test_insights import _answer, _good, _scope

    first = insights.start(_scope(), ai_on, viewer)
    second = insights.start(_scope(), ai_on, admin_user)  # its insert meets fmm_one_running_insight
    assert first == second and Insight.objects.filter(status=Insight.Status.RUNNING).count() == 1
    calls = []
    found = facts.build(_scope(), ai_on)

    def create(**params):
        calls.append(params)
        return _answer(_good(found))

    api = SimpleNamespace(responses=SimpleNamespace(create=create))
    api.with_options = lambda **_: api
    monkeypatch.setattr(agent, "client", lambda: api)
    row = insights.generate(_scope(), user=viewer, insight=first)
    assert row.status == Insight.Status.OK and len(calls) == 1
    assert json.loads(calls[0]["input"][0]["content"]) == found.payload


def test_the_credit_running_out_fails_the_brief_and_pauses_the_next(built, ai_on, viewer, monkeypatch):
    from neurodb.assistant import agent
    from neurodb.fmm.ai import insights
    from tests.fmm.test_insights import _scope

    def create(**params):
        raise _error({"message": "You exceeded your current quota.", "code": "insufficient_quota"})

    api = SimpleNamespace(responses=SimpleNamespace(create=create))
    api.with_options = lambda **_: api
    monkeypatch.setattr(agent, "client", lambda: api)
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY)
    assert (row.status, row.reason, row.called) == ("failed", "quota", False)
    assert AIState.objects.get().paused_until > timezone.now()
    row = insights.generate(_scope(), user=viewer, trigger=Insight.Trigger.USER)
    assert (row.status, row.reason) == (Insight.Status.LIMITED, budget.REASONS[budget.PAUSED])


@override_settings(FMM_DAILY_TOKEN_CAP=1_000_000)
def test_the_quota_pill_shows_the_office_budget(built, ai_on, client, viewer):
    from django.urls import reverse

    _use(usage.FMM, 620_000)
    client.force_login(viewer)
    html = client.get(f"{reverse('fmm:insights')}?{YEAR}").content.decode()
    assert "0 of 5 today · office AI budget 62% used" in html
    _use(usage.FMM, 380_001)  # the office cap is reached: Regenerate says why
    html = client.get(f"{reverse('fmm:insights')}?{YEAR}").content.decode()
    assert "Today&#x27;s AI budget for Monitoring insights is used; it resets at midnight." in html


# ------------------------------------------------------------------------------------------ the chat
def _question(user, version, status="answered", when=None):
    import uuid

    from neurodb.fmm.models import ChatQuestion

    row = ChatQuestion.objects.create(
        user=user, conversation=uuid.uuid4(), scope_hash="s", version=version, question="q", status=status
    )
    if when is not None:
        ChatQuestion.objects.filter(pk=row.pk).update(created_at=when)
    return row


def _chat(client):
    from django.urls import reverse

    return client.post(reverse("fmm:chat_stream"), {"question": "How many visits?", "scope": "section="})


def test_the_chat_quota_counts_todays_questions_and_not_asks(ai_on, viewer, admin_user):
    import datetime as dt

    from neurodb.assistant import views as ask_views
    from neurodb.assistant.models import AssistantQuestion

    assert budget.quota("chat", viewer) == (0, 20)
    _question(viewer, ai_on)
    _question(viewer, ai_on, status="limited")  # a refused question does not count
    _question(admin_user, ai_on)  # nor another person's
    midnight = timezone.make_aware(dt.datetime.combine(timezone.localdate(), dt.time.min))
    _question(viewer, ai_on, when=midnight - dt.timedelta(seconds=1))  # nor yesterday's
    assert budget.quota("chat", viewer) == (1, 20)
    # Ask's hourly questions do not count against the chat, nor the chat's against Ask
    for _ in range(30):
        AssistantQuestion.objects.create(user=viewer, question="q", status="answered")
    assert budget.quota("chat", viewer) == (1, 20)
    AssistantQuestion.objects.all().delete()
    for _ in range(25):
        _question(viewer, ai_on)
    assert not ask_views._over_limit(viewer)


def test_the_fifth_chat_answer_at_once_on_the_site_waits(ai_on, client_viewer, admin_user):
    from neurodb.fmm import views
    from neurodb.fmm.models import ChatQuestion

    with override_settings(FMM_CHAT_MAX_RUNNING=4):
        for _ in range(4):
            _question(admin_user, ai_on, status="in_progress")
        response = _chat(client_viewer)
        assert response.status_code == 503 and response.json()["error"] == str(views.CHAT_BUSY)
    assert not ChatQuestion.objects.exclude(user=admin_user).exists()


def test_the_chat_stops_when_the_days_budget_is_used(ai_on, client_viewer):
    from neurodb.fmm.models import ChatQuestion

    _use(usage.FMM, 1_200_000)
    response = _chat(client_viewer)
    assert response.status_code == 429 and response.json()["error"] == budget.REASONS[budget.BUDGET]
    assert ChatQuestion.objects.get().status == "limited"
    assert budget.quota("chat", ChatQuestion.objects.get().user)[0] == 0  # a refused question does not count
    budget.pause()
    response = _chat(client_viewer)
    assert response.status_code == 503 and response.json()["error"] == budget.REASONS[budget.PAUSED]
