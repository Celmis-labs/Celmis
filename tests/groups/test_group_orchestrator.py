"""Tests для GroupGenerationOrchestrator (unit з mocks)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.generation.group_orchestrator import (
    GroupGenerationOrchestrator,
    GroupGenerationResult,
)
from src.generation.orchestrator import GenerationResult
from src.groups.indexer import GroupIndexResult
from src.groups.models import RepoGroup


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    import src.groups.manager as gm
    from src.config import get_settings
    get_settings.cache_clear()
    gm._default_manager = None
    yield tmp_path
    get_settings.cache_clear()
    gm._default_manager = None


def _mock_gen_result(slug: str, modules: int = 3, features: int = 2) -> GenerationResult:
    return GenerationResult(
        repo=slug,
        commit="a" * 40,
        modules_generated=[f"mod_{i}" for i in range(modules)],
        features_generated=[f"feat_{i}" for i in range(features)],
        integrations_generated=[],
        total_tokens_in=1000,
        total_tokens_out=500,
        semgrep_findings=2,
    )


# ─── Result aggregation ────────────────────────────────────────────


class TestResultAggregation:
    def test_total_metrics(self) -> None:
        result = GroupGenerationResult(group_name="g")
        result.per_repo = [
            _mock_gen_result("r1", modules=3, features=2),
            _mock_gen_result("r2", modules=5, features=4),
        ]
        assert result.total_modules == 8
        assert result.total_features == 6
        # tokens: each repo має 1000 + 500 = 1500
        assert result.total_tokens == 3000


# ─── Orchestrator behavior ─────────────────────────────────────────


class TestOrchestrator:
    def test_empty_group_no_failures(self, isolated_workspace) -> None:
        group = RepoGroup(name="empty")
        orch = GroupGenerationOrchestrator(group)

        # Mock GroupIndexer.index() — повертає empty result
        with patch.object(
            orch._group_indexer, "index",
            return_value=GroupIndexResult(group_name="empty"),
        ):
            result = orch.run()

        assert result.per_repo == []
        assert result.failures == []
        # Overview document має бути написаний навіть для empty group
        assert result.overview_path is not None
        assert result.overview_path.exists()

    def test_per_repo_iteration(self, isolated_workspace) -> None:
        """Кожен repo passed до GenerationOrchestrator.run."""
        group = RepoGroup(name="g")
        group.add_repo("github:foo/repo1")
        group.add_repo("github:foo/repo2")

        orch = GroupGenerationOrchestrator(group)

        called_repos: list[str] = []

        def fake_run(repo_id, **kwargs):
            called_repos.append(repo_id)
            return _mock_gen_result(repo_id.replace(":", "_"))

        with patch.object(orch._orchestrator, "run", side_effect=fake_run), \
             patch.object(
                 orch._group_indexer, "index",
                 return_value=GroupIndexResult(group_name="g"),
             ):
            result = orch.run()

        assert called_repos == ["github:foo/repo1", "github:foo/repo2"]
        assert len(result.per_repo) == 2
        assert result.failures == []

    def test_per_repo_failure_collected_not_raised(self, isolated_workspace) -> None:
        """Помилка на одному repo → у failures, інші продовжують."""
        from src.sync.clone import CloneError

        group = RepoGroup(name="g")
        group.add_repo("github:foo/good")
        group.add_repo("github:foo/bad")
        group.add_repo("github:foo/another")

        orch = GroupGenerationOrchestrator(group)

        def fake_run(repo_id, **kwargs):
            if "bad" in repo_id:
                raise CloneError("repo not found")
            return _mock_gen_result(repo_id.replace(":", "_"))

        with patch.object(orch._orchestrator, "run", side_effect=fake_run), \
             patch.object(
                 orch._group_indexer, "index",
                 return_value=GroupIndexResult(group_name="g"),
             ):
            result = orch.run()

        assert len(result.per_repo) == 2  # good + another
        assert len(result.failures) == 1
        assert "github:foo/bad" in result.failures[0]
        assert "clone_failed" in result.failures[0]

    def test_progress_callback_invoked(self, isolated_workspace) -> None:
        group = RepoGroup(name="g")
        group.add_repo("github:foo/r1")

        orch = GroupGenerationOrchestrator(group)

        callback_calls: list[tuple[str, str]] = []

        def fake_run(repo_id, **kwargs):
            return _mock_gen_result(repo_id)

        with patch.object(orch._orchestrator, "run", side_effect=fake_run), \
             patch.object(
                 orch._group_indexer, "index",
                 return_value=GroupIndexResult(group_name="g"),
             ):
            orch.run(progress_callback=lambda p, d: callback_calls.append((p, d)))

        # Має бути callback з 'repo', 'cross_repo', 'overview' phases
        phases = {phase for phase, _ in callback_calls}
        assert "repo" in phases
        assert "cross_repo" in phases
        assert "overview" in phases


# ─── Overview document ─────────────────────────────────────────────


class TestOverviewDocument:
    def test_overview_contains_group_metadata(self, isolated_workspace) -> None:
        group = RepoGroup(name="my-platform", description="Test platform")
        orch = GroupGenerationOrchestrator(group)
        result = GroupGenerationResult(group_name="my-platform")

        overview_path = orch._write_overview(result)
        assert overview_path.exists()
        content = overview_path.read_text()

        assert "group: my-platform" in content
        assert "description: Test platform" in content
        assert "# 🗂️ my-platform" in content

    def test_overview_repos_table(self, isolated_workspace) -> None:
        group = RepoGroup(name="g")
        orch = GroupGenerationOrchestrator(group)

        result = GroupGenerationResult(group_name="g")
        result.per_repo = [
            _mock_gen_result("github_foo-r1", modules=5, features=3),
            _mock_gen_result("github_foo-r2", modules=2, features=1),
        ]

        path = orch._write_overview(result)
        content = path.read_text()
        assert "## Repositories" in content
        assert "github_foo-r1" in content
        assert "| 5 |" in content  # modules count

    def test_overview_failures_section(self, isolated_workspace) -> None:
        group = RepoGroup(name="g")
        orch = GroupGenerationOrchestrator(group)

        result = GroupGenerationResult(group_name="g")
        result.failures = ["clone_failed repo=foo: 404 not found"]

        path = orch._write_overview(result)
        content = path.read_text()
        assert "## ⚠️ Failures" in content
        assert "404 not found" in content

    def test_overview_cross_repo_edges_section(self, isolated_workspace) -> None:
        """Cross-repo edges витягуються з materialized FalkorDB."""
        group = RepoGroup(name="g")
        orch = GroupGenerationOrchestrator(group)

        # Manually create cross-repo graph
        from src.groups.cross_repo import CrossRepoEdge
        from src.groups.indexer import GroupIndexer

        idx = GroupIndexer(group)
        idx._persist_cross_repo_edges([
            CrossRepoEdge(
                from_repo="repo-a",
                from_id="compose.yml::svc",
                to_repo="repo-b",
                to_id="Dockerfile::__module__",
                kind="REFERENCES_REPO",
                confidence="strong",
                rationale="",
            ),
        ])

        result = GroupGenerationResult(group_name="g", cross_repo_edges=1)
        path = orch._write_overview(result)
        content = path.read_text()

        assert "## Cross-repo edges" in content
        assert "REFERENCES_REPO" in content
        assert "repo-a" in content
        assert "repo-b" in content

    def test_overview_no_cross_repo_message(self, isolated_workspace) -> None:
        """Якщо немає edges — explanatory message, не пустий header."""
        group = RepoGroup(name="g")
        orch = GroupGenerationOrchestrator(group)
        result = GroupGenerationResult(group_name="g")

        path = orch._write_overview(result)
        content = path.read_text()
        assert "## Cross-repo edges" in content
        assert "No cross-repo edges materialized" in content
