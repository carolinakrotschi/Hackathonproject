import { $, api, metres } from "/static/hospital/common.js";
import { createPlan, floorLabel } from "/static/hospital/plan.js";

const S = { map: null, plan: null, route: null, frame: null, timer: null, goal: "", status: "" };
const floorOf = (id) => S.map?.floors.find((f) => f.id === id);
const spaceOf = (id) => S.map?.spaces.find((s) => s.id === id);
const mapReady = api("/api/hospital/map").then((m) => {
  S.map = m;
  if (m.building?.name) $("#bname").textContent = m.building.name;
  return m;
}).catch(() => null);

function notice(text) {
  $("#notice").hidden = !text;
  $("#notice").textContent = text || "";
}

function status(iconName, text) {
  S.status = text;
  $("#status").innerHTML = `<span class="ms">${iconName}</span><span></span>`;
  $("#status").lastChild.textContent = text;
}

const into = (floor) => `to ${floorLabel(floor, true)}`;

function stopAnimation() {
  cancelAnimationFrame(S.frame);
  clearTimeout(S.timer);
}

function animate() {
  stopAnimation();
  const route = S.route, P = route?.points || [];
  if (P.length < 2) return;
  let total = 0;
  for (let i = 0; i < P.length - 1; i++) if (P[i].floor_id === P[i + 1].floor_id) total += Math.hypot(P[i + 1].x - P[i].x, P[i + 1].y - P[i].y);
  const speed = Math.max(2.5, total / 16);
  let i = 0, along = 0, pause = 0, last = null;
  const show = () => {
    const a = P[i], b = P[Math.min(i + 1, P.length - 1)];
    const len = a.floor_id === b.floor_id ? Math.hypot(b.x - a.x, b.y - a.y) : 0;
    const k = len ? along / len : 0;
    S.plan.setDots([{ id: "visitor", kind: "visitor", floor_id: a.floor_id, x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, label: "You" }]);
    S.plan.drawRoute(route, { fromIndex: i });
  };
  S.plan.setFloor(P[0].floor_id);
  S.plan.fit("route", 0);
  status("door_front", "Start at the main entrance");
  show();
  const step = (now) => {
    const dt = last == null ? 0 : Math.min(0.1, (now - last) / 1000);
    last = now;
    if (pause > 0) pause -= dt;
    else {
      let rest = speed * dt;
      while (rest > 0 && i < P.length - 1) {
        const a = P[i], b = P[i + 1];
        if (a.floor_id !== b.floor_id) {
          while (i < P.length - 1 && P[i].floor_id !== P[i + 1].floor_id) i++;
          const kind = S.plan.kindAt(a);
          const target = floorOf(P[i].floor_id);
          S.plan.setFloor(P[i].floor_id);
          S.plan.fit("route");
          status(kind === "stairs" ? "stairs" : "elevator", `${kind === "stairs" ? "Take the stairs" : "Take the lift"} ${into(target)}`);
          along = 0; pause = 1.6; rest = 0;
          break;
        }
        const len = Math.hypot(b.x - a.x, b.y - a.y);
        if (along + rest < len) { along += rest; rest = 0; } else { rest -= len - along; along = 0; i++; }
      }
      if (pause <= 0 && !S.status.startsWith("Walk") && i < P.length - 1) status("directions_walk", `Walk towards ${S.goal}`);
    }
    show();
    if (i >= P.length - 1) { status("location_on", `Arrived: ${S.goal}`); return; }
    S.frame = requestAnimationFrame(step);
  };
  S.timer = setTimeout(() => { S.frame = requestAnimationFrame(step); }, 700);
}

function asRoom(room) {
  if (!room) return {};
  return typeof room === "string" ? { id: room, ...(spaceOf(room) || { name: room }) } : room;
}

function asFloor(floor, room) {
  const f = typeof floor === "string" ? floorOf(floor) || { name: floor } : floor || floorOf(spaceOf(room.id)?.floor_id);
  if (!f) return "";
  return f.level != null ? floorLabel(f, true) : f.name || "";
}

async function lookup(patient, code) {
  stopAnimation();
  notice("");
  $("#submit").disabled = true;
  try {
    const r = await api(`/api/hospital/visit?patient=${encodeURIComponent(patient)}&code=${encodeURIComponent(code)}`);
    const map = await mapReady;
    if (!map) throw new Error("map");
    const p = r.patient || {}, room = asRoom(p.room);
    S.goal = room.name || room.ref || "Room";
    $("#rRoom").textContent = room.ref ? `Room ${room.ref}` : room.name || "Room";
    $("#rName").textContent = [p.name, room.ref && room.name ? room.name : ""].filter(Boolean).join(" · ");
    $("#rWard").textContent = typeof p.ward === "string" ? p.ward : p.ward?.name || "–";
    $("#rFloor").textContent = asFloor(p.floor, room) || "–";
    const walk = r.route?.walk_m, mins = r.route?.minutes;
    $("#rWalk").textContent = walk != null ? `${metres(walk)}${mins != null ? ` · ${Math.max(1, Math.round(mins))} min` : ""}` : "–";
    $("#rHours").textContent = r.visiting_hours ? `Visiting hours ${r.visiting_hours}` : "Please ask the ward for visiting hours";
    $("#rSay").hidden = !r.say;
    $("#rSay").textContent = r.say || "";
    $("#result").hidden = false;
    S.plan ||= createPlan($("#plan"), map, { dotTween: 0, pad: 28 });
    S.route = r.route;
    S.plan.highlight(room.id ? [room.id] : []);
    animate();
    $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    $("#result").hidden = true;
    notice(e.status === 403 || e.status === 404 ? "Not found or wrong code." : "Currently unavailable. Please try again later.");
  } finally {
    $("#submit").disabled = false;
  }
}

$("#form").addEventListener("submit", (e) => {
  e.preventDefault();
  const patient = $("#patient").value.trim(), code = $("#code").value.trim();
  if (patient && code) lookup(patient, code);
});
$("#replay").addEventListener("click", animate);

const q = new URLSearchParams(location.search);
if (q.get("patient")) $("#patient").value = q.get("patient");
if (q.get("code")) $("#code").value = q.get("code");
if (q.get("patient") && q.get("code")) lookup(q.get("patient"), q.get("code"));
