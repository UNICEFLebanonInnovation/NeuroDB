"""The youth page in parts (STAGE3 group D1, SPEC7 §3.3-3.4 for ``/youth/``).

The page answers its frame at once (the filter bar, whose choices are read in the database without decoding the
year's payload, and the line saying when Compiler's figures were read) with a placeholder for each of its two parts:
``summary`` (the key figures and the youth indicators with their eTools indicators), the group's leader, and
``details`` (the breakdowns, Compiler and eTools side by side, the programme documents in Compiler). Both read one
build, kept in the cache every worker shares. These tests check the frame without any figure work, each part, the
part queries, that the page put together from its parts is the page drawn at once (``_eager=1``), that a stored build
never shows one person's view to another, that a link confirmed in the admin or a new sync shows at once, and that
the build no longer computes the chart data no template read.

The world: tests/youth/test_youth.py's figures for 2026 (three young people, two partners, one eTools programme
document with a suggested link) read by its sync, and the same figures stored for 2025.
"""

# ruff: noqa: F811 (the eTools fixture "etools" comes from test_youth)
from __future__ import annotations

import gettext
import html as html_module
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from neurodb.accounts.models import User
from neurodb.core import compute
from neurodb.donors.models import DonorAccount
from neurodb.youth import linking, services
from neurodb.youth.figures import Figures
from neurodb.youth.models import YouthFigures, YouthIndicatorLink
from neurodb.youth.sync import sync
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
from tests.youth.test_youth import FakeClient, etools, payload  # noqa: F401 - etools is a fixture

pytestmark = pytest.mark.django_db

PAGE = "/youth/"  # youth:dashboard
PARTS = ("summary", "details")  # the frame's placeholders, in page order; "summary" leads the group
TEMPLATES = Path(settings.BASE_DIR) / "neurodb" / "youth" / "templates" / "youth"
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">', re.S)
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame: the request's floor
# (session, user, donor check, user role, the sidebar's sections and years), the stored years, the year's row
# without its payload, the filter's choices cut out of the payload in the database (cold), the year's links (one
# digest) and the cache stamp (last sync run, indicator flags); drawn inline the same without the choices (stored).
# Each part with nothing stored computes the build (the payload, the links, the eTools indicators of the linked
# programme documents and their reports); the group's second part reading the stored build only reads the keys.
SHELL_QUERIES = 14
SHELL_QUERIES_INLINE = 13
PART_QUERIES = 19
PART_QUERIES_STORED = 10


@pytest.fixture
def world(etools):
    """See the module docstring."""
    sync(triggered_by="test", client=FakeClient(["2026"]))
    YouthFigures.objects.create(year="2025", fetched_at=timezone.now(), payload=payload("2025"))
    return etools


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the page raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    for name in ("dashboard", "_links", "_indicator_rows", "_breakdown", "_programme_documents", "load"):
        monkeypatch.setattr(services, name, refuse)
    monkeypatch.setattr(linking, "describe", refuse)
    monkeypatch.setattr(linking.monitoring, "indicators", refuse)


def _links_tile(client, url: str = PAGE) -> str:
    """The "Links to eTools" tile's hint, as the summary part draws it."""
    html = text(get_part(client, url, "summary"))
    return re.search(r"(\d+ confirmed, \d+ suggested)", html).group(1)


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("youth:dashboard") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, world, monkeypatch):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    with CaptureQueriesContext(connection) as queries:
        response = client_viewer.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Youth programmes" in html and 'aria-label="Youth filters"' in html
    # the filter bar whole: the years and every choice, cut out of the payload in the database
    for field in ("f-year", "f-master", "f-partner", "f-donor", "f-governorate"):
        assert f'id="{field}"' in html, field
    assert ">YP</option>" in html and ">EU</option>" in html and ">1 Skills</option>" in html
    assert "Counted in Compiler, read on" in html
    assert not any(
        '"youth_youthfigures"."payload"' in q["sql"] for q in queries.captured_queries
    )  # never decoded
    assert list(placeholders(html)) == list(PARTS)
    for figure in (
        'class="kpi__label"',
        "Youth indicators",
        "By partner",
        "Compiler and eTools, side by side",
    ):
        assert figure not in html, figure


def test_the_filter_choices_read_in_the_database_are_the_payloads(world):
    """The frame's choices come from a few lists cut out of the payload in the database: they are those the whole
    payload gives (the page's choices before it loaded in parts)."""
    for year in ("2026", "2025"):
        record = services.stored_row(year)
        assert "payload" in record.get_deferred_fields()
        caches["shared"].clear()
        assert services.filter_options(record) == services.options(Figures(services.load(year).payload))


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_filters(client_viewer, world):
    html = text(client_viewer.get(PAGE))
    found = placeholder_attributes(html)
    assert list(found) == list(PARTS)
    stamp = services.fingerprint("2026")
    for name in PARTS:
        url = found[name]["hx-get"]
        assert urlsplit(url).path == PAGE
        # the latest year written out on a first load, the figures' fingerprint
        assert _query(url) == {"year": ["2026"], "as_of": [stamp], "_part": [name]}, url
        assert found[name]["data-part-group"] == "youth"
    assert (
        found["summary"]["data-part-priority"] == "high"
        and found["details"]["data-part-priority"] == "normal"
    )
    summary = html[html.index('id="part-summary"') : html.index('id="part-details"')]
    assert summary.count("skeleton__block--tile") == 5  # the five key figures
    # the room of a first load's parts, on a wide screen, a tablet or narrow window and a phone, as measured on
    # group D1's x20 parity world
    assert found["summary"]["style"] == "--part-h: 5600px; --part-h-sm: 6879px; --part-h-md: 5844px"
    assert found["details"]["style"] == "--part-h: 8309px; --part-h-sm: 9645px; --part-h-md: 8547px"
    chosen = text(
        client_viewer.get(
            PAGE, {"year": "2025", "partner": "6", "master": "10", "governorate": "22", "x": "y"}
        )
    )
    assert _query(placeholders(chosen)["details"]) == {
        "year": ["2025"],
        "master": ["10"],
        "partner": ["6"],
        "governorate": ["22"],
        "as_of": [services.fingerprint("2025")],
        "_part": ["details"],
    }
    # a year not stored: the latest one
    assert _query(placeholders(text(client_viewer.get(PAGE, {"year": "1999"})))["summary"])["year"] == [
        "2026"
    ]


@pytest.mark.shell
def test_without_figures_there_is_no_part(client_viewer, db):
    html = text(client_viewer.get(PAGE))
    assert "Compiler is not connected" in html or "No youth figures yet" in html
    assert placeholders(html) == {}
    assert client_viewer.get(PAGE + "?_part=summary", **HX).status_code == 404


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(client_viewer, world, django_assert_max_num_queries):
    raw_get(client_viewer, PAGE)  # the sidebar's navigation is kept per worker after the first page
    caches["shared"].clear()
    with django_assert_max_num_queries(SHELL_QUERIES):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and list(placeholders(text(response))) == list(PARTS)


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("name", PARTS)
def test_each_part_answers_its_wrapper(client_viewer, world, name):
    response = get_part(client_viewer, PAGE, name)
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "summary": (
            "Young people reached",
            "Youth indicators",
            "1.1 Digital skills",
            "0 confirmed, 1 suggested",
        ),
        "details": ("By partner", "Compiler and eTools, side by side", "Programme documents in Compiler"),
    }[name]
    for figure in expected:
        assert figure in html, figure
    assert not JSON_SCRIPT.findall(html)  # the page draws no chart
    assert executable(html) == []


def test_the_parts_draw_the_figures_of_the_year(client_viewer, world):
    data = get_part(client_viewer, PAGE, "summary").context["data"]
    assert (data["total"], data["female"], data["male"], data["partners"]) == (3, 2, 1, 2)
    row = data["links"][0]
    assert row["counted"] == 2 and row["reported"] == 40 and row["etools"].prp_cumulative == 60
    assert row["link"].source == "suggested" and row["etools"].url.startswith("/partner-monitoring/")
    details = text(get_part(client_viewer, PAGE, "details"))
    assert 'data-value="-38.0">-38<' in details  # Compiler's 2 against eTools' 40


def test_the_build_no_longer_computes_the_unused_chart_data(world):
    """``chart_data`` was computed and never read by any template: it is gone, and no template asks for it."""
    data = services.dashboard(services.load("2026"), services.YouthFilters(year="2026"))
    assert "chart_data" not in data and "figures" not in data and "options" not in data
    for template in TEMPLATES.rglob("*.html"):
        assert "chart_data" not in template.read_text(encoding="utf-8"), template


@pytest.mark.parametrize("name", PARTS)
def test_each_part_stays_within_its_query_ceiling(client_viewer, world, name, django_assert_max_num_queries):
    address = f"{PAGE}?year=2026&_part={name}"
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)
    other = f"{PAGE}?year=2026&_part={'details' if name == 'summary' else 'summary'}"
    with django_assert_max_num_queries(PART_QUERIES_STORED):
        response = client_viewer.get(other, **HX)
    assert response.status_code == 200


@pytest.mark.parametrize("name", PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    assert client.get(f"{PAGE}?year=2026&_part={name}", **HX).status_code == 403


def test_a_visitor_asking_for_a_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?year=2026&_part=summary", **HX)
    assert response.status_code == 200 and "Young people reached" not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


# --------------------------------------------------------------------------------------- inline when stored
def test_a_stored_build_is_drawn_in_the_frame(
    client_viewer, world, monkeypatch, django_assert_max_num_queries
):
    get_part(client_viewer, PAGE, "summary")  # the group's leader stores the build (and the filter's choices)

    def refuse(*args, **kwargs):
        raise AssertionError("computed again")

    monkeypatch.setattr(services, "dashboard", refuse)
    monkeypatch.setattr(services, "_options_payload", refuse)  # the build stored the filter's choices too
    with django_assert_max_num_queries(SHELL_QUERIES_INLINE):
        html = text(raw_get(client_viewer, PAGE))
    assert placeholders(html) == {}
    for name in PARTS:
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert "Young people reached" in html and "By partner" in html and 'id="f-partner"' in html
    other = text(raw_get(client_viewer, PAGE + "?year=2026&partner=6"))  # another filter is not stored
    assert list(placeholders(other)) == list(PARTS)


def test_a_stored_build_is_the_same_for_every_person(client, admin_user, viewer, world):
    staff, reader = _signed_in(admin_user), _signed_in(viewer)
    stored_by_staff = get_part(staff, PAGE, "summary").context["data"]
    assert get_part(reader, PAGE, "summary").context["data"] == stored_by_staff
    caches["shared"].clear()
    by_viewer = get_part(reader, PAGE, "details").context["data"]
    assert get_part(staff, PAGE, "details").context["data"] == by_viewer == stored_by_staff
    assert placeholders(text(raw_get(staff, PAGE))) == {}  # the viewer's build, drawn inline for staff


def test_two_filters_never_share_a_key(world):
    record = services.stored_row("2026")
    today = timezone.localdate()
    filters = [
        services.YouthFilters(year="2026"),
        services.YouthFilters(year="2026", partner=6),
        services.YouthFilters(year="2026", donor=6),
        services.YouthFilters(year="2026", master=6),
        services.YouthFilters(year="2026", governorate=6),
    ]
    keys = {services._cache_key(record, f, today) for f in filters}
    keys.add(services._cache_key(record, filters[0], today.replace(day=1 if today.day > 1 else 2)))
    assert len(keys) == len(filters) + 1


class _EveryStringTranslated(gettext.NullTranslations):
    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_each_language_has_its_own_build_and_one_set_of_choices(client_viewer, world, monkeypatch):
    """The build holds the breakdowns' names, translated as it is computed: it is kept per language. The filter's
    choices are Compiler's names, never translated: one value serves every language."""
    calls = Counter()
    real_dashboard, real_options = services.dashboard, services._options_payload

    def dashboard(*args, **kwargs):
        calls["build"] += 1
        return real_dashboard(*args, **kwargs)

    def options(*args, **kwargs):
        calls["options"] += 1
        return real_options(*args, **kwargs)

    monkeypatch.setattr(services, "dashboard", dashboard)
    monkeypatch.setattr(services, "_options_payload", options)
    caches["shared"].clear()
    raw_get(client_viewer, PAGE, HTTP_ACCEPT_LANGUAGE="en")
    get_part(client_viewer, PAGE, "summary", HTTP_ACCEPT_LANGUAGE="en")
    compute.reset_memo()
    get_part(client_viewer, PAGE, "details", HTTP_ACCEPT_LANGUAGE="en")
    assert calls["build"] == 1
    get_part(client_viewer, PAGE, "summary", HTTP_ACCEPT_LANGUAGE="fr")
    assert calls["build"] == 2 and calls["options"] == 1
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        built = real_dashboard(services.load("2026"), services.YouthFilters("2026"))
        labels = [b["label"] for b in built["breakdowns"]]
    finally:
        translation.deactivate()
    assert all(label.startswith("[") for label in labels)


# ------------------------------------------------------------------------------------------- fresh after an edit
def test_a_link_confirmed_with_the_admin_action_shows_at_once(client, admin_user, world):
    """Fresh after edit (STAGE3): the admin's *Confirm the selected links* action changes the link's source with an
    update that touches no time of change; the page shows it at once, though its build was stored a moment
    before."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    assert _links_tile(client) == "0 confirmed, 1 suggested"  # stored now
    link = YouthIndicatorLink.objects.get()
    response = client.post(
        reverse("admin:youth_youthindicatorlink_changelist"),
        {"action": "confirm", "_selected_action": [link.pk]},
    )
    assert response.status_code == 302
    assert _links_tile(client) == "1 confirmed, 0 suggested"


def test_a_link_corrected_in_the_admin_form_shows_at_once(client, admin_user, world):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    link = YouthIndicatorLink.objects.get()
    assert link.etools_title not in ("A corrected title",)
    before = text(get_part(client, PAGE, "details"))  # stored now
    response = client.post(
        reverse("admin:youth_youthindicatorlink_change", args=[link.pk]),
        {
            "year": link.year,
            "level": link.level,
            "youth_indicator_id": link.youth_indicator_id,
            "youth_indicator": link.youth_indicator,
            "compiler_pd_id": link.compiler_pd_id,
            "compiler_pd_code": link.compiler_pd_code,
            "pd": link.pd_id,
            "etools_key": link.etools_key,
            "etools_title": "A corrected title",
            "note": "",
        },
    )
    assert response.status_code == 302, text(response)[:2000]
    after = text(get_part(client, PAGE, "details"))
    assert "A corrected title" not in before and "A corrected title" in after


class _Fewer(FakeClient):
    """Compiler sending other figures for the year."""

    def __init__(self, years, figures):
        super().__init__(years)
        self.sent = figures

    def figures(self, year=None):
        self.asked.append(year)
        return self.sent if (year or self.years[0]) == self.sent["year"] else None


def test_a_new_read_of_compilers_figures_shows_at_once(client_viewer, world):
    def total() -> int:
        return get_part(client_viewer, PAGE, "summary").context["data"]["total"]

    assert total() == 3  # stored now
    fewer = payload("2026")
    for grouping in fewer["figures"]:
        if grouping["by"] == []:
            grouping["rows"] = [[2]]
    sync(triggered_by="test", client=_Fewer(["2026"], fewer))  # a finished sync run, new figures
    assert total() == 2


def test_the_details_part_reads_the_build_the_summary_stored_though_a_link_was_confirmed_between(
    client, admin_user, world, monkeypatch
):
    client.force_login(admin_user)
    builds = Counter()
    real = services.dashboard

    def counted(*args, **kwargs):
        builds["youth"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "dashboard", counted)
    address = placeholders(text(raw_get(client, PAGE)))
    first = client.get(address["summary"], **HX)
    assert first.status_code == 200 and builds["youth"] == 1
    YouthIndicatorLink.objects.update(source=YouthIndicatorLink.Source.CONFIRMED)  # between the two parts
    second = client.get(address["details"], **HX)
    assert second.status_code == 200 and builds["youth"] == 1
    assert second.context["data"] == first.context["data"]
    assert get_part(client, PAGE, "details").context["data"]["linked"] == 1 and builds["youth"] == 2


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    "",  # first load: the latest year
    "?year=2025",  # the previous year
    "?year=2026&partner=6",  # one partner
    "?year=2026&donor=7",  # one donor
    "?year=2026&governorate=22",  # one governorate: targets hidden
    "?year=2026&master=10",  # one master indicator
    "?year=2026&master=10&partner=5&donor=7&governorate=22",  # every filter
    "?year=2026&partner=999999",  # a partner without young people
    "?year=1999",  # a year not stored: the latest
]


@pytest.mark.parametrize("role", ["viewer", "staff"])
@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(viewer, admin_user, world, role, query):
    client = _signed_in(viewer if role == "viewer" else admin_user)
    assembled = assemble(client, PAGE + query)
    assert set(assembled.parts) == set(PARTS)
    drawn = eager(client, PAGE + query)
    assert drawn.status_code == 200
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link for link in LINK.findall(text(drawn)))
    assert "_part=" not in "".join(LINK.findall(assembled.html))
    assert placeholders(text(drawn)) == {}
    assert normalise(assembled.html) == normalise(text(drawn))
    ids = re.findall(r'\sid="([^"]+)"', assembled.html)
    assert len(ids) == len(set(ids)), "an id twice in the page"


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world):
    frame = text(client_viewer.get(PAGE))
    shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert len(shown) == len(PARTS) and len(set(shown)) == 1
    address = PAGE + html_module.unescape(shown[0])
    page = text(client_viewer.get(address))
    assert placeholders(page) == {}
    for figure in (
        "Young people reached",
        "Youth indicators",
        "By partner",
        "Programme documents in Compiler",
    ):
        assert figure in page, figure
    assert "part__skeleton" not in page and "Show the figures" not in page
    assert "_eager=1" not in "".join(LINK.findall(page))
