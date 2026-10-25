let presetData = window.PEW_PRESETS || { shared: [], wards: [], active_ward: null, home_slot: 1, version: 0 };
const NAME_MAX = 24;  // keep in sync with presets.NAME_MAX
let speed = 1;  // Fine; keep in sync with the "active" button in index.html
// Screen wake lock state, shared with the version check so a reload can hand
// it on to the new page (see "keep screen awake" below).
const wake = { armed: false, needsTap: false, carried: false };
// Set when the controller restarted: its assumed Zoom state starts over at
// ON AIR, so the operator should check Zoom until a toggle or a real reading.
let restartedNotice = false;

const toast = document.getElementById("toast");
let toastTimer = null;
function flash(msg) {
  toast.textContent = msg;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 1200);
}

// ---- connection health ----
// Browsers can't see the Wi-Fi name or signal strength, but they can time the
// status poll (every 2 s). Consistently slow polls, or repeated failures, mean a weak
// signal; several failures in a row mean the phone has lost the controller
// (left the Wi-Fi, or switched to mobile data). Android Chrome also says when
// the phone is on mobile data; iPhones don't.
const WIFI = window.PEW_WIFI || null;
const netBanner = document.getElementById("netBanner");
const NET_WINDOW = 5;        // recent polls considered
const NET_SLOW_MS = 1000;    // median round trip that counts as weak (LAN is ~10-50 ms)
const NET_WEAK_FAILS = 2;    // failures within the window that count as weak (one is a blip)
const NET_LOST_FAILS = 3;    // consecutive failures that count as lost (~6 s)
// Phones freeze a page that's in the background or behind a locked screen, and
// their Wi-Fi naps; requests caught by that finish late or time out. Ignore
// everything until the page has been awake for a few seconds.
const NET_SETTLE_MS = 5000;
const netRecent = [];        // {ok, ms}
let netFailsInARow = 0;
let netQuietUntil = performance.now() + NET_SETTLE_MS;  // the page just loaded
let netLastSample = performance.now();
function netSettle() {
  netRecent.length = 0;
  netFailsInARow = 0;
  netQuietUntil = performance.now() + NET_SETTLE_MS;
  renderNet();
}
function noteNet(ok, ms) {
  const now = performance.now();
  // Polls run every 2 s; a much longer gap means the page was frozen.
  if (now - netLastSample > 8000) netSettle();
  netLastSample = now;
  if (document.visibilityState !== "visible" || now < netQuietUntil) return;
  netRecent.push({ ok, ms });
  if (netRecent.length > NET_WINDOW) netRecent.shift();
  netFailsInARow = ok ? 0 : netFailsInARow + 1;
  renderNet();
}
document.addEventListener("visibilitychange", netSettle);
function onMobileData() {
  const c = navigator.connection;
  return !!(c && c.type === "cellular");
}
function wifiNamed() { return WIFI ? `the "${WIFI}" Wi-Fi` : "the right Wi-Fi network"; }
function renderNet() {
  let level = null;
  let text = "";
  if (!navigator.onLine || netFailsInARow >= NET_LOST_FAILS) {
    level = "lost";
    text = onMobileData()
      ? `This phone is on mobile data and can't reach the camera. Turn Wi-Fi on and join ${wifiNamed()}.`
      : `Lost connection to the controller. Check Wi-Fi is on and connected to ${wifiNamed()}.`;
  } else {
    const times = netRecent.filter(s => s.ok).map(s => s.ms).sort((a, b) => a - b);
    const median = times.length >= 3 ? times[Math.floor(times.length / 2)] : 0;
    if (netRecent.filter(s => !s.ok).length >= NET_WEAK_FAILS || median > NET_SLOW_MS) {
      level = "weak";
      text = "Weak Wi-Fi: controls may lag. Move closer to the router" +
        (WIFI ? ` or check you're on "${WIFI}".` : ".");
    }
  }
  netBanner.hidden = !level;
  netBanner.className = "net-banner" + (level ? " " + level : "");
  netBanner.textContent = text;
}
window.addEventListener("online", renderNet);
window.addEventListener("offline", renderNet);
if (navigator.connection) navigator.connection.addEventListener("change", renderNet);

// fetch that gives up after timeoutMs, so a hung request counts as a failure.
async function fetchWithin(path, opts, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try { return await fetch(path, { ...opts, signal: ctl.signal }); }
  finally { clearTimeout(timer); }
}

async function post(path) {
  // A stop that doesn't arrive leaves the camera moving, so stops are retried.
  const isStop = path.endsWith("/stop");
  for (let attempt = 1; ; attempt++) {
    try {
      const r = await fetchWithin(path, { method: "POST", credentials: "same-origin" }, 4000);
      const j = await r.json().catch(() => ({}));
      if (r.status === 409 && j.foreground_process !== undefined) {
        flash("⚠ Zoom not focused (" + (j.foreground_process || "unknown") + ")");
      } else if (!r.ok) {
        flash("⚠ " + (j.error || r.statusText));
      }
      return j;
    } catch (e) {
      noteNet(false, 0);
      if (isStop && attempt < 3) { await new Promise(res => setTimeout(res, 300)); continue; }
      flash(isStop ? "⚠ Stop didn't reach the camera. Tap the button again." : "⚠ Didn't reach the controller");
      return undefined;
    }
  }
}

// ---- hold-to-move for D-pad ----
let holdsActive = 0;  // buttons held right now; the page never reloads mid-hold
function bindHold(btn, onStart, onEnd) {
  let active = false;
  const start = (e) => {
    e.preventDefault();
    if (active) return;
    active = true;
    holdsActive++;
    onStart();
  };
  const end = (e) => {
    if (!active) return;
    active = false;
    holdsActive = Math.max(0, holdsActive - 1);
    onEnd();
  };
  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerup", end);
  btn.addEventListener("pointerleave", end);
  btn.addEventListener("pointercancel", end);
}

// Fine (speed 1): a quick tap nudges a small exact step; holding past
// NUDGE_HOLD_MS creeps at the slowest speed until released.
const FINE_SPEED = 1;
const NUDGE_HOLD_MS = 250;
document.querySelectorAll(".dpad button[data-dir]").forEach(btn => {
  const dir = btn.dataset.dir;
  let holdTimer = null;
  let driving = false;
  bindHold(btn,
    () => {
      if (speed !== FINE_SPEED) { post(`/ptz/move/${dir}?speed=${speed}`); return; }
      driving = false;
      holdTimer = setTimeout(() => {
        holdTimer = null;
        driving = true;
        post(`/ptz/move/${dir}?speed=${FINE_SPEED}`);
      }, NUDGE_HOLD_MS);
    },
    () => {
      if (speed !== FINE_SPEED) { post(`/ptz/stop`); return; }
      if (holdTimer) {            // released quickly: a tap
        clearTimeout(holdTimer);
        holdTimer = null;
        post(`/ptz/nudge/${dir}`);
      } else if (driving) {
        driving = false;
        post(`/ptz/stop`);
      }
    });
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

const copyBtn = document.getElementById("copyBtn");
function setEditing(on) {
  editing = on;
  editToggle.setAttribute("aria-pressed", on ? "true" : "false");
  editToggle.textContent = on ? "Done" : "Edit";
  editBanner.hidden = !on;
  presetsCard.classList.toggle("editing", on);
  // Offer the one-time copy only inside a ward.
  const ward = presetData.active_ward;
  copyBtn.hidden = !ward;
  if (ward) copyBtn.textContent = `Copy No-ward positions into ${ward}`;
}

// ---- copy No-ward positions into the selected ward ----
const copyProgress = document.getElementById("copyProgress");
const copyText = document.getElementById("copyText");
let copyWasRunning = false;
copyBtn.addEventListener("click", async () => {
  const ward = presetData.active_ward;
  const n = Math.min(presetData.shared.length,
    (presetData.wards.find(w => w.name === ward) || { presets: [] }).presets.length);
  if (!confirm(`Copy the ${n} No-ward camera positions into ${ward}?\n\n` +
               `This replaces ${ward}'s saved positions (names stay the same). ` +
               `The camera will move through each shot; it takes about a minute.`)) return;
  const j = await post("/ward/copy-no-ward");
  if (j && j.copy) { setEditing(false); renderCopy(j.copy); }
});
document.getElementById("copyCancel").addEventListener("click", () => post("/ward/copy-no-ward/cancel"));

function renderCopy(c) {
  if (!c) return;
  copyProgress.hidden = !c.running;
  if (c.running) {
    copyText.textContent = `Copying positions into ${c.ward}: ${c.done} of ${c.total}…`;
  } else if (copyWasRunning) {
    flash(c.result === "done" ? `Copied ${c.done} positions into ${c.ward}`
        : c.result === "cancelled" ? `Stopped after ${c.done} of ${c.total}`
        : c.result == null ? "⚠ Copy interrupted by a controller restart"
        : `⚠ Copy failed (${c.result})`);
  }
  copyWasRunning = !!c.running;
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
  restartedNotice = false;
  const j = await post("/zoom_meeting/toggle_air");
  if (j && j.status) flash("Toggled video + mic");
  refreshZoomStatus();
});
document.getElementById("vidBtn").addEventListener("click", async () => {
  restartedNotice = false;
  const j = await post("/zoom_meeting/toggle_video");
  if (j && j.status) flash("Toggled video");
  refreshZoomStatus();
});
document.getElementById("micBtn").addEventListener("click", async () => {
  restartedNotice = false;
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
  if (s.observed) restartedNotice = false;  // real state again
  if (restartedNotice) warnings.push("⚠ Controller restarted · check Zoom's mic and video");
  if (wake.needsTap) warnings.push("Tap anywhere to keep the screen on");
  // Last, so nothing above it moves when it appears.
  if (s.restart_needed) warnings.push("Update waiting · controls still work · tell whoever looks after this PC");
  zoomStatusEl.replaceChildren(...warnings.map(text => {
    const span = document.createElement("span");
    span.className = "pill warn";
    span.textContent = text;
    return span;
  }));
}
// ---- server version ----
// After the controller restarts on new code, a page that was left open still
// runs the old script against the new server. Reload it as soon as nothing is
// in progress: no button held, no preset sheet or edit mode, no copy running.
const PAGE_BUILD = window.PEW_BUILD || null;
let serverStarted = window.PEW_STARTED || null;
const RELOADED_KEY = "pew_ptz_reloaded_at";
const CARRY_KEY = "pew_ptz_carry";  // what a reload hands on to the new page
function sessionGet(key) {
  try { return sessionStorage.getItem(key); } catch (e) { return null; }  // private mode
}
function sessionSet(key, value) {
  try { sessionStorage.setItem(key, value); } catch (e) { /* private mode */ }
}
(function takeOverFromReload() {
  const carried = sessionGet(CARRY_KEY);
  if (!carried) return;
  try { sessionStorage.removeItem(CARRY_KEY); } catch (e) { /* ignore */ }
  try {
    const c = JSON.parse(carried);
    restartedNotice = !!c.restarted;
    wake.carried = !!c.wake;
  } catch (e) { /* ignore */ }
})();
function busy() {
  return holdsActive > 0 || !sheet.hidden || editing || copyWasRunning;
}
async function checkServerVersion(st) {
  if (st.started_at && serverStarted && st.started_at !== serverStarted) {
    // A new server process: its assumed Zoom state starts over at ON AIR, and
    // presets.json may have been edited before the restart.
    restartedNotice = true;
    reloadPresets();
  }
  if (st.started_at) serverStarted = st.started_at;
  if (!st.build || !PAGE_BUILD || st.build === PAGE_BUILD || busy()) return;
  if (Date.now() - (Number(sessionGet(RELOADED_KEY)) || 0) < 30000) return;  // don't loop
  // Only trade this working page for one that actually loads.
  try {
    const r = await fetch("/", { cache: "no-store", credentials: "same-origin" });
    if (!r.ok || !(await r.text()).includes(st.build)) return;
  } catch (e) { return; }
  if (busy()) return;
  sessionSet(RELOADED_KEY, String(Date.now()));
  sessionSet(CARRY_KEY, JSON.stringify({ restarted: restartedNotice, wake: wake.armed }));
  location.reload();
}

async function refreshZoomStatus() {
  const t0 = performance.now();
  let r;
  try {
    r = await fetchWithin("/zoom_meeting/state", { credentials: "same-origin" }, 4000);
  } catch (e) {
    noteNet(false, 0);  // no answer: network trouble
    return;
  }
  noteNet(true, performance.now() - t0);
  try {
    if (r.ok) {
      const st = await r.json();
      await checkServerVersion(st);
      renderZoomStatus(st);
      renderCopy(st.copy);
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
// releases the lock when the page is backgrounded. After an automatic
// reload (see "server version") the new page re-arms straight away and
// shows a pill asking for a tap if the browser won't allow that.
//
// Video assets adapted from NoSleep.js (MIT, Rich Tibbett).
(function() {
  let wakeLock = null;
  let video = null;

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
    if (await tryNativeLock()) return true;
    return tryVideoFallback();
  }

  async function armOnce() {
    if (wake.armed) return;
    wake.armed = true;
    wake.needsTap = false;
    document.removeEventListener("pointerdown", armOnce);
    await enable();
  }

  document.addEventListener("pointerdown", armOnce, { once: true });
  document.addEventListener("visibilitychange", () => {
    if (wake.armed && document.visibilityState === "visible") enable();
  });
  if (wake.carried) {
    enable().then(ok => {
      if (ok) {
        wake.armed = true;
        document.removeEventListener("pointerdown", armOnce);
      } else if (!wake.armed) {
        wake.needsTap = true;  // pill until the first tap arms it
      }
    });
  }
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
