const PRESETS = window.PEW_PRESETS || [];
let speed = 6;

const toast = document.getElementById("toast");
let toastTimer = null;
function flash(msg) {
  toast.textContent = msg;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 1200);
}

async function post(path) {
  try {
    const r = await fetch(path, { method: "POST", credentials: "same-origin" });
    const j = await r.json().catch(() => ({}));
    if (r.status === 409 && j.foreground_process !== undefined) {
      flash("⚠ Zoom not focused (" + (j.foreground_process || "unknown") + ")");
    } else if (!r.ok) {
      flash("⚠ " + (j.error || r.statusText));
    }
    return j;
  } catch (e) {
    flash("⚠ network");
  }
}

// ---- hold-to-move for D-pad ----
function bindHold(btn, onStart, onEnd) {
  let active = false;
  const start = (e) => {
    e.preventDefault();
    if (active) return;
    active = true;
    onStart();
  };
  const end = (e) => {
    if (!active) return;
    active = false;
    onEnd();
  };
  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerup", end);
  btn.addEventListener("pointerleave", end);
  btn.addEventListener("pointercancel", end);
}

document.querySelectorAll(".dpad button[data-dir]").forEach(btn => {
  const dir = btn.dataset.dir;
  bindHold(btn,
    () => post(`/ptz/move/${dir}?speed=${speed}`),
    () => post(`/ptz/stop`));
});

document.querySelectorAll("button[data-zoom]").forEach(btn => {
  const action = btn.dataset.zoom;
  bindHold(btn,
    () => post(`/zoom/${action}`),
    () => post(`/zoom/stop`));
});

// "HOME" = jump to preset 1 (Speaker), the most useful default framing
document.getElementById("homeBtn").addEventListener("click", () => post("/preset/recall/1"));

document.querySelectorAll(".mode button[data-speed]").forEach(btn => {
  btn.addEventListener("click", () => {
    speed = parseInt(btn.dataset.speed, 10);
    document.querySelectorAll(".mode button[data-speed]")
      .forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  });
});

// ---- presets ----
const presetsEl = document.getElementById("presets");
PRESETS.forEach((name, i) => {
  const b = document.createElement("button");
  b.textContent = name;
  const slot = i + 1;
  b.addEventListener("click", async () => {
    await post(`/preset/recall/${slot}`);
    flash(name);
  });
  presetsEl.appendChild(b);
});

// ---- zoom meeting ----
document.getElementById("airBtn").addEventListener("click", async () => {
  const j = await post("/zoom_meeting/toggle_air");
  if (j && j.status) flash("Toggled video + mic");
  refreshZoomStatus();
});
document.getElementById("vidBtn").addEventListener("click", async () => {
  const j = await post("/zoom_meeting/toggle_video");
  if (j && j.status) flash("Toggled video");
  refreshZoomStatus();
});
document.getElementById("micBtn").addEventListener("click", async () => {
  const j = await post("/zoom_meeting/toggle_mic");
  if (j && j.status) flash("Toggled mic");
  refreshZoomStatus();
});

// ---- zoom state polling ----
const zoomStatusEl = document.getElementById("zoomStatus");
function esc(text) {
  const d = document.createElement("div");
  d.textContent = String(text);
  return d.innerHTML;
}
function fmtTime(epoch) {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
const airBtnEl = document.getElementById("airBtn");
function renderZoomStatus(s) {
  if (!s) { zoomStatusEl.innerHTML = ""; return; }
  const observed = !!s.observed;
  const tag = observed ? "live" : "assumed";
  const airCls = s.air_on ? "air-on" : "air-off";
  const airTxt = (s.air_on ? "ON AIR" : "OFF AIR") + " (" + tag + ")";
  // Tally-light: green when off-air, red when on-air. Pills carry the same
  // info; this just makes the state legible at a glance.
  airBtnEl.classList.toggle("on-air",  !!s.air_on);
  airBtnEl.classList.toggle("off-air", !s.air_on);
  const vidCls = s.video_on ? "on" : "off";
  const micCls = s.mic_on ? "on" : "off";
  let focusPill;
  if (!s.focus_check_active) {
    focusPill = `<span class="pill warn">focus check off</span>`;
  } else if (s.zoom_focused) {
    focusPill = `<span class="pill on">Zoom focused</span>`;
  } else {
    const fg = s.foreground_process || "no window";
    focusPill = `<span class="pill warn">⚠ ${esc(fg)}</span>`;
  }
  let truthPill;
  if (!s.uia_available) {
    truthPill = `<span class="pill warn">UIA off</span>`;
  } else if (observed) {
    truthPill = `<span class="pill on">● live</span>`;
  } else if (s.in_meeting === false) {
    truthPill = `<span class="pill warn">no meeting</span>`;
  } else {
    truthPill = `<span class="pill warn">toolbar hidden</span>`;
  }
  zoomStatusEl.innerHTML =
    `<span class="pill ${airCls}">${airTxt}</span>` +
    `<span class="pill ${vidCls}">VID ${s.video_on ? "ON" : "OFF"}</span>` +
    `<span class="pill ${micCls}">MIC ${s.mic_on ? "ON" : "OFF"}</span>` +
    truthPill +
    focusPill +
    `<span class="pill">last ${fmtTime(s.last_toggled)}</span>`;
}
async function refreshZoomStatus() {
  try {
    const r = await fetch("/zoom_meeting/state", { credentials: "same-origin" });
    if (r.ok) renderZoomStatus(await r.json());
  } catch (e) { /* network blip — ignore */ }
}
refreshZoomStatus();
setInterval(refreshZoomStatus, 2000);

// ---- keep screen awake while operating ----
// Tries the Wake Lock API first (works in secure contexts: HTTPS,
// localhost, or PWA installed from HTTPS). On plain HTTP/LAN — which
// is the default deployment — both Wake Lock and Service Worker are
// unavailable, so we fall back to playing a tiny silent looped video
// (the NoSleep.js trick) which most mobile browsers honor by NOT
// sleeping the screen.
//
// Both paths arm on the first user gesture (browser autoplay rules)
// and re-engage when the tab becomes visible again, since the OS
// releases the lock when the page is backgrounded.
//
// Video assets adapted from NoSleep.js (MIT, Rich Tibbett).
(function() {
  let wakeLock = null;
  let video = null;
  let armed = false;

  function makeVideo() {
    const v = document.createElement("video");
    v.setAttribute("playsinline", "");
    v.setAttribute("muted", "");
    v.setAttribute("loop", "");
    v.muted = true;
    v.loop = true;
    v.style.cssText = "position:fixed;width:1px;height:1px;opacity:0;" +
      "pointer-events:none;left:-10px;top:-10px;";
    const s1 = document.createElement("source");
    s1.src = "/static/silent.webm"; s1.type = "video/webm";
    const s2 = document.createElement("source");
    s2.src = "/static/silent.mp4"; s2.type = "video/mp4";
    v.appendChild(s1); v.appendChild(s2);
    document.body.appendChild(v);
    return v;
  }

  async function tryNativeLock() {
    if (!("wakeLock" in navigator)) return false;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
      return true;
    } catch (e) { return false; }
  }

  async function tryVideoFallback() {
    if (!video) video = makeVideo();
    try { await video.play(); return true; } catch (e) { return false; }
  }

  async function enable() {
    if (await tryNativeLock()) return;
    await tryVideoFallback();
  }

  async function armOnce() {
    if (armed) return;
    armed = true;
    document.removeEventListener("pointerdown", armOnce);
    await enable();
  }

  document.addEventListener("pointerdown", armOnce, { once: true });
  document.addEventListener("visibilitychange", () => {
    if (armed && document.visibilityState === "visible") enable();
  });
})();

// ---- rule-of-thirds overlay toggle ----
// Defaults ON; persists per-device in localStorage so each operator's
// phone remembers its own preference.
(function() {
  const wrap = document.getElementById("previewWrap");
  const btn = document.getElementById("thirdsToggle");
  const KEY = "pew_ptz_thirds";
  const saved = localStorage.getItem(KEY);
  const startOn = saved === null ? true : saved === "on";
  wrap.classList.toggle("no-thirds", !startOn);
  btn.addEventListener("click", () => {
    wrap.classList.toggle("no-thirds");
    const nowOn = !wrap.classList.contains("no-thirds");
    localStorage.setItem(KEY, nowOn ? "on" : "off");
  });
})();

// ---- live snapshot preview ----
// Poll the camera's snapshot URL (configured server-side) and swap into the
// visible <img> only after the new frame finishes loading. Avoids flicker / blanks.
(function() {
  const imgEl = document.getElementById("preview");
  const base = imgEl.src.split("?")[0];
  const PERIOD_MS = 400;
  function tick() {
    const next = new Image();
    next.onload = () => { imgEl.src = next.src; setTimeout(tick, PERIOD_MS); };
    next.onerror = () => { setTimeout(tick, 1500); };
    next.src = base + "?t=" + Date.now();
  }
  tick();
})();
