"""On OpenAI every feature reads each response shape as it did before the provider layer: the outcome it
keeps for a completed answer, an incomplete one for each reason, a refusal part, a failed response, a
partial fake and an empty answer (tests.assistant.openai_shapes) is the one recorded from the code at
fbe0b64. Here: the AI checks and the action point review (B, C), the document review's stages (E), the
country programme reading (G), the daily review's narration and decisions (H1, H2), the What's new note
(I), the Watch morning note (J), the periodic report figures (M) and the document summaries (N). The
Monitoring insights brief and the action point summary (A, D) are in tests/fmm, the theme paragraph (F)
in tests/knowledge."""

import datetime
import json
from types import SimpleNamespace

import pytest
from django.core.files.base import ContentFile
from django.test import override_settings

from tests.assistant.openai_shapes import SHAPES, install, outcome_of, shaped

ON = {"AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "test-key-not-real", "AI_ASSISTANT_MODEL": "gpt-5.5"}
TODAY = datetime.date(2026, 10, 15)


def _check(api):
    from neurodb.fmm.ai import checks

    found = checks.check(
        api,
        SimpleNamespace(code="R7"),
        "Is the narrative coherent with the rating?",
        {"narrative": "Sessions were held as planned in two centres."},
        SimpleNamespace(ai_model="", ai_temperature=None, ai_max_output_tokens=2000),
        frozenset(),
    )
    return None if found is None else [found.passed, found.detail, found.input_tokens, found.output_tokens]


def _ap_review(api):
    from neurodb.fmm.ai import ap_review

    found = ap_review.review(
        api,
        "Is the action taken adequate?",
        {"issue": "Restock the school kits.", "action_taken": "The kits were delivered on 8 September."},
        SimpleNamespace(ai_model="", ai_temperature=None, ai_max_output_tokens=2000),
        frozenset(),
    )
    return (
        None if found is None else [found.verdict, found.explanation, found.input_tokens, found.output_tokens]
    )


def _doc_review(api):
    from neurodb.knowledge import review
    from neurodb.knowledge.models import DocumentReviewSettings

    ctx = review.Context(api=api, setting=DocumentReviewSettings.load(), model="gpt-5.5")
    return [review.ask(ctx, review.FINDINGS, "[page 1] Attendance fell in two centres."), dict(ctx.tally)]


def _cpd(api):
    from neurodb.cpd import extraction

    document = SimpleNamespace(filename="framework.txt", file=ContentFile(b"Outcome 1: Children learn."))
    return extraction.extract(document)


def _findings():
    from neurodb.review.models import ReviewFinding

    return [
        ReviewFinding(
            key="pd-late-1",
            severity=ReviewFinding.Severity.CRITICAL,
            state="new",
            section="Education",
            title="Two progress reports are overdue",
            detail="Two progress reports of the programme document are overdue.",
            evidence={"numbers": {"overdue": 2}},
        )
    ]


def _narrate(api):
    from neurodb.review import services
    from neurodb.review.models import DailyReview

    review = DailyReview(date=TODAY, stats={})
    text = services.narrate(review, _findings())
    return [text, review.narrated_by, review.model_input_tokens, review.model_output_tokens]


def _decide(api):
    from neurodb.review import services
    from neurodb.review.models import DailyReview

    review = DailyReview(date=TODAY, stats={})
    decisions = services.decide(review, _findings())
    return [decisions, review.decided_by, review.model_input_tokens, review.model_output_tokens]


def _digest(api):
    from neurodb.graph import digest

    return list(digest.narrate("the Education section", ["A report was submitted.", "A visit was planned."]))


def _watch_note(api):
    from neurodb.watch import explain

    facts = SimpleNamespace(
        payload={"items": [{"key": "item:1", "title": "A progress report is due"}]},
        citable={"item:1": {"key": "item:1", "title": "A progress report is due"}},
    )
    asked = explain.ask(facts, TODAY, [])
    return [asked.sentences, asked.reason, asked.called, list(asked.tokens), asked.dropped]


def _figures(api):
    from neurodb.knowledge import periodic
    from neurodb.knowledge.models import Document

    document = Document.objects.create(title="Situation report 12", text="Children reached: 1,200.")
    return periodic._ai_figures(document, ["Children reached: 1,200."], [])


def _summary(api):
    from neurodb.knowledge import indexing
    from neurodb.knowledge.models import Document

    document = Document.objects.create(title="Mid-term review", text="The programme reached its targets.")
    done = indexing.summarise(document)
    document.refresh_from_db()
    return [done, document.summary, document.key_points]


ANSWERS = {
    "B_checks": (_check, {"is_coherent": True, "detail": "The narrative is specific."}),
    "C_ap_review": (_ap_review, None),  # set below: the first verdict label
    "E_doc_review": (
        _doc_review,
        {
            "findings": [
                {
                    "category": "challenge",
                    "tag": "Other",
                    "text": "Attendance fell in two centres.",
                    "quote": "Attendance fell in two centres.",
                    "place": "",
                    "date": "",
                    "kind": "reported",
                }
            ]
        },
    ),
    "G_cpd": (
        _cpd,
        {"outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]},
    ),
    "H1_narration": (_narrate, "Two progress reports are overdue today and need a person."),
    "H2_decisions": (
        _decide,
        {
            "decisions": [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ]
        },
    ),
    "I_whats_new": (_digest, "A report was submitted and a visit was planned."),
    "J_watch_note": (_watch_note, {"sentences": [{"text": "A progress report is due.", "keys": ["item:1"]}]}),
    "M_figures": (_figures, {"figures": [{"group": "Children", "metric": "Reached", "value": 1200}]}),
    "N_summary": (
        _summary,
        {
            "summary": "The programme reached its targets.",
            "key_points": ["Targets reached"],
            "document_date": "",
            "organisations": [],
            "places": [],
            "references": [],
        },
    ),
}


def _answer(caller):
    from neurodb.fmm.ai import ap_review

    if caller == "C_ap_review":
        return {"verdict": next(iter(ap_review.VERDICTS)), "explanation": "The kits were delivered."}
    return ANSWERS[caller][1]


def run(caller, shape, monkeypatch):
    answer = _answer(caller)
    text = answer if isinstance(answer, str) else json.dumps(answer)
    api = install(monkeypatch, shaped(shape, text))
    with override_settings(**ON):
        return json.loads(json.dumps(outcome_of(lambda: ANSWERS[caller][0](api)), default=str))


# Recorded from the code at fbe0b64 (the same answer text in every shape but "empty")
EXPECTED = {
    "B_checks": {
        "completed": [True, "The narrative is specific.", 150, 20],
        "max_output_tokens": None,
        "content_filter": None,
        "other_reason": None,
        "refusal": [True, "The narrative is specific.", 150, 20],
        "failed": [True, "The narrative is specific.", 150, 20],
        "partial_fake": [True, "The narrative is specific.", 0, 0],
        "empty": None,
    },
    "C_ap_review": {
        "completed": ["adequate", "The kits were delivered.", 150, 20],
        "max_output_tokens": None,
        "content_filter": None,
        "other_reason": None,
        "refusal": ["adequate", "The kits were delivered.", 150, 20],
        "failed": ["adequate", "The kits were delivered.", 150, 20],
        "partial_fake": ["adequate", "The kits were delivered.", 0, 0],
        "empty": None,
    },
    "E_doc_review": {
        "completed": [
            [
                {
                    "category": "challenge",
                    "tag": "Other",
                    "text": "Attendance fell in two centres.",
                    "quote": "Attendance fell in two centres.",
                    "place": "",
                    "date": "",
                    "kind": "reported",
                }
            ],
            {"calls": 1, "tokens": 170},
        ],
        "max_output_tokens": [None, {"calls": 1, "tokens": 170}],
        "content_filter": [None, {"calls": 1, "tokens": 170}],
        "other_reason": [None, {"calls": 1, "tokens": 170}],
        "refusal": [
            [
                {
                    "category": "challenge",
                    "tag": "Other",
                    "text": "Attendance fell in two centres.",
                    "quote": "Attendance fell in two centres.",
                    "place": "",
                    "date": "",
                    "kind": "reported",
                }
            ],
            {"calls": 1, "tokens": 170},
        ],
        "failed": [
            [
                {
                    "category": "challenge",
                    "tag": "Other",
                    "text": "Attendance fell in two centres.",
                    "quote": "Attendance fell in two centres.",
                    "place": "",
                    "date": "",
                    "kind": "reported",
                }
            ],
            {"calls": 1, "tokens": 170},
        ],
        "partial_fake": [
            [
                {
                    "category": "challenge",
                    "tag": "Other",
                    "text": "Attendance fell in two centres.",
                    "quote": "Attendance fell in two centres.",
                    "place": "",
                    "date": "",
                    "kind": "reported",
                }
            ],
            {"calls": 1, "tokens": 0},
        ],
        "empty": [None, {"calls": 1, "tokens": 170}],
    },
    "G_cpd": {
        "completed": {
            "outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]
        },
        "max_output_tokens": {
            "outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]
        },
        "content_filter": {
            "outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]
        },
        "other_reason": {
            "outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]
        },
        "refusal": {"outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]},
        "failed": {"outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]},
        "partial_fake": {
            "outcomes": [{"code": "1", "title": "Children learn", "indicators": [], "outputs": []}]
        },
        "empty": ["raised", "ExtractionError", "The answer could not be read as a framework."],
    },
    "H1_narration": {
        "completed": ["Two progress reports are overdue today and need a person.", "gpt-5.5", 150, 20],
        "max_output_tokens": [
            "Two progress reports are overdue today and need a person.",
            "gpt-5.5",
            150,
            20,
        ],
        "content_filter": ["Two progress reports are overdue today and need a person.", "gpt-5.5", 150, 20],
        "other_reason": ["Two progress reports are overdue today and need a person.", "gpt-5.5", 150, 20],
        "refusal": ["Two progress reports are overdue today and need a person.", "gpt-5.5", 150, 20],
        "failed": ["Two progress reports are overdue today and need a person.", "gpt-5.5", 150, 20],
        "partial_fake": ["Two progress reports are overdue today and need a person.", "gpt-5.5", 0, 0],
        "empty": [
            "The daily review found 1 items to look at: 1 critical, 0 warnings and 0 to "
            "note. Start with: Two progress reports are overdue.",
            "template",
            0,
            0,
        ],
    },
    "H2_decisions": {
        "completed": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            150,
            20,
        ],
        "max_output_tokens": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            150,
            20,
        ],
        "content_filter": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            150,
            20,
        ],
        "other_reason": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            150,
            20,
        ],
        "refusal": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            150,
            20,
        ],
        "failed": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            150,
            20,
        ],
        "partial_fake": [
            [
                {
                    "decision": "Follow up the overdue reports",
                    "why": "Two progress reports are overdue.",
                    "who": "Education section",
                    "urgency": "today",
                    "finding_keys": ["pd-late-1"],
                }
            ],
            "gpt-5.5",
            0,
            0,
        ],
        "empty": [[], "rules", 150, 20],
    },
    "I_whats_new": {
        "completed": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "max_output_tokens": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "content_filter": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "other_reason": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "refusal": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "failed": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "partial_fake": ["A report was submitted and a visit was planned.", "gpt-5.5"],
        "empty": [
            "2 notable changes in the last 24 hours.\n• A report was submitted.\n• A visit was planned.",
            "template",
        ],
    },
    "J_watch_note": {
        "completed": [
            [{"text": "A progress report is due.", "keys": ["item:1"]}],
            "",
            True,
            [100, 50, 20],
            [],
        ],
        "max_output_tokens": [
            [{"text": "A progress report is due.", "keys": ["item:1"]}],
            "",
            True,
            [100, 50, 20],
            [],
        ],
        "content_filter": [
            [{"text": "A progress report is due.", "keys": ["item:1"]}],
            "",
            True,
            [100, 50, 20],
            [],
        ],
        "other_reason": [
            [{"text": "A progress report is due.", "keys": ["item:1"]}],
            "",
            True,
            [100, 50, 20],
            [],
        ],
        "refusal": [[{"text": "A progress report is due.", "keys": ["item:1"]}], "", True, [100, 50, 20], []],
        "failed": [[{"text": "A progress report is due.", "keys": ["item:1"]}], "", True, [100, 50, 20], []],
        "partial_fake": [
            [{"text": "A progress report is due.", "keys": ["item:1"]}],
            "",
            True,
            [0, 0, 0],
            [],
        ],
        "empty": [[], "error", True, [100, 50, 20], ["malformed"]],
    },
    "M_figures": {
        "completed": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "max_output_tokens": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "content_filter": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "other_reason": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "refusal": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "failed": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "partial_fake": [{"group": "Children", "metric": "Reached", "value": 1200}],
        "empty": ["raised", "JSONDecodeError", "Expecting value: line 1 column 1 (char 0)"],
    },
    "N_summary": {
        "completed": [True, "The programme reached its targets.", ["Targets reached"]],
        "max_output_tokens": [True, "The programme reached its targets.", ["Targets reached"]],
        "content_filter": [True, "The programme reached its targets.", ["Targets reached"]],
        "other_reason": [True, "The programme reached its targets.", ["Targets reached"]],
        "refusal": [True, "The programme reached its targets.", ["Targets reached"]],
        "failed": [True, "The programme reached its targets.", ["Targets reached"]],
        "partial_fake": [True, "The programme reached its targets.", ["Targets reached"]],
        "empty": ["raised", "JSONDecodeError", "Expecting value: line 1 column 1 (char 0)"],
    },
}


@pytest.mark.django_db
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("caller", list(ANSWERS))
def test_each_feature_reads_each_openai_shape_as_before(caller, shape, monkeypatch):
    assert run(caller, shape, monkeypatch) == EXPECTED[caller][shape]
