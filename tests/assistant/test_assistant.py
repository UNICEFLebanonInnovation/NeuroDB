"""AI assistant: the function-calling loop against a scripted stand-in for the OpenAI SDK's
``responses.stream()``, the page, limits, errors, the question log, the tools and the settings."""

import ast
import hashlib
import json
import re
import runpy
import typing
import uuid
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import environ
import httpx2
import openai
import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from openai.resources.responses import Responses
from openai.types.responses import ParsedResponse, Response
from openai.types.shared import ReasoningEffort

from neurodb.assistant import agent, tools, views
from neurodb.assistant.models import AssistantQuestion
from neurodb.indicators.models import NeuroReportComment
from neurodb.partnerships.models import PartnerOrganization
from tests.assistant import openai_mock
from tests.assistant.openai_mock import reasoning, refusal, usage

# The model and effort are pinned: the tests check them, whatever the environment sets.
ENABLED = {
    "AI_ASSISTANT_ENABLED": True,
    "OPENAI_API_KEY": "test-key-not-real",
    "AI_ASSISTANT_MODEL": "gpt-5.5",
    "AI_ASSISTANT_EFFORT": "medium",
}


# ------------------------------------------------------------------------- a scripted model


def say(text, item_id="msg_1", phase=None):
    """An assistant message, streamed word by word."""
    return openai_mock.message(item_id, [w for w in re.split(r"(?<= )", text) if w], phase=phase)


def call(name, arguments, call_id="call_1"):
    """A function call; ``arguments`` is a dict (sent as JSON) or the raw arguments string."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return openai_mock.function_call(f"fc_{call_id}", call_id, name, raw)


def reply(*items, end="completed", reason=None, input_tokens=150, cached=50, output_tokens=20):
    """One model call: its output items and how the stream ends (completed, incomplete, failed,
    error or cut, as in openai_mock.turn)."""
    return SimpleNamespace(
        items=list(items), end=end, reason=reason, usage=usage(input_tokens, output_tokens, cached=cached)
    )


def _event(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


class FakeStream:
    """What ``responses.stream()`` returns: a context manager yielding the stream's events by their
    API type names; like the SDK, it only builds a final response after ``response.completed``,
    and that response is the SDK's own ParsedResponse (as get_final_response() returns)."""

    def __init__(self, step):
        self.step = step
        self.body = openai_mock.response_object(
            f"resp_{uuid.uuid4().hex[:8]}",
            status=step.end if step.end in ("incomplete", "failed") else "completed",
            output=[openai_mock.done_item(item) for item in step.items],
            usage_=step.usage,
            incomplete_reason=step.reason if step.end == "incomplete" else None,
            error={"code": "server_error", "message": "The server had an error."}
            if step.end == "failed"
            else None,
        )
        self.completed = self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        yield _event("response.created")
        deltas = {
            "message": "response.output_text.delta",
            "refusal": "response.refusal.delta",
            "function_call": "response.function_call_arguments.delta",
        }
        for index, item in enumerate(self.step.items):
            added = SimpleNamespace(type="message" if item.kind in ("message", "refusal") else item.kind)
            yield _event("response.output_item.added", output_index=index, item=added)
            for delta in item.deltas:
                yield _event(deltas[item.kind], output_index=index, delta=delta)
            yield _event("response.output_item.done", output_index=index)
        if self.step.end == "completed":
            self.completed = True
            yield _event("response.completed", response=self.get_final_response())
        elif self.step.end in ("incomplete", "failed"):  # the SDK hands over a plain Response here
            yield _event(f"response.{self.step.end}", response=Response.model_validate(self.body))
        elif self.step.end == "error":  # yielded by the SDK, not raised
            yield _event("error", code="rate_limit_exceeded", message="Slow down", param=None)

    def get_final_response(self):
        if not self.completed:
            raise RuntimeError("Didn't receive a `response.completed` event.")
        return ParsedResponse.model_validate(self.body)


STREAM_SIGNATURE = signature(Responses.stream)


class FakeClient:
    """Plays back one scripted reply per model call and records the requests (and the per-call
    options given to ``with_options``)."""

    max_retries = 2

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.options = []
        self.streams = []
        self.responses = SimpleNamespace(stream=self.stream)

    def with_options(self, **options):
        self.options.append(options)
        return self

    def stream(self, **params):
        STREAM_SIGNATURE.bind(None, **params)  # only parameters the real responses.stream() takes
        self.requests.append(json.loads(json.dumps(params)))  # plain JSON: no SDK objects sent back
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        self.streams.append(FakeStream(step))
        return self.streams[-1]


@pytest.fixture
def fake(monkeypatch):
    def install(*script):
        client = FakeClient(script)
        monkeypatch.setattr(agent, "client", lambda: client)
        return client

    return install


def _events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def _ask(client, question, conversation=None):
    data = {"question": question}
    if conversation:
        data["conversation"] = str(conversation)
    return client.post(reverse("assistant:stream"), data)


def _outputs(request):
    """The function_call_output items of a request, by call id, with their JSON decoded."""
    return {
        item["call_id"]: json.loads(item["output"])
        for item in request["input"]
        if item.get("type") == "function_call_output"
    }


# ------------------------------------------------------------------------- the loop and the page


@override_settings(**ENABLED)
def test_answer_streams_lookups_and_a_sanitized_answer(client_viewer, viewer, hierarchy, fake):
    db = hierarchy["database"]
    model = fake(
        reply(
            reasoning("rs_1", "enc-1-not-real", ["Find the database first"]),
            say("Let me check the databases.", phase="commentary"),
            call("list_databases", {}),
        ),
        reply(
            say(
                f"**{db.label}** has 2 indicators. See the [dashboard](/databases/{db.id}/).\n\n"
                "| Indicator | Value |\n|---|---|\n| Children | 300 |\n\n"
                "<script>alert(1)</script>![x](https://evil.example/x.png?leak=1)",
                item_id="msg_2",
                phase="final_answer",
            )
        ),
    )

    response = _ask(client_viewer, "How is Child Protection doing?")

    assert response["Content-Type"] == "text/event-stream"
    events = _events(response)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "text" and "tool" in kinds and kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "text" and e["round"] == 1) == (
        "Let me check the databases."
    )
    tool_events = [e for e in events if e["type"] == "tool"]
    assert tool_events == [{"type": "tool", "label": "Listing databases and reports", "round": 1}]
    html = events[-1]["html"]
    assert f'href="/databases/{db.id}/"' in html and 'target="_blank"' in html
    assert "<table>" in html
    assert "<script" not in html and "<img" not in html and "evil.example" not in html

    # the request: model, reasoning effort, stateless, function tools, instructions with the date
    first = model.requests[0]
    assert first["model"] == "gpt-5.5"
    assert first["reasoning"] == {"effort": "medium"}
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"]
    assert first["parallel_tool_calls"] is True and first["max_output_tokens"] >= 16000
    assert first["prompt_cache_key"] == "neurodb-assistant"
    assert first["tools"] == tools.definitions()
    assert all(t["type"] == "function" and t["strict"] is False for t in first["tools"])
    assert first["instructions"].startswith(agent.SYSTEM_PROMPT)  # stable text first: cached prefix
    assert f"Today is {timezone.localdate():%A %d %B %Y}." in first["instructions"]
    assert "The current reporting year is 2026." in first["instructions"]
    assert first["input"] == [{"role": "user", "content": "How is Child Protection doing?"}]
    for absent in ("temperature", "top_p", "previous_response_id", "user", "system", "messages", "betas"):
        assert absent not in first
    # a keyed hash of the user, never the id, username or email
    identifier = first["safety_identifier"]
    assert identifier == agent.safety_identifier(viewer) and re.fullmatch(r"[0-9a-f]{64}", identifier)
    assert (
        identifier != str(viewer.pk) and viewer.username not in identifier and viewer.email not in identifier
    )

    # round 2 replays round 1's output items as input, then the tool result with the matching call id
    second = model.requests[1]
    assert {k: v for k, v in second.items() if k != "input"} == {
        k: v for k, v in first.items() if k != "input"
    }
    assert second["input"][0] == first["input"][0]
    assert second["input"][1:4] == [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "Find the database first"}],
            "encrypted_content": "enc-1-not-real",
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_1",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Let me check the databases.", "annotations": []}],
            "phase": "commentary",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "list_databases",
            "arguments": "{}",
            "id": "fc_call_1",
        },
    ]
    result = second["input"][4]
    assert len(second["input"]) == 5
    assert result["type"] == "function_call_output" and result["call_id"] == "call_1"
    assert db.label in result["output"] and "error" not in json.loads(result["output"])

    logged = AssistantQuestion.objects.get()
    assert logged.status == "answered" and logged.user.username == "viewer" and logged.error == ""
    assert logged.model == "gpt-5.5"
    assert logged.tools == [{"tool": "list_databases", "input": {}, "ok": True, "ms": logged.tools[0]["ms"]}]
    # OpenAI counts cached tokens inside input_tokens: 2 rounds of 150 in (50 cached) and 20 out
    assert logged.input_tokens == 200 and logged.cache_read_tokens == 100 and logged.output_tokens == 40
    assert logged.answer.startswith(f"**{db.label}**")


@override_settings(**ENABLED)
def test_follow_up_questions_carry_the_conversation(client_viewer, db, fake):
    conversation = uuid.uuid4()
    model = fake(
        reply(say("There are 4 databases.")), reply(say("In 2025 there were 3.")), reply(say("Eight."))
    )

    _events(_ask(client_viewer, "How many databases?", conversation))
    _events(_ask(client_viewer, "And in 2025?", conversation))
    _events(_ask(client_viewer, "How many partners?", uuid.uuid4()))

    assert model.requests[0]["input"] == [{"role": "user", "content": "How many databases?"}]
    assert model.requests[1]["input"] == [
        {"role": "user", "content": "How many databases?"},
        {"role": "assistant", "content": "There are 4 databases.", "phase": "final_answer"},
        {"role": "user", "content": "And in 2025?"},
    ]
    assert model.requests[2]["input"] == [
        {"role": "user", "content": "How many partners?"}
    ]  # new conversation


@override_settings(**ENABLED)
def test_invalid_tool_arguments_go_back_to_the_model_as_an_error(client_viewer, db, fake):
    model = fake(
        reply(  # three parallel calls: arguments outside the schema, arguments that are not JSON, no such tool
            call("activity_breakdown", {"database_id": 1, "group_by": "colour"}, "call_1"),
            call("population", '{"category": "total",', "call_2"),
            call("drop_tables", {}, "call_3"),
        ),
        reply(say("Sorry, I used a wrong grouping.", item_id="msg_2")),
    )

    events = _events(_ask(client_viewer, "Reports by colour?"))

    assert [e["label"] for e in events if e["type"] == "tool"] == [
        "Counting activity reports",
        "Reading population figures",
        "Looking up data",
    ]
    outputs = _outputs(model.requests[1])
    assert list(outputs) == ["call_1", "call_2", "call_3"]
    assert "must be one of" in outputs["call_1"]["error"]
    assert outputs["call_1"]["received"] == {"database_id": 1, "group_by": "colour"}
    assert "not valid JSON" in outputs["call_2"]["error"]
    assert outputs["call_2"]["received"] == '{"category": "total",'
    assert "Unknown tool" in outputs["call_3"]["error"]
    assert events[-1]["type"] == "done"
    logged = AssistantQuestion.objects.get()
    assert logged.status == "answered"
    assert [(t["tool"], t["ok"]) for t in logged.tools] == [
        ("activity_breakdown", False),
        ("population", False),
        ("drop_tables", False),
    ]
    assert logged.tools[1]["input"] == '{"category": "total",'


@override_settings(**ENABLED)
def test_a_failing_lookup_is_reported_to_the_model_without_details(client_viewer, db, fake, monkeypatch):
    def broken():
        raise RuntimeError("connection to the warehouse lost")

    monkeypatch.setitem(tools.TOOLS, "data_freshness", (broken, *tools.TOOLS["data_freshness"][1:]))
    model = fake(
        reply(call("data_freshness", {})), reply(say("The freshness check failed.", item_id="msg_2"))
    )

    events = _events(_ask(client_viewer, "Is the data up to date?"))

    assert events[-1]["type"] == "done"
    assert _outputs(model.requests[1]) == {"call_1": {"error": "The lookup failed on the server."}}
    assert AssistantQuestion.objects.get().tools[0]["ok"] is False


@pytest.mark.parametrize(
    "declined",
    [
        reply(refusal("msg_1", "I can't help with that.")),  # a refusal content part
        reply(say("I can't help with that."), end="incomplete", reason="content_filter"),
    ],
    ids=["refusal-part", "content-filter"],
)
@override_settings(**ENABLED)
def test_refusal_is_reported_not_shown_as_an_answer(client_viewer, db, fake, declined):
    fake(declined)
    events = _events(_ask(client_viewer, "Something the model declines"))
    assert events[-1] == {"type": "error", "message": agent.REFUSED}
    assert "done" not in [e["type"] for e in events]
    logged = AssistantQuestion.objects.get()
    assert logged.status == "refused" and logged.answer == ""


@pytest.mark.parametrize(
    "reason, message, error",
    [
        ("max_output_tokens", "The answer was too long. Try a narrower question.", "max_output_tokens"),
        ("max_messages", "The answer could not be completed. Please try again.", "incomplete: max_messages"),
    ],
)
@override_settings(**ENABLED)
def test_an_incomplete_answer_fails_without_running_its_calls(
    client_viewer, db, fake, reason, message, error
):
    model = fake(
        reply(
            say("Here is a very long"),
            call("data_freshness", '{"', "call_1"),  # cut off mid-arguments: must not run
            end="incomplete",
            reason=reason,
            output_tokens=25000,
        )
    )

    events = _events(_ask(client_viewer, "Everything about everything"))

    assert events[-1] == {"type": "error", "message": message}
    assert "tool" not in [e["type"] for e in events] and len(model.requests) == 1
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == error and logged.tools == []
    assert (logged.input_tokens, logged.cache_read_tokens, logged.output_tokens) == (100, 50, 25000)


def _status_error(cls, status, body=None):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    return cls(f"Error code: {status}", response=response, body=body)


@pytest.mark.parametrize(
    "error, message",
    [
        (
            _status_error(openai.RateLimitError, 429),
            "The AI service is busy right now. Please try again in a minute.",
        ),
        (
            _status_error(
                openai.RateLimitError,
                429,
                {
                    "message": "You exceeded your current quota.",
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                },
            ),
            "The AI service's credit for this application has run out. Ask an administrator.",
        ),
        (
            _status_error(openai.AuthenticationError, 401),
            "The AI assistant's API key was rejected. Ask an administrator.",
        ),
        (
            _status_error(openai.NotFoundError, 404),
            "The AI assistant's model is not available. Ask an administrator.",
        ),
        (
            openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com/v1/responses")),
            "The AI service did not answer in time. Please try again.",
        ),
    ],
    ids=["rate-limit", "no-credit", "bad-key", "unknown-model", "timeout"],
)
@override_settings(**ENABLED)
def test_api_errors_become_friendly_messages(client_viewer, db, fake, error, message):
    fake(error)
    events = _events(_ask(client_viewer, "Anything"))
    assert events == [{"type": "error", "message": message}]
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error.startswith(type(error).__name__)


UNFINISHED = "The AI service could not finish this answer. Please try again."


@pytest.mark.parametrize(
    "end, code, message",
    [
        ("failed", "server_error", UNFINISHED),
        ("error", "rate_limit_exceeded", views.BUSY),  # the same message as an HTTP 429
        ("cut", "stream_ended", UNFINISHED),
    ],
)
@override_settings(**ENABLED)
def test_a_stream_that_ends_badly_becomes_a_friendly_message(client_viewer, db, fake, end, code, message):
    fake(reply(say("Checking the data."), end=end))
    events = _events(_ask(client_viewer, "Anything"))
    assert events[0]["type"] == "text"
    assert events[-1] == {"type": "error", "message": message}
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error.startswith(f"ServiceError: {code}")
    if end == "failed":  # a failed response still reports (and costs) its tokens
        assert (logged.input_tokens, logged.cache_read_tokens, logged.output_tokens) == (100, 50, 20)


@override_settings(**ENABLED, AI_ASSISTANT_MAX_TOOL_ROUNDS=2)
def test_too_many_lookup_rounds_stop_the_answer(client_viewer, db, fake):
    model = fake(reply(call("data_freshness", {}, "call_1")), reply(call("data_freshness", {}, "call_2")))
    events = _events(_ask(client_viewer, "Keep looking"))
    assert events[-1] == {
        "type": "error",
        "message": "This question needed too many lookups. Try splitting it up.",
    }
    assert len(model.requests) == 2
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == "too many lookups" and len(logged.tools) == 2


@override_settings(**ENABLED, AI_ASSISTANT_TIME_LIMIT_SECONDS=-1)
def test_the_time_limit_stops_the_answer(client_viewer, db, fake):
    model = fake(reply(say("Never sent.")))
    events = _events(_ask(client_viewer, "Slow question"))
    assert events == [
        {"type": "error", "message": "This question took too long. Try asking something narrower."}
    ]
    assert model.requests == []
    assert AssistantQuestion.objects.get().error == "time limit"


@override_settings(**ENABLED)
def test_a_response_without_text_is_not_an_answer(client_viewer, db, fake):
    fake(reply(reasoning("rs_1")))  # only reasoning came back
    events = _events(_ask(client_viewer, "Anything"))
    assert events == [
        {"type": "error", "message": "The assistant did not write an answer. Try rephrasing it."}
    ]
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == "empty answer"


@override_settings(**ENABLED, AI_ASSISTANT_HOURLY_LIMIT=2)
def test_hourly_limit_per_user(client_viewer, db, fake):
    fake(reply(say("One.")), reply(say("Two.")))
    _events(_ask(client_viewer, "Q1"))
    _events(_ask(client_viewer, "Q2"))
    third = _ask(client_viewer, "Q3")
    assert third.status_code == 429
    assert AssistantQuestion.objects.filter(status="limited").count() == 1


@override_settings(**ENABLED, AI_ASSISTANT_HOURLY_LIMIT=1)
def test_questions_sent_at_the_same_time_count_towards_the_limit(client_viewer, db, fake):
    model = fake(reply(say("One.")), reply(say("Two.")), reply(say("Three.")))
    responses = [_ask(client_viewer, f"Q{n}") for n in range(3)]  # sent before any answer is done
    assert [r.status_code for r in responses] == [200, 429, 429]
    assert _events(responses[0])[-1]["type"] == "done"
    assert len(model.requests) == 1
    assert AssistantQuestion.objects.filter(status="answered").count() == 1
    assert AssistantQuestion.objects.filter(status="limited").count() == 2


@override_settings(**ENABLED)
def test_a_user_can_have_two_questions_answered_at_a_time(client_viewer, db, fake):
    fake(reply(say("One.")), reply(say("Two.")), reply(say("Three.")))
    first, second, third = (_ask(client_viewer, f"Q{n}") for n in range(3))
    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    running = AssistantQuestion.objects.filter(error=views.IN_PROGRESS)
    # not "answered" until done: never replayed as history or listed as a recent question
    assert running.count() == 2 and set(running.values_list("status", flat=True)) == {"failed"}
    _events(first)
    assert _ask(client_viewer, "Q3").status_code == 200  # one is done: there is room again


@override_settings(**ENABLED)
def test_questions_are_validated(client_viewer, db):
    assert _ask(client_viewer, "   ").status_code == 400
    assert _ask(client_viewer, "x" * 1001).status_code == 400
    assert _ask(client_viewer, "\x00 ").status_code == 400


@override_settings(**ENABLED)
def test_a_nul_character_is_removed_from_the_question(client_viewer, db, fake):
    model = fake(reply(say("Eight.")))
    events = _events(_ask(client_viewer, "How many\x00 partners?"))
    assert events[-1]["type"] == "done"
    assert model.requests[0]["input"] == [{"role": "user", "content": "How many partners?"}]
    logged = AssistantQuestion.objects.get()
    assert logged.question == "How many partners?" and logged.status == "answered"


@override_settings(**ENABLED)
def test_values_the_database_cannot_store_are_still_logged(client_viewer, db, fake):
    fake(
        reply(
            call("database_results", '{"database_id": NaN}', "call_1"),  # JSON as Python reads it
            call("population", {"category": "tot\u0000al"}, "call_2"),
        ),
        reply(say("No such database.\x00", item_id="msg_2")),
    )
    events = _events(_ask(client_viewer, "Results of database NaN?"))
    assert events[-1]["type"] == "done"
    logged = AssistantQuestion.objects.get()
    assert logged.status == "answered" and logged.answer == "No such database."
    assert [t["input"] for t in logged.tools] == [{"database_id": "nan"}, {"category": "total"}]


@override_settings(**ENABLED)
def test_a_failed_log_write_does_not_break_the_answer(client_viewer, db, fake, monkeypatch, caplog):
    fake(reply(say("Eight.")))
    response = _ask(client_viewer, "How many partners?")  # the question's row is written here

    def broken(self, *args, **kwargs):
        raise DatabaseError("the database went away")

    monkeypatch.setattr(AssistantQuestion, "save", broken)
    assert [e["type"] for e in _events(response)] == ["text", "done"]
    assert "could not log question" in caplog.text


def test_disabled_without_an_api_key(client_viewer, db):
    with override_settings(AI_ASSISTANT_ENABLED=False, OPENAI_API_KEY=""):
        page = client_viewer.get(reverse("assistant:ask")).content.decode()
        assert "not set up yet" in page and "OPENAI_API_KEY" in page
        assert _ask(client_viewer, "Hello?").status_code == 503
        with pytest.raises(agent.AssistantUnavailable):
            agent.client()
    assert not AssistantQuestion.objects.exists()


def test_the_client_talks_to_the_openai_api(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with override_settings(**ENABLED):
        api = agent.client()
    assert isinstance(api, openai.OpenAI) and api.api_key == "test-key-not-real"
    assert str(api.base_url) == "https://api.openai.com/v1/"
    # connecting fails fast (an unreachable API); a stream may stay silent while the model reasons
    assert api.timeout.connect == 10.0 and api.timeout.read == 120.0 and api.max_retries == 2


@pytest.mark.parametrize(
    "remaining, retries, wait",
    [
        (179.9, 1, 89.95),  # the first round: one retry, each attempt waits at most half the time
        (130.0, 1, 65.0),
        (119.0, 0, 119.0),  # no time for a retry (nor a Retry-After sleep)
        (60.0, 0, 60.0),
        (3.0, 0, 10.0),  # never less than the connect timeout
        (400.0, 2, 120.0),  # a longer time limit: the client's own retries and timeout
    ],
)
def test_each_model_call_is_held_to_the_time_left(remaining, retries, wait):
    with override_settings(**ENABLED):
        api = agent._within(agent.client(), remaining)
    assert api.max_retries == retries
    assert api.timeout.read == pytest.approx(wait) and api.timeout.connect == 10.0
    # the worst case with no data at all stays within the time left (plus a connect timeout)
    assert (retries + 1) * wait <= max(remaining, 10.0) + 1e-9


@override_settings(**ENABLED)
def test_the_rounds_use_the_time_left(client_viewer, db, fake):
    model = fake(reply(call("data_freshness", {})), reply(say("Fresh.", item_id="msg_2")))
    _events(_ask(client_viewer, "Is the data fresh?"))
    first, second = model.options
    assert first["max_retries"] == 1 and first["timeout"].read <= 90.0
    assert second["timeout"].read <= first["timeout"].read and second["timeout"].connect == 10.0


def test_the_safety_identifier_is_a_keyed_hash_of_the_user(viewer, admin_user):
    identifier = agent.safety_identifier(viewer)
    assert re.fullmatch(r"[0-9a-f]{64}", identifier)  # the API allows at most 64 characters
    assert identifier == agent.safety_identifier(viewer) != agent.safety_identifier(admin_user)
    assert (
        identifier != hashlib.sha256(str(viewer.pk).encode()).hexdigest()
    )  # keyed: not guessable from the id
    assert agent.safety_identifier(None) is None and agent.safety_identifier(AnonymousUser()) is None


def test_assistant_requires_sign_in(client, db):
    assert client.get(reverse("assistant:ask")).status_code == 302
    assert client.post(reverse("assistant:stream"), {"question": "hi"}).status_code in (302, 403)


@override_settings(**ENABLED)
def test_ask_page_and_search_entry(client_viewer, hierarchy):
    page = client_viewer.get(reverse("assistant:ask") + "?q=hello").content.decode()
    assert 'data-initial-question="hello"' in page and "js/ask.js" in page
    assert "OpenAI API" in page

    search = reverse("reports:search")
    question = client_viewer.get(search + "?q=which indicators are off track", HTTP_HX_REQUEST="true")
    html = question.content.decode()
    assert "Ask NeuroDB AI" in html  # a question with no name matches: the AI entry is offered
    keyword = client_viewer.get(search + "?q=Children", HTTP_HX_REQUEST="true").content.decode()
    assert keyword.index("Children") < keyword.index("Ask NeuroDB AI")  # keyword search: results first


@override_settings(**ENABLED)
def test_a_stopped_answer_is_logged_as_stopped(rf, viewer, fake):
    from neurodb.assistant.views import _stream

    model = fake(reply(say("A long answer that the user stops.")))
    request = rf.post("/ask/stream/")
    request.user = viewer
    row = AssistantQuestion.objects.create(
        user=viewer, question="Something long", status="failed", error=views.IN_PROGRESS
    )
    stream = _stream(request, row)
    next(stream)  # the first words arrive, then the browser goes away and the server closes the stream
    stream.close()
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == "stopped"
    assert model.streams[0].closed  # the model's stream (its HTTP response) was closed too


def test_friendly_messages_cover_the_openai_errors():
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    assert views._friendly(openai.APIError("server_error", request, body=None)) == (
        "The AI service is temporarily unavailable. Please try again."
    )
    assert views._friendly(openai.APIConnectionError(request=request)) == (
        "The AI service could not be reached. Please try again."
    )
    assert views._friendly(_status_error(openai.PermissionDeniedError, 403)) == (
        "The AI assistant's API key is not allowed to use this model."
    )
    assert views._friendly(_status_error(openai.InternalServerError, 500)) == (
        "The AI service is temporarily unavailable. Please try again."
    )
    assert views._friendly(ValueError("unexpected")) is None  # logged with its traceback instead
    # the same conditions inside the stream get the same messages as the HTTP errors
    assert views._friendly(agent.ServiceError("insufficient_quota", "No credit")) == views.NO_CREDIT
    assert views._friendly(agent.ServiceError("rate_limit_exceeded", "Slow down")) == views.BUSY


# ------------------------------------------------------------------------- the tools themselves


def test_database_results_match_the_dashboard(hierarchy):
    db = hierarchy["database"]
    data = tools.run("database_results", {"database_id": db.id})
    by_code = {i["awp_code"]: i for i in data["indicators"]}
    assert by_code["1"]["achieved_value"] == 500.0  # 100 + 50 + 200 + 150
    assert by_code["1"]["target"] == 1000.0
    assert data["url"] == reverse("reports:database_dashboard", args=[db.id])


def test_activity_breakdown_groups_and_filters(hierarchy):
    db = hierarchy["database"]
    by_partner = tools.run("activity_breakdown", {"database_id": db.id, "group_by": "partner"})
    assert {r["partner"]: r["activity_reports"] for r in by_partner["rows"]} == {
        "Partner A": 2,
        "Partner B": 2,
    }
    feb = tools.run("activity_breakdown", {"database_id": db.id, "group_by": "governorate", "month": 2})
    assert [r["governorate"] for r in feb["rows"]] == ["Akkar"]


def test_the_tools_do_not_read_records_the_pages_hide(hierarchy):
    kept = PartnerOrganization.objects.create(
        etl_id="98", name="Kept Org", partner_type="Civil Society Organization", vendor_number="V-2"
    )
    assert tools.run("partner_details", {"partner_id": kept.id})["name"] == "Kept Org"
    deleted = PartnerOrganization.objects.create(
        etl_id="99",
        name="Deleted Org",
        partner_type="Civil Society Organization",
        vendor_number="V-1",
        deleted_flag=True,
    )
    with pytest.raises(tools.ToolInputError, match="No partner"):
        tools.run("partner_details", {"partner_id": deleted.id})
    report = hierarchy["report"]
    report.is_active = False
    report.save()
    with pytest.raises(tools.ToolInputError, match="No Neuro report"):
        tools.run("neuro_report", {"report_id": report.id})


def test_neuro_report_sends_the_comment_text(hierarchy):
    report = hierarchy["report"]
    NeuroReportComment.objects.create(
        report=report, comment="Stock-out of kits in Akkar.", related_month="01"
    )
    NeuroReportComment.objects.create(report=report, comment="x" * 5000, related_month="02")
    comments = tools.run("neuro_report", {"report_id": report.id, "month": 3})["comments"]
    assert [c["text"] for c in comments] == ["Stock-out of kits in Akkar.", "x" * 1000]


def test_tool_arguments_are_checked():
    with pytest.raises(tools.ToolInputError):
        tools.validate("database_results", {})
    with pytest.raises(tools.ToolInputError):
        tools.validate("database_results", {"database_id": "3"})
    with pytest.raises(tools.ToolInputError):
        tools.validate("population", {"category": "total", "extra": 1})
    with pytest.raises(tools.ToolInputError):
        tools.validate("no_such_tool", {})
    assert tools.validate("neuro_report", {"report_id": 1, "month": 5}) == {"report_id": 1, "month": 5}


def test_a_null_required_argument_is_an_input_error():
    with pytest.raises(tools.ToolInputError, match="Missing required argument 'database_id'"):
        tools.validate("database_results", {"database_id": None})


def test_every_tool_definition_is_a_well_formed_function_tool():
    definitions = tools.definitions()
    assert [d["name"] for d in definitions] == list(tools.TOOLS)  # a fixed order keeps the cached prefix
    assert json.loads(json.dumps(definitions)) == definitions
    for d in definitions:
        assert set(d) == {"type", "name", "description", "parameters", "strict"}
        # strict is sent explicitly: the Responses API applies strict mode when it is left out
        assert d["type"] == "function" and d["strict"] is False
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", d["name"])
        assert len(d["description"]) > 40
        schema = d["parameters"]
        assert schema == tools.TOOLS[d["name"]][2]  # the schemas validate() checks, sent unchanged
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


def _sample(spec):
    if "enum" in spec:
        return spec["enum"][0]
    return {"string": "x", "integer": spec.get("minimum", 1), "boolean": True, "object": {}, "array": []}[
        spec["type"]
    ]


def test_validate_enforces_every_schema_keyword():
    """Strict mode is off, so the API does not hold the model to the schemas: validate() is the
    guard, and it must understand and enforce every keyword the schemas use."""
    wrong_type = {"string": 7, "integer": "7", "boolean": "yes", "object": "x", "array": "x"}
    for name, (_, _, schema, _) in tools.TOOLS.items():
        props = schema["properties"]
        valid = {key: _sample(props[key]) for key in schema["required"]}
        assert tools.validate(name, valid) == valid
        for key in schema["required"]:
            with pytest.raises(tools.ToolInputError):
                tools.validate(name, {k: v for k, v in valid.items() if k != key})
        with pytest.raises(tools.ToolInputError):
            tools.validate(name, {**valid, "not_a_parameter": 1})
        for key, spec in props.items():
            assert set(spec) <= {"type", "description", "enum", "minimum", "maximum", "items"}, (name, key)
            if spec["type"] == "array":
                assert spec["items"] == {"type": "string"}, (name, key)  # validate() allows strings only
            with pytest.raises(tools.ToolInputError):
                tools.validate(name, {**valid, key: wrong_type[spec["type"]]})
            if spec["type"] == "integer":  # booleans are not integers here, unlike in Python
                with pytest.raises(tools.ToolInputError):
                    tools.validate(name, {**valid, key: True})
            if "enum" in spec:
                with pytest.raises(tools.ToolInputError):
                    tools.validate(name, {**valid, key: "not-a-choice"})
            if "minimum" in spec:
                with pytest.raises(tools.ToolInputError):
                    tools.validate(name, {**valid, key: spec["minimum"] - 1})
            if "maximum" in spec:
                with pytest.raises(tools.ToolInputError):
                    tools.validate(name, {**valid, key: spec["maximum"] + 1})
            if key not in schema["required"]:  # an optional argument sent as null is left out
                assert tools.validate(name, {**valid, key: None}) == valid


def test_rendering_keeps_alignment_and_drops_unsafe_links():
    html = agent.render("|a|b|\n|---|--:|\n|x|1|\n\n[bad](javascript:alert(1)) [ok](/databases/3/)")
    assert '<td align="right">1</td>' in html
    assert "javascript:" not in html
    assert 'href="/databases/3/"' in html


@pytest.mark.parametrize(
    "markdown_",
    [
        "[Sign in again](https://evil.example/login?d=secret)",
        "[x](//evil.example/)",
        "[x](/\\evil.example/)",
        '<a href="/&#9;/evil.example/">x</a>',
        '<a href="/&#x0A;/evil.example/">x</a>',
        "<https://evil.example/a>",
        "[m](mailto:a@evil.example?body=secret)",
    ],
)
def test_rendering_keeps_only_links_to_neurodb_pages(markdown_):
    html = agent.render(f"See {markdown_} and the [population](/population/?year=2026&view=idp).")
    # the link text stays, but only the link to NeuroDB's own page keeps its href
    assert html.count("href=") == 1 and 'href="/population/?year=2026&amp;view=idp"' in html


def test_questions_are_listed_in_the_admin(client, db, roles):
    from neurodb.accounts.models import User

    root = User.objects.create_superuser(
        username="root", email="root@example.org", password="root-pass-123456"
    )
    AssistantQuestion.objects.create(
        user=root, question="How many partners?", answer="Eight.", output_tokens=12
    )
    client.force_login(root)
    page = client.get(reverse("admin:assistant_assistantquestion_changelist"))
    assert page.status_code == 200 and "How many partners?" in page.content.decode()
    assert "AI questions" in client.get(reverse("admin:index")).content.decode()


# ------------------------------------------------------------------------- settings and imports

SETTINGS_FILE = Path(__file__).resolve().parents[2] / "config" / "settings.py"


def _settings(monkeypatch, **env):
    """Run the settings module afresh with these environment variables (and not a local .env)."""
    monkeypatch.setattr(environ.Env, "read_env", classmethod(lambda cls, *args, **kwargs: None))
    for var in ("OPENAI_API_KEY", "AI_ASSISTANT_ENABLED", "AI_ASSISTANT_MODEL", "AI_ASSISTANT_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    return runpy.run_path(str(SETTINGS_FILE))


def test_settings_read_the_openai_key_and_defaults(monkeypatch):
    loaded = _settings(monkeypatch, OPENAI_API_KEY="test-key-not-real")
    assert loaded["OPENAI_API_KEY"] == "test-key-not-real" and loaded["AI_ASSISTANT_ENABLED"] is True
    assert loaded["AI_ASSISTANT_MODEL"] == "gpt-5.5" and loaded["AI_ASSISTANT_EFFORT"] == "medium"

    unresolved = _settings(
        monkeypatch, OPENAI_API_KEY="@Microsoft.KeyVault(VaultName=kv;SecretName=openai-api-key)"
    )
    assert unresolved["OPENAI_API_KEY"] == "" and unresolved["AI_ASSISTANT_ENABLED"] is False
    assert _settings(monkeypatch)["AI_ASSISTANT_ENABLED"] is False
    switched_off = _settings(monkeypatch, OPENAI_API_KEY="test-key-not-real", AI_ASSISTANT_ENABLED="false")
    assert switched_off["AI_ASSISTANT_ENABLED"] is False


def test_settings_strip_the_key(monkeypatch):
    padded = _settings(monkeypatch, OPENAI_API_KEY=" test-key-not-real\n")  # e.g. saved from a file
    assert padded["OPENAI_API_KEY"] == "test-key-not-real" and padded["AI_ASSISTANT_ENABLED"] is True
    blank = _settings(monkeypatch, OPENAI_API_KEY="  \n")
    assert blank["OPENAI_API_KEY"] == "" and blank["AI_ASSISTANT_ENABLED"] is False


def test_settings_check_the_reasoning_effort(monkeypatch):
    for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        assert _settings(monkeypatch, AI_ASSISTANT_EFFORT=effort)["AI_ASSISTANT_EFFORT"] == effort
    with pytest.raises(ImproperlyConfigured, match="AI_ASSISTANT_EFFORT"):
        _settings(monkeypatch, AI_ASSISTANT_EFFORT="extreme")


def test_the_effort_values_are_the_sdks():
    """Settings list the values instead of importing the SDK at start-up: they must stay the same."""
    sdk = tuple(v for arg in typing.get_args(ReasoningEffort) for v in typing.get_args(arg) if v is not None)
    assert settings.AI_ASSISTANT_EFFORTS == sdk
    tree = ast.parse(SETTINGS_FILE.read_text(encoding="utf-8"))
    imported = {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not [m for m in imported if m.split(".")[0] == "openai"]


def test_the_example_env_file_works_as_a_env_file(monkeypatch):
    """README: cp .env.example .env. A comment after a value would become part of the value."""
    example = {}
    monkeypatch.setattr(environ.Env, "ENVIRON", example)  # settings read only the example's values
    environ.Env.read_env(str(SETTINGS_FILE.parents[1] / ".env.example"), overwrite=True)
    assert example and not {key: value for key, value in example.items() if "#" in value}
    loaded = _settings(monkeypatch)
    assert loaded["AI_ASSISTANT_EFFORT"] == "medium" and loaded["AI_ASSISTANT_HOURLY_LIMIT"] == 30
    assert loaded["LOG_FORMAT"] == "plain" and loaded["AI_ASSISTANT_ENABLED"] is False


def test_the_app_no_longer_imports_the_anthropic_sdk():
    root = Path(agent.__file__).resolve().parents[2]
    for path in [*(root / "neurodb").rglob("*.py"), *(root / "config").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            assert all(m.split(".")[0] != "anthropic" for m in modules), path
    assert not hasattr(settings, "ANTHROPIC_API_KEY")
    for path in (root / "neurodb" / "assistant" / "templates").rglob("*.html"):
        assert "anthropic" not in path.read_text(encoding="utf-8").lower(), path
