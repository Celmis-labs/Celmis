"""Working out which repositories a caller may reach must not open any graph.

The access scope used to list candidates through `tools.list_repos()`, which
builds a summary per repository by starting and stopping that repository's
graph server (seconds each). Every howto call paid that for every repository in
the workspace: four repositories, sixteen seconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.access import effective
from src.config import get_settings
from src.mcp_server import tools


@pytest.fixture
def repos_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        root = get_settings().repos_dir
        for name in ("github_acme-shop", "github_acme-billing"):
            (root / name / ".git").mkdir(parents=True)
        (root / "not-a-checkout").mkdir()
        (root / "stray-file.txt").write_text("x", encoding="utf-8")
        yield root
    finally:
        get_settings.cache_clear()


def test_the_candidate_slugs_come_from_the_checkouts_without_opening_a_graph(
        repos_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[object] = []

    def spy(*args: object, **kwargs: object) -> None:
        opened.append(args)
        raise AssertionError("a graph was opened to list repositories")

    monkeypatch.setattr(tools, "make_graph_store", spy)
    assert effective._indexed_slugs() == ["github_acme-billing", "github_acme-shop"]
    assert not opened


def test_the_slug_listing_skips_anything_that_is_not_a_checkout(repos_dir: Path) -> None:
    assert tools.list_repo_slugs() == ["github_acme-billing", "github_acme-shop"]


def test_the_slug_listing_of_a_missing_directory_is_empty(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "nowhere"))
    get_settings.cache_clear()
    try:
        assert tools.list_repo_slugs() == []
    finally:
        get_settings.cache_clear()
