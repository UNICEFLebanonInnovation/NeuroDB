"""Names of people in the KM Database's texts (SPEC_km §4.4, the parts its downloads use): citations and
interview speakers withheld without any list of names, a document's own authors withheld in its texts, the
KM Database's own authors and names after a title withheld anywhere, links and numbers kept; and the final
check. Fake strings only."""

from __future__ import annotations

import pytest

from neurodb.km import privacy
from neurodb.km.privacy import NAME_WITHHELD as W
from neurodb.watch import people as watch_people

PEOPLE = privacy.People(names=frozenset({"jane doe", "fakefirst fakelast"}), surnames=frozenset({"doe"}))


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # citations (no list of names needed)
        ("(Fakeorg Institute, 2020; Doe, 2019)", f"(Fakeorg Institute, 2020; {W}, 2019)"),
        ("as Roe (2018) found", f"as {W} (2018) found"),
        ("Fake limitations (Poe, 2017, p. 4).", f"Fake limitations ({W}, 2017, p. 4)."),
        ("(Poe and Moe, 2017: 12)", f"({W} and {W}, 2017: 12)"),
        ("(Doe et al., 2019)", f"({W} et al., 2019)"),
        ("Jane Doe et al. (2021) asked", f"{W} et al. (2021) asked"),
        ("as noted by Poe, 2019, the", f"as noted by {W}, 2019, the"),
        ("Poe and Moe (2011) found", f"{W} and {W} (2011) found"),
        # spans shaped like a citation that are not names
        ("(February 2023)", "(February 2023)"),
        ("(July–November 2022)", "(July–November 2022)"),
        ("(Jan-Feb 2021)", "(Jan-Feb 2021)"),
        ("(Census 2019)", "(Census 2019)"),
        ("Lebanon (2020)", "Lebanon (2020)"),
        ("(FaSyR 2022)", "(FaSyR 2022)"),
        ("(Fakeorg Institute, 2020)", "(Fakeorg Institute, 2020)"),
        ("the Fake Response Plan (2020)", "the Fake Response Plan (2020)"),
        ("in Chile (2011) and Beirut, 2020", "in Chile (2011) and Beirut, 2020"),
        ("fake NGOs, 2019-2020", "fake NGOs, 2019-2020"),
        # interview speakers
        ("“Fake words.” – Fakename, 34", f"“Fake words.” – {W}, 34"),
        ("(Fakename, 15)", f"({W}, 15)"),
        ("(Mother, 34)", "(Mother, 34)"),
        ("(Fakesector, 61.5% of jobs)", "(Fakesector, 61.5% of jobs)"),
        # names after a title, and the KM Database's own authors
        ("Dr Fakename Fakelast said", f"{W} said"),
        ("as Jane Doe wrote", f"as {W} wrote"),
        ("FAKEFIRST FAKELAST wrote", f"{W} wrote"),
        # kept: e-mails go, links, numbers and ordinary words stay
        ("write to jane.doe@fake.example.org", f"write to {privacy.EMAIL_WITHHELD}"),
        ("see fake.example.org/x and +961 3 123 456", "see fake.example.org/x and +961 3 123 456"),
        (
            "n=3 450 households, 1 250 000 people, 4 500 000",
            "n=3 450 households, 1 250 000 people, 4 500 000",
        ),
        ("<5 years and not reported values", "<5 years and not reported values"),
    ],
)
def test_for_download(text, shown):
    assert privacy.for_download(text, PEOPLE) == shown


def test_a_documents_own_authors_are_withheld_in_its_texts_only():
    own = frozenset({"doe"})
    assert (
        privacy.for_download("J. Doe interviewed the fake pupils", PEOPLE, own)
        == f"{W} interviewed the fake pupils"
    )
    assert privacy.for_download("Doe's survey found", PEOPLE, own) == f"{W}'s survey found"
    assert privacy.for_download("Doe’s survey found", PEOPLE, own) == f"{W}’s survey found"
    assert (
        privacy.for_download("J. Doe interviewed", PEOPLE) == "J. Doe interviewed"
    )  # another document's text
    assert privacy.for_download("doe and does", PEOPLE, own) == "doe and does"  # not capitalised: a word


def test_for_ai_also_withholds_links_and_phones():
    text = "see fake.example.org/x and +961 3 123 456 or 00961 3 123 456, n=3 450"
    shown = privacy.for_ai(text, PEOPLE)
    assert shown == (
        f"see {privacy.LINK_WITHHELD} and {privacy.PHONE_WITHHELD} or {privacy.PHONE_WITHHELD}, n=3 450"
    )


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("Fake words about fake pupils (February 2023).", []),
        ("write to jane.doe@fake.example.org", ["email", "link"]),  # its domain reads as a link too
        ("see https://fake.example.org/x", ["link"]),
        ("call +961 3 123 456", ["phone"]),
        ("as Jane Doe wrote", ["name"]),
        ("Dr Fakename said", ["name"]),
        ("as Roe (2018) found", ["name"]),
        ("“Fake.” – Fakename, 34", ["name"]),
        (f"as {W} (2018) found and {W} et al.", []),
    ],
)
def test_leftover_finds_what_is_left(text, kinds):
    assert privacy.leftover(text, PEOPLE) == kinds


def test_what_is_withheld_leaves_nothing_for_the_final_check():
    texts = [
        "Jane Doe et al. (2021) asked fake pupils; “…” – Fakename, 34",
        "Fake analysis, as Roe (2018) found (Fakeorg Institute, 2020; Doe, 2019).",
        "Dr Fakename Fakelast and (Poe, 2017, p. 4) and Moe, 2016",
    ]
    for text in texts:
        assert privacy.leftover(privacy.for_ai(text, PEOPLE), PEOPLE) == []


def test_names_are_never_looked_up_in_neurodbs_own_list(monkeypatch):

    def refused(*args, **kwargs):
        raise AssertionError("NeuroDB's list of the people it knows was read")

    monkeypatch.setattr(watch_people, "known_names", refused)
    assert (
        privacy.for_download("as Fakefirst Fakelast wrote", privacy.NOBODY) == "as Fakefirst Fakelast wrote"
    )
    assert privacy.for_download("as Fakefirst Fakelast wrote", PEOPLE) == f"as {W} wrote"
    assert privacy.leftover("as Fakefirst Fakelast wrote", privacy.NOBODY) == []


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # citations written "Name YEAR", without a comma
        ("Trials in Lebanon (Fakecited 2010, Fakeother 2020)", f"Trials in Lebanon ({W} 2010, {W} 2020)"),
        ("(Fakesolo 2002 methodology)", f"({W} 2002 methodology)"),
        ("a fake (Fakeone and Faketwo 2020) design", f"a fake ({W} and {W} 2020) design"),
        ("(FAKEORG, Fakeone and Faketwo 2020)", f"(FAKEORG, {W} and {W} 2020)"),
        ("(Fakeone, Faketwo and Fakethree 2020)", f"({W}, {W} and {W} 2020)"),
        ("(Fakeone 2019; Faketwo 2020, p. 4)", f"({W} 2019; {W} 2020, p. 4)"),
        ("as Fakeone and Faketwo 2020 found", f"as {W} and {W} 2020 found"),
        ("as Fakeone et al. 2020 found", f"as {W} et al. 2020 found"),
        (
            "(Fake Programme/Fakeone 2015, 10-13y; Fakeone 2020 11-14y)",
            f"(Fake Programme/{W} 2015, 10-13y; {W} 2020 11-14y)",
        ),
        ("(e.g. Fakeone 2015 and Faketwo 2020 totals)", f"(e.g. {W} 2015 and {W} 2020 totals)"),
        ("(2 fake studies: Fakeone 2016, Faketwo 2019)", f"(2 fake studies: {W} 2016, {W} 2019)"),
        (
            "ages 12-59 months in Fakeone 2016 and for Faketwo 2017",
            f"ages 12-59 months in {W} 2016 and for {W} 2017",
        ),
        (
            "pooling Fakeone 2020 (fake); Fakeone 2020 (Lebanon) relates",
            f"pooling {W} 2020 (fake); {W} 2020 (Lebanon) relates",
        ),
        ("(Fakeone and Faketwo 2020); Faketwo's trial", f"({W} and {W} 2020); {W}'s trial"),
        # kept: dates, places, surveys, acronyms and words of time
        ("Since 2019 the fake rate rose (since 2019)", "Since 2019 the fake rate rose (since 2019)"),
        ("(Since 2019)", "(Since 2019)"),
        ("(Until 2021, Early 2020, Mid 2021)", "(Until 2021, Early 2020, Mid 2021)"),
        ("(VASyR 2021, Survey 2019, Beirut 2020)", "(VASyR 2021, Survey 2019, Beirut 2020)"),
        ("(February 2023, Fakeorg Institute)", "(February 2023, Fakeorg Institute)"),
        (
            "in Jordan and Lebanon 2020 and January and February 2021",
            "in Jordan and Lebanon 2020 and January and February 2021",
        ),
        ("(UNICEF and WFP 2020)", "(UNICEF and WFP 2020)"),
        ("Fakeword 2020 rose", "Fakeword 2020 rose"),  # outside a parenthesis, one word before a year
        ("[Fake Target 2021 vs Achieved 2021]: 143", "[Fake Target 2021 vs Achieved 2021]: 143"),
        ("the Fakeword and Child Development Survey 2023", "the Fakeword and Child Development Survey 2023"),
        (
            "(Fake Integrated Fakeword, Fakeword and Child Development Survey, FAKS, 2022)",
            "(Fake Integrated Fakeword, Fakeword and Child Development Survey, FAKS, 2022)",
        ),
    ],
)
def test_citations_without_a_comma(text, shown):
    assert privacy.for_download(text) == shown
    assert privacy.for_ai(text) == shown
    assert privacy.leftover(shown) == []


# ------------------------------------------------------------------------------ the assistant (SPEC_km §4.4)
@pytest.mark.django_db
def test_a_question_loses_neurodbs_known_names_and_the_km_authors(django_user_model):

    django_user_model.objects.create_user(
        username="fakeuser", first_name="Fakeneuro", last_name="Fakecolleague", password="fake-pass-123456"
    )
    watch_people.forget()
    cleaned = privacy.clean_question(
        "Did Fakeneuro Fakecolleague, Jane Doe or Dr Fakename Fakesur find that (Fakeone, 2019) girls "
        "left school? Write to jane@fake.example.org, see https://fake.example.org/x, call +961 3 123 456.",
        PEOPLE,
    )
    for never in (
        "Fakeneuro",
        "Fakecolleague",
        "Jane",
        "Doe",
        "Fakename",
        "Fakeone",
        "jane@",
        "fake.example",
        "123 456",
    ):
        assert never not in cleaned, (never, cleaned)
    assert "girls left school?" in cleaned and "(" + W + ", 2019)" in cleaned
    # a KM author by surname, and the first name before it
    assert privacy.clean_question("What did Janet Doe find?", PEOPLE) == f"What did {W} find?"
    assert privacy.clean_question("What does the evidence say about girls?", PEOPLE) == (
        "What does the evidence say about girls?"
    )


def test_for_ai_withholds_what_the_final_check_looks_for():
    sent = privacy.for_ai(
        "J. Doe interviewed; see www.fake.example.org and +961 3 123 456; “…” – Fakename, 34; Roe (2018)",
        PEOPLE,
        frozenset({"doe"}),
    )
    assert "Doe" not in sent and "fake.example" not in sent and "123 456" not in sent and "Roe" not in sent
    assert privacy.leftover(sent, PEOPLE) == []
    assert privacy.leftover("as Roe (2018) found", PEOPLE) == ["name"]
    assert privacy.leftover("“…” – Fakename, 34", PEOPLE) == ["name"]


# ------------------------------------------------------------------------- other shapes (stage 3 check)
# the KM Database's own authors of another document: "Doe, J." and "Roe, R." (surnames doe and roe); the
# texts below are a second document's, where only these lists apply (not its own surnames)
KM = privacy.People(
    names=frozenset(watch_people.name_forms("Doe, J.") | watch_people.name_forms("Roe, R.")),
    surnames=frozenset({"doe", "roe"}),
)


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # a reference list: "Surname, I." items, with up to three words before the comma
        (
            "cited with Fakeone, P., Faketwo, M., & Roe Poe, M. C. (2020), Fake Journal",
            f"cited with {W}, {W}, & {W} (2020), Fake Journal",
        ),
        # "Surname I" without a full stop: a KM surname only
        (
            "a fake reader (Doe M) wrote; (Appendix C) and (Type I)",
            f"a fake reader ({W} M) wrote; (Appendix C) and (Type I)",
        ),
        # "Name YEAR:" and "Name YEAR;", and "Name YEAR (place)"
        ("Doe 2020: 20-60y; Roe 2022: fake parents", f"{W} 2020: 20-60y; {W} 2022: fake parents"),
        (
            "Fakeone 2019 (Chile) fits, Faketwo 2021 (Lebanon) too",
            f"{W} 2019 (Chile) fits, {W} 2021 (Lebanon) too",
        ),
        # "(KM surname, Word)", and the other parentheses of that text written so
        (
            "(Doe, Lebanon) to (Roe, Jordan) and (Fakename, Chile)",
            f"({W}, Lebanon) to ({W}, Jordan) and ({W}, Chile)",
        ),
        # "by A, B and C" when one of them is a KM surname
        ("studies by Doe, Roe and Poe found", f"studies by {W}, {W} and {W} found"),
        # a speaker at a sentence's start, by an age in parentheses, by a role after the name
        ("Fakename, 11, was hurt by fake debris.", f"{W}, 11, was hurt by fake debris."),
        ("“Fake words.” Fakename, 20. (p. 5)", f"“Fake words.” {W}, 20. (p. 5)"),
        ("so Fakename (20, male) left fake work", f"so {W} (20, male) left fake work"),
        (
            "Fakename, a boy of 12, left fake school; fake age for Fakename.",
            f"{W}, a boy of 12, left fake school; fake age for {W}.",
        ),
        # a name with an office, before or after it
        ("Minister Jane Fakelast signed it", f"Minister {W} signed it"),
        ("Fake Centre Director Jane Fakelast wrote", f"Fake Centre Director {W} wrote"),
        ("Jane Fakelast, Director of the Fake Centre, said", f"{W}, Director of the Fake Centre, said"),
        # a KM surname right after a name withheld, a first name (and an initial) right before one
        ("as J. Doe Roe wrote", f"as {W} wrote"),
        ("[name withheld] Doe wrote that", f"{W} wrote that"),
        ("Jane [name withheld] found", f"{W} found"),
        ("Jane J. [name withheld] found", f"{W} found"),
    ],
)
def test_names_in_other_shapes_are_withheld_and_found_by_the_final_check(text, shown):
    for clean in (privacy.for_ai, privacy.for_download):
        assert clean(text, KM) == shown, clean.__name__
    assert privacy.leftover(shown, KM) == []
    if W not in text:
        assert privacy.leftover(text, KM) == ["name"]  # the final check finds the shape


@pytest.mark.parametrize(
    "text",
    [
        # sentence starts are not names ("However, 2021" is no citation)
        "However, 2021 data show fake rates rose. However the 2022 figure fell.",
        "Similarly, 2022 figures; Overall, 2021; Meanwhile, 2024; Moreover, 2023 saw; Among Syrians, 2021",
        "Overall, 12,345 fake persons came. Finally, 23,456 children. Additionally, 34,567 children",
        # dates, offices without a name, organisations, places, acronyms and words before a year
        "Fake data collection: 3 March-9 April 2021; Fake fieldwork May-June 2020; fake listing",
        "The Director General of Fake Affairs met; Head Teachers said; the Fake Programme Director",
        "Fake Target 2021: 50; Achieved 2021: 40; Since 2019: rose; In March 2020: fell; Census 2020 (Lebanon)",
        "In Fakeplace Hamlet, the head of the municipality said",
        "(Appendix C), (Type I), (Path B), (Fake QQ) and (Fake-Test U)",
        "fieldwork by Fakeorg, Fakeaid, Fakefund and Fakerelief",  # a list without a KM surname
        "Fakeword, a female-led fake trade; Adults (aged 18+) and Score (12, 15) and Duration (12 years)",
        "(Beirut, Lebanon) and (Doe Fakeorg Institute, Lebanon)",
    ],
)
def test_words_shaped_like_these_names_are_kept(text):
    assert privacy.for_ai(text, KM) == text
    assert privacy.leftover(text, KM) == []


def test_a_word_the_text_also_writes_in_lower_case_is_not_a_first_name():
    text = f"Fakeword {W} lens: the fakeword evidence"
    assert privacy.for_ai(text, KM) == text
    assert privacy.for_ai(f"Fakeword {W} lens", KM) == f"{W} lens"


# ------------------------------------------------------------------- the review's checks (questions, hyphens)
# the KM Database's authors: "Fakeorg Institute; Vantrell, Q.; Blenkinsop Zorgwell" and a one-word entry
# "Fairplayer"; the texts also write "fairplayer" and "inclusion" in lower case (ordinary words)
ASKED = privacy.People(
    names=frozenset(watch_people.name_forms("Vantrell, Q.") | watch_people.name_forms("Blenkinsop Zorgwell")),
    surnames=frozenset({"vantrell", "zorgwell", "fairplayer", "inclusion"}),
    ordinary=frozenset({"fairplayer", "inclusion"}),
    ordinary_alone=frozenset({"fairplayer"}),
)


@pytest.mark.parametrize(
    ("question", "sent"),
    [
        # a surname in lower case, as people type in a chat box
        ("what did vantrell find about zqkmtoken?", f"what did {W} find about zqkmtoken?"),
        ("what did vantrell's survey find?", f"what did {W}'s survey find?"),
        ("what did q. vantrell find?", f"what did {W} find?"),
        ("what did zorgwell find?", f"what did {W} find?"),  # the surname of a full-name author
        ("the vantrell-led survey", f"the {W}-led survey"),
        ("What did VANTRELL find?", f"What did {W} find?"),
        ("what did blenkinsop zorgwell find?", f"what did {W} find?"),
        # a surname the texts also write as an ordinary word: withheld only when capitalised
        ("inclusion of children in schools?", "inclusion of children in schools?"),
        ("Inclusion of children in schools?", f"{W} of children in schools?"),
        # a surname only a one-word entry gives: kept as a word, withheld when written as a name
        ("Fairplayer of children in schools?", "Fairplayer of children in schools?"),
        ("fairplayer of children?", "fairplayer of children?"),
        ("What did Fairplayer (2020) find?", f"What did {W} (2020) find?"),
        ("What did Fairplayer 2020 find?", f"What did {W} 2020 find?"),
        ("Fairplayer's survey?", f"{W}'s survey?"),
        ("What did Q. Fairplayer find?", f"What did {W} find?"),
        ("a study by Fairplayer?", f"a study by {W}?"),
        # nothing else changes
        ("e.g. girls' schooling in fake villages", "e.g. girls' schooling in fake villages"),
    ],
)
@pytest.mark.django_db
def test_a_question_loses_a_km_surname_in_any_case(question, sent):
    assert privacy.typed_for_ai(question, ASKED) == sent
    assert privacy.any_case(sent, ASKED) == sent  # run again, it changes nothing
    assert privacy.clean_question(question, ASKED) == sent


def test_any_case_withholds_what_a_question_kept_before_the_rule_holds():
    assert (
        privacy.any_case("Q: what did vantrell find? [name withheld]", ASKED) == f"Q: what did {W} find? {W}"
    )
    assert privacy.any_case("inclusion and Fairplayer", ASKED) == "inclusion and Fairplayer"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("The Vantrell-led survey found fake rates.", f"The {W}-led survey found fake rates."),
        ("A Vantrell-based model; J. Vantrell-led work", f"A {W}-based model; {W}-led work"),
        ("The Smith-Fakejones-led survey", f"The {W}-led survey"),  # a hyphenated surname
        ("Vantrell’s, (Vantrell) and VANTRELL", f"{W}’s, ({W}) and {W}"),
        # kept: an organisation, a word, a capitalised second part
        (
            "A Fakeorg-led and child-led survey; Vantrell-Fakeword",
            "A Fakeorg-led and child-led survey; Vantrell-Fakeword",
        ),
    ],
)
def test_a_documents_own_author_joined_by_a_hyphen_is_withheld(text, shown):
    own = frozenset({"vantrell", "smith-fakejones"})
    for clean in (privacy.for_ai, privacy.for_download):
        assert clean(text, privacy.NOBODY, own) == shown, clean.__name__
