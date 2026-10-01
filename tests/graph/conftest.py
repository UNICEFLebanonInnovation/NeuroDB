"""Shared records for the knowledge hub tests: one record of every source, related the way the real
data is."""

import datetime
from types import SimpleNamespace

import pytest

from neurodb.accounts.models import Section
from neurodb.cpd.models import CountryProgramme, Indicator, Outcome, Output
from neurodb.cpd.models import Link as CPDLink
from neurodb.datamart.models import Grant
from neurodb.geo.models import GovernorateLocation, Location, LocationType
from neurodb.indicators.models import MasterIndicator
from neurodb.knowledge.models import Document
from neurodb.knowledge.models import Link as KnowledgeLink
from neurodb.library.models import Map, Resource
from neurodb.partnerships.models import PCA, PartnerLink, PartnerOrganization
from neurodb.review.models import DailyReview, ReviewFinding
from neurodb.wellbeing.models import CenterSummary

NUMBER = "LEB/PCA2026005/PD2026012-1"


@pytest.fixture
def no_ai(monkeypatch):
    from neurodb.assistant.agent import AssistantUnavailable

    def unavailable():
        raise AssistantUnavailable("off")

    monkeypatch.setattr("neurodb.assistant.agent.client", unavailable)


@pytest.fixture
def media(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _place(pk, name, level, parent=None, p_code=""):
    kind, _ = LocationType.objects.get_or_create(name=f"Level {level}", admin_level=level)
    return Location.objects.create(
        id=pk, name=name, type=kind, parent=parent, p_code=p_code, lft=pk, rght=pk + 1, tree_id=1, level=level
    )


@pytest.fixture
def world(db, database, no_ai):
    """One record of every source, related the way the real data is."""
    education = Section.objects.create(name="Education", code="EDU")
    GovernorateLocation.objects.create(code="LB1", name="Akkar", ai_id=1)
    lebanon = _place(1, "Lebanon", 0)
    akkar = _place(10, "Akkar", 1, lebanon, "LB1")
    halba = _place(20, "Halba", 2, akkar, "LB11")
    village = _place(30, "Halba village", 3, halba)
    amel = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association International", short_name="AMEL", vendor_number="2500212345"
    )
    care = PartnerOrganization.objects.create(etl_id="2", name="CARE International", short_name="CARE")
    pd = PCA.objects.create(
        etl_id="11",
        partner=amel,
        partner_name=amel.name,
        number=NUMBER,
        title="Education support in the north",
        status="active",
        section_names=["Education"],
        donors=["European Union"],
        grants=["SC220001"],
        cp_outputs=["1.1 Access to learning"],
        country_programme="LEB CP 2026",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2027, 12, 31),
    )
    pd.locations.add(village)
    Grant.objects.create(datamart_id=1, name="SC220001", donor="European Union")
    master = MasterIndicator.objects.create(
        database=database, name="Children in case management", awp_code="3.1", aggregation_method="SUM"
    )
    PartnerLink.objects.create(
        label="Amel", partner=amel, method="manual", records=12, database_ids=[database.pk]
    )
    cycle = CountryProgramme.objects.create(
        name="Lebanon CP 2026-2028", start_year=2026, end_year=2028, current=True, etools_name="LEB CP 2026"
    )
    outcome = Outcome.objects.create(programme=cycle, code="1", title="Children learn", sections="Education")
    output = Output.objects.create(outcome=outcome, code="1.1", title="Access to learning")
    indicator = Indicator.objects.create(output=output, code="1.1.1", title="Children enrolled", target=30000)
    CPDLink.objects.create(indicator=indicator, kind="etools", year=2026, pd=pd, label=NUMBER, confirmed=True)
    note = Document.objects.create(
        title="Field visit minutes", text="CARE staff met families.", status=Document.Status.READY
    )
    KnowledgeLink.objects.create(
        document=note, kind="partner", object_id=care.pk, label="CARE", origin="auto"
    )
    Resource.objects.create(
        title="Out-of-school study",
        publication_year="2025",
        section="Education",
        description="Why children leave.",
    )
    Map.objects.create(name="Schools map", status="Completed", link="https://example.org/map")
    review = DailyReview.objects.create(date=datetime.date(2026, 9, 30), status="succeeded")
    ReviewFinding.objects.create(
        review=review,
        key="pd_ending:LEB/PCA2026005/PD2026012",
        check_id="pd_ending",
        severity="warning",
        section="Education",
        title="A programme document ends soon",
    )
    ReviewFinding.objects.create(
        review=review, key="ap_overdue:CARE", check_id="ap_overdue", severity="critical", title="Overdue"
    )
    CenterSummary.objects.create(
        center_id=7,
        center_name="Center A",
        partner_name="AMEL",
        governorate="Akkar",
        month=datetime.date(2026, 9, 1),
        figures={"children": 120, "children_with_open_flag": 4},
    )
    return SimpleNamespace(
        amel=amel, care=care, pd=pd, database=database, master=master, education=education, output=output,
        indicator=indicator, note=note, cycle=cycle,
    )  # fmt: skip
