"""Chat with Data (stage 7): a question about the visits of the page's filter, answered with the four
field monitoring look-ups only, under FMM's own prompt; the filter and the limit of texts bound for each
tool call; citations checked; its own log, quota and concurrency limits; history within one conversation
and filter."""

import datetime
import json
import threading
import uuid
from dataclasses import replace

import pytest
from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, usage
from neurodb.assistant import tools as assistant_tools
from neurodb.assistant.models import AssistantQuestion
from neurodb.fmm import privacy, views
from neurodb.fmm.ai import chat, citations, profiles, sampling, tools
from neurodb.fmm.models import ChatQuestion, ModelCapability
from neurodb.fmm.scope import Scope
from tests.assistant.test_assistant import FakeClient, call, reply, say
from tests.fmm.test_sampling import refusal

pytestmark = pytest.mark.django_db

AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}
FM_TOOLS = ["fm_summary", "fm_visits", "fm_visit", "fm_search"]
YEAR = "year=2026&section="
BEKAA = "year=2026&section=&governorate=bekaa"


@pytest.fixture
def ai_on():
    with override_settings(**AI_ON):
        yield profiles.published()


@pytest.fixture
def model(monkeypatch):
    """A scripted model: one reply (or an exception) per call; the requests are recorded."""

    def install(*script):
        client = FakeClient(script)
        monkeypatch.setattr(agent, "client", lambda: client)
        return client

    return install


def _ask(client, question, scope=YEAR, conversation=None):
    data = {"question": question, "scope": scope, "conversation": str(conversation or uuid.uuid4())}
    return client.post(reverse("fmm:chat_stream"), data)


def _events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def _done(events):
    return next(e for e in events if e["type"] == "done")


def _outputs(request):
    return [json.loads(i["output"]) for i in request["input"] if i.get("type") == "function_call_output"]


def _scope(query=YEAR):
    return Scope.from_params(QueryDict(query))


# ------------------------------------------------------------------------------------------ the request
def test_the_chat_offers_the_four_look_ups_under_its_own_prompt(built, ai_on, model, client_viewer):
    fake = model(reply(say("Eight visits are in this filter.")))
    events = _events(_ask(client_viewer, "How many visits?"))
    assert _done(events)["html"]
    request = fake.requests[0]
    assert [t["name"] for t in request["tools"]] == FM_TOOLS
    text = request["instructions"]
    assert text.startswith(ai_on.chat_instructions.strip())
    assert "Today is " in text and text.endswith(f"The visits in scope: {_scope().label()}.")
    assert agent.SYSTEM_PROMPT not in text
    for word in ("list_databases", "etools_query", "make_chart"):
        assert word not in text
    # what the admin's Preview shows is what is sent
    assert text == profiles.chat_instructions(ai_on, _scope())
    assert request["prompt_cache_key"] == "neurodb-fmm-chat"
    assert request["max_output_tokens"] == ai_on.chat_max_output_tokens
    assert request["reasoning"] == {"effort": ai_on.effort} and request["store"] is False
    assert request["temperature"] == 0.3 and "top_p" not in request
    assert request["safety_identifier"]


def test_the_preview_shows_the_chat_instructions_and_the_look_ups(built, ai_on, client, admin_user):
    client.force_login(admin_user)
    html = client.get(reverse("admin:fmm_promptversion_preview", args=[ai_on.pk])).content.decode()
    for name in FM_TOOLS:
        assert f"&quot;name&quot;: &quot;{name}&quot;" in html
    assert "own prompt is not sent to this chat" in html


def test_asks_and_watchs_requests_are_unchanged_by_the_chat():
    from neurodb.watch import investigate

    assert agent.RunOptions().tool_context is None
    assert agent._request(None)["instructions"].startswith(agent.SYSTEM_PROMPT)
    options = investigate.run_options([], [])
    assert options.tool_context is None and options.base_prompt is None
    assert agent._background_request(options)["instructions"].startswith(agent.SYSTEM_PROMPT)


# ------------------------------------------------------------------------------------------ the scope
def test_the_context_is_entered_for_each_tool_call_also_in_another_thread(monkeypatch, ai_on):
    seen = []

    def spy(**_args):
        seen.append((tools.bound(), tools.current()))
        return {"label": "spy", "url": "/fmm/"}

    monkeypatch.setitem(assistant_tools.TOOLS, "fm_summary", (spy, *assistant_tools.TOOLS["fm_summary"][1:]))
    ctx = chat.context(_scope(), ai_on)
    run = replace(
        chat.options(ai_on, ctx, sampling.Plan()), read_only=False, tool_filter=None
    )  # no database in the thread
    client = FakeClient(
        [reply(call("fm_summary", {}), call("fm_summary", {}, "call_2")), reply(say("Done."))]
    )
    monkeypatch.setattr(agent, "client", lambda: client)
    outside = []

    def answer():
        outcome = agent.Outcome()
        for _event in agent.answer("How many?", [], outcome, options=run):
            outside.append(tools.bound())  # between calls nothing is bound

    worker = threading.Thread(target=answer)
    worker.start()
    worker.join(30)
    assert len(seen) == 2 and all(bound and found is ctx for bound, found in seen)
    assert outside and not any(outside) and not tools.bound()


def test_a_look_up_can_narrow_the_filter_never_widen_it(built, ai_on):
    ctx = chat.context(_scope(BEKAA), ai_on)
    with tools.bind(ctx):
        assert tools.fm_visits(governorate="North")["total"] == 0
        assert tools.fm_visits()["total"] == _scope(BEKAA).visits().count() > 0
        out = tools.fm_visit("1726")  # a visit in the North
        assert out == {"error": f"Visit 1726 is not in the current filter ({_scope(BEKAA).label()})."}
        assert tools.fm_summary(period_from="2020-01-01")["period_from"] == "2026-01-01"
    bound = _scope("year=2026&section=Education")
    assert bound.narrow(sections="Health").empty and not bound.narrow(sections="Education").empty
    assert bound.narrow(start="2020-01-01").start == datetime.date(2026, 1, 1)
    assert bound.narrow(end="2030-01-01").end == datetime.date(2026, 12, 31)
    assert bound.narrow(start="2027-01-01").empty
    with_partner = _scope("year=2026&section=&partner=1")
    assert with_partner.narrow(partners=[2]).empty
    assert _scope(BEKAA).narrow(governorate="north").empty
    assert bound.narrow(programmatic=False).programmatic is False


def test_a_visit_is_read_by_the_key_its_card_gives(built, ai_on):
    """The model passes on the "key" a list returned ("visit:1722", "visit:r-…"): it must read that visit."""
    ctx = chat.context(_scope(), ai_on)
    with tools.bind(ctx):
        cards = tools.fm_visits(limit=30)["visits"]
        assert cards and all(c["key"].startswith("visit:") for c in cards)
        for c in cards:
            one = tools.fm_visit(c["key"])
            assert "error" not in one, c["key"]
            assert (one["key"], one["url"]) == (c["key"], c["url"])
        assert tools.fm_visit("Visit: 1722")["url"] == "/fmm/visits/1722/"


def test_the_last_check_keeps_every_field_a_look_up_returns(built, ai_on):
    """chat_filter drops keys that hold a person; none of the look-ups' own keys may look like one."""

    def keys(value, path=""):
        if isinstance(value, dict):
            return {
                k for key, inner in value.items() for k in {f"{path}.{key}"} | keys(inner, f"{path}.{key}")
            }
        if isinstance(value, list):
            return {k for inner in value for k in keys(inner, f"{path}[]")}
        return set()

    ctx = chat.context(_scope(), ai_on)
    check = privacy.chat_filter(ctx)
    with tools.bind(ctx):
        results = [tools.fm_summary(group_by=group) for group in ("none", "partner", "rule", "month")]
        results += [tools.fm_visits(limit=30), tools.fm_visit("1722"), tools.fm_search("classes")]
    for result in results:
        assert keys(check("fm_x", result)) == keys(result)
    assert results[1]["grouping"] == "partner" and ctx.privacy_blocked == 0


# ------------------------------------------------------------------------------------------ citations
def test_citations_of_visits_not_looked_up_are_unlinked_and_figures_listed(
    built, ai_on, model, client_viewer
):
    answer = (
        "[Visit 1722](/fmm/visits/1722/) was off track; [Visit 9999](/fmm/visits/9999/) too. "
        "It reached 444 children on 12 May 2026 under LEB/PCA2023597/PD2025123, see [the page](https://x.org)."
    )
    fake = model(reply(call("fm_visits", {"limit": 30})), reply(say(answer)))
    done = _done(_events(_ask(client_viewer, "Which visits were off track and why?")))
    assert 'href="/fmm/visits/1722/"' in done["html"] and "/fmm/visits/9999/" not in done["html"]
    assert "Visit 9999 (not checked)" in done["html"] and "x.org" not in done["html"]
    assert done["notice"] == (
        "1 visit reference was removed (not among the visits looked up). "
        "These figures could not be checked against the data: 444."
    )
    row = ChatQuestion.objects.get()
    assert row.status == "answered" and "(not checked)" in row.answer
    assert (row.checks["kept"], row.checks["removed"], row.checks["unchecked_numbers"]) == (
        ["1722"],
        ["9999"],
        ["444"],
    )
    assert row.checks["numbers"] and row.checks["privacy_blocked"] == 0
    assert len(fake.requests) == 2 and _outputs(fake.requests[1])[0]["total"] > 0


def test_a_fully_checked_answer_says_so():
    ctx = tools.ChatContext(scope=_scope(), texts_left=0, cards_max=15, seen={"1722"}, numbers={"64.7", "65"})
    checked = citations.verify(
        "[Visit 1722](/fmm/visits/1722/) scored 64.7% (about 65), 3 flags.", ctx, frozenset()
    )
    assert checked.kept == ["1722"] and not checked.removed and not checked.unchecked_numbers
    assert checked.notice == "All visit references were checked against the visits looked up."
    # e-mail addresses, phones and known names are replaced, line by line
    named = citations.verify("Ask Karim Canary\n- karim.canary@example.org", ctx, frozenset({"karim canary"}))
    assert "Karim" not in named.text and "@" not in named.text and "\n- " in named.text


def test_a_link_written_another_way_is_checked_too():
    """A titled link, a reference link or raw HTML must not carry an unseen visit past the check."""
    ctx = tools.ChatContext(
        scope=_scope(), texts_left=0, cards_max=15, seen={"1722"}, urls={"/fmm/?year=2026"}
    )
    answer = (
        '[Visit 9999](/fmm/visits/9999/ "Visit 9999") and [Visit 1722](</fmm/visits/1722/>).\n\n'
        'See [Visit 8888][a] and <a href="/fmm/visits/7777/">Visit 7777</a>, '
        '[the page](/fmm/?year=2026) and <a href="/partners/1/">a partner</a>.\n\n'
        "[a]: /fmm/visits/8888/\n"
    )
    checked = citations.verify(answer, ctx, frozenset())
    for key in ("9999", "8888", "7777"):
        assert f"/fmm/visits/{key}/" not in checked.html, key
        assert key in checked.removed, key
    assert 'href="/fmm/visits/1722/"' in checked.html and checked.kept == ["1722"]
    assert 'href="/fmm/?year=2026"' in checked.html  # a url a look-up returned
    assert "/partners/1/" not in checked.html and "a partner" in checked.html
    assert "Visit 9999 (not checked)" in checked.text and "Visit 7777 (not checked)" in checked.html
    # a name after a title is withheld even when NeuroDB does not know it
    assert "Layla" not in citations.verify("Mrs Layla Saab called.", ctx, frozenset()).text


# ------------------------------------------------------------------------------------------ history
def test_a_follow_up_keeps_the_visits_verified_earlier_and_a_new_filter_starts_afresh(
    built, ai_on, model, client_viewer
):
    conversation = uuid.uuid4()
    model(
        reply(call("fm_visit", {"visit": "1722"})),
        reply(say("[Visit 1722](/fmm/visits/1722/) was off track.")),
    )
    _events(_ask(client_viewer, "Tell me about visit 1722", conversation=conversation))
    fake = model(reply(say("Yes: [Visit 1722](/fmm/visits/1722/) and [Visit 1723](/fmm/visits/1723/).")))
    done = _done(_events(_ask(client_viewer, "Was it the worst?", conversation=conversation)))
    assert 'href="/fmm/visits/1722/"' in done["html"]  # verified in the first turn
    assert "Visit 1723 (not checked)" in done["html"]  # never looked up
    roles = [i.get("role") for i in fake.requests[0]["input"]]
    assert roles == ["user", "assistant", "user"]  # the first turn is re-sent
    # the same conversation under another filter: no history, nothing verified
    fake = model(reply(say("[Visit 1722](/fmm/visits/1722/).")))
    done = _done(_events(_ask(client_viewer, "And now?", scope=BEKAA, conversation=conversation)))
    assert [i.get("role") for i in fake.requests[0]["input"]] == ["user"]
    assert "Visit 1722 (not checked)" in done["html"]


# ------------------------------------------------------------------------------------------ the log and limits
def test_a_question_is_logged_apart_from_ask_and_recorded_as_fmm_use(
    built, ai_on, model, client_viewer, viewer
):
    model(reply(say("Eight visits.")))
    _events(_ask(client_viewer, "How many visits?"))
    row = ChatQuestion.objects.get()
    assert (row.user, row.status, row.version, row.question) == (
        viewer,
        "answered",
        ai_on,
        "How many visits?",
    )
    assert row.scope_hash == _scope().hash() and row.scope == _scope().canonical()
    assert set(row.checks) == {"kept", "removed", "unchecked_numbers", "numbers", "privacy_blocked"}
    assert row.sampling["temperature"]["state"] == "applied" and row.input_tokens > 0
    assert ModelCapability.objects.get(parameter="temperature").accepted is True
    assert not AssistantQuestion.objects.exists()
    assert usage.today_calls(usage.FMM) == 1 and usage.today_calls(usage.ASK) == 0


def test_the_21st_question_of_the_day_is_refused(built, ai_on, model, client_viewer, viewer):
    for _ in range(ai_on.chat_per_user_per_day):
        ChatQuestion.objects.create(
            user=viewer,
            conversation=uuid.uuid4(),
            scope_hash="s",
            version=ai_on,
            question="q",
            status="answered",
        )
    response = _ask(client_viewer, "One more?")
    assert response.status_code == 429
    assert response.json()["error"] == "You have asked 20 questions today; the count starts again tomorrow."
    assert ChatQuestion.objects.filter(status="limited").count() == 1
    # yesterday's questions do not count
    ChatQuestion.objects.update(created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
    model(reply(say("Yes.")))
    assert _ask(client_viewer, "One more?").status_code == 200


def test_the_chat_switched_off_says_why(built, model, client_viewer):
    response = _ask(client_viewer, "How many?")  # FMM_AI is off at deploy
    assert response.status_code == 503 and response.json()["error"] == str(views.CHAT_OFF)
    with override_settings(**AI_ON):
        draft = profiles.draft_from(profiles.published(), None, "No chat", chat_enabled=False)
        profiles.publish(draft, None)
        response = _ask(client_viewer, "How many?")
        assert response.status_code == 503 and response.json()["error"] == str(views.CHAT_DISABLED)
        html = client_viewer.get(reverse("fmm:dashboard"), {"year": "2026", "section": ""}).content.decode()
        assert str(views.CHAT_DISABLED) in html and "data-stream-url" not in html
    assert not ChatQuestion.objects.filter(status="in_progress").exists()


def test_at_most_two_answers_at_a_time_per_person(built, ai_on, model, client_viewer, viewer):
    for _ in range(2):
        ChatQuestion.objects.create(
            user=viewer,
            conversation=uuid.uuid4(),
            scope_hash="s",
            version=ai_on,
            question="q",
            status="in_progress",
        )
    response = _ask(client_viewer, "How many?")
    assert response.status_code == 503 and response.json()["error"] == str(views.CHAT_BUSY)
    # a row left in progress by a stopped server stops counting after the time limit
    ChatQuestion.objects.update(created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
    model(reply(say("Eight.")))
    assert _ask(client_viewer, "How many?").status_code == 200


def test_questions_are_checked(built, ai_on, client_viewer):
    assert _ask(client_viewer, "  ").status_code == 400
    assert _ask(client_viewer, "x" * 1001).status_code == 400
    with override_settings(FMM_ENABLED=False):
        assert _ask(client_viewer, "How many?").status_code == 404


def test_the_panel_sits_under_the_brief_with_the_page_filter(built, ai_on, client_viewer):
    html = client_viewer.get(reverse("fmm:dashboard"), {"year": "2026", "section": ""}).content.decode()
    assert 'data-module="ask"' in html and 'data-stream-url="/fmm/chat/stream/"' in html
    assert 'data-scope="year=2026&amp;section="' in html and "<template data-ask-turn>" in html
    assert "0 of 20 today" in html and "What are the main programmatic issues in this period?" in html
    assert "Names, emails and phone numbers are removed from your question before it is sent." in html
    for element in ("ask-form", "ask-input", "ask-thread", "ask-intro", "ask-submit", "ask-stop", "ask-new"):
        assert html.count(f'id="{element}"') == 1, element


def test_an_error_mid_answer_is_logged_and_shown_kindly(built, ai_on, model, client_viewer):
    model(ValueError("boom"))
    events = _events(_ask(client_viewer, "How many?"))
    assert events[-1] == {
        "type": "error",
        "message": "Something went wrong while answering. Please try again.",
    }
    row = ChatQuestion.objects.get()
    assert row.status == "failed" and "boom" in row.error and row.answer == ""


# ------------------------------------------------------------------------------------------ sampling
def test_a_refused_temperature_restarts_the_answer_once_without_it(built, ai_on, model, client_viewer):
    fake = model(refusal("temperature"), reply(say("Eight visits.")))
    done = _done(_events(_ask(client_viewer, "How many?")))
    assert "Eight visits." in done["html"]
    assert [("temperature" in r) for r in fake.requests] == [True, False]
    assert ModelCapability.objects.get(parameter="temperature").accepted is False
    row = ChatQuestion.objects.get()
    assert row.status == "answered" and row.sampling["temperature"]["state"] == "not_applied"
    # the next answer does not send it (a known refusal)
    fake = model(reply(say("Still eight.")))
    _events(_ask(client_viewer, "And now?"))
    assert "temperature" not in fake.requests[0]
