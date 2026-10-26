"""Raw eTools data (Admin → Data and sync; datamart.raw_admin): who opens it, what the first page lists, what each
sync received and kept (datamart.runfacts), the filters, a record's page, the CSV and its limits, and the queries
each page makes. Synthetic people only."""

from __future__ import annotations

import csv
import datetime
import io
import re
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
import responses
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.signals import request_finished
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VALIDATORS, VIEWER
from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, raw, reporting, runfacts
from neurodb.datamart import models as dm
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS, FR_VENDOR_KINDS
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


def test_a_validator_reads_the_reporting_checks_and_cannot_run_them(
    client, roles, client_validator, admin_user, monkeypatch
):
    """Read-only for validators, with no link that refuses them (the rows' table, the failed run, the Datamart's
    tables in the header); an Administrator keeps them all."""
    from neurodb.datamart import checks

    monkeypatch.setattr(checks, "run_all", lambda: [])
    failed = SyncRun.objects.create(job=SyncRun.Job.REPORTING_CHECKS, status=SyncRun.Status.RUNNING)
    failed.finish(SyncRun.Status.FAILED)
    url = reverse("admin:datamart_reporting_checks")
    administrators_only = (
        reverse("admin:datamart_reportedindicator_changelist"),
        reverse("admin:core_syncrun_change", args=[failed.pk]),
        reverse("admin:app_list", args=["datamart"]),
    )
    page = client_validator.get(url)
    html = page.content.decode()
    assert page.status_code == 200 and "Run the checks now" not in html
    assert "failed" in html and not [link for link in administrators_only if f'href="{link}"' in html]
    assert client_validator.post(url).status_code == 403
    for groups in ((VIEWER, VALIDATORS), (SECTION_EDITOR, VALIDATORS)):  # every link it shows opens
        client.force_login(_user(f"reader-{len(groups[0])}", staff=True, groups=groups))
        html = client.get(url).content.decode()
        content = html.split('id="content-main"', 1)[1]
        for link in set(re.findall(r'href="(/manage/[^"#]*)"', _header(html) + content)):
            assert client.get(link.replace("&amp;", "&")).status_code == 200, (groups, link)
    client.force_login(admin_user)
    html = client.get(url).content.decode()
    assert all(f'href="{link}"' in html for link in administrators_only)


def _header(html: str) -> str:
    """The admin page's own header: its title, and the links Unfold puts before it."""
    return re.search(r"<h1\b.*?</h1>", html, re.S).group(0)


def test_the_raw_pages_head_with_their_title_and_no_link_to_a_developer_table(client, roles, world):
    """Raw eTools data's pages say what they are in their header (the page's title), never a link to the
    Datamart's tables: a Viewer or Section editor validator could not open them, an Administrator would land in
    the table that shows people."""
    record = dm.ReportedIndicator.objects.order_by("pk").first()
    pages = (
        (INDEX, "Raw eTools data"),
        (f"{INDEX}partner_reports/", raw.NAMES["partner_reports"]),
        (
            reverse("admin:datamart_raw_record", args=["partner_reports", record.pk]),
            raw.NAMES["partner_reports"],
        ),
    )
    developer = (
        reverse("admin:app_list", args=["datamart"]),
        reverse("admin:datamart_datamartdocument_changelist"),
        reverse("admin:datamart_reportedindicator_changelist"),
    )
    for n, groups in enumerate(((VIEWER, VALIDATORS), (SECTION_EDITOR, VALIDATORS), (ADMIN, VALIDATORS))):
        client.force_login(_user(f"reader-{n}", staff=True, groups=groups))
        for url, title in pages:
            header = _header(client.get(url).content.decode())
            assert title in header, (groups, url, header)
            assert not [link for link in developer if link in header], (groups, url, header)


def test_data_health_offers_the_reporting_checks_to_validators_with_staff_status(client, roles):
    url = reverse("admin:datamart_reporting_checks")
    client.force_login(_user("plain-validator", staff=False, groups=(VIEWER, VALIDATORS)))
    html = client.get(reverse("reports:data_health")).content.decode()
    assert f'href="{url}"' not in html and "eTools reporting checks: ask for staff status." in html
    client.force_login(_user("staff-validator", staff=True, groups=(VIEWER, VALIDATORS)))
    html = client.get(reverse("reports:data_health")).content.decode()
    assert f'href="{url}"' in html and "ask for staff status" not in html


def test_the_admin_home_links_a_staff_validator_only_where_they_may_go(client, roles, world, monkeypatch):
    """The admin home a validator with staff status lands on: its tiles, its lines to look at and its jobs link
    only to the pages the reader may open, the others read as plain text."""
    from neurodb.web import health

    lines = [
        health.Warning(
            key="no_role", text="Active users without a role", url=reverse("admin:users_user_changelist")
        ),
        health.Warning(key="raw", text="Raw eTools data", url=INDEX),
    ]
    monkeypatch.setattr(health, "warnings", lambda now=None: lines)
    for n, groups in enumerate(((VIEWER, VALIDATORS), (SECTION_EDITOR, VALIDATORS))):
        client.force_login(_user(f"reader-{n}", staff=True, groups=groups))
        html = client.get(reverse("admin:index")).content.decode()
        assert "Active users without a role" in html and "eTools Datamart" in html  # a line, a job: as text
        for link in sorted(set(re.findall(r'href="(/manage/[^"#]*)"', html))):
            if "logout" in link:
                continue
            response = client.get(link.replace("&amp;", "&"))
            assert response.status_code not in (403, 404), (groups, link, response.status_code)


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
    assert facts.sent_twice == 74 and facts.never_came == 74 and facts.copies_proven == 0
    # the 74 that came twice, not proven the Datamart's own copies, may stand for the 74 that did not come
    assert facts.gap_by_repeats and facts.twice_unproven == 74
    assert facts.complete == (
        False,
        "No: 74 were sent twice and may stand for others that did not come; nothing was removed",
    )


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
    assert "failed (an unexpected error).</span>" in html and "boom" not in html
    assert "The records in NeuroDB come from the sync of" in html


def test_records_sent_twice_are_said_plainly_on_the_source_page(client_validator, world):
    """25 Oct 2026, reports/indicators: eTools said it had 8,899, NeuroDB received 6,960 and 1,939 came twice.
    Proven the Datamart's own copies (every copy in one answer), the read is complete; not proven, the page says
    they may stand for records that did not come, why NeuroDB cannot count them as copies, and that nothing was
    removed, never that the next sync reads them."""
    from html import unescape

    proven = {"datamart_count": 8_899, "duplicates_read": 1_939, "twice_identical": True,
              "copies_proven": {"twice": 1_939, "same_page": 1_939, "asked_again": 0}}  # fmt: skip
    run = _run("cp_indicators", rows_in=6_960, details=proven)
    dm.DatamartDocument.objects.bulk_create(
        dm.DatamartDocument(dataset="cp_indicators", record_key=f"{n:064x}") for n in range(6_960)
    )
    html = " ".join(unescape(client_validator.get(f"{INDEX}cp_indicators/").content.decode()).split())
    assert "<strong>Complete?</strong> Yes" in html
    assert (
        "The Datamart sent 1,939 records twice (identical), every copy in one of its answers (on one page)"
        in html
    )
    assert "1,939 the Datamart sent twice (identical)" in client_validator.get(INDEX).content.decode()

    SyncRun.objects.filter(pk=run.pk).update(
        status=SyncRun.Status.PARTIAL,
        details={"datamart_count": 8_899, "duplicates_read": 1_939, "twice_identical": True,
                 "pages_shifted": {"twice": 1_939, "missing": 1_939}, "ordering": {"key": "id", "carried": False},
                 "removal_skipped": True, "read": 6_960, "partial": True},
    )  # fmt: skip
    html = " ".join(unescape(client_validator.get(f"{INDEX}cp_indicators/").content.decode()).split())
    assert "No: 1,939 were sent twice and may stand for others that did not come; nothing was removed" in html
    assert (
        "eTools said it had 1,939 more than NeuroDB received. 1,939 records came twice (identical) while the "
        "Datamart's pages were read: rows moved between pages, and then as many others did not come, or the "
        "Datamart holds them twice. Nothing was removed; the records do not carry id, so the order asked by id "
        "cannot be checked, and the next read is in the Datamart's own order again. NeuroDB counts a record sent "
        "twice as eTools' own copy only when every copy comes identical in one answer of the Datamart (one page): "
        "these did not, so the read is not complete." in html
    )
    assert "The next sync reads them." not in html


@responses.activate
def test_the_engagements_count_what_their_detail_datasets_saved_right_after_them(world):
    """The engagements run writes the engagements; the audits run, right after it in the same sync, saves every
    one it matches again (24 Oct 2026: 633 engagements, every one saved again at 21:59). Those saves count as
    the sync's, as the programme documents count what their budgets saved: updated by the sync 3, not 0, and
    none updated since. The detail dataset's own row does not move, and an engagement a later sync wrote stays
    updated since."""
    from neurodb.integrations.etools import datamart_sync
    from neurodb.integrations.etools.datamart import DatamartClient
    from neurodb.integrations.http import make_session
    from tests.integrations.fake_datamart import FakeDatamart

    api = "https://datamart.test/api/latest/datamart"
    lebanon = {"country_name": "Lebanon"}
    FakeDatamart([{"id": n, "reference_number": f"LEB/AUD/{n}", "engagement_type": "audit", **lebanon}
                  for n in (1, 2, 3)]).register(f"{api}/audit/engagements/")  # fmt: skip
    FakeDatamart([{"id": 10 + n, "reference_number": f"LEB/AUD/{n}", "audited_expenditure": "100", **lebanon}
                  for n in (1, 2, 3)]).register(f"{api}/audit/audit/")  # fmt: skip
    client = DatamartClient("https://datamart.test", "svc-user", "not-a-real-secret", country="Lebanon",
                            session=make_session("", backoff=0))  # fmt: skip
    engagements, audits = datamart_sync.sync_all(
        only=["engagements", "audits"], triggered_by="test", client=client
    )
    assert engagements.finished_at < audits.started_at
    assert dm.AuditEngagement.objects.filter(synced_at__gt=engagements.finished_at).count() == 3
    facts = runfacts.facts("engagements")
    assert (facts.held, facts.updated_by_sync, facts.updated_since, facts.from_earlier) == (3, 3, 0, 0)
    assert facts.complete == (True, "Yes")
    assert raw.held_counts(runfacts.runs(["engagements", "audits"])) == {
        "engagements": [3, 3, 0],
        "audits": [3, 3, 0],
    }

    # the next sync's engagements run started: what it, or the audits after it, saved is not the first one's
    later = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="engagements")
    after = _run("audits", ago=0)
    SyncRun.objects.filter(pk=after.pk).update(started_at=timezone.now(), finished_at=timezone.now())
    dm.AuditEngagement.objects.filter(reference_number="LEB/AUD/1").update(synced_at=timezone.now())
    assert later.started_at > audits.finished_at
    assert raw.written_until("engagements", engagements) == audits.finished_at
    assert runfacts.counts("engagements", engagements) == (3, 2, 1)


@responses.activate
def test_an_engagement_kept_from_an_earlier_sync_and_saved_by_its_audit_is_not_the_sync_s(
    client_validator, world
):
    """The second night's engagements read is not whole (2 of the 3 announced came), so engagement 3 is kept, and
    its audit, read right after, saves it again. Updated by the last sync counts at most what the engagements
    run kept itself (2): the third was kept from an earlier sync, and nothing is unexplained."""
    from neurodb.integrations.etools import datamart_sync
    from neurodb.integrations.etools.datamart import DatamartClient
    from neurodb.integrations.http import make_session
    from tests.integrations.fake_datamart import FakeDatamart

    api, lebanon = "https://datamart.test/api/latest/datamart", {"country_name": "Lebanon"}
    client = DatamartClient("https://datamart.test", "svc-user", "not-a-real-secret", country="Lebanon",
                            session=make_session("", backoff=0))  # fmt: skip
    for cap in (None, 2):
        responses.reset()
        FakeDatamart([{"id": n, "reference_number": f"LEB/AUD/{n}", "engagement_type": "audit", **lebanon}
                      for n in (1, 2, 3)], cap=cap, cut_count=False).register(f"{api}/audit/engagements/")  # fmt: skip
        FakeDatamart([{"id": 10 + n, "reference_number": f"LEB/AUD/{n}", "audited_expenditure": "100", **lebanon}
                      for n in (1, 2, 3)]).register(f"{api}/audit/audit/")  # fmt: skip
        engagements, _ = datamart_sync.sync_all(
            only=["engagements", "audits"], triggered_by="test", client=client
        )
    assert engagements.details["removal_skipped"]
    facts = runfacts.facts("engagements")
    assert (facts.received, facts.held, facts.updated_by_sync, facts.updated_since) == (2, 3, 2, 0)
    assert (facts.from_earlier, facts.unexplained) == (1, 0)
    html = " ".join(client_validator.get(f"{INDEX}engagements/").content.decode().split())
    assert "not explained" not in html


def test_a_dataset_the_datamart_account_may_not_read_says_whom_to_ask(client_validator, world):
    """24 Oct 2026: audit/financial-findings-all answered 403 while the same account read about 40 other
    datasets that night: signed in, not allowed this one. Raw eTools data says so and whom to ask, never the
    credentials."""
    error = "IntegrationError: GET /api/latest/datamart/audit/financial-findings-all/: HTTP 403 Forbidden"
    _run("audit_findings", status=SyncRun.Status.FAILED, ago=0, error=error)
    facts = runfacts.facts("audit_findings")
    assert facts.run is None and facts.not_allowed
    assert facts.complete == (False, "No: not allowed to read it (ask the eTools Datamart team)")
    html = " ".join(client_validator.get(INDEX).content.decode().split())
    assert (
        "failed (eTools Datamart answered 403: the account NeuroDB uses is not allowed to read it; ask the "
        "eTools Datamart team to grant it)" in html
    )
    assert "No: not allowed to read it (ask the eTools Datamart team)" in html and "credentials" not in html
    unauthorised = _run(
        "audit_findings",
        status=SyncRun.Status.FAILED,
        ago=0,
        error=error.replace("403 Forbidden", "401 Unauthorized"),
    )
    assert not runfacts.not_allowed(unauthorised)
    assert runfacts.failure_words(unauthorised) == "eTools Datamart answered 401: sign-in refused"


FORBIDDEN = "IntegrationError: GET /api/latest/datamart/{path}/: HTTP 403 Forbidden"


def _refused_run(target: str, ago: float, status=SyncRun.Status.FAILED, body: str = "") -> SyncRun:
    started = timezone.now() - datetime.timedelta(hours=ago)
    error = (FORBIDDEN.format(path=raw.path_of(target)) + body) if status == SyncRun.Status.FAILED else ""
    return SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target=target, status=status, error=error,
                                  started_at=started, finished_at=started + datetime.timedelta(minutes=1))  # fmt: skip


def test_every_dataset_refused_403_is_never_said_as_one_dataset_not_allowed(db):
    """Every dataset of one sync answered 403, nothing read: nothing shows the account was signed in, so no page
    says it is not allowed one dataset; they say the sign-in or the dataset, and to renew the credentials if every
    dataset fails so. A body naming the sign-in is the credentials refused."""
    from neurodb.core.admin import error_hint

    runs = [
        _refused_run(name, ago)
        for name, ago in (("grants", 1.2), ("engagements", 1.1), ("partner_reports", 1))
    ]
    for run in runs:
        assert runfacts.refused(run) == runfacts.EITHER and not runfacts.not_allowed(run)
        assert (
            runfacts.failure_words(run)
            == "eTools Datamart answered 403: it refused NeuroDB's sign-in or this dataset"
        )
        hint = str(error_hint(run))
        assert (
            "renew the credentials" in hint
            and "grant the account access" in hint
            and "not allowed" not in hint
        )
    assert runfacts.facts("engagements").complete == (False, "No: the last sync failed")
    signed_out = _refused_run("audits", 1, body=' {"detail":"Invalid username/password."}')
    assert runfacts.refused(signed_out) == runfacts.SIGN_IN
    assert "refused NeuroDB's credentials" in str(error_hint(signed_out))
    assert runfacts.failure_words(signed_out) == "eTools Datamart answered 403: sign-in refused"


def test_a_403_beside_a_dataset_that_read_is_this_dataset_not_allowed(db):
    """24 Oct 2026: audit/financial-findings-all answered 403 between the audits that read: the sign-in worked, so
    the account is not allowed that dataset. The same for "Sync this dataset again" the next morning, the nearest
    run of another dataset being last night's audits."""
    _refused_run("audits", 10, status=SyncRun.Status.SUCCEEDED)
    findings = _refused_run("audit_findings", 9.9)
    _refused_run("spot_checks", 9.8, status=SyncRun.Status.PARTIAL)
    again = _refused_run("audit_findings", 1)
    for run in (findings, again):
        assert runfacts.not_allowed(run)
        assert runfacts.failure_words(run).startswith(
            "eTools Datamart answered 403: the account NeuroDB uses is not"
        )


ERRORS_SEEN = (  # what a failed run's error holds -> what Raw eTools data says of it
    (
        "IntegrationError: GET /api/latest/datamart/audit/financial-findings-all/: HTTP 500 Internal Server "
        f'Error {{"detail": "{NAME} <{EMAIL}>"}}',
        "eTools Datamart answered 500",
    ),
    (
        "IntegrationError: GET /api/latest/datamart/x/: HTTP 404 Not Found",
        "eTools Datamart answered 404: not found",
    ),
    (
        "IntegrationError: GET /api/latest/datamart/x/: HTTP 503 Service Unavailable",
        "eTools Datamart answered 503: unavailable",
    ),
    (
        "IntegrationError: GET /api/latest/datamart/x/: HTTP 401 Unauthorized",
        "eTools Datamart answered 401: sign-in refused",
    ),
    (
        "IntegrationError: unrecognised list payload from datamart/x",
        "eTools Datamart sent an answer NeuroDB cannot read",
    ),
    (
        "Cut off: the sync process stopped before this run finished (the container was restarted).",
        "NeuroDB was restarted during the run",
    ),
    ("KeyError: 'Person One'", "an unexpected error"),
    (  # signed in, not allowed this dataset (24 Oct 2026); a sign-in refused is 401, above
        "IntegrationError: GET /api/latest/datamart/audit/financial-findings-all/: HTTP 403 Forbidden "
        '{"detail": "You do not have permission to perform this action."}',
        "eTools Datamart answered 403: the account NeuroDB uses is not allowed to read it; ask the eTools "
        "Datamart team to grant it",
    ),
)


@pytest.mark.parametrize(("error", "words"), ERRORS_SEEN)
def test_a_failed_run_is_said_by_its_cause_never_by_its_text(error, words):
    run = SyncRun(job=SyncRun.Job.ETOOLS_DATAMART, target="audit_findings", status="failed", error=error)
    assert runfacts.failure_words(run) == words
    for planted in (NAME, EMAIL, "Internal Server Error", "/api/"):
        assert planted not in words
    rest = SyncRun(
        job=SyncRun.Job.ETOOLS_REST_RAW,
        status="failed",
        error='IntegrationError: GET /api/v2/partners/: HTTP 403 Forbidden {"detail": "Invalid token."}',
    )
    assert runfacts.failure_words(rest) == "eTools answered 403: not allowed"


def _server(answer: bytes | None) -> tuple[str, Callable[[], None]]:
    """A server on this machine that takes every connection, sends ``answer`` (None: nothing), then never another
    byte; and how to close it."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    stop, taken = threading.Event(), []

    def serve() -> None:
        while not stop.is_set():
            try:
                taken_one, _address = listener.accept()
            except TimeoutError:
                continue
            taken.append(taken_one)
            if answer:
                taken_one.sendall(answer)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def close() -> None:
        stop.set()
        thread.join()
        for taken_one in taken:
            taken_one.close()
        listener.close()

    return f"http://127.0.0.1:{listener.getsockname()[1]}", close


def _closed_port() -> str:
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _no_connection_in_time(monkeypatch) -> str:
    """No connection made in time (the address answers nothing): urllib3's own error, raised where it opens one."""
    from urllib3.connection import HTTPConnection
    from urllib3.exceptions import ConnectTimeoutError

    def no_connection(self):
        raise ConnectTimeoutError(self, f"Connection to {self.host} timed out. (connect timeout=0.3)")

    monkeypatch.setattr(HTTPConnection, "_new_conn", no_connection)
    return "http://192.0.2.1"


@pytest.mark.parametrize(
    ("how", "cause", "words", "hint"),
    (
        ("silent", "ReadTimeout", "no answer in time", "eTools did not answer in time"),
        ("stalled", "ReadTimeout", "no answer in time", "eTools did not answer in time"),
        ("closed", "ConnectionError", "could not connect", "eTools could not be reached"),
        ("connect", "ConnectTimeout", "could not connect", "eTools could not be reached"),
    ),
)
def test_a_request_with_no_answer_is_said_alike_on_raw_etools_data_and_on_its_run(
    monkeypatch, how, cause, words, hint
):
    """The errors the eTools client really makes: a server that answers nothing in time (with the retries used
    up, requests calls it a ConnectionError), one that stops part way through its answer, a closed port and an
    address where no connection is made in time. Raw eTools data and the run page say the same of each."""
    from neurodb.core.admin import error_hint
    from neurodb.integrations.etools.client import EToolsClient
    from neurodb.integrations.http import IntegrationError, make_session
    from neurodb.integrations.runs import describe_error

    close = lambda: None  # noqa: E731
    if how == "silent":
        base, close = _server(None)
    elif how == "stalled":
        base, close = _server(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 50\r\n\r\n["
        )
    elif how == "closed":
        base = _closed_port()
    else:
        base = _no_connection_in_time(monkeypatch)
    session = make_session("Token abc", 0.3, retries=1, backoff=0)
    session.trust_env = False  # never through a proxy: the server is this machine
    try:
        with pytest.raises(IntegrationError) as caught:
            EToolsClient(base, "abc", session=session).get("/api/v2/partners/")
    finally:
        close()
    error = describe_error(caught.value)
    assert error == f"IntegrationError: GET /api/v2/partners/: {cause}"
    run = SyncRun(job=SyncRun.Job.ETOOLS_REST_RAW, target="rest_partners", status="failed", error=error)
    assert runfacts.failure_words(run) == words
    assert hint in str(error_hint(run))


def _no_code_errors() -> list[str]:
    """The errors of a PRP dataset's run with no business area code: none set, a country's name set (set aside),
    and the guard's, should a value that is not a code ever reach the request."""
    from neurodb.integrations.etools import datamart_sync
    from neurodb.integrations.http import IntegrationError
    from neurodb.integrations.runs import describe_error

    errors = [
        describe_error(IntegrationError(datamart_sync._no_code_error(choice, "Lebanon")))
        for choice in (datamart_sync.AreaCode(), datamart_sync.AreaCode("", "", {"environment": "Lebanon"}))
    ]
    scope = datamart_sync.CountryScope(client=None)
    scope.choice = datamart_sync.AreaCode("Lebanon", datamart_sync.FROM_ADMIN)
    with pytest.raises(IntegrationError) as caught:
        scope.request(catalogue.DOCUMENTS["prp_indicator_reports"])
    return [*errors, describe_error(caught.value)]


def test_no_business_area_code_is_said_in_words_without_the_value_set_aside(client_validator, world):
    words = "no PRP business area code is set: enter it in Admin → Data and sync → eTools Datamart"
    for error in _no_code_errors():
        run = SyncRun(
            job=SyncRun.Job.ETOOLS_DATAMART, target="prp_indicator_reports", status="failed", error=error
        )
        assert runfacts.failure_words(run) == words, error
    _run("prp_indicator_reports", status=SyncRun.Status.FAILED, error=_no_code_errors()[1])
    html = client_validator.get(INDEX).content.decode()
    row = html.split(f'<th scope="row"><a class="nd-link" href="{INDEX}prp_indicator_reports/"', 1)[1]
    row = row.split("</tr>", 1)[0]
    assert f"({words})</small>" in row and "Lebanon" not in row


def test_the_first_page_says_why_a_dataset_failed_and_links_the_run_for_those_who_open_runs(
    client, client_validator, world, admin_user
):
    """The print of 24 Oct: "Financial findings: Never, the sync of 21 Oct 2026 failed", with no cause and no link,
    and the Datamart's own monitoring shown as an alarm although no page or check uses it."""
    failed = _run(
        "audit_findings",
        status=SyncRun.Status.FAILED,
        error=ERRORS_SEEN[0][0],
    )
    monitor = _run("datamart_etl_status", status=SyncRun.Status.FAILED, error=ERRORS_SEEN[0][0])
    html = client_validator.get(INDEX).content.decode()
    row = html.split(f'href="{INDEX}audit_findings/"', 1)[1].split("</tr>", 1)[0]
    assert "failed</small>" not in row and "(eTools Datamart answered 500)</small>" in row
    assert reverse("admin:core_syncrun_change", args=[failed.pk]) not in html  # a validator opens no run
    assert NAME not in html and EMAIL not in html and "Internal Server Error" not in html
    assert "No: the last sync failed" in row
    monitor_row = html.split(f'href="{INDEX}datamart_etl_status/"', 1)[1].split("</tr>", 1)[0]
    assert "diagnostic: no page or check uses it" in monitor_row and "nd-found__warn" not in monitor_row
    superuser = User.objects.create_superuser(
        username="root", email="root@example.org", password="x-123456-pw"
    )
    client.force_login(superuser)
    html = client.get(INDEX).content.decode()
    for run in (failed, monitor):
        assert (
            f'<a class="nd-link" href="{reverse("admin:core_syncrun_change", args=[run.pk])}">the sync of'
            in html
        )
    page = client.get(f"{INDEX}audit_findings/").content.decode()
    assert "Nothing received yet." in page and "failed</a> (eTools Datamart answered 500)." in page
    assert NAME not in page and EMAIL not in page


@pytest.mark.parametrize("later", [SyncRun.Status.RUNNING, SyncRun.Status.FAILED])
def test_the_records_a_later_sync_rewrote_are_explained(client_validator, world, later):
    """Tonight's sync of the dataset is running, or failed after rewriting rows: the rows it rewrote are updated
    since, no longer by the last sync, and the counts still explain them; a real gap stays shown."""
    from neurodb.datamart import trail

    run = world["run"]
    after = run.finished_at + datetime.timedelta(minutes=10)
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=later,
        started_at=after,
        finished_at=None if later == SyncRun.Status.RUNNING else after + datetime.timedelta(minutes=30),
    )
    rewritten = list(dm.ReportedIndicator.objects.order_by("pk").values_list("pk", flat=True)[:5])
    dm.ReportedIndicator.objects.filter(pk__in=rewritten).update(
        synced_at=after + datetime.timedelta(minutes=5)
    )
    facts = runfacts.facts("partner_reports")
    assert (facts.received, facts.updated_by_sync, facts.updated_since, facts.unexplained) == (10, 1, 5, 0)
    running = later == SyncRun.Status.RUNNING
    assert facts.complete == ((True, "Yes") if running else (False, "No: the last sync failed"))
    html = client_validator.get(f"{INDEX}partner_reports/").content.decode()
    steps = trail.source_steps(facts, "partner_reports", "partner report rows", facts.held)
    words = [line.words for step in steps for line in step.lines]
    assert "not explained by the sync's counts" not in html + " ".join(words)
    # 3 more received than the counts and the later sync explain: still shown
    run.details = {**run.details, "read": 13}
    run.save(update_fields=["details"])
    facts = runfacts.facts("partner_reports")
    assert facts.unexplained == 3
    if running:
        assert facts.complete == (False, "No: 3 records are not explained by the sync's counts")


def test_the_rest_records_a_later_sync_rewrote_are_explained(world):
    from neurodb.integrations.etools import rest_raw

    run = _rest_world()
    after = run.finished_at + datetime.timedelta(minutes=10)
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS,
        target=rest_raw.SYNC_TARGETS["rest_partners"],
        status=SyncRun.Status.RUNNING,
        started_at=after,
    )
    dm.RestRecord.objects.filter(entity="rest_partners").update(
        synced_at=after + datetime.timedelta(minutes=5)
    )
    facts = runfacts.facts("rest_partners")
    assert (facts.received, facts.updated_by_sync, facts.updated_since, facts.unexplained) == (2, 0, 2, 0)
    assert facts.complete == (True, "Yes")


def test_what_could_not_be_saved_into_neurodb_s_own_tables_says_complete_no(client_validator, world):
    """A source written by another sync: its raw copies add up, but 3 locations, which the pages read, could
    not be saved."""
    assert set(runfacts.WRITTEN_INTO) == runfacts.WRITTEN_BY
    error = {
        "error": "DataError: value too long for type character varying(100)",
        "count": 3,
        "examples": ["1"],
    }
    _run(
        "locations",
        status=SyncRun.Status.PARTIAL,
        rows_in=10,
        rows_written=7,
        rows_failed=3,
        details={"read": 10, "datamart_count": 10, "errors": [error]},
    )
    dm.DatamartDocument.objects.bulk_create(
        dm.DatamartDocument(dataset="locations", record_key=str(n)) for n in range(10)
    )
    facts = runfacts.facts("locations")
    assert (facts.received, facts.not_saved, facts.updated_by_sync, facts.unexplained) == (10, 0, 10, 0)
    assert facts.not_written == 3
    assert facts.complete == (False, "No: 3 could not be saved into NeuroDB's locations")
    page = client_validator.get(f"{INDEX}locations/").content.decode()
    assert "3 could not be saved into NeuroDB's locations, which the pages read." in page
    assert escape("No: 3 could not be saved into NeuroDB's locations") in page
    index = client_validator.get(INDEX).content.decode()
    assert escape("0 could not be saved; 3 could not be saved into NeuroDB's locations") in index
    # a run that did not succeed and met errors its counts do not hold is never complete either
    run = _run(
        "tpm_visits", status=SyncRun.Status.PARTIAL, details={"read": 5, "errors": [{**error, "count": 2}]}
    )
    assert runfacts.build("tpm_visits", run, None, (5, 5, 0)).complete == (False, "No: 2 could not be saved")


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


FUNDS_LINKED = {
    "by_number": 1234,
    "exact": 1200,
    "amended": 5,
    "pd_part": 9,
    "header": 20,
    "by_id": 12,
    "number_not_held": 20,
    "number_not_held_id_held": 15,
    "id_other": 343,
    "moved": 343,
    "vendor_compared_exact": 1190,
    "vendor_same_exact": 1180,
    "vendor_compared_by_id": 12,
    "vendor_same_by_id": 10,
}


def test_the_funds_lines_page_says_how_the_last_sync_linked_them_to_their_pd(client_validator, world):
    url = f"{INDEX}funds_reservations/"
    _run("funds_reservations", ago=3, rows_in=2, details={"read": 2, "datamart_count": 2})
    html = client_validator.get(url).content.decode()
    assert "Linked to their programme document" not in html  # a sync before the rule says nothing
    _run(
        "funds_reservations",
        rows_in=1286,
        details={"read": 1286, "datamart_count": 1286, "pd_link": FUNDS_LINKED},
    )
    said = raw.fr_link_words(FUNDS_LINKED, "funds_reservations")
    assert said == (
        "Linked to their programme document by reference number: 1,234 (exact 1,200; another amendment's number "
        "5; the PD part alone 9; the FR's header number 20). Of these, 0 carry the number of several PDs: each "
        "went to the PD whose dates cover it, else to the one that ends last. By eTools id only (no number): 12. "
        "Not linked: a number of a PD NeuroDB does not hold 20 (the eTools id names a PD NeuroDB holds on 15 of "
        "them); a number several PDs fit (another amendment's or the PD part alone) 0. The eTools id names "
        "another PD NeuroDB holds on 343 of the lines linked by number (the old rule put them there); moved to "
        "another PD by this sync: 343. The partner's vendor number matches on 1,180 of 1,190 lines linked by "
        "exact number, 10 of 12 by eTools id."
    )
    html = client_validator.get(url).content.decode()
    assert escape(said) in html
    assert 'href="?fr_link=number_not_held"' in html  # lists them
    assert tuple(raw.FR_VENDOR_WORDS) == FR_VENDOR_KINDS  # one word per kind the sync counts
    headers = raw.fr_link_words({"by_number": 2, "exact": 2, "id_other": 1}, "funds_reservation_headers")
    assert "header number" not in headers and "1 of the funds reservations linked by number" in headers


def test_the_funds_lines_whose_number_names_no_pd_neurodb_holds_are_listed(client_validator, world):
    """The same rule as the sync: the exact number (case and spaces aside), another amendment's, the PD part."""
    pd = world["running"]  # LEB/PD1
    for pk, number in enumerate(
        ("LEB/PD1", " leb/pd2 ", "LEB/PD9", "", "LEBA/PCA2020009/PD2020009-1"), start=1
    ):
        dm.FundsReservation.objects.create(
            datamart_id=pk,
            pd_reference_number=number,
            intervention=pd if pk == 1 else None,
            data={"id": pk, "pd_reference_number": number},
        )
    assert _shown(client_validator, "funds_reservations") == 5
    assert _shown(client_validator, "funds_reservations", fr_link="number_not_held") == 2
    response = client_validator.get(f"{INDEX}funds_reservations/", {"fr_link": "number_not_held"})
    assert response.context["shown"]["fr_link"] and not response.context["shown"]["pd_link"]
    assert "Their number names no PD NeuroDB holds" in response.content.decode()
    rows = _csv(client_validator.get(f"{INDEX}funds_reservations/rows.csv", {"fr_link": "number_not_held"}))
    assert sorted(r["pd_reference_number"] for r in rows) == ["LEB/PD9", "LEBA/PCA2020009/PD2020009-1"]
    assert _shown(client_validator, "funds_reservation_headers", fr_link="number_not_held") == 0
    assert not client_validator.get(f"{INDEX}partner_reports/").context["shown"]["fr_link"]


def test_a_funds_line_left_unlinked_says_why_on_the_list_its_page_and_the_csv(client_validator, world):
    """Its PD in NeuroDB: "several PDs fit" when its number fits several PDs NeuroDB holds (the sync links it
    to none of them), "not in NeuroDB" when its number, or its FR header's for a line without one, names none
    NeuroDB holds, "names none" when it carries neither. The page and the download read the programme
    documents and the FR headers once, whatever the number of lines."""
    partner = world["partner"]
    for etl_id, number in (("21", "LEBA/PCA2020003/PD2020003-1"), ("22", "LEBA/PCA2020004/PD2020003-2")):
        PCA.objects.create(etl_id=etl_id, partner=partner, number=number, title=number)
    dm.FundsReservationHeader.objects.create(
        datamart_id=1, fr_number="0400001", pd_reference_number="LEBA/PCA2020009/PD2020009-1"
    )
    lines = (("PD2020003", ""), ("", "0400001"), ("", ""), ("LEB/PD9", ""))
    for pk, (number, fr) in enumerate(lines, start=1):
        dm.FundsReservation.objects.create(
            datamart_id=pk, pd_reference_number=number, fr_number=fr, data={"id": pk, "fr_number": fr}
        )
    said = {"1": "several PDs fit", "2": "not in NeuroDB", "3": "names none", "4": "not in NeuroDB"}
    rows = _csv(client_validator.get(f"{INDEX}funds_reservations/rows.csv"))
    assert {r["record_id"]: r["pd_number_in_neurodb"] for r in rows} == said
    response = client_validator.get(f"{INDEX}funds_reservations/")
    columns = list(response.context["columns"])
    at, pd_at = columns.index("Record id"), columns.index("PD in NeuroDB")
    assert {r["cells"][at]: r["cells"][pd_at] for r in response.context["records"]} == said
    line = dm.FundsReservation.objects.get(datamart_id=1)
    page = client_validator.get(f"{INDEX}funds_reservations/{line.pk}/").content.decode()
    assert "Programme document: several PDs fit" in page
    page_queries = _queries(client_validator, f"{INDEX}funds_reservations/")
    csv_queries = _queries(client_validator, f"{INDEX}funds_reservations/rows.csv")
    dm.FundsReservation.objects.bulk_create(
        dm.FundsReservation(datamart_id=100 + i, pd_reference_number=f"PD20200{i:02}", data={"id": 100 + i})
        for i in range(40)
    )
    assert _queries(client_validator, f"{INDEX}funds_reservations/") == page_queries
    assert _queries(client_validator, f"{INDEX}funds_reservations/rows.csv") == csv_queries


def _rest_world() -> SyncRun:
    """Two partners, two programme documents and a trip as the eTools REST API sent them, and the read that
    kept them."""
    run = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_REST_RAW,
        target="rest_partners",
        status=SyncRun.Status.SUCCEEDED,
        started_at=timezone.now() - datetime.timedelta(minutes=5),
        finished_at=timezone.now() + datetime.timedelta(minutes=5),
        rows_in=2,
        rows_written=2,
        details={"read": 2, "kept": 2, "announced": 2, "removed": 0},
    )
    for pk, vendor, name in ((1, "V1", "Amel Association"), (2, "V2", "Akkar Network V1")):
        dm.RestRecord.objects.create(
            entity="rest_partners",
            record_key=str(pk),
            source_id=str(pk),
            reference=vendor,
            title=name,
            data={"id": pk, "vendor_number": vendor, "name": name},
        )
    for pk, number, partner in ((100, "LEB/PD1", 1), (101, "LEB/PD2", 2)):
        dm.RestRecord.objects.create(
            entity="rest_intervention_details",
            record_key=str(pk),
            source_id=str(pk),
            reference=number,
            parent_key=str(pk),
            data={"number": number, "partner_id": partner},
        )
    dm.RestRecord.objects.create(
        entity="rest_travels", record_key="5", source_id="5", reference="T5", title="T5", data={"id": 5}
    )
    return run


def test_the_rest_entities_are_listed_once_each_and_filter_as_their_page_says(client_validator, world):
    run = _rest_world()
    html = client_validator.get(INDEX).content.decode()
    rows = html.split('id="rest"', 1)[1]
    for name in raw.REST_ENTITIES:
        assert rows.count(f'<a class="nd-link" href="{INDEX}{name}/">') == 1, name
    assert html.count("Not read yet.") == len(raw.REST_ENTITIES) - 1  # the partners were read
    facts = runfacts.facts("rest_partners")
    assert (facts.run, facts.said_it_had, facts.received, facts.updated_by_sync) == (run, 2, 2, 2)
    assert facts.complete == (True, "Yes")
    assert _shown(client_validator, "rest_partners") == 2
    assert _shown(client_validator, "rest_partners", partner="V1") == 1  # the vendor number, never the title
    assert _shown(client_validator, "rest_partners", partner="2") == 1  # the eTools id
    assert _shown(client_validator, "rest_partners", q="Akkar") == 1  # a partner's name is its title
    assert _shown(client_validator, "rest_partners", record="1") == 1
    assert _shown(client_validator, "rest_partners", updated="last") == 2
    assert _shown(client_validator, "rest_partners", year=str(timezone.localdate().year)) == 2
    assert _shown(client_validator, "rest_intervention_details", pd="LEB/PD2") == 1
    assert _shown(client_validator, "rest_intervention_details", partner="1") == 1  # the partner it names
    assert _shown(client_validator, "rest_travels", q="T5") == 1
    page = client_validator.get(f"{INDEX}rest_partners/").content.decode()
    assert "Read from the eTools REST API" in page and "of another country" not in page
    pk = dm.RestRecord.objects.get(entity="rest_partners", record_key="1").pk
    assert client_validator.get(f"{INDEX}rest_partners/{pk}/").status_code == 200
    assert client_validator.get(f"{INDEX}rest_unknown/").status_code == 404
    rows = _csv(client_validator.get(f"{INDEX}rest_partners/rows.csv"))
    assert {r["record_id"] for r in rows} == {"1", "2"}
    # the record's own vendor number beside the one of the partner NeuroDB holds under its eTools id
    assert {(r["vendor_number (as received)"], r["vendor_number"]) for r in rows} == {
        ("V1", "V1"),
        ("V2", ""),
    }


def test_a_rest_record_says_what_neurodb_holds_under_its_etools_id(client_validator, world):
    """A programme document's record: the programme document NeuroDB holds under its eTools id, with the
    status the pages read (the REST sync writes it) and its partner; a partner's record: that partner; an
    agreement's, an engagement's or an action point's: the partner it names. One query a page or a chunk of the
    CSV, whatever the number of records."""
    ended = world["ended"]  # eTools id 12; its partner's is 1

    def add(entity: str, key: str, data: dict) -> dm.RestRecord:
        return dm.RestRecord.objects.create(entity=entity, record_key=key, source_id=key, data=data)

    pd_row = add("rest_interventions", "12", {"id": 12, "number": "LEB/PD2", "status": "active"})
    add("rest_interventions", "999", {"id": 999, "number": "LEB/PD9"})
    partner_row = add("rest_partners", "1", {"id": 1, "vendor_number": "V1"})
    add("rest_agreements", "10", {"id": 10, "partner": 1})
    add("rest_engagements", "7", {"id": 7, "partner": {"id": 1, "name": "Amel Association"}})
    add("rest_travels", "5", {"id": 5})
    page = client_validator.get(f"{INDEX}rest_interventions/{pd_row.pk}/").content.decode()
    pd_url = reverse("reports:programme_detail", args=[ended.pk])
    assert f'Programme document: <a class="nd-link" href="{pd_url}">LEB/PD2</a> (ended)' in page
    assert "Partner: Amel Association (vendor number V1)" in page

    def links(entity: str) -> dict[str, tuple[str, ...]]:
        columns = ("pd_number_in_neurodb", "pd_status_in_neurodb", "partner_in_neurodb", "vendor_number")
        rows = _csv(client_validator.get(f"{INDEX}{entity}/rows.csv"))
        return {r["record_id"]: tuple(r[c] for c in columns) for r in rows}

    assert links("rest_interventions") == {
        "12": ("LEB/PD2", "ended", "Amel Association", "V1"),
        "999": ("not in NeuroDB", "", "", ""),
    }
    assert links("rest_partners") == {"1": ("", "", "Amel Association", "V1")}
    assert links("rest_agreements") == {"10": ("", "", "Amel Association", "V1")}
    assert links("rest_engagements") == {"7": ("", "", "Amel Association", "V1")}
    assert links("rest_travels") == {"5": ("", "", "", "")}
    page = client_validator.get(f"{INDEX}rest_partners/{partner_row.pk}/").content.decode()
    assert "Programme document:" not in page and "Partner: Amel Association (vendor number V1)" in page
    trip = dm.RestRecord.objects.get(entity="rest_travels")
    page = client_validator.get(f"{INDEX}rest_travels/{trip.pk}/").content.decode()
    assert "Programme document:" not in page and "Partner:" not in page
    download = f"{INDEX}rest_interventions/rows.csv"
    few = _queries(client_validator, download)
    dm.RestRecord.objects.bulk_create(
        dm.RestRecord(entity="rest_interventions", record_key=str(n), source_id=str(n), data={"id": n})
        for n in range(2000, 2060)
    )
    assert _queries(client_validator, download) == few


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


def test_the_records_keep_short_values_on_one_line_and_wrap_long_texts(client_validator, world):
    """A reference, a date or a status never breaks across lines (the list scrolls sideways); a long text wraps
    in a column wide enough to read it, so a row stays a few lines high (the browser check found references cut
    in two and rows ten lines high)."""
    long = "# of children and caregivers reached with psychosocial support in schools"
    dm.ReportedIndicator.objects.filter(datamart_id=1).update(indicator=long)
    html = client_validator.get(f"{INDEX}partner_reports/").content.decode()
    table = html.split('class="nd-jobs nd-found__table nd-raw__records"', 1)[1].split("</table>", 1)[0]
    assert f'<td class="nd-raw__long">{escape(long)}</td>' in table
    # a one-word value (a reference, a number, a date) is marked so that a print never breaks it either
    assert '<td class="nd-raw__atom">LEB/PD1</td>' in table and 'class="nd-raw__long">LEB/PD1' not in table
    assert '<td class="nd-raw__atom">12</td>' in table
    # a date as shown is three words: it may wrap between them, so the room an indicator needs comes from it
    assert "<td>March 31, 2026</td>" in table and 'class="nd-raw__atom">March' not in table
    assert "<td>Amel Association</td>" in table  # a name of several words wraps between them
    css = (Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "admin.css").read_text()
    assert ".nd-raw__records th, .nd-raw__records td { white-space: nowrap; }" in css
    assert '.nd-raw__records th[scope="row"] { min-width: 0; max-width: 16rem; white-space: normal;' in css
    assert (
        ".nd-raw__records td.nd-raw__long { white-space: normal; min-width: 16rem; max-width: 28rem; }" in css
    )


def test_a_field_s_key_in_a_heading_may_break_after_its_underscores(client_validator, world):
    """PRP indicator reports printed: its headings financial_contribution_currency and the like are one long word
    each, so the list ran past the sheet and its last columns were cut. They may now break after each "_" (in
    print: on screen the list keeps them on one line and scrolls); a heading in words never breaks in a word."""
    data = {
        "pd_reference_number": "LEB/PD1",
        "report_type": "QPR",
        "report_number": 1,
        "financial_contribution_to_date": 57522.5,
        "financial_contribution_currency": "USD",
    }
    for pk in (1, 2):
        dm.DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key=str(pk), data=data)
    html = client_validator.get(f"{INDEX}prp_indicator_reports/").content.decode()
    table = html.split('class="nd-jobs nd-found__table nd-raw__records"', 1)[1].split("</table>", 1)[0]
    heads = re.findall(r'<th scope="col">(.*?)</th>', table)
    assert "financial_<wbr>contribution_<wbr>currency" in heads and "report_<wbr>number" in heads
    assert "Record id" in heads and "Vendor number" in heads and "Progress value" in heads
    assert not [h for h in heads if "<wbr>" in h and "_" not in h]
    assert re.sub(r"<wbr>", "", " ".join(heads)).count("<") == 0  # nothing else in a heading is markup


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


def test_a_download_that_takes_too_long_stops_cleanly_and_says_so(client_validator, world, monkeypatch):
    _slow(monkeypatch)
    monkeypatch.setattr(raw, "STREAM_TIMEOUT_MS", 1)
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 200
    (row,) = _csv(response)  # never a file that looks complete: its last row says it stopped
    assert row["figures_as_of"] == raw.STOPPED and not any(v for k, v in row.items() if k != "figures_as_of")
    assert dm.ReportedIndicator.objects.count() == 6  # the connection works on


def test_a_download_cut_part_way_holds_the_records_read_then_says_so(client_validator, world, monkeypatch):
    """Only the older records are slow: the file holds the newer ones, read before the cut, then the row saying
    it stopped there."""
    original = raw.base
    slow = (
        "CASE WHEN datamart_reportedindicator.datamart_id >= 5 THEN true ELSE pg_sleep(0.5) IS NOT NULL END"
    )
    monkeypatch.setattr(raw, "base", lambda name: original(name).extra(where=[slow]))
    monkeypatch.setattr(raw, "CSV_CHUNK", 2)
    monkeypatch.setattr(raw, "STREAM_TIMEOUT_MS", 300)
    response = client_validator.get(f"{INDEX}partner_reports/rows.csv")
    assert response.status_code == 200
    rows = _csv(response)
    assert [r["record_id"] for r in rows] == ["6", "5", ""]  # the newest first, then the cut
    assert rows[-1]["figures_as_of"] == raw.STOPPED
    assert dm.ReportedIndicator.objects.count() == 6


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
    warm = client.get(url, params)  # warm: the names, the choices
    if warm.streaming:
        b"".join(warm.streaming_content)  # read whole: its download slot is freed
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


def test_records_linked_to_a_programme_document_alone_cost_no_query_each(client_validator, world):
    """Their partner is the programme document's, read in the same query, on the page and in the CSV."""
    pd = world["running"]

    def add(name: str, start: int, n: int) -> None:
        if raw.typed(name):
            model = raw.model_of(name)
            model.objects.bulk_create(
                model(datamart_id=start + i, intervention=pd, data={"id": start + i}) for i in range(n)
            )
        else:
            dm.DatamartDocument.objects.bulk_create(
                dm.DatamartDocument(
                    dataset=name, record_key=str(start + i), intervention=pd, data={"id": start + i}
                )
                for i in range(n)
            )

    for name in ("funds_reservations", "pd_activities", "intervention_epd"):
        page, download = f"{INDEX}{name}/", f"{INDEX}{name}/rows.csv"
        add(name, 0, 5)
        few = (_queries(client_validator, page), _queries(client_validator, download))
        add(name, 100, 45)
        assert (_queries(client_validator, page), _queries(client_validator, download)) == few, name
        rows = _csv(client_validator.get(download))
        assert len(rows) == 50 and {r["partner_in_neurodb"] for r in rows} == {"Amel Association"}, name


def test_the_csv_and_the_record_page_make_a_fixed_number_of_queries(client_validator, world, monkeypatch):
    monkeypatch.setattr(raw, "CSV_CHUNK", 2)
    url = f"{INDEX}partner_reports/rows.csv"
    few = _queries(client_validator, url)
    _more_rows(world, 20)
    assert _queries(client_validator, url) == few  # the records come in chunks from one cursor
    row = dm.ReportedIndicator.objects.get(datamart_id=1)
    assert _queries(client_validator, f"{INDEX}partner_reports/{row.pk}/") <= ADMIN_OWN + PAGE_QUERIES
    # every source: their runs (2) and the moment of the counts kept for an hour; the eTools REST entities:
    # their runs (2), their counts (1), whether a read of the eTools REST API is running (1) and its latest
    # read (1: what it reads, and whether it reached each entity)
    assert _queries(client_validator, INDEX) <= ADMIN_OWN + 3 + 5
    with connection.cursor() as cursor:  # every download counted here freed its slot, the warm-up's too
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", [raw.RAW_CSV_LOCK]
        )
        assert cursor.fetchone()[0] == 0
