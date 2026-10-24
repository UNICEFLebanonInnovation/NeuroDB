"""Names of people in the KM Database's texts (SPEC_km §4.4, the parts its downloads use): citations and
interview speakers withheld without any list of names, a document's own authors withheld in its texts, the
KM Database's own authors and names after a title withheld anywhere, links and numbers kept; and the final
check. Fake strings only."""

from __future__ import annotations

import pytest

from neurodb.km import privacy
from neurodb.km.privacy import NAME_WITHHELD as W

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
    from neurodb.watch import people as watch_people

    def refused(*args, **kwargs):
        raise AssertionError("NeuroDB's list of the people it knows was read")

    monkeypatch.setattr(watch_people, "known_names", refused)
    assert (
        privacy.for_download("as Fakefirst Fakelast wrote", privacy.NOBODY) == "as Fakefirst Fakelast wrote"
    )
    assert privacy.for_download("as Fakefirst Fakelast wrote", PEOPLE) == f"as {W} wrote"
    assert privacy.leftover("as Fakefirst Fakelast wrote", privacy.NOBODY) == []
