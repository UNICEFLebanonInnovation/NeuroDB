"""The provider layer on Claude with the real anthropic SDK against a local stand-in for the Messages API.

These check what the SDK actually puts on the wire for one call described for either provider (the path,
the key, the beta header and the body, checked against the SDK's own request types by
``assert_claude_params``) and how the final message it builds from the stream is read back: the text,
a refusal, a refusal fallback, a cut stream, the errors and the connection test. Then Ask NeuroDB's answers
with tools on Claude: the request, the conversation sent back round after round, the events, refusals and
fallbacks, the limits, every way a stream can fail, and NeuroDB Watch's look-up. Last, each feature's own
call (the AI checks, the action point review, the document review, the country programme reading, the daily
review, the What's new note, the Watch morning note, the periodic figures and the document summaries)
driven through its own function: what it sends, and that it keeps what it keeps on OpenAI. No network
access and no real API key.
"""

import base64
import json
from types import SimpleNamespace

import pytest
from anthropic.lib.streaming._beta_messages import BetaMessageStream
from django.conf import settings
from django.core.cache import caches
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, llm, tools, usage, views
from neurodb.assistant.llm import claude_backend
from neurodb.assistant.models import AIUsage, AssistantQuestion
from neurodb.watch import investigate
from neurodb.web import health
from tests.assistant import anthropic_mock
from tests.assistant import test_openai_parity as parity
from tests.assistant.anthropic_mock import (
    MODEL,
    cut_before_start,
    error_body,
    fallback,
    http_error,
    iteration,
    message_json,
    refusal,
    stall,
    stream_error,
    text,
    thinking,
    tool_use,
    turn,
)
from tests.assistant.claude_params import assert_claude_params

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
DECISIONS = {  # the shape of the daily review's decisions: a list cut to five by the code
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "why": {"type": "string"}},
                "required": ["key", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}
FRAMEWORK = {  # the shape of the country programme reading: numbers that may be missing
    "type": "object",
    "properties": {
        "outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "baseline": {"type": ["number", "null"], "description": "the baseline value"},
                    "year": {"type": ["integer", "null"]},
                },
                "required": ["title", "baseline", "year"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["outcomes"],
    "additionalProperties": False,
}


def _sent(server, i=0):
    request = server.requests[i]
    return request["body"], request["headers"]


def test_a_text_call(claude):
    server = claude(
        turn(
            [thinking("sig-1"), text(["Two findings ", "need a decision."])],
            usage=anthropic_mock.usage(80, 30),
        )
    )
    result = llm.complete(llm.Request(system="Write two sentences.", user="{}", max_output_tokens=600))
    body, headers = _sent(server)
    assert server.requests[0]["path"] == "/v1/messages?beta=true"
    assert headers["x-api-key"] == "test-key-not-real"
    assert_claude_params(body, headers)
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 2000  # the floor: thinking counts
    assert body["system"] == [{"type": "text", "text": "Write two sentences."}]  # no cache key: not cached
    assert body["messages"] == [{"role": "user", "content": "{}"}]
    assert body["output_config"] == {"effort": "low"} and "metadata" not in body
    assert body["fallbacks"] == "default" and headers["anthropic-beta"] == claude_backend.FALLBACK_BETA
    assert result.text == "Two findings need a decision." and result.complete and result.parsed is None
    assert result.model == MODEL and result.served_by == "" and result.provider == llm.CLAUDE
    assert result.usage == llm.Usage(
        input_tokens=80, cache_read_tokens=0, cache_write_tokens=0, output_tokens=30
    )


def test_parts_with_a_pdf_and_a_schema_with_type_unions(claude):
    answer = {"outcomes": [{"title": "Learning", "baseline": None, "year": 2026}]}
    server = claude(turn([text(json.dumps(answer))]))
    result = llm.complete(
        llm.Request(
            user=(llm.Pdf("tiny.pdf", PDF), llm.Text("Read the results framework.")),
            schema=FRAMEWORK,
            schema_name="cpd_results_framework",
            effort="medium",
            max_output_tokens=32000,
        )
    )
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert "system" not in body and body["max_tokens"] == 32000
    document, prompt = body["messages"][0]["content"]
    assert document == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(PDF).decode()},
        "title": "tiny.pdf",
    }
    assert prompt == {"type": "text", "text": "Read the results framework."}
    outcome = body["output_config"]["format"]["schema"]["properties"]["outcomes"]["items"]["properties"]
    assert outcome["baseline"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}],
        "description": "the baseline value",
    }
    assert outcome["year"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert "name" not in body["output_config"]["format"] and "strict" not in body["output_config"]["format"]
    assert result.parsed == answer


def test_a_schema_keyword_claude_does_not_take_moves_into_the_description(claude):
    server = claude(turn([text('{"decisions": [{"key": "a", "why": "b"}]}')]))
    result = llm.complete(llm.Request(system="Choose.", user="{}", schema=DECISIONS, schema_name="decisions"))
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    decisions = body["output_config"]["format"]["schema"]["properties"]["decisions"]
    assert "maxItems" not in decisions and "{maxItems: 5}" in decisions["description"]
    assert result.parsed == {"decisions": [{"key": "a", "why": "b"}]}


def test_the_cache_key_marks_the_instructions_and_the_person_goes_as_a_hash(claude):
    server = claude(turn([text("Done.")]))
    llm.complete(
        llm.Request(
            system="Stable instructions.",
            user="The varying facts.",
            cache_key="neurodb-fmm-insights",
            user_id="a" * 64,
            sampling=(("temperature", 0.3), ("top_p", 0.9)),  # never sent to Claude
            effort="none",  # runs as low
        )
    )
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert body["system"] == [
        {"type": "text", "text": "Stable instructions.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["metadata"] == {"user_id": "a" * 64}
    assert body["output_config"]["effort"] == "low"
    assert "cache_control" not in body  # a structured call's tail is unique: no write premium for nothing


def test_a_refusal_keeps_no_text(claude):
    claude(refusal("cyber", blocks=[text("Here is a partial")]))
    result = llm.complete(llm.Request(system="x", user="y", schema=DECISIONS))
    assert result.stop == "refused" and result.reason == "refusal: cyber"
    assert result.text == "" and result.parsed is None and not result.complete


def test_a_fallback_answers_and_both_attempts_count(claude):
    attempts = [
        iteration("message", 50, 10, read=200),
        iteration("fallback_message", 60, 40, model="claude-opus-5", write=30),
    ]
    claude(
        turn(
            [text("The first half, "), fallback(MODEL, "claude-opus-5", "cyber"), text("and the rest.")],
            usage=anthropic_mock.usage(60, 40, write=30, iterations=attempts),
        )
    )
    result = llm.complete(llm.Request(system="x", user="y"))
    assert result.text == "The first half, and the rest."  # the continuation goes on from the partial
    assert result.served_by == "claude-opus-5" and result.ledger_model == "claude-opus-5"
    assert result.model == MODEL and result.complete
    # Anthropic's input_tokens has no cache reads or writes; the writes are prompt tokens not read from it
    assert result.usage == llm.Usage(
        input_tokens=140, cache_read_tokens=200, cache_write_tokens=30, output_tokens=50
    )


@pytest.mark.parametrize(
    "stop, expected",
    [
        ("max_tokens", "max_tokens"),
        ("model_context_window_exceeded", "incomplete"),
        ("pause_turn", "incomplete"),
        ("stop_sequence", "ok"),
    ],
)
def test_how_an_answer_stops(claude, stop, expected):
    claude(turn([text("Partial answer")], stop_reason=stop))
    result = llm.complete(llm.Request(system="x", user="y"))
    assert result.stop == expected and result.reason == stop and result.text == "Partial answer"


@pytest.mark.parametrize("reply", [anthropic_mock.cut(), cut_before_start()], ids=["cut", "cut before start"])
def test_a_stream_that_stops_early_is_a_service_error(claude, reply):
    claude(reply)
    with pytest.raises(llm.ServiceError) as raised:
        llm.complete(llm.Request(system="x", user="y"))
    assert raised.value.code == "stream_ended"
    assert llm.friendly(raised.value) == llm.UNFINISHED


@pytest.mark.parametrize(
    "reply, message, quota",
    [
        (http_error(401, error_body("authentication_error", "invalid x-api-key")), llm.KEY_REJECTED, False),
        (
            http_error(402, error_body("billing_error", "Your credit balance is too low.")),
            llm.NO_CREDIT,
            True,
        ),
        (http_error(403, error_body("permission_error", "Not allowed.")), llm.NOT_ALLOWED, False),
        (http_error(404, error_body("not_found_error", "model: claude-x")), llm.NO_MODEL, False),
        (
            http_error(413, error_body("request_too_large", "Request exceeds the maximum size")),
            llm.NOT_PROCESSED,
            False,
        ),
        (http_error(429, error_body("rate_limit_error", "Too many requests")), llm.BUSY, False),
        (http_error(500, error_body("api_error", "Internal error")), llm.UNAVAILABLE, False),
        (http_error(529, error_body("overloaded_error", "Overloaded")), llm.BUSY, False),
        (stream_error("overloaded_error", "Overloaded"), llm.BUSY, False),
        (stream_error("billing_error", "Your credit balance is too low."), llm.NO_CREDIT, True),
        (stream_error("api_error", "Internal error"), llm.UNAVAILABLE, False),
    ],
)
def test_failures_become_the_messages_every_feature_shows(claude, reply, message, quota):
    claude(reply)
    with pytest.raises(Exception) as raised:
        llm.connect(timeout=5, retries=0).complete(llm.Request(system="x", user="y"))
    assert llm.friendly(raised.value) == message
    assert llm.is_quota(raised.value) is quota


def test_an_account_that_refuses_the_fallback_header_gets_the_call_without_it(claude, db):
    refused = error_body(
        "invalid_request_error",
        f"Unexpected value(s) `{claude_backend.FALLBACK_BETA}` for the `anthropic-beta` header.",
    )
    server = claude(http_error(400, refused), turn([text("Answered.")]), turn([text("Again.")]))
    assert llm.complete(llm.Request(system="x", user="y")).text == "Answered."
    first, second = server.requests[:2]
    assert (
        first["body"]["fallbacks"] == "default"
        and claude_backend.FALLBACK_BETA in first["headers"]["anthropic-beta"]
    )
    assert "fallbacks" not in second["body"] and claude_backend.FALLBACK_BETA not in second["headers"].get(
        "anthropic-beta", ""
    )
    assert claude_backend.fallbacks_refused()
    # for 24 hours the next calls go without it from the start
    assert llm.complete(llm.Request(system="x", user="y")).text == "Again."
    body, headers = _sent(server, 2)
    assert "fallbacks" not in body and claude_backend.FALLBACK_BETA not in headers.get("anthropic-beta", "")
    assert len(server.requests) == 3
    # the administrators see it in Needs attention (and NeuroDB Watch) until the 24 hours are over
    line = {line.key: line for line in health.warnings()}["ai_fallbacks_refused"]
    assert line.text.startswith("This Anthropic account does not accept refusal fallbacks")
    assert line.url.endswith("/assistant/aisettings/")
    caches["shared"].delete(claude_backend.REFUSED_KEY)
    assert "ai_fallbacks_refused" not in {line.key for line in health.warnings()}


def test_another_400_is_not_retried(claude):
    server = claude(
        http_error(
            400, error_body("invalid_request_error", "messages: text content blocks must be non-empty")
        )
    )
    with pytest.raises(Exception) as raised:
        llm.connect(retries=0).complete(llm.Request(system="x", user="y"))
    assert llm.friendly(raised.value) == llm.NOT_PROCESSED and len(server.requests) == 1
    assert not claude_backend.fallbacks_refused()


@pytest.mark.parametrize(
    "model, offered", [("claude-opus-5", True), ("claude-sonnet-5-5", True), ("claude-haiku-5-5", False)]
)
def test_the_fallback_goes_only_to_a_model_that_offers_it(claude, model, offered):
    server = claude(turn([text("Done.")], model=model))
    llm.complete(llm.Request(system="x", user="y", model=model))
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert body["model"] == model and ("fallbacks" in body) is offered


def test_the_fallback_switched_off_in_ai_settings(claude, ai_settings):
    ai_settings.claude_fallbacks = False
    ai_settings.save()
    server = claude(turn([text("Done.")]))
    llm.complete(llm.Request(system="x", user="y"))
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert "fallbacks" not in body and "anthropic-beta" not in headers


def test_a_model_of_the_other_provider_runs_on_claudes_default(claude):
    server = claude(turn([text("Done.")]))
    result = llm.complete(llm.Request(system="x", user="y", model="gpt-5.5"))
    assert _sent(server)[0]["model"] == settings.CLAUDE_MODEL and result.model == settings.CLAUDE_MODEL


# ------------------------------------------------------------------------------ test the connection
def test_the_connection_test_on_claude(claude, db):
    server = claude(message_json([thinking("sig"), text("OK")], usage_=anthropic_mock.usage(12, 16)))
    check = llm.test_connection()
    body, headers = _sent(server)
    assert_claude_params(body, headers, stream=False)
    assert body == {
        "model": "claude-opus-5-5",
        "max_tokens": 16,
        "output_config": {"effort": "low"},
        "messages": [{"role": "user", "content": "Reply with the word OK."}],  # no NeuroDB data
        "fallbacks": "default",
    }
    assert check.ok and check.model == "claude-opus-5-5"
    assert check.text.startswith("Claude (Anthropic) answered with claude-opus-5-5 in ")
    assert check.text.endswith(": the key, the model and the credit work.")
    row = AIUsage.objects.get(feature=usage.TEST)
    assert (row.model, row.calls, row.input_tokens, row.output_tokens) == ("claude-opus-5-5", 1, 12, 16)


def test_a_connection_test_that_fails_says_why(claude, db):
    claude(http_error(401, error_body("authentication_error", "invalid x-api-key")))
    check = llm.test_connection()
    assert not check.ok and check.text == llm.KEY_REJECTED
    assert "test-key-not-real" not in check.text
    assert not AIUsage.objects.exists()


def test_a_refused_fallback_header_shows_in_the_connection_test(claude, db):
    refused = f"Unexpected value(s) `{claude_backend.FALLBACK_BETA}` for the `anthropic-beta` header."
    server = claude(http_error(400, error_body("invalid_request_error", refused)))
    check = llm.test_connection()
    assert not check.ok and len(server.requests) == 1  # no retry without the fallback here
    assert check.text == (
        f"{llm.NOT_PROCESSED} The API said: {refused} Untick Retry a declined answer on another Claude model "
        "and test again."
    )


# ------------------------------------------------------------------------------ Ask NeuroDB on Claude
QUESTION = "Is the data up to date?"
BETA_REFUSED = error_body(
    "invalid_request_error",
    f"Unexpected value(s) `{claude_backend.FALLBACK_BETA}` for the `anthropic-beta` header.",
)


def _answer(question=QUESTION, history=(), user=None, options=None):
    outcome = agent.Outcome()
    events = list(agent.answer(question, list(history), outcome, user=user, options=options))
    return outcome, events


def _ask(client, question=QUESTION):
    """Ask NeuroDB's page streams the answer: its events, as the browser reads them."""
    response = client.post(reverse("assistant:stream"), {"question": question})
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def test_ask_with_one_tool_round(claude, viewer):
    server = claude(
        turn(
            [thinking("sig-1"), text("Checking."), tool_use("toolu_1", "data_freshness", ["{", "}"])],
            stop_reason="tool_use",
            usage=anthropic_mock.usage(120, 30, write=900),
        ),
        turn(
            [thinking("sig-2"), text(["The data is ", "**fresh**."])],
            usage=anthropic_mock.usage(200, 12, read=900),
        ),
    )

    outcome, events = _answer(user=viewer)

    assert events[:2] == [
        {"type": "text", "text": "Checking.", "round": 1},
        {"type": "tool", "label": "Checking data freshness", "round": 1},
    ]
    assert [e["type"] for e in events] == ["text", "tool", "text", "text", "done"]
    assert events[-1]["answer"] == "The data is **fresh**." and "<strong>fresh</strong>" in events[-1]["html"]
    assert outcome.status == "answered" and outcome.model == MODEL and outcome.calls == 2
    assert outcome.tools[0]["tool"] == "data_freshness" and outcome.tools[0]["ok"]
    # input_tokens + cache_read_tokens is the whole prompt: 120 + 900 written, then 200 + 900 read
    assert (outcome.input_tokens, outcome.cache_read_tokens, outcome.cache_write_tokens) == (1220, 900, 900)
    assert outcome.output_tokens == 42

    first, second = server.requests
    body = first["body"]
    assert_claude_params(body, first["headers"])
    assert body["model"] == MODEL and body["max_tokens"] == agent.MAX_OUTPUT_TOKENS
    # the tools, then Ask's instructions, cached across people; the date after them
    assert body["system"][0] == {
        "type": "text",
        "text": agent.SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }
    assert "cache_control" not in body["system"][1]
    assert "".join(block["text"] for block in body["system"]) == agent._instructions()
    assert body["tools"] == tools.claude_definitions()
    assert body["cache_control"] == {"type": "ephemeral"}  # the conversation is cached as it grows
    assert body["output_config"] == {"effort": "medium"}
    assert body["metadata"] == {"user_id": agent.safety_identifier(viewer)}
    assert body["messages"] == [{"role": "user", "content": QUESTION}]
    assert body["fallbacks"] == "default"

    # round 2: the same prefix, Claude's whole turn as it wrote it, then one message with the results
    again = second["body"]
    assert_claude_params(again, second["headers"])
    assert again["system"] == body["system"] and again["tools"] == body["tools"]
    question, said, results = again["messages"]
    assert question == {"role": "user", "content": QUESTION}
    assert said == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "data_freshness", "input": {}},
        ],
    }
    (result,) = results["content"]
    assert (
        results["role"] == "user" and result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    )
    assert "is_error" not in result and json.loads(result["content"])["url"] == "/data/health/"


def test_parallel_lookups_get_one_message_of_results_and_the_text_keeps_its_breaks(claude, db):
    server = claude(
        turn(
            [
                text("Let me check."),
                text("Two lookups."),
                tool_use("toolu_a", "data_freshness", "{}"),
                tool_use("toolu_b", "no_such_tool", "{}"),
            ],
            stop_reason="tool_use",
        ),
        turn([text("First part."), thinking("sig"), text(["Second ", "part."])]),
    )

    outcome, events = _answer()

    assert events == [
        {"type": "text", "text": "Let me check.", "round": 1},
        {"type": "text", "text": "\n\n", "round": 1},
        {"type": "text", "text": "Two lookups.", "round": 1},
        {"type": "tool", "label": "Checking data freshness", "round": 1},
        {"type": "tool", "label": "Looking up data", "round": 1},
        {"type": "text", "text": "First part.", "round": 2},
        {"type": "text", "text": "\n\n", "round": 2},
        {"type": "text", "text": "Second ", "round": 2},
        {"type": "text", "text": "part.", "round": 2},
        {
            "type": "done",
            "answer": "First part.\n\nSecond part.",
            "html": agent.render("First part.\n\nSecond part."),
        },
    ]
    body = server.requests[1]["body"]
    assert_claude_params(body, server.requests[1]["headers"])
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    found, unknown = body["messages"][2]["content"]  # every result in one message, in the calls' order
    assert found["tool_use_id"] == "toolu_a" and "is_error" not in found
    assert unknown["tool_use_id"] == "toolu_b" and unknown["is_error"] is True
    assert json.loads(unknown["content"])["error"] == "Unknown tool no_such_tool."
    assert [(t["tool"], t["ok"]) for t in outcome.tools] == [
        ("data_freshness", True),
        ("no_such_tool", False),
    ]


def test_a_refusal_before_any_output(claude, db):
    server = claude(refusal("cyber"))
    outcome, events = _answer()
    assert events == [{"type": "error", "message": agent.REFUSED, "discard": True}]
    assert outcome.status == "refused" and outcome.error == "refusal: cyber"
    assert outcome.tools == [] and outcome.calls == 1 and len(server.requests) == 1


def test_a_refusal_after_streamed_text_takes_it_back_and_runs_nothing(claude, db):
    claude(refusal("bio", blocks=[text("Here is a partial "), tool_use("toolu_1", "data_freshness", "{}")]))
    outcome, events = _answer()
    assert events == [
        {"type": "text", "text": "Here is a partial ", "round": 1},
        {"type": "error", "message": agent.REFUSED, "discard": True},  # the page takes the partial away
    ]
    assert outcome.status == "refused" and outcome.error == "refusal: bio"
    assert outcome.tools == [] and outcome.answer == ""


def test_a_fallback_in_the_middle_of_a_round(claude, db):
    attempts = [iteration("message", 50, 10), iteration("fallback_message", 60, 40, model="claude-opus-5")]
    server = claude(
        turn(
            [
                thinking("sig-declined"),
                text("The first half, "),
                tool_use("toolu_x", "data_freshness", "{}"),
                fallback(MODEL, "claude-opus-5", "cyber"),
                thinking("sig-fallback"),
                text("and the rest."),
                tool_use("toolu_2", "data_freshness", "{}"),
            ],
            stop_reason="tool_use",
            usage=anthropic_mock.usage(60, 40, iterations=attempts),
        ),
        turn([text("Fresh.")], usage=anthropic_mock.usage(300, 5)),
    )

    outcome, events = _answer()

    # the streamed partial and the continuation make the text, with no break between them
    assert [e["text"] for e in events if e["type"] == "text"] == [
        "The first half, ",
        "and the rest.",
        "Fresh.",
    ]
    assert [e["type"] for e in events].count("tool") == 1  # the declined attempt's call never runs
    assert outcome.status == "answered" and outcome.served_by == "claude-opus-5"
    # each attempt's tokens count, under the model that ran them; each call under the model that wrote it
    assert outcome.by_model == {
        MODEL: llm.Usage(input_tokens=350, output_tokens=15),
        "claude-opus-5": llm.Usage(input_tokens=60, output_tokens=40),
    }
    assert outcome.model_calls == {"claude-opus-5": 1, MODEL: 1}
    assert (outcome.input_tokens, outcome.output_tokens) == (410, 55)
    # the replay leaves out the declined attempt's thinking and tool call, and the fallback marker
    body = server.requests[1]["body"]
    assert_claude_params(body, server.requests[1]["headers"])
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "The first half, "},
        {"type": "thinking", "thinking": "", "signature": "sig-fallback"},
        {"type": "text", "text": "and the rest."},
        {"type": "tool_use", "id": "toolu_2", "name": "data_freshness", "input": {}},
    ]
    assert [r["tool_use_id"] for r in body["messages"][2]["content"]] == ["toolu_2"]


def test_an_answer_cut_at_its_token_limit_runs_no_tool(claude, db):
    claude(
        turn([text("Let me "), tool_use("toolu_1", "data_freshness", '{"x": 1')], stop_reason="max_tokens")
    )
    outcome, events = _answer()
    assert events[-1] == {"type": "error", "message": agent.TOO_LONG}
    assert outcome.status == "failed" and outcome.error == "max_output_tokens" and outcome.tools == []


def test_an_answer_past_the_context_window(claude, db):
    claude(turn([text("Partial")], stop_reason="model_context_window_exceeded"))
    outcome, events = _answer()
    assert events[-1] == {"type": "error", "message": agent.NOT_COMPLETED}
    assert outcome.error == "incomplete: model_context_window_exceeded"


@pytest.mark.parametrize(
    "reply, message, logged",
    [
        (
            stream_error("overloaded_error", "Overloaded", blocks=[text("Partial")]),
            llm.BUSY,
            "APIStatusError",
        ),
        (stream_error("billing_error", "Your credit balance is too low."), llm.NO_CREDIT, "APIStatusError"),
        (
            http_error(401, error_body("authentication_error", "invalid x-api-key")),
            llm.KEY_REJECTED,
            "AuthenticationError",
        ),
        (
            http_error(402, error_body("billing_error", "Your credit balance is too low.")),
            llm.NO_CREDIT,
            "APIStatusError",
        ),
        (
            http_error(403, error_body("permission_error", "Not allowed.")),
            llm.NOT_ALLOWED,
            "PermissionDeniedError",
        ),
        (http_error(404, error_body("not_found_error", "model: claude-x")), llm.NO_MODEL, "NotFoundError"),
        (
            http_error(413, error_body("request_too_large", "Request exceeds the maximum size")),
            llm.NOT_PROCESSED,
            "RequestTooLargeError",
        ),
        (anthropic_mock.cut(), llm.UNFINISHED, "ServiceError: stream_ended"),
        (cut_before_start(), llm.UNFINISHED, "ServiceError: stream_ended"),
        # a network failure while the answer streams, which the SDK does not wrap
        (
            stall(turn([text("Half an answer")], end="cut"), seconds=0.2, drop=True),
            llm.UNFINISHED,
            "ServiceError: stream_cut",
        ),
    ],
    ids=["overloaded", "billing", "401", "402", "403", "404", "413", "cut", "cut-before-start", "dropped"],
)
def test_a_failed_answer_shows_what_went_wrong(claude, client_viewer, reply, message, logged):
    claude(reply)
    events = _ask(client_viewer)
    assert events[-1] == {"type": "error", "message": message}
    assert not any("Something went wrong" in json.dumps(event) for event in events)
    row = AssistantQuestion.objects.get()
    assert row.status == "failed" and row.model == MODEL and row.error.startswith(logged)
    assert "test-key-not-real" not in row.error


def test_an_answer_that_goes_silent_did_not_answer_in_time(claude, client_viewer, monkeypatch):
    monkeypatch.setattr(agent, "READ_TIMEOUT", 0.5)  # the longest silence a call waits for
    monkeypatch.setattr(agent, "CONNECT_TIMEOUT", 0.5)
    claude(stall(turn([text("Thinking about it")], end="cut"), seconds=3))
    events = _ask(client_viewer)
    assert events[-1] == {"type": "error", "message": llm.LATE}
    assert AssistantQuestion.objects.get().error.startswith("ServiceError: timeout")


BROKEN = ['{"a": ', "]]"]  # tool input the SDK cannot read at all (it raises from the stream)


def test_unreadable_tool_input_is_sent_again_once_as_a_new_round(claude, db):
    server = claude(
        turn([text("Checking."), tool_use("toolu_1", "data_freshness", BROKEN)], stop_reason="tool_use"),
        turn([text("Fresh.")]),
    )
    outcome, events = _answer()
    # the page starts the text afresh: the second attempt is round 2
    assert [e for e in events if e["type"] == "text"] == [
        {"type": "text", "text": "Checking.", "round": 1},
        {"type": "text", "text": "Fresh.", "round": 2},
    ]
    assert events[-1]["type"] == "done" and outcome.answer == "Fresh." and outcome.tools == []
    assert server.requests[1]["body"]["messages"] == [{"role": "user", "content": QUESTION}]
    assert outcome.calls == 1  # the broken attempt left no message, nor tokens to count


def test_unreadable_tool_input_twice_could_not_finish(claude, client_viewer):
    broken = tool_use("toolu_1", "data_freshness", BROKEN)
    server = claude(turn([broken], stop_reason="tool_use"), turn([broken], stop_reason="tool_use"))
    events = _ask(client_viewer)
    assert events[-1] == {"type": "error", "message": llm.UNFINISHED} and len(server.requests) == 2
    assert AssistantQuestion.objects.get().error.startswith("ServiceError: tool_input")


def test_a_text_block_with_no_text_is_not_sent_back(claude, db):
    server = claude(
        turn(
            [thinking("sig-1"), text(""), tool_use("toolu_1", "data_freshness", "{}")], stop_reason="tool_use"
        ),
        turn([text("Fresh.")]),
    )
    _outcome, events = _answer()
    assert [e for e in events if e["type"] == "text"] == [{"type": "text", "text": "Fresh.", "round": 2}]
    body = server.requests[1]["body"]
    assert_claude_params(body, server.requests[1]["headers"])  # an empty text block would be a 400
    assert body["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "tool_use", "id": "toolu_1", "name": "data_freshness", "input": {}},
    ]


def test_an_account_that_refuses_the_fallback_header_gets_its_answer_without_it(claude, db):
    server = claude(
        http_error(400, BETA_REFUSED),
        turn([tool_use("toolu_1", "data_freshness", "{}")], stop_reason="tool_use"),
        turn([text("Fresh.")]),
    )
    outcome, events = _answer()
    assert events[-1]["type"] == "done" and len(server.requests) == 3
    first, *rest = server.requests
    assert first["body"]["fallbacks"] == "default"
    for request in rest:  # the round sent again, and the next round too, without the pair
        assert "fallbacks" not in request["body"]
        assert claude_backend.FALLBACK_BETA not in request["headers"].get("anthropic-beta", "")
        assert_claude_params(request["body"], request["headers"])
    assert claude_backend.fallbacks_refused()
    caches["shared"].delete(claude_backend.REFUSED_KEY)


def test_a_stopped_answer_closes_claudes_stream(claude, rf, viewer, monkeypatch):
    closed = []
    close = BetaMessageStream.close

    def closing(stream):
        close(stream)
        closed.append(stream.response.is_closed)

    monkeypatch.setattr(BetaMessageStream, "close", closing)
    claude(stall(turn([text("A long answer that the user stops.")], end="cut"), seconds=5))
    request = rf.post("/ask/stream/")
    request.user = viewer
    row = AssistantQuestion.objects.create(
        user=viewer, question="Something long", status="failed", error=views.IN_PROGRESS
    )
    stream = views._stream(request, row)
    next(stream)  # the first words arrive, then the browser goes away and the server closes the stream
    stream.close()
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == "stopped"
    assert closed == [True]  # Claude's stream, and its HTTP response, were closed too


def test_the_watch_look_up_on_claude(claude, db):
    returned = []
    options = investigate.run_options(returned, frozenset())
    server = claude(turn([text('{"what_i_found": "Nothing new.", "numbers": []}')]))

    outcome, events = _answer("The point, as JSON", options=options)

    assert events[-1]["type"] == "done" and outcome.status == "answered"
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert body["tools"] == tools.claude_definitions(investigate.TOOLS)
    assert body["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": claude_backend.schema(investigate.FORMAT["schema"])},
    }
    assert "metadata" not in body  # no person asked
    assert body["max_tokens"] == investigate.MAX_OUTPUT_TOKENS
    assert "".join(block["text"] for block in body["system"]) == agent.effective_instructions(options)
    assert body["system"][1]["text"].endswith(investigate.INSTRUCTIONS.strip())
    assert body["model"] == settings.CLAUDE_MODEL  # WATCH_MODEL names an OpenAI model: Claude's default runs


def test_blank_earlier_turns_are_never_sent_to_claude(claude):
    history = [
        {"question": "Question 1?", "answer": "Answer 1."},
        {"question": "  ", "answer": "An answer to nothing."},  # an empty text is refused
        {"question": "Question 3?", "answer": "\n"},
        {"question": "Question 4?", "answer": "Answer 4."},
    ]
    assert agent._claude_history(history) == [
        {"role": "user", "content": "Question 1?"},
        {"role": "assistant", "content": "Answer 1."},
        {"role": "user", "content": "Question 4?"},
        {"role": "assistant", "content": "Answer 4."},
    ]


def test_a_run_with_an_empty_prompt_of_its_own_sends_one_system_block(claude, db):
    params = agent._claude_request(None, agent.RunOptions(base_prompt="", instructions="Look."))
    (block,) = params["system"]
    assert block["text"].startswith("\nToday is ") and block["text"].endswith("\n\nLook.")
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in params and "metadata" not in params  # no tools offered, no person asked


# ------------------------------------------------------------------------- each feature's own call
def _framework_from_a_pdf(api):
    from neurodb.cpd import extraction

    return extraction.extract(SimpleNamespace(filename="tiny.pdf", file=ContentFile(PDF)))


# Each feature's call as the OpenAI parity tests make it (tests/assistant/test_openai_parity.py), the country
# programme reading with a PDF, sent whole
CALLERS = {caller: call for caller, (call, _answer) in parity.ANSWERS.items()}
CALLERS["G_cpd"] = _framework_from_a_pdf
# What each call sends (§3 of the design): its instructions marked for caching (None: it has none), the
# effort and max_tokens (at least 2,000: the thinking counts too)
SENT = {
    "B_checks": (True, "low", 2000),
    "C_ap_review": (True, "low", 2000),
    "E_doc_review": (True, "low", 8000),
    "G_cpd": (None, "medium", 32000),
    "H1_narration": (False, "low", 2000),
    "H2_decisions": (False, "low", 2000),
    "I_whats_new": (False, "low", 2000),
    "J_watch_note": (True, "low", 2000),
    "M_figures": (None, "medium", 32000),
    "N_summary": (None, "medium", 8000),
}
USED = anthropic_mock.usage(100, 20, read=50)  # the parity shapes' tokens: a prompt of 150, 50 of it cached
# A reply on Claude, and the OpenAI shape whose kept outcome it gives: an answer cut at its limit is read as
# OpenAI's, and a declined one, which keeps no text, as an empty answer
REPLIES = {
    "ok": ("completed", lambda answer: turn([thinking("sig-1"), text(answer)], usage=USED)),
    "max_tokens": (
        "max_output_tokens",
        lambda answer: turn([text(answer)], stop_reason="max_tokens", usage=USED),
    ),
    "refusal": ("empty", lambda answer: refusal("cyber", blocks=[text(answer[:10])], usage=USED)),
}


def _on_claude(value):
    """An outcome recorded on OpenAI, with the model that wrote it on Claude."""
    if isinstance(value, list):
        return [_on_claude(item) for item in value]
    if isinstance(value, dict):
        return {key: _on_claude(item) for key, item in value.items()}
    return MODEL if value == "gpt-5.5" else value


@pytest.mark.django_db
@pytest.mark.parametrize("reply", list(REPLIES))
@pytest.mark.parametrize("caller", list(CALLERS))
def test_each_feature_keeps_on_claude_what_it_keeps_on_openai(claude, caller, reply):
    shape, make = REPLIES[reply]
    answer = parity._answer(caller)
    server = claude(make(answer if isinstance(answer, str) else json.dumps(answer)))

    outcome = json.loads(json.dumps(parity.outcome_of(lambda: CALLERS[caller](None)), default=str))

    assert outcome == _on_claude(parity.EXPECTED[caller][shape])
    (booked,) = AIUsage.objects.all()  # one row, of the model that ran the call
    assert (booked.model, booked.calls) == (MODEL, 1)
    assert (booked.input_tokens, booked.cached_tokens, booked.output_tokens) == (100, 50, 20)
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    cached, effort, most = SENT[caller]
    assert body["model"] == MODEL  # a feature that names an OpenAI model runs on Claude's
    assert body["output_config"]["effort"] == effort and body["max_tokens"] == most
    if cached is None:
        assert "system" not in body
    else:
        assert ("cache_control" in body["system"][0]) is cached
    assert "cache_control" not in body and "metadata" not in body  # a one-off call; no person asked


def test_the_country_programme_goes_whole_and_its_numbers_may_be_missing(claude, db):
    from neurodb.cpd import extraction

    server = claude(turn([text(json.dumps(parity._answer("G_cpd")))]))
    _framework_from_a_pdf(None)
    body, _headers = _sent(server)
    document, prompt = body["messages"][0]["content"]
    assert document["type"] == "document" and document["title"] == "tiny.pdf"
    assert base64.b64decode(document["source"]["data"]) == PDF
    assert prompt == {"type": "text", "text": extraction.PROMPT}
    outcome = body["output_config"]["format"]["schema"]["properties"]["outcomes"]["items"]["properties"]
    baseline = outcome["indicators"]["items"]["properties"]["baseline"]
    assert baseline == {"anyOf": [{"type": "number"}, {"type": "null"}]}


@pytest.mark.parametrize(
    "caller, key, limit",
    [("H2_decisions", "decisions", "{maxItems: 5}"), ("J_watch_note", "sentences", "{maxItems: 6}")],
)
def test_a_list_limit_claude_does_not_take_is_said_in_words_and_kept_by_the_code(
    claude, db, caller, key, limit
):
    server = claude(turn([text(json.dumps(parity._answer(caller)))]))
    parity.outcome_of(lambda: CALLERS[caller](None))
    body, _headers = _sent(server)
    listed = body["output_config"]["format"]["schema"]["properties"][key]
    assert "maxItems" not in listed and limit in listed["description"]


def test_a_feature_with_no_key_for_claude_is_not_set_up(claude, db):
    from neurodb.cpd import extraction

    with override_settings(ANTHROPIC_API_KEY=""):
        with pytest.raises(extraction.ExtractionError) as raised:
            _framework_from_a_pdf(None)
    assert str(raised.value) == "The AI assistant is not configured: enter an API key in AI settings."


def test_a_document_too_large_for_claude_says_to_import_the_framework(claude, db):
    from neurodb.cpd import extraction

    claude(http_error(413, error_body("request_too_large", "Request exceeds the maximum size")))
    with pytest.raises(extraction.ExtractionError) as raised:
        _framework_from_a_pdf(None)
    assert str(raised.value) == extraction.TOO_LARGE


def test_a_call_answered_after_a_refusal_fallback_books_each_model_its_own_attempt(claude, db):
    attempts = [
        iteration("message", 50, 10, read=200),
        iteration("fallback_message", 60, 40, model="claude-opus-5", write=30),
    ]
    claude(
        turn(
            [text("The first half, "), fallback(MODEL, "claude-opus-5", "cyber"), text("and the rest.")],
            usage=anthropic_mock.usage(60, 40, write=30, iterations=attempts),
        )
    )
    result = llm.complete(llm.Request(system="x", user="y"))
    assert result.by_model == {
        MODEL: llm.Usage(input_tokens=50, cache_read_tokens=200, output_tokens=10),
        "claude-opus-5": llm.Usage(input_tokens=90, cache_write_tokens=30, output_tokens=40),
    }
    assert result.model_calls == {"claude-opus-5": 1}  # the model that wrote the answer
    llm.record(usage.REVIEW, result.model, result)
    rows = {row.model: row for row in AIUsage.objects.filter(feature=usage.REVIEW)}
    assert {
        name: (row.calls, row.input_tokens, row.cached_tokens, row.output_tokens)
        for name, row in rows.items()
    } == {
        MODEL: (0, 50, 200, 10),  # the declined attempt, at the prices of the model asked for
        "claude-opus-5": (1, 90, 0, 40),
    }
