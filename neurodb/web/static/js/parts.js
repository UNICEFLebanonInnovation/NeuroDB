// Pages that load in parts (SPEC7 §1.4). A page arrives as its shell, with one placeholder ([data-part]) per
// data block; this module asks for each block in the background, a few at a time, and htmx swaps the answer
// in place of its placeholder. createQueue() is the scheduler: plain logic, no DOM, so node tests import it
// (nothing here touches the page until app.js calls initParts()).
//
// The htmx 2.0.10 event order this module relies on. Read in the unminified source of htmx.org@2.0.10
// (dist/htmx.js; its dist/htmx.min.js is byte for byte vendor/htmx/htmx.min.js, sha256 71ea6718…5c0de):
//  1. htmx.trigger(placeholder, "nd-part") issues the request synchronously: the hx-trigger listener calls
//     issueAjaxRequest(), which fires htmx:beforeRequest on the placeholder before xhr.send(). No
//     htmx:beforeRequest after the trigger means htmx never bound it: the part is asked again once.
//  2. xhr.onload -> handleAjaxResponse(): htmx:beforeSwap on the target (the placeholder), then swap() with
//     no swap delay, so at once: outerHTML inserts the new nodes, then cleanUpElement() fires
//     htmx:beforeCleanupElement on the placeholder while it is still attached, then removes it; then, for a
//     4xx/5xx (never swapped), htmx:responseError. Back in onload: htmx:afterRequest and htmx:afterOnLoad on
//     the placeholder, detached by then (its own listeners still run, nothing reaches body), then both again
//     on the closest ancestor still in the page, with detail.elt overwritten (requestConfig.elt keeps the
//     placeholder).
//     So the swap comes BEFORE htmx:afterRequest, and htmx:beforeCleanupElement fires on a placeholder that
//     its own answer replaces: a part is marked "swapping" in htmx:beforeSwap, is never aborted then, and its
//     slot is freed once, by htmx:afterRequest (listened to on the placeholder itself).
//  3. The default responseHandling swaps every 2xx/3xx, a bodiless 202 too: htmx:beforeSwap turns the swap
//     off for the server's "still computing" answer (202 with X-NeuroDB-Part-Pending: 1), and for an answer
//     that is not the part asked for (no X-NeuroDB-Part with its name: answersPart()). A response that starts
//     with <html> would otherwise be swapped in as its <body>: the whole page, inside the page.
//  4. Failures come in two steps: xhr.onerror -> htmx:afterRequest then htmx:sendError; ontimeout ->
//     htmx:afterRequest then htmx:timeout; onabort -> htmx:afterRequest then htmx:sendAbort. The first event
//     carries detail.error (responseError's carries detail.failed), so htmx:afterRequest leaves those to the
//     specific event, which decides. sendAbort is never an error.
//  5. htmx:load fires in the settle step (makeAjaxLoadTask, a setTimeout of 20 ms after the swap) on each new
//     top-level node, after htmx has processed it; and once on body, from a setTimeout after htmx has
//     processed the page (htmx starts on DOMContentLoaded, after this module ran). Placeholders are
//     discovered there only, so htmx has always bound their trigger before they are fired.
//  6. htmx.ajax(verb, url, {target}) without `source` issues the request on document.body (`elt == null ->
//     body`); with `source: form`, on the form. htmx's own body listener aborts the request of detail.elt on
//     htmx:abort, so an abort is triggered on the element that issued it (the placeholder, the filter form),
//     never on body (the help panel loads through body). Cleanup runs before removal, so a placeholder being
//     cleaned up is still attached and its htmx:abort reaches that listener; xhr.abort() then fires
//     htmx:afterRequest and htmx:sendAbort on it. A swap cleans up and removes its old nodes one at a time
//     (each with everything inside it): while one placeholder is cleaned up, the next ones of the same swap are
//     still attached, so the queue is asked for the next parts only once the swap is over (pumpSoon).
import { loadScript, loadStyle } from "./lib.js";

const PRIORITY = { high: 0, normal: 1, low: 2 };
const SLOW_AFTER = 8000; // the "Still working on this part" line
const RETRY_AFTER = 2000; // the one automatic retry
const PENDING_AFTER = 1500; // asked again after a 202, backing off
const PENDING_MAX = 6000;
const BUDGET = 60000; // as the part's own request timeout (hx-request)
const ANNOUNCE_AFTER = 1000; // "Loading the figures…" only when it takes longer than this
const LOAD_WAIT = 1000; // longest wait for htmx:load after a swap (it comes 20 ms after)
const ANCHOR_DEPTH = 3; // how many parts deep a link looks for its target (a part that brings a part...)

// The words staff see. base.html gives the translated ones on #parts-status (data-text-*); these are the
// English defaults.
export const TEXTS = {
  forbidden: "You do not have access to this.",
  gone: "This part is no longer on the page. Reload the page.",
  timeout: "This part took too long. Try again, or narrow the filter.",
  generic: "This part could not be loaded.",
  loading: "Loading the figures…",
  done: "All figures are shown.",
  failedOne: "1 part could not be loaded.",
  failedMany: "{n} parts could not be loaded.",
  preparing: "Preparing the page…",
};

/** What a failed part request leads to: `retry` once automatically (network errors, timeouts, 502, 503, 504;
 * never a 4xx) and which message it shows. A status of 0 is a network error. */
export function classify(reason) {
  if (reason === "timeout") return { retry: true, message: "timeout" };
  if (reason === "foreign") return { retry: false, message: "gone" }; // not the part asked for: answersPart()
  if (reason === "network" || reason === 0) return { retry: true, message: "generic" };
  const status = Number(reason);
  if (status === 502 || status === 503 || status === 504) return { retry: true, message: "generic" };
  if (status === 403) return { retry: false, message: "forbidden" };
  if (status === 404) return { retry: false, message: "gone" };
  return { retry: false, message: "generic" };
}

/** The identity of a group: its name plus the part's address without `_part`, so two modals or two filter
 * generations of the same page never share a leader. */
export function groupKey(name, href, base = "http://localhost/") {
  if (!name) return null;
  let url;
  try {
    url = new URL(href || "", base);
  } catch {
    return `${name} ${href || ""}`;
  }
  url.searchParams.delete("_part");
  url.searchParams.sort();
  return `${name} ${url.pathname}?${url.searchParams}`;
}

/** Whether a 2xx answer to a placeholder is the part it asked for. A part asked at the page's address
 * (`_part=<name>` in `href`) comes with `X-NeuroDB-Part: <name>` (`header`, web/parts.py); an answer without it
 * comes from something else, such as a release without parts (after a rollback) answering the whole page, and
 * is never swapped in. A url= part asks an existing endpoint, which sends no such header: any answer is its own. */
export function answersPart(href, header, base = "http://localhost/") {
  let asked;
  try {
    asked = new URL(href || "", base).searchParams.get("_part");
  } catch {
    return true;
  }
  return !asked || header === asked;
}

/** The live region's last word on a batch of parts, or null when what it says still holds. The failures of this
 * batch (`errors`; an earlier failure is not said again). Otherwise, with parts still in error from an earlier
 * batch (`left`): how many, when "loading" was said, or when fewer are in error than the region says (`said`,
 * `saidCount`: one put right since). "All shown" only when none is in error, after "loading" or a failure.
 * The count to speak with it: `errors` when this batch failed, else `left`. */
export function closingMessage({ errors = 0, spoke = false, said = null, saidCount = 0, left = 0 } = {}) {
  if (errors > 0) return errors === 1 ? "failedOne" : "failedMany";
  const failure = said === "failedOne" || said === "failedMany";
  if (left === 0) return spoke || failure ? "done" : null;
  if (spoke || (failure && left < saidCount)) return left === 1 ? "failedOne" : "failedMany";
  return null;
}

/**
 * The live region's summary of each batch of parts (SPEC7 §1.4), plain logic over a createQueue(): a batch
 * starts when parts are to come and ends when none are. "Loading the figures…" only when it lasts longer than
 * `delay`; at its end, closingMessage(): this batch's own failures, or the parts still in error when the region's
 * word no longer holds. Returns update(), called after every change; `say(key, n)` speaks.
 */
export function createAnnouncer(queue, { say = () => {}, delay = ANNOUNCE_AFTER, schedule = setTimeout, cancel = clearTimeout } = {}) {
  let batch = null;
  let said = null; // what the region says now
  let saidCount = 0; // and the count it says
  const speak = (key, n = 0) => {
    said = key;
    saidCount = n;
    say(key, n);
  };
  return function update() {
    const remaining = queue.remaining();
    if (remaining > 0 && !batch) {
      const current = { spoke: false, mark: queue.mark() };
      current.timer = schedule(() => {
        if (batch === current && queue.remaining() > 0) {
          current.spoke = true;
          speak("loading");
        }
      }, delay);
      batch = current;
    } else if (remaining === 0 && batch) {
      cancel(batch.timer);
      const errors = queue.errorsSince(batch.mark);
      const left = queue.counts().error;
      const key = closingMessage({ errors, spoke: batch.spoke, said, saidCount, left });
      batch = null;
      if (key) speak(key, errors || left);
    }
  };
}

/**
 * The parts a link to `#id` waits for (SPEC7 §1.4), from `parts` ([{ el, strict, anchors }]): the part the target
 * is in (or is), a part whose data-part-anchors prefix the id starts with, and the parts inside the target when
 * it is a block of the page holding them (#daily-review). Not there: a strict part (a link to the review is no
 * request for its folded findings, which load when their <details> opens), nor anything when the target is the
 * page's main area ("Skip to main content").
 */
export function anchorParts(id, target, parts) {
  const block = target && !target.matches?.("html, body, main, [role='main']");
  return parts.filter(({ el, strict, anchors = "" }) => {
    if (anchors.split(/\s+/).some((prefix) => prefix && id.startsWith(prefix))) return true;
    if (!target) return false;
    if (el.contains(target)) return true;
    return block && !strict && target.contains(el);
  });
}

// ---------------------------------------------------------------------------------------------- the queue
/**
 * The scheduler of one page's parts. Each item: { id, order (document order), priority ("high" | "normal" |
 * "low" or 0-2), group (a groupKey or null), strict (only when seen or asked for), visible (null until the
 * page has measured it) }.
 *
 * - next() returns the items to ask for now (they become "loading"): never more than `max` at once, `leaderMax`
 *   while a group's first member (its leader) is loading; loadAll() raises it to `max` again.
 * - Wanted first: bumped items (an anchor, Try again, a <details> opened; the latest bump first), then high,
 *   then visible normal, then visible low, each in document order. A group is closed until one of its members
 *   has finished (done or error): until then only its leader, the first by priority then document order, may
 *   load, alone, and it is wanted as soon as any member is.
 * - Prefetch: when nothing wanted is left and nothing is loading, the remaining non-strict items, one at a time.
 * - Failures: fail() frees the slot and retries once (state "waiting", requeue() after `delay`) for network
 *   errors, timeouts and 502/503/504, never for a 4xx; otherwise the item is in "error" until retry().
 * - A 202 "still computing": wait() keeps the item loading (its slot too) and returns the delay before asking
 *   again, or null once the `budget` is spent.
 * - swapping(): the item's answer is being swapped in. drop() then does nothing, so the slot is freed once,
 *   by done(). drop() of a loading item frees its slot and returns true: the caller aborts the request.
 * - mark() and errorsSince(mark): the parts that went into error after a point (the live region's batches).
 */
export function createQueue(options = {}) {
  const max = Math.max(1, Math.floor(Number(options.max)) || 3);
  const leaderMax = Math.max(1, Math.min(max, Math.floor(Number(options.leaderMax ?? max - 1)) || 1));
  const alive = options.alive || (() => true);
  const onDrop = options.onDrop || (() => {});
  const now = options.now || (() => Date.now());
  const retryDelay = options.retryDelay ?? RETRY_AFTER;
  const pendingDelay = options.pendingDelay ?? PENDING_AFTER;
  const pendingMax = options.pendingMax ?? PENDING_MAX;
  const budget = options.budget ?? BUDGET;

  const items = new Map();
  const groups = new Map(); // key -> { opened }
  const loading = new Set(); // ids holding a slot (loading or swapping)
  const doneIds = new Set();
  let bumps = 0;
  let failures = 0; // parts that went into error so far: an item's failedAt is its place in that count
  let all = false;
  let waiters = [];

  const members = (key) => [...items.values()].filter((i) => i.group === key);
  const isOpen = (item) => !item.group || groups.get(item.group)?.opened === true;
  const byPlace = (a, b) => a.tier - b.tier || a.order - b.order;
  const leaderOf = (key) => members(key).sort(byPlace)[0];
  const isLeader = (item) => !isOpen(item) && leaderOf(item.group) === item;
  const wantedSelf = (item) => all || item.bump > 0 || item.visible === true || (item.tier === 0 && !item.strict);
  const wanted = (item) => wantedSelf(item) || (isLeader(item) && members(item.group).some(wantedSelf));
  const rank = (item) => (isLeader(item) ? Math.max(...members(item.group).map((m) => m.bump)) : item.bump);
  const byRank = (a, b) => rank(b) - rank(a) || byPlace(a, b);
  const prefetchable = (item) => !item.strict || (isLeader(item) && members(item.group).some((m) => !m.strict));
  const eligible = (item) => item.state === "queued" && (isOpen(item) || isLeader(item));
  const leaderLoading = () => [...loading].some((id) => !isOpen(items.get(id)));

  function limit() {
    return all || !leaderLoading() ? max : leaderMax;
  }

  function open(item) {
    if (item.group) groups.get(item.group).opened = true;
  }

  function remove(item) {
    items.delete(item.id);
    loading.delete(item.id);
    if (item.group && !members(item.group).length) groups.delete(item.group);
  }

  function unfinished() {
    let n = 0;
    for (const item of items.values()) if (item.state !== "error") n += 1;
    return n;
  }

  function settle() {
    if (!waiters.length || unfinished() > 0) return;
    const resolve = waiters;
    waiters = [];
    all = false;
    resolve.forEach((fn) => fn());
  }

  function choose() {
    const queued = [...items.values()].filter(eligible);
    const want = queued.filter(wanted).sort(byRank);
    if (want.length) return want[0];
    if (loading.size) return null;
    // Prefetch waits until the page has said which parts are in view.
    if ([...items.values()].some((i) => i.state === "queued" && i.visible === null && !i.strict && i.tier !== 0)) return null;
    return queued.filter(prefetchable).sort(byRank)[0] || null;
  }

  const queue = {
    add({ id, order = 0, priority = 1, group = null, strict = false, visible = null, data } = {}) {
      if (items.has(id)) return items.get(id);
      const tier = typeof priority === "number" ? priority : (PRIORITY[priority] ?? 1);
      const item = {
        id,
        order,
        tier,
        group: group || null,
        strict: !!strict,
        visible,
        bump: 0,
        state: "queued",
        autoRetried: false,
        firedAt: 0,
        waits: 0,
        data,
      };
      items.set(id, item);
      if (item.group && !groups.has(item.group)) groups.set(item.group, { opened: false });
      return item;
    },
    update(id, fields = {}) {
      const item = items.get(id);
      if (item && Number.isFinite(fields.order)) item.order = fields.order;
    },
    setVisible(id, visible) {
      const item = items.get(id);
      if (item) item.visible = !!visible;
    },
    bump(ids) {
      bumps += 1;
      for (const id of [].concat(ids)) {
        const item = items.get(id);
        if (item) item.bump = bumps;
      }
    },
    next() {
      for (const item of [...items.values()]) {
        if (item.state === "queued" && !alive(item)) {
          remove(item);
          onDrop(item);
        }
      }
      const out = [];
      while (loading.size < limit()) {
        const item = choose();
        if (!item) break;
        // a leader starts only within the lower cap: its slot is kept for it rather than given to a later part
        if (!all && isLeader(item) && loading.size >= leaderMax) break;
        item.state = "loading";
        item.firedAt = now();
        item.waits = 0;
        loading.add(item.id);
        out.push(item);
      }
      settle();
      return out;
    },
    swapping(id) {
      const item = items.get(id);
      if (!item || item.state !== "loading") return false;
      item.state = "swapping";
      return true;
    },
    done(id) {
      const item = items.get(id);
      if (!item || (item.state !== "loading" && item.state !== "swapping")) return false;
      open(item);
      remove(item);
      doneIds.add(id);
      settle();
      return true;
    },
    fail(id, reason) {
      const item = items.get(id);
      if (!item || (item.state !== "loading" && item.state !== "swapping")) return null;
      loading.delete(id);
      const verdict = classify(reason);
      if (verdict.retry && !item.autoRetried) {
        item.autoRetried = true;
        item.state = "waiting";
        return { retry: true, delay: retryDelay, message: verdict.message };
      }
      item.state = "error";
      item.message = verdict.message;
      failures += 1;
      item.failedAt = failures;
      open(item);
      settle();
      return { retry: false, message: verdict.message };
    },
    requeue(id) {
      const item = items.get(id);
      if (!item || item.state !== "waiting") return false;
      item.state = "queued";
      queue.bump(id);
      return true;
    },
    retry(id) {
      const item = items.get(id);
      if (!item || item.state !== "error") return false;
      item.state = "queued";
      item.autoRetried = false;
      queue.bump(id);
      return true;
    },
    reset(id) {
      const item = items.get(id);
      if (!item || !["loading", "swapping", "waiting"].includes(item.state)) return false;
      loading.delete(id);
      item.state = "queued";
      return true;
    },
    wait(id) {
      const item = items.get(id);
      if (!item || item.state !== "loading") return null;
      item.waits += 1;
      const delay = Math.min(pendingDelay * 1.5 ** (item.waits - 1), pendingMax);
      return now() - item.firedAt + delay > budget ? null : Math.round(delay);
    },
    drop(id, { force = false } = {}) {
      const item = items.get(id);
      if (!item || (item.state === "swapping" && !force)) return false;
      const held = loading.has(id);
      remove(item);
      settle();
      return held;
    },
    loadAll() {
      all = true;
      return new Promise((resolve) => {
        waiters.push(resolve);
        settle();
      });
    },
    state(id) {
      return items.get(id)?.state ?? (doneIds.has(id) ? "done" : undefined);
    },
    /** Parts still to come: the loading and waiting ones, and the queued ones that will be asked for (a strict
     * one only once seen or asked for). */
    remaining() {
      let n = 0;
      for (const item of items.values()) {
        if (item.state === "error") continue;
        if (item.state === "queued" && item.strict && !all && !item.bump && item.visible !== true) continue;
        n += 1;
      }
      return n;
    },
    unfinished,
    /** A mark in the failures so far: errorsSince(mark) counts the parts in error that failed after it (a part
     * that failed before, and failed again after Try again, counts). */
    mark() {
      return failures;
    },
    errorsSince(mark = 0) {
      let n = 0;
      for (const item of items.values()) if (item.state === "error" && item.failedAt > mark) n += 1;
      return n;
    },
    counts() {
      const counts = { queued: 0, loading: loading.size, waiting: 0, error: 0, done: doneIds.size };
      for (const item of items.values())
        if (item.state === "queued" || item.state === "waiting" || item.state === "error") counts[item.state] += 1;
      return counts;
    },
    limit,
    get size() {
      return items.size;
    },
  };
  return queue;
}

// --------------------------------------------------------------------------------------- the page's glue
const moduleWork = new Set();

/** app.js hands over each enhance() promise (charts, maps... starting): loadAll() waits for them. */
export function trackModules(promise) {
  if (!promise || typeof promise.then !== "function") return promise;
  moduleWork.add(promise);
  const forget = () => moduleWork.delete(promise);
  promise.then(forget, forget);
  return promise;
}

let page = null;

/** How many parts of the page have not arrived or failed yet (strict ones included). */
export function pendingParts() {
  return page ? page.queue.unfinished() : 0;
}

/** How many enhance() runs (charts, maps... starting) have not settled yet: the charts of a part that has just
 * arrived, or of parts drawn with the page (the management brief once its figures are stored), may still be
 * drawing when every part is in. Printing waits for them too. */
export function pendingModules() {
  return moduleWork.size;
}

/** Load every part, strict ones too, whether in view or not, with the cap raised, then wait for the charts
 * inside them: for printing. Resolves when all have arrived or failed, or after `timeout` ms. */
export function loadAll(options) {
  return page ? page.loadAll(options) : Promise.resolve();
}

/** A word staff see, translated when base.html gives it on #parts-status. */
export function partText(key, n) {
  const raw =
    (typeof document !== "undefined" && document.getElementById("parts-status")?.dataset[`text${key[0].toUpperCase()}${key.slice(1)}`]) ||
    TEXTS[key] ||
    TEXTS.generic;
  return raw.replace("{n}", String(n ?? ""));
}

const later = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

// A page with parts keeps its scroll offset in its own history entry, under this key (initParts(): "Back,
// Forward and reload").
const OFFSET = "ndPartsOffset";
const SAVE_AFTER = 200; // the offset is kept once the scrolling has stopped this long

/** The state for app.js to give the history entry it replaces (the filter bar's new address): nothing of
 * htmx's, and the page's offset when the page keeps its own. */
export function offsetState() {
  try {
    return window.history.scrollRestoration === "manual" ? { [OFFSET]: Math.round(window.scrollY || 0) } : {};
  } catch {
    return {};
  }
}

function navigationType() {
  try {
    return window.performance?.getEntriesByType?.("navigation")?.[0]?.type || "";
  } catch {
    return "";
  }
}

function entryState() {
  try {
    const state = window.history?.state;
    return state && typeof state === "object" ? state : null;
  } catch {
    return null;
  }
}

export function initParts() {
  if (page || typeof document === "undefined") return page;
  const htmx = () => window.htmx;
  const status = document.getElementById("parts-status");
  const parallel = Number.parseInt(document.querySelector('meta[name="nd-parts-parallel"]')?.content, 10);
  const recs = new Map(); // id -> record
  const byEl = new WeakMap(); // placeholder -> record
  const loadWaiters = new Set();
  const arrivals = new Set();
  let uid = 0;
  let leaving = false;
  let scrolled = false;
  let anchor = null;
  let landing = null; // a link whose parts are all in, until it goes to its target: { id, depth }
  let hold = null;
  let started = false;
  let booted = false;
  let restore = null; // { y } while the page goes back to the offset it had (Back, Forward, reload)
  let saveTimer = null;

  const queue = createQueue({
    max: Number.isFinite(parallel) && parallel > 0 ? Math.min(parallel, 6) : 3,
    alive: (item) => recs.get(item.id)?.el.isConnected === true,
    onDrop: (item) => recs.has(item.id) && finish(recs.get(item.id)),
  });
  const announce = createAnnouncer(queue, { say }); // the live region (say: below)

  const watchNear = "IntersectionObserver" in window ? new IntersectionObserver(seen, { rootMargin: "800px 0px" }) : null;
  const watchStrict = "IntersectionObserver" in window ? new IntersectionObserver(seen, { rootMargin: "0px" }) : null;

  function seen(entries) {
    for (const entry of entries) {
      const rec = byEl.get(entry.target);
      if (rec && !rec.over) queue.setVisible(rec.id, entry.isIntersecting);
    }
    pump();
  }

  // ------------------------------------------------------------------ placeholders
  function register(el) {
    const strict = el.dataset.partLazy === "strict";
    const rec = {
      id: (uid += 1),
      el,
      name: el.dataset.part,
      elId: el.id,
      strict,
      anchors: el.dataset.partAnchors || "",
      inner: /^\s*innerHTML/i.test(el.getAttribute("hx-swap") || ""),
      marker: el.querySelector("[data-part-error]") || el.firstElementChild,
      generic: el.querySelector("[data-part-error-text]")?.textContent.trim() || "",
      timers: {},
      off: [],
      aborts: 0,
    };
    recs.set(rec.id, rec);
    byEl.set(el, rec);
    queue.add({
      id: rec.id,
      priority: el.dataset.partPriority || "normal",
      group: groupKey(el.dataset.partGroup, el.getAttribute("hx-get"), window.location.href),
      strict,
      visible: watchNear ? null : !strict,
    });
    setState(rec, "queued");
    listen(rec);
    (strict ? watchStrict : watchNear)?.observe(el);
    if (!strict) preload(el);
    return rec;
  }

  function listen(rec) {
    const on = (type, handler) => {
      const fn = (e) => e.target === rec.el && handler(e.detail || {});
      rec.el.addEventListener(type, fn);
      rec.off.push(() => rec.el.removeEventListener(type, fn));
    };
    on("htmx:beforeRequest", () => {
      rec.sent = true;
    });
    on("htmx:responseError", (d) => failed(rec, d.xhr?.status ?? 0));
    on("htmx:sendError", () => failed(rec, "network"));
    on("htmx:timeout", () => failed(rec, "timeout"));
    on("htmx:sendAbort", () => aborted(rec));
    on("htmx:onLoadError", () => failed(rec, "error"));
    on("htmx:afterRequest", (d) => afterRequest(rec, d));
  }

  function setState(rec, state) {
    const { el } = rec;
    el.dataset.partState = state;
    el.setAttribute("aria-busy", state === "error" ? "false" : "true");
    const box = el.querySelector("[data-part-error]");
    if (box) box.hidden = state !== "error";
  }

  function clearTimers(rec) {
    Object.values(rec.timers).forEach((t) => clearTimeout(t));
    rec.timers = {};
  }

  // The record is over: no more timers, observers or listeners.
  function finish(rec) {
    rec.over = true;
    clearTimers(rec);
    watchNear?.unobserve(rec.el);
    watchStrict?.unobserve(rec.el);
    rec.off.forEach((fn) => fn());
    rec.off = [];
    recs.delete(rec.id);
    byEl.delete(rec.el);
  }

  function preload(el) {
    const needs = (el.dataset.partNeeds || "").split(/\s+/);
    try {
      if (needs.includes("plotly")) loadScript("plotly").catch(() => {});
      if (needs.includes("maplibre")) {
        loadScript("maplibre").catch(() => {});
        loadStyle("maplibreCss");
      }
    } catch {
      /* an unknown asset: the module loads what it needs itself */
    }
  }

  // ------------------------------------------------------------------ asking
  let pumping = false;
  let again = false;
  function pump() {
    if (leaving) return;
    if (pumping) {
      again = true;
      return;
    }
    pumping = true;
    try {
      do {
        again = false;
        for (const item of queue.next()) {
          const rec = recs.get(item.id);
          if (rec) fire(rec);
        }
      } while (again);
    } finally {
      pumping = false;
    }
    announce();
  }

  // pump() once the running task is over. htmx cleans up and removes the old nodes of a swap one at a time:
  // asked from a cleanup, pump() would still find the next placeholders of that swap attached and send their
  // requests (which the server computes in full) a moment before they go.
  let pumpQueued = false;
  function pumpSoon() {
    if (pumpQueued) return;
    pumpQueued = true;
    queueMicrotask(() => {
      pumpQueued = false;
      pump();
    });
  }

  function fire(rec) {
    preload(rec.el);
    rec.swapping = false;
    rec.pendingHit = false;
    rec.foreign = false;
    setState(rec, "loading");
    clearTimers(rec);
    rec.timers.slow = setTimeout(() => {
      const s = queue.state(rec.id);
      if (!rec.over && (s === "loading" || s === "swapping")) setState(rec, "slow");
    }, SLOW_AFTER);
    send(rec);
  }

  function send(rec) {
    const hx = htmx();
    rec.sent = false;
    if (hx) {
      hx.trigger(rec.el, "nd-part");
      if (!rec.sent && rec.el.isConnected) {
        hx.process(rec.el); // the trigger was not bound (should not happen: discovery waits for htmx:load)
        hx.trigger(rec.el, "nd-part");
      }
    }
    if (!rec.sent && !rec.over) failed(rec, "network");
  }

  // ------------------------------------------------------------------ answers
  function afterRequest(rec, d) {
    if (rec.over) return;
    if (rec.swapping) return succeed(rec);
    if (rec.pendingHit) {
      rec.pendingHit = false;
      return askAgain(rec);
    }
    if (rec.foreign) {
      rec.foreign = false;
      return failed(rec, "foreign"); // "Reload the page", never asked again by itself
    }
    if (d.error || d.failed) return; // decided by htmx:responseError (before) or sendError / timeout / sendAbort (after)
    const xhr = d.xhr;
    if (xhr && /^(HX-Redirect|HX-Location|HX-Refresh):/im.test(xhr.getAllResponseHeaders?.() || "")) return; // leaving the page
    failed(rec, "empty"); // answered without anything to swap in
  }

  function succeed(rec) {
    const { el } = rec;
    const swapped = rec.inner ? el.isConnected && !el.contains(rec.marker) : !el.isConnected;
    if (!swapped) {
      // the swap was called off by someone else: not shown, so not done
      rec.swapping = false;
      return failed(rec, "empty");
    }
    queue.done(rec.id);
    finish(rec);
    let target;
    if (rec.inner) {
      // an existing endpoint that answers its inner content (url= with swap="innerHTML"): the placeholder
      // stays, as the part's element, so it stops being a placeholder (and its 60 s hx-request timeout,
      // which htmx would pass on to the requests made inside it, goes too)
      [...el.attributes].forEach((a) => a.name.startsWith("data-part") && a.name !== "data-part-loaded" && el.removeAttribute(a.name));
      ["aria-busy", "hx-get", "hx-trigger", "hx-swap", "hx-request"].forEach((name) => el.removeAttribute(name));
      el.classList.add("is-loaded");
      target = el;
    } else {
      target = rec.elId ? document.getElementById(rec.elId) : null;
    }
    // Arrivals never move the focus; when it was on the placeholder (Try again), it goes to the new wrapper.
    const active = document.activeElement;
    if (rec.hadFocus && target && (!active || active === document.body || !active.isConnected)) target.focus({ preventScroll: true });
    reached(rec, arrival(target));
    pump();
  }

  function failed(rec, reason) {
    if (rec.over || leaving) return;
    if (!rec.el.isConnected) {
      // gone from the page without htmx cleaning it up: nothing to show, nothing to count
      queue.drop(rec.id, { force: true });
      finish(rec);
      pump();
      return;
    }
    rec.swapping = false;
    rec.pendingHit = false;
    rec.foreign = false;
    clearTimers(rec);
    const verdict = queue.fail(rec.id, reason);
    if (!verdict) return;
    if (verdict.retry) {
      setState(rec, "queued");
      rec.timers.retry = setTimeout(() => queue.requeue(rec.id) && pump(), verdict.delay);
    } else {
      showError(rec, verdict.message);
      reached(rec, null);
    }
    pump();
  }

  // An abort: by this module (cleanup, back-forward restore; then ignored), or by something else while the
  // part is still on the page: it goes back in the queue, not an error; a part aborted again and again shows
  // its error box so Try again is at hand.
  function aborted(rec) {
    if (rec.over || rec.restarting || leaving) return;
    if (!rec.el.isConnected) return failed(rec, "aborted");
    rec.aborts += 1;
    if (rec.aborts > 2) return failed(rec, "aborted");
    clearTimers(rec);
    rec.swapping = false;
    queue.reset(rec.id);
    setState(rec, "queued");
    pump();
  }

  // 202 "still computing" (another request is building what this part reads): ask again later, within the
  // budget, holding the slot; no error.
  function askAgain(rec) {
    const delay = queue.wait(rec.id);
    if (delay == null) return failed(rec, "timeout");
    rec.timers.pending = setTimeout(() => !rec.over && send(rec), delay);
  }

  function showError(rec, key) {
    const { el } = rec;
    setState(rec, "error");
    const text = el.querySelector("[data-part-error-text]");
    if (text) text.textContent = key === "generic" && rec.generic ? rec.generic : partText(key);
    // the focus stays in the part it was in (after Try again): on the button, so another try is one key away
    if (el.contains(document.activeElement)) el.querySelector("[data-part-retry]")?.focus({ preventScroll: true });
  }

  function drop(rec) {
    if (rec.over || rec.swapping) return; // a part being replaced by its own answer is never aborted
    const held = queue.drop(rec.id);
    finish(rec);
    rec.dropped = true;
    if (held && rec.el.isConnected) htmx()?.trigger(rec.el, "htmx:abort");
    reached(rec, null);
    pumpSoon(); // the other placeholders of the same swap are cleaned up next
  }

  // ------------------------------------------------------------------ after a swap
  // Resolves once htmx:load has run for the new content (app.js has then started its charts).
  function arrival(target) {
    if (!target) return Promise.resolve();
    let waiter;
    const promise = new Promise((resolve) => {
      waiter = { target, resolve };
      waiter.timer = setTimeout(() => {
        loadWaiters.delete(waiter);
        resolve();
      }, LOAD_WAIT);
      loadWaiters.add(waiter);
    });
    arrivals.add(promise);
    promise.then(() => arrivals.delete(promise));
    return promise;
  }

  function loaded(elt) {
    for (const waiter of [...loadWaiters]) {
      if (waiter.target === elt || waiter.target.contains?.(elt)) {
        clearTimeout(waiter.timer);
        loadWaiters.delete(waiter);
        waiter.resolve();
      }
    }
  }

  // ------------------------------------------------------------------ anchors
  // A link to something inside a part (or to a shell wrapper holding parts, or matching a part's
  // data-part-anchors prefix: anchorParts) bumps those parts; once they are in, the page scrolls to it, unless
  // the person has scrolled since. The parts above it that are still to come then push it down as they arrive:
  // until they are all in, each arrival puts it back at the top (a hold), still only if the person has not
  // scrolled. A target in a part that one of those parts brings (a part inside a part: the partner page's
  // Monitoring insights panel inside its figures) is not there yet when they are in: the link then waits for
  // that part too (lookInside), up to ANCHOR_DEPTH parts deep.
  function partsFor(id) {
    const list = [...recs.values()].map((rec) => ({ rec, el: rec.el, strict: rec.strict, anchors: rec.anchors }));
    return anchorParts(id, document.getElementById(id), list)
      .map(({ rec }) => rec)
      .filter((rec) => queue.state(rec.id) !== "error");
  }

  function goTo(hash) {
    restore = null; // a link followed: the offset the page came back to no longer counts
    let id = "";
    try {
      id = decodeURIComponent(String(hash || "").replace(/^#/, ""));
    } catch {
      return;
    }
    if (!id) return;
    const list = partsFor(id);
    if (!list.length) return;
    scrolled = false;
    hold = null;
    waitFor(id, list, 0);
  }

  // The link to #id waits for these parts (asked for first); `depth`: how many parts deep it has looked.
  function waitFor(id, list, depth) {
    landing = null;
    queue.bump(list.map((rec) => rec.id));
    anchor = { id, waiting: new Set(list.map((rec) => rec.id)), arrivals: [], depth };
    pump();
  }

  // The parts a link waited for are in, and these placeholders (`fresh`) came with them (discover, on their
  // htmx:load, before the queue is asked for more): when the target is not on the page and is in one of them,
  // the link waits for that one too.
  function lookInside(fresh) {
    if (!landing || scrolled || landing.depth >= ANCHOR_DEPTH || document.getElementById(landing.id)) return;
    const inner = partsFor(landing.id).filter((rec) => fresh.includes(rec.el));
    if (inner.length) waitFor(landing.id, inner, landing.depth + 1);
  }

  // Instant: a smooth scroll (Bootstrap sets scroll-behavior: smooth) keeps the end point it started with while
  // the parts above arrive and grow, and leaves the target half a screen down.
  function place(target) {
    target.scrollIntoView({ block: "start", behavior: "instant" });
    const top = target.getBoundingClientRect().top;
    if (hold) hold.top = top;
    return top;
  }

  // The browser's own smooth scroll to the address's #anchor, still running when the page has just loaded, moves
  // the page one more step after an instant scroll: two frames later the target is put back where it went.
  function scrollTo(target) {
    const top = place(target);
    if (hold) hold.settling = true;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (hold) hold.settling = false;
        if (!scrolled && target.isConnected && Math.abs(target.getBoundingClientRect().top - top) > 2) place(target);
      }),
    );
  }

  function reached(rec, arrived) {
    if (restore) {
      keepOffset(); // at once, before the swap is painted
      settleOffset(arrived);
    }
    if (hold?.above.delete(rec.id)) {
      const { id } = hold;
      if (!hold.above.size) hold = null;
      Promise.resolve(arrived).then(() => {
        const target = !scrolled && document.getElementById(id);
        if (target) scrollTo(target);
      });
    }
    if (!anchor || !anchor.waiting.has(rec.id)) return;
    anchor.waiting.delete(rec.id);
    if (arrived) anchor.arrivals.push(arrived);
    if (anchor.waiting.size) return;
    const { id, arrivals: waits, depth } = anchor;
    anchor = null;
    const current = (landing = { id, depth });
    Promise.all(waits).then(() => {
      if (landing !== current) return; // it waits for a part they brought (lookInside), or another link was followed
      landing = null;
      const target = document.getElementById(id);
      if (!target || scrolled) return;
      target.classList.add("is-target");
      // the parts above the target still to come (a strict one only once asked for: it may never come)
      const above = [...recs.values()].filter(
        (other) =>
          !target.contains(other.el) &&
          other.el.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING &&
          queue.state(other.id) !== "error" &&
          (!other.strict || queue.state(other.id) !== "queued"),
      );
      hold = above.length ? { id, above: new Set(above.map((other) => other.id)), top: null } : null;
      scrollTo(target);
    });
  }

  // ------------------------------------------------------------------ Back, Forward and reload
  // A page drawn again by the server (Back or Forward when the back-forward cache did not keep it, a reload)
  // comes with its placeholders. Left to the browser, it would be put back at its old offset at once, over
  // heights that are only guesses (with an animation: Bootstrap sets scroll-behavior: smooth), then moved by
  // scroll anchoring as the parts arrive above: the person lands elsewhere. So a page with parts keeps its offset
  // in its own history entry (history.scrollRestoration "manual"; saved when the scrolling stops, a link is
  // followed or a form sent) and, back on it, goes there at once and again as each part arrives, until every
  // part starting above the bottom of the screen is in: the layout is then the one the person left, the same
  // figures in view. It stops as soon as the person scrolls, clicks or presses a key. An entry the page has not
  // taken over (visited before, or left by an htmx push: htmx reloads it) stays the browser's.
  function partsAbove(y) {
    const bottom = y + window.innerHeight;
    return [...recs.values()].filter((rec) => {
      const state = queue.state(rec.id);
      if (state === "error" || (rec.strict && state === "queued")) return false;
      const box = rec.el.getBoundingClientRect();
      return (box.width > 0 || box.height > 0) && box.top + (window.scrollY || 0) < bottom;
    });
  }

  // Puts the page at the offset it had; `settled` (the charts of what arrived drawn): ends once no part is to come
  // above the bottom of the screen.
  function keepOffset(settled = false) {
    if (!restore) return;
    if (scrolled) {
      restore = null;
      return;
    }
    const { y } = restore;
    if (Math.abs((window.scrollY || 0) - y) > 1) window.scrollTo({ top: y, behavior: "instant" });
    if (settled && !partsAbove(y).length) restore = null;
  }

  // Once htmx:load has run for what arrived and its charts are drawn (they give their blocks their height; the
  // browser's scroll anchoring has moved the page meanwhile), the page goes back to its offset again.
  function settleOffset(arrived) {
    Promise.resolve(arrived)
      .then(() => Promise.race([Promise.allSettled([...moduleWork]), later(LOAD_WAIT * 2)]))
      .then(() => keepOffset(true));
  }

  // Once per page, when htmx has processed it (and its first placeholders are known): true when it goes back to
  // the offset this history entry kept.
  function resume() {
    const history = window.history;
    if (!history || !("scrollRestoration" in history)) return false;
    const type = navigationType();
    if (history.scrollRestoration !== "manual") {
      // a new visit of a page with parts: its entry is the page's from now on
      if (type === "navigate" && recs.size) {
        try {
          history.scrollRestoration = "manual";
        } catch {
          /* kept by the browser */
        }
      }
      return false;
    }
    const y = Number(entryState()?.[OFFSET]);
    if ((type !== "back_forward" && type !== "reload") || !Number.isFinite(y)) return false;
    restore = { y };
    scrolled = false;
    const above = partsAbove(y).filter((rec) => !rec.strict);
    if (above.length) queue.bump(above.map((rec) => rec.id)); // what is above the screen first
    keepOffset();
    settleOffset(); // the charts of the parts drawn with the page
    return true;
  }

  function saveOffset() {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (window.history?.scrollRestoration !== "manual") return;
    const y = Math.round(restore ? restore.y : window.scrollY || 0);
    const state = entryState();
    if (state?.[OFFSET] === y) return;
    try {
      window.history.replaceState({ ...state, [OFFSET]: y }, "");
    } catch {
      /* refused (too many changes in a row): the next save tries again */
    }
  }

  function bumpWithin(root) {
    const ids = [...root.querySelectorAll("[data-part]")].map((el) => byEl.get(el)?.id).filter(Boolean);
    if (!ids.length) return;
    queue.bump(ids);
    pump();
  }

  // ------------------------------------------------------------------ live region
  function say(key, n) {
    if (!status || !key) return;
    const message = partText(key, n);
    status.textContent = "";
    setTimeout(() => {
      status.textContent = message;
    }, 50);
  }

  // ------------------------------------------------------------------ discovery and page events
  function discover(root) {
    if (!root?.querySelectorAll) return;
    const found = [...(root.matches?.("[data-part]") ? [root] : []), ...root.querySelectorAll("[data-part]")];
    const fresh = found.filter((el) => !byEl.has(el) && el.isConnected);
    if (!fresh.length) return;
    if (!htmx()) {
      // htmx did not load: say so in each placeholder rather than show skeletons for ever
      fresh.forEach((el) => {
        el.dataset.partState = "error";
        el.setAttribute("aria-busy", "false");
        const box = el.querySelector("[data-part-error]");
        if (box) box.hidden = false;
        el.querySelector("[data-part-retry]")?.setAttribute("hidden", "");
      });
      return;
    }
    fresh.forEach(register);
    [...document.querySelectorAll("[data-part]")].forEach((el, order) => byEl.has(el) && queue.update(byEl.get(el).id, { order }));
    if (!started) {
      started = true;
      if (!boot() && window.location.hash) goTo(window.location.hash); // back to its offset, else to its #anchor
    } else lookInside(fresh);
    pump();
  }

  function boot() {
    if (booted) return false;
    booted = true;
    return resume();
  }

  document.addEventListener("htmx:load", (e) => {
    const elt = e.detail?.elt || e.target;
    loaded(elt);
    discover(elt);
    boot(); // the first one is on body: a page without parts goes back to its offset too, if it kept one
  });

  document.addEventListener("htmx:beforeSwap", (e) => {
    const d = e.detail || {};
    const rec = byEl.get(d.requestConfig?.elt);
    if (!rec || rec.over) return;
    if (d.xhr?.status === 202 && d.xhr.getResponseHeader("X-NeuroDB-Part-Pending")) {
      d.shouldSwap = false;
      rec.pendingHit = true;
      return;
    }
    if (d.shouldSwap && !answersPart(rec.el.getAttribute("hx-get"), d.xhr?.getResponseHeader?.("X-NeuroDB-Part"), window.location.href)) {
      // not the part: a whole page (a release without parts, after a rollback) would nest the page in itself
      d.shouldSwap = false;
      rec.foreign = true;
      return;
    }
    if (d.shouldSwap) {
      rec.swapping = true;
      rec.hadFocus = rec.hadFocus || rec.el.contains(document.activeElement);
      queue.swapping(rec.id);
    }
  });

  // A filter or tab swap replaces the area holding placeholders: a part still loading there is aborted.
  document.addEventListener("htmx:beforeCleanupElement", (e) => {
    const rec = byEl.get(e.target);
    if (rec) drop(rec);
  });

  document.addEventListener("click", (e) => {
    if (e.target.closest?.("a[href]")) saveOffset(); // leaving, or off to an #anchor: the entry keeps its offset
    const retry = e.target.closest?.("[data-part-retry]");
    if (retry) {
      const rec = byEl.get(retry.closest("[data-part]"));
      if (rec && queue.retry(rec.id)) {
        rec.hadFocus = true;
        rec.aborts = 0;
        rec.el.tabIndex = -1;
        rec.el.focus({ preventScroll: true }); // before the button hides: the focus stays in the part
        setState(rec, "queued");
        pump();
      }
      return;
    }
    const bump = e.target.closest?.("[data-part-bump]");
    if (bump) {
      const names = bump.dataset.partBump.split(/\s+/);
      const ids = [...recs.values()].filter((rec) => names.includes(rec.name)).map((rec) => rec.id);
      if (ids.length) {
        queue.bump(ids);
        pump();
      }
    }
    const link = e.target.closest?.('a[href^="#"]');
    if (link) goTo(link.getAttribute("href"));
  });
  window.addEventListener("hashchange", () => goTo(window.location.hash));

  // A <details> opened with a placeholder inside (the daily review's "more", strict) asks for it at once.
  document.addEventListener(
    "toggle",
    (e) => {
      if (e.target instanceof HTMLDetailsElement && e.target.open) bumpWithin(e.target);
    },
    true,
  );

  for (const type of ["wheel", "touchmove", "keydown"]) window.addEventListener(type, () => (scrolled = true), { passive: true });
  // During a hold, the target moving on screen while the page scrolls means the person scrolled (with the scroll
  // bar too, which sends none of those events). A part arriving above the target moves it without a scroll (or
  // the browser's scroll anchoring scrolls and keeps it in place), and putting it back scrolls it to where it was.
  window.addEventListener(
    "scroll",
    () => {
      if (!hold || hold.top == null || hold.settling) return;
      const target = document.getElementById(hold.id);
      if (!target || Math.abs(target.getBoundingClientRect().top - hold.top) > 2) {
        hold = null;
        scrolled = true;
      }
    },
    { passive: true },
  );

  // The offset this history entry keeps (Back, Forward and reload, above).
  window.addEventListener(
    "scroll",
    () => {
      if (window.history?.scrollRestoration !== "manual") return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(saveOffset, SAVE_AFTER);
    },
    { passive: true },
  );
  document.addEventListener("submit", saveOffset, true);
  window.addEventListener("pointerdown", () => (restore = null), { passive: true }); // a click, or the scroll bar
  // Back or Forward within the page (to or from an #anchor): the browser puts back nothing for an entry the page
  // keeps, so the page does.
  window.addEventListener("popstate", (e) => {
    if (window.history?.scrollRestoration !== "manual") return;
    restore = null;
    const y = Number(e.state?.[OFFSET]);
    if (Number.isFinite(y)) window.scrollTo({ top: y, behavior: "instant" });
    else if (window.location.hash) {
      let target = null;
      try {
        target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
      } catch {
        /* not an id */
      }
      target?.scrollIntoView({ block: "start", behavior: "instant" });
    }
  });
  // htmx is about to push or replace the address: it gives this entry a state of its own and, back on it, reloads
  // the page. The entry goes back to the browser, which then keeps its offset as it always has.
  document.addEventListener("htmx:beforeHistorySave", () => {
    restore = null;
    try {
      window.history.scrollRestoration = "auto";
    } catch {
      /* kept as it was */
    }
  });

  window.addEventListener("pagehide", () => {
    leaving = true;
  });
  // Back to this page from the back-forward cache: what had not arrived is asked for again.
  window.addEventListener("pageshow", (e) => {
    leaving = false;
    if (!e.persisted) return;
    for (const rec of [...recs.values()]) {
      const state = queue.state(rec.id);
      if (state === "error") continue;
      if (state !== "queued") {
        rec.restarting = true;
        if (rec.el.isConnected) htmx()?.trigger(rec.el, "htmx:abort");
        rec.restarting = false;
        clearTimers(rec);
        queue.reset(rec.id);
      }
      rec.swapping = false;
      rec.pendingHit = false;
      rec.foreign = false;
      setState(rec, "queued");
    }
    pump();
  });

  async function loadEverything({ timeout = BUDGET } = {}) {
    const deadline = Date.now() + timeout;
    const left = () => deadline - Date.now();
    const everything = queue.loadAll();
    pump();
    await Promise.race([everything, later(left())]);
    await Promise.race([Promise.allSettled([...arrivals]), later(Math.min(left(), LOAD_WAIT * 2))]);
    await Promise.race([Promise.allSettled([...moduleWork]), later(left())]);
  }

  page = { queue, loadAll: loadEverything };
  return page;
}
