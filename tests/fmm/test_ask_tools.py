"""The four field monitoring look-ups in Ask NeuroDB (stage 7): registered into the assistant's tools when
the app starts, named in Ask's prompt, and, without a chat's context, giving structured fields only for
this calendar year in the whole country: no note, no answer, no snippet, never a person."""

import datetime
import json

import pytest
from django.test import override_settings

from neurodb.assistant import agent
from neurodb.assistant import tools as assistant_tools
from neurodb.fmm import scope as scope_module
from neurodb.fmm.ai import tools
from tests.fmm.conftest import CANARIES, LEAD, MEMBER, MEMBER_EMAIL

FM_TOOLS = ("fm_summary", "fm_visits", "fm_visit", "fm_search")
NARRATIVE_WORDS = ("Classes held", "registers up to date", "delayed and suspended", "Registers checked")


@pytest.fixture
def in_2026(monkeypatch):
    """Ask's default scope is this calendar year: the fixture's visits end in 2026."""
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or datetime.date(2026, 10, 5))


def _blob(result) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def _no_person(blob: str) -> None:
    for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL):
        assert canary not in blob, canary
    assert '"team' not in blob and "visit_lead" not in blob and "@" not in blob


def test_the_four_look_ups_are_registered_and_offered():
    assert set(FM_TOOLS) <= set(assistant_tools.TOOLS)
    names = [d["name"] for d in assistant_tools.definitions()]
    assert set(FM_TOOLS) <= set(names)
    assert [assistant_tools.label(name) for name in FM_TOOLS] == [
        "Counting monitoring visits",
        "Listing monitoring visits",
        "Reading a monitoring visit",
        "Searching visit notes",
    ]
    assert [d["name"] for d in tools.definitions()] == list(FM_TOOLS)
    for name in FM_TOOLS:  # no word staff pages never use
        assert not any(w in assistant_tools.TOOLS[name][1].lower() for w in (" item", "agent", "llm"))


def test_asks_prompt_names_the_look_ups():
    line = next(line for line in agent.SYSTEM_PROMPT.splitlines() if "fm_summary" in line)
    assert "Field monitoring visits" in line
    for name in FM_TOOLS:
        assert name in agent.SYSTEM_PROMPT
    assert "the visit notes themselves are read in Monitoring insights" in agent.SYSTEM_PROMPT
    for name in ("etools_datasets", "etools_query", "etools_search", "etools_record"):
        assert "for visits use fm_visits / fm_visit" in assistant_tools.TOOLS[name][1], name


def test_without_a_chat_they_cover_this_year_in_the_whole_country(built, in_2026):
    assert not tools.bound()
    ctx = tools.current()
    assert (ctx.scope.preset, ctx.scope.start.year, ctx.scope.sections, ctx.scope.governorate) == (
        "this_year",
        2026,
        (),
        "",
    )
    assert (ctx.texts_left, ctx.cards_max) == (0, tools.ASK_CARDS)
    summary = assistant_tools.run("fm_summary", {})
    from neurodb.fmm import metrics

    k = metrics.kpis(ctx.scope)
    assert summary["visits"] == k["visits"] > 0 and summary["entities"] == k["entities"]
    assert summary["avg_quality"] == float(k["avg_quality"])
    assert summary["url"].startswith("/fmm/?") and "section=" in summary["url"]


def test_they_give_no_note_answer_or_snippet_and_no_person(built, in_2026):
    listed = assistant_tools.run("fm_visits", {"limit": 30})
    assert listed["total"] == listed["shown"] > 0
    assert all(v["url"].startswith("/fmm/visits/") for v in listed["visits"])
    one = assistant_tools.run("fm_visit", {"visit": "Visit 1722"})
    assert one["key"] == "visit:1722" and one["url"] == "/fmm/visits/1722/"
    narratives = [e["narrative"] for e in one["entities"]]
    assert narratives and set(narratives) == {tools.NARRATIVES_ELSEWHERE}
    answered = [a for a in one["answers"] if a["answered"]]
    assert answered and {a["answer"] for a in answered} == {tools.ANSWERS_ELSEWHERE}
    assert {a["code"] for a in one["answers"]} >= {"on_track", "no"}  # the structured codes stay
    found = assistant_tools.run("fm_search", {"text": "delayed"})
    assert found["matches"] and not any("snippet" in m for m in found["matches"])
    assert found["matches"][0]["url"] == "/fmm/visits/1726/"
    for result in (listed, one, found, assistant_tools.run("fm_summary", {"group_by": "partner"})):
        blob = _blob(result)
        _no_person(blob)
        for words in NARRATIVE_WORDS:
            assert words not in blob, words


def test_a_search_for_a_person_is_refused(built, in_2026):
    from neurodb.watch import people

    people.forget()  # the team names are known once the visits are built (the refresh's commit)
    assert assistant_tools.run("fm_search", {"text": MEMBER}) == {"error": tools.NO_PEOPLE}
    assert assistant_tools.run("fm_search", {"text": MEMBER_EMAIL}) == {"error": tools.NO_PEOPLE}
    with pytest.raises(assistant_tools.ToolInputError):
        assistant_tools.run("fm_search", {"text": "ab"})


def test_every_group_of_the_summary(built, in_2026):
    for group in (
        "section",
        "governorate",
        "office",
        "partner",
        "month",
        "rating",
        "rule",
        "entity_type",
        "status",
    ):
        out = assistant_tools.run("fm_summary", {"group_by": group})
        assert out["groups"], group
        if group != "rule":
            assert sum(g["visits"] for g in out["groups"]) >= out["visits"], group
    by_partner = assistant_tools.run("fm_summary", {"group_by": "partner"})["groups"]
    assert {g["group"] for g in by_partner} == {"Amel Association", "Mercy Corps Lebanon"}
    by_month = [g["code"] for g in assistant_tools.run("fm_summary", {"group_by": "month"})["groups"]]
    assert by_month == sorted(by_month)


def test_unknown_names_go_back_to_the_model_as_input_errors(built, in_2026):
    with pytest.raises(assistant_tools.ToolInputError, match="sections with visits"):
        assistant_tools.run("fm_visits", {"section": "Nutrition"})
    with pytest.raises(assistant_tools.ToolInputError, match="governorates"):
        assistant_tools.run("fm_visits", {"governorate": "Atlantis"})
    with pytest.raises(assistant_tools.ToolInputError, match="partner"):
        assistant_tools.run("fm_visits", {"partner": "Nobody at all"})
    with pytest.raises(assistant_tools.ToolInputError, match="YYYY-MM-DD"):
        assistant_tools.run("fm_summary", {"period_from": "May"})
    assert assistant_tools.run("fm_visits", {"partner": "AMEL"})["total"] > 0
    assert assistant_tools.run("fm_visits", {"governorate": "Beqaa"})["total"] > 0  # a gazetteer spelling


def test_switched_off_at_call_time_gives_an_error(built, in_2026):
    with override_settings(FMM_ENABLED=False):
        for name, args in (("fm_summary", {}), ("fm_visits", {}), ("fm_visit", {"visit": "1722"})):
            assert assistant_tools.run(name, args) == {"error": tools.SWITCHED_OFF}
        assert assistant_tools.run("fm_search", {"text": "delayed"}) == {"error": tools.SWITCHED_OFF}


def test_a_visit_that_is_not_there(built, in_2026):
    assert "No visit 1799" in assistant_tools.run("fm_visit", {"visit": "1799"})["error"]
