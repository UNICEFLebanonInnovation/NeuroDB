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
from urllib.parse import urljoin

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


# initParts(), the page's glue, on the stand-in page of parts_dom.mjs (elements, events, a clock the test
# moves, and a fake htmx answering part requests in htmx 2.0.10's order). page() gives a fresh page and a
# fresh copy of parts.js for it.
DOM_PRELUDE = """
const dom = await import(process.argv[2]);
const partsUrl = process.argv[1];
const out = {};
let pages = 0;
async function page(options = {}) {
  const env = dom.installPage(options);
  pages += 1;
  const parts = await import(`${partsUrl}?page=${pages}`);
  return { ...env, parts };
}
const states = (doc) => [...doc.querySelectorAll("[data-part]")].map((el) => `${el.dataset.part}:${el.dataset.partState}`);
"""


def run_dom(body: str) -> dict:
    """Run ``body`` in node after DOM_PRELUDE (``page()`` and ``out``) and return ``out``."""
    script = DOM_PRELUDE + body + "\nconsole.log(JSON.stringify(out));\n"
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            script,
            (STATIC / "js" / "parts.js").as_uri(),
            (Path(__file__).parent / "parts_dom.mjs").as_uri(),
        ],
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


@needs_node
def test_a_batch_ends_with_its_own_failures_never_an_earlier_one():
    data = run(
        """
const said = [];
const timers = [];
const q = createQueue({ max: 3 });
const update = parts.createAnnouncer(q, {
  say: (key, n) => said.push(parts.partText(key, n)),
  schedule: (fn) => timers.push(fn) - 1,
  cancel: (i) => { timers[i] = null; },
});
const step = (fn) => { fn(); update(); };
q.add({ id: "news", order: 0, priority: "low", visible: true });
step(() => q.next());
step(() => q.fail("news", 404));
out.first = [...said];
// a later batch that loads fine, slow enough that "loading" is said, then a quick one
q.add({ id: "more", order: 1, strict: true, visible: true });
step(() => q.next());
timers.at(-1)?.();
step(() => q.done("more"));
q.add({ id: "quick", order: 2, priority: "high" });
step(() => q.next());
step(() => q.done("quick"));
out.later = said.slice(out.first.length);
// a batch with a failure of its own says only that one
q.add({ id: "x", order: 3, priority: "high" });
step(() => q.next());
step(() => q.fail("x", 403));
out.own = said.at(-1);
out.mark = q.mark();
out.since = [q.errorsSince(0), q.errorsSince(1), q.errorsSince(out.mark)];
out.closing = [
  parts.closingMessage({ errors: 0, said: "failedOne", saidCount: 1, left: 0 }),
  parts.closingMessage({ errors: 0, said: "failedMany", saidCount: 2, left: 2 }),
  parts.closingMessage({ errors: 0, said: "done", left: 0 }),
  parts.closingMessage({ errors: 0, said: "failedMany", saidCount: 2, left: 1 }),
  parts.closingMessage({ errors: 0, said: "failedOne", saidCount: 1, left: 2 }),
  parts.closingMessage({ errors: 0, spoke: true, said: "loading", left: 1 }),
  parts.closingMessage({ errors: 0, spoke: true, said: "loading", left: 3 }),
];
"""
    )
    assert data["first"] == ["1 part could not be loaded."]
    # "loading" was said, and news is still in error: never "All figures are shown." over its error box
    assert data["later"] == ["Loading the figures…", "1 part could not be loaded."]
    assert data["own"] == "1 part could not be loaded."
    assert data["since"] == [2, 1, 0]
    # every failure put right: the region says so; still as many in error as it says: it keeps its word; fewer:
    # it says how many are left; more (an earlier failure): not said again; after "loading": how many are left
    assert data["closing"] == ["done", None, None, "failedOne", None, "failedOne", "failedMany"]


# ------------------------------------------------------------------------------------------ the page's glue
@needs_node
def test_discovery_waits_for_htmx_load_and_a_202_is_asked_again_without_an_error():
    data = run_dom(
        """
const { doc, win, htmx, clock, main, placeholder, loaded, start, parts } = await page();
main.append(placeholder({ name: "a", priority: "high" }), placeholder({ name: "b", priority: "high" }));
parts.initParts();
doc.dispatchEvent(new dom.Event("DOMContentLoaded"));
win.dispatchEvent(new dom.Event("load"));
await clock.tick(100);
out.beforeHtmx = htmx.sent();
await start();
out.afterHtmx = htmx.sent();
htmx.respond("a", { status: 202, headers: { "X-NeuroDB-Part-Pending": "1" } });
await clock.tick(100);
const a = doc.getElementById("part-a");
out.pending = { attached: a.isConnected, state: a.dataset.partState, errorShown: !a.querySelector("[data-part-error]").hidden };
await clock.tick(1300);
out.before1500 = htmx.sent();
await clock.tick(200);
out.after1500 = htmx.sent();
htmx.respond("a", { nodes: () => [loaded("a")] });
await clock.tick(100);
out.loaded = !!doc.querySelector("[data-part-loaded='a']");
out.states = states(doc);
"""
    )
    assert data["beforeHtmx"] == []  # never on DOMContentLoaded or load: htmx has not bound the trigger yet
    assert data["afterHtmx"] == ["a", "b"]
    assert data["pending"] == {"attached": True, "state": "loading", "errorShown": False}
    assert data["before1500"] == ["a", "b"] and data["after1500"] == ["a", "b", "a"]
    assert data["loaded"] is True and data["states"] == ["b:loading"]


@needs_node
def test_a_part_replaced_by_its_own_answer_is_done_not_dropped():
    data = run_dom(
        """
const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page({ parallel: 1 });
main.append(placeholder({ name: "a", priority: "high" }), placeholder({ name: "b", priority: "high" }));
parts.initParts();
await start();
htmx.respond("a", { status: 404 });
htmx.respond("b", { nodes: () => [loaded("b")] });
await clock.tick(100);
out.error = states(doc);
doc.getElementById("part-a").querySelector("[data-part-retry]").click();
out.retried = htmx.open();
// its answer replaces it: htmx:beforeCleanupElement fires on it during that swap, before htmx:afterRequest
htmx.respond("a", { nodes: () => [loaded("a")] });
await clock.tick(100);
out.aborted = htmx.requests.filter((r) => r.state === "aborted").length;
// the focus was on the placeholder (Try again): it goes to the new wrapper, which only an arrival does
const active = doc.activeElement;
out.focus = { id: active.id, attached: active.isConnected, loaded: active.dataset.partLoaded ?? null };
out.pending = parts.pendingParts();
"""
    )
    assert data["error"] == ["a:error"]
    assert data["retried"] == ["a"]
    assert data["aborted"] == 0
    assert data["focus"] == {"id": "part-a", "attached": True, "loaded": "a"}
    assert data["pending"] == 0


@needs_node
def test_an_answer_that_is_not_the_part_asked_for_is_never_swapped_in():
    data = run_dom(
        """
// after a rollback, a release without parts answers each part with its whole page: 200, no X-NeuroDB-Part
const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page();
out.answers = [
  parts.answersPart("/?year=2025&_part=kpis", "kpis"),
  parts.answersPart("/?year=2025&_part=kpis", null),
  parts.answersPart("/?year=2025&_part=kpis", "figures"),
  parts.answersPart("/watch/card/", null),
];
main.append(
  placeholder({ name: "kpis", priority: "high" }),
  placeholder({ name: "for-you", priority: "high", url: "/watch/card/", swap: "innerHTML" }),
);
parts.initParts();
await start();
const wholePage = () => [doc.h("header", { class: "topbar" }), doc.h("main", { id: "main", class: "main" }, doc.h("h1", {}, "Country overview"))];
htmx.respond("kpis", { nodes: wholePage, asPart: false });
// a url= part asks an existing endpoint, which sends no such header: its answer is its own
htmx.respond("for-you", { nodes: () => [doc.h("section", { class: "panel watch-overview" })] });
await clock.tick(100);
const kpis = doc.getElementById("part-kpis");
out.mains = doc.querySelectorAll("main").length;
out.states = states(doc);
out.text = kpis.querySelector("[data-part-error-text]").textContent;
out.errorShown = !kpis.querySelector("[data-part-error]").hidden;
out.forYou = !!doc.querySelector("section.watch-overview") && doc.getElementById("part-for-you").classList.contains("is-loaded");
await clock.tick(5000); // never asked again by itself: the same release would answer the same
out.sent = htmx.sent();
out.said = [...doc.said];
// Try again, answered by this release: the part comes in
kpis.querySelector("[data-part-retry]").click();
htmx.respond("kpis", { nodes: () => [loaded("kpis")] });
await clock.tick(100);
out.retried = { mains: doc.querySelectorAll("main").length, loaded: !!doc.querySelector("[data-part-loaded='kpis']") };
"""
    )
    assert data["answers"] == [True, False, False, True]
    assert data["mains"] == 1  # the page is not nested in itself
    assert data["states"] == ["kpis:error"] and data["errorShown"] is True
    assert data["text"] == "This part is no longer on the page. Reload the page."
    assert data["forYou"] is True
    assert data["sent"] == ["kpis", "for-you"]
    assert data["said"] == ["1 part could not be loaded."]
    assert data["retried"] == {"mains": 1, "loaded": True}


@needs_node
def test_the_skip_link_and_a_link_to_the_review_never_load_the_folded_findings():
    data = run_dom(
        """
const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page();
main.append(
  placeholder({ name: "kpis", kind: "tiles", priority: "high" }),
  doc.h("div", { class: "overview-row", id: "daily-review" }, placeholder({ name: "review" })),
  placeholder({ name: "news", kind: "list", priority: "low" }),
  doc.h("a", { id: "to-review", href: "#daily-review" }, "The daily review"),
  doc.h("a", { id: "to-more", href: "#part-review-more" }, "The other findings"),
);
parts.initParts();
await start();
const more = () => doc.h("details", { class: "review-more", id: "more" }, doc.h("summary", {}, "Show 3 more findings"),
  placeholder({ name: "review-more", kind: "list", lazy: "strict" }));
htmx.respond("kpis", { nodes: () => [loaded("kpis")] });
htmx.respond("review", { nodes: () => [loaded("review", more())] });
htmx.respond("news", { nodes: () => [loaded("news")] });
await clock.tick(100);
out.registered = doc.getElementById("part-review-more").dataset.partState;
doc.getElementById("skip").click(); // "Skip to main content": #main holds every part
await clock.tick(1000);
out.afterSkip = htmx.sent();
doc.getElementById("to-review").click();
await clock.tick(1000);
out.afterReview = htmx.sent();
out.scrolls = doc.scrolls.length;
out.open = doc.getElementById("more").open;
doc.getElementById("to-more").click(); // a link to the folded findings themselves loads them
await clock.tick(100);
out.afterMore = htmx.sent();
// the wrapper still counts for a part not yet in: a link to it puts that part first
{
  const { doc, htmx, clock, main, placeholder, start, parts } = await page({ parallel: 1 });
  main.append(
    placeholder({ name: "kpis", kind: "tiles", priority: "high" }),
    placeholder({ name: "for-you", priority: "high" }),
    doc.h("div", { id: "daily-review" }, placeholder({ name: "review" })),
    doc.h("a", { id: "to-review", href: "#daily-review" }, "The daily review"),
  );
  parts.initParts();
  await start();
  doc.getElementById("to-review").click(); // review is normal, for-you high: without the link for-you goes first
  htmx.respond("kpis", { nodes: () => [] });
  await clock.tick(100);
  out.wrapperFirst = htmx.sent();
}
"""
    )
    assert data["registered"] == "queued"
    assert sorted(data["afterSkip"]) == ["kpis", "news", "review"]
    assert sorted(data["afterReview"]) == ["kpis", "news", "review"]
    assert data["scrolls"] == 0 and data["open"] is False
    assert data["afterMore"][-1] == "review-more"
    assert data["wrapperFirst"] == ["kpis", "review"]


@needs_node
def test_live_region_reports_each_batch_and_clears_a_failure_put_right():
    data = run_dom(
        """
// news fails; then the review's "Show more" opens and its folded findings load (slowly) without trouble
{
  const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page();
  main.append(doc.h("div", { id: "daily-review" }, placeholder({ name: "review" })), placeholder({ name: "news", priority: "low" }));
  parts.initParts();
  await start();
  const more = () => doc.h("details", { id: "more" }, doc.h("summary", {}, "Show 3 more findings"),
    placeholder({ name: "review-more", kind: "list", lazy: "strict" }));
  htmx.respond("review", { nodes: () => [loaded("review", more())] });
  htmx.respond("news", { status: 404 });
  await clock.tick(200);
  out.first = [...doc.said];
  const details = doc.getElementById("more");
  details.open = true;
  details.dispatchEvent(new dom.Event("toggle", { bubbles: false }));
  await clock.tick(1300);
  htmx.respond("review-more", { nodes: () => [loaded("review-more")] });
  await clock.tick(200);
  out.more = doc.said.slice(out.first.length);
  out.moreStates = states(doc);
}
// two parts fail; Try again on each, answered at once: the region always says how many are still in error
{
  const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page();
  main.append(placeholder({ name: "a", priority: "high" }), placeholder({ name: "b", priority: "high" }), placeholder({ name: "c", priority: "high" }));
  parts.initParts();
  await start();
  htmx.respond("a", { status: 404 });
  htmx.respond("b", { nodes: () => [loaded("b")] });
  htmx.respond("c", { status: 404 });
  await clock.tick(200);
  out.failed = [...doc.said];
  doc.getElementById("part-a").querySelector("[data-part-retry]").click();
  await clock.tick(300);
  htmx.respond("a", { nodes: () => [loaded("a")] });
  await clock.tick(200);
  out.afterA = doc.said.slice(out.failed.length);
  out.statusAfterA = doc.getElementById("parts-status").textContent;
  out.statesAfterA = states(doc);
  doc.getElementById("part-c").querySelector("[data-part-retry]").click();
  await clock.tick(300);
  htmx.respond("c", { nodes: () => [loaded("c")] });
  await clock.tick(200);
  out.afterC = doc.said.slice(out.failed.length);
  out.status = doc.getElementById("parts-status").textContent;
}
"""
    )
    assert data["first"] == ["1 part could not be loaded."]
    # the folded findings are in, news is not: never "All figures are shown." while its error box shows
    assert data["more"] == ["Loading the figures…", "1 part could not be loaded."]
    assert data["moreStates"] == ["news:error"]
    assert data["failed"] == ["2 parts could not be loaded."]
    # a is in, c is not: the region no longer says 2
    assert data["afterA"] == ["1 part could not be loaded."]
    assert data["statusAfterA"] == "1 part could not be loaded." and data["statesAfterA"] == ["c:error"]
    assert data["afterC"] == ["1 part could not be loaded.", "All figures are shown."]
    assert data["status"] == "All figures are shown."


# A request htmx answers with a swap, in htmx 2.0.10's order (parts.js's header, point 2): htmx:beforeSwap on the
# target, the swap (innerHTML: the target's content; outerHTML: the target itself, by `fresh`, which may be empty),
# then htmx:afterRequest on the element that asked and, when the swap removed it, again on its closest ancestor still
# in the page; 20 ms later htmx:afterSettle on each element settled (the target for innerHTML, the new top-level
# elements for outerHTML: none for an empty answer). `trigger` is the type of the event that issued it (none for
# htmx.ajax, a poll or a load).
ASK = """
let asked = 0;
function ask(htmx, elt, target, fresh, { trigger, swap = "outerHTML", status = 200 } = {}) {
  asked += 1;
  const xhr = { status, asked, getResponseHeader: () => null };
  const requestConfig = { elt, triggeringEvent: trigger ? { type: trigger } : undefined };
  const hierarchy = [];
  for (let node = elt.parentNode; node && !node.isDocument; node = node.parentNode) hierarchy.push(node);
  const before = { xhr, requestConfig, target, shouldSwap: status < 400, isError: status >= 400 };
  htmx.fire(target, "htmx:beforeSwap", before);
  if (before.shouldSwap) {
    if (swap === "innerHTML") htmx.swapInner(target, fresh);
    else {
      fresh.forEach((node) => target.parentNode.insertBefore(node, target));
      target.remove();
    }
    const settled = swap === "innerHTML" ? [target] : fresh;
    setTimeout(() => settled.forEach((node) => htmx.fire(node, "htmx:afterSettle", { xhr, requestConfig, target })), 20);
  }
  const info = { xhr, requestConfig, target, successful: status < 400, failed: status >= 400 };
  htmx.fire(elt, "htmx:afterRequest", { ...info });
  const still = !elt.isConnected && hierarchy.find((node) => node.isConnected);
  if (still) htmx.fire(still, "htmx:afterRequest", { ...info });
}
"""


@needs_node
def test_a_swap_of_a_results_area_that_is_not_live_is_said_once():
    """A results area that can hold parts is not aria-live (each arriving part would be read out). Monitoring insights'
    tabs and the document review's tabs, views and filters swap such an area (#fmm-results, #review-results) with no
    part in most answers, and screen readers heard nothing of the change (review R2-critic-2). parts.js now says the
    area's own words (data-results-updated) once in #parts-status after a swap of the area itself, however it was
    asked for (a tab link, the filter bar's htmx.ajax); a part, a poll or what loads when reached swapped inside it
    (the Documents tab's poll, the AI card's) is not said, nor is an area without the words."""
    data = run_dom(
        ASK
        + """
const { doc, htmx, clock, main, placeholder, loaded, start, parts } = await page();
const tabs = doc.h("nav", {}, doc.h("a", { id: "tab-quality" }, "Quality"));
const form = doc.h("form", { id: "filters" });
const area = doc.h("div", { id: "fmm-results", "data-results-updated": "The results were updated." },
  doc.h("section", { id: "fmm-insights" }, "AI brief"), placeholder({ name: "map", kind: "map" }));
const plain = doc.h("div", { id: "edu-results" }, doc.h("p", { id: "edu-p" }, "Figures"));
main.append(tabs, form, area, plain);
parts.initParts();
await start();
htmx.respond("map", { nodes: () => [loaded("map")] });
await clock.tick(1500);
out.part = [...doc.said];
// the tab bar: the area's content swapped (innerHTML)
ask(htmx, doc.getElementById("tab-quality"), area, [doc.h("section", { id: "fmm-quality" }, "Quality")], { trigger: "click", swap: "innerHTML" });
await clock.tick(100);
out.tab = doc.said.slice(out.part.length);
out.status = doc.getElementById("parts-status").textContent;
// the filter bar (htmx.ajax with the form as source: no triggering event)
let seen = doc.said.length;
ask(htmx, form, area, [doc.h("section", { id: "fmm-insights" }, "AI brief")], { swap: "innerHTML" });
await clock.tick(100);
out.filter = doc.said.slice(seen);
// inside it: the AI card's poll (outerHTML of itself), a panel loaded when reached, a list opened; an area that gives
// no words; an answer refused
seen = doc.said.length;
const card = doc.getElementById("fmm-insights");
ask(htmx, card, card, [doc.h("section", { id: "fmm-insights" }, "AI brief, newer")]);
const panel = doc.h("div", { id: "fmm-rule-trends-panel" }, "…");
area.append(panel);
ask(htmx, panel, panel, [doc.h("div", { id: "fmm-rule-trends-panel" }, "Trends")], { trigger: "revealed" });
const rest = doc.h("div", { id: "fmm-places-rest" });
area.append(doc.h("details", {}, rest));
ask(htmx, rest, rest, [doc.h("div", { id: "fmm-places-rest" }, "More places")], { trigger: "toggle" });
ask(htmx, doc.getElementById("edu-p"), plain, [doc.h("p", {}, "Other figures")], { trigger: "click", swap: "innerHTML" });
ask(htmx, doc.getElementById("tab-quality"), area, [], { trigger: "click", swap: "innerHTML", status: 500 });
await clock.tick(100);
out.inside = doc.said.slice(seen);
// a second change of tab is said again
seen = doc.said.length;
ask(htmx, doc.getElementById("tab-quality"), area, [doc.h("section", {}, "Analysis")], { trigger: "click", swap: "innerHTML" });
await clock.tick(100);
out.again = doc.said.slice(seen);
"""
    )
    # a part arriving is said in parts.js's own words only
    assert "The results were updated." not in data["part"]
    assert data["tab"] == ["The results were updated."] and data["status"] == "The results were updated."
    assert data["filter"] == ["The results were updated."]
    assert data["inside"] == []
    assert data["again"] == ["The results were updated."]


@needs_node
def test_a_change_asked_for_inside_a_results_area_is_said_once():
    """In the base, #fmm-results and #review-results were aria-live, so every swap inside them was read out as well.
    Once they were not, the changes people make inside them were silent: Monitoring insights' records pages, sort
    headers and rows per page (#fmm-visits), its entity chips (#fmm-entities), the document review's Accept, Reject
    and Undo, an action point's status, a finding deleted (hx-target "closest tr"), and a page of partner
    monitoring's grid (review R3-fixes-2). A swap inside an area that a click, a submit or a change asked for is said
    in the area's words, once per request, even when it removes the element that asked or leaves nothing in place."""
    data = run_dom(
        ASK
        + """
const { doc, htmx, clock, main, start, parts } = await page();
const said = async (fn) => {
  const seen = doc.said.length;
  fn();
  await clock.tick(100);
  return doc.said.slice(seen);
};
const row = (id) => {
  const form = doc.h("form", { id: `${id}-verdict` }, doc.h("button", { type: "submit" }, "Accept"));
  return doc.h("tr", { id }, doc.h("td", {}, form));
};
const visits = doc.h("div", { id: "fmm-visits" }, doc.h("a", { id: "page-2" }, "2"), doc.h("select", { id: "per-page" }));
const entities = doc.h("div", { id: "fmm-entities" }, doc.h("a", { id: "chip-partner" }, "Partner"));
const tbody = doc.h("tbody", {}, row("finding-1"), row("finding-2"), row("action-3"));
const area = doc.h("div", { id: "review-results", "data-results-updated": "The results were updated." },
  visits, entities, doc.h("table", {}, tbody));
main.append(area);
parts.initParts();
await start();
// a page link and the rows per page of the records table (innerHTML of #fmm-visits)
out.page = await said(() => ask(htmx, doc.getElementById("page-2"), visits, [doc.h("a", { id: "page-3" }, "3"), doc.h("select", { id: "per-page" })], { trigger: "click", swap: "innerHTML" }));
out.perPage = await said(() => ask(htmx, doc.getElementById("per-page"), visits, [doc.h("p", {}, "50 rows")], { trigger: "change", swap: "innerHTML" }));
// an entity chip (outerHTML of #fmm-entities, which holds the chip)
out.chip = await said(() => ask(htmx, doc.getElementById("chip-partner"), entities, [doc.h("div", { id: "fmm-entities" }, "Partners")], { trigger: "click" }));
// Accept on a finding: its form is in the row the answer replaces
out.verdict = await said(() => ask(htmx, doc.getElementById("finding-1-verdict"), doc.getElementById("finding-1"), [doc.h("tr", { id: "finding-1" }, "Accepted")], { trigger: "submit" }));
// a status changed, a finding deleted (the answer is empty: nothing is left to settle)
out.status = await said(() => ask(htmx, doc.getElementById("action-3-verdict"), doc.getElementById("action-3"), [doc.h("tr", { id: "action-3" }, "Done")], { trigger: "change" }));
out.deleted = await said(() => ask(htmx, doc.getElementById("finding-2-verdict"), doc.getElementById("finding-2"), [], { trigger: "submit" }));
out.gone = doc.getElementById("finding-2") === null;
// a refused verdict (403) changes nothing and says nothing
out.refused = await said(() => ask(htmx, doc.getElementById("finding-1"), doc.getElementById("finding-1"), [], { trigger: "submit", status: 403 }));
"""
    )
    for action in ("page", "perPage", "chip", "verdict", "status", "deleted"):
        assert data[action] == ["The results were updated."], action
    assert data["gone"] is True
    assert data["refused"] == []


@needs_node
def test_the_ai_card_speaks_for_itself_in_the_results_area():
    """Monitoring insights' AI card (#fmm-insights) is in the results area, but says what a person asked for in its
    own words, in the Insights tab's live line (the brief being written, then ready or refused: fmm.views.insights,
    tests/fmm/test_insights.py). Its empty data-results-updated keeps parts.js from saying the area's "The results
    were updated." when Regenerate swaps it for the card that polls, before any brief exists; the polls, still
    writing or with the brief, are never said by parts.js either (review R1-C-1). A control: the same click on a
    card without the empty words says the area's words."""
    data = run_dom(
        ASK
        + """
const { doc, htmx, clock, main, start, parts } = await page();
const card = (busy, text) => doc.h("section", { id: "fmm-insights", "data-results-updated": "", ...(busy ? { "aria-busy": "true" } : {}) },
  doc.h("button", { id: "regenerate" }, text));
const area = doc.h("div", { id: "fmm-results", "data-results-updated": "The results were updated." }, card(false, "Brief"));
main.append(area);
parts.initParts();
await start();
const said = async (act) => { const seen = doc.said.length; act(); await clock.tick(100); return doc.said.slice(seen); };
const here = () => doc.getElementById("fmm-insights");
out.regenerate = await said(() => ask(htmx, doc.getElementById("regenerate"), here(), [card(true, "Writing…")], { trigger: "click" }));
out.writing = await said(() => ask(htmx, here(), here(), [card(true, "Writing…")]));
out.ready = await said(() => ask(htmx, here(), here(), [card(false, "The brief")]));
here().removeAttribute("data-results-updated");
out.control = await said(() => ask(htmx, doc.getElementById("regenerate"), here(), [card(true, "Writing…")], { trigger: "click" }));
"""
    )
    assert data["regenerate"] == [] and data["writing"] == [] and data["ready"] == []
    assert data["control"] == ["The results were updated."]


@needs_node
def test_a_swap_removing_queued_placeholders_never_asks_for_them():
    data = run_dom(
        """
for (const wrapped of [false, true]) {
  const { doc, htmx, clock, main, placeholder, start, parts } = await page({ parallel: 3 });
  const holders = Array.from({ length: 8 }, (_, i) => placeholder({ name: `old${i}`, priority: "high" }));
  const area = doc.h("div", { id: "area" }, wrapped ? [doc.h("section", { class: "stack" }, holders)] : holders);
  main.append(area);
  parts.initParts();
  await start();
  const first = htmx.sent();
  // a tab swap of the area: the new content brings two placeholders of its own
  htmx.swapInner(area, [doc.h("section", {}, placeholder({ name: "new0", priority: "high" }), placeholder({ name: "new1", priority: "high" }))]);
  await clock.tick(100);
  out[wrapped ? "wrapped" : "direct"] = {
    first,
    after: htmx.sent(),
    aborted: htmx.requests.filter((r) => r.state === "aborted").map((r) => r.name),
  };
}
"""
    )
    for case in ("direct", "wrapped"):
        result = data[case]
        assert result["first"] == ["old0", "old1", "old2"], case
        assert result["aborted"] == ["old0", "old1", "old2"], case
        assert result["after"] == ["old0", "old1", "old2", "new0", "new1"], case  # nothing for old3-old7


@needs_node
def test_an_anchor_scroll_is_instant_and_holds_the_target_while_the_parts_above_arrive():
    data = run_dom(
        """
const build = async (options) => {
  const env = await page({ hash: "#daily-review", ...options });
  const { doc, main, placeholder } = env;
  main.append(
    placeholder({ name: "kpis", kind: "tiles", priority: "high" }),
    placeholder({ name: "for-you", priority: "high" }),
    doc.h("div", { id: "daily-review" }, placeholder({ name: "review" })),
    placeholder({ name: "news", priority: "low" }),
  );
  env.parts.initParts();
  await env.start();
  return env;
};
const answer = async ({ htmx, clock, loaded }, name) => {
  htmx.respond(name, { nodes: () => [loaded(name)] });
  await clock.tick(50);
};
const scrolls = (doc) => doc.scrolls.map((s) => `${s.id} ${s.options?.block} ${s.options?.behavior}`);
{
  const env = await build();
  out.sent = env.htmx.sent();
  await answer(env, "review");
  out.first = scrolls(env.doc);
  await answer(env, "kpis");
  await answer(env, "for-you");
  out.held = scrolls(env.doc);
  await answer(env, "news"); // below the target: no scroll
  out.end = scrolls(env.doc);
}
{
  // the person scrolls (wheel) once the target is shown: nothing scrolls it back
  const env = await build();
  await answer(env, "review");
  env.win.dispatchEvent(new dom.Event("wheel"));
  await answer(env, "kpis");
  out.wheel = scrolls(env.doc).length;
}
{
  // or drags the scroll bar: the target moves on screen while the page scrolls
  const env = await build();
  await answer(env, "review");
  await env.clock.tick(50);
  env.doc.getElementById("daily-review").rect.top = -400;
  env.win.dispatchEvent(new dom.Event("scroll"));
  await answer(env, "kpis");
  out.bar = scrolls(env.doc).length;
}
{
  // the browser's own smooth scroll to the #anchor lands one more step just after: the next frames put it back
  const env = await build();
  env.htmx.respond("review", { nodes: () => [env.loaded("review")] });
  await env.clock.tick(25); // scrolled at 20 ms; the frames come at 36 and 52 ms
  env.doc.getElementById("daily-review").rect.top = 67;
  env.win.dispatchEvent(new dom.Event("scroll"));
  await env.clock.tick(50);
  out.stray = scrolls(env.doc).length;
  env.doc.getElementById("daily-review").rect.top = 0;
  await answer(env, "kpis");
  out.strayHeld = scrolls(env.doc).length; // not taken for the person scrolling: the hold goes on
}
"""
    )
    assert data["sent"][0] == "review"  # the anchor's part first
    assert data["first"] == ["daily-review start instant"]
    assert data["held"] == ["daily-review start instant"] * 3
    assert data["end"] == data["held"]
    assert data["wheel"] == 1 and data["bar"] == 1
    assert data["stray"] == 2 and data["strayHeld"] == 3


@needs_node
def test_a_link_into_a_part_inside_a_part_waits_for_both_and_lands_on_its_target():
    data = run_dom(
        """
// The partner page: its figures (datamart) bring the Monitoring insights placeholder (fmm, low, after a normal one),
// whose answer holds the panel #fmm-partner and its title #fmm-partner-title. Up to round 1 of step 2 a link to
// either waited for the figures only, found no panel there and dropped: the page stayed at the top.
const build = async (hash) => {
  const env = await page({ hash, parallel: 1 });
  const { doc, main, placeholder } = env;
  main.append(
    placeholder({ name: "datamart", kind: "tiles", priority: "high", anchors: "activityinfo-reporting partner- fmm-partner" }),
    doc.h("section", { id: "knowledge" }),
  );
  env.parts.initParts();
  await env.start();
  return env;
};
const figures = ({ doc, loaded, placeholder }) => () => [
  loaded("datamart", doc.h("div", { class: "kpi-grid" }), placeholder({ name: "activityinfo" }),
    placeholder({ name: "fmm", priority: "low", anchors: "fmm-partner" }), doc.h("section", { id: "partner-pds" })),
];
const panel = ({ doc, loaded }) => () => [
  loaded("fmm", doc.h("section", { id: "fmm-partner" }, doc.h("h2", { id: "fmm-partner-title" }, "Monitoring insights"))),
];
const scrolls = (doc) => doc.scrolls.map((s) => `${s.id} ${s.options?.block} ${s.options?.behavior}`);
for (const [key, hash] of [["panel", "#fmm-partner"], ["title", "#fmm-partner-title"]]) {
  const env = await build(hash);
  const { doc, htmx, clock } = env;
  htmx.respond("datamart", { nodes: figures(env) });
  await clock.tick(50);
  const between = { sent: htmx.sent(), scrolls: scrolls(doc) };
  htmx.respond("fmm", { nodes: panel(env) });
  await clock.tick(50);
  out[key] = { between, sent: htmx.sent(), scrolls: scrolls(doc), marked: doc.getElementById(hash.slice(1)).classList.contains("is-target") };
}
{
  // a partner whose panel turns out empty: nothing to land on, nothing asked for twice
  const env = await build("#fmm-partner");
  env.htmx.respond("datamart", { nodes: figures(env) });
  await env.clock.tick(50);
  env.htmx.respond("fmm", { nodes: () => [env.loaded("fmm")] });
  await env.clock.tick(50);
  env.htmx.respond("activityinfo", { nodes: () => [env.loaded("activityinfo")] });
  await env.clock.tick(5000);
  out.empty = { sent: env.htmx.sent(), scrolls: scrolls(env.doc) };
}
{
  // the person scrolls once the figures are in: the panel arriving later does not take the page there
  const env = await build("#fmm-partner");
  env.htmx.respond("datamart", { nodes: figures(env) });
  await env.clock.tick(50);
  env.win.dispatchEvent(new dom.Event("wheel"));
  env.htmx.respond("fmm", { nodes: panel(env) });
  await env.clock.tick(50);
  out.wheel = scrolls(env.doc);
}
{
  // parts that keep bringing parts and never the target: the link stops looking three parts deep
  const env = await page({ hash: "#deep-end", parallel: 1 });
  const { doc, main, placeholder, loaded, htmx, clock } = env;
  const deep = (n) => placeholder({ name: `deep${n}`, lazy: "strict", anchors: "deep-" });
  main.append(deep(0));
  env.parts.initParts();
  await env.start();
  for (let n = 0; n < 5; n++) {
    if (!htmx.open().includes(`deep${n}`)) break;
    htmx.respond(`deep${n}`, { nodes: () => [loaded(`deep${n}`, deep(n + 1))] });
    await clock.tick(50);
  }
  out.deep = { sent: htmx.sent(), scrolls: doc.scrolls.length };
}
"""
    )
    for key, target in (("panel", "fmm-partner"), ("title", "fmm-partner-title")):
        result = data[key]
        # once the figures are in, the panel is asked for before the normal part beside it
        assert result["between"] == {"sent": ["datamart", "fmm"], "scrolls": []}, key
        assert result["sent"] == ["datamart", "fmm", "activityinfo"], key
        assert result["scrolls"] == [f"{target} start instant"] and result["marked"] is True, key
    assert data["empty"] == {"sent": ["datamart", "fmm", "activityinfo"], "scrolls": []}
    assert data["wheel"] == []
    assert data["deep"] == {"sent": ["deep0", "deep1", "deep2", "deep3"], "scrolls": 0}


@needs_node
def test_back_to_a_page_whose_parts_load_again_goes_to_where_it_was_once_the_parts_above_are_in():
    data = run_dom(
        """
// A page drawn again on Back (its stored figures gone): its entry kept the offset 5000; the screen is 800 high.
const build = async (options = {}) => {
  const env = await page({ parallel: 1, navigation: "back_forward", scrollRestoration: "manual", state: { ndPartsOffset: 5000 }, ...options });
  const at = (el, top) => { el.rect.top = top; return el; };
  env.main.append(
    at(env.placeholder({ name: "kpis", priority: "high" }), 100),
    at(env.placeholder({ name: "news", priority: "low" }), 300), // above the screen: asked for before review
    at(env.placeholder({ name: "review" }), 9000), // below the screen
  );
  env.parts.initParts();
  await env.start();
  return env;
};
const tops = (doc) => doc.windowScrolls.map((s) => `${s.top} ${s.behavior}`);
{
  const { doc, win, htmx, clock, loaded } = await build();
  out.atOnce = tops(doc);
  win.scrollY = 4700; // the swap moved the page (the document was shorter, or scroll anchoring)
  htmx.respond("kpis", { nodes: () => [loaded("kpis")] });
  out.afterKpis = tops(doc);
  await clock.tick(100);
  out.sent = htmx.sent();
  win.scrollY = 5100;
  htmx.respond("news", { nodes: () => [loaded("news")] });
  await clock.tick(100);
  out.afterNews = tops(doc);
  win.scrollY = 4000; // every part above the bottom of the screen is in: the page is the person's again
  htmx.respond("review", { nodes: () => [loaded("review")] });
  await clock.tick(100);
  out.afterReview = tops(doc).length;
}
{
  // the person scrolls while the parts come back: the page is not put back afterwards
  const { doc, win, htmx, clock, loaded } = await build();
  win.dispatchEvent(new dom.Event("wheel"));
  win.scrollY = 4200;
  htmx.respond("kpis", { nodes: () => [loaded("kpis")] });
  await clock.tick(100);
  out.wheel = tops(doc);
}
{
  // a new visit: the page keeps its entry's offset from now on, when the scrolling stops and when a link is followed
  const { doc, win, clock, main, placeholder, start, parts, htmx } = await page();
  main.append(placeholder({ name: "kpis", priority: "high" }), doc.h("a", { id: "away", href: "/help/" }, "Help"));
  parts.initParts();
  await start();
  out.fresh = { mode: win.history.scrollRestoration, scrolls: tops(doc) };
  win.scrollY = 1234;
  win.dispatchEvent(new dom.Event("scroll"));
  await clock.tick(100);
  out.whileScrolling = win.history.state;
  await clock.tick(200);
  out.stopped = win.history.state;
  win.scrollY = 1500;
  doc.getElementById("away").click();
  out.followed = win.history.state;
  // Back or Forward within the page: put back from the entry's state
  win.dispatchEvent(Object.assign(new dom.Event("popstate"), { state: { ndPartsOffset: 640 } }));
  out.popstate = tops(doc);
  // htmx is about to push an address: the entry stays the page's (review R1-browser-1)
  htmx.fire(doc.body, "htmx:beforeHistorySave");
  out.kept = win.history.scrollRestoration;
}
{
  // an entry the page never took over (a visit before this one): the browser puts it back, as before
  const { doc, win } = await build({ scrollRestoration: "auto" });
  out.notOurs = { mode: win.history.scrollRestoration, scrolls: tops(doc) };
}
{
  // a reload of a page without parts whose entry the page kept: it goes back there too
  const { doc, start, parts } = await page({ navigation: "reload", scrollRestoration: "manual", state: { ndPartsOffset: 777 } });
  parts.initParts();
  await start();
  out.noParts = tops(doc);
}
"""
    )
    assert data["atOnce"] == ["5000 instant"]  # never smooth: Bootstrap's scroll-behavior would animate it
    assert data["sent"] == ["kpis", "news"]  # what is above the bottom of the screen first
    # put back at once, before the swap is painted
    assert data["afterKpis"] == ["5000 instant", "5000 instant"]
    assert data["afterNews"] == ["5000 instant", "5000 instant", "5000 instant"]
    assert data["afterReview"] == 3
    assert data["wheel"] == ["5000 instant"]
    assert data["fresh"] == {"mode": "manual", "scrolls": []}
    assert data["whileScrolling"] is None and data["stopped"] == {"ndPartsOffset": 1234}
    assert data["followed"] == {"ndPartsOffset": 1500}
    assert data["popstate"] == ["640 instant"]
    assert data["kept"] == "manual"
    assert data["notOurs"] == {"mode": "auto", "scrolls": []}
    assert data["noParts"] == ["777 instant"]


@needs_node
def test_back_to_an_entry_htmx_pushed_goes_to_where_it_was():
    """The entries htmx pushes (a tab, a view, a filter, a page of a part) went back to the browser: htmx writes
    {htmx: true} over the entry's state before it pushes, so the page's offset was lost, and back on such an entry
    the browser put the page at its old offset at once, over the frame and its placeholders (By document reserves
    one document of ten): clamped to the frame's height and never put back, the editor landed 37,000 px higher, on
    another document (review R1-browser-1). They are the page's now: a pushed entry keeps the mode of the entry it
    comes from, its offset is kept beside htmx's state (htmx's own write keeps it), and back on it the page goes
    there once its parts above are in; Back within the page, which htmx answers by reloading the entry reached,
    keeps that entry's offset, never the one of the entry left. Parts brought by a swap into a page that had none
    make its entry the page's too."""
    data = run_dom(
        """
const tops = (doc) => doc.windowScrolls.map((s) => `${s.top} ${s.behavior}`);
const scrollTo = async (win, clock, y) => { win.scrollY = y; win.dispatchEvent(new dom.Event("scroll")); await clock.tick(300); };
{
  const { doc, win, htmx, clock, main, placeholder, start, parts } = await page();
  main.append(placeholder({ name: "findings", priority: "high" }));
  parts.initParts();
  await start();
  await scrollTo(win, clock, 1234);
  // htmx pushes page 2 of the part: its event, its state over the entry, then the new entry
  htmx.fire(doc.body, "htmx:beforeHistorySave", { path: "/page?year=2025" });
  win.history.replaceState({ htmx: true }, "", win.location.href);
  out.left = { ...win.history.state };
  win.history.pushState({ htmx: true }, "", "/page?year=2025&page=2");
  win.location.search = "?year=2025&page=2";
  out.pushedMode = win.history.scrollRestoration;
  await scrollTo(win, clock, 44019);
  out.pushed = { ...win.history.state };
  // Back within the page: the entry reached, then htmx (its event names the page left, its state over the entry
  // reached) reloads it
  win.history.state = { htmx: true, ndPartsOffset: 1234 };
  win.location.search = "?year=2025";
  win.dispatchEvent(Object.assign(new dom.Event("popstate"), { state: { htmx: true, ndPartsOffset: 1234 } }));
  htmx.fire(doc.body, "htmx:beforeHistorySave", { path: "/page?year=2025&page=2" });
  win.history.replaceState({ htmx: true }, "", win.location.href);
  await clock.tick(300);
  out.reached = { ...win.history.state };
}
{
  // back on the pushed entry, drawn again (Back from another page): the page goes to where it was
  const { doc, win, htmx, clock, main, placeholder, loaded, start, parts } = await page({
    navigation: "back_forward", scrollRestoration: "manual", state: { htmx: true, ndPartsOffset: 44019 },
  });
  const p = placeholder({ name: "findings", priority: "high" });
  p.rect.top = 100;
  main.append(p);
  parts.initParts();
  await start();
  win.scrollY = 7327; // the frame was shorter than the offset
  htmx.respond("findings", { nodes: () => [loaded("findings")] });
  await clock.tick(100);
  out.back = tops(doc);
}
{
  // a page without parts (the review's dashboard tab) gets some from a swap (the By document tab)
  const { doc, win, htmx, clock, main, placeholder, start, parts } = await page();
  parts.initParts();
  await start();
  out.before = win.history.scrollRestoration;
  const p = placeholder({ name: "findings", priority: "high" });
  main.append(p);
  htmx.fire(p, "htmx:load");
  await clock.flush();
  out.after = win.history.scrollRestoration;
}
{
  // a tab change whose part comes drawn in at once (the themes): drawn again on Back, it is a placeholder
  const { doc, win, htmx, clock, main, loaded, start, parts } = await page();
  parts.initParts();
  await start();
  const area = doc.h("div", { id: "review-results" }, loaded("synthesis", doc.h("p", {}, "Themes")));
  main.append(area);
  htmx.fire(area, "htmx:load");
  await clock.flush();
  out.inline = win.history.scrollRestoration;
}
{
  // a new visit of a page whose parts were all drawn with it (stored): its entry is the page's too
  const { doc, win, main, loaded, start, parts } = await page();
  main.append(loaded("kpis", doc.h("p", {}, "Tiles")));
  parts.initParts();
  await start();
  out.drawnWith = win.history.scrollRestoration;
}
"""
    )
    assert data["inline"] == "manual" and data["drawnWith"] == "manual"
    assert data["left"] == {"htmx": True, "ndPartsOffset": 1234}
    assert data["pushedMode"] == "manual" and data["pushed"] == {"htmx": True, "ndPartsOffset": 44019}
    assert data["reached"] == {"htmx": True, "ndPartsOffset": 1234}
    assert data["back"] == ["44019 instant", "44019 instant"]
    assert data["before"] == "auto" and data["after"] == "manual"


@needs_node
def test_back_within_the_page_keeps_the_entry_reached_while_its_reload_is_slow():
    """Back within the page to an entry htmx pushed: htmx reloads it, and the page left lives on until the new one
    comes (a request and its frame: often more than the 200 ms after which an offset is saved). parts.js sent the
    page left to the entry's offset, deeper than that page could go (By document: page 10 kept at 75,676 px, page 11
    69,217 px tall); the browser clamped it, its scroll event came a frame later, and 200 ms after that the clamped
    offset was saved over the entry's own: the page reloaded landed 7,227 px higher, on another document (review
    R2-fixes-1). The page left is no longer moved, and nothing it does until it goes (a scroll, a link, htmx's history
    event, whichever of htmx's popstate handler and the page's runs first) is written over the offset the entry
    reached keeps; reloaded, the page goes there."""
    data = run_dom(
        """
const tops = (doc) => doc.windowScrolls.map((s) => `${s.top} ${s.behavior}`);
// By document page 11, reached from page 10 by an htmx push: its deepest offset 68449, scrolled to 10000; the
// browser clamps window.scrollTo and sends its scroll event a frame later
const page11 = () => page({
  href: "http://localhost/page?year=2025&page=11",
  scrollRestoration: "manual",
  state: { htmx: true, ndPartsOffset: 10000 },
  scrollMax: 68449,
  scrollEvents: "frame",
});
const popstate = (win) => win.dispatchEvent(Object.assign(new dom.Event("popstate"), { state: { htmx: true, ndPartsOffset: 75676 } }));
// htmx's popstate handler: its event names the page left (the address is already the entry reached), its state over
// the entry reached, then location.reload()
const htmxReloads = (doc, win, htmx) => {
  htmx.fire(doc.body, "htmx:beforeHistorySave", { path: "/page?year=2025&page=11" });
  win.history.replaceState({ htmx: true }, "", win.location.href);
};
for (const order of ["page first", "htmx first"]) {
  const { doc, win, htmx, clock, main, placeholder, loaded, start, parts } = await page11();
  main.append(
    placeholder({ name: "findings", priority: "high", anchors: "doc-" }),
    doc.h("a", { id: "to-doc", href: "#doc-5" }, "Document 5"),
    doc.h("a", { id: "next", href: "/page?year=2025&page=12" }, "12"),
  );
  parts.initParts();
  await start();
  win.scrollY = 10000;
  doc.getElementById("to-doc").click(); // a link into the part still loading: it waits for it
  win.dispatchEvent(new dom.Event("scroll"));
  await clock.tick(100); // the save of this offset is still to come when Back is pressed
  const before = doc.windowScrolls.length;
  const intoView = doc.scrolls.length;
  // Back: the entry reached, as the browser has it, then both popstate handlers
  win.history.state = { htmx: true, ndPartsOffset: 75676 };
  win.location.search = "?year=2025&page=10";
  win.location.href = "http://localhost/page?year=2025&page=10";
  if (order === "page first") {
    popstate(win); // app.js registers it before htmx sets window.onpopstate (on DOMContentLoaded)
    htmxReloads(doc, win, htmx);
  } else {
    htmxReloads(doc, win, htmx);
    popstate(win);
  }
  // the server answers late; meanwhile the part the link waited for arrives, the page left scrolls (the wheel, scroll
  // anchoring) and a link is followed
  await clock.tick(50);
  htmx.respond("findings", { nodes: () => [loaded("findings", doc.h("div", { id: "doc-5" }, "Document 5"))] });
  await clock.tick(100);
  win.scrollY = 60000;
  win.dispatchEvent(new dom.Event("scroll"));
  await clock.tick(300);
  doc.getElementById("next").click();
  await clock.tick(300);
  out[order] = { state: { ...win.history.state }, moved: [...tops(doc).slice(before), ...doc.scrolls.slice(intoView).map((s) => s.id)] };
}
{
  // the page reloaded on that entry goes to its offset
  const { doc, main, placeholder, start, parts } = await page({
    href: "http://localhost/page?year=2025&page=10",
    navigation: "reload",
    scrollRestoration: "manual",
    state: out["page first"].state,
  });
  main.append(placeholder({ name: "findings", priority: "high" }));
  parts.initParts();
  await start();
  out.reloaded = tops(doc);
}
"""
    )
    for order in ("page first", "htmx first"):
        assert data[order] == {"state": {"htmx": True, "ndPartsOffset": 75676}, "moved": []}, order
    assert data["reloaded"] == ["75676 instant"]


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


# Copy, CSV (data-table-copy / data-table-csv) and Copy summary (data-copy-target) look for the element they name when
# they are pressed (app.js initTables) and do nothing when it is not there. Up to round 2 of step 2 data health drew
# the datasets panel's Copy and CSV in its frame and the table in a part: they did nothing while the table loaded,
# nor ever when its part failed. So a button that copies an element comes in the same answer as that element.
TOOLBAR = re.compile(r'\{%\s*include\s+"components/table_toolbar\.html"\s+with\s+([^%]*)%\}')
INCLUDE = re.compile(r'\{%\s*include\s+"([^"]+)"')
COPY_TARGET = re.compile(r'\sdata-(?:table|copy)-target="#([^"]+)"')


def _with_includes(source: str, seen: frozenset[str] = frozenset()) -> str:
    """``source`` and every template it includes by name, all the way down: what one answer draws (the parts it
    places come by themselves; the table toolbar is read from its include by :func:`_copied_elsewhere`)."""
    from django.template.loader import get_template

    for name in INCLUDE.findall(source):
        if name in seen or name == "components/table_toolbar.html":
            continue
        included = Path(get_template(name).origin.name).read_text()
        source += "\n" + _with_includes(included, seen | {name})
    return source


def _copied_elsewhere(source: str) -> list[str]:
    """The elements that a Copy or CSV button of this template names and that the same answer does not draw."""
    drawn = _with_includes(source)
    targets = set(COPY_TARGET.findall(drawn))
    for options in TOOLBAR.findall(drawn):
        if "no_copy=True" not in options:
            targets.update(re.findall(r'\btable_id="([^"]+)"', options))
    return sorted(target for target in targets if not re.search(rf'\sid="{re.escape(target)}"', drawn))


def test_copy_and_csv_come_in_the_same_answer_as_what_they_copy():
    frame = (
        '{% include "components/table_toolbar.html" with title="T" table_id="t" %}{% part "t" kind="table" %}'
    )
    assert _copied_elsewhere(frame) == ["t"]  # a frame's toolbar over a table its part draws
    assert _copied_elsewhere(frame.replace('table_id="t"', 'table_id="t" no_copy=True')) == []
    assert _copied_elsewhere(frame + '<table id="t">') == []
    assert _copied_elsewhere('<button data-copy-target="#u">{% part "u" %}') == ["u"]
    root = Path(settings.BASE_DIR) / "neurodb"
    # every template that places a part: the frames, and the parts that bring parts of their own
    placing = {
        path.as_posix().split("/templates/", 1)[1]: path.read_text()
        for path in root.rglob("*.html")
        if "/templates/" in path.as_posix()
        and "/templates/components/" not in path.as_posix()
        and re.search(r'\{%\s*part\s+"', path.read_text())
    }
    pages = {
        "reports/overview.html",
        "reports/brief.html",
        "reports/partner_profile.html",
        "reports/data_health.html",
    }
    assert pages | {"reports/parts/partner_datamart.html"} <= set(placing)
    found = {name: _copied_elsewhere(source) for name, source in placing.items()}
    assert {name: targets for name, targets in found.items() if targets} == {}


def test_filter_bars_abort_on_their_form_never_on_body_and_modules_start_independently():
    app = _app()
    bar = app.split("function initFilterBar(form)", 1)[1].split("\n}\n", 1)[0]
    assert 'window.htmx.trigger(form, "htmx:abort")' in bar
    assert "source: form" in bar
    assert "document.body" not in bar
    # htmx adds a form's fields to a request issued on it: the filter bar's own address must stay the query
    assert "e.detail.parameters = {};" in bar
    # the new address keeps the offset Back returns to (parts.js), and nothing of htmx's
    assert 'window.history.replaceState(offsetState(), "", url)' in bar
    modules = app.split("function initModules(root)", 1)[1].split("\n}\n", 1)[0]
    assert "Promise.allSettled(elements.map(startModule))" in modules
    assert "return modules;" in app.split("function enhance(", 1)[1].split("\n}\n", 1)[0]
    assert "trackModules(enhance(e.detail.elt))" in app and "trackModules(enhance(document))" in app
    autoprint = app.split("async function printWhenReady", 1)[1].split("\n}\n", 1)[0]
    assert "pendingParts()" in autoprint and "await loadAll({ timeout: 60000 })" in autoprint


FILTER_BAR = r"""
const source = process.argv[1];
const classes = () => {
  const set = new Set();
  return { toggle: (name, on) => (on ? set.add(name) : set.delete(name)), contains: (name) => set.has(name) };
};
const listeners = (thing) => {
  thing.on = {};
  thing.addEventListener = (type, fn) => (thing.on[type] ||= []).push(fn);
  thing.fire = (type, event = {}) => (thing.on[type] || []).forEach((fn) => fn({ target: thing, ...event }));
  return thing;
};
const make = ({ results = true } = {}) => {
  const log = { requests: [], aborts: 0, assigned: [], replaced: [] };
  const button = { classList: classes(), attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
  const area = { classList: classes() };
  const form = listeners({ dataset: { hxTarget: "#results" }, fields: [["year", "2026"]], getAttribute: () => "/funds/" });
  const win = listeners({
    location: { pathname: "/funds/", assign: (url) => log.assigned.push(url) },
    history: { replaceState: (state, title, url) => log.replaced.push(url) },
    htmx: {
      trigger: (elt, name) => {
        if (name !== "htmx:abort") return;
        log.aborts += 1;
        const open = log.requests.filter((r) => !r.done);
        open.forEach((r) => { r.done = true; form.fire("htmx:afterRequest", { detail: { xhr: { status: 0 } } }); r.reject(new Error("abort")); });
      },
      ajax: (verb, url, options) => new Promise((resolve, reject) => {
        const request = { url, options, reject, done: false };
        // the answer, as htmx 2.0.10 gives it: htmx:afterRequest on the source, then the promise settles
        request.answer = (status) => { request.done = true; form.fire("htmx:afterRequest", { detail: { xhr: { status } } }); resolve(); };
        request.fail = () => { request.done = true; form.fire("htmx:afterRequest", { detail: { xhr: { status: 0 } } }); reject(new Error("timeout")); };
        log.requests.push(request);
      }),
    },
  });
  const doc = { querySelector: (sel) => (results && sel === "#results" ? area : null) };
  const $ = (sel) => (sel === 'button[type="submit"]' ? button : null);
  const $$ = () => [];
  const debounce = (fn) => fn; // at once: the test is about what is sent, not when
  class FormData { constructor(f) { return f.fields; } }
  const body = source.split("function initFilterBar(form)", 2)[1].split("\n}\n", 1)[0];
  const init = new Function("$", "$$", "debounce", "offsetState", "window", "document", "FormData",
    `return function initFilterBar(form)${body}\n}`)($, $$, debounce, () => ({}), win, doc, FormData);
  init(form);
  const state = () => ({
    busy: button.classList.contains("is-busy"),
    ariaBusy: button.attrs["aria-busy"] || null,
    dimmed: area.classList.contains("is-updating"),
    requests: log.requests.map((r) => r.url),
    aborts: log.aborts,
    assigned: [...log.assigned],
    replaced: [...log.replaced],
  });
  const apply = () => form.fire("submit", { preventDefault() {} });
  return { form, win, log, state, apply };
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const out = {};

// Apply pressed three times while the answer is slow: one request, the button busy, the results dimmed
let bar = make();
bar.apply(); bar.apply(); bar.apply();
out.slow = bar.state();
bar.log.requests[0].answer(200);
await tick();
out.slowDone = bar.state();
bar.apply(); // the same filter, asked again on purpose: sent again
out.again = bar.state().requests.length;
bar.log.requests[1].answer(200);
await tick();
bar.form.fire("change", { target: {} }); // a change that comes back to the filter shown: nothing sent
out.unchanged = bar.state().requests.length;

// No answer (a network error, a 502, a timeout): the page itself is loaded with the filter
for (const [name, how] of [["network", (r) => r.answer(0)], ["502", (r) => r.answer(502)], ["timeout", (r) => r.fail()]]) {
  bar = make();
  bar.apply();
  how(bar.log.requests[0]);
  await tick();
  out[name] = bar.state();
}

// A 4xx is the page's own answer (it says what is wrong): shown, not loaded again nor kept in the address
bar = make();
bar.apply();
bar.log.requests[0].answer(403);
await tick();
out.forbidden = bar.state();

// A newer filter while one runs: the old one is aborted and never loads the page; the newer one settles
bar = make();
bar.apply();
bar.form.fields = [["year", "2025"]];
bar.apply();
await tick();
out.newer = bar.state();
bar.log.requests[1].answer(200);
await tick();
out.newerDone = bar.state();

// Values picked while a list of several stays open: one request once it closes
bar = make();
bar.form.fields = [["donor", "A"]];
bar.form.fire("change", { target: { multiple: true, tomselect: { isOpen: true } } });
bar.form.fields = [["donor", "A"], ["donor", "B"]];
bar.form.fire("change", { target: { multiple: true, tomselect: { isOpen: true } } });
out.listOpen = bar.state().requests.length;
bar.form.fire("nd:listclose");
out.listClosed = bar.state().requests;
bar.form.fire("nd:listclose"); // closed again with nothing picked: nothing sent
out.closedAgain = bar.state().requests.length;

// A request the browser cuts short because the page is being left: never loaded as a page
bar = make();
bar.apply();
bar.win.fire("pagehide");
bar.log.requests[0].fail();
await tick();
out.leaving = bar.state();

// Without its results area, the filter loads the page
bar = make({ results: false });
bar.apply();
out.noResults = bar.state();
console.log(JSON.stringify(out));
"""


@needs_node
def test_apply_sends_one_request_says_it_is_working_and_loads_the_page_when_no_answer_comes():
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", FILTER_BAR, _app()], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    url = "/funds/?year=2026"
    assert out["slow"] == {
        "busy": True,
        "ariaBusy": "true",
        "dimmed": True,
        "requests": [url],
        "aborts": 1,
        "assigned": [],
        "replaced": [],
    }
    assert out["slowDone"]["busy"] is False and out["slowDone"]["ariaBusy"] is None
    assert out["slowDone"]["dimmed"] is False and out["slowDone"]["replaced"] == [url]
    assert out["again"] == 2 and out["unchanged"] == 2
    for name in ("network", "502", "timeout"):
        assert out[name]["assigned"] == [url] and out[name]["replaced"] == [], name
        assert out[name]["busy"] is False, name
    assert out["forbidden"]["assigned"] == [] and out["forbidden"]["replaced"] == []
    assert out["newer"]["requests"] == [url, "/funds/?year=2025"] and out["newer"]["aborts"] == 2
    assert out["newer"]["assigned"] == [] and out["newer"]["busy"] is True
    assert out["newerDone"]["busy"] is False and out["newerDone"]["replaced"] == ["/funds/?year=2025"]
    assert out["newerDone"]["assigned"] == []
    assert out["listOpen"] == 0 and out["listClosed"] == ["/funds/?donor=A&donor=B"]
    assert out["closedAgain"] == 1
    assert out["leaving"]["assigned"] == [] and out["leaving"]["busy"] is False
    assert out["noResults"]["assigned"] == [url] and out["noResults"]["requests"] == []


def test_a_list_of_several_values_says_when_it_closes():
    selects = _app().split("new window.TomSelect(el, {", 1)[1].split("});", 1)[0]
    assert 'onDropdownClose: () => el.dispatchEvent(new Event("nd:listclose", { bubbles: true }))' in selects
    css = (STATIC / "css" / "app.css").read_text()
    assert ".is-updating {" in css and ".btn.is-busy::after {" in css
    assert "@keyframes nd-spin" in css


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


RELATIVE_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["'](\.{1,2}/[^"']+)["']""")


def test_a_page_loads_every_module_of_its_release_under_its_hashed_name(client_viewer, tmp_path):
    """The project's modules import one another by relative path (app.js: from "./parts.js"), which collectstatic
    leaves as it is: the browser fetched parts.js and lib.js under their plain names, which WhiteNoise lets it keep
    for a minute, so for that minute around a release a new page ran with the old parts.js, which never marks the
    page "parts-ready": base.html's fallback took its placeholders for dead and hid their Try again (review
    R2-critic-1). The page's import map sends every plain name a module of this release imports to the hashed file
    collectstatic made: walking the modules from the page's own and those app.js loads on demand, the browser never
    asks for a module under its plain name. The map comes before any module, under the page's script nonce."""
    from django.core.management import call_command
    from django.test import override_settings

    # without hashed names (development, tests) the names are the same: no map
    assert 'type="importmap"' not in client_viewer.get(reverse("reports:library")).content.decode()

    root = tmp_path / "static"
    manifest_storage = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
    }
    with override_settings(STATIC_ROOT=root, STORAGES=manifest_storage):
        call_command("collectstatic", interactive=False, verbosity=0)
    hashed = json.loads((root / "staticfiles.json").read_text())["paths"]
    with override_settings(STATIC_ROOT=root, STORAGES=manifest_storage):
        response = client_viewer.get(reverse("reports:library"))
    html = response.content.decode()

    found = re.search(r'<script type="importmap" nonce="([^"]+)">(.*?)</script>', html, re.S)
    assert found, "no import map on the page"
    assert f"'nonce-{found.group(1)}'" in response["Content-Security-Policy"]
    assert found.start() < html.index('<script type="module"')
    imports = json.loads(found.group(2))["imports"]
    static_url = settings.STATIC_URL

    def file_of(url: str) -> Path:
        assert url.startswith(static_url), url
        return root / url[len(static_url) :]

    pages = re.findall(r'<script type="module" src="([^"]+)"', html)
    assets = json.loads(
        re.search(r'<script id="asset-urls" type="application/json">(.*?)</script>', html, re.S)[1]
    )
    on_demand = [url for url in assets.values() if url.startswith(f"{static_url}js/")]
    assert pages and on_demand
    loaded, todo = set(), [*pages, *on_demand]
    while todo:
        url = todo.pop()
        if url in loaded:
            continue
        loaded.add(url)
        name = url[len(static_url) :]
        assert name in hashed.values(), f"{url} is not under a hashed name"
        for spec in RELATIVE_IMPORT.findall(file_of(url).read_text()):
            plain = urljoin(url, spec)
            assert plain in imports, f"{name} imports {spec}: the browser would ask for {plain}"
            target = imports[plain]
            assert target == static_url + hashed[plain[len(static_url) :]] and file_of(target).is_file()
            todo.append(target)
    for module in ("parts", "lib", "charts"):
        assert static_url + hashed[f"js/{module}.js"] in loaded

    # the pages that load a module without base.html (so without its map) load one that imports nothing by its name
    for template in Path(settings.BASE_DIR, "neurodb").glob("**/templates/**/*.html"):
        source = template.read_text()
        if template == TEMPLATES / "base.html" or '{% extends "base.html" %}' in source:
            continue
        for module in re.findall(r"""<script type="module" src="\{% static '([^']+)' %\}""", source):
            assert not RELATIVE_IMPORT.findall((STATIC / module).read_text()), (template.name, module)


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


def _code(js: str) -> str:
    """A script without its comments: what runs, not what it says."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(^|\s)//.*$", r"\1", js, flags=re.M)


def test_parts_js_listens_where_its_event_order_says():
    code = _code((STATIC / "js" / "parts.js").read_text())
    for event in ("htmx:load", "htmx:beforeSwap", "htmx:beforeCleanupElement"):
        assert f'document.addEventListener("{event}"' in code, event
    assert "DOMContentLoaded" not in code  # placeholders are discovered on htmx:load only
    # the server's "still computing" (202) is turned away in htmx:beforeSwap, where a part is also marked swapping
    before_swap = code.split('document.addEventListener("htmx:beforeSwap"', 1)[1].split("\n  });", 1)[0]
    assert (
        'getResponseHeader("X-NeuroDB-Part-Pending")' in before_swap
        and "d.shouldSwap = false;" in before_swap
    )
    assert "queue.swapping(rec.id);" in before_swap
    # a cleanup never asks for the next parts at once: the rest of that swap is still attached
    drop = code.split("function drop(rec) {", 1)[1].split("\n  }\n", 1)[0]
    assert "pumpSoon();" in drop and "pump();" not in drop


def _media_widths(query: str, widths) -> list[int]:
    """The window widths at which a media query list of min-width / max-width conditions holds."""
    held = []
    for width in widths:
        for alternative in query.split(","):
            limits = re.findall(r"\((min|max)-width: ([\d.]+)px\)", alternative)
            if all(width >= float(v) if k == "min" else width <= float(v) for k, v in limits):
                held.append(width)
                break
    return held


# The overview on the x20 parity world in Chromium, cold: (window width, room the scroll bar takes, width of the
# placeholders, columns of the loaded figures' .grid--2). Hidden scroll bars stand for a phone, a tablet or a Mac,
# 15 px for Windows. Where the grids stack moves with the scroll bar and the sidebar (from 992 px), so no window
# width can say it: 905 px is two columns without a scroll bar and one with it, 1199 px the same.
GRID_SWITCH = [
    (768, 15, 714.6, 1),
    (900, 0, 855.0, 1),
    (902, 0, 856.9, 2),
    (905, 0, 859.8, 2),
    (905, 15, 844.8, 1),
    (919, 0, 873.1, 2),
    (919, 15, 858.1, 2),
    (991, 0, 941.5, 2),
    (992, 0, 670.4, 1),
    (1185, 0, 853.8, 1),
    (1190, 0, 858.5, 2),
    (1199, 0, 867.1, 2),
    (1199, 15, 852.1, 1),
    (1200, 15, 853.0, 1),
    (1204, 15, 856.8, 2),
    (1366, 0, 1030.0, 2),
]


def test_placeholders_keep_a_middle_height_where_their_own_grids_have_stacked():
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    # no window width decides the middle height (it did up to round 3: 919.98 / 1199.98 px, wrong by about 1,400 px
    # of figures in a two-column layout at 902-919 and 1188-1199 px without a scroll bar)
    assert not re.search(r"@media[^{]*\{\s*\.part \{ min-height: var\(--part-h-md", block)
    # it is a float beside the placeholder's content, as tall as height_md where the placeholder is narrower than
    # .grid--2 needs for two columns: percentage padding is of the placeholder's width
    rule = re.search(
        r"@media \(min-width: 768px\) \{\n"
        r"  \[data-part\]:not\(\.is-loaded\) \{ display: flow-root; \}\n"
        r'  \[data-part\]:not\(\.is-loaded\)::before \{ content: ""; float: left; padding-top: '
        r"clamp\(0px, \((\d+)px \+ ([\d.]+)rem - 100%\) \* (\d+), var\(--part-h-md, 0px\)\); \}\n\}",
        block,
    )
    assert rule, "the middle height's float"
    px, rem, factor = float(rule.group(1)), float(rule.group(2)), int(rule.group(3))
    # its width is the one .grid--2 needs for two columns: two of its minimum and one gap of .grid
    column = re.search(
        r"\.grid--2 \{ grid-template-columns: repeat\(auto-fit, minmax\(min\(100%, (\d+)px\), 1fr\)\); \}",
        css,
    )
    gap = re.search(r"\.grid \{ display: grid; gap: ([\d.]+)rem; \}", css)
    assert (px, rem) == (2 * float(column.group(1)), float(gap.group(1)))

    def middle(width, md=6200):
        """The float's height in a placeholder of this width, as the clamp works it out."""
        return min(max(0.0, (px + rem * 16 - width) * factor), md)

    seen = {(window, bar): (middle(width), cols) for window, bar, width, cols in GRID_SWITCH}
    assert seen == {key: (6200 if cols == 1 else 0, cols) for key, (_, cols) in seen.items()}
    # the KPI tiles size themselves (below): no float there, nor on an answer, nor on paper
    assert '#part-kpis[data-part-group="overview"]:not(.is-loaded)::before { content: none; }' in block
    assert "[data-part]:not(.is-loaded)::before { content: none; }" in block.split("@media print {", 1)[1]
    # off screen, a low-priority placeholder skips only its grey shapes: a skipped box would leave the float out
    low = '.part[data-part-priority="low"]:not(.is-loaded)'
    skipped = "content-visibility: auto; contain-intrinsic-size: auto 0px;"
    assert f"{low} > .part__skeleton {{ {skipped} }}" in block
    assert f"{low} {{ content-visibility" not in block
    # phones: height_sm, else height_md; the float takes over where the phone height stops, never both
    assert ".part { min-height: var(--part-h-sm, var(--part-h-md, var(--part-h, 8rem))); }" in block
    phone = re.search(r"@media (\([^)]*\)) \{\n  \.part--charts", block).group(1)
    widths = range(700, 801)
    assert sorted(_media_widths("(min-width: 768px)", widths) + _media_widths(phone, widths)) == list(widths)
    # the For you card is a part now: its old loading slot has no rule left
    assert "watch-overview-slot" not in css


# The overview's KPI tiles on the x20 parity world (scripts/parity), the larger of an administrator's and a viewer's
# page: (window width, width of the tiles' grid, height of the tiles). The tiles wrap by the grid's width (five,
# four or three to a row, two on a phone) and a tile is taller the narrower it is.
KPI_TILES = [
    (390, 358, 851),
    (768, 730, 549),
    (940, 893, 483),
    (960, 912, 345),
    (980, 931, 328),
    (1024, 701, 523),
    (1100, 773, 500),
    (1200, 868, 483),
    (1240, 906, 364),
    (1280, 944, 328),
    (1366, 1030, 295),
    (1440, 1104, 295),
    (1536, 1200, 261),
    (1600, 1264, 261),
    (1680, 1344, 244),
    (1920, 1584, 208),
    (2560, 2224, 175),
]


def test_the_overview_tiles_placeholder_follows_the_tiles_grid_at_every_width():
    css = (STATIC / "css" / "app.css").read_text()
    kpis = '#part-kpis[data-part-group="overview"]'
    assert f"{kpis}:not(.is-loaded) {{ min-height: 0; }}" in css  # no fixed height by window width
    assert f"{kpis} .skeleton__block--tile {{ height: auto; container-type: inline-size; }}" in css
    tile_rule = re.escape(kpis) + r" \.skeleton__block--tile"
    tiles = {
        int(n): {k: float(v) for k, v in re.findall(r"--(w\d+): ([\d.]+)px", body)}
        for n, body in re.findall(tile_rule + r":nth-child\((\d)\) \{([^}]*)\}", css)
    }
    assert sorted(tiles) == [1, 4, 5]
    default = re.search(
        tile_rule + r'::before \{ content: ""; display: block; height: var\(--(w\d+), ([\d.]+)px\); \}', css
    )
    fallback = float(default.group(2))
    bands = [
        (re.findall(r"\((min|max)-width: ([\d.]+)px\)", condition), var)
        for condition, var in re.findall(
            r"@container ([^{]+) \{ " + tile_rule + r"::before \{ height: var\(--(w\d+), [\d.]+px\); \} \}",
            css,
        )
    ]
    assert len(bands) >= 6

    def tile(index, width):
        """The height of a skeleton tile of this width: the last @container rule that holds wins."""
        var = default.group(1)
        for limits, name in bands:
            if all(width >= float(v) if k == "min" else width <= float(v) for k, v in limits):
                var = name
        return tiles.get(index, {}).get(var, fallback)

    def grid(width):
        """The skeleton's grid: as .kpi-grid places five tiles (auto-fit, minmax(170px), a 12 px gap)."""
        cols = max(1, min(5, (width + 12) // 182))
        tile_width = (width - 12 * (cols - 1)) / cols
        heights = [tile(i, tile_width) for i in range(1, 6)]
        rows = [max(heights[i : i + cols]) for i in range(0, 5, cols)]
        return sum(rows) + 12 * (len(rows) - 1)

    misses = {window: grid(width) - real for window, width, real in KPI_TILES if abs(grid(width) - real) > 40}
    assert misses == {}


def test_the_overview_tiles_placeholder_keeps_its_room_when_slow_and_only_its_error_box_when_failed():
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    kpis = '#part-kpis[data-part-group="overview"]'
    # its room is the grey tiles' (no min-height): the slow line and the error box share their grid cell instead
    # of adding to it (up to round 3: +28 px after 8 s, and down to the 97 px error box on a failure)
    assert f"{kpis}:not(.is-loaded) {{ display: grid; }}" in block
    assert f"{kpis}:not(.is-loaded) > :not(noscript) {{ grid-area: 1 / 1; min-width: 0; }}" in block
    slow = re.search(re.escape(kpis) + r" \.part__slow \{([^}]*)\}", block).group(1)
    for declaration in ("align-self: end;", "position: relative;", "background: var(--nd-bg);"):
        assert declaration in slow, declaration
    assert re.search(re.escape(kpis) + r" \.part__error \{ align-self: start; \}", block)
    # on a failure the tiles go, as every placeholder's grey shapes do: the error box keeps no more room than it
    # takes. They stayed laid out, unseen, behind it: on a phone the five tiles stack to 868 px, which left 771 px of
    # blank page under the 97 px error box, the other parts' error boxes below the first screen (review
    # R1-browser-3; "a row of tiles, never a screen" held only on wide screens)
    error = '.part[data-part-state="error"]'
    assert f"{error} .part__skeleton, {error} .part__slow {{ display: none; }}" in block
    assert not [
        (selector, body)
        for selector, body in _rules(css)
        if selector.startswith(kpis)
        and '[data-part-state="error"]' in selector
        and "part__skeleton" in selector
    ]
    # the tiles are far taller than the error box (a title and a button in 1rem of padding, about 100 px)
    assert min(real for _, _, real in KPI_TILES) > 160


def test_a_parts_answer_holds_no_room_while_htmx_settles_it():
    # htmx 2 gives the new element the class and style of the one it replaces until it settles (about 20 ms)
    htmx = (STATIC / "vendor" / "htmx" / "htmx.min.js").read_text()
    settled = json.loads(re.search(r"attributesToSettle:(\[[^\]]*\])", htmx).group(1))
    assert "class" in settled and "style" in settled
    # the answer is known by its own attribute, which htmx leaves alone
    loaded = (TEMPLATES / "components" / "part_loaded.html").read_text()
    assert 'class="part is-loaded" data-part-loaded="' in loaded
    assert "data-part-loaded" not in settled
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    # so it holds no room from its first frame, with the placeholder's class and its --part-h (up to round 3 the
    # For you card and the figures jumped 88 px down and back on a cold load at 1920 px)
    zero = ".part[data-part-loaded] { min-height: 0; }"
    assert zero in block
    reserving = [m.start() for m in re.finditer(r"\.part \{ min-height: var\(--part-h", block)]
    assert reserving and all(at < block.index(zero) for at in reserving)
    # and the middle height's float is a placeholder's only ([data-part], which the answer does not carry)
    assert 'data-part="' not in loaded


def _stage_a() -> str:
    css = (STATIC / "css" / "app.css").read_text()
    return css.split("stage A (reports core)", 1)[1].split("end of stage A */", 1)[0]


# The partner page's Monitoring insights panel on the x20 parity world (scripts/parity), partners 1, 3, 4, 5 and 7:
# (window width, width of its part, the panel's height most of them have there; an administrator's is a viewer's).
# Its four figures and the line on the last visit wrap with the part's width (the figures two to a row below a
# 576 px window). Up to round 1 of step 2 the placeholder kept 150 px, 165 px where the page's panels stack and
# 300 px on a phone: the headings below moved 18 px at 768-1100 px, 13 px at 1200-1280 px, 117 px at 600 px.
FMM_PANEL = [
    (320, 288, 344.2),
    (360, 328, 295.7),
    (390, 358, 295.7),
    (440, 408, 276.5),
    (480, 448, 256.9),
    (520, 488, 242.5),
    (540, 508, 228.1),
    (580, 548, 197.4),
    (600, 568, 183),
    (768, 730, 183),
    (850, 807.5, 183),
    (860, 817, 163.3),
    (1000, 678, 183),
    (1024, 701, 183),
    (1100, 773, 183),
    (1140, 811, 163.3),
    (1200, 868, 163.3),
    (1280, 944, 163.3),
    (1310, 974, 148.9),
    (1366, 1030, 148.9),
    (1600, 1264, 148.9),
    (1920, 1584, 148.9),
]


def test_the_partner_monitoring_insights_placeholder_follows_the_panel_at_every_width():
    block = _stage_a()
    fmm = '#part-datamart #part-fmm[data-part="fmm"]'
    # no fixed height by window width, nor the middle height's float: its grey shapes hold the room
    assert f"{fmm}:not(.is-loaded) {{ min-height: 0; display: grid; }}" in block
    assert f"{fmm}:not(.is-loaded)::before {{ content: none; }}" in block
    # their height follows their own width (a container query), and they are laid out even off screen: a
    # low-priority placeholder skips its grey shapes there otherwise (content-visibility), which would leave no room
    assert f"{fmm} > .part__skeleton {{ container-type: inline-size; content-visibility: visible; }}" in block
    shapes = re.escape(f"{fmm} .part__skeleton > .skeleton")
    default = float(re.search(shapes + r" \{ height: ([\d.]+)px; \}", block).group(1))
    ladder = [
        (float(least), float(height))
        for least, height in re.findall(
            r"@container \(min-width: ([\d.]+)px\) \{ " + shapes + r" \{ height: ([\d.]+)px; \} \}", block
        )
    ]
    assert len(ladder) >= 8 and ladder == sorted(ladder, key=lambda rule: rule[0])

    def room(width):
        """The grey shapes' height in a part of this width: the last rule that holds wins."""
        height = default
        for least, value in ladder:
            if width >= least:
                height = value
        return height

    misses = {window: (room(width), real) for window, width, real in FMM_PANEL if abs(room(width) - real) > 1}
    assert misses == {}
    # its other states keep that room: the slow line and the error box share the grey shapes' grid cell
    assert f"{fmm}:not(.is-loaded) > :not(noscript) {{ grid-area: 1 / 1; min-width: 0; }}" in block
    hidden = f'{fmm}[data-part-state="error"] .part__skeleton {{ display: block; visibility: hidden; }}'
    assert hidden in block
    slow = re.search(re.escape(fmm) + r" \.part__slow \{([^}]*)\}", block).group(1)
    assert "align-self: end;" in slow and "background: var(--nd-bg);" in slow
    # the rules hold for that placeholder only: the low-priority "fmm" part the figures' answer (#part-datamart) brings
    figures = (Path(settings.BASE_DIR) / "neurodb" / "reports" / "templates" / "reports" / "parts").joinpath(
        "partner_datamart.html"
    )
    tag = re.search(r'\{% part "fmm"[^%]*%\}', figures.read_text()).group(0)
    assert 'priority="low"' in tag and "element_id" not in tag
    assert '<div id="part-{{ part_name }}"' in (TEMPLATES / "components" / "part_loaded.html").read_text()


def test_a_failed_partner_figures_part_shows_its_error_box_without_the_figures_room():
    block = _stage_a()
    # up to round 1 of step 2 the 97 px error box stood on 7,500 px of empty room (12,400 px on a phone), with the
    # knowledge base panel and the end of the page below it
    # the generic rule does it (a rule of the partner page's own did, the same two declarations: review
    # R1-cross-2): the error state drops the room every placeholder gets (and the phone's) and the middle height's
    # float, coming after them with as much weight
    css = (STATIC / "css" / "app.css").read_text()
    generic = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    collapse = '.part[data-part-state="error"] { min-height: 0; }'
    no_float = '.part[data-part-state="error"]::before { content: none; }'
    for reserving in (
        ".part { min-height: var(--part-h, 8rem); }",
        ".part { min-height: var(--part-h-sm, var(--part-h-md, var(--part-h, 8rem))); }",
        '[data-part]:not(.is-loaded)::before { content: ""; float: left;',
    ):
        assert generic.index(reserving) < generic.index(collapse) < generic.index(no_float), reserving
    # and no rule of the partner page gives its figures a room of their own, in any state: Try again gives the
    # placeholder's room back while the part loads
    assert re.findall(r"#part-datamart\[[^{]*\{", block) == []


def _rules(css: str) -> list[tuple[str, str]]:
    """Every (selector, body) of a stylesheet, media queries opened (comments dropped)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2).strip()) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def test_a_failed_part_keeps_no_room_on_any_page():
    """A part that could not be loaded leaves its error box, not a screen of empty space (STAGE3). Up to step 3's
    review the overview's figures kept 8,100 px under a 97 px error box on a phone, the brief's sections 11,215 px,
    Data health's jobs 1,395 px (their grey cards laid out unseen), and partner monitoring's summary 1,212 px (only
    the last part of its area collapsed): reviews R1-browser-1 and R1-browser-2."""
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    collapse = '.part[data-part-state="error"] { min-height: 0; }'
    no_float = '.part[data-part-state="error"]::before { content: none; }'
    assert collapse in block and no_float in block
    # it outranks the room every placeholder gets (.part: one class) and comes after the middle height's float, whose
    # selector weighs the same ([data-part]:not(.is-loaded)::before)
    for reserving in (
        ".part { min-height: var(--part-h, 8rem); }",
        ".part { min-height: var(--part-h-sm, var(--part-h-md, var(--part-h, 8rem))); }",
        '[data-part]:not(.is-loaded)::before { content: ""; float: left;',
    ):
        assert block.index(reserving) < block.index(no_float), reserving
    # no rule gives a failed part room back: only the placeholder whose room is a row of its own grey shapes (344 px
    # at most) keeps them, laid out unseen (the partner page's Monitoring insights panel). The overview's key figures
    # no longer do: on a phone their five tiles stack to 868 px, which left 771 px of blank page under the error box
    # (review R1-browser-3)
    kept = [
        selector
        for selector, body in _rules(css)
        if '[data-part-state="error"]' in selector
        and ("visibility: hidden" in body or "min-height:" in body)
        and "min-height: 0" not in body
    ]
    assert kept == ['#part-datamart #part-fmm[data-part="fmm"][data-part-state="error"] .part__skeleton']
    # and the two rules above do it for every page: no page keeps a copy of its own (review R1-cross-2)
    copies = [
        selector
        for selector, body in _rules(css)
        if '[data-part-state="error"]' in selector
        and ("min-height: 0" in body or "content: none" in body)
        and selector not in ('.part[data-part-state="error"]', '.part[data-part-state="error"]::before')
    ]
    assert copies == []
    # nor does an id give the parts of the step 1 and 2 pages, or partner monitoring's summary, a room of their own
    for name in ("figures", "review", "news", "brief", "sections", "jobs", "readiness", "summary"):
        for selector, body in _rules(css):
            if f"#part-{name}" in selector and "min-height" in body:
                assert ":not(.is-loaded)" in selector or "min-height: 0" in body, (selector, body)


def test_phone_filter_bars_are_folded_from_the_first_paint():
    """On a phone the filter bars fold behind their Filters button. They used to fold only once app.js had run,
    just after the first paint, and the figures under them jumped up 230-340 px (a layout shift of 0.55-0.64 on
    partner monitoring, Progress reports and Monitoring insights): review R1-browser-3. The head's script, which runs
    before the page is drawn, marks that scripts run (html.js); app.css folds the bars from then on and keeps the
    room of the button app.js adds. Without JavaScript nothing is folded."""
    base = (TEMPLATES / "base.html").read_text()
    head = base.split("</head>", 1)[0]
    script = head[head.index("<script nonce=") :]
    assert 'document.documentElement.classList.add("js");' in script
    assert script.index('classList.add("js")') < script.index("try {")  # whatever the storage does
    css = (STATIC / "css" / "app.css").read_text()
    phone = css.split("/* Filter bars fold behind", 1)[1].split("@media (max-width: 767.98px) {", 1)[1]
    phone = phone.split("\n}", 1)[0]
    assert ".filters-collapse.is-collapsible:not(.is-open) .filter-bar { display: none; }" in phone
    assert ".filters-collapse.is-collapsible .filters-toggle { display: inline-flex;" in phone
    # before app.js has run, the bar keeps only the room of the button that takes its place, unseen, where the button
    # will be (just before the bar): nothing above or below it moves when app.js runs
    waiting = dict(_rules(phone))[".js .filters-collapse:not(.is-collapsible):not(.is-open) .filter-bar"]
    for declaration in (
        "visibility: hidden;",
        "overflow: hidden;",
        "min-height: 0;",
        "margin: 0;",
        "padding: 0;",
    ):
        assert declaration in waiting, declaration
    assert (
        "height: calc(0.875rem * 1.5 + 0.5rem + 2px + 0.75rem);" in waiting
    )  # a small button and its margin
    # folded only on phones, and before app.js only where scripts run
    rules = [sel for sel, _body in _rules(css) if ".filters-collapse" in sel and ".filter-bar" in sel]
    assert rules == [
        ".filters-collapse.is-collapsible:not(.is-open) .filter-bar",
        ".js .filters-collapse:not(.is-collapsible):not(.is-open) .filter-bar",
    ]


# base.html's head script, run in node on a stand-in window: ``ran`` says whether app.js ran (it marks the page
# html.app-ready) before the load event, which the script then fires.
HEAD_RUN = """
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
if (ran) classes.add("app-ready");
(listeners.load || []).forEach((fn) => fn({ type: "load" }));
out.afterLoad = [...classes].sort();
console.log(JSON.stringify(out));
"""


@needs_node
def test_phone_filter_bars_stay_open_where_app_js_never_runs():
    """The phone filter bars are folded from the first paint where scripts run (html.js), and only app.js adds the
    Filters button that opens them. Where the head's script ran but app.js did not (app.js or one of its imports
    failed to load or was blocked, or the browser runs no modules), every filter bar stayed hidden with no way to
    open it (review R2-critic-1). app.js marks the page html.app-ready once it has bound the bars; at the load event
    the head drops html.js from a page without that mark, and the bars stay open as they were before step 3."""
    base = (TEMPLATES / "base.html").read_text()
    head = base.split("</head>", 1)[0]
    script = head[head.index("<script nonce=") :].split(">", 1)[1].split("</script>", 1)[0]

    def run_head(ran: bool) -> dict:
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", HEAD_RUN, script, "1" if ran else "0"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    never = run_head(ran=False)
    assert never == {"beforeLoad": ["js"], "afterLoad": []}  # folded until load, then open: no html.js
    ran = run_head(ran=True)
    assert ran == {"beforeLoad": ["js"], "afterLoad": ["app-ready", "js"]}  # folded behind its button
    # app.js marks the page right after its first enhance() has bound the bars, at the top level of the module (it
    # runs before the load event: a module script runs before DOMContentLoaded)
    app = (STATIC / "js" / "app.js").read_text()
    tail = app[app.rindex("\ntrackModules(enhance(document));\n") :]
    assert tail.split("\n")[-2:] == ['document.documentElement.classList.add("app-ready");', ""]
    assert 'classList.add("app-ready")' not in app.replace(tail, "")  # nowhere else
    assert '<script type="module" src="{% static \'js/app.js\' %}"' in base  # deferred, never async
    # the bars a page has bound keep their fold behind the button whatever html.js says; before app.js has bound
    # them they are folded only under html.js
    css = (STATIC / "css" / "app.css").read_text()
    folded = [
        sel
        for sel, body in _rules(css)
        if ".filters-collapse" in sel
        and ".filter-bar" in sel
        and ("display: none" in body or "hidden" in body)
    ]
    assert folded == [
        ".filters-collapse.is-collapsible:not(.is-open) .filter-bar",
        ".js .filters-collapse:not(.is-collapsible):not(.is-open) .filter-bar",
    ]


# base.html's scripts, run in node on a stand-in page with placeholders (components/part.html: the error box with
# its text, Try again and the fallback link, hidden): the head's, then the one a page in parts carries; ``app`` and
# ``parts`` say whether app.js and parts.js ran (they mark the page html.app-ready and html.parts-ready) before the
# load event, which the test then fires; a swap then brings one more placeholder (htmx:load).
HEAD_PARTS_RUN = """
const [head, script, app, parts] = [process.argv[1], process.argv[2], process.argv[3] === "1", process.argv[4] === "1"];
const classes = new Set();
const listeners = {};
const docListeners = {};
function el(attrs = {}, children = []) {
  const node = {
    attrs: { ...attrs },
    children,
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    get hidden() { return "hidden" in this.attrs; },
    set hidden(value) { if (value) this.attrs.hidden = ""; else delete this.attrs.hidden; },
    matches(selector) { return selector.slice(1, -1) in this.attrs; }, // "[name]" only
    querySelectorAll(selector) {
      const all = [];
      const walk = (n) => n.children.forEach((c) => { if (c.matches(selector)) all.push(c); walk(c); });
      walk(this);
      return all;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  };
  return node;
}
const placeholder = (name, extra = {}) => el({ "data-part": name, "aria-busy": "true", ...extra }, [
  el({ class: "part__skeleton", "aria-hidden": "true" }),
  el({ "data-part-error": "", hidden: "" }, [
    el({ "data-part-error-text": "" }),
    el({ "data-part-retry": "" }),
    el({ "data-part-fallback": "", href: `?year=2026&_eager=1`, hidden: "" }),
  ]),
]);
const main = el({ id: "main" }, [placeholder("summary"), placeholder("outcomes")]);
globalThis.document = {
  documentElement: {
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    setAttribute() {},
  },
  querySelectorAll: (selector) => main.querySelectorAll(selector),
  addEventListener: (type, fn) => (docListeners[type] ||= []).push(fn),
};
globalThis.window = { addEventListener: (type, fn) => (listeners[type] ||= []).push(fn), matchMedia: () => ({ matches: false }) };
globalThis.localStorage = { getItem: () => null };
new Function(head)();
new Function(script)();
if (app) classes.add("app-ready");
if (parts) classes.add("parts-ready");
(listeners.load || []).forEach((fn) => fn({ type: "load" }));
const later = placeholder("later");
main.children.push(later);
(docListeners["htmx:load"] || []).forEach((fn) => fn({ type: "htmx:load", target: later }));
const view = (p) => {
  const box = p.querySelector("[data-part-error]");
  return {
    state: p.getAttribute("data-part-state"),
    busy: p.getAttribute("aria-busy"),
    box: !box.hidden,
    retry: !p.querySelector("[data-part-retry]").hidden,
    link: p.querySelector("[data-part-fallback]").hidden ? null : p.querySelector("[data-part-fallback]").getAttribute("href"),
  };
};
console.log(JSON.stringify({ classes: [...classes].sort(), parts: main.children.map(view) }));
"""


@needs_node
def test_placeholders_offer_the_page_drawn_at_once_where_parts_js_never_runs():
    """Before step 3 the pages it converted were drawn whole on the server, so their figures showed even when app.js
    did not run. Now each block is a placeholder only parts.js fills; where scripts run but parts.js never did
    (app.js or one of its imports failed to load or was blocked), every placeholder stayed a grey shape, busy for
    screen readers, for ever, and its way out (*Show the figures*) was only in a <noscript>, which such a browser
    never shows (review R3-critic-2). parts.js marks the page html.parts-ready once it has started; at the load event
    the head turns each placeholder of a page without that mark, and each one a swap brings later, into its error
    box with the link to the page drawn at once in place of Try again, no longer busy (app.css hides the grey
    shapes of a part in error). Where parts.js runs, the head leaves them to it."""
    base = (TEMPLATES / "base.html").read_text()
    head = base.split("</head>", 1)[0]
    head = head[head.index("<script nonce=") :].split(">", 1)[1].split("</script>", 1)[0]
    # only a page in parts (render_page gives parts_eager) carries it: no other page names the mechanism (the block's
    # {% comment %}, never sent, names the mark too; the script reads it once)
    block = base.split("{% if parts_eager is not None %}", 1)[1].split("{% endif %}", 1)[0]
    script = block[block.index("<script nonce=") :].split(">", 1)[1].split("</script>", 1)[0]
    assert base.count("parts-ready") == block.count("parts-ready")
    assert script.count("parts-ready") == 1 and "parts-ready" not in head

    def run_head(app: bool, parts: bool) -> dict:
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                HEAD_PARTS_RUN,
                head,
                script,
                "1" if app else "0",
                "1" if parts else "0",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    stopped = {"state": "error", "busy": "false", "box": True, "retry": False, "link": "?year=2026&_eager=1"}
    waiting = {"state": None, "busy": "true", "box": False, "retry": True, "link": None}
    never = run_head(app=False, parts=False)
    assert never == {"classes": [], "parts": [stopped, stopped, stopped]}  # the one a swap brought too
    # app.js stopped after parts.js had started: the parts still load (only the filter bars stay open)
    assert run_head(app=False, parts=True) == {
        "classes": ["parts-ready"],
        "parts": [waiting, waiting, waiting],
    }
    assert run_head(app=True, parts=True) == {
        "classes": ["app-ready", "js", "parts-ready"],
        "parts": [waiting, waiting, waiting],
    }
    # parts.js marks the page once it has started, at the end of initParts(), and nowhere else
    js = (STATIC / "js" / "parts.js").read_text()
    init = js[js.index("export function initParts()") :]
    assert init.count('document.documentElement.classList.add("parts-ready");') == 1
    tail = init[init.index('document.documentElement.classList.add("parts-ready");') :]
    assert tail.split("\n")[1:3] == ["  return page;", "}"]
    assert js.count("parts-ready") == init.count("parts-ready")
    # a part in error shows its error box, not its grey shapes, and keeps no room
    css = (STATIC / "css" / "app.css").read_text()
    assert (
        '.part[data-part-state="error"] .part__skeleton, .part[data-part-state="error"] .part__slow { display: none; }'
        in css
    )
    assert '.part[data-part-state="error"] { min-height: 0; }' in css


@needs_node
def test_initparts_marks_the_page_and_without_htmx_offers_the_page_drawn_at_once():
    """parts.js marks the page html.parts-ready once initParts() has run (base.html's head then leaves the
    placeholders to it). Where htmx itself did not load, nothing can ask for a part and htmx never fires htmx:load
    (on which parts.js finds its placeholders): parts.js used to mark the page all the same, so base.html's fallback
    stood down and every placeholder stayed grey and busy for ever, while this test passed by firing htmx:load
    through its own stand-in htmx (review R1-browser-2). Now parts.js leaves the page unmarked and asks for nothing,
    and at the load event base.html's fallback (its own script, run here) shows each placeholder's error box, not
    busy, without Try again (nothing could ask) but with the link to the page drawn at once."""
    base = (TEMPLATES / "base.html").read_text()
    block = base.split("{% if parts_eager is not None %}", 1)[1].split("{% endif %}", 1)[0]
    fallback = block[block.index("<script nonce=") :].split(">", 1)[1].split("</script>", 1)[0]
    data = run_dom(
        f"const FALLBACK = {json.dumps(fallback)};\n"
        + """
{
  const { doc, parts } = await page(); // htmx loaded: the page is parts.js's
  parts.initParts();
  out.withHtmx = doc.documentElement.classList.contains("parts-ready");
}
const { doc, win, htmx, clock, main, placeholder, parts } = await page();
const p = placeholder({ name: "summary", kind: "tiles" });
p.querySelector("[data-part-error]").append(doc.h("a", { href: "?year=2025&_eager=1", "data-part-fallback": true, hidden: true }, "Show the figures"));
main.append(p);
new Function(FALLBACK)(); // base.html's own script, as the page runs it before app.js
delete win.htmx; // htmx.min.js did not load
out.page = parts.initParts() ?? null;
out.marked = doc.documentElement.classList.contains("parts-ready");
win.dispatchEvent(new dom.Event("load")); // the load event: nothing has fired htmx:load
await clock.flush();
out.sent = htmx.sent();
out.state = p.dataset.partState;
out.busy = p.getAttribute("aria-busy");
out.box = !p.querySelector("[data-part-error]").hidden;
out.retry = !p.querySelector("[data-part-retry]").hidden;
out.link = !p.querySelector("[data-part-fallback]").hidden;
"""
    )
    assert data["withHtmx"] is True
    assert data["page"] is None and data["marked"] is False and data["sent"] == []
    assert data == {**data, "state": "error", "busy": "false", "box": True, "retry": False, "link": True}


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
    # placeholders take part in the browser's scroll anchoring: a placeholder that is not an anchor lets the card
    # the reader is on move by the whole miss of the placeholders above it as they arrive (up to round 3: 168 to
    # 1,897 px); Back, Forward, reload and #anchors are put back by parts.js (keepOffset, hold) either way
    assert "overflow-anchor" not in block
    # colours only from the theme tokens, which the dark theme redefines
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", block)
    assert "var(--nd-danger)" in block and "var(--nd-border)" in block
    for stage in ("A", "B", "C", "D1", "D2"):
        assert f"stage {stage} (" in css and f"end of stage {stage} */" in css, stage
    assert css.rstrip().endswith("end of stage D2 */")
