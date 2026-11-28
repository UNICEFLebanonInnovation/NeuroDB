"""Names of people in the KM Database's texts (SPEC_km §4.4, the parts its downloads use): citations and
interview speakers withheld without any list of names, a document's own authors withheld in its texts, the
KM Database's own authors and names after a title withheld anywhere, links and numbers kept; and the final
check. Fake strings only."""

from __future__ import annotations

import re

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
        # a "Surname, I." item before the quoted title it wrote (the browser check found an evidence origin
        # "Own analysis of: Surname, I. 'Title', ..." whose name the downloads kept)
        (
            "Own fake analysis of: Fakeone, A. 'Fake Costing Report', Fake Final Report, July 2021",
            f"Own fake analysis of: {W} 'Fake Costing Report', Fake Final Report, July 2021",
        ),
        (
            'as Fakeone, A. B. "Fake Title" and Faketwo, C. “Fake Other”',
            f'as {W} "Fake Title" and {W} “Fake Other”',
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
        # a section's letter before a quoted heading, and a place's
        "(Results, E. 'Fake factors') and Lebanon, A. 'Fake area'",
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
        # a surname with digits or "_" after it (a citation key, a file name)
        ("what about vantrell2019 instead?", f"what about {W}2019 instead?"),
        ("what about vantrell_survey instead?", f"what about {W}_survey instead?"),
        ("what about Vantrell2019 instead?", f"what about {W}2019 instead?"),
        ("what about Vantrell_2019 instead?", f"what about {W}_2019 instead?"),
        ("what about vantrell2019's data?", f"what about {W}2019's data?"),
        ("what about inclusion2019 instead?", "what about inclusion2019 instead?"),
        # a surname anywhere in a file name or a citation key: after "_", before a hyphen and digits
        ("what did LCRP_Vantrell_2019 find", f"what did LCRP_{W}_2019 find"),
        ("survey_vantrell found", f"survey_{W} found"),
        ("UNICEF_Vantrell found", f"UNICEF_{W} found"),
        ("Vantrell-2019 on disability", f"{W}-2019 on disability"),
        ("vantrell-2019 on disability", f"{W}-2019 on disability"),
        ("vantrell2019vantrell", f"{W}2019{W}"),
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
        ("The Vantrell2019 survey and Vantrell_2020 data", f"The {W}2019 survey and {W}_2020 data"),
        ("LCRP_Vantrell_2019, survey_vantrell and Vantrell-2019", f"LCRP_{W}_2019, survey_{W} and {W}-2019"),
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


# an e-mail address written out to dodge a filter: withheld whole, its local part (a name) with it
SPELLED = (
    "zayna.fakeharb at example.org",
    "zayna.fakeharb(at)example.org",
    "zayna.fakeharb[at]example.org",
    "zayna.fakeharb{at}example.org",
    "zayna.fakeharb -at- example.org",
    "zfakeharb at mail.example.org",
    "zayna.fakeharb [at] example [dot] org",
    "zayna.fakeharb (at) example (dot) org",
    "zayna.fakeharb {at} example {dot} org",
    "zayna.fakeharb at example dot org",
    "zayna.fakeharb at example [dot] org",
    # "-dot-", "<dot>" and spaces in the brackets; a name with "_" or in capitals before any domain's case
    "zfakeharb -at- example -dot- org",
    "zfakeharb <at> example <dot> org",
    "zfakeharb [ at ] example [ dot ] org",
    "zfakeharb ( at ) example ( dot ) org",
    "zayna_fakeharb at Example.Org",
    "ZFAKEHARB AT EXAMPLE.ORG",
    "zayna_fakeharb at example.invalid",
)
# in a question, any name before "at" and a domain, whatever its case or ending, and "at" or a dot in letters
# that look like them
SPELLED_TYPED = (
    "zfakeharb at Example.Org",
    "zfakeharb at example.invalid",
    "zfakeharb at example. org",
    "zfakeharb \u0430t example.org",
    "zfakeharb \uff41\uff54 example.org",
    "zfakeharb at example\uff0eorg",
)


@pytest.mark.parametrize("address", SPELLED)
@pytest.mark.django_db
def test_an_address_written_out_is_withheld_whole(address):
    question = f"out of school children in Akkar, contact {address}"
    assert (
        privacy.clean_question(question)
        == f"out of school children in Akkar, contact {privacy.EMAIL_WITHHELD}"
    )
    text = f"For the data, write to {address} before June."
    for clean in (privacy.for_download, privacy.for_ai):
        assert clean(text) == f"For the data, write to {privacy.EMAIL_WITHHELD} before June.", clean.__name__
    assert privacy.leftover(privacy.for_ai(text)) == []


@pytest.mark.parametrize("address", SPELLED_TYPED)
@pytest.mark.django_db
def test_an_address_written_out_in_a_question_is_withheld_whole(address):
    question = f"out of school children in Akkar, contact {address}"
    assert (
        privacy.clean_question(question)
        == f"out of school children in Akkar, contact {privacy.EMAIL_WITHHELD}"
    )


@pytest.mark.parametrize(
    ("question", "kept"),
    [
        # a common word before "at" and a domain is no name: only the domain goes
        ("out of school children at example.org", f"out of school children at {privacy.LINK_WITHHELD}"),
        ("children at example dot org", f"children at {privacy.LINK_WITHHELD}"),
        # nothing else changes
        ("meet at 5 pm about child labour", "meet at 5 pm about child labour"),
        ("look at data on child labour", "look at data on child labour"),
        ("located at Tyre. Net enrolment rose", "located at Tyre. Net enrolment rose"),
        ("at Halba. info sessions", "at Halba. info sessions"),
        ("at school. net enrolment", "at school. net enrolment"),
        ("e.g. at 45% (dot) org", "e.g. at 45% (dot) org"),
    ],
)
@pytest.mark.django_db
def test_a_common_word_before_a_domain_is_kept(question, kept):
    assert privacy.clean_question(question) == kept


def test_a_download_keeps_a_word_and_a_link_after_at():
    text = "Data were published at Example.Org in 2024."
    assert privacy.for_download(text) == text
    assert privacy.for_ai(text) == f"Data were published at {privacy.LINK_WITHHELD} in 2024."


def test_the_address_rule_is_neurodbs_shared_one():
    """The KM Database's rule for e-mail addresses written out is the one every assistant uses
    (``watch.people``): a question loses an address in every written-out form, a record's text in the
    forms the record rule takes, and the KM Database cleans exactly as before."""
    from neurodb.watch import people
    from tests.written_addresses import FORMS, NOT_ADDRESSES, RECORD, RECORD_FORMS, TYPED

    for form, written in FORMS.items():
        for text in (TYPED.format(written), RECORD.format(written), written, written.upper()):
            for typed in (False, True):
                kept = people.withhold_addresses(text, typed, links=False)  # the KM Database's rule
                assert privacy._emails(text, typed) == kept, (form, text)
            assert privacy.without_addresses(text) == people.without_addresses(text), (form, text)
        assert people.without_addresses(TYPED.format(written)) == TYPED.format(privacy.EMAIL_WITHHELD), form
    for form, written in RECORD_FORMS.items():
        text = people.visible(RECORD.format(written))
        assert people.withhold_addresses(text) == RECORD.format(privacy.EMAIL_WITHHELD), form
    for text in NOT_ADDRESSES:
        assert people.withhold_addresses(text) == text


def test_a_km_text_loses_an_address_with_a_digit_capitals_or_before_a_links_scheme():
    """A KM text, sent to the AI and downloaded: a name holding a digit anywhere ("z2fakemira", "mira-2"),
    letters then digits in capitals ("MHADDAD2"), "AT" in capitals before a word of two letters ("MIRA AT
    example.it", "MIRA AT EXAMPLE.IT" at the end), an address's name before a link with its scheme."""
    from tests.written_addresses import NOT_ADDRESSES, RECORD_ADDRESSES

    for text, sent in RECORD_ADDRESSES.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.for_download(text) == sent, text
    for text, sent in NOT_ADDRESSES.items():  # a sentence that only looks like one is downloaded as it is
        if sent == text:
            assert privacy.for_download(text) == text, text


@pytest.mark.django_db
def test_a_km_question_keeps_what_holds_no_address_and_loses_what_does(monkeypatch):
    """Ask the KM Database: the question as it is kept and sent, and its search words."""
    from neurodb.km import ask
    from tests.written_addresses import TYPED_ADDRESSES, TYPED_NOT_ADDRESSES, left

    for text, sent in TYPED_NOT_ADDRESSES.items():
        assert privacy.clean_question(text) == sent, text
    as_typed = [text for text, sent in TYPED_NOT_ADDRESSES.items() if text == sent]
    searched = {text: ask._searchable(text) for text in as_typed}
    with monkeypatch.context() as patched:  # the search words, as they would be without the address rule
        patched.setattr(privacy, "without_addresses", lambda text: text)
        assert searched == {text: ask._searchable(text) for text in as_typed}
    for text, sent in TYPED_ADDRESSES.items():
        assert privacy.clean_question(text) == sent, text
        assert not left(ask._searchable(text)), text


@pytest.mark.django_db
def test_a_km_question_loses_what_a_record_loses():
    """What a record's text loses ("ZFAKEMIRA AT EXAMPLE.IT holds the files", "ZFAKEMIRA1990 at example.it"),
    a question typed with it loses too, and so do its search words."""
    from neurodb.km import ask
    from tests.written_addresses import RECORD_ADDRESSES, left

    for text, sent in RECORD_ADDRESSES.items():
        assert privacy.clean_question(text) == sent, text
        assert not left(ask._searchable(text)), text


@pytest.mark.django_db
def test_the_written_spec_in_a_km_text_and_question():
    """The written spec's e-mail rules in the KM Database (which reads no link after them): a question loses what
    any typed text loses (E1, E2, E5), a KM text and its download what any record's text loses; a page's domain
    after a verb that goes to a page goes as a link, a word between "the" and the page's word too (E3); the
    sentence of E4 is sent and downloaded as it is."""
    from tests.written_addresses import SPEC_KEPT, SPEC_PAGES, SPEC_RECORD, SPEC_TYPED

    for text, sent in SPEC_TYPED.items():
        assert privacy.clean_question(text) == sent, text
    for text, sent in SPEC_RECORD.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.for_download(text) == sent, text
        assert privacy.typed_for_ai(text) == sent, text
    for text, sent in SPEC_PAGES.items():
        for clean in (privacy.for_ai, privacy.for_download, privacy.typed_for_ai, privacy.clean_question):
            assert clean(text) == sent, (clean.__name__, text)
    for text in SPEC_KEPT:
        for clean in (privacy.for_ai, privacy.for_download, privacy.typed_for_ai, privacy.clean_question):
            assert clean(text) == text, (clean.__name__, text)


# ------------------------------------------------------- a person's name that is also a town's (places.py)
TOWN_NAMES = (
    "Naameh (2021) found that 12% of girls married before 18.",
    "According to Naameh and Fakedoe (2021), 12% of girls married before 18.",
    '"We had no choice" (Naameh, 16)',
    '"I left school to work" – Sayda, 14',
    '"I left school to work" (Houla, 15)',
    "(Hadath 2019) found high rates.",
    "Rayak (2020) reported that 30% of children worked.",
    "Kousba (2018) found high rates.",
    "Hasroun (2020) found high rates.",
)


@pytest.mark.parametrize("text", TOWN_NAMES)
def test_a_name_that_is_also_a_towns_is_withheld_where_it_is_written_as_a_name(text):
    """A town's spelling (:mod:`neurodb.km.places`) keeps its place only where it is written as a place
    ("in Saida 2023", "numbers for Sayda, 2024"): a citation, an interview's speaker or an author's surname
    written so is a person's, as it was before the towns were known."""
    town = re.search(r"Naameh|Sayda|Houla|Hadath|Rayak|Kousba|Hasroun", text).group()
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert town not in cleaned and W in cleaned, cleaned


@pytest.mark.django_db
@pytest.mark.parametrize("text", TOWN_NAMES[:3])
def test_a_question_loses_a_name_that_is_also_a_towns(text):
    assert "Naameh" not in privacy.clean_question(text)


def test_a_documents_own_author_named_as_a_town_is_withheld():
    from neurodb.km import authors

    assert authors.surname("Naameh, R.") == "naameh" and authors.surname("Jane Kousba") == "kousba"
    people = authors.gather(
        [("INV-0001", "Hasroun, J."), ("INV-0002", "Naameh, R."), ("INV-0003", "Jane Kousba")], {}
    )
    assert {"hasroun", "naameh", "kousba"} <= set(people.surnames)
    text = "Naameh's survey of 300 households found that 12% of girls married before 18."
    own = people.own.get("INV-0002", frozenset())
    for cleaned in (privacy.for_ai(text, people, own), privacy.for_download(text, people, own)):
        assert "Naameh" not in cleaned, cleaned


# a town's spelling written as a person's: in a citation's shape ("et al.", "(2019)", a parenthesis's item, a
# possessive), with a name after "&" or "and" that is no place, after a place and the comma that opens a
# sentence
TOWN_CITED = (
    ("Evidence from Akar & Doering 2020 shows that 30% of fake girls married early.", ("Akar", "Doering")),
    ("Findings from Naameh & Doering 2020 show high dropout.", ("Naameh", "Doering")),
    ("Data from Akar and Doering 2020 show high dropout.", ("Akar", "Doering")),
    ("Data from Tyre & Doering 2020 shows dropout.", ("Doering",)),
    ("Numbers for Akar and Doering, 2020 show dropout.", ("Akar", "Doering")),
    ("As shown in Naameh (2021), enrolment fell by 12%.", ("Naameh",)),
    ("Findings from Naameh et al. show high dropout.", ("Naameh",)),
    ("Data from Kousba, 2019 show a rise in child labour.", ("Kousba",)),
    ("As discussed in Akar et al. (2020), fees rose.", ("Akar",)),
    ("The figures in Naameh's (2021) study were higher.", ("Naameh",)),
    ("The figures in Naameh's 2021 study were higher.", ("Naameh",)),
    ("Fake data (in Naameh, 2021) show a rise.", ("Naameh",)),
    ("In Saida, Kousba (2019) counted 1,200 fake girls.", ("Kousba",)),
    ("In Lebanon, Naameh et al. (2020) estimated that one in four girls married before 18.", ("Naameh",)),
    ("Among refugee children from Syria, Akar (2019) found that 40% worked.", ("Akar",)),
    ("In Lebanon, Kousba (2019) found that 40% of refugee children worked.", ("Kousba",)),
    ("For girls in Akkar, Akar (2019) reports higher dropout.", ("Akar ",)),
    ("In Saida, Naameh (2021) found that 40% of children were out of school.", ("Naameh",)),
    ("In Lebanon, Kousba, 2019 found that 40% of children worked.", ("Kousba",)),
    # a year with a letter, a page after the year, a citing word before "in", "for" or "from"
    ("As reported in Naameh 2021a, 12% of girls married before 18.", ("Naameh",)),
    ("As reported in Naameh 2021, p. 12, most girls married before 18.", ("Naameh",)),
    ("As noted in Naameh 2021, 12% of girls married before 18.", ("Naameh",)),
    ("Key finding: As reported in Naameh 2021a, 30% of fake girls married early.", ("Naameh",)),
    ("Fake data (cited in Akar 2018) show dropout.", ("Akar",)),
    ("Fake data (as cited in Akar 2018) show dropout.", ("Akar",)),
    ("as cited in Akar 2018, teachers lacked training.", ("Akar",)),
    ("Findings in Akar 2016 show that teachers lacked training.", ("Akar",)),
    ("as described in Akar 2016a, teachers lacked training.", ("Akar",)),
    ("Fake rates rose, as shown in Kousba 2019b.", ("Kousba",)),
    ("Dropout rose in Naameh 2019, pp. 4-5, for girls.", ("Naameh",)),
    ("Child labour rose (in Naameh 2019a).", ("Naameh",)),
    # more citing words before "in" or "from", "as" within the two words before it, "e.g.," before it
    ("As found in Naameh 2021, 12% of girls married before 18.", ("Naameh",)),
    ("As mentioned in Akar 2018, fees rose.", ("Akar",)),
    ("As indicated in Kousba 2019, fees rose.", ("Kousba",)),
    ("As explained in Hasroun 2020, fees rose.", ("Hasroun",)),
    ("As observed in Naameh 2021, fees rose.", ("Naameh",)),
    ("As suggested in Akar 2018, fees rose.", ("Akar",)),
    ("As seen in Kousba 2019, fees rose.", ("Kousba",)),
    ("As detailed in Hasroun 2020, fees rose.", ("Hasroun",)),
    ("As demonstrated in Naameh 2021, fees rose.", ("Naameh",)),
    ("Similar results were found in Naameh 2021.", ("Naameh",)),
    ("Table adapted from Naameh 2021.", ("Naameh",)),
    ("Source: adapted from Akar 2018.", ("Akar",)),
    ("Figures taken from Kousba 2019 show a rise in child labour.", ("Kousba",)),
    ("Dropout was higher among girls (e.g., in Naameh 2021).", ("Naameh",)),
    ("Figure 2 (adapted from Akar 2018) shows enrolment by grade.", ("Akar",)),
    ("These patterns were also observed in Akar 2018.", ("Akar",)),
    ("A similar trend was found in Hasroun 2020 and Kousba 2019.", ("Hasroun", "Kousba")),
    ("As also noted in Naameh 2021, fees rose.", ("Naameh",)),
    # "&" between two towns' one-word spellings joins authors, never places
    ("Data from Akar & Naameh 2020 show high dropout.", ("Akar", "Naameh")),
    ("Survey data from Hasroun & Kousba 2021 show high dropout.", ("Hasroun", "Kousba")),
)


@pytest.mark.parametrize(("text", "names"), TOWN_CITED)
def test_a_towns_spelling_written_as_a_citation_is_a_name(text, names):
    """A town's one-word spelling is a place only in the shapes of a place before a bare year ("in Saida
    2023", "numbers for Sayda, 2024", "in Tyre, Qana and Houla 2024", where every name listed is a place):
    "et al.", a year in parentheses, a parenthesis's item, a possessive, a name after "&" that is no place,
    and a name after a place and the comma that opens a sentence ("In Lebanon, Kousba (2019)") are a
    person's."""
    people = privacy.People(surnames=frozenset({"akar", "kousba"}))
    for cleaned in (
        privacy.for_ai(text),
        privacy.for_download(text),
        privacy.for_ai(text, people),
        privacy.for_download(text, people),
    ):
        assert W in cleaned and not any(name in cleaned for name in names), cleaned
        assert privacy.leftover(cleaned) == [], cleaned
    if "Tyre" in text or "Saida," in text or "Lebanon" in text or "Syria" in text:
        place = re.search(r"Tyre|Saida|Lebanon|Syria|Akkar", text).group()
        assert place in privacy.for_ai(text)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "asked",
    [
        "Summarize the findings from Akar et al. on child marriage",
        "What is reported in Naameh (2021) about dropout?",
        "What did findings from Akar & Doering 2020 say about girls who married early?",
        "In Lebanon, Kousba (2019) found what?",
        "What is reported in Akar 2020 about school fees?",
        "What is reported in Naameh 2021a about dropout?",
        "As noted in Naameh 2021, how many girls married?",
        "What does the study in Akar 2021, p. 4, say?",
    ],
)
def test_a_question_loses_a_towns_spelling_written_as_a_citation(asked):
    cleaned = privacy.clean_question(asked)
    assert not re.search(r"Akar|Naameh|Doering|Kousba", cleaned), cleaned


@pytest.mark.parametrize(
    "text",
    [
        "child labour in Saida 2023",
        "numbers for Sayda, 2024",
        "numbers for Saida and Sayda, 2024",
        "out of school refugees in Majdal Anjar and Saadnayel 2023",
        "child labour in Tyre, Qana and Houla 2024",
        "child labour in Tyre, Qana, and Houla 2024",
        "child labour from Tyre and Qana 2024",
        "trends in out-of-school children for Aakar 2019-2022",
    ],
)
def test_a_towns_spelling_written_as_a_place_before_a_year_stays(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert cleaned == text


@pytest.mark.parametrize(
    "text",
    [
        "Cholera 2022 (Bebnine) kept children home.",
        "Floods 2023 (Saida) closed schools.",
        "Displacement 2024 (Saida) rose.",
        "Floods 2023 (Akkar) closed schools.",
        "Cholera 2022 (Tripoli) kept children home.",
        "Cholera 2022: cases rose.",
    ],
)
def test_a_common_word_before_a_year_and_a_place_in_parentheses_stays(text):
    """S2 ("Doe 2021 (Bebnine) found"): the word before the year and the place in parentheses must look like a
    name; a disease, an event or a word of a sentence ("Cholera 2022 (Bebnine)", "Floods 2023 (Saida)")
    stays."""
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert cleaned == text
    assert (
        privacy.typed_for_ai(f"{text[:-1]}: what did schools report?")
        == f"{text[:-1]}: what did schools report?"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Doe 2021 (Bebnine) found high rates.",
        "Fakename 2021 (Saida) found high rates.",
        "Fakeone 2020 (Lebanon) relates.",
        "Clement 2021 (Saida) found high rates.",
        "Fakeroberts 2021 (Saida) found high rates.",
        # a family name with a common noun's ending, or an event's word in the singular
        "Blankenship 2021 (Lebanon) found that 12% of girls married before 18.",
        "Garrity 2021 (Lebanon) found high dropout.",
        "McGuinness 2021 (Lebanon) found high dropout.",
        "Holderness 2020: 20-60y; Fakedoe 2022: 10-15y",
        "Storm 2021 (Lebanon) found high dropout.",
        "Flood 2021 (Lebanon) found high dropout.",
    ],
)
def test_a_name_before_a_year_and_a_place_in_parentheses_is_withheld(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert W in cleaned and text.split(" ", 1)[0] not in cleaned, cleaned


# ------------------------------------------------- a name with a digit before a two-letter word as an ending
@pytest.mark.parametrize(
    "text",
    [
        "Fake rainfall data: contact zfakemira2 at example.it for the tables.",
        "Fake rainfall survey: hsaab90 at example.be holds the files.",
        "Fake rainfall survey: zfakemira2 at example.no holds the files.",
        "Fake rainfall survey: zfakemira2 at example.me holds the files.",
    ],
)
def test_a_name_with_a_digit_is_withheld_before_any_countrys_ending(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert "zfakemira2" not in cleaned and "hsaab90" not in cleaned and "example" not in cleaned, cleaned
        assert privacy.EMAIL_WITHHELD in cleaned


# ------------------------------------------------------------- round 3: the KM Database's names, converged
def test_a_km_surname_never_takes_a_towns_place_exception():
    """A surname of the KM Database's authors spelled like a town ("Naameh") is a person's wherever a citation
    is written, also before a bare year after "in" ("Dropout rose in Naameh 2021"); without that surname,
    a town's spelling there stays a place."""
    people = privacy.People(surnames=frozenset({"naameh"}))
    for text in ("Child labour rose in Naameh 2021.", "Similar results were found in Naameh 2021."):
        for cleaned in (privacy.for_ai(text, people), privacy.for_download(text, people)):
            assert "Naameh" not in cleaned and W in cleaned, cleaned
    assert privacy.for_ai("Child labour rose in Naameh 2021.") == "Child labour rose in Naameh 2021."
    assert privacy.for_ai("Dropout rose in Saida 2023.") == "Dropout rose in Saida 2023."


@pytest.mark.parametrize(
    "text",
    [
        "Saad Nayel et al. (2020) found that 12% of girls married before 18.",
        "Wadi Khaled et al. (2019) reported higher dropout.",
        "Tal Abbas et al. (2019) reported higher dropout.",
        "Tell Abbas et al. (2019) reported higher dropout.",
        "Kab Elias et al. (2019) reported higher dropout.",
        "Bar Elias et al. (2019) reported higher dropout.",
        "Mar Mikhael et al. (2019) reported higher dropout.",
        "Saad Nayel (2020) found that fees rose.",
        "Dropout rose (Saad Nayel, 2021; Fakedoe, 2020).",
        "According to Saad Nayel, 2021, fees rose.",
        "Saad Nayel and Fakedoe (2020) found that fees rose.",
    ],
)
def test_a_place_of_several_words_that_reads_as_a_name_is_withheld_in_a_citations_shape(text):
    """A place's name of several words made only of words a name has ("Saad Nayel", "Tal Abbas", "Wadi
    Khaled") is kept only where it is written as a place before a bare year ("in Saad Nayel 2023"), never in a
    citation's shape ("et al.", "(2020) found", a citation's parenthesis, "NAME, 2021,")."""
    place = re.match(r"(?:According to |Dropout rose \()?(\w+ \w+)", text).group(1)
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert place not in cleaned and W in cleaned, cleaned


@pytest.mark.parametrize(
    "text",
    [
        "child labour (Western Bekaa 2023)",
        "out of school refugees in Majdal Anjar and Saadnayel 2023",
        "child labour in Saad Nayel 2023",
        "child labour in Tal Abbas 2023",
        "numbers for Bar Elias, 2024",
        "child labour (Jabal Mohsen 2023)",
    ],
)
def test_a_place_of_several_words_written_as_a_place_stays(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert cleaned == text


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("According to Kousba 2019, 40% of refugee children worked.", ("Kousba",)),
        ("Key finding: According to Hasroun 2019, fake girls married early.", ("Hasroun",)),
        ("According to Jane Fakedoe 2019, 40% worked.", ("Fakedoe",)),
        ("Source: Fakedoe 2021.", ("Fakedoe",)),
        ("Per Fakedoe 2019, 40% worked.", ("Fakedoe",)),
        ("Based on Fakedoe 2019, dropout rose.", ("Fakedoe",)),
        ("Adapted from Fakedoe and Roefake 2019.", ("Fakedoe", "Roefake")),
        ("References: Fakedoe 2019.", ("Fakedoe",)),
        ("Data: Fakedoe 2020.", ("Fakedoe",)),
        ("Sources: Kousba 2019 and Fakeorg 2020.", ("Kousba", "Fakeorg")),
        ("Sources: Fakedoe 2019; Roefake 2020.", ("Fakedoe", "Roefake")),
        ("Sources: Fakedoe 2019, Roefake 2020 and Poefake 2021.", ("Fakedoe", "Roefake", "Poefake")),
        ("Fakedoe's 2021 survey found high dropout.", ("Fakedoe",)),
        ("The Fakedoe's 2020 study covered Akkar.", ("Fakedoe",)),
        # a sentence that opens with "In", a name and a year
        ("In Fakedoe 2021, 12% of girls married before 18.", ("Fakedoe",)),
        ("In Fakedoe 2021 12% of girls married before 18.", ("Fakedoe",)),
        ("Child marriage fell. In Fakedoe 2021, 12% of girls married before 18.", ("Fakedoe",)),
    ],
)
def test_a_name_and_a_year_after_a_sources_words_are_withheld(text, names):
    """ "According to", "Based on", "Source:", "Sources:" (each item of the list), "References:", "Adapted
    from", "Data:", "Per" and a name before a bare year, a possessive name before a year and a study's word,
    and a sentence that opens with "In", a name and a year: a citation; its kept names stay."""
    for people in (privacy.NOBODY, privacy.People(surnames=frozenset({"kousba", "fakedoe"}))):
        for cleaned in (privacy.for_ai(text, people), privacy.for_download(text, people)):
            assert W in cleaned and not any(name in cleaned for name in names), cleaned
            assert privacy.leftover(cleaned, people) == [], cleaned


@pytest.mark.parametrize(
    "text",
    [
        "According to UNICEF 2021, 40% of refugee children worked.",
        "Based on Lebanon 2020 data, dropout rose.",
        "Sources: UNICEF 2021 and Lebanon 2020.",
        "Lebanon's 2020 survey found high dropout.",
        "In Saida 2023, dropout rose.",
        "In Lebanon 2020, dropout rose.",
        "In Naameh 2021, dropout rose.",
        "Since 2019, dropout rose.",
    ],
)
def test_kept_names_after_a_sources_words_stay(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert cleaned == text


def test_a_sentence_that_opens_with_in_and_a_km_surname_is_withheld():
    people = privacy.People(surnames=frozenset({"naameh"}))
    cleaned = privacy.for_ai("In Naameh 2021, 12% of girls married before 18.", people)
    assert "Naameh" not in cleaned and W in cleaned


@pytest.mark.parametrize(
    "text",
    [
        "\u201cI left school to work,\u201d said Fakename, 14.",
        "\u201cI left school to work,\u201d Fakename, 14, explained.",
        "In an interview, Fakename, 14, said she left school to work.",
        "\u201cWe had no choice,\u201d said Naameh, 16.",
        "\u201cWe had no choice,\u201d Naameh, 16, said.",
        "In an interview with Sayda, 14, she said she left school.",
        "\u201cWe had no choice,\u201d said Rayak, 16, about fake girls who married early.",
        "\u201cI want to go back to school,\u201d says Fakename, 13.",
        "\u201cI want to go back to school,\u201d explained Fakename, 15, a fake pupil in Akkar.",
        '"I left school to work," said Fakename, 14.',
        "She was interviewed with Fakename, 12, in Akkar.",
    ],
)
def test_a_speaker_after_a_quote_said_or_an_interview_is_withheld(text):
    """An interview's speaker by first name and age after a closing quote and a comma ("…,” said Fakename,
    14."), after "said", "says", "told" or "explained", or after "In an interview," or "interview with"."""
    speaker = re.search(r"Fakename|Naameh|Sayda|Rayak", text).group()
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert speaker not in cleaned and W in cleaned, cleaned
        assert privacy.leftover(cleaned) == [], cleaned
    assert speaker not in privacy.typed_for_ai(text)


@pytest.mark.parametrize(
    "text",
    [
        "\u201cWe had no choice,\u201d said Mother, 34.",
        "\u201cWe had no choice,\u201d said a teacher, 34 years in the job.",
        "In an interview, Lebanon, 12 schools said fees rose.",
        "\u201cFees rose,\u201d said UNICEF, 2021.",
        "Enrolment rose, said Fakeorg, by 12.5% in Akkar.",
    ],
)
def test_a_role_or_a_kept_word_after_said_stays(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert cleaned == text


def test_a_km_surname_in_a_vancouver_reference_is_withheld():
    """A reference written the Vancouver way ("Akar J, Naameh R. Fake title. 2021."): every author whose
    surname is one of the KM Database's, the last one before the title too."""
    people = privacy.People(surnames=frozenset({"naameh", "akar", "kousba"}))
    for text in (
        "Naameh R, Akar J. Fake report. 2021.",
        "Akar J, Naameh R. Fake title. 2021.",
        "Akar J. Fake title. 2021.",
        "Akar IJ. Fake title. 2021.",
        "Evidence drawn from Naameh R, Akar J. Fake report on schools. Beirut; 2021.",
    ):
        for cleaned in (privacy.for_ai(text, people), privacy.for_download(text, people)):
            assert "Akar" not in cleaned and "Naameh" not in cleaned, cleaned
            assert privacy.leftover(cleaned, people) == [], cleaned
    assert privacy.for_ai("Fake title. Beirut: Fake Press. 2021.", people) == (
        "Fake title. Beirut: Fake Press. 2021."
    )


@pytest.mark.parametrize(
    "text",
    [
        "For questions write to zayna.fakemira at Example.No before June.",
        "Focal point: z.fakemira at EXAMPLE.BE for the annex.",
        "Contact: zayna.fakemira at EXAMPLE.NO",
        "For the data, write to zfakemira at example.no before June.",
        "Contact: zfakemira arobase example point org",
    ],
)
def test_a_km_text_loses_an_address_whose_ending_is_also_a_word(text):
    for cleaned in (privacy.for_ai(text), privacy.for_download(text)):
        assert "fakemira" not in cleaned and "example" not in cleaned.lower(), cleaned
        assert privacy.EMAIL_WITHHELD in cleaned, cleaned


# ------------------------------------------------------- the regression sweep against the pushed rules
TOWNS_AS_SOURCES = {
    "In Lebanon, Akar and Naameh 2020 found dropout.": f"In Lebanon, {W} and {W} 2020 found dropout.",
    "Building on the work in Akar 2020, we asked girls why they left school.": (
        f"Building on the work in {W} 2020, we asked girls why they left school."
    ),
    "The same pattern appears in Hasroun 2020 and in Akar 2016.": (
        f"The same pattern appears in {W} 2020 and in {W} 2016."
    ),
    "Figures from Kousba 2019 show a rise in child labour.": f"Figures from {W} 2019 show a rise in child labour.",
    "The figures given for Akar, 2016 were higher.": f"The figures given for {W}, 2016 were higher.",
    "Girls in Naameh 2021 reported early marriage.": f"Girls in {W} 2021 reported early marriage.",
    "(for Naameh, 2021)": f"(for {W}, 2021)",
    "Numbers for Akar & Naameh, 2020 show dropout.": f"Numbers for {W} & {W}, 2020 show dropout.",
    "Similar findings are reported for Naameh, 2021 and for Akar, 2020.": (
        f"Similar findings are reported for {W}, 2021 and for {W}, 2020."
    ),
    "Jabal Mohsen (2020) found": f"{W} (2020) found",
    "Jabal Mohsen et al. (2019) reported higher dropout.": f"{W} et al. (2019) reported higher dropout.",
    "Deir Ammar (2019) reported that 30% of children worked.": f"{W} (2019) reported that 30% of children worked.",
}
TOWNS_AS_PLACES = (
    "Dropout in Akar 2023 was high.",
    "Numbers for Sayda, 2024 show a rise.",
    "Compare child labour in Tyre, Qana and Houla 2024 with 2023",
    "Enrolment fell in Akkar 2023 and in Akar 2016.",
    "child labour (Jabal Mohsen 2023)",
)


def test_a_town_written_as_a_source_is_withheld_even_with_no_author_known():
    """A town's spelling that is also a family name is a place only where a sentence writes it as one: a
    citation's word before it ("the work in", "appears in", "figures from", "given for"), a source's verb
    after its year ("2020 found", "2021 reported"), a parenthesis, "&" or a list that goes on from a source
    make it a source's name, as the pushed rules withheld it, whoever the KM Database's authors are; so does
    a place's name of several words in a source's form ("Jabal Mohsen et al.", "Deir Ammar (2019) reported"),
    which stays a place before a bare year ("child labour (Jabal Mohsen 2023)")."""
    for text, sent in TOWNS_AS_SOURCES.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.for_download(text) == sent, text
    for text in TOWNS_AS_PLACES:
        assert privacy.for_ai(text) == text, text


def test_a_km_download_loses_a_codes_web_address_as_a_link():
    """A code before "at" and a domain with an ending is a name's word as any other (the written spec's E1:
    initials or a birth date and digits look like a grade's or a field's code), in a download too ("F09 at
    Example.Org", "CE1 at Tyre.Net"); letters in capitals then digits before a sentence missing its space are
    an address's name, as initials and a digit write one ("KZ3 at school.it")."""
    for text, sent in {
        "F09 at Example.Org": "[email withheld]",
        "CE1 at Tyre.Net 2023": "[email withheld] 2023",
    }.items():
        assert privacy.for_download(text) == sent, text
        assert privacy.for_ai(text) == sent, text
    assert (
        privacy.for_download("Students of KZ3 at school.it was hot.")
        == "Students of [email withheld] was hot."
    )


# ------------------------------------------------------------------ the second regression sweep (KM names)
# a pair of places before a year: each name judged on its own, a town's other spelling or a place's name of
# several words kept only where a word that writes a place comes before the pair ("in Tyre, Qana and Houla
# 2024"); a request's word that opens a typed question is no name, written in lower case ("show Beirut and Tyre
# 2023"), and a record's text reads it as part of the name
PAIRS_PLACES = (
    "in Tyre, Qana and Houla 2024",
    "Dropout in Saida and Tyre 2023 was high.",
    "Fees in Bar Elias and Zahle 2023 rose.",
    "In Tyre and Qana 2023, fees rose.",
)
PAIRS_NOT_WRITTEN_AS_PLACES = {
    "In Chtoura and Zahle 2023, fees rose.": f"{W} and Zahle 2023, fees rose.",
    "In Bar Elias and Zahle 2023, fees rose.": f"{W} and Zahle 2023, fees rose.",
    "What about Saida and Tyre 2023?": f"What about {W} and Tyre 2023?",
    "Visits to Chtoura and Tyre 2023": f"Visits to {W} and Tyre 2023",
    "Haret Hreik and Beirut 2023 dropout rates": f"{W} and Beirut 2023 dropout rates",
}
PAIRS_WITHHELD = {
    "from Tyre & Doering 2020": f"from Tyre & {W} 2020",
    "Quenvell and Tyre 2023 found high dropout.": f"{W} and Tyre 2023 found high dropout.",
    "Akar and Tyre 2020 found": f"{W} and Tyre 2020 found",
}
# a request's word that opens a typed question, then a capitalised word: written in lower case when the
# question then names no one; read with the name after it when one is withheld
REQUESTS_TYPED = {
    "Show Beirut and Tyre 2023": "show Beirut and Tyre 2023",
    "Compare Akkar and Tripoli 2023": "compare Akkar and Tripoli 2023",
    "Show Quenvell and Tyre 2023": f"{W} and Tyre 2023",
    "Show Midterm and Tyre 2023": f"{W} and Tyre 2023",
    "Analyse Halba and Naameh 2023": f"{W} and {W} 2023",
    "Summarise Vantrell et al. 2021": f"{W} et al. 2021",
}


@pytest.mark.django_db
def test_a_pair_of_places_before_a_year_keeps_its_places_only_where_they_are_written_as_places():
    """C5 ("X and Y 2023"): a town's other spelling or a place's name of several words stays only after a word
    that writes a place ("in", "for", "from"); "&", a source's verb after the year or a name that is no place
    still goes, and so does one of the KM Database's surnames. A request's word that opens a typed question is
    no name (written in lower case there); in a record's text it is read with the name."""
    for text in PAIRS_PLACES:
        assert privacy.for_ai(text) == text, text
        assert privacy.clean_question(text) == text, text
    for text, sent in {**PAIRS_NOT_WRITTEN_AS_PLACES, **PAIRS_WITHHELD}.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.for_download(text) == sent, text
        assert privacy.clean_question(text) == sent, text
    for text, sent in REQUESTS_TYPED.items():
        assert privacy.clean_question(text) == sent, text
        assert privacy.typed_for_ai(text) == sent, text
        assert privacy.leftover(f"Question: {sent}") == [], sent  # the final check reads it as it was sent
        assert privacy.for_ai(sent) == sent, sent
    assert privacy.for_ai("Show Beirut and Tyre 2023") == f"{W} and Tyre 2023"
    assert privacy.for_ai("Show Quenvell and Tyre 2023") == f"{W} and Tyre 2023"
    # a question that needs no lower case keeps its own
    for text in ("Compare Akkar and Beirut", "Show dropout in Akkar 2023", "Show Akkar dropout"):
        assert privacy.clean_question(text) == text, text
    saida = privacy.People(surnames=frozenset({"saida"}))
    assert privacy.for_ai("What about Saida and Tyre 2023?", saida) == f"What about {W} and Tyre 2023?"
    assert privacy.for_ai("Dropout in Saida and Tyre 2023 was high.", saida) == (
        f"Dropout in {W} and Tyre 2023 was high."
    )
    assert (
        privacy.for_ai("Brevet and Zelwick 2020 found high dropout.")
        == f"{W} and {W} 2020 found high dropout."
    )


@pytest.mark.django_db
def test_a_sources_word_after_in_or_a_source_is_no_name():
    """After a sentence's "In", a source's words ("Source:", "Data:", "Based on") or before a possessive and a
    year, an exam, a school level, a way of collecting data or a tool is no person ("In Brevet 2023",
    "Source: Fieldwork 2023", "Data: ActivityInfo 2023", "Kobo's 2023 data"); a holiday that is a family name
    too ("Ramadan", "Eid") and an invented surname still go."""
    for text in (
        "In Kindergarten 2023, attendance fell.",
        "In Brevet 2023, how many students passed?",
        "Data: ActivityInfo 2023.",
        "Based on Brevet 2023 results, pass rates fell.",
        "Source: Fieldwork 2023.",
        "Source: Interviews 2023.",
        "Kobo's 2023 data show high dropout.",
    ):
        assert privacy.for_ai(text) == text, text
        assert privacy.clean_question(text) == text, text
    for text, sent in {
        "In Ramadan 2023, attendance fell.": f"In {W} 2023, attendance fell.",
        "Source: Zorgwell 2023.": f"Source: {W} 2023.",
        "Eid et al. (2019) found high dropout.": f"{W} et al. (2019) found high dropout.",
    }.items():
        assert privacy.for_ai(text) == sent, text


def test_a_list_with_a_year_after_each_name_is_a_list_of_sources():
    """ "in X 2016, Y 2018 and Z 2019": every name goes, the first too, unless every other one is a place kept
    wherever it is written ("in Saida 2023 and Tyre 2024", "in Akar 2016, Halba 2018 and Tripoli 2019")."""
    for text, sent in {
        "Rates in Akar 2016, Naameh 2018 and Kousba 2019 rose.": f"Rates in {W} 2016, {W} 2018 and {W} 2019 rose.",
        "This mirrors the rates in Akar 2016 and Kousba 2019.": f"This mirrors the rates in {W} 2016 and {W} 2019.",
        "Dropout was high in Naameh 2018 and Kousba 2019.": f"Dropout was high in {W} 2018 and {W} 2019.",
        "Dropout was high in Naameh 2018 and in Kousba 2019.": f"Dropout was high in {W} 2018 and in {W} 2019.",
    }.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.for_download(text) == sent, text
    for text in (
        "Any data on child marriage in Saida 2023 and Tyre 2024?",
        "Rates in Akar 2016, Halba 2018 and Tripoli 2019 rose.",
    ):
        assert privacy.for_ai(text) == text, text


@pytest.mark.django_db
def test_a_scholars_verb_or_a_publications_part_before_in_makes_a_source():
    """A town's spelling after "in" is a source's name after a scholar's verb ("examined", "analysed"), a verb
    in the passive ("was collected"), a publication's part ("the chapter", "Table 2", "The framework"), or
    before a source's verb or a verb and "that" ("2021 revealed", "2021 adds that"), or ", which found"; a
    place stays ("Dropout in Akar 2023 was high.")."""
    for text, sent in {
        "This was examined in Naameh 2021.": f"This was examined in {W} 2021.",
        "Similar patterns were analysed in Kousba 2019.": f"Similar patterns were analysed in {W} 2019.",
        "See the chapter in Hasroun 2021.": f"See the chapter in {W} 2021.",
        "Table 2 in Naameh 2021 shows the rates.": f"Table 2 in {W} 2021 shows the rates.",
        "The framework in Akar 2020 applies.": f"The framework in {W} 2020 applies.",
        "Girls in Naameh 2021 revealed high rates.": f"Girls in {W} 2021 revealed high rates.",
        "This was collected in Sayda 2021.": f"This was collected in {W} 2021.",
        "Families from Houla 2021 adds that fees rose.": f"Families from {W} 2021 adds that fees rose.",
        "The issue is captured in Qana 2021, which found high dropout.": (
            f"The issue is captured in {W} 2021, which found high dropout."
        ),
    }.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.for_download(text) == sent, text
        assert privacy.clean_question(text) == sent, text
    assert privacy.for_ai("Dropout in Akar 2023 was high.") == "Dropout in Akar 2023 was high."
    assert privacy.for_ai("Children were enrolled in Saida 2023.") == "Children were enrolled in Saida 2023."


# ---------------------------------------------------------- the converge round (KM names, no new leaks)
KM_PEOPLE = privacy.People(
    surnames=frozenset({"vantrell", "zorgwell", "fakedoe", "naameh"}),
    own={"INV-0001": frozenset({"fakedoe"})},
)
CITED_REQUEST_WORDS = {
    "Rank et al. (2019) found high dropout.": f"{W} et al. (2019) found high dropout.",
    "What did List et al. (2019) find about class size?": f"What did {W} et al. (2019) find about class size?",
    "(Rank et al., 2019; List, 2020)": f"({W} et al., 2019; {W}, 2020)",
    "List, Q. (2019). Dropout in Lebanon.": f"{W} (2019). Dropout in Lebanon.",
    "See & Quenby 2021 reported high dropout.": f"{W} & {W} 2021 reported high dropout.",
    "Rank & Zorgwell 2020 found high dropout.": f"{W} & {W} 2020 found high dropout.",
    "According to Rank and Vantrell 2020, dropout rose.": f"According to {W} and {W} 2020, dropout rose.",
    "Sources: Show 2019; Plot 2020.": f"Sources: {W} 2019; {W} 2020.",
}


@pytest.mark.django_db
@pytest.mark.parametrize(("text", "sent"), CITED_REQUEST_WORDS.items())
def test_a_requests_word_is_a_name_in_a_citations_shape(text, sent):
    """A request's word ("Show", "List", "Rank", "See") is a family name too: in a citation's shape it is
    withheld in every path, and so is the name it is cited with."""
    assert privacy.for_ai(text) == sent
    assert privacy.for_download(text) == sent
    assert privacy.clean_question(text) == sent
    assert privacy.for_ai(text, KM_PEOPLE, frozenset({"fakedoe"})) == sent


@pytest.mark.django_db
def test_a_first_name_that_is_also_a_word_stays_in_its_name():
    """ "Said" before a surname is a first name: never trimmed from the name withheld."""
    for text, sent in {
        "Said Vantrell and Zorgwell 2020 found high dropout.": f"{W} and {W} 2020 found high dropout.",
        "In Said Vantrell and Zorgwell 2020, dropout rose.": f"{W} and {W} 2020, dropout rose.",
        "According to Said Vantrell and Zorgwell 2020, dropout rose.": (
            f"According to {W} and {W} 2020, dropout rose."
        ),
        "What did Said Vantrell and Zorgwell 2020 find?": f"What did {W} and {W} 2020 find?",
        "Data from Said Vantrell and Zorgwell 2020": f"Data from {W} and {W} 2020",
    }.items():
        for cleaned in (privacy.for_ai(text), privacy.for_download(text), privacy.clean_question(text)):
            assert cleaned == sent, (text, cleaned)
    assert privacy.clean_question("Show Said Vantrell and Zorgwell 2020") == f"{W} and {W} 2020"
    assert privacy.for_ai("Show Said Vantrell and Zorgwell 2020") == f"{W} and {W} 2020"


@pytest.mark.django_db
def test_a_pair_written_by_its_authors_loses_both_names():
    """After "by" or an author's word, a pair before a year is a citation's: an exam's or a tool's word that is
    a family name too ("Brevet", "Kobo") and a town's spelling ("Houla", "Naameh") go with the other name."""
    for text, sent in {
        "A paper by Brevet and Zorgwell 2020 shows high dropout.": (
            f"A paper by {W} and {W} 2020 shows high dropout."
        ),
        "Edited by Kobo and Vantrell 2021.": f"Edited by {W} and {W} 2021.",
        "A study by Morning and Quenby 2019 shows high dropout.": f"A study by {W} and {W} 2019 shows high dropout.",
        "Authors: Brevet and Zorgwell 2020.": f"Authors: {W} and {W} 2020.",
        "Brevet and Zorgwell 2020 make the case that fees matter.": (
            f"{W} and {W} 2020 make the case that fees matter."
        ),
        "A paper by Naameh and Jordan 2020 shows high dropout.": f"A paper by {W} and {W} 2020 shows high dropout.",
        "Edited by Houla and Tyre 2021.": f"Edited by {W} and Tyre 2021.",
        "The report by Qana and Tyre 2021 shows high dropout.": f"The report by {W} and Tyre 2021 shows high dropout.",
        "Naameh and Tyre 2021 make the case that fees matter.": f"{W} and Tyre 2021 make the case that fees matter.",
    }.items():
        for cleaned in (privacy.for_ai(text), privacy.for_download(text), privacy.clean_question(text)):
            assert cleaned == sent, (text, cleaned)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "text",
    [
        "Summarize Beirut and Tripoli 2023",
        "Summarise Beirut and Tripoli 2023",
        "Describe Akkar and Tripoli 2023 dropout",
        "Map Akkar and Tripoli 2023 schools",
        "Contrast Akkar and Beirut 2023 attendance",
        "Analyse Akkar and Tripoli 2023",
        "Count Akkar and Tripoli 2023 schools",
        "Chart Akkar and Tripoli 2023",
        "Explain Akkar and Tripoli 2023 enrolment",
    ],
)
def test_a_typed_request_before_a_pair_of_places_keeps_them(text):
    """A request's verb that opens a typed question is no name: the places after it stay (the verb in lower
    case), and the final check reads the question as it was kept."""
    sent = text[0].lower() + text[1:]
    assert privacy.clean_question(text) == sent
    assert privacy.typed_for_ai(text) == sent
    assert privacy.leftover(f"Question: {sent}") == []


# (final review) a request's verb before a citation's form, a title's full stop before it, "&", or a place's
# word that is a given or a family name too: the verb is read with the name, as every commit of the chain read it
REQUESTS_CITED = {
    "Summarise Tyre et al. (2020)": f"{W} et al. (2020)",
    "Summarize French, Q. (2019)": f"{W} (2019)",
    "Summarise Tyre, Q. (2020)": f"{W} (2020)",
    "Summarise West and Jordan (2020)": f"{W} and {W} (2020)",
    "Summarise Halba and Tyre (2019)": f"{W} and Tyre (2019)",
    "Summarise West et al. (2019) on attendance in Akkar": f"{W} et al. (2019) on attendance in Akkar",
    "Explain French et al. 2019 findings on attendance": f"{W} et al. 2019 findings on attendance",
    "Describe West (2019) on dropout": f"{W} (2019) on dropout",
    "Summarise West et al.": f"{W} et al.",
    "Thanks. Summarise French et al. (2019).": f"Thanks. {W} et al. (2019).",
    "Summarise France & Jordan 2020": f"{W} & {W} 2020",
    "Dr. Show Haddad 2020": f"{W} 2020",
    "Explain Beirut et al. (2018)": f"{W} et al. (2018)",
    "Compare Georgia and Chad 2020": f"{W} and {W} 2020",
    "Compare West and Jordan 2020": f"{W} and {W} 2020",
    "Show West and Tyre 2023": f"{W} and Tyre 2023",
    "Show Jordan and Tyre 2023": f"{W} and Tyre 2023",
    "Show North and Tyre 2023": f"{W} and Tyre 2023",
    "Summarise France and Jordan 2020": f"{W} and {W} 2020",
    "Summarise Jordan and West 2020 who interviewed 40 teachers": f"{W} and {W} 2020 who interviewed 40 teachers",
    "Summarise Georgia and Chad 2020's findings": f"{W} and {W} 2020's findings",
}


@pytest.mark.django_db
@pytest.mark.parametrize(("text", "sent"), REQUESTS_CITED.items())
def test_a_typed_request_before_a_citation_is_read_with_its_name(text, sent):
    """A request's verb is read apart only before places listed before a bare year ("Show Beirut and Tyre
    2023"): before "et al.", a year in brackets, an initial, "&", after a title's full stop, or before a place's
    word that is a given or a family name too ("Jordan", "West", "Georgia"), it is read with the name, which
    goes, as in a record's text."""
    assert privacy.typed_for_ai(text) == sent
    assert privacy.clean_question(text) == sent
    assert privacy.typed_for_ai(text, KM_PEOPLE) == sent


@pytest.mark.django_db
def test_a_typed_request_before_places_and_a_bare_year_stays():
    """Places only, joined by commas or "and", before a bare year: the verb is read apart (in lower case)."""
    for text in ("Show Beirut and Tyre 2023", "Compare Akkar, Halba and Tripoli 2023 schools"):
        sent = text[0].lower() + text[1:]
        assert privacy.typed_for_ai(text) == sent, text
        assert privacy.clean_question(text) == sent, text
    for text in ("Summarize Akkar 2022", "Compare Lebanon and Syria 2023"):  # nothing to read apart
        assert privacy.clean_question(text) == text, text


@pytest.mark.django_db
def test_an_and_pair_judges_each_name_on_its_own():
    """ "&" or "and": an organisation or a place stays, the other name goes unless it is itself a place written
    as one; a word that is a first name too ("Said") goes; a title ("the Fakeword and Child Development Survey
    2023") stays."""
    for text, sent in {
        "UNICEF & Vantrell 2020 found high dropout.": f"UNICEF & {W} 2020 found high dropout.",
        "Said & Zorgwell 2020 found high dropout.": f"{W} & {W} 2020 found high dropout.",
        "UNICEF & Morvane 2020 found high dropout.": f"UNICEF & {W} 2020 found high dropout.",
        "Morvane & UNICEF 2020 found high dropout.": f"{W} & UNICEF 2020 found high dropout.",
        "Data from UNICEF & Morvane 2020 show high dropout.": f"Data from UNICEF & {W} 2020 show high dropout.",
        "Said and Zorgwell 2020 found high dropout.": f"{W} and {W} 2020 found high dropout.",
    }.items():
        for cleaned in (
            privacy.for_ai(text),
            privacy.for_download(text),
            privacy.clean_question(text),
            privacy.for_ai(text, KM_PEOPLE, frozenset({"fakedoe"})),
        ):
            assert cleaned == sent, (text, cleaned)
    assert privacy.for_ai("the Fakeword and Child Development Survey 2023") == (
        "the Fakeword and Child Development Survey 2023"
    )
    assert (
        privacy.for_ai("Quenby and Child Development Survey 2023") == f"{W} and Child Development Survey 2023"
    )


def test_a_list_after_by_loses_every_name_once_one_is_withheld():
    """S4 ("studies by Doe, Roe and Poe"): a name already withheld (the document's own author) is an item of
    the list, so the KM Database's other surnames in it go too."""
    for text, sent in {
        "Studies by Vantrell, Zorgwell and Fakedoe show high dropout.": (
            f"Studies by {W}, {W} and {W} show high dropout."
        ),
        "Studies by Fakedoe, Vantrell and Zorgwell show high dropout.": (
            f"Studies by {W}, {W} and {W} show high dropout."
        ),
        "Studies by Vantrell, Fakedoe and Zorgwell show high dropout.": (
            f"Studies by {W}, {W} and {W} show high dropout."
        ),
        "Studies by Vantrell and Fakedoe show high dropout.": f"Studies by {W} and {W} show high dropout.",
        "Reports from Vantrell, Zorgwell and Fakedoe found high dropout.": (
            f"Reports from {W}, {W} and {W} found high dropout."
        ),
    }.items():
        for cleaned in (
            privacy.for_ai(text, KM_PEOPLE, frozenset({"fakedoe"})),
            privacy.for_download(text, KM_PEOPLE, frozenset({"fakedoe"})),
        ):
            assert cleaned == sent, (text, cleaned)
    assert privacy.for_ai("Funding from UNICEF and Lebanon reached schools.") == (
        "Funding from UNICEF and Lebanon reached schools."
    )


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        ("Quenby Q, Morvane J. Dropout in Lebanon. 2019.", f"{W} Q, {W} J. Dropout in Lebanon. 2019."),
        (
            "Quenby QM, Morvane JR, Brannock K. Fake report on schools. Beirut; 2021.",
            f"{W} QM, {W} JR, {W} K. Fake report on schools. Beirut; 2021.",
        ),
        (
            "Evidence drawn from Quenby Q, Morvane J. Fake report on schools. Beirut; 2021.",
            f"Evidence drawn from {W} Q, {W} J. Fake report on schools. Beirut; 2021.",
        ),
        (
            "Quenby Q, Morvane J, et al. Fake title. Beirut: Example Press; 2020.",
            f"{W} Q, {W} J, et al. Fake title. Beirut: Example Press; 2020.",
        ),
        # kept: a grade's, a phase's or an annex's letters
        ("Grade A, Grade B. In 2020 fees rose.", "Grade A, Grade B. In 2020 fees rose."),
        ("Phase I, Phase II. Fees rose in 2020.", "Phase I, Phase II. Fees rose in 2020."),
        ("Quenby Q, Morvane J. Fake title.", f"{W} Q, {W} J. Fake title."),
    ],
)
def test_a_vancouver_list_of_authors_is_withheld_without_a_km_surname(text, sent):
    """A reference written the Vancouver way with two authors or more ("Quenby Q, Morvane J. Title. 2019."):
    every surname goes, whatever the KM Database's authors, with or without a year."""
    assert privacy.for_ai(text) == sent
    assert privacy.for_download(text) == sent
    assert privacy.leftover(sent) == []


@pytest.mark.django_db
def test_a_typed_question_that_asks_what_a_name_and_a_year_said_loses_the_name():
    """A name before a bare year after "did", "does", "By", "according to" or "per" is a citation's, in what a
    person typed and in a record's text alike; a place, an organisation, an event, an exam's or a tool's word
    stays."""
    for text, sent in {
        "What did Quenby 2019 say about dropout?": f"What did {W} 2019 say about dropout?",
        "Did Morvane 2020 find that fees rose?": f"Did {W} 2020 find that fees rose?",
        "What does Quenby 2019 say about fees?": f"What does {W} 2019 say about fees?",
        "By Quenby 2019, how many girls left school?": f"By {W} 2019, how many girls left school?",
    }.items():
        assert privacy.clean_question(text) == sent, text
        assert privacy.typed_for_ai(text) == sent, text
        assert privacy.for_ai(text) == sent, text
    for text in (
        "What did Lebanon 2020 report about fees?",
        "What did UNICEF 2021 say about dropout?",
        "How does Akkar 2023 compare with Beirut?",
        "How did Covid 2020 change enrolment?",
        "What did Brevet 2023 show about fees?",
    ):
        assert privacy.clean_question(text) == text, text
        assert privacy.for_ai(text) == text, text


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        (
            "Vantrell, Zorgwell and Fakedoe (2019) found high dropout.",
            f"{W}, {W} and {W} (2019) found high dropout.",
        ),
        (
            "Vantrell, Zorgwell and Quenby 2019 found high dropout.",
            f"{W}, {W} and {W} 2019 found high dropout.",
        ),
        (
            "Brindlecombe, Quenby and Marlowfake 2019 found high dropout.",
            f"{W}, {W} and {W} 2019 found high dropout.",
        ),
        (
            "Vantrell, Brindlecombe, Zorgwell and Quenby (2019) found high dropout.",
            f"{W}, {W}, {W} and {W} (2019) found high dropout.",
        ),
        (
            "Brindlecombe, Quenby, and Marlowfake (2019) found high dropout.",
            f"{W}, {W}, and {W} (2019) found high dropout.",
        ),
        # kept: places, and a place written as a place
        (
            "Tripoli, Akkar and Beirut (2019) saw high dropout.",
            "Tripoli, Akkar and Beirut (2019) saw high dropout.",
        ),
        (
            "Dropout in Saida, Qana and Houla 2024 was high.",
            "Dropout in Saida, Qana and Houla 2024 was high.",
        ),
        ("In Lebanon, Akar and Naameh 2020 found dropout.", f"In Lebanon, {W} and {W} 2020 found dropout."),
    ],
)
def test_every_name_of_a_list_of_three_authors_or_more_is_withheld(text, sent):
    """C3 and C5 take two names; the names listed before them with commas are the same citation's."""
    assert privacy.for_ai(text) == sent
    assert privacy.for_download(text) == sent


# ---------------------------------------------------- the written spec (K1-K3): KM names, a few texts per rule
# K1: a name token in a request's or a citation's shape, a given name or an initial before a request's verb,
# a title before a verb: withheld on every KM path (a record's, a download's, a typed question's), whatever the
# word also is (a place: "Jordan", "Costa", "Arab", "West"; a word: "Said")
SPEC_K1 = {
    "Show Jordan and Tyre 2023": "jordan",
    "Show Tyre and Costa 2023": "costa",
    "Compare Asia and Costa 2020": "costa",
    "Rank Tyre and Quenby 2021 dropout": "quenby",
    "Summarise Costa and Tyre 2020 who interviewed 40 teachers": "costa",
    "Arab et al. (2021)": "arab",
    "See Tyre and Jordan (2019)": "jordan",
    "Quenby & West 2020 found high dropout.": "west",
    "West, Q. (2019)": "west",
    "By Said 2019, dropout rose.": "said",
    "By Quenby 2021, how many passed?": "quenby",
    "What did Quenby 2019 say?": "quenby",
    "What did Jordan 2020 say about dropout?": "jordan",
    "According to Said 2021": "said",
    "According to Costa 2019, dropout rose.": "costa",
    "Quenby, Arab and Said (2018) argue that fees rose.": "arab said",
    "See Tyre and Halba 2019; Quenby 2020": "quenby",
    "Quenby AB, Zayna C.": "quenby zayna",
    "Jordan AB, Tyre C.": "jordan",
    "Zayna Q. Show Tyre and Halba 2020": "zayna",
    "Mira Show Tyre and Halba 2020": "mira",
    "Q. Rank Halba and Tyre 2020": "rank",
    "Q. R. Show Beirut and Byblos 2018": "show",
    "Amb. Show Haddad 2023": "show",
    "Pr. Summarize Haddad 2020": "summarize",
    "Sen. Rank Tyre and Halba 2020": "rank",
    "Dr.\xa0Show Tyre and Halba 2020": "show",
    "Mr.\xa0Summarise Tyre and Akkar 2024": "summarise",
    # a K2 shape with a citation's tail is read as the rule reads it (the verb with the town)
    "Summarise Halba and Tyre 2019: 4-5": "summarise halba",
    "See Tyre and Halba 2019 et al.": "see tyre",
    "Compare Tyre and Akkar 2020 (p. 4)": "compare tyre",
    "Compare Zahle and Tripoli 2019 who found high dropout": "compare zahle",
    # (follow-up 1) the same shapes with new values: towns joined by a comma, a request's lead-in, a citation's
    # names listed with a comma, a Vancouver surname of two words, other titles and a thin no-break space
    "Thanks. Plot Lina, Nabatieh 2017": "lina",
    "Please contrast Jounieh, Zorgwell 2025 please": "zorgwell",
    "count Hill, Chtaura 2019 findings": "hill",
    "Explain Hill, Quenvell (2021), p. 4": "hill quenvell",
    "Describe Morvane, Tripoli (2020) argue that fees rose.": "morvane",
    "Compare Wadi, Beirut (2025).": "wadi",
    "Tavistrom AB, Bou Nassar D.": "tavistrom nassar",
    "Green AB, Hill QK, Bint Jbeil RJ.": "green hill",
    "Georgia AB, Quenvell QK, Abi Saab RJ.": "georgia quenvell saab",
    "Amb.\u202fPlot Morvane 2023": "plot morvane",
    "Hon.\u202fExplain Khalil 2017": "explain khalil",
}
# K1 on the "+people" paths: the KM Database's surnames (town spellings among them) in the same shapes
SPEC_K1_PEOPLE = {
    "Show Tyre and Naameh 2023": "naameh",
    "Show Saida and Halba 2023": "saida",
    "By Akar 2022, dropout rose.": "akar",
    "What did Doe 2018 say": "doe",
    "Kousba et al. (2020) found high dropout.": "kousba",
    "Naameh AB, Hasroun C.": "naameh hasroun",
}
SPEC_PEOPLE = privacy.People(
    names=frozenset({"q fakedoe", "r naameh"}),
    surnames=frozenset({"naameh", "hasroun", "kousba", "akar", "saida", "fakedoe", "doe", "roe"}),
    own={"INV-0001": frozenset({"naameh", "fakedoe"})},
)


def _km_paths(text: str, people: privacy.People = privacy.NOBODY) -> dict[str, str]:
    own = frozenset({"naameh", "fakedoe"}) if people is not privacy.NOBODY else frozenset()
    return {
        "for_ai": privacy.for_ai(text, people, own),
        "for_download": privacy.for_download(text, people, own),
        "clean_question": privacy.clean_question(text, people),
        "typed_for_ai": privacy.typed_for_ai(text, people),
    }


def _words(text: str) -> list[str]:
    return [w.casefold() for w in re.findall(r"[^\W_]+", text.replace(W, " "))]


@pytest.mark.django_db
@pytest.mark.parametrize(("text", "names"), SPEC_K1.items())
def test_k1_a_name_in_a_request_or_a_citation_is_withheld_on_every_km_path(text, names):
    for people in (privacy.NOBODY, SPEC_PEOPLE):
        for path, cleaned in _km_paths(text, people).items():
            for name in names.split():
                assert _words(cleaned).count(name) < _words(text).count(name), (path, name, cleaned)


@pytest.mark.django_db
@pytest.mark.parametrize(("text", "names"), SPEC_K1_PEOPLE.items())
def test_k1_a_km_surname_in_a_request_or_a_citation_is_withheld(text, names):
    for path, cleaned in _km_paths(text, SPEC_PEOPLE).items():
        for name in names.split():
            assert name not in _words(cleaned), (path, name, cleaned)


# K2: a plain request about places (a request's verb at the start of the text, "What about", "Any data on",
# "How did ... compare?", "Dropout in" or nothing; two or three of the main towns joined by "," or "and"; a
# bare year; nothing, "?", "." or a measure's word after it): every town stays (the verb may go to lower case),
# and what is kept reads the same to every later check (a record's rule, the final check, a download)
SPEC_K2 = [
    "Show Beirut and Tyre 2023",
    "Compare Chtoura and Zahle 2022",
    "compare Chtoura and Zahle 2022",
    "Show Chtoura and Zahle 2023",
    "What about Chtoura and Zahle 2022?",
    "How did Chtoura and Zahle 2022 compare?",
    "Chtoura and Zahle 2022 enrolment",
    "Any data on Anjar and Baalbek 2023?",
    "Compare Majdal Anjar and Zahle 2022",
    "Compare Chtoura, Zahle and Baalbek 2022",
    "Chtoura, Zahle and Baalbek 2022 enrolment",
    "Rank Akkar, Minieh, and Halba 2024 dropout rates?",
    "Contrast Jbeil and Batroun and Aley 2021 attendance rates",
    "Dropout in Chtaura, Jbeil and Byblos 2024",
    "Plot Jounieh, Hermel 2020.",
    "What about Majdal Anjar and Tyre 2023?",
    # (follow-up 1) any town of the place lists that is no family name's, more request verbs and openers, more
    # measures after the year
    "Compare Majdal Anjar and Amchit 2024",
    "Show Bikfaya and Hermel 2021.",
    "Rank Qobayat and Anjar 2019?",
    "How did Tyre, Tebnine and Aley 2020 compare?",
    "Visualise Chtoura and Tripoli 2022 pass rates",
    "Display Zahle and Baalbek 2023 attendance rate",
    "Data on Jbail and Batroun 2021 repetition rates",
    "Enrolment in Akkar and Anjar 2020",
]


def _towns(text: str) -> list[str]:
    return [
        w for w in _words(text) if w not in {"show", "compare", "what", "about", "how", "did", "any", "data"}
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("text", SPEC_K2)
def test_k2_a_plain_request_about_places_keeps_every_town(text):
    people = privacy.People(surnames=frozenset({"vantrell", "naameh"}))
    for who in (privacy.NOBODY, people):
        for cleaned in (privacy.clean_question(text, who), privacy.typed_for_ai(text, who)):
            assert cleaned.casefold() == text.casefold(), cleaned
            assert privacy.for_ai(cleaned, who) == cleaned
            assert privacy.leftover(f"Question: {cleaned}", who) == []
            assert privacy.typed_for_ai(cleaned, who) == cleaned


@pytest.mark.django_db
def test_k2_is_that_shape_only():
    """A town that is one of the KM Database's surnames, a name, a citation's tail, "See", or anything before
    the verb: the rule reads the text as it reads any other."""
    zahle = privacy.People(surnames=frozenset({"zahle"}))
    assert "Zahle" not in privacy.clean_question("Compare Chtoura and Zahle 2022", zahle)
    for text in (
        "Compare Chtoura and Quenvell 2022",
        "See Tyre and Halba 2019",
        "List Tyre and Halba 2019 findings",
        "Analyse Tripoli and Tyre 2021 paper on dropout",
        "Show Tyre and Halba 2019 as reported by Quenby",
        "Thanks. Show Chtoura and Zahle 2023",
        "Visits to Chtoura and Tyre 2023",
        "Compare Chtoura & Zahle 2022",
        "Chtoura and Zahle (2022)",
        # (follow-up 1) a town spelt as a family name ("Houla", "Qana"; people.TOWN_WORDS) or a direction's word
        "Show Houla and Tyre 2023",
        "Compare Qana and Zahle 2023",
        "Summarise North and South 2020",
    ):
        assert W in privacy.clean_question(text), text
        assert W in privacy.typed_for_ai(text), text
    # a record's text: a request's verb written with a capital is read with the town after it
    assert privacy.for_ai("Compare Chtoura and Zahle 2022") == f"{W} and Zahle 2022"


# (after the written spec) the same plain request with a subject's words after the year ("out of school", "child
# labour", "by gender", "projects"), as people ask the KM Database: every town stays (the verb in lower case), as
# at 85a35e4, so that the question kept, the one sent to the AI and a follow-up's places all hold them
REQUESTS_WITH_A_SUBJECT = [
    "Compare Beirut and Bourj Hammoud 2023 out of school",
    "Compare Baalbek, Hermel and Arsal 2024 child labour",
    "Compare Sidon and Tyre 2025 out of school",
    "Compare Bekaa and Akkar 2024 out of school children",
    "Show Beirut and Tyre 2023 by gender",
    "List Beirut and Tyre 2023 projects",
    "Compare Tyre and Nabatieh 2025 out of school",
    "Show Halba and Zahle 2022 child labour",
    "Compare Beirut and Tripoli 2024 learning outcomes",
    "Rank Akkar, Minieh and Halba 2024 out of school rates?",
]


@pytest.mark.django_db
@pytest.mark.parametrize("text", REQUESTS_WITH_A_SUBJECT)
def test_a_plain_request_about_places_with_a_subject_after_the_year_keeps_every_town(text):
    people = privacy.People(surnames=frozenset({"vantrell", "naameh"}))
    sent = text[0].lower() + text[1:]
    for who in (privacy.NOBODY, people):
        for cleaned in (privacy.clean_question(text, who), privacy.typed_for_ai(text, who)):
            assert cleaned == sent, cleaned
            assert privacy.for_ai(cleaned, who) == cleaned
            assert privacy.leftover(f"Question: {cleaned}", who) == []
            assert privacy.typed_for_ai(cleaned, who) == cleaned


@pytest.mark.django_db
def test_a_request_with_a_subject_after_the_year_is_still_read_with_a_name_or_a_citations_words():
    """Only towns, and only a subject's words after the year: a name among the places, a title before the
    verb, a citation's word after the year ("who found", "findings", "as reported", "paper") or one of the KM
    Database's surnames still has the rule read the text as it reads any other."""
    for text in (
        "Compare Jordan and Tyre 2023 out of school",
        "Show Quenby and Tyre 2023 by gender",
        "Compare North and Tyre 2023 out of school",
        "Dr. Compare Beirut and Tyre 2023 out of school",
        "Summarise Tyre and Halba 2019 who found high dropout",
        "List Tyre and Halba 2019 findings on dropout",
        "Analyse Tripoli and Tyre 2021 paper on dropout",
        "Show Tyre and Halba 2019 as reported by schools",
        "Compare Tyre and Halba 2019 et al",
    ):
        assert W in privacy.clean_question(text), text
        assert W in privacy.typed_for_ai(text), text
    saida = privacy.People(surnames=frozenset({"saida"}))
    for text in ("Compare Tyre and Saida 2025 out of school", "Show Saida and Tyre 2025 out of school"):
        assert "saida" not in privacy.clean_question(text, saida).casefold(), text
        assert "saida" not in privacy.typed_for_ai(text, saida).casefold(), text


# K3: "By", "According to", "Per", "Did", "Does" or "What did", then an exam's, a tool's or a holiday's word and
# a year: that word stays (a holiday only in this shape; a name there still goes)
SPEC_K3 = [
    "By Brevet 2019, how many passed?",
    "According to Kobo 2023, how many children were reached?",
    "According to ActivityInfo 2023, how many children were reached?",
    "Per Midterm 2022 data",
    "Did Baccalaureate 2023 pass rates rise?",
    "Does KoboToolbox 2021 show higher dropout?",
    "What did Brevet 2023 show about fees?",
    "What did Easter 2023 attendance look like?",
    "By Christmas 2020, dropout rose.",
    "Per Easter 2024, how many passed?",
    # (follow-up 1) the lead in lower case
    "by Brevet 2023, dropout rose.",
    "per Kobo 2022, dropout rose.",
    "by Christmas 2023, dropout rose.",
    "per Fieldwork 2019",
]


@pytest.mark.django_db
@pytest.mark.parametrize("text", SPEC_K3)
def test_k3_an_exams_a_tools_or_a_holidays_word_after_by_or_did_stays(text):
    for who in (privacy.NOBODY, SPEC_PEOPLE):
        assert privacy.clean_question(text, who) == text
        assert privacy.typed_for_ai(text, who) == text
        assert privacy.for_ai(text, who) == text
        assert privacy.leftover(text, who) == []


@pytest.mark.django_db
def test_k3_a_holiday_is_kept_in_that_shape_only():
    for text, sent in {
        "Christmas et al. (2019) found high dropout.": f"{W} et al. (2019) found high dropout.",
        "Source: Easter 2023.": f"Source: {W} 2023.",
        "By Quenby 2019, how many passed?": f"By {W} 2019, how many passed?",
        # (follow-up 1) "by" after a word that is no opening one: a citation's author
        "A paper by Brevet 2020 shows high dropout.": f"A paper by {W} 2020 shows high dropout.",
    }.items():
        assert privacy.for_ai(text) == sent, text
        assert privacy.clean_question(text) == sent, text
