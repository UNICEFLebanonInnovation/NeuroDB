// Plotly charts declared in templates:
//   <div data-module="charts" data-chart="status-donut" data-source="chart-data" data-key="status_counts"></div>
// The JSON comes from {{ chart_data|json_script:"chart-data" }}. Charts follow the light/dark theme.
import { cssVar, isDark, loadScript, readJSON } from "./lib.js";

const STATUS_ORDER = ["on_track", "over_target", "off_track", "no_target", "not_reported"];
const STATUS_VARS = { on_track: "--nd-success", over_target: "--nd-warning", off_track: "--nd-danger", no_target: "--nd-neutral", not_reported: "--nd-muted" };
const PALETTE = ["#446ab3", "#5ba4d9", "#16865a", "#f0b04a", "#c63b3b", "#7c5cc4", "#00a3a3", "#e27d27", "#5b6778", "#9bc53d"];
const NATIONALITY = { LEB: "Lebanese", SYR: "Syrian", PRS: "Palestinian (Syria)", PRL: "Palestinian (Lebanon)", PAL: "Palestinian (PRL + PRS)", OTH: "Other", ALL: "All" };

// One colour per nationality code, the same in every population chart
const NATIONALITY_COLORS = { LEB: "#446ab3", SYR: "#5ba4d9", PRS: "#e27d27", PRL: "#16865a", PAL: "#7c5cc4", OTH: "#5b6778", ALL: "#9bc53d" };
const nationalityColor = (code, i) => NATIONALITY_COLORS[code] || PALETTE[i % PALETTE.length];

/** A share as people read it: one decimal, two below 1 % (0.44 %, not 0 % or 4 %). */
const shareLabel = (share) => `${share < 1 ? share.toFixed(2) : share.toFixed(1)}%`;

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
    // {months, indicators: [{id, label, unit, values, reports}], default}: one indicator at a time
    // (the indicator picked in the <select> named by data-select), as adding indicators with
    // different units gave a meaningless total. The records line shares the months, on its own axis.
    const select = el.dataset.select ? document.getElementById(el.dataset.select) : null;
    const list = data?.indicators || [];
    const series = list.find((i) => String(i.id) === String(select?.value ?? data?.default)) || list[0];
    const months = data?.months || [];
    if (!series || !months.length) {
      window.Plotly.purge(el);
      el.innerHTML = `<div class="state state--empty"><p class="state__title">${el.dataset.emptyTitle || "No data yet"}</p></div>`;
      return null;
    }
    const unit = series.unit ? ` ${series.unit}` : "";
    return {
      traces: [
        {
          type: "bar",
          x: months,
          y: series.values.map((v) => (v === null ? null : num(v))),
          marker: { color: cssVar("--nd-primary"), line: { width: 0 } },
          hovertemplate: `%{x}: %{y:,.1~f}${unit}<extra></extra>`,
          name: "Reported in the month",
        },
        {
          type: "scatter",
          mode: "lines+markers",
          x: months,
          y: series.reports.map(num),
          yaxis: "y2",
          line: { color: cssVar("--nd-warning"), width: 2 },
          marker: { size: 5 },
          hovertemplate: "%{x}: %{y:,} records<extra></extra>",
          name: "Records",
        },
      ],
      layout: {
        bargap: 0.35,
        showlegend: true,
        legend: { orientation: "h", y: 1.12, x: 0 },
        yaxis: { rangemode: "tozero" },
        yaxis2: { overlaying: "y", side: "right", showgrid: false, rangemode: "tozero", tickfont: { color: cssVar("--nd-muted") } },
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
  "nationality-pie"(el, data) {
    // {code: value} -> donut in the nationality colours; small slices are labelled outside the ring
    const rows = pairs(data).filter((r) => r[1] > 0);
    const total = rows.reduce((sum, r) => sum + r[1], 0) || 1;
    const shares = rows.map((r) => (100 * r[1]) / total);
    return {
      traces: [
        {
          type: "pie",
          hole: 0.5,
          sort: false,
          labels: rows.map((r) => NATIONALITY[r[0]] || r[0]),
          values: rows.map((r) => r[1]),
          marker: { colors: rows.map((r, i) => nationalityColor(r[0], i)) },
          text: shares.map(shareLabel),
          textinfo: "text",
          textposition: shares.map((p) => (p < 4 ? "outside" : "inside")),
          automargin: true,
          hovertemplate: "%{label}: %{value:,} (%{text})<extra></extra>",
        },
      ],
      layout: { showlegend: true, legend: { orientation: "v", x: 1, y: 0.5 }, margin: { t: 16, r: 4, b: 16, l: 4 } },
    };
  },
  "nationality-bars"(el, data) {
    // {columns: [codes], rows: [{label, values, total}]} -> stacked horizontal bars in the nationality colours
    const rows = (data?.rows || []).slice().sort((a, b) => num(a.total) - num(b.total));
    const columns = data?.columns || [];
    return {
      traces: columns.map((col, i) => ({
        type: "bar",
        orientation: "h",
        name: NATIONALITY[col] || col,
        y: rows.map((r) => r.label),
        x: rows.map((r) => num(r.values[i])),
        marker: { color: nationalityColor(col, i), line: { width: 0 } },
        hovertemplate: `%{y} · ${NATIONALITY[col] || col}: %{x:,}<extra></extra>`,
      })),
      layout: {
        barmode: "stack",
        showlegend: true,
        legend: { orientation: "h", y: -0.12, traceorder: "normal" },
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
  // ---- management brief
  bullet(el, data) {
    // [{section, achieved, target, expected, cp_target}] -> target bar, achieved bar, expected marker, CP target diamond
    const rows = (Array.isArray(data) ? data : []).slice().sort((a, b) => num(a.target) - num(b.target));
    const names = rows.map((r) => String(r.section ?? ""));
    const text = cssVar("--nd-text");
    const shapes = rows
      .filter((r) => r.expected != null)
      .map((r, i) => ({ type: "line", x0: num(r.expected), x1: num(r.expected), y0: i - 0.34, y1: i + 0.34, xref: "x", yref: "y", line: { color: text, width: 2 } }));
    const cp = rows.filter((r) => r.cp_target != null);
    const traces = [
      { type: "bar", orientation: "h", name: "PD target", y: names, x: rows.map((r) => num(r.target)), width: 0.64, marker: { color: withAlpha(cssVar("--nd-neutral"), 0.28), line: { width: 0 } }, hovertemplate: "%{y} · PD target: %{x:,.0f}<extra></extra>" },
      { type: "bar", orientation: "h", name: "Achieved", y: names, x: rows.map((r) => num(r.achieved)), width: 0.38, customdata: rows.map((r) => (r.percent == null ? "–" : `${Math.round(num(r.percent))}%`)), marker: { color: cssVar("--nd-primary"), line: { width: 0 } }, hovertemplate: "%{y} · achieved: %{x:,.0f} (%{customdata} of target)<extra></extra>" },
      { type: "scatter", mode: "markers", name: "Expected at this point", x: [null], y: [null], marker: { symbol: "line-ns-open", size: 10, color: text, line: { width: 2, color: text } }, hoverinfo: "skip" },
    ];
    if (cp.length) {
      traces.push({ type: "scatter", mode: "markers", name: "Country Programme target", x: cp.map((r) => num(r.cp_target)), y: cp.map((r) => String(r.section)), marker: { symbol: "diamond", size: 11, color: cssVar("--nd-series-2"), line: { width: 1, color: cssVar("--nd-surface") } }, hovertemplate: "%{y} · CP target: %{x:,.0f}<extra></extra>" });
    }
    return { traces, layout: { barmode: "overlay", bargap: 0.2, shapes, yaxis: { type: "category" }, xaxis: { rangemode: "tozero" }, margin: { t: 4, r: 16, b: 56, l: 8 }, height: rowsHeight(el, rows.length), ...legendFor(3) } };
  },
  projection(el, data) {
    // {labels, series: {name: [12 cumulative values]}, actual_months, target, colors} -> solid to date, dashed projection, target line
    const { labels, series, colors } = seriesOf(data);
    const shown = Math.max(1, Number(data?.actual_months) || 0);
    const traces = [];
    series.forEach(([name, values], i) => {
      const color = seriesColor(name, i, colors);
      if (!values.some((v) => v)) return;
      traces.push({ type: "scatter", mode: "lines", name, x: labels.slice(0, shown), y: values.slice(0, shown), line: { color, width: 2.5 }, hovertemplate: `%{x} · ${name}: %{y:,.0f}<extra></extra>` });
      traces.push({ type: "scatter", mode: "lines", name: `${name} projected`, x: labels.slice(shown - 1), y: values.slice(shown - 1), line: { color, width: 2, dash: "dot" }, showlegend: false, hovertemplate: `%{x} · ${name} projected: %{y:,.0f}<extra></extra>` });
    });
    const shapes = [];
    const annotations = [];
    if (num(data?.target) > 0) {
      shapes.push({ type: "line", x0: 0, x1: 1, xref: "paper", y0: num(data.target), y1: num(data.target), yref: "y", line: { color: cssVar("--nd-muted"), width: 1, dash: "dash" } });
      annotations.push({ x: 1, xref: "paper", y: num(data.target), yref: "y", text: `PD targets ${Math.round(num(data.target)).toLocaleString()}`, showarrow: false, xanchor: "right", yanchor: "bottom", font: { size: 10, color: cssVar("--nd-muted") } });
    }
    if (shown < 12) shapes.push({ type: "rect", x0: shown - 1, x1: 11, xref: "x", y0: 0, y1: 1, yref: "paper", fillcolor: withAlpha(cssVar("--nd-neutral"), 0.08), line: { width: 0 } });
    return { traces, layout: { shapes, annotations, xaxis: { type: "category" }, yaxis: { rangemode: "tozero" }, margin: { t: 8, r: 8, b: 56, l: 48 }, ...legendFor(traces.filter((t) => t.showlegend !== false).length) } };
  },
  "stacked-series"(el, data) {
    // {labels, series: {name: [...]}, colors?} -> horizontal stacked bars, one bar per label
    const { labels, series, colors } = seriesOf(data);
    const traces = series
      .map(([name, values], i) => ({ type: "bar", orientation: "h", name, y: labels, x: values, marker: { color: seriesColor(name, i, colors), line: { width: 0 } }, hovertemplate: `%{y} · ${name}: %{x:,}<extra></extra>` }))
      .filter((t) => t.x.some((v) => v > 0));
    return { traces, layout: { barmode: "stack", bargap: 0.3, yaxis: { type: "category", autorange: "reversed" }, xaxis: { rangemode: "tozero" }, margin: { t: 4, r: 16, b: 56, l: 8 }, height: rowsHeight(el, labels.length), ...legendFor(traces.length) } };
  },
  "bubble-quadrant"(el, data) {
    // [{name, x, y, size, quadrant}] -> sized markers coloured by quadrant, a diagonal reference line
    const rows = (Array.isArray(data) ? data : []).filter((r) => r.x != null && r.y != null);
    const top = Math.max(105, ...rows.map((r) => Math.max(num(r.x), num(r.y)) + 8));
    const maxSize = Math.max(1, ...rows.map((r) => num(r.size)));
    const muted = cssVar("--nd-muted");
    const group = (list, name, token) => ({
      type: "scatter",
      mode: "markers+text",
      name,
      x: list.map((r) => num(r.x)),
      y: list.map((r) => num(r.y)),
      text: list.map((r) => String(r.name ?? "").slice(0, 18)),
      textposition: "top center",
      textfont: { size: 10, color: muted },
      customdata: list.map((r) => num(r.size)),
      marker: { size: list.map((r) => 8 + 22 * Math.sqrt(num(r.size) / maxSize)), color: cssVar(token), opacity: 0.85, line: { width: 1.5, color: cssVar("--nd-surface") } },
      hovertemplate: "%{text}<br>Disbursed: %{x:.0f}%<br>Achieved: %{y:.0f}%<br>Reserved: $%{customdata:,.0f}<extra></extra>",
    });
    const traces = [
      group(rows.filter((r) => r.quadrant === "ahead"), "Delivering ahead of spending", "--nd-success"),
      group(rows.filter((r) => r.quadrant === "spending_ahead"), "Spending ahead of delivery", "--nd-warning"),
      group(rows.filter((r) => r.quadrant === "behind"), "Behind on both", "--nd-danger"),
    ].filter((t) => t.x.length);
    return { traces, layout: { shapes: [{ type: "line", x0: 0, y0: 0, x1: top, y1: top, xref: "x", yref: "y", line: { color: cssVar("--nd-border"), width: 1, dash: "dot" } }], xaxis: { range: [0, top], ticksuffix: "%", title: { text: "Disbursed of reserved", font: { size: 11, color: muted } } }, yaxis: { range: [0, top], ticksuffix: "%", title: { text: "Achieved (mean, capped)", font: { size: 11, color: muted } } }, margin: { t: 12, r: 12, b: 64, l: 52 }, ...legendFor(traces.length) } };
  },
  flow(el, data) {
    // {donors: [{name, amount}], links: [{donor, section, amount}], sections: [{name, children, reserved}]}
    // -> donor → section → children bands, drawn as SVG (the basic Plotly bundle has no sankey)
    const donors = (data?.donors || []).filter((d) => num(d.amount) > 0);
    const sections = (data?.sections || []).filter((s) => num(s.reserved) > 0);
    const W = 920;
    const H = Math.max(240, Math.max(donors.length, sections.length) * 34 + 60);
    const top = 34;
    const gap = 10;
    const usable = H - top - 16 - gap * (Math.max(donors.length, sections.length) - 1);
    const totalDonors = donors.reduce((a, d) => a + num(d.amount), 0) || 1;
    const totalSections = sections.reduce((a, s) => a + num(s.reserved), 0) || 1;
    const scale = usable / Math.max(totalDonors, totalSections);
    const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
    const money = (v) => (v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${Math.round(v)}`);
    const left = [];
    let y = top;
    donors.forEach((d) => {
      const h = Math.max(4, num(d.amount) * scale);
      left.push({ name: d.name, y, h, cursor: y, amount: num(d.amount) });
      y += h + gap;
    });
    const mid = [];
    y = top;
    sections.forEach((s, i) => {
      const h = Math.max(4, num(s.reserved) * scale);
      mid.push({ name: s.name, y, h, cursor: y, color: PALETTE[i % PALETTE.length], reserved: num(s.reserved), children: num(s.children) });
      y += h + gap;
    });
    const x1 = 200;
    const x2 = 600;
    const x3 = 900;
    const parts = [];
    const band = (xa, ya, ha, xb, yb, hb, color, title) => {
      const c = (xa + xb) / 2;
      return `<path d="M${xa},${ya} C${c},${ya} ${c},${yb} ${xb},${yb} L${xb},${yb + hb} C${c},${yb + hb} ${c},${ya + ha} ${xa},${ya + ha} Z" fill="${color}" opacity="0.32"><title>${esc(title)}</title></path>`;
    };
    (data?.links || []).forEach((l) => {
      const d = left.find((x) => x.name === String(l.donor));
      const s = mid.find((x) => x.name === String(l.section));
      if (!d || !s || !(num(l.amount) > 0)) return;
      const h = num(l.amount) * scale;
      parts.push(band(x1 + 14, d.cursor, h, x2, s.cursor, h, s.color, `${l.donor} → ${l.section}: ${money(num(l.amount))}`));
      d.cursor += h;
      s.cursor += h;
    });
    mid.forEach((s) => {
      parts.push(band(x2 + 14, s.y, s.h, x3, s.y, s.h, s.color, `${s.name}: ${s.children.toLocaleString()} children reached`));
    });
    const muted = cssVar("--nd-muted");
    const neutral = cssVar("--nd-neutral");
    left.forEach((d) => {
      parts.push(`<rect x="${x1}" y="${d.y}" width="14" height="${d.h}" rx="2" fill="${neutral}"><title>${esc(d.name)}: ${money(d.amount)}</title></rect>`);
      parts.push(`<text x="${x1 - 6}" y="${d.y + d.h / 2 + 4}" text-anchor="end">${esc(d.name.length > 22 ? `${d.name.slice(0, 21)}…` : d.name)} <tspan class="flow__muted">${money(d.amount)}</tspan></text>`);
    });
    mid.forEach((s) => {
      parts.push(`<rect x="${x2}" y="${s.y}" width="14" height="${s.h}" rx="2" fill="${s.color}"><title>${esc(s.name)}: ${money(s.reserved)} reserved</title></rect>`);
      parts.push(`<text x="${x2 + 20}" y="${s.y + s.h / 2 + 4}">${esc(s.name)}</text>`);
      parts.push(`<rect x="${x3}" y="${s.y}" width="14" height="${s.h}" rx="2" fill="${s.color}"></rect>`);
      parts.push(`<text x="${x3 + 20}" y="${s.y + s.h / 2 + 4}">${s.children.toLocaleString()}</text>`);
    });
    const head = `<text x="${x1}" y="18" class="flow__muted">Donors</text><text x="${x2}" y="18" class="flow__muted">Sections</text><text x="${x3}" y="18" class="flow__muted">Children reached</text>`;
    el.innerHTML = `<svg class="flow" viewBox="0 0 ${W + 120} ${H}" role="img" aria-label="${esc(el.getAttribute("aria-label") || "")}" style="color:${muted}">${head}${parts.join("")}</svg>`;
    return null;
  },
  "hbars-flag"(el, data) {
    // [{label, value, flag, days}] -> horizontal bars, flagged ones in the danger colour
    const rows = (Array.isArray(data) ? data : []).slice().reverse();
    const prefix = el.dataset.prefix || "";
    return {
      traces: [{ type: "bar", orientation: "h", y: rows.map((r) => String(r.label)), x: rows.map((r) => num(r.value)), customdata: rows.map((r) => (r.days == null ? "no expiry" : `${r.days} days`)), marker: { color: rows.map((r) => cssVar(r.flag ? "--nd-danger" : "--nd-primary")), line: { width: 0 } }, hovertemplate: `%{y}: ${prefix}%{x:,.0f} unspent · %{customdata}<extra></extra>` }],
      layout: { bargap: 0.3, yaxis: { type: "category" }, xaxis: { rangemode: "tozero" }, margin: { t: 4, r: 16, b: 32, l: 8 }, height: rowsHeight(el, rows.length, 26, 60) },
    };
  },
  funded(el, data) {
    // [{section, required, reserved, percent}] -> required (grey) with reserved (accent) over it
    const rows = (Array.isArray(data) ? data : []).slice().sort((a, b) => num(a.required) - num(b.required));
    const names = rows.map((r) => String(r.section));
    return {
      traces: [
        { type: "bar", orientation: "h", name: "Required", y: names, x: rows.map((r) => num(r.required)), width: 0.64, marker: { color: withAlpha(cssVar("--nd-neutral"), 0.28), line: { width: 0 } }, hovertemplate: "%{y} · required: $%{x:,.0f}<extra></extra>" },
        { type: "bar", orientation: "h", name: "Reserved", y: names, x: rows.map((r) => num(r.reserved)), width: 0.38, customdata: rows.map((r) => (r.percent == null ? "–" : `${Math.round(num(r.percent))}% funded`)), marker: { color: cssVar("--nd-primary"), line: { width: 0 } }, hovertemplate: "%{y} · reserved: $%{x:,.0f} (%{customdata})<extra></extra>" },
      ],
      layout: { barmode: "overlay", bargap: 0.2, yaxis: { type: "category" }, xaxis: { rangemode: "tozero" }, margin: { t: 4, r: 16, b: 56, l: 8 }, height: rowsHeight(el, rows.length), ...legendFor(2) },
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
  const built = builder(el, data, source.labels);
  if (!built) return; // the builder drew the element itself (SVG)
  const { traces, layout } = built;
  window.Plotly.react(el, traces, baseLayout(el, layout), CONFIG);
}

export async function init(el) {
  el.setAttribute("role", el.getAttribute("role") || "img");
  await loadScript("plotly");
  render(el);
  document.addEventListener("nd:themechange", () => render(el));
  // A chart that shows one series at a time redraws when its <select> changes.
  if (el.dataset.select) document.getElementById(el.dataset.select)?.addEventListener("change", () => render(el));
}
