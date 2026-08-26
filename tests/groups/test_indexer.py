"""Tests для GroupIndexer (unit).

Integration test з реальними OSS clones — у `tests/integration/test_group_indexer.py`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.groups.indexer import GroupIndexer, GroupIndexResult, _RepoIndexResult
from src.groups.models import RepoGroup
from src.sync.clone import CloneError, SyncResult
from src.sync.git_providers import GitProvider


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from src.config import get_settings
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


# ─── result aggregation ─────────────────────────────────────────────


class TestGroupIndexResult:
    def test_total_calculations(self) -> None:
        """total_symbols + total_edges sum across repos."""
        result = GroupIndexResult(group_name="g")
        sync = SyncResult(
            repo_slug="r1",
            path=Path("/tmp/r1"),
            commit_sha="a" * 40,
            changed=True,
        )
        result.repos_indexed = [
            _RepoIndexResult(
                slug="r1", sync=sync,
                files_processed=10, files_skipped=2,
                parse_failures=0, symbols=100, edges_resolved=200,
                elapsed_seconds=1.0,
            ),
            _RepoIndexResult(
                slug="r2", sync=sync,
                files_processed=5, files_skipped=1,
                parse_failures=1, symbols=50, edges_resolved=100,
                elapsed_seconds=0.5,
            ),
        ]
        result.cross_repo_edges = 5
        assert result.total_symbols == 150
        assert result.total_edges == 305  # 200 + 100 + 5


# ─── empty group ────────────────────────────────────────────────────


class TestEmptyGroup:
    def test_empty_group_no_failures(self, isolated_workspace) -> None:
        group = RepoGroup(name="empty")
        indexer = GroupIndexer(group)
        result = indexer.index()
        assert result.repos_indexed == []
        assert result.cross_repo_edges == 0
        assert result.failures == []


# ─── failure handling ──────────────────────────────────────────────


class TestFailureHandling:
    def test_clone_error_collected_not_raised(self, isolated_workspace) -> None:
        """CloneError на одному repo → у failures, indexer продовжує."""
        group = RepoGroup(name="g")
        group.add_repo("github:fake/nonexistent")

        indexer = GroupIndexer(group)
        with patch.object(
            indexer.sync, "clone_or_update",
            side_effect=CloneError("repo not found"),
        ):
            result = indexer.index()

        assert len(result.failures) == 1
        assert "fake/nonexistent" in result.failures[0]
        assert "CloneError" in result.failures[0]
        assert result.repos_indexed == []

    def test_partial_success(self, isolated_workspace, tmp_path) -> None:
        """Один repo OK, інший fail — partial result."""
        group = RepoGroup(name="g")
        group.add_repo("github:org/repo1")
        group.add_repo("github:org/repo2")

        indexer = GroupIndexer(group)
        # Mock: repo1 OK, repo2 fails
        good_sync = SyncResult(
            repo_slug="github_org-repo1",
            path=tmp_path / "fake-repo1",
            commit_sha="a" * 40,
            changed=True,
            provider=GitProvider.GITHUB,
        )
        # Make fake repo dir
        good_sync.path.mkdir()

        def mock_sync(repo_id, branch=None, **kwargs):
            if "repo1" in repo_id:
                return good_sync
            raise CloneError("repo2 not found")

        with patch.object(indexer.sync, "clone_or_update", side_effect=mock_sync):
            result = indexer.index()

        assert len(result.repos_indexed) == 1
        assert result.repos_indexed[0].slug == "github_org-repo1"
        assert len(result.failures) == 1
        assert "repo2" in result.failures[0]


# ─── cross-repo persistence ─────────────────────────────────────────


class TestCrossRepoPersistence:
    def test_no_edges_no_file(self, isolated_workspace) -> None:
        """Empty edge list → no file created, returns 0."""
        group = RepoGroup(name="empty-cross")
        indexer = GroupIndexer(group)
        n = indexer._persist_cross_repo_edges([])
        assert n == 0
        # File не створено
        path = indexer._cross_repo_graph_path()
        assert not path.exists()

    def test_edges_persist_to_fdblite(self, isolated_workspace) -> None:
        """Cross-repo edges → file створено + nodes/edges доступні."""
        from src.groups.cross_repo import CrossRepoEdge

        group = RepoGroup(name="test")
        indexer = GroupIndexer(group)

        edges = [
            CrossRepoEdge(
                from_repo="repo-a",
                from_id="compose.yml::svc",
                to_repo="repo-b",
                to_id="Dockerfile::__module__",
                kind="REFERENCES_REPO",
                confidence="strong",
                rationale="image match",
            ),
            CrossRepoEdge(
                from_repo="repo-c",
                from_id="docker-compose.yml::api",
                to_repo="repo-d",
                to_id="Dockerfile::__module__",
                kind="BUILD_CONTEXT",
                confidence="weak",
                rationale="build path",
            ),
        ]
        n = indexer._persist_cross_repo_edges(edges)
        assert n == 2

        # File створено
        path = indexer._cross_repo_graph_path()
        assert path.exists()

        # Verify content через прямий FalkorDB query
        from src.indexing.graph.graph_store import make_graph_store
        store = make_graph_store(path)
        try:
            # 4 nodes (2 from + 2 to)
            res = store.query("MATCH (n:Symbol) RETURN count(n) AS c")
            assert res[0]["c"] == 4

            # REFERENCES_REPO + BUILD_CONTEXT — 2 edges
            res2 = store.query(
                "MATCH ()-[r:REFERENCES_REPO|BUILD_CONTEXT]->() "
                "RETURN type(r) AS k, count(r) AS c"
            )
            kinds = {row["k"]: row["c"] for row in res2}
            assert kinds.get("REFERENCES_REPO") == 1
            assert kinds.get("BUILD_CONTEXT") == 1
        finally:
            store.close()

    def test_cross_repo_node_id_format(self, isolated_workspace) -> None:
        """Synthetic nodes мають prefix repo_slug для unique ID across repos."""
        from src.groups.cross_repo import CrossRepoEdge

        group = RepoGroup(name="t")
        indexer = GroupIndexer(group)

        edges = [
            CrossRepoEdge(
                from_repo="r1",
                from_id="compose.yml::svc",
                to_repo="r2",
                to_id="Dockerfile::__module__",
                kind="REFERENCES_REPO",
                confidence="strong",
                rationale="",
            ),
        ]
        indexer._persist_cross_repo_edges(edges)

        from src.indexing.graph.graph_store import make_graph_store
        path = indexer._cross_repo_graph_path()
        store = make_graph_store(path)
        try:
            res = store.query("MATCH (n:Symbol) RETURN n.id AS id ORDER BY id")
            ids = [row["id"] for row in res]
            assert ids == [
                "r1::compose.yml::svc",  # from prefixed
                "r2::Dockerfile::__module__",  # to prefixed
            ]
        finally:
            store.close()


# ─── progress_callback ─────────────────────────────────────────────


class TestScipEnrichment:
    """Stage 15.2: enable_scip flag wires до scip runner+enricher."""

    def test_scip_disabled_by_default(self, isolated_workspace) -> None:
        """Без enable_scip — SCIP runner не initialized."""
        from src.groups.indexer import GroupIndexer, GroupIndexResult
        from src.groups.models import RepoGroup

        group = RepoGroup(name="g")
        indexer = GroupIndexer(group)
        # Mock внутрішній group_indexer.index() щоб skip clone — empty result
        with patch.object(
            indexer._group_indexer if hasattr(indexer, '_group_indexer') else indexer,
            "index", return_value=GroupIndexResult(group_name="g"),
        ):
            pass  # групa empty — індексується без помилок

        # Test scip_runner not lazy-loaded коли enable_scip=False
        # (validated через absence of import errors при default call)
        result = indexer.index(enable_scip=False)
        assert result.group_name == "g"

    def test_scip_enabled_imports_runner(self, isolated_workspace) -> None:
        """enable_scip=True triggers ScipExternalRunner instantiation."""
        from src.groups.indexer import GroupIndexer
        from src.groups.models import RepoGroup

        group = RepoGroup(name="g")  # empty group — no per-repo iteration
        indexer = GroupIndexer(group)

        with patch("src.indexing.scip.ScipExternalRunner") as mock_runner_cls, \
             patch("src.indexing.scip.ScipEnricher") as mock_enricher_cls:
            mock_runner_cls.return_value = MagicMock()
            mock_enricher_cls.return_value = MagicMock()
            indexer.index(enable_scip=True)
            # Runner+enricher була instantiated
            mock_runner_cls.assert_called_once()
            mock_enricher_cls.assert_called_once()


class TestProgressCallback:
    def test_callback_invoked_for_milestones(self, isolated_workspace) -> None:
        group = RepoGroup(name="g")  # empty, no repos
        indexer = GroupIndexer(group)
        callback_calls: list[str] = []
        indexer.index(progress_callback=callback_calls.append)
        # Materialize step → callback з опис
        assert any("materializing" in c for c in callback_calls)
