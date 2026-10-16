"""Small worlds for the management brief's children figures, counted by hand (R2 brief review): programme
documents with one children indicator each, reporting monthly or quarterly (Accepted, summed across periods), and
ActivityInfo databases per year with one HPM children master and monthly records."""

from __future__ import annotations

import calendar
import datetime
from decimal import Decimal

from neurodb.accounts.models import Section
from neurodb.core import compute
from neurodb.datamart import models as dm
from neurodb.facts.models import ActivityReportNew
from neurodb.geo.models import Location, LocationType
from neurodb.indicators.models import (
    Activity,
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterSubIndicator,
    NeuroReport,
    NeuroReportMasterIndicator,
    ReportingYear,
    SubIndicator,
)
from neurodb.indicators.services import activityinfo
from neurodb.partnerships.models import PCA, PartnerOrganization

CHILDREN = "# of children reached with psychosocial support"


def months(year: int, upto: int, value: int, start: int = 1) -> dict[tuple[int, int], int]:
    """A report each month from ``start`` to ``upto`` of ``year``, ``value`` children each."""
    return {(year, m): value for m in range(start, upto + 1)}


def quarters(year: int, numbers, value: int) -> dict[tuple[int, int], int]:
    """A quarterly report for each quarter in ``numbers`` of ``year`` (its period ends with the quarter)."""
    return {(year, 3 * q): value for q in numbers}


class World:
    def __init__(self):
        gov_type = LocationType.objects.create(name="Governorate", admin_level=1)
        cad_type = LocationType.objects.create(name="Cadaster", admin_level=3)
        self.gov = Location.objects.create(
            id=11, name="Akkar", p_code="LB1", type=gov_type, lft=1, rght=2, level=0, tree_id=1
        )
        self.place = Location.objects.create(
            id=1001, name="Halba", p_code="LB1101", type=cad_type, parent=self.gov, lft=1, rght=2, level=0,
            tree_id=1,
        )  # fmt: skip
        self.sections: dict[str, Section] = {}
        self.n_pd = self.n_row = self.n_db = 0

    def section(self, name: str, code: str) -> Section:
        if name not in self.sections:
            self.sections[name] = Section.objects.create(name=name, code=code)
        return self.sections[name]

    def pd(self, section: str, reports: dict[tuple[int, int], int]) -> PCA:
        """A programme document of ``section`` (2024 to 2027) with one children indicator: one Accepted report
        per ``(year, month)`` key, the month its period ends, with that many children."""
        self.n_pd += 1
        n = self.n_pd
        partner = PartnerOrganization.objects.create(
            etl_id=str(n),
            name=f"Partner {n}",
            partner_type="Civil Society Organization",
            vendor_number=f"V{n}",
        )
        pd = PCA.objects.create(
            etl_id=str(100 + n), partner=partner, partner_name=partner.name, number=f"LEB/PD{n}", title=f"PD {n}",
            status="active", start=datetime.date(2024, 1, 1), end=datetime.date(2027, 12, 31),
            section_names=[section],
        )  # fmt: skip
        dm.PDIndicator.objects.create(
            datamart_id=n, source_id=1000 + n, title=CHILDREN, target_numerator=Decimal(100000),
            location_name=self.place.name, location_pcode=self.place.p_code, location=self.place,
            tag_age_group="Children", intervention=pd, pd_reference_number=pd.number, section_name=section,
            lower_result_name="1.1", display_type="number", unit="number", baseline_numerator=Decimal(0),
        )  # fmt: skip
        cumulative = 0
        for (year, month), value in sorted(reports.items()):
            self.n_row += 1
            cumulative += value
            end = datetime.date(year, month, calendar.monthrange(year, month)[1])
            dm.ReportedIndicator.objects.create(
                datamart_id=self.n_row, partner=partner, intervention=pd, partner_name=partner.name,
                pd_reference_number=pd.number, progress_report=f"PR-{self.n_row}", report_number=f"MPR{self.n_row}",
                report_type="QPR", report_status="Accepted", period_start=datetime.date(year, month, 1),
                period_end=end, due_date=end + datetime.timedelta(days=15),
                submission_date=end + datetime.timedelta(days=10), indicator=CHILDREN, target="100000",
                location=self.place.name, p_code=self.place.p_code, location_ref=self.place,
                achievement_in_period=str(value), total_cumulative_progress=str(cumulative),
                total_cumulative_progress_in_location=str(value), calculation_across_locations="sum",
                etools_indicator_id=str(1000 + n),
            )  # fmt: skip
        return pd

    def ai(self, year: int, section: str, records: dict[int, int], *, current: bool = False) -> Database:
        """An ActivityInfo database of ``year`` for ``section``: one SUM master counting children in the year's
        HPM report, with ``records[month]`` children each month."""
        self.n_db += 1
        n = self.n_db
        ryear, _ = ReportingYear.objects.get_or_create(
            name=str(year), defaults={"year": str(year), "current": current}
        )
        database = Database.objects.create(
            ai_id=900000 + n, db_id=f"db{n}", name=f"{section} {year}", label=section, username="", password="",
            section=self.sections[section], reporting_year=ryear, is_funded_by_unicef=False,
        )  # fmt: skip
        activity = Activity.objects.create(database=database, name="Act", label="Act", ai_form_id=f"f{n}")
        leaf = IndicatorNew.objects.create(
            database=database, activity=activity, ai_indicator=f"i{n}", name="Children reached_Male",
            awp_code="1.1",
        )  # fmt: skip
        sub = SubIndicator.objects.create(
            database=database, name="Children reached", awp_code="1.1", aggregation_method="SUM"
        )
        sub.indicators.set([leaf])
        master = MasterIndicator.objects.create(
            database=database, name="Children reached (total)", awp_code="1", aggregation_method="SUM",
            awp_target=1000, sequence=1,
        )  # fmt: skip
        MasterSubIndicator.objects.create(master=master, sub=sub, effect="TOTAL", sequence=1)
        for month, value in sorted(records.items()):
            ActivityReportNew.objects.create(
                dbase=database, database_ai_id=str(database.ai_id), indicator_id=f"i{n}", indicator_name="x",
                indicator_value=value, month_name=f"{year}-{month:02d}-01", month=f"{year}-{month:02d}",
                partner_label="P", project_label="LEB/PCA", location_adminlevel_governorate="Akkar",
                location_adminlevel_governorate_code="AKK", location_adminlevel_caza="Akkar",
                location_adminlevel_caza_code="AKK1", location_name="Site", funded_by="UNICEF",
                last_edited_time=datetime.datetime(year, month, 20, tzinfo=datetime.UTC),
            )  # fmt: skip
        report, _ = NeuroReport.objects.get_or_create(name=f"HPM {year}", is_hpm=True, ryear=ryear)
        NeuroReportMasterIndicator.objects.create(report=report, master=master, label="Children", target=1200)
        activityinfo.invalidate()  # records reach the state at the end of an import, which drops it
        compute.reset_memo()
        return database
