"""Ask NeuroDB sends no person to the AI provider: the question, the earlier questions and answers and
every lookup's result reach the model without e-mail addresses (also written out), links, phone numbers,
the person names NeuroDB knows or names written after a title, while NeuroDB's own page links, partner
and place names, numbers, dates and codes stay. Help, Monitoring insights and NeuroDB Watch keep their
own requests. Every name, address and number here is made up."""

import datetime
import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, privacy, tools
from neurodb.assistant.models import AssistantQuestion
from neurodb.help import assistant as help_assistant
from neurodb.indicators.models import NeuroReportComment
from neurodb.knowledge import indexing
from neurodb.knowledge.models import Document, DocumentFinding, FindingCategory, Link, ReviewBatch, Topic
from neurodb.partnerships.models import PartnerOrganization
from neurodb.review.models import DailyReview, ReviewFinding
from tests.assistant.anthropic_mock import text, tool_use, turn
from tests.assistant.test_assistant import ENABLED, FakeClient, reply, say

EMAIL = "rania.planted@example.org"
SPELLED = "karim dot planted at example dot org"
PHONE = "03 123 456"
LINK = "https://files.example.org/minutes.pdf"
# a person NeuroDB knows (a user), an address, an address written out, a phone, a link, a name after a title
DIRTY = (
    f"Rania Plantedname ({EMAIL}, {SPELLED}, {PHONE}) shared {LINK} and Mr Karim Plantedtwo agreed. "
    "Akkar schools reopened."
)
LEAKS = ("plantedname", "rania.planted", "karim dot planted", "123 456", "files.example.org", "plantedtwo")


def _clean(sent: str) -> None:
    """``sent`` names nobody planted, says what was taken out and keeps the rest of the sentence."""
    folded = sent.casefold()
    assert not [leak for leak in LEAKS if leak in folded], sent
    for placeholder in ("[name withheld]", "[email withheld]", "[phone withheld]", "[link withheld]"):
        assert placeholder in sent, (placeholder, sent)
    assert "Akkar schools reopened." in sent


@pytest.fixture
def person(db):
    """A NeuroDB user: a name NeuroDB knows."""
    return get_user_model().objects.create_user(
        username="rplanted", first_name="Rania", last_name="Plantedname", email=EMAIL
    )


def _answer(question="Any news?", history=(), options=None):
    outcome = agent.Outcome()
    events = list(agent.answer(question, list(history), outcome, options=options))
    return outcome, events


def _one_lookup(claude, tool: str, arguments: dict):
    """Claude calls ``tool`` once, then answers: the stand-in that records both requests."""
    return claude(
        turn([tool_use("toolu_1", tool, json.dumps(arguments))], stop_reason="tool_use"),
        turn([text("Done.")]),
    )


def _result(server) -> dict:
    """The lookup's result as the model read it (the second request's last message)."""
    (block,) = server.requests[1]["body"]["messages"][-1]["content"]
    assert block["type"] == "tool_result"
    return json.loads(block["content"])


# ------------------------------------------------------------------------------ the question and the turns
def test_the_question_reaches_the_ai_cleaned(claude, person):
    server = claude(turn([text("Done.")]))
    outcome, events = _answer(question=f"What did {DIRTY}")
    assert outcome.status == "answered"
    (asked,) = server.requests[0]["body"]["messages"]
    assert asked["role"] == "user"
    _clean(asked["content"])
    assert asked["content"].startswith("What did [name withheld] ([email withheld], [email withheld]")


def test_earlier_questions_and_answers_are_cleaned_and_the_log_keeps_what_was_typed(
    claude, client_viewer, person
):
    conversation = uuid.uuid4()
    viewer = get_user_model().objects.get(username="viewer")
    AssistantQuestion.objects.create(
        user=viewer,
        conversation=conversation,
        question=f"Who wrote: {DIRTY}",
        answer=f"The minutes say:\n\n- {DIRTY}\n\n| Partner | Schools |\n|---|---|\n| AMEL | 1,200 |",
        status=AssistantQuestion.Status.ANSWERED,
    )
    server = claude(turn([text("Done.")]))
    typed = f"And what else did Rania Plantedname say? Write to {EMAIL}."
    response = client_viewer.post(
        reverse("assistant:stream"), {"question": typed, "conversation": str(conversation)}
    )
    b"".join(response.streaming_content)

    earlier_question, earlier_answer, asked = server.requests[0]["body"]["messages"]
    _clean(earlier_question["content"])
    _clean(earlier_answer["content"])
    # the answer keeps its Markdown: its lines, its list and its table
    assert earlier_answer["content"].startswith("The minutes say:\n\n- [name withheld]")
    assert earlier_answer["content"].endswith("\n\n| Partner | Schools |\n|---|---|\n| AMEL | 1,200 |")
    assert asked["content"] == "And what else did [name withheld] say? Write to [email withheld]."
    # the question log keeps what the person typed: it is theirs, and stays in NeuroDB
    assert AssistantQuestion.objects.filter(conversation=conversation).latest("created_at").question == typed


def test_the_same_turns_give_the_same_bytes_every_time(claude, person):
    history = [{"question": f"Who wrote: {DIRTY}", "answer": f"It was:\n{DIRTY}"}]
    server = claude(turn([text("One.")]), turn([text("Two.")]))
    _answer(question=DIRTY, history=history)
    _answer(question=DIRTY, history=history)
    first, second = (json.dumps(r["body"]["messages"], ensure_ascii=False) for r in server.requests)
    assert first == second  # the prompt cache sees the same prefix
    once = privacy.text(DIRTY)
    assert privacy.text(once) == once  # cleaning a cleaned text changes nothing
    assert privacy.history(privacy.history(history)) == privacy.history(history)


def test_on_openai_the_question_and_the_turns_are_cleaned_too(monkeypatch, person):
    client = FakeClient([reply(say("Done."))])
    monkeypatch.setattr(agent, "client", lambda: client)
    with override_settings(**ENABLED, AI_PROVIDER="openai"):
        _answer(question=DIRTY, history=[{"question": f"Who? {DIRTY}", "answer": DIRTY}])
    earlier_question, earlier_answer, asked = client.requests[0]["input"]
    for message in (earlier_question, earlier_answer, asked):
        _clean(message["content"])


# ------------------------------------------------------------------------------------- the lookups
def test_a_knowledge_passage_and_its_summary_reach_the_ai_cleaned(claude, person):
    partner = PartnerOrganization.objects.create(etl_id="7", name="Dr. Example Social Foundation")
    document = Document.objects.create(
        title="Education working group minutes",
        text=f"Dropout notes. {DIRTY}",
        summary=f"Minutes of the group. {DIRTY}",
        status=Document.Status.READY,
    )
    indexing.index(document)
    Link.objects.create(document=document, kind="partner", object_id=partner.pk, label=partner.name)
    server = _one_lookup(claude, "search_knowledge", {"query": "dropout"})
    _answer()

    result = _result(server)
    (passage,) = result["passages"]
    _clean(passage["text"])
    (listed,) = result["documents"]
    _clean(listed["summary"])
    # NeuroDB's own links, the title and the partner the document is linked to stay as they are
    assert passage["url"] == listed["url"] == document.get_absolute_url()
    assert passage["title"] == "Education working group minutes"
    assert listed["linked_to"] == [
        {"kind": "partner", "name": "Dr. Example Social Foundation", "url": f"/partners/{partner.pk}/"}
    ]
    assert result["url"] == reverse("knowledge:index")


def test_a_document_read_whole_keeps_its_lines_and_loses_a_name_split_over_two(claude, person):
    document = Document.objects.create(
        title="Field visit note",
        text=f"Visit to Akkar.\nPrepared by Rania\nPlantedname.\n\n{DIRTY}",
        status=Document.Status.READY,
    )
    indexing.index(document)
    server = _one_lookup(claude, "read_knowledge", {"document_id": document.pk})
    _answer()

    read = _result(server)
    assert read["text"].startswith("Visit to Akkar.\nPrepared by [name withheld].\n\n")
    _clean(read["text"])
    assert read["url"] == document.get_absolute_url() and read["document_id"] == document.pk


def test_a_document_review_quote_reaches_the_ai_cleaned(claude, person):
    batch = ReviewBatch.objects.create(name="Annual reports")
    document = Document.objects.create(
        title="Annual report 2025", text="x", status=Document.Status.READY, review_batch=batch
    )
    DocumentFinding.objects.create(
        document=document,
        category=FindingCategory.CHALLENGE,
        topic=Topic.objects.first(),
        text=f"Schools lack teachers. {DIRTY}",
        quote=f"As the report says, {DIRTY}",
        page_label="12",
        place_text="Sheikh Taba",
        evidence=80,
    )
    server = _one_lookup(claude, "search_document_findings", {"query": "teachers"})
    _answer()

    (finding,) = _result(server)["findings"]
    _clean(finding["quote"])
    _clean(finding["finding"])
    assert finding["place"] == "Sheikh Taba"  # a place's name: kept
    assert (
        finding["document"] == "Annual report 2025" and finding["page"] == "12" and finding["evidence"] == 80
    )
    assert finding["url"].startswith(reverse("knowledge:review"))


def test_a_staff_comment_reaches_the_ai_cleaned_and_its_figures_stay(claude, hierarchy, person):
    report = hierarchy["report"]
    NeuroReportComment.objects.create(report=report, comment=DIRTY, related_month="01")
    expected = tools.run("neuro_report", {"report_id": report.id, "month": 3})
    server = _one_lookup(claude, "neuro_report", {"report_id": report.id, "month": 3})
    _answer()

    sent = _result(server)
    (comment,) = sent["comments"]
    _clean(comment["text"])
    assert comment["month"] == "01"
    # every figure, label, period and link of the report reaches the model as the lookup returned it
    assert sent["sections"] == expected["sections"]
    assert {k: v for k, v in sent.items() if k != "comments"} == {
        k: v for k, v in expected.items() if k != "comments"
    }


def test_a_daily_review_detail_reaches_the_ai_cleaned(claude, person):
    review = DailyReview.objects.create(
        date=datetime.date(2026, 11, 18), status="succeeded", summary=f"Calm day. {DIRTY}"
    )
    ReviewFinding.objects.create(
        review=review,
        key="reports_overdue:LEB/PCA2026001/PD2026001",
        check_id="reports_overdue",
        severity=ReviewFinding.Severity.CRITICAL,
        section="Child Protection",
        title="Two progress reports are overdue",
        detail=f"Two reports are late. {DIRTY}",
        url="/programmes/12/",
    )
    server = _one_lookup(claude, "daily_review", {})
    _answer()

    sent = _result(server)
    _clean(sent["summary"])
    (finding,) = sent["findings"]
    _clean(finding["detail"])
    assert finding["title"] == "Two progress reports are overdue" and finding["section"] == "Child Protection"
    assert finding["url"] == "/programmes/12/" and sent["url"] == reverse("reports:daily_review")
    assert sent["date"] == "2026-11-18"


def test_partner_names_survive_a_lookup(claude, person):
    PartnerOrganization.objects.create(
        etl_id="8", name="Dr. Example Social Foundation", short_name="DESF", vendor_number="2500212345"
    )
    server = _one_lookup(claude, "search_partners", {"text": "Example"})
    _answer()

    (partner,) = _result(server)["partners"]
    assert partner["name"] == "Dr. Example Social Foundation" and partner["short_name"] == "DESF"
    assert partner["url"] == f"/partners/{partner['partner_id']}/"


def test_a_result_keeps_links_ids_numbers_dates_codes_and_labels():
    found = {
        "partner": "Sheikh Example Association",
        "name": "Dr. Example Social Foundation",
        "section": "Child Protection",
        "title": "Annual report 2025",
        "number": "LEB/PCA2021123/PD2021456",
        "vendor_number": "2500212345",
        "awp_code": "1.2.3",
        "date": "2024-03-15",
        "period": "2023-2024",
        "share": "76.9%",
        "value": 71123456,  # a number as a number: never changed
        "target": 1250.5,
        "active": True,
        "missing": None,
        "url": "/partners/3/",
        "editions_url": "/knowledge/reports/?series=2",
        "urls": ["/programmes/4/"],
        "linked_to": [{"kind": "partner", "name": "Sheikh Example Association", "url": "/partners/3/"}],
        "text": "Dr. Plantedthree and Mrs Plantedfour met Sheikh Example Association",
        "elsewhere": {"url": "https://example.org/x", "name": "External page"},
        "off_site": {"url": "//example.org/x"},
    }
    sent = privacy.result("search_partners", found, frozenset())
    assert sent == {
        **found,
        "text": "[name withheld] and [name withheld] met [name withheld]",  # a text: any title
        "elsewhere": {"url": "[link withheld]", "name": "External page"},
        "off_site": {"url": "//[link withheld]"},
    }
    assert privacy.result("search_partners", sent, frozenset()) == sent


def test_the_etools_lookups_are_not_cleaned_twice(monkeypatch):
    seen = []
    monkeypatch.setattr(privacy, "text", lambda value, *args, **kwargs: seen.append(value) or value)
    record = {"records": [{"remarks": "Already cleaned by the Datamart: [name withheld]"}]}
    for name in ("etools_query", "etools_record", "etools_search", "etools_datasets"):
        assert privacy.result(name, record, frozenset()) is record
    assert seen == []


def test_a_chart_takes_only_numbers_the_model_was_sent(claude, monkeypatch, db):
    def lookup():
        return {"phone_like": "71123456", "schools": 5, "note": f"Call {PHONE}"}

    monkeypatch.setitem(tools.TOOLS, "data_freshness", (lookup, *tools.TOOLS["data_freshness"][1:]))
    chart = {"kind": "bar", "title": "x", "labels": ["a"], "series": [{"name": "s", "values": [71123456]}]}
    allowed = {**chart, "series": [{"name": "s", "values": [5]}]}
    server = claude(
        turn([tool_use("toolu_1", "data_freshness", "{}")], stop_reason="tool_use"),
        turn([tool_use("toolu_2", "make_chart", json.dumps(chart))], stop_reason="tool_use"),
        turn([tool_use("toolu_3", "make_chart", json.dumps(allowed))], stop_reason="tool_use"),
        turn([text("Done.")]),
    )
    outcome, _ = _answer()

    assert _result(server) == {
        "phone_like": "[phone withheld]",
        "schools": 5,
        "note": "Call [phone withheld]",
    }
    assert [(t["tool"], t["ok"]) for t in outcome.tools] == [
        ("data_freshness", True),
        ("make_chart", False),  # 71123456 was never sent: it cannot be drawn
        ("make_chart", True),
    ]
    assert outcome.numbers == {5.0}


def test_a_lookup_error_reaches_the_ai_cleaned(claude, person):
    server = _one_lookup(claude, "no_such_tool", {"text": DIRTY})
    _answer()
    (block,) = server.requests[1]["body"]["messages"][-1]["content"]
    error = json.loads(block["content"])
    assert block["is_error"] is True and error["error"] == "Unknown tool no_such_tool."
    _clean(error["received"]["text"])


# --------------------------------------------------------------------- the other features keep theirs
def test_a_run_with_options_sends_its_question_turns_and_results_as_before(claude, person):
    document = Document.objects.create(
        title="Minutes", text=f"Dropout notes. {DIRTY}", status=Document.Status.READY
    )
    indexing.index(document)
    server = _one_lookup(claude, "search_knowledge", {"query": "dropout"})
    history = [{"question": f"Who? {DIRTY}", "answer": DIRTY}]
    _answer(question=DIRTY, history=history, options=agent.RunOptions(tools=("search_knowledge",)))

    earlier_question, earlier_answer, asked = server.requests[0]["body"]["messages"]
    assert (earlier_question["content"], earlier_answer["content"], asked["content"]) == (
        f"Who? {DIRTY}",
        DIRTY,
        DIRTY,
    )
    (passage,) = _result(server)["passages"]
    assert DIRTY in passage["text"]  # a run with options has its own filter, or none


def test_the_help_assistant_sends_its_question_as_before(claude, viewer, person):
    server = claude(turn([text("Done.")]))
    run = help_assistant.options(viewer, "The page: Home.")
    _answer(question=DIRTY, history=[{"question": f"Who? {DIRTY}", "answer": DIRTY}], options=run)
    assert [m["content"] for m in server.requests[0]["body"]["messages"]] == [f"Who? {DIRTY}", DIRTY, DIRTY]


def test_the_monitoring_insights_chat_filter_is_its_own(monkeypatch):
    """Ask's filter never runs on a run with options: Monitoring insights' chat passes its own."""
    called = []
    monkeypatch.setattr(privacy, "result", lambda *a, **k: called.append(a) or a[1])
    monkeypatch.setitem(tools.TOOLS, "data_freshness", (lambda: {"x": 1}, *tools.TOOLS["data_freshness"][1:]))
    own = agent.RunOptions(
        tools=("data_freshness",), tool_filter=lambda name, result: {**result, "own": True}, read_only=False
    )
    assert agent._lookup("data_freshness", {}, own) == {"x": 1, "own": True}
    assert called == []
