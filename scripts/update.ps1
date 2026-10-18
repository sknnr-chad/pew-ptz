#requires -RunAsAdministrator

<#
.SYNOPSIS
  Updates pew-ptz to the latest commit of its branch and reinstalls it.

.DESCRIPTION
  1. Preflight, before anything is changed: git is available, InstallDir is
     the root of a git checkout, HEAD is on a branch with an upstream, the
     task user exists, the venv's python runs (else run install.ps1 first,
     it creates the venv), then 'git fetch' and the list of incoming
     commits. Local edits to tracked files stop the update only when commits
     are incoming; with nothing incoming they are kept as they are.
  2. Stops pew-ptz, fast-forwards to exactly the commits that were listed
     (git merge --ff-only '@{u}'), then runs scripts\install.ps1 in this
     same PowerShell, which installs dependencies, re-registers the task,
     starts it and waits until the new build is healthy.

  It runs install.ps1 even when nothing is incoming, so after a manual
  'git pull' run update.ps1: it installs new dependencies and re-registers
  the task, which a plain restart does not. After a local edit of a tracked
  file with no dependency change, scripts\restart.ps1 is enough (update.ps1
  would also keep the edit when nothing is incoming).

  update.ps1 -CheckOnly runs the preflight (including git fetch) and lists
  the incoming commits without stopping or changing anything.

  If anything fails after the stop, it tries to start the controller again
  and prints the commands to roll back to the code that ran before.

  Settings default to what the registered task was installed with (else the
  install.ps1 defaults).

  Works in Windows PowerShell 5.1 and PowerShell 7 (elevated).

.PARAMETER InstallDir
  Default: the task's working directory, else C:\tools\pew-ptz

.PARAMETER TaskUser
  Default: the task's user, else Chapel-AV

.PARAMETER Port
  Default: the --port in the task's arguments, else 8080

.PARAMETER TaskName
  Default: "Pew PTZ Controller"

.PARAMETER CheckOnly
  Run the preflight (including git fetch) and list incoming commits, then stop
  without changing anything.
#>
[CmdletBinding()]
param(
  [string]$InstallDir,
  [string]$TaskUser,
  [int]   $Port,
  [string]$TaskName = "Pew PTZ Controller",
  [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
$PSNativeCommandUseErrorActionPreference = $false

. (Join-Path $PSScriptRoot "pewctl.ps1")

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK  ($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Assert-GitOk($result, [string]$what) {
  if ($result.Code -eq 0) { return }
  if ($result.Text -match 'dubious ownership') { throw (Get-PewGitOwnershipHint -InstallDir $InstallDir) }
  throw "git $what failed (exit $($result.Code)):`n$($result.Text)"
}

function Get-GitLine($result) {
  $lines = @($result.Out | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
  if ($lines.Count -eq 0) { return '' }
  return $lines[$lines.Count - 1]
}

# ---- Settings --------------------------------------------------------------
$settings = Get-PewTaskSettings -TaskName $TaskName
if (-not $PSBoundParameters.ContainsKey('InstallDir')) {
  if ($settings -and $settings.InstallDir) { $InstallDir = $settings.InstallDir } else { $InstallDir = "C:\tools\pew-ptz" }
}
if (-not $PSBoundParameters.ContainsKey('TaskUser')) {
  if ($settings -and $settings.TaskUser) { $TaskUser = $settings.TaskUser } else { $TaskUser = "Chapel-AV" }
}
if (-not $PSBoundParameters.ContainsKey('Port')) {
  if ($settings) { $Port = $settings.Port } else { $Port = 8080 }
}
$InstallDir = Resolve-PewPath $InstallDir
$TaskUser = Get-PewBareUserName $TaskUser
$installer = Join-Path $InstallDir "scripts\install.ps1"
$restarter = Join-Path $InstallDir "scripts\restart.ps1"
$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
$safeDir = $InstallDir -replace '\\', '/'

# ---- Preflight: nothing below changes anything until "Stopping" -----------
Write-Step "Preflight (nothing is changed yet)"
Write-Note "InstallDir=$InstallDir  TaskUser=$TaskUser  Port=$Port  Task='$TaskName'"

if (-not (Get-Command git -ErrorAction Ignore)) {
  throw ("git was not found on PATH in this elevated shell. Install Git for Windows " +
    "(winget install --id Git.Git -e), open a new elevated shell, and re-run.")
}
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml"))) {
  throw "$InstallDir is not a pew-ptz checkout (no pyproject.toml). Pass -InstallDir."
}

# install.ps1 checks these too, but only after the controller is stopped.
try {
  Get-LocalUser -Name $TaskUser -ErrorAction Stop | Out-Null
} catch {
  throw "Local user '$TaskUser' not found, so the task cannot be registered for it. Pass -TaskUser."
}
Write-OK "User '$TaskUser' exists"

$venv = Get-PewVenvPythonInfo -VenvPython $venvPython
if (-not $venv.Ok) {
  throw ("The virtual environment is missing or broken: $($venv.Detail).`n" +
    "Run scripts\install.ps1 first (it creates the venv, installing Python 3.14 if needed), " +
    "then re-run update.ps1. Nothing was stopped or changed.")
}
Write-OK "venv runs: $($venv.Detail)"

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('rev-parse', '--show-prefix')
if ($r.Code -ne 0 -and $r.Text -match 'not a git repository') {
  throw ("$InstallDir is not a git checkout, so update.ps1 cannot update it. " +
    "If it was installed from a copy or zip, replace the files and run scripts\install.ps1.")
}
Assert-GitOk $r 'rev-parse --show-prefix'
$prefix = Get-GitLine $r
if ($prefix) {
  throw "$InstallDir is inside a git checkout but is not its root (it is '$prefix' in that checkout)."
}

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD')
if ($r.Code -ne 0) {
  if ($r.Text -match 'dubious ownership') { throw (Get-PewGitOwnershipHint -InstallDir $InstallDir) }
  throw ("HEAD is detached (not on a branch), so there is nothing to update to. Check out the branch to follow, e.g.:`n" +
    "    git -C `"$InstallDir`" checkout main")
}
$branch = Get-GitLine $r

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
if ($r.Code -ne 0) {
  if ($r.Text -match 'dubious ownership') { throw (Get-PewGitOwnershipHint -InstallDir $InstallDir) }
  throw ("Branch '$branch' has no upstream, so there is nothing to update from. Set one, e.g.:`n" +
    "    git -C `"$InstallDir`" branch --set-upstream-to=origin/$branch")
}
$upstream = Get-GitLine $r

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('rev-parse', 'HEAD')
Assert-GitOk $r 'rev-parse HEAD'
$preSha = Get-GitLine $r
$preShort = $preSha.Substring(0, [Math]::Min(12, $preSha.Length))

Write-Step "Fetching ($branch tracks $upstream)"
$r = Invoke-Git -InstallDir $InstallDir -Arguments @('fetch')
Assert-GitOk $r 'fetch'

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('log', '--oneline', '--no-decorate', 'HEAD..@{u}')
Assert-GitOk $r "log 'HEAD..@{u}'"
$incoming = @($r.Out | Where-Object { "$_".Trim() })

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('rev-list', '--count', '@{u}..HEAD')
Assert-GitOk $r "rev-list '@{u}..HEAD'"
$ahead = 0
[void][int]::TryParse((Get-GitLine $r), [ref]$ahead)
if ($ahead -gt 0 -and $incoming.Count -gt 0) {
  throw ("Branch '$branch' has $ahead local commit(s) that are not on $upstream, and $upstream has " +
    "$($incoming.Count) new commit(s); a fast-forward is impossible. Sort it out by hand " +
    "(git -C `"$InstallDir`" status), then re-run update.ps1.")
}
if ($ahead -gt 0) { Write-Note "$branch has $ahead local commit(s) that are not on $upstream; they are kept." }

if ($incoming.Count -gt 0) {
  Write-OK "$($incoming.Count) new commit(s) on ${upstream}:"
  foreach ($c in $incoming) { Write-Host "      $c" }
} else {
  Write-OK "No new commits on $upstream. Reinstalling the current code ($preShort) anyway."
}

# Local edits to tracked files only matter when a merge has to run.
$r = Invoke-Git -InstallDir $InstallDir -Arguments @('status', '--porcelain', '--untracked-files=no')
Assert-GitOk $r 'status'
$dirtyFiles = @(foreach ($line in @($r.Out)) {
    $l = "$line"
    if (-not $l.Trim()) { continue }
    if ($l.Length -gt 3) { $l.Substring(3).Trim() } else { $l.Trim() }
  })
if ($dirtyFiles.Count -gt 0) {
  if ($incoming.Count -gt 0) {
    $git = "git -C `"$InstallDir`" -c safe.directory=$safeDir"
    throw ("Tracked files in $InstallDir have local changes, and $upstream has $($incoming.Count) new commit(s) " +
      "to merge, so update.ps1 stops here:`n    " + ($dirtyFiles -join "`n    ") +
      "`nStash them (a local commit would block the fast-forward as well), re-run update.ps1, then re-apply" +
      "`nthe edit with 'git stash pop' and restart.ps1, so it is not lost:" +
      "`n    $git stash" +
      "`n    & `"$PSCommandPath`"" +
      "`n    $git stash pop" +
      "`n    & `"$restarter`"" +
      "`nIf 'stash pop' reports a conflict, the update changed the same lines: make the edit again by hand," +
      "`nthen run restart.ps1." +
      "`nNothing was stopped or changed. Untracked files such as presets.json and logs are fine.")
  }
  Write-Note ("Nothing incoming, so local edits to tracked files are kept: " + ($dirtyFiles -join ', '))
}

if ($CheckOnly) {
  Write-Host ""
  Write-Host "Check only: nothing was changed." -ForegroundColor Green
  return
}

# ---- Apply -------------------------------------------------------------------
$stopAttempted = $false
$ok = $false
$failure = $null
try {
  Write-Step "Stopping pew-ptz"
  $stopAttempted = $true
  $stopped = Stop-PewPtz -TaskName $TaskName -Port $Port
  if ($stopped) { Write-OK "Stopped" } else { Write-OK "It was not running" }

  if ($incoming.Count -gt 0) {
    Write-Step "Fast-forwarding $branch to $upstream"
    $r = Invoke-Git -InstallDir $InstallDir -Arguments @('merge', '--ff-only', '@{u}')
    Assert-GitOk $r "merge --ff-only '@{u}'"
    Write-OK "Now at $(Get-GitLine (Invoke-Git -InstallDir $InstallDir -Arguments @('log', '-1', '--oneline', '--no-decorate')))"
  }

  if (-not (Test-Path -LiteralPath $installer)) { throw "$installer not found after the update." }
  Write-Step "Running $installer"
  & $installer -InstallDir $InstallDir -TaskUser $TaskUser -Port $Port -TaskName $TaskName
  $ok = $true
} catch {
  $failure = $_
  throw
} finally {
  if (-not $ok -and $stopAttempted) {
    Write-Host ""
    if ($failure) { Write-Warn "update.ps1 failed: $(($failure.Exception.Message -split "`n")[0])" }
    Write-Warn "Making sure '$TaskName' is started so the booth is not left without a controller..."
    try {
      Write-Warn (Start-PewPtz -TaskName $TaskName -TaskUser $TaskUser).Message
    } catch {
      Write-Warn "Could not start it: $($_.Exception.Message)"
    }
    $nowSha = Get-GitLine (Invoke-Git -InstallDir $InstallDir -Arguments @('rev-parse', 'HEAD'))
    if ($nowSha -and $nowSha -ne $preSha) {
      Write-Warn "The code was updated from $preShort. To roll back to the version that ran before, in an elevated shell:"
      Write-Host "    git -C `"$InstallDir`" -c safe.directory=$safeDir reset --hard $preSha"
      Write-Host "    & `"$installer`" -InstallDir `"$InstallDir`" -TaskUser `"$TaskUser`" -Port $Port -TaskName `"$TaskName`""
    } else {
      Write-Warn "The code was not changed (still $preShort). Fix the problem above, then re-run scripts\update.ps1."
    }
  }
}

$r = Invoke-Git -InstallDir $InstallDir -Arguments @('rev-parse', 'HEAD')
$newShort = Get-GitLine $r
if ($newShort.Length -gt 12) { $newShort = $newShort.Substring(0, 12) }
Write-Host ""
if ($incoming.Count -gt 0) {
  Write-Host "Update complete: $preShort -> $newShort ($($incoming.Count) commit(s))." -ForegroundColor Green
} else {
  Write-Host "Reinstall complete at $newShort (no new commits)." -ForegroundColor Green
}
Write-Host "Phones that had the controller open: reload the page (or close and reopen the home-screen app) if it does not refresh by itself."
