// Plotly charts declared in templates:
//   <div data-module="charts" data-chart="status-donut" data-source="chart-data" data-key="status_counts"></div>
// The JSON comes from {{ chart_data|json_script:"chart-data" }}. Charts follow the light/dark theme.
import { cssVar, debounce, escapeHTML, isDark, loadScript, readJSON } from "./lib.js";

const STATUS_ORDER = ["on_track", "over_target", "off_track", "no_target", "not_reported"];
const STATUS_VARS = { on_track: "--nd-success", over_target: "--nd-warning", off_track: "--nd-danger", no_target: "--nd-neutral", not_reported: "--nd-muted", constrained: "--nd-warning", not_monitored: "--nd-muted" };
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
    width: Number(el.dataset.width) || undefined,
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
// A chart of a printable report (data-static): drawn once at its fixed width (data-width), no hover, no zoom
const STATIC_CONFIG = { ...CONFIG, responsive: false, staticPlot: true };

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

/** A legend under the plot when the chart has two or more series, none otherwise. It sits at the foot of the
 * whole chart (container coordinates), so Plotly makes room for it under the axis labels: placed under the
 * plot area instead, it covered the labels where a narrow chart turns them on their side. */
const legendFor = (count) =>
  count >= 2
    ? { showlegend: true, legend: { orientation: "h", x: 0, yref: "container", y: 0, yanchor: "bottom", font: { size: 11 } } }
    : { showlegend: false };

/** Height for horizontal category charts: the declared minimum, or one row per category. */
const rowsHeight = (el, count, row = 28, extra = 84) => Math.max(Number(el.dataset.height) || 0, count * row + extra);

function emptyState(el) {
  window.Plotly?.purge(el);
  el.innerHTML = `<div class="state state--empty"><p class="state__title">${escapeHTML(el.dataset.emptyTitle || "No data yet")}</p></div>`;
  return null;
}

// ---- helpers shared by the category builders (dist, donut, share-bar)
const CATEGORICAL = 10;
const DARK_INK = "#0f141b";

/** --nd-cat-1 … --nd-cat-10 (themed in app.css); the fixed PALETTE where a token is missing. */
const categorical = () => Array.from({ length: CATEGORICAL }, (_, i) => cssVar(`--nd-cat-${i + 1}`) || PALETTE[i]);

/** "#446ab3", "--nd-cat-2" or "var(--nd-cat-2)" -> a colour Plotly can draw. */
function resolveColor(value) {
  const text = String(value ?? "").trim();
  const token = /^var\(\s*(--[\w-]+)\s*\)$/.exec(text)?.[1] || (text.startsWith("--") ? text : "");
  return token ? cssVar(token) : text;
}

/** pairs, keeping the colour and the drill value an item may carry ([{label, value, color, drill}]); a value
 * that is not a number counts 0. */
function items(data) {
  const own = (d, key) => (d && typeof d === "object" && !Array.isArray(d) ? d[key] : null);
  const colors = Array.isArray(data) ? data.map((d) => own(d, "color")) : [];
  const drills = Array.isArray(data) ? data.map((d) => own(d, "drill")) : [];
  return pairs(data).map(([label, value], i) => ({ label, value: Number.isFinite(value) ? value : 0, color: colors[i] || null, drill: drills[i] ?? null }));
}

// ---- click-through (data-href-template): the drill value travels with each point, never read back from a
// drawn label (dist draws categories by index, hbars cuts long labels, grouped series carry display names)

/** trace.meta for a builder fed {drill: {labels: [per x], series: {name: code}}}: undefined without drill values. */
function drillMeta(data, name) {
  const labels = Array.isArray(data?.drill?.labels) ? data.drill.labels.map((v) => (v == null ? null : String(v))) : null;
  const series = data?.drill?.series?.[name];
  if (!labels && series == null) return undefined;
  return { drill: labels || [], series: series == null ? null : String(series) };
}

/** trace.meta from one drill value per point, in the order the points are drawn; undefined when none has one. */
const pointMeta = (drills) => (drills.some((d) => d != null && d !== "") ? { drill: drills.map((d) => (d == null ? null : String(d))) } : undefined);

/** The address a click on ``point`` opens: the template with {drill} (the point's own drill value), {series_drill}
 * (its trace's code) and, only when the chart carries no drill values, {label} and {series} (the trace name), all
 * URI-encoded; null when a placeholder has no value, so a point without a drill value is not clickable. */
export function drillUrl(template, point) {
  const meta = point?.data?.meta || {};
  const carried = Array.isArray(meta.drill) && meta.drill.length > 0;
  const values = {
    drill: carried ? point.data.meta.drill[point.pointIndex ?? point.pointNumber] : undefined,
    series_drill: meta.series,
    label: carried ? undefined : point?.label ?? point?.x,
    series: carried ? undefined : point?.data?.name,
  };
  let missing = false;
  const url = String(template).replace(/\{(drill|series_drill|label|series)\}/g, (_, key) => {
    const value = values[key];
    if (value === undefined || value === null || value === "") {
      missing = true;
      return "";
    }
    return encodeURIComponent(String(value));
  });
  return missing ? null : url;
}

/** Each item its own colour, else the next categorical slot; data-palette="single" paints all in --nd-primary.
 * A chart read through its legend (keyed: donut, share-bar) paints the items past the tenth in --nd-neutral
 * rather than a slot already used: an eleventh slice would take the blue of the first, right next to it. */
function itemColors(el, rows, keyed = false) {
  if (el.dataset.palette === "single") return rows.map(() => cssVar("--nd-primary"));
  const palette = categorical();
  const neutral = cssVar("--nd-neutral") || PALETTE[8];
  const slot = (i) => (keyed && i >= palette.length ? neutral : palette[i % palette.length]);
  return rows.map((r, i) => (r.color && resolveColor(r.color)) || slot(i));
}

const luminance = (hex) =>
  [0, 2, 4]
    .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
    .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);

/** White or near-black text on a fill, whichever contrasts more (WCAG); white on a colour that is not hex. */
function inkOn(color) {
  const hex = /^#([0-9a-f]{6})$/i.exec(String(color).trim())?.[1];
  if (!hex) return "#ffffff";
  const fill = luminance(hex) + 0.05;
  return 1.05 / fill >= fill / (luminance(DARK_INK.slice(1)) + 0.05) ? "#ffffff" : DARK_INK;
}

/** Plotly reads text as light HTML: a label such as "SAM (MUAC <11.5 cm)" must not open a tag. */
const plotlyText = (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const countFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
/** 9906 -> "9,906" */
const count = (v) => countFormat.format(v);

/** Whole percent of the total ("27%"); a share that is not zero never shows as "0%". */
function wholePercent(value, total) {
  const share = total ? (100 * value) / total : 0;
  return share > 0 && share < 0.5 ? "<1%" : `${Math.round(share)}%`;
}

/** The text on a bar or slice for data-show: percent -> "27%", value -> "9,906", both -> "9,906 (27%)". */
function shownText(show, value, total) {
  if (show === "value") return count(value);
  if (show === "both") return `${count(value)} (${wholePercent(value, total)})`;
  return wholePercent(value, total);
}

/** A long category label cut near max characters, at a word when one ends close by, with an ellipsis. */
function shorten(label, max) {
  if (label.length <= max) return label;
  const cut = label.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:(/–-]+$/, "")}…`;
}

/** Words wrapped into lines of about width characters; a line may also break after "-" or "/"
 * ("Baalbek-Hermel" -> "Baalbek-", "Hermel"). */
function wrap(label, width) {
  const parts = label.split(/\s+/).flatMap((word, i) => word.split(/(?<=[-/])(?=.)/).map((part, j) => ({ part, space: i > 0 && j === 0 })));
  return parts.reduce((lines, { part, space }) => {
    const last = lines.length - 1;
    const joined = last >= 0 ? `${lines[last]}${space ? " " : ""}${part}` : "";
    if (last >= 0 && joined.length <= width) lines[last] = joined;
    else lines.push(part);
    return lines;
  }, []);
}

/** wrap() kept to count lines, the last one ending with an ellipsis when the label goes on. */
function clampLines(label, width, count) {
  const lines = wrap(label, width);
  if (lines.length <= count) return lines;
  const kept = lines.slice(0, count);
  kept[count - 1] = shorten(`${kept[count - 1]} ${lines.slice(count).join(" ")}`, width);
  return kept;
}

/** Pixels of a line of chart text: a close enough estimate for 11-12px digits and letters. */
const textWidth = (chars, size = 12) => chars * size * 0.58;

/** Label, value and percent for the hover of one item. */
const hoverData = (r, total) => [plotlyText(r.label), count(r.value), plotlyText(wholePercent(r.value, total))];
const HOVER = "<b>%{customdata[0]}</b><br>%{customdata[1]} (%{customdata[2]})<extra></extra>";

/** A donut's legend goes under it when the box is narrow (charts redraw when the box crosses this width). */
const NARROW = 480;
const WIDTH_AWARE = ["dist", "donut"];
const isNarrow = (el) => (el.clientWidth || NARROW) < NARROW;

/** Pixels of a legend laid under a chart: its items (each a list of lines) flow in rows across width. */
function legendUnderHeight(entries, width) {
  const rows = [];
  let x = width;
  entries.forEach((lines) => {
    const item = 40 + textWidth(Math.max(...lines.map((line) => line.length)));
    if (x + item > width) {
      rows.push(0);
      x = 0;
    }
    x += item;
    rows[rows.length - 1] = Math.max(rows[rows.length - 1], lines.length);
  });
  return rows.reduce((sum, lines) => sum + lines * 15 + 6, 0);
}

export const BUILDERS = {
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
  trend(el, data) {
    // {series: [{id, label, unit, dates, values}], default}: one measure of a periodic report at a time
    // (picked in the <select> named by data-select), its values by date.
    const select = el.dataset.select ? document.getElementById(el.dataset.select) : null;
    const list = data?.series || [];
    const series = list.find((s) => String(s.id) === String(select?.value ?? data?.default)) || list[0];
    if (!series || !series.dates.length) {
      emptyState(el);
      return null;
    }
    const unit = series.unit ? ` ${plotlyText(series.unit)}` : "";
    const color = cssVar("--nd-primary");
    return {
      traces: [
        {
          type: "scatter",
          mode: "lines+markers",
          x: series.dates,
          y: series.values,
          line: { color, width: 2 },
          marker: { size: 6, color },
          fill: "tozeroy",
          fillcolor: withAlpha(color, 0.12),
          hovertemplate: `%{x|%d %b %Y}: %{y:,.1~f}${unit}<extra></extra>`,
        },
      ],
      layout: {
        xaxis: { type: "date", tickformat: "%d %b" },
        yaxis: { rangemode: "tozero" },
        margin: { t: 8, r: 12, b: 40, l: 56 },
      },
    };
  },
  monthly(el, data) {
    // {months, indicators: [{id, label, unit, values, reports}], default, bar_name?, line_name?, line_unit?,
    // drill?: {labels, series}}: one indicator at a time
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
    const unit = series.unit ? (series.unit === "%" ? "%" : ` ${series.unit}`) : "";
    // optional names of the bars and the line, and the line's unit ("%" draws one decimal)
    const barName = data.bar_name || "Reported in the month";
    const lineName = data.line_name || "Records";
    const lineUnit = data.line_unit || "records";
    const lineHover = lineUnit === "%" ? "%{x}: %{y:,.1~f}%<extra></extra>" : `%{x}: %{y:,} ${plotlyText(lineUnit)}<extra></extra>`;
    return {
      traces: [
        {
          type: "bar",
          x: months,
          y: series.values.map((v) => (v === null ? null : num(v))),
          marker: { color: cssVar("--nd-primary"), line: { width: 0 } },
          hovertemplate: `%{x}: %{y:,.1~f}${unit}<extra></extra>`,
          name: barName,
          meta: drillMeta(data, barName),
        },
        {
          type: "scatter",
          mode: "lines+markers",
          x: months,
          y: series.reports.map((v) => (v === null ? null : num(v))),
          yaxis: "y2",
          line: { color: cssVar("--nd-warning"), width: 2 },
          marker: { size: 5 },
          hovertemplate: lineHover,
          name: lineName,
          meta: drillMeta(data, lineName),
        },
      ],
      layout: {
        bargap: 0.35,
        showlegend: true,
        // at the top of the whole chart, so Plotly makes room for it: above the plot area, it covered the bars
        legend: { orientation: "h", x: 0, yref: "container", y: 1, yanchor: "top" },
        yaxis: { rangemode: "tozero" },
        yaxis2: { overlaying: "y", side: "right", showgrid: false, rangemode: "tozero", tickfont: { color: cssVar("--nd-muted") } },
        margin: { t: 24, r: 44, b: 36, l: 56 },
      },
    };
  },
  "dual-axis"(el, data) {
    // The monthly payload above, drawn as FMS draws it (Monitoring insights' Quality tab), each series on its own
    // axis and the legend centred on top: data-primary="area" (quality score trends) gives two smooth lines with
    // markers, the first filled to zero on a 0-100 axis; data-primary="bar" (monitoring volume) gives light bars
    // and a smooth line. data-y-title and data-y2-title name the axes; a point or bar opens its month's visits.
    const series = (data?.indicators || [])[0];
    const months = data?.months || [];
    if (!series || !months.length) return emptyState(el);
    const area = el.dataset.primary === "area";
    const blue = cssVar("--nd-chart-blue");
    const green = cssVar("--nd-chart-green");
    const grid = cssVar("--nd-border");
    const muted = cssVar("--nd-muted");
    const barName = plotlyText(data.bar_name || "Value");
    const lineName = plotlyText(data.line_name || "Records");
    const percentLine = data.line_unit === "%";
    const smooth = { shape: "spline", smoothing: 0.8, width: 2.5 };
    const values = (series.values || []).map((v) => (v === null || v === undefined ? null : num(v)));
    const line = (series.reports || []).map((v) => (v === null || v === undefined ? null : num(v)));
    const primary = area
      ? {
          type: "scatter",
          mode: "lines+markers",
          line: { ...smooth, color: blue },
          marker: { size: 6, color: blue },
          fill: "tozeroy",
          fillcolor: withAlpha(blue, 0.1),
          connectgaps: true,
          hovertemplate: `${barName}: %{y:.1f}%<extra></extra>`,
        }
      : {
          type: "bar",
          marker: { color: withAlpha(cssVar("--nd-chart-sky"), 0.85), line: { width: 0 } },
          hovertemplate: `${barName}: %{y:,}<extra></extra>`,
        };
    const title = (text) => (text ? { title: { text: plotlyText(text), font: { size: 11, color: muted } } } : {});
    return {
      traces: [
        { ...primary, name: barName, x: months, y: values, meta: drillMeta(data, data.bar_name || "Value") },
        {
          type: "scatter",
          mode: "lines+markers",
          name: lineName,
          x: months,
          y: line,
          yaxis: "y2",
          connectgaps: true,
          line: { ...smooth, color: green },
          marker: { size: 6, color: green },
          hovertemplate: percentLine ? `${lineName}: %{y:.1f}%<extra></extra>` : `${lineName}: %{y:,}<extra></extra>`,
          meta: drillMeta(data, data.line_name || "Records"),
        },
      ],
      layout: {
        bargap: 0.3,
        showlegend: true,
        legend: { orientation: "h", x: 0.5, xanchor: "center", yref: "container", y: 1, yanchor: "top" },
        xaxis: { type: "category", tickangle: months.length > 8 ? -45 : 0 },
        yaxis: { ...(area ? { range: [0, 100] } : { rangemode: "tozero" }), ...title(el.dataset.yTitle) },
        yaxis2: {
          overlaying: "y",
          side: "right",
          showgrid: false,
          zeroline: false,
          linecolor: grid,
          tickfont: { color: muted },
          ...(percentLine ? { range: [0, 100], ticksuffix: "%" } : { rangemode: "tozero" }),
          ...title(el.dataset.y2Title),
        },
        hovermode: "x unified",
        margin: { t: 36, r: 56, b: 56, l: 56 },
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
    // {labels, series: {name: [...]}, colors?: {name: "--nd-token"}, drill?: {labels, series: {name: code}}}
    // -> grouped vertical bars; data-barmode="stack" stacks them, data-legend="top" centres the legend above the
    // plot (as FMS draws its HACT Q1 chart) and data-y-title names the value axis
    const { labels, series, colors } = seriesOf(data);
    const top = el.dataset.legend === "top" && series.length >= 2;
    const muted = cssVar("--nd-muted");
    const yTitle = el.dataset.yTitle ? { title: { text: plotlyText(el.dataset.yTitle), font: { size: 11, color: muted } } } : {};
    const legend = top
      ? { showlegend: true, legend: { orientation: "h", x: 0.5, xanchor: "center", yref: "container", y: 1, yanchor: "top", traceorder: "normal" } }
      : legendFor(series.length);
    return {
      traces: series.map(([name, values], i) => ({
        type: "bar",
        name,
        x: labels,
        y: values,
        marker: { color: seriesColor(name, i, colors), line: { width: 0 } },
        hovertemplate: `%{x} · ${name}: %{y:,}<extra></extra>`,
        meta: drillMeta(data, name),
      })),
      layout: {
        barmode: el.dataset.barmode === "stack" ? "stack" : "group",
        bargap: 0.25,
        bargroupgap: 0.06,
        xaxis: { type: "category", ...(top && labels.length > 8 ? { tickangle: -45 } : {}) },
        yaxis: { rangemode: "tozero", ...yTitle },
        margin: top ? { t: 36, r: 8, b: 56, l: 56 } : { t: 8, r: 8, b: 56, l: 40 },
        ...legend,
      },
    };
  },
  "share-lines"(el, data) {
    // {labels, series: {name: [share or null]}, drill?: {labels, series: {name: code}}} -> one line per series
    // on a 0-100 % axis; a month without a value is a gap, never a 0 (Monitoring insights' rule trends)
    const labels = Array.isArray(data?.labels) ? data.labels.map(String) : [];
    const series = Object.entries(data?.series || {}).map(([name, values]) => [name, (values || []).map((v) => (v === null || v === undefined ? null : Number(v)))]);
    if (!labels.length || !series.some(([, values]) => values.some((v) => v !== null))) return emptyState(el);
    return {
      traces: series.map(([name, values], i) => {
        const color = PALETTE[i % PALETTE.length];
        return {
          type: "scatter",
          mode: "lines+markers",
          name: plotlyText(name),
          x: labels,
          y: values,
          connectgaps: false,
          line: { color, width: 2 },
          marker: { size: 6, color },
          hovertemplate: `%{x} · ${plotlyText(name)}: %{y:.1f}%<extra></extra>`,
          meta: drillMeta(data, name),
        };
      }),
      layout: {
        xaxis: { type: "category" },
        yaxis: { range: [0, 105], ticksuffix: "%" },
        margin: { t: 8, r: 12, b: 56, l: 48 },
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
    // data-color-by="status" colours each bar by its label ("On Track" -> success); a third element of a
    // pair ([label, value, drill]) is the bar's drill value
    const drills = Array.isArray(data) ? data.map((d) => (Array.isArray(d) ? d[2] : d?.drill) ?? null) : [];
    const rows = pairs(data)
      .map((r, i) => [r[0], r[1], drills[i] ?? null])
      .reverse();
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
          meta: pointMeta(rows.map((r) => r[2])),
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
  // ---- education dashboards: one colour per category, the figure written on each bar or slice
  dist(el, data) {
    // pairs or [{label, value, color?}] -> one bar per category, in the order given. data-orientation
    // v (columns) | h (first item on top); data-show percent | value | both; data-palette categorical |
    // single; data-max-label (28): longer labels are cut, the hover keeps them whole
    const rows = items(data);
    const total = rows.reduce((sum, r) => sum + r.value, 0);
    if (!total) return emptyState(el);
    const horizontal = el.dataset.orientation === "h";
    const show = ["value", "both"].includes(el.dataset.show) ? el.dataset.show : "percent";
    const max = Number(el.dataset.maxLabel) || 28;
    const size = rows.length > 20 ? 11 : 12;
    const short = rows.map((r) => shorten(r.label, max));
    // columns: the room of one column decides how labels and figures are written (the chart redraws
    // when its box changes width)
    const slot = (Math.max(el.clientWidth || 480, 240) - 16) / rows.length;
    // labels: up to three short lines under each column, else one line that Plotly turns
    const perLine = Math.floor(slot / textWidth(1));
    const wrapped = short.map((label) => wrap(label, perLine));
    const upright = !horizontal && wrapped.every((lines) => lines.length <= 3 && lines.every((line) => line.length <= perLine));
    const ticks = (upright ? wrapped : short.map((label) => [label])).map((lines) => lines.map(plotlyText).join("<br>"));
    // figures: "9,906 (25%)" on two lines above a column when that fits it, else turned upright
    const lines = rows.map((r) => (show === "both" && !horizontal ? [count(r.value), `(${wholePercent(r.value, total)})`] : [shownText(show, r.value, total)]));
    const widest = Math.max(...lines.flat().map((t) => t.length));
    const turned = !horizontal && textWidth(widest, size) > slot * 0.95;
    const text = turned ? rows.map((r) => shownText(show, r.value, total)) : lines.map((l) => l.map(plotlyText).join("<br>"));
    const index = rows.map((_, i) => i);
    const values = rows.map((r) => r.value);
    const grid = cssVar("--nd-border");
    // the category axis carries the labels by position, so two labels cut to the same text stay two bars
    const categoryAxis = { tickmode: "array", tickvals: index, ticktext: ticks, showgrid: false, zeroline: false, showline: true, linecolor: grid, ticks: "", fixedrange: true, ...(horizontal ? { autorange: "reversed" } : upright ? { tickangle: 0 } : {}) };
    const valueAxis = { showgrid: false, zeroline: false, showticklabels: false, rangemode: "tozero", fixedrange: true };
    // the figures sit past the bar ends: room for them above the columns or right of the bars
    const longest = Math.max(...rows.map((r) => shownText(show, r.value, total).length));
    const margin = horizontal
      ? { t: 4, r: 12 + textWidth(longest, size), b: 8, l: 8 }
      : { t: turned ? 10 + textWidth(longest, size) : 6 + 16 * Math.max(...lines.map((l) => l.length)), r: 8, b: 8, l: 8 };
    return {
      traces: [
        {
          type: "bar",
          orientation: horizontal ? "h" : "v",
          x: horizontal ? values : index,
          y: horizontal ? index : values,
          text: turned ? text.map(plotlyText) : text,
          textposition: "outside",
          textangle: turned ? -90 : 0,
          cliponaxis: false,
          constraintext: "none",
          textfont: { color: cssVar("--nd-text"), size },
          customdata: rows.map((r) => hoverData(r, total)),
          marker: { color: itemColors(el, rows), line: { width: 0 }, cornerradius: 3 },
          hovertemplate: HOVER,
          meta: pointMeta(rows.map((r) => r.drill)),
        },
      ],
      layout: {
        bargap: rows.length > 12 ? 0.2 : 0.35,
        xaxis: horizontal ? valueAxis : categoryAxis,
        yaxis: horizontal ? categoryAxis : valueAxis,
        margin,
        height: horizontal ? rowsHeight(el, rows.length, 30, 24) : Number(el.dataset.height) || undefined,
      },
    };
  },
  donut(el, data) {
    // pairs or [{label, value, color?}] -> donut, zeros left out. data-show both ("9,906 (25%)", the
    // default) | percent | value on each slice, outside the ring when it does not fit inside, none on a
    // slice under half a percent (the legend and hover have it); legend on the right, under the ring in
    // a narrow box, its labels on up to two lines of data-max-label (28) characters at most, fewer when the
    // box is small (one line when two do not fit); the chart grows to hold its legend;
    // data-center-total="1" writes the total in the hole (data-center-label under it)
    const rows = items(data).filter((r) => r.value > 0);
    if (!rows.length) return emptyState(el);
    const total = rows.reduce((sum, r) => sum + r.value, 0);
    const show = ["percent", "value"].includes(el.dataset.show) ? el.dataset.show : "both";
    const max = Number(el.dataset.maxLabel) || 28;
    const colors = itemColors(el, rows, true);
    const ink = cssVar("--nd-text");
    const narrow = isNarrow(el);
    const width = el.clientWidth || NARROW;
    // the declared height, else the box's CSS minimum (not its current height, which the last draw set)
    const height = Number(el.dataset.height) || parseFloat(getComputedStyle(el).minHeight) || 260;
    // legend lines are also cut to the room they have: beside the ring under half the box (a wider
    // legend would push into the ring and its labels), under it the whole width
    const perLine = Math.max(12, Math.min(max, Math.floor(((narrow ? width : 0.45 * width) - 40) / textWidth(1))));
    const twoLines = rows.map((r) => clampLines(r.label, perLine, 2));
    const legendHeight = (entries) => (narrow ? legendUnderHeight(entries, width) : entries.flat().length * 15 + entries.length * 6);
    // one line each when two would not fit: beside the ring the height of the box, under it ~240px
    const tall = legendHeight(twoLines) > (narrow ? 240 : height - 32);
    const entries = tall ? rows.map((r) => [shorten(r.label, perLine)]) : twoLines;
    // the box grows when the legend needs it: beside the ring its full height; under it ~200px of ring on
    // top, and twice the legend (Plotly gives a legend under the plot half the height, then scrolls it)
    const needed = narrow ? Math.max(232 + legendHeight(entries), 2 * legendHeight(entries) + 16) : legendHeight(entries) + 32;
    // a pie adds up slices of the same label: two labels cut to the same text are kept apart
    const seen = new Set();
    const labels = entries.map((lines) => {
      let label = lines.map(plotlyText).join("<br>");
      while (seen.has(label)) label += "\u200b"; // zero-width space
      seen.add(label);
      return label;
    });
    const center = el.dataset.centerTotal === "1";
    const caption = el.dataset.centerLabel ? `<br><span style="font-size:11px">${plotlyText(el.dataset.centerLabel)}</span>` : "";
    return {
      traces: [
        {
          type: "pie",
          hole: 0.55,
          sort: false,
          direction: "clockwise",
          labels,
          values: rows.map((r) => r.value),
          text: rows.map((r) => {
            const share = (100 * r.value) / total;
            if (share < 0.5) return "";
            // a large slice takes "9,906 (25%)" on two lines, which fits inside the ring more often
            return show === "both" && share >= 5 ? `${count(r.value)}<br>(${wholePercent(r.value, total)})` : plotlyText(shownText(show, r.value, total));
          }),
          textinfo: "text",
          // inside, or outside when the text would have to shrink; a narrow ring is thin: there large
          // slices keep their label inside (turned, smaller, or left out) so it never meets the legend
          textposition: narrow ? rows.map((r) => ((100 * r.value) / total >= 5 ? "inside" : "outside")) : "auto",
          insidetextorientation: narrow ? "auto" : "horizontal",
          insidetextfont: { color: colors.map(inkOn) },
          outsidetextfont: { color: ink },
          automargin: true,
          marker: { colors, line: { color: cssVar("--nd-surface"), width: 1.5 } },
          customdata: rows.map((r) => hoverData(r, total)),
          hovertemplate: HOVER,
        },
      ],
      layout: {
        showlegend: true,
        legend: narrow ? { orientation: "h", x: 0.5, xanchor: "center", y: -0.04, yanchor: "top" } : { orientation: "v", x: 1.04, xanchor: "left", y: 0.5, yanchor: "middle" },
        margin: { t: 16, r: 8, b: 16, l: 8 },
        height: Math.max(height, needed),
        ...(narrow ? { uniformtext: { mode: "hide", minsize: 9 } } : {}),
        annotations: center ? [{ text: `<b>${count(total)}</b>${caption}`, x: 0.5, y: 0.5, xref: "paper", yref: "paper", showarrow: false, font: { size: 16, color: ink } }] : [],
      },
    };
  },
  "share-bar"(el, data) {
    // pairs or [{label, value, color?}] -> one 100% bar split by category, each part labelled
    // "Female 51%" (hidden when it does not fit; the hover has it), legend above
    const rows = items(data).filter((r) => r.value > 0);
    if (!rows.length) return emptyState(el);
    const total = rows.reduce((sum, r) => sum + r.value, 0);
    const colors = itemColors(el, rows, true);
    const max = Number(el.dataset.maxLabel) || 28;
    const surface = cssVar("--nd-surface");
    return {
      traces: rows.map((r, i) => ({
        type: "bar",
        orientation: "h",
        name: plotlyText(shorten(r.label, max)),
        y: [0],
        x: [(100 * r.value) / total],
        text: [plotlyText(`${shorten(r.label, max)} ${wholePercent(r.value, total)}`)],
        textposition: "inside",
        insidetextanchor: "middle",
        textangle: 0,
        textfont: { color: inkOn(colors[i]), size: 13 },
        marker: { color: colors[i], line: { color: surface, width: 2 } },
        customdata: [hoverData(r, total)],
        hovertemplate: HOVER,
      })),
      layout: {
        barmode: "stack",
        bargap: 0.2,
        showlegend: true,
        legend: { orientation: "h", x: 0, xanchor: "left", y: 1, yanchor: "bottom", traceorder: "normal" },
        uniformtext: { mode: "hide", minsize: 10 },
        xaxis: { range: [0, 100], visible: false, fixedrange: true },
        yaxis: { visible: false, fixedrange: true },
        margin: { t: 8, r: 4, b: 4, l: 4 },
        height: Number(el.dataset.height) || 110,
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
    emptyState(el);
    return;
  }
  const built = builder(el, data, source.labels);
  if (!built) return; // the builder drew the element itself (SVG)
  const { traces, layout } = built;
  window.Plotly.react(el, traces, baseLayout(el, layout), el.dataset.static ? STATIC_CONFIG : CONFIG);
}

let hints = 0;

/** A button with data-chart-png="#chart-id" saves that chart as a PNG (Plotly's own downloadImage: no other
 * library); data-filename names the file. One listener for the page, as the buttons come and go with HTMX. */
function savePng(event) {
  const button = event.target.closest?.("[data-chart-png]");
  if (!button) return;
  const el = document.querySelector(button.dataset.chartPng);
  if (!el || !window.Plotly || !el.data) return;
  const width = Math.max(el.clientWidth || 0, 640);
  const height = Math.max(el.clientHeight || 0, 360);
  window.Plotly.downloadImage(el, { format: "png", filename: button.dataset.filename || "chart", width, height });
}
if (typeof document !== "undefined" && typeof document.addEventListener === "function" && !window.__ndChartPng) {
  window.__ndChartPng = true;
  document.addEventListener("click", savePng);
}

/** A chart with data-href-template opens the visits behind a bar: a click fills the template from the point's
 * drill value (drillUrl) and loads it into data-href-target (default the modal, which app.js opens). */
function clickThrough(el) {
  const template = el.dataset.hrefTemplate;
  if (!template) return null;
  const onClick = (event) => {
    const point = event?.points?.[0];
    const url = point ? drillUrl(template, point) : null;
    if (url && window.htmx) window.htmx.ajax("GET", url, { target: el.dataset.hrefTarget || "#modal-content" });
  };
  el.classList.add("chart--clickable");
  const hint = document.createElement("span");
  hint.className = "visually-hidden";
  hint.id = `chart-drill-hint-${++hints}`;
  hint.textContent = "Click a bar to list its visits";
  el.after(hint);
  el.setAttribute("aria-describedby", hint.id);
  // bound once per plot: a plot purged (an empty state) and drawn again gets a new emitter
  let boundTo = null;
  return {
    bind() {
      if (typeof el.on === "function" && boundTo !== el.on) {
        el.on("plotly_click", onClick);
        boundTo = el.on;
      }
    },
    release() {
      el.removeListener?.("plotly_click", onClick);
      boundTo = null;
    },
  };
}

export async function init(el) {
  el.setAttribute("role", el.getAttribute("role") || "img");
  await loadScript("plotly");
  if (!el.isConnected) return; // swapped out while Plotly loaded
  const click = clickThrough(el);
  const draw = () => {
    render(el);
    click?.bind();
  };
  draw();
  let observer = null;
  const select = el.dataset.select ? document.getElementById(el.dataset.select) : null;
  const redrawTheme = () => (el.isConnected ? draw() : release());
  const redrawSelect = () => draw();
  // A chart swapped out by HTMX (a filter change) stops listening and frees its plot; one removed some
  // other way does so at the next theme change.
  function release() {
    document.removeEventListener("nd:themechange", redrawTheme);
    select?.removeEventListener("change", redrawSelect);
    observer?.disconnect();
    click?.release();
    window.Plotly?.purge(el);
  }
  el.addEventListener("htmx:beforeCleanupElement", (e) => e.target === el && release());
  document.addEventListener("nd:themechange", redrawTheme);
  // A chart that shows one series at a time redraws when its <select> changes.
  select?.addEventListener("change", redrawSelect);
  // A donut puts its legend under the ring in a narrow box, columns fit their labels to their width:
  // they redraw when the box width changes.
  if (WIDTH_AWARE.includes(el.dataset.chart) && window.ResizeObserver) {
    let width = el.clientWidth;
    const redraw = debounce(() => {
      if (!el.isConnected) return release();
      const crossed = el.clientWidth < NARROW !== width < NARROW;
      if (!crossed && Math.abs(el.clientWidth - width) < 24) return;
      width = el.clientWidth;
      draw();
    }, 200);
    observer = new ResizeObserver(redraw);
    observer.observe(el);
  }
}
