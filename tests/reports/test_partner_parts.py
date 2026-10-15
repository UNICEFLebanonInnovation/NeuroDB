"""The partner page in parts (SPEC7 §3.3-3.4 for the partner profile; STAGEA "Partner profile").

The partner page answers its frame at once (the header, the deleted notice, the knowledge base documents) with
one placeholder, ``datamart``: everything from the key figures to the last list. Those blocks stand between one
another on the page and read one computation, the partner's eTools Datamart figures (``partner_datamart``: model
instances, never stored for other workers), with its programme documents (``partner_profile``) and its
ActivityInfo records (``partner_activityinfo``, which the key figures count and the ActivityInfo panel lists).
Inside it, ``fmm`` (the Monitoring insights panel, its own computation) when Monitoring insights is on and a visit
ever monitored the partner. These tests check the frame without any figure work, each part, and that the page
put together from its parts is the page drawn at once (``_eager=1``), for every kind of partner and user. They
also pin the two quick wins of ``partner_datamart``: the progress reports read once (not three times), and the
field monitoring ratings counted by the database.

On ``world``: Amel (an active programme document with an indicator and two progress reports, an engagement,
two open action points, assessments, findings, a third-party visit, a HACT year, a staff trip, two ActivityInfo
records under its own name, a Monitoring insights visit and a knowledge base document), an organisation with
nothing, and one deleted in eTools.
"""

from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.donors.models import DonorAccount
from neurodb.facts.services import partners as partner_facts
from neurodb.fmm import services as fmm_services
from neurodb.fmm.models import Visit
from neurodb.knowledge.models import Document, Link
from neurodb.partnerships import linking
from neurodb.partnerships import services as partnerships
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerLink, PartnerOrganization
from neurodb.reports import views
from tests.reports.test_cross_page_fixes import AK, D, indicator, report
from tests.web.parts import (
    HX,
    assemble,
    eager,
    executable,
    get_part,
    normalise,
    placeholder_attributes,
    placeholders,
    raw_get,
    text,
)

pytestmark = pytest.mark.django_db

DATAMART_JSON = "partner-datamart-data"
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# the suite's day (tests/conftest.py SITE_NOW), not the real clock: this is read at import, before it travels
TODAY = datetime.date(2026, 10, 15)
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame warm is 7: the request's
# floor (session, user, donor check, user role, the navigation's current year), the partner and the knowledge base
# documents that mention it; cold it also reads the rest of the navigation (4). The datamart part (37) reads the
# partner's figures: its programme documents, the Datamart blocks, its ActivityInfo records and whether a visit
# monitored it; the Monitoring insights panel (12) its own.
SHELL_QUERIES_WARM = 9
# Cold the menu also reads the ActivityInfo state and its sources, kept per worker (SPEC_HOME §4): 17.
SHELL_QUERIES_COLD = 19
PART_QUERIES = {"datamart": 39, "fmm": 14}


@pytest.fixture
def world(hierarchy, settings):
    """Amel with every block of the page, an organisation with nothing and one deleted in eTools."""
    settings.FMM_ENABLED = True
    amel = PartnerOrganization.objects.create(
        etl_id="41",
        name="Partner A",  # the name it reports under in ActivityInfo (the conftest facts): linked by name
        short_name="Amel",
        partner_type="Civil Society Organization",
        cso_type="National",
        vendor_number="2500000041",
        total_ct_cy=Decimal("1000"),
        total_ct_cp=Decimal("5000"),
        shared_with=["UNHCR"],
    )
    pd = PCA.objects.create(
        etl_id="410", partner=amel, partner_name=amel.name, number="LEB/PCA-AMEL", title="Protection",
        status="active", start=D(2026, 1, 1), end=D(2026, 12, 31), document_type="PD",
    )  # fmt: skip
    indicator(pd, 741, "# of girls enrolled", [AK])
    q1, q2 = (D(2026, 1, 1), D(2026, 3, 31)), (D(2026, 4, 1), D(2026, 6, 30))
    report(pd, 741, "# of girls enrolled", "AM-1", "QPR1", *q1, "Accepted", {AK: 30})
    report(pd, 741, "# of girls enrolled", "AM-2", "QPR2", *q2, "Submitted", {AK: 5})
    engagement = dm.AuditEngagement.objects.create(
        datamart_id=4101, partner=amel, reference_number="LEB/2026/SC/41", engagement_type="sc",
        status="final", start_date=TODAY, financial_findings=Decimal("150"),
    )  # fmt: skip
    dm.AuditFinding.objects.create(
        datamart_id=4101, partner=amel, engagement=engagement, reference_number="LEB/2026/SC/41",
        title="Ineligible expenditure", amount=Decimal("150"),
    )  # fmt: skip
    for n, due in ((1, TODAY - datetime.timedelta(days=3)), (2, TODAY + datetime.timedelta(days=9))):
        dm.ActionPoint.objects.create(
            datamart_id=4100 + n, partner=amel, intervention=pd, reference_number=f"LEB/2026/{n}/APD",
            status="open", description=f"Follow up {n}", due_date=due, related_module="audit",
        )  # fmt: skip
    dm.PartnerAssessment.objects.create(datamart_id=4101, partner=amel, type="Micro Assessment", rating="Low")
    dm.PSEAAssessment.objects.create(datamart_id=4101, partner=amel, status="final", overall_rating=3)
    for n, rating, day in ((1, "On Track", 1), (2, "Off Track", 20), (3, "", 40), (4, "On Track", 60)):
        dm.MonitoringFinding.objects.create(
            datamart_id=4100 + n, partner=amel, monitoring_activity=f"MA-4{n}", overall_finding_rating=rating,
            start_date=TODAY - datetime.timedelta(days=day + 1), end_date=TODAY - datetime.timedelta(days=day),
        )  # fmt: skip
    dm.TPMVisit.objects.create(
        datamart_id=4101, partner=amel, reference_number="TPM/41", status="assigned", start_date=TODAY
    )
    dm.PartnerHACTYear.objects.create(
        datamart_id=4101, partner=amel, year=TODAY.year, risk_rating="Low", cash_transfers=Decimal("1000"),
        pv_required=2, pv_completed=1,
    )  # fmt: skip
    dm.ProgrammaticVisit.objects.create(
        datamart_id=4101, partner=amel, travel_type="Programmatic Visit", date=TODAY
    )
    linking.link_activityinfo_partners()  # "Partner A" -> Amel; "Partner B" is nobody's
    Visit.objects.create(
        key="4101", label="Visit 4101", partner_ids=[amel.pk], start_date=TODAY, end_date=TODAY,
        visit_date=TODAY, status="completed", status_group="reported", rating="on_track",
        refreshed_at=timezone.now(),
    )  # fmt: skip
    document = Document.objects.create(title="Amel field report", text="Amel", status=Document.Status.READY)
    Link.objects.create(document=document, kind=Link.Kind.PARTNER, object_id=amel.pk, label=amel.name)
    empty = PartnerOrganization.objects.create(etl_id="42", name="Empty Org", partner_type="Government")
    deleted = PartnerOrganization.objects.create(
        etl_id="43", name="Gone Org", partner_type="Civil Society Organization", deleted_flag=True
    )
    return {"amel": amel, "pd": pd, "empty": empty, "deleted": deleted}


def _url(partner) -> str:
    return reverse("reports:partner_profile", args=[partner.pk])


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the partner page raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the shell computed figures")

    monkeypatch.setattr(partnerships, "partner_profile", refuse)
    monkeypatch.setattr(datamart, "partner_datamart", refuse)
    monkeypatch.setattr(datamart, "progress_reports", refuse)
    monkeypatch.setattr(datamart, "visits_chart", refuse)
    monkeypatch.setattr(partner_facts, "partner_activityinfo", refuse)
    monkeypatch.setattr(fmm_services, "partner_summary", refuse)
    monkeypatch.setattr(views, "_fmm_panel", refuse)
    monkeypatch.setattr(views, "_fmm_partner_seen", refuse)


def _json_blocks(html: str) -> dict[str, object]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a chart JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
@pytest.mark.parametrize("who", ["amel", "deleted"])
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, world, monkeypatch, who):
    from django.core.cache import caches

    caches["shared"].clear()  # nothing stored (nothing ever is on this page)
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(_url(world[who]))
    assert response.status_code == 200
    html = text(response)
    assert f"<title>{world[who].name}" in html and ">Partners</a>" in html  # the header and its crumbs
    assert ("marked as deleted in eTools" in html) == (who == "deleted")
    assert ("In the knowledge base" in html and "Amel field report" in html) == (who == "amel")
    assert list(placeholders(html)) == ["datamart"]
    assert f'id="{DATAMART_JSON}"' not in html and 'id="chart-data"' not in html
    for figure in ("Active documents", "Programmatic visits by year", "Partner reporting", "LEB/PCA-AMEL"):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholder_carries_the_page_address_and_keeps_its_room(client_viewer, world):
    amel = world["amel"]
    found = placeholder_attributes(text(client_viewer.get(_url(amel))))
    assert list(found) == ["datamart"]
    attrs = found["datamart"]
    assert attrs["hx-get"] == f"{_url(amel)}?_part=datamart"  # no filter on this page: nothing else to carry
    assert (attrs["data-part-priority"], attrs["class"]) == ("high", "part part--tiles")
    assert "data-part-group" not in attrs and "data-part-lazy" not in attrs
    # the panels' ids stay anchors (Ask NeuroDB links to #activityinfo-reporting)
    anchors = attrs["data-part-anchors"].split()
    assert {"activityinfo-reporting", "etools-reporting", "partner-", "fmm-partner"} <= set(anchors)
    # the room of the blocks, on a desktop, a tablet or narrow window and a phone, as measured on the x20 world
    assert attrs["style"] == "--part-h: 7500px; --part-h-sm: 12400px; --part-h-md: 7500px"
    # a stray query string changes nothing: the part's address is the page's own
    other = placeholder_attributes(text(client_viewer.get(_url(amel) + "?tab=x")))
    assert other["datamart"]["hx-get"] == f"{_url(amel)}?_part=datamart"


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(client_viewer, world, django_assert_max_num_queries):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        response = client_viewer.get(_url(world["amel"]))
    assert response.status_code == 200 and "datamart" in placeholders(text(response))
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        response = client_viewer.get(_url(world["amel"]))
    assert response.status_code == 200 and "datamart" in placeholders(text(response))


@pytest.mark.shell
def test_an_unknown_partner_is_a_404_frame_and_part(client_viewer, world):
    url = reverse("reports:partner_profile", args=[987654])
    assert client_viewer.get(url).status_code == 404
    assert client_viewer.get(url + "?_part=datamart", **HX).status_code == 404


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("name", ["datamart", "fmm"])
def test_each_part_answers_its_wrapper(client_viewer, world, name):
    response = get_part(client_viewer, _url(world["amel"]), name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {"datamart": ("Active documents", "LEB/PCA-AMEL", "ActivityInfo · reporting history"),
                "fmm": ('id="fmm-partner"', "FM visits in")}[name]  # fmt: skip
    assert all(item in html for item in expected), expected
    assert (f'id="{DATAMART_JSON}"' in html) == (name == "datamart")  # the one chart JSON, in its own part
    assert 'id="chart-data"' not in html and 'data-source="chart-data"' not in html
    assert executable(html) == []  # nothing that would run once swapped in, its partials' neither


def _key_figures(client, partner) -> dict[str, str]:
    html = text(get_part(client, _url(partner), "datamart"))
    return dict(
        re.findall(
            r'<div class="kpi__label">([^<]+)</div>\s*<div class="kpi__value">(?:<a [^>]*>)?([^<]*)', html
        )
    )


def test_the_key_figures_are_the_partner_s(client_viewer, world):
    """The assembled page and the eager page draw the same part template, so their equivalence cannot catch a
    wrong figure in it: the key figures themselves are checked here. Then Amel gets an ended and a closed
    programme document (in All documents only), a draft (in neither), two more open action points and a
    completed one (not open), so no two of these tiles show the same figure: a tile that showed another's
    figure would fail."""
    amel = world["amel"]
    tiles = _key_figures(client_viewer, amel)
    assert tiles["Active documents"] == "1" and tiles["All documents"] == "1"
    assert tiles["ActivityInfo records"] == "2" and tiles["Open action points"] == "2"
    for n, status in ((1, "ended"), (2, "closed"), (3, "draft")):
        PCA.objects.create(
            etl_id=f"41{n}", partner=amel, partner_name=amel.name, number=f"LEB/PCA-AMEL-{n}", title="Education",
            status=status, start=D(2025, 1, 1), end=D(2025, 12, 31), document_type="PD",
        )  # fmt: skip
    for n, status in ((3, "open"), (4, "in_progress"), (5, "completed")):
        dm.ActionPoint.objects.create(
            datamart_id=4100 + n, partner=amel, reference_number=f"LEB/2026/{n}/APD", status=status,
            description=f"Follow up {n}", due_date=TODAY + datetime.timedelta(days=20), related_module="fm",
        )  # fmt: skip
    tiles = _key_figures(client_viewer, amel)
    assert (tiles["Active documents"], tiles["All documents"]) == ("1", "3")
    assert (tiles["ActivityInfo records"], tiles["Open action points"]) == ("2", "4")


def test_the_monitoring_insights_panel_is_a_part_inside_the_figures(client_viewer, world, settings):
    url = _url(world["amel"])
    inner = placeholder_attributes(text(get_part(client_viewer, url, "datamart")))
    assert list(inner) == ["fmm"]
    attrs = inner["fmm"]
    assert attrs["hx-get"] == f"{url}?_part=fmm" and attrs["data-part-priority"] == "low"
    assert attrs["data-part-anchors"] == "fmm-partner"
    assert attrs["style"] == "--part-h: 150px; --part-h-sm: 300px; --part-h-md: 165px"
    # a partner no visit monitored keeps no room for it, and nothing asks for it when Monitoring insights is off
    assert placeholders(text(get_part(client_viewer, _url(world["empty"]), "datamart"))) == {}
    settings.FMM_ENABLED = False
    figures = text(get_part(client_viewer, url, "datamart"))
    assert placeholders(figures) == {} and "/fmm/" not in figures
    assert client_viewer.get(f"{url}?_part=fmm", **HX).status_code == 404  # no such part then


@pytest.mark.parametrize("name", ["datamart", "fmm"])
def test_each_part_stays_within_its_query_ceiling(client_viewer, world, name, django_assert_max_num_queries):
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(f"{_url(world['amel'])}?_part={name}", **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)


@pytest.mark.parametrize("name", ["datamart", "fmm"])
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    response = client.get(f"{_url(world['amel'])}?_part={name}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_is_sent_to_sign_in(client, world):
    url = _url(world["amel"])
    response = client.get(f"{url}?_part=datamart", **{**HX, "HTTP_HX_CURRENT_URL": f"http://testserver{url}"})
    assert response.status_code == 200 and "Active documents" not in text(response)
    assert response["HX-Redirect"] == reverse("account_login") + f"?next={url}"


def test_the_admin_link_is_drawn_for_whoever_asks(client, admin_user, viewer, world):
    """The part is drawn for the person asking (nothing on this page is stored for anyone else): the
    ActivityInfo link editor is offered to an administrator, never to a viewer."""
    url = _url(world["amel"])
    client.force_login(admin_user)
    assert "Edit the link" in text(get_part(client, url, "datamart"))
    client.force_login(viewer)
    assert "Edit the link" not in text(get_part(client, url, "datamart"))


def test_nothing_is_stored_and_the_frame_never_draws_figures(client_viewer, world):
    """``partner_datamart`` returns model instances: the page stores nothing in the shared cache, so a second
    visit asks for its figures again, and they are computed again."""
    from django.core.cache import caches

    url = _url(world["amel"])
    get_part(client_viewer, url, "datamart")
    get_part(client_viewer, url, "fmm")
    assert not caches["shared"]._cache  # LocMem in the tests: nothing went in
    assert list(placeholders(text(raw_get(client_viewer, url)))) == ["datamart"]


def test_an_edit_in_the_admin_shows_at_once(client, admin_user, world):
    """Fresh after edit: an administrator links another ActivityInfo name to the partner (Admin → ActivityInfo
    partner links); the key figures and the ActivityInfo panel count its records at the next request."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    url = _url(world["amel"])
    before = get_part(client, url, "datamart")
    assert before.context["activityinfo"]["records"] == 2 and "2 records" in text(before)
    link = PartnerLink.objects.get(label="Partner B")
    assert link.partner_id is None
    response = client.post(
        reverse("admin:etools_partnerlink_change", args=[link.pk]), {"partner": world["amel"].pk}
    )
    assert response.status_code == 302, text(response)[:2000]
    after = get_part(client, url, "datamart")
    assert after.context["activityinfo"]["records"] == 4 and "4 records" in text(after)
    assert after.context["activityinfo"]["labels"] == ["Partner A", "Partner B"]


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [  # (partner, signed in as, Monitoring insights on)
    ("amel", "viewer", True),
    ("amel", "admin", True),
    ("amel", "viewer", False),
    ("empty", "viewer", True),
    ("deleted", "admin", True),
]


@pytest.mark.parametrize(("who", "user", "fmm_on"), MATRIX)
def test_the_assembled_page_is_the_eager_page(client, admin_user, viewer, world, settings, who, user, fmm_on):
    settings.FMM_ENABLED = fmm_on
    client.force_login(admin_user if user == "admin" else viewer)
    url = _url(world[who])
    assembled = assemble(client, url)
    expected = ["datamart", "fmm"] if who == "amel" and fmm_on else ["datamart"]
    assert list(assembled.parts) == expected
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client, url)
    assert drawn.status_code == 200
    # no link of a part (nor of the eager page) carries _part, and no part sends anything that would run
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link and "_eager" not in link for link in LINK.findall(text(drawn)))
    assert placeholders(text(drawn)) == {}  # every part drawn
    assert normalise(assembled.html) == normalise(text(drawn))
    # one chart JSON, unique in the page, the same as the eager page's
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone and DATAMART_JSON in together and "chart-data" not in together
    figures = _json_blocks(text(assembled.parts["datamart"]))
    assert figures == {DATAMART_JSON: alone[DATAMART_JSON]}


def test_the_chart_json_is_the_page_s_chart_data(client_viewer, world):
    """The JSON the part sends is the page's chart data as it was (one block, with the same keys), now under
    the part's own id, which every chart of the part names."""
    amel = world["amel"]
    response = get_part(client_viewer, _url(amel), "datamart")
    html = text(response)
    sent = _json_blocks(html)[DATAMART_JSON]
    profile, extra = partnerships.partner_profile(amel), datamart.partner_datamart(amel)
    activityinfo = partner_facts.partner_activityinfo(amel)
    assert sent == json.loads(
        json.dumps(
            {
                "visits_by_year": datamart.visits_chart(
                    extra["staff_visits_by_year"] or profile["visits_by_year"],
                    extra["monitoring_visits_by_year"],
                ),
                "engagement_counts": profile["engagement_counts"],
                "activityinfo_by_year": activityinfo["by_year"],
                "etools_reports_by_year": extra["reports_by_year"],
            }
        )
    )
    assert sent["visits_by_year"]["series"]["UNICEF staff trips"] == [1]
    assert sent["etools_reports_by_year"] == [["2026", 2]] and sent["activityinfo_by_year"] == [["2026", 2]]
    charts = re.findall(r'<div class="chart[^"]*" data-module="charts"[^>]*>', html)
    assert len(charts) == 3 and all(f'data-source="{DATAMART_JSON}"' in chart for chart in charts)


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world):
    """*Show the figures* (no JavaScript) leads to the page drawn at once: every part in place, the panel
    of Monitoring insights too, no grey shapes left and no link back to the same page."""
    url = _url(world["amel"])
    frame = text(client_viewer.get(url))
    shown = re.search(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert shown, "the frame offers the figures without JavaScript"
    address = url + shown.group(1).replace("&amp;", "&")
    assert address == f"{url}?_eager=1"
    page = text(client_viewer.get(address))
    assert placeholders(page) == {} and "part__skeleton" not in page and "Show the figures" not in page
    blocks = (
        "Active documents",
        'id="fmm-partner"',
        "Partner reporting",
        "LEB/PCA-AMEL",
        "Amel field report",
    )
    for block in blocks:
        assert block in page, block
    assert "_eager" not in "".join(LINK.findall(page))


# ------------------------------------------------------------------------------------------------ quick wins
def test_partner_datamart_reads_the_progress_reports_once_with_the_same_figures(world, monkeypatch):
    """The summary, the list and the chart of the partner's progress reports come from one reading (they
    were three), with the figures the three readings gave."""
    amel = world["amel"]
    reported = amel.reported_indicators.exclude(intervention__status__in=pd_statuses.NOT_A_PARTNERSHIP)
    expected = {
        "reporting": datamart.reporting_summary(reported),
        "reports": list(datamart.progress_reports(reported)[:10]),
        "reports_by_year": datamart.reports_by_year(reported),
    }
    calls = []
    real = datamart.progress_reports

    def counted(queryset):
        calls.append(1)
        return real(queryset)

    monkeypatch.setattr(datamart, "progress_reports", counted)
    data = datamart.partner_datamart(amel)
    assert len(calls) == 1
    assert {key: data[key] for key in expected} == expected
    summary = data["reporting"]
    assert (summary["reports"], summary["accepted"], summary["submitted"]) == (2, 1, 1)
    assert data["reports_by_year"] == [("2026", 2)]


def test_partner_datamart_reads_more_than_ten_progress_reports_once(world):
    """With more reports than the list shows (ten), the summary and the chart still count every report, from the
    one reading."""
    amel, pd = world["amel"], world["pd"]
    for month in range(1, 13):  # twelve monthly reports of 2025, before the world's two of 2026
        report(pd, 741, "# of girls enrolled", f"AM-{month + 2}", f"HR{month}", D(2025, month, 1),
               D(2025, month, 28), "Accepted", {AK: 1})  # fmt: skip
    reported = amel.reported_indicators.exclude(intervention__status__in=pd_statuses.NOT_A_PARTNERSHIP)
    data = datamart.partner_datamart(amel)
    assert data["reporting"] == datamart.reporting_summary(reported) and data["reporting"]["reports"] == 14
    assert data["reports"] == list(datamart.progress_reports(reported)[:10]) and len(data["reports"]) == 10
    assert data["reports_by_year"] == datamart.reports_by_year(reported) == [("2025", 12), ("2026", 2)]


def test_partner_datamart_counts_the_finding_ratings_in_the_database(world):
    """The field monitoring ratings are counted by one grouped query instead of reading every finding, with
    the same counts, in the same order where the ratings' latest findings differ in date (latest end date
    first); ratings tied on that date come in no particular order, as they did."""
    amel = world["amel"]
    findings = dm.MonitoringFinding.objects.filter(partner=amel)
    expected = dict(Counter(f.overall_finding_rating or "—" for f in findings))  # the old way
    assert expected == {"On Track": 2, "Off Track": 1, "—": 1}
    with CaptureQueriesContext(connection) as queries:
        counted = datamart._finding_ratings(findings)
    assert counted == expected and list(counted) == list(expected)
    assert len(queries.captured_queries) == 1 and "GROUP BY" in queries.captured_queries[0]["sql"]
    assert datamart.partner_datamart(amel)["finding_ratings"] == expected
    # undated findings come first, as the database sorts them
    dm.MonitoringFinding.objects.create(datamart_id=4199, partner=amel, overall_finding_rating="Constrained")
    findings = dm.MonitoringFinding.objects.filter(partner=amel)
    old = dict(Counter(f.overall_finding_rating or "—" for f in findings))
    assert list(datamart._finding_ratings(findings).items()) == list(old.items())
    assert datamart._finding_ratings(dm.MonitoringFinding.objects.none()) == {}
    # two ratings whose latest findings end the same day: the same counts, in either order
    latest = findings.exclude(end_date=None).order_by("-end_date").first()
    dm.MonitoringFinding.objects.create(
        datamart_id=4198, partner=amel, overall_finding_rating="Not Monitored", end_date=latest.end_date
    )
    findings = dm.MonitoringFinding.objects.filter(partner=amel)
    tied = datamart._finding_ratings(findings)
    assert tied == dict(Counter(f.overall_finding_rating or "—" for f in findings))
    assert tied["Not Monitored"] == 1 and sum(tied.values()) == findings.count() == 6
