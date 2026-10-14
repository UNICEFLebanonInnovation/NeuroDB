// A stand-in donor page for donor.js in node (tests/donors/test_donor_page_parts.py): no browser, no DOM library.
//
// - The elements donor.js reads by id for loading its figures (#main, the error box, the tabs' first blocks) are plain
//   records: dataset, hidden, textContent, innerHTML, attributes and listeners, which the test reads afterwards.
// - Any other element, and anything the drawing code asks of an element (an SVG node, a measured width), is a
//   stand-in that accepts every call and every property, so the drawing runs without a page.
// - A clock the test moves runs the timers (setTimeout, Date.now): a 202's wait or the 60 s budget take no real time.
// - fetch answers from the list the test gives: {status, headers, body}, or "network" for a network error.
//
// installPage() sets all of it on globalThis; the test then runs a fresh copy of donor.js as a module.

const realSetImmediate = globalThis.setImmediate;

// Accepts every call and property; numbers read 0 and text reads "". Each property is the same stand-in each time.
export function anything() {
  const store = new Map();
  const proxy = new Proxy(function () {}, {
    get(_target, prop) {
      if (prop === Symbol.toPrimitive) return (hint) => (hint === "number" ? 0 : "");
      if (prop === Symbol.iterator) return function* () {};
      if (prop === "then") return undefined; // never a promise
      if (prop === "length") return 0;
      if (!store.has(prop)) store.set(prop, anything());
      return store.get(prop);
    },
    set(_target, prop, value) {
      store.set(prop, value);
      return true;
    },
    apply: () => anything(),
    construct: () => anything(),
  });
  return proxy;
}

// One element the test reads back. Unknown properties fall back on a stand-in.
function record(id, dataset = {}) {
  const base = {
    id,
    dataset: { ...dataset },
    hidden: false,
    textContent: "",
    innerHTML: "",
    attributes: {},
    listeners: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; },
    removeAttribute(name) { delete this.attributes[name]; },
    hasAttribute(name) { return name in this.attributes; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    click() { for (const fn of this.listeners.click || []) fn({ target: this, preventDefault() {} }); },
  };
  const extra = anything();
  return new Proxy(base, {
    get: (target, prop) => (prop in target ? target[prop] : extra[prop]),
    set: (target, prop, value) => { target[prop] = value; return true; },
  });
}

// The ids donor.js loads its figures with, and the first block of each tab (drawn once the figures are in)
const KNOWN = ["loadError", "loadErrorText", "loadRetry", "mineEmpty", "mineContent", "fGrant", "tiles", "oTiles"];

export function installPage({ src = "/donor/?year=2026&_part=data", inline = null, answers = [] } = {}) {
  const elements = new Map();
  const main = record("main", { donorSrc: src, donorState: inline ? "drawing" : "waiting" });
  elements.set("#main", main);
  for (const id of KNOWN) elements.set(`#${id}`, record(id));
  elements.get("#loadError").hidden = true;
  const others = new Map();
  const find = (selector) => {
    if (elements.has(selector)) return elements.get(selector);
    if (!others.has(selector)) others.set(selector, anything());
    return others.get(selector);
  };
  const html = record("html");
  html.setAttribute("data-bs-theme", "light");
  const classes = new Set(["js"]); // set by the page's head before donor.js runs
  html.classList = {
    add: (...names) => names.forEach((name) => classes.add(name)),
    remove: (...names) => names.forEach((name) => classes.delete(name)),
    contains: (name) => classes.has(name),
  };
  globalThis.document = {
    documentElement: html,
    querySelector: find,
    querySelectorAll: () => [],
    getElementById: (id) => (id === "donor-data" ? (inline ? { textContent: JSON.stringify(inline) } : null) : find(`#${id}`)),
    createElement: () => anything(),
    createElementNS: () => anything(),
    addEventListener() {},
  };
  globalThis.location = { href: "http://testserver/donor/?year=2026", pathname: "/donor/", search: "?year=2026" };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
  globalThis.MutationObserver = class { observe() {} disconnect() {} };
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.window = globalThis;

  // the clock: timers run when the test moves it (run()), in order of their time
  const clock = { now: 1_000_000, timers: [], seq: 0 };
  Date.now = () => clock.now;
  globalThis.setTimeout = (fn, ms = 0) => {
    const id = ++clock.seq;
    clock.timers.push({ id, at: clock.now + ms, fn });
    return id;
  };
  globalThis.clearTimeout = (id) => {
    const at = clock.timers.findIndex((t) => t.id === id);
    if (at >= 0) clock.timers.splice(at, 1);
  };

  // fetch: each request takes the next answer; the requests are kept (address, headers, time)
  const requests = [];
  const queue = [...answers];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, headers: options.headers || {}, at: clock.now });
    const next = queue.shift();
    if (next === undefined || next === "network") throw new TypeError("Failed to fetch");
    const headers = next.headers || {};
    return {
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      headers: { get: (name) => (name in headers ? headers[name] : null) },
      json: async () => next.body,
    };
  };

  // let every promise settle, run the earliest timer, again, until no timer is left
  async function run(limit = 500) {
    for (let i = 0; i < limit; i++) {
      for (let j = 0; j < 5; j++) await new Promise((resolve) => realSetImmediate(resolve));
      if (!clock.timers.length) return;
      clock.timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const timer = clock.timers.shift();
      clock.now = timer.at;
      timer.fn();
    }
    throw new Error("the timers never stopped");
  }

  return {
    main,
    start: clock.now,
    el: (id) => elements.get(`#${id}`),
    requests,
    answer: (...more) => queue.push(...more),
    run,
    clock,
    classes: () => [...classes].sort(), // the page's html classes
  };
}
