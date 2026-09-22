// Plotly charts declared in templates:
//   <div data-module="charts" data-chart="status-donut" data-source="chart-data" data-key="status_counts"></div>
// The JSON comes from {{ chart_data|json_script:"chart-data" }}. Charts follow the light/dark theme.
import { cssVar, isDark, loadScript, readJSON } from "./lib.js";

const STATUS_ORDER = ["on_track", "over_target", "off_track", "no_target"];
const STATUS_VARS = { on_track: "--nd-success", over_target: "--nd-warning", off_track: "--nd-danger", no_target: "--nd-neutral" };
const PALETTE = ["#1a73c7", "#34b1e6", "#16865a", "#f0b04a", "#c63b3b", "#7c5cc4", "#00a3a3", "#e27d27", "#5b6778", "#9bc53d"];
const NATIONALITY = { LEB: "Lebanese", SYR: "Syrian", PRS: "Palestinian (Syria)", PRL: "Palestinian (Lebanon)", OTH: "Other", ALL: "All" };

const num = (v) => (v === null || v === undefined || v === "" ? 0 : Number(v));

function baseLayout(el, extra = {}) {
  const text = cssVar("--nd-text");
  const muted = cssVar("--nd-muted");
  const grid = cssVar("--nd-border");
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: cssVar("--nd-font") || "system-ui", color: text, size: 12 },
    margin: { t: 8, r: 8, b: 40, l: 48 },
    height: Number(el.dataset.height) || undefined,
    showlegend: false,
    colorway: PALETTE,
    hoverlabel: { bgcolor: isDark() ? "#1d2632" : "#ffffff", bordercolor: grid, font: { color: text } },
    ...extra,
    // Axis settings merge with the builder's (e.g. a category axis for year labels).
    xaxis: { gridcolor: grid, linecolor: grid, zerolinecolor: grid, tickfont: { color: muted }, automargin: true, ...(extra.xaxis || {}) },
    yaxis: { gridcolor: grid, linecolor: grid, zerolinecolor: grid, tickfont: { color: muted }, automargin: true, ...(extra.yaxis || {}) },
  };
}

const CONFIG = { displaylogo: false, responsive: true, displayModeBar: false };

function pairs(data) {
  // [[label, value], ...] | {label: value} | [{label/name/x, value/y}, ...]
  if (Array.isArray(data)) {
    return data.map((d) => {
      if (Array.isArray(d)) return [String(d[0]), num(d[1])];
      const values = Object.values(d);
      const label = d.label ?? d.name ?? d.x ?? values.find((v) => typeof v === "string") ?? "";
      const value = d.value ?? d.y ?? d.interventions ?? d.n ?? values.find((v) => typeof v === "number") ?? 0;
      return [String(label), num(value)];
    });
  }
  return Object.entries(data || {}).map(([k, v]) => [k, num(v)]);
}

const BUILDERS = {
  "status-donut"(el, data, labels) {
    const keys = STATUS_ORDER.filter((k) => num(data[k]) > 0);
    return {
      traces: [
        {
          type: "pie",
          hole: 0.62,
          sort: false,
          labels: keys.map((k) => labels?.[k] || k),
          values: keys.map((k) => num(data[k])),
          marker: { colors: keys.map((k) => cssVar(STATUS_VARS[k])) },
          textinfo: "none",
          hovertemplate: "%{label}: %{value} (%{percent})<extra></extra>",
        },
      ],
      layout: {
        margin: { t: 4, r: 4, b: 4, l: 4 },
        annotations: [{ text: `<b>${keys.reduce((s, k) => s + num(data[k]), 0)}</b><br>indicators`, showarrow: false, font: { size: 14 } }],
      },
    };
  },
  monthly(el, data) {
    const rows = Array.isArray(data) ? data : [];
    return {
      traces: [
        {
          type: "bar",
          x: rows.map((r) => r.month),
          y: rows.map((r) => num(r.value)),
          marker: { color: cssVar("--nd-primary"), line: { width: 0 } },
          hovertemplate: "%{x}: %{y:,.0f}<extra></extra>",
          name: "Value",
        },
        {
          type: "scatter",
          mode: "lines+markers",
          x: rows.map((r) => r.month),
          y: rows.map((r) => num(r.reports)),
          yaxis: "y2",
          line: { color: cssVar("--nd-warning"), width: 2, shape: "spline" },
          marker: { size: 5 },
          hovertemplate: "%{x}: %{y:,} reports<extra></extra>",
          name: "Reports",
        },
      ],
      layout: {
        bargap: 0.35,
        showlegend: true,
        legend: { orientation: "h", y: 1.12, x: 0 },
        yaxis2: { overlaying: "y", side: "right", showgrid: false, tickfont: { color: cssVar("--nd-muted") } },
        margin: { t: 24, r: 44, b: 36, l: 56 },
      },
    };
  },
  bars(el, data) {
    let rows = pairs(data);
    const limit = Number(el.dataset.limit) || 0;
    if (limit) rows = rows.slice(0, limit);
    const horizontal = el.dataset.orientation !== "v";
    if (horizontal) rows = rows.slice().reverse();
    const labels = rows.map((r) => (r[0].length > 38 ? `${r[0].slice(0, 36)}…` : r[0]));
    return {
      traces: [
        {
          type: "bar",
          orientation: horizontal ? "h" : "v",
          x: horizontal ? rows.map((r) => r[1]) : labels,
          y: horizontal ? labels : rows.map((r) => r[1]),
          customdata: rows.map((r) => r[0]),
          marker: { color: cssVar(el.dataset.color || "--nd-primary") },
          hovertemplate: `%{customdata}: %{${horizontal ? "x" : "y"}:,.0f}<extra></extra>`,
        },
      ],
      layout: {
        bargap: 0.3,
        [horizontal ? "yaxis" : "xaxis"]: { type: "category" },
        margin: horizontal ? { t: 4, r: 16, b: 32, l: 8 } : { t: 8, r: 8, b: 60, l: 56 },
        height: Number(el.dataset.height) || Math.max(220, rows.length * (horizontal ? 26 : 0) + 60),
      },
    };
  },
  pie(el, data) {
    const rows = pairs(data).filter((r) => r[1] > 0);
    const map = el.dataset.labels === "nationality" ? NATIONALITY : {};
    return {
      traces: [
        {
          type: "pie",
          hole: 0.5,
          labels: rows.map((r) => map[r[0]] || r[0]),
          values: rows.map((r) => r[1]),
          textinfo: "percent",
          hovertemplate: "%{label}: %{value:,} (%{percent})<extra></extra>",
        },
      ],
      layout: { showlegend: true, legend: { orientation: "v", x: 1, y: 0.5 }, margin: { t: 4, r: 4, b: 4, l: 4 } },
    };
  },
  matrix(el, data) {
    // {columns: [...], rows: [{label, values: [...]}]} -> stacked horizontal bars
    const rows = (data?.rows || []).slice().sort((a, b) => num(a.total) - num(b.total));
    const map = el.dataset.labels === "nationality" ? NATIONALITY : {};
    return {
      traces: (data?.columns || []).map((col, i) => ({
        type: "bar",
        orientation: "h",
        name: map[col] || col,
        y: rows.map((r) => r.label),
        x: rows.map((r) => num(r.values[i])),
        hovertemplate: `%{y} · ${map[col] || col}: %{x:,}<extra></extra>`,
      })),
      layout: {
        barmode: "stack",
        showlegend: true,
        legend: { orientation: "h", y: -0.12 },
        margin: { t: 4, r: 16, b: 48, l: 8 },
        height: Number(el.dataset.height) || Math.max(260, rows.length * 24 + 90),
      },
    };
  },
};

function lookup(source, key) {
  if (!key) return source;
  return key.split(".").reduce((obj, part) => (obj == null ? obj : obj[part]), source);
}

function render(el) {
  const source = readJSON(el.dataset.source || "chart-data") || {};
  const data = lookup(source, el.dataset.key);
  const builder = BUILDERS[el.dataset.chart];
  if (!builder) throw new Error(`Unknown chart type ${el.dataset.chart}`);
  const empty = data == null || (Array.isArray(data) && !data.length) || (typeof data === "object" && !Array.isArray(data) && !Object.keys(data).length);
  if (empty) {
    el.innerHTML = `<div class="state state--empty"><p class="state__title">${el.dataset.emptyTitle || "No data yet"}</p></div>`;
    return;
  }
  const { traces, layout } = builder(el, data, source.labels);
  window.Plotly.react(el, traces, baseLayout(el, layout), CONFIG);
}

export async function init(el) {
  el.setAttribute("role", el.getAttribute("role") || "img");
  await loadScript("plotly");
  render(el);
  document.addEventListener("nd:themechange", () => render(el));
}
