"""Ask NeuroDB takes the people it can find out of what it sends the AI provider: the question, the earlier
questions and answers and every lookup's result reach the model without e-mail addresses (also written out
or spaced), links, phone numbers, the person names NeuroDB knows or names written after a title, while
NeuroDB's own page links, partner and place names, numbers, amounts, dates and codes stay. A long text is
cleaned whole before it is cut. Help, Monitoring insights and NeuroDB Watch keep their
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


def _said(message) -> str:
    """A message's text, sent as a string or as blocks (a block marked for the cache)."""
    content = message["content"]
    return content if isinstance(content, str) else "".join(block.get("text", "") for block in content)


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
    _clean(_said(earlier_question))  # sent as a block marked for the cache
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
    sent = privacy.result("partner_details", found, frozenset())  # one partner: its name is a partner's
    assert sent == {
        **found,
        "text": "[name withheld] and [name withheld] met [name withheld]",  # a text: any title
        "elsewhere": {"url": "[link withheld]", "name": "External page"},
        "off_site": {"url": "//[link withheld]"},
    }
    assert privacy.result("partner_details", sent, frozenset()) == sent


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


# ------------------------------------------------------------ spaces and line breaks as they were written
@pytest.mark.parametrize(
    ("written", "placeholder"),
    [
        ("Tel 03  123456", "[phone withheld]"),
        ("Tel 01 /  345678", "[phone withheld]"),
        ("Tel 0933  123 456", "[phone withheld]"),
        ("Mob. 76  123 456", "[phone withheld]"),
        ("or 71  12 34 56 today", "[phone withheld]"),
        ("Tel 03\n\n123456", "[phone withheld]"),
        ("Tel 03 \n123456", "[phone withheld]"),
        ("rania . plantedfive  @  example . org", "[email withheld]"),
        ("rania.plantedfive  @  example . org", "[email withheld]"),
        ("r  .  plantedfive  at  example  .  org", "[email withheld]"),
        ("rania.plantedfive\n@\nexample.org", "[email withheld]"),
    ],
)
def test_a_phone_or_address_written_with_more_spaces_or_over_lines_is_taken_out(written, placeholder):
    sent = privacy.text(f"Ask the head teacher: {written}.\n\nAkkar schools reopened.", frozenset())
    assert placeholder in sent, sent
    assert not any(leak in sent for leak in ("123", "345678", "plantedfive", "12 34")), sent
    assert sent.endswith(".\n\nAkkar schools reopened.")  # the line breaks around it stay
    assert privacy.text(sent, frozenset()) == sent


def test_spaced_phones_and_addresses_never_reach_the_ai(claude, db):
    server = claude(turn([text("Done.")]))
    history = [
        {
            "question": "Who runs it? Tel 03\n\n123456",
            "answer": "Write to r  .  plantedfive  at  example  .  org.",
        }
    ]
    _answer(question="Ask the head teacher on 03  123456 or 71  12 34 56 about Akkar.", history=history)
    sent = [_said(m) for m in server.requests[0]["body"]["messages"]]
    assert sent == [
        "Who runs it? Tel [phone withheld]",
        "Write to [email withheld].",
        "Ask the head teacher on [phone withheld] or [phone withheld] about Akkar.",
    ]


# --------------------------------------------------- a name after Dr or Sheikh: kept only for a partner or place
def test_a_doctors_name_in_a_documents_title_or_source_is_taken_out(claude, db):
    partner = PartnerOrganization.objects.create(etl_id="9", name="Dr. Example Social Foundation")
    document = Document.objects.create(
        title="Interview notes, Dr. Hiba Plantedthree, Akkar clinic",
        source="Dr Hiba Plantedthree, school doctor",
        text="Dropout notes from the clinic.",
        summary="Dropout notes.",
        status=Document.Status.READY,
    )
    indexing.index(document)
    Link.objects.create(document=document, kind="partner", object_id=partner.pk, label=partner.name)
    server = _one_lookup(claude, "search_knowledge", {"query": "dropout"})
    _answer()

    result = _result(server)
    assert "Plantedthree" not in json.dumps(result)
    (passage,) = result["passages"]
    assert passage["title"] == "Interview notes, [name withheld], Akkar clinic"
    (listed,) = result["documents"]
    assert listed["source"] == "[name withheld], school doctor"
    # the partner the document is linked to keeps its name
    assert listed["linked_to"][0]["name"] == "Dr. Example Social Foundation"


def test_a_library_title_that_names_a_doctor_is_taken_out(claude, db):
    from neurodb.library.models import Resource

    Resource.objects.create(
        title="Learning study by Dr. Hiba Plantedthree", publication_year="2025", section="Education"
    )
    server = _one_lookup(claude, "search_library", {"text": "learning"})
    _answer()
    (resource,) = _result(server)["resources"]
    assert resource["title"] == "Learning study by [name withheld]"


def test_only_partners_and_places_keep_a_name_after_dr_or_sheikh():
    found = {
        "report": "Report by Dr. Rami Plantedsix",
        "category": "Dr Rami Plantedsix",
        "topic": "Sheikh Omar Plantedsix's school committee",
        "indicator": "Doctor Rania Plantedsix follow-up",
        "title": "Field notes of Dr. Rami Plantedsix",
        "Doctor Rania Plantedsix": 3,
        "partners": [{"name": "Dr. Example Social Foundation", "short_name": "DESF"}],
        "linked_to": [{"kind": "partner", "name": "Sheikh Example Association"}],
        "entities": [{"kind": "governorate", "key": "g1", "name": "Sheikh Example Place"}],
        "partner_name": "Sheikh Example Charity",
        "by_partner": {"Dr. Example Social Foundation": 4},
        "governorate": "Sheikh Example Place",
    }
    sent = privacy.result("search_partners", found, frozenset())
    assert sent == {
        "report": "Report by [name withheld]",
        "category": "[name withheld]",
        "topic": "[name withheld] school committee",
        "indicator": "[name withheld] follow-up",
        "title": "Field notes of [name withheld]",
        "[name withheld]": 3,
        "partners": [{"name": "Dr. Example Social Foundation", "short_name": "DESF"}],
        "linked_to": [{"kind": "partner", "name": "Sheikh Example Association"}],
        "entities": [{"kind": "governorate", "key": "g1", "name": "Sheikh Example Place"}],
        "partner_name": "Sheikh Example Charity",
        "by_partner": {"Dr. Example Social Foundation": 4},
        "governorate": "Sheikh Example Place",
    }
    # partner_details is one partner: its own name and short name are a partner's
    details = privacy.result("partner_details", {"name": "Dr. Example Social Foundation"}, frozenset())
    assert details == {"name": "Dr. Example Social Foundation"}
    assert privacy.result("search_partners", {"name": "Dr. Rami Plantedsix"}, frozenset()) == {
        "name": "[name withheld]"
    }


# ---------------------------------------------------------- an earlier answer keeps the partners it named
def test_an_earlier_answer_keeps_the_partners_named_after_dr_or_sheikh_and_a_question_does_not(claude, db):
    answer = (
        "Two partners:\n\n| Partner | Amount |\n|---|---|\n"
        "| [Dr. Example Social Foundation](/partners/3/) | 1,000 |\n| Sheikh Example Charity | 2,000 |\n\n"
        "Mrs Layla Plantedseven, 03 123 456, wrote it."
    )
    history = [{"question": "Which partners? Ask Dr. Rami Plantedsix.", "answer": answer}]
    server = claude(turn([text("Done.")]))
    _answer(question="What PDs does the second one have?", history=history)
    earlier_question, earlier_answer, _asked = (_said(m) for m in server.requests[0]["body"]["messages"])
    assert earlier_question == "Which partners? Ask [name withheld]."
    assert "| [Dr. Example Social Foundation](/partners/3/) | 1,000 |" in earlier_answer
    assert "| Sheikh Example Charity | 2,000 |" in earlier_answer
    assert earlier_answer.endswith("[name withheld], [phone withheld], wrote it.")  # people still go


# --------------------------------------------------------------------- amounts and counts are not phones
@pytest.mark.parametrize(
    "written",
    [
        "The grant covers LBP 76 500 000",
        "USD 71.500.000 in 2025",
        "LBP 71500000 was paid",
        "Paid 71 500 000 LBP",
        "a cost of $ 78 123 456",
        "Reached 81 250 300 people",
        "76 543 210 children",
    ],
)
def test_an_amount_beside_a_currency_or_a_count_is_not_a_phone(written):
    assert privacy.text(written, frozenset()) == written


@pytest.mark.parametrize(
    "written", ["Call 71 123 456 now", "Population 76543210", "Tel. LBP office 03 123 456"]
)
def test_a_number_with_a_phones_shape_elsewhere_is_still_taken_out(written):
    assert "[phone withheld]" in privacy.text(written, frozenset())


def test_an_amount_under_an_amount_key_stays():
    sent = privacy.cleaned(
        {"amount": "71123456", "local_amount": "71123456", "note": "71123456"}, frozenset()
    )
    assert sent == {"amount": "71123456", "local_amount": "71123456", "note": "[phone withheld]"}


# ------------------------------------------------------------- cleaned whole, then cut
def test_a_long_staff_comment_is_cleaned_whole_before_it_is_cut(claude, hierarchy, person):
    report = hierarchy["report"]
    NeuroReportComment.objects.create(
        report=report, comment="y " * 494 + "Ask Rania Plantedname on 03 123 456 today.", related_month="01"
    )
    server = _one_lookup(claude, "neuro_report", {"report_id": report.id, "month": 3})
    _answer()
    (comment,) = _result(server)["comments"]
    assert len(comment["text"]) == 1000
    assert comment["text"].startswith("y y ") and comment["text"].endswith("Ask [name wi")
    assert "Rania" not in comment["text"] and "123" not in comment["text"]


def test_a_document_read_in_parts_never_splits_a_name_before_it_is_cleaned(claude, person):
    document = Document.objects.create(
        title="Field visit note",
        text="z " * 3997 + "Rania Plantedname, 03 123 456, runs the centre.",
        status=Document.Status.READY,
    )
    indexing.index(document)
    first = _one_lookup(claude, "read_knowledge", {"document_id": document.pk})
    _answer()
    second = _one_lookup(claude, "read_knowledge", {"document_id": document.pk, "part": 2})
    _answer()
    part_1, part_2 = _result(first), _result(second)
    assert (part_1["parts"], part_2["parts"]) == (2, 2)
    sent = part_1["text"] + part_2["text"]
    assert "Rania" not in sent and "Plantedname" not in sent and "123" not in sent
    assert sent.endswith("[name withheld], [phone withheld], runs the centre.")


def test_a_long_text_is_cut_as_written_outside_ask(person):
    """NeuroDB Watch and the other runs read the lookups as before: cut as written, cleaned by their own filter."""
    document = Document.objects.create(
        title="Field visit note",
        text="z " * 3997 + "Rania Plantedname, 03 123 456, runs the centre.",
        status=Document.Status.READY,
    )
    read = tools.run("read_knowledge", {"document_id": document.pk})
    assert read["text"].endswith("Rania ")


# ------------------------------------------------------------------------------------ written-out addresses
def test_no_written_out_address_reaches_ask_neurodb(monkeypatch, db):
    """The question and the earlier question (typed: every written-out form), the earlier answer and a
    knowledge passage and a daily review detail (the record rule), as the model is sent them, hold no part
    of an address."""
    from tests.assistant.test_assistant import call
    from tests.written_addresses import FORMS, RECORD, RECORD_FORMS, TYPED, leaks

    document = Document.objects.create(title="Minutes", text="Dropout notes.", status=Document.Status.READY)
    review = DailyReview.objects.create(date=datetime.date(2026, 11, 18), status="succeeded", summary="Calm.")
    finding = ReviewFinding.objects.create(
        review=review,
        key="reports_overdue:X",
        check_id="reports_overdue",
        severity=ReviewFinding.Severity.CRITICAL,
        section="Education",
        title="Two progress reports are overdue",
        detail="x",
        url="/programmes/12/",
    )
    found = {}
    for form, written in FORMS.items():
        typed = TYPED.format(written)
        record = RECORD.format(written if form in RECORD_FORMS else FORMS["F00 plain @"])
        document.text = f"Dropout notes. {record}"
        document.save()
        indexing.index(document)
        finding.detail = record
        finding.save()
        client = FakeClient(
            [
                reply(call("search_knowledge", {"query": "dropout"}, "c1"), call("daily_review", {}, "c2")),
                reply(say("Done.")),
            ]
        )
        monkeypatch.setattr(agent, "client", lambda client=client: client)
        with override_settings(**ENABLED, AI_PROVIDER="openai"):
            _answer(question=typed, history=[{"question": typed, "answer": record}])
        earlier_question, earlier_answer, asked = client.requests[0]["input"]
        knowledge, daily = [
            json.loads(i["output"])
            for i in client.requests[1]["input"]
            if i.get("type") == "function_call_output"
        ]
        sent = {
            "question": asked["content"],
            "earlier question": earlier_question["content"],
            "earlier answer": earlier_answer["content"],
            "knowledge passage": " ".join(p["text"] for p in knowledge["passages"]),
            "daily review detail": daily["findings"][0]["detail"],
        }
        assert knowledge["passages"], form
        found |= {f"{form} {where}": text for where, text in leaks(sent).items()}
    assert not found, json.dumps(found, ensure_ascii=False, indent=1)


def test_a_record_text_loses_a_written_out_address_and_keeps_its_sentences_in_ask():
    """Ask's cleaner (a record's text, an earlier answer): an address written out goes whole, over two lines
    too, and a sentence that only looks like one keeps its words."""
    from tests.written_addresses import NOT_ADDRESSES, RECORD, RECORD_FORMS

    for form, written in RECORD_FORMS.items():
        assert privacy.text(RECORD.format(written), frozenset()) == RECORD.format("[email withheld]"), form
        assert privacy.text(RECORD.format(written), frozenset(), label=True) == RECORD.format(
            "[email withheld]"
        )
    for text_, sent in NOT_ADDRESSES.items():
        assert privacy.text(text_, frozenset()) == sent
    assert privacy.text("Write to ZFAKEMIRA AT\nEXAMPLE.ORG.\n\nAkkar.", frozenset()) == (
        "Write to [email withheld].\n\nAkkar."
    )
    assert privacy.text("Write to zayna_fakemira  at  example.invalid now", frozenset()) == (
        "Write to [email withheld] now"
    )


def test_addresses_joined_by_a_slash_or_before_a_path_go_whole_in_ask_and_the_raw_records(db):
    """Ask's cleaner, the Raw eTools data page and its download (``raw_people.public_record``) and the
    Monitoring insights cleaner: a list of focal points joined by "/", an address before a path or a web
    address's "www.", a spelled dot ("-dot-", "(dot)", "[dot]"): every name before "at" goes."""
    from neurodb.datamart.raw_people import public_record
    from neurodb.fmm import privacy as fmm_privacy
    from tests.written_addresses import JOINED, left

    for text_ in JOINED:
        shown, _withheld = public_record(
            {"status": text_}, "pd_activities", frozenset(), reviewed=frozenset({"status"})
        )
        sent = {
            "Ask": privacy.text(text_, frozenset()),
            "Ask, an earlier answer": privacy.text(text_, frozenset(), label=True),
            "Raw eTools data": json.dumps(shown, ensure_ascii=False),
            "Monitoring insights": fmm_privacy.clean(text_, 1000, frozenset())[0],
        }
        assert not {where: t for where, t in sent.items() if left(t)}, (text_, sent)
    assert privacy.text("Focal points: zfakemira at example.org/zaynafake at example.org.", frozenset()) == (
        "Focal points: [email withheld]/[email withheld]."
    )
    from tests.written_addresses import TYPED_NOT_ADDRESSES

    for text_, sent_ in TYPED_NOT_ADDRESSES.items():  # a question that holds no address is sent as typed
        assert privacy.question(text_, frozenset()) == sent_, text_
    from tests.written_addresses import RECORD_ADDRESSES, TYPED_ADDRESSES

    for text_, sent_ in TYPED_ADDRESSES.items():
        assert privacy.question(text_, frozenset()) == sent_, text_
        assert help_assistant.clean(text_, 1000, frozenset()) == sent_, text_
    for text_, sent_ in RECORD_ADDRESSES.items():
        assert privacy.text(text_, frozenset()) == sent_, text_
