"""
pew_ptz.supervise — keep the server running.

    python -m pew_ptz.supervise [--port N] [--log-dir DIR]

The Scheduled Task runs this, and this runs `python -m pew_ptz` as a child
process. Task Scheduler's restart-on-failure only covers a task that fails to
start, not a program that starts fine and crashes an hour later, so the
supervisor restarts the server itself: after 1 s the first time, then 5, 10,
20, 40 and 60 s between attempts. The delay goes back to 1 s once the server
has stayed up for 5 minutes.

It does not restart after a clean exit, a configuration error or "port
already in use" (see exitcodes.py); it exits with the same code, which shows
up as the task's Last Run Result.

On Windows the child runs inside a job object that kills it when the
supervisor goes away for any reason, so stopping the task never leaves an
orphaned server holding the port. Stopping the task ends the venv launcher
that started the supervisor; if that launcher ever stopped taking the
supervisor down with it, the supervisor notices and stops itself as well.

Nothing here imports pew_ptz.server: the supervisor must not open the
server's log, bind its port or touch the camera.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pew_ptz import cli, dotenv
from pew_ptz.exitcodes import EXIT_CONFIG, EXIT_OK, EXIT_PORT_IN_USE

log = logging.getLogger("pew_ptz.supervise")

NO_RESTART = {
    EXIT_OK: "it stopped normally",
    EXIT_CONFIG: "of a configuration error (see server.log)",
    EXIT_PORT_IN_USE: "the port is already in use (see server.log)",
}
FIRST_DELAY_S = 1
BACKOFF_S = (5, 10, 20, 40, 60)
HEALTHY_RUN_S = 300
STDERR_LOG = "server-stderr.log"
STDERR_MAX_BYTES = 1_000_000

# Exit codes worth naming in the log. Native crashes have no Python traceback;
# server-stderr.log may have a faulthandler dump.
_RC_NAMES = {
    1: "Python error or killed",
    0xC0000005: "access violation",
    0xC00000FD: "stack overflow",
    0xC0000374: "heap corruption",
    0xC0000409: "fatal error",
    0xFFFFFFFF: "terminated",
}


def describe_rc(rc: int) -> str:
    rc &= 0xFFFFFFFF
    text = f"exit code {rc}" if rc < 0x10000 else f"exit code 0x{rc:08X}"
    return f"{text} ({_RC_NAMES[rc]})" if rc in _RC_NAMES else text


def next_delay(streak: int) -> float:
    """Wait before restart number `streak` (0 = first restart after a good run)."""
    if streak == 0:
        return FIRST_DELAY_S
    return BACKOFF_S[min(streak - 1, len(BACKOFF_S) - 1)]


def child_command(args) -> list[str]:
    # sys.executable is the venv's pythonw.exe launcher in production, so the
    # child runs in the same venv.
    cmd = [sys.executable, "-m", "pew_ptz"]
    if args.port is not None:
        cmd += ["--port", str(args.port)]
    if args.log_dir:
        cmd += ["--log-dir", args.log_dir]
    return cmd


def open_stderr_log(log_dir: str | None):
    """Append handle for the child's stderr, rotated while no child is running.
    Catches what Python's logging can't: import errors before logging is set up
    and faulthandler dumps from native crashes."""
    if not log_dir:
        return None if sys.stderr is not None else subprocess.DEVNULL
    path = Path(log_dir) / STDERR_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > STDERR_MAX_BYTES:
            os.replace(path, path.with_name(STDERR_LOG + ".1"))
        f = open(path, "ab")  # handed to the child; run_once closes our copy
        f.write(f"\n=== server starting {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        f.flush()
        return f
    except OSError as e:
        log.warning("can't open %s: %s", path, e)
        return subprocess.DEVNULL


class _NoJob:
    def add(self, pid: int) -> None:
        pass

    def kill_all(self) -> None:
        pass


class WinJob:
    """A job object that kills every process in it when its last handle
    closes, i.e. when the supervisor exits or is killed."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32

        class BasicLimits(ctypes.Structure):
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

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self.handle, 9,  # ExtendedLimitInformation
                                           ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, pid: int) -> None:
        h = self._k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not h:
            raise self._ct.WinError(self._ct.get_last_error())
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, h):
                raise self._ct.WinError(self._ct.get_last_error())
        finally:
            self._k32.CloseHandle(h)

    def kill_all(self) -> None:
        self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def make_job():
    if sys.platform != "win32":
        return _NoJob()
    try:
        return WinJob()
    except OSError as e:
        log.error("can't create a job object (%s); a killed supervisor may leave "
                  "the server running", e)
        return _NoJob()


def watch_parent(job) -> bool:
    """Stop when the venv launcher that started us exits. Only applies when
    the parent is that launcher (same executable as sys.executable)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    ppid = os.getppid()
    h = k32.OpenProcess(0x00100000 | 0x1000, False, ppid)  # SYNCHRONIZE | QUERY_LIMITED
    if not h:
        return False
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    if not ok or os.path.normcase(buf.value) != os.path.normcase(sys.executable):
        k32.CloseHandle(h)
        return False

    def wait():
        k32.WaitForSingleObject(h, 0xFFFFFFFF)
        log.warning("launcher (pid %d) exited; stopping the server", ppid)
        job.kill_all()
        logging.shutdown()
        os._exit(1)

    threading.Thread(target=wait, daemon=True, name="parent-watch").start()
    return True


def run_once(cmd: list[str], log_dir: str | None, job) -> int:
    """Start the server, wait for it to exit, return its exit code."""
    stderr = open_stderr_log(log_dir)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=stderr)
    finally:
        if hasattr(stderr, "close"):
            stderr.close()  # the child has its own handle now
    try:
        job.add(proc.pid)
    except OSError as e:
        log.error("can't put the server (pid %d) in the job object: %s", proc.pid, e)
    log.info("server started (pid %d)", proc.pid)
    return proc.wait()


def supervise(cmd: list[str], log_dir: str | None, job, *, spawn=run_once,
              sleep=time.sleep, clock=time.monotonic) -> int:
    streak = 0
    while True:
        started = clock()
        try:
            rc = spawn(cmd, log_dir, job)
            how = describe_rc(rc)
        except OSError as e:  # couldn't even start it; retry like a crash
            rc, how = None, f"a failure to start ({e})"
        ran = clock() - started
        if rc in NO_RESTART:
            log.info("server exited after %.0f s because %s; not restarting",
                     ran, NO_RESTART[rc])
            return rc
        if ran >= HEALTHY_RUN_S:
            streak = 0
        delay = next_delay(streak)
        streak += 1
        log.warning("server exited after %.0f s with %s; restarting in %d s", ran, how, delay)
        sleep(delay)


def setup_logging(log_dir: str | None) -> list[logging.Handler]:
    """Log to supervisor.log (and stdout when there is one). Its own file:
    two processes rotating one RotatingFileHandler file fails on Windows (the
    rename hits a file the other process has open)."""
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:  # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(Path(log_dir) / "supervisor.log",
                                            maxBytes=1_000_000, backupCount=3,
                                            encoding="utf-8"))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    return handlers


def _log_dir_arg(argv: list[str]) -> str | None:
    """--log-dir from the raw command line, so that even a command line
    argparse rejects gets logged."""
    for i, arg in enumerate(argv):
        if arg == "--log-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--log-dir="):
            return arg.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # .env is only read here as a fallback for the log folder. It is not
    # applied to the environment: each server start reads it afresh, so it
    # picks up edits and logs which settings it applied.
    env_problem = None
    try:
        file_settings = dotenv.read(os.environ.get("PEW_PTZ_ENV_FILE", ".env"))
    except dotenv.EnvFileError as e:
        file_settings, env_problem = {}, e
    log_dir = (_log_dir_arg(argv) or os.environ.get("PEW_PTZ_LOG_DIR")
               or file_settings.get("PEW_PTZ_LOG_DIR") or None)
    setup_logging(log_dir)
    # Under pythonw nothing else would record a crash of the supervisor itself.
    sys.excepthook = lambda *exc: log.critical("supervisor crashed", exc_info=exc)
    if env_problem is not None:
        log.warning("%s (the server will refuse to start until it's fixed)", env_problem)
    try:
        args = cli.parse_args(argv, prog="pew_ptz.supervise")
    except SystemExit:
        log.error("bad command line %r; check the Scheduled Task's arguments. Not starting.",
                  argv)
        raise
    cmd = child_command(args)
    log.info("supervisor starting (pid %d, parent %d): %s", os.getpid(), os.getppid(),
             " ".join(cmd))
    job = make_job()
    if watch_parent(job):
        log.info("watching launcher (pid %d)", os.getppid())
    return supervise(cmd, log_dir, job)


if __name__ == "__main__":
    raise SystemExit(main())
