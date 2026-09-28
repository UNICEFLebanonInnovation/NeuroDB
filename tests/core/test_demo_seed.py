"""The demo seed writes figures that agree with each other (local copy only; never runs in production)."""

import datetime as dt

import pytest
from django.core.management import call_command
from django.db.models import F, Sum

from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.datamart import models as dm


@pytest.mark.django_db
def test_demo_seed_is_self_consistent():
    call_command("seed_demo", password="test-only-pass", months=1)

    # a run's rows read = rows written + rows failed
    runs = SyncRun.objects.exclude(rows_in=F("rows_written") + F("rows_failed"))
    assert not runs.exists(), list(runs.values("job", "status", "rows_in", "rows_written", "rows_failed"))
    # national population = the sum of the governorates (and of the age bands)
    for category in ("total", "children"):
        base = PopulationFigure.objects.filter(year=2025, category=category, age_group="", sex="")
        national = dict(base.filter(level="national").values_list("nationality", "value"))
        governorates = dict(
            base.filter(level="governorate").values_list("nationality").annotate(total=Sum("value"))
        )
        assert national and national == governorates, category
    bands = dict(
        PopulationFigure.objects.filter(year=2025, level="national")
        .exclude(age_group="")
        .values_list("nationality")
        .annotate(total=Sum("value"))
    )
    assert bands == dict(
        PopulationFigure.objects.filter(
            year=2025, level="national", category="total", age_group=""
        ).values_list("nationality", "value")
    )
    # no action point completed after today
    assert dm.ActionPoint.objects.filter(date_of_completion__isnull=False).exists()
    assert not dm.ActionPoint.objects.filter(date_of_completion__date__gt=dt.date.today()).exists()
