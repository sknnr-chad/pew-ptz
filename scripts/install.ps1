#requires -RunAsAdministrator

<#
.SYNOPSIS
  One-shot installer for pew-ptz on Windows.

.DESCRIPTION
  - Reuses <InstallDir>\.venv when its python runs (whatever Python it was
    made with); only otherwise ensures Python 3.14 (winget:
    Python.Python.3.14) and creates the venv with it
  - Stops any running pew-ptz (old or new style), then installs the hash-pinned
    dependencies and the package itself (editable, so the checkout is the code)
  - Adds a Windows Firewall rule for inbound TCP <Port> (Private profile)
  - Registers a Scheduled Task that runs at logon of the operator user:
    pythonw.exe -m pew_ptz.supervise, which keeps the server running and
    restarts it if it crashes (no console window)
  - Drops a desktop shortcut to http://localhost:<Port>
  - Starts the task and waits until /healthz reports the new build

  Re-run safely: each step is idempotent. If anything fails after the running
  controller was stopped, the installer tries to start whatever task is
  registered so the booth is not left without a controller.

  Defaults match the chapel deployment (C:\tools\pew-ptz, user "Chapel-AV").
  Override with -InstallDir / -TaskUser / -Port for your own deployment.

  Works in Windows PowerShell 5.1 and PowerShell 7 (elevated).

.PARAMETER InstallDir
  Where the app lives. Default: C:\tools\pew-ptz

.PARAMETER TaskUser
  The local user account that auto-logs into the host PC. Default: Chapel-AV

.PARAMETER Port
  HTTP port the controller binds to. Default: 8080

.PARAMETER TaskName
  Name shown in Task Scheduler. Default: "Pew PTZ Controller"

#>
[CmdletBinding()]
param(
  [string]$InstallDir = "C:\tools\pew-ptz",
  [string]$TaskUser   = "Chapel-AV",
  [int]   $Port       = 8080,
  [string]$TaskName   = "Pew PTZ Controller"
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
$PSNativeCommandUseErrorActionPreference = $false

. (Join-Path $PSScriptRoot "pewctl.ps1")

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK  ($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Get-Python314 {
  # Path of the Python 3.14 interpreter via the py launcher, or $null.
  $ErrorActionPreference = 'Continue'
  if (-not (Get-Command py -ErrorAction Ignore)) { return $null }
  try { $out = & py -3.14 -c "import sys; print(sys.executable)" 2>$null } catch { return $null }
  if ($LASTEXITCODE -ne 0) { return $null }
  $lines = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
  if ($lines.Count -eq 0) { return $null }
  return $lines[$lines.Count - 1]
}

# ---- Sanity checks (no changes yet) -------------------------------------
Write-Step "Verifying environment"
$InstallDir = Resolve-PewPath $InstallDir
if (-not (Test-Path -LiteralPath $InstallDir)) {
  throw "InstallDir '$InstallDir' does not exist. Copy/clone the repo there first."
}
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml"))) {
  throw "pyproject.toml not found in $InstallDir. Are you sure the repo is here?"
}
Write-OK "Repo found at $InstallDir"

try { Get-LocalUser -Name $TaskUser | Out-Null; Write-OK "User '$TaskUser' exists" }
catch { throw "Local user '$TaskUser' not found. Create it first or pass -TaskUser." }

# ---- Paths ---------------------------------------------------------------
$venvDir    = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPyw    = Join-Path $venvDir "Scripts\pythonw.exe"
$logDir     = Join-Path $InstallDir "logs"
$launcher   = Join-Path $InstallDir "scripts\launch.cmd"   # obsolete pre-supervisor wrapper
$lockFile   = Join-Path $InstallDir "requirements.txt"

# ---- Virtual environment (Python 3.14 only if it must be created) --------
Write-Step "Checking the virtual environment at $venvDir"
$venv = Get-PewVenvPythonInfo -VenvPython $venvPython
if ($venv.Ok) {
  Write-OK "venv already exists and runs: $($venv.Detail)"
} else {
  if (Test-Path -LiteralPath $venvPython) {
    Write-Warn "The venv exists but does not run ($($venv.Detail)); creating it again with Python 3.14."
  } else {
    Write-Note "No venv yet."
  }

  Write-Step "Ensuring Python 3.14 is installed (via winget)"
  $py314 = Get-Python314
  if (-not $py314) {
    Write-Note "Python 3.14 not found, installing via winget..."
    & winget install --id Python.Python.3.14 -e --source winget `
      --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw "winget install of Python.Python.3.14 failed (exit $LASTEXITCODE)" }
    $py314 = Get-Python314
    if (-not $py314) { throw "Python 3.14 installed but 'py -3.14' still cannot find it. Open a new shell and re-run." }
  }
  Write-OK "Python 3.14: $py314"

  Write-Step "Creating virtual environment at $venvDir"
  & $py314 -m venv $venvDir
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
  $venv = Get-PewVenvPythonInfo -VenvPython $venvPython
  if (-not $venv.Ok) { throw "The new venv does not run ($($venv.Detail)). Delete $venvDir and re-run." }
  Write-OK "venv created: $($venv.Detail)"
}

# Everything from here on runs with the controller stopped. If any step fails,
# the finally block below starts whatever task is registered again.
$stopFinished     = $false
$stoppedSomething = $false
$done             = $false
$softStart        = $false
$health           = $null
$failure          = $null
try {
  Write-Step "Stopping any running instance"
  $stoppedSomething = Stop-PewPtz -TaskName $TaskName -Port $Port
  $stopFinished = $true
  if ($stoppedSomething) { Write-OK "Stopped" } else { Write-OK "Nothing was running" }

  Write-Step "Installing pew-ptz into venv"
  & $venvPython -m pip install --upgrade pip --disable-pip-version-check | Out-Null
  # Dependencies come from the hash-pinned lockfile so every install gets the
  # exact versions that were tested; the package itself is a separate
  # editable install (no deps), so the checkout in InstallDir is the code
  # that runs and version/build checks look at the right files.
  & $venvPython -m pip install --disable-pip-version-check --require-hashes -r $lockFile
  if ($LASTEXITCODE -ne 0) { throw "pip install of locked dependencies failed (exit $LASTEXITCODE)" }
  & $venvPython -m pip install --disable-pip-version-check --no-deps -e $InstallDir
  if ($LASTEXITCODE -ne 0) { throw "pip install of pew-ptz failed (exit $LASTEXITCODE)" }
  Write-OK "Package installed"

  $expectedBuild = Get-PewExpectedBuild -VenvPython $venvPython -InstallDir $InstallDir
  if ($expectedBuild) {
    Write-OK "Build on disk: $expectedBuild"
  } else {
    Write-Note "Could not compute the build of the files on disk; the build check will be skipped."
  }

  # ---- Clean up leftovers from the retired PIN-lock feature --------------
  foreach ($stale in @("auth.json", "auth.json.tmp")) {
    $path = Join-Path $InstallDir $stale
    if (Test-Path -LiteralPath $path) {
      Remove-Item -LiteralPath $path -Force
      Write-Note "Removed obsolete $path"
    }
  }

  Write-Step "Creating logs directory"
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
  $acl = Get-Acl -LiteralPath $logDir
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $TaskUser, "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
  $acl.SetAccessRule($rule)
  Set-Acl -LiteralPath $logDir -AclObject $acl
  Write-OK "Logs at $logDir (writable by $TaskUser)"

  # ---- Firewall rule (Private profile only) -----------------------------
  Write-Step "Adding Windows Firewall rule for TCP $Port (Private profile)"
  $ruleName = "Pew PTZ Controller (TCP $Port)"
  Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Private -Enabled True | Out-Null
  Write-OK "Firewall rule added"

  # ---- Scheduled Task ----------------------------------------------------
  Write-Step "Registering Scheduled Task '$TaskName'"
  if (-not (Test-Path -LiteralPath $venvPyw)) { throw "$venvPyw not found; the venv looks broken. Delete $venvDir and re-run." }

  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser

  # Run only when the user is logged on (CRITICAL: pynput needs a desktop session)
  $principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited

  # The supervisor is the action process itself, so the task shows "Running"
  # for the controller's whole life: Stop/End really stop it and IgnoreNew
  # blocks duplicates. Priority 5 = normal (the default 7 is below normal,
  # which every child would inherit, next to a CPU-hungry Zoom).
  # RestartCount/RestartInterval only cover "the task failed to launch"; the
  # supervisor does the crash restarts.
  $settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew `
    -Priority 5

  $supervisorArgs = '-m pew_ptz.supervise --port {0} --log-dir "{1}"' -f $Port, $logDir
  $action = New-ScheduledTaskAction -Execute $venvPyw -Argument $supervisorArgs -WorkingDirectory $InstallDir
  $task   = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
  $task.Description = "pew-ptz: phone-based PTZ + Zoom Alt+V/Alt+A toggles (supervised; logs in $logDir)"

  # -Force replaces an existing definition in place, so a failure here leaves
  # the previous task untouched.
  Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
  Write-OK "Task registered (at logon of $TaskUser): $venvPyw $supervisorArgs"

  # The old task ran scripts\launch.cmd; it is obsolete only now that the new
  # task is registered.
  if (Test-Path -LiteralPath $launcher) {
    Remove-Item -LiteralPath $launcher -Force
    Write-Note "Removed obsolete $launcher"
  }

  # ---- Desktop shortcut --------------------------------------------------
  Write-Step "Creating desktop shortcut for $TaskUser"
  $desktop = "C:\Users\$TaskUser\Desktop"
  if (Test-Path -LiteralPath $desktop) {
    $lnk = Join-Path $desktop "Pew PTZ.url"
    @"
[InternetShortcut]
URL=http://localhost:$Port
"@ | Set-Content -Encoding ASCII -LiteralPath $lnk
    Write-OK "Shortcut: $lnk"
  } else {
    Write-Note "Desktop folder for $TaskUser not found yet (user has never logged in?). Skipping."
  }

  # ---- Start and verify --------------------------------------------------
  Write-Step "Starting the task"
  $startedAfter = Get-PewEpochSeconds
  $start = Start-PewPtz -TaskName $TaskName -TaskUser $TaskUser
  if ($start.Soft) {
    $softStart = $true
    Write-Warn $start.Message
  } else {
    Write-OK $start.Message
    Write-Step "Waiting for http://127.0.0.1:$Port/healthz to report the new server"
    $health = Wait-PewPtzHealthy -Port $Port -StartedAfter $startedAfter -ExpectedBuild $expectedBuild `
      -LogDir $logDir -TaskName $TaskName -TimeoutSec 60
    Write-OK ("healthy: version {0}, build {1}, pid {2}" -f $health.Version, $health.Build, $health.Pid)
  }
  $done = $true
} catch {
  $failure = $_
  throw
} finally {
  if (-not $done -and ($stoppedSomething -or -not $stopFinished)) {
    Write-Host ""
    if ($failure) { Write-Warn "install.ps1 failed: $(($failure.Exception.Message -split "`n")[0])" }
    Write-Warn "This run stopped the controller and did not finish. Making sure '$TaskName' is started so the booth is not left without it..."
    try {
      $restart = Start-PewPtz -TaskName $TaskName -TaskUser $TaskUser
      Write-Warn $restart.Message
    } catch {
      Write-Warn "Could not start it: $($_.Exception.Message)"
    }
    Write-Warn "Fix the problem above and re-run scripts\install.ps1. Logs: $logDir"
  }
}

Write-Host ""
if ($softStart) {
  Write-Host "Installed. pew-ptz will start at the next logon of $TaskUser." -ForegroundColor Green
} else {
  Write-Host ("Installed and running: pew-ptz {0} (build {1}, pid {2})" -f $health.Version, $health.Build, $health.Pid) -ForegroundColor Green
}
Write-Host "  Logs:      $logDir\server.log  (supervisor: supervisor.log)"
Write-Host "  URL:       http://localhost:$Port  (or http://<this-pc-lan-ip>:$Port from the phone)"
Write-Host "  Task:      '$TaskName' (Task Scheduler -> Task Scheduler Library)"
Write-Host "  Restart:   scripts\restart.ps1    Update: scripts\update.ps1"
Write-Host "  Uninstall: scripts\uninstall.ps1"
