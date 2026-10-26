"""Migration c8d3a5f1b762: sync_jobs gains the 'deferred' bookkeeping.

Four columns (`defer_checks` NOT NULL, 0), the dedup index and a lookup index
for the deferred list; the model carries the same columns; a downgrade sends
waiting jobs back to the queue and drops exactly what was added.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.models import SyncJob

REVISION = "c8d3a5f1b762"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_sync_jobs_deferred.py")
NEW = ("deferred_since", "defer_reason", "defer_code", "defer_checks")


def _migration():
    spec = importlib.util.spec_from_file_location("m_c8d3a5f1b762", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    cols = [sa.Column(c.name, sa.JSON() if "JSON" in type(c.type).__name__.upper() else c.type,
                      primary_key=c.primary_key, nullable=c.nullable or c.name in NEW)
            for c in SyncJob.__table__.columns if c.name not in NEW]
    sa.Table("sync_jobs", md, *cols)
    md.create_all(e)
    with e.begin() as conn:
        conn.execute(sa.text("CREATE UNIQUE INDEX ix_sync_jobs_dedup ON sync_jobs (dedup_key)"))
    return e


def _run(engine, fn):
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        fn()


def _cols(engine):
    return {c["name"]: c for c in sa.inspect(engine).get_columns("sync_jobs")}


def _indexes(engine):
    return {i["name"] for i in sa.inspect(engine).get_indexes("sync_jobs")}


def test_the_model_and_the_migration_agree_on_the_columns():
    for name in NEW:
        assert name in SyncJob.__table__.columns
    assert SyncJob.__table__.columns["defer_checks"].nullable is False


def test_upgrade_adds_the_columns_and_the_lookup_index(engine):
    _run(engine, _migration().upgrade)
    cols = _cols(engine)
    assert set(NEW) <= set(cols)
    assert cols["defer_checks"]["nullable"] is False
    assert "ix_sync_jobs_deferred" in _indexes(engine)
    assert "ix_sync_jobs_dedup" in _indexes(engine)


def test_an_existing_row_reads_zero_checks_after_the_upgrade(engine):
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO sync_jobs (id, kind, status, payload, created_at, next_run_at, attempts, max_attempts) "
            "VALUES ('j', 'review', 'pending', '{}', '2026-01-01', '2026-01-01', 0, 5)"))
    _run(engine, _migration().upgrade)
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT defer_checks, deferred_since FROM sync_jobs")).one() == (0, None)


def test_downgrade_sends_waiting_jobs_back_to_the_queue_and_drops_the_columns(engine):
    m = _migration()
    _run(engine, m.upgrade)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO sync_jobs (id, kind, status, payload, created_at, next_run_at, attempts, max_attempts, "
            "dedup_key, defer_checks) VALUES "
            "('w', 'review', 'deferred', '{}', '2026-01-01', '2026-01-01', 0, 5, 'k-1', 3)"))
    _run(engine, m.downgrade)
    assert not set(NEW) & set(_cols(engine))
    assert "ix_sync_jobs_deferred" not in _indexes(engine)
    assert "ix_sync_jobs_dedup" in _indexes(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT status FROM sync_jobs")).scalar() == "pending"


def test_the_migration_is_the_only_head_after_its_parent():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == [REVISION]
