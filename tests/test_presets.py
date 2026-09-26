import json

import pytest

from pew_ptz.presets import MAX_WARDS, Preset, PresetStore, parse_config, ward_slot

CONFIG = {
    "shared": ["Speaker", "Pulpit", "Wide"],
    "wards": [
        {"name": "1st Ward", "presets": ["Bishopric", "Choir"]},
        {"name": "2nd Ward", "presets": ["Bishopric", "Organ", "Youth"]},
        {"name": "3rd Ward", "presets": []},
    ],
}


def write(tmp_path, data):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_slot_layout():
    shared, wards = parse_config(CONFIG)
    assert [p.slot for p in shared] == [1, 2, 3]
    assert [p.slot for p in wards[0].presets] == [16, 17]
    assert [p.slot for p in wards[1].presets] == [32, 33, 34]
    assert ward_slot(MAX_WARDS - 1, 15) <= 254


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"shared": [f"p{i}" for i in range(16)]}, "max is 15"),
        ({"wards": [{"name": "A", "presets": [f"p{i}" for i in range(17)]}]}, "max is 16"),
        ({"wards": [{"name": f"W{i}"} for i in range(MAX_WARDS + 1)]}, "max is"),
        ({"wards": [{"name": "A"}, {"name": "a"}]}, "duplicate"),
        ({"wards": [{"presets": []}]}, "needs a name"),
        ({"shared": ["ok", " "]}, "empty name"),
        ({"shared": "Speaker"}, "list of names"),
        (["not", "an", "object"], "JSON object"),
    ],
)
def test_invalid_configs_are_rejected(data, message):
    with pytest.raises(ValueError, match=message):
        parse_config(data)


def test_missing_file_uses_fallback_names(tmp_path):
    store = PresetStore.load(tmp_path / "presets.json", ["Speaker", "Choir"])
    assert store.shared == (Preset("Speaker", 1), Preset("Choir", 2))
    assert store.wards == ()


def test_broken_file_falls_back_instead_of_failing(tmp_path):
    (tmp_path / "presets.json").write_text("{ not json", encoding="utf-8")
    store = PresetStore.load(tmp_path / "presets.json", ["Speaker"])
    assert [p.name for p in store.shared] == ["Speaker"]


def test_active_ward_persists_across_restarts(tmp_path):
    path = write(tmp_path, CONFIG)
    PresetStore.load(path, []).set_active("2nd Ward")
    assert PresetStore.load(path, []).active_ward == "2nd Ward"


def test_active_ward_dropped_if_removed_from_config(tmp_path):
    path = write(tmp_path, CONFIG)
    PresetStore.load(path, []).set_active("3rd Ward")
    write(tmp_path, {"shared": ["Speaker"], "wards": [{"name": "1st Ward"}]})
    assert PresetStore.load(path, []).active_ward is None


def test_unknown_ward_rejected(tmp_path):
    store = PresetStore.load(write(tmp_path, CONFIG), [])
    with pytest.raises(ValueError):
        store.set_active("9th Ward")


def test_usable_slots_follow_active_ward(tmp_path):
    store = PresetStore.load(write(tmp_path, CONFIG), [])
    assert set(store.usable()) == {1, 2, 3}
    store.set_active("1st Ward")
    assert set(store.usable()) == {1, 2, 3, 16, 17}
    store.set_active(None)
    assert set(store.usable()) == {1, 2, 3}


def test_home_slot(tmp_path):
    store = PresetStore.load(write(tmp_path, CONFIG), [])
    assert store.home_slot() == 1
    store.set_active("2nd Ward")
    assert store.home_slot() == 32
    store.set_active("3rd Ward")  # no presets of its own
    assert store.home_slot() == 1
