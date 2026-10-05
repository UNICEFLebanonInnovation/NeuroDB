"""Which programme document a field monitoring finding is about (``datamart.fm.PDResolver``) and the
relink of the stored findings."""

import datetime

import pytest

from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

D = datetime.date


def resolver(*rows):
    """PCA rows as (pk, number, title, partner_id, start, end)."""
    return fm.PDResolver(rows)


PD_2025 = (1, "LEB/PCA2023597/PD2025123", "Education services", 7, D(2025, 1, 1), D(2025, 12, 31))
PD_2026 = (2, "LEB/PCA2023597/PD2025123-2", "Education services", 7, D(2026, 1, 1), D(2026, 12, 31))
SSFA = (3, "LEB/SSFA2024001", "Water trucking", 8, D(2024, 1, 1), D(2026, 12, 31))
OTHER = (4, "LEB/PCA2023600/PD2025200", "Child protection case management", 7, D(2026, 1, 1), D(2026, 12, 31))


def test_exact_match_first():
    found = resolver(PD_2025, PD_2026)
    assert found.resolve("LEB/PCA2023597/PD2025123", "pd", 7, D(2026, 5, 1)) == (1, "exact")
    assert found.resolve(" leb/pca2023597/pd2025123-2 ", "pd", None, None) == (2, "exact")


def test_token_ignores_the_country_prefix_and_the_amendment():
    found = resolver(PD_2025, PD_2026)
    # LEBA/ instead of LEB/, no -2: both amendments share the PCA/PD pair
    assert found.resolve("LEBA/PCA2023597/PD2025123", "pd", 7, D(2026, 5, 12)) == (2, "token")
    assert found.resolve("LEBA/PCA2023597/PD2025123: BLN classes", "pd", 7, D(2025, 3, 1)) == (1, "token")


def test_several_amendments_without_one_covering_the_visit_take_the_latest_end():
    found = resolver(PD_2025, PD_2026)
    assert found.resolve("LEBA/PCA2023597/PD2025123", "pd", 7, D(2027, 2, 1)) == (2, "token")
    assert found.resolve("LEBA/PCA2023597/PD2025123", "pd", 7, None) == (2, "token")
    same_end = (5, "LEB/PCA2023597/PD2025123-3", "", 7, D(2026, 1, 1), D(2026, 12, 31))
    assert resolver(PD_2026, same_end).resolve("PCA2023597/PD2025123", "pd", 7, None) == (5, "token")


def test_base_number_for_an_ssfa():
    found = resolver(SSFA)
    assert found.resolve("LEB/SSFA2024001-1", "pd", None, D(2025, 1, 1)) == (3, "base")
    assert found.resolve("LEBA/SSFA2024001", "pd", None, D(2025, 1, 1)) == (3, "base")


def test_title_only_among_the_partners_programme_documents():
    found = resolver(PD_2025, OTHER, SSFA)
    assert found.resolve("child protection case management", "pd", 7, None) == (4, "title")
    assert found.resolve("Child protection case management", "pd", 8, None) == (None, "")
    assert found.resolve("Child protection case management", "pd", None, None) == (None, "")


def test_never_the_partners_only_programme_document():
    found = resolver(OTHER)
    assert found.resolve("Some other programme", "pd", 7, D(2026, 5, 1)) == (None, "")
    assert found.resolve("", "pd", 7, D(2026, 5, 1)) == (None, "")


def test_cp_output_and_partner_rows_resolve_only_by_exact_or_token():
    found = resolver(PD_2025, SSFA, OTHER)
    assert found.resolve("LEB/PCA2023597/PD2025123", "cp_output", 7, None) == (1, "exact")
    assert found.resolve("LEBA/PCA2023600/PD2025200", "other", 7, None) == (4, "token")
    assert found.resolve("LEB/SSFA2024001-1", "cp_output", None, None) == (None, "")
    assert found.resolve("Child protection case management", "partner", 7, None) == (None, "")


@pytest.mark.django_db
def test_relink_findings_links_unlinked_rows_and_counts_how():
    partner = PartnerOrganization.objects.create(
        etl_id="7", name="Amel", partner_type="CSO", vendor_number="V7"
    )
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        number="LEB/PCA2023597/PD2025123",
        title="Education services",
        start=D(2026, 1, 1),
        end=D(2026, 12, 31),
    )
    ssfa = PCA.objects.create(etl_id="12", partner=partner, number="LEB/SSFA2024001", title="Water")
    rows = {
        "exact": ("LEB/PCA2023597/PD2025123", "PD/SSFA"),
        "token": ("LEBA/PCA2023597/PD2025123-2", "PD/SSFA"),
        "base": ("LEB/SSFA2024001-1", "SSFA"),
        "title": ("Education services", "PD/SSFA"),
        "unresolved": ("LEB/PCA9999999/PD9999999", "PD/SSFA"),
        "partner": ("Amel", "Partner"),
    }
    for n, (entity, entity_type) in enumerate(rows.values(), start=1):
        dm.MonitoringFinding.objects.create(
            datamart_id=n, partner=partner, entity=entity, entity_type=entity_type, end_date=D(2026, 5, 1)
        )
    counts = fm.relink_findings()
    assert counts == {"exact": 1, "token": 1, "base": 1, "title": 1, "unresolved": 1, "pd_kind_rows": 5}
    linked = dict(dm.MonitoringFinding.objects.values_list("datamart_id", "intervention_id"))
    assert linked == {1: pd.pk, 2: pd.pk, 3: ssfa.pk, 4: pd.pk, 5: None, 6: None}
    assert dm.MonitoringFinding.objects.get(datamart_id=3).pd_match == "base"
    # linked rows are not read again; the given rows are, and a row that no longer matches is cleared
    assert fm.relink_findings()["pd_kind_rows"] == 1
    dm.MonitoringFinding.objects.filter(datamart_id=1).update(entity="LEB/PCA0000000/PD0000000")
    again = fm.relink_findings(dm.MonitoringFinding.objects.filter(datamart_id=1))
    assert again["unresolved"] == 1
    row = dm.MonitoringFinding.objects.get(datamart_id=1)
    assert (row.intervention_id, row.pd_match) == (None, "")
