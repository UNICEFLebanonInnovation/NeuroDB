"""Every PD status filter opens on Active (the owner's decision of 25 Nov 2026: "PD status field filter in all
pages should be 'Active' value only by default").

* **The pages**: Partner monitoring (and its map), Funds and the HACT visit plan of Field monitoring, as
  Progress reports since 21 Nov 2026. An address without the PD status parameter reads the programme
  documents whose eTools status is ``active`` only (``partnerships.statuses.ACTIVE``), and the filter shows
  Active chosen. Any status in the address is read as before; the parameter with nothing chosen (``pd_status=``
  on Partner monitoring and Funds, ``status=`` on the plan) is every status, and the filter bar sends it when
  its choice is cleared (a hidden empty field), so clearing never falls back to Active.
* **What reads the same address** follows the same parsing code: the parts, the map, the exports, the figure
  trail, the Power BI comparison sheet, Ask NeuroDB's look-ups that say they read "as the page opens".
* **What is stored** is keyed by the statuses read: what the old default (Active, Signed and Suspended) stored
  is never served for the new one.
* **Links that state a figure** agree with the page they open: the daily review's, NeuroDB Watch's and the
  partner page's write their own statuses out (their scope is not a page filter); the HACT visit plan's strip
  on Field monitoring follows the plan's default.

The world, on the suite's day (15 October 2026), Child Protection: one programme document per status.

- LEB/PD31, active (Example Partner One): two indicators, one Accepted report; an FR of 1,000;
- LEB/PD32, signed (Example Partner Two): two indicators, one Submitted report; an FR of 2,000;
- LEB/PD33, suspended (Example Partner Three): two indicators, never reported;
- LEB/PD34, ended (Example Partner Three): two indicators; an FR of 4,000;
- LEB/PD35, draft (Example Partner Four): two indicators; an FR of 8,000: never read but where every status
  read it before (the funds reservations tab).
"""

# ruff: noqa: F811 (the fixtures are defined here and used by name)
from __future__ import annotations

import csv
import datetime
import io
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.http import QueryDict
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import comparison, funds, hact_plan, monitoring, reporting
from neurodb.datamart import models as dm
from neurodb.datamart import trail_monitoring as tm
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PartnerOrganization
from neurodb.review import checks as review_checks
from tests.reports import test_hact_plan_page as plan_world
from tests.reports.test_hact_plan_page import people  # noqa: F401 (a fixture)
from tests.reports.test_home_reporting import _indicator, _pd, _report
from tests.web.parts import raw_get, text

pytestmark = pytest.mark.django_db

DAY = datetime.date(2026, 10, 15)
CP = "Child Protection"
RUNNING = ["active", "signed", "suspended"]
PM = reverse("reports:pd_monitoring")
FUNDS = reverse("reports:funds")
FIELD = reverse("reports:monitoring")
SELECT = r'<select[^>]*\bid="{id}"[^>]*>(.*?)</select>'
OPTION = re.compile(r'<option value="([^"]*)"\s*(selected)?')
EVERY_PD = {"LEB/PD31", "LEB/PD32", "LEB/PD33", "LEB/PD34"}  # every status: never the draft PD35


@pytest.fixture
def world(db):
    d = datetime.date
    one = PartnerOrganization.objects.create(etl_id="31", name="Example Partner One", partner_type="CSO")
    two = PartnerOrganization.objects.create(etl_id="32", name="Example Partner Two", partner_type="CSO")
    three = PartnerOrganization.objects.create(etl_id="33", name="Example Partner Three", partner_type="CSO")
    four = PartnerOrganization.objects.create(etl_id="34", name="Example Partner Four", partner_type="CSO")
    pds = {
        "active": _pd(one, "LEB/PD31", sections=[CP]),
        "signed": _pd(two, "LEB/PD32", status="signed", sections=[CP]),
        "suspended": _pd(three, "LEB/PD33", status="suspended", sections=[CP]),
        "ended": _pd(three, "LEB/PD34", status="ended", sections=[CP]),
        "draft": _pd(four, "LEB/PD35", status="draft", sections=[CP]),
    }
    for pd in pds.values():
        _indicator(pd, "# of children reached", CP)
        _indicator(pd, "# of caregivers reached", CP)
    _report(pds["active"], one, "QPR1", d(2026, 3, 31), "Accepted", section=CP, submitted=d(2026, 4, 10))
    _report(pds["signed"], two, "QPR1", d(2026, 6, 30), "Submitted", section=CP, submitted=d(2026, 7, 10))
    for n, (key, amount) in enumerate(
        (("active", 1000), ("signed", 2000), ("ended", 4000), ("draft", 8000)), 1
    ):
        pd = pds[key]
        fr = f"04000{30 + n}"
        dm.FundsReservation.objects.create(
            datamart_id=n,
            intervention=pd,
            fr_number=fr,
            line_item=1,
            donor="Example Donor Fund",
            grant_number=f"SC0000{30 + n}",
            overall_amount=Decimal(amount),
            start_date=d(2026, 2, 1),
        )
        dm.FundsReservationHeader.objects.create(
            datamart_id=n,
            intervention=pd,
            fr_number=fr,
            total_amt=Decimal(amount),
            actual_amt=Decimal(amount) / 2,
            start_date=d(2026, 2, 1),
        )
    return pds


def _ticked(html: str, select_id: str = "f-pd-status") -> list[str]:
    """The statuses a filter bar's select shows as chosen."""
    found = re.search(SELECT.format(id=select_id), html, re.S)
    assert found, f"the filter bar has the select {select_id}"
    return [value for value, selected in OPTION.findall(found.group(1)) if selected]


def _keeps_empty(html: str, name: str) -> bool:
    """The filter bar sends ``name=`` when its choice is cleared: a hidden empty field and the select both
    marked data-keep-empty (app.js, lib.filterParams)."""
    hidden = re.search(rf'<input type="hidden" name="{name}" value="" data-keep-empty>', html)
    select = re.search(rf'<select[^>]*name="{name}"[^>]*data-keep-empty', html)
    return bool(hidden and select)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _csv(response) -> list[dict[str, str]]:
    body = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(body)))


def _pm_pds(response) -> set[str]:
    return {r.pd.number for r in response.context["rows"]}


# ------------------------------------------------------------------------------------------ one definition
def test_every_page_reads_one_default_active_alone():
    assert pd_statuses.ACTIVE == ("active",)
    assert monitoring.Filters.from_params({}).status_choice == list(pd_statuses.ACTIVE)
    assert monitoring.Filters.from_params({}).statuses_read == list(pd_statuses.ACTIVE)
    assert reporting.ReportFilters.from_params({}).status_choice == list(pd_statuses.ACTIVE)
    assert funds.FundFilters.from_params({}).status_choice == list(pd_statuses.ACTIVE)
    assert hact_plan.PlanFilters.from_params({}, DAY).status_choice == pd_statuses.ACTIVE
    # the parameter with nothing chosen is every status, on every page
    assert monitoring.Filters.from_params({"pd_status": ""}).statuses_read == []
    assert reporting.ReportFilters.from_params({"pd_status": ""}).status_choice == []
    assert funds.FundFilters.from_params(QueryDict("pd_status=")).status_choice == []
    assert hact_plan.PlanFilters.from_params(QueryDict("status="), DAY).status_choice == ()


# ---------------------------------------------------------------------------------------- Partner monitoring
def test_partner_monitoring_opens_on_the_active_programme_documents(client_viewer, world):
    page = client_viewer.get(PM, {"section": ""})
    assert page.status_code == 200
    assert _pm_pds(page) == {"LEB/PD31"}
    html = text(page)
    assert _ticked(html) == ["active"]
    found = re.search(SELECT.format(id="f-pd-status"), html, re.S).group(1)
    for status in ("signed", "suspended", "ended"):
        assert f'<option value="{status}"' in found, status
    assert _keeps_empty(html, "pd_status")
    assert "By default the active ones" in html and "Clear the choice for every status" in html
    assert "By default the running ones" not in html


@pytest.mark.parametrize(
    ("query", "expected", "ticked"),
    [
        ("pd_status=signed", {"LEB/PD32"}, ["signed"]),
        (
            "pd_status=active&pd_status=signed&pd_status=suspended",
            {"LEB/PD31", "LEB/PD32", "LEB/PD33"},
            RUNNING,
        ),
        ("pd_status=", EVERY_PD, []),  # every status, never the draft
        ("pd_status=&pd_status=ended", {"LEB/PD34"}, ["ended"]),  # as the filter bar sends one status
        ("scope=all", EVERY_PD, []),  # the closed ones too, as before
    ],
)
def test_partner_monitoring_reads_the_statuses_of_its_address(client_viewer, world, query, expected, ticked):
    page = client_viewer.get(f"{PM}?section=&{query}")
    assert _pm_pds(page) == expected
    assert _ticked(text(page)) == ticked


def test_program_performance_the_map_and_the_exports_read_the_default(client_viewer, world):
    table = client_viewer.get(PM, {"view": "table", "section": ""})
    assert _pm_pds(table) == {"LEB/PD31"}
    points = client_viewer.get(reverse("api:pd_map"), {"year": "2026"}).json()
    assert {p["number"] for p in points["pds"].values()} == {"LEB/PD31"}
    every = client_viewer.get(reverse("api:pd_map"), {"year": "2026", "pd_status": ""}).json()
    assert {p["number"] for p in every["pds"].values()} == EVERY_PD
    exported = _csv(raw_get(client_viewer, PM, {"view": "table", "export": "csv"}))
    assert {r["programme_document"] for r in exported} == {"LEB/PD31"}
    exported = _csv(raw_get(client_viewer, PM, {"view": "table", "export": "csv", "pd_status": ""}))
    assert {r["programme_document"] for r in exported} == EVERY_PD


def test_partner_monitoring_s_trail_and_comparison_sheet_read_the_default(world):
    found = tm.monitoring_trail({"year": "2026", "section": ""}, DAY)
    assert found.figure("pm.indicators").page == 2  # PD31's two indicators
    assert not found.differences
    assert "PD status: Active (the page's default)" in found.filter_words
    assert not any("running" in words for words in found.filter_words)
    every = tm.monitoring_trail({"year": "2026", "section": "", "pd_status": ""}, DAY)
    assert every.figure("pm.indicators").page == 8
    sheet = comparison.read("partner-monitoring", {"view": "table"}, DAY)
    assert {r.pd.number for r in sheet.data["program_performance"]} == {"LEB/PD31"}
    slicers, _clear = comparison.settings_rows(sheet)
    assert dict(slicers)["PD status"] == "Active"


def test_figures_stored_for_the_old_default_are_never_served_for_the_new_one(world, settings):
    """The old default read the running programme documents: what it stored is under the running statuses'
    key, which the bare address no longer reads. The bare address and Active chosen share one key."""
    settings.DEBUG = False
    bare = monitoring.Filters.from_params({})
    active = monitoring.Filters.from_params({"pd_status": ["active"]})
    running = monitoring.Filters.from_params({"pd_status": RUNNING})
    assert bare.cache_key() == active.cache_key() != running.cache_key()
    old = monitoring.stored_indicators(running, DAY)  # what the old default stored
    assert {r.pd.number for r in old} == {"LEB/PD31", "LEB/PD32", "LEB/PD33"}
    assert {r.pd.number for r in monitoring.stored_indicators(bare, DAY)} == {"LEB/PD31"}
    old_places = monitoring.stored_map_points(running, DAY)
    assert {p["number"] for p in old_places["pds"].values()} == {"LEB/PD31", "LEB/PD32", "LEB/PD33"}
    places = monitoring.stored_map_points(bare, DAY)
    assert {p["number"] for p in places["pds"].values()} == {"LEB/PD31"}


def test_ask_neurodb_reads_partner_monitoring_as_the_page_opens(client_viewer, world):
    out = tools.run("pd_indicator_progress", {})
    assert out["indicators"] == 2 and out["programme_documents"] == 1
    page = client_viewer.get(out["url"])
    assert len(page.context["rows"]) == out["indicators"]
    closed = tools.run("pd_indicator_progress", {"include_closed": True})
    assert closed["indicators"] == 8
    assert len(client_viewer.get(closed["url"]).context["rows"]) == 8
    _function, description, _schema, _label = tools.TOOLS["pd_indicator_progress"]
    assert "of the active programme documents" in description and "as the page opens" in description


# ---------------------------------------------------------------------------------- links into Partner monitoring
def test_the_partner_page_writes_its_running_statuses_into_its_links(client_viewer, world):
    partner = world["signed"].partner
    page = client_viewer.get(reverse("reports:partner_profile", args=[partner.pk]))
    html = text(page)
    stated = re.search(r'href="([^"]*)">All 2 indicators of the running', html)
    assert stated, "the partner page states its indicators"
    url = stated.group(1).replace("&amp;", "&")
    assert _query(url)["pd_status"] == RUNNING
    assert len(client_viewer.get(url).context["rows"]) == 2  # the signed PD32's two indicators
    for path in (PM, reverse("reports:pd_monitoring_map")):
        for link in re.findall(rf'href="({re.escape(path)}\?[^"]*)"', html):
            assert _query(link.replace("&amp;", "&"))["pd_status"] == RUNNING, link


def test_the_daily_review_keeps_its_running_programme_documents_and_says_so_in_its_links(
    client_viewer, world
):
    ctx = review_checks.Context(DAY)
    assert {i.pd.number for i in ctx.rows} == {"LEB/PD31", "LEB/PD32", "LEB/PD33"}  # its own scope, as before
    drafts = review_checks.check_not_reported(ctx)
    by_pd = {d.key.split(":")[1]: d for d in drafts}
    assert "LEB/PD33" in by_pd  # the suspended PD's indicators, never reported
    for number, draft in by_pd.items():
        assert _query(draft.url)["pd_status"] == RUNNING, number
        stated = int(draft.title.split()[0])
        assert len(client_viewer.get(draft.url).context["rows"]) == stated, number


def test_the_etools_reporting_checks_keep_the_running_programme_documents(world, monkeypatch):
    from neurodb.datamart import checks

    read = []
    indicators = monitoring.indicators
    monkeypatch.setattr(monitoring, "indicators", lambda f, *a, **k: read.append(f) or indicators(f, *a, **k))
    checks._reached_against_prp()
    assert [f.statuses_read for f in read] == [sorted(RUNNING)]


# --------------------------------------------------------------------------------------------------- Funds
def _contributions(response) -> set[str]:
    return {r["programme_document"] for r in response.context["contributions"]["rows"]}


def test_funds_opens_on_the_active_programme_documents(client_viewer, world):
    page = client_viewer.get(FUNDS, {"tab": "contributions"})
    assert _contributions(page) == {"LEB/PD31"}
    html = text(page)
    assert _ticked(html) == ["active"]
    assert _keeps_empty(html, "pd_status")
    assert "By default the active ones" in html and "Clear the choice for every status" in html
    reservations = client_viewer.get(FUNDS)
    data = reservations.context["data"]
    assert data["fr_count"] == 1 and data["totals"]["reserved"] == Decimal(1000)
    assert _ticked(text(reservations)) == ["active"]
    programmes = client_viewer.get(FUNDS, {"tab": "programmes"})
    assert {r["programme_document"] for r in programmes.context["programmes"]["rows"]} == {"LEB/PD31"}


def test_funds_reads_the_statuses_of_its_address(client_viewer, world):
    signed = client_viewer.get(f"{FUNDS}?tab=contributions&pd_status=signed")
    assert _contributions(signed) == {"LEB/PD32"} and _ticked(text(signed)) == ["signed"]
    every = client_viewer.get(f"{FUNDS}?tab=contributions&pd_status=")
    assert _contributions(every) == EVERY_PD and _ticked(text(every)) == []  # never the draft
    # the reservations tab: every funds reservation, the draft PD's too, as every status read it before
    reservations = client_viewer.get(f"{FUNDS}?pd_status=")
    assert reservations.context["data"]["fr_count"] == 4
    assert reservations.context["data"]["totals"]["reserved"] == Decimal(15000)


def test_the_funds_exports_and_tabs_keep_the_choice(client_viewer, world):
    rows = _csv(raw_get(client_viewer, FUNDS, {"tab": "contributions", "export": "csv"}))
    assert {r["programme_document"] for r in rows} == {"LEB/PD31"}
    rows = _csv(raw_get(client_viewer, FUNDS, {"tab": "contributions", "export": "csv", "pd_status": ""}))
    assert {r["programme_document"] for r in rows} == EVERY_PD
    page = client_viewer.get(f"{FUNDS}?tab=contributions&pd_status=")
    for url in (*page.context["tab_urls"].values(), *(d["url"] for d in page.context["downloads"])):
        assert _query(url)["pd_status"] == [""], url


def test_ask_neurodb_reads_funds_as_the_page_opens(client_viewer, world):
    out = tools.run("funds_overview", {})
    assert out["funds_reservations"] == 1 and out["reserved_usd"] == 1000
    page = client_viewer.get(out["url"])
    assert page.context["data"]["fr_count"] == out["funds_reservations"]
    donor = tools.run("funds_overview", {"donor": "Example Donor"})
    assert client_viewer.get(donor["url"]).context["data"]["fr_count"] == donor["funds_reservations"] == 1
    _function, description, _schema, _label = tools.TOOLS["funds_overview"]
    assert "of the active programme documents" in description and "as the page opens" in description


def test_a_programme_document_s_link_into_funds_shows_it_whatever_its_status(client_viewer, world):
    pd = world["signed"]
    pd.cso_contribution = "500"
    pd.save(update_fields=["cso_contribution"])
    html = text(client_viewer.get(reverse("reports:programme_detail", args=[pd.pk])))
    found = re.search(rf'href="({re.escape(FUNDS)}\?tab=contributions[^"]*)"', html)
    assert found
    url = found.group(1).replace("&amp;", "&")
    assert _query(url)["pd_status"] == [""]
    assert _contributions(client_viewer.get(url)) == {"LEB/PD32"}


# ---------------------------------------------------------------------------------------- the HACT visit plan
@pytest.fixture
def plan(db):
    """Partner A: an active PD with 2 visits planned, none done; a signed PD with 1 planned, none done; an
    ended PD with 1 planned, none done. Partner B: an active PD with 1 planned and done."""
    a, b = plan_world.partner("Example Plan Partner A"), plan_world.partner("Example Plan Partner B")
    pds = {
        "active": plan_world.programme(a),
        "signed": plan_world.programme(a, status="signed"),
        "ended": plan_world.programme(a, status="ended", end=datetime.date(2026, 3, 31)),
        "done": plan_world.programme(b),
    }
    plan_world.plan(pds["active"], (1, 1, 0, 0))
    plan_world.plan(pds["signed"], (1, 0, 0, 0))
    plan_world.plan(pds["ended"], (1, 0, 0, 0))
    plan_world.plan(pds["done"], (1, 0, 0, 0))
    plan_world.fm_visit(pds["done"])
    return {"partners": {"a": a, "b": b}, **pds}


def _plan_ids(response) -> set[int]:
    return {row["pd_id"] for row in response.context["rows"]}


def test_the_visit_plan_opens_on_the_active_programme_documents(client_viewer, plan):
    page = client_viewer.get(FIELD, {"tab": "plan"})
    assert _plan_ids(page) == {plan["active"].pk, plan["done"].pk}
    html = text(page)
    assert _ticked(html, "f-status") == ["active"]
    assert _keeps_empty(html, "status")
    assert "By default the active ones" in html and "Clear the choice for every status" in html
    every = client_viewer.get(f"{FIELD}?tab=plan&status=")
    assert _plan_ids(every) == {pd.pk for key, pd in plan.items() if key != "partners"}
    assert _ticked(text(every), "f-status") == []
    ended = client_viewer.get(f"{FIELD}?tab=plan&status=&status=ended")
    assert _plan_ids(ended) == {plan["ended"].pk} and _ticked(text(ended), "f-status") == ["ended"]


def test_the_visit_plan_s_files_read_the_default(client_viewer, plan):
    rows = _csv(raw_get(client_viewer, FIELD, {"tab": "plan", "export": "csv"}))
    assert {r["reference_number"] for r in rows} == {plan["active"].number, plan["done"].number}
    rows = _csv(raw_get(client_viewer, FIELD, {"tab": "plan", "export": "csv", "status": ""}))
    assert len(rows) == 4


def test_the_strip_states_the_plan_as_it_opens(client_viewer, plan):
    findings = client_viewer.get(FIELD)
    strip = findings.context["hact_strip"]
    page = client_viewer.get(strip["url"])
    figures = page.context["figures"]
    assert strip["not_visited"] == figures["not_visited"]
    assert strip["still_needed"] == figures["still_needed"]
    assert strip["still_needed"]["visits"] == 2  # the active PD's two, never the signed or ended PD's


def test_watch_and_the_daily_review_open_the_plan_on_every_programme_document_they_count(client_viewer, plan):
    """Their HACT items count every programme document of the year (``hact_plan.partner_counts``): their link
    writes every status out (``status=``), so the plan shows what they count. The items' own tests check
    that their links carry it (tests/watch/test_money_calendar.py, tests/review/test_new_checks.py)."""
    a = plan["partners"]["a"]
    counted = hact_plan.partner_counts(DAY.year, DAY)[a.pk]
    assert counted == {"pds": 3, "visits": 4}  # every PD of the year: the HACT assurance's own scope
    url = review_checks.link(
        "reports:monitoring", tab="plan", partner=a.pk, year=DAY.year, status=pd_statuses.EVERY
    )
    assert _query(url)["status"] == [""]
    page = client_viewer.get(url)
    assert len(page.context["rows"]) == counted["pds"]
    assert page.context["figures"]["still_needed"]["visits"] == counted["visits"]
    bare = client_viewer.get(
        review_checks.link("reports:monitoring", tab="plan", partner=a.pk, year=DAY.year)
    )
    assert len(bare.context["rows"]) == 1  # the plan as it opens: the active PD alone


def test_ask_neurodb_reads_the_visit_plan_as_the_page_opens(client_viewer, plan):
    out = tools.run("hact_visit_plan", {})
    page = client_viewer.get(out["url"])
    assert out["pds"] == page.context["figures"]["pds"] == 2
    assert out["still_needed"] == page.context["figures"]["still_needed"]
    _function, description, _schema, _label = tools.TOOLS["hact_visit_plan"]
    assert "of the active programme documents" in description and "as the page opens" in description


# ------------------------------------------------------------------------------- Programmes and Donors (review)
PROGRAMMES = reverse("reports:programmes")
DONORS = reverse("reports:donors")


def _programmes(response, tab: str = "documents") -> set[str]:
    """The programme documents a tab of Programmes lists."""
    if tab == "indicators":
        return {r["programme_document"] for r in response.context["page_obj"]}
    if tab == "overall":
        return {r["number"] for r in response.context["overall"]["rows"]}
    return {r["number"] for r in response.context["rows"]}


@pytest.mark.parametrize("tab", ["documents", "indicators", "overall"])
def test_programmes_status_filter_opens_on_active(client_viewer, world, tab):
    """Its Status filter is a PD status filter: the bare address lists the active programme documents and
    shows Active chosen; ``status=`` alone is every status (never a draft); a status chosen is kept."""
    query = {} if tab == "documents" else {"tab": tab}
    page = client_viewer.get(PROGRAMMES, query)
    assert _programmes(page, tab) == {"LEB/PD31"}
    html = text(page)
    assert _ticked(html, "f-status") == ["active"]
    assert _keeps_empty(html, "status")
    assert "By default the active ones" in html and "Clear the choice for every status" in html
    every = client_viewer.get(PROGRAMMES, {**query, "status": ""})
    assert _programmes(every, tab) == EVERY_PD
    assert _ticked(text(every), "f-status") == []
    signed = client_viewer.get(PROGRAMMES, {**query, "status": "signed"})
    assert _programmes(signed, tab) == {"LEB/PD32"}
    assert _ticked(text(signed), "f-status") == ["signed"]


def test_programmes_tabs_and_files_keep_every_status(client_viewer, world):
    page = client_viewer.get(PROGRAMMES, {"status": ""})
    for tab in ("documents", "indicators", "overall"):
        assert _query(page.context["tab_urls"][tab])["status"] == [""], tab
    overall = client_viewer.get(PROGRAMMES, {"tab": "overall", "status": ""})
    for download in overall.context["downloads"]:
        assert _query(download["url"])["status"] == [""], download
    rows = _csv(raw_get(client_viewer, PROGRAMMES, {"tab": "overall", "export": "csv", "status": ""}))
    assert {r["programme_document"] for r in rows} == EVERY_PD
    rows = _csv(raw_get(client_viewer, PROGRAMMES, {"tab": "overall", "export": "csv"}))
    assert {r["programme_document"] for r in rows} == {"LEB/PD31"}
    rows = _csv(raw_get(client_viewer, PROGRAMMES, {"tab": "indicators", "export": "csv"}))
    assert {r["programme_document"] for r in rows} == {"LEB/PD31"}


def test_programmes_overall_and_funds_agree_as_they_open(client_viewer, world):
    """Funded programme overall is Funds' Funded Programs tab: opened bare, both list the active PDs."""
    overall = client_viewer.get(PROGRAMMES, {"tab": "overall"})
    funds_tab = client_viewer.get(FUNDS, {"tab": "programmes"})
    listed = {r["programme_document"] for r in funds_tab.context["programmes"]["rows"]}
    assert _programmes(overall, "overall") == listed == {"LEB/PD31"}


def test_donors_status_filter_opens_on_active_and_its_links_carry_the_choice(client_viewer, world):
    page = client_viewer.get(DONORS)
    assert page.context["data"]["count"] == 1
    html = text(page)
    assert _ticked(html, "f-status") == ["active"]
    assert _keeps_empty(html, "status")
    every = client_viewer.get(DONORS, {"status": ""})
    assert every.context["data"]["count"] == 4  # never the draft
    assert _ticked(text(every), "f-status") == []
    for response, chosen in ((page, ["active"]), (every, [""])):
        url = response.context["programmes_url"]
        assert _query(url)["status"] == chosen
        listed = client_viewer.get(url)  # "All N on Programme documents"
        assert listed.context["page_obj"].paginator.count == response.context["data"]["count"]
        assert _query(response.context["funds_url"])["pd_status"] == chosen


def test_ask_neurodb_s_programme_and_donor_lookups_open_their_numbers(client_viewer, world):
    """search_programmes and donor_funding keep their own scope (every status but drafts, or the status
    asked): their links write it out, so the pages, which open on Active, show the same count."""
    out = tools.run("search_programmes", {})
    assert out["count"] == 4
    assert client_viewer.get(out["url"]).context["page_obj"].paginator.count == out["count"]
    signed = tools.run("search_programmes", {"status": "signed"})
    assert client_viewer.get(signed["url"]).context["page_obj"].paginator.count == signed["count"] == 1
    active = tools.run("search_programmes", {"active_only": True})
    assert client_viewer.get(active["url"]).context["page_obj"].paginator.count == active["count"] == 1
    funding = tools.run("donor_funding", {})
    assert client_viewer.get(funding["url"]).context["data"]["count"] == funding["programme_documents"] == 4


def test_the_overview_brief_and_home_open_funds_and_programmes_on_every_status(client, admin_user, world):
    """Their figures are of every programme document running in the year, whatever its status now: their
    links to Funds and Programmes write every status out, as the pages opened before 25 Nov 2026."""
    from neurodb.reports import brief

    client.force_login(admin_user)
    for path in (reverse("reports:country_overview"), reverse("reports:overview")):
        html = text(client.get(path, {"section": ""}))
        main = html[html.index("<main") : html.index("</main>")]
        funds_links = re.findall(rf'href="({re.escape(FUNDS)}[^"]*)"', main)
        programmes_links = re.findall(rf'href="({re.escape(PROGRAMMES)}(?:\?[^"]*)?)"', main)  # the list
        for link in funds_links:
            assert _query(link.replace("&amp;", "&"))["pd_status"] == [""], (path, link)
        for link in programmes_links:
            assert _query(link.replace("&amp;", "&"))["status"] == [""], (path, link)
        if path == reverse("reports:country_overview"):
            assert funds_links and programmes_links
    tiles = {
        t["key"]: t for t in brief.build(brief.Scope(year=2026, today=DAY), cache=False)["headline"]["tiles"]
    }
    assert _query(tiles["disbursed"]["url"])["pd_status"] == [""]


# ------------------------------------------------------------------------- Ask NeuroDB and NeuroDB Watch (review)
def test_ask_neurodb_s_partner_monitoring_link_opens_every_section_for_a_section_editor(
    client_viewer, viewer, section, world
):
    """The look-up counts every section; its link writes ``section=``, so the page never applies the
    person's own section on a first load."""
    viewer.section = section  # Child Protection
    viewer.save()
    other = PartnerOrganization.objects.create(etl_id="41", name="Example Partner Five", partner_type="CSO")
    _indicator(_pd(other, "LEB/PD41", sections=["Education"]), "# of children enrolled", "Education")
    out = tools.run("pd_indicator_progress", {})
    assert out["indicators"] == 3
    assert _query(out["url"])["section"] == [""]
    assert len(client_viewer.get(out["url"]).context["rows"]) == out["indicators"]


def test_ask_neurodb_reads_every_status_when_asked(client_viewer, world):
    out = tools.run("funds_overview", {"include_closed": True})
    assert out["funds_reservations"] == 4 and out["reserved_usd"] == 15000  # as Funds with pd_status=
    assert _query(out["url"])["pd_status"] == [""]
    assert client_viewer.get(out["url"]).context["data"]["fr_count"] == out["funds_reservations"]
    grant = tools.run("funds_overview", {"grant": "SC000033", "include_closed": True})  # the ended PD34's
    assert grant["funds_reservations"] == 1
    assert client_viewer.get(grant["url"]).context["data"]["fr_count"] == 1
    _function, description, schema, _label = tools.TOOLS["funds_overview"]
    assert "include_closed" in schema["properties"] and "include_closed" in description


def test_ask_neurodb_reads_the_visit_plan_of_every_status_when_asked(client_viewer, plan):
    out = tools.run("hact_visit_plan", {"include_closed": True})
    assert _query(out["url"])["status"] == [""]
    page = client_viewer.get(out["url"])
    assert out["pds"] == page.context["figures"]["pds"] == 4
    assert out["still_needed"] == page.context["figures"]["still_needed"]


def test_neurodb_watch_s_look_ups_read_what_watch_counts(world):
    """Watch's look-ups (its AI looks into critical points) read the statuses Watch counts, never a page's
    default: every programme document's funds (its grant, funds reservation and PD closure points) and the
    running programme documents' indicators (the daily review's points it copies), as before 25 Nov 2026."""
    from neurodb.assistant import agent
    from neurodb.watch import investigate

    options = investigate.run_options([], [])
    assert options.tool_context is tools.for_watch
    assert "funds lookup reads programme documents of every status" in investigate.INSTRUCTIONS
    # the grant of the ended PD34 alone: nothing as Funds opens, its funds reservation in Watch's look-up
    assert tools.run("funds_overview", {"grant": "SC000033"})["funds_reservations"] == 0
    found = agent._lookup("funds_overview", {"grant": "SC000033"}, options)
    assert found["funds_reservations"] == 1 and found["reserved_usd"] == 4000
    with tools.for_watch():
        out = tools.run("funds_overview", {"grant": "SC000033"})
        assert _query(out["url"])["pd_status"] == [""]
        signed = tools.run("pd_indicator_progress", {"programme_document": "LEB/PD32"})
        assert signed["indicators"] == 2  # the signed PD32's, as the daily review reads it
        assert _query(signed["url"])["pd_status"] == RUNNING
        ended = tools.run("pd_indicator_progress", {"programme_document": "LEB/PD34"})
        assert ended["indicators"] == 0  # the running ones, as before
    assert tools.run("pd_indicator_progress", {"programme_document": "LEB/PD32"})["indicators"] == 0


def test_partner_details_says_its_indicators_are_of_the_running_programme_documents():
    _function, description, _schema, _label = tools.TOOLS["partner_details"]
    assert "running (active, signed or suspended) programme documents" in description


# ------------------------------------------------------------------------- the plan's comment page (review)
def test_the_plan_comment_page_goes_back_to_a_plan_that_lists_its_programme_document(people, plan):
    from tests.reports.test_hact_plan_page import _client

    client = _client(people["admin"])
    for key in ("signed", "ended", "active"):
        pd = plan[key]
        url = reverse("reports:visit_plan_comment", args=[pd.pk])
        back = client.get(url, {"year": DAY.year}).context["back"]
        assert pd.pk in _plan_ids(client.get(back.split("#")[0])), key
        saved = client.post(url, {"text": "Example note", "year": DAY.year})
        assert saved.status_code == 302 and saved["Location"] == back
    assert "status" not in _query(back)  # an active PD: the plan as it opens


# ------------------------------------------------------------------------------- the figure trail (review)
def test_the_trail_of_the_bare_address_names_the_default_even_after_active_was_chosen(world, settings):
    settings.DEBUG = False
    chosen = tm.monitoring_trail({"year": "2026", "section": "", "pd_status": "active"}, DAY)
    bare = tm.monitoring_trail({"year": "2026", "section": ""}, DAY)

    def title(found):
        return next(step.title for step in found.steps if step.key == "pd_status")

    assert title(chosen) == "Status not chosen (Active chosen)"
    assert title(bare) == "Status not Active (the page's default)"
    assert "PD status: Active (the page's default)" in bare.filter_words
    assert bare.figure("pm.indicators").page == chosen.figure("pm.indicators").page == 2
