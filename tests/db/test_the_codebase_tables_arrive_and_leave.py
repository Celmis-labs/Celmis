"""e5b8c2d9a1f6 executed both ways: the seven knowledge-pack tables and the
``mcp_call_log.packs`` column.

A database that predates them gets them; one created from the models (they are
already there) is left alone; a downgrade removes exactly what the upgrade made
and leaves the call log's other columns alone.
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
REVISION = "e5b8c2d9a1f6"
TABLES = (
    "knowledge_packs", "knowledge_pack_teams", "knowledge_pack_members",
    "knowledge_pack_versions", "knowledge_items", "knowledge_pack_invites",
    "knowledge_pack_tokens",
)


def _module():
    [path] = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, direction: str) -> None:
    module = _module()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    with e.begin() as conn:
        # The call log as it was before this revision.
        conn.execute(sa.text(
            "CREATE TABLE mcp_call_log (id TEXT PRIMARY KEY, tool TEXT NOT NULL, "
            "repos JSON NOT NULL DEFAULT '[]')"))
    yield e
    e.dispose()


def _cols(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _tables(engine):
    return set(sa.inspect(engine).get_table_names())


def test_it_chains_onto_the_head_before_it():
    module = _module()
    assert module.revision == REVISION and module.down_revision == "d4a7f1c8e3b2"


def test_tables_and_column_arrive_and_leave(engine):
    _run(engine, "upgrade")
    assert set(TABLES) <= _tables(engine)
    assert "packs" in _cols(engine, "mcp_call_log")
    _run(engine, "downgrade")
    assert not set(TABLES) & _tables(engine)
    assert _cols(engine, "mcp_call_log") == {"id", "tool", "repos"}


def test_every_table_holds_the_columns_its_model_reads(engine):
    from src.db import models

    _run(engine, "upgrade")
    for model in (
        models.KnowledgePack, models.KnowledgePackTeam, models.KnowledgePackMember,
        models.KnowledgePackVersion, models.KnowledgeItem, models.KnowledgePackInvite,
        models.KnowledgePackToken,
    ):
        assert {c.name for c in model.__table__.columns} <= _cols(
            engine, model.__tablename__), model.__tablename__
    assert "packs" in {c.name for c in models.McpCallLog.__table__.columns}


def test_the_indexes_and_uniques_the_models_declare_arrive_too(engine):
    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    assert "ix_knowledge_packs_workspace" in {
        i["name"] for i in inspector.get_indexes("knowledge_packs")}
    assert "ix_knowledge_items_version" in {
        i["name"] for i in inspector.get_indexes("knowledge_items")}
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(
        "knowledge_packs")}
    assert ("workspace_id", "slug") in uniques
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(
        "knowledge_pack_versions")}
    assert ("pack_id", "number") in uniques
    assert "ix_knowledge_pack_tokens_pack" in {
        i["name"] for i in inspector.get_indexes("knowledge_pack_tokens")}
    # A token is found by the hash of what the client sends: one row per hash.
    assert any(
        {"token_hash"} == set(u["column_names"]) for u in inspector.get_unique_constraints(
            "knowledge_pack_tokens")) or any(
        i["unique"] and i["column_names"] == ["token_hash"]
        for i in inspector.get_indexes("knowledge_pack_tokens"))


def test_a_second_upgrade_and_a_model_made_database_are_left_alone(engine, tmp_path):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    from src.db.models import Base

    other = sa.create_engine(f"sqlite:///{tmp_path}/models.db")
    Base.metadata.create_all(other)
    _run(other, "upgrade")
    _run(other, "downgrade")
    other.dispose()


def test_a_downgrade_with_nothing_to_remove_does_not_raise(engine):
    _run(engine, "downgrade")
    _run(engine, "downgrade")
