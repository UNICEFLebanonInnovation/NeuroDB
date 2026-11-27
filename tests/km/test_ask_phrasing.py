"""Questions to Ask the KM Database as people phrase them freely (the question bench's root causes): a breakdown
never counted as a word of the subject, the words of an event and "trend" that only rank, request, courtesy and
format words, place names kept by the privacy rule, places in Arabic and French and with accents, towns in their
district and governorate, the phrases for all of Lebanon, the regions by the owner's rule, more time phrases,
"dropping out of school" and a misspelt word found in the version's own words when the AI gives none. Invented
texts only; places are public geography."""

from __future__ import annotations

import datetime
import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from neurodb.km import ask, places, privacy
from neurodb.km.privacy import NAME_WITHHELD as W
from tests.km.test_ask import _ids, _picked, ai, terms, today  # noqa: F401 (fixtures)
from tests.km.test_ask_search import ids_of, load, read

pytestmark = pytest.mark.django_db

ON = datetime.date(2026, 11, 26)  # the owner's examples: "last year" is 2025


def words_of(question: str) -> list[list[str]]:
    """The groups of words a first question searches (its places, years and codes read first)."""
    text = ask._searchable(question)
    return ask.understand(text, question).groups


def keys(question: str) -> list[str]:
    return places.read(question).keys


# ------------------------------------------------------------------------------- a breakdown's quorum
FEEDING = (
    "In Akkar, school feeding reached 40% of pupils.",
    "In Beirut, schools reopened after the strike.",
    "In Tripoli, feeding programmes for infants grew.",
    "School feeding reached 30% of pupils nationally.",
)


def test_a_breakdown_never_counts_as_a_word_of_the_subject(ai):  # noqa: F811
    load(("2024", FEEDING))
    for question in ("school feeding by governorate", "school feeding by district"):
        picked = _picked(question)
        assert picked.breakdown and read(picked) == {FEEDING[0]}, question


def test_the_verb_of_a_measure_only_ranks_in_a_breakdown_too(ai):  # noqa: F811
    """ "% of children covered by MHPSS by governorate": a finding on MHPSS that names a place is read whether
    it says "covered" or "received" (those that say it first); the breakdown stays required."""
    both = [f"In {town}, MHPSS covered {n}% of children." for town, n in (("Akkar", 20), ("Tyre", 25))]
    one = [
        f"In {town}, {n}% of children received MHPSS."
        for town, n in (("Zahle", 30), ("Halba", 31), ("Saida", 32), ("Jbeil", 33), ("Aley", 34))
    ]
    load(("2024", [*both, *one, "Nationally, MHPSS covered 15% of children."]))
    picked = _picked("please, % of children covered by MHPSS by governorate and district thx")
    assert picked.breakdown and set(_ids(picked)[:2]) == set(ids_of(*both))
    assert picked.matched == len(both) + len(one) and picked.groups[0][0] == "mhpss"
    assert "Nationally, MHPSS covered 15% of children." not in read(picked)


def test_a_breakdown_reads_the_subjects_best_matches_before_the_governorates_named(ai):  # noqa: F811
    texts = (
        "In Akkar, Beirut, Tripoli and Tyre, school feeding was discussed.",
        "In Akkar, school feeding coverage reached 40% of pupils.",
    )
    load(("2024", texts))
    picked = _picked("school feeding coverage by governorate")
    assert [f.key_finding for f in picked.findings] == [texts[1], texts[0]]


# --------------------------------------------------------------------------- events and trends only rank
@pytest.mark.parametrize(
    ("asked", "left", "ranks"),
    [
        ("bullying in Rachaya after the 2020 port explosion", "bullying in Rachaya after", ["2020", "port", "explosion"]),
        ("anaemia in Tyre during the 2024 escalation", "anaemia in Tyre during", ["2024", "escalation"]),
        ("child marriage in El Metn after the 2020 port explosion", "child marriage in El Metn after", ["2020", "port", "explosion"]),
        ("impact of 2020 port explosion on children", "impact of on children", ["2020", "port", "explosion"]),
    ],
)  # fmt: skip
def test_the_words_of_an_event_only_rank(asked, left, ranks):
    frame = ask.time_frame(asked, ON)
    assert frame.years == [] and " ".join(frame.text.split()) == left and frame.ranks == ranks


def test_an_events_words_never_outvote_the_subject(ai, today):  # noqa: F811
    texts = (
        "In Rachaya, bullying affected 20% of pupils.",
        "In Rachaya, the port explosion damaged two schools.",
        "In Rachaya, bullying rose after the port explosion.",
    )
    load(("2023", texts))
    picked = _picked("bullying in Rachaya after the 2020 port explosion")
    assert [f.key_finding for f in picked.findings] == [texts[2], texts[0]]  # the event's finding first
    anaemia = ("In Tyre, anaemia affected 30% of children.", "In Tyre, the escalation displaced families.")
    load(("2024", anaemia))
    picked = _picked("anaemia in Tyre during the 2024 escalation")
    assert read(picked) == {anaemia[0]} and not picked.loose


def test_trend_only_ranks(ai):  # noqa: F811
    texts = (
        "In the Bekaa, child marriage affected 4% of girls.",
        "In the Bekaa, the trend in child marriage rose.",
        "In the Bekaa, the trend in rents rose.",
    )
    load(("2024", texts))
    for question in (
        "what is the trend in child marriage in the Bekaa",
        "child marriage trends in the Bekaa",
    ):
        picked = _picked(question)
        assert picked.groups == [["child marriage", "early marriage"]], question
        assert [f.key_finding for f in picked.findings] == [texts[1], texts[0]], question


# ------------------------------------------------------------------- request, courtesy and format words
@pytest.mark.parametrize(
    ("asked", "groups"),
    [
        ("Could you provide the available evidence on school feeding in Beirut?", [["school"], ["feeding"]]),
        ("hi can u get me numbers on stunting in Akkar asap", [["stunting"]]),
        (
            "Please prepare a short summary of what is currently known about bullying among affected children "
            "in Nabatieh",
            [["bullying"]],
        ),
        ("hey, send me info on stunting in Akkar urgently", [["stunting"]]),
        ("hello, please share information on stunting", [["stunting"]]),
        ("Dear all, I would be grateful for a brief note on stunting", [["stunting"]]),
        ("present a comparison of stunting among concerned kids in Akkar", [["stunting"]]),
        ("what does teh evidence say abt stunting? wat do we know", [["stunting"]]),
        ("indicate the studies that address stunting", [["stunting"]]),
        ("address stunting in Akkar", [["stunting"]]),
        ("summarising the evidence on stunting in a short table", [["stunting"]]),
        ("summarizing what is available on stunting, a brief overview please", [["stunting"]]),
        ("stunting in Akkar with a lence on girls", [["stunting"], ["girls"]]),
        # a noun: the subject's
        ("birth registration address in Akkar", [["birth"], ["registration"], ["address"]]),
    ],
)
def test_request_courtesy_and_format_words_are_never_searched(asked, groups):
    assert words_of(asked) == groups


# ------------------------------------------------------------------- place names kept by the privacy rule
TOWNS_NAMED = ["Saida", "Zghorta", "Kura", "Kisrawan", "Western Bekaa", "Baalbak", "Hirmil", "Tyr", "Sayda"]


@pytest.mark.parametrize("place", [*TOWNS_NAMED, "Bab al-Tabbaneh", "Jabal Mohsen", "Qana", "Karantina"])
def test_a_place_before_a_year_is_never_withheld_as_a_name(place):
    for asked in (
        f"child labour in {place} 2023",
        f"numbers for {place}, 2024",
        f"child labour across {place} 2023",
    ):
        assert privacy.clean_question(asked) == asked, asked


def test_places_listed_before_a_year_are_never_withheld_as_names():
    for asked in (
        "out of school refugees in Majdal Anjar and Saadnayel 2023",
        "child labour in Tyre, Qana and Houla 2024",
        "numbers for Saida and Sayda, 2024",
        "numbers on bullying in schools in Chtoura 2024",  # the place word nearest the name
        "trends in out-of-school children for Aakar 2019-2022",
    ):
        assert privacy.clean_question(asked) == asked, asked
    # never a name between them: "in Doe and Naameh 2016" is a citation
    assert "Naameh" not in privacy.clean_question("as shown in Fakedoe and Naameh 2016")


@pytest.mark.parametrize("place", ["Western Bekaa", "Bab al-Tabbaneh", "Jabal Mohsen", "Tyre", "Zahle"])
def test_a_place_of_several_words_or_a_main_towns_stays_in_a_citations_parenthesis(place):
    asked = f"child labour ({place} 2023)"
    assert privacy.clean_question(asked) == asked


@pytest.mark.parametrize("place", ["Saida", "Sayda", "Qana", "Houla", "Naameh"])
def test_a_towns_name_of_one_word_written_as_a_citation_is_a_name(place):
    """A citation's shape ("(Naameh 2021)", "Naameh (2021) found"): a town's one-word spelling is also a
    family name, kept as a place only where it is written as one ("in Naameh 2021")."""
    for asked in (f"child labour ({place} 2023)", f"{place} (2021) found high rates"):
        assert place not in privacy.clean_question(asked), asked


def _written(spelling: str) -> str:
    """A spelling as a person may type it, each word capitalised ("Bab Al-Tabbaneh")."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"(\s+|-)", spelling))


def test_every_place_of_a_typed_question_survives_in_the_question_kept_and_sent():
    every = [p for p in (*places.GOVERNORATES, *places.DISTRICTS) if not p.camp]
    for place in every:
        for spelling in place.spellings:
            name = _written(spelling)
            for asked in (f"child labour in {name} 2023", f"numbers for {name}, 2024"):
                cleaned = privacy.clean_question(asked)
                assert W not in cleaned and keys(cleaned) == keys(asked), asked


def test_the_words_of_a_places_name_are_still_names_alone():
    for asked in (
        "child labour in Mohsen 2019",
        "child labour for Elias 2020",
        "child labour by Hammoud 2018",
    ):
        assert W in privacy.clean_question(asked), asked


def test_a_place_before_a_year_stays_the_place_of_the_next_turn(ai):  # noqa: F811
    load(("2023", ["In Saida, 12% of children worked.", "In Akkar, 9% of children worked."]))
    kept = privacy.clean_question("child labour in Saida 2023")  # the question a follow-up reads again
    assert kept == "child labour in Saida 2023"
    picked = _picked("and for girls?", earlier=[{"question": kept, "answer": "Fake.", "cited": []}])
    assert picked.places == "Saida (the South)" and "places" in picked.kept


# --------------------------------------------------------------------------- places in other spellings
@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("child labour in عكار", ["akkar"]),
        ("stunting in بعلبك الهرمل", ["baalbek-hermel"]),
        ("التسرب المدرسي في البقاع", ["bekaa"]),
        ("عمالة الأطفال بالبقاع", ["bekaa"]),
        ("الزواج المبكر في جبل لبنان", ["mount-lebanon"]),
        ("الأطفال في النبطية", ["nabatieh"]),
        ("الأطفال في النبطيه", ["nabatieh"]),
        ("الأطفال في كسروان جبيل", ["keserwan-jbeil"]),
        ("التعليم في محافظة الشمال", ["north"]),
        ("التعليم في الشمال", ["north", "akkar"]),
        ("التعليم في الجنوب", ["south", "nabatieh"]),
        ("الأطفال في بيروت وطرابلس", ["beirut", "tripoli"]),
        ("الأطفال في بنت جبيل", ["bint-jbeil"]),
        ("الأطفال في صيدا وصور", ["saida", "tyre"]),
        ("learning loss in la Békaa since 2020", ["bekaa"]),
        ("enfants non scolarisés au Mont-Liban", ["mount-lebanon"]),
        ("santé des enfants à Zahlé", ["zahle"]),
        ("éducation dans la Bekaa et le Akkar", ["bekaa", "akkar"]),
        ("travail des enfants de la Békaa", ["bekaa"]),
        ("travail des enfants du Hermel", ["hermel"]),
        ("travail des enfants à l'Akkar", ["akkar"]),
        ("mariage précoce à Nabatiyé", ["nabatieh"]),
    ],
)
def test_places_in_arabic_and_french_and_with_accents(asked, expected):
    reading = places.read(asked)
    assert reading.keys == expected
    left = set(reading.text.casefold().split())
    assert not left & {"la", "le", "l'akkar", "du", "de", "békaa", "bekaa", "akkar", "عكار", "البقاع"}, left


# the towns written in code, each in its district and governorate (public geography: the official cazas)
TOWNS = {
    "Karantina": ("beirut", "beirut"),
    "Tariq el Jdideh": ("beirut", "beirut"),
    "Achrafieh": ("beirut", "beirut"),
    "Ouzai": ("baabda", "mount-lebanon"),
    "Choueifat": ("aley", "mount-lebanon"),
    "Barja": ("chouf", "mount-lebanon"),
    "Dekwaneh": ("metn", "mount-lebanon"),
    "Zouk Mosbeh": ("keserwan", "keserwan-jbeil"),
    "Amchit": ("jbeil", "keserwan-jbeil"),
    "El Mina": ("tripoli", "north"),
    "Bab al-Tabbaneh": ("tripoli", "north"),
    "Jabal Mohsen": ("tripoli", "north"),
    "Ehden": ("zgharta", "north"),
    "Amioun": ("koura", "north"),
    "Chekka": ("batroun", "north"),
    "Bebnine": ("akkar", "akkar"),
    "Minyara": ("akkar", "akkar"),
    "Qlayaat": ("akkar", "akkar"),
    "Labweh": ("baalbek", "baalbek-hermel"),
    "Al-Qaa": ("baalbek", "baalbek-hermel"),
    "Douris": ("baalbek", "baalbek-hermel"),
    "Majdal Anjar": ("zahle", "bekaa"),
    "Saadnayel": ("zahle", "bekaa"),
    "Qab Elias": ("zahle", "bekaa"),
    "Joub Jannine": ("west-bekaa", "bekaa"),
    "Ghazieh": ("saida", "south"),
    "Qana": ("tyre", "south"),
    "Kfar Kila": ("marjayoun", "nabatieh"),
    "Aitaroun": ("bint-jbeil", "nabatieh"),
    "Rmeish": ("bint-jbeil", "nabatieh"),
    "Shebaa": ("hasbaya", "nabatieh"),
}


def test_towns_are_read_in_their_district_and_governorate():
    for town, (district, governorate) in TOWNS.items():
        found = keys(f"children in {town} in 2024")
        assert len(found) == 1, town
        place = places.BY_KEY[found[0]]
        assert (place.district, place.governorates[0]) == (district, governorate), town
    # each district of the code is in its governorate, and each town's district is one of them
    districts = {p.key for p in places.DISTRICTS if not p.district} | {g.key for g in places.GOVERNORATES}
    assert {p.district for p in places.DISTRICTS if p.district} <= districts


def test_a_town_is_its_place_and_a_district_finds_its_towns(ai):  # noqa: F811
    texts = (
        "In Karantina, school feeding reached 30% of pupils.",
        "In Akkar, school feeding reached 40% of pupils.",
        "In Bar Elias, child labour affected 12% of children.",
        "In Zahle, child labour affected 9% of children.",
        "In Tyre, child labour affected 5% of children.",
    )
    load(("2024", texts))
    picked = _picked("school feeding for Karantina 2024")
    assert picked.groups == [["school"], ["feeding"]] and picked.places == "Karantina (Beirut)"
    assert _ids(picked)[0] == ids_of(texts[0])[0] and (picked.relaxed, picked.in_place) == ("few", 1)
    picked = _picked("child labour in Zahle district")
    assert set(_ids(picked)[:2]) == set(ids_of(texts[2], texts[3])) and picked.in_place == 2
    assert read(picked) == set(texts[2:])


# ------------------------------------------------------------------------------------ all of Lebanon
@pytest.mark.parametrize(
    "asked",
    [
        "early childhood development lebanon-wide",
        "early childhood development Lebanon wide",
        "digital learning across the country",
        "digital learning at country level",
        "digital learning at the country level",
        "digital learning throughout the country",
        "digital learning nation wide",
        "apprentissage numérique dans tout le Liban",
        "apprentissage numérique au niveau national",
        "التعلم الرقمي في كل لبنان",
        "التعلم الرقمي على المستوى الوطني",
    ],
)
def test_all_of_lebanon_in_more_words(asked):
    reading = places.read(asked)
    assert reading.national and not reading.keys, asked
    left = set(reading.text.casefold().split())
    assert not left & {
        "wide",
        "lebanon-wide",
        "across",
        "country",
        "level",
        "throughout",
        "tout",
        "niveau",
        "national",
    }


# ------------------------------------------------------------------------ the regions by the owner's rule
@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("liban-sud", ["south", "nabatieh"]),
        ("Liban Sud", ["south", "nabatieh"]),
        ("le Liban-Sud", ["south", "nabatieh"]),
        ("the north", ["north", "akkar"]),
        ("the North", ["north", "akkar"]),
        ("North Lebanon", ["north", "akkar"]),
        ("northern Lebanon", ["north", "akkar"]),
        ("Liban-Nord", ["north", "akkar"]),
        ("Shamal", ["north", "akkar"]),
        ("the northern governorates", ["north", "akkar"]),
        ("the North governorate", ["north"]),
        ("North governorate", ["north"]),
        ("the northern governorate", ["north"]),
        ("the South governorate", ["south"]),
        ("the southern governorate", ["south"]),
        # a governorate's word with a region's spelling: that governorate ("North Lebanon governorate" is the
        # North governorate's official name)
        ("North Lebanon governorate", ["north"]),
        ("the North Lebanon Governorate", ["north"]),
        ("the governorate of North Lebanon", ["north"]),
        ("Liban-Nord governorate", ["north"]),
        ("the Liban-Nord governorate", ["north"]),
        ("the North and South governorates", ["north", "south"]),
        ("South Lebanon governorate", ["south"]),
        ("the South Lebanon governorate", ["south"]),
        ("the governorate of South Lebanon", ["south"]),
        ("the Liban-Sud governorate", ["south"]),
        ("the South Lebanon governorate and Nabatieh", ["south", "nabatieh"]),
    ],
)
def test_the_regions_by_the_owners_rule(asked, expected):
    assert keys(f"child marriage in {asked}") == expected
    left = set(places.read(f"child marriage in {asked}").text.casefold().split())
    assert not left & {"lebanon", "north", "south", "governorate", "governorates"}, left


# ------------------------------------------------- an Arabic word that is also a place's name, not a place
@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("ما هي صور العنف ضد الأطفال في لبنان؟", []),  # "forms of violence"
        ("هل هناك صور نمطية عن تعليم الفتيات في لبنان؟", []),  # "stereotypes"
        ("هل هناك نسبه عاليه من الأطفال خارج المدرسة في لبنان؟", []),  # "a high rate"
        ("ما هو رأس المال بشري للأطفال؟", []),  # "human capital"
        ("عمالة الأطفال في صور", ["tyre"]),
        ("عمالة الأطفال بصور", ["tyre"]),
        ("الأطفال في صيدا وصور", ["saida", "tyre"]),
        ("التسرب المدرسي في قضاء عاليه", ["aley"]),
        ("الأطفال في بشري", ["bcharre"]),
        # in a question written in English or French
        ("list the key findings on nutrition in عاليه", ["aley"]),
        ("what is known about OOSC in صور?", ["tyre"]),
        ("travail des enfants à صور", ["tyre"]),
    ],
)
def test_an_arabic_word_that_is_also_a_places_name_is_a_place_only_where_written_as_one(asked, expected):
    assert keys(asked) == expected


def test_the_north_is_said_as_the_north_and_akkar(ai):  # noqa: F811
    load(
        (
            "2024",
            [
                "In Halba, child labour rose.",
                "In Tripoli, child labour rose.",
                "In Zahle, child labour rose.",
            ],
        )
    )
    picked = _picked("child labour in the north")
    assert picked.places == "the north (North and Akkar governorates)"
    assert read(picked) >= {"In Halba, child labour rose.", "In Tripoli, child labour rose."}


# --------------------------------------------------------------------------------------- time phrases
D = list(range(2026, 2015, -1))  # the last decade: 2016-2026


@pytest.mark.parametrize(
    ("asked", "years", "left"),
    [
        ("anaemia last 2 yrs", [2026, 2025, 2024], "anaemia"),
        ("anaemia past 3 yrs", [2026, 2025, 2024, 2023], "anaemia"),
        ("anaemia in the past yr", [2026, 2025], "anaemia"),
        ("anaemia over the last decade", D, "anaemia"),
        ("anaemia in the past decade", D, "anaemia"),
        ("anaemia in the past two decades", list(range(2026, 2005, -1)), "anaemia"),
        ("stunting 2021 onwards", list(range(2026, 2020, -1)), "stunting"),
        ("stunting from 2021 onwards", list(range(2026, 2020, -1)), "stunting"),
        ("stunting from 2021 on", list(range(2026, 2020, -1)), "stunting"),
        ("stunting 2021 and after", list(range(2026, 2020, -1)), "stunting"),
        ("stunting 2021 and later", list(range(2026, 2020, -1)), "stunting"),
        ("stunting in 2021 onward.", list(range(2026, 2020, -1)), "stunting ."),
        ("the 2021 report on stunting", [2021], "the report on stunting"),
        ("child labour since teh 2019 crisis", list(range(2026, 2018, -1)), "child labour"),
        # "last year": the previous calendar year; "the past year", "in the last year": this year and the one before
        ("anaemia last year", [2025], "anaemia"),
        ("SUMMARISE THE EVIDENCE ON BREASTFEEDING FROM LAST YEAR", [2025], "SUMMARISE THE EVIDENCE ON BREASTFEEDING"),
        ("what about last year?", [2025], "what about ?"),
        ("last year's figures on anaemia", [2025], "figures on anaemia"),
        ("anaemia in the previous year", [2026, 2025], "anaemia"),
        ("anaemia in the past year", [2026, 2025], "anaemia"),
        ("anaemia in the last year", [2026, 2025], "anaemia"),
        ("anaemia over the last 12 months", [2026, 2025], "anaemia"),
        # compared with last year: that year and this one
        ("has child marriage increased compared to last year?", [2026, 2025], "has child marriage increased ?"),
        ("is child labour higher than last year?", [2026, 2025], "is child labour higher ?"),
        ("child marriage this year compared with last year", [2026, 2025], "child marriage"),
    ],
)  # fmt: skip
def test_more_time_phrases(asked, years, left):
    frame = ask.time_frame(ask._searchable(asked), ON)
    assert frame.years == years and " ".join(frame.text.split()) == left


@pytest.mark.parametrize(
    "asked",
    [
        "is there anything newer?",
        "anything more recent on anaemia?",
        "is there an update on anaemia?",
        "any updates?",
    ],
)
def test_newer_and_an_update_are_the_latest(asked):
    frame = ask.time_frame(asked, ON)
    assert frame.latest and frame.years == [2026, 2025, 2024], asked


@pytest.mark.parametrize(
    "asked",
    ["uptake of newer vaccines among infants", "how do more recent arrivals cope with rent?"],
)
def test_newer_of_the_subject_is_no_time_frame(asked):
    """ "newer vaccines", "more recent arrivals": words of the subject, never "anything newer"."""
    assert ask.time_frame(asked, ON).years == [], asked
    assert ask.time_frame("is there newer data on stunting?", ON).latest


@pytest.mark.parametrize(
    ("asked", "groups"),
    [
        ("access to information for adolescents in Akkar", [["access"], ["information"], ["adolescents"]]),
        ("information sessions for parents", [["information"], ["sessions"], ["parents"]]),
        ("what information do we have on stunting?", [["stunting"]]),
        ("info on breastfeeding in Zahle", [["breastfeeding"]]),
        ("stunting in Akkar with a lense on girls", [["stunting"], ["girls"]]),
        ("child labour with a gender lenz", [["labour", "labor"], ["gender", "girls", "boys"]]),
    ],
)
def test_information_asked_for_and_a_misspelt_lens_are_never_searched(asked, groups):
    assert words_of(asked) == groups


def test_an_update_with_a_year_of_its_own_is_that_year():
    for asked in (
        "is there an update this year?",
        "is there anything newer this year?",
        "any update for 2026?",
    ):
        frame = ask.time_frame(asked, ON)
        assert frame.years == [2026] and not frame.latest, asked


# -------------------------------------------------------------------------------- dropping out of school
@pytest.mark.parametrize(
    "asked",
    [
        "children dropping out of school in Akkar",
        "children who dropped out of school in Akkar",
        "why do children drop out of school in Akkar",
        "a child drops out of school in Akkar",
        "dropouts from school in Akkar",
    ],
)
def test_dropping_out_of_school_is_a_dropout(asked):
    found = words_of(asked)
    assert len(found) == 1 and "dropout" in found[0], found


def test_dropping_out_of_school_finds_dropouts_written_without_school(ai):  # noqa: F811
    texts = (
        "In Akkar, 12% of adolescents dropped out.",
        "In Akkar, out-of-school children lacked transport.",
        "In Akkar, dropping water levels hit farms.",
    )
    load(("2024", texts))
    assert read(_picked("children dropping out of school in Akkar")) == {texts[0]}


# ------------------------------------------------------------------------------- a word misspelt
TYPOS = (
    "Child poverty rose to 40% in Akkar.",
    "In Akkar, vaccination coverage fell to 60%.",
    "In Akkar, teachers left public schools.",
    "Fake rainfall fell in Akkar.",
)


def test_a_misspelt_word_is_read_as_the_versions_closest_word_when_the_ai_gives_none(ai, today):  # noqa: F811
    load(("2024", TYPOS))
    assert read(_picked("child povrety in Akkar")) == {TYPOS[0]}
    assert read(_picked("vacination since 2022")) == {TYPOS[1]}
    assert read(_picked("teachrs in Akkar")) == {TYPOS[2]}
    assert _picked("child povrety in Akkar").corrected == {"povrety": "poverty"}
    # the search-words call gives nothing: the same
    ai(terms_=[terms()])
    assert read(_picked("child povrety in Akkar", may_call=True)) == {TYPOS[0]}
    # a word with no close word stays unmatched: the place's findings are read
    assert read(_picked("xqzvbnw in Akkar")) == set(TYPOS)


def test_the_versions_words_are_read_once_a_version(ai, today):  # noqa: F811
    load(("2024", TYPOS))
    _picked("child povrety in Akkar")
    with CaptureQueriesContext(connection) as queries:
        assert read(_picked("vacination in Akkar")) == {TYPOS[1]}
    assert not any("ts_stat" in q["sql"] for q in queries.captured_queries)
    load(("2024", [*TYPOS, "In Akkar, nutrition improved."]))  # a new version: its own words
    assert read(_picked("nutriton in Akkar")) == {"In Akkar, nutrition improved."}


def test_a_place_or_a_request_word_is_never_corrected(ai):  # noqa: F811
    load(
        ("2024", ["In Akkar, the table of fees rose.", "In Akkar, stunting fell.", "In Tyre, stunting fell."])
    )
    picked = _picked("stunting in Akkar in a tabel")  # "table": a request's word
    assert picked.corrected == {} and _ids(picked)[0] == ids_of("In Akkar, stunting fell.")[0]
    picked = _picked("stunting in Akkkar")  # a place's spelling
    assert picked.corrected == {} and read(picked) == {"In Akkar, stunting fell.", "In Tyre, stunting fell."}


def test_a_word_read_for_a_misspelt_one_is_never_kept_on_the_row(ai):  # noqa: F811
    texts = ["In Akkar, child labour rose.", "In Akkar, poverty rose to 40%.", "In Tyre, poverty fell."]
    load(("2024", [*texts, *["Fake rainfall fell."] * 3]))
    earlier = [{"question": "child labour in Akkar", "answer": "Fake.", "cited": []}]
    picked = _picked("what about povrety?", earlier=earlier)
    assert "In Akkar, poverty rose to 40%." in read(picked)
    assert picked.new_subject == [["povrety"]]  # as typed: the version's word is never kept


def test_an_event_alone_is_the_subject_and_its_place_the_place(ai):  # noqa: F811
    texts = (
        "After the explosion, 30% of adolescents in Beirut reported distress.",
        "In Beirut, the port reopened.",
        "In Akkar, the war closed schools.",
    )
    load(("2023", texts))
    for question in ("the 2020 explosion", "children after the 2020 explosion"):
        assert read(_picked(question)) == {texts[0]}, question
    assert read(_picked("children after the 2024 war")) == {texts[2]}
    picked = _picked("the situation of children after the 2020 Beirut port explosion")
    assert picked.places == "Beirut" and picked.groups == [["port"], ["explosion"]]


def test_a_correct_word_no_finding_holds_is_never_read_as_another(ai):  # noqa: F811
    """ "studying" is no misspelling of "stunting" (two letters changed), nor "syria" of "syrian" (a
    country's name): a word read so would put findings on another subject first."""
    texts = [
        "In the North, girls worked instead of going to school.",
        "In the North, girls worked on farms during the harvest.",
        "In the North, stunting affected 20% of girls.",
        "Among Syrian refugees, birth registration reached 40%.",
        "Among Palestinian refugees, birth registration reached 60%.",
    ]
    load(("2024", texts))
    for typed in ("studying", "syria"):
        assert ask.closest(typed) == "", typed
    ai(terms_=[terms(), terms()])
    picked = _picked("do girls work instead of studying in the North governorate?", may_call=True)
    assert picked.corrected == {} and texts[2] not in read(picked)
    picked = _picked("status of birth registration among Palestinian refugees from Syria", may_call=True)
    assert picked.corrected == {} and _ids(picked)[0] == ids_of(texts[4])[0]


def test_the_findings_of_the_words_as_typed_stay_before_a_misspelt_words_others(ai):  # noqa: F811
    """A misspelt word read as the version's closest one: the findings that hold every word as meant first,
    then those the words as typed found, and only then those of the words as meant that hold fewer."""
    loss = [f"In {town}, learning loss in schools reached {n}%." for town, n in (
        ("Akkar", 30), ("Tyre", 31), ("Zahle", 32), ("Halba", 33), ("Saida", 34), ("Jbeil", 35),
    )]  # fmt: skip
    extra = [f"Online learning in schools grew by {n}%." for n in (40, 41, 42, 43, 44, 45, 46, 47)]
    other = [f"Fake rainfall rose by {n} mm in Zahle." for n in range(1, 21)]
    load(("2025", [*loss, *extra, *other]))
    ai(terms_=[terms()])
    picked = _picked("lerning loss in schools across lebanon pls??", may_call=True)
    assert picked.corrected == {"lerning": "learning"}
    assert set(_ids(picked)[: len(loss)]) == set(ids_of(*loss))


def test_a_misspelling_is_read_only_when_it_is_one(ai):  # noqa: F811
    load(
        ("2024", ["In Akkar, waiting lists grew.", "Plain fees rose nationally.", "In Akkar, stunting fell."])
    )
    ai(terms_=[terms(), terms(), terms()])
    # a letter changed in a word under 8 letters, a word under 5 letters, "national": never read as another
    for question, typed in (("wasting in Akkar", "wasting"), ("the plan in Akkar", "plan")):
        picked = _picked(question, may_call=True)
        assert picked.corrected == {} and ask.closest(typed) == "", question
    picked = _picked("and the national strategy?", may_call=True)
    assert picked.corrected == {} and read(picked) == set()
