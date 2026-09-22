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

export function readJSON(id) {
  const el = document.getElementById(id);
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

/** Plain-text table extraction used by the copy/CSV buttons and the pivot export. */
export function tableRows(table) {
  return [...table.querySelectorAll("tr")].map((tr) =>
    [...tr.children].map((cell) => (cell.dataset.value ?? cell.innerText).replace(/\s+/g, " ").trim()),
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
