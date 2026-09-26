// Plotly charts declared in templates:
//   <div data-module="charts" data-chart="status-donut" data-source="chart-data" data-key="status_counts"></div>
// The JSON comes from {{ chart_data|json_script:"chart-data" }}. Charts follow the light/dark theme.
import { cssVar, isDark, loadScript, readJSON } from "./lib.js";

const STATUS_ORDER = ["on_track", "over_target", "off_track", "no_target", "not_reported"];
const STATUS_VARS = { on_track: "--nd-success", over_target: "--nd-warning", off_track: "--nd-danger", no_target: "--nd-neutral", not_reported: "--nd-muted" };
const PALETTE = ["#446ab3", "#5ba4d9", "#16865a", "#f0b04a", "#c63b3b", "#7c5cc4", "#00a3a3", "#e27d27", "#5b6778", "#9bc53d"];
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


// ---- helpers shared by the overview builders
const statusKey = (label) => String(label ?? "").trim().toLowerCase().replace(/[\s-]+/g, "_");

/** "#446ab3" + 0.15 -> "#446ab326"; other colour notations are returned unchanged. */
function withAlpha(color, alpha) {
  if (!/^#[0-9a-f]{6}$/i.test(color)) return color;
  return `${color}${Math.round(alpha * 255).toString(16).padStart(2, "0")}`;
}

/** {labels: [...], series: {name: [...]}, colors?: {name: "--nd-token"}} -> normalised parts. */
function seriesOf(data) {
  const labels = Array.isArray(data?.labels) ? data.labels.map(String) : [];
  const series = Object.entries(data?.series || {}).map(([name, values]) => [name, (values || []).map(num)]);
  return { labels, series, colors: data?.colors || {} };
}

const seriesColor = (name, i, colors) => (colors[name] ? cssVar(colors[name]) : i === 0 ? cssVar("--nd-primary") : PALETTE[i % PALETTE.length]);

/** A legend under the plot when the chart has two or more series, none otherwise. */
const legendFor = (count) => (count >= 2 ? { showlegend: true, legend: { orientation: "h", y: -0.24, x: 0, font: { size: 11 } } } : { showlegend: false });

/** Height for horizontal category charts: the declared minimum, or one row per category. */
const rowsHeight = (el, count, row = 28, extra = 84) => Math.max(Number(el.dataset.height) || 0, count * row + extra);

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
  // ---- overview dashboard builders
  "hbars-target"(el, data) {
    // [{section, achieved, target, percent, elapsed}] -> grey target bar, accent achieved bar, elapsed marker
    const rows = (Array.isArray(data) ? data : [])
      .slice()
      .sort((a, b) => num(a.target) - num(b.target) || num(a.achieved) - num(b.achieved));
    const names = rows.map((r) => String(r.section ?? r.name ?? ""));
    const text = cssVar("--nd-text");
    const shapes = rows
      .filter((r) => num(r.target) > 0 && r.elapsed != null)
      .map((r) => {
        const i = names.indexOf(String(r.section ?? r.name ?? ""));
        const x = (num(r.target) * Math.min(Math.max(num(r.elapsed), 0), 100)) / 100;
        return { type: "line", x0: x, x1: x, y0: i - 0.32, y1: i + 0.32, xref: "x", yref: "y", line: { color: text, width: 2 } };
      });
    return {
      traces: [
        {
          type: "bar",
          orientation: "h",
          name: "Target",
          y: names,
          x: rows.map((r) => num(r.target)),
          width: 0.64,
          marker: { color: withAlpha(cssVar("--nd-neutral"), 0.28), line: { width: 0 } },
          hovertemplate: "%{y} · target: %{x:,.0f}<extra></extra>",
        },
        {
          type: "bar",
          orientation: "h",
          name: "Achieved",
          y: names,
          x: rows.map((r) => num(r.achieved)),
          width: 0.38,
          customdata: rows.map((r) => (r.percent == null ? "–" : `${Math.round(num(r.percent))}%`)),
          marker: { color: cssVar("--nd-primary"), line: { width: 0 } },
          hovertemplate: "%{y} · achieved: %{x:,.0f} (%{customdata} of target)<extra></extra>",
        },
        {
          type: "scatter",
          mode: "markers",
          name: "Share of the PD period elapsed",
          x: [null],
          y: [null],
          marker: { symbol: "line-ns-open", size: 10, color: text, line: { width: 2, color: text } },
          hoverinfo: "skip",
        },
      ],
      layout: {
        barmode: "overlay",
        bargap: 0.2,
        shapes,
        yaxis: { type: "category" },
        xaxis: { rangemode: "tozero" },
        margin: { t: 4, r: 16, b: 56, l: 8 },
        height: rowsHeight(el, rows.length),
        ...legendFor(3),
      },
    };
  },
  lines(el, data) {
    // {labels, series: {name: [...]}, colors?} -> multi-line chart with a light fill
    const { labels, series, colors } = seriesOf(data);
    return {
      traces: series.map(([name, values], i) => {
        const color = seriesColor(name, i, colors);
        return {
          type: "scatter",
          mode: "lines+markers",
          name,
          x: labels,
          y: values,
          line: { color, width: 2, shape: "linear" },
          marker: { size: 5, color },
          fill: "tozeroy",
          fillcolor: withAlpha(color, 0.12),
          hovertemplate: `${name}: %{y:,.0f}<extra></extra>`,
        };
      }),
      layout: {
        hovermode: "x unified",
        xaxis: { type: "category" },
        yaxis: { rangemode: "tozero" },
        margin: { t: 8, r: 12, b: 56, l: 48 },
        ...legendFor(series.length),
      },
    };
  },
  "stacked-h"(el, data, labels) {
    // [{section, on_track, off_track, over_target, no_target, not_reported, total}] -> stacked bars in status colours
    const rows = (Array.isArray(data) ? data : []).slice().sort((a, b) => num(a.total) - num(b.total));
    const names = rows.map((r) => String(r.section ?? r.name ?? ""));
    const traces = STATUS_ORDER.map((key) => {
      const label = labels?.[key] || key;
      return {
        type: "bar",
        orientation: "h",
        name: label,
        y: names,
        x: rows.map((r) => num(r[key])),
        marker: { color: cssVar(STATUS_VARS[key]), line: { width: 0 } },
        hovertemplate: `%{y} · ${label}: %{x:,}<extra></extra>`,
      };
    }).filter((t) => t.x.some((v) => v > 0));
    return {
      traces,
      layout: {
        barmode: "stack",
        bargap: 0.3,
        yaxis: { type: "category" },
        margin: { t: 4, r: 16, b: 56, l: 8 },
        height: rowsHeight(el, rows.length),
        ...legendFor(traces.length),
      },
    };
  },
  grouped(el, data) {
    // {labels, series: {name: [...]}, colors?: {name: "--nd-token"}} -> grouped vertical bars
    const { labels, series, colors } = seriesOf(data);
    return {
      traces: series.map(([name, values], i) => ({
        type: "bar",
        name,
        x: labels,
        y: values,
        marker: { color: seriesColor(name, i, colors), line: { width: 0 } },
        hovertemplate: `%{x} · ${name}: %{y:,}<extra></extra>`,
      })),
      layout: {
        barmode: "group",
        bargap: 0.25,
        bargroupgap: 0.06,
        xaxis: { type: "category" },
        yaxis: { rangemode: "tozero" },
        margin: { t: 8, r: 8, b: 56, l: 40 },
        ...legendFor(series.length),
      },
    };
  },
  "scatter-xy"(el, data) {
    // [{name, x, y, ahead}] -> markers coloured by ahead/behind, a diagonal reference line, both axes in percent
    const rows = (Array.isArray(data) ? data : []).filter((r) => r.x != null && r.y != null);
    const top = Math.max(105, ...rows.map((r) => Math.max(num(r.x), num(r.y)) + 8));
    const muted = cssVar("--nd-muted");
    const group = (list, name, token) => ({
      type: "scatter",
      mode: "markers+text",
      name,
      x: list.map((r) => num(r.x)),
      y: list.map((r) => num(r.y)),
      text: list.map((r) => String(r.name ?? "")),
      textposition: "top center",
      textfont: { size: 10, color: muted },
      marker: { size: 10, color: cssVar(token), line: { width: 1.5, color: cssVar("--nd-surface") } },
      hovertemplate: "%{text}<br>Disbursed: %{x:.0f}%<br>Achieved: %{y:.0f}%<extra></extra>",
    });
    const traces = [
      group(rows.filter((r) => r.ahead), "Delivery ahead of spending", "--nd-success"),
      group(rows.filter((r) => !r.ahead), "Spending ahead of delivery", "--nd-warning"),
    ].filter((t) => t.x.length);
    return {
      traces,
      layout: {
        shapes: [{ type: "line", x0: 0, y0: 0, x1: top, y1: top, xref: "x", yref: "y", line: { color: cssVar("--nd-border"), width: 1, dash: "dot" } }],
        xaxis: { range: [0, top], ticksuffix: "%", title: { text: "Disbursed", font: { size: 11, color: muted } } },
        yaxis: { range: [0, top], ticksuffix: "%", title: { text: "Achieved", font: { size: 11, color: muted } } },
        margin: { t: 12, r: 12, b: 64, l: 52 },
        ...legendFor(traces.length),
      },
    };
  },
  hbars(el, data) {
    // pairs -> horizontal bars, first pair at the top; data-prefix/data-suffix decorate the hover value,
    // data-color-by="status" colours each bar by its label ("On Track" -> success)
    const rows = pairs(data).slice().reverse();
    const prefix = el.dataset.prefix || "";
    const suffix = el.dataset.suffix || "";
    const byStatus = el.dataset.colorBy === "status";
    const labels = rows.map((r) => (r[0].length > 38 ? `${r[0].slice(0, 36)}…` : r[0]));
    return {
      traces: [
        {
          type: "bar",
          orientation: "h",
          y: labels,
          x: rows.map((r) => r[1]),
          customdata: rows.map((r) => r[0]),
          marker: {
            color: byStatus ? rows.map((r) => cssVar(STATUS_VARS[statusKey(r[0])] || "--nd-neutral")) : cssVar(el.dataset.color || "--nd-primary"),
            line: { width: 0 },
          },
          hovertemplate: `%{customdata}: ${prefix}%{x:,.0f}${suffix}<extra></extra>`,
        },
      ],
      layout: {
        bargap: 0.3,
        yaxis: { type: "category" },
        xaxis: { rangemode: "tozero" },
        margin: { t: 4, r: 16, b: 32, l: 8 },
        height: rowsHeight(el, rows.length, 26, 60),
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
