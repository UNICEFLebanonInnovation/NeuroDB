"""The country overview in parts (SPEC7 §3.3-3.4 for the overview; STAGE0 D1, D2, D9).

The overview answers its frame at once (purpose, filter bar, subtitle, freshness strip) with a placeholder per
data block: ``kpis`` and ``figures`` (one overview build), ``for-you`` (the NeuroDB Watch card, from its own
address), ``review`` (the daily review card; its folded findings are ``review-more``, inside it) and ``news``
(What's new and the forecast risk card). These tests check the frame without any figure work, each part, the
part queries, and that the page put together from its parts is the page drawn at once (``_eager=1``).

On ``overview_fixture``: one programme document (Amel, Child Protection) with 500 children reached in 2026, plus
the ActivityInfo hierarchy; a daily review is run on it where a test needs the card.
"""

from __future__ import annotations

import dataclasses
import datetime
import gettext
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.core.cache import caches
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape

from neurodb.accounts.models import User
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.donors.models import DonorAccount
from neurodb.geo.models import Location
from neurodb.graph import news
from neurodb.indicators.models import ReportingYear
from neurodb.insights import services as insight_services
from neurodb.reports import overview as overview_service
from neurodb.reports import views
from neurodb.review import checks
from neurodb.review import services as review_services
from neurodb.review.models import DailyReview, ReviewFinding
from neurodb.watch import page as watch_page
from tests.reports.overview_fixture import TODAY, make_overview_data
from tests.reports.test_headline import _second
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

PAGE = "/"  # reports:overview, the site root once signed in
PARTS = ("kpis", "for-you", "figures", "review", "news")  # the frame's placeholders, in page order
OWN_PARTS = ("kpis", "figures", "review", "review-more", "news")  # the parts the view itself answers
FIGURES_JSON = "overview-figures-data"
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame warm (its filter
# sections, freshness strip and navigation stored, as after the first visit of a page) is 11: the request's floor
# (session, user, donor check, user role) plus the year, the cache stamp (last sync run, flags), the own section,
# the governorates (read at each visit: staff edit them in the admin) and whether a daily review exists. Cold it
# also reads the filter sections (1), the freshness strip (4) and the navigation (4). Each part is measured with
# nothing stored: kpis and figures compute the whole build.
SHELL_QUERIES_WARM = 12
SHELL_QUERIES_COLD = 22
PART_QUERIES = {"kpis": 39, "figures": 39, "review": 9, "review-more": 9, "news": 8}


@pytest.fixture
def world(hierarchy, reporting_year, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


@pytest.fixture
def reviewed(world, monkeypatch):
    """The world with a daily review: the card's place is kept in the frame. The card shows one finding and
    folds the others (``review-more``), as the real card does past its first findings."""
    review_services.run(date=TODAY, today=TODAY)
    monkeypatch.setattr(review_services, "PAGE_FINDINGS", 1)
    return world


@pytest.fixture
def own_section(viewer, section):
    """The viewer works in Child Protection: a first load shows that section."""
    viewer.section = section
    viewer.save()
    return viewer


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the overview raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the shell computed figures")

    monkeypatch.setattr(overview_service, "build", refuse)
    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    monkeypatch.setattr(views, "_overview_chart_data", refuse)
    monkeypatch.setattr(views, "_whats_new", refuse)
    monkeypatch.setattr(views, "_forecast_risk", refuse)
    monkeypatch.setattr(review_services, "for_page", refuse)
    monkeypatch.setattr(news, "recent", refuse)
    monkeypatch.setattr(insight_services, "at_risk", refuse)
    monkeypatch.setattr(watch_page, "card", refuse)


def _json_blocks(html: str) -> dict[str, object]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a chart JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _without_for_you(assembled) -> str:
    """The assembled page without the NeuroDB Watch card, which eager mode leaves to the browser (a ``url=``
    part): the comparison with the eager page is about the overview's own parts."""
    card = assembled.parts.get("for-you")
    return assembled.html.replace(text(card), "", 1) if card is not None else assembled.html


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, reviewed, monkeypatch):
    from django.core.cache import caches

    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Country overview" in html and 'class="page-purpose"' in html
    assert 'aria-label="Overview filters"' in html and 'name="governorate"' in html  # the filter bar
    assert "Reporting year 2026 · every section · all governorates" in html  # the subtitle
    assert "Data freshness" in html and "Daily AI review" in html  # the freshness strip
    assert "ActivityInfo databases of the year" in html
    assert 'id="daily-review"' in html  # the review card's anchor stays in the frame
    assert list(placeholders(html)) == list(PARTS)
    for data_id in (FIGURES_JSON, "overview-chart-data"):  # no figures, so no chart data
        assert f'id="{data_id}"' not in html
    for figure in ("Children reached", 'id="gov-title"', 'id="review-title"', 'id="whats-new-title"'):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_filter(client_viewer, reviewed):
    html = text(client_viewer.get(PAGE))
    found = placeholder_attributes(html)
    assert list(found) == list(PARTS)
    stamp = overview_service.fingerprint()  # the figures the frame was drawn with (test_the_figures_part_...)
    for name in ("kpis", "figures", "review", "news"):
        url = found[name]["hx-get"]
        assert urlsplit(url).path == PAGE
        # every section written out (never a first load again), the year always
        assert _query(url) == {"section": [""], "year": ["2026"], "as_of": [stamp], "_part": [name]}, url
    assert found["for-you"]["hx-get"] == reverse("watch:card") and found["for-you"]["hx-swap"] == "outerHTML"
    assert (found["kpis"]["data-part-priority"], found["kpis"]["data-part-group"]) == ("high", "overview")
    assert (found["figures"]["data-part-priority"], found["figures"]["data-part-group"]) == (
        "normal",
        "overview",
    )
    assert found["for-you"]["data-part-priority"] == "high" and found["news"]["data-part-priority"] == "low"
    assert "data-part-group" not in found["review"] and "data-part-lazy" not in found["review"]
    assert "gov-title" in found["figures"]["data-part-anchors"].split()  # its blocks' ids stay anchors
    # the room of the four blocks, on a desktop, a tablet or narrow window (stacked beside the sidebar) and a
    # phone, as measured on the x20 parity world
    assert found["figures"]["class"] == "part part--charts"
    assert found["figures"]["style"] == "--part-h: 4650px; --part-h-sm: 8100px; --part-h-md: 6200px"
    assert found["kpis"]["style"] == "--part-h: 18.5rem; --part-h-sm: 54rem; --part-h-md: 34rem"
    assert found["review"]["style"] == "--part-h: 1850px; --part-h-sm: 4900px; --part-h-md: 2500px"
    assert found["news"]["style"] == "--part-h: 16rem; --part-h-sm: 32rem; --part-h-md: 23rem"
    kpis = html[html.index('id="part-kpis"') : html.index('id="part-for-you"')]
    assert kpis.count("skeleton__block--tile") == 5  # the five key figure tiles
    # the governorate and the review tab travel too
    html = text(
        client_viewer.get(PAGE, {"section": "Child Protection", "governorate": "Akkar", "review": "week"})
    )
    assert _query(placeholders(html)["review"]) == {
        "section": ["Child Protection"],
        "year": ["2026"],
        "governorate": ["Akkar"],
        "review": ["week"],
        "as_of": [stamp],
        "_part": ["review"],
    }


@pytest.mark.shell
def test_a_first_load_writes_the_own_section_and_every_section_writes_section(
    client_viewer, own_section, world, monkeypatch
):
    seen = []
    real_build = overview_service.build

    def build(scope, **kwargs):
        seen.append(list(scope.sections))
        return real_build(scope, **kwargs)

    monkeypatch.setattr(overview_service, "build", build)
    first = text(client_viewer.get(PAGE))
    assert "Your section, Child Protection, by default" in first
    for name in ("kpis", "figures", "news"):
        assert _query(placeholders(first)[name])["section"] == ["Child Protection"]
    # the part request is never a first load: it computes what the frame wrote, the own section
    get_part(client_viewer, PAGE, "kpis")
    assert seen == [["Child Protection"]]
    every = text(client_viewer.get(PAGE + "?section="))
    assert "by default" not in every
    for name in ("kpis", "figures", "news"):
        assert _query(placeholders(every)[name])["section"] == [""]
    get_part(client_viewer, PAGE + "?section=", "kpis")  # "every section", not the own section again
    assert seen[-1] == []


@pytest.mark.shell
def test_without_a_reporting_year_there_is_no_part(client_viewer, world, reporting_year):
    reporting_year.delete()
    html = text(client_viewer.get(PAGE))
    assert "No reporting year is configured" in html and placeholders(html) == {}
    response = client_viewer.get(PAGE + "?_part=kpis", **HX)
    assert response.status_code == 404  # no part to ask for


@pytest.mark.shell
def test_without_a_daily_review_the_frame_keeps_no_room_for_it(client_viewer, world):
    """No daily review has ever succeeded: no ``#daily-review`` and no review placeholder, so no empty block
    of the card's height waits for a part that brings nothing."""
    html = text(client_viewer.get(PAGE))
    assert list(placeholders(html)) == ["kpis", "for-you", "figures", "news"]
    assert 'id="daily-review"' not in html


def _governorate_choices(client) -> list[str]:
    html = text(raw_get(client, PAGE + "?section="))
    select = re.search(r'<select[^>]*name="governorate".*?</select>', html, re.S)
    assert select, "the filter bar has its governorate choice"
    return re.findall(r"<option[^>]*>([^<]*)</option>", select.group(0))


def test_a_governorate_edited_in_the_admin_shows_in_the_filter_bar_at_once(client, admin_user, world):
    """The gazetteer is edited by hand (Admin → Locations), outside any sync: the filter bar's governorates
    must not wait for a stored value to expire."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    assert "Akkar" in _governorate_choices(client)  # the filter choices are stored now
    akkar = Location.objects.get(name="Akkar")
    change = reverse("admin:locations_location_change", args=[akkar.pk])
    response = client.post(
        change,
        {
            "name": "Akkar North",
            "type": akkar.type_id,
            "p_code": akkar.p_code,
            "cas_code": "",
            "is_active": "on",
        },
    )
    assert response.status_code == 302, text(response)[:2000]
    assert "Akkar North" in _governorate_choices(client) and "Akkar" not in _governorate_choices(client)

    akkar.refresh_from_db()
    response = client.post(
        change, {"name": akkar.name, "type": akkar.type_id, "p_code": akkar.p_code, "cas_code": ""}
    )  # made inactive
    assert response.status_code == 302, text(response)[:2000]
    assert _governorate_choices(client) == ["All", "Beirut"]


def _freshness_of(html: str, label: str) -> str:
    """The state the freshness strip shows for one source (``unknown``, ``stale``, ``fresh``)."""
    item = re.search(rf'<strong>{re.escape(label)}</strong><span[^>]*\sdata-status="([a-z]+)"', html)
    assert item, f"{label} is not in the freshness strip"
    return item.group(1)


def test_a_finished_sync_shows_in_the_frame_at_once(client_viewer, world):
    """The freshness strip and the filter bar's sections are stored for the frame (two and ten minutes),
    under keys that a finished sync changes: a sync shows at once, in both."""
    before = text(raw_get(client_viewer, PAGE + "?section="))  # both stored now
    assert _freshness_of(before, "eTools Datamart sync") == "unknown"  # not yet synced
    assert 'value="Health"' not in before
    indicator = dm.PDIndicator.objects.first()
    indicator.pk, indicator.datamart_id, indicator.section_name = None, 987654, "Health"
    indicator.save()  # a section the sync brings
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="all", status=SyncRun.Status.RUNNING)
    run.finish(SyncRun.Status.SUCCEEDED)
    after = text(raw_get(client_viewer, PAGE + "?section="))
    assert _freshness_of(after, "eTools Datamart sync") == "fresh"
    assert 'value="Health"' in after


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(
    client_viewer, own_section, reviewed, django_assert_max_num_queries
):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and "kpis" in placeholders(text(response))
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and "kpis" in placeholders(text(response))


def test_the_frame_reads_no_watch_table(client_viewer, reviewed):
    with CaptureQueriesContext(connection) as queries:
        response = raw_get(client_viewer, PAGE)
    assert response.status_code == 200
    assert not [q["sql"] for q in queries.captured_queries if '"watch_' in q["sql"]]


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("name", OWN_PARTS)
def test_each_part_answers_its_wrapper(client_viewer, reviewed, name):
    response = get_part(client_viewer, PAGE, name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "kpis": "Children reached",
        "figures": "Impact on children",
        "review": 'id="review-title"',
        "review-more": "review-finding",
        "news": 'id="whats-new-title"',
    }[name]
    assert expected in html
    assert (f'id="{FIGURES_JSON}"' in html) == (name == "figures")  # the one chart JSON, in its own part
    # nothing that would run once swapped in, in the part or in any partial it includes (SPEC7 §1.3 rule 3)
    assert executable(html) == []


@pytest.mark.parametrize("name", OWN_PARTS)
def test_each_part_stays_within_its_query_ceiling(
    client_viewer, reviewed, name, django_assert_max_num_queries
):
    from django.core.cache import caches

    address = f"{PAGE}?section=&year=2026&_part={name}"
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)


@pytest.mark.parametrize("name", OWN_PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    response = client.get(f"{PAGE}?section=&year=2026&_part={name}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_of_the_site_root_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?section=&year=2026&_part=kpis", **HX)
    assert response.status_code == 200 and "in one place." not in text(response)  # never the landing page
    assert response["HX-Redirect"] == reverse("account_login") + "?next=/"


def test_no_link_inside_a_part_carries_part(client_viewer, own_section, reviewed):
    assembled = assemble(client_viewer, PAGE)
    assert set(OWN_PARTS) <= set(assembled.parts)
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)  # (a placeholder's noscript: _eager)
    assert "_part=" not in "".join(LINK.findall(assembled.html))


def test_the_review_more_part_holds_only_the_folded_findings(client_viewer, reviewed):
    page = review_services.for_page("today", [])
    shown = escape(page["findings"][0].title)
    folded = [escape(f.title) for f in page["more_findings"]]
    assert len(page["findings"]) == 1 and folded and shown not in folded
    item = '<li class="review-finding">'
    card = text(get_part(client_viewer, PAGE, "review"))
    # the card lists its first finding only (its summary may name the others) ...
    assert card.count(item) == 1 and shown in card.split(item, 1)[1].split("</li>", 1)[0]
    assert f"Show {len(folded)} more finding" in card
    inner = placeholder_attributes(card)
    assert list(inner) == ["review-more"] and inner["review-more"]["data-part-lazy"] == "strict"
    assert _query(inner["review-more"]["hx-get"])["_part"] == ["review-more"]
    more = text(get_part(client_viewer, PAGE, "review-more"))
    # ... and the folded ones come in their own part, alone
    assert more.count(item) == len(folded) and all(title in more for title in folded)
    assert shown not in more and 'id="review-title"' not in more and "review-summary" not in more


@pytest.mark.parametrize("tab", ["", "yesterday", "week"])
def test_the_folded_findings_stay_those_of_their_card(client_viewer, reviewed, tab, monkeypatch):
    """The card and its folded findings are two requests, maybe hours apart (``review-more`` loads when
    *Show more* is opened, or for print): a review that succeeds in between never puts its findings under the
    card drawn before it, whatever the card's tab. A newer day's review (the next morning's job) leaves the
    card's findings as they were; a run of the card's own day (the admin's *Run the daily review now*, a
    second run of the morning's job) replaces its review, and the part then asks the reader to reload."""
    if tab == "yesterday":  # a review the day before, so that the tab has a card with folded findings
        review_services.run(date=TODAY - datetime.timedelta(days=1), today=TODAY - datetime.timedelta(days=1))
    page = review_services.for_page(tab or "today", [])
    folded = [escape(f.title) for f in page["more_findings"]]
    assert folded
    card = text(get_part(client_viewer, PAGE + (f"?section=&review={tab}" if tab else ""), "review"))
    assert f"Show {len(folded)} more finding" in card
    address = placeholder_attributes(card)["review-more"]["hx-get"]
    before = text(raw_get(client_viewer, address, **HX))
    assert before.count('<li class="review-finding">') == len(folded) and all(t in before for t in folded)
    # the next review succeeds while the card is open, with findings of its own
    newer = DailyReview.objects.create(
        date=TODAY + datetime.timedelta(days=1),
        status=DailyReview.Status.SUCCEEDED,
        finished_at=timezone.now(),
        checks_run=1,
    )
    for n in range(3):
        ReviewFinding.objects.create(
            review=newer,
            key=f"new_off_track:NEXT-{n}",
            check_id="new_off_track",
            severity=ReviewFinding.Severity.WARNING,
            title=f"Found by the next review, number {n}",
            rank=n,
        )
    assert review_services.for_page(tab or "today", [])["review"] != page["review"]  # the tab moved on
    assert text(raw_get(client_viewer, address, **HX)) == before  # still the card's own findings

    # the card's own day is run again (DailyReview.date is unique: the run replaces that day's review)
    run_all = checks.run_all

    def run_again(ctx):
        drafts, errors = run_all(ctx)
        return [dataclasses.replace(d, title=f"Run again: {d.title}") for d in drafts], errors

    monkeypatch.setattr(checks, "run_all", run_again)
    shown = page["review"]
    again = review_services.run(date=shown.date, today=shown.date, narrate=False)
    assert again.status == DailyReview.Status.SUCCEEDED and again.date == shown.date and again.pk != shown.pk
    after = text(raw_get(client_viewer, address, **HX))
    assert "Run again:" not in after and '<li class="review-finding">' not in after  # never the new run's
    assert "This review has been run again since the page opened. Reload the page" in after


def test_a_warm_overview_draws_kpis_and_figures_in_the_frame(client_viewer, reviewed, monkeypatch):
    get_part(client_viewer, PAGE, "kpis")  # the group leader computes and stores the build

    def refuse(self):
        raise AssertionError("the stored build was computed again")

    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    html = text(raw_get(client_viewer, PAGE))
    assert list(placeholders(html)) == ["for-you", "review", "news"]  # kpis and figures are already in
    for name in ("kpis", "figures"):
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert "Children reached" in html and "Impact on children" in html and f'id="{FIGURES_JSON}"' in html
    # another filter is not stored: its figures are asked for
    other = text(raw_get(client_viewer, PAGE + "?section=Child+Protection"))
    assert list(placeholders(other)) == list(PARTS)


def test_an_edit_shows_at_once_in_the_part(client, admin_user, reviewed):
    """Fresh after edit (STAGE0 D9): the main indicator chosen on the programme document page changes the
    children reached in the key figures, though the build of that filter was stored a moment before."""
    _second(reviewed)  # a second children indicator of the PD: 700, the larger, stands for the PD
    client.force_login(admin_user)
    before = text(get_part(client, PAGE, "kpis"))
    assert ">700</a>" in before
    response = client.post(reverse("reports:pd_main_indicator", args=[reviewed["pd"].pk]), {"key": "100"})
    assert response.status_code == 302
    after = text(get_part(client, PAGE, "kpis"))
    assert ">500</a>" in after and ">700</a>" not in after  # the unique beneficiaries indicator now


def test_the_figures_part_reads_the_build_kpis_stored_though_its_key_moved_between(
    client, admin_user, reviewed, monkeypatch
):
    """The browser asks for ``figures`` once ``kpis`` is in. A run that finishes between the two (any job's: each
    of the 52 runs of an eTools Datamart sync, the morning jobs) or an edit moves the build's key: ``figures``
    still reads the build ``kpis`` stored, found from the fingerprint its address carries (``as_of``), so the
    build is computed once and the page never shows the figures of two moments side by side (up to round 2
    the second part computed a build of its own). The next page shows the figures of the moment, and so does
    the old address once its build is no longer stored."""
    _second(reviewed)  # a second children indicator of the PD: 700, the larger, stands for the PD
    client.force_login(admin_user)
    builds = Counter()
    real_build = overview_service._Builder.build

    def build(self):
        builds["overview"] += 1
        return real_build(self)

    def children(response) -> int:
        assert response.status_code == 200
        return response.context["data"]["impact"]["children_reached"]

    monkeypatch.setattr(overview_service._Builder, "build", build)
    address = placeholders(text(raw_get(client, PAGE)))
    first = client.get(address["kpis"], **HX)
    assert children(first) == 700 and builds["overview"] == 1
    SyncRun.objects.create(job=SyncRun.Job.WATCH, target="all", status=SyncRun.Status.RUNNING).finish(
        SyncRun.Status.SUCCEEDED
    )
    response = client.post(reverse("reports:pd_main_indicator", args=[reviewed["pd"].pk]), {"key": "100"})
    assert response.status_code == 302
    second = client.get(address["figures"], **HX)
    assert children(second) == 700 and builds["overview"] == 1  # the first part's build, read
    assert second.context["data"] == first.context["data"]
    assert children(get_part(client, PAGE, "figures")) == 500 and builds["overview"] == 2  # the next page
    caches["shared"].clear()  # two minutes later: the frame's build is gone
    assert children(client.get(address["figures"], **HX)) == 500 and builds["overview"] == 3


def test_kpis_and_figures_read_one_build_though_the_figures_moved_before_kpis_and_again_between(
    client, admin_user, reviewed, monkeypatch
):
    """The main indicator is chosen on the programme document page between the frame and ``kpis`` (the unique
    beneficiaries one: 500) and chosen again between ``kpis`` and ``figures`` (the larger one: 700): ``kpis``
    computes the build of its moment and keeps it for the frame's ``as_of``, ``figures`` reads it there, so the
    build is computed once and the page shows one moment (up to round 3 ``figures`` computed a second build, and
    showed 700 under tiles of 500). The next page shows the figures of the moment."""
    _second(reviewed)  # a second children indicator of the PD: 700, the larger, stands for the PD
    client.force_login(admin_user)
    builds = Counter()
    real_build = overview_service._Builder.build

    def build(self):
        builds["overview"] += 1
        return real_build(self)

    def children(response) -> int:
        assert response.status_code == 200
        return response.context["data"]["impact"]["children_reached"]

    def choose(key: str) -> None:
        response = client.post(reverse("reports:pd_main_indicator", args=[reviewed["pd"].pk]), {"key": key})
        assert response.status_code == 302

    monkeypatch.setattr(overview_service._Builder, "build", build)
    address = placeholders(text(raw_get(client, PAGE)))
    choose("100")  # before kpis: the unique beneficiaries indicator, 500
    first = client.get(address["kpis"], **HX)
    assert children(first) == 500 and builds["overview"] == 1
    choose("300")  # between the two: the larger one, 700
    second = client.get(address["figures"], **HX)
    assert children(second) == 500 and builds["overview"] == 1  # the first part's build, read
    assert second.context["data"] == first.context["data"]
    assert children(get_part(client, PAGE, "figures")) == 700 and builds["overview"] == 2  # the next page


def test_a_crafted_as_of_never_changes_what_a_page_of_another_moment_draws(client_viewer, reviewed):
    """``as_of`` comes from the address: a part asked with a moved one keeps the build it reads for that
    fingerprint, in a key space of its own. Asked for the fingerprint of a moment to come (here worked out with
    the secret key, which a reader does not have; a fingerprint also comes back when an edit is undone), it
    never makes the page of that moment draw the figures kept: its frame looks only at the moment's own key, so
    it draws no figures inline, and its parts show the figures of the moment."""
    address = placeholders(text(raw_get(client_viewer, PAGE)))
    run = SyncRun.objects.create(job=SyncRun.Job.WATCH, target="all", status=SyncRun.Status.RUNNING)
    coming = compute.fingerprint(f"{run.pk}:{overview_service.flags_fingerprint()}")  # once the run finishes
    query = parse_qs(urlsplit(address["kpis"]).query, keep_blank_values=True)
    query["as_of"] = [coming]
    kept = raw_get(client_viewer, f"{PAGE}?{urlencode(query, doseq=True)}", **HX)
    assert kept.status_code == 200 and kept.context["data"]["impact"]["children_reached"] == 500
    _second(reviewed)  # the sync brings a second children indicator, 700, the larger
    run.finish(SyncRun.Status.SUCCEEDED)
    compute.reset_memo()
    assert overview_service.fingerprint() == coming
    page = text(raw_get(client_viewer, PAGE))
    assert {"kpis", "figures"} <= set(placeholders(page))  # nothing stored for this moment: no figure inline
    assert get_part(client_viewer, PAGE, "kpis").context["data"]["impact"]["children_reached"] == 700


def _children_reached(client, query: str) -> int:
    response = raw_get(client, f"{PAGE}?{query}&_part=kpis", **HX)
    assert response.status_code == 200
    return response.context["data"]["impact"]["children_reached"]


def test_a_stored_build_never_answers_another_governorate(client_viewer, world):
    """The build is stored for every worker: its key must hold the governorate. Beirut alone, then the
    country stored, then Beirut again: the same figure as alone, never the country's."""
    alone = _children_reached(client_viewer, "section=&year=2026&governorate=Beirut")
    caches["shared"].clear()
    country = _children_reached(client_viewer, "section=&year=2026")
    assert (alone, country) == (200, 500)  # the two filters do differ
    assert _children_reached(client_viewer, "section=&year=2026&governorate=Beirut") == alone


def test_a_stored_build_never_answers_another_year(client_viewer, world):
    ReportingYear.objects.get_or_create(name="2025", defaults={"year": "2025", "current": False})
    alone = _children_reached(client_viewer, "section=&year=2025")
    caches["shared"].clear()
    current = _children_reached(client_viewer, "section=&year=2026")
    assert (alone, current) == (0, 500)
    assert _children_reached(client_viewer, "section=&year=2025") == alone


def test_a_section_spelt_another_way_never_answers_the_real_one(client_viewer, world):
    """The sections come from the address as typed: "Child_Protection" (no such section) asked first stores an
    empty build for every user, which must never answer the real section afterwards."""
    crafted = _children_reached(client_viewer, "section=Child_Protection&year=2026")
    compute.reset_memo()  # another request
    assert (crafted, _children_reached(client_viewer, "section=Child+Protection&year=2026")) == (0, 500)


def test_two_filters_never_share_a_key(world):
    """However a section or a governorate is spelt (a space or an underscore, a bar or a colon inside a name),
    two different filters never share the key of a stored build."""
    filters = [
        (["Child Protection"], ""),
        (["Child_Protection"], ""),
        (["Child Protection", "Education"], ""),
        (["Child Protection|Education"], ""),
        (["Child Protection"], "Beirut:Akkar"),
        (["Child Protection:Beirut"], "Akkar"),
        ([], "Mount Lebanon"),
        ([], "Mount_Lebanon"),
    ]
    keys = {
        overview_service._cache_key(
            overview_service.Scope(year=2026, sections=sections, governorate=governorate, today=TODAY)
        )
        for sections, governorate in filters
    }
    assert len(keys) == len(filters)


class _EveryStringTranslated(gettext.NullTranslations):
    """A language in which every translatable string reads differently: a build that held one would differ
    from the English build."""

    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_the_overview_build_reads_the_same_in_every_language(world, reporting_year):
    """One stored build serves every language (``lang_neutral``): nothing in it may be translated, the
    freshness strip neither. Labels are translated by the templates."""
    scope = overview_service.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    with translation.override("en"):
        english = (overview_service._Builder(scope).build(), overview_service._freshness())
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        assert translation.gettext("Children reached") == "[Children reached]"  # the language is on
        other = (overview_service._Builder(scope).build(), overview_service._freshness())
    finally:
        translation.deactivate()
    assert other[0] == english[0], "the overview build holds a translated string"
    assert other[1] == english[1], "the freshness strip holds a translated string"


def test_a_visit_in_another_language_reads_the_stored_build(client_viewer, world, monkeypatch):
    calls = []
    real = overview_service._Builder.build

    def counted(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(overview_service._Builder, "build", counted)
    english = text(get_part(client_viewer, PAGE, "kpis", HTTP_ACCEPT_LANGUAGE="en"))
    compute.reset_memo()
    for language in ("ar", "fr"):
        assert text(get_part(client_viewer, PAGE, "kpis", HTTP_ACCEPT_LANGUAGE=language)) == english
    assert len(calls) == 1, "the stored build was computed again for another language"
    frame = text(raw_get(client_viewer, PAGE, HTTP_ACCEPT_LANGUAGE="fr"))
    assert 'data-part-loaded="kpis"' in frame  # ... and the frame draws it inline in any language


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    "",  # first load: the own section
    "?section=",  # every section
    "?section=Child+Protection",
    "?section=&governorate=Akkar",
    "?section=Education",  # a filter with no result
    "?year=2025",  # the previous year
    "?section=&review=week",
    "?section=&review=yesterday",
]


@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(client_viewer, own_section, reviewed, query):
    ReportingYear.objects.get_or_create(name="2025", defaults={"year": "2025", "current": False})
    assembled = assemble(client_viewer, PAGE + query)
    assert {"kpis", "figures", "review", "news"} <= set(assembled.parts)  # review-more: when findings fold
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client_viewer, PAGE + query)
    assert drawn.status_code == 200
    # under every filter: no link of a part (nor of the eager page) carries _part, and no part sends anything
    # that would run once swapped in, its included partials too
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link for link in LINK.findall(text(drawn)))
    assert placeholders(text(drawn)) == {"for-you": reverse("watch:card")}  # only the Watch card is left
    assert normalise(_without_for_you(assembled)) == normalise(text(drawn), exclude=("part-for-you",))
    # one chart JSON per part, unique in the page, the same as the eager page's
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone and FIGURES_JSON in together
    figures = _json_blocks(text(assembled.parts["figures"]))
    assert list(figures) == [FIGURES_JSON] and figures[FIGURES_JSON] == alone[FIGURES_JSON]
    assert (
        figures[FIGURES_JSON]["labels"]["not_reported"] == "Not reported"
    )  # the labels travel with the charts


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_offers_for_you_its_own_page(client_viewer, reviewed):
    """*Show the figures* (no JavaScript) leads to the page drawn at once. The For you card comes from another
    view, so that page cannot draw it: a quiet slot (no grey shapes forever, not busy), whose link is the
    card's own page, never the page already shown."""
    frame = text(client_viewer.get(PAGE))
    shown = re.search(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert shown, "the frame offers the figures without JavaScript"
    address = PAGE + shown.group(1).replace("&amp;", "&")
    assert "_eager=1" in address
    page = text(client_viewer.get(address))
    slot = placeholder_attributes(page)
    assert list(slot) == ["for-you"]  # every other part is drawn
    assert "aria-busy" not in slot["for-you"] and slot["for-you"]["hx-get"] == reverse("watch:card")
    start = page.index('id="part-for-you"')
    card = page[start : page.index('id="part-figures"', start)]
    assert "part__skeleton" not in card and "Show the figures" not in card
    assert f'<noscript><a href="{reverse("watch:for_you")}">' in card
    assert "_eager=1" not in "".join(LINK.findall(card))
    # where scripts run but parts.js never did, its error box offers the same page (review R3-critic-2)
    assert (
        f'href="{reverse("watch:for_you")}" data-part-fallback hidden>Open this block on its own page</a>'
        in card
    )
    # base.html's script that shows those boxes comes with a page in parts (the frame, and the page drawn at once for
    # its quiet slot), and with no other page
    assert (
        frame.count('classList.contains("parts-ready")')
        == page.count('classList.contains("parts-ready")')
        == 1
    )
    assert "parts-ready" not in text(client_viewer.get(reverse("help:index")))


# ------------------------------------------------------------------------------------- templates left to load
LOAD_TRIGGERS_ALLOWED = {
    "web/templates/base.html",  # the sidebar's For you count
    "fmm/templates/fmm/tabs/insights.html",  # the Monitoring insights AI card, which polls
}


def test_every_load_trigger_left_is_on_the_allow_list():
    """A block that loads after the page is a part now (SPEC7 §3.2); only these keep ``hx-trigger="load"``."""
    from pathlib import Path

    root = Path(views.__file__).resolve().parents[1]
    found = {
        str(path.relative_to(root))
        for path in root.glob("*/templates/**/*.html")
        if re.search(r'hx-trigger="load\b', path.read_text(encoding="utf-8"))
    }
    assert found <= LOAD_TRIGGERS_ALLOWED, found - LOAD_TRIGGERS_ALLOWED
    overview = (root / "reports/templates/reports/overview.html").read_text(encoding="utf-8")
    assert "watch-overview-slot" not in overview and '{% part "for-you"' in overview
