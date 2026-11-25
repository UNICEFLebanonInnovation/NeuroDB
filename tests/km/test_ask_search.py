"""What Ask the KM Database reads for a question: the words it searches and those it leaves out (requests,
figures, places), the AI's suggested search words, phrases and their word forms, places and breakdowns, the
years of a question, follow-ups in a conversation and the lines under the answer. Invented texts only;
places are public geography."""

from __future__ import annotations

import datetime

import pytest

from neurodb.km import ask, places, search
from neurodb.km import columns as cols
from neurodb.km.models import KMFinding
from tests.km import factory
from tests.km.test_ask import _finish, _ids, _picked, ai, terms, today  # noqa: F401 (fixtures)
from tests.km.test_ask_session import Q3, build

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
        # the year of an event, or a year compared with: no Year filter, never searched
        (
            "mental health of adolescents after the 2020 explosion",
            [],
            "mental health of adolescents after explosion",
        ),
        (
            "how did the 2024 war affect children in the south?",
            [],
            "how did war affect children in the south?",
        ),
        ("children affected by the August 2020 blast", [], "children affected by blast"),
        ("impact of 2020 port explosion on children", [], "impact of port explosion on children"),
        ("Lebanon's 2019 crisis and child labour", [], "Lebanon's crisis and child labour"),
        ("injuries from 2020 explosion", [], "injuries from explosion"),
        ("out of school rates compared to 2019", [], "out of school rates"),
        ("is child labour higher than 2019?", [], "is child labour higher ?"),
        ("is child labour higher than in 2019?", [], "is child labour higher ?"),
        ("out of school children before the 2019 crisis", [], "out of school children"),
        ("out of school children before 2020", [], "out of school children"),
        # "since the 2019 crisis": since 2019 (the event's words only rank)
        ("child marriage since the 2019 crisis", list(range(2026, 2018, -1)), "child marriage"),
        ("child labour since the 2019 economic crisis", list(range(2026, 2018, -1)), "child labour"),
        ("child labour since the 2019 school year", list(range(2026, 2018, -1)), "child labour school year"),
    ],
)
def test_years_of_a_question(asked, years, left):
    frame = ask.time_frame(asked, datetime.date(2026, 11, 25))
    assert frame.years == years
    assert " ".join(frame.text.split()) == left


@pytest.mark.parametrize(
    ("asked", "ranks"),
    [
        ("mental health of adolescents after the 2020 explosion", ["2020"]),
        ("out of school rates compared to 2019", ["2019"]),
        ("child marriage since the 2019 crisis", ["crisis"]),
        ("out of school children before the 2019 economic crisis", ["2019", "economic", "crisis"]),
        ("child marriage 2025", []),
    ],
)
def test_the_year_of_an_event_only_ranks(asked, ranks):
    assert ask.time_frame(asked, datetime.date(2026, 11, 25)).ranks == ranks


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
        "After the 2020 explosion, 30% of adolescents reported psychological distress.",
        "One year after the explosion, 1 in 3 adolescents in Beirut still had nightmares.",
    )
    load(("2023", texts[:1]), ("2021", texts[1:]), ("2020", ["Fake rainfall fell."]))
    picked = _picked("distress of adolescents after the 2020 explosion")
    assert picked.filters.year == [] and read(picked) == set(texts)
    assert ["2020"] not in picked.groups
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
    ],
)
def test_more_place_spellings_and_the_camps(ai, asked, text):  # noqa: F811
    load(("2024", [text, "Fake rates rose in Fakeland.", "Fake rates rose."]))
    picked = _picked(f"situation of children in {asked}")
    assert read(picked) == {text} and picked.groups == []


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
    picked = _picked("child labour in Akkar and the North")
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


def test_a_follow_up_that_fell_back_to_its_own_words_keeps_them_next_turn(ai):  # noqa: F811
    load(("2024", CHILD_LABOUR))
    first = ids_of(CHILD_LABOUR[0])
    earlier = [{"question": "child labour in Akkar", "answer": "Fake.", "cited": first}]
    picked = _picked("and for girls?", earlier=earlier)
    assert picked.follow_up and ids_of(CHILD_LABOUR[1])[0] in _ids(picked)
    earlier.append({"question": "and for girls?", "answer": "Fake.", "cited": ids_of(CHILD_LABOUR[1])})
    picked = _picked("do the same for all lebanon", earlier=earlier)
    assert read(picked) == {CHILD_LABOUR[1], CHILD_LABOUR[2]}
    # the earlier answer's finding is on girls: read first, and said
    assert _ids(picked)[0] == ids_of(CHILD_LABOUR[1])[0] and picked.cited_again == 1
    assert (
        picked.notes[0]
        == "A follow-up of your earlier question; the findings its answer cited were read again."
    )


def test_a_follow_up_on_a_new_subject_keeps_the_place_and_not_the_earlier_findings(ai):  # noqa: F811
    texts = (
        "In Tripoli, 35% of children were out of school.",
        "In Tripoli, child marriage affected 13% of girls.",
        "In Akkar, child marriage affected 9% of girls.",
    )
    load(("2024", texts))
    earlier = [
        {"question": "out of school children in Tripoli", "answer": "Fake.", "cited": ids_of(texts[0])}
    ]
    picked = _picked("what about child marriage?", earlier=earlier)
    assert picked.follow_up and _ids(picked)[0] == ids_of(texts[1])[0]
    assert ids_of(texts[0])[0] not in _ids(picked)
    assert picked.notes[0] == "A follow-up of your earlier question."
    assert picked.notes[1].startswith("Only 1 finding on this subject names Tripoli (the North)")


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
    # markdown leaves an HTML table as written: no reference in it becomes a link, so no row is kept
    "an html table": (
        "<table><tr><th>Indicator</th><th>Finding ID</th></tr><tr><td>Fake rate 40%</td><td>TST-0001-F00001</td>"
        "</tr><tr><td>Fake unsupported rate 99%</td><td>none</td></tr><tr><td>Fake other rate 77%</td><td></td>"
        "</tr></table>",
        3,
    ),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_table_shape_keeps_only_rows_that_give_a_finding_read(ai, shape):  # noqa: F811
    factory.load(factory.workbook(documents=2, findings=4))
    text, removed = SHAPES[shape]
    checked = _finish("Fake rates rose [TST-0001-F00001].\n\n" + text)
    assert checked.rows_removed == removed, checked.html
    assert "/km/findings/TST-0001-F00001/" in checked.html
    if shape == "an html table":
        assert "<table" not in checked.html and "40%" not in checked.html
    else:
        assert "40%" in checked.html and checked.html.count("<tr>") == 2  # the header and the row kept
    for gone in ("99%", "77%"):
        assert gone not in checked.html, (gone, checked.html)
        if shape != "an html table":
            assert gone not in checked.answer, (gone, checked.answer)


def test_a_table_whose_rows_all_give_no_finding_read_goes_whole(ai):  # noqa: F811
    factory.load(factory.workbook(documents=2, findings=4))
    checked = _finish(
        "Fake rates rose [TST-0001-F00001].\n\n" + TABLE_HEAD + "|---|---|---|---| \n| Fake | 9% | 2024 | |"
    )
    assert checked.rows_removed == 1 and "<table" not in checked.html


@pytest.mark.parametrize(
    ("text", "uncited"),
    [
        (
            "Fake lead sentence of the answer [TST-0001-F00001]. The Excel under this answer shows that 99% of "
            "fake children are out of school in every governorate.",
            1,
        ),
        (
            "Fake lead sentence of the answer [TST-0001-F00001]. The CSV survey of 2023 shows 40% of children "
            "work.",
            1,
        ),
        (
            "Fake lead sentence of the answer [TST-0001-F00001]. Girls in the South do not report higher dropout "
            "than boys and the gap is small.",
            1,
        ),
        (
            "Fake lead sentence of the answer [TST-0001-F00001]. The Excel of these findings, under this answer, "
            "lists each finding read with its figures.",
            0,
        ),
        (
            "Fake lead sentence of the answer [TST-0001-F00001]. The findings read do not report figures by sex "
            "for the fake villages.",
            0,
        ),
    ],
)
def test_a_sentence_that_names_a_download_or_says_do_not_is_still_a_claim(ai, text, uncited):  # noqa: F811
    factory.load(factory.workbook(documents=1, findings=2))
    assert _finish(text).uncited == uncited
