"""Pages that load in parts: the mechanism (SPEC7 §1.1-1.3, §3.2; STAGE0 D2, D3, D6, D9).

A small page of its own, served by this module's URLconf beside the site's and drawn from the templates
below, goes through ``render_page``, so these tests depend on no converted page. Its shell resolves
``year`` (2026 by default) and ``section`` (``Own`` on a first load, as a user's own section).
"""

import datetime
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.template import Context, Template, TemplateSyntaxError
from django.test import Client, RequestFactory
from django.urls import path, reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from config import urls as site_urls
from neurodb.core import compute
from neurodb.web import parts
from neurodb.web.parts import Part, first_load, render_page
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

pytestmark = pytest.mark.urls(__name__)

PAGE = "/parts-test/"
STORED_KEY = "parts-test-stored"  # the "stored" part's figures, through compute.once
SLOW_KEY = "parts-test-slow"  # the "slow" part's figures, which another request is computing

TEMPLATES = {
    "parts_test/page.html": (
        '{% load parts %}<!doctype html><html lang="en"><head><title>{{ title }}</title></head><body>'
        '<h1 id="page-title">{{ title }}</h1>'
        '<a id="self-link" href="{{ request.get_full_path }}">This page</a>'
        '<div id="parts-results">{% include "parts_test/results.html" %}</div>'
        '{% part "for-you" kind="card" priority="high" url="/parts-test/card/" swap="innerHTML" '
        'element_id="for-you" %}'
        "</body></html>"
    ),
    "parts_test/results.html": (
        '{% load parts %}<h2 id="kpis-title">Key figures</h2>'
        '{% part "kpis" kind="tiles" priority="high" group="figures" rows=2 labelledby="kpis-title" %}'
        '{% part "table" kind="table" group="figures" height="420px" %}'
        '{% part "stored" kind="card" %}'
        '{% part "ready" kind="text" priority="low" %}'
        '{% part "form" kind="card" %}'
    ),
    "parts_test/kpis.html": (
        '<div class="kpi-grid">{% for t in tiles %}<div class="kpi">Tile {{ t }}</div>{% endfor %}</div>'
        '<p id="kpis-scope">Year {{ year }}, section {{ section|default:"every section" }}</p>'
        '<a id="kpis-sign-in" href="/accounts/login/?next={{ request.get_full_path|urlencode }}">Sign in</a>'
    ),
    "parts_test/table.html": (
        "{% load ui %}<table>{% for n in page_obj %}<tr><td>Row {{ n }}</td></tr>{% endfor %}</table>"
        '<a id="table-sort" href="?{% qs_replace sort="name" %}">Sort</a>'
        '{% include "components/pagination.html" with part="table" %}'
        "{% load parts %}<details><summary>More</summary>"
        '{% part "more" kind="list" lazy="strict" anchors="more-" %}</details>'
    ),
    "parts_test/more.html": '<p id="more-1">{{ more }}</p>',
    "parts_test/stored.html": "<p>Stored figure {{ value }}</p>",
    "parts_test/ready.html": "<p>Ready figure {{ ready_value }}</p>",
    "parts_test/form.html": (
        '<form method="post" action="/parts-test/post/">{% csrf_token %}<button>Save</button></form>'
    ),
    "parts_test/slow.html": "<p>Slow figure {{ value }}</p>",
    "parts_test/refused.html": (
        '{% load parts %}<h1>Refused</h1>{% part "kpis" kind="tiles" %}'
        '{% part "denied" url="/parts-test/denied/" %}'
    ),
    "parts_test/denied.html": "<p>{{ secret }}</p>",
    "parts_test/scripted.html": '{% load parts %}<h1>Scripted</h1>{% part "card" url="/parts-test/card/scripted/" %}',
    "parts_test/scripted_card.html": '<section>{% include "parts_test/_scripted.html" %}</section>',
    "parts_test/_scripted.html": "<p>Card</p><script>window.ran = 1;</script>",
}

SEEN: list[dict] = []  # what each request of the page saw
CALLS: Counter = Counter()  # how often each part's figures (and the shell's context) were computed
READY = {"ready": False}  # whether the "ready" part says its figures are stored


def _figures(name, **values):
    def computed():
        CALLS[name] += 1
        return values

    return computed


def _page(request, template="parts_test/page.html"):
    SEEN.append(
        {
            "GET": request.GET.urlencode(),
            "path": request.get_full_path(),
            "first_load": first_load(request),
            "part": getattr(request, "part", None),
            "eager": getattr(request, "parts_eager", None),
        }
    )
    year = request.GET.get("year") or "2026"
    if "section" in request.GET:
        section = request.GET["section"]
    else:
        section = "Own" if first_load(request) else ""
    resolved = {"year": year, "section": section}
    if request.GET.get("page"):
        resolved["page"] = request.GET["page"]
    rows = Paginator(list(range(1, 31)), 10).get_page(request.GET.get("page"))

    def shell():
        CALLS["shell"] += 1
        return {"title": "Parts test page"}

    def stored():
        return {"value": compute.once(STORED_KEY, _figures("stored", total=42), 60)["total"]}

    def slow():
        return {"value": compute.once(SLOW_KEY, _figures("slow", total=7), 60, lang_neutral=True)["total"]}

    def pending():
        raise compute.Pending("parts-test-pending")

    return render_page(
        request,
        template,
        scope={"year": year, "section": section},
        shell=shell,
        parts={
            "kpis": Part("parts_test/kpis.html", _figures("kpis", tiles=[3, 5, 8])),
            "table": Part("parts_test/table.html", lambda: {**_figures("table")(), "page_obj": rows}),
            "more": Part("parts_test/more.html", _figures("more", more="Folded findings")),
            "stored": Part("parts_test/stored.html", stored, peek=STORED_KEY),
            "ready": Part(
                "parts_test/ready.html", _figures("ready", ready_value=9), ready=lambda: READY["ready"]
            ),
            "form": Part("parts_test/form.html", _figures("form")),
            "slow": Part("parts_test/slow.html", slow),
            "pending": Part("parts_test/slow.html", pending),
            "data": Part("", _figures("data", rows=[1, 2]), json=True),
        },
        query=urlencode(resolved),
        results_template="parts_test/results.html",
        results_target="parts-results",
    )


@login_not_required
def _public_page(request):
    return _page(request)


@login_not_required
@parts.public
def _open_page(request):
    return _page(request)


@login_not_required
def _feed(request):
    return HttpResponse("a key of its own", status=403)


_feed.powerbi_feed = True


def _refused_page(request):
    return _page(request, "parts_test/refused.html")


def _denied(request):
    return render(request, "parts_test/denied.html", {"secret": "not for this person"}, status=403)


def _card(request):
    return HttpResponse("<p>Your card</p>")


def _scripted_page(request):
    return render_page(
        request, "parts_test/scripted.html", scope={}, shell=dict, parts={}, query=request.GET.urlencode()
    )


def _scripted_card(request):
    return render(request, "parts_test/scripted_card.html")


@require_POST
def _post(request):
    return HttpResponse("saved")


urlpatterns = [
    path("parts-test/", _page, name="parts_test_page"),
    path("parts-test/public/", _public_page, name="parts_test_public"),
    path("parts-test/open/", _open_page, name="parts_test_open"),
    path("parts-test/feed/", _feed, name="parts_test_feed"),
    path("parts-test/card/", _card, name="parts_test_card"),
    path("parts-test/refused/", _refused_page, name="parts_test_refused"),
    path("parts-test/scripted/", _scripted_page, name="parts_test_scripted"),
    path("parts-test/card/scripted/", _scripted_card, name="parts_test_scripted_card"),
    path("parts-test/denied/", _denied, name="parts_test_denied"),
    path("parts-test/post/", _post, name="parts_test_post"),
    *site_urls.urlpatterns,
]


@pytest.fixture(autouse=True)
def _test_page(settings):
    """This module's templates in front of the site's, and a fresh record of what the page did."""
    engine = {**settings.TEMPLATES[0], "APP_DIRS": False}
    engine["OPTIONS"] = {
        **engine["OPTIONS"],
        "loaders": [
            ("django.template.loaders.locmem.Loader", TEMPLATES),
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    }
    settings.TEMPLATES = [engine]
    SEEN.clear()
    CALLS.clear()
    READY["ready"] = False
    yield
    READY["ready"] = False


def _hx(**extra):
    return {**HX, **extra}


def _part(client, name, query="year=2026&section=Own", **extra):
    url = f"{PAGE}?{query}&_part={name}" if query else f"{PAGE}?_part={name}"
    return raw_get(client, url, **_hx(**extra))


def _next(response) -> str:
    return parse_qs(urlsplit(response["HX-Redirect"]).query)["next"][0]


# ------------------------------------------------------------------------------- the request's address
@pytest.mark.shell
def test_part_and_eager_never_reach_the_view_or_its_links(client_viewer):
    response = raw_get(client_viewer, f"{PAGE}?year=2025&_part=kpis&_eager=1&section=CP", **HX)
    assert response.status_code == 200 and response["X-NeuroDB-Part"] == "kpis"
    seen = SEEN[-1]
    assert seen["GET"] == "year=2025&section=CP" and seen["path"] == f"{PAGE}?year=2025&section=CP"
    assert seen["part"] == "kpis" and seen["eager"] is True
    # the part's own links are built from that clean request
    html = text(response)
    assert 'href="/accounts/login/?next=/parts-test/%3Fyear%3D2025%26section%3DCP"' in html

    raw_get(client_viewer, f"{PAGE}?_eager=1&year=2024")
    assert SEEN[-1]["GET"] == "year=2024" and SEEN[-1]["path"] == f"{PAGE}?year=2024"
    assert SEEN[-1]["eager"] is True and SEEN[-1]["part"] is None


@pytest.mark.shell
def test_a_part_without_htmx_or_from_history_is_the_whole_page(client_viewer):
    plain = raw_get(client_viewer, f"{PAGE}?_part=kpis")
    restore = raw_get(client_viewer, f"{PAGE}?_part=kpis", **_hx(HTTP_HX_HISTORY_RESTORE_REQUEST="true"))
    for response in (plain, restore):
        html = text(response)
        assert response.status_code == 200 and '<h1 id="page-title">' in html and 'data-part="kpis"' in html
        assert not response.has_header("X-NeuroDB-Part")
        assert "no-store" not in response.get("Cache-Control", "")
    assert [seen["part"] for seen in SEEN] == [None, None]
    assert [seen["GET"] for seen in SEEN] == ["", ""]


@pytest.mark.shell
@pytest.mark.parametrize("name", ["Bad_Name", "kpis!", "-kpis", "", "a" * 41])
def test_an_invalid_part_name_draws_the_whole_page(client_viewer, name):
    response = raw_get(client_viewer, f"{PAGE}?_part={name}", **HX)
    assert response.status_code == 200 and '<h1 id="page-title">' in text(response)
    assert SEEN[-1]["part"] is None and not response.has_header("X-NeuroDB-Part")


def test_an_unknown_part_is_not_found(client_viewer):
    response = _part(client_viewer, "nope")
    assert response.status_code == 404
    assert response["X-NeuroDB-Part"] == "nope" and response["Cache-Control"] == "no-store"
    assert "HX-Request" in response["Vary"]


# ------------------------------------------------------------------------------------ response headers
def test_every_answer_varies_on_htmx_and_parts_are_never_stored(client_viewer):
    shell = raw_get(client_viewer, PAGE)
    part = _part(client_viewer, "kpis")
    data = _part(client_viewer, "data")
    other_page = raw_get(client_viewer, reverse("help:index"))
    anonymous = raw_get(Client(), "/")
    redirect = raw_get(Client(), PAGE)
    for response in (shell, part, data, other_page, anonymous, redirect):
        assert "HX-Request" in response["Vary"], response
    assert shell.status_code == 200 and "no-store" not in shell.get("Cache-Control", "")
    assert not shell.has_header("X-NeuroDB-Part")
    assert part["Cache-Control"] == "no-store" and part["X-NeuroDB-Part"] == "kpis"
    assert data["Cache-Control"] == "no-store" and data["Content-Type"] == "application/json"
    assert data.json() == {"rows": [1, 2]}
    assert redirect.status_code == 302  # a page, without HTMX: the usual sign-in redirect


def test_a_page_without_parts_answers_a_part_request_without_the_part_header(client_viewer):
    """``X-NeuroDB-Part`` is the browser's proof that an answer is the part it asked for (parts.js
    ``answersPart()``). A view that draws no parts answers ``_part`` with its whole page: without the
    header, or a tab left open on a page in parts would nest that page in itself, when a release where the
    page has no parts answers it (a rollback)."""
    for address in ("/parts-test/card/", reverse("help:index"), reverse("reports:databases")):
        response = raw_get(client_viewer, f"{address}?year=2026&_part=kpis", **HX)
        assert response.status_code == 200, address
        assert not response.has_header("X-NeuroDB-Part"), address
        assert response["Cache-Control"] == "no-store" and "HX-Request" in response["Vary"], address
    # the same request to a page in parts is answered as the part, with the header
    part = _part(client_viewer, "kpis")
    assert part["X-NeuroDB-Part"] == "kpis" and 'id="part-kpis"' in text(part)


def test_parts_middleware_sits_right_after_htmx():
    middleware = settings.MIDDLEWARE
    assert middleware.index("neurodb.web.parts.PartsMiddleware") == (
        middleware.index("django_htmx.middleware.HtmxMiddleware") + 1
    )


# ----------------------------------------------------------------------------------------- signing in
def test_an_expired_session_sends_the_browser_to_sign_in_and_back_to_the_page(db):
    client = Client()
    response = _part(
        client, "kpis", query="year=2025", HTTP_HX_CURRENT_URL="http://testserver/parts-test/?year=2025#top"
    )
    assert response.status_code == 200 and not response.has_header("Location")
    assert urlsplit(response["HX-Redirect"]).path == reverse("account_login")
    assert _next(response) == "/parts-test/?year=2025"
    assert response["Cache-Control"] == "no-store" and "HX-Request" in response["Vary"]

    # any HTMX request, not only parts; the page's address never carries _part
    response = raw_get(
        client,
        f"{PAGE}?year=2025",
        **_hx(HTTP_HX_CURRENT_URL="http://testserver/parts-test/?_part=x&year=2024"),
    )
    assert _next(response) == "/parts-test/?year=2024"


def test_an_address_from_another_site_is_never_where_sign_in_leads(db):
    for current in ("https://elsewhere.example/parts-test/", "//elsewhere.example/x", "javascript:alert(1)"):
        response = _part(Client(), "kpis", query="year=2025", HTTP_HX_CURRENT_URL=current)
        assert _next(response) == "/parts-test/?year=2025", current


def test_a_visitor_asking_for_a_part_of_a_public_page_is_sent_to_sign_in(db, settings):
    visitor = Client()
    root = raw_get(visitor, "/?_part=kpis", **_hx(HTTP_HX_CURRENT_URL="http://testserver/"))
    assert root.status_code == 200 and urlsplit(root["HX-Redirect"]).path == reverse("account_login")
    assert _next(root) == "/" and b"<html" not in root.content

    public = raw_get(visitor, "/parts-test/public/?_part=kpis", **HX)
    assert urlsplit(public["HX-Redirect"]).path == reverse("account_login") and SEEN == []

    settings.PUBLIC_PAGES = ["parts_test_page"]  # a page opened to visitors by the PUBLIC_PAGES setting
    listed = raw_get(visitor, f"{PAGE}?_part=kpis", **HX)
    assert urlsplit(listed["HX-Redirect"]).path == reverse("account_login") and SEEN == []
    assert raw_get(visitor, PAGE).status_code == 200  # the page itself stays public

    # a view marked @parts.public serves its parts to visitors; the Power BI feed keeps its own check
    opened = raw_get(visitor, "/parts-test/open/?year=2026&section=&_part=kpis", **HX)
    assert opened.status_code == 200 and 'id="part-kpis"' in text(opened)
    feed = raw_get(visitor, "/parts-test/feed/?_part=kpis", **HX)
    assert feed.status_code == 403 and feed.content == b"a key of its own"


# ------------------------------------------------------------------------------------------- 202 pending
def test_a_part_still_being_computed_answers_202(client_viewer):
    response = _part(client_viewer, "pending")
    assert response.status_code == 202 and response.content == b""
    assert response["X-NeuroDB-Part-Pending"] == "1" and response["Cache-Control"] == "no-store"
    assert response["X-NeuroDB-Part"] == "pending" and "HX-Request" in response["Vary"]


def test_a_part_waits_briefly_for_another_request_computing_its_figures(client_viewer, monkeypatch):
    monkeypatch.setattr(compute, "PART_WAIT", 0.3)
    lock = compute._ThreadLock(compute._full_key(SLOW_KEY, True))  # another request computing them
    assert lock.acquire()
    try:
        response = _part(client_viewer, "slow")
        assert response.status_code == 202 and response["X-NeuroDB-Part-Pending"] == "1"
        assert CALLS["slow"] == 0
    finally:
        lock.release()
    assert getattr(compute._local, "part_wait", None) is None  # the part's wait ended with its request
    response = _part(client_viewer, "slow")
    assert response.status_code == 200 and "Slow figure 7" in text(response) and CALLS["slow"] == 1


# ------------------------------------------------------------------------------------------ first load
@pytest.mark.shell
def test_a_part_request_is_never_a_first_load(client_viewer):
    shell = raw_get(client_viewer, PAGE)
    assert SEEN[-1]["first_load"] is True
    assert placeholders(text(shell))["kpis"] == f"{PAGE}?year=2026&section=Own&_part=kpis"

    bare = _part(client_viewer, "kpis", query="")  # no query at all, still a part request
    assert SEEN[-1]["first_load"] is False and SEEN[-1]["GET"] == ""
    # ... drawn with the request the view resolved (year 2026, every section), as the eager page would be
    assert 'next=/parts-test/%3Fyear%3D2026%26section%3D"' in text(bare)

    every = raw_get(client_viewer, f"{PAGE}?section=")
    assert SEEN[-1]["first_load"] is False
    assert placeholders(text(every))["kpis"] == f"{PAGE}?year=2026&section=&_part=kpis"

    request = RequestFactory().get("/")
    assert first_load(request) is True
    assert first_load(RequestFactory().get("/?year=2026")) is False
    request.part = "kpis"
    assert first_load(request) is False


# ------------------------------------------------------------------------------------- the {% part %} tag
@pytest.mark.shell
def test_the_placeholders_of_the_shell(client_viewer):
    html = text(raw_get(client_viewer, PAGE))
    found = placeholder_attributes(html)
    assert list(found) == ["kpis", "table", "stored", "ready", "form", "for-you"]

    kpis = found["kpis"]
    assert kpis["id"] == "part-kpis" and kpis["class"] == "part part--tiles"
    assert kpis["data-part-priority"] == "high" and kpis["data-part-group"] == "figures"
    assert kpis["aria-busy"] == "true" and kpis["aria-labelledby"] == "kpis-title"
    assert kpis["hx-get"] == f"{PAGE}?year=2026&section=Own&_part=kpis"
    assert kpis["hx-trigger"] == "nd-part" and kpis["hx-swap"] == "outerHTML"
    assert kpis["hx-request"] == '{"timeout": 60000}'
    assert kpis["style"] == "--part-rows: 2" and "data-part-needs" not in kpis
    assert found["table"]["style"] == "--part-h: 420px" and "data-part-group" in found["table"]
    assert found["ready"]["data-part-priority"] == "low" and "data-part-lazy" not in found["ready"]

    card = found["for-you"]
    assert (
        card["id"] == "for-you" and card["hx-get"] == "/parts-test/card/" and card["hx-swap"] == "innerHTML"
    )

    start = html.index('id="part-kpis"')
    kpis_html = html[start : html.index('id="part-table"')]
    assert '<div class="part__skeleton" aria-hidden="true">' in kpis_html
    assert kpis_html.count("skeleton__block--tile") == 8  # two rows of four tiles
    assert 'role="status"' not in kpis_html and "aria-live" not in kpis_html
    assert (
        '<noscript><a href="?year=2026&amp;section=Own&amp;_eager=1">Show the figures</a></noscript>'
        in kpis_html
    )
    assert "data-part-error hidden" in kpis_html and "data-part-retry" in kpis_html
    assert 'class="part__slow' in kpis_html and 'class="part__print-note"' in kpis_html
    # where scripts run but parts.js never did, a script of base.html shows the error box with this link in place of
    # Try again (review R3-critic-2): the same page drawn at once, as without JavaScript; hidden until then
    box = kpis_html[kpis_html.index("data-part-error hidden") : kpis_html.index('class="part__print-note"')]
    assert (
        '<a class="btn btn-sm btn-outline-secondary" href="?year=2026&amp;section=Own&amp;_eager=1" '
        "data-part-fallback hidden>Show the figures</a>"
    ) in box
    for name, attrs in found.items():  # every placeholder of the frame has it, a url= part too
        start = html.index(f'id="{attrs["id"]}"')
        own = html[start : html.index("</noscript>", start)]
        assert re.search(r'href="\?[^"]*_eager=1" data-part-fallback hidden>Show the figures</a>', own), name

    # the figures are not in the shell, and the shell computed none of them
    assert "Tile 3" not in html and "Row 1" not in html
    assert CALLS == Counter({"shell": 1})


def _tag(source, **context):
    request = RequestFactory().get("/page/", {"year": "2026"})
    return Template("{% load parts %}" + source).render(Context({"request": request, **context}))


@pytest.mark.parametrize("height", ["760px", "12.5rem", "70vh", "0px"])
def test_a_valid_height_is_reserved(height):
    html = _tag('{% part "x" url="/x/" height=h %}', h=height)
    assert f'style="--part-h: {height}"' in html


@pytest.mark.parametrize(
    "height",
    ["760", "760px;color:red", "760px; --x: 1", "calc(1px)", "-5px", "1e3px", "760 px", "", None, "5em"],
)
def test_any_other_height_is_left_out(height):
    html = _tag('{% part "x" url="/x/" height=h %}', h=height)
    assert "style=" not in html and "--part-h" not in html


@pytest.mark.parametrize(
    ("kind", "needs"),
    [
        ("chart", "plotly"),
        ("charts", "plotly"),
        ("map", "maplibre"),
        ("tiles", None),
        ("table", None),
        ("text", None),
    ],
)
def test_charts_and_maps_ask_for_their_library_ahead(kind, needs):
    attrs = placeholder_attributes(_tag('{% part "x" url="/x/" kind=k %}', k=kind))["x"]
    assert attrs.get("data-part-needs") == needs and attrs["class"] == f"part part--{kind}"


def test_the_tag_arguments_as_given():
    html = _tag(
        '{% part "x" url="/x/?a=1&b=2" kind="list" lazy="strict" anchors="outcome- watch-" needs="maplibre plotly" '
        'rows=3 element_id="local-points" %}'
    )
    attrs = placeholder_attributes(html)["x"]
    assert attrs["id"] == "local-points" and attrs["hx-get"] == "/x/?a=1&b=2"
    assert attrs["data-part-lazy"] == "strict" and attrs["data-part-anchors"] == "outcome- watch-"
    assert attrs["data-part-needs"] == "maplibre plotly" and attrs["style"] == "--part-rows: 3"
    assert attrs["data-part-priority"] == "normal" and attrs["hx-swap"] == "outerHTML"
    assert html.count("skeleton__block--row") == 3
    assert 'href="?year=2026&amp;_eager=1"' in html  # the request's query when no page registered one
    assert "data-part-needs" not in _tag('{% part "x" url="/x/" kind="map" needs="" %}')


@pytest.mark.parametrize(
    "source",
    [
        '{% part "x" url="/x/" kind="pie" %}',
        '{% part "x" url="/x/" priority="urgent" %}',
        '{% part "x" url="/x/" lazy="soon" %}',
        '{% part "x" url="/x/" swap="beforeend" %}',
        '{% part "x" url="/x/" needs="jquery" %}',
        '{% part "x" url="/x/" element_id="1 bad" %}',
        '{% part "Bad" url="/x/" %}',
        '{% part "x" %}',  # no part of that name on the page, and no url=
        '{% part "x" url="/x/" page="https://elsewhere.example/x/" %}',  # page=: a path on this site
        '{% part "x" url="/x/" page="//elsewhere.example/x/" %}',
        '{% part "x" url="/x/" page="javascript:alert(1)" %}',
    ],
)
def test_a_wrong_argument_is_an_error_not_a_broken_placeholder(source):
    with pytest.raises(TemplateSyntaxError):
        _tag(source)


def test_three_heights_and_the_tiles_of_a_row():
    html = _tag(
        '{% part "x" url="/x/" kind="tiles" tiles=5 height="18.5rem" height_md="34rem" height_sm="54rem" %}'
    )
    attrs = placeholder_attributes(html)["x"]
    assert attrs["style"] == "--part-h: 18.5rem; --part-h-sm: 54rem; --part-h-md: 34rem"
    assert html.count("skeleton__block--tile") == 5
    for tiles in (0, 13, "many", None):  # anything else: a row of four
        assert (
            _tag('{% part "x" url="/x/" kind="tiles" tiles=t %}', t=tiles).count("skeleton__block--tile") == 4
        )
    assert "--part-h-md" not in _tag('{% part "x" url="/x/" height_md="34rem; color: red" %}')


@pytest.mark.parametrize("swap", ["outerHTML", "innerHTML"])
def test_a_placeholder_answers_into_itself_whatever_surrounds_it(swap):
    """htmx takes hx-target, hx-select, hx-push-url and hx-replace-url from the closest element that has them.
    A placeholder inside a block that targets elsewhere, selects a piece of the answer or pushes the address
    would swap its part there, lose it, or show the part's address in the address bar: it sets all four
    itself, and keeps them from what an innerHTML part holds once loaded."""
    html = _tag(
        '<section hx-get="/elsewhere/" hx-target="#elsewhere" hx-select="#review-documents" hx-push-url="true" '
        'hx-replace-url="true">{% part "x" url="/x/" swap=swap %}</section>',
        swap=swap,
    )
    attrs = placeholder_attributes(html)["x"]
    assert attrs["hx-target"] == "this"
    assert attrs["hx-select"] == "unset"  # htmx 2: no value, whatever an ancestor says
    assert attrs["hx-push-url"] == "false" and attrs["hx-replace-url"] == "false"
    assert set(attrs["hx-disinherit"].split()) == {"hx-target", "hx-select", "hx-push-url", "hx-replace-url"}


def test_part_url_and_page_url_start_from_the_resolved_query():
    query = "year=2026&section=Own&page=1&_part=old"
    html = _tag(
        "{% part_url 'grid' page=3 %}|{% part_url 'grid' %}|{% page_url page=3 %}|{% page_url page=None %}",
        parts_query=query,
    )
    assert html.split("|") == [
        "/page/?year=2026&amp;section=Own&amp;page=3&amp;_part=grid",
        "/page/?year=2026&amp;section=Own&amp;page=1&amp;_part=grid",
        "/page/?year=2026&amp;section=Own&amp;page=3",
        "/page/?year=2026&amp;section=Own",
    ]
    assert _tag("{% part_url 'grid' %}|{% page_url %}", parts_query="") == "/page/?_part=grid|/page/"


def test_render_page_refuses_a_bad_name_or_half_a_results_area():
    request = RequestFactory().get("/")
    with pytest.raises(ValueError):
        render_page(request, "x.html", scope={}, shell=dict, parts={"Kpis": Part("x.html", dict)}, query="")
    with pytest.raises(TypeError):
        render_page(request, "x.html", scope={}, shell=dict, parts={}, query="", results_target="results")


# --------------------------------------------------------------------------------------- part responses
def test_a_part_answers_its_wrapper_with_the_scope_and_its_own_figures(client_viewer):
    response = get_part(client_viewer, PAGE, "kpis")
    html = text(response)
    assert html.lstrip().startswith(
        '<div id="part-kpis" class="part is-loaded" data-part-loaded="kpis" tabindex="-1">'
    )
    assert "Tile 3" in html and "Tile 8" in html and "Year 2026, section Own" in html
    assert response.context["tiles"] == [3, 5, 8] and response.context["part_name"] == "kpis"
    assert CALLS["kpis"] == 1 and CALLS["shell"] == 1  # the shell's context: only for the shell get_part read


def test_a_part_is_drawn_alone_then_put_in_its_wrapper(client_viewer):
    """Drawn inside its wrapper (an ``{% include %}`` in components/part_loaded.html), a part took about twice the
    time. It is drawn alone, with the request's context, and the wrapper gets only its name and that drawing."""
    response = get_part(client_viewer, PAGE, "kpis")
    names = [template.name for template in response.templates]
    assert names == ["parts_test/kpis.html", parts.LOADED_TEMPLATE]
    part, wrapper = response.context
    assert part["tiles"] == [3, 5, 8] and part["request"].part == "kpis"  # the request's context, as before
    assert set(wrapper.flatten()) - {"True", "False", "None"} == {"part_name", "part_body"}
    drawn = Template(TEMPLATES["parts_test/kpis.html"]).render(Context(part.flatten()))
    expected = f'<div id="part-kpis" class="part is-loaded" data-part-loaded="kpis" tabindex="-1">\n{drawn}\n</div>\n'
    assert text(response) == expected


def test_no_link_inside_a_part_carries_part(client_viewer):
    page = assemble(client_viewer, PAGE)
    assert set(page.parts) == {"kpis", "table", "more", "stored", "ready", "form", "for-you"}
    for name, response in page.parts.items():
        for attribute, value in re.findall(r'\s(href|action|hx-push-url)="([^"]*)"', text(response)):
            assert "_part" not in value, (name, attribute, value)  # (_eager=1: the noscript links)


def test_the_pagination_of_a_part_reloads_that_part_and_shows_the_page_address(client_viewer):
    html = text(get_part(client_viewer, PAGE, "table"))
    link = re.search(r'<a class="page-link" href="([^"]*)" (hx-get="[^"]*" [^>]*)>2</a>', html)
    assert link is not None, html
    href, attributes = link.group(1).replace("&amp;", "&"), link.group(2).replace("&amp;", "&")
    assert href == "?year=2026&section=Own&page=2"
    assert f'hx-get="{PAGE}?year=2026&section=Own&page=2&_part=table"' in attributes
    assert 'hx-target="#part-table"' in attributes and 'hx-swap="outerHTML"' in attributes
    assert f'hx-push-url="{PAGE}?year=2026&section=Own&page=2"' in attributes

    second = raw_get(client_viewer, f"{PAGE}?year=2026&section=Own&page=2&_part=table", **HX)
    assert second.status_code == 200 and "Row 11" in text(second) and "Row 1<" not in text(second)
    assert 'href="?year=2026&amp;section=Own&amp;page=2&amp;sort=name"' in text(second)
    assert 'hx-push-url="/parts-test/?year=2026&amp;section=Own&amp;page=3"' in text(second)


def test_a_form_inside_a_part_carries_a_token_its_cookie_matches(viewer):
    client = Client(enforce_csrf_checks=True)
    client.force_login(viewer)
    response = _part(client, "form")
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text(response)).group(1)
    assert settings.CSRF_COOKIE_NAME in response.cookies  # the token was first asked for inside the part
    assert client.post("/parts-test/post/", {"csrfmiddlewaretoken": token}).status_code == 200


# ----------------------------------------------------------------------------------------- eager mode
@pytest.mark.shell
def test_eager_draws_every_part_inline_once(client_viewer):
    html = text(eager(client_viewer, PAGE))
    assert list(placeholders(html)) == ["for-you"]  # an existing endpoint stays the browser's to load
    for name in ("kpis", "table", "more", "stored", "ready", "form"):
        assert html.count(f'data-part-loaded="{name}"') == 1, name
    assert "Tile 3" in html and "Row 1<" in html and "Folded findings" in html and "Stored figure 42" in html
    assert CALLS == Counter(shell=1, kpis=1, table=1, more=1, stored=1, ready=1, form=1)


@pytest.mark.shell
def test_eager_leaves_a_part_from_another_address_a_quiet_slot(client_viewer):
    """The page drawn at once (``_eager=1``: no JavaScript, print) draws every part of its own; a ``url=``
    part comes from another view, so it stays the browser's to load. Without JavaScript nothing ever fills
    it: no grey shapes forever, not busy for screen readers, and never a *Show the figures* link to the page
    it is on."""
    html = text(eager(client_viewer, PAGE))
    slot = placeholder_attributes(html)["for-you"]
    assert (
        slot["hx-get"] == "/parts-test/card/" and slot["hx-trigger"] == "nd-part"
    )  # parts.js still loads it
    assert "aria-busy" not in slot
    inside = html[html.index('id="for-you"') :]
    assert "part__skeleton" not in inside and "<noscript>" not in inside and "_eager=1" not in inside
    assert "data-part-error hidden" in inside  # its error state, should it fail to load
    assert "data-part-fallback" not in inside  # no page of its own given: no link (review R3-critic-2)

    frame = text(raw_get(client_viewer, PAGE))  # the frame: a placeholder like any other
    card = placeholder_attributes(frame)["for-you"]
    assert card["aria-busy"] == "true"
    assert "part__skeleton" in frame[frame.index('id="for-you"') :]


def test_the_assembled_page_is_the_eager_page(client_viewer):
    for query in ("", "?section=", "?year=2025&section=CP"):
        assembled = assemble(client_viewer, PAGE + query)
        drawn = eager(client_viewer, PAGE + query)
        assert normalise(assembled.html, exclude=["for-you"]) == normalise(text(drawn), exclude=["for-you"])
        assert "Tile 5" in assembled.html and "Folded findings" in assembled.html


def test_two_drawings_a_moment_apart_compare_equal_whatever_naturaltime_wrote():
    """Equivalence compares two drawings of a page made one after the other. A time drawn with naturaltime can
    fall either side of a step between them ("now" and "a second ago" when a run ended at the fixture's time,
    "59 seconds ago" and "a minute ago"): every form it writes for a past time is folded alike."""
    from django.contrib.humanize.templatetags.humanize import naturaltime

    now = timezone.now()
    seconds = (0, 1, 2, 59, 60, 61, 3599, 3600, 3601, 7200, 86399, 86400, 90000, 40 * 86400, 400 * 86400)
    written = {naturaltime(now - datetime.timedelta(seconds=n)) for n in seconds}
    assert {"now", "a second ago", "a minute ago", "an hour ago"} <= written
    assert len({normalise(f"<p>Last success {when}</p>") for when in written}) == 1
    assert normalise("<p>Last success now</p>") != normalise("<p>Last success 2026</p>")


def test_a_test_that_undoes_its_monkeypatch_still_gets_the_assembled_page(client_viewer, monkeypatch):
    """The page assembler of tests/conftest.py patches the client with a MonkeyPatch of its own: a test that
    calls ``monkeypatch.undo()`` still reads the page put together, so its later assertions on the page (above
    all "not on the page" ones) still test the figures, not the bare frame."""
    monkeypatch.setattr(compute, "PART_WAIT", 0.5)
    monkeypatch.undo()
    html = text(client_viewer.get(PAGE))
    assert "Tile 5" in html and "Folded findings" in html and "Stored figure 42" in html
    assert set(placeholders(html)) <= {"for-you"}


# --------------------------------------------------------------------------------- inline when ready
@pytest.mark.shell
def test_a_part_whose_figures_are_stored_is_drawn_inline(client_viewer):
    first = text(raw_get(client_viewer, PAGE))
    assert 'data-part="stored"' in first and CALLS["stored"] == 0  # an empty cache: a placeholder

    assert "Stored figure 42" in text(get_part(client_viewer, PAGE, "stored")) and CALLS["stored"] == 1

    again = text(raw_get(client_viewer, PAGE))  # stored now: drawn inline, one request, nothing recomputed
    assert 'data-part="stored"' not in again and 'data-part-loaded="stored"' in again
    assert "Stored figure 42" in again and CALLS["stored"] == 1

    READY["ready"] = True
    ready = text(raw_get(client_viewer, PAGE))
    assert 'data-part="ready"' not in ready and "Ready figure 9" in ready and CALLS["ready"] == 1
    assert 'data-part="kpis"' in ready  # the others still load in the background


def test_a_part_peeks_at_a_value_stored_for_every_language():
    compute.once("parts-test-neutral", lambda: {"n": 1}, 60, lang_neutral=True)
    assert Part("x.html", dict, peek="parts-test-neutral", peek_lang_neutral=True).is_ready()
    assert not Part("x.html", dict, peek="parts-test-neutral").is_ready()  # a value per language: not stored
    compute.once("parts-test-english", lambda: {"n": 2}, 60)
    assert Part("x.html", dict, peek="parts-test-english").is_ready()
    assert not Part("x.html", dict, peek="parts-test-english", peek_lang_neutral=True).is_ready()


# ---------------------------------------------------------------------------------------- results shell
@pytest.mark.shell
def test_only_the_declared_results_area_gets_the_results_shell(client_viewer):
    results = raw_get(client_viewer, f"{PAGE}?year=2025", **_hx(HTTP_HX_TARGET="parts-results"))
    html = text(results)
    assert results.status_code == 200 and "<h1" not in html and '<h2 id="kpis-title">' in html
    assert placeholders(html)["kpis"] == f"{PAGE}?year=2025&section=&_part=kpis" and "Tile 3" not in html
    assert CALLS == Counter(shell=1)

    for extra in ({"HTTP_HX_TARGET": "modal-content"}, {}):
        other = raw_get(client_viewer, f"{PAGE}?year=2025", **_hx(**extra))
        assert '<h1 id="page-title">' in text(other), extra


# ------------------------------------------------------------------------------------------ templates
INCLUDE = re.compile(r"""{%\s*include\s+(["'])([^"']+)\1""")


def _with_includes(template: Path) -> dict[Path, str]:
    """The source of ``template`` and of every template it includes by name, and so on: what a part sends is
    mostly in the partials its one-line template includes."""
    from django.template.loader import get_template

    found: dict[Path, str] = {}
    todo = [template]
    while todo:
        current = todo.pop()
        if current in found:
            continue
        found[current] = current.read_text(encoding="utf-8")
        todo.extend(Path(get_template(name).origin.name) for _, name in INCLUDE.findall(found[current]))
    return found


def test_part_templates_hold_no_script_but_json_data():
    root = Path(settings.BASE_DIR) / "neurodb"
    templates = sorted(root.glob("*/templates/*/parts/*.html")) + sorted(
        (root / "web" / "templates" / "components").glob("part*.html")
    )
    assert templates, "the part components are missing"
    for template in templates:
        source = template.read_text(encoding="utf-8")
        for attributes in re.findall(r"<script\b([^>]*)>", source, re.I):
            assert 'type="application/json"' in attributes, f"{template}: <script{attributes}>"
        assert "hx-on" not in source, template
    # ... and every partial they include (SPEC7 §1.3 rule 3 is about what a part sends)
    sources = {file: source for template in templates for file, source in _with_includes(template).items()}
    assert len(sources) > len(templates)  # the overview's parts are one-line includes
    for file, source in sources.items():
        for attributes in re.findall(r"<script\b([^>]*)>", source, re.I):
            assert 'type="application/json"' in attributes, f"{file}: <script{attributes}>"
        assert "hx-on" not in source, file


def test_what_would_run_in_a_part():
    assert executable('<script id="d" type="application/json">{}</script><p onclick-free="1">x</p>') == []
    assert executable('<a href="/x/">x</a><button type="button" data-part-retry>Try again</button>') == []
    found = executable(
        '<script>window.x = 1</script><script type="module" src="/a.js"></script>'
        '<button onclick="go()">x</button><img src="x" onerror="go()"><div hx-on:click="go()"></div>'
        '<div hx-on::after-request="go()"></div><a href=" JavaScript:go()">x</a>'
    )
    assert len(found) == 7, found


def test_the_assembly_refuses_a_part_that_would_run_a_script(client_viewer):
    """A script in a partial a part includes would run once swapped in (htmx re-creates it with the page's
    nonce): every assembled page checks every part it asks for."""
    with pytest.raises(AssertionError, match="would run"):
        client_viewer.get("/parts-test/scripted/")


# ------------------------------------------------------------------------- the test client's assembly
def test_the_test_client_answers_the_assembled_page(client_viewer):
    response = client_viewer.get(PAGE)
    html = text(response)
    assert response.status_code == 200 and placeholders(html) == {}
    assert "Tile 3" in html and "Row 10" in html and "Folded findings" in html and "Your card" in html
    assert 'id="for-you" class="part is-loaded" data-part-loaded="for-you"' in html
    assert response.context["title"] == "Parts test page"  # the shell's context ...
    assert response.context["tiles"] == [3, 5, 8]  # ... and the parts', the strict one inside another too
    assert response.context["more"] == "Folded findings"
    assert set(response.parts) == {"kpis", "table", "more", "stored", "ready", "form", "for-you"}
    assert "parts_test/kpis.html" in [t.name for t in response.templates]

    # an HTMX request is left as it is
    assert placeholders(text(client_viewer.get(PAGE, HTTP_HX_TARGET="parts-results", **HX)))


@pytest.mark.shell
def test_a_shell_test_gets_the_shell(client_viewer):
    response = client_viewer.get(PAGE)
    assert "kpis" in placeholders(text(response)) and "Tile 3" not in text(response)
    with pytest.raises(KeyError):
        response.context["tiles"]


def test_a_part_refused_in_an_assembled_page_fails_the_test(client_viewer):
    """Its placeholder would stay, and every "not on the page" assertion of the test would pass without
    testing the part's figures."""
    with pytest.raises(AssertionError, match=r"the part 'denied' answered 403 at /parts-test/denied/"):
        client_viewer.get("/parts-test/refused/")


@pytest.mark.parts_may_fail
def test_the_assembly_keeps_the_placeholder_of_a_part_that_is_refused(client_viewer):
    response = client_viewer.get("/parts-test/refused/")
    html = text(response)
    assert list(placeholders(html)) == ["denied"] and "Tile 3" in html
    assert response.parts["denied"].status_code == 403
    assert "not for this person" not in html and "secret" not in response.context
    assert response.context["tiles"] == [3, 5, 8]


def test_assembling_without_strict_parts_leaves_them_to_the_browser(client_viewer):
    page = assemble(client_viewer, PAGE, strict=False)
    assert list(placeholders(page.html)) == ["more"] and "more" not in page.parts
    assert "Row 1<" in page.html and "Folded findings" not in page.html


def test_normalise_drops_what_differs_between_two_drawings():
    one = (
        '<meta name="csrf-token" content="abc"><div id="part-x" class="part" data-part="x" hx-get="/a?b=1"\n'
        ' aria-busy="true"><input type="hidden" name="csrfmiddlewaretoken" value="t1">'
        '<script nonce="n1" type="application/json">{}</script><p>Updated 3\xa0minutes ago</p>  </div>'
        '<div id="for-you"><div>loading</div></div>'
    )
    two = (
        '<meta name="csrf-token" content="xyz"><div id="part-x" class="part" data-part-loaded="x">'
        '<input type="hidden" name="csrfmiddlewaretoken" value="t2"><script nonce="n2" type="application/json">'
        "{}</script><p>Updated 1 hour, 2 minutes ago</p></div>"
        '<div id="for-you" class="is-loaded"><p>Your card</p></div>'
    )
    assert normalise(one, exclude=["for-you"]) == normalise(two, exclude=["for-you"])
    assert normalise(one) != normalise(two)
    assert "Updated … ago" in normalise(one) and "data-part" not in normalise(one)
