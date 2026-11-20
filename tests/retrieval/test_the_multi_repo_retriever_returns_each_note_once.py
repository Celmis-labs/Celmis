"""The chat retriever collapses a note's chunks to one hit."""

from __future__ import annotations

from types import SimpleNamespace

from src.retrieval.note_chunks import collapse_chunks
from src.retrieval.vector_store import VectorScope
from tests.retrieval.conftest import WS
from tests.retrieval.test_the_tail_of_a_long_note_is_found_by_meaning import _write


def test_the_multi_repo_retriever_returns_each_note_once(chunk_world):
    from src.config import get_settings
    from src.qa.multi_repo_retriever import MultiRepoRetriever

    _write(chunk_world)
    _write(chunk_world, path="modules/small.md", body="small zebra note")
    r = MultiRepoRetriever.__new__(MultiRepoRetriever)
    r.settings = get_settings()
    r._qdrant = chunk_world.qdrant
    r.vault_unavailable = None
    hits = r._qdrant_query([1.0, 0.0, 0.0, 0.0], ["repo-a"], scope=VectorScope.for_workspace(WS))
    assert sorted(h.note_path for h in hits) == ["modules/big.md", "modules/small.md"]


def test_collapse_keeps_the_best_chunk_per_note_in_score_order():
    def pt(path, score, repo="r"):
        return SimpleNamespace(score=score, payload={"note_path": path, "repo": repo})

    out = collapse_chunks([pt("a", 0.5), pt("b", 0.9), pt("a", 0.7), pt("a", 0.6, repo="x")], limit=3)
    assert [(p.payload["note_path"], p.payload["repo"], p.score) for p in out] == [
        ("b", "r", 0.9), ("a", "r", 0.7), ("a", "x", 0.6)]
