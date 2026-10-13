"""Ask NeuroDB's ``hact_visit_plan`` tool: the HACT programmatic visit plan per programme document, counts
only. It returns the Field monitoring page's figures (``hact_plan.headline`` over ``hact_plan.build``) by
remark and by section, the year, the sync they are as of and the page's link; never a partner name, a PD
reference, a section comment or a focal point. The comments are no Datamart dataset, so no eTools tool
reads them either.

Synthetic data only: "Partner A", references like TEST/PCA2026001/PD2026001, focal point "Focal Person
Test"."""

import datetime
import itertools
import json

import pytest
from django.utils import timezone

from neurodb.assistant import agent, tools
from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, hact_plan
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

D = datetime.date
YEAR = 2026
TODAY = D(2026, 10, 13)
DEC_31 = D(2026, 12, 31)
FOCAL = "Focal Person Test"
COMMENT = "Quarterly visit booked with the Zanzibarite team"  # a word found nowhere else
_ids = itertools.count(1)

# Every key the tool may return: fixed names, never a partner, a PD or a person
KEYS = {
    "year",
    "pds",
    "pds_with_plan",
    "not_visited",
    "partners",
    "partners_no_visit",
    "ended",
    "still_needed",
    "visits",
    "visits_ended",
    "pds_ended",
    "completed",
    "counted",
    "required",
    "awaiting",
    "etools_behind",
    "with_row",
    "available_years",
    "sections",
    "by_remark",
    "remark",
    "label",
    "by_section",
    "section",
    "as_of",
    "url",
}


def partner(name: str) -> PartnerOrganization:
    n = next(_ids)
    return PartnerOrganization.objects.create(
        etl_id=f"p{n}", name=name, partner_type="Civil Society Organization", vendor_number=f"V{n:05d}"
    )


def programme(owner, *, sections=("Education",), status="active", end=DEC_31) -> PCA:
    n = next(_ids)
    return PCA.objects.create(
        etl_id=f"pd{n}",
        partner=owner,
        partner_name=owner.name,
        number=f"TEST/PCA2026{n:03d}/PD2026{n:03d}",
        title=f"Test programme {n}",
        status=status,
        start=D(2026, 1, 1),
        end=end,
        section_names=list(sections),
        unicef_focal_points=[FOCAL, "focal.person@example.org"],
    )


def plan(pd, quarters):
    q1, q2, q3, q4 = quarters
    dm.PlannedVisits.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner_id=pd.partner_id,
        pd_reference_number=pd.number,
        year=YEAR,
        q1=q1,
        q2=q2,
        q3=q3,
        q4=q4,
    )


def fm_visit(pd, status="completed"):
    activity = next(_ids)
    dm.MonitoringFinding.objects.create(
        datamart_id=next(_ids),
        partner=pd.partner,
        vendor_number=pd.partner.vendor_number,
        entity=pd.number,
        entity_type="PD/SSFA",
        intervention=pd,
        monitoring_activity=f"FM-TEST-{activity}",
        monitoring_activity_id=activity,
        status=status,
        is_programmatic_visit=True,
        start_date=D(2026, 5, 9),
        end_date=D(2026, 5, 10),
    )


def tpm_visit(pd):
    dm.TPMActivity.objects.create(
        datamart_id=next(_ids),
        partner=pd.partner,
        intervention=pd,
        visit_reference_number=f"TPM/2026/{next(_ids)}",
        visit_status="unicef_approved",
        is_programmatic_visit=True,
        pd_reference_number=pd.number,
        date=D(2026, 6, 1),
    )


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """The tool's "today" (the current year and the ended PDs): 13 October 2026."""
    real = timezone.localdate
    monkeypatch.setattr(
        timezone, "localdate", lambda *args, **kwargs: real(*args, **kwargs) if args or kwargs else TODAY
    )


@pytest.fixture
def world(admin_user):
    """Two partners, five PDs of 2026:

    * Partner A: an Education PD planned 4, visited once (FM; a second report awaiting approval), and a
      PD of Education and Health planned 1, never visited (it carries the section comment);
    * Partner B: a Health PD planned 1 and visited (TPM), a Health PD with no plan, and an Education PD
      that ended in March, planned 2 and never visited.

    Partner A's eTools HACT year is behind its minimum. Every PD has a focal point."""
    a, b = partner("Partner A"), partner("Partner B")
    short, unvisited = programme(a), programme(a, sections=("Education", "Health"))
    met, no_plan = programme(b, sections=("Health",)), programme(b, sections=("Health",))
    ended = programme(b, status="ended", end=D(2026, 3, 31))
    plan(short, (1, 1, 1, 1))
    plan(unvisited, (1, 0, 0, 0))
    plan(met, (0, 1, 0, 0))
    plan(ended, (1, 1, 0, 0))
    fm_visit(short)
    fm_visit(short, status="submitted")
    tpm_visit(met)
    dm.PartnerHACTYear.objects.create(
        datamart_id=next(_ids),
        partner=a,
        partner_name=a.name,
        vendor_number=a.vendor_number,
        year=YEAR,
        risk_rating="Medium",
        pv_required=3,
        pv_completed=1,
    )
    hact_plan.set_comment(unvisited, YEAR, COMMENT, admin_user)
    at = timezone.make_aware(datetime.datetime(2026, 10, 12, 21))
    for target in hact_plan.SYNC_TARGETS:
        SyncRun.objects.create(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=target,
            status=SyncRun.Status.SUCCEEDED,
            started_at=at,
            finished_at=at,
        )
    return {"partners": [a, b], "pds": [short, unvisited, met, no_plan, ended], "user": admin_user}


# ---------------------------------------------------------------------------------- registration
def test_the_tool_is_registered_next_to_assurance_overview_and_named_in_the_instructions():
    names = list(tools.TOOLS)
    assert names.index("hact_visit_plan") == names.index("assurance_overview") + 1
    (definition,) = tools.definitions(["hact_visit_plan"])
    assert set(definition["parameters"]["properties"]) == {"year", "section"}
    assert definition["parameters"]["required"] == []
    assert "counts only" in definition["description"]
    assert tools.label("hact_visit_plan") == "Reading the HACT visit plan"
    assert "hact_visit_plan" in agent.SYSTEM_PROMPT
    with pytest.raises(tools.ToolInputError, match="Unknown argument 'partner'"):
        tools.run("hact_visit_plan", {"partner": "Partner A"})
    with pytest.raises(tools.ToolInputError, match="must be a integer"):
        tools.run("hact_visit_plan", {"year": "2026"})


def test_assurance_overview_links_to_the_plan(world):
    assert tools.run("assurance_overview", {})["urls"]["hact_visit_plan"] == "/field-monitoring/?tab=plan"
    urls = tools.run("assurance_overview", {"year": 2025})["urls"]
    assert urls["hact_visit_plan"] == "/field-monitoring/?tab=plan&year=2025"


# ---------------------------------------------------------------------------------- the figures
def test_the_figures_are_the_pages(world):
    result = tools.run("hact_visit_plan", {})
    rows = hact_plan.build(YEAR, TODAY)
    page = hact_plan.headline(rows, YEAR)
    for key in ("year", "pds", "pds_with_plan", "not_visited", "still_needed", "completed", "etools_behind"):
        assert result[key] == page[key], key
    assert result["year"] == YEAR and result["available_years"] == [YEAR]
    assert result["not_visited"] == {"pds": 2, "partners": 2, "partners_no_visit": 0, "ended": 1}
    assert result["still_needed"] == {"visits": 6, "pds": 3, "partners": 2, "visits_ended": 2, "pds_ended": 1}
    assert result["completed"] == {"counted": 2, "required": 8, "completed": 2, "awaiting": 1}
    assert result["etools_behind"] == {"partners": 1, "with_row": 1}
    assert result["by_remark"] == [
        {"remark": "needed", "label": "HACT FMM needed", "pds": 2},
        {"remark": "ended", "label": "PD ended, visits missing", "pds": 1},
        {"remark": "completed", "label": "Completed", "pds": 1},
        {"remark": "no_plan", "label": "No visit planned", "pds": 1},
    ]
    assert {code: n for code, n in ((r["remark"], r["pds"]) for r in result["by_remark"])} == page[
        "by_remark"
    ]
    # a PD with two sections counts under each; the total is the headline
    assert result["by_section"] == [
        {"section": "Education", "pds": 3, "pds_with_plan": 3, "not_visited": 2, "still_needed": 6},
        {"section": "Health", "pds": 3, "pds_with_plan": 2, "not_visited": 1, "still_needed": 1},
    ]
    assert result["by_section"] == [
        {k: v for k, v in r.items() if k != "total"} for r in hact_plan.by_section(rows) if not r["total"]
    ]
    assert result["sections"] == []
    assert result["as_of"] == "2026-10-12"
    assert result["url"] == "/field-monitoring/?tab=plan&year=2026"


def test_a_section_narrows_the_figures_as_the_pages_filter_does(world):
    result = tools.run("hact_visit_plan", {"year": YEAR, "section": "heal"})
    every = hact_plan.build(YEAR, TODAY)
    rows = hact_plan.select(every, hact_plan.PlanFilters(year=YEAR, sections=("Health",)))
    assert result["sections"] == ["Health"]
    assert result["pds"] == 3 == len(rows)
    assert result["not_visited"] == hact_plan.headline(rows, YEAR, every)["not_visited"]
    # Partner A's Health PD is not visited, but its Education PD was: a visit on some PD of the year
    assert result["not_visited"] == {"pds": 1, "partners": 1, "partners_no_visit": 0, "ended": 0}
    assert result["still_needed"]["visits"] == 1
    # only the section asked about: Education's line would hold just the PD both share
    assert result["by_section"] == [
        r for r in tools.run("hact_visit_plan", {})["by_section"] if r["section"] == "Health"
    ]
    assert result["url"] == "/field-monitoring/?tab=plan&year=2026&section=Health"
    with pytest.raises(tools.ToolInputError, match="No programme document of 2026 has a section matching"):
        tools.run("hact_visit_plan", {"section": "Nutrition"})


def test_the_year_defaults_to_this_year_and_a_pd_with_no_section_counts_under_no_section(world):
    PCA.objects.filter(pk=world["pds"][3].pk).update(section_names=[])
    assert tools.run("hact_visit_plan", {"year": 1900})["year"] == YEAR  # out of range: this year
    other = tools.run("hact_visit_plan", {"year": 2027})
    assert (other["year"], other["pds"], other["pds_with_plan"]) == (2027, 0, 0)
    assert other["url"] == "/field-monitoring/?tab=plan&year=2027"
    sections = tools.run("hact_visit_plan", {})["by_section"]
    assert sections[-1] == {
        "section": "No section",
        "pds": 1,
        "pds_with_plan": 0,
        "not_visited": 0,
        "still_needed": 0,
    }


def test_it_runs_read_only(world):
    with tools.read_only():
        assert tools.run("hact_visit_plan", {})["pds"] == 5


# ---------------------------------------------------------------------------------- privacy
def test_the_result_holds_only_counts_section_names_and_labels(world, monkeypatch):
    def never(*_args, **_kwargs):
        raise AssertionError("the tool read names or comments")

    for name in ("focal_names", "comments", "for_page"):  # the page's readers of names and comments
        monkeypatch.setattr(hact_plan, name, never)
    results = [tools.run("hact_visit_plan", {}), tools.run("hact_visit_plan", {"section": "Education"})]

    allowed_texts = (
        {"Education", "Health", "No section", "2026-10-12"}
        | set(hact_plan.REMARKS)
        | {str(label) for label in hact_plan.REMARKS.values()}
        | {r["url"] for r in results}
    )

    def walk(value, where="result"):
        if isinstance(value, dict):
            for key, inner in value.items():
                assert key in KEYS, f"{where}: unexpected key {key!r}"
                walk(inner, f"{where}.{key}")
        elif isinstance(value, list):
            for inner in value:
                walk(inner, where)
        elif isinstance(value, str):
            assert value in allowed_texts, f"{where}: unexpected text {value!r}"
        else:
            assert value is None or isinstance(value, bool | int), f"{where}: {value!r}"

    for result in results:
        walk(result)
        text = json.dumps(result)
        for partner in world["partners"]:
            assert partner.name not in text and partner.vendor_number not in text
        for pd in world["pds"]:
            assert pd.number not in text and pd.title not in text and str(pd.number).split("/")[1] not in text
        assert FOCAL not in text and "Focal Person" not in text and "example.org" not in text
        assert COMMENT not in text and "Zanzibarite" not in text
        assert world["user"].username not in text


def test_the_comments_are_never_an_etools_dataset(world):
    models = {model for model, *_rest in catalogue.TYPED.values()}
    assert "VisitPlanComment" not in models
    assert not {"visit_plan_comment", "visit_plan_comments"} & set(catalogue.dataset_names())
    listing = json.dumps(tools.run("etools_datasets", {}))
    assert "visit_plan" not in listing and "VisitPlanComment" not in listing
    with pytest.raises(tools.ToolInputError):
        tools.run("etools_query", {"dataset": "visit_plan_comment"})
    assert tools.run("etools_search", {"text": "Zanzibarite"})["datasets"] == []
