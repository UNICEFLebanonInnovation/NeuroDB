"""A small, hand-counted data set for the country overview and the daily review.

One active programme document (Child Protection, Amel, 2026) with two indicators: one counting
children (target 1,000, reported 300 in Akkar in Q1 and 200 in Beirut in Q2, cumulative 500) and one
counting teachers (target 50, never reported). One funds reservation (10,000 reserved, 4,000
disbursed, donor EU), two TPM visits (one approved, one without its report), two action points (one
open, high priority, overdue; one closed ten days after its due date), one off-track monitoring
finding, children population for Akkar and Beirut. With the ActivityInfo ``hierarchy`` fixture of
``tests/conftest.py`` (500 children: Beirut 150 in January, Akkar 350 in February), the numbers of
every block can be counted by hand. ``TODAY`` is half way through the programme document.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from neurodb.core.models import PopulationFigure
from neurodb.datamart import models as dm
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PCA, PartnerOrganization

TODAY = datetime.date(2026, 7, 1)
CHILDREN_TITLE = "# of children reached with psychosocial support"
TEACHERS_TITLE = "# of teachers trained"


def _location(pk: int, name: str, p_code: str, location_type: LocationType, parent: Location | None = None):
    return Location.objects.create(
        id=pk, name=name, p_code=p_code, type=location_type, parent=parent, lft=1, rght=2, level=0, tree_id=1
    )


def make_overview_data(today: datetime.date = TODAY) -> dict:  # noqa: ARG001 - dates are fixed to TODAY
    governorate = LocationType.objects.create(name="Governorate", admin_level=1)
    cadaster = LocationType.objects.create(name="Cadaster", admin_level=3)
    akkar = _location(11, "Akkar", "LB1", governorate)
    beirut = _location(12, "Beirut", "LB6", governorate)
    halba = _location(1001, "Halba", "LB1101", cadaster, akkar)
    achrafieh = _location(1002, "Achrafieh", "LB6101", cadaster, beirut)

    partner = PartnerOrganization.objects.create(
        etl_id="1",
        name="Amel Association",
        partner_type="Civil Society Organization",
        vendor_number="V1",
        rating="High",
    )
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PD1",
        title="Child protection services",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
        section_names=["Child Protection"],
    )
    common = {
        "intervention": pd,
        "pd_reference_number": pd.number,
        "section_name": "Child Protection",
        "lower_result_name": "1.1 Protection services",
        "display_type": "number",
        "unit": "number",
        "baseline_numerator": Decimal(0),
    }
    dm.PDIndicator.objects.create(
        datamart_id=1,
        source_id=100,
        title=CHILDREN_TITLE,
        target_numerator=Decimal(1000),
        location_name=halba.name,
        location_pcode=halba.p_code,
        location=halba,
        tag_age_group="Children",
        **common,
    )
    dm.PDIndicator.objects.create(
        datamart_id=2,
        source_id=200,
        title=TEACHERS_TITLE,
        target_numerator=Decimal(50),
        location_name=achrafieh.name,
        location_pcode=achrafieh.p_code,
        location=achrafieh,
        tag_age_group="Adults",
        **common,
    )
    for n, (end, place, value, cumulative) in enumerate(
        [(datetime.date(2026, 3, 31), halba, 300, 300), (datetime.date(2026, 6, 30), achrafieh, 200, 500)],
        start=1,
    ):
        dm.ReportedIndicator.objects.create(
            datamart_id=n,
            partner=partner,
            intervention=pd,
            partner_name=partner.name,
            pd_reference_number=pd.number,
            progress_report=f"PR-{n}",
            report_number=f"QPR{n}",
            report_type="QPR",
            report_status="Accepted",
            period_start=end - datetime.timedelta(days=89),
            period_end=end,
            due_date=end + datetime.timedelta(days=15),
            submission_date=end + datetime.timedelta(days=10),
            indicator=CHILDREN_TITLE,
            target="1000",
            location=place.name,
            p_code=place.p_code,
            location_ref=place,
            achievement_in_period=str(value),
            total_cumulative_progress=str(cumulative),
            total_cumulative_progress_in_location=str(value),
            calculation_across_locations="sum",
            etools_indicator_id="100",
        )
    dm.FundsReservationHeader.objects.create(
        datamart_id=1,
        intervention=pd,
        pd_reference_number=pd.number,
        fr_number="0400001",
        total_amt=Decimal(10000),
        actual_amt=Decimal(4000),
        outstanding_amt=Decimal(6000),
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 12, 31),
    )
    dm.FundsReservation.objects.create(
        datamart_id=1,
        intervention=pd,
        fr_number="0400001",
        line_item=1,
        donor="EU",
        overall_amount=Decimal(10000),
    )
    approved = dm.TPMVisit.objects.create(
        datamart_id=1,
        partner=partner,
        reference_number="TPM/1",
        status="unicef_approved",
        start_date=datetime.date(2026, 2, 10),
        end_date=datetime.date(2026, 2, 12),
    )
    late = dm.TPMVisit.objects.create(
        datamart_id=2,
        partner=partner,
        reference_number="TPM/2",
        status="assigned",
        tpm_name="Monitoring Ltd",
        start_date=datetime.date(2026, 3, 1),
        end_date=datetime.date(2026, 3, 2),
    )
    for n, visit in enumerate((approved, late), start=1):
        activity = dm.TPMActivity.objects.create(
            datamart_id=n,
            partner=partner,
            intervention=pd,
            visit=visit,
            visit_reference_number=visit.reference_number,
            section="Child Protection",
            date=visit.start_date,
        )
        activity.location_links.add(halba)
    dm.ActionPoint.objects.create(
        datamart_id=1,
        partner=partner,
        intervention=pd,
        reference_number="AP/1",
        description="Replace the damaged water tank",
        status="open",
        high_priority=True,
        due_date=datetime.date(2026, 5, 1),
        section="Child Protection",
        related_module="tpm",
        partner_name=partner.name,
        location=halba,
    )
    dm.ActionPoint.objects.create(
        datamart_id=2,
        partner=partner,
        intervention=pd,
        reference_number="AP/2",
        description="Submit the attendance sheets",
        status="completed",
        due_date=datetime.date(2026, 2, 1),
        date_of_completion=datetime.datetime(2026, 2, 11, 10, 0, tzinfo=datetime.UTC),
        section="Child Protection",
        related_module="fm",
        partner_name=partner.name,
        location=halba,
    )
    dm.MonitoringFinding.objects.create(
        datamart_id=1,
        partner=partner,
        entity=pd.number,
        monitoring_activity="MA-1",
        overall_finding_rating="Off Track",
        end_date=datetime.date(2026, 6, 20),
        location=halba,
    )
    for nationality, area, code, value in (
        ("ALL", "Akkar", "LB1", 10000),
        ("LEB", "Beirut", "LB6", 3000),
        ("SYR", "Beirut", "LB6", 1000),
    ):
        PopulationFigure.objects.create(
            year=2025, category="children", level="governorate", nationality=nationality,
            area_name=area, area_code=code, value=value,
        )  # fmt: skip
    return {"partner": partner, "pd": pd, "akkar": akkar, "beirut": beirut, "halba": halba}
