"""The Codebase models and revision e5b8c2d9a1f6 agree on the schema, constraint for constraint.

The contract (src/db/models.py "Codebase", alembic/versions/e5b8c2d9a1f6_*):

  * the seven foreign keys of the pack children are declared in the models
    exactly as the migration creates them — `ON DELETE CASCADE`, named by the
    repository's naming convention — so that `alembic revision --autogenerate`
    proposes nothing for the `knowledge_*` tables (it used to propose to drop all
    seven);
  * comparing the migrated database with the models (columns, nullability,
    indexes, unique constraints, foreign keys) finds no difference.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite
from src.db.models import Base

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
REVISION = "e5b8c2d9a1f6"
FOREIGN_KEYS = {
    "fk_knowledge_pack_teams_pack_id_knowledge_packs",
    "fk_knowledge_pack_members_pack_id_knowledge_packs",
    "fk_knowledge_pack_versions_pack_id_knowledge_packs",
    "fk_knowledge_items_version_id_knowledge_pack_versions",
    "fk_knowledge_items_pack_id_knowledge_packs",
    "fk_knowledge_pack_invites_pack_id_knowledge_packs",
    "fk_knowledge_pack_tokens_pack_id_knowledge_packs",
}


def _knowledge_tables():
    return [t for t in Base.metadata.sorted_tables if t.name.startswith("knowledge_")]


def test_the_models_declare_the_seven_cascading_foreign_keys_the_migration_creates():
    seen: dict[str, str] = {}
    for table in _knowledge_tables():
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for name, rule in re.findall(
                r"CONSTRAINT (fk_\w+) FOREIGN KEY\([^)]*\) REFERENCES [^)]*\)([^,\n]*)", ddl):
            seen[name] = rule.strip()
    assert set(seen) == FOREIGN_KEYS
    assert set(seen.values()) == {"ON DELETE CASCADE"}


def test_the_foreign_keys_leave_the_workspace_columns_free_of_any_reference():
    for table in _knowledge_tables():
        for fk in table.foreign_key_constraints:
            assert {c.name for c in fk.columns} <= {"pack_id", "version_id"}, table.name


def _migration():
    [path] = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}_schema", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE mcp_call_log (id TEXT PRIMARY KEY, tool TEXT NOT NULL, "
            "repos JSON NOT NULL DEFAULT '[]')"))
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(
            conn, opts={"target_metadata": Base.metadata}))
        module.upgrade()
    yield engine
    engine.dispose()


def _only_knowledge(obj, name, type_, reflected, compare_to):
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", "")
    return str(table or "").startswith("knowledge_")


def _differences(engine):
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={
            "target_metadata": Base.metadata, "include_object": _only_knowledge,
            "compare_type": False,          # SQLite reflects UUID/JSONB as other types
        })
        return compare_metadata(context, Base.metadata)


def test_the_migrated_database_and_the_models_have_no_difference(migrated):
    assert _differences(migrated) == []


def test_the_migrated_database_cascades_the_way_the_models_say(migrated):
    inspector = sa.inspect(migrated)
    cascading = {
        (table.name, fk.column_keys[0], fk.referred_table.name)
        for table in _knowledge_tables() for fk in table.foreign_key_constraints
    }
    reflected = {
        (name, fk["constrained_columns"][0], fk["referred_table"])
        for name in inspector.get_table_names() if name.startswith("knowledge_")
        for fk in inspector.get_foreign_keys(name)
        if (fk.get("options") or {}).get("ondelete", "").upper() == "CASCADE"
    }
    assert cascading == reflected and len(reflected) == 7
