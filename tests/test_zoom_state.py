"""Button-name parsing against a fake UI Automation tree (runs on any OS)."""

import pytest

from pew_ptz import zoom_state
from pew_ptz.zoom_state import ZoomStateReader


class Node:
    def __init__(self, name="", control="PaneControl", cls="", children=()):
        self.Name = name
        self.ControlTypeName = control
        self.ClassName = cls
        self._children = list(children)

    def GetChildren(self):
        return self._children


def button(name):
    return Node(name, "ButtonControl")


@pytest.mark.parametrize(
    ("names", "mic_on", "video_on"),
    [
        # Zoom Workplace 7.x: state embedded in the name
        (["Mute, currently unmuted, Alt+A, Noise removal is on. Mute my audio (Alt+A)",
          "Stop my video, Alt+V"], True, True),
        (["Unmute, currently muted, Alt+A, Noise removal is on. Unmute my audio (Alt+A)",
          "Start my video, Alt+V"], False, False),
        # Legacy builds: button-action names
        (["Mute My Microphone", "Stop Video"], True, True),
        (["Unmute my microphone", "Start Video"], False, False),
        (["Unmute my mic"], False, None),
        (["Unmute audio"], False, None),
        # Only one control visible
        (["Stop my video, Alt+V"], None, True),
        (["Leave", "Chat"], None, None),
    ],
)
def test_scan_buttons(names, mic_on, video_on):
    tree = Node(children=[Node(children=[button(n) for n in names])])
    assert ZoomStateReader()._scan_buttons(tree) == (mic_on, video_on)


def test_scan_ignores_non_buttons():
    tree = Node(children=[Node("Mute, currently unmuted", "TextControl")])
    assert ZoomStateReader()._scan_buttons(tree) == (None, None)


def test_scan_respects_max_depth():
    deep = button("Stop my video")
    for _ in range(5):
        deep = Node(children=[deep])
    assert ZoomStateReader(max_walk_depth=3)._scan_buttons(deep) == (None, None)
    assert ZoomStateReader(max_walk_depth=10)._scan_buttons(deep) == (None, True)


@pytest.fixture
def fake_uia(monkeypatch):
    def install(*windows):
        root = Node(children=windows)

        class Auto:
            @staticmethod
            def GetRootControl():
                return root

        monkeypatch.setattr(zoom_state, "auto", Auto)
        monkeypatch.setattr(zoom_state, "_UIA_OK", True)

    return install


def test_read_once_finds_meeting_by_class(fake_uia):
    fake_uia(
        Node("Notepad", cls="Notepad"),
        Node("Zoom", cls="ConfMultiTabContentWndClass",
             children=[button("Unmute, currently muted"), button("Stop my video")]),
    )
    s = ZoomStateReader()._read_once()
    assert (s.observed, s.in_meeting, s.mic_on, s.video_on) == (True, True, False, True)


def test_read_once_falls_back_to_window_name(fake_uia):
    fake_uia(Node("Zoom Meeting", cls="SomethingNew", children=[button("Start my video")]))
    s = ZoomStateReader()._read_once()
    assert (s.in_meeting, s.video_on) == (True, False)


def test_read_once_ignores_zoom_workplace_launcher(fake_uia):
    fake_uia(Node("Zoom Workplace meeting", children=[button("Start my video")]))
    s = ZoomStateReader()._read_once()
    assert (s.observed, s.in_meeting) == (False, False)


def test_read_once_without_uia(monkeypatch):
    monkeypatch.setattr(zoom_state, "_UIA_OK", False)
    assert ZoomStateReader()._read_once().observed is False
