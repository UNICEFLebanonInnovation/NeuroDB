"""The people rule of Raw eTools data (datamart.raw_people): what a validator may see of a raw eTools record, and
download. Synthetic people only: "Person One", person.one@example.org, the phones +961 3 123 457 and 03 123 457, and
the unknown "Zorba Quintel"."""

from __future__ import annotations

import json

import pytest
from django.contrib.postgres.fields import ArrayField
from django.db import models

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, raw, raw_people, runfacts
from neurodb.datamart import models as dm
from neurodb.datamart.raw_people import NOT_REVIEWED, PEOPLE, TEXT, public_record
from neurodb.integrations.etools import rest_raw
from neurodb.partnerships import models as legacy
from neurodb.watch import people

pytestmark = pytest.mark.django_db

NAME, EMAIL = "Person One", "person.one@example.org"
PHONES = ("+961 3 123 457", "03 123 457")
UNKNOWN = "Zorba Quintel"
NAMES = frozenset(people.name_forms(NAME) | {EMAIL})


def _text_of(found) -> str:
    return json.dumps(found, ensure_ascii=False, default=str)


def _values(found) -> list:
    """The values of what is shown or written (field names aside: "focalPerson" names a field, not a person)."""
    if isinstance(found, dict):
        return [v for value in found.values() for v in _values(value)]
    if isinstance(found, list | tuple):
        return [v for value in found for v in _values(value)]
    return [found]


def _assert_no_people(*found) -> None:
    text = _text_of(_values(found))
    for planted in (NAME, EMAIL, UNKNOWN, *PHONES, "Person", "Quintel", "Zorba", "123 457"):
        assert planted not in text, planted


PLANTED = {
    "id": 77,
    "submitted_by": NAME,
    "visit_lead": NAME,
    "team_members": f"{NAME}, Second Person; Third Person",
    "assigned_to_name": NAME,
    "author_name": NAME,
    "primary_traveler": NAME,
    "unicef_focal_points_data": [{"first_name": "Person", "last_name": "One"}],
    "supervisor_name": NAME,
    "staff_members": [NAME, "Another Person"],
    "traveler_name": NAME,
    "prc_officers": [NAME],
    "reviewedByDisplay": NAME,
    "focalPerson": NAME,
    "approver_full_name": NAME,
    "participants": [UNKNOWN, "Quintel Zorba"],
    "interviewees": [{"full": UNKNOWN}],
    "pme": UNKNOWN,
    "beneficiary": UNKNOWN,
    "attendees": UNKNOWN,
    UNKNOWN: "approved",
    f"{EMAIL}": "approved",
    "contact_email": EMAIL,
    "phone": PHONES[0],
    "location": {"name": "Tripoli", "focal_point": NAME},
    "participants_count": 12,
}


def test_people_come_out_as_counts_and_never_as_names():
    dm.RawFieldReview.objects.create(source="field_monitoring", key="location")
    shown, withheld = public_record(PLANTED, "field_monitoring", NAMES)
    _assert_no_people(shown, {k: w.words for k, w in withheld.items()})
    assert shown["participants_count"] == 12
    assert shown["id"] == 77
    assert withheld["team_members"] == raw_people.Withheld(PEOPLE, 3)
    assert withheld["staff_members"] == raw_people.Withheld(PEOPLE, 2)
    assert withheld["participants"] == raw_people.Withheld(PEOPLE, 2)
    for key in (
        "submitted_by", "visit_lead", "assigned_to_name", "author_name", "primary_traveler", "supervisor_name",
        "traveler_name", "prc_officers", "reviewedByDisplay", "focalPerson", "approver_full_name", "pme",
        "beneficiary", "attendees", "interviewees", "unicef_focal_points_data",
    ):  # fmt: skip
        assert withheld[key].kind == PEOPLE and withheld[key].count == 1, key
        assert key not in shown
    # a field named by a person or an e-mail address is no column: it adds to one count
    assert withheld[raw_people.FIELDS_NAMING].count == 2
    assert UNKNOWN not in withheld and EMAIL not in withheld
    # contact fields are dropped, never counted
    assert "contact_email" not in withheld and "phone" not in withheld
    # a person inside a field shown: counted under its dotted path, and in people_inside
    assert shown["location"] == {"name": "Tripoli"}
    assert withheld["location.focal_point"] == raw_people.Withheld(PEOPLE, 1, inside=True)
    assert raw_people.people_inside(withheld) == 1
    assert withheld["team_members"].words == "3 people (not shown)"


def test_texts_people_wrote_are_withheld_never_counted_as_people():
    record = {"comments": f"Met {NAME}", "rejection_note": "x", "report_note": "y", "notes": ["a", "b"]}
    shown, withheld = public_record(record, "action_points", NAMES)
    assert shown == {}
    for key in record:
        assert withheld[key].kind == TEXT, key
        assert withheld[key].words == "text withheld (written by a person)"
    assert withheld["notes"].count == 2


def test_a_field_not_reviewed_shows_only_numbers_and_dates_until_a_superuser_reviews_it():
    record = {
        "site_feedback": "The visit went well",
        "visit_day": "2026-03-01",
        "amount_paid": 30,
        "flag": True,
    }
    shown, withheld = public_record(record, "fm_options", NAMES)
    assert "site_feedback" not in shown and withheld["site_feedback"] == raw_people.Withheld(NOT_REVIEWED, 1)
    assert withheld["site_feedback"].words == "1 value (not reviewed)"
    assert withheld["site_feedback"].cell == "1 values (not reviewed)"
    assert shown == {"visit_day": "2026-03-01", "amount_paid": 30, "flag": True}

    dm.RawFieldReview.objects.create(source="fm_options", key="site_feedback")
    shown, withheld = public_record(record, "fm_options", NAMES)
    assert shown["site_feedback"] == "The visit went well" and "site_feedback" not in withheld
    # reviewed, a text is still shown through the rule
    shown, _ = public_record({"site_feedback": f"Met {NAME} at {EMAIL}"}, "fm_options", NAMES)
    _assert_no_people(shown)

    dm.RawFieldReview.objects.filter(source="fm_options", key="site_feedback").delete()  # "Hide again"
    shown, withheld = public_record(record, "fm_options", NAMES)
    assert "site_feedback" not in shown and withheld["site_feedback"].kind == NOT_REVIEWED


def test_texts_lose_addresses_phones_links_and_names_and_amounts_keep_their_numbers():
    narrative = (
        f"Visit with {NAME} ({EMAIL}), call {PHONES[0]} or {PHONES[1]}, notes at https://example.org/x; "
        "Mrs Person One agreed."
    )
    shown, _ = public_record(
        {"narrative": narrative, "total_cumulative_progress": 3000000, "achievement": "3000000"},
        "partner_reports",
        NAMES,
    )
    _assert_no_people(shown)
    assert "https://" not in shown["narrative"] and "Visit with" in shown["narrative"]
    assert shown["total_cumulative_progress"] == 3000000


def test_typed_texts_and_titles_follow_the_payload_rule():
    long_text = f"{NAME} " + "x" * 120
    assert raw_people.column("field_monitoring", "narrative_finding", long_text, NAMES, frozenset()) == (
        catalogue.FM_TEXT_WITHHELD
    )  # withheld whole, never cut
    assert (
        raw_people.column("partner_reports", "submitted_by", f"{NAME}; Another One", NAMES, frozenset()) == 2
    )
    with pytest.raises(ValueError):
        raw_people.column("partnerorganization", "email", EMAIL, NAMES, frozenset())
    # a title built from a trip's purpose or a file's name, cleaned with the record's own names
    for source, data, title in (
        (
            "staff_trips",
            {"traveler": UNKNOWN, "purpose": f"Visit by {UNKNOWN}"},
            f"TA/1 · Visit by {UNKNOWN}",
        ),
        (
            "attachments",
            {"uploaded_by": UNKNOWN, "filename": f"{UNKNOWN} report.pdf"},
            f"{UNKNOWN} report.pdf",
        ),
    ):
        own = raw.dmq._own_names(data)
        cleaned = raw_people.column(source, "title", title, NAMES, own)
        assert UNKNOWN not in cleaned and "Zorba" not in cleaned, (source, cleaned)


def test_every_text_column_is_cleaned_and_codes_and_amounts_keep_their_values():
    """Not only the free texts of TEXT_COLUMNS: a name, an e-mail address or a phone in any of NeuroDB's own
    text columns (a funds reservation's text, a grant's description, an indicator's means of verification) is
    never shown."""
    planted = f"Advance for {NAME}, {EMAIL}, {PHONES[0]}"
    for name in catalogue.TYPED:
        for f in raw.model_of(name)._meta.concrete_fields:
            if f.is_relation or not isinstance(f, models.CharField | models.TextField):
                continue
            if (name, f.name) in raw_people.NEVER_COLUMNS | raw_people.PERSON_COLUMNS:
                continue
            _assert_no_people(raw_people.column(name, f.name, planted, NAMES, frozenset()))
    for name, column, value in (
        ("funds_reservations", "fr_number", "0400051234"),
        ("funds_reservations", "pd_reference_number", "LEB/PCA2020123/PD2021456"),
        ("funds_reservations", "wbs", "0810/A0/05/882/002/001"),
        ("funds_reservations", "line_item_text", "Advance, second tranche"),
        ("partner_reports", "total_cumulative_progress", "3000000"),
        ("partner_reports", "vendor_number", "2500212345"),
        ("pd_indicators", "means_of_verification", "Attendance sheets"),
        ("action_points", "status", "open"),
    ):
        assert raw_people.column(name, column, value, NAMES, frozenset()) == value, (name, column)


def test_a_name_used_as_a_field_name_inside_a_field_is_never_shown():
    record = {"id": 1, "scores": {UNKNOWN: 4, "Other Person": 2}, "disaggregation": {"()": 3, "(12, 31)": 1}}
    # a field nobody reviewed: numbers alone are not enough, the names inside it must pass too
    shown, withheld = public_record(record, "fm_options", NAMES)
    assert "scores" not in shown and withheld["scores"] == raw_people.Withheld(NOT_REVIEWED, 2)
    assert shown["disaggregation"] == {"()": 3, "(12, 31)": 1}  # PRP's ids hold no letter: they stay
    # a field reviewed: the names inside it are dropped, at any depth, and counted
    dm.RawFieldReview.objects.create(source="fm_options", key="scores")
    dm.RawFieldReview.objects.create(source="fm_options", key="marks")
    record["marks"] = [{"by_site": {UNKNOWN: 3, "Tripoli": 2}, "size": 3}]
    shown, withheld = public_record(record, "fm_options", NAMES)
    assert shown["scores"] == {} and shown["marks"] == [{"by_site": {"Tripoli": 2}, "size": 3}]
    assert withheld[raw_people.FIELDS_NAMING] == raw_people.Withheld(raw_people.FIELD_NAMES, 3)
    assert UNKNOWN not in _text_of(shown) and "Other Person" not in _text_of(shown)


def test_psea_answers_keep_only_the_safeguarding_fields():
    record = {
        "id": 1,
        "indicator_subject": "Policy",
        "rating": "Yes",
        "comments": "A case was described",
        "evidence": "file",
        "assessment_vendor_number": "250",
    }
    shown, withheld = public_record(record, "psea_answers", NAMES)
    assert set(shown) == {"id", "indicator_subject", "rating", "assessment_vendor_number"}
    assert withheld == {raw_people.WITHHELD_KEYS: raw_people.Withheld(raw_people.SAFEGUARDING, 2)}
    assert "comments" not in _text_of(withheld) and "evidence" not in _text_of(withheld)


def test_an_error_line_shows_only_its_kind():
    run = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="tpm_visits",
        status=SyncRun.Status.PARTIAL,
        rows_failed=1,
        details={
            "errors": [
                {"error": f"ValidationError: '{EMAIL}' is not valid for 4512", "count": 1, "examples": ["9"]}
            ]
        },
    )
    facts = runfacts.build("tpm_visits", run, None, (0, 0, 0))
    assert facts.error_lines[0].words == "ValidationError: … is not valid for …"
    assert EMAIL not in _text_of([line.words for line in facts.error_lines])


# --------------------------------------------------------------------------------------- the pinned lists
LEGACY = {
    legacy.PCA: "pca",
    legacy.PartnerOrganization: "partnerorganization",
    legacy.Agreement: "agreement",
    legacy.Travel: "travel",
    legacy.TravelActivity: "travelactivity",
    legacy.Engagement: "engagement",
    legacy.ActionPoint: "actionpoint",
}


def _models() -> dict[str, type[models.Model]]:
    """Every model a raw page, a record page or a raw CSV reads, by the name the pinned lists use."""
    found = {name: raw.model_of(name) for name in catalogue.TYPED}
    found["*documents"] = dm.DatamartDocument
    found["*rest"] = dm.RestRecord  # the eTools REST records
    for model, name in LEGACY.items():
        found[name] = model
    return found


def test_every_json_array_and_person_column_is_pinned():
    pinned = raw_people.JSON_COLUMNS | raw_people.PERSON_COLUMNS | raw_people.NEVER_COLUMNS
    missing = []
    for name, model in _models().items():
        for f in model._meta.get_fields():
            column = getattr(f, "name", "")
            if not column or column == "data" or getattr(f, "auto_created", False) and not f.concrete:
                continue
            key = (name, column)
            if name in LEGACY.values():
                # the partners, programme documents... a record links to: only their person columns matter (a page
                # reads their name, number, status and vendor number alone)
                if (
                    catalogue.holds_person(column)
                    and key not in raw_people.PERSON_COLUMNS | raw_people.NEVER_COLUMNS
                ):
                    missing.append(key)
                continue
            if isinstance(f, models.JSONField | ArrayField) and key not in pinned:
                missing.append(key)
            elif (
                catalogue.holds_person(column)
                and key not in raw_people.PERSON_COLUMNS | raw_people.NEVER_COLUMNS
            ):
                missing.append(key)
    assert not missing, f"list these in datamart.raw_people's pinned lists: {missing}"


REVIEWED = {
    "action_points": {"budget", "cost", "intervention_source_id", "local_amount", "overall_status", "partner_source_id"},
    "cp_indicators": {"target_numerator"},
    "engagements": {"spotcheck_total_amount_tested"},
    "fm_options": {"question_id"},
    "fm_programme_activities": {"intervention_number", "monitoring_activity_id", "vendor_number"},
    "fm_questions": {"monitoring_activity_id", "question_id"},
    "funds_reservations": {"source_intervention_id"},
    "hact_history": {"audits_outstanding_findings", "partner_source_id", "pv_completed_year"},
    "intervention_epd": {"hq_support_cost"},
    "interventions": {"agreement_id", "agreement_reference_number", "funds_reservation", "intervention_id"},
    "partner_reports": {
        "etools_cp_output_id", "etools_intervention_id", "indicator_type", "reporting_period_due_date"
    },
    "pd_indicators": {"cluster_indicator_id"},
    "planned_visits": {"partner_vendor_number"},
    "prp_indicator_reports": {"financial_contribution_currency", "status"},
    "prp_programme_documents": {"budget", "end_date", "legacy_id", "prp_id"},
    "prp_progress_reports": {"due_date", "programme_document_id", "status", "submission_date"},
    "staff_trips": {"approved_cost", "approved_cost_traveler", "end_date"},
    "staff_visits": {"source_partner_id", "source_partnership_id"},
    "tpm_visits": {"source_partner_id"},
}  # fmt: skip


def test_the_reviewed_fields_are_pinned_and_none_names_a_person():
    assert {k: set(v) for k, v in raw.REVIEWED_KEYS.items()} == REVIEWED
    assert raw.COUNTRY_KEYS == {"country_name", "business_area", "business_area_code"}
    for source, keys in raw.REVIEWED_KEYS.items():
        for key in keys:
            assert not raw_people.names_people(key) and not raw_people.written(key), (source, key)


def test_no_search_reads_a_person_or_a_text():
    for name in [*catalogue.dataset_names(), *raw.REST_ENTITIES]:
        for column in raw.view(name).search:
            for pinned in (raw_people.PERSON_COLUMNS, raw_people.TEXT_COLUMNS, raw_people.NEVER_COLUMNS):
                assert (name, column) not in pinned and ("*", column) not in pinned, (name, column)
            assert not raw_people.names_people(column), (name, column)
    assert raw.TITLE_NOT_SEARCHED == {"staff_trips", "attachments", "rest_travels", "rest_travel_details"}
    # an eTools REST trip's title is its reference number, never its purpose
    assert rest_raw.TITLE["rest_travels"] == rest_raw.TITLE["rest_travel_details"] == "reference_number"
