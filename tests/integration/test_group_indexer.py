"""Integration test: GroupIndexer на реальних OSS repos.

Перевіряє end-to-end:
    1. Group definition з 2+ repos
    2. Clone all через RepoSync
    3. Index per-repo (full pipeline через factory + registry)
    4. Persist per-repo graphs у ~/code-analysis/data/{slug}/graph.fdblite
    5. Materialize cross-repo edges (matching image refs / build contexts)
    6. Persist cross-repo graph у ~/code-analysis/groups/{name}.fdblite

OSS repos використовуємо пара з реалістичною cross-repo конфігурацією — pallets/click
як Python source repo, spf13/cobra як Go repo. Реальних cross-repo edges між
ними не очікується (різні організації), тому тест ВЕРИФІКУЄ що pipeline running
end-to-end successfully, не assert'ить N>0 cross-repo edges на real repos.

Run: .venv/bin/pytest -m integration tests/integration/test_group_indexer.py
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.groups.indexer import GroupIndexer
from src.groups.models import RepoGroup

logger = logging.getLogger(__name__)


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    from src.config import get_settings
    get_settings.cache_clear()
    yield workspace
    get_settings.cache_clear()


@pytest.mark.integration
def test_group_indexer_two_repo_group(isolated_workspace: Path) -> None:
    """Two-repo group, end-to-end: clone + index + materialize + persist."""
    group = RepoGroup(name="oss-mix", description="Test multi-language group")
    group.add_repo("https://github.com/pallets/click")
    group.add_repo("https://github.com/spf13/cobra")

    indexer = GroupIndexer(group)
    progress_log: list[str] = []
    result = indexer.index(progress_callback=progress_log.append)

    # Both repos must indexed successfully
    assert len(result.repos_indexed) == 2, (
        f"expected 2 repos, got {len(result.repos_indexed)}, failures: {result.failures}"
    )
    assert result.failures == [], f"unexpected failures: {result.failures}"

    # Per-repo files dispatched (>0 для known languages)
    for r in result.repos_indexed:
        assert r.files_processed > 0, f"no files indexed для {r.slug}"
        assert r.symbols > 0, f"no symbols extracted для {r.slug}"

    # Per-repo graph files exist
    from src.config import get_settings
    settings = get_settings()
    for r in result.repos_indexed:
        path = settings.repo_graph_path(r.slug)
        assert path.exists(), f"per-repo graph missing для {r.slug} at {path}"

    # Group cross-repo path may or may not exist depending on materialization
    # У click + cobra немає cross-repo edges (різні orgs, no image references)
    # Так що це нормально якщо file НЕ створений
    cross_repo_path = indexer._cross_repo_graph_path()
    if result.cross_repo_edges > 0:
        assert cross_repo_path.exists()

    # Diagnostic output
    print("\n=== Group Index Result ===")
    print(f"group: {result.group_name}")
    for r in result.repos_indexed:
        print(
            f"  {r.slug}: {r.files_processed} files, {r.symbols} symbols, "
            f"{r.edges_resolved} edges, {r.elapsed_seconds:.1f}s"
        )
    print(f"cross-repo edges: {result.cross_repo_edges}")
    print(f"total elapsed: {result.elapsed_seconds:.1f}s")


@pytest.mark.integration
def test_group_indexer_synthetic_cross_repo_match(
    isolated_workspace: Path, tmp_path: Path,
) -> None:
    """Synthetic two-repo setup з matching image references —
    validates cross-repo materializer actually fires.

    Створюємо локально 2 fake repos:
        repo-a: Dockerfile (image)
        repo-b: docker-compose.yml що references 'org/repo-a:tag'
    Bypassing clone (вже є локально), безпосередньо викликаємо _index_one_repo
    і materialize.
    """
    from src.groups.cross_repo import CrossRepoMaterializer
    from src.indexing.graph.languages.factory import (
        build_default_registry,
        walk_repo_files,
    )

    # Setup: 2 fake repos у tmp_path
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    (repo_a / "Dockerfile").write_text("""
FROM python:3.13-slim AS app
WORKDIR /app
EXPOSE 8000
""")

    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    (repo_b / "docker-compose.yml").write_text("""
services:
  worker:
    image: myorg/repo-a:latest
""")

    # Викликаємо registry + walker напряму, mimic-ing що зробив би _index_one_repo
    registry_a = build_default_registry(repo_root=repo_a)
    extractions_a: dict = {}
    for f in walk_repo_files(repo_a):
        extractor = registry_a.match(f)
        if extractor is None:
            continue
        rel = str(f.relative_to(repo_a))
        extractions_a[rel] = extractor.extract(f)

    registry_b = build_default_registry(repo_root=repo_b)
    extractions_b: dict = {}
    for f in walk_repo_files(repo_b):
        extractor = registry_b.match(f)
        if extractor is None:
            continue
        rel = str(f.relative_to(repo_b))
        extractions_b[rel] = extractor.extract(f)

    # Materialize. Slugs мають бути realistic (як ParsedRepo.slug повертає):
    #   - 'github_{owner}-{repo}' для GitHub
    #   - '{owner}-{repo}' для Bitbucket (legacy)
    # _extract_repo_name strips provider prefix + splits by first '-'.
    slug_a = "github_myorg-repo-a"  # → repo name 'repo-a'
    slug_b = "github_myorg-repo-b"  # → repo name 'repo-b'

    group = RepoGroup(name="synth")
    materializer = CrossRepoMaterializer(group)
    materializer.add_repo(slug_a, extractions_a)
    materializer.add_repo(slug_b, extractions_b)
    edges = materializer.materialize()

    # Має бути cross-repo REFERENCES_REPO edge: slug_b → slug_a
    ref_edges = [e for e in edges if e.kind == "REFERENCES_REPO"]
    assert len(ref_edges) == 1, (
        f"expected 1 ref edge, got {len(ref_edges)}. "
        f"All edges: {edges}"
    )
    assert ref_edges[0].from_repo == slug_b
    assert ref_edges[0].to_repo == slug_a
    assert ref_edges[0].confidence == "strong"

    # Persist через GroupIndexer
    indexer = GroupIndexer(group)
    n_persisted = indexer._persist_cross_repo_edges(edges)
    assert n_persisted == 1

    # Verify через FalkorDB query
    from src.indexing.graph.graph_store import make_graph_store
    store = make_graph_store(indexer._cross_repo_graph_path())
    try:
        res = store.query(
            "MATCH (a:Symbol)-[r:REFERENCES_REPO]->(b:Symbol) "
            "RETURN a.id AS from_id, b.id AS to_id, type(r) AS kind"
        )
        assert len(res) == 1
        row = res[0]
        assert row["from_id"].startswith(f"{slug_b}::")
        assert row["to_id"].startswith(f"{slug_a}::")
        assert row["kind"] == "REFERENCES_REPO"
    finally:
        store.close()
