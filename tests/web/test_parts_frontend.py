"""Pages that load in parts, the browser side (SPEC7 §1.4, §3.7): parts.js's scheduler, createQueue(), run
in node (it touches no page, so it imports without one), and the static rules app.js, base.html and app.css
must keep: app.js starts parts.js, part requests raise no toasts, export links drop ``_part`` and
``_eager``, Back and Forward reload the page, the parallel cap is a meta tag, and every placeholder keeps its
height in both themes."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
TEMPLATES = Path(settings.BASE_DIR) / "neurodb" / "web" / "templates"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = """
const parts = await import(process.argv[1]);
const { createQueue } = parts;
const ids = (items) => items.map((item) => item.id);
const out = {};
"""


def run(body: str) -> dict:
    """Run ``body`` in node after PRELUDE (parts.js imported, ``out`` to fill) and return ``out``."""
    script = PRELUDE + body + "\nconsole.log(JSON.stringify(out));\n"
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script, (STATIC / "js" / "parts.js").as_uri()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ------------------------------------------------------------------------------------------ the queue
@needs_node
def test_parts_js_imports_without_a_page_and_never_has_more_than_three_loading():
    data = run(
        """
out.noPage = parts.initParts() ?? null;
const q = createQueue({ max: 3 });
for (let i = 1; i <= 6; i++) q.add({ id: `h${i}`, order: i, priority: "high" });
out.first = ids(q.next());
out.again = ids(q.next());
q.done("h1");
out.after = ids(q.next());
// a long mixed run: never more than 3 in flight, never more than 2 while a group leader loads
const r = createQueue({ max: 3 });
let seed = 7;
const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
for (let i = 0; i < 40; i++) {
  r.add({ id: i, order: i, priority: ["high", "normal", "low"][i % 3], group: i % 4 === 0 ? `g${i % 8} /?y=1` : null,
          strict: i % 9 === 0, visible: rnd() > 0.5 });
}
let most = 0;
let leaderBreach = false;
for (let step = 0; step < 400; step++) {
  r.next();
  const loading = r.counts().loading;
  most = Math.max(most, loading);
  if (loading > r.limit() || (r.limit() === 2 && loading > 2)) leaderBreach = true;
  const live = [...Array(40).keys()].filter((id) => ["loading", "swapping"].includes(r.state(id)));
  if (!live.length) { for (let id = 0; id < 40; id++) r.setVisible(id, true); continue; }
  const id = live[Math.floor(rnd() * live.length)];
  const roll = rnd();
  if (roll < 0.6) r.done(id);
  else if (roll < 0.8) { const v = r.fail(id, [0, 404, 503][Math.floor(rnd() * 3)]); if (v?.retry) r.requeue(id); }
  else if (roll < 0.9) { r.swapping(id); r.drop(id); r.done(id); }
  else r.drop(id);
}
out.most = most;
out.leaderBreach = leaderBreach;
"""
    )
    assert data["noPage"] is None  # no document in node: nothing is started
    assert data["first"] == ["h1", "h2", "h3"]
    assert data["again"] == []
    assert data["after"] == ["h4"]
    assert data["most"] <= 3 and not data["leaderBreach"]


@needs_node
def test_high_before_visible_normal_before_visible_low_then_document_order():
    data = run(
        """
const q = createQueue({ max: 1 });
q.add({ id: "low0", order: 0, priority: "low", visible: true });
q.add({ id: "normal1", order: 1, priority: "normal", visible: true });
q.add({ id: "high2", order: 2, priority: "high" });
q.add({ id: "normal3", order: 3, priority: "normal", visible: true });
q.add({ id: "high4", order: 4, priority: "high" });
q.add({ id: "normal5", order: 5, priority: "normal", visible: false });
const seen = [];
for (let i = 0; i < 6; i++) { const [item] = q.next(); if (!item) break; seen.push(item.id); q.done(item.id); }
out.seen = seen;
"""
    )
    # the part out of view comes last, by prefetch, once everything in view is in
    assert data["seen"] == ["high2", "high4", "normal1", "normal3", "low0", "normal5"]


@needs_node
def test_a_group_leader_goes_alone_and_its_members_follow_when_it_finishes_or_fails():
    data = run(
        """
const g = parts.groupKey("overview", "/?year=2025&_part=kpis");
const make = () => {
  const q = createQueue({ max: 3 });
  q.add({ id: "kpis", order: 0, priority: "high", group: g });
  q.add({ id: "figures", order: 1, priority: "normal", group: g, visible: true });
  q.add({ id: "review", order: 2, priority: "normal", group: g, visible: true });
  q.add({ id: "watch", order: 3, priority: "high" });
  return q;
};
let q = make();
out.start = ids(q.next());
out.blocked = ids(q.next());
q.done("kpis");
out.afterDone = ids(q.next());
q = make();
q.next();
q.fail("kpis", 404);
out.afterFail = ids(q.next());
// an automatic retry keeps the group closed: the members wait for the leader's second try
q = make();
q.next();
out.retry = q.fail("kpis", 503);
out.whileWaiting = ids(q.next());
q.requeue("kpis");
out.requeued = ids(q.next());
// a member in view pulls its leader, even a leader out of view
const p = createQueue({ max: 3 });
p.add({ id: "lead", order: 0, priority: "normal", group: "g /x", visible: false });
p.add({ id: "member", order: 1, priority: "normal", group: "g /x", visible: true });
out.pulled = ids(p.next());
"""
    )
    assert data["start"] == ["kpis", "watch"]  # the leader alone in its group; the cap is 2 while it loads
    assert data["blocked"] == []
    assert data["afterDone"] == ["figures", "review"]
    assert data["afterFail"] == ["figures", "review"]
    assert data["retry"]["retry"] is True and data["whileWaiting"] == []
    assert data["requeued"] == ["kpis"]
    assert data["pulled"] == ["lead"]


@needs_node
def test_the_cap_is_two_while_a_group_leader_loads_and_three_after():
    data = run(
        """
const q = createQueue({ max: 3 });
q.add({ id: "lead", order: 0, priority: "high", group: "overview /?year=2025" });
for (let i = 1; i <= 4; i++) q.add({ id: `h${i}`, order: i, priority: "high" });
out.start = ids(q.next());
out.limitWhileLeader = q.limit();
q.done("lead");
out.limitAfter = q.limit();
out.after = ids(q.next());
out.loading = q.counts().loading;
// the meta tag's value: a cap of 1 stays 1 while a leader loads
const one = createQueue({ max: 1 });
one.add({ id: "a", order: 0, priority: "high", group: "g /" });
one.add({ id: "b", order: 1, priority: "high" });
out.one = ids(one.next());
"""
    )
    assert data["start"] == ["lead", "h1"]
    assert data["limitWhileLeader"] == 2 and data["limitAfter"] == 3
    assert data["after"] == ["h2", "h3"] and data["loading"] == 3
    assert data["one"] == ["a"]


@needs_node
def test_strict_parts_are_never_prefetched_but_load_when_seen_or_bumped():
    data = run(
        """
const q = createQueue({ max: 3 });
q.add({ id: "more", order: 0, priority: "normal", strict: true, visible: false });
q.add({ id: "news", order: 1, priority: "low", visible: false });
out.first = ids(q.next());
q.done("news");
out.nothing = ids(q.next());
out.remaining = q.remaining();
q.setVisible("more", true);
out.seen = ids(q.next());
const b = createQueue({ max: 3 });
b.add({ id: "more", order: 0, priority: "normal", strict: true, visible: false });
b.bump("more");
out.bumped = ids(b.next());
"""
    )
    assert data["first"] == ["news"]  # prefetched; the strict one is not
    assert data["nothing"] == [] and data["remaining"] == 0
    assert data["seen"] == ["more"]
    assert data["bumped"] == ["more"]


@needs_node
def test_prefetch_runs_one_at_a_time_once_the_page_says_what_is_in_view():
    data = run(
        """
const q = createQueue({ max: 3 });
for (const [i, id] of ["a", "b", "c"].entries()) q.add({ id, order: i, priority: "normal" });
out.unknown = ids(q.next()); // visibility not measured yet: wait
["a", "b", "c"].forEach((id) => q.setVisible(id, false));
out.one = ids(q.next());
out.stillOne = ids(q.next());
q.done("a");
out.two = ids(q.next());
q.done("b");
out.three = ids(q.next());
q.done("c");
out.end = ids(q.next());
"""
    )
    assert data["unknown"] == []
    assert data["one"] == ["a"] and data["stillOne"] == []
    assert data["two"] == ["b"] and data["three"] == ["c"] and data["end"] == []


@needs_node
def test_one_automatic_retry_for_network_errors_timeouts_and_502_503_504_never_for_4xx():
    data = run(
        """
const verdicts = {};
for (const reason of [0, "network", "timeout", 502, 503, 504, 400, 403, 404, 410, 429, 500]) {
  const q = createQueue({ max: 3 });
  q.add({ id: "p", order: 0, priority: "high" });
  q.next();
  const first = q.fail("p", reason);
  const state = q.state("p");
  let second = null;
  if (first.retry) {
    q.requeue("p");
    q.next();
    second = q.fail("p", reason);
  }
  verdicts[reason] = { first, state, second, end: q.state("p"), errors: q.counts().error };
}
out.verdicts = verdicts;
// Try again gives the part its automatic retry back
const t = createQueue({ max: 3 });
t.add({ id: "p", order: 0, priority: "high" });
t.next(); t.fail("p", 503); t.requeue("p"); t.next(); t.fail("p", 503);
out.retried = t.retry("p");
t.next();
out.again = t.fail("p", 503);
"""
    )
    v = data["verdicts"]
    for reason in ("0", "network", "timeout", "502", "503", "504"):
        assert v[reason]["first"] == {"retry": True, "delay": 2000, "message": v[reason]["first"]["message"]}
        assert v[reason]["state"] == "waiting"  # not an error while it waits for its retry
        assert v[reason]["second"]["retry"] is False and v[reason]["end"] == "error"
    for reason in ("400", "403", "404", "410", "429", "500"):
        assert v[reason]["first"]["retry"] is False and v[reason]["second"] is None
        assert v[reason]["end"] == "error" and v[reason]["errors"] == 1
    assert v["timeout"]["second"]["message"] == "timeout"
    assert v["403"]["first"]["message"] == "forbidden"
    assert v["404"]["first"]["message"] == "gone"
    assert v["500"]["first"]["message"] == "generic" and v["0"]["second"]["message"] == "generic"
    assert data["retried"] is True and data["again"]["retry"] is True


@needs_node
def test_a_202_still_computing_is_asked_again_with_backoff_holding_its_slot_never_an_error():
    data = run(
        """
let clock = 0;
const q = createQueue({ max: 3, now: () => clock });
q.add({ id: "kpis", order: 0, priority: "high" });
q.add({ id: "x", order: 1, priority: "high" });
q.add({ id: "y", order: 2, priority: "high" });
q.add({ id: "z", order: 3, priority: "high" });
q.next();
const delays = [];
let delay;
while ((delay = q.wait("kpis")) !== null) { delays.push(delay); clock += delay + 2000; }
out.delays = delays;
out.clock = clock;
out.state = q.state("kpis");
out.counts = q.counts();
out.blocked = ids(q.next()); // still holding its slot
out.verdict = q.fail("kpis", "timeout"); // the budget spent: a timeout, retried once
"""
    )
    assert data["delays"][:4] == [1500, 2250, 3375, 5063]
    assert all(d <= 6000 for d in data["delays"]) and len(data["delays"]) >= 5
    assert data["clock"] <= 60000 + 6000 + 2000
    assert data["state"] == "loading" and data["counts"]["error"] == 0 and data["counts"]["loading"] == 3
    assert data["blocked"] == []
    assert data["verdict"]["retry"] is True and data["verdict"]["message"] == "timeout"


@needs_node
def test_a_part_being_swapped_in_is_never_aborted_nor_freed_twice():
    data = run(
        """
const q = createQueue({ max: 3 });
for (const [i, id] of ["a", "b", "c", "d", "e"].entries()) q.add({ id, order: i, priority: "high" });
out.start = ids(q.next());
out.marked = q.swapping("a");
out.dropWhileSwapping = q.drop("a"); // htmx:beforeCleanupElement of its own swap
out.loadingAfterDrop = q.counts().loading;
out.done = q.done("a");
out.loadingAfterDone = q.counts().loading;
out.doneTwice = q.done("a");
out.dropAfterDone = q.drop("a");
out.next = ids(q.next());
out.dropLoading = q.drop("b"); // a loading part cleaned up: the caller aborts it
out.dropTwice = q.drop("b");
out.loadingAfterAbort = q.counts().loading;
out.last = ids(q.next());
out.finalLoading = q.counts().loading;
"""
    )
    assert data["start"] == ["a", "b", "c"] and data["marked"] is True
    assert data["dropWhileSwapping"] is False and data["loadingAfterDrop"] == 3
    assert data["done"] is True and data["loadingAfterDone"] == 2
    assert data["doneTwice"] is False and data["dropAfterDone"] is False
    assert data["next"] == ["d"]
    assert data["dropLoading"] is True and data["dropTwice"] is False and data["loadingAfterAbort"] == 2
    assert data["last"] == ["e"] and data["finalLoading"] == 3


@needs_node
def test_a_group_is_keyed_by_name_and_url_and_forgotten_with_its_placeholders():
    data = run(
        """
const k = parts.groupKey;
out.same = k("overview", "/?year=2025&section=A&_part=kpis") === k("overview", "/?_part=figures&section=A&year=2025");
out.otherYear = k("overview", "/?year=2025&_part=kpis") === k("overview", "/?year=2024&_part=kpis");
out.otherName = k("overview", "/?year=2025") === k("brief", "/?year=2025");
out.none = k("", "/?x=1");
const a = k("pd", "/programmes/1/detail/?_part=funds");
const b = k("pd", "/programmes/2/detail/?_part=funds");
const q = createQueue({ max: 3 });
q.add({ id: "a-funds", order: 0, priority: "high", group: a });
q.add({ id: "a-ind", order: 1, priority: "normal", group: a, visible: true });
q.add({ id: "b-funds", order: 2, priority: "high", group: b });
q.add({ id: "b-ind", order: 3, priority: "normal", group: b, visible: true });
out.start = ids(q.next());
q.done("a-funds");
out.afterA = ids(q.next());
// the placeholders of group a are cleaned up (a filter swap): a new generation with the same address leads again
q.drop("a-ind");
q.add({ id: "a2-funds", order: 4, priority: "high", group: a });
q.add({ id: "a2-ind", order: 5, priority: "normal", group: a, visible: true });
q.done("b-funds");
out.next = ids(q.next());
"""
    )
    assert data["same"] is True and data["otherYear"] is False and data["otherName"] is False
    assert data["none"] is None
    assert data["start"] == ["a-funds", "b-funds"]  # two leaders, one per address
    assert data["afterA"] == ["a-ind"]  # group b still closed
    assert data["next"] == ["a2-funds", "b-ind"]  # a2-ind waits for its own leader


@needs_node
def test_detached_placeholders_are_dropped_and_never_asked_for():
    data = run(
        """
const gone = new Set(["b"]);
const dropped = [];
const q = createQueue({ max: 3, alive: (item) => !gone.has(item.id), onDrop: (item) => dropped.push(item.id) });
for (const [i, id] of ["a", "b", "c"].entries()) q.add({ id, order: i, priority: "high" });
out.fired = ids(q.next());
out.dropped = dropped;
out.state = q.state("b") ?? null;
out.size = q.size;
"""
    )
    assert data["fired"] == ["a", "c"] and data["dropped"] == ["b"]
    assert data["state"] is None and data["size"] == 2


@needs_node
def test_an_anchor_bump_goes_to_the_front_the_latest_first():
    data = run(
        """
const q = createQueue({ max: 1 });
q.add({ id: "kpis", order: 0, priority: "high" });
q.add({ id: "watch", order: 1, priority: "high" });
q.add({ id: "review", order: 2, priority: "normal", visible: false });
q.add({ id: "news", order: 3, priority: "low", visible: false });
q.bump("news");
q.bump("review");
const seen = [];
for (let i = 0; i < 4; i++) { const [item] = q.next(); seen.push(item.id); q.done(item.id); }
out.seen = seen;
"""
    )
    assert data["seen"] == ["review", "news", "kpis", "watch"]


@needs_node
def test_load_all_includes_strict_parts_and_resolves_once_all_have_finished_or_failed():
    data = run(
        """
const q = createQueue({ max: 3 });
q.add({ id: "lead", order: 0, priority: "high", group: "overview /?y=1" });
q.add({ id: "figures", order: 1, priority: "normal", group: "overview /?y=1", visible: false });
q.add({ id: "news", order: 2, priority: "low", visible: false });
q.add({ id: "more", order: 3, priority: "normal", strict: true, visible: false });
let resolved = false;
const all = q.loadAll().then(() => { resolved = true; });
out.first = ids(q.next()); // the cap goes back to 3, the leader still goes alone in its group
q.done("lead");
out.second = ids(q.next());
q.fail("news", 404);
q.done("figures");
await Promise.resolve();
out.beforeLast = resolved;
q.done("more");
await all;
out.resolved = resolved;
out.limitAfter = q.limit();
const empty = createQueue();
await empty.loadAll();
out.empty = true;
"""
    )
    assert data["first"] == ["lead", "more", "news"]
    assert data["second"] == ["figures"]
    assert data["beforeLast"] is False and data["resolved"] is True
    assert data["empty"] is True


@needs_node
def test_live_region_words_and_error_texts():
    data = run(
        """
out.texts = parts.TEXTS;
out.closing = [
  parts.closingMessage({ errors: 0, spoke: false }),
  parts.closingMessage({ errors: 0, spoke: true }),
  parts.closingMessage({ errors: 1, spoke: false }),
  parts.closingMessage({ errors: 3, spoke: true }),
];
out.many = parts.partText("failedMany", 3);
out.classify = [403, 404, "timeout", 500, 0].map((r) => parts.classify(r).message);
"""
    )
    texts = data["texts"]
    assert texts["forbidden"] == "You do not have access to this."
    assert texts["gone"] == "This part is no longer on the page. Reload the page."
    assert texts["timeout"] == "This part took too long. Try again, or narrow the filter."
    assert texts["generic"] == "This part could not be loaded."
    assert texts["loading"] == "Loading the figures…" and texts["done"] == "All figures are shown."
    assert data["closing"] == [None, "done", "failedOne", "failedMany"]
    assert data["many"] == "3 parts could not be loaded."
    assert data["classify"] == ["forbidden", "gone", "timeout", "generic", "generic"]


# ------------------------------------------------------------------------------------------ static rules
def _app() -> str:
    return (STATIC / "js" / "app.js").read_text()


def test_app_js_starts_parts_js_and_part_requests_raise_no_toasts():
    app = _app()
    assert re.search(r'^import \{[^}]*\binitParts\b[^}]*\} from "\./parts\.js";$', app, re.M)
    assert "initParts();" in app
    assert 'const isPartRequest = (e) => e.detail?.elt?.hasAttribute?.("data-part") === true;' in app
    for event in ("htmx:responseError", "htmx:sendError"):
        handler = app.split(f'addEventListener("{event}"', 1)[1].split("});", 1)[0]
        assert "if (isPartRequest(e)) return;" in handler, event
        assert handler.index("isPartRequest") < handler.index("toast(")


def test_page_only_keys_drop_the_part_keys_from_export_links():
    keys = re.search(r"const PAGE_ONLY_KEYS = \[([^\]]*)\];", _app()).group(1)
    names = re.findall(r'"([^"]+)"', keys)
    assert "_part" in names and "_eager" in names and "tab" in names


def test_filter_bars_abort_on_their_form_never_on_body_and_modules_start_independently():
    app = _app()
    bar = app.split("function initFilterBar(form)", 1)[1].split("\n}\n", 1)[0]
    assert 'window.htmx.trigger(form, "htmx:abort")' in bar
    assert "source: form" in bar
    assert "document.body" not in bar
    # htmx adds a form's fields to a request issued on it: the filter bar's own address must stay the query
    assert "e.detail.parameters = {};" in bar
    modules = app.split("function initModules(root)", 1)[1].split("\n}\n", 1)[0]
    assert "Promise.allSettled(elements.map(startModule))" in modules
    assert "return modules;" in app.split("function enhance(", 1)[1].split("\n}\n", 1)[0]
    assert "trackModules(enhance(e.detail.elt))" in app and "trackModules(enhance(document))" in app
    autoprint = app.split("async function printWhenReady", 1)[1].split("\n}\n", 1)[0]
    assert "pendingParts()" in autoprint and "await loadAll({ timeout: 60000 })" in autoprint


def test_base_html_reloads_on_history_and_has_the_cap_and_the_live_region():
    base = (TEMPLATES / "base.html").read_text()
    raw = re.search(r"<meta name=\"htmx-config\" content='([^']*)'>", base).group(1)
    config = json.loads(raw.replace("{{ request.csp_nonce }}", "nonce"))
    assert config["historyCacheSize"] == 0
    assert config["refreshOnHistoryMiss"] is True
    assert config["historyRestoreAsHxRequest"] is False
    assert config["allowEval"] is False and config["inlineScriptNonce"] == "nonce"
    assert '<meta name="nd-parts-parallel" content="3">' in base
    after_toasts = base.split('id="toasts"', 1)[1]
    status = re.search(r'<div id="parts-status"[^>]*>', after_toasts, re.S).group(0)
    assert (
        'class="visually-hidden"' in status and 'role="status"' in status and 'aria-live="polite"' in status
    )
    for key in (
        "loading",
        "done",
        "failed-one",
        "failed-many",
        "forbidden",
        "gone",
        "timeout",
        "generic",
        "preparing",
    ):
        assert f'data-text-{key}="{{{{ t_' in status, key


def test_a_rendered_page_carries_the_live_region_with_its_words_and_the_history_settings(client_viewer):
    html = client_viewer.get(reverse("reports:library")).content.decode()
    status = re.search(r'<div id="parts-status"[^>]*>', html, re.S).group(0)
    assert 'data-text-loading="Loading the figures…"' in status
    assert 'data-text-failed-many="{n} parts could not be loaded."' in status
    assert 'data-text-forbidden="You do not have access to this."' in status
    assert 'data-text-preparing="Preparing the page…"' in status
    assert "{{" not in status and "{%" not in status
    assert '<meta name="nd-parts-parallel" content="3">' in html
    assert '"historyCacheSize":0,"refreshOnHistoryMiss":true,"historyRestoreAsHxRequest":false' in html


def test_parts_js_names_the_htmx_event_order_it_relies_on():
    js = (STATIC / "js" / "parts.js").read_text()
    head = js.split("\nimport ", 1)[0]
    for words in (
        "htmx:beforeSwap",
        "htmx:beforeCleanupElement",
        "htmx:afterRequest",
        "htmx:sendAbort",
        "htmx:load",
        "source: form",
        "2.0.10",
    ):
        assert words in head, words
    assert "export function createQueue(" in js


def test_placeholders_keep_their_height_in_both_themes_and_each_later_stage_has_its_block():
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    for selector in (
        ".part { min-height: var(--part-h, 8rem); }",
        ".part--tiles { --part-h: calc(88px * var(--part-rows, 1)",
        ".part.is-loaded { min-height: 0; }",
        '.part[data-part-state="slow"] .part__slow { display: block; }',
        '.part[aria-busy="true"] .part__print-note { display: block; }',
        ".part + .panel, .panel + .part, .part + .part, .stack > .part + * { margin-top: 1rem; }",
        ".fmm-tab > .part",
        ".overview-row > .part",
    ):
        assert selector in block, selector
    # colours only from the theme tokens, which the dark theme redefines
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", block)
    assert "var(--nd-danger)" in block and "var(--nd-border)" in block
    for stage in ("A", "B", "C", "D1", "D2"):
        assert f"stage {stage} (" in css and f"end of stage {stage} */" in css, stage
    assert css.rstrip().endswith("end of stage D2 */")
