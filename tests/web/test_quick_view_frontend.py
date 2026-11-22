"""Quick view, the browser side (static/js/quickview.js), run in node on the stand-in page of parts_dom.mjs: which
links open in the quick view and which never do, the click itself (a plain click only, never with Ctrl, Cmd, Shift or
another button), the request it sends, and the way out when the answer is not a quick view (the page opens as
usual). Then the static rules the layout, app.js and app.css keep for it."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
TEMPLATES = Path(settings.BASE_DIR) / "neurodb" / "web" / "templates"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = """
const quick = await import(process.argv[1]);
const dom = await import(process.argv[2]);
const out = {};
const env = dom.installPage({ href: "http://localhost/home/?section=CP" });
const { doc, main, win, clock } = env;
const h = doc.h.bind(doc);
doc.head.append(h("meta", { name: "nd-quick-skip", content: "/manage/ /accounts/ /donor/" }));
const sidebar = h("nav", { id: "sidebar", class: "sidebar" });
const topbar = h("header", { class: "topbar" });
doc.body.insertBefore(topbar, main);
doc.body.insertBefore(sidebar, main);
const content = h("div", { class: "modal-content", id: "modal-content" });
const dialog = h("div", { class: "modal-dialog modal-lg modal-dialog-scrollable" }, content);
const modal = h("div", { class: "modal fade", id: "modal", tabindex: "-1", "aria-labelledby": "modal-title" }, dialog);
doc.body.append(modal);
const target = (link) => quick.quickTarget(link, { page: win.location.href });
const link = (attrs, ...parents) => {
  const a = h("a", attrs, "x");
  let inner = a;
  for (const parent of parents) { parent.append(inner); inner = parent; }
  main.append(inner);
  return a;
};
"""


def run(body: str) -> dict:
    script = PRELUDE + body + "\nconsole.log(JSON.stringify(out));\n"
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            script,
            (STATIC / "js" / "quickview.js").as_uri(),
            (Path(__file__).parent / "parts_dom.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_a_plain_link_to_another_page_in_the_content_opens_in_the_quick_view():
    data = run(
        """
out.partner = target(link({ href: "/partners/3/" }));
out.figure = target(link({ href: "/partner-reporting/?status=overdue&section=CP" }, h("div", { class: "kpi__value" })));
out.cell = target(link({ href: "/programmes/12/detail/" }, h("td"), h("tr"), h("tbody"), h("table")));
out.absolute = target(link({ href: "http://localhost/partner-monitoring/?view=quality" }));
out.withHash = target(link({ href: "/fmm/visits/4/#record-7" }));
"""
    )
    assert data["partner"] == {"url": "/partners/3/", "replace": False}
    assert data["figure"] == {"url": "/partner-reporting/?status=overdue&section=CP", "replace": False}
    assert data["cell"] == {"url": "/programmes/12/detail/", "replace": False}
    assert data["absolute"] == {"url": "/partner-monitoring/?view=quality", "replace": False}
    assert data["withHash"] == {"url": "/fmm/visits/4/#record-7", "replace": False}


@needs_node
def test_the_links_that_work_the_page_or_leave_it_on_purpose_never_open_a_quick_view():
    data = run(
        """
const cases = {
  menu: (() => { const a = h("a", { href: "/partners/" }); sidebar.append(a); return a; })(),
  topbar: (() => { const a = h("a", { href: "/partners/" }); topbar.append(a); return a; })(),
  breadcrumb: link({ href: "/partners/" }, h("li", { class: "breadcrumb-item" }), h("ol", { class: "breadcrumb" }), h("nav")),
  tab: link({ href: "/programmes/?tab=summary" }, h("div", { class: "segmented", role: "group" })),
  tablist: link({ href: "/programmes/?tab=summary" }, h("div", { role: "tablist" })),
  filterReset: link({ href: "/programmes/" }, h("div", { class: "filter-bar__actions" }), h("form", { class: "filter-bar" })),
  filterChip: link({ href: "/action-points/", class: "chip__remove" }, h("span", { class: "chip chip--removable" })),
  sort: link({ href: "/fmm/?sort=date" }, h("th"), h("tr"), h("thead"), h("table")),
  pagination: link({ href: "/partners/?page=2", class: "page-link" }, h("li", { class: "page-item" }), h("ul", { class: "pagination" })),
  pageAction: link({ href: "/partners/?export=xlsx" }, h("div", { class: "page-actions" })),
  emptyState: link({ href: "/partners/" }, h("div", { class: "state state--empty" })),
  download: link({ href: "/partners/3/", download: "" }),
  csv: link({ href: "/check/pr/rows.csv" }),
  xlsx: link({ href: "/exports/etools-locations.xlsx" }),
  exportQuery: link({ href: "/funds/?tab=x&export=csv" }),
  fileView: link({ href: "/library/4/download/" }),
  newTab: link({ href: "/partners/3/", target: "_blank" }),
  selfTarget: link({ href: "/partners/3/", target: "_self" }),
  optedOut: link({ href: "/partners/3/", "data-no-quick": "" }),
  optedOutAbove: link({ href: "/partners/3/" }, h("div", { "data-no-quick": "" })),
  external: link({ href: "https://example.org/report" }),
  mail: link({ href: "mailto:someone@example.org" }),
  hash: link({ href: "#figures" }),
  samePage: link({ href: "/home/?section=CP#tasks" }),
  samePath: link({ href: "/home/?section=ED" }),
  admin: link({ href: "/manage/datamart/" }),
  signIn: link({ href: "/accounts/password/change/" }),
  donor: link({ href: "/donor/" }),
  htmx: link({ href: "/progress/1/", "hx-get": "/progress/1/", "hx-target": "#modal-content" }),
  toggle: link({ href: "#more", "data-bs-toggle": "collapse" }),
  button: (() => { const b = h("button", { type: "button" }); main.append(b); return b; })(),
  noHref: link({}),
};
for (const [name, el] of Object.entries(cases)) out[name] = target(el);
"""
    )
    for name, found in data.items():
        assert found is None, (name, found)


@needs_node
def test_a_link_in_an_ask_neurodb_answer_opens_in_the_quick_view():
    """An answer of Ask NeuroDB (or of a chat in a page) opens its links in a new tab (assistant.agent.render: so a
    link never leaves the conversation). On a page the quick view keeps the conversation as well, so a plain click on
    a figure, a table link or a detail link of an answer opens it there, like any link of the content (the browser
    check of 22 Nov 2026 found them opening a new tab). Any other link with a target is still the browser's."""
    data = run(
        """
const answer = (attrs, ...inner) => link(
  { rel: "noopener noreferrer", target: "_blank", ...attrs },
  ...inner,
  h("div", { class: "ask-turn__body" }),
  h("div", { class: "ask-turn__answer panel" }),
  h("article", { class: "ask-turn" }),
  h("div", { class: "ask__thread", id: "ask-thread" }),
  h("div", { class: "ask ask--home", "data-ask": "" }),
);
out.detail = target(answer({ href: "/programmes/12/detail/" }, h("p")));
out.figure = target(answer({ href: "/partner-reporting/?status=overdue&pd_status=active" }, h("strong"), h("p")));
out.cell = target(answer({ href: "/partners/3/" }, h("td"), h("tr"), h("tbody"), h("table")));
// never: a download, another site (the server drops its address anyway), a link that is not the answer's
out.download = target(answer({ href: "/funds/?export=csv" }, h("p")));
out.external = target(answer({ href: "https://example.org/report" }, h("p")));
out.otherNewTab = target(link({ href: "/partners/3/", target: "_blank" }, h("div", { class: "ask__note" })));
out.otherTarget = target(answer({ href: "/partners/3/", target: "report" }, h("p")));
// a plain click asks for the quick view (the conversation stays); Ctrl-click is the browser's (a new tab)
win.bootstrap = { Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) } };
const asked = [];
globalThis.fetch = async (url) => {
  asked.push(url);
  return { type: "basic", status: 500, headers: { get: () => null }, text: async () => "" };
};
win.location.assign = () => {};
win.open = () => ({});
quick.initQuickView();
const click = (el, props = {}) => el.dispatchEvent(Object.assign(new dom.Event("click"), { button: 0 }, props));
out.plainWent = click(answer({ href: "/partners/4/" }, h("p")));
out.ctrlWent = click(answer({ href: "/partners/5/" }, h("p")), { ctrlKey: true });
await clock.tick(0);
out.asked = asked;
"""
    )
    assert data["detail"] == {"url": "/programmes/12/detail/", "replace": False}
    assert data["figure"] == {"url": "/partner-reporting/?status=overdue&pd_status=active", "replace": False}
    assert data["cell"] == {"url": "/partners/3/", "replace": False}
    for name in ("download", "external", "otherNewTab", "otherTarget"):
        assert data[name] is None, name
    assert data["plainWent"] is False and data["ctrlWent"] is True
    assert data["asked"] == ["/partners/4/"]


@needs_node
def test_an_answer_link_the_quick_view_cannot_show_opens_in_a_new_tab_and_keeps_the_conversation():
    """When the answer is not a quick view (a page that is never one, a sign-in after the session ended, an error), a
    link of an Ask NeuroDB answer opens its page in a new tab, as it did before the quick view, so the conversation is
    not left; only when the browser blocks the tab does the page open in place. Any other link opens in place."""
    data = run(
        """
const shown = [];
win.bootstrap = { Modal: { getOrCreateInstance: () => ({
  show: () => { shown.push("show"); modal.classList.add("show"); },
  hide: () => { shown.push("hide"); modal.classList.remove("show"); },
}) } };
const assigned = [];
const opened = [];
win.location.assign = (url) => assigned.push(url);
let blocked = false;
win.open = (url, name) => { opened.push([url, name]); return blocked ? null : { opener: "page" }; };
globalThis.fetch = async () => ({ type: "basic", status: 500, headers: { get: () => null }, text: async () => "" });
quick.initQuickView();
const click = (el) => el.dispatchEvent(Object.assign(new dom.Event("click"), { button: 0 }));
const answer = (href) => link({ href, target: "_blank", rel: "noopener noreferrer" }, h("p"), h("div", { class: "ask-turn__body" }));
click(answer("/fmm/report/"));
await clock.tick(0);
out.first = { opened: opened.slice(), assigned: assigned.slice(), shown: shown.slice() };
blocked = true;
click(answer("/fmm/report/?again=1"));
await clock.tick(0);
out.blocked = { opened: opened.slice(), assigned: assigned.slice() };
click(link({ href: "/partners/9/" }));
await clock.tick(0);
out.plain = { opened: opened.length, assigned: assigned.slice() };
"""
    )
    assert data["first"]["opened"] == [["/fmm/report/", "_blank"]]
    assert data["first"]["assigned"] == []
    assert data["first"]["shown"][-1] == "hide"  # the window closes over the conversation
    assert data["blocked"]["assigned"] == ["/fmm/report/?again=1"]
    assert data["plain"] == {"opened": 2, "assigned": ["/fmm/report/?again=1", "/partners/9/"]}


@needs_node
def test_inside_a_quick_view_links_open_in_it_and_the_page_controls_replace_it():
    data = run(
        """
const view = h("div", { class: "qview", "data-quick-view": "", "data-quick-url": "/programmes/?tab=summary&section=CP" });
content.append(view);
const inView = (attrs, ...parents) => {
  const a = h("a", attrs, "x");
  let inner = a;
  for (const parent of parents) { parent.append(inner); inner = parent; }
  view.append(inner);
  return a;
};
out.detail = target(inView({ href: "/partners/3/" }));
out.relativePage = target(inView({ href: "?tab=overall&section=CP" }, h("div", { class: "segmented" })));
out.pagination = target(inView({ href: "?tab=summary&section=CP&page=2" }, h("ul", { class: "pagination" })));
out.sort = target(inView({ href: "?tab=summary&sort=name" }, h("th"), h("thead")));
out.partModal = target(inView({ href: "/partner-reporting/report/?report=9", "hx-get": "/partner-reporting/report/?report=9", "hx-target": "#modal-content" }));
out.pushes = target(inView({ href: "/fmm/?tab=visits", "hx-get": "/fmm/visits/", "hx-target": "#fmm-results", "hx-push-url": "true" }));
out.inPlace = target(inView({ href: "/km/?more=1", "hx-get": "/km/more/", "hx-target": "this" }));
out.fullPage = target(inView({ href: "/programmes/?tab=summary&section=CP", "data-quick-full": "", "data-no-quick": "" }, h("div", { class: "modal-header" })));
out.download = target(inView({ href: "?tab=summary&export=xlsx" }));
out.samePlace = target(inView({ href: "?tab=summary&section=CP" }));
"""
    )
    assert data["detail"] == {"url": "/partners/3/", "replace": False}
    # read against the quick view's own address, not the page under it (/home/)
    assert data["relativePage"] == {"url": "/programmes/?tab=overall&section=CP", "replace": True}
    assert data["pagination"] == {"url": "/programmes/?tab=summary&section=CP&page=2", "replace": True}
    assert data["sort"] == {"url": "/programmes/?tab=summary&sort=name", "replace": True}
    assert data["partModal"] == {"url": "/partner-reporting/report/?report=9", "replace": False}
    assert data["pushes"] == {"url": "/fmm/?tab=visits", "replace": True}
    assert data["inPlace"] is None  # htmx updates it in place, inside the quick view
    assert data["fullPage"] is None and data["download"] is None
    # the window's own address reloads it in place (never left to the browser, which would leave the page)
    assert data["samePlace"] == {"url": "/programmes/?tab=summary&section=CP", "replace": True}


@needs_node
def test_only_a_plain_click_opens_a_quick_view():
    data = run(
        """
const ev = (props) => Object.assign(new dom.Event("click"), props);
out.plain = quick.plainClick(ev({ button: 0 }));
out.ctrl = quick.plainClick(ev({ button: 0, ctrlKey: true }));
out.cmd = quick.plainClick(ev({ button: 0, metaKey: true }));
out.shift = quick.plainClick(ev({ button: 0, shiftKey: true }));
out.alt = quick.plainClick(ev({ button: 0, altKey: true }));
out.middle = quick.plainClick(ev({ button: 1 }));
out.prevented = quick.plainClick(Object.assign(ev({ button: 0 }), { defaultPrevented: true }));
"""
    )
    assert data == {
        "plain": True,
        "ctrl": False,
        "cmd": False,
        "shift": False,
        "alt": False,
        "middle": False,
        "prevented": False,
    }


@needs_node
def test_a_click_asks_for_the_quick_view_and_goes_to_the_page_when_the_answer_is_not_one():
    data = run(
        """
const shown = [];
win.bootstrap = { Modal: { getOrCreateInstance: () => ({ show: () => shown.push("show"), hide: () => shown.push("hide") }) } };
const asked = [];
const assigned = [];
win.location.assign = (url) => assigned.push(url);
let answer = { type: "opaqueredirect", status: 0 };
globalThis.fetch = async (url, options) => {
  asked.push({ url, headers: options.headers, redirect: options.redirect });
  return { ...answer, headers: { get: (name) => (answer.headers || {})[name] ?? null }, text: async () => answer.body || "" };
};
quick.initQuickView();
const click = (el, props = {}) => {
  const event = Object.assign(new dom.Event("click"), { button: 0 }, props);
  const went = el.dispatchEvent(event);
  return went;
};
const a = link({ href: "/partners/3/" });
out.plainWent = click(a);
await clock.tick(0);
out.asked = asked.slice();
out.shown = shown.slice();
out.assigned = assigned.slice();

// a signed-out answer (a redirect), an error, a page that is not a quick view: the page opens as usual
answer = { type: "basic", status: 204, headers: { "X-NeuroDB-Quick-View": "refused" } };
click(link({ href: "/ask/" }));
await clock.tick(0);
answer = { type: "basic", status: 500 };
click(link({ href: "/partners/4/" }));
await clock.tick(0);
out.assignedAfter = assigned.slice();

// never asked: Ctrl-click, a link of the menu, a tab
asked.length = 0;
out.ctrlWent = click(link({ href: "/partners/5/" }), { ctrlKey: true });
const menuLink = h("a", { href: "/partners/" });
sidebar.append(menuLink);
out.menuWent = click(menuLink);
out.tabWent = click(link({ href: "/home/?tab=x" }, h("div", { class: "segmented" })));
await clock.tick(0);
out.askedAfter = asked.length;
"""
    )
    assert data["plainWent"] is False  # the browser does not leave the page
    (request,) = data["asked"]
    assert request["url"] == "/partners/3/"
    assert request["headers"]["X-NeuroDB-Quick"] == "1"
    assert request["redirect"] == "manual"  # a sign-in redirect is seen, never followed in the window
    assert data["shown"] == ["show"]  # the window opens at once, with its loading shapes
    assert data["assigned"] == ["/partners/3/"]  # a redirect: the page opens as usual
    assert data["assignedAfter"] == ["/partners/3/", "/ask/", "/partners/4/"]
    assert data["ctrlWent"] is True and data["menuWent"] is True and data["tabWent"] is True
    assert data["askedAfter"] == 0


@needs_node
def test_the_module_does_nothing_on_import_without_a_page():
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            "const q = await import(process.argv[1]); console.log(JSON.stringify({ ok: typeof q.initQuickView }));",
            (STATIC / "js" / "quickview.js").as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": "function"}


def _code(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(^|\s)//.*$", r"\1", js, flags=re.M)


def test_the_quick_view_never_pushes_history_and_returns_the_focus():
    code = _code((STATIC / "js" / "quickview.js").read_text())
    assert "pushState" not in code and "replaceState" not in code  # it never writes the address bar
    assert "hidden.bs.modal" in code and ".focus(" in code  # Esc and Close give the focus back to the link
    assert "X-NeuroDB-Quick" in code and 'redirect: "manual"' in code
    assert "htmx:beforeHistoryUpdate" in code  # a request made inside it never writes the address either
    assert "modal-fullscreen-md-down" in code and "modal-xl" in code  # large; full screen on a phone


def test_app_js_leaves_the_quick_view_its_filters_links_and_tables():
    app = _code((STATIC / "js" / "app.js").read_text())
    # a filter bar inside a quick view is the quick view's: it never changes the page under it or its address
    bar = app.split("function initFilterBar(form) {", 1)[1].split("\n}", 1)[0]
    assert 'if (form.closest?.("[data-quick-view]")) return;' in bar
    follow = app.split("function followQuery(event) {", 1)[1].split("\n}", 1)[0]
    assert 'link.closest?.("[data-quick-view]")' in follow
    tables = app.split("function initTables(root) {", 1)[1].split("\n}", 1)[0]
    assert "scopedQuery(btn.dataset.tableTarget, btn)" in tables and "nd:quick-open" in tables
    lib = _code((STATIC / "js" / "lib.js").read_text())
    assert "export function scopedQuery(" in lib and "export function filterParams(" in lib
    # the same query as the page's own filter bar (app.js keeps its copy: its node test runs it alone)
    keep = "const keep = new Set("
    assert keep in bar and keep in lib.split("export function filterParams(", 1)[1]
    assert "export function readJSON(id, near)" in lib


def test_the_quick_view_keeps_dark_mode_phones_and_reduced_motion():
    css = (STATIC / "css" / "app.css").read_text()
    rule = "/* " + "-" * 66
    block = css.split(f"{rule} quick view", 1)[1].split(rule, 1)[0]
    assert not re.search(r":[^;{}]*(?:#[0-9a-fA-F]{3,8}\b|rgba?\()", block), "colours come from the tokens"
    assert "var(--nd-" in block
    assert "@media (prefers-reduced-motion: reduce)" in block
    assert "max-width: 575.98px" in block  # the header fits a 390 px phone
    layout = (TEMPLATES / "layouts" / "quick.html").read_text()
    assert 'id="modal-title"' in layout and 'tabindex="-1"' in layout
    assert "data-quick-back" in layout and "data-quick-full" in layout and "data-no-quick" in layout
    assert "<script" not in layout and "{% block scripts" not in layout


# ------------------------------------------------------------------------------------- the answer drawn
# The stand-in parses no markup: an answer's body names a view built here (`answers`), a <template> holds it, and
# htmx.swap puts the very element in the window (its outerHTML is a key to it).
DRAW = """
globalThis.Event = dom.Event;
const lib = await import(new URL("./lib.js", process.argv[1]).href);
const answers = {};
const made = new Map();
let seq = 0;
Object.defineProperty(dom.Element.prototype, "outerHTML", { get() { const key = `@el${(this._key ??= ++seq)}`; made.set(key, this); return key; } });
const realCreate = doc.createElement.bind(doc);
doc.createElement = (tag) => {
  if (String(tag).toLowerCase() !== "template") return realCreate(tag);
  const box = realCreate("div");
  return { content: box, set innerHTML(html) { const make = answers[html]; if (make) box.append(make(html)); } };
};
win.htmx.swap = (target, html) => {
  for (const child of [...target.children]) child.remove();
  const el = made.get(html);
  if (el) target.append(el);
};
globalThis.FormData = class {
  constructor(form) { this.pairs = form.querySelectorAll("[name]").filter((el) => !el.disabled).map((el) => [el.name, el.value]); }
  [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
};
let showOnShow = true;
win.bootstrap = { Modal: { getOrCreateInstance: () => ({ show() { if (showOnShow) modal.classList.add("show"); }, hide() {} }) } };
const asked = [];
const waiting = [];
let hold = false;
globalThis.fetch = (url) => {
  asked.push(url);
  const reply = { type: "basic", status: 200, headers: { get: (n) => (n === quick.ANSWER_HEADER ? "1" : null) }, text: async () => url };
  if (!hold) return Promise.resolve(reply);
  return new Promise((resolve) => waiting.push(() => resolve(reply)));
};
const view = (url, ...children) => h("div", { class: "qview", "data-quick-view": "", "data-quick-url": url },
  h("div", { class: "modal-header qview__header" },
    h("button", { type: "button", "data-quick-back": "", hidden: true }, "Back"),
    h("h2", { class: "modal-title", id: "modal-title", tabindex: "-1" }, "A page")),
  h("div", { class: "modal-body qview__body" }, ...children));
const click = (el, props = {}) => el.dispatchEvent(Object.assign(new dom.Event("click"), { button: 0 }, props));
const inContent = (selector) => content.querySelector(selector);
quick.initQuickView();
"""


@needs_node
def test_the_answer_is_drawn_once_the_window_is_shown():
    """Bootstrap shows the window after its fade: an answer that comes sooner waits for it, so a chart is never
    drawn in a hidden box (Plotly would draw it 700 px wide)."""
    data = run(
        DRAW
        + """
showOnShow = false;
answers["/funds/"] = (url) => view(url, h("div", { class: "chart", "data-module": "charts" }));
click(link({ href: "/funds/" }));
await clock.tick(0);
out.before = Boolean(inContent("[data-quick-view]"));
modal.classList.add("show");
modal.dispatchEvent(new dom.Event("shown.bs.modal", { bubbles: false }));
await clock.tick(0);
out.after = Boolean(inContent("[data-quick-view] .chart"));
out.focus = doc.activeElement.id;
"""
    )
    assert data == {"before": False, "after": True, "focus": "modal-title"}


@needs_node
def test_a_filter_change_keeps_the_field_its_letters_and_the_open_filters():
    """A filter applied inside the window keeps the person's place, as the page does: the view stays while the
    answer comes, the focus goes back to the same field (not the title), letters typed meanwhile are kept and
    applied, and the filters opened on a phone stay open."""
    data = run(
        DRAW
        + """
const bar = (url) => {
  const q = new URL(url, "http://localhost").searchParams.get("q") || "";
  const year = new URL(url, "http://localhost").searchParams.get("year") || "";
  return view(url, h("div", { class: "filters-collapse", "data-filters-collapse": "" },
    h("form", { method: "get", class: "filter-bar", "data-filter-bar": "" },
      h("label", { for: "f-q" }, "Search"), h("input", { type: "search", id: "f-q", name: "q", value: q }),
      h("select", { id: "f-year", name: "year", value: year }))));
};
for (const url of ["/partner-reporting/", "/partner-reporting/?q=Hima", "/partner-reporting/?q=Himaya", "/partner-reporting/?q=Himaya&year=2026"]) answers[url] = bar;
click(link({ href: "/partner-reporting/" }));
await clock.tick(0);
inContent("[data-filters-collapse]").classList.add("is-open");  // opened on a phone
const first = inContent('[name="q"]');
first.focus();
first.value = "Hima";
first.dispatchEvent(new dom.Event("input"));
hold = true;
await clock.tick(500);
out.asked = asked.slice(1);
out.kept = inContent('[name="q"]') === first;  // the view stays while its answer comes
first.value = "Himaya";
first.setSelectionRange(6, 6);
first.dispatchEvent(new dom.Event("input"));
waiting.shift()();
await clock.tick(0);
const field = inContent('[name="q"]');
out.fresh = field !== first;
out.focus = doc.activeElement === field;
out.value = field.value;
out.caret = [field.selectionStart, field.selectionEnd];
out.open = inContent("[data-filters-collapse]").classList.contains("is-open");
hold = false;
await clock.tick(500);
out.again = asked.slice(2);
const year = inContent('[name="year"]');
year.focus();
year.value = "2026";
year.dispatchEvent(new dom.Event("change"));
await clock.tick(400);
out.yearFocus = doc.activeElement === inContent('[name="year"]') && doc.activeElement !== year;
out.title = doc.activeElement.id === "modal-title";
"""
    )
    assert data["asked"] == ["/partner-reporting/?q=Hima"]
    assert data["kept"] and data["fresh"] and data["focus"]
    assert data["value"] == "Himaya" and data["caret"] == [6, 6]
    assert data["open"] is True
    assert data["again"] == ["/partner-reporting/?q=Himaya"]  # the letters typed meanwhile are applied
    assert data["yearFocus"] is True and data["title"] is False


@needs_node
def test_ids_the_page_already_has_are_the_quick_view_s_own():
    """A quick view of the same kind of page brings the page's ids: those are renamed in the window, with every
    label, aria reference and htmx target inside it, so a label names its own field and an answer lands in the
    window; an id only the window has, and the window's title, keep theirs."""
    data = run(
        DRAW
        + """
main.append(h("label", { for: "f-q" }, "Search"), h("input", { id: "f-q", name: "q" }), h("div", { id: "fmm-review" }),
  h("script", { id: "map-config" }, "{}"));
answers["/partner-reporting/?partner=4"] = (url) => view(url,
  h("label", { for: "f-q" }, "Search"), h("input", { type: "search", id: "f-q", name: "q" }),
  h("button", { "hx-post": "/fmm/review/", "hx-target": "#fmm-review", "aria-controls": "fmm-review only-here" }, "Save"),
  h("div", { id: "fmm-review" }), h("a", { href: "#fmm-review" }, "Jump"),
  h("h3", { id: "only-here" }, "Only here"), h("div", { "aria-labelledby": "only-here modal-title" }),
  h("section", { "data-module": "pdmap", "data-config": "map-config" }), h("script", { id: "map-config" }, "{}"));
click(link({ href: "/partner-reporting/?partner=4" }));
await clock.tick(0);
const v = inContent("[data-quick-view]");
const input = v.querySelector("input");
out.input = [input.id, input.getAttribute("data-quick-id")];
out.label = v.querySelector("label").getAttribute("for");
const button = v.querySelector("button[hx-post]");
out.target = button.getAttribute("hx-target");
out.controls = button.getAttribute("aria-controls");
out.anchor = v.querySelector("a[href]").getAttribute("href");
out.onlyHere = v.querySelector("h3").id;
out.labelledby = v.querySelector("[aria-labelledby]").getAttribute("aria-labelledby");
out.config = v.querySelector("section").getAttribute("data-config");
out.title = v.querySelector("h2").id;
out.pageIds = main.querySelectorAll("[id]").map((el) => el.id);
out.byId = lib.byId("f-q", button) === input;
out.byConfig = lib.byId("map-config", button) === v.querySelector("script");
"""
    )
    assert data["input"] == ["qv-f-q", "f-q"]
    assert data["label"] == "qv-f-q"
    assert data["target"] == "#qv-fmm-review"
    assert data["controls"] == "qv-fmm-review only-here"
    assert data["anchor"] == "#qv-fmm-review"
    assert data["onlyHere"] == "only-here" and data["labelledby"] == "only-here modal-title"
    assert data["config"] == "qv-map-config" and data["title"] == "modal-title"
    assert data["pageIds"] == ["f-q", "fmm-review", "map-config"]
    assert data["byId"] is True and data["byConfig"] is True


@needs_node
def test_in_a_quick_view_its_own_address_reloads_it_and_an_anchor_scrolls_it():
    """Inside the window, a link to the window's own address (its current tab, a Reset with nothing set) reloads
    it in place, and an anchor of it scrolls the window: neither leaves the page or writes its address."""
    data = run(
        DRAW
        + """
answers["/partner-monitoring/?pd=8&view=quality"] = (url) => view(url,
  h("div", { class: "segmented" }, h("a", { href: "/partner-monitoring/?pd=8&view=quality", "aria-current": "page" }, "Data quality")),
  h("nav", { class: "brief-tabs" }, h("a", { href: "#brief-partners" }, "Partners")),
  h("a", { href: "?pd=8&view=quality#brief-partners", class: "same" }, "Same"),
  h("section", { id: "brief-partners" }, "Partners"));
const assigned = [];
win.location.assign = (url) => assigned.push(url);
click(link({ href: "/partner-monitoring/?pd=8&view=quality" }));
await clock.tick(0);
const v = inContent("[data-quick-view]");
out.self = quick.quickTarget(v.querySelector(".segmented a"));
out.anchor = quick.quickTarget(v.querySelector(".brief-tabs a"));
out.sameAnchor = quick.quickTarget(v.querySelector("a.same"));
out.went = click(inContent(".segmented a"));
await clock.tick(0);
out.asked = asked.slice();
out.stillOpen = Boolean(inContent("[data-quick-view]"));
out.anchorWent = click(inContent(".brief-tabs a"));
await clock.tick(0);
out.hash = win.location.hash;
out.scrolled = doc.scrolls.map((s) => s.id);
out.marked = inContent("section").classList.contains("is-target");
out.assigned = assigned;
// on the page itself the browser keeps its own way
out.pageHash = quick.quickTarget(link({ href: "#figures" }));
out.pageSelf = quick.quickTarget(link({ href: "/home/?section=CP" }));
"""
    )
    assert data["self"] == {"url": "/partner-monitoring/?pd=8&view=quality", "replace": True}
    assert data["anchor"] == {"anchor": "brief-partners"}
    assert data["sameAnchor"] == {"anchor": "brief-partners"}
    assert data["went"] is False and data["stillOpen"] is True
    assert data["asked"] == [
        "/partner-monitoring/?pd=8&view=quality",
        "/partner-monitoring/?pd=8&view=quality",
    ]
    assert data["anchorWent"] is False and data["hash"] == ""
    assert data["scrolled"] == ["brief-partners"] and data["marked"] is True
    assert data["assigned"] == []
    assert data["pageHash"] is None and data["pageSelf"] is None


@needs_node
def test_the_help_shortcut_and_an_answer_ending_never_fight_an_open_window():
    """The Help panel cannot take the focus from an open window (Bootstrap keeps it there): the shortcut closes the
    window first, then opens the panel. An Ask answer ending moves the focus to the question box only when the
    focus is still in the conversation or nowhere, and no window is open."""
    data = run(
        """
const lib = await import(new URL("./lib.js", process.argv[1]).href);
const hidden = [];
win.bootstrap = { Modal: { getOrCreateInstance: (el) => ({ hide() {
  hidden.push(el.id); el.classList.remove("show"); el.dispatchEvent(new dom.Event("hidden.bs.modal", { bubbles: false }));
} }) } };
let opened = 0;
lib.afterDialogs(() => { opened += 1; });
out.direct = [opened, hidden.slice()];
modal.classList.add("show");
lib.afterDialogs(() => { opened += 1; });
out.afterClose = [opened, hidden.slice()];
const root = h("div", {}, h("textarea", { name: "question" }), h("button", { type: "submit" }));
const other = h("a", { href: "/partners/" });
main.append(root, other);
doc.activeElement = doc.body;
out.nowhere = lib.mayTakeFocus(root);
root.querySelector("button").focus();
out.inside = lib.mayTakeFocus(root);
other.focus();
out.elsewhere = lib.mayTakeFocus(root);
doc.activeElement = doc.body;
modal.classList.add("show");
out.window = lib.mayTakeFocus(root);
"""
    )
    assert data["direct"] == [1, []]
    assert data["afterClose"] == [2, ["modal"]]
    assert data == {**data, "nowhere": True, "inside": True, "elsewhere": False, "window": False}
    app = _code((STATIC / "js" / "app.js").read_text())
    help_block = app.split("function initHelp()", 1)[1].split("\n}\n", 1)[0]
    assert "afterDialogs(open)" in help_block
    ask = _code((STATIC / "js" / "ask.js").read_text())
    finally_block = ask.split("} finally {", 1)[1].split("}", 1)[0]
    assert "if (mayTakeFocus(root)) input.focus();" in finally_block


@needs_node
def test_a_scrolling_tab_bar_shows_its_current_tab():
    """On a phone a long tab bar scrolls sideways with its scroll bar hidden: the current tab is scrolled into
    sight, and an edge fades while more tabs are hidden on that side."""
    data = run(
        """
const lib = await import(new URL("./lib.js", process.argv[1]).href);
const bar = { left: 33, right: 357, width: 324 };
out.offRight = lib.revealInBar(bar, 0, { left: 599, right: 812, width: 213 });
out.partly = lib.revealInBar(bar, 0, { left: 339, right: 597, width: 258 });
out.inSight = lib.revealInBar(bar, 0, { left: 37, right: 228, width: 191 });
out.offLeft = lib.revealInBar(bar, 300, { left: 10, right: 100, width: 90 });
out.none = lib.revealInBar(bar, 0, null);
"""
    )
    assert data["offRight"] == 0 + (812 - 357) + 8
    assert data["partly"] == 0 + (597 - 357) + 8
    assert data["inSight"] is None and data["none"] is None
    assert data["offLeft"] == 300 - (33 - 10) - 8
    app = _code((STATIC / "js" / "app.js").read_text())
    enhance = app.split("function enhance(root = document) {", 1)[1].split("\n}", 1)[0]
    assert "initScrollBars(root);" in enhance
    assert "revealInBar(" in app
    css = (STATIC / "css" / "app.css").read_text()
    assert ".segmented--scroll.is-more-end" in css and ".segmented--scroll.is-more-start" in css
    assert "mask-image" in css.split(".segmented--scroll.is-more-end", 1)[1].split("}", 1)[0]
