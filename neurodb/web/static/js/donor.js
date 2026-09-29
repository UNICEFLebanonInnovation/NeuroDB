/* The donor page (donors/page.html): charts drawn as inline SVG and HTML bars from the JSON the
   view embeds (#donor-data, built by neurodb/donors/services.py). Filters (grant, programme area,
   governorate) re-draw the "Your contribution" tab in the browser; nothing is fetched. Every text
   from the data goes through esc() before it reaches innerHTML. */

const DATA = JSON.parse(document.getElementById("donor-data").textContent);
const $ = (s, el = document) => el.querySelector(s);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const nf = new Intl.NumberFormat("en-US");
const num = (v) => nf.format(Math.round(v || 0));
const money = (v) => (v >= 1e6 ? `$${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M` : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${Math.round(v || 0)}`);
const pct = (v) => `${Math.round(v || 0)}%`;
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtDate = (iso, opts = { day: "numeric", month: "short", year: "numeric" }) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-GB", opts) : "—");
const sum = (list, key) => list.reduce((a, r) => a + ((typeof key === "function" ? key(r) : r[key]) || 0), 0);
const NS = "http://www.w3.org/2000/svg";
function svg(w, h, label) { const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", `0 0 ${w} ${h}`); s.setAttribute("role", "img"); s.setAttribute("aria-label", label); return s; }
function el(tag, attrs = {}, parent) { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (parent) parent.appendChild(e); return e; }
function txt(parent, x, y, text, attrs = {}) { const t = el("text", { x, y, "font-size": 12, fill: css("--ink-2"), ...attrs }, parent); t.textContent = text; return t; }

const SECTIONS = DATA.sections || [];
const slotOf = (section) => { const s = SECTIONS.find((x) => x.key === section); return `--s${s ? s.slot : 0}`; };
const colorOf = (section) => css(slotOf(section));
const GOVS = DATA.governorates || [];
const govName = (key) => (GOVS.find((g) => g.key === key) || { name: key }).name;

const ICONS = {
  check: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  x: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  up: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3M4 7l4-4 4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warn: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2l6.5 12h-13L8 2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 6.5v3.5M8 12v.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  dash: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 8h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
};
const pill = (s) => `<span class="pill ${s.cls}">${ICONS[s.icon] || ""}${esc(s.label)}</span>`;
const statusOf = (key) => (DATA.statuses || {})[key] || { cls: "none", label: "Not reported yet", icon: "warn" };
function paceStatus(achieved, expected) {
  if (achieved < expected - 10) return { cls: "crit", label: "Behind", icon: "x" };
  if (achieved > expected + 10) return { cls: "good", label: "Ahead of schedule", icon: "up" };
  return { cls: "good", label: "On track", icon: "check" };
}

// ------------------------------------------------------------------ tooltip
const tip = $("#tip");
function showTip(ev, html) { tip.innerHTML = html; tip.hidden = false; moveTip(ev); }
function moveTip(ev) {
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > innerHeight - 8) y = ev.clientY - h - pad;
  tip.style.left = `${Math.max(8, x)}px`; tip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { tip.hidden = true; }
function hover(node, html) {
  node.addEventListener("pointerenter", (e) => showTip(e, typeof html === "function" ? html() : html));
  node.addEventListener("pointermove", moveTip);
  node.addEventListener("pointerleave", hideTip);
}

// ------------------------------------------------------------------ data for the filters
const state = { grant: "all", sections: new Set(), gov: null, metric: "money", sort: { key: "committed", dir: -1 } };

function rows(ignoreGov = false) {
  const out = [];
  for (const pd of DATA.pds || []) {
    let committed = pd.committed, disbursed = pd.disbursed, grants = pd.grants;
    if (state.grant !== "all") {
      committed = pd.grants[state.grant] || 0;
      disbursed = (pd.paid || {})[state.grant] || 0;
      grants = { [state.grant]: committed };
    }
    if (!committed) continue;
    if (state.sections.size && !state.sections.has(pd.section)) continue;
    const g = committed / pd.committed; // this grant's part of the donor's money in the programme
    const f = state.gov && !ignoreGov ? pd.gov[state.gov] || 0 : 1;
    if (!f) continue;
    out.push({
      ...pd, f, g,
      grantsAmt: Object.fromEntries(Object.entries(grants).map(([k, v]) => [k, v * f])),
      committed: committed * f, disbursed: disbursed * f,
      children: pd.children * g * f, achieved: pd.achieved * g * f, target: pd.target * g * f,
    });
  }
  return out;
}

// ------------------------------------------------------------------ filters
function buildFilters() {
  const g = $("#fGrant");
  g.innerHTML = `<option value="all">All your grants</option>` + (DATA.grants || []).map((x) => `<option value="${esc(x.key)}">${esc(x.label)}</option>`).join("");
  g.addEventListener("change", () => { state.grant = g.value; render(); });
  const funded = SECTIONS.filter((s) => (DATA.pds || []).some((p) => p.section === s.key));
  const box = $("#fSections");
  box.innerHTML = funded.length > 1 ? funded.map((s) => `<button class="chip" type="button" aria-pressed="false" data-section="${esc(s.key)}"><i style="background:var(${slotOf(s.key)})"></i>${esc(s.key)}</button>`).join("") : "";
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-section]"); if (!b) return;
    const k = b.dataset.section; state.sections.has(k) ? state.sections.delete(k) : state.sections.add(k); render();
  });
  document.querySelectorAll("[data-metric]").forEach((b) => b.addEventListener("click", () => { state.metric = b.dataset.metric; render(); }));
}
function renderFilterState() {
  document.querySelectorAll("[data-section]").forEach((b) => b.setAttribute("aria-pressed", String(state.sections.has(b.dataset.section))));
  document.querySelectorAll("[data-metric]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.metric === state.metric)));
  const gov = $("#fGov");
  gov.innerHTML = state.gov ? `<span class="chip" aria-pressed="true">${esc(govName(state.gov))}</span>` : "";
  const any = state.grant !== "all" || state.sections.size || state.gov;
  if (any) gov.insertAdjacentHTML("beforeend", `<button class="reset" type="button" id="reset">Clear filters</button>`);
  const r = $("#reset");
  if (r) r.addEventListener("click", () => { state.grant = "all"; state.sections.clear(); state.gov = null; $("#fGrant").value = "all"; render(); });
  const parts = [state.grant === "all" ? "all grants" : state.grant, state.sections.size ? [...state.sections].join(", ") : "all areas", state.gov ? govName(state.gov) : "all of Lebanon"];
  $("#scope").textContent = `Showing ${parts.join(" · ")}`;
}

// ------------------------------------------------------------------ tiles
function tilesHtml(tiles) {
  return tiles.map((t) => `<div class="tile"><div class="k">${esc(t.k)}</div><div class="v">${esc(t.v)}</div><div class="h">${esc(t.h)}</div></div>`).join("");
}
function renderTiles(list) {
  const committed = sum(list, "committed"), disbursed = sum(list, "disbursed"), children = sum(list, "children");
  const partners = new Set(list.map((r) => r.partner)).size;
  const govs = new Set(list.flatMap((r) => Object.keys(r.gov).filter((g) => !state.gov || g === state.gov))).size;
  $("#tiles").innerHTML = tilesHtml([
    { k: "Committed from your grants", v: money(committed), h: `${list.length} programme${list.length === 1 ? "" : "s"}` },
    { k: "Disbursed to partners", v: money(disbursed), h: committed ? `${pct((100 * disbursed) / committed)} of committed` : "—" },
    { k: "Children reached", v: num(children), h: "attributed to your funding" },
    { k: "Cost per child", v: children >= 1 ? `$${Math.round(disbursed / children)}` : "—", h: "disbursed ÷ children reached" },
    { k: "Partners", v: String(partners), h: `in ${govs} governorate${govs === 1 ? "" : "s"}` },
  ]);
}

// ------------------------------------------------------------------ flow (grant -> area -> partner)
function renderFlow(list) {
  const box = $("#flow"); box.innerHTML = "";
  if (!list.length) { box.innerHTML = `<p class="note">Nothing funded in this selection.</p>`; return; }
  const W = 660, nodeW = 12, gap = 10, cols = [96, 310, 474];
  const short = (name) => (name.length > 24 ? name.slice(0, 23).trimEnd() + "…" : name);
  const gs = {}, sp = {}, gv = {}, sv = {}, pv = {}, partnerSection = {};
  list.forEach((r) => {
    Object.entries(r.grantsAmt).forEach(([g, v]) => { gs[`${g}|${r.section}`] = (gs[`${g}|${r.section}`] || 0) + v; gv[g] = (gv[g] || 0) + v; });
    sp[`${r.section}|${r.partner}`] = (sp[`${r.section}|${r.partner}`] || 0) + r.committed;
    sv[r.section] = (sv[r.section] || 0) + r.committed;
    pv[r.partner] = (pv[r.partner] || 0) + r.committed;
    if (!partnerSection[r.partner]) partnerSection[r.partner] = r.section;
  });
  const grants = Object.keys(gv).sort((a, b) => gv[b] - gv[a]);
  const sections = SECTIONS.map((s) => s.key).filter((k) => sv[k]);
  const partners = [];
  sections.forEach((s) => Object.keys(pv).filter((p) => sp[`${s}|${p}`]).sort((a, b) => pv[b] - pv[a]).forEach((p) => { if (!partners.includes(p)) partners.push(p); }));
  const colNodes = [grants, sections, partners];
  const values = [gv, sv, pv];
  const H = Math.max(240, Math.min(560, 26 * Math.max(...colNodes.map((n) => n.length)) + 60));
  const total = sum(list, "committed");
  const scale = Math.min(...colNodes.map((n) => (H - gap * (n.length - 1)) / total));
  const pos = colNodes.map((nodes, c) => {
    const used = nodes.reduce((a, k) => a + values[c][k] * scale, 0) + gap * (nodes.length - 1);
    let y = (H - used) / 2; const out = {};
    nodes.forEach((k) => { const h = values[c][k] * scale; out[k] = { y, h, inY: y, outY: y }; y += h + gap; });
    return out;
  });
  const s = svg(W, H + 6, "Flow of committed funds from grants to programme areas to partners");
  const links = el("g", {}, s);
  function band(x0, y0, x1, y1, w, color, html) {
    const xm = (x0 + x1) / 2;
    const p = el("path", { d: `M${x0},${y0} C${xm},${y0} ${xm},${y1} ${x1},${y1} L${x1},${y1 + w} C${xm},${y1 + w} ${xm},${y0 + w} ${x0},${y0 + w} Z`, fill: color, "fill-opacity": 0.32, class: "fade" }, links);
    hover(p, html);
    p.addEventListener("pointerenter", () => p.setAttribute("fill-opacity", 0.6));
    p.addEventListener("pointerleave", () => p.setAttribute("fill-opacity", 0.32));
  }
  grants.forEach((g) => sections.forEach((sec) => {
    const v = gs[`${g}|${sec}`]; if (!v) return;
    const w = v * scale, a = pos[0][g], b = pos[1][sec];
    band(cols[0] + nodeW, a.outY, cols[1], b.inY, w, colorOf(sec), `<b>${esc(g)} → ${esc(sec)}</b><br>${money(v)} committed`);
    a.outY += w; b.inY += w;
  }));
  sections.forEach((sec) => partners.forEach((p) => {
    const v = sp[`${sec}|${p}`]; if (!v) return;
    const w = v * scale, a = pos[1][sec], b = pos[2][p];
    band(cols[1] + nodeW, a.outY, cols[2], b.inY, w, colorOf(sec), `<b>${esc(sec)} → ${esc(p)}</b><br>${money(v)} committed`);
    a.outY += w; b.inY += w;
  }));
  const nodes = el("g", {}, s);
  const halo = { "paint-order": "stroke", stroke: css("--surface"), "stroke-width": 4, "stroke-linejoin": "round" };
  colNodes.forEach((keys, c) => keys.forEach((k) => {
    const p = pos[c][k], color = c === 0 ? css("--ink-2") : c === 1 ? colorOf(k) : colorOf(partnerSection[k]);
    const r = el("rect", { x: cols[c], y: p.y, width: nodeW, height: Math.max(2, p.h), rx: 3, fill: color }, nodes);
    hover(r, `<b>${esc(k)}</b><br>${money(values[c][k])} committed`);
    const x = c === 0 ? cols[0] - 8 : cols[c] + nodeW + 6, anchor = c === 0 ? "end" : "start";
    const t = txt(nodes, x, p.y + p.h / 2 + 4, c === 1 ? k : short(k), { "text-anchor": anchor, "font-weight": c === 2 ? 500 : 700, "font-size": 13, fill: css("--ink"), ...halo });
    if (c !== 1) hover(t, `<b>${esc(k)}</b><br>${money(values[c][k])} committed`);
    if (c !== 1 && p.h >= 30) txt(nodes, x, p.y + p.h / 2 + 19, money(values[c][k]), { "text-anchor": anchor, "font-size": 11.5, fill: css("--muted"), ...halo });
  }));
  box.appendChild(s);
  box.dataset.table = JSON.stringify({ head: ["From", "To", "Committed"], rows: [...Object.entries(gs), ...Object.entries(sp)].map(([k, v]) => [...k.split("|"), money(v)]) });
}

// ------------------------------------------------------------------ schematic tile map
function tileMap(box, byGov, { metricOf, fmt, unit, tipHtml, onPick, label }) {
  box.innerHTML = "";
  const vals = byGov.map(metricOf).filter((v) => v);
  const max = Math.max(...vals, 1);
  const ramp = ["--q1", "--q2", "--q3", "--q4", "--q5", "--q6"].map(css);
  const step = (v) => (v ? Math.min(5, Math.floor((v / max) * 6 - 1e-9)) : -1);
  const cw = 130, ch = 84, g_ = 6, W = 3 * (cw + g_) + 6;
  const s = svg(W, 4 * (ch + g_), label);
  byGov.forEach((g) => {
    const x = g.c * (cw + g_) + 6, y = g.r * (ch + g_), v = metricOf(g), st = step(v);
    const attrs = onPick ? { tabindex: 0, role: "button", "aria-label": `${g.name}: ${v ? fmt(v) : "none"}. Filter to ${g.name}`, style: "cursor:pointer" } : {};
    const grp = el("g", attrs, s);
    el("rect", { x, y, width: cw, height: ch, rx: 8, fill: st < 0 ? css("--surface-2") : ramp[st], stroke: state.gov === g.key && onPick ? css("--ink") : "none", "stroke-width": 2.5 }, grp);
    const ink = st >= 3 ? "#ffffff" : css("--ink"), faded = st < 0 ? css("--muted") : ink;
    txt(grp, x + 10, y + 22, g.name, { "font-size": 12.5, "font-weight": 700, fill: faded });
    txt(grp, x + 10, y + 46, v ? fmt(v) : "—", { "font-size": 17, "font-weight": 800, fill: faded });
    txt(grp, x + 10, y + 66, typeof unit === "function" ? unit(g) : unit, { "font-size": 11, fill: faded, opacity: 0.85 });
    hover(grp, () => tipHtml(g));
    if (onPick) {
      grp.addEventListener("click", () => onPick(g));
      grp.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(g); } });
    }
  });
  box.appendChild(s);
  return { max, ramp };
}
function renderMap() {
  const all = rows(true); // every governorate for the other filters
  const byGov = GOVS.map((g) => {
    const committed = sum(all, (r) => r.committed * (r.gov[g.key] || 0));
    const disbursed = sum(all, (r) => r.disbursed * (r.gov[g.key] || 0));
    const children = sum(all, (r) => r.children * (r.gov[g.key] || 0));
    return { ...g, committed, disbursed, children, cost: children >= 1 ? disbursed / children : null };
  });
  const metricOf = (g) => (state.metric === "money" ? g.committed : state.metric === "children" ? g.children : g.cost);
  const fmt = (v) => (state.metric === "money" ? money(v) : state.metric === "children" ? num(v) : `$${Math.round(v)}`);
  const { max, ramp } = tileMap($("#map"), byGov, {
    metricOf, fmt, label: "Schematic map of Lebanon's governorates",
    unit: state.metric === "money" ? "committed" : state.metric === "children" ? "children" : "per child",
    tipHtml: (g) => `<b>${esc(g.name)}</b><br>${money(g.committed)} committed · ${money(g.disbursed)} disbursed<br>${num(g.children)} children · ${g.cost ? "$" + Math.round(g.cost) + " per child" : "no children reported"}<br><i>Click to ${state.gov === g.key ? "clear" : "filter"}</i>`,
    onPick: (g) => { state.gov = state.gov === g.key ? null : g.key; render(); },
  });
  const unplaced = sum(all, (r) => (Object.keys(r.gov).length ? 0 : r.committed));
  const stops = [0, 1, 2, 3, 4, 5].map((i) => fmt((max * (i + 1)) / 6));
  $("#mapLegend").innerHTML = `<span>Lower</span>` + ramp.map((c, i) => `<span title="up to ${esc(stops[i])}"><i style="background:${c}"></i></span>`).join("") + `<span>Higher (${esc(stops[5])})</span>` +
    (unplaced && state.metric === "money" ? `<span>${money(unplaced)} not placed: no children reported by location yet</span>` : "");
}

// ------------------------------------------------------------------ spending
function renderSpend(list) {
  const box = $("#spend");
  const secs = SECTIONS.filter((s) => list.some((r) => r.section === s.key)).map((s) => {
    const mine = list.filter((r) => r.section === s.key);
    return { key: s.key, committed: sum(mine, "committed"), disbursed: sum(mine, "disbursed") };
  });
  if (!secs.length) { box.innerHTML = `<p class="note">Nothing funded in this selection.</p>`; return; }
  const max = Math.max(...secs.map((s) => s.committed), 1);
  box.innerHTML = secs.map((s) => `
    <div class="prog-row">
      <div class="head"><span><span class="sw" style="background:var(${slotOf(s.key)})"></span>${esc(s.key)}</span><span class="note" style="margin:0">${pct((100 * s.disbursed) / (s.committed || 1))} disbursed</span></div>
      <div class="track" title="${esc(s.key)}: ${money(s.disbursed)} of ${money(s.committed)}">
        <div class="fill" style="width:${(100 * s.committed) / max}%;background:var(${slotOf(s.key)});opacity:.3;position:absolute;inset:0 auto 0 0"></div>
        <div class="fill" style="width:${(100 * s.disbursed) / max}%;background:var(${slotOf(s.key)});position:absolute;inset:0 auto 0 0"></div>
      </div>
      <div class="foot">${money(s.disbursed)} disbursed of ${money(s.committed)} committed</div>
    </div>`).join("");
  box.dataset.table = JSON.stringify({ head: ["Programme area", "Committed", "Disbursed", "Share"], rows: secs.map((s) => [s.key, money(s.committed), money(s.disbursed), pct((100 * s.disbursed) / (s.committed || 1))]) });
}

// ------------------------------------------------------------------ grants
function renderGrants(list) {
  const out = (DATA.grants || []).map((g) => {
    const committed = sum(list, (r) => r.grantsAmt[g.key] || 0);
    const disbursed = sum(list, (r) => (r.grantsAmt[g.key] ? ((r.paid || {})[g.key] || 0) * r.f : 0));
    const left = committed - disbursed, share = committed ? (100 * disbursed) / committed : 0;
    let st = { cls: "good", label: "On schedule", icon: "check" };
    if (g.days_left === null) st = { cls: "none", label: "No end date on record", icon: "dash" };
    else if (g.days_left < 0) st = { cls: "warn", label: "Expired", icon: "warn" };
    else if (g.days_left < 120 && left / (committed || 1) > 0.25) st = { cls: "warn", label: "Expiring soon with funds left", icon: "warn" };
    return { g, committed, disbursed, left, share, st };
  }).filter((x) => x.committed > 0);
  if (!out.length) { $("#grants").innerHTML = `<p class="note">No grant in this selection.</p>`; return; }
  $("#grants").innerHTML = out.map((x) => `
    <div class="grant">
      <div class="head"><strong>${esc(x.g.label)}</strong>${pill(x.st)}</div>
      <div class="mini" style="height:10px;margin-top:8px" title="${pct(x.share)} disbursed"><b style="width:${Math.min(100, x.share)}%;background:var(--s1)"></b></div>
      <div class="foot">
        <span>${money(x.disbursed)} of ${money(x.committed)} disbursed (${pct(x.share)})</span>
        <span>${x.g.expires ? `Expires ${fmtDate(x.g.expires)}${x.g.days_left >= 0 ? ` · ${x.g.days_left} days left` : ""} · ` : ""}${money(Math.max(0, x.left))} to disburse</span>
      </div>
    </div>`).join("");
}

// ------------------------------------------------------------------ who benefits
function renderProgress(list) {
  const secs = SECTIONS.filter((s) => list.some((r) => r.section === s.key && r.target > 0));
  if (!secs.length) { $("#progress").innerHTML = `<p class="note">No children target reported for these programmes yet.</p>`; return; }
  $("#progress").innerHTML = secs.map((s) => {
    const mine = list.filter((r) => r.section === s.key && r.target > 0);
    const achieved = sum(mine, "achieved"), target = sum(mine, "target");
    const expected = sum(mine, (r) => r.elapsed * r.target) / target;
    const ach = (100 * achieved) / target;
    return `<div class="prog-row">
      <div class="head"><span><span class="sw" style="background:var(${slotOf(s.key)})"></span>${esc(s.key)}</span>${pill(paceStatus(ach, expected))}</div>
      <div class="track" title="${num(achieved)} of ${num(target)} children (${pct(ach)}); expected by now ${pct(expected)}">
        <div class="fill" style="width:${Math.min(100, ach)}%;background:var(${slotOf(s.key)})"></div>
        <div class="tick" style="left:calc(${Math.min(100, expected)}% - 1px)"></div>
      </div>
      <div class="foot">${num(achieved)} of ${num(target)} children · ${pct(ach)} (expected ${pct(expected)})</div>
    </div>`;
  }).join("");
}
function barsHtml(data, color = "var(--q4)") {
  const max = Math.max(...data.map((d) => d[1]), 1);
  return data.map(([k, v, note]) => `<div class="hbar"><span>${esc(k)}</span><div class="track"><div class="fill" style="width:${(100 * v) / max}%;background:${color}"></div></div><span class="val">${esc(note ?? num(v))}</span></div>`).join("");
}
function renderSexAge(list) {
  const children = sum(list, "children");
  const girls = sum(list, (r) => r.children * (r.sex.Girls || 0)), boys = sum(list, (r) => r.children * (r.sex.Boys || 0));
  const rest = Math.max(0, children - girls - boys);
  if (!children) $("#sex").innerHTML = `<p class="note">No children reported yet.</p>`;
  else if (girls + boys < 1) $("#sex").innerHTML = `<p class="note">The indicators of these programmes count girls and boys together.</p>`;
  else {
    const parts = [["Girls", girls, "--s5"], ["Boys", boys, "--s1"], ["Not split", rest, "--axis"]].filter((p) => p[1] >= 1);
    $("#sex").innerHTML = `<div class="stack">${parts.map(([l, v, c]) => `<div title="${l}: ${num(v)} (${pct((100 * v) / children)})" style="flex:${v};background:var(${c})"></div>`).join("")}</div>
      <div class="legend">${parts.map(([l, v, c]) => `<span><i style="background:var(${c})"></i>${l} ${pct((100 * v) / children)}</span>`).join("")}</div>`;
  }
  const ages = {};
  list.forEach((r) => Object.entries(r.age || {}).forEach(([k, v]) => { ages[k] = (ages[k] || 0) + r.children * v; }));
  const data = Object.entries(ages).filter((a) => a[1] >= 1).sort((a, b) => b[1] - a[1]);
  $("#ageBlock").hidden = data.length < 2;
  $("#age").innerHTML = barsHtml(data, "var(--s1)");
}
function renderGovBars(list) {
  const data = GOVS.filter((g) => !state.gov || g.key === state.gov)
    .map((g) => [g.name, sum(list, (r) => (r.children / r.f) * (r.gov[g.key] || 0))])
    .filter((d) => d[1] >= 1).sort((a, b) => b[1] - a[1]);
  const located = sum(data, (d) => d[1]), total = state.gov ? located : sum(list, "children");
  const box = $("#govbars");
  box.innerHTML = data.length ? barsHtml(data) : `<p class="note">No children reported by location yet.</p>`;
  if (total - located >= 1) box.insertAdjacentHTML("beforeend", `<p class="note" style="margin-top:10px">${num(total - located)} more children reached where the indicators do not split by location.</p>`);
  box.dataset.table = JSON.stringify({ head: ["Governorate", "Children reached"], rows: data.map(([k, v]) => [k, num(v)]) });
}

// ------------------------------------------------------------------ value for money
function renderVfm(list) {
  const box = $("#vfm"); box.innerHTML = "";
  const secs = SECTIONS.filter((s) => list.some((r) => r.section === s.key)).map((s) => {
    const mine = list.filter((r) => r.section === s.key), children = sum(mine, "children"), disb = sum(mine, "disbursed");
    return { key: s.key, avg: s.avg, cost: children >= 1 ? disb / children : null, children, disb };
  }).filter((s) => s.cost || s.avg);
  if (!secs.length) { box.innerHTML = `<p class="note">No children reached yet, so no cost per child.</p>`; return; }
  const W = 560, rowH = 40, L = 170, R = 40, H = secs.length * rowH + 30;
  const max = Math.max(...secs.map((s) => Math.max(s.cost || 0, s.avg || 0)), 1) * 1.15;
  const x = (v) => L + (W - L - R) * (v / max);
  const s = svg(W, H, "Cost per child by programme area against the country average");
  [0, 0.25, 0.5, 0.75, 1].forEach((f) => { const v = max * f; el("line", { x1: x(v), x2: x(v), y1: 0, y2: H - 22, stroke: css("--grid") }, s); txt(s, x(v), H - 6, `$${Math.round(v)}`, { "text-anchor": "middle", "font-size": 11, fill: css("--muted") }); });
  secs.forEach((sec, i) => {
    const cy = i * rowH + 20;
    txt(s, L - 12, cy + 4, sec.key.length > 24 ? sec.key.slice(0, 23) + "…" : sec.key, { "text-anchor": "end", "font-size": 12.5, "font-weight": 600, fill: css("--ink") });
    if (sec.cost && sec.avg) el("line", { x1: x(Math.min(sec.cost, sec.avg)), x2: x(Math.max(sec.cost, sec.avg)), y1: cy, y2: cy, stroke: css("--axis"), "stroke-width": 2 }, s);
    if (sec.avg) {
      const ring = el("circle", { cx: x(sec.avg), cy, r: 7, fill: css("--surface"), stroke: css("--ink-2"), "stroke-width": 2 }, s);
      hover(ring, `<b>${esc(sec.key)}</b><br>Country average: $${Math.round(sec.avg)} per child`);
    }
    if (sec.cost) {
      const dot = el("circle", { cx: x(sec.cost), cy, r: 7, fill: colorOf(sec.key), stroke: css("--surface"), "stroke-width": 2 }, s);
      hover(dot, `<b>${esc(sec.key)}</b><br>Your funding: $${Math.round(sec.cost)} per child<br>${money(sec.disb)} disbursed · ${num(sec.children)} children${sec.avg ? `<br>Country average: $${Math.round(sec.avg)}` : ""}`);
      const right = !sec.avg || sec.cost >= sec.avg;
      txt(s, x(sec.cost) + (right ? 12 : -12), cy - 10, `$${Math.round(sec.cost)}`, { "text-anchor": right ? "start" : "end", "font-size": 11.5, "font-weight": 700, fill: css("--ink") });
    }
  });
  box.appendChild(s);
  box.insertAdjacentHTML("beforeend", `<div class="legend"><span><i style="background:var(--s1);border-radius:50%"></i>Your funding</span><span><i style="background:var(--surface);border:2px solid var(--ink-2);border-radius:50%"></i>Country average (all donors)</span></div>`);
  box.dataset.table = JSON.stringify({ head: ["Programme area", "Your cost per child", "Country average"], rows: secs.map((r) => [r.key, r.cost ? `$${Math.round(r.cost)}` : "—", r.avg ? `$${Math.round(r.avg)}` : "—"]) });
}
function renderDumbbell(list) {
  const box = $("#dumbbell"); box.innerHTML = "";
  const secs = SECTIONS.filter((s) => list.some((r) => r.section === s.key && r.target > 0)).map((s) => {
    const mine = list.filter((r) => r.section === s.key && r.target > 0);
    return { key: s.key, spent: (100 * sum(mine, "disbursed")) / (sum(mine, "committed") || 1), done: Math.min(100, (100 * sum(mine, "achieved")) / sum(mine, "target")) };
  });
  if (!secs.length) { box.innerHTML = `<p class="note">No children target reported for these programmes yet.</p>`; return; }
  const W = 560, rowH = 40, L = 170, R = 30, H = secs.length * rowH + 30, x = (v) => L + (W - L - R) * (Math.min(100, v) / 100);
  const s = svg(W, H, "Share of funds disbursed against share of target achieved, by programme area");
  [0, 25, 50, 75, 100].forEach((v) => { el("line", { x1: x(v), x2: x(v), y1: 0, y2: H - 22, stroke: css("--grid") }, s); txt(s, x(v), H - 6, `${v}%`, { "text-anchor": "middle", "font-size": 11, fill: css("--muted") }); });
  secs.forEach((sec, i) => {
    const cy = i * rowH + 20, col = colorOf(sec.key), ahead = sec.spent - sec.done;
    txt(s, L - 12, cy + 4, sec.key.length > 24 ? sec.key.slice(0, 23) + "…" : sec.key, { "text-anchor": "end", "font-size": 12.5, "font-weight": 600, fill: css("--ink") });
    el("line", { x1: x(Math.min(sec.spent, sec.done)), x2: x(Math.max(sec.spent, sec.done)), y1: cy, y2: cy, stroke: ahead > 25 ? css("--crit") : css("--axis"), "stroke-width": 3 }, s);
    const sq = el("rect", { x: x(sec.spent) - 6, y: cy - 6, width: 12, height: 12, rx: 2, fill: css("--surface"), stroke: col, "stroke-width": 2.5 }, s);
    const dt = el("circle", { cx: x(sec.done), cy, r: 6.5, fill: col, stroke: css("--surface"), "stroke-width": 2 }, s);
    const html = `<b>${esc(sec.key)}</b><br>Disbursed: ${pct(sec.spent)} of committed<br>Achieved: ${pct(sec.done)} of target${ahead > 25 ? "<br>Spending is more than 25 points ahead of results" : ""}`;
    hover(sq, html); hover(dt, html);
  });
  box.appendChild(s);
  box.insertAdjacentHTML("beforeend", `<div class="legend"><span><i style="background:var(--surface);border:2px solid var(--ink-2)"></i>Disbursed</span><span><i style="background:var(--ink-2);border-radius:50%"></i>Achieved</span><span><i style="background:var(--crit);height:3px;width:14px"></i>Spending more than 25 points ahead</span></div>`);
}

// ------------------------------------------------------------------ programmes table
const COLS = [
  { key: "title", label: "Programme", get: (r) => r.title },
  { key: "section", label: "Area", get: (r) => r.section },
  { key: "committed", label: "Your funds", n: true, get: (r) => r.committed },
  { key: "disbursedPct", label: "Disbursed", n: true, get: (r) => r.disbursed / (r.committed || 1) },
  { key: "children", label: "Children", n: true, get: (r) => r.children },
  { key: "cost", label: "Cost/child", n: true, get: (r) => (r.children >= 1 ? r.disbursed / r.children : Infinity) },
  { key: "result", label: "Main result", get: (r) => (r.indicator_target ? (r.indicator_cumulative || 0) / r.indicator_target : -1) },
  { key: "status", label: "Status", get: (r) => statusOf(r.status).label },
];
function renderTable(list) {
  const { key, dir } = state.sort, col = COLS.find((c) => c.key === key);
  const sorted = [...list].sort((a, b) => { const x = col.get(a), y = col.get(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
  const t = $("#prog");
  const month = { month: "short", year: "numeric" };
  t.innerHTML = `<thead><tr>${COLS.map((c) => `<th scope="col" class="${c.n ? "n" : ""}" data-sort="${c.key}" tabindex="0" aria-sort="${c.key === key ? (dir > 0 ? "ascending" : "descending") : "none"}">${c.label}</th>`).join("")}</tr></thead>
  <tbody>${sorted.map((r) => {
    const disb = (100 * r.disbursed) / (r.committed || 1);
    const result = r.indicator
      ? `${esc(r.indicator)}<small>${r.indicator_target ? `${num(r.indicator_cumulative)} of ${num(r.indicator_target)} (${pct((100 * (r.indicator_cumulative || 0)) / r.indicator_target)}), ` : ""}${pct(r.elapsed)} of the time elapsed</small>`
      : `<small>No children result reported yet</small>`;
    return `<tr>
      <td class="pd">${esc(r.title)}<small>${esc(r.partner)} · ${esc(r.id)} · ${fmtDate(r.start, month)} to ${fmtDate(r.end, month)}</small></td>
      <td><span class="sw" style="background:var(${slotOf(r.section)})"></span>${esc(r.section)}<small>${esc(Object.keys(r.gov).map(govName).join(", "))}</small></td>
      <td class="n">${money(r.committed)}<small>${pct(r.share * 100)} of the programme</small></td>
      <td class="n">${pct(disb)}<div class="mini"><b style="width:${Math.min(100, disb)}%"></b></div></td>
      <td class="n">${num(r.children)}</td>
      <td class="n">${r.children >= 1 ? "$" + Math.round(r.disbursed / r.children) : "—"}</td>
      <td style="min-width:200px">${result}</td>
      <td>${pill(statusOf(r.status))}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="note">No programme in this selection.</td></tr>`}</tbody>`;
  t.querySelectorAll("[data-sort]").forEach((th) => {
    const go = () => { const k = th.dataset.sort; state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : -1 }; renderTable(rows()); };
    th.addEventListener("click", go);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
}

// ------------------------------------------------------------------ UNICEF Lebanon overall
function renderOverall() {
  const o = DATA.overall || {};
  $("#oTiles").innerHTML = tilesHtml([
    { k: "Children reached", v: num(o.children), h: `in ${DATA.year}, every programme` },
    { k: "Programmes", v: num(o.programmes), h: "with partners, running this year" },
    { k: "Partners", v: num(o.partners), h: "delivering for children" },
    { k: "Governorates reached", v: `${o.governorates_reached || 0} of ${GOVS.length}`, h: "where children were reached" },
    { k: "Results on track", v: o.on_track_percent === null || o.on_track_percent === undefined ? "—" : pct(o.on_track_percent), h: `of ${num(o.results_measured)} results with a target` },
    { k: "Field visits", v: num(o.visits), h: "monitoring and third-party visits" },
  ]);
  // children by month
  const box = $("#oMonths"); box.innerHTML = "";
  const months = o.months || [], labels = o.month_labels || [];
  if (!months.some((v) => v)) box.innerHTML = `<p class="note">No monthly figures yet.</p>`;
  else {
    const W = 560, H = 240, L = 50, R = 10, T = 12, B = 26, max = Math.max(...months, 1) * 1.1;
    const bw = (W - L - R) / 12, y = (v) => T + (H - T - B) * (1 - v / max);
    const s = svg(W, H, "Children reached each month");
    [0, 0.5, 1].forEach((f) => { el("line", { x1: L, x2: W - R, y1: y(max * f), y2: y(max * f), stroke: css("--grid") }, s); txt(s, L - 6, y(max * f) + 4, num(max * f), { "text-anchor": "end", "font-size": 11, fill: css("--muted") }); });
    months.forEach((v, i) => {
      const r = el("rect", { x: L + i * bw + 4, y: y(v), width: bw - 8, height: Math.max(0, y(0) - y(v)), rx: 3, fill: css("--s1") }, s);
      hover(r, `<b>${esc(labels[i])} ${DATA.year}</b><br>${num(v)} children`);
      txt(s, L + i * bw + bw / 2, H - 8, labels[i] || "", { "text-anchor": "middle", "font-size": 11, fill: css("--ink-2") });
    });
    box.appendChild(s);
    box.dataset.table = JSON.stringify({ head: ["Month", "Children reached"], rows: months.map((v, i) => [labels[i], num(v)]) });
  }
  // sections
  const secs = o.sections || [];
  $("#oSections").innerHTML = secs.length ? secs.map((s) => {
    const st = paceStatus(s.percent || 0, s.elapsed || 0);
    return `<div class="prog-row">
      <div class="head"><span><span class="sw" style="background:var(${slotOf(s.section)})"></span>${esc(s.section)}</span>${pill(st)}</div>
      <div class="track" title="${pct(s.percent)} of the target; ${pct(s.elapsed)} of the year elapsed"><div class="fill" style="width:${Math.min(100, s.percent || 0)}%;background:var(${slotOf(s.section)})"></div><div class="tick" style="left:calc(${Math.min(100, s.elapsed || 0)}% - 1px)"></div></div>
      <div class="foot">${pct(s.percent)} of the target reached · ${pct(s.elapsed)} of the time elapsed</div>
    </div>`;
  }).join("") : `<p class="note">No targets reported yet.</p>`;
  // governorates
  const byKey = Object.fromEntries((o.governorates || []).map((g) => [g.key, g]));
  const govs = GOVS.map((g) => ({ ...g, ...(byKey[g.key] || { reached: 0 }) }));
  tileMap($("#oMap"), govs, {
    metricOf: (g) => g.reached, fmt: num, label: "Children reached by governorate, schematic map",
    unit: (g) => (g.coverage ? `${pct(g.coverage)} of children` : "children"),
    tipHtml: (g) => `<b>${esc(g.name)}</b><br>${num(g.reached)} children reached${g.coverage ? `<br>${pct(g.coverage)} of the governorate's children` : ""}`,
  });
  $("#oMap").dataset.table = JSON.stringify({ head: ["Governorate", "Children reached", "Share of children"], rows: govs.map((g) => [g.name, num(g.reached), g.coverage ? pct(g.coverage) : "—"]) });
  // status
  const counts = o.status_counts || {};
  const order = [["over_target", "--good"], ["on_track", "--s3"], ["off_track", "--crit"], ["not_reported", "--axis"]];
  const total = order.reduce((a, [k]) => a + (counts[k] || 0), 0);
  $("#oStatus").innerHTML = total ? `<div class="stack">${order.filter(([k]) => counts[k]).map(([k, c]) => `<div title="${esc(statusOf(k).label)}: ${counts[k]}" style="flex:${counts[k]};background:var(${c})"></div>`).join("")}</div>
    <div class="legend">${order.filter(([k]) => counts[k]).map(([k, c]) => `<span><i style="background:var(${c})"></i>${esc(statusOf(k).label)} ${counts[k]} (${pct((100 * counts[k]) / total)})</span>`).join("")}</div>` : `<p class="note">No results reported yet.</p>`;
  // who is reached
  const facts = [];
  const sex = o.sex || {}, sexTotal = Object.values(sex).reduce((a, b) => a + b, 0);
  Object.entries(sex).forEach(([k, v]) => { if (sexTotal) facts.push([pct((100 * v) / sexTotal), `${k.toLowerCase()} (where named)`]); });
  if (o.disability) facts.push([num(o.disability), "children with disabilities"]);
  if (o.sites_visited) facts.push([num(o.sites_visited), "sites visited by third-party monitors"]);
  const nat = Object.entries(o.nationality || {});
  $("#oWho").innerHTML = (facts.length ? `<div class="facts">${facts.map(([v, l]) => `<div class="fact"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("")}</div>` : "") +
    (nat.length > 1 ? `<div class="cardhead mt"><div><h3>Children reached by nationality</h3><p class="note">Where the indicators name it.</p></div></div>${barsHtml(nat, "var(--s1)")}` : "");
}

// ------------------------------------------------------------------ table views for charts
document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-table]"); if (!b) return;
  const box = document.getElementById(b.dataset.table), card = b.closest(".card");
  const open = card.querySelector(".alttable");
  if (open) { open.parentElement.remove(); b.textContent = "Table"; return; }
  const data = JSON.parse(box.dataset.table || '{"head":[],"rows":[]}');
  const wrap = document.createElement("div"); wrap.className = "scroll";
  wrap.innerHTML = `<table class="alttable"><thead><tr>${data.head.map((h, i) => `<th class="${i ? "n" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${data.rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i && /^[$\d—]/.test(c) ? "n" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  card.appendChild(wrap); b.textContent = "Hide table";
});

// ------------------------------------------------------------------ tabs, year, theme
document.querySelectorAll("[data-tab]").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  const tab = a.dataset.tab;
  document.querySelectorAll("[data-tab]").forEach((x) => x.setAttribute("aria-selected", String(x === a)));
  $("#panel-mine").hidden = tab !== "mine";
  $("#panel-overall").hidden = tab !== "overall";
  hideTip();
  try { history.replaceState(null, "", a.getAttribute("href")); } catch (err) { /* file:// or sandboxed */ }
}));
document.querySelectorAll("[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));
const themeButton = $("#theme");
function syncThemeButton() { themeButton.setAttribute("aria-pressed", String(document.documentElement.getAttribute("data-bs-theme") === "dark")); }
themeButton.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-bs-theme", next);
  try { localStorage.setItem("neurodb-theme", next); } catch (err) { /* storage unavailable */ }
});

// ------------------------------------------------------------------ render
function render() {
  hideTip();
  syncThemeButton();
  if ((DATA.pds || []).length) {
    const list = rows();
    renderFilterState();
    renderTiles(list);
    renderFlow(list);
    renderMap();
    renderSpend(list);
    renderGrants(list);
    renderProgress(list);
    renderSexAge(list);
    renderGovBars(list);
    renderVfm(list);
    renderDumbbell(list);
    renderTable(list);
  }
  renderOverall();
  document.querySelectorAll(".alttable").forEach((t) => t.parentElement.remove());
  document.querySelectorAll("button[data-table]").forEach((b) => { b.textContent = "Table"; });
}
if ((DATA.pds || []).length) buildFilters();
render();
new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ["data-bs-theme"] });
