"""Education figures from Compiler (Makani, Bridging): reading blocks, the sync and the page."""

from itertools import combinations

import pytest
import responses
from django.core.management import CommandError, call_command
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.education.figures import Figures
from neurodb.education.models import EducationFigures
from neurodb.education.sync import sync, wanted_years
from neurodb.youth.compiler import CompilerClient

pytestmark = pytest.mark.django_db

BASE = "https://compiler.test"
CHILDREN = [  # one row per registration: the same child (1) with two partners
    {"person": 1, "partner": 5, "governorate": 20, "district": 21, "sex": "Female", "age_group": "6-9"},
    {"person": 1, "partner": 6, "governorate": 22, "district": None, "sex": "Female", "age_group": "6-9"},
    {"person": 2, "partner": 5, "governorate": 20, "district": 21, "sex": "Male", "age_group": "10-14"},
]


def groupings(main, details, extra=()):
    out = []
    for size in range(len(main) + 1):
        for chosen in combinations(main, size):
            out.append(tuple(sorted(chosen)))
            out += [tuple(sorted(chosen + (d,))) for d in details]
    return list(dict.fromkeys(out + [tuple(sorted(e)) for e in extra]))


def people_block(records, main, details, extra=()):
    figures = []
    for by in groupings(main, details, extra):
        cells = {}
        for r in records:
            cells.setdefault(tuple(r[d] for d in by), set()).add(r["person"])
        figures.append({"by": list(by), "rows": [list(k) + [len(v)] for k, v in cells.items()]})
    return {"measures": ["people"], "figures": figures}


def payload(year="2025"):
    services = [
        dict(CHILDREN[0], service="Education"),
        dict(CHILDREN[2], service="Education"),
        dict(CHILDREN[0], service="Child protection (PSS)"),
    ]
    attendance = {
        "measures": ["days_recorded", "days_attended"],
        "figures": [
            {"by": [], "rows": [[10, 8]]},
            {"by": ["month"], "rows": [["2025-03", 6, 5], ["2025-04", 4, 3]]},
            {"by": ["partner"], "rows": [[5, 10, 8]]},
            {"by": ["month", "partner"], "rows": [["2025-03", 5, 6, 5], ["2025-04", 5, 4, 3]]},
        ],
    }
    return {
        "format": 1,
        "programme": "mscc",
        "label": "Makani (MSCC)",
        "year": year,
        "counted_at": "2025-09-29T02:31:00+03:00",
        "rounds": [{"id": 1, "name": "Round 1 2025"}],
        "partners": [
            {"id": 5, "name": "Makani Partner", "short_name": "MP"},
            {"id": 6, "name": "Other", "short_name": ""},
        ],
        "locations": [{"id": 20, "name": "Akkar"}, {"id": 21, "name": "Halba"}, {"id": 22, "name": "Beirut"}],
        "sites": [],
        "nationalities": [],
        "disabilities": [],
        "blocks": {
            "registrations": people_block(
                CHILDREN,
                ("partner", "governorate"),
                ("district", "sex", "age_group"),
                (("sex", "age_group"),),
            ),
            "services": people_block(services, ("service", "partner", "governorate"), ("sex", "age_group")),
            "sites": {"measures": ["people"], "figures": [{"by": [], "rows": [[2]]}]},
            "staff": {
                "measures": ["people"],
                "figures": [{"by": [], "rows": [[3]]}, {"by": ["sex"], "rows": [["Female", 3]]}],
            },
            "attendance": attendance,
        },
    }


class FakeClient:
    def __init__(self, ready=True):
        self.ready = ready

    def education_index(self):
        return [
            {
                "programme": "mscc",
                "label": "Makani (MSCC)",
                "current_year": "2025",
                "years": [{"year": "2025", "counted": self.ready}, {"year": "2024", "counted": False}],
            }
        ]

    def education(self, programme, year):
        return payload(year) if self.ready else None


def test_a_block_answers_from_the_grouping_that_matches():
    block = Figures(payload()).block("registrations")
    assert block.total({}) == {"people": 2}  # child 1 once, though with two partners
    assert block.table({}, "partner") == {(5,): {"people": 2}, (6,): {"people": 1}}
    assert block.table({"governorate": 20}, "sex") == {("Female",): {"people": 1}, ("Male",): {"people": 1}}
    assert block.table({"partner": 5}, "district", "sex") is None  # two details: not sent


def test_wanted_years_are_the_current_one_and_the_counted_ones():
    entry = {
        "current_year": "2026",
        "years": [
            {"year": "2026", "counted": False},
            {"year": "2025", "counted": True},
            {"year": "2024", "counted": True},
            {"year": "2023", "counted": True},
        ],
    }
    assert wanted_years(entry) == ["2026", "2025", "2024"]


def test_sync_stores_counted_years_and_waits_for_the_others():
    run = sync(triggered_by="test", client=FakeClient())
    assert run.status == SyncRun.Status.SUCCEEDED
    assert list(EducationFigures.objects.values_list("programme", "year")) == [("mscc", "2025")]
    assert EducationFigures.objects.get().counted_at is not None

    EducationFigures.objects.all().delete()
    run = sync(triggered_by="test", client=FakeClient(ready=False))
    assert run.status == SyncRun.Status.SUCCEEDED and run.details["pending"] == ["mscc 2025"]
    assert not EducationFigures.objects.exists()


def test_page_shows_children_services_attendance(client_viewer):
    sync(triggered_by="test", client=FakeClient())
    response = client_viewer.get(reverse("education:dashboard"))
    assert response.status_code == 200
    data = response.context["data"]
    assert (data["children"], data["female"], data["male"], data["partners"]) == (2, 1, 1, 2)
    assert data["sites"] == 2 and data["staff"] == 3 and data["attendance_rate"] == 80.0
    assert [(m["month"], m["rate"]) for m in data["months"]] == [("2025-03", 83.3), ("2025-04", 75.0)]
    education = next(s for s in data["services"] if s["label"] == "Education")
    assert (education["people"], education["female"], education["male"]) == (2, 1, 1)
    assert data["matrix"][0] == {"age_group": "6-9", "female": 1, "male": None, "other": None}
    assert b"Education programmes" in response.content

    filtered = client_viewer.get(reverse("education:dashboard"), {"partner": "6"}).context["data"]
    assert filtered["children"] == 1 and filtered["male"] == 0
    by_place = client_viewer.get(reverse("education:dashboard"), {"governorate": "20"}).context["data"]
    assert by_place["children"] == 2
    assert "partner" in [b["key"] for b in by_place["breakdowns"]]


def test_page_without_figures_says_how_to_connect(client_viewer):
    response = client_viewer.get(reverse("education:dashboard"))
    assert response.status_code == 200 and b"Compiler is not connected" in response.content


def test_command_refuses_to_run_unconfigured(settings):
    settings.COMPILER_API_URL = ""
    with pytest.raises(CommandError, match="not configured"):
        call_command("sync_compiler_education")


@responses.activate
def test_client_reads_the_index_and_treats_202_as_not_ready():
    responses.get(BASE + "/api/figures/", json={"programmes": FakeClient().education_index()})
    responses.get(
        BASE + "/api/figures/mscc/",
        status=202,
        json={"detail": "being counted"},
        match=[responses.matchers.query_param_matcher({"year": "2026"})],
    )
    responses.get(
        BASE + "/api/figures/mscc/",
        json=payload(),
        match=[responses.matchers.query_param_matcher({"year": "2025"})],
    )
    client = CompilerClient(BASE, "abc123")
    assert client.education_index()[0]["programme"] == "mscc"
    assert client.education("mscc", "2026") is None
    assert client.education("mscc", "2025")["year"] == "2025"
    assert responses.calls[0].request.headers["Authorization"] == "Token abc123"
