import mimetypes
import shutil

import pytest
from flask import Flask, render_template, url_for

from pew_ptz import assets, server


@pytest.fixture
def copy_app(tmp_path):
    """A Flask app serving snapshots of a temp copy of the package's templates
    and static files, so tests can change the copy on disk."""
    root = tmp_path / "pkg"
    shutil.copytree(server.app.root_path, root, ignore=shutil.ignore_patterns("__pycache__"))
    app = Flask("copy", static_folder=None)
    assets.install(app, assets.snapshot_templates(root / "templates"),
                   assets.snapshot_static(root / "static"), last_modified=1_700_000_000)

    @app.get("/page")
    def page():
        return render_template("help.html", title="T", camera_ip="x", shared=[], wards=[],
                               active_ward=None, home=None, presets_file_used=False,
                               contact=None, contact_tel="", version="v", build="b",
                               started="s", restart_needed=False)

    return app, root


def test_static_is_frozen_at_startup(copy_app):
    app, root = copy_app
    c = app.test_client()
    before = c.get("/static/app.js").get_data()
    (root / "static" / "app.js").write_text("// replaced on disk", encoding="utf-8")
    (root / "static" / "new.js").write_text("// added on disk", encoding="utf-8")
    assert c.get("/static/app.js").get_data() == before
    assert c.get("/static/new.js").status_code == 404


def test_templates_are_frozen_at_startup(copy_app):
    app, root = copy_app
    c = app.test_client()
    before = c.get("/page").get_data(as_text=True)
    (root / "templates" / "help.html").write_text("REPLACED", encoding="utf-8")
    assert c.get("/page").get_data(as_text=True) == before


def test_url_for_static_still_works(copy_app):
    app, _ = copy_app
    with app.test_request_context():
        assert url_for("static", filename="app.css") == "/static/app.css"


def test_etag_and_conditional_get(copy_app):
    app, _ = copy_app
    c = app.test_client()
    r = c.get("/static/app.css")
    assert r.headers["ETag"]
    assert r.headers["Cache-Control"] == "no-cache"
    assert c.get("/static/app.css", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304


def test_range_requests_for_wake_lock_video(copy_app):
    # iOS fetches video with Range requests and won't play it without 206s.
    app, root = copy_app
    data = (root / "static" / "silent.mp4").read_bytes()
    c = app.test_client()
    r = c.get("/static/silent.mp4", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 0-1/{len(data)}"
    assert r.get_data() == data[:2]
    r = c.get("/static/silent.mp4", headers={"Range": "bytes=100-"})
    assert r.status_code == 206 and r.get_data() == data[100:]
    # The same file again: each response gets its own stream.
    assert c.get("/static/silent.mp4").get_data() == data


@pytest.mark.parametrize("path,mimetype", [
    ("app.js", "text/javascript"),
    ("app.css", "text/css"),
    ("manifest.json", "application/json"),
    ("icon.svg", "image/svg+xml"),
    ("icon-192.png", "image/png"),
    ("silent.mp4", "video/mp4"),
    ("silent.webm", "video/webm"),
])
def test_fixed_mimetypes(copy_app, path, mimetype):
    app, _ = copy_app
    assert app.test_client().get(f"/static/{path}").mimetype == mimetype


def test_mimetypes_ignore_the_registry(tmp_path, monkeypatch):
    # On Windows, mimetypes reads the registry; a bad entry must not matter.
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("text/plain", None))
    (tmp_path / "a.css").write_text("body{}", encoding="utf-8")
    assert assets.snapshot_static(tmp_path)["a.css"].mimetype == "text/css"


def test_only_known_file_types_are_served(tmp_path):
    (tmp_path / "app.js~").write_text("backup", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.js").write_text("1", encoding="utf-8")
    assert set(assets.snapshot_static(tmp_path)) == {"sub/x.js"}


@pytest.mark.parametrize("path", [
    "/static/../server.py", "/static/..%2Fserver.py", "/static/..%5Cserver.py",
    "/static/%2e%2e/server.py", "/static/APP.JS", "/static/nope.js",
])
def test_traversal_and_unknown_paths_are_404(client, path):
    assert client.get(path).status_code == 404


def test_real_app_serves_from_snapshot(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert r.get_data() == (server.Path(server.app.root_path) / "static" / "app.js").read_bytes()
