"""On OpenAI the Monitoring insights brief (A) and the action points' AI content summary (D) read each
response shape as they did before the provider layer (see tests/assistant/openai_shapes.py): the outcome
each keeps is the one recorded from the code at fbe0b64."""

import json

import pytest
from django.test import override_settings

from neurodb.datamart import models as dm
from neurodb.fmm.ai import ap_summary, facts, insights, profiles
from neurodb.fmm.models import Insight
from neurodb.watch import people
from tests.assistant.openai_shapes import SHAPES, install, outcome_of, shaped
from tests.fmm.test_insights import AI_ON, TODAY, _good, _scope
from tests.fmm.test_stage_c import points  # noqa: F401 - the action points the summary reads

pytestmark = pytest.mark.django_db


def _brief(monkeypatch, shape):
    people.forget()
    with override_settings(**AI_ON):
        found = facts.build(_scope(), profiles.published(), TODAY)
        install(monkeypatch, shaped(shape, json.dumps(_good(found))))

        def write():
            row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
            return [
                row.status,
                row.reason,
                row.called,
                row.input_tokens,
                row.cached_tokens,
                row.output_tokens,
            ]

        return outcome_of(write)


def _summary(monkeypatch, shape, viewer):
    people.forget()
    answer = {
        "themes": [{"name": "Stock and supplies", "count": 1, "example": "LEBA/2026/9001/AP"}],
        "pattern": "One of the action points asks a partner to restock supplies.",
    }
    install(monkeypatch, shaped(shape, json.dumps(answer)))
    with override_settings(**AI_ON):

        def write():
            found = ap_summary.summarise(dm.ActionPoint.objects.all(), viewer)
            return [found.ok, found.message, found.themes, found.pattern, found.points]

        return json.loads(json.dumps(outcome_of(write), default=str))


# Recorded from the code at fbe0b64 (the same answer text in every shape but "empty")
EXPECTED_BRIEF = {
    "completed": ["ok", "", True, 150, 50, 20],
    "max_output_tokens": ["failed", "cut off", True, 150, 50, 20],
    "content_filter": ["failed", "refused", True, 150, 50, 20],
    "other_reason": ["failed", "cut off", True, 150, 50, 20],
    "refusal": ["failed", "refused", True, 150, 50, 20],
    "failed": ["ok", "", True, 150, 50, 20],
    "partial_fake": ["ok", "", True, 0, 0, 0],
    "empty": ["failed", "malformed", True, 150, 50, 20],
}
EXPECTED_SUMMARY = {
    "completed": [
        True,
        "",
        [{"name": "Stock and supplies", "count": 1, "example": "LEBA/2026/9001/AP"}],
        "One of the action points asks a partner to restock supplies.",
        5,
    ],
    "max_output_tokens": [
        False,
        "The summary did not add up to the action points read, so it is not shown. Try again.",
        [],
        "",
        0,
    ],
    "content_filter": [
        False,
        "The summary did not add up to the action points read, so it is not shown. Try again.",
        [],
        "",
        0,
    ],
    "other_reason": [
        False,
        "The summary did not add up to the action points read, so it is not shown. Try again.",
        [],
        "",
        0,
    ],
    "refusal": [
        True,
        "",
        [{"name": "Stock and supplies", "count": 1, "example": "LEBA/2026/9001/AP"}],
        "One of the action points asks a partner to restock supplies.",
        5,
    ],
    "failed": [
        True,
        "",
        [{"name": "Stock and supplies", "count": 1, "example": "LEBA/2026/9001/AP"}],
        "One of the action points asks a partner to restock supplies.",
        5,
    ],
    "partial_fake": [
        True,
        "",
        [{"name": "Stock and supplies", "count": 1, "example": "LEBA/2026/9001/AP"}],
        "One of the action points asks a partner to restock supplies.",
        5,
    ],
    "empty": [False, "The summary could not be written. Try again in a moment.", [], "", 0],
}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_brief_reads_each_openai_shape_as_before(built, monkeypatch, shape):
    assert _brief(monkeypatch, shape) == EXPECTED_BRIEF[shape]


@pytest.mark.parametrize("shape", SHAPES)
def test_the_action_point_summary_reads_each_openai_shape_as_before(points, viewer, monkeypatch, shape):  # noqa: F811
    assert _summary(monkeypatch, shape, viewer) == EXPECTED_SUMMARY[shape]
