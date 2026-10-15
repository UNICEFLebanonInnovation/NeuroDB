"""Progress reports' Report/Location and Report/Disaggregation in parts, and the Funds page's quick win (SPEC7 §3.3-3.4;
STAGE3 group B).

The two tabs answer their frame at once (the filter bar with its options stored for every worker and the tab's own
filters, the tabs) with one placeholder, ``table``: the tab's table, computed once for every worker
(``datamart.services.reporting_table``, keyed on the filter, the tab's filters, the day and the eTools jobs' latest
runs), with its pages and downloads, and no chart data (no chart of these tabs reads it). A change of filter answers the
table itself (it takes well under a second). The other two tabs stay one request and read the same stored filter
options. The Funds page's Partner Fund Monitoring and Funded Programs tabs read only the filter choices of the funds
reservations.

On the world of tests/reports/test_r4_views.py (``labelled``: one indicator's two progress reports at Akkar and Bekaa,
with their disaggregations) and ``funded`` for the Funds page.
"""

# ruff: noqa: F401, F811 (the fixtures come from the modules that define them)
from __future__ import annotations

import gettext
import html as html_module
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import caches
from django.http import QueryDict
from django.test import Client
from django.urls import reverse
from django.utils import translation

from neurodb.accounts.models import Section, User
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.datamart import keys, reporting
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.donors.models import DonorAccount
from tests.reports.test_pd_monitoring import datamart_synced
from tests.reports.test_r4_views import INDICATOR, funded, labelled
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

PAGE = "/partner-reporting/"  # reports:partner_reporting
TABS = ("locations", "disaggregations")  # the tabs whose table is a part
CHART_JSON = "reporting-chart-data"  # the Reporting progress tab's chart data; the tabs in parts carry none
RESULTS = "reporting-results"
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: measured on this world, plus 2. The frame warm (its filter options stored) is 9: the request's floor
# and the moment of the eTools figures (two queries); cold it also computes the filter options: 17. Drawn inline (the
# table stored): 8. The part with nothing stored computes the page's progress reports, as the old page did, and the
# tab's rows: 20, 22 with the disaggregation labels; another page of the same table reads it stored: 5.
# Cold the menu also reads the ActivityInfo state and its sources, kept per worker (SPEC_HOME §4): 23.
FRAME_QUERIES_COLD = 25
FRAME_QUERIES_WARM = 11
FRAME_QUERIES_INLINE = 10
PART_QUERIES = {"locations": 22, "disaggregations": 24}
PART_QUERIES_STORED = 7


@pytest.fixture
def world(labelled):
    return labelled


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _json_blocks(html: str) -> dict[str, object]:
    """The page's JSON blocks, its frame's asset addresses aside (base.html)."""
    found = [(name, body) for name, body in JSON_SCRIPT.findall(html) if name != "asset-urls"]
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _heavy_services_refuse(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    monkeypatch.setattr(datamart, "partner_reporting", refuse)
    for name in ("progress_reports", "location_rows", "disaggregation_rows", "filled_blocks"):
        monkeypatch.setattr(reporting, name, refuse)


def _tables(monkeypatch) -> Counter:
    """Count the computations of the stored tables (``datamart.partner_reporting`` runs once per computation)."""
    calls: Counter = Counter()
    real = datamart.partner_reporting

    def counted(params, today=None, **kwargs):
        calls["table"] += 1
        return real(params, today, **kwargs)

    monkeypatch.setattr(datamart, "partner_reporting", counted)
    return calls


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("reports:partner_reporting") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
@pytest.mark.parametrize("tab", TABS)
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, world, monkeypatch, tab):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE, {"tab": tab})
    assert response.status_code == 200
    html = text(response)
    assert "<title>Progress reports" in html and 'aria-label="Partner reporting filters"' in html
    assert 'aria-label="Progress report views"' in html  # the tabs
    assert list(placeholders(html)) == ["table"]
    assert f'id="{CHART_JSON}"' not in html
    for figure in ('id="report-locations"', 'id="report-disaggregations"', "Location total", "In the period"):
        assert figure not in html, figure
    assert (
        '<option value="Adolescent &amp; Youth"' in html
    )  # the filter bar's choices, stored for every worker
    # the Report/Disaggregation tab's own filters stay in the frame
    assert ('name="disaggregation"' in html) == (tab == "disaggregations")
    # the results area receives the part: not a live region; parts.js says its words after a swap (review R3)
    assert f'<div id="{RESULTS}" data-results-updated="The results were updated.">' in html
    assert "aria-live" not in html.split(f'<div id="{RESULTS}"', 1)[1].split(">", 1)[0]


@pytest.mark.shell
def test_every_tab_gives_the_words_said_after_a_swap_of_its_results(client_viewer, world):
    """The filter bar and the page links swap #reporting-results. On Report/Location and Report/Disaggregation it
    receives the table as a part, so it is not a live region, and a filter change there was no longer announced;
    the reports and partners tabs kept aria-live, so the same area behaved differently from tab to tab (review R3).
    Every tab's frame gives the words parts.js says after such a swap, never aria-live, and a swap answers the
    area's content, never a second area."""
    for tab in ("reports", "partners", *TABS):
        html = text(raw_get(client_viewer, PAGE, {"tab": tab}))
        areas = re.findall(rf'<div id="{RESULTS}"[^>]*>', html)
        assert areas == [f'<div id="{RESULTS}" data-results-updated="The results were updated.">'], tab
        assert html.count("data-results-updated=") == 1, tab
        swapped = raw_get(
            client_viewer,
            PAGE,
            {"tab": tab, "status": "accepted"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET=RESULTS,
        )
        assert swapped.status_code == 200, tab
        assert f'id="{RESULTS}"' not in text(swapped) and "data-results-updated" not in text(swapped), tab


# The table's room on each tab, as measured on the x20 parity world (reports/partials/reporting_table.html):
# --part-h at 1366 px, --part-h-md at 900 px, --part-h-sm at 390 px; its grey lines.
ROOM = {
    "locations": {"--part-h": "789px", "--part-h-md": "809px", "--part-h-sm": "1053px", "--part-rows": "20"},
    "disaggregations": {
        "--part-h": "828px",
        "--part-h-md": "848px",
        "--part-h-sm": "1134px",
        "--part-rows": "20",
    },
}


@pytest.mark.shell
def test_the_placeholder_keeps_the_room_of_the_tab_s_table(admin_user, viewer, world):
    for client in (_signed_in(viewer), _signed_in(admin_user)):
        for tab in TABS:
            html = text(client.get(PAGE, {"tab": tab}))
            attrs = placeholder_attributes(html)["table"]
            assert dict(item.split(": ", 1) for item in attrs["style"].split("; ")) == ROOM[tab]
            start = html.index('id="part-table"')
            assert html[start : html.index('class="part__slow', start)].count("skeleton__block--row") == 20


@pytest.mark.shell
def test_the_placeholder_carries_the_page_s_query(client_viewer, world):
    found = placeholder_attributes(text(client_viewer.get(PAGE, {"tab": "disaggregations"})))
    assert list(found) == ["table"]
    attrs = found["table"]
    assert urlsplit(attrs["hx-get"]).path == PAGE
    assert _query(attrs["hx-get"]) == {"tab": ["disaggregations"], "_part": ["table"]}
    assert attrs["class"] == "part part--table" and attrs["data-part-priority"] == "high"
    assert "data-part-group" not in attrs
    # the page has no first-load default: its own query, every key it reads, without the export
    query = {
        "tab": "locations",
        "year": "2026",
        "status": "accepted",
        "pd_status": "",
        "page": "2",
        "export": "x",
    }
    found = placeholders(text(client_viewer.get(PAGE, query)))
    assert _query(found["table"]) == {
        "tab": ["locations"],
        "year": ["2026"],
        "status": ["accepted"],
        "pd_status": [""],
        "page": ["2"],
        "_part": ["table"],
    }


@pytest.mark.shell
@pytest.mark.parametrize("tab", TABS)
def test_the_frame_stays_within_its_query_ceiling(client_viewer, world, tab, django_assert_max_num_queries):
    with django_assert_max_num_queries(FRAME_QUERIES_COLD):
        response = client_viewer.get(PAGE, {"tab": tab})
    assert response.status_code == 200 and list(placeholders(text(response))) == ["table"]
    with django_assert_max_num_queries(FRAME_QUERIES_WARM):
        response = client_viewer.get(PAGE, {"tab": tab, "year": "2026"})
    assert response.status_code == 200 and list(placeholders(text(response))) == ["table"]


# ------------------------------------------------------------------------------------------------- the part
@pytest.mark.parametrize("tab", TABS)
def test_the_part_answers_its_wrapper(client_viewer, world, tab):
    response = get_part(client_viewer, f"{PAGE}?tab={tab}", "table")
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == "table"
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "locations": ('id="report-locations"', "LB71", "Excel of the filter"),
        "disaggregations": ('id="report-disaggregations"', "Location total", "Sex:</span> subtotal"),
    }[tab]
    for figure in expected:
        assert figure in html, figure
    assert _json_blocks(html) == {}  # no chart data: no chart of the tab reads it
    assert executable(html) == []


@pytest.mark.parametrize("tab", TABS)
def test_the_part_stays_within_its_query_ceiling(client_viewer, world, tab, django_assert_max_num_queries):
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[tab]):
        response = client_viewer.get(f"{PAGE}?tab={tab}&_part=table", **HX)
    assert response.status_code == 200 and 'id="part-table"' in text(response)
    with django_assert_max_num_queries(PART_QUERIES_STORED):  # another page of the same table: stored
        response = client_viewer.get(f"{PAGE}?tab={tab}&page=2&_part=table", **HX)
    assert response.status_code == 200


@pytest.mark.parametrize("tab", TABS)
def test_a_donor_is_refused_the_part(client, world, tab):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    assert client.get(f"{PAGE}?tab={tab}&_part=table", **HX).status_code == 403


def test_a_visitor_asking_for_the_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?tab=locations&_part=table", **HX)
    assert response.status_code == 200 and "LB71" not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


@pytest.mark.parametrize("tab", TABS)
def test_a_filter_change_answers_the_table_itself(client_viewer, world, tab):
    """A change of filter (aimed at #reporting-results) answers the tabs and the table with its figures: the
    table takes well under a second (SPEC7_review item 3c)."""
    response = raw_get(
        client_viewer,
        PAGE,
        {"tab": tab, "status": "accepted"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET=RESULTS,
    )
    html = text(response)
    assert response.status_code == 200 and "<html" not in html and placeholders(html) == {}
    assert 'id="part-table" class="part is-loaded"' in html and "LB71" in html
    assert f'id="{CHART_JSON}"' not in html
    assert "QPR2" not in html  # the sent back report is left out by the filter


def test_the_other_tabs_stay_one_request(client_viewer, world):
    for tab in ("reports", "partners"):
        html = text(raw_get(client_viewer, PAGE, {"tab": tab}))
        assert placeholders(html) == {} and 'data-part-loaded="table"' not in html
        assert f'id="{CHART_JSON}"' in html


# ------------------------------------------------------------------------------------- one table, stored
def test_a_stored_table_is_drawn_in_the_frame(
    client_viewer, world, monkeypatch, django_assert_max_num_queries
):
    get_part(client_viewer, f"{PAGE}?tab=locations", "table")  # computed and stored
    _heavy_services_refuse(monkeypatch)
    with django_assert_max_num_queries(FRAME_QUERIES_INLINE):
        html = text(raw_get(client_viewer, PAGE, {"tab": "locations"}))
    assert placeholders(html) == {} and 'data-part-loaded="table"' in html and "LB71" in html
    assert list(placeholders(text(raw_get(client_viewer, PAGE, {"tab": "locations", "year": "2025"})))) == [
        "table"
    ]


def test_the_pages_read_the_stored_table(client_viewer, world, monkeypatch):
    calls = _tables(monkeypatch)
    for page in ("1", "2", "1"):
        assert get_part(client_viewer, f"{PAGE}?tab=locations&page={page}", "table").status_code == 200
        compute.reset_memo()
    assert calls["table"] == 1


@pytest.mark.parametrize("tab", TABS)
def test_the_table_never_waits_for_the_filter_options(client_viewer, world, tab):
    """The stored table reads none of the filter bar's choices. While another request computes them (their lock
    held), the table part still answers its table, never "pending" (a part does not wait for a lock): the work done
    for the table is not thrown away (review R1-B-1)."""
    caches["shared"].clear()  # nothing stored: neither the options nor the table
    compute.reset_memo()
    options_key = compute._full_key(f"pr-options:v{reporting.CACHE_VERSION}:{keys.fingerprint()}", True)
    lock = compute._ThreadLock(options_key)  # another request computing the filter options
    assert lock.acquire()
    try:
        response = client_viewer.get(f"{PAGE}?tab={tab}&_part=table", **HX)
    finally:
        lock.release()
    assert response.status_code == 200, response.status_code
    assert "X-NeuroDB-Part-Pending" not in response and 'id="part-table"' in text(response)
    assert ("LB71" if tab == "locations" else "Location total") in text(response)


def test_a_stored_table_never_answers_another_filter_or_tab(client_viewer, world):
    def rows(**params) -> int:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        context = get_part(client_viewer, f"{PAGE}?{query}", "table").context
        return context["page_obj"].paginator.count

    assert rows(tab="locations") == 3
    assert rows(tab="locations", status="accepted") == 2
    assert rows(tab="locations", year="2025") == 0
    assert rows(tab="disaggregations") == 3
    assert rows(tab="disaggregations", location="Bekaa") == 1
    assert rows(tab="disaggregations", disaggregation="Lebanese") == 1


def test_two_filters_never_share_a_key():
    filters = [
        reporting.ReportFilters(),
        reporting.ReportFilters(q="amel"),
        reporting.ReportFilters(year=2026),
        reporting.ReportFilters(statuses=["accepted"]),
        reporting.ReportFilters(report_types=["QPR"]),
        reporting.ReportFilters(sections=["Education"]),
        reporting.ReportFilters(sections=["Child_Protection"]),
        reporting.ReportFilters(report_numbers=["QPR1"]),
        reporting.ReportFilters(partners=["7"]),
        reporting.ReportFilters(partner_types=["CSO"]),
        reporting.ReportFilters(pd_statuses=[]),
        reporting.ReportFilters(pd_statuses=["closed"]),
        reporting.ReportFilters(emergency="yes"),
        reporting.ReportFilters(overdue=True),
    ]
    assert len({f.cache_key() for f in filters}) == len(filters)
    tab = [
        reporting.DisaggregationFilters(),
        reporting.DisaggregationFilters(indicator="girls"),
        reporting.DisaggregationFilters(location="girls"),
        reporting.DisaggregationFilters(p_code="LB1"),
        reporting.DisaggregationFilters(disaggregation="female"),
    ]
    assert len({f.cache_key() for f in tab}) == len(tab)


def test_the_page_s_links_and_the_filter_bar_share_the_key_of_one_filter(client_viewer, world, monkeypatch):
    """The page's own links carry no ``pd_status`` (the running PDs); the filter bar sends the running statuses it
    shows chosen. One filter, the same figures: one stored table (review R1-B-2). ``pd_status=`` alone (every
    status) and another choice keep keys of their own."""
    link = reporting.ReportFilters.from_params(QueryDict("section=Education&year=2026"))
    bar = reporting.ReportFilters.from_params(
        QueryDict("section=Education&pd_status=suspended&pd_status=signed&pd_status=active&year=2026")
    )
    every = reporting.ReportFilters.from_params(QueryDict("section=Education&pd_status=&year=2026"))
    closed = reporting.ReportFilters.from_params(QueryDict("section=Education&pd_status=closed&year=2026"))
    assert link.pd_statuses is None and bar.pd_statuses and every.pd_statuses == []
    assert link.cache_key() == bar.cache_key()
    assert len({link.cache_key(), every.cache_key(), closed.cache_key()}) == 3
    calls = _tables(monkeypatch)
    first = get_part(client_viewer, f"{PAGE}?tab=locations", "table").context["locations"]
    compute.reset_memo()
    bar_url = f"{PAGE}?tab=locations&pd_status=active&pd_status=signed&pd_status=suspended"
    assert get_part(client_viewer, bar_url, "table").context["locations"] == first
    assert calls["table"] == 1


def test_the_disaggregation_lines_are_made_for_the_page_shown_on_copies(client_viewer, world, monkeypatch):
    """The page's blocks get their lines; the stored table keeps none (the blocks are filled on copies), so the next
    page reads it as it was computed."""
    monkeypatch.setattr("neurodb.reports.views.DISAGGREGATION_BLOCKS", 2)
    context = get_part(client_viewer, f"{PAGE}?tab=disaggregations", "table").context
    shown = list(context["page_obj"])
    assert len(shown) == 2 and all("table_lines" in b for b in shown)
    assert sum("table_lines" in b for b in context["disaggregations"]["blocks"]) == 2
    compute.reset_memo()
    stored = datamart.reporting_table("disaggregations", {"tab": "disaggregations"})
    assert not any("table_lines" in b for b in stored["disaggregations"]["blocks"])
    assert list(stored) == ["disaggregations"]  # the table only: no chart data, which no chart reads


def test_a_stored_table_never_shows_one_person_s_view_to_another(admin_user, viewer, world):
    """Nothing in a stored table depends on the person asking: a table stored by an administrator's request answers
    a viewer with the same figures, and the other way round."""
    admin, reader = _signed_in(admin_user), _signed_in(viewer)
    for first, second in ((admin, reader), (reader, admin)):
        caches["shared"].clear()
        stored = get_part(first, f"{PAGE}?tab=disaggregations", "table").context
        read = get_part(second, f"{PAGE}?tab=disaggregations", "table").context
        assert read["disaggregations"] == stored["disaggregations"] and stored["disaggregations"]["blocks"]


class _EveryStringTranslated(gettext.NullTranslations):
    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_the_stored_values_read_the_same_in_every_language(world):
    """The tables and the filter options are stored once for every language (``lang_neutral``): nothing in them
    is translated."""

    def values():
        compute.reset_memo()
        caches["shared"].clear()
        return (
            datamart.reporting_table("locations", {"tab": "locations"}),
            datamart.reporting_table("disaggregations", {"tab": "disaggregations"}),
            reporting.filter_options(),
        )

    with translation.override("en"):
        english = values()
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        assert translation.gettext("Accepted") == "[Accepted]"
        other = values()
    finally:
        translation.deactivate()
    assert json.dumps(other, default=str, sort_keys=True) == json.dumps(english, default=str, sort_keys=True)


# ------------------------------------------------------------------------------------------- fresh after edit
def test_a_finished_etools_sync_shows_at_once(client_viewer, world, monkeypatch):
    calls = _tables(monkeypatch)

    def statuses() -> list[str]:
        rows = get_part(client_viewer, f"{PAGE}?tab=locations&pd_status=", "table").context["locations"][
            "rows"
        ]
        return sorted(r["report_status"] for r in rows)

    assert statuses() == ["Accepted", "Accepted", "Sent back"]
    # the sync reads the sent back report again, accepted now, and ends its run
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(report_status="Accepted")
    assert statuses() == ["Accepted", "Accepted", "Sent back"] and calls["table"] == 1  # stored
    datamart_synced()
    assert statuses() == ["Accepted", "Accepted", "Accepted"] and calls["table"] == 2
    # a run of a job that writes nothing here changes nothing
    SyncRun.objects.create(job=SyncRun.Job.WATCH, target="all", status=SyncRun.Status.RUNNING).finish(
        SyncRun.Status.SUCCEEDED
    )
    compute.reset_memo()
    statuses()
    assert calls["table"] == 2


def test_the_filter_options_are_stored_and_follow_a_sync(client_viewer, world, monkeypatch):
    calls: Counter = Counter()
    real = reporting.filter_options

    def counted():
        calls["options"] += 1
        return real()

    monkeypatch.setattr(reporting, "filter_options", counted)
    for tab in ("reports", "partners", "locations", "disaggregations"):
        assert raw_get(client_viewer, PAGE, {"tab": tab}).status_code == 200
        compute.reset_memo()
    assert calls["options"] == 1  # every tab reads the stored options
    datamart_synced()
    raw_get(client_viewer, PAGE)
    assert calls["options"] == 2


FILTER_BAR = re.compile(r'<form method="get" class="filter-bar".*?</form>', re.S)
SELECT = re.compile(r'<select[^>]*\sname="([^"]+)"[^>]*>(.*?)</select>', re.S)
OPTION = re.compile(r'<option value="([^"]*)"[^>]*>([^<]*)</option>')


def _filter_choices(html: str) -> dict[str, list[tuple[str, str]]]:
    """The choices the page's filter bar offers: per field, its options (value and label), selected or not."""
    bar = FILTER_BAR.search(html)
    assert bar, "the page has its filter bar"
    return {name: OPTION.findall(body) for name, body in SELECT.findall(bar.group(0))}


def test_the_stored_filter_options_are_the_same_for_every_person(admin_user, viewer, world, monkeypatch):
    """The filter options (``pr-options``) depend on nothing about the person asking. Stored by the frame of an
    administrator of one section (Education), they answer the frames of a viewer of another (Child Protection),
    on a tab in parts and on a tab drawn in one request, with the same choices in the filter bar, and the other way
    round: computed by either person, they are the same."""
    admin_user.section = Section.objects.create(name="Education", code="EDU")
    admin_user.save()
    viewer.section = Section.objects.create(name="Child protection", code="CP")
    viewer.save()
    calls: Counter = Counter()
    real = reporting.filter_options

    def counted():
        calls["options"] += 1
        return real()

    monkeypatch.setattr(reporting, "filter_options", counted)
    admin, reader = _signed_in(admin_user), _signed_in(viewer)
    tabs = ("locations", "reports")
    stored = []
    for first, second in ((admin, reader), (reader, admin)):
        caches["shared"].clear()
        compute.reset_memo()
        calls.clear()
        frames = {}
        for person in (first, second):
            for tab in tabs:
                frames[person, tab] = response = raw_get(person, PAGE, {"tab": tab})
                compute.reset_memo()
                assert response.status_code == 200
        assert calls["options"] == 1  # the first frame stored them; every other frame read them
        options = [response.context["data"]["options"] for response in frames.values()]
        assert all(o == options[0] for o in options) and options[0]["sections"]
        for tab in tabs:
            assert _filter_choices(text(frames[first, tab])) == _filter_choices(text(frames[second, tab]))
        stored.append(options[0])
    assert stored[0] == stored[1]


# ------------------------------------------------------------------------------------------------ the links
def test_the_pages_of_the_table_reload_their_part(client_viewer, world, monkeypatch):
    monkeypatch.setattr("neurodb.reports.views.DISAGGREGATION_BLOCKS", 1)
    html = text(get_part(client_viewer, f"{PAGE}?tab=disaggregations&year=2026", "table"))
    link = re.search(r'<a class="page-link" href="([^"]+)" hx-get="([^"]+)"[^>]*hx-target="([^"]+)"[^>]*'
                     r'hx-push-url="([^"]+)"[^>]*>2</a>', html)  # fmt: skip
    assert link, "the table has a second page"
    href, hx_get, target, pushed = (html_module.unescape(g) for g in link.groups())
    assert href == "?tab=disaggregations&year=2026&page=2"  # as before
    assert target == "#part-table" and _query(hx_get) == {
        "tab": ["disaggregations"],
        "year": ["2026"],
        "page": ["2"],
        "_part": ["table"],
    }
    assert pushed == f"{PAGE}?tab=disaggregations&year=2026&page=2"
    second = client_viewer.get(hx_get, **HX)
    assert second.context["page_obj"].number == 2 and "table_lines" in second.context["page_obj"][0]


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    "tab=locations",
    "tab=locations&year=2026&pd_status=",
    "tab=locations&status=accepted",
    "tab=locations&section=No+such+section",
    "tab=locations&page=2",
    "tab=disaggregations",
    "tab=disaggregations&disaggregation=female",
    "tab=disaggregations&p_code=LB8",
    "tab=disaggregations&status=sent_back",
    "tab=disaggregations&page=2",
]


@pytest.mark.parametrize("role", ["viewer", "staff"])
@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(admin_user, viewer, world, role, query, monkeypatch):
    monkeypatch.setattr("neurodb.reports.views.PAGE_SIZE", 2)
    monkeypatch.setattr("neurodb.reports.views.DISAGGREGATION_BLOCKS", 2)
    client = _signed_in(viewer if role == "viewer" else admin_user)
    url = f"{PAGE}?{query}"
    assembled = assemble(client, url)
    assert list(assembled.parts) == ["table"] and assembled.parts["table"].status_code == 200
    drawn = eager(client, url)
    assert drawn.status_code == 200
    links = LINK.findall(text(assembled.parts["table"]))
    assert all("_part" not in link for link in links), links
    assert executable(text(assembled.parts["table"])) == []
    assert all("_part" not in link and "_eager" not in link for link in LINK.findall(text(drawn)))
    assert placeholders(text(drawn)) == {}
    assert normalise(assembled.html) == normalise(text(drawn))
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone == {}  # no chart data: no chart of these tabs reads it
    assert _json_blocks(text(assembled.parts["table"])) == alone


@pytest.mark.shell
@pytest.mark.parametrize("tab", TABS)
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world, tab):
    frame = text(client_viewer.get(PAGE, {"tab": tab}))
    shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert len(shown) == 1
    address = PAGE + html_module.unescape(shown[0])
    assert address == f"{PAGE}?tab={tab}&_eager=1"
    page = text(client_viewer.get(address))
    assert placeholders(page) == {} and "part__skeleton" not in page and "Show the figures" not in page
    assert "LB71" in page and f'id="{CHART_JSON}"' not in page
    assert "_eager=1" not in "".join(LINK.findall(page))


@pytest.mark.parametrize("tab", TABS)
def test_the_exports_stay_whole_files(client_viewer, world, tab, monkeypatch):
    response = client_viewer.get(PAGE, {"tab": tab, "export": "csv", "pd_status": ""})
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert INDICATOR.encode() in b"".join(response.streaming_content)
    limit = "LOCATION_LIMIT" if tab == "locations" else "DISAGGREGATION_LIMIT"
    monkeypatch.setattr(reporting, limit, 1)
    assert client_viewer.get(PAGE, {"tab": tab, "export": "csv", "pd_status": ""}).status_code == 400


# ------------------------------------------------------------------------------------- Funds: the quick win
@pytest.mark.parametrize("tab", ["contributions", "programmes"])
def test_the_funds_tabs_read_only_the_filter_choices_of_the_reservations(
    client_viewer, funded, tab, monkeypatch
):
    """Partner Fund Monitoring and Funded Programs draw the filter bar's choices and their own rows: they read the
    choices alone (the same as the reservations tab's), never the whole funds reservations computation, and carry
    no chart data (no chart of theirs reads it)."""
    reservations = client_viewer.get(reverse("reports:funds")).context["data"]["options"]

    def refuse(params):
        raise AssertionError("the whole funds reservations computation")

    monkeypatch.setattr(datamart, "funds", refuse)
    response = client_viewer.get(reverse("reports:funds"), {"tab": tab})
    assert response.status_code == 200
    assert response.context["data"]["options"] == reservations == datamart.funds_options()
    assert reservations["donors"] and reservations["grants"]
    assert 'id="funds-chart-data"' not in text(response)
    assert "LEBA/PCA2023583/PD20261704-3" in text(response)


def test_the_funds_reservations_tab_keeps_its_charts(client_viewer, funded):
    response = client_viewer.get(reverse("reports:funds"))
    data = _json_blocks(text(response))["funds-chart-data"]
    assert set(data) == {"by_donor", "by_year", "disbursed_by_year"} and data["by_donor"]
