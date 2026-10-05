"""The budget of Monitoring insights' AI (fmm.ai.budget), stage 6a part: whether a call may start
(switched on, not paused, within the office caps and the shared soft cap) and the per-person quotas.
The parts of the brief and the chat that use them come with those steps."""

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
