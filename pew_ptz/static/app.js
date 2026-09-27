let presetData = window.PEW_PRESETS || { shared: [], wards: [], active_ward: null, home_slot: 1, version: 0 };
const NAME_MAX = 24;  // keep in sync with presets.NAME_MAX
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

// "HOME" = the active ward's first preset, else the first shared preset.
document.getElementById("homeBtn").addEventListener("click",
  () => post(`/preset/recall/${presetData.home_slot}`));

document.querySelectorAll(".mode button[data-speed]").forEach(btn => {
  btn.addEventListener("click", () => {
    speed = parseInt(btn.dataset.speed, 10);
    document.querySelectorAll(".mode button[data-speed]")
      .forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  });
});

// ---- presets ----
// The active ward is server-wide: changing it here changes it for every
// phone, and the status poll below re-renders when another phone changes it.
const presetsEl = document.getElementById("presets");
const presetsCard = document.getElementById("presetsCard");
const wardSelect = document.getElementById("wardSelect");
const editToggle = document.getElementById("editToggle");
const editBanner = document.getElementById("editBanner");
let editing = false;

function setEditing(on) {
  editing = on;
  editToggle.setAttribute("aria-pressed", on ? "true" : "false");
  editToggle.textContent = on ? "Done" : "Edit";
  editBanner.hidden = !on;
  presetsCard.classList.toggle("editing", on);
}
editToggle.addEventListener("click", () => setEditing(!editing));

function presetButton(p) {
  const b = document.createElement("button");
  b.textContent = p.name;
  b.addEventListener("click", async () => {
    if (editing) { openSheet(p); return; }
    await post(`/preset/recall/${p.slot}`);
    flash(p.name);
  });
  return b;
}

// ---- edit-mode action sheet: save position / rename ----
const sheet = document.getElementById("presetSheet");
let sheetPreset = null;
function openSheet(p) {
  sheetPreset = p;
  document.getElementById("sheetTitle").textContent = p.name;
  document.getElementById("sheetSub").textContent = presetData.active_ward || "No ward";
  sheet.hidden = false;
}
function closeSheet(done) {
  sheet.hidden = true;
  sheetPreset = null;
  if (done) setEditing(false);  // one change per trip into edit mode
}
document.getElementById("sheetCancel").addEventListener("click", () => closeSheet(false));
sheet.addEventListener("click", e => { if (e.target === sheet) closeSheet(false); });
document.getElementById("sheetSave").addEventListener("click", async () => {
  const p = sheetPreset;
  const j = await post(`/preset/save/${p.slot}`);
  if (j && j.status) flash(`Saved ${p.name}`);
  closeSheet(true);
});
document.getElementById("sheetRename").addEventListener("click", async () => {
  const p = sheetPreset;
  const name = prompt(`New name for "${p.name}" (max ${NAME_MAX} characters):`, p.name);
  if (name === null || name.trim() === p.name) { closeSheet(false); return; }
  try {
    const r = await fetch(`/preset/rename/${p.slot}`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const j = await r.json();
    if (!r.ok) { flash("⚠ " + (j.error || r.statusText)); return; }
    presetData = j;
    renderPresets();
    flash(j.status);
    closeSheet(true);
  } catch (e) {
    flash("⚠ network");
  }
});

function renderPresets() {
  // One set at a time: the selected ward's presets, or the "No ward" set.
  presetsEl.replaceChildren();
  const ward = presetData.wards.find(w => w.name === presetData.active_ward);
  const list = ward ? ward.presets : presetData.shared;
  list.forEach(p => presetsEl.appendChild(presetButton(p)));

  wardSelect.hidden = presetData.wards.length === 0;
  wardSelect.replaceChildren();
  const none = new Option("No ward", "");
  wardSelect.appendChild(none);
  presetData.wards.forEach(w => wardSelect.appendChild(new Option(w.name, w.name)));
  wardSelect.value = presetData.active_ward || "";
}

wardSelect.addEventListener("change", async () => {
  try {
    const r = await fetch("/ward", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ward: wardSelect.value || null }),
    });
    const j = await r.json();
    if (!r.ok) { flash("⚠ " + (j.error || r.statusText)); return; }
    presetData = j;
    renderPresets();
    flash(j.active_ward ? `Ward: ${j.active_ward}` : "No ward");
  } catch (e) {
    flash("⚠ network");
  }
});

async function reloadPresets() {
  try {
    const r = await fetch("/presets", { credentials: "same-origin" });
    if (r.ok) { presetData = await r.json(); renderPresets(); }
  } catch (e) { /* next poll will retry */ }
}

renderPresets();

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
const airBtnEl = document.getElementById("airBtn");
const airLabelEl = document.getElementById("airLabel");
const airHintEl = document.getElementById("airHint");
const vidBtnEl = document.getElementById("vidBtn");
const micBtnEl = document.getElementById("micBtn");
function renderZoomStatus(s) {
  if (!s) { zoomStatusEl.replaceChildren(); return; }
  // The buttons carry the state: tally colour + ON AIR / OFF AIR on the big
  // button, green/red on Video and Mic. "(assumed)" when Zoom's real state
  // can't be read.
  airLabelEl.textContent = s.air_on ? "ON AIR" : "OFF AIR";
  airHintEl.textContent = "tap to toggle video + mic" + (s.observed ? "" : " · assumed");
  airBtnEl.classList.toggle("on-air",  !!s.air_on);
  airBtnEl.classList.toggle("off-air", !s.air_on);
  vidBtnEl.textContent = "Video: " + (s.video_on ? "ON" : "OFF");
  micBtnEl.textContent = "Mic: " + (s.mic_on ? "ON" : "OFF");
  vidBtnEl.className = s.video_on ? "on" : "off";
  micBtnEl.className = s.mic_on ? "on" : "off";

  // Pills only for things that need the operator's attention.
  const warnings = [];
  if (!s.focus_check_active) warnings.push("focus check off");
  else if (!s.zoom_focused) warnings.push("⚠ " + (s.foreground_process || "no window") + " in front of Zoom");
  if (!s.uia_available) warnings.push("UIA off");
  else if (!s.observed) warnings.push(s.in_meeting === false ? "no meeting" : "toolbar hidden");
  zoomStatusEl.replaceChildren(...warnings.map(text => {
    const span = document.createElement("span");
    span.className = "pill warn";
    span.textContent = text;
    return span;
  }));
}
async function refreshZoomStatus() {
  try {
    const r = await fetch("/zoom_meeting/state", { credentials: "same-origin" });
    if (r.ok) {
      const st = await r.json();
      renderZoomStatus(st);
      if ((st.active_ward || null) !== (presetData.active_ward || null) ||
          st.presets_version !== presetData.version) reloadPresets();
    }
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
