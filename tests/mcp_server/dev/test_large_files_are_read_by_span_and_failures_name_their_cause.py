"""A line span of a big text file is read without loading the file, and a file
that cannot be read says why.

`read_symbol` used to take the whole file through `show_file`, which gave up on
anything over 2 MB (and on binary, missing and slow reads alike) and told the
caller the clone had lost the revision. Pinned here, for a git clone and for an
unpacked archive alike: a symbol near the end of a 3 MB and a 30 MB text file is
read; a binary file, a missing path, a file over the cap and a revision that is
really gone each get their own message, and only the last one says re-index; the
whole-file readers (`show_file`, the `find` preview) keep their 2 MB limit.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mcp_server.dev_profile import git_io, tools_find, tools_read
from tests.mcp_server.dev.conftest import SHOP

UPLOAD_SHA = "9a" * 32
TAIL = "def late_symbol(a):\n    return a + 1\n"


def _filler(n_bytes: int) -> str:
    line = "# filler line of synthetic text, padded to a fixed width ......... \n"
    return line * (n_bytes // len(line))


def _make_files(root: Path) -> dict[str, int]:
    """Writes the synthetic files; returns the 1-based line of `late_symbol` in each."""
    starts = {}
    for name, size in (("big3.txt", 3_000_000), ("big30.txt", 30_000_000)):
        head = _filler(size)
        starts[name] = head.count("\n") + 1
        (root / name).write_text(head + TAIL)
    (root / "small.txt").write_text("def small_symbol(a):\n    return a\n")
    (root / "bin.dat").write_bytes(b"\x00\x01binary\n" * 50)
    return starts


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(params=["clone", "unpacked"])
def backend(request, tmp_path, monkeypatch, as_caller, freshness):
    """(sha, starts): the files committed to a clone, or unpacked as an archive."""
    root = tmp_path / request.param
    root.mkdir()
    starts = _make_files(root)
    if request.param == "clone":
        _git(root, "init", "-q", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
        sha = _git(root, "rev-parse", "HEAD")
        monkeypatch.setattr(git_io, "clone_path", lambda slug: root)
        monkeypatch.setattr(git_io, "_unpacked_at", lambda slug, s: None)
        gone = "f" * 40
    else:
        sha = UPLOAD_SHA
        monkeypatch.setattr(git_io, "clone_path", lambda slug: None)
        monkeypatch.setattr(git_io, "_unpacked_at", lambda slug, s: root if s == sha else None)
        gone = "e1" * 32
    as_caller({SHOP: "code"})
    freshness[SHOP]["sha"] = sha
    return SimpleNamespace(sha=sha, gone=gone, starts=starts, fresh=freshness)


def _serve(monkeypatch, rows: list[dict]) -> None:
    store = SimpleNamespace(find_named=lambda name, limit=60: rows,
                            find_symbols=lambda *a, **k: [])

    @contextmanager
    def fake_open(slug):  # noqa: ANN001, ANN202
        yield store

    monkeypatch.setattr(tools_read.common, "open_store", fake_open)


def _row(file: str, start: int, end: int, name: str = "late_symbol") -> dict:
    return {"id": f"{file}::{name}", "name": name, "kind": "function", "file": file,
            "start_line": start, "end_line": end, "signature": "", "in_degree": 0}


@pytest.mark.parametrize("name", ["big3.txt", "big30.txt"])
def test_a_symbol_at_the_end_of_a_big_file_is_read(backend, monkeypatch, name):
    start = backend.starts[name]
    _serve(monkeypatch, [_row(name, start, start + 1)])
    out = tools_read.read_symbol(SHOP, "late_symbol", include="")
    assert f"{name}:{start}-{start + 1} function late_symbol" in out
    assert "sig: def late_symbol(a)" in out
    assert "    return a + 1" in out
    assert "not readable" not in out and "too big" not in out


def test_the_span_is_exactly_the_requested_lines(backend):
    start = backend.starts["big3.txt"]
    got = git_io.show_span(SHOP, backend.sha, "big3.txt", start, start + 1)
    assert got.problem == "" and got.lines == ["def late_symbol(a):", "    return a + 1"]
    assert git_io.show_span(SHOP, backend.sha, "big3.txt", 2, 3).lines == [
        _filler(200).split("\n")[0]] * 2


def test_a_symbol_in_a_small_file_still_reads(backend, monkeypatch):
    _serve(monkeypatch, [_row("small.txt", 1, 2, "small_symbol")])
    assert "return a" in tools_read.read_symbol(SHOP, "small_symbol", include="")


def test_a_binary_file_says_binary(backend, monkeypatch):
    _serve(monkeypatch, [_row("bin.dat", 1, 2)])
    out = tools_read.read_symbol(SHOP, "late_symbol", include="")
    assert "bin.dat is a binary file" in out and "re-index" not in out


def test_a_path_that_does_not_exist_says_so(backend, monkeypatch):
    _serve(monkeypatch, [_row("nope/missing.txt", 1, 2)])
    out = tools_read.read_symbol(SHOP, "late_symbol", include="")
    assert "nope/missing.txt is not in this repository" in out and "re-index" not in out


def test_a_file_over_the_cap_gives_its_size_and_the_limit_and_no_reindex(backend, monkeypatch):
    monkeypatch.setenv("CELMIS_MAX_READ_FILE_BYTES", "1000000")
    start = backend.starts["big3.txt"]
    _serve(monkeypatch, [_row("big3.txt", start, start + 1)])
    out = tools_read.read_symbol(SHOP, "late_symbol", include="")
    assert "big3.txt is too big to read: 2,999,993 bytes" in out and "the limit is 1,000,000" in out
    assert "re-index" not in out.replace("re-indexing does not help", "")


def test_a_really_missing_revision_is_the_only_case_that_says_re_index(backend, monkeypatch):
    backend.fresh[SHOP]["sha"] = backend.gone
    _serve(monkeypatch, [_row("big3.txt", 1, 2)])
    out = tools_read.read_symbol(SHOP, "late_symbol", include="")
    assert "not readable at" in out and "ask an admin to re-index" in out


def test_a_slow_clone_read_says_timeout(backend, monkeypatch):
    if backend.sha == UPLOAD_SHA:
        pytest.skip("an unpacked file has no process to time out")
    import time

    monkeypatch.setattr(git_io, "SPAN_READ_SECONDS", 0.05)
    real = git_io._take

    def slow(stream, start, end):  # noqa: ANN001, ANN202
        time.sleep(0.3)
        return real(stream, start, end)

    monkeypatch.setattr(git_io, "_take", slow)
    assert git_io.show_span(SHOP, backend.sha, "big3.txt", 1, 2).problem == "timeout"


def test_an_unsafe_path_is_refused(backend):
    for rel in ("../escape.txt", "/etc/passwd"):
        assert git_io.show_span(SHOP, backend.sha, rel, 1, 2).problem == "unsafe"


def test_whole_file_readers_keep_the_two_megabyte_limit(backend):
    assert git_io.show_file(SHOP, backend.sha, "big3.txt") is None
    assert git_io.show_file(SHOP, backend.sha, "small.txt")[0] == "def small_symbol(a):"


def test_the_find_preview_gives_a_big_file_no_preview_and_a_small_one_its_signature(backend):
    scope = SimpleNamespace(readable=lambda slug, f: True)
    cache: dict = {}

    def line(file, start, name):  # noqa: ANN001, ANN202
        hit = SimpleNamespace(slug=SHOP, row={"file": file, "start_line": start, "kind": "function",
                                              "name": name, "signature": ""})
        return tools_find.line_for(scope, hit, backend.sha, cache, detailed=False)

    assert line("big3.txt", backend.starts["big3.txt"], "late_symbol").endswith(" late_symbol")
    assert cache[(SHOP, "big3.txt")] is None
    assert "def small_symbol(a)" in line("small.txt", 1, "small_symbol")
