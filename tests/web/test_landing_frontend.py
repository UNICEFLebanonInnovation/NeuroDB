"""The public landing page's script (static/js/landing.js), run in node on the page the server renders
(tests/web/landing_dom.mjs builds it from the HTML, parsed here): the story, the roles and the sample insights
fill every field of the state chosen and press only its button, the evidence and the mobile menu say whether
they are open, Escape closes the menu and gives focus back to its button, Tab goes from the button to the
links it opened, Back never drops keyboard focus to the page, the story's and the role's text are live
regions once drawn, nothing moves under prefers-reduced-motion, and a photo that failed before the script ran
is hidden. The contract between the page and the script is checked without node: every field a state fills
has its place in its region, and the controls the page names exist."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

pytestmark = pytest.mark.django_db

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# the interactive parts' states (<template>s) and the region each fills
REGIONS = {"data-story-state": "storyboard", "data-role-state": "rolePanel", "data-insight-state": "insights"}


class _Tree(HTMLParser):
    """The page as a JSON-able tree: ["tag", {attrs}, ...children], text as strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["#root", {}]
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = [tag, {key: value or "" for key, value in attrs}]
        self.stack[-1].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].append([tag, {key: value or "" for key, value in attrs}])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].append(data)


def tree_of(html: str) -> list:
    parser = _Tree()
    parser.feed(html)
    parser.close()
    return next(node for node in parser.root[2:] if isinstance(node, list) and node[0] == "html")


def _elements(node, inside_template=False):
    """(element, inside a <template>) for every element under ``node``, in document order."""
    for child in node[2:]:
        if isinstance(child, list):
            yield child, inside_template
            yield from _elements(child, inside_template or child[0] == "template")


def _find(root, **attrs):
    return [el for el, _ in _elements(root) if all(el[1].get(k) == v for k, v in attrs.items())]


@pytest.fixture
def landing_html(client, db):
    cache.clear()
    response = client.get(reverse("landing"))
    assert response.status_code == 200
    yield response.text
    cache.clear()


@pytest.fixture
def landing_tree(landing_html, tmp_path):
    path = tmp_path / "landing.json"
    path.write_text(json.dumps(tree_of(landing_html)))
    return path


# ------------------------------------------------------------------------------------------ the contract
def test_every_field_a_state_fills_has_its_place_in_its_region(landing_html):
    root = tree_of(landing_html)
    for attribute, region_id in REGIONS.items():
        (region,) = [
            el for el, in_template in _elements(root) if el[1].get("id") == region_id and not in_template
        ]
        places = {
            el[1]["data-f"] for el, in_template in _elements(region) if "data-f" in el[1] and not in_template
        }
        states = [el for el, _ in _elements(root) if el[0] == "template" and attribute in el[1]]
        assert states, attribute
        for state in states:
            fields = {el[1]["data-f"] for el, _ in _elements(state) if "data-f" in el[1]}
            assert fields and fields <= places, (attribute, state[1], fields - places)


def test_the_skip_link_landmarks_and_named_controls_exist(landing_html):
    root = tree_of(landing_html)
    ids = {el[1]["id"] for el, in_template in _elements(root) if "id" in el[1] and not in_template}
    assert _find(root, **{"class": "skip", "href": "#main"}) and "main" in ids
    tags = [el[0] for el, in_template in _elements(root) if not in_template]
    assert {"header", "nav", "main", "footer"} <= set(tags)
    for el, _ in _elements(root):
        if "aria-controls" in el[1]:
            assert el[1]["aria-controls"] in ids, el[1]
    for control in ("mobileMenu", "evidenceToggle"):
        (el,) = _find(root, id=control)
        assert el[1]["aria-expanded"] == "false" and el[1]["aria-controls"] in ids
    # the menu button comes before the links it opens: Tab goes from it to the first link
    order = [el[1].get("id") for el, _ in _elements(root)]
    assert order.index("mobileMenu") < order.index("navLinks")
    # the story's picture shows its photo's alt, caption and credit: not one role="img" over them
    (canvas,) = _find(root, id="visualCanvas")
    assert "role" not in canvas[1] and "aria-label" not in canvas[1]
    # each photo link is named by its photo, not by one label shared by four links to four stories
    photo_links = _find(root, **{"class": "field-photo-image"})
    assert len(photo_links) == 4 and all("aria-label" not in a[1] for a in photo_links)
    assert len({a[1]["href"] for a in photo_links}) == 4
    for a in photo_links:
        (img,) = [el for el, _ in _elements(a) if el[0] == "img"]
        assert img[1]["alt"].strip()
    # the regions the script makes live once drawn
    assert {"storyText", "roleText"} <= ids
    for button in [el for el, _ in _elements(root) if el[0] == "button"]:
        assert button[1].get("type") == "button", button[1]


# ------------------------------------------------------------------------------------------ in node
PRELUDE = """
const dom = await import(process.argv[1]);
const fs = await import("node:fs");
// landing.js has no import or export: node would take the file for CommonJS and run it once only, so each
// page runs its source as a module of its own (the browser loads it with type="module")
const source = fs.readFileSync(new URL(process.argv[2]), "utf8");
const tree = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
let pages = 0;
/** A fresh page and a fresh run of landing.js on it; `before(env)` runs first (to set a photo's state). */
async function page(options = {}, before = () => {}) {
  const env = dom.installPage(tree, options);
  before(env);
  pages += 1;
  await import("data:text/javascript," + encodeURIComponent(`${source}\n// page ${pages}`));
  return env;
}
const text = (el) => (el ? el.textContent.replace(/\\s+/g, " ").trim() : null);
const pressed = (els) => els.filter((b) => b.getAttribute("aria-pressed") === "true");
"""


def run(tree_path: Path, body: str) -> dict:
    """Run ``body`` in node after PRELUDE (``page()`` and ``out``) and return ``out``."""
    script = PRELUDE + body + "\nconsole.log(JSON.stringify(out));\n"
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            script,
            (Path(__file__).parent / "landing_dom.mjs").as_uri(),
            (STATIC / "js" / "landing.js").as_uri(),
            str(tree_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_each_story_and_stage_fills_every_field_and_presses_only_its_button(landing_tree):
    out = run(
        landing_tree,
        """
const { doc, press } = await page();
const board = doc.querySelector("#storyboard");
const canvas = doc.querySelector("#visualCanvas");
const next = doc.querySelector("#next");
out.live = doc.querySelector("#storyText").getAttribute("aria-live");
out.stages = [];
const scenarios = doc.querySelectorAll("button[data-scenario]");
for (const button of scenarios) {
  button.focus();
  press("Enter");
  for (let step = 0; step < 4; step++) {
    const state = doc.querySelector(`template[data-story-state="${button.dataset.scenario}"][data-story-stage="${step}"]`).content;
    const fields = {};
    for (const f of state.querySelectorAll("[data-f]")) fields[f.dataset.f] = [text(f), text(board.querySelector(`[data-f="${f.dataset.f}"]`))];
    out.stages.push({
      scenario: button.dataset.scenario,
      step,
      pressed: pressed(scenarios).map((b) => b.dataset.scenario),
      fields,
      photo: Boolean(canvas.querySelector("img")),
      role: canvas.getAttribute("role"),
      current: doc.querySelectorAll(".stepbtn[data-step]").filter((s) => s.getAttribute("aria-current") === "step").map((s) => s.dataset.step),
      number: text(doc.querySelector("#stageCount")),
      next: text(next),
    });
    next.focus();
    press("Enter");
  }
  out[`${button.dataset.scenario}-after-last`] = text(board.querySelector('[data-f="title"]'));
}
""",
    )
    assert out["live"] == "polite"
    assert len(out["stages"]) == 12
    for stage in out["stages"]:
        assert stage["pressed"] == [stage["scenario"]], stage
        assert set(stage["fields"]) == {"eyebrow", "title", "copy", "label", "evidence"}, stage
        for name, (wanted, shown) in stage["fields"].items():
            assert wanted and shown == wanted, (stage["scenario"], stage["step"], name)
        assert stage["photo"] is (stage["step"] == 0) and stage["role"] is None
        assert stage["current"] == [str(stage["step"])] and stage["number"] == f"0{stage['step'] + 1} / 04"
        assert stage["next"] == ("Restart story ↻" if stage["step"] == 3 else "Next chapter →")
    # Next on the last stage starts the story again
    first = {s["scenario"]: s["fields"]["title"][0] for s in out["stages"] if s["step"] == 0}
    for scenario, title in first.items():
        assert out[f"{scenario}-after-last"] == title


@needs_node
def test_back_to_the_first_stage_keeps_keyboard_focus_on_a_control(landing_tree):
    out = run(
        landing_tree,
        """
const { doc, press } = await page();
const next = doc.querySelector("#next");
const prev = doc.querySelector("#prev");
out.start = prev.disabled;
next.focus();
press("Enter");
out.second = prev.disabled;
prev.focus();
press("Enter");
out.back = prev.disabled;
out.focused = doc.activeElement.id || doc.activeElement.tagName;
""",
    )
    assert out == {"start": True, "second": False, "back": True, "focused": "next"}


@needs_node
def test_each_role_and_insight_presses_only_its_button_and_fills_its_fields(landing_tree):
    out = run(
        landing_tree,
        """
const { doc, press } = await page();
out.live = doc.querySelector("#roleText").getAttribute("aria-live");
out.roles = [];
const cards = doc.querySelectorAll(".rolecard[data-role]");
for (const card of cards) {
  card.focus();
  press("Enter");
  const state = doc.querySelector(`template[data-role-state="${card.dataset.role}"]`).content;
  out.roles.push({
    role: card.dataset.role,
    pressed: pressed(cards).map((c) => c.dataset.role),
    title: [text(state.querySelector('[data-f="title"]')), text(doc.querySelector("#roleTitle"))],
    desc: [text(state.querySelector('[data-f="desc"]')), text(doc.querySelector("#roleDesc"))],
  });
}
out.insights = [];
const tabs = doc.querySelectorAll(".insight-tab[data-insight]");
const toggle = doc.querySelector("#evidenceToggle");
const detail = doc.querySelector("#insightEvidenceDetail");
for (const tab of tabs) {
  toggle.focus();
  press("Enter");
  const opened = [toggle.getAttribute("aria-expanded"), detail.hidden];
  tab.focus();
  press("Enter");
  const state = doc.querySelector(`template[data-insight-state="${tab.dataset.insight}"]`).content;
  const closedBySwitch = [toggle.getAttribute("aria-expanded"), detail.hidden];
  toggle.focus();
  press("Enter");
  const reopened = [toggle.getAttribute("aria-expanded"), detail.hidden, text(detail) === text(state.querySelector('[data-f="detail"]'))];
  press("Enter");
  out.insights.push({
    insight: tab.dataset.insight,
    pressed: pressed(tabs).map((t) => t.dataset.insight),
    headline: [text(state.querySelector('[data-f="headline"]')), text(doc.querySelector("#insightHeadline"))],
    opened,
    closedBySwitch,
    reopened,
    closed: [toggle.getAttribute("aria-expanded"), detail.hidden],
  });
}
""",
    )
    assert out["live"] == "polite"
    assert [r["role"] for r in out["roles"]] == ["staff", "donor", "partner"]
    for role in out["roles"]:
        assert role["pressed"] == [role["role"]]
        assert role["title"][0] and role["title"][1] == role["title"][0]
        assert role["desc"][0] and role["desc"][1] == role["desc"][0]
    assert [i["insight"] for i in out["insights"]] == ["delivery", "assurance", "reach"]
    for insight in out["insights"]:
        assert insight["pressed"] == [insight["insight"]]
        assert insight["headline"][0] and insight["headline"][1] == insight["headline"][0]
        assert insight["opened"] == ["true", False]  # the evidence opens
        assert insight["closedBySwitch"] == ["false", True]  # another insight starts closed
        assert insight["reopened"] == ["true", False, True]  # with that insight's own detail
        assert insight["closed"] == ["false", True]


@needs_node
def test_the_mobile_menu_opens_onto_its_links_and_closes_on_escape_or_leaving(landing_tree):
    out = run(
        landing_tree,
        """
const { doc, press } = await page();
const menu = doc.querySelector("#mobileMenu");
const nav = doc.querySelector("#navLinks");
const state = () => [menu.getAttribute("aria-expanded"), nav.classList.contains("open")];
out.start = state();
menu.focus();
press("Enter");
out.opened = state();
press("Tab");
out.first = doc.activeElement.getAttribute("href");
for (let i = 0; i < 5; i++) press("Tab");
out.last = [doc.activeElement.getAttribute("href"), ...state()];
press("Tab");
out.left = [doc.activeElement.getAttribute("class"), ...state()];
menu.focus();
press("Enter");
press("Tab");
press("Escape");
out.escape = [...state(), doc.activeElement.id];
menu.focus();
press("Enter");
press("Enter");
out.toggled = state();
""",
    )
    assert out["start"] == ["false", False]
    assert out["opened"] == ["true", True]
    assert out["first"] == "#meaning"  # Tab goes from the button to the first link it opened
    assert out["last"] == ["#access", "true", True]  # still open while focus is in its links
    assert out["left"] == ["navcta", "false", False]  # Tab past the last link closes it
    assert out["escape"] == ["false", False, "mobileMenu"]
    assert out["toggled"] == ["false", False]


@needs_node
def test_nothing_moves_under_reduced_motion_and_figures_count_up_otherwise(landing_tree):
    out = run(
        landing_tree,
        """
const still = await page({ reduceMotion: true });
still.clock.tick(2000);
out.still = {
  ready: still.doc.documentElement.classList.contains("motion-ready"),
  revealed: still.doc.querySelectorAll(".data-reveal").every((el) => el.classList.contains("is-visible")),
  observers: still.observers.length,
  tallies: still.doc.querySelectorAll("[data-tally]").map((el) => [el.dataset.tally, text(el)]),
};
const moving = await page();
const hiddenFirst = moving.doc.querySelectorAll(".data-reveal").filter((el) => !el.classList.contains("is-visible")).length;
moving.clock.tick(16);
const tally = moving.doc.querySelector("[data-tally]");
moving.intersect(tally);
moving.clock.tick(160);
const mid = text(tally);
moving.clock.tick(2000);
out.moving = { ready: moving.doc.documentElement.classList.contains("motion-ready"), hiddenFirst, mid, end: text(tally), goal: tally.dataset.tally };
""",
    )
    still = out["still"]
    assert still["ready"] is False and still["revealed"] is True and still["observers"] == 0
    assert still["tallies"] and all(value == shown for value, shown in still["tallies"])
    moving = out["moving"]
    assert moving["ready"] is True and moving["hiddenFirst"] > 0
    assert int(moving["mid"]) < int(moving["goal"]) and moving["end"] == moving["goal"]


@needs_node
def test_a_photo_that_failed_before_the_script_ran_is_hidden(landing_tree):
    out = run(
        landing_tree,
        """
const portrait = (env) => env.doc.querySelector("img[data-hide-on-error]");
const failed = await page({}, (env) => Object.assign(portrait(env), { complete: true, naturalWidth: 0, currentSrc: "http://x/p.jpg" }));
out.failedEarly = portrait(failed).style.display ?? null;
const loaded = await page({}, (env) => Object.assign(portrait(env), { complete: true, naturalWidth: 960, currentSrc: "http://x/p.jpg" }));
out.loaded = portrait(loaded).style.display ?? null;
const lazy = await page();
out.lazyBefore = portrait(lazy).style.display ?? null;
portrait(lazy).dispatchEvent(new dom.Event("error", { bubbles: false }));
out.lazyAfter = portrait(lazy).style.display ?? null;
""",
    )
    assert out == {"failedEarly": "none", "loaded": None, "lazyBefore": None, "lazyAfter": "none"}
