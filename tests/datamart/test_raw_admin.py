"""Raw eTools data (Admin → Data and sync; datamart.raw_admin): who opens it, what the first page lists, what each
sync received and kept (datamart.runfacts), the filters, a record's page, the CSV and its limits, and the queries
each page makes. Synthetic people only."""

from __future__ import annotations

import csv
import datetime
import io
import re

import psycopg
import pytest
from django.contrib.auth.models import Group
from django.core.signals import request_finished
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, raw, reporting, runfacts
from neurodb.datamart import models as dm
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

NAME, EMAIL = "Person One", "person.one@example.org"
INDEX = reverse("admin:datamart_raw")


def _user(username: str, *, staff: bool, groups: tuple[str, ...] = (VIEWER,)) -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="pass-123456-x", is_staff=staff
    )
    user.groups.set(Group.objects.filter(name__in=groups))
    return user


@pytest.fixture
def validator(roles):
    return _user("validator", staff=True, groups=(VIEWER, VALIDATORS))


@pytest.fixture
def client_validator(client, validator):
    client.force_login(validator)
    return client


def _run(target: str, *, status=SyncRun.Status.SUCCEEDED, ago: int = 1, **fields) -> SyncRun:
    now = timezone.now()
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target=target,
        status=status,
        started_at=now - datetime.timedelta(hours=ago),
        finished_at=now + datetime.timedelta(hours=1)
        if ago == 1
        else now - datetime.timedelta(hours=ago - 1),
        **fields,
    )


@pytest.fixture
def world(db):
    """Partner report rows of a running, an ended and a draft programme document, of one NeuroDB does not hold
    and of none; a partner that names a person nowhere but in the rows' payloads; their last sync."""
    partner = PartnerOrganization.objects.create(etl_id="1", name="Amel Association", vendor_number="V1")
    running = PCA.objects.create(
        etl_id="11",
        partner=partner,
        number="LEB/PD1",
        title="A",
        status="active",
        section_names=["Education"],
    )
    ended = PCA.objects.create(etl_id="12", partner=partner, number="LEB/PD2", title="B", status="ended")
    draft = PCA.objects.create(etl_id="13", partner=partner, number="LEB/PD3", title="C", status="draft")
    run = _run("partner_reports", rows_in=6, details={"datamart_count": 10, "other_country_skipped": 4})
    rows = []
    for pk, (pd, reference, section, kind) in enumerate(
        (
            (running, "LEB/PD1", "Education", "QPR"),
            (running, "LEB/PD1", "", "HR"),
            (ended, "LEB/PD2", "Health", "QPR"),
            (draft, "LEB/PD3", "Health", "QPR"),
            (None, "LEB/PD9", "Education", "QPR"),
            (None, "", "", "QPR"),
        ),
        start=1,
    ):
        rows.append(
            dm.ReportedIndicator(
                datamart_id=pk,
                partner=partner if pd else None,
                intervention=pd,
                pd_reference_number=reference,
                progress_report=f"PR-{pk}",
                indicator_report=f"IR-{pk}",
                report_number=f"{kind}1",
                report_type=kind,
                report_status="Accepted",
                period_start=datetime.date(2026, 1, 1),
                period_end=datetime.date(2026, 3, 31),
                section=section,
                indicator="# of children reached",
                achievement_in_period="12",
                submitted_by=NAME,
                data={
                    "id": pk,
                    "submitted_by": NAME,
                    "submitter_email": EMAIL,
                    "narrative_assessment": f"Reached as planned, says {NAME}",
                    "indicator_type": "quantity",
                    "performance_indicator": '=HYPERLINK("https://example.org/x","open")',
                    "total_cumulative_progress": "3000000",
                    "report_status": "Accepted",
                },
            )
        )
    dm.ReportedIndicator.objects.bulk_create(rows)
    return {"partner": partner, "running": running, "ended": ended, "draft": draft, "run": run}


# ------------------------------------------------------------------------------------------------- access
def test_only_validators_with_staff_status_open_raw_etools_data(client, roles, admin_user, world):
    pages = (INDEX, f"{INDEX}partner_reports/", f"{INDEX}partner_reports/rows.csv")
    for url in pages:
        assert client.get(url).status_code == 302  # anonymous: the admin's sign-in
    client.force_login(_user("plain-validator", staff=False, groups=(VIEWER, VALIDATORS)))
    for url in pages:
        assert client.get(url).status_code == 302  # the admin asks for staff status
    for user in (_user("staff-viewer", staff=True), admin_user):  # an Administrator not ticked: no
        client.force_login(user)
        for url in pages:
            assert client.get(url).status_code == 404, (user, url)
    for user in (_user("staff-validator", staff=True, groups=(VIEWER, VALIDATORS)), _superuser()):
        client.force_login(user)
        for url in pages:
            response = client.get(url)
            assert response.status_code == 200, (user, url)
            if response.streaming:
                b"".join(response.streaming_content)  # read whole: its download slot is freed
    assert client.get(f"{INDEX}no_such_source/").status_code == 404


def _superuser() -> User:
    return User.objects.create_superuser(
        username="root", email="root@example.org", password="root-pass-123456"
    )


def test_the_sidebar_quick_action_and_data_health_link_are_for_validators(
    client, roles, admin_user, validator
):
    client.force_login(validator)
    html = client.get(reverse("admin:index")).content.decode()
    assert f'href="{INDEX}"' in html and "Raw eTools data" in html
    assert reverse("admin:datamart_reporting_checks") in html
    health = client.get(reverse("reports:data_health")).content.decode()
    assert 'id="raw-data-link"' in health

    client.force_login(admin_user)
    html = client.get(reverse("admin:index")).content.decode()
    assert f'href="{INDEX}"' not in html and reverse("admin:datamart_reporting_checks") in html
    assert 'id="raw-data-link"' not in client.get(reverse("reports:data_health")).content.decode()


def test_a_validator_reads_the_reporting_checks_and_cannot_run_them(client_validator, monkeypatch):
    from neurodb.datamart import checks

    monkeypatch.setattr(checks, "run_all", lambda: [])
    page = client_validator.get(reverse("admin:datamart_reporting_checks"))
    assert page.status_code == 200 and "Run the checks now" not in page.content.decode()
    assert client_validator.post(reverse("admin:datamart_reporting_checks")).status_code == 403


# ------------------------------------------------------------------------------------------- the first page
def test_the_first_page_lists_every_source_once(client_validator, world):
    html = client_validator.get(INDEX).content.decode()
    for name in ENTITY_SYNCS:
        assert html.count(f'href="{INDEX}{name}/"') >= 1, name
        assert html.count(f"<code>{raw.path_of(name)}</code>") >= 1, name
    for _label, path in raw.REST_ENTITIES.values():
        assert f"<code>{escape(path)}</code>" in html
    for path in catalogue.EXCLUDED:
        assert f"<code>{path}</code>" in html
    assert html.count("Not read yet.") == len(raw.REST_ENTITIES)
    # the source rows: once each, in the two tables (the by-page table links them too)
    rows = html.split("Read by Progress reports and Partner monitoring", 1)[1]
    for name in ENTITY_SYNCS:
        assert rows.count(f'<a class="nd-link" href="{INDEX}{name}/">') == 1, name


def test_a_page_s_line_keeps_the_sources_it_reads(client_validator, world):
    html = client_validator.get(INDEX, {"page": "reports:partner_reporting"}).content.decode()
    rows = html.split("Read by Progress reports and Partner monitoring", 1)[1]
    shown = {name for name in ENTITY_SYNCS if f'<a class="nd-link" href="{INDEX}{name}/">' in rows}
    assert shown == set(raw.PAGE_SOURCES["reports:partner_reporting"])
    assert "Not read by NeuroDB" not in html


# -------------------------------------------------------------------------------------------- the run facts
PRP_21_OCT = {
    "read": 150_221,
    "datamart_count": 150_295,
    "duplicates_read": 74,
    "removal_skipped": True,
    "documents": 1_590,
    "other_country_skipped": 148_631,
    "business_area": "2490",
}


def test_the_21_october_progress_reports_sync_adds_up(world):
    run = _run("prp_progress_reports", status=SyncRun.Status.PARTIAL, rows_in=1_590, details=PRP_21_OCT)
    dm.DatamartDocument.objects.bulk_create(
        dm.DatamartDocument(dataset="prp_progress_reports", record_key=f"{n:064x}", data={"report_number": n})
        for n in range(1_590)
    )
    facts = runfacts.facts("prp_progress_reports")
    assert facts.run == run
    assert (facts.received, facts.other_country, facts.not_saved, facts.updated_by_sync) == (
        150_221,
        148_631,
        0,
        1_590,
    )
    assert facts.received - facts.other_country - facts.unnamed - facts.not_saved == facts.updated_by_sync
    assert facts.unexplained == 0 and facts.held == 1_590 and facts.from_earlier == 0
    assert facts.sent_twice == 74 and facts.never_came == 74
    assert facts.complete == (False, "No: eTools said it had 74 more")


def test_a_typed_and_a_kept_whole_dataset_satisfy_the_equation(world):
    # typed: 10 received, 4 of another country (1 of them through the business area filter), 2 sent twice
    run = _run(
        "tpm_visits",
        rows_in=6,
        details={"read": 10, "other_country_skipped": 4, "area_strays": 1, "duplicates_read": 2},
    )
    dm.TPMVisit.objects.bulk_create(dm.TPMVisit(datamart_id=n, reference_number=f"T{n}") for n in range(8))
    earlier = run.started_at - datetime.timedelta(days=1)
    dm.TPMVisit.objects.filter(datamart_id__in=(6, 7)).update(synced_at=earlier)
    facts = runfacts.facts("tpm_visits")
    assert (facts.received, facts.other_country, facts.through_area_filter, facts.updated_by_sync) == (
        10,
        4,
        1,
        6,
    )
    assert (facts.held, facts.from_earlier, facts.sent_twice, facts.unexplained) == (8, 2, 2, 0)
    assert facts.complete == (True, "Yes")
    # kept whole: 20 received, 12 not kept, 3 of which named no country, 8 written
    _run(
        "prp_programme_documents",
        rows_in=8,
        details={"read": 20, "other_country_skipped": 12, "unnamed_skipped": 3, "datamart_count": 20},
    )
    dm.DatamartDocument.objects.bulk_create(
        dm.DatamartDocument(dataset="prp_programme_documents", record_key=str(n)) for n in range(8)
    )
    facts = runfacts.facts("prp_programme_documents")
    assert (facts.other_country, facts.unnamed, facts.updated_by_sync, facts.unexplained) == (9, 3, 8, 0)
    assert facts.received - facts.other_country - facts.unnamed - facts.not_saved == facts.updated_by_sync


def test_a_newer_failed_sync_is_named_and_complete_says_why(client_validator, world):
    _run("partner_reports", status=SyncRun.Status.FAILED, ago=0, error="boom")
    SyncRun.objects.filter(target="partner_reports", status=SyncRun.Status.FAILED).update(
        started_at=timezone.now() + datetime.timedelta(hours=2),
        finished_at=timezone.now() + datetime.timedelta(hours=3),
    )
    facts = runfacts.facts("partner_reports")
    assert facts.run == world["run"] and facts.newer_failed is not None
    assert facts.complete == (False, "No: the last sync failed")
    html = client_validator.get(f"{INDEX}partner_reports/").content.decode()
    assert "failed: the records in NeuroDB come from the sync of" in html


def test_an_unexplained_remainder_is_shown_in_red(client_validator, world):
    SyncRun.objects.filter(pk=world["run"].pk).update(details={"read": 14, "other_country_skipped": 4})
    html = client_validator.get(f"{INDEX}partner_reports/").content.decode()
    assert "4 records are not explained by the sync's counts: tell the NeuroDB team." in html


# ------------------------------------------------------------------------------------------------ filters
def _shown(client, source: str, **params) -> int:
    response = client.get(f"{INDEX}{source}/", params)
    assert response.status_code == 200
    return response.context["n"]


def test_the_filters_narrow_as_the_pages_do(client_validator, world):
    rows = dm.ReportedIndicator.objects.all()
    assert _shown(client_validator, "partner_reports") == 6
    assert (
        _shown(client_validator, "partner_reports", section="Education")
        == rows.filter(reporting.section_q(["Education"])).count()
        == 3
    )  # its own section, or its programme document's when it names none
    links = {k: _shown(client_validator, "partner_reports", pd_link=k) for k in raw.PD_LINKS}
    assert links == {"running": 2, "other_status": 1, "draft_or_cancelled": 1, "not_held": 1, "no_pd": 1}
    assert sum(links.values()) == rows.count()
    assert _shown(client_validator, "partner_reports", pd="LEB/PD9") == 1  # its own reference
    assert _shown(client_validator, "partner_reports", pd="LEB/PD1") == 2
    assert _shown(client_validator, "partner_reports", partner="V1") == 4
    assert _shown(client_validator, "partner_reports", report_type="HR") == 1
    assert _shown(client_validator, "partner_reports", year="2026") == 6
    assert _shown(client_validator, "partner_reports", year="2025") == 0
    assert _shown(client_validator, "partner_reports", record="3") == 1
    assert _shown(client_validator, "partner_reports", updated="last") == 6
    assert _shown(client_validator, "partner_reports", updated="earlier") == 0
    assert _shown(client_validator, "partner_reports", q="IR-2") == 1
    assert _shown(client_validator, "partner_reports", status="Accepted") == 6


def test_a_search_for_a_person_is_refused(client_validator, world):
    User.objects.create_user(
        username="someone", first_name="Person", last_name="One", password="pass-123456-x"
    )
    response = client_validator.get(f"{INDEX}partner_reports/", {"q": NAME})
    assert response.context["n"] == 0 and raw.REFUSED in response.content.decode()
    response = client_validator.get(f"{INDEX}partner_reports/", {"partner": NAME})
    assert raw.REFUSED in response.content.decode()
    csv_response = client_validator.get(f"{INDEX}partner_reports/rows.csv", {"q": NAME})
    assert csv_response.status_code == 400 and csv_response.content.decode() == raw.REFUSED


# ------------------------------------------------------------------------------------------------ the pages
def test_the_source_page_shows_no_person(client_validator, world):
    html = client_validator.get(f"{INDEX}partner_reports/").content.decode()
    assert NAME not in html and EMAIL not in html
    assert "Fields this source sends" in html and "names people: shown as a count" in html
    assert (
        "LEB/PD1" in html and "Amel Association" in html and "not in NeuroDB" in html and "names none" in html
    )


def test_a_partner_report_row_shows_its_report_and_the_status_the_pages_read(client_validator, world):
    from neurodb.datamart import reached as rc

    row = dm.ReportedIndicator.objects.get(datamart_id=1)
    response = client_validator.get(f"{INDEX}partner_reports/{row.pk}/")
    html = response.content.decode()
    values = {f: getattr(row, f) for f in rc.ROW_FIELDS if hasattr(row, f)}
    assert response.context["report"]["report_key"] == rc.report_key(values)
    assert response.context["report"]["status"] == "Accepted"
    assert NAME not in html and EMAIL not in html
    assert "submitted_by (people)" in html and "1 person (not shown)" in html
    assert "the sync of" in html and "(the last one)" in html


def test_a_record_held_by_its_content_says_so(client_validator, world):
    doc = dm.DatamartDocument.objects.create(
        dataset="prp_progress_reports", record_key="a" * 64, data={"status": "Due"}
    )
    html = client_validator.get(f"{INDEX}prp_progress_reports/{doc.pk}/").content.decode()
    assert "Its id is made from its content" in html


def test_a_superuser_reviews_a_field_and_hides_it_again(client, roles, world):
    dm.DatamartDocument.objects.create(dataset="fm_options", record_key="1", data={"site_feedback": "Good"})
    client.force_login(_superuser())
    html = client.get(f"{INDEX}fm_options/").content.decode()
    assert "new field: not shown until reviewed" in html and "Reviewed: show it" in html
    url = f"{INDEX}fm_options/fields/"
    assert client.post(url, {"key": "site_feedback"}).status_code == 302
    assert dm.RawFieldReview.objects.filter(source="fm_options", key="site_feedback").exists()
    assert "Hide again" in client.get(f"{INDEX}fm_options/").content.decode()
    client.post(url, {"key": "site_feedback", "action": "hide"})
    assert not dm.RawFieldReview.objects.exists()
    client.post(url, {"key": NAME})  # never a field whose name is not an identifier
    assert not dm.RawFieldReview.objects.exists()


def test_a_validator_who_is_not_a_superuser_reviews_nothing(client_validator, world):
    assert client_validator.post(f"{INDEX}fm_options/fields/", {"key": "x"}).status_code == 404


# ----------------------------------------------------------------------------------------------- the CSV
def _csv(response) -> list[dict[str, str]]:
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_the_csv_keeps_texts_as_texts_numbers_as_numbers_and_no_person(client_validator, world):
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="raw_partner_reports_')
    rows = _csv(response)
    assert len(rows) == 6
    row = next(r for r in rows if r["record_id"] == "1")
    assert row["performance_indicator"].startswith("'=HYPERLINK")
    assert row["total_cumulative_progress"] == "3000000"
    assert row["submitted_by (people)"] == "1"
    assert row["pd_link"] == "running" and row["pd_number_in_neurodb"] == "LEB/PD1"
    assert row["figures_as_of"] and row["updated_by_last_sync"] == "yes"
    text = repr(rows)
    assert NAME not in text and EMAIL not in text and "submitter_email" not in text


def test_over_its_limit_the_csv_says_which_filter_to_choose(client_validator, world, monkeypatch):
    monkeypatch.setitem(raw.CSV_LIMIT, "long_texts", 5)
    page = client_validator.get(f"{INDEX}partner_reports/").content.decode()
    sentence = "The filter holds 6 records, more than 5: choose a year, a section, a partner or a programme document first."
    assert sentence in page and "CSV of these" not in page
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 400 and response.content.decode() == sentence
    assert len(_csv(client_validator.get(f"{INDEX}partner_reports/rows.csv", {"pd": "LEB/PD1"}))) == 2


def test_a_field_first_met_late_goes_into_other_fields(client_validator, world, monkeypatch):
    monkeypatch.setattr(raw, "HEADER_RECORDS", 3)
    monkeypatch.setattr(raw, "FIELDS_COUNTED", 3)
    dm.ReportedIndicator.objects.filter(datamart_id=1).update(data={"id": 1, "late_amount": 5})
    rows = _csv(client_validator.get(f"{INDEX}partner_reports/rows.csv"))
    row = next(r for r in rows if r["record_id"] == "1")
    assert "late_amount" not in rows[0] and '"late_amount":5' in row["other_fields"]


def test_two_downloads_at_once_then_a_third_waits(client_validator, world, settings):
    with connection.cursor() as cursor:  # a download an earlier test never read
        cursor.execute("SELECT pg_advisory_unlock_all()")
    db = connection.settings_dict
    other = psycopg.connect(
        host=db["HOST"], port=db["PORT"] or 5432, user=db["USER"], password=db["PASSWORD"], dbname=db["NAME"]
    )
    try:
        for i in range(settings.RAW_CSV_MAX_RUNNING):  # two downloads elsewhere
            assert other.execute("SELECT pg_try_advisory_lock(%s)", [raw.RAW_CSV_LOCK + i]).fetchone()[0]
        response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
        assert response.status_code == 503 and response.content.decode() == raw.BUSY
    finally:
        other.close()
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 200 and len(_csv(response)) == 6
    with connection.cursor() as cursor:  # the download freed its slot
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", [raw.RAW_CSV_LOCK]
        )
        assert cursor.fetchone()[0] == 0


def test_a_download_never_read_frees_its_slot_when_closed(client_validator, world):
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 200
    request_finished.disconnect(close_old_connections)  # as the test client closes a response
    try:
        response.close()
    finally:
        request_finished.connect(close_old_connections)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", [raw.RAW_CSV_LOCK]
        )
        assert cursor.fetchone()[0] == 0


def _slow(monkeypatch):
    """Every record of a source read through pg_sleep: a count or a download that takes its time."""
    original = raw.base
    monkeypatch.setattr(raw, "base", lambda name: original(name).extra(where=["pg_sleep(0.05) IS NOT NULL"]))


def test_a_count_that_takes_too_long_says_so(client_validator, world, monkeypatch):
    _slow(monkeypatch)
    monkeypatch.setattr(raw, "COUNT_TIMEOUT_MS", 1)
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 400
    assert response.content.decode().startswith("This filter takes too long on")
    page = client_validator.get(f"{INDEX}partner_reports/")
    assert page.status_code == 200 and "This filter takes too long on" in page.content.decode()


def test_a_download_that_takes_too_long_stops_cleanly(client_validator, world, monkeypatch):
    _slow(monkeypatch)
    monkeypatch.setattr(raw, "STREAM_TIMEOUT_MS", 1)
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 200 and _csv(response) == []
    assert dm.ReportedIndicator.objects.count() == 6  # the connection works on


# ----------------------------------------------------------------------------------------------- queries
# The statements that only frame a query: a count runs in its own snapshot, with its own time limit
FRAMING = re.compile(r"^(SAVEPOINT|RELEASE SAVEPOINT|ROLLBACK TO SAVEPOINT|SHOW |SET |SELECT set_config)")
ADMIN_OWN = (
    6  # every admin page: the session, the user, the donor check, the groups, the two permission reads
)
PAGE_QUERIES = (
    8  # a source's page: its runs (2), its counts, the filter's count, the fields, the reviews, 50 records
)
PARTNER_REPORTS_MORE = 3  # the partner reports' page also says from when NeuroDB holds them, and check 10


def _queries(client, url: str, **params) -> int:
    client.get(url, params)  # warm: the names, the choices
    with CaptureQueriesContext(connection) as found:
        response = client.get(url, params)
        if response.streaming:
            b"".join(response.streaming_content)
    assert response.status_code == 200
    return sum(not FRAMING.match(q["sql"]) for q in found.captured_queries)


def _more_rows(world, n: int) -> None:
    dm.ReportedIndicator.objects.bulk_create(
        dm.ReportedIndicator(
            datamart_id=1000 + i,
            intervention=world["running"],
            partner=world["partner"],
            pd_reference_number="LEB/PD1",
            report_type="QPR",
            period_end=datetime.date(2026, 3, 31),
            data={"id": 1000 + i, "indicator_type": "quantity", "submitted_by": NAME},
        )
        for i in range(n)
    )


def test_a_source_page_makes_as_many_queries_for_5_or_500_records(client_validator, world):
    url = f"{INDEX}partner_reports/"
    few = _queries(client_validator, url)
    _more_rows(world, 500)
    assert _queries(client_validator, url) == few <= ADMIN_OWN + PAGE_QUERIES + PARTNER_REPORTS_MORE
    dm.TPMVisit.objects.bulk_create(dm.TPMVisit(datamart_id=n) for n in range(60))
    assert _queries(client_validator, f"{INDEX}tpm_visits/") <= ADMIN_OWN + PAGE_QUERIES


def test_the_csv_and_the_record_page_make_a_fixed_number_of_queries(client_validator, world, monkeypatch):
    monkeypatch.setattr(raw, "CSV_CHUNK", 2)
    url = f"{INDEX}partner_reports/rows.csv"
    few = _queries(client_validator, url)
    _more_rows(world, 20)
    assert _queries(client_validator, url) == few  # the records come in chunks from one cursor
    row = dm.ReportedIndicator.objects.get(datamart_id=1)
    assert _queries(client_validator, f"{INDEX}partner_reports/{row.pk}/") <= ADMIN_OWN + PAGE_QUERIES
    # every source: their runs (2) and the moment of the counts kept for an hour
    assert _queries(client_validator, INDEX) <= ADMIN_OWN + 3
