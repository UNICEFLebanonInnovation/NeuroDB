"""The author rule of the KM Database's downloads (SPEC_km §3.7, the user's decision "on screen only, not in
downloads"): organisations are named, individual authors counted and never named, piece by piece, privacy
first; the administrators' corrections; the KM Database's own people (never NeuroDB's list of the people it
knows). Fake strings and fake generated workbooks only."""

from __future__ import annotations

import pytest

from neurodb.km import authors, privacy
from neurodb.km.models import KMOrganisation
from neurodb.watch import people as watch_people
from tests.km import factory


@pytest.mark.parametrize(
    ("entry", "shown"),
    [
        ("Fakeorg Institute; Fakeorg University", "Fakeorg Institute; Fakeorg University"),
        ("Jane Doe; John Roe", "Authors: 2"),
        ("Fakeorg Institute; Doe, J.; Roe, R. et al.", "Fakeorg Institute + 2 authors"),
        ("Fakeorg Institute; Doe, J.", "Fakeorg Institute + 1 author"),
        ("Doe, J.", "Authors: 1"),  # a surname and its initial: one person
        ("Doe J, Roe K", "Authors: 2"),  # Vancouver style: two
        ("Jane Doe, Fakeorg University", "Fakeorg University + 1 author"),
        ("J. Doe (Fakeorg Institute)", "Fakeorg Institute + 1 author"),
        ("DOE Jane", "Authors: 1"),
        ("Jane DOE", "Authors: 1"),
        ("Fake Org (FO)", "Authors: 1"),  # kept whole (its acronym), and not taken for an organisation
        ("Fakeorg Agency (FA)", "Fakeorg Agency (FA)"),
        ("Ministry of Fake Affairs (MOFA)", "Ministry of Fake Affairs (MOFA)"),
        ("Fakeorg Institute for Research and Development", "Fakeorg Institute for Research and Development"),
        ("Fakeorg University, Beirut", "Fakeorg University, Beirut"),
        ("FAKEORG", "FAKEORG"),
        ("FAKEORG Lebanon", "FAKEORG Lebanon"),  # shaped like "UNICEF Lebanon"
        ("FAKEORG, FKO", "FAKEORG; FKO"),
        ("FaSyR Fakeworks", "FaSyR Fakeworks"),
        ("Fakefirst Fakelast", "Authors: 1"),  # an unknown name of two words: a person
        ("Jane Q. Doe and the Fakeorg Working Group", "the Fakeorg Working Group + 1 author"),
        ("Jane Doe of Fakeorg Systems International", "Fakeorg Systems International + 1 author"),
        ("Jane Fakeone-FakeDoe", "Authors: 1"),  # a hyphenated name is not an acronym
        ("Jane FAKE-DOE", "Authors: 1"),
        ("Not reported", ""),
        ("Unknown; n/a", ""),
        ("", ""),
        ("fake topic words", "Authors: 1"),  # neither a person nor an organisation: counted, never named
        ("Fakeorg Institute; fake topic words", "Fakeorg Institute + 1 author"),
        ("Fakeorg Institute\nDoe, J. | Roe, R.", "Fakeorg Institute + 2 authors"),
        ("Fakeorg Institute; Fakeorg Institute", "Fakeorg Institute"),  # named once
        ("Fake​org Institute", "Fakeorg Institute"),  # an invisible character is taken out
    ],
)
def test_the_rule_on_fake_entries(entry, shown):
    assert authors.for_downloads(entry) == shown


def test_placeholders_and_topics_give_no_name_form():
    for entry in ("Not reported", "fake topic words", "Anonymous"):
        found = authors.classify(entry)
        assert found.people == [] and found.orgs == []
    assert authors.classify("Doe, J.").people == ["Doe J"]


def test_the_safety_net_counts_an_organisation_that_holds_a_name():
    people = privacy.People(names=frozenset({"jane doe"}))
    assert authors.for_downloads("Jane Doe Fakeorg Foundation", people=people) == "Authors: 1"
    assert authors.for_downloads("Jane Doe Fakeorg Foundation") == "Jane Doe Fakeorg Foundation"
    assert authors.for_downloads("Dr Fakename Foundation") == "Authors: 1"  # a name after a title


def test_surnames():
    assert [
        authors.surname(p) for p in ("Doe J.", "Doe, J.", "DOE Jane", "Jane Doe", "J. Doe", "Al Fakeh")
    ] == [
        "doe",
        "doe",
        "doe",
        "doe",
        "doe",
        "fakeh",
    ]
    assert authors.surname("Jo Li") == ""  # under three letters
    assert authors.surname("Jane Lebanon") == ""  # a place
    assert authors.surname("Jane March") == ""  # a month


@pytest.mark.django_db
def test_the_corrections_of_each_kind_change_downloads_at_once():
    version = factory.load(factory.workbook())
    known, people = authors.context(version)
    assert (
        authors.for_downloads("Fakeorg Institute; Doe, J.", known, people) == "Fakeorg Institute + 1 author"
    )
    KMOrganisation.objects.create(name="Fakeorg Institute", kind="person")
    known, people = authors.context(version)  # the list changed: read again
    assert known == {"fakeorg institute": "person"}
    assert authors.for_downloads("Fakeorg Institute; Doe, J.", known, people) == "Authors: 2"
    assert "fakeorg institute" in people.names  # a person's name forms are withheld in the texts
    row = KMOrganisation.objects.get()
    row.kind = "words"
    row.save()
    known, people = authors.context(version)
    assert authors.for_downloads("Fakeorg Institute; Doe, J.", known, people) == "Authors: 1"
    assert "fakeorg institute" not in people.names
    KMOrganisation.objects.create(name="Fakefirst Fakelast.", kind="organisation")  # its full stop is dropped
    known, people = authors.context(version)
    assert known["fakefirst fakelast"] == "organisation"
    assert authors.for_downloads("Fakefirst Fakelast (Beirut); Doe, J.", known, people) == (
        "Fakefirst Fakelast (Beirut) + 1 author"
    )


@pytest.mark.django_db
def test_the_people_of_a_version_are_its_own():
    entity = "Fakeorg Institute, Jane Doe; DOE Fakefirst; Roe, R."
    edits = {("documents", 2, "Publishing Entity / Authors"): entity}
    version = factory.load(factory.workbook(edits=edits))
    people = authors.people(version)
    assert people is not None and isinstance(people, privacy.People)
    assert {"jane doe", "doe fakefirst", "roe r", "doe j"} <= people.names
    assert {"doe", "roe"} <= people.surnames
    assert people.own[_document_pk("TST-0001")] >= {"doe", "roe"}
    assert (
        people.own.get(_document_pk("TST-0002"), frozenset()) == frozenset()
    )  # "Fakeorg University": no person
    assert not people.names & watch_people.known_names(refresh=True)  # never NeuroDB's own list
    assert authors.people(None) is privacy.NOBODY or authors.people(None).names == frozenset()


def _document_pk(record_id: str) -> int:
    from neurodb.km.models import KMDocument

    return KMDocument.objects.get(record_id=record_id).pk


@pytest.mark.django_db
def test_the_people_are_read_once_until_the_corrections_change(django_assert_num_queries):
    version = factory.load(factory.workbook())
    authors.context(version)
    with django_assert_num_queries(1):  # the corrections' count and latest change
        authors.context(version)
    KMOrganisation.objects.create(name="Fakeorg Institute", kind="person")
    with django_assert_num_queries(3):  # read again: the corrections, then both sheets' authors
        authors.context(version)


@pytest.mark.django_db
def test_the_summary_and_the_strings_named_as_organisations():
    edits = {
        ("documents", 2, "Publishing Entity / Authors"): "Doe, J.; Roe, R.",
        ("documents", 3, "Publishing Entity / Authors"): "Fakeorg Agency; Poe, K.",
        ("documents", 4, "Publishing Entity / Authors"): "Not reported",
    }
    version = factory.load(factory.workbook(documents=4, findings=4, edits=edits))
    known, people = authors.context(version)
    assert authors.summary(known, people) == {"organisations": 1, "authors_only": 1, "mixed": 1, "empty": 1}
    named = authors.named_organisations(known, people)
    assert "Fakeorg Agency" in named and "Fakeorg Institute" in named  # the findings sheet's too
    assert not any("Doe" in n or "Roe" in n or "Poe" in n for n in named)


@pytest.mark.django_db
def test_author_entries_that_look_like_words_are_listed_and_warned():
    # an author entry that is also ordinary words of more than 20 findings
    edits = {("documents", 2, "Publishing Entity / Authors"): "Fakeword Phrase; Doe, J."}
    edits |= {("findings", n, "Finding Analysis"): "A fakeword phrase is common." for n in range(2, 25)}
    version = factory.load(factory.workbook(documents=2, findings=23, edits=edits))
    known, people = authors.context(version)
    assert authors.look_like_words(known, people) == ["Fakeword Phrase"]
    assert "1 author entry looks like words, not a name: check it on this page." in version.warnings
    KMOrganisation.objects.create(name="Fakeword Phrase", kind="words")
    known, people = authors.context(version)
    assert authors.look_like_words(known, people) == []
    assert "fakeword phrase" not in people.names
    edits = {key: value for key, value in edits.items() if key[1] <= 6}
    few = factory.load(factory.workbook(documents=2, findings=5, edits=edits), fewer_ok=True)
    assert not any("look like words" in w or "looks like words" in w for w in few.warnings)


@pytest.mark.parametrize(
    ("entry", "shown"),
    [
        # a person written inside an organisation part (the shape found in a real entry, with fake names)
        (
            "Fakeorg Institute; author Jane Fakename. Published in 'The Fake Monitor' with Fakeorg "
            "International under the FAKE project",
            "Fakeorg Institute; Fakeorg International under the FAKE project + 1 author",
        ),
        ("Report prepared by Jane Fakename for the Fakeorg Foundation", "the Fakeorg Foundation + 1 author"),
        ("Fakeorg Institute, led by Jane Fakename", "Fakeorg Institute + 1 author"),
        ("Jane Fakename (consultant) for Fakeorg Foundation", "Fakeorg Foundation + 1 author"),
        ("Fakeorg Institute; consultant", "Fakeorg Institute + 1 author"),  # a role alone: counted
        ("Jane Fakename - Fakeorg Consulting", "Fakeorg Consulting + 1 author"),
        ("Jane Fakename – Fakeorg Consulting", "Fakeorg Consulting + 1 author"),
        ("Jane Fakename/Fakeorg Consulting", "Fakeorg Consulting + 1 author"),
        ("Jane Fakename with Fakeorg Consulting", "Fakeorg Consulting + 1 author"),
        ("Fakeorg Research Team: Jane Fakename, John Fakeroe", "Fakeorg Research Team + 2 authors"),
        ("Fakeorg Institute and consultant Jane Fakename", "Fakeorg Institute + 1 author"),
        ("Fakeorg Country Office; consultant John Fakeroe", "Fakeorg Country Office + 1 author"),
        ("Fakeorg Foundation report by Fakename", "Fakeorg Foundation report + 1 author"),
        ("Fakeorg Institute funded by the Fakeorg Agency", "Fakeorg Institute; the Fakeorg Agency"),
        # a person whose name holds a word of an organisation
        ("Jane Cross", "Authors: 1"),
        ("John Banks", "Authors: 1"),
        ("Charity Fakename", "Authors: 1"),
        # organisations stay whole when no person stands in them
        ("Fakeland Red Cross", "Fakeland Red Cross"),
        ("Red Cross", "Red Cross"),
        ("Fakeorg - Lebanon", "Fakeorg - Lebanon"),
        ("FAKEORG/FKO", "FAKEORG/FKO"),
        ("Children with Fakeword Association", "Children with Fakeword Association"),
        ("Fakeorg Institute: Fakeland Office", "Fakeorg Institute: Fakeland Office"),
        ("Fakeorg Inc. Fakeland Office", "Fakeorg Inc. Fakeland Office"),
        ("Fakeorg Institute, n/a", "Fakeorg Institute"),
    ],
)
def test_a_person_inside_an_organisation_part_is_counted_never_named(entry, shown):
    assert authors.for_downloads(entry) == shown


def test_the_safety_net_counts_an_organisation_with_a_name_after_by():
    authors_ = authors.Authors(orgs=["Fakeorg Foundation report by Fakename"])
    assert authors.safe_orgs(authors_, privacy.NOBODY) == ([], 1)
    assert authors.safe_orgs(authors.Authors(orgs=["Fakeorg Foundation by UNICEF"]), privacy.NOBODY) == (
        ["Fakeorg Foundation by UNICEF"],
        0,
    )


def test_a_role_word_before_a_name_gives_the_persons_name_forms_and_surname():
    found = authors.classify("Fakeorg Country Office; consultant John Fakeroe")
    assert found.orgs == ["Fakeorg Country Office"] and found.people == ["John Fakeroe"]
    assert authors.classify("author Jane Fakename. Published in 'The Fake Monitor'").people == [
        "Jane Fakename"
    ]
    people = authors._gather([(1, "Fakeorg Country Office; consultant John Fakeroe")], {})
    assert "john fakeroe" in people.names and people.own[1] == {"fakeroe"}
    assert (
        privacy.for_download(
            "Done by Fakeorg Country Office and consultant John Fakeroe.", people, people.own[1]
        )
        == f"Done by Fakeorg Country Office and consultant {privacy.NAME_WITHHELD}."
    )


def test_a_citation_of_a_person_whose_name_holds_a_word_of_an_organisation_is_withheld():
    assert privacy.for_download("as Banks (2019) and Jane Cross (2020) found") == (
        f"as {privacy.NAME_WITHHELD} (2019) and {privacy.NAME_WITHHELD} (2020) found"
    )
    assert privacy.for_download("the Red Cross (2020) found") == "the Red Cross (2020) found"
