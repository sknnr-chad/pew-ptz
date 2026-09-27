"""
pew-ptz — Phone-based PTZ camera control + Zoom mute/video toggles.

A small Flask app for anyone who runs broadcast from a seat in the audience
instead of a control booth. Drives any VISCA-over-IP PTZ camera (PTZOptics,
Sony, AVer, Marshall, HuddleCam, BirdDog, ClearTouch RL500, etc.) from a
phone, and provides bidirectional Zoom mute/video toggles via the host's
Alt+V / Alt+A keyboard shortcuts.

Runs on the Zoom host PC (so pynput can drive Zoom's hotkeys). The phone
hits this server over the LAN. Camera control is VISCA-over-IP UDP 52381
(see visca.py). On Windows the actual Zoom mute/video state is read back
via UI Automation (see zoom_state.py) so the on-air pills can't drift.

Config via environment variables:

    PEW_PTZ_CAMERA_IP             default 192.168.100.88
    PEW_PTZ_VISCA_PORT            default 52381
    PEW_PTZ_SERVER_PORT           default 8080
    PEW_PTZ_CAMERA_SNAPSHOT_PATH  default /snapshot.jpg  (HTTP path on the
                                  camera that returns a JPEG; varies by
                                  camera vendor — see docs/ptz-cameras.md)
    PEW_PTZ_PRESETS               comma-separated preset names. Slot N on
                                  the camera maps to the Nth name (1-indexed).
                                  default: 9 sacrament-meeting positions.
                                  Ignored when presets.json exists.
    PEW_PTZ_PRESETS_FILE          shared + per-ward presets (see presets.py).
                                  Default: presets.json in the working dir
    PEW_PTZ_LOG_DIR               where to put rotating server.log; if unset,
                                  logs only to stdout
    PEW_PTZ_TITLE                 browser tab / home-screen title. Default: pew-ptz
    PEW_PTZ_CONTACT_NAME,         optional "Need help?" contact on the help page
    PEW_PTZ_CONTACT_EMAIL,
    PEW_PTZ_CONTACT_PHONE

Any of these can go in a .env file in the working directory (see dotenv.py);
real environment variables take precedence.
    PEW_PTZ_SKIP_FOCUS_CHECK      set to 1 to bypass the "Zoom must be
                                  foreground" guard (useful for UI testing)
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from waitress import serve

from pew_ptz import dotenv
from pew_ptz.presets import PresetStore
from pew_ptz.visca import (
    PAN_LEFT,
    PAN_RIGHT,
    PAN_STOP,
    TILT_DOWN,
    TILT_STOP,
    TILT_UP,
    ViscaIP,
)
from pew_ptz.zoom_state import ZoomStateReader

# Site settings from .env in the install folder (see dotenv.py). Must run
# before any _env() read below.
_DOTENV_APPLIED = dotenv.load(os.environ.get("PEW_PTZ_ENV_FILE", ".env"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get("PEW_PTZ_" + name, default)


CAMERA_IP = _env("CAMERA_IP", "192.168.100.88")
VISCA_PORT = int(_env("VISCA_PORT", "52381"))
SERVER_PORT = int(_env("SERVER_PORT", "8080"))
CAMERA_SNAPSHOT_PATH = _env("CAMERA_SNAPSHOT_PATH", "/snapshot.jpg")
PRESETS = [
    p.strip() for p in _env(
        "PRESETS",
        "Speaker,Choir,Chorister,Piano,Organ,Sacrament,North Stand,Congregation,Back Row",
    ).split(",") if p.strip()
]
PRESETS_FILE = Path(_env("PRESETS_FILE", "presets.json"))
PAGE_TITLE = _env("TITLE", "pew-ptz")
# Optional "Need help?" box on the help page. Keep these in .env, not in git.
CONTACT = {
    "name": _env("CONTACT_NAME").strip(),
    "email": _env("CONTACT_EMAIL").strip(),
    "phone": _env("CONTACT_PHONE").strip(),
}
SKIP_FOCUS_CHECK = _env("SKIP_FOCUS_CHECK", "").lower() in ("1", "true", "yes")

# Log to a rotating file when PEW_PTZ_LOG_DIR is set (the Task Scheduler launch
# uses pythonw.exe so stdout is gone — without this we'd be flying blind).
def _setup_logging():
    log_dir = _env("LOG_DIR")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            Path(log_dir) / "server.log",
            maxBytes=1_000_000, backupCount=5, encoding="utf-8",
        )
        handlers.append(fh)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

_setup_logging()
log = logging.getLogger("pew_ptz")

camera = ViscaIP(CAMERA_IP, VISCA_PORT)
app = Flask(__name__)
START_TIME = time.time()
zoom_reader = ZoomStateReader(poll_interval=1.5)
presets = PresetStore.load(PRESETS_FILE, PRESETS)

# ---- Zoom hotkeys (Windows-only via pynput) ------------------------------

try:
    from pynput.keyboard import Controller, Key
    _kbd = Controller()
except Exception as e:  # pragma: no cover - non-Windows / headless
    _kbd = None
    log.warning("keyboard controller unavailable: %s", e)


def _alt_chord(letter: str):
    """Send Alt+<letter> to the foreground window (must be Zoom)."""
    if _kbd is None:
        return False
    _kbd.press(Key.alt)
    try:
        _kbd.press(letter)
        _kbd.release(letter)
    finally:
        _kbd.release(Key.alt)
    return True


def zoom_toggle_video() -> bool:
    return _alt_chord("v")


def zoom_toggle_mic() -> bool:
    return _alt_chord("a")


# ---- Foreground-window detection (Windows) -------------------------------
# Used to verify Zoom is the active window before sending hotkeys, so we don't
# accidentally fire Alt+V / Alt+A into whatever else has focus.

ZOOM_PROCESS_NAMES = {"zoom.exe", "cpthost.exe"}

_user32 = None
_kernel32 = None
if sys.platform == "win32":
    try:
        _user32 = ctypes.windll.user32
        _kernel32 = ctypes.windll.kernel32
    except Exception as e:
        log.warning("Win32 foreground check unavailable: %s", e)


def foreground_info() -> tuple[bool, str, str]:
    """Return (is_zoom, window_title, process_basename). is_zoom False off-Windows."""
    if _user32 is None or _kernel32 is None:
        return (False, "", "")
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return (False, "", "")
    length = _user32.GetWindowTextLengthW(hwnd)
    tbuf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, tbuf, length + 1)
    title = tbuf.value
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        try:
            size = wintypes.DWORD(260)
            pbuf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                proc = pbuf.value.rsplit("\\", 1)[-1]
        finally:
            _kernel32.CloseHandle(h)
    return (proc.lower() in ZOOM_PROCESS_NAMES, title, proc)


# ---- Optimistic Zoom state ----------------------------------------------
# Fallback for when UI Automation can't see the toolbar (see zoom_state.py):
# we track what we *believe* the state is from the toggles we've sent. Resets
# to "on air" every server restart.

_zoom_state = {"video_on": True, "mic_on": True, "last_toggled": None}


def _public_state() -> dict:
    is_zoom, _title, proc = foreground_info()
    focus_active = _user32 is not None and not SKIP_FOCUS_CHECK
    uia = zoom_reader.get()

    # Prefer observed (UIA) values when we have them; fall back to optimistic.
    def pick(key: str) -> bool:
        if uia["observed"] and uia[key] is not None:
            return uia[key]
        return _zoom_state[key]

    video_on = pick("video_on")
    mic_on = pick("mic_on")

    return {
        "video_on": video_on,
        "mic_on": mic_on,
        "air_on": bool(video_on) and bool(mic_on),
        "last_toggled": _zoom_state["last_toggled"],
        "zoom_focused": is_zoom,
        "foreground_process": proc,
        "active_ward": presets.active_ward,  # lets other phones notice a ward change
        "presets_version": presets.version,  # ...and a rename
        "focus_check_active": focus_active,
        # Source-of-truth metadata so the UI can show observed vs assumed:
        "observed": uia["observed"],
        "in_meeting": uia["in_meeting"],
        "uia_available": uia["uia_available"],
        "uia_last_observed": uia["last_observed"],
        "uia_walk_ms": uia["walk_ms"],
    }


def _focus_block():
    """Return a (response, status) tuple if the chord should be blocked, else None."""
    if not _user32 or SKIP_FOCUS_CHECK:
        return None
    if foreground_info()[0]:
        return None
    return jsonify({
        "error": "Zoom is not the foreground window",
        **_public_state(),
    }), 409


# ---- Direction map -------------------------------------------------------

DIRS = {
    "up":         (PAN_STOP,  TILT_UP),
    "down":       (PAN_STOP,  TILT_DOWN),
    "left":       (PAN_LEFT,  TILT_STOP),
    "right":      (PAN_RIGHT, TILT_STOP),
    "up_left":    (PAN_LEFT,  TILT_UP),
    "up_right":   (PAN_RIGHT, TILT_UP),
    "down_left":  (PAN_LEFT,  TILT_DOWN),
    "down_right": (PAN_RIGHT, TILT_DOWN),
}


# ---- Routes --------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        title=PAGE_TITLE,
        camera_ip=CAMERA_IP,
        camera_snapshot_path=CAMERA_SNAPSHOT_PATH,
        presets=presets.payload(),
    )


@app.get("/help")
def help_page():
    """Operator guide. Filled in with this install's wards and presets so it
    matches what's on screen."""
    usable = presets.usable()
    home = usable.get(presets.home_slot())
    return render_template(
        "help.html",
        title=PAGE_TITLE,
        camera_ip=CAMERA_IP,
        shared=[p.name for p in presets.shared],
        wards=[{"name": w.name, "presets": [p.name for p in w.presets]} for w in presets.wards],
        active_ward=presets.active_ward,
        home=home.name if home else None,
        presets_file_used=PRESETS_FILE.exists(),
        contact=CONTACT if any(CONTACT.values()) else None,
        # tel: links want digits (and a leading +) only
        contact_tel="".join(c for c in CONTACT["phone"] if c.isdigit() or c == "+"),
    )


@app.post("/ptz/move/<direction>")
def ptz_move(direction: str):
    if direction not in DIRS:
        return jsonify({"error": "unknown direction"}), 400
    speed = request.args.get("speed", 12, type=int)
    pan_dir, tilt_dir = DIRS[direction]
    camera.pan_tilt(pan_dir, tilt_dir, pan_speed=speed, tilt_speed=speed)
    return jsonify({"status": f"moving {direction}"})


@app.post("/ptz/stop")
def ptz_stop():
    camera.pan_tilt_stop()
    return jsonify({"status": "stopped"})


@app.post("/zoom/<action>")
def zoom_action(action: str):
    speed = request.args.get("speed", 2, type=int)
    if action == "tele":
        camera.zoom_tele(speed)
    elif action == "wide":
        camera.zoom_wide(speed)
    elif action == "stop":
        camera.zoom_stop()
    else:
        return jsonify({"error": "unknown zoom action"}), 400
    return jsonify({"status": f"zoom {action}"})


@app.get("/presets")
def presets_list():
    return jsonify(presets.payload())


@app.post("/ward")
def set_ward():
    """Set the server-wide active ward. Body: {"ward": "<name>"} or
    {"ward": null} for the "No ward" presets."""
    data = request.get_json(silent=True) or {}
    name = data.get("ward")
    try:
        presets.set_active(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log.info("presets: active ward set to %s from %s", name or "(none)", _client_ip())
    return jsonify(presets.payload())


def _client_ip() -> str:
    return request.remote_addr or "unknown"


@app.post("/preset/recall/<int:n>")
def preset_recall(n: int):
    # Only the presets on screen (the selected ward's, or the "No ward" set)
    # are reachable, so one ward can't jump to another's framing by accident.
    if n not in presets.usable() and n != presets.home_slot():
        return jsonify({"error": f"slot {n} isn't one of the selected ward's presets"}), 400
    camera.preset_recall(n)
    return jsonify({"status": f"recalled preset {n}"})


@app.post("/preset/save/<int:n>")
def preset_save(n: int):
    preset = presets.usable().get(n)
    if preset is None:
        return jsonify({"error": f"slot {n} isn't one of the selected ward's presets"}), 400
    camera.preset_set(n)
    log.info("presets: saved %r (slot %d, ward %s) from %s",
             preset.name, n, presets.active_ward or "no ward", _client_ip())
    return jsonify({"status": f"saved {preset.name}", "slot": n})


@app.post("/preset/rename/<int:n>")
def preset_rename(n: int):
    """Rename a preset on screen. Body: {"name": "<new name>"}. The camera
    slot and saved position don't change."""
    data = request.get_json(silent=True) or {}
    try:
        old = presets.rename(n, data.get("name", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    new = presets.usable()[n].name
    log.info("presets: renamed %r -> %r (slot %d, ward %s) from %s",
             old.name, new, n, presets.active_ward or "no ward", _client_ip())
    return jsonify({"status": f"renamed to {new}", **presets.payload()})


@app.get("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "uptime_s": int(time.time() - START_TIME),
        "keyboard_ok": _kbd is not None,
    })


@app.get("/zoom_meeting/state")
def http_zoom_state():
    return jsonify(_public_state())


def _sync_optimistic_from_uia():
    """Pre-toggle correction: if UIA can see the real state, adopt it before we flip,
    so a single toggle from a drifted optimistic state doesn't compound the drift."""
    uia = zoom_reader.get()
    if uia["observed"]:
        if uia["video_on"] is not None:
            _zoom_state["video_on"] = uia["video_on"]
        if uia["mic_on"] is not None:
            _zoom_state["mic_on"] = uia["mic_on"]


@app.post("/zoom_meeting/toggle_video")
def http_toggle_video():
    blocked = _focus_block()
    if blocked:
        return blocked
    _sync_optimistic_from_uia()
    if not zoom_toggle_video():
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    _zoom_state["video_on"] = not _zoom_state["video_on"]
    _zoom_state["last_toggled"] = time.time()
    zoom_reader.trigger_refresh()
    return jsonify({"status": "toggled video (Alt+V)", **_public_state()})


@app.post("/zoom_meeting/toggle_mic")
def http_toggle_mic():
    blocked = _focus_block()
    if blocked:
        return blocked
    _sync_optimistic_from_uia()
    if not zoom_toggle_mic():
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    _zoom_state["mic_on"] = not _zoom_state["mic_on"]
    _zoom_state["last_toggled"] = time.time()
    zoom_reader.trigger_refresh()
    return jsonify({"status": "toggled mic (Alt+A)", **_public_state()})


@app.post("/zoom_meeting/toggle_air")
def http_toggle_air():
    """Toggle both video and mic together — the 'panic' button."""
    if _kbd is None:
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    blocked = _focus_block()
    if blocked:
        return blocked
    _sync_optimistic_from_uia()
    if not zoom_toggle_video():
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    _zoom_state["video_on"] = not _zoom_state["video_on"]
    _zoom_state["last_toggled"] = time.time()
    time.sleep(0.12)
    if not zoom_toggle_mic():
        # Video already flipped; report the half-done state rather than lie.
        zoom_reader.trigger_refresh()
        return jsonify({"error": "mic toggle failed after video toggled",
                        **_public_state()}), 500
    _zoom_state["mic_on"] = not _zoom_state["mic_on"]
    zoom_reader.trigger_refresh()
    return jsonify({"status": "toggled video + mic", **_public_state()})


def main():
    log.info("pew-ptz starting on http://0.0.0.0:%s", SERVER_PORT)
    log.info("  Camera:        %s:%s", CAMERA_IP, VISCA_PORT)
    if _DOTENV_APPLIED:
        log.info("  .env settings: %s", ", ".join(sorted(_DOTENV_APPLIED)))
    log.info("  Presets:       %d shared, %d wards (%s)", len(presets.shared),
             len(presets.wards), PRESETS_FILE if PRESETS_FILE.exists() else "PEW_PTZ_PRESETS")
    log.info("  Active ward:   %s", presets.active_ward or "(none)")
    log.info("  Open on phone: http://<this-pc-ip>:%s", SERVER_PORT)
    if _kbd is None:
        log.warning("  pynput keyboard unavailable — Zoom hotkeys disabled.")
    if zoom_reader.available:
        zoom_reader.start()
    else:
        log.warning("  uiautomation unavailable — Zoom state will be optimistic only.")
    # AF should always be on at the chapel — assert it now so a power-cycled
    # camera or a stray manual-focus poke from the IR remote can't leave us
    # stuck out of focus mid-service.
    try:
        camera.focus_auto(True)
        log.info("  Autofocus: enabled")
    except Exception as e:
        log.warning("  Could not enable autofocus at startup: %s", e)
    # Waitress instead of Flask's dev server. It doesn't write per-request
    # access lines, so the rotating log stays quiet despite the 2s polling.
    # 8 threads: each phone holds a status poll plus pan/zoom presses.
    serve(app, host="0.0.0.0", port=SERVER_PORT, threads=8)


if __name__ == "__main__":
    sys.exit(main())
