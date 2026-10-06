"""The AI monitoring brief (fmm.ai.facts, insights, fallback; stage 6b): the facts and their selection,
the request, the checks of every sentence and priority action, what is kept when the call fails or
nothing passes, the cache, the badges, the page's card (Regenerate in a background process, polling,
what was sent), the nightly briefs and their housekeeping, the visit page and the visits table, the
admin's test run, Preview and brief pages, and the strict format through the real SDK."""

import datetime
import json
import uuid
from types import SimpleNamespace

import openai
import pytest
from django.contrib.admin.sites import site
from django.core.management import call_command
from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.assistant import agent, usage
from neurodb.assistant.models import AIUsage
from neurodb.core.models import SyncRun
from neurodb.datamart.models import MonitoringFinding
from neurodb.fmm import privacy
from neurodb.fmm.ai import FMM_NAMED_FIELDS, facts, fallback, insights, profiles, sections
from neurodb.fmm.models import Insight, Visit
from neurodb.fmm.scope import Scope
from neurodb.integrations import background
from neurodb.watch import grounding

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 5)
YEAR = "year=2026&section="
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}
NON_FOOD = "The partner distributed non-food items to families in both centres during the visit."
PARTS = sections.of(None)  # the parts of the published version (FMS's Lebanon parts)
SECTIONS = [part["key"] for part in sections.text_parts(PARTS)]
SCHEMA = sections.schema(PARTS)


@pytest.fixture
def ai_on():
    from neurodb.watch import people

    people.forget()  # the names read during the refresh, before its visits (and teams) were written
    with override_settings(**AI_ON):
        yield profiles.published()


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


class FakeInsights:
    """``agent.client()``: records each ``responses.create`` call and its client options, and returns
    (or raises) the next scripted answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.options: list[dict] = []

    def client(self):
        def with_options(**options):
            self.options.append(options)
            return api

        api = SimpleNamespace(with_options=with_options, responses=SimpleNamespace(create=self.create))
        return api

    def create(self, **params):
        self.requests.append(params)
        answer = self.answers.pop(0) if self.answers else _answer({})
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def fake_insights_client(monkeypatch):
    def install(*answers):
        fake = FakeInsights(*answers)
        monkeypatch.setattr(agent, "client", fake.client)
        return fake

    return install


def _answer(raw, *, status="completed", reason=None, output=None, tokens=(5000, 800)):
    return SimpleNamespace(
        output_text=raw if isinstance(raw, str) else json.dumps(raw),
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=output or [],
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            input_tokens_details=SimpleNamespace(cached_tokens=1000),
            output_tokens=tokens[1],
        ),
    )


def _scope(query=YEAR):
    return Scope.from_params(QueryDict(query), None, TODAY)


def _good(found: facts.Facts) -> dict:
    """An answer whose every sentence and action passes the checks."""
    k = found.payload["kpi"]
    narrative = sorted(found.payload["narratives"])[0]
    visit = sorted(found.payload["visits"])[0]
    section = next(iter(found.payload["sections"].values()))["name"]
    return {
        "coverage_summary": [
            {"text": f"{k['visits']} visits and {k['entities']} entities in the period.", "keys": ["kpi"]}
        ],
        "key_findings": [{"text": "A visit noted how the classes went.", "keys": [narrative]}],
        "challenges": [{"text": "One follow-up action point is overdue.", "keys": ["ap:summary"]}],
        "recommendations": [
            {"text": "Complete the general observation before submitting a report.", "keys": ["rule:R1"]}
        ],
        "action_points": [
            {
                "priority": "High",
                "section": section,
                "partner": "",
                "action": "Follow up the visit's findings with the partner",
                "owner_role": "Education section lead",
                "timeframe": "within 2 weeks",
                "keys": [visit],
            }
        ],
    }


def _keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


# ------------------------------------------------------------------------------------------ the facts
def test_every_entry_of_the_payload_is_citable_and_shares_are_precomputed(built, ai_on):
    found = facts.build(_scope(), ai_on, TODAY)
    payload = found.payload
    for name in ("scope", "kpi", "previous", "action_points", "hact"):
        assert payload[name]["key"] in found.citable
    for name in (
        "rules",
        "issues",
        "sections",
        "offices",
        "partners",
        "modalities",
        "places",
        "visits",
        "narratives",
    ):
        for key, entry in payload[name].items():
            assert entry["key"] == key and found.citable[key] is entry
    assert set(found.citable) >= {"scope", "kpi", "previous", "ap:summary", "hact:2026", "gap:governorates"}
    kpi = payload["kpi"]
    assert (kpi["visits"], kpi["entities"], kpi["entities_rated"]) == (8, 12, 8)
    assert "entities_not_monitored_share" not in kpi and kpi["avg_quality"] == 79.2
    # every share of ratings is over the rated visits (entities); Not monitored is a count apart
    assert (kpi["rated_visits"], kpi["on_track_visits"], kpi["off_track_visits"]) == (5, 3, 2)
    assert (kpi["on_track_share_of_rated"], kpi["off_track_share_of_rated"]) == (60.0, 40.0)
    assert kpi["not_monitored_visits"] == 1 and kpi["entities_off_track_share_of_rated"] == 25.0
    assert payload["rules"]["rule:R1"]["flagged_share"] is not None
    assert payload["previous"]["visits_change"] == kpi["visits"] - payload["previous"]["visits"]
    assert (
        payload["scope"]["period"] == "1 Jan 2026 – 31 Dec 2026"
        and payload["scope"]["filters"] == "All visits"
    )
    assert isinstance(payload["notes"], list) and payload["notes"]
    # every float rounded to one decimal by code, and no Decimal left
    floats = [v for e in found.citable.values() for v in e.values() if isinstance(v, float)]
    assert floats and all(round(v, 1) == v for v in floats)
    assert found.sent == {
        "narratives": 3,
        "narratives_allowed": 20,
        "narratives_withheld": 1,  # the note naming people, with contacts and a link
        "flags": len(payload["issues"]),
        "flags_allowed": 15,  # comp: the compliance depth
        "visits": 8,
    }


def test_the_same_data_gives_the_same_selection_and_hash(built, ai_on):
    first = facts.build(_scope(), ai_on, TODAY)
    again = facts.build(_scope(), ai_on, TODAY)
    assert first.input_hash == again.input_hash and first.payload == again.payload
    assert list(first.payload["narratives"]) == list(again.payload["narratives"])
    assert facts.card_order(_scope(), 15) == facts.card_order(_scope(), 15)
    # the version is part of the hash: another output limit is another input
    draft = profiles.draft_from(ai_on, None, "More room", max_output_tokens=5000)
    assert facts.build(_scope(), draft, TODAY).input_hash != first.input_hash


def test_notes_off_track_first_then_urgent_visits_and_never_the_same_text_twice(built, ai_on):
    order = facts.narrative_order(_scope())
    ratings = [row["rating"] for row in order]
    bad = [r for r in ratings if r in ("off_track", "constrained")]
    assert ratings[: len(bad)] == bad
    notes = facts.sample_narratives(_scope(), 20)
    texts = [n["text"] for n in notes]
    assert len(texts) == len(set(texts))  # the two programme documents of 1726 share their note
    assert all(len(t) >= facts.NARRATIVE_MIN_CHARS for t in texts)
    assert facts.sample_narratives(_scope(), 1) == notes[:1]


def test_visit_cards_red_first_with_a_cap_per_section(built, ai_on):
    order = facts.card_order(_scope(), 3)
    assert order[0] == Visit.objects.get(key="1723").pk and len(order) == 3  # the most urgent (amber) first
    cards = facts.cards(_scope(), 15)
    assert len(cards) == 8 and {"team", "visit_lead", "narrative"}.isdisjoint(_keys(cards))


def test_scope_of_gives_back_the_same_filter(built):
    for query in (
        YEAR,
        "section=",
        "preset=last_90&section=Education",
        "preset=custom&from=2026-02-01&to=2026-06-30",
    ):
        scope = _scope(query)
        assert insights.scope_of(scope.canonical(), TODAY).hash() == scope.hash()


# ------------------------------------------------------------------------------------------ the request
def test_the_request_is_strict_unstored_and_identifies_only_a_person(
    built, ai_on, fake_insights_client, viewer
):
    found = facts.build(_scope(), ai_on, TODAY)
    api = fake_insights_client(_answer(_good(found)), _answer(_good(found)))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert row.status == Insight.Status.OK, (row.reason, row.dropped)
    sent = api.requests[0]
    assert sent["text"]["format"] == {
        "type": "json_schema",
        "name": "fmm_brief",
        "strict": True,
        "schema": SCHEMA,
    }
    schema = json.dumps(SCHEMA)
    assert "$defs" not in schema and "maxItems" not in schema
    assert sent["store"] is False and sent["prompt_cache_key"] == "neurodb-fmm-insights"
    assert sent["reasoning"] == {"effort": "low"} and sent["max_output_tokens"] == 8000
    assert sent["model"] == profiles.model_of(ai_on) and sent["instructions"] == profiles.compose(
        ai_on, "insights"
    )
    assert json.loads(sent["input"][0]["content"]) == found.payload
    assert sent["temperature"] == 0.3 and "top_p" not in sent  # v1: temperature 0.30, top-p not set
    assert "safety_identifier" not in sent  # nobody asked for a nightly brief
    assert api.options[0] == {"timeout": 90, "max_retries": 1}
    # a person's Regenerate carries their keyed hash
    insights.generate(_scope("section="), user=viewer, trigger=Insight.Trigger.USER, today=TODAY)
    assert api.requests[1]["safety_identifier"] == agent.safety_identifier(viewer)


def test_a_brief_is_kept_with_what_was_used(built, ai_on, fake_insights_client):
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    row.refresh_from_db()
    assert row.status == Insight.Status.OK and row.called
    assert (row.input_tokens, row.cached_tokens, row.output_tokens) == (5000, 1000, 800)
    assert row.sent == found.sent and row.sent_payload == found.payload and row.input_hash == found.input_hash
    assert row.sampling["temperature"]["state"] == "applied" and row.sampling["top_p"]["state"] == "not_set"
    assert (row.model, row.effort, row.max_output_tokens, row.rules_version) == (
        profiles.model_of(ai_on),
        "low",
        8000,
        built.details["rules_version"],
    )
    narrative = sorted(found.payload["narratives"])[0]
    assert (
        narrative.split(":")[1] in row.cited_keys and sorted(found.payload["visits"])[0][6:] in row.cited_keys
    )
    ledger = AIUsage.objects.get(feature=usage.FMM)
    assert (ledger.calls, ledger.input_tokens, ledger.cached_tokens, ledger.output_tokens) == (
        1,
        4000,
        1000,
        800,
    )


# ------------------------------------------------------------------------------------------ the checks
def test_grounding_drops_what_the_facts_do_not_say_and_keeps_named_words(built, ai_on):
    MonitoringFinding.objects.filter(datamart_id=103).update(narrative_finding=NON_FOOD)
    found = facts.build(_scope(), ai_on, TODAY)
    narrative = next(k for k, n in found.payload["narratives"].items() if "non-food" in n["text"])
    raw = {
        "coverage_summary": [
            {"text": "There were 777 visits in the period.", "keys": ["kpi"]},
            {"text": "Eight visits were made.", "keys": ["nowhere:1"]},
            {"text": "Karim Canary led most visits.", "keys": ["kpi"]},
            {"text": "Write to karim@example.org for more.", "keys": ["kpi"]},
        ],
        "key_findings": [
            {"text": "See https://evil.example/x for the photos.", "keys": ["kpi"]},
            {"text": "**Classes** were full.", "keys": ["kpi"]},
            {"text": "Some items arrived late.", "keys": ["kpi"]},
            {"text": "A visit noted that non-food items reached families.", "keys": [narrative]},
        ],
        "challenges": [],
        "recommendations": [{"text": "Keep it up.", "keys": []}],
        "action_points": [],
    }
    sections, actions, dropped = insights.validate(raw, found, TODAY)
    assert sections["coverage_summary"] == []
    assert [s["text"] for s in sections["key_findings"]] == [
        "A visit noted that non-food items reached families."
    ]
    assert dropped == {
        "number": 1,
        "unknown_key": 1,
        "person": 1,
        "email": 1,
        "link": 1,
        "markup": 1,
        "wording": 1,
        "no_keys": 1,
    }


def _strings(value):
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def test_every_visit_the_payload_names_can_be_cited_or_rides_on_its_note(built, ai_on, monkeypatch):
    monkeypatch.setattr(facts, "VISIT_CARDS", 1)
    few = profiles.draft_from(ai_on, None, "One flag", comparison_visits=1)
    found = facts.build(_scope(), few, TODAY)
    carded = set(found.payload["visits"])
    # comp (the compliance depth) is the number of quality flags; the flag's examples are sent in full
    assert len(found.payload["issues"]) == 1 and found.sent["flags"] == 1
    listed = {k for issue in found.payload["issues"].values() for k in issue["visit_keys"]}
    assert listed and listed <= carded and all(k in found.citable for k in listed)
    assert len(carded) <= 1 + len(listed)
    # a note's visit not sent in full: citing it beside its note keeps the sentence, on the note alone
    note = next(n for n in found.payload["narratives"].values() if n["visit"] not in carded)
    raw = {
        "key_findings": [
            {"text": "A visit noted how the classes went.", "keys": [note["key"], note["visit"]]},
            {"text": "A visit was made.", "keys": [note["visit"]]},  # on its own: still unknown
        ],
        "action_points": [
            {
                "priority": "High",
                "section": "All sections",
                "partner": "",
                "action": "Follow up the visit's findings with the partner",
                "owner_role": "Section lead",
                "timeframe": "within 2 weeks",
                "keys": [note["visit"], note["key"]],
            }
        ],
    }
    sections, actions, dropped = insights.validate(raw, found, TODAY)
    assert sections["key_findings"] == [
        {"text": "A visit noted how the classes went.", "keys": [note["key"]]}
    ]
    assert [a["keys"] for a in actions] == [[note["key"]]]
    assert dropped == {"unknown_key": 1}
    assert insights.cited_visits(sections, actions) == [note["visit"][6:]]
    # with every visit sent in full, no string of the payload names a visit that cannot be cited
    whole = facts.build(_scope(), ai_on, TODAY)
    named = {s for s in _strings(whole.payload) if s.startswith("visit:")}
    assert named and named <= set(whole.citable)


def test_priority_actions_keep_their_enums_and_replace_unknown_sections_and_named_owners(built, ai_on):
    found = facts.build(_scope(), ai_on, TODAY)
    visit = sorted(found.payload["visits"])[0]

    def action(**changes):
        return {
            "priority": "High",
            "section": "Education",
            "action": "Follow up the visit's findings",
            "owner_role": "Education section lead",
            "timeframe": "within 2 weeks",
            "keys": [visit],
            **changes,
        }

    raw = {
        "action_points": [
            action(),
            action(section="Nowhere office", owner_role="Karim Canary"),
            action(owner_role="The 25 monitors"),
            action(priority="Urgent"),
            action(timeframe="soon"),
            action(keys=[]),
            action(keys=["visit:none"]),
            action(action="Call 999 people"),
        ]
    }
    _sections, actions, dropped = insights.validate(raw, found, TODAY)
    assert [(a["section"], a["owner_role"]) for a in actions] == [
        ("Education", "Education section lead"),
        ("All sections", "Section lead"),
        ("Education", "Section lead"),
    ]
    assert dropped == {
        "section_replaced": 1,
        "owner_replaced": 2,
        "malformed": 2,
        "too_many": 3,  # at most 5 action points are read (the version's limit)
    }
    raw["action_points"] = raw["action_points"][4:]
    _sections, actions, dropped = insights.validate(raw, found, TODAY)
    assert actions == [] and dropped == {"malformed": 1, "no_keys": 1, "unknown_key": 1, "number": 1}


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_answer({"coverage_summary": []}, status="incomplete", reason="max_output_tokens"), "cut off"),
        (_answer("", output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])]), "refused"),
        (_answer("this is not JSON"), "malformed"),
    ],
)
def test_a_cut_off_refused_or_malformed_answer_fails_and_the_earlier_brief_stays(
    built, ai_on, fake_insights_client, answer, reason
):
    earlier = Insight.objects.create(
        scope_hash=_scope().hash(),
        scope=_scope().canonical(),
        scope_label="earlier",
        trigger=Insight.Trigger.NIGHTLY,
        version=ai_on,
        rules_version=1,
        input_hash="older data",
        status=Insight.Status.OK,
        sections={"coverage_summary": [{"text": "Earlier.", "keys": ["kpi"]}]},
    )
    fake_insights_client(answer)
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert (row.status, row.reason, row.called) == (Insight.Status.FAILED, reason, True)
    assert insights.current(_scope()).insight == earlier


def test_nothing_kept_gives_the_code_written_brief(built, ai_on, fake_insights_client):
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer({name: [{"text": "777 visits.", "keys": ["kpi"]}] for name in SECTIONS}))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert row.status == Insight.Status.FALLBACK and row.reason == insights.NOTHING_KEPT
    assert row.dropped["number"] == 4
    written = fallback.brief(found, PARTS)
    assert (
        row.sections["coverage_summary"] == written["sections"]["coverage_summary"]
        and row.actions == written["actions"]
    )
    assert row.sections["notes"]["key_findings"] == fallback.FINDINGS_NOTE


def test_every_sentence_of_the_code_written_brief_passes_the_checks(built, ai_on):
    for query in (
        YEAR,
        "year=2026&section=Education",
        "year=2025&section=",
        "year=2026&section=&rating=off_track",
    ):
        found = facts.build(_scope(query), None, TODAY, narratives=False)
        written = fallback.brief(found)
        parts = written["sections"]
        assert parts["key_findings"] == [] and parts["coverage_summary"]
        for name in SECTIONS:
            for sentence in parts[name]:
                cited = [found.citable[k] for k in sentence["keys"]]
                verdict = grounding.check(sentence["text"], cited, TODAY, privacy.names(), FMM_NAMED_FIELDS)
                assert verdict, (query, sentence, verdict)
        raw = {**parts, "action_points": written["actions"]}
        kept, actions, dropped = insights.validate(raw, found, TODAY)
        assert actions == written["actions"] and dropped == {}, (query, dropped)
        assert all(kept[n] == parts[n] for n in SECTIONS)


def test_the_code_written_brief_words(built):
    written = fallback.brief(facts.build(_scope(), None, TODAY, narratives=False))
    actions = written["actions"]
    written = written["sections"]
    texts = [s["text"] for s in written["coverage_summary"]]
    assert texts[:3] == [
        "8 visits and 12 entities in the period.",
        # shares of the rated visits only; Not monitored (planned, not conducted) apart, never a share
        "Of the 5 rated visits, 3 were On track (60.0%), 0 Constrained (0.0%) and 2 Off track (40.0%).",
        "1 visit was Not monitored (planned, not conducted), counted apart from the rated visits.",
    ]
    assert texts[3] == "6 visits were reported, 1 is in progress and 0 are planned."
    assert texts[4] == "The average quality score was 79.2% on 6 scored visits."
    challenges = [s["text"] for s in written["challenges"]]
    assert challenges[0].startswith("R23 flagged 3 visits: Visit location not among registered PD locations")
    assert not [t for t in texts + challenges if "monitoring gap" in t or "not monitored (" in t]
    assert written["recommendations"][0]["text"] == fallback.RULE_ADVICE["R1"]  # the most flagged rule
    action = actions[0]
    assert (action["priority"], action["owner_role"], action["timeframe"]) == (
        "Medium",
        "Section lead",
        "within 2 weeks",
    )


# ------------------------------------------------------------------------------------------ the gates and the cache
def test_a_brief_written_from_the_same_input_is_reused_without_a_call(
    built, ai_on, fake_insights_client, viewer
):
    found = facts.build(_scope(), ai_on, TODAY)
    api = fake_insights_client(_answer(_good(found)))
    first = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    running = insights.start(_scope(), ai_on, viewer)
    again = insights.generate(_scope(), user=viewer, insight=running, today=TODAY)
    assert again == first and again.reused and len(api.requests) == 1
    running.refresh_from_db()
    assert (running.status, running.reason, running.called) == (Insight.Status.SKIPPED, "up to date", False)
    from neurodb.fmm.ai import budget

    assert budget.quota("insights", viewer) == (0, 5)  # a brief found up to date uses no quota


def test_a_new_version_or_rules_version_asks_again(built, ai_on, fake_insights_client):
    found = facts.build(_scope(), ai_on, TODAY)
    api = fake_insights_client(*[_answer(_good(found))] * 3)
    insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    for run in SyncRun.objects.filter(job=built.job):  # the full refresh and the rescore after it
        SyncRun.objects.filter(pk=run.pk).update(details={**run.details, "rules_version": 3})
    insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert len(api.requests) == 2
    draft = profiles.draft_from(ai_on, None, "Shorter", narratives_sampled=10)
    profiles.publish(draft, None)
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert len(api.requests) == 3 and row.version == draft


def test_too_few_visits_or_the_ai_off_make_no_call(built, ai_on, fake_insights_client, viewer):
    api = fake_insights_client()
    one = _scope("year=2026&section=&q=FM-2026-022")
    row = insights.generate(one, trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert (
        row.status == Insight.Status.SKIPPED
        and row.reason == "Too few visits in this filter for an AI brief (3 needed)."
    )
    assert row.pk is None and api.requests == []
    with override_settings(FMM_AI=False):
        row = insights.generate(_scope(), user=viewer, trigger=Insight.Trigger.USER, today=TODAY)
    assert (row.status, row.reason) == (Insight.Status.SKIPPED, "AI is switched off") and api.requests == []
    draft = profiles.draft_from(ai_on, None, "Briefs off", insights_enabled=False)
    profiles.publish(draft, None)
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert row.reason == "switched off by an administrator" and api.requests == []


def test_the_effort_or_the_service_failing_is_kept_with_a_reason(built, ai_on, fake_insights_client):
    import httpx2

    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    effort = openai.BadRequestError(
        "Unsupported value: 'reasoning.effort'",
        response=httpx2.Response(400, request=request),
        body={
            "error": {
                "param": "reasoning.effort",
                "message": "Unsupported value",
                "code": "unsupported_value",
            }
        },
    )
    busy = openai.RateLimitError("Busy", response=httpx2.Response(429, request=request), body=None)
    fake_insights_client(effort, busy)
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert (row.status, row.reason, row.called) == ("failed", f"effort not accepted by {row.model}", False)
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert (
        row.status == "failed"
        and row.reason == "The AI service is busy right now. Please try again in a minute."
    )


# ------------------------------------------------------------------------------------------ what the page shows
def test_the_badges(built, ai_on, fake_insights_client):
    with override_settings(FMM_AI=False):
        shown = insights.current(_scope())
    assert shown.insight is None and shown.fallback["sections"]["coverage_summary"]
    assert shown.badge == "Written by NeuroDB from the figures — AI not used: AI is switched off"
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert insights.current(_scope()).badge == "Up to date"
    Insight.objects.filter(pk=row.pk).update(input_hash="older")
    badge = insights.current(_scope()).badge
    assert (
        badge.startswith("Written ")
        and badge.endswith("— the data has changed since")
        and "from data of" in badge
    )
    profiles.publish(profiles.draft_from(ai_on, None, "v2"), None)
    shown = insights.current(_scope())
    assert shown.insight.pk == row.pk and shown.badge == "Written with prompt v4 (now v5)"


def test_regenerate_starts_a_background_brief_and_the_card_polls_it(
    built, ai_on, client_viewer, viewer, started, fake_insights_client, django_capture_on_commit_callbacks
):
    url = f"{reverse('fmm:insights')}?{YEAR}"
    with django_capture_on_commit_callbacks(execute=True):
        response = client_viewer.post(url, HTTP_HX_REQUEST="true")
    row = Insight.objects.get()
    html = response.content.decode()
    assert (row.status, row.trigger, row.created_by) == ("running", "user", viewer)
    assert started == [("fmm_insights", "--insight", str(row.pk), "--triggered-by", f"user:{viewer.pk}")]
    assert (
        'hx-trigger="every 3s"' in html
        and f"running={row.pk}" in html
        and "Writing… about 30 seconds" in html
    )
    # a second click polls the same brief
    with django_capture_on_commit_callbacks(execute=True):
        again = client_viewer.post(url, HTTP_HX_REQUEST="true").content.decode()
    assert Insight.objects.count() == 1 and len(started) == 1 and f"running={row.pk}" in again
    # the background process writes it, then the card shows it
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    call_command("fmm_insights", "--insight", str(row.pk))
    row.refresh_from_db()
    assert row.status == Insight.Status.OK
    card = client_viewer.get(f"{url}&running={row.pk}").content.decode()
    assert "every 3s" not in card and "AI monitoring insights" in card
    assert "A visit noted how the classes went." in card and "Priority Action Points" in card
    assert "[PRIORITY: High]" in card and "What was sent" in card
    assert "Up to date" in card and "1 of 5 today" in card and 'class="visit-chip"' in card
    assert "temp 0.30 · applied" in card and "top-p · not set (API default 1.00)" in card
    assert "narr 3/20" in card and f"comp {row.sent['flags']}/15" in card and "prompt v4" in card


def test_a_stopped_brief_is_shown_as_stopped(built, ai_on, client_viewer, viewer):
    row = insights.start(_scope(), ai_on, viewer)
    Insight.objects.filter(pk=row.pk).update(created_at=timezone.now() - datetime.timedelta(seconds=200))
    html = client_viewer.get(f"{reverse('fmm:insights')}?{YEAR}&running={row.pk}").content.decode()
    row.refresh_from_db()
    assert (row.status, row.reason) == ("failed", "stopped")
    assert "The last attempt stopped; you can try again." in html and "every 3s" not in html


def test_the_card_without_the_ai_shows_the_code_written_brief(built, client_viewer):
    html = client_viewer.get(f"{reverse('fmm:insights')}?{YEAR}").content.decode()
    assert "Written by NeuroDB from the figures — AI not used: AI is switched off" in html
    assert "8 visits and 12 entities in the period" in html and fallback.FINDINGS_NOTE in html
    assert "AI switched off" in html and "disabled" in html  # Regenerate is disabled, with the reason
    assert "What was sent" not in html
    response = client_viewer.post(f"{reverse('fmm:insights')}?{YEAR}")  # pressed anyway: kept, no call
    assert response.status_code == 200 and "AI switched off" in response.content.decode()
    assert list(Insight.objects.values_list("status", "reason", "called")) == [
        ("skipped", "AI is switched off", False)
    ]


def test_the_card_follows_only_a_brief_of_its_own_filter(built, ai_on, client_viewer, viewer, admin_user):
    other = insights.start(_scope("year=2026&section=Education"), ai_on, viewer)
    test = insights.start(_scope(), ai_on, admin_user, Insight.Trigger.TEST)
    for row in (other, test):
        html = client_viewer.get(f"{reverse('fmm:insights')}?{YEAR}&running={row.pk}").content.decode()
        assert "every 3s" not in html and "Written by NeuroDB from the figures" in html


def test_a_code_written_brief_from_the_same_data_may_be_regenerated(
    built, ai_on, client_viewer, fake_insights_client
):
    fake_insights_client(_answer({name: [] for name in [*SECTIONS, "action_points"]}))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    assert row.status == Insight.Status.FALLBACK
    html = client_viewer.get(f"{reverse('fmm:insights')}?{YEAR}").content.decode()
    assert "Up to date" in html  # the badge: written from the same data
    assert "disabled" not in html.split("Regenerate")[0].rsplit("<button", 1)[1]
    # the AI's brief from the same data: nothing to regenerate
    Insight.objects.filter(pk=row.pk).update(status=Insight.Status.PARTIAL, actions=[])
    html = client_viewer.get(f"{reverse('fmm:insights')}?{YEAR}").content.decode()
    assert "disabled" in html.split("Regenerate")[0].rsplit("<button", 1)[1]
    assert "No priority action of the AI passed the checks" in html
    assert "No visit of this filter needs urgent follow-up" not in html


def test_the_brief_card_stays_within_its_queries(
    built, ai_on, client_viewer, fake_insights_client, django_assert_max_num_queries
):
    url = f"{reverse('fmm:insights')}?{YEAR}"
    client_viewer.get(url)  # warm the figures
    # the AI on, no brief yet: the code-written one, and every gate of Regenerate (quota, budget)
    with django_assert_max_num_queries(28):
        client_viewer.get(url)
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    client_viewer.get(url)
    with django_assert_max_num_queries(20):  # a brief up to date
        assert "Up to date" in client_viewer.get(url).content.decode()


def test_a_regenerate_over_the_quota_is_refused_and_kept(built, ai_on, client_viewer, viewer, started):
    for _ in range(5):
        Insight.objects.create(
            scope_hash="other", scope={}, scope_label="x", trigger="user", version=ai_on, rules_version=1,
            input_hash="i", status="ok", called=True, created_by=viewer,
        )  # fmt: skip
    html = client_viewer.post(f"{reverse('fmm:insights')}?{YEAR}").content.decode()
    assert "5 of 5 today — resets at midnight" in html and started == []
    assert Insight.objects.filter(status=Insight.Status.LIMITED, created_by=viewer).count() == 1


def test_what_was_sent(built, ai_on, client_viewer, fake_insights_client):
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    html = client_viewer.get(
        reverse("fmm:insight_sent", args=[row.pk]), HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "What was sent to the AI" in html and "3 monitors' notes" in html
    assert (
        "[name withheld]" in html
        and "Rania Canary" not in html
        and "&quot;key&quot;: &quot;kpi&quot;" in html
    )
    Insight.objects.filter(pk=row.pk).update(sent_payload=None)
    html = client_viewer.get(reverse("fmm:insight_sent", args=[row.pk])).content.decode()
    assert "kept for 30 days only" in html
    Insight.objects.filter(pk=row.pk).update(trigger=Insight.Trigger.TEST)  # a test run: administrators only
    assert client_viewer.get(reverse("fmm:insight_sent", args=[row.pk])).status_code == 404


def test_the_visit_page_and_the_visits_table_show_what_the_brief_cites(
    built, ai_on, client_viewer, fake_insights_client
):
    landing = _scope("section=")
    found = facts.build(landing, ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    row = insights.generate(landing, trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    visit = sorted(found.payload["visits"])[0][6:]
    page = client_viewer.get(reverse("fmm:visit", args=[visit])).content.decode()
    assert "Cited in the AI brief" in page and "Follow up the visit&#x27;s findings with the partner" in page
    table = client_viewer.get(f"{reverse('fmm:visits')}?section=").content.decode()
    assert table.count("cited in the AI brief</span>") == len(row.cited_keys)
    other = next(v.key for v in Visit.objects.all() if v.key not in row.cited_keys)
    assert (
        "Cited in the AI brief" not in client_viewer.get(reverse("fmm:visit", args=[other])).content.decode()
    )


def test_the_insights_tab_stays_within_its_queries(built, client_viewer, django_assert_max_num_queries):
    client_viewer.get(reverse("fmm:dashboard"), {"year": "2026"})  # warm the figures
    with django_assert_max_num_queries(20):
        client_viewer.get(
            reverse("fmm:dashboard"), {"year": "2026", "tab": "insights"}, HTTP_HX_REQUEST="true"
        )
    with django_assert_max_num_queries(20):
        client_viewer.get(reverse("fmm:visits"), {"year": "2026", "section": ""})


# ------------------------------------------------------------------------------------------ nightly
def _sections(*names_and_codes):
    from neurodb.accounts.models import Section, User

    out = []
    for n, (name, code) in enumerate(names_and_codes):
        section = Section.objects.get_or_create(name=name, defaults={"code": code})[0]
        User.objects.create_user(username=f"u{n}", password="pass-123456-x", section=section)
        out.append(section)
    return out


def test_nightly_scopes_are_what_people_land_on(built, ai_on):
    from neurodb.accounts.models import User

    wash = ["WASH / Water, Sanitation and Hygiene", "WASH Cluster"]
    keys = list(Visit.objects.filter(end_date__year=2026).order_by("key").values_list("key", flat=True))
    for key, name in zip(keys[:4], [wash[0], wash[1], wash[0], wash[1]], strict=True):
        Visit.objects.filter(key=key).update(section_names=[name])
    _sections(("Education", "EDU"), ("WASH", "W1"), ("Water and sanitation", "WASH"))
    scopes = insights.nightly_scopes(TODAY)
    hashes = [s.hash() for s in scopes]
    assert [s.preset for s in scopes] == ["this_year", "this_year", "last_90"]  # Education has too few now
    assert scopes[0].sections == () and set(scopes[1].sections) == set(wash)
    for user in User.objects.filter(username__in=("u1", "u2")):  # two sections, one brief
        assert Scope.from_params(QueryDict(""), user, TODAY).hash() in hashes
    assert Scope.from_params(QueryDict(""), None, TODAY).hash() == hashes[0]
    with override_settings(FMM_NIGHTLY_MAX_INSIGHTS=2):
        assert len(insights.nightly_scopes(TODAY)) == 2


def test_the_nightly_run_writes_reuses_and_stops_at_the_budget(built, ai_on, fake_insights_client):
    scopes = [_scope(), _scope("year=2026&section=Education")]
    answers = [_answer(_good(facts.build(s, ai_on, TODAY))) for s in scopes]
    api = fake_insights_client(*answers)
    run = insights.write_nightly(scopes, "test", TODAY)
    assert run.status == SyncRun.Status.SUCCEEDED, run.details
    assert (run.rows_in, run.rows_written, run.rows_failed) == (2, 2, 0)
    assert run.details["generated"] == 2 and run.details["tokens"] == 2 * 5800 and len(api.requests) == 2
    run = insights.write_nightly(scopes, "test", TODAY)
    assert run.details["reused"] == 2 and len(api.requests) == 2
    with override_settings(FMM_DAILY_TOKEN_CAP=1):
        run = insights.write_nightly([_scope("year=2026&section=&rating=off_track"), *scopes], "test", TODAY)
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 3 and run.details["limited"] == 1
    assert len(api.requests) == 2


def test_the_nightly_run_with_the_ai_off_writes_nothing(built):
    call_command("fmm_insights", "--triggered-by", "test")
    run = SyncRun.objects.get(job=SyncRun.Job.FMM_INSIGHTS)
    assert run.status == SyncRun.Status.SUCCEEDED and run.details["skipped"] == "AI is off"
    assert not Insight.objects.exists()


def test_the_dry_run_prints_sizes_without_a_call(built, ai_on, fake_insights_client, capsys):
    api = fake_insights_client()
    call_command("fmm_insights", "--dry-run", "--scopes", "country")
    out = capsys.readouterr().out
    assert "characters, about" in out and "tokens with the output" in out and api.requests == []
    assert not SyncRun.objects.filter(job=SyncRun.Job.FMM_INSIGHTS).exists()


def test_housekeeping(built, ai_on):
    now = timezone.now()

    def brief(days, status="ok", payload=True):
        row = Insight.objects.create(
            scope_hash=f"h{days}{status}", scope={}, scope_label="x", trigger="nightly", version=ai_on,
            rules_version=1, input_hash="i", status=status, sent_payload={"kpi": {}} if payload else None,
        )  # fmt: skip
        Insight.objects.filter(pk=row.pk).update(created_at=now - datetime.timedelta(days=days))
        return row

    fresh, old, limited, ancient = brief(2), brief(31), brief(31, "limited", False), brief(181)
    stale = brief(0, "running", False)
    Insight.objects.filter(pk=stale.pk).update(created_at=now - datetime.timedelta(minutes=5))
    from neurodb.fmm.models import ChatQuestion

    def question(days):
        row = ChatQuestion.objects.create(
            conversation=uuid.uuid4(), scope_hash="s", version=ai_on, question="q", status="answered"
        )
        ChatQuestion.objects.filter(pk=row.pk).update(created_at=now - datetime.timedelta(days=days))
        return row

    kept_question, old_question = question(179), question(181)
    done = insights.housekeeping(now)
    assert done == {
        "stopped": 1,
        "payloads_blanked": 2,
        "refused_deleted": 1,
        "deleted": 1,
        "chat_deleted": 1,
    }
    assert list(ChatQuestion.objects.values_list("pk", flat=True)) == [kept_question.pk]
    assert old_question.pk != kept_question.pk
    assert Insight.objects.get(pk=fresh.pk).sent_payload == {"kpi": {}}
    assert Insight.objects.get(pk=old.pk).sent_payload is None
    assert not Insight.objects.filter(pk__in=[limited.pk, ancient.pk]).exists()
    assert Insight.objects.get(pk=stale.pk).reason == "stopped"


# ------------------------------------------------------------------------------------------ the admin
def test_a_test_run_starts_in_the_background_and_its_page_follows_it(
    built, ai_on, admin_client, admin_user, started, fake_insights_client, django_capture_on_commit_callbacks
):
    draft = profiles.draft_from(ai_on, admin_user, "Try a shorter brief", narratives_sampled=5)
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            reverse("admin:fmm_promptversion_test_run", args=[draft.pk]), {"url": f"/fmm/?{YEAR}"}
        )
    row = Insight.objects.get()
    assert (row.trigger, row.version, row.status) == ("test", draft, "running")
    assert response.url == reverse("admin:fmm_insight_change", args=[row.pk])
    assert started == [("fmm_insights", "--insight", str(row.pk), "--triggered-by", f"admin:{admin_user.pk}")]
    page = admin_client.get(response.url).content.decode()
    assert 'http-equiv="refresh"' in page and "Writing" in page
    found = facts.build(_scope(), draft, TODAY)
    fake_insights_client(_answer(_good(found)))
    call_command("fmm_insights", "--insight", str(row.pk))
    page = admin_client.get(response.url).content.decode()
    assert 'http-equiv="refresh"' not in page and "A visit noted how the classes went." in page
    assert insights.current(_scope()).insight is None  # a test run is never shown on the page
    from neurodb.fmm.ai import budget

    assert budget.quota("insights", admin_user) == (0, 5)  # nor counted in a person's quota
    assert draft.status == "draft" and profiles.published() == ai_on


def test_a_test_run_is_shown_beside_the_brief_people_see(built, ai_on, admin_client, fake_insights_client):
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)), _answer(_good(found)), _answer(_good(found)))
    seen = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    other_test = insights.generate(
        _scope(), trigger=Insight.Trigger.TEST, today=TODAY
    )  # of the published one
    draft = profiles.draft_from(ai_on, None, "Try")
    row = insights.generate(_scope(), version=draft, trigger=Insight.Trigger.TEST, today=TODAY)
    assert seen.status == other_test.status == row.status == Insight.Status.OK
    response = admin_client.get(reverse("admin:fmm_insight_change", args=[row.pk]))
    assert response.context["beside"] == seen


def test_the_preview_shows_the_facts_without_a_call(built, ai_on, admin_client, monkeypatch):
    monkeypatch.setattr(agent, "client", lambda: (_ for _ in ()).throw(AssertionError("called")))
    before = Insight.objects.count()
    html = admin_client.get(
        reverse("admin:fmm_promptversion_preview", args=[ai_on.pk]), {"url": f"/fmm/?{YEAR}"}
    ).content.decode()
    assert "The brief: the facts sent for this filter" in html and "&quot;key&quot;: &quot;kpi&quot;" in html
    assert (
        "Monitors' notes sent: 3 of 20 allowed (1 withheld" in html
        and "Quality flags sent (compliance depth): " in html
        and "Visits sent in full: 8" in html
    )
    assert "input tokens" in html and "Start the test run" in html
    assert Insight.objects.count() == before
    with override_settings(AI_PRICE_INPUT_PER_MTOK="1.25", AI_PRICE_OUTPUT_PER_MTOK="10"):
        html = admin_client.get(reverse("admin:fmm_promptversion_preview", args=[ai_on.pk])).content.decode()
    assert "per brief" in html


def test_the_insight_admin_lists_briefs_and_shows_the_payload_to_administrators_only(
    built, ai_on, admin_client, fake_insights_client, viewer
):
    found = facts.build(_scope(), ai_on, TODAY)
    fake_insights_client(_answer(_good(found)))
    row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    listing = admin_client.get(reverse("admin:fmm_insight_changelist")).content.decode()
    assert "1 Jan – 31 Dec 2026" in listing and "5,000 / 800" in listing
    page = admin_client.get(reverse("admin:fmm_insight_change", args=[row.pk])).content.decode()
    assert "This brief" in page and "Sent payload" in page
    model_admin = site._registry[Insight]
    assert "sent_payload" not in model_admin.get_fields(SimpleNamespace(user=viewer), row)
    assert not model_admin.has_change_permission(SimpleNamespace(user=viewer))


# ------------------------------------------------------------------------------------------ the wire
def test_the_strict_format_goes_through_the_real_sdk(built, ai_on, monkeypatch):
    from tests.assistant.openai_mock import MockResponsesServer, completed, message
    from tests.assistant.openai_mock import usage as mock_usage

    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    for var in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"):
        monkeypatch.delenv(var, raising=False)
    found = facts.build(_scope(), ai_on, TODAY)
    server = MockResponsesServer(
        [completed([message("msg_1", json.dumps(_good(found)))], usage_=mock_usage(1200, 300, cached=200))]
    ).start()
    try:
        monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
        row = insights.generate(_scope(), trigger=Insight.Trigger.NIGHTLY, today=TODAY)
    finally:
        server.stop()
    assert row.status == Insight.Status.OK, (row.reason, row.dropped)
    assert (row.input_tokens, row.cached_tokens, row.output_tokens) == (1200, 200, 300)
    body = server.requests[0]["body"]
    assert server.requests[0]["path"] == "/v1/responses" and "stream" not in body
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "fmm_brief",
        "strict": True,
        "schema": SCHEMA,
    }

    def strict(node):  # what strict mode requires of every object: closed, every property required
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False and sorted(node["required"]) == sorted(
                    node["properties"]
                )
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    strict(body["text"]["format"]["schema"])
    assert body["store"] is False and body["temperature"] == 0.3
