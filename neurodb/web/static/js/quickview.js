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
// files, ?export=); links with a target or data-no-quick (on the link or around it), the window's own header, Bootstrap
// toggles; other sites, mailto and hash links; the administration, sign-in and donor addresses (<meta name=
// "nd-quick-skip">, layouts/page.html). On a page, also the links that work the page itself: breadcrumbs, tab bars,
// filter bars and their chips, sort, pagination, page actions, empty states' reset links, links to the page itself,
// and links htmx handles (an existing pop-up, a filter). Inside a quick view those open in the same quick view, in its
// place (no Back), and a GET form there (a filter bar) opens its result there too; a link htmx updates in place inside
// it keeps doing so.
//
// Everything is delegated from the document, so it covers what comes later: parts, Ask NeuroDB's answers, partials.
import { filterParams } from "./lib.js";

export const ASK_HEADER = "X-NeuroDB-Quick";
export const ANSWER_HEADER = "X-NeuroDB-Quick-View";
const ROOT = "[data-quick-view]";
const AREAS = "#main, #modal-content";
const NEVER = "[data-no-quick], [download], [target], [data-bs-toggle], [data-bs-dismiss], [data-page-link], .modal-header, .skip-link";
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

/**
 * Where a click on `el` (a link, or a row with data-href) going to `raw` opens: { url, replace } for a quick view
 * (`replace`: in place of the quick view it is in, with no Back), or null for the browser's own way. `page`: the
 * page's address; a relative address in a quick view is read against the quick view's own (data-quick-url).
 */
export function targetOf(el, raw, { page = window.location.href, skip = skipList() } = {}) {
  if (!el?.closest || !el.closest(AREAS) || el.closest(NEVER)) return null;
  const address = String(raw ?? "").trim();
  if (!address || address.startsWith("#")) return null;
  const view = el.closest(ROOT);
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
  if (url.pathname === here.pathname && url.search === here.search) return null; // this very page (an anchor of it)
  const target = url.pathname + url.search + url.hash;
  const control = url.pathname === here.pathname || Boolean(el.closest(PAGE_CONTROLS));
  const htmx = Boolean(el.matches?.(HTMX));
  if (!view) return control || htmx ? null : { url: target, replace: false };
  if (htmx && !leavesView(el)) return null; // updated in place, inside the quick view
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

  // ------------------------------------------------------------------ the window
  function swapIn(html) {
    if (window.htmx?.swap) window.htmx.swap(content, html, { swapStyle: "innerHTML" }); // cleans up, processes, htmx:load
    else content.innerHTML = html;
  }

  function showLoading() {
    // the Help assistant's panel keeps the focus inside it: it closes while the window is open
    if (document.body.classList.contains("has-help-panel")) document.querySelector("#help-panel [data-help-close]")?.click();
    if (!state.active) {
      state.active = true;
      dialog?.classList.add(...DIALOG);
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

  function leave(url) {
    reset();
    window.location.assign(url);
  }

  function focusTitle() {
    if (!state.active || !shown()) return;
    content.querySelector(`${ROOT} #modal-title`)?.focus({ preventScroll: true });
  }

  function reveal(view, hash) {
    let id = hash;
    try {
      id = decodeURIComponent(hash);
    } catch {
      /* kept as it is */
    }
    const target = [...view.querySelectorAll("[id]")].find((el) => el.id === id);
    if (!target) return;
    target.classList.add("is-target");
    const go = () => target.scrollIntoView({ block: "start" });
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

  function draw(html, url) {
    const box = document.createElement("template");
    box.innerHTML = html;
    const root = box.content.querySelector(ROOT);
    if (!root) return false;
    const base = new URL(root.dataset.quickUrl || withoutHash(url), window.location.href);
    takeHead(root);
    fixAddresses(root, base);
    root.querySelectorAll("form").forEach(takeOverForm);
    content.removeAttribute("aria-busy");
    swapIn(root.outerHTML);
    const view = content.querySelector(ROOT);
    if (!view) return false;
    const back = view.querySelector("[data-quick-back]");
    if (back) back.hidden = state.stack.length < 2;
    const body = view.querySelector(".modal-body");
    if (body) body.scrollTop = 0;
    const hash = new URL(url, window.location.href).hash.slice(1);
    if (hash) reveal(view, hash);
    focusTitle();
    return true;
  }

  async function load(url) {
    const token = ++state.loading;
    state.controller?.abort();
    const controller = (state.controller = new AbortController());
    let late = false;
    const timer = setTimeout(() => {
      late = true;
      controller.abort();
    }, TIMEOUT);
    showLoading();
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
      if (!draw(html, url)) leave(url);
    } catch (err) {
      if (token !== state.loading) return; // a newer quick view, or the window closed
      if (err?.name === "AbortError" && !late) return;
      leave(url);
    } finally {
      clearTimeout(timer);
    }
  }

  function open(url, { replace = false, opener = null } = {}) {
    if (!state.active) {
      state.stack = [];
      state.opener = opener;
    }
    if (replace && state.stack.length) state.stack[state.stack.length - 1] = url;
    else state.stack.push(url);
    return load(url);
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
      open(found.url, { replace: found.replace, opener: link });
    },
    true,
  );

  // A row that opens a page (app.js, tr[data-href]) asks first.
  document.addEventListener("nd:quick-open", (e) => {
    const { url, event } = e.detail || {};
    if (!url || (event && !plainClick(event)) || !canShow()) return;
    const found = targetOf(e.target, url);
    if (!found) return;
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
    if (found) open(url, { replace: action.pathname === base.pathname }); // a filter of the same page: in its place
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
    if (base && el.querySelectorAll) fixAddresses(el, base);
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
