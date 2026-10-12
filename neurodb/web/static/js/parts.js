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
//     off for the server's "still computing" answer (202 with X-NeuroDB-Part-Pending: 1).
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
//     htmx:afterRequest and htmx:sendAbort on it.
import { loadScript, loadStyle } from "./lib.js";

const PRIORITY = { high: 0, normal: 1, low: 2 };
const SLOW_AFTER = 8000; // the "Still working on this part" line
const RETRY_AFTER = 2000; // the one automatic retry
const PENDING_AFTER = 1500; // asked again after a 202, backing off
const PENDING_MAX = 6000;
const BUDGET = 60000; // as the part's own request timeout (hx-request)
const ANNOUNCE_AFTER = 1000; // "Loading the figures…" only when it takes longer than this
const LOAD_WAIT = 1000; // longest wait for htmx:load after a swap (it comes 20 ms after)

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

/** The live region's last word on a batch of parts: the failures, or "all shown" when "loading" was said. */
export function closingMessage({ errors = 0, spoke = false } = {}) {
  if (errors === 1) return "failedOne";
  if (errors > 1) return "failedMany";
  return spoke ? "done" : null;
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
  let batch = null;
  let started = false;

  const queue = createQueue({
    max: Number.isFinite(parallel) && parallel > 0 ? Math.min(parallel, 6) : 3,
    alive: (item) => recs.get(item.id)?.el.isConnected === true,
    onDrop: (item) => recs.has(item.id) && finish(recs.get(item.id)),
  });

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

  function fire(rec) {
    preload(rec.el);
    rec.swapping = false;
    rec.pendingHit = false;
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
    pump();
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
  // data-part-anchors prefix) bumps those parts; once they are in, the page scrolls to it, unless the person
  // has scrolled since.
  function partsFor(id) {
    const found = new Set();
    const target = document.getElementById(id);
    if (target) {
      const holder = target.closest("[data-part]");
      if (holder && byEl.has(holder)) found.add(byEl.get(holder));
      target.querySelectorAll("[data-part]").forEach((el) => byEl.has(el) && found.add(byEl.get(el)));
    }
    for (const rec of recs.values()) {
      const prefixes = (rec.el.dataset.partAnchors || "").split(/\s+/).filter(Boolean);
      if (prefixes.some((p) => id.startsWith(p))) found.add(rec);
    }
    return [...found].filter((rec) => queue.state(rec.id) !== "error");
  }

  function goTo(hash) {
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
    queue.bump(list.map((rec) => rec.id));
    anchor = { id, waiting: new Set(list.map((rec) => rec.id)), arrivals: [] };
    pump();
  }

  function reached(rec, arrived) {
    if (!anchor || !anchor.waiting.has(rec.id)) return;
    anchor.waiting.delete(rec.id);
    if (arrived) anchor.arrivals.push(arrived);
    if (anchor.waiting.size) return;
    const { id, arrivals: waits } = anchor;
    anchor = null;
    Promise.all(waits).then(() => {
      const target = document.getElementById(id);
      if (!target || scrolled) return;
      target.classList.add("is-target");
      target.scrollIntoView({ block: "start" });
    });
  }

  function bumpWithin(root) {
    const ids = [...root.querySelectorAll("[data-part]")].map((el) => byEl.get(el)?.id).filter(Boolean);
    if (!ids.length) return;
    queue.bump(ids);
    pump();
  }

  // ------------------------------------------------------------------ live region
  function say(message) {
    if (!status || !message) return;
    status.textContent = "";
    setTimeout(() => {
      status.textContent = message;
    }, 50);
  }

  function announce() {
    const remaining = queue.remaining();
    if (remaining > 0 && !batch) {
      const current = { spoke: false };
      current.timer = setTimeout(() => {
        if (batch === current && queue.remaining() > 0) {
          current.spoke = true;
          say(partText("loading"));
        }
      }, ANNOUNCE_AFTER);
      batch = current;
    } else if (remaining === 0 && batch) {
      clearTimeout(batch.timer);
      const errors = queue.counts().error;
      const key = closingMessage({ errors, spoke: batch.spoke });
      batch = null;
      if (key) say(partText(key, errors));
    }
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
      if (window.location.hash) goTo(window.location.hash);
    }
    pump();
  }

  document.addEventListener("htmx:load", (e) => {
    const elt = e.detail?.elt || e.target;
    loaded(elt);
    discover(elt);
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
