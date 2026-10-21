"""The document review's theme paragraph (F) on Claude, with the real anthropic SDK against the local
stand-in for the Messages API (tests/assistant/anthropic_mock.py): what it sends, and that it keeps what it
keeps on OpenAI (tests/knowledge/test_openai_parity.py). No network access and no real API key."""

import json

import pytest
from django.test import override_settings

from neurodb.knowledge import review_paragraph
from neurodb.knowledge.models import ReviewBatch, ReviewParagraph
from tests.assistant import anthropic_mock as am
from tests.assistant.claude_params import assert_claude_params
from tests.knowledge.test_openai_parity import EXPECTED
from tests.knowledge.test_review import AI_ON, _add, _doc, _topics

pytestmark = pytest.mark.django_db

USED = am.usage(100, 20, read=50)  # the parity shapes' tokens: a prompt of 150, 50 of it cached
ANSWER = json.dumps({"paragraph": "Two reports find teachers lack training [1][2]."})
# A reply on Claude, and the OpenAI shape whose kept outcome it gives: a declined answer keeps no text
REPLIES = {
    "ok": ("completed", am.turn([am.thinking("sig-1"), am.text(ANSWER)], usage=USED)),
    "max_tokens": ("max_output_tokens", am.turn([am.text(ANSWER)], stop_reason="max_tokens", usage=USED)),
    "refusal": ("empty", am.refusal("cyber", blocks=[am.text(ANSWER[:10])], usage=USED)),
}


@pytest.mark.parametrize("reply", list(REPLIES))
def test_the_theme_paragraph_keeps_on_claude_what_it_keeps_on_openai(claude, viewer, reply):
    shape, events = REPLIES[reply]
    annual = ReviewBatch.objects.create(name="Annual")
    topic = _topics(1)[0]
    _add(_doc(annual, "Report A", 2023), topic, "Teachers lack training.", 2023)
    _add(_doc(annual, "Report B", 2024), topic, "Teachers lack training in the north.", 2024)
    server = claude(events)
    with override_settings(**AI_ON):
        found = review_paragraph.write(topic, viewer, None, False, False)

    row = ReviewParagraph.objects.get()
    kept = [found.ok, found.message, found.text, row.status, row.reason, row.input_tokens, row.output_tokens]
    assert kept == EXPECTED[shape]
    assert row.model == am.MODEL
    body, headers = server.requests[0]["body"], server.requests[0]["headers"]
    assert_claude_params(body, headers)
    assert body["system"] == [
        {"type": "text", "text": review_paragraph.INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}
    ]
    assert body["output_config"]["effort"] == "low" and body["max_tokens"] == review_paragraph.OUTPUT_TOKENS
    assert body["messages"][0]["content"].startswith(f"Theme: {topic.path}")
