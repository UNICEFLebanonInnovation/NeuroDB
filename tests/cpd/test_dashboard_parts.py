"""The country programme page in parts (STAGE3 group D1, SPEC7 §3.3-3.4 for ``/country-programme/``).

The page answers its frame at once (the cycle selector, the cycle's panel with the share of the cycle elapsed and its
documents, the note on AI-suggested indicators, the staff-only admin link) with a placeholder for each of its two
parts: ``summary`` (the key figures, the status donut with its JSON ``cpd-summary-data``, the outcomes at a glance),
the group's leader, and ``outcomes`` (each outcome with its outputs and indicators; the ``#outcome-<id>`` anchors).
Both read one build, kept in the cache every worker shares. These tests check the frame without any figure work,
each part, the part queries, that the page put together from its parts is the page drawn at once (``_eager=1``),
that a stored build never shows one person's view to another, that an edit in the admin shows at once, and the
quick win of the stage: each Makani or Dirasa payload is read once per call, however many links name it.

The world: the cycle of tests/cpd/test_cpd.py (2026-2028, two indicators under outcome 1 and output 1.1, an eTools
programme document reporting 6,000 children on the output), with a value typed for the rate, the children enrolled
linked to the PD's indicator (confirmed), to an ActivityInfo master indicator (500), to Makani twice and Dirasa twice
(30 and 12 children), to Compiler's youth figures (3) and to Makani again in 2028 (suggested, not counted), an
AI-suggested indicator, a CPD document, and a previous cycle with one indicator.
"""

# ruff: noqa: F811 (the cycle fixtures come from test_cpd)
from __future__ import annotations

import datetime
import gettext
import html as html_module
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import caches
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from neurodb.accounts.models import User
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.cpd import services
from neurodb.cpd import views as cpd_views
from neurodb.cpd.models import CountryProgramme, Indicator, Link, Milestone, Origin, Outcome, Output, Value
from neurodb.datamart import monitoring
from neurodb.donors.models import DonorAccount
from neurodb.education.models import EducationFigures
from neurodb.indicators.models import IndicatorNew, MasterSubIndicator
from neurodb.youth.models import YouthFigures
from tests.cpd.test_cpd import cycle, framework, intervention, media, pdf  # noqa: F401 - fixtures
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
from tests.youth.test_youth import payload as youth_payload

pytestmark = pytest.mark.django_db

PAGE = "/country-programme/"  # cpd:dashboard
PARTS = ("summary", "outcomes")  # the frame's placeholders, in page order; "summary" leads the group
SUMMARY_JSON = "cpd-summary-data"  # the one chart JSON of the page, in the summary part
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame: the request's floor
# (session, user, donor check, user role, the sidebar's sections and years), the cycles, the cycle's indicators and
# those AI-suggested (one query), its documents, what staff entered by hand (one query over the rows themselves),
# the cache stamp (last sync run, indicator flags); drawn inline the same, both parts reading the build the frame
# found stored. Each part with nothing stored computes the whole build (the linked sources of each kind, the
# programme documents of the cycle, their partner reporting); the group's second part reading the stored build is
# the request's floor and the key's reads.
SHELL_QUERIES = 13
SHELL_QUERIES_INLINE = 13
PART_QUERIES = 36
PART_QUERIES_STORED = 10


# ------------------------------------------------------------------------------------------------- the world
def _education(programme: str, period: str, children: int) -> EducationFigures:
    """Stored Makani or Dirasa figures enrolling ``children`` (a cube of format 2, as Compiler sends it)."""
    return EducationFigures.objects.create(
        programme=programme,
        year=period,
        fetched_at=timezone.now(),
        payload={
            "format": 2,
            "cubes": {
                "enrolment": {"dims": ["partner"], "measures": ["registrations"], "rows": [[1, children]]}
            },
        },
    )


@pytest.fixture
def world(framework, intervention, pdf, hierarchy):
    """See the module docstring."""
    reached = framework.reached
    Value.objects.create(indicator=framework.rate, year=2026, value=38, source="MEHE")
    Milestone.objects.create(indicator=reached, year=2027, value=10000)
    row = monitoring.indicators(monitoring.Filters(pd_ids=[intervention.pk], scope="all", year=2027))[0]
    Link.objects.create(
        indicator=reached,
        kind="etools",
        year=2027,
        pd=intervention,
        etools_key=row.key,
        label="NFE enrolment",
    )
    Link.objects.create(
        indicator=reached, kind="activityinfo", year=2026, master=hierarchy["master"], label="Case management"
    )
    _education("mscc", "2026", 30)
    _education("bridging", "Bridging 2025-2026", 12)
    for year in (2026, 2027):  # two links on each payload: each is read once per call
        Link.objects.create(
            indicator=reached,
            kind="education",
            year=year,
            education_programme="mscc",
            source_period="2026",
            label="Makani enrolment",
        )
        Link.objects.create(
            indicator=reached,
            kind="education",
            year=year,
            education_programme="bridging",
            source_period="Bridging 2025-2026",
            label="Dirasa enrolment",
        )
    YouthFigures.objects.create(year="2026", fetched_at=timezone.now(), payload=youth_payload("2026"))
    Link.objects.create(
        indicator=reached,
        kind="youth",
        year=2026,
        youth_level="master",
        youth_id=10,
        source_period="2026",
        label="Young people with skills",
    )
    Link.objects.create(  # suggested, not confirmed: never counted
        indicator=reached,
        kind="education",
        year=2028,
        education_programme="mscc",
        source_period="2026",
        label="Suggested",
        confirmed=False,
    )
    Indicator.objects.create(
        output=framework.output, code="1.1.2", title="Teachers trained", target=500, origin=Origin.AI
    )
    previous = CountryProgramme.objects.create(name="Lebanon CP 2023-2025", start_year=2023, end_year=2025)
    old = Outcome.objects.create(programme=previous, code="9", title="Older outcome")
    Indicator.objects.create(outcome=old, code="9.a", title="Older indicator", target=10)
    framework.previous = previous
    framework.pd = intervention
    framework.document = pdf
    return framework


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _json_blocks(html: str) -> dict[str, object]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a chart JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the page raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    for name in ("dashboard", "link_values", "progress", "interventions", "_education_values"):
        monkeypatch.setattr(services, name, refuse)
    monkeypatch.setattr(monitoring, "indicators", refuse)
    monkeypatch.setattr(cpd_views, "_chart_data", refuse)


def _counted(client, url: str = PAGE) -> int:
    """The children enrolled, as the outcomes part shows them (the indicator 1.1.1's value)."""
    data = get_part(client, url, "outcomes").context["data"]
    rows = [p for o in data["outcomes"] for r in o["outputs"] for p in r["indicators"]]
    return next(p for p in rows if p.indicator.code == "1.1.1").value


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("cpd:dashboard") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client, admin_user, world, monkeypatch):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    client.force_login(admin_user)
    response = client.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Country programme" in html and 'id="f-cycle"' in html  # the title, the cycle selector
    assert "Lebanon CP 2026-2028" in html and "2026–2028 · 3 years" in html  # the cycle's panel
    assert "of the cycle elapsed" in html
    assert reverse("cpd:document", args=[world.document.pk]) in html  # its document (review R1-cross-1)
    assert reverse("admin:cpd_countryprogramme_change", args=[world.cycle.pk]) in html  # staff only
    assert "1 indicator was AI-suggested from the CPD" in html  # the note
    assert list(placeholders(html)) == list(PARTS)
    assert f'id="{SUMMARY_JSON}"' not in html and 'id="chart-data"' not in html
    for figure in ("Where the CPD indicators stand", "CPD indicators", "Outcome 1", "Out-of-school rate"):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_cycle(client_viewer, world):
    html = text(client_viewer.get(PAGE))
    found = placeholder_attributes(html)
    assert list(found) == list(PARTS)
    stamp = services.fingerprint(world.cycle)  # the figures the frame was drawn with
    for name in PARTS:
        url = found[name]["hx-get"]
        assert urlsplit(url).path == PAGE
        # the current cycle written out on a first load, the figures' fingerprint
        assert _query(url) == {"cycle": [str(world.cycle.pk)], "as_of": [stamp], "_part": [name]}, url
        assert found[name]["data-part-group"] == "cpd"
    assert (
        found["summary"]["data-part-priority"] == "high"
        and found["outcomes"]["data-part-priority"] == "normal"
    )
    assert (
        found["summary"]["data-part-needs"] == "plotly"
    )  # the donut's library loads while the server computes
    assert found["outcomes"]["data-part-anchors"] == "outcome-"
    # the room of what each part holds, on a wide screen, a tablet or narrow window (the summary's panels stacked)
    # and a phone, as measured on group D1's x20 parity world
    assert found["summary"]["style"] == "--part-h: 565px; --part-h-sm: 1298px; --part-h-md: 876px"
    assert found["outcomes"]["style"] == "--part-h: 6425px; --part-h-sm: 8425px; --part-h-md: 7070px"
    summary = html[html.index('id="part-summary"') : html.index('id="part-outcomes"')]
    assert summary.count("skeleton__block--tile") == 6  # the six key figures
    # another cycle, and a cycle that does not exist (the current one)
    other = text(client_viewer.get(PAGE, {"cycle": world.previous.pk}))
    assert _query(placeholders(other)["outcomes"])["cycle"] == [str(world.previous.pk)]
    missing = text(client_viewer.get(PAGE, {"cycle": "999999", "x": "1"}))
    assert _query(placeholders(missing)["summary"])["cycle"] == [str(world.cycle.pk)]
    assert "x" not in _query(placeholders(missing)["summary"])  # a key the page does not read does not travel


@pytest.mark.shell
def test_without_a_cycle_or_indicators_there_is_no_part(client_viewer, db):
    html = text(client_viewer.get(PAGE))
    assert "No country programme cycle yet" in html and placeholders(html) == {}
    assert client_viewer.get(PAGE + "?_part=summary", **HX).status_code == 404
    empty = CountryProgramme.objects.create(name="Empty", start_year=2026, end_year=2028, current=True)
    html = text(client_viewer.get(PAGE))
    assert "No results framework yet" in html and placeholders(html) == {}
    assert client_viewer.get(f"{PAGE}?cycle={empty.pk}&_part=summary", **HX).status_code == 404


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
    response = get_part(client_viewer, PAGE, name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "summary": (
            "CPD indicators",
            "Where the CPD indicators stand",
            f'href="#outcome-{world.outcome.pk}"',
        ),
        "outcomes": (f'id="outcome-{world.outcome.pk}"', "Out-of-school rate", "LEB/PCA2027001"),
    }[name]
    for figure in expected:
        assert figure in html, figure
    assert (f'id="{SUMMARY_JSON}"' in html) == (name == "summary")  # the one chart JSON, in its own part
    charts = re.findall(r"<div[^>]*data-chart=[^>]*>", html)
    assert all(f'data-source="{SUMMARY_JSON}"' in chart for chart in charts) and len(charts) == (
        name == "summary"
    )
    assert executable(html) == []  # nothing that would run once swapped in (SPEC7 §1.3 rule 3)


def test_the_parts_draw_the_figures_of_the_cycle(client_viewer, world):
    """Both drawings of the page share the part templates, so their equivalence cannot catch a wrong figure: the
    figures are checked here. The children enrolled in 2027 add the PD's 6,000 and Makani's 30 and Dirasa's 12
    (each payload linked twice, read once); 2026 adds ActivityInfo's 500, Makani, Dirasa and youth's 3; the
    suggested link of 2028 counts nothing."""
    summary = get_part(client_viewer, PAGE, "summary")
    data = summary.context["data"]
    assert data["indicator_total"] == 3 and data["pd_total"] == 1 and data["unreviewed"] == 1
    reached = next(
        p
        for o in data["outcomes"]
        for r in o["outputs"]
        for p in r["indicators"]
        if p.indicator.code == "1.1.1"
    )
    assert reached.years[2026]["value"] == 500 + 30 + 12 + 3
    assert reached.years[2027]["value"] == 6000 + 30 + 12
    assert 2028 not in reached.years and reached.value == 545 + 6042
    chart = _json_blocks(text(summary))[SUMMARY_JSON]
    assert sum(chart["status_counts"].values()) == 3 and chart["labels"]["off_track"] == "Off track"
    outcomes = text(get_part(client_viewer, PAGE, "outcomes"))
    assert (
        "6,587" in outcomes and "MEHE" not in outcomes
    )  # the value, and the source only on the indicator page


@pytest.mark.parametrize("name", PARTS)
def test_each_part_stays_within_its_query_ceiling(client_viewer, world, name, django_assert_max_num_queries):
    address = f"{PAGE}?cycle={world.cycle.pk}&_part={name}"
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)
    other = f"{PAGE}?cycle={world.cycle.pk}&_part={'outcomes' if name == 'summary' else 'summary'}"
    with django_assert_max_num_queries(PART_QUERIES_STORED):  # the group's second part only reads the build
        response = client_viewer.get(other, **HX)
    assert response.status_code == 200


@pytest.mark.parametrize("name", PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    response = client.get(f"{PAGE}?cycle={world.cycle.pk}&_part={name}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?cycle={world.cycle.pk}&_part=summary", **HX)
    assert response.status_code == 200 and "CPD indicators" not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


def test_every_outcome_anchor_lives_in_the_part_that_declares_it(client_viewer, world):
    """The summary's outcome links and the knowledge graph's ``?cycle=N#outcome-<id>`` links (graph/builders.py)
    point at ids inside ``outcomes``, whose placeholder declares the prefix (``anchors="outcome-"``): a link asks
    for that part first and the page scrolls to the outcome once it is in (parts.js)."""
    frame = text(raw_get(client_viewer, PAGE))
    assert placeholder_attributes(frame)["outcomes"]["data-part-anchors"] == "outcome-"
    summary = text(get_part(client_viewer, PAGE, "summary"))
    outcomes = text(get_part(client_viewer, PAGE, "outcomes"))
    targets = re.findall(r'href="#(outcome-\d+)"', summary)
    assert targets == [f"outcome-{world.outcome.pk}"]
    for target in targets:
        assert f'id="{target}"' in outcomes and f'id="{target}"' not in frame + summary


# --------------------------------------------------------------------------------------- inline when stored
def test_a_stored_build_is_drawn_in_the_frame(
    client_viewer, world, monkeypatch, django_assert_max_num_queries
):
    """Inline when ready: once the group's leader has stored the build, the frame draws both parts itself, so a
    warm page is one request."""
    get_part(client_viewer, PAGE, "summary")  # the group leader computes and stores the build

    def refuse(*args, **kwargs):
        raise AssertionError("the stored build was computed again")

    monkeypatch.setattr(services, "dashboard", refuse)
    with django_assert_max_num_queries(SHELL_QUERIES_INLINE):
        html = text(raw_get(client_viewer, PAGE))
    assert placeholders(html) == {}
    for name in PARTS:
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert "Where the CPD indicators stand" in html and f'id="{SUMMARY_JSON}"' in html
    # another cycle is not stored: its figures are asked for
    other = text(raw_get(client_viewer, f"{PAGE}?cycle={world.previous.pk}"))
    assert list(placeholders(other)) == list(PARTS)


def test_a_stored_build_never_shows_one_persons_view_to_another(client, admin_user, viewer, world):
    """The build holds nothing about the person asking: a staff member's visit stores it and a viewer reads it,
    and the other way round; the admin link, the one thing of the page that depends on the person, is the
    frame's, drawn for each person."""
    staff, reader = _signed_in(admin_user), _signed_in(viewer)
    edit = reverse("admin:cpd_countryprogramme_change", args=[world.cycle.pk])
    stored_by_staff = get_part(staff, PAGE, "summary").context["data"]
    read_by_viewer = get_part(reader, PAGE, "summary").context["data"]
    assert read_by_viewer == stored_by_staff
    frame = text(raw_get(reader, PAGE))  # both parts drawn inline from the build a staff member stored
    assert placeholders(frame) == {} and edit not in frame
    assert edit in text(raw_get(staff, PAGE))
    caches["shared"].clear()
    by_viewer = get_part(reader, PAGE, "outcomes").context["data"]  # stored by the viewer's request this time
    assert get_part(staff, PAGE, "outcomes").context["data"] == by_viewer == stored_by_staff
    for name in PARTS:  # no part draws anything that depends on the person
        assert edit not in text(get_part(staff, PAGE, name)) and "/manage/" not in text(
            get_part(staff, PAGE, name)
        )


def test_two_cycles_or_two_days_never_share_a_key(world):
    today = datetime.date(2026, 10, 13)
    keys = {
        services._cache_key(world.cycle, today),
        services._cache_key(world.previous, today),
        services._cache_key(world.cycle, today + datetime.timedelta(days=1)),
    }
    assert len(keys) == 3


class _EveryStringTranslated(gettext.NullTranslations):
    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_each_language_has_its_own_build(client_viewer, world, monkeypatch):
    """A build holds the linked sources' kinds, translated as it is computed: it is kept per language."""
    calls = Counter()
    real = services.dashboard

    def counted(*args, **kwargs):
        calls["build"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "dashboard", counted)
    get_part(client_viewer, PAGE, "outcomes", HTTP_ACCEPT_LANGUAGE="en")
    compute.reset_memo()
    get_part(client_viewer, PAGE, "summary", HTTP_ACCEPT_LANGUAGE="en")
    assert calls["build"] == 1
    get_part(client_viewer, PAGE, "summary", HTTP_ACCEPT_LANGUAGE="fr")
    assert calls["build"] == 2, "a build stored in one language answered another"
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        kinds = {
            part["kind"]
            for o in real(world.cycle)["outcomes"]
            for r in o["outputs"]
            for p in r["indicators"]
            for row in p.years.values()
            for part in row["parts"]
        }
    finally:
        translation.deactivate()
    assert kinds and all(kind.startswith("[") for kind in kinds)  # translated: the language is in the key


# ------------------------------------------------------------------------------------------- fresh after an edit
def _indicator_form(indicator: Indicator, **changes) -> dict[str, str]:
    """The indicator's admin change form as the page posts it, its inline rows untouched unless added."""
    data = {
        "programme": str(indicator.programme_id),
        "outcome": str(indicator.outcome_id or ""),
        "output": str(indicator.output_id or ""),
        "code": indicator.code,
        "title": indicator.title,
        "unit": indicator.unit,
        "direction": indicator.direction,
        "accumulate": indicator.accumulate,
        "baseline": "" if indicator.baseline is None else str(indicator.baseline),
        "baseline_year": "" if indicator.baseline_year is None else str(indicator.baseline_year),
        "target": "" if indicator.target is None else str(indicator.target),
        "means_of_verification": indicator.means_of_verification,
        "origin": indicator.origin,
    }
    for inline in ("milestones", "values", "links"):
        data[f"{inline}-TOTAL_FORMS"] = "0"
        data[f"{inline}-INITIAL_FORMS"] = "0"
    data.update(changes)
    return data


def test_a_value_typed_in_the_admin_shows_at_once(client, admin_user, world):
    """Fresh after edit (STAGE3): a value typed in the indicator's admin replaces the linked sources of its year;
    the page shows it at once, though its build was stored a moment before."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    before = _counted(client)  # stored now
    reached = world.reached
    response = client.post(
        reverse("admin:cpd_indicator_change", args=[reached.pk]),
        _indicator_form(
            reached,
            **{
                "values-TOTAL_FORMS": "1",
                "values-0-year": "2027",
                "values-0-value": "9000",
                "values-0-source": "Annual report",
                "values-0-note": "",
            },
        ),
    )
    assert response.status_code == 302, text(response)[:3000]
    assert _counted(client) == before - 6042 + 9000


def test_a_target_changed_in_the_admin_shows_at_once(client, admin_user, world):
    """An indicator's target is edited in place: its table keeps no time of change, so the key reads the rows
    themselves. The rate's status changes at once."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)

    def rate() -> tuple:
        data = get_part(client, PAGE, "summary").context["data"]
        row = data["outcomes"][0]["indicators"][0]
        return row.indicator.target, row.status

    target, _status = rate()  # stored now
    assert target == 20
    response = client.post(
        reverse("admin:cpd_indicator_change", args=[world.rate.pk]), _indicator_form(world.rate, target="39")
    )
    assert response.status_code == 302, text(response)[:3000]
    assert rate()[0] == 39


def test_a_suggested_link_confirmed_in_the_admin_shows_at_once(client, admin_user, world):
    """A suggested link confirmed in the indicator's admin (the review of what the AI or a colleague suggested)
    counts at once."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    before = _counted(client)  # stored now: the suggested link of 2028 counts nothing
    suggested = Link.objects.get(confirmed=False)
    response = client.post(
        reverse("admin:cpd_indicator_change", args=[world.reached.pk]),
        _indicator_form(
            world.reached,
            **{
                "links-TOTAL_FORMS": "1",
                "links-INITIAL_FORMS": "1",
                "links-0-id": str(suggested.pk),
                "links-0-indicator": str(world.reached.pk),
                "links-0-source": "edu:mscc:2026",
                "links-0-year": "2028",
                "links-0-confirmed": "on",
            },
        ),
    )
    assert response.status_code == 302, text(response)[:3000]
    suggested.refresh_from_db()
    assert suggested.confirmed
    assert _counted(client) == before + 30  # 2028 now counts Makani's 30


def test_every_hand_entered_table_moves_the_key(world, hierarchy):
    """What staff enter by hand that the figures read: each edit, even in place, moves the build's key (the
    cycle, an outcome, an output, an indicator, a milestone, a value, a link, and how the admin set up a linked
    ActivityInfo master indicator)."""
    edits = [
        lambda: CountryProgramme.objects.filter(pk=world.cycle.pk).update(etools_name="Another CP"),
        lambda: Outcome.objects.filter(pk=world.outcome.pk).update(title="Children learn more"),
        lambda: Output.objects.filter(pk=world.output.pk).update(etools_output="1.1 Access"),
        lambda: Indicator.objects.filter(pk=world.reached.pk).update(baseline=5),
        lambda: Milestone.objects.filter(indicator=world.reached).update(value=11000),
        lambda: Value.objects.filter(indicator=world.rate).update(value=37),
        lambda: Link.objects.filter(kind="activityinfo").update(year=2027),
        lambda: MasterSubIndicator.objects.filter(master=hierarchy["master"]).update(effect="NUMERATOR"),
        lambda: (
            hierarchy["master"].__class__.objects.filter(pk=hierarchy["master"].pk).update(name="Renamed")
        ),
        # an indicator a linked sub-indicator lists, edited in the admin: the ActivityInfo field its figure reads
        # (review R1-D1-1)
        lambda: IndicatorNew.objects.filter(pk=hierarchy["leaves"][0].pk).update(ai_indicator="i_other"),
    ]
    seen = {services.hand_entered(world.cycle)}
    for edit in edits:
        edit()
        compute.reset_memo()
        seen.add(services.hand_entered(world.cycle))
    assert len(seen) == len(edits) + 1
    # nothing of another cycle moves this one's key
    compute.reset_memo()
    before = services.hand_entered(world.cycle)
    Indicator.objects.filter(programme=world.previous).update(target=99)
    compute.reset_memo()
    assert services.hand_entered(world.cycle) == before


def test_a_finished_sync_shows_at_once(client_viewer, world):
    """A Compiler read that brings new Makani figures (a finished sync run): the page shows them at once."""
    before = _counted(client_viewer)  # stored now
    EducationFigures.objects.filter(programme="mscc").update(
        fetched_at=timezone.now(),
        payload={
            "format": 2,
            "cubes": {"enrolment": {"dims": ["partner"], "measures": ["registrations"], "rows": [[1, 80]]}},
        },
    )
    run = SyncRun.objects.create(job=SyncRun.Job.COMPILER_EDUCATION, target="compiler", status="running")
    run.finish(SyncRun.Status.SUCCEEDED)
    assert _counted(client_viewer) == before + 2 * 50  # Makani counts in 2026 and 2027


def test_the_outcomes_part_reads_the_build_the_summary_stored_though_an_edit_came_between(
    client, admin_user, world, monkeypatch
):
    """The browser asks for ``outcomes`` once ``summary`` is in. An edit in between moves the build's key:
    ``outcomes`` still reads the build ``summary`` stored (the fingerprint its address carries, ``as_of``), so the
    page never shows two moments side by side; the next page shows the edit."""
    client.force_login(admin_user)
    builds = Counter()
    real = services.dashboard

    def counted(*args, **kwargs):
        builds["cpd"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "dashboard", counted)
    address = placeholders(text(raw_get(client, PAGE)))
    first = client.get(address["summary"], **HX)
    assert first.status_code == 200 and builds["cpd"] == 1
    Value.objects.filter(indicator=world.rate).update(value=21)  # an edit between the two parts
    second = client.get(address["outcomes"], **HX)
    assert second.status_code == 200 and builds["cpd"] == 1
    assert second.context["data"] == first.context["data"]
    assert get_part(client, PAGE, "outcomes").context["data"] != first.context["data"]  # the next page
    assert builds["cpd"] == 2


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = ["", "?cycle={current}", "?cycle={previous}", "?cycle=999999"]


@pytest.mark.parametrize("role", ["viewer", "staff"])
@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(viewer, admin_user, world, role, query):
    client = _signed_in(viewer if role == "viewer" else admin_user)
    url = PAGE + query.format(current=world.cycle.pk, previous=world.previous.pk)
    assembled = assemble(client, url)
    assert set(assembled.parts) == set(PARTS)
    drawn = eager(client, url)
    assert drawn.status_code == 200
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link for link in LINK.findall(text(drawn)))
    assert "_part=" not in "".join(LINK.findall(assembled.html))
    assert placeholders(text(drawn)) == {}
    assert normalise(assembled.html) == normalise(text(drawn))
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone and [key for key in together if key.startswith("cpd-")] == [SUMMARY_JSON]
    summary = _json_blocks(text(assembled.parts["summary"]))
    assert list(summary) == [SUMMARY_JSON] and summary[SUMMARY_JSON] == alone[SUMMARY_JSON]
    assert summary[SUMMARY_JSON]["labels"]["not_reported"] == "No data yet"
    assert not _json_blocks(text(assembled.parts["outcomes"]))


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world):
    frame = text(client_viewer.get(PAGE))
    shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert len(shown) == len(PARTS) and len(set(shown)) == 1
    address = PAGE + html_module.unescape(shown[0])
    assert "_eager=1" in address
    page = text(client_viewer.get(address))
    assert placeholders(page) == {}
    for figure in (
        "CPD indicators",
        "Where the CPD indicators stand",
        "Out-of-school rate",
        f'id="{SUMMARY_JSON}"',
    ):
        assert figure in page, figure
    assert "part__skeleton" not in page and "Show the figures" not in page
    assert "_eager=1" not in "".join(LINK.findall(page))


# ------------------------------------------------------------------------------------------- the quick win
def _payload_reads(queries) -> int:
    return sum(
        '"education_educationfigures"."payload"' in q["sql"] and "SELECT" in q["sql"]
        for q in queries.captured_queries
    )


def test_each_education_payload_is_read_once_per_call_and_kept_per_worker(world):
    """Eight links name two stored payloads (Makani 2026 and Dirasa's round, twice each per year): each payload is
    read once (it was read once per link), the totals are those of ``education_total``, and a second call reads
    none while the figures are not fetched again; a new fetch is read again."""
    caches["default"].clear()
    links = list(Link.objects.filter(kind="education", confirmed=True))
    assert len(links) == 4
    with CaptureQueriesContext(connection) as first:
        values = services.link_values(links)
    assert _payload_reads(first) == 2
    expected = {
        link.pk: float(
            services.education_total(
                EducationFigures.objects.get(
                    programme=link.education_programme, year=link.source_period
                ).payload
            )
        )
        for link in links
    }
    assert values == expected and sorted(set(values.values())) == [12.0, 30.0]
    with CaptureQueriesContext(connection) as again:
        assert services.link_values(links) == values
    assert _payload_reads(again) == 0
    EducationFigures.objects.filter(programme="mscc").update(
        fetched_at=timezone.now() + datetime.timedelta(minutes=1),
        payload={"cubes": {"enrolment": {"dims": ["p"], "measures": ["registrations"], "rows": [[1, 31]]}}},
    )
    with CaptureQueriesContext(connection) as fetched:
        newer = services.link_values(links)
    assert _payload_reads(fetched) == 1 and sorted(set(newer.values())) == [12.0, 31.0]
    # a link to figures not stored has no value, as before
    missing = Link(pk=-1, kind="education", education_programme="mscc", source_period="1999")
    assert services.link_values([missing]) == {-1: None}
