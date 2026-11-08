export const $ = (sel, root = document) => root.querySelector(sel);
export const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(typeof data?.detail === "string" ? data.detail : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const toMs = (t) => (t == null ? null : t > 1e12 ? t : t * 1000);

export function hhmm(t) {
  const ms = toMs(t);
  return ms == null ? "–" : new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

export function countdown(t) {
  const s = Math.max(0, Math.round((toMs(t) - Date.now()) / 1000));
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")} h` : `${m}:${String(s % 60).padStart(2, "0")}`;
}

export const metres = (m) => (m == null ? "" : m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);
export const minutes = (min) => (min == null ? "" : `${Math.max(1, Math.round(min))} min`);

let toastTimer = null;
export function toast(message, kind = "") {
  let el = document.getElementById("toast");
  if (!el) {
    el = Object.assign(document.createElement("div"), { id: "toast", className: "toast" });
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 3200);
}

let audio = null, spoken = 0;

export function stopSpeaking() {
  spoken++;
  audio?.pause();
  audio = null;
  window.speechSynthesis?.cancel();
}

// Fish Audio via the server; the browser voice if that fails or autoplay is blocked.
export async function speak(text) {
  if (!text) return;
  stopSpeaking();
  const mine = spoken;
  try {
    const r = await fetch(`/api/hospital/tts?text=${encodeURIComponent(text.slice(0, 500))}`);
    if (!r.ok) throw new Error(r.status);
    const blob = await r.blob();
    if (mine !== spoken) return; // a newer message replaced this one
    audio = new Audio(URL.createObjectURL(blob));
    audio.onended = () => URL.revokeObjectURL(audio.src);
    await audio.play();
  } catch {
    if (mine === spoken) browserSpeak(text);
  }
}

function browserSpeak(text) {
  if (!window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  const voice = speechSynthesis.getVoices().find((v) => v.lang?.toLowerCase().startsWith("en"));
  if (voice) u.voice = voice;
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
}

export function poll(fn, ms) {
  let stopped = false, timer = null;
  const tick = async () => {
    if (stopped) return;
    try { await fn(); } catch { /* next tick retries */ }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

export const asList = (data, key) => (Array.isArray(data) ? data : data?.[key] || []);
