"""`grep` in an uploaded archive runs through a search index kept beside it.

`git grep --no-index` walks every path through the secret-file pathspecs before
the first line, which on a large archive outlasts the call. The index (a git dir
of intent-to-add entries under the repository's data dir, never inside the
unpacked tree) makes the archive a work tree for git. Pinned here: it is built
at install and lazily, rebuilt for a new archive, keeps the secret floor, the
archive's `.gitignore`s and the no-symlink rule, falls back when it cannot be
built, and `-i` works for Cyrillic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from src.mcp_server.dev_profile import git_io
from tests.security.mcp_world import set_env

SLUG = "bundle"
SHA = "c3" * 32
OTHER = "d4" * 32


def _git_supports_utf8_case() -> bool:
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        Path(d, "f.txt").write_text("ПРОЦЕДУРА\n", encoding="utf-8")
        proc = subprocess.run(
            ["git", "grep", "--no-index", "-i", "-F", "-e", "процедура", "--", "."],
            cwd=d, capture_output=True, check=False, env={**os.environ, **git_io._ENV})
        return proc.returncode == 0


@pytest.fixture
def tree(tmp_path, monkeypatch):
    set_env(monkeypatch, CELMIS_WORKSPACE_DIR=str(tmp_path / "ws"))
    from src.config import get_settings

    settings = get_settings()
    root = settings.repo_path(SLUG)
    for stale in (root, settings.repo_data_path(SLUG)):
        shutil.rmtree(stale, ignore_errors=True)
    files = {
        "src/a.txt": "needle in a\n",
        "Модуль/b.txt": "ПРОЦЕДУРА Тест\nслово\n",
        ".env": "needle=1\n",
        "sub/.gitignore": "skipped.txt\n",
        "sub/skipped.txt": "needle skipped\n",
        "sub/kept.txt": "needle kept\n",
        "bin.dat": "needle\0binary\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())
    outside = tmp_path / "outside.txt"
    outside.write_text("needle outside\n")
    (root / "link.txt").symlink_to(outside)
    data = settings.repo_data_path(SLUG)
    data.mkdir(parents=True, exist_ok=True)
    (data / "upload.json").write_text(json.dumps({"sha256": SHA, "size": 1}), encoding="utf-8")
    yield root
    set_env(monkeypatch)


def _paths(pattern="needle", **kw):
    return sorted(h.path for h in git_io.grep(SLUG, SHA, pattern, **kw))


def test_index_lives_outside_the_tree_and_is_built_at_install(tree):
    assert git_io.build_upload_index(SLUG) is True
    from src.config import get_settings

    gitdir = get_settings().repo_data_path(SLUG) / "grep-index.git"
    assert (gitdir / "grep-index.sha").read_text() == SHA
    assert not (tree / ".git").exists()


def test_grep_through_the_index_keeps_floor_gitignore_binary_and_symlinks(tree, monkeypatch):
    calls = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (calls.append(cmd), real(cmd, **kw))[1])
    assert _paths() == ["src/a.txt", "sub/kept.txt"]
    assert "--git-dir" in calls[-1] and "--no-index" not in calls[-1]


def test_a_sha_that_is_not_the_current_archive_reads_nothing(tree):
    assert git_io.grep(SLUG, OTHER, "needle") == []


def test_a_new_archive_rebuilds_the_index(tree):
    from src.config import get_settings

    assert _paths() == ["src/a.txt", "sub/kept.txt"]
    (tree / "src" / "new.txt").write_text("needle new\n")
    meta = get_settings().repo_data_path(SLUG) / "upload.json"
    meta.write_text(json.dumps({"sha256": OTHER, "size": 1}), encoding="utf-8")
    got = sorted(h.path for h in git_io.grep(SLUG, OTHER, "needle"))
    assert got == ["src/a.txt", "src/new.txt", "sub/kept.txt"]


def test_a_missing_index_is_built_on_first_use_under_concurrency(tree):
    out: list = []
    builds = []
    real = git_io._build_index
    git_io._build_index = lambda *a: (builds.append(1), real(*a))[1]
    try:
        ts = [threading.Thread(target=lambda: out.append(_paths())) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        git_io._build_index = real
    assert out == [["src/a.txt", "sub/kept.txt"]] * 4
    assert len(builds) == 1


def test_when_the_index_cannot_be_built_the_search_falls_back(tree, monkeypatch):
    monkeypatch.setattr(git_io, "_build_index", lambda *a: False)
    calls = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (calls.append(cmd), real(cmd, **kw))[1])
    assert _paths() == ["src/a.txt", "sub/kept.txt"]
    assert "--no-index" in calls[-1]


def test_the_remaining_call_time_bounds_git(tree, monkeypatch):
    import time

    seen = []
    real = threading.Timer
    monkeypatch.setattr(threading, "Timer", lambda t, f: (seen.append(t), real(t, f))[1])
    git_io.grep(SLUG, SHA, "needle", deadline=time.monotonic() + 9)
    git_io.grep(SLUG, SHA, "needle", deadline=time.monotonic() - 5)
    git_io.grep(SLUG, SHA, "needle", deadline=time.monotonic() + 500)
    assert 8 < seen[0] <= 9 and seen[1] == git_io.MIN_GREP_SECONDS
    assert seen[2] == git_io.MAX_GREP_SECONDS


@pytest.mark.skipif(not _git_supports_utf8_case(), reason="git here has no UTF-8 case folding")
def test_lowercase_cyrillic_matches_an_uppercase_occurrence(tree):
    assert _paths("процедура", ignore_case=True) == ["Модуль/b.txt"]
    assert _paths("слово", ignore_case=True, word=True) == ["Модуль/b.txt"]
