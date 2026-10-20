"""On OpenAI the document review's theme paragraph (F) reads each response shape as it did before the
provider layer (see tests/assistant/openai_shapes.py): the outcome it keeps is the one recorded from the
code at fbe0b64."""

import json

import pytest
from django.test import override_settings

from neurodb.knowledge import review_paragraph
from neurodb.knowledge.models import ReviewBatch, ReviewParagraph
from tests.assistant.openai_shapes import SHAPES, install, outcome_of, shaped
from tests.knowledge.test_review import AI_ON, _add, _doc, _topics

pytestmark = pytest.mark.django_db


def _paragraph(monkeypatch, shape, viewer):
    annual = ReviewBatch.objects.create(name="Annual")
    topic = _topics(1)[0]
    _add(_doc(annual, "Report A", 2023), topic, "Teachers lack training.", 2023)
    _add(_doc(annual, "Report B", 2024), topic, "Teachers lack training in the north.", 2024)
    install(
        monkeypatch,
        shaped(shape, json.dumps({"paragraph": "Two reports find teachers lack training [1][2]."})),
    )
    with override_settings(**AI_ON):

        def write():
            found = review_paragraph.write(topic, viewer, None, False, False)
            row = ReviewParagraph.objects.get()
            return [
                found.ok,
                found.message,
                found.text,
                row.status,
                row.reason,
                row.input_tokens,
                row.output_tokens,
            ]

        return outcome_of(write)


# Recorded from the code at fbe0b64 (the same answer text in every shape but "empty")
EXPECTED = {
    "completed": [
        True,
        "",
        "Two reports find teachers lack training (Report A, p. 2) (Report B, p. 2).",
        "done",
        "",
        150,
        20,
    ],
    "max_output_tokens": [
        False,
        "The paragraph cited none of the findings, so it is not shown. Try again.",
        "",
        "failed",
        "not cited",
        150,
        20,
    ],
    "content_filter": [
        False,
        "The paragraph cited none of the findings, so it is not shown. Try again.",
        "",
        "failed",
        "not cited",
        150,
        20,
    ],
    "other_reason": [
        False,
        "The paragraph cited none of the findings, so it is not shown. Try again.",
        "",
        "failed",
        "not cited",
        150,
        20,
    ],
    "refusal": [
        True,
        "",
        "Two reports find teachers lack training (Report A, p. 2) (Report B, p. 2).",
        "done",
        "",
        150,
        20,
    ],
    "failed": [
        True,
        "",
        "Two reports find teachers lack training (Report A, p. 2) (Report B, p. 2).",
        "done",
        "",
        150,
        20,
    ],
    "partial_fake": [
        True,
        "",
        "Two reports find teachers lack training (Report A, p. 2) (Report B, p. 2).",
        "done",
        "",
        0,
        0,
    ],
    "empty": [
        False,
        "The paragraph could not be written. Try again in a moment.",
        "",
        "failed",
        "no answer",
        150,
        20,
    ],
}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_theme_paragraph_reads_each_openai_shape_as_before(viewer, monkeypatch, shape):
    assert _paragraph(monkeypatch, shape, viewer) == EXPECTED[shape]
