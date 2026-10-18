"""
pew_ptz.dotenv — load a .env file of site-specific settings.

The installer re-registers the Scheduled Task on every run, so settings put
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


class EnvFileError(ValueError):
    """The file exists but can't be read as settings."""


def read(path: Path | str = ".env") -> dict[str, str]:
    """path's settings without applying them. A missing file gives {}."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise EnvFileError(f"can't read {path}: {e}") from e
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            # Windows PowerShell 5.1 writes UTF-16 with `>` and Out-File.
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")  # tolerate Notepad's BOM
    except UnicodeDecodeError as e:
        raise EnvFileError(f"{path} is not UTF-8 text; save it as UTF-8") from e
    if "\0" in text:
        raise EnvFileError(f"{path} contains NUL characters (mixed encodings?); "
                           "save it as UTF-8")
    return parse(text)


def load(path: Path | str = ".env") -> dict[str, str]:
    """Apply path's settings to os.environ without overriding existing ones.
    Returns what was applied. A missing file is not an error; an unreadable
    one raises EnvFileError."""
    applied = {}
    for key, value in read(path).items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
