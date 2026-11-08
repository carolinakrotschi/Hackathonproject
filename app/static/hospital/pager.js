import { api, esc, hhmm, metres, speak, stopSpeaking, poll, toMs } from "/static/hospital/common.js";
import { createPlan, floorLabel, MANEUVER } from "/static/hospital/plan.js";

const query = new URLSearchParams(location.search);
const segments = location.pathname.split("/").filter(Boolean);
const PAGER = decodeURIComponent(segments[0] === "hospital" && segments[1] === "pager" && segments[2] ? segments[2] : query.get("id") || "");
const SPEAK = query.get("speak") === "1";
const OUTLINE = query.get("frame") !== "0";
const DEBUG = query.get("debug") === "1";
const size = (v, d) => Math.max(200, Math.min(1200, Number(v) || d));
// Framed desktop preview = the Z Flip7 cover screen in CSS px (948 × 1048 at DPR 3), ?w=&h= to try others.
const STAGE = { w: size(query.get("w"), 316), h: size(query.get("h"), 349) };

const TYPE_LABEL = {
  bed: "Bed", stretcher: "Stretcher", wheelchair: "Wheelchair", infusion_pump: "Infusion pump", coffee_machine: "Coffee machine",
  vending_machine: "Vending machine", defibrillator: "Defibrillator", ultrasound: "Ultrasound", monitor: "Monitor", walker: "Walker",
};
const STOP = { coffee: ["local_cafe", "Coffee"], toilet: ["wc", "Toilet"], kiosk: ["storefront", "Kiosk"], pharmacy: ["local_pharmacy", "Pharmacy"] };
const FIT = [["heroBig", 1, 20], ["heroSub", 3, 14]];
// Shown over the map: what is being heard, what is being worked on, and what went wrong. A command that went
// through needs no text: the screen changes.
const VOICE_KIND = { graphic_eq: "listening", record_voice_over: "working", mic_off: "problem", error: "problem", help: "problem" };
const MAP_MIN = 0.15; // of the screen height; below that the texts shrink, the instruction last
const SHOW_MS = 8000; // how long a found place stays in view before the map follows you again
const AHEAD_M = 2.5;
// The position arrives in steps (a walk tick, a poll); the dot and the camera glide between them at an even pace,
// taking as long as the steps are apart.
const GLIDE_MIN_MS = 150, GLIDE_MAX_MS = 1200, GLIDE_JUMP_M = 25;

const $ = (id) => document.getElementById(id);
const S = { map: null, plan: null, skew: 0, heroKey: null, cam: null, hl: "", chips: "", lastSay: null, stop: null,
  voice: null, rec: null, heading: null, lastPos: null, fitKey: null, fitQueued: false, seen: {} };
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

const nowMs = () => Date.now() + S.skew;

// Every browser showing this pager shows the same screen: the status line, a found place and the demo walk are
// published to the server, and the newest one wins (demo only, no conflict handling).
function publish(key, value) {
  value = { ...value, at: nowMs() };
  S.seen[key] = value.at;
  api(`/api/hospital/pagers/${encodeURIComponent(PAGER)}/screen`, { method: "POST", body: { [key]: value } }).catch(() => {});
  return value;
}

function adopt(screen = {}) {
  const fresh = (key) => screen[key] && screen[key].at > (S.seen[key] ?? 0) && (S.seen[key] = screen[key].at);
  if (fresh("voice")) S.voice = screen.voice;
  if (fresh("show")) { S.show = screen.show.ids ? screen.show : null; S.map = null; }
  if (fresh("walk") && !screen.walk.on && S.walking) { clearInterval(S.walking); S.walking = null; }
}
const icon = (m) => (MANEUVER[m] || MANEUVER.straight)[0];
const distance = (m) => (m == null ? "" : m < 3 ? "Now" : metres(m));
const deg = (dx, dy) => (Math.atan2(dy, dx) * 180) / Math.PI;

function left(t) {
  const s = Math.max(0, Math.round((toMs(t) - nowMs()) / 1000));
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")} h` : `${m}:${String(s % 60).padStart(2, "0")}`;
}

function setText(id, text) {
  const el = $(id);
  if (el.textContent !== text) el.textContent = text;
}

function fitText() {
  S.fitQueued = false;
  const items = FIT.map(([id, lines, min]) => ({ el: $(id), lines, min })).filter((it) => it.el.getClientRects().length && it.el.textContent);
  const key = `${innerWidth}x${innerHeight}|${$("pg").dataset.state}|${items.map((it) => `${it.el.id}:${it.el.textContent}`).join("|")}`;
  if (key === S.fitKey) return;
  S.fitKey = key;
  for (const it of FIT) $(it[0]).style.fontSize = "";
  for (const it of items) {
    const cs = getComputedStyle(it.el);
    it.fs = parseFloat(cs.fontSize);
    it.lh = parseFloat(cs.lineHeight) / it.fs || 1.2;
  }
  const set = (it, fs) => { it.fs = fs; it.el.style.fontSize = `${fs}px`; };
  const wide = (it) => it.el.scrollWidth > it.el.clientWidth + 1;
  const rows = (it) => Math.round(it.el.offsetHeight / (it.fs * it.lh));
  for (const it of items) {
    const base = it.fs;
    while (it.fs > it.min && (wide(it) || rows(it) > it.lines)) set(it, Math.max(it.min, it.fs - 1));
    if (it.lines < 2 || rows(it) <= it.lines) continue;
    const n = rows(it);
    while (it.fs + 1 <= base) {
      set(it, it.fs + 1);
      if (rows(it) > n || wide(it)) { set(it, it.fs - 1); break; }
    }
  }
  const map = $("pg").querySelector(".map");
  const mapMin = Math.max(52, $("pg").offsetHeight * MAP_MIN);
  while (map.getClientRects().length && map.clientHeight < mapMin) {
    const shrink = items.filter((i) => i.lines > 1 && i.fs > i.min);
    const it = (shrink.some((i) => i.el.id !== "heroSub") ? shrink.filter((i) => i.el.id !== "heroSub") : shrink)
      .sort((a, b) => b.fs / b.min - a.fs / a.min)[0];
    if (!it) break;
    set(it, Math.max(it.min, it.fs - 1));
  }
}

function queueFit() {
  if (S.fitQueued) return;
  S.fitQueued = true;
  requestAnimationFrame(fitText);
}

function placeName(goal) {
  if (!goal) return "";
  return S.map?.spaces.find((s) => s.id === goal.space_id)?.name || goal.name || "";
}

function modeOf(g) {
  if (g.status === "idle" || (g.mode === "idle" && !g.route)) return "idle";
  return g.mode || (g.search ? "search" : g.appointment ? "appointment" : "return");
}

function searchLabel(g, goal) {
  const s = g.search || {};
  const what = TYPE_LABEL[s.type] || s.label || s.name || s.type || "Item";
  return goal && goal !== what ? `${what} – ${goal}` : what;
}

function liftHint(g) {
  if (g.status !== "wait" && g.status !== "go") return "";
  const slots = g.lift_slots?.length ? g.lift_slots : g.route?.lift_slots || [];
  const slot = slots.find((s) => toMs(s.end ?? s.start) > nowMs());
  if (!slot) return "";
  const name = S.map?.connectors?.find((c) => c.id === slot.connector_id)?.name || "Lift";
  return `${name} at ${hhmm(slot.start)}`;
}

function view(g) {
  const turns = g.next_turns || [];
  const mode = modeOf(g);
  const goal = placeName(g.goal);
  const turn = (t, lbl) => (t ? { lbl, icon: icon(t.maneuver), txt: t.text || MANEUVER[t.maneuver]?.[1] || "", dist: distance(t.distance_m) } : null);

  if (mode === "idle") {
    return { state: "idle", mode, icon: "mic", big: "Ready", sub: "Hold the mic and speak", then: null,
      status: { icon: "record_voice_over", txt: "Say “take me to CT” or “where is the MRI?”" } };
  }
  if (g.status === "arrived") {
    return { state: "arrived", mode, icon: "task_alt", big: "Arrived", sub: goal, then: null,
      status: { icon: "where_to_vote", txt: `Arrived: ${goal || "Destination"}` } };
  }
  if (g.status === "wait") {
    const at = g.depart_at != null ? hhmm(g.depart_at) : "";
    return { state: "wait", mode, icon: "hourglass_top", big: at ? left(g.depart_at) : "Please wait",
      sub: mode === "search" ? `Please wait · Finding: ${searchLabel(g, goal)}` : at ? "Please wait" : goal,
      then: turn(turns[0], "First"),
      status: { icon: "schedule", txt: at ? `Please wait – leave at ${at}` : "Please wait", count: at ? left(g.depart_at) : "" } };
  }
  const first = turns[0];
  const eta = g.eta != null ? `ETA ${hhmm(g.eta)}` : "";
  const status = mode === "search" ? { icon: "search", txt: `Finding: ${searchLabel(g, goal)}` }
    : mode === "appointment" ? { icon: "event", txt: `Appointment: ${goal}${g.appointment?.start != null ? ` at ${hhmm(g.appointment.start)}` : ""}` }
    : mode === "return" ? { icon: "home", txt: `Way back: ${goal}` }
    : { icon: "near_me", txt: `On the way: ${goal}` };
  return {
    state: "go", mode,
    icon: first ? icon(first.maneuver) : "location_on",
    big: first ? distance(first.distance_m) || "Now" : "Almost there",
    sub: first ? first.text || MANEUVER[first.maneuver]?.[1] || "" : goal,
    then: turn(turns[1], "Then"),
    status: { ...status, count: eta },
  };
}

function replay(el) {
  el.classList.remove("enter");
  void el.offsetWidth;
  el.classList.add("enter");
}

function renderText(g) {
  const v = view(g);
  const pg = $("pg");
  pg.dataset.state = v.state;
  pg.dataset.mode = v.mode;

  const heroKey = `${v.state}|${v.icon}|${v.sub}`;
  if (heroKey !== S.heroKey) { S.heroKey = heroKey; replay($("hero")); }
  setText("heroIcon", v.icon);
  setText("heroBig", v.big);
  setText("heroSub", v.sub);

  showVoice();

  const lift = liftHint(g);
  const stops = g.status === "arrived" || v.state === "idle" ? [] : g.stops || [];
  const chips = [
    g.step_free && v.state === "go" ? `<span class="chip"><span class="ms">accessible</span>Step-free</span>` : "",
    lift ? `<span class="chip lift"><span class="ms">elevator</span>${esc(lift)}</span>` : "",
    ...stops.map((s) => {
      const [ic, label] = STOP[s.category] || ["flag", "Stop"];
      return `<span class="chip"><span class="ms">${ic}</span>${esc(s.name || label)}</span>`;
    }),
  ].join("");
  if (chips !== S.chips) { S.chips = chips; $("chips").innerHTML = chips; }
  queueFit();
  document.title = `Pager · ${g.patient?.name || PAGER}`;
}

function routeHeading(points, p) {
  let best = null;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    if (a.floor_id !== p.floor_id || b.floor_id !== p.floor_id) continue;
    const dx = b.x - a.x, dy = b.y - a.y, len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2)) : 0;
    const d = Math.hypot(a.x + dx * t - p.x, a.y + dy * t - p.y);
    if (!best || d < best.d - 1e-6) best = { i, d, x: a.x + dx * t, y: a.y + dy * t };
  }
  if (!best) return null;
  let budget = AHEAD_M, x = best.x, y = best.y, tx = x, ty = y, last = null;
  for (let j = best.i; j < points.length - 1 && budget > 0; j++) {
    const a = points[j], b = points[j + 1];
    if (a.floor_id !== p.floor_id || b.floor_id !== p.floor_id) break;
    if (Math.hypot(b.x - a.x, b.y - a.y) > 0.05) last = [b.x - a.x, b.y - a.y];
    const seg = Math.hypot(b.x - x, b.y - y);
    const k = seg > budget ? budget / seg : 1;
    tx = x + (b.x - x) * k; ty = y + (b.y - y) * k;
    budget -= seg; x = b.x; y = b.y;
  }
  if (Math.hypot(tx - p.x, ty - p.y) >= 0.5) return deg(tx - p.x, ty - p.y);
  return last ? deg(...last) : null;
}

function headingFor(route, p) {
  const h = route ? routeHeading(route.points, p) : null;
  const last = S.lastPos;
  const moved = last && last.floor_id === p.floor_id ? Math.hypot(p.x - last.x, p.y - last.y) : Infinity;
  if (h != null) S.heading = h;
  else if (last && moved >= 0.5 && moved < Infinity) S.heading = deg(p.x - last.x, p.y - last.y);
  if (moved >= 0.5) S.lastPos = { floor_id: p.floor_id, x: p.x, y: p.y };
  return S.heading;
}

function renderMap(g) {
  const plan = S.plan;
  if (!plan) return;
  const active = g.status !== "arrived" && modeOf(g) !== "idle";
  const route = active && g.route?.points?.length ? g.route : null;
  const p = g.position?.floor_id && g.position.x != null ? g.position : route?.points[0] || null;
  if (p && !S.map.floors.some((f) => f.id === p.floor_id)) { S.map = null; return; }

  // The server tracks how far along the route the pager is; matching the nearest point here would skip ahead
  // where the route passes itself (into a stop and back out), hiding the whole line behind that point.
  S.route = route;
  if (p) glideTo(p, headingFor(route, p), route ? g.progress ?? 0 : 0);
  else { S.glide = null; plan.setDots([]); plan.drawRoute(route); }
  const showing = S.show && S.show.until > nowMs() ? S.show : null;
  if (!showing && S.show) { S.show = null; S.cam = null; }
  const hl = [route ? g.goal?.space_id : null, g.search?.asset_id, ...(showing?.ids || [])].filter(Boolean).join(",");
  if (hl !== S.hl) { S.hl = hl; plan.highlight(hl ? hl.split(",") : []); }

  const box = $("plan");
  const zoom = Math.max(5, Math.min(18, box.clientWidth / 34));
  const shown = showing && S.map.spaces.find((s) => s.id === showing.space);
  if (shown) {
    // What the agent found ("where is the MRI?") is shown for a moment, then the map follows you again.
    if (S.cam !== `show:${shown.id}`) {
      S.cam = `show:${shown.id}`;
      plan.setFloor(shown.floor_id);
      plan.fit({ space_id: shown.id });
    }
  } else if (p) {
    S.cam = { follow: true, zoom }; // glideFrame keeps the camera on the dot
    queueGlide();
  } else if (S.cam !== "fit") {
    S.cam = "fit";
    plan.fit();
  }
  const floor = S.map.floors.find((f) => f.id === plan.floor);
  $("floor").hidden = !floor;
  const here = shown || S.map.spaces.find((s) => s.id === g.position?.space_id);
  const building = S.map.buildings?.find((b) => b.id === here?.building)?.name;
  if (floor) setText("floor", building ? `${building} · ${floorLabel(floor, true)}` : floorLabel(floor, true));
}

function glideAt(now) {
  const { from, to, start, dur } = S.glide;
  const k = Math.min(1, (now - start) / dur);
  const turn = from.heading != null && to.heading != null ? ((to.heading - from.heading + 540) % 360) - 180 : null;
  return { k, floor_id: to.floor_id, x: from.x + (to.x - from.x) * k, y: from.y + (to.y - from.y) * k,
    heading: turn == null ? to.heading : from.heading + turn * k,
    progress: to.progress >= from.progress ? from.progress + (to.progress - from.progress) * k : to.progress };
}

function glideTo(p, heading, progress) {
  const now = performance.now();
  const here = S.glide ? glideAt(now) : null;
  const to = { floor_id: p.floor_id, x: p.x, y: p.y, heading, progress };
  const last = S.glide?.to;
  if (last && last.floor_id === to.floor_id && last.x === to.x && last.y === to.y && last.heading === heading
      && last.progress === progress) { queueGlide(); return; }
  // a lift ride, a reset or a new route (progress starts again) is shown at once
  const jump = !here || here.floor_id !== to.floor_id || Math.hypot(here.x - to.x, here.y - to.y) > GLIDE_JUMP_M
    || progress < here.progress;
  const dur = Math.max(GLIDE_MIN_MS, Math.min(GLIDE_MAX_MS, now - (S.movedAt ?? -Infinity)));
  S.movedAt = now;
  S.glide = { from: jump ? to : here, to, start: now, dur };
  queueGlide();
}

function queueGlide() {
  if (!S.glideRaf) S.glideRaf = requestAnimationFrame(glideFrame);
}

function glideFrame() {
  S.glideRaf = null;
  if (!S.glide || !S.plan) return;
  const at = glideAt(performance.now());
  S.plan.drawRoute(S.route, { fromIndex: at.progress });
  S.plan.setDots([at.heading == null
    ? { id: "me", kind: "me", floor_id: at.floor_id, x: at.x, y: at.y, color: "#ffffff" }
    : { id: "me", kind: "me", floor_id: at.floor_id, x: at.x, y: at.y, heading_deg: at.heading, color: "#2f80ff" }]);
  if (S.cam?.follow) S.plan.follow(at.x, at.y, at.floor_id, S.cam.zoom, 0);
  if (at.k < 1) queueGlide();
}

async function loadMap() {
  try {
    S.map = await api("/api/hospital/map");
  } catch {
    return;
  }
  if (S.plan) S.plan.setMap(S.map);
  else S.plan = createPlan($("plan"), S.map, { interactive: false, controls: false, floorButtons: false, labels: "minimal", pad: 12, dotTween: 0 });
  S.cam = null;
}

function fatal(title, sub) {
  S.stop?.();
  $("pg").dataset.state = "error";
  setText("heroIcon", "link_off");
  setText("heroBig", title);
  setText("heroSub", `${sub} (${PAGER ? `Pager ID: ${PAGER}` : "Address: /hospital/pager/<ID>"})`);
  queueFit();
}

function showVoice() {
  if (S.voice?.until != null && S.voice.until < nowMs()) S.voice = null;
  $("pg").classList.toggle("listening", S.voice?.icon === "graphic_eq");
  const shown = S.voice && VOICE_KIND[S.voice.icon] && S.voice.txt ? S.voice : null;
  $("voice").hidden = !shown;
  if (!shown) return;
  $("voice").dataset.kind = VOICE_KIND[shown.icon];
  setText("voiceIcon", shown.icon);
  setText("voiceTxt", shown.txt);
}

function voiceStatus(icon, txt, ms) {
  S.voice = publish("voice", { icon, txt, until: ms ? nowMs() + ms : null });
  showVoice();
}

async function send(text) {
  voiceStatus("record_voice_over", `“${text}”`, 30000); // until the answer comes
  try {
    const r = await api(`/api/hospital/pagers/${encodeURIComponent(PAGER)}/voice`, { method: "POST", body: { text } });
    speak(r.say);
    const ids = [...(r.view?.spaces || []), ...(r.view?.assets || [])];
    S.show = ids.length ? publish("show", { ids, space: r.view.spaces[0], until: nowMs() + SHOW_MS }) : null;
    if (ids.length) S.map = null; // a label, door, closure or move may have changed the plan: reload it
    // Always show the reply: speech may be muted or unsupported, and "where am I?" changes nothing else on screen.
    voiceStatus(r.ok ? "chat" : "help", r.say, r.ok ? 10000 : 8000);
  } catch (e) {
    voiceStatus("error", e.message, 5000);
    speak("Sorry, that didn't work.");
  }
  await refresh();
}

// Push-to-talk: the mic stays open while the button or Space is held. Browsers end recognition on their own
// after a pause, so while the user still holds, a new session starts and its words are appended.
const TAP_MS = 300;

function startListening() {
  if (S.rec) return;
  if (!Recognition) {
    const text = prompt("This browser doesn't support speech input. What would you like to say?");
    if (text?.trim()) send(text.trim());
    return;
  }
  stopSpeaking();
  S.holding = true;
  S.pressedAt = Date.now();
  S.heard = [];
  $("pg").classList.add("listening");
  voiceStatus("graphic_eq", "Listening…");
  session();
}

function session() {
  const rec = new Recognition();
  rec.lang = "en-US";
  rec.interimResults = true;
  rec.continuous = true;
  const part = S.heard.push("") - 1;
  rec.onresult = (e) => {
    S.heard[part] = [...e.results].map((r) => r[0].transcript).join(" ").trim();
    voiceStatus("graphic_eq", `“${S.heard.join(" ").trim()}”`);
  };
  rec.onerror = (e) => {
    if (e.error === "no-speech" || e.error === "aborted") return;
    S.holding = false;
    voiceStatus("mic_off", e.error === "not-allowed" ? "Microphone not allowed" : "Didn't catch that – please try again", 4000);
  };
  rec.onend = () => {
    if (S.holding) { session(); return; }
    S.rec = null;
    $("pg").classList.remove("listening");
    const text = S.heard.join(" ").replace(/\s+/g, " ").trim();
    if (text) send(text);
    else if (S.voice?.icon === "graphic_eq") voiceStatus("graphic_eq", "", 1);
  };
  S.rec = rec;
  rec.start();
}

function stopListening() {
  S.holding = false;
  S.rec?.stop();
}

// A long press is push-to-talk; a short tap leaves the mic on until the next tap.
function press() {
  if (S.rec) { stopListening(); S.tapped = true; return; }
  S.tapped = false;
  startListening();
}

function release() {
  if (S.tapped || !S.rec) return;
  if (Date.now() - S.pressedAt >= TAP_MS) stopListening();
}

async function refresh() {
  let g;
  try {
    g = await api(`/api/hospital/pagers/${encodeURIComponent(PAGER)}/guidance`);
  } catch (e) {
    if (e.status === 404) { fatal("Unknown pager", "This pager is not registered in the system."); return; }
    $("offline").hidden = false;
    if (!S.heroKey) { setText("heroBig", "Offline"); setText("heroSub", "No connection – retrying…"); queueFit(); }
    return;
  }
  $("offline").hidden = true;
  if (g.now != null) S.skew = toMs(g.now) - Date.now();
  adopt(g.screen);
  S.walkOn = !!g.screen?.walk?.on;
  if (!S.map) await loadMap();
  renderText(g);
  if (S.map) renderMap(g);
  // The countdown "In 18 metres" changes every step; speak again only when the instruction itself changes.
  const sayKey = g.say?.replace(/\d+ metres/g, "# metres");
  if (SPEAK && sayKey && sayKey !== S.lastSay) speak(g.say);
  S.lastSay = sayKey ?? S.lastSay;
}

// Demo: L walks this pager along its route (we can't really go), L again stops; it stops by itself on arrival.
const WALK_STEP_M = 2, WALK_TICK_MS = 400;

function toggleWalk() {
  if (S.walking || S.walkOn) { stopWalk("Simulated walk stopped"); return; }
  S.walkOn = true;
  publish("walk", { on: true });
  voiceStatus("directions_walk", "Simulated walk – press L to stop");
  S.walking = setInterval(async () => {
    if (S.stepping) return; // a slow connection must not stack up steps
    S.stepping = true;
    try {
      const g = await api(`/api/hospital/pagers/${encodeURIComponent(PAGER)}/walk`, { method: "POST", body: { metres: WALK_STEP_M } });
      if (g.status === "arrived" || g.status === "idle") stopWalk(g.status === "arrived" ? "Arrived" : "No destination to walk to");
      await refresh();
    } catch (e) {
      stopWalk(e.message);
    } finally {
      S.stepping = false;
    }
  }, WALK_TICK_MS);
}

function stopWalk(message) {
  clearInterval(S.walking);
  S.walking = null;
  S.walkOn = false;
  publish("walk", { on: false });
  voiceStatus("directions_walk", message, 3000);
}

// Demo: R (or the Start button) is a clean slate for the next test: back at the start point, no route, nothing
// highlighted, closures reopened. The map stays empty until the next request.
async function resetWalk() {
  if (S.walking || S.walkOn) stopWalk("Resetting…");
  for (let i = 0; S.stepping && i < 50; i++) await new Promise((r) => setTimeout(r, 50)); // let a step in flight land first
  stopSpeaking();
  try {
    const g = await api(`/api/hospital/pagers/${encodeURIComponent(PAGER)}/demo-reset`, { method: "POST" });
    S.lastSay = g.say?.replace(/\d+ metres/g, "# metres"); // don't announce "No active destination"
    S.show = publish("show", {});
    S.cam = null;
    S.hl = "";
    S.map = null; // closures are gone: redraw the plan
    voiceStatus("restart_alt", "Reset – say where to go", 3000);
  } catch (e) {
    voiceStatus("error", e.message, 4000);
  }
  await refresh();
}

// ?cam=55x20 sets the camera cut-out (percent of width x height, bottom-right), ?cam=55x20l puts it bottom-left, ?cam=0 turns it off.
function camZone() {
  const m = /^(\d+(?:\.\d+)?)(?:x(\d+(?:\.\d+)?))?([lr])?$/.exec(query.get("cam") || "");
  if (!m) return;
  const w = Math.min(90, Number(m[1])), h = m[2] == null ? (w ? 20 : 0) : Math.min(60, Number(m[2]));
  const root = document.documentElement;
  root.style.setProperty("--cam-w", String(w));
  root.style.setProperty("--cam-h", String(h));
  if (m[3]) root.dataset.camSide = m[3];
}

function showDebug() {
  if (!DEBUG) return;
  const pg = $("pg"), cams = $("cams"), css = getComputedStyle(document.documentElement);
  $("debug").hidden = false;
  $("debug").textContent = [
    `window ${innerWidth} × ${innerHeight}`,
    `screen ${screen.width} × ${screen.height}`,
    `dpr ${devicePixelRatio}  → ${Math.round(innerWidth * devicePixelRatio)} × ${Math.round(innerHeight * devicePixelRatio)} px`,
    `page ${pg.offsetWidth} × ${pg.offsetHeight}`,
    `cam ${css.getPropertyValue("--cam-w").trim()}% × ${css.getPropertyValue("--cam-h").trim()}% ${document.documentElement.dataset.camSide === "l" ? "left" : "right"} = ${cams.offsetWidth} × ${cams.offsetHeight}`,
  ].join("\n");
}

function fitStage() {
  const w = innerWidth, h = innerHeight;
  const framed = w >= 560 && h >= 620;
  const pg = $("pg");
  document.body.classList.toggle("framed", framed);
  document.body.classList.toggle("outline", framed && OUTLINE);
  document.body.classList.toggle("debug", DEBUG);
  for (const [k, v] of [["--pg-w", STAGE.w], ["--pg-h", STAGE.h]]) {
    if (framed) pg.style.setProperty(k, `${v}px`); else pg.style.removeProperty(k);
  }
  const pad = OUTLINE ? 120 : 64;
  const scale = framed ? Math.max(1, Math.min(3, (w - pad) / STAGE.w, (h - pad) / STAGE.h)) : 1;
  document.documentElement.style.setProperty("--scale", scale.toFixed(3));
  showDebug();
  queueFit();
}

camZone();
fitStage();
addEventListener("resize", fitStage);
document.fonts?.ready.then(() => { S.fitKey = null; queueFit(); });
$("reset").addEventListener("click", resetWalk);
const mic = $("mic");
mic.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  mic.setPointerCapture(e.pointerId);
  press();
});
mic.addEventListener("pointerup", release);
mic.addEventListener("pointercancel", release);
mic.addEventListener("contextmenu", (e) => e.preventDefault());
addEventListener("keydown", (e) => {
  const plain = !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey;
  if (e.code === "KeyL" && plain) { toggleWalk(); return; }
  if (e.code === "KeyR" && plain) { resetWalk(); return; }
  if (e.code !== "Space") return;
  e.preventDefault();
  if (!e.repeat) press();
});
addEventListener("keyup", (e) => {
  if (e.code !== "Space") return;
  e.preventDefault();
  release();
});
// /hospital/pager without an ID: pick one of the staff pagers.
async function choosePager() {
  $("pg").dataset.state = "idle";
  setText("heroIcon", "badge");
  setText("heroBig", "Choose pager");
  setText("heroSub", "Tap a name to open that pager");
  $("mic").hidden = true;
  queueFit();
  let map;
  try { map = await api("/api/hospital/map"); } catch { return; }
  const pagers = (map.staff || []).filter((s) => s.pager_id);
  $("chips").innerHTML = `<nav class="pick">${pagers.map((s) => `<a href="/hospital/pager/${encodeURIComponent(s.pager_id)}${location.search}">
    <span class="ms">badge</span><b>${esc(s.label)}</b><small>${esc(s.role || s.pager_id)}</small></a>`).join("")}</nav>`;
}

if (!PAGER) choosePager();
else {
  S.stop = poll(refresh, 1000);
  // ?ask=… sends a request on open (several ask=… one after the other), ?reset=1 resets the pager first:
  // scripted demos, docs/route-tests.md and screenshots without a microphone
  const asks = query.getAll("ask");
  if (asks.length || query.get("reset")) setTimeout(async () => {
    if (query.get("reset")) await resetWalk();
    for (const text of asks) await send(text);
  }, 800);
}
