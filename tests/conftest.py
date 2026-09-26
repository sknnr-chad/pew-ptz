"""Shared fixtures.

server.py builds its Flask app, camera client and Zoom reader at import time,
so the environment must be set before the first import. The fixtures then
swap the camera and keyboard for recorders — nothing here sends real UDP to a
camera or real keystrokes to the desktop (important on Windows CI runners).
"""

import os

os.environ.setdefault("PEW_PTZ_CAMERA_IP", "127.0.0.1")
os.environ.setdefault("PEW_PTZ_SKIP_FOCUS_CHECK", "1")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from pew_ptz import server  # noqa: E402
from pew_ptz.presets import PresetStore  # noqa: E402


class FakeCamera:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return record


class FakeZoomReader:
    def __init__(self):
        self.state = {
            "observed": False,
            "in_meeting": False,
            "mic_on": None,
            "video_on": None,
            "last_read": 0.0,
            "last_observed": 0.0,
            "error": None,
            "poll_count": 0,
            "walk_ms": 0,
            "uia_available": False,
        }
        self.refreshes = 0

    def get(self):
        return dict(self.state)

    def trigger_refresh(self):
        self.refreshes += 1


@pytest.fixture
def camera(monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(server, "camera", cam)
    return cam


@pytest.fixture
def zoom_reader(monkeypatch):
    reader = FakeZoomReader()
    monkeypatch.setattr(server, "zoom_reader", reader)
    return reader


@pytest.fixture
def chords(monkeypatch):
    """Records Alt+<letter> chords instead of pressing keys."""
    sent = []

    def fake_chord(letter):
        sent.append(letter)
        return True

    monkeypatch.setattr(server, "_alt_chord", fake_chord)
    monkeypatch.setattr(server, "_kbd", object())
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    return sent


@pytest.fixture(autouse=True)
def default_presets(monkeypatch):
    """Shared-only presets from PEW_PTZ_PRESETS, no state file on disk."""
    monkeypatch.setattr(server, "presets", PresetStore.load(Path("/nonexistent/presets.json"),
                                                            server.PRESETS))


@pytest.fixture
def wards(monkeypatch, tmp_path):
    """Three wards configured via presets.json in a temp dir."""
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "shared": ["Speaker", "Wide"],
        "wards": [
            {"name": "1st Ward", "presets": ["Bishopric", "Choir"]},
            {"name": "2nd Ward", "presets": ["Organ"]},
            {"name": "3rd Ward", "presets": []},
        ],
    }), encoding="utf-8")
    store = PresetStore.load(path, [])
    monkeypatch.setattr(server, "presets", store)
    return store


@pytest.fixture
def client(camera, zoom_reader, chords, monkeypatch):
    monkeypatch.setattr(
        server, "_zoom_state", {"video_on": True, "mic_on": True, "last_toggled": None}
    )
    return server.app.test_client()
