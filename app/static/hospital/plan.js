const NS = "http://www.w3.org/2000/svg";

export const DOT_COLORS = { patient: "#1a73e8", pager: "#9334e6", asset: "#e8710a", staff: "#188038", visitor: "#1a73e8", me: "#1a73e8" };
export const KIND_LABELS = { patient: "Patients", pager: "Pagers", asset: "Equipment", staff: "Staff" };
export const ROOM_LABELS = {
  emergency: "Emergency", triage: "Triage", radiology: "Radiology", ward: "Ward", patient_room: "Patient room",
  nurse_station: "Nurses' station", toilet: "Toilet", cafe: "Café", kiosk: "Kiosk", lobby: "Entrance", pharmacy: "Pharmacy",
};
export const MANEUVER = {
  depart: ["trip_origin", "Start walking"], left: ["turn_left", "Turn left"], right: ["turn_right", "Turn right"],
  straight: ["straight", "Go straight"], lift_up: ["elevator", "Take the lift up"], lift_down: ["elevator", "Take the lift down"],
  stairs_up: ["stairs", "Take the stairs up"], stairs_down: ["stairs", "Take the stairs down"], arrive: ["location_on", "You have arrived"],
  waypoint: ["flag", "Stopover"], stop: ["local_cafe", "Stopover"],
};

// reversed "inferno" (perceptually uniform), low = pale yellow, high = deep purple; ends trimmed so it reads on a light plan
const HEAT_STOPS = ["#fcffa4", "#f6d746", "#fca50a", "#f37819", "#dd513a", "#bc3754", "#932667", "#6a176e", "#420a68"];
export const HEAT_GRADIENT = `linear-gradient(90deg, ${HEAT_STOPS.join(", ")})`;
const HEAT_RGB = HEAT_STOPS.map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));

function heatColor(t) {
  const f = Math.min(1, Math.max(0, t)) * (HEAT_RGB.length - 1), i = Math.min(HEAT_RGB.length - 2, Math.floor(f)), k = f - i;
  return HEAT_RGB[i].map((c, j) => Math.round(c + (HEAT_RGB[i + 1][j] - c) * k));
}

function heatImage(grid, log) {
  const { cols, rows, counts } = grid;
  const max = grid.max || Math.max(0, ...counts.flat());
  if (!cols || !rows || !max) return null;
  const canvas = Object.assign(document.createElement("canvas"), { width: cols, height: rows });
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(cols, rows);
  const norm = log ? (v) => Math.log1p(v) / Math.log1p(max) : (v) => v / max;
  for (let r = 0; r < rows; r++) {
    const row = counts[r] || [];
    for (let c = 0; c < cols; c++) {
      const v = row[c] || 0;
      if (v <= 0) continue;
      const t = norm(v), [R, G, B] = heatColor(t);
      const o = ((rows - 1 - r) * cols + c) * 4;  // row 0 is at origin y (bottom); canvas rows run top-down
      img.data[o] = R; img.data[o + 1] = G; img.data[o + 2] = B; img.data[o + 3] = Math.round(255 * (0.3 + 0.55 * t));
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas.toDataURL();
}

export function floorLabel(floor, long = false) {
  const level = floor?.level ?? 0;
  if (long) return level === 0 ? "Ground floor" : level > 0 ? `Floor ${level}` : `Basement ${-level}`;
  return level === 0 ? "GF" : level > 0 ? `F${level}` : `B${-level}`;
}

export function roomOptions(map, { exclude = ["corridor", "lift", "stair", "outdoor"] } = {}) {
  const floors = [...map.floors].sort((a, b) => b.level - a.level);
  return floors.map((f) => {
    const rooms = map.spaces.filter((s) => s.floor_id === f.id && !exclude.includes(s.type))
      .sort((a, b) => a.name.localeCompare(b.name, "en", { numeric: true }));
    if (!rooms.length) return "";
    const opts = rooms.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}${s.ref ? ` (${esc(s.ref)})` : ""}</option>`).join("");
    return `<optgroup label="${esc(floorLabel(f, true))}">${opts}</optgroup>`;
  }).join("");
}

const LANDMARKS = new Set(["emergency", "lobby", "cafe", "pharmacy", "operating_theatre", "recovery", "waiting"]);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pts = (polygon) => polygon.map(([x, y]) => `${x},${-y}`).join(" ");
const lerpDeg = (a, b, k) => a + ((((b - a) % 360) + 540) % 360 - 180) * k;
let planSeq = 0;

function el(tag, attrs, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function inside([x, y], polygon) {
  let hit = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i], [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

function bounds(points) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of points) { minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); }
  return minX === Infinity ? null : [minX, minY, maxX, maxY];
}

export function createPlan(container, mapPayload, opts = {}) {
  const st = {
    map: mapPayload, floor: null, cam: { x: 0, y: 0, z: 4 }, route: null, fromIndex: 0, dots: [], hl: new Set(),
    assets: null, assetCls: {}, tap: [], floorCbs: [], moved: 0, sized: false, pad: opts.pad ?? 24, fitTarget: undefined, prevPos: {}, tweenStart: 0,
    heat: null, heatHref: null, follow: null, prevHeading: {},
  };
  const uid = `plan${++planSeq}`;
  const interactive = opts.interactive !== false;
  container.classList.add("plan");
  container.classList.toggle("static", !interactive);
  container.innerHTML = `<svg class="plan-svg" xmlns="${NS}"><defs>
      <radialGradient id="${uid}-cone" gradientUnits="userSpaceOnUse" cx="0" cy="0" r="54"><stop offset="0" stop-color="#4d94ff" stop-opacity=".55"/><stop offset="1" stop-color="#4d94ff" stop-opacity="0"/></radialGradient>
      <filter id="${uid}-shadow" x="-60%" y="-60%" width="220%" height="220%"><feDropShadow dx="0" dy="1.5" stdDeviation="1.8" flood-color="#000" flood-opacity=".55"/></filter>
    </defs><g class="plan-world"></g><g class="plan-screen"></g><g class="plan-dots"></g></svg>
    ${opts.floorButtons === false || !interactive ? "" : '<div class="plan-floors"></div>'}
    ${opts.controls === false || !interactive ? "" : `<div class="plan-zoom"><button type="button" data-z="in" aria-label="Zoom in"><span class="ms">add</span></button><button type="button" data-z="out" aria-label="Zoom out"><span class="ms">remove</span></button><button type="button" data-z="fit" aria-label="Whole floor"><span class="ms">fit_screen</span></button></div>`}`;
  const svgEl = container.querySelector(".plan-svg");
  const world = container.querySelector(".plan-world");
  const screen = container.querySelector(".plan-screen");
  const dotsEl = container.querySelector(".plan-dots");
  const floorsEl = container.querySelector(".plan-floors");
  const W = () => container.clientWidth || 1;
  const H = () => container.clientHeight || 1;
  const project = (x, y) => [W() / 2 + (x - st.cam.x) * st.cam.z, H() / 2 - (y - st.cam.y) * st.cam.z];
  const unproject = (sx, sy) => [st.cam.x + (sx - W() / 2) / st.cam.z, st.cam.y - (sy - H() / 2) / st.cam.z];
  const clampZ = (z) => Math.max(0.5, Math.min(80, z));
  const tweenK = () => { const dur = opts.dotTween ?? 900; return dur ? Math.min(1, (performance.now() - st.tweenStart) / dur) : 1; };
  const floorById = () => Object.fromEntries(st.map.floors.map((f) => [f.id, f]));

  function defaultFloor() {
    const floors = st.map.floors;
    return (opts.floor && floors.find((f) => f.id === opts.floor)?.id) || floors.find((f) => f.level === 0)?.id || floors[0]?.id;
  }

  function kindAt(p) {
    const s = st.map.spaces.find((sp) => sp.floor_id === p.floor_id && (sp.type === "lift" || sp.type === "stair") && inside([p.x, p.y], sp.polygon));
    return s?.type === "stair" ? "stairs" : "lift";
  }

  // ---------- world layer (metres) ----------
  function renderWorld() {
    world.innerHTML = "";
    const spaces = st.map.spaces.filter((s) => s.floor_id === st.floor);
    for (const s of spaces) el("polygon", { points: pts(s.polygon), class: "plan-outline" }, world);
    const closed = new Set(st.map.closed || []);
    for (const c of st.map.connectors || []) if (closed.has(c.id)) (c.space_ids || []).forEach((id) => closed.add(id));
    for (const s of spaces) {
      const cls = ["sp", `t-${s.type}`, s.room_type ? `rt-${s.room_type}` : "", st.hl.has(s.id) ? "hl" : "",
        closed.has(s.id) ? "closed" : ""].join(" ");
      el("polygon", { points: pts(s.polygon), class: cls, "data-id": s.id }, world);
    }
    if (st.heatHref && st.heat.floor_id === st.floor) {
      const { origin: [x0, y0], cell_m: cell, cols, rows } = st.heat;
      el("image", { href: st.heatHref, x: x0, y: -(y0 + rows * cell), width: cols * cell, height: rows * cell,
        preserveAspectRatio: "none", class: "plan-heat" }, world);
    }
    for (const o of st.map.openings || []) {
      if (o.floor_id !== st.floor || !(o.space_ids || []).includes("outside")) continue;
      const w = o.width_m || 1.2;
      el("rect", { x: o.position[0] - w / 2, y: -o.position[1] - 0.25, width: w, height: 0.5, rx: 0.25, class: "plan-entrance" }, world);
    }
    for (const a of st.assets || st.map.assets) {
      if (a.floor_id !== st.floor || !a.position) continue;
      const f = a.footprint || {}, w = f.width_m || 0.6, d = f.depth_m || 0.6, r = f.rotation_deg || 0;
      const [x, y] = [a.position[0], -a.position[1]];
      const extra = st.assetCls[a.id] || "";
      el("rect", {
        x: x - w / 2, y: y - d / 2, width: w, height: d, rx: Math.min(w, d) * 0.18,
        class: `as at-${a.type || "other"} ac-${a.category || "other"} ${extra} ${st.hl.has(a.id) ? "hl" : ""}`,
        transform: `rotate(${-r} ${x} ${y})`, "data-id": a.id,
      }, world).appendChild(Object.assign(document.createElementNS(NS, "title"), { textContent: a.name || a.type || a.id }));
    }
  }

  // ---------- screen layer (pixels) ----------
  function renderScreen() {
    screen.innerHTML = "";
    const z = st.cam.z;
    if (opts.labels !== false) {
      for (const s of st.map.spaces) {
        if (s.floor_id !== st.floor || ["corridor", "outdoor"].includes(s.type)) continue;
        const b = bounds(s.polygon), wpx = (b[2] - b[0]) * z, hpx = (b[3] - b[1]) * z;
        const [ax, ay] = s.anchor ? project(s.anchor[0], s.anchor[1]) : project((b[0] + b[2]) / 2, (b[1] + b[3]) / 2);
        if (s.type === "lift" || s.type === "stair") {
          if (wpx > 16 && hpx > 16) el("text", { x: ax, y: ay, class: "plan-icon" }, screen).textContent = s.type === "lift" ? "elevator" : "stairs";
          continue;
        }
        // "minimal" (the pager): only what matters now (highlighted rooms) and landmarks people steer by
        if (opts.labels === "minimal" && !st.hl.has(s.id) && !LANDMARKS.has(s.room_type)) continue;
        const label = s.ref && wpx < s.name.length * 6.4 + 8 ? s.ref : s.name;
        if (wpx < label.length * 6.4 + 8 || hpx < 16) continue;
        el("text", { x: ax, y: ay, class: `plan-label${st.hl.has(s.id) ? " hl" : ""}` }, screen).textContent = label;
      }
    }
    drawRouteLayer();
    renderDots();
  }

  let tweening = false;
  function renderDots() {
    dotsEl.innerHTML = "";
    const k = tweenK();
    for (const d of st.dots) {
      if (d.floor_id !== st.floor || d.x == null) continue;
      const from = st.prevPos[d.id];
      const [x, y] = from && k < 1 ? project(from.x + (d.x - from.x) * k, from.y + (d.y - from.y) * k) : project(d.x, d.y);
      const color = d.color || DOT_COLORS[d.kind] || "#5f6368";
      const g = el("g", { class: `plan-dot k-${d.kind}${st.hl.has(d.id) ? " hl" : ""}`, transform: `translate(${x} ${y})`, "data-dot": d.id }, dotsEl);
      if (d.heading_deg != null) {
        const h0 = st.prevHeading[d.id];
        const deg = h0 != null && k < 1 ? lerpDeg(h0, d.heading_deg, k) : d.heading_deg;
        // heading_deg is in building coordinates (0 = +x, counter-clockwise); screen y points down, hence the sign flip
        const r = el("g", { class: "plan-heading", transform: `rotate(${-deg})` }, g);
        el("path", { d: "M0 0L47.6-25.3A54 54 0 0 1 47.6 25.3Z", fill: `url(#${uid}-cone)` }, r);
        el("path", { d: "M13 0L-9-11L-4 0L-9 11Z", fill: color, stroke: "#fff", "stroke-width": 2.6, "stroke-linejoin": "round", filter: `url(#${uid}-shadow)` }, r);
      } else if (d.kind === "me" || d.kind === "visitor" || d.big) {
        el("circle", { r: 16, fill: color, opacity: 0.18 }, g);
        el("circle", { r: 8, fill: color, stroke: "#fff", "stroke-width": 3 }, g);
      } else if (d.kind === "asset") {
        el("rect", { x: -4.5, y: -4.5, width: 9, height: 9, rx: 2, fill: d.in_use ? "#fff" : color, stroke: color, "stroke-width": 2 }, g);
      } else {
        el("circle", { r: d.kind === "pager" ? 4.5 : 6.5, fill: color, stroke: "#fff", "stroke-width": 2 }, g);
      }
      if (st.hl.has(d.id)) el("circle", { r: 13, fill: "none", stroke: color, "stroke-width": 2.5 }, g);
      if (d.label) el("title", {}, g).textContent = d.label;
      if (d.showLabel && d.label) el("text", { x: 11, y: 4, class: "plan-dot-label" }, g).textContent = d.label;
    }
    if (k < 1 && !tweening && (Object.keys(st.prevPos).length || Object.keys(st.prevHeading).length)) {
      tweening = true;
      requestAnimationFrame(() => { tweening = false; renderDots(); });
    }
  }

  function drawRouteLayer() {
    const route = st.route;
    if (!route?.points?.length) return;
    const P = route.points, fb = floorById();
    const lineAttrs = { fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" };
    const line = (pts, classes) => {
      if (pts.length < 2) return;
      const points = pts.map((p) => project(p.x, p.y).join(",")).join(" ");
      for (const cls of classes) el("polyline", { ...lineAttrs, points, class: cls }, screen);
    };
    // fromIndex may be fractional: the line is cut exactly there, not at the last corner passed.
    const at = Math.max(0, Math.min(st.fromIndex, P.length - 1)), i0 = Math.floor(at), f = at - i0;
    const a = P[i0], b = P[Math.min(i0 + 1, P.length - 1)];
    const cut = b.floor_id === a.floor_id ? { floor_id: a.floor_id, x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f } : a;
    const segs = [];
    let cur = null;
    for (let i = 0; i < P.length; i++) {
      if (P[i].floor_id !== st.floor) { cur = null; continue; }
      if (!cur) segs.push(cur = []);
      cur.push(i);
    }
    for (const seg of segs) {
      const here = seg.includes(i0) ? [cut] : [];
      line([...seg.filter((i) => i <= i0).map((i) => P[i]), ...here], ["plan-route-walked"]);
      line([...here, ...seg.filter((i) => i > i0).map((i) => P[i])], ["plan-route-casing", "plan-route"]);
    }
    const marker = (p, icon, text, cls) => {
      const [x, y] = project(p.x, p.y);
      const g = el("g", { class: `plan-marker ${cls}`, transform: `translate(${x} ${y})` }, screen);
      el("circle", { r: 12 }, g);
      el("text", { class: "plan-marker-icon", y: 0 }, g).textContent = icon;
      if (text) {
        const t = el("text", { class: "plan-marker-text", x: 17, y: 4 }, g);
        t.textContent = text;
      }
    };
    for (let i = 0; i < P.length - 1; i++) {
      const a = P[i], b = P[i + 1];
      if (a.floor_id === b.floor_id) continue;
      const kind = kindAt(a) === "stairs" || kindAt(b) === "stairs" ? "stairs" : "lift";
      const icon = kind === "lift" ? "elevator" : "stairs";
      if (a.floor_id === st.floor) marker(a, icon, `to ${floorLabel(fb[b.floor_id], true).toLowerCase()}`, "transfer");
      if (b.floor_id === st.floor) marker(b, icon, `from ${floorLabel(fb[a.floor_id], true).toLowerCase()}`, "transfer");
    }
    const first = P[0], last = P[P.length - 1];
    if (first.floor_id === st.floor) {
      const [x, y] = project(first.x, first.y);
      el("circle", { cx: x, cy: y, r: 6, class: "plan-start" }, screen);
    }
    if (last.floor_id === st.floor) {
      const [x, y] = project(last.x, last.y);
      el("path", { d: "M0 0C-1.5-6-9-10-9-17a9 9 0 0 1 18 0c0 7-7.5 11-9 17Z", transform: `translate(${x} ${y})`, class: "plan-goal" }, screen);
      el("circle", { cx: x, cy: y - 17, r: 3.4, fill: "#fff" }, screen);
    }
  }

  function renderFloors() {
    if (!floorsEl) return;
    const routeFloors = new Set((st.route?.points || []).map((p) => p.floor_id));
    const counts = {};
    for (const d of st.dots) if (d.floor_id && d.kind !== "me") counts[d.floor_id] = (counts[d.floor_id] || 0) + 1;
    floorsEl.innerHTML = [...st.map.floors].sort((a, b) => b.level - a.level).map((f) =>
      `<button type="button" data-floor="${esc(f.id)}" class="${f.id === st.floor ? "on" : ""} ${routeFloors.has(f.id) ? "route" : ""}" title="${esc(floorLabel(f, true))}">${esc(floorLabel(f))}${counts[f.id] && opts.floorCounts ? `<i>${counts[f.id]}</i>` : ""}</button>`).join("");
  }

  let queued = false;
  function applyCam() {
    const { x, y, z } = st.cam;
    world.setAttribute("transform", `translate(${W() / 2 - x * z} ${H() / 2 + y * z}) scale(${z})`);
    if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; renderScreen(); }); }
  }

  let flight = null;
  function flyTo(target, ms = 400) {
    cancelAnimationFrame(flight);
    const from = { ...st.cam }, start = performance.now();
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
    const step = (now) => {
      const t = ms ? Math.min(1, (now - start) / ms) : 1, k = ease(t);
      st.cam = { z: from.z * Math.pow(target.z / from.z, k), x: from.x + (target.x - from.x) * k, y: from.y + (target.y - from.y) * k };
      applyCam();
      if (t < 1) flight = requestAnimationFrame(step);
    };
    flight = requestAnimationFrame(step);
  }

  function camFor(b, maxZ = 40) {
    const pad = st.pad;
    const w = Math.max(b[2] - b[0], 3), h = Math.max(b[3] - b[1], 3);
    const z = clampZ(Math.min(maxZ, (W() - pad * 2) / w, (H() - pad * 2) / h));
    return { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, z };
  }

  function fitBounds(target) {
    if (Array.isArray(target)) return target;
    if (target === "route" && st.route) return bounds(st.route.points.filter((p) => p.floor_id === st.floor).map((p) => [p.x, p.y]));
    if (target?.space_id) {
      const s = st.map.spaces.find((sp) => sp.id === target.space_id);
      return s ? bounds(s.polygon) : null;
    }
    return bounds(st.map.spaces.filter((s) => s.floor_id === st.floor).flatMap((s) => s.polygon));
  }

  // ---------- gestures ----------
  const pointers = new Map();
  let gesture = null;
  const local = (e) => { const r = svgEl.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  if (interactive) wireGestures();
  function wireGestures() {
    svgEl.addEventListener("pointerdown", (e) => {
      svgEl.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, local(e));
      cancelAnimationFrame(flight);
      if (pointers.size === 1) gesture = { start: local(e), cam: { ...st.cam }, moved: false };
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        gesture = { pinch: true, d0: Math.hypot(a.x - b.x, a.y - b.y) || 1, cam: { ...st.cam }, world: unproject(mid.x, mid.y), moved: true };
      }
      container.classList.add("dragging");
    });
    svgEl.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId) || !gesture) return;
      pointers.set(e.pointerId, local(e));
      if (gesture.pinch && pointers.size >= 2) {
        const [a, b] = [...pointers.values()];
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const z = clampZ(gesture.cam.z * Math.hypot(a.x - b.x, a.y - b.y) / gesture.d0);
        st.cam = { z, x: gesture.world[0] - (mid.x - W() / 2) / z, y: gesture.world[1] + (mid.y - H() / 2) / z };
        st.moved = Date.now();
        applyCam();
      } else if (!gesture.pinch) {
        const p = local(e), dx = p.x - gesture.start.x, dy = p.y - gesture.start.y;
        if (Math.hypot(dx, dy) > 5) gesture.moved = true;
        if (gesture.moved) { st.cam = { ...st.cam, x: gesture.cam.x - dx / st.cam.z, y: gesture.cam.y + dy / st.cam.z }; st.moved = Date.now(); applyCam(); }
      }
    });
    const end = (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      if (gesture && !gesture.moved && !gesture.pinch && pointers.size === 0) tapAt(e);
      if (pointers.size === 0) { gesture = null; container.classList.remove("dragging"); }
    };
    svgEl.addEventListener("pointerup", end);
    svgEl.addEventListener("pointercancel", end);
    svgEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      cancelAnimationFrame(flight);
      const p = local(e), w = unproject(p.x, p.y);
      const z = clampZ(st.cam.z * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018)));
      st.cam = { z, x: w[0] - (p.x - W() / 2) / z, y: w[1] + (p.y - H() / 2) / z };
      st.moved = Date.now();
      applyCam();
    }, { passive: false });
    container.addEventListener("click", (e) => {
      const fbtn = e.target.closest("[data-floor]");
      if (fbtn) { api.setFloor(fbtn.dataset.floor); st.moved = Date.now(); return; }
      const zbtn = e.target.closest("[data-z]");
      if (!zbtn) return;
      const k = zbtn.dataset.z;
      if (k === "fit") { st.moved = 0; api.fit(); return; }
      st.moved = Date.now();
      flyTo({ ...st.cam, z: clampZ(st.cam.z * (k === "in" ? 1.6 : 1 / 1.6)) }, 220);
    });
  }

  function tapAt(e) {
    const p = local(e), [x, y] = unproject(p.x, p.y);
    let hit = { x, y, floor_id: st.floor, id: null, kind: null };
    for (const node of document.elementsFromPoint(e.clientX, e.clientY)) {
      const dot = node.closest?.("[data-dot]");
      if (dot) { hit = { ...hit, id: dot.dataset.dot, kind: "dot" }; break; }
      if (node.dataset?.id) {
        const isAsset = node.tagName.toLowerCase() === "rect";
        hit = { ...hit, id: node.dataset.id, kind: isAsset ? "asset" : "space" };
        if (isAsset) break;
      }
    }
    st.tap.forEach((cb) => cb(hit));
  }

  const api = {
    get floor() { return st.floor; },
    get map() { return st.map; },
    get userMovedAt() { return st.moved; },
    kindAt,
    setFloor(floorId) {
      if (!floorId || floorId === st.floor) return api;
      st.floor = floorId;
      renderWorld(); renderFloors(); applyCam();
      st.floorCbs.forEach((cb) => cb(floorId));
      return api;
    },
    setMap(map) {
      st.map = map;
      if (!map.floors.some((f) => f.id === st.floor)) st.floor = defaultFloor();
      renderWorld(); renderFloors(); applyCam();
      return api;
    },
    drawRoute(route, { fromIndex = 0 } = {}) {
      st.route = route && route.points?.length ? route : null;
      st.fromIndex = fromIndex;
      renderFloors(); applyCam();
      return api;
    },
    setDots(dots) {
      const before = st.dots.length;
      const k = tweenK();
      const old = Object.fromEntries(st.dots.map((d) => {
        const f = st.prevPos[d.id];
        const h = st.prevHeading[d.id], heading_deg = h != null && d.heading_deg != null && k < 1 ? lerpDeg(h, d.heading_deg, k) : d.heading_deg;
        return [d.id, f && k < 1 ? { floor_id: d.floor_id, x: f.x + (d.x - f.x) * k, y: f.y + (d.y - f.y) * k, heading_deg } : { ...d, heading_deg }];
      }));
      st.dots = dots || [];
      st.prevPos = {};
      st.prevHeading = {};
      for (const d of st.dots) {
        const o = old[d.id];
        if (o && o.floor_id === d.floor_id && o.x != null && Math.hypot(o.x - d.x, o.y - d.y) < 25) st.prevPos[d.id] = { x: o.x, y: o.y };
        if (o?.heading_deg != null && d.heading_deg != null && Math.abs(lerpDeg(o.heading_deg, d.heading_deg, 1) - o.heading_deg) > 0.5) st.prevHeading[d.id] = o.heading_deg;
      }
      st.tweenStart = performance.now();
      if (opts.floorCounts || before !== st.dots.length) renderFloors();
      renderDots();
      return api;
    },
    highlight(ids) {
      st.hl = new Set(ids || []);
      renderWorld(); applyCam();
      return api;
    },
    setAssets(assets, classes = {}) {
      st.assets = assets || null;
      st.assetCls = classes;
      renderWorld(); applyCam();
      return api;
    },
    fit(target, ms = 350) {
      st.fitTarget = target;
      st.follow = null;
      const b = fitBounds(target);
      if (!b) return api;
      const cam = camFor(b, target === undefined ? 40 : 24);
      if (ms && st.sized) flyTo(cam, ms); else { cancelAnimationFrame(flight); st.cam = cam; applyCam(); }
      return api;
    },
    focus(x, y, z = st.cam.z, ms = 350) { flyTo({ x, y, z: clampZ(z) }, ms); return api; },
    follow(x, y, floorId, zoom, ms = 600) {
      const jump = !ms || !st.sized || (floorId && floorId !== st.floor);
      if (floorId) api.setFloor(floorId);
      st.follow = { x, y, z: clampZ(zoom ?? st.follow?.z ?? st.cam.z) };
      if (jump) { cancelAnimationFrame(flight); st.cam = { ...st.follow }; applyCam(); } else flyTo(st.follow, 600);
      return api;
    },
    setHeatmap(grid, { log = true } = {}) {
      st.heat = grid?.counts ? grid : null;
      st.heatHref = st.heat ? heatImage(st.heat, log) : null;
      renderWorld(); applyCam();
      return api;
    },
    onTap(cb) { st.tap.push(cb); return api; },
    onFloor(cb) { st.floorCbs.push(cb); return api; },
    refresh() { renderWorld(); renderFloors(); applyCam(); return api; },
  };

  st.floor = defaultFloor();
  renderWorld(); renderFloors();
  new ResizeObserver(() => {
    if (!container.clientWidth || !container.clientHeight) return;
    if (st.follow) { st.sized = true; cancelAnimationFrame(flight); st.cam = { ...st.follow }; applyCam(); }
    else if (!st.sized || !st.moved) { st.sized = true; api.fit(st.fitTarget, 0); } else applyCam();
  }).observe(container);
  return api;
}
