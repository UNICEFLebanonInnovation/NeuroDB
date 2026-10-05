"""Monitoring insights does not move the existing pages (invariant 6): on the overview fixture, the
Datamart pages' fixture and a field monitoring fixture, the /field-monitoring/ figures, the overview's field monitoring visits and the
partner page's visit series keep their pinned values after the programme documents are linked to the
findings (``datamart.fm.relink_findings``). The demo's own field monitoring rows are checked in
``tests/core/test_demo_seed.py`` (the first 50 findings are left as they were)."""

import datetime

import pytest
from django.http import QueryDict

from neurodb.datamart import fm, services
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports import overview
from tests.reports.overview_fixture import TODAY, make_overview_data
from tests.reports.test_datamart_pages import datamart  # noqa: F401

pytestmark = pytest.mark.django_db
D = datetime.date


@pytest.fixture
def field_monitoring(db):
    """Six finding rows: one activity with two entities, a PD written in the LEBA/ form, a row without
    an activity reference, last year's visit and a row without an end date."""
    a = PartnerOrganization.objects.create(
        etl_id="1", name="Partner A", partner_type="CSO", vendor_number="V1"
    )
    b = PartnerOrganization.objects.create(
        etl_id="2", name="Partner B", partner_type="CSO", vendor_number="V2"
    )
    PCA.objects.create(etl_id="11", partner=a, number="LEB/PCA2023597/PD2025123", title="Education")
    rows = [
        (a, "LEB/PCA2023597/PD2025123", "PD/SSFA", "FM-2026-001", 1001, "On Track", D(2026, 3, 10)),
        (a, "Partner A", "Partner", "FM-2026-001", 1001, "Off Track", D(2026, 3, 10)),
        (a, "LEBA/PCA2023597/PD2025123", "PD/SSFA", "FM-2026-002", None, "", D(2026, 4, 2)),
        (b, "2.2 INCREASED ACCESS TO EDUCATION", "CP Output", "", None, "On Track", D(2026, 4, 15)),
        (b, "Partner B", "Partner", "FM-2025-010", 1500, "Off Track", D(2025, 11, 1)),
        (a, "Partner A", "Partner", "FM-2026-003", 1600, "Constrained", None),
    ]
    for n, (partner, entity, entity_type, activity, activity_id, rating, end) in enumerate(rows, start=1):
        dm.MonitoringFinding.objects.create(
            datamart_id=n,
            partner=partner,
            vendor_number=partner.vendor_number,
            entity=entity,
            entity_type=entity_type,
            monitoring_activity=activity,
            monitoring_activity_id=activity_id,
            overall_finding_rating=rating,
            end_date=end,
        )
    return {"a": a, "b": b}


def page_figures(params: str) -> dict:
    data = services.monitoring(QueryDict(params, mutable=False))
    return {
        "findings": data["findings"].count(),
        "activities": data["activities"],
        "by_rating": dict(data["by_rating"]),
        "by_month": data["by_month"],
    }


def figures(partners) -> dict:
    return {
        "page": page_figures(""),
        "page_2026": page_figures("year=2026"),
        "page_off_track": page_figures("rating=Off+Track"),
        "page_partner_a": page_figures("q=Partner+A"),
        "partner_series": {k: services.partner_datamart(p)["monitoring_visits_by_year"] for k, p in partners},
    }


PINNED = {
    "page": {
        "findings": 6,
        "activities": 4,
        "by_rating": {"On Track": 2, "Off Track": 2, "—": 1, "Constrained": 1},
        "by_month": [("2025-11", 1), ("2026-03", 2), ("2026-04", 2)],
    },
    "page_2026": {
        "findings": 4,
        "activities": 2,
        "by_rating": {"On Track": 2, "Off Track": 1, "—": 1},
        "by_month": [("2026-03", 2), ("2026-04", 2)],
    },
    "page_off_track": {
        "findings": 2,
        "activities": 2,
        "by_rating": {"Off Track": 2},
        "by_month": [("2025-11", 1), ("2026-03", 1)],
    },
    "page_partner_a": {
        "findings": 4,
        "activities": 3,
        "by_rating": {"On Track": 1, "Off Track": 1, "—": 1, "Constrained": 1},
        "by_month": [("2026-03", 2), ("2026-04", 1)],
    },
    "partner_series": {
        "a": {"field": {2026: 2}, "tpm": {}},
        "b": {"field": {2026: 1, 2025: 1}, "tpm": {}},
    },
}


def test_field_monitoring_page_and_partner_series_do_not_move(field_monitoring):
    partners = [("a", field_monitoring["a"]), ("b", field_monitoring["b"])]
    assert figures(partners) == PINNED
    counts = fm.relink_findings()
    assert (counts["exact"], counts["token"]) == (1, 1)  # the PD rows are now linked
    assert dm.MonitoringFinding.objects.exclude(intervention=None).count() == 2
    assert figures(partners) == PINNED


def _overview_figures(reporting_year) -> dict:
    out = {}
    for name, scope in {
        "all": {},
        "section": {"sections": ["Child Protection"]},
        "other_section": {"sections": ["WASH"]},
        "akkar": {"governorate": "Akkar"},
        "beirut": {"governorate": "Beirut"},
    }.items():
        data = overview.build(
            overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY, **scope), cache=False
        )
        delivery = data["delivery"]
        out[name] = (delivery["assurance"]["field_monitoring_visits"], delivery["findings_by_rating"])
    return out


def test_overview_field_monitoring_figures_do_not_move(hierarchy, reporting_year):
    data = make_overview_data()
    pinned = {
        "all": (1, [["Off Track", 1]]),
        "section": (1, [["Off Track", 1]]),
        "other_section": (0, []),
        "akkar": (1, [["Off Track", 1]]),
        "beirut": (0, []),
    }
    assert _overview_figures(reporting_year) == pinned
    page = page_figures("year=2026")
    series = services.partner_datamart(data["partner"])["monitoring_visits_by_year"]
    assert (page["findings"], page["activities"], series) == (1, 1, {"field": {2026: 1}, "tpm": {2026: 2}})
    assert fm.relink_findings()["exact"] == 1  # "LEB/PD1" is the programme document's number
    assert dm.MonitoringFinding.objects.get().intervention == data["pd"]
    assert _overview_figures(reporting_year) == pinned
    assert page_figures("year=2026") == page
    assert services.partner_datamart(data["partner"])["monitoring_visits_by_year"] == series


def test_datamart_pages_fixture_does_not_move(datamart):  # noqa: F811 (the imported fixture)
    """The /field-monitoring/ fixture of ``test_datamart_pages`` (a partner row), with a row about its
    programme document added: the page, the partner series and Ask's assurance overview are the same
    before and after the relink links that row."""
    from neurodb.assistant import tools

    dm.MonitoringFinding.objects.create(
        datamart_id=2,
        partner=datamart["partner"],
        entity="LEB/PD1",
        entity_type="PD/SSFA",
        overall_finding_rating="Off Track",
        monitoring_activity="MA-2",
        end_date=datetime.date.today(),
    )

    def snapshot():
        year = datetime.date.today().year
        return (
            figures([("a", datamart["partner"])]),
            page_figures(f"year={year}"),
            tools.run("assurance_overview", {"partner": "Partner A"})["field_monitoring"],
        )

    before = snapshot()
    assert before[1]["findings"] == 2 and before[1]["activities"] == 2
    assert fm.relink_findings()["exact"] == 1
    assert dm.MonitoringFinding.objects.get(datamart_id=2).intervention == datamart["pd"]
    assert snapshot() == before
