"""Which page reads which eTools source (datamart.raw.PAGE_SOURCES): the "Raw data behind this page" line opens Raw
eTools data on them, so the list must be the truth. Each page is opened with every query captured, and every eTools
table it reads must be one of its sources; every page of the focused menu has its list."""

from __future__ import annotations

import datetime
import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from neurodb.datamart import catalogue, raw
from neurodb.datamart import models as dm
from neurodb.indicators.services import navigation
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

ADDRESSES = {
    "reports:overview": ["/"],
    "reports:partner_reporting": [
        "/partner-reporting/",
        "/partner-reporting/?tab=partners",
        "/partner-reporting/?tab=locations",
        "/partner-reporting/?tab=disaggregations",
    ],
    "reports:pd_monitoring": [
        "/partner-monitoring/",
        "/partner-monitoring/?view=table",
        "/partner-monitoring/?view=quality",
        "/partner-monitoring/map/",
    ],
    "reports:monitoring": ["/field-monitoring/", "/field-monitoring/?tab=plan"],
}
# eTools tables that are a source's records or their links, by the source they come from
LINKED = {
    "etools_pca": "interventions",
    "etools_pca_locations": "interventions",
    "etools_partnerorganization": "partners",
    "etools_agreement": "agreements",
    "locations_location": "locations",
    "datamart_monitoringsite": "location_sites",
    "datamart_disaggregationlabel": "partner_reports",  # the labels of the ids the partner report rows name
    "datamart_auditengagement_interventions": "engagements",
    "datamart_tpmactivity_location_links": "tpm_activities",
}
# NeuroDB's own tables in the datamart app: settings people edit, never a source
OWN = re.compile(r"datamart_(indicatorflag|recordpeople|visitplancomment|etoolsmaster\w*|rawfieldreview)$")
TABLE = re.compile(r'(?:FROM|JOIN)\s+"((?:datamart|etools|locations)_[a-z0-9_]+)"')
DATASET = re.compile(r'"datamart_datamartdocument"\."dataset"\s*(?:=|IN)\s*\(?([^)]*?)\)?(?:\s|$)')


def _sources_read(sql: list[str]) -> set[str]:
    typed = {raw.model_of(name)._meta.db_table: name for name in catalogue.TYPED}
    found: set[str] = set()
    for text in sql:
        for table in TABLE.findall(text):
            if table == "datamart_datamartdocument":
                for match in DATASET.findall(text):
                    found |= set(re.findall(r"'([a-z_]+)'", match))
            elif table in typed:
                found.add(typed[table])
            elif table in LINKED:
                found.add(LINKED[table])
            elif not OWN.match(table) and not table.startswith("locations_locationtype"):
                found.add(f"? {table}")
    return found


@pytest.fixture
def world(db):
    partner = PartnerOrganization.objects.create(etl_id="1", name="Amel Association", vendor_number="V1")
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        number="LEB/PD1",
        title="A",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
        section_names=["Education"],
    )
    dm.PDIndicator.objects.create(
        datamart_id=1, source_id=10, intervention=pd, pd_reference_number=pd.number, title="# reached",
        section_name="Education", target_numerator=100,
    )  # fmt: skip
    dm.ReportedIndicator.objects.create(
        datamart_id=1, partner=partner, intervention=pd, pd_reference_number=pd.number, progress_report="PR-1",
        report_number="QPR1", report_type="QPR", report_status="Accepted", period_start=datetime.date(2026, 1, 1),
        period_end=datetime.date(2026, 3, 31), indicator="# reached", etools_indicator_id="10", location="Akkar",
        achievement_in_period="5", submission_date=datetime.date(2026, 4, 5),
    )  # fmt: skip
    dm.MonitoringFinding.objects.create(
        datamart_id=1, partner=partner, intervention=pd, end_date=datetime.date(2026, 5, 1)
    )
    dm.PlannedVisits.objects.create(datamart_id=1, intervention=pd, partner=partner, year=2026)
    return pd


@pytest.mark.parametrize("page", list(ADDRESSES))
def test_every_etools_table_a_page_reads_is_one_of_its_sources(client_viewer, world, page):
    with CaptureQueriesContext(connection) as found:
        for address in ADDRESSES[page]:
            assert client_viewer.get(address).status_code == 200, address
    read = _sources_read([q["sql"] for q in found.captured_queries])
    assert read, page
    assert read <= set(raw.PAGE_SOURCES[page]), (page, sorted(read - set(raw.PAGE_SOURCES[page])))


def test_every_page_of_the_focused_menu_has_its_sources():
    assert set(navigation.FOCUSED) - {navigation.HELP_KEY} <= set(raw.PAGE_SOURCES)
    assert raw.PAGE_SOURCES["assistant:ask"] == (raw.EVERY_DATASET,)
    for page, sources in raw.PAGE_SOURCES.items():
        if sources != (raw.EVERY_DATASET,):
            assert set(sources) <= set(raw.sources()), page
    assert set(raw.USED_BY) == set(catalogue.dataset_names())  # Ask NeuroDB reads every dataset
