#requires -RunAsAdministrator

<#
.SYNOPSIS
  Restarts pew-ptz and waits until the new server is healthy.

.DESCRIPTION
  Stops the scheduled task and every pew-ptz process, starts the task again,
  and waits for http://127.0.0.1:<Port>/healthz to report a fresh server
  running the code that is on disk.

  This does NOT install anything. After a git pull run scripts\update.ps1
  (installs new dependencies and re-registers the task). After editing a
  tracked file by hand, restart.ps1 is enough.

  Settings default to what the registered task was installed with, so a
  deployment installed with -Port / -InstallDir needs no arguments here.

  Non-admin alternative: sign out and back in as the operator user (or
  reboot); the task starts at logon.

  Works in Windows PowerShell 5.1 and PowerShell 7 (elevated).

.PARAMETER InstallDir
  Default: the task's working directory.

.PARAMETER TaskUser
  Default: the task's user.

.PARAMETER Port
  Default: the --port in the task's arguments (else 8080).

.PARAMETER TaskName
  Default: "Pew PTZ Controller"

.PARAMETER TimeoutSec
  How long to wait for /healthz. Default: 60
#>
[CmdletBinding()]
param(
  [string]$InstallDir,
  [string]$TaskUser,
  [int]   $Port,
  [string]$TaskName   = "Pew PTZ Controller",
  [int]   $TimeoutSec = 60
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
$PSNativeCommandUseErrorActionPreference = $false

. (Join-Path $PSScriptRoot "pewctl.ps1")

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK  ($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

$settings = Get-PewTaskSettings -TaskName $TaskName
if (-not $settings) {
  throw "Scheduled task '$TaskName' is not registered. Run scripts\install.ps1 first."
}
if ($settings.IsLegacy) {
  throw ("Task '$TaskName' still uses the old scripts\launch.cmd launcher (installed before restart.ps1 existed). " +
    "Run scripts\update.ps1 (or scripts\install.ps1) once; after that restart.ps1 works.")
}
if (-not $PSBoundParameters.ContainsKey('InstallDir')) { $InstallDir = $settings.InstallDir }
if (-not $PSBoundParameters.ContainsKey('TaskUser'))   { $TaskUser   = $settings.TaskUser }
if (-not $PSBoundParameters.ContainsKey('Port'))       { $Port       = $settings.Port }
if (-not $InstallDir) { throw "Could not tell the install folder from task '$TaskName'; pass -InstallDir." }
$InstallDir = Resolve-PewPath $InstallDir
$logDir = $settings.LogDir
if (-not $logDir -or $PSBoundParameters.ContainsKey('InstallDir')) { $logDir = Join-Path $InstallDir "logs" }
$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"

Write-Note "InstallDir=$InstallDir  TaskUser=$TaskUser  Port=$Port  Task='$TaskName'"

Write-Step "Stopping pew-ptz"
try {
  $stopped = Stop-PewPtz -TaskName $TaskName -Port $Port
} catch {
  Write-Warn "Stop failed; trying to start '$TaskName' again anyway..."
  try { Write-Warn (Start-PewPtz -TaskName $TaskName -TaskUser $TaskUser).Message } catch { Write-Warn "Could not start it: $($_.Exception.Message)" }
  throw
}
if ($stopped) { Write-OK "Stopped" } else { Write-OK "It was not running" }

Write-Step "Starting '$TaskName'"
$startedAfter = Get-PewEpochSeconds
$start = Start-PewPtz -TaskName $TaskName -TaskUser $TaskUser
if ($start.Soft) {
  Write-Warn $start.Message
  Write-Host ""
  Write-Host "Stopped. pew-ptz will start at the next logon of $TaskUser." -ForegroundColor Yellow
  return
}
Write-OK $start.Message

$expectedBuild = Get-PewExpectedBuild -VenvPython $venvPython -InstallDir $InstallDir
if (-not $expectedBuild) { Write-Note "Could not compute the build of the files on disk; skipping the build check." }

Write-Step "Waiting for http://127.0.0.1:$Port/healthz"
$health = Wait-PewPtzHealthy -Port $Port -StartedAfter $startedAfter -ExpectedBuild $expectedBuild `
  -LogDir $logDir -TaskName $TaskName -TimeoutSec $TimeoutSec

Write-Host ""
Write-Host ("OK: pew-ptz restarted - version {0}, build {1}, pid {2}, http://127.0.0.1:{3}" -f `
    $health.Version, $health.Build, $health.Pid, $Port) -ForegroundColor Green
