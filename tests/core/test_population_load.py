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


def test_palestinian_age_bands_and_children_are_stored_as_pal_not_prl(db):
    """The PAL sheet's age bands, sex and children figures are PRL + PRS: they must not be labelled PRL."""
    from neurodb.assistant import tools
    from neurodb.reports.brief import NATIONALITY_OF_POPULATION

    call_command("load_population_figures", bundled=True)

    total = population_view(2026)
    assert total["totals_by_nationality"]["PRL"] == 201_136  # the PRL total itself stays PRL
    assert total["totals_by_nationality"]["PRS"] == 23_655
    assert "PAL" not in total["totals_by_nationality"]  # no PAL total row: PRL + PRS would count twice
    ages = total["by_age_group"]
    assert "PRL" not in ages["columns"]
    assert ages["column_totals"][ages["columns"].index("PAL")] == 201_136 + 23_655
    children = population_view(2026, "children")
    assert (
        "PRL" not in children["totals_by_nationality"] and "PRL" not in children["by_governorate"]["columns"]
    )
    assert children["totals_by_nationality"]["PAL"] == 83_817
    assert not PopulationFigure.objects.filter(nationality="PRL").exclude(age_group="", sex="").exists()
    assert not PopulationFigure.objects.filter(nationality="PRL", category="children").exists()
    # every stored nationality has a label on the page, in the brief and in the assistant's tool
    stored = set(PopulationFigure.objects.values_list("nationality", flat=True)) - {"ALL"}
    assert stored <= set(PopulationFigure.Nationality.values)
    assert stored <= set(NATIONALITY_OF_POPULATION) | {"OTH"}  # OTH is the brief's "Other"
    codes = tools.population(2026, "children")["nationality_codes"]
    assert set(children["totals_by_nationality"]) <= set(codes)
    assert "PRL + PRS" in PopulationFigure.Nationality.PAL.label
