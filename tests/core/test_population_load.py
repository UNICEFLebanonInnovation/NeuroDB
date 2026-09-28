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


def _load_like_the_old_loader():
    """What the loader wrote before the PAL fix: the PAL sheet's rows stored under PRL."""
    call_command("load_population_figures", bundled=True)
    for row in PopulationFigure.objects.filter(nationality="PAL"):
        if not PopulationFigure.objects.filter(
            **{f: getattr(row, f) for f in ("year", "level", "area_code", "category", "age_group", "sex")},
            nationality="PRL",
        ).exists():
            row.nationality = "PRL"
            row.save(update_fields=["nationality"])
    PopulationFigure.objects.filter(nationality="PAL").delete()


def test_container_start_repairs_years_loaded_by_the_old_loader(db):
    from neurodb.core.management.commands.load_population_figures import outdated_years

    _load_like_the_old_loader()
    PopulationFigure.objects.create(
        year=2026, level="district", area_code="akkar", area_name="Akkar", nationality="SYR",
        category="vulnerable", vulnerability_level="high", value=10,
    )  # fmt: skip
    assert outdated_years() == {2025, 2026}

    call_command("load_population_figures", bundled=True)  # what the container runs at start

    assert outdated_years() == set()
    assert PopulationFigure.objects.filter(nationality="PAL").exists()
    assert not PopulationFigure.objects.filter(nationality="PRL").exclude(age_group="").exists()
    assert PopulationFigure.objects.filter(category="vulnerable").count() == 1  # admin figures stay
    # and the next start changes nothing
    runs = SyncRun.objects.count()
    call_command("load_population_figures", bundled=True)
    assert SyncRun.objects.count() == runs


def test_admin_button_reloads_every_bundled_year(client, admin_user, viewer):
    from django.urls import reverse

    _load_like_the_old_loader()
    url = reverse("admin:core_populationfigure_reload_bundled")

    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    client.post(url, {"_form_submitted": "on"})
    assert not PopulationFigure.objects.filter(nationality="PAL").exists()  # viewers cannot

    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    assert client.get(url).status_code == 200  # a GET only shows the confirmation
    response = client.post(url, {"_form_submitted": "on"}, follow=True)
    assert "Loaded" in response.content.decode()
    assert PopulationFigure.objects.filter(nationality="PAL").exists()
    assert SyncRun.objects.filter(job=SyncRun.Job.POPULATION, triggered_by=admin_user.username).count() == 2
