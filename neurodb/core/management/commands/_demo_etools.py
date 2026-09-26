"""Demo eTools records for ``seed_demo``: a gazetteer, PD indicators with partner reports, funds
reservations, TPM visits, action points and field monitoring findings, so the overview, the partner
monitoring pages and the assurance pages show something on a local database.

Shapes follow what the Datamart sync writes (``neurodb.integrations.etools.datamart_sync``); the
numbers are random but paced so that every status appears.
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from neurodb.core.models import PopulationFigure
from neurodb.datamart import models as dm
from neurodb.datamart.tags import tags_of
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PCA

GOVERNORATES = [
    ("Akkar", "LB1", (35.95, 34.45, 36.45, 34.69)),
    ("North", "LB2", (35.75, 34.20, 36.10, 34.45)),
    ("Baalbek-Hermel", "LB3", (36.10, 33.95, 36.60, 34.45)),
    ("Beqaa", "LB4", (35.75, 33.40, 36.10, 33.95)),
    ("Mount Lebanon", "LB5", (35.45, 33.55, 35.75, 34.20)),
    ("Beirut", "LB6", (35.46, 33.86, 35.54, 33.91)),
    ("Nabatieh", "LB7", (35.45, 33.10, 35.75, 33.55)),
    ("South", "LB8", (35.10, 33.05, 35.45, 33.55)),
]
INDICATORS = {
    "Child Protection": [
        ("# of children reached with case management services", 1200),
        ("# of girls and boys receiving psychosocial support", 4000),
        ("# of caregivers reached with parenting programmes", 900),
    ],
    "Education": [
        ("# of children enrolled in non-formal education", 6000),
        ("# of students receiving learning materials", 9000),
        ("# of teachers trained on inclusive education", 250),
    ],
    "WASH": [
        ("# of children with access to safe water in schools", 15000),
        ("# of people reached with hygiene kits", 20000),
        ("# of informal settlements with functional sanitation", 60),
    ],
    "Health and Nutrition": [
        ("# of children under 5 screened for malnutrition", 8000),
        ("# of children vaccinated against measles", 12000),
        ("# of pregnant women receiving micronutrients", 1500),
    ],
}
TPM_STATUSES_PAST = ["unicef_approved", "unicef_approved", "tpm_reported", "tpm_accepted", "cancelled"]
MODULES = ["tpm", "fm", "audit"]


def _quarters(year: int, today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """The quarterly report periods of ``year`` that ended before ``today``."""
    out = []
    for q in range(4):
        start = dt.date(year, 3 * q + 1, 1)
        end = dt.date(year, 3 * q + 3, 1) + dt.timedelta(days=31)
        end = end.replace(day=1) - dt.timedelta(days=1)
        if end < today:
            out.append((start, end))
    return out


def seed_etools(rng: random.Random, today: dt.date) -> None:
    year = today.year
    gazetteer = _gazetteer(rng)
    pcas = list(PCA.objects.order_by("id"))
    sections = list(INDICATORS)
    counters = {"pdi": 0, "report": 0, "fr": 0, "line": 0}
    for pca in pcas:
        section = next((s for s in (pca.section_names or []) if s in INDICATORS), rng.choice(sections))
        places = rng.sample(gazetteer["cadasters"], rng.randint(2, 4))
        _indicators_and_reports(rng, pca, section, places, counters, today, year)
        _funds(rng, pca, counters)
    _tpm(rng, pcas, gazetteer, today, year)
    _action_points(rng, pcas, gazetteer, today, year)
    _findings(rng, pcas, gazetteer, today, year)
    _children_population(rng)


def _gazetteer(rng: random.Random) -> dict[str, list[Location]]:
    types = {
        level: LocationType.objects.create(name=name, admin_level=level)
        for level, name in ((0, "Country"), (1, "Governorate"), (2, "District"), (3, "Cadaster"))
    }
    tree = {"lft": 1, "rght": 2, "level": 0}
    country = Location.objects.create(id=1, name="Lebanon", p_code="LB", type=types[0], tree_id=1, **tree)
    governorates, districts, cadasters = [], [], []
    for n, (name, p_code, (x1, y1, x2, y2)) in enumerate(GOVERNORATES, start=1):
        gov = Location.objects.create(
            id=10 + n,
            name=name,
            p_code=p_code,
            type=types[1],
            parent=country,
            latitude=(y1 + y2) / 2,
            longitude=(x1 + x2) / 2,
            tree_id=1,
            **tree,
        )
        governorates.append(gov)
        for d, side in enumerate(("West", "East"), start=1):
            district = Location.objects.create(
                id=100 + n * 10 + d,
                name=f"{name} {side}",
                p_code=f"{p_code}{d}",
                type=types[2],
                parent=gov,
                latitude=(y1 + y2) / 2 + rng.uniform(-0.05, 0.05),
                longitude=(x1 + x2) / 2 + (-0.1 if side == "West" else 0.1),
                tree_id=1,
                **tree,
            )
            districts.append(district)
            for c in range(1, 3):
                cadasters.append(
                    Location.objects.create(
                        id=1000 + n * 100 + d * 10 + c,
                        name=f"{name} {side} cadaster {c}",
                        p_code=f"{p_code}{d}0{c}",
                        type=types[3],
                        parent=district,
                        latitude=rng.uniform(y1, y2),
                        longitude=rng.uniform(x1, x2),
                        tree_id=1,
                        **tree,
                    )
                )
    return {"governorates": governorates, "districts": districts, "cadasters": cadasters}


def _indicators_and_reports(rng, pca, section, places, counters, today, year) -> None:
    quarters = _quarters(year, today)
    pace = rng.choice([0.2, 0.5, 0.7, 0.85, 1.1])
    reports_pace = rng.choice(["none", "late", "all", "all", "all"])
    for i_index, (title, target) in enumerate(INDICATORS[section], start=1):
        counters["pdi"] += 1
        source_id = pca.id * 100 + i_index
        tags = tags_of(title)
        share = target / max(1, len(places))
        cumulative = 0.0
        rows = []
        for place in places:
            counters["pdi"] += 1
            rows.append(
                dm.PDIndicator(
                    datamart_id=counters["pdi"],
                    source_id=source_id,
                    intervention=pca,
                    pd_reference_number=pca.number or "",
                    title=title,
                    unit="number",
                    display_type="number",
                    baseline_numerator=Decimal(0),
                    target_numerator=Decimal(target),
                    section_name=section,
                    lower_result_name=f"Output {i_index}: {section} services",
                    location_name=place.name,
                    location_pcode=place.p_code,
                    location_source_id=place.id,
                    location=place,
                    is_high_frequency=False,
                    tag_gender=tags.gender,
                    tag_age_group=tags.age_group,
                    tag_nationality=tags.nationality,
                    tag_disability=tags.disability,
                )
            )
        dm.PDIndicator.objects.bulk_create(rows)
        if reports_pace == "none":
            continue
        report_rows = []
        for q, (start, end) in enumerate(quarters, start=1):
            due = end + dt.timedelta(days=30)
            last = q == len(quarters)
            missing = reports_pace == "late" and last
            per_period = share * pace / max(1, len(quarters))
            for place in places:
                value = 0.0 if missing else rng.uniform(per_period * 0.6, per_period * 1.4)
                cumulative += value
                counters["report"] += 1
                report_rows.append(
                    dm.ReportedIndicator(
                        datamart_id=counters["report"],
                        partner=pca.partner,
                        intervention=pca,
                        partner_name=pca.partner_name or "",
                        pd_reference_number=pca.number or "",
                        progress_report=f"{pca.number}/QPR{q}",
                        report_number=f"QPR{q}",
                        report_type="QPR",
                        report_status="Due" if missing else "Accepted",
                        period_start=start,
                        period_end=end,
                        due_date=due,
                        submission_date=None if missing else min(due, today) - dt.timedelta(days=3),
                        section=section,
                        indicator=title,
                        target=str(target),
                        location=place.name,
                        p_code=place.p_code,
                        location_ref=place,
                        achievement_in_period="" if missing else f"{value:.0f}",
                        total_cumulative_progress=f"{cumulative:.0f}",
                        total_cumulative_progress_in_location=f"{value:.0f}",
                        calculation_across_locations="sum",
                        calculation_across_periods="sum",
                        etools_indicator_id=str(source_id),
                        pd_output_progress_status="On Track" if pace >= 0.7 else "Constrained",
                        tag_gender=tags.gender,
                        tag_age_group=tags.age_group,
                        tag_nationality=tags.nationality,
                        tag_disability=tags.disability,
                    )
                )
        dm.ReportedIndicator.objects.bulk_create(report_rows)


def _funds(rng, pca, counters) -> None:
    budget = Decimal(pca.total_budget or 0) or Decimal(rng.randint(100, 900) * 1000)
    donors = pca.donors or ["European Union"]
    disbursed_share = Decimal(str(rng.choice([0.15, 0.35, 0.55, 0.7, 0.9])))
    for n, donor in enumerate(donors, start=1):
        counters["fr"] += 1
        total = (budget / len(donors)).quantize(Decimal("1"))
        actual = (total * disbursed_share).quantize(Decimal("1"))
        fr_number = f"04{pca.id:03d}{n:02d}"
        dm.FundsReservationHeader.objects.create(
            datamart_id=counters["fr"],
            intervention=pca,
            pd_reference_number=pca.number or "",
            fr_number=fr_number,
            fr_type="Programme Document Against PCA",
            vendor_code=pca.partner.vendor_number if pca.partner else "",
            currency="USD",
            total_amt=total,
            intervention_amt=total,
            actual_amt=actual,
            outstanding_amt=total - actual,
            document_date=pca.start,
            start_date=pca.start,
            end_date=pca.end,
        )
        counters["line"] += 1
        grants = pca.grants or []
        grant = grants[min(n - 1, len(grants) - 1)] if grants else f"SC{rng.randint(100000, 999999)}"
        dm.FundsReservation.objects.create(
            datamart_id=counters["line"],
            intervention=pca,
            pd_reference_number=pca.number or "",
            fr_number=fr_number,
            line_item=1,
            donor=donor,
            grant_number=grant,
            currency="USD",
            overall_amount=total,
            total_amt=total,
            actual_amt=actual,
            outstanding_amt=total - actual,
            start_date=pca.start,
            end_date=pca.end,
        )


def _tpm(rng, pcas, gazetteer, today, year) -> None:
    visit_id = activity_id = 0
    for month in range(1, 13):
        planned = rng.randint(3, 6)
        for _ in range(planned):
            visit_id += 1
            pca = rng.choice(pcas)
            start = dt.date(year, month, rng.randint(1, 26))
            end = start + dt.timedelta(days=rng.randint(1, 4))
            if end < today - dt.timedelta(days=21):
                status = rng.choice(TPM_STATUSES_PAST)
            elif end < today:
                status = rng.choice(["tpm_reported", "tpm_accepted", "assigned"])
            else:
                status = rng.choice(["assigned", "assigned", "draft"])
            visit = dm.TPMVisit.objects.create(
                datamart_id=visit_id,
                partner=pca.partner,
                partner_name=pca.partner_name or "",
                vendor_number=pca.partner.vendor_number if pca.partner else "",
                reference_number=f"TPM/{year}/{visit_id}",
                status=status,
                tpm_name="Monitoring Partner Ltd",
                start_date=start,
                end_date=end,
                date_of_unicef_approved=end + dt.timedelta(days=12) if status == "unicef_approved" else None,
            )
            place = rng.choice(gazetteer["cadasters"])
            activity_id += 1
            activity = dm.TPMActivity.objects.create(
                datamart_id=activity_id,
                partner=pca.partner,
                intervention=pca,
                visit=visit,
                visit_reference_number=visit.reference_number,
                task_reference_number=f"{visit.reference_number}/1",
                visit_status=status,
                status=status,
                tpm_name=visit.tpm_name,
                partner_name=visit.partner_name,
                vendor_number=visit.vendor_number,
                pd_reference_number=pca.number or "",
                section=next((s for s in (pca.section_names or []) if s in INDICATORS), "Child Protection"),
                locations=place.name,
                location_pcodes=[place.p_code],
                date=start,
            )
            activity.location_links.add(place)


def _action_points(rng, pcas, gazetteer, today, year) -> None:
    for n in range(1, 61):
        pca = rng.choice(pcas)
        due = dt.date(year, rng.randint(1, 12), rng.randint(1, 28))
        closed = due < today and rng.random() < 0.7
        place = rng.choice(gazetteer["cadasters"])
        module = rng.choice(MODULES)
        dm.ActionPoint.objects.create(
            datamart_id=n,
            partner=pca.partner,
            intervention=pca,
            reference_number=f"LEB/{year}/{n}/APD",
            description=rng.choice(
                [
                    "Submit the attendance sheets of the second quarter",
                    "Replace the damaged water tank at the site",
                    "Update the child safeguarding focal point list",
                    "Provide the receipts for the training venue",
                    "Correct the beneficiary count reported in the QPR",
                ]
            ),
            status="completed" if closed else "open",
            high_priority=rng.random() < 0.3,
            due_date=due,
            date_of_completion=dt.datetime.combine(
                due + dt.timedelta(days=rng.randint(-10, 40)), dt.time(10, 0), tzinfo=dt.UTC
            )
            if closed
            else None,
            assigned_to_name=rng.choice(["Programme officer", "Field monitor", "Partnership focal point"]),
            office="Lebanon",
            section=next((s for s in (pca.section_names or []) if s in INDICATORS), "Child Protection"),
            related_module=module,
            module_reference_number=f"{module.upper()}/{year}/{rng.randint(1, 40)}",
            partner_name=pca.partner_name or "",
            intervention_number=pca.number or "",
            location_name=place.name,
            location_pcode=place.p_code,
            location_source_id=place.id,
            location=place,
        )


def _findings(rng, pcas, gazetteer, today, year) -> None:
    for n in range(1, 51):
        pca = rng.choice(pcas)
        place = rng.choice(gazetteer["cadasters"])
        end = dt.date(year, rng.randint(1, max(1, today.month)), rng.randint(1, 28))
        if end > today:
            end = today
        dm.MonitoringFinding.objects.create(
            datamart_id=n,
            partner=pca.partner,
            vendor_number=pca.partner.vendor_number if pca.partner else "",
            entity=pca.number or "",
            entity_type="intervention",
            monitoring_activity=f"FM-{year}-{n:03d}",
            reference_number=f"FM/{year}/{n}",
            status="completed",
            overall_finding_rating=rng.choice(["On Track", "On Track", "On Track", "Off Track"]),
            narrative_finding="Activities observed as planned."
            if rng.random() < 0.75
            else "Attendance below plan.",
            start_date=end - dt.timedelta(days=1),
            end_date=end,
            location_name=place.name,
            location_pcode=place.p_code,
            location_source_id=place.id,
            location=place,
            site=place.name,
            monitoring_activity_id=n,
        )


def _children_population(rng) -> None:
    """Children per governorate (the coverage denominators of the overview)."""
    figures = []
    for name, p_code, _ in GOVERNORATES:
        for nat, share in (("LEB", 0.62), ("SYR", 0.30), ("PRL", 0.04), ("PRS", 0.01), ("OTH", 0.03)):
            figures.append(
                PopulationFigure(
                    year=2025,
                    nationality=nat,
                    level="governorate",
                    area_code=p_code,
                    area_name=name,
                    value=int(5_800_000 * share * 0.38 * rng.uniform(0.06, 0.2)),
                    category="children",
                    source="Demo",
                )
            )
    PopulationFigure.objects.bulk_create(figures, ignore_conflicts=True)
