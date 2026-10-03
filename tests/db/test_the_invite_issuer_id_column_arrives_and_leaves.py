"""`workspace_invites.created_by_id` — executed, not just read.

`tests/db/test_migration_chain.py` holds the chain as text (one head, parents
resolve, each column added once). This drives the revision itself: an invite
written before the column existed must still be readable through the model —
it falls back to its email on accept — and the downgrade must reverse.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.orm import Session

from src.db.models import WorkspaceInvite

REVISION = "e5a7c2f19d63"
COLUMN = "created_by_id"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_invite_created_by_id.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_before() -> sa.Table:
    return sa.Table(
        WorkspaceInvite.__tablename__, sa.MetaData(),
        *[sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
          for c in WorkspaceInvite.__table__.columns if c.name != COLUMN],
    )


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    legacy = _table_before()
    legacy.create(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        conn.execute(legacy.insert().values(
            id="inv-1", workspace_id="ws", token_hash="h", email=None, role="member",
            max_uses=1, used_count=0, expires_at=now, revoked=False,
            created_by="admin@acme-corp.io", created_at=now,
        ))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("workspace_invites")}


def test_the_column_arrives_and_an_old_invite_keeps_its_email(engine):
    _run(engine, "upgrade")
    assert COLUMN in _columns(engine)
    with Session(engine) as s:
        row = s.get(WorkspaceInvite, "inv-1")
        assert row is not None
        assert row.created_by_id is None
        assert row.created_by == "admin@acme-corp.io"


def test_running_it_twice_is_harmless(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    assert COLUMN in _columns(engine)


def test_the_migration_reverses(engine):
    _run(engine, "upgrade")
    with Session(engine) as s:
        s.get(WorkspaceInvite, "inv-1").created_by_id = "u-1"
        s.commit()
    _run(engine, "downgrade")
    assert COLUMN not in _columns(engine)
    with engine.begin() as conn:
        row = conn.execute(sa.select(_table_before())).mappings().one()
    assert row["created_by"] == "admin@acme-corp.io"
