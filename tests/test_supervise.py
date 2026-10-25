import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pew_ptz import cli, supervise
from pew_ptz.exitcodes import EXIT_CONFIG, EXIT_OK, EXIT_PORT_IN_USE


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run_with(codes, run_times=None):
    """Drive supervise() with a scripted list of child exit codes."""
    clock = Clock()
    codes = list(codes)
    run_times = list(run_times or [1.0] * len(codes))
    sleeps = []

    def spawn(cmd, log_dir, job):
        clock.now += run_times.pop(0)
        return codes.pop(0)

    def sleep(s):
        sleeps.append(s)
        clock.now += s

    rc = supervise.supervise(["cmd"], None, supervise._NoJob(), spawn=spawn, sleep=sleep,
                             clock=clock)
    return rc, sleeps, codes


@pytest.mark.parametrize("final", [EXIT_OK, EXIT_CONFIG, EXIT_PORT_IN_USE])
def test_stops_on_codes_that_a_restart_would_not_fix(final):
    rc, sleeps, _ = run_with([final])
    assert rc == final
    assert sleeps == []


def test_restarts_crashes_with_backoff():
    crash = 0xC0000005
    rc, sleeps, _ = run_with([1, crash, 1, 1, 1, 1, 1, 0])
    assert rc == 0
    assert sleeps == [1, 5, 10, 20, 40, 60, 60]


def test_backoff_resets_after_a_healthy_run():
    rc, sleeps, _ = run_with([1, 1, 1, 1, 0], run_times=[1, 1, 600, 1, 1])
    assert sleeps == [1, 5, 1, 5]


def test_python_abort_code_3_is_restarted():
    # Python exits 3 on abort() with faulthandler on; that's a crash.
    rc, sleeps, _ = run_with([3, 0])
    assert sleeps == [1]


def test_describe_rc():
    assert supervise.describe_rc(0xC0000005) == "exit code 0xC0000005 (access violation)"
    assert supervise.describe_rc(-1) == "exit code 0xFFFFFFFF (terminated)"
    assert supervise.describe_rc(1) == "exit code 1 (Python error or killed)"
    assert supervise.describe_rc(42) == "exit code 42"


def test_child_command_passes_options_through():
    args = cli.parse_args(["--port", "9000", "--log-dir", "D:/logs"])
    assert supervise.child_command(args) == [
        sys.executable, "-m", "pew_ptz", "--port", "9000", "--log-dir", "D:/logs"]
    assert supervise.child_command(cli.parse_args([])) == [sys.executable, "-m", "pew_ptz"]


def test_stderr_log_rotates_when_large(tmp_path):
    path = tmp_path / supervise.STDERR_LOG
    path.write_bytes(b"x" * (supervise.STDERR_MAX_BYTES + 1))
    f = supervise.open_stderr_log(str(tmp_path))
    f.close()
    assert (tmp_path / (supervise.STDERR_LOG + ".1")).stat().st_size > supervise.STDERR_MAX_BYTES
    assert b"=== server starting" in path.read_bytes()


def test_child_stderr_is_captured(tmp_path):
    # Errors before logging is set up (and native crash dumps) land here.
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('boom-marker'); sys.exit(7)"]
    rc = supervise.run_once(cmd, str(tmp_path), supervise._NoJob())
    assert rc == 7
    assert "boom-marker" in (tmp_path / supervise.STDERR_LOG).read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_job_object_kills_children_when_closed():
    job = supervise.WinJob()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job.add(proc.pid)
        job.close()  # what happens when the supervisor dies
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_killing_the_supervisor_kills_the_server(tmp_path):
    """End to end: a supervisor process runs a sleeping 'server' in its job;
    killing the supervisor (as stopping the task does) takes the server down."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "sup.py"
    script.write_text(
        "import sys\n"
        "from pew_ptz import supervise\n"
        "code = 'import os, sys, time; open(sys.argv[1], \"w\").write(str(os.getpid())); "
        "time.sleep(120)'\n"
        "child = [sys.executable, '-c', code, sys.argv[1]]\n"
        "supervise.supervise(child, None, supervise.make_job())\n",
        encoding="utf-8")
    sup = subprocess.Popen([sys.executable, str(script), str(pidfile)])
    try:
        deadline = time.monotonic() + 30
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline, "child never started"
            time.sleep(0.1)
        child_pid = int(pidfile.read_text())
        sup.kill()
        sup.wait(timeout=10)
        deadline = time.monotonic() + 10
        while _pid_alive(child_pid):
            assert time.monotonic() < deadline, "server outlived the supervisor"
            time.sleep(0.1)
    finally:
        if sup.poll() is None:
            sup.kill()


def _pid_alive(pid: int) -> bool:
    import ctypes

    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102  # WAIT_TIMEOUT
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def test_failure_to_start_is_retried_like_a_crash():
    calls = []

    def spawn(cmd, log_dir, job):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("launcher missing")
        return EXIT_OK

    sleeps = []
    rc = supervise.supervise(["cmd"], None, supervise._NoJob(), spawn=spawn,
                             sleep=sleeps.append, clock=lambda: 0.0)
    assert rc == EXIT_OK
    assert sleeps == [1]


# ---- supervise.main: logging comes first, .env is read but not exported ----

@pytest.fixture
def run_main(tmp_path, monkeypatch):
    """Run supervise.main() in-process with the loop and OS hooks stubbed."""
    seen = {}

    def fake_supervise(cmd, log_dir, job):
        seen.update(cmd=cmd, log_dir=log_dir, environ=dict(os.environ))
        return EXIT_OK

    monkeypatch.setattr(supervise, "supervise", fake_supervise)
    monkeypatch.setattr(supervise, "watch_parent", lambda job: False)
    monkeypatch.setattr(supervise, "make_job", supervise._NoJob)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PEW_PTZ_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("PEW_PTZ_LOG_DIR", raising=False)
    logs = tmp_path / "logs"

    def run(*args):
        try:
            return supervise.main(["--log-dir", str(logs), *args])
        finally:
            supervise.setup_logging(None)  # close supervisor.log

    yield run, seen, tmp_path, logs


def read_log(logs):
    return (logs / "supervisor.log").read_text(encoding="utf-8")


def test_main_does_not_export_env_file(run_main, monkeypatch):
    run, seen, tmp, logs = run_main
    monkeypatch.delenv("PEW_PTZ_TITLE", raising=False)
    (tmp / ".env").write_text("PEW_PTZ_TITLE=from-file\n", encoding="utf-8")
    assert run() == EXIT_OK
    # The server reads .env itself on every start, picking up edits and
    # logging what it applied.
    assert "PEW_PTZ_TITLE" not in seen["environ"]
    assert "supervisor starting" in read_log(logs)


def test_main_logs_a_broken_env_file_and_carries_on(run_main):
    run, seen, tmp, logs = run_main
    (tmp / ".env").write_bytes("PEW_PTZ_CONTACT_NAME=Zoë\n".encode("cp1252"))
    assert run() == EXIT_OK
    assert "save it as UTF-8" in read_log(logs)


def test_main_logs_a_bad_command_line(run_main):
    run, seen, tmp, logs = run_main
    with pytest.raises(SystemExit) as e:
        run("--prot", "8080")
    assert e.value.code == EXIT_CONFIG
    assert "bad command line" in read_log(logs)


def test_main_takes_log_dir_from_env_file(run_main, monkeypatch):
    run, seen, tmp, logs = run_main
    other = tmp / "elsewhere"
    (tmp / ".env").write_text(f"PEW_PTZ_LOG_DIR={other}\n", encoding="utf-8")
    try:
        assert supervise.main([]) == EXIT_OK
    finally:
        supervise.setup_logging(None)
    assert seen["log_dir"] == str(other)
    assert "supervisor starting" in read_log(other)


# ---- the production process tree ----

SLEEPER = ("import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
           "time.sleep(int(sys.argv[2]) if len(sys.argv) > 2 else 120)")


def write_wrapper(tmp_path, child_rc=None):
    """A script that runs supervise.main with a stand-in server: it writes its
    pid, then sleeps, or exits with child_rc."""
    wrapper = tmp_path / "wrapper.py"
    child = (f"import sys; sys.exit({child_rc})" if child_rc is not None else SLEEPER)
    wrapper.write_text(
        "import sys\n"
        "from pew_ptz import supervise\n"
        f"CHILD = {child!r}\n"
        "pidfile = sys.argv.pop(1)\n"
        "supervise.child_command = lambda args: [sys.executable, '-c', CHILD, pidfile]\n"
        "sys.exit(supervise.main())\n",
        encoding="utf-8")
    return wrapper


def wait_for_file(path, timeout=30):
    deadline = time.monotonic() + timeout
    while not path.exists() or not path.read_text(encoding="utf-8").strip():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.1)
    return path.read_text(encoding="utf-8").strip()


def wait_dead(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while _pid_alive(pid):
        assert time.monotonic() < deadline, f"pid {pid} still alive"
        time.sleep(0.1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_supervisor_stops_when_its_launcher_dies(tmp_path):
    """watch_parent: if the process that started the supervisor dies, the
    supervisor and the server go too, even without the venv launcher's job."""
    pidfile = tmp_path / "server.pid"
    logs = tmp_path / "logs"
    wrapper = write_wrapper(tmp_path)
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable] + sys.argv[1:])\n"
        "time.sleep(120)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(parent), str(wrapper), str(pidfile),
                             "--log-dir", str(logs)])
    try:
        server_pid = int(wait_for_file(pidfile))
        log_text = (logs / "supervisor.log").read_text(encoding="utf-8")
        assert "watching launcher" in log_text
        launcher_pid = int(log_text.split("watching launcher (pid ")[1].split(")")[0])
        import signal
        os.kill(launcher_pid, signal.SIGTERM)  # TerminateProcess on Windows
        wait_dead(server_pid)
    finally:
        proc.kill()


def make_venv(tmp_path):
    """A bare venv whose interpreter can import pew_ptz from this checkout:
    the same launcher layer as production, without installing anything."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   timeout=120)
    repo = Path(supervise.__file__).resolve().parent.parent
    (venv / "Lib" / "site-packages" / "pew_ptz_checkout.pth").write_text(f"{repo}\n",
                                                                         encoding="utf-8")
    return venv / "Scripts" / "python.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="the venv launcher is Windows-only")
@pytest.mark.parametrize("child_rc", [EXIT_CONFIG, EXIT_PORT_IN_USE])
def test_exit_codes_reach_the_task_through_both_launchers(tmp_path, child_rc):
    venv_python = make_venv(tmp_path)
    wrapper = write_wrapper(tmp_path, child_rc)
    r = subprocess.run([str(venv_python), str(wrapper), str(tmp_path / "x.pid"),
                        "--log-dir", str(tmp_path / "logs")], timeout=120)
    assert r.returncode == child_rc


@pytest.mark.skipif(sys.platform != "win32", reason="the venv launcher is Windows-only")
def test_ending_the_task_ends_the_whole_venv_tree(tmp_path):
    """Root venv launcher -> supervisor -> venv launcher -> server, as the
    Scheduled Task runs it. Killing the root (what End / Stop-ScheduledTask
    do) must leave nothing running."""
    venv_python = make_venv(tmp_path)
    pidfile = tmp_path / "server.pid"
    root = subprocess.Popen([str(venv_python), str(write_wrapper(tmp_path)), str(pidfile),
                             "--log-dir", str(tmp_path / "logs")])
    try:
        server_pid = int(wait_for_file(pidfile))
        root.kill()
        root.wait(timeout=10)
        wait_dead(server_pid)
    finally:
        if root.poll() is None:
            root.kill()
