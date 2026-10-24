"""The KM Database is kept apart from the rest of NeuroDB (SPEC_km §5, the parts that exist from stage 1): no
module outside neurodb/km imports it, its tables relate only to each other and to the user model, no other
model relates to them, and its load writes no import run and nothing outside its own tables."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings

from neurodb.core.models import SyncRun
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
    assert {m.__name__ for m in km_models} == {"KMVersion", "KMDocument", "KMFinding"}
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
