"""The assistant with the real openai SDK against a local stand-in for the Responses API.

The scripted-client tests cover the loop; these check what the SDK actually puts on the wire (path,
key, request body, the output items replayed from the SDK's own response objects and the tool
results) and that the SSE stream it parses drives the loop and the error handling, without network
access or an API key.
"""

import json
import uuid

import pytest
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, tools, views
from neurodb.assistant.models import AssistantQuestion
from tests.assistant.openai_mock import (
    MockResponsesServer,
    api_error_body,
    function_call,
    http_error,
    message,
    reasoning,
    refusal,
    turn,
    usage,
)

ENABLED = {
    "AI_ASSISTANT_ENABLED": True,
    "OPENAI_API_KEY": "test-key-not-real",
    "AI_ASSISTANT_MODEL": "gpt-5.5",
    "AI_ASSISTANT_EFFORT": "medium",
}
QUESTION = "Is the data up to date?"


@pytest.fixture
def openai_api(monkeypatch):
    """Point the SDK (through OPENAI_BASE_URL, as agent.client() builds a fresh client) at a local
    stand-in that plays back the given replies."""
    for var in ("NO_PROXY", "no_proxy"):  # reach the local stand-in directly even behind a proxy
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    for var in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"):
        monkeypatch.delenv(var, raising=False)
    servers = []

    def install(*replies):
        server = MockResponsesServer(list(replies)).start()
        servers.append(server)
        monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
        return server

    yield install
    for server in servers:
        server.stop()


def _keys(value):
    """Every dict key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_real_sdk_round_trip(openai_api, viewer):
    server = openai_api(
        turn(
            [
                reasoning("rs_1", "enc-1-not-real", summary=["Check the import log"]),
                message("msg_1", "Checking.", phase="commentary"),
                # "async" and an unknown field, as a newer API might add: neither may be sent back
                function_call(
                    "fc_1", "call_A", "data_freshness", ["{", "}"], **{"async": False, "future_field": 1}
                ),
            ],
            usage=usage(120, 30, cached=80, reasoning_tokens=12),
        ),
        turn(
            [
                reasoning("rs_2", "enc-2-not-real"),
                message("msg_2", ["The data is ", "**fresh**."], phase="final_answer"),
            ],
            usage=usage(200, 12, cached=150),
        ),
    )
    with override_settings(**ENABLED):
        outcome = agent.Outcome()
        events = list(agent.answer(QUESTION, [], outcome, user=viewer))

    assert events[:2] == [
        {"type": "text", "text": "Checking.", "round": 1},
        {"type": "tool", "label": "Checking data freshness", "round": 1},
    ]
    assert [e["type"] for e in events] == ["text", "tool", "text", "text", "done"]
    assert events[-1]["answer"] == "The data is **fresh**."
    assert "<strong>fresh</strong>" in events[-1]["html"]
    assert outcome.status == "answered" and outcome.model == "gpt-5.5"
    assert outcome.tools[0]["tool"] == "data_freshness" and outcome.tools[0]["ok"]
    # input_tokens holds the uncached part: (120 - 80) + (200 - 150)
    assert outcome.input_tokens == 90 and outcome.cache_read_tokens == 230 and outcome.output_tokens == 42

    first, second = server.requests
    assert first["path"] == second["path"] == "/v1/responses"
    assert first["headers"]["authorization"] == "Bearer test-key-not-real"
    assert "x-api-key" not in first["headers"]
    body = first["body"]
    assert body["model"] == "gpt-5.5" and body["stream"] is True
    assert body["store"] is False and body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "medium"} and body["parallel_tool_calls"] is True
    assert body["tools"] == tools.definitions()
    assert all(t["type"] == "function" and t["strict"] is False for t in body["tools"])
    assert body["input"] == [{"role": "user", "content": QUESTION}]
    assert body["safety_identifier"] == agent.safety_identifier(viewer)
    assert "Today is" in body["instructions"]

    # turn 2 replays turn 1's output items (built from the SDK's objects) and answers the call
    *replayed, result = second["body"]["input"]
    assert replayed == [
        {"role": "user", "content": QUESTION},
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "Check the import log"}],
            "encrypted_content": "enc-1-not-real",
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_1",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Checking.", "annotations": []}],
            "phase": "commentary",
        },
        {
            "type": "function_call",
            "call_id": "call_A",
            "name": "data_freshness",
            "arguments": "{}",
            "id": "fc_1",
        },
    ]
    assert result["type"] == "function_call_output" and result["call_id"] == "call_A"
    assert json.loads(result["output"])["url"] == "/data/health/"
    # nothing that is only a field of the SDK's Python objects goes back to the API
    for sent in (first["body"], second["body"]):
        assert not _keys(sent) & {"parsed", "parsed_arguments", "async", "async_", "future_field"}


def _events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def _ask(client, question, conversation=None):
    data = {"question": question}
    if conversation:
        data["conversation"] = str(conversation)
    return client.post(reverse("assistant:stream"), data)


@override_settings(**ENABLED)
def test_progress_notes_are_not_part_of_the_answer(client_viewer, openai_api):
    conversation = uuid.uuid4()
    final = "There are **12** databases."
    server = openai_api(
        turn(
            [
                reasoning("rs_1"),
                message("msg_1", "I have the figures; summarising them now.", phase="commentary"),
                message("msg_2", ["There are ", "**12** databases."], phase="final_answer"),
            ]
        ),
        turn([message("msg_3", "Still 12.", phase="final_answer")]),
    )

    events = _events(_ask(client_viewer, QUESTION, conversation))

    # while streaming, the note and the answer are kept apart; the answer itself is only the answer
    live = "".join(e["text"] for e in events if e["type"] == "text")
    assert live == "I have the figures; summarising them now.\n\n" + final
    assert events[-1]["type"] == "done" and events[-1]["answer"] == final
    assert "summarising" not in events[-1]["html"]
    assert AssistantQuestion.objects.get().answer == final
    # a follow-up question sends only the answer back, as the final answer
    _events(_ask(client_viewer, "And now?", conversation))
    assert server.requests[1]["body"]["input"] == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": final, "phase": "final_answer"},
        {"role": "user", "content": "And now?"},
    ]


@pytest.mark.parametrize("phase", ["commentary", None], ids=["commentary-only", "no-phase"])
def test_without_a_final_answer_every_message_counts(db, openai_api, phase):
    openai_api(turn([reasoning("rs_1"), message("msg_1", "There are 12 databases.", phase=phase)]))
    with override_settings(**ENABLED):
        outcome = agent.Outcome()
        events = list(agent.answer(QUESTION, [], outcome))
    assert events[-1]["type"] == "done" and outcome.answer == "There are 12 databases."


BUSY = views.BUSY
UNFINISHED = "The AI service could not finish this answer. Please try again."


@pytest.mark.parametrize(
    "reply, message_, status, error",
    [
        (
            turn([message("msg_1", "Cut ")], end="incomplete"),
            "The answer was too long. Try a narrower question.",
            "failed",
            "max_output_tokens",
        ),
        (
            turn([message("msg_1", "No")], end="incomplete", incomplete_reason="content_filter"),
            agent.REFUSED,
            "refused",
            "content_filter",
        ),
        (turn([refusal("msg_1", ["I can't ", "help with that."])]), agent.REFUSED, "refused", ""),
        (turn([message("msg_1", "x")], end="failed"), UNFINISHED, "failed", "ServiceError: server_error"),
        (
            turn(
                [message("msg_1", "x")],
                end="error",
                error_code="rate_limit_exceeded",
                error_message="Slow down",
            ),
            BUSY,  # the same message as an HTTP 429
            "failed",
            "ServiceError: rate_limit_exceeded",
        ),
        (
            turn(
                [message("msg_1", "x")], end="error", error_code="insufficient_quota", error_message="Quota"
            ),
            views.NO_CREDIT,
            "failed",
            "ServiceError: insufficient_quota",
        ),
        # OpenAI's safety checks: declined (asking again will not help), not a failure
        (
            turn([message("msg_1", "x")], end="failed", error_code="bio_policy", error_message="Blocked"),
            agent.REFUSED,
            "refused",
            "bio_policy: Blocked",
        ),
        (
            turn([message("msg_1", "x")], end="failed", error_code="invalid_prompt", error_message="Flagged"),
            agent.REFUSED,
            "refused",
            "invalid_prompt: Flagged",
        ),
        (
            turn(
                [message("msg_1", "x")],
                end="error",
                error_code="misalignment_policy_violation",
                error_message="Blocked",
            ),
            agent.REFUSED,
            "refused",
            "misalignment_policy_violation: Blocked",
        ),
        (
            http_error(400, api_error_body("Invalid prompt: flagged.", code="invalid_prompt")),
            agent.REFUSED,
            "refused",
            "invalid_prompt: ",
        ),
        (
            http_error(403, api_error_body("Blocked.", code="misalignment_policy_violation")),
            agent.REFUSED,
            "refused",
            "misalignment_policy_violation: ",
        ),
        (turn([message("msg_1", "x")], end="cut"), UNFINISHED, "failed", "ServiceError: stream_ended"),
        (
            turn([message("msg_1", "x")], end="legacy_error"),
            "The AI service is temporarily unavailable. Please try again.",
            "failed",
            "APIError",
        ),
        (
            http_error(
                429, api_error_body("Rate limit reached.", type_="requests", code="rate_limit_exceeded")
            ),
            BUSY,
            "failed",
            "RateLimitError",
        ),
        (
            http_error(
                429,
                api_error_body(
                    "You exceeded your current quota.", type_="insufficient_quota", code="insufficient_quota"
                ),
            ),
            "The AI service's credit for this application has run out. Ask an administrator.",
            "failed",
            "RateLimitError",
        ),
        (
            http_error(401, api_error_body("Incorrect API key provided.", code="invalid_api_key")),
            "The AI assistant's API key was rejected. Ask an administrator.",
            "failed",
            "AuthenticationError",
        ),
        (
            http_error(404, api_error_body("The model `gpt-5.5` does not exist.", code="model_not_found")),
            "The AI assistant's model is not available. Ask an administrator.",
            "failed",
            "NotFoundError",
        ),
        (
            http_error(
                400, api_error_body("Unknown parameter: 'input[1].parsed'.", code="unknown_parameter")
            ),
            "The AI service could not process this question.",
            "failed",
            "BadRequestError",
        ),
    ],
    ids=[
        "too-long",
        "content-filter",
        "refusal",
        "response-failed",
        "error-event",
        "error-event-no-credit",
        "failed-bio-policy",
        "failed-invalid-prompt",
        "error-event-misalignment",
        "http-invalid-prompt",
        "http-misalignment",
        "stream-cut",
        "error-payload",
        "rate-limit",
        "no-credit",
        "bad-key",
        "unknown-model",
        "bad-request",
    ],
)
@override_settings(**ENABLED)
def test_real_sdk_failures_become_friendly_messages(
    client_viewer, openai_api, reply, message_, status, error
):
    server = openai_api(reply)

    body = b"".join(client_viewer.post(reverse("assistant:stream"), {"question": QUESTION}).streaming_content)
    events = [json.loads(chunk[6:]) for chunk in body.decode().split("\n\n") if chunk.startswith("data: ")]

    assert events[-1] == {"type": "error", "message": message_}
    assert "done" not in [e["type"] for e in events]
    assert len(server.requests) == 1  # no retry and no tool round
    logged = AssistantQuestion.objects.get()
    assert logged.status == status and logged.error.startswith(error) and logged.answer == ""


@override_settings(**ENABLED)
def test_real_sdk_stream_stopped_by_the_browser(rf, viewer, openai_api):
    from neurodb.assistant.views import _stream

    openai_api(turn([message("msg_1", ["A ", "long ", "answer ", "that ", "the ", "user ", "stops."])]))
    request = rf.post("/ask/stream/")
    request.user = viewer
    row = AssistantQuestion.objects.create(
        user=viewer, question="Something long", status="failed", error=views.IN_PROGRESS
    )
    stream = _stream(request, row)
    assert json.loads(next(stream)[6:]) == {"type": "text", "text": "A ", "round": 1}
    stream.close()  # closes the SDK stream inside the loop without raising
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == "stopped"


@override_settings(**ENABLED)
def test_real_sdk_error_event_before_the_response_starts(client_viewer, openai_api, caplog):
    # the SDK's stream helper raises RuntimeError for it while iterating (not only at the end)
    openai_api(
        [{"type": "error", "code": "server_error", "message": "Boom", "param": None, "sequence_number": 0}]
    )
    events = _events(_ask(client_viewer, QUESTION))
    assert events == [{"type": "error", "message": UNFINISHED}]
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.answer == ""
    assert logged.error.startswith("ServiceError: stream_error: Expected to have received `response.created`")
    assert "AI assistant failed" not in caplog.text  # a known failure, not an unexpected exception
