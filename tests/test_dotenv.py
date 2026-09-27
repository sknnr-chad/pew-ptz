import os

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
