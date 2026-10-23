"""The privacy sweep of every download and every page Raw eTools data writes, and of the two exports the
standing rule changed: people planted in a record of every source, and in every text column of NeuroDB's own
tables, never reach a cell, a column's name, a source's list or a record's page. Synthetic people only."""

from __future__ import annotations

import csv
import datetime
import io

import pytest
from django.contrib.auth.models import Group
from django.db import models
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.datamart import catalogue, raw, raw_people
from neurodb.datamart import models as dm
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

NAME, EMAIL = "Person One", "person.one@example.org"
PHONES = ("+961 3 123 457", "03 123 457")
UNKNOWN = "Zorba Quintel"
STRANGER = "Test Stranger"  # a name the record holds nowhere else: only the field name rule finds it
# a record naming people every way eTools does; the unknown name only where a person or a field's name is, since
# no rule can know it inside a text
PLANTED = {
    "id": 4242,
    "source_id": 4242,
    "submitted_by": NAME,
    "visit_lead": NAME,
    "team_members": [NAME, UNKNOWN],
    "assigned_to": {"name": NAME, "email": EMAIL},
    "unicef_focal_points_data": [{"first_name": "Person", "last_name": "One", "email": EMAIL}],
    "supervisor_name": UNKNOWN,
    "participants": [UNKNOWN],
    "interviewees": [{"full": UNKNOWN}],
    "pme": UNKNOWN,
    "beneficiary": UNKNOWN,
    "attendees": UNKNOWN,
    "focalPerson": UNKNOWN,
    "approver_full_name": UNKNOWN,
    UNKNOWN: "approved",
    EMAIL: "approved",
    "contact_email": EMAIL,
    "email": EMAIL,
    "phone": PHONES[0],
    "mobile": PHONES[1],
    "comments": f"Called {UNKNOWN} at {PHONES[1]}",
    "narrative": f"Visit with {NAME} ({EMAIL}), call {PHONES[0]} or {PHONES[1]}; Mrs Person One agreed.",
    "location": {"name": "Tripoli", "focal_point": UNKNOWN},
    "scores": {STRANGER: 4},
    "participants_count": 12,
    "status": "Accepted",
}
FORBIDDEN = (NAME, EMAIL, UNKNOWN, STRANGER, *PHONES, "Quintel", "123 457")
# what every text column of NeuroDB's own tables holds (cut to the column's length)
TYPED_TEXT = f"Note by {NAME} ({EMAIL}) call {PHONES[0]} or {PHONES[1]}"
CONTACT = ("email", "phone", "mobile")


@pytest.fixture
def client_validator(client, roles):
    user = User.objects.create_user(username="validator", password="pass-123456-x", is_staff=True)
    user.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    User.objects.create_user(username="known", first_name="Person", last_name="One", password="pass-123456-x")
    client.force_login(user)
    return client


def _plant(name: str) -> int:
    """A record of ``name`` naming people every way, in its data and in each of its text columns: its pk."""
    if raw.typed(name):
        model = raw.model_of(name)
        fields = {"datamart_id": 4242, "data": PLANTED}
        if model is dm.HACTAggregate:
            fields["year"] = 2026
        for f in model._meta.concrete_fields:
            if isinstance(f, models.CharField | models.TextField) and not f.is_relation:
                fields[f.name] = TYPED_TEXT[: f.max_length] if f.max_length else TYPED_TEXT
        return model.objects.create(**fields).pk
    return dm.DatamartDocument.objects.create(
        dataset=name, record_key="4242", title=f"Report · {NAME}", data=PLANTED
    ).pk


def _download(client, name: str) -> tuple[list[str], list[list[str]]]:
    response = client.get(reverse("admin:datamart_raw_csv", args=[name]))
    assert response.status_code == 200, name
    rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    return rows[0], rows[1:]


@pytest.mark.parametrize("name", list(ENTITY_SYNCS))
def test_no_person_reaches_a_raw_csv(client_validator, name):
    _plant(name)
    header, rows = _download(client_validator, name)
    assert len(rows) == 1, name
    text = repr(rows)
    for planted in FORBIDDEN:
        assert planted not in text, (name, planted)
    for column in header:
        assert "@" not in column and UNKNOWN not in column, (name, column)
        bare = column.removesuffix(" (people)").removesuffix(" (text withheld)")
        assert bare not in CONTACT and not bare.startswith("contact"), (name, column)
        if (
            bare == column
            and column not in raw.NEURODB_COLUMNS + raw.PARTNER_REPORT_COLUMNS + raw.TAIL_COLUMNS
        ):
            assert not raw_people.names_people(column), (
                name,
                column,
            )  # a person field is a count, or withheld
    if name in catalogue.SAFEGUARDING:
        assert set(header) - set(raw.NEURODB_COLUMNS) <= catalogue.SAFEGUARDING_KEYS | {
            raw_people.WITHHELD_KEYS
        }
    else:
        row = dict(zip(header, rows[0], strict=True))
        assert row["participants_count"] == "12"
        assert row["submitted_by (people)"] == "1" and row["team_members (people)"] == "2"
        assert row["people_inside"] in ("0", "1")  # the location's focal point, when the location is shown


@pytest.mark.parametrize("name", list(ENTITY_SYNCS))
def test_no_person_reaches_a_source_s_list_or_a_record_s_page(client_validator, name):
    pk = _plant(name)
    for url in (
        reverse("admin:datamart_raw_source", args=[name]),
        reverse("admin:datamart_raw_record", args=[name, pk]),
    ):
        response = client_validator.get(url)
        assert response.status_code == 200, url
        html = response.content.decode()
        for planted in FORBIDDEN:
            assert planted not in html, (url, planted)


def test_the_progress_reports_export_counts_the_focal_points(client_viewer):
    partner = PartnerOrganization.objects.create(etl_id="1", name="Amel Association", vendor_number="V1")
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        number="LEB/PD1",
        title="A",
        status="active",
        unicef_focal_points=[NAME, EMAIL, "Second Person"],
    )
    dm.ReportedIndicator.objects.create(
        datamart_id=1,
        partner=partner,
        intervention=pd,
        pd_reference_number=pd.number,
        progress_report="PR-1",
        report_number="QPR1",
        report_type="QPR",
        report_status="Accepted",
        period_end=datetime.date(2026, 3, 31),
        indicator="# reached",
    )
    response = client_viewer.get(reverse("reports:partner_reporting"), {"export": "csv"})
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert (
        rows[0]["unicef_focal_points_count"] == "2"
    )  # an entry holding an e-mail address is no person NeuroDB names
    assert "unicef_focal_points" not in rows[0] and NAME not in text and "Second Person" not in text


def test_the_action_points_csv_says_whether_one_is_assigned_never_to_whom(client_viewer):
    dm.ActionPoint.objects.create(
        datamart_id=1, reference_number="AP/1", description=f"Call {NAME} back", assigned_to_name=NAME
    )
    dm.ActionPoint.objects.create(datamart_id=2, reference_number="AP/2", description="Follow up")
    response = client_viewer.get(reverse("reports:action_points"), {"export": "csv", "all": "1"})
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    rows = {r["reference"]: r for r in csv.DictReader(io.StringIO(text))}
    assert rows["AP/1"]["assigned_to_count"] == "1" and rows["AP/2"]["assigned_to_count"] == "0"
    assert "assigned_to" not in rows["AP/1"] and NAME not in text
