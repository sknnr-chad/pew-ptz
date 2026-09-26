<#
.SYNOPSIS
  Probe a VISCA-over-IP PTZ camera for what pew-ptz (and per-ward presets) need.

.DESCRIPTION
  Read-only checks (always run):
    1. Ping
    2. HTTP snapshot at -SnapshotPath (what the phone preview uses)
    3. VISCA-over-IP on UDP -ViscaPort: sequence reset + version inquiry
    4. Pan/tilt and zoom position inquiries (needed to verify recalls)

  Preset test (skipped with -ReadOnly). WRITES the preset slots in -Slots and
  MOVES the camera:
    - For each slot: nudge the camera to a distinct position, read it back,
      save it to the slot (CAM_Memory Set).
    - Then recall every slot and check the camera returns to the position
      saved there. A slot the camera doesn't really have shows up as an error
      reply, or as a recall that lands somewhere else (e.g. a camera that
      wraps at 128 stores slot 192 on top of slot 64).
    - Returns the camera to where it started.
    - Saves the results to probe-presets.json so you can power-cycle the
      camera and re-run with -VerifyOnly to check presets survive.

  Pick slots nobody uses. The defaults stay above the 9 chapel presets.
  Runs on Windows PowerShell 5.1 and PowerShell 7+. Needs no admin rights.

.EXAMPLE
  .\probe-camera.ps1 -ReadOnly
  .\probe-camera.ps1 -Slots 100,127,128,254
  .\probe-camera.ps1 -VerifyOnly        # after power-cycling the camera
#>
[CmdletBinding()]
param(
  [string]$CameraIp     = "192.168.100.88",
  [int]   $ViscaPort    = 52381,
  [string]$SnapshotPath = "/snapshot.jpg",
  # Pairs 64/192 and 100/228 are 128 apart, so a camera that silently wraps
  # slot numbers at 128 overwrites one with the other and the recall check
  # catches it.
  [int[]] $Slots        = @(64, 100, 127, 128, 192, 228, 254),
  [switch]$ReadOnly,
  [switch]$VerifyOnly,
  [switch]$Force,
  [string]$ResultsFile  = (Join-Path $PWD "probe-presets.json"),
  [int]   $Tolerance    = 8   # position units a recall may miss by
)

$ErrorActionPreference = "Stop"

function Write-Head($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Write-Pass($m) { Write-Host "  PASS  $m" -ForegroundColor Green }
function Write-Fail($m) { Write-Host "  FAIL  $m" -ForegroundColor Red }
function Write-Info($m) { Write-Host "        $m" -ForegroundColor DarkGray }
function Hex([byte[]]$b) { ($b | ForEach-Object { $_.ToString("X2") }) -join " " }

$summary = [ordered]@{}

# ---- VISCA-over-IP transport ----------------------------------------------

$script:udp = New-Object System.Net.Sockets.UdpClient
$script:udp.Client.ReceiveTimeout = 1500
$script:seq = 0
$script:target = New-Object System.Net.IPEndPoint ([Net.IPAddress]::Parse($CameraIp)), $ViscaPort

function Receive-Packet {
  try {
    $ep = New-Object System.Net.IPEndPoint ([Net.IPAddress]::Any), 0
    return ,$script:udp.Receive([ref]$ep)
  } catch { return $null }
}

function Send-Raw([int]$ptype, [byte[]]$payload) {
  $script:seq = ($script:seq + 1) % 0x7FFFFFFF
  $s = $script:seq
  $hdr = [byte[]](
    (($ptype -shr 8) -band 0xFF), ($ptype -band 0xFF),
    (($payload.Length -shr 8) -band 0xFF), ($payload.Length -band 0xFF),
    (($s -shr 24) -band 0xFF), (($s -shr 16) -band 0xFF),
    (($s -shr 8) -band 0xFF), ($s -band 0xFF))
  $pkt = $hdr + $payload
  [void]$script:udp.Send($pkt, $pkt.Length, $script:target)
}

# Returns @{ ok; replies; error } — ok when a completion (9y 5z) arrives.
function Invoke-Visca([byte[]]$payload, [switch]$Inquiry) {
  $ptype = 0x0100
  if ($Inquiry) { $ptype = 0x0110 }
  Send-Raw $ptype $payload
  $replies = @()
  for ($i = 0; $i -lt 4; $i++) {
    $pkt = Receive-Packet
    if ($null -eq $pkt) { break }
    if ($pkt.Length -lt 11) { continue }
    $rseq = ([int]$pkt[4] -shl 24) -bor ([int]$pkt[5] -shl 16) -bor ([int]$pkt[6] -shl 8) -bor $pkt[7]
    if ($rseq -ne $script:seq) { $i--; continue }  # stale reply to an earlier command
    $body = [byte[]]$pkt[8..($pkt.Length - 1)]
    $replies += ,$body
    $kind = $body[1] -band 0xF0
    if ($kind -eq 0x50) { return @{ ok = $true; replies = $replies; body = $body } }
    if ($kind -eq 0x60) {
      $why = switch ($body[2]) {
        0x02 { "syntax error" } 0x03 { "command buffer full" } 0x04 { "canceled" }
        0x05 { "no socket" } 0x41 { "not executable" } default { "error 0x{0:X2}" -f $body[2] }
      }
      return @{ ok = $false; replies = $replies; error = $why }
    }
  }
  if ($replies.Count -gt 0) { return @{ ok = $false; replies = $replies; error = "ACK but no completion" } }
  return @{ ok = $false; replies = $replies; error = "no reply" }
}

function Get-Nibbles([byte[]]$b, [int]$start) {
  $v = 0
  for ($i = 0; $i -lt 4; $i++) { $v = ($v -shl 4) -bor ($b[$start + $i] -band 0x0F) }
  if ($v -ge 0x8000) { $v -= 0x10000 }  # pan/tilt are signed
  return $v
}

function Split-Nibbles([int]$v) {
  if ($v -lt 0) { $v += 0x10000 }
  return [byte[]]((($v -shr 12) -band 0xF), (($v -shr 8) -band 0xF),
                  (($v -shr 4) -band 0xF), ($v -band 0xF))
}

function Get-Position {
  $pt = Invoke-Visca ([byte[]](0x81, 0x09, 0x06, 0x12, 0xFF)) -Inquiry
  if (-not $pt.ok -or $pt.body.Length -lt 11) { return $null }
  $pos = @{ pan = (Get-Nibbles $pt.body 2); tilt = (Get-Nibbles $pt.body 6); zoom = $null }
  $z = Invoke-Visca ([byte[]](0x81, 0x09, 0x04, 0x47, 0xFF)) -Inquiry
  if ($z.ok -and $z.body.Length -ge 7) {
    $zv = Get-Nibbles $z.body 2
    if ($zv -lt 0) { $zv += 0x10000 }
    $pos.zoom = $zv
  }
  return $pos
}

function Format-Pos($p) {
  if ($null -eq $p) { return "(unknown)" }
  return "pan={0} tilt={1} zoom={2}" -f $p.pan, $p.tilt, $p.zoom
}

# Poll until the camera stops moving (3 identical reads) or timeout.
function Wait-Settled([int]$timeoutMs = 15000) {
  $last = $null; $same = 0
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.ElapsedMilliseconds -lt $timeoutMs) {
    Start-Sleep -Milliseconds 250
    $p = Get-Position
    if ($null -ne $p -and $null -ne $last -and
        $p.pan -eq $last.pan -and $p.tilt -eq $last.tilt -and $p.zoom -eq $last.zoom) {
      $same++
      if ($same -ge 2) { return $p }
    } else { $same = 0 }
    $last = $p
  }
  return $last
}

function Test-Near($a, $b) {
  if ($null -eq $a -or $null -eq $b) { return $false }
  return ([math]::Abs($a.pan - $b.pan) -le $Tolerance) -and
         ([math]::Abs($a.tilt - $b.tilt) -le $Tolerance)
}

function Invoke-Recall([int]$slot) {
  $r = Invoke-Visca ([byte[]](0x81, 0x01, 0x04, 0x3F, 0x02, $slot, 0xFF))
  $p = Wait-Settled
  return @{ reply = $r; pos = $p }
}

function Set-Absolute($pos) {
  $cmd = [byte[]](0x81, 0x01, 0x06, 0x02, 0x10, 0x10) + (Split-Nibbles $pos.pan) +
         (Split-Nibbles $pos.tilt) + [byte[]](0xFF)
  $r = Invoke-Visca $cmd
  if ($r.ok -and $null -ne $pos.zoom) {
    [void](Invoke-Visca ([byte[]](0x81, 0x01, 0x04, 0x47) + (Split-Nibbles $pos.zoom) + [byte[]](0xFF)))
  }
  [void](Wait-Settled)
  return $r
}

# ---- 1. Ping --------------------------------------------------------------

Write-Head "Network: $CameraIp"
$ping = New-Object System.Net.NetworkInformation.Ping
try { $pr = $ping.Send($CameraIp, 1500) } catch { $pr = $null }
if ($pr -and $pr.Status -eq "Success") {
  Write-Pass "ping ($($pr.RoundtripTime) ms)"; $summary.ping = "ok"
} else {
  Write-Fail "no ping reply (may just be ICMP blocked; continuing)"; $summary.ping = "no reply"
}

# ---- 2. Snapshot ----------------------------------------------------------

Write-Head "Snapshot: http://$CameraIp$SnapshotPath"
try {
  $r = Invoke-WebRequest "http://$CameraIp$SnapshotPath" -UseBasicParsing -TimeoutSec 5
  $ct = "$($r.Headers['Content-Type'])"
  if ($ct -like "image/*") {
    Write-Pass "HTTP $($r.StatusCode), $ct, $($r.RawContentLength) bytes"; $summary.snapshot = "ok"
  } else {
    Write-Fail "HTTP $($r.StatusCode) but Content-Type is '$ct', not an image"
    $summary.snapshot = "not an image"
  }
} catch {
  Write-Fail $_.Exception.Message
  Write-Info "See docs/ptz-cameras.md for other vendors' snapshot paths."
  $summary.snapshot = "failed"
}

# ---- 3. VISCA reachability ------------------------------------------------

Write-Head "VISCA-over-IP: UDP $ViscaPort"
Send-Raw 0x0200 ([byte[]](0x01))     # control command: reset sequence number
$reset = Receive-Packet
$script:seq = 0
if ($null -ne $reset) { Write-Info "sequence reset acknowledged" }

$ver = Invoke-Visca ([byte[]](0x81, 0x09, 0x00, 0x02, 0xFF)) -Inquiry
if ($ver.ok -and $ver.body.Length -ge 10) {
  $b = $ver.body
  $vendor = ($b[2] -shl 8) -bor $b[3]; $model = ($b[4] -shl 8) -bor $b[5]
  $rom = ($b[6] -shl 8) -bor $b[7]
  Write-Pass ("version: vendor=0x{0:X4} model=0x{1:X4} rom=0x{2:X4} sockets={3}" -f $vendor, $model, $rom, $b[8])
  $summary.visca = "ok"
} elseif ($ver.replies.Count -gt 0) {
  Write-Fail "version inquiry: $($ver.error) (raw: $(Hex $ver.replies[-1]))"
  $summary.visca = "replies, but $($ver.error)"
} else {
  Write-Fail "no VISCA reply. Check VISCA-over-IP is enabled and the port is $ViscaPort."
  $summary.visca = "no reply"
}

# ---- 4. Position inquiry --------------------------------------------------

Write-Head "Position inquiry"
$start = Get-Position
if ($null -ne $start) {
  Write-Pass "current position: $(Format-Pos $start)"; $summary.position_inquiry = "ok"
} else {
  Write-Fail "camera did not answer the pan/tilt position inquiry"
  Write-Info "Preset recalls can't be verified without it; the preset test will only check replies."
  $summary.position_inquiry = "unsupported"
}

# ---- 5. Presets -----------------------------------------------------------

function Write-Summary {
  Write-Head "Summary"
  foreach ($k in $summary.Keys) { Write-Host ("  {0,-18} {1}" -f $k, $summary[$k]) }
  $script:udp.Close()
}

if ($VerifyOnly) {
  Write-Head "Verify saved presets (from $ResultsFile)"
  if (-not (Test-Path $ResultsFile)) { Write-Fail "no results file; run the full probe first"; Write-Summary; exit 1 }
  $saved = Get-Content $ResultsFile -Raw | ConvertFrom-Json
  $kept = 0; $total = 0
  foreach ($s in $saved.slots) {
    if (-not $s.stored) { continue }
    $total++
    $rc = Invoke-Recall ([int]$s.slot)
    $want = @{ pan = $s.pan; tilt = $s.tilt }
    if (Test-Near $rc.pos $want) { $kept++; Write-Pass "slot $($s.slot) still at $(Format-Pos $rc.pos)" }
    else { Write-Fail "slot $($s.slot): expected pan=$($s.pan) tilt=$($s.tilt), got $(Format-Pos $rc.pos)" }
  }
  $summary.presets_survived = "$kept of $total"
  if ($null -ne $start) { [void](Set-Absolute $start) }
  Write-Summary
  exit 0
}

if ($ReadOnly) {
  $summary.preset_test = "skipped (-ReadOnly)"
  Write-Summary
  exit 0
}

if ($summary.visca -ne "ok") {
  $summary.preset_test = "skipped (VISCA not working)"
  Write-Summary
  exit 1
}

$bad = $Slots | Where-Object { $_ -lt 0 -or $_ -gt 254 }
if ($bad) { throw "Slots must be 0-254; got $($bad -join ', ')" }

Write-Head "Preset test"
Write-Host "  This OVERWRITES preset slots $($Slots -join ', ') and moves the camera." -ForegroundColor Yellow
Write-Host "  It returns the camera to its current position at the end." -ForegroundColor Yellow
if (-not $Force) {
  $ans = Read-Host "  Type YES to continue"
  if ($ans -ne "YES") { $summary.preset_test = "cancelled"; Write-Summary; exit 0 }
}

$results = @()
$script:wrapped = $false
$dir = 0
foreach ($slot in $Slots) {
  # Nudge the camera so each slot holds a distinct position: short pan
  # bursts, alternating tilt, at moderate speed.
  $tilt = 0x03
  if ($dir % 2 -eq 0) { $tilt = 0x01 } else { $tilt = 0x02 }
  [void](Invoke-Visca ([byte[]](0x81, 0x01, 0x06, 0x01, 0x08, 0x06, 0x02, $tilt, 0xFF)))
  Start-Sleep -Milliseconds 400
  [void](Invoke-Visca ([byte[]](0x81, 0x01, 0x06, 0x01, 0x01, 0x01, 0x03, 0x03, 0xFF)))
  $dir++
  $pos = Wait-Settled

  $set = Invoke-Visca ([byte[]](0x81, 0x01, 0x04, 0x3F, 0x01, $slot, 0xFF))
  $entry = [ordered]@{ slot = $slot; stored = $set.ok; set_error = $set.error;
                       pan = $null; tilt = $null; zoom = $null; recalled = $null }
  if ($null -ne $pos) { $entry.pan = $pos.pan; $entry.tilt = $pos.tilt; $entry.zoom = $pos.zoom }
  if ($set.ok) { Write-Pass "slot $slot saved at $(Format-Pos $pos)" }
  else { Write-Fail "slot ${slot}: save rejected ($($set.error))" }
  $results += ,$entry
}

# Recall newest-first. Each recall starts from a parked position so a recall
# that does nothing can't pass by accident.
$ok = 0; $stored = @($results | Where-Object { $_.stored })
for ($i = $results.Count - 1; $i -ge 0; $i--) {
  $e = $results[$i]
  if (-not $e.stored) { continue }
  [void](Invoke-Visca ([byte[]](0x81, 0x01, 0x06, 0x01, 0x08, 0x06, 0x01, 0x03, 0xFF)))
  Start-Sleep -Milliseconds 600
  [void](Invoke-Visca ([byte[]](0x81, 0x01, 0x06, 0x01, 0x01, 0x01, 0x03, 0x03, 0xFF)))
  [void](Wait-Settled)

  $rc = Invoke-Recall $e.slot
  if ($null -eq $e.pan) {
    $e.recalled = "reply only: $($rc.reply.ok)"
    Write-Info "slot $($e.slot): recall reply ok=$($rc.reply.ok) (position not verifiable)"
    if ($rc.reply.ok) { $ok++ }
  } elseif (Test-Near $rc.pos $e) {
    $e.recalled = "ok"; $ok++
    Write-Pass "slot $($e.slot) recalled to $(Format-Pos $rc.pos)"
  } else {
    $e.recalled = "wrong position"
    $other = $stored | Where-Object { $_.slot -ne $e.slot -and (Test-Near $rc.pos $_) } |
             Select-Object -First 1
    if ($null -ne $other) {
      $e.recalled = "overwritten by slot $($other.slot)"
      $script:wrapped = $true
      Write-Fail ("slot {0} holds slot {1}'s position: the camera maps both numbers to one slot" -f
                  $e.slot, $other.slot)
    } else {
      Write-Fail ("slot {0}: expected pan={1} tilt={2}, got {3}" -f
                  $e.slot, $e.pan, $e.tilt, (Format-Pos $rc.pos))
    }
  }
}

# Check distinct slots didn't collapse onto the same stored position.
$dupes = $stored | Group-Object { "$($_.pan),$($_.tilt)" } | Where-Object { $_.Count -gt 1 }
if ($dupes) { Write-Info "note: some slots were saved at identical positions; wrap detection is weaker for those." }

$summary.preset_test = "$ok of $($Slots.Count) slots recalled to their saved position"
# Slots that overwrote another slot are aliases of it, not real storage.
$aliases = @($results | ForEach-Object {
  if ("$($_.recalled)" -like "overwritten by slot *") { [int]("$($_.recalled)" -replace '\D', '') }
})
$good = @($results | Where-Object { $_.recalled -eq "ok" -and $aliases -notcontains $_.slot } |
          ForEach-Object { [int]$_.slot })
if ($aliases.Count -gt 0) {
  # Slots at or above the lowest one that overwrote another are aliases.
  $limit = ($aliases | Measure-Object -Minimum).Minimum
  $good = @($good | Where-Object { $_ -lt $limit })
}
if ($good.Count -gt 0) { $summary.highest_good_slot = ($good | Measure-Object -Maximum).Maximum }
if ($script:wrapped) {
  $summary.slot_wrapping = "yes: some slot numbers share storage (see FAIL lines)"
}

if ($null -ne $start) {
  $r = Set-Absolute $start
  if ($r.ok) { Write-Info "camera returned to $(Format-Pos $start)"; $summary.absolute_move = "ok" }
  else { Write-Info "absolute move not supported ($($r.error)); camera left where the test ended"; $summary.absolute_move = $r.error }
}

@{ camera = $CameraIp; tested_at = (Get-Date).ToString("s"); slots = $results } |
  ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $ResultsFile
Write-Info "Results saved to $ResultsFile"
Write-Info "Power-cycle the camera, then run: .\probe-camera.ps1 -VerifyOnly"

Write-Summary
