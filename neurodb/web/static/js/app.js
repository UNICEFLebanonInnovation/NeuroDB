// NeuroDB application shell: theme, command palette, HTMX glue, tables, filters and page modules.
import { asset, debounce, download, loadScript, loadStyle, tableRows, toast, toCSV, csrfToken } from "./lib.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ------------------------------------------------------------------ theme
function setTheme(theme) {
  document.documentElement.setAttribute("data-bs-theme", theme);
  try {
    localStorage.setItem("neurodb-theme", theme);
  } catch {
    /* storage unavailable */
  }
  $("#theme-toggle")?.setAttribute("aria-pressed", String(theme === "dark"));
  document.dispatchEvent(new CustomEvent("nd:themechange", { detail: { theme } }));
}

function initTheme() {
  const button = $("#theme-toggle");
  if (!button) return;
  button.setAttribute("aria-pressed", String(document.documentElement.getAttribute("data-bs-theme") === "dark"));
  button.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
    setTheme(next);
  });
}

// ------------------------------------------------------------------ command palette (Ctrl/Cmd + K)
function initSearch() {
  const modalEl = $("#search-modal");
  if (!modalEl || !window.bootstrap) return;
  const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
  const input = $("#search-input");
  const results = $("#search-results");
  const open = () => modal.show();

  $("#search-trigger")?.addEventListener("click", open);
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "") || document.activeElement?.isContentEditable;
    if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) {
      e.preventDefault();
      open();
    }
  });
  modalEl.addEventListener("shown.bs.modal", () => {
    input?.focus();
    input?.select();
  });

  const items = () => $$(".search-item", results);
  const move = (delta) => {
    const list = items();
    if (!list.length) return;
    const current = list.findIndex((el) => el.classList.contains("is-active"));
    const next = (current + delta + list.length) % list.length;
    list.forEach((el, i) => {
      el.classList.toggle("is-active", i === next);
      el.setAttribute("aria-selected", String(i === next));
    });
    list[next].scrollIntoView({ block: "nearest" });
  };
  input?.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter") {
      const active = $(".search-item.is-active", results) || items()[0];
      if (active) {
        e.preventDefault();
        window.location.assign(active.href);
      }
    }
  });
}

// ------------------------------------------------------------------ HTMX glue
function initHtmx() {
  document.body.addEventListener("htmx:configRequest", (e) => {
    e.detail.headers["X-CSRFToken"] = csrfToken();
  });
  document.body.addEventListener("htmx:responseError", (e) => {
    const status = e.detail.xhr?.status;
    toast(status === 403 ? "You do not have access to this." : `The page could not be updated (${status || "error"}).`, "error");
  });
  document.body.addEventListener("htmx:sendError", () => toast("Network error — check your connection.", "error"));
  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target?.id === "modal-content" && window.bootstrap) {
      window.bootstrap.Modal.getOrCreateInstance($("#modal")).show();
    }
  });
  // New content (partials, modals) gets the same enhancements as the first page load.
  document.body.addEventListener("htmx:load", (e) => enhance(e.detail.elt));
}

// ------------------------------------------------------------------ filter bars driven by HTMX
function initFilterBar(form) {
  if (form.dataset.ndBound) return;
  form.dataset.ndBound = "1";
  const target = form.dataset.hxTarget;
  if (!target || !window.htmx) return;
  const submit = () => {
    const params = new URLSearchParams(new FormData(form));
    for (const [key, value] of [...params.entries()]) if (!value) params.delete(key);
    const url = `${form.getAttribute("action") || window.location.pathname}${params.size ? `?${params}` : ""}`;
    window.htmx.ajax("GET", url, { target, swap: "innerHTML" }).then(() => {
      window.history.replaceState({}, "", url);
    });
  };
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  const auto = debounce(submit, 350);
  form.addEventListener("change", auto);
  $$('input[type="search"]', form).forEach((el) => el.addEventListener("input", auto));
}

// ------------------------------------------------------------------ tables
function initTables(root) {
  $$("[data-table-copy]", root).forEach((btn) => {
    if (btn.dataset.ndBound) return;
    btn.dataset.ndBound = "1";
    btn.addEventListener("click", async () => {
      const table = $(btn.dataset.tableTarget);
      if (!table) return;
      const text = tableRows(table).map((r) => r.join("\t")).join("\n");
      try {
        await navigator.clipboard.writeText(text);
        toast("Table copied — paste it into Excel or a document.");
      } catch {
        toast("Copy is not available in this browser.", "error");
      }
    });
  });
  $$("[data-table-csv]", root).forEach((btn) => {
    if (btn.dataset.ndBound) return;
    btn.dataset.ndBound = "1";
    btn.addEventListener("click", () => {
      const table = $(btn.dataset.tableTarget);
      if (table) download(`${btn.dataset.filename || "table"}.csv`, toCSV(tableRows(table)));
    });
  });
  $$("tr[data-href]", root).forEach((tr) => {
    if (tr.dataset.ndBound) return;
    tr.dataset.ndBound = "1";
    tr.classList.add("row-link");
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a, button, input, select, label")) return;
      window.location.assign(tr.dataset.href);
    });
  });
  // Client-side sort on any table with data-sortable: click a header to toggle asc/desc.
  $$("table[data-sortable]", root).forEach((table) => {
    if (table.dataset.ndBound) return;
    table.dataset.ndBound = "1";
    $$("thead th", table).forEach((th, index) => {
      if (th.dataset.nosort !== undefined) return;
      th.setAttribute("role", "button");
      th.tabIndex = 0;
      const sort = () => {
        const dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        $$("thead th", table).forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", dir);
        const body = table.tBodies[0];
        const rows = [...body.rows];
        const key = (row) => {
          const cell = row.cells[index];
          const raw = cell?.dataset.value ?? cell?.innerText ?? "";
          const n = Number(String(raw).replace(/[,%\s]/g, ""));
          return raw !== "" && Number.isFinite(n) ? n : String(raw).toLowerCase();
        };
        rows.sort((a, b) => {
          const x = key(a);
          const y = key(b);
          const r = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
          return dir === "ascending" ? r : -r;
        });
        rows.forEach((r) => body.appendChild(r));
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), sort()));
    });
  });
}

// ------------------------------------------------------------------ multi-selects
async function initSelects(root) {
  const selects = $$("select[data-tom]", root).filter((el) => !el.tomselect);
  if (!selects.length) return;
  try {
    loadStyle("tomSelectCss");
    await loadScript("tomSelect");
  } catch {
    return; // plain <select multiple> still works
  }
  selects.forEach((el) => {
    if (el.tomselect) return;
    // eslint-disable-next-line no-new
    new window.TomSelect(el, {
      plugins: ["remove_button", "clear_button"],
      maxOptions: 500,
      hidePlaceholder: true,
      onChange: () => el.dispatchEvent(new Event("change", { bubbles: true })),
    });
  });
}

// ------------------------------------------------------------------ page modules
const MODULES = { charts: "charts", pivot: "pivotModule", map: "mapModule" };

async function initModules(root) {
  const elements = [...(root.matches?.("[data-module]") ? [root] : []), ...$$("[data-module]", root)];
  for (const el of elements) {
    if (el.dataset.ndBound) continue;
    el.dataset.ndBound = "1";
    const key = MODULES[el.dataset.module];
    if (!key) continue;
    try {
      const mod = await import(asset(key));
      await mod.init(el);
    } catch (err) {
      console.error(err);
      el.innerHTML = "";
      const box = document.createElement("div");
      box.className = "state state--error";
      box.setAttribute("role", "alert");
      box.innerHTML = '<p class="state__title">This view could not be loaded.</p>';
      const msg = document.createElement("p");
      msg.className = "state__message";
      msg.textContent = err?.message || String(err);
      box.appendChild(msg);
      el.appendChild(box);
    }
  }
}

function enhance(root = document) {
  $$("form[data-filter-bar]", root).forEach(initFilterBar);
  initTables(root);
  initSelects(root);
  initModules(root);
  $$("[data-autoprint]", root).forEach((btn) => btn.addEventListener("click", () => window.print()));
}

initTheme();
initSearch();
initHtmx();
enhance(document);
