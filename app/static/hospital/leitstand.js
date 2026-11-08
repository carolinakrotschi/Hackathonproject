import { $, esc, api, hhmm, toast, poll, asList, toMs } from "/static/hospital/common.js";
import { createPlan, floorLabel, roomOptions, DOT_COLORS, KIND_LABELS, HEAT_GRADIENT } from "/static/hospital/plan.js";

const ASSET_LABEL = { infusion_pump: "Infusion pump", wheelchair: "Wheelchair", bed: "Bed", stretcher: "Stretcher", chair: "Chair" };
const ASSET_PLURAL = {
  infusion_pump: "Infusion pumps", wheelchair: "Wheelchairs", bed: "Beds", stretcher: "Stretchers", chair: "Chairs",
  coffee_machine: "Coffee machines", vending_machine: "Vending machines", cabinet: "Cabinets",
};
const DEFAULT_ADD = ["stretcher", "bed", "wheelchair", "chair"];
const LIFT_WINDOW = [-60, 540];
const SKIP_ROOMS = ["corridor", "lift", "stair", "outdoor"];

const S = {
  map: null, plan: null, tags: [], inUse: {}, inUseSig: "", tab: "devices", show: { patient: true, pager: true, asset: true, staff: true },
  hl: { asset: [], room: [], dot: [] }, proposal: null, before: null, after: null, sim: null, stopLifts: null,
  offset: 0, marks: [], heat: null, heatSeq: 0, analysis: null, placement: null, upload: { files: [], summary: null },
};

const serverNow = () => Date.now() + S.offset;
const num = (v, d = 0) => (v == null ? "–" : Number(v).toFixed(d));

const spaceOf = (id) => S.map.spaces.find((s) => s.id === id);
const floorOf = (id) => S.map.floors.find((f) => f.id === id);
const roomName = (id) => spaceOf(id)?.name || "";
const where = (spaceId, floorId) => [roomName(spaceId), floorLabel(floorOf(floorId || spaceOf(spaceId)?.floor_id), true)].filter(Boolean).join(" · ");
const pos = (v) => (Array.isArray(v) ? v : v ? [v.x, v.y] : null);

function applyHl() { S.plan.highlight([...S.hl.asset, ...S.hl.room, ...S.hl.dot]); }

// ---------- live tracking ----------
function renderLegend() {
  $("#legend").innerHTML = Object.entries(KIND_LABELS).map(([k, label]) =>
    `<label><input type="checkbox" data-kind="${k}" ${S.show[k] ? "checked" : ""}><span class="sw${k === "asset" ? " sq" : ""}" style="background:${DOT_COLORS[k]}"></span>${label} <b data-count="${k}">0</b></label>`).join("");
}

function renderDots() {
  const tagDots = S.tags.filter((t) => S.show[t.kind] !== false && t.floor_id && t.x != null && t.y != null).map((t) => ({
    id: t.tag_id, floor_id: t.floor_id, x: t.x, y: t.y, kind: t.kind, in_use: t.in_use,
    label: [t.label, roomName(t.space_id), t.seen_at ? `last seen ${hhmm(t.seen_at)}` : ""].filter(Boolean).join(" · "),
    showLabel: S.hl.dot.includes(t.tag_id),
  }));
  S.plan.setDots([...tagDots, ...S.marks]);
  for (const k of Object.keys(KIND_LABELS)) {
    const b = document.querySelector(`[data-count="${k}"]`);
    if (b) b.textContent = S.tags.filter((t) => t.kind === k).length;
  }
}

async function pollTracking() {
  let busy;
  try {
    [S.tags, busy] = await Promise.all([api("/api/hospital/tracking").then((r) => asList(r, "tags")), api("/api/hospital/assets/in_use")]);
    $("#live").classList.remove("off");
    $("#liveText").textContent = `Live · ${new Date().toLocaleTimeString("en-GB")}`;
  } catch {
    $("#live").classList.add("off");
    $("#liveText").textContent = "Offline";
    return;
  }
  S.inUse = {};
  for (const t of S.tags) if (t.kind === "asset" && t.ref && t.in_use != null) S.inUse[t.ref] = !!t.in_use;
  for (const id of busy.in_use || []) S.inUse[id] = true;
  renderDots();
  const sig = JSON.stringify(S.inUse);
  if (sig !== S.inUseSig) { S.inUseSig = sig; renderAssetList(); }
}

// ---------- devices ----------
function renderAssetList() {
  const type = $("#assetType").value;
  const assets = S.map.assets.filter((a) => a.type === type)
    .sort((a, b) => (floorOf(a.floor_id)?.level ?? 0) - (floorOf(b.floor_id)?.level ?? 0) || a.name.localeCompare(b.name, "en", { numeric: true }));
  $("#assetListTitle").textContent = `All ${(ASSET_PLURAL[type] || type).toLowerCase()} (${assets.length})`;
  $("#assetList").innerHTML = assets.map((a) => {
    const busy = !!S.inUse[a.id];
    return `<div class="item"><span class="ms" style="color:${busy ? "var(--amber)" : "var(--green)"}">${busy ? "do_not_disturb_on" : "check_circle"}</span>
      <button type="button" class="grow" data-focus-asset="${esc(a.id)}" style="border:0;background:none;text-align:left;padding:0"><div>${esc(a.name)}</div><small>${esc(where(a.space_id, a.floor_id))}</small></button>
      <button type="button" class="toggle${busy ? " busy" : ""}" data-inuse="${esc(a.id)}" data-val="${busy ? "0" : "1"}">${busy ? "in use" : "free"}</button></div>`;
  }).join("") || `<div class="item muted">No devices of this type.</div>`;
}

async function setInUse(id, value) {
  try {
    const r = await api(`/api/hospital/assets/${encodeURIComponent(id)}`, { method: "PATCH", body: { in_use: value } });
    S.inUse[id] = r && typeof r.in_use === "boolean" ? r.in_use : value;
    S.inUseSig = JSON.stringify(S.inUse);
    renderAssetList();
    const name = S.map.assets.find((a) => a.id === id)?.name || id;
    toast(`${name}: ${S.inUse[id] ? "in use" : "free"}`);
  } catch (e) {
    toast(`Could not update status (${e.message})`, "bad");
  }
}

function focusAsset(id) {
  const a = S.map.assets.find((x) => x.id === id);
  if (!a) return;
  S.hl.asset = [id];
  applyHl();
  S.plan.setFloor(a.floor_id);
  S.plan.focus(a.position[0], a.position[1], 14);
}

// ---------- optimize room ----------
function selectRoom(id, fit = true) {
  const s = spaceOf(id);
  if (!s) return;
  S.hl.room = [id];
  applyHl();
  S.plan.setFloor(s.floor_id);
  if (fit) S.plan.fit({ space_id: id });
  loadRoomOptions(id);
}

const allowedItem = (it) => it.allowed !== false && it.max_count !== 0;
const addable = (it) => allowedItem(it) && (it.can_add == null || it.can_add === true || it.can_add > 0);

function fillTypeSelect(items) {
  const sel = $("#optType"), prev = sel.value;
  const offered = items ? items.filter(allowedItem) : DEFAULT_ADD.map((type) => ({ type, can_add: null }));
  sel.innerHTML = offered.map((it) => `<option value="${esc(it.type)}" ${addable(it) ? "" : "disabled"}>${esc(ASSET_PLURAL[it.type] || it.label || it.type)}${addable(it) ? "" : " – full"}</option>`).join("")
    || `<option value="" disabled selected>Nothing allowed</option>`;
  const pick = offered.find((it) => it.type === prev && addable(it)) || offered.find(addable);
  if (pick) sel.value = pick.type;
  $("#optRun").disabled = !pick;
}

async function loadRoomOptions(id) {
  const box = $("#optRules");
  box.innerHTML = `<div class="muted">Loading room rules …</div>`;
  try {
    const o = await api(`/api/hospital/rooms/${encodeURIComponent(id)}/options`);
    if ($("#optRoom").value !== id) return;
    const items = o.items || [];
    fillTypeSelect(items);
    const rows = items.map((it) => {
      const chip = !allowedItem(it) ? `<span class="chip bad">not allowed</span>`
        : addable(it) ? `<span class="chip ok">${it.can_add > 0 ? `${it.can_add} more` : "allowed"}</span>` : `<span class="chip warn">full</span>`;
      const counts = [`${it.existing ?? 0} present`, it.max_count != null ? `max ${it.max_count}` : ""].filter(Boolean).join(" · ");
      return `<div class="item"><div class="grow"><div>${esc(it.label || ASSET_LABEL[it.type] || it.type)} <small>${esc(counts)}</small></div>
        ${it.reason ? `<small class="wrap">${esc(it.reason)}</small>` : ""}</div>${chip}</div>`;
    }).join("");
    box.innerHTML = `<div class="card rules">
      <div class="title">${esc(o.room_label || o.room_type || "")}${o.area_m2 != null ? ` <span class="muted">· ${m2(o.area_m2)}</span>` : ""}</div>
      ${o.note ? `<p class="muted" style="margin:4px 0 0">${esc(o.note)}</p>` : ""}
      ${rows ? `<div class="list" style="margin-top:10px">${rows}</div>` : `<p class="muted" style="margin:8px 0 0">No objects are planned for this room.</p>`}</div>`;
  } catch (e) {
    if ($("#optRoom").value !== id) return;
    fillTypeSelect(null);
    box.innerHTML = `<div class="muted">Room rules unavailable (${esc(e.message)}).</div>`;
  }
}

const area = (v, which) => (v == null ? null : typeof v === "number" ? (which === "after" ? v : null) : v[which]);
const m2 = (v) => (v == null ? "–" : `${Number(v).toFixed(1)} m²`);

function proposalAssets(prop) {
  const room = spaceOf(prop.space_id);
  const norm = (a) => ({ ...a, position: pos(a.position), floor_id: a.floor_id || room.floor_id, space_id: a.space_id || room.id });
  const moves = Object.fromEntries((prop.moves || []).map((m) => [m.asset_id, m]));
  const base = S.map.assets;
  const before = base;
  const after = base.map((a) => {
    const m = moves[a.id];
    if (!m) return a;
    const to = m.to || {};
    return { ...a, position: pos(to), footprint: { ...(a.footprint || {}), rotation_deg: to.rotation_deg ?? m.rotation_deg ?? a.footprint?.rotation_deg ?? 0 } };
  });
  const ghosts = (prop.moves || []).map((m) => {
    const a = base.find((x) => x.id === m.asset_id);
    const from = m.from || {};
    return a && { ...a, id: `was-${a.id}`, position: pos(from) || a.position, footprint: { ...(a.footprint || {}), rotation_deg: from.rotation_deg ?? a.footprint?.rotation_deg ?? 0 } };
  }).filter(Boolean);
  const added = (prop.added || []).map(norm);
  const beforeCls = {}, afterCls = {};
  for (const id of Object.keys(moves)) { beforeCls[id] = "moved"; afterCls[id] = "moved"; afterCls[`was-${id}`] = "was"; }
  for (const a of added) afterCls[a.id] = "added";
  return { before, after: [...ghosts, ...after, ...added], beforeCls, afterCls };
}

function showCompare() {
  const prop = S.proposal;
  if (!prop) return;
  const room = spaceOf(prop.space_id);
  $("#compare").hidden = false;
  $("#cmpTitle").textContent = `${room?.name || prop.space_id}: before / after`;
  $("#cmpBefore").textContent = `free ${m2(area(prop.free_area_m2, "before"))}`;
  $("#cmpAfter").textContent = `free ${m2(area(prop.free_area_m2, "after"))}`;
  const opts = { controls: false, floorButtons: false, pad: 18 };
  S.before ||= createPlan($("#planBefore"), S.map, opts);
  S.after ||= createPlan($("#planAfter"), S.map, opts);
  const { before, after, beforeCls, afterCls } = proposalAssets(prop);
  for (const [plan, assets, cls] of [[S.before, before, beforeCls], [S.after, after, afterCls]]) {
    plan.setMap(S.map).setFloor(room.floor_id);
    plan.setAssets(assets, cls).highlight([prop.space_id]).fit({ space_id: prop.space_id }, 0);
  }
}

async function runOptimize() {
  const id = $("#optRoom").value, type = $("#optType").value;
  const count = parseInt($("#optCount").value, 10);
  const out = $("#optResult");
  if (!type) return;
  if (!(count >= 1)) { out.innerHTML = `<div class="card err">Please enter a quantity of at least 1.</div>`; return; }
  out.innerHTML = `<div class="muted">Calculating proposal …</div>`;
  try {
    const prop = await api(`/api/hospital/rooms/${encodeURIComponent(id)}/optimize`, { method: "POST", body: { add: { type, count } } });
    S.proposal = { ...prop, space_id: prop.space_id || id };
    const moves = prop.moves?.length || 0, added = prop.added?.length || 0;
    const label = ASSET_PLURAL[type] || type, requested = prop.requested ?? count;
    const limited = prop.limited_by || prop.feasible === false;
    const fitChip = limited
      ? `<div class="limit"><span class="ms">block</span><span>Only ${prop.max_count ?? added} of ${requested} fit${prop.reason ? ` – ${esc(prop.reason)}` : ""}</span></div>`
      : `<span class="chip ok"><span class="ms" style="font-size:16px">check</span>All ${requested} ${esc(label.toLowerCase())} fit</span>`;
    out.innerHTML = `<div class="card">
      <div class="row" style="align-items:center">${fitChip}</div>
      <div class="stat">
        <div><b>${m2(area(prop.free_area_m2, "before"))}</b><small>free area before</small></div>
        <div><b>${m2(area(prop.free_area_m2, "after"))}</b><small>after</small></div>
      </div>
      <div class="stat">
        <div><b style="color:var(--amber)">${moves}</b><small>moved</small></div>
        <div><b style="color:var(--green)">${added}</b><small>new</small></div>
      </div>
      <div class="row" style="margin-top:12px">
        <button type="button" class="btn ghost" id="optShow"><span class="ms">compare</span>Before / after</button>
        <button type="button" class="btn ok" id="optApply" ${moves + added ? "" : "disabled"}><span class="ms">done_all</span>Apply</button>
      </div></div>`;
    showCompare();
  } catch (e) {
    S.proposal = null;
    out.innerHTML = e.status === 422
      ? `<div class="card err limit"><span class="ms">block</span><span>${esc(e.message)}</span></div>`
      : `<div class="card err">Proposal failed (${esc(e.message)}).</div>`;
  }
}

function setMapData(map) {
  S.map = map;
  if (map.now != null) S.offset = toMs(map.now) - Date.now();
  document.title = `Control Room · ${map.building?.name || "Hospital"}`;
}

async function reloadMap() {
  setMapData(await api("/api/hospital/map"));
  S.plan.setMap(S.map);
  fillRoomSelects(true);
  renderAssetList();
  renderPatients();
  renderStaff();
}

async function applyProposal() {
  const prop = S.proposal;
  if (!prop) return;
  try {
    await api(`/api/hospital/rooms/${encodeURIComponent(prop.space_id)}/apply`, { method: "POST", body: { proposal_id: prop.proposal_id } });
    S.proposal = null;
    $("#compare").hidden = true;
    await reloadMap();
    selectRoom(prop.space_id);
    $("#optResult").innerHTML = `<div class="card"><span class="chip ok"><span class="ms" style="font-size:16px">check</span>Applied</span>
      <p class="muted" style="margin:8px 0 0">The room is updated in the control room and for all routes.</p></div>`;
    toast("Proposal applied");
  } catch (e) {
    toast(`Apply failed (${e.message})`, "bad");
  }
}

// ---------- pagers & lifts ----------
function patientFor(holder) {
  return (S.map.patients || []).find((p) => p.id === holder || p.pager_id === holder || p.number === holder);
}

function renderPatients() {
  const list = S.map.patients || [];
  $("#patients").innerHTML = list.map((p) => `<div class="item">
      <span class="ms" style="color:${DOT_COLORS.patient}">person</span>
      <button type="button" class="grow" data-focus-patient="${esc(p.id)}" style="border:0;background:none;text-align:left;padding:0">
        <div>${esc(p.name)}</div><small>No. ${esc(p.number)} · ${esc(roomName(p.room_id))}</small></button>
      <a class="btn ghost" style="min-height:34px;padding:0 12px" href="/hospital/pager/${encodeURIComponent(p.pager_id)}" target="_blank" rel="noopener">
        <span class="ms">vibration</span>${esc(p.pager_id)}</a></div>`).join("") || `<div class="item muted">No patients.</div>`;
}

function renderStaff() {
  const list = S.map.staff || [];
  $("#staff").innerHTML = list.map((p) => `<div class="item">
      <span class="ms" style="color:${DOT_COLORS.staff}">badge</span>
      <button type="button" class="grow" data-focus-staff="${esc(p.id)}" style="border:0;background:none;text-align:left;padding:0">
        <div>${esc(p.label || p.id)}</div><small>${esc(p.role || "Staff")}</small></button>
      ${p.pager_id ? `<a class="btn ghost" style="min-height:34px;padding:0 12px" href="/hospital/pager/${encodeURIComponent(p.pager_id)}" target="_blank" rel="noopener">
        <span class="ms">vibration</span>${esc(p.pager_id)}</a>` : `<span class="muted">no pager</span>`}</div>`).join("") || `<div class="item muted">No staff.</div>`;
}

function focusTag(t) {
  if (!t?.floor_id || t.x == null) return false;
  S.hl.dot = [t.tag_id];
  applyHl();
  S.plan.setFloor(t.floor_id);
  S.plan.focus(t.x, t.y, 12);
  renderDots();
  return true;
}

function focusStaff(id) {
  const p = (S.map.staff || []).find((x) => x.id === id);
  const t = S.tags.find((x) => x.kind === "staff" && (x.ref === id || (p?.tag_id && x.tag_id === p.tag_id)))
    || S.tags.find((x) => x.ref === p?.pager_id);
  if (!focusTag(t)) toast(`${p?.label || id}: location unknown`);
}

function focusPatient(id) {
  const t = S.tags.find((x) => x.ref === id && x.kind === "patient") || S.tags.find((x) => x.ref === id && x.kind === "pager");
  const p = (S.map.patients || []).find((x) => x.id === id);
  if (t?.floor_id && t.x != null) {
    S.hl.dot = [t.tag_id];
    applyHl();
    S.plan.setFloor(t.floor_id);
    S.plan.focus(t.x, t.y, 12);
    renderDots();
  } else if (p) selectRoom(p.room_id);
}

function normLifts(data) {
  let lifts = [];
  if (data?.lifts) return normLifts(data.lifts);
  if (Array.isArray(data)) {
    if (data.length && "holder" in data[0]) {
      const by = {};
      for (const s of data) (by[s.connector_id] ||= []).push(s);
      lifts = Object.entries(by).map(([id, slots]) => ({ id, slots }));
    } else lifts = data.map((l) => ({ ...l, id: l.id ?? l.connector_id, slots: l.slots || [] }));
  } else if (data && typeof data === "object") {
    lifts = Object.entries(data).map(([id, v]) => (Array.isArray(v) ? { id, slots: v } : { ...v, id, slots: v.slots || [] }));
  }
  for (const c of S.map.connectors.filter((x) => x.kind === "lift")) if (!lifts.some((l) => l.id === c.id)) lifts.push({ id: c.id, slots: [] });
  return lifts.map((l) => ({ ...l, name: l.name || S.map.connectors.find((c) => c.id === l.id)?.name || l.id }));
}

function renderLifts(data) {
  const now = Date.now(), t0 = now + LIFT_WINDOW[0] * 1000, span = (LIFT_WINDOW[1] - LIFT_WINDOW[0]) * 1000;
  const pct = (ms) => Math.max(0, Math.min(100, ((ms - t0) / span) * 100));
  const ticks = [];
  for (let m = 0; m <= LIFT_WINDOW[1] / 60; m += 2) ticks.push(`<span style="left:${pct(now + m * 60000)}%">${m ? `+${m}` : "now"}</span>`);
  const fl = (id) => floorLabel(floorOf(id));
  const html = normLifts(data).map((l) => {
    const slots = [...l.slots].sort((a, b) => a.start - b.start);
    const visible = slots.filter((s) => toMs(s.end) >= t0 && toMs(s.start) <= t0 + span);
    const waiting = slots.filter((s) => toMs(s.start) > now).length;
    const bars = visible.map((s) => {
      const p = patientFor(s.holder), name = p?.name || s.holder;
      const left = pct(toMs(s.start)), width = Math.max(0.8, pct(toMs(s.end)) - left);
      return `<div class="slot${toMs(s.end) < now ? " past" : ""}" style="left:${left}%;width:${width}%" title="${esc(`${name}: ${hhmm(s.start)}–${hhmm(s.end)}, ${fl(s.from_floor)} → ${fl(s.to_floor)}`)}">${esc(name.split(" ").pop())}</div>`;
    }).join("");
    const next = slots.filter((s) => toMs(s.end) >= now).slice(0, 3).map((s) =>
      `<div>${hhmm(s.start)} · ${esc(patientFor(s.holder)?.name || s.holder)} <span class="muted">${esc(fl(s.from_floor))} → ${esc(fl(s.to_floor))}</span></div>`).join("");
    return `<div class="lift"><div class="lift-head"><span><span class="ms" style="font-size:18px">elevator</span> ${esc(l.name)}</span>
      <span class="chip${waiting ? " warn" : ""}">${waiting} queued</span></div>
      <div class="lift-bar">${bars}<div class="now" style="left:${pct(now)}%"></div></div>
      <div class="lift-axis">${ticks.join("")}</div>
      <div style="font-size:12px">${next || '<span class="muted">No trips scheduled.</span>'}</div></div>`;
  }).join("");
  $("#lifts").className = "";
  $("#lifts").innerHTML = html || `<span class="muted">No lifts.</span>`;
}

async function pollLifts() {
  try { renderLifts(await api("/api/hospital/lifts")); } catch (e) { $("#lifts").innerHTML = `<span class="err">Lifts unavailable (${esc(e.message)})</span>`; }
}

async function sim(action) {
  const factor = Number($("#simFactor").value) || 30;
  try {
    const r = await api(`/api/hospital/sim/${action}`, { method: "POST", body: action === "start" ? { factor } : {} });
    S.sim = r && typeof r.running === "boolean" ? r.running : action === "start";
    $("#simState").textContent = S.sim ? `running (${r?.factor ?? factor}× speed)` : "stopped";
  } catch (e) {
    toast(`Simulator: ${e.message}`, "bad");
  }
}

async function resetAll() {
  if (!confirm("Reset the demo? Positions, stops, lift bookings and room changes will be lost.")) return;
  try {
    await api("/api/hospital/reset", { method: "POST", body: {} });
    resetView();
    await reloadMap();
    applyHl();
    toast("Demo reset");
  } catch (e) {
    toast(`Reset failed (${e.message})`, "bad");
  }
}

function resetView() {
  S.inUse = {}; S.inUseSig = ""; S.proposal = null; S.placement = null; S.marks = []; S.heat = null;
  S.hl = { asset: [], room: [], dot: [] };
  $("#compare").hidden = true;
  for (const id of ["#optResult", "#optRules", "#placeResult"]) $(id).innerHTML = "";
  S.plan.drawRoute(null).setAssets(null).setHeatmap(null);
  $("#heatLegend").hidden = true;
  $("#simState").textContent = "";
}

// ---------- analytics ----------
function periodParams() {
  const p = $("#hmPeriod").value, now = serverNow();
  if (p === "today") { const d = new Date(now); d.setHours(0, 0, 0, 0); return { since: d.getTime() / 1000 }; }
  if (p === "week") return { since: now / 1000 - 7 * 86400 };
  const t = (id) => ($(id).value ? new Date($(id).value).getTime() / 1000 : undefined);
  return { since: t("#hmSince"), until: t("#hmUntil") };
}

function drawHeat() {
  const g = S.heat;
  const has = g && g.floor_id === S.plan.floor && g.max > 0;
  S.plan.setHeatmap(has ? g : null, { log: $("#hmLog").checked });
  $("#heatLegend").hidden = !has || S.tab !== "analytics";
  $("#heatMax").textContent = has ? `max. ${g.max}` : "";
}

async function refreshHeat() {
  if (S.tab !== "analytics" || !S.plan.floor) return;
  const seq = ++S.heatSeq, floor = S.plan.floor;
  const q = new URLSearchParams({ floor_id: floor });
  const add = (k, v) => { if (v !== undefined && v !== "" && v !== null && !Number.isNaN(v)) q.set(k, typeof v === "number" ? Math.round(v) : v); };
  add("tag", $("#hmTag").value); add("kind", $("#hmKind").value); add("role", $("#hmRole").value);
  const { since, until } = periodParams();
  add("since", since); add("until", until);
  $("#hmState").textContent = "Loading heatmap …";
  try {
    const g = await api(`/api/hospital/heatmap?${q}`);
    if (seq !== S.heatSeq) return;
    S.heat = g;
    drawHeat();
    $("#hmState").textContent = g?.max > 0
      ? `${floorLabel(floorOf(floor), true)} · grid ${num(g.cell_m, 1)} m`
      : `No movement in this period on ${floorLabel(floorOf(floor), true)}.`;
  } catch (e) {
    if (seq !== S.heatSeq) return;
    S.heat = null;
    drawHeat();
    $("#hmState").innerHTML = `<span class="err">Heatmap unavailable (${esc(e.message)}).</span>`;
  }
}

const KIND_GROUP = { staff: "Staff", patient: "Patients", asset: "Devices" };

function renderPeople(people) {
  const sel = $("#hmTag"), prev = sel.value;
  const groups = {};
  for (const p of people) (groups[p.kind] ||= []).push(p);
  sel.innerHTML = `<option value="">All people</option>` + Object.entries(groups).map(([kind, list]) =>
    `<optgroup label="${esc(KIND_GROUP[kind] || kind)}">${list.sort((a, b) => (a.label || a.tag_id).localeCompare(b.label || b.tag_id, "en"))
      .map((p) => `<option value="${esc(p.tag_id)}">${esc(p.label || p.tag_id)}${p.role ? ` · ${esc(p.role)}` : ""}</option>`).join("")}</optgroup>`).join("");
  sel.value = people.some((p) => p.tag_id === prev) ? prev : "";
  const roles = [...new Set(people.map((p) => p.role).filter(Boolean))].sort((a, b) => a.localeCompare(b, "en"));
  const rsel = $("#hmRole"), rprev = rsel.value;
  rsel.innerHTML = `<option value="">All roles</option>` + roles.map((r) => `<option>${esc(r)}</option>`).join("");
  rsel.value = roles.includes(rprev) ? rprev : "";
}

function renderRoutines(list) {
  $("#routines").innerHTML = list.map((r, i) => `<button type="button" class="item" data-routine="${i}">
      <span class="ms" style="color:${DOT_COLORS.staff}">route</span>
      <div class="grow"><div>${esc(r.label || r.tag_id)}${r.role ? ` <span class="muted">· ${esc(r.role)}</span>` : ""}</div>
        <small>${esc(`on ${r.days ?? "?"} day${r.days === 1 ? "" : "s"} · ${num(r.per_day, 1)}×/day${r.minutes_per_day != null ? ` · ${num(r.minutes_per_day)} min/day` : ""}`)}</small>
        <small class="wrap">${esc((r.sequence || []).map((x) => x.name || roomName(x.space_id)).join(" → "))}</small></div></button>`).join("")
    || `<div class="item muted">No recurring routes found.</div>`;
}

function renderPlaceAssets(list) {
  $("#placeAssets").innerHTML = list.map((a) => `<button type="button" class="item" data-place="${esc(a.asset_id)}">
      <span class="ms" style="color:${DOT_COLORS.asset}">${a.type === "coffee_machine" || a.type === "vending_machine" ? "local_cafe" : "inventory_2"}</span>
      <div class="grow"><div>${esc(a.name || a.asset_id)}</div><small>${esc(roomName(a.space_id))} · ${num(a.trips_per_day, 1)} trips/day</small></div>
      <span class="ms muted">chevron_right</span></button>`).join("") || `<div class="item muted">No objects with enough movement data.</div>`;
}

async function loadAnalysis() {
  S.analysis = { routines: [], assets: [] };
  const [people, routines, assets] = await Promise.allSettled([
    api("/api/hospital/history/people"), api("/api/hospital/insights/routines"), api("/api/hospital/insights/assets")]);
  if (people.status === "fulfilled") renderPeople(asList(people.value, "people"));
  const fail = (r) => `<div class="item err">Unavailable (${esc(r.reason?.message || "error")}).</div>`;
  if (routines.status === "fulfilled") renderRoutines(S.analysis.routines = asList(routines.value, "routines"));
  else $("#routines").innerHTML = fail(routines);
  if (assets.status === "fulfilled") renderPlaceAssets(S.analysis.assets = asList(assets.value, "assets"));
  else $("#placeAssets").innerHTML = fail(assets);
}

function clearAnalysisOverlay() {
  S.marks = []; S.placement = null;
  S.hl.dot = []; S.hl.room = [];
  S.plan.drawRoute(null).setAssets(null);
  document.querySelectorAll("[data-routine].sel, [data-place].sel").forEach((b) => b.classList.remove("sel"));
  applyHl(); renderDots();
}

function showRoutine(i) {
  const r = S.analysis?.routines[i];
  if (!r) return;
  clearAnalysisOverlay();
  $("#placeResult").innerHTML = "";
  document.querySelector(`[data-routine="${i}"]`)?.classList.add("sel");
  S.hl.dot = r.tag_id ? [r.tag_id] : [];
  S.hl.room = (r.sequence || []).map((x) => x.space_id).filter(Boolean);
  applyHl();
  const P = r.route?.points || [];
  if (P.length) {
    S.plan.drawRoute(r.route).setFloor(P[0].floor_id);
    S.plan.fit("route");
  } else toast("No route stored for this routine.");
  renderDots();
}

function placementMarks(res) {
  const a = res.asset || {}, base = S.map.assets.find((x) => x.id === a.id);
  const now = base?.position ? { floor_id: base.floor_id, x: base.position[0], y: base.position[1] } : null;
  const prop = res.proposal, propPos = pos(prop?.position);
  const propFloor = spaceOf(prop?.space_id)?.floor_id;
  const marks = [];
  if (now) marks.push({ id: "mark-now", kind: "mark", big: true, color: "#e37400", ...now, label: `Now: ${a.space_name || roomName(a.space_id)}`, showLabel: true });
  if (propPos && propFloor) marks.push({ id: "mark-new", kind: "mark", big: true, color: "#188038", floor_id: propFloor, x: propPos[0], y: propPos[1], label: `Proposed: ${prop.name || roomName(prop.space_id)}`, showLabel: true });
  S.marks = marks;
  const classes = {}, assets = [...S.map.assets];
  if (base) {
    classes[base.id] = "moved";
    if (propPos && propFloor) {
      assets.push({ ...base, id: `prop-${base.id}`, position: propPos, floor_id: propFloor, space_id: prop.space_id });
      classes[`prop-${base.id}`] = "added";
    }
  }
  S.plan.setAssets(assets, classes);
  S.hl.room = [a.space_id, prop?.space_id].filter(Boolean);
  S.hl.dot = (res.users || []).map((u) => u.tag_id);
  applyHl();
  const first = marks[0];
  if (first) {
    S.plan.setFloor(first.floor_id);
    const same = marks.filter((m) => m.floor_id === first.floor_id);
    const xs = same.map((m) => m.x), ys = same.map((m) => m.y);
    S.plan.fit([Math.min(...xs) - 6, Math.min(...ys) - 6, Math.max(...xs) + 6, Math.max(...ys) + 6]);
  }
  renderDots();
}

async function runPlacement(assetId) {
  clearAnalysisOverlay();
  document.querySelector(`[data-place="${CSS.escape(assetId)}"]`)?.classList.add("sel");
  const out = $("#placeResult");
  out.innerHTML = `<div class="muted">Calculating placement …</div>`;
  try {
    const res = await api("/api/hospital/insights/placement", { method: "POST", body: { asset_id: assetId } });
    S.placement = res;
    placementMarks(res);
    const a = res.asset || {}, prop = res.proposal;
    const saving = res.saving_min_per_day;
    const users = (res.users || []).map((u) => `<div class="item"><span class="ms" style="color:${DOT_COLORS.staff}">person</span>
      <div class="grow"><div>${esc(u.label || u.tag_id)}</div><small>${esc(u.role || "")}</small></div><span class="chip">${num(u.trips, 1)} trips</span></div>`).join("");
    out.innerHTML = `<div class="card">
      <div class="title">${esc(a.name || assetId)}</div>
      <div class="moveline"><span class="chip warn">Now</span><span>${esc(a.space_name || roomName(a.space_id))}</span></div>
      <div class="moveline"><span class="chip ok">Proposed</span><span>${prop ? esc(`${prop.name || roomName(prop.space_id)} · ${floorLabel(floorOf(spaceOf(prop.space_id)?.floor_id), true)}`) : "no better location"}</span></div>
      <div class="stat">
        <div><b>${num(res.minutes_per_day_before)}</b><small>min/day before</small></div>
        <div><b>${num(res.minutes_per_day_after)}</b><small>min/day after</small></div>
        <div><b style="color:${saving > 0 ? "var(--green)" : "inherit"}">${num(saving)}</b><small>min/day saved</small></div>
        <div><b>${num(res.trips_per_day, 1)}</b><small>trips/day</small></div>
      </div>
      ${res.reason ? `<p class="muted" style="margin:10px 0 0">${esc(res.reason)}</p>` : ""}
      ${users ? `<div class="list" style="margin-top:10px">${users}</div>` : ""}
      <div class="row" style="margin-top:12px">
        <button type="button" class="btn ghost" id="placeDiscard"><span class="ms">close</span>Discard</button>
        <button type="button" class="btn ok" id="placeApply" ${prop && res.proposal_id ? "" : "disabled"}><span class="ms">done_all</span>Apply</button>
      </div></div>`;
  } catch (e) {
    S.placement = null;
    out.innerHTML = `<div class="card err">Placement failed (${esc(e.message)}).</div>`;
  }
}

async function applyPlacement() {
  const res = S.placement;
  if (!res?.proposal_id) return;
  try {
    await api("/api/hospital/insights/placement/apply", { method: "POST", body: { proposal_id: res.proposal_id } });
    clearAnalysisOverlay();
    await reloadMap();
    const name = res.asset?.name || "Object", to = res.proposal?.name || roomName(res.proposal?.space_id);
    $("#placeResult").innerHTML = `<div class="card"><span class="chip ok"><span class="ms" style="font-size:16px">check</span>Applied</span>
      <p class="muted" style="margin:8px 0 0">${esc(name)} is now in ${esc(to)}. Saves about ${num(res.saving_min_per_day)} min per day.</p></div>`;
    S.hl.room = [res.proposal?.space_id].filter(Boolean);
    applyHl();
    toast(`${name} moved`);
    api("/api/hospital/insights/assets").then((r) => renderPlaceAssets(S.analysis.assets = asList(r, "assets"))).catch(() => {});
  } catch (e) {
    toast(`Apply failed (${e.message})`, "bad");
  }
}

// ---------- buildings & plan upload ----------
async function loadBuildings() {
  const sel = $("#building");
  try {
    const r = await api("/api/hospital/buildings");
    const list = asList(r, "buildings"), current = r?.current?.id ?? r?.current;
    const floors = (b) => (Array.isArray(b.floors) ? b.floors.length : b.floors);
    const opt = (b) => `<option value="${esc(b.id)}">${esc(b.name || b.id)}${floors(b) ? ` · ${floors(b)} floor${floors(b) === 1 ? "" : "s"}` : ""}</option>`;
    const group = (src, label) => { const l = list.filter((b) => (b.source || "demo") === src); return l.length ? `<optgroup label="${label}">${l.map(opt).join("")}</optgroup>` : ""; };
    sel.innerHTML = group("demo", "Demo") + group("imported", "Uploaded");
    sel.value = current || S.map?.building?.id || "";
    sel.closest(".bsel").hidden = !list.length;
  } catch {
    sel.closest(".bsel").hidden = true;
  }
}

async function selectBuilding(id) {
  await api("/api/hospital/buildings/select", { method: "POST", body: { building_id: id } });
  resetView();
  if (S.stopLifts) { S.stopLifts(); S.stopLifts = null; }
  await reloadMap();
  S.plan.fit();
  S.analysis = null;
  if (S.tab === "analytics") { loadAnalysis(); refreshHeat(); }
  if (S.tab === "pager") S.stopLifts = poll(pollLifts, 1500);
  if (S.tab === "room") selectRoom($("#optRoom").value);
  await loadBuildings();
}

function renderStaged() {
  const files = S.upload.files;
  $("#upStaged").innerHTML = files.map((f, i) => `<figure><img src="${f.url}" alt=""><button type="button" data-unstage="${i}" aria-label="Remove"><span class="ms">close</span></button>
    <figcaption>Floor ${i + 1}</figcaption></figure>`).join("");
  $("#upRun").disabled = !files.length;
}

function openUpload() {
  if (S.upload.summary) { S.upload = { files: [], summary: null }; $("#uploadOut").innerHTML = ""; }
  $("#uploadStep").hidden = false;
  renderStaged();
  $("#upload").showModal();
}

function summaryHtml(s, loadError) {
  const n = (v, one, many) => `${v} ${v === 1 ? one : many}`;
  const issues = (s.issues || []).length
    ? s.issues.map((t) => `<div class="item"><span class="ms" style="color:var(--amber)">warning</span><div class="grow wrap">${esc(t)}</div></div>`).join("")
    : `<div class="item"><span class="ms" style="color:var(--green)">check_circle</span><div class="grow wrap">No issues – every room is reachable from an entrance.</div></div>`;
  return `<div class="card">
    <div class="title">${esc(s.name)}</div>
    <div class="muted">${[n(s.floors?.length ?? 0, "floor", "floors"), n(s.rooms ?? 0, "room", "rooms"), n(s.doors ?? 0, "door", "doors"), n(s.entrances ?? 0, "entrance", "entrances"), `${s.stairs_lifts ?? 0} stairs & lifts`].join(" · ")}</div>
    ${loadError ? `<p class="err">Could not load building (${esc(loadError)}).</p>` : `<p style="margin:8px 0 0"><span class="chip ok">Loaded in control room</span></p>`}
    <div class="moveline" style="margin-top:10px"><span class="ms" ${s.size_guessed ? 'style="color:var(--amber)"' : ""}>straighten</span>
      <span>${num(s.size_m?.[0], 1)} × ${num(s.size_m?.[1], 1)} m${s.size_guessed ? " – estimated, please enter the real width" : ""}</span></div>
    <div class="row" style="margin-top:8px">
      <label class="field"><span>Width (m)</span><input id="scW" type="number" min="1" step="0.1" inputmode="decimal" value="${esc(s.size_m?.[0] ?? "")}"></label>
      <label class="field"><span>Depth (m, optional)</span><input id="scD" type="number" min="1" step="0.1" inputmode="decimal"></label>
      <button type="button" class="btn ghost" id="scApply">Resize</button>
    </div>
    <h3 style="margin-top:14px">Notes</h3>
    <div class="list" style="margin-top:6px">${issues}</div>
    <div class="row" style="margin-top:12px">
      <button type="button" class="btn ghost" id="upAgain"><span class="ms">add</span>Another building</button>
      <button type="button" class="btn" id="upDone"><span class="ms">check</span>Done</button>
    </div></div>`;
}

async function showImported(summary) {
  S.upload.summary = summary;
  let loadError = null;
  try { await selectBuilding(summary.id); } catch (e) { loadError = e.message; }
  $("#uploadStep").hidden = true;
  $("#uploadOut").innerHTML = summaryHtml(summary, loadError);
}

async function runUpload() {
  const files = S.upload.files;
  if (!files.length) return;
  const form = new FormData();
  files.forEach((f) => form.append("files", f.file, f.file.name));
  const name = $("#upName").value.trim(), width = parseFloat(String($("#upWidth").value).replace(",", "."));
  if (name) form.append("name", name);
  if (width > 0) form.append("width_m", String(width));
  $("#upRun").disabled = true;
  $("#uploadOut").innerHTML = `<div class="muted">Reading plans: rooms, doors, stairs and lifts … this takes about 10–20 seconds.</div>`;
  try {
    const res = await fetch("/api/import", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
    await showImported(body);
  } catch (e) {
    $("#uploadOut").innerHTML = `<div class="card err">Could not read plans: ${esc(e.message)}</div>`;
    $("#upRun").disabled = false;
  }
}

async function rescale() {
  const s = S.upload.summary;
  const w = parseFloat(String($("#scW").value).replace(",", ".")), d = parseFloat(String($("#scD").value).replace(",", "."));
  if (!(w > 0)) { toast("Please enter a width in metres", "bad"); return; }
  try {
    const summary = await api(`/api/import/${encodeURIComponent(s.id)}/scale`, { method: "POST", body: { width_m: w, depth_m: d > 0 ? d : null } });
    await showImported(summary);
    toast("Size updated");
  } catch (e) {
    toast(`Resize failed (${e.message})`, "bad");
  }
}

// ---------- wiring ----------
function setSelect(sel, ...candidates) {
  const ok = (v) => v && [...sel.options].some((o) => o.value === v && !o.disabled);
  sel.value = candidates.find(ok) || sel.options[0]?.value || "";
}

function fillRoomSelects(keep = false) {
  const opt = $("#optRoom").value;
  $("#optRoom").innerHTML = roomOptions(S.map);
  const first = (rt) => S.map.spaces.find((s) => s.room_type === rt && !SKIP_ROOMS.includes(s.type))?.id;
  setSelect($("#optRoom"), keep && opt, first("emergency"), first("patient_room"));
}

function setTab(tab) {
  const prev = S.tab;
  S.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".tabpane").forEach((p) => { p.hidden = p.id !== `tab-${tab}`; });
  if (tab !== "room") $("#compare").hidden = true;
  if (prev === "analytics" && tab !== "analytics") { clearAnalysisOverlay(); S.plan.setHeatmap(null); $("#heatLegend").hidden = true; }
  if (tab === "analytics") { if (!S.analysis) loadAnalysis(); refreshHeat(); }
  if (tab === "room") { if (S.proposal) showCompare(); else selectRoom($("#optRoom").value); }
  if (tab === "pager") S.stopLifts ||= poll(pollLifts, 1500);
  else if (S.stopLifts) { S.stopLifts(); S.stopLifts = null; }
}

function wire() {
  document.querySelector(".tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) setTab(b.dataset.tab); });
  $("#legend").addEventListener("change", (e) => { const k = e.target.dataset.kind; if (k) { S.show[k] = e.target.checked; renderDots(); } });
  $("#assetType").addEventListener("change", renderAssetList);
  $("#optRoom").addEventListener("change", (e) => { S.proposal = null; $("#compare").hidden = true; $("#optResult").innerHTML = ""; selectRoom(e.target.value); });
  $("#optRun").addEventListener("click", runOptimize);
  for (const id of ["#hmTag", "#hmKind", "#hmRole", "#hmSince", "#hmUntil"]) $(id).addEventListener("change", refreshHeat);
  $("#hmPeriod").addEventListener("change", (e) => { $("#hmCustom").hidden = e.target.value !== "custom"; refreshHeat(); });
  $("#hmLog").addEventListener("change", drawHeat);
  S.plan.onFloor(() => { if (S.tab === "analytics") refreshHeat(); });
  $("#building").addEventListener("change", async (e) => {
    const id = e.target.value;
    try { await selectBuilding(id); toast(`${S.map.building?.name || id} loaded`); }
    catch (err) { toast(`Could not load building (${err.message})`, "bad"); loadBuildings(); }
  });
  $("#uploadOpen").addEventListener("click", openUpload);
  $("#uploadForm").addEventListener("submit", (e) => { if (e.submitter?.value !== "close") e.preventDefault(); });
  $("#upFiles").addEventListener("change", (e) => {
    S.upload.files.push(...[...e.target.files].map((file) => ({ file, url: URL.createObjectURL(file) })));
    e.target.value = "";
    renderStaged();
  });
  $("#upRun").addEventListener("click", runUpload);
  $("#cmpClose").addEventListener("click", () => { $("#compare").hidden = true; });
  $("#simStart").addEventListener("click", () => sim("start"));
  $("#simStop").addEventListener("click", () => sim("stop"));
  $("#reset").addEventListener("click", resetAll);
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-inuse], [data-focus-asset], [data-focus-patient], [data-focus-staff], [data-routine], [data-place], [data-unstage], #optShow, #optApply, #placeApply, #placeDiscard, #scApply, #upAgain, #upDone");
    if (!t) return;
    if (t.dataset.inuse) setInUse(t.dataset.inuse, t.dataset.val === "1");
    else if (t.dataset.focusAsset) focusAsset(t.dataset.focusAsset);
    else if (t.dataset.focusPatient) focusPatient(t.dataset.focusPatient);
    else if (t.dataset.focusStaff) focusStaff(t.dataset.focusStaff);
    else if (t.dataset.routine) showRoutine(Number(t.dataset.routine));
    else if (t.dataset.place) runPlacement(t.dataset.place);
    else if (t.dataset.unstage) { S.upload.files.splice(Number(t.dataset.unstage), 1); renderStaged(); }
    else if (t.id === "optShow") showCompare();
    else if (t.id === "optApply") applyProposal();
    else if (t.id === "placeApply") applyPlacement();
    else if (t.id === "placeDiscard") { clearAnalysisOverlay(); $("#placeResult").innerHTML = ""; }
    else if (t.id === "scApply") rescale();
    else if (t.id === "upAgain") { S.upload = { files: [], summary: null }; $("#uploadOut").innerHTML = ""; $("#uploadStep").hidden = false; renderStaged(); }
    else if (t.id === "upDone") $("#upload").close();
  });
  S.plan.onTap((hit) => {
    if (hit.kind === "dot") {
      const tag = S.tags.find((t) => t.tag_id === hit.id);
      S.hl.dot = [hit.id];
      applyHl(); renderDots();
      if (tag) toast(`${tag.label} · ${where(tag.space_id, tag.floor_id)}`);
      return;
    }
    S.hl.dot = [];
    if (hit.kind === "asset") { const a = S.map.assets.find((x) => x.id === hit.id); if (a) toast(`${a.name} · ${S.inUse[a.id] ? "in use" : "free"}`); }
    const space = hit.kind === "space" ? spaceOf(hit.id) : null;
    if (!space || ["corridor", "lift", "stair", "outdoor"].includes(space.type)) { applyHl(); renderDots(); return; }
    if (S.tab === "devices") S.hl.room = [space.id];
    else if (S.tab === "room") { $("#optRoom").value = space.id; S.proposal = null; $("#optResult").innerHTML = ""; S.hl.room = [space.id]; loadRoomOptions(space.id); }
    applyHl(); renderDots();
  });
}

async function boot() {
  try {
    setMapData(await api("/api/hospital/map"));
  } catch (e) {
    $("#plan").innerHTML = `<div class="card err" style="margin:16px;background:#fff">Could not load building plan (${esc(e.message)}).</div>`;
    loadBuildings().catch(() => {});
    return;
  }
  S.plan = createPlan($("#plan"), S.map, { floorCounts: true });
  document.querySelector(".heat-bar").style.background = HEAT_GRADIENT;
  renderLegend();
  fillRoomSelects();
  fillTypeSelect(null);
  renderAssetList();
  renderPatients();
  renderStaff();
  wire();
  loadBuildings();
  poll(pollTracking, 1000);
}

boot();
