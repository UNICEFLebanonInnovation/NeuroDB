// NeuroDB application shell: theme, command palette, HTMX glue, tables, filters and page modules.
import { asset, debounce, download, isHelpShortcut, loadScript, loadStyle, tableRows, toast, toCSV, trapFocus, csrfToken } from "./lib.js";

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

// ------------------------------------------------------------------ Help assistant (Ctrl/Cmd + Shift + H)
// The panel's frame is on every page (help/_panel.html); its body is loaded the first time it opens, then
// started by initModules like any other chat (data-module="ask"). While it is open the focus stays inside
// it; Esc, the × or the shortcut again closes it and gives the focus back to where it was.
function initHelp() {
  const panel = $("#help-panel");
  if (!panel) return;
  const trigger = $("#help-trigger");
  const body = $("[data-help-body]", panel);
  let loaded = null;
  let before = null;
  const isOpen = () => !panel.hidden;
  const focusInput = () => ($("[data-ask-part='input']", panel) || $("[data-help-close]", panel))?.focus();
  const load = () => {
    if (!loaded) {
      loaded = window.htmx
        ? window.htmx.ajax("GET", panel.dataset.src, { target: body, swap: "innerHTML" })
        : Promise.reject(new Error("htmx is not loaded"));
      loaded.catch(() => {
        loaded = null;
        body.innerHTML = '<p class="small m-3" role="alert">The Help assistant could not be loaded. <a href="/help/">Open the help pages</a>.</p>';
      });
    }
    return loaded;
  };
  const open = () => {
    if (isOpen()) return;
    before = document.activeElement;
    panel.hidden = false;
    trigger?.setAttribute("aria-expanded", "true");
    document.body.classList.add("has-help-panel");
    load().then(focusInput, () => {});
    focusInput();
  };
  const close = () => {
    if (!isOpen()) return;
    panel.hidden = true;
    trigger?.setAttribute("aria-expanded", "false");
    document.body.classList.remove("has-help-panel");
    (before && document.contains(before) ? before : trigger)?.focus();
  };
  trigger?.addEventListener("click", () => (isOpen() ? close() : open()));
  $("[data-help-close]", panel)?.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (isHelpShortcut(e)) {
      e.preventDefault();
      if (isOpen()) close();
      else open();
    } else if (e.key === "Escape" && isOpen()) {
      e.preventDefault();
      close();
    } else if (e.key === "Tab" && isOpen()) {
      const next = trapFocus(panel, e);
      if (next) {
        e.preventDefault();
        next.focus();
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
    // A field marked data-keep-empty keeps its key when nothing is chosen ("section=": every section,
    // which is not the same as no section key at all, where a page applies the user's default)
    const keep = new Set($$("[data-keep-empty]", form).map((el) => el.name));
    for (const [key, value] of [...params.entries()]) if (!value && !keep.has(key)) params.delete(key);
    for (const name of keep) {
      const values = params.getAll(name).filter(Boolean);
      params.delete(name);
      if (values.length) values.forEach((v) => params.append(name, v));
      else params.append(name, "");
    }
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

// ------------------------------------------------------------------ filter bars folded on phones
// A wrapper with data-filters-collapse gets a "Filters" button; under 768px (CSS) its filter bar stays
// folded until the button opens it, so the page's figures come first. Without JS nothing is folded.
function initFiltersCollapse(root) {
  $$("[data-filters-collapse]", root).forEach((wrap, index) => {
    const form = $("form.filter-bar", wrap);
    if (wrap.dataset.ndBound || !form) return;
    wrap.dataset.ndBound = "1";
    form.id ||= `filter-bar-${index + 1}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-outline-secondary btn-sm filters-toggle";
    button.setAttribute("aria-controls", form.id);
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-filter"></use></svg><span>Filters</span>';
    button.addEventListener("click", () => {
      const open = wrap.classList.toggle("is-open");
      button.setAttribute("aria-expanded", String(open));
    });
    form.before(button);
    wrap.classList.add("is-collapsible");
  });
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
  $$("[data-copy-target]", root).forEach((btn) => {
    if (btn.dataset.ndBound) return;
    btn.dataset.ndBound = "1";
    btn.addEventListener("click", async () => {
      const el = $(btn.dataset.copyTarget);
      if (!el) return;
      try {
        await navigator.clipboard.writeText(el.textContent || "");
        toast("Copied — paste it into the minutes.");
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
const MODULES = {
  charts: "charts",
  pivot: "pivotModule",
  map: "mapModule",
  pdmap: "pdMapModule",
  edumap: "eduMapModule",
  ask: "askModule",
};

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

// ------------------------------------------------------------------ links that follow the page's filter
// A link with data-current-query (an export menu in the page header) takes the filter of the address bar
// when it is clicked: a filter bar driven by HTMX changes the address, not the links drawn with the page.
// The attribute's value is added after the filter ("export=csv"); the page's own keys (tab, page...) are not.
const PAGE_ONLY_KEYS = ["tab", "page", "sort", "places", "entity_kind", "entity_all", "rule_trends", "pd_scope", "visit"];
function followQuery(event) {
  const link = event.target.closest?.("a[data-current-query]");
  if (!link) return;
  const params = new URLSearchParams(window.location.search);
  if (!params.size) return; // the first load: the link already carries the filter shown (a default section)
  PAGE_ONLY_KEYS.forEach((key) => params.delete(key));
  for (const [key, value] of new URLSearchParams(link.dataset.currentQuery || "")) params.set(key, value);
  const url = new URL(link.getAttribute("href"), window.location.href);
  url.search = params.toString();
  link.href = url.toString();
}

// ------------------------------------------------------------------ print pages
// A button with data-autoprint prints the page; data-autoprint="load" also opens the print dialog once the
// page and its charts are drawn (the printable reports: staff choose "Save as PDF").
function initAutoprint(root) {
  $$("[data-autoprint]", root).forEach((btn) => {
    if (btn.dataset.ndBound) return;
    btn.dataset.ndBound = "1";
    btn.addEventListener("click", () => window.print());
    if (btn.dataset.autoprint === "load" && root === document) {
      const open = () => window.setTimeout(() => window.print(), 1500);
      if (document.readyState === "complete") open();
      else window.addEventListener("load", open, { once: true });
    }
  });
}

function enhance(root = document) {
  $$("form[data-filter-bar]", root).forEach(initFilterBar);
  initFiltersCollapse(root);
  initTables(root);
  initSelects(root);
  initModules(root);
  initAutoprint(root);
}

initTheme();
initSearch();
initHelp();
initHtmx();
document.addEventListener("click", followQuery);
document.addEventListener("auxclick", followQuery);
enhance(document);
