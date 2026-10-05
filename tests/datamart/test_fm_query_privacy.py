"""Ask's generic eTools tools send field monitoring records without people and without long texts (A8):
person keys dropped and never queried, texts over 80 characters withheld, the shorter ones redacted,
and a search for a person's name finds no field monitoring record. Other datasets are unchanged."""

import datetime
import json

import pytest

from neurodb.assistant import tools
from neurodb.datamart import catalogue, query
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

PD_NUMBER = "LEB/PCA2023597/PD2025123"
CANARIES = (
    "Rania Canary",
    "Karim Canary",
    "karim.canary@example.org",
    "+961 3 123 456",
    "03-123456",
    "https://evil.example/x",
    "evil.example",
    "Mrs Layla Saab",
)
PERSON_KEYS = ("visit_lead", "team_members", "person_responsible", "focal_point", "monitors")
FM = sorted(catalogue.FM_PRIVATE)
LONG = (
    "The visit with Karim Canary (karim.canary@example.org, +961 3 123 456) found that Mrs Layla Saab "
    "runs the centre; see https://evil.example/x for the photos. Attendance was 412 children."
)


@pytest.fixture
def records(db):
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="CSO", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", partner=partner, number=PD_NUMBER, title="Education")
    record = {
        "id": 7,
        "vendor_number": "V1",
        "entity": PD_NUMBER,
        "entity_type": "PD/SSFA",
        "monitoring_activity": "FM-2026-022",
        "monitoring_activity_id": 1722,
        "overall_finding_rating": "On Track",
        "narrative_finding": LONG,
        "monitoring_activity_end_date": "2026-05-11",
        "location": {"name": "Halba", "p_code": "LB1101"},
        "site": "Halba public school",
        "visit_lead": "Rania Canary",
        "team_members": [{"name": "Karim Canary", "email": "karim.canary@example.org"}],
        "person_responsible": {"name": "Rania Canary"},
        "field_office": "Tripoli",
    }
    dm.MonitoringFinding.objects.create(
        datamart_id=7,
        partner=partner,
        intervention=pd,
        entity=PD_NUMBER,
        entity_type="PD/SSFA",
        monitoring_activity="FM-2026-022",
        monitoring_activity_id=1722,
        overall_finding_rating="On Track",
        narrative_finding=LONG,
        end_date=datetime.date(2026, 5, 11),
        visit_lead="Rania Canary",
        data=record,
    )
    base = {"monitoring_activity_id": 1722, "monitoring_activity": "FM-2026-022", "vendor_number": "V1"}
    answers = [
        {"id": 1, "question_id": 12, "answer": "On Track", "summary": "", "entity": PD_NUMBER},
        {"id": 2, "question_id": 15, "answer": "Yes", "summary": "Call Rania Canary on 03-123456"},
        {"id": 3, "question_id": 16, "answer": "See https://evil.example/x", "summary": LONG},
        {"id": 4, "question_id": 17, "answer": "Mail karim.canary@example.org", "monitors": ["Karim Canary"]},
        {"id": 5, "question_id": 18, "answer": "Reached +961 3 123 456", "question_text": "Contact?"},
    ]
    for answer in answers:
        dm.DatamartDocument.objects.create(
            dataset="fm_questions",
            record_key=str(answer["id"]),
            partner=partner,
            title="FM-2026-022",
            date=datetime.date(2026, 5, 11),
            data={**base, **answer},
        )
    dm.DatamartDocument.objects.create(
        dataset="fm_options",
        record_key="70001",
        title="On track",
        data={"id": 70001, "question_id": 12, "value": "1", "label": "On track"},
    )
    dm.DatamartDocument.objects.create(
        dataset="fm_programme_activities",
        record_key="80001",
        title="FM-2026-022 · BLN classes",
        date=datetime.date(2026, 5, 11),
        data={
            **base,
            "id": 80001,
            "programme_activity": "BLN classes",
            "intervention_number": PD_NUMBER,
            "focal_point": "Karim Canary",
        },
    )
    # a dataset outside field monitoring, with a name in a value: unchanged by A8
    dm.DatamartDocument.objects.create(
        dataset="intervention_epd",
        record_key="1",
        partner=partner,
        intervention=pd,
        title=f"{PD_NUMBER} · ePD",
        date=datetime.date(2026, 1, 1),
        data={"pd_number": PD_NUMBER, "context": "Prepared with Rania Canary. " + "x" * 300, "id": 1},
    )
    dm.ActionPoint.objects.create(
        datamart_id=1,
        partner=partner,
        intervention=pd,
        status="open",
        assigned_to_name="Rania Canary",
        related_module="fm",
        data={"id": 1, "assigned_to_name": "Rania Canary", "description": "Repair the tank"},
    )
    return {"partner": partner, "pd": pd}


def strings(value, key=""):
    """Every (key, text) of a tool result, nested ones included."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v, key)
    elif isinstance(value, str):
        yield key, value


def assert_clean(result):
    text = json.dumps(result, ensure_ascii=False)
    for canary in CANARIES:
        assert canary not in text, canary
    for key in PERSON_KEYS:
        assert f'"{key}"' not in text, key


def assert_short(rows):
    """The record values: none over 80 characters (the long ones are withheld)."""
    for key, text in strings(rows):
        if key in ("url", "name", "number"):  # NeuroDB's own links to the partner and PD pages
            continue
        assert len(text) <= catalogue.FM_TEXT_MAX or text == catalogue.FM_TEXT_WITHHELD, (key, text)


@pytest.mark.parametrize("dataset", FM)
def test_records_rows_and_examples_carry_no_person_and_no_long_text(records, dataset):
    rows = tools.run("etools_query", {"dataset": dataset, "limit": 50})
    assert rows["matching_records"] >= 1
    assert_clean(rows)
    assert_short(rows["rows"])
    for row in rows["rows"]:
        record = tools.run("etools_record", {"dataset": dataset, "record": row["record"]})
        assert_clean(record)
        assert_short(record["data"])
    described = tools.run("etools_datasets", {"dataset": dataset})
    assert_clean(described)
    assert not [k for k in described["fields_with_example_values"] if catalogue.person_key(k)]
    assert_short(described["fields_with_example_values"])


def test_long_texts_are_withheld_and_short_ones_redacted(records):
    finding = tools.run("etools_record", {"dataset": "field_monitoring", "record": "7"})["data"]
    assert finding["narrative_finding"] == catalogue.FM_TEXT_WITHHELD
    assert finding["overall_finding_rating"] == "On Track"  # short values pass unchanged
    assert finding["entity"] == PD_NUMBER
    assert finding["location"] == {"name": "Halba", "p_code": "LB1101"}
    assert finding["monitoring_activity_end_date"] == "2026-05-11"
    assert finding["monitoring_activity_id"] == 1722
    answers = {
        r["question_id"]: r
        for r in tools.run("etools_query", {"dataset": "fm_questions", "limit": 50})["rows"]
    }
    assert answers[12]["answer"] == "On Track"
    assert answers[15]["answer"] == "Yes"
    assert answers[15]["summary"] == "Call [name withheld] on [phone withheld]"
    assert answers[16]["answer"] == "See [link withheld]"
    assert answers[16]["summary"] == catalogue.FM_TEXT_WITHHELD
    assert answers[17]["answer"] == "Mail [email withheld]"
    assert answers[18]["answer"] == "Reached [phone withheld]"
    assert answers[12]["entity"] == PD_NUMBER


def test_groups_and_sums_carry_no_person(records):
    grouped = tools.run("etools_query", {"dataset": "fm_questions", "group_by": "summary", "sum": "order"})
    assert_clean(grouped)
    labels = {g["summary"] for g in grouped["groups"]}
    assert catalogue.FM_TEXT_WITHHELD in labels and "Call [name withheld] on [phone withheld]" in labels
    by_answer = tools.run("etools_query", {"dataset": "fm_questions", "group_by": "answer"})
    assert "Yes" in {g["answer"] for g in by_answer["groups"]}
    assert_clean(by_answer)
    ratings = tools.run("etools_query", {"dataset": "field_monitoring", "group_by": "overall_finding_rating"})
    assert ratings["groups"] == [{"overall_finding_rating": "On Track", "records": 1}]
    summed = tools.run("etools_query", {"dataset": "fm_questions", "sum": "question_id"})
    assert summed["sum"] == {"question_id": 78.0}
    assert_clean(summed)
    by_pd = tools.run("etools_query", {"dataset": "field_monitoring", "group_by": "programme_document"})
    assert by_pd["groups"] == [{"programme_document": PD_NUMBER, "records": 1}]


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"filters": {"visit_lead": "Rania Canary"}}, "visit_lead"),
        ({"filters": {"visit_lead__contains": "Rania"}}, "visit_lead"),
        ({"filters": {"team_members__isnull": False}}, "team_members"),
        ({"group_by": "visit_lead"}, "visit_lead"),
        ({"group_by": "person_responsible"}, "person_responsible"),
        ({"sum": "team_members"}, "team_members"),
        ({"fields": ["entity", "visit_lead"]}, "visit_lead"),
        ({"order_by": "-visit_lead"}, "visit_lead"),
    ],
)
def test_person_keys_cannot_be_queried(records, args, field):
    with pytest.raises(tools.ToolInputError) as raised:
        tools.run("etools_query", {"dataset": "field_monitoring", **args})
    assert str(raised.value) == (
        f"'{field}' holds personal data and cannot be queried; use the Monitoring insights page."
    )


def test_a_person_cannot_be_looked_for_in_field_monitoring(records):
    with pytest.raises(tools.ToolInputError, match="cannot be searched for a person"):
        tools.run("etools_query", {"dataset": "fm_questions", "search": "Rania Canary"})
    with pytest.raises(tools.ToolInputError, match="cannot be searched for a person"):
        tools.run(
            "etools_query", {"dataset": "fm_questions", "filters": {"summary__contains": "Rania Canary"}}
        )
    # the same search on another dataset is answered as before
    other = tools.run("etools_query", {"dataset": "intervention_epd", "search": "Rania Canary"})
    assert other["matching_records"] == 1


def test_search_shows_field_monitoring_by_reference_and_finds_no_person(records):
    found = tools.run("etools_search", {"text": "Canary"})
    by_dataset = {h["dataset"]: h for h in found["datasets"]}
    assert by_dataset["field_monitoring"]["examples"] == ["FM-2026-022"]
    assert set(by_dataset["fm_questions"]["examples"]) == {"FM-2026-022"}
    assert by_dataset["fm_programme_activities"]["examples"] == ["FM-2026-022"]
    assert_clean({k: v for k, v in by_dataset.items() if k in catalogue.FM_PRIVATE})
    named = tools.run("etools_search", {"text": "Rania Canary"})
    datasets = {h["dataset"] for h in named["datasets"]}
    assert not datasets & catalogue.FM_PRIVATE
    assert {"intervention_epd", "action_points"} <= datasets  # other datasets are searched as before
    email = tools.run("etools_search", {"text": "karim.canary@example.org"})
    assert not {h["dataset"] for h in email["datasets"]} & catalogue.FM_PRIVATE


def test_person_key_is_token_based():
    for key in (
        "visit_lead",
        "team_members",
        "person_responsible",
        "teamMembers",
        "monitors",
        "focal_point",
        "assigned_to",
        "reviewed_by",
        "team.name",
        "data.visit_lead",
        "user_id",
        "contact_email",
    ):
        assert catalogue.person_key(key), key
    for key in (
        "monitoring_activity",
        "monitoring_activity_id",
        "is_remote_monitoring",
        "entity",
        "overall_finding_rating",
        "narrative_finding",
        "field_office",
        "sections",
        "location",
    ):
        assert not catalogue.person_key(key), key


def test_other_datasets_are_unchanged(records, monkeypatch):
    """Every non field monitoring output is byte-identical to the output without A8."""

    def outputs():
        out = {}
        for name in ("intervention_epd", "action_points"):
            out[name] = [
                tools.run("etools_query", {"dataset": name}),
                tools.run("etools_query", {"dataset": name, "group_by": "partner"}),
                tools.run("etools_query", {"dataset": name, "fields": ["id"]}),
                tools.run("etools_datasets", {"dataset": name}),
                tools.run("etools_record", {"dataset": name, "record": "1"}),
                query.public({"assigned_to_name": "Rania Canary", "x": "y" * 500}, dataset=name),
            ]
        searched = tools.run("etools_search", {"text": "Rania Canary"})["datasets"]
        out["search"] = [h for h in searched if h["dataset"] not in FM]
        return json.dumps(out, sort_keys=True, default=str)

    with_a8 = outputs()
    monkeypatch.setattr(catalogue, "FM_PRIVATE", frozenset())
    assert outputs() == with_a8
    assert "Rania Canary" in with_a8  # names in other datasets' records are still sent, as documented
