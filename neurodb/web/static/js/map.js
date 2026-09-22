// Intervention map: MapLibre (CSP build) with OpenStreetMap tiles, a choropleth per admin level
// and circles for sites. Data comes from the internal API (/api/internal/databases/<id>/map/).
import { asset, cssVar, escapeHTML, fetchJSON, fmt, loadScript, loadStyle, readJSON } from "./lib.js";

const LEBANON = { center: [35.86, 33.87], zoom: 7.3 };
const RAMP = ["#deebf7", "#9ecae1", "#6baed6", "#3182bd", "#08519c"];

function styleSpec() {
  return {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        maxzoom: 19,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.6, "raster-opacity": 0.85 } }],
  };
}

function bounds(features) {
  let minX = 180, minY = 90, maxX = -180, maxY = -90;
  const visit = (c) => {
    if (typeof c[0] === "number") {
      minX = Math.min(minX, c[0]); maxX = Math.max(maxX, c[0]);
      minY = Math.min(minY, c[1]); maxY = Math.max(maxY, c[1]);
    } else c.forEach(visit);
  };
  features.forEach((f) => visit(f.geometry.coordinates));
  return minX <= maxX ? [[minX, minY], [maxX, maxY]] : null;
}

function fillTable(tbody, rows, level) {
  if (!tbody) return;
  tbody.innerHTML = rows.length
    ? rows
        .slice()
        .sort((a, b) => b.interventions - a.interventions)
        .map(
          (r) => `<tr><td>${escapeHTML(r.name || r.code || "—")}${level === "site" && r.district ? `<span class="cell-sub">${escapeHTML(r.district)}</span>` : ""}</td>
            <td class="num" data-value="${r.interventions}">${fmt(r.interventions)}</td>
            <td class="num" data-value="${r.value}">${fmt(r.value)}</td>
            ${level === "site" ? "" : `<td class="num">${fmt(r.partners)}</td>`}</tr>`,
        )
        .join("")
    : `<tr><td colspan="4" class="text-center text-muted py-4">No interventions match these filters.</td></tr>`;
}

export async function init(el) {
  const config = readJSON(el.dataset.config || "map-config");
  const canvas = el.querySelector("[data-map-canvas]");
  const legend = el.querySelector("[data-map-legend]");
  const tbody = document.querySelector("[data-map-table]");
  const totals = document.querySelector("[data-map-totals]");
  loadStyle("maplibreCss");
  const params = new URLSearchParams({ level: config.level, ...config.filters });
  const [data] = await Promise.all([fetchJSON(`${config.api}?${params}`), loadScript("maplibre")]);
  const maplibregl = window.maplibregl;
  maplibregl.setWorkerUrl(asset("maplibreWorker"));

  const map = new maplibregl.Map({ container: canvas, style: styleSpec(), ...LEBANON, attributionControl: { compact: true }, cooperativeGestures: false });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.FullscreenControl(), "top-right");

  const rows = config.level === "site" ? data.sites : data.areas;
  fillTable(tbody, rows, config.level);
  if (totals) totals.textContent = `${fmt(data.totals.interventions)} interventions · ${fmt(config.level === "site" ? data.totals.sites : data.totals.areas)} ${config.level === "site" ? "sites" : "areas"}`;
  const max = Math.max(1, ...rows.map((r) => Number(r.interventions) || 0));
  if (legend) {
    legend.querySelector("[data-legend-max]").textContent = fmt(max);
    legend.hidden = false;
  }

  map.on("load", () => {
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
    const html = (p) =>
      `<strong>${escapeHTML(p.name || p.code)}</strong><br>${fmt(p.interventions)} interventions<br>Value ${fmt(p.value)}${p.partners != null ? `<br>${fmt(p.partners)} partners` : ""}`;

    if (config.level === "site") {
      const features = data.sites
        .filter((s) => Number.isFinite(s.latitude) && Number.isFinite(s.longitude))
        .map((s) => ({ type: "Feature", geometry: { type: "Point", coordinates: [s.longitude, s.latitude] }, properties: s }));
      map.addSource("sites", { type: "geojson", data: { type: "FeatureCollection", features } });
      map.addLayer({
        id: "sites",
        type: "circle",
        source: "sites",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["get", "interventions"], 1, 4, max, 16],
          "circle-color": cssVar("--nd-primary") || "#1a73c7",
          "circle-opacity": 0.75,
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 1,
        },
      });
      const b = bounds(features);
      if (b) map.fitBounds(b, { padding: 40, maxZoom: 11, duration: 0 });
      ["sites"].forEach((layer) => {
        map.on("mousemove", layer, (e) => {
          map.getCanvas().style.cursor = "pointer";
          popup.setLngLat(e.lngLat).setHTML(html(e.features[0].properties)).addTo(map);
        });
        map.on("mouseleave", layer, () => {
          map.getCanvas().style.cursor = "";
          popup.remove();
        });
      });
      return;
    }

    const geo = data.geojson || { type: "FeatureCollection", features: [] };
    geo.features.forEach((f) => {
      f.properties.interventions = Number(f.properties.interventions) || 0;
    });
    map.addSource("areas", { type: "geojson", data: geo });
    map.addLayer({
      id: "areas-fill",
      type: "fill",
      source: "areas",
      paint: {
        "fill-color": [
          "case",
          ["==", ["get", "interventions"], 0],
          "rgba(150,150,150,0.08)",
          ["interpolate", ["linear"], ["get", "interventions"], 1, RAMP[0], max * 0.25, RAMP[1], max * 0.5, RAMP[2], max * 0.75, RAMP[3], max, RAMP[4]],
        ],
        "fill-opacity": 0.78,
      },
    });
    map.addLayer({ id: "areas-line", type: "line", source: "areas", paint: { "line-color": "#ffffff", "line-width": 0.8 } });
    const b = bounds(geo.features);
    if (b) map.fitBounds(b, { padding: 24, duration: 0 });
    map.on("mousemove", "areas-fill", (e) => {
      map.getCanvas().style.cursor = "pointer";
      popup.setLngLat(e.lngLat).setHTML(html(e.features[0].properties)).addTo(map);
    });
    map.on("mouseleave", "areas-fill", () => {
      map.getCanvas().style.cursor = "";
      popup.remove();
    });
  });
}
