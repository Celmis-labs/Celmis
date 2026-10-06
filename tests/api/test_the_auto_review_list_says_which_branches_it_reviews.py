"""The auto-review list names the target branches each repository is reviewed for.

`GET /api/repos` feeds the "Auto-review PRs" card. It showed a toggle and a mode
and nothing about WHICH base branches a review runs for, so a person who turned
a repository on could not tell that `target_branches` restricted it to one
branch. The list now carries the effective patterns and the layer they came
from, resolved by the orchestrator's own `resolve` (repo > workspace > install).
Run against real SQLite rows and a real AutoReview store.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def world(tmp_path, monkeypatch):
    import src.api.auto_review as ar_mod
    from src.api.auto_review import RepoConfig
    from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults

    store = ar_mod.AutoReviewStore(tmp_path / "ar.db")
    monkeypatch.setattr(ar_mod, "_default_store", store)
    store.upsert(RepoConfig(user_id="u1", repo_slug="github_acme-api", provider="github",
                            full_name="acme/api", url="https://github.com/acme/api",
                            workspace_id="ws-1", enabled=True))
    url = f"sqlite:///{tmp_path}/celmis.db"
    engine = sa.create_engine(url)
    RepoReviewPolicy.__table__.create(engine)
    WorkspaceReviewDefaults.__table__.create(engine)
    monkeypatch.setenv("DATABASE_URL", url)

    def _set(*, repo=None, workspace=None):
        with engine.begin() as conn:
            if repo is not None:
                conn.execute(RepoReviewPolicy.__table__.insert().values(
                    repo_slug="github_acme-api", workspace_id="ws-1", enabled=True,
                    prompt_template="", folder_rules=[], agent_prompt_overrides={},
                    mcp_sources=[], target_branches=repo))
            if workspace is not None:
                conn.execute(WorkspaceReviewDefaults.__table__.insert().values(
                    workspace_id="ws-1", target_branches=workspace))

    yield _set
    engine.dispose()


def _listed():
    from src.api.routers.repos import _workspace_repos

    (row,) = _workspace_repos("ws-1")
    return row.target_branches, row.target_branches_source


def test_a_repository_that_overrides_nothing_reviews_every_branch(world):
    assert _listed() == ([], "install")


def test_the_workspace_default_is_named_as_the_source(world):
    world(workspace=["develop"])

    assert _listed() == (["develop"], "workspace")


def test_the_repository_override_beats_the_workspace(world):
    world(repo=["main", "!release/*"], workspace=["develop"])

    assert _listed() == (["main", "!release/*"], "repo")


def test_an_unreadable_database_claims_nothing(world, monkeypatch):
    monkeypatch.delenv("DATABASE_URL")

    assert _listed() == (None, None)
