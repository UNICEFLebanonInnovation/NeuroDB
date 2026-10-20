"""Ask NeuroDB's eTools tools send no person from any dataset, not only field monitoring: the keys that
hold a person (assignees, authors, focal points, travellers) are dropped and never queried, every text is
sent without e-mail addresses, links, phone numbers, known person names or names written after a title,
and a search for a person's name finds no record. Texts are kept whole (no length rule outside field
monitoring), places named after a title stay under a place key, and amounts written as text pass."""

import json

import pytest

from neurodb.assistant import tools
from neurodb.datamart import models as dm
from neurodb.datamart import query
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

PD_NUMBER = "LEB/PCA2023597/PD2025123"
DESCRIPTION = (
    "Rania Canary will call Mrs Layla Saab (layla.saab@example.org, 03-123456, +44 20 7946 0958) and "
    "share https://evil.example/x before the visit to the water tank."
)
CANARIES = (
    "Rania Canary",
    "Layla Saab",
    "layla.saab@example.org",
    "03-123456",
    "+44 20 7946 0958",
    "evil.example",
    "Nour Haddad",
)
PERSON_KEYS = ("assigned_to_name", "assigned_to", "author", "focal_point")


@pytest.fixture
def point(db):
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="CSO", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", partner=partner, number=PD_NUMBER, title="Education")
    dm.ActionPoint.objects.create(
        datamart_id=1,
        partner=partner,
        intervention=pd,
        status="open",
        assigned_to_name="Rania Canary",
        related_module="fm",
        data={
            "id": 1,
            "assigned_to_name": "Rania Canary",
            "assigned_to": {"name": "Rania Canary"},
            "author": {"name": "Nour Haddad"},
            "description": DESCRIPTION,
            "location": {"name": "Sheikh Taba", "p_code": "LB1101", "focal_point": "Nour Haddad"},
            "budget": "2500000.00",
            "status": "open",
        },
    )
    return partner


def assert_clean(result):
    text = json.dumps(result, ensure_ascii=False, default=str)
    for canary in CANARIES:
        assert canary not in text, canary
    for key in PERSON_KEYS:
        assert f'"{key}"' not in text, key


def test_records_rows_examples_and_search_carry_no_person(point):
    record = tools.run("etools_record", {"dataset": "action_points", "record": "1"})
    rows = tools.run("etools_query", {"dataset": "action_points"})
    described = tools.run("etools_datasets", {"dataset": "action_points"})
    found = tools.run("etools_search", {"text": "water tank"})
    for result in (record, rows, described, found):
        assert_clean(result)
    data = record["data"]
    # the text is sent whole, cleaned: the rest of what it says is kept
    assert data["description"].endswith("before the visit to the water tank.")
    for marker in ("[name withheld]", "[email withheld]", "[phone withheld]", "[link withheld]"):
        assert marker in data["description"], marker
    assert data["location"] == {"name": "Sheikh Taba", "p_code": "LB1101"}  # a place keeps its title
    assert data["budget"] == "2500000.00"  # an amount written as text is not a phone number
    assert rows["rows"][0]["description"] == data["description"]
    assert "action_points" in {h["dataset"] for h in found["datasets"]}


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"filters": {"assigned_to_name": "Rania Canary"}}, "assigned_to_name"),
        ({"filters": {"assigned_to_name__contains": "Rania"}}, "assigned_to_name"),
        ({"group_by": "assigned_to_name"}, "assigned_to_name"),
        ({"group_by": "author"}, "author"),
        ({"fields": ["description", "assigned_to"]}, "assigned_to"),
        ({"order_by": "-assigned_to_name"}, "assigned_to_name"),
        ({"sum": "focal_point"}, "focal_point"),
    ],
)
def test_person_keys_cannot_be_queried_in_any_dataset(point, args, field):
    with pytest.raises(tools.ToolInputError) as raised:
        tools.run("etools_query", {"dataset": "action_points", **args})
    assert str(raised.value) == f"'{field}' holds personal data and cannot be queried."


def test_a_person_cannot_be_looked_for_in_any_dataset(point):
    with pytest.raises(tools.ToolInputError, match="eTools records cannot be searched for a person"):
        tools.run("etools_query", {"dataset": "action_points", "search": "Rania Canary"})
    with pytest.raises(tools.ToolInputError, match="eTools records cannot be searched for a person"):
        tools.run(
            "etools_query", {"dataset": "action_points", "filters": {"description__contains": "Rania Canary"}}
        )
    assert tools.run("etools_search", {"text": "Rania Canary"})["datasets"] == []
    assert tools.run("etools_search", {"text": "layla.saab@example.org"})["datasets"] == []
    # what is not a person is searched as before
    assert (
        tools.run("etools_query", {"dataset": "action_points", "search": "water tank"})["matching_records"]
        == 1
    )


def test_group_values_carry_no_person(point):
    by_text = tools.run("etools_query", {"dataset": "action_points", "group_by": "description"})
    assert_clean(by_text)
    assert by_text["groups"][0]["records"] == 1
    # a field that holds an object reaches the group as its JSON text: read back, without its person keys
    by_place = tools.run("etools_query", {"dataset": "action_points", "group_by": "location"})
    assert by_place["groups"] == [{"location": {"name": "Sheikh Taba", "p_code": "LB1101"}, "records": 1}]
    by_status = tools.run("etools_query", {"dataset": "action_points", "group_by": "status"})
    assert by_status["groups"] == [{"status": "open", "records": 1}]


def test_public_keeps_places_amounts_and_long_texts_and_drops_the_rest(point):
    place = query.public({"name": "Sheikh Taba", "admin_level_name": "Village"}, dataset="locations")
    assert place == {"name": "Sheikh Taba", "admin_level_name": "Village"}
    partner = query.public(
        {"title": "Visit of Dr. Haddad", "notes": "Seen by Rania Canary", "total": "-12.50", "x": "y" * 500},
        dataset="partners",
    )
    assert partner["title"] == "Visit of [name withheld]"  # outside a place key, a title takes the name
    assert "notes" not in partner and partner["total"] == "-12.50"
    assert partner["x"] == "y" * query.MAX_TEXT + "…"  # long texts are cut, not withheld


# ---- keys that hold a person by a role, a name split in parts, or "by" someone
PEOPLE_BY_ROLE = {
    "supervisor_name": "Karim Nassar",
    "reviewed_by_name": "Nour Haddad",
    "createdByName": "Omar Khalil",
    "authorized_officers": "Omar Khalil, Hiba Saad",
    "overall_approver": "Karim Nassar",
    "prc_officers": ["Hiba Saad"],
    "unicef_signatory_first_name": "Hiba",
    "unicef_signatory_last_name": "Saad",
    "partner_manager_name": "Omar Khalil",
    "contact_name": "Nour Haddad",
    "beneficiary_name": "Nour Haddad",
}
NOT_PEOPLE = {
    "partner_name": "Amel Association",
    "cp_output_name": "Children learn",
    "short_name": "Amel",
    "lead_office": "Tripoli",
    "lead_section": "Education",
    "persons_reached": 412,
    "team_members_count": 3,
    "staff_cost": 1200,
}


def test_keys_that_hold_a_person_by_a_role_are_dropped_and_others_stay(point):
    sent = query.public({**PEOPLE_BY_ROLE, **NOT_PEOPLE}, dataset="interventions")
    assert sent == NOT_PEOPLE
    for name in ("Karim Nassar", "Nour Haddad", "Omar Khalil", "Hiba", "Saad"):
        assert name not in json.dumps(sent)


@pytest.mark.parametrize("field", sorted(PEOPLE_BY_ROLE))
def test_keys_that_hold_a_person_by_a_role_cannot_be_queried(point, field):
    with pytest.raises(tools.ToolInputError, match="holds personal data"):
        tools.run("etools_query", {"dataset": "action_points", "group_by": field})


def test_the_names_a_record_holds_are_taken_out_of_its_own_texts(point):
    record = {
        "supervisor_name": "Karim Nassar",
        "signatory_first_name": "Hiba",
        "signatory_last_name": "Saad",
        "purpose": "Trip with Karim Nassar to Halba; Hiba Saad signs the report",
    }
    sent = query.public(record, dataset="staff_trips")
    assert sent == {"purpose": "Trip with [name withheld] to Halba; [name withheld] signs the report"}


# ---- texts: cleaned whole, then cut
def test_a_text_keeps_its_end_and_loses_its_phone_when_a_marker_is_longer(point):
    assert query.public({"note_text": "x@y.io: call 03 123 456 tomorrow"}, dataset="partners") == {
        "note_text": "[email withheld]: call [phone withheld] tomorrow"
    }


def test_examples_are_cleaned_before_they_are_cut(point):
    dm.DatamartDocument.objects.create(
        dataset="partners",
        record_key="9",
        title="Amel",
        data={"id": 9, "remarks": "x" * 61 + " hiba.saad@example.org is the contact for 03 123 456"},
    )
    example = tools.run("etools_datasets", {"dataset": "partners"})["fields_with_example_values"]["remarks"]
    assert len(example) <= 80 and "hiba" not in example and "@" not in example


def test_an_address_cut_short_is_withheld(point):
    assert query.public({"remarks": "write to hiba.saad@exam"}, dataset="partners") == {
        "remarks": "write to [email withheld]"
    }


# ---- phones and amounts
def test_amounts_and_counts_written_as_text_are_not_phone_numbers(point):
    record = {
        "amount": "2500000",
        "total": "2500000.00",
        "target": {"v": "1500000", "d": 1},
        "finding": "Ineligible expenditure of USD 1250000 (1 500 000 USD, LBP 5.000.000, target 1.200.000)",
    }
    assert query.public(record, dataset="audit_findings") == record


def test_phone_numbers_are_withheld_in_every_written_form(point):
    record = {
        "hotline": "03.123456",  # a key that names a way to reach someone is dropped whole
        "line": "03.123456",
        "reference": "71123456",
        "details": "Tel 01/345678, fax (01) 345679, guard 961-3-123456, office 03 12 34 56",
    }
    assert query.public(record, dataset="partners") == {
        "line": "[phone withheld]",
        "reference": "[phone withheld]",
        "details": "Tel [phone withheld], fax [phone withheld], guard [phone withheld], office [phone withheld]",
    }


def test_up_to_three_names_after_a_title_are_withheld(point):
    assert query.public({"details": "Call Mrs Layla Abi Saab tomorrow"}, dataset="partners") == {
        "details": "Call [name withheld] tomorrow"
    }


def test_a_place_keeps_its_title_only_under_a_key_that_holds_a_place(point):
    record = {"site_visit_notes": "Met Dr. Haddad at the school", "location": {"name": "Sheikh Taba"}}
    assert query.public(record, dataset="tpm_visits") == {
        "site_visit_notes": "Met [name withheld] at the school",
        "location": {"name": "Sheikh Taba"},
    }


def test_a_sentence_missing_its_space_is_not_a_link(point):
    text = "The session ended soon.To be continued approx.US standards"
    assert query.public({"details": text}, dataset="partners") == {"details": text}


# ---- donors and searches
def test_a_donor_is_not_a_person(point, django_user_model):
    from neurodb.donors.models import DonorAccount
    from neurodb.watch import people

    user = django_user_model.objects.create_user(username="donor", first_name="European Union")
    DonorAccount.objects.create(user=user, name="European Union", donors=["European Union"])
    people.forget()
    assert "european union" not in people.known_names()
    record = {"donor": "European Union", "remarks": "European Union contribution received"}
    assert query.public(record, dataset="grants") == record
    tools.run("etools_query", {"dataset": "action_points", "search": "European Union"})  # not refused


def test_a_search_for_a_person_says_why_it_finds_nothing(point):
    found = tools.run("etools_search", {"text": "Rania Canary"})
    assert found["datasets"] == [] and "people are never sent" in found["note"]


def test_a_search_never_looks_in_the_keys_that_hold_a_person(point):
    dm.DatamartDocument.objects.create(
        dataset="staff_trips",
        record_key="5",
        title="Trip 5",
        data={"id": 5, "supervisor_name": "Karim Nassar", "purpose": "Monitoring visit to Halba"},
    )
    assert tools.run("etools_query", {"dataset": "staff_trips", "search": "Nassar"})["matching_records"] == 0
    assert tools.run("etools_query", {"dataset": "staff_trips", "search": "Halba"})["matching_records"] == 1
    assert "staff_trips" not in {
        h["dataset"] for h in tools.run("etools_search", {"text": "Nassar"})["datasets"]
    }


def test_selected_fields_are_sent_as_the_whole_record_sends_them(point):
    whole = tools.run("etools_record", {"dataset": "action_points", "record": "1"})["data"]
    picked = tools.run("etools_query", {"dataset": "action_points", "fields": ["description", "location"]})
    row = picked["rows"][0]
    assert row["description"] == whole["description"] and row["location"] == whole["location"]


# ---- round 2: nested keys, group labels, split names, searches, other datasets' names
def _trip(key: str, data: dict) -> None:
    dm.DatamartDocument.objects.create(dataset="staff_trips", record_key=key, title=f"Trip {key}", data=data)


@pytest.mark.parametrize(
    "args", [{"group_by": "location__focal_point"}, {"order_by": "-location__focal_point"}, {"sum": "a__b"}]
)
def test_a_key_inside_an_object_cannot_be_grouped_sorted_or_summed(point, args):
    with pytest.raises(tools.ToolInputError, match="whole fields only"):
        tools.run("etools_query", {"dataset": "action_points", **args})


def test_group_labels_lose_the_names_the_grouped_records_hold(point):
    _trip("5", {"id": 5, "supervisor_name": "Karim Nassar", "purpose": "Trip with Karim Nassar to Halba"})
    groups = tools.run("etools_query", {"dataset": "staff_trips", "group_by": "purpose"})["groups"]
    assert groups == [{"purpose": "Trip with [name withheld] to Halba", "records": 1}]


def test_split_names_are_put_together_per_person(point):
    record = {
        "unicef_signatory_first_name": "Hiba",
        "unicef_signatory_last_name": "Saad",
        "partner_signatory_first_name": "Omar",
        "partner_signatory_last_name": "Khalil",
        "partner_focal_points": [{"title": "Director", "first_name": "Layla", "last_name": "Haddad"}],
        "implementation_strategy": "Signed by Hiba Saad and Omar Khalil; Layla Haddad confirmed the plan",
    }
    assert query.public(record, dataset="interventions") == {
        "implementation_strategy": "Signed by [name withheld] and [name withheld]; [name withheld] confirmed the plan"
    }


def test_a_search_or_a_filter_never_finds_what_is_withheld(point):
    # a name held only inside an object: learnt from the records (a search for it is refused), and part
    # of it finds nothing
    dm.ActionPoint.objects.create(
        datamart_id=2,
        status="open",
        data={"id": 2, "location": {"name": "Halba", "focal_point": "Ziad Karam"}},
    )
    with pytest.raises(tools.ToolInputError, match="cannot be searched for a person"):
        tools.run("etools_query", {"dataset": "action_points", "search": "Ziad Karam"})
    for args in (
        {"search": "Canary"},
        {"search": "Karam"},
        {"search": "assigned_to"},  # the name of a key that is never sent
        {"search": "focal_point"},
        {"filters": {"description__contains": "Rania Canar"}},
        {"filters": {"description__contains": "layla.saab@example"}},
        {"filters": {"location__contains": "Ziad"}},
    ):
        assert tools.run("etools_query", {"dataset": "action_points", **args})["matching_records"] == 0, args
    assert "action_points" not in {
        h["dataset"] for h in tools.run("etools_search", {"text": "Canary"})["datasets"]
    }
    # what is sent is still found
    assert (
        tools.run("etools_query", {"dataset": "action_points", "search": "Sheikh Taba"})["matching_records"]
        == 1
    )


def test_comparing_texts_letter_by_letter_is_refused(point):
    with pytest.raises(tools.ToolInputError, match="take a number or a date"):
        tools.run("etools_query", {"dataset": "action_points", "filters": {"description__gte": "Rania C"}})
    tools.run("etools_query", {"dataset": "action_points", "filters": {"due_date__gte": "2026-01-01"}})


def test_a_name_another_dataset_holds_is_taken_out_of_texts(point):
    _trip("6", {"id": 6, "supervisor_name": "Karim Nassar", "purpose": "Monitoring"})
    dm.DatamartDocument.objects.create(
        dataset="audits",
        record_key="7",
        title="Audit 7",
        data={"id": 7, "remarks": "Karim Nassar approved the plan"},
    )
    assert tools.run("etools_record", {"dataset": "audits", "record": "7"})["data"]["remarks"] == (
        "[name withheld] approved the plan"
    )


def test_more_keys_that_hold_a_person(point):
    record = {
        "travellers": ["Layla Saab"],
        "assignees": ["Omar Khalil"],
        "authors": "Hiba Saad",
        "budget_owner_name": "Layla Saab",
        "section_leads": ["Omar Khalil"],
        "approval": {"name": "Layla Saab", "date": "2026-01-02"},
        "location": {"name": "Halba"},
        "persons_reached": 412,
    }
    assert query.public(record, dataset="interventions") == {
        "approval": {"date": "2026-01-02"},
        "location": {"name": "Halba"},
        "persons_reached": 412,
    }
    assert query.public({"respondent_name": "Karim Nassar", "rating": "Good"}, dataset="fm_questions") == {
        "rating": "Good"
    }


# ---- round 2: what must still reach Ask
def test_counts_dates_parties_and_names_of_things_stay(point):
    from neurodb.datamart import catalogue

    for key in ("visits_by_status", "rated_visits_by_rating", "records_by_rating", "signed_by_unicef_date"):
        assert not catalogue.holds_person(key), key
    record = {
        "signed_by_unicef_date": "2024-03-01",
        "date_of_comments_by_ip": "2024-01-02",
        "supply_items": [{"provided_by": "unicef", "name": "Hygiene kit"}],
        "tpm_name": "Majmoua Firm",
        "ll_name": "Children learn",
        "alternate_name": "Amel",
        "lead_office": "Tripoli",
    }
    assert query.public(record, dataset="interventions") == record
    found = tools.run(
        "etools_query", {"dataset": "action_points", "filters": {"signed_by_unicef_date__gte": "2024-01-01"}}
    )
    assert found["matching_records"] == 0  # queried, not refused


def test_sections_named_under_a_person_key_do_not_wipe_the_record(point):
    from neurodb.accounts.models import Section

    Section.objects.create(name="Child Protection", code="CP")
    record = {"unicef_focal_points": ["Child Protection"], "sections": ["Child Protection"], "number": "PD1"}
    assert query.public(record, dataset="interventions") == {
        "sections": ["Child Protection"],
        "number": "PD1",
    }


def test_amounts_prices_and_organisations_are_not_people_or_phones(point):
    record = {
        "local_amount": "70000000",
        "remarks": (
            "Transport LBP 70.000.000 paid; budget of LBP 81 000 000; variance +1 250 000 LBP; "
            "10 hygiene kits @ USD 25 each; the +2023-2024 plan; sessions on MS Teams; returnees from DR Congo"
        ),
        "partner_name": "Dr. Mohamad Khaled Social Foundation",
    }
    assert query.public(record, dataset="intervention_budgets") == record


def test_more_ways_people_are_written_are_withheld(point):
    record = {
        "line": "96171123456",
        "remarks": (
            "Met Mme Layla Saab, Dr.Haddad and Dr. Karim el Khoury; call 0044 20 7946 0958 or 961 3 12 34 56; "
            "write to layla.saab at unicef dot org; see AMEL.ORG/report"
        ),
        "location": {"name": "Halba", "remarks": "Met Dr. Haddad at home"},
    }
    assert query.public(record, dataset="partners") == {
        "line": "[phone withheld]",
        "remarks": (
            "Met [name withheld], [name withheld] and [name withheld]; call [phone withheld] or [phone withheld]; "
            "write to [email withheld]; see [link withheld]"
        ),
        "location": {"name": "Halba", "remarks": "Met [name withheld] at home"},
    }


# ---- round 3: sums, comparisons and equality look only at what Ask is sent
def _without_record_people(monkeypatch):
    """NeuroDB's names without those the eTools records teach (``query.datamart_people``), so that a
    test can show what the record's own check does for a name nobody else knows."""
    from neurodb.watch import people

    monkeypatch.setattr(
        people, "EXTRA_SOURCES", [s for s in people.EXTRA_SOURCES if s is not query.datamart_people]
    )
    people.forget()


def test_a_sum_or_a_comparison_never_tells_a_withheld_number(point):
    dm.ActionPoint.objects.create(
        datamart_id=3,
        status="closed",
        data={
            "id": 3,
            "status": "closed",
            "reference": "71123456",
            "code": 71999888,
            "local_amount": "71123456",
        },
    )
    sent = tools.run("etools_record", {"dataset": "action_points", "record": "3"})["data"]
    assert sent["reference"] == sent["code"] == "[phone withheld]"  # a number written as a JSON number too
    assert sent["local_amount"] == "71123456"  # an amount stays

    def run(**args):
        return tools.run("etools_query", {"dataset": "action_points", **args})

    assert run(sum="reference")["sum"] == {"reference": None}
    assert run(sum="code")["sum"] == {"code": None}
    assert run(sum="local_amount")["sum"] == {"local_amount": 71123456.0}
    closed = [g for g in run(group_by="status", sum="reference")["groups"] if g["status"] == "closed"]
    assert closed == [{"status": "closed", "records": 1, "sum_reference": None}]
    for filters in (
        {"reference__gt": 71123455},
        {"reference__lt": 99999999},
        {"code__gte": 0},
        {"reference": "71123456"},
        {"reference": 71123456},
    ):
        assert run(filters=filters)["matching_records"] == 0, filters
    assert run(filters={"reference__ne": "71123456"})["matching_records"] == 2  # as for any other number
    with pytest.raises(tools.ToolInputError, match="take a number or a date"):
        run(filters={"reference__gte": "7112-00"})
    with pytest.raises(tools.ToolInputError, match="does not hold dates only"):
        run(filters={"reference__gte": "7112-00-00"})
    assert run(filters={"local_amount__gt": 71123455})["matching_records"] == 1


def test_equal_and_not_equal_compare_what_ask_is_sent(point, monkeypatch):
    _without_record_people(monkeypatch)
    dm.ActionPoint.objects.create(
        datamart_id=4,
        status="open",
        data={
            "id": 4,
            "status": "open",
            "remarks": "Met Dr. Haddad at home",
            "location": {"name": "Halba", "focal_point": "Ziad Karam"},
        },
    )

    def count(**filters):
        return tools.run("etools_query", {"dataset": "action_points", "filters": filters})["matching_records"]

    # the stored text and a guess give the same answer: nothing tells which one the record holds
    assert count(remarks="Met Dr. Haddad at home") == count(remarks="Met Dr. Saab at home") == 0
    assert count(remarks__ne="Met Dr. Haddad at home") == count(remarks__ne="Met Dr. Saab at home") == 2
    stored = json.dumps({"name": "Halba", "focal_point": "Ziad Karam"})
    guessed = json.dumps({"name": "Halba", "focal_point": "Omar Karam"})
    assert count(location=stored) == count(location=guessed) == 0
    assert count(location__ne=stored) == count(location__ne=guessed) == 2
    # what is not withheld is compared as before
    assert count(status="open") == 2 and count(status__ne="OPEN") == 0
    assert count(remarks__ne="Somewhere else") == 2


# ---- round 3: field monitoring searches clean as field monitoring sends
def test_field_monitoring_searches_look_in_texts_cleaned_as_field_monitoring_sends_them(db):
    data = {
        "id": 41,
        "monitoring_activity": "MA-41",
        "site_feedback": "Met Dr. Omar Khalil at the gate",
        "answer": "Guard on 3 123 456 at gate",
        "summary": "Called 3123456",
    }
    dm.DatamartDocument.objects.create(dataset="fm_questions", record_key="41", title="MA-41", data=data)
    sent = tools.run("etools_record", {"dataset": "fm_questions", "record": "41"})["data"]
    assert sent["site_feedback"] == "Met [name withheld] at the gate"
    assert sent["answer"] == "Guard on [phone withheld] at gate"
    assert sent["summary"] == "Called [phone withheld]"
    for args in (
        {"search": "Dr. Omar Khalil"},
        {"search": "3123456"},
        {"filters": {"site_feedback__contains": "Dr. Omar K"}},
        {"filters": {"answer__contains": "guard on 3 12"}},
    ):
        assert tools.run("etools_query", {"dataset": "fm_questions", **args})["matching_records"] == 0, args
    for text in ("Dr. Omar Khalil", "3123456"):
        found = {h["dataset"] for h in tools.run("etools_search", {"text": text})["datasets"]}
        assert "fm_questions" not in found, text
    assert tools.run("etools_query", {"dataset": "fm_questions", "search": "gate"})["matching_records"] == 1


# ---- round 3: the names other records hold, whole, nested or split
def test_names_held_nested_or_split_in_another_dataset_are_taken_out_of_texts(point):
    dm.ActionPoint.objects.create(
        datamart_id=5, data={"id": 5, "location": {"name": "Halba", "focal_point": "Ziad Karam"}}
    )
    dm.DatamartDocument.objects.create(
        dataset="interventions",
        record_key="8",
        data={
            "id": 8,
            "unicef_signatory_first_name": "Hiba",
            "unicef_signatory_last_name": "Saad",
            "supervisor_name": "Karim Nassar",
            "tpm_activities": [{"unicef_focal_points": ["Nour Fares"]}],
            "partner_focal_points": [{"first_name": "Rami", "last_name": "El Khoury"}],
        },
    )
    dm.DatamartDocument.objects.create(
        dataset="audits",
        record_key="9",
        data={
            "id": 9,
            "remarks": "Ziad Karam approved; Hiba Saad and Karim Nassar sign; Nour Fares and Rami El Khoury called",
        },
    )
    assert tools.run("etools_record", {"dataset": "audits", "record": "9"})["data"]["remarks"] == (
        "[name withheld] approved; [name withheld] and [name withheld] sign; [name withheld] and "
        "[name withheld] called"
    )
    with pytest.raises(tools.ToolInputError, match="cannot be searched for a person"):
        tools.run("etools_query", {"dataset": "action_points", "search": "Hiba Saad"})
    assert tools.run("etools_search", {"text": "Hiba Saad"})["datasets"] == []


def test_the_names_the_records_hold_are_read_at_each_sync_and_kept(point, monkeypatch):
    from neurodb.integrations.etools import datamart_sync

    _trip("6", {"id": 6, "supervisor_name": "Karim Nassar"})
    monkeypatch.setattr(datamart_sync, "ENTITY_SYNCS", {})
    datamart_sync.sync_all(client=object())
    stored = ["karim nassar", "nour haddad", "rania canary"]
    assert dm.RecordPeople.objects.get().names == stored and query.datamart_people() == stored
    # between two syncs the names stored are used as they are
    _trip("7", {"id": 7, "supervisor_name": "Hiba Saad"})
    assert query.datamart_people() == stored
    # none stored yet (before the first sync after a release): the latest records of each dataset are read
    dm.RecordPeople.objects.all().delete()
    monkeypatch.setattr(query, "PEOPLE_LATEST", 1)
    assert "karim nassar" not in query.datamart_people() and "hiba saad" in query.datamart_people()


def test_the_names_the_records_hold_survive_a_full_or_emptied_page_figure_cache(point, monkeypatch):
    """The page figures fill the shared cache past its 300 entries, and the Empty button empties it: the
    names read at the last sync are kept in their own table, so another dataset's texts still lose them
    when the latest records alone no longer hold them."""
    from django.core.cache import caches

    from neurodb.core import compute
    from neurodb.watch import people

    _trip("6", {"id": 6, "supervisor_name": "Karim Nassar"})
    dm.DatamartDocument.objects.create(
        dataset="audits", record_key="7", data={"id": 7, "remarks": "Karim Nassar approved the plan"}
    )
    query.remember_people()
    for n in range(400):
        caches["shared"].set(f"dev:overview:{n}", n, 600)
    compute.clear()
    _trip("8", {"id": 8, "purpose": "Monitoring"})
    monkeypatch.setattr(query, "PEOPLE_LATEST", 1)
    people.forget()
    assert tools.run("etools_record", {"dataset": "audits", "record": "7"})["data"]["remarks"] == (
        "[name withheld] approved the plan"
    )


# ---- round 3: titles
def test_a_name_after_mr_or_mrs_is_never_kept_and_after_dr_only_in_the_name_of_a_place(point):
    assert query.public(
        {
            "partner_feedback": "Mrs Hana Saleh confirmed receipt",
            "site_observation": "Met Dr. Omar Khalil at the gate",
            "tpm_feedback": "Mr. Omar Khalil was absent",
            "location": {"name": "Halba", "observation": "Dr. Omar Khalil absent"},
            "site_name": "Sheikh Taba school",
        },
        dataset="tpm_visits",
    ) == {
        "partner_feedback": "[name withheld] confirmed receipt",
        "site_observation": "Met [name withheld] at the gate",
        "tpm_feedback": "[name withheld] was absent",
        "location": {"name": "Halba", "observation": "[name withheld] absent"},
        "site_name": "Sheikh Taba school",
    }
    assert query.public(
        {"other_partners_involved": "Ministry of Social Affairs (Dr. Hana Saleh, head of the SDC) and Amel"},
        dataset="intervention_epd",
    ) == {"other_partners_involved": "Ministry of Social Affairs ([name withheld], head of the SDC) and Amel"}
    assert query.public(
        {"pd_title": "Support to Dr. Omar Khalil household", "name": "Sheikh Taba", "area": "Mr Omar Khalil"},
        dataset="intervention_locations",
    ) == {
        "pd_title": "Support to [name withheld] household",
        "name": "Sheikh Taba",
        "area": "[name withheld]",
    }
    assert query.public(
        {"name": "Mrs Hana Saleh", "partner_response": "Mr Omar Khalil agreed"}, dataset="partners"
    ) == {
        "name": "[name withheld]",
        "partner_response": "[name withheld] agreed",
    }


def test_more_titles_are_taken_with_the_name_and_degrees_are_not_titles(point):
    texts = {
        "Met Madam Hana Saleh at the centre": "Met [name withheld] at the centre",
        "Met Sir Omar Khalil": "Met [name withheld]",
        "Met Mx Omar Khalil and Hajj Karim Nassar": "Met [name withheld] and [name withheld]",
        "التقينا الأستاذ عمر خليل في المدرسة": "التقينا [name withheld] في المدرسة",
        "التقينا الحاج عمر خليل": "التقينا [name withheld]",
        "التقينا الآنسة هناء صالح": "التقينا [name withheld]",
        "التقينا د. عمر خليل": "التقينا [name withheld]",
        "Trainer holds an M.Sc in Education": "Trainer holds an M.Sc in Education",
        "Teachers at M. Sc. level and an M.Ed degree": "Teachers at M. Sc. level and an M.Ed degree",
        "Classes start at 5 م. in the centre": "Classes start at 5 م. in the centre",
    }
    for text, sent in texts.items():
        assert query.public({"remarks": text}, dataset="partners") == {"remarks": sent}, text


# ---- round 3: phones and amounts
def test_more_phone_forms_are_withheld_and_never_taken_for_amounts(point):
    texts = (
        "Hotline 03 123456/7",
        "Tel/Fax: 01-345678/9",
        "03123456/7",
        "call 71 12 34 56",
        "call 00 44 20 7946 0958",
        "call 03 - 123456",
        "call (961) 3-123456",
        "Contact the transporter on 03 123456 for the USD 300 invoice",
        "Driver 71 123 456, paid $20 per trip",
        "Paid $20 to 03-123456 driver",
        "number 03 123456; amount LBP 1,000",
        "Call 03 123 000 tomorrow",
    )
    for dataset in ("partners", "fm_questions"):
        for text in texts:
            sent = query.public({"answer": text}, dataset=dataset)["answer"]
            assert "[phone withheld]" in sent and not any(d in sent for d in ("123", "345", "7946")), (
                text,
                sent,
            )
    kept = "LBP 70.000.000 paid; budget LBP 81 000 000; LBP 71,500,000; +1 250 000 LBP; the +2023-2024 plan"
    assert query.public({"remarks": kept}, dataset="partners") == {"remarks": kept}
    for reference in ("PR 0010123456 raised", "FR 0400012345 line 1"):
        assert query.public({"remarks": reference}, dataset="partners") == {"remarks": reference}


def test_amounts_under_any_amount_key_are_not_phone_numbers(point):
    record = {
        "unicef_cash": "71000000",
        "cso_contribution": "81000000",
        "outstanding_amt": "76000000",
        "audited_expenditure": "79000000",
        "net_ct_cy": "70500000",
        "value": "78000000",
        "funds_received_to_date": "81250000",
        "achieved": {"v": "76000000", "d": "1", "c": "76000000"},
        "reference_code": 71123456,
    }
    sent = query.public(record, dataset="hact_history")
    assert sent == {**record, "reference_code": "[phone withheld]"}
    assert query.public({"total": "{'v': 71000000, 'd': 1, 'c': 71000000}"}, dataset="partner_reports") == {
        "total": {"v": 71000000, "d": 1, "c": 71000000}
    }
    trip = {
        "approved_cost_traveler": "350.00",
        "approved_cost_travel_agencies": "120.00",
        "funds_manager": "x",
    }
    assert query.public(trip, dataset="staff_trips") == {
        "approved_cost_traveler": "350.00",
        "approved_cost_travel_agencies": "120.00",
    }
    _trip("8", {"id": 8, "approved_cost_traveler": "350.00"})
    found = tools.run("etools_query", {"dataset": "staff_trips", "sum": "approved_cost_traveler"})
    assert found["sum"] == {"approved_cost_traveler": 350.0}
    # PRP's values, as an object or as its text, are summed as before
    progress = {"id": 11, "progress": "{'v': 12, 'd': 1, 'c': 12}", "reach": {"v": 7, "d": 1, "c": 7}}
    dm.DatamartDocument.objects.create(dataset="interventions", record_key="11", data=progress)
    for field, total in (("progress", 12.0), ("reach", 7.0)):
        found = tools.run("etools_query", {"dataset": "interventions", "sum": field})
        assert found["sum"] == {field: total}, field


# ---- round 3: search examples
def test_search_examples_are_cleaned_whole_before_they_are_cut(point):
    dm.ActionPoint.objects.create(
        datamart_id=6,
        description=(
            "Follow up on the water tank repairs at the school with the municipality; Rania Canary to call"
        ),
        data={"id": 6, "description": "Follow up on the water tank repairs of the school"},
    )
    dm.ActionPoint.objects.create(
        datamart_id=7,
        description="Send the photos of the water tank rehabilitation to the focal point omar.khalil@example.org",
        data={"id": 7, "description": "the water tank rehabilitation"},
    )
    dm.PDActivity.objects.create(
        datamart_id=8,
        code="1.1",
        name="Water tank rehab at the school, call Mr Ahmad Haddad on 03 123 456",
        data={"id": 8, "name": "Water tank rehab"},
    )
    dm.PartnerAssessment.objects.create(
        datamart_id=9,
        type="Micro Assessment",
        partner_name="Dr. Mohamad Khaled Social Foundation",
        data={"id": 9, "partner_name": "Dr. Mohamad Khaled Social Foundation", "note_text": "water tank"},
    )
    found = tools.run("etools_search", {"text": "water tank"})
    examples = {h["dataset"]: h["examples"] for h in found["datasets"]}
    text = json.dumps(examples)
    for part in ("Rania C", "omar.khalil", "Ahmad", "03 1"):
        assert part not in text, part
    assert "Micro Assessment Dr. Mohamad Khaled Social Foundation" in examples["assessments"]


# ---- round 3: name keys inside objects, objects written as text, keys that are people
def test_a_name_inside_an_object_of_no_thing_is_dropped_and_objects_written_as_text_too(point):
    record = {
        "approval": {"display_name": "Omar Khalil", "date": "2024-01-02"},
        "review": {"displayName": "Hiba Saad"},
        "sent_back": {"label": "Karim Nassar"},
        "remarks": "Omar Khalil approved; Hiba Saad reviewed; Karim Nassar sent it back",
        "history": [{"action": "approved", "display_name": "Nour Fares"}],
        "decision": "{'name': 'Layla Haddad', 'email': 'layla.haddad@example.org'}",
        "site": '{"name": "Halba", "focal_point": "Ziad Karam"}',
        "options": [{"label": "Yes"}],
    }
    assert query.public(record, dataset="action_points") == {
        "approval": {"date": "2024-01-02"},
        "remarks": "[name withheld] approved; [name withheld] reviewed; [name withheld] sent it back",
        "history": [{"action": "approved"}],
        "site": {"name": "Halba"},
        "options": [{"label": "Yes"}],
    }


def test_the_names_of_sections_offices_places_and_outputs_in_data_lists_stay(point):
    strategy = "Zahle Field Office runs sessions in Bab El Tabbaneh so that Children Learn Better."
    data = {
        "id": 10,
        "sections_data": [{"name": "Education", "source_id": 3}],
        "offices_data": [{"name": "Zahle Field Office"}],
        "cp_outputs_data": [{"name": "Children Learn Better"}],
        "locations_data": [{"name": "Bab El Tabbaneh", "pcode": "LB-AK"}],
        "funds_reservation": {"name": "FR 0400012345"},
        "implementation_strategy": strategy,
    }
    dm.DatamartDocument.objects.create(dataset="interventions", record_key="10", data=data)
    assert query.public(data, dataset="interventions") == {k: v for k, v in data.items() if k != "id"}
    by_office = tools.run("etools_query", {"dataset": "interventions", "group_by": "offices_data"})["groups"]
    assert by_office == [{"offices_data": [{"name": "Zahle Field Office"}], "records": 1}]
    assert (
        tools.run("etools_query", {"dataset": "interventions", "search": "Zahle Field Office"})[
            "matching_records"
        ]
        == 1
    )


def test_a_key_that_is_a_person_an_address_or_a_phone_is_dropped(point, django_user_model):
    django_user_model.objects.create_user(username="omar", first_name="Omar", last_name="Khalil")
    dm.ActionPoint.objects.create(
        datamart_id=11,
        data={
            "id": 11,
            "approvals": {
                "layla.saab@example.org": "approved",
                "Omar Khalil": "pending",
                "Was Dr. Haddad present? call +961 3 123456": "yes",
                "2024": "done",
            },
        },
    )
    record = tools.run("etools_record", {"dataset": "action_points", "record": "11"})["data"]
    assert record["approvals"] == {"2024": "done"}
    groups = tools.run("etools_query", {"dataset": "action_points", "group_by": "approvals"})["groups"]
    assert {"approvals": {"2024": "done"}, "records": 1} in groups


def test_split_names_with_parts_of_several_words_are_put_together(point):
    for first, last in (("Layla", "Abi Saab"), ("Marie Claire", "Haddad")):
        record = {
            "signatory_first_name": first,
            "signatory_last_name": last,
            "remarks": f"Signed by {first} {last}",
        }
        assert query.public(record, dataset="interventions") == {"remarks": "Signed by [name withheld]"}
    record = {
        "partner_focal_points": [{"title": "Director", "first_name": "Rami", "last_name": "El Khoury"}],
        "remarks": "Call Rami El Khoury before the visit",
    }
    assert query.public(record, dataset="interventions") == {
        "remarks": "Call [name withheld] before the visit"
    }


# ---- round 3: keys
def test_acronym_led_and_date_word_keys_that_hold_a_person_are_dropped(point):
    from neurodb.datamart import catalogue

    record = {
        "UNICEFFocalPoints": ["Omar Khalil"],
        "TPMFocalPoint": "Omar Khalil",
        "PRCReviewer": "Hiba Saad",
        "UNICEFSignatory": "Hiba Saad",
        "CSOManager": "Karim Nassar",
        "DCTOfficer": "Karim Nassar",
        "led_by": "Rania Haddad",
        "rated_by": "Hiba Saad",
        "ordered_by": "Hiba Saad",
        "focal_point_at_site": "Rami Haddad",
        "staff_on_duty": ["Rami Haddad"],
        "amount": 5,
    }
    assert query.public(record, dataset="tpm_visits") == {"amount": 5}
    for key in ("submitted_at", "created_on", "visits_by_status", "TPMVisitDate", "sorted_by_date"):
        assert not catalogue.holds_person(key), key
    for key in ("led_by", "UNICEFFocalPoints"):
        with pytest.raises(tools.ToolInputError, match="holds personal data"):
            tools.run("etools_query", {"dataset": "action_points", "group_by": key})


# ---- round 3: e-mail addresses and sentences
def test_addresses_written_with_spaces_or_dodges_are_withheld_and_sentences_stay(point):
    for text in (
        "write to omar.khalil @ example.org",
        "write to omar.khalil @example.org",
        "write to omar.khalil＠example.org",
        "write to omar.khalil[at]gmail[.]com",
        "write to omar.khalil(at)gmail(.)com",
        "write to omar.khalil at gmail.com",
    ):
        assert query.public({"remarks": text}, dataset="partners") == {
            "remarks": "write to [email withheld]"
        }, text
    for text in (
        "The centre is located at Tyre. Net enrolment rose by 5%. Session ended at 9. Net attendance 80%",
        "Training at CERD. Int partners joined; 2 trainers @ 50 USD/day; meeting @ 10am",
    ):
        assert query.public({"remarks": text}, dataset="prp_progress_reports") == {"remarks": text}


# ---- round 3: comments are not names
def test_the_words_of_comments_and_notes_are_never_taken_for_names(point):
    from neurodb.watch import people

    dm.ActionPoint.objects.create(
        datamart_id=12,
        data={
            "id": 12,
            "comments": [{"comment": "Water tank repaired in Halba school", "user": "Rami Haddad"}],
        },
    )
    _trip("9", {"id": 9, "report_note": "Partner distributed hygiene kits to families"})
    text = "Water tank repaired in Halba school; Partner distributed hygiene kits to families; Rami Haddad"
    dm.DatamartDocument.objects.create(dataset="audits", record_key="12", data={"id": 12, "remarks": text})
    known = people.known_names()
    assert "rami haddad" in known and not {"water school", "partner families"} & known
    assert tools.run("etools_record", {"dataset": "audits", "record": "12"})["data"]["remarks"] == (
        "Water tank repaired in Halba school; Partner distributed hygiene kits to families; [name withheld]"
    )
    assert (
        tools.run("etools_query", {"dataset": "audits", "search": "water tank repaired in Halba"})[
            "matching_records"
        ]
        == 1
    )
    assert "audits" in {
        h["dataset"] for h in tools.run("etools_search", {"text": "hygiene kits"})["datasets"]
    }


# ---- round 3: speed and honest counts
AT_LEAST_2 = "At least 2; etools_query with this search counts them all."


def test_a_search_of_every_dataset_checks_a_sample_and_says_how_many_it_found(point, monkeypatch):
    monkeypatch.setattr(query, "SEARCH_SAMPLE", 2)

    def trips() -> dict:
        found = tools.run("etools_search", {"text": "Halba"})["datasets"]
        return next(h for h in found if h["dataset"] == "staff_trips")

    for n in range(4):
        _trip(str(20 + n), {"id": 20 + n, "purpose": "Visit to Halba"})
    first = trips()
    assert first["records"] == 2 and first["note"] == AT_LEAST_2
    # the latest records hold the word only under a person key: older ones are checked, and the answer
    # is the same, word for word
    for n in (30, 31):
        _trip(str(n), {"id": n, "purpose": "Visit", "supervisor_name": "Halba Nassar"})
    assert trips() == first
    monkeypatch.setattr(query, "SEARCH_SAMPLE", 50)
    assert trips() == {"dataset": "staff_trips", "records": 4, "examples": trips()["examples"]}


def test_a_search_of_every_dataset_never_counts_records_that_hold_the_text_only_where_it_is_withheld(
    point, monkeypatch
):
    """The database's count also counts the records whose only match is a withheld name: never given,
    even as "about", so that comparing it with etools_query's count tells nothing."""
    monkeypatch.setattr(query, "SEARCH_SAMPLE", 2)
    for n in range(3):
        _trip(str(40 + n), {"id": 40 + n, "purpose": "Visit", "supervisor_name": "Nour Saidawi"})
    for n in range(2):
        _trip(str(50 + n), {"id": 50 + n, "purpose": "Visit to Saida"})
    found = tools.run("etools_search", {"text": "Saida"})["datasets"]
    trips = next(h for h in found if h["dataset"] == "staff_trips")
    assert trips["records"] == 2 and trips["note"] == AT_LEAST_2
    assert tools.run("etools_query", {"dataset": "staff_trips", "search": "Saida"})["matching_records"] == 2


def test_only_a_check_against_what_is_sent_is_ever_refused_for_too_many_records(point, monkeypatch):
    monkeypatch.setattr(query, "CONFIRM_MAX", 2)
    for n in range(3):
        dm.ActionPoint.objects.create(
            datamart_id=40 + n, status="open", data={"id": 40 + n, "status": "open"}
        )
    assert (
        tools.run("etools_query", {"dataset": "action_points", "filters": {"status": "open"}})[
            "matching_records"
        ]
        == 4
    )  # a plain word is compared by the database
    with pytest.raises(tools.ToolInputError, match="More than 2 records"):
        tools.run("etools_query", {"dataset": "action_points", "search": "open"})


# ---- round 4: sorting tells nothing a record withholds
def test_a_sort_on_a_text_or_an_object_follows_what_is_sent_never_the_stored_value(point):
    for key, focal in ((21, "Zeina Fares"), (22, "Adam Khalil"), (23, "Mona Saab")):
        dm.ActionPoint.objects.create(
            datamart_id=key,
            data={"id": key, "location": {"name": "Halba", "p_code": "LB1101", "focal_point": focal}},
        )
    rows = tools.run(
        "etools_query", {"dataset": "action_points", "order_by": "location", "fields": ["location"]}
    )["rows"]
    # sent alike, the three are in the latest-first order: the focal points' names decide nothing
    assert [r["record"] for r in rows] == ["23", "22", "21", "1"]
    assert rows[0]["location"] == {"name": "Halba", "p_code": "LB1101"}
    for key, purpose, supervisor in (
        ("31", "Joint visit", ""),
        ("32", "Karim Nassar visit to Halba", "Karim Nassar"),
        ("33", "Lunch meeting", ""),
        ("34", "Monitoring Tyre", ""),
    ):
        _trip(key, {"id": int(key), "purpose": purpose, "supervisor_name": supervisor})
    for order, expected in (
        ("purpose", ["[name withheld] visit to Halba", "Joint visit", "Lunch meeting", "Monitoring Tyre"]),
        ("-purpose", ["Monitoring Tyre", "Lunch meeting", "Joint visit", "[name withheld] visit to Halba"]),
    ):
        found = tools.run(
            "etools_query", {"dataset": "staff_trips", "order_by": order, "fields": ["purpose"]}
        )
        assert [r["purpose"] for r in found["rows"]] == expected, order
    # a key that is never sent cannot be sorted on, nor selected
    for args in ({"order_by": "emailaddress"}, {"fields": ["emailaddress"]}, {"group_by": "mobile_no"}):
        with pytest.raises(tools.ToolInputError, match="holds personal data"):
            tools.run("etools_query", {"dataset": "staff_trips", **args})


def test_numbers_and_dates_are_still_sorted_by_the_database(point):
    for key, cost, day in (
        ("41", "350.00", "2026-03-01"),
        ("42", "71123456", "2026-01-15"),
        ("43", "90", ""),
    ):
        _trip(key, {"id": int(key), "reference": cost, "end_date": day, "approved_cost": cost})

    def order(by: str, field: str) -> list:
        found = tools.run("etools_query", {"dataset": "staff_trips", "order_by": by, "fields": [field]})
        return [r[field] for r in found["rows"]]

    assert order("approved_cost", "approved_cost") == ["90", "350.00", "71123456"]  # an amount
    # a number with a phone's shape outside an amount key is withheld: it sorts with the empty values
    assert order("-reference", "reference") == ["350.00", "90", "[phone withheld]"]
    assert order("end_date", "end_date") == ["2026-01-15", "2026-03-01", ""]
    assert order("-end_date", "end_date") == ["2026-03-01", "2026-01-15", ""]


# ---- round 4: roles named after a thing are people; things stay
@pytest.mark.parametrize("role", ["visitor", "reporter", "agent", "pme", "poc"])
def test_roles_whose_word_starts_like_a_thing_hold_a_person(point, role):
    assert query.public({f"{role}_name": "Zaid Qammar", "rating": "Good"}, dataset="tpm_visits") == {
        "rating": "Good"
    }
    assert query.public({role: {"name": "Zaid Qammar", "date": "2026-01-02"}}, dataset="tpm_visits") == {
        role: {"date": "2026-01-02"}
    }
    record = {f"{role}_name": "Zaid Qammar", "purpose": "Zaid Qammar visit to Halba"}
    assert query.public(record, dataset="staff_trips") == {"purpose": "[name withheld] visit to Halba"}
    for args in ({"group_by": f"{role}_name"}, {"filters": {f"{role}_name__contains": "Qamm"}}):
        with pytest.raises(tools.ToolInputError, match="holds personal data"):
            tools.run("etools_query", {"dataset": "staff_trips", **args})


def test_the_names_of_organisations_places_and_phases_are_things_and_never_learnt(point):
    record = {
        "ngo_name": "Amel Association",
        "place_name": "Bourj Hammoud",
        "phase_name": "Phase Two",
        "foundation_name": "Khaled Foundation",
        "auditor_name": "Majmoua Firm",
        "agencies_data": [{"name": "Relief Agency"}],
        "p_code": "LB1101",
        "description": "Amel Association opened a centre in Bourj Hammoud. Phase Two started.",
    }
    assert query.public(record, dataset="intervention_epd") == record
    dm.DatamartDocument.objects.create(dataset="intervention_epd", record_key="60", data={"id": 60, **record})
    assert not {"amel association", "bourj hammoud", "phase two"} & set(query.remember_people())


# ---- round 4: keys that are numbers
def test_a_key_that_is_a_number_is_judged_by_its_dataset_s_rule(point):
    progress = {"1234567": {"v": 5, "d": 1, "c": 5}, "12345": {"v": 3, "d": 1, "c": 3}, "71123456": {"v": 1}}
    sent = query.public({"progress_by_location": progress}, dataset="prp_indicator_reports")
    assert sent == {"progress_by_location": {k: v for k, v in progress.items() if k != "71123456"}}
    assert query.public({"totals": {"1234567": 1, "2024": 5}}, dataset="fm_questions") == {
        "totals": {"2024": 5}
    }


# ---- round 4: phones
def test_phones_joined_or_after_a_word_s_full_stop_or_in_arabic_digits_are_withheld(point):
    for dataset in ("partners", "fm_questions"):
        for text, sent in (
            ("Mob: 03123456/71123456", "Mob: [phone withheld]"),
            ("03-123456/71-123456", "[phone withheld]"),
            ("70123456/03123456", "[phone withheld]"),
            ("03123456-71123456", "[phone withheld]"),
            ("03 123456/71 123456", "[phone withheld]"),
            (
                "Tel.01-345678, Mob.03-123456 (Mob.71 123456)",
                "Tel.[phone withheld], Mob.[phone withheld] (Mob.[phone withheld])",
            ),
            ("call 03 – 123456 or 03—123456", "call [phone withheld] or [phone withheld]"),
            ("اتصل على ٠٣١٢٣٤٥٦", "اتصل على [phone withheld]"),
            ("رقم الهاتف ٧١١٢٣٤٥٦ منذ ٢٠٢٤", "رقم الهاتف [phone withheld] منذ ٢٠٢٤"),
            ("+٩٦١ ٣ ١٢٣ ٤٥٦", "[phone withheld]"),
        ):
            assert query.public({"details": text}, dataset=dataset) == {"details": sent}, (dataset, text)


def test_time_ranges_and_figures_with_decimals_are_not_phones(point):
    texts = (
        "Remedial classes run 08.30-10.30 and 13.00-14.30",
        "Training 09.00-12.00 then lunch; shifts 07.30-08.45",
    )
    for dataset in ("partners", "fm_questions"):
        for text in texts:
            assert query.public({"remarks": text}, dataset=dataset) == {"remarks": text}, (dataset, text)
    figures = "achieved 76.923077% of the target; score 70.123456"
    assert query.public({"remarks": figures}, dataset="partners") == {"remarks": figures}


# ---- round 4: e-mail addresses in more written forms
def test_addresses_written_in_more_forms_are_withheld_whole(point):
    for text in (
        "write to layla.saab​@example.org",
        "write to Layla.Saab at Unicef.Org",
        "write to LAYLA.SAAB AT UNICEF.ORG",
        "write to layla.saab(@)example.org",
        "write to layla.saab [@] example.org",
        "write to layla.saab{at}example.org",
        "write to layla.saab﹫example.org",
        "write to layla . saab @ example . org",
    ):
        assert query.public({"details": text}, dataset="partners") == {
            "details": "write to [email withheld]"
        }, text
    assert query.public({"details": "write to layla(@)exam"}, dataset="partners") == {
        "details": "write to [email withheld]"
    }
    kept = "The centre is located at Tyre. Net enrolment rose; Training at CERD. Int partners joined"
    assert query.public({"details": kept}, dataset="partners") == {"details": kept}


# ---- round 4: names with a middle (father's) name, and names with an invisible character
def test_a_known_name_written_with_a_middle_name_is_withheld(point):
    _trip("6", {"id": 6, "supervisor_name": "Karim Nassar", "purpose": "Monitoring"})
    dm.DatamartDocument.objects.create(
        dataset="audits",
        record_key="7",
        data={
            "id": 7,
            "remarks": "Approved by Karim Ahmad Nassar and KARIM  NASSAR; Karim met Nassar; Ka​rim Nassar",
        },
    )
    assert tools.run("etools_record", {"dataset": "audits", "record": "7"})["data"]["remarks"] == (
        "Approved by [name withheld] and [name withheld]; Karim met Nassar; [name withheld]"
    )
    record = {"supervisor_name": "Karim Nassar", "purpose": "Trip with Karim Ahmad Nassar"}
    assert query.public(record, dataset="staff_trips", names=frozenset()) == {
        "purpose": "Trip with [name withheld]"
    }
    with pytest.raises(tools.ToolInputError, match="cannot be searched for a person"):
        tools.run("etools_query", {"dataset": "audits", "search": "Karim Ahmad Nassar"})


# ---- round 4: titles
def test_m_after_a_figure_or_in_a_time_is_not_a_title(point):
    for text in (
        "Budget of USD 2.5 M. The partner reached 300 children.",
        "Sessions end at 2 P.M. The Ministry of Education was informed.",
        "The visit started at 9 A.M. Teachers and parents attended.",
        "Funds: 3 M. Beirut received most.",
    ):
        for dataset in ("action_points", "fm_questions"):
            assert query.public({"description": text}, dataset=dataset) == {"description": text}, (
                dataset,
                text,
            )
    assert query.public({"description": "Met M. Haddad at the gate"}, dataset="partners") == {
        "description": "Met [name withheld] at the gate"
    }
    dm.ActionPoint.objects.create(
        datamart_id=25,
        data={"id": 25, "description": "Sessions end at 2 P.M. The Ministry of Education was informed."},
    )
    found = tools.run("etools_query", {"dataset": "action_points", "search": "Ministry of Education"})
    assert found["matching_records"] == 1


# ---- round 4: equalities on statuses, long texts, speed
def test_a_status_of_several_words_is_compared_without_a_cap_on_texts(point, monkeypatch):
    monkeypatch.setattr(query, "CONFIRM_MAX", 2)
    for n in range(3):
        dm.ActionPoint.objects.create(
            datamart_id=70 + n, data={"id": 70 + n, "status": "Sent back", "overall_status": "On track"}
        )

    def count(**filters):
        return tools.run("etools_query", {"dataset": "action_points", "filters": filters})["matching_records"]

    assert count(status="Sent back") == count(overall_status="on track") == 3
    assert count(overall_status__ne="On track") == 1  # the fixture's record
    with pytest.raises(tools.ToolInputError, match="More than 2 records"):
        count(status__contains="sent")  # a text is still checked record by record, under the cap


def test_an_equality_still_never_matches_a_value_that_holds_the_record_s_own_name(point, monkeypatch):
    _without_record_people(monkeypatch)
    for n, focal in ((80, "Ziad Karam"), (81, "")):
        dm.ActionPoint.objects.create(
            datamart_id=n,
            data={
                "id": n,
                "summary": "Ziad Karam visited",
                "location": {"name": "Halba", "focal_point": focal},
            },
        )

    def count(**filters):
        return tools.run("etools_query", {"dataset": "action_points", "filters": filters})["matching_records"]

    assert count(summary="Ziad Karam visited") == 1  # only the record that does not name him
    assert count(summary__ne="Ziad Karam visited") == 2


def test_a_long_text_is_checked_around_the_searched_words_as_the_whole_text_would_be(point):
    """A long text is cleaned only around each place the searched words are written: the answer is
    the whole text's, for names, titles, addresses and phones on either side."""
    from neurodb.datamart.query import Rules, Sent

    filler = " ".join(["the partner ran sessions for children in the centre"] * 8)
    names = frozenset({"karim nassar", "hiba saad"})
    texts = [
        f"{filler} Karim Nassar visited the water tank. {filler}",
        f"{filler} Dr. Omar Khalil checked the water tank. {filler}",
        f"{filler} call 03 123456 about the water tank {filler}",
        f"{filler} write to hiba.saad at unicef dot org about the water tank {filler}",
        f"{filler} the water tank was repaired by Hiba Saad {filler}",
        f"{filler} Hiba Ahmad Saad and the water tank {filler}",
    ]
    for dataset in ("partners", "fm_questions"):
        sent = Sent(Rules(names, dataset, whole=True))
        for text in texts:
            whole = query._clean_text(text, sent.rules, frozenset()).casefold()
            for needle in (
                "water tank",
                "nassar visited",
                "khalil checked",
                "123456 about",
                "unicef dot",
                "by hiba",
                "saad and",
                "children in",
            ):
                around = sent.holds({"remarks": text}, frozenset(), "remarks", needle, False)
                assert around == (needle in whole), (dataset, needle, text[400:470])


def test_the_check_around_the_searched_words_agrees_with_the_whole_text_on_random_texts(point):
    import random

    from neurodb.datamart.query import Rules, Sent

    rng = random.Random(4)
    pieces = (
        "the partner ran sessions for children in the centre",
        "Karim Nassar",
        "Karim Ahmad Nassar",
        "Hiba Saad Karam",
        "Dr. Omar Khalil",
        "Mrs Layla Abi Saab",
        "call 03 123456/7 or 71 12 34 56",
        "03123456/71123456",
        "+44 20 7946 0958",
        "write to hiba.saad at unicef dot org",
        "hiba . saad @ example . org",
        "see amel.org/report",
        "USD 2.5 M. The water tank",
        "at 9 A.M. Teachers",
        "water tank",
        "repaired,",
        "08.30-10.30",
    )
    names = frozenset({"karim nassar", "hiba saad", "saad karam"})
    needles = (
        "water tank",
        "nassar",
        "karim",
        "saad",
        "khalil",
        "the partner",
        "123456",
        "unicef",
        "teachers",
    )
    for dataset in ("partners", "fm_questions"):
        sent = Sent(Rules(names, dataset, whole=True))
        for _ in range(150):
            text = " ".join(rng.choice(pieces) for _ in range(rng.randint(20, 60)))
            if len(text) <= query.LONG_TEXT:
                continue
            whole = query._clean_text(text, sent.rules, frozenset()).casefold()
            for needle in needles:
                around = sent.holds({"remarks": text}, frozenset(), "remarks", needle, False)
                assert around == (needle in whole), (dataset, needle, text)


def test_the_benchmark_search_and_filters_stay_fast(point):
    """Not a measure of production: a guard that a search, a status equality and a sort on a few
    thousand records do not slow down by orders of magnitude (the full benchmark runs outside the suite)."""
    import time

    words = "school children learning sessions went well teachers parents centre attendance".split()
    dm.DatamartDocument.objects.bulk_create(
        dm.DatamartDocument(
            dataset="prp_indicator_reports",
            record_key=str(n),
            data={
                "id": n,
                "status": "Sent back" if n % 2 else "Accepted",
                "narrative_assessment": " ".join(words[(n + i) % len(words)] for i in range(60)),
                "submitted_by": f"Person{n % 50} Family{n % 7}",
            },
        )
        for n in range(3000)
    )
    start = time.perf_counter()
    for args in (
        {"search": "school"},
        {"filters": {"status": "Sent back"}},
        {"filters": {"narrative_assessment__contains": "went well"}},
        {"order_by": "status", "fields": ["status"]},
    ):
        tools.run("etools_query", {"dataset": "prp_indicator_reports", **args})
    assert time.perf_counter() - start < 20


# ---- round 5: what a search or a filter found never decides a refusal, a group, its order or a note
def _answer(dataset: str = "action_points", **args):
    """An etools_query answer, or the message of its refusal."""
    try:
        return tools.run("etools_query", {"dataset": dataset, **args})
    except tools.ToolInputError as refused:
        return str(refused)


def test_a_date_comparison_is_judged_on_the_whole_field_never_on_what_a_search_found(point):
    dm.ActionPoint.objects.create(
        datamart_id=90,
        assigned_to_name="Ziad Karam",
        data={
            "id": 90,
            "assigned_to_name": "Ziad Karam",
            "description": "Fix the water tank; call 03-123456 or write to zkaram@example.org",
            "due_date": "2026-02-01",
        },
    )
    query.remember_people()
    # part of the withheld name, phone or address and a guess give the same answer, letter by letter
    for filters in ({"description__gte": "2000-01-01"}, {"due_date__gte": "2000-01-01"}):
        answers = {
            json.dumps(_answer(search=text, filters=filters))
            for text in ("Ziad K", "Ziad Kara", "Ziad Q", "Ziad Karb")
        }
        assert len(answers) == 1, (filters, answers)
    refused = "'description__gte': 'description' does not hold dates only; compare dates or numbers."
    for part in ("03-1", "03-123", "03-13", "zkar", "zkaram@exa", "zkaq", "zkaram@exb"):
        assert _answer(filters={"description__contains": part, "description__gte": "2000-01-01"}) == refused
    # a field that holds dates only is still compared
    assert _answer(search="water tank", filters={"due_date__gte": "2026-01-01"})["matching_records"] == 1


def test_a_date_comparison_on_field_monitoring_tells_nothing_of_the_visit_lead(db):
    for n, lead in ((95, "Hiba Saad"), (96, "")):
        dm.MonitoringFinding.objects.create(
            datamart_id=n,
            visit_lead=lead,
            data={"id": n, "visit_lead": lead, "site": "Halba public school", "end": "2026-05-11"},
        )
    for filters in ({"site__gte": "2000-01-01"}, {"end__gte": "2000-01-01"}):
        answers = {
            json.dumps(_answer("field_monitoring", search=text, filters=filters))
            for text in ("Hiba Sa", "Hiba Sb")
        }
        assert len(answers) == 1, (filters, answers)


def test_groups_are_the_values_as_sent_counted_summed_and_ordered_as_sent(point):
    for n, (focal, cost) in enumerate(
        (("Adam Khalil", 10), ("Ziad Karam", 20), ("Mona Fares", 30)), start=91
    ):
        dm.ActionPoint.objects.create(
            datamart_id=n,
            data={
                "id": n,
                "cost": cost,
                "location": {"name": "Halba", "p_code": "LB1", "focal_point": focal},
            },
        )
    # one group per place as sent, never one per withheld focal point
    assert _answer(group_by="location", sum="cost")["groups"] == [
        {"location": {"name": "Halba", "p_code": "LB1"}, "records": 3, "sum_cost": 60.0},
        {"location": {"name": "Sheikh Taba", "p_code": "LB1101"}, "records": 1, "sum_cost": None},
    ]
    for n, person in enumerate(("Adam Khalil", "Ziad Karam", "Mona Fares"), start=60):
        _trip(str(n), {"id": n, "supervisor_name": person, "purpose": f"Visit with {person}"})
    # groups of one record each are ordered by what is sent: a withheld name falls nowhere among them
    for n, purpose in ((64, "Zahle office"), (65, "Ziad Karam"), (66, "Zgharta office"), (67, "Zouk office")):
        _trip(str(n), {"id": n, "purpose": purpose, "supervisor_name": purpose if n == 65 else ""})
    assert _answer("staff_trips", group_by="purpose")["groups"] == [
        {"purpose": "Visit with [name withheld]", "records": 3},
        {"purpose": "[name withheld]", "records": 1},
        {"purpose": "Zahle office", "records": 1},
        {"purpose": "Zgharta office", "records": 1},
        {"purpose": "Zouk office", "records": 1},
    ]


def test_too_many_records_to_check_is_counted_before_the_text_narrows_them(point, monkeypatch):
    monkeypatch.setattr(query, "CONFIRM_MAX", 2)
    for n in range(3):
        dm.ActionPoint.objects.create(
            datamart_id=100 + n,
            assigned_to_name="Ziad Karam",
            data={"id": 100 + n, "assigned_to_name": "Ziad Karam", "description": "Repair"},
        )
    refused = _answer(search="Ziad Q")
    assert refused.startswith("More than 2 records to look in") and _answer(search="Ziad K") == refused
    assert _answer(filters={"description__contains": "Ziad K"}) == refused
    # narrowed by the partner first, both find nothing
    for text in ("Ziad K", "Ziad Q"):
        assert _answer(search=text, partner="Amel")["matching_records"] == 0


def test_too_many_values_to_sort_or_group_is_never_counted_on_the_records_found(point, monkeypatch):
    """Three closed records whose places differ only by a withheld focal point, and three pending ones
    alike: the same answer for both."""
    monkeypatch.setattr(query, "CONFIRM_MAX", 2)
    rows = [("closed", "Halba", focal) for focal in ("Adam Khalil", "Ziad Karam", "Mona Fares")]
    rows += [("pending", "Tyre", "Adam Khalil")] * 3
    for n, (status, place, focal) in enumerate(rows, start=110):
        dm.ActionPoint.objects.create(
            datamart_id=n,
            status=status,
            data={"id": n, "status": status, "location": {"name": place, "focal_point": focal}},
        )
    for args in ({"group_by": "location"}, {"order_by": "location"}):
        closed = _answer(filters={"status": "closed"}, **args)
        assert isinstance(closed, str) and closed.startswith("More than 2 different values"), closed
        assert _answer(filters={"status": "pending"}, **args) == closed


def test_a_search_of_every_dataset_says_the_same_whatever_holds_the_text_where_it_is_withheld(
    point, monkeypatch
):
    def trips() -> dict | None:
        found = tools.run("etools_search", {"text": "Halba"})["datasets"]
        return next((h for h in found if h["dataset"] == "staff_trips"), None)

    def add(first: int, count: int, data: dict) -> None:
        dm.DatamartDocument.objects.bulk_create(
            dm.DatamartDocument(dataset="staff_trips", record_key=str(n), data={"id": n, **data})
            for n in range(first, first + count)
        )

    add(200, 50, {"purpose": "Visit to Halba"})
    seen = trips()
    assert (
        seen["records"] == 50
        and seen["note"] == "At least 50; etools_query with this search counts them all."
    )
    add(300, 60, {"purpose": "Visit", "supervisor_name": "Halba Nassar"})  # newer, withheld only
    assert trips() == seen
    dm.DatamartDocument.objects.filter(dataset="staff_trips").delete()
    add(400, 10, {"purpose": "Visit to Halba"})
    seen = trips()
    assert seen == {"dataset": "staff_trips", "records": 10, "examples": seen["examples"]}
    add(500, 200, {"purpose": "Visit", "supervisor_name": "Halba Nassar"})
    assert trips() == seen
    assert _answer("staff_trips", search="Halba")["matching_records"] == 10


def test_a_search_of_every_dataset_looks_in_the_latest_records_of_each_and_says_so(point, monkeypatch):
    monkeypatch.setattr(query, "SEARCH_WINDOW", 3)
    for n in range(4):
        _trip(str(20 + n), {"id": 20 + n, "purpose": "Visit to Halba"})
    found = tools.run("etools_search", {"text": "Halba"})
    trips = next(h for h in found["datasets"] if h["dataset"] == "staff_trips")
    assert trips["records"] == 3 and trips["note"] == (
        "At least 3: only the latest 3 records of the dataset were looked in; etools_query with this "
        "search counts them all."
    )
    assert found["note"] == (
        "Datasets of more than 3 records were looked in for their latest 3 only: etools_query with this "
        "search looks in all of a dataset's records."
    )


# ---- round 5: objects written as Python text
def test_an_object_written_as_python_text_with_decimals_and_dates_is_read_back(point):
    activities = (
        "[{'primary_traveler': 'Rami Haddad', 'cost': Decimal('120.00'), 'date': datetime.date(2024, 1, 1)}]"
    )
    _trip("70", {"id": 70, "purpose": "Visit", "trip_activities": activities})
    assert "rami haddad" in query.remember_people()
    sent = tools.run("etools_record", {"dataset": "staff_trips", "record": "70"})["data"]
    assert sent["trip_activities"] == [{"cost": "120.00", "date": "2024-01-01"}]
    for text in ("Rami H", "Haddad"):
        assert _answer("staff_trips", search=text)["matching_records"] == 0, text
    assert _answer("staff_trips", search="120.00")["matching_records"] == 1
    aware = (
        "{'traveler': 'Rami Haddad', 'at': datetime.datetime(2024, 1, 1, 9, 30, tzinfo=<UTC>), "
        "'ref': UUID('0b5a7d4e-1c2f-4a3b-9d8e-7f6a5b4c3d2e'), 'seats': (1, 2)}"
    )
    assert query.public({"trip": aware}, dataset="staff_trips", names=frozenset()) == {
        "trip": {"at": "2024-01-01T09:30:00", "ref": "0b5a7d4e-1c2f-4a3b-9d8e-7f6a5b4c3d2e", "seats": [1, 2]}
    }
    # an object that cannot be read is withheld whole, and the names its pairs hold are still learnt
    cut = "{'traveler': 'Rami Haddad', 'note': 'Visit to Halba"
    assert query.public({"trip": cut}, dataset="staff_trips", names=frozenset()) == {
        "trip": "[object withheld]"
    }
    assert "rami haddad" in query._own_names({"trip": cut})
    # texts that only start like a list stay; a text inside a tuple is cleaned
    texts = {"title": "[Draft] Visit to Halba", "steps": "[1, 2", "stage": "[Draft]", "marks": "[...]"}
    assert query.public(texts, dataset="staff_trips") == texts
    assert query.public(
        {"steps": "{'remarks': ('Met Rami Haddad today',)}"},
        dataset="staff_trips",
        names=frozenset({"rami haddad"}),
    ) == {"steps": {"remarks": ["Met [name withheld] today"]}}


# ---- round 5: titles
def test_titles_with_accents_spelled_out_or_that_are_also_words(point):
    for text, sent in (
        ("Mrs Élise Martin visited the school", "[name withheld] visited the school"),
        ("Mr Ömer Çelik met the partner", "[name withheld] met the partner"),
        ("Met with Mr. Élie Khoury today", "Met with [name withheld] today"),
        ("Dr. Émile Haddad checked", "[name withheld] checked"),
        ("Ustaz Édouard Saab came", "[name withheld] came"),
        ("Met Doctor Rami Haddad at the clinic", "Met [name withheld] at the clinic"),
        ("Met Mister Rami Haddad at the school", "Met [name withheld] at the school"),
        ("Professor Rami Haddad trained teachers", "[name withheld] trained teachers"),
        ("Engineer Rami Haddad checked the tank", "[name withheld] checked the tank"),
        ("Hajjeh Mona Fares runs the centre", "[name withheld] runs the centre"),
        ("Hajji Rami Haddad and Sayyed Omar Khalil came", "[name withheld] and [name withheld] came"),
        ("Miss Layla Saab called", "[name withheld] called"),
        ("Met Eng. Omar Khalil", "Met [name withheld]"),
    ):
        for dataset in ("partners", "fm_questions"):
            assert query.public({"answer": text}, dataset=dataset) == {"answer": sent}, (dataset, text)
    for title in (
        "No Child Will Miss Out On Learning in Akkar",
        "Eng Language Classes for Grade 4",
        "Prof Development Sessions for Teachers",
        "Hajj Season Health Response",
        "Children Miss School Days",
        "Doctor Visits Increased in Halba",
    ):
        assert query.public({"title": title}, dataset="interventions") == {"title": title}, title
    dm.DatamartDocument.objects.create(
        dataset="interventions",
        record_key="71",
        data={"id": 71, "title": "No Child Will Miss Out On Learning in Akkar"},
    )
    assert _answer("interventions", search="Learning in Akkar")["matching_records"] == 1
    found = tools.run("etools_search", {"text": "Miss Out"})["datasets"]
    assert "interventions" in {h["dataset"] for h in found}
    # Doctor, as Dr, keeps the name of an organisation, never a name in a text
    record = {"partner_name": "Doctor Rami Haddad Foundation", "remarks": "Met Doctor Rami Haddad"}
    assert query.public(record, dataset="partners") == {
        "partner_name": "Doctor Rami Haddad Foundation",
        "remarks": "Met [name withheld]",
    }


# ---- round 5: e-mail addresses and amounts
def test_addresses_spelled_out_with_any_ending_or_written_with_a_space_are_withheld(point):
    for text in (
        "write to rhaddad at hotmail.fr",
        "write to rhaddad at yahoo.co.uk",
        "write to rhaddad at unicef.org.lb",
        "write to rhaddad at unicef dot org dot lb",
        "write to rhaddad<at>unicef.org",
        "write to rhaddad -at- unicef.org",
        "write to rhaddad @unicef",
        "write to r . haddad at unicef . org",
        "write to layla dot saab at unicef dot org",
    ):
        for dataset in ("partners", "fm_questions"):
            assert query.public({"details": text}, dataset=dataset) == {
                "details": "write to [email withheld]"
            }, (
                dataset,
                text,
            )
    kept = (
        "We met at school.it was fine; stay-at-home orders; 2 trainers @ 50 USD/day; meeting @10am; "
        "The centre is located at Tyre. Net enrolment rose"
    )
    assert query.public({"details": kept}, dataset="partners") == {"details": kept}


def test_the_decimals_of_amounts_are_never_taken_for_an_international_number(point):
    for text in (
        "Unit costs 1500.00 1200.00 1300.00 per school",
        "Total 300.00 (2023) 450.00 (2024)",
        "USD 0.00 123456789",
        "Rates 12.00 14.00 16.00 18.00 20.00",
        "1,200.00 and 3,400.00",
    ):
        for dataset in ("partners", "fm_questions"):
            for key in ("description", "total_amount"):
                assert query.public({key: text}, dataset=dataset) == {key: text}, (dataset, key, text)
    for text in ("Call 0044 20 7946 0958", "call 00 961 3 123 456", "tel: 00.44.20.7946.0958"):
        assert "[phone withheld]" in query.public({"details": text}, dataset="partners")["details"], text


# ---- round 6: field monitoring answers, Arabic titles, long names, Syrian phones, speed, "@Place"
def test_a_field_monitoring_answer_under_value_is_an_answer_never_an_amount(point):
    """Field monitoring's answer keys ("value", "answer.value", "answer_value") hold what a monitor
    wrote: any 7 or 8 digits there are a phone, in a record, its rows, groups, examples, sums,
    comparisons and searches. Outside field monitoring "value" still holds an amount."""
    for key, data in (
        ("61", {"id": 61, "value": "71123456", "question": "Contact number of the site focal point"}),
        ("62", {"id": 62, "answer": {"value": "3123456", "label": "3123456"}, "question": "Guard"}),
        ("63", {"id": 63, "answer_value": 76123456, "question": "Driver"}),
        ("64", {"id": 64, "value": "2", "answer_value": 3, "question": "Classes"}),
    ):
        dm.DatamartDocument.objects.create(dataset="fm_questions", record_key=key, title=key, data=data)
    numbers = ("71123456", "3123456", "76123456")

    def assert_no_number(result):
        text = json.dumps(result, ensure_ascii=False, default=str)
        assert not any(number in text for number in numbers), text

    sent = {
        key: tools.run("etools_record", {"dataset": "fm_questions", "record": key})["data"]
        for key in ("61", "62", "63", "64")
    }
    assert sent["61"]["value"] == "[phone withheld]"
    assert sent["62"]["answer"] == {"value": "[phone withheld]", "label": "[phone withheld]"}
    assert sent["63"]["answer_value"] == "[phone withheld]"
    assert sent["64"]["value"] == "2" and sent["64"]["answer_value"] == 3  # an option's code stays
    assert_no_number(tools.run("etools_query", {"dataset": "fm_questions"}))
    assert_no_number(tools.run("etools_query", {"dataset": "fm_questions", "fields": ["value", "answer"]}))
    for field in ("value", "answer", "answer_value"):
        assert_no_number(tools.run("etools_query", {"dataset": "fm_questions", "group_by": field}))
    assert_no_number(tools.run("etools_datasets", {"dataset": "fm_questions"}))
    for field, total in (("value", 2.0), ("answer_value", 3.0)):
        found = tools.run("etools_query", {"dataset": "fm_questions", "sum": field})
        assert found["sum"] == {field: total}, field
    for key, bound in (("value__gt", 71000000), ("answer_value__gt", 76000000), ("value__lt", 99999999)):
        found = tools.run("etools_query", {"dataset": "fm_questions", "filters": {key: bound}})
        assert found["matching_records"] == (1 if key == "value__lt" else 0), key
    for text in ("7112345", "71123456", "3123456", "7612345"):
        assert tools.run("etools_query", {"dataset": "fm_questions", "search": text})["matching_records"] == 0
        found = {h["dataset"] for h in tools.run("etools_search", {"text": text})["datasets"]}
        assert "fm_questions" not in found, text
    # outside field monitoring, "value" is an amount
    assert query.public({"value": "71123456"}, dataset="hact_history") == {"value": "71123456"}


ARABIC_TITLED = {
    "تم إرسال التقرير للسيد كريم نصار": "تم إرسال التقرير [name withheld]",
    "للسيدة ليلى صعب": "[name withheld]",
    "للأستاذ كريم نصار": "[name withheld]",
    "للدكتور كريم نصار": "[name withheld]",
    "للمهندس كريم نصار": "[name withheld]",
    "للحاج كريم نصار": "[name withheld]",
    "للشيخ كريم نصار": "[name withheld]",
    "وللسيدة ليلى صعب": "و[name withheld]",
    "السيده ليلى صعب": "[name withheld]",
    "الاستاذه ليلى صعب": "[name withheld]",
    "الدكتوره ليلى صعب": "[name withheld]",
    "المهندسه ليلى صعب": "[name withheld]",
    "الحاجه ليلى صعب": "[name withheld]",
    "الانسه ليلى صعب": "[name withheld]",
    "السيد/ كريم نصار": "[name withheld]",
    "السيد/كريم نصار": "[name withheld]",
    "السيدة: ليلى صعب": "[name withheld]",
    "مدام ليلى صعب": "[name withheld]",
    "مع مدام ليلى صعب": "مع [name withheld]",
    "أ. كريم نصار": "[name withheld]",
}


def test_arabic_titles_in_their_written_forms_are_taken_with_the_name(point):
    for text, sent in ARABIC_TITLED.items():
        for dataset in ("action_points", "field_monitoring"):
            assert query.public({"description": text}, dataset=dataset) == {"description": sent}, (
                dataset,
                text,
            )
    # words that only look like a title stay, and a place keeps its Sheikh under a place key
    for text in ("الحاجز الأمني عند المدخل", "استخدام المياه", "مبدأ. الشراكة", "الحاجة إلى المياه"):
        assert query.public({"description": text}, dataset="action_points") == {"description": text}
    assert query.public({"location": {"name": "الشيخ طابا"}}, dataset="action_points") == {
        "location": {"name": "الشيخ طابا"}
    }
    narrative = "تم تسليم التقرير للسيدة ليلى صعب في المركز مع مدام هبة سعد"
    dm.DatamartDocument.objects.create(
        dataset="prp_progress_reports", record_key="81", data={"id": 81, "narrative": narrative}
    )
    sent = tools.run("etools_record", {"dataset": "prp_progress_reports", "record": "81"})["data"]
    assert sent["narrative"] == "تم تسليم التقرير [name withheld] في المركز مع [name withheld]"
    for text in ("ليلى صعب", "هبة سعد"):
        found = _answer("prp_progress_reports", search=text)
        assert found["matching_records"] == 0, text
    assert _answer("prp_progress_reports", search="تسليم التقرير")["matching_records"] == 1


def test_a_titled_name_of_three_or_four_parts_is_withheld_whole(point):
    texts = {
        "حضر السيد كريم أحمد نصار الاجتماع": "حضر [name withheld] الاجتماع",
        "السيدة ليلى محمد صعب": "[name withheld]",
        "د. كريم أحمد نصار": "[name withheld]",
        "السيد محمد عبد الرحمن نصار حضر": "[name withheld] حضر",
        "Mr. Mohamad Ali Hassan Khalil attended": "[name withheld] attended",
        "Met Mrs Layla Mona Abi Saab today": "Met [name withheld] today",
        # a title starts a name of its own, never one of the name before
        "Met Mrs Layla Saab Mrs Hiba Haddad": "Met [name withheld] [name withheld]",
        "Mr Omar Mr Omar Eng. Rami came": "[name withheld] [name withheld] [name withheld] came",
        "السيد كريم السيدة ليلى صعب": "[name withheld] [name withheld]",
        "السيد كريم د. ليلى صعب": "[name withheld] [name withheld]",
    }
    for text, sent in texts.items():
        for dataset in ("partners", "fm_questions"):
            assert query.public({"remarks": text}, dataset=dataset) == {"remarks": sent}, (dataset, text)
    dm.ActionPoint.objects.create(
        datamart_id=91, data={"id": 91, "description": "حضر السيد كريم أحمد نصار الاجتماع"}
    )
    sent = tools.run("etools_record", {"dataset": "action_points", "record": "91"})["data"]
    assert sent["description"] == "حضر [name withheld] الاجتماع"
    found = {h["dataset"] for h in tools.run("etools_search", {"text": "نصار"})["datasets"]}
    assert "action_points" not in found
    assert _answer("action_points", search="نصار")["matching_records"] == 0
    assert _answer("action_points", search="الاجتماع")["matching_records"] == 1


def test_syrian_mobile_numbers_are_phones_and_references_padded_with_zeros_stay(point):
    text = "Syrian 0944 123 456 or 0944123456 or +963 944 123 456 or 0933-123-456 or 0944.123.456"
    sent = "Syrian [phone withheld] or [phone withheld] or [phone withheld] or [phone withheld] or [phone withheld]"
    for dataset in ("action_points", "interventions", "tpm_activities", "spot_checks"):
        assert query.public({"description": text}, dataset=dataset) == {"description": sent}, dataset
    for dataset in ("action_points", "interventions", "field_monitoring", "fm_questions"):
        for phone in ("0944 123 456", "0944123456", "0933-123-456", "(0944) 123456"):
            assert query.public({"narrative": f"Call {phone}"}, dataset=dataset) == {
                "narrative": "Call [phone withheld]"
            }, (dataset, phone)
        assert query.public({"narrative": "0944123456"}, dataset=dataset) == {"narrative": "[phone withheld]"}
    kept = {
        "vendor_number": "0900012345",
        "reference": "PR 0910012345 raised",
        "remarks": "Sessions 09:30-10:30 and 09.30-11.00 on 09/12/2024; USD 0.944 per child",
    }
    assert query.public(kept, dataset="partners") == kept


def test_more_arabic_titles_take_one_name_or_a_role_and_keep_the_words_after(point):
    texts = {
        "السيد الوزير وافق على الخطة": "[name withheld] وافق على الخطة",
        "المهندس المسؤول عن المشروع": "[name withheld] عن المشروع",
        "مدرسة الشيخ زايد الرسمية في عكار": "مدرسة [name withheld] الرسمية في عكار",
        "زارت السيدة ليلى المدرسة في صيدا": "زارت [name withheld] في صيدا",
        "التقينا السيدة ليلى في المدرسة": "التقينا [name withheld] في المدرسة",
        "تحدث المهندس أحمد مع الشريك": "تحدث [name withheld] مع الشريك",
        "السيد أحمد و الشريك": "[name withheld] و الشريك",
        "السيد كريم، مدير المركز": "[name withheld]، مدير المركز",
        "الحاج محمد الخطيب": "[name withheld]",
        "مركز الحاج حسن": "مركز [name withheld]",
    }
    for text, sent in texts.items():
        assert query.public({"remarks": text}, dataset="partners") == {"remarks": sent}, text


def test_a_word_before_at_place_is_not_an_address_cut_short(point):
    for text in (
        "Workshop @Beirut on 12 May",
        "Visit @Akkar CDC with 40 children",
        "Follow @UNICEFLebanon",
        "Meeting @Halba Municipality",
    ):
        for dataset in ("partners", "fm_questions"):
            assert query.public({"details": text}, dataset=dataset) == {"details": text}, (dataset, text)
    for text, sent in (
        ("write to rhaddad @unicef", "write to [email withheld]"),
        ("write to rhaddad @unicef for details", "write to [email withheld] for details"),
        ("write to Rhaddad @unicef", "write to [email withheld]"),
        ("write to R.Haddad @Unicef", "write to [email withheld]"),
        ("write to r_haddad2 @Unicef", "write to [email withheld]"),
    ):
        assert query.public({"details": text}, dataset="partners") == {"details": sent}, text
    dm.ActionPoint.objects.create(
        datamart_id=92, data={"id": 92, "description": "Workshop @Beirut on 12 May"}
    )
    assert _answer("action_points", search="Beirut")["matching_records"] == 1


def test_the_check_without_cleaning_agrees_with_the_whole_text_with_the_record_s_own_names(point):
    """A place the searched words are written where no rule reaches is taken at once; anywhere else the
    text is cleaned: the answer is always the whole text's, short or long, with known and own names."""
    import random

    from neurodb.datamart.query import Rules, Sent

    rng = random.Random(6)
    pieces = (
        "the partner ran sessions for children in the centre",
        "Karim Nassar",
        "karim nassar",
        "Hiba Ahmad Saad",
        "Dr. Omar Khalil",
        "Mr Mohamad Ali Hassan Khalil",
        "Mrs Layla Abi Saab",
        "Eng. Rami",
        "السيدة ليلى صعب",
        "call 03 123456 or 0944 123 456",
        "+44 20 7946 0958",
        "write to hiba.saad at unicef dot org",
        "rhaddad @unicef",
        "Workshop @Beirut",
        "see amel.org/report",
        "water tank",
        "45 children",
        "Layla Saab",
        "layla saab",
    )
    names = frozenset({"karim nassar", "hiba saad"})
    own = frozenset({"layla saab"})
    needles = (
        "water tank",
        "nassar",
        "karim",
        "saab",
        "layla",
        "khalil",
        "the partner",
        "beirut",
        "children in",
        "rami",
    )
    for dataset in ("partners", "fm_questions"):
        sent = Sent(Rules(names, dataset, whole=True))
        for _ in range(200):
            text = " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
            for names_of_record in (frozenset(), own):
                whole = query._clean_text(text, sent.rules, names_of_record).casefold()
                for needle in needles:
                    around = sent.holds({"remarks": text}, names_of_record, "remarks", needle, False)
                    assert around == (needle in whole), (dataset, needle, names_of_record, text)
            record = {"remarks": text, "submitted_by": "Layla Saab"}
            whole = query._clean_text(text, sent.rules, own).casefold()
            for needle in needles:
                lazy = query._OwnNames(record)
                assert sent.holds(record, lazy, "remarks", needle, True) == (needle in whole), (needle, text)


def test_a_search_on_many_records_is_confirmed_mostly_without_cleaning_them(point, monkeypatch):
    """A guard on the speed of a search (the full benchmark, 19,000 PRP reports and 5,600 known names,
    runs outside the suite: 2 to 3 s for a search that finds them all): the records whose searched
    words no rule can reach are taken without cleaning their texts, so most texts are never cleaned."""
    import time

    words = "school children learning sessions went well teachers parents centre attendance Halba".split()
    dm.DatamartDocument.objects.bulk_create(
        dm.DatamartDocument(
            dataset="prp_indicator_reports",
            record_key=str(n),
            data={
                "id": n,
                "status": "Sent back" if n % 2 else "Accepted",
                "narrative_assessment": " ".join(words[(n + i) % len(words)] for i in range(90)),
                "challenges": "Transport to the school was delayed for 3 days"
                if n % 10
                else "Call 03 123456",
                "submitted_by": f"Person{n % 50} Family{n % 7}",
                "unicef_focal_points": [{"name": "Rania Canary", "email": "rania.canary@example.org"}],
            },
        )
        for n in range(4000)
    )
    cleaned = []
    clean_text = query._clean_text
    monkeypatch.setattr(
        query, "_clean_text", lambda value, *a, **k: cleaned.append(1) or clean_text(value, *a, **k)
    )
    start = time.perf_counter()
    for args, matching in (
        ({"search": "school"}, 4000),
        ({"search": "Halba"}, 4000),
        ({"filters": {"narrative_assessment__contains": "went well"}}, 4000),
        ({"filters": {"challenges__contains": "delayed"}}, 3600),
    ):
        found = tools.run("etools_query", {"dataset": "prp_indicator_reports", **args})
        assert found["matching_records"] == matching, args
    assert len(cleaned) < 2000, len(cleaned)  # of 4 x 4,000 records checked (and the 50 sent rows)
    assert time.perf_counter() - start < 20
    assert (
        tools.run("etools_query", {"dataset": "prp_indicator_reports", "search": "Rania"})["matching_records"]
        == 0
    )
