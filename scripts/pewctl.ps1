<#
.SYNOPSIS
  Shared helpers for install.ps1, restart.ps1, update.ps1 and uninstall.ps1.

.DESCRIPTION
  Dot-source it:   . (Join-Path $PSScriptRoot "pewctl.ps1")

  This file only defines functions. Dot-sourcing it changes nothing else (no
  preference variables, no output), so tests can load it safely.

  Works in Windows PowerShell 5.1 and PowerShell 7. Keep it pure ASCII:
  5.1 reads a BOM-less file as Windows-1252, and a single em dash turns
  into a stray quote character that breaks string parsing.

  Process model: the scheduled task runs
    <InstallDir>\.venv\Scripts\pythonw.exe -m pew_ptz.supervise --port N --log-dir "..."
  which makes four pythonw.exe processes (venv launcher -> supervisor ->
  venv launcher -> server), all with the same kind of command line. Killing
  any ancestor takes its descendants down within milliseconds, so a PID that
  was listed a moment ago may already be gone. Everywhere below, "already
  gone" counts as success.

  Every health probe uses http://127.0.0.1:<port>, never "localhost": the
  server is IPv4-only, and "localhost" tries ::1 first and wastes ~2 s.
#>

function Get-PewEpochSeconds {
  # Current time as unix epoch seconds (what /healthz "started_at" uses).
  [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Get-PewBareUserName {
  # "HOST\Chapel-AV" -> "Chapel-AV"; a SID is translated to its account name first.
  param([string]$UserName)
  if (-not $UserName) { return $UserName }
  $name = $UserName.Trim()
  if ($name -match '^S-1-\d+(-\d+)+$') {
    try {
      $sid = New-Object System.Security.Principal.SecurityIdentifier($name)
      $name = $sid.Translate([System.Security.Principal.NTAccount]).Value
    } catch { }
  }
  $slash = $name.LastIndexOf('\')
  if ($slash -ge 0) { $name = $name.Substring($slash + 1) }
  return $name
}

function Get-PewNetstatLines {
  # Raw 'netstat -ano' output. No "-p tcp": that would hide IPv6 rows, and a
  # foreign program listening on [::]:<port> must be visible too.
  $ErrorActionPreference = 'Continue'
  $PSNativeCommandUseErrorActionPreference = $false
  $exe = Join-Path $env:SystemRoot 'System32\NETSTAT.EXE'
  if (-not (Test-Path -LiteralPath $exe)) { $exe = 'netstat.exe' }
  $out = & $exe -ano 2>&1
  if ($LASTEXITCODE -ne 0) { throw "netstat -ano failed (exit $LASTEXITCODE): $out" }
  foreach ($line in @($out)) { "$line" }
}

function Get-PewListeners {
  <#
    One object per listening TCP socket on $Port: { LocalAddress; Pid; IPv6 }.

    Parses 'netstat -ano' (IPv4 and IPv6) by whitespace tokens and never reads
    the State column, because Windows localizes it ("LISTENING", "ABHOEREN",
    ...). A row is a listener when Proto is TCP, the local address ends in
    ":<Port>" and the foreign address is 0.0.0.0:0 or [::]:0. TIME_WAIT,
    ESTABLISHED and FIN_WAIT rows (which keep a dead PID or show :<Port> in the
    foreign column) and other ports such as :18080 are ignored. The PID is the
    last token. -NetstatLines lets tests inject sample text.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][int]$Port,
    [string[]]$NetstatLines
  )
  if (-not $PSBoundParameters.ContainsKey('NetstatLines')) {
    $NetstatLines = @(Get-PewNetstatLines)
  }
  $suffix = ":$Port"
  foreach ($chunk in @($NetstatLines)) {
    foreach ($line in ("$chunk" -split '\r?\n')) {
      $t = @(-split $line)
      if ($t.Count -lt 4) { continue }
      if ($t[0] -ne 'TCP' -and $t[0] -ne 'TCPv6') { continue }
      $local = $t[1]
      $remote = $t[2]
      if (-not $local.EndsWith($suffix)) { continue }
      if ($remote -ne '0.0.0.0:0' -and $remote -ne '[::]:0') { continue }
      $ownerPid = 0
      if (-not [int]::TryParse($t[$t.Count - 1], [ref]$ownerPid)) { continue }
      [pscustomobject]@{
        LocalAddress = $local
        Pid          = $ownerPid
        IPv6         = $local.StartsWith('[')
      }
    }
  }
}

function Get-PewProcessPattern {
  <#
    The CommandLine regex for pew-ptz processes, shared by Get-PewProcesses,
    Stop-PewPtz and Test-PewCommandLine.
    Default: every pew-ptz process (supervisor, server, their venv launchers,
    and the old pre-supervisor "pythonw.exe -m pew_ptz"). "-m pew_ptz" must be
    followed by whitespace, a quote or the end of the line, so "-m pew_ptzx",
    "-m pytest" or "-c ""import pew_ptz""" never match.
    -Supervisor: the supervisor and its venv launcher only.
  #>
  param([switch]$Supervisor)
  if ($Supervisor) { return '-m\s+pew_ptz\.supervise' }
  return '-m\s+pew_ptz(\.supervise)?(\s|"|$)'
}

function Test-PewCommandLine {
  # Pure: $true when $CommandLine is a pew-ptz process by the default
  # Get-PewProcesses match; with -Supervisor, when it is also a supervisor
  # (what Stop-PewPtz kills first).
  param([string]$CommandLine, [switch]$Supervisor)
  if (-not $CommandLine) { return $false }
  if ($CommandLine -notmatch (Get-PewProcessPattern)) { return $false }
  if ($Supervisor) { return ($CommandLine -match (Get-PewProcessPattern -Supervisor)) }
  return $true
}

function Get-PewProcesses {
  <#
    Win32_Process objects for python.exe / pythonw.exe whose CommandLine
    matches $Pattern (default: Get-PewProcessPattern, every pew-ptz process).
    Tests pass a unique marker as -Pattern so they never touch a developer's
    real pew_ptz. CommandLine is only readable for other users' processes
    when elevated, which install/update/restart/uninstall always are.
  #>
  [CmdletBinding()]
  param([string]$Pattern = (Get-PewProcessPattern))
  $procs = Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop
  foreach ($p in @($procs)) {
    if ($p.CommandLine -and ($p.CommandLine -match $Pattern)) { $p }
  }
}

function Get-PewProcessOwner {
  param($Process)
  try {
    $o = Invoke-CimMethod -InputObject $Process -MethodName GetOwner -ErrorAction Stop
    if ($o.ReturnValue -eq 0) {
      if ($o.Domain) { return "$($o.Domain)\$($o.User)" }
      return "$($o.User)"
    }
  } catch { }
  return '<unknown owner>'
}

function Format-PewProcess {
  param($Process)
  $cmd = "$($Process.CommandLine)"
  if (-not $cmd) { $cmd = '<command line not readable; run elevated>' }
  'PID {0} {1} (owner {2}): {3}' -f $Process.ProcessId, $Process.Name, (Get-PewProcessOwner $Process), $cmd
}

function Stop-PewProcessQuietly {
  # Kills one process. Returns $null on success or when the process is already
  # gone (its parent's job object took it down first); returns the error text
  # for a real failure such as access denied.
  param([int]$ProcessId, $CreationDate)
  $p = Get-Process -Id $ProcessId -ErrorAction Ignore
  if (-not $p) { return $null }
  if ($CreationDate) {
    # Guard against PID reuse: if this PID now belongs to a process that started
    # at a different time, ours is gone and this one is not ours to kill.
    $started = $null
    try { $started = $p.StartTime } catch { }
    if ($started -and ([math]::Abs(($started - $CreationDate).TotalSeconds) -gt 2)) { return $null }
  }
  try {
    $p.Kill()
    return $null
  } catch {
    $msg = $_.Exception.Message
    if ($_.Exception.InnerException) { $msg = $_.Exception.InnerException.Message }
    if (-not (Get-Process -Id $ProcessId -ErrorAction Ignore)) { return $null }
    try { if ($p.HasExited) { return $null } } catch { }
    return $msg
  }
}

function Get-PewTaskResultText {
  # Human meaning of a scheduled task LastTaskResult (UInt32).
  param($Result)
  if ($null -eq $Result) { return 'unknown' }
  $hex = '0x{0:X8}' -f $Result
  switch ($hex) {
    '0x00000000' { return "$hex (exited normally)" }
    '0x0000004E' { return "$hex = 78: configuration error (bad .env value or command-line argument); see server.log / supervisor.log" }
    '0x00000062' { return "$hex = 98: port already in use (another pew-ptz or another program holds it); see supervisor.log" }
    '0x00041301' { return "$hex (task is running)" }
    '0x00041303' { return "$hex (task has not run yet)" }
    '0x00041306' { return "$hex (task was terminated)" }
    '0x80041320' { return "$hex (the task user is not logged on)" }
    '0x800710E0' { return "$hex (start refused: the task user is not logged on)" }
    '0xFFFFFFFF' { return "$hex (process was killed)" }
  }
  return $hex
}

function Get-PewTask {
  # Full task object (Actions, Principal, ...) from the root folder, where
  # install.ps1 registers it, or $null. Slow (~1.5 s), so polling loops use
  # Get-PewTaskState instead.
  param([Parameter(Mandatory = $true)][string]$TaskName)
  Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Ignore | Select-Object -First 1
}

function Get-PewTaskState {
  <#
    'Running', 'Ready', 'Queued', 'Disabled' or 'Unknown' for a task in the
    root folder (where install.ps1 registers it), or $null when there is no
    such task. Uses the Task Scheduler COM API, which answers in milliseconds;
    Get-ScheduledTask takes ~1.5 s per call. Falls back to the cmdlet if COM
    is unavailable.
  #>
  param([Parameter(Mandatory = $true)][string]$TaskName)
  try {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $t = $svc.GetFolder('\').GetTask($TaskName)
    switch ([int]$t.State) {
      1 { return 'Disabled' }
      2 { return 'Queued' }
      3 { return 'Ready' }
      4 { return 'Running' }
      default { return 'Unknown' }
    }
  } catch {
    # 0x80070002 (file not found) = no such task.
    if ($_.Exception -is [System.IO.FileNotFoundException] -or $_.Exception.HResult -eq -2147024894 -or
        ($_.Exception.InnerException -and $_.Exception.InnerException.HResult -eq -2147024894)) {
      return $null
    }
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Ignore | Select-Object -First 1
    if ($task) { return "$($task.State)" }
    return $null
  }
}

function Stop-PewPtz {
  <#
    Stops pew-ptz completely and returns $true when something was running.

    1. Stop-ScheduledTask, only if the task is registered (a fresh install has none).
    2. Until the deadline: list matching processes again, kill supervisors first
       (so nothing gets relaunched), then roots, then the rest. A process that is
       already gone counts as success; real errors (access denied) are collected.
    3. Done when no matching process is left AND no listener on $Port belongs to
       a matching or already-dead PID. A listener owned by some other program is
       reported straight away (waiting will not help).
    4. Waits (bounded) for the task to leave the Running state, so the next
       Start-ScheduledTask is not swallowed by MultipleInstances IgnoreNew.

    Throws at the deadline, naming each survivor (PID, owner, command line).
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][int]$Port,
    [int]$TimeoutSec = 20,
    [string]$Pattern = (Get-PewProcessPattern),
    [string]$SupervisorPattern = (Get-PewProcessPattern -Supervisor)
  )
  $ErrorActionPreference = 'Stop'
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $stoppedAny = $false
  $killErrors = @{}
  $notes = @()

  $taskState = Get-PewTaskState -TaskName $TaskName
  if ($taskState) {
    if ($taskState -eq 'Running') {
      $stoppedAny = $true
      try {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop | Out-Null
      } catch {
        $notes += "Stop-ScheduledTask '$TaskName' failed: $($_.Exception.Message)"
      }
    }
  }

  $foreignPasses = 0
  while ($true) {
    $procs = @(Get-PewProcesses -Pattern $Pattern)
    $blocking = @()
    $foreign = @()
    if ($procs.Count -gt 0) {
      $stoppedAny = $true
      $ids = @{}
      foreach ($p in $procs) { $ids[[int]$p.ProcessId] = $true }
      $ordered = @($procs | Sort-Object -Property `
          @{ Expression = { if ($_.CommandLine -match $SupervisorPattern) { 0 } else { 1 } } },
          @{ Expression = { if ($ids.ContainsKey([int]$_.ParentProcessId)) { 1 } else { 0 } } })
      foreach ($p in $ordered) {
        $err = Stop-PewProcessQuietly -ProcessId ([int]$p.ProcessId) -CreationDate $p.CreationDate
        if ($err) { $killErrors[[int]$p.ProcessId] = $err }
      }
    } else {
      foreach ($l in @(Get-PewListeners -Port $Port)) {
        $owner = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$($l.Pid)" -ErrorAction Ignore
        if (-not $owner) {
          $blocking += "PID $($l.Pid) listening on $($l.LocalAddress) (process already exited)"
        } elseif ($owner.CommandLine -and ($owner.CommandLine -match $Pattern) -and
                  ($owner.Name -eq 'python.exe' -or $owner.Name -eq 'pythonw.exe')) {
          $blocking += "$(Format-PewProcess $owner) listening on $($l.LocalAddress)"
        } else {
          $foreign += "$(Format-PewProcess $owner) listening on $($l.LocalAddress)"
        }
      }
      if ($blocking.Count -eq 0) {
        if ($foreign.Count -gt 0) {
          $foreignPasses++
          if ($foreignPasses -ge 2) {
            throw ("pew-ptz is stopped, but port $Port is held by another program:`n  " +
              ($foreign -join "`n  ") +
              "`nStop that program, or install pew-ptz on another port with -Port.")
          }
        } else {
          break
        }
      }
    }
    if ((Get-Date) -ge $deadline) {
      Start-Sleep -Milliseconds 300
      $lines = @("Could not stop pew-ptz within $TimeoutSec s.")
      foreach ($p in @(Get-PewProcesses -Pattern $Pattern)) {
        $lines += "  still running: $(Format-PewProcess $p)"
        if ($killErrors.ContainsKey([int]$p.ProcessId)) {
          $lines += "    kill failed: $($killErrors[[int]$p.ProcessId])"
        }
      }
      foreach ($b in $blocking) { $lines += "  port ${Port}: $b" }
      foreach ($f in $foreign) { $lines += "  port ${Port}: $f" }
      $lines += $notes
      throw ($lines -join "`n")
    }
    Start-Sleep -Milliseconds 300
  }

  if ($taskState) {
    # Processes are gone; give Task Scheduler a moment to mark the instance
    # finished. Bounded and not fatal: Start-PewPtz copes with a stale state.
    $taskDeadline = (Get-Date).AddSeconds(10)
    $retried = $false
    while ($true) {
      if ((Get-PewTaskState -TaskName $TaskName) -ne 'Running') { break }
      if (-not $retried -and ((Get-Date) -ge $taskDeadline.AddSeconds(-5))) {
        try { Stop-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop | Out-Null } catch { }
        $retried = $true
      }
      if ((Get-Date) -ge $taskDeadline) {
        Write-Warning "Task '$TaskName' still shows 'Running' although no pew-ptz process is left."
        break
      }
      Start-Sleep -Milliseconds 250
    }
  }
  foreach ($n in $notes) { Write-Warning $n }
  return [bool]$stoppedAny
}

function Test-PewUserSession {
  # $true when $UserName owns an explorer.exe (has a desktop session), $false
  # when nobody visible does, $null when owners could not be read.
  param([Parameter(Mandatory = $true)][string]$UserName)
  $bare = Get-PewBareUserName $UserName
  $unknown = $false
  foreach ($e in @(Get-CimInstance -ClassName Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Ignore)) {
    try {
      $o = Invoke-CimMethod -InputObject $e -MethodName GetOwner -ErrorAction Stop
      if ($o.ReturnValue -eq 0) {
        if ("$($o.User)" -eq $bare) { return $true }
      } else {
        $unknown = $true
      }
    } catch {
      $unknown = $true
    }
  }
  if ($unknown) { return $null }
  return $false
}

function New-PewStartStatus {
  param([bool]$Started, [bool]$Soft, [string]$Message)
  [pscustomobject]@{ Started = $Started; Soft = $Soft; Message = $Message }
}

function Test-PewNotLoggedOnResult {
  # $true for the Task Scheduler codes that mean "the task user is not logged
  # on": 0x80041320 (SCHED_E_USER_NOT_LOGGED_ON) and 0x800710E0 (request
  # refused, what an Interactive task gets when started with nobody logged
  # on). Takes a UInt32 LastTaskResult or an Int32 HResult.
  param($Result)
  if ($null -eq $Result) { return $false }
  $hex = ''
  try { $hex = '{0:X8}' -f $Result } catch { return $false }
  return ($hex -eq '80041320' -or $hex -eq '800710E0')
}

function Get-PewStartDecision {
  <#
    Pure verdict for Start-PewPtz, made only AFTER Start-ScheduledTask was
    called (never instead of calling it):
      'Started'  the task reached State Running.
      'Ran'      LastRunTime moved but the task is not Running any more (it
                 ran and exited; Wait-PewPtzHealthy reports why).
      'Soft'     the start did not take because the task user is not logged
                 on; the AtLogOn trigger starts it at their next logon.
      'Failed'   the start did not take for any other reason.
    -LastTaskResult / -StartErrorCode count as "not logged on" when they are
    0x80041320 or 0x800710E0. A result from this start (-Ran, or the error
    Start-ScheduledTask threw) is trusted as is. When the task did not run,
    LastTaskResult may be left over from an earlier run, so it only counts
    unless -Session (Test-PewUserSession: $true / $false / $null) shows the
    user does have a desktop session; -Session $false alone also means Soft.
  #>
  param(
    [bool]$Running,
    [bool]$Ran,
    $LastTaskResult,
    $Session,
    $StartErrorCode
  )
  if ($Running) { return 'Started' }
  if (Test-PewNotLoggedOnResult $StartErrorCode) { return 'Soft' }
  $notLoggedOn = Test-PewNotLoggedOnResult $LastTaskResult
  if ($Ran) {
    if ($notLoggedOn) { return 'Soft' }
    return 'Ran'
  }
  if ($Session -eq $false) { return 'Soft' }
  if ($notLoggedOn -and ($Session -ne $true)) { return 'Soft' }
  return 'Failed'
}

function Get-PewNotLoggedOnCode {
  # The not-logged-on code carried by an error from Start-ScheduledTask, or
  # $null. CimException keeps it in ErrorData.error_Code; the message may
  # also end in "(Exception from HRESULT: 0x800710E0)".
  param($ErrorRecord)
  $ex = $ErrorRecord.Exception
  $codes = @()
  if ($ex) { $codes += $ex.HResult }
  if ($ex -and $ex.InnerException) { $codes += $ex.InnerException.HResult }
  try { $codes += $ex.ErrorData.CimInstanceProperties['error_Code'].Value } catch { }
  if ("$($ex.Message)" -match '0x(80041320|800710E0)') { $codes += [Convert]::ToUInt32($Matches[1], 16) }
  foreach ($c in $codes) {
    if (Test-PewNotLoggedOnResult $c) { return $c }
  }
  return $null
}

function Start-PewPtz {
  <#
    Starts the scheduled task and returns { Started; Soft; Message }.

    Always calls Start-ScheduledTask first, polls ~$PollSec s for State
    Running (or a new LastRunTime), and retries once, because
    MultipleInstances IgnoreNew drops a start that lands while the previous
    instance still looks Running.

    Only a start that did not take is explained (Get-PewStartDecision):
    LastTaskResult 0x80041320 / 0x800710E0, or no desktop session for the
    task user (Test-PewUserSession, looked at again after 2 s in case
    explorer.exe was just restarting), gives Soft = $true: "not started now,
    and that is fine", the AtLogOn trigger starts it at the user's next
    logon. Callers treat Soft as success. Any other failure throws.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [string]$TaskUser,
    [int]$PollSec = 5
  )
  $ErrorActionPreference = 'Stop'
  $state = Get-PewTaskState -TaskName $TaskName
  if (-not $state) { throw "Scheduled task '$TaskName' is not registered. Run scripts\install.ps1." }
  if (-not $TaskUser) { $TaskUser = "$((Get-PewTask -TaskName $TaskName).Principal.UserId)" }
  $user = Get-PewBareUserName $TaskUser
  if ($state -eq 'Running') {
    if (@(Get-PewProcesses).Count -gt 0) {
      return (New-PewStartStatus -Started $true -Soft $false -Message "Task '$TaskName' is already running.")
    }
    # Stale "Running" with no pew-ptz process: end that instance first, or
    # IgnoreNew would silently drop the start below.
    try { Stop-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop | Out-Null } catch { }
    $until = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $until) {
      if ((Get-PewTaskState -TaskName $TaskName) -ne 'Running') { break }
      Start-Sleep -Milliseconds 250
    }
  }
  if ($state -eq 'Disabled') {
    throw "Scheduled task '$TaskName' is disabled. Enable it (Enable-ScheduledTask -TaskName '$TaskName') and try again."
  }
  $later = "It starts by itself at the next logon of $user (or a reboot with auto-logon)."

  $startError = $null
  $startErrorCode = $null
  for ($attempt = 1; $attempt -le 2; $attempt++) {
    $before = (Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\').LastRunTime
    try {
      Start-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop | Out-Null
    } catch {
      $startError = $_.Exception.Message
      $startErrorCode = Get-PewNotLoggedOnCode $_
      if ($null -ne $startErrorCode) { break }
      continue
    }
    $until = (Get-Date).AddSeconds($PollSec)
    do {
      Start-Sleep -Milliseconds 250
      $now = Get-PewTaskState -TaskName $TaskName
      if ($now -eq 'Running') {
        return (New-PewStartStatus -Started $true -Soft $false -Message "Task '$TaskName' started.")
      }
      $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\'
      if ($info.LastRunTime -ne $before) {
        $resultText = Get-PewTaskResultText $info.LastTaskResult
        if ((Get-PewStartDecision -Ran $true -LastTaskResult $info.LastTaskResult) -eq 'Soft') {
          return (New-PewStartStatus -Started $false -Soft $true -Message (
              "Task Scheduler did not start '$TaskName': $user is not logged on (LastTaskResult $resultText). $later"))
        }
        return (New-PewStartStatus -Started $true -Soft $false -Message (
            "Task '$TaskName' ran, but is now '$now' (LastTaskResult $resultText)."))
      }
    } while ((Get-Date) -lt $until)
  }

  # The start did not take. Only now ask why.
  $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\' -ErrorAction Ignore
  $lastResult = $null
  if ($info) { $lastResult = $info.LastTaskResult }
  $session = Test-PewUserSession -UserName $user
  $decision = Get-PewStartDecision -LastTaskResult $lastResult -Session $session -StartErrorCode $startErrorCode
  if ($decision -eq 'Soft' -and $session -eq $false -and $null -eq $startErrorCode) {
    # explorer.exe may just be restarting: look once more before calling it a logoff.
    Start-Sleep -Seconds 2
    $session = Test-PewUserSession -UserName $user
    $decision = Get-PewStartDecision -LastTaskResult $lastResult -Session $session -StartErrorCode $startErrorCode
  }
  $result = 'unknown'
  if ($info) { $result = Get-PewTaskResultText $lastResult }
  if ($decision -eq 'Soft') {
    if ($null -ne $startErrorCode) {
      $why = "Task Scheduler refused to start '$TaskName': $user is not logged on ($startError)."
    } elseif ($session -eq $false) {
      $why = "'$TaskName' did not start, and $user has no desktop session on this PC right now (not logged on)."
    } else {
      $why = "'$TaskName' did not start: $user is not logged on (LastTaskResult $result)."
    }
    if ($startError -and $null -eq $startErrorCode) { $why += " (Start-ScheduledTask said: $startError)" }
    return (New-PewStartStatus -Started $false -Soft $true -Message "$why $later")
  }
  $detail = "LastTaskResult $result"
  if ($startError) { $detail += "; Start-ScheduledTask: $startError" }
  throw "Scheduled task '$TaskName' did not start after 2 attempts ($detail). Check Task Scheduler -> '$TaskName' -> History."
}

function Test-PewHealth {
  <#
    Pure check of one /healthz probe. Returns $null when healthy, else the reason.
    Healthy = JSON has "build", restart_needed is false, started_at is not before
    this start (2 s slack), build equals $ExpectedBuild (when given), and exactly
    one listener on the port whose PID equals /healthz "pid".
  #>
  param(
    $Health,
    [string]$RequestError,
    [object[]]$Listeners = @(),
    [int]$Port,
    [long]$StartedAfter,
    [string]$ExpectedBuild
  )
  $Listeners = @($Listeners | Where-Object { $_ })
  $pids = (@($Listeners | ForEach-Object { $_.Pid }) -join ', ')
  if ($null -eq $Health) {
    if ($Listeners.Count -eq 0) {
      $why = ''
      if ($RequestError) { $why = " ($RequestError)" }
      return "nothing listening on 127.0.0.1:$Port$why"
    }
    return "port $Port has a listener (PID $pids) but /healthz did not answer: $RequestError"
  }
  if (-not $Health.PSObject.Properties['build']) {
    return "old server still answering: /healthz has no 'build' field, so the pre-update pew-ptz is still the one serving port $Port"
  }
  $startedAt = [long]0
  [void][long]::TryParse("$($Health.started_at)", [ref]$startedAt)
  if ($startedAt -lt ($StartedAfter - 2)) {
    $when = [DateTimeOffset]::FromUnixTimeSeconds([Math]::Max($startedAt, 0)).LocalDateTime
    return "old server still answering: the server on port $Port started at $when, before this restart (pid $($Health.pid))"
  }
  if (-not $Health.PSObject.Properties['restart_needed']) {
    return "/healthz has no 'restart_needed' field"
  }
  if ($Health.restart_needed -ne $false) {
    return "server reports restart_needed=true: the package files on disk changed after it loaded them"
  }
  if ($ExpectedBuild -and ("$($Health.build)" -ne $ExpectedBuild)) {
    return "wrong build: the server runs build $($Health.build), the files on disk are build $ExpectedBuild"
  }
  if ($Listeners.Count -gt 1) {
    return "two listeners on port ${Port}: PIDs $pids (expected only pid $($Health.pid))"
  }
  if ($Listeners.Count -eq 0) {
    return "/healthz answered, but netstat shows no listener on port $Port yet"
  }
  if ([int]$Listeners[0].Pid -ne [int]$Health.pid) {
    return "the listener on port $Port is PID $($Listeners[0].Pid), but /healthz reports pid $($Health.pid)"
  }
  return $null
}

function Get-PewLogTail {
  param([string]$LogDir, [int]$Lines = 20)
  foreach ($name in @('server.log', 'supervisor.log', 'server-stderr.log')) {
    $path = Join-Path $LogDir $name
    if (Test-Path -LiteralPath $path) {
      "--- last $Lines lines of $path ---"
      try {
        foreach ($l in @(Get-Content -LiteralPath $path -Tail $Lines -Encoding UTF8 -ErrorAction Stop)) { "$l" }
      } catch {
        "(could not read it: $($_.Exception.Message))"
      }
    } else {
      "--- $path (not found) ---"
    }
  }
}

function Wait-PewPtzHealthy {
  <#
    Polls http://127.0.0.1:<Port>/healthz (3 s per attempt, wall-clock deadline)
    until Test-PewHealth passes. Returns { Version; Build; Pid; StartedAt; Health }.
    On timeout throws the last reason ('old server still answering', 'wrong
    build', 'two listeners', 'nothing listening', ...) plus the last lines of
    server.log, supervisor.log and server-stderr.log. With -TaskName it fails
    early when the task has stopped (e.g. exit 78 config error / 98 port in use).
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][long]$StartedAfter,
    [string]$ExpectedBuild,
    [int]$TimeoutSec = 60,
    [Parameter(Mandatory = $true)][string]$LogDir,
    [string]$TaskName
  )
  $url = "http://127.0.0.1:$Port/healthz"
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $reason = 'no probe was made'
  $stoppedSince = $null
  $wrongBuildSince = $null
  while ($true) {
    $health = $null
    $err = $null
    try {
      $health = Invoke-RestMethod -Uri $url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    } catch {
      $err = $_.Exception.Message
    }
    if ($null -ne $health -and $health -is [string]) {
      $err = 'response was not JSON'
      $health = $null
    }
    $listeners = @(Get-PewListeners -Port $Port)
    $reason = Test-PewHealth -Health $health -RequestError $err -Listeners $listeners -Port $Port `
      -StartedAfter $StartedAfter -ExpectedBuild $ExpectedBuild
    if (-not $reason) {
      return [pscustomobject]@{
        Version   = "$($health.version)"
        Build     = "$($health.build)"
        Pid       = [int]$health.pid
        StartedAt = [long]$health.started_at
        Health    = $health
      }
    }
    # A fresh server with a different build will not change its mind.
    if ($reason.StartsWith('wrong build')) {
      if (-not $wrongBuildSince) { $wrongBuildSince = Get-Date }
      elseif (((Get-Date) - $wrongBuildSince).TotalSeconds -ge 5) { break }
    } else {
      $wrongBuildSince = $null
    }
    if ($TaskName) {
      $state = Get-PewTaskState -TaskName $TaskName
      if ($state -and $state -ne 'Running' -and @(Get-PewProcesses).Count -eq 0) {
        if (-not $stoppedSince) {
          $stoppedSince = Get-Date
        } elseif (((Get-Date) - $stoppedSince).TotalSeconds -ge 5) {
          $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\' -ErrorAction Ignore
          $result = 'unknown'
          if ($info) { $result = Get-PewTaskResultText $info.LastTaskResult }
          $reason = "task not running: '$TaskName' has stopped (LastTaskResult $result); last probe: $reason"
          break
        }
      } else {
        $stoppedSince = $null
      }
    }
    if ((Get-Date) -ge $deadline) { break }
    Start-Sleep -Milliseconds 500
  }
  $waited = [int]($TimeoutSec - ($deadline - (Get-Date)).TotalSeconds)
  $msg = @("pew-ptz is not healthy after $waited s: $reason", "Probe: $url") + @(Get-PewLogTail -LogDir $LogDir -Lines 20)
  throw ($msg -join "`n")
}

function Get-PewVenvPythonInfo {
  <#
    Runs <venv>\Scripts\python.exe -c "import sys; ..." and returns
    { Ok; Version; BasePrefix; Detail }. Ok = $true when it exits 0; Detail
    then says which Python the venv uses, else why it does not run (missing,
    or the base Python it was created from is gone). install.ps1 skips the
    Python 3.14 / winget step when Ok; update.ps1 refuses to stop anything
    without it.
  #>
  [CmdletBinding()]
  param([Parameter(Mandatory = $true)][string]$VenvPython)
  if (-not (Test-Path -LiteralPath $VenvPython)) {
    return [pscustomobject]@{ Ok = $false; Version = $null; BasePrefix = $null; Detail = "$VenvPython does not exist" }
  }
  $ErrorActionPreference = 'Continue'
  $PSNativeCommandUseErrorActionPreference = $false
  $out = $null
  $code = -1
  try {
    $out = & $VenvPython -c "import sys; print(sys.version.split()[0]); print(sys.base_prefix)" 2>&1
    $code = $LASTEXITCODE
  } catch {
    $out = @("$($_.Exception.Message)")
  }
  $lines = @(foreach ($o in @($out)) {
      if ($o -is [System.Management.Automation.ErrorRecord]) { "$($o.Exception.Message)".Trim() } else { "$o".Trim() }
    })
  $lines = @($lines | Where-Object { $_ })
  if ($code -ne 0 -or $lines.Count -lt 2) {
    return [pscustomobject]@{
      Ok = $false; Version = $null; BasePrefix = $null
      Detail = "$VenvPython does not run (exit $code): $(($lines | Select-Object -First 3) -join ' ')"
    }
  }
  $version = $lines[$lines.Count - 2]
  $base = $lines[$lines.Count - 1]
  [pscustomobject]@{ Ok = $true; Version = $version; BasePrefix = $base; Detail = "Python $version from $base" }
}

function Get-PewExpectedBuild {
  # Build fingerprint of the package files on disk, computed by the app's own
  # code (no git, no server import). $null when it cannot be computed.
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$VenvPython,
    [Parameter(Mandatory = $true)][string]$InstallDir
  )
  if (-not (Test-Path -LiteralPath $VenvPython)) { return $null }
  if (-not (Test-Path -LiteralPath $InstallDir)) { return $null }
  $ErrorActionPreference = 'Continue'
  $PSNativeCommandUseErrorActionPreference = $false
  $out = $null
  $code = -1
  Push-Location -LiteralPath $InstallDir
  try {
    $out = & $VenvPython -c "import pew_ptz.version as v; print(v.disk_build())" 2>&1
    $code = $LASTEXITCODE
  } catch {
    return $null
  } finally {
    Pop-Location
  }
  if ($code -ne 0) { return $null }
  $lines = @($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
      ForEach-Object { "$_".Trim() } | Where-Object { $_ })
  if ($lines.Count -eq 0) { return $null }
  $last = $lines[$lines.Count - 1]
  if ($last -cmatch '^[0-9a-f]{16}$') { return $last }
  return $null
}

function Invoke-Git {
  <#
    Runs: git -C <InstallDir> -c safe.directory=<InstallDir with forward slashes> <Arguments>
    Returns { Code; Out (string[]); Text }. Never throws for a git failure:
    callers decide from Code. Windows PowerShell 5.1 turns redirected native
    stderr into terminating errors under ErrorActionPreference=Stop, and git
    writes normal progress to stderr, so this runs with 'Continue' in its own
    scope and converts ErrorRecords to plain strings.
    The install dir is trusted for this one call only (never safe.directory=*).
    Quote '@{u}' in Arguments: unquoted it is a hashtable/scriptblock.
  #>
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string[]]$Arguments
  )
  $ErrorActionPreference = 'Continue'
  $PSNativeCommandUseErrorActionPreference = $false
  $safe = ($InstallDir -replace '\\', '/')
  $oldPrompt = $env:GIT_TERMINAL_PROMPT
  $env:GIT_TERMINAL_PROMPT = '0'
  try {
    $raw = & git -C $InstallDir -c "safe.directory=$safe" @Arguments 2>&1
    $code = $LASTEXITCODE
  } catch {
    $raw = @("$($_.Exception.Message)")
    $code = -1
  } finally {
    $env:GIT_TERMINAL_PROMPT = $oldPrompt
  }
  $lines = @(foreach ($r in @($raw)) {
      if ($r -is [System.Management.Automation.ErrorRecord]) { "$($r.Exception.Message)" } else { "$r" }
    })
  [pscustomobject]@{ Code = $code; Out = $lines; Text = ($lines -join "`n") }
}

function Get-PewGitOwnershipHint {
  # Message for git's "detected dubious ownership" refusal.
  param([Parameter(Mandatory = $true)][string]$InstallDir)
  $owner = '<unknown>'
  try { $owner = (Get-Acl -LiteralPath $InstallDir).Owner } catch { }
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $fwd = $InstallDir -replace '\\', '/'
  @(
    "git refused to work in $InstallDir ('detected dubious ownership').",
    "The folder is owned by $owner; this elevated shell runs as $me.",
    "These scripts already pass -c safe.directory=$fwd to every git call, so an old Git",
    "for Windows is the likely cause. Update Git (winget upgrade --id Git.Git -e), or",
    "trust this one folder for your admin account once:",
    "    git config --global --add safe.directory $fwd",
    "Never use safe.directory=*. Running update.ps1 / install.ps1 elevated trusts",
    "whatever the operator account can write inside $InstallDir."
  ) -join "`n"
}

function Get-PewTaskSettings {
  <#
    Reads the deployment settings back from the registered task, so restart.ps1
    and update.ps1 reuse what install.ps1 was given. Returns $null when the
    task is not registered. -Task lets tests pass a fake task object.
    IsLegacy = the old scripts\launch.cmd task (pre-supervisor install).
  #>
  [CmdletBinding()]
  param(
    [string]$TaskName = 'Pew PTZ Controller',
    $Task
  )
  if (-not $Task) {
    $Task = Get-PewTask -TaskName $TaskName
    if (-not $Task) { return $null }
  }
  $action = @($Task.Actions)[0]
  $execute = "$($action.Execute)".Trim().Trim('"')
  $arguments = "$($action.Arguments)"
  $installDir = "$($action.WorkingDirectory)".Trim().Trim([char[]]@('"')).TrimEnd([char[]]@('\'))
  $legacy = $execute -match '(?i)launch\.cmd$'
  if (-not $installDir -and $execute) {
    if ($legacy) {
      $installDir = Split-Path -Parent (Split-Path -Parent $execute)
    } elseif ($execute -match '(?i)\\\.venv\\Scripts\\pythonw?\.exe$') {
      $installDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $execute))
    }
  }
  $port = 8080
  if ($arguments -match '--port[\s=]+"?(\d+)') {
    $port = [int]$Matches[1]
  } elseif ($legacy -and (Test-Path -LiteralPath $execute)) {
    $m = Select-String -LiteralPath $execute -Pattern 'PEW_PTZ_SERVER_PORT=(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
  }
  $logDir = $null
  if ($arguments -match '--log-dir[\s=]+"([^"]+)"') {
    $logDir = $Matches[1]
  } elseif ($arguments -match '--log-dir[\s=]+(\S+)') {
    $logDir = $Matches[1]
  } elseif ($installDir) {
    $logDir = Join-Path $installDir 'logs'
  }
  $userId = "$($Task.Principal.UserId)"
  [pscustomobject]@{
    TaskName   = "$($Task.TaskName)"
    InstallDir = $installDir
    TaskUser   = (Get-PewBareUserName $userId)
    TaskUserId = $userId
    Port       = $port
    LogDir     = $logDir
    Execute    = $execute
    Arguments  = $arguments
    IsLegacy   = [bool]$legacy
    State      = "$($Task.State)"
  }
}

function Resolve-PewPath {
  # Absolute path relative to the PowerShell location (not the process cwd),
  # without a trailing backslash.
  param([Parameter(Mandatory = $true)][string]$Path)
  $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
  if ($full.Length -gt 3) { $full = $full.TrimEnd([char[]]@('\', '/')) }
  return $full
}
