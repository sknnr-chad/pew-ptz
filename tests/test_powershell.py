"""Checks for the PowerShell scripts in scripts/ (Windows only).

- every scripts/*.ps1 is pure ASCII: Windows PowerShell 5.1 reads a BOM-less
  file as Windows-1252, where an em dash's 0x94 byte becomes a closing quote
- every script parses with zero errors under powershell.exe (5.1) and pwsh (7)
- Get-Help finds the comment-based help of each admin script
- pewctl.ps1 helpers: the default pew-ptz command-line patterns, netstat
  listener parsing, the /healthz verdict, the Start-PewPtz decision (and its
  flow with the task cmdlets mocked), reading settings back from a task,
  the venv python probe, Invoke-Git under ErrorActionPreference Stop,
  Wait-PewPtzHealthy against a fake /healthz, and Stop-PewPtz against dummy
  processes that carry a unique marker (never a real pew_ptz: the match
  pattern is overridden)

Nothing here registers, starts or stops scheduled tasks or changes system
settings; Stop-PewPtz is given a task name that does not exist, and the
Start-PewPtz flow test replaces every task cmdlet with a function (and uses
a task name that does not exist, in case one were not replaced). Sockets are
loopback-only, so no firewall prompt appears.
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="PowerShell scripts are Windows-only")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PEWCTL = SCRIPTS / "pewctl.ps1"
PS_FILES = sorted(SCRIPTS.glob("*.ps1"))

SHELLS = [pytest.param("powershell.exe", id="ps51"), pytest.param("pwsh.exe", id="pwsh7")]

PRELUDE = (
    "$ErrorActionPreference = 'Stop'\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
    ". $env:PEWCTL_PS1\n"
)

# Kills leftover dummy processes whose command line carries the test marker.
SWEEP_SCRIPT = r"""
$filter = "Name='python.exe' OR Name='pythonw.exe'"
Get-CimInstance -ClassName Win32_Process -Filter $filter |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:PEW_MARKER) } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"""


def _shell_exe(shell: str) -> str:
    exe = shutil.which(shell)
    if not exe:
        pytest.skip(f"{shell} is not on PATH")
    return exe


def run_ps(shell: str, script: str, *, env: dict[str, str] | None = None,
           prelude: bool = True, timeout: float = 60) -> list[str]:
    """Run a script in a fresh shell; return its stdout lines, fail on a non-zero exit."""
    exe = _shell_exe(shell)
    text = (PRELUDE if prelude else "") + script
    encoded = base64.b64encode(text.encode("utf-16-le")).decode("ascii")
    full_env = dict(os.environ)
    # A PSModulePath inherited from pwsh 7 makes powershell.exe 5.1 load the
    # wrong module versions; let each shell compute its own.
    full_env.pop("PSModulePath", None)
    full_env["PEWCTL_PS1"] = str(PEWCTL)
    full_env.update(env or {})
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=full_env, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (
        f"{shell} exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]


def _tagged(lines: list[str], tag: str) -> dict[str, str]:
    """Lines like 'TAG key value...' -> {key: value}."""
    out = {}
    for line in lines:
        parts = line.split(" ", 2)
        if parts[0] == tag and len(parts) >= 2:
            out[parts[1]] = parts[2] if len(parts) == 3 else ""
    return out


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sweep(shell: str, marker: str) -> None:
    try:
        run_ps(shell, SWEEP_SCRIPT, env={"PEW_MARKER": marker}, prelude=False, timeout=30)
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


# --------------------------------------------------------------------------
# (a) ASCII and (b) parsing
# --------------------------------------------------------------------------

def test_expected_scripts_exist():
    names = {p.name for p in PS_FILES}
    assert {"pewctl.ps1", "install.ps1", "restart.ps1", "update.ps1", "uninstall.ps1"} <= names


@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_script_is_ascii(path: Path):
    bad = []
    for lineno, line in enumerate(path.read_bytes().splitlines(), start=1):
        if any(b > 0x7F for b in line):
            bad.append(f"{path.name}:{lineno}: {line.decode('utf-8', 'replace')!r}")
    assert not bad, ("non-ASCII characters (Windows PowerShell 5.1 misreads them):\n"
                     + "\n".join(bad))


PARSE_SCRIPT = r"""
$parser = [System.Management.Automation.Language.Parser]
foreach ($f in Get-ChildItem -LiteralPath $env:PEW_SCRIPTS_DIR -Filter '*.ps1') {
  # Default encoding on purpose: that is how each shell reads a script it runs.
  $text = Get-Content -LiteralPath $f.FullName -Raw
  $tokens = $null
  $errors = $null
  [void]$parser::ParseInput($text, [ref]$tokens, [ref]$errors)
  foreach ($e in @($errors)) {
    'ERR {0}:{1}: {2}' -f $f.Name, $e.Extent.StartLineNumber, $e.Message
  }
  'PARSED {0}' -f $f.Name
}
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_scripts_parse(shell: str):
    lines = run_ps(shell, PARSE_SCRIPT, env={"PEW_SCRIPTS_DIR": str(SCRIPTS)}, prelude=False)
    errors = [line for line in lines if line.startswith("ERR ")]
    assert not errors, "\n".join(errors)
    parsed = {line.split(" ", 1)[1] for line in lines if line.startswith("PARSED ")}
    assert parsed == {p.name for p in PS_FILES}


# Get-Help only reads the file; nothing is run. Without a blank line between
# '#requires -RunAsAdministrator' and '<#', Get-Help ignores the help block
# and .Synopsis is just the generated syntax line.
HELP_SCRIPT = r"""
foreach ($name in 'install.ps1', 'restart.ps1', 'update.ps1', 'uninstall.ps1') {
  $h = Get-Help (Join-Path $env:PEW_SCRIPTS_DIR $name) -Full
  'SYN {0} {1}' -f $name, ((("$($h.Synopsis)" -split '\r?\n') | ForEach-Object { $_.Trim() }) -join ' ')
  'DESC {0} {1}' -f $name, (((@($h.description) | ForEach-Object { $_.Text }) -join ' ') -replace '\s+', ' ')
}
$p = Get-Help (Join-Path $env:PEW_SCRIPTS_DIR 'update.ps1') -Parameter CheckOnly
'PARAM CheckOnly {0}' -f (((@($p.description) | ForEach-Object { $_.Text }) -join ' ') -replace '\s+', ' ')
"""

ADMIN_SYNOPSES = {
    "install.ps1": "One-shot installer for pew-ptz",
    "restart.ps1": "Restarts pew-ptz and waits",
    "update.ps1": "Updates pew-ptz to the latest commit",
    "uninstall.ps1": "Removes the pew-ptz scheduled task",
}


@pytest.mark.parametrize("shell", SHELLS)
def test_admin_scripts_expose_help(shell: str):
    lines = run_ps(shell, HELP_SCRIPT, env={"PEW_SCRIPTS_DIR": str(SCRIPTS)}, prelude=False)
    syn = _tagged(lines, "SYN")
    desc = _tagged(lines, "DESC")
    for name, start in ADMIN_SYNOPSES.items():
        assert syn[name].startswith(start), f"{name}: Get-Help did not find .SYNOPSIS: {syn[name]!r}"
        assert desc.get(name), f"{name}: Get-Help found no .DESCRIPTION"
    assert "-CheckOnly" in desc["update.ps1"]
    assert "restart.ps1 is enough" in desc["update.ps1"]
    assert "restart.ps1 is enough" in desc["restart.ps1"]
    assert "preflight" in _tagged(lines, "PARAM")["CheckOnly"]


# --------------------------------------------------------------------------
# Default command-line patterns (pure: no process is listed)
# --------------------------------------------------------------------------

# (command line, is pew-ptz, is supervisor)
CMDLINE_CASES = [
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptz.supervise --port 8080 --log-dir "C:\x\logs"',
     True, True),
    (r'C:\Python314\pythonw.exe -m pew_ptz.supervise --port 8080 --log-dir "C:\x\logs"', True, True),
    (r'"C:\x\.venv\Scripts\pythonw.exe"  -m pew_ptz --port 8080', True, False),
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptz --port 8080 --log-dir "C:\x\logs"', True, False),
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptz', True, False),
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptz.supervise', True, True),
    (r'"C:\tools\pew-ptz\.venv\Scripts\pythonw.exe" -m pew_ptz', True, False),
    ('"C:\\x\\.venv\\Scripts\\pythonw.exe" -m\tpew_ptz', True, False),  # tab
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptzx', False, False),
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptz.supervisex', False, False),
    (r'"C:\x\.venv\Scripts\pythonw.exe" -m pew_ptz.version', False, False),
    (r'python -m pytest', False, False),
    (r'python -m pytest tests/test_supervise.py', False, False),
    (r'python -c "import pew_ptz"', False, False),
    (r'"C:\tools\pew-ptz\.venv\Scripts\python.exe" -c "import pew_ptz.version as v; print(v.disk_build())"',
     False, False),
]

PATTERN_SCRIPT = r"""
$cases = @(Get-Content -LiteralPath $env:PEW_CMDLINES -Encoding UTF8)
for ($i = 0; $i -lt $cases.Count; $i++) {
  'CL {0} {1}|{2}' -f $i, (Test-PewCommandLine -CommandLine $cases[$i]), (Test-PewCommandLine -CommandLine $cases[$i] -Supervisor)
}
'CL empty {0}|{1}' -f (Test-PewCommandLine -CommandLine ''), (Test-PewCommandLine -CommandLine $null -Supervisor)
'PAT all {0}' -f (Get-PewProcessPattern)
'PAT sup {0}' -f (Get-PewProcessPattern -Supervisor)
# The real callers' defaults must come from the same helper.
foreach ($name in 'Get-PewProcesses', 'Stop-PewPtz') {
  foreach ($p in @((Get-Command $name).ScriptBlock.Ast.Body.ParamBlock.Parameters)) {
    if ($p.DefaultValue -and $p.Name.VariablePath.UserPath -like '*Pattern') {
      'DEF {0}.{1} {2}' -f $name, $p.Name.VariablePath.UserPath, $p.DefaultValue.Extent.Text
    }
  }
}
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_default_process_patterns(shell: str, tmp_path: Path):
    sample = tmp_path / "cmdlines.txt"
    sample.write_text("\n".join(c for c, _, _ in CMDLINE_CASES) + "\n", encoding="utf-8")
    lines = run_ps(shell, PATTERN_SCRIPT, env={"PEW_CMDLINES": str(sample)})
    got = _tagged(lines, "CL")
    assert len(got) == len(CMDLINE_CASES) + 1, lines
    for i, (cmd, is_pew, is_sup) in enumerate(CMDLINE_CASES):
        assert got[str(i)] == f"{is_pew}|{is_sup}", f"{cmd!r}: got {got[str(i)]}"
    assert got["empty"] == "False|False"
    pat = _tagged(lines, "PAT")
    assert pat["all"] == r'-m\s+pew_ptz(\.supervise)?(\s|"|$)'
    assert pat["sup"] == r"-m\s+pew_ptz\.supervise"
    assert _tagged(lines, "DEF") == {
        "Get-PewProcesses.Pattern": "(Get-PewProcessPattern)",
        "Stop-PewPtz.Pattern": "(Get-PewProcessPattern)",
        "Stop-PewPtz.SupervisorPattern": "(Get-PewProcessPattern -Supervisor)",
    }


# --------------------------------------------------------------------------
# Start-PewPtz: try first, explain a failed start afterwards
# --------------------------------------------------------------------------

DECISION_SCRIPT = r"""
function Show-Decision($name, [bool]$running, [bool]$ran, $result, $session, $errCode) {
  $d = Get-PewStartDecision -Running $running -Ran $ran -LastTaskResult $result -Session $session -StartErrorCode $errCode
  'SD {0} {1}' -f $name, $d
}
$nlo = [uint32]2147750688    # 0x80041320 as LastTaskResult (UInt32)
$refused = 0x800710E0        # Int32 literal, like an HResult
$ok = [uint32]0
Show-Decision 'running_nosession' $true $false $ok $false $null
Show-Decision 'running_stale_nlo' $true $false $nlo $false $null
Show-Decision 'ran_ok_nosession' $false $true $ok $false $null
Show-Decision 'ran_config' $false $true ([uint32]78) $null $null
Show-Decision 'ran_nlo' $false $true $nlo $true $null
Show-Decision 'ran_refused' $false $true $refused $true $null
Show-Decision 'failed_nosession' $false $false $ok $false $null
Show-Decision 'failed_session' $false $false $ok $true $null
Show-Decision 'failed_unknown' $false $false $ok $null $null
Show-Decision 'failed_stale_nlo_session' $false $false $nlo $true $null
Show-Decision 'failed_stale_nlo_unknown' $false $false $nlo $null $null
Show-Decision 'failed_noresult_session' $false $false $null $true $null
Show-Decision 'threw_refused' $false $false $ok $true $refused
Show-Decision 'threw_other' $false $false $ok $true ([int]-2147024891)
'NLO {0}|{1}|{2}|{3}' -f (Test-PewNotLoggedOnResult $nlo), (Test-PewNotLoggedOnResult $refused), (Test-PewNotLoggedOnResult 0x41301), (Test-PewNotLoggedOnResult $null)
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_start_decision_is_soft_only_after_a_failed_start(shell: str):
    lines = run_ps(shell, DECISION_SCRIPT)
    d = _tagged(lines, "SD")
    # A start that took wins over any session heuristic or stale result.
    assert d["running_nosession"] == "Started"
    assert d["running_stale_nlo"] == "Started"
    assert d["ran_ok_nosession"] == "Ran"
    assert d["ran_config"] == "Ran"
    # Fresh not-logged-on results from this start are trusted.
    assert d["ran_nlo"] == "Soft"
    assert d["ran_refused"] == "Soft"
    assert d["threw_refused"] == "Soft"
    # A start that did not take: Soft only with evidence of no logon.
    assert d["failed_nosession"] == "Soft"
    assert d["failed_session"] == "Failed"
    assert d["failed_unknown"] == "Failed"
    assert d["failed_stale_nlo_session"] == "Failed"
    assert d["failed_stale_nlo_unknown"] == "Soft"
    assert d["failed_noresult_session"] == "Failed"
    assert d["threw_other"] == "Failed"
    assert "NLO True|True|False|False" in lines


# Every task cmdlet Start-PewPtz uses is replaced by a function (functions
# win over cmdlets), and the task name does not exist, so even a cmdlet that
# slipped through could not start anything.
START_FLOW_SCRIPT = r"""
$global:M = $null
function Reset-Mock([string]$after, $result, [bool]$bump, $session, [string]$throwText) {
  $global:M = @{
    State = 'Ready'; After = $after; Result = $result; Bump = $bump
    Session = $session; Throw = $throwText
    LastRunTime = [datetime]'2026-01-01'; LastTaskResult = [uint32]0
    Starts = 0; SessionChecks = 0; Order = @()
  }
}
function Get-PewTaskState { param($TaskName) $global:M.State }
function Get-PewProcesses { @() }
function Get-PewTask { throw 'mock: Get-PewTask is not expected' }
function Test-PewUserSession {
  param($UserName)
  $global:M.SessionChecks++
  $global:M.Order += 'session'
  $global:M.Session
}
function Get-ScheduledTaskInfo {
  [CmdletBinding()] param($TaskName, $TaskPath)
  [pscustomobject]@{ LastRunTime = $global:M.LastRunTime; LastTaskResult = $global:M.LastTaskResult }
}
function Stop-ScheduledTask { [CmdletBinding()] param($TaskName, $TaskPath) throw 'mock: Stop-ScheduledTask is not expected' }
function Start-ScheduledTask {
  [CmdletBinding()] param($TaskName, $TaskPath)
  $global:M.Starts++
  $global:M.Order += 'start'
  if ($global:M.Throw) { throw $global:M.Throw }
  if ($global:M.After) { $global:M.State = $global:M.After }
  if ($global:M.Bump) { $global:M.LastRunTime = Get-Date }
  if ($null -ne $global:M.Result) { $global:M.LastTaskResult = [uint32]$global:M.Result }
}
foreach ($c in 'Start-ScheduledTask', 'Get-ScheduledTaskInfo', 'Stop-ScheduledTask', 'Get-PewTaskState') {
  $cmd = Get-Command $c
  # The real task commands are CDXML functions of the ScheduledTasks module.
  $kind = 'real'
  if ($cmd.CommandType -eq 'Function' -and -not $cmd.ModuleName -and
      $cmd.ScriptBlock.ToString() -match 'mock|global:M') { $kind = 'mock' }
  'KIND {0} {1}' -f $c, $kind
}
$refused = [Convert]::ToUInt32('800710E0', 16)
$task = 'pewctl-test-no-such-task-' + [guid]::NewGuid().ToString('N')
function Show-Start($name) {
  try {
    $s = Start-PewPtz -TaskName $task -TaskUser 'HOSTPC\Chapel-AV' -PollSec 1
    $out = '{0}|{1}|{2}' -f $s.Started, $s.Soft, $s.Message
  } catch {
    $out = 'THROW|' + $_.Exception.Message
  }
  'SP {0} {1}|{2}|{3}|{4}' -f $name, $global:M.Starts, $global:M.SessionChecks, ($global:M.Order -join ','), $out
}
Reset-Mock 'Running' $null $false $false ''
Show-Start 'starts_without_explorer'
Reset-Mock '' $refused $true $true ''
Show-Start 'refused_fresh'
Reset-Mock '' ([uint32]78) $true $false ''
Show-Start 'ran_and_exited'
Reset-Mock '' $null $false $false ''
Show-Start 'never_takes_logged_off'
Reset-Mock '' $null $false $true ''
Show-Start 'never_takes_logged_on'
Reset-Mock '' $null $false $true 'The operator or administrator has refused the request. (Exception from HRESULT: 0x800710E0)'
Show-Start 'throws_refused'
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_start_pewptz_tries_before_explaining(shell: str):
    lines = run_ps(shell, START_FLOW_SCRIPT, timeout=90)
    kinds = _tagged(lines, "KIND")
    assert kinds == {"Start-ScheduledTask": "mock", "Get-ScheduledTaskInfo": "mock",
                     "Stop-ScheduledTask": "mock", "Get-PewTaskState": "mock"}, kinds
    sp = {k: v.split("|", 4) for k, v in _tagged(lines, "SP").items()}

    # Logged on but explorer.exe missing: the start is still attempted and taken.
    starts, checks, order, started, rest = sp["starts_without_explorer"]
    assert (starts, checks, order, started) == ("1", "0", "start", "True"), sp
    assert rest.startswith("False|")

    # Task Scheduler refused this start (not logged on): Soft, no retry needed.
    starts, checks, order, started, rest = sp["refused_fresh"]
    assert (starts, checks, order, started) == ("1", "0", "start", "False"), sp
    assert rest.startswith("True|") and "not logged on" in rest

    # Ran and exited with 78: Started (Wait-PewPtzHealthy reports the reason), never Soft.
    starts, checks, order, started, rest = sp["ran_and_exited"]
    assert (starts, checks, started) == ("1", "0", "True"), sp
    assert rest.startswith("False|") and "78" in rest

    # Nothing happens and no session: two starts first, then Soft (session looked at twice).
    starts, checks, order, started, rest = sp["never_takes_logged_off"]
    assert (starts, checks, order, started) == ("2", "2", "start,start,session,session", "False"), sp
    assert rest.startswith("True|") and "no desktop session" in rest

    # Nothing happens although the user is logged on: a real failure.
    starts, checks, order, started, rest = sp["never_takes_logged_on"]
    assert (starts, order, started) == ("2", "start,start,session", "THROW"), sp
    assert "did not start after 2 attempts" in rest

    # Start-ScheduledTask itself refuses with the not-logged-on HRESULT.
    starts, checks, order, started, rest = sp["throws_refused"]
    assert (starts, started) == ("1", "False"), sp
    assert rest.startswith("True|")


VENV_SCRIPT = r"""
$i = Get-PewVenvPythonInfo -VenvPython $env:PEW_GOOD_PY
'VENV good {0}|{1}|{2}' -f $i.Ok, $i.Version, $i.BasePrefix
$i = Get-PewVenvPythonInfo -VenvPython $env:PEW_MISSING_PY
'VENV missing {0}|{1}' -f $i.Ok, $i.Detail
$i = Get-PewVenvPythonInfo -VenvPython $env:PEW_FAILING_PY
'VENV failing {0}|{1}' -f $i.Ok, $i.Detail
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_venv_python_info(shell: str, tmp_path: Path):
    failing = tmp_path / "fail.cmd"
    failing.write_text("@echo No Python at 'C:\\gone\\python.exe' 1>&2\r\n@exit /b 103\r\n",
                       encoding="ascii")
    out = _tagged(run_ps(shell, VENV_SCRIPT, env={
        "PEW_GOOD_PY": sys.executable,
        "PEW_MISSING_PY": str(tmp_path / "nope" / "python.exe"),
        "PEW_FAILING_PY": str(failing),
    }), "VENV")
    ok, version, base = out["good"].split("|")
    assert ok == "True"
    assert version == sys.version.split()[0]
    assert base == sys.base_prefix
    assert out["missing"].startswith("False|") and "does not exist" in out["missing"]
    assert out["failing"].startswith("False|") and "exit 103" in out["failing"]
    assert "No Python at" in out["failing"]


# --------------------------------------------------------------------------
# (c) netstat parsing, health verdict, task settings
# --------------------------------------------------------------------------

NETSTAT_SAMPLE = "\r\n".join([
    "",
    "Active Connections",
    "",
    "  Proto  Local Address          Foreign Address        State           PID",
    "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1868",
    "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       4242",
    "  TCP    0.0.0.0:18080          0.0.0.0:0              LISTENING       5555",
    "  TCP    127.0.0.1:8081         0.0.0.0:0              LISTENING       8181",
    # established both ways, :8080 in the local or the foreign column
    "  TCP    127.0.0.1:8080         127.0.0.1:50123        ESTABLISHED     4242",
    "  TCP    127.0.0.1:50123        127.0.0.1:8080         ESTABLISHED     7777",
    # TIME_WAIT / FIN_WAIT_2 rows keep :8080 and sometimes a dead PID
    "  TCP    192.168.100.10:8080    192.168.100.50:51515   TIME_WAIT       0",
    "  TCP    192.168.100.10:52000   192.168.100.88:8080    TIME_WAIT       0",
    "  TCP    192.168.100.10:8080    192.168.100.51:51516   FIN_WAIT_2      3131",
    # localized State text (German, and a two-word one)
    "  TCP    0.0.0.0:8080           0.0.0.0:0              ABH\u00d6REN         4343",
    "  TCP    192.168.100.10:8080    0.0.0.0:0              EN ESCUCHA      4344",
    # IPv6 listeners and connections
    "  TCP    [::]:8080              [::]:0                 LISTENING       6060",
    "  TCP    [::]:18080             [::]:0                 LISTENING       6161",
    "  TCP    [fe80::1%7]:8080       [::]:0                 LISTENING       7171",
    "  TCP    [::1]:49999            [::1]:8080             ESTABLISHED     7878",
    "  TCP    [::1]:8080             [::1]:49999            ESTABLISHED     6060",
    # UDP is never a TCP listener
    "  UDP    0.0.0.0:8080           *:*                                    9090",
    "  UDP    [::]:8080              *:*                                    9091",
    "",
])

LISTENER_SCRIPT = r"""
$lines = Get-Content -LiteralPath $env:PEW_NETSTAT_SAMPLE -Encoding UTF8
function Show-Pids($key, $listeners) {
  'PORT {0} {1}' -f $key, ((@($listeners | ForEach-Object { $_.Pid }) | Sort-Object) -join ',')
}
foreach ($port in 8080, 18080, 8081, 135, 9999) {
  Show-Pids $port @(Get-PewListeners -Port $port -NetstatLines $lines)
}
Show-Pids 'v6' @(Get-PewListeners -Port 8080 -NetstatLines $lines | Where-Object { $_.IPv6 })
Show-Pids 'joined' @(Get-PewListeners -Port 18080 -NetstatLines ($lines -join "`n"))
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_get_pew_listeners_parses_sample(shell: str, tmp_path: Path):
    sample = tmp_path / "netstat.txt"
    sample.write_text(NETSTAT_SAMPLE, encoding="utf-8")
    lines = run_ps(shell, LISTENER_SCRIPT, env={"PEW_NETSTAT_SAMPLE": str(sample)})
    ports = _tagged(lines, "PORT")

    def pids(key: str) -> set[int]:
        return {int(p) for p in ports[key].split(",") if p}

    assert pids("8080") == {4242, 4343, 4344, 6060, 7171}
    assert pids("18080") == {5555, 6161}
    assert pids("8081") == {8181}
    assert pids("135") == {1868}
    assert pids("9999") == set()
    assert pids("v6") == {6060, 7171}
    assert pids("joined") == {5555, 6161}


@pytest.mark.parametrize("shell", SHELLS)
def test_get_pew_listeners_sees_a_real_socket(shell: str):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        script = (f"@(Get-PewListeners -Port {port}) | "
                  "ForEach-Object { 'L ' + $_.Pid + ' ' + $_.LocalAddress }")
        lines = run_ps(shell, script)
    assert lines == [f"L {os.getpid()} 127.0.0.1:{port}"]


HEALTH_SCRIPT = r"""
function New-Listener($address, $id) {
  [pscustomobject]@{ LocalAddress = $address; Pid = $id; IPv6 = $address.StartsWith('[') }
}
$one = @(New-Listener '0.0.0.0:8080' 100)
$two = $one + @(New-Listener '[::]:8080' 200)
$other = @(New-Listener '0.0.0.0:8080' 300)
function New-FakeHealth([hashtable]$change, [string[]]$drop) {
  $h = [ordered]@{
    ok = $true; uptime_s = 3; keyboard_ok = $true; pid = 100; started_at = 1000
    version = '1.0.0+abc1234'; build = '0123456789abcdef'; restart_needed = $false
  }
  if ($change) { foreach ($k in $change.Keys) { $h[$k] = $change[$k] } }
  foreach ($k in @($drop)) { if ($k) { $h.Remove($k) } }
  [pscustomobject]$h
}
function Show-Verdict($name, $health, $listeners, $build = '0123456789abcdef', $err = '') {
  $reason = Test-PewHealth -Health $health -RequestError $err -Listeners $listeners `
    -Port 8080 -StartedAfter 1001 -ExpectedBuild $build
  if ($reason) { 'CASE {0} {1}' -f $name, $reason } else { 'CASE {0} HEALTHY' -f $name }
}
$oldKeys = @('build', 'version', 'pid', 'started_at', 'restart_needed')
Show-Verdict 'healthy' (New-FakeHealth) $one
Show-Verdict 'nobuildcheck' (New-FakeHealth) $one ''
Show-Verdict 'oldserver' (New-FakeHealth $null $oldKeys) $one
Show-Verdict 'stale' (New-FakeHealth @{ started_at = 900 }) $one
Show-Verdict 'restart' (New-FakeHealth @{ restart_needed = $true }) $one
Show-Verdict 'wrongbuild' (New-FakeHealth @{ build = 'ffffffffffffffff' }) $one
Show-Verdict 'twolisteners' (New-FakeHealth) $two
Show-Verdict 'pidmismatch' (New-FakeHealth) $other
Show-Verdict 'nothing' $null @() '0123456789abcdef' 'refused'
Show-Verdict 'noanswer' $null $one '0123456789abcdef' 'timed out'
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_health_verdicts(shell: str):
    cases = _tagged(run_ps(shell, HEALTH_SCRIPT), "CASE")
    assert cases["healthy"] == "HEALTHY"
    assert cases["nobuildcheck"] == "HEALTHY"
    assert cases["oldserver"].startswith("old server still answering")
    assert cases["stale"].startswith("old server still answering")
    assert "restart_needed=true" in cases["restart"]
    assert cases["wrongbuild"].startswith("wrong build")
    assert cases["twolisteners"].startswith("two listeners")
    assert "PID 300" in cases["pidmismatch"]
    assert cases["nothing"].startswith("nothing listening")
    assert "did not answer" in cases["noanswer"]


TASK_SETTINGS_SCRIPT = r"""
function New-FakeTask($execute, $arguments, $workdir, $user) {
  $action = [pscustomobject]@{
    Execute = $execute; Arguments = $arguments; WorkingDirectory = $workdir
  }
  [pscustomobject]@{
    TaskName = 'Pew PTZ Controller'; TaskPath = '\'; State = 'Ready'
    Actions = @($action); Principal = [pscustomobject]@{ UserId = $user }
  }
}
function Show-Settings($key, $s) {
  'TS {0} {1}|{2}|{3}|{4}|{5}' -f $key, $s.InstallDir, $s.TaskUser, $s.Port, $s.LogDir, $s.IsLegacy
}
$newArgs = '-m pew_ptz.supervise --port 9090 --log-dir "C:\pp dir\logs"'
$new = New-FakeTask 'C:\pp dir\.venv\Scripts\pythonw.exe' $newArgs 'C:\pp dir' 'HOSTPC\Chapel-AV'
Show-Settings 'new' (Get-PewTaskSettings -Task $new)
$old = New-FakeTask $env:PEW_LAUNCH_CMD $null $env:PEW_LEGACY_DIR 'Chapel-AV'
Show-Settings 'legacy' (Get-PewTaskSettings -Task $old)
$missing = 'pewctl-test-no-such-task-' + [guid]::NewGuid().ToString('N')
'TS missing {0}' -f ($null -eq (Get-PewTaskSettings -TaskName $missing))
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_task_settings(shell: str, tmp_path: Path):
    legacy_dir = tmp_path / "legacy"
    (legacy_dir / "scripts").mkdir(parents=True)
    launch = legacy_dir / "scripts" / "launch.cmd"
    launch.write_text(
        "@echo off\r\nset PEW_PTZ_LOG_DIR=C:\\x\\logs\r\nset PEW_PTZ_SERVER_PORT=8181\r\n"
        'start "" "C:\\x\\.venv\\Scripts\\pythonw.exe" -m pew_ptz\r\n',
        encoding="ascii",
    )
    env = {"PEW_LAUNCH_CMD": str(launch), "PEW_LEGACY_DIR": str(legacy_dir)}
    ts = _tagged(run_ps(shell, TASK_SETTINGS_SCRIPT, env=env), "TS")
    assert ts["new"] == r"C:\pp dir|Chapel-AV|9090|C:\pp dir\logs|False"
    assert ts["legacy"] == f"{legacy_dir}|Chapel-AV|8181|{legacy_dir}\\logs|True"
    assert ts["missing"] == "True"


# --------------------------------------------------------------------------
# Invoke-Git: stderr must not throw under 5.1 with ErrorActionPreference Stop
# --------------------------------------------------------------------------

GIT_SCRIPT = r"""
$repo = $env:PEW_REPO
$r = Invoke-Git -InstallDir $repo -Arguments @('rev-parse', '--verify', 'refs/heads/pewctl-no-such')
'GIT neg {0}|{1}' -f $r.Code, ($r.Text -replace '\s+', ' ')
$upstreamArgs = @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
$r = Invoke-Git -InstallDir $repo -Arguments $upstreamArgs
'GIT upstream {0}' -f ($r.Code -is [int])
$env:GIT_TEST_ASSUME_DIFFERENT_OWNER = '1'
$r = Invoke-Git -InstallDir $repo -Arguments @('rev-parse', '--is-inside-work-tree')
'GIT owner {0}|{1}' -f $r.Code, ($r.Text -replace '\s+', ' ')
"""


def _git_or_skip() -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not on PATH")
    if not (ROOT / ".git").exists():
        pytest.skip("not running from a git checkout")
    return git


@pytest.mark.parametrize("shell", SHELLS)
def test_invoke_git(shell: str):
    git = _git_or_skip()
    out = _tagged(run_ps(shell, GIT_SCRIPT, env={"PEW_REPO": str(ROOT)}), "GIT")
    code, text = out["neg"].split("|", 1)
    assert int(code) != 0
    assert "fatal" in text
    assert out["upstream"] == "True"

    # Does this git honour the ownership test knob? If so, the per-call
    # safe.directory must be what lets Invoke-Git through.
    probe = subprocess.run(
        [git, "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
    )
    if probe.returncode == 0 or "dubious" not in probe.stderr:
        pytest.skip("git ignores GIT_TEST_ASSUME_DIFFERENT_OWNER here")
    if (ROOT / ".git").is_file():
        pytest.skip("worktree checkout: ownership of the common git dir is checked too")
    assert out["owner"] == "0|true"


# --------------------------------------------------------------------------
# Wait-PewPtzHealthy against a fake /healthz on 127.0.0.1
# --------------------------------------------------------------------------

FAKE_HEALTHZ = r'''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

mode, build, port_file = sys.argv[1], sys.argv[2], sys.argv[3]
started = int(time.time())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"ok": True, "uptime_s": 1, "keyboard_ok": True}
        if mode != "old":  # the pre-change server has only these three keys
            body.update(pid=os.getpid(), started_at=started, version="1.0.0+test",
                        build=build, restart_needed=False)
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


srv = HTTPServer(("127.0.0.1", 0), Handler)
with open(port_file + ".tmp", "w") as f:
    f.write(str(srv.server_address[1]))
os.replace(port_file + ".tmp", port_file)
srv.serve_forever()
'''

WAIT_SCRIPT = r"""
$common = @{ StartedAfter = [long]$env:PEW_STARTED_AFTER; LogDir = $env:PEW_LOG_DIR }
$good = [int]$env:PEW_GOOD_PORT
$h = Wait-PewPtzHealthy -Port $good -ExpectedBuild $env:PEW_BUILD -TimeoutSec 15 @common
'WAIT good {0}|{1}|{2}' -f $h.Build, $h.Version, ($h.Pid -gt 0)
function Get-Failure($port, $build, $timeout) {
  try {
    $null = Wait-PewPtzHealthy -Port $port -ExpectedBuild $build -TimeoutSec $timeout @common
    'NO ERROR'
  } catch {
    ($_.Exception.Message -split "`n") -join ' || '
  }
}
'WAIT wrongbuild {0}' -f (Get-Failure $good 'ffffffffffffffff' 2)
'WAIT old {0}' -f (Get-Failure ([int]$env:PEW_OLD_PORT) $env:PEW_BUILD 2)
'WAIT nothing {0}' -f (Get-Failure ([int]$env:PEW_FREE_PORT) $env:PEW_BUILD 1)
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_wait_pewptz_healthy_against_fake_server(shell: str, tmp_path: Path):
    _shell_exe(shell)
    marker = "pewctltest" + uuid.uuid4().hex
    script = tmp_path / "fake_healthz.py"
    script.write_text(FAKE_HEALTHZ, encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "server.log").write_text(f"line one\nLAST-SERVER-LINE {marker}\n", encoding="utf-8")
    build = "0123456789abcdef"
    started_after = int(time.time()) - 1
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    procs: list[subprocess.Popen] = []
    ports = {}
    try:
        for mode in ("good", "old"):
            port_file = tmp_path / f"{mode}.port"
            procs.append(subprocess.Popen(
                [sys.executable, str(script), mode, build, str(port_file), marker],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags))
            deadline = time.monotonic() + 15
            while not port_file.exists():
                assert time.monotonic() < deadline, f"fake {mode} server did not start"
                assert procs[-1].poll() is None, f"fake {mode} server exited"
                time.sleep(0.05)
            ports[mode] = port_file.read_text()
        out = _tagged(run_ps(shell, WAIT_SCRIPT, env={
            "PEW_STARTED_AFTER": str(started_after),
            "PEW_LOG_DIR": str(logs),
            "PEW_BUILD": build,
            "PEW_GOOD_PORT": ports["good"],
            "PEW_OLD_PORT": ports["old"],
            "PEW_FREE_PORT": str(_free_port()),
        }, timeout=60), "WAIT")
        assert out["good"] == f"{build}|1.0.0+test|True"
        assert "wrong build" in out["wrongbuild"]
        assert "old server still answering" in out["old"]
        assert "nothing listening" in out["nothing"]
        # the failure message carries the log tails
        assert f"LAST-SERVER-LINE {marker}" in out["nothing"]
        assert "supervisor.log (not found)" in out["nothing"]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        _sweep(shell, marker)


# --------------------------------------------------------------------------
# (d) Stop-PewPtz against dummy process chains
# --------------------------------------------------------------------------

# A parent that puts its child in a kill-on-close job, like the supervisor
# does: killing the parent takes the child down within milliseconds, so the
# child's PID is often gone by the time Stop-PewPtz gets to it.
PARENT_SCRIPT = r'''
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
k32.SetInformationJobObject.restype = wintypes.BOOL
k32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
k32.AssignProcessToJobObject.restype = wintypes.BOOL
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]


class Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class Io(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]


class Extended(ctypes.Structure):
    _fields_ = [
        ("Basic", Basic),
        ("Io", Io),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


job = k32.CreateJobObjectW(None, None)
info = Extended()
info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
# 9 = JobObjectExtendedLimitInformation
k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", sys.argv[2]])
k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(child._handle)))
time.sleep(120)
'''

STOP_SCRIPT = r"""
$marker = $env:PEW_MARKER
$want = [int]$env:PEW_EXPECTED
$until = (Get-Date).AddSeconds(15)
do {
  $n = @(Get-PewProcesses -Pattern $marker).Count
  if ($n -ge $want) { break }
  Start-Sleep -Milliseconds 200
} while ((Get-Date) -lt $until)
'STOP before {0}' -f $n
$target = @{ TaskName = $env:PEW_TASK; Port = [int]$env:PEW_PORT; Pattern = $marker }
$t0 = Get-Date
$stopped = Stop-PewPtz @target -TimeoutSec 20 -SupervisorPattern ($marker + '-[a-z]-sup')
'STOP result {0}' -f $stopped
'STOP seconds {0:N1}' -f ((Get-Date) - $t0).TotalSeconds
'STOP after {0}' -f @(Get-PewProcesses -Pattern $marker).Count
'STOP again {0}' -f (Stop-PewPtz @target -TimeoutSec 5)
"""


@pytest.mark.parametrize("shell", SHELLS)
def test_stop_pewptz_kills_dummy_chains(shell: str, tmp_path: Path):
    _shell_exe(shell)
    marker = "pewctltest" + uuid.uuid4().hex
    parent = tmp_path / "parent.py"
    parent.write_text(PARENT_SCRIPT, encoding="utf-8")
    sleeper = "import time; time.sleep(120)"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # In a venv, sys.executable is the venv launcher, which runs the real
    # interpreter as a child with the same command line: two processes each.
    per = 2 if sys.prefix != sys.base_prefix else 1
    commands = [
        [sys.executable, "-c", sleeper, f"{marker}-a"],
        [sys.executable, "-c", sleeper, f"{marker}-b-sup"],
        [sys.executable, str(parent), f"{marker}-c-sup", f"{marker}-c-kid"],
    ]
    procs: list[subprocess.Popen] = []
    try:
        for cmd in commands:
            procs.append(subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags))
        out = _tagged(run_ps(shell, STOP_SCRIPT, env={
            "PEW_MARKER": marker,
            "PEW_EXPECTED": str(4 * per),
            "PEW_TASK": "pewctl-test-no-such-task-" + uuid.uuid4().hex,
            "PEW_PORT": str(_free_port()),
        }, timeout=60), "STOP")
        assert int(out["before"]) >= 4, out
        assert out["result"] == "True", out
        assert out["after"] == "0", out
        assert out["again"] == "False", out
        for p in procs:
            p.wait(timeout=10)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        _sweep(shell, marker)
