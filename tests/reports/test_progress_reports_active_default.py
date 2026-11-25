"""Progress reports opens on the active programme documents (the owner's decision of 21 Nov 2026).

* With no ``pd_status`` in its address, the page reads the PD status Active only (``partnerships.statuses.ACTIVE``)
  instead of the running ones (Active, Signed, Suspended): its tiles, every tab, the exports and the filter bar,
  where Active is the one status ticked. A ``pd_status`` in the address is honoured as before, and
  ``pd_status=`` alone is still every status.
* Home keeps Active, Signed and Suspended. Home's figures stay as they were, so every link from Home into
  Progress reports carries the statuses Home counted, and opens the page on the same numbers as the tile or
  the row clicked. Partner monitoring, Funds and the HACT visit plan open on Active too since 25 Nov 2026
  (tests/reports/test_pd_status_active_default.py).
* The page's figure trail, its Power BI comparison sheet and Ask NeuroDB's partner reporting lookup (which says
  it reads "as the page opens") follow the page. The eTools reporting checks keep the running programme
  documents. Compare with Power BI writes Power BI's own statuses out, so nothing there changes
  (tests/datamart/test_powerbi_compare.py).

The world, on the suite's day (15 October 2026): one partner report per programme document status.

- LEB/PD21, active: Accepted QPR1 and an Overdue QPR2;
- LEB/PD22, signed: Submitted QPR1;
- LEB/PD23, suspended: Overdue QPR1;
- LEB/PD24, ended: Accepted QPR1.
"""

# ruff: noqa: F811 (the fixture "world" is defined here and used by name)
from __future__ import annotations

import csv
import datetime
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import comparison, monitoring, reporting, trail
from neurodb.datamart import services as datamart
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PartnerOrganization
from neurodb.reports import home as home_service
from tests.reports.test_home_reporting import _indicator, _pd, _report
from tests.web.parts import raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/partner-reporting/"  # reports:partner_reporting
CP = "Child Protection"
RUNNING = ["active", "signed", "suspended"]
DAY = datetime.date(2026, 10, 15)
PD_STATUS_SELECT = re.compile(r'<select[^>]*\bid="f-pd-status"[^>]*>(.*?)</select>', re.S)
OPTION = re.compile(r'<option value="([^"]*)"\s*(selected)?')


@pytest.fixture
def world(db):
    d = datetime.date
    alpha = PartnerOrganization.objects.create(etl_id="21", name="Partner Alpha", partner_type="CSO")
    beta = PartnerOrganization.objects.create(etl_id="22", name="Partner Beta", partner_type="CSO")
    gamma = PartnerOrganization.objects.create(etl_id="23", name="Partner Gamma", partner_type="CSO")
    active = _pd(alpha, "LEB/PD21", sections=[CP])
    signed = _pd(beta, "LEB/PD22", status="signed", sections=[CP])
    suspended = _pd(gamma, "LEB/PD23", status="suspended", sections=[CP])
    ended = _pd(gamma, "LEB/PD24", status="ended", sections=[CP])
    for pd in (active, signed, suspended, ended):
        _indicator(pd, "# of children reached", CP)
        _indicator(pd, "# of caregivers reached", CP)
    _report(active, alpha, "QPR1", d(2026, 3, 31), "Accepted", section=CP, submitted=d(2026, 4, 10))
    _report(active, alpha, "QPR2", d(2026, 6, 30), section=CP)  # due 15 July: overdue
    _report(signed, beta, "QPR1", d(2026, 6, 30), "Submitted", section=CP, submitted=d(2026, 7, 10))
    _report(suspended, gamma, "QPR1", d(2026, 6, 30), section=CP)  # overdue
    _report(ended, gamma, "QPR1", d(2026, 3, 31), "Accepted", section=CP, submitted=d(2026, 4, 2))
    return {"active": active, "signed": signed, "suspended": suspended, "ended": ended}


def _pds(response) -> set[str]:
    """The programme documents of the page's progress reports."""
    return {r["pd_reference_number"] for r in response.context["data"]["reports"]}


def _ticked(html: str) -> list[str]:
    """The PD statuses the filter bar shows as chosen."""
    found = PD_STATUS_SELECT.search(html)
    assert found, "the filter bar has a PD status choice"
    return [value for value, selected in OPTION.findall(found.group(1)) if selected]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


# ------------------------------------------------------------------------------------------- the default
def test_the_page_opens_on_the_active_programme_documents_only(client_viewer, world):
    page = client_viewer.get(PAGE)
    assert page.status_code == 200
    assert _pds(page) == {"LEB/PD21"}
    counts = page.context["data"]["counts"]
    assert counts["progress_reports"] == 2 and counts["indicator_reports"] == 4
    filters = page.context["f"]
    assert filters.pd_statuses is None and filters.status_choice == ["active"]
    html = text(page)
    assert _ticked(html) == ["active"]  # Active ticked, the other statuses offered
    found = PD_STATUS_SELECT.search(html).group(1)
    for status in ("signed", "suspended", "ended"):
        assert f'<option value="{status}"' in found, status
    assert "By default the active ones" in html and "Clear the choice for every status" in html


@pytest.mark.parametrize(
    ("query", "expected", "ticked"),
    [
        ("pd_status=signed", {"LEB/PD22"}, ["signed"]),
        (
            "pd_status=active&pd_status=signed&pd_status=suspended",
            {"LEB/PD21", "LEB/PD22", "LEB/PD23"},
            RUNNING,
        ),
        ("pd_status=", {"LEB/PD21", "LEB/PD22", "LEB/PD23", "LEB/PD24"}, []),  # every status
        ("pd_status=&pd_status=ended", {"LEB/PD24"}, ["ended"]),  # as the filter bar sends one status
    ],
)
def test_a_pd_status_in_the_address_is_honoured(client_viewer, world, query, expected, ticked):
    page = client_viewer.get(f"{PAGE}?{query}")
    assert _pds(page) == expected
    assert _ticked(text(page)) == ticked


def test_every_tab_and_download_reads_the_default(client_viewer, world):
    partners = client_viewer.get(PAGE, {"tab": "partners"})
    assert partners.context["data"]["partners"]["partners"] == 1  # Partner Alpha's active PD only
    assert {r["pd_reference_number"] for r in partners.context["data"]["partners"]["follow_up"]} == {
        "LEB/PD21"
    }
    locations = client_viewer.get(PAGE, {"tab": "locations"})
    assert {r["programme_document"] for r in locations.context["locations"]["rows"]} == {"LEB/PD21"}
    disaggregations = client_viewer.get(PAGE, {"tab": "disaggregations"})
    assert {b["programme_document"] for b in disaggregations.context["disaggregations"]["blocks"]} <= {
        "LEB/PD21"
    }
    export = raw_get(client_viewer, PAGE, {"export": "csv"})
    rows = list(csv.DictReader(io.StringIO(b"".join(export.streaming_content).decode("utf-8-sig"))))
    assert {r["programme_document"] for r in rows} == {"LEB/PD21"}
    every = raw_get(client_viewer, PAGE, {"export": "csv", "pd_status": ""})
    rows = list(csv.DictReader(io.StringIO(b"".join(every.streaming_content).decode("utf-8-sig"))))
    assert {r["programme_document"] for r in rows} == {"LEB/PD21", "LEB/PD22", "LEB/PD23", "LEB/PD24"}


def test_the_default_and_active_chosen_share_what_is_stored_every_status_keeps_its_own():
    bare = reporting.ReportFilters.from_params({})
    active = reporting.ReportFilters.from_params({"pd_status": ["active"]})
    every = reporting.ReportFilters.from_params({"pd_status": [""]})
    running = reporting.ReportFilters.from_params({"pd_status": RUNNING})
    assert bare.cache_key() == active.cache_key()
    assert len({bare.cache_key(), every.cache_key(), running.cache_key()}) == 3
    # the tiles' hints count the indicators of active programme documents, as before
    assert bare.indicator_statuses == running.indicator_statuses == ["active"]


# ------------------------------------------------------- Partner monitoring opens on Active too; Home keeps its own
def test_partner_monitoring_opens_on_active_too(client_viewer, world):
    """Partner monitoring kept Active, Signed and Suspended until 25 Nov 2026: it opens on Active since."""
    assert monitoring.Filters.from_params({}).status_choice == ["active"]
    page = client_viewer.get(reverse("reports:pd_monitoring"))
    assert page.status_code == 200
    assert _ticked(text(page)) == ["active"]
    pds = {r.pd.number for r in page.context["rows"]}
    assert pds == {"LEB/PD21"}
    running = client_viewer.get(reverse("reports:pd_monitoring"), {"pd_status": RUNNING})
    assert {r.pd.number for r in running.context["rows"]} == {"LEB/PD21", "LEB/PD22", "LEB/PD23"}


def _home(client):
    response = client.get("/home/?section=")
    assert response.status_code == 200
    return response


def test_home_keeps_counting_active_signed_and_suspended(client_viewer, world):
    counts = _home(client_viewer).context["reporting"]["counts"]
    assert counts["progress_reports"] == 4  # PD21's two, PD22's and PD23's; never the ended PD24's
    progress, _keep = home_service.figure_queries([], DAY.year)
    assert progress.getlist("pd_status") == RUNNING


def test_every_home_link_into_progress_reports_carries_home_s_statuses(client_viewer, world):
    home = _home(client_viewer)
    context = home.context
    reporting_path = reverse("reports:partner_reporting")
    links = [
        context["progress_url"],
        context["partners_url"],
        *context["tile_urls"].values(),
        *(row["reports_url"] for row in context["pd_rows"] if row["reports_url"]),
    ]
    assert len(links) > 3
    for url in links:
        assert urlsplit(url).path == reporting_path, url
        assert _query(url)["pd_status"] == RUNNING, url
    # every address of Progress reports Home draws carries them (none takes the page's own default); the menu's
    # row, above Home's own blocks, is the bare address
    html = text(home)
    for url in re.findall(r'href="([^"]*)"', html[html.index('id="home-prp-title"') :]):
        url = url.replace("&amp;", "&")
        if urlsplit(url).path == reporting_path:
            assert _query(url).get("pd_status") == RUNNING, url
    # and Home's figure trail reads them too
    assert _query(context["check_url"])["pd_status"] == RUNNING


def test_home_s_links_open_progress_reports_on_the_same_numbers(client_viewer, world):
    home = _home(client_viewer)
    figures = home.context["reporting"]
    counts = figures["counts"]
    page = client_viewer.get(home.context["tile_urls"]["total"])
    assert page.context["data"]["counts"] == counts
    assert client_viewer.get(home.context["progress_url"]).context["data"]["counts"] == counts
    for s in counts["statuses"]:
        status_page = client_viewer.get(home.context["tile_urls"][s["key"]])
        found = status_page.context["data"]["counts"]
        assert found["progress_reports"] == s["progress_reports"], s["key"]
        assert found["indicators"] == s["indicators"], s["key"]
        assert found["indicator_reports"] == s["indicator_reports"], s["key"]
    partners = client_viewer.get(home.context["partners_url"]).context["data"]["partners"]
    assert {k: partners[k] for k in ("partners", "reported", "not_reported")} == {
        k: figures["partners"][k] for k in ("partners", "reported", "not_reported")
    }
    for row in home.context["pd_rows"]:
        opened = client_viewer.get(row["reports_url"])
        assert opened.context["page_obj"].paginator.count == row["follow_up"], row["pd_reference_number"]
    # the signed and the suspended programme documents' rows open on their reports, not on an empty page
    assert {row["pd_reference_number"] for row in home.context["pd_rows"]} >= {"LEB/PD22", "LEB/PD23"}


# --------------------------------------------------------------------- the trail, the sheet, Ask, the checks
def test_the_figure_trail_reads_the_page_s_default(world):
    page = datamart.partner_reporting({}, DAY, options=False)
    found = trail.progress_trail("progress-reports", {}, DAY)
    assert found.figure("pr.progress_reports").page == page["counts"]["progress_reports"] == 2
    assert not found.differences
    assert "PD status: Active (the page's default)" in found.filter_words
    # Home's block: the statuses Home counted, written out by Home's link
    progress, _keep = home_service.figure_queries([], DAY.year)
    home = trail.progress_trail("home", progress, DAY)
    assert home.figure("pr.progress_reports").page == 4
    assert "PD status: Active, Signed, Suspended" in home.filter_words


def test_the_power_bi_comparison_sheet_reads_the_page_s_figures(world):
    found = comparison.read("progress-reports", {}, DAY)
    assert found.data["page"]["counts"]["progress_reports"] == 2
    slicers, _clear = comparison.settings_rows(found)
    assert dict(slicers)["PD status"] == "Active"
    every = comparison.read("progress-reports", {"pd_status": ""}, DAY)
    assert every.data["page"]["counts"]["progress_reports"] == 5


def test_ask_neurodb_reads_progress_reports_as_the_page_opens(world):
    out = tools.run("partner_reporting", {})
    assert out["pd_statuses"] == ["active"]
    assert out["reports"] == 2 and out["url"] == reverse("reports:partner_reporting")
    _function, description, _schema, _label = tools.TOOLS["partner_reporting"]
    assert "of the active programme documents" in description and "as the page opens" in description


def test_the_home_links_and_the_page_differ_where_home_reads_more_statuses(client_viewer, world):
    """The bare address (the menu's) is the page's default; Home's link is Home's figures."""
    bare = client_viewer.get(PAGE).context["data"]["counts"]["progress_reports"]
    home = _home(client_viewer).context["reporting"]["counts"]["progress_reports"]
    assert (bare, home) == (2, 4)
    assert pd_statuses.ACTIVE == ("active",)


def test_the_etools_reporting_checks_keep_the_running_programme_documents(world):
    """Check 11 counts what the partner reports feed holds of the running programme documents: the page's
    default is not its own."""
    from tests.reports.test_reporting_checks_values import figures, one

    found = figures(one("11"))
    # the indicator reports of PD21's QPR2 and of the suspended PD23's QPR1, two each (Active only: 2)
    assert found["Due or overdue in the partner reports"] == 4
