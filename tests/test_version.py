import shutil
from pathlib import Path

import pytest

from pew_ptz import __version__, version

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def make_repo(root: Path, head: str, loose: dict | None = None, packed: str | None = None):
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    for ref, sha in (loose or {}).items():
        path = git / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return git


# ---- git commit (display only) ----

def test_loose_branch_ref(tmp_path):
    make_repo(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    assert version.git_commit(tmp_path) == SHA_A


def test_branch_with_slashes(tmp_path):
    make_repo(tmp_path, "ref: refs/heads/feature/x/y", {"refs/heads/feature/x/y": SHA_B})
    assert version.git_commit(tmp_path) == SHA_B


def test_packed_ref(tmp_path):
    make_repo(tmp_path, "ref: refs/heads/main",
              packed=f"# pack-refs with: peeled fully-peeled sorted\n{SHA_A} refs/heads/main\n"
                     f"^{SHA_C}\n{SHA_B} refs/heads/other\n")
    assert version.git_commit(tmp_path) == SHA_A


def test_loose_ref_wins_over_stale_packed_entry(tmp_path):
    make_repo(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_B},
              packed=f"{SHA_A} refs/heads/main\n")
    assert version.git_commit(tmp_path) == SHA_B


def test_detached_head(tmp_path):
    make_repo(tmp_path, SHA_C)
    assert version.git_commit(tmp_path) == SHA_C


def test_worktree_reads_branch_refs_from_common_dir(tmp_path):
    main = tmp_path / "main"
    common = make_repo(main, "ref: refs/heads/main", {"refs/heads/main": SHA_A},
                       packed=f"{SHA_B} refs/heads/feature\n")
    wt_gitdir = common / "worktrees" / "wt"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")
    assert version.git_commit(wt) == SHA_B


@pytest.mark.parametrize("head", [
    "ref: refs/heads/.invalid",  # reftable repositories
    "ref: refs/heads/missing",
    "not a sha",
])
def test_unreadable_refs_give_none(tmp_path, head):
    make_repo(tmp_path, head)
    assert version.git_commit(tmp_path) is None


def test_no_repo_gives_none(tmp_path):
    assert version.git_commit(tmp_path) is None


def test_garbage_ref_contents_give_none(tmp_path):
    make_repo(tmp_path, "ref: refs/heads/main", {"refs/heads/main": "garbage"})
    assert version.git_commit(tmp_path) is None


def test_describe(tmp_path):
    make_repo(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    assert version.describe(tmp_path) == f"{__version__}+aaaaaaa"
    assert version.describe(tmp_path / "nowhere") == __version__


# ---- build fingerprint (drives restart_needed) ----

@pytest.fixture
def pkg(tmp_path):
    root = tmp_path / "pkg"
    shutil.copytree(version.PACKAGE_DIR, root,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_build_of_the_real_package_matches_disk_build():
    assert version.build_of(version.PACKAGE_DIR) == version.disk_build()
    assert len(version.disk_build()) == 16


def test_build_changes_with_content_and_new_files(pkg):
    before = version.build_of(pkg)
    js = pkg / "static" / "app.js"
    js.write_bytes(js.read_bytes() + b"\n// changed\n")
    changed = version.build_of(pkg)
    assert changed != before
    (pkg / "static" / "extra.css").write_text("body{}", encoding="utf-8")
    assert version.build_of(pkg) != changed


def test_build_ignores_caches_and_stray_files(pkg):
    before = version.build_of(pkg)
    (pkg / "__pycache__").mkdir(exist_ok=True)
    (pkg / "__pycache__" / "server.cpython-313.pyc").write_bytes(b"\0\1")
    (pkg / "static" / "app.js~").write_text("backup", encoding="utf-8")
    (pkg / "notes.txt").write_text("hi", encoding="utf-8")
    assert version.build_of(pkg) == before


def test_build_watcher_notices_changes_after_cache_expires(pkg):
    now = [0.0]
    watcher = version.BuildWatcher(version.build_of(pkg), pkg, cache_s=5, clock=lambda: now[0])
    assert watcher.restart_needed() is False
    (pkg / "server.py").write_text("# new code\n", encoding="utf-8")
    now[0] = 4.9
    assert watcher.restart_needed() is False  # still cached
    now[0] = 5.0
    assert watcher.restart_needed() is True


def test_version_module_does_not_import_flask():
    # install.ps1 imports it to compute the expected build without starting
    # anything.
    import subprocess
    import sys

    code = ("import sys, pew_ptz.version as v; v.disk_build(); "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('flask', 'werkzeug', 'pew_ptz')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=60).stdout
    assert "flask" not in out and "werkzeug" not in out
    assert "pew_ptz.server" not in out
