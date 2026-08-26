"""Unit tests для MCP tools.py — pure functions без FastMCP dependency."""

from __future__ import annotations

import pytest

from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.graph_store import make_graph_store
from src.mcp_server import tools


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    import src.groups.manager as gm
    from src.config import get_settings
    get_settings.cache_clear()
    gm._default_manager = None
    yield tmp_path
    get_settings.cache_clear()
    gm._default_manager = None


@pytest.fixture
def synthetic_repo_graph(isolated_workspace, tmp_path) -> str:
    """Створити synthetic repo з graph файл — для symbol queries."""
    from src.config import get_settings
    settings = get_settings()
    slug = "test-repo"
    db_path = settings.repo_graph_path(slug)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = make_graph_store(db_path)
    try:
        # Граф: caller_func → CALLS → helper_func → CALLS → util_func
        symbols = [
            SymbolInfo(
                id="src/main.py::caller_func", name="caller_func",
                kind="function", file="src/main.py", start_line=1,
                language="python", is_exported=True,
            ),
            SymbolInfo(
                id="src/lib.py::helper_func", name="helper_func",
                kind="function", file="src/lib.py", start_line=10,
                language="python", is_exported=True,
            ),
            SymbolInfo(
                id="src/util.py::util_func", name="util_func",
                kind="function", file="src/util.py", start_line=5,
                language="python", is_exported=False,
            ),
        ]
        edges = [
            EdgeInfo(
                from_id="src/main.py::caller_func",
                to_id="src/lib.py::helper_func",
                kind="CALLS", confidence="strong",
            ),
            EdgeInfo(
                from_id="src/lib.py::helper_func",
                to_id="src/util.py::util_func",
                kind="CALLS", confidence="strong",
            ),
        ]
        store.add_symbols_batch(symbols)
        store.add_edges_batch(edges)
        store.commit()
    finally:
        store.close()

    return slug


# ─── Cypher safety ──────────────────────────────────────────────────


class TestCypherSafety:
    def test_read_only_match_passes(self) -> None:
        assert tools._is_read_only_cypher("MATCH (n) RETURN n") is True

    def test_create_blocked(self) -> None:
        assert tools._is_read_only_cypher("CREATE (n:Foo)") is False

    def test_delete_blocked(self) -> None:
        assert tools._is_read_only_cypher("MATCH (n) DELETE n") is False

    def test_set_blocked(self) -> None:
        assert tools._is_read_only_cypher("MATCH (n) SET n.x = 1") is False

    def test_merge_blocked(self) -> None:
        assert tools._is_read_only_cypher("MERGE (n:Foo {id: 'x'})") is False

    def test_keyword_in_string_safe(self) -> None:
        """'CREATE' у string literal — НЕ trigger."""
        cypher = "MATCH (n) WHERE n.name = 'CreatedAt' RETURN n"
        # CreatedAt — не keyword (lowercase + camelCase). 'CREATE' in upper text
        # буде found, але оскільки ми strip strings, OK.
        assert tools._is_read_only_cypher(cypher) is True

    def test_keyword_substring_not_match(self) -> None:
        """'MATCHED' (column name) — не false-positive."""
        cypher = "MATCH (n) RETURN n.matched AS matched"
        assert tools._is_read_only_cypher(cypher) is True


# ─── list_groups / list_repos ───────────────────────────────────────


class TestListGroups:
    def test_empty_workspace(self, isolated_workspace) -> None:
        result = tools.list_groups()
        assert result == []

    def test_with_groups(self, isolated_workspace) -> None:
        from src.groups import get_group_manager
        mgr = get_group_manager()
        mgr.create("alpha", description="A")
        mgr.create("beta", description="B")
        mgr.add_repo("alpha", "github:foo/bar")

        result = tools.list_groups()
        assert len(result) == 2
        names = {g.name for g in result}
        assert names == {"alpha", "beta"}

        alpha = next(g for g in result if g.name == "alpha")
        assert alpha.repo_count == 1
        assert alpha.description == "A"
        assert alpha.cross_repo_indexed is False
        assert alpha.cross_repo_edges == 0


class TestListRepos:
    def test_empty(self, isolated_workspace) -> None:
        assert tools.list_repos() == []

    def test_group_filter(self, isolated_workspace) -> None:
        from src.groups import get_group_manager
        mgr = get_group_manager()
        mgr.create("g")
        mgr.add_repo("g", "github:foo/bar")
        mgr.add_repo("g", "github:baz/qux")

        result = tools.list_repos(group_name="g")
        assert len(result) == 2
        slugs = {r.slug for r in result}
        assert "github_foo-bar" in slugs
        assert "github_baz-qux" in slugs

    def test_nonexistent_group(self, isolated_workspace) -> None:
        assert tools.list_repos(group_name="ghost") == []


# ─── find_symbol / get_symbol ───────────────────────────────────────


class TestFindSymbol:
    def test_find_existing(self, synthetic_repo_graph) -> None:
        result = tools.find_symbol(name="helper_func", repo_slug=synthetic_repo_graph)
        assert len(result) == 1
        assert result[0]["name"] == "helper_func"
        assert result[0]["kind"] == "function"
        assert result[0]["repo_slug"] == synthetic_repo_graph

    def test_find_nonexistent(self, synthetic_repo_graph) -> None:
        result = tools.find_symbol(name="ghost", repo_slug=synthetic_repo_graph)
        assert result == []

    def test_find_no_repo(self, isolated_workspace) -> None:
        result = tools.find_symbol(name="any", repo_slug="missing-repo")
        assert result == []


class TestGetSymbol:
    def test_get_existing(self, synthetic_repo_graph) -> None:
        result = tools.get_symbol(
            symbol_id="src/lib.py::helper_func",
            repo_slug=synthetic_repo_graph,
        )
        assert result is not None
        assert result["name"] == "helper_func"
        assert result["language"] == "python"
        assert result["repo_slug"] == synthetic_repo_graph

    def test_get_missing(self, synthetic_repo_graph) -> None:
        result = tools.get_symbol(
            symbol_id="nonexistent::id",
            repo_slug=synthetic_repo_graph,
        )
        assert result is None


# ─── find_callers / find_callees ────────────────────────────────────


class TestCallers:
    def test_direct_callers(self, synthetic_repo_graph) -> None:
        """helper_func is called by caller_func (depth 1)."""
        result = tools.find_callers(
            symbol_id="src/lib.py::helper_func",
            repo_slug=synthetic_repo_graph,
            depth=1,
        )
        callers = result["callers"]
        assert len(callers) == 1
        assert callers[0]["name"] == "caller_func"
        assert callers[0]["hops"] == 1

    def test_transitive_callers(self, synthetic_repo_graph) -> None:
        """util_func ← helper_func ← caller_func — depth 2."""
        result = tools.find_callers(
            symbol_id="src/util.py::util_func",
            repo_slug=synthetic_repo_graph,
            depth=2,
        )
        callers = result["callers"]
        names = {c["name"] for c in callers}
        # Має містити обидва — direct (helper_func) + transitive (caller_func)
        assert "helper_func" in names
        assert "caller_func" in names

    def test_callers_for_root(self, synthetic_repo_graph) -> None:
        """caller_func — entry point, у нього нема callers."""
        result = tools.find_callers(
            symbol_id="src/main.py::caller_func",
            repo_slug=synthetic_repo_graph,
            depth=2,
        )
        assert result["callers"] == []


class TestCallees:
    def test_direct_callees(self, synthetic_repo_graph) -> None:
        """caller_func calls helper_func (depth 1)."""
        result = tools.find_callees(
            symbol_id="src/main.py::caller_func",
            repo_slug=synthetic_repo_graph,
            depth=1,
        )
        callees = result["callees"]
        names = {c["name"] for c in callees}
        assert "helper_func" in names

    def test_transitive_callees(self, synthetic_repo_graph) -> None:
        """caller_func → helper_func → util_func — depth 2."""
        result = tools.find_callees(
            symbol_id="src/main.py::caller_func",
            repo_slug=synthetic_repo_graph,
            depth=2,
        )
        callees = result["callees"]
        names = {c["name"] for c in callees}
        assert "helper_func" in names
        assert "util_func" in names


# ─── query_graph (Cypher escape hatch) ──────────────────────────────


class TestQueryGraph:
    def test_read_only_query_runs(self, synthetic_repo_graph) -> None:
        result = tools.query_graph(
            cypher="MATCH (s:Symbol) RETURN count(s) AS c",
            repo_slug=synthetic_repo_graph,
        )
        assert result["ok"] is True
        assert result["rows"][0]["c"] == 3

    def test_write_query_blocked(self, synthetic_repo_graph) -> None:
        result = tools.query_graph(
            cypher="CREATE (n:Foo {id: 'x'})",
            repo_slug=synthetic_repo_graph,
        )
        assert result["ok"] is False
        assert "write keywords" in result["error"].lower()

    def test_no_target_db(self, isolated_workspace) -> None:
        result = tools.query_graph(
            cypher="MATCH (n) RETURN n",
        )
        assert result["ok"] is False
        assert "repo_slug or group_name" in result["error"]

    def test_missing_db(self, isolated_workspace) -> None:
        result = tools.query_graph(
            cypher="MATCH (n) RETURN n",
            repo_slug="nonexistent-repo",
        )
        assert result["ok"] is False
        assert "not found" in result["error"].lower()


# ─── cross_repo_edges ───────────────────────────────────────────────


class TestCrossRepoEdges:
    def test_no_group_db(self, isolated_workspace) -> None:
        result = tools.cross_repo_edges(group_name="ghost")
        assert result == []

    def test_with_edges(self, isolated_workspace) -> None:
        """Створюємо synthetic cross-repo graph + перевіряємо retrieval."""
        from src.groups.cross_repo import CrossRepoEdge
        from src.groups.indexer import GroupIndexer
        from src.groups.models import RepoGroup

        group = RepoGroup(name="test")
        indexer = GroupIndexer(group)
        edges_in = [
            CrossRepoEdge(
                from_repo="repo-a",
                from_id="compose.yml::svc",
                to_repo="repo-b",
                to_id="Dockerfile::__module__",
                kind="REFERENCES_REPO",
                confidence="strong",
                rationale="",
            ),
        ]
        indexer._persist_cross_repo_edges(edges_in)

        result = tools.cross_repo_edges(group_name="test")
        assert len(result) == 1
        assert result[0]["kind"] == "REFERENCES_REPO"
        assert result[0]["from_repo"] == "repo-a"
        assert result[0]["to_repo"] == "repo-b"
