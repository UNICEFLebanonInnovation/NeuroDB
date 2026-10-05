// Education maps (Makani, Dirasa): MapLibre (CSP build) with OpenStreetMap tiles. The page gives the
// data in a json_script named by data-config:
//   {"mode": "choropleth", "geojson": {FeatureCollection, properties {name, value, label}}, "value_label": "children"}
//   {"mode": "points", "points": [{name, latitude, longitude, color, group, lines: [[key, value], ...]}],
//    "legend": [{label, color}]}
// Colours may be hex or "var(--nd-cat-3)"; they follow the light/dark theme. A table next to the map
// (rendered by the page) is the text alternative, and the keyboard route: popups open on hover or tap.
// Points mode also reads, all optional:
//   point.href     a click or tap opens it: into the modal when config.open (or point.open) is "modal",
//                  else as a page (same-site addresses only)
//   point.shape    "ring": a hollow circle in the point's colour (e.g. a planned place not yet visited)
//   point.opacity  0..1, default 1 (e.g. 0.6 for an approximate point)
//   legend[].toggle with legend[].group: the entry is a button that shows or hides that group's points
//   legend[].shape "ring": a hollow swatch
//   config.focus   a point's name: the map centres on it and opens its popup
import { asset, cssVar, escapeHTML, fmt, isDark, loadScript, loadStyle, readJSON } from "./lib.js";

const LEBANON = { center: [35.86, 33.87], zoom: 7.3 };
const RAMP = ["--nd-ramp-2", "--nd-ramp-3", "--nd-ramp-4", "--nd-ramp-5", "--nd-ramp-6"];
const RAMP_FALLBACK = ["#c9d6ef", "#a3b9e3", "#7391cf", "#446ab3", "#2b4b87"];
const NO_DATA = "rgba(150,150,150,0.12)";
// the OpenStreetMap tiles are light: in the dark theme they are dimmed so the page and the ramp read the same way
const basemapBrightness = () => (isDark() ? 0.55 : 1);

function styleSpec() {
  return {
    version: 8,
    sources: {
      osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" },
    },
    layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.6, "raster-opacity": 0.85, "raster-brightness-max": basemapBrightness() } }],
  };
}

/** "#446ab3", "--nd-cat-2" or "var(--nd-cat-2)" -> a colour MapLibre can draw. */
function resolveColor(value, fallback) {
  const text = String(value ?? "").trim();
  const token = /^var\(\s*(--[\w-]+)\s*\)$/.exec(text)?.[1] || (text.startsWith("--") ? text : "");
  return (token ? cssVar(token) : text) || fallback;
}

const numeric = (v) => (v === null || v === undefined || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));

function bounds(features) {
  let minX = 180, minY = 90, maxX = -180, maxY = -90;
  const visit = (c) => {
    if (!Array.isArray(c)) return; // a geometry without coordinates (GeometryCollection, broken feature)
    if (typeof c[0] === "number") {
      if (!Number.isFinite(c[0]) || !Number.isFinite(c[1])) return;
      minX = Math.min(minX, c[0]); maxX = Math.max(maxX, c[0]);
      minY = Math.min(minY, c[1]); maxY = Math.max(maxY, c[1]);
    } else c.forEach(visit);
  };
  features.forEach((f) => visit(f.geometry.coordinates));
  return minX <= maxX ? [[minX, minY], [maxX, maxY]] : null;
}

// ------------------------------------------------------------------ choropleth
/** The areas with a value, the value range and five equal steps from the smallest value to the largest. */
function classes(geojson) {
  const features = (geojson?.features || []).filter((f) => f?.geometry);
  features.forEach((f) => {
    f.properties = { ...(f.properties || {}), value: numeric(f.properties?.value) };
  });
  const values = features.map((f) => f.properties.value).filter((v) => v !== null && v > 0);
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  return { features, min, max, located: values.length };
}

function fillColor({ min, max }) {
  const colours = RAMP.map((token, i) => cssVar(token) || RAMP_FALLBACK[i]);
  // MapLibre needs strictly ascending steps: with one value (or all equal) every area takes the top step
  const steps = max > min ? colours.slice(1).flatMap((colour, i) => [min + ((max - min) * (i + 1)) / RAMP.length, colour]) : [];
  const ramp = steps.length ? ["step", ["get", "value"], colours[0], ...steps] : colours[RAMP.length - 1];
  return ["case", ["any", ["==", ["get", "value"], null], ["<=", ["get", "value"], 0]], NO_DATA, ramp];
}

/** The legend box shows html, or stays hidden when there is nothing to say. */
function setLegend(legend, html) {
  if (!legend) return;
  legend.innerHTML = html;
  legend.hidden = !html;
}

const nothingToMap = (config) => `<div class="map-legend__title">${escapeHTML(config.empty_title || "Nothing to map for these filters")}</div>`;

function choroplethLegend(legend, stats, config) {
  if (!stats.located) return setLegend(legend, nothingToMap(config));
  const colours = RAMP.map((token, i) => cssVar(token) || RAMP_FALLBACK[i]);
  const stops = colours.map((c, i) => `${c} ${i * 20}% ${(i + 1) * 20}%`).join(", ");
  return setLegend(
    legend,
    `<div class="map-legend__title">${escapeHTML(config.value_label || "Value")}</div>
    <div class="map-legend__ramp" style="background: linear-gradient(90deg, ${stops})"></div>
    <div class="map-legend__scale"><span>${fmt(stats.min)}</span><span>${fmt(stats.max)}</span></div>`,
  );
}

function choropleth(map, legend, config, popup) {
  const stats = classes(config.geojson);
  map.addSource("areas", { type: "geojson", data: { type: "FeatureCollection", features: stats.features } });
  map.addLayer({ id: "areas-fill", type: "fill", source: "areas", paint: { "fill-color": fillColor(stats), "fill-opacity": 0.8 } });
  map.addLayer({ id: "areas-line", type: "line", source: "areas", paint: { "line-color": cssVar("--nd-surface") || "#ffffff", "line-width": 1 } });
  const html = (p) => {
    const value = numeric(p.value);
    const label = p.label || (value === null ? "No data" : `${fmt(value)}${config.value_label ? ` ${config.value_label}` : ""}`);
    return `<strong>${escapeHTML(p.name || p.code || "—")}</strong><span class="map-popup__line">${escapeHTML(label)}</span>`;
  };
  hoverPopup(map, "areas-fill", popup, html);
  choroplethLegend(legend, stats, config);
  return {
    features: stats.features,
    recolour() {
      map.setPaintProperty("areas-fill", "fill-color", fillColor(stats));
      map.setPaintProperty("areas-line", "line-color", cssVar("--nd-surface") || "#ffffff");
      choroplethLegend(legend, stats, config);
    },
  };
}

// ------------------------------------------------------------------ points
const DOT_LAYER = "points";
const RING_LAYER = "points-ring";
// a per-point opacity, 1 when the point gives none
const OPACITY = ["coalesce", ["get", "opacity"], 1];
// only addresses of this site are followed from a point
const isLocalHref = (href) => typeof href === "string" && /^\/(?![/\\])/.test(href);

/** The points with coordinates as GeoJSON features (MapLibre keeps flat properties only: the popup
 * lines travel as JSON). */
export function pointFeatures(points) {
  return (points || [])
    .filter((p) => numeric(p.latitude) !== null && numeric(p.longitude) !== null)
    .map((p, i) => {
      const properties = {
        name: p.name || "",
        group: p.group || "",
        color: p.color || "",
        lines: JSON.stringify(p.lines || []),
        shape: p.shape === "ring" ? "ring" : "dot",
      };
      if (isLocalHref(p.href)) properties.href = p.href;
      if (p.open === "modal" || p.open === "page") properties.open = p.open;
      const opacity = numeric(p.opacity);
      if (opacity !== null) properties.opacity = Math.min(1, Math.max(0, opacity));
      return { type: "Feature", id: i, geometry: { type: "Point", coordinates: [Number(p.longitude), Number(p.latitude)] }, properties };
    });
}

/** The filter of one circle layer: its shape, without the groups the legend has hidden. */
export function layerFilter(shape, hidden = new Set()) {
  const own = ["==", ["get", "shape"], shape];
  return hidden.size ? ["all", own, ["!", ["in", ["get", "group"], ["literal", [...hidden]]]]] : own;
}

/** The legend's HTML: a swatch and a label per entry; an entry with toggle and group is a button
 * that shows or hides the group (aria-pressed: shown). */
export function legendHTML(config, located, hidden = new Set(), resolve = resolveColor) {
  const entries = config.legend || [];
  if (!located) return nothingToMap(config);
  if (!entries.length) return "";
  const fallback = cssVar("--nd-primary");
  const items = entries
    .map((e) => {
      const colour = escapeHTML(resolve(e.color, fallback));
      const swatch =
        e.shape === "ring"
          ? `<span class="legend__swatch legend__swatch--ring" style="border-color: ${colour}"></span>`
          : `<span class="legend__swatch legend__swatch--dot" style="background: ${colour}"></span>`;
      if (e.toggle && e.group) {
        const shown = !hidden.has(e.group);
        return `<button type="button" class="legend__item legend__toggle" data-group="${escapeHTML(e.group)}" aria-pressed="${shown}">${swatch}${escapeHTML(e.label)}</button>`;
      }
      return `<span class="legend__item">${swatch}${escapeHTML(e.label)}</span>`;
    })
    .join("");
  return `${config.legend_title ? `<div class="map-legend__title">${escapeHTML(config.legend_title)}</div>` : ""}<div class="legend">${items}</div>`;
}

/** Open a point's address: into the modal (app.js shows it) or as a page. */
function openPoint(properties, config) {
  if (!isLocalHref(properties.href)) return;
  const how = properties.open || config.open;
  if (how === "modal" && window.htmx) window.htmx.ajax("GET", properties.href, { target: "#modal-content" });
  else window.location.assign(properties.href);
}

function points(map, legend, config, popup) {
  const features = pointFeatures(config.points);
  const hidden = new Set();
  const colour = () => {
    const fallback = cssVar("--nd-primary") || "#446ab3";
    features.forEach((f) => {
      f.properties.fill = resolveColor(f.properties.color, fallback);
    });
    return { type: "FeatureCollection", features };
  };
  const radius = ["interpolate", ["linear"], ["zoom"], 7, 5, 11, 8];
  map.addSource("points", { type: "geojson", data: colour() });
  // the rings first, so a visit drawn at a planned place stays on top
  map.addLayer({
    id: RING_LAYER,
    type: "circle",
    source: "points",
    filter: layerFilter("ring", hidden),
    paint: {
      "circle-radius": radius,
      "circle-color": "rgba(0, 0, 0, 0)",
      "circle-opacity": OPACITY,
      "circle-stroke-color": ["get", "fill"],
      "circle-stroke-width": 2,
      "circle-stroke-opacity": OPACITY,
    },
  });
  map.addLayer({
    id: DOT_LAYER,
    type: "circle",
    source: "points",
    filter: layerFilter("dot", hidden),
    paint: {
      "circle-radius": radius,
      "circle-color": ["get", "fill"],
      "circle-opacity": ["*", 0.9, OPACITY],
      "circle-stroke-color": cssVar("--nd-surface") || "#ffffff",
      "circle-stroke-width": 1.5,
      "circle-stroke-opacity": OPACITY,
    },
  });
  const html = (p) => {
    let lines = [];
    try {
      lines = JSON.parse(p.lines || "[]");
    } catch {
      lines = [];
    }
    const rows = (Array.isArray(lines) ? lines : []).filter(Array.isArray).map(([key, value]) => `<span class="map-popup__line"><span>${escapeHTML(key)}:</span> ${escapeHTML(value ?? "—")}</span>`).join("");
    return `<strong>${escapeHTML(p.name || "—")}</strong>${rows}`;
  };
  const layers = [RING_LAYER, DOT_LAYER];
  hoverPopup(map, layers, popup, html);
  layers.forEach((layer) => map.on("click", layer, (e) => openPoint(e.features[0].properties, config)));

  const drawLegend = () => setLegend(legend, legendHTML(config, features.length, hidden));
  legend?.addEventListener("click", (e) => {
    const button = e.target.closest("[data-group]");
    if (!button) return;
    const group = button.dataset.group;
    if (hidden.has(group)) hidden.delete(group);
    else hidden.add(group);
    map.setFilter(RING_LAYER, layerFilter("ring", hidden));
    map.setFilter(DOT_LAYER, layerFilter("dot", hidden));
    popup.remove();
    drawLegend();
    legend.querySelector(`[data-group="${CSS.escape(group)}"]`)?.focus();
  });
  drawLegend();
  return {
    features,
    focus(name) {
      const found = features.find((f) => f.properties.name === name);
      if (!found) return;
      map.jumpTo({ center: found.geometry.coordinates, zoom: Math.max(map.getZoom(), 11) });
      popup.setLngLat(found.geometry.coordinates).setHTML(html(found.properties)).addTo(map);
    },
    recolour() {
      map.getSource("points").setData(colour());
      map.setPaintProperty(DOT_LAYER, "circle-stroke-color", cssVar("--nd-surface") || "#ffffff");
      drawLegend();
    },
  };
}

// ------------------------------------------------------------------ shared
/** A popup on hover, and on tap (touch screens have no hover); a tap elsewhere closes it. ``layer`` is
 * one layer id or several. */
function hoverPopup(map, layer, popup, html) {
  const layers = Array.isArray(layer) ? layer : [layer];
  const show = (e) => {
    map.getCanvas().style.cursor = "pointer";
    popup.setLngLat(e.lngLat).setHTML(html(e.features[0].properties)).addTo(map);
  };
  layers.forEach((id) => {
    map.on("mousemove", id, show);
    map.on("click", id, show);
    map.on("mouseleave", id, () => {
      map.getCanvas().style.cursor = "";
      popup.remove();
    });
  });
  map.on("click", (e) => {
    if (!map.queryRenderedFeatures(e.point, { layers }).length) popup.remove();
  });
}

export async function init(el) {
  const config = readJSON(el.dataset.config || "map-config") || {};
  const canvas = el.querySelector("[data-map-canvas]");
  const legend = el.querySelector("[data-map-legend]");
  const label = el.getAttribute("aria-label") || config.aria_label || "Map of Lebanon";
  el.setAttribute("aria-label", label);
  if (!el.getAttribute("role")) el.setAttribute("role", "region");
  loadStyle("maplibreCss");
  await loadScript("maplibre");
  if (!el.isConnected) return; // swapped out while MapLibre loaded
  const maplibregl = window.maplibregl;
  maplibregl.setWorkerUrl(asset("maplibreWorker"));

  // cooperative gestures: the page scrolls past the map; ctrl + wheel (two fingers on a phone) moves it
  const map = new maplibregl.Map({ container: canvas, style: styleSpec(), ...LEBANON, attributionControl: { compact: true }, cooperativeGestures: true });
  map.getCanvas().setAttribute("aria-label", label);
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.FullscreenControl(), "top-right");

  // A map swapped out by HTMX (a filter change swaps the results) frees its WebGL context at once:
  // browsers keep only a few contexts alive, and a map merely dropped from the page holds on to its own.
  let recolour = null;
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    if (recolour) document.removeEventListener("nd:themechange", recolour);
    map.remove();
  };
  el.addEventListener("htmx:beforeCleanupElement", (e) => e.target === el && release());

  map.on("load", () => {
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
    const layer = config.mode === "points" ? points(map, legend, config, popup) : choropleth(map, legend, config, popup);
    const b = bounds(layer.features);
    if (b) map.fitBounds(b, { padding: 32, maxZoom: 11, duration: 0 });
    if (config.focus && layer.focus) layer.focus(String(config.focus));
    recolour = () => {
      // a map removed some other way stops listening and frees its canvas at the next theme change
      if (!el.isConnected) return release();
      map.setPaintProperty("osm", "raster-brightness-max", basemapBrightness());
      layer.recolour();
    };
    document.addEventListener("nd:themechange", recolour);
  });
}
