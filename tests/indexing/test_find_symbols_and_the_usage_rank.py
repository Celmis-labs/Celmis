"""`find_symbols` (ranked-search candidates) and the in-degree rank written at index time."""

from __future__ import annotations

import pytest

from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.graph_store import make_graph_store, name_tokens
from src.indexing.graph.pagerank import compute_pagerank, write_ranks


def _sym(name, kind="function", file="a.py", start=1):
    return SymbolInfo(id=f"{file}::{name}", name=name, kind=kind, file=file,
                      start_line=start, end_line=start + 2, language="python")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = make_graph_store(tmp_path_factory.mktemp("g") / "g.fdblite")
    syms = [_sym(f"user{i}", "variable", "v.py", i + 1) for i in range(25)]
    syms += [
        _sym("user_summary", file="svc.py", start=1),
        _sym("OrderService", "class", "svc.py", 10),
        _sym("create_order", file="svc.py", start=20),
        _sym("create_order_remote", file="svc.py", start=30),
        _sym("helper", file="svc.py", start=40),
        _sym("caller_one", file="c.py", start=1),
        _sym("caller_two", file="c.py", start=10),
        _sym("caller_three", file="c.py", start=20),
    ]
    s.add_symbols_batch(syms)
    s.add_edges_batch([
        EdgeInfo(from_id="c.py::caller_one", to_id="svc.py::create_order", kind="CALLS", confidence="strong"),
        EdgeInfo(from_id="c.py::caller_two", to_id="svc.py::create_order", kind="CALLS", confidence="strong"),
        EdgeInfo(from_id="c.py::caller_three", to_id="svc.py::create_order", kind="CALLS", confidence="strong"),
        EdgeInfo(from_id="c.py::caller_one", to_id="svc.py::helper", kind="CALLS", confidence="strong"),
    ])
    s.commit()
    yield s
    s.close()


def _names(rows):
    return [r["name"] for r in rows]


# ─── name_tokens ─────────────────────────────────────────────────────

def test_name_tokens_split_camel_snake_and_dotted_names():
    assert name_tokens("OrderService.create_order") == ["order", "service", "create"]
    assert name_tokens("HTTPServer2") == ["http", "server", "2"]
    assert name_tokens("") == []


# ─── find_symbols ────────────────────────────────────────────────────

def test_exact_mode_returns_only_the_symbol_with_that_name(store):
    assert _names(store.find_symbols("create_order", mode="exact")) == ["create_order"]


def test_prefix_mode_returns_every_symbol_that_starts_with_the_text(store):
    assert set(_names(store.find_symbols("create_", mode="prefix"))) == {
        "create_order", "create_order_remote"}


def test_kind_is_applied_before_the_limit_so_one_function_beats_twenty_five_variables(store):
    rows = store.find_symbols("user", kind="function", limit=3)
    assert _names(rows) == ["user_summary"]


def test_without_kind_a_small_limit_is_spent_on_the_shortest_names_first(store):
    rows = store.find_symbols("user", mode="prefix", limit=5)
    assert len(rows) == 5


def test_a_misspelt_name_is_found_in_fuzzy_mode(store):
    assert "OrderService" in _names(store.find_symbols("OrderSrv", mode="fuzzy"))


def test_auto_mode_falls_back_to_fuzzy_when_the_exact_list_is_short(store):
    assert "OrderService" in _names(store.find_symbols("OrderSrv"))


def test_every_row_carries_how_often_the_symbol_is_used(store):
    row = store.find_symbols("create_order", mode="exact")[0]
    assert row["in_degree"] == 3


def test_a_blank_query_returns_nothing(store):
    assert store.find_symbols("   ") == []


def test_a_query_with_quotes_cannot_break_out_of_the_cypher(store):
    assert store.find_symbols("x' OR 1=1 //", mode="exact") == []


def test_symbols_in_file_come_back_in_source_order(store):
    assert [s.name for s in store.symbols_in_file("svc.py")] == [
        "user_summary", "OrderService", "create_order", "create_order_remote", "helper"]


# ─── the usage rank ──────────────────────────────────────────────────

def test_the_most_used_symbol_scores_one_and_an_unused_symbol_is_absent(store):
    scores = compute_pagerank(store)
    assert scores["svc.py::create_order"] == 1.0
    assert 0 < scores["svc.py::helper"] < 1.0
    assert "svc.py::user_summary" not in scores


def test_write_ranks_stores_the_score_on_the_node_where_find_reads_it(store):
    assert write_ranks(store) == 2
    row = store.find_symbols("create_order", mode="exact")[0]
    assert row["rank"] == 1.0


def test_write_ranks_never_raises_on_a_broken_store():
    class Broken:
        def query(self, *a, **kw):
            raise RuntimeError("down")

    assert write_ranks(Broken()) == 0


# ─── the candidate cap is spent on the best names, not the shortest ──

@pytest.fixture
def crowded(tmp_path):
    """One real class among hundreds of short names that share its common token."""
    s = make_graph_store(tmp_path / "g.fdblite")
    syms = [_sym(f"x{i}Service", "class", "noise.py", i + 1) for i in range(400)]
    syms += [_sym("OrderService", "class", "svc.py", 1), _sym("PaymentService", "class", "svc.py", 10)]
    syms += [_sym(f"caller{i}", file="c.py", start=i + 1) for i in range(3)]
    s.add_symbols_batch(syms)
    s.add_edges_batch([EdgeInfo(from_id=f"c.py::caller{i}", to_id="svc.py::PaymentService",
                                kind="CALLS", confidence="strong") for i in range(3)])
    s.commit()
    yield s
    s.close()


def test_a_typo_still_reaches_the_symbol_when_hundreds_of_names_share_a_token(crowded):
    rows = crowded.find_symbols("OrdrService", mode="auto", limit=200)
    assert "OrderService" in _names(rows)


def test_the_most_used_symbol_of_a_common_word_survives_the_candidate_cap(crowded):
    rows = crowded.find_symbols("Service", mode="auto", limit=50)
    assert "PaymentService" in _names(rows)
    assert _names(rows)[0] == "PaymentService"  # most called first among equal matches


def test_an_exact_name_always_wins_the_cap(crowded):
    assert _names(crowded.find_symbols("OrderService", mode="auto", limit=5))[0] == "OrderService"


def test_a_symbol_that_lost_its_last_caller_loses_its_rank(crowded):
    write_ranks(crowded)
    assert compute_pagerank(crowded).get("svc.py::PaymentService")
    crowded.query("MATCH ()-[r:CALLS]->() DELETE r")
    write_ranks(crowded)
    row = next(r for r in crowded.find_symbols("PaymentService", mode="exact"))
    assert not row["rank"] and row["in_degree"] == 0
