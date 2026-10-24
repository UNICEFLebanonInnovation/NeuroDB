"""The KM Database is kept apart from the rest of NeuroDB (SPEC_km §5, the parts that exist from stage 2): no
module outside neurodb/km imports it, its tables relate only to each other and to the user model, no other
model relates to them, its load writes no import run and nothing outside its own tables, the knowledge base,
the site search and NeuroDB Watch never read its rows or learn its authors, and the Help assistant hears only
that a page is the KM Database's."""

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
    assert {m.__name__ for m in km_models} == {"KMVersion", "KMDocument", "KMFinding", "KMOrganisation"}
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
