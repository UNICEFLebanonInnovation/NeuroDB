"""Office, emergency, output and partner type: no page offers these filters any more (the owner's decision of
21 Nov 2026: "Remove the filters in all pages: office, emergency, output, partner type").

* No filter bar has an Office, Emergency, CP output or Partner type control, and the other filters work as
  before. The columns that show an office, a partner type, a CP output or the emergency flag stay.
* An address that still carries one of them (an old bookmark, a link of the figure trail or of the Power BI
  comparison sheet):

  - Progress reports (partner type, emergency) and Partner monitoring (CP output, partner type, office,
    emergency) apply it, as the figure trail and the comparison sheet built from that address do, and say so
    on a line with a link that removes it; the filter bar keeps it (hidden) so Apply does not drop it silently;
  - Programme documents, Donors, Funds, Partners and Action points ignore it: their figures, links and
    downloads are those of the address without it.

* No regular page builds a link with one of them (Home, the pages' tabs and tiles, the action points' charts,
  Ask NeuroDB, the watch's detectors). Monitoring insights is tests/fmm/test_office_filter_removed.py; Compare
  with Power BI is tests/datamart/test_powerbi_filters_removed.py.

On the world of tests/reports/test_progress_reports_active_default.py, given offices, CP outputs, an SPD and a
government partner: LEB/PD21 (active, Partner Alpha, CSO, SPD, Zahle, "1.1 Protection services"), LEB/PD22
(signed, Partner Beta, CSO, Tripoli, "2.1 Learning"), LEB/PD23 (suspended, Partner Gamma, Government, Tripoli,
"2.1 Learning") and LEB/PD24 (ended, Partner Gamma).
"""

# ruff: noqa: F401, F811 (the fixtures come from the modules that define them)
from __future__ import annotations

import csv
import io
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.reports.test_progress_reports_active_default import world
from tests.web.parts import raw_get, text

pytestmark = pytest.mark.django_db

REMOVED = ("office", "emergency", "partner_type", "cp_output")
# a control of one of them in a form (a select, a search box, a radio), or the label of one
CONTROL = re.compile(
    r'<(?:select|input)\b(?![^>]*type="hidden")[^>]*\bname="(office|emergency|partner_type|cp_output)"'
    r'|<label[^>]*\bfor="f-(?:office|emergency|partner_type|cp-output)"'
)
# a link (or a link template, a form's hidden field, a script's address) carrying one of them
LINK = re.compile(
    r'[?&](?:amp;)?(office|emergency|partner_type|cp_output)=|name="(office|emergency|partner_type|cp_output)"'
)
NOTE = re.compile(r'<p[^>]*\bid="old-filters-note"[^>]*>(.*?)</p>', re.S)
HREF = re.compile(r'href="([^"]*)"')
TAGS = re.compile(r"<[^>]+>")
PR, PM = "/partner-reporting/", "/partner-monitoring/"
EVERY = {"LEB/PD21", "LEB/PD22", "LEB/PD23", "LEB/PD24"}


@pytest.fixture
def old_world(world):
    """The world with offices, CP outputs, an SPD and a government partner."""
    PartnerOrganization.objects.filter(name="Partner Gamma").update(partner_type="Government")
    for number, offices, kind, output in (
        ("LEB/PD21", ["Zahle"], "SPD", "1.1 Protection services"),
        ("LEB/PD22", ["Tripoli"], "PD", "2.1 Learning"),
        ("LEB/PD23", ["Tripoli"], "PD", "2.1 Learning"),
        ("LEB/PD24", ["Tripoli"], "PD", "2.1 Learning"),
    ):
        PCA.objects.filter(number=number).update(offices_set=offices, document_type=kind, cp_outputs=[output])
        dm.PDIndicator.objects.filter(intervention__number=number).update(cp_output=output)
    return world


def _validator() -> Client:
    user = User.objects.create_user(username="checker", email="checker@example.org", password="x-pass-123456")
    user.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=VALIDATORS))
    client = Client()
    client.force_login(user)
    return client


def _controls(html: str) -> set[str]:
    return {m.group(1) or "label" for m in CONTROL.finditer(html)}


def _links(html: str) -> set[str]:
    return {m.group(1) or m.group(2) for m in LINK.finditer(html)}


def _note(html: str) -> str:
    found = NOTE.search(html)
    assert found, "the page names the filter of the address"
    return " ".join(unescape(TAGS.sub(" ", found.group(1))).split())


def _note_link(html: str) -> dict[str, list[str]]:
    found = NOTE.search(html)
    href = unescape(HREF.findall(found.group(1))[-1])
    return parse_qs(urlsplit(href).query, keep_blank_values=True)


def _csv(response) -> list[dict[str, str]]:
    content = b"".join(response.streaming_content) if response.streaming else response.content
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def _reported(response) -> set[str]:
    return {r["pd_reference_number"] for r in response.context["data"]["reports"]}


def _monitored(client, **params) -> set[str]:
    """The programme documents of Program Performance's export for ``params`` (every year), of the running
    programme documents, written out (the page opens on the active ones alone since 25 Nov 2026)."""
    query = {"view": "table", "year": "all", "export": "csv", "pd_status": ["active", "signed", "suspended"]}
    found = raw_get(client, PM, {**query, **params})
    assert found.status_code == 200
    return {row["programme_document"] for row in _csv(found)}


# ------------------------------------------------------------------------------------- no filter bar offers them
REGULAR_PAGES = [
    PR,
    PR + "?tab=partners",
    PR + "?tab=locations",
    PR + "?tab=disaggregations",
    PM,
    PM + "?view=table",
    PM + "map/",
    "/programmes/",
    "/programmes/?tab=indicators",
    "/programmes/?tab=summary",
    "/programmes/?tab=overall",
    "/funds/",
    "/funds/?tab=contributions",
    "/funds/?tab=programmes",
    "/partners/",
    "/action-points/",
    "/donors/",
    "/field-monitoring/",
    "/field-monitoring/?tab=plan",
]


@pytest.mark.parametrize("url", REGULAR_PAGES)
def test_no_filter_bar_offers_office_emergency_output_or_partner_type(client_viewer, old_world, url):
    page = client_viewer.get(url)
    assert page.status_code == 200, url
    html = text(page)
    assert 'class="filter-bar' in html, url
    assert _controls(html) == set(), url
    assert "old-filters-note" not in html  # nothing in the address: nothing to name


def test_the_filter_options_are_no_longer_computed(old_world):
    from neurodb.datamart import funds, monitoring, reporting
    from neurodb.datamart import services as datamart
    from neurodb.partnerships import services as partnerships

    assert "partner_types" not in reporting.filter_options()
    pm = monitoring.filter_options(monitoring.Filters())
    assert "offices" not in pm and "partner_types" not in pm
    assert "partner_types" not in funds.filter_options()
    assert "partner_types" not in partnerships.partner_filter_options()
    assert "offices" not in datamart.action_points({})["options"]
    # Ask NeuroDB still matches an office in its programme documents lookup
    assert "offices" in partnerships.pd_filter_options()


def test_the_tables_keep_their_office_partner_type_output_and_emergency_columns(client_viewer, old_world):
    indicators = text(client_viewer.get("/programmes/", {"tab": "indicators"}))
    for heading in ("Office", "CP output", "Emergency"):
        assert f'<th scope="col">{heading}</th>' in indicators, heading
    assert "Zahle" in indicators and "1.1 Protection services" in indicators
    partners = text(client_viewer.get("/partners/"))
    assert "Government" in partners and "CSO" in partners
    table = text(client_viewer.get(PM + "?view=table&year=all&pd_status="))  # every PD status
    assert "2.1 Learning" in table


# ------------------------------------------------------------------------------------- the other filters work
def test_the_other_filters_still_work(client_viewer, old_world):
    beta = PartnerOrganization.objects.get(name="Partner Beta")
    alpha = PartnerOrganization.objects.get(name="Partner Alpha")
    assert _reported(client_viewer.get(PR, {"pd_status": "", "partner": str(beta.pk)})) == {"LEB/PD22"}
    assert _reported(client_viewer.get(PR, {"pd_status": "", "q": "Gamma"})) == {"LEB/PD23", "LEB/PD24"}
    assert _monitored(client_viewer, partner=str(alpha.pk)) == {"LEB/PD21"}
    assert _monitored(client_viewer, pd="LEB/PD22") == {"LEB/PD22"}
    documents = client_viewer.get("/programmes/", {"status": "signed"})
    assert [r["number"] for r in documents.context["rows"]] == ["LEB/PD22"]
    partners = client_viewer.get("/partners/", {"q": "Alpha"})
    assert [p.name for p in partners.context["page_obj"]] == ["Partner Alpha"]
    funds = client_viewer.get("/funds/", {"pd_status": "active", "tab": "contributions"})
    assert funds.context["f"].pd_statuses == ["active"]


# --------------------------------------------------------------------------- an old address: applied and named
@pytest.mark.parametrize(
    ("params", "expected", "words"),
    [
        ({"partner_type": "Government"}, {"LEB/PD23", "LEB/PD24"}, "Partner type: Government"),
        ({"emergency": "yes"}, {"LEB/PD21"}, "Emergency indicator: Yes"),
        ({"emergency": "no", "partner_type": "CSO"}, {"LEB/PD22"}, "Emergency indicator: No"),
    ],
)
def test_progress_reports_applies_an_old_filter_and_names_it_with_a_link_that_removes_it(
    client_viewer, old_world, params, expected, words
):
    page = client_viewer.get(PR, {"pd_status": "", **params})
    assert _reported(page) == expected
    html = text(page)
    note = _note(html)
    assert note.startswith("This link also filters by") and words in note
    removed = _note_link(html)
    assert not set(removed) & set(REMOVED) and removed["pd_status"] == [""]  # the rest of the filter kept
    assert _reported(client_viewer.get(PR, removed)) == EVERY
    # no control offers it, but the filter bar keeps it, so Apply does not drop it without a word
    assert _controls(html) == set()
    for key, value in params.items():
        assert f'<input type="hidden" name="{key}" value="{value}">' in html, key
    # every tab, and the downloads, keep reading it
    partners = client_viewer.get(PR, {"pd_status": "", "tab": "partners", **params})
    assert words in _note(text(partners))
    exported = raw_get(client_viewer, PR, {"pd_status": "", "export": "csv", **params})
    assert {row["programme_document"] for row in _csv(exported)} == expected


def test_progress_reports_check_line_follows_the_old_filter_the_page_applies(roles, old_world):
    """The figure trail and the Power BI comparison sheet read the page's address: they and the page give the
    same numbers."""
    html = text(_validator().get(PR, {"pd_status": "", "partner_type": "Government"}))
    trail = re.search(r'<a href="([^"]+)" data-check-trail>', html)
    assert trail and "partner_type=Government" in unescape(trail.group(1))
    compare = re.search(r"<form[^>]*data-check-compare>(.*?)</form>", html, re.S).group(1)
    assert '<input type="hidden" name="partner_type" value="Government">' in compare


@pytest.mark.parametrize(
    ("params", "expected", "words"),
    [
        ({"office": "Zahle"}, {"LEB/PD21"}, "Office: Zahle"),
        ({"cp_output": "Learning"}, {"LEB/PD22", "LEB/PD23"}, "CP output: Learning"),
        ({"partner_type": "Government"}, {"LEB/PD23"}, "Partner type: Government"),
        ({"emergency": "yes"}, {"LEB/PD21"}, "Emergency indicator: Yes"),
    ],
)
def test_partner_monitoring_applies_an_old_filter_and_names_it_with_a_link_that_removes_it(
    client_viewer, old_world, params, expected, words
):
    assert _monitored(client_viewer) == {"LEB/PD21", "LEB/PD22", "LEB/PD23"}  # the running ones, written out
    assert _monitored(client_viewer, **params) == expected
    for view in ({}, {"view": "table", "year": "all"}):
        html = text(client_viewer.get(PM, {**view, **params}))
        note = _note(html)
        assert note.startswith("This link also filters by") and words in note
        removed = _note_link(html)
        assert not set(removed) & set(REMOVED)
        assert _controls(html) == set()
        for key, value in params.items():
            assert f'<input type="hidden" name="{key}" value="{value}">' in html, key
    # the map, which reads the same filter
    assert words in _note(text(client_viewer.get(PM + "map/", params)))


def test_partner_monitoring_check_line_follows_the_old_filter_the_page_applies(roles, old_world):
    html = text(_validator().get(PM, {"view": "table", "year": "all", "office": "Zahle"}))
    trail = re.search(r'<a href="([^"]+)" data-check-trail>', html)
    assert trail and "office=Zahle" in unescape(trail.group(1))


# -------------------------------------------------------------------------------- an old address: ignored
def test_programme_documents_ignore_an_old_office_or_emergency(client_viewer, old_world):
    page = client_viewer.get("/programmes/", {"office": "Zahle"})
    assert {r["number"] for r in page.context["rows"]} == EVERY
    html = text(page)
    assert "old-filters-note" not in html and _links(html) == set()
    every = {
        i["programme_document"]
        for i in client_viewer.get("/programmes/", {"tab": "indicators"}).context["page_obj"]
    }
    for params in ({"emergency": "yes"}, {"emergency": "no"}, {"office": "Zahle"}):
        page = client_viewer.get("/programmes/", {"tab": "indicators", **params})
        assert {i["programme_document"] for i in page.context["page_obj"]} == every, params
        assert _links(text(page)) == set(), params
        exported = raw_get(client_viewer, "/programmes/", {"tab": "indicators", "export": "csv", **params})
        assert {row["programme_document"] for row in _csv(exported)} == every, params
    for tab in ("summary", "overall"):
        assert _links(text(client_viewer.get("/programmes/", {"tab": tab, "office": "Zahle"}))) == set(), tab


def test_donors_funds_and_partners_ignore_an_old_office_or_partner_type(client_viewer, old_world):
    donors = client_viewer.get("/donors/", {"office": "Zahle"})
    assert donors.context["data"]["count"] == 4 and _links(text(donors)) == set()
    for tab in ("reservations", "contributions", "programmes"):
        funds = client_viewer.get("/funds/", {"tab": tab, "partner_type": "Government"})
        assert funds.context["f"].partner_types == [], tab
        assert _links(text(funds)) == set(), tab
    partners = client_viewer.get("/partners/", {"partner_type": "Government"})
    assert {p.name for p in partners.context["page_obj"]} >= {
        "Partner Alpha",
        "Partner Beta",
        "Partner Gamma",
    }
    assert _links(text(partners)) == set()


@pytest.fixture
def action_points(old_world):
    for n, office in enumerate(("Zahle", "Tripoli"), start=1):
        dm.ActionPoint.objects.create(
            datamart_id=n, source_id=600 + n, reference_number=f"AP/60{n}", status="open", office=office,
            section="Child Protection", description=f"Follow up {n}",
        )  # fmt: skip


def test_action_points_ignore_an_old_office(client_viewer, action_points):
    page = client_viewer.get("/action-points/", {"office": "Zahle"})
    assert {p.reference_number for p in page.context["page_obj"]} == {"AP/601", "AP/602"}
    html = text(page)
    assert _controls(html) == set() and _links(html) == set()
    report = client_viewer.get("/action-points/report/", {"office": "Zahle"})
    assert report.context["total"] == 2 and "Office: Zahle" not in text(report)
    exported = raw_get(client_viewer, "/action-points/", {"office": "Zahle", "export": "csv"})
    assert {row["reference"] for row in _csv(exported)} == {"AP/601", "AP/602"}
    # the other filters still work
    narrowed = client_viewer.get("/action-points/", {"q": "AP/601"})
    assert [p.reference_number for p in narrowed.context["page_obj"]] == ["AP/601"]


def test_the_open_by_office_chart_stays_without_a_link_to_an_office(client_viewer, action_points):
    html = text(client_viewer.get("/action-points/"))
    chart = re.search(r'<div[^>]*\bid="ap-chart-office"[^>]*>', html)
    assert chart and "data-href-template" not in chart.group(0)
    assert re.search(r'<div[^>]*\bid="ap-chart-section"[^>]*data-href-template="[^"]*section=', html)


# ------------------------------------------------------------------------------- no regular page links them
@pytest.mark.parametrize(
    "url", ["/home/", reverse("assistant:ask"), reverse("watch:for_you"), *REGULAR_PAGES]
)
def test_no_regular_page_builds_a_link_with_them(client_viewer, action_points, url):
    page = client_viewer.get(url)
    assert page.status_code == 200, url
    assert _links(text(page)) == set(), url


def test_the_watchs_detectors_never_link_with_them():
    root = Path(__file__).resolve().parents[2] / "neurodb" / "watch"
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for call in re.findall(r"\blink\((.*?)\)", source, re.S):
            for key in REMOVED:
                assert not re.search(rf"\b{key}\s*=", call), (path.name, call)
