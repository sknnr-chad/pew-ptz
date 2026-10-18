"""
pew_ptz.version — which code is running, and has the code on disk changed?

The build is a fingerprint of the package's own files (Python, templates and
static assets). The server records it at startup; if the files on disk later
fingerprint differently, someone updated the install without restarting, and
the phones show a "restart needed" pill. Unlike a git commit, the fingerprint
ignores commits that don't touch the app and catches edits that were never
committed, and it needs no git at all.

The git commit is shown for humans only. It's read straight from .git (no
subprocess) and anything unusual — reftable, a missing or locked file — just
means no commit is shown.

Standard library only: scripts/install.ps1 imports this to compute the build
it expects the restarted server to report, without starting the server.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from pew_ptz import __version__

PACKAGE_DIR = Path(__file__).resolve().parent

# Files that make up the running app. Editor backups and other stray files
# are ignored so they neither change the build nor get served.
BUILD_SUFFIXES = frozenset({
    ".py", ".html", ".js", ".css", ".json", ".webmanifest",
    ".svg", ".png", ".ico", ".mp4", ".webm",
})

_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

# Set by cli.main before the server module (or any other app code) is
# imported, so the build describes code that was on disk no later than it was
# loaded. A change after that point can only cause a spurious "restart
# needed", never a missed one.
STARTUP_BUILD: str | None = None


def package_files(root: Path = PACKAGE_DIR) -> list[Path]:
    """The app's files under root, sorted, skipping __pycache__."""
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in BUILD_SUFFIXES
        and "__pycache__" not in p.relative_to(root).parts
    )


def build_of(root: Path = PACKAGE_DIR) -> str:
    """16-hex fingerprint of the files under root (paths and contents)."""
    h = hashlib.sha256()
    for path in package_files(root):
        h.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:  # vanished or locked mid-update: still counts as a change
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def disk_build() -> str:
    return build_of(PACKAGE_DIR)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def git_commit(repo: Path = PACKAGE_DIR.parent) -> str | None:
    """The commit checked out in repo, or None if it can't be read simply."""
    try:
        dot_git = repo / ".git"
        if dot_git.is_file():  # worktree or submodule: "gitdir: <path>"
            text = _read(dot_git)
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = repo / gitdir
        elif dot_git.is_dir():
            gitdir = dot_git
        else:
            return None
        # A worktree's own gitdir holds HEAD; branch refs live in the common dir.
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = Path(_read(gitdir / "commondir"))
            if not common.is_absolute():
                common = gitdir / common
        head = _read(gitdir / "HEAD")
        if not head.startswith("ref:"):
            return head if _SHA.fullmatch(head) else None  # detached HEAD
        ref = head[len("ref:"):].strip()
        # Loose refs win over packed-refs (the packed entry may be stale).
        for base in (gitdir, common):
            loose = base / ref
            if loose.is_file():
                sha = _read(loose)
                return sha if _SHA.fullmatch(sha) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith(("#", "^")):
                    continue
                sha, _, name = line.partition(" ")
                if name.strip() == ref and _SHA.fullmatch(sha):
                    return sha
    except (OSError, ValueError):
        return None
    return None


def describe(repo: Path = PACKAGE_DIR.parent) -> str:
    """Human-readable version, e.g. 1.0.0+80c2409."""
    commit = git_commit(repo)
    return f"{__version__}+{commit[:7]}" if commit else __version__


class BuildWatcher:
    """Compares the build the server started with against the files on disk.
    The disk fingerprint takes a few ms, so it's cached briefly; every phone
    polls the state endpoint every 2 s."""

    def __init__(self, running: str, root: Path = PACKAGE_DIR, cache_s: float = 5.0,
                 clock=time.monotonic):
        self.running = running
        self._root = root
        self._cache_s = cache_s
        self._clock = clock
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._needed = False

    def restart_needed(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= self._cache_s:
                try:
                    self._needed = build_of(self._root) != self.running
                except OSError:
                    self._needed = False
                self._checked_at = now
            return self._needed
