"""Migration a9d4f6b8c034: the comment-command settings and the command ledger.

Three nullable policy columns on both tables (NULL = inherit), the
`pr_command_events` table with the unique key that makes a comment handled
once, an idempotent upgrade, and a downgrade that drops exactly what was added.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults

REVISION = "a9d4f6b8c034"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_pr_commands.py")

POLICY_COLUMNS = ("commands_enabled", "chat_enabled", "command_permission")
TABLES = {"repo_review_policies": RepoReviewPolicy,
          "workspace_review_defaults": WorkspaceReviewDefaults}


def _migration():
    spec = importlib.util.spec_from_file_location("m_a9d4f6b8c034", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy(table: str) -> list[sa.Column]:
    """The table as f9d4a6c8e134 left it: the model's columns minus the new."""
    return [
        sa.Column(c.name, sa.JSON() if "JSON" in type(c.type).__name__.upper() else c.type,
                  primary_key=c.primary_key, nullable=c.nullable,
                  server_default=sa.text("''") if c.name == "prompt_template" else None)
        for c in TABLES[table].__table__.columns if c.name not in POLICY_COLUMNS
    ]


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    for table in TABLES:
        sa.Table(table, md, *_legacy(table))
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


def _event(**over) -> dict:
    row = {"id": "1", "workspace_id": "ws", "provider": "github", "repo": "acme/shop",
           "pr_number": 7, "comment_id": "c1", "command": "review"}
    return {**row, **over}


def test_it_chains_off_the_previous_revision():
    module = _migration()
    assert (module.revision, module.down_revision) == (REVISION, "f9d4a6c8e134")


def test_the_policy_columns_arrive_nullable_on_both_tables(engine):
    _run(engine, "upgrade")
    for table in TABLES:
        columns = _columns(engine, table)
        for name in POLICY_COLUMNS:
            assert name in columns and columns[name]["nullable"], f"{table}.{name}"
            assert columns[name]["default"] is None, f"{table}.{name}: NULL means inherit"


def test_the_ledger_table_arrives_with_the_columns_the_model_declares(engine):
    from src.db.models import PRCommandEvent

    _run(engine, "upgrade")
    assert set(_columns(engine, "pr_command_events")) == {
        c.name for c in PRCommandEvent.__table__.columns}


def test_a_comment_can_be_claimed_only_once(engine):
    _run(engine, "upgrade")
    insert = sa.text(
        "INSERT INTO pr_command_events (id, workspace_id, provider, repo, pr_number, "
        "comment_id, command) VALUES (:id, :workspace_id, :provider, :repo, :pr_number, "
        ":comment_id, :command)")
    with engine.begin() as conn:
        conn.execute(insert, _event())
        conn.execute(insert, _event(id="2", workspace_id="other"))  # another workspace: fine
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(insert, _event(id="3"))


def test_upgrading_twice_changes_nothing(engine):
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO pr_command_events (id, workspace_id, provider, repo, pr_number, "
            "comment_id, command) VALUES ('1', 'ws', 'github', 'acme/shop', 7, 'c1', 'review')"))
    _run(engine, "upgrade")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM pr_command_events")).scalar_one() == 1


def test_downgrade_drops_what_was_added(engine):
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert not sa.inspect(engine).has_table("pr_command_events")
    for table in TABLES:
        assert not [n for n in POLICY_COLUMNS if n in _columns(engine, table)], table
    _run(engine, "downgrade")  # again: nothing left to drop
    _run(engine, "upgrade")
    assert "commands_enabled" in _columns(engine, "repo_review_policies")
