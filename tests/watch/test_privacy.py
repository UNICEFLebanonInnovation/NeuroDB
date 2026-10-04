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
            {"text": "Education has 12 open items, 2 of them critical.", "keys": ["count:3"]},
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
        {"text": "Education has 12 open items, 2 of them critical.", "keys": ["count:3"]},
        {"text": "Partner A has a report due 15 Oct and grant SC123 ending 20 Nov.", "keys": ["partner:9"]},
    ]
    assert dropped == [grounding.NUMBER, grounding.UNKNOWN_KEY, grounding.MALFORMED, grounding.MALFORMED]
    no_keys = {"sentences": [{"text": "Partner A is late.", "keys": []}]}
    assert grounding.validate(no_keys, citable, names=frozenset()) == ([], [grounding.NO_KEYS])


def test_the_review_number_helper_is_shared_under_its_new_name():
    assert review_services._numbers is review_services.numbers_in
    assert review_services.numbers_in("12,500 PDs and 3. Then 4.5") == {"12500", "3", "4.5"}
