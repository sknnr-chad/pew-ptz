"""Startup: CLI, settings validation and claiming the port.

Socket tests bind 127.0.0.1 only, so no Windows Firewall prompt appears."""

import os
import socket
import subprocess
import sys
import tomllib
import types
from pathlib import Path

import pytest

from pew_ptz import cli, server
from pew_ptz.exitcodes import EXIT_CONFIG, EXIT_PORT_IN_USE

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_python(args, tmp_path, **env):
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("PEW_PTZ_")}
    full_env.update(PEW_PTZ_ENV_FILE=str(tmp_path / "none.env"), PEW_PTZ_CAMERA_IP="127.0.0.1",
                    PYTHONPATH=str(ROOT))
    full_env.update(env)
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=full_env,
                          capture_output=True, text=True, timeout=120)


# ---- claiming the port ----

def test_bind_listener_claims_the_port():
    port = free_port()
    sock = server.bind_listener(port, "127.0.0.1", wait_s=0)
    try:
        with pytest.raises(server.PortInUse) as e:
            server.bind_listener(port, "127.0.0.1", wait_s=5)
        assert e.value.live  # it answered, so no 5 s wait was needed
    finally:
        sock.close()
    server.bind_listener(port, "127.0.0.1", wait_s=0).close()  # free again


@pytest.mark.skipif(sys.platform != "win32", reason="Windows socket sharing rules")
def test_old_style_server_cannot_share_our_port():
    # Waitress on its own sets SO_REUSEADDR; on Windows that used to let a
    # second copy bind the same port silently.
    port = free_port()
    sock = server.bind_listener(port, "127.0.0.1", wait_s=0)
    try:
        old = socket.socket()
        old.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            old.bind(("127.0.0.1", port))
        old.close()
    finally:
        sock.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows socket sharing rules")
def test_we_refuse_to_share_with_an_old_style_server():
    # The rollout case: an old copy (SO_REUSEADDR) still holds the port.
    port = free_port()
    old = socket.socket()
    old.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    old.bind(("127.0.0.1", port))
    try:
        with pytest.raises(server.PortInUse) as e:
            server.bind_listener(port, "127.0.0.1", wait_s=0)
        assert not e.value.live  # bound but not listening
    finally:
        old.close()


def test_main_exits_port_in_use_without_touching_camera_or_zoom(camera, zoom_reader,
                                                                monkeypatch, caplog):
    port = free_port()
    holder = server.bind_listener(port, "127.0.0.1", wait_s=0)
    monkeypatch.setattr(server, "SERVER_PORT", port)
    monkeypatch.setattr(server, "BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_log_uncaught", lambda: None)
    started = []
    monkeypatch.setattr(zoom_reader, "start", lambda: started.append(1), raising=False)
    try:
        assert server.main() == EXIT_PORT_IN_USE
    finally:
        holder.close()
    assert camera.calls == []
    assert started == []
    assert "already in use" in caplog.text


def test_main_serves_on_the_claimed_socket(camera, zoom_reader, monkeypatch):
    port = free_port()
    monkeypatch.setattr(server, "SERVER_PORT", port)
    monkeypatch.setattr(server, "BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_log_uncaught", lambda: None)
    served = {}

    def fake_serve(app, **kwargs):
        served.update(kwargs)
        kwargs["sockets"][0].close()

    monkeypatch.setattr(server, "serve", fake_serve)
    zoom_reader.available = False
    assert server.main() == 0
    assert set(served) == {"sockets", "threads"}  # waitress rejects host/port with sockets
    assert ("focus_auto", (True,), {}) in camera.calls


# ---- settings ----

@pytest.mark.parametrize("name,value", [
    ("NUDGE_STEP", "3.5"),
    ("NUDGE_STEP", "0"),
    ("SERVER_PORT", "70000"),
    ("SERVER_PORT", ""),
    ("VISCA_PORT", "abc"),
    ("COPY_SETTLE_SECONDS", "soon"),
    ("COPY_SETTLE_SECONDS", "-1"),
])
def test_bad_numeric_setting_exits_config_and_says_why(tmp_path, name, value):
    r = run_python(["-c", "import pew_ptz.server"], tmp_path, **{f"PEW_PTZ_{name}": value})
    assert r.returncode == EXIT_CONFIG
    assert f"PEW_PTZ_{name}" in r.stdout + r.stderr


def test_bad_setting_is_logged_to_server_log(tmp_path):
    logs = tmp_path / "logs"
    r = run_python(["-c", "import pew_ptz.server"], tmp_path,
                   PEW_PTZ_NUDGE_STEP="lots", PEW_PTZ_LOG_DIR=str(logs))
    assert r.returncode == EXIT_CONFIG
    assert "PEW_PTZ_NUDGE_STEP" in (logs / "server.log").read_text(encoding="utf-8")


# ---- command line ----

def test_cli_options_become_environment(monkeypatch):
    monkeypatch.delenv("PEW_PTZ_SERVER_PORT", raising=False)
    monkeypatch.delenv("PEW_PTZ_LOG_DIR", raising=False)
    cli.apply_to_env(cli.parse_args(["--port", "9001", "--log-dir", "C:/x/logs"]))
    assert os.environ["PEW_PTZ_SERVER_PORT"] == "9001"
    assert os.environ["PEW_PTZ_LOG_DIR"] == "C:/x/logs"


def test_cli_without_options_leaves_environment(monkeypatch):
    monkeypatch.setenv("PEW_PTZ_SERVER_PORT", "8123")
    cli.apply_to_env(cli.parse_args([]))
    assert os.environ["PEW_PTZ_SERVER_PORT"] == "8123"


@pytest.mark.parametrize("args", [["--port", "abc"], ["--bogus"]])
def test_bad_cli_args_exit_config(tmp_path, args):
    r = run_python(["-m", "pew_ptz", *args], tmp_path)
    assert r.returncode == EXIT_CONFIG


def test_console_script_uses_the_cli():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert scripts["project"]["scripts"]["pew-ptz"] == "pew_ptz.cli:main"


def test_supervisor_does_not_import_the_server(tmp_path):
    r = run_python(["-c", "import sys, pew_ptz.supervise; "
                          "print('pew_ptz.server' in sys.modules)"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"


def test_unreadable_env_file_exits_config_and_says_why(tmp_path):
    env = tmp_path / "bad.env"
    env.write_bytes("PEW_PTZ_CONTACT_NAME=Zoë\n".encode("cp1252"))
    logs = tmp_path / "logs"
    r = run_python(["-c", "import pew_ptz.server"], tmp_path,
                   PEW_PTZ_ENV_FILE=str(env), PEW_PTZ_LOG_DIR=str(logs))
    assert r.returncode == EXIT_CONFIG
    assert "save it as UTF-8" in (logs / "server.log").read_text(encoding="utf-8")


def test_cli_fingerprints_before_starting_the_server(monkeypatch):
    # The build must describe code on disk no later than when it was loaded.
    import pew_ptz
    from pew_ptz import version

    order = []
    fake_server = types.ModuleType("pew_ptz.server")
    fake_server.main = lambda: order.append("server") or 0
    monkeypatch.setitem(sys.modules, "pew_ptz.server", fake_server)
    monkeypatch.setattr(pew_ptz, "server", fake_server)
    monkeypatch.setattr(version, "disk_build", lambda: order.append("build") or "f" * 16)
    monkeypatch.setattr(version, "STARTUP_BUILD", None)
    assert cli.main([]) == 0
    assert order == ["build", "server"]
    assert version.STARTUP_BUILD == "f" * 16
