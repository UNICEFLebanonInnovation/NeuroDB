"""Ask NeuroDB's lookups never under-report a list: each list sent is cut to its limit, and next to it the
lookup gives the real number of rows (counted over every match, not over the rows fetched) and whether
the list was cut, so the model can say a list is partial."""

import datetime

import pytest
from django.utils import timezone

from neurodb.assistant import agent, hub_tools, tools
from neurodb.cpd.models import CountryProgramme, Outcome, Output
from neurodb.facts.models import ActivityReportNew
from neurodb.graph.models import Edge, Entity
from neurodb.indicators.models import MasterIndicator, NeuroReportMasterIndicator
from neurodb.library.models import Resource
from neurodb.partnerships import linking
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.reports.test_pd_monitoring import report_rows

pytestmark = pytest.mark.django_db

MORE = tools.MAX_ROWS + 5  # more rows than one list holds, and more than the MAX_ROWS + 1 fetched before


def _cut_flags(result, key, total, kept):
    assert len(result[key]) == kept
    assert (result["total_rows"], result["truncated"]) == (total, True)


# ------------------------------------------------------------------- (a) the total is counted, not fetched


def test_find_indicators_gives_the_real_number_of_matches(database):
    MasterIndicator.objects.bulk_create(
        MasterIndicator(
            database=database,
            name=f"Children reached {n}",
            awp_code=f"9.{n}",
            aggregation_method="SUM",
            sequence=n,
        )
        for n in range(MORE)
    )
    result = tools.run("find_indicators", {"query": "children reached"})
    _cut_flags(result, "master_indicators", MORE, tools.MAX_ROWS)


def test_activity_breakdown_gives_the_real_number_of_groups(database):
    ActivityReportNew.objects.bulk_create(
        ActivityReportNew(
            dbase=database,
            database_ai_id=str(database.ai_id),
            indicator_id="i_a",
            indicator_name="i_a",
            indicator_value=1,
            month_name="2026-01-01",
            month="2026-01",
            partner_label="Cedar Relief",
            location_name=f"Site {n}",
            funded_by="UNICEF",
        )
        for n in range(MORE)
    )
    result = tools.run("activity_breakdown", {"database_id": database.id, "group_by": "site"})
    _cut_flags(result, "rows", MORE, tools.MAX_ROWS)


def test_partner_reporting_gives_the_real_number_of_reports(monkeypatch):
    monkeypatch.setattr(tools, "MAX_ROWS", 2)
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Cedar Relief", partner_type="CSO", vendor_number="V1"
    )
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA2026010",
        title="Learning",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2027, 12, 31),
    )
    for n in range(5):
        end = datetime.date(2026, 1, 31) + datetime.timedelta(days=30 * n)
        report_rows(pd, partner, "# of children", f"PR-{n}", f"QPR{n}", "QPR", end, {"Beirut": 10}, 10)
    result = tools.run("partner_reporting", {})
    assert result["reports"] == 5  # the page's count of progress reports
    _cut_flags(result, "progress_reports", 5, 2)


def test_search_programmes_gives_the_real_number_of_documents(db):
    PCA.objects.bulk_create(
        PCA(etl_id=str(n), number=f"LEB/PCA2026{n:03d}", title=f"Learning {n}", status="active")
        for n in range(MORE)
    )
    result = tools.run("search_programmes", {"text": "learning"})
    assert result["count"] == MORE
    _cut_flags(result, "programme_documents", MORE, tools.MAX_ROWS)


def test_search_partners_gives_the_real_number_of_partners(db):
    PartnerOrganization.objects.bulk_create(
        PartnerOrganization(etl_id=str(n), name=f"Cedar Relief {n:02d}", partner_type="CSO")
        for n in range(MORE)
    )
    result = tools.run("search_partners", {"text": "cedar"})
    assert result["count"] == MORE
    _cut_flags(result, "partners", MORE, tools.MAX_ROWS)


def test_the_cut_takes_the_real_total():
    rows = list(range(3))
    assert tools._cut(rows, "items", total=9) == {"items": [0, 1, 2], "total_rows": 9, "truncated": True}
    assert tools._cut(rows, "items") == {"items": [0, 1, 2], "total_rows": 3, "truncated": False}


# ----------------------------------------------------------------- (b) connected counts every thing it reached


def _entity(kind, key, name):
    return Entity.objects.create(kind=kind, key=key, name=name, built_at=timezone.now())


def test_connected_gives_the_real_count_and_says_when_the_list_is_cut(db):
    partner = _entity("partner", "1", "Cedar Relief")
    total = hub_tools.MAX_LIST + 5
    for n in range(total):
        donor = _entity("donor", f"d{n}", f"Donor {n:02d}")
        Edge.objects.create(source=partner, target=donor, relation="funded_by", origin="test")
    result = tools.run("connected", {"kind": "partner", "key": "1", "to_kind": "donor"})
    assert len(result["found"]) == hub_tools.MAX_LIST
    assert (result["count"], result["truncated"]) == (total, True)
    few = tools.run("connected", {"kind": "donor", "key": "d0", "to_kind": "partner"})
    assert (len(few["found"]), few["count"], few["truncated"]) == (1, 1, False)


# ------------------------------------------------------------------ (c) every cut list says it was cut


def test_neuro_report_flags_a_section_cut_short(hierarchy, monkeypatch):
    monkeypatch.setattr(tools, "MAX_ROWS", 1)
    report = hierarchy["report"]
    NeuroReportMasterIndicator.objects.create(
        report=report, master=hierarchy["ratio"], label="Share of girls", target=50
    )
    section = tools.run("neuro_report", {"report_id": report.id, "month": 3})["sections"][0]
    _cut_flags(section, "indicators", 2, 1)


def test_partner_details_flags_the_programme_documents_cut_short(monkeypatch, db):
    monkeypatch.setattr(tools, "MAX_PARTNER_PDS", 2)
    partner = PartnerOrganization.objects.create(etl_id="1", name="Cedar Relief", partner_type="CSO")
    for n in range(3):
        PCA.objects.create(
            etl_id=str(10 + n), partner=partner, partner_name=partner.name, number=f"LEB/PCA2026{n:03d}"
        )
    result = tools.run("partner_details", {"partner_id": partner.id})
    assert len(result["programme_documents"]) == 2
    assert (result["programme_documents_total"], result["programme_documents_truncated"]) == (3, True)


def test_partner_activityinfo_flags_sites_and_indicators_cut_short(hierarchy, monkeypatch):
    monkeypatch.setattr(tools, "MAX_ROWS", 1)
    database = hierarchy["database"]
    ActivityReportNew.objects.create(
        dbase=database,
        database_ai_id=str(database.ai_id),
        indicator_id="i_a",
        indicator_name="i_a",
        indicator_value=5,
        month_name="2026-01-01",
        month="2026-01",
        partner_label="Partner A",
        location_name="Site Zahle",
        funded_by="UNICEF",
    )
    partner = PartnerOrganization.objects.create(etl_id="1", name="Partner A", partner_type="CSO")
    linking.link_activityinfo_partners()
    entry = tools.run("partner_activityinfo", {"partner": partner.name})["databases"][0]
    assert (len(entry["sites"]), entry["sites_total"], entry["sites_truncated"]) == (1, 2, True)
    assert (len(entry["indicators"]), entry["indicators_total"], entry["indicators_truncated"]) == (
        1,
        2,
        True,
    )


def test_country_programme_flags_documents_and_partners_cut_short(monkeypatch, db):
    monkeypatch.setattr(hub_tools, "MAX_PER_OUTPUT", 2)
    cycle = CountryProgramme.objects.create(
        name="CP 2026-2028", start_year=2026, end_year=2028, current=True, etools_name="CP 2026"
    )
    outcome = Outcome.objects.create(programme=cycle, code="1", title="Children learn")
    Output.objects.create(outcome=outcome, code="1.1", title="Access to learning")
    for n in range(3):
        partner = PartnerOrganization.objects.create(
            etl_id=str(n), name=f"Cedar Relief {n}", partner_type="CSO"
        )
        PCA.objects.create(
            etl_id=str(10 + n),
            partner=partner,
            partner_name=partner.name,
            number=f"LEB/PCA2026{n:03d}",
            status="active",
            start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 12, 31),
            country_programme="CP 2026",
            cp_outputs=["1.1 Access to learning"],
        )
    output = tools.run("country_programme", {})["outcomes"][0]["outputs"][0]
    assert len(output["programme_documents"]) == 2 and len(output["partners"]) == 2
    assert (output["programme_documents_total"], output["programme_documents_truncated"]) == (3, True)
    assert (output["partners_total"], output["partners_truncated"]) == (3, True)


def test_search_library_flags_the_resources_cut_short(monkeypatch, db):
    monkeypatch.setattr(tools, "MAX_LIBRARY", 2)
    for n in range(3):
        Resource.objects.create(title=f"Learning study {n}", publication_year="2025", section="Education")
    result = tools.run("search_library", {"text": "learning"})
    assert result["count"] == 3
    _cut_flags(result, "resources", 3, 2)


# ------------------------------------------------------------------ (d) the model is told what they mean


def test_the_model_is_told_the_total_is_real_and_a_cut_list_is_partial():
    assert "total_rows" in agent.SYSTEM_PROMPT and "truncated" in agent.SYSTEM_PROMPT
    assert "partial" in agent.SYSTEM_PROMPT


# ---------------------------------------------------- (e) the shorter lists of a partner, a report, the library


def test_partner_details_flags_its_findings_and_assessments_and_counts_every_tpm_visit(db):
    from neurodb.datamart import models as dm

    partner = PartnerOrganization.objects.create(etl_id="1", name="Cedar Relief", partner_type="CSO")
    for n in range(14):
        dm.AuditFinding.objects.create(datamart_id=100 + n, partner=partner, title=f"Finding {n}", amount=10)
        dm.PartnerAssessment.objects.create(datamart_id=200 + n, partner=partner, type="Micro Assessment")
    for n in range(25):
        dm.TPMVisit.objects.create(datamart_id=300 + n, partner=partner, reference_number=f"TPM-{n}")
    result = tools.run("partner_details", {"partner_id": partner.id})
    assert len(result["financial_findings"]) == 10
    assert (result["financial_findings_total"], result["financial_findings_truncated"]) == (14, True)
    assert len(result["hact_assessments"]) == 10
    assert (result["hact_assessments_total"], result["hact_assessments_truncated"]) == (14, True)
    assert result["tpm_visits"] == 25 and "tpm_visits_recent" not in result


def test_neuro_report_sends_the_latest_comments_and_flags_the_cut(hierarchy, monkeypatch):
    from neurodb.indicators.models import NeuroReportComment

    monkeypatch.setattr(tools, "MAX_COMMENTS", 3)
    report = hierarchy["report"]
    for n in range(1, 6):
        NeuroReportComment.objects.create(report=report, comment=f"Note {n}", related_month=f"{n:02d}")
    result = tools.run("neuro_report", {"report_id": report.id, "month": 12})
    assert (result["comments_total"], result["comments_truncated"]) == (5, True)
    assert [c["month"] for c in result["comments"]] == ["03", "04", "05"]  # the latest


def test_search_library_flags_the_maps_cut_short(monkeypatch, db):
    from neurodb.library.models import Map

    monkeypatch.setattr(tools, "MAX_MAPS", 2)
    for n in range(3):
        Map.objects.create(name=f"Learning map {n}", status="Completed")
    result = tools.run("search_library", {"text": "learning"})
    assert len(result["maps"]) == 2
    assert (result["maps_total"], result["maps_truncated"]) == (3, True)


# ------------------------------------------------------- (f) a partner or PD text that matches too many


def test_pd_indicator_progress_refuses_a_partner_text_that_matches_too_many(db):
    for n in range(tools.MAX_MATCHES + 5):
        PartnerOrganization.objects.create(
            etl_id=str(n), name=f"Cedar Association {n:02d}", partner_type="CSO"
        )
    with pytest.raises(tools.ToolInputError) as raised:
        tools.run("pd_indicator_progress", {"partner": "Association", "year": 2026})
    said = str(raised.value)
    assert f"More than {tools.MAX_MATCHES} partners match 'Association'" in said
    assert "Cedar Association 00" in said and "narrower" in said


def test_pd_indicator_progress_refuses_a_pd_text_that_matches_too_many(db):
    partner = PartnerOrganization.objects.create(etl_id="1", name="Cedar Relief", partner_type="CSO")
    for n in range(tools.MAX_MATCHES + 5):
        PCA.objects.create(
            etl_id=str(10 + n), partner=partner, partner_name=partner.name, number=f"LEB/PD/2026/{n:03d}"
        )
    with pytest.raises(tools.ToolInputError) as raised:
        tools.run("pd_indicator_progress", {"programme_document": "PD/2026", "year": 2026})
    assert f"More than {tools.MAX_MATCHES} programme documents" in str(raised.value)
