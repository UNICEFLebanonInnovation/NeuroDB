"""NeuroDB Watch's privacy firewall and grounding checks: what the AI may read (an allow-list, with no
person's name, email or link), the names NeuroDB knows, and the checks every sentence of the AI passes
before anyone reads it."""

import datetime
import decimal
import json
import re

import pytest

from neurodb.accounts.models import Section, User
from neurodb.datamart import models as dm
from neurodb.graph.models import Change
from neurodb.partnerships.models import PCA
from neurodb.review import services as review_services
from neurodb.review.models import FindingAssignment
from neurodb.watch import grounding, people, redact
from neurodb.watch.models import WatchItem, WatchReceipt

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 5)
PD = "LEB/PCA2026001/PD2026001"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# One planted person in each place NeuroDB keeps a person's name
PLANTED = {
    "action point assignee": "Zelda Plantedone",
    "TPM report author": "Yusuf Plantedtwo",
    "field monitoring visit lead": "Xenia Plantedthree",
    "progress report submitter": "Walid Plantedfour",
    "staff traveller": "Vera Plantedfive",
    "PD focal point": "Umar Plantedsix",
    "user": "Salma Plantedseven",
    "assignment owner": "Ms Rania Plantedeight",
}
PLANTED_EMAILS = ["tariq.planted@example.org", "salma.planted@unicef.org"]
NOTE = "Call Qasim Plantednine before Friday"
COMMENT = "Pia Plantedten says this is wrong"


@pytest.fixture(autouse=True)
def _fresh_names():
    people.forget()
    yield
    people.forget()


@pytest.fixture
def planted(db):
    """Every source field holds a planted person, and an email where one fits."""
    dm.ActionPoint.objects.create(datamart_id=1, assigned_to_name=PLANTED["action point assignee"])
    dm.TPMVisit.objects.create(datamart_id=1, author_name=PLANTED["TPM report author"])
    dm.MonitoringFinding.objects.create(datamart_id=1, visit_lead=PLANTED["field monitoring visit lead"])
    dm.ReportedIndicator.objects.create(datamart_id=1, submitted_by=PLANTED["progress report submitter"])
    dm.ProgrammaticVisit.objects.create(datamart_id=1, primary_traveler=PLANTED["staff traveller"])
    PCA.objects.create(
        etl_id="1",
        number=PD,
        title="Learning support",
        unicef_focal_points=[PLANTED["PD focal point"], PLANTED_EMAILS[0]],
    )
    user = User.objects.create(
        username="splanted",
        first_name="Salma",
        last_name="Plantedseven",
        name="Salma R. Plantedseven",
        email=PLANTED_EMAILS[1],
    )
    FindingAssignment.objects.create(
        key="reports_overdue:" + PD, owner=PLANTED["assignment owner"], note=NOTE, status="assigned"
    )
    return user


def _everything_planted() -> str:
    return "; ".join([*PLANTED.values(), *PLANTED_EMAILS, NOTE, COMMENT])


def _item(key: str = "due:report:" + PD + ":7", **fields) -> WatchItem:
    values = {
        "key": key,
        "detector": "report_due_soon",
        "kind": WatchItem.Kind.DEADLINE,
        "severity": WatchItem.Severity.WARNING,
        "title": f"Progress report due 15 Oct 2026: {PD} (Partner A)",
        "due_date": datetime.date(2026, 10, 15),
        "etools_sections": ["Education"],
        "evidence": {
            "source": "eTools progress reports",
            "source_job": "etools_datamart",
            "synced_at": "2026-10-04T20:41:00+03:00",
            "records": [{"label": "QPR 7", "date": "2026-10-01", "value": "due", "url": "/programme/1/"}],
            "numbers": {"indicators": 3, "unspent": 50000},
        },
        "first_seen_on": datetime.date(2026, 9, 28),
        "last_seen_on": TODAY,
        "changed_on": TODAY,
    }
    values.update(fields)
    return WatchItem.objects.create(**values)


def _careless_item(user) -> WatchItem:
    """An item whose every field carries a planted person, as a careless check might write it."""
    everything = _everything_planted()
    item = _item(
        title=f"Report due 15 Oct 2026: {PD} (focal point {PLANTED['PD focal point']}, {PLANTED_EMAILS[0]})",
        detail=everything,
        url="https://etools.example.org/pd/1?who=Zelda+Plantedone",
        entity_kind="programme_document",
        entity_key="1",
        related=[
            {"kind": "programme_document", "key": "1", "name": f"PD led by {PLANTED['staff traveller']}"},
            {"kind": "partner", "key": "9", "name": PLANTED["TPM report author"]},
        ],
        review_key="reports_overdue:" + PD,
        evidence={
            "source": f"eTools, read by {PLANTED['user']}",
            "source_job": "etools_datamart",
            "synced_at": "2026-10-04T20:41:00+03:00",
            "records": [
                {
                    "label": PLANTED["action point assignee"],
                    "date": "2026-10-01",
                    "value": everything,
                    "url": "/x/",
                },
            ],
            "numbers": {
                "indicators": 3,
                "unspent": 50000.0,
                "pd_end": "2026-12-31",
                "lead": PLANTED["field monitoring visit lead"],
                "submitted_by": 4,
                "who": {"name": PLANTED["progress report submitter"]},
                "contact": PLANTED_EMAILS[1],
            },
        },
        close_reason=everything[:300],
        story=[{"on": "2026-10-01", "text": everything[:300]}],
        has_owner=True,
        assignment_status="assigned",
    )
    WatchReceipt.objects.create(
        user=user, item=item, first_told_on=TODAY, last_told_on=TODAY, told_step="new", comment=COMMENT
    )
    return item


def _assert_no_person(text: str) -> None:
    lowered = text.lower()
    assert "planted" not in lowered, text
    for word in ("zelda", "yusuf", "xenia", "walid", "vera", "umar", "salma", "rania", "qasim", "pia "):
        assert word not in lowered, (word, text)
    assert not EMAIL.search(text), text
    assert "http" not in lowered and "example.org" not in lowered


# ---------------------------------------------------------------------------- the names known
def test_every_source_of_names_is_read_two_words_or_more_and_emails(planted):
    dm.ActionPoint.objects.create(datamart_id=2, assigned_to_name="Madonna")  # one word: too common to tell
    PCA.objects.create(
        etl_id="2", number="LEB/PCA2026002/PD2026002", unicef_focal_points=["Child Protection"]
    )
    Section.objects.create(name="Child Protection")  # a section's name is not a person
    known = people.known_names()
    for name in PLANTED.values():
        assert " ".join(people.words(name)).removeprefix("ms ") in known, name
    assert "salma r plantedseven" in known and "salma plantedseven" in known
    assert set(PLANTED_EMAILS) <= known
    assert "madonna" not in known and "child protection" not in known


def test_names_are_read_once_per_run(planted, django_assert_num_queries):
    people.known_names()
    with django_assert_num_queries(0):
        people.known_names()
    dm.ActionPoint.objects.create(datamart_id=3, assigned_to_name="Nadia Latecomer")
    assert "nadia latecomer" not in people.known_names()
    people.forget()
    assert "nadia latecomer" in people.known_names()


def test_a_name_is_looked_for_in_its_usual_forms():
    assert people.name_forms("Doe, Jane") == {"doe jane", "jane doe"}
    assert people.name_forms("Ms Jane Mary Doe (UNICEF)") == {"jane mary doe", "jane doe"}
    assert people.name_forms("Jane Doe; John Roe") == {"jane doe", "john roe"}
    assert people.name_forms("Madonna") == set()
    names = frozenset({"jane doe"})
    assert people.mentions("Ask JANE DOÉ today", names) == ["jane doe"]
    assert people.scrub("Ask Jane Doe (jane@x.org) about it", names) == (
        "Ask [name withheld] ([email withheld]) about it"
    )
    assert people.mentions("Jane and Doe are words", names) == []


def test_a_record_s_own_names_are_looked_for_beside_the_known_ones_and_spaced_addresses_too():
    names = frozenset({"jane doe"})
    own = frozenset({"john roe"})
    assert people.scrub("Jane Doe met John Roe", names, own) == "[name withheld] met [name withheld]"
    assert people.mentions("John Roe", names, own) == ["john roe"]
    assert redact.text("write to jane @ example.org or jane＠example.org", 200, frozenset()) == (
        "write to [email withheld] or [email withheld]"
    )
    assert redact.text("2 trainers @ 50 USD/day", 200, frozenset()) == "2 trainers @ 50 USD/day"


def test_a_known_name_is_found_with_a_middle_name_overlapping_names_and_invisible_characters():
    names = frozenset({"karim nassar", "hiba saad", "saad karam"})
    assert people.scrub("Karim Ahmad Nassar and Karim el Nassar came", names) == (
        "[name withheld] and [name withheld] came"
    )
    assert people.scrub("Karim met Nassar", names) == "Karim met Nassar"  # a lower-case word is no name
    assert people.scrub("Hiba Saad Karam came", names) == "[name withheld] came"  # every word of both
    assert people.scrub("Ka\u200brim Nassar wrote to layla.saab\u200b@example.org", names) == (
        "[name withheld] wrote to [email withheld]"
    )
    assert people.mentions("Karim Ahmad Nassar", names) == ["karim nassar"]


def test_the_longest_name_is_counted_once_per_set_of_names_however_often_the_names_are_read():
    """A set read again holds the same names in a new object: it is counted again, never compared name by
    name with the one before (which would cost more than every text it cleans)."""
    first = frozenset({f"name {n} doe" for n in range(200)})
    again = frozenset(list(first))
    assert again == first and again is not first
    assert people._longest(first) == people._longest(again) == 3
    assert people._LONGEST[id(again)][0] is again


# ---------------------------------------------------------------------------- what the AI may read
def test_planted_people_never_reach_the_ai_input(planted):
    item = _careless_item(planted)
    sent = redact.for_model(
        item, {"today": TODAY, "sections": ["Education"], "times_told": 2, "change": "new"}
    )
    text = json.dumps(sent, default=str)
    _assert_no_person(text)
    assert NOTE not in text and COMMENT not in text
    # what may go is there
    assert sent["key"] == item.key and PD in sent["title"] and "[name withheld]" in sent["title"]
    assert sent["due_date"] == "2026-10-15" and sent["days_left"] == 10 and sent["days_open"] == 7
    assert sent["numbers"] == {"indicators": 3, "unspent": 50000, "pd_end": "2026-12-31"}
    assert sent["evidence_dates"] == ["2026-10-01"]
    assert sent["about"] == {"kind": "programme_document", "name": "PD led by [name withheld]"}
    assert (sent["has_owner"], sent["assignment_status"]) == (True, "assigned")
    assert (sent["sections"], sent["times_told"], sent["change"]) == (["Education"], 2, "new")
    # never the detail, the records, the link, the story or the reason it closed
    assert not {"detail", "url", "records", "story", "close_reason", "review_key"} & set(sent)


def test_the_assignment_owner_and_note_never_appear(planted):
    item = _item(key="review:reports_overdue:" + PD, has_owner=True, assignment_status="assigned")
    text = json.dumps(redact.for_model(item, {"today": TODAY}))
    assert "Rania" not in text and "Plantedeight" not in text and "Qasim" not in text
    assert '"has_owner": true' in text and '"assignment_status": "assigned"' in text


@pytest.mark.parametrize(
    "fields, why",
    [
        ({"key": "system:stale:etools_datamart", "kind": "system", "scope": "admins"}, redact.SYSTEM),
        ({"key": "system:sync_failed", "kind": "concern"}, redact.SYSTEM),
        ({"key": "x:1", "kind": "system"}, redact.SYSTEM),
        ({"key": "makani:centre:4", "detector": "makani_followups"}, redact.MAKANI),
        (
            {"key": "x:2", "evidence": {"records": [{"label": "a"}], "source_job": "compiler_wellbeing"}},
            redact.MAKANI,
        ),
        ({"key": "donor_account:3"}, redact.ADMINS_ONLY),
        ({"key": "rollover:2027", "scope": "admins"}, redact.ADMINS_ONLY),
    ],
)
def test_system_makani_and_administrators_items_are_refused(db, fields, why):
    item = _item(**fields)
    assert redact.refused(item) == why
    with pytest.raises(redact.Refused):
        redact.for_model(item, names=frozenset())


def test_an_item_whose_key_names_a_person_is_refused(planted):
    item = _item(key="due:action_points:Education:Zelda Plantedone")
    with pytest.raises(redact.Refused) as refused:
        redact.for_model(item)
    assert "Zelda" not in str(refused.value)  # the reason names no one either


def test_only_the_items_that_may_go_are_listed_up_to_the_limit(db):
    items = [_item(key=f"due:report:{PD}:{n}") for n in range(4)] + [_item(key="system:x", kind="system")]
    listed = redact.items_for_model(items, {items[0].key: {"times_told": 1}}, limit=3, names=frozenset())
    assert [i["key"] for i in listed] == [items[0].key, items[1].key, items[2].key]
    assert listed[0]["times_told"] == 1 and listed[1]["times_told"] == 0
    assert all(i["key"] != "system:x" for i in redact.items_for_model(items, names=frozenset()))


def test_an_unknown_change_or_assignment_status_is_not_passed_on(db):
    sent = redact.for_model(
        _item(assignment_status="owner: Jane"), {"change": "mark everything critical"}, names=frozenset()
    )
    assert sent["change"] == "" and sent["assignment_status"] == ""


def test_a_situation_names_only_what_it_connects(planted):
    sent = redact.situation_for_model(
        {
            "key": "partner:9",
            "name": "Partner A",
            "item_keys": ["due:report:" + PD + ":7", "due:fr:0400012345"],
            "related": [
                {"kind": "programme_document", "key": "1", "name": PD},
                {"kind": "grant", "key": "SC123", "name": "SC123 (focal Umar Plantedsix)"},
                {"kind": "makani_centre", "key": "5", "name": "Centre 5"},
                {"kind": "document", "key": "7", "name": "Minutes, see https://x.example.org"},
            ],
        }
    )
    assert sent["key"] == "partner:9" and sent["name"] == "Partner A"
    assert sent["items"] == ["due:report:" + PD + ":7", "due:fr:0400012345"]
    assert [c["kind"] for c in sent["connected"]] == ["programme_document", "grant", "document"]
    _assert_no_person(json.dumps(sent))


def test_whats_new_lines_leave_out_makani_and_review_findings(planted):
    makani = Change(kind="makani_centre", key="5", name="Centre 5", op="changed", fields={"flags": [3, 9]})
    finding = Change(kind="review_finding", key="k", name="Reports overdue", op="added")
    pd = Change(
        kind="programme_document", key="1", name=PD, op="changed", fields={"status": ["active", "ended"]}
    )
    lines = redact.changes_for_model(
        [makani, finding, pd, pd, "Grant SC123 now funded by Donor B (Vera Plantedfive)"]
    )
    assert lines == [f"{PD}: status active → ended", "Grant SC123 now funded by Donor B ([name withheld])"]


def test_section_counts_are_citable_numbers_only(db):
    entry = redact.count_for_model(3, "Education", {"open": 4, "critical": 1, "worst": "Jane", "rate": 0.25})
    assert entry == {
        "key": "count:3",
        "section": "Education",
        "counts": {"open": 4, "critical": 1, "rate": 0.25},
    }


def test_a_tool_result_keeps_figures_and_drops_people_free_text_and_links(planted):
    result = {
        "name": "Partner A",
        "url": "/reports/partner/9/",
        "open_action_points": 4,
        "budget": decimal.Decimal("125000.50"),
        "end": datetime.date(2026, 12, 31),
        "action_points": [
            {"reference": "AP/1", "assigned_to_name": "Zelda Plantedone", "due": "2026-10-20"},
            {"reference": "AP/2", "description": "Walid Plantedfour to follow up", "due": None},
        ],
        "focal_points": ["Umar Plantedsix"],
        "submitted_by": "Walid Plantedfour",
        "knowledge_documents": [
            {"title": "Minutes with Salma Plantedseven", "summary": "Long text", "id": 7}
        ],
        "remark": "Write to tariq.planted@example.org or see https://evil.example.org",
        "rows": list(range(50)),
    }
    out = redact.for_tool(result)
    text = json.dumps(out)
    _assert_no_person(text)
    assert out["name"] == "Partner A" and out["open_action_points"] == 4
    assert out["budget"] == 125000.5 and out["end"] == "2026-12-31"
    assert out["action_points"][0] == {"reference": "AP/1", "due": "2026-10-20"}
    assert out["knowledge_documents"] == [{"title": "Minutes with [name withheld]", "id": 7}]
    assert "url" not in out and "Long text" not in text and len(out["rows"]) == 30


# ---------------------------------------------------------------------------- the checks on each sentence
@pytest.fixture
def cited(db):
    return _item()


@pytest.fixture
def other(db):
    return _item(
        key="due:grant:SC123",
        title="Grant SC123 expires 20 Nov 2026 with money unspent",
        due_date=datetime.date(2026, 11, 20),
        evidence={"records": [{"label": "SC123", "date": "2026-11-20"}], "numbers": {"unspent": 73500}},
    )


def test_a_sentence_resting_on_its_facts_is_kept(cited):
    for sentence in (
        f"Partner A's progress report for {PD} is due on 15 Oct.",
        "The report covering 3 indicators is due on October 15, with 50,000 unspent.",
        "Two reports are due in Oct 2026; one was noticed on 1 Oct and is due in 10 days.",
        "PD2026001 has been open for 7 days; the report is due 2026-10-15.",
    ):
        verdict = grounding.check(sentence, [cited], TODAY, frozenset())
        assert verdict and verdict.ok, (sentence, verdict)


def test_a_number_found_only_in_an_item_not_cited_is_dropped(cited, other):
    sentence = "Grant SC123 has 73,500 unspent."
    assert grounding.check(sentence, [cited], TODAY, frozenset()).reason == grounding.NUMBER
    assert grounding.check(sentence, [other], TODAY, frozenset())
    assert grounding.check(
        "Partner A has 50,000.00 unspent and 3 indicators to report.", [cited], TODAY, frozenset()
    )
    assert not grounding.check("Partner A has 50,001 unspent.", [cited], TODAY, frozenset())


@pytest.mark.parametrize(
    "sentence, kept",
    [
        ("The report is due on 15 Oct.", True),
        ("The report is due on 16 Oct.", False),
        ("The report is due on 15 Oct 2027.", False),
        ("The report is due by October 15, 2026.", True),
        ("The report is due by Nov 2026.", False),
        ("The grant expires on 20 Nov.", False),
        ("The report is due on 2026-10-16.", False),
        ("The report is due on 31 Feb.", False),
        ("It was noticed on 1 Oct.", True),  # an evidence date
    ],
)
def test_a_date_must_be_a_date_of_a_cited_item(cited, sentence, kept):
    assert bool(grounding.check(sentence, [cited], TODAY, frozenset())) is kept


def test_a_date_of_another_item_passes_only_when_that_item_is_cited(cited, other):
    assert grounding.check("The grant expires on 20 Nov.", [other], TODAY, frozenset())
    assert (
        grounding.check("The grant expires on 20 Nov.", [cited], TODAY, frozenset()).reason == grounding.DATE
    )


def test_an_etools_reference_must_be_in_the_cited_items(cited):
    assert grounding.check(f"{PD} is due.", [cited], TODAY, frozenset())
    assert grounding.check("LEB/PCA2026009/PD2026009 is due.", [cited], TODAY, frozenset()).reason == (
        grounding.REFERENCE
    )
    assert grounding.check("PD2026009 is due.", [cited], TODAY, frozenset()).reason == grounding.REFERENCE


@pytest.mark.parametrize(
    "sentence",
    [
        "See https://evil.example.org for details.",
        "Details on www.unicef.org now.",
        "Details at unicef.org/lebanon now.",
        "Open the report [here](/programme/1/).",
        "Open /programme/1/ to see it.",
        "Read it at mailto:someone now.",
    ],
)
def test_any_link_drops_the_sentence(cited, sentence):
    verdict = grounding.check(sentence, [cited], TODAY, frozenset())
    assert not verdict and verdict.reason in (grounding.LINK, grounding.MARKUP)


@pytest.mark.parametrize(
    "sentence",
    ["The report is **late**.", "The report is <b>late</b>.", "# Late reports", "- one report", "`late`"],
)
def test_markdown_and_html_drop_the_sentence(cited, sentence):
    assert grounding.check(sentence, [cited], TODAY, frozenset()).reason == grounding.MARKUP


def test_a_sentence_naming_a_planted_person_or_an_email_is_dropped(planted, cited):
    for name in [*PLANTED.values(), "salma plantedseven", "ZELDA PLANTEDONE"]:
        verdict = grounding.check(f"{name} should follow up the report due 15 Oct.", [cited], TODAY)
        assert verdict.reason == grounding.PERSON, name
        assert "lanted" not in verdict.detail
    verdict = grounding.check("Write to x@example.org about the report.", [cited], TODAY)
    assert verdict.reason == grounding.EMAIL


def test_a_long_or_empty_sentence_is_dropped(cited):
    assert grounding.check("x" * 401, [cited], TODAY, frozenset()).reason == grounding.TOO_LONG
    assert grounding.check("   ", [cited], TODAY, frozenset()).reason == grounding.EMPTY
    assert grounding.check("The report is due on 15 Oct.", [], TODAY, frozenset()).reason == grounding.NO_KEYS


def test_the_ai_input_entries_can_be_cited_as_facts(cited, other):
    entries = {e["key"]: e for e in redact.items_for_model([cited, other], names=frozenset())}
    count = redact.count_for_model(3, "Education", {"open": 12, "critical": 2})
    situation = redact.situation_for_model(
        {"key": "partner:9", "name": "Partner A", "item_keys": [cited.key, other.key]}, names=frozenset()
    )
    citable = {**entries, count["key"]: count, situation["key"]: situation}
    answer = {
        "sentences": [
            {"text": "Education has 12 open points, 2 of them critical.", "keys": ["count:3"]},
            {
                "text": "Partner A has a report due 15 Oct and grant SC123 ending 20 Nov.",
                "keys": ["partner:9"],
            },
            {"text": "Partner A has 73,500 unspent.", "keys": [cited.key]},
            {"text": "Partner A is late.", "keys": ["due:report:LEB/PCA2026099/PD2026099:1"]},
            {"text": "Partner A is late.", "keys": "partner:9"},
            "Partner A is late.",
            {"text": "A seventh sentence is never read.", "keys": ["count:3"]},
        ]
    }
    kept, dropped = grounding.validate(answer, citable, today=TODAY, names=frozenset())
    assert kept == [
        {"text": "Education has 12 open points, 2 of them critical.", "keys": ["count:3"]},
        {"text": "Partner A has a report due 15 Oct and grant SC123 ending 20 Nov.", "keys": ["partner:9"]},
    ]
    assert dropped == [grounding.NUMBER, grounding.UNKNOWN_KEY, grounding.MALFORMED, grounding.MALFORMED]
    no_keys = {"sentences": [{"text": "Partner A is late.", "keys": []}]}
    assert grounding.validate(no_keys, citable, names=frozenset()) == ([], [grounding.NO_KEYS])


def test_a_sentence_with_the_systems_words_is_dropped_unless_a_name_it_cites_has_them(cited):
    """Staff pages say "points" and "NeuroDB": never item, detector, receipt, agent or LLM."""
    for sentence, word in (
        ("Two items are due on 15 Oct.", "items"),
        ("The detector found a report due on 15 Oct.", "detector"),
        ("The agent saw a report due on 15 Oct.", "agent"),
    ):
        verdict = grounding.check(sentence, [cited], TODAY, frozenset())
        assert (verdict.reason, verdict.detail) == (grounding.WORDING, word)
    named = {"key": "partner:9", "name": "Non-food items for Partner A", "items": [cited.key]}
    assert grounding.check(
        "Non-food items for Partner A: a report is due 15 Oct.", [named, cited], TODAY, frozenset()
    )
    assert grounding.check("Two points are due on 15 Oct.", [cited], TODAY, frozenset())


def test_the_review_number_helper_is_shared_under_its_new_name():
    assert review_services._numbers is review_services.numbers_in
    assert review_services.numbers_in("12,500 PDs and 3. Then 4.5") == {"12500", "3", "4.5"}


# ------------------------------------------------------------------------------------ written-out addresses
def test_a_text_sent_loses_a_written_out_address_and_keeps_its_sentences():
    """Every text the watch (and Monitoring insights, the Help assistant's look-ups) sends: an address
    written out goes whole, not only its domain as a link; a sentence that only looks like one keeps its
    words ("published at Example.Org" loses its link)."""
    from tests.written_addresses import NOT_ADDRESSES, RECORD, RECORD_FORMS

    for form, written in RECORD_FORMS.items():
        assert redact.text(RECORD.format(written), 500, frozenset()) == RECORD.format(
            people.EMAIL_WITHHELD
        ), form
    for text, sent in NOT_ADDRESSES.items():
        assert redact.text(text, 500, frozenset()) == sent


def test_no_written_out_address_reaches_the_look_up_or_the_morning_note():
    """An item's title, a look-up's result and a What's new line, as the watch sends them."""
    from neurodb.watch import explain, investigate
    from tests.written_addresses import RECORD, RECORD_FORMS, leaks

    found = {}
    for form, written in RECORD_FORMS.items():
        record = RECORD.format(written)
        WatchItem.objects.all().delete()
        item = WatchItem.objects.create(
            key=f"review:reports_overdue:{PD}",
            detector="review",
            kind=WatchItem.Kind.CONCERN,
            severity="critical",
            title=f"Progress reports overdue. {record}",
            detail="never sent",
            url="/programme/1/",
            entity_kind="programme_document",
            entity_key=PD,
            etools_sections=["Education"],
            evidence={"records": [], "numbers": {"overdue_reports": 2}},
            first_seen_on=TODAY,
            last_seen_on=TODAY,
            changed_on=TODAY,
        )
        asked = json.loads(investigate.question(item, TODAY, frozenset())[len(investigate.QUESTION) :])
        tool = redact.for_tool({"name": record, "partner": record}, frozenset())
        facts = explain.facts(
            "all sections", [item], changes=[f"A change: {record}"], names=frozenset(), today=TODAY
        )
        sent = {
            "look-up title": asked["title"],
            "look-up result": json.dumps(tool, ensure_ascii=False),
            "note": json.dumps(facts.payload, ensure_ascii=False),
        }
        found |= {f"{form} {where}": text for where, text in leaks(sent).items()}
    assert not found, json.dumps(found, ensure_ascii=False, indent=1)


def test_addresses_joined_by_a_slash_or_before_a_path_go_whole():
    """A list of focal points joined by "/" ("x at example dot org/y at example dot org") or an address
    before a path ("x at example.org/ops"): every name goes, and so does the path, which may hold one."""
    from tests.written_addresses import JOINED, left

    for text in JOINED:
        for sent in (redact.text(text, 500, frozenset()), people.without_emails(text)):
            assert not left(sent), (text, sent)
    assert redact.text("Contacts: zfakemira at example dot org/zaynafake at example dot org", 500, ()) == (
        "Contacts: [email withheld]/[email withheld]"
    )
    assert redact.text("Email zfakemira at example.org/ops for details", 500, ()) == (
        "Email [email withheld] for details"
    )


def test_every_character_nobody_sees_is_taken_out():
    """Unicode's format characters (the marks of right-to-left text, the isolates, the zero-width ones),
    the grapheme joiner, the vowel separator, the variation selectors and the Hangul fillers: an address or
    a name with one inside is found as it reads."""
    import unicodedata

    hidden = [chr(c) for c in range(0x80, 0x30000) if unicodedata.category(chr(c)) == "Cf"]
    hidden += ["͏", "ᅟ", "ᅠ", "᠋", "ㅤ", "️", "ﾠ", "\U000e0101"]
    for char in hidden:
        assert people.visible(f"zfake{char}mira") == "zfakemira", hex(ord(char))
    assert people.visible("مرحبا‏ بكم") == "مرحبا بكم"  # the words of right-to-left text stay


def test_a_typed_text_keeps_its_places_and_takes_a_web_address_whole():
    """What a person types: a place written with an abbreviation or a sentence missing its space stays
    ("at Mt.Lebanon", "at Akkar.No"), a web address after a word goes as a link with its path, and styled
    letters are read as the plain ones only where they are letters ("10³ km²" stays)."""
    from tests.written_addresses import TYPED_NOT_ADDRESSES

    for text, sent in TYPED_NOT_ADDRESSES.items():
        assert people.without_addresses(text) == sent, text


def test_a_typed_text_loses_an_address_by_what_follows_it_or_writes_to_it():
    """What a person types: after a dot and a space, ".net" or ".fr" ends an address when the text ends, a
    mark or a word that follows an address comes after it, or a word that writes to one ("contact") comes
    before its name; a common word's ending (".school") or a chat's (".show") does too."""
    from tests.written_addresses import TYPED_ADDRESSES, TYPED_NOT_ADDRESSES

    found = {text: people.without_addresses(text) for text in TYPED_ADDRESSES}
    assert found == TYPED_ADDRESSES
    for text, sent in TYPED_NOT_ADDRESSES.items():  # what it holds no address of is sent the same each turn
        assert people.without_addresses(people.without_kept_names(sent)) == sent, text


def test_a_record_text_loses_an_address_with_a_digit_capitals_or_before_a_links_scheme():
    """A record's text: a name holding a digit anywhere, "AT" in capitals before a word of two letters, an
    address's name before a link with its scheme, an at sign before a dot and a space at the text's end."""
    from tests.written_addresses import RECORD_ADDRESSES

    for text, sent in RECORD_ADDRESSES.items():
        assert people.without_emails(text) == sent, text
        assert redact.text(text, 500, frozenset()) == sent, text


def test_a_record_text_loses_a_domain_the_link_rule_does_not_know_as_a_link():
    """A word that names no one before "at" and a domain of an ending the link rule does not know (".ngo",
    ".de", ".jo"): the domain goes as a link in every record path, the words stay; a link defanged as a
    security note writes it goes too."""
    from tests.written_addresses import RECORD_LINKS

    for text, sent in RECORD_LINKS.items():
        assert people.without_emails(text) == sent, text
        assert redact.text(text, 500, frozenset()) == sent, text
    assert redact.LINK.sub(redact.LINK_WITHHELD, "Do not open hxxps[:]//example[.]org today") == (
        "Do not open [link withheld] today"
    )
    assert redact.LINK.sub(redact.LINK_WITHHELD, "See example[.]org for it") == "See [link withheld] for it"


def test_the_final_review_texts_lose_what_a_commit_of_the_chain_withheld():
    """The final review's texts, privacy first: a surname or an acronym in capitals before a place's name and a
    capitalised ending, a name after a determiner, "point" after "the" or a word written to, the items of a list
    of addresses, a mailbox written to at a place's domain, a page's word after a verb that mails, a name before
    "St.Joseph"; in a record's text too, and a capitalised name before a domain no sentence writes and ". Org"."""
    from tests.written_addresses import FINAL_RECORD, FINAL_TYPED, left

    found = {text: people.without_addresses(text) for text in FINAL_TYPED}
    assert found == FINAL_TYPED
    for (
        text,
        sent,
    ) in FINAL_TYPED.items():  # no part of an invented address is left (a name is the name rule's)
        assert not left(sent.replace("Zayna Fakemira", "")), text
    found = {text: people.without_emails(text) for text in FINAL_RECORD}
    assert found == FINAL_RECORD
    for text, sent in FINAL_RECORD.items():
        assert redact.text(text, 500, frozenset()) == sent, text


def test_the_final_review_keeps_a_sentence_after_a_places_name():
    """A plain word in lower case before a place's name written capitalised, a full stop, a space and a sentence
    stays, as d1b90c8 read it ("Teachers trained at Hammana. Net enrolment rose after that?"); a word that may name
    someone does not ("Met Rania at Mt. Lebanon. Org chart updated" keeps no name: its record is the record
    rule's); a page's word after a verb that goes to a page stays, its domain a link, in a KM text too."""
    from neurodb.km import privacy as km_privacy

    for text in (
        "Teachers trained at Hammana. Net enrolment rose after that?",
        "Classes resumed at Zouk. Org chart updated, who approved it?",
        "Kits arrived at Deir. Info sessions followed. How many children attended?",
        "Staff were present at Mt. Lebanon. Org chart updated.",
        "We arrived at the point eg the school gate",
    ):
        assert people.without_addresses(text) == text, text
        assert people.without_emails(text) == text, text
    assert people.without_addresses("Met Rania at Mt. Lebanon. Org chart updated.") == (
        "Met [email withheld] chart updated."
    )
    for text, sent in {
        "How do I submit my report to the portal at zfakemira. org?": (
            "How do I submit my report to the portal at [link withheld]?"
        ),
        "How do I submit my report to the portal at example.org?": (
            "How do I submit my report to the portal at [link withheld]?"
        ),
    }.items():
        assert people.without_addresses(text) == sent, text
    assert km_privacy.for_ai("How do I submit my report to the portal at zfakemira. org?") == (
        "How do I submit my report to the portal at [link withheld]?"
    )


def test_what_the_pushed_rules_withheld_stays_withheld():
    """The regression sweep against the pushed rules (every corpus text through every path, compared word by
    word): a document sent whole, which no link rule reads after, loses a web address written after a word
    and "at" as a link, a code's too; what is glued after an address withheld goes with it; a sentence's
    "point" is no French dot; a filter's search words and a page's address read a web address after "at" the
    typed rule withholds."""
    for text, sent in {
        "Contact us at zaynafakemira.org for the files.": "Contact us at [link withheld] for the files.",
        "How many Session2 at SCHOOL.Net many visits": "How many Session2 at [link withheld] many visits",
        "x.y at example.org.It was": "[email withheld] was",
        "Fake notes: ZFAKEMIRA AT example.co.SHOW is the focal point": (
            "Fake notes: [email withheld] is the focal point"
        ),
    }.items():
        assert people.without_emails(text) == sent, text
    assert (
        people.glued_after("Mark the [email withheld].parts_may_fail now.")
        == "Mark the [email withheld] now."
    )
    assert (
        people.glued_after("Write to [email withheld]. It was sent.")
        == "Write to [email withheld]. It was sent."
    )
    for text in (
        "Problems at this point co-ordination with partners is weak",
        "We arrived at the point eg the school gate",
        "Attendance at baseline point net of dropouts",
        "Children at school point de vue des parents",
    ):
        assert people.without_addresses(text) == text, text
        assert people.without_emails(text) == text, text
        assert redact.text(text, 500, frozenset()) == text, text
    assert people.without_addresses("zfakemira at example point org") == "[email withheld]"
    assert people.withheld_typed("children at example.org") and not people.withheld_address(
        "children at example.org"
    )
    assert not people.withheld_typed("How many visits at Halba had red flags?")


def test_what_the_pushed_rules_withheld_stays_withheld_round_two():
    """The second regression sweep: a role's mailbox after a phone number in a list of contacts is an address's
    name; what the link rule leaves glued after a link withheld goes with it; a code before a domain whose words
    no sentence writes is an address's name in every path; a capitalised name inside a sentence before a dot,
    a space and an ending is an address's ("is Zfakemira at example. ORG correct?"), a plain verb is not
    ("Volunteers worked at night. Net enrolment rose.")."""
    assert (
        people.without_addresses("Tel 01 234 567 / office at example.org")
        == "Tel 01 234 567 / [email withheld]"
    )
    assert redact.text("Phone 01 234 567, info at example.org", 500, frozenset()) == (
        "Phone 01 234 567, [email withheld]"
    )
    assert people.glued_after("see [link withheld].it was closed") == "see [link withheld] was closed"
    assert people.glued_after("see [link withheld]. It was closed") == "see [link withheld]. It was closed"
    assert redact.text("Visit example.com.it today", 500, frozenset()) == "Visit [link withheld] today"
    for text, sent in {
        "How many G5 at example.it was closed": "How many [email withheld] was closed",
        "F09 at Example.Org": "[email withheld]",
        "Grade5 at fakemira-school.Be is old": "[email withheld] is old",
    }.items():
        assert people.without_addresses(text) == sent, text
        assert people.without_emails(text) == sent, text
        assert redact.text(text, 500, frozenset()) == sent, text
    assert people.without_addresses("is Zfakemira at example. ORG correct?") == "is [email withheld] correct?"
    assert (
        people.without_addresses("Who wrote the profile at example.org?")
        == "Who wrote the profile at [link withheld]?"
    )


def test_what_the_pushed_rules_withheld_stays_withheld_round_three():
    """The third convergence round: an item after a phone number and a dash is an address's name; a name
    before ". Org" written in a sentence goes with the domain, a place before it stays; a sentence that only
    writes to a school at a town keeps its words."""
    assert (
        people.without_addresses("Phone 01 234 567 - monitoring at example.org")
        == "Phone 01 234 567 - [email withheld]"
    )
    assert redact.text("Phone 01 234 567 - partners at example.org", 500, frozenset()) == (
        "Phone 01 234 567 - [email withheld]"
    )
    assert people.without_addresses("Contact: Zayna Fakemira, zfakemira at example. Org for the files.") == (
        "Contact: Zayna Fakemira, [email withheld] for the files."
    )
    for text in (
        "Staff were present at Halba. Org chart updated.",
        "We contacted the school at Bebnine. It was closed.",
        "Contact Rami at Halba. Net enrolment is low.",
        "Send the kits to the school at Halba. Info sessions followed.",
    ):
        assert people.without_addresses(text) == text, text
        assert redact.text(text, 500, frozenset()) == text, text


def test_a_page_address_that_names_a_person_after_a_title_is_never_sent():
    """The name gate: a page's address whose query writes a name after a title ("Ms Rania met the principal",
    "Fr Elias attended") is dropped by the Help assistant and by Monitoring insights, as one holding an
    address is; one without a name is kept."""
    from neurodb.fmm import privacy as fmm_privacy
    from neurodb.help import assistant as help_assistant

    for text in (
        "Ms Rania met the principal at noon. Org chart updated.",
        "Attendance was low at first. Fr Elias attended.",
    ):
        assert people.titled_name(text), text
        assert help_assistant.page_path(f"/fmm/?q={text}") == "", text
        assert not fmm_privacy._safe_url(f"/fmm/?q={text.replace(' ', '+')}"), text
    assert not people.titled_name("French Fr classes")
    assert help_assistant.page_path("/fmm/?q=dropout in Akkar") == "/fmm/?q=dropout in Akkar"
    assert fmm_privacy._safe_url("/fmm/?q=dropout+in+Akkar")
