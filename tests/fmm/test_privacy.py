"""Monitoring insights' privacy rules: how a text is cleaned, which keys hold a person, what a key's
example shows, and how a team is shown (names only); what an AI brief sends and keeps (stage 6b); what
the chat's look-ups, its requests and its log carry (stage 7)."""

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


# ------------------------------------------------------------------------------------------ team names
def test_the_team_names_join_the_names_neurodb_removes(fm_world):
    """A team member who is on no other list (here only in a record's team) is known once the visits are
    built: Watch, Ask and Monitoring insights then remove the name from every text they send."""
    assert privacy.team_names in people.EXTRA_SOURCES  # registered when the app started
    people.forget()
    assert "karim canary" not in people.known_names()  # only in the team, not built yet
    refresh.run(triggered_by="test")
    assert MEMBER in privacy.team_names() and not any("@" in n for n in privacy.team_names())
    people.forget()
    assert "karim canary" in people.known_names()
    assert privacy.clean(f"Met {MEMBER} at the centre", 100)[0] == "Met [name withheld] at the centre"
    people.forget()


def test_a_source_of_names_that_fails_is_skipped(db, monkeypatch, caplog):
    def broken():
        raise RuntimeError("no such table")

    monkeypatch.setattr(people, "EXTRA_SOURCES", [broken, lambda: ["Nour Khalil"]])
    people.forget()
    assert "nour khalil" in people.known_names()  # the other sources are read
    assert "could not be read" in caplog.text
    people.forget()


# ------------------------------------------------------------------------------------------ the AI (stage 6a)
CARD_KEYS = {
    "key",
    "label",
    "date",
    "start",
    "end",
    "status",
    "partner",
    "pd",
    "sections",
    "governorate",
    "place",
    "rating",
    "rated_on",
    "hact_q1",
    "quality",
    "flags",
    "urgency",
    "action_points_open",
    "action_points_overdue",
}


@pytest.mark.parametrize(
    "payload",
    [
        {"narratives": {"narr:1:1": {"text": "Rania Canary met the partner."}}},
        {"visits": [{"partner": "Write to karim.canary@example.org"}]},
        {"text": "Call +961 3 123 456 for access."},
        {"text": "Call 03-123456."},
        {"text": "See https://evil.example/x"},
        {"Karim Canary": 1},  # keys are checked too
        {"visits": {"karim.canary": {"x": 1}}},
    ],
)
def test_assert_clean_stops_a_payload_holding_a_person_or_a_contact(payload):
    with pytest.raises(privacy.PrivacyRefused) as refused:
        privacy.assert_clean(payload, NAMES)
    message = str(refused.value)
    for canary in CANARIES:
        assert canary not in message  # it says what and where, never the text


def test_assert_clean_lets_figures_references_and_placeholders_through():
    payload = {
        "kpi": {"key": "kpi", "visits": 33, "avg_quality": 94.7, "from": "2026-05-11"},
        "visits": {
            "visit:1722": {"pd": "LEB/PCA2023597/PD2025123", "flags": ["R1"], "url": "/fmm/visits/1722/"}
        },
        "narratives": {
            "narr:1722:1": {"text": f"{' '.join(KEPT)} [name withheld] [phone withheld] [link withheld]"}
        },
    }
    privacy.assert_clean(payload, NAMES)


def test_a_visit_card_is_an_allow_list_copy_without_people_or_narratives(built):
    from neurodb.fmm.models import Visit

    names = privacy.names()
    visit = Visit.objects.select_related("partner", "pd").get(key="1722")
    card = privacy.visit_card(visit, names)
    assert set(card) == CARD_KEYS
    assert card["key"] == "visit:1722" and card["date"] == visit.visit_date.isoformat()
    assert card["date"] == card["start"] == "2026-05-11"  # the visit date periods read: its start
    assert card["end"] == visit.end_date.isoformat() and card["date"] != card["end"]
    assert card["rated_on"] == card["end"] and card["urgency"] == visit.urgency
    assert card["pd"] and card["partner"]
    blob = repr(card)
    for canary in (*CANARIES, LEAD, MEMBER):
        assert canary not in blob, canary
    privacy.assert_clean(card, names)
    # a planned visit not rated yet carries no rating date
    planned = Visit.objects.get(key="1724")
    assert privacy.visit_card(planned, names)["rated_on"] is None


# ------------------------------------------------------------------------------------------ the brief's payload
SOME_PEOPLE = (
    "Met Mrs Layla Saab: 412 children attended on 2026-05-11; 2,800 kits for PCA2023597 under "
    "LEB/PCA2023597/PD2025123."
)


@pytest.fixture
def brief_world(built, monkeypatch):
    """The built world with one more note naming a single person (so it is sent, redacted), the AI on,
    and a fake model that answers with a brief resting on the facts."""
    import json
    from types import SimpleNamespace

    from django.test import override_settings

    from neurodb.assistant import agent
    from neurodb.datamart.models import MonitoringFinding

    MonitoringFinding.objects.filter(datamart_id=103).update(narrative_finding=SOME_PEOPLE)
    people.forget()
    sent = []

    def create(**params):
        sent.append(params)
        brief = {
            "coverage_summary": [{"text": "Eight visits in the period.", "keys": ["kpi"]}],
            "key_findings": [],
            "challenges": [],
            "recommendations": [],
            "action_points": [],
        }
        return SimpleNamespace(output_text=json.dumps(brief), usage=None, status="completed", output=[])

    api = SimpleNamespace(responses=SimpleNamespace(create=create))
    api.with_options = lambda **_: api
    monkeypatch.setattr(agent, "client", lambda: api)
    with override_settings(FMM_AI=True, AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="x"):
        yield sent


def _year():
    from django.http import QueryDict

    from neurodb.fmm.scope import Scope

    return Scope.from_params(QueryDict("year=2026&section="))


def test_no_canary_reaches_the_brief_request_its_kept_payload_or_the_preview(brief_world, client, admin_user):
    import json

    from django.urls import reverse

    from neurodb.fmm.ai import insights, profiles
    from neurodb.fmm.models import Insight

    row = insights.generate(_year(), trigger=Insight.Trigger.NIGHTLY)
    assert row.status == Insight.Status.PARTIAL and len(brief_world) == 1
    request = brief_world[0]
    sent = request["instructions"] + json.dumps(request["input"], ensure_ascii=False)  # (a)
    kept = json.dumps(Insight.objects.get(pk=row.pk).sent_payload, ensure_ascii=False)  # (b)
    client.force_login(admin_user)
    preview = client.get(
        reverse("admin:fmm_promptversion_preview", args=[profiles.published().pk]),
        {"url": "/fmm/?year=2026&section="},
    ).content.decode()  # (c)
    for blob in (sent, kept, preview):
        for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL):
            assert canary not in blob, canary
    for figure in (*KEPT, "LEB/PCA2023597/PD2025123"):  # the figures and references of a note go intact
        assert figure in sent and figure in kept, figure
    assert "[name withheld]" in sent


def test_a_note_naming_more_than_three_people_or_contacts_is_never_sent(brief_world):
    from neurodb.fmm.ai import facts, profiles

    found = facts.build(_year(), profiles.published())
    texts = [n["text"] for n in found.payload["narratives"].values()]
    assert texts and not any("photos at" in t for t in texts)  # the canary note: 6 placeholders
    assert found.sent["narratives_withheld"] == 1
    _cleaned, placeholders = privacy.clean(CANARY_TEXT, 600, privacy.names())
    assert placeholders > 3


def test_narr_0_sends_no_note(brief_world):
    from neurodb.fmm.ai import facts, profiles

    none = profiles.draft_from(profiles.published(), None, "No notes", narratives_sampled=0)
    found = facts.build(_year(), none)
    assert found.payload["narratives"] == {} and found.sent["narratives"] == 0
    assert found.sent["narratives_allowed"] == 0


def test_the_last_check_stops_a_brief_when_a_canary_slips_through(brief_world, monkeypatch, caplog):
    from neurodb.fmm.ai import insights
    from neurodb.fmm.models import Insight

    monkeypatch.setattr(privacy, "clean", lambda text, limit, names_=None: (" ".join(str(text).split()), 0))
    with caplog.at_level("ERROR"):
        row = insights.generate(_year(), trigger=Insight.Trigger.NIGHTLY)
    assert (row.status, row.reason, row.called) == ("failed", "privacy", False)
    assert brief_world == []  # no call
    assert "a brief was not sent" in caplog.text
    assert not any(canary in caplog.text for canary in CANARIES)


def test_the_ai_code_never_reads_raw_records_or_people():
    import re
    from pathlib import Path

    import neurodb.fmm.ai as ai

    reads = re.compile(r"""\.data\b|\["data"\]|visit_lead|\.team\b|["']team["']""")
    offenders = [
        path.name for path in Path(ai.__file__).parent.glob("*.py") if reads.search(path.read_text("utf-8"))
    ]
    assert offenders == []


def test_no_canary_is_kept_in_any_brief_field(brief_world, viewer):
    from django.db import models

    from neurodb.fmm.ai import insights
    from neurodb.fmm.models import Insight

    insights.generate(_year(), trigger=Insight.Trigger.NIGHTLY)
    insights.generate(_year(), user=viewer, trigger=Insight.Trigger.USER)  # reused, a skipped row
    text_fields = [
        f.name
        for f in Insight._meta.concrete_fields
        if isinstance(f, models.CharField | models.TextField | models.JSONField)
        or f.get_internal_type() == "ArrayField"
    ]
    for row in Insight.objects.values(*text_fields):
        blob = repr(row)
        for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL):
            assert canary not in blob, canary


# ------------------------------------------------------------------------------------------ the chat
CHAT_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}


def _chat_scope():
    from django.http import QueryDict

    from neurodb.fmm.scope import Scope

    return Scope.from_params(QueryDict("year=2026&section="))


def _canary_free(value) -> None:
    import json

    blob = json.dumps(value, ensure_ascii=False, default=str)
    for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL):
        assert canary not in blob, canary


@pytest.fixture
def chat_world(built, monkeypatch):
    """The built world with the names known, the AI on and a scripted streaming model."""
    from django.test import override_settings

    from neurodb.assistant import agent
    from tests.assistant.test_assistant import FakeClient

    people.forget()

    def install(*script):
        client = FakeClient(script)
        monkeypatch.setattr(agent, "client", lambda: client)
        return client

    with override_settings(**CHAT_ON):
        yield install


def _look_ups():
    return (
        ("fm_summary", {"group_by": "partner"}),
        ("fm_visits", {"limit": 30}),
        ("fm_visit", {"visit": "1722"}),
        ("fm_visit", {"visit": "1723"}),
        ("fm_visit", {"visit": "1726"}),
        ("fm_search", {"text": "registers"}),
        ("fm_search", {"text": "children"}),
        ("fm_search", {"text": "PSEA"}),
    )


def test_no_canary_reaches_a_look_up_of_the_chat_or_of_ask(chat_world, monkeypatch):
    """(d): every look-up, through the chat's filter (bound) and as Ask runs it (unbound)."""
    from neurodb.assistant import tools as assistant_tools
    from neurodb.fmm import scope as scope_module
    from neurodb.fmm.ai import tools

    ctx = tools.ChatContext(scope=_chat_scope(), texts_left=50, cards_max=15)
    run = privacy.chat_filter(ctx)
    texts = []
    for name, args in _look_ups():
        with tools.bind(ctx):
            result = run(name, assistant_tools.run(name, args))
        assert "error" not in result, (name, result)
        _canary_free(result)
        texts.append(result)
    assert ctx.privacy_blocked == 0 and ctx.texts_sent > 0
    one = texts[2]
    notes = [e["narrative"] for e in one["records"]]
    assert notes[0].startswith("Classes held as planned. Visit led by [name withheld]")
    assert notes[1] == tools.TOO_MANY_NAMES  # the canary note names too many people and contacts
    assert any(m.get("snippet") for m in texts[5]["matches"])
    monkeypatch.setattr(
        scope_module, "_today", lambda today=None: today or __import__("datetime").date(2026, 10, 5)
    )
    for name, args in _look_ups():
        _canary_free(assistant_tools.run(name, args))


def test_the_limit_of_texts_holds_across_the_look_ups_of_one_answer(chat_world):
    from neurodb.fmm.ai import tools

    ctx = tools.ChatContext(scope=_chat_scope(), texts_left=2, cards_max=15)
    with tools.bind(ctx):
        one = tools.fm_visit("1722")
        found = tools.fm_search("delayed")
        other = tools.fm_visit("1726")
    notes = [e["narrative"] for e in one["records"]]
    assert notes[0] and notes[0] not in (tools.LIMIT_REACHED, tools.TOO_MANY_NAMES)
    assert notes[1] == tools.TOO_MANY_NAMES and notes[2] and notes[2] != tools.LIMIT_REACHED  # 2 read
    answers = {a["answer"] for a in one["answers"] if a["answered"]}
    assert tools.LIMIT_REACHED in answers and answers <= {tools.LIMIT_REACHED, tools.TOO_MANY_NAMES}
    assert found["matches"] and not any("snippet" in m for m in found["matches"])
    assert {e["narrative"] for e in other["records"]} == {tools.LIMIT_REACHED}
    assert (ctx.texts_left, ctx.texts_sent) == (0, 2)
    # narr = 0 (or the AI's texts off): no text at all
    none = tools.ChatContext(scope=_chat_scope(), texts_left=0, cards_max=15)
    with tools.bind(none):
        assert {e["narrative"] for e in tools.fm_visit("1726")["records"]} == {tools.LIMIT_REACHED}
    assert none.texts_sent == 0


def test_a_question_naming_someone_is_cleaned_before_it_is_sent_and_kept(chat_world, client_viewer):
    """(e): the chat request carries no canary, nor does any field of its log, whatever the model writes."""
    import json

    from django.db import models
    from django.urls import reverse

    from neurodb.fmm.models import ChatQuestion
    from tests.assistant.test_assistant import call, reply, say

    fake = chat_world(
        reply(call("fm_visit", {"visit": "1722"})),
        reply(say(f"{MEMBER} wrote to {MEMBER_EMAIL} and +961 3 123 456: [Visit 1722](/fmm/visits/1722/).")),
    )
    response = client_viewer.post(
        reverse("fmm:chat_stream"),
        {"question": f"What did {MEMBER} ({MEMBER_EMAIL}) find?", "scope": "year=2026&section="},
    )
    body = b"".join(response.streaming_content).decode()
    for request in fake.requests:
        _canary_free(request["input"])
        _canary_free(request["instructions"])
    events = [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]
    done = next(e for e in events if e["type"] == "done")  # the checked answer shown and kept
    _canary_free(done)
    assert 'href="/fmm/visits/1722/"' in done["html"]
    text_fields = [
        f.name
        for f in ChatQuestion._meta.concrete_fields
        if isinstance(f, models.CharField | models.TextField | models.JSONField)
    ]
    row = ChatQuestion.objects.values(*text_fields).get()
    _canary_free(row)
    assert row["question"].startswith("What did [name withheld]")


def test_a_look_up_holding_a_canary_is_stopped_before_the_model_reads_it(
    chat_world, client_viewer, monkeypatch, caplog
):
    import json

    from django.urls import reverse

    from neurodb.fmm.models import ChatQuestion
    from tests.assistant.test_assistant import call, reply, say

    monkeypatch.setattr(privacy, "clean", lambda text, limit, names_=None: (" ".join(str(text).split()), 0))
    fake = chat_world(reply(call("fm_visit", {"visit": "1722"})), reply(say("Nothing to share.")))
    with caplog.at_level("ERROR"):
        response = client_viewer.post(
            reverse("fmm:chat_stream"), {"question": "Tell me about 1722", "scope": "year=2026&section="}
        )
        b"".join(response.streaming_content)
    outputs = [
        json.loads(i["output"]) for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"
    ]
    assert outputs == [{"error": privacy.LOOKUP_WITHHELD}]
    assert ChatQuestion.objects.get().checks["privacy_blocked"] == 1
    assert "was not shared" in caplog.text
    assert not any(canary in caplog.text for canary in CANARIES)


def test_earlier_answers_are_cleaned_and_cut_when_sent_again(built, viewer):
    import uuid

    from neurodb.fmm.ai import chat, profiles
    from neurodb.fmm.models import ChatQuestion

    people.forget()
    conversation = uuid.uuid4()
    ChatQuestion.objects.create(
        user=viewer,
        conversation=conversation,
        scope_hash="s",
        version=profiles.published(),
        question="Who?",
        answer=f"{MEMBER_EMAIL} and {LEAD} noted " + "x" * 2000,
        status="answered",
        checks={"kept": ["1722"], "numbers": ["412"]},
    )
    turns, seen, numbers = chat.history(viewer, conversation, "s")
    assert len(turns) == 1 and len(turns[0]["answer"]) <= 1500
    _canary_free(turns)
    assert (seen, numbers) == ({"1722"}, {"412"})
    assert chat.history(viewer, conversation, "another filter") == ([], set(), set())


# ------------------------------------------------------------------------------------------ AI checks
@pytest.mark.django_db
def test_an_ai_check_sends_one_record_with_no_person_no_visit_and_no_entity_name(fm_world, monkeypatch):
    """Release 2 step 5: a check sends one record's texts, cleaned; never the visit's label or reference,
    the entity's name, the team, the visit lead or a monitor's e-mail address."""
    import json

    from django.test import override_settings

    from neurodb.assistant import agent
    from neurodb.fmm.ai import checks

    from .conftest import PD_BASE, PD_EDU, PD_LEBA, SSFA, FakeChecks

    refresh.run(triggered_by="test")
    people.forget()
    fake = FakeChecks()
    monkeypatch.setattr(agent, "client", fake.client)
    with override_settings(FMM_AI=True, AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="x"):
        run = checks.run("test")
    assert run.rows_written == len(fake.requests) > 0
    blob = json.dumps([r["input"] for r in fake.requests], ensure_ascii=False)
    for canary in CANARIES:
        assert canary not in blob, canary
    for name in ("Amel Association", "Mercy Corps Lebanon", PD_LEBA, PD_BASE, PD_EDU, SSFA, "FM-2026-0"):
        assert name not in blob, name
    for sent in fake.sent():
        assert set(sent) - {"entity_type"} <= set(checks.VISIT_FIELDS) | {
            "hact_q1_answer",
            "hact_q2_answer",
            "hact_q3_answer",
            "narrative_finding",
            "overall_finding_rating",
            "visit_goals",
            "objective",
        }
    assert "412 children" in blob  # the figures of the texts stay
