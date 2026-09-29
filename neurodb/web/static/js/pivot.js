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
// Numerator/denominator links of ratio masters are not part of the master's value: leave them out by
// default so master rows add up to what is counted (the filter on value_role can bring them back).
const DEFAULT_EXCLUSIONS = { value_role: ["Not counted in master indicator"] };

// Readable names for the fields of the API rows. The pivot shows these; layouts (presets, saved
// views) keep the field names, translated when a layout is drawn and when it is saved.
const LABELS = {
  master_indicator: "Master indicator",
  target: "Target",
  sub_indicator: "Sub-indicator",
  value_role: "Counted in master?",
  ai_indicator: "ActivityInfo indicator",
  gender: "Gender",
  nationality: "Nationality",
  disability: "Disability",
  programme: "Programme",
  age: "Age group",
  indicator_name: "ActivityInfo indicator code",
  awp_code: "AWP code",
  emergency: "Emergency",
  governorate: "Governorate",
  district: "District",
  cadaster: "Cadaster",
  partner: "Partner",
  pd: "Programme document",
  plan: "Plan",
  project: "Project",
  month: "Month",
  indicator_value: "Value",
  database: "Database",
};
const FIELDS = Object.fromEntries(Object.entries(LABELS).map(([field, label]) => [label, field]));
const toLabel = (field) => LABELS[field] || field;
const toField = (label) => FIELDS[label] || label;
const mapNames = (list, fn) => (Array.isArray(list) ? list.map(fn) : list);
const mapKeys = (obj, fn) => (obj ? Object.fromEntries(Object.entries(obj).map(([k, v]) => [fn(k), v])) : obj);
function translateLayout(layout, fn) {
  const out = { ...layout };
  for (const key of ["rows", "cols", "vals"]) if (key in out) out[key] = mapNames(out[key], fn);
  for (const key of ["exclusions", "inclusions"]) if (key in out) out[key] = mapKeys(out[key], fn);
  return out;
}

// Fields that tell indicators apart: adding up across them mixes children, schools and percentages.
const INDICATOR_FIELDS = ["master_indicator", "sub_indicator", "ai_indicator", "indicator_name", "awp_code"].map(toLabel);
// The aggregators offered: a saved view that used another one keeps it (added when it is drawn).
const AGGREGATORS = { Sum: "Integer Sum", Count: "Count", Average: "Average" };

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
  // One pivot row per indicator link, month, place, partner and breakdown, not per ActivityInfo record.
  if (count) {
    count.textContent = `${fmt(rows.length)} pivot rows`;
    count.title = "A pivot row groups the records of one indicator, month, place, partner and breakdown. One record can count in several indicators, so this differs from the number of records.";
  }
  const labelled = rows.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [toLabel(k), v])));
  const masterLabel = toLabel("master_indicator");

  // The table renderers leave out the totals that would add up different indicators: the Totals row
  // when rows are indicators, the Totals column when columns are, both when every cell mixes them.
  const note = root.querySelector("[data-pivot-mix]");
  const withTotals = (render) => (pivotData, opts) => {
    // Counts of pivot rows add up across indicators; sums and averages of values do not.
    const counting = /^Count/.test(pivotData.aggregatorName || "");
    const mixed = !counting && new Set(labelled.filter((r) => pivotData.filter(r)).map((r) => r[masterLabel])).size > 1;
    const inRows = pivotData.rowAttrs.some((a) => INDICATOR_FIELDS.includes(a));
    const inCols = pivotData.colAttrs.some((a) => INDICATOR_FIELDS.includes(a));
    const everyCell = mixed && !inRows && !inCols;
    if (note) note.hidden = !everyCell;
    const table = { rowTotals: !(mixed && (inCols || everyCell)), colTotals: !(mixed && (inRows || everyCell)) };
    return render(pivotData, $.extend(true, {}, opts, { table }));
  };
  const tables = Object.fromEntries(Object.entries(utils.renderers).map(([name, fn]) => [name, withTotals(fn)]));
  const renderers = $.extend({}, tables, utils.plotly_renderers || {}, utils.export_renderers || {});
  const aggregatorsFor = (name) => {
    const chosen = Object.fromEntries(Object.entries(AGGREGATORS).map(([label, key]) => [label, utils.aggregators[key]]));
    if (name && !(name in chosen) && utils.aggregators[name]) chosen[name] = utils.aggregators[name];
    return chosen;
  };

  if (!rows.length) {
    out.innerHTML = '<div class="state state--empty"><p class="state__title">No ActivityInfo records for this year yet</p><p class="state__message">Run the data import from the administration pages, then reload.</p></div>';
    return;
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const HIDDEN = ["master_id", "sequence", "database_ai_id", "month_num"];
  const numericOnly = Object.keys(labelled[0]).filter((k) => k !== toLabel("indicator_value"));

  const draw = (layout = {}) => {
    // Saved views store field names and v2's "Integer Sum": show them with the readable names.
    const shown = translateLayout({ rows: PRESETS.month.rows, cols: PRESETS.month.cols, exclusions: DEFAULT_EXCLUSIONS, ...layout }, toLabel);
    const aggregatorName = Object.entries(AGGREGATORS).find(([, key]) => key === layout.aggregatorName)?.[0] || layout.aggregatorName || "Sum";
    const options = {
      renderers,
      aggregators: aggregatorsFor(aggregatorName),
      sorters: { [toLabel("month")]: utils.sortAs(MONTHS) },
      hiddenAttributes: HIDDEN,
      hiddenFromAggregators: numericOnly,
      vals: [toLabel("indicator_value")],
      rendererName: "Table",
      unusedAttrsVertical: false,
      menuLimit: 1000,
      hiddenFromDragDrop: [toLabel("indicator_value")],
      rendererOptions: { plotly: { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)" }, plotlyConfig: { displaylogo: false, responsive: true } },
      onRefresh: (cfg) => {
        if (note && !(cfg.rendererName in tables)) note.hidden = true;
        const layoutNow = Object.fromEntries(LAYOUT_KEYS.map((k) => [k, cfg[k]]));
        current = translateLayout({ ...layoutNow, aggregatorName: AGGREGATORS[cfg.aggregatorName] || cfg.aggregatorName }, toField);
      },
      ...shown,
      aggregatorName,
    };
    $(out).pivotUI(labelled, options, true);
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
      draw({ ...current, ...PRESETS[btn.dataset.preset], exclusions: DEFAULT_EXCLUSIONS, inclusions: {} });
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

  // The unused fields sit in a band above the table; it folds away so the table keeps the width.
  const fieldsBtn = root.querySelector("[data-pivot-fields]");
  fieldsBtn?.addEventListener("click", () => {
    const open = fieldsBtn.getAttribute("aria-expanded") !== "true";
    fieldsBtn.setAttribute("aria-expanded", String(open));
    root.classList.toggle("pivot--fields-open", open);
  });

  renderViews();
  draw({ ...PRESETS[config.defaultPreset] });
  root.querySelector(`[data-preset="${config.defaultPreset}"]`)?.setAttribute("aria-pressed", "true");
}
