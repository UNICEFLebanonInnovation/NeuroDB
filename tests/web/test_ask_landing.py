"""Ask NeuroDB opens first: signing in, the site address and the logo lead to Ask NeuroDB while the AI is on and
the person's menu shows it, else to Home, which moved to /home/ and still opens from its menu row. Ask NeuroDB
is the first row of the full and the focused menu, and its page starts with a welcome, the question box, the
example questions by topic, the person's recent questions and links to the pages people open most."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.models import Group
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VIEWER
from neurodb.assistant import views as assistant_views
from neurodb.assistant.models import AssistantQuestion
from neurodb.indicators.services import navigation
from tests.web.menu_rows import rows

pytestmark = pytest.mark.django_db

ROOT = "/"
HOME = "/home/"
ASK = "/ask/"
PASSWORD = "pass-123456-x"
APP_CSS = Path(django_settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "app.css"
ASK_JS = Path(django_settings.BASE_DIR) / "neurodb" / "web" / "static" / "js" / "ask.js"


def _person(username: str, role: str = VIEWER, *, is_staff=False, is_superuser=False, **fields) -> User:
    user = User.objects.create_user(
        username=username, password=PASSWORD, is_staff=is_staff, is_superuser=is_superuser, **fields
    )
    user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def ai_on(settings):
    settings.AI_ASSISTANT_ENABLED = True
    settings.OPENAI_API_KEY = "test-not-a-key"


@pytest.fixture
def ai_off(settings):
    settings.AI_ASSISTANT_ENABLED = False


def _start(html: str) -> str:
    """The Ask page's start: from the welcome to the end of the page's own content."""
    return html.split('class="ask ask--home', 1)[1].split('<template id="ask-turn-template">', 1)[0]


def _quick_links(html: str) -> list[tuple[str, str]]:
    """(label, address) of the start page's links to other pages, in order."""
    block = html.split('class="ask-home__links"', 1)[1].split("</nav>", 1)[0]
    return [
        (label.strip(), href)
        for href, label in re.findall(
            r'<a class="ask-home__link" href="([^"]+)">.*?<span>([^<]+)</span>', block, re.S
        )
    ]


# ------------------------------------------------------------------------------------------- the addresses
def test_home_has_its_own_address_and_the_root_its_own_name():
    assert reverse("reports:overview") == HOME
    assert reverse("reports:root") == ROOT
    assert django_settings.LOGIN_REDIRECT_URL == "reports:root"


def test_with_the_ai_on_the_root_opens_ask_neurodb(client, roles, ai_on):
    client.force_login(_person("asker"))
    response = client.get(ROOT)
    assert response.status_code == 302 and response["Location"] == reverse("assistant:ask")
    assert client.get(response["Location"]).status_code == 200  # no loop: the page itself


def test_with_the_ai_off_the_root_opens_home(client, roles, ai_off):
    client.force_login(_person("no-ai"))
    response = client.get(ROOT)
    assert response.status_code == 302 and response["Location"] == HOME
    assert client.get(HOME).status_code == 200


def test_staff_with_the_ai_off_open_home_although_their_menu_shows_ask(client, roles, ai_off):
    """Ask NeuroDB shows to staff while the AI is off (its page says it is not set up): they land on Home."""
    client.force_login(_person("staff-no-ai", is_staff=True))
    assert client.get(ROOT)["Location"] == HOME


def test_a_person_whose_menu_leaves_ask_out_opens_home(client, roles, ai_on, menu_choice):
    menu_choice(VIEWER, sorted(navigation.FOCUSED - {"assistant:ask"}))
    client.force_login(_person("no-ask-row"))
    assert client.get(ROOT)["Location"] == HOME


def test_a_superuser_opens_ask_neurodb(client, roles, ai_on):
    client.force_login(_person("root-like", is_staff=True, is_superuser=True))
    assert client.get(ROOT)["Location"] == reverse("assistant:ask")


def test_a_donor_account_is_unchanged(client, db, ai_on):
    from tests.donors.test_donor_access import make_donor

    donor = make_donor(username="landing-donor")
    client.force_login(donor.user)
    assert client.get(ROOT)["Location"] == reverse("donors:page")
    assert client.get(HOME)["Location"] == reverse("donors:page")


def test_a_visitor_still_gets_the_public_landing_page_at_the_root(client, db, ai_on):
    response = client.get(ROOT)
    assert response.status_code == 200 and 'id="access"' in response.content.decode()
    home = client.get(HOME)  # Home itself is behind sign-in, and comes back to it
    assert home.status_code == 302 and home["Location"].startswith(reverse("account_login"))
    assert "next=/home/" in home["Location"]


@pytest.mark.parametrize(
    ("query", "page"),
    [
        ("year=2025", "/country-overview/?year=2025"),  # the old country overview's filters
        ("review=latest", "/daily-review/?review=latest"),  # a tab of the old daily review card
        ("section=Education", "/home/?section=Education"),  # Home's own filter, from a link of before
        ("section=", "/home/?section="),
        ("_eager=1", "/home/?_eager=1"),  # an old print or no-JavaScript link of Home
    ],
)
def test_an_old_link_to_the_root_with_a_query_keeps_its_page(client, roles, ai_on, query, page):
    client.force_login(_person("old-link"))
    assert client.get(f"{ROOT}?{query}")["Location"] == page


def test_an_open_tab_of_home_at_the_root_still_gets_its_parts(client, roles, ai_on):
    client.force_login(_person("open-tab"))
    response = client.get(f"{ROOT}?section=&_part=reporting", HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and response["X-NeuroDB-Part"] == "reporting"


# ------------------------------------------------------------------------------------------- signing in
def test_signing_in_opens_the_landing_then_ask_neurodb(client, roles, ai_on):
    _person("signs-in")
    response = client.post(reverse("account_login"), {"login": "signs-in", "password": PASSWORD})
    assert response.status_code == 302 and response["Location"] == ROOT
    assert client.get(ROOT)["Location"] == reverse("assistant:ask")


def test_signing_in_with_the_ai_off_ends_on_home(client, roles, ai_off):
    _person("signs-in-off")
    response = client.post(reverse("account_login"), {"login": "signs-in-off", "password": PASSWORD})
    assert response["Location"] == ROOT
    assert client.get(ROOT)["Location"] == HOME


def test_signing_in_from_a_page_goes_back_to_it(client, roles, ai_on):
    _person("signs-in-next")
    response = client.post(
        reverse("account_login") + "?next=/brief/", {"login": "signs-in-next", "password": PASSWORD}
    )
    assert response["Location"] == "/brief/"


def test_a_donor_still_signs_in_to_the_donor_page(client, db, ai_on):
    from tests.donors.test_donor_access import PASSWORD as DONOR_PASSWORD
    from tests.donors.test_donor_access import make_donor

    donor = make_donor(username="landing-donor-sign-in")
    response = client.post(
        reverse("account_login"), {"login": donor.user.username, "password": DONOR_PASSWORD}
    )
    assert response["Location"] == reverse("donors:page")


# ------------------------------------------------------------------------------------------- the menus
def test_home_still_opens_from_its_menu_row(client, roles, ai_on):
    client.force_login(_person("home-row"))
    html = client.get(reverse("assistant:ask")).content.decode()
    links = {row["label"]: row["href"] for row in rows(html)}
    assert links["Home"] == HOME
    home = client.get(HOME).content.decode()
    current = [row["label"] for row in rows(home) if 'aria-current="page"' in row["attrs"]]
    assert current == ["Home"]


def test_the_logo_opens_the_landing(client, roles, ai_on):
    client.force_login(_person("logo"))
    html = client.get(HOME).content.decode()
    assert '<a class="brand" href="/">' in html


def test_ask_neurodb_is_the_first_row_of_the_focused_menu(client, roles, ai_on):
    client.force_login(_person("focused-first"))
    labels = [row["label"] for row in rows(client.get(HOME).content.decode())]
    assert labels[:2] == ["Ask NeuroDB", "Home"]
    assert navigation.FOCUS[:2] == ("assistant:ask", "reports:overview")


def test_ask_neurodb_is_the_first_row_of_the_full_menu(client, roles, ai_on):
    superuser = _person("full-first", is_staff=True, is_superuser=True)
    client.force_login(superuser)
    labels = [row["label"] for row in rows(client.get(HOME).content.decode())]
    assert labels[:5] == ["Ask NeuroDB", "Home", "For you", "Daily review", "What's new"]
    assert [str(item.label) for item in navigation.MENU[0].items][:2] == ["Ask NeuroDB", "Home"]


def test_the_menu_page_lists_ask_neurodb_first_at_the_top_of_the_menu():
    top = [entry["key"] for entry in navigation.entries() if str(entry["group"]) == "Top of the menu"]
    assert top == ["assistant:ask", "reports:overview"]


def test_the_search_lists_ask_neurodb_first_among_the_pages(roles, ai_on):
    superuser = _person("pages-first", is_staff=True, is_superuser=True)
    labels = [str(page["label"]) for page in navigation.pages(superuser)]
    assert labels[:2] == ["Ask NeuroDB", "Home"]


# ------------------------------------------------------------------------------------------- the start page
def test_the_start_page_welcomes_the_person_by_first_name(client, roles, ai_on):
    client.force_login(_person("named", first_name="Quillon"))
    html = client.get(reverse("assistant:ask")).content.decode()
    start = _start(html)
    assert "Welcome, Quillon" in start
    title = html.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "Quillon" not in title  # the title is what the Help assistant sends with a question
    assert start.count("Quillon") == 1 and html.count("Quillon") == 2  # and the user menu's own name
    assert not re.search(r'data-[\w-]+="[^"]*Quillon', html)  # in no attribute a script reads


def test_the_start_page_welcomes_an_account_without_a_first_name(client, roles, ai_on):
    client.force_login(_person("unnamed"))
    start = _start(client.get(reverse("assistant:ask")).content.decode())
    assert "Welcome to Ask NeuroDB" in start and "Welcome," not in start


def test_the_start_page_says_what_ask_neurodb_does_and_puts_the_box_under_the_welcome(client, roles, ai_on):
    client.force_login(_person("box"))
    html = client.get(reverse("assistant:ask")).content.decode()
    start = _start(html)
    assert 'class="ask ask--home is-empty"' in html  # the start layout until the first question (ask.js)
    welcome = start.index('class="ask-home__welcome"')
    lead = start.index('class="ask-home__lead"')
    form = start.index('id="ask-form"')
    cards = start.index('id="ask-intro"')
    assert welcome < lead < form < cards  # the box under the welcome, the cards under the box
    assert start.count('id="ask-form"') == 1 and start.count("<textarea") == 1  # one form, not two
    # the parts ask.js reads keep their ids
    for element in ("ask-form", "ask-input", "ask-thread", "ask-intro", "ask-submit", "ask-stop", "ask-new"):
        assert f'id="{element}"' in html, element


def test_the_examples_are_cards_grouped_by_topic(client, roles, ai_on):
    client.force_login(_person("cards"))
    start = _start(client.get(reverse("assistant:ask")).content.decode())
    topics = [str(topic["title"]) for topic in assistant_views.EXAMPLES]
    assert topics == ["Partner reporting", "Funds and donors", "Field monitoring", "Documents and data"]
    for topic in assistant_views.EXAMPLES:
        assert topic["icon"] and topic["questions"]
        assert f">{topic['title']}</h3>" in start
        for question in topic["questions"]:
            # a card: its topic's icon and the question, asked as it is when clicked
            card = re.search(
                rf'<button type="button" class="ask-card" data-ask-example="{re.escape(question)}">'
                rf'\s*<svg class="icon[^"]*" aria-hidden="true" focusable="false"><use href="#i-{topic["icon"]}">'
                rf"</use></svg>\s*<span>{re.escape(question)}</span>",
                start,
            )
            assert card, question
    questions = [q for topic in assistant_views.EXAMPLES for q in topic["questions"]]
    assert "Which partners have progress reports overdue?" in questions
    assert "Is the eTools data up to date?" in questions
    assert len(questions) == len(set(questions))


def test_the_recent_questions_are_a_short_list_on_the_start_page(client, roles, ai_on):
    person = _person("recent")
    for n in range(8):
        AssistantQuestion.objects.create(
            user=person, question=f"Fake question {n}?", status=AssistantQuestion.Status.ANSWERED
        )
    client.force_login(person)
    start = _start(client.get(reverse("assistant:ask")).content.decode())
    recent = start.split("Your recent questions", 1)[1]
    asked = re.findall(r'data-ask-example="(Fake question \d\?)"', recent)
    assert asked == [f"Fake question {n}?" for n in (7, 6, 5, 4, 3, 2)]


def test_the_quick_links_follow_the_person_s_menu(client, roles, ai_on, menu_choice):
    client.force_login(_person("links"))
    html = client.get(reverse("assistant:ask")).content.decode()
    # the focused menu: Programme documents is not in it
    assert _quick_links(html) == [
        ("Home", HOME),
        ("Progress reports", reverse("reports:partner_reporting")),
        ("Partner monitoring", reverse("reports:pd_monitoring")),
    ]
    menu_choice(VIEWER, [*navigation.FOCUSED, "reports:programmes"])
    html = client.get(reverse("assistant:ask")).content.decode()
    assert [label for label, _url in _quick_links(html)] == [
        "Home",
        "Progress reports",
        "Partner monitoring",
        "Programme documents",
    ]
    menu_choice(VIEWER, ["assistant:ask", "reports:pd_monitoring"])
    html = client.get(reverse("assistant:ask")).content.decode()
    assert _quick_links(html) == [("Partner monitoring", reverse("reports:pd_monitoring"))]


def test_a_superuser_gets_every_quick_link(client, roles, ai_on):
    client.force_login(_person("links-super", is_staff=True, is_superuser=True))
    html = client.get(reverse("assistant:ask")).content.decode()
    assert [label for label, _url in _quick_links(html)] == [
        "Home",
        "Progress reports",
        "Partner monitoring",
        "Programme documents",
    ]


def test_no_quick_links_row_when_the_menu_shows_none_of_them(client, roles, ai_on, menu_choice):
    menu_choice(VIEWER, ["assistant:ask"])
    client.force_login(_person("no-links"))
    html = client.get(reverse("assistant:ask")).content.decode()
    assert 'class="ask-home__links"' not in html


def test_the_note_keeps_its_meaning_shorter(client, roles, ai_on):
    client.force_login(_person("note"))
    html = client.get(reverse("assistant:ask")).content.decode()
    note = " ".join(html.split('class="ask__note', 1)[1].split("</p>", 1)[0].split())
    assert "can make mistakes" in note and "check important figures on the linked pages" in note
    assert "This conversation and the data looked up go to the OpenAI API" in note
    assert "do not enter personal data" in note and "Up to 30 questions an hour." in note


def test_the_not_set_up_state_is_unchanged(client, roles, ai_off):
    client.force_login(_person("off-page", is_staff=True))
    html = client.get(reverse("assistant:ask")).content.decode()
    assert "The AI assistant is not set up yet" in html
    main = html.split('id="main"', 1)[1].split("</main>", 1)[0]
    assert "ask--home" not in html and "Welcome" not in main


# ------------------------------------------------------------------------------------------- the styles
def _rules(css: str, selector: str) -> list[str]:
    return re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css)


def test_the_start_page_styles_use_the_colour_tokens_and_stack_on_a_phone():
    css = APP_CSS.read_text()
    start = "\n".join(
        line
        for line in css.splitlines()
        if ".ask-home" in line or ".ask--home" in line or ".ask-card" in line
    )
    assert start, "no styles for the start page"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", start)  # colours from the tokens only (dark mode)
    # the cards' grid never needs more than the screen: one column on a phone
    assert any("minmax(min(100%," in rule for rule in _rules(css, ".ask-home__topics"))
    # the box under the welcome is not stuck to the bottom of the screen until the first question
    assert any("position: static" in rule for rule in _rules(css, ".ask--home.is-empty .ask__form"))


def test_the_start_page_s_motion_stops_when_less_motion_is_asked():
    css = APP_CSS.read_text()
    reduced = [
        block.split("\n}", 1)[0] for block in css.split("@media (prefers-reduced-motion: reduce) {")[1:]
    ]
    assert any(".ask-card" in block and "transition: none" in block for block in reduced)


# ------------------------------------------------------------------------------------------- ask.js
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ask_js_moves_the_box_down_once_a_conversation_starts_and_back_up_for_a_new_one():
    script = r"""
const el = (name) => {
  const e = { name, textContent: "", innerHTML: "", hidden: false, value: "", style: {}, dataset: {}, scrollHeight: 20,
    children: [], handlers: {}, classList: { replace() {}, toggle() {} },
    append(child) { this.children.push(child); }, querySelectorAll: () => [], querySelector: () => null,
    addEventListener(type, fn) { this.handlers[type] = fn; }, focus() {}, setSelectionRange() {}, requestSubmit() {},
    remove() {} };
  return e;
};
const store = { "ask-test": JSON.stringify({ conversation: "c", turns: [{ question: "Fake?", html: "<p>Fake</p>" }] }) };
globalThis.window = { crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000000" },
  sessionStorage: { getItem: (k) => store[k] ?? null, setItem(k, v) { store[k] = v; }, removeItem(k) { delete store[k]; } } };
const turn = { cloneNode: () => ({ querySelector: () => el("part"), remove() {} }) };
const template = { content: { firstElementChild: turn } };
globalThis.document = { querySelector: () => null, createElement: () => el("p") };
const mod = await import(process.argv[1]);
const named = {};
const classes = new Set(["is-empty"]);
const root = { dataset: { module: "ask", historyKey: "ask-test" },
  classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
  querySelector: (sel) => {
    if (sel === "template[data-ask-turn]") return template;
    const m = sel.match(/data-ask-part="(\w+)"/); return m ? (named[m[1]] ??= el(m[1])) : null; },
  querySelectorAll: () => [] };
mod.init(root);
const out = { kept: classes.has("is-empty"), introKept: named.intro.hidden };
named.thread.querySelectorAll = () => [];
named.new.handlers.click();
out.reset = classes.has("is-empty");
out.introReset = named.intro.hidden;
console.log(JSON.stringify(out));
"""
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script, ASK_JS.as_uri()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip())
    # a conversation kept for the tab: the start is put away; New conversation brings it back
    assert result == {"kept": False, "introKept": True, "reset": True, "introReset": False}
