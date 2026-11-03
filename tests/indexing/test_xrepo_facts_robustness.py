"""Facts are additive: a failure while taking them never fails the index, and
never leaves the previous facts standing as if they described the new graph."""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.indexing.graph import pipeline
from src.indexing.graph.pipeline import index_repo_graph
from src.indexing.graph.xrepo import FactSet, RouteFact, load_facts, save_facts

SLUG = "acme-svc"
APP = '@app.get("/ping")\nasync def ping(): ...\n'


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    repo = tmp_path / "repos" / SLUG
    repo.mkdir(parents=True)
    (repo / "main.py").write_text(APP, encoding="utf-8")
    return Settings(workspace_dir=tmp_path)


def test_a_full_index_records_facts(cfg: Settings) -> None:
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    facts = load_facts(cfg.repo_graph_path(SLUG))
    assert facts is not None and [r.path for r in facts.routes] == ["/ping"]


def test_a_failure_taking_facts_does_not_fail_the_index_and_forgets_the_old_ones(
        cfg: Settings, monkeypatch, caplog) -> None:
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    assert load_facts(cfg.repo_graph_path(SLUG)) is not None

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("compose parse")

    monkeypatch.setattr(pipeline, "service_facts", boom)
    with caplog.at_level("WARNING"):
        result = index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    assert result is not None
    assert load_facts(cfg.repo_graph_path(SLUG)) is None  # unknown, not the old answer
    assert any("xrepo_facts_failed" in r.getMessage() for r in caplog.records)


def test_a_failed_write_after_the_swap_forgets_the_old_facts(cfg: Settings, monkeypatch) -> None:
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)

    def full_disk(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("no space left")

    monkeypatch.setattr(pipeline, "save_facts", full_disk)
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    assert load_facts(cfg.repo_graph_path(SLUG)) is None


def test_the_group_indexer_drops_the_old_sidecar_with_the_old_graph(
        cfg: Settings, monkeypatch) -> None:
    from src.groups.indexer import GroupIndexer
    from src.groups.models import RepoGroup

    graph = cfg.repo_graph_path(SLUG)
    graph.parent.mkdir(parents=True, exist_ok=True)
    save_facts(graph, FactSet(routes=[RouteFact("GET", "/stale", "x.py", 1)]))
    sync = MagicMock()
    sync.clone_or_update.return_value = MagicMock(repo_slug=SLUG, path=cfg.repo_path(SLUG))
    indexer = GroupIndexer(RepoGroup(name="g"), cfg)
    indexer.sync = sync

    def full_disk(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("no space left")

    monkeypatch.setattr("src.indexing.graph.xrepo.save_facts", full_disk)
    with contextlib.suppress(Exception):  # what matters is the sidecar
        indexer._index_one_repo("https://git.example.com/acme/svc", None)
    assert load_facts(graph) is None
