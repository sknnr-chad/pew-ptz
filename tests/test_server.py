import pytest

from pew_ptz import server
from pew_ptz.visca import PAN_LEFT, PAN_RIGHT, PAN_STOP, TILT_DOWN, TILT_STOP, TILT_UP


def test_index_renders_presets(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for name in server.PRESETS:
        assert name in body


def test_index_uses_configured_title(client, monkeypatch):
    monkeypatch.setattr(server, "PAGE_TITLE", "Chapel PTZ")
    assert "<title>Chapel PTZ</title>" in client.get("/").get_data(as_text=True)


def test_static_assets_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_healthz_is_minimal(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert set(r.get_json()) == {"ok", "uptime_s", "keyboard_ok"}


def test_state_never_includes_window_title(client, monkeypatch):
    monkeypatch.setattr(server, "foreground_info", lambda: (False, "Secret.docx", "winword.exe"))
    j = client.get("/zoom_meeting/state").get_json()
    assert "Secret.docx" not in str(j)
    assert j["foreground_process"] == "winword.exe"


@pytest.mark.parametrize(
    ("direction", "pan", "tilt"),
    [
        ("up", PAN_STOP, TILT_UP),
        ("down", PAN_STOP, TILT_DOWN),
        ("left", PAN_LEFT, TILT_STOP),
        ("right", PAN_RIGHT, TILT_STOP),
        ("up_left", PAN_LEFT, TILT_UP),
        ("down_right", PAN_RIGHT, TILT_DOWN),
    ],
)
def test_ptz_move_directions(client, camera, direction, pan, tilt):
    r = client.post(f"/ptz/move/{direction}?speed=6")
    assert r.status_code == 200
    assert camera.calls == [("pan_tilt", (pan, tilt), {"pan_speed": 6, "tilt_speed": 6})]


def test_ptz_move_unknown_direction(client, camera):
    assert client.post("/ptz/move/sideways").status_code == 400
    assert camera.calls == []


def test_ptz_move_bad_speed_falls_back_to_default(client, camera):
    assert client.post("/ptz/move/up?speed=fast").status_code == 200
    assert camera.calls[0][2] == {"pan_speed": 12, "tilt_speed": 12}


def test_ptz_stop(client, camera):
    assert client.post("/ptz/stop").status_code == 200
    assert camera.calls == [("pan_tilt_stop", (), {})]


@pytest.mark.parametrize(
    ("action", "call"),
    [("tele", ("zoom_tele", (2,), {})), ("wide", ("zoom_wide", (2,), {})),
     ("stop", ("zoom_stop", (), {}))],
)
def test_zoom_actions(client, camera, action, call):
    assert client.post(f"/zoom/{action}").status_code == 200
    assert camera.calls == [call]


def test_zoom_unknown_action(client, camera):
    assert client.post("/zoom/sideways").status_code == 400
    assert camera.calls == []


def test_preset_recall(client, camera):
    assert client.post("/preset/recall/3").status_code == 200
    assert camera.calls == [("preset_recall", (3,), {})]


def test_toggle_video_flips_state_and_sends_alt_v(client, chords, zoom_reader):
    j = client.post("/zoom_meeting/toggle_video").get_json()
    assert chords == ["v"]
    assert j["video_on"] is False and j["mic_on"] is True
    assert j["air_on"] is False
    assert zoom_reader.refreshes == 1


def test_toggle_mic_flips_state_and_sends_alt_a(client, chords):
    j = client.post("/zoom_meeting/toggle_mic").get_json()
    assert chords == ["a"]
    assert j["mic_on"] is False and j["video_on"] is True


def test_toggle_air_flips_both(client, chords):
    j = client.post("/zoom_meeting/toggle_air").get_json()
    assert chords == ["v", "a"]
    assert j["video_on"] is False and j["mic_on"] is False


def test_toggle_syncs_from_observed_state_first(client, chords, zoom_reader):
    # Optimistic state says video on, but UIA sees it off: the toggle must
    # turn it ON, not compound the drift.
    zoom_reader.state.update(observed=True, video_on=False, mic_on=True)
    client.post("/zoom_meeting/toggle_video")
    zoom_reader.state.update(observed=False)
    assert client.get("/zoom_meeting/state").get_json()["video_on"] is True


def test_state_prefers_observed_values(client, zoom_reader):
    zoom_reader.state.update(observed=True, video_on=False, mic_on=None)
    j = client.get("/zoom_meeting/state").get_json()
    assert j["video_on"] is False  # observed
    assert j["mic_on"] is True  # unobserved -> optimistic fallback
    assert j["observed"] is True


def test_toggle_without_keyboard_is_500(client, monkeypatch):
    monkeypatch.setattr(server, "_alt_chord", lambda _letter: False)
    assert client.post("/zoom_meeting/toggle_video").status_code == 500
    assert client.post("/zoom_meeting/toggle_mic").status_code == 500
    monkeypatch.setattr(server, "_kbd", None)
    assert client.post("/zoom_meeting/toggle_air").status_code == 500


def test_toggle_air_reports_half_done_when_mic_fails(client, monkeypatch):
    sent = []

    def chord(letter):
        sent.append(letter)
        return letter == "v"

    monkeypatch.setattr(server, "_alt_chord", chord)
    r = client.post("/zoom_meeting/toggle_air")
    assert r.status_code == 500
    j = r.get_json()
    assert sent == ["v", "a"]
    assert j["video_on"] is False  # video did flip
    assert j["mic_on"] is True  # mic did not


def test_toggle_air_video_failure_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(server, "_alt_chord", lambda _letter: False)
    assert client.post("/zoom_meeting/toggle_air").status_code == 500
    j = client.get("/zoom_meeting/state").get_json()
    assert j["video_on"] is True and j["mic_on"] is True


def test_toggle_blocked_when_zoom_not_focused(client, chords, monkeypatch):
    monkeypatch.setattr(server, "SKIP_FOCUS_CHECK", False)
    monkeypatch.setattr(server, "_user32", object())
    monkeypatch.setattr(server, "foreground_info", lambda: (False, "Notepad", "notepad.exe"))
    r = client.post("/zoom_meeting/toggle_air")
    assert r.status_code == 409
    assert r.get_json()["foreground_process"] == "notepad.exe"
    assert chords == []


def test_toggle_allowed_when_zoom_focused(client, chords, monkeypatch):
    monkeypatch.setattr(server, "SKIP_FOCUS_CHECK", False)
    monkeypatch.setattr(server, "_user32", object())
    monkeypatch.setattr(server, "foreground_info", lambda: (True, "Zoom Meeting", "Zoom.exe"))
    assert client.post("/zoom_meeting/toggle_mic").status_code == 200
    assert chords == ["a"]


# ---- per-ward presets --------------------------------------------------------


def test_presets_without_config_are_shared_only(client):
    j = client.get("/presets").get_json()
    assert [p["name"] for p in j["shared"]] == server.PRESETS
    assert j["wards"] == [] and j["active_ward"] is None


def test_set_ward_is_server_wide(client, wards):
    r = client.post("/ward", json={"ward": "1st Ward"})
    assert r.status_code == 200
    assert r.get_json()["active_ward"] == "1st Ward"
    # Another phone sees it on the presets list and on the status poll.
    other = server.app.test_client()
    assert other.get("/presets").get_json()["active_ward"] == "1st Ward"
    assert other.get("/zoom_meeting/state").get_json()["active_ward"] == "1st Ward"


def test_set_unknown_ward_is_400(client, wards):
    assert client.post("/ward", json={"ward": "9th Ward"}).status_code == 400


def test_clear_ward(client, wards):
    client.post("/ward", json={"ward": "1st Ward"})
    assert client.post("/ward", json={"ward": None}).get_json()["active_ward"] is None


def test_recall_limited_to_presets_on_screen(client, wards, camera):
    assert client.post("/preset/recall/1").status_code == 200       # No ward set
    assert client.post("/preset/recall/16").status_code == 400      # 1st Ward, not active
    client.post("/ward", json={"ward": "1st Ward"})
    assert client.post("/preset/recall/16").status_code == 200
    assert client.post("/preset/recall/2").status_code == 400       # No-ward set hidden now
    assert client.post("/preset/recall/32").status_code == 400      # 2nd Ward
    assert [c for c in camera.calls if c[0] == "preset_recall"] == [
        ("preset_recall", (1,), {}), ("preset_recall", (16,), {})]


def test_save_preset(client, wards, camera):
    client.post("/ward", json={"ward": "2nd Ward"})
    r = client.post("/preset/save/32")
    assert r.status_code == 200
    assert r.get_json()["status"] == "saved Organ"
    assert camera.calls[-1] == ("preset_set", (32,), {})


def test_save_rejects_other_wards_and_unused_slots(client, wards, camera):
    client.post("/ward", json={"ward": "2nd Ward"})
    assert client.post("/preset/save/16").status_code == 400   # 1st Ward's slot
    assert client.post("/preset/save/1").status_code == 400    # No-ward set, hidden
    assert not [c for c in camera.calls if c[0] == "preset_set"]


def test_index_embeds_ward_presets(client, wards):
    client.post("/ward", json={"ward": "1st Ward"})
    body = client.get("/").get_data(as_text=True)
    assert "Bishopric" in body and "1st Ward" in body


# ---- help page ----------------------------------------------------------------


def test_help_without_wards(client):
    r = client.get("/help")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Moving the camera" in body
    assert server.PRESETS[0] in body
    assert "Pick your ward" not in body  # no ward instructions when there are none


def test_help_lists_wards_and_active_ward(client, wards):
    client.post("/ward", json={"ward": "1st Ward"})
    body = client.get("/help").get_data(as_text=True)
    assert "Pick your ward" in body
    assert "1st Ward" in body and "Bishopric, Choir" in body
    assert "no presets yet" in body  # 3rd Ward is empty
    assert "Selected right now: <b>1st Ward</b>" in body
    assert "<b>Bishopric</b>" in body  # HOME target


def test_index_links_to_help(client):
    assert 'href="/help"' in client.get("/").get_data(as_text=True)


# ---- contact box on the help page --------------------------------------------


def test_help_has_no_contact_box_by_default(client, monkeypatch):
    monkeypatch.setattr(server, "CONTACT", {"name": "", "email": "", "phone": ""})
    assert "Need help?" not in client.get("/help").get_data(as_text=True)


def test_help_shows_contact(client, monkeypatch):
    monkeypatch.setattr(server, "CONTACT", {
        "name": "Pat Example", "email": "pat@example.com", "phone": "+1 (555) 555-0100"})
    body = client.get("/help").get_data(as_text=True)
    assert "Need help?" in body and "Pat Example" in body
    assert 'href="mailto:pat@example.com"' in body
    assert 'href="tel:+15555550100"' in body


def test_help_contact_is_escaped(client, monkeypatch):
    monkeypatch.setattr(server, "CONTACT", {"name": "<script>x</script>", "email": "", "phone": ""})
    body = client.get("/help").get_data(as_text=True)
    assert "<script>x</script>" not in body
