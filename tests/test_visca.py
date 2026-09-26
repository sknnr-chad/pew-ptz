"""VISCA wire format, checked against a real UDP socket on localhost."""

import socket

import pytest

from pew_ptz.visca import PAN_LEFT, TILT_UP, ViscaIP


@pytest.fixture
def cam_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2)
    yield sock
    sock.close()


@pytest.fixture
def visca(cam_socket):
    return ViscaIP("127.0.0.1", cam_socket.getsockname()[1], recv_timeout=0.05)


def _recv(sock):
    frame, _ = sock.recvfrom(64)
    assert frame[:2] == b"\x01\x00"  # payload type: VISCA command
    length = int.from_bytes(frame[2:4], "big")
    seq = int.from_bytes(frame[4:8], "big")
    payload = frame[8:]
    assert len(payload) == length
    return seq, payload


@pytest.mark.parametrize(
    ("send", "expected"),
    [
        (lambda v: v.pan_tilt(PAN_LEFT, TILT_UP, 6, 6),
         bytes([0x81, 0x01, 0x06, 0x01, 6, 6, 0x01, 0x01, 0xFF])),
        (lambda v: v.pan_tilt_stop(),
         bytes([0x81, 0x01, 0x06, 0x01, 1, 1, 0x03, 0x03, 0xFF])),
        (lambda v: v.zoom_tele(2), bytes([0x81, 0x01, 0x04, 0x07, 0x22, 0xFF])),
        (lambda v: v.zoom_wide(2), bytes([0x81, 0x01, 0x04, 0x07, 0x32, 0xFF])),
        (lambda v: v.zoom_stop(), bytes([0x81, 0x01, 0x04, 0x07, 0x00, 0xFF])),
        (lambda v: v.focus_auto(True), bytes([0x81, 0x01, 0x04, 0x38, 0x02, 0xFF])),
        (lambda v: v.focus_auto(False), bytes([0x81, 0x01, 0x04, 0x38, 0x03, 0xFF])),
        (lambda v: v.preset_recall(5), bytes([0x81, 0x01, 0x04, 0x3F, 0x02, 5, 0xFF])),
    ],
)
def test_command_bytes(visca, cam_socket, send, expected):
    send(visca)
    _, payload = _recv(cam_socket)
    assert payload == expected


def test_speeds_and_presets_are_clamped(visca, cam_socket):
    visca.pan_tilt(PAN_LEFT, TILT_UP, pan_speed=99, tilt_speed=0)
    _, payload = _recv(cam_socket)
    assert payload[4:6] == bytes([0x18, 0x01])  # pan max 24, tilt min 1

    visca.zoom_tele(50)
    _, payload = _recv(cam_socket)
    assert payload[4] == 0x27

    visca.preset_recall(999)
    _, payload = _recv(cam_socket)
    assert payload[5] == 254


def test_sequence_increments_and_wraps(visca, cam_socket):
    visca.zoom_stop()
    visca.zoom_stop()
    seqs = [_recv(cam_socket)[0] for _ in range(2)]
    assert seqs[1] == seqs[0] + 1

    visca._seq = 0xFFFFFFFF
    visca.zoom_stop()
    assert _recv(cam_socket)[0] == 0


def test_returns_camera_reply_payload(cam_socket):
    import threading

    visca = ViscaIP("127.0.0.1", cam_socket.getsockname()[1], recv_timeout=2)

    def reply():
        frame, addr = cam_socket.recvfrom(64)
        cam_socket.sendto(b"\x01\x11\x00\x03" + frame[4:8] + b"\x90\x41\xff", addr)

    t = threading.Thread(target=reply)
    t.start()
    assert visca.zoom_stop() == b"\x90\x41\xff"
    t.join()


def test_no_reply_returns_none(visca):
    assert visca.zoom_stop() is None
