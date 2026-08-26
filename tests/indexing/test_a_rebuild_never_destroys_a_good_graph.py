"""A graph rebuild that finds nothing keeps the graph it already had.

TWO DEFECTS, ONE OBSERVED LOSS. A `celmis-demo-worker` repository holding Go
and Rust had every Go symbol disappear from its graph after a vault build.
`Reconcile` and `Total` at `internal/settle/event.go` were searchable at 15:14
and gone from 15:25 onward, across eight identical probes; the Rust symbols in
the same repository survived. `/api/repos` went on reporting `indexed: true`,
`last_index_error: null`.

FIRST — the walk root. `index_repo_graph(src_subdir="src")` was the default, a
layout assumption borrowed from one customer's repository. The Rust lives in
`src/`; the Go lives in `cmd/` and `internal/`. Vault generation calls this
without the argument, so the rebuild walked `src/` alone and produced a
Rust-only graph. Both other callers already passed None explicitly — one with
a comment saying it would not assume that layout — so only the caller relying
on the default was wrong, which is what a bad default looks like.

SECOND — the rebuild was destructive before it was correct. `db_path.unlink()`
ran first, so whatever the rebuild then produced replaced a working graph with
no way back, and vault generation downgrades any exception here to a warning
and carries on. Silent twice over.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.indexing.graph.graph_store import SymbolInfo
from src.indexing.graph.pipeline import (
    EmptyGraphRebuild,
    _graph_has_symbols,
    index_repo_graph,
    make_graph_store,
)


def graph_with_symbols(path: Path) -> Path:
    store = make_graph_store(path)
    try:
        store.add_symbols_batch([SymbolInfo(
            id="s1", name="Reconcile", kind="function",
            file="internal/settle/event.go", start_line=19, end_line=21,
            language="go",
        )])
        store.commit()
    finally:
        store.close()
    return path


# ─── the default ─────────────────────────────────────────────────────


def test_the_walk_defaults_to_the_whole_repository():
    """The one-line cause of a lost language."""
    assert inspect.signature(index_repo_graph).parameters["src_subdir"].default is None


def test_the_docstring_says_what_none_means():
    doc = index_repo_graph.__doc__ or ""
    assert "whole repository" in doc


# ─── the emptiness guard ─────────────────────────────────────────────


def test_an_empty_graph_reads_as_empty(tmp_path):
    p = tmp_path / "empty.fdblite"
    store = make_graph_store(p)
    store.commit()
    store.close()

    assert _graph_has_symbols(p) is False


def test_a_populated_graph_reads_as_populated(tmp_path):
    assert _graph_has_symbols(graph_with_symbols(tmp_path / "full.fdblite")) is True


def test_a_missing_graph_reads_as_empty(tmp_path):
    """Nothing to protect, so a first build must not be blocked."""
    assert _graph_has_symbols(tmp_path / "nope.fdblite") is False


def test_an_unreadable_graph_reads_as_empty(tmp_path):
    p = tmp_path / "junk.fdblite"
    p.write_bytes(b"not a database")

    assert _graph_has_symbols(p) is False


# ─── the swap ────────────────────────────────────────────────────────


def test_a_rebuild_finding_nothing_keeps_the_existing_graph(tmp_path, monkeypatch):
    """The whole point. An empty repository is indistinguishable from a wrong
    walk root, a grammar that failed to load and a shallow clone — and only one
    of those four means the graph should become empty."""
    from src.config import get_settings

    graph = tmp_path / "graphs" / "acme-api.fdblite"
    graph.parent.mkdir(parents=True)
    graph_with_symbols(graph)

    settings = get_settings()
    monkeypatch.setattr(type(settings), "repo_graph_path",
                        lambda self, slug: graph, raising=False)
    empty_repo = tmp_path / "repo"
    empty_repo.mkdir()

    with pytest.raises(EmptyGraphRebuild):
        index_repo_graph(empty_repo, "acme-api", settings, src_subdir=None)

    assert _graph_has_symbols(graph) is True, "the good graph was destroyed"


def test_a_first_build_of_an_empty_repo_is_not_refused(tmp_path, monkeypatch):
    """There is nothing to protect, so refusing would block a legitimate
    first index of a repository with no parseable code."""
    from src.config import get_settings

    graph = tmp_path / "graphs" / "acme-new.fdblite"
    graph.parent.mkdir(parents=True)
    settings = get_settings()
    monkeypatch.setattr(type(settings), "repo_graph_path",
                        lambda self, slug: graph, raising=False)
    empty_repo = tmp_path / "repo"
    empty_repo.mkdir()

    result = index_repo_graph(empty_repo, "acme-new", settings, src_subdir=None)

    assert result.symbols == 0


def test_no_build_artefacts_are_left_behind(tmp_path, monkeypatch):
    from src.config import get_settings

    graph = tmp_path / "graphs" / "acme-new.fdblite"
    graph.parent.mkdir(parents=True)
    settings = get_settings()
    monkeypatch.setattr(type(settings), "repo_graph_path",
                        lambda self, slug: graph, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")

    index_repo_graph(repo, "acme-new", settings, src_subdir=None)

    leftovers = [p.name for p in graph.parent.iterdir()
                 if p.name.endswith((".building", ".previous"))]
    assert leftovers == []
