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
  // htmx is about to push an address: the entry goes back to the browser
  htmx.fire(doc.body, "htmx:beforeHistorySave");
  out.handedBack = win.history.scrollRestoration;
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
    assert data["handedBack"] == "auto"
    assert data["notOurs"] == {"mode": "auto", "scrolls": []}
    assert data["noParts"] == ["777 instant"]


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
    # the new address keeps the offset Back returns to (parts.js), and nothing of htmx's
    assert 'window.history.replaceState(offsetState(), "", url)' in bar
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


def test_the_overview_tiles_placeholder_keeps_its_room_when_slow_or_failed():
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
    # on a failure the tiles stay laid out, unseen: every other placeholder drops its grey shapes (its min-height
    # keeps the room), and the one rule that would drop them here is outranked by the id
    error = '.part[data-part-state="error"]'
    assert f"{error} .part__skeleton, {error} .part__slow {{ display: none; }}" in block
    hidden = f'{kpis}[data-part-state="error"] .part__skeleton {{ display: block; visibility: hidden; }}'
    assert hidden in block
    # the tiles are always taller than the error box (a title and a button in 1rem of padding, about 100 px)
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
