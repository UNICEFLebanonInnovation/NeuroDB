"""What Ask the KM Database reads for a question: the words it searches and those it leaves out (requests,
figures, places), the AI's suggested search words, phrases and their word forms, places and breakdowns, the
years of a question, follow-ups in a conversation and the lines under the answer. Invented texts only;
places are public geography."""

from __future__ import annotations

import datetime
import re
import uuid

import pytest
from django.contrib.postgres.search import SearchVector

from neurodb.km import ask, places, search
from neurodb.km import columns as cols
from neurodb.km.models import KMFinding, KMQuestion
from tests.km import factory
from tests.km.test_ask import (  # noqa: F401 (fixtures)
    _finish,
    _ids,
    _picked,
    _sent,
    ai,
    answer,
    done,
    post,
    terms,
    today,
)
from tests.km.test_ask_session import Q1, Q3, Q4, build

pytestmark = pytest.mark.django_db

F, M, DIS = "Female", "Male", "Disability Inclusion"
ALL_NO = {flag.header: "No" for flag in cols.FLAGS}


def f(text: str, *flags: str) -> dict:
    """A finding's cells: its key finding, no mark but ``flags``."""
    return {**ALL_NO, "Key Finding": text, **{flag: "Yes" for flag in flags}}


def load(*docs) -> None:
    """A workbook of ``docs`` ((year, [texts or cells]) each), loaded even when it has fewer findings than the
    version it replaces."""
    data = build([(year, [f(t) if isinstance(t, str) else t for t in texts]) for year, texts in docs])
    factory.load(data, fewer_ok=True)


def ids_of(*texts: str) -> list[str]:
    return [KMFinding.objects.get(key_finding=t).finding_id for t in texts]


def read(picked) -> set[str]:
    return {f.key_finding for f in picked.findings}


# ------------------------------------------------------------------------------- the suggested words
LENS = (
    "Children with disabilities had less access to school.",
    "Gender gaps in learning widened, with girls ahead of boys in reading.",
    "Boys were more often engaged in child labour (17%).",
    "Children with disabilities lacked transport to school.",
    "Fake rainfall flooded schools.",
    "In Mount Lebanon, 18,300 children were out of school.",
    "In the Metn, out-of-school rates were lowest among refugee children.",
    "In Keserwan-Jbeil, 7% of children were out of school.",
    "In Akkar, 27% of children were out of school.",
)
SUGGESTED = (
    "gender",
    "girls",
    "boys",
    "sex-disaggregated",
    "disability",
    "children with disabilities",
    "disabled children",
    "inclusive education",
    "female",
    "male",
)


def test_q3_alone_reads_the_findings_on_either_angle_and_says_so(ai):  # noqa: F811
    load(("2025", LENS))
    picked = _picked(Q3)
    assert ["population"] not in picked.groups  # "by population type": a request's words
    assert read(picked) == {LENS[0], LENS[1], LENS[2], LENS[3]}
    assert "No finding matches all your words: findings that match one of them were read." in picked.notes


def test_q3_alone_is_answered_from_the_suggested_words(ai):  # noqa: F811
    load(("2025", LENS))
    ai(terms_=[terms(*SUGGESTED)])
    picked = _picked(Q3, may_call=True)
    assert {LENS[0], LENS[1], LENS[2], LENS[3]} <= read(picked)
    assert LENS[4] not in read(picked)


def test_suggested_words_in_a_place_need_one_of_them_and_never_a_place(ai):  # noqa: F811
    load(("2025", LENS))
    ai(
        terms_=[
            terms(
                "out of school",
                "out-of-school children",
                "school enrolment",
                "non-enrolled children",
                "school attendance",
                "education access",
                "dropout",
                "Mount Lebanon",
                "children not in school",
            )
        ]
    )
    picked = _picked("enfants non scolarisés au Mont-Liban", may_call=True)
    assert read(picked) >= {LENS[5], LENS[6], LENS[7]} and LENS[8] not in read(picked)
    assert ["mount lebanon"] not in [g[:1] for g in picked.groups]


def test_words_no_finding_holds_with_a_place_read_the_places_findings(ai):  # noqa: F811
    load(("2025", LENS))
    picked = _picked("enfants non scolarisés au Mont-Liban")  # no search-words call (the AI off)
    assert read(picked) == {LENS[5], LENS[6], LENS[7]}
    assert picked.notes[0] == "No finding holds your words: the findings that name Mount Lebanon were read."


# ----------------------------------------------------------------------------------- word forms
DROPS = (
    "In Tripoli, 35% of adolescents dropped out of secondary school.",
    "Boys drops out earlier than girls.",
    "Girls drop out of school to marry.",
    "Dropout rates rose to 12%.",
    "Children dropping out of school worked.",
    "Enrolment saw a drop in 2023.",
)


@pytest.mark.parametrize("asked", ["dropout", "drop out", "dropping out", "dropped out"])
def test_dropout_finds_dropped_out_and_drops_out_never_a_drop(ai, asked):  # noqa: F811
    load(("2024", DROPS))
    assert read(_picked(asked)) == set(DROPS[:5])
    found, how = search.find(KMFinding.objects.all(), asked)  # the search box too
    assert how == "words" and {x.key_finding for x in found} == set(DROPS[:5])


def test_school_dropout_in_a_town_reads_its_dropped_out_finding_first(ai):  # noqa: F811
    load(("2024", DROPS))
    assert _ids(_picked("school dropout in Tripoli"))[0] == ids_of(DROPS[0])[0]


def test_words_asking_for_figures_are_never_required(ai):  # noqa: F811
    texts = (
        "In Akkar, 27% of children aged 6-14 were out of school.",
        "In Akkar, the out-of-school rate was 27% among girls.",
        "Out-of-school rates rose among refugee children.",
        "In Beirut, 9,100 children were out of school.",
    )
    load(("2024", texts))
    for question in (
        "out of school rate in Akkar",
        "out of school rates Akkar",
        "proportion of out of school",
    ):
        picked = _picked(question)
        assert picked.groups == [["oosc", "out-of-school", "out of school"]], question
    picked = _picked("out of school rate in Akkar")
    assert set(_ids(picked)[:2]) == set(ids_of(*texts[:2]))
    assert picked.notes == [
        "Only 2 findings on this subject name Akkar: findings from all of Lebanon were read too."
    ]
    assert _picked("most recent vaccination figures").groups == [["vaccination"]]


# ----------------------------------------------------------------------------------------- years
def test_latest_reads_the_newest_findings_when_none_is_recent(ai, today):  # noqa: F811
    load(
        ("2022", ["Measles vaccination coverage fell to 62%."]),
        ("2020", ["Measles vaccination coverage was 70%."]),
        ("2025", ["Fake rainfall flooded schools."]),
        ("2026", ["Fake teachers left."]),
    )
    for question in ("latest data on vaccination coverage", "most recent vaccination figures"):
        picked = _picked(question)
        assert [f.year for f in picked.findings] == [2022, 2020], question
        assert picked.filters.year == []
        assert picked.notes == [
            "No finding of 2024–2026 matches your words: the most recent ones were read, newest first."
        ]
    load(
        ("2022", ["Measles vaccination coverage fell to 62%."]),
        ("2025", ["Measles vaccination coverage rose to 80%."]),
    )
    picked = _picked("latest data on vaccination coverage")
    assert [f.year for f in picked.findings] == [2025, 2022]
    assert picked.notes == [
        "Only 1 finding of 2024–2026 matches your words: the most recent others were read too, newest first."
    ]


@pytest.mark.parametrize(
    ("asked", "years", "left"),
    [
        ("compare child marriage in 2022 and 2025", [2025, 2022], "compare child marriage"),
        ("child marriage 2022 vs 2025", [2025, 2022], "child marriage"),
        ("child marriage in 2022, 2023 and 2025", [2025, 2023, 2022], "child marriage"),
        ("child marriage 2025", [2025], "child marriage"),
        ("nabatiyeh chid labour 2024", [2024], "nabatiyeh chid labour"),
        ("children in 2000 households", [], "children in 2000 households"),
        ("progress of the 2030 agenda", [], "progress of the 2030 agenda"),
        ("targets for 2030", [], "targets for 2030"),
        ("recently displaced children", [], "recently displaced children"),
        ("recent arrivals in Akkar", [], "recent arrivals in Akkar"),
        ("what changed recently?", [2026, 2025, 2024], "what changed ?"),
        ("recent data on child labour", [2026, 2025, 2024], "data on child labour"),
        # a full stop or a comma after a year (never a decimal point, a thousands comma or "2024/25")
        (
            "In 2024, how many children were out of school in Akkar?",
            [2024],
            ", how many children were out of school in Akkar?",
        ),
        ("out of school children in Akkar in 2024.", [2024], "out of school children in Akkar ."),
        ("compare child marriage in 2022 and 2025.", [2025, 2022], "compare child marriage ."),
        ("child marriage in 2022, 2023 and 2025.", [2025, 2023, 2022], "child marriage ."),
        ("child marriage 2025.", [2025], "child marriage ."),
        ("child labour in 2024, by governorate", [2024], "child labour , by governorate"),
        ("children in 2024.5 households", [], "children in 2024.5 households"),
        ("children in 2024,500 households", [], "children in 2024,500 households"),
        ("out of school children in 2024/25.", [2025, 2024], "out of school children in ."),
        # a school or an academic year
        ("dropout in 2024 school year", [2024], "dropout school year"),
        ("dropout in the 2024 school year", [2024], "dropout in the school year"),
        ("enrolment in the 2023 academic year", [2023], "enrolment in the academic year"),
        ("schools in 2000 schools", [], "schools in 2000 schools"),
        ("more than 2000 children were out of school", [], "more than 2000 children were out of school"),
        ("the 2000 households surveyed", [], "the 2000 households surveyed"),
        ("what do the 2024 figures say on child labour?", [2024], "what do the figures say on child labour?"),
        ("child labour in 2024 during the war", [2024], "child labour during the war"),
        # "the" and a year: the year of the data, unless an event follows
        (
            "What were the 2024 out of school figures in Akkar?",
            [2024],
            "What were the out of school figures in Akkar?",
        ),
        ("the 2024 out-of-school rate in Akkar", [2024], "the out-of-school rate in Akkar"),
        (
            "What was the 2024 situation of children in the South?",
            [2024],
            "What was the situation of children in the South?",
        ),
        ("what about the 2024 situation?", [2024], "what about the situation?"),
        (
            "what were the 2024 trends in child labour in the Bekaa?",
            [2024],
            "what were the trends in child labour in the Bekaa?",
        ),
        ("What did the 2024 VASyR find on child labour?", [2024], "What did the VASyR find on child labour?"),
        ("the 2024 MICS on child marriage", [2024], "the MICS on child marriage"),
        (
            "what did the 2023 back to school campaign achieve",
            [2023],
            "what did the back to school campaign achieve",
        ),
        ("the 2024 response in the south", [2024], "the response in the south"),
        # a year before "child" is a year ("the 2024 child labour figures"); before "children", a count
        ("the 2024 child labour figures in the Bekaa", [2024], "the child labour figures in the Bekaa"),
        ("the 2023 child marriage rate in the North", [2023], "the child marriage rate in the North"),
        ("2024 child labour in the Bekaa", [2024], "child labour in the Bekaa"),
        ("in 2024 child labour rose", [2024], "child labour rose"),
        ("in 2000 children", [], "in 2000 children"),
        # a year compared with, when the question names a year of its own: both years
        (
            "Were more children out of school in Akkar in 2025 than in 2019?",
            [2025, 2019],
            "Were more children out of school in Akkar ?",
        ),
        (
            "Was child marriage in the North higher in 2024 than in 2019?",
            [2024, 2019],
            "Was child marriage in the North higher ?",
        ),
        (
            "What was the out of school rate in Akkar in 2025 compared with the 2019 rate?",
            [2025, 2019],
            "What was the out of school rate in Akkar ?",
        ),
        (
            "Is child labour in Bekaa higher in 2024 than 2019?",
            [2024, 2019],
            "Is child labour in Bekaa higher ?",
        ),
        (
            "Has child poverty in Baalbek risen in 2025 compared to the 2019 levels?",
            [2025, 2019],
            "Has child poverty in Baalbek risen ?",
        ),
        # "before" a year is never that year
        ("child marriage in 2023 and before 2020", [2023], "child marriage and"),
        # the year of an event, or a year compared with: no Year filter, never searched; nor are the event's
        # words (they only rank)
        ("mental health of adolescents after the 2020 explosion", [], "mental health of adolescents after"),
        ("how did the 2024 war affect children in the south?", [], "how did affect children in the south?"),
        ("children affected by the August 2020 blast", [], "children affected by"),
        (
            "During the 2024 escalation, how many children were displaced?",
            [],
            "During , how many children were displaced?",
        ),
        ("impact of 2020 port explosion on children", [], "impact of on children"),
        ("Lebanon's 2019 crisis and child labour", [], "Lebanon's and child labour"),
        ("injuries from 2020 explosion", [], "injuries from"),
        # an event's name with "and", "&", "of" or "the" in it
        ("how has the 2019 financial and economic crisis affected children", [], "how has affected children"),
        ("child labour after the 2019 economic and financial collapse", [], "child labour after"),
        # the event's place is the question's
        ("impact of the 2020 port of Beirut explosion on children", [], "impact of Beirut on children"),
        ("children after the 2019 economic & financial crisis", [], "children after"),
        (
            "child labour since the 2019 economic and financial crisis",
            list(range(2026, 2018, -1)),
            "child labour",
        ),
        # no event after it: the year of the data
        (
            "the 2024 out of school figures and the war in the south",
            [2024],
            "the out of school figures and the war in the south",
        ),
        ("the 2024 figures of the war", [2024], "the figures of the war"),
        ("out of school rates compared to 2019", [], "out of school rates"),
        ("is child labour higher than 2019?", [], "is child labour higher ?"),
        ("is child labour higher than in 2019?", [], "is child labour higher ?"),
        ("out of school children before the 2019 crisis", [], "out of school children"),
        ("out of school children before 2020", [], "out of school children"),
        # "since the 2019 crisis": since 2019 (the event's words only rank)
        ("child marriage since the 2019 crisis", list(range(2026, 2018, -1)), "child marriage"),
        ("child labour since the 2019 economic crisis", list(range(2026, 2018, -1)), "child labour"),
        ("child labour since the 2019 school year", list(range(2026, 2018, -1)), "child labour school year"),
        # "in 2024" before what happened then ("amid", "despite", "following" and the like): the year of the
        # data, the event's words searched as written
        (
            "out of school children in the South in 2024 amid the recent escalation",
            [2024],
            "out of school children in the South amid the recent escalation",
        ),
        ("children in 2024 amid the ongoing war", [2024], "children amid the ongoing war"),
        (
            "school enrolment in 2024 despite the economic crisis",
            [2024],
            "school enrolment despite the economic crisis",
        ),
        (
            "child poverty in 2024 given the economic crisis",
            [2024],
            "child poverty given the economic crisis",
        ),
        (
            "child labour in Akkar in 2024 amid the economic crisis",
            [2024],
            "child labour in Akkar amid the economic crisis",
        ),
        (
            "situation of children in South Lebanon in 2024 following the October escalation",
            [2024],
            "situation of children in South Lebanon following the October escalation",
        ),
        (
            "children in Nabatieh in 2024 amidst the cross-border hostilities",
            [2024],
            "children in Nabatieh amidst the cross-border hostilities",
        ),
        (
            "school attendance in 2025 throughout the economic crisis",
            [2025],
            "school attendance throughout the economic crisis",
        ),
        (
            "child labour in all gov in 2023 amid the deepening economic crisis",
            [2023],
            "child labour in all gov amid the deepening economic crisis",
        ),
        (
            "out of school children in 2024 across the south amid hostilities",
            [2024],
            "out of school children across the south amid hostilities",
        ),
        (
            "children in Akkar 2024 amid the economic crisis",
            [2024],
            "children in Akkar amid the economic crisis",
        ),
        # a year before the year of an event: the year of the data (the event's year only ranks)
        (
            "out of school children per governorate in 2025 following the 2024 war",
            [2025],
            "out of school children per governorate following",
        ),
        ("school enrolment in 2025 following the 2024 escalation", [2025], "school enrolment following"),
    ],
)
def test_years_of_a_question(asked, years, left):
    frame = ask.time_frame(asked, datetime.date(2026, 11, 25))
    assert frame.years == years
    assert " ".join(frame.text.split()) == left


@pytest.mark.parametrize(
    ("asked", "ranks"),
    [
        ("mental health of adolescents after the 2020 explosion", ["2020", "explosion"]),
        ("out of school rates compared to 2019", ["2019"]),
        ("child marriage since the 2019 crisis", ["crisis"]),
        ("out of school children before the 2019 economic crisis", ["2019", "economic", "crisis"]),
        ("child marriage 2025", []),
        ("Were more children out of school in Akkar in 2025 than in 2019?", ["2019"]),
        (
            "how has the 2019 financial and economic crisis affected children",
            ["2019", "financial", "economic", "crisis"],
        ),
        (
            "child labour since the 2019 economic and financial crisis",
            ["economic", "financial", "crisis"],
        ),
        (
            "out of school children before the 2020 port of Beirut explosion",
            ["2020", "port", "beirut", "explosion"],
        ),
        ("out of school children per governorate in 2025 following the 2024 war", ["2024", "war"]),
    ],
)
def test_the_year_of_an_event_only_ranks(asked, ranks):
    assert ask.time_frame(asked, datetime.date(2026, 11, 25)).ranks == ranks


def test_the_year_of_the_data_after_the_is_the_year_filter(ai, today):  # noqa: F811
    texts = (
        "In the South, 30% of children were out of school.",
        "In Nabatieh, 12% of children worked.",
        "In Tyre, 20% of children were displaced.",
    )
    load(("2024", texts[:1]), ("2025", texts[1:2]), ("2019", texts[2:]))
    picked = _picked("What was the 2024 situation of children in the South?")
    assert picked.filters.year == ["2024"] and read(picked) == {texts[0]}
    assert picked.notes[-1].startswith("Years 2024, from your question")
    # a follow-up's own year of the data replaces the earlier years
    earlier = [{"question": "situation of children in the South in 2019", "answer": "Fake.", "cited": []}]
    picked = _picked("what about the 2024 situation?", earlier=earlier)
    assert picked.filters.year == ["2024"] and read(picked) == {texts[0]}


def test_a_year_compared_with_a_year_of_the_question_reads_both(ai, today):  # noqa: F811
    texts = (
        "In Akkar, 27% of children were out of school.",
        "In Akkar, 18% of children aged 6-14 were out of school.",
        "In Akkar, 22% of children were out of school.",
    )
    load(("2025", texts[:1]), ("2019", texts[1:2]), ("2022", texts[2:]))
    for question in (
        "Were more children out of school in Akkar in 2025 than in 2019?",
        "What was the out of school rate in Akkar in 2025 compared with the 2019 rate?",
    ):
        picked = _picked(question)
        assert picked.filters.year == ["2025", "2019"] and read(picked) == set(texts[:2]), question
    # a follow-up that compares with a year keeps the earlier years and adds it
    earlier = [
        {"question": "out of school children in Akkar in 2025", "answer": "Fake.", "cited": ids_of(texts[0])}
    ]
    picked = _picked("and compared to 2019?", earlier=earlier)
    assert picked.follow_up and picked.filters.year == ["2025", "2019"] and read(picked) == set(texts[:2])
    # and the next follow-up keeps both
    earlier.append({"question": "and compared to 2019?", "answer": "Fake.", "cited": ids_of(texts[1])})
    picked = _picked("can u do it for all lebanon?", earlier=earlier)
    assert picked.filters.year == ["2025", "2019"]


def test_a_year_before_a_full_stop_or_a_comma_is_the_year_filter(ai, today):  # noqa: F811
    load(
        ("2024", ["In Akkar, 27% of children were out of school."]),
        ("2021", ["In Akkar, 31% of children were out of school."]),
    )
    for question in ("out of school children in Akkar in 2024.", "In 2024, out of school children in Akkar?"):
        picked = _picked(question)
        assert picked.filters.year == ["2024"] and picked.years == "2024", question
        assert read(picked) == {"In Akkar, 27% of children were out of school."}


def test_a_rate_compared_to_a_year_reads_every_year(ai, today):  # noqa: F811
    texts = (
        "In Akkar, 27% of children were out of school.",
        "In the South in 2019, 30% of children were out of school.",
    )
    load(("2025", texts[:1]), ("2019", texts[1:]), ("2024", ["Fake rainfall fell."]))
    picked = _picked("out of school rates compared to 2019")
    assert picked.filters.year == [] and read(picked) == set(texts)
    assert picked.groups == [["oosc", "out-of-school", "out of school"]]


def test_the_year_of_an_event_reads_the_studies_of_the_event(ai, today):  # noqa: F811
    texts = (
        "One year after the explosion, 1 in 3 adolescents in Beirut reported distress.",
        "In 2019, 12% of adolescents reported distress at school.",
        "After the 2020 explosion, schools reopened.",
    )
    load(("2021", texts[:1]), ("2023", texts[1:]), ("2020", ["Fake rainfall fell."]))
    picked = _picked("distress of adolescents after the 2020 explosion")
    # every year; the event's words only rank: its finding first, never one on the event alone
    assert picked.filters.year == [] and [f.key_finding for f in picked.findings] == list(texts[:2])
    assert ["2020"] not in picked.groups and ["explosion"] not in picked.groups
    marriage = (
        "Since the 2019 crisis, child marriage rose among displaced girls to 20%.",
        "In Akkar, child marriage affected 6% of girls.",
        "In the Bekaa, child marriage affected 4% of girls.",
    )
    load(("2023", marriage[:1]), ("2019", marriage[1:2]), ("2018", marriage[2:]))
    picked = _picked("child marriage since the 2019 crisis")
    assert picked.filters.year == ["2023", "2019"] and picked.groups == [["child marriage", "early marriage"]]
    assert [f.key_finding for f in picked.findings] == list(marriage[:2])  # the crisis's finding first


def test_a_comparison_of_two_years_reads_both(ai, today):  # noqa: F811
    load(
        ("2022", ["In the Bekaa, child marriage reached 24%."]),
        ("2025", ["In the North, child marriage affected 13% of girls."]),
        ("2019", ["In Akkar, child marriage affected 6% of girls."]),
    )
    picked = _picked("compare child marriage in 2022 and 2025")
    assert picked.filters.year == ["2025", "2022"] and picked.groups == [["child marriage", "early marriage"]]
    assert {f.year for f in picked.findings} == {2022, 2025}


# ---------------------------------------------------------------------------------------- places
def test_national_with_a_place_reads_everywhere_the_place_first(ai):  # noqa: F811
    exams = (
        "Nationally, 60% of pupils passed the exams.",
        "In Beirut, 70% of pupils passed the national exams.",
        "In Akkar, 40% of pupils passed the national exams.",
    )
    load(("2024", exams))
    picked = _picked("national exam results in Akkar")
    assert _ids(picked)[0] == ids_of(exams[2])[0] and read(picked) == set(exams)
    assert picked.notes == ["All of Lebanon, from your question, with the findings that name Akkar first."]
    load(
        (
            "2024",
            [
                "Nationally, 23% of children were out of school.",
                "In Beirut, 9% of children were out of school.",
                "In Akkar, 27% of children were out of school.",
            ],
        )
    )
    picked = _picked("compare the national out of school rate with Akkar")
    assert _ids(picked)[0] == ids_of("In Akkar, 27% of children were out of school.")[0]
    assert len(picked.findings) == 3


def test_national_as_an_adjective_of_the_subject_is_a_word(ai):  # noqa: F811
    load(("2024", ["The national education strategy targets enrolment.", "Fake rainfall fell."]))
    picked = _picked("What does the national education strategy say about enrolment?")
    assert not picked.national and ["national"] in picked.groups
    assert picked.notes == []
    assert _picked("can you do it at national level?").national
    assert _picked("and the national figures?").national
    assert _picked("national").national


def test_a_breakdown_by_governorate_reads_findings_that_name_only_a_district(ai, today):  # noqa: F811
    texts = (
        "In Akkar, 31% of children were stunted, against 12% in Beirut.",
        "In Tripoli, 35% of adolescents dropped out of school.",
        "In Saida and Tyre, 9% of children were out of school.",
        "In Zahle, 14% of children worked.",
        "In Marjayoun, 30% of schools closed.",
        "In Hermel and Halba, 6% of children were displaced.",
        "Fake rainfall fell on the plain.",
    )
    load(("2025", texts))
    q6 = "i need a paragraph describing situation of children in all gov in the past 2 years only"
    picked = _picked(q6)
    assert read(picked) == set(texts[:6])
    assert set(_ids(picked)[:2]) == set(ids_of(texts[0], texts[5]))  # two governorates each
    assert places.compares(texts[5]) and not places.compares(texts[2])


def test_a_follow_up_by_governorate_reads_findings_of_districts(ai):  # noqa: F811
    texts = (
        "In Minieh-Danniyeh, child marriage affected 13% of girls.",
        "In Hermel, child marriage affected 9% of girls.",
        "Child marriage remained a concern nationally.",
    )
    load(("2024", texts))
    earlier = [
        {"question": "child marriage in the North", "answer": "Fake.", "cited": ids_of(texts[0])},
    ]
    picked = _picked("can you do it for all governorates?", earlier=earlier)
    assert read(picked) == set(texts[:2])


def test_by_district_is_a_breakdown_and_a_district_word_goes_with_its_place(ai):  # noqa: F811
    texts = (
        "In Akar district, 14% of children were out of school.",
        "In Tripoli, 35% of children were out of school.",
        "Out-of-school rates rose among refugee children.",
    )
    load(("2024", texts))
    picked = _picked("out of school children by district")
    assert picked.breakdown and read(picked) == set(texts[:2])
    assert picked.notes == [
        "By district, from your question: findings that name districts or governorates first."
    ]
    picked = _picked("out of school children in Akkar district")
    assert picked.groups == [["oosc", "out-of-school", "out of school"]] and not picked.breakdown


PER = (
    "In Akkar, 27% of children were out of school.",
    "In Beirut, 9,100 children were out of school.",
    "In Akkar, households spent USD 50 per month on drinking water.",
    "In Tripoli, public schools had 40 pupils per classroom.",
    "Across the Bekaa, water trucking costs doubled.",
    "In Tyre, broken windows were reported in 12 schools.",
    "In Beirut, the poverty level rose to 44 per cent.",
    "In Zahle, 3 health visits per child were recorded within each clinic.",
    "In Halba, a wise use of water was taught.",
)


@pytest.mark.parametrize(
    "asked",
    [
        "out of school rate per governorate",
        "out of school rate per gov",
        "out of school children per mohafaza",
        "out of school children across governorates",
        "out of school children across all the governorates",
        "out of school children, broken down by governorate",
        "out of school children disaggregated by governorate",
        "out of school children with a breakdown by governorate",
        "out of school children governorate-level",
        "out of school children at governorate level",
        "out of school children at the governorate level",
        "out of school children within each governorate",
        "out of school children governorate by governorate",
        "out of school children governorate wise",
        "breakdown of out of school children by governorate",
        "out of school children by region",
        "out of school children per district",
        "out of school children at district level",
        "out of school children, district-level",
        "out of school children within each caza",
        "out of school children across Lebanon's governorates",
        "out of school children in all eight governorates",
        "out of school children in all 8 governorates",
        "out of school children across all Lebanese governorates",
        "out of school children across the country's governorates",
        "out of school children in the nine governorates",
        "out of school children by governorate and district",
        "out of school children per governorate and district",
        "out of school children for each governorate and district",
        "out of school children by district and governorate",
    ],
)
def test_every_way_of_asking_a_breakdown_searches_no_word_of_it(ai, asked):  # noqa: F811
    load(("2024", PER))
    picked = _picked(asked)
    assert picked.breakdown and picked.groups == [
        ["oosc", "out-of-school", "out of school"],
        places.breakdown_alternatives(),
    ]
    assert read(picked) == set(PER[:2])  # never "per month", "across the Bekaa", "broken windows"...
    # by governorate and district: by district
    assert picked.by_district is ("district" in asked or "caza" in asked)


def test_a_follow_up_per_governorate_reads_the_subject_by_governorate(ai):  # noqa: F811
    load(("2024", PER))
    earlier = [{"question": "out of school children in Akkar", "answer": "Fake.", "cited": ids_of(PER[0])}]
    for question in ("can you do it per governorate?", "do the same across governorates"):
        picked = _picked(question, earlier=earlier)
        assert picked.breakdown and read(picked) == set(PER[:2]), question


def test_a_breakdown_word_with_a_place_named_is_never_searched(ai):  # noqa: F811
    for asked in (
        "out of school children in Akkar at district level",
        "out of school children in Akkar per district",
        "out of school children in the North at governorate level",
    ):
        assert ask.understand(asked, asked).groups == [["oosc", "out-of-school", "out of school"]], asked


def test_suggested_words_with_a_follow_ups_breakdown_need_two_of_three(ai):  # noqa: F811
    texts = (
        "In Akkar, girls with disabilities had less access to school.",
        "In Akkar, 27% of children were out of school.",
        "In Tyre, 20% of children were displaced.",
        "In Tyre, displaced children lacked safe water.",
    )
    load(("2025", texts))
    earlier = [{"question": "girls with disabilities", "answer": "Fake.", "cited": ids_of(texts[0])}]
    ai(terms_=[terms("school", "water", "displaced")])
    picked = _picked("can you do it for all governorates?", earlier=earlier, may_call=True)
    assert read(picked) == {texts[0], texts[3]}


NATIONALLY = (
    "Nationally, 8% of children with disabilities had no assistive devices.",
    "Child labour nationally reached 5% of children aged 10-17.",
    "Children of Syrian nationality were less often enrolled in Zahle.",
    "In Jezzine, no child protection case management was available.",
    "In Akkar, 27% of children were out of school.",
)


def test_national_is_never_the_only_word_searched(ai):  # noqa: F811
    load(("2024", NATIONALLY))
    for question in (
        "national education strategy",
        "national vacination stratgy",
        "what does the national strategy say about early childhood development",
    ):
        picked = _picked(question)
        assert read(picked) == set(), question
    assert read(_picked("national social protection strategy")) == {NATIONALLY[3]}
    # a follow-up: the earlier subject, never "nationally" alone
    earlier = [{"question": "out of school children in Akkar", "answer": "Fake.", "cited": []}]
    assert read(_picked("what about the national education strategy?", earlier=earlier)) == {NATIONALLY[4]}


@pytest.mark.parametrize(
    ("asked", "text"),
    [
        ("Liban-Sud", "In Saida, fake rates rose."),
        ("Sud-Liban", "In Bint Jbeil, fake rates rose."),  # South Lebanon: the South and Nabatieh
        ("Al Janoub", "In Tyre, fake rates rose."),
        ("Liban-Nord", "In Tripoli, fake rates rose."),
        ("Al-Shamal", "In Koura, fake rates rose."),
        ("Ein el-Hilweh", "In Ain al-Hilweh camp, fake rates rose."),
        ("the South", "In Rashidieh camp, fake rates rose."),
        ("South Lebanon", "In Ein el-Hilweh, fake rates rose."),
        ("the North", "In Nahr el-Bared, fake rates rose."),
        ("North Lebanon", "In Beddawi camp, fake rates rose."),
        ("Beirut", "In Shatila, fake rates rose."),
        ("Mount Lebanon", "In Bourj el-Barajneh, fake rates rose."),
        # the southern governorates: the South and Nabatieh (the owner's rule for "the south")
        ("the southern governorates", "In Bint Jbeil, fake rates rose."),
        ("southern governorates", "In Tyre, fake rates rose."),
        ("the south governorates", "In Marjayoun, fake rates rose."),
        ("the southern districts", "In Hasbaya, fake rates rose."),
        ("the southern region", "In Saida, fake rates rose."),
        ("the south region", "In Nabatiyeh, fake rates rose."),
        ("South Metn", "In Baabda, fake rates rose."),
        ("North Bekaa", "In Hermel, fake rates rose."),  # Baalbek-Hermel
        ("West Bekaa", "In the Western Bekaa, fake rates rose."),
        # more ways of saying the south: the South and Nabatieh
        ("the southern regions", "In Tyre, fake rates rose."),
        ("southern areas", "In Marjayoun, fake rates rose."),
        ("the southern parts", "In Saida, fake rates rose."),
        ("the southern part of Lebanon", "In Nabatiyeh, fake rates rose."),
        # the northern governorates: the North and Akkar
        ("the northern governorates", "In Halba, fake rates rose."),
        ("the northern region", "In Tripoli, fake rates rose."),
        ("north region", "In Akkar, fake rates rose."),
        ("the northern areas", "In Koura, fake rates rose."),
        # South Bekaa: the Bekaa (West Bekaa and Rashaya), never the South
        ("South Bekaa", "In Rashaya, fake rates rose."),
        ("southern Bekaa", "In West Bekaa, fake rates rose."),
    ],
)
def test_more_place_spellings_and_the_camps(ai, asked, text):  # noqa: F811
    load(("2024", [text, "Fake rates rose in Fakeland.", "Fake rates rose."]))
    picked = _picked(f"situation of children in {asked}")
    assert read(picked) == {text} and picked.groups == []


@pytest.mark.parametrize("asked", ["the northern governorates", "the northern region", "north region"])
def test_the_northern_governorates_are_the_north_and_akkar_and_the_line_says_so(ai, asked):  # noqa: F811
    load(("2024", ["In Halba, child labour rose.", "In Zahle, child labour rose."]))
    picked = _picked(f"child labour in {asked}")
    assert not picked.breakdown and picked.groups == [["labour", "labor"]]
    assert picked.places == "the north (North and Akkar governorates)"
    assert _ids(picked)[0] == ids_of("In Halba, child labour rose.")[0]
    assert places.read(f"child labour in {asked}").keys == ["north", "akkar"]


def test_south_bekaa_is_the_bekaa_and_a_region_of_a_place_is_that_place(ai):  # noqa: F811
    assert places.read("out of school children in South Bekaa").keys == ["bekaa"]
    assert places.read("children in the southern region of the Bekaa").keys == ["bekaa"]
    assert places.read("children in the southern areas of Mount Lebanon").keys == ["mount-lebanon"]
    assert places.read("children in the southern region of Lebanon").keys == ["south", "nabatieh"]


@pytest.mark.parametrize(
    ("asked", "keys"),
    [
        # "in", "of" or "within" before a word that is no place: the region still
        (
            "situation of children in the southern governorates in light of the escalation",
            ["south", "nabatieh"],
        ),
        ("out of school children in the southern governorates in terms of enrolment", ["south", "nabatieh"]),
        ("psychosocial support for children in the southern districts in shelters", ["south", "nabatieh"]),
        ("displaced children from the southern region in collective shelters", ["south", "nabatieh"]),
        ("child protection in the southern region in the context of the war", ["south", "nabatieh"]),
        ("children of the southern region in need of support", ["south", "nabatieh"]),
        ("out of school children in the southern governorates in public schools", ["south", "nabatieh"]),
        ("children in the southern governorates in comparison with Beirut", ["south", "nabatieh", "beirut"]),
        ("child labour in the northern region in particular", ["north", "akkar"]),
        ("children in the southern region of Lebanon", ["south", "nabatieh"]),
        ("children in the southern areas within the country", ["south", "nabatieh"]),
        # a place after it: that place
        ("children in the southern region of the Bekaa", ["bekaa"]),
        ("children in the southern areas of Mount Lebanon", ["mount-lebanon"]),
        ("children in the southern districts within El Nabatieh", ["nabatieh"]),
        ("children in the northern parts of Akkar", ["akkar"]),
    ],
)
def test_a_region_is_a_place_unless_a_place_follows_it(ai, asked, keys):  # noqa: F811
    reading = places.read(asked)
    assert reading.keys == keys and not reading.breakdown
    if keys[:2] in (["south", "nabatieh"], ["north", "akkar"]):  # never a word to search
        assert not {"southern", "northern", "region", "governorates"} & set(reading.text.split())


@pytest.mark.parametrize(
    "asked",
    ["the southern governorates", "southern governorates", "the south governorates", "the south region"],
)
def test_the_southern_governorates_are_the_south_and_nabatieh_and_the_line_says_so(ai, asked):  # noqa: F811
    load(("2024", ["In Bint Jbeil, child labour rose.", "In Akkar, child labour rose."]))
    picked = _picked(f"child labour in {asked}")
    assert not picked.breakdown and picked.groups == [["labour", "labor"]]
    assert picked.places == "the South (South and Nabatieh governorates)"
    assert picked.notes[0].startswith("Only 1 finding on this subject names the South (South and Nabatieh")
    assert _ids(picked)[0] == ids_of("In Bint Jbeil, child labour rose.")[0]
    # one governorate named "southern governorate" stays the South governorate only
    assert places.read("child labour in the southern governorate").keys == ["south"]


def test_a_place_inside_another_places_name_is_not_that_place(ai):  # noqa: F811
    texts = (
        "In Bint Jbeil, 30% of schools closed.",
        "In Jbeil, school enrolment rose to 92%.",
        "In North Metn, 4% of children worked.",
        "In the North, 22% of children were out of school.",
    )
    load(("2024", texts))
    assert read(_picked("situation of children in Mount Lebanon")) == {texts[1], texts[2]}
    assert read(_picked("situation of children in Nabatieh")) == {texts[0]}
    assert read(_picked("situation of children in the North")) == {texts[3]}
    assert places.shadows("jbeil") == ("bint jbeil", "bent jbeil", "bint-jbeil")


def test_a_place_inside_another_places_name_counts_with_its_own_place_and_in_a_breakdown(ai):  # noqa: F811
    texts = (
        "In Bint Jbeil, 30% of schools closed.",
        "In Jbeil, school enrolment rose to 92%.",
        "In North Metn, 4% of children worked.",
        "Fake rates rose.",
    )
    load(("2024", texts))
    assert {"bint jbeil", "bint jbail", "bent jbeil", "bint-jbeil"} <= set(places.breakdown_alternatives())
    for question in (
        "situation of children in all gov",
        "situation of children by district",
        "situation of children in Mount Lebanon and Nabatieh",
        "situation of children in Mount Lebanon and South Lebanon",
        "situation of children in Keserwan-Jbeil and the South",
        "situation of children in Jbeil and Bint Jbeil",
    ):
        assert read(_picked(question)) >= set(texts[:2]), question
    for question in (
        "situation of children in all gov",
        "situation of children in Mount Lebanon and the North",
    ):
        assert texts[2] in read(_picked(question)), question
    assert read(_picked("situation of children in Jbeil")) == {texts[1]}
    assert read(_picked("situation of children in the North and Akkar")) == set()


def test_south_metn_and_north_bekaa_are_not_the_south_or_the_north(ai):  # noqa: F811
    texts = (
        "In South Metn, 12% of children were out of school.",
        "In North Bekaa, 9% of children were out of school.",
        "In Saida, 15% of children were out of school.",
        "In Tripoli, 35% of children were out of school.",
        "In Zahle, 8% of children were out of school.",
        "In Hermel, 7% of children were out of school.",
        "In North Lebanon and North Bekaa, 5% of children were out of school.",
    )
    load(("2024", texts))
    assert places.read("out of school children in South Metn").keys == ["baabda"]
    assert places.read("out of school children in southern Metn").keys == ["baabda"]
    assert places.read("out of school children in North Bekaa").keys == ["baalbek-hermel"]
    assert not places.compares(texts[0]) and not places.compares(texts[1]) and places.compares(texts[6])
    assert read(_picked("situation of children in South Lebanon")) == {texts[2]}
    assert read(_picked("situation of children in the south")) == {texts[2]}
    assert read(_picked("situation of children in the North")) == {texts[3], texts[6]}
    assert read(_picked("situation of children in the Bekaa")) == {texts[4]}
    assert read(_picked("situation of children in Mount Lebanon")) == {texts[0]}
    assert read(_picked("situation of children in Baabda")) == {texts[0]}
    assert read(_picked("situation of children in Baalbek-Hermel")) == {texts[1], texts[5], texts[6]}
    assert read(_picked("situation of children in North Bekaa")) == {texts[1], texts[5], texts[6]}
    # in a breakdown, each counts once, as its own governorate: never ranked first as naming two
    for text in texts[:2]:
        found = KMFinding.objects.filter(key_finding=text)
        assert sum(found.filter(ask.hit(g)).exists() for g in places.governorate_alternatives()) == 1, text


def test_a_text_that_names_a_place_on_its_own_too_names_it(ai):  # noqa: F811
    texts = (
        "Child labour in the Bekaa and North Bekaa rose to 20%.",
        "Child labour in the North and North Bekaa rose to 16%.",
        "In North Bekaa, 9% of children were in child labour.",
        "In the South and South Bekaa, 7% of children were in child labour.",
        "In Jbeil and Bint Jbeil, 5% of children were in child labour.",
        "In North-Bekaa, 6% of children were in child labour.",
    )
    load(("2024", texts))
    assert read(_picked("situation of children in the Bekaa")) == {texts[0], texts[3]}
    assert read(_picked("situation of children in the North")) == {texts[1]}
    assert read(_picked("situation of children in Baalbek-Hermel")) == {
        texts[0],
        texts[1],
        texts[2],
        texts[5],
    }
    assert read(_picked("situation of children in the South")) == {texts[3], texts[4]}
    assert read(_picked("situation of children in Mount Lebanon")) == {texts[4]}
    assert read(_picked("situation of children in Nabatieh")) == {texts[4]}


# governorates listed with a comma, a full stop or a line break between them: each is named ("the South,
# Bekaa" is never "South Bekaa"); only a space or one hyphen joins the words of a longer name
LISTS = (
    "In the South, Bekaa and Baalbek-Hermel, 31% of children were out of school.",
    "Out-of-school rates were highest in the North, Bekaa and Akkar (28%).",
    "Displacement rose in the South. Bekaa governorate hosted 20,000 displaced children.",
    "Child marriage was reported in the South, Beqaa and Mount Lebanon.",
    "In Jbeil, Bint Jbeil and Tyre, 4% of children worked.",
    "In South Bekaa, 7% of children worked.",
    "In North-Bekaa, 6% of children worked.",
    "In South Metn, 5% of children worked.",
    "In Bint Jbeil, 3% of children worked.",
    "Rates rose in the North\nBekaa hosted more children.",
    "In South  Bekaa and South-Metn, 2% of children worked.",
)


def test_a_list_of_places_names_each_of_them(ai):  # noqa: F811
    t = LISTS
    load(("2024", t[:5]), ("2024", t[5:]))  # at most 6 findings a document are read
    assert read(_picked("situation of children in the South")) == {t[0], t[2], t[3], t[4], t[8]}
    assert read(_picked("situation of children in the North")) == {t[1], t[9]}
    assert read(_picked("situation of children in the Bekaa")) == {t[0], t[1], t[2], t[3], t[5], t[9], t[10]}
    assert read(_picked("situation of children in Baalbek-Hermel")) == {t[0], t[6]}
    assert read(_picked("situation of children in North-Bekaa")) == {t[0], t[6]}
    assert read(_picked("situation of children in Jbeil")) == {t[4]}
    assert read(_picked("situation of children in Mount Lebanon")) == {t[3], t[4], t[7], t[10]}
    assert read(_picked("situation of children in Nabatieh")) == {t[4], t[8]}
    # in a breakdown, each counts for every governorate it names, and only those
    keys = [g.key for g in places.GOVERNORATES]
    expected = {
        t[1]: {"north", "bekaa", "akkar"},
        t[2]: {"south", "bekaa"},
        t[5]: {"bekaa"},
        t[6]: {"baalbek-hermel"},
        t[9]: {"north", "bekaa"},
        t[10]: {"bekaa", "mount-lebanon"},
    }
    for text, named in expected.items():
        found = KMFinding.objects.filter(key_finding=text)
        groups = zip(keys, places.governorate_alternatives(), strict=True)
        assert {key for key, g in groups if found.filter(ask.hit(g)).exists()} == named, text
    # a question, and a finding's text read for a breakdown, by the same rule
    assert places.read("out of school children in North-Bekaa").keys == ["baalbek-hermel"]
    assert places.read("out of school children in South-Metn").keys == ["baabda"]
    asked = places.read("out of school children in the South, Bekaa and Akkar")
    assert asked.keys == ["south", "nabatieh", "bekaa", "akkar"]
    assert not places.compares(t[6]) and places.compares(t[1]) and places.compares(t[2])


# a longer name joined to the next word ("Bint Jbeil-based"), written with a dash ("Bint–Jbeil"), or places
# with a slash between them ("the North/Bekaa"): each names the places it writes, and only those
JOINED = (
    "In the Bint Jbeil-Marjayoun area, 40% of children were out of school.",
    "Bint Jbeil-based families reported that 35% of children were out of school.",
    "In South Metn-based shelters, 22% of displaced children were out of school.",
    "North Bekaa-based groups found 30% of children out of school.",
    "In Bint–Jbeil, 17% of children were out of school.",
    "In North—Bekaa, 19% of children were out of school.",
    "In the North/Bekaa, 13% of children worked.",
    "In Akkar/North, 30% of children worked.",
    "In South Metn/Baabda, 12% of children worked.",
)


def test_a_longer_name_joined_to_a_word_or_a_slash_names_only_its_place(ai):  # noqa: F811
    t = JOINED
    load(("2024", t[:5]), ("2024", t[5:]))
    keys = [g.key for g in places.GOVERNORATES]
    expected = {
        t[0]: {"nabatieh"},
        t[1]: {"nabatieh"},
        t[2]: {"mount-lebanon"},
        t[3]: {"baalbek-hermel"},
        t[4]: {"nabatieh"},
        t[5]: {"baalbek-hermel"},
        t[6]: {"north", "bekaa"},
        t[7]: {"akkar", "north"},
        t[8]: {"mount-lebanon"},
    }
    for text, named in expected.items():
        found = KMFinding.objects.filter(key_finding=text)
        groups = zip(keys, places.governorate_alternatives(), strict=True)
        assert {key for key, g in groups if found.filter(ask.hit(g)).exists()} == named, text
    assert read(_picked("situation of children in Jbeil")) == set()
    assert read(_picked("situation of children in Nabatieh")) == {t[0], t[1], t[4]}
    assert read(_picked("situation of children in the North")) == {t[6], t[7]}
    assert read(_picked("situation of children in Mount Lebanon")) == {t[2], t[8]}
    assert read(_picked("situation of children in Baalbek-Hermel")) == {t[3], t[5]}
    # a finding loaded before its texts' slashes were spaced in the index: "South Metn/Baabda" is still no South
    KMFinding.objects.filter(key_finding=t[8]).update(
        own_vector=SearchVector("key_finding", weight="A", config="english")
    )
    south = places.governorate_alternatives()[keys.index("south")]
    assert not KMFinding.objects.filter(key_finding=t[8]).filter(ask.hit(south)).exists()


def test_retrievals_reads_of_every_finding_may_be_shared_out_among_parallel_workers(db):
    from django.db import connection

    with ask._shared_out(), connection.cursor() as cursor:
        cursor.execute("SHOW parallel_setup_cost")
        assert cursor.fetchone()[0] == str(ask.PARALLEL_SETUP_COST)
        cursor.execute("SHOW cpu_operator_cost")
        assert cursor.fetchone()[0] == str(ask.CPU_OPERATOR_COST)


def test_west_bekaa_is_in_the_bekaa_and_the_bekaa_is_not_west_bekaa(ai):  # noqa: F811
    texts = ("In West Bekaa, 11% of children worked.", "In Zahle, 8% of children worked.")
    load(("2024", texts))
    assert read(_picked("child labour in the Bekaa")) == set(texts)
    assert read(_picked("child labour in West Bekaa")) == {texts[0]}
    assert places.read("child labour in West Bekaa").keys == ["west-bekaa"]


def test_two_places_are_said_with_or(ai):  # noqa: F811
    load(("2024", ["In Akkar, fake child labour rose.", "In Tripoli, fake child labour fell."]))
    picked = _picked("education in Jabal Lubnan and Al Janoub")
    assert picked.groups == [["education"]]
    picked = _picked("child labour in Akkar and the North governorate")
    assert picked.notes == ["Places from your question: Akkar or the North."]


# ------------------------------------------------------------------------------- words of a question
def test_needs_as_a_noun_and_population_type_as_a_request(ai):  # noqa: F811
    assert ["needs"] in ask._groups("children with special needs in public schools")
    assert ["population"] not in ask.understand(Q3, Q3).groups
    assert ["population"] in ask._groups("refugee population growth")


def test_a_gender_lens_reads_findings_on_girls_or_boys(ai):  # noqa: F811
    texts = (
        "Gender gaps in learning widened.",
        "Girls were more likely than boys to be out of school.",
        "Fake rainfall fell.",
    )
    load(("2024", texts))
    assert read(_picked("with a gender lens")) == set(texts[:2])


def test_identical_texts_of_two_documents_are_both_read_with_their_years(ai):  # noqa: F811
    water = "In Nabatieh, 41% of children lacked safe water."
    load(("2019", [water]), ("2025", [water]))
    picked = _picked("safe water")
    assert len(picked.findings) == 2 and picked.copies == {}
    assert {f.year for f in picked.findings} == {2019, 2025}


# ------------------------------------------------------------------------------------ follow-ups
CHILD_LABOUR = (
    "In Akkar, 20% of children were in child labour.",
    "In Akkar, girls lacked safe spaces.",
    "Nationally, girls lacked safe spaces in schools.",
    "Nationally, 5% of children were in child labour.",
)


def test_an_addition_no_finding_joins_to_the_subject_stays_an_angle_next_turn(ai):  # noqa: F811
    load(("2024", CHILD_LABOUR))
    first = ids_of(CHILD_LABOUR[0])
    earlier = [{"question": "child labour in Akkar", "answer": "Fake.", "cited": first}]
    picked = _picked("and for girls?", earlier=earlier)
    assert picked.follow_up and ids_of(CHILD_LABOUR[1])[0] in _ids(picked)
    # no finding joins girls to child labour: girls are read alone, and stay an angle on child labour (an
    # addition is never a new subject)
    assert picked.new_subject == []
    turn = {"question": "and for girls?", "answer": "Fake.", "cited": ids_of(CHILD_LABOUR[1])}
    earlier.append(turn)
    picked = _picked("do the same for all lebanon", earlier=earlier)
    assert read(picked) == {CHILD_LABOUR[1], CHILD_LABOUR[2]}
    # the earlier answer's finding read first, and said
    assert _ids(picked)[0] == ids_of(CHILD_LABOUR[1])[0] and picked.cited_again == 1
    assert (
        picked.notes[0]
        == "A follow-up of your earlier question; the findings its answer cited were read again."
    )


TRIPOLI = (
    "In Tripoli, 35% of children were out of school.",
    "In Tripoli, child marriage affected 13% of girls.",
    "In Akkar, child marriage affected 9% of girls.",
)
TRIPOLI_TURN = {"question": "out of school children in Tripoli", "answer": "Fake.", "cited": []}


@pytest.mark.parametrize(
    "question",
    [
        "what about child marriage?",
        "and what about child marriage?",
        "And how about child marriage?",
        # a subject alone, or added with "and": a new subject too (never a group of people)
        "child marriage",
        "child marriage?",
        "and for child marriage?",
    ],
)
def test_a_follow_up_on_a_new_subject_keeps_the_place_and_not_the_earlier_findings(ai, question):  # noqa: F811
    load(("2024", TRIPOLI))
    earlier = [{**TRIPOLI_TURN, "cited": ids_of(TRIPOLI[0])}]
    picked = _picked(question, earlier=earlier)
    assert picked.follow_up and _ids(picked)[0] == ids_of(TRIPOLI[1])[0]
    assert ids_of(TRIPOLI[0])[0] not in _ids(picked)
    assert picked.notes[0] == "A follow-up of your earlier question."
    assert picked.notes[1].startswith("Only 1 finding on this subject names Tripoli (the North)")
    # the next follow-up's subject
    assert ["child marriage", "early marriage"] in picked.new_subject


@pytest.mark.parametrize(
    "question",
    [
        "adjust the text above with a lens on child marriage",
        "can you rewrite it with a lens on child marriage",
    ],
)
def test_a_rework_on_a_new_subject_keeps_the_earlier_findings_first(ai, question):  # noqa: F811
    load(("2024", TRIPOLI))
    earlier = [{**TRIPOLI_TURN, "cited": ids_of(TRIPOLI[0])}]
    picked = _picked(question, earlier=earlier)
    # no finding joins child marriage to out of school: its own words are searched alone, in Tripoli first,
    # and the earlier answer's finding stays first (it is reworked)
    assert _ids(picked) == ids_of(*TRIPOLI) and picked.cited_again == 1 and picked.new_subject == []
    assert (
        picked.notes[0]
        == "A follow-up of your earlier question; the findings its answer cited were read again."
    )


def test_the_owners_q3_after_q1_keeps_q1s_figure(ai):  # noqa: F811
    texts = (
        "In Mount Lebanon, 18,300 children aged 6-14 were out of school.",
        "In Mount Lebanon, girls were more likely than boys to miss meals.",
        "In Mount Lebanon, children with disabilities lacked transport.",
        "In Akkar, 27% of girls were out of school.",
    )
    load(("2025", texts))
    earlier = [{"question": Q1, "answer": "Fake.", "cited": ids_of(texts[0])}]
    picked = _picked(Q3, earlier=earlier)
    assert _ids(picked)[0] == ids_of(texts[0])[0] and picked.cited_again == 1
    assert set(ids_of(*texts[1:3])) <= set(_ids(picked))
    # a rework's angle stays an angle on Q1's subject: Q4 reads out of school girls in all of Lebanon
    assert picked.new_subject == []
    earlier.append({"question": Q3, "answer": "Fake.", "cited": ids_of(texts[0]), "new_subject": []})
    picked = _picked(Q4, earlier=earlier)
    assert not picked.carried_only and texts[3] in read(picked)


GIRLS = (
    (
        "2025",
        [
            "In Akkar, 30% of children were out of school.",
            "In Akkar, girls had fewer fake textbooks than boys.",
            "In Akkar, girls joined a fake chess club.",
        ],
    ),
    ("2025", ["In Beirut, 25% of girls were out of school.", "Girls in Saida joined a fake art club."]),
    (
        "2024",
        [
            "Out-of-school girls in Baalbek worked on fake farms.",
            "Nationally, 12% of boys were out of school.",
        ],
    ),
)


@pytest.mark.parametrize(
    "angle",
    [
        "adjust it for girls",
        "adjust the text above for girls",
        "can you adjust the text above with a lence on gender",
        "what about girls?",
        "and for girls?",
    ],
)
def test_an_angle_on_the_subject_stays_an_angle_for_the_next_follow_up(ai, angle):  # noqa: F811
    load(*GIRLS)
    akkar = ids_of("In Akkar, 30% of children were out of school.")
    earlier = [{"question": "out of school children in Akkar", "answer": "Fake.", "cited": akkar}]
    picked = _picked(angle, earlier=earlier)
    assert picked.new_subject == [], angle  # a rework, an addition or a group of people: never a new subject
    earlier.append({"question": angle, "answer": "Fake.", "cited": akkar, "new_subject": picked.new_subject})
    picked = _picked("can u do it for all lebanon?", earlier=earlier)
    found = read(picked)
    assert {
        "In Beirut, 25% of girls were out of school.",
        "Out-of-school girls in Baalbek worked on fake farms.",
    } <= found
    assert not found & {
        "In Akkar, girls joined a fake chess club.",
        "Girls in Saida joined a fake art club.",
    }, angle


def test_a_follow_up_for_all_of_lebanon_after_a_place_alone_reads_beyond_the_place(ai):  # noqa: F811
    texts = (
        "In Akkar, 27% of children were out of school.",
        "In Akkar, girls dropped out to work.",
        "In Tripoli, fake rainfall flooded schools.",
        "Nationally, 5% of children were in child labour.",
    )
    load(("2025", texts[:2]), ("2024", texts[2:]))
    earlier = [{"question": "situation of children in Akkar", "answer": "Fake.", "cited": ids_of(*texts[:2])}]
    for question in ("can u do it for all lebanon?", "and for all of Lebanon?", "same for all of lebanon"):
        picked = _picked(question, earlier=earlier)
        assert picked.follow_up and picked.national and not picked.carried_only, question
        assert _ids(picked)[:2] == ids_of(*texts[:2]) and set(_ids(picked)) == set(ids_of(*texts)), question
        meta = ask._meta(ask.words.ANSWERED_META, picked, len(picked.findings))
        assert "no other finding matches" not in meta and "All of Lebanon, from your question" in meta


# an earlier subject, a follow-up that changed it ("what about child marriage?"), then an angle on that one
SWITCHED = (
    *TRIPOLI,
    "In Tripoli, girls were more likely than boys to be out of school (28% vs 20%).",
    "In Tripoli, 10% of children were in child labour.",
    "In Akkar, girls were more likely than boys to be out of school.",
)


def test_a_follow_up_after_a_change_of_subject_keeps_the_new_subject(client_viewer, ai):  # noqa: F811
    load(("2024", SWITCHED))
    conversation = uuid.uuid4()
    cm_tripoli, cm_akkar, oos_girls = ids_of(SWITCHED[1], SWITCHED[2], SWITCHED[3])
    ai(
        [
            answer(f"In Tripoli, 35% were out of school [{ids_of(SWITCHED[0])[0]}]."),
            answer(f"In Tripoli, child marriage affected 13% of girls [{cm_tripoli}]."),
            answer(f"Girls [{cm_tripoli}]."),
        ]
    )
    for question in ("out of school children in Tripoli", "what about child marriage?", "and for girls?"):
        final = done(post(client_viewer, question, conversation=conversation))
    row = KMQuestion.objects.order_by("-created_at").first()
    # the subject is child marriage now: girls in it, never girls out of school
    assert row.read == [cm_tripoli, cm_akkar] and oos_girls not in row.read
    assert final["meta"].startswith(
        "Answered from 2 matching findings. Same subject as your earlier question"
    )


def test_a_follow_up_that_matches_one_of_its_two_words_says_so(ai):  # noqa: F811
    load(("2024", SWITCHED))
    earlier = [{**TRIPOLI_TURN, "cited": ids_of(SWITCHED[0])}]
    picked = _picked("what about child marriage and child labour?", earlier=earlier)
    # no finding joins either to out of school, and none holds both: those holding one were read
    assert picked.loose and set(ids_of(SWITCHED[1], SWITCHED[4])) <= set(_ids(picked))
    assert "No finding matches all your words: findings that match one of them were read." in picked.notes
    # the new subject is kept as it was read: any one of its words (one group)
    assert len(picked.new_subject) == 1
    assert set(picked.new_subject[0]) == {"child marriage", "early marriage", "labour", "labor"}
    turn = {"question": "what about child marriage and child labour?", "answer": "Fake."}
    earlier.append({**turn, "cited": ids_of(SWITCHED[1]), "new_subject": picked.new_subject})
    picked = _picked("can u do it for all lebanon?", earlier=earlier)
    assert not picked.carried_only and set(SWITCHED[1:3]) | {SWITCHED[4]} <= read(picked)
    picked = _picked("and in Akkar?", earlier=earlier)
    assert _ids(picked)[0] == ids_of(SWITCHED[2])[0] and picked.in_place == 1


def test_an_angle_on_the_subject_found_elsewhere_keeps_the_subject(ai):  # noqa: F811
    texts = (
        "In Tripoli, 35% of children were out of school.",
        "In Tripoli, child marriage affected 13% of girls.",
        "In Akkar, girls were more likely than boys to be out of school.",
    )
    load(("2024", texts))
    earlier = [{**TRIPOLI_TURN, "cited": ids_of(texts[0])}]
    for question in ("and for girls?", "for girls"):
        picked = _picked(question, earlier=earlier)
        # no finding of Tripoli joins girls to out of school, one elsewhere does: the subject stays, the place
        # is widened (never girls on any subject)
        assert _ids(picked) == ids_of(texts[0], texts[2]), question
        assert "subject" in picked.kept and picked.relaxed == "none", question


AKKAR = (
    "In Akkar, 27% of children were out of school.",
    "In Akkar, girls were more likely than boys to be out of school (31% vs 22%).",
    "In Akkar, child marriage affected 9% of girls.",
    "In Akkar, child marriage rose after the crisis.",
    "In Akkar, early childhood development centres closed.",
    "In Akkar, 30% of women lacked care during pregnancy.",
    "In Tripoli, child marriage affected 13% of girls.",
    "In Tripoli, 40% of Syrian refugee children were out of school.",
    "In Akkar, refugee families lacked birth registration.",
)
AKKAR_TURN = {"question": "out of school children in Akkar", "answer": "Fake.", "cited": []}


def test_a_subject_alone_then_a_group_of_people_reads_the_new_subject(ai):  # noqa: F811
    load(("2024", AKKAR))
    earlier = [{**AKKAR_TURN, "cited": ids_of(*AKKAR[:2])}]
    picked = _picked("child marriage", earlier=earlier)
    # a subject of its own that no finding joins to out of school: the out-of-school answer's findings are not
    # read first, and child marriage is the next follow-up's subject
    assert picked.follow_up and read(picked) >= set(AKKAR[2:4]) and not read(picked) & set(AKKAR[:2])
    assert [set(g) for g in picked.new_subject] == [{"child marriage", "early marriage"}]
    turn = {"question": "child marriage", "answer": "Fake.", "cited": ids_of(AKKAR[2])}
    earlier.append({**turn, "new_subject": picked.new_subject})
    picked = _picked("and for girls?", earlier=earlier)
    # girls on child marriage, never girls out of school
    assert ids_of(AKKAR[2])[0] in _ids(picked) and AKKAR[1] not in read(picked)
    assert picked.new_subject == [] and "subject" in picked.kept


@pytest.mark.parametrize(
    ("question", "text"),
    [
        ("what about ECD?", AKKAR[4]),
        ("what about early childhood development?", AKKAR[4]),
        ("what about pregnancy?", AKKAR[5]),
        ("and what about preschool?", None),
    ],
)
def test_an_age_group_named_as_a_subject_is_a_new_subject(ai, question, text):  # noqa: F811
    load(("2024", [*AKKAR, "In Akkar, preschool enrolment fell to 20%."]))
    earlier = [{**AKKAR_TURN, "cited": ids_of(*AKKAR[:2])}]
    picked = _picked(question, earlier=earlier)
    assert picked.follow_up and not read(picked) & set(AKKAR[:2]), question
    assert picked.new_subject and (text is None or text in read(picked)), question


@pytest.mark.parametrize(
    "question",
    [
        "what about refugees?",
        "what about syrian refugees?",
        "what about displaced children?",
        "and for Palestinian refugees?",
        "what about migrants and the host community?",
        "now focus on refugees",
        "with a focus on refugees",
        "now with a lens on IDPs",
    ],
)
def test_a_population_group_is_an_angle_on_the_subject(ai, question):  # noqa: F811
    load(("2024", AKKAR))
    earlier = [{**AKKAR_TURN, "cited": ids_of(AKKAR[0])}]
    picked = _picked(question, earlier=earlier)
    # never a new subject: out of school stays the subject of the next follow-up
    assert picked.follow_up and picked.new_subject == [], question
    if question == "what about refugees?":  # refugees out of school elsewhere: the place widened
        assert "subject" in picked.kept and AKKAR[7] in read(picked)
    turn = {"question": question, "answer": "Fake.", "cited": ids_of(AKKAR[0]), "new_subject": []}
    picked = _picked("and for girls?", earlier=[*earlier, turn])
    assert AKKAR[1] in read(picked), question


def test_a_follow_up_asking_for_an_update_keeps_the_subject_and_place_with_its_year(ai, today):  # noqa: F811
    load(
        ("2024", AKKAR[:2]),
        ("2026", ["In Akkar, 25% of children were out of school.", "In Akkar, fake teachers left."]),
    )
    earlier = [{**AKKAR_TURN, "cited": ids_of(AKKAR[0])}]
    for question in (
        "is there an update this year?",
        "any update for 2026?",
        "is there anything newer this year?",
        "anything new this year?",
        "are there updated figures for 2026?",
    ):
        picked = _picked(question, earlier=earlier)
        assert picked.follow_up and picked.kept[:2] == ["subject", "places"], question
        assert picked.filters.year == ["2026"] and read(picked) == {
            "In Akkar, 25% of children were out of school."
        }, question


def test_translating_findings_of_a_subject_of_its_own_is_a_new_question(ai):  # noqa: F811
    load(("2024", [*TRIPOLI, "In Mount Lebanon, 18,300 children were out of school."]))
    q1 = "number of out of school children in mont lebanon"
    earlier = [
        {
            "question": q1,
            "answer": "Fake.",
            "cited": ids_of("In Mount Lebanon, 18,300 children were out of school."),
        }
    ]
    for question in (
        "can you translate the key findings on child marriage into Arabic?",
        "translate the findings on child marriage to French",
    ):
        picked = _picked(question, earlier=earlier)
        assert not picked.follow_up and picked.places == "", question
        assert read(picked) == set(TRIPOLI[1:]) and picked.groups == [["child marriage", "early marriage"]], (
            question
        )
    for question in (
        "translate it to Arabic",
        "translate into Arabic please",
        "can you translate this into French?",
        "translate please",
    ):
        picked = _picked(question, earlier=earlier)
        assert picked.follow_up and picked.places == "Mount Lebanon" and picked.cited_again == 1, question


LANGUAGES = (
    "In Mount Lebanon, 18,300 children were out of school.",
    "Arabic literacy among refugee children in Akkar fell to 40%.",
    "In the Metn, out-of-school rates were lowest.",
    "English classes in Akkar were full.",
    "Longer distances to school kept children at home in Akkar.",
)


@pytest.mark.parametrize(
    "question",
    [
        "the same in Arabic",
        "I need it in Arabic",
        "can I have it in French",
        "shorter please",
        "a longer version",
        "I need it shorter",
        "please answer in Arabic",
        "can you give it to me in arabic",
        "Arabic please",
        "can u do it in arabic",
        "the same in English, please",
        "more formal please",
        "the same for donors",
    ],
)
def test_a_follow_up_asking_for_another_language_or_length_keeps_the_subject(ai, question):  # noqa: F811
    load(("2025", LANGUAGES))
    earlier = [
        {
            "question": "number of out of school children in mont lebanon",
            "answer": "Fake.",
            "cited": ids_of(LANGUAGES[0]),
        }
    ]
    picked = _picked(question, earlier=earlier)
    assert picked.follow_up and picked.kept[:2] == ["subject", "places"], question
    assert picked.places == "Mount Lebanon" and _ids(picked)[0] == ids_of(LANGUAGES[0])[0]
    asked = {w for g in picked.groups for alt in g for w in alt.split()}
    assert not asked & {"arabic", "french", "english", "shorter", "longer", "formal", "donors"}, question
    assert LANGUAGES[1] not in read(picked)


@pytest.mark.parametrize(
    ("question", "word", "text"),
    [
        ("what about Arabic literacy?", "arabic", LANGUAGES[1]),
        ("and English classes?", "english", LANGUAGES[3]),
        ("what about longer distances to school?", "longer", LANGUAGES[4]),
    ],
)
def test_a_follow_up_on_a_language_or_a_length_as_a_subject_searches_it(ai, question, word, text):  # noqa: F811
    load(("2025", LANGUAGES))
    earlier = [
        {
            "question": "number of out of school children in mont lebanon",
            "answer": "Fake.",
            "cited": ids_of(LANGUAGES[0]),
        }
    ]
    picked = _picked(question, earlier=earlier)
    assert any(word in alt.split() for g in picked.groups for alt in g), question
    assert text in read(picked), question


def test_only_the_earlier_answers_findings_read_says_so(ai):  # noqa: F811
    load(("2024", ["Fake rainfall flooded schools.", "Fake teachers left."]))
    cited = ids_of("Fake rainfall flooded schools.")
    earlier = [{"question": "rainfall flooding schools", "answer": "Fake.", "cited": cited}]
    picked = _picked("can you do it for all governorates?", earlier=earlier)
    assert _ids(picked) == cited
    meta = ask._meta(ask.words.ANSWERED_META, picked, 1)
    assert meta.startswith(
        "Answered from the 1 finding your earlier answer cited, read again: no other finding matches. Same "
        "subject as your earlier question. By governorate"
    )


# ------------------------------------------------------------------------------------------ tables
TABLE_HEAD = "| Indicator or measure | Figure as stated | Year | Finding ID |\n"
SHAPES = {
    # lines broken by a carriage return alone, a line of no-break spaces (no blank line for markdown), the
    # words of a link written in a cell (no link)
    "rows on carriage returns": (
        TABLE_HEAD.replace("\n", "\r") + "|---|---|---|---|\r| Fake rate | 40% | 2024 | TST-0001-F00001 |\r"
        "| Fake unsupported rate | 99% | 2024 | none |\r| Fake other rate | 77% | 2025 | |",
        2,
    ),
    "the words of a link in a cell": (
        TABLE_HEAD + "|---|---|---|---|\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        '| Fake unsupported rate | 99% | 2024 | href="/km/findings/ |\n'
        '| Fake other rate | 77% | 2025 | `<a href="/km/findings/` |',
        2,
    ),
    "a line of no-break spaces": (
        TABLE_HEAD + "|---|---|---|---|\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n\u00a0\n"
        '| Fake unsupported rate | 99% | 2024 | `href="/km/findings/` |\n| Fake other rate | 77% | 2025 | |',
        2,
    ),
    "a line without a pipe": (
        TABLE_HEAD
        + "|---|---|---|---|\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\nFake unsupported rate 99%\n"
        "| Fake other rate | 77% | 2025 | none |",
        2,
    ),
    "a rule with a trailing space": (
        TABLE_HEAD + "|---|---|---|---| \n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "| Fake unsupported rate | 99% | 2024 | none |\n| Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a rule of spaced dashes": (
        TABLE_HEAD + "| - - | - - | - - | - - |\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "| Fake unsupported rate | 99% | 2024 | none |\n| Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a quoted table": (
        "> " + TABLE_HEAD + "> |---|---|---|---|\n> | Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "> | Fake unsupported rate | 99% | 2024 | none |\n> | Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a table on a list item": (
        "- " + TABLE_HEAD + "  |---|---|---|---|\n  | Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "  | Fake unsupported rate | 99% | 2024 | none |\n  | Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a table on a numbered item": (
        "1. " + TABLE_HEAD + "   |---|---|---|---|\n   | Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "   | Fake unsupported rate | 99% | 2024 | none |\n   | Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a table on a quoted list item": (
        "> - " + TABLE_HEAD + ">   |---|---|---|---|\n>   | Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        ">   | Fake unsupported rate | 99% | 2024 | none |\n>   | Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a quote with no space": (
        ">" + TABLE_HEAD + ">|---|---|---|---|\n>| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        ">| Fake unsupported rate | 99% | 2024 | none |\n>| Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a rule of spaced dashes with no leading pipe": (
        TABLE_HEAD + "- - | - - | - - | - - |\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "| Fake unsupported rate | 99% | 2024 | none |\n| Fake other rate | 77% | 2025 | |",
        2,
    ),
    # a pipe in code in the header: markdown reads the table, the check of the text does not (the rendered
    # rows are checked, and those taken out are taken out of the text too)
    "a pipe in code in the header": (
        "| Indicator `a|b` | Figure as stated | Year | Finding ID |\n|---|---|---|---|\n"
        "| Fake rate | 40% | 2024 | TST-0001-F00001 |\n| Fake unsupported rate | 99% | 2024 | none |\n"
        "| Fake other rate | 77% | 2025 | |",
        2,
    ),
    # markup in a row taken out of what is shown is taken out of the text too
    "a line break in a row of a table only markdown reads": (
        "| Indicator `a|b` | Figure as stated | Year | Finding ID |\n|---|---|---|---|\n"
        "| Fake rate | 40% | 2024 | TST-0001-F00001 |\n| Invented<br>fake hills | 99% | |\n"
        "| Fake other rate<sup>a</sup> | 77% | 2025 | |",
        2,
    ),
    # a tab after the header or the rule (markdown reads tabs as spaces), and text between "<" and ">" in the
    # rows taken out: in code, or as plain signs
    "a header ending in a tab": (
        TABLE_HEAD.replace(" |\n", " |\t\n")
        + "|---|---|---|---|\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "| Fake `<b>` unsupported rate | 99% | 2024 | none |\n| Fake stunting <5 in the hills | >77% | 2025 | |",
        2,
    ),
    "a rule ending in a tab": (
        TABLE_HEAD + "|---|---|---|---|\t\n| Fake rate | 40% | 2024 | TST-0001-F00001 |\n"
        "| Fake `<b>` unsupported rate | 99% | 2024 | none |\n| Fake stunting <5 in the hills | >77% | 2025 | |",
        2,
    ),
    "a pipe in code in the header and code in a row": (
        "| Indicator `a|b` | Figure as stated | Year | Finding ID |\n|---|---|---|---|\n"
        "| Fake rate | 40% | 2024 | TST-0001-F00001 |\n| Fake `<b>` unsupported rate | 99% | 2024 | none |\n"
        "| Fake stunting <5 in the hills | >77% | 2025 | |",
        2,
    ),
    "a table of one column": (
        "Fake figures |\n---|\nInvented 99% of fake girls<br>in the fake hills |\nFake rate 40% TST-0001-F00001 |",
        1,
    ),
    # a Finding ID read written as code: linked like any other, its row kept
    "a finding id in code": (
        TABLE_HEAD + "|---|---|---|---|\n| Fake rate | 40% | 2024 | `TST-0001-F00001` |\n"
        "| Fake unsupported rate | 99% | 2024 | none |\n| Fake other rate | 77% | 2025 | |",
        2,
    ),
    "a finding id in code in brackets": (
        TABLE_HEAD + "|---|---|---|---|\n| Fake rate | 40% | 2024 | `[TST-0001-F00001]` |\n"
        "| Fake unsupported rate | 99% | 2024 | none |\n| Fake other rate | 77% | 2025 | |",
        2,
    ),
    "an html table in a cell": (
        TABLE_HEAD + "|---|---|---|---|\n| Fake rate | 40% <table></table> | 2024 | TST-0001-F00001 |\n"
        "| Fake unsupported rate | 99% | 2024 | none |\n| Fake other rate | 77% | 2025 | |",
        2,
    ),
    # the AI writes markdown tables only: an HTML table is taken out whole, its rows counted
    "an html table": (
        "<table><tr><th>Indicator</th><th>Finding ID</th></tr><tr><td>Fake rate 40%</td><td>TST-0001-F00001</td>"
        "</tr><tr><td>Fake unsupported rate 99%</td><td>none</td></tr><tr><td>Fake other rate 77%</td><td></td>"
        "</tr></table>",
        3,
    ),
    "an html table in an html table": (
        "<table><tr><th>Indicator</th><th>Figure</th></tr><tr><td>Fake rate</td><td>40% <table></table></td></tr>"
        "<tr><td>Fake unsupported rate</td><td>99%</td></tr><tr><td>Fake other rate</td><td>77%</td></tr></table>",
        3,
    ),
    "an html table in an html cell": (
        "<TABLE><tr><th>Indicator</th></tr><tr><td><table><tr><td>Fake rate 40%</td></tr></table></td></tr>"
        "<tr><td>Fake unsupported rate: 99% of fake children were out of school in 2025</td></tr>"
        "<tr><td>Fake other rate 77%</td></tr></TABLE>",
        3,
    ),
}
HTML_SHAPES = ("an html table", "an html table in an html table", "an html table in an html cell")


def _shown(html_text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html_text).split())


@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_table_shape_keeps_only_rows_that_give_a_finding_read(ai, shape):  # noqa: F811
    factory.load(factory.workbook(documents=2, findings=4))
    text, removed = SHAPES[shape]
    checked = _finish("Fake rates rose [TST-0001-F00001].\n\n" + text)
    assert checked.rows_removed == removed, checked.html
    assert "/km/findings/TST-0001-F00001/" in checked.html
    if shape in HTML_SHAPES:
        assert "<table" not in checked.html and "40%" not in checked.html
        assert "<table" not in checked.answer.lower() and "40%" not in checked.answer
    else:
        assert "40%" in checked.html and checked.html.count("<tr>") == 2  # the header and the row kept
        assert "40%" in checked.answer and "<table" not in checked.answer
    # what is kept (the Copy text, the answer a follow-up reworks) is what is shown
    for gone in ("99%", "77%"):
        assert gone not in checked.html, (gone, checked.html)
        assert gone not in checked.answer, (gone, checked.answer)
    assert re.findall(r"\d+%", _shown(checked.html)) == re.findall(r"\d+%", checked.answer)


def test_a_rendered_row_is_kept_for_a_link_to_a_finding_never_for_its_words(ai):  # noqa: F811
    html_text = ask.render(
        '| Indicator | Finding ID |\n|---|---|\n| Invented 99% | href="/km/findings/ |', []
    )
    shown, removed, _gone = ask._unlinked_rows(html_text)
    assert removed == 1 and "<table" not in shown and "99%" not in shown


def test_the_answer_kept_and_copied_is_the_answer_shown(client_viewer, ai):  # noqa: F811
    factory.load(factory.workbook(documents=2, findings=4))
    conversation = uuid.uuid4()
    text, _removed = SHAPES["a table on a list item"]
    fake = ai([answer("Fake rates rose [TST-0001-F00001].\n\n" + text), answer("Fake [TST-0001-F00001].")])
    final = done(post(client_viewer, "zqkmtoken?", conversation=conversation))
    row = KMQuestion.objects.get()
    assert final["answer"] == row.answer and "40%" in row.answer
    assert "99%" not in row.answer and "77%" not in row.answer
    done(post(client_viewer, "shorten it", conversation=conversation))
    assert "40%" in _sent(fake) and "99%" not in _sent(fake)  # the answer the follow-up reworks


def test_a_table_whose_rows_all_give_no_finding_read_goes_whole(ai):  # noqa: F811
    factory.load(factory.workbook(documents=2, findings=4))
    checked = _finish(
        "Fake rates rose [TST-0001-F00001].\n\n" + TABLE_HEAD + "|---|---|---|---| \n| Fake | 9% | 2024 | |"
    )
    assert checked.rows_removed == 1 and "<table" not in checked.html


@pytest.mark.parametrize(
    "rows",
    ["Child marriage in Akkar | 90%", "Akkar | 90%", "Akkar | 90%\n| Fake other rate | 77% |"],
)
def test_a_row_under_a_rule_is_taken_out_where_markdown_reads_a_paragraph(ai, rows):  # noqa: F811
    factory.load(factory.workbook(documents=2, findings=4))
    checked = _finish("Fake rates rose [TST-0001-F00001].\n\nIndicator | Figure\n|---|---|\n" + rows)
    assert checked.rows_removed == rows.count("\n") + 1
    assert checked.answer == "Fake rates rose [TST-0001-F00001]." and "90%" not in checked.html
    # a row that gives a finding read stays, with the lines over it
    checked = _finish(
        "Fake rates rose [TST-0001-F00001].\n\nIndicator | Figure\n|---|---|\nFake rate | 40% TST-0001-F00001\n"
        + rows
    )
    assert checked.rows_removed == rows.count("\n") + 1 and "40%" in checked.answer
    assert "90%" not in checked.answer and "90%" not in checked.html


def test_a_claim_in_a_tables_header_is_counted_and_a_header_of_labels_is_not(ai):  # noqa: F811
    factory.load(factory.workbook(documents=1, findings=2))
    lead = "Fake lead sentence of the answer [TST-0001-F00001].\n\n"
    claim = (
        "| In Akkar 90% of girls are married by 15 | Finding ID |\n|---|---|\n| OOSC 27% | TST-0001-F00001 |"
    )
    assert _finish(lead + claim).uncited == 1
    for head in (
        "| Governorate | 2023 | 2024 | Finding ID |",
        "| % girls | Figure as stated | Year | Finding ID |",
        "| Out-of-school children aged 6-14 (%) | 2024 | Year | Finding ID |",
        TABLE_HEAD.strip(),
    ):
        table = head + "\n|---|---|---|---|\n| Fake rate | 23% | 2024 | [TST-0001-F00001] |"
        assert _finish(lead + table).uncited == 0, head


LEAD = "Fake lead sentence of the answer [TST-0001-F00001]. "
CLAIMS = [
    # a claim
    "The Excel under this answer shows that 99% of fake children are out of school in every governorate.",
    "The CSV survey of 2023 shows 40% of children work.",
    "Girls in the South do not report higher dropout than boys and the gap is small.",
    "Many adolescent girls who face harassment say they do not report it to anyone.",
    "Children in these villages do not report enough food at home during the winter months.",
    "Families say the cash grant is too small and it does not cover transport to school.",
    "Girls who marry early drop out, and those who stay do not give up on secondary school.",
    "The studies found that girls do not report harassment to their teachers or parents.",
    "Findings show girls do not report harassment to their teachers at school.",
    "As the Excel under this answer shows, girls with disabilities are the group most often excluded from school.",
    "The CSV of these findings confirms that boys leave school earlier than girls in every governorate.",
    "The Excel under this answer lists each finding read; girls with disabilities are the most excluded.",
    # people missing, people with no information: claims
    "Nearly 30% of girls in Akkar are missing from secondary school because they work.",
    "In half of the fake schools a qualified teacher is missing for grades 7 to 9.",
    "Children with disabilities in Baalbek-Hermel are missing out on education, with 40% never enrolled.",
    "In Akkar, 45% of households had no information on how to register children in school.",
    "No data were collected in the fake hills, where 60% of children work on farms.",
    "There is no evidence that the cash grant reduced dropout among boys in Akkar.",
    # what people said, or people in the studies: claims
    "Teachers in the fake villages said they do not give information to parents about dropout.",
    "Girls in these studies do not report harassment to their teachers or parents.",
    "Adolescents in the reports do not report harassment to anyone at school.",
    # a pointer with a clause of its own
    "The Excel under this answer lists the findings read, mostly from Akkar, where girls left school early.",
    "The Excel under this answer lists the findings read, and girls in Akkar left school early to marry.",
    "The CSV of these findings lists each finding read, as girls in Akkar left school early because of work.",
    # data as the subject of what the findings show, not of what is missing
    "Data show that 30% of girls in Akkar are missing from secondary school because they work.",
    "Figures show that a qualified teacher is missing in half of the fake schools of Akkar.",
    "The Excel you asked for shows that 99% of fake children are out of school in the hills.",
    # what is missing, then a figure in a clause of its own
    "Figures by sex are missing for Nabatieh, where 60% of girls left school by age 15.",
    "Data on children with disabilities are not available for the South, but 45% of them were out of school.",
    "Evidence for Akkar is lacking while 90% of girls there were married before 18.",
    "Figures by sex are missing for Akkar, and girls there left school early to work in the fields.",
]
NOT_CLAIMS = [
    # what is missing from the findings
    "What is missing is a breakdown of these figures by sex and by age.",
    "Figures by sex and by age are missing from the findings read here.",
    "There are no data on child labour by sex in the findings read.",
    "The age of the children in the fake villages is missing.",
    # what the findings lack, or a pointer to the file under the answer
    "The findings read do not report figures by sex for the fake villages.",
    "The findings read here do not give separate figures for girls with disabilities.",
    "They do not give figures by sex or by disability for the fake villages.",
    "The Excel of these findings, under this answer, lists each finding read with its figures.",
    "The Excel of these findings, under this answer, lists all 7 findings read with their figures.",
    "The Excel and the CSV of these findings are under this answer.",
    "You can download an Excel of the findings read under this answer.",
    "The Excel under this answer lists the 12 findings read, with their figures and years.",
    # what the owner's Q3 asks for: what is missing, and where the Excel is
    "Figures by disability are missing for the South governorate and for Nabatieh.",
    "Sex-disaggregated figures are missing for most governorates.",
    "Data on children with disabilities are missing in the findings for Nabatieh.",
    "Data disaggregated by disability are not available for the South governorate.",
    "Disability-specific figures are not available for Nabatieh in the findings read.",
    "Figures by sex are not available for 2024 in the South governorate and Nabatieh.",
    "Figures by disability are missing for the South governorate, and for Nabatieh.",
    "An Excel and a CSV of the findings read are available under this answer.",
    "An Excel of these findings is available under this answer.",
    "The Excel and the CSV under this answer list each finding read with its publication year and Finding ID.",
    "The Excel under this answer lists every finding read, including the ones not cited here.",
    "The Excel you asked for is under this answer.",
    "You can download the Excel and CSV of these findings under this answer.",
]


@pytest.mark.parametrize(("text", "uncited"), [(t, 1) for t in CLAIMS] + [(t, 0) for t in NOT_CLAIMS])
def test_a_sentence_that_names_a_download_or_says_do_not_is_still_a_claim(ai, text, uncited):  # noqa: F811
    factory.load(factory.workbook(documents=1, findings=2))
    assert _finish(LEAD + text).uncited == uncited


TABLE = TABLE_HEAD + "|---|---|---|---|\n| Fake rate | 23% | 2024 | [TST-0001-F00001] |"


@pytest.mark.parametrize(
    ("text", "uncited"),
    [
        # a line with a pipe that no table holds is prose: counted
        (TABLE + "\n\n| Invented | 99% of fake children in the hills | x |", 1),
        (TABLE + "\n\n| Invented | 99% | x |", 1),
        ("Out-of-school rates were highest in the fake hills | lowest in the fake coast.", 1),
        ("| Fake hills | 41% out of school in 2025 |\n| Fake coast | 12% out of school in 2025 |", 2),
        ("- Fake hills: 41% out of school | fake coast: 12% out of school in 2025.", 1),
        # a header with no pipe at either end over a rule between pipes: markdown reads no table
        (
            "In the fake hills 99% of girls work | in the fake coast 12%\n|---|---|\n| Real | [TST-0001-F00001] |",
            1,
        ),
        # the lines of a table are not
        (TABLE, 0),
        ("> " + TABLE.replace("\n", "\n> "), 0),
        ("- " + TABLE.replace("\n", "\n  "), 0),
        ("Fake rates by place:\n" + TABLE, 0),
    ],
)
def test_a_line_with_a_pipe_outside_a_table_is_prose(ai, text, uncited):  # noqa: F811
    factory.load(factory.workbook(documents=1, findings=2))
    assert _finish(LEAD + "\n\n" + text).uncited == uncited
