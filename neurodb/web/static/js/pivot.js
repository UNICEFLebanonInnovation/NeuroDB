// Analytical view: PivotTable.js over the flat ActivityInfo rows, with presets and saved views.
// Markup contract (templates/reports/_pivot.html): a [data-module="pivot"] root holding the
// toolbar controls and a [data-pivot-output] container; config in #pivot-config, views in #saved-views.
import { download, escapeHTML, fetchJSON, fmt, loadScripts, loadStyle, readJSON, tableRows, toast, toCSV } from "./lib.js";

const PRESETS = {
  month: { rows: ["master_indicator"], cols: ["month"] },
  governorate: { rows: ["master_indicator"], cols: ["governorate"] },
  partner: { rows: ["partner"], cols: ["master_indicator"] },
  sub: { rows: ["master_indicator", "sub_indicator"], cols: ["month"] },
  demographics: { rows: ["master_indicator"], cols: ["gender", "nationality"] },
};
const LAYOUT_KEYS = ["rows", "cols", "vals", "aggregatorName", "rendererName", "exclusions", "inclusions"];

export async function init(root) {
  const config = readJSON(root.dataset.config || "pivot-config");
  if (!config) throw new Error("Missing pivot configuration.");
  const out = root.querySelector("[data-pivot-output]");
  const count = root.querySelector("[data-pivot-count]");
  const select = root.querySelector("[data-saved-select]");
  const saveForm = root.querySelector("[data-saved-form]");
  const deleteBtn = root.querySelector("[data-saved-delete]");
  let views = readJSON(root.dataset.views || "saved-views") || [];
  let current = {};

  loadStyle("pivotCss");
  const [rows] = await Promise.all([
    fetchJSON(config.api),
    loadScripts("jquery", "jqueryui", "plotly", "pivot", "pivotPlotly", "pivotExport"),
  ]);
  const $ = window.jQuery;
  const utils = $.pivotUtilities;
  const renderers = $.extend({}, utils.renderers, utils.plotly_renderers || {}, utils.export_renderers || {});
  if (count) count.textContent = `${fmt(rows.length)} records`;

  if (!rows.length) {
    out.innerHTML = '<div class="state state--empty"><p class="state__title">No ActivityInfo records for this year yet</p><p class="state__message">Run the data import from the administration pages, then reload.</p></div>';
    return;
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const HIDDEN = ["sequence", "database_ai_id", "month_num"];
  const numericOnly = Object.keys(rows[0]).filter((k) => k !== "indicator_value");

  const draw = (layout = {}) => {
    const options = {
      renderers,
      sorters: { month: utils.sortAs(MONTHS) },
      hiddenAttributes: HIDDEN,
      hiddenFromAggregators: numericOnly,
      rows: PRESETS.month.rows,
      cols: PRESETS.month.cols,
      vals: ["indicator_value"],
      aggregatorName: "Integer Sum",
      rendererName: "Table",
      unusedAttrsVertical: true,
      menuLimit: 1000,
      hiddenFromDragDrop: ["indicator_value"],
      rendererOptions: { plotly: { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)" }, plotlyConfig: { displaylogo: false, responsive: true } },
      onRefresh: (cfg) => {
        current = Object.fromEntries(LAYOUT_KEYS.map((k) => [k, cfg[k]]));
      },
      ...layout,
    };
    $(out).pivotUI(rows, options, true);
  };

  const renderViews = () => {
    if (!select) return;
    select.innerHTML = '<option value="">Saved views…</option>';
    for (const v of views) {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = `${v.name}${v.is_shared ? " · shared" : ""}${v.mine ? "" : ` · ${v.owner}`}`;
      select.appendChild(opt);
    }
    if (deleteBtn) deleteBtn.disabled = true;
  };

  root.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      root.querySelectorAll("[data-preset]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      draw({ ...current, ...PRESETS[btn.dataset.preset], exclusions: {}, inclusions: {} });
    });
  });

  select?.addEventListener("change", () => {
    const view = views.find((v) => String(v.id) === select.value);
    if (deleteBtn) deleteBtn.disabled = !(view && view.mine);
    if (view) draw(view.layout || {});
  });

  saveForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = new FormData(saveForm);
    const name = String(data.get("name") || "").trim();
    if (!name) return;
    try {
      const saved = await fetchJSON(config.savedViewsApi, {
        method: "POST",
        body: { name, page: config.page, object_id: config.objectId, query: {}, layout: current, is_shared: data.get("is_shared") === "on" },
      });
      views = [...views.filter((v) => v.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name));
      renderViews();
      select.value = saved.id;
      if (deleteBtn) deleteBtn.disabled = false;
      saveForm.reset();
      toast(`Saved view “${saved.name}”.`);
    } catch (err) {
      toast(`Could not save the view: ${err.message}`, "error");
    }
  });

  deleteBtn?.addEventListener("click", async () => {
    const view = views.find((v) => String(v.id) === select.value);
    if (!view || !window.confirm(`Delete the saved view “${view.name}”?`)) return;
    try {
      await fetchJSON(`${config.savedViewsApi}${view.id}/`, { method: "DELETE" });
      views = views.filter((v) => v.id !== view.id);
      renderViews();
      toast(`Deleted “${escapeHTML(view.name)}”.`);
    } catch (err) {
      toast(`Could not delete the view: ${err.message}`, "error");
    }
  });

  root.querySelector("[data-pivot-csv]")?.addEventListener("click", () => {
    const table = out.querySelector("table.pvtTable");
    if (!table) {
      toast("Switch the renderer to a table to export it.", "error");
      return;
    }
    download(`${config.exportName || "pivot"}.csv`, toCSV(tableRows(table)));
  });

  renderViews();
  draw({ ...PRESETS[config.defaultPreset] });
  root.querySelector(`[data-preset="${config.defaultPreset}"]`)?.setAttribute("aria-pressed", "true");
}
