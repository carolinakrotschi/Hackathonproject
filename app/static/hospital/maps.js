import { $, esc, api, toast, poll, asList } from "/static/hospital/common.js";
import { createPlan, floorLabel, MANEUVER, ROOM_LABELS } from "/static/hospital/plan.js";

const TYPES = {
  bed: ["Bed", "Beds", "bed", "hospital bed"],
  stretcher: ["Stretcher", "Stretchers", "airline_seat_flat", "gurney", "gurneys", "trolley", "trolleys"],
  wheelchair: ["Wheelchair", "Wheelchairs", "accessible", "wheel chair", "wheel chairs"],
  infusion_pump: ["Infusion pump", "Infusion pumps", "medication_liquid", "pump", "pumps", "infusion", "drip"],
  coffee_machine: ["Coffee machine", "Coffee machines", "coffee_maker", "coffee", "espresso"],
  vending_machine: ["Vending machine", "Vending machines", "storefront", "vending", "snack", "snacks", "drinks"],
  water_dispenser: ["Water dispenser", "Water dispensers", "water_full", "water", "water fountain"],
  defibrillator: ["Defibrillator", "Defibrillators", "ecg_heart", "defib", "aed"],
  monitor: ["Patient monitor", "Patient monitors", "monitor_heart", "monitor", "monitors"],
  ventilator: ["Ventilator", "Ventilators", "air", "respirator"],
  xray: ["X-ray machine", "X-ray machines", "radiology", "x-ray", "xray", "x ray"],
  ct_scanner: ["CT scanner", "CT scanners", "radiology", "ct", "scanner"],
  mri_scanner: ["MRI scanner", "MRI scanners", "radiology", "mri", "scanner"],
  ultrasound: ["Ultrasound", "Ultrasound machines", "sensors", "sonography"],
  operating_table: ["Operating table", "Operating tables", "table_restaurant", "op table"],
  anaesthesia_machine: ["Anaesthesia machine", "Anaesthesia machines", "masks", "anaesthesia", "anesthesia"],
  therapy_table: ["Therapy table", "Therapy tables", "table_restaurant"],
  chair: ["Chair", "Chairs", "chair"], bench: ["Bench", "Benches", "chair"], table: ["Table", "Tables", "table_restaurant"],
  desk: ["Desk", "Desks", "desk"], cabinet: ["Cabinet", "Cabinets", "shelves"], shelf: ["Shelf", "Shelves", "shelves"],
  counter: ["Counter", "Counters", "countertops"],
};
const ROOMS = {
  toilet: ["wc", "toilet", "toilets", "wc", "restroom", "restrooms", "loo", "washroom"],
  bathroom: ["shower", "bathroom", "bathrooms", "shower"],
  radiology: ["radiology", "radiology", "imaging", "x-ray", "xray"],
  cafe: ["local_cafe", "cafe", "cafeteria", "coffee", "canteen"],
  kiosk: ["storefront", "kiosk", "shop"],
  pharmacy: ["local_pharmacy", "pharmacy", "medicine"],
  emergency: ["emergency", "emergency", "er", "a&e", "casualty"],
  lobby: ["door_front", "entrance", "lobby", "foyer", "main entrance"],
  reception: ["support_agent", "reception", "admissions", "information", "front desk"],
  patient_room: ["hotel", "patient room", "patient rooms"],
  nurse_station: ["medical_services", "nurse", "nurses", "nurse station", "nurses station"],
  operating_theatre: ["health_and_safety", "operating theatre", "operating room", "theatre", "surgery", "or"],
  recovery: ["healing", "recovery"],
  waiting: ["chair", "waiting", "waiting room", "waiting area"],
  lounge: ["weekend", "lounge", "break room", "staff room"],
  lab: ["science", "lab", "labs", "laboratory"],
  triage: ["emergency", "triage"],
  resuscitation: ["emergency", "resus", "resuscitation"],
  treatment_bay: ["medical_services", "treatment", "treatment bay"],
  storage: ["inventory_2", "storage", "store room", "supplies"],
  utility: ["cleaning_services", "utility", "sluice"],
  office: ["work", "office", "offices"],
  therapy: ["self_improvement", "therapy", "physio", "physiotherapy"],
  consulting: ["stethoscope", "consulting", "consultation", "outpatients"],
  lift: ["elevator", "lift", "lifts", "elevator", "elevators"],
  stair: ["stairs", "stairs", "stairwell", "staircase"],
};
const CHIPS = ["bed", "stretcher", "wheelchair", "infusion_pump"];
const EXAMPLES = ["free stretcher", "wheelchair", "infusion pump", "toilet", "radiology", "cafe"];
const SKIP_ROOMS = ["corridor", "outdoor"];
const STOP = new Set(["a", "an", "the", "all", "any", "find", "show", "me", "where", "is", "are", "nearest", "closest", "near", "nearby", "some", "in", "on", "of", "for", "please", "i", "need"]);
const FREE = new Set(["free", "available", "unused", "idle"]);
const WIDE = "(min-width: 641px)";

const S = {
  map: null, plan: null, inUse: {}, inUseSig: "", buildings: [], current: null,
  query: "", items: [], committed: false, sel: null, hot: null, suggest: [], cursor: -1, files: [], imported: null,
};

const norm = (t) => String(t ?? "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9&\- ]+/g, " ").replace(/\s+/g, " ").trim();
const titleCase = (t) => String(t || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const typeInfo = (type) => {
  const [one, many, icon, ...extra] = TYPES[type] || [titleCase(type), `${titleCase(type)}s`, "medical_services"];
  return { one, many, icon, terms: [one, many, ...extra].map(norm) };
};
const roomKey = (s) => (s.type === "lift" ? "lift" : s.type === "stair" ? "stair" : s.room_type || s.type);
const roomInfo = (key) => {
  const [icon, ...terms] = ROOMS[key] || ["meeting_room"];
  const label = ROOM_LABELS[key] || titleCase(key);
  return { icon, label, terms: [label, ...terms].map(norm) };
};
const spaceOf = (id) => S.map.spaces.find((s) => s.id === id);
const floorOf = (id) => S.map.floors.find((f) => f.id === id);
const plural = (n, info) => `${n} ${(n === 1 ? info.one : info.many).toLowerCase()}`;
const isFree = (item) => item.kind === "asset" && !S.inUse[item.id];
const whereOf = (item) => where(item.space_id, item.floor_id, item.kind === "asset");

function where(spaceId, floorId, withRoom = true) {
  const s = spaceOf(spaceId);
  const b = s?.building && (S.map.buildings || []).find((x) => x.id === s.building);
  return [withRoom && s && !SKIP_ROOMS.includes(s.type) ? s.name : "", floorLabel(floorOf(floorId || s?.floor_id), true), b?.name].filter(Boolean).join(" · ");
}

// ---------- search ----------
function parse(raw) {
  const words = norm(raw).split(" ").filter(Boolean);
  return { q: words.filter((w) => !FREE.has(w) && !STOP.has(w)).join(" "), free: words.some((w) => FREE.has(w)) };
}

function hits(q, terms, partial) {
  if (!q) return false;
  const padded = ` ${q} `;
  return terms.some((t) => t === q || padded.includes(` ${t} `) || (partial && q.length >= 2 && t.startsWith(q)));
}

const assetItem = (a) => ({ kind: "asset", id: a.id, name: a.name || typeInfo(a.type).one, type: a.type, icon: typeInfo(a.type).icon,
  floor_id: a.floor_id, space_id: a.space_id, x: a.position?.[0], y: a.position?.[1] });

function roomItem(s) {
  const info = roomInfo(roomKey(s));
  const [x, y] = s.anchor || centre(s.polygon);
  return { kind: "room", id: s.id, name: s.name, type: roomKey(s), icon: info.icon, label: info.label, floor_id: s.floor_id, space_id: s.id, x, y };
}

function centre(poly) {
  const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
  return [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
}

const named = (name, q) => q.length >= 2 && ` ${norm(name)}`.includes(` ${q}`);
const level = (item) => floorOf(item.floor_id)?.level ?? 0;
const byPlace = (a, b) => level(a) - level(b) || a.name.localeCompare(b.name, "en", { numeric: true });
const byFree = (a, b) => isFree(b) - isFree(a) || byPlace(a, b);

function search(raw) {
  const { q, free } = parse(raw);
  const assets = S.map.assets.filter((a) => a.position);
  const types = [...new Set(assets.map((a) => a.type))].filter((t) => hits(q, typeInfo(t).terms, true));
  const rooms = S.map.spaces.filter((s) => !SKIP_ROOMS.includes(s.type));
  const roomTypes = new Set(rooms.map(roomKey).filter((k) => hits(q, roomInfo(k).terms, q.length >= 3)));
  const seen = new Set();
  const take = (list) => list.filter((x) => !seen.has(x.id) && seen.add(x.id));

  if (!q) {
    const tracked = new Set(Object.keys(S.inUse));
    const list = free ? take(assets.filter((a) => tracked.has(a.id) || CHIPS.includes(a.type)).map(assetItem)) : [];
    return { q, free, types: [], items: list.filter(isFree).sort(byPlace) };
  }
  let typed = assets.filter((a) => types.includes(a.type));
  const inRooms = roomTypes.size ? typed.filter((a) => roomTypes.has(roomKey(spaceOf(a.space_id) || {}))) : [];
  const narrowed = inRooms.length > 0;
  if (narrowed) typed = inRooms;
  const typedItems = take(typed.map(assetItem)).sort(byFree);
  const roomItems = narrowed ? [] : take(rooms.filter((s) => roomTypes.has(roomKey(s)) || named(s.name, q) || (q && norm(s.ref) === q)).map(roomItem)).sort(byPlace);
  const nameItems = narrowed ? [] : take(assets.filter((a) => named(a.name, q)).map(assetItem)).sort(byFree);
  let items = [...typedItems, ...roomItems, ...nameItems];
  if (free) items = items.filter(isFree);
  return { q, free, types, items };
}

function header(res) {
  const assets = res.items.filter((i) => i.kind === "asset");
  const nFree = assets.filter(isFree).length;
  const oneType = res.types.length === 1 && assets.length === res.items.length;
  if (oneType) {
    const info = typeInfo(res.types[0]);
    return res.free ? `${plural(res.items.length, info).replace(/^(\d+) /, "$1 free ")}` : `${plural(res.items.length, info)} · ${nFree} free`;
  }
  const n = res.items.length;
  return `${n} result${n === 1 ? "" : "s"}${assets.length && !res.free ? ` · ${nFree} free` : ""}`;
}

// ---------- results panel ----------
function statusHtml(item) {
  if (item.kind !== "asset") return `<span class="kind">${esc(item.label)}</span>`;
  return isFree(item) ? `<span class="st free">Free</span>` : `<span class="st busy">In use</span>`;
}

function rowHtml(item) {
  return `<button type="button" class="row${item.id === S.sel ? " on" : ""}" data-item="${esc(item.id)}">
    <span class="row-ic ${item.kind === "asset" ? (isFree(item) ? "free" : "busy") : "room"}"><span class="ms">${esc(item.icon)}</span></span>
    <span class="row-txt"><b>${esc(item.name)}</b>${statusHtml(item)}<small>${esc(whereOf(item))}</small></span></button>`;
}

function emptyHtml(raw) {
  const present = EXAMPLES.filter((e) => search(e).items.length);
  if (present.length < 3) {
    const rooms = S.map.spaces.filter((s) => s.type === "room" || s.type === "hall").map((s) => s.name);
    present.push(...[...new Set(rooms)].sort((a, b) => a.localeCompare(b, "en", { numeric: true })).slice(0, 4 - present.length));
  }
  return `<div class="empty"><span class="ms">search_off</span><b>No results for “${esc(raw.trim())}”</b>
    <p>Search for equipment or a room${present.length ? ", for example:" : "."}</p>
    <div class="examples">${present.map((e) => `<button type="button" class="chip" data-example="${esc(e)}">${esc(e)}</button>`).join("")}</div></div>`;
}

function renderResults() {
  const box = $("#results");
  box.hidden = !S.committed;
  document.body.classList.toggle("open", S.committed);
  if (!S.committed) return;
  const res = S.res;
  $("#resultsHead").innerHTML = res.items.length ? `<h1>${esc(header(res))}</h1>` : "";
  $("#resultsList").innerHTML = res.items.length ? res.items.map(rowHtml).join("") : emptyHtml(S.query);
}

// ---------- suggestions while typing ----------
function renderSuggest() {
  const box = $("#suggest");
  const raw = $("#q").value;
  if (!raw.trim() || document.activeElement !== $("#q")) { box.hidden = true; $("#side").classList.remove("suggesting"); return; }
  const res = search(raw);
  const typeRows = res.types.slice(0, 3).map((t) => {
    const info = typeInfo(t), all = S.map.assets.filter((a) => a.type === t && a.position);
    const free = all.filter((a) => !S.inUse[a.id]).length;
    return { kind: "type", id: t, icon: info.icon, name: res.free ? `Free ${info.many.toLowerCase()}` : info.many, sub: `${all.length} in this building · ${free} free`, q: res.free ? `free ${info.one}` : info.many };
  });
  const rest = res.items.filter((i) => !res.types.includes(i.type)).slice(0, 6 - typeRows.length)
    .map((i) => ({ kind: i.kind, id: i.id, icon: i.icon, name: i.name, sub: whereOf(i) }));
  S.suggest = [...typeRows, ...rest];
  S.cursor = Math.min(S.cursor, S.suggest.length - 1);
  box.innerHTML = S.suggest.map((s, i) => `<button type="button" role="option" class="sg${i === S.cursor ? " on" : ""}" data-sg="${i}">
      <span class="ms">${esc(s.kind === "type" ? "search" : s.icon)}</span><span class="sg-txt"><b>${esc(s.name)}</b><small>${esc(s.sub)}</small></span></button>`).join("")
    || `<div class="sg none"><span class="ms">search</span><span class="sg-txt"><b>Search for “${esc(raw.trim())}”</b></span></div>`;
  box.hidden = false;
  $("#side").classList.add("suggesting");
}

function pickSuggest(i) {
  const s = S.suggest[i];
  if (!s) return commit($("#q").value);
  if (s.kind === "type") return commit(s.q);
  commit($("#q").value);
  select(s.id);
}

// ---------- state changes ----------
function preview(raw) {
  S.query = raw;
  S.committed = false;
  S.res = raw.trim() ? search(raw) : { items: [], types: [] };
  S.items = S.res.items;
  S.sel = null;
  $("#clear").hidden = !raw;
  renderResults(); renderPlace(); renderChips(); paintMap(); renderSuggest();
}

function commit(raw) {
  if (!raw.trim()) return clearSearch();
  $("#q").value = raw;
  S.query = raw;
  S.committed = true;
  S.res = search(raw);
  S.items = S.res.items;
  S.sel = null;
  $("#clear").hidden = false;
  $("#q").blur();
  renderSuggest(); renderResults(); renderPlace(); renderChips();
  const floors = new Set(S.items.map((i) => i.floor_id));
  if (S.items.length && !floors.has(S.plan.floor)) S.plan.setFloor(S.items[0].floor_id);
  paintMap();
  fitItems(S.items.filter((i) => i.floor_id === S.plan.floor && i.x != null));
}

function fitItems(list) {
  if (!list.length) return;
  const xs = list.map((i) => i.x), ys = list.map((i) => i.y), m = 8;
  S.plan.fit([Math.min(...xs) - m, Math.min(...ys) - m, Math.max(...xs) + m, Math.max(...ys) + m], 500);
}

function zoomTo(item, z = 16) {
  const el = $("#plan"), w = (el.clientWidth - 96) / z / 2, h = (el.clientHeight - 96) / z / 2;
  S.plan.fit([item.x - w, item.y - h, item.x + w, item.y + h], 450);
}

function clearSearch() {
  $("#q").value = "";
  S.query = ""; S.committed = false; S.res = { items: [], types: [] }; S.items = []; S.sel = null;
  $("#clear").hidden = true;
  renderSuggest(); renderResults(); renderPlace(); renderChips(); paintMap();
}

function itemFor(id) {
  const found = S.items.find((i) => i.id === id);
  if (found) return found;
  const a = S.map.assets.find((x) => x.id === id);
  if (a?.position) return assetItem(a);
  const s = spaceOf(id);
  return s && !SKIP_ROOMS.includes(s.type) ? roomItem(s) : null;
}

function select(id, fly = true) {
  const item = itemFor(id);
  if (!item) { S.sel = null; renderPlace(); paintMap(); return; }
  S.sel = id;
  S.selItem = item;
  S.plan.setFloor(item.floor_id);
  if (fly) {
    if (item.kind === "room") S.plan.fit({ space_id: item.id });
    else zoomTo(item);
  }
  document.querySelectorAll("#resultsList .row").forEach((r) => r.classList.toggle("on", r.dataset.item === id));
  document.querySelector(`#resultsList [data-item="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  renderPlace(); paintMap();
}

function deselect() {
  S.sel = null;
  document.querySelectorAll("#resultsList .row.on").forEach((r) => r.classList.remove("on"));
  renderPlace(); paintMap();
}

// ---------- place card ----------
function contents(spaceId) {
  const counts = {};
  for (const a of S.map.assets) if (a.space_id === spaceId && a.position) counts[a.type] = (counts[a.type] || 0) + 1;
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([t, n]) => plural(n, typeInfo(t))).join(" · ");
}

function renderPlace() {
  const card = $("#place");
  const item = S.sel && (S.items.find((i) => i.id === S.sel) || S.selItem);
  document.body.classList.toggle("placed", !!item);
  if (!item) { card.hidden = true; return; }
  const free = isFree(item);
  const sub = item.kind === "asset"
    ? `<p class="place-st">${free ? `<span class="st free">Free</span>` : `<span class="st busy">In use</span>`}<span class="dot">·</span>${esc(typeInfo(item.type).one)}</p>`
    : `<p class="place-st"><span class="kind">${esc(item.label)}</span></p>`;
  const inside = item.kind === "room" ? contents(item.id) : "";
  card.innerHTML = `<span class="place-ic ${item.kind === "asset" ? (free ? "free" : "busy") : "room"}"><span class="ms">${esc(item.icon)}</span></span>
    <div class="place-txt"><h2>${esc(item.name)}</h2>${sub}
      <p class="place-where"><span class="ms">location_on</span>${esc(whereOf(item))}</p>
      ${inside ? `<p class="place-where"><span class="ms">inventory_2</span>${esc(inside)}</p>` : ""}
      <button type="button" class="dir-btn" id="placeDir"><span class="ms">directions</span>Directions</button></div>
    <button type="button" class="ibtn" id="placeClose" aria-label="Close"><span class="ms">close</span></button>`;
  card.hidden = false;
  document.documentElement.style.setProperty("--card-h", `${card.offsetHeight}px`);
}

// ---------- map: markers drawn over plan.js ----------
function paintMap() {
  if (!S.plan) return;
  const cls = {};
  for (const i of S.items) if (i.kind === "asset") cls[i.id] = isFree(i) ? "m" : "m busy";
  S.plan.setAssets(null, cls);
  const rooms = S.items.filter((i) => i.kind === "room").map((i) => i.id);
  S.plan.highlight([...rooms, ...(S.sel ? [S.sel] : [])]);
  renderPins();
  markFloors();
}

function renderPins() {
  const floor = S.plan.floor;
  const list = S.items.filter((i) => i.floor_id === floor && i.x != null);
  const sel = S.sel && S.selItem && S.selItem.floor_id === floor && S.selItem.x != null ? (S.items.find((i) => i.id === S.sel) || S.selItem) : null;
  const dots = list.filter((i) => i.id !== S.sel).map((i) =>
    `<button type="button" class="pin dot ${i.kind === "room" ? "room" : isFree(i) ? "" : "busy"}${i.id === S.hot ? " hot" : ""}" data-pin="${esc(i.id)}" data-x="${i.x}" data-y="${i.y}" aria-label="${esc(i.name)}" title="${esc(i.name)}"></button>`);
  if (sel) dots.push(`<button type="button" class="pin big ${sel.kind === "asset" && !isFree(sel) ? "busy" : ""}" data-pin="${esc(sel.id)}" data-x="${sel.x}" data-y="${sel.y}" aria-label="${esc(sel.name)}">
      <svg viewBox="0 0 28 40" aria-hidden="true"><path d="M14 1C6.8 1 1 6.7 1 13.9 1 23.6 14 39 14 39s13-15.4 13-25.1C27 6.7 21.2 1 14 1z"/></svg><span class="ms">${esc(sel.icon)}</span></button>`);
  S.pins.innerHTML = dots.join("");
  placePins();
}

function placePins() {
  const m = S.world.transform.baseVal.consolidate()?.matrix;
  if (!m) return;
  for (const p of S.pins.children) {
    const x = +p.dataset.x, y = -p.dataset.y;
    p.style.translate = `${m.a * x + m.c * y + m.e}px ${m.b * x + m.d * y + m.f}px`;
  }
}

function markFloors() {
  const floors = new Set(S.items.map((i) => i.floor_id));
  document.querySelectorAll("#plan .plan-floors [data-floor]").forEach((b) => b.classList.toggle("has", floors.has(b.dataset.floor) && b.dataset.floor !== S.plan.floor));
}

function setHot(id) {
  S.hot = id;
  for (const p of S.pins.children) p.classList.toggle("hot", p.dataset.pin === id);
}

// ---------- chips ----------
function renderChips() {
  const present = CHIPS.filter((t) => S.map.assets.some((a) => a.type === t && a.position));
  const active = S.committed && S.res.types.length === 1 ? S.res.types[0] : null;
  $("#chips").innerHTML = present.map((t) => {
    const info = typeInfo(t);
    return `<button type="button" class="chip${t === active ? " on" : ""}" data-chip="${t}"><span class="ms">${info.icon}</span>${info.many}</button>`;
  }).join("");
  $("#chips").hidden = !present.length;
}

// ---------- live status ----------
async function pollTracking() {
  const [tags, status] = await Promise.all([api("/api/hospital/tracking"), api("/api/hospital/assets/in_use")]);
  const next = {};
  for (const t of asList(tags, "tags")) if (t.kind === "asset" && t.ref && t.in_use != null) next[t.ref] = !!t.in_use;
  for (const id of status.in_use || []) next[id] = true;
  const sig = JSON.stringify(next);
  if (sig === S.inUseSig) return;
  S.inUse = next;
  S.inUseSig = sig;
  if (S.committed) {
    S.res = search(S.query);
    S.items = S.res.items;
    renderResults();
  }
  renderPlace(); paintMap();
}

// ---------- directions: A to B with the pager's planner ----------
function openDirections(to = null) {
  clearSearch();
  S.dir = { from: null, to, stepFree: S.dirStepFree || false, field: "from", result: null, error: null, sug: [] };
  document.body.classList.add("dir");
  $("#dir").hidden = false;
  $("#dFrom").value = "";
  $("#dTo").value = to?.name || "";
  $("#dStep").checked = S.dir.stepFree;
  renderDir();
  $("#dFrom").focus();
}

function closeDirections() {
  S.dir = null;
  document.body.classList.remove("dir");
  $("#dir").hidden = true;
  S.plan.drawRoute(null);
  paintMap();
}

function dirSuggest(field) {
  const d = S.dir;
  d.field = field;
  const raw = $(field === "from" ? "#dFrom" : "#dTo").value;
  d.sug = raw.trim() ? search(raw).items.slice(0, 6) : [];
  const box = $("#dSug");
  box.hidden = !raw.trim();
  box.innerHTML = d.sug.map((i, n) => `<button type="button" class="sg" data-dsg="${n}"><span class="ms">${esc(i.icon)}</span>
      <span class="sg-txt"><b>${esc(i.name)}</b><small>${esc(whereOf(i))}</small></span></button>`).join("")
    || `<div class="sg none"><span class="ms">search_off</span><span class="sg-txt"><b>Nothing called “${esc(raw.trim())}”</b></span></div>`;
}

function setEnd(item, field = S.dir.field) {
  const d = S.dir;
  d[field] = item;
  $(field === "from" ? "#dFrom" : "#dTo").value = item.name;
  $("#dSug").hidden = true;
  const other = field === "from" ? "to" : "from";
  d.field = d[other] ? field : other;
  if (!d[other]) $(other === "from" ? "#dFrom" : "#dTo").focus();
  else document.activeElement?.blur();
  plotRoute();
}

async function plotRoute() {
  const d = S.dir;
  if (!d) return;
  d.result = null; d.error = null;
  if (!d.from || !d.to) { renderDir(); return; }
  const want = `${d.from.id}|${d.to.id}|${d.stepFree}`;
  d.want = want;
  renderDir(true);
  let result = null, error = null;
  try {
    result = await api("/api/hospital/directions", { method: "POST", body: { origin: d.from.id, destination: d.to.id, step_free: d.stepFree } });
  } catch (e) { error = e.message; }
  if (S.dir !== d || d.want !== want) return; // changed while asking
  d.result = result; d.error = error;
  renderDir();
  if (result) showRoute(result.route, d.from.floor_id);
}

function showRoute(route, floorId) {
  S.plan.drawRoute(route, { fromIndex: 0 });
  S.plan.highlight([S.dir.from.space_id, S.dir.to.space_id]);
  S.plan.setFloor(floorId);
  fitPoints(route.points.filter((p) => p.floor_id === floorId));
}

function fitPoints(points) {
  if (!points.length) return;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y), m = 6;
  S.plan.fit([Math.min(...xs) - m, Math.min(...ys) - m, Math.max(...xs) + m, Math.max(...ys) + m], 500);
}

function stepsHtml(route) {
  let floor = null;
  return route.instructions.map((step, n) => {
    const p = route.points[step.index] || route.points[0];
    const head = step.floor_id !== floor && (floor = step.floor_id) ? `<div class="dir-floor">${esc(floorLabel(floorOf(step.floor_id), true))}</div>` : "";
    const [icon] = MANEUVER[step.maneuver] || MANEUVER.straight;
    return `${head}<button type="button" class="dir-step" data-step="${n}" data-floor="${esc(p.floor_id)}" data-x="${p.x}" data-y="${p.y}">
      <span class="ms">${esc(icon)}</span><span>${esc(step.text)}${step.detail ? `<small>${esc(step.detail)}</small>` : ""}</span></button>`;
  }).join("");
}

function renderDir(busy = false) {
  const d = S.dir;
  if (!d) return;
  $("#dStep").checked = d.stepFree;
  const out = $("#dOut");
  if (busy) { out.innerHTML = `<p class="dir-note"><span class="ms spin">progress_activity</span>Finding the best way…</p>`; return; }
  if (d.error) { out.innerHTML = `<p class="dir-note bad"><span class="ms">block</span>${esc(d.error)}</p>`; S.plan.drawRoute(null); return; }
  if (!d.result) {
    out.innerHTML = `<p class="dir-note"><span class="ms">touch_app</span>Type a room or item, or tap one on the map.</p>`;
    S.plan.drawRoute(null);
    return;
  }
  const r = d.result, via = r.choice && r.choice !== "walk" ? `via ${r.choice.replace(/^lift /, "")}` : "on one floor";
  out.innerHTML = `<div class="dir-sum"><b>${Math.max(1, Math.round(r.minutes))} min</b><span>(${Math.round(r.walk_m)} m)</span>
      <small>${esc(via)}${r.step_free ? " · step-free" : ""}</small></div>
    <div class="dir-steps">${stepsHtml(r.route)}</div>`;
}

// ---------- assistant: levels 1-4 by conversation (Claude with the same tools as the pager) ----------
const ASK_EXAMPLES = [
  ["search", "Where is the CT?"],
  ["directions", "How do I get from the main entrance to the pharmacy without stairs?"],
  ["accessible", "I need a free wheelchair near the cafeteria"],
  ["block", "Lift A is out of order"],
  ["door_front", "Add a door between Room 2.03 and Room 2.04"],
  ["edit", "Room 1.05 is the plaster room"],
];
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const SESSION = Math.random().toString(36).slice(2);

function openAsk(text = "") {
  if (S.dir) closeDirections();
  clearSearch();
  document.body.classList.add("asking");
  $("#ask").hidden = false;
  S.ask = S.ask || { log: [], busy: false };
  renderAsk();
  $("#askIn").value = "";
  if (text) send(text); else $("#askIn").focus();
}

function closeAsk() {
  S.recog?.abort();
  document.body.classList.remove("asking");
  $("#ask").hidden = true;
  S.plan.drawRoute(null);
  S.items = []; S.sel = null;
  renderPlace(); paintMap();
}

const rich = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

function renderAsk() {
  const log = $("#askLog");
  const turns = S.ask.log.map((t, n) => t.who === "me"
    ? `<p class="bub me">${esc(t.text)}</p>`
    : `<div class="bub ai${t.ok === false ? " bad" : ""}"><p>${rich(t.text)}</p>${t.route ? `<button type="button" class="tbtn steps" data-route="${n}"><span class="ms">directions</span>Show steps</button>` : ""}</div>`).join("");
  const examples = S.ask.log.length ? "" : `<div class="ask-hello"><p>I know every room, door, lift and piece of equipment in this building. Try:</p>
    ${ASK_EXAMPLES.map(([icon, text]) => `<button type="button" class="ex" data-ask="${esc(text)}"><span class="ms">${icon}</span>${esc(text)}</button>`).join("")}</div>`;
  log.innerHTML = examples + turns + (S.ask.busy ? `<p class="bub ai wait"><span class="ms spin">progress_activity</span>Thinking…</p>` : "");
  log.scrollTop = log.scrollHeight;
  $("#askMic").hidden = !Recognition;
}

function pageContext() {
  const floor = floorOf(S.plan.floor);
  const sel = S.sel && itemFor(S.sel);
  return [floor ? `Showing ${floorLabel(floor, true)}.` : "", sel ? `Selected: ${sel.name} (${whereOf(sel)}).` : ""].filter(Boolean).join(" ");
}

async function send(text) {
  text = text.trim();
  if (!text || S.ask.busy) return;
  S.ask.log.push({ who: "me", text });
  S.ask.busy = true;
  renderAsk();
  let r;
  try {
    r = await api("/api/hospital/maps/ask", { method: "POST", body: { text, session: SESSION, page: pageContext() } });
  } catch (e) {
    r = { say: e.message, ok: false };
  }
  S.ask.busy = false;
  S.ask.log.push({ who: "ai", text: r.say, ok: r.ok, route: r.route || null });
  renderAsk();
  if (r.changed) await refreshMap();
  showAnswer(r);
}

function showAnswer(r) {
  S.plan.drawRoute(null);
  if (r.route) {
    S.items = []; S.sel = null; renderPlace(); paintMap();
    S.plan.drawRoute(r.route.route, { fromIndex: 0 });
    S.plan.highlight([r.route.from.space_id, r.route.to.space_id]);
    S.plan.setFloor(r.route.from.floor_id);
    fitPoints(r.route.route.points.filter((p) => p.floor_id === r.route.from.floor_id));
    return;
  }
  const ids = [...(r.view?.assets || []), ...(r.view?.spaces || [])];
  const items = [...new Map(ids.map(itemFor).filter(Boolean).map((i) => [i.id, i])).values()];
  if (!items.length) return;
  if (items.length === 1) { S.items = items; select(items[0].id); return; }
  S.items = items; S.sel = null; renderPlace();
  if (!items.some((i) => i.floor_id === S.plan.floor)) S.plan.setFloor(items[0].floor_id);
  paintMap();
  fitItems(items.filter((i) => i.floor_id === S.plan.floor && i.x != null));
}

// A route from the assistant opens in the directions panel with its steps.
function showSteps(result) {
  const end = (e) => itemFor(e.asset_id || e.space_id) || { id: e.space_id, name: e.name, floor_id: e.floor_id, space_id: e.space_id };
  closeAsk();
  openDirections(end(result.to));
  const d = S.dir;
  d.from = end(result.from);
  d.stepFree = S.dirStepFree = !!result.step_free;
  $("#dFrom").value = d.from.name;
  $("#dFrom").blur();
  d.result = result;
  renderDir();
  showRoute(result.route, d.from.floor_id);
}

// The building changed (closure, door, label, move): redraw the plan where it is.
async function refreshMap() {
  try { S.map = await api("/api/hospital/map"); } catch { return; }
  const floor = S.plan.floor;
  S.plan.setMap(S.map);
  S.plan.setFloor(floor);
  renderChips(); paintMap();
}

function listen() {
  if (S.recog) { S.recog.stop(); return; }
  const rec = new Recognition();
  rec.lang = "en-US";
  rec.interimResults = true;
  rec.onresult = (e) => { $("#askIn").value = [...e.results].map((x) => x[0].transcript).join(" "); };
  rec.onend = () => {
    S.recog = null;
    $("#askMic").classList.remove("on");
    const text = $("#askIn").value;
    $("#askIn").value = "";
    send(text);
  };
  rec.onerror = () => {};
  S.recog = rec;
  $("#askMic").classList.add("on");
  rec.start();
}

// ---------- buildings ----------
async function loadBuildings() {
  try {
    const r = await api("/api/hospital/buildings");
    S.buildings = asList(r, "buildings");
    S.current = r?.current?.id ?? r?.current;
  } catch { S.buildings = []; }
  const cur = S.buildings.find((b) => b.id === S.current);
  $("#bname").textContent = cur?.name || S.map?.building?.name || "Building";
  renderMenu();
}

function renderMenu() {
  const item = (b) => `<button type="button" role="menuitemradio" aria-checked="${b.id === S.current}" class="mi${b.id === S.current ? " on" : ""}" data-building="${esc(b.id)}">
      <span class="ms">${b.id === S.current ? "check" : ""}</span><span class="mi-txt"><b>${esc(b.name || b.id)}</b><small>${b.source === "imported" ? "Uploaded" : "Demo"} · ${b.floors} floor${b.floors === 1 ? "" : "s"}</small></span></button>`;
  $("#bmenu").innerHTML = S.buildings.map(item).join("")
    + `<hr><button type="button" role="menuitem" class="mi" data-upload><span class="ms">upload</span><span class="mi-txt"><b>Upload floor plan</b></span></button>`;
}

function toggleMenu(open = $("#bmenu").hidden) {
  $("#bmenu").hidden = !open;
  $("#bchip").setAttribute("aria-expanded", String(open));
}

async function selectBuilding(id) {
  await api("/api/hospital/buildings/select", { method: "POST", body: { building_id: id } });
  await reloadMap();
}

async function reloadMap() {
  S.map = await api("/api/hospital/map");
  S.inUse = {}; S.inUseSig = "";
  S.plan.setMap(S.map);
  S.plan.fit(undefined, 0);
  clearSearch();
  await Promise.all([loadBuildings(), pollTracking().catch(() => {})]);
}

// ---------- upload ----------
function floorName(i) { return i === 0 ? "Ground floor" : `Floor ${i}`; }

function renderStaged() {
  $("#staged").innerHTML = S.files.map((f, i) => `<li><img src="${f.url}" alt=""><span><b>${floorName(i)}</b><small>${esc(f.file.name)}</small></span>
    <button type="button" class="ibtn" data-unstage="${i}" aria-label="Remove ${esc(f.file.name)}"><span class="ms">close</span></button></li>`).join("");
  $("#upRun").disabled = !S.files.length;
  $("#upRun").textContent = S.files.length > 1 ? `Import ${S.files.length} floors` : "Import";
}

function stage(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (files.length && !images.length) return upError("Please choose image files (PNG or JPG).");
  S.files.push(...images.map((file) => ({ file, url: URL.createObjectURL(file) })));
  upError("");
  renderStaged();
}

function upError(msg) { $("#upErr").textContent = msg; $("#upErr").hidden = !msg; }

function upView(view) {
  $("#upStep").hidden = view !== "pick";
  $("#upBusy").hidden = view !== "busy";
  $("#upScale").hidden = view !== "scale";
  $("#upFoot").hidden = view !== "pick";
}

function openUpload() {
  toggleMenu(false);
  S.files.forEach((f) => URL.revokeObjectURL(f.url));
  S.files = []; S.imported = null;
  $("#upName").value = "";
  upView("pick"); upError(""); renderStaged();
  $("#upload").showModal();
}

async function runUpload() {
  if (!S.files.length) return;
  const form = new FormData();
  S.files.forEach((f) => form.append("files", f.file, f.file.name));
  const name = $("#upName").value.trim();
  if (name) form.append("name", name);
  upError(""); upView("busy");
  try {
    const res = await fetch("/api/import", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Server error ${res.status}`);
    S.imported = body;
    await selectBuilding(body.id);
  } catch (e) {
    upView("pick");
    upError(S.imported ? `The plan was imported but could not be opened: ${e.message}` : `Could not read the plans: ${e.message}`);
    return;
  }
  if (S.imported.size_guessed) return showScale(S.imported);
  finishUpload(S.imported);
}

function finishUpload(s) {
  $("#upload").close();
  const floors = s.floors?.length ?? 0;
  toast(`${s.name} · ${s.rooms ?? 0} rooms on ${floors} floor${floors === 1 ? "" : "s"}`);
}

function showScale(s) {
  const [w, d] = s.size_m || [];
  $("#upScale").innerHTML = `<div class="ok"><span class="ms">check_circle</span><div><b>${esc(s.name)} is on the map.</b>
      <p>It was read from a photo, so its size is estimated at ${esc(w)} × ${esc(d)} m. Enter the real width to make distances exact.</p></div></div>
    <label class="field"><span>Building width in metres</span><input id="scW" type="number" min="1" step="0.1" inputmode="decimal" value="${esc(w ?? "")}"></label>
    <footer class="dlg-foot"><button type="button" class="tbtn" id="scSkip">Keep estimate</button><button type="button" class="pbtn" id="scApply">Apply width</button></footer>`;
  upView("scale");
  $("#scW").focus();
}

async function applyScale() {
  const s = S.imported, w = parseFloat(String($("#scW").value).replace(",", "."));
  if (!(w > 0)) return upError("Please enter a width in metres.");
  upError(""); $("#scApply").disabled = true;
  try {
    const summary = await api(`/api/import/${encodeURIComponent(s.id)}/scale`, { method: "POST", body: { width_m: w } });
    await selectBuilding(s.id);
    finishUpload(summary);
  } catch (e) {
    upError(`Could not resize: ${e.message}`);
    $("#scApply").disabled = false;
  }
}

// ---------- wiring ----------
function wire() {
  const q = $("#q");
  q.addEventListener("input", () => { S.cursor = -1; preview(q.value); });
  q.addEventListener("focus", renderSuggest);
  q.addEventListener("blur", () => setTimeout(renderSuggest, 150));
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!S.suggest.length) return;
      e.preventDefault();
      const n = S.suggest.length;
      S.cursor = e.key === "ArrowDown" ? (S.cursor + 1 >= n ? -1 : S.cursor + 1) : (S.cursor < 0 ? n - 1 : S.cursor - 1);
      renderSuggest();
    } else if (e.key === "Escape") { q.blur(); }
  });
  $("#searchForm").addEventListener("submit", (e) => {
    e.preventDefault();
    if (S.cursor >= 0) return pickSuggest(S.cursor);
    // a sentence the search can't match ("how do I get to…", "lift A is broken") goes to the assistant
    if (!search(q.value).items.length && q.value.trim().split(/\s+/).length >= 3) return openAsk(q.value);
    commit(q.value);
  });
  $("#clear").addEventListener("click", () => { clearSearch(); q.focus(); });
  $("#suggest").addEventListener("pointerdown", (e) => e.preventDefault());
  $("#suggest").addEventListener("click", (e) => { const b = e.target.closest("[data-sg]"); if (b) pickSuggest(+b.dataset.sg); });
  $("#chips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-chip]");
    if (!b) return;
    if (b.classList.contains("on")) clearSearch(); else commit(typeInfo(b.dataset.chip).many);
  });
  $("#results").addEventListener("click", (e) => {
    const ex = e.target.closest("[data-example]");
    if (ex) return commit(ex.dataset.example);
    const row = e.target.closest("[data-item]");
    if (row) select(row.dataset.item);
  });
  $("#results").addEventListener("pointerover", (e) => setHot(e.target.closest("[data-item]")?.dataset.item || null));
  $("#results").addEventListener("pointerleave", () => setHot(null));
  $("#place").addEventListener("click", (e) => {
    if (e.target.closest("#placeClose")) deselect();
    else if (e.target.closest("#placeDir")) { const to = S.selItem; deselect(); openDirections(to); }
  });
  $("#dirOpen").addEventListener("click", () => openDirections());
  $("#askOpen").addEventListener("click", () => openAsk());
  $("#askClose").addEventListener("click", closeAsk);
  $("#askForm").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#askIn").value; $("#askIn").value = ""; send(t); });
  $("#askMic").addEventListener("click", listen);
  $("#askLog").addEventListener("click", (e) => {
    const ex = e.target.closest("[data-ask]");
    if (ex) return send(ex.dataset.ask);
    const steps = e.target.closest("[data-route]");
    if (steps) showSteps(S.ask.log[+steps.dataset.route].route);
  });
  $("#dirClose").addEventListener("click", closeDirections);
  for (const [sel, field] of [["#dFrom", "from"], ["#dTo", "to"]]) {
    const input = $(sel);
    input.addEventListener("focus", () => { S.dir.field = field; dirSuggest(field); });
    input.addEventListener("input", () => { S.dir[field] = null; dirSuggest(field); plotRoute(); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && S.dir.sug[0]) { e.preventDefault(); setEnd(S.dir.sug[0], field); } });
  }
  $("#dSug").addEventListener("pointerdown", (e) => e.preventDefault());
  $("#dSug").addEventListener("click", (e) => { const b = e.target.closest("[data-dsg]"); if (b) setEnd(S.dir.sug[+b.dataset.dsg]); });
  $("#dSwap").addEventListener("click", () => {
    const d = S.dir;
    [d.from, d.to] = [d.to, d.from];
    [$("#dFrom").value, $("#dTo").value] = [$("#dTo").value, $("#dFrom").value];
    plotRoute();
  });
  $("#dStep").addEventListener("change", (e) => { S.dirStepFree = S.dir.stepFree = e.target.checked; plotRoute(); });
  $("#dOut").addEventListener("click", (e) => {
    const step = e.target.closest("[data-step]");
    if (!step) return;
    S.plan.setFloor(step.dataset.floor);
    zoomTo({ x: +step.dataset.x, y: +step.dataset.y }, 14);
  });
  S.pins.addEventListener("click", (e) => { const p = e.target.closest("[data-pin]"); if (p) select(p.dataset.pin, false); });
  S.plan.onTap((hit) => {
    if (S.dir) { // tapping a room or item fills the start, then the destination
      const item = (hit.kind === "asset" || hit.kind === "space") && itemFor(hit.id);
      if (item) setEnd(item, S.dir.from && document.activeElement !== $("#dFrom") ? "to" : "from");
      return;
    }
    if (hit.kind === "asset" || hit.kind === "space") {
      const item = itemFor(hit.id);
      if (item) return select(item.id, false);
    }
    if (S.sel) deselect();
  });
  S.plan.onFloor(() => { renderPins(); markFloors(); });

  $("#bchip").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
  $("#bmenu").addEventListener("click", async (e) => {
    if (e.target.closest("[data-upload]")) return openUpload();
    const b = e.target.closest("[data-building]");
    if (!b) return;
    toggleMenu(false);
    if (b.dataset.building === S.current) return;
    $("#bname").textContent = "Loading…";
    try { await selectBuilding(b.dataset.building); } catch (err) { toast(`Could not open building (${err.message})`, "bad"); loadBuildings(); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".bmenu")) toggleMenu(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement === document.body) { e.preventDefault(); q.focus(); }
    if (e.key === "Escape" && S.dir && !$("#upload").open) { closeDirections(); return; }
    if (e.key === "Escape" && S.ask && !$("#ask").hidden && !$("#upload").open) { closeAsk(); return; }
    if (e.key === "Escape" && document.activeElement === document.body && !$("#upload").open) {
      if (!$("#bmenu").hidden) toggleMenu(false); else if (S.sel) deselect(); else if (S.committed) clearSearch();
    }
  });

  $("#uploadOpen").addEventListener("click", openUpload);
  $("#uploadForm").addEventListener("submit", (e) => { if (e.submitter?.value !== "close") e.preventDefault(); });
  $("#upCancel").addEventListener("click", () => $("#upload").close());
  $("#upFiles").addEventListener("change", (e) => { stage(e.target.files); e.target.value = ""; });
  const drop = $("#drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); stage(e.dataTransfer.files); });
  $("#upload").addEventListener("dragover", (e) => e.preventDefault());
  $("#upload").addEventListener("drop", (e) => { e.preventDefault(); if ($("#upStep").hidden === false) stage(e.dataTransfer.files); });
  $("#upRun").addEventListener("click", runUpload);
  $("#upload").addEventListener("click", (e) => {
    const un = e.target.closest("[data-unstage]");
    if (un) { const [f] = S.files.splice(+un.dataset.unstage, 1); URL.revokeObjectURL(f.url); renderStaged(); }
    else if (e.target.closest("#scApply")) applyScale();
    else if (e.target.closest("#scSkip")) finishUpload(S.imported);
  });
  $("#upload").addEventListener("cancel", (e) => { if (!$("#upBusy").hidden) e.preventDefault(); });
  matchMedia(WIDE).addEventListener("change", () => renderPlace());
}

async function boot() {
  try {
    S.map = await api("/api/hospital/map");
  } catch (e) {
    $("#plan").innerHTML = `<div class="boot-err"><span class="ms">cloud_off</span>Could not load the building (${esc(e.message)}).</div>`;
    return;
  }
  S.res = { items: [], types: [] };
  S.plan = createPlan($("#plan"), S.map, { pad: 48 });
  S.world = $("#plan .plan-world");
  S.pins = Object.assign(document.createElement("div"), { className: "pins" });
  $("#plan").appendChild(S.pins);
  new MutationObserver(placePins).observe(S.world, { attributes: true, attributeFilter: ["transform"] });
  renderChips();
  wire();
  loadBuildings();
  poll(pollTracking, 3000);
}

boot();
