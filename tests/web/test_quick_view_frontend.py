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
    assert data["fullPage"] is None and data["download"] is None and data["samePlace"] is None


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
