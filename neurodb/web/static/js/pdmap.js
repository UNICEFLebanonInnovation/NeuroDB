// Partner monitoring map: every implementation location of the filtered PD indicators, filled with the worst
// tracking status there and ringed and sized by its level (the API's "level": the whole country, a governorate, a
// district or a smaller place), so the levels are told apart at a glance; the legend, the side list and the place's
// details name the level in words. Data comes from /api/internal/partner-monitoring/map/.
import { asset, cssVar, escapeHTML, fetchJSON, fmt, isDark, loadScript, loadStyle, readJSON } from "./lib.js";

const LEBANON = { center: [35.86, 33.87], zoom: 7.3 };
const STATUS_VARS = { off_track: "--nd-danger", on_track: "--nd-success", over_target: "--nd-warning", no_target: "--nd-neutral", not_reported: "--nd-muted" };
const FALLBACK = { off_track: "#c0392b", on_track: "#1e8449", over_target: "#d68910", no_target: "#7f8c8d", not_reported: "#95a5a6" };
// each level: its ring colour (a design token of both themes), ring width, size band (the radius for one indicator
// and for the most indicators on the map: the bands do not overlap) and drawing order (smaller places on top)
export const LEVELS = {
  country: { token: "--nd-level-country", fallback: "#111827", radius: [20, 26], ring: 4, order: 0 },
  governorate: { token: "--nd-level-governorate", fallback: "#7b3fe4", radius: [14, 19], ring: 3.5, order: 1 },
  district: { token: "--nd-level-district", fallback: "#0b7285", radius: [9, 13], ring: 2.5, order: 2 },
  place: { token: "--nd-level-place", fallback: "#475569", radius: [5, 8], ring: 1.5, order: 3 },
};
const LEVEL_WORDS = { country: "Whole country", governorate: "Governorate", district: "District", place: "Smaller place" };
// the OpenStreetMap tiles are light: in the dark theme they are dimmed, as on the other maps, and the rings are light
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

const color = (status) => cssVar(STATUS_VARS[status]) || FALLBACK[status] || FALLBACK.no_target;
const ringColor = (level) => cssVar(LEVELS[level].token) || LEVELS[level].fallback;
/** A place's level: one of LEVELS, a smaller place when the API gives none it knows. */
export const levelOf = (p) => (Object.hasOwn(LEVELS, p?.level || "") ? p.level : "place");
const levelLabel = (level, config) => (config?.levels || {})[level] || LEVEL_WORDS[level];
/** The level in words, after a small ring in its colour (the legend's). */
function levelBadge(p, config, typeName = false) {
  const level = levelOf(p);
  const kind = typeName && level === "place" && p.level_name ? ` (${escapeHTML(p.level_name)})` : "";
  return `<span class="pdmap-level"><span class="pdmap-ring pdmap-ring--${level}" aria-hidden="true"></span>${escapeHTML(levelLabel(level, config))}${kind}</span>`;
}
const VARIANTS = { on_track: "success", off_track: "danger", over_target: "warning", no_target: "neutral", not_reported: "neutral" };
const pill = (status, label) =>
  `<span class="pill pill--${VARIANTS[status] || "neutral"} pill--sm" data-status="${escapeHTML(status)}"><span class="pill__dot" aria-hidden="true"></span>${escapeHTML(label || status)}</span>`;
const num = (v, digits = 0) => (v === null || v === undefined || v === "" ? "—" : fmt(Number(v).toFixed(digits)));

function placeName(p) {
  const where = [p.district, p.governorate].filter((x) => x && x !== p.name).join(", ");
  return `${escapeHTML(p.name || p.p_code || "—")}${where ? ` <span class="text-muted">· ${escapeHTML(where)}</span>` : ""}`;
}

/** A place's details: its name, a line on the place (its level, P-code, how it is placed, its counts and the
 * monitoring done there) and, per partner and programme document, the indicators planned or reported there. A
 * programme document's budget is not a figure of one place: none is shown. */
export function detailHTML(place, data, config) {
  const m = place.monitoring || {};
  const meta = [
    levelBadge(place, config, true),
    place.p_code ? `P-code <span class="mono">${escapeHTML(place.p_code)}</span>` : "",
    place.approximate ? `placed at ${escapeHTML(place.located_by)} (no coordinates of its own)` : "",
    `${fmt(place.partners)} partners · ${fmt(place.pds)} PDs · ${fmt(place.indicators)} indicators`,
    m.tpm_activities ? `${fmt(m.tpm_activities)} TPM activities` : "",
    m.findings ? `${fmt(m.findings)} monitoring findings${m.findings_off_track ? ` (${fmt(m.findings_off_track)} off track)` : ""}` : "",
    m.action_points_open ? `${fmt(m.action_points_open)} open action points` : "",
  ].filter(Boolean);
  // partner -> PD -> indicators
  const groups = new Map();
  (place.rows || []).forEach((r) => {
    const key = `${r.partner_id || r.partner}|${r.pd_id}`;
    if (!groups.has(key)) groups.set(key, { partner: r.partner, partner_id: r.partner_id, pd: data.pds[r.pd_id] || { id: r.pd_id, number: r.pd }, rows: [] });
    groups.get(key).rows.push(r);
  });
  // the detail opens for the map's year and report type, not the current year's QPR
  const q = `?report_type=${encodeURIComponent(data.report_type || "")}&year=${encodeURIComponent(data.year || "")}`;
  const yearLabel = data.year && data.year !== "all" ? ` in ${escapeHTML(String(data.year))}` : " in the year chosen";
  const body = [...groups.values()]
    .map((g) => {
      const pd = g.pd;
      const partnerLink = g.partner_id ? `<a href="${config.partnerUrl.replace(/0\/$/, `${g.partner_id}/`)}">${escapeHTML(g.partner)}</a>` : escapeHTML(g.partner || "—");
      const rows = g.rows
        .map(
          (r) => `<tr>
            <td><a class="cell-title" href="${r.url}${q}" hx-get="${r.url}${q}" hx-target="#modal-content" hx-push-url="false">${escapeHTML(r.indicator)}</a>${r.output ? `<span class="cell-sub">${escapeHTML(r.output)}</span>` : ""}${r.planned && !r.reported ? `<span class="cell-sub">Planned here, nothing reported yet</span>` : ""}</td>
            <td class="num">${num(r.target)}</td>
            <td class="num">${num(r.reached_here)}${r.prp_cumulative_here !== null && r.prp_cumulative_here !== undefined ? `<span class="cell-sub">PRP ${num(r.prp_cumulative_here)}</span>` : ""}</td>
            <td class="num">${num(r.reached_here_in_year)}</td>
            <td class="num">${num(r.reached)}${r.achieved_percent !== null && r.achieved_percent !== undefined ? `<span class="cell-sub">${num(r.achieved_percent)}% of target</span>` : ""}${r.additive === false && r.method ? `<span class="cell-sub">${escapeHTML(r.method)}</span>` : ""}</td>
            <td>${pill(r.tracking, r.tracking_label)}</td>
            <td class="small text-nowrap">${r.period ? escapeHTML(r.period) : "—"}${r.report ? `<span class="cell-sub mono">${escapeHTML(r.report)}</span>` : ""}</td>
          </tr>`,
        )
        .join("");
      return `<h3 class="h6 mt-3">${partnerLink} · <a href="${pd.url || "#"}">${escapeHTML(pd.number || "")}</a> <span class="text-muted fw-normal small">${escapeHTML(pd.title || "")}</span></h3>
        <div class="table-wrap"><table class="table table-sm"><thead><tr><th>Indicator</th><th class="num">Target (PD)</th><th class="num">Reached here</th><th class="num">Reached here${yearLabel}</th><th class="num">Reached (all locations)</th><th>Status</th><th>Latest period</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    })
    .join("");
  return { title: placeName(place), meta: meta.join(" · "), body };
}

function detail(place, data, config) {
  const box = document.querySelector("[data-pdmap-detail]");
  if (!box) return;
  const html = detailHTML(place, data, config);
  box.hidden = false;
  box.querySelector("[data-pdmap-detail-title]").innerHTML = html.title;
  box.querySelector("[data-pdmap-detail-sub]").innerHTML = html.meta;
  const body = box.querySelector("[data-pdmap-detail-body]");
  body.innerHTML = html.body;
  if (window.htmx) window.htmx.process(body);
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** The side list's rows: each place with its level, counts and worst status; ``filter`` keeps the places whose
 * name, P-code, district, governorate or level holds it ("district" lists the districts). */
export function tableHTML(points, filter = "", config = {}) {
  const q = filter.trim().toLowerCase();
  const rows = points.filter((p) => !q || `${p.name} ${p.p_code} ${p.district} ${p.governorate} ${levelLabel(levelOf(p), config)}`.toLowerCase().includes(q));
  return rows.length
    ? rows
        .map(
          (p) => `<tr data-pdmap-row="${escapeHTML(p.key)}" tabindex="0"><td>${placeName(p)}${p.approximate ? ' <span class="chip">approx.</span>' : ""}<span class="cell-sub">${levelBadge(p, config)}</span></td>
            <td class="num">${fmt(p.partners)}</td><td class="num">${fmt(p.pds)}</td><td class="num">${fmt(p.indicators)}</td>
            <td>${pill(p.worst, p.worst.replace("_", " "))}</td></tr>`,
        )
        .join("")
    : `<tr><td colspan="5" class="text-center text-muted py-4">No location matches.</td></tr>`;
}

function fillTable(tbody, points, onPick, filter = "", config = {}) {
  if (!tbody) return;
  tbody.innerHTML = tableHTML(points, filter, config);
  tbody.querySelectorAll("[data-pdmap-row]").forEach((tr) => {
    const pick = () => onPick(tr.dataset.pdmapRow);
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
  });
}

/** The places as map features: the status (``worst``) for the fill, the level for the ring, its width, the size
 * (within the level's band, larger for more indicators) and the drawing order. */
export function features(points) {
  const max = points.reduce((most, p) => Math.max(most, p.indicators || 0), 2);
  return points.map((p) => {
    const level = levelOf(p);
    const [small, large] = LEVELS[level].radius;
    const share = Math.min(1, Math.max(0, ((p.indicators || 1) - 1) / (max - 1)));
    return {
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.longitude, p.latitude] },
      properties: {
        key: p.key, name: p.name, indicators: p.indicators, partners: p.partners, pds: p.pds, worst: p.worst, level,
        approximate: p.approximate ? 1 : 0, radius: Math.round((small + (large - small) * share) * 10) / 10,
        ring: LEVELS[level].ring, order: LEVELS[level].order,
      },
    };
  });
}

/** The points' paint: the fill in the status tokens, the ring in the level tokens (read again at a theme change);
 * a place placed at a larger area is pale, its ring kept. */
export function paint() {
  return {
    "circle-radius": ["get", "radius"],
    "circle-color": ["match", ["get", "worst"], ...Object.keys(STATUS_VARS).flatMap((s) => [s, color(s)]), color("no_target")],
    "circle-opacity": ["case", ["==", ["get", "approximate"], 1], 0.35, 0.85],
    "circle-stroke-color": ["match", ["get", "level"], ...Object.keys(LEVELS).flatMap((l) => [l, ringColor(l)]), ringColor("place")],
    "circle-stroke-width": ["get", "ring"],
  };
}

/** Smaller places are drawn over the larger ones, so a governorate's circle never hides a village. */
export const layout = () => ({ "circle-sort-key": ["get", "order"] });

function kpis(data) {
  const box = document.querySelector("[data-pdmap-kpis]");
  if (!box) return;
  if (!data) {
    box.querySelectorAll(".kpi__value").forEach((el) => { el.textContent = "—"; });
    return;
  }
  const t = data.totals;
  const values = [t.locations, t.indicators, t.programme_documents, t.partners, t.status_counts.on_track || 0, t.status_counts.off_track || 0, t.status_counts.not_reported || 0];
  box.querySelectorAll(".kpi__value").forEach((el, i) => { el.textContent = fmt(values[i] ?? 0); });
}

export async function init(el) {
  const config = readJSON(el.dataset.config || "map-config", el);
  const canvas = el.querySelector("[data-map-canvas]");
  const tbody = document.querySelector("[data-pdmap-table]");
  const totals = document.querySelector("[data-map-totals]");
  const search = document.querySelector("[data-pdmap-search]");
  loadStyle("maplibreCss");
  const library = loadScript("maplibre");
  library.catch(() => {}); // awaited below, once the figures are shown
  // the figures, the location table and the details do not need the map: fill them first, so a
  // map library that fails to load leaves only the map area with an error
  let data;
  try {
    data = await fetchJSON(config.api);
  } catch (err) {
    if (!el.isConnected) return; // the page moved on while the figures loaded
    kpis(null);
    if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4">—</td></tr>`;
    throw err;
  }
  if (!el.isConnected) return; // swapped out while the figures loaded: nothing to fill
  const byKey = new Map(data.points.map((p) => [p.key, p]));
  kpis(data);
  // each number carries its figure trail key (data-figure), as the server's pages draw them; numbers only
  const figure = (key, n) => `<span data-figure="${key}">${fmt(n)}</span>`;
  if (totals) totals.innerHTML = `${figure("map.located", data.totals.located)} on the map${data.totals.approximate ? ` (${figure("map.approximate", data.totals.approximate)} approximate)` : ""}${data.unlocated.length ? ` · ${figure("map.unlocated", data.unlocated.length)} without coordinates` : ""}`;
  const unl = document.querySelector("[data-pdmap-unlocated]");
  if (unl && data.unlocated.length) {
    unl.hidden = false;
    unl.querySelector("[data-pdmap-unlocated-list]").innerHTML = data.unlocated
      .map((p) => `<span class="chip">${escapeHTML(p.name || p.p_code)}${p.p_code ? ` <span class="mono">${escapeHTML(p.p_code)}</span>` : ""} · ${fmt(p.indicators)}</span>`)
      .join("");
  }
  // the legend names the whole country only when a place on the map is at that level
  const country = el.querySelector('[data-pdmap-level="country"]');
  if (country) country.hidden = !data.points.some((p) => levelOf(p) === "country");
  let map = null;
  const pick = (key, fly = true) => {
    const p = byKey.get(key);
    if (!p) return;
    detail(p, data, config);
    if (fly && map) map.flyTo({ center: [p.longitude, p.latitude], zoom: Math.max(map.getZoom(), 10) });
  };
  fillTable(tbody, data.points, pick, "", config);
  if (search) search.addEventListener("input", () => fillTable(tbody, data.points, pick, search.value, config));

  await library;
  if (!el.isConnected) return; // swapped out while MapLibre loaded
  const maplibregl = window.maplibregl;
  maplibregl.setWorkerUrl(asset("maplibreWorker"));
  map = new maplibregl.Map({ container: canvas, style: styleSpec(), ...LEBANON, attributionControl: { compact: true }, cooperativeGestures: false });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.FullscreenControl(), "top-right");
  // A map swapped out by HTMX frees its WebGL context at once (as edumap.js does): browsers keep only a few
  // contexts alive, and a map merely dropped from the page holds on to its own.
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
    const placed = features(data.points);
    map.addSource("places", { type: "geojson", data: { type: "FeatureCollection", features: placed } });
    map.addLayer({ id: "places", type: "circle", source: "places", layout: layout(), paint: paint() });
    if (placed.length) {
      let minX = 180, minY = 90, maxX = -180, maxY = -90;
      placed.forEach((f) => { const [x, y] = f.geometry.coordinates; minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); });
      // the legend covers the bottom of the map: the first view keeps the points above it (at most half the map)
      const legend = el.querySelector("[data-map-legend]");
      const bottom = Math.min(Math.max(48, (legend?.offsetHeight || 0) + 24), Math.max(48, (canvas.clientHeight || 0) / 2));
      map.fitBounds([[minX, minY], [maxX, maxY]], { padding: { top: 48, right: 48, bottom, left: 48 }, maxZoom: 11, duration: 0 });
    }
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    map.on("mousemove", "places", (e) => {
      map.getCanvas().style.cursor = "pointer";
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setHTML(`<strong>${escapeHTML(p.name)}</strong><br>${levelBadge(p, config)}<br>${fmt(p.partners)} partners · ${fmt(p.pds)} PDs · ${fmt(p.indicators)} indicators<br>${pill(p.worst, p.worst.replace("_", " "))}${p.approximate ? "<br><em>approximate position</em>" : ""}`).addTo(map);
    });
    map.on("mouseleave", "places", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
    map.on("click", "places", (e) => pick(e.features[0].properties.key, false));
    const wanted = new URLSearchParams(location.search).get("focus");
    if (wanted && byKey.has(wanted)) pick(wanted);
    // a theme change repaints the fills and rings in the new theme's tokens and dims or lights the map
    recolour = () => {
      if (!el.isConnected) return release(); // a map removed some other way stops listening and frees its canvas
      map.setPaintProperty("osm", "raster-brightness-max", basemapBrightness());
      const colours = paint();
      map.setPaintProperty("places", "circle-color", colours["circle-color"]);
      map.setPaintProperty("places", "circle-stroke-color", colours["circle-stroke-color"]);
    };
    document.addEventListener("nd:themechange", recolour);
  });
}
