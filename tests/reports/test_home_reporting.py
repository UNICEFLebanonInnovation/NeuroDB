"""Home led by partner reporting (SPEC_focus_menu §2, §4.4): the Progress reports block and the indicator block
show exactly the figures of the Progress reports and Partner monitoring pages at the addresses Home links to, the
programme documents to follow up count what their own link opens, and the rest of Home waits in *More on Home*.

The world, on the suite's day (15 October 2026): three running programme documents and a draft one, with
progress reports in all six statuses.

- LEB/PD1 (Partner Alpha, Child Protection): Accepted, Overdue, Due, Sent back (its reference is not the PD's
  number, as the sync can link a report by its eTools id) and Not yet due;
- LEB/PD12 (Partner Beta, Education): Submitted, and two Overdue, one of them with a blank reference;
- LEB/PD3 (Partner Gamma, no section anywhere): Accepted and Due;
- LEB/PD4, a draft (never counted), and a report naming no programme document (never counted).
"""

from __future__ import annotations

import datetime
import itertools
import pickle
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN
from neurodb.datamart import models as dm
from neurodb.datamart import monitoring, reporting
from neurodb.datamart import services as datamart
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports import home as home_service
from neurodb.reports import home_views
from tests.web.parts import HX, get_part, placeholder_attributes, raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/"
CP, EDU = "Child Protection", "Education"
TILE = re.compile(
    r'<div class="kpi__label">([^<]+)</div>\s*<div class="kpi__value"[^>]*>(?:<a href="[^"]*">)?([^<]+)<'
)
_ids = itertools.count(1)


def _pd(partner, number, *, status="active", sections=()):
    return PCA.objects.create(
        etl_id=number,
        partner=partner,
        partner_name=partner.name,
        number=number,
        title=f"Programme {number}",
        status=status,
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
        section_names=list(sections),
    )


def _indicator(pd, title, section):
    n = next(_ids)
    dm.PDIndicator.objects.create(
        datamart_id=n,
        source_id=n * 100,
        intervention=pd,
        pd_reference_number=pd.number,
        title=title,
        section_name=section,
        lower_result_name="1.1 Services",
        target_numerator=Decimal(1000),
        baseline_numerator=Decimal(0),
        display_type="number",
        location_name="Akkar",
    )


def _report(pd, partner, number, end, word="", *, kind="QPR", section="", ref=None, submitted=None, due=None):
    """One progress report of two indicators (two indicator reports), one row each, in Akkar."""
    for title in ("# of children reached", "# of caregivers reached"):
        dm.ReportedIndicator.objects.create(
            datamart_id=next(_ids),
            partner=partner,
            intervention=pd,
            partner_name=partner.name,
            pd_reference_number=pd.number if ref is None and pd is not None else (ref or ""),
            progress_report=f"PR-{number}-{pd.number if pd else 'none'}",
            report_number=number,
            report_type=kind,
            report_status=word,
            period_start=end - datetime.timedelta(days=89),
            period_end=end,
            due_date=due or end + datetime.timedelta(days=15),
            submission_date=submitted,
            section=section,
            indicator=title,
            target="1000",
            location="Akkar",
            achievement_in_period="10",
            total_cumulative_progress="10",
        )


@pytest.fixture
def world(db):
    alpha = PartnerOrganization.objects.create(etl_id="1", name="Partner Alpha", partner_type="CSO")
    beta = PartnerOrganization.objects.create(etl_id="2", name="Partner Beta", partner_type="CSO")
    gamma = PartnerOrganization.objects.create(etl_id="3", name="Partner Gamma", partner_type="CSO")
    one = _pd(alpha, "LEB/PD1", sections=[CP])
    twelve = _pd(beta, "LEB/PD12", sections=[EDU])
    three = _pd(gamma, "LEB/PD3")
    draft = _pd(gamma, "LEB/PD4", status="draft")
    for pd, section in ((one, CP), (twelve, EDU), (three, ""), (draft, CP)):
        _indicator(pd, "# of children reached", section)
        _indicator(pd, "# of caregivers reached", section)
    d = datetime.date
    _report(one, alpha, "QPR1", d(2026, 3, 31), "Accepted", section=CP, submitted=d(2026, 4, 10))
    _report(one, alpha, "QPR2", d(2026, 6, 30), section=CP)  # due 15 July: overdue
    _report(one, alpha, "QPR3", d(2026, 9, 30), section=CP)  # due 15 October: due
    _report(one, alpha, "HR1", d(2026, 8, 31), "Sent back", kind="HR", section=CP, ref="LEB/PD1-OLD")
    _report(one, alpha, "QPR4", d(2026, 12, 31), section=CP)  # not yet due
    _report(twelve, beta, "QPR1", d(2026, 6, 30), "Submitted", section=EDU, submitted=d(2026, 7, 10))
    _report(twelve, beta, "QPR2", d(2026, 6, 30), section=EDU, ref="")  # overdue, no reference of its own
    _report(twelve, beta, "QPR3", d(2026, 9, 30), section=EDU, due=d(2026, 10, 10))  # overdue
    _report(three, gamma, "QPR1", d(2026, 3, 31), "Accepted", submitted=d(2026, 4, 10))
    _report(three, gamma, "QPR2", d(2026, 9, 30))  # due
    _report(draft, gamma, "QPR1", d(2026, 6, 30), section=CP)  # a draft PD: never read
    _report(None, gamma, "QPR9", d(2026, 6, 30), ref="")  # names no programme document: never read
    return {"one": one, "twelve": twelve, "three": three, "draft": draft}


@pytest.fixture
def own_viewer(viewer):
    viewer.section = Section.objects.create(name=CP, code="CP")
    viewer.save()
    return viewer


@pytest.fixture
def superuser(db, roles):
    return User.objects.create_user(
        username="super",
        email="super@example.org",
        password="super-pass-123456",
        is_staff=True,
        is_superuser=True,
    )


def _home(client, query: str = "?section="):
    """Home put together from its parts (the test client asks for each, strict ones too)."""
    response = client.get(PAGE + query)
    assert response.status_code == 200
    return response


def _figures(response) -> dict:
    return {
        "reporting": response.context["reporting"],
        "pd_rows": response.context["pd_rows"],
        "tile_urls": response.context["tile_urls"],
        "partners_url": response.context["partners_url"],
        "indicators": response.context["indicators"],
        "status_urls": response.context["status_urls"],
    }


def _tiles(html: str, start: str, end: str | None = None) -> dict[str, str]:
    """The tiles drawn between two marks of ``html``: {label: value as written}."""
    block = html[html.index(start) :]
    if end is not None:
        block = block[: block.index(end)]
    return {label.strip(): value.strip() for label, value in TILE.findall(block)}


def _words(html: str) -> str:
    """The text a person reads: the numbers' ``data-figure`` spans (the figure trail's keys) taken away."""
    return re.sub(r'<span data-figure="[^"]*">([^<]*)</span>', r"\1", html)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


# ------------------------------------------------------------------------- Progress reports: the same figures
@pytest.mark.parametrize("query", ["?section=", f"?section={CP.replace(' ', '+')}", f"?section={EDU}"])
def test_the_tiles_are_the_progress_reports_page_s_at_their_link(client_viewer, world, query):
    home = _home(client_viewer, query)
    figures = _figures(home)
    counts = figures["reporting"]["counts"]
    page = client_viewer.get(figures["tile_urls"]["total"])
    assert page.status_code == 200
    assert page.context["data"]["counts"] == counts  # indicator reports and progress reports, per status
    home_html, page_html = text(home), text(page)
    drawn = _tiles(home_html, 'id="home-pr-title"', 'id="home-pds-title"')
    assert drawn == _tiles(page_html, 'class="kpi-grid kpi-grid--7"', '<p class="small text-muted mb-3">')
    assert len(drawn) == 7
    for s in counts["statuses"]:  # each status tile opens the page on that status, with the same number
        status_page = client_viewer.get(figures["tile_urls"][s["key"]])
        assert status_page.context["data"]["counts"]["indicator_reports"] == s["indicator_reports"]
        assert status_page.context["data"]["counts"]["progress_reports"] == s["progress_reports"]


def test_every_status_is_counted_and_what_no_page_counts_stays_out(client_viewer, world):
    counts = _figures(_home(client_viewer))["reporting"]["counts"]
    progress = {s["key"]: s["progress_reports"] for s in counts["statuses"]}
    assert progress == {
        "accepted": 2,
        "submitted": 1,
        "sent_back": 1,
        "due": 2,
        "overdue": 3,
        "not_yet_due": 1,
    }
    # two indicators per report; the draft PD's and the report naming no PD are never read
    assert (counts["progress_reports"], counts["indicator_reports"]) == (10, 20)


@pytest.mark.parametrize("query", ["?section=", f"?section={EDU}"])
def test_each_programme_document_row_counts_what_its_link_opens(client_viewer, world, query):
    figures = _figures(_home(client_viewer, query))
    rows = figures["pd_rows"]
    assert rows, "the world has reports to follow up"
    for row in rows:
        page = client_viewer.get(row["reports_url"])
        summary = page.context["data"]["summary"]["statuses"]
        assert {key: row["counts"][key] for key in reporting.STATUS_KEYS} == {
            key: summary.get(key, 0) for key in reporting.STATUS_KEYS
        }, row["pd_reference_number"]
        partners_tab = client_viewer.get(row["reports_url"] + "&tab=partners")
        assert len(partners_tab.context["data"]["partners"]["follow_up"]) == row["follow_up"]
        assert _query(row["reports_url"])["pd"] == [row["pd_reference_number"]]
        # Home's sections, written out (none for every section: the page's own default)
        assert _query(row["reports_url"]).get("section", []) == ([] if query == "?section=" else [EDU])


def test_the_rows_name_their_programme_document_by_its_number_whatever_the_reports_say(client_viewer, world):
    rows = {row["pd_reference_number"]: row for row in _figures(_home(client_viewer))["pd_rows"]}
    # a report whose reference is not its PD's number, and one with no reference: under their PD's number
    assert "LEB/PD1-OLD" not in rows and "" not in rows
    assert rows["LEB/PD1"]["counts"]["sent_back"] == 1  # the HR report referenced LEB/PD1-OLD
    assert rows["LEB/PD12"]["counts"]["overdue"] == 2  # one of them with a blank reference
    # the most pressing first: the most overdue, then sent back, due, submitted
    assert [row["pd_reference_number"] for row in rows.values()] == ["LEB/PD12", "LEB/PD1", "LEB/PD3"]
    # the oldest due date of its Due or Overdue reports: its overdue report's, long past
    assert rows["LEB/PD1"]["oldest_due"] == datetime.date(2026, 7, 15)
    assert rows["LEB/PD3"]["sections"] == "" and rows["LEB/PD1"]["sections"] == CP
    for row in rows.values():  # every row of a PD NeuroDB holds opens its indicators on Program Performance
        assert _query(row["indicators_url"])["view"] == ["table"]
        assert _query(row["indicators_url"])["pd"] == [row["pd_reference_number"]]


@pytest.mark.parametrize("named", [f"{EDU}, {CP}", ""])
def test_a_row_s_indicators_link_shows_them_whichever_section_they_sit_in(client_viewer, world, named):
    """A programme document of two sections whose indicators all sit in one: Progress reports counts it under
    the other too (its reports name both, or name none and the PD names both), while Partner monitoring reads
    each indicator's own section. Its Indicators link names no section, so it still shows them."""
    alpha = PartnerOrganization.objects.get(name="Partner Alpha")
    both = _pd(alpha, "LEB/PD77", sections=[EDU, CP])
    _indicator(both, "# of children reached", EDU)
    _indicator(both, "# of caregivers reached", EDU)
    _report(both, alpha, "QPR1", datetime.date(2026, 6, 30), section=named)  # due 15 July: overdue
    home = _home(client_viewer, f"?section={CP.replace(' ', '+')}")
    row = next(r for r in _figures(home)["pd_rows"] if r["pd_reference_number"] == "LEB/PD77")
    assert row["counts"]["overdue"] == 1
    table = get_part(client_viewer, row["indicators_url"], "table")
    assert len(table.context["rows"]) == 2
    assert {r.pd.number for r in table.context["rows"]} == {"LEB/PD77"}
    query = _query(row["indicators_url"])  # every section, this year, every PD running in it
    assert query["section"] == [""] and query["pd"] == ["LEB/PD77"]
    assert query["year"] == ["2026"] and query["scope"] == ["year"]


def test_the_partners_line_is_the_partners_tab(client_viewer, world):
    home = _home(client_viewer)
    figures = _figures(home)
    page = client_viewer.get(figures["partners_url"])
    perf = page.context["data"]["partners"]
    line = figures["reporting"]["partners"]
    assert (line["partners"], line["reported"], line["not_reported"]) == (
        perf["partners"],
        perf["reported"],
        perf["not_reported"],
    )
    assert (line["partners"], line["reported"], line["not_reported"]) == (3, 3, 0)
    assert (
        "3 partners have progress reports here: 3 have reported (Submitted or Accepted), 0 have not yet."
        in _words(text(home))
    )


# ------------------------------------------------------------------------- Partner monitoring: the same figures
@pytest.mark.parametrize("query", ["?section=", f"?section={CP.replace(' ', '+')}"])
def test_the_indicator_tiles_are_the_partner_monitoring_grid_s(client_viewer, world, query):
    home = _home(client_viewer, query)
    figures = _figures(home)
    mine = figures["indicators"]
    monitoring_url = home.context["monitoring_url"]
    assert _query(monitoring_url)["scope"] == ["year"] and _query(monitoring_url)["year"] == ["2026"]
    grid = get_part(client_viewer, monitoring_url, "summary")
    theirs = grid.context["data"]
    for key in ("indicators", "programme_documents", "partners", "status_counts"):
        assert mine[key] == theirs[key], key
    drawn = _tiles(text(home), 'id="home-pm-title"', 'class="home-fold"')
    assert list(drawn) == [
        "Indicators",
        "On track",
        "Off track",
        "Not reported",
        "Programme documents",
        "Partners",
    ]
    # the standing part's Delivery status reads the same filter: the same counts
    delivery = home.context["data"]["delivery"]
    assert delivery["indicators"] == mine["indicators"]
    assert {k: v for k, v in delivery["status_counts"].items() if v} == {
        k: v for k, v in mine["status_counts"].items() if v
    }
    for key, url in figures["status_urls"].items():
        assert _query(url)["status"] == [key] and _query(url)["scope"] == ["year"]


def test_the_indicator_block_reads_the_grid_s_stored_rows(client_viewer, world, monkeypatch):
    """The part reads the key the grid's summary part reads at Home's link: computed once, the grid draws it
    without computing it again."""
    home = _home(client_viewer)
    built = []
    real = monitoring.indicators
    monkeypatch.setattr(monitoring, "indicators", lambda *a, **k: built.append(1) or real(*a, **k))
    grid = raw_get(client_viewer, home.context["monitoring_url"])
    assert 'data-part-loaded="summary"' in text(grid) and built == []


# ------------------------------------------------------------------------- the day and the links
def test_in_january_the_reports_still_owed_from_last_year_stay_on_home(client, viewer, world, site_today):
    site_today.set(datetime.date(2027, 1, 10))
    client.force_login(viewer)  # signed in on that day (a session from October would have expired)
    figures = _figures(_home(client))
    counts = figures["reporting"]["counts"]
    assert {s["key"]: s["progress_reports"] for s in counts["statuses"]}["overdue"] == 5  # every period
    page = client.get(figures["tile_urls"]["total"])
    assert page.context["data"]["counts"] == counts
    assert page.context["data"]["today"] == datetime.date(2027, 1, 10)
    # the indicator block is this year's: Partner monitoring's 2027
    assert _query(_home(client).context["monitoring_url"])["year"] == ["2027"]


def test_a_first_load_writes_home_s_sections_into_every_link(client_viewer, own_viewer, world):
    home = _home(client_viewer, "")
    assert home.context["progress_url"] == reverse("reports:partner_reporting") + "?section=Child+Protection"
    assert _query(home.context["monitoring_url"])["section"] == [CP]
    figures = _figures(home)
    for url in [figures["partners_url"], *figures["tile_urls"].values(), *figures["status_urls"].values()]:
        assert _query(url)["section"] == [CP], url
    for row in figures["pd_rows"]:
        assert _query(row["reports_url"])["section"] == [CP]
    # Home's numbers are the page's at Home's own link (the menu's bare address opens every section)
    page = client_viewer.get(home.context["progress_url"])
    assert page.context["data"]["counts"] == figures["reporting"]["counts"]
    html = text(home)
    assert f'href="{home.context["progress_url"]}"' in html and "Partner reporting: your section" in html


def test_the_page_reads_its_day_where_home_reads_it(client_viewer, world, monkeypatch):
    monkeypatch.setattr(datamart, "reporting_today", lambda: datetime.date(2026, 7, 1))
    page = client_viewer.get(reverse("reports:partner_reporting"))
    assert page.context["data"]["today"] == datetime.date(2026, 7, 1)
    assert datamart.reporting_figures({})["today"] == datetime.date(2026, 7, 1)


# ------------------------------------------------------------------------- the pd filter of Progress reports
def test_the_pd_filter_matches_exactly_and_by_the_pd_s_number(world):
    one = datamart.partner_reporting({"pd": "LEB/PD1"}, datetime.date(2026, 10, 15))
    assert {r["intervention_id"] for r in one["reports"]} == {world["one"].pk}  # never LEB/PD12
    assert one["counts"]["progress_reports"] == 5  # the report referenced LEB/PD1-OLD included
    twelve = datamart.partner_reporting({"pd": "LEB/PD12"}, datetime.date(2026, 10, 15))
    assert twelve["counts"]["progress_reports"] == 3  # the one with a blank reference included
    old = reporting.ReportFilters.from_params({"pd": "LEB/PD1-OLD"})
    assert old.queryset(datetime.date(2026, 10, 15)).count() == 2  # its own reference finds it too
    keys = {
        reporting.ReportFilters.from_params({"pd": ref}).cache_key() for ref in ("", "LEB/PD1", "LEB/PD12")
    }
    assert len(keys) == 3


def test_the_page_keeps_the_pd_filter_says_so_and_offers_to_drop_it(client_viewer, world):
    page = client_viewer.get(reverse("reports:partner_reporting") + "?pd=LEB%2FPD1&section=Education+Extra")
    html = text(page)
    assert '<input type="hidden" name="pd" value="LEB/PD1">' in html
    note = re.search(r'<p class="small mb-2" id="reporting-pd-note">(.*?)</p>', html, re.S)
    assert note and "Programme document LEB/PD1 only" in note.group(1)
    drop = re.search(r'href="([^"]+)">Show every programme document<', note.group(1)).group(1)
    assert "pd" not in _query(drop.replace("&amp;", "&")) and _query(drop.replace("&amp;", "&"))["section"]
    # a section the options do not hold (Home's names come from the PD indicators) stays chosen
    assert '<option value="Education Extra" selected>Education Extra</option>' in html
    for link in page.context["downloads"]:  # the exports carry it
        assert _query(link["url"])["pd"] == ["LEB/PD1"]
    every = text(client_viewer.get(reverse("reports:partner_reporting")))
    assert 'name="pd"' not in every and "reporting-pd-note" not in every


def test_only_progress_reports_keeps_a_chosen_value_outside_its_options(client_viewer, world):
    html = text(client_viewer.get(reverse("reports:action_points") + "?section=Nowhere"))
    assert 'name="section"' in html and '<option value="Nowhere" selected>' not in html


# ------------------------------------------------------------------------- the fold
@pytest.mark.shell
def test_more_on_home_is_folded_for_every_role_and_its_parts_wait(client_viewer, world):
    html = text(client_viewer.get(PAGE + "?section="))
    fold = html[html.index('<details class="home-fold"') :]
    assert fold.startswith('<details class="home-fold">')  # closed
    assert '<summary class="home-fold__summary">More on Home: today\'s tasks, what changed' in fold
    found = placeholder_attributes(html)
    assert list(found)[:2] == ["reporting", "indicators"]
    assert all("data-part-lazy" not in found[name] for name in ("reporting", "indicators"))
    for name in ("for-you", "tasks", "since", "standing", "programmes"):
        assert found[name]["data-part-lazy"] == "strict", name
    # the old purpose and its links, at the top of the fold: the brief stays one click away for everyone
    assert re.search(r'<p class="home-fold__purpose">What needs you today.*?management brief</a>', fold, re.S)
    assert "The rest of Home is under <em>More on Home</em>." in html
    assert html.index('id="home-prp-title"') < html.index('class="home-fold"')


@pytest.mark.shell
def test_more_on_home_is_open_for_a_superuser_and_a_role_whose_menu_shows_for_you(
    client, superuser, viewer, world, menu_choice
):
    client.force_login(superuser)
    html = text(client.get(PAGE + "?section="))
    assert '<details class="home-fold" open>' in html
    assert "data-part-lazy" not in "".join(str(a) for a in placeholder_attributes(html).values())
    assert "How your partners are reporting, then what needs you today" in html
    client.force_login(viewer)
    assert '<details class="home-fold">' in text(client.get(PAGE + "?section="))
    from neurodb.indicators.services import navigation

    menu_choice("Viewer", [*navigation.FOCUSED, "watch:for_you"])
    assert '<details class="home-fold" open>' in text(client.get(PAGE + "?section="))
    menu_choice("Viewer", [*navigation.FOCUSED, "reports:daily_review"])
    assert home_views.more_open(viewer) is True
    menu_choice("Viewer", list(navigation.FOCUSED))
    assert home_views.more_open(viewer) is False


# ------------------------------------------------------------------------- the parts
@pytest.mark.shell
def test_the_indicator_block_is_drawn_inline_once_stored_the_reporting_block_never(
    client_viewer, world, monkeypatch
):
    get_part(client_viewer, PAGE + "?section=", "indicators")  # computes and stores the grid's rows
    html = text(raw_get(client_viewer, PAGE + "?section="))
    assert 'data-part-loaded="indicators"' in html and "Indicator progress in 2026" in html
    # Progress reports' figures are computed at each request, as the page computes them: never stored, so
    # Home shows the page's figure of the moment, also during a sync
    assert "reporting" in placeholder_attributes(html)
    calls = []
    real = datamart.partner_reporting
    monkeypatch.setattr(datamart, "partner_reporting", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(2):
        assert client_viewer.get(f"{PAGE}?section=&_part=reporting", **HX).status_code == 200
    assert calls == [1, 1]


def test_the_part_answers_hold_no_person(client, admin_user, world):
    PCA.objects.filter(pk=world["one"].pk).update(
        unicef_focal_points=["Focal Point One", "focal@example.org"]
    )
    client.force_login(admin_user)
    response = get_part(client, PAGE + "?section=", "reporting")
    dumped = repr(response.context["reporting"]) + repr(response.context["pd_rows"])
    for word in ("focal", "Focal Point One", "@example.org", admin_user.username, "submitted_by"):
        assert word not in dumped, word
    pickle.dumps(response.context["reporting"])  # plain data
    assert "Focal Point One" not in text(response)


def test_no_etools_data_reads_one_line_in_each_block(client_viewer, db):
    html = text(_home(client_viewer))
    assert html.count("No eTools data yet: the eTools Datamart sync has not run on this server.") >= 2
    reporting_block = html[html.index('id="home-pr-title"') : html.index('id="home-pm-title"')]
    assert "kpi-grid" not in reporting_block


def test_a_scope_with_true_zeros_keeps_its_tiles(client_viewer, world):
    """A section with no progress reports at all keeps its seven tiles of 0, and says it has none: never that
    every report is in order, nor that 0 partners have reported."""
    html = text(_home(client_viewer, "?section=Nowhere"))
    block = html[html.index('id="home-pr-title"') : html.index('id="home-pm-title"')]
    assert "No eTools data yet" not in html.split('class="home-fold"')[0]
    assert '<p class="home-empty">No progress reports here.</p>' in block
    assert "Nothing to follow up" not in block and "partners have progress reports here" not in block
    assert 'class="home-prp__partners"' not in block
    assert len(_tiles(html, 'id="home-pr-title"', 'id="home-pds-title"')) == 7


def test_a_scope_whose_reports_need_no_follow_up_says_so(client_viewer, world):
    """Reports that are all Accepted (or not yet due): nothing to follow up, and the partners line."""
    delta = PartnerOrganization.objects.create(etl_id="4", name="Partner Delta", partner_type="CSO")
    pd, d = _pd(delta, "LEB/PD40", sections=["Health"]), datetime.date
    _report(pd, delta, "QPR1", d(2026, 3, 31), "Accepted", section="Health", submitted=d(2026, 4, 10))
    _report(pd, delta, "QPR4", d(2026, 12, 31), section="Health")  # not yet due
    home = _home(client_viewer, "?section=Health")
    assert _figures(home)["reporting"]["counts"]["progress_reports"] == 2 and not _figures(home)["pd_rows"]
    html = text(home)
    block = html[html.index('id="home-pr-title"') : html.index('id="home-pm-title"')]
    assert "Nothing to follow up: every report here is Accepted or not yet due.</p>" in block
    assert "No progress reports here." not in block
    line = "1 partner has progress reports here: 1 has reported (Submitted or Accepted), 0 has not yet."
    assert line in _words(block)


# ------------------------------------------------------------------------- accessibility
def test_the_table_is_labelled_its_counts_carry_their_unit_and_their_word(client_viewer, world):
    html = text(_home(client_viewer))
    table = html[
        html.index('<table class="table table-sm home-pds"') : html.index("</table>", html.index("home-pds"))
    ]
    assert 'aria-labelledby="home-pds-title"' in table and 'id="home-pds-title"' in html
    assert '<th scope="colgroup" colspan="4" class="home-pds__group">Progress reports</th>' in table
    assert table.count('<th scope="col"') == 8 and table.count('<th scope="row"') == 3
    # the date column says what it holds: the oldest due date, a past one for an overdue report
    assert re.findall(r'<th scope="col"[^>]*>([^<]+)', table) == [
        "Programme document",
        "Partner",
        "Overdue",
        "Sent back",
        "Due",
        "Submitted",
        "Oldest due date",
        "Indicators",
    ]
    assert "15 Jul 2026" in table
    assert (
        '<span class="pill__dot" aria-hidden="true"></span>2 overdue</span>' in table
    )  # the word, not colour
    assert "1 sent back</span>" in table
    assert '<span class="home-muted">0</span>' in table
    names = re.findall(r'>Indicators<span class="visually-hidden"> (of [^<]+)</span></a>', table)
    assert names == [
        "of LEB/PD12 on Partner monitoring",
        "of LEB/PD1 on Partner monitoring",
        "of LEB/PD3 on Partner monitoring",
    ]
    wrapped = html[: html.index('<table class="table table-sm home-pds"')]
    # scrolls in its box on a phone, a box on a panel: the box's scroll cues are drawn in the panel's colour (on
    # the page's background they showed as pale patches at both ends of each row)
    assert wrapped.endswith('<div class="panel p-0 home-pds__panel"><div class="table-wrap">')


def test_the_focused_menu_on_home_marks_home_and_groups_partner_reporting(client_viewer, world):
    html = text(_home(client_viewer))
    assert '<div class="nav-group" role="group" aria-labelledby="nav-title-prp">' in html
    current = re.findall(r'<a class="nav-item-link[^"]*" href="([^"]+)" aria-current="page">', html)
    assert current == ["/"]


def test_a_staff_administrator_has_the_same_home_order(client, world, roles):
    user = User.objects.create_user(username="pd-admin", password="admin-pass-123456", is_staff=True)
    user.groups.add(Group.objects.get(name=ADMIN))
    client.force_login(user)
    html = text(_home(client))
    assert html.index('id="home-prp-title"') < html.index('class="home-fold"')
    assert '<details class="home-fold">' in html  # "Needs a decision" waits in the fold (named in OPERATIONS)
    assert home_service.HOME_PDS == 10


def test_home_shows_at_most_the_first_screen_s_rows_and_says_so(client_viewer, world, monkeypatch):
    """Every row drawn: the count alone. Fewer rows than the count: the hint says they are the first ones."""
    hint = re.compile(r'id="home-pds-title">[^<]+</h3>\s*<span class="home-prp__hint">([^<]*)</span>')
    assert hint.findall(_words(text(_home(client_viewer)))) == ["3 programme documents"]
    monkeypatch.setattr(home_service, "HOME_PDS", 2)
    home = _home(client_viewer)
    figures = _figures(home)
    assert len(figures["pd_rows"]) == 2 and figures["reporting"]["pds"]["count"] == 3
    assert hint.findall(_words(text(home))) == [
        "The first 2 of 3 programme documents, the most overdue first"
    ]
