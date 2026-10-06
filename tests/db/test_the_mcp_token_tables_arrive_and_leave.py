"""The two revisions of the per-person MCP tokens, executed both ways.

`test_migration_chain.py` holds the chain as text (one head, parents resolve).
These two EXECUTE: a database that predates the tables gets them, a database
created from the models (the tables are already there) is left alone, and a
downgrade removes exactly what the upgrade made — the rollback path of a deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
TOKENS = ("a7c41e9b2d10", "mcp_tokens", "c1f6b8d0e256", "mcp_tokens")
LOG = ("a7c41e9b2d11", "mcp_call_log", "a7c41e9b2d10", "mcp_call_log")


def _module(revision: str):
    [path] = VERSIONS.glob(f"{revision}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, revision: str, direction: str) -> None:
    module = _module(revision)
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    yield e
    e.dispose()


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_the_revisions_chain_onto_the_head_before_them_and_nothing_follows():
    for revision, _table, parent, _t in (TOKENS, LOG):
        module = _module(revision)
        assert module.revision == revision and module.down_revision == parent


def test_the_tables_arrive_with_their_indexes_and_leave_without_a_trace(engine):
    _run(engine, TOKENS[0], "upgrade")
    _run(engine, LOG[0], "upgrade")
    assert {"mcp_tokens", "mcp_call_log"} <= _tables(engine)
    names = {i["name"] for t in ("mcp_tokens", "mcp_call_log")
             for i in sa.inspect(engine).get_indexes(t)}
    assert {"ix_mcp_tokens_ws_user", "ix_mcp_call_log_ts", "ix_mcp_call_log_token"} <= names
    _run(engine, LOG[0], "downgrade")
    _run(engine, TOKENS[0], "downgrade")
    assert not {"mcp_tokens", "mcp_call_log"} & _tables(engine)


def test_the_tables_hold_the_columns_the_models_read(engine):
    from src.db.models import McpCallLog, McpToken

    _run(engine, TOKENS[0], "upgrade")
    _run(engine, LOG[0], "upgrade")
    for model in (McpToken, McpCallLog):
        have = {c["name"] for c in sa.inspect(engine).get_columns(model.__tablename__)}
        assert {c.name for c in model.__table__.columns} <= have, model.__tablename__


def test_a_database_made_from_the_models_is_left_alone(engine):
    from src.db.models import Base

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO mcp_tokens (id, kind, workspace_id, user_id, allow_write, expires_at) "
            "VALUES ('t1', 'pat', 'w', 'u', 0, '2030-01-01')"))
    _run(engine, TOKENS[0], "upgrade")
    _run(engine, LOG[0], "upgrade")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM mcp_tokens")).scalar() == 1


def test_a_downgrade_with_nothing_to_remove_does_not_raise(engine):
    _run(engine, LOG[0], "downgrade")
    _run(engine, TOKENS[0], "downgrade")
