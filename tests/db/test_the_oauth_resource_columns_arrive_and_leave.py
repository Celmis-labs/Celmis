"""Migration b4d8f2a6c1e7: `resource` on codes and refresh tokens, `grant_id` on refresh tokens.

Nullable text columns, an idempotent upgrade, a downgrade that drops exactly
these and keeps the rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.models import OAuthAuthCode, OAuthRefreshToken

REVISION = "b4d8f2a6c1e7"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_oauth_resource_and_grant.py")
NEW = {"oauth_auth_codes": {"resource"}, "oauth_refresh_tokens": {"resource", "grant_id"}}
MODELS = {"oauth_auth_codes": OAuthAuthCode, "oauth_refresh_tokens": OAuthRefreshToken}


def _migration():
    spec = importlib.util.spec_from_file_location("m_b4d8f2a6c1e7", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(md: sa.MetaData, name: str, without: set[str]) -> None:
    sa.Table(name, md, *[
        sa.Column(c.name, sa.JSON() if "JSON" in type(c.type).__name__.upper() else c.type,
                  primary_key=c.primary_key, nullable=c.nullable)
        for c in MODELS[name].__table__.columns if c.name not in without])


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    for name, new in NEW.items():
        _table(md, name, new)
    md.create_all(e)
    yield e
    e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_it_chains_off_the_previous_head():
    assert (_migration().revision, _migration().down_revision) == (REVISION, "a3c5e7b91d24")


def test_the_columns_arrive_nullable(engine):
    _run(engine, "upgrade")
    for table, new in NEW.items():
        columns = _columns(engine, table)
        for name in new:
            assert name in columns and columns[name]["nullable"], f"{table}.{name}"


def test_upgrading_twice_changes_nothing(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    assert "grant_id" in _columns(engine, "oauth_refresh_tokens")


def test_a_database_made_from_the_models_upgrades_cleanly(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/from_models.db")
    md = sa.MetaData()
    for name in NEW:
        _table(md, name, set())
    md.create_all(e)
    _run(e, "upgrade")
    assert "resource" in _columns(e, "oauth_auth_codes")
    e.dispose()


def test_downgrade_drops_only_those_columns_and_keeps_rows(engine):
    with engine.begin() as conn:
        cols = [c for c in MODELS["oauth_auth_codes"].__table__.columns
                if c.name not in NEW["oauth_auth_codes"] and not c.nullable]
        conn.execute(
            sa.text(f"INSERT INTO oauth_auth_codes ({', '.join(c.name for c in cols)}) "
                    f"VALUES ({', '.join(repr('x') if 'Bool' not in type(c.type).__name__ and 'DateTime' not in type(c.type).__name__ else ('0' if 'Bool' in type(c.type).__name__ else repr('2026-01-01 00:00:00')) for c in cols)})"))
    before = {t: set(_columns(engine, t)) for t in NEW}
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    for table in NEW:
        assert set(_columns(engine, table)) == before[table], table
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM oauth_auth_codes")).scalar() == 1
    _run(engine, "downgrade")
    _run(engine, "upgrade")
