"""Youth figures from Compiler: reading the groupings, the sync, eTools links and the dashboard."""

import datetime
from decimal import Decimal
from itertools import combinations

import pytest
import responses
from django.core.management import CommandError, call_command
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.youth import linking
from neurodb.youth.compiler import CompilerClient
from neurodb.youth.figures import DETAILS, MAIN, Figures
from neurodb.youth.models import YouthFigures, YouthIndicatorLink
from neurodb.youth.sync import sync

pytestmark = pytest.mark.django_db

BASE = "https://compiler.test"

# What Compiler would count: one entry per enrolment (youth id and its dimensions).
ENROLMENTS = [
    {
        "youth": 1,
        "master": 10,
        "sub": 101,
        "partner": 5,
        "donor": 7,
        "program_document": 3,
        "governorate": 20,
        "district": 21,
        "sex": "Female",
        "age_group": "18-24",
        "nationality": 30,
    },
    {
        "youth": 1,
        "master": 10,
        "sub": 102,
        "partner": 5,
        "donor": 8,
        "program_document": 3,
        "governorate": 20,
        "district": 21,
        "sex": "Female",
        "age_group": "18-24",
        "nationality": 30,
    },
    {
        "youth": 2,
        "master": 10,
        "sub": 101,
        "partner": 5,
        "donor": 7,
        "program_document": 3,
        "governorate": 22,
        "district": None,
        "sex": "Male",
        "age_group": "15-17",
        "nationality": 30,
    },
    {
        "youth": 3,
        "master": 10,
        "sub": 102,
        "partner": 6,
        "donor": 8,
        "program_document": None,
        "governorate": 22,
        "district": None,
        "sex": "Female",
        "age_group": "18-24",
        "nationality": 30,
    },
]


def payload(year="2026"):
    groupings = []
    for size in range(len(MAIN) + 1):
        for main in combinations(MAIN, size):
            groupings.append(tuple(sorted(main)))
            groupings += [tuple(sorted(main + (d,))) for d in DETAILS]
    groupings.append(("program_document", "sub"))
    figures = []
    for by in groupings:
        cells = {}
        for e in ENROLMENTS:
            cells.setdefault(tuple(e[d] for d in by), set()).add(e["youth"])
        figures.append({"by": list(by), "rows": [list(k) + [len(v)] for k, v in cells.items()]})
    return {
        "format": 1,
        "year": year,
        "generated_at": "2026-09-29T10:00:00+03:00",
        "indicators": [
            {"id": 10, "level": "master", "number": "1", "name": "Skills", "master": None, "active": True},
            {
                "id": 101,
                "level": "sub",
                "number": "1.1",
                "name": "Digital skills",
                "master": 10,
                "active": None,
            },
            {"id": 102, "level": "sub", "number": "1.2", "name": "Life skills", "master": 10, "active": None},
        ],
        "partners": [
            {"id": 5, "name": "Youth Partner", "short_name": "YP"},
            {"id": 6, "name": "Other", "short_name": ""},
        ],
        "donors": [{"id": 7, "name": "EU"}, {"id": 8, "name": "Japan"}],
        "program_documents": [
            {
                "id": 3,
                "project_code": "LEB/PCA2026005",
                "project_name": "Youth skills",
                "partner": 5,
                "year": year,
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "donors": [7, 8],
                "governorates": [20],
            }
        ],
        "targets": [{"program_document": 3, "master": 10, "sub": 101, "baseline": None, "target": 100}],
        "locations": [
            {"id": 20, "name": "Akkar", "p_code": "LB1", "parent": None, "type": "Governorate"},
            {"id": 21, "name": "Halba", "p_code": "LB11", "parent": 20, "type": "District"},
            {"id": 22, "name": "Beirut", "p_code": "LB6", "parent": None, "type": "Governorate"},
        ],
        "nationalities": [{"id": 30, "name": "Syrian"}],
        "figures": figures,
    }


@pytest.fixture
def etools(db):
    partner = PartnerOrganization.objects.create(etl_id="1", name="Youth Partner", partner_type="CSO")
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA2026005-1",
        title="Youth",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    for n, title in enumerate(
        ["# of young people (15-24) trained in digital skills", "# of schools rehabilitated"]
    ):
        dm.PDIndicator.objects.create(
            datamart_id=n + 1,
            source_id=500 + n,
            intervention=pd,
            pd_reference_number=pd.number,
            title=title,
            section_name="Adolescents",
            lower_result_name="1.1 Skills",
            target_numerator=Decimal("100"),
            display_type="number",
            location_name="Akkar",
        )
    dm.ReportedIndicator.objects.create(
        datamart_id=77,
        partner=partner,
        intervention=pd,
        partner_name=partner.name,
        pd_reference_number=pd.number,
        progress_report="PR-1",
        report_number="QPR1",
        report_type="QPR",
        report_status="Accepted",
        period_start=datetime.date(2026, 4, 1),
        period_end=datetime.date(2026, 6, 30),
        indicator="# of young people (15-24) trained in digital skills",
        target="100",
        location="Akkar",
        achievement_in_period="40",
        total_cumulative_progress="60",
        total_cumulative_progress_in_location="60",
    )
    return pd


class FakeClient:
    def __init__(self, years):
        self.years = years
        self.asked = []

    def figures(self, year=None):
        self.asked.append(year)
        return payload(year or self.years[0]) if (year or self.years[0]) in self.years else None


def test_a_question_reads_the_one_grouping_that_answers_it():
    figures = Figures(payload())
    assert figures.count({}) == 3
    assert figures.table({}, "sub") == {(101,): 2, (102,): 2}  # youth 1 in both: never added up
    assert figures.table({"partner": 5}, "sex") == {("Female",): 1, ("Male",): 1}
    assert figures.table({"governorate": 22, "master": 10}, "district") == {(None,): 2}
    assert figures.table({}, "sub", "district") is None  # two details: Compiler sends no such grouping
    assert figures.label("sub", 101) == "1.1 Digital skills"
    assert figures.label("partner", 5) == "YP"


def test_programme_documents_match_on_reference_number_without_amendment(etools):
    assert linking.match_pds({"LEB/PCA2026005"}, 2026) == {"LEB/PCA2026005": etools}
    assert linking.match_pds({"leb/pca2026005 "}, 2026)["leb/pca2026005 "] == etools
    assert linking.match_pds({"LEB/PCA2099999"}, 2026) == {}


def test_sync_keeps_counts_and_suggests_the_matching_etools_indicator(etools):
    client = FakeClient(["2026"])
    run = sync(triggered_by="test", client=client)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert client.asked == [None, "2025"]  # this year, then the year before (not in Compiler: skipped)
    assert list(YouthFigures.objects.values_list("year", flat=True)) == ["2026"]
    link = YouthIndicatorLink.objects.get()
    assert (link.level, link.youth_indicator_id, link.compiler_pd_id) == ("sub", 101, 3)
    assert link.pd == etools and link.etools_key == "500" and link.source == "suggested"
    assert "digital skills" in link.etools_title


def test_confirmed_links_survive_the_next_sync(etools):
    sync(triggered_by="test", client=FakeClient(["2026"]))
    YouthIndicatorLink.objects.update(source=YouthIndicatorLink.Source.CONFIRMED, etools_key="501")
    sync(triggered_by="test", client=FakeClient(["2026"]))
    link = YouthIndicatorLink.objects.get()
    assert link.source == "confirmed" and link.etools_key == "501"


def test_dashboard_shows_counts_targets_and_etools_side_by_side(client_viewer, etools):
    sync(triggered_by="test", client=FakeClient(["2026"]))
    response = client_viewer.get(reverse("youth:dashboard"))
    assert response.status_code == 200
    data = response.context["data"]
    assert data["total"] == 3 and data["female"] == 2 and data["male"] == 1
    skills = data["indicators"][0]
    assert skills["reached"] == 3
    digital = skills["subs"][0]
    assert (digital["reached"], digital["target"], digital["percent"]) == (2, 100, 2.0)
    row = data["links"][0]
    assert row["counted"] == 2 and row["reported"] == 60 and row["difference"] == -58
    assert b"Youth programmes" in response.content

    filtered = client_viewer.get(reverse("youth:dashboard"), {"partner": "6"}).context["data"]
    assert filtered["total"] == 1 and filtered["links"] == []  # the other partner's PD is not linked
    by_place = client_viewer.get(reverse("youth:dashboard"), {"governorate": "22"}).context["data"]
    assert by_place["total"] == 2 and by_place["targets_hidden"]


def test_dashboard_without_figures_says_how_to_connect(client_viewer):
    response = client_viewer.get(reverse("youth:dashboard"))
    assert response.status_code == 200 and response.context["data"] is None
    assert b"Compiler is not connected" in response.content


def test_command_refuses_to_run_unconfigured(settings):
    settings.COMPILER_API_URL = ""
    with pytest.raises(CommandError, match="not configured"):
        call_command("sync_compiler_youth")


@responses.activate
def test_client_sends_the_token_and_treats_an_unknown_year_as_none():
    url = BASE + "/api/youth/indicator-figures/"
    responses.get(url, json=payload(), match=[responses.matchers.query_param_matcher({})])
    responses.get(
        url,
        status=404,
        json={"detail": "No such reporting year"},
        match=[responses.matchers.query_param_matcher({"year": "1999"})],
    )
    client = CompilerClient(BASE, "abc123")
    assert client.figures()["year"] == "2026"
    assert responses.calls[0].request.headers["Authorization"] == "Token abc123"
    assert client.figures("1999") is None
