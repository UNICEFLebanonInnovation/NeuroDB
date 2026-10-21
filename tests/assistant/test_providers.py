"""An answer with tools, and a feature's own call, behave the same on both providers: each test runs once on
OpenAI (the real openai SDK against openai_mock, a stand-in for the Responses API) and once on Claude (the
real anthropic SDK against anthropic_mock, a stand-in for the Messages API). The replies differ only as the
two APIs differ; what the person sees, what is kept, what is logged and what the ledger books are the same.
No network access and no real API key.
"""

import datetime
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, llm, tools, usage
from neurodb.assistant.models import AIUsage, AssistantQuestion
from tests.assistant import anthropic_mock as am
from tests.assistant import openai_mock as om

OPENAI = {
    "AI_ASSISTANT_ENABLED": True,
    "OPENAI_API_KEY": "test-key-not-real",
    "AI_ASSISTANT_MODEL": "gpt-5.5",
    "AI_ASSISTANT_EFFORT": "medium",
}
QUESTION = "Is the data up to date?"


class OpenAIScript:
    """Replies of OpenAI's Responses API, and how its requests read. A lookup round's whole prompt is 100
    tokens (40 of them read from the cache), an answer's 150 (100 read)."""

    provider = llm.OPENAI
    model = om.MODEL

    def lookup(self, *names):
        calls = [
            om.function_call(f"fc_{i}", f"call_{i}", name, "{}")
            for i, name in enumerate(names or ["data_freshness"], 1)
        ]
        return om.turn(calls, usage=om.usage(100, 10, cached=40))

    def answer(self, text):
        return om.turn([om.message("msg_a", text, phase="final_answer")], usage=om.usage(150, 20, cached=100))

    def refused(self):
        return om.turn([om.refusal("msg_r", "I can't help with that.")])

    def too_long(self):
        return om.turn([om.message("msg_c", "Cut ")], end="incomplete")

    def empty(self):
        return om.turn([om.reasoning("rs_1")])

    def structured(self, text):
        """A feature's own call (not streamed): its prompt of 150 tokens, 50 of them read from the cache."""
        return om.completed([om.message("msg_s", text)], usage_=om.usage(150, 20, cached=50))

    def busy(self):
        return om.http_error(429, om.api_error_body("Rate limit reached", type_="rate_limit_error"))

    def no_credit(self):
        body = om.api_error_body(
            "You exceeded your current quota.", type_="insufficient_quota", code="insufficient_quota"
        )
        return om.http_error(429, body)

    def earlier(self, body):
        """The earlier turns a request sent, as (role, text)."""
        return [(item["role"], item["content"]) for item in body["input"] if "role" in item][:-1]

    def results(self, body):
        """The tool results a request sent back, by call."""
        return {
            item["call_id"]: json.loads(item["output"])
            for item in body["input"]
            if item.get("type") == "function_call_output"
        }


class ClaudeScript:
    """Replies of Claude's Messages API, and how its requests read; the same prompts as OpenAIScript's
    (Anthropic's input_tokens counts neither cache reads nor writes)."""

    provider = llm.CLAUDE
    model = am.MODEL

    def lookup(self, *names):
        calls = [
            am.tool_use(f"toolu_{i}", name, "{}") for i, name in enumerate(names or ["data_freshness"], 1)
        ]
        return am.turn([am.thinking("sig"), *calls], stop_reason="tool_use", usage=am.usage(60, 10, read=40))

    def answer(self, text):
        return am.turn([am.text(text)], usage=am.usage(50, 20, read=100))

    def refused(self):
        return am.refusal("cyber")

    def too_long(self):
        return am.turn([am.text("Cut ")], stop_reason="max_tokens")

    def empty(self):
        return am.turn([am.thinking("sig")])

    def structured(self, text):
        return am.turn([am.thinking("sig"), am.text(text)], usage=am.usage(100, 20, read=50))

    def busy(self):
        return am.http_error(529, am.error_body("overloaded_error", "Overloaded"))

    def no_credit(self):
        return am.http_error(402, am.error_body("billing_error", "Your credit balance is too low."))

    def earlier(self, body):
        return [(message["role"], message["content"]) for message in body["messages"][:-1]]

    def results(self, body):
        return {
            block["tool_use_id"]: json.loads(block["content"])
            for message in body["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        }


class AI:
    def __init__(self, script, install):
        self.script = script
        self.install = install


@pytest.fixture(params=[llm.OPENAI, llm.CLAUDE])
def ai(request, monkeypatch):
    """The provider under test, with ``install(*replies)`` starting its stand-in."""
    if request.param == llm.CLAUDE:
        return AI(ClaudeScript(), request.getfixturevalue("claude"))
    for var in ("NO_PROXY", "no_proxy"):  # reach the local stand-in directly even behind a proxy
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    for var in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"):
        monkeypatch.delenv(var, raising=False)
    overrides = override_settings(**OPENAI)
    overrides.enable()
    request.addfinalizer(overrides.disable)
    servers = []

    def install(*replies):
        server = om.MockResponsesServer(list(replies)).start()
        servers.append(server)
        monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
        return server

    request.addfinalizer(lambda: [server.stop() for server in servers])
    return AI(OpenAIScript(), install)


def _answer(history=(), options=None):
    outcome = agent.Outcome()
    events = list(agent.answer(QUESTION, list(history), outcome, options=options))
    return outcome, events


def _ask(client):
    response = client.post(reverse("assistant:stream"), {"question": QUESTION})
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def test_the_provider_in_use(ai):
    assert llm.provider() == ai.script.provider


def test_one_lookup_round_answered(ai, db):
    server = ai.install(ai.script.lookup(), ai.script.answer("The data is fresh."))
    outcome, events = _answer()
    assert [event["type"] for event in events] == ["tool", "text", "done"]
    assert events[0] == {"type": "tool", "label": "Checking data freshness", "round": 1}
    assert events[-1]["answer"] == "The data is fresh." and outcome.status == "answered"
    assert outcome.model == ai.script.model and outcome.calls == 2
    (result,) = ai.script.results(server.requests[1]["body"]).values()
    assert result["url"] == "/data/health/"
    assert [(t["tool"], t["ok"]) for t in outcome.tools] == [("data_freshness", True)]


def test_a_declined_question(ai, db):
    ai.install(ai.script.refused())
    outcome, events = _answer()
    assert events[-1]["type"] == "error" and events[-1]["message"] == agent.REFUSED
    assert outcome.status == "refused" and outcome.tools == [] and outcome.answer == ""


def test_an_answer_too_long(ai, db):
    ai.install(ai.script.too_long())
    outcome, events = _answer()
    assert events[-1] == {"type": "error", "message": agent.TOO_LONG}
    assert outcome.status == "failed" and outcome.error == "max_output_tokens"


def test_the_time_limit(ai, db):
    server = ai.install(ai.script.answer("Never sent."))
    outcome, events = _answer(options=agent.RunOptions(tools=("data_freshness",), time_limit=-1))
    assert events == [{"type": "error", "message": agent.TIME_UP}]
    assert outcome.error == "time limit" and server.requests == []


def test_too_many_lookups(ai, db):
    server = ai.install(ai.script.lookup(), ai.script.lookup())
    outcome, events = _answer(options=agent.RunOptions(tools=("data_freshness",), max_rounds=2))
    assert events[-1] == {"type": "error", "message": agent.TOO_MANY} and len(server.requests) == 2
    assert outcome.status == "failed" and outcome.error == "too many lookups"


def test_an_empty_answer(ai, db):
    ai.install(ai.script.empty())
    outcome, events = _answer()
    assert events == [{"type": "error", "message": agent.NO_ANSWER}]
    assert outcome.status == "failed" and outcome.error == "empty answer"


def test_the_same_earlier_turns_are_sent(ai, db):
    """The last six turns, then only those with a question and an answer (the 7th has no answer yet)."""
    history = [{"question": f"Question {n}?", "answer": f"Answer {n}."} for n in range(1, 7)]
    history.append({"question": "Question 7?", "answer": ""})
    server = ai.install(ai.script.answer("Fine."))
    _answer(history)
    expected = [
        pair for n in range(2, 7) for pair in (("user", f"Question {n}?"), ("assistant", f"Answer {n}."))
    ]
    assert ai.script.earlier(server.requests[0]["body"]) == expected


@pytest.mark.parametrize("reply, message", [("busy", llm.BUSY), ("no_credit", llm.NO_CREDIT)])
def test_a_busy_service_and_no_credit(ai, client_viewer, reply, message):
    ai.install(getattr(ai.script, reply)())
    events = _ask(client_viewer)
    assert events[-1] == {"type": "error", "message": message}
    assert AssistantQuestion.objects.get().status == "failed"


def test_the_question_log_and_the_ledger_count_the_whole_prompt(ai, client_viewer):
    ai.install(ai.script.lookup(), ai.script.answer("The data is fresh."))
    events = _ask(client_viewer)
    assert events[-1]["type"] == "done"
    row = AssistantQuestion.objects.get()
    # input_tokens + cache_read_tokens is the whole prompt (100 + 150) on both providers
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (110, 140, 30)
    assert row.input_tokens + row.cache_read_tokens == 250 and row.model == ai.script.model
    (booked,) = AIUsage.objects.filter(feature=usage.ASK)  # one model ran it: one row
    assert (booked.model, booked.calls) == (ai.script.model, 2)
    assert (booked.input_tokens, booked.cached_tokens, booked.output_tokens) == (110, 140, 30)


def test_a_background_run_filters_binds_and_reads_only(ai, db, caplog):
    def writes():
        AssistantQuestion.objects.create(question="written by a tool")
        return {"written": True}

    def freshness():
        return {"jobs": 4, "contact": "someone"}

    registry = {
        "data_freshness": (freshness, *tools.TOOLS["data_freshness"][1:]),
        "daily_review": (writes, *tools.TOOLS["daily_review"][1:]),
    }
    entered, filtered = [], []

    @contextmanager
    def bound():
        entered.append(True)
        yield

    def keep(name, result):
        filtered.append(name)
        return {"jobs": result["jobs"]}

    options = agent.RunOptions(
        tools=tuple(registry), registry=registry, tool_filter=keep, tool_context=bound, instructions="Look."
    )
    server = ai.install(
        ai.script.lookup("data_freshness", "daily_review"), ai.script.answer('{"found": "4 jobs"}')
    )

    outcome, events = _answer(options=options)

    assert events[-1]["type"] == "done" and outcome.status == "answered"
    found, failed = ai.script.results(server.requests[1]["body"]).values()
    assert found == {"jobs": 4}  # what the filter let through, nothing else
    assert failed == {"error": "The lookup failed on the server."}  # it tried to write
    assert "read-only transaction" in caplog.text and not AssistantQuestion.objects.exists()
    assert filtered == ["data_freshness"] and len(entered) == 2  # the context around each call
    assert outcome.numbers == {4.0}


def test_the_watch_morning_note(ai, db):
    from neurodb.watch import explain

    facts = SimpleNamespace(
        payload={"items": [{"key": "item:1", "title": "A progress report is due"}]},
        citable={"item:1": {"key": "item:1", "title": "A progress report is due"}},
    )
    answer = {"sentences": [{"text": "A progress report is due.", "keys": ["item:1"]}]}
    server = ai.install(ai.script.structured(json.dumps(answer)))

    asked = explain.ask(facts, datetime.date(2026, 10, 15), [])

    assert asked.called and asked.sentences == [{"text": "A progress report is due.", "keys": ["item:1"]}]
    assert asked.tokens == (100, 50, 20) and asked.model == ai.script.model and asked.dropped == []
    (booked,) = AIUsage.objects.filter(feature=usage.WATCH)
    assert (booked.model, booked.calls, booked.input_tokens, booked.cached_tokens) == (
        ai.script.model,
        1,
        100,
        50,
    )
    assert len(server.requests) == 1


def test_an_ai_check(ai, db):
    from neurodb.fmm.ai import checks

    server = ai.install(
        ai.script.structured(json.dumps({"is_coherent": False, "detail": "The rating is vague."}))
    )

    found = checks.check(
        llm.connect(timeout=10, retries=0),
        SimpleNamespace(code="R7"),
        "Is the narrative coherent with the rating?",
        {"narrative": "Sessions were held as planned in two centres."},
        SimpleNamespace(ai_model="", ai_temperature=0.3, ai_max_output_tokens=2000),
        frozenset(),
    )

    assert (found.passed, found.detail, found.model) == (False, "The rating is vague.", ai.script.model)
    assert (found.input_tokens, found.output_tokens) == (150, 20)  # the whole prompt, on both providers
    (booked,) = AIUsage.objects.filter(feature=usage.FMM_RULES)
    assert (booked.model, booked.calls) == (ai.script.model, 1)
    # the temperature goes to OpenAI only: Claude Opus 5.5 refuses it
    body = server.requests[0]["body"]
    sent = {name: body[name] for name in ("temperature", "top_p") if name in body}
    assert sent == ({"temperature": 0.3} if ai.script.provider == llm.OPENAI else {})
