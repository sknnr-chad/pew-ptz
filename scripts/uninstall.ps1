#requires -RunAsAdministrator

<#
.SYNOPSIS
  Removes the pew-ptz scheduled task, firewall rule, and shortcut.
  Leaves the source tree and logs in place - delete the InstallDir manually
  if you want a clean slate.

.DESCRIPTION
  Settings default to what the registered task was installed with (else the
  install.ps1 defaults). Even if stopping the controller fails, the task,
  firewall rule, shortcut and an old scripts\launch.cmd are still removed.

  Works in Windows PowerShell 5.1 and PowerShell 7 (elevated).
#>
[CmdletBinding()]
param(
  [string]$InstallDir,
  [string]$TaskUser,
  [int]   $Port,
  [string]$TaskName = "Pew PTZ Controller"
)
$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"

. (Join-Path $PSScriptRoot "pewctl.ps1")

$settings = $null
try { $settings = Get-PewTaskSettings -TaskName $TaskName } catch { }
if (-not $PSBoundParameters.ContainsKey('InstallDir')) {
  if ($settings -and $settings.InstallDir) { $InstallDir = $settings.InstallDir } else { $InstallDir = "C:\tools\pew-ptz" }
}
if (-not $PSBoundParameters.ContainsKey('TaskUser')) {
  if ($settings -and $settings.TaskUser) { $TaskUser = $settings.TaskUser } else { $TaskUser = "Chapel-AV" }
}
if (-not $PSBoundParameters.ContainsKey('Port')) {
  if ($settings) { $Port = $settings.Port } else { $Port = 8080 }
}

Write-Host "==> Stopping pew-ptz" -ForegroundColor Cyan
try {
  $null = Stop-PewPtz -TaskName $TaskName -Port $Port
} catch {
  Write-Warning "Could not stop pew-ptz cleanly (continuing with the uninstall):`n$($_.Exception.Message)"
}

Write-Host "==> Unregistering scheduled task '$TaskName'" -ForegroundColor Cyan
foreach ($t in @(Get-ScheduledTask -TaskName $TaskName -ErrorAction Ignore)) {
  try {
    Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false -ErrorAction Stop
  } catch {
    Write-Warning "Could not unregister '$($t.TaskName)': $($_.Exception.Message)"
  }
}

Write-Host "==> Removing firewall rule" -ForegroundColor Cyan
# install.ps1 always names the rule "Pew PTZ Controller (TCP <port>)"; older
# uninstallers looked for "<TaskName> (TCP <port>)". Remove both.
$ruleNames = @(@("Pew PTZ Controller (TCP $Port)", "$TaskName (TCP $Port)") | Select-Object -Unique)
foreach ($ruleName in $ruleNames) {
  Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

Write-Host "==> Removing desktop shortcut" -ForegroundColor Cyan
$lnk = "C:\Users\$TaskUser\Desktop\Pew PTZ.url"
if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force }

$launcher = Join-Path $InstallDir "scripts\launch.cmd"
if (Test-Path -LiteralPath $launcher) {
  Write-Host "==> Removing obsolete $launcher" -ForegroundColor Cyan
  Remove-Item -LiteralPath $launcher -Force
}

Write-Host ""
Write-Host "Done. Source tree left at $InstallDir (delete manually if desired)." -ForegroundColor Green
