// Partner reporting map: every implementation location of the filtered PD indicators, coloured by
// the worst tracking status there. Data comes from /api/internal/partner-monitoring/map/.
import { asset, cssVar, escapeHTML, fetchJSON, fmt, loadScript, loadStyle, readJSON } from "./lib.js";

const LEBANON = { center: [35.86, 33.87], zoom: 7.3 };
const STATUS_VARS = { off_track: "--nd-danger", on_track: "--nd-success", over_target: "--nd-warning", no_target: "--nd-neutral", not_reported: "--nd-muted" };
const FALLBACK = { off_track: "#c0392b", on_track: "#1e8449", over_target: "#d68910", no_target: "#7f8c8d", not_reported: "#95a5a6" };

function styleSpec() {
  return {
    version: 8,
    sources: {
      osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" },
    },
    layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.6, "raster-opacity": 0.85 } }],
  };
}

const color = (status) => cssVar(STATUS_VARS[status]) || FALLBACK[status] || FALLBACK.no_target;
const VARIANTS = { on_track: "success", off_track: "danger", over_target: "warning", no_target: "neutral", not_reported: "neutral" };
const pill = (status, label) =>
  `<span class="pill pill--${VARIANTS[status] || "neutral"} pill--sm" data-status="${escapeHTML(status)}"><span class="pill__dot" aria-hidden="true"></span>${escapeHTML(label || status)}</span>`;
const num = (v, digits = 0) => (v === null || v === undefined || v === "" ? "—" : fmt(Number(v).toFixed(digits)));
const money = (v, currency) => (v === null || v === undefined ? "—" : `${fmt(Math.round(v))}${currency ? ` ${escapeHTML(currency)}` : ""}`);

function placeName(p) {
  const where = [p.district, p.governorate].filter((x) => x && x !== p.name).join(", ");
  return `${escapeHTML(p.name || p.p_code || "—")}${where ? ` <span class="text-muted">· ${escapeHTML(where)}</span>` : ""}`;
}

function detail(place, data, config, el) {
  const box = document.querySelector("[data-pdmap-detail]");
  if (!box) return;
  box.hidden = false;
  box.querySelector("[data-pdmap-detail-title]").innerHTML = placeName(place);
  const m = place.monitoring || {};
  const meta = [
    place.p_code ? `P-code <span class="mono">${escapeHTML(place.p_code)}</span>` : "",
    place.level_name ? escapeHTML(place.level_name) : "",
    place.approximate ? `placed at ${escapeHTML(place.located_by)} (no coordinates of its own)` : "",
    `${fmt(place.partners)} partners · ${fmt(place.pds)} PDs · ${fmt(place.indicators)} indicators`,
    m.tpm_activities ? `${fmt(m.tpm_activities)} TPM activities` : "",
    m.findings ? `${fmt(m.findings)} monitoring findings${m.findings_off_track ? ` (${fmt(m.findings_off_track)} off track)` : ""}` : "",
    m.action_points_open ? `${fmt(m.action_points_open)} open action points` : "",
  ].filter(Boolean);
  box.querySelector("[data-pdmap-detail-sub]").innerHTML = meta.join(" · ");
  // partner -> PD -> indicators
  const groups = new Map();
  place.rows.forEach((r) => {
    const key = `${r.partner_id || r.partner}|${r.pd_id}`;
    if (!groups.has(key)) groups.set(key, { partner: r.partner, partner_id: r.partner_id, pd: data.pds[r.pd_id] || { id: r.pd_id, number: r.pd }, rows: [] });
    groups.get(key).rows.push(r);
  });
  const html = [...groups.values()]
    .map((g) => {
      const pd = g.pd;
      const partnerLink = g.partner_id ? `<a href="${config.partnerUrl.replace(/0\/$/, `${g.partner_id}/`)}">${escapeHTML(g.partner)}</a>` : escapeHTML(g.partner || "—");
      const funding = pd.total_budget !== undefined
        ? `<span class="cell-sub">Total budget ${money(pd.total_budget, pd.currency)} · UNICEF cash ${money(pd.unicef_cash, pd.currency)} · partner contribution ${money(pd.partner_contribution, pd.currency)}${pd.donors && pd.donors.length ? ` · donors: ${escapeHTML(pd.donors.join(", "))}` : ""}${pd.agreement ? ` · agreement ${escapeHTML(pd.agreement)}` : ""}</span>`
        : "";
      const rows = g.rows
        .map(
          (r) => `<tr>
            <td><a class="cell-title" href="${r.url}?year=${encodeURIComponent(data.year || "")}" hx-get="${r.url}" hx-target="#modal-content" hx-push-url="false">${escapeHTML(r.indicator)}</a>${r.output ? `<span class="cell-sub">${escapeHTML(r.output)}</span>` : ""}${r.planned && !r.reported ? `<span class="cell-sub">Planned here, nothing reported yet</span>` : ""}</td>
            <td class="num">${num(r.target)}</td>
            <td class="num">${num(r.achieved_here)}</td>
            <td class="num">${num(r.cumulative_here)}</td>
            <td class="num">${num(r.cumulative)}${r.achieved_percent !== null && r.achieved_percent !== undefined ? `<span class="cell-sub">${num(r.achieved_percent)}% of target</span>` : ""}</td>
            <td>${pill(r.tracking, r.tracking_label)}</td>
            <td class="small text-nowrap">${r.period ? escapeHTML(r.period) : "—"}${r.report ? `<span class="cell-sub mono">${escapeHTML(r.report)}</span>` : ""}</td>
          </tr>`,
        )
        .join("");
      return `<h3 class="h6 mt-3">${partnerLink} · <a href="${pd.url || "#"}">${escapeHTML(pd.number || "")}</a> <span class="text-muted fw-normal small">${escapeHTML(pd.title || "")}</span></h3>${funding}
        <div class="table-wrap"><table class="table table-sm"><thead><tr><th>Indicator</th><th class="num">Target (PD)</th><th class="num">Achieved here, this year</th><th class="num">Cumulative here</th><th class="num">Cumulative (all locations)</th><th>Status</th><th>Latest period</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    })
    .join("");
  const body = box.querySelector("[data-pdmap-detail-body]");
  body.innerHTML = html;
  if (window.htmx) window.htmx.process(body);
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

function fillTable(tbody, points, onPick, filter = "") {
  if (!tbody) return;
  const q = filter.trim().toLowerCase();
  const rows = points.filter((p) => !q || `${p.name} ${p.p_code} ${p.district} ${p.governorate}`.toLowerCase().includes(q));
  tbody.innerHTML = rows.length
    ? rows
        .map(
          (p, i) => `<tr data-pdmap-row="${escapeHTML(p.key)}" tabindex="0"><td>${placeName(p)}${p.approximate ? ' <span class="chip">approx.</span>' : ""}</td>
            <td class="num">${fmt(p.partners)}</td><td class="num">${fmt(p.pds)}</td><td class="num">${fmt(p.indicators)}</td>
            <td>${pill(p.worst, p.worst.replace("_", " "))}</td></tr>`,
        )
        .join("")
    : `<tr><td colspan="5" class="text-center text-muted py-4">No location matches.</td></tr>`;
  tbody.querySelectorAll("[data-pdmap-row]").forEach((tr) => {
    const pick = () => onPick(tr.dataset.pdmapRow);
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
  });
}

function kpis(data) {
  const box = document.querySelector("[data-pdmap-kpis]");
  if (!box) return;
  const t = data.totals;
  const values = [t.locations, t.indicators, t.programme_documents, t.partners, t.status_counts.on_track || 0, t.status_counts.off_track || 0, t.status_counts.not_reported || 0];
  box.querySelectorAll(".kpi__value").forEach((el, i) => { el.textContent = fmt(values[i] ?? 0); });
}

export async function init(el) {
  const config = readJSON(el.dataset.config || "map-config");
  const canvas = el.querySelector("[data-map-canvas]");
  const tbody = document.querySelector("[data-pdmap-table]");
  const totals = document.querySelector("[data-map-totals]");
  const search = document.querySelector("[data-pdmap-search]");
  loadStyle("maplibreCss");
  const [data] = await Promise.all([fetchJSON(config.api), loadScript("maplibre")]);
  const maplibregl = window.maplibregl;
  maplibregl.setWorkerUrl(asset("maplibreWorker"));
  const byKey = new Map(data.points.map((p) => [p.key, p]));
  kpis(data);
  if (totals) totals.textContent = `${fmt(data.totals.located)} on the map${data.totals.approximate ? ` (${fmt(data.totals.approximate)} approximate)` : ""}${data.unlocated.length ? ` · ${fmt(data.unlocated.length)} without coordinates` : ""}`;
  const unl = document.querySelector("[data-pdmap-unlocated]");
  if (unl && data.unlocated.length) {
    unl.hidden = false;
    unl.querySelector("[data-pdmap-unlocated-list]").innerHTML = data.unlocated
      .map((p) => `<span class="chip">${escapeHTML(p.name || p.p_code)}${p.p_code ? ` <span class="mono">${escapeHTML(p.p_code)}</span>` : ""} · ${fmt(p.indicators)}</span>`)
      .join("");
  }

  const map = new maplibregl.Map({ container: canvas, style: styleSpec(), ...LEBANON, attributionControl: { compact: true }, cooperativeGestures: false });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.FullscreenControl(), "top-right");
  const pick = (key, fly = true) => {
    const p = byKey.get(key);
    if (!p) return;
    detail(p, data, config, el);
    if (fly) map.flyTo({ center: [p.longitude, p.latitude], zoom: Math.max(map.getZoom(), 10) });
  };
  fillTable(tbody, data.points, pick);
  if (search) search.addEventListener("input", () => fillTable(tbody, data.points, pick, search.value));

  map.on("load", () => {
    const max = Math.max(2, ...data.points.map((p) => p.indicators)); // interpolate stops must ascend strictly
    const features = data.points.map((p) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.longitude, p.latitude] },
      properties: { key: p.key, name: p.name, indicators: p.indicators, partners: p.partners, pds: p.pds, worst: p.worst, approximate: p.approximate ? 1 : 0, color: color(p.worst) },
    }));
    map.addSource("places", { type: "geojson", data: { type: "FeatureCollection", features } });
    map.addLayer({
      id: "places",
      type: "circle",
      source: "places",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["get", "indicators"], 1, 6, max, 20],
        "circle-color": ["get", "color"],
        "circle-opacity": ["case", ["==", ["get", "approximate"], 1], 0.25, 0.8],
        "circle-stroke-color": ["get", "color"],
        "circle-stroke-width": 2,
      },
    });
    if (features.length) {
      let minX = 180, minY = 90, maxX = -180, maxY = -90;
      features.forEach((f) => { const [x, y] = f.geometry.coordinates; minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); });
      map.fitBounds([[minX, minY], [maxX, maxY]], { padding: 48, maxZoom: 11, duration: 0 });
    }
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    map.on("mousemove", "places", (e) => {
      map.getCanvas().style.cursor = "pointer";
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setHTML(`<strong>${escapeHTML(p.name)}</strong><br>${fmt(p.partners)} partners · ${fmt(p.pds)} PDs · ${fmt(p.indicators)} indicators<br>${pill(p.worst, p.worst.replace("_", " "))}${p.approximate ? "<br><em>approximate position</em>" : ""}`).addTo(map);
    });
    map.on("mouseleave", "places", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
    map.on("click", "places", (e) => pick(e.features[0].properties.key, false));
    const wanted = new URLSearchParams(location.search).get("focus");
    if (wanted && byKey.has(wanted)) pick(wanted);
  });
}
