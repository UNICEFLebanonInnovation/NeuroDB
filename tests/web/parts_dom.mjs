// A small stand-in for the browser, enough for parts.js's initParts() to run in node (tests/web/
// test_parts_frontend.py): elements with attributes, dataset, children and events (capture and bubble), the few
// selectors parts.js uses, a clock the test moves, the window's scroll offset and history entry, and a fake htmx
// that records part requests and answers them in the order htmx 2.0.10 does (parts.js's header, points 1-6). No
// jsdom, nothing to install.

const FOLLOWING = 4;
const PRECEDING = 2;
const CONTAINS = 8;
const CONTAINED_BY = 16;

const kebab = (prop) => prop.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);

// "tag#id.class[attr][attr=v][attr^=v]", in a comma list; no combinators (parts.js uses none)
function matcher(selector) {
  const alternatives = selector.split(",").map((part) => {
    const text = part.trim();
    const tag = /^[a-zA-Z][\w-]*/.exec(text)?.[0]?.toUpperCase() || null;
    const id = /#([\w-]+)/.exec(text)?.[1] || null;
    const classes = [...text.matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
    const attrs = [...text.matchAll(/\[([\w-]+)(?:(\^?=)["']?([^"'\]]*)["']?)?\]/g)].map((m) => ({ name: m[1], op: m[2], value: m[3] }));
    return (el) =>
      (!tag || el.tagName === tag) &&
      (!id || el.id === id) &&
      classes.every((c) => el.classList.contains(c)) &&
      attrs.every(({ name, op, value }) => {
        const v = el.getAttribute(name);
        if (v === null) return false;
        if (op === "=") return v === value;
        if (op === "^=") return v.startsWith(value);
        return true;
      });
  });
  return (el) => alternatives.some((test) => test(el));
}

function listenersOf(target) {
  if (!target._listeners) target._listeners = [];
  return target._listeners;
}

function addListener(target, type, fn, options) {
  const capture = options === true || options?.capture === true;
  listenersOf(target).push({ type, fn, capture });
}

function removeListener(target, type, fn, options) {
  const capture = options === true || options?.capture === true;
  const list = listenersOf(target);
  const i = list.findIndex((l) => l.type === type && l.fn === fn && l.capture === capture);
  if (i >= 0) list.splice(i, 1);
}

export class Event {
  constructor(type, { detail, bubbles = true } = {}) {
    this.type = type;
    this.detail = detail;
    this.bubbles = bubbles;
    this.defaultPrevented = false;
    this.target = null;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

function dispatch(target, event, win) {
  event.target = target;
  const path = [];
  for (let node = target.parentNode; node; node = node.parentNode) path.push(node);
  if (path.length && path[path.length - 1].isDocument) path.push(win);
  const run = (node, capture) => {
    for (const l of [...listenersOf(node)])
      if (l.type === event.type && (capture === null || l.capture === capture)) l.fn.call(node, event);
  };
  for (const node of [...path].reverse()) run(node, true);
  run(target, null);
  if (event.bubbles) for (const node of path) run(node, false);
  return !event.defaultPrevented;
}

export class Element {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.children = [];
    this.parentNode = null;
    this.text = "";
    this.rect = { top: 0, width: 100, height: 20 }; // top: in the page (getBoundingClientRect() takes off the scroll)
    this.dataset = new Proxy(
      {},
      {
        get: (_, prop) => (typeof prop === "string" ? (this.getAttribute(`data-${kebab(prop)}`) ?? undefined) : undefined),
        set: (_, prop, value) => {
          this.setAttribute(`data-${kebab(prop)}`, value);
          return true;
        },
        deleteProperty: (_, prop) => {
          this.removeAttribute(`data-${kebab(prop)}`);
          return true;
        },
      },
    );
    const el = this;
    this.classList = {
      contains: (c) => (el.getAttribute("class") || "").split(/\s+/).includes(c),
      add: (...names) => {
        const list = (el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
        for (const n of names) if (!list.includes(n)) list.push(n);
        el.setAttribute("class", list.join(" "));
      },
      remove: (...names) =>
        el.setAttribute(
          "class",
          (el.getAttribute("class") || "")
            .split(/\s+/)
            .filter((c) => c && !names.includes(c))
            .join(" "),
        ),
    };
  }
  get id() {
    return this.getAttribute("id") || "";
  }
  set id(value) {
    this.setAttribute("id", value);
  }
  get hidden() {
    return this.hasAttribute("hidden");
  }
  set hidden(value) {
    if (value) this.setAttribute("hidden", "");
    else this.removeAttribute("hidden");
  }
  get tabIndex() {
    return Number(this.getAttribute("tabindex") ?? -1);
  }
  set tabIndex(value) {
    this.setAttribute("tabindex", value);
  }
  get attributes() {
    return [...this.attrs].map(([name, value]) => ({ name, value }));
  }
  get content() {
    return this.getAttribute("content"); // <meta content>
  }
  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  hasAttribute(name) {
    return this.attrs.has(name);
  }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
  }
  removeAttribute(name) {
    this.attrs.delete(name);
  }
  get textContent() {
    return this.text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.text = String(value);
    this.ownerDocument.textSet(this, this.text);
  }
  get firstElementChild() {
    return this.children[0] || null;
  }
  get isConnected() {
    for (let node = this; node; node = node.parentNode) if (node.isDocument) return true;
    return false;
  }
  append(...nodes) {
    for (const node of nodes) {
      if (typeof node === "string") this.text += node;
      else {
        node.remove();
        node.parentNode = this;
        this.children.push(node);
      }
    }
  }
  insertBefore(node, ref) {
    node.remove();
    node.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node);
    else this.children.splice(i, 0, node);
  }
  remove() {
    const parent = this.parentNode;
    if (parent && !parent.isDocument) parent.children.splice(parent.children.indexOf(this), 1);
    this.parentNode = null;
  }
  contains(other) {
    for (let node = other; node; node = node.parentNode) if (node === this) return true;
    return false;
  }
  matches(selector) {
    return matcher(selector)(this);
  }
  closest(selector) {
    const test = matcher(selector);
    for (let node = this; node && !node.isDocument; node = node.parentNode) if (test(node)) return node;
    return null;
  }
  *descendants() {
    for (const child of this.children) {
      yield child;
      yield* child.descendants();
    }
  }
  querySelectorAll(selector) {
    const test = matcher(selector);
    return [...this.descendants()].filter(test);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  compareDocumentPosition(other) {
    if (this === other) return 0;
    if (this.contains(other)) return CONTAINED_BY | FOLLOWING;
    if (other.contains(this)) return CONTAINS | PRECEDING;
    const order = [...this.ownerDocument.documentElement.descendants()];
    return order.indexOf(other) > order.indexOf(this) ? FOLLOWING : PRECEDING;
  }
  addEventListener(type, fn, options) {
    addListener(this, type, fn, options);
  }
  removeEventListener(type, fn, options) {
    removeListener(this, type, fn, options);
  }
  dispatchEvent(event) {
    return dispatch(this, event, this.ownerDocument.window);
  }
  focus() {
    this.ownerDocument.activeElement = this;
  }
  click() {
    this.dispatchEvent(new Event("click"));
  }
  scrollIntoView(options) {
    this.ownerDocument.scrolls.push({ id: this.id, options: options ?? null });
  }
  getBoundingClientRect() {
    return { ...this.rect, top: this.rect.top - (this.ownerDocument.window.scrollY || 0) };
  }
}

export class DetailsElement extends Element {
  get open() {
    return this.hasAttribute("open");
  }
  set open(value) {
    if (value) this.setAttribute("open", "");
    else this.removeAttribute("open");
  }
}

class Document {
  constructor(win) {
    this.isDocument = true;
    this.window = win;
    this.scrolls = [];
    this.windowScrolls = []; // window.scrollTo() calls
    this.said = []; // what #parts-status said, in order
    this.documentElement = this.createElement("html");
    this.documentElement.parentNode = this;
    this.head = this.createElement("head");
    this.body = this.createElement("body");
    this.documentElement.append(this.head, this.body);
    this.activeElement = this.body;
  }
  createElement(tag) {
    return tag.toLowerCase() === "details" ? new DetailsElement(this, tag) : new Element(this, tag);
  }
  textSet(el, text) {
    if (el.id === "parts-status" && text) this.said.push(text);
  }
  /** h("div", { id: "x", "data-part": "kpis" }, child, "text", ...) */
  h(tag, attrs = {}, ...children) {
    const el = this.createElement(tag);
    for (const [name, value] of Object.entries(attrs))
      if (value !== undefined && value !== null && value !== false) el.setAttribute(name, value === true ? "" : value);
    el.append(...children.flat());
    return el;
  }
  getElementById(id) {
    return [...this.documentElement.descendants()].find((el) => el.id === id) || null;
  }
  querySelectorAll(selector) {
    const test = matcher(selector);
    return [this.documentElement, ...this.documentElement.descendants()].filter(test);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  addEventListener(type, fn, options) {
    addListener(this, type, fn, options);
  }
  removeEventListener(type, fn, options) {
    removeListener(this, type, fn, options);
  }
  dispatchEvent(event) {
    return dispatch(this, event, this.window);
  }
}

// ------------------------------------------------------------------------------------------ the clock
function createClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  const realImmediate = globalThis.setImmediate;
  const flush = async () => {
    for (let i = 0; i < 5; i++) await new Promise((resolve) => realImmediate(resolve));
  };
  return {
    now: () => now,
    setTimeout(fn, ms = 0) {
      seq += 1;
      timers.set(seq, { at: now + Math.max(0, Number(ms) || 0), fn, seq });
      return seq;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    flush,
    /** Moves the clock by `ms`, running each timer due on the way, in order, with the promises it starts. */
    async tick(ms = 0) {
      const end = now + ms;
      await flush();
      for (;;) {
        const due = [...timers.values()].filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.seq - b.seq)[0];
        if (!due) break;
        timers.delete(due.seq);
        now = due.at;
        due.fn();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}

// ------------------------------------------------------------------------------------------ htmx
function createHtmx(doc, clock) {
  // the listeners get this very detail object (htmx:beforeSwap's shouldSwap is read back after them)
  const fire = (el, type, detail = {}, bubbles = true) =>
    el.dispatchEvent(new Event(type, { detail: Object.assign(detail, { elt: el }), bubbles }));
  const xhrOf = (status, headers = {}) => ({
    status,
    getResponseHeader: (name) => headers[name] ?? null,
    getAllResponseHeaders: () =>
      Object.entries(headers)
        .map(([k, v]) => `${k}: ${v}`)
        .join("\r\n"),
  });
  // htmx's cleanUpElement: the element first, then its children, each while still attached
  const cleanUp = (el) => {
    fire(el, "htmx:beforeCleanupElement");
    for (const child of [...el.children]) cleanUp(child);
  };
  const settle = (nodes) => clock.setTimeout(() => nodes.forEach((node) => node.isConnected && fire(node, "htmx:load")), 20);

  const htmx = {
    requests: [],
    process() {},
    trigger(el, name, detail) {
      if (name === "nd-part") {
        // the hx-trigger listener issues the request at once: htmx:beforeRequest before the send
        const req = { el, name: el.dataset.part, url: el.getAttribute("hx-get"), state: "open", at: clock.now() };
        if (!fire(el, "htmx:beforeRequest", { requestConfig: { elt: el } })) return;
        htmx.requests.push(req);
        return;
      }
      if (name === "htmx:abort") {
        const req = htmx.requests.find((r) => r.el === el && r.state === "open");
        if (!req) return;
        req.state = "aborted";
        const info = { requestConfig: { elt: el }, xhr: xhrOf(0) };
        fire(el, "htmx:afterRequest", { error: "htmx:afterRequest", ...info });
        fire(el, "htmx:sendAbort", { error: "htmx:sendAbort", ...info });
        return;
      }
      fire(el, name, detail);
    },
    /** The requests still open, by part name. */
    open: () => htmx.requests.filter((r) => r.state === "open").map((r) => r.name),
    sent: () => htmx.requests.map((r) => r.name),
    /** Answers the open request of part `name`: `nodes` (a function returning the new elements) swapped as its
     * hx-swap says, `status`, `headers`. A part asked with `_part` gets `X-NeuroDB-Part: <name>`, as the server
     * sends it (web/parts.py), unless `asPart` is false (an answer from something else). */
    respond(name, { status = 200, nodes = () => [], headers = {}, asPart = true } = {}) {
      const req = htmx.requests.find((r) => r.name === name && r.state === "open");
      if (!req) throw new Error(`no open request for ${name}: ${JSON.stringify(htmx.open())}`);
      req.state = "done";
      const el = req.el;
      const asked = new URL(req.url || "", "http://localhost/").searchParams.get("_part");
      const xhr = xhrOf(status, asPart && asked ? { "X-NeuroDB-Part": asked, ...headers } : headers);
      const isError = status >= 400;
      const info = { xhr, requestConfig: { elt: el }, target: el };
      const before = { ...info, shouldSwap: status >= 200 && status < 400 && status !== 204, isError };
      fire(el, "htmx:beforeSwap", before);
      if (before.shouldSwap) {
        const fresh = nodes();
        if (/^\s*innerHTML/i.test(el.getAttribute("hx-swap") || "")) {
          const old = [...el.children];
          fresh.forEach((node) => el.insertBefore(node, old[0] || null));
          for (const child of old) {
            cleanUp(child);
            child.remove();
          }
        } else {
          const parent = el.parentNode;
          fresh.forEach((node) => parent.insertBefore(node, el));
          cleanUp(el);
          el.remove();
        }
        settle(fresh);
      }
      if (isError) fire(el, "htmx:responseError", { ...info, failed: true });
      fire(el, "htmx:afterRequest", { ...info, successful: !isError, failed: isError });
    },
    /** A tab or filter swap of `area`'s content (innerHTML), as htmx does it: the new nodes go in first, then each
     * old node is cleaned up and removed, one after the other. */
    swapInner(area, fresh) {
      const old = [...area.children];
      fresh.forEach((node) => area.insertBefore(node, old[0] || null));
      for (const child of old) {
        cleanUp(child);
        child.remove();
      }
      settle(fresh);
    },
    fire,
  };
  return htmx;
}

// ------------------------------------------------------------------------------------------ the page
/**
 * Installs window, document, Node, HTMLDetailsElement and the clock's timers as globals, with a page holding
 * #main and #parts-status. `navigation` is how the page was reached (performance's navigation type), `state` and
 * `scrollRestoration` its history entry's. `scrollMax` is the page's deepest offset (its height less the screen's):
 * window.scrollTo stops there, as a browser clamps it. `scrollEvents: "frame"` sends window.scrollTo's scroll event a
 * frame later, as browsers do, instead of at once. Returns { win, doc, htmx, clock, main, placeholder, loaded, start }.
 */
export function installPage({
  parallel = 3,
  hash = "",
  href = "http://localhost/page?year=2025",
  navigation = "navigate",
  state = null,
  scrollRestoration = "auto",
  innerHeight = 800,
  scrollMax = null,
  scrollEvents = "now",
} = {}) {
  const clock = createClock();
  const url = new URL(href);
  const win = { location: { hash, href, pathname: url.pathname, search: url.search }, _listeners: [], scrollY: 0, innerHeight };
  win.history = {
    state,
    scrollRestoration,
    replaceState(next) {
      this.state = next;
    },
    // a new entry keeps the mode of the one it comes from (HTML: "scroll restoration mode" of pushState's entry)
    pushState(next) {
      this.state = next;
    },
  };
  win.performance = { getEntriesByType: (type) => (type === "navigation" ? [{ type: navigation }] : []) };
  win.addEventListener = (type, fn, options) => addListener(win, type, fn, options);
  win.removeEventListener = (type, fn, options) => removeListener(win, type, fn, options);
  win.dispatchEvent = (event) => {
    event.target = win;
    for (const l of [...listenersOf(win)]) if (l.type === event.type) l.fn.call(win, event);
    return true;
  };
  const doc = new Document(win);
  win.document = doc;
  win.scrollTo = (options) => {
    doc.windowScrolls.push({ ...options });
    win.scrollY = scrollMax === null ? options.top : Math.min(options.top, scrollMax);
    if (scrollEvents === "frame") clock.setTimeout(() => win.dispatchEvent(new Event("scroll")), 16);
    else win.dispatchEvent(new Event("scroll"));
  };
  const htmx = createHtmx(doc, clock);
  win.htmx = htmx;
  Object.assign(globalThis, {
    window: win,
    document: doc,
    Node: { DOCUMENT_POSITION_FOLLOWING: FOLLOWING, DOCUMENT_POSITION_PRECEDING: PRECEDING },
    HTMLDetailsElement: DetailsElement,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    requestAnimationFrame: (fn) => clock.setTimeout(() => fn(clock.now()), 16),
  });
  doc.head.append(doc.h("meta", { name: "nd-parts-parallel", content: String(parallel) }));
  const main = doc.h("main", { id: "main", class: "main", tabindex: "-1" });
  doc.body.append(
    doc.h("a", { class: "skip-link", id: "skip", href: "#main" }, "Skip to main content"),
    main,
    doc.h("div", { id: "parts-status", class: "visually-hidden", role: "status", "aria-live": "polite" }),
  );

  /** The markup of components/part.html. */
  const placeholder = ({ name, kind = "card", priority = "normal", group, lazy, anchors, swap = "outerHTML", url, id } = {}) =>
    doc.h(
      "div",
      {
        id: id || `part-${name}`,
        class: `part part--${kind}`,
        "data-part": name,
        "data-part-priority": priority,
        "data-part-group": group,
        "data-part-lazy": lazy,
        "data-part-anchors": anchors,
        "hx-get": url || `/page?year=2025&_part=${name}`,
        "hx-trigger": "nd-part",
        "hx-swap": swap,
        "aria-busy": "true",
      },
      doc.h("div", { class: "part__skeleton", "aria-hidden": "true" }),
      doc.h("p", { class: "part__slow" }, "Still working on this part."),
      doc.h(
        "div",
        { class: "part__error", "data-part-error": true, hidden: true },
        doc.h("p", { class: "state__title", "data-part-error-text": true }, "This part could not be loaded."),
        doc.h("button", { type: "button", "data-part-retry": true }, "Try again"),
      ),
      doc.h("p", { class: "part__print-note" }, "This part was still loading when the page was printed."),
    );
  /** The markup of components/part_loaded.html. */
  const loaded = (name, ...children) =>
    doc.h("div", { id: `part-${name}`, class: "part is-loaded", "data-part-loaded": name, tabindex: "-1" }, ...children);
  /** htmx has processed the page: htmx:load on body (parts.js discovers its placeholders then). */
  const start = async () => {
    htmx.fire(doc.body, "htmx:load");
    await clock.flush();
  };
  return { win, doc, htmx, clock, main, placeholder, loaded, start };
}
