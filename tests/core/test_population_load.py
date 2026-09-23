"""The bundled population files load completely, without double counting, and only once."""

from django.core.management import call_command

from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.core.services.population import population_view


def test_bundled_figures_load_every_area_once(db):
    call_command("load_population_figures", bundled=True)

    assert set(PopulationFigure.objects.values_list("year", flat=True)) >= {2025, 2026}
    view = population_view(2026)
    governorates = view["by_governorate"]
    # 8 governorates, with the SYR/PAL sheets' "Nabatieh" merged into "El Nabatieh"
    assert [r["label"] for r in governorates["rows"]] == [
        "Akkar",
        "Baalbek-El Hermel",
        "Beirut",
        "Bekaa",
        "El Nabatieh",
        "Mount Lebanon",
        "North",
        "South",
    ]
    assert len(view["by_district"]["rows"]) == 26
    assert "ALL" not in governorates["columns"]
    # the all-nationality figure is the total, not an extra nationality counted twice
    assert view["grand_total"] == governorates["grand_total"] == view["by_district"]["grand_total"]
    assert sum(view["totals_by_nationality"].values()) == view["grand_total"]
    assert view["totals_by_nationality"]["LEB"] == sum(
        r["values"][governorates["columns"].index("LEB")] for r in governorates["rows"]
    )
    assert SyncRun.objects.filter(job=SyncRun.Job.POPULATION, status=SyncRun.Status.SUCCEEDED).count() >= 2


def test_bundled_load_skips_years_already_loaded(db):
    call_command("load_population_figures", bundled=True)
    count = PopulationFigure.objects.count()
    runs = SyncRun.objects.count()

    call_command("load_population_figures", bundled=True)

    assert PopulationFigure.objects.count() == count
    assert SyncRun.objects.count() == runs
