# Troubleshooting

> The server runs as a Scheduled Task with no console window. Its logs are in
> `C:\tools\pew-ptz\logs\`:
>
> - `server.log`: the app itself. `Get-Content C:\tools\pew-ptz\logs\server.log -Tail 80`
>   is your friend.
> - `supervisor.log`: every start, crash and restart of the server.
> - `server-stderr.log`: anything the server printed before its log was set up,
>   and crash dumps that never reach `server.log`.

## "Page won't load on my phone"

1. Make sure the phone is on the **same Wi-Fi as the host PC and the camera**.
   If you have a dedicated controller SSID, double-check the phone is on it
   and not your guest network or personal hotspot.
2. From a desktop on the same LAN, try `http://<host-pc-ip>:8080` first to
   isolate phone vs. server.
3. Confirm the server is actually running:
   `Get-ScheduledTaskInfo -TaskName "Pew PTZ Controller"` and look at
   `LastTaskResult` (see [the table below](#what-the-tasks-last-run-result-means)).
   Or just open `http://127.0.0.1:8080/healthz` on the booth PC (`127.0.0.1`
   rather than `localhost`, which tries IPv6 first and takes a couple of
   seconds to fall back).
4. If the firewall rule went missing:
   `Get-NetFirewallRule -DisplayName "Pew PTZ Controller (TCP 8080)"`.
   Re-run `scripts\install.ps1` to recreate it.

## "The camera doesn't move"

Check `server.log` after pressing a direction button.

- **No log entry at all** → the request isn't reaching the server. See the
  page-loading section above.
- **`[visca] send error: ...`** → the server can't reach the camera's IP.
  From PowerShell on the booth PC: `ping 192.168.100.88` and
  `Test-NetConnection 192.168.100.88 -Port 52381`. (UDP isn't reliably
  probeable, so the port test will succeed even if VISCA itself isn't
  responding — start with the ping.)
- **Logs scroll, no movement, no ACK** → wrong protocol/port. See
  [docs/ptz-cameras.md](ptz-cameras.md#if-your-firmware-doesnt-speak-visca-over-ip-udp).
- **Logs scroll, camera moves wrong direction** → almost certainly the
  pan/tilt-direction bytes are swapped for *your* firmware. Try swapping
  `PAN_LEFT`/`PAN_RIGHT` (or `TILT_UP`/`TILT_DOWN`) values in
  `pew_ptz/visca.py` and report back.

## "The camera moves, then keeps moving forever"

That means `pointerup` isn't firing — usually because you dragged off the
button before releasing. The app catches `pointerleave` and `pointercancel`
too, so this should be rare. As a fallback, tap any other direction button:
the next drive command implicitly stops the previous motion. The HOME
button also resets framing back to preset 1.

## "Zoom video toggles but mic doesn't (or vice versa)"

Open Zoom → **Settings → Keyboard Shortcuts** and confirm:

- **Mute/unmute my audio** is `Alt+A`
- **Start/stop my video** is `Alt+V`
- Both have **Enable Global Shortcut** *off* — we want them to require Zoom
  focus so they can't fire while the operator is in another window.

If shortcuts are bound to different keys, change them in Zoom or update
`_alt_chord` calls in `pew_ptz/server.py`.

## "TOGGLE AIR does nothing"

Almost always: Zoom isn't the foreground window on the host PC. The
controller will pop a "⚠ Zoom not focused" toast on the phone when this
happens. Click on the Zoom meeting window to give it focus, then try again.

## "Zoom Meeting card pills say 'assumed' instead of '● live'"

The UI Automation reader can't see the toolbar. Check the small status pill
on the Zoom Meeting card:

| Pill | Meaning | Fix |
|---|---|---|
| `● live` | UIA observed real state | nothing to do |
| `toolbar hidden` | meeting open, toolbar auto-hid | enable *Settings → Meetings & Webinars → Controls → "Keep meeting controls visible"* in Zoom |
| `no meeting` | not in a meeting/webinar | join one |
| `UIA off` | `uiautomation` package didn't install or isn't loadable | re-run `scripts\install.ps1`; check `server.log` for `uiautomation unavailable: ...` |

If `● live` still doesn't appear after enabling the always-visible toolbar
and being in a meeting, a Zoom upgrade has probably renamed the meeting
window class or the mute/video button labels. Inspect the meeting window with
Microsoft's [Accessibility Insights for Windows](https://accessibilityinsights.io/docs/windows/overview/)
(or `inspect.exe` from the Windows SDK), note the window's class name and the
buttons' names, and add them to `_MEETING_CLASS_HINTS` or the `_MIC_*` /
`_VID_*` name lists in `pew_ptz/zoom_state.py`.

## "Server starts but logs `keyboard controller unavailable`"

You're not on Windows, or `pynput` couldn't get the permissions it needs.
Camera control still works; only the Zoom endpoints are disabled. On macOS
this typically means the Python interpreter needs to be granted Accessibility
permission in *System Settings → Privacy & Security*.

## "I can't reach the camera's web UI from the phone"

The phone needs to be able to route to `192.168.100.88` (or wherever the
camera lives). With the phone and camera on the same flat LAN this just
works. If you've put the camera on a separate VLAN or subnet, route
accordingly.

## "Latency on hold-to-move feels sluggish"

Two likely causes:

1. **Slow Wi-Fi between phone and router.** Check signal at the operator's
   seat; consider a closer access point.
2. **Camera ACK timeouts.** `ViscaIP(recv_timeout=...)` defaults to 150 ms.
   If your camera doesn't reply at all, every command waits the full
   timeout. Drop it to `0.05` or `0.0` (fire-and-forget) in
   `pew_ptz/server.py` where `ViscaIP` is constructed.

## "The phones say *Update waiting*"

Someone changed the app's files in the install folder (for example with
`git pull`) without restarting the controller. Nothing is broken: the running
server keeps serving the version it started with, because it loads its pages
and scripts into memory at startup.

After the meeting, an administrator runs [`scripts\update.ps1`](#updating-the-app).
With nothing new to fetch, it still installs any new dependencies,
re-registers the task, restarts and verifies. A plain restart or reboot is
**not** enough after a `git pull` that changed `requirements.txt`: the new code
would start without its new dependency and keep restarting with exit code 1.
If someone only edited a file by hand, [`scripts\restart.ps1`](#restarting-the-service)
is enough. Open phone pages reload themselves once the controller is back.

The pill only tracks the app's own files (`pew_ptz\`). A pull that changes
only `requirements.txt` shows nothing on the phones, but still needs
`update.ps1`.

## "The server keeps restarting" / "It won't start"

Look at `logs\supervisor.log`. Each line says how the server exited:

- `exit code 0xC0000005 (access violation)` or another `0xC...` code: a crash
  inside a native library (usually Zoom's UI Automation). The supervisor
  restarts it after 1 s, then backs off to 5, 10, 20, 40 and 60 s.
  `logs\server-stderr.log` may have a stack dump.
- `exit code 1`: a Python error; the traceback is in `server.log` or, if it
  happened during startup, in `server-stderr.log` (for example a missing
  dependency after a `git pull` without the installer: run `scripts\update.ps1`).
- `because of a configuration error`: a bad value in `.env` or the
  environment, or a `.env` that isn't UTF-8 text. `server.log` names the
  setting or the file. Fix it, then restart. (Windows PowerShell's `>` and
  `Out-File` write UTF-16, which is accepted; `Set-Content` writes the ANSI
  code page, which isn't if a name has accented letters. Notepad's UTF-8 is
  safest.)
- `because the port is already in use`: something else is listening on port
  8080, usually a second copy of the controller. `netstat -ano | findstr :8080`
  shows the owner's PID. `scripts\restart.ps1` stops every copy and starts one.

### What the task's Last Run Result means

| `LastTaskResult` | Meaning |
|---|---|
| `0x41301` (267009) | Running. |
| `0x41303` (267011) | Never run yet: the operator user hasn't logged on since install. |
| `0x80041320` | Couldn't start: the operator user isn't logged on. It starts at the next logon. |
| `0x4E` (78) | Stopped: configuration error (see `server.log`, or `supervisor.log` if the task's own arguments are wrong). |
| `0x62` (98) | Stopped: port already in use (see `server.log`). |
| `0x41306` (267014) | Stopped from Task Scheduler (End, `Stop-ScheduledTask`, or a script). |
| `0` | The server stopped normally. |
| anything else | The supervisor itself was stopped or died; see `supervisor.log`. |

If `supervisor.log` has nothing new (for example the venv's Python itself
can't start), run the task's command in a console from the install folder to
see the error:

```powershell
cd C:\tools\pew-ptz
.\.venv\Scripts\python.exe -m pew_ptz.supervise --port 8080 --log-dir logs
```

## Restarting the service

Anyone at the booth PC: **sign out and back in as the operator user, or
reboot.** Signing out ends the controller; the task starts it again at logon.

An administrator, from an elevated PowerShell:

```powershell
cd C:\tools\pew-ptz
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\restart.ps1
```

`restart.ps1` stops every running copy (and waits until the port is free),
starts the task, then waits until `/healthz` answers from the new process with
the code that's on disk. It reads the install folder, user and port from the
registered task. Ending and running the task from Task Scheduler works too.

## Updating the app

From an elevated PowerShell:

```powershell
cd C:\tools\pew-ptz
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\update.ps1
```

`update.ps1` checks everything it can before touching the running controller:
that git works, that the branch tracks a remote, that the operator user and
the venv exist, then fetches and lists the incoming commits. Only then does it
stop the controller, fast-forward to exactly those commits, and re-run
`install.ps1`, which installs any new dependencies, re-registers the task,
starts it and confirms the new version is the one answering. If anything fails
after the controller was stopped, it starts the controller again and prints
the commands to roll back. `.\scripts\update.ps1 -CheckOnly` runs the checks
and lists what would come in, without changing anything.

Don't `git pull` on its own while the controller is running. The running
server keeps working on its old version (the phones show *Update waiting*),
but a new dependency only gets installed by `update.ps1` or `install.ps1`.
If you did pull by hand, just run `update.ps1`: with nothing new to fetch it
still installs, restarts and verifies.

**Local edits.** Some fixes on this page involve editing a file in the install
folder (for example swapping the pan directions in `visca.py`). After such an
edit, `restart.ps1` is enough. `update.ps1` keeps local edits when there is
nothing new to fetch, but refuses to update over them when commits are
incoming, because the update could overwrite them. Then stash the edit, update,
and put it back:

```powershell
git -c safe.directory=C:/tools/pew-ptz stash
.\scripts\update.ps1
git -c safe.directory=C:/tools/pew-ptz stash pop
.\scripts\restart.ps1
```

`update.ps1` and `install.ps1` run as administrator and execute code from the
install folder, so only accounts you trust should be able to write to it.

### Upgrading an install from before the supervisor

`update.ps1` doesn't exist on an install that predates it, and the controller
running there doesn't freeze its files, so it must be stopped before the new
files arrive. Do this once, outside meeting times, in an **elevated**
PowerShell. Steps 1 and 2 change nothing, so if either fails the controller
is still running.

```powershell
cd C:\tools\pew-ptz
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
$git = @('-c', 'safe.directory=C:/tools/pew-ptz')   # in case another account owns the folder

# 1. No local edits to tracked files (this must print nothing; see "Local edits" above)
git @git status --porcelain --untracked-files=no
# 2. Fetch the new code (must succeed)
git @git fetch

# 3. Stop the old controller, so it never serves half-updated files
$old = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match '-m\s+pew_ptz' }
if (-not $old) { Write-Warning "No running controller found. Is this shell elevated?" }
$old | ForEach-Object { "Stopping PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 4. Move to exactly the fetched commits, then reinstall, start and verify
git @git merge --ff-only '@{u}'
.\scripts\update.ps1
```

If step 4 fails, the controller is stopped. `Start-ScheduledTask -TaskName
"Pew PTZ Controller"` (the old task works with the old or the new code) or
signing out and back in brings it back; then fix what the error names and run
`.\scripts\update.ps1` again.

Then reload the page once on every phone (or close and reopen the home-screen
app); from then on pages reload themselves after an update. Until this is done,
`restart.ps1` refuses to work on the old-style task and says so.
