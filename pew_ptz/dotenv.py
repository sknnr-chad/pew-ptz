"""
pew_ptz.dotenv — load a .env file of site-specific settings.

The installer regenerates scripts/launch.cmd on every run, so settings put
there are lost on reinstall. A .env file in the working directory (the install
folder) survives reinstalls and is git-ignored, which keeps personal details
such as the help page's contact info out of the public repository.

Format: one KEY=value per line; blank lines and # comments are ignored; values
may be wrapped in single or double quotes. Variables already set in the real
environment win over the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("pew_ptz.dotenv")


def parse(text: str) -> dict[str, str]:
    values = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            log.warning(".env line %d ignored (expected KEY=value)", n)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: Path | str = ".env") -> dict[str, str]:
    """Apply path's settings to os.environ without overriding existing ones.
    Returns what was applied. A missing file is not an error."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate Notepad's BOM
    except FileNotFoundError:
        return {}
    applied = {}
    for key, value in parse(text).items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
