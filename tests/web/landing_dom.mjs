// A small stand-in for the browser, enough for the public landing page's script (landing.js) to run in node
// (tests/web/test_landing_frontend.py). The page is the server's own HTML, parsed by Python's html.parser into
// a JSON tree (["tag", {attrs}, ...children] with strings for text): elements with attributes, dataset,
// classList and style, text nodes, <template> content (outside the document, as in a browser), cloning,
// events that bubble to the document and the window, focus with focusin/focusout, the Tab order of the page,
// Enter on a button, matchMedia (prefers-reduced-motion), an IntersectionObserver the test drives and a clock
// for the animation frames. No jsdom, nothing to install.

const kebab = (prop) => prop.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);

// "tag#id.class[attr][attr=v][attr="v"]", in a comma list; no combinators (landing.js uses none)
function matcher(selector) {
  const alternatives = selector.split(",").map((part) => {
    const text = part.trim();
    const tag = /^[a-zA-Z][\w-]*/.exec(text)?.[0]?.toUpperCase() || null;
    const id = /#([\w-]+)/.exec(text.replace(/\[[^\]]*\]/g, ""))?.[1] || null;
    const classes = [...text.replace(/\[[^\]]*\]/g, "").matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
    const attrs = [...text.matchAll(/\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/g)].map((m) => ({ name: m[1], value: m[2] }));
    return (el) =>
      el.nodeType === 1 &&
      (!tag || el.tagName === tag) &&
      (!id || el.id === id) &&
      classes.every((c) => el.classList.contains(c)) &&
      attrs.every(({ name, value }) => {
        const v = el.getAttribute(name);
        return v !== null && (value === undefined || v === value);
      });
  });
  return (el) => alternatives.some((test) => test(el));
}

export class Event {
  constructor(type, { bubbles = true, key, shiftKey = false, relatedTarget = null } = {}) {
    Object.assign(this, { type, bubbles, key, shiftKey, relatedTarget, defaultPrevented: false, target: null });
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

class Listeners {
  constructor() {
    this._listeners = [];
  }
  addEventListener(type, fn, options) {
    this._listeners.push({ type, fn, capture: options === true || options?.capture === true });
  }
  removeEventListener(type, fn) {
    this._listeners = this._listeners.filter((l) => !(l.type === type && l.fn === fn));
  }
  _run(event, capture) {
    for (const l of [...this._listeners])
      if (l.type === event.type && (capture === null || l.capture === capture)) l.fn.call(this, event);
  }
}

class Node extends Listeners {
  constructor(doc) {
    super();
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }
  get parentElement() {
    return this.parentNode?.nodeType === 1 ? this.parentNode : null;
  }
  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }
  get firstElementChild() {
    return this.children[0] || null;
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(value) {
    this.replaceChildren(String(value));
  }
  _adopt(nodes) {
    const out = [];
    for (const node of nodes) {
      if (typeof node === "string") out.push(new Text(this.ownerDocument, node));
      else if (node.nodeType === 11) out.push(...node.childNodes.splice(0));
      else out.push(node);
    }
    for (const node of out) {
      node.remove();
      node.parentNode = this;
    }
    return out;
  }
  append(...nodes) {
    this.childNodes.push(...this._adopt(nodes));
  }
  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  remove() {
    if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  contains(other) {
    for (let node = other; node; node = node.parentNode) if (node === this) return true;
    return false;
  }
  *descendants() {
    for (const child of this.childNodes) {
      if (child.nodeType !== 1) continue;
      yield child;
      yield* child.descendants(); // a <template>'s content is not among its children
    }
  }
  querySelectorAll(selector) {
    const test = matcher(selector);
    return [...this.descendants()].filter(test);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  dispatchEvent(event) {
    event.target = this;
    const path = [];
    for (let node = this.parentNode; node; node = node.parentNode) path.push(node);
    if (path.at(-1) === this.ownerDocument) path.push(this.ownerDocument.defaultView);
    for (const node of [...path].reverse()) node._run(event, true);
    this._run(event, null);
    if (event.bubbles) for (const node of path) node._run(event, false);
    return !event.defaultPrevented;
  }
}

class Text extends Node {
  constructor(doc, data) {
    super(doc);
    this.nodeType = 3;
    this.data = data;
  }
  get textContent() {
    return this.data;
  }
  set textContent(value) {
    this.data = String(value);
  }
  cloneNode() {
    return new Text(this.ownerDocument, this.data);
  }
}

class Fragment extends Node {
  constructor(doc) {
    super(doc);
    this.nodeType = 11;
  }
  cloneNode(deep) {
    const copy = new Fragment(this.ownerDocument);
    if (deep) copy.append(...this.childNodes.map((n) => n.cloneNode(true)));
    return copy;
  }
}

export class Element extends Node {
  constructor(doc, tag) {
    super(doc);
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.style = {};
    if (this.tagName === "TEMPLATE") this.content = new Fragment(doc);
    if (this.tagName === "IMG") Object.assign(this, { complete: false, naturalWidth: 0, currentSrc: "" });
    this.dataset = new Proxy(
      {},
      {
        get: (_, prop) => (typeof prop === "string" ? (this.getAttribute(`data-${kebab(prop)}`) ?? undefined) : undefined),
        set: (_, prop, value) => {
          this.setAttribute(`data-${kebab(prop)}`, value);
          return true;
        },
      },
    );
    const el = this;
    const names = () => (el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    this.classList = {
      contains: (c) => names().includes(c),
      add: (...add) => el.setAttribute("class", [...new Set([...names(), ...add])].join(" ")),
      remove: (...drop) => el.setAttribute("class", names().filter((c) => !drop.includes(c)).join(" ")),
      toggle: (c, force) => {
        const on = force === undefined ? !names().includes(c) : Boolean(force);
        if (on) el.classList.add(c);
        else el.classList.remove(c);
        return on;
      },
    };
  }
  get id() {
    return this.getAttribute("id") || "";
  }
  get hidden() {
    return this.hasAttribute("hidden");
  }
  set hidden(value) {
    if (value) this.setAttribute("hidden", "");
    else this.removeAttribute("hidden");
  }
  get disabled() {
    return this.hasAttribute("disabled");
  }
  set disabled(value) {
    if (!value) return this.removeAttribute("disabled");
    this.setAttribute("disabled", "");
    // a browser's focus fixup: a control disabled while focused loses focus to the page
    if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
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
  matches(selector) {
    return matcher(selector)(this);
  }
  closest(selector) {
    const test = matcher(selector);
    for (let node = this; node?.nodeType === 1; node = node.parentNode) if (test(node)) return node;
    return null;
  }
  cloneNode(deep) {
    const copy = new Element(this.ownerDocument, this.tagName);
    for (const [k, v] of this.attrs) copy.attrs.set(k, v);
    if (deep) copy.append(...this.childNodes.map((n) => n.cloneNode(true)));
    if (deep && this.content) copy.content = this.content.cloneNode(true);
    return copy;
  }
  get focusable() {
    if (this.disabled || this.closest("[hidden]")) return false;
    if (this.hasAttribute("tabindex")) return Number(this.getAttribute("tabindex")) >= 0;
    return (this.tagName === "A" && this.hasAttribute("href")) || this.tagName === "BUTTON";
  }
  focus() {
    const doc = this.ownerDocument;
    if (this.disabled || doc.activeElement === this) return;
    const before = doc.activeElement;
    if (before && before !== doc.body) before.dispatchEvent(new Event("focusout", { relatedTarget: this }));
    doc.activeElement = this;
    this.dispatchEvent(new Event("focusin", { relatedTarget: before === doc.body ? null : before }));
  }
  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }
}

class Document extends Node {
  constructor(win) {
    super(null);
    this.ownerDocument = this;
    this.nodeType = 9;
    this.defaultView = win;
  }
  get documentElement() {
    return this.children[0];
  }
  get body() {
    return this.querySelector("body");
  }
  createElement(tag) {
    return new Element(this, tag);
  }
  querySelectorAll(selector) {
    const test = matcher(selector);
    return [this.documentElement, ...this.documentElement.descendants()].filter(test);
  }
}

function build(doc, node) {
  if (typeof node === "string") return new Text(doc, node);
  const [tag, attrs, ...children] = node;
  const el = new Element(doc, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  (el.content || el).append(...children.map((child) => build(doc, child)));
  return el;
}

function createClock() {
  let now = 0;
  let seq = 0;
  const frames = new Map();
  return {
    now: () => now,
    frame(fn) {
      seq += 1;
      frames.set(seq, fn);
      return seq;
    },
    /** Moves the clock on `ms`, a frame every 16 ms, running the frames asked for on the way. */
    tick(ms = 16) {
      const end = now + ms;
      while (now < end) {
        now = Math.min(end, now + 16);
        const due = [...frames.values()];
        frames.clear();
        for (const fn of due) fn(now);
      }
    },
  };
}

/**
 * Installs window, document, the animation frames and (unless `observer` is false) IntersectionObserver as
 * globals, with the page built from `tree`. `reduceMotion` is what prefers-reduced-motion matches.
 * Returns { win, doc, clock, observers, press, tab, intersect }.
 */
export function installPage(tree, { reduceMotion = false, observer = true } = {}) {
  const clock = createClock();
  const win = new Listeners();
  Object.assign(win, { scrollY: 0, innerHeight: 800 });
  win.matchMedia = (query) => ({ matches: reduceMotion && /prefers-reduced-motion:\s*reduce/.test(query), media: query });
  const doc = new Document(win);
  win.document = doc;
  doc.append(build(doc, tree));
  doc.documentElement.scrollHeight = 9000;
  doc.activeElement = doc.body;
  const observers = [];
  class IntersectionObserver {
    constructor(callback, options) {
      Object.assign(this, { callback, options, watched: new Set() });
      observers.push(this);
    }
    observe(el) {
      this.watched.add(el);
    }
    unobserve(el) {
      this.watched.delete(el);
    }
    disconnect() {
      this.watched.clear();
    }
  }
  if (observer) win.IntersectionObserver = IntersectionObserver;
  const globals = {
    window: win,
    document: doc,
    requestAnimationFrame: (fn) => clock.frame(fn),
    IntersectionObserver: observer ? IntersectionObserver : undefined,
  };
  for (const [name, value] of Object.entries(globals))
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  Object.defineProperty(globalThis, "performance", { value: { now: clock.now }, configurable: true, writable: true });

  /** A key pressed on the focused element: keydown bubbles to the document; Enter on a button or a link
   * clicks it, as a browser does. */
  const press = (key, { shiftKey = false } = {}) => {
    const target = doc.activeElement;
    const go = target.dispatchEvent(new Event("keydown", { key, shiftKey }));
    if (!go) return;
    if (key === "Enter" && (target.tagName === "BUTTON" || target.tagName === "A")) target.click();
    if (key === "Tab") tab(shiftKey ? -1 : 1);
  };
  /** Tab (1) or Shift+Tab (-1): focus moves to the next control of the page in document order. */
  const tab = (direction = 1) => {
    const order = [...doc.documentElement.descendants()].filter((el) => el.focusable);
    const at = order.indexOf(doc.activeElement);
    const next = order[at < 0 ? (direction > 0 ? 0 : order.length - 1) : at + direction];
    if (next) next.focus();
  };
  /** `el` comes into view (or leaves it) for every observer that watches it. */
  const intersect = (el, isIntersecting = true) => {
    for (const o of observers) if (o.watched.has(el)) o.callback([{ target: el, isIntersecting }], o);
  };
  return { win, doc, clock, observers, press, tab, intersect };
}
