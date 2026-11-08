"use strict";
/* Indoor Maps: a phone-style indoor maps app over the hackathon factories.
   Map engine (SVG world + screen-space overlay), bottom sheet, search, place cards, directions,
   simulated turn-by-turn navigation and the "Ask" assistant (Claude via /api/chat). */

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const NAV_SPEED_M_S = 6.0;      // simulated walking speed in navigation (about 5x real time)
const NAV_ZOOM = 11;            // px per metre while navigating
const LIFT_PAUSE_S = 1.8;
const MANEUVER_ICON = {
  depart: "trip_origin", left: "turn_left", right: "turn_right", straight: "straight",
  lift_up: "elevator", lift_down: "elevator", stairs_up: "stairs", stairs_down: "stairs", arrive: "location_on", waypoint: "flag",
};
const MODES = [
  { id: "walk", icon: "directions_walk", label: "Walk" },
  { id: "accessible", icon: "accessible", label: "Step-free" },
  { id: "cart", icon: "pallet", label: "Pallet" },
];
const QUICK = [
  { cat: "machines", label: "Machines", icon: "precision_manufacturing", color: "#12806e" },
  { cat: "toilets", label: "Toilets", icon: "wc", color: "#7b8794" },
  { cat: "food", label: "Kitchens", icon: "restaurant", color: "#e8710a" },
  { cat: "exits", label: "Exits", icon: "door_open", color: "#188038" },
];
const FACILITY = {
  toilet: ["wc", "facility"], lift: ["elevator", "facility"], stairwell: ["stairs", "facility"],
  kitchen: ["restaurant", "food"], canteen: ["restaurant", "food"], meeting_room: ["groups", "work"],
  locker_room: ["checkroom", "facility"], server_room: ["dns", "work"],
};

const S = {
  session: null, buildingId: null, map: null, floor: null, cam: { x: 0, y: 0, z: 4 },
  location: null, selected: null, place: null, results: [], resultsTitle: "", chip: null,
  route: null, routes: {}, mode: "walk", dirFrom: null, dirTo: null, dirStops: [], stepsOpen: false, picking: false,
  screen: "explore", chat: [], showMachines: true, importing: null, sound: false, nav: null, sheetH: 0, sheetState: "peek",
};
let spaceById = {}, assetById = {}, floorById = {}, overlayItems = [];

// ---------- utilities ----------

const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const W = () => $("screen").clientWidth;
const H = () => $("screen").clientHeight;
const metres = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);
const clock = (date) => date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}
const sapi = (path, options) => api(`/api/sessions/${S.session}${path}`, options);

let toastTimer = null;
function toast(message) {
  const el = $("toast");
  el.textContent = message; el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2600);
}

function speak(text) {
  if (!S.sound || !window.speechSynthesis) return;
  speechSynthesis.cancel();
  speechSynthesis.speak(new SpeechSynthesisUtterance(text.replace(/[*#]/g, "")));
}

function local(event) {
  const rect = $("screen").getBoundingClientRect();
  const scale = rect.width / W();
  return { x: (event.clientX - rect.left) / scale, y: (event.clientY - rect.top) / scale };
}

function pointOf(id) {
  if (assetById[id]) { const a = assetById[id]; return { x: a.position[0], y: a.position[1], floor_id: a.floor_id }; }
  const s = spaceById[id];
  return s ? { x: s.anchor[0], y: s.anchor[1], floor_id: s.floor_id } : null;
}
const nameOf = (id) => (assetById[id] || spaceById[id] || {}).name || "";

// ---------- camera ----------

function project(x, y) { return [W() / 2 + (x - S.cam.x) * S.cam.z, H() / 2 - (y - S.cam.y) * S.cam.z]; }
function unproject(sx, sy) { return [S.cam.x + (sx - W() / 2) / S.cam.z, S.cam.y - (sy - H() / 2) / S.cam.z]; }

let frameQueued = false;
function applyCam() {
  const { x, y, z } = S.cam;
  $("cam").setAttribute("transform", `translate(${W() / 2 - x * z} ${H() / 2 + y * z}) scale(${z})`);
  if (!frameQueued) { frameQueued = true; requestAnimationFrame(() => { frameQueued = false; positionOverlay(); drawRoute(); }); }
}

let flight = null;
function flyTo(target, ms = 450) {
  cancelAnimationFrame(flight);
  const from = { ...S.cam }, start = performance.now();
  const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
  const step = (now) => {
    const t = Math.min(1, (now - start) / ms), k = ease(t);
    // zoom geometrically so the motion feels even
    S.cam.z = from.z * Math.pow(target.z / from.z, k);
    S.cam.x = from.x + (target.x - from.x) * k;
    S.cam.y = from.y + (target.y - from.y) * k;
    applyCam();
    if (t < 1) flight = requestAnimationFrame(step);
  };
  flight = requestAnimationFrame(step);
}

function visibleBand() {
  const top = { directions: 196, navigate: 190 }[S.screen] ?? 170;
  return { top, bottom: H() - S.sheetH - 12 };
}

function camFor(minX, minY, maxX, maxY, maxZoom = 30) {
  const band = visibleBand();
  const w = Math.max(maxX - minX, 4), h = Math.max(maxY - minY, 4);
  const z = Math.max(1.2, Math.min(maxZoom, (W() - 48) / w, (band.bottom - band.top - 24) / h));
  const vy = (band.top + band.bottom) / 2;
  return { x: (minX + maxX) / 2, y: (minY + maxY) / 2 + (vy - H() / 2) / z, z };
}

function camAt(x, y, z) {
  const band = visibleBand();
  const vy = (band.top + band.bottom) / 2;
  return { x, y: y + (vy - H() / 2) / z, z };
}

function floorBounds(floorId) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const s of S.map.spaces) {
    if (floorId && s.floor_id !== floorId) continue;
    for (const [x, y] of s.polygon) { minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); }
  }
  return [minX, minY, maxX, maxY];
}

// ---------- world (SVG in metres) ----------

function closedSpaces() {
  const closed = new Set(S.map.closed);
  for (const c of S.map.connectors) if (closed.has(c.id)) c.space_ids.forEach((id) => closed.add(id));
  return closed;
}

function svg(tag, attrs, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  parent.appendChild(node);
  return node;
}
const pts = (polygon) => polygon.map(([x, y]) => `${x},${-y}`).join(" ");

function renderWorld() {
  const g = $("cam");
  g.innerHTML = "";
  const spaces = S.map.spaces.filter((s) => s.floor_id === S.floor);
  const closed = closedSpaces();
  const selectedSpace = S.selected && (spaceById[S.selected] ? S.selected : null);
  for (const s of spaces) svg("polygon", { points: pts(s.polygon), class: "outline" }, g);
  for (const s of spaces) {
    const cls = ["sp", s.type, closed.has(s.id) ? "closed" : "", s.id === selectedSpace ? "sel" : ""].join(" ");
    svg("polygon", { points: pts(s.polygon), class: cls, "data-id": s.id }, g);
  }
  for (const a of S.map.assets) {
    if (a.floor_id !== S.floor) continue;
    const f = a.footprint || {}, w = f.width_m || 0.6, d = f.depth_m || 0.6, r = f.rotation_deg || 0;
    const [x, y] = [a.position[0], -a.position[1]];
    svg("rect", {
      x: x - w / 2, y: y - d / 2, width: w, height: d, rx: Math.min(w, d) * 0.15,
      class: `as ${a.category}${a.id === S.selected ? " sel" : ""}`, transform: `rotate(${-r} ${x} ${y})`, "data-id": a.id,
    }, g);
  }
}

// ---------- overlay (screen-space labels, POIs, pins) ----------

function addOverlay(el, x, y, opts = {}) {
  $("overlay").appendChild(el);
  overlayItems.push({ el, x, y, ...opts });
}

function pinSvg(color = "#ea4335", inner = "#b31412") {
  return `<svg class="pin" viewBox="0 0 27 43"><path d="M13.5 0C6 0 0 6 0 13.4 0 23.5 13.5 43 13.5 43S27 23.5 27 13.4C27 6 21 0 13.5 0Z" fill="${color}"/><path d="M13.5 1.2c6.7 0 12.3 5.4 12.3 12.2 0 8.7-10.6 25-12.3 27.5C11.8 38.4 1.2 22.1 1.2 13.4 1.2 6.6 6.8 1.2 13.5 1.2Z" fill="none" stroke="${inner}" stroke-opacity=".35"/><circle cx="13.5" cy="13.5" r="4.6" fill="${inner}"/></svg>`;
}

function buildOverlay() {
  $("overlay").innerHTML = "";
  overlayItems = [];
  const resultIds = new Set(S.results.map((r) => r.id));
  for (const s of S.map.spaces) {
    if (s.floor_id !== S.floor || s.type === "corridor") continue;
    const xs = s.polygon.map((p) => p[0]), ys = s.polygon.map((p) => p[1]);
    const size = Math.min(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys) * 1);
    const width = Math.max(...xs) - Math.min(...xs);
    const facility = FACILITY[s.room_type] || (s.type === "lift" ? FACILITY.lift : s.type === "stair" ? FACILITY.stairwell : null);
    if (resultIds.has(s.id)) continue;
    if (facility) {
      const el = document.createElement("div");
      el.className = `poi ${facility[1]}`; el.dataset.id = s.id;
      el.innerHTML = `<span class="dot"><span class="ms">${facility[0]}</span></span><span class="name">${esc(s.ref && s.type !== "lift" && s.type !== "stair" ? s.name : s.name)}</span>`;
      addOverlay(el, s.anchor[0], s.anchor[1], { kind: "poi", minZ: 2.2, nameZ: 5.5, prio: 2, chars: s.name.length });
      continue;
    }
    const el = document.createElement("div");
    el.className = `lbl${s.type === "hall" ? " hall" : ""}`;
    el.textContent = s.type === "hall" ? s.name : (s.ref || s.name);
    const chars = el.textContent.length * (s.type === "hall" ? 7 : 6.2) + 8;
    addOverlay(el, s.anchor[0], s.anchor[1], { kind: "label", fit: width, need: chars, size, prio: s.type === "hall" ? 2.5 : 4 });
  }
  if (S.showMachines) {
    for (const a of S.map.assets) {
      if (a.floor_id !== S.floor || a.category !== "machine" || resultIds.has(a.id) || a.id === S.selected) continue;
      const el = document.createElement("div");
      el.className = "poi machine"; el.dataset.id = a.id;
      el.innerHTML = `<span class="dot"><span class="ms">precision_manufacturing</span></span><span class="name">${esc(a.name)}</span>`;
      addOverlay(el, a.position[0], a.position[1], { kind: "poi", minZ: 5, nameZ: 13, prio: 3, chars: a.name.length });
    }
  }
  for (const r of S.results) {
    if (r.floor_id !== S.floor || r.id === S.selected) continue;
    const el = document.createElement("div");
    el.className = "poi result"; el.dataset.id = r.id;
    el.innerHTML = `<span class="dot"><span class="ms">${esc(r.icon || "place")}</span></span><span class="name">${esc(r.name)}</span>`;
    addOverlay(el, r.x, r.y, { kind: "poi", minZ: 0, nameZ: S.screen === "place" ? Infinity : 4, big: true, prio: 1, chars: r.name.length });
  }
  const route = S.route && ["directions", "navigate", "ask"].includes(S.screen) ? S.route : null;
  if (route) {
    const p = route.points;
    for (let i = 0; i + 1 < p.length; i++) {
      if (p[i].floor_id === p[i + 1].floor_id) continue;
      const up = floorById[p[i + 1].floor_id].level > floorById[p[i].floor_id].level;
      for (const [here, there, text] of [[p[i], p[i + 1], `${up ? "↑" : "↓"} ${floorById[p[i + 1].floor_id].name}`], [p[i + 1], p[i], `from ${floorById[p[i].floor_id].name}`]]) {
        if (here.floor_id !== S.floor) continue;
        const el = document.createElement("div");
        el.className = "transfer"; el.dataset.floor = there.floor_id;
        el.innerHTML = `<span class="ms">${route.instructions.some((s) => s.maneuver.startsWith("lift")) ? "elevator" : "stairs"}</span>${esc(text)}`;
        addOverlay(el, here.x, here.y, { kind: "chip" });
      }
    }
    const first = p[0], last = p[p.length - 1];
    if (first && first.floor_id === S.floor && !(S.location && route.from.id === S.location)) {
      const el = document.createElement("div"); el.className = "endpoint";
      addOverlay(el, first.x, first.y, { kind: "pin" });
    }
    if (last && last.floor_id === S.floor && route.to.id !== S.selected) {
      const el = document.createElement("div"); el.innerHTML = pinSvg();
      addOverlay(el.firstChild, last.x, last.y, { kind: "pin" });
    }
    (route.stops || []).forEach((stop, i) => {
      const at = route.instructions.filter((s) => s.maneuver === "waypoint")[i];
      const point = at ? p[Math.min(p.length - 1, nextLegStart(route, at))] : pointOf(stop.id);
      if (!point || point.floor_id !== S.floor) return;
      const el = document.createElement("div");
      el.className = "stop-marker"; el.textContent = String.fromCharCode(66 + i);
      addOverlay(el, point.x, point.y, { kind: "pin" });
    });
  }
  if (S.selected) {
    const p = pointOf(S.selected);
    if (p && p.floor_id === S.floor) {
      const el = document.createElement("div"); el.innerHTML = pinSvg();
      addOverlay(el.firstChild, p.x, p.y, { kind: "pin" });
    }
  }
  const here = S.location && S.screen !== "navigate" ? pointOf(S.location) : null;
  if (here && here.floor_id === S.floor) {
    const el = document.createElement("div"); el.className = "me";
    addOverlay(el, here.x, here.y, { kind: "pin" });
  }
  if (S.screen === "navigate" && S.nav) {
    const el = document.createElement("div");
    el.innerHTML = `<svg class="nav-arrow" viewBox="0 0 34 34"><circle cx="17" cy="17" r="15" fill="#1a73e8" stroke="#fff" stroke-width="3"/><path d="M17 8l7 16-7-4-7 4z" fill="#fff"/></svg>`;
    S.nav.arrow = el.firstChild;
    addOverlay(S.nav.arrow, S.nav.pos?.x ?? 0, S.nav.pos?.y ?? 0, { kind: "arrow" });
  }
  positionOverlay();
}

// The point where a waypoint step ends: the first point of the next step's stretch.
function nextLegStart(route, waypoint) {
  const i = route.instructions.indexOf(waypoint);
  const next = route.instructions[i + 1];
  return next ? next.index : route.points.length - 1;
}

function positionOverlay() {
  const z = S.cam.z, w = W(), h = H();
  const placed = [];
  const hits = (box) => placed.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]);
  const candidates = [];
  for (const item of overlayItems) {
    const [sx, sy] = project(item.x, item.y);
    item.sx = sx; item.sy = sy;
    const off = sx < -80 || sy < -60 || sx > w + 80 || sy > h + 60;
    if (item.prio == null) {  // pins, the blue dot, transfer chips: always drawn, and they claim space
      item.el.style.display = off ? "none" : "";
      if (!off) placed.push([sx - 14, sy - 40, sx + 14, sy + 8]);
      continue;
    }
    let show = !off;
    if (item.kind === "label") show = show && item.fit * z >= item.need && item.size * z >= 14;
    if (item.kind === "poi") show = show && z >= item.minZ;
    if (show) candidates.push(item); else item.el.style.display = "none";
  }
  candidates.sort((a, b) => a.prio - b.prio);
  for (const item of candidates) {
    const { sx, sy } = item;
    let visible = false, withName = false;
    if (item.kind === "label") {
      const box = [sx - item.need / 2, sy - 8, sx + item.need / 2, sy + 8];
      if (!hits(box)) { placed.push(box); visible = true; }
    } else {
      const r = item.big ? 11 : 10;
      const dot = [sx - r - 2, sy - r - 2, sx + r + 2, sy + r + 2];
      const named = [dot[0], dot[1], sx + r + 6 + item.chars * 6.4, dot[3]];
      if (z >= item.nameZ && !hits(named)) { placed.push(named); visible = withName = true; }
      else if (!hits(dot)) { placed.push(dot); visible = true; }
    }
    item.el.style.display = visible ? "" : "none";
    if (!visible) continue;
    const name = item.el.querySelector?.(".name");
    if (name) name.style.display = withName ? "" : "none";
    if (item.kind === "label") item.el.style.transform = `translate(${sx}px, ${sy}px) translate(-50%, -50%)`;
    else item.el.style.transform = `translate(${sx - (item.big ? 11 : 10)}px, ${sy - (item.big ? 11 : 10)}px)`;
  }
  for (const item of overlayItems) {
    if (item.prio != null || item.el.style.display === "none") continue;
    const { sx, sy } = item;
    if (item.kind === "chip") item.el.style.transform = `translate(${sx}px, ${sy}px) translate(-50%, -50%) translateY(-28px)`;
    else if (item.kind === "arrow") item.el.style.transform = `translate(${sx}px, ${sy}px) rotate(${S.nav?.heading ?? 0}deg)`;
    else item.el.style.transform = `translate(${sx}px, ${sy}px)`;
  }
}

// ---------- route drawing (screen space) ----------

function drawRoute() {
  const layer = $("routeLayer");
  layer.innerHTML = "";
  const route = S.route && ["directions", "navigate", "ask"].includes(S.screen) ? S.route : null;
  if (!route) return;
  const segments = [];
  for (const p of route.points) {
    const last = segments[segments.length - 1];
    if (last && last.floor_id === p.floor_id) last.pts.push(p); else segments.push({ floor_id: p.floor_id, pts: [p] });
  }
  for (const seg of segments) {
    if (seg.floor_id !== S.floor || seg.pts.length < 2) continue;
    const line = seg.pts.map((p) => project(p.x, p.y).join(",")).join(" ");
    if (route.mode === "cart") {
      svg("polyline", { points: line, fill: "none", stroke: "#1967d2", "stroke-width": 9, "stroke-linecap": "round", "stroke-linejoin": "round" }, layer);
      svg("polyline", { points: line, fill: "none", stroke: "#4285f4", "stroke-width": 6, "stroke-linecap": "round", "stroke-linejoin": "round" }, layer);
    } else {
      svg("polyline", { points: line, fill: "none", stroke: "#1a73e8", "stroke-width": 7, "stroke-linecap": "round", "stroke-linejoin": "round", "stroke-dasharray": "0 11" }, layer);
    }
  }
}

// ---------- floors ----------

function setFloor(floorId) {
  if (!floorId || floorId === S.floor) return;
  S.floor = floorId;
  renderWorld(); buildOverlay(); drawRoute(); renderFloors();
}

function renderFloors() {
  const routeFloors = new Set((S.route && ["directions", "navigate", "ask"].includes(S.screen) ? S.route.points : []).map((p) => p.floor_id));
  const hereFloor = S.location ? pointOf(S.location)?.floor_id : null;
  $("floors").innerHTML = [...S.map.floors].reverse().map((f) =>
    `<button data-floor="${f.id}" class="${f.id === S.floor ? "on" : ""} ${routeFloors.has(f.id) ? "route" : ""} ${f.id === hereFloor ? "here" : ""}" title="${esc(f.name)}">${esc(f.label)}</button>`).join("");
}

// ---------- gestures ----------

const pointers = new Map();
let gesture = null;

function initGestures() {
  const map = $("map");
  map.addEventListener("pointerdown", (e) => {
    map.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, local(e));
    cancelAnimationFrame(flight);
    if (S.nav) S.nav.follow = false;
    if (pointers.size === 1) gesture = { start: local(e), cam: { ...S.cam }, moved: false, client: { x: e.clientX, y: e.clientY } };
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      gesture = { pinch: true, d0: Math.hypot(a.x - b.x, a.y - b.y), cam: { ...S.cam }, world: unproject(mid.x, mid.y), moved: true };
    }
    map.classList.add("dragging");
  });
  map.addEventListener("pointermove", (e) => {
    if (!pointers.has(e.pointerId) || !gesture) return;
    pointers.set(e.pointerId, local(e));
    if (gesture.pinch && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const z = clampZ(gesture.cam.z * Math.hypot(a.x - b.x, a.y - b.y) / gesture.d0);
      S.cam = { z, x: gesture.world[0] - (mid.x - W() / 2) / z, y: gesture.world[1] + (mid.y - H() / 2) / z };
      applyCam();
    } else if (!gesture.pinch) {
      const p = local(e), dx = p.x - gesture.start.x, dy = p.y - gesture.start.y;
      if (Math.hypot(dx, dy) > 5) gesture.moved = true;
      if (gesture.moved) { S.cam = { ...S.cam, x: gesture.cam.x - dx / S.cam.z, y: gesture.cam.y + dy / S.cam.z }; applyCam(); }
    }
  });
  const end = (e) => {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    if (gesture && !gesture.moved && !gesture.pinch && pointers.size === 0) tap(e.clientX, e.clientY);
    if (pointers.size === 0) { gesture = null; map.classList.remove("dragging"); }
  };
  map.addEventListener("pointerup", end);
  map.addEventListener("pointercancel", end);
  map.addEventListener("wheel", (e) => {
    e.preventDefault();
    cancelAnimationFrame(flight);
    const p = local(e);
    const world = unproject(p.x, p.y);
    const z = clampZ(S.cam.z * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018)));
    S.cam = { z, x: world[0] - (p.x - W() / 2) / z, y: world[1] + (p.y - H() / 2) / z };
    applyCam();
  }, { passive: false });
  map.addEventListener("dblclick", (e) => {
    const p = local(e), world = unproject(p.x, p.y);
    flyTo({ x: world[0], y: world[1], z: clampZ(S.cam.z * 2) }, 300);
  });
}
const clampZ = (z) => Math.max(0.8, Math.min(60, z));

function tap(clientX, clientY) {
  for (const el of document.elementsFromPoint(clientX, clientY)) {
    if (el.dataset?.floor) { setFloor(el.dataset.floor); return; }
    if (el.closest?.(".poi")?.dataset.id) { selectPlace(el.closest(".poi").dataset.id); return; }
    if (el.dataset?.id) {
      if (S.screen === "navigate") return;
      const space = spaceById[el.dataset.id];
      if (space && space.type === "corridor") break;
      selectPlace(el.dataset.id); return;
    }
  }
  if (["place", "results"].includes(S.screen)) goExplore();
}

// ---------- bottom sheet ----------

const SHEET = { import: [0.86, 0.86, 0.9], explore: [210, 0.5, 0.9], results: [210, 0.5, 0.9], place: [330, 0.62, 0.9], directions: [236, 0.5, 0.9], navigate: [118, 118, 118], ask: [260, 0.55, 0.9] };

function sheetHeights() {
  const [peek, half, full] = SHEET[S.screen] || SHEET.explore;
  const px = (v) => (v <= 1 ? Math.round(H() * v) : v);
  return { peek: px(peek), half: px(half), full: px(full) };
}

function setSheet(state, animate = true) {
  const heights = sheetHeights();
  S.sheetState = state;
  S.sheetH = heights[state] ?? heights.peek;
  const sheet = $("sheet");
  sheet.classList.toggle("dragging", !animate);
  sheet.style.height = `${S.sheetH}px`;
  layoutFloating();
}

function layoutFloating() {
  const loc = $("locBtn");
  loc.style.bottom = `${S.sheetH + 16}px`;
  const tooTall = S.sheetH > H() * 0.7;
  const fabs = $("fabs");
  const crowded = H() - S.sheetH - 16 - 56 < fabs.offsetTop + fabs.offsetHeight + 8;  // would cover the floor picker
  loc.style.opacity = tooTall || crowded || S.screen === "navigate" ? 0 : 1;
  loc.style.pointerEvents = tooTall || crowded || S.screen === "navigate" ? "none" : "";
  $("fabs").style.opacity = tooTall || S.screen === "navigate" ? 0 : 1;
  $("fabs").style.pointerEvents = tooTall || S.screen === "navigate" ? "none" : "";
  $("fabs").style.top = S.screen === "directions" ? "206px" : "172px";
}

function initSheet() {
  const handle = $("handle");
  let drag = null;
  handle.addEventListener("pointerdown", (e) => { handle.setPointerCapture(e.pointerId); drag = { y: local(e).y, h: S.sheetH }; $("sheet").classList.add("dragging"); });
  handle.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const heights = sheetHeights();
    S.sheetH = Math.max(heights.peek * 0.6, Math.min(heights.full, drag.h - (local(e).y - drag.y)));
    $("sheet").style.height = `${S.sheetH}px`;
    layoutFloating();
  });
  handle.addEventListener("pointerup", () => {
    if (!drag) return;
    const moved = Math.abs(S.sheetH - drag.h) > 6;
    drag = null;
    const heights = sheetHeights();
    if (!moved) { setSheet(S.sheetState === "peek" ? "half" : S.sheetState === "half" ? "full" : "peek"); return; }
    const best = Object.entries(heights).sort((a, b) => Math.abs(a[1] - S.sheetH) - Math.abs(b[1] - S.sheetH))[0][0];
    setSheet(best);
  });
}

// ---------- top area ----------

function renderTop() {
  const top = $("top");
  if (S.screen === "directions") {
    const fromIsMe = S.dirFrom === S.location;
    top.innerHTML = `<div class="dir-card">
      <div class="dir-top">
        <button class="icon-btn" data-act="back"><span class="ms">arrow_back</span></button>
        <div class="dir-fields">
          <div class="field from ${fromIsMe ? "me-field" : ""}"><span class="field-text">${fromIsMe ? "Your location" : esc(nameOf(S.dirFrom))}</span></div>
          ${[...S.dirStops, S.dirTo].map((id, i) => `<div class="field ${i === S.dirStops.length ? "to" : "stop"}" data-letter="${String.fromCharCode(66 + i)}">
            <span class="field-text">${esc(nameOf(id))}</span>
            ${S.dirStops.length ? `<button class="field-x" data-act="remove-stop" data-i="${i}" title="Remove stop"><span class="ms">close</span></button>` : ""}</div>`).join("")}
          ${S.dirStops.length < 4 ? `<button class="add-stop" data-act="add-stop"><span class="ms">add</span>Add stop</button>` : ""}
        </div>
        ${S.dirStops.length ? "" : `<button class="icon-btn" data-act="swap"><span class="ms">swap_vert</span></button>`}
      </div>
      <div class="modes">${MODES.map((m) => {
        const r = S.routes[m.id];
        const label = r === undefined ? "…" : r ? `${r.minutes} min` : "—";
        return `<button class="mode ${m.id === S.mode ? "on" : ""} ${r === null ? "off" : ""}" data-mode="${m.id}" title="${m.label}"><span class="ms">${m.icon}</span>${label}</button>`;
      }).join("")}</div>
    </div>`;
    return;
  }
  if (S.screen === "navigate") {
    top.innerHTML = navBannerHtml();
    return;
  }
  const back = ["place", "results", "ask", "import"].includes(S.screen);
  const text = S.screen === "place" ? nameOf(S.selected) : S.screen === "results" ? S.resultsTitle : S.screen === "ask" ? (S.chat[S.chat.length - 1]?.q || "") : S.screen === "import" ? "Add a building" : "";
  const chips = ["explore", "results"].includes(S.screen) ? `<div class="chips">${S.map.categories.map((c) =>
    `<button class="chip ${S.chip === c.id ? "on" : ""}" data-cat="${c.id}"><span class="ms">${c.icon}</span>${esc(c.label)}</button>`).join("")}</div>` : "";
  top.innerHTML = `<div class="searchbar">
      ${back ? `<button class="icon-btn" data-act="back"><span class="ms">arrow_back</span></button>` : `<span class="ms fill brand">location_on</span>`}
      <div class="ph ${text ? "filled" : ""}" data-act="search">${esc(text || "Search here")}</div>
      ${back ? `<button class="icon-btn" data-act="close"><span class="ms">close</span></button>` : `<button class="icon-btn" data-act="mic"><span class="ms">mic</span></button><span class="avatar">T</span>`}
    </div>${chips}`;
}

function navBannerHtml() {
  const n = S.nav, route = S.route;
  if (!n) return "";
  if (n.done) {
    return `<div class="nav-banner"><span class="ms big">location_on</span><div><div class="dist">You have arrived</div><div class="instr">${esc(route.to.name)}</div></div></div>`;
  }
  const current = route.instructions[n.current] || route.instructions[1] || route.instructions[0];
  const next = route.instructions[n.current + 1];
  const toGo = n.toManeuver;
  const dist = current.maneuver.startsWith("lift") || current.maneuver.startsWith("stairs") ? "Now" : toGo != null ? metres(Math.max(0, toGo)) : "";
  return `<div class="nav-banner"><span class="ms big">${MANEUVER_ICON[current.maneuver] || "straight"}</span>
      <div><div class="dist">${esc(dist)}</div><div class="instr">${esc(current.text)}</div></div></div>
      ${next ? `<div class="nav-then">Then <span class="ms">${MANEUVER_ICON[next.maneuver] || "straight"}</span></div>` : ""}`;
}

// ---------- sheet content per screen ----------

function renderSheet() {
  const body = $("sheetBody");
  const B = S.map.building;
  if (S.screen === "explore") {
    const machines = S.map.assets.filter((a) => a.category === "machine").length;
    body.innerHTML = `<div class="pad"><div class="h1">${esc(B.name)}</div>
        <div class="sub">Indoor map<span class="dotsep">·</span>${S.map.floors.length} floors<span class="dotsep">·</span>${machines} machines</div></div>
      <div class="quick">${QUICK.map((q) => `<button data-cat="${q.cat}"><span class="q" style="background:${q.color}"><span class="ms">${q.icon}</span></span>${q.label}</button>`).join("")}</div>
      <div class="thick-divider"></div>
      <div class="section-title">Ask this building</div>
      <div class="suggest">${suggestions().map((s) => `<button data-ask="${esc(s)}"><span class="ms">auto_awesome</span>${esc(s)}</button>`).join("")}</div>
      <div class="thick-divider"></div>
      <div class="list-item" data-act="import"><span class="ico" style="background:var(--blue-tonal);color:var(--blue)"><span class="ms">add_business</span></span>
        <div><div class="t">Add a building</div><div class="s">From floor plans in your photos</div></div></div>`;
    return;
  }
  if (S.screen === "import") { body.innerHTML = importHtml(); return; }
  if (S.screen === "results") {
    body.innerHTML = `<div class="pad"><div class="h1">${esc(S.resultsTitle)}</div><div class="sub">${S.results.length} results · sorted by walking distance</div></div><div class="divider"></div>` +
      (S.results.length ? S.results.map(listItem).join("") : `<div class="pad sub">Nothing found.</div>`);
    return;
  }
  if (S.screen === "place") {
    const p = S.place;
    if (!p) { body.innerHTML = `<div class="pad sub">Loading…</div>`; return; }
    const walk = p.route ? `<div class="meta"><span class="ms">directions_walk</span>${p.route.minutes} min<span class="dotsep">·</span>${metres(p.route.walk_m)} from you</div>` : "";
    const isHere = S.location === p.id;
    body.innerHTML = `<div class="pad"><div class="h1">${esc(p.name)}</div>
        <div class="sub">${esc(p.subtitle)}<span class="dotsep">·</span>${esc(p.floor_name)}</div>${walk}
        ${p.closed ? `<div class="meta warn"><span class="ms">block</span>Closed</div>` : ""}</div>
      <div class="actions">
        <button class="pill primary" data-act="directions"><span class="ms">directions</span>Directions</button>
        <button class="pill" data-act="start"><span class="ms">navigation</span>Start</button>
        <button class="pill outline" data-act="here"><span class="ms">${isHere ? "check" : "my_location"}</span>${isHere ? "You are here" : "I'm here"}</button>
        <button class="pill outline" data-act="ask-about"><span class="ms">auto_awesome</span>Ask</button>
      </div>
      <div class="divider"></div>
      <div class="row"><span class="ms">square_foot</span><div>${p.area_m2} m²<small>${esc(p.room.name)} · ${esc(p.room.type)}</small></div></div>
      ${p.contents.length ? `<div class="row"><span class="ms">precision_manufacturing</span><div>${p.contents.map(([t, c]) => `${c} × ${esc(t)}`).join(", ")}<small>Machines and equipment here</small></div></div>` : ""}
      ${p.widest_door_m ? `<div class="row"><span class="ms">door_front</span><div>Doors up to ${p.widest_door_m} m wide<small>${p.step_free ? "Step-free access" : "Some doors have steps"}</small></div></div>` : ""}
      <div class="row"><span class="ms">layers</span><div>${esc(p.floor_name)}<small>${esc(B.name)}</small></div></div>`;
    return;
  }
  if (S.screen === "directions") {
    const r = S.route;
    if (!r) {
      body.innerHTML = `<div class="pad sub">${S.routes[S.mode] === null ? "No route for this mode with the current closures and door widths." : "Finding the best route…"}</div>`;
      return;
    }
    const via = r.instructions.find((s) => s.maneuver.startsWith("lift") || s.maneuver.startsWith("stairs"));
    const note = { walk: "Fastest route", accessible: "Step-free route", cart: "Wide doors only · no steps" }[r.mode];
    body.innerHTML = `<div class="pad"><div class="dur"><b>${r.minutes} min</b> <span>(${metres(r.walk_m)})</span></div>
        <div class="sub">${note}${S.dirStops.length ? `<span class="dotsep">·</span>${S.dirStops.length} stop${S.dirStops.length > 1 ? "s" : ""}` : ""}${via ? `<span class="dotsep">·</span>${esc(via.text.replace(/^Take the /, "via "))}` : ""}</div></div>
      <div class="actions">
        <button class="pill primary" data-act="start"><span class="ms">navigation</span>Start</button>
        <button class="pill outline" data-act="steps"><span class="ms">format_list_bulleted</span>${S.stepsOpen ? "Hide steps" : "Steps"}</button>
      </div>
      <div class="divider"></div>
      ${r.instructions.map((s) => `<div class="step ${s.maneuver === "waypoint" ? "waypoint" : ""}"><span class="ms">${MANEUVER_ICON[s.maneuver] || "straight"}</span><div class="txt"><b>${esc(s.text)}</b><small>${esc(s.detail || "")}</small></div></div>`).join("")}`;
    return;
  }
  if (S.screen === "navigate") {
    const n = S.nav, r = S.route;
    const left = n ? Math.max(0, n.total - n.s) : r.walk_m;
    const minutes = Math.max(1, Math.round((left / r.walk_m) * r.minutes || 0));
    const eta = new Date(Date.now() + minutes * 60000);
    body.innerHTML = n?.done
      ? `<div class="nav-bottom"><div class="eta grow" style="flex:1"><b>Arrived</b><span>${esc(r.to.name)}</span></div><button class="pill primary" data-act="done"><span class="ms">check</span>Done</button></div>`
      : `<div class="nav-bottom"><button class="round close" data-act="exit"><span class="ms">close</span></button>
          <div class="eta" style="flex:1;text-align:center"><b>${minutes} min</b><span>${metres(left)} · ${clock(eta)}</span></div>
          <button class="round ${S.sound ? "on" : ""}" data-act="sound"><span class="ms">${S.sound ? "volume_up" : "volume_off"}</span></button></div>`;
    return;
  }
  if (S.screen === "ask") {
    const turns = S.chat.map((t, i) => {
      const last = i === S.chat.length - 1;
      const answer = t.a == null
        ? `<div class="answer thinking"><span class="typing"><span></span><span></span><span></span></span>Looking at the plan…</div>`
        : `<div class="answer">${formatAnswer(t.a)}</div>`;
      const card = last && t.route ? `<div class="route-card"><span class="ms">directions_walk</span><div class="grow"><b>${t.route.minutes} min</b> · ${metres(t.route.walk_m)}<small>to ${esc(t.route.to.name)}</small></div>
          <button class="pill outline" data-act="preview">Steps</button><button class="pill primary" data-act="start"><span class="ms">navigation</span>Start</button></div>` : "";
      return `<div class="bubble">${esc(t.q)}</div>${answer}${card}`;
    }).join("");
    body.innerHTML = `<div class="ask-head"><span class="spark"><span class="ms">auto_awesome</span></span><b>Ask this building</b></div>${turns}
      <form class="ask-input" id="askForm"><input id="askInput" placeholder="Ask a follow-up" autocomplete="off"><button type="button" class="icon-btn" data-act="mic"><span class="ms">mic</span></button><button class="icon-btn" style="color:var(--blue)"><span class="ms fill">send</span></button></form>`;
    $("askForm").onsubmit = (e) => { e.preventDefault(); const v = $("askInput").value.trim(); if (v) askQuestion(v); };
    body.scrollTop = body.scrollHeight;
  }
}

function formatAnswer(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/^#+\s*/gm, "");
}

function listItem(r) {
  return `<div class="list-item" data-id="${r.id}"><span class="ico"><span class="ms">${esc(r.icon || "place")}</span></span>
    <div><div class="t">${esc(r.name)}</div><div class="s">${esc(r.subtitle)} · ${esc(r.floor_name)}</div></div>
    <div class="d">${r.distance_m != null ? metres(r.distance_m) : ""}</div></div>`;
}

function suggestions() {
  const machine = S.map.assets.find((a) => a.category === "machine");
  const lift = S.map.connectors.find((c) => c.kind === "lift");
  const lab = S.map.spaces.find((s) => s.room_type === "quality_lab") || S.map.spaces.find((s) => s.type === "room");
  const out = [];
  if (machine) out.push(`Where is the nearest ${machine.type.replaceAll("_", " ")}?`);
  if (lab) out.push(`I'm pushing a 1.4 m pallet. How do I get to ${lab.name}?`);
  if (lift) out.push(`The ${lift.name} is closed for maintenance. What changes?`);
  return out;
}

// ---------- screens ----------

function render() {
  renderTop(); renderSheet(); renderFloors(); renderWorld(); buildOverlay(); drawRoute(); layoutFloating();
}

function goExplore() {
  S.screen = "explore"; S.selected = null; S.place = null; S.results = []; S.chip = null; S.route = null; S.nav = null;
  setSheet("peek"); render();
}

async function showCategory(cat) {
  const c = S.map.categories.find((x) => x.id === cat);
  S.chip = cat; S.screen = "results"; S.resultsTitle = c ? c.label : cat; S.selected = null; S.route = null;
  setSheet("half"); render();
  const { results } = await sapi(`/search?category=${cat}&from_id=${encodeURIComponent(S.location || "")}`);
  S.results = results;
  render(); fitResults();
}

async function showQuery(q) {
  S.chip = null; S.screen = "results"; S.resultsTitle = q; S.selected = null; S.route = null;
  setSheet("half"); render();
  const { results } = await sapi(`/search?q=${encodeURIComponent(q)}&from_id=${encodeURIComponent(S.location || "")}`);
  S.results = results;
  render(); fitResults();
}

function fitResults() {
  if (!S.results.length) return;
  const floor = S.results[0].floor_id;
  setFloor(floor);
  const on = S.results.filter((r) => r.floor_id === floor);
  const xs = on.map((r) => r.x), ys = on.map((r) => r.y);
  flyTo(camFor(Math.min(...xs) - 4, Math.min(...ys) - 4, Math.max(...xs) + 4, Math.max(...ys) + 4, 9));
}

async function selectPlace(id) {
  if (!pointOf(id)) return;
  S.selected = id; S.place = null; S.screen = "place"; S.route = null; S.chip = null;
  const p = pointOf(id);
  setSheet("peek");
  setFloor(p.floor_id);
  render();
  flyTo(camAt(p.x, p.y, Math.max(S.cam.z, 7)));
  try {
    const details = await sapi(`/place/${encodeURIComponent(id)}`);
    if (S.location && S.location !== id) {
      details.route = await sapi("/route", { method: "POST", body: JSON.stringify({ from_id: S.location, to_id: id, mode: "walk" }) }).catch(() => null);
    }
    if (S.selected === id) { S.place = details; renderSheet(); }
  } catch (error) { toast(error.message); }
}

async function openDirections(toId, fromId = S.location, stops = []) {
  if (!fromId) { toast("Set your location first"); return; }
  S.dirFrom = fromId; S.dirTo = toId; S.dirStops = stops; S.screen = "directions"; S.selected = null; S.results = []; S.chip = null;
  S.routes = {}; S.route = null; S.stepsOpen = false;
  setSheet("peek"); render();
  const body = (mode) => JSON.stringify({ from_id: fromId, to_id: toId, via_ids: stops, mode });
  const results = await Promise.all(MODES.map((m) => sapi("/route", { method: "POST", body: body(m.id) }).catch(() => null)));
  if (S.dirTo !== toId || S.dirStops !== stops) return;  // changed while loading
  MODES.forEach((m, i) => (S.routes[m.id] = results[i]));
  if (!S.routes[S.mode]) S.mode = MODES.find((m) => S.routes[m.id])?.id || S.mode;
  showRoute(S.routes[S.mode]);
}

function showRoute(route) {
  S.route = route || null;
  renderTop(); renderSheet(); renderFloors();
  if (!route) { buildOverlay(); drawRoute(); return; }
  const startFloor = route.points[0].floor_id;
  setFloor(startFloor);
  buildOverlay(); drawRoute(); renderFloors();
  fitRoute(startFloor);
}

function fitRoute(floor) {
  const on = S.route.points.filter((p) => p.floor_id === floor);
  const xs = on.map((p) => p.x), ys = on.map((p) => p.y);
  flyTo(camFor(Math.min(...xs) - 3, Math.min(...ys) - 3, Math.max(...xs) + 3, Math.max(...ys) + 3, 14));
}

// ---------- navigation ----------

function startNav(route = S.route) {
  if (!route) return;
  S.route = route; S.screen = "navigate"; S.selected = null; S.results = [];
  const pts = route.points, cum = [0];
  for (let i = 1; i < pts.length; i++) {
    const same = pts[i].floor_id === pts[i - 1].floor_id;
    cum.push(cum[i - 1] + (same ? Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y) : 0));
  }
  const starts = route.instructions.map((s) => cum[Math.min(s.index, cum.length - 1)]);
  S.nav = { s: 0, total: cum[cum.length - 1], cum, starts, seg: 0, pause: 0, last: null, current: -1, follow: true, done: false, heading: 0, pos: pts[0] };
  setSheet("peek"); setFloor(pts[0].floor_id); render();
  S.cam.z = NAV_ZOOM;
  requestAnimationFrame(navTick);
}

function navTick(now) {
  const n = S.nav;
  if (S.screen !== "navigate" || !n || n.done) return;
  const dt = n.last == null ? 0 : Math.min(0.1, (now - n.last) / 1000);
  n.last = now;
  if (n.pause > 0) n.pause -= dt; else n.s = Math.min(n.total, n.s + dt * NAV_SPEED_M_S);
  const pts = S.route.points, cum = n.cum;
  while (n.seg < pts.length - 2 && cum[n.seg + 1] <= n.s) {
    n.seg++;
    if (pts[n.seg].floor_id !== pts[n.seg - 1].floor_id) n.pause = LIFT_PAUSE_S;
  }
  const a = pts[n.seg], b = pts[n.seg + 1] || a;
  let pos = a;
  if (a.floor_id === b.floor_id && cum[n.seg + 1] > cum[n.seg]) {
    const t = Math.min(1, (n.s - cum[n.seg]) / (cum[n.seg + 1] - cum[n.seg]));
    pos = { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, floor_id: a.floor_id };
    if (Math.hypot(b.x - a.x, b.y - a.y) > 0.05) n.heading = 90 - (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI;
  } else if (n.s >= cum[n.seg + 1]) pos = b;
  n.pos = pos;
  if (pos.floor_id !== S.floor) { setFloor(pos.floor_id); buildOverlay(); }
  // current instruction: the last one (after "depart") whose stretch has started
  const ins = S.route.instructions;
  let current = Math.min(1, ins.length - 1);
  for (let i = 1; i < ins.length; i++) if (n.starts[i] <= n.s + 0.01) current = i;
  n.toManeuver = (n.starts[current + 1] ?? n.total) - n.s;
  if (current !== n.current) {
    const reached = ins[n.current];
    if (reached && reached.maneuver === "waypoint") {
      n.pause = Math.max(n.pause, 2.2);
      toast(`Stop reached: ${reached.text.replace(/^Stop \d+: /, "")}`);
      speak(`You have reached stop ${reached.stop}`);
    }
    n.current = current; speak(ins[current].text); renderTop();
  }
  else { const d = $("top").querySelector(".dist"); if (d && !ins[current].maneuver.match(/^(lift|stairs)/)) d.textContent = metres(Math.max(0, n.toManeuver)); }
  const item = overlayItems.find((o) => o.kind === "arrow");
  if (item) { item.x = pos.x; item.y = pos.y; }
  if (n.follow) { const c = camAt(pos.x, pos.y, S.cam.z); S.cam.x = c.x; S.cam.y = c.y; }
  applyCam();
  if (Math.floor(now / 1000) !== Math.floor((now - dt * 1000) / 1000)) renderSheet();
  if (n.s >= n.total && n.pause <= 0) {
    n.done = true;
    speak(`You have arrived at ${S.route.to.name}`);
    renderTop(); renderSheet();
    return;
  }
  requestAnimationFrame(navTick);
}

// ---------- import floor plans (level 4) ----------

async function openImport() {
  S.screen = "import"; S.selected = null; S.results = []; S.route = null; S.chip = null;
  S.importing = { stage: "choose", samples: S.importing?.samples || [], staged: [] };
  setSheet("peek"); render();
  if (!S.importing.samples.length) {
    S.importing.samples = await api("/api/samples").catch(() => []);
    if (S.screen === "import") renderSheet();
  }
}

async function runImport(form, previews) {
  S.importing = { ...S.importing, stage: "reading", previews };
  renderSheet();
  try {
    const response = await fetch("/api/import", { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || response.statusText);
    S.importing = { ...S.importing, stage: "done", summary: body };
  } catch (error) {
    S.importing = { ...S.importing, stage: "error", error: error.message };
  }
  if (S.screen === "import") renderSheet();
}

function importHtml() {
  const imp = S.importing || { stage: "choose", samples: [] };
  if (imp.stage === "choose") {
    const staged = imp.staged || [];
    const sampleRow = (sample) => `<div class="list-item" data-sample="${esc(sample.id)}">
        <span class="thumbs">${sample.images.map((src) => `<img src="${src}" alt="">`).join("")}</span>
        <div><div class="t">${esc(sample.name)}</div><div class="s">${sample.images.length} ${sample.kind === "photos" ? "phone photo" : "floor plan"}${sample.images.length > 1 ? "s" : ""}</div></div></div>`;
    return `<div class="pad"><div class="h1">Add a building</div>
        <div class="sub">Take one photo per floor with the whole sheet in view, or choose clean plan images. Rooms, doors, entrances, stairs and lifts are found automatically; room numbers and the title are read by AI.</div></div>
      <div class="actions">
        <button class="pill ${staged.length ? "" : "primary"}" data-act="take-photo"><span class="ms">photo_camera</span>Take photo</button>
        <button class="pill outline" data-act="pick-plans"><span class="ms">photo_library</span>Choose from photos</button>
      </div>
      ${staged.length ? `<div class="staged">${staged.map((item, i) => `<figure><img src="${item.url}" alt=""><button data-act="unstage" data-i="${i}" title="Remove"><span class="ms">close</span></button><figcaption>Floor photo ${i + 1}</figcaption></figure>`).join("")}</div>
        <div class="actions"><button class="pill primary" data-act="build-map"><span class="ms">auto_fix_high</span>Build map · ${staged.length} floor${staged.length > 1 ? "s" : ""}</button></div>` : ""}
      <div class="thick-divider"></div>
      <div class="section-title">Try a sample</div>
      ${imp.samples.filter((x) => x.kind === "photos").map(sampleRow).join("")}
      ${imp.samples.filter((x) => x.kind === "plans").map(sampleRow).join("")}`;
  }
  if (imp.stage === "reading") {
    return `<div class="pad"><div class="h1">Reading floor plans…</div><div class="sub">Finding rooms, doors, stairs and lifts, then reading the labels.</div></div>
      <div class="import-previews">${(imp.previews || []).map((src) => `<img src="${src}" alt="">`).join("")}</div>
      <div class="answer thinking"><span class="typing"><span></span><span></span><span></span></span>This takes about 10–20 seconds</div>`;
  }
  if (imp.stage === "error") {
    return `<div class="pad"><div class="h1">Couldn't read these plans</div><div class="sub warn">${esc(imp.error)}</div></div>
      <div class="actions"><button class="pill primary" data-act="import">Try again</button></div>`;
  }
  const s = imp.summary;
  const issues = s.issues.length
    ? s.issues.map((text) => `<div class="row"><span class="ms" style="color:#e37400">warning</span><div>${esc(text)}</div></div>`).join("")
    : `<div class="row"><span class="ms ok">check_circle</span><div>No problems found<small>Every space can be reached from an entrance</small></div></div>`;
  return `<div class="pad"><div class="h1">${esc(s.name)}</div>
      <div class="sub">${s.floors.length} floor${s.floors.length > 1 ? "s" : ""}<span class="dotsep">·</span>${s.rooms} rooms<span class="dotsep">·</span>${s.doors} doors<span class="dotsep">·</span>${s.entrances} entrance${s.entrances === 1 ? "" : "s"}<span class="dotsep">·</span>${s.stairs_lifts} stairs &amp; lifts</div></div>
    <div class="actions">
      <button class="pill primary" data-act="open-import"><span class="ms">map</span>Open map</button>
      <button class="pill outline" data-act="import"><span class="ms">add</span>Import another</button>
    </div>
    <div class="divider"></div>
    <div class="row"><span class="ms" ${s.size_guessed ? 'style="color:#e37400"' : ""}>straighten</span><div>${s.size_m[0]} × ${s.size_m[1]} m<small>${s.size_guessed ? "Estimated from typical room sizes: please enter the real size" : s.photos ? "From your measurement" : "Detected size. Wrong? Enter the real size."}</small></div>
      <button class="pill outline" style="margin-left:auto" data-act="rescale">Edit</button></div>
    <div class="row"><span class="ms">layers</span><div>${s.floors.map(esc).join(", ")}<small>Lined up by their stairs and lifts</small></div></div>
    ${s.floors.length > 1 ? `<div class="row"><span class="ms" ${s.heights_guessed ? 'style="color:#e37400"' : ""}>height</span><div>Floor-to-floor ${s.floor_heights_m.join(" / ")} m<small>${s.heights_guessed ? "Assumed: plans don't show how far apart floors are. Stair times depend on it." : "As you entered them"}</small></div>
      <button class="pill outline" style="margin-left:auto" data-act="heights">Edit</button></div>` : ""}
    ${issues}
    <div class="row"><span class="ms">auto_awesome</span><div>Tell the assistant what rooms are<small>e.g. "0.17 is the kitchen" or "H-A1 is the press shop"</small></div></div>`;
}

async function refreshBuildings(selectId) {
  const buildings = await api("/api/buildings");
  const select = $("building");
  select.innerHTML = buildings.map((b) => `<option value="${b.id}">${b.imported ? "★ " : ""}${esc(b.name)} · ${b.floors} floor${b.floors > 1 ? "s" : ""}</option>`).join("");
  if (selectId) select.value = selectId;
}

// ---------- ask ----------

async function askQuestion(text) {
  closeSearch();
  S.screen = "ask"; S.selected = null; S.chip = null;
  const turn = { q: text, a: null, route: null };
  S.chat.push(turn);
  setSheet("half"); render();
  try {
    const result = await api("/api/chat", { method: "POST", body: JSON.stringify({ session_id: S.session, building_id: S.buildingId, message: text, location_id: S.location }) });
    turn.a = result.reply;
    S.map = result.map; indexMap();
    const view = result.view || {};
    const ids = [...new Set([...(view.highlight_assets || []), ...(view.highlight_spaces || [])])].filter((id) => id !== S.location);
    S.results = ids.map((id) => { const p = pointOf(id); return p && { id, name: nameOf(id), floor_id: p.floor_id, x: p.x, y: p.y, icon: assetById[id] ? "precision_manufacturing" : "place" }; }).filter(Boolean);
    if (view.route) {
      turn.route = view.route; S.route = view.route; S.routes = { [view.route.mode]: view.route }; S.mode = view.route.mode;
      S.dirFrom = view.route.from.id; S.dirTo = view.route.to.id; S.dirStops = (view.route.stops || []).map((x) => x.id);
      S.results = S.results.filter((r) => r.id !== view.route.to.id);
    } else S.route = null;
    if (S.screen !== "ask") return;
    render();
    if (view.route) fitRoute(view.route.points[0].floor_id === S.floor ? S.floor : (setFloor(view.route.points[0].floor_id), S.floor));
    else if (S.results.length) fitResults();
    speak(turn.a);
  } catch (error) {
    turn.a = `Sorry, something went wrong: ${error.message}`;
    renderSheet();
  }
}

// ---------- search screen ----------

let searchTimer = null;
function openSearch(prefill = "") {
  $("searchScreen").hidden = false;
  $("searchInput").value = prefill;
  $("searchInput").focus();
  updateSuggestions();
}
function closeSearch() { $("searchScreen").hidden = true; S.picking = false; $("searchInput").placeholder = "Search or ask about this building"; }

function pickStop() {
  S.picking = true;
  $("searchInput").placeholder = "Add a stop";
  openSearch();
}

function addStop(id) {
  closeSearch();
  // like Google Maps: the new stop becomes the destination, the old destination a stop on the way
  openDirections(id, S.dirFrom, [...S.dirStops, S.dirTo]);
}

function updateSuggestions() {
  const q = $("searchInput").value.trim();
  clearTimeout(searchTimer);
  if (S.picking) {
    if (!q) {
      $("searchList").innerHTML = S.map.categories.map((c) => `<div class="list-item" data-pickcat="${c.id}"><span class="ico"><span class="ms">${c.icon}</span></span><div><div class="t">${esc(c.label)}</div><div class="s">Nearest first</div></div></div>`).join("");
      return;
    }
    searchTimer = setTimeout(async () => {
      const { results } = await sapi(`/search?q=${encodeURIComponent(q)}`).catch(() => ({ results: [] }));
      if ($("searchInput").value.trim() === q) $("searchList").innerHTML = results.slice(0, 15).map(listItem).join("") || `<div class="pad sub" style="padding-top:16px">No matches</div>`;
    }, 120);
    return;
  }
  if (!q) {
    $("searchList").innerHTML = S.map.categories.map((c) => `<div class="list-item" data-cat="${c.id}"><span class="ico"><span class="ms">${c.icon}</span></span><div><div class="t">${esc(c.label)}</div><div class="s">Category</div></div></div>`).join("") +
      suggestions().map((s) => `<div class="list-item ask-row" data-ask="${esc(s)}"><span class="ico"><span class="ms">auto_awesome</span></span><div><div class="t">${esc(s)}</div><div class="s">Ask this building</div></div></div>`).join("");
    return;
  }
  searchTimer = setTimeout(async () => {
    const { results } = await sapi(`/search?q=${encodeURIComponent(q)}`).catch(() => ({ results: [] }));
    if ($("searchInput").value.trim() !== q) return;
    $("searchList").innerHTML = `<div class="list-item ask-row" data-ask="${esc(q)}"><span class="ico"><span class="ms">auto_awesome</span></span><div><div class="t">Ask: “${esc(q)}”</div><div class="s">Get an answer from the building</div></div></div>` +
      results.slice(0, 12).map(listItem).join("");
  }, 120);
}

// ---------- voice ----------

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
function listen() {
  if (!Recognition) { toast("Voice input needs Chrome or Edge"); return; }
  const rec = new Recognition();
  rec.lang = "en-US"; rec.interimResults = false;
  rec.onresult = (e) => { S.sound = true; askQuestion(e.results[0][0].transcript); };
  rec.onerror = () => toast("Didn't catch that");
  toast("Listening…");
  rec.start();
}

// ---------- events ----------

function onClick(e) {
  const el = e.target.closest("[data-act],[data-cat],[data-ask],[data-mode],[data-floor],[data-pickcat],[data-sample],.list-item[data-id]");
  if (!el || el.closest("#map")) return;
  if (el.dataset.floor) { setFloor(el.dataset.floor); return; }
  if (el.dataset.cat) { closeSearch(); showCategory(el.dataset.cat); return; }
  if (el.dataset.ask) { askQuestion(el.dataset.ask); return; }
  if (el.dataset.mode) { S.mode = el.dataset.mode; if (S.routes[S.mode]) showRoute(S.routes[S.mode]); else { S.route = null; renderTop(); renderSheet(); drawRoute(); } return; }
  if (el.dataset.sample) {
    const sample = S.importing.samples.find((x) => x.id === el.dataset.sample);
    const form = new FormData(); form.append("sample", el.dataset.sample);
    runImport(form, sample ? sample.images : []);
    return;
  }
  if (el.dataset.pickcat) {
    sapi(`/search?category=${el.dataset.pickcat}&from_id=${encodeURIComponent(S.dirTo || S.location || "")}`)
      .then(({ results }) => ($("searchList").innerHTML = results.map(listItem).join("")));
    return;
  }
  if (el.classList.contains("list-item") && el.dataset.id) {
    if (S.picking) { addStop(el.dataset.id); return; }
    closeSearch(); selectPlace(el.dataset.id); return;
  }
  const act = el.dataset.act;
  if (act === "search") openSearch();
  else if (act === "mic") listen();
  else if (act === "back" || act === "close") {
    if (S.screen === "directions" && S.selected === null && S.dirTo) { selectPlace(S.dirTo); return; }
    if (S.screen === "place" && S.chip) { showCategory(S.chip); return; }
    goExplore();
  }
  else if (act === "directions") openDirections(S.selected);
  else if (act === "start") {
    if (S.screen === "place") { openDirections(S.selected).then(() => startNav()); return; }
    startNav();
  }
  else if (act === "steps") { S.stepsOpen = !S.stepsOpen; setSheet(S.stepsOpen ? "full" : "peek"); renderSheet(); }
  else if (act === "swap") { if (S.dirFrom && S.dirTo) openDirections(S.dirFrom, S.dirTo); }
  else if (act === "add-stop") pickStop();
  else if (act === "import") openImport();
  else if (act === "pick-plans") $("planInput").click();
  else if (act === "take-photo") $("cameraInput").click();
  else if (act === "unstage") { S.importing.staged.splice(Number(el.dataset.i), 1); renderSheet(); }
  else if (act === "build-map") {
    const form = new FormData();
    S.importing.staged.forEach((item) => form.append("files", item.file, item.file.name));
    runImport(form, S.importing.staged.map((item) => item.url));
  }
  else if (act === "open-import") {
    const id = S.importing.summary.id;
    refreshBuildings(id).then(() => loadBuilding(id)).then(() => toast("Tip: tell the assistant what rooms are"));
  }
  else if (act === "heights") {
    const s = S.importing.summary;
    const value = prompt(`Floor-to-floor distance in metres (how high a staircase climbs), lowest floor first (${s.floors.join(", ")})`, s.floor_heights_m.join(", "));
    const heights = String(value || "").split(/[,;\s]+/).map((v) => parseFloat(v)).filter((v) => v > 0);
    if (heights.length) api(`/api/import/${encodeURIComponent(s.id)}/scale`, { method: "POST", body: JSON.stringify({ width_m: s.size_m[0], depth_m: s.size_m[1], floor_heights_m: heights }) })
      .then((summary) => { S.importing.summary = summary; renderSheet(); toast("Floor heights updated"); }).catch((error) => toast(error.message));
  }
  else if (act === "rescale") {
    const s = S.importing.summary;
    const value = prompt(`Size of ${s.name} in metres: width × depth (depth optional)`, `${s.size_m[0]} x ${s.size_m[1]}`);
    const [width, depth] = String(value || "").replace(/,/g, ".").split(/[x×*\s]+/).map(parseFloat);
    if (width > 0) api(`/api/import/${encodeURIComponent(s.id)}/scale`, { method: "POST", body: JSON.stringify({ width_m: width, depth_m: depth > 0 ? depth : null }) })
      .then((summary) => { S.importing.summary = summary; renderSheet(); toast("Size updated"); }).catch((error) => toast(error.message));
  }
  else if (act === "remove-stop") {
    const all = [...S.dirStops, S.dirTo];
    all.splice(Number(el.dataset.i), 1);
    openDirections(all[all.length - 1], S.dirFrom, all.slice(0, -1));
  }
  else if (act === "here") { S.location = S.selected; toast("Location set"); render(); }
  else if (act === "ask-about") openSearch(`How do I get to ${nameOf(S.selected)}?`);
  else if (act === "preview") { S.screen = "directions"; S.stepsOpen = true; setSheet("half"); render(); fitRoute(S.route.points[0].floor_id); }
  else if (act === "exit") { S.nav = null; S.screen = "directions"; S.cam.z = Math.min(S.cam.z, 10); setSheet("peek"); render(); fitRoute(S.floor); }
  else if (act === "done") { S.location = S.route.to.id; S.nav = null; goExplore(); toast("Location updated"); }
  else if (act === "sound") { S.sound = !S.sound; if (!S.sound) speechSynthesis?.cancel(); renderSheet(); }
}

// ---------- boot ----------

function indexMap() {
  spaceById = Object.fromEntries(S.map.spaces.map((s) => [s.id, s]));
  assetById = Object.fromEntries(S.map.assets.map((a) => [a.id, a]));
  floorById = Object.fromEntries(S.map.floors.map((f) => [f.id, f]));
}

async function loadBuilding(id) {
  S.session = crypto.randomUUID(); S.buildingId = id; S.chat = []; S.route = null; S.results = []; S.selected = null; S.nav = null;
  S.map = await api(`/api/sessions/${S.session}/map?building_id=${id}`);
  indexMap();
  S.location = S.map.default_location;
  S.floor = pointOf(S.location).floor_id;
  S.screen = "explore";
  setSheet("peek", false);
  render();
  const b = floorBounds(S.floor);
  S.cam = camFor(b[0], b[1], b[2], b[3]);
  applyCam();
}

function fitPhone() {
  if (innerWidth <= 460) return;
  const scale = Math.min(1, (innerHeight - 24) / 876, (innerWidth - (innerWidth > 760 ? 400 : 24)) / 417);
  $("phoneWrap").style.setProperty("--scale", scale.toFixed(3));
}

function tickClock() { $("sbTime").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).replace(/\s?[AP]M/i, ""); }

async function boot() {
  fitPhone(); addEventListener("resize", () => { fitPhone(); applyCam(); });
  tickClock(); setInterval(tickClock, 15000);
  initGestures(); initSheet();
  document.addEventListener("click", onClick);
  $("searchBack").onclick = closeSearch;
  // photos are collected first (a camera gives one floor at a time), then built into one building
  for (const id of ["planInput", "cameraInput"]) {
    $(id).addEventListener("change", () => {
      const files = [...$(id).files];
      if (!files.length || !S.importing) return;
      S.importing.staged = [...(S.importing.staged || []), ...files.map((file) => ({ file, url: URL.createObjectURL(file) }))];
      $(id).value = "";
      renderSheet();
    });
  }
  $("searchMic").onclick = () => { closeSearch(); listen(); };
  $("searchInput").addEventListener("input", updateSuggestions);
  $("searchInput").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = $("searchInput").value.trim();
    if (!q) return;
    const words = q.split(/\s+/).length;
    if (words >= 4 || q.endsWith("?")) askQuestion(q); else { closeSearch(); showQuery(q); }
  });
  $("locBtn").onclick = () => {
    const p = S.location && pointOf(S.location);
    if (!p) return;
    setFloor(p.floor_id);
    flyTo(camAt(p.x, p.y, Math.max(8, S.cam.z)));
  };
  $("layersBtn").onclick = () => { S.showMachines = !S.showMachines; $("layersBtn").classList.toggle("on", !S.showMachines); buildOverlay(); toast(S.showMachines ? "Machines shown" : "Machines hidden"); };
  const buildings = await api("/api/buildings");
  const select = $("building");
  await refreshBuildings();
  const preferred = buildings.find((b) => b.id === "proc-factory-dev-005") || buildings[0];
  select.value = preferred.id;
  select.onchange = () => loadBuilding(select.value);
  await loadBuilding(preferred.id);
}

boot().catch((error) => toast(`Could not start: ${error.message}`));
