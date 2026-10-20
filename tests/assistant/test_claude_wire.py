"""The provider layer on Claude with the real anthropic SDK against a local stand-in for the Messages API.

These check what the SDK actually puts on the wire for one call described for either provider (the path,
the key, the beta header and the body, checked against the SDK's own request types by
``assert_claude_params``) and how the final message it builds from the stream is read back: the text,
a refusal, a refusal fallback, a cut stream, the errors and the connection test. No network access and no
real API key.
"""

import base64
import json

import pytest
from django.conf import settings
from django.core.cache import caches

from neurodb.assistant import llm, usage
from neurodb.assistant.llm import claude_backend
from neurodb.assistant.models import AIUsage
from neurodb.web import health
from tests.assistant import anthropic_mock
from tests.assistant.anthropic_mock import (
    MODEL,
    cut_before_start,
    error_body,
    fallback,
    http_error,
    iteration,
    message_json,
    refusal,
    stream_error,
    text,
    thinking,
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
