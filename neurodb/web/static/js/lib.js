// Shared helpers for NeuroDB page modules. No framework: small functions over the DOM.

const assets = (() => {
  try {
    return JSON.parse(document.getElementById("asset-urls")?.textContent || "{}");
  } catch {
    return {};
  }
})();

export function asset(name) {
  const url = assets[name];
  if (!url) throw new Error(`Unknown asset: ${name}`);
  return url;
}

const pending = new Map();

/** Load a classic script once (vendor UMD bundles). Resolves when it has executed. */
export function loadScript(name) {
  const url = asset(name);
  if (!pending.has(url)) {
    pending.set(
      url,
      new Promise((resolve, reject) => {
        const el = document.createElement("script");
        el.src = url;
        el.async = false; // keep execution order for dependent bundles
        el.onload = () => resolve();
        el.onerror = () => reject(new Error(`Could not load ${name}`));
        document.head.appendChild(el);
      }),
    );
  }
  return pending.get(url);
}

/** Load scripts strictly in order (jQuery -> jQuery UI -> pivottable ...). */
export async function loadScripts(...names) {
  for (const name of names) await loadScript(name);
}

export function loadStyle(name) {
  const url = asset(name);
  if (!pending.has(url)) {
    const el = document.createElement("link");
    el.rel = "stylesheet";
    el.href = url;
    document.head.appendChild(el);
    pending.set(url, Promise.resolve());
  }
  return pending.get(url);
}

export function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

export async function fetchJSON(url, { method = "GET", body, signal } = {}) {
  const headers = { Accept: "application/json", "X-Requested-With": "fetch" };
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    signal,
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail || `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return data;
}

// "#id" or [id="id"]: a selector of one id, which a quick view may have renamed (quickview.js ownIds)
const ONE_ID = /^(?:#([\w-]+)|\[id="([^"\\]+)"\])$/;

/** The element `selector` names for `near`: the one in the quick view `near` is in first (an id the page also has
 * is renamed there and keeps its own as data-quick-id: quickview.js), else the page's own. */
export function scopedQuery(selector, near) {
  if (!selector) return null;
  const view = near?.closest?.("[data-quick-view]");
  if (view) {
    const own = view.querySelector(selector);
    if (own) return own;
    const id = ONE_ID.exec(String(selector).trim());
    const renamed = id && view.querySelector(`[data-quick-id="${id[1] || id[2]}"]`);
    if (renamed) return renamed;
  }
  return document.querySelector(selector);
}

/** The element with this id for `near` (scopedQuery). */
export function byId(id, near) {
  return id ? scopedQuery(`[id="${String(id).replace(/["\\]/g, "\\$&")}"]`, near) : null;
}

/** The JSON data block with this id, the one of the quick view `near` is in first (scopedQuery). */
export function readJSON(id, near) {
  const el = byId(id, near);
  if (!el) return null;
  try {
    return JSON.parse(el.textContent);
  } catch {
    return null;
  }
}

export function toast(message, kind = "info", timeout = 4500) {
  const region = document.getElementById("toasts");
  if (!region) return;
  const el = document.createElement("div");
  el.className = `nd-toast nd-toast--${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  region.appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
export function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? numberFormat.format(n) : String(value);
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function isDark() {
  return document.documentElement.getAttribute("data-bs-theme") === "dark";
}

/** Plain-text table extraction used by the copy/CSV buttons and the pivot export. A cell marked
 * data-export="no" (e.g. the team names of Monitoring insights) is never copied or exported; a cell marked
 * data-export="only" and hidden (e.g. the four columns Partner monitoring's grid no longer shows) is only
 * copied and exported, its value in data-value. */
export function tableRows(table) {
  return [...table.querySelectorAll("tr")].map((tr) =>
    [...tr.children]
      .filter((cell) => cell.dataset.export !== "no")
      .map((cell) => (cell.dataset.value ?? cell.innerText).replace(/\s+/g, " ").trim()),
  );
}

export function toCSV(rows) {
  const esc = (v) => (/[",\n;]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return rows.map((r) => r.map((v) => esc(String(v ?? ""))).join(",")).join("\r\n");
}

export function download(filename, text, type = "text/csv;charset=utf-8") {
  const blob = new Blob(["﻿", text], { type });
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The query a filter form asks for: its fields (and the button that sent it), the empty ones left out, except that a
 * field marked data-keep-empty keeps its key when nothing is chosen ("section=": every section, which is not the
 * same as no section key at all, where a page applies the person's default). app.js and quickview.js send it. */
export function filterParams(form, submitter) {
  const params = new URLSearchParams(submitter ? new FormData(form, submitter) : new FormData(form));
  const keep = new Set([...form.querySelectorAll("[data-keep-empty]")].map((el) => el.name));
  for (const [key, value] of [...params.entries()]) if (!value && !keep.has(key)) params.delete(key);
  for (const name of keep) {
    const values = params.getAll(name).filter(Boolean);
    params.delete(name);
    if (values.length) values.forEach((v) => params.append(name, v));
    else params.append(name, "");
  }
  return params;
}

export function debounce(fn, wait = 250) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}

export function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// The Help assistant's shortcut: Ctrl+Shift+H, or Cmd+Shift+H on a Mac (Alt is left to the browser and
// the system). e.code covers keyboards whose H key gives another letter.
export function isHelpShortcut(e) {
  if (!e || !e.shiftKey || e.altKey || !(e.ctrlKey || e.metaKey) || e.isComposing) return false;
  return String(e.key || "").toLowerCase() === "h" || e.code === "KeyH";
}

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Keep Tab and Shift+Tab inside ``container`` (a dialog open over the page): from the last focusable
// element Tab goes to the first, from the first Shift+Tab goes to the last. Returns the element to focus,
// or null when the browser's own move stays inside.
export function trapFocus(container, e) {
  if (!container || e.key !== "Tab") return null;
  const items = [...container.querySelectorAll(FOCUSABLE)].filter((el) => !el.hidden && !el.closest("[hidden]"));
  if (!items.length) return null;
  const first = items[0];
  const last = items[items.length - 1];
  const active = globalThis.document?.activeElement;
  const inside = typeof container.contains === "function" ? container.contains(active) : false;
  if (e.shiftKey && (active === first || !inside)) return last;
  if (!e.shiftKey && (active === last || !inside)) return first;
  return null;
}

// ------------------------------------------------------------------ windows (Bootstrap modals)
/** The window open over the page (the quick view or a pop-up), or null. */
export function openDialog() {
  return globalThis.document?.querySelector?.(".modal.show") || null;
}

/** Runs `fn` once no window is open over the page: at once, or after closing the one that is (its focus trap would
 * keep the focus from a panel opened beside it, the Help assistant's). */
export function afterDialogs(fn) {
  const dialog = openDialog();
  const Modal = globalThis.window?.bootstrap?.Modal;
  if (!dialog || !Modal) {
    fn();
    return;
  }
  dialog.addEventListener("hidden.bs.modal", () => fn(), { once: true });
  Modal.getOrCreateInstance(dialog).hide();
}

/** Whether a chat may move the focus back to its question box (an answer ended): only while no window is open and
 * the focus is still in the conversation `root` or nowhere, never away from what the person moved on to. */
export function mayTakeFocus(root) {
  if (openDialog()) return false;
  const active = globalThis.document?.activeElement;
  return !active || active === globalThis.document.body || !active.isConnected || Boolean(root?.contains?.(active));
}

// ------------------------------------------------------------------ tab bars that scroll sideways
/** The scroll offset that brings an item of a sideways-scrolling bar into sight (8 px from the edge it was past), or
 * null when it is in sight already. The rectangles are the bar's and the item's on screen. */
export function revealInBar(bar, scrollLeft, item, margin = 8) {
  if (!bar || !item) return null;
  if (item.left >= bar.left && item.right <= bar.right) return null;
  if (item.left < bar.left || item.width > bar.width - 2 * margin) return Math.max(0, scrollLeft - (bar.left - item.left) - margin);
  return scrollLeft + (item.right - bar.right) + margin;
}
