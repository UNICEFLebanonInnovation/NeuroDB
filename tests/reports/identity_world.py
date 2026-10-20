"""A small world of partner report rows for the report identity (findings ID-1, ID-6, ID-7): three running
programme documents' worth of quarterly and monthly reports, every status, two indicators of one PD sharing a
title under two outputs, and two sharing both, a PD NeuroDB does not hold and a row naming no PD.

Each report is written in one of three shapes, the ways PRP's ids can come (``SHAPES``):

* ``prp_ids``: PRP's progress report id per report, its indicator report id per indicator report, and the
  PD indicator's eTools id on every row (what the tests and the demo had so far);
* ``ir_only``: no progress report id, an indicator report id, no eTools id;
* ``production``: none of the three (what the Lebanon rows carry on 20 Oct 2026).

Every object has a fixed id, so the figures read the same ids whatever ran before; nothing names a person."""

from __future__ import annotations

import datetime
from decimal import Decimal

from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

TODAY = datetime.date(2026, 10, 20)
SHAPES = ("prp_ids", "ir_only", "production")
ID = 7_400_000  # the fixed ids of the world's partners, programme documents and rows start here

CHILDREN = "# of children reached with PSS"
CAREGIVERS = "# of caregivers reached with parenting sessions"
KITS = "# of emergency kits distributed"
WELLBEING = "% of children with improved wellbeing"
YOUTH = "# of youth trained"  # two indicators of PD B, under two outputs
HOUSEHOLDS = "# of households supported"  # two indicators of PD B, under one output
NOT_HELD = "LEB/PCA-X/PD9"  # a programme document NeuroDB does not hold

PLACES = {"Akkar": "LB11", "Bekaa": "LB21"}
QUARTERS = {
    "QPR1": (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31)),
    "QPR2": (datetime.date(2026, 4, 1), datetime.date(2026, 6, 30)),
    "QPR3": (datetime.date(2026, 7, 1), datetime.date(2026, 9, 30)),
    "QPR4": (datetime.date(2026, 10, 1), datetime.date(2026, 12, 31)),
}
MONTHS = {
    f"HR{m}": (datetime.date(2026, m, 1), datetime.date(2026, m + 1, 1) - datetime.timedelta(days=1))
    for m in range(1, 6)
}


def _moment(day: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{day}T12:00:00+00:00")


class World:
    """The world in one shape: ``build()`` writes it; ``pds`` and ``partners`` by letter."""

    def __init__(self, shape: str) -> None:
        assert shape in SHAPES
        self.shape = shape
        self.next_row = ID + 1000
        self.pds: dict[str, PCA] = {}
        self.partners: dict[str, PartnerOrganization] = {}

    # ------------------------------------------------------------------------------ the definitions
    def _pd(self, letter: str, n: int, status: str, start: datetime.date, end: datetime.date) -> PCA:
        partner = PartnerOrganization.objects.create(
            id=ID + n, etl_id=str(ID + n), name=f"Partner {letter}", partner_type="CSO", vendor_number=f"V{n}"
        )
        pd = PCA.objects.create(
            id=ID + 100 + n,
            etl_id=str(ID + 100 + n),
            partner=partner,
            partner_name=partner.name,
            number=f"LEB/PCA-{letter}/PD{n}",
            title=f"Programme {letter}",
            status=status,
            start=start,
            end=end,
        )
        self.partners[letter], self.pds[letter] = partner, pd
        return pd

    def _indicator(self, pd: PCA, source_id: int, title: str, output: str, **extra) -> None:
        for place, p_code in PLACES.items():
            dm.PDIndicator.objects.create(
                datamart_id=source_id * 10 + (1 if place == "Akkar" else 2),
                source_id=source_id,
                intervention=pd,
                pd_reference_number=pd.number,
                title=title,
                section_name="Child Protection",
                lower_result_name=output,
                target_numerator=Decimal("1000"),
                baseline_numerator=Decimal("0"),
                display_type=extra.get("display_type", "number"),
                unit=extra.get("unit", "number"),
                location_name=place,
                location_pcode=p_code,
                is_high_frequency=extra.get("is_high_frequency", False),
            )

    # --------------------------------------------------------------------------------- the reports
    def report(
        self,
        pd: PCA | None,
        reference: str,
        source_id: int | None,
        title: str,
        output: str,
        number: str,
        status: str,
        values: dict[str, object],
        changed: str,
        *,
        sequence: int,
        partner: PartnerOrganization | None = None,
        across_locations: str = "sum",
        across_periods: str = "sum",
        rows_per_place: dict[str, list[object]] | None = None,
        period: tuple[datetime.date, datetime.date] | None = None,
    ) -> None:
        """One indicator in one progress report, a row per location (``rows_per_place``: a location given
        several rows, each with its value). ``status``: PRP's word; Accepted, Submitted and Sent back
        carry a submission date, Due none."""
        kind = "HR" if number.startswith("HR") else "QPR"
        start, end = period or (MONTHS if kind == "HR" else QUARTERS)[number]
        ir = f"{source_id or 99}{sequence:02d}"
        given = {
            "prp_ids": (f"{reference}/{number}", ir, str(source_id or "")),
            "ir_only": ("", ir, ""),
            "production": ("", "", ""),
        }[self.shape]
        progress_report, indicator_report, etools_id = given
        submitted = end + datetime.timedelta(days=10) if status != "Due" else None
        cells = rows_per_place or {place: [value] for place, value in values.items()}
        rows = []
        for place, place_values in cells.items():
            for value in place_values:
                self.next_row += 1
                if isinstance(value, tuple):  # a percentage: numerator and denominator
                    total = {"v": value[0], "d": value[1], "c": round(value[0] / value[1] * 100, 2)}
                elif value is None:
                    total = None
                else:
                    total = {"v": value, "d": 1, "c": value}
                rows.append(
                    dm.ReportedIndicator(
                        datamart_id=self.next_row,
                        partner=partner if partner is not None else (pd.partner if pd else None),
                        intervention=pd,
                        partner_name=pd.partner_name if pd else "",
                        pd_reference_number=reference,
                        progress_report=progress_report,
                        indicator_report=indicator_report,
                        report_number=number,
                        report_type=kind,
                        report_status=status,
                        period_start=start,
                        period_end=end,
                        due_date=end + datetime.timedelta(days=15),
                        submission_date=submitted,
                        last_modify_date=_moment(changed),
                        section="Child Protection",
                        pd_output=output,
                        indicator=title,
                        target="1000",
                        location=place,
                        p_code=PLACES[place],
                        achievement_in_period="" if total is None else str(value),
                        disaggregation={} if total is None else {"()": total},
                        calculation_across_locations=across_locations,
                        calculation_across_periods=across_periods,
                        etools_indicator_id=etools_id,
                        pd_output_progress_status="On Track",
                    )
                )
        dm.ReportedIndicator.objects.bulk_create(rows)

    def build(self) -> World:
        a = self._pd("A", 1, "active", datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        b = self._pd("B", 2, "active", datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        c = self._pd("C", 3, "closed", datetime.date(2025, 1, 1), datetime.date(2025, 12, 31))
        self._indicator(a, 1101, CHILDREN, "1.1 Protection services")
        self._indicator(a, 1102, CAREGIVERS, "1.2 Caregivers")
        self._indicator(a, 1103, KITS, "1.3 Emergency response", is_high_frequency=True)
        self._indicator(a, 1104, WELLBEING, "1.4 Wellbeing", display_type="percentage", unit="percentage")
        self._indicator(b, 2101, CHILDREN, "1.1 Protection services")
        self._indicator(b, 2201, YOUTH, "2.1 Skills")
        self._indicator(b, 2202, YOUTH, "2.2 Livelihoods")
        self._indicator(b, 2301, HOUSEHOLDS, "3.1 Shelter")
        self._indicator(b, 2302, HOUSEHOLDS, "3.1 Shelter")
        self._indicator(c, 3101, CHILDREN, "1.1 Protection services")
        ra, rb = a.number, b.number
        # PD A, children: three quarters counted (the third awaiting acceptance), the fourth not yet due
        for seq, (number, status, values, changed) in enumerate(
            [
                ("QPR1", "Accepted", {"Akkar": 200, "Bekaa": 100}, "2026-04-12"),
                ("QPR2", "Accepted", {"Akkar": 150, "Bekaa": 50}, "2026-07-12"),
                ("QPR3", "Submitted", {"Akkar": 80, "Bekaa": 20}, "2026-10-10"),
                ("QPR4", "Due", {"Akkar": None}, "2026-10-01"),
            ],
            start=1,
        ):
            self.report(
                a,
                ra,
                1101,
                CHILDREN,
                "1.1 Protection services",
                number,
                status,
                values,
                changed,
                sequence=seq,
            )
        # PD A, caregivers: QPR2's rows still say Submitted, synced before the children's said Accepted (a
        # partial re-sync: one report, two words)
        for seq, (number, status, value, changed) in enumerate(
            [
                ("QPR1", "Accepted", 40, "2026-04-12"),
                ("QPR2", "Submitted", 30, "2026-07-05"),
                ("QPR3", "Submitted", 10, "2026-10-10"),
            ],
            start=1,
        ):
            self.report(
                a,
                ra,
                1102,
                CAREGIVERS,
                "1.2 Caregivers",
                number,
                status,
                {"Akkar": value},
                changed,
                sequence=seq,
            )
        # PD A, kits: monthly, a sent-back and an overdue month, and two rows of one place in HR2
        for seq, (number, status, cells, changed) in enumerate(
            [
                ("HR1", "Accepted", {"Akkar": [30]}, "2026-02-10"),
                ("HR2", "Accepted", {"Akkar": [20, 25]}, "2026-03-10"),
                ("HR3", "Sent back", {"Akkar": [99]}, "2026-04-10"),
                ("HR4", "Due", {"Akkar": [0]}, "2026-05-01"),
                ("HR5", "Accepted", {"Akkar": [15], "Bekaa": [5]}, "2026-06-10"),
            ],
            start=1,
        ):
            self.report(
                a,
                ra,
                1103,
                KITS,
                "1.3 Emergency response",
                number,
                status,
                {},
                changed,
                sequence=seq,
                rows_per_place=cells,
            )
        # PD A, a percentage: numerators over denominators, the latest period
        self.report(
            a,
            ra,
            1104,
            WELLBEING,
            "1.4 Wellbeing",
            "QPR1",
            "Accepted",
            {"Akkar": (30, 50), "Bekaa": (20, 50)},
            "2026-04-12",
            sequence=1,
            across_periods="latest",
        )
        self.report(
            a,
            ra,
            1104,
            WELLBEING,
            "1.4 Wellbeing",
            "QPR2",
            "Accepted",
            {"Akkar": (40, 50)},
            "2026-07-12",
            sequence=2,
            across_periods="latest",
        )
        # PD B, children: its QPR1 (the same number and dates as PD A's) is overdue, changed after PD A's
        self.report(
            b,
            rb,
            2101,
            CHILDREN,
            "1.1 Protection services",
            "QPR1",
            "Due",
            {"Akkar": 60},
            "2026-09-01",
            sequence=1,
        )
        self.report(
            b,
            rb,
            2101,
            CHILDREN,
            "1.1 Protection services",
            "QPR2",
            "Accepted",
            {"Akkar": 70},
            "2026-07-01",
            sequence=2,
        )
        # PD B, in QPR2: two indicators with one title under two outputs, and two with one title and one output
        self.report(
            b, rb, 2201, YOUTH, "2.1 Skills", "QPR2", "Accepted", {"Akkar": 12}, "2026-07-01", sequence=2
        )
        self.report(
            b, rb, 2202, YOUTH, "2.2 Livelihoods", "QPR2", "Accepted", {"Akkar": 30}, "2026-07-01", sequence=2
        )
        self.report(
            b, rb, 2301, HOUSEHOLDS, "3.1 Shelter", "QPR2", "Accepted", {"Akkar": 5}, "2026-07-01", sequence=2
        )
        self.report(
            b, rb, 2302, HOUSEHOLDS, "3.1 Shelter", "QPR2", "Accepted", {"Bekaa": 7}, "2026-07-01", sequence=2
        )
        # PD C, closed: a report of 2025
        self.report(
            c,
            c.number,
            3101,
            CHILDREN,
            "1.1 Protection services",
            "QPR1",
            "Accepted",
            {"Akkar": 9},
            "2025-04-12",
            sequence=1,
            period=(datetime.date(2025, 1, 1), datetime.date(2025, 3, 31)),
        )
        # a programme document NeuroDB does not hold (its QPR1 has PD A's number and dates), and a row naming
        # no programme document at all
        self.report(
            None,
            NOT_HELD,
            9901,
            CHILDREN,
            "1.1 Protection services",
            "QPR1",
            "Accepted",
            {"Akkar": 500},
            "2026-04-12",
            sequence=1,
        )
        self.report(
            None,
            "",
            9902,
            CHILDREN,
            "1.1 Protection services",
            "QPR1",
            "Accepted",
            {"Akkar": 1},
            "2026-04-12",
            sequence=1,
        )
        return self
