"""017a06ecb85d executed both ways: the ``repo_shares`` table and the two columns
that name a share, ``mcp_tokens.shared_repos`` and ``mcp_call_log.shares``.

A database that predates them gets them; one created from the models (they are
already there) is left alone; a downgrade removes exactly what the upgrade made
and nothing of the token and call tables besides the two columns. The models and
the migrated schema describe the same table, and the database itself refuses the
two things the code must never be able to write: a share of a workspace to itself
and a second live share of one repository to the same workspace.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite
from src.db.models import Base

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
REVISION = "017a06ecb85d"
PARENT = "e5b8c2d9a1f6"
INDEXES = {"ix_repo_shares_owner", "ix_repo_shares_grantee", "uq_repo_shares_live"}


def _module():
    [path] = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, direction: str, *, named: bool = True) -> None:
    module = _module()
    with engine.begin() as conn:
        # The models' naming convention is what `alembic upgrade` runs under
        # (env.py hands it the same metadata), and it names the CHECK constraints.
        opts = {"target_metadata": Base.metadata} if named else {}
        module.op = Operations(MigrationContext.configure(conn, opts=opts))
        getattr(module, direction)()


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    with e.begin() as conn:
        # The two tables as they were before this revision.
        conn.execute(sa.text(
            "CREATE TABLE mcp_tokens (id TEXT PRIMARY KEY, repo_patterns JSON NOT NULL DEFAULT '[]')"))
        conn.execute(sa.text(
            "CREATE TABLE mcp_call_log (id TEXT PRIMARY KEY, tool TEXT NOT NULL, "
            "repos JSON NOT NULL DEFAULT '[]', packs JSON)"))
    yield e
    e.dispose()


def _cols(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _indexes(engine, table):
    return {i["name"] for i in sa.inspect(engine).get_indexes(table)}


def _row(**over):
    base = {
        "id": "a1b2c3d4-0000-4000-8000-000000000001", "owner_workspace_id": "ws-o",
        "grantee_workspace_id": "ws-g", "repo_slug": "github_acme-shared",
        "repo_provider": "github", "repo_full_name": "acme/shared",
        "expires_at": datetime.now(UTC) + timedelta(days=30),
    }
    return {**base, **over}


def _insert(conn, **over) -> None:
    from src.db.models import RepoShare

    conn.execute(sa.insert(RepoShare.__table__).values(**_row(**over)))


def test_it_chains_onto_the_head_before_it():
    module = _module()
    assert module.revision == REVISION and module.down_revision == PARENT


def test_the_table_and_both_columns_arrive_and_leave_without_a_trace(engine):
    _run(engine, "upgrade")
    assert "repo_shares" in sa.inspect(engine).get_table_names()
    assert _indexes(engine, "repo_shares") >= INDEXES
    assert "shared_repos" in _cols(engine, "mcp_tokens")
    assert "shares" in _cols(engine, "mcp_call_log")

    _run(engine, "downgrade")
    assert "repo_shares" not in sa.inspect(engine).get_table_names()
    assert _cols(engine, "mcp_tokens") == {"id", "repo_patterns"}
    assert _cols(engine, "mcp_call_log") == {"id", "tool", "repos", "packs"}


def test_running_it_twice_changes_nothing(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    assert _indexes(engine, "repo_shares") >= INDEXES


def test_a_database_made_from_the_models_is_left_alone(engine, tmp_path):
    made = sa.create_engine(f"sqlite:///{tmp_path}/from-models.db")
    Base.metadata.create_all(made)
    with made.begin() as conn:
        _insert(conn)
    _run(made, "upgrade", named=False)
    with made.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM repo_shares")).scalar() == 1
    made.dispose()


def test_a_downgrade_with_nothing_to_remove_does_not_raise(engine):
    _run(engine, "downgrade")


def test_the_table_holds_every_column_the_model_reads(engine):
    from src.db.models import McpCallLog, McpToken, RepoShare

    _run(engine, "upgrade")
    assert {c.name for c in RepoShare.__table__.columns} <= _cols(engine, "repo_shares")
    assert McpToken.shared_repos.key in _cols(engine, "mcp_tokens")
    assert McpCallLog.shares.key in _cols(engine, "mcp_call_log")


def _only_shares(obj, name, type_, reflected, compare_to):
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", "")
    return table == "repo_shares"


def test_the_migrated_table_and_the_model_have_no_difference(engine):
    _run(engine, "upgrade")
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={
            "target_metadata": Base.metadata, "include_object": _only_shares,
            "compare_type": False,          # SQLite reflects JSONB as another type
        })
        assert compare_metadata(context, Base.metadata) == []


# ─── what the database itself refuses ────────────────────────────────


def test_a_workspace_cannot_share_with_itself(engine):
    _run(engine, "upgrade")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _insert(conn, grantee_workspace_id="ws-o")


@pytest.mark.parametrize("people", [0, 26, -1])
def test_the_number_of_people_stays_between_one_and_twenty_five(engine, people):
    _run(engine, "upgrade")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _insert(conn, max_people=people)


def test_a_second_live_share_of_the_same_repository_to_the_same_workspace_is_refused(engine):
    _run(engine, "upgrade")
    with engine.begin() as conn:
        _insert(conn)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _insert(conn, id="a1b2c3d4-0000-4000-8000-000000000002")


def test_a_revoked_share_does_not_block_a_new_one_and_stays_as_history(engine):
    _run(engine, "upgrade")
    with engine.begin() as conn:
        _insert(conn, revoked_at=datetime.now(UTC), revoked_by="u-owner", revoke_reason="owner")
        _insert(conn, id="a1b2c3d4-0000-4000-8000-000000000002")
        # ... and a different receiver, or a different repository, is a different share.
        _insert(conn, id="a1b2c3d4-0000-4000-8000-000000000003", grantee_workspace_id="ws-h")
        _insert(conn, id="a1b2c3d4-0000-4000-8000-000000000004", repo_slug="github_acme-other")
        assert conn.execute(sa.text("SELECT count(*) FROM repo_shares")).scalar() == 4


def test_a_new_share_starts_closed_to_questions_and_with_five_places(engine):
    _run(engine, "upgrade")
    with engine.begin() as conn:
        _insert(conn)
        row = conn.execute(sa.text(
            "SELECT allow_ask, max_people, deny_globs, blocked_user_ids FROM repo_shares")).one()
    assert not row.allow_ask and row.max_people == 5
    assert json.loads(row.deny_globs) == [] and json.loads(row.blocked_user_ids) == []
