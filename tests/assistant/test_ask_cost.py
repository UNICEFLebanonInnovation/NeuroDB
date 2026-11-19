"""Ask NeuroDB sends less new text on each call, without losing what answers need: a long list of PD
indicators leaves out each row's detail (``detail=true`` brings it back), later parts of a document leave out
what part 1 gave, partner reports' rows leave out their default source, every lookup's result is compact JSON
(Arabic as it is written), the older earlier answers of a follow-up are shortened to their first paragraph,
and the instructions ask for every lookup that needs no other lookup's result in the same round. Help,
Monitoring insights and NeuroDB Watch keep their own earlier turns. Every name here is made up."""

import datetime
import json

import pytest
from django.conf import settings
from django.test import override_settings

from neurodb.assistant import agent, tools
from neurodb.help import assistant as help_assistant
from neurodb.knowledge.models import Document, Link
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.assistant.anthropic_mock import text, tool_use, turn
from tests.assistant.test_assistant import ENABLED, FakeClient, call, reply, say
from tests.reports.test_pd_monitoring import data, frozen_today, report_rows  # noqa: F401 - fixtures

# each row's detail, left out of a long list of PD indicators unless detail=true asks for it
DETAIL = {"by_month", "by_year", "locations", "tags", "cp_output", "output", "method"}
# the flags a long list gives only when they are true
FLAGS = {"differs_from_prp_cumulative", "partial_history"}
# what every row keeps: the figures, their status and the link to the indicator's page
KEPT = {
    "programme_document",
    "partner",
    "section",
    "indicator",
    "emergency",
    "unit",
    "baseline",
    "target",
    "reached",
    "reached_as_of",
    "awaiting_acceptance",
    "reports_awaiting_acceptance",
    "achieved_percent",
    "status",
    "value_status",
    "partner_assessment",
    "counted_reports",
    "report_type",
    "prp_cumulative",
    "year",
    "reached_in_year",
    "url",
}
TOTALS = (
    "filters",
    "indicators",
    "programme_documents",
    "partners",
    "status_counts",
    "indicators_with_reached",
    "overdue_reports",
    "rule",
    "total_rows",
    "truncated",
    "url",
)


# --------------------------------------------------------------------------- narrower rows, not fewer
def test_a_long_list_of_pd_indicators_leaves_out_each_rows_detail(data, frozen_today, monkeypatch):  # noqa: F811 - fixtures
    asked = {"partner": "amel", "year": 2026}
    whole = tools.run("pd_indicator_progress", asked)
    assert len(whole["indicators_detail"]) == 2 and "left_out" not in whole  # a short list comes whole
    assert all(DETAIL <= set(row) for row in whole["indicators_detail"])

    monkeypatch.setattr(tools, "DETAIL_ROWS", 1)  # now the same two rows make a long list
    narrow = tools.run("pd_indicator_progress", asked)
    assert {key: narrow[key] for key in TOTALS} == {key: whole[key] for key in TOTALS}  # totals kept
    assert len(narrow["indicators_detail"]) == 2  # narrower rows, not fewer
    for full, row in zip(whole["indicators_detail"], narrow["indicators_detail"], strict=True):
        assert not DETAIL & set(row), row
        assert KEPT <= set(row)
        assert {k: v for k, v in row.items() if k not in FLAGS} == {
            k: v for k, v in full.items() if k not in DETAIL | FLAGS
        }
        for flag in FLAGS:  # given only when true
            assert (flag in row) is bool(full[flag]) and row.get(flag, False) == full[flag]
    assert "detail=true" in narrow["left_out"] and "by_month" in narrow["left_out"]

    detailed = tools.run("pd_indicator_progress", {**asked, "detail": True})
    assert detailed == whole  # detail=true: every row whole, as before


def test_a_narrow_row_keeps_a_flag_that_is_true():
    row = {
        "reached": 5.0,
        "differs_from_prp_cumulative": True,
        "partial_history": False,
        "by_month": {"03": 5},
    }
    assert tools._narrow_indicator(row) == {"reached": 5.0, "differs_from_prp_cumulative": True}


def test_the_model_is_told_how_to_ask_for_the_detail():
    _function, description, schema, _label = tools.TOOLS["pd_indicator_progress"]
    assert schema["properties"]["detail"]["type"] == "boolean"
    assert "detail" not in schema["required"]
    assert f"more than {tools.DETAIL_ROWS} indicators" in description and "detail=true" in description
    assert "per month" in description  # still promised, now with how to get it


def _document(parts: int = 2) -> Document:
    partner = PartnerOrganization.objects.create(etl_id="31", name="Cedar Relief")
    document = Document.objects.create(
        title="Education working group minutes",
        text="Dropout fell. " * (parts * tools.KNOWLEDGE_PART // 14),
        summary="The group reviewed dropout.",
        key_points=["Dropout fell in the north."],
        status=Document.Status.READY,
    )
    Link.objects.create(document=document, kind="partner", object_id=partner.pk, label=partner.name)
    return document


def test_later_parts_of_a_document_leave_out_what_part_one_gave(db):
    document = _document()
    first = tools.run("read_knowledge", {"document_id": document.pk})
    assert first["summary"] == "The group reviewed dropout." and first["key_points"] == [
        "Dropout fell in the north."
    ]
    assert [link["name"] for link in first["linked_to"]] == ["Cedar Relief"]
    second = tools.run("read_knowledge", {"document_id": document.pk, "part": 2})
    assert not {"summary", "key_points", "linked_to"} & set(second)
    assert (second["title"], second["part"], second["parts"]) == (document.title, 2, first["parts"])
    assert second["text"] and second["url"] == first["url"] and second["note"] == tools.KNOWLEDGE_NOTE
    _function, description, _schema, _label = tools.TOOLS["read_knowledge"]
    assert "with part 1" in description


def test_partner_reports_rows_leave_out_their_default_source(db):
    partner = PartnerOrganization.objects.create(etl_id="1", name="Cedar Relief", vendor_number="V1")
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA2026010",
        title="Learning",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2027, 12, 31),
    )
    end = datetime.date(2026, 3, 31)
    report_rows(pd, partner, "# of children", "PR-1", "QPR1", "QPR", end, {"Beirut": 10}, 10)
    (row,) = tools.run("partner_reporting", {})["progress_reports"]
    assert row["report"] == "QPR1" and "source" not in row  # a partner report: the default source
    _function, description, _schema, _label = tools.TOOLS["partner_reporting"]
    assert "source ('PRP progress reports')" in description


# --------------------------------------------------------------------------------------- compact JSON
ARABIC = "مدرسة عكار"


@pytest.fixture
def lookup(monkeypatch):
    """data_freshness answers a list of figures and an Arabic name."""
    monkeypatch.setitem(
        tools.TOOLS,
        "data_freshness",
        (
            lambda: {"place": ARABIC, "values": [1200, 300], "url": "/data/"},
            *tools.TOOLS["data_freshness"][1:],
        ),
    )


def _compact(content: str) -> None:
    assert content == json.dumps(json.loads(content), ensure_ascii=False, separators=(",", ":"))
    assert ", " not in content and ": " not in content


def test_on_claude_a_lookups_result_is_compact_json_and_its_figures_are_still_checked(claude, db, lookup):
    server = claude(
        turn(
            [tool_use("toolu_1", "data_freshness", "{}"), tool_use("toolu_2", "no_such_tool", "{}")],
            stop_reason="tool_use",
        ),
        turn([text("Akkar had 1,200 and Bekaa 300.")]),
    )
    outcome = agent.Outcome()
    events = list(agent.answer("How fresh?", [], outcome))
    found, error = server.requests[1]["body"]["messages"][-1]["content"]
    _compact(found["content"])
    assert ARABIC in found["content"]  # not escaped
    assert json.loads(found["content"]) == {"place": ARABIC, "values": [1200, 300], "url": "/data/"}
    _compact(error["content"])  # an input error too
    assert error["is_error"] is True
    # each figure of the list is a figure looked up: none is listed under the answer
    assert events[-1]["type"] == "done" and "notice" not in events[-1]


@override_settings(**ENABLED, AI_PROVIDER="openai")
def test_on_openai_a_lookups_result_is_compact_json(monkeypatch, db, lookup):
    client = FakeClient([reply(call("data_freshness", {})), reply(say("Done."))])
    monkeypatch.setattr(agent, "client", lambda: client)
    list(agent.answer("How fresh?", [], agent.Outcome()))
    (output,) = [
        item["output"] for item in client.requests[1]["input"] if item.get("type") == "function_call_output"
    ]
    _compact(output)
    assert ARABIC in output


# ---------------------------------------------------------------------------------------- the history
LEAD = "Partners reached **1,240 children**, 78.4% of the target ([Partner monitoring](/monitoring/pd/))."
REST = "| Partner | Reached |\n|---|---|\n| Cedar Relief | 4,950 |\n\n- Two reports are overdue."


def _turns(n: int) -> list[dict]:
    return [
        {"question": f"Question {k} about Akkar?", "answer": f"{LEAD}\n\n{REST}"} for k in range(1, n + 1)
    ]


def test_the_latest_earlier_answer_goes_whole_and_older_ones_keep_their_first_paragraph():
    turns = _turns(3)
    shaped = agent.ask_history(turns)
    assert [t["question"] for t in shaped] == [t["question"] for t in turns]  # questions whole
    assert shaped[-1]["answer"] == f"{LEAD}\n\n{REST}"  # the latest whole
    for older in shaped[:-1]:
        assert older["answer"] == f"{LEAD}\n\n{agent.LEFT_OUT}"
    assert agent.ask_history(turns) == shaped  # the same turns give the same text
    assert turns[0]["answer"] == f"{LEAD}\n\n{REST}"  # the turns given are not changed


@pytest.mark.parametrize(
    ("answer", "kept"),
    [
        ("One paragraph only.", "One paragraph only."),  # nothing to leave out: unchanged
        ("Two lines\nof one paragraph.", "Two lines\nof one paragraph."),
        (
            "### Children reached\n\nThe answer.\n\nMore.",
            "### Children reached\n\nThe answer.\n\n<left out>",
        ),
        ("\n\nThe answer.  \n \n\nMore.", "The answer.\n\n<left out>"),
    ],
)
def test_an_older_answer_keeps_its_direct_answer(answer, kept):
    shaped = agent.ask_history(
        [{"question": "Q?", "answer": answer}, {"question": "Latest?", "answer": "A."}]
    )
    assert shaped[0]["answer"] == kept.replace("<left out>", agent.LEFT_OUT)


def test_ask_sends_the_shaped_turns_and_each_round_only_adds_to_them(claude, db, lookup):
    server = claude(
        turn([tool_use("toolu_1", "data_freshness", "{}")], stop_reason="tool_use"),
        turn([text("Done.")]),
    )
    list(agent.answer("And Bekaa?", _turns(3), agent.Outcome()))
    first, second = (r["body"]["messages"] for r in server.requests)
    assert [m["content"] for m in first] == [
        "Question 1 about Akkar?",
        f"{LEAD}\n\n{agent.LEFT_OUT}",
        "Question 2 about Akkar?",
        f"{LEAD}\n\n{agent.LEFT_OUT}",
        "Question 3 about Akkar?",
        f"{LEAD}\n\n{REST}",
        "And Bekaa?",
    ]
    assert second[: len(first)] == first  # append-only within the question's calls


@override_settings(**ENABLED, AI_PROVIDER="openai")
def test_on_openai_ask_sends_the_same_shaped_turns(monkeypatch, db):
    client = FakeClient([reply(say("Done."))])
    monkeypatch.setattr(agent, "client", lambda: client)
    list(agent.answer("And Bekaa?", _turns(2), agent.Outcome()))
    assert [m["content"] for m in client.requests[0]["input"]] == [
        "Question 1 about Akkar?",
        f"{LEAD}\n\n{agent.LEFT_OUT}",
        "Question 2 about Akkar?",
        f"{LEAD}\n\n{REST}",
        "And Bekaa?",
    ]


def test_a_figure_only_an_older_answers_left_out_part_gave_is_listed(claude, db):
    claude(turn([text("Cedar Relief reached 4,950 and 1,240 in all.")]))
    outcome = agent.Outcome()
    events = list(
        agent.answer(
            "And Cedar Relief?", _turns(2)[:1] + [{"question": "Q2?", "answer": "No figure."}], outcome
        )
    )
    assert events[-1]["notice"].endswith("4950.")  # the model was never sent the table that gave it
    assert "1240" not in events[-1]["notice"]  # the first paragraph was sent


def test_help_and_other_runs_keep_their_earlier_answers_whole(claude, viewer):
    server = claude(turn([text("Done.")]), turn([text("Done.")]))
    turns = _turns(3)
    list(
        agent.answer(
            "And?", turns, agent.Outcome(), options=help_assistant.options(viewer, "The page: Home.")
        )
    )
    list(agent.answer("And?", turns, agent.Outcome(), options=agent.RunOptions()))
    for request in server.requests:
        answers = [m["content"] for m in request["body"]["messages"] if m["role"] == "assistant"]
        assert answers == [f"{LEAD}\n\n{REST}"] * 3


# ------------------------------------------------------------------------------------ the instructions
def test_the_instructions_ask_for_every_independent_lookup_in_the_same_round():
    assert "Several lookups can run in parallel." not in agent.SYSTEM_PROMPT
    assert (
        "Run together, in the same round, every lookup that does not need another lookup's result; wait "
        "for a result only when the next lookup needs it."
    ) in agent.SYSTEM_PROMPT
    assert settings.AI_ASSISTANT_MAX_TOOL_ROUNDS == 8  # the limit of rounds stays
