from __future__ import annotations

import pytest

from src.access import RepoAccessDecision
from tests.security import leaky_repo


@pytest.fixture
def leaky(tmp_path, monkeypatch):
    """A committed repo full of planted secrets, readable by the caller."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    import src.groups.manager as gm
    from src.config import get_settings

    get_settings.cache_clear()
    gm._default_manager = None
    repo = leaky_repo.build(get_settings().repos_dir)
    # Freshness is read from a database this test does not need.
    from src.mcp_server.howto import engine

    monkeypatch.setattr(
        engine, "accessible_code_repos",
        lambda: {repo.slug: RepoAccessDecision.full(repo.slug)},
    )
    monkeypatch.setattr(
        engine, "_idx_provider",
        lambda slug, path: engine.IdxInfo(slug=slug, branch="main", sha="3f2a91c8d7e6b5a4", age="2h", state="fresh"),
    )
    yield repo
    get_settings.cache_clear()
    gm._default_manager = None
