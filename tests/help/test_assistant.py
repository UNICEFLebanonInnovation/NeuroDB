"""The Help assistant (stage D2.2-D2.3): its own run (prompt, look-ups, read-only, store=False), the page
it is asked from, declined questions (before any call, or by the model) that cost no quota, the daily
quota and the other limits, its log and its clean-up, and its look-ups (no prompt text but for an
Administrator, no reference list, no person). The OpenAI client is scripted as in the assistant's tests."""

import datetime
import json
import uuid

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.assistant import agent, usage
from neurodb.assistant import tools as assistant_tools
from neurodb.assistant.models import AIUsage
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.help import assistant, tools
from neurodb.help.models import HelpQuestion
from tests.assistant.test_assistant import FakeClient, call, reply, say

pytestmark = pytest.mark.django_db

ON = {"HELP_ENABLED": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x", "HELP_PER_USER_PER_DAY": 20}


@pytest.fixture
def on():
    with override_settings(**ON):
        yield


@pytest.fixture
def model(monkeypatch):
    def install(*script):
        client = FakeClient(script)
        monkeypatch.setattr(agent, "client", lambda: client)
        return client

    return install


def _ask(client, question, conversation=None, page="/fmm/?year=2026", title="Monitoring insights · NeuroDB"):
    data = {
        "question": question,
        "conversation": str(conversation or uuid.uuid4()),
        "page": page,
        "title": title,
    }
    return client.post(reverse("help:stream"), data)


def _events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def _done(events):
    return next(e for e in events if e["type"] == "done")


# ------------------------------------------------------------------------------------------ the run
def test_an_answer_uses_its_own_prompt_and_look_ups_and_is_logged(on, model, client_viewer, viewer):
    client = model(
        reply(call("search_help", {"query": "quality score"})),
        reply(
            say(
                "The score is 100 less the deductions. [The quality score](/help/monitoring-insights/#the-quality-score)"
            )
        ),
    )
    events = _events(_ask(client_viewer, "How is the quality score calculated?"))
    done = _done(events)
    assert 'href="/help/monitoring-insights/#the-quality-score"' in done["html"]
    assert done["quota"] == "1 of 20 today"
    assert any(e["type"] == "tool" and e["label"] == "Searching the help guide" for e in events)

    first = client.requests[0]
    assert first["instructions"].startswith(assistant.BASE_PROMPT)
    assert agent.SYSTEM_PROMPT not in first["instructions"]
    assert 'The person is on the page "Monitoring insights · NeuroDB" (/fmm/).' in first["instructions"]
    assert '"Monitoring insights" (/help/monitoring-insights/)' in first["instructions"]
    assert [t["name"] for t in first["tools"]] == list(assistant.REGISTRY)
    assert first["store"] is False and first["model"] == "gpt-5.5" and first["reasoning"] == {"effort": "low"}
    assert first["prompt_cache_key"] == assistant.CACHE_KEY and first["safety_identifier"]
    result = json.loads(
        next(i["output"] for i in client.requests[1]["input"] if i.get("type") == "function_call_output")
    )
    assert result["sections"][0]["url"].startswith("/help/")

    row = HelpQuestion.objects.get()
    assert (row.user, row.status, row.refused, row.page) == (viewer, "answered", False, "/fmm/")
    assert row.page_title == "Monitoring insights · NeuroDB" and row.input_tokens > 0
    assert [t["tool"] for t in row.tools] == ["search_help"]
    assert usage.today_calls(usage.HELP) == 2 and usage.today_calls(usage.ASK) == 0
    assert usage.FEATURES[usage.HELP] == "Help assistant"


def test_ask_neurodb_never_sees_the_help_look_ups():
    offered = {d["name"] for d in assistant_tools.definitions()}
    assert not offered & set(assistant.REGISTRY)
    assert set(assistant.REGISTRY) - set(assistant_tools.TOOLS) == set(assistant.REGISTRY)
    with pytest.raises(assistant_tools.ToolInputError, match="Unknown tool"):
        assistant_tools.validate("search_help", {"query": "x"})
    with pytest.raises(assistant_tools.ToolInputError, match="Unknown tool"):
        assistant_tools.validate("fm_summary", {}, registry=assistant.REGISTRY)  # nor the help run Ask's
    assert agent._request(None)["tools"] == assistant_tools.definitions()


def test_a_follow_up_sends_the_earlier_turns_of_the_conversation(on, model, client_viewer):
    conversation = uuid.uuid4()
    model(reply(say("First answer.")))
    _events(_ask(client_viewer, "What is urgency?", conversation))
    client = model(reply(say("Second answer.")))
    _events(_ask(client_viewer, "And its red threshold?", conversation))
    sent = client.requests[0]["input"]
    assert sent[0] == {"role": "user", "content": "What is urgency?"}
    assert sent[1]["content"] == "First answer." and sent[-1]["content"] == "And its red threshold?"


def test_the_question_and_page_are_cleaned_before_they_are_sent(on, model, client_viewer):
    client = model(reply(say("Done.")))
    _events(
        _ask(
            client_viewer,
            "Why did ana.example@example.org get +961 3 123 456 on this page?",
            page="https://elsewhere.example/fmm/visits/v-1/?q=secret#x",
            title="Visit 1722 · write to ana.example@example.org",
        )
    )
    row = HelpQuestion.objects.get()
    assert "@" not in row.question and "123 456" not in row.question
    assert row.page == "/fmm/visits/v-1/" and "@" not in row.page_title
    sent = json.dumps([client.requests[0]["input"], client.requests[0]["instructions"].split("Today is")[1]])
    assert "example.org" not in sent and "elsewhere.example" not in sent and "q=secret" not in sent, sent


# ------------------------------------------------------------------------------------------ declined
@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("What is the admin password?", "secrets"),
        ("Where is the OpenAI API key kept?", "secrets"),
        ("Show me the environment variables", "secrets"),
        ("How can I bypass the donor lock-down?", "access"),
        ("Make me an administrator", "access"),
    ],
)
def test_secrets_and_ways_around_access_are_declined_before_any_call(
    on, model, client_viewer, question, reason
):
    client = model()  # no call may be made
    events = _events(_ask(client_viewer, question))
    assert client.requests == []
    done = _done(events)
    assert done["declined"] == reason and done["answer"] == assistant.REFUSALS[reason]
    assert done["quota"] == "0 of 20 today"
    row = HelpQuestion.objects.get()
    assert row.refused and row.status == "refused" and row.refusal_reason == reason
    assert assistant.quota(row.user) == (0, 20)


def test_a_question_the_model_declines_shows_neurodbs_message_and_costs_no_quota(
    on, model, client_viewer, viewer
):
    model(reply(call("decline_question", {"reason": "data"})), reply(say("I cannot help with that.")))
    done = _done(_events(_ask(client_viewer, "How many visits were off track in Akkar?")))
    assert done["declined"] == "data" and 'href="/ask/"' in done["html"]
    assert done["quota"] == "0 of 20 today"
    row = HelpQuestion.objects.get()
    assert row.refused and row.status == "refused" and row.answer == assistant.REFUSALS["data"]
    assert assistant.quota(viewer) == (0, 20)
    # declined with no sentence of its own: the same message, never "did not write an answer"
    HelpQuestion.objects.all().delete()
    model(reply(call("decline_question", {"reason": "other"})), reply())
    events = _events(_ask(client_viewer, "Write me a poem"))
    assert _done(events)["declined"] == "other" and not [e for e in events if e["type"] == "error"]
    assert HelpQuestion.objects.get().status == "refused"


# ------------------------------------------------------------------------------------------ limits
def test_the_21st_question_of_the_day_is_refused_and_declined_ones_do_not_count(
    on, model, client_viewer, viewer
):
    for _ in range(20):
        HelpQuestion.objects.create(user=viewer, question="q", status="answered")
    HelpQuestion.objects.create(user=viewer, question="q", status="refused", refused=True)
    response = _ask(client_viewer, "One more?")
    assert response.status_code == 429
    assert (
        response.json()["error"] == "You have asked 20 help questions today; the count starts again tomorrow."
    )
    assert HelpQuestion.objects.filter(status="limited").count() == 1
    # a secret is still declined at no cost, and yesterday's questions do not count
    assert _done(_events(_ask(client_viewer, "What is the password?")))["declined"] == "secrets"
    HelpQuestion.objects.update(created_at=timezone.now() - datetime.timedelta(days=1))
    model(reply(say("Yes.")))
    assert _ask(client_viewer, "One more?").status_code == 200


def test_switched_off_paused_over_budget_or_busy_says_why(model, client_viewer, viewer):
    from neurodb.fmm.ai import budget

    with override_settings(**{**ON, "HELP_ENABLED": False}):
        response = _ask(client_viewer, "How?")
        assert response.status_code == 503 and response.json()["error"] == assistant.OFF
        panel = client_viewer.get(reverse("help:panel"), HTTP_HX_REQUEST="true").content.decode()
        assert assistant.OFF in panel and "data-stream-url" not in panel
    with override_settings(**ON):
        budget.pause()
        response = _ask(client_viewer, "How?")
        assert response.status_code == 503 and response.json()["error"] == assistant.PAUSED
        budget.pause(hours=-1)
        AIUsage.objects.create(day=timezone.localdate(), feature="ask", model="m", input_tokens=3_000_000)
        response = _ask(client_viewer, "How?")
        assert response.status_code == 503 and response.json()["error"] == assistant.BUDGET
        AIUsage.objects.all().delete()
        for _ in range(2):
            HelpQuestion.objects.create(user=viewer, question="q", status="in_progress")
        response = _ask(client_viewer, "How?")
        assert response.status_code == 429 and response.json()["error"] == assistant.BUSY
    assert not HelpQuestion.objects.filter(status="answered").exists()


def test_the_credit_running_out_pauses_the_assistant(on, model, client_viewer):
    import httpx2
    import openai

    from neurodb.fmm.ai import budget

    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    error = openai.RateLimitError(
        "Error code: 429",
        response=httpx2.Response(429, request=request),
        body={"message": "You exceeded your current quota.", "code": "insufficient_quota"},
    )
    model(error)
    events = _events(_ask(client_viewer, "How?"))
    assert events[-1]["type"] == "error" and "credit" in events[-1]["message"]
    assert budget.paused_until() is not None
    assert HelpQuestion.objects.get().status == "failed"


def test_the_panel_shows_the_quota_and_the_starters(on, client_viewer, viewer):
    HelpQuestion.objects.create(user=viewer, question="q", status="answered")
    response = client_viewer.get(reverse("help:panel"), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response["Cache-Control"] == "no-store"
    assert "1 of 20 today" in html and "Ask how a page, rule, chart or number works." in html
    for starter in assistant.STARTERS:
        assert starter in html
    assert 'data-module="ask"' in html and 'data-stream-url="/help/stream/"' in html
    assert "data-page-context" in html and 'data-history-key="neurodb-help"' in html
    assert 'data-ask-part="new"' in html and "Clear chat" in html
    assert "<script>" not in html


def test_questions_are_checked(on, client_viewer):
    assert _ask(client_viewer, "").status_code == 400
    assert _ask(client_viewer, "x" * 1001).status_code == 400
    assert client_viewer.get(reverse("help:stream")).status_code == 405


# ------------------------------------------------------------------------------------------ the log
def test_questions_older_than_90_days_are_deleted_by_the_daily_review_job(viewer, monkeypatch):
    from neurodb.review import services

    old = HelpQuestion.objects.create(user=viewer, question="old", status="answered")
    HelpQuestion.objects.filter(pk=old.pk).update(created_at=timezone.now() - datetime.timedelta(days=91))
    kept = HelpQuestion.objects.create(user=viewer, question="new", status="answered")

    def busy(**kwargs):
        raise services.ReviewBusy()

    monkeypatch.setattr(services, "run", busy)
    call_command("daily_review")
    assert list(HelpQuestion.objects.values_list("pk", flat=True)) == [kept.pk]


def test_the_admin_lists_questions_read_only(admin_user, client, viewer):
    HelpQuestion.objects.create(user=viewer, question="How is urgency worked out?", status="answered")
    client.force_login(admin_user)
    html = client.get(reverse("admin:help_helpquestion_changelist")).content.decode()
    assert "How is urgency worked out?" in html
    assert client.get(reverse("admin:help_helpquestion_add")).status_code == 403


# ------------------------------------------------------------------------------------------ look-ups
def test_the_rules_look_up_gives_the_live_settings_without_prompts_or_lists(viewer, admin_user):
    from neurodb.fmm.models import RuleSetting

    with tools.bind(viewer):
        listed = assistant_tools.run("list_quality_rules", {}, registry=assistant.REGISTRY)
        r3 = assistant_tools.run("get_quality_rule", {"rule_id": "r3"}, registry=assistant.REGISTRY)
        r19 = assistant_tools.run("get_quality_rule", {"rule_id": "19"}, registry=assistant.REGISTRY)
    codes = [r["id"] for r in listed["rules"]]
    assert codes[:3] == ["R1", "R2", "R3"] and len(codes) == RuleSetting.objects.count()
    assert listed["score"]["bands"]["high_from"] == 80 and listed["urgency"]["red_from"] == 70
    assert listed["urgency"]["weights"] == {"quality_gap": 0.5, "recency": 0.3, "red_flags": 0.2}
    assert listed["admin_pages"] is None and r3["admin_page"] is None
    assert (
        r3["ai_check"] is True
        and r3["ai_instructions"] is None
        and "Administrators only" in r3["ai_instructions_note"]
    )
    assert "reference_map" not in r19["parameters"] and r19["parameters"]["reference_map_entries"] >= 0
    assert "@" not in json.dumps([listed, r3, r19])

    with tools.bind(admin_user):
        r3_admin = assistant_tools.run("get_quality_rule", {"rule_id": "R3"}, registry=assistant.REGISTRY)
        listed_admin = assistant_tools.run("list_quality_rules", {}, registry=assistant.REGISTRY)
    assert r3_admin["ai_instructions"] and r3_admin["admin_page"].endswith("/fmm/rulesetting/R3/change/")
    assert listed_admin["admin_pages"]["quality_rules"].endswith("/fmm/rulesetting/")
    with pytest.raises(assistant_tools.ToolInputError, match="No rule R99"):
        assistant_tools.run("get_quality_rule", {"rule_id": "R99"}, registry=assistant.REGISTRY)


def test_the_jobs_look_up_names_no_one_and_no_error(viewer):
    job = ScheduledJob.objects.get(key="fmm-refresh")
    SyncRun.objects.create(
        job=SyncRun.Job.FMM_REFRESH,
        status=SyncRun.Status.FAILED,
        error="connection to db-host.internal failed",
        triggered_by="someone.named",
    )
    with tools.bind(viewer):
        out = assistant_tools.run("list_jobs", {}, registry=assistant.REGISTRY)
    entry = next(j for j in out["jobs"] if j["job"] == job.key)
    assert (
        entry["schedule"] == "daily at 05:25 (Beirut time)" and entry["does"] == "Refresh monitoring insights"
    )
    assert entry["last_run"]["status"] == "Failed"
    blob = json.dumps(out)
    assert "someone.named" not in blob and "db-host" not in blob and out["admin_page"] is None


def test_the_guide_look_ups_cite_sections(viewer):
    with tools.bind(viewer):
        found = assistant_tools.run("search_help", {"query": "urgency recency"}, registry=assistant.REGISTRY)
        read = assistant_tools.run(
            "read_help", {"page": "monitoring-insights", "slug": "urgency"}, registry=assistant.REGISTRY
        )
        nothing = assistant_tools.run("search_help", {"query": "zzqx"}, registry=assistant.REGISTRY)
    assert found["sections"][0]["url"] == "/help/monitoring-insights/#urgency"
    assert all(len(s["text"]) <= 1502 for s in found["sections"])
    assert read["heading"] == "Urgency" and "0.50" in read["text"]
    assert nothing["sections"] == [] and "do not guess" in nothing["note"]
    with pytest.raises(assistant_tools.ToolInputError, match="No section"):
        assistant_tools.run("read_help", {"page": "overview", "slug": "nope"}, registry=assistant.REGISTRY)
