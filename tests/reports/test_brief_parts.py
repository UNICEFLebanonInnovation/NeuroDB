"""The management brief in parts (SPEC7 §3.3-3.4 for the brief; STAGEA "Brief").

The brief answers its frame at once (purpose, filter bar with its "Figures as of" line and Print button, the tab bar,
the Reference block) with a placeholder for each of its two parts: ``brief`` (the headline figures, the things to
decide with their staff-only Assign links, the sections at a glance and the brief as text behind Copy summary), the
group's leader, and ``sections`` (ahead or behind, how sure, who and where, partners, money, action, with the one
chart JSON). Both read one brief build, kept in the cache every worker shares. These tests check the frame without
any figure work, each part, the part queries, that the page put together from its parts is the page drawn at once
(``_eager=1``), that a stored build never shows one person's view to another, and that an edit shows at once.

On the brief's world (``data`` of tests/reports/test_brief.py): the overview fixture's programme document (Amel,
Child Protection, 500 children reached in 2026), a district, a section plan, a grant and a partner link; a daily
review is run on it where a test needs the things to decide.
"""

from __future__ import annotations

import datetime
import gettext
import html as html_module
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from neurodb.accounts.models import User
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.donors.models import DonorAccount
from neurodb.indicators.models import ReportingYear
from neurodb.partnerships.models import PartnerLink
from neurodb.reports import brief as brief_service
from neurodb.reports import overview as overview_service
from neurodb.reports import views
from neurodb.reports.models import SectionPlan
from neurodb.review import services as review_services
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from tests.reports.overview_fixture import TODAY
from tests.reports.test_brief import data  # noqa: F401 - the brief's hand-counted world (a fixture)
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
from tests.web.test_parts_frontend import needs_node, run_dom

pytestmark = pytest.mark.django_db

PAGE = "/brief/"  # reports:brief
PARTS = ("brief", "sections")  # the frame's placeholders, in page order; "brief" leads the group
SECTIONS_JSON = "brief-sections-data"  # the one chart JSON of the page, in the sections part
TABS = ("brief", "pace", "confidence", "who", "partners", "money", "action", "reference")  # #brief-<key>
STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame warm (its filter sections,
# lineage and navigation stored, as after the first visit of a page) is 12: the request's floor (session, user, donor
# check, user role, the sidebar's sections and years) plus the year, the cache stamp (last sync run, flags), the
# governorates of the filter options, the latest review (the lineage's key and the build's), and the hand-entered
# tables of the brief build's key (section plans, finding assignments, ActivityInfo partner links: one query) to see
# whether it is stored. Cold it also computes the filter sections, the lineage (3 sync runs, the population year, the
# review and its findings) and the navigation: 24. Drawn inline (the build stored) it is the same 12: both parts read
# the build the frame found stored, under the key it read. Each part is measured with nothing stored: brief and
# sections each compute the whole build (the year before's reporting year, two overview builds and the brief's own
# reads): 91; the group's second part reading the stored build is 8. SPEC_HOME §3.4: the year is the calendar year,
# its ActivityInfo reporting year read from the ActivityInfo state (4 queries cold, kept 30 s per worker) and the
# Year select's years (1); cold the menu also reads its sources, kept per worker (SPEC_HOME §4), and shares the
# ActivityInfo state: cold 27, each part 94.
SHELL_QUERIES_WARM = 14
SHELL_QUERIES_COLD = 29
SHELL_QUERIES_INLINE = 14
PART_QUERIES = {"brief": 96, "sections": 96}
PART_QUERIES_STORED = 10


@pytest.fixture
def world(request):
    """The brief's hand-counted world: ``data`` of tests/reports/test_brief.py (imported above)."""
    return request.getfixturevalue("data")


@pytest.fixture
def reviewed(world):
    """The brief's world with a daily review: the things to decide list its open findings, each with an Assign
    link for staff."""
    review_services.run(date=TODAY, today=TODAY)
    return world


@pytest.fixture
def sectioned(viewer, section):
    """The viewer works in Child Protection (the brief shows every section to everyone by default)."""
    viewer.section = section
    viewer.save()
    return viewer


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the brief raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    monkeypatch.setattr(brief_service, "build", refuse)
    monkeypatch.setattr(brief_service, "_build", refuse)
    monkeypatch.setattr(brief_service._Builder, "build", refuse)
    monkeypatch.setattr(overview_service, "build", refuse)
    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    monkeypatch.setattr(views, "_brief_chart_data", refuse)


def _json_blocks(html: str) -> dict[str, object]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a chart JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _decisions(html: str) -> str:
    """The things to decide, from their heading to the sections at a glance."""
    return html.split("Things to decide", 1)[1].split("Sections at a glance", 1)[0]


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("reports:brief") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, reviewed, monkeypatch):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Management brief" in html and 'class="page-purpose"' in html
    assert "2026 · every section" in html  # the subtitle: the calendar year (SPEC_HOME §3.4)
    assert 'aria-label="Brief filters"' in html and 'name="section"' in html  # the filter bar ...
    stamp = html.split('class="brief-stamp"')[1].split("</p>")[0]
    assert "Figures as of" in stamp and "eTools Datamart sync" in stamp  # ... its stamp, from the lineage
    assert re.search(r"<button[^>]*data-autoprint[^>]*>", html)  # ... and its Print button
    nav = re.search(r'<nav class="brief-tabs".*?</nav>', html, re.S)
    assert nav and re.findall(r'href="#brief-([a-z]+)"', nav.group(0)) == list(TABS)
    assert 'id="brief-reference"' in html and "Lineage of this brief" in html and "Data dictionary" in html
    assert list(placeholders(html)) == list(PARTS)
    for data_id in (SECTIONS_JSON, "brief-chart-data"):  # no figures, so no chart data
        assert f'id="{data_id}"' not in html
    for figure in (
        "Country management brief",
        "Things to decide",
        'id="brief-text"',
        "Are we ahead or behind?",
    ):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_filter(client_viewer, reviewed):
    html = text(client_viewer.get(PAGE))
    found = placeholder_attributes(html)
    assert list(found) == list(PARTS)
    stamp = brief_service.fingerprint()  # the figures the frame was drawn with (test_the_sections_part_...)
    for name in PARTS:
        url = found[name]["hx-get"]
        assert urlsplit(url).path == PAGE
        # every section written out, the year always
        assert _query(url) == {"section": [""], "year": ["2026"], "as_of": [stamp], "_part": [name]}, url
        assert found[name]["data-part-group"] == "brief"
    assert (
        found["brief"]["data-part-priority"] == "high" and found["sections"]["data-part-priority"] == "normal"
    )
    assert found["brief"]["class"] == "part part--tiles" and found["sections"]["class"] == "part part--charts"
    assert "data-part-needs" not in found["brief"] and found["sections"]["data-part-needs"] == "plotly"
    # the anchors of the tab bar that live in each part (test_each_tab_anchor_...)
    assert found["brief"]["data-part-anchors"] == "brief-brief"
    assert found["sections"]["data-part-anchors"].split() == [f"brief-{key}" for key in TABS[1:-1]]
    # the room of what each part holds, on a wide screen, a tablet or narrow window (grids stacked) and a phone, as
    # measured on the x20 parity world
    assert found["brief"]["style"] == "--part-h: 1565px; --part-h-sm: 3060px; --part-h-md: 1595px"
    assert found["sections"]["style"] == "--part-h: 6555px; --part-h-sm: 11215px; --part-h-md: 8235px"
    brief = html[html.index('id="part-brief"') : html.index('id="part-sections"')]
    assert brief.count("skeleton__block--tile") == 5  # the five headline tiles
    # the chosen sections and year travel, a key the page does not read does not
    html = text(
        client_viewer.get(PAGE, {"section": ["Child Protection", "Education"], "year": "2026", "tab": "x"})
    )
    assert _query(placeholders(html)["sections"]) == {
        "section": ["Child Protection", "Education"],
        "year": ["2026"],
        "as_of": [stamp],
        "_part": ["sections"],
    }


@pytest.mark.shell
def test_without_any_reporting_year_both_parts_are_there(client_viewer, world, reporting_year):
    """The brief's year is the calendar year (SPEC_HOME §3.4): without any ActivityInfo reporting year its frame
    draws both placeholders and the "Figures as of" line, and its parts compute eTools' figures."""
    reporting_year.delete()
    html = text(client_viewer.get(PAGE))
    assert "No reporting year is configured" not in html and list(placeholders(html)) == list(PARTS)
    assert "Figures as of" in html
    brief = get_part(client_viewer, PAGE, "brief")
    tiles = {t["key"]: t for t in brief.context["data"]["headline"]["tiles"]}
    assert (
        tiles["children"]["value"] == 500 and "ActivityInfo was not used in 2026" in tiles["children"]["hint"]
    )


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(
    client_viewer, sectioned, reviewed, django_assert_max_num_queries
):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and list(placeholders(text(response))) == list(PARTS)
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and list(placeholders(text(response))) == list(PARTS)


def test_a_finished_sync_shows_in_the_figures_as_of_line_at_once(client_viewer, world):
    """The lineage is stored for the frame (two minutes), under a key a finished sync changes."""

    def stamp() -> str:
        return text(raw_get(client_viewer, PAGE)).split('class="brief-stamp"')[1].split("</p>")[0]

    assert "eTools Datamart sync: not yet synced" in stamp()  # stored now
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="all", status=SyncRun.Status.RUNNING)
    run.finish(SyncRun.Status.SUCCEEDED)
    assert "eTools Datamart sync: not yet synced" not in stamp()
    assert f"Run {run.pk}" in stamp()


def test_a_new_review_shows_in_the_reference_block_at_once(client_viewer, world):
    """A review stored without a sync run (the review job always finishes one) still changes the lineage's key."""

    def reference() -> str:
        html = text(raw_get(client_viewer, PAGE))
        return html[html.index('id="brief-reference"') :]

    assert "No review yet." in reference()  # stored now
    DailyReview.objects.create(date=TODAY, status=DailyReview.Status.SUCCEEDED, checks_run=3)
    assert "No review yet." not in reference() and f"{TODAY.isoformat()}, 3 checks, 0 findings" in reference()


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("name", PARTS)
def test_each_part_answers_its_wrapper(client_viewer, reviewed, name):
    response = get_part(client_viewer, PAGE, name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "brief": ('id="brief-brief"', "Country management brief", "Things to decide", "Sections at a glance"),
        "sections": ('id="brief-pace"', 'id="brief-confidence"', 'id="brief-action"', "Partner scorecard"),
    }[name]
    for figure in expected:
        assert figure in html, figure
    assert (f'id="{SECTIONS_JSON}"' in html) == (name == "sections")  # the one chart JSON, in its own part
    assert all(
        f'data-source="{SECTIONS_JSON}"' in chart for chart in re.findall(r"<div[^>]*data-chart=[^>]*>", html)
    )
    # nothing that would run once swapped in, in the part or in any partial it includes (SPEC7 §1.3 rule 3)
    assert executable(html) == []


def test_the_parts_draw_the_figures_of_the_build(client_viewer, world):
    """The assembled page and the eager page draw the same part templates, so their equivalence cannot catch a
    wrong figure in one: the figures themselves are checked here, the headline tile and the finding lifecycle."""
    review = DailyReview.objects.create(
        date=datetime.date.today() - datetime.timedelta(days=10),
        status=DailyReview.Status.SUCCEEDED,
        checks_run=3,
    )
    start = datetime.datetime.combine(review.date, datetime.time.min, tzinfo=datetime.UTC)
    days = [
        (2, 5, 9),
        (4, 5, None),
        (None, None, None),
    ]  # acknowledged, assigned, closed: days after it was raised
    for n, (acknowledged, assigned, closed) in enumerate(days):
        ReviewFinding.objects.create(
            review=review,
            key=f"lifecycle:{n}",
            check_id="lifecycle",
            severity="warning",
            title=f"Finding {n}",
        )
        if acknowledged is not None:
            FindingAssignment.objects.create(
                key=f"lifecycle:{n}",
                status=FindingAssignment.Status.CLOSED if closed else FindingAssignment.Status.ASSIGNED,
                acknowledged_at=start + datetime.timedelta(days=acknowledged),
                assigned_at=start + datetime.timedelta(days=assigned),
                closed_at=start + datetime.timedelta(days=closed) if closed else None,
            )
    brief_part = text(get_part(client_viewer, PAGE, "brief"))
    tile = r'<div class="kpi__label">Children reached</div>\s*<div class="kpi__value">\s*<a [^>]*>500</a>\s*</div>'
    assert re.search(tile, brief_part)
    sections_part = text(get_part(client_viewer, PAGE, "sections"))
    lifecycle = sections_part[sections_part.index('id="life-title"') :]
    # medians: acknowledged after 2 and 4 days (3), assigned 3 and 1 day later (2), closed 4 days after (4)
    for value, label in (("3", "Median days to acknowledge"), ("2", "to assign"), ("4", "to close")):
        mini = f'<div class="mini__value">{value}</div><div class="mini__label">{label}</div>'
        assert mini in lifecycle, (value, label)


@pytest.mark.parametrize("name", PARTS)
def test_each_part_stays_within_its_query_ceiling(
    client_viewer, reviewed, name, django_assert_max_num_queries
):
    address = f"{PAGE}?section=&year=2026&_part={name}"
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)
    # the group's second part, after its leader stored the build, only reads it
    other = f"{PAGE}?section=&year=2026&_part={'sections' if name == 'brief' else 'brief'}"
    with django_assert_max_num_queries(PART_QUERIES_STORED):
        response = client_viewer.get(other, **HX)
    assert response.status_code == 200


@pytest.mark.parametrize("name", PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    response = client.get(f"{PAGE}?section=&year=2026&_part={name}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?section=&year=2026&_part=brief", **HX)
    assert response.status_code == 200 and "Things to decide" not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


def test_copy_summary_and_its_text_travel_together_in_the_brief_part(client_viewer, reviewed):
    html = text(get_part(client_viewer, PAGE, "brief"))
    button = re.search(r"<button[^>]*data-copy-target=\"#brief-text\"[^>]*>", html)
    summary = re.search(r'<pre class="visually-hidden" id="brief-text">(.*?)</pre>', html, re.S)
    assert button and summary
    words = html_module.unescape(summary.group(1))
    assert words.startswith("Management brief") or "Children reached:" in words
    assert "Children reached:" in words and "Source: NeuroDB" in words


def test_the_assign_links_are_staff_only_whoever_stored_the_build(viewer, admin_user, reviewed):
    """The build is stored for every worker and every person: the Assign links come from the person asking,
    never from the build. A staff member's visit stores it, a viewer reads it (and the other way round)."""
    staff, reader = _signed_in(admin_user), _signed_in(viewer)
    add = reverse("admin:review_findingassignment_add")
    first = text(get_part(staff, PAGE, "brief"))  # the build is stored by a staff member's request
    assert add in _decisions(first) and ">Assign</a>" in _decisions(first)
    second = text(get_part(reader, PAGE, "brief"))
    assert add not in second and "Assign" not in _decisions(second)
    frame = text(raw_get(reader, PAGE))  # drawn inline from the stored build
    assert 'data-part-loaded="brief"' in frame and add not in frame
    caches["shared"].clear()
    assert add not in text(get_part(reader, PAGE, "brief"))  # stored by the viewer's request this time
    assert add in _decisions(text(get_part(staff, PAGE, "brief")))
    assert add in _decisions(text(raw_get(staff, PAGE)))  # ... and drawn inline for staff


def test_each_tab_anchor_is_in_the_frame_or_bumps_the_part_holding_it(client_viewer, reviewed):
    """The tab bar's links are in-page anchors: one inside a part is listed in that part's data-part-anchors (and
    no other's), so a click asks for that part first and scrolls to it once it is in (parts.js)."""
    frame = text(raw_get(client_viewer, PAGE))
    found = placeholder_attributes(frame)
    held = {name: text(get_part(client_viewer, PAGE, name)) for name in PARTS}
    for key in TABS:
        anchor = f"brief-{key}"
        inside = [name for name, body in held.items() if f'id="{anchor}"' in body]
        bumped = [
            name
            for name in PARTS
            if any(anchor.startswith(p) for p in found[name]["data-part-anchors"].split())
        ]
        if f'id="{anchor}"' in frame:
            assert (inside, bumped) == ([], []), anchor  # in the frame (Reference)
        else:
            assert inside == bumped and len(inside) == 1, (anchor, inside, bumped)


def test_a_stored_brief_is_drawn_in_the_frame(
    client_viewer, sectioned, reviewed, monkeypatch, django_assert_max_num_queries
):
    """Inline when ready: once the group's leader has stored the build, the frame draws both parts itself, so a
    warm brief is one request."""
    get_part(client_viewer, PAGE, "brief")  # the group leader computes and stores the build

    def refuse(self):
        raise AssertionError("the stored build was computed again")

    monkeypatch.setattr(brief_service._Builder, "build", refuse)
    with django_assert_max_num_queries(SHELL_QUERIES_INLINE):
        html = text(raw_get(client_viewer, PAGE))
    assert placeholders(html) == {}  # both parts are already in
    for name in PARTS:
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert "Things to decide" in html and "Partner scorecard" in html and f'id="{SECTIONS_JSON}"' in html
    # another filter is not stored: its figures are asked for
    other = text(raw_get(client_viewer, PAGE + "?section=Child+Protection"))
    assert list(placeholders(other)) == list(PARTS)


def test_a_warm_frame_reads_what_the_build_key_is_made_of_once(client_viewer, reviewed):
    """The frame's look at the stored build and the build it then draws (both parts inline) share one reading of
    the key's hand-entered tables and latest review (the lineage's key reads that review too), and the year
    before's reporting year is looked up only to compute a build: a stored one never needs it."""
    get_part(client_viewer, PAGE, "brief")  # the frame's lineage and the build stored
    with CaptureQueriesContext(connection) as queries:
        html = text(raw_get(client_viewer, PAGE))
    assert placeholders(html) == {}  # both parts drawn inline, from the stored build
    sql = [query["sql"] for query in queries.captured_queries]

    def count(fragment: str) -> int:
        return sum(fragment in statement for statement in sql)

    assert count('MAX("reports_sectionplan"."updated_at")') == 1
    assert count('MAX("review_findingassignment"."updated_at")') == 1
    assert count('MAX("review_dailyreview"."id")') == 1
    assert count('"pivoting_reportingyear"."name"::text LIKE \'2025%\'') == 0  # the year before's


def _children(client, query: str) -> int:
    response = raw_get(client, f"{PAGE}?{query}&_part=brief", **HX)
    assert response.status_code == 200
    return {t["key"]: t["value"] for t in response.context["data"]["headline"]["tiles"]}["children"]


def test_a_stored_build_never_answers_another_section_or_year(client_viewer, world):
    ReportingYear.objects.get_or_create(name="2025", defaults={"year": "2025", "current": False})
    alone = _children(client_viewer, "section=Education&year=2026")
    caches["shared"].clear()
    country = _children(client_viewer, "section=&year=2026")
    assert (alone, country) == (0, 500)  # the two filters do differ
    assert _children(client_viewer, "section=Education&year=2026") == alone
    assert _children(client_viewer, "section=&year=2025") == 0  # another year, never the stored 2026


def test_a_section_spelt_another_way_never_answers_the_real_one(client_viewer, world):
    """The sections come from the address as typed: "Child_Protection" (no such section) asked first stores an
    empty brief, and the empty overview build it reads, for every user; neither may answer the real section."""
    crafted = _children(client_viewer, "section=Child_Protection&year=2026")
    compute.reset_memo()  # another request
    assert (crafted, _children(client_viewer, "section=Child+Protection&year=2026")) == (0, 500)


def test_two_filters_never_share_a_key(world, reporting_year):
    filters = [
        ["Child Protection"],
        ["Child_Protection"],
        ["Child Protection", "Education"],
        ["Child Protection|Education"],
    ]
    keys = {
        brief_service._cache_key(
            brief_service.Scope(year=2026, reporting_year=reporting_year, sections=sections, today=TODAY)
        )
        for sections in filters
    }
    assert len(keys) == len(filters)
    # nor two days: a build stored just before midnight never answers the next day
    tomorrow = brief_service.Scope(
        year=2026, reporting_year=reporting_year, today=TODAY + datetime.timedelta(days=1)
    )
    today = brief_service.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    assert brief_service._cache_key(tomorrow) != brief_service._cache_key(today)


class _EveryStringTranslated(gettext.NullTranslations):
    """A language in which every translatable string reads differently: a value that held one would differ from
    the English value."""

    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_the_lineage_reads_the_same_in_every_language(reviewed):
    """The frame's lineage is stored once for every language (``lang_neutral``): nothing in it is translated."""
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="all", status=SyncRun.Status.RUNNING)
    run.finish(SyncRun.Status.SUCCEEDED)
    with translation.override("en"):
        english = brief_service._lineage(2026)
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        assert translation.gettext("Children reached") == "[Children reached]"  # the language is on
        other = brief_service._lineage(2026)
    finally:
        translation.deactivate()
    assert other == english, "the lineage holds a translated string"
    assert english["review"]["findings"] > 0 and english["sources"][0]["run"] == run.pk


def test_each_language_has_its_own_build_and_one_lineage(client_viewer, reviewed, monkeypatch):
    """The build holds labels translated as it is computed (an assignment's status), so it is kept per language;
    the lineage is not translated, so the frame reads one for every language."""
    calls = Counter()
    real_build, real_lineage = brief_service._Builder.build, brief_service._lineage

    def build(self):
        calls["build"] += 1
        return real_build(self)

    def lineage(year, jobs=None):
        calls["lineage"] += 1
        return real_lineage(year, jobs)

    monkeypatch.setattr(brief_service._Builder, "build", build)
    monkeypatch.setattr(brief_service, "_lineage", lineage)
    english = text(get_part(client_viewer, PAGE, "brief", HTTP_ACCEPT_LANGUAGE="en"))
    compute.reset_memo()
    assert text(get_part(client_viewer, PAGE, "brief", HTTP_ACCEPT_LANGUAGE="en")) == english
    assert calls["build"] == 1
    get_part(client_viewer, PAGE, "brief", HTTP_ACCEPT_LANGUAGE="fr")
    assert calls["build"] == 2, "a build stored in one language answered another"
    assert calls["lineage"] == 1, "the lineage was computed again for another language"


@override_settings(
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
        "shared": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "neurodb_cache"},
    }
)
def test_the_management_brief_look_up_answers_inside_a_read_only_tool_run(world):
    """The AI's management brief look-up reads the build every worker shares. In a read-only tool run (as NeuroDB
    Watch's, the help's and the field monitoring chat's are), with the cache in the database as in production
    and nothing stored, the build is computed there and nothing is written."""
    from neurodb.assistant import tools

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM neurodb_cache")
        before = cursor.fetchone()[0]
    with tools.read_only():
        answer = tools.run("management_brief", {})
        assert SyncRun.objects.count() >= 0  # the transaction still answers
    assert answer["year"] == 2026
    assert {t["key"]: t["value"] for t in answer["headline"]["tiles"]}["children"] == 500
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM neurodb_cache")
        assert cursor.fetchone()[0] == before


# ------------------------------------------------------------------------------------------- fresh after an edit
def test_an_assignment_made_from_the_assign_link_shows_at_once(client, admin_user, reviewed):
    """Fresh after edit (STAGEA): a staff member follows a decision's Assign link to the admin, gives it an owner,
    and the brief shows that owner at once, though its build was stored a moment before."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    before = text(get_part(client, PAGE, "brief"))
    add = reverse("admin:review_findingassignment_add")
    link = re.search(rf'<a href="({re.escape(add)}\?[^"]+)">Assign</a>', _decisions(before))
    assert link, "a decision without an owner offers staff its Assign link"
    address = html_module.unescape(link.group(1))
    assert client.get(address).status_code == 200  # the admin's form, filled in from the link
    filled = {key: values[0] for key, values in _query(address).items()}
    response = client.post(
        add,
        {
            **filled,
            "owner": "Chief of Child Protection",
            "due_date": "2026-11-01",
            "status": "assigned",
            "note": "",
        },
    )
    assert response.status_code == 302, text(response)[:2000]
    after = text(get_part(client, PAGE, "brief"))
    assert "Owner: Chief of Child Protection" not in before
    assert "Owner: Chief of Child Protection" in _decisions(after)


def test_a_main_indicator_chosen_on_the_pd_page_shows_at_once(client, admin_user, world):
    """The programme document page's "unique beneficiaries indicator" choice (an indicator flag) changes the
    children reached: the brief shows it at once, though its build was stored a moment before."""
    _second(world)  # a second children indicator of the PD: 700, the larger, counts until one is chosen
    client.force_login(admin_user)

    def children() -> int:
        tiles = get_part(client, PAGE, "brief").context["data"]["headline"]["tiles"]
        return {t["key"]: t["value"] for t in tiles}["children"]

    assert children() == 700  # stored now
    response = client.post(reverse("reports:pd_main_indicator", args=[world["pd"].pk]), {"key": "100"})
    assert response.status_code == 302
    assert children() == 500  # the chosen indicator


def test_a_section_plan_changed_in_the_admin_shows_at_once(client, admin_user, reviewed):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)

    def bullets() -> list[dict]:
        return _json_blocks(text(get_part(client, PAGE, "sections")))[SECTIONS_JSON]["bullets"]

    assert bullets()[0]["cp_target"] == 2000  # stored now
    plan = SectionPlan.objects.get(section="Child Protection")
    response = client.post(
        reverse("admin:reports_sectionplan_change", args=[plan.pk]),
        {
            "year": 2026,
            "section": "Child Protection",
            "children_target": 3000,
            "required_usd": "20000.00",
            "note": "",
        },
    )
    assert response.status_code == 302, text(response)[:2000]
    assert bullets()[0]["cp_target"] == 3000


def _finish_a_watch_run() -> None:
    """A run of a job that changes no figure (NeuroDB Watch, every morning): it moves every build's key all the
    same, as each of the 52 runs of an eTools Datamart sync does."""
    SyncRun.objects.create(job=SyncRun.Job.WATCH, target="all", status=SyncRun.Status.RUNNING).finish(
        SyncRun.Status.SUCCEEDED
    )


def test_the_sections_part_reads_the_build_the_brief_part_stored_though_its_key_moved_between(
    client, admin_user, reviewed, monkeypatch
):
    """The browser asks for ``sections`` once ``brief`` is in. A run that finishes between the two (any job's)
    or an edit moves the build's key: ``sections`` still reads the build ``brief`` stored, found from the
    fingerprint its address carries (``as_of``), so the build is computed once and the page never shows the
    figures of two moments side by side (up to round 2 the second part computed a build of its own). The next
    page shows the figures of the moment, and so does the old address once its build is no longer stored."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    builds = Counter()
    real_build = brief_service._Builder.build

    def build(self):
        builds["brief"] += 1
        return real_build(self)

    def target(response) -> int:
        return _json_blocks(text(response))[SECTIONS_JSON]["bullets"][0]["cp_target"]

    monkeypatch.setattr(brief_service._Builder, "build", build)
    address = placeholders(text(raw_get(client, PAGE)))
    first = client.get(address["brief"], **HX)
    assert first.status_code == 200 and builds["brief"] == 1
    _finish_a_watch_run()
    plan = SectionPlan.objects.get(section="Child Protection")
    response = client.post(
        reverse("admin:reports_sectionplan_change", args=[plan.pk]),
        {"year": 2026, "section": "Child Protection", "children_target": 3000, "required_usd": "20000.00"},
    )
    assert response.status_code == 302, text(response)[:2000]
    second = client.get(address["sections"], **HX)
    assert second.status_code == 200 and builds["brief"] == 1  # read, not computed again
    assert second.context["data"] == first.context["data"]  # the first part's build
    assert target(second) == 2000
    assert target(get_part(client, PAGE, "sections")) == 3000 and builds["brief"] == 2  # the next page
    caches["shared"].clear()  # two minutes later: the frame's build is gone
    assert target(client.get(address["sections"], **HX)) == 3000 and builds["brief"] == 3


def test_both_parts_read_one_build_though_the_figures_moved_before_the_first_and_again_between(
    client, admin_user, reviewed, monkeypatch
):
    """An edit lands between the frame and ``brief`` (the section plan's target: 3000), and another one between
    ``brief`` and ``sections`` (4000): ``brief`` computes the build of its moment and keeps it for the frame's
    ``as_of``, and ``sections`` reads it there, so the build is computed once and the page shows one moment (up
    to round 3 ``sections`` computed a second build, with 4000 beside the first part's 3000). The next page
    shows the figures of the moment."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    builds = Counter()
    real_build = brief_service._Builder.build

    def build(self):
        builds["brief"] += 1
        return real_build(self)

    def target(response) -> int:
        return _json_blocks(text(response))[SECTIONS_JSON]["bullets"][0]["cp_target"]

    def plan_target(children: int) -> None:
        plan = SectionPlan.objects.get(section="Child Protection")
        response = client.post(
            reverse("admin:reports_sectionplan_change", args=[plan.pk]),
            {
                "year": 2026,
                "section": "Child Protection",
                "children_target": children,
                "required_usd": "20000.00",
            },
        )
        assert response.status_code == 302, text(response)[:2000]

    monkeypatch.setattr(brief_service._Builder, "build", build)
    address = placeholders(text(raw_get(client, PAGE)))
    plan_target(3000)  # before the first part
    first = client.get(address["brief"], **HX)
    assert first.status_code == 200 and builds["brief"] == 1
    plan_target(4000)  # between the two parts
    second = client.get(address["sections"], **HX)
    assert second.status_code == 200 and builds["brief"] == 1  # read, not computed again
    assert second.context["data"] == first.context["data"] and target(second) == 3000
    _finish_a_watch_run()  # and a run that moves the key once more changes nothing either
    again = client.get(address["sections"], **HX)
    assert target(again) == 3000 and builds["brief"] == 1
    assert target(get_part(client, PAGE, "sections")) == 4000 and builds["brief"] == 2  # the next page


def test_a_partner_link_changed_in_the_admin_shows_at_once(client, admin_user, reviewed):
    """Staff correct an ActivityInfo partner link in the admin (the partner page's *Edit the link* leads there),
    an edit that starts no sync run. The brief reads the links (*How sure*: partners linked across sources; the
    two sources per partner), so they are in its key: the edit shows at once, though the build was stored a
    moment before (up to round 3 it showed only when the stored build expired, for every user)."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)

    def linked() -> float:
        (row,) = get_part(client, PAGE, "sections").context["data"]["confidence"]["rows"]
        return row["linked_percent"]

    def link_to(partner) -> None:
        response = client.post(
            reverse("admin:etools_partnerlink_change", args=[link.pk]), {"partner": partner, "_save": "Save"}
        )
        assert response.status_code == 302, text(response)[:2000]

    assert linked() == 100.0  # stored now
    link = PartnerLink.objects.get(label="Partner A")
    partner = link.partner_id
    link_to("")  # not linked
    link.refresh_from_db()
    assert link.partner_id is None and link.method == PartnerLink.Method.MANUAL
    assert linked() == 0.0
    link_to(partner)  # linked again: as many links as before, a later change
    assert linked() == 100.0


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    "",  # first load: every section
    "?section=",
    "?section=Child+Protection",
    "?section=Child+Protection&section=Education",
    "?section=Education",  # a filter with no result
    "?year=2025",  # the previous year
    "?section=Child+Protection&year=2026",
]


@pytest.mark.parametrize("role", ["viewer", "staff"])
@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(viewer, admin_user, sectioned, reviewed, role, query):
    ReportingYear.objects.get_or_create(name="2025", defaults={"year": "2025", "current": False})
    client = _signed_in(viewer if role == "viewer" else admin_user)
    assembled = assemble(client, PAGE + query)
    assert set(assembled.parts) == set(PARTS)
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client, PAGE + query)
    assert drawn.status_code == 200
    # under every filter: no link of a part (nor of the eager page) carries _part, and no part sends anything that
    # would run once swapped in, its included partials too
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link for link in LINK.findall(text(drawn)))
    assert "_part=" not in "".join(LINK.findall(assembled.html))
    assert placeholders(text(drawn)) == {}  # every part drawn
    assert normalise(assembled.html) == normalise(text(drawn))
    # one chart JSON, unique in the page, the same as the eager page's, with the labels the builders read
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone and [key for key in together if key.startswith("brief-")] == [SECTIONS_JSON]
    sections = _json_blocks(text(assembled.parts["sections"]))
    assert list(sections) == [SECTIONS_JSON] and sections[SECTIONS_JSON] == alone[SECTIONS_JSON]
    assert sections[SECTIONS_JSON]["labels"]["not_reported"] == "Not reported"
    assert not _json_blocks(text(assembled.parts["brief"]))  # the brief part draws no chart


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, reviewed):
    """*Show the figures* (no JavaScript) leads to the page drawn at once: every block, no grey shapes left and no
    link to the page already shown."""
    frame = text(client_viewer.get(PAGE))
    shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert len(shown) == len(PARTS) and len(set(shown)) == 1
    address = PAGE + html_module.unescape(shown[0])
    assert "_eager=1" in address
    page = text(client_viewer.get(address))
    assert placeholders(page) == {}
    for figure in (
        "Things to decide",
        "Copy summary",
        "Are we ahead or behind?",
        "Partner scorecard",
        "Finding lifecycle",
        "Lineage of this brief",
        f'id="{SECTIONS_JSON}"',
    ):
        assert figure in page, figure
    assert "part__skeleton" not in page and "Show the figures" not in page
    assert "_eager=1" not in "".join(LINK.findall(page))


# ------------------------------------------------------------------------------------------------- printing
def test_the_print_button_waits_for_every_part_and_for_the_charts_being_drawn():
    """The brief's Print button (data-autoprint) loads every part still to come and waits for the charts inside
    them; with every part in, or drawn with the page, it still waits for the charts being drawn."""
    app = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    autoprint = app.split("async function printWhenReady", 1)[1].split("\n}\n", 1)[0]
    assert "pendingParts() > 0 || pendingModules() > 0" in autoprint
    assert "await loadAll({ timeout: 60000 })" in autoprint and autoprint.rstrip().endswith("window.print();")


@needs_node
def test_load_all_waits_for_the_charts_of_parts_already_in():
    found = run_dom(
        """
const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page();
main.append(
  placeholder({ name: "brief", kind: "tiles", priority: "high", group: "brief" }),
  placeholder({ name: "sections", kind: "charts", group: "brief" }),
);
parts.initParts();
await start();
out.first = htmx.open();
htmx.respond("brief", { nodes: () => [loaded("brief")] });
await clock.tick(100);
out.second = htmx.open();
htmx.respond("sections", { nodes: () => [loaded("sections")] });
await clock.tick(100);
out.partsLeft = parts.pendingParts();
// the charts of the parts are still being drawn (app.js hands enhance()'s promise over)
let drawn;
parts.trackModules(new Promise((resolve) => { drawn = resolve; }));
out.modules = parts.pendingModules();
let ready = false;
parts.loadAll({ timeout: 60000 }).then(() => { ready = true; });
await clock.tick(5000);
out.beforeCharts = ready;
drawn();
await clock.tick(10);
out.afterCharts = ready;
out.modulesAfter = parts.pendingModules();
"""
    )
    assert (found["first"], found["second"]) == (["brief"], ["sections"])  # the leader first, alone
    assert found["partsLeft"] == 0 and found["modules"] == 1
    assert (found["beforeCharts"], found["afterCharts"], found["modulesAfter"]) == (False, True, 0)
