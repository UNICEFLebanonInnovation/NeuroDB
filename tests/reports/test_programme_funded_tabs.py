"""Programme documents: the *Funded programme summary info* and *Funded programme overall* tabs (the old
NeuroDB's Summary Info (Active PDs / All PDs) pages and its Funded Programs page, the owner's request of 21 Nov
2026).

* The tab bar shows four tabs: Programme documents, Indicators, then the two new ones (``tab=summary``,
  ``tab=overall``).
* **Summary info**: an Active / All switch; the programme documents summed by partner and by section (each in
  its budget currency), the active ones ending within 90 days, and the totals. Each programme document counts
  once per partner; under each of its sections.
* **Overall**: the old page's filters (donor, grant, partner, CSO type, section, status, start years) and its
  list, with downloads of the whole filter.
* The old summary address (``reports:programme_summary``) opens the summary tab.
* No person (focal points) on the page or in a download.

The world (made-up names): two partners, six programme documents (one a draft), funds reservation lines and
headers on three of them.
"""

from __future__ import annotations

import csv
import datetime
import html as html_module
import io
import re
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.web.parts import HX, raw_get

pytestmark = pytest.mark.django_db

PAGE = "/programmes/"  # reports:programmes
TABS = ["Programme documents", "Indicators", "Funded programme summary info", "Funded programme overall"]
TAB_BAR = re.compile(
    r'<div class="segmented[^"]*" role="group" aria-label="Programme documents views">(.*?)</div>', re.S
)
TAB_LINK = re.compile(r'<a href="([^"]*)"([^>]*)>(.*?)</a>', re.S)
TAGS = re.compile(r"<[^>]+>")
PEOPLE = ("Rana Example", "rana.example@example.org", "Karim Sample", "+961 1 000000")
FILTER_FORM = re.compile(r'<form method="get" class="filter-bar".*?</form>', re.S)
FIELD_NAME = re.compile(r'\sname="([a-z_]+)"')


def _tabs(html: str) -> list[tuple[str, str, bool]]:
    bars = TAB_BAR.findall(html)
    assert len(bars) == 1, bars
    return [
        (" ".join(TAGS.sub("", label).split()), html_module.unescape(href), "aria-current" in attrs)
        for href, attrs, label in TAB_LINK.findall(bars[0])
    ]


def _day(days: int) -> datetime.date:
    return timezone.localdate() + datetime.timedelta(days=days)


@pytest.fixture
def world(db):
    alpha = PartnerOrganization.objects.create(
        etl_id="901", name="Alpha Aid", partner_type="Civil Society Organization", cso_type="National"
    )
    beta = PartnerOrganization.objects.create(
        etl_id="902", name="Beta Relief", partner_type="Civil Society Organization", cso_type="International"
    )
    people = {"unicef_focal_points": ["Rana Example", "rana.example@example.org"]}

    def pd(n, partner, status, **fields):
        values = {
            "etl_id": f"95{n}",
            "partner": partner,
            "partner_name": partner.name,
            "number": f"LEB/PCA2026{n:03d}",
            "title": f"Programme {n}",
            "status": status,
            "document_type": "PD",
            "budget_currency": "USD",
            **people,
            **fields,
        }
        return PCA.objects.create(**values)

    p1 = pd(
        1,
        alpha,
        "active",
        section_names=["Education"],
        start=datetime.date(2024, 3, 1),
        end=_day(20),
        total_unicef_budget="1,000",
        total_budget="1,500",
        unicef_cash="800",
        donors=["Donor North"],
        grants=["SC100"],
    )
    p2 = pd(
        2,
        alpha,
        "active",
        document_type="SPD",
        section_names=["Education", "Health"],
        start=datetime.date(2025, 1, 10),
        end=_day(60),
        total_unicef_budget="2000",
        total_budget="2500",
        unicef_cash="1500",
        donors=["Donor South"],
        grants=["SC200"],
    )
    p3 = pd(
        3,
        beta,
        "ended",
        section_names=["Health"],
        start=datetime.date(2022, 5, 1),
        end=datetime.date(2023, 12, 31),
        total_unicef_budget="3000",
        total_budget="3200",
        unicef_cash="3000",
        donors=["Donor North"],
        grants=["SC100"],
    )
    p4 = pd(
        4,
        beta,
        "active",
        section_names=["Health"],
        start=datetime.date(2021, 2, 1),
        end=_day(200),
        budget_currency="LBP",
        total_unicef_budget="80,000",
        total_budget="90,000",
        unicef_cash="80,000",
    )
    pd(5, alpha, "draft", section_names=["Education"], total_budget="9,999", start=datetime.date(2025, 1, 1))
    p6 = pd(
        6,
        beta,
        "closed",
        section_names=["Education"],
        start=datetime.date(2020, 6, 1),
        end=datetime.date(2021, 6, 30),
        total_unicef_budget="500",
        total_budget="600",
        unicef_cash="not given",
    )
    for n, (intervention, amount, donor, grant) in enumerate(
        [
            (p1, "700", "Donor North", "SC100"),
            (p1, "300", "Donor North", "SC100"),
            (p2, "1200", "Donor South", "SC200"),
            (p3, "2500", "Donor North", "SC100"),
        ],
        start=1,
    ):
        dm.FundsReservation.objects.create(
            datamart_id=n,
            intervention=intervention,
            fr_number=f"0400{n}",
            line_item=n,
            donor=donor,
            grant_number=grant,
            overall_amount=Decimal(amount),
        )
    for n, (intervention, actual) in enumerate(
        [(p1, "400"), (p2, "1000"), (p2, "100"), (p3, "2500")], start=1
    ):
        dm.FundsReservationHeader.objects.create(
            datamart_id=n, intervention=intervention, fr_number=f"0500{n}", actual_amt=Decimal(actual)
        )
    return {"alpha": alpha, "beta": beta, "pds": {1: p1, 2: p2, 3: p3, 4: p4, 6: p6}}


# --------------------------------------------------------------------------------------------- the tab bar
@pytest.mark.parametrize("tab", ["", "documents", "indicators", "summary", "overall"])
def test_the_tab_bar_shows_four_tabs(client_viewer, world, tab):
    response = client_viewer.get(PAGE, {"tab": tab} if tab else {})
    assert response.status_code == 200
    tabs = _tabs(response.text)
    assert [label for label, _, _ in tabs] == TABS
    current = [label for label, _, on in tabs if on]
    assert current == [
        {
            "": "Programme documents",
            "documents": "Programme documents",
            "indicators": "Indicators",
            "summary": "Funded programme summary info",
            "overall": "Funded programme overall",
        }[tab]
    ]
    summary, overall = (href for _, href, _ in tabs[2:])
    assert "tab=summary" in summary and "tab=overall" in overall
    assert not any(href.endswith("?") for _, href, _ in tabs)


@pytest.mark.parametrize("tab", ["summary", "overall"])
def test_a_change_of_filter_draws_the_four_tabs(client_viewer, world, tab):
    response = raw_get(client_viewer, PAGE, {"tab": tab}, **HX)
    assert response.status_code == 200
    assert [label for label, _, _ in _tabs(response.text)] == TABS


def test_the_page_no_longer_links_the_old_summary(client_viewer, world):
    for tab in ("documents", "indicators", "summary", "overall"):
        html = client_viewer.get(PAGE, {"tab": tab}).text
        assert reverse("reports:programme_summary") not in html, tab


# ------------------------------------------------------------------------------------------- summary info
def _summary(client, **params):
    response = client.get(PAGE, {"tab": "summary", **params})
    assert response.status_code == 200
    return response, response.context["summary"]


def _keyed(rows, name):
    return {(r[name], r["currency"]): r for r in rows}


def test_summary_opens_on_the_active_programme_documents_by_partner(client_viewer, world):
    _response, summary = _summary(client_viewer)
    assert summary["scope"] == "active"
    assert summary["count"] == 3  # P1, P2, P4: the active ones
    assert summary["partners"] == 2
    rows = _keyed(summary["by_partner"], "partner")
    assert set(rows) == {("Alpha Aid", "USD"), ("Beta Relief", "LBP")}
    alpha = rows[("Alpha Aid", "USD")]
    assert alpha["partner_id"] == world["alpha"].pk
    assert (alpha["pds"], alpha["unicef_planned"], alpha["total"]) == (2, 3000, 4000)
    assert (alpha["reserved"], alpha["disbursed"]) == (2200, 1500)  # each PD once, whatever its FR lines
    beta = rows[("Beta Relief", "LBP")]
    assert (beta["pds"], beta["unicef_planned"], beta["total"]) == (1, 80000, 90000)
    assert not beta["reserved"] and not beta["disbursed"]
    # the most programme documents first
    assert summary["by_partner"][0]["partner"] == "Alpha Aid"


def test_summary_by_section_counts_a_pd_under_each_of_its_sections(client_viewer, world):
    _response, summary = _summary(client_viewer)
    rows = _keyed(summary["by_section"], "section")
    assert set(rows) == {("Education", "USD"), ("Health", "USD"), ("Health", "LBP")}
    education, health = rows[("Education", "USD")], rows[("Health", "USD")]
    assert (education["pds"], education["unicef_planned"], education["total"]) == (2, 3000, 4000)
    assert (education["reserved"], education["disbursed"]) == (2200, 1500)
    assert (health["pds"], health["unicef_planned"], health["total"]) == (1, 2000, 2500)
    assert (health["reserved"], health["disbursed"]) == (1200, 1100)
    assert rows[("Health", "LBP")]["total"] == 90000


def test_summary_all_counts_every_programme_document_but_drafts(client_viewer, world):
    _response, summary = _summary(client_viewer, scope="all")
    assert summary["scope"] == "all"
    assert summary["count"] == 5  # every PD but the draft (P5)
    rows = _keyed(summary["by_partner"], "partner")
    beta = rows[("Beta Relief", "USD")]  # P3 ended and P6 closed
    assert (beta["pds"], beta["unicef_planned"], beta["total"]) == (2, 3500, 3800)
    assert (beta["reserved"], beta["disbursed"]) == (2500, 2500)
    assert rows[("Alpha Aid", "USD")]["total"] == 4000  # the draft's 9,999 is never added
    sections = _keyed(summary["by_section"], "section")
    assert sections[("Education", "USD")]["pds"] == 3
    assert sections[("Health", "USD")]["pds"] == 2


def test_summary_totals_per_currency_and_funds(client_viewer, world):
    response, summary = _summary(client_viewer)
    totals = {t["currency"]: t for t in summary["totals"]}
    assert set(totals) == {"USD", "LBP"}
    assert (totals["USD"]["pds"], totals["USD"]["unicef_planned"], totals["USD"]["total"]) == (2, 3000, 4000)
    assert (totals["LBP"]["pds"], totals["LBP"]["total"]) == (1, 90000)
    assert (summary["reserved"], summary["disbursed"]) == (2200, 1500)
    text = response.text
    for label in (
        "Programme documents",
        "Partners",
        "Funds reserved (USD)",
        "Disbursed (USD)",
        "Ending within 90 days",
        "Summary by section",
        "Summary by partner",
        "Totals by currency",
    ):
        assert label in text, label
    assert "2,200" in text and "1,500" in text
    assert text.count("Excel of the summary") == 1  # one download for the three tables


def test_summary_lists_the_active_programme_documents_ending_within_90_days(client_viewer, world):
    for scope in ("active", "all"):
        response, summary = _summary(client_viewer, scope=scope)
        ending = summary["ending_soon"]
        assert [r["number"] for r in ending] == ["LEB/PCA2026001", "LEB/PCA2026002"], scope
        assert [r["days_to_end"] for r in ending] == [20, 60]
        assert summary["ending_30"] == 1
        assert ending[1]["sections"] == ["Education", "Health"]
        assert ending[0]["partner_id"] == world["alpha"].pk
        assert reverse("reports:programme_detail", args=[world["pds"][1].pk]) in response.text


def test_the_scope_switch_keeps_the_tab(client_viewer, world):
    response, _summary_data = _summary(client_viewer)
    form = FILTER_FORM.search(response.text).group(0)
    assert '<input type="hidden" name="tab" value="summary">' in form
    assert set(FIELD_NAME.findall(form)) == {"tab", "scope"}
    assert 'value="active" checked' in form


def test_summary_charts_read_their_data(client_viewer, world):
    response, _summary_data = _summary(client_viewer)
    assert 'data-source="pd-summary-chart-data"' in response.text
    assert 'id="pd-summary-chart-data"' in response.text
    charts = response.context["summary_charts"]
    assert dict(charts["by_type"]) == {"PD": 2, "SPD": 1}
    assert dict(charts["by_section"]) == {"Health": 2, "Education": 2}


def test_summary_excel_has_three_sheets(client_viewer, world):
    response = client_viewer.get(PAGE, {"tab": "summary", "scope": "all", "export": "xlsx"})
    assert response.status_code == 200
    book = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    assert book.sheetnames == ["Ending within 90 days", "Summary by section", "Summary by partner"]
    partner_rows = list(book["Summary by partner"].iter_rows(values_only=True))
    assert partner_rows[0][:3] == ("partner", "currency", "programme_documents")
    assert ("Beta Relief", "USD", 2, 3500, 3800, 2500, 2500) in partner_rows
    _no_people(repr([list(sheet.iter_rows(values_only=True)) for sheet in book.worksheets]))


def test_the_old_summary_address_opens_the_summary_tab(client_viewer, world):
    response = client_viewer.get(reverse("reports:programme_summary"), {"scope": "all"})
    assert response.status_code == 302
    assert response["Location"] == PAGE + "?tab=summary&scope=all"
    response = client_viewer.get(reverse("reports:programme_summary"))
    assert response["Location"] == PAGE + "?tab=summary"
    assert client_viewer.get(reverse("reports:programme_summary"), {"scope": "nonsense"})["Location"] == (
        PAGE + "?tab=summary"
    )


# ---------------------------------------------------------------------------------------------- overall
def _overall(client, **params):
    response = client.get(PAGE, {"tab": "overall", **params})
    assert response.status_code == 200
    return response, response.context["overall"]


def _numbers(overall) -> list[int]:
    return sorted(int(r["number"][-3:]) for r in overall["rows"])


def test_overall_lists_every_programme_document_but_drafts_with_its_amounts(client_viewer, world):
    response, overall = _overall(client_viewer)
    assert overall["count"] == 5
    assert _numbers(overall) == [1, 2, 3, 4, 6]
    rows = {int(r["number"][-3:]): r for r in overall["rows"]}
    p1 = rows[1]
    assert (p1["partner"], p1["document_type"], p1["status"]) == ("Alpha Aid", "PD", "active")
    assert (p1["unicef_planned"], p1["total"], p1["unicef_cash"]) == (1000, 1500, 800)
    assert (p1["reserved"], p1["disbursed"]) == (1000, 400)
    assert p1["days_to_end"] == 20 and p1["donors"] == ["Donor North"] and p1["grants"] == ["SC100"]
    assert rows[3]["days_to_end"] == -1  # ended
    assert rows[6]["unicef_cash"] is None  # "not given" is not a number
    assert rows[4]["currency"] == "LBP"
    assert (overall["reserved"], overall["disbursed"]) == (4700, 4000)
    totals = {t["currency"]: t for t in overall["totals"]}
    assert totals["USD"]["total"] == 1500 + 2500 + 3200 + 600 and totals["LBP"]["total"] == 90000
    for word in ("Funds reserved (USD)", "Disbursed (USD)", "UNICEF cash", "Days to end", "Donors", "Grants"):
        assert word in response.text, word


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"donor": "Donor North"}, [1, 3]),
        ({"grant": "SC200"}, [2]),
        ({"partner": "Beta Relief"}, [3, 4, 6]),
        ({"cso_type": "National"}, [1, 2]),
        ({"section": "Health"}, [2, 3, 4]),
        ({"status": "ended"}, [3]),
        ({"start_from": "2024", "start_to": "2025"}, [1, 2]),
        ({"start_to": "2021"}, [4, 6]),
        ({"pd_q": "2026003"}, [3]),
        ({"start_from": "99999", "start_to": "20x6"}, [1, 2, 3, 4, 6]),  # not a year: no filter
        ({"office": "Beirut", "document_type": "SPD", "pd": "LEB/PCA2026001"}, [1, 2, 3, 4, 6]),  # not here
    ],
)
def test_each_filter_of_the_overall_tab(client_viewer, world, params, expected):
    _response, overall = _overall(client_viewer, **params)
    assert _numbers(overall) == expected
    assert overall["count"] == len(expected)


def test_the_start_years_never_filter_the_programme_documents_tab(client_viewer, world):
    response = client_viewer.get(PAGE, {"start_from": "2025", "start_to": "2025"})
    assert response.context["page_obj"].paginator.count == 5
    tabs = dict((label, href) for label, href, _ in _tabs(response.text))
    assert "start_from" not in tabs["Programme documents"] and "start_from" not in tabs["Indicators"]
    assert "start_from=2025" in tabs["Funded programme overall"]


def test_the_overall_filter_bar_has_the_old_page_s_filters_only(client_viewer, world):
    response, _overall_data = _overall(client_viewer)
    form = FILTER_FORM.search(response.text).group(0)
    names = set(FIELD_NAME.findall(form))
    assert names == {
        "tab",
        "pd_q",
        "donor",
        "grant",
        "partner",
        "cso_type",
        "section",
        "status",
        "start_from",
        "start_to",
    }
    assert '<input type="hidden" name="tab" value="overall">' in form
    for removed in ("office", "emergency", "partner_type", "cp_output", "document_type", "scope"):
        assert removed not in names
    # the start years are the programme documents' own
    assert '<option value="2020"' in form and '<option value="2025"' in form


def test_overall_downloads_hold_the_whole_filter(client_viewer, world):
    response, _overall_data = _overall(client_viewer, section="Health")
    assert "export=xlsx" in response.text and "export=csv" in response.text
    response = client_viewer.get(PAGE, {"tab": "overall", "section": "Health", "export": "csv"})
    assert response.status_code == 200
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert sorted(r["programme_document"] for r in rows) == [
        "LEB/PCA2026002",
        "LEB/PCA2026003",
        "LEB/PCA2026004",
    ]
    p3 = next(r for r in rows if r["programme_document"] == "LEB/PCA2026003")
    assert p3["days_to_end"] == "-1" and p3["funds_reserved_usd"] == "2500.00"
    assert p3["sections"] == "Health" and p3["donors"] == "Donor North"
    _no_people(text)
    response = client_viewer.get(PAGE, {"tab": "overall", "export": "xlsx"})
    book = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    sheet = list(book.active.iter_rows(values_only=True))
    assert len(sheet) == 1 + 5
    assert "unicef_cash" in sheet[0] and "funds_reserved_usd" in sheet[0]


def test_overall_pages_its_list(client_viewer, world):
    partner = world["alpha"]
    for n in range(60):
        PCA.objects.create(
            etl_id=f"97{n}", partner=partner, partner_name="Alpha Aid", number=f"LEB/SPD{n:03d}",
            title="More", status="active", start=datetime.date(2019, 1, 1),
        )  # fmt: skip
    _response, overall = _overall(client_viewer)
    assert overall["count"] == 65
    assert len(overall["rows"]) == 50  # the pages of every list
    _response, overall = _overall(client_viewer, page=2)
    assert len(overall["rows"]) == 15


# ----------------------------------------------------------------------------------------------- people
def _no_people(text: str) -> None:
    for person in PEOPLE:
        assert person not in text, person


def test_no_person_on_the_new_tabs(client_viewer, world):
    PCA.objects.filter(number="LEB/PCA2026001").update(unicef_focal_points=["Karim Sample", "+961 1 000000"])
    for params in ({"tab": "summary"}, {"tab": "summary", "scope": "all"}, {"tab": "overall"}):
        _no_people(client_viewer.get(PAGE, params).text)
    response = client_viewer.get(PAGE, {"tab": "overall", "export": "csv"})
    _no_people(b"".join(response.streaming_content).decode("utf-8-sig"))
    response = client_viewer.get(PAGE, {"tab": "overall", "export": "xlsx"})
    book = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    header = list(book.active.iter_rows(values_only=True))
    _no_people(repr(header))
    for column in ("focal", "officer", "email", "e-mail", "phone"):
        assert not any(column in str(name) for name in header[0]), column
    response = client_viewer.get(PAGE, {"tab": "summary", "export": "xlsx"})
    book = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    _no_people(repr([list(sheet.iter_rows(values_only=True)) for sheet in book.worksheets]))


# ------------------------------------------------------------------------------------------ performance
def _queries(client, params) -> int:
    with CaptureQueriesContext(connection) as queries:
        assert client.get(PAGE, params).status_code == 200
    return len(queries)


@pytest.mark.parametrize("tab", ["summary", "overall"])
def test_the_number_of_queries_does_not_grow_with_the_programme_documents(client_viewer, world, tab):
    _queries(client_viewer, {"tab": tab, "scope": "all"})  # the session and the kept choices are read once
    before = _queries(client_viewer, {"tab": tab, "scope": "all"})
    partner = world["beta"]
    for n in range(40):
        pca = PCA.objects.create(
            etl_id=f"98{n}", partner=partner, partner_name="Beta Relief", number=f"LEB/PCA2027{n:03d}",
            title="Extra", status="active", section_names=[f"Section {n % 4}"], total_budget="10",
            end=_day(n), start=datetime.date(2026, 1, 1), donors=["Donor East"],
        )  # fmt: skip
        dm.FundsReservation.objects.create(datamart_id=100 + n, intervention=pca, overall_amount=Decimal(5))
        dm.FundsReservationHeader.objects.create(datamart_id=100 + n, intervention=pca, actual_amt=Decimal(1))
    assert _queries(client_viewer, {"tab": tab, "scope": "all"}) == before


# ----------------------------------------------------------------------------------------------- the docs
def test_the_help_guide_and_operations_describe_the_two_tabs():
    root = Path(__file__).resolve().parents[2]
    guide = (root / "neurodb/help/guide/partner-progress.md").read_text()
    operations = (root / "docs/OPERATIONS.md").read_text()
    for text in (guide, operations):
        assert "Funded programme summary info" in text
        assert "Funded programme overall" in text
