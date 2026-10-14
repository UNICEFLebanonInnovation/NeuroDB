"""The donor page with its figures as one JSON part (STAGE3 group D1, SPEC7 §2.1 ``/donor/``, SPEC7_review item 24).

The page answers its frame at once (the bar with the year, the tabs, the headings and the cards, the date of the
data) and ``donor.js`` fetches its figures: the page's ``data`` part, at the page's address plus ``_part=data`` with
the year written out (and in an administrator's preview the account and the tab), answered as JSON from one build
kept in the cache every worker shares. When that build is already stored, or the page is asked for whole
(``_eager=1``), the frame carries the figures itself. These tests check the frame without any figure work, the part
and who may fetch it (a donor never another account's figures), that the figures are the same for a donor and an
administrator's preview, that an edit shows at once, and that ``donor.js`` fetches its figures, waits for a build in
progress and shows the error box with Try again.

On ``tests/donors/test_donor_access.py``'s world: one programme document (Amel), half funded by the EU (the
account ``eu-donor``), half by Japan.
"""

# ruff: noqa: F811 (the fixtures "data", "donor" and "donor_client" come from test_donor_access)
from __future__ import annotations

import datetime
import html as html_module
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core.cache import caches
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.donors import services
from neurodb.reports import overview
from tests.donors.test_donor_access import data, donor, donor_client, make_donor  # noqa: F401
from tests.reports.overview_fixture import TODAY
from tests.web.parts import HX, raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/donor/"  # donors:page
DONOR_JS = Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "js" / "donor.js"
DONOR_CSS = Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "donor.css"
INLINE = re.compile(r'<script id="donor-data" type="application/json">(.*?)</script>', re.S)
# Query ceilings: what was measured on this world, plus 2. The frame: the request's floor (session, user, the donor
# account, its last visit), the years the account has funds in (its FR lines, their windows), the date of the data
# (the last eTools Datamart run, else the other sources') and the cache stamp (last sync run, indicator flags) to see
# whether the figures are stored. The part with nothing stored computes the build (the account's funds, the
# overview of the country, the children of each programme document).
SHELL_QUERIES = 12
PART_QUERIES = 62


@pytest.fixture
def world(data, reporting_year):
    """The donor world of tests/donors/test_donor_access.py, with the reporting year."""
    return data


def _source(response) -> str:
    """The address donor.js fetches the figures from (main[data-donor-src])."""
    found = re.search(r'data-donor-src="([^"]+)"', text(response))
    assert found, "the frame names no address for its figures"
    return html_module.unescape(found.group(1))


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _figures(client, url: str = PAGE) -> dict:
    """The page's figures as donor.js gets them: in the frame when stored, else from the part."""
    frame = raw_get(client, url)
    assert frame.status_code == 200
    inline = INLINE.search(text(frame))
    if inline:
        return json.loads(inline.group(1))
    part = client.get(_source(frame), HTTP_HX_REQUEST="true")
    assert part.status_code == 200 and part["Content-Type"] == "application/json"
    return part.json()


def _heavy_services_refuse(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    for name in ("build", "_build", "contribution", "country"):
        monkeypatch.setattr(services, name, refuse)
    monkeypatch.setattr(overview, "build", refuse)
    monkeypatch.setattr(overview, "children_by_pd", refuse)


def _admin(admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    return admin_user


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("donors:page") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
def test_the_frame_answers_with_every_figure_service_refusing(donor_client, world, monkeypatch):
    caches["shared"].clear()  # nothing stored: the figures may not come with the frame either
    _heavy_services_refuse(monkeypatch)
    response = donor_client.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "European Union" in html and 'id="year"' in html and 'data-tab="overall"' in html  # bar, tabs
    for heading in ("Where your money goes", "How it is being spent", "Who benefits", "Children reached"):
        assert heading in html, heading  # the cards' frames and headings
    assert "The date of the data is not available yet." in html  # the date of the data
    assert 'data-donor-state="waiting"' in html and 'id="loadRetry"' in html and "Try again" in html
    assert 'aria-busy="true"' not in html  # busy only while donor.js fetches: never for a browser without it
    assert 'id="donor-data"' not in html  # the figures come later
    assert _query(_source(response)) == {"year": ["2026"], "_part": ["data"]}
    assert 'id="mineEmpty" hidden' in html  # no "no funding" before the figures say so
    assert "Japan" not in html and "Amel" not in html


def test_the_frame_stays_within_its_query_ceiling(donor_client, world, django_assert_max_num_queries):
    raw_get(donor_client, PAGE)
    caches["shared"].clear()
    with django_assert_max_num_queries(SHELL_QUERIES):
        assert donor_client.get(PAGE).status_code == 200


def test_the_part_answers_the_accounts_figures_as_json(
    donor_client, donor, world, django_assert_max_num_queries
):
    caches["shared"].clear()
    address = _source(raw_get(donor_client, PAGE))
    with django_assert_max_num_queries(PART_QUERIES):
        response = donor_client.get(address, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == "data"
    figures = response.json()
    assert figures == json.loads(json.dumps(services.build(donor, 2026, datetime.date.today()), default=str))
    assert [p["id"] for p in figures["pds"]] == ["LEB/PD1"] and figures["pds"][0]["committed"] == 10000
    assert "Japan" not in json.dumps(figures)
    # without HTMX, the address is the page itself (a stray _part link draws the page)
    assert donor_client.get(address)["Content-Type"].startswith("text/html")


# ---------------------------------------------------------------------------------------------- who may fetch
def test_a_donor_never_fetches_another_accounts_figures(donor_client, donor, world):
    """The view ignores ``account`` for a donor (SPEC7_review item 24): asked for with another account's id, in the
    page or in the part's address, the figures are the donor's own."""
    japan = make_donor("japan-donor", name="Japan", donors=["Japan"])
    own = _figures(donor_client)
    frame = raw_get(donor_client, PAGE, {"account": japan.pk})
    assert "account" not in _query(_source(frame))  # the frame never writes another account in
    crafted = donor_client.get(f"{PAGE}?account={japan.pk}&year=2026&_part=data", HTTP_HX_REQUEST="true")
    assert crafted.status_code == 200 and crafted.json() == own
    theirs = services.build(japan, 2026)
    assert theirs["pds"][0]["committed"] == 10000 and json.dumps(theirs) != json.dumps(own)  # they do differ


def test_staff_have_no_donor_part_and_administrators_preview_one(client, viewer, admin_user, donor, world):
    client.force_login(viewer)
    assert client.get(PAGE).status_code == 404
    assert client.get(f"{PAGE}?account={donor.pk}&year=2026&_part=data", **HX).status_code == 404
    client.force_login(_admin(admin_user))
    assert client.get(f"{PAGE}?year=2026&_part=data", **HX).status_code == 404  # no account: no page
    frame = raw_get(client, PAGE, {"account": donor.pk, "tab": "overall"})
    assert frame.status_code == 200 and "Preview" in text(frame)
    assert _query(_source(frame)) == {
        "year": ["2026"],
        "account": [str(donor.pk)],
        "tab": ["overall"],
        "_part": ["data"],
    }


def test_a_visitor_asking_for_the_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?year=2026&_part=data", **HX)
    assert response.status_code == 200 and response["HX-Redirect"].startswith(reverse("account_login"))


def test_an_account_still_on_its_temporary_password_is_refused_the_part(client, world):
    account = make_donor("fresh", must_change_password=True)
    client.force_login(account.user)
    assert client.get(f"{PAGE}?year=2026&_part=data", **HX).status_code == 403


# ---------------------------------------------------------------------------------- one build for everyone
def test_the_figures_are_the_same_for_the_donor_and_an_administrators_preview(
    client, admin_user, donor, world
):
    """Nothing in the build depends on who asks: the donor's own page and an administrator's preview of the same
    account read one stored build (the preview banner and the account in the address are the frame's)."""
    client.force_login(donor.user)
    by_donor = _figures(client)
    client.force_login(_admin(admin_user))
    frame = raw_get(client, PAGE, {"account": donor.pk})
    inline = INLINE.search(text(frame))
    assert inline, "the build the donor's visit stored comes with the preview's frame"
    assert json.loads(inline.group(1)) == by_donor
    caches["shared"].clear()
    by_preview = _figures(client, f"{PAGE}?account={donor.pk}")
    client.force_login(donor.user)
    assert _figures(client) == by_preview == by_donor


def test_stored_figures_come_with_the_frame(donor_client, world, monkeypatch, django_assert_max_num_queries):
    """Inline when ready: once the figures are stored, the frame carries them, so a warm page is one request (as
    before, when they were kept per worker)."""
    fetched = _figures(donor_client)  # the part computes and stores the build

    def refuse(*args, **kwargs):
        raise AssertionError("the stored figures were computed again")

    monkeypatch.setattr(services, "_build", refuse)
    with django_assert_max_num_queries(SHELL_QUERIES):
        frame = raw_get(donor_client, PAGE)
    html = text(frame)
    assert json.loads(INLINE.search(html).group(1)) == fetched
    main = html.split("<main", 1)[1].split(">", 1)[0]
    # nothing to fetch, but the cards keep their room until donor.js has drawn the figures (no jump on first paint)
    assert 'data-donor-state="drawing"' in main and "data-donor-src=" in main
    assert 'id="mineEmpty" hidden' in html and 'id="mineContent" hidden' not in html


def test_the_page_asked_for_whole_carries_its_figures(donor_client, world):
    caches["shared"].clear()
    page = raw_get(donor_client, PAGE + "?_eager=1")
    found = INLINE.search(text(page))
    assert found and json.loads(found.group(1))["pds"][0]["id"] == "LEB/PD1"


def test_an_account_without_funds_shows_its_empty_card_once_known(donor_client, world):
    """The frame cannot know whether the year has funds before the figures say so: the empty card waits, hidden,
    and donor.js shows it; once the figures are stored, the frame shows it itself."""
    no_funds = make_donor("nobody", name="Nobody", donors=["Nobody"])
    donor_client.force_login(no_funds.user)
    assert _figures(donor_client)["pds"] == []
    frame = text(raw_get(donor_client, PAGE))  # now stored: the frame knows
    assert 'id="mineEmpty"' in frame and 'id="mineEmpty" hidden' not in frame
    assert 'id="mineContent" hidden' in frame


# ------------------------------------------------------------------------------------------- fresh after an edit
def test_an_account_changed_in_the_admin_shows_at_once(client, admin_user, donor, world):
    """Fresh after edit (STAGE3): partners' names switched off in the account's admin; the donor's page shows the
    aliases at once, though its figures were stored a moment before."""
    client.force_login(donor.user)
    assert _figures(client)["pds"][0]["partner"] != "Partner 1 (civil society)"  # stored now
    client.force_login(_admin(admin_user))
    response = client.post(
        reverse("admin:donors_donoraccount_change", args=[donor.pk]),
        {
            "name": donor.name,
            "donor_choices": ["EU"],
            "other_donors": "",
            "grant_choices": [],
            "grant_list": "",
            "contact": "",
            "active": "on",
            "expires_on": "",
        },
    )
    assert response.status_code == 302, text(response)[:2000]
    donor.refresh_from_db()
    assert donor.show_partner_names is False
    client.force_login(donor.user)
    assert _figures(client)["pds"][0]["partner"] == "Partner 1 (civil society)"


def test_a_finished_sync_shows_at_once(donor_client, donor, world):
    from neurodb.datamart import models as dm

    assert _figures(donor_client)["pds"][0]["committed"] == 10000  # stored now
    dm.FundsReservation.objects.filter(fr_number="0400001").update(overall_amount=12000)
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="all", status=SyncRun.Status.RUNNING)
    run.finish(SyncRun.Status.SUCCEEDED)
    assert _figures(donor_client)["pds"][0]["committed"] == 12000


def test_two_accounts_years_or_days_never_share_a_key(donor, world):
    other = make_donor("eu-2", name="EU 2", donors=["EU"])
    keys = {
        services._cache_key(donor, 2026, TODAY),
        services._cache_key(other, 2026, TODAY),
        services._cache_key(donor, 2025, TODAY),
        services._cache_key(donor, 2026, TODAY.replace(day=TODAY.day % 28 + 1)),
    }
    assert len(keys) == 4
    before = services._cache_key(donor, 2026, TODAY)
    donor.grants = ["SC1"]
    assert services._cache_key(donor, 2026, TODAY) != before  # what the donor sees changed


# ------------------------------------------------------------------------------------------------- donor.js
def test_donor_js_fetches_its_figures_and_offers_try_again():
    """donor.js no longer reads #donor-data at load: it uses it when the frame carries it, else it fetches the
    page's part as HTMX would (the donor lock-out lets donors:page through), waits for a build in progress (202),
    follows a sign-in redirect, retries once on a network error or a server restarting, and otherwise shows the
    error box with Try again."""
    source = DONOR_JS.read_text(encoding="utf-8")
    assert 'JSON.parse(document.getElementById("donor-data").textContent)' not in source
    assert 'const inline = document.getElementById("donor-data");' in source
    assert "fetch(main.dataset.donorSrc" in source and '"HX-Request": "true"' in source
    assert 'response.status === 202 && response.headers.get("X-NeuroDB-Part-Pending")' in source
    assert 'response.headers.get("HX-Redirect")' in source
    assert "const RETRY_ONCE = new Set([0, 502, 503, 504]);" in source
    assert '$("#loadRetry").addEventListener("click"' in source
    assert "if (!DATA) return;" in source.split("function render()", 1)[1]  # a tab clicked before the figures


# donor.js run in node on the stand-in page of donor_dom.mjs (its elements, a clock the test moves, fetch answering
# from a list): page(options) gives a fresh page and a fresh copy of donor.js, run until no timer is left. FIGURES is
# the part's real JSON (process.argv[3]).
DONOR_DOM = Path(__file__).parent / "donor_dom.mjs"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
DOM_PRELUDE = """
const dom = await import(process.argv[2]);
const { readFileSync } = await import("node:fs");
const donorSource = readFileSync(new URL(process.argv[1]), "utf8");
const FIGURES = JSON.parse(process.argv[3]);
const PENDING = { status: 202, headers: { "X-NeuroDB-Part-Pending": "1" } };
const DATA = { status: 200, body: FIGURES };
const out = {};
let pages = 0;
async function page(options = {}) {
  const env = dom.installPage(options);
  pages += 1;
  // a fresh copy, run as the module the page loads (donor.js imports and exports nothing, so node would run a file of
  // it as a script, once)
  await import("data:text/javascript;charset=utf-8," + encodeURIComponent(`${donorSource}\n// page ${pages}`));
  await env.run();
  return env;
}
const seen = (env) => ({
  state: env.main.dataset.donorState,
  busy: env.main.getAttribute("aria-busy"),
  error: env.el("loadError").hidden ? null : env.el("loadErrorText").textContent,
  asked: env.requests.map((r) => r.at - env.start),
  tiles: env.el("tiles").innerHTML.includes("children") || env.el("tiles").innerHTML.includes("Children"),
  overall: env.el("oTiles").innerHTML.includes("Children reached"),
  grants: (env.el("fGrant").innerHTML.match(/<option /g) || []).length,
});
"""


def _run_donor_js(body: str, figures: dict) -> dict:
    """Run ``body`` in node after DOM_PRELUDE (``page()``, ``seen()``, ``out``) and return ``out``."""
    script = DOM_PRELUDE + body + "\nconsole.log(JSON.stringify(out));\n"
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            script,
            DONOR_JS.as_uri(),
            DONOR_DOM.as_uri(),
            json.dumps(figures),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_donor_js_waits_for_a_build_in_progress_then_draws_the_figures(donor_client, world):
    """donor.js run (review R1-D1-3): a 202 "still computing" is asked again 1.5 s later, never an error; the figures
    that come are drawn (the tiles of both tabs, the grant filter) and the page is "ready", no longer busy. The
    request is the page's own part, as HTMX would ask for it. Figures that came with the page are drawn without a
    request."""
    figures = _figures(donor_client)
    assert figures["pds"]  # a funded account: every card is drawn
    out = _run_donor_js(
        """
const env = await page({ answers: [PENDING, PENDING, DATA] });
out.pending = seen(env);
out.request = env.requests[0];
const inline = await page({ inline: FIGURES });
out.inline = seen(inline);
""",
        figures,
    )
    assert out["pending"] == {
        "state": "ready",
        "busy": None,
        "error": None,
        "asked": [0, 1500, 3750],  # 1.5 s, then 1.5 times longer
        "tiles": True,
        "overall": True,
        "grants": 1 + len(figures["grants"]),  # "All your grants" and each grant
    }
    assert out["request"]["url"] == "/donor/?year=2026&_part=data"
    assert out["request"]["headers"]["HX-Request"] == "true"
    assert out["inline"]["state"] == "ready" and out["inline"]["asked"] == [] and out["inline"]["tiles"]


@needs_node
def test_donor_js_retries_a_network_error_once_then_shows_the_error_box_and_try_again_works(
    donor_client, world
):
    """A network error (or 502, 503, 504) is asked again once, 2 s later; a second failure shows the error box, the
    page no longer busy. Try again asks again and draws the figures. A refusal (403) is never asked again and says
    why; a build still not ready after the 60 s budget says it took too long."""
    figures = _figures(donor_client)
    out = _run_donor_js(
        """
const env = await page({ answers: ["network", "network"] });
out.failed = seen(env);
env.answer(DATA);
env.el("loadRetry").click();
await env.run();
out.again = seen(env);
out.restarted = await page({ answers: [{ status: 503 }, DATA] }).then(seen);
out.refused = await page({ answers: [{ status: 403 }, DATA] }).then(seen);
out.late = await page({ answers: Array(100).fill(PENDING) }).then(seen);
""",
        figures,
    )
    failed = out["failed"]
    assert failed["state"] == "error" and failed["busy"] is None and failed["asked"] == [0, 2000]
    assert failed["error"] == "The figures could not be loaded."
    assert not failed["tiles"] and not failed["overall"]
    again = out["again"]
    assert again["state"] == "ready" and again["error"] is None and len(again["asked"]) == 3
    assert again["tiles"] and again["overall"]
    assert out["restarted"]["state"] == "ready" and out["restarted"]["asked"] == [0, 2000]
    refused = out["refused"]
    assert refused["state"] == "error" and refused["asked"] == [0]
    assert refused["error"] == "You do not have access to this."
    late = out["late"]
    assert (
        late["state"] == "error"
        and late["error"] == "The figures took too long to come. Try again in a moment."
    )
    assert 5 < len(late["asked"]) < 30 and late["asked"][-1] < 60000


def test_a_failed_page_shows_the_error_box_without_the_empty_cards():
    """Figures that could not be loaded: the error box with Try again, and none of the empty cards below it, whose
    controls would act on nothing (review R1-D1-2). Both tabs: the cards of "Your contribution" and the overall tab's
    tiles and sections (its opening line stays). Try again shows them again (state "waiting")."""
    css = DONOR_CSS.read_text(encoding="utf-8")
    rule = re.search(r'main\[data-donor-state="error"\] :is\(([^)]*)\) \{([^}]*)\}', css)
    assert rule, "no rule for the error state"
    assert {s.strip() for s in rule.group(1).split(",")} == {
        "#mineContent",
        "#panel-overall > .tiles",
        "#panel-overall > .q",
    }
    assert rule.group(2).strip() == "display: none;"
    template = (
        Path(settings.BASE_DIR) / "neurodb" / "donors" / "templates" / "donors" / "page.html"
    ).read_text()
    overall = template.split('id="panel-overall"', 1)[1].split("<footer>", 1)[0]
    # every card of the overall tab is in one of its tiles or sections
    assert overall.count('class="card"') == overall.split('<section class="q"', 1)[1].count('class="card"')
    assert '<div class="tiles" id="oTiles">' in overall


def _z_index(rule: str) -> int:
    return int(re.search(r"z-index: (\d+)", rule).group(1))


def test_the_cards_keep_their_room_until_the_figures_are_drawn():
    """Browser check of the verify step (x20 world, 1366 px): with the figures in the page, the browser painted the
    empty cards before donor.js drew them and the first screen jumped (layout shift 0.45, as on the old page). The
    cards keep their room while the figures are fetched ("waiting") and while figures that came with the page are
    drawn ("drawing"); only donor.js's useData() makes the page "ready". The "Loading the figures" line is shown only
    while fetching, above the sticky filter bar (z-index 5) that used to hide it."""
    css = DONOR_CSS.read_text(encoding="utf-8")
    both = '.js main:is([data-donor-state="waiting"], [data-donor-state="drawing"])'
    for rule in ("#tiles:empty { min-height: 132px; }", "#mineContent .filters { min-height: 85px; }"):
        assert f"{both} {rule}" in css
    assert '.js main[data-donor-state="waiting"] :is(' not in css  # no room rule for "waiting" alone
    assert '.js main[data-donor-state="waiting"] .loadstate__wait { display: block; }' in css
    wait = re.search(r"\.loadstate__wait \{([^}]*)\}", css).group(1)
    filters = re.search(r"\n\.filters \{([^}]*)\}", css).group(1)
    assert "position: absolute" in wait and _z_index(wait) > _z_index(filters)
    source = DONOR_JS.read_text(encoding="utf-8")
    assert source.count('main.dataset.donorState = "ready";') == 1
    assert 'main.dataset.donorState = "ready";' in source.split("function useData(", 1)[1].split("\n}", 1)[0]


# The donor page's head script, run in node on a stand-in window: ``ran`` says whether donor.js ran (it marks the page
# html.donor-ready) before the load event, which the script then fires.
DONOR_HEAD_RUN = """
const [script, ran] = [process.argv[1], process.argv[2] === "1"];
const classes = new Set();
const listeners = {};
globalThis.document = {
  documentElement: {
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    setAttribute() {},
  },
};
globalThis.window = { addEventListener: (type, fn) => (listeners[type] ||= []).push(fn), matchMedia: () => ({ matches: false }) };
globalThis.localStorage = { getItem: () => null };
new Function(script)();
const out = { beforeLoad: [...classes] };
if (ran) classes.add("donor-ready");
(listeners.load || []).forEach((fn) => fn({ type: "load" }));
out.afterLoad = [...classes].sort();
console.log(JSON.stringify(out));
"""


@needs_node
def test_without_donor_js_the_page_says_its_charts_cannot_be_drawn(donor_client, world):
    """The page's head marks that scripts run (html.js), and donor.css then shows "Loading the figures…" and grey
    cards while donor.js fetches the figures. Where donor.js never ran (it failed to load or was blocked), they stayed
    for ever, with no error box; the old page showed its empty cards and claimed nothing (review R3-critic-2).
    donor.js marks the page html.donor-ready once it has started; at the load event the head drops html.js from a page
    without that mark, so no grey card and no "Loading" line are left, and the sentence a browser without JavaScript
    gets is shown instead, as a normal element (a <noscript> never shows where scripts run)."""
    template = (
        Path(settings.BASE_DIR) / "neurodb" / "donors" / "templates" / "donors" / "page.html"
    ).read_text()
    head = template.split("</head>", 1)[0]
    script = head[head.index("<script nonce=") :].split(">", 1)[1].split("</script>", 1)[0]

    def run_head(ran: bool) -> dict:
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", DONOR_HEAD_RUN, script, "1" if ran else "0"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert run_head(ran=False) == {"beforeLoad": ["js"], "afterLoad": []}
    assert run_head(ran=True) == {"beforeLoad": ["js"], "afterLoad": ["donor-ready", "js"]}
    # donor.js marks the page once it has started (the figures asked for or drawn), whatever the answer
    out = _run_donor_js(
        """
out.fetched = (await page({ answers: ["network", "network"] })).classes();
out.inline = (await page({ inline: FIGURES })).classes();
""",
        _figures(donor_client),
    )
    assert out == {"fetched": ["donor-ready", "js"], "inline": ["donor-ready", "js"]}
    source = DONOR_JS.read_text(encoding="utf-8")
    assert source.rstrip().endswith('document.documentElement.classList.add("donor-ready");')
    # the sentence: a normal element, shown only where html.js is missing (no JavaScript, or donor.js never ran)
    html = text(donor_client.get(PAGE))
    sentence = "The charts of this page are drawn in your browser by a script that did not run"
    assert f'<p class="loadstate__nojs">{sentence}' in html and "<noscript><p>" not in html
    css = DONOR_CSS.read_text(encoding="utf-8")
    assert re.search(r"\n\.loadstate__nojs \{ display: none;", css)
    assert "html:not(.js) .loadstate__nojs { display: block; }" in css
    # the "Loading" line and the grey cards are drawn only under html.js
    for selector, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
        if "loadstate__wait" in selector and "display: block" in _body or "donor-shimmer" in _body:
            assert selector.strip().startswith(".js "), selector


def test_the_part_answers_a_donor_htmx_request_that_staff_pages_refuse(donor_client, world):
    """The donor lock-out refuses every HTMX request but the donor page's (SPEC7 §1.1): the part is that page's."""
    assert donor_client.get(reverse("reports:overview"), HTTP_HX_REQUEST="true").status_code == 403
    assert donor_client.get(f"{PAGE}?year=2026&_part=data", HTTP_HX_REQUEST="true").status_code == 200
