"""The Monitoring insights brief (A) and the action points' AI content summary (D) on Claude, with the real
anthropic SDK against the local stand-in for the Messages API (tests/assistant/anthropic_mock.py): what
each sends, and that each keeps what it keeps on OpenAI (tests/fmm/test_openai_parity.py). No network
access and no real API key."""

import json

import pytest
from django.test import override_settings

from neurodb.assistant import agent, usage
from neurodb.assistant.llm import claude_backend
from neurodb.assistant.models import AIUsage
from neurodb.datamart import models as dm
from neurodb.fmm.ai import ap_summary, facts, insights, profiles, sampling, sections
from neurodb.fmm.models import Insight
from neurodb.watch import people
from tests.assistant import anthropic_mock as am
from tests.assistant.claude_params import assert_claude_params
from tests.fmm.test_insights import AI_ON, TODAY, _good, _scope
from tests.fmm.test_openai_parity import EXPECTED_BRIEF, EXPECTED_SUMMARY
from tests.fmm.test_stage_c import points  # noqa: F401 - the action points the summary reads

pytestmark = pytest.mark.django_db

USED = am.usage(100, 20, read=50)  # the parity shapes' tokens: a prompt of 150, 50 of it cached
# A reply on Claude, and the OpenAI shape whose kept outcome it gives
REPLIES = {
    "ok": ("completed", lambda answer: am.turn([am.thinking("sig-1"), am.text(answer)], usage=USED)),
    "max_tokens": (
        "max_output_tokens",
        lambda answer: am.turn([am.text(answer)], stop_reason="max_tokens", usage=USED),
    ),
    "context_window": (
        "other_reason",
        lambda answer: am.turn([am.text(answer)], stop_reason="model_context_window_exceeded", usage=USED),
    ),
    "refusal": ("refusal", lambda answer: am.refusal("cyber", blocks=[am.text(answer[:10])], usage=USED)),
}
SUMMARY = {
    "themes": [{"name": "Stock and supplies", "count": 1, "example": "LEBA/2026/9001/AP"}],
    "pattern": "One of the action points asks a partner to restock supplies.",
}


def _sent(server, i=0):
    return server.requests[i]["body"], server.requests[i]["headers"]


@pytest.mark.parametrize("reply", list(REPLIES))
def test_the_brief_keeps_on_claude_what_it_keeps_on_openai(built, claude, reply):
    shape, make = REPLIES[reply]
    people.forget()
    with override_settings(**AI_ON):
        version = profiles.published()
        found = facts.build(_scope(), version, TODAY)
        server = claude(make(json.dumps(_good(found))))
        row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)

    kept = [row.status, row.reason, row.called, row.input_tokens, row.cached_tokens, row.output_tokens]
    assert kept == EXPECTED_BRIEF[shape]
    assert (row.model, row.effort) == (am.MODEL, "low")
    (booked,) = AIUsage.objects.filter(feature=usage.FMM)
    assert (booked.model, booked.calls, booked.input_tokens, booked.cached_tokens) == (am.MODEL, 1, 100, 50)
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert body["model"] == am.MODEL  # the version names an OpenAI model: Claude's runs, the version keeps it
    assert body["system"] == [
        {
            "type": "text",
            "text": profiles.compose(version, "insights"),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert json.loads(body["messages"][0]["content"]) == found.payload
    assert body["output_config"] == {
        "effort": "low",
        "format": {
            "type": "json_schema",
            "schema": claude_backend.schema(sections.schema(sections.of(version))),
        },
    }
    assert body["max_tokens"] == version.max_output_tokens and "metadata" not in body  # nobody asked


def test_a_brief_on_claude_sends_no_sampling_and_a_person_only_as_a_hash(built, claude, viewer):
    people.forget()
    with override_settings(**AI_ON):
        found = facts.build(_scope("section="), profiles.published(), TODAY)
        server = claude(am.turn([am.text(json.dumps(_good(found)))], usage=USED))
        row = insights.generate(_scope("section="), user=viewer, trigger=Insight.Trigger.USER, today=TODAY)

    assert row.status == Insight.Status.OK, (row.reason, row.dropped)
    body, _headers = _sent(server)
    assert body["metadata"] == {"user_id": agent.safety_identifier(viewer)}
    assert "temperature" not in body and "top_p" not in body
    # v1 sets temperature 0.30: Claude chooses its own, and the chip says it was not sent
    assert row.sampling["temperature"]["state"] == sampling.NOT_SENT
    assert (
        row.sampling["temperature"]["why"]
        == "Claude chooses its own sampling: temperature is not sent to it."
    )


@pytest.mark.parametrize("reply", ["ok", "max_tokens", "refusal"])
def test_the_action_point_summary_keeps_on_claude_what_it_keeps_on_openai(points, viewer, claude, reply):  # noqa: F811
    shape, make = REPLIES[reply]
    shape = {"refusal": "empty"}.get(reply, shape)  # a declined answer keeps no text: an empty answer
    people.forget()
    server = claude(make(json.dumps(SUMMARY)))
    with override_settings(**AI_ON):
        found = ap_summary.summarise(dm.ActionPoint.objects.all(), viewer)

    kept = json.loads(json.dumps([found.ok, found.message, found.themes, found.pattern, found.points]))
    assert kept == EXPECTED_SUMMARY[shape]
    body, headers = _sent(server)
    assert_claude_params(body, headers)
    assert body["model"] == am.MODEL and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"]["effort"] == "low" and body["max_tokens"] == ap_summary.OUTPUT_TOKENS
    assert "temperature" not in body  # the AI checks' temperature is OpenAI's only
