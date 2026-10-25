"""The KM Database is kept apart from the rest of NeuroDB (SPEC_km §5): no module outside neurodb/km imports
it, its tables relate only to each other and to the user model, no other model relates to them, its load
writes no import run and nothing outside its own tables, Ask NeuroDB, the knowledge base, the library, the
site search and NeuroDB Watch never read its rows or learn its authors, the Help assistant hears only that a
page is the KM Database's, and its own assistant sends nothing but the KM Database's cleaned findings (no
other NeuroDB data, no author, no search box words) and books its calls under its own feature."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.km.models import KMFinding
from tests.km import factory

ROOT = Path(settings.BASE_DIR) / "neurodb"
IMPORT = re.compile(
    r"^\s*(?:import\s+neurodb\.km\b|from\s+neurodb\.km\b|from\s+neurodb\s+import\s+[^#\n]*\bkm\b)", re.M
)


def test_no_module_outside_the_km_app_imports_it():
    found = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "km" not in path.relative_to(ROOT).parts[:1] and IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_its_relations_stay_inside_it():
    km_models = set(apps.get_app_config("km").get_models())
    user = apps.get_model(settings.AUTH_USER_MODEL)
    assert {m.__name__ for m in km_models} == {
        "KMVersion",
        "KMDocument",
        "KMFinding",
        "KMOrganisation",
        "KMQuestion",
    }
    for model in km_models:
        for field in model._meta.get_fields():
            if field.is_relation and field.concrete:
                assert field.related_model in km_models | {user}, (model.__name__, field.name)
    for model in apps.get_models():
        if model in km_models:
            continue
        for field in model._meta.get_fields():
            if field.is_relation and field.concrete:
                assert field.related_model not in km_models, (model.__name__, field.name)


@pytest.mark.django_db
def test_a_load_writes_no_import_run():
    runs = SyncRun.objects.count()
    assert factory.load(factory.workbook()).status == "live"
    assert SyncRun.objects.count() == runs


@pytest.mark.django_db
def test_nothing_else_finds_its_rows_or_learns_its_authors(roles):
    from neurodb.knowledge import search as knowledge_search
    from neurodb.reports import services
    from neurodb.watch import people

    sources = list(people.EXTRA_SOURCES)
    factory.load(factory.workbook())
    assert factory.TOKEN in KMFinding.objects.first().key_finding
    assert knowledge_search.search(factory.TOKEN) == []
    assert services.search(factory.TOKEN, user=None) == []  # the site search reads no KM row
    known = people.known_names(refresh=True)
    assert not {"jane doe", "doe j", "roe r"} & known  # NeuroDB Watch never learns the KM authors
    assert people.EXTRA_SOURCES == sources


@pytest.mark.django_db
def test_the_help_assistant_hears_only_that_the_page_is_the_km_database(client_viewer, monkeypatch):
    from django.test import override_settings

    from neurodb.assistant import agent
    from neurodb.help.models import HelpQuestion
    from tests.assistant.test_assistant import FakeClient, reply, say

    client = FakeClient([reply(say("Fake answer."))])
    monkeypatch.setattr(agent, "client", lambda: client)
    on = {
        "HELP_ENABLED": True,
        "AI_ASSISTANT_ENABLED": True,
        "OPENAI_API_KEY": "x",
        "HELP_PER_USER_PER_DAY": 20,
    }
    finding = factory.finding_id(1, 1)
    with override_settings(**on):
        response = client_viewer.post(
            reverse("help:stream"),
            {
                "question": "How do I read this page?",
                "page": f"/km/findings/{finding}/?v=3",
                "title": "Fake document title 1 · NeuroDB",
            },
        )
        b"".join(response.streaming_content)
    row = HelpQuestion.objects.get()
    assert (row.page, row.page_title) == ("/km/", "KM Database")
    sent = json.dumps(client.requests)
    assert finding not in sent and "Fake document title" not in sent


@pytest.mark.django_db
def test_ask_neurodb_offers_no_km_tool_and_finds_nothing_of_it():
    from django.http import QueryDict

    from neurodb.assistant import agent, tools
    from neurodb.library import services as library

    factory.load(factory.workbook())
    for found in (tools.definitions(), tools.claude_definitions()):
        names = [d.get("name") or d.get("function", {}).get("name") for d in found]
        assert names and not [n for n in names if "km" in n.lower()]
        assert "KM Database" not in json.dumps(found) and factory.TOKEN not in json.dumps(found)
    assert "KM" not in agent.SYSTEM_PROMPT
    result = tools.run("search_knowledge", {"query": factory.TOKEN})
    assert factory.TOKEN not in json.dumps(result) and "Fake key finding" not in json.dumps(result)
    assert list(library.search_resources(QueryDict(f"q={factory.TOKEN}")).object_list) == []


@pytest.fixture
def other_data(db, django_user_model):
    """NeuroDB data with unique fake words, which the KM Database's assistant must never send."""
    from neurodb.cpd.models import CountryProgramme, Indicator, Outcome
    from neurodb.knowledge.models import Document
    from neurodb.partnerships.models import PCA, PartnerOrganization

    django_user_model.objects.create_user(
        username="fakecolleague", first_name="Fakefirst", last_name="Fakelast", password="fake-pass-123456"
    )
    partner = PartnerOrganization.objects.create(etl_id="9", name="Zqpartnerword Fake", partner_type="CSO")
    PCA.objects.create(
        etl_id="91",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA2026091",
        title="Zqpdtitleword fake programme",
        status="active",
    )
    cycle = CountryProgramme.objects.create(name="Fake CP", start_year=2026, end_year=2028, current=True)
    outcome = Outcome.objects.create(programme=cycle, code="1", title="Fake outcome")
    Indicator.objects.create(
        outcome=outcome, code="1.a", title="Zqcpdindicatorword rate", baseline=1, target=2
    )
    Document.objects.create(
        title="Zqknowledgeword fake report", text="Zqknowledgeword about zqkmtoken schools."
    )
    return (
        "Zqpartnerword",
        "Zqpdtitleword",
        "Zqcpdindicatorword",
        "Zqknowledgeword",
        "Fakefirst",
        "Fakelast",
    )


def _km_events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def _never_sent(sent: str, version) -> None:
    from neurodb.assistant import agent

    for never in ("Jane", "Doe", "Roe", "Poe", "Fakename", "fake.example.org", "123 456", "Fake short title"):
        assert never not in sent, never
    assert "Fakeorg University" not in sent  # an author entry (on screen only)
    assert agent.SYSTEM_PROMPT.splitlines()[0] not in sent  # Ask NeuroDB's prompt
    assert "This year is" not in sent and "Today is" not in sent  # nor its date and year lines
    finding_ids = set(KMFinding.objects.values_list("finding_id", flat=True))
    shown = set(re.findall(r"TST-\d{4}-F\d{5}", sent))
    assert shown <= finding_ids
    for record in set(re.findall(r"TST-\d{4}(?!-F)", sent)):
        raise AssertionError(f"a Document ID was sent: {record}")


@pytest.mark.parametrize("scope", ["q=Jane+Doe", "pillar=P1"])
def test_the_km_assistant_sends_only_the_km_database_openai(
    client_viewer, other_data, monkeypatch, settings, scope
):
    from neurodb.assistant import agent, usage
    from neurodb.assistant.models import AssistantQuestion
    from neurodb.help.models import HelpQuestion
    from neurodb.km import ask
    from neurodb.km.models import KMQuestion
    from tests.km.test_ask import FakeAI, answer

    for key, value in {"AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "test-key-not-real"}.items():
        setattr(settings, key, value)
    factory.load(factory.workbook(documents=2, findings=4))
    fake = FakeAI([answer("Fake schools [TST-0001-F00001].")])
    monkeypatch.setattr(agent, "client", fake.client)
    response = client_viewer.post(
        reverse("km:ask_stream"), {"question": "What does the evidence say about zqkmtoken?", "scope": scope}
    )
    final = _km_events(response)[-1]
    assert final["type"] == "done" and "/km/findings/TST-0001-F00001/" in final["html"]
    sent = json.dumps(fake.requests, ensure_ascii=False)
    for word in other_data:
        assert word not in sent, word
    _never_sent(sent, None)
    answer_call = fake.calls()[-1]
    assert "tools" not in answer_call and answer_call["instructions"] == ask.system_prompt()
    row = KMQuestion.objects.get()
    assert "q=" not in row.scope and "Jane" not in row.scope
    assert set(re.findall(r"TST-\d{4}-F\d{5}", answer_call["input"][0]["content"])) == set(row.read)
    # ledgers and logs: the KM Database's own only
    assert usage.today_calls(usage.ASK) == 0 and usage.today_calls(usage.KM) >= 1
    assert AssistantQuestion.objects.count() == 0 and HelpQuestion.objects.count() == 0
    page = client_viewer.get(reverse("assistant:ask")).content.decode()
    assert "zqkmtoken" not in page  # Ask NeuroDB's recent questions never list it


def test_the_km_assistant_sends_only_the_km_database_claude(client_viewer, other_data, claude):
    from tests.assistant import anthropic_mock as am

    factory.load(factory.workbook(documents=2, findings=4))
    server = claude(
        am.turn([am.text(json.dumps({"terms": ["schools"], "groups": []}))]),
        am.turn([am.text(json.dumps({"answer": "Fake schools [TST-0001-F00001].", "covered": "yes"}))]),
    )
    response = client_viewer.post(
        reverse("km:ask_stream"),
        {"question": "What does the evidence say about zqkmtoken?", "scope": "q=Jane+Doe"},
    )
    assert _km_events(response)[-1]["type"] == "done"
    sent = json.dumps([r["body"] for r in server.requests], ensure_ascii=False)
    for word in other_data:
        assert word not in sent, word
    _never_sent(sent, None)
    for request in server.requests:
        assert "tools" not in request["body"]
