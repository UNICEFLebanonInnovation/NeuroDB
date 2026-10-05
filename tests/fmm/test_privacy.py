"""Monitoring insights' privacy rules: how a text is cleaned, which keys hold a person, what a key's
example shows, and how a team is shown (names only). The AI payload and chat parts arrive with them."""

import pytest

from neurodb.datamart import query
from neurodb.fmm import privacy, refresh
from neurodb.fmm.models import KeyProbe
from neurodb.watch import people

from .conftest import CANARIES, CANARY_TEXT, KEPT, LEAD, MEMBER, MEMBER_EMAIL

NAMES = frozenset({"karim canary", "rania canary", MEMBER_EMAIL})


# ------------------------------------------------------------------------------------------ clean
def test_clean_removes_people_contacts_and_links_and_keeps_the_figures():
    text, inserted = privacy.clean(CANARY_TEXT, 600, NAMES)
    for canary in CANARIES:
        assert canary not in text, canary
    for kept in KEPT:
        assert kept in text, kept
    assert "LEB/PCA2023597/PD2025123" in privacy.clean("Visit to LEB/PCA2023597/PD2025123.", 100, NAMES)[0]
    # Mrs Layla Saab, Karim Canary, the e-mail address, two phone numbers and the link
    assert inserted == 6
    assert text.count(people.NAME_WITHHELD) == 2 and text.count(privacy.PHONE_WITHHELD) == 2


@pytest.mark.parametrize(
    "phone",
    ["+961 3 123 456", "03-123456", "03 123 456", "00961 71 123 456", "+961-1-345678", "+44 20 7946 0958"],
)
def test_clean_withholds_phone_numbers(phone):
    assert privacy.clean(f"Call {phone} today", 100, frozenset())[0] == "Call [phone withheld] today"


@pytest.mark.parametrize(
    "kept", ["2026-05-11", "412 children", "2,800", "2500212345", "FM-2026-022", "LB1101"]
)
def test_clean_keeps_dates_numbers_and_references(kept):
    assert privacy.clean(kept, 100, frozenset())[0] == kept


def test_clean_cuts_puts_on_one_line_and_counts_only_what_it_put_in():
    text, inserted = privacy.clean("A  [name withheld]\nmet  Mr Haddad " + "x" * 200, 40, frozenset())
    assert text == ("A [name withheld] met [name withheld] " + "x" * 200)[:40] and len(text) == 40
    assert inserted == 1
    assert privacy.clean(None, 10) == ("", 0)
    assert privacy.clean(12, 10, frozenset()) == ("12", 0)


@pytest.mark.django_db
def test_clean_reads_the_names_neurodb_knows():
    from neurodb.datamart import models as dm

    dm.MonitoringFinding.objects.create(datamart_id=1, visit_lead=LEAD)
    people.forget()
    assert privacy.clean(f"Led by {LEAD}.", 100)[0] == "Led by [name withheld]."
    assert "rania canary" in privacy.names()


# ------------------------------------------------------------------------------------------ person keys
@pytest.mark.parametrize(
    "key",
    [
        "visit_lead",
        "team_members",
        "team_members.name",
        "person_responsible",
        "monitors",
        "teamMembers",
        "first_name",
        "username",
        "contacts",
        "unicef_manager",
        "unicef_manager.name",
        "comments",
        "assigned_to.name",
        "focal_points",
    ],
)
def test_keys_that_hold_a_person(key):
    assert privacy.person_like(key)


@pytest.mark.parametrize(
    "key",
    [
        "monitoring_activity",
        "monitoring_activity_id",
        "monitoring_activity.reference_number",
        "answer",
        "summary",
        "entity",
        "field_office",
        "location.name",
    ],
)
def test_keys_that_do_not_hold_a_person(key):
    assert not privacy.person_like(key)


@pytest.mark.parametrize(
    "key, names",
    [
        ("visit_lead", True),
        ("team_members.name", True),
        ("comments.user", True),
        ("comment_by", True),
        ("comments", False),
        ("note", False),
        ("review_note", False),
        ("narrative_finding", False),
    ],
)
def test_keys_whose_texts_are_names(key, names):
    """Names are learnt from these keys only; a note or a comment holds a person's words, not a name."""
    assert privacy.names_person(key) is names


def test_person_keys_agree_with_ask():
    """Ask's eTools tools and Monitoring insights drop the same keys."""
    for key in ("visit_lead", "team", "first_name", "contacts", "comments", "answer", "monitoring_activity"):
        assert privacy.person_like(key) == query._fm_person(key), key


# ------------------------------------------------------------------------------------------ examples
def test_examples_withhold_person_keys_and_clean_the_rest():
    assert privacy.example("field_monitoring", "visit_lead", LEAD, NAMES) == privacy.WITHHELD
    assert privacy.example("field_monitoring", "team_members", [{"name": MEMBER}], NAMES) == privacy.WITHHELD
    # a candidate key of the team field is withheld whatever it is called
    assert privacy.example("field_monitoring", "visit_team", "x", NAMES) == privacy.WITHHELD
    assert privacy.example("fm_questions", "summary", CANARY_TEXT, NAMES).startswith(
        "Met [name withheld] and"
    )
    assert len(privacy.example("fm_questions", "summary", CANARY_TEXT, NAMES)) == privacy.EXAMPLE_CHARS
    assert privacy.example("fm_questions", "answer", "On track", NAMES) == "On track"


@pytest.mark.parametrize(
    "value, shown",
    [
        (True, "true"),
        (1722, "1722"),
        (0.5, "0.5"),
        ({"id": 1722, "reference_number": "FM-2026-022"}, "{id, reference_number}"),
        (["Education", "WASH"], "[Education, WASH]"),
        ([{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}], "[{id}, {id}, {id}, … 4 in all]"),
        (None, ""),
    ],
)
def test_example_text_of_every_shape(value, shown):
    assert privacy.example("sections", "x", value, frozenset()) == shown


@pytest.mark.django_db
def test_probe_examples_never_hold_a_canary(fm_world):
    refresh.run(triggered_by="test", probe_only=True)
    every = " ".join(e for examples in KeyProbe.objects.values_list("examples", flat=True) for e in examples)
    for canary in (*CANARIES, MEMBER_EMAIL):
        assert canary not in every, canary


# ------------------------------------------------------------------------------------------ team
@pytest.mark.parametrize(
    "value, names, unnamed",
    [
        ([{"name": "Karim Canary", "email": "karim.canary@example.org"}], ["Karim Canary"], 0),
        ([{"first_name": "Nour", "last_name": "Haddad", "email": "n@x.org"}], ["Nour Haddad"], 0),
        ([{"name": "", "email": "only@example.org"}, {"id": 7}], [], 2),
        ([{"name": "karim.canary@example.org"}], [], 1),
        (
            "Rania Canary, Karim Canary; Nour Haddad / Ali Hassan and Zeina Fares",
            ["Rania Canary", "Karim Canary", "Nour Haddad", "Ali Hassan", "Zeina Fares"],
            0,
        ),
        ("Rania Canary <rania@example.org>, only@example.org", ["Rania Canary"], 1),
        ("{'name': 'Rania Canary', 'email': 'rania@example.org'}", ["Rania Canary"], 0),
        ('[{"first_name": "Nour", "last_name": "Haddad"}, {"email": "x@y.org"}]', ["Nour Haddad"], 1),
        ([12, 15], [], 2),
        (["Rania Canary", "RANIA CANARY"], ["Rania Canary"], 0),
        (None, [], 0),
        ("", [], 0),
    ],
)
def test_person_display_gives_names_never_emails(value, names, unnamed):
    shown, count = privacy.person_display(value)
    assert (shown, count) == (names, unnamed)
    assert not any("@" in name for name in shown)
