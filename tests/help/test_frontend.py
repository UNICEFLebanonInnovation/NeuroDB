"""The Help assistant's frontend (stage D2.2), in the style of the other frontend tests: the shortcut and
the focus trap (lib.js, run in node), app.js opening the panel with them, and ask.js sending the page it
is on and keeping the conversation for the tab without failing when the browser refuses storage."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
NODE = shutil.which("node")


def _node(script: str, module: str) -> str:
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script, (STATIC / "js" / module).as_uri()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_shortcut_is_ctrl_or_cmd_shift_h_only():
    script = """
const lib = await import(process.argv[1]);
const key = (k, mods = {}) => ({ key: k, code: "", shiftKey: false, ctrlKey: false, metaKey: false, altKey: false, ...mods });
console.log(JSON.stringify([
  lib.isHelpShortcut(key("H", { ctrlKey: true, shiftKey: true })),
  lib.isHelpShortcut(key("h", { metaKey: true, shiftKey: true })),
  lib.isHelpShortcut(key("Ĥ", { ctrlKey: true, shiftKey: true, code: "KeyH" })),
  lib.isHelpShortcut(key("h", { ctrlKey: true })),
  lib.isHelpShortcut(key("H", { shiftKey: true })),
  lib.isHelpShortcut(key("H", { ctrlKey: true, shiftKey: true, altKey: true })),
  lib.isHelpShortcut(key("K", { ctrlKey: true, shiftKey: true })),
  lib.isHelpShortcut(key("H", { ctrlKey: true, shiftKey: true, isComposing: true })),
]));
"""
    assert json.loads(_node(script, "lib.js")) == [True, True, True, False, False, False, False, False]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_tab_stays_inside_the_open_panel():
    script = """
const lib = await import(process.argv[1]);
const el = (name) => ({ name, hidden: false, closest: () => null });
const first = el("close"), middle = el("input"), last = el("ask");
const panel = { querySelectorAll: () => [first, middle, last], contains: (x) => [first, middle, last].includes(x) };
const outside = el("page link");
const at = (active, shiftKey) => { globalThis.document = { activeElement: active }; return lib.trapFocus(panel, { key: "Tab", shiftKey })?.name ?? null; };
console.log(JSON.stringify([at(last, false), at(first, true), at(middle, false), at(outside, false), at(outside, true),
  lib.trapFocus(panel, { key: "Enter" })]));
"""
    assert json.loads(_node(script, "lib.js")) == ["close", "ask", None, "close", "ask", None]


def test_app_js_opens_the_panel_with_the_shortcut_and_the_button_and_closes_it_with_escape():
    app = (STATIC / "js" / "app.js").read_text()
    assert "isHelpShortcut" in app and "trapFocus" in app and "initHelp();" in app
    block = app.split("function initHelp()", 1)[1].split("\n}\n", 1)[0]
    assert 'document.addEventListener("keydown"' in block
    assert "if (isHelpShortcut(e))" in block and 'e.key === "Escape" && isOpen()' in block
    assert 'trigger?.addEventListener("click"' in block and '"[data-help-close]"' in block
    assert 'window.htmx.ajax("GET", panel.dataset.src' in block  # the body is loaded when it first opens
    assert 'setAttribute("aria-expanded", "true")' in block and ".focus()" in block


def test_ask_js_sends_the_page_and_keeps_the_conversation_for_the_tab():
    ask = (STATIC / "js" / "ask.js").read_text()
    assert 'body.append("page", window.location.pathname);' in ask
    assert 'body.append("title", document.title);' in ask
    assert "window.sessionStorage.getItem(key)" in ask and "window.sessionStorage.setItem(key" in ask
    # every storage access is wrapped: a private window or blocked storage never breaks the panel
    for fn in ("readKept", "writeKept"):
        body = ask.split(f"function {fn}(", 1)[1].split("\n}\n", 1)[0]
        assert "try {" in body and "catch" in body, fn
    assert re.search(r"\.slice\(-MAX_KEPT_TURNS\)", ask)
    assert "[data-ask-quota]" in ask


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_panel_starts_when_the_browser_refuses_storage():
    """ask.js's init with data-history-key, sessionStorage throwing on every access: it starts, with a
    fresh conversation, and never takes the focus on load."""
    script = """
const focused = [];
globalThis.window = {
  crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000000" },
  get sessionStorage() { throw new Error("SecurityError"); },
};
const element = (id) => ({
  id, style: {}, value: "", hidden: false, dataset: {}, scrollHeight: 20,
  addEventListener() {}, focus() { focused.push(id); }, setSelectionRange() {},
  querySelectorAll: () => [], querySelector: () => null, classList: { toggle() {} },
});
const parts = {};
const root = {
  dataset: { module: "ask", historyKey: "neurodb-help", pageContext: "" },
  querySelector: (sel) => {
    const m = sel.match(/data-ask-part="(\\w+)"/);
    return m ? (parts[m[1]] ??= element(m[1])) : null;
  },
  querySelectorAll: () => [],
  classList: { toggle() {} },
};
globalThis.document = { querySelector: () => null };
const mod = await import(process.argv[1]);
mod.init(root);
console.log(JSON.stringify([Object.keys(parts).sort(), focused.length, parts.intro.hidden]));
"""
    parts, focused, intro_hidden = json.loads(_node(script, "ask.js"))
    assert parts == ["form", "input", "intro", "new", "stop", "submit", "thread"]
    assert focused == 0 and intro_hidden is False


def test_the_panel_styles_exist_and_hide_in_print():
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("Help (/help/, the Help assistant panel)", 1)[1]
    for selector in (".help-panel {", ".help-panel[hidden]", ".help-layout", ".help-nav__link.is-active"):
        assert selector in block, selector
    assert "position: fixed; right: 1rem; bottom: 1rem;" in block
    assert "@media print { .help-panel, .help-trigger { display: none !important; } }" in block
