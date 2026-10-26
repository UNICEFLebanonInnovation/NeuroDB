import datetime as dt
from decimal import Decimal

import pytest
import responses
from django.core.management import CommandError, call_command

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import raw
from neurodb.geo.models import Location
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, Agreement, PartnerOrganization

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
API = f"{BASE}/api/latest/datamart"


def make_client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def page(dataset: str, results: list[dict]) -> None:
    responses.get(f"{API}/{dataset}/", json={"count": len(results), "next": None, "results": results})


def partner(source_id: int, **extra) -> dict:
    return {
        "id": 900 + source_id,
        "source_id": source_id,
        "name": f"Partner {source_id}",
        "short_name": f"P{source_id}",
        "partner_type": "Civil Society Organization",
        "cso_type": "National",
        "vendor_number": f"V{source_id}",
        "rating": "Low",
        "hidden": False,
        "deleted_flag": False,
        "total_ct_cy": "1500.00",
        "hact_values": {"programmatic_visits": {"completed": {"total": 2}}},
        **extra,
    }


def intervention(source_id: int, **extra) -> dict:
    return {
        "id": 700 + source_id,
        "source_id": source_id,
        "intervention_id": source_id,
        "number": f"LEB/PCA2023{source_id}/PD2024{source_id}",
        "title": f"Programme {source_id}",
        "document_type": "PD",
        "status": "active",
        "partner_name": "Partner 1",
        "partner_source_id": 1,
        "partner_vendor_number": "V1",
        "agreement_id": 55,
        "agreement_reference_number": "LEB/PCA2023123",
        "start_date": "2024-01-01",
        "end_date": "2025-06-30",
        "planned_programmatic_visits": 4,
        "sections_data": [{"name": "Education", "source_id": 3}],
        "offices_data": [{"name": "Beirut"}, {"name": "Zahle"}],
        "unicef_focal_points_data": [{"first_name": "Rana", "last_name": "K", "email": "r@example.org"}],
        "cp_outputs_data": [{"name": "Output 1"}],
        "locations_data": [{"name": "Akkar", "pcode": "LB-AK"}, {"name": "Unknown", "pcode": "LB-XX"}],
        "donors": ["European Union"],
        "grants": ["SC220001"],
        **extra,
    }


def run_all(only: str) -> list[SyncRun]:
    return sync.sync_all(only=only.split(","), triggered_by="test", client=make_client())


@responses.activate
def test_partners_and_interventions_feed_the_etools_tables():
    Location.objects.create(id=10, name="Akkar", p_code="LB-AK", lft=1, rght=2, level=0, tree_id=10)
    page("partners", [partner(1), partner(2, name="Second")])
    page("interventions", [intervention(11)])
    page(
        "interventions-budget",
        [
            {
                "id": 1,
                "source_id": 11,
                "budget_total": "1000.50",
                "budget_unicef_cash": "800",
                "budget_unicef_supply": "100",
                "budget_cso_contribution": "100.50",
                "budget_currency": "USD",
            }
        ],
    )
    page(
        "partners/agreements",
        [
            {
                "reference_number": "LEB/PCA2023123",
                "agreement_type": "PCA",
                "start": "2023-01-01",
                "end": "2025-12-31",
            }
        ],
    )
    runs = run_all("partners,interventions,intervention_budgets,agreements")
    assert [r.status for r in runs] == [SyncRun.Status.SUCCEEDED] * 4
    assert all(r.job == SyncRun.Job.ETOOLS_DATAMART for r in runs)

    p1 = PartnerOrganization.objects.get(etl_id="1")
    assert (p1.name, p1.vendor_number, p1.total_ct_cy) == ("Partner 1", "V1", "1500.00")
    pd = PCA.objects.get(etl_id="11")
    assert pd.partner == p1
    assert pd.number == "LEB/PCA202311/PD202411"
    assert (str(pd.start), str(pd.end), pd.planned_visits) == ("2024-01-01", "2025-06-30", 4)
    assert pd.section_names == ["Education"]
    assert pd.offices_set == ["Beirut", "Zahle"] and pd.offices_names == "Beirut, Zahle"
    assert pd.unicef_focal_points == ["Rana K"]
    assert pd.donors == ["European Union"] and pd.grants == ["SC220001"]
    assert pd.location_p_codes == ["LB-AK", "LB-XX"]
    assert list(pd.locations.values_list("p_code", flat=True)) == ["LB-AK"]
    assert (pd.total_budget, pd.unicef_cash, pd.total_unicef_budget, pd.budget_currency) == (
        "1000.50",
        "800",
        "900",
        "USD",
    )
    agreement = Agreement.objects.get(etl_id="55")
    assert (agreement.agreement_number, agreement.agreement_type, str(agreement.end)) == (
        "LEB/PCA2023123",
        "PCA",
        "2025-12-31",
    )
    assert pd.agreement == agreement


@responses.activate
def test_a_bad_record_is_counted_and_the_rest_are_written():
    page("partners", [partner(1), {"id": 5, "name": "no source id"}, partner(3)])
    (run,) = run_all("partners")
    assert run.status == SyncRun.Status.PARTIAL
    assert (run.rows_in, run.rows_written, run.rows_failed) == (3, 2, 1)
    assert set(PartnerOrganization.objects.values_list("etl_id", flat=True)) == {"1", "3"}


@responses.activate
def test_funds_reservations_link_to_the_pd_and_rebuild_its_donors():
    p = PartnerOrganization.objects.create(etl_id="1", name="Partner 1", partner_type="Government")
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    line = {
        "fr_number": "0400001",
        "donor": "European Union",
        "donor_code": "I49901",
        "grant_number": "SC220001",
        "total_amt": "1000",
        "actual_amt": "400",
        "outstanding_amt": "600",
        "source_intervention_id": 11,
        "pd_reference_number": "LEB/PD1",
        "start_date": "2024-01-01",
    }
    page(
        "funds-reservation",
        [
            {"id": 1, "line_item": 1, "overall_amount": "600", **line},
            {"id": 2, "line_item": 2, "overall_amount": "400", **line},
            {
                "id": 3,
                "line_item": 1,
                "overall_amount": "50",
                **line,
                "source_intervention_id": 999,
                "pd_reference_number": "UNKNOWN",
            },
        ],
    )
    (run,) = run_all("funds_reservations")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["not_linked"] == {"programme_document": 1}
    assert dm.FundsReservation.objects.filter(intervention=pd).count() == 2
    pd.refresh_from_db()
    assert pd.donors_set == [
        {"donor": "European Union", "donor_code": "I49901", "grant_number": "SC220001", "value": 1000.0}
    ]

    # The next full read replaces the table: a line no longer returned is removed.
    responses.replace(
        responses.GET,
        f"{API}/funds-reservation/",
        json={"results": [{"id": 1, "line_item": 1, "overall_amount": "600", **line}], "next": None},
    )
    (run,) = run_all("funds_reservations")
    assert run.details["removed"] == 2
    assert list(dm.FundsReservation.objects.values_list("datamart_id", flat=True)) == [1]
    pd.refresh_from_db()
    assert pd.donors_set[0]["value"] == 600.0

    # A PD whose last FR line disappears loses its FR donors.
    responses.replace(
        responses.GET,
        f"{API}/funds-reservation/",
        json={
            "results": [
                {
                    "id": 9,
                    "line_item": 1,
                    "overall_amount": "5",
                    **line,
                    "source_intervention_id": 999,
                    "pd_reference_number": "X",
                }
            ],
            "next": None,
        },
    )
    run_all("funds_reservations")
    pd.refresh_from_db()
    assert pd.donors_set == []


# ------------------------------------------------- funds reservations: linked by the number they carry first
PD1_NUMBER, PD2_NUMBER = "LEBA/PCA2020001/PD2020001-1", "LEBA/PCA2020002/PD2020002-2"
GRANT = {"donor": "Donor A", "donor_code": "I00001", "grant_number": "SC000001", "value": 100.0}


@pytest.fixture
def two_pds(db):
    """Two programme documents of two partners (fake numbers): PD1 (eTools id 11, Partner A) and PD2 (12, B)."""
    a = PartnerOrganization.objects.create(etl_id="1", name="Partner A", vendor_number="2500000001")
    b = PartnerOrganization.objects.create(etl_id="2", name="Partner B", vendor_number="2500000002")
    pd1 = PCA.objects.create(etl_id="11", number=PD1_NUMBER, title="PD one", partner=a)
    pd2 = PCA.objects.create(etl_id="12", number=PD2_NUMBER, title="PD two", partner=b)
    return pd1, pd2


def fr_line(pk: int, number: str = "", source_id: int | None = None, **extra) -> dict:
    return {
        "id": pk,
        "fr_number": "0400001",
        "line_item": pk,
        "donor": "Donor A",
        "donor_code": "I00001",
        "grant_number": "SC000001",
        "overall_amount": "100",
        "vendor_code": "",
        "pd_reference_number": number,
        "source_intervention_id": source_id,
        "start_date": "2026-01-01",
        **extra,
    }


def read_funds(*records: dict, dataset: str = "funds_reservations") -> SyncRun:
    path = "funds-reservation" if dataset == "funds_reservations" else "funds/fundsreservationheader"
    responses.upsert(responses.GET, f"{API}/{path}/", json={"results": list(records), "next": None})
    (run,) = run_all(dataset)
    return run


def pd_of(datamart_id: int, model: type = dm.FundsReservation) -> int | None:
    return model.objects.get(datamart_id=datamart_id).intervention_id


@responses.activate
def test_a_funds_line_links_to_the_pd_its_number_names_not_its_etools_id(two_pds):
    """The line carries PD1's number and PD2's eTools id: it sat on PD2 (the old rule, id first) and moves to PD1,
    which gains its grant while PD2 loses it."""
    pd1, pd2 = two_pds
    dm.FundsReservation.objects.create(datamart_id=1, intervention=pd2, grant_number="SC000001")
    PCA.objects.filter(pk=pd2.pk).update(donors_set=[GRANT])
    run = read_funds(fr_line(1, PD1_NUMBER, 12))
    assert run.status == SyncRun.Status.SUCCEEDED
    assert pd_of(1) == pd1.pk
    assert run.details["pd_link"] == {"by_number": 1, "exact": 1, "id_other": 1, "moved": 1}
    assert "not_linked" not in run.details
    pd1.refresh_from_db()
    pd2.refresh_from_db()
    assert (pd1.donors_set, pd2.donors_set) == ([GRANT], [])
    # the next read finds it where it is: nothing moves, the id still names another PD
    run = read_funds(fr_line(1, PD1_NUMBER, 12))
    assert run.details["pd_link"] == {"by_number": 1, "exact": 1, "id_other": 1}


@responses.activate
def test_a_funds_line_with_no_number_links_by_its_header_s_number_then_its_etools_id(two_pds):
    pd1, pd2 = two_pds
    dm.FundsReservationHeader.objects.create(
        datamart_id=1, fr_number="0400001", pd_reference_number=PD1_NUMBER
    )
    dm.FundsReservationHeader.objects.create(datamart_id=2, fr_number="0400002", pd_reference_number="")
    run = read_funds(fr_line(1, "", 12), fr_line(2, " ", 12, fr_number="0400002"))
    assert (pd_of(1), pd_of(2)) == (pd1.pk, pd2.pk)
    assert run.details["pd_link"] == {"by_number": 1, "header": 1, "id_other": 1, "by_id": 1}


@responses.activate
def test_a_funds_line_whose_number_no_pd_carries_is_not_linked_by_its_id(two_pds):
    pd1, _ = two_pds
    dm.FundsReservation.objects.create(datamart_id=1, intervention=pd1)
    run = read_funds(fr_line(1, "LEBA/PCA2020009/PD2020009-1", 11), fr_line(2, "", 99))
    assert (pd_of(1), pd_of(2)) == (None, None)
    assert run.details["pd_link"] == {"number_not_held": 1, "number_not_held_id_held": 1, "id_not_held": 1}
    assert run.details["not_linked"] == {"programme_document": 2}


@responses.activate
def test_a_funds_line_of_an_earlier_amendment_links_to_the_pd_held(two_pds):
    _, pd2 = two_pds
    run = read_funds(fr_line(1, "LEBA/PCA2020002/PD2020002-1", 11))
    assert pd_of(1) == pd2.pk
    assert run.details["pd_link"] == {"by_number": 1, "amended": 1, "id_other": 1}


@responses.activate
def test_a_bare_pd_number_links_by_its_pd_part(two_pds):
    pd1, _ = two_pds
    a = pd1.partner
    PCA.objects.create(etl_id="13", number="LEBA/PCA2020003/PD2020003-1", title="PD three", partner=a)
    PCA.objects.create(etl_id="14", number="LEBA/PCA2020004/PD2020003-2", title="PD four", partner=a)
    run = read_funds(fr_line(1, " pd2020001 "), fr_line(2, "PD2020003"))
    assert (pd_of(1), pd_of(2)) == (pd1.pk, None)  # a PD part two PDs carry links to neither
    assert run.details["pd_link"] == {"by_number": 1, "pd_part": 1, "unclear": 1}
    assert run.details["not_linked"] == {"programme_document": 1}


@pytest.fixture
def twins(two_pds):
    """Two programme documents under one number: the first runs through 2025, the second through 2026."""
    a = two_pds[0].partner
    number = "LEBA/PCA2020005/PD2020005-1"
    first = PCA.objects.create(
        etl_id="15",
        number=number,
        title="T1",
        partner=a,
        start=dt.date(2025, 1, 1),
        end=dt.date(2025, 12, 31),
    )
    second = PCA.objects.create(
        etl_id="16",
        number=number,
        title="T2",
        partner=a,
        start=dt.date(2026, 1, 1),
        end=dt.date(2026, 12, 31),
    )
    return number, first, second


@responses.activate
def test_two_pds_sharing_a_line_s_number_take_the_one_the_id_names(twins):
    number, first, _ = twins
    run = read_funds(fr_line(1, number, 15, start_date="2026-03-01"))
    assert pd_of(1) == first.pk  # the id and the number agree
    assert run.details["pd_link"] == {"by_number": 1, "exact": 1}


@responses.activate
def test_two_pds_sharing_a_line_s_number_else_the_one_whose_dates_cover_the_line(twins):
    number, first, second = twins
    run = read_funds(
        fr_line(1, number, None, start_date="2025-03-01"), fr_line(2, number, 99, start_date="2027-03-01")
    )
    assert (pd_of(1), pd_of(2)) == (first.pk, second.pk)  # no date covered: the one that ends last
    assert run.details["pd_link"] == {"by_number": 2, "exact": 2, "twice": 2}


@responses.activate
def test_a_funds_header_follows_the_same_rule(two_pds):
    pd1, pd2 = two_pds
    dm.FundsReservationHeader.objects.create(datamart_id=1, intervention=pd2)
    run = read_funds(
        {"id": 1, "fr_number": "0400001", "pd_reference_number": PD1_NUMBER, "source_intervention_id": 12},
        {"id": 2, "fr_number": "0400002", "pd_reference_number": "", "source_intervention_id": 12},
        {"id": 3, "fr_number": "0400003", "pd_reference_number": "LEBA/PCA2020009/PD2020009-1"},
        dataset="funds_reservation_headers",
    )
    header = dm.FundsReservationHeader
    assert (pd_of(1, header), pd_of(2, header), pd_of(3, header)) == (pd1.pk, pd2.pk, None)
    assert run.details["pd_link"] == {
        "by_number": 1,
        "exact": 1,
        "id_other": 1,
        "moved": 1,
        "by_id": 1,
        "number_not_held": 1,
    }


@responses.activate
def test_the_vendor_check_counts_lines_whose_vendor_is_the_pd_s_partner_per_link_kind(two_pds):
    """Partner A carries 2500000001, B 2500000002; spaces and leading zeros aside."""
    run = read_funds(
        fr_line(1, PD1_NUMBER, vendor_code="2500000001"),
        fr_line(2, PD1_NUMBER, vendor_code=" 002500000001"),
        fr_line(3, PD1_NUMBER, vendor_code="2500000002"),
        fr_line(4, PD1_NUMBER),  # no vendor: not compared
        fr_line(5, "LEBA/PCA2020002/PD2020002-1", vendor_code="2500000002"),
        fr_line(6, "", 11, vendor_code="2500000002"),
    )
    found = run.details["pd_link"]
    assert {k: v for k, v in found.items() if k.startswith("vendor_")} == {
        "vendor_compared_exact": 3,
        "vendor_same_exact": 2,
        "vendor_compared_amended": 1,
        "vendor_same_amended": 1,
        "vendor_compared_by_id": 1,
    }


@responses.activate
def test_a_funds_line_returned_twice_is_counted_once(two_pds):
    """Linked or not: "not linked" beside the sentence counts each record once, as the sentence does."""
    pd1, _ = two_pds
    first = [fr_line(1, PD1_NUMBER, 12), fr_line(2, "LEBA/PCA2020009/PD2020009-1", 11)]
    responses.get(
        f"{API}/funds-reservation/",
        match=[responses.matchers.query_param_matcher({"page_size": "500", "country_name": "Lebanon"})],
        json={"count": 4, "next": f"{API}/funds-reservation/?page=2", "results": first},
    )
    responses.get(
        f"{API}/funds-reservation/",
        match=[responses.matchers.query_param_matcher({"page": "2"})],
        json={"count": 4, "next": None, "results": first},
    )
    (run,) = run_all("funds_reservations")
    assert (pd_of(1), pd_of(2)) == (pd1.pk, None)
    assert run.details["pd_link"] == {
        "by_number": 1,
        "exact": 1,
        "id_other": 1,
        "number_not_held": 1,
        "number_not_held_id_held": 1,
    }
    assert run.details["not_linked"] == {"programme_document": 1}


@responses.activate
def test_the_lines_listed_as_naming_no_pd_are_those_the_sync_counted_so(two_pds):
    """Raw eTools data's "Their number names no PD NeuroDB holds" lists every line the sync counted so: by its
    own number, or by its FR header's for a line without one, never a line its own number links."""
    pd1, _ = two_pds
    gone = "LEBA/PCA2020009/PD2020009-1"
    dm.FundsReservationHeader.objects.create(datamart_id=1, fr_number="0400001", pd_reference_number=gone)
    dm.FundsReservationHeader.objects.create(
        datamart_id=2, fr_number="0400002", pd_reference_number=PD1_NUMBER
    )
    run = read_funds(
        fr_line(1, "", 11),  # its header's number names no PD NeuroDB holds
        fr_line(2, "LEBA/PCA2020008/PD2020008-1"),  # its own names none
        fr_line(3, "", 12, fr_number="0400002"),  # its header's names PD1
        fr_line(4, PD1_NUMBER),  # its own number decides, whatever its header's
        fr_line(5, "", 11, fr_number="0400003"),  # no header: by its eTools id
    )
    assert run.details["pd_link"]["number_not_held"] == 2
    assert [pd_of(pk) for pk in range(1, 6)] == [None, None, pd1.pk, pd1.pk, pd1.pk]
    listed = raw.Filters(fr_link="number_not_held").apply(
        "funds_reservations", raw.base("funds_reservations"), run
    )
    assert sorted(listed.values_list("datamart_id", flat=True)) == [1, 2]


@responses.activate
def test_an_empty_answer_never_empties_a_table():
    dm.Grant.objects.create(datamart_id=1, name="SC1", donor="EU")
    page("funds/grants", [])
    (run,) = run_all("grants")
    # the rows held before the read and the count the Datamart announced are recorded on every run
    assert run.details == {"removed": 0, "empty_response": True, "held_before": 1, "datamart_count": 0}
    assert dm.Grant.objects.count() == 1


def grant(pk: int, **extra) -> dict:
    return {"id": pk, "name": f"SC{pk}", "donor": "EU", **extra}


@responses.activate
def test_a_read_shorter_than_the_datamart_announced_removes_nothing():
    """The first page announces 4 grants, the answer ends after 2 (a page cut short, rows moving while the
    pages were read): the grant it did not return stays, and the run says so as PARTIAL."""
    for pk in (1, 2, 3):
        dm.Grant.objects.create(datamart_id=pk, name=f"SC{pk}", donor="EU")
    responses.get(f"{API}/funds/grants/", json={"count": 4, "next": None, "results": [grant(1), grant(2)]})
    (run,) = run_all("grants")
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 0
    assert run.details["removal_skipped"] is True and run.details["removed"] == 0
    assert (run.details["read"], run.details["datamart_count"], run.details["held_before"]) == (2, 4, 3)
    assert sorted(dm.Grant.objects.values_list("datamart_id", flat=True)) == [1, 2, 3]


@responses.activate
def test_a_whole_read_with_another_country_s_record_still_removes_what_is_gone():
    """The count includes another country's record the Datamart let through (and the sync left out): read and
    skipped together make the whole answer, so the grant no longer returned is removed."""
    for pk in (1, 3):
        dm.Grant.objects.create(datamart_id=pk, name=f"SC{pk}", donor="EU")
    results = [grant(1, country_name="Lebanon"), grant(9, country_name="Syria")]
    responses.get(f"{API}/funds/grants/", json={"count": 2, "next": None, "results": results})
    (run,) = run_all("grants")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["removed"] == 1 and run.details["other_country_skipped"] == 1
    assert "removal_skipped" not in run.details
    assert list(dm.Grant.objects.values_list("datamart_id", flat=True)) == [1]


@responses.activate
def test_a_record_returned_twice_removes_nothing_and_says_so():
    """Three grants held; the Datamart announces 3 and returns grant 1 and 2 on page 1, grant 2 again on page 2
    (rows moving between its pages) and never grant 3: three rows read, two records. Grant 3 stays, and
    the run says why as PARTIAL."""
    for pk in (1, 2, 3):
        dm.Grant.objects.create(datamart_id=pk, name=f"SC{pk}", donor="EU")
    responses.get(
        f"{API}/funds/grants/",
        match=[responses.matchers.query_param_matcher({"page_size": "500", "country_name": "Lebanon"})],
        json={"count": 3, "next": f"{API}/funds/grants/?page=2", "results": [grant(1), grant(2)]},
    )
    responses.get(
        f"{API}/funds/grants/",
        match=[responses.matchers.query_param_matcher({"page": "2"})],
        json={"count": 3, "next": None, "results": [grant(2)]},
    )
    (run,) = run_all("grants")
    assert sorted(dm.Grant.objects.values_list("datamart_id", flat=True)) == [1, 2, 3]
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 0
    details = run.details
    assert details["removal_skipped"] is True and details["removed"] == 0
    assert (details["read"], details["duplicates_read"], details["datamart_count"]) == (2, 1, 3)


@responses.activate
def test_a_server_error_on_a_later_page_fails_the_run_and_removes_nothing():
    """Today's behaviour, pinned: a page that fails ends the read before anything is removed."""
    for pk in (1, 2, 3):
        dm.Grant.objects.create(datamart_id=pk, name=f"SC{pk}", donor="EU")
    responses.get(
        f"{API}/funds/grants/",
        match=[responses.matchers.query_param_matcher({"page_size": "500", "country_name": "Lebanon"})],
        json={"count": 4, "next": f"{API}/funds/grants/?page=2", "results": [grant(1), grant(2)]},
    )
    responses.get(
        f"{API}/funds/grants/", match=[responses.matchers.query_param_matcher({"page": "2"})], status=500
    )
    (run,) = run_all("grants")
    assert run.status == SyncRun.Status.FAILED and "HTTP 500" in run.error
    assert dm.Grant.objects.count() == 3


@responses.activate
def test_engagements_link_partner_by_vendor_and_pds_by_number():
    p = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Government", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    page(
        "audit/engagements",
        [
            {
                "id": 40,
                "partner_name": "Partner 1",
                "partner_code": "V1",
                "partner": {"name": "Partner 1"},
                "engagement_type": "sc",
                "status": "final",
                "reference_number": "LEB/2024/SC/01",
                "start_date": "2024-02-01",
                "total_value": "25000",
                "spotcheck_total_amount_tested": "12000",
                "financial_findings": "150.5",
                "active_pd_data": [{"number": "LEB/PD1"}],
            }
        ],
    )
    (run,) = run_all("engagements")
    assert run.status == SyncRun.Status.SUCCEEDED
    engagement = dm.AuditEngagement.objects.get(datamart_id=40)
    assert engagement.partner == p and engagement.vendor_number == "V1"
    assert engagement.amount_tested == Decimal("12000") and engagement.financial_findings == Decimal("150.5")
    assert list(engagement.interventions.all()) == [pd]
    assert engagement.data["reference_number"] == "LEB/2024/SC/01"


@responses.activate
def test_action_points_indicators_and_monitoring_link_to_existing_rows():
    p = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Government", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    page(
        "actionpoints",
        [
            {
                "id": 1,
                "source_id": 501,
                "reference_number": "LEB/2024/1/APD",
                "status": "open",
                "description": "Fix it",
                "due_date": "2024-01-31",
                "partner_source_id": 1,
                "intervention_source_id": 11,
                "related_module": "audit",
                "high_priority": True,
            }
        ],
    )
    page(
        "pd-indicators",
        [
            {
                "id": 1,
                "source_id": 77,
                "title": "Children reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Akkar",
                "target_numerator": "100",
                "display_type": "number",
            },
            {
                "id": 2,
                "source_id": 77,
                "title": "Children reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Beirut",
                "target_numerator": "100",
                "display_type": "number",
            },
        ],
    )
    page(
        "fm-ontrack",
        [
            {
                "id": 1,
                "source_id": 9,
                "vendor_number": "V1",
                "entity": "Partner 1",
                "entity_type": "partner",
                "overall_finding_rating": "On Track",
                "monitoring_activity": "MA-1",
                "location": {"name": "Akkar"},
                "monitoring_activity_end_date": "2024-05-01",
            }
        ],
    )
    page(
        "tpm-visits",
        [
            {
                "id": 1,
                "source_id": 3,
                "source_partner_id": 1,
                "visit_reference_number": "TPM/1",
                "visit_status": "unicef_approved",
                "visit_start_date": "2024-04-01",
            }
        ],
    )
    page(
        "hact/aggregate", [{"id": 1, "year": 2024, "completed_spotcheck": 12, "microassessments_total": None}]
    )
    runs = run_all("action_points,pd_indicators,field_monitoring,tpm_visits,hact")
    assert {r.status for r in runs} == {SyncRun.Status.SUCCEEDED}

    point = dm.ActionPoint.objects.get()
    assert (point.partner, point.intervention, point.high_priority) == (p, pd, True)
    assert dm.PDIndicator.objects.filter(intervention=pd).count() == 2
    finding = dm.MonitoringFinding.objects.get()
    assert (finding.partner, finding.location_name) == (p, "Akkar")
    assert dm.TPMVisit.objects.get().partner == p
    hact = dm.HACTAggregate.objects.get()
    assert (hact.year, hact.completed_spotcheck, hact.microassessments_total) == (2024, 12, 0)


@responses.activate
def test_a_failed_dataset_does_not_stop_the_next_one():
    responses.get(f"{API}/funds/grants/", status=500)
    page("hact/aggregate", [{"id": 1, "year": 2024}])
    grants, hact = run_all("grants,hact")
    assert grants.status == SyncRun.Status.FAILED and "HTTP 500" in grants.error
    assert hact.status == SyncRun.Status.SUCCEEDED


def test_unknown_dataset_is_rejected():
    with pytest.raises(ValueError, match="unknown eTools Datamart datasets"):
        sync.sync_all(only=["nope"], client=make_client())


def test_command_without_credentials_fails_clearly(settings):
    settings.ETOOLS_USERNAME, settings.ETOOLS_PASSWORD = "", ""
    with pytest.raises(CommandError, match="ETOOLS_USERNAME and ETOOLS_PASSWORD"):
        call_command("sync_etools_datamart", "--only", "grants")
    assert not SyncRun.objects.exists()


def test_names_accepts_the_shapes_the_datamart_uses():
    assert sync.names([{"name": "A"}, {"name": "A"}, "B", None]) == ["A", "B"]
    assert sync.names("A, B") == ["A", "B"]
    assert sync.names({"name": "A"}) == ["A"]
    assert sync.names([{"pcode": "LB-1", "name": "x"}], "pcode", "p_code") == ["LB-1"]
    assert sync.names(None) == []


@pytest.fixture
def linked(db):
    p = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Government", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    return p, pd


@responses.activate
def test_fr_headers_tpm_activities_visits_and_workplan_link_to_the_pd(linked):
    p, pd = linked
    page(
        "funds/fundsreservationheader",
        [
            {
                "id": 1,
                "fr_number": "0400001",
                "pd_reference_number": "LEB/PD1",
                "total_amt": "1000",
                "actual_amt": "400",
                "outstanding_amt": "600",
            }
        ],
    )
    page(
        "tpm-activities",
        [
            {
                "id": 1,
                "source_id": 5,
                "visit_reference_number": "TPM/1",
                "task_reference_number": "TPM/1/1",
                "partner_vendor_number": "V1",
                "pd_ssfa_reference_number": "LEB/PD1",
                "date": "2024-03-01",
                "is_pv": True,
                "locations_data": [{"name": "Akkar"}, {"name": "Tripoli"}],
            }
        ],
    )
    page(
        "travel-activities",
        [
            {
                "id": 1,
                "travel_reference_number": "2024/12",
                "travel_type": "programmatic visit",
                "date": "2024-02-02",
                "source_partner_id": 1,
                "source_partnership_id": 11,
            }
        ],
    )
    page(
        "interventions-planned-visits",
        [
            {
                "id": 1,
                "pd_reference_number": "LEB/PD1",
                "year": 2024,
                "programmatic_q1": 1,
                "programmatic_q2": 2,
                "programmatic_q3": None,
                "partner_vendor_number": "V1",
            }
        ],
    )
    page(
        "interventions-activities",
        [
            {
                "id": 1,
                "pd_number": "LEB/PD1",
                "ll_name": "Output 1",
                "activity_code": "1.1",
                "activity": "Train teachers",
                "activity_unicef_cash": "5000",
                "activity_cso_cash": "500",
            }
        ],
    )
    runs = run_all("funds_reservation_headers,tpm_activities,staff_visits,planned_visits,pd_activities")
    assert {r.status for r in runs} == {SyncRun.Status.SUCCEEDED}
    assert pd.fr_headers.get().outstanding_amt == Decimal("600")
    activity = pd.tpm_activities.get()
    assert (activity.partner, activity.locations, activity.is_programmatic_visit) == (
        p,
        "Akkar, Tripoli",
        True,
    )
    visit = pd.programmatic_visits.get()
    assert visit.partner == p and visit.travel_type == "programmatic visit"
    planned = pd.planned_visits_by_year.get()
    assert (planned.partner, planned.total) == (p, 3)
    assert pd.workplan_activities.get().unicef_cash == Decimal("5000")


@responses.activate
def test_partner_reports_are_read_for_a_window_and_only_that_window_is_kept_and_marked(linked, settings):
    """A whole read of the window keeps the rows of the window it no longer returns, marked no longer sent
    (``test_datamart_unsent``); a row before the window is left alone."""
    p, pd = linked
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2
    old = dm.ReportedIndicator.objects.create(datamart_id=90, period_start="2001-01-01", indicator="old")
    gone = dm.ReportedIndicator.objects.create(datamart_id=91, period_start="2099-01-01", indicator="gone")
    start = f"{__import__('datetime').date.today().year - 1}-01-01"
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        match=[
            responses.matchers.query_param_matcher(
                {"page_size": "500", "country_name": "Lebanon", "reporting_period_start_date__gte": start}
            )
        ],
        json={
            "results": [
                {
                    "id": 1,
                    "partner_vendor_number": "V1",
                    "etools_intervention_id": "11",
                    "intervention_reference_number": "LEB/PD1",
                    "progress_report": "QPR1",
                    "report_number": "QPR1",
                    "report_type": "QPR",
                    "report_status": "Accepted",
                    "reporting_period_start_date": start,
                    "reporting_period_due_date": "2030-01-20",
                    "performance_indicator": "Children reached",
                    "total_cumulative_progress": "250",
                }
            ],
            "next": None,
        },
    )
    (run,) = run_all("partner_reports")
    assert run.status == SyncRun.Status.SUCCEEDED and run.details["removed"] == 0
    assert run.details["no_longer_sent_new"] == 1
    row = dm.ReportedIndicator.objects.get(datamart_id=1)
    assert (row.partner, row.intervention, str(row.due_date)) == (p, pd, "2030-01-20")
    old.refresh_from_db()
    gone.refresh_from_db()
    assert old.not_sent_since is None  # before the window: left alone
    assert gone.not_sent_since == run.started_at  # in the window, no longer returned: kept, marked


@responses.activate
def test_partner_reports_of_another_country_are_never_written_and_old_ones_go(linked, settings):
    """A ``country_name`` filter the Datamart ignored must not let another country's report rows in: they
    are counted and never written, and those an earlier run wrote are removed by the complete read. A row
    whose stored record names no other country, and whose Datamart id the read returns naming another, is
    no longer sent: kept and marked, never removed as another country's."""
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2
    start = f"{__import__('datetime').date.today().year - 1}-01-01"
    earlier = dm.ReportedIndicator.objects.create(
        datamart_id=93, period_start=start, indicator="written before"
    )
    common = {
        "partner_vendor_number": "V1",
        "etools_intervention_id": "11",
        "intervention_reference_number": "LEB/PD1",
        "report_status": "Accepted",
        "reporting_period_start_date": start,
        "performance_indicator": "Children reached",
    }
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        json={
            "results": [
                {**common, "id": 1, "country_name": "Lebanon"},
                {**common, "id": 2, "country_name": " LEBANON "},
                {**common, "id": 3},  # no country named: the filter chose it
                {**common, "id": 93, "country_name": "Syria"},
                {**common, "id": 94, "country_name": "Jordan"},
            ],
            "next": None,
        },
    )
    # before the window: kept as ever when it is this country's, removed when it names another
    old_ours = dm.ReportedIndicator.objects.create(
        datamart_id=97, period_start="2001-01-01", data={"country_name": "lebanon"}
    )
    old_other = dm.ReportedIndicator.objects.create(
        datamart_id=98, period_start="2001-01-01", data={"country_name": "Syria"}
    )
    (run,) = run_all("partner_reports")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["other_country_skipped"] == 2 and run.details["removed"] == 0
    assert run.details["other_country_removed"] == 1 and run.details["no_longer_sent_new"] == 1
    assert run.rows_in == 3
    assert set(dm.ReportedIndicator.objects.values_list("datamart_id", flat=True)) == {1, 2, 3, 93, 97}
    assert not dm.ReportedIndicator.objects.filter(pk=old_other.pk).exists()
    earlier.refresh_from_db()
    assert earlier.not_sent_since == run.started_at  # kept and marked, never deleted as another country's
    assert dm.ReportedIndicator.objects.filter(pk=old_ours.pk).exists()


@responses.activate
def test_partner_reports_scope_and_request_use_one_since(linked, monkeypatch):
    """The date the partner reports are read from is computed once per run: the request asks from it and the
    rows the read replaces are those from it, even when a second computation would give another date (a PD
    changing status during the read, midnight passing)."""
    import datetime

    dates = iter([datetime.date(2025, 1, 1), datetime.date(2024, 1, 1)])
    monkeypatch.setattr(sync.DATASETS["partner_reports"][1], "since", lambda: next(dates))
    before = dm.ReportedIndicator.objects.create(
        datamart_id=90, period_start="2024-06-01", indicator="earlier"
    )
    gone = dm.ReportedIndicator.objects.create(datamart_id=91, period_start="2025-06-01", indicator="gone")
    common = {"partner_vendor_number": "V1", "intervention_reference_number": "LEB/PD1",
              "reporting_period_start_date": "2025-02-01", "country_name": "Lebanon"}  # fmt: skip
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        match=[
            responses.matchers.query_param_matcher(
                {
                    "page_size": "500",
                    "country_name": "Lebanon",
                    "reporting_period_start_date__gte": "2025-01-01",
                }
            )
        ],
        json={"count": 1, "next": None, "results": [{**common, "id": 1}]},
    )
    (run,) = run_all("partner_reports")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert (run.details["read_from"], run.details["held_before"], run.details["no_longer_sent_new"]) == (
        "2025-01-01",
        1,
        1,
    )
    before.refresh_from_db()
    gone.refresh_from_db()
    assert before.not_sent_since is None  # before the window asked: left alone
    assert gone.not_sent_since == run.started_at


def _reports(start: str, n: int) -> list[dict]:
    """``n`` partner report rows of the linked PD, all of this country and of the window."""
    common = {"partner_vendor_number": "V1", "etools_intervention_id": "11", "intervention_reference_number": "LEB/PD1",
              "report_status": "Accepted", "reporting_period_start_date": start, "country_name": "Lebanon"}  # fmt: skip
    return [{**common, "id": pk, "performance_indicator": f"Indicator {pk}"} for pk in range(1, n + 1)]


@responses.activate
def test_a_round_count_removes_nothing(linked, settings, monkeypatch):
    """A count of 5,000 or more that is a multiple of 1,000 (here 20 and 10) reads as a limit of the
    Datamart's answer, not what it holds: the rows held that the read did not return stay, and the run ends
    PARTIAL saying so, even when every record announced was read. This answer ignores every filter, so it
    cannot be read in windows either (``windows_unproven``). A count that is not round reads as ever."""
    monkeypatch.setattr(sync, "CAP_FLOOR", 20)
    monkeypatch.setattr(sync, "CAP_ROUND", 10)
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2
    start = f"{__import__('datetime').date.today().year - 1}-01-01"
    held = dm.ReportedIndicator.objects.create(datamart_id=90, period_start=start, indicator="not returned")
    url = f"{BASE}/api/latest/prp/datareport/"
    responses.get(url, json={"count": 20, "next": None, "results": _reports(start, 20)})
    (run,) = run_all("partner_reports")
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 0
    details = run.details
    assert (details["removal_skipped"], details["cap_suspected"], details["removed"]) == (
        True,
        "windows_unproven",
        0,
    )
    assert (details["read"], details["datamart_count"]) == (20, 20)
    assert dm.ReportedIndicator.objects.filter(pk=held.pk).exists()

    responses.reset()
    responses.get(url, json={"count": 21, "next": None, "results": _reports(start, 21)})
    (run,) = run_all("partner_reports")
    assert run.status == SyncRun.Status.SUCCEEDED and run.details["no_longer_sent_new"] == 1
    held.refresh_from_db()
    assert "cap_suspected" not in run.details and held.not_sent_since == run.started_at  # kept, marked


@responses.activate
def test_a_round_count_removes_no_record_kept_whole(settings, monkeypatch):
    monkeypatch.setattr(sync, "CAP_FLOOR", 20)
    monkeypatch.setattr(sync, "CAP_ROUND", 10)
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    dm.DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="old")
    records = [{"id": pk, "business_area": "2490", "pd_reference_number": "LEB/PD1"} for pk in range(1, 21)]
    responses.get(
        f"{BASE}/api/latest/prp/indicator-report-v2/", json={"count": 20, "next": None, "results": records}
    )
    (run,) = run_all("prp_indicator_reports")
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 0
    details = run.details
    assert (details["removal_skipped"], details["cap_suspected"], details["documents_removed"]) == (
        True,
        "windows_unproven",
        0,
    )
    assert dm.DatamartDocument.objects.filter(dataset="prp_indicator_reports", record_key="old").exists()
    monkeypatch.undo()  # the real constants: 5,000 and 1,000
    assert [sync.capped(n) for n in (None, 4_000, 5_000, 9_970, 10_000, 12_000)] == [
        False,
        False,
        True,
        False,
        True,
        True,
    ]


@responses.activate
def test_this_countrys_name_with_odd_spaces_or_case_is_never_another_country(linked, settings):
    """The sync compares names one way (case and every kind of space aside): a row it writes is never
    deleted as another country's in the same run."""
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2
    start = f"{__import__('datetime').date.today().year - 1}-01-01"
    common = {"report_status": "Accepted", "reporting_period_start_date": start, "performance_indicator": "X"}
    old_other = dm.ReportedIndicator.objects.create(datamart_id=60, data={"country_name": "Syria"})
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        json={
            "results": [
                {**common, "id": 61, "country_name": "Lebanon\u00a0"},
                {**common, "id": 62, "country_name": "\u00a0"},
                {**common, "id": 63, "country_name": "LEBANON"},
            ],
            "next": None,
        },
    )
    (run,) = run_all("partner_reports")
    assert set(dm.ReportedIndicator.objects.values_list("datamart_id", flat=True)) == {61, 62, 63}
    assert run.details["other_country_removed"] == 1 and "other_country_skipped" not in run.details
    assert not dm.ReportedIndicator.objects.filter(pk=old_other.pk).exists()


@responses.activate
def test_partners_and_programme_documents_of_another_country_are_never_written():
    page("partners", [partner(1, country_name="Lebanon"), partner(2, country_name="Syria")])
    page("interventions", [intervention(11), intervention(12, country_name="Syria")])
    runs = run_all("partners,interventions")
    assert [r.details.get("other_country_skipped") for r in runs] == [1, 1]
    assert set(PartnerOrganization.objects.values_list("etl_id", flat=True)) == {"1"}
    assert set(PCA.objects.values_list("etl_id", flat=True)) == {"11"}
    assert not dm.DatamartDocument.objects.filter(dataset="partners", record_key="902").exists()


@responses.activate
def test_an_answer_of_another_country_only_never_empties_the_table(linked):
    kept = dm.ReportedIndicator.objects.create(datamart_id=95, indicator="kept")
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        json={"results": [{"id": 96, "country_name": "Syria", "report_status": "Accepted"}], "next": None},
    )
    old_other = dm.ReportedIndicator.objects.create(datamart_id=99, data={"country_name": "Syria"})
    (run,) = run_all("partner_reports")
    assert run.details["empty_response"] and run.details["other_country_skipped"] == 1
    assert "other_country_removed" not in run.details  # a wrong country name never empties a table
    assert dm.ReportedIndicator.objects.filter(pk__in=[kept.pk, old_other.pk]).count() == 2


@responses.activate
def test_partner_reports_of_running_pds_are_read_from_their_start_with_their_cp_output(linked, settings):
    """Reached adds up every counted report of a PD: the window reaches back to the start of every
    running programme document; the indicator report id and the CP output get columns, and each PD
    indicator its CP output (by its eTools id, else its title)."""
    import datetime

    p, pd = linked
    PCA.objects.filter(pk=pd.pk).update(
        status="active", start=datetime.date(2019, 7, 1), end=datetime.date(2030, 12, 31)
    )
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2
    by_id = dm.PDIndicator.objects.create(
        datamart_id=5, source_id=77, intervention=pd, title="Children reached"
    )
    by_title = dm.PDIndicator.objects.create(datamart_id=6, intervention=pd, title="Teachers trained")
    common = {
        "partner_vendor_number": "V1",
        "etools_intervention_id": "11",
        "intervention_reference_number": "LEB/PD1",
        "progress_report": "QPR1",
        "report_number": "QPR1",
        "report_type": "QPR",
        "report_status": "Accepted",
        "reporting_period_start_date": "2019-07-01",
    }
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        match=[
            responses.matchers.query_param_matcher(
                {
                    "page_size": "500",
                    "country_name": "Lebanon",
                    "reporting_period_start_date__gte": "2019-07-01",
                }
            )
        ],
        json={
            "results": [
                {
                    **common,
                    "id": 1,
                    "performance_indicator": "Children reached",
                    "etools_cp_output_indicators_id": "77",
                    "indicator_report": 501,
                    "cp_output": "Output 2.1 Children access protection services",
                    "etools_cp_output_id": 3301,
                    "disaggregation": {"()": {"v": 10, "d": 1, "c": 10}},
                },
                {
                    **common,
                    "id": 2,
                    "performance_indicator": "Teachers trained",
                    "indicator_report": "502",
                    "cp_output": {"name": "Output 3.1 Teachers"},
                },
            ],
            "next": None,
        },
    )
    (run,) = run_all("partner_reports")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["read_from"] == "2019-07-01" and run.details["cp_outputs"] == 2
    row = dm.ReportedIndicator.objects.get(datamart_id=1)
    assert (row.indicator_report, row.cp_output, row.etools_cp_output_id) == (
        "501",
        "Output 2.1 Children access protection services",
        "3301",
    )
    assert row.disaggregation == {"()": {"v": 10, "d": 1, "c": 10}}
    assert dm.ReportedIndicator.objects.get(datamart_id=2).cp_output == "Output 3.1 Teachers"
    by_id.refresh_from_db()
    by_title.refresh_from_db()
    assert (by_id.cp_output, by_title.cp_output) == (
        "Output 2.1 Children access protection services",
        "Output 3.1 Teachers",
    )
    # the setting off: the last years only
    settings.ETOOLS_DATAMART_REPORTING_FROM_PD_START = False
    assert sync._reporting_since() == datetime.date(datetime.date.today().year - 1, 1, 1)


def test_the_clean_up_fills_the_new_columns_from_the_records(linked):
    """The eTools update clean-up fills the columns of 0009 from the records already synced, the way
    the sync reads them (a nested record by its name); a second run writes nothing, and a row the
    sync already wrote is left as the sync wrote it."""
    from neurodb.datamart import backfill

    _, pd = linked
    indicator = dm.PDIndicator.objects.create(datamart_id=5, source_id=77, intervention=pd, title="Children")
    teachers = dm.PDIndicator.objects.create(datamart_id=6, intervention=pd, title="Teachers trained")
    row = dm.ReportedIndicator.objects.create(
        datamart_id=1,
        intervention=pd,
        etools_indicator_id="77",
        data={"indicator_report": 501, "cp_output": "Output 2.1", "etools_cp_output_id": 3301},
    )
    nested = dm.ReportedIndicator.objects.create(
        datamart_id=2,
        intervention=pd,
        indicator="Teachers trained",
        data={"indicator_report": "502", "cp_output": {"id": 9, "name": "Output 3.1 Teachers"}},
    )
    untouched = dm.ReportedIndicator.objects.create(datamart_id=3, intervention=pd, data={"other": 1})
    synced = dm.ReportedIndicator.objects.create(
        datamart_id=4, intervention=pd, indicator_report="601", cp_output="Output 9 (as synced)",
        data={"indicator_report": 601, "cp_output": "Output 9"},
    )  # fmt: skip

    first = backfill.fill_reached_columns()
    assert first["report_rows_filled"] == 2
    assert backfill.fill_reached_columns()["report_rows_filled"] == 0  # nothing left: nothing written
    synced.refresh_from_db()
    assert (synced.indicator_report, synced.cp_output) == ("601", "Output 9 (as synced)")
    for record in (row, nested, indicator, teachers):
        record.refresh_from_db()
    assert (row.indicator_report, row.cp_output, row.etools_cp_output_id) == ("501", "Output 2.1", "3301")
    assert (nested.indicator_report, nested.cp_output) == ("502", "Output 3.1 Teachers")
    assert (indicator.cp_output, teachers.cp_output) == ("Output 2.1", "Output 3.1 Teachers")
    assert dm.ReportedIndicator.objects.get(pk=untouched.pk).cp_output == ""


@responses.activate
def test_hact_history_counts_arrive_as_strings(linked):
    p, _ = linked
    page(
        "hact/history",
        [
            {
                "id": 1,
                "year": 2024,
                "partner_source_id": 1,
                "vendor_number": "V1",
                "risk_rating": "Low",
                "ct_jan_dec": "12000.50",
                "pv_mr": "2",
                "pv_completed_year": "1.0",
                "sc_mr": "",
                "audits_outstanding_findings": None,
            }
        ],
    )
    (run,) = run_all("hact_history")
    assert run.status == SyncRun.Status.SUCCEEDED
    year = dm.PartnerHACTYear.objects.get()
    assert (year.partner, year.cash_transfers, year.pv_required, year.pv_completed, year.sc_required) == (
        p,
        Decimal("12000.50"),
        2,
        1,
        None,
    )


@responses.activate
def test_audit_details_enrich_engagements_and_findings_link_to_them(linked):
    p, _ = linked
    engagement = dm.AuditEngagement.objects.create(
        datamart_id=40, reference_number="LEB/2024/AUD/01", engagement_type="audit", partner=p
    )
    page(
        "audit/results",
        [
            {
                "id": 1,
                "reference_number": "LEB/2024/AUD/01",
                "risk_rating": "High",
                "count_high_risk_findings": 2,
                "count_key_control_weaknesses": 4,
                "pending_unsupported_amount": "300",
                "audit_opinion": "qualified",
            },
            {"id": 2, "reference_number": "UNKNOWN"},
        ],
    )
    page("audit/audit", [])
    page("audit/spot-check-findings", [])
    page("audit/micro-assessment", [])
    page("audit/special-audit", [])
    page(
        "audit/financial-findings-all",
        [
            {
                "id": 7,
                "reference_number": "LEB/2024/AUD/01",
                "finding_number": 1,
                "title": "Ineligible expenditure",
                "amount": "250.75",
                "partner_vendor_number": "V1",
            }
        ],
    )
    runs = run_all("audit_results,audits,spot_checks,micro_assessments,special_audits,audit_findings")
    assert [r.target for r in runs][-1] == "audit_findings"
    assert runs[0].details["not_linked"] == {"engagement": 1} and runs[0].details["documents"] == 2
    engagement.refresh_from_db()
    assert (engagement.risk_rating, engagement.high_priority_findings, engagement.key_control_weaknesses) == (
        "High",
        2,
        4,
    )
    assert engagement.pending_unsupported_amount == Decimal("300") and engagement.audit_opinion == "qualified"
    assert engagement.details["audit_results"]["risk_rating"] == "High"
    finding = dm.AuditFinding.objects.get()
    assert (finding.engagement, finding.partner, finding.amount) == (engagement, p, Decimal("250.75"))


def test_array_elements_are_cut_to_the_column_width():
    from neurodb.integrations.etools.fields import coerce

    field = PCA._meta.get_field("cp_outputs")  # varchar(200)[]
    long = "x" * 250
    assert coerce(field, [long, "ok", {"name": "y"}]) == [long[:200], "ok", '{"name": "y"}']


@responses.activate
def test_failed_records_are_explained_in_the_run_details(linked):
    p, pd = linked
    page(
        "interventions",
        [
            intervention(11, cp_outputs_data=[{"name": "o" * 300}]),  # would have failed before the array fix
            {"id": 700, "number": "no-source-id"},
            {"id": 701, "number": "no-source-id-either"},
            intervention(12, planned_programmatic_visits="four"),
        ],
    )
    (run,) = run_all("interventions")
    assert run.status == SyncRun.Status.PARTIAL and (run.rows_written, run.rows_failed) == (1, 3)
    errors = sorted(run.details["errors"], key=lambda e: -e["count"])
    assert errors[0]["count"] == 2 and errors[0]["error"].startswith(
        "ValueError: intervention without source_id"
    )
    assert errors[0]["examples"] == ["700", "701"]
    assert errors[1]["count"] == 1 and "four" in errors[1]["error"] and errors[1]["examples"] == ["12"]
    assert PCA.objects.get(etl_id="11").cp_outputs == ["o" * 200]


def test_a_lagging_id_sequence_is_raised_before_inserting():
    from django.db import connection

    PCA.objects.create(id=5000, etl_id="x", title="old row restored with its id")
    with connection.cursor() as cursor:  # the restore left the sequence behind the data
        cursor.execute("SELECT setval(pg_get_serial_sequence('etools_pca', 'id'), 1)")
    done = sync.align_sequences()
    assert done["etools_pca"] == {"was": 1, "now": 5000}
    PCA.objects.create(etl_id="y", title="a new row inserts again")
    assert sync.align_sequences() == {}  # nothing to do once the sequences are ahead


@responses.activate
def test_an_agreement_or_location_failure_does_not_lose_the_programme_document(linked, monkeypatch):
    page("interventions", [intervention(11)])

    def boom(*args, **kwargs):
        raise RuntimeError("cannot write agreements")

    monkeypatch.setattr(sync, "_agreement", boom)
    monkeypatch.setattr(sync.Location.objects, "filter", boom)
    (run,) = run_all("interventions")
    assert run.status == SyncRun.Status.SUCCEEDED and run.rows_written == 1
    assert run.details["not_linked"] == {"agreement": 1, "locations": 1}
    assert PCA.objects.get(etl_id="11").title == "Programme 11"


@responses.activate
def test_pd_indicators_and_reports_carry_tags_and_reporting_fields(linked):
    p, pd = linked
    page(
        "pd-indicators",
        [
            {
                "id": 1,
                "source_id": 77,
                "title": "# of Syrian girls with disabilities reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Akkar",
                "location_level": 1,
                "means_of_verification": "Attendance sheets",
            }
        ],
    )
    responses.get(
        f"{BASE}/api/latest/prp/datareport/",
        json={
            "results": [
                {
                    "id": 5,
                    "partner_vendor_number": "V1",
                    "intervention_reference_number": "LEB/PD1",
                    "progress_report": "PR-1",
                    "report_type": "HR",
                    "performance_indicator": "# of Syrian girls with disabilities reached",
                    "reporting_period_start_date": f"{__import__('datetime').date.today().year}-01-01",
                    "high_frequency": "True",
                    "calculation_method_across_location": "max",
                    "etools_pd_result_id": "88",
                    "etools_cp_output_indicators_id": "77",
                    "disaggregation": {"()": {"v": 10, "d": 1, "c": 10}},
                    "pd_output_narrative_assessment": "Going well",
                    "previous_location_progress": "5",
                    "admin_level": 1,
                }
            ],
            "next": None,
        },
    )
    runs = run_all("pd_indicators,partner_reports")
    assert {r.status for r in runs} == {SyncRun.Status.SUCCEEDED}
    indicator = dm.PDIndicator.objects.get()
    assert (indicator.tag_gender, indicator.tag_nationality, indicator.tag_disability) == (
        "Girls",
        "Syrian",
        "Yes",
    )
    assert (indicator.location_level, indicator.means_of_verification) == (1, "Attendance sheets")
    report = dm.ReportedIndicator.objects.get()
    assert report.high_frequency is True and report.calculation_across_locations == "max"
    assert (report.etools_indicator_id, report.etools_pd_result_id, report.previous_location_progress) == (
        "77",
        "88",
        "5",
    )
    assert (
        report.disaggregation == {"()": {"v": 10, "d": 1, "c": 10}}
        and report.narrative_assessment == "Going well"
    )
    assert (report.tag_gender, report.tag_disability, report.admin_level) == ("Girls", "Yes", 1)


def location(source_id: int, name: str, p_code: str, level: int, parent: int | None = None, **extra) -> dict:
    return {
        "id": 3000 + source_id,
        "source_id": source_id,
        "name": name,
        "p_code": p_code,
        "admin_level": level,
        "admin_level_name": {0: "Country", 1: "Governorate", 2: "District", 3: "Cadastral"}.get(
            level, f"Level {level}"
        ),
        "parent": 3000 + parent if parent else None,
        "is_active": True,
        "country_name": "Lebanon",
        **extra,
    }


@responses.activate
def test_locations_become_the_gazetteer_and_every_record_links_to_them_by_id_or_pcode(linked):
    from neurodb.geo.models import Location, LocationType

    p, pd = linked
    page(
        "locations",
        [
            location(1, "Lebanon", "LB", 0, latitude=33.9, longitude=35.5),
            location(2, "Akkar", "LB1", 1, parent=1, latitude=34.55, longitude=36.1),
            location(3, "Akkar District", "LB11", 2, parent=2),  # no coordinates of its own
            location(4, "Halba", "LB1101", 3, parent=3, latitude=34.54, longitude=36.08),
        ],
    )
    page(
        "location-sites",
        [
            {
                "id": 1,
                "source_id": 51,
                "name": "Halba school",
                "p_code": "SITE-1",
                "point": "POINT (36.081 34.541)",
                "parent": {"id": 3004, "source_id": 4, "p_code": "LB1101", "name": "Halba"},
                "country_name": "Lebanon",
            }
        ],
    )
    page(
        "pd-indicators",
        [
            {
                "id": 1,
                "source_id": 77,
                "title": "Children reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Halba (typo)",
                "location_pcode": "LB1101",
                "location_source_id": 4,
                "target_numerator": "100",
            },
            {
                "id": 2,
                "source_id": 77,
                "title": "Children reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Nowhere",
                "location_pcode": "LB9999",
                "target_numerator": "100",
            },
        ],
    )
    page(
        "tpm-activities",
        [
            {
                "id": 1,
                "source_id": 5,
                "visit_reference_number": "TPM/1",
                "task_reference_number": "TPM/1/1",
                "pd_ssfa_reference_number": "LEB/PD1",
                "partner_vendor_number": "V1",
                "locations": "Halba, Akkar",
                "locations_data": [{"id": 4, "name": "Halba", "p_code": "LB1101"}, {"p_code": "LB1"}],
                "date": "2024-05-01",
            }
        ],
    )
    page(
        "fm-ontrack",
        [
            {
                "id": 1,
                "source_id": 9,
                "vendor_number": "V1",
                "entity": "Partner 1",
                "overall_finding_rating": "Off Track",
                "monitoring_activity": "MA-1",
                "monitoring_activity_id": 400,
                "location": {"source_id": 4, "name": "Halba", "p_code": "LB1101"},
                "site": "Halba School",
            }
        ],
    )
    page(
        "actionpoints",
        [
            {
                "id": 1,
                "source_id": 501,
                "reference_number": "LEB/2024/1/APD",
                "status": "open",
                "partner_source_id": 1,
                "intervention_source_id": 11,
                "location_name": "Halba",
                "location_pcode": "LB1101",
                "location_source_id": 4,
                "location_level": 3,
                "tpm_activity_source_id": 5,
                "related_module": "tpm",
                "related_module_id": 5,
            }
        ],
    )
    page(
        "travel-activities",
        [
            {
                "id": 1,
                "travel_reference_number": "2024/1",
                "travel_type": "Programmatic Visit",
                "date": "2024-05-02",
                "source_partner_id": 1,
                "partnership_number": "LEB/PD1",
                "location": 4,
                "location_pcode": "LB1101",
                "location_name": "Halba",
            }
        ],
    )
    runs = run_all(
        "locations,location_sites,pd_indicators,tpm_activities,field_monitoring,action_points,staff_visits"
    )
    assert {r.status for r in runs} == {SyncRun.Status.SUCCEEDED}, [(r.target, r.error) for r in runs]

    halba = Location.objects.get(pk=4)
    assert (halba.p_code, halba.latitude, halba.type.admin_level, halba.parent_id) == ("LB1101", 34.54, 3, 3)
    assert Location.objects.get(pk=3).parent_id == 2 and LocationType.objects.count() == 4
    assert runs[0].details["parents_linked"] == 3
    site = dm.MonitoringSite.objects.get()
    assert (site.parent, site.latitude, site.longitude) == (halba, 34.541, 36.081)

    linked_row, unlinked_row = dm.PDIndicator.objects.order_by("datamart_id")
    assert linked_row.location == halba and unlinked_row.location is None  # a name never links
    assert runs[2].details["not_linked"]["location"] == 1
    activity = dm.TPMActivity.objects.get()
    assert set(activity.location_links.values_list("pk", flat=True)) == {4, 2}
    assert activity.location_pcodes == ["LB1101", "LB1"]
    finding = dm.MonitoringFinding.objects.get()
    assert (finding.location, finding.monitoring_site, finding.monitoring_activity_id) == (halba, site, 400)
    point = dm.ActionPoint.objects.get()
    assert (point.location, point.tpm_activity, point.intervention, point.location_level) == (
        halba,
        activity,
        pd,
        3,
    )
    assert dm.ProgrammaticVisit.objects.get().location == halba


@responses.activate
def test_records_synced_before_the_gazetteer_are_linked_by_the_locations_run(linked):
    from neurodb.geo.models import Location

    p, pd = linked
    PCA.objects.filter(pk=pd.pk).update(location_p_codes=["LB1101"])
    early = dm.PDIndicator.objects.create(
        datamart_id=1, source_id=77, intervention=pd, title="x", location_pcode="lb1101", location_source_id=4
    )
    report = dm.ReportedIndicator.objects.create(
        datamart_id=2, intervention=pd, indicator="x", p_code="LB1101"
    )
    point = dm.ActionPoint.objects.create(datamart_id=3, intervention=pd, location_pcode="LB1101")
    page("locations", [location(4, "Halba", "LB1101", 3, latitude=34.54, longitude=36.08)])
    (run,) = run_all("locations")
    halba = Location.objects.get(pk=4)
    for row in (early, report, point):
        row.refresh_from_db()
    assert (early.location, report.location_ref, point.location) == (halba, halba, halba)
    assert list(pd.locations.all()) == [halba]
    assert run.details["relinked"] == {
        "pdindicator": 1,
        "reportedindicator": 1,
        "actionpoint": 1,
        "programme_documents": 1,
    }


@responses.activate
def test_locations_reuse_the_v2_location_types_that_already_carry_etools_names(linked):
    """Production keeps v2's ``locations_locationtype`` rows (unique by name, mostly without an
    admin level): the sync must adopt them instead of failing on the unique name."""
    from neurodb.geo.models import Location, LocationType

    cadaster = LocationType.objects.create(name="Cadaster", admin_level=None)
    school = LocationType.objects.create(name="School", admin_level=9)
    page(
        "locations",
        [
            location(1, "Lebanon", "LB", 0),
            location(2, "Akkar", "LB1", 1, parent=1, admin_level_name="Governorate"),
            location(3, "Halba", "LB1101", 3, parent=2, admin_level_name="cadaster"),
            location(4, "Halba School", "LB1101-S1", 5, parent=3, admin_level_name="School"),
        ],
    )
    (run,) = run_all("locations")
    assert (run.status, run.rows_written, run.rows_failed) == (SyncRun.Status.SUCCEEDED, 4, 0), run.details

    cadaster.refresh_from_db()
    school.refresh_from_db()
    assert (cadaster.admin_level, school.admin_level) == (3, 9)
    assert Location.objects.get(pk=3).type == cadaster
    assert Location.objects.get(pk=4).type == school
    assert LocationType.objects.count() == 4
    assert LocationType.objects.get(name="Governorate").admin_level == 1


@responses.activate
def test_partners_with_a_duplicate_vendor_number_and_blank_name_are_still_written():
    """The v2 table is unique on (name, vendor_number); eTools holds hidden duplicates of a vendor
    with an empty name. Production rejected them; they are named from the eTools id instead."""
    PartnerOrganization.objects.create(
        etl_id="9", name="", vendor_number="2300043124", partner_type="Government"
    )
    page(
        "partners",
        [
            partner(371, name="", short_name="", vendor_number="2300043124", hidden=True),
            partner(372, name="", short_name="", vendor_number="2300043124", hidden=True),
            partner(373, name="Ministry", vendor_number="2300043124"),
            partner(374, name="Ministry", vendor_number="2300043124"),
        ],
    )
    (run,) = run_all("partners")
    assert (run.status, run.rows_written, run.rows_failed) == (SyncRun.Status.SUCCEEDED, 4, 0), run.details
    names = dict(PartnerOrganization.objects.values_list("etl_id", "name"))
    assert names["371"] == "Partner 371" and names["372"] == "Partner 372"
    assert names["373"] == "Ministry" and names["374"] == "Ministry (eTools 374)"
    assert names["9"] == ""
    (again,) = run_all("partners")
    assert again.rows_failed == 0
    assert dict(PartnerOrganization.objects.values_list("etl_id", "name")) == names


@responses.activate
def test_the_locations_job_reads_the_datamart_by_default(db):
    from neurodb.geo.models import Location
    from neurodb.integrations.management.commands.sync_locations import sync_from_datamart

    page(
        "locations",
        [
            location(1, "Lebanon", "LB", 0),
            location(2, "Akkar", "LB1", 1, parent=1),
            location(3, "Halba", "LB11", 2, parent=2),
        ],
    )
    (run,) = sync_from_datamart("test", client=make_client())
    assert run.job == SyncRun.Job.LOCATIONS and run.status == SyncRun.Status.SUCCEEDED, run.error
    akkar = Location.objects.get(p_code="LB1")
    assert akkar.type.admin_level == 1
    assert Location.objects.get(p_code="LB11").parent_id == akkar.pk
