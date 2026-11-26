"""Conversations with Ask the KM Database as people hold them (the question bench's root causes): a follow-up
after a question that matched nothing, "and <a group of people>?" of any length, "and by district?" after a
place, a switch to another subject ("what about vaccination?"), groups of people of the same kind that replace
each other, "anything newer?" and a short question of its own. Invented texts only; places are public
geography."""

from __future__ import annotations

import uuid

import pytest

from neurodb.km import ask
from neurodb.km.filters import Filters, facets
from neurodb.km.models import KMFinding, KMQuestion
from tests.km import factory
from tests.km.test_ask import (  # noqa: F401 (fixtures)
    RAIN_TURN,
    _ids,
    _people,
    _picked,
    _rain,
    _version,
    ai,
    answer,
    done,
    post,
    today,
)
from tests.km.test_ask_search import ids_of, load, read

pytestmark = pytest.mark.django_db


def turn(question: str, *cited: str, new_subject=()) -> dict:
    """An earlier turn as :func:`neurodb.km.ask.history` gives it."""
    return {
        "question": question,
        "answer": "Fake.",
        "cited": ids_of(*cited),
        "new_subject": [list(g) for g in new_subject],
    }


def searched(picked) -> set[str]:
    return {alt for g in picked.groups for alt in g}


# ---------------------------------------------------------------------- after a question that matched nothing
def test_the_history_holds_the_questions_that_matched_nothing_and_the_ai_is_never_sent_them(viewer):
    factory.load(factory.workbook(documents=1, findings=2))
    version = _version()
    conversation = uuid.uuid4()
    rows = (
        ("Q0", "A0", "answered", []),
        ("Q1", "", "no_match", [["stunting"]]),
        ("Q2", "", "failed", []),
        ("Q3", "A3", "answered", []),
    )
    for question, text, status, new_subject in rows:
        KMQuestion.objects.create(
            user=viewer,
            conversation=conversation,
            version=version,
            question=question,
            answer=text,
            status=status,
            new_subject=new_subject,
        )
    turns = ask.history(viewer, conversation, version)
    assert [t["question"] for t in turns] == ["Q0", "Q1", "Q3"]
    assert turns[1]["new_subject"] == [["stunting"]] and turns[1]["cited"] == []
    picked = ask.Picked(findings=list(KMFinding.objects.all()), question="Q?")
    message = ask.prompt("Q?", turns, Filters(), picked, _people(), facets(version))
    assert "Q: Q0\n" in message.text and "Q: Q3\n" in message.text and "Q: Q1\n" not in message.text


NOTHING = (
    ("2024", ["In Akkar, child labour rose.", "In Zahle, stunting affected 10% of children."]),
    ("2022", ["In Akkar, juvenile justice cases rose.", "In Akkar, child labour fell."]),
    ("2023", ["In Beirut, stunting fell to 8%."]),
)


def test_a_follow_up_after_no_match_keeps_its_subject_places_and_years(client_viewer, viewer, ai, today):  # noqa: F811
    load(*NOTHING)
    conversation = uuid.uuid4()
    justice = ids_of("In Akkar, juvenile justice cases rose.")
    fake = ai([answer(f"Fake [{justice[0]}].")])
    final = done(
        post(client_viewer, "juvenile justice in Tyre in 2024 as a table", conversation=conversation)
    )
    assert final["meta"].startswith("No finding within your filters matches your words.") and not fake.calls()
    # the earlier subject and place, never every finding of Akkar of 2022
    done(post(client_viewer, "thank you, same for Akkar in 2022 please", conversation=conversation))
    row = KMQuestion.objects.order_by("-created_at").first()
    assert row.status == "answered" and row.read == justice
    turns = ask.history(viewer, conversation, _version())
    assert [t["question"] for t in turns][0] == "juvenile justice in Tyre in 2024 as a table"
    # "translate it into French" after the question that matched nothing: a follow-up, its years kept, its
    # language never searched
    picked = _picked("translate it into French please", earlier=turns[:1])
    assert picked.follow_up and "french" not in searched(picked) and picked.filters.year == ["2024"]


def test_a_switch_after_no_match_searches_the_new_subject_in_its_place(ai, today):  # noqa: F811
    load(*NOTHING)
    earlier = [{"question": "juvenile justice in Zahle", "answer": "", "cited": [], "new_subject": []}]
    picked = _picked("what about stunting instead?", earlier=earlier)
    assert picked.follow_up and "places" in picked.kept
    assert _ids(picked)[0] == ids_of("In Zahle, stunting affected 10% of children.")[0]
    assert picked.new_subject == [["stunting"]]
    # no finding of the place and years on the new subject (the place's are read, and the line says so): it
    # is still the next follow-up's subject
    earlier = [turn("child labour in Akkar in 2022", "In Akkar, child labour fell.")]
    picked = _picked("what about stunting instead?", earlier=earlier)
    assert picked.relaxed == "words" and picked.new_subject == [["stunting"]]


# ------------------------------------------------------------------------- "and <a group of people>?"
@pytest.mark.parametrize(
    "question",
    [
        "and children with disabilities?",
        "and Syrian refugee children?",
        "and adolescent girls in Akkar?",
        "and for children with disabilities in particular?",
        "and for refugees?",
    ],
)
def test_and_a_group_of_people_of_any_length_is_a_follow_up(ai, today, question):  # noqa: F811
    _rain()
    assert _picked(question, earlier=[RAIN_TURN]).follow_up


CWD = (
    "In Akkar, child marriage affected 9% of girls.",
    "In Akkar, child marriage was more common among children with disabilities.",
    "In Beirut, children with disabilities lacked transport.",
    "In Akkar, children with disabilities lacked transport.",
)


def test_and_children_with_disabilities_is_an_angle_on_the_subject_in_its_place(ai):  # noqa: F811
    load(("2024", CWD))
    picked = _picked("and children with disabilities?", earlier=[turn("child marriage in Akkar", CWD[0])])
    assert picked.follow_up and picked.places == "Akkar" and "subject" in picked.kept
    assert read(picked) == set(CWD[:2]) and picked.new_subject == []


# ---------------------------------------------------------------------------------- "and by district?"
NORTH = (
    "In the North governorate, stunting affected 15% of children.",
    "Stunting was 12% in Tripoli, 18% in Zgharta and 20% in Koura.",
    "In Tripoli, stunting affected 14% of children.",
    "In Akkar, stunting affected 25% of children.",
    "In Beirut, stunting fell to 8%.",
)


def test_and_by_district_after_a_place_breaks_it_down_by_its_districts(ai):  # noqa: F811
    load(("2024", NORTH))
    earlier = [turn("stunting in the North governorate", NORTH[0])]
    for question in ("and by district?", "can you break it down by district?", "per caza please"):
        picked = _picked(question, earlier=earlier)
        assert picked.follow_up and picked.places == "the North" and "places" in picked.kept, question
        assert picked.breakdown and picked.by_district, question
        # the earlier answer's finding, then the one naming the most districts of the North
        assert _ids(picked)[:3] == ids_of(*NORTH[:3]), question
        assert "By district within the North: findings that name more of its districts first." in picked.notes
        assert "by district within the North" in picked.read_line()
    # a breakdown by governorate is of all of Lebanon, and so is one by district with no place before it
    picked = _picked("can you do it for each governorate?", earlier=earlier)
    assert picked.breakdown and not picked.by_district and picked.places == ""
    picked = _picked("and by district?", earlier=[turn("stunting", NORTH[4])])
    assert picked.breakdown and picked.by_district and picked.places == ""


def test_a_breakdown_within_a_place_says_when_no_finding_compares_its_districts(ai):  # noqa: F811
    load(("2024", NORTH))
    picked = ask.Picked(breakdown=True, by_district=True, within=["north"], places="the North")
    apart = list(KMFinding.objects.filter(key_finding__in=[NORTH[0], NORTH[2]]))
    assert picked.missing(apart) == ["no finding read compares the districts of the North"]
    assert picked.missing(list(KMFinding.objects.filter(key_finding=NORTH[1]))) == []
    # a place of one district has none to compare
    assert ask.Picked(breakdown=True, by_district=True, within=["akkar"], places="Akkar").missing(apart) == []


def test_a_follow_up_after_a_breakdown_within_a_place_keeps_it(ai):  # noqa: F811
    load(("2024", NORTH))
    earlier = [turn("stunting in the North governorate", NORTH[0]), turn("and by district?", NORTH[1])]
    picked = _picked("in 2024?", earlier=earlier)
    assert picked.places == "the North" and picked.breakdown and picked.by_district


# ----------------------------------------------------------------------- a switch to another subject
VACCINATION = (
    "In Akkar, child marriage affected 9% of girls.",
    "In Akkar, child marriage and missed vaccination went together.",
    "In Akkar, vaccination coverage fell to 60%.",
    "In Akkar, measles vaccination reached 70% of infants.",
    "In Beirut, vaccination coverage rose to 80%.",
)


@pytest.mark.parametrize(
    "question",
    [
        "what about vaccination?",
        "how about vaccination?",
        "and what about vaccination?",
        "ok, what about vaccination instead?",
        "vaccination instead",
        "do the same for vaccination",
    ],
)
def test_a_switch_to_another_subject_searches_it_alone_in_the_earlier_place(ai, question):  # noqa: F811
    load(("2024", VACCINATION))
    picked = _picked(question, earlier=[turn("child marriage in Akkar", VACCINATION[0])])
    assert picked.follow_up and "places" in picked.kept and "subject" not in picked.kept, question
    # every vaccination finding of Akkar, never only those that hold child marriage too
    assert set(_ids(picked)[:3]) == set(ids_of(*VACCINATION[1:4])), question
    assert VACCINATION[0] not in read(picked) and picked.cited_again == 0, question
    assert len(picked.new_subject) == 1 and "vaccination" in picked.new_subject[0], question


def test_a_switch_to_words_no_finding_holds_leaves_the_earlier_subject(ai):  # noqa: F811
    load(("2024", VACCINATION))
    earlier = [turn("vaccination in Akkar", VACCINATION[2])]
    picked = _picked("what about Zorvex?", earlier=earlier)
    assert picked.new_subject == []  # never a subject of words no finding holds
    picked = _picked("and in Beirut?", earlier=[*earlier, turn("what about Zorvex?", VACCINATION[2])])
    assert "subject" in picked.kept and _ids(picked)[0] == ids_of(VACCINATION[4])[0]
    # "the same thing for Beirut": the earlier subject in Beirut, never the word "thing"
    picked = _picked("same thing for Beirut", earlier=earlier)
    assert "thing" not in searched(picked) and _ids(picked)[0] == ids_of(VACCINATION[4])[0]
    assert picked.new_subject == [] and "subject" in picked.kept


SPLIT = (
    "In Tripoli, 35% of children were out of school.",
    "In Akkar, child marriage affected 9% of girls.",
    "In Beirut, 10% of children were in child labour.",
)


def test_a_follow_up_of_no_word_of_its_own_on_a_two_word_subject_reads_either(ai):  # noqa: F811
    load(("2024", SPLIT))
    switch = "what about child marriage and child labour?"
    picked = _picked(switch, earlier=[turn("out of school children in Tripoli", SPLIT[0])])
    stored = picked.new_subject
    assert {"child marriage", "labour"} <= {alt for g in stored for alt in g}
    earlier = [turn("out of school children in Tripoli", SPLIT[0]), {**turn(switch), "new_subject": stored}]
    picked = _picked("can u do it for all lebanon?", earlier=earlier)
    assert read(picked) == set(SPLIT[1:]) and picked.loose


def test_a_group_of_people_after_what_about_stays_an_angle(ai):  # noqa: F811
    load(("2024", VACCINATION))
    picked = _picked("what about girls?", earlier=[turn("child marriage in Akkar", VACCINATION[0])])
    assert picked.new_subject == [] and "subject" in picked.kept


# ------------------------------------------------------ groups of people of the same kind replace each other
LABOUR = (
    "In the Bekaa, 15% of children were in child labour.",
    "In the Bekaa, boys in child labour worked on farms.",
    "In the Bekaa, girls in child labour worked in homes.",
    "In the Bekaa, girls lacked safe spaces.",
)


def test_and_for_girls_after_and_for_boys_replaces_boys(ai):  # noqa: F811
    load(("2024", LABOUR))
    earlier = [turn("child labour in the Bekaa", LABOUR[0]), turn("and for boys?", LABOUR[0])]
    picked = _picked("and for girls?", earlier=earlier)
    assert "girls" in searched(picked) and "boys" not in searched(picked)
    assert LABOUR[2] in read(picked) and LABOUR[1] not in read(picked) and LABOUR[3] not in read(picked)
    # and the next follow-up keeps girls only
    topic = ask.conversation([*earlier, turn("and for girls?", LABOUR[2])])
    assert topic.angles == [["girls"]]


def test_a_group_of_people_replaces_one_of_its_kind_in_the_subject_too(ai):  # noqa: F811
    load(("2024", LABOUR))
    picked = _picked("and for boys?", earlier=[turn("girls in child labour in the Bekaa", LABOUR[2])])
    assert "boys" in searched(picked) and "girls" not in searched(picked)


def test_population_groups_replace_each_other_and_groups_of_other_kinds_add_up(ai):  # noqa: F811
    load(("2024", LABOUR))
    first = turn("child labour in the Bekaa", LABOUR[0])
    picked = _picked("and for Syrian refugees?", earlier=[first, turn("what about Palestinian refugees?")])
    assert {"syrian", "refugees"} <= searched(picked) and "palestinian" not in searched(picked)
    picked = _picked("and for girls?", earlier=[first, turn("what about refugees?")])
    assert {"girls", "refugees"} <= searched(picked)


# ------------------------------------------------------------------------------------ "anything newer?"
NEWER = (
    ("2019", ["In Akkar, 12% of children were in child labour."]),
    ("2021", ["In Akkar, child labour was 17%."]),
    ("2024", ["In Akkar, child labour rose to 25%."]),
    ("2026", ["In Akkar, child labour reached 30%."]),
)


@pytest.mark.parametrize(
    "question",
    [
        "is there anything newer?",
        "more recent?",
        "any update?",
        "anything more recent?",
        "is there an update?",
    ],
)
def test_anything_newer_reads_the_years_after_the_earlier_ones(ai, today, question):  # noqa: F811
    load(*NEWER)
    earlier = [turn("child labour in Akkar in 2019", "In Akkar, 12% of children were in child labour.")]
    picked = _picked(question, earlier=earlier)
    assert picked.follow_up and picked.years == "2020–2026", question
    assert read(picked) == {t for _y, texts in NEWER[1:] for t in texts}, question


def test_anything_newer_with_no_earlier_years_is_the_latest(ai, today):  # noqa: F811
    load(*NEWER)
    picked = _picked("anything newer?", earlier=[turn("child labour in Akkar")])
    assert picked.years == "2024–2026"
    picked = _picked("anything newer?", earlier=[turn("child labour in Akkar this year")])
    assert picked.years == "2026"


def test_the_years_after_the_earlier_ones_stay_and_none_newer_reads_the_newest(ai, today):  # noqa: F811
    load(*NEWER, ("2019", ["In Akkar, child marriage affected 9% of girls."]))
    earlier = [turn("child labour in Akkar in 2019"), turn("is there anything newer?")]
    assert ask.conversation(earlier).spans == [(2020, 2026)]
    picked = _picked("and for girls?", earlier=earlier)
    assert picked.years == "2020–2026"
    # nothing newer on child marriage: the newest findings, and the line says so
    picked = _picked("anything newer?", earlier=[turn("child marriage in Akkar in 2019")])
    assert picked.recent == "none" and read(picked) == {"In Akkar, child marriage affected 9% of girls."}


# --------------------------------------------------------------------------- a short question of its own
@pytest.mark.parametrize(
    ("question", "follows"),
    [
        ("stunting in Baalbek?", False),
        ("child marriage Akkar", False),
        ("child marriage 2024", False),
        ("stunting nationally?", False),
        # no place or year of its own, a group of people, or a cue
        ("child marriage", True),
        ("girls in Akkar?", True),
        ("what about Akkar?", True),
        ("Akkar in 2024?", True),
        ("stunting, more recent?", True),
    ],
)
def test_a_short_question_of_its_own_subject_and_place_or_years_is_new(ai, today, question, follows):  # noqa: F811
    _rain()
    earlier = [
        {**RAIN_TURN, "question": "why are Syrian refugee boys sent to work instead of going to school"}
    ]
    assert _picked(question, earlier=earlier).follow_up is follows
