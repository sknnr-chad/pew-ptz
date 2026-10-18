"""
pew_ptz.assets — serve templates and static files from memory.

Flask normally reads static files from disk on every request but keeps each
template cached after its first use. If the install folder is updated while
the server keeps running, a phone then gets a new app.js with an old page and
the script breaks. Loading both into memory at startup means a running server
always serves one consistent version, whatever happens on disk; the update
takes effect when the server restarts.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, abort, send_file
from jinja2 import FunctionLoader

# Fixed types rather than mimetypes.guess_type, which on Windows reads the
# registry: a machine where some program registered .css as text/plain would
# otherwise serve a stylesheet the browser refuses to apply.
MIME = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


@dataclass(frozen=True)
class StaticFile:
    data: bytes
    mimetype: str
    etag: str


def snapshot_static(root: Path) -> dict[str, StaticFile]:
    """Every servable file under root, keyed by its URL path (a/b.js)."""
    files = {}
    for path in sorted(root.rglob("*")):
        mimetype = MIME.get(path.suffix.lower())
        if mimetype is None or not path.is_file():
            continue
        data = path.read_bytes()
        files[path.relative_to(root).as_posix()] = StaticFile(
            data, mimetype, hashlib.sha256(data).hexdigest()[:16])
    return files


def snapshot_templates(root: Path) -> dict[str, tuple[str, str]]:
    """Template name -> (source, original path, for tracebacks)."""
    return {
        path.relative_to(root).as_posix(): (path.read_text(encoding="utf-8"), str(path))
        for path in sorted(root.rglob("*.html")) if path.is_file()
    }


def install(app: Flask, templates: dict[str, tuple[str, str]],
            static: dict[str, StaticFile], last_modified: float) -> None:
    """Point app's template loader and /static route at the snapshots. The app
    must be created with static_folder=None; the route keeps the endpoint name
    'static' so url_for('static', filename=...) still works."""

    def load(name: str):
        if name not in templates:
            return None
        source, filename = templates[name]
        return source, filename, lambda: True  # never reloaded

    app.jinja_loader = FunctionLoader(load)

    def serve_static(filename: str):
        f = static.get(filename)
        if f is None:
            abort(404)
        # A fresh BytesIO each time: send_file closes it after the response.
        # conditional=True gives ETag 304s and Range 206s (the silent wake-lock
        # video needs ranges on iOS).
        return send_file(io.BytesIO(f.data), mimetype=f.mimetype, etag=f.etag,
                         last_modified=last_modified, conditional=True,
                         download_name=filename.rsplit("/", 1)[-1])

    app.add_url_rule("/static/<path:filename>", endpoint="static", view_func=serve_static)
