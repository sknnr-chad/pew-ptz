import os

import pytest

from pew_ptz import dotenv


def test_parse():
    text = """
# comment
PEW_PTZ_CONTACT_NAME="Pat Example"
PEW_PTZ_CONTACT_EMAIL = pat@example.com
export PEW_PTZ_TITLE='Chapel PTZ'
not a setting
PEW_PTZ_CONTACT_PHONE=(555) 555-0100
"""
    assert dotenv.parse(text) == {
        "PEW_PTZ_CONTACT_NAME": "Pat Example",
        "PEW_PTZ_CONTACT_EMAIL": "pat@example.com",
        "PEW_PTZ_TITLE": "Chapel PTZ",
        "PEW_PTZ_CONTACT_PHONE": "(555) 555-0100",
    }


def test_load_applies_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("﻿PEW_TEST_A=from-file\nPEW_TEST_B=from-file\n", encoding="utf-8")  # BOM
    monkeypatch.delenv("PEW_TEST_A", raising=False)
    monkeypatch.setenv("PEW_TEST_B", "from-environment")
    applied = dotenv.load(env)
    assert applied == {"PEW_TEST_A": "from-file"}
    assert os.environ["PEW_TEST_A"] == "from-file"
    assert os.environ["PEW_TEST_B"] == "from-environment"
    monkeypatch.delenv("PEW_TEST_A")


def test_missing_file_is_fine(tmp_path):
    assert dotenv.load(tmp_path / "nope.env") == {}


def test_utf16_file_from_windows_powershell(tmp_path):
    # Windows PowerShell 5.1's `>` and Out-File write UTF-16 with a BOM.
    env = tmp_path / ".env"
    env.write_bytes("PEW_PTZ_CONTACT_NAME=Zoë\r\n".encode("utf-16"))
    assert dotenv.read(env) == {"PEW_PTZ_CONTACT_NAME": "Zoë"}


@pytest.mark.parametrize("data", [
    "PEW_PTZ_CONTACT_NAME=Zoë\n".encode("cp1252"),           # Set-Content in 5.1
    b"A=1\n" + "B=2\n".encode("utf-16-le"),                  # `>>` onto a UTF-8 file
])
def test_unreadable_file_is_a_clear_error(tmp_path, data):
    env = tmp_path / ".env"
    env.write_bytes(data)
    with pytest.raises(dotenv.EnvFileError, match="save it as UTF-8"):
        dotenv.load(env)
