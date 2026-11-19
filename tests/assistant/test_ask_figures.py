"""Ask NeuroDB checks its answer against what the lookups of that answer returned, as Monitoring
insights' chat does (``neurodb.fmm.ai.citations``): a figure no lookup returned is listed under the answer
(the figures themselves are never changed), and a link to a NeuroDB page no lookup returned keeps its text
but loses its link. Years, dates, references, page numbers, small counts, figures the question names and
simple shares and totals of the looked-up figures are not listed. Help, Monitoring insights and NeuroDB
Watch keep their own answers. Every name, number and address here is made up."""

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import admin as assistant_admin
from neurodb.assistant import agent, figures, tools
from neurodb.assistant.models import AssistantQuestion
from neurodb.fmm.ai import citations
from tests.assistant.anthropic_mock import text, tool_use, turn
from tests.assistant.test_assistant import ENABLED, FakeClient, call, reply, say

LOOKUP = {
    "partners": [
        {"name": "Example Aid", "url": "/partners/3/", "reached": 1200, "target": 1500},
        {"name": "Sample Relief", "url": "/partners/4/", "reached": 450, "target": 500},
    ],
    "total_rows": 2,
    "rate": 76.923077,
    "as_of": "2026-09-13",
    "report": {"title": "Mid-term review", "url": "/knowledge/12/"},
    "note": "The survey counted 2,345 children out of school.",
}
QUESTION = "How are the partners doing in the 37 schools?"
GROUNDED = (
    "[Example Aid](/partners/3/) reached 1,200 of 1,500 (80.0%) and [Sample Relief](/partners/4/) 450 of "
    "500 (90%): 1,650 children in all, 72.7% of them through Example Aid. The rate is 76.9% (about 77%) as "
    "of 13 Sep 2026 under LEB/PCA2026001/PD2026001, for 2026 and the 2025/26 school year. The "
    "[Mid-term review](/knowledge/12/) counted 2,345 children out of school (p. 41). Of the 37 schools, "
    "3 partners report."
)
NOTICE = "These figures could not be checked against the data: 63.5, 4950."


@pytest.fixture
def lookup(monkeypatch, db):
    """data_freshness returns LOOKUP."""
    monkeypatch.setitem(tools.TOOLS, "data_freshness", (lambda: LOOKUP, *tools.TOOLS["data_freshness"][1:]))


def _answer(question=QUESTION, history=(), options=None):
    outcome = agent.Outcome()
    events = list(agent.answer(question, list(history), outcome, options=options))
    return outcome, events


def _looked_up_then(claude, answer: str):
    """Claude looks the data up once, then writes ``answer``."""
    return claude(
        turn([tool_use("toolu_1", "data_freshness", "{}")], stop_reason="tool_use"),
        turn([text(answer)]),
    )


def _events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


# ------------------------------------------------------------------------------------------- figures
def test_a_figure_no_lookup_returned_is_listed_under_the_answer(claude, lookup):
    answer = "Example Aid reached 1,200 children and 4,950 youths; 63.5% stayed."
    _looked_up_then(claude, answer)
    outcome, events = _answer()

    done = events[-1]
    assert done["type"] == "done" and done["notice"] == NOTICE
    # the figures are never changed, and nothing is held back
    assert done["answer"] == answer == outcome.answer and outcome.status == "answered"
    assert "4,950" in done["html"] and "63.5%" in done["html"]


def test_the_notice_has_monitoring_insights_wording():
    assert citations.figures_line(["63.5", "4950"]) == NOTICE
    assert citations.figures_line([]) == ""


def test_an_answer_whose_figures_all_come_from_the_lookups_has_no_notice(claude, lookup):
    _looked_up_then(claude, GROUNDED)
    outcome, events = _answer()

    done = events[-1]
    assert done["type"] == "done" and "notice" not in done
    assert done["answer"] == GROUNDED and outcome.check == {}


def test_a_share_or_total_across_unrelated_figures_is_listed(claude, lookup):
    # 1,200 / 500 is one row's reached over another row's target; 1,700 adds two of them
    _looked_up_then(claude, "That is 240.0% of the target, or 1,700 children.")
    _, events = _answer()
    assert events[-1]["notice"] == "These figures could not be checked against the data: 240.0, 1700."


def test_a_share_of_two_figures_written_side_by_side_is_not_listed(claude, lookup):
    # 450 of 1,500 is one row's reached over another row's target: worked out in the sentence itself, as
    # are the difference of two figures and the change from one to the other
    _looked_up_then(
        claude,
        "Sample Relief's 450 is 30.0% of Example Aid's 1,500.\n\n"
        "From 450 to 1,200 is 750 more, up 166.7%.\n\nThe rest: 35.0%, and 760 more.",
    )
    _, events = _answer()
    assert events[-1]["notice"] == "These figures could not be checked against the data: 35.0, 760."


def test_the_figures_of_the_question_count_but_an_invented_one_does_not(claude, lookup):
    _looked_up_then(claude, "Of the 37 schools, 41 have a partner.")
    _, events = _answer()
    assert events[-1]["notice"] == "These figures could not be checked against the data: 41."


# ------------------------------------------------------------------------------------------- links
def test_a_made_up_neurodb_link_loses_its_link_and_a_returned_one_keeps_it(claude, lookup):
    answer = (
        "[Example Aid](/partners/3/) and [Made-up Partner](/partners/99/), "
        '<a href="/partners/98/">a raw link</a> and [a reference link][r].\n\n'
        "[r]: /partners/97/\n"
    )
    _looked_up_then(claude, answer)
    outcome, events = _answer()

    done = events[-1]
    assert 'href="/partners/3/"' in done["html"]
    for made_up in ("/partners/99/", "/partners/98/", "/partners/97/"):
        assert made_up not in done["html"], made_up
    for kept in ("Made-up Partner", "a raw link", "a reference link"):
        assert kept in done["html"], kept
    assert "/partners/99/" not in done["answer"] and "Made-up Partner" in done["answer"]
    assert outcome.answer == done["answer"]
    assert "notice" not in done  # a link taken out is not a figure: nothing to list
    assert outcome.check == {"check": "answer", "notice": "", "unchecked_numbers": [], "links_removed": 3}


def test_a_link_an_earlier_answer_gave_may_be_given_again(claude, db):
    claude(turn([text("See [Example Aid](/partners/3/) and [Other Aid](/partners/5/).")]))
    _, events = _answer(history=[{"question": "Who?", "answer": "[Example Aid](/partners/3/) did."}])
    html = events[-1]["html"]
    assert 'href="/partners/3/"' in html and "/partners/5/" not in html and "Other Aid" in html


# ------------------------------------------------------------------------------ the log and follow-ups
def test_the_question_log_records_the_notice_and_the_checked_answer(claude, lookup, client_viewer):
    _looked_up_then(claude, "[Made-up Partner](/partners/99/) reached 4,950 children; 63.5% stayed.")
    events = _events(client_viewer.post(reverse("assistant:stream"), {"question": QUESTION}))
    assert events[-1]["notice"] == NOTICE

    row = AssistantQuestion.objects.get()
    assert row.status == "answered"
    assert row.answer == "Made-up Partner reached 4,950 children; 63.5% stayed."
    assert row.tools[0]["tool"] == "data_freshness"
    assert row.tools[-1] == {
        "check": "answer",
        "notice": NOTICE,
        "unchecked_numbers": ["63.5", "4950"],
        "links_removed": 1,
    }
    # the admin's list counts the lookups only, and shows the figures to check
    shown = assistant_admin.AssistantQuestionAdmin(AssistantQuestion, None)
    assert shown.lookups(row) == 1 and shown.unchecked(row) == 2


def test_a_clean_answer_adds_nothing_to_the_log(claude, lookup, client_viewer):
    _looked_up_then(claude, GROUNDED)
    _events(client_viewer.post(reverse("assistant:stream"), {"question": QUESTION}))
    row = AssistantQuestion.objects.get()
    assert [entry["tool"] for entry in row.tools] == ["data_freshness"]
    assert assistant_admin.AssistantQuestionAdmin(AssistantQuestion, None).unchecked(row) == ""


def test_a_follow_up_may_repeat_the_checked_figures_of_an_earlier_answer(claude, db, client_viewer):
    conversation = uuid.uuid4()
    viewer = get_user_model().objects.get(username="viewer")
    AssistantQuestion.objects.create(
        user=viewer,
        conversation=conversation,
        question="And the 88 centres?",
        answer="Example Aid reached 1,234 children; 4,950 youths.",
        tools=[
            {"tool": "data_freshness", "input": {}, "ok": True, "ms": 3},
            {"check": "answer", "notice": "x", "unchecked_numbers": ["4950"], "links_removed": 0},
        ],
        status=AssistantQuestion.Status.ANSWERED,
    )
    claude(turn([text("Still 1,234 children and 4,950 youths in the 88 centres.")]))
    events = _events(
        client_viewer.post(
            reverse("assistant:stream"), {"question": "And now?", "conversation": str(conversation)}
        )
    )
    # 1,234 was checked in the earlier answer, 88 was asked; 4,950 was listed then and is listed again
    assert events[-1]["notice"] == "These figures could not be checked against the data: 4950."


# --------------------------------------------------------------------------------- the other runs
def test_help_monitoring_insights_and_watch_runs_are_not_checked(claude, lookup):
    answer = "[Made-up Partner](/partners/99/) reached 4,950 children."
    _looked_up_then(claude, answer)
    outcome, events = _answer(options=agent.RunOptions(tools=("data_freshness",)))
    assert events[-1] == {"type": "done", "answer": answer, "html": agent.render(answer)}
    assert outcome.check == {}


def test_on_openai_the_answer_is_checked_too(monkeypatch, lookup):
    client = FakeClient(
        [reply(call("data_freshness", {})), reply(say("[Made-up](/partners/99/) reached 4,950 and 63.5%."))]
    )
    monkeypatch.setattr(agent, "client", lambda: client)
    with override_settings(**ENABLED, AI_PROVIDER="openai"):
        _, events = _answer()
    assert events[-1]["notice"] == NOTICE and "/partners/99/" not in events[-1]["html"]


# ------------------------------------------------------------------------------------------- the parts
def test_the_lookups_give_their_links_figures_shares_and_totals():
    looked = figures.Looked()
    looked.add(LOOKUP, json.dumps(LOOKUP))
    looked.add({"urls": ["/a/", "https://example.org/x"], "edition_url": "/b/", "link": "/c/"}, "{}")
    assert looked.urls == {"/partners/3/", "/partners/4/", "/knowledge/12/", "/a/", "/b/"}
    assert {"1200", "1500", "76.923077", "76.9", "77", "2345", "2026"} <= looked.numbers
    assert {80.0, 90.0} <= looked.shares  # one row's two figures
    assert {1650.0, 2000.0} <= looked.totals  # a column of the list
    assert round(100 * 1200 / 1650, 3) in {round(s, 3) for s in looked.shares}  # a row's share of its column


def test_years_dates_references_and_pages_are_set_aside():
    checked = figures.check(
        "In 2026 and 2025/26 (15 Oct 2026, 2026-10-15, Oct 2026), LEB/PCA2026001/PD2026001 and PD2026002, "
        "see pp. 41-43 and page 77; Q3, 4 partners. But 2,000 children.",
        figures.Looked(),
    )
    assert checked.unchecked_numbers == ["2000"]
