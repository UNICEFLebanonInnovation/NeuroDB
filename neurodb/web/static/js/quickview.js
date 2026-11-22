// Quick view: a plain click on a link to another page of NeuroDB, in a page's content (#main) or in the shared window
// (#modal-content: a partial, or an earlier quick view), opens that page in the shared window (components/modal.html)
// instead of leaving the page: large, full screen on a phone, with the loading shapes at once. Its header holds Back
// (to the quick view before, once there is one), the page's title (the window is labelled by it), Open full page (the
// page's own address) and Close; Esc and Close give the focus back to the link that opened it. It never writes the
// browser's history, and a request made inside it never does either.
//
// The server (web/quick.py) answers a request sent with X-NeuroDB-Quick: 1 with the page drawn in layouts/quick.html,
// and X-NeuroDB-Quick-View: 1. Any other answer (a page that is not quick-viewable: 204 "refused"; a redirect, such as
// signing in again; an error; no answer within 30 s) opens the address as a normal page.
//
// Never a quick view (quickTarget()): a click with Ctrl, Cmd, Shift, Alt or another button (the browser's own); a link
// outside the content (the menu, the top bar, the search); downloads (the download attribute, CSV, Excel, PDF and other
// files, ?export=); links with a target (but an Ask NeuroDB answer's: below) or data-no-quick (on the link or around it),
// the window's own header, Bootstrap toggles; other sites, mailto and hash links; the administration, sign-in and donor addresses (<meta name=
// "nd-quick-skip">, layouts/page.html). On a page, also the links that work the page itself: breadcrumbs, tab bars,
// filter bars and their chips, sort, pagination, page actions, empty states' reset links, links to the page itself,
// and links htmx handles (an existing pop-up, a filter). Inside a quick view those open in the same quick view, in its
// place (no Back), and a GET form there (a filter bar) opens its result there too; a link htmx updates in place inside
// it keeps doing so. There, a link to the quick view's own address (its current tab, a Reset with nothing set) reloads
// it in place and an anchor of it (#id) scrolls the window: neither is left to the browser, which would leave the page
// or write its address.
//
// The window's place for the person: an answer is drawn once the window is shown (Bootstrap shows it after its fade; a
// chart drawn in a hidden box would be drawn 700 px wide). A filter applied inside it keeps the view while its answer
// comes, then puts the focus back in the same field with the letters typed meanwhile (applied in turn) and keeps the
// filters opened on a phone open; only a link or Back moves the focus to the window's title. A quick view of the same
// kind of page as the one under it brings that page's ids (a filter's f-q, a form's target): those are renamed in the
// window (ownIds), so a label names its own field and an htmx answer lands in the window.
//
// Everything is delegated from the document, so it covers what comes later: parts, Ask NeuroDB's answers, partials.
// An answer of Ask NeuroDB or of a chat in a page (.ask-turn__body) has its links open in a new tab (target="_blank",
// assistant.agent.render), so a link never leaves the conversation: in the content the quick view keeps the
// conversation too, so a plain click on one opens it there, like any other link of the content.
import { filterParams } from "./lib.js";

export const ASK_HEADER = "X-NeuroDB-Quick";
export const ANSWER_HEADER = "X-NeuroDB-Quick-View";
const ROOT = "[data-quick-view]";
const AREAS = "#main, #modal-content";
const NEVER = "[data-no-quick], [download], [data-bs-toggle], [data-bs-dismiss], [data-page-link], .modal-header, .skip-link";
const ANSWER = ".ask-turn__body"; // an Ask NeuroDB answer, whose links the server opens in a new tab
const PAGE_CONTROLS = [
  "nav",
  ".breadcrumb",
  ".segmented",
  "[role=tablist]",
  "[role=tab]",
  ".nav-tabs",
  "form",
  ".filter-bar",
  "[data-filters-collapse]",
  ".chip__remove",
  ".chip--removable",
  "thead",
  ".pagination",
  ".page-actions",
  ".dropdown-menu",
  ".table-toolbar",
  ".state",
].join(", ");
const HTMX = "[hx-get], [hx-post], [hx-put], [hx-patch], [hx-delete], [data-hx-get], [data-hx-post], [hx-boost], [data-hx-boost]";
const FILE = /\.(?:csv|xlsx?|zip|pdf|json|geojson|docx?|pptx?|txt|png|jpe?g|gif|svg)$/i;
const FILE_PATH = /\/(?:download|file|cover)\/$/;
const FILE_KEYS = ["export", "download"];
const DIALOG = ["modal-xl", "modal-fullscreen-md-down", "qview-dialog"];
const TIMEOUT = 30000;
const URL_ATTRS = ["href", "action", "formaction", "data-href", "hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete", "data-hx-get", "data-hx-post"];
const KEPT_AS_IS = /^(?:[a-z][a-z0-9+.-]*:|\/|#)/i; // another scheme, an address from the site's root, an anchor
const HTMX_FORM_ATTRS = ["hx-get", "hx-trigger", "hx-target", "hx-select", "hx-swap", "hx-push-url", "hx-include", "hx-indicator"];
// what names an element by its id: id lists (labels, aria), selectors (htmx targets, the page's own buttons), bare ids
// (a chart's or map's data block, a list a chart reads)
const ID_LISTS = ["for", "aria-labelledby", "aria-describedby", "aria-controls", "aria-owns", "aria-details", "aria-errormessage", "aria-activedescendant", "aria-flowto", "list", "form", "headers"];
const ID_SELECTORS = ["hx-target", "hx-include", "hx-indicator", "hx-select", "hx-disabled-elt", "data-hx-target", "data-hx-include", "data-hx-indicator", "data-bs-target", "data-table-target", "data-copy-target", "data-chart-png", "data-rows-png", "data-href-target"];
const ID_NAMES = ["data-source", "data-config", "data-select", "data-table"];
const WINDOW_IDS = new Set(["modal-title"]); // the window's own: it is labelled by its title
const TEXT_FIELD = /^(?:search|text|textarea)$/;

/**
 * Gives the ids of `root` (a quick view) that `taken(id)` says the page under it already has a name of its own
 * ("qv-…"), and rewrites every reference to them inside `root`: labels, aria, htmx targets and the page's own
 * selectors, data blocks and anchors. The old id stays as data-quick-id, which lib.scopedQuery reads. The window's
 * title keeps its id. Returns the names given (old → new).
 */
export function ownIds(root, taken) {
  const renamed = new Map();
  const given = new Set();
  for (const el of [root, ...root.querySelectorAll("[id]")]) {
    const id = el.getAttribute?.("id");
    if (!id || WINDOW_IDS.has(id) || renamed.has(id) || !taken(id)) continue;
    let fresh = `qv-${id}`;
    for (let n = 2; taken(fresh) || given.has(fresh); n += 1) fresh = `qv${n}-${id}`;
    given.add(fresh);
    renamed.set(id, fresh);
  }
  if (!renamed.size) return renamed;
  const inSelector = (text) => text.replace(/#([\w-]+)/g, (all, id) => (renamed.has(id) ? `#${renamed.get(id)}` : all));
  for (const el of [root, ...root.querySelectorAll("*")]) {
    const id = el.getAttribute?.("id");
    if (id && renamed.has(id)) {
      el.setAttribute("id", renamed.get(id));
      el.setAttribute("data-quick-id", id);
    }
    for (const name of ID_LISTS) {
      const value = el.getAttribute?.(name);
      if (value && value.split(/\s+/).some((token) => renamed.has(token)))
        el.setAttribute(name, value.split(/\s+/).map((token) => renamed.get(token) || token).join(" "));
    }
    for (const name of ID_SELECTORS) {
      const value = el.getAttribute?.(name);
      if (value && value.includes("#")) el.setAttribute(name, inSelector(value));
    }
    for (const name of ID_NAMES) {
      const value = el.getAttribute?.(name)?.trim();
      if (value && renamed.has(value)) el.setAttribute(name, renamed.get(value));
    }
    const href = el.getAttribute?.("href");
    if (href?.startsWith("#") && renamed.has(href.slice(1))) el.setAttribute("href", `#${renamed.get(href.slice(1))}`);
  }
  return renamed;
}

/** Whether a click is a plain one (the main button, no Ctrl, Cmd, Shift or Alt) that nothing handled yet. */
export function plainClick(e) {
  return Boolean(e) && !e.defaultPrevented && (e.button ?? 0) === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey;
}

/** Whether an address is a file to download rather than a page. */
export function isDownload(url) {
  return FILE.test(url.pathname) || FILE_PATH.test(url.pathname) || FILE_KEYS.some((key) => url.searchParams.has(key));
}

function skipList() {
  const meta = typeof document !== "undefined" ? document.querySelector('meta[name="nd-quick-skip"]') : null;
  return String(meta?.content || "").split(/\s+/).filter(Boolean);
}

const attr = (el, name) => el?.getAttribute?.(name) ?? el?.getAttribute?.(`data-${name}`) ?? null;

// htmx would push an address (a tab, a filter, a page of a table) or fill the shared window with a partial: inside a
// quick view such a link opens in the quick view instead. hx-target and hx-push-url are inherited from around it.
function leavesView(link) {
  const push = attr(link.closest("[hx-push-url], [data-hx-push-url]"), "hx-push-url");
  const target = attr(link.closest("[hx-target], [data-hx-target]"), "hx-target");
  return Boolean(push && push !== "false") || target === "#modal-content";
}

function pushesUrl(link) {
  const push = attr(link.closest("[hx-push-url], [data-hx-push-url]"), "hx-push-url");
  return Boolean(push && push !== "false");
}

/** Whether `el` is in a link (or form) with a target of its own: the browser's way, but a new tab an answer's link
 * opens (the quick view keeps the conversation as well). */
function ownTarget(el) {
  const named = el.closest("[target]");
  return Boolean(named) && !fromAnswer(named);
}

/** Whether `link` is a link of an Ask NeuroDB answer, which the server opens in a new tab. */
const fromAnswer = (link) =>
  Boolean(link?.matches?.("a[href]") && link.getAttribute("target") === "_blank" && link.closest(ANSWER));

/** Opens `url` in a new tab, with no way back to this page (as rel="noopener"); false when the browser blocked it. */
function newTab(url) {
  const tab = window.open(url, "_blank");
  if (!tab) return false;
  try {
    tab.opener = null;
  } catch {
    /* nothing to cut */
  }
  return true;
}

/**
 * Where a click on `el` (a link, or a row with data-href) going to `raw` opens: { url, replace } for a quick view
 * (`replace`: in place of the quick view it is in, with no Back), { anchor } for an anchor of the quick view it is in
 * (scrolled to in the window), or null for the browser's own way. `page`: the page's address; a relative address in
 * a quick view is read against the quick view's own (data-quick-url).
 */
export function targetOf(el, raw, { page = window.location.href, skip = skipList() } = {}) {
  if (!el?.closest || !el.closest(AREAS) || el.closest(NEVER) || ownTarget(el)) return null;
  const address = String(raw ?? "").trim();
  if (!address) return null;
  const view = el.closest(ROOT);
  const htmx = Boolean(el.matches?.(HTMX));
  const inPlace = Boolean(view) && htmx && !leavesView(el); // htmx updates it inside the quick view
  if (address.startsWith("#")) return view && address.length > 1 && !inPlace ? { anchor: address.slice(1) } : null;
  let here;
  let url;
  try {
    here = new URL(view?.dataset.quickUrl || page, page);
    url = new URL(address, here);
  } catch {
    return null;
  }
  if (url.origin !== here.origin || !/^https?:$/.test(url.protocol)) return null;
  if (skip.some((prefix) => url.pathname.startsWith(prefix)) || isDownload(url)) return null;
  if (url.pathname === here.pathname && url.search === here.search) {
    // this very page: on a page, the browser's own way (an anchor of it); in a quick view, an anchor of it scrolls the
    // window and the address itself reloads it in place
    if (!view || inPlace) return null;
    return url.hash.length > 1 ? { anchor: url.hash.slice(1) } : { url: url.pathname + url.search, replace: true };
  }
  const target = url.pathname + url.search + url.hash;
  const control = url.pathname === here.pathname || Boolean(el.closest(PAGE_CONTROLS));
  if (!view) return control || htmx ? null : { url: target, replace: false };
  if (inPlace) return null; // updated in place, inside the quick view
  return { url: target, replace: control || (htmx && pushesUrl(el)) };
}

/** Where a click on a link opens (targetOf), or null. */
export function quickTarget(link, options) {
  if (!link?.matches?.("a[href]")) return null;
  return targetOf(link, link.getAttribute("href"), options);
}

const withoutHash = (url) => String(url).split("#", 1)[0];

let started = false;

/** Starts the quick view on this page (once): every listener is on the document. */
export function initQuickView() {
  if (started || typeof document === "undefined") return;
  const modalEl = document.getElementById("modal");
  const content = document.getElementById("modal-content");
  if (!modalEl || !content) return;
  started = true;
  const dialog = modalEl.querySelector(".modal-dialog");
  const loadingHTML = content.innerHTML; // the window's own loading shapes, Close and "Loading" title
  const state = { active: false, stack: [], opener: null, loading: 0, controller: null };
  const canShow = () => Boolean(window.bootstrap?.Modal);
  const modal = () => window.bootstrap.Modal.getOrCreateInstance(modalEl);
  const shown = () => modalEl.classList.contains("show");
  // Bootstrap shows the window after its fade: what is drawn in it waits for that (a chart in a hidden box is drawn
  // 700 px wide), or for the window closing (the answer is dropped then)
  const whenShown = () =>
    shown()
      ? Promise.resolve()
      : new Promise((resolve) => {
          const done = () => {
            modalEl.removeEventListener("shown.bs.modal", done);
            modalEl.removeEventListener("hidden.bs.modal", done);
            resolve();
          };
          modalEl.addEventListener("shown.bs.modal", done);
          modalEl.addEventListener("hidden.bs.modal", done);
        });
  // an id the page (outside the window) already has
  const outsideHas = (id) =>
    [...document.querySelectorAll(`[id="${String(id).replace(/["\\]/g, "\\$&")}"]`)].some((el) => !content.contains(el));

  // ------------------------------------------------------------------ the window
  function swapIn(html) {
    if (window.htmx?.swap) window.htmx.swap(content, html, { swapStyle: "innerHTML" }); // cleans up, processes, htmx:load
    else content.innerHTML = html;
  }

  function showLoading(quiet = false) {
    // the Help assistant's panel keeps the focus inside it: it closes while the window is open
    if (document.body.classList.contains("has-help-panel")) document.querySelector("#help-panel [data-help-close]")?.click();
    if (!state.active) {
      state.active = true;
      dialog?.classList.add(...DIALOG);
    }
    if (quiet && shown() && content.querySelector(ROOT)) {
      // a filter of the window applied: the view stays (its field keeps the focus and what is typed) until the answer
      content.setAttribute("aria-busy", "true");
      return;
    }
    swapIn(loadingHTML);
    content.setAttribute("aria-busy", "true");
    if (shown()) modalEl.focus({ preventScroll: true }); // what had the focus is gone: the window says "Loading"
    modal().show();
  }

  function reset() {
    state.loading += 1; // an answer still on its way is dropped
    state.controller?.abort();
    state.active = false;
    state.stack = [];
    dialog?.classList.remove(...DIALOG);
    content.removeAttribute("aria-busy");
  }

  // A quick view opened from an Ask NeuroDB answer: a page it cannot show opens in a new tab, as the answer's link did
  // before, and the window closes over the conversation; in place only when the browser blocks the tab.
  function leave(url) {
    if (fromAnswer(state.opener) && newTab(url)) {
      state.loading += 1; // an answer still on its way is dropped
      state.controller?.abort();
      if (shown()) modal().hide();
      else whenShown().then(() => shown() && modal().hide());
      return;
    }
    reset();
    window.location.assign(url);
  }

  function focusTitle() {
    if (!state.active || !shown()) return;
    content.querySelector(`${ROOT} #modal-title`)?.focus({ preventScroll: true });
  }

  // An anchor of the view (also one renamed by ownIds): marked and scrolled to; a click on a link to it also moves the
  // focus there, as the browser moves its starting point.
  function reveal(view, hash, { focus = false } = {}) {
    let id = hash;
    try {
      id = decodeURIComponent(hash);
    } catch {
      /* kept as it is */
    }
    const target = [...view.querySelectorAll("[id]")].find((el) => el.id === id || el.getAttribute("data-quick-id") === id);
    if (!target) {
      // in a part of the view not in yet (its data-part-anchors name it): the part is scrolled to, so it loads, and
      // the anchor is revealed once it is in
      const holder = [...view.querySelectorAll("[data-part-anchors]")].find((el) => el.dataset.partAnchors.split(/\s+/).includes(id));
      if (!holder) return;
      holder.scrollIntoView({ block: "start" });
      const arrived = () => {
        if (!view.isConnected) return document.removeEventListener("htmx:load", arrived);
        if (![...view.querySelectorAll("[id]")].some((el) => el.id === id || el.getAttribute("data-quick-id") === id)) return;
        document.removeEventListener("htmx:load", arrived);
        reveal(view, hash, { focus });
      };
      document.addEventListener("htmx:load", arrived);
      setTimeout(() => document.removeEventListener("htmx:load", arrived), TIMEOUT);
      return;
    }
    target.classList.add("is-target");
    const go = () => {
      target.scrollIntoView({ block: "start" });
      if (!focus) return;
      if (!target.matches?.("a[href], button, input, select, textarea, [tabindex]")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
    };
    if (shown()) go();
    else modalEl.addEventListener("shown.bs.modal", go, { once: true });
  }

  // ------------------------------------------------------------------ the answer
  // The stylesheets of the page's head (Home's own) are added to the page once; nothing else of it runs (a refresh
  // it asks for, while a document is read, is left to the full page).
  function takeHead(root) {
    const head = root.querySelector("template[data-quick-head]");
    if (!head) return;
    for (const link of head.content.querySelectorAll('link[rel~="stylesheet"][href]')) {
      const href = new URL(link.getAttribute("href"), window.location.href).href;
      if ([...document.querySelectorAll('link[rel~="stylesheet"]')].some((el) => el.href === href)) continue;
      const added = document.createElement("link");
      added.rel = "stylesheet";
      added.href = href;
      document.head.appendChild(added);
    }
    head.remove();
  }

  // A relative address of the page ("?page=2", a form with no action) is read against the quick view's address, not
  // the page under it: written out before htmx reads it.
  function fixElement(el, base) {
    for (const name of URL_ATTRS) {
      const value = el.getAttribute?.(name);
      if (value === null || value === undefined || KEPT_AS_IS.test(value.trim())) continue;
      const url = new URL(value.trim(), base);
      el.setAttribute(name, url.pathname + url.search + url.hash);
    }
    if (el.matches?.("form") && !el.hasAttribute("action")) el.setAttribute("action", base.pathname + base.search);
  }

  function fixAddresses(root, base) {
    const selector = [...URL_ATTRS.map((name) => `[${name}]`), "form"].join(", ");
    [root, ...root.querySelectorAll(selector)].forEach((el) => fixElement(el, base));
  }

  // A GET form htmx drives in the page, pushing its address (a period picker), is the quick view's inside it: its
  // answer opens in the quick view (takeOver), never in the page under it.
  function takeOverForm(form) {
    const push = attr(form, "hx-push-url");
    if (!attr(form, "hx-get") || !push || push === "false") return;
    const trigger = attr(form, "hx-trigger") || "";
    if (!form.hasAttribute("action")) form.setAttribute("action", attr(form, "hx-get"));
    for (const name of HTMX_FORM_ATTRS) {
      form.removeAttribute(name);
      form.removeAttribute(`data-${name}`);
    }
    if (/\bchange\b/.test(trigger)) form.setAttribute("data-quick-auto", "");
  }

  // Where the person is in the view now (a filter applied: drawn again): the field with the focus (its name, the
  // letters in it and the caret; a list's box stands for its list) and which filter bars are open.
  function placeIn(old) {
    const place = { open: [], name: "", choice: null, value: null, start: null, end: null };
    if (!old) return place;
    place.open = [...old.querySelectorAll("[data-filters-collapse]")].map((wrap) => wrap.classList.contains("is-open"));
    const active = document.activeElement;
    if (!active || !old.contains(active)) return place;
    const list = active.getAttribute("name") ? null : [...old.querySelectorAll("select")].find((s) => s.tomselect?.wrapper?.contains(active));
    const field = list || active;
    place.name = field.getAttribute("name") || "";
    if (field.type === "radio" || field.type === "checkbox") place.choice = field.value;
    if (!list && TEXT_FIELD.test(field.type || "")) {
      place.value = field.value;
      place.start = field.selectionStart ?? null;
      place.end = field.selectionEnd ?? null;
    }
    return place;
  }

  function focusField(field, place) {
    if (field.hasAttribute("data-tom")) {
      // its box comes once the list is made (app.js, a moment after the swap): focused without opening the list
      let tries = 0;
      const wait = () => {
        const tom = field.tomselect;
        if (!field.isConnected || tries > 40) return;
        if (!tom) {
          tries += 1;
          setTimeout(wait, 25);
          return;
        }
        const was = tom.settings.openOnFocus;
        tom.settings.openOnFocus = false;
        tom.focus();
        setTimeout(() => {
          tom.settings.openOnFocus = was;
        }, 50);
      };
      wait();
      return;
    }
    field.focus({ preventScroll: true });
    if (place.start !== null && typeof field.setSelectionRange === "function") {
      try {
        field.setSelectionRange(place.start, place.end ?? place.start);
      } catch {
        /* not a field with a caret */
      }
    }
  }

  // The same field in the new view: focused again, with the letters typed while the answer came (`asked`: the
  // address drawn; what differs from what it asked is applied in turn, never what the page wrote back its own way).
  function restore(view, place, asked) {
    if (!place.name) return false;
    const field = [...view.querySelectorAll("[name]")].find(
      (el) => el.getAttribute("name") === place.name && (place.choice === null || el.value === place.choice),
    );
    if (!field) return false;
    if (place.value !== null && place.value !== field.value && place.value !== (asked.get(place.name) ?? "")) {
      field.value = place.value;
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    focusField(field, place);
    return true;
  }

  function draw(html, url, fromForm = false) {
    const box = document.createElement("template");
    box.innerHTML = html;
    const root = box.content.querySelector(ROOT);
    if (!root) return false;
    const base = new URL(root.dataset.quickUrl || withoutHash(url), window.location.href);
    takeHead(root);
    fixAddresses(root, base);
    ownIds(root, outsideHas);
    root.querySelectorAll("form").forEach(takeOverForm);
    const place = fromForm ? placeIn(content.querySelector(ROOT)) : null;
    if (place) root.querySelectorAll("[data-filters-collapse]").forEach((wrap, i) => place.open[i] && wrap.classList.add("is-open"));
    content.removeAttribute("aria-busy");
    swapIn(root.outerHTML);
    const view = content.querySelector(ROOT);
    if (!view) return false;
    const back = view.querySelector("[data-quick-back]");
    if (back) back.hidden = state.stack.length < 2;
    const body = view.querySelector(".modal-body");
    if (body) body.scrollTop = 0;
    const drawn = new URL(url, window.location.href);
    const hash = drawn.hash.slice(1);
    if (hash) reveal(view, hash);
    if (!(place && restore(view, place, drawn.searchParams))) focusTitle();
    return true;
  }

  async function load(url, fromForm = false) {
    const token = ++state.loading;
    state.controller?.abort();
    const controller = (state.controller = new AbortController());
    let late = false;
    const timer = setTimeout(() => {
      late = true;
      controller.abort();
    }, TIMEOUT);
    showLoading(fromForm);
    try {
      const response = await fetch(withoutHash(url), {
        headers: { [ASK_HEADER]: "1", Accept: "text/html" },
        credentials: "same-origin",
        redirect: "manual",
        signal: controller.signal,
      });
      if (token !== state.loading) return;
      if (response.type === "opaqueredirect" || response.status !== 200 || response.headers.get(ANSWER_HEADER) !== "1") {
        leave(url);
        return;
      }
      const html = await response.text();
      if (token !== state.loading) return;
      await whenShown();
      if (token !== state.loading) return; // a newer quick view, or the window closed meanwhile
      if (!draw(html, url, fromForm)) leave(url);
    } catch (err) {
      if (token !== state.loading) return; // a newer quick view, or the window closed
      if (err?.name === "AbortError" && !late) return;
      leave(url);
    } finally {
      clearTimeout(timer);
    }
  }

  function open(url, { replace = false, opener = null, fromForm = false } = {}) {
    if (!state.active) {
      state.stack = [];
      state.opener = opener;
    }
    if (replace && state.stack.length) state.stack[state.stack.length - 1] = url;
    else state.stack.push(url);
    return load(url, fromForm);
  }

  function back() {
    if (state.stack.length < 2) return;
    state.stack.pop();
    load(state.stack[state.stack.length - 1]);
  }

  // ------------------------------------------------------------------ clicks and forms
  // Capture: before htmx's own listener on a link inside a quick view (it never sees the click then).
  document.addEventListener(
    "click",
    (e) => {
      if (!plainClick(e)) return;
      const backButton = e.target?.closest?.("[data-quick-back]");
      if (backButton && backButton.closest(ROOT)) {
        e.preventDefault();
        back();
        return;
      }
      const link = e.target?.closest?.("a[href]");
      const found = link && quickTarget(link);
      if (!found || !canShow()) return;
      e.preventDefault();
      if (link.closest(ROOT)) e.stopPropagation?.();
      if (found.anchor) reveal(link.closest(ROOT), found.anchor, { focus: true });
      else open(found.url, { replace: found.replace, opener: link });
    },
    true,
  );

  // A row that opens a page (app.js, tr[data-href]) asks first.
  document.addEventListener("nd:quick-open", (e) => {
    const { url, event } = e.detail || {};
    if (!url || (event && !plainClick(event)) || !canShow()) return;
    const found = targetOf(e.target, url);
    if (!found?.url) return;
    e.preventDefault();
    open(found.url, { replace: found.replace, opener: e.target.querySelector?.("a[href]") || null });
  });

  const quickForm = (form) =>
    Boolean(form?.matches?.("form") && form.closest(ROOT) && state.active) &&
    (form.getAttribute("method") || "get").toLowerCase() === "get" &&
    !form.hasAttribute("target") &&
    !form.closest("[data-no-quick]");
  const autoForm = (form) => quickForm(form) && (form.hasAttribute("data-filter-bar") || form.hasAttribute("data-quick-auto"));

  function submitForm(form, submitter) {
    const base = new URL(form.closest(ROOT).dataset.quickUrl, window.location.href);
    const action = new URL(form.getAttribute("action") || base.pathname, base);
    const params = filterParams(form, submitter);
    const url = action.pathname + (params.size ? `?${params}` : "");
    const found = action.origin === base.origin && !isDownload(new URL(url, base)) && !skipList().some((p) => action.pathname.startsWith(p));
    if (found) open(url, { replace: action.pathname === base.pathname, fromForm: true }); // a filter of the same page: in its place
    else window.location.assign(url);
  }

  document.addEventListener(
    "submit",
    (e) => {
      const form = e.target;
      if (!quickForm(form) || e.defaultPrevented) return;
      e.preventDefault();
      e.stopPropagation?.(); // htmx's own handler on the form never runs
      submitForm(form, e.submitter);
    },
    true,
  );

  // A filter bar in a quick view applies as on the page: a change, a search typed, a list of several values once it
  // closes (one request for all the values picked there).
  const timers = new WeakMap();
  const held = new WeakSet();
  const later = (form, wait) => {
    clearTimeout(timers.get(form));
    timers.set(
      form,
      setTimeout(() => form.isConnected && autoForm(form) && submitForm(form), wait),
    );
  };
  document.addEventListener("change", (e) => {
    const form = e.target?.closest?.("form");
    if (!autoForm(form)) return;
    if (e.target.multiple && e.target.tomselect?.isOpen) held.add(form);
    else later(form, 350);
  });
  document.addEventListener("nd:listclose", (e) => {
    const form = e.target?.closest?.("form");
    if (!form || !held.has(form)) return;
    held.delete(form);
    if (autoForm(form)) later(form, 350);
  });
  document.addEventListener("input", (e) => {
    if (e.target?.type !== "search") return;
    const form = e.target.closest("form");
    if (autoForm(form)) later(form, 500);
  });

  // ------------------------------------------------------------------ htmx inside a quick view
  // What comes later inside a quick view (a part, a table's next page) reads its relative addresses against the quick
  // view's, as the first answer did (before htmx binds the element, then the plain links once it is in).
  const baseOf = (el) => {
    const view = el?.closest?.(ROOT);
    return view?.dataset.quickUrl ? new URL(view.dataset.quickUrl, window.location.href) : null;
  };
  document.addEventListener("htmx:beforeProcessNode", (e) => {
    const el = e.target;
    const base = baseOf(el);
    if (!base) return;
    fixElement(el, base);
    if (el.matches?.("form")) takeOverForm(el);
  });
  document.addEventListener("htmx:load", (e) => {
    const el = e.detail?.elt || e.target;
    const base = baseOf(el);
    if (base && el.querySelectorAll) {
      fixAddresses(el, base);
      ownIds(el, outsideHas); // a part or a table's next page brings the page's ids too
    }
  });

  // A request made inside a quick view never writes the address bar (htmx's hx-push-url): the history is off for it.
  const inView = new WeakSet();
  let historyWas = null;
  document.addEventListener("htmx:beforeRequest", (e) => {
    const elt = e.detail?.elt;
    if (e.detail?.xhr && elt?.closest?.(ROOT)) inView.add(e.detail.xhr);
  });
  document.addEventListener("htmx:beforeHistoryUpdate", (e) => {
    if (!window.htmx || !e.detail?.xhr || !inView.has(e.detail.xhr)) return;
    historyWas = window.htmx.config.historyEnabled;
    window.htmx.config.historyEnabled = false;
  });
  const restoreHistory = () => {
    if (historyWas === null || !window.htmx) return;
    window.htmx.config.historyEnabled = historyWas;
    historyWas = null;
  };
  document.addEventListener("htmx:pushedIntoHistory", restoreHistory);
  document.addEventListener("htmx:replacedInHistory", restoreHistory);

  // A partial swapped into the shared window by htmx (an existing pop-up) takes the quick view's place.
  document.addEventListener("htmx:beforeSwap", (e) => {
    if (state.active && e.detail?.target === content && e.detail.shouldSwap) reset();
  });

  // ------------------------------------------------------------------ closing
  modalEl.addEventListener("shown.bs.modal", focusTitle);
  modalEl.addEventListener("hidden.bs.modal", () => {
    const wasQuick = state.active;
    const opener = state.opener;
    state.opener = null;
    reset();
    if (wasQuick) swapIn(loadingHTML); // the window as it was: its parts and charts are cleaned up
    if (opener?.isConnected) opener.focus();
  });
}

if (typeof document !== "undefined" && document.querySelector?.('meta[name="nd-quick-skip"]')) initQuickView();
