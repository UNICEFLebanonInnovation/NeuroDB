"""The public landing page's design rules: the way in is NeuroDB's own sign-in, nothing on it runs but
the page's own scripts, every picture is a static file of this site, the live figures are aggregate
counts shown only when switched on, and the figures it quotes about NeuroDB's rules are read from the code
that applies them (so they cannot drift)."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from PIL import Image

from neurodb.fmm.models import RuleSetting
from neurodb.review import checks
from neurodb.watch.detectors import deadlines, money
from neurodb.web import views

pytestmark = pytest.mark.django_db

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
PHOTOS = ("learning-activities", "child-health-care", "digital-skills-classroom", "digital-learning-access")


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


class _Tags(HTMLParser):
    """Every start tag of a page (those inside <template>s too), as (tag, attributes)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {key: value or "" for key, value in attrs}))

    handle_startendtag = handle_starttag


def start_tags(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser.tags


def tags(html: str, name: str) -> list[dict[str, str]]:
    return [attrs for tag, attrs in start_tags(html) if tag == name]


def _landing(client):
    response = client.get(reverse("landing"))
    assert response.status_code == 200
    return response, response.text


# ------------------------------------------------------------------------------------------ access
def test_visitors_get_the_page_without_signing_in(client, db):
    response, html = _landing(client)
    assert "Sign in" in html and response.templates[0].name == "landing.html"
    assert client.get("/").status_code == 200  # the site root shows it to visitors too


def test_every_way_in_is_neurodbs_own_sign_in(client, db):
    _, html = _landing(client)
    login = reverse("account_login")
    links = [a for a in tags(html, "a") if a.get("href") == login]
    # the nav, the hero, the staff role (drawn and its state) and the closing band
    assert len(links) == 5, links
    assert {a.get("class", "") for a in links} >= {"navcta", "btn btn-ghost", "btn btn-blue", "endingbtn"}
    assert "azurewebsites" not in html and "Open NeuroDB" not in html


def test_a_signed_in_visitor_is_offered_neurodb_itself(client_viewer, db):
    _, html = _landing(client_viewer)
    home = reverse("reports:overview")
    assert reverse("account_login") not in html
    assert html.count("Open NeuroDB") == 4  # nav, staff role (drawn and its state), closing band
    assert "Open Home" in html
    opened = [a for a in tags(html, "a") if a.get("href") == home]
    assert {a.get("class", "") for a in opened} >= {"navcta", "btn btn-ghost", "btn btn-blue", "endingbtn"}


# ------------------------------------------------------------------------------------------ what runs
def test_no_inline_handler_and_every_script_is_static_or_carries_the_nonce(client, db):
    response, html = _landing(client)
    policy = response["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", policy)[1]
    assert (
        "script-src 'self' 'nonce-" in policy
        and "unsafe-inline" not in policy.split("script-src")[1].split(";")[0]
    )
    handlers = [
        (tag, key) for tag, attrs in start_tags(html) for key in attrs if re.fullmatch(r"on[a-z]+", key)
    ]
    assert handlers == []
    assert "javascript:" not in html
    scripts = tags(html, "script")
    assert scripts, "the page loads its script"
    for script in scripts:
        assert script.get("nonce") == nonce or script.get("src", "").startswith(settings.STATIC_URL), script
        if script.get("src"):
            assert script["src"].startswith(settings.STATIC_URL) and script.get("type") == "module"
    # the module imports nothing: the page has no import map
    assert not re.search(r"^\s*import\b|\bimport\(", (STATIC / "js" / "landing.js").read_text(), re.M)


def test_every_picture_is_a_static_file_with_its_text_and_size(client, db):
    _, html = _landing(client)
    images = tags(html, "img")
    assert len(images) >= 11  # logo twice, hero, four cards, the portrait, three story photos
    for img in images:
        src = img["src"]
        assert src.startswith(settings.STATIC_URL), src
        assert (STATIC / src[len(settings.STATIC_URL) :]).is_file(), src
        assert img.get("alt", "").strip(), src
        assert img.get("width", "").isdigit() and img.get("height", "").isdigit(), src
    assert "http://" not in "".join(i["src"] for i in images) and "https://" not in "".join(
        i["src"] for i in images
    )
    assert "url(" not in (STATIC / "css" / "landing.css").read_text()  # no picture from a stylesheet either


@pytest.mark.parametrize("name", PHOTOS)
def test_the_photos_are_web_sized_progressive_jpegs_whose_size_the_page_states(client, db, name):
    path = STATIC / "img" / "landing" / f"{name}.jpg"
    with Image.open(path) as photo:
        assert photo.format == "JPEG" and photo.info.get("progressive")
        assert max(photo.size) <= 1600
        assert "exif" not in photo.info and "comment" not in photo.info
        width, height = photo.size
    _, html = _landing(client)
    for img in tags(html, "img"):
        if img["src"].endswith(f"img/landing/{name}.jpg"):
            assert (img["width"], img["height"]) == (str(width), str(height))


# ------------------------------------------------------------------------------------------ live figures
def test_the_live_highlights_show_only_when_switched_on(client, hierarchy, activityinfo_in_use, settings):
    _, html = _landing(client)
    assert "LIVE PUBLIC HIGHLIGHTS" in html and 'data-count="4"' in html
    assert "aggregate counts only" in html and "About data volume:" not in html  # in place of the note
    assert "Partner A" not in html  # counts, never names
    settings.PUBLIC_LANDING_STATS = False
    cache.clear()
    _, html = _landing(client)
    assert "LIVE PUBLIC HIGHLIGHTS" not in html and "data-count" not in html
    assert "About data volume:" in html  # the design's note stays


def test_the_fixed_figures_never_use_the_live_figures_attribute(client, db):
    """``data-count`` marks the live public counts only: the rule figures count up with ``data-tally``."""
    _, html = _landing(client)
    tallies = re.findall(r'data-tally="(\d+)"', html)
    assert tallies and "data-count" not in html


# ------------------------------------------------------------------------------------------ rule figures
def test_the_rule_figures_are_neurodbs_own(client, db):
    """The figures the design quoted, checked against the code: 14 morning checks (review.checks), AI in 13
    of the concept note's 14 areas, the field visit rules switched on (six of them AI checks), 90 days of
    grant warning with USD 10,000 unspent, PDs ending in 60 days, reports due in 14, HACT gaps from
    1 October. The page reads them from the code, so these pins say what it shows today."""
    assert len(checks.CHECKS) == 14
    assert (money.GRANT_DAYS, deadlines.PD_ENDING_DAYS, deadlines.REPORT_DAYS) == (90, 60, 14)
    assert money.HACT_FROM == (10, 1) and settings.WATCH_GRANT_MIN_UNSPENT == 10_000
    assert (views.AI_AREAS, views.ANALYSIS_AREAS) == (13, 14)
    _seed_rules()
    _, html = _landing(client)
    assert '<strong data-tally="14">14</strong><span>morning checks</span>' in html
    assert '<strong data-tally="13">13</strong><span>of 14 analysis areas where AI can help</span>' in html
    assert "<small>Runs only where AI is switched on</small>" in html
    assert '<strong data-tally="12">12</strong><span>field-visit quality checks</span>' in html
    assert "<small>6 rules-based; 6 AI-assisted if approved</small>" in html
    assert "Visit-report quality is assessed against 12 rules" in html
    assert "6 fixed checks" in html and "6 AI checks" in html and "--split:180.0deg" in html
    assert '<strong data-tally="90">90</strong><span>days of early grant warning</span>' in html
    assert "grant expiry 90 days; programme document ending 60 days; partner report due 14 days" in html
    assert "--barw:100.000%" in html and "--barw:66.667%" in html and "--barw:15.556%" in html
    assert (
        "The grant alert applies to USD 10,000 or more unspent; HACT gaps are flagged from 1 October." in html
    )
    assert "flags a grant 90 days before expiry when at least USD 10,000 remains unspent" in html


def test_the_design_copy_that_was_not_true_is_corrected(client, db):
    _, html = _landing(client)
    for wrong in (
        "morning programme checks",  # four of the fourteen check the data pipeline
        "analysis areas with potential AI support",
        "Activated only after required clearance",
        "never child-level registration records",  # NeuroDB keeps Makani wellbeing flags per child
        "without receiving child-level registration records",
        "assigned to a responsible team",  # Watch tells the sections the grant's PDs belong to
        "AI assistance is read-only",  # AI check verdicts lower quality scores
        "Moustafa",  # no child's name on the page
    ):
        assert wrong not in html, wrong
    assert (
        "NeuroDB’s programme figures come from aggregated totals, not from children’s registration records."
        in html
    )
    assert "The source-linked alert is sent to the sections it concerns." in html
    assert "AI assistance never changes source data" in html
    assert "A young participant · " in html


def test_the_rule_counts_follow_what_administrators_switch_on(client, db, settings):
    _seed_rules()
    RuleSetting.objects.filter(code="R3").update(enabled=False)  # an AI check switched off
    settings.WATCH_GRANT_MIN_UNSPENT = 25_000
    _, html = _landing(client)
    assert (
        '<strong data-tally="11">11</strong>' in html and "6 rules-based; 5 AI-assisted if approved" in html
    )
    assert "5 AI checks" in html and "USD 25,000 or more unspent" in html
    RuleSetting.objects.update(enabled=False)
    _, html = _landing(client)
    assert "field-visit quality checks" not in html and "quality rules" not in html
    assert "the rules administrators switch on" in html


def _seed_rules():
    """FMS Lebanon's rule set as migration fmm/0013 seeds it (a flush by an earlier test may have emptied it)."""
    from neurodb.fmm.lebanon import RULE_SET

    for rule in RULE_SET["rules"]:
        RuleSetting.objects.update_or_create(
            code=rule["id"],
            defaults={"label": rule["name"][:80], "type": rule["type"], "enabled": rule["enabled"]},
        )
    assert RuleSetting.objects.filter(enabled=True).count() == 12
    assert RuleSetting.objects.filter(enabled=True, type=RuleSetting.Type.NARRATIVE).count() == 6
