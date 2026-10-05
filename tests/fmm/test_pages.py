"""The Monitoring insights page (``/fmm/``): its shell, filters, key figures and tabs.

On ``fm_world`` built on 5 October 2026: 8 visits ending in 2026 (6 reported, 1 in progress, 1
cancelled), 12 finding rows (8 rated, 4 not monitored), 6 scored visits averaging 64.4%, one red visit
(the one known by its reference only, urgency 72) and three amber ones.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import replace

import pytest
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import SECTION_EDITOR, ensure_groups
from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.fmm import refresh
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import RuleSetVersion, Visit, VisitReview
from neurodb.fmm.scope import Scope, link
from neurodb.reports.overview import governorate_key

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
REFERENCE_KEY = fm.visit_key(None, "FM/2026/9", 0)
ALL = {"1722", "1723", "1724", "1725", "1726", "1727", "1728", REFERENCE_KEY}
FORBIDDEN = re.compile(r"\b(item|items|agent|detector|receipt|LLM)\b", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    """ "This year" and the rolling periods are read on 5 October 2026."""
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


def _keys(params: dict, user=None) -> set[str]:
    return set(Scope.from_params(params, user).visits().values_list("key", flat=True))


def visible(html: str) -> str:
    """The text a user reads: no scripts, styles, icons or comments, and no synced eTools text."""
    html = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    html = re.sub(r"<(\w+)[^>]*\bdata-synced\b[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _section_editor(section, username="editor") -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="x-pass-123456", section=section
    )
    user.groups.add(ensure_groups()[SECTION_EDITOR])
    return user


# ------------------------------------------------------------------------------------------ the shell
def test_the_page_opens_for_a_viewer_with_its_key_figures(built, client_viewer):
    response = client_viewer.get(PAGE)
    html = response.content.decode()
    assert response.status_code == 200
    assert "<title>Monitoring insights ·" in html
    text = " ".join(visible(html).split())
    assert "Monitoring visits 8 6 reported · 1 in progress · 0 planned · 1 cancelled" in text
    assert "Monitored entities 12 8 rated · 4 not monitored" in text
    assert "Average quality score 64.4% on 6 scored visits · rules v1" in text
    assert "High urgency 1 ≥ 70 · 3 amber (40–69)" in text
    assert "Showing 1 Jan – 31 Dec 2026 · Lebanon" in text
    assert "scores computed" in text and "with quality rules v1" in text
    assert "How scores work" in html and "NeuroDB has no field office staff list" in html
    assert 'class="kpi kpi--off_track"' in html  # the high urgency tile is red while any visit is
    assert "The AI brief and chat appear here once switched on" in html


@pytest.mark.parametrize("tab", ["insights", "visits"])
def test_each_tab_is_an_htmx_partial(built, client_viewer, tab):
    response = client_viewer.get(PAGE, {"tab": tab, "year": "2026"}, HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in html and 'id="fmm-filters"' not in html
    assert f'name="tab" value="{tab}" form="fmm-filters"' in html  # the filter bar keeps the tab
    assert ("Monitoring visits — detail and flags" in html) == (tab == "visits")


def test_the_tab_bar_lists_the_tabs_of_this_stage_with_the_scope(built, client_viewer):
    html = client_viewer.get(PAGE, {"year": "2026", "rating": "off_track"}).content.decode()
    nav = html.split('class="segmented segmented--scroll fmm-tabs"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r">(\w+)</a>", nav) == ["Insights", "Visits"]
    assert "year=2026&amp;section=&amp;rating=off_track&amp;tab=visits" in nav
    assert 'hx-target="#fmm-results"' in nav and 'hx-push-url="true"' in nav


def test_an_empty_database_shows_the_empty_states(db, client_viewer):
    html = client_viewer.get(PAGE).content.decode()
    assert "No eTools field monitoring synced yet" in html
    assert "Monitoring visits" not in html
    dm.MonitoringFinding.objects.create(datamart_id=1, monitoring_activity="FM-1")
    html = client_viewer.get(PAGE).content.decode()
    assert "The visits are being prepared" in html


def test_a_filter_with_no_visit_offers_to_clear_it(built, client_viewer):
    html = client_viewer.get(PAGE, {"tab": "visits", "year": "2025"}).content.decode()
    assert "No visits in this filter — widen the period" in html
    assert "Clear filters" in html and "Try this year" in html


def test_a_signed_out_visitor_is_sent_to_sign_in(db, client):
    response = client.get(PAGE)
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_the_menu_item_equals_the_page_title(built, client_viewer):
    html = client_viewer.get(reverse("reports:overview")).content.decode()
    sidebar = html.split('id="sidebar"', 1)[1]
    item = re.search(r'<a class="nav-item-link[^"]*" href="/fmm/"[^>]*>(.*?)</a>', sidebar, re.S)
    assert item is not None
    label = re.search(r"<span>([^<]+)</span>", item.group(1)).group(1)
    assert label == "Monitoring insights" and '<span class="nav-badge">AI</span>' in item.group(1)
    assert 'href="#i-ruler"' in item.group(1)
    page = client_viewer.get(PAGE).content.decode()
    assert re.search(r"<title>(.*?) · ", page).group(1) == label
    # the item comes right after Field monitoring
    assert sidebar.index('href="/field-monitoring/"') < sidebar.index('href="/fmm/"')


def test_switched_off_every_view_is_404_and_the_menu_item_hidden(built, client_viewer, settings):
    settings.FMM_ENABLED = False
    for url in (
        PAGE,
        reverse("fmm:visits"),
        reverse("fmm:visit", args=["1722"]),
        reverse("fmm:lookup") + "?id=1722",
    ):
        assert client_viewer.get(url).status_code == 404, url
    assert client_viewer.post(reverse("fmm:review", args=["1722"])).status_code == 404
    assert 'href="/fmm/"' not in client_viewer.get(reverse("reports:overview")).content.decode()


def test_the_year_menu_keeps_the_page_and_its_filters(built, client_viewer, reporting_year):
    html = client_viewer.get(PAGE, {"rating": "off_track", "preset": "last_90"}).content.decode()
    assert "/fmm/?rating=off_track&amp;preset=last_90&amp;year=2026" in html
    # ... and the year wins over a preset
    assert _keys({"preset": "last_90", "year": "2026"}) == ALL


# ------------------------------------------------------------------------------------------ filters
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ALL),
        ({"preset": "this_year"}, ALL),
        ({"preset": "last_year"}, set()),
        ({"year": "2026"}, ALL),
        ({"period": "2026"}, ALL),
        ({"year": "2025"}, set()),
        ({"preset": "this_quarter"}, set()),
        ({"preset": "last_quarter"}, {"1724", "1726", "1727"}),
        ({"preset": "last_30"}, {"1724"}),
        ({"preset": "last_90"}, {"1724", "1726", "1727"}),
        ({"preset": "custom", "from": "2026-05-01", "to": "2026-06-30"}, {"1722", "1723"}),
        ({"preset": "custom", "from": "2026-06-30", "to": "2026-05-01"}, {"1722", "1723"}),  # swapped
        ({"section": "Education"}, {"1724", "1726", REFERENCE_KEY}),
        ({"section": "none"}, {"1722", "1723", "1725", "1727", "1728"}),
        ({"section": ""}, ALL),
        ({"governorate": "bekaa"}, {"1722", "1723", "1725", "1727", "1728"}),
        ({"governorate": "Beqaa"}, {"1722", "1723", "1725", "1727", "1728"}),
        ({"governorate": "North"}, {"1724", "1726", REFERENCE_KEY}),
        ({"governorate": "none"}, set()),
        ({"office": "Tripoli"}, {"1724", "1726", REFERENCE_KEY}),
        ({"office": "Zahle"}, {"1722"}),
        ({"office": "none"}, {"1723", "1725", "1727", "1728"}),
        ({"entity_type": "cp_output"}, {"1722"}),
        ({"entity_type": "partner"}, {"1722", "1723"}),
        ({"rating": "off_track"}, {"1722", REFERENCE_KEY}),
        ({"rating": "not_monitored"}, {"1723", "1724", "1725"}),
        ({"status": "in_progress"}, {"1724"}),
        ({"status": "cancelled"}, {"1725"}),
        ({"status": "planned"}, set()),
        ({"programmatic": "1"}, {"1722", "1723", "1726", "1727"}),
        ({"q": "Zahle"}, {"1722", "1725", "1728"}),
        ({"q": "fm/2026/9"}, {REFERENCE_KEY}),
        ({"month": "2026-05"}, {"1722"}),
        ({"hact_q1": "constrained"}, {"1723", "1727"}),
        ({"hact_q1": "none"}, {"1724", "1725", "1728"}),
        ({"bucket": "40-60"}, {"1723", "1728"}),
        ({"bucket": "60-80"}, {"1722", "1726", "1727", REFERENCE_KEY}),
        ({"bucket": "none"}, {"1724", "1725"}),
        ({"flag": "R1"}, {"1723", "1727", "1728", REFERENCE_KEY}),
        ({"flags": "1"}, {"1727", "1728", REFERENCE_KEY}),
        ({"flags": "3+"}, {"1723"}),
        ({"urgency": "red"}, {REFERENCE_KEY}),
        ({"urgency": "amber"}, {"1722", "1723", "1727"}),
        ({"rule": "R3", "rule_state": "fail"}, {"1722"}),
        ({"issue": "R3:q1_missing"}, {"1722"}),
        ({"month": "May 2026"}, ALL),  # a drawn label is not a drill value: ignored
        ({"bucket": "80–100"}, ALL),
    ],
)
def test_each_filter_keeps_the_expected_visits(built, params, expected):
    assert _keys(params) == expected


def test_the_partner_and_pd_filters(built, fm_world):
    amel, mercy = fm_world.partners["amel"], fm_world.partners["mercy"]
    assert _keys({"partner": str(amel.pk)}) == {"1722", "1725", "1727"}
    assert _keys({"partner": [str(amel.pk), str(mercy.pk)]}) == ALL
    assert _keys({"pd": str(fm_world.pds["education"].pk)}) == {"1724", "1726", REFERENCE_KEY}


def test_an_entity_must_match_the_partner_and_the_entity_type_together(built, fm_world):
    amel, mercy = fm_world.partners["amel"], fm_world.partners["mercy"]
    assert _keys({"partner": str(amel.pk), "entity_type": "partner"}) == {"1722"}
    assert _keys({"partner": str(mercy.pk), "entity_type": "cp_output"}) == set()
    scope = Scope.from_params({"partner": str(mercy.pk), "entity_type": "partner", "section": ""})
    assert scope.entities().count() == 1  # the partner row of 1723


def test_a_drill_on_the_latest_review(built, admin_user):
    VisitReview.objects.create(visit_key="1722", status="reviewed", reviewed_by=admin_user)
    VisitReview.objects.create(visit_key="1722", status="follow_up", reviewed_by=admin_user)
    VisitReview.objects.create(visit_key="1723", status="reviewed", reviewed_by=admin_user)
    assert _keys({"review": "follow_up"}) == {"1722"}  # its latest review
    assert _keys({"review": "reviewed"}) == {"1723"}
    assert _keys({"review": "none"}) == ALL - {"1722", "1723"}


def test_entity_filters_count_the_matching_rows_only(built, client_viewer):
    html = client_viewer.get(PAGE, {"entity_type": "partner"}).content.decode()
    text = " ".join(visible(html).split())
    assert "Monitoring visits 2" in text and "Monitored entities 2 1 rated · 1 not monitored" in text
    assert "Monitored entities counts only the finding rows that match" in text


def test_a_pd_from_a_link_shows_as_a_removable_chip(built, client_viewer, fm_world):
    pd = fm_world.pds["education"]
    html = client_viewer.get(PAGE, {"pd": pd.pk, "section": ""}).content.decode()
    assert f"Programme document {pd.number}" in html
    assert f'name="pd" value="{pd.pk}"' in html  # kept by the filter bar
    chip = re.search(r'class="chip__remove" href="([^"]+)"', html).group(1)
    assert "pd=" not in chip and "section=" in chip


def test_drill_values_show_as_removable_chips_and_go_with_the_filter_bar(built, client_viewer):
    html = client_viewer.get(PAGE, {"urgency": "amber", "section": ""}).content.decode()
    assert "Urgency: amber" in html
    assert '<input type="hidden" name="urgency" value="amber" form="fmm-filters">' in html
    assert "Monitoring visits 3" in " ".join(visible(html).split())


# ------------------------------------------------------------------------------------------ sections
def test_a_user_with_a_section_gets_it_only_on_a_bare_visit(built, client, fm_world):
    client.force_login(_section_editor(fm_world.section))
    text = " ".join(visible(client.get(PAGE).content.decode()).split())
    assert "Monitoring visits 3" in text and "Your section: Education · show all" in text
    text = " ".join(visible(client.get(PAGE, {"section": ""}).content.decode()).split())
    assert "Monitoring visits 8" in text and "Your section" not in text
    text = " ".join(visible(client.get(PAGE, {"year": "2026"}).content.decode()).split())
    assert "Monitoring visits 3" in text  # no section key at all: the default again


def test_the_scope_query_always_carries_the_section_and_reads_back_the_same(built, fm_world):
    user = _section_editor(fm_world.section)
    bare = Scope.from_params({}, user)
    assert bare.sections == ("Education",) and bare.default_section
    everything = Scope.from_params({"section": ""}, user)
    assert everything.sections == () and "section=" in everything.query
    for scope in (
        bare,
        everything,
        Scope.from_params({"year": "2026", "urgency": "red", "section": ""}, user),
    ):
        from django.http import QueryDict

        again = Scope.from_params(QueryDict(scope.query), user)
        assert again.hash() == scope.hash(), scope.query
    assert link(partner=5, year=2026) == "/fmm/?partner=5&year=2026&section="
    assert link(section="Education") == "/fmm/?section=Education"


def test_a_governorate_name_equals_its_key(built):
    by_key = Scope.from_params({"governorate": "bekaa"})
    for name in ("Bekaa", "Beqaa", "BEKAA"):
        assert Scope.from_params({"governorate": name}).hash() == by_key.hash()
    assert Scope.from_params({"governorate": "Baalbek - El Hermel"}).governorate == governorate_key(
        "Baalbek-Hermel"
    )
    assert Scope.from_params({"governorate": "none"}).governorate == "none"


def test_canonical_scopes_use_preset_tokens_and_previous_periods(built):
    this_year = Scope.from_params({})
    assert this_year.canonical()["preset"] == "this_year" and "from" not in this_year.canonical()
    custom = Scope.from_params({"preset": "custom", "from": "2026-03-01", "to": "2026-03-31"})
    assert custom.canonical()["from"] == "2026-03-01"
    previous = this_year.previous()
    assert (previous.start, previous.end) == (datetime.date(2025, 1, 1), datetime.date(2025, 10, 5))
    assert (custom.previous().start, custom.previous().end) == (
        datetime.date(2026, 1, 29),
        datetime.date(2026, 2, 28),
    )
    quarter = Scope.from_params({"preset": "this_quarter"}).previous()
    assert (quarter.start, quarter.end) == (datetime.date(2026, 7, 1), datetime.date(2026, 9, 30))


def test_narrowing_never_widens_the_scope(built, fm_world):
    bound = Scope.from_params({"governorate": "north", "section": ""})
    assert set(bound.narrow(governorate="Bekaa").visits().values_list("key", flat=True)) == set()
    assert bound.narrow(governorate="north").visits().count() == 3
    rated = Scope.from_params({"rating": "off_track", "section": ""})
    assert rated.narrow(ratings=["on_track"]).visits().count() == 0
    assert rated.narrow(ratings=["off_track", "on_track"]).ratings == ("off_track",)
    narrowed = Scope.from_params({"section": ""}).narrow(start="2026-05-01", end="2026-06-30")
    assert set(narrowed.visits().values_list("key", flat=True)) == {"1722", "1723"}
    assert replace(narrowed, empty=False).narrow(end="2027-01-01").end == datetime.date(2026, 6, 30)


# ------------------------------------------------------------------------------------------ wording
def test_no_forbidden_word_none_or_nan_on_the_page(built, client_viewer):
    pages = [
        client_viewer.get(PAGE).content.decode(),
        client_viewer.get(PAGE, {"tab": "visits"}).content.decode(),
        client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode(),
        client_viewer.get(reverse("fmm:visit", args=["1723"])).content.decode(),
        client_viewer.get(reverse("fmm:lookup"), {"id": "1799"}).content.decode(),
    ]
    for html in pages:
        main = html.split('<main id="main"', 1)[-1]
        text = visible(main)
        assert not FORBIDDEN.search(text), FORBIDDEN.search(text)
        assert not re.search(r"\b(None|nan)\b", text)


def test_a_reference_date_next_to_every_rating_and_status(built, client_viewer):
    html = client_viewer.get(PAGE, {"tab": "visits"}).content.decode()
    body = html.split('id="fmm-visits-table"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr data-href.*?</tr>", body, re.S)
    assert len(rows) == 8
    for row in rows:
        rating = re.search(r'data-status="[a-z_]+".*?</span>(.*?)</td>', row, re.S)
        assert rating and re.search(r"(rated|ends) \d{1,2} \w{3} 2026", rating.group(1)), row
    page = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert re.search(
        r'data-status="reported".*?</span>\s*<span[^>]*>as of (eTools sync \d|the last eTools sync)',
        page,
        re.S,
    )
    entities = page.split("Monitored entities</h3>", 1)[1].split("</table>", 1)[0]
    for row in re.findall(r"<tr>.*?</tr>", entities.split("<tbody>", 1)[1], re.S):
        rating = row.split("</td>", 2)[1]
        assert re.search(r"rated 12 May 2026", rating), row


def test_how_scores_work_lists_the_rules_and_links_administrators_to_their_settings(
    built, client_viewer, client, admin_user
):
    html = client_viewer.get(PAGE).content.decode()
    how = html.split('id="fmm-how"', 1)[1].split('<div class="modal fade"', 1)[0]
    assert "R1 Completeness" in how and "R6 Rating quality" in how and "a flag only" in how
    assert "/fmm/rulesetting/" not in how  # a viewer gets no admin link
    client.force_login(admin_user)
    how = client.get(PAGE).content.decode().split('id="fmm-how"', 1)[1]
    assert reverse("admin:fmm_rulesetting_changelist") in how
    assert reverse("admin:fmm_fieldmapping_changelist") in how


def test_the_reference_line_says_when_scores_are_being_recomputed(built, client_viewer):
    assert "recomputing" not in client_viewer.get(PAGE).content.decode()
    RuleSetVersion.objects.create(number=2, snapshot={}, note="test", created_by_name="test")
    html = client_viewer.get(PAGE).content.decode()
    assert "recomputing with rules v2" in html


def test_the_reference_line_shows_a_failed_refresh(built, client_viewer, monkeypatch):
    from neurodb.fmm import build

    monkeypatch.setattr(build, "build_visits", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    refresh.run_safely(triggered_by="test", today=TODAY)
    html = client_viewer.get(PAGE).content.decode()
    assert "The last refresh of Monitoring insights failed" in html
    assert "Monitoring visits" in html  # the previous visits stay


def test_not_available_texts_without_the_checklist_answers(fm_world, client_viewer):
    dm.DatamartDocument.objects.filter(dataset__in=("fm_questions", "fm_options")).delete()
    refresh.run(triggered_by="test", today=TODAY)
    html = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert "Not available — the answers of the checklist records were not found" in html
    assert "Not available — the checklist answers were not found in the eTools data" in html


# ------------------------------------------------------------------------------------------ speed
def test_each_tab_and_the_page_stay_within_their_query_budget(
    built, client_viewer, django_assert_max_num_queries
):
    from django.core.cache import cache

    for tab in ("insights", "visits"):
        cache.clear()
        with django_assert_max_num_queries(20):
            client_viewer.get(PAGE, {"tab": tab}, HTTP_HX_REQUEST="true")
        cache.clear()
        with django_assert_max_num_queries(30):
            client_viewer.get(PAGE, {"tab": tab})


def test_the_data_note_counts_rows_without_a_reference(built, client_viewer):
    dm.MonitoringFinding.objects.create(
        datamart_id=999, end_date=datetime.date(2026, 6, 1), overall_finding_rating="On Track"
    )
    dm.MonitoringFinding.objects.create(datamart_id=998, overall_finding_rating="On Track")  # no date
    refresh.run(triggered_by="test", today=TODAY)
    text = " ".join(visible(client_viewer.get(PAGE).content.decode()).split())
    assert "1 finding row has no activity reference and counts as its own visit here" in text
    assert "1 visit has no end date and is left out of the period" in text
    assert Visit.objects.filter(end_date=None).count() == 1


def test_a_partner_id_too_large_for_an_id_is_ignored_not_an_error(built, client_viewer, fm_world):
    for params in ({"partner": "999999999999"}, {"partner": "2147483648", "tab": "visits"}):
        assert client_viewer.get(PAGE, params).status_code == 200
        assert client_viewer.get(reverse("fmm:visits"), {**params, "export": "csv"}).status_code == 200
    assert Scope.from_params({"partner": ["999999999999", "7"]}).partners == (7,)
    amel = fm_world.partners["amel"]
    bound = Scope.from_params({"partner": str(amel.pk), "section": ""})
    assert bound.narrow(partners=["999999999999"]).visits().count() == 0  # outside the bound scope


def test_a_review_drill_counts_a_new_review_at_once(built, client_viewer, admin_user, settings):
    from django.core.cache import cache

    from neurodb.fmm import metrics

    settings.DEBUG = False
    cache.clear()
    scope = Scope.from_params({"review": "follow_up", "section": ""})
    assert metrics.kpis(scope)["visits"] == 0
    VisitReview.objects.create(visit_key="1722", status="follow_up", reviewed_by=admin_user)
    assert metrics.kpis(scope)["visits"] == 1
    html = client_viewer.get(PAGE, {"review": "follow_up", "section": "", "tab": "visits"}).content.decode()
    assert "Showing 1 visit ·" in html


def test_one_scored_visit_is_written_in_the_singular(built, client_viewer):
    Visit.objects.exclude(key="1722").update(quality_score=None)
    html = client_viewer.get(PAGE, {"section": ""}).content.decode()
    assert "on 1 scored visit · rules v1" in " ".join(visible(html).split())
