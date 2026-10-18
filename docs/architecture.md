# Architecture

This is a single-process Flask app with three responsibilities:

1. Translate HTTP requests from the operator's phone into **VISCA-over-IP**
   UDP packets aimed at the camera.
2. Translate the "TOGGLE AIR" requests into **keyboard events** (`Alt+V`,
   `Alt+A`) delivered to whatever window is focused on the host PC — which
   needs to be the Zoom client.
3. Read Zoom's **actual** mute/video state via Windows UI Automation in a
   background thread so the on-air pills reflect reality and not just the
   toggles we sent.

There is no database and no auth. The camera stores its own preset
positions; the app sends recall and save commands. The only state the app
keeps is preset names and wards in `presets.json` and the selected ward in
`active-ward.json`, both in the install folder.

## Why the server lives on the Zoom host PC

`pynput` synthesizes OS-level keystrokes. They go to whatever window has
focus. To mute Zoom, the keystrokes have to be generated **on the Zoom PC
itself** — you can't send `Alt+V` over a network to a different machine
and have Zoom receive it.

That single constraint pins the server's location. Everything else (the
phone UI, the camera) is on the LAN by happy coincidence.

## Why the camera control is HTTP, not WebSocket

Hold-to-move uses `pointerdown` → POST drive command, `pointerup` → POST stop
command. Two HTTP requests per gesture. On a LAN this is well under 10 ms
round-trip and no slower than a WebSocket would be for this volume of
traffic, while keeping the server trivial.

If you ever wanted continuous-speed control (analog joystick on the phone),
WebSocket would start paying for itself. We don't.

## VISCA-over-IP framing

`pew_ptz/visca.py` implements the standard Sony VISCA-over-IP wire format.
Every UDP packet to port 52381 looks like:

```
| 0x01 0x00 | length(2 BE) | seq(4 BE) | VISCA payload (variable) |
```

- `0x0100` — payload type "VISCA command"
- `length` — number of bytes in the VISCA payload
- `seq` — monotonic per-socket counter; the camera echoes it on reply
- VISCA payload — the standard `81 … FF` Sony VISCA frame

The reply uses the same header (with payload type `0x01 0x11` for ACK /
completion). We read with a short timeout and discard — we don't gate UI
responsiveness on it.

A `threading.Lock` serializes sequence-number increment + send so concurrent
requests can't collide on the same socket.

## Pan/tilt-drive byte layout

```
81 01 06 01  VV  WW  XX  YY  FF
              │   │   │   │
              │   │   │   └── tilt direction: 01 up, 02 down, 03 stop
              │   │   └────── pan direction:  01 left, 02 right, 03 stop
              │   └────────── tilt speed (1..0x14)
              └────────────── pan speed (1..0x18)
```

If you only want to move on one axis, set the *other* axis's direction to
`0x03` (stop). E.g., "up" is `pan_dir=03, tilt_dir=01`. The `DIRS` table in
`server.py` encodes all eight diagonals + cardinals this way.

## Zoom state via UI Automation

`pew_ptz/zoom_state.py` runs a daemon thread that polls Zoom's in-meeting
toolbar via Windows UIA every 1.5 s. The mute and video buttons advertise
their *current* state through their accessible names, e.g.:

| Button name (substring) | What it tells us |
|---|---|
| `currently unmuted` | mic is on |
| `currently muted` | mic is off |
| `stop my video` | video is on |
| `start my video` | video is off |

(Older Zoom builds use `mute my microphone` / `start video` / etc. — the
matchers handle both.)

Critical implementation details:

- **COM is per-thread on Windows.** The poller calls `comtypes.CoInitialize()`
  on its own thread before any UIA call. Without this, `auto.GetRootControl()`
  throws `WinError -2147221008 ("CoInitialize has not been called")`.
- **Toolbar visibility matters.** Zoom auto-hides the meeting toolbar after
  a few seconds of mouse inactivity, and hidden buttons drop out of the
  UIA tree. The operator must enable
  *Zoom → Settings → Meetings & Webinars → Controls → "Keep meeting
  controls visible"* (in Zoom Workplace 7.x; older builds had this under
  *Accessibility*).
- **Meeting window class.** Zoom Workplace 7.x uses
  `ConfMultiTabContentWndClass` for both meetings and webinars. Older builds
  used `ZPMeetingWndClass`/`ZPContentViewWndClass`. The `Zoom Workplace`
  launcher window (`ZPPTMainFrmWndClassEx`) does *not* host the meeting
  toolbar and is explicitly skipped.

When UIA gives us a state, `_public_state` returns it with `observed: true`.
When the toolbar is hidden or no meeting is active, `observed: false` and the
UI falls back to the optimistic state (last toggle we sent). After every
toggle, the server pre-syncs the optimistic state from UIA before flipping,
so one tap from a drifted state can't compound the drift.

## Porting to macOS or Linux

Camera control is OS-agnostic — pure socket code. Zoom hotkey injection is
the only Windows-flavored bit, and even that is mostly true on macOS:

- **macOS**: Zoom uses `Cmd+Shift+V` (video) and `Cmd+Shift+A` (mic). Replace
  the `_alt_chord` helper with a Cmd+Shift chord. `pynput` works on macOS
  but requires Accessibility permission for the Python interpreter.
- **Linux**: Zoom hotkeys are configurable in-app and `pynput` works under
  X11 (less reliably under Wayland). You'll likely need to run the server
  on a desktop session, not headless.

The UIA-based state reader is Windows-only — `uiautomation` is gated to
`sys_platform == 'win32'` in `pyproject.toml`. On other platforms the server
falls back to optimistic-state mode.

If you're not on Windows, the server still starts and the camera still works;
the Zoom endpoints just return 500 with `keyboard unavailable on this host`.

## Running unattended

For Sunday-morning operation the server is registered as a per-user
**Scheduled Task** that runs at logon of the operator account. Do not use
a Windows service: services run in Session 0 by default and can't deliver
keystrokes to an interactive desktop, which breaks `pynput`.

The full setup is automated by [`scripts/install.ps1`](../scripts/install.ps1).
It will:

- Ensure Python 3.14 is installed (via winget) and create `.venv`
- Install the package, including `uiautomation`
- Open Windows Firewall on TCP 8080 (Private profile only)
- Register a Scheduled Task `Pew PTZ Controller`:
  - Trigger: At logon of the operator user (default `Chapel-AV`)
  - Action: `.venv\Scripts\pythonw.exe -m pew_ptz.supervise --port 8080
    --log-dir <InstallDir>\logs`, working directory `<InstallDir>`
  - Settings: one instance at a time (a second start is ignored), no
    execution time limit, normal priority (Task Scheduler's default is below
    normal, which would share the CPU poorly with Zoom), run only when the
    user is logged on
- Drop a `Pew PTZ.url` shortcut on the operator's desktop

The non-obvious requirements baked in:

1. **Run only when user is logged on** (not "whether or not user is logged
   on") — so we get a proper desktop session and pynput can target windows.
2. **Launch via `pythonw.exe`** so no console window pops up on the booth PC.
3. **The task's action is the long-running process itself.** Launching
   through a `.cmd` with `start ""` (as early versions did) makes the task
   finish immediately, so Task Scheduler loses track of the server: *End*
   and `Stop-ScheduledTask` do nothing, "one instance at a time" never
   applies, and restart-on-failure never fires.

The Zoom client still has to be the foreground window when an air button is
tapped. That's a Zoom limitation, not ours.

### The supervisor

Task Scheduler's restart-on-failure only covers a task that fails to *start*,
not one that starts fine and crashes later, so the task runs a small
supervisor ([`pew_ptz/supervise.py`](../pew_ptz/supervise.py)) that runs the
server as a child and restarts it: after 1 s the first time, then 5, 10, 20,
40 and 60 s, back to 1 s once it has stayed up for five minutes. Every exit
is logged to `supervisor.log` with its exit code, and the child's stderr goes
to `server-stderr.log` so import errors and native crash dumps aren't lost
(`pythonw.exe` has nowhere to print them). The supervisor sets up its log
before anything that could fail, and reads `.env` only to find the log folder
if it wasn't given one; it doesn't pass `.env` on, so every server start reads
the file afresh and logs which settings it applied.

Three exits are not restarted, because restarting wouldn't help; the
supervisor exits with the same code, which becomes the task's Last Run Result
([`exitcodes.py`](../pew_ptz/exitcodes.py)):

| Code | Meaning |
|---|---|
| 0 | the server stopped normally |
| 78 | bad configuration (a setting that isn't a number, a port out of range…) |
| 98 | the port is already in use |

The values avoid codes Python produces by itself (1 for an exception, 2 for
usage errors, 3 for `abort()` with faulthandler on, `0xC…` for native
crashes), so a crash can't be mistaken for one of them.

The process tree, all `pythonw.exe`: the venv launcher started by the task →
the supervisor → a venv launcher → the server. Each venv launcher kills its
child when it dies, and the supervisor puts the server in a Windows job object
that kills it when the supervisor dies, so ending the task takes down the
whole tree and never leaves a server holding the port.

### One server per port

Waitress normally sets `SO_REUSEADDR` on its socket, and on Windows that lets
a second server bind the same port *without any error* while the first one
keeps answering every request. A forgotten old copy could therefore survive a
"restart" and keep serving old code. The server binds its port itself with
`SO_EXCLUSIVEADDRUSE` before it touches the camera or Zoom, and hands the
socket to waitress. A second copy logs which port is taken and exits with
code 98.

### Updating while running

Python modules are loaded once at startup, and the server also loads its
templates and static files into memory then
([`pew_ptz/assets.py`](../pew_ptz/assets.py)). A `git pull` underneath a
running server therefore changes nothing it serves; it can't send a new
`app.js` with an old page. The server fingerprints the package's files at
startup ([`pew_ptz/version.py`](../pew_ptz/version.py)); when the files on disk
fingerprint differently, `/healthz` and the status poll report
`restart_needed` and the phones show an *Update waiting* pill.

Each page knows the build it was served from. When the status poll reports a
different build (the server restarted on new code), the page reloads itself
as soon as no button is held, no preset sheet or edit mode is open and no
copy is running, and only after checking that the new page loads. The reload
hands the screen wake lock on to the new page; if the browser wants a tap
first, a pill asks for one. After any restart (a new `started_at`), the page
re-reads the presets and shows a *Controller restarted* pill until Zoom's real
state is readable or a Zoom button is tapped, because the server's assumed
Zoom state starts over at ON AIR.

`/healthz` reports `pid`, `started_at`, `version` (package version plus git
commit) and `build`. `scripts/restart.ps1`, `update.ps1` and `install.ps1` use
them to confirm that the process answering is the one they just started, on
the code they just installed, and that exactly one process is listening.
