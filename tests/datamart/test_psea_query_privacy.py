"""PSEA assessment answers and Ask NeuroDB: a comment can describe a case of sexual exploitation or abuse,
so the assistant reads only the standard, its rating, the partner and the date of each answer, and can
never search the records by text nor query them on another key."""

import json

import pytest

from neurodb.assistant import tools
from neurodb.assistant.tools import ToolInputError
from neurodb.datamart import catalogue, query
from neurodb.datamart import models as dm

CASE = "An allegation of sexual abuse by a volunteer was reported in March"


@pytest.fixture
def answers(db):
    for n, (subject, rating) in enumerate(
        (("Reporting", "Yes"), ("Reporting", "No"), ("Policy", "Yes")), start=1
    ):
        dm.DatamartDocument.objects.create(
            dataset="psea_answers",
            record_key=str(n),
            title=f"Partner {n} · {subject}",
            data={
                "id": n,
                "indicator_subject": subject,
                "rating": rating,
                "assessment_partner_name": f"Partner {n}",
                "assessment_vendor_number": f"25000000{n}",
                "assessment_date": "2026-03-01",
                "comments": CASE,
                "evidence": "See the incident file",
            },
        )


def _text(value) -> str:
    return json.dumps(value, default=str)


def test_only_the_standard_rating_partner_and_date_are_read(answers):
    record = tools.etools_record("psea_answers", "1")
    assert (
        set(record["data"]) <= catalogue.SAFEGUARDING_KEYS
        and record["data"]["indicator_subject"] == "Reporting"
    )
    rows = tools.etools_query("psea_answers", limit=10)["rows"]
    assert len(rows) == 3 and "comments" not in _text(rows) and CASE not in _text(rows)
    described = tools.etools_datasets("psea_answers")["fields_with_example_values"]
    assert set(described) <= catalogue.SAFEGUARDING_KEYS and CASE not in _text(described)


def test_ratings_per_standard_can_be_counted(answers):
    groups = tools.etools_query("psea_answers", group_by="rating", filters={"indicator_subject": "Reporting"})
    assert {g["rating"]: g["records"] for g in groups["groups"]} == {"Yes": 1, "No": 1}


@pytest.mark.parametrize(
    "criteria",
    [
        {"search": "abuse"},
        {"fields": ["comments"]},
        {"group_by": "comments"},
        {"order_by": "-evidence"},
        {"filters": {"comments__icontains": "abuse"}},
        {"sum": "comments"},
    ],
)
def test_the_comments_can_never_be_searched_selected_grouped_or_filtered(answers, criteria):
    with pytest.raises(ToolInputError):
        tools.etools_query("psea_answers", **criteria)


def test_a_search_of_every_dataset_never_looks_into_the_answers(answers):
    hits = tools.etools_search("allegation")
    assert "psea_answers" not in {h["dataset"] for h in hits["datasets"]}
    assert query.search_all("Reporting")["datasets"] == [] or all(
        h["dataset"] != "psea_answers" for h in query.search_all("Reporting")["datasets"]
    )
