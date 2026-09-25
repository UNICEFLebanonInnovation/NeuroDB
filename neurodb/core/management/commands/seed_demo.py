"""Fill an empty local database with realistic demo data for development and design reviews.

Refuses to run outside DJANGO_ENV=local/test and on a database that already holds databases,
so it can never touch production data. Usage:

    python manage.py migrate
    python manage.py seed_demo --password <choose-one>
"""

from __future__ import annotations

import datetime as dt
import random
import secrets

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER, ensure_groups
from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.geo.models import DistrictLocation, GovernorateLocation
from neurodb.indicators.models import (
    Activity,
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterSubIndicator,
    NeuroReport,
    NeuroReportComment,
    NeuroReportMasterIndicator,
    ReportingYear,
    SubIndicator,
)
from neurodb.indicators.services.navigation import invalidate as invalidate_navigation
from neurodb.library.models import Map, Resource, ResourceTag, ResourceTopic, ResourceType
from neurodb.partnerships.models import PCA, PartnerOrganization

# Rough rectangles per governorate: enough for a readable choropleth, not survey-grade geometry.
GOVERNORATES = [
    ("AKK", "Akkar", (35.95, 34.45, 36.45, 34.69)),
    ("NOR", "North", (35.75, 34.20, 36.10, 34.45)),
    ("BEK", "Baalbek-Hermel", (36.10, 33.95, 36.60, 34.45)),
    ("BEQ", "Bekaa", (35.75, 33.40, 36.10, 33.95)),
    ("MOU", "Mount Lebanon", (35.45, 33.55, 35.75, 34.20)),
    ("BEI", "Beirut", (35.46, 33.86, 35.54, 33.91)),
    ("NAB", "Nabatieh", (35.45, 33.10, 35.75, 33.55)),
    ("SOU", "South", (35.10, 33.05, 35.45, 33.55)),
]
SECTIONS = [
    (
        "Child Protection",
        "CP",
        "#e03c31",
        [
            ("Children reached with case management", "SUM", 12000),
            ("Caregivers reached with parenting programmes", "SUM", 8000),
            ("Children in psychosocial support", "SUM", 20000),
            ("Share of girls among children reached", "SUM_OVER_SUM", 50),
        ],
    ),
    (
        "Education",
        "EDU",
        "#1cabe2",
        [
            ("Children enrolled in formal education", "MAXIMUM", 450000),
            ("Children in non-formal education", "SUM", 60000),
            ("Teachers trained", "SUM", 3500),
            ("Schools rehabilitated", "COUNT", 120),
        ],
    ),
    (
        "WASH",
        "WASH",
        "#00833d",
        [
            ("People with access to safe water", "AVERAGE", 900000),
            ("People reached with hygiene kits", "SUM", 150000),
            ("Informal settlements with sanitation", "SUM", 1800),
        ],
    ),
    (
        "Health and Nutrition",
        "HN",
        "#ffc20e",
        [
            ("Children vaccinated against measles", "SUM", 160000),
            ("Children screened for malnutrition", "SUM", 90000),
            ("Pregnant women receiving micronutrients", "SUM", 30000),
        ],
    ),
]
PARTNERS = [
    ("Terre des Hommes", "International"),
    ("Save the Children", "International"),
    ("Himaya", "National"),
    ("Amel Association", "National"),
    ("Caritas Lebanon", "National"),
    ("World Vision", "International"),
    ("Makhzoumi Foundation", "National"),
    ("SAWA for Development", "Community Based Organisation"),
]
DONORS = ["European Union", "Germany (BMZ)", "United Kingdom (FCDO)", "USAID", "Japan", "Norway", "CERF"]
GENDERS = ["Male", "Female"]
NATIONALITIES = ["Lebanese", "Syrian", "Palestine"]  # v2 column is varchar(10)


def box(x1: float, y1: float, x2: float, y2: float) -> list[str]:
    return [f"[{x}, {y}]" for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1))]


class Command(BaseCommand):
    help = "Seed an empty local database with demo data (never runs in staging or production)."

    def add_arguments(self, parser):
        parser.add_argument("--password", help="Password for the demo users (random if omitted).")
        parser.add_argument("--months", type=int, default=8, help="Months of facts to generate (1-12).")

    def handle(self, *args, **options):
        if settings.ENV not in ("local", "test"):
            raise CommandError("seed_demo only runs with DJANGO_ENV=local or test.")
        if Database.objects.exists():
            raise CommandError(
                "This database already holds NeuroDB databases; seed_demo only fills an empty one."
            )
        password = options["password"] or secrets.token_urlsafe(12)
        months = max(1, min(12, options["months"]))
        rng = random.Random(7)  # noqa: S311 - reproducible demo data, not security-relevant
        with transaction.atomic():
            self._seed(rng, password, months)
        invalidate_navigation()
        self.stdout.write(
            self.style.SUCCESS("Demo data created. Users: demo-admin, demo-editor, demo-viewer.")
        )
        if not options["password"]:
            self.stdout.write(f"Generated password for the demo users: {password}")

    # ------------------------------------------------------------------ steps
    def _seed(self, rng: random.Random, password: str, months: int) -> None:
        groups = ensure_groups()
        year = ReportingYear.objects.create(name="2026", year="2026", current=True)
        ReportingYear.objects.create(name="2025", year="2025", current=False)
        for code, name, (x1, y1, x2, y2) in GOVERNORATES:
            GovernorateLocation.objects.create(
                code=code, name=name, polygon_coordinates=box(x1, y1, x2, y2), ai_id=0
            )
            mx = (x1 + x2) / 2
            DistrictLocation.objects.create(
                code=f"{code}1", gov_code=code, name=f"{name} West", polygon_coordinates=box(x1, y1, mx, y2)
            )
            DistrictLocation.objects.create(
                code=f"{code}2", gov_code=code, name=f"{name} East", polygon_coordinates=box(mx, y1, x2, y2)
            )

        sections = []
        for s_name, s_code, color, _ in SECTIONS:
            sections.append(
                Section.objects.create(
                    name=s_name, code=s_code, color=color, have_hpm_indicator=True, etools=True
                )
            )
        self._users(groups, sections[0], password)

        hpm = NeuroReport.objects.create(name="HPM 2026", report_code="HPM", ryear=year, is_hpm=True)
        results = NeuroReport.objects.create(
            name="Country programme results 2026", report_code="CPR", ryear=year, is_hpm=False
        )
        for index, (section, (s_name, s_code, _color, masters)) in enumerate(
            zip(sections, SECTIONS, strict=True)
        ):
            db = Database.objects.create(
                ai_id=2026 * 100 + 10 + index,
                db_id=f"demo{index}",
                name=f"{s_name} 2026",
                label=s_name,
                username="",
                password="",
                section=section,
                reporting_year=year,
                is_funded_by_unicef=False,
                display=True,
                last_monthly_update_date=timezone.now() - dt.timedelta(days=rng.choice([2, 5, 9, 52])),
            )
            activity = Activity.objects.create(
                database=db, name=f"{s_name} response", label=f"{s_name} response", ai_form_id=f"form{index}"
            )
            for m_index, (label, method, target) in enumerate(masters, start=1):
                master = self._indicator(rng, db, activity, s_code, m_index, label, method, target, months)
                link = NeuroReportMasterIndicator.objects.create(
                    report=hpm, master=master, label=label, target=target
                )
                NeuroReportMasterIndicator.objects.create(
                    report=results, master=master, label=label, target=target
                )
                if m_index == 1:
                    NeuroReportComment.objects.create(
                        report=hpm,
                        master=link,
                        related_month=f"{months:02d}",
                        is_active=True,
                        comment=f"{s_name}: partners scaled up in the North after the funding top-up.",
                    )
        self._partnerships(rng)
        self._library()
        self._population(rng)
        self._sync_runs()

    def _users(self, groups, section, password: str) -> None:
        admin = User.objects.create_user(
            "demo-admin",
            "demo-admin@example.org",
            password,
            is_staff=True,
            is_superuser=True,
            first_name="Demo",
            last_name="Admin",
        )
        admin.groups.add(Group.objects.get(name=ADMIN))
        editor = User.objects.create_user(
            "demo-editor",
            "demo-editor@example.org",
            password,
            first_name="Demo",
            last_name="Editor",
            section=section,
        )
        editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
        viewer = User.objects.create_user(
            "demo-viewer", "demo-viewer@example.org", password, first_name="Demo", last_name="Viewer"
        )
        viewer.groups.add(Group.objects.get(name=VIEWER))

    def _indicator(
        self, rng, db, activity, s_code, m_index, label, method, target, months
    ) -> MasterIndicator:
        awp = f"{s_code}.{m_index}"
        master = MasterIndicator.objects.create(
            database=db,
            name=label,
            awp_code=awp,
            aggregation_method=method,
            awp_target=target,
            sequence=m_index,
            is_active=True,
            unit="%" if method == "SUM_OVER_SUM" else None,
        )
        leaves = []
        for gender in GENDERS:
            for nat in NATIONALITIES:
                leaves.append(
                    IndicatorNew.objects.create(
                        database=db,
                        activity=activity,
                        ai_indicator=f"{db.db_id}_{m_index}_{gender[0]}{nat[0]}",
                        name=f"{label}_{gender}_{nat}",
                        awp_code=awp,
                        gender=gender,
                        nationality=nat,
                    )
                )
        if method == "SUM_OVER_SUM":
            num = SubIndicator.objects.create(
                database=db, name=f"{label} (girls)", awp_code=f"{awp}.1", aggregation_method="SUM"
            )
            num.indicators.set([leaf for leaf in leaves if leaf.gender == "Female"])
            den = SubIndicator.objects.create(
                database=db, name=f"{label} (all)", awp_code=f"{awp}.2", aggregation_method="SUM"
            )
            den.indicators.set(leaves)
            MasterSubIndicator.objects.create(master=master, sub=num, effect="NUMERATOR", sequence=1)
            MasterSubIndicator.objects.create(master=master, sub=den, effect="DENOMINATOR", sequence=2)
        else:
            for n, gender in enumerate(GENDERS, start=1):
                sub = SubIndicator.objects.create(
                    database=db,
                    name=f"{label} ({gender.lower()})",
                    awp_code=f"{awp}.{n}",
                    aggregation_method="SUM" if method != "COUNT" else "COUNT",
                )
                sub.indicators.set([leaf for leaf in leaves if leaf.gender == gender])
                MasterSubIndicator.objects.create(master=master, sub=sub, effect="TOTAL", sequence=n)
        # Facts: pace each indicator differently so the dashboard shows every tracking status.
        pace = rng.choice([0.35, 0.55, 0.62, 0.7, 0.95, 1.2])
        per_record = max(1, int(target * pace / max(1, months * 10)))
        for month in range(1, months + 1):
            for _ in range(rng.randint(6, 12)):
                code, gov, _ = rng.choice(GOVERNORATES)
                partner, _ = rng.choice(PARTNERS)
                leaf = rng.choice(leaves)
                site = f"{gov} site {rng.randint(1, 9)}"
                x1, y1, x2, y2 = next(b for c, _, b in GOVERNORATES if c == code)
                ActivityReportNew.objects.create(
                    dbase=db,
                    database_ai_id=str(db.ai_id),
                    indicator_id=leaf.ai_indicator,
                    indicator_name=leaf.name,
                    indicator_awp_code=awp,
                    indicator_value=rng.randint(per_record // 2, per_record * 2),
                    month_name=f"2026-{month:02d}-01",
                    month=f"2026-{month:02d}",
                    partner_label=partner,
                    project_label=f"LEB/PCA2026{rng.randint(1, 12):03d}",
                    funded_by="UNICEF",
                    location_adminlevel_governorate=gov,
                    location_adminlevel_governorate_code=code,
                    location_adminlevel_caza=f"{gov} {'West' if rng.random() < 0.5 else 'East'}",
                    location_adminlevel_caza_code=f"{code}{rng.choice([1, 2])}",
                    location_name=site,
                    location_latitude=f"{rng.uniform(y1, y2):.5f}",
                    location_longitude=f"{rng.uniform(x1, x2):.5f}",
                    emergency="yes" if rng.random() < 0.15 else "no",
                    last_edited_time=timezone.make_aware(dt.datetime(2026, month, 10))
                    if month < 12
                    else timezone.now(),
                )
        return master

    def _partnerships(self, rng) -> None:
        today = dt.date.today()
        partners = []
        for n, (name, cso) in enumerate(PARTNERS, start=1):
            partners.append(
                PartnerOrganization.objects.create(
                    etl_id=str(100 + n),
                    name=name,
                    short_name="".join(w[0] for w in name.split())[:6].upper(),
                    partner_type="Civil Society Organization",
                    cso_type=cso,
                    vendor_number=f"25{n:05d}",
                    rating=rng.choice(["Low", "Medium", "Significant", "High"]),
                    total_ct_cp=str(rng.randint(50, 900) * 1000),
                    total_ct_cy=str(rng.randint(10, 300) * 1000),
                    last_assessment_date=today - dt.timedelta(days=rng.randint(60, 700)),
                )
            )
        section_names = [s[0] for s in SECTIONS]
        for n in range(1, 13):
            partner = rng.choice(partners)
            start = today - dt.timedelta(days=rng.randint(30, 600))
            donors = rng.sample(DONORS, rng.randint(1, 3))
            PCA.objects.create(
                etl_id=str(500 + n),
                partner=partner,
                partner_name=partner.name,
                number=f"LEB/PCA2026{n:03d}-1",
                title=f"{rng.choice(section_names)} services for vulnerable children and families ({n})",
                status=rng.choice(["active", "active", "active", "ended", "signed"]),
                document_type=rng.choice(["PD", "SPD"]),
                start=start,
                end=start + dt.timedelta(days=rng.choice([180, 365, 540])),
                section_names=rng.sample(section_names, rng.randint(1, 2)),
                offices_set=[rng.choice(["Beirut", "Zahle", "Tripoli", "Tyre"])],
                donors=donors,
                grants=[f"SC{rng.randint(100000, 999999)}" for _ in donors],
                donors_set=[
                    {
                        "donor": d,
                        "grant": f"SC{rng.randint(100000, 999999)}",
                        "value": str(rng.randint(40, 600) * 1000),
                    }
                    for d in donors
                ],
                location_p_codes=[f"LB{rng.randint(1000, 9999)}" for _ in range(rng.randint(2, 8))],
                total_budget=str(rng.randint(100, 2500) * 1000),
            )

    def _library(self) -> None:
        types = {
            n: ResourceType.objects.create(name=n) for n in ("Evaluation", "Study", "Assessment", "Brief")
        }
        topics = {
            n: ResourceTopic.objects.create(name=n)
            for n in ("Learning", "Protection", "Nutrition", "Social protection")
        }
        tags = [
            ResourceTag.objects.create(name=n)
            for n in ("Syrian refugees", "Adolescents", "Disability", "Gender")
        ]
        items = [
            (
                "Out-of-school children in Lebanon: barriers and pathways",
                "2025",
                "Education",
                "Study",
                "Learning",
            ),
            (
                "Evaluation of the case management programme 2021-2024",
                "2025",
                "Child Protection",
                "Evaluation",
                "Protection",
            ),
            (
                "Nutrition SMART survey among refugees",
                "2024",
                "Health and Nutrition",
                "Assessment",
                "Nutrition",
            ),
            (
                "Haddi child grant: impact on household wellbeing",
                "2025",
                "Social Policy",
                "Brief",
                "Social protection",
            ),
            ("Water quality monitoring in informal settlements", "2024", "WASH", "Assessment", "Protection"),
            (
                "Adolescent participation in community programmes",
                "2023",
                "Youth & Adolescent",
                "Study",
                "Learning",
            ),
        ]
        for n, (title, year, section, rtype, topic) in enumerate(items):
            r = Resource.objects.create(
                title=title,
                publication_year=year,
                section=section,
                type=types[rtype],
                topic=topics[topic],
                description=(
                    "Key findings and recommendations for programme design, targeting and monitoring "
                    "in Lebanon."
                ),
                resource_link="https://www.unicef.org/lebanon/reports",
            )
            r.tags.set(tags[n % 4 : n % 4 + 2])
        for name in (
            "Schools supported, 2025-2026",
            "Water network rehabilitation",
            "Child protection service map",
        ):
            Map.objects.create(
                name=name,
                status="Completed",
                link="https://www.unicef.org/lebanon",
                description="Interactive map produced by the information management team.",
            )

    def _population(self, rng) -> None:
        figures = []
        for nat, share in (("LEB", 0.62), ("SYR", 0.30), ("PRL", 0.04), ("PRS", 0.01), ("OTH", 0.03)):
            total = int(5_800_000 * share)
            figures.append(
                PopulationFigure(
                    year=2025, nationality=nat, level="national", value=total, category="total", source="Demo"
                )
            )
            figures.append(
                PopulationFigure(
                    year=2025,
                    nationality=nat,
                    level="national",
                    value=int(total * 0.38),
                    category="children",
                    source="Demo",
                )
            )
            for code, name, _ in GOVERNORATES:
                figures.append(
                    PopulationFigure(
                        year=2025,
                        nationality=nat,
                        level="governorate",
                        area_code=code,
                        area_name=name,
                        value=int(total * rng.uniform(0.06, 0.2)),
                        category="total",
                        source="Demo",
                    )
                )
            for band in ("0-4", "5-9", "10-14", "15-19", "20-59", "60+"):
                figures.append(
                    PopulationFigure(
                        year=2025,
                        nationality=nat,
                        level="national",
                        age_group=band,
                        value=int(total * rng.uniform(0.05, 0.3)),
                        category="total",
                        source="Demo",
                    )
                )
        PopulationFigure.objects.bulk_create(figures)

    def _sync_runs(self) -> None:
        now = timezone.now()
        for job, hours, status in (
            ("ai_structure", 20, "succeeded"),
            ("ai_data", 6, "succeeded"),
            ("etools", 3, "partial"),
            ("etools", 27, "succeeded"),
            ("locations", 400, "succeeded"),
            ("population", 2, "failed"),
        ):
            run = SyncRun.objects.create(
                job=job,
                status=status,
                rows_in=1200,
                rows_written=1180 if status != "failed" else 0,
                rows_failed=20 if status == "partial" else 0,
                error="Upstream returned 503" if status == "failed" else "",
            )
            SyncRun.objects.filter(pk=run.pk).update(
                started_at=now - dt.timedelta(hours=hours, minutes=5),
                finished_at=now - dt.timedelta(hours=hours),
            )
