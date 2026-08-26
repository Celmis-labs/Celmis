"""Tests for src/indexing/graph/graph_store.py — Phase 4 (Graph Storage Layer).

Golden tests на простому graph fixture:
    seed → A → B → C → D
                ↓
                E (other branch)

Перевіряє: insertion + 2-hop BFS + edge kind filter + persistence + sanitization.
"""

from __future__ import annotations

import pytest

from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.graph_store import (
    ALLOWED_EDGE_KINDS,
    FalkorDBLiteStore,
    GraphExpansion,
    _validate_edge_kinds,
    make_graph_store,
)

# ─── fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    """Свіжий FalkorDBLite store у tmp dir."""
    s = make_graph_store(tmp_path / "test.fdblite")
    yield s
    s.close()


@pytest.fixture
def small_graph(store: FalkorDBLiteStore) -> FalkorDBLiteStore:
    """Fixture з 6 symbols + 5 CALLS edges:

        seed → A → B → C → D
                   ↓
                   E
    """
    syms = [
        SymbolInfo(id="seed", name="seed", kind="function", file="a.ts", start_line=1, end_line=5, language="ts"),
        SymbolInfo(id="A",    name="A",    kind="function", file="a.ts", start_line=10, end_line=15, language="ts"),
        SymbolInfo(id="B",    name="B",    kind="function", file="b.ts", start_line=1, end_line=10, language="ts"),
        SymbolInfo(id="C",    name="C",    kind="function", file="c.ts", start_line=1, end_line=10, language="ts"),
        SymbolInfo(id="D",    name="D",    kind="function", file="d.ts", start_line=1, end_line=10, language="ts"),
        SymbolInfo(id="E",    name="E",    kind="function", file="e.ts", start_line=1, end_line=10, language="ts"),
    ]
    edges = [
        EdgeInfo(from_id="seed", to_id="A", kind="CALLS"),
        EdgeInfo(from_id="A",    to_id="B", kind="CALLS"),
        EdgeInfo(from_id="B",    to_id="C", kind="CALLS"),
        EdgeInfo(from_id="C",    to_id="D", kind="CALLS"),
        EdgeInfo(from_id="B",    to_id="E", kind="IMPORTS"),
    ]
    store.add_symbols_batch(syms)
    store.add_edges_batch(edges)
    return store


# ─── basic insertion ────────────────────────────────────────────────


def test_add_single_symbol(store: FalkorDBLiteStore):
    sym = SymbolInfo(id="x", name="x", kind="function", file="f.ts", start_line=1, language="ts")
    store.add_symbol(sym)
    got = store.get_symbol("x")
    assert got is not None
    assert got.id == "x"
    assert got.name == "x"
    assert got.kind == "function"
    assert got.file == "f.ts"


def test_add_symbols_batch_count(store: FalkorDBLiteStore):
    syms = [
        SymbolInfo(id=f"s{i}", name=f"n{i}", kind="function", file="f.ts", start_line=i, language="ts")
        for i in range(50)
    ]
    n = store.add_symbols_batch(syms)
    assert n == 50


def test_add_symbols_idempotent(store: FalkorDBLiteStore):
    """Повторне MERGE того самого id не створює дубль."""
    sym = SymbolInfo(id="dup", name="dup", kind="function", file="f.ts", start_line=1, language="ts")
    store.add_symbols_batch([sym])
    store.add_symbols_batch([sym])
    rows = store.query("MATCH (s:Symbol {id: 'dup'}) RETURN count(s) AS n")
    assert rows[0]["n"] == 1


def test_add_edges_batch(small_graph: FalkorDBLiteStore):
    rows = small_graph.query("MATCH ()-[r:CALLS]->() RETURN count(r) AS n")
    assert rows[0]["n"] == 4
    rows = small_graph.query("MATCH ()-[r:IMPORTS]->() RETURN count(r) AS n")
    assert rows[0]["n"] == 1


def test_unresolved_edges_skipped(store: FalkorDBLiteStore):
    """Edges з to_id=None (unresolved) не вставляються."""
    syms = [SymbolInfo(id="a", name="a", kind="function", file="f.ts", start_line=1, language="ts")]
    store.add_symbols_batch(syms)
    store.add_edges_batch([EdgeInfo(from_id="a", to_id=None, kind="CALLS", confidence="unresolved")])
    rows = store.query("MATCH ()-[r]->() RETURN count(r) AS n")
    assert rows[0]["n"] == 0


# ─── lookups ────────────────────────────────────────────────────────


def test_get_symbol_missing(store: FalkorDBLiteStore):
    assert store.get_symbol("does-not-exist") is None


def test_find_by_name(small_graph: FalkorDBLiteStore):
    results = small_graph.find_by_name("B")
    assert len(results) == 1
    assert results[0].id == "B"


def test_find_by_name_no_match(small_graph: FalkorDBLiteStore):
    assert small_graph.find_by_name("nonexistent") == []


# ─── BFS expansion ──────────────────────────────────────────────────


def test_bfs_depth_1(small_graph: FalkorDBLiteStore):
    """1-hop від seed → тільки A."""
    exp = small_graph.bfs_expand(["seed"], depth=1)
    assert len(exp.roots) == 1
    assert exp.roots[0].id == "seed"
    reached_ids = {s.id for s in exp.reached}
    assert reached_ids == {"A"}


def test_bfs_depth_2(small_graph: FalkorDBLiteStore):
    """2-hop від seed → A, B."""
    exp = small_graph.bfs_expand(["seed"], depth=2)
    reached_ids = {s.id for s in exp.reached}
    assert reached_ids == {"A", "B"}


def test_bfs_depth_3_full_chain(small_graph: FalkorDBLiteStore):
    """3-hop CALLS-only від seed → A, B, C (D на 4-му hop)."""
    exp = small_graph.bfs_expand(["seed"], depth=3, edge_kinds=["CALLS"])
    reached_ids = {s.id for s in exp.reached}
    assert reached_ids == {"A", "B", "C"}


def test_bfs_includes_imports(small_graph: FalkorDBLiteStore):
    """3-hop, без edge_kinds filter → також IMPORTS, тому E reachable від B (depth=2)."""
    exp = small_graph.bfs_expand(["seed"], depth=3)
    reached_ids = {s.id for s in exp.reached}
    assert "E" in reached_ids  # seed→A→B→E (3 hops, mix CALLS+IMPORTS)


def test_bfs_edge_kind_filter_excludes(small_graph: FalkorDBLiteStore):
    """edge_kinds=['CALLS'] виключає E (доступний тільки через IMPORTS)."""
    exp = small_graph.bfs_expand(["seed"], depth=5, edge_kinds=["CALLS"])
    reached_ids = {s.id for s in exp.reached}
    assert "E" not in reached_ids
    assert reached_ids == {"A", "B", "C", "D"}


def test_bfs_edges_returned(small_graph: FalkorDBLiteStore):
    """exp.edges містить правильні (from, to, kind) пари."""
    exp = small_graph.bfs_expand(["seed"], depth=3, edge_kinds=["CALLS"])
    edge_set = {(f, t, k) for f, t, k in exp.edges}
    assert ("seed", "A", "CALLS") in edge_set
    assert ("A", "B", "CALLS") in edge_set
    assert ("B", "C", "CALLS") in edge_set


def test_bfs_max_nodes_truncates(store: FalkorDBLiteStore):
    """Лінійний ланцюг з 50 нодами + max_nodes=10 → truncated=True."""
    syms = [SymbolInfo(id=f"n{i}", name=f"n{i}", kind="function", file="f.ts", start_line=i, language="ts") for i in range(50)]
    edges = [EdgeInfo(from_id=f"n{i}", to_id=f"n{i+1}", kind="CALLS") for i in range(49)]
    store.add_symbols_batch(syms)
    store.add_edges_batch(edges)
    exp = store.bfs_expand(["n0"], depth=10, max_nodes=10)
    assert len(exp.reached) <= 10
    assert exp.truncated is True


def test_bfs_backward_traversal(small_graph: FalkorDBLiteStore):
    """B досяжний у backward напрямку від C (callers пошук)."""
    exp = small_graph.bfs_expand(["C"], depth=2)
    reached_ids = {s.id for s in exp.reached}
    # C ← B ← A (backward) і також C → D (forward)
    assert "B" in reached_ids
    assert "A" in reached_ids
    assert "D" in reached_ids


def test_bfs_no_seeds(store: FalkorDBLiteStore):
    exp = store.bfs_expand([], depth=2)
    assert isinstance(exp, GraphExpansion)
    assert exp.roots == []
    assert exp.reached == []


# ─── security: edge kind validation ─────────────────────────────────


def test_validate_edge_kinds_allowed():
    assert _validate_edge_kinds(None) == list(ALLOWED_EDGE_KINDS)
    assert _validate_edge_kinds(["CALLS"]) == ["CALLS"]


def test_validate_edge_kinds_rejects_injection():
    """Cypher injection через edge_kinds: must raise."""
    with pytest.raises(ValueError, match="Unknown edge kinds"):
        _validate_edge_kinds(["CALLS|EVIL]->()-//comment"])


def test_add_edge_unknown_kind_skipped(store: FalkorDBLiteStore, caplog):
    """Edge з unknown kind ігнорується + warning у лозі."""
    syms = [
        SymbolInfo(id="a", name="a", kind="function", file="f.ts", start_line=1, language="ts"),
        SymbolInfo(id="b", name="b", kind="function", file="f.ts", start_line=1, language="ts"),
    ]
    store.add_symbols_batch(syms)
    n = store.add_edges_batch([EdgeInfo(from_id="a", to_id="b", kind="MALICIOUS_INJECTION")])
    assert n == 0
    rows = store.query("MATCH ()-[r]->() RETURN count(r) AS n")
    assert rows[0]["n"] == 0


def test_bfs_unknown_kind_raises(small_graph: FalkorDBLiteStore):
    with pytest.raises(ValueError):
        small_graph.bfs_expand(["seed"], edge_kinds=["INVALID_KIND"])


# ─── persistence ────────────────────────────────────────────────────


def test_persistence_across_open(tmp_path):
    """Дані залишаються після close + reopen."""
    db_path = tmp_path / "persist.fdblite"
    s1 = make_graph_store(db_path)
    s1.add_symbol(SymbolInfo(id="persist_me", name="persist_me", kind="function", file="f.ts", start_line=1, language="ts"))
    s1.commit()
    s1.close()

    s2 = make_graph_store(db_path)
    got = s2.get_symbol("persist_me")
    s2.close()
    assert got is not None
    assert got.id == "persist_me"


def test_indexes_present(store: FalkorDBLiteStore):
    """Smoke test: indexes створені при init."""
    rows = store.query("CALL db.indexes()")
    # indexes повертаються як рядки з полями label, properties...
    found_props: set[str] = set()
    for row in rows:
        props = row.get("properties") or []
        if isinstance(props, list):
            found_props.update(props)
    assert "id" in found_props
    assert "name" in found_props


# ─── DoD section §13 Phase 4 ────────────────────────────────────────


def test_phase4_dod_2hop_bfs(small_graph: FalkorDBLiteStore):
    """DoD: symbol+edge insertion + 2-hop BFS expansion на test fixture.

    Виконуємо роль як у Tier 2 retriever: дано seed, повернути все що
    досяжне за 2 hops.
    """
    exp = small_graph.bfs_expand(["seed"], depth=2)

    # Інваріанти DoD:
    # 1. roots повертаються
    assert len(exp.roots) == 1
    assert exp.roots[0].id == "seed"

    # 2. reached: A (1-hop) і B (2-hop)
    reached_ids = {s.id for s in exp.reached}
    assert reached_ids == {"A", "B"}

    # 3. edges між зачепленими нодами
    edge_set = {(f, t) for f, t, _ in exp.edges}
    assert ("seed", "A") in edge_set
    assert ("A", "B") in edge_set

    # 4. metadata symbol info збережена правильно (file:line clickable links)
    a_sym = next(s for s in exp.reached if s.id == "A")
    assert a_sym.file == "a.ts"
    assert a_sym.start_line == 10
    assert a_sym.end_line == 15
