"""
pew_ptz.presets — shared and per-ward camera presets.

The camera stores the positions; this module only maps names to slot numbers
and remembers which ward is active. Slots are laid out in fixed blocks so a
ward's presets never collide with another's:

    shared      slots 1-15
    ward 1      slots 16-31
    ward 2      slots 32-47
    ward N      slots 16*N .. 16*N+15     (up to 14 wards, slot 254 max)

A preset's slot comes from its position in its list, so presets.json only
needs names:

    {
      "shared": ["Speaker", "Pulpit", "Wide"],
      "wards": [
        {"name": "1st Ward", "presets": ["Bishopric", "Choir"]},
        {"name": "2nd Ward", "presets": ["Bishopric", "Organ"]}
      ]
    }

Reordering or inserting names therefore changes which slot a name points at;
append new names to the end of a list instead.

With no presets.json, the PEW_PTZ_PRESETS names become the shared presets and
there are no wards — exactly the pre-ward behaviour.

Selecting a ward shows only that ward's presets; "No ward" shows only the
shared ones. The active ward is server-wide and persisted in active-ward.json
next to presets.json so it survives restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pew_ptz.presets")

SHARED_MAX = 15
NAME_MAX = 24  # longest preset name that still fits a button on a phone
WARD_BLOCK = 16
MAX_WARDS = (254 - WARD_BLOCK + 1) // WARD_BLOCK  # 14: last block is 224-239


@dataclass(frozen=True)
class Preset:
    name: str
    slot: int


@dataclass(frozen=True)
class Ward:
    name: str
    presets: tuple[Preset, ...]


def ward_slot(ward_index: int, preset_index: int) -> int:
    """Slot for the preset_index-th (0-based) preset of the ward_index-th
    (0-based) ward."""
    return WARD_BLOCK * (ward_index + 1) + preset_index


def _names(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ValueError(f"{where} must be a list of names")
    names = [n.strip() for n in value]
    if any(not n for n in names):
        raise ValueError(f"{where} contains an empty name")
    return names


def parse_config(data: dict) -> tuple[tuple[Preset, ...], tuple[Ward, ...]]:
    if not isinstance(data, dict):
        raise ValueError("presets.json must contain a JSON object")
    shared_names = _names(data.get("shared", []), "shared")
    if len(shared_names) > SHARED_MAX:
        raise ValueError(f"shared has {len(shared_names)} presets; max is {SHARED_MAX}")
    shared = tuple(Preset(n, i + 1) for i, n in enumerate(shared_names))

    raw_wards = data.get("wards", [])
    if not isinstance(raw_wards, list):
        raise ValueError("wards must be a list")
    if len(raw_wards) > MAX_WARDS:
        raise ValueError(f"{len(raw_wards)} wards; max is {MAX_WARDS}")
    wards = []
    seen = set()
    for wi, w in enumerate(raw_wards):
        if not isinstance(w, dict) or not isinstance(w.get("name"), str) or not w["name"].strip():
            raise ValueError(f"ward #{wi + 1} needs a name")
        name = w["name"].strip()
        if name.lower() in seen:
            raise ValueError(f"duplicate ward name: {name}")
        seen.add(name.lower())
        names = _names(w.get("presets", []), f"ward {name!r} presets")
        if len(names) > WARD_BLOCK:
            raise ValueError(f"ward {name!r} has {len(names)} presets; max is {WARD_BLOCK}")
        wards.append(Ward(name, tuple(Preset(n, ward_slot(wi, i)) for i, n in enumerate(names))))
    return shared, tuple(wards)


class PresetStore:
    def __init__(self, shared, wards, state_path: Path | None = None,
                 config_path: Path | None = None):
        self.shared: tuple[Preset, ...] = tuple(shared)
        self.wards: tuple[Ward, ...] = tuple(wards)
        self.state_path = state_path
        # Where renames are written. None when presets.json exists but is
        # broken: overwriting it would destroy whatever the admin was editing.
        self.config_path = config_path
        self.version = 0  # bumped on every rename so phones know to refresh
        self._lock = threading.Lock()
        self._active: str | None = None
        self._load_active()

    @classmethod
    def load(cls, path: Path, fallback_names: list[str]) -> PresetStore:
        """Load presets.json at path. A missing file means shared-only presets
        from fallback_names. A broken file is logged and also falls back, so a
        typo never leaves the operator without a camera."""
        path = Path(path)
        state_path = path.with_name("active-ward.json")
        fallback = [Preset(n, i + 1) for i, n in enumerate(fallback_names[:SHARED_MAX])]
        if not path.exists():
            return cls(fallback, (), state_path, config_path=path)
        try:
            shared, wards = parse_config(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log.error("ignoring %s (%s); using PEW_PTZ_PRESETS instead", path, e)
            return cls(fallback, (), state_path, config_path=None)
        log.info("loaded %d shared presets and %d wards from %s", len(shared), len(wards), path)
        return cls(shared, wards, state_path, config_path=path)

    # ---- active ward ------------------------------------------------------

    def _ward(self, name: str | None) -> Ward | None:
        if name is None:
            return None
        return next((w for w in self.wards if w.name == name), None)

    def _load_active(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            name = json.loads(self.state_path.read_text(encoding="utf-8")).get("active_ward")
        except (OSError, ValueError, AttributeError):
            return
        if self._ward(name) is not None:
            self._active = name

    @property
    def active_ward(self) -> str | None:
        with self._lock:
            return self._active

    def set_active(self, name: str | None) -> None:
        if name is not None and self._ward(name) is None:
            raise ValueError(f"unknown ward: {name}")
        with self._lock:
            self._active = name
            if self.state_path:
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"active_ward": name}), encoding="utf-8")
                os.replace(tmp, self.state_path)

    # ---- lookups ------------------------------------------------------------

    def usable(self) -> dict[int, Preset]:
        """Slots the operator may recall or save right now: the active ward's
        presets, or the shared ("No ward") presets when no ward is selected.
        The two are never mixed on screen."""
        ward = self._ward(self.active_ward)
        source = ward.presets if ward else self.shared
        return {p.slot: p for p in source}

    def home_slot(self) -> int:
        ward = self._ward(self.active_ward)
        if ward and ward.presets:
            return ward.presets[0].slot
        return self.shared[0].slot if self.shared else 1

    # ---- renaming -------------------------------------------------------------

    def rename(self, slot: int, new_name: str) -> Preset:
        """Rename one of the presets on screen (the active ward's, or No
        ward's). The slot, and so the saved camera position, is unchanged.
        Writes presets.json; the previous version is kept as presets.json.bak."""
        name = " ".join(str(new_name).split())  # trim, collapse whitespace
        if not name:
            raise ValueError("name can't be blank")
        if len(name) > NAME_MAX:
            raise ValueError(f"name is too long (max {NAME_MAX} characters)")
        if self.config_path is None:
            raise ValueError("presets.json has an error; fix it on the host PC before renaming")
        with self._lock:
            ward = self._ward(self._active)
            source = ward.presets if ward else self.shared
            old = next((p for p in source if p.slot == slot), None)
            if old is None:
                raise ValueError(f"slot {slot} isn't one of the selected ward's presets")
            if any(p.name.lower() == name.lower() and p.slot != slot for p in source):
                raise ValueError(f'"{name}" is already used in this set')
            renamed = tuple(Preset(name, p.slot) if p.slot == slot else p for p in source)
            if ward:
                wards = tuple(Ward(w.name, renamed) if w is ward else w for w in self.wards)
                shared = self.shared
            else:
                wards, shared = self.wards, renamed
            self._write_config(shared, wards)
            self.shared, self.wards = shared, wards
            self.version += 1
            return old

    def _write_config(self, shared, wards) -> None:
        data = {
            "shared": [p.name for p in shared],
            "wards": [{"name": w.name, "presets": [p.name for p in w.presets]} for w in wards],
        }
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.with_suffix(".json.bak").write_bytes(path.read_bytes())
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def payload(self) -> dict:
        def plist(ps):
            return [{"name": p.name, "slot": p.slot} for p in ps]

        return {
            "shared": plist(self.shared),
            "wards": [{"name": w.name, "presets": plist(w.presets)} for w in self.wards],
            "active_ward": self.active_ward,
            "home_slot": self.home_slot(),
            "version": self.version,
        }
