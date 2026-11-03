// Urja Meter Console — a small client over the Urja Meter API (no build step).

const STATUS = {
  installed: { label: "Installed", color: "var(--good)", hex: "#0ca30c" },
  faulty: { label: "Faulty", color: "var(--serious)", hex: "#ec835a" },
  decommissioned: { label: "Decommissioned", color: "var(--neutral)", hex: "#8c8b86" },
};
const ANOMALY_LABELS = {
  decommissioned_but_consuming: "Decommissioned but consuming",
  faulty_but_reporting: "Faulty but reporting",
  zero_consumption: "Zero consumption",
  stale_data: "Stale data",
  voltage_out_of_band: "Voltage out of band",
  register_decreased: "Register went backwards",
  missing_values: "Missing values",
};
const LEVEL_LABELS = { zone: "Zone", circle: "Circle", division: "Division", subdivision: "Subdiv.", substation: "Substation", feeder: "Feeder", transformer: "DT" };

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = (n, d = 1) => (n == null ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const calm = matchMedia("(prefers-reduced-motion: reduce)");

// Counts each [data-count] element up from 0 to its value. Skipped under reduced motion.
function countUp(root) {
  root.querySelectorAll("[data-count]").forEach((el) => {
    const target = Number(el.dataset.count);
    if (calm.matches || !target) { el.textContent = fmt(target, 0); return; }
    const t0 = performance.now(), dur = 900;
    const tick = (t) => {
      const p = Math.min(1, (t - t0) / dur);
      el.textContent = fmt(target * (1 - (1 - p) ** 3), 0);
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}
// Index children so CSS can stagger their entrance (capped so long lists don't drag).
function stagger(root, cap = 30) {
  [...root.children].forEach((el, i) => el.style.setProperty("--i", Math.min(i, cap)));
  root.classList.remove("rows-in"); void root.offsetWidth; root.classList.add("rows-in");
}

function apiKey() {
  try { return localStorage.getItem("urja-api-key") || ""; } catch { return ""; }
}

async function api(path, params = {}, attempt = 0) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) url.searchParams.set(k, v);
  const headers = apiKey() ? { "X-API-Key": apiKey() } : {};
  const res = await fetch(url, { headers });
  if (res.status === 401) {
    const key = prompt("This API requires an X-API-Key:");
    if (key) { try { localStorage.setItem("urja-api-key", key); } catch {} return api(path, params); }
  }
  if (res.status === 503 && attempt < 15) {
    // Inventory still loading or portal briefly unavailable: honour Retry-After (capped).
    const wait = Math.min(Number(res.headers.get("Retry-After")) || 2, 5) * 1000;
    await new Promise((r) => setTimeout(r, wait));
    return api(path, params, attempt + 1);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.detail || body.title || res.statusText);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function statusPill(status) {
  const s = STATUS[status] || { label: status || "Unknown", color: "var(--muted)" };
  return `<span class="pill"><span class="dot" style="background:${s.color}"></span>${esc(s.label)}</span>`;
}

function splitBar(counts, total, cls = "bar-split") {
  return `<div class="${cls} grow" role="img" aria-label="${Object.entries(counts).map(([k, v]) => `${k} ${v}`).join(", ")}">${Object.keys(STATUS)
    .filter((k) => counts[k])
    .map((k) => `<span style="flex:${counts[k] / total};background:${STATUS[k].color}" title="${STATUS[k].label}: ${counts[k]}"></span>`)
    .join("")}</div>`;
}

// ------------------------------------------------------------------ routing
const views = ["overview", "map", "meters", "network"];
const loaded = new Set();
function show(view) {
  if (!views.includes(view)) view = "overview";
  for (const v of views) {
    const el = $(`#view-${v}`);
    const was = el.hidden;
    el.hidden = v !== view;
    if (v === view && (was || !loaded.has(v))) { el.classList.remove("enter"); void el.offsetWidth; el.classList.add("enter"); }
  }
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  if (!loaded.has(view)) { loaded.add(view); ({ overview: loadOverview, map: loadMap, meters: loadMeters, network: loadNetwork })[view](); }
  if (view === "map" && mapObj) setTimeout(() => mapObj.invalidateSize(), 50);
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

// ---------------------------------------------------------------- freshness
async function loadHealth() {
  try {
    const h = await api("/v1/health");
    const color = h.status === "ok" ? "var(--good)" : h.status === "degraded" ? "var(--warning)" : "var(--muted)";
    const age = h.inventory.age_s == null ? "not synced yet" : h.inventory.age_s < 90 ? "synced just now" : `synced ${Math.round(h.inventory.age_s / 60)} min ago`;
    $("#freshness").innerHTML = `<span class="dot" style="background:${color}"></span>${esc(h.status === "ok" ? "Live" : h.status)} · ${age}`;
    $("#freshness").title = `Portal requests: ${h.portal.requests}, rate-limited: ${h.portal.rate_limited}, last error: ${h.portal.last_error || "none"}`;
    return h;
  } catch (e) {
    $("#freshness").innerHTML = `<span class="dot" style="background:var(--critical)"></span>API unreachable`;
  }
}

// ----------------------------------------------------------------- overview
let fleetChart;
let insightsCache;
let anomalyFilter = null;

async function loadOverview() {
  let health = await loadHealth();
  while (health && health.status === "starting") {
    $("#kpis").innerHTML = `<div class="kpi loading"><div class="label">Loading inventory from the portal…</div></div>`;
    await new Promise((r) => setTimeout(r, 2000));
    health = await loadHealth();
  }
  const [facets, transformers, quality] = await Promise.all([api("/v1/meters/facets"), api("/v1/transformers"), api("/v1/data-quality")]);
  renderKpis(facets.data, transformers.data);
  renderQuality(quality);
  await refreshInsights();
}

async function refreshInsights() {
  const ins = await api("/v1/insights");
  insightsCache = ins;
  renderFleetChart(ins);
  renderAnomalies(ins);
  renderLoading(ins);
  const cov = ins.coverage;
  const warm = ins.warmup || {};
  $("#coverage-note").textContent = cov.pct < 100
    ? `Readings cached for ${cov.meters_with_readings} of ${cov.meters_total} meters (${cov.pct}%). ${warm.running ? `Background warm-up ${warm.done}/${warm.total} — refreshing automatically.` : ""}`
    : `All ${cov.meters_total} meters covered.`;
  if (cov.pct < 100 && warm.enabled) setTimeout(refreshInsights, 15000);
}

function renderKpis(f, transformers) {
  const st = f.status;
  const total = Object.values(st).reduce((a, b) => a + b, 0);
  const cap = transformers.reduce((a, t) => a + (t.capacity_kva || 0), 0);
  const phase = f.phase;
  $("#kpis").innerHTML = `
    <div class="kpi"><div class="label">Meters</div><div class="value" data-count="${total}">0</div>${splitBar(st, total)}
      <div class="sub">${Object.keys(STATUS).map((k) => `${STATUS[k].label} ${st[k] || 0}`).join(" · ")}</div></div>
    <div class="kpi"><div class="label"><span class="dot" style="background:var(--serious)"></span>Faulty</div><div class="value" data-count="${st.faulty || 0}">0</div><div class="sub">${fmt((100 * (st.faulty || 0)) / total)}% of fleet</div></div>
    <div class="kpi"><div class="label">Transformers</div><div class="value" data-count="${transformers.length}">0</div><div class="sub">${fmt(cap, 0)} kVA installed</div></div>
    <div class="kpi"><div class="label">Phase mix</div><div class="value"><span data-count="${phase.single || 0}">0</span><span class="muted"> / ${fmt(phase.three || 0, 0)}</span></div><div class="sub">single / three phase</div></div>
    <div class="kpi"><div class="label">Record formats</div><div class="value"><span data-count="${f.record_format.v2 || 0}">0</span><span class="muted"> v2</span></div><div class="sub">${fmt(f.record_format.legacy || 0, 0)} legacy — both normalised</div></div>`;
  [...$("#kpis").children].forEach((el, i) => el.style.setProperty("--i", i));
  countUp($("#kpis"));
}

function renderFleetChart(ins) {
  const bins = ins.consumption_histogram || [];
  $("#fleet-window").textContent = ins.window.from
    ? `avg ${fmt(ins.avg_daily_kwh_per_meter, 1)} kWh/day per meter · ${ins.window.from} → ${ins.window.to}`
    : "";
  const data = {
    labels: bins.map((b) => `${b.from}–${b.to}`),
    datasets: [{ label: "Meters", data: bins.map((b) => b.meters), backgroundColor: css("--series-1"), borderRadius: 4, borderSkipped: "start", maxBarThickness: 48, categoryPercentage: 0.92, barPercentage: 0.96 }],
  };
  const opts = baseOptions({ unit: "meters" });
  opts.scales.x.title = { display: true, text: "average kWh per day", color: css("--muted") };
  opts.scales.y.title = { display: true, text: "meters", color: css("--muted") };
  opts.plugins.tooltip.callbacks = { title: (c) => `${c[0].label} kWh/day`, label: (c) => ` ${c.parsed.y} meters` };
  if (fleetChart) { fleetChart.data = data; fleetChart.update(); return; }
  opts.animation = calm.matches ? false : { duration: 900, easing: "easeOutQuart", delay: (c) => (c.type === "data" && c.mode === "default" ? c.dataIndex * 35 : 0) };
  fleetChart = new Chart($("#fleet-chart"), { type: "bar", data, options: opts });
}

function baseOptions({ unit, legend = false }) {
  const grid = css("--border");
  const ink = css("--muted");
  return {
    maintainAspectRatio: false,
    animation: { duration: 250 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: legend, labels: { color: css("--text-2"), boxWidth: 10, boxHeight: 10 } },
      tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(c.parsed.y, 2)} ${unit}` } },
    },
    scales: {
      x: { grid: { display: false }, ticks: { color: ink, maxRotation: 0, autoSkipPadding: 14 }, border: { color: grid } },
      y: { grid: { color: grid }, ticks: { color: ink }, border: { display: false }, beginAtZero: true },
    },
  };
}

function renderAnomalies(ins) {
  const counts = ins.anomaly_counts;
  $("#anomaly-total").textContent = `${ins.anomalies.length} flags`;
  $("#anomaly-types").innerHTML = Object.entries(counts)
    .map(([t, n]) => `<button class="chip" data-type="${t}" aria-pressed="${anomalyFilter === t}">${esc(ANOMALY_LABELS[t] || t)} <b>${n}</b></button>`)
    .join("") || `<span class="muted">No anomalies in cached data.</span>`;
  $("#anomaly-types").querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => { anomalyFilter = anomalyFilter === c.dataset.type ? null : c.dataset.type; renderAnomalies(insightsCache); }));
  const list = ins.anomalies.filter((a) => !anomalyFilter || a.type === anomalyFilter).slice(0, 200);
  $("#anomaly-list").innerHTML = list
    .map((a) => `<li data-id="${a.meter_id}"><span class="sev-${a.severity}" title="${a.severity} severity">${a.severity === "high" ? "▲" : a.severity === "medium" ? "●" : "·"}</span>
      <span class="mono">${a.meter_id}</span><span class="msg">${esc(a.message)} <span class="muted">· ${a.transformer_code}</span></span></li>`)
    .join("");
  stagger($("#anomaly-list"));
  $("#anomaly-list").querySelectorAll("li").forEach((li) => li.addEventListener("click", () => openMeter(li.dataset.id)));
}

function renderLoading(ins) {
  const rows = ins.transformer_loading;
  if (!rows.length) { $("#loading-list").innerHTML = `<p class="muted">Waiting for readings…</p>`; return; }
  const max = Math.max(...rows.map((r) => r.utilisation_pct || 0), 1);
  const box = $("#loading-list");
  const first = !box.querySelector(".load-row");  // grow only on first draw, not on every 15 s refresh
  box.classList.toggle("grow", first);
  box.innerHTML = rows
    .map((r, i) => `<div class="load-row" title="${r.transformer_code}: ${fmt(r.avg_load_kva, 2)} kVA avg of ${fmt(r.capacity_kva, 0)} kVA">
      <span class="mono">${r.transformer_code}</span>
      <div class="load-track"><div class="load-fill" style="--i:${Math.min(i, 40)};width:${(100 * (r.utilisation_pct || 0)) / max}%"></div></div>
      <span class="num">${fmt(r.utilisation_pct, 1)}% <span class="muted">of ${fmt(r.capacity_kva, 0)}</span></span></div>`)
    .join("");
}

function renderQuality(q) {
  const labels = {
    blank_code_filled: "Blank node codes inferred from names",
    blank_name_filled: "Blank node names inferred from codes",
    dt_name_alias: "Transformer names corrected to master list",
    chain_differs_from_dt: "Meters re-attached via their DT",
    unresolved_blank_code: "Unresolvable blank codes",
  };
  const rows = Object.entries(q.issue_counts).map(([k, v]) => `<div class="q-row"><span>${esc(labels[k] || k)}</span><b>${v}</b></div>`);
  rows.push(`<div class="q-row"><span>Codes reused under different parents</span><b>${q.code_reuse.length}</b></div>`);
  $("#quality").innerHTML = rows.join("") + `<details class="q-more"><summary>Why codes are path-scoped</summary>
    <p>Division, subdivision, substation and feeder codes repeat under different parents (e.g. <span class="mono">${esc(q.code_reuse[0]?.code || "")}</span> sits under
    ${(q.code_reuse[0]?.parents || []).map((p) => `<span class="mono">${esc(p)}</span>`).join(", ")}), while every DT has one consistent chain.
    So nodes are identified by their full path, not their code.</p></details>`;
}

// ---------------------------------------------------------------------- map
let mapObj;
let mapLayers = {};
async function loadMap() {
  const all = await fetchAllMeters();
  mapObj = L.map("map", { zoomControl: true }).setView([26.91, 75.79], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(mapObj);
  const groups = {};
  for (const key of Object.keys(STATUS)) groups[key] = L.layerGroup().addTo(mapObj);
  for (const m of all) {
    if (!m.location) continue;
    const s = STATUS[m.status] || { hex: "#999" };
    const marker = L.circleMarker([m.location.lat, m.location.lng], { radius: 6, color: css("--surface") || "#fff", weight: 2, fillColor: s.hex, fillOpacity: 0.95 });
    marker.bindTooltip(`<b>${m.meter_id}</b> · ${s.label}<br>${esc(m.network.transformer.name)}`, { direction: "top" });
    marker.on("click", () => openMeter(m.meter_id));
    marker.addTo(groups[m.status] || mapObj);
  }
  mapLayers = groups;
  const bounds = all.filter((m) => m.location).map((m) => [m.location.lat, m.location.lng]);
  // Leaflet measures its container once; re-measure after layout settles or tiles stay grey.
  setTimeout(() => { mapObj.invalidateSize(); if (bounds.length) mapObj.fitBounds(bounds, { padding: [20, 20] }); }, 100);
  const counts = all.reduce((a, m) => ((a[m.status] = (a[m.status] || 0) + 1), a), {});
  $("#map-filters").innerHTML = Object.entries(STATUS)
    .map(([k, s]) => `<label class="legend-item"><input type="checkbox" checked data-status="${k}"/><span class="dot" style="background:${s.color}"></span>${s.label} (${counts[k] || 0})</label>`)
    .join("") + `<span class="muted">Click a meter for details. Tip: the API answers "what's near here?" via <span class="mono">?near=lat,lng&radius_km=</span></span>`;
  $("#map-filters").querySelectorAll("input").forEach((cb) =>
    cb.addEventListener("change", () => (cb.checked ? mapLayers[cb.dataset.status].addTo(mapObj) : mapObj.removeLayer(mapLayers[cb.dataset.status]))));
}

let allMetersPromise;
function fetchAllMeters() {
  allMetersPromise ??= (async () => {
    const out = [];
    let offset = 0;
    while (offset != null) {
      const page = await api("/v1/meters", { limit: 500, offset });
      out.push(...page.data);
      offset = page.page.next_offset;
    }
    return out;
  })();
  return allMetersPromise;
}

// ------------------------------------------------------------------- meters
const meterState = { q: "", status: "", make: "", phase: "", zone: "", offset: 0, limit: 25 };
async function loadMeters() {
  const f = (await api("/v1/meters/facets")).data;
  const fill = (id, obj, labeler = (k) => k) =>
    ($(id).innerHTML += Object.keys(obj).sort().map((k) => `<option value="${esc(k)}">${esc(labeler(k))} (${obj[k]})</option>`).join(""));
  fill("#f-status", f.status, (k) => STATUS[k]?.label || k);
  fill("#f-make", f.make);
  fill("#f-phase", f.phase, (k) => `${k} phase`);
  fill("#f-zone", f.zone);
  let t;
  $("#meter-q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { meterState.q = e.target.value.trim(); meterState.offset = 0; renderMeters(); }, 200); });
  for (const k of ["status", "make", "phase", "zone"]) $(`#f-${k}`).addEventListener("change", (e) => { meterState[k] = e.target.value; meterState.offset = 0; renderMeters(); });
  $("#prev").addEventListener("click", () => { meterState.offset = Math.max(0, meterState.offset - meterState.limit); renderMeters(); });
  $("#next").addEventListener("click", () => { meterState.offset += meterState.limit; renderMeters(); });
  renderMeters();
}

async function renderMeters() {
  const { q, status, make, phase, zone, offset, limit } = meterState;
  const res = await api("/v1/meters", { q, status, make, phase, zone, offset, limit });
  $("#meter-count").textContent = `${res.page.total} meters`;
  $("#meter-rows").innerHTML = res.data.length
    ? res.data.map((m) => `<tr data-id="${m.meter_id}">
        <td class="mono">${m.meter_id}</td><td class="mono">${esc(m.serial_no)}</td><td>${esc(m.make)}</td><td>${esc(m.phase)}</td>
        <td>${statusPill(m.status)}</td><td>${esc(m.network.transformer.name)} <span class="muted">${m.transformer_code}</span></td>
        <td class="mono">${m.network.feeder.code}</td>
        <td>${m.data_quality.length ? `<span class="flag" title="${esc(m.data_quality.map((i) => i.message).join("\n"))}">⚑ ${m.data_quality.length} repaired</span>` : ""}</td></tr>`).join("")
    : `<tr><td colspan="8" class="empty">No meters match these filters.</td></tr>`;
  stagger($("#meter-rows"));
  $("#meter-rows").querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openMeter(tr.dataset.id)));
  $("#page-info").textContent = `${res.page.total ? offset + 1 : 0}–${Math.min(offset + limit, res.page.total)} of ${res.page.total}`;
  $("#prev").disabled = offset === 0;
  $("#next").disabled = res.page.next_offset == null;
}

// ------------------------------------------------------------------ network
let treeData;
async function loadNetwork() {
  treeData = (await api("/v1/network/tree")).data;
  $("#tree").innerHTML = `<ul>${treeData.map((n) => treeNode(n, 0)).join("")}</ul>`;
  $("#tree").addEventListener("click", (e) => {
    const el = e.target.closest(".node");
    if (!el) return;
    const li = el.parentElement;
    const kids = li.querySelector(":scope > ul");
    if (kids) { kids.hidden = !kids.hidden; el.querySelector(".tw").textContent = kids.hidden ? "▸" : "▾"; }
    document.querySelectorAll(".node.active").forEach((n) => n.classList.remove("active"));
    el.classList.add("active");
    openNode(el.dataset.id);
  });
}

function treeNode(n, depth) {
  const hasKids = n.children.length > 0;
  const open = depth < 1;
  return `<li><div class="node" data-id="${esc(n.id)}"><span class="tw">${hasKids ? (open ? "▾" : "▸") : ""}</span>
    <span class="lvl">${LEVEL_LABELS[n.level]}</span><span class="nm">${esc(n.name)} <span class="muted mono">${esc(n.code)}</span></span>
    ${splitBar(n.status_counts, n.meter_count, "mini-split")}<span class="cnt">${n.meter_count}</span></div>
    ${hasKids ? `<ul ${open ? "" : "hidden"}>${n.children.map((c) => treeNode(c, depth + 1)).join("")}</ul>` : ""}</li>`;
}

async function openNode(id) {
  const res = await api(`/v1/network/nodes/${id}`, { limit: 500 });
  const n = res.data;
  const crumbs = id.split("/").map((code, i, arr) => `<span class="mono">${esc(code)}</span>`).join(`<span class="sep">›</span>`);
  $("#node-panel").innerHTML = `<header><h2>${esc(LEVEL_LABELS[n.level])}: ${esc(n.name)}</h2><span class="muted">${n.meter_count} meters</span></header>
    <div class="crumbs">${crumbs}</div>
    ${splitBar(n.status_counts, n.meter_count)}
    <div class="muted" style="margin:6px 0 12px">${Object.keys(STATUS).map((k) => `${STATUS[k].label} ${n.status_counts[k] || 0}`).join(" · ")}${n.capacity_kva ? ` · ${n.capacity_kva} kVA` : ""}</div>
    <div class="table-wrap"><table class="data"><thead><tr><th>Meter</th><th>Status</th><th>Make</th><th>DT</th></tr></thead><tbody>
    ${res.meters.map((m) => `<tr data-id="${m.meter_id}"><td class="mono">${m.meter_id}</td><td>${statusPill(m.status)}</td><td>${esc(m.make)}</td><td class="mono">${m.transformer_code}</td></tr>`).join("")}
    </tbody></table></div>`;
  $("#node-panel").querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openMeter(tr.dataset.id)));
}

// ------------------------------------------------------------- meter drawer
let meterChart;
const drawerState = { id: null, granularity: "daily", from: "", to: "" };

async function openMeter(id) {
  drawerState.id = id;
  drawerState.from = drawerState.to = "";
  $("#drawer").classList.add("open");
  $("#scrim").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-body").innerHTML = `<p class="skeleton">Loading ${esc(id)}…</p>`;
  const { data: m } = await api(`/v1/meters/${id}`);
  drawerState.status = m.status;
  const net = m.network;
  const crumbs = ["zone", "circle", "division", "subdivision", "substation", "feeder", "transformer"]
    .map((l) => `<a data-node="${esc(net[l].id)}" title="${LEVEL_LABELS[l]}">${esc(net[l].name || net[l].code)}</a>`)
    .join(`<span class="sep">›</span>`);
  $("#drawer-body").innerHTML = `
    <div class="muted">Meter</div>
    <h1 class="mono" style="font-size:22px">${m.meter_id} ${statusPill(m.status)}</h1>
    <div class="crumbs">${crumbs}</div>
    <dl class="facts">
      <div><dt>Serial</dt><dd class="mono">${esc(m.serial_no)}</dd></div>
      <div><dt>Make</dt><dd>${esc(m.make)}</dd></div>
      <div><dt>Phase</dt><dd>${esc(m.phase)}</dd></div>
      <div><dt>Installation</dt><dd>${esc((m.install_type || "").replace("_", " "))}</dd></div>
      <div><dt>Transformer</dt><dd>${esc(net.transformer.name)}${net.transformer.alias ? `<div class="muted">a.k.a. ${esc(net.transformer.alias)}</div>` : ""}</dd></div>
      <div><dt>Location</dt><dd class="mono">${m.location ? `<a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat=${m.location.lat}&mlon=${m.location.lng}#map=17/${m.location.lat}/${m.location.lng}">${m.location.lat.toFixed(5)}, ${m.location.lng.toFixed(5)}</a>` : "—"}</dd></div>
    </dl>
    <div class="toolbar">
      <div class="seg" role="group" aria-label="Granularity">
        <button data-g="daily" aria-pressed="${drawerState.granularity === "daily"}">Daily kWh</button>
        <button data-g="raw" aria-pressed="${drawerState.granularity === "raw"}">Interval</button>
      </div>
      <div><input type="date" id="d-from" aria-label="From"/> → <input type="date" id="d-to" aria-label="To"/> <button class="btn" id="d-apply">Apply</button></div>
    </div>
    <div id="readings"></div>
    ${m.data_quality.length ? `<h3 style="font-size:13px;margin:18px 0 6px">Portal data repaired for this meter</h3><ul class="issues">${m.data_quality.map((i) => `<li>${esc(i.message)}</li>`).join("")}</ul>` : ""}
    <p class="muted" style="margin-top:18px">API: <a class="mono" target="_blank" href="/v1/meters/${m.meter_id}/readings?granularity=${drawerState.granularity}">/v1/meters/${m.meter_id}/readings</a></p>`;
  $("#drawer-body").querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    drawerState.granularity = b.dataset.g;
    $("#drawer-body").querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    loadReadings();
  }));
  $("#d-apply").addEventListener("click", () => { drawerState.from = $("#d-from").value; drawerState.to = $("#d-to").value; loadReadings(); });
  $("#drawer-body").querySelectorAll(".crumbs a").forEach((a) => a.addEventListener("click", () => { closeDrawer(); show("network"); setTimeout(() => openNode(a.dataset.node), 300); }));
  loadReadings();
}

async function loadReadings() {
  const box = $("#readings");
  box.innerHTML = `<p class="skeleton">Fetching readings…</p>`;
  let r;
  try {
    r = await api(`/v1/meters/${drawerState.id}/readings`, { granularity: drawerState.granularity, from: drawerState.from, to: drawerState.to });
  } catch (e) {
    box.innerHTML = `<p class="empty">Couldn't load readings: ${esc(e.message)}</p>`;
    return;
  }
  $("#d-from").value = r.from;
  $("#d-to").value = r.to;
  const s = r.summary;
  const cadence = s.cadence_minutes === 30 ? "Half-hourly (AMI)" : s.cadence_minutes === 1440 ? "Daily reads" : s.cadence_minutes ? `${s.cadence_minutes} min` : "—";
  const warn = drawerState.status === "decommissioned" && (s.avg_daily_kwh || 0) > 0.5
    ? `<div class="callout"><b>Decommissioned, but still consuming ~${fmt(s.avg_daily_kwh, 1)} kWh/day.</b> Either the status is wrong or energy is being drawn through a meter that should be off the network.</div>`
    : "";
  box.innerHTML = warn + `<div class="stat-row">
      <div class="stat"><div class="v">${fmt(s.consumption_kwh, 1)}</div><div class="l">kWh in window</div></div>
      <div class="stat"><div class="v">${fmt(s.avg_daily_kwh, 1)}</div><div class="l">avg kWh / day</div></div>
      <div class="stat"><div class="v">${fmt(s.power_factor, 3)}</div><div class="l">power factor</div></div>
      <div class="stat"><div class="v">${s.voltage_min ?? "—"}–${s.voltage_max ?? "—"}</div><div class="l">voltage range (V)</div></div>
      <div class="stat"><div class="v" style="font-size:14px">${cadence}</div><div class="l">${s.readings} readings · ${r.meta.cache === "hit" ? "from cache" : `${r.meta.portal_calls} portal call(s)`}</div></div>
    </div>
    ${r.data.length ? `<div class="chart-box small"><canvas id="meter-chart"></canvas></div>` : `<p class="empty">No readings in ${r.from} → ${r.to}. The portal only holds June 2026 data.</p>`}`;
  if (!r.data.length) return;
  if (meterChart) meterChart.destroy();
  const daily = r.granularity === "daily";
  const labels = daily ? r.data.map((d) => d.date.slice(5)) : r.data.map((d) => d.timestamp.slice(5, 16).replace("T", " "));
  const values = r.data.map((d) => d.kwh);
  meterChart = new Chart($("#meter-chart"), {
    type: daily ? "bar" : "line",
    data: {
      labels,
      datasets: [{
        label: daily ? "kWh / day" : "kWh / interval",
        data: values,
        backgroundColor: daily ? css("--series-1") : css("--series-1-soft"),
        borderColor: css("--series-1"),
        borderWidth: daily ? 0 : 2,
        borderRadius: 4,
        borderSkipped: "start",
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: !daily,
        tension: 0.25,
        maxBarThickness: 32,
        spanGaps: false,
      }],
    },
    options: baseOptions({ unit: "kWh" }),
  });
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#scrim").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

// --------------------------------------------------------------------- boot
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
show(location.hash.slice(1) || "overview");
loadHealth();
setInterval(loadHealth, 30000);
