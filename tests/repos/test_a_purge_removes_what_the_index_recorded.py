"""Purge has to remove the index-state row that now actually gets written.

`src/repos/purge.py` has always named `RepoIndexState` in its cascade, but the
table was empty in production, so that line had never deleted anything. Now
that every full index writes a row, it does — and the failure mode it prevents
is specific: `last_error` survives the purge, the repo is re-added, and the
repositories page reports a failure from a previous life of a slug that is not
unique across tenants.

Driven against a temporary sqlite database with the real `purge_repo` and the
real writers, because a faked session (which the sibling suite in
tests/repos/test_purge.py uses for its own subject) would delete from nothing.
"""

from __future__ import annotations

import types

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from src.api.auto_review import AutoReviewStore
from src.api.review_runs import ReviewRunStore
from src.config import Settings
from src.db.models import (
    Base,
    DeprecatedSymbol,
    OwnershipSnapshot,
    ProjectRepo,
    RepoAccessRule,
    RepoIndexState,
    RepoReviewPolicy,
    RepoSummary,
    RepoTeamAccess,
)
from src.repos.index_state import (
    read_index_state,
    record_index_failure,
    record_index_success,
)
from src.repos.purge import purge_repo

SLUG = "github_Acme-Dev-todo-app"
SHA_A = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f80911"
SHA_B = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"

# The models `_purge_postgres` deletes from. The first delete (ProjectRepo) is
# the one NOT wrapped in its own try, so a missing table there aborts the whole
# postgres step and the index-state row would survive for the wrong reason.
_PURGED = (
    ProjectRepo, RepoReviewPolicy, RepoIndexState, RepoSummary,
    OwnershipSnapshot, DeprecatedSymbol, RepoTeamAccess, RepoAccessRule,
)


# `repo_review_policies` and friends are JSONB from `target_branches` down.
# Rendering JSONB as sqlite's JSON is a TEST-side shim: the DDL that reaches a
# real database still comes from Alembic.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "celmis.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = sa.create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine, tables=[m.__table__ for m in _PURGED])
    engine.dispose()

    settings = Settings(
        gemini_api_key="test-key",
        workspace_dir=tmp_path / "ws",
        vault_dir=tmp_path / "vault",
    )
    settings.ensure_directories()
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    # GroupManager holds its own `get_settings` reference from import time.
    monkeypatch.setattr("src.groups.manager.get_settings", lambda: settings)
    monkeypatch.setattr(
        "src.api.auto_review.get_auto_review_store",
        lambda: AutoReviewStore(tmp_path / "auto_review.db"),
    )
    monkeypatch.setattr(
        "src.api.review_runs.get_review_run_store",
        lambda: ReviewRunStore(tmp_path / "review_runs.db"),
    )
    return types.SimpleNamespace(db=db, settings=settings)


async def _purge(world):
    """The real cascade, minus the two stores this file is not about."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{world.db}")
    try:
        async with AsyncSession(engine) as session:
            return await purge_repo(
                SLUG, session=session, workspace_id="ws-1",
                skip_qdrant=True, skip_disk=True,
            )
    finally:
        await engine.dispose()


async def test_a_purge_removes_the_row_a_successful_index_wrote(world):
    record_index_success(SLUG, sha=SHA_A, files=37, full_rebuild=True)
    assert read_index_state(SLUG) is not None, "nothing to purge — bad setup"

    report = await _purge(world)

    assert read_index_state(SLUG) is None
    assert report.orphan_rows_removed >= 1
    assert not [e for e in report.errors if "repo_index_state" in e]


async def test_a_purge_takes_the_recorded_failure_with_it(world):
    """A stale `last_error` outliving its repo would blame a re-added repo for
    the previous tenant's dead index."""
    record_index_success(SLUG, sha=SHA_A, files=37, full_rebuild=True)
    record_index_failure(SLUG, "dangling symlink packages/prisma/.env")
    assert read_index_state(SLUG).last_error is not None

    await _purge(world)

    assert read_index_state(SLUG) is None


async def test_a_reindex_after_a_purge_starts_clean(world):
    record_index_success(SLUG, sha=SHA_A, files=37, full_rebuild=True)
    record_index_failure(SLUG, "dangling symlink packages/prisma/.env")
    before = read_index_state(SLUG)

    await _purge(world)
    record_index_success(SLUG, sha=SHA_B, files=5, full_rebuild=True)

    after = read_index_state(SLUG)
    assert after.last_indexed_sha == SHA_B
    assert after.last_indexed_files == 5
    assert after.last_error is None
    assert after.last_full_rebuild_at > before.last_full_rebuild_at


async def test_purging_a_repo_that_was_never_indexed_is_a_no_op(world):
    report = await _purge(world)

    assert read_index_state(SLUG) is None
    assert not [e for e in report.errors if "repo_index_state" in e]
