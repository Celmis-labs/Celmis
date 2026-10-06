"""Migration f1a2b3c4d5e6: the 2.3.0 review settings, at both layers.

Executed against SQLite (like its siblings) for what is not visible as text:

  * every new column arrives NULLABLE with no server default, on both
    tables — and a row that predates them reads NULL everywhere, which the
    resolver turns into exactly the built-ins (no behaviour change);
  * running it twice is harmless, and it reverses: the columns leave and the
    rows they were added to keep everything else;
  * the id is the one the rules migration chains off.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from src.review.review_defaults import (
    BUILTIN_DEFAULTS,
    INHERITABLE_FIELDS,
    install_defaults,
    resolve,
)
from src.review.settings import ReviewSettings

# The fields THIS migration added: from `enabled_agents` to the last of the 2.3.0
# settings. Later migrations append to INHERITABLE_FIELDS, so the end is fixed
# here instead of following the list.
V23_FIELDS: tuple[str, ...] = INHERITABLE_FIELDS[
    INHERITABLE_FIELDS.index("enabled_agents"):
    INHERITABLE_FIELDS.index("message_finished_header") + 1
]
# Columns that arrive in LATER migrations; the legacy table has none of them
# either, and this migration must leave them alone.
LATER_FIELDS: tuple[str, ...] = INHERITABLE_FIELDS[
    INHERITABLE_FIELDS.index("message_finished_header") + 1:
]

REVISION = "f1a2b3c4d5e6"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_review_settings_v23.py"
)
MODEL_COLUMNS = ("performance_model", "business_logic_model")
#: Settings appended to `V23_FIELDS` by later migrations (d6a1c3e5f701 …); this
#: migration did not add them.
_LATER = ("completed_comment", "commands_guide_enabled", "review_cadence",
          "auto_pause_pushes", "auto_pause_window_minutes", "ignored_title_keywords",
          "review_scope", "commands_enabled", "chat_enabled", "command_permission",
          # d2a7c9e1f367, the Jira task context:
          "task_context_enabled", "task_project_keys", "task_acceptance_field",
          "task_include_comments", "business_logic_auto")
_AT_V23 = tuple(n for n in V23_FIELDS if n not in _LATER)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _server_default(c):
    """The model column's server default, carried into the legacy table: a
    NOT NULL column a later migration added with a default (the agent
    guidelines, c5d6e7f8a9b0) must not refuse the legacy insert below."""
    return c.server_default.arg if c.server_default is not None else None


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy(table: str) -> list[sa.Column]:
    """The table as e4c8a1f7b2d9 left it: the model's columns minus the new."""
    from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults

    model = {"repo_review_policies": RepoReviewPolicy,
             "workspace_review_defaults": WorkspaceReviewDefaults}[table]
    return [
        sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable,
                  server_default=_server_default(c))
        for c in model.__table__.columns
        if c.name not in (*V23_FIELDS, *MODEL_COLUMNS, *LATER_FIELDS)
    ]


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    policy = sa.Table("repo_review_policies", md, *_legacy("repo_review_policies"))
    defaults = sa.Table("workspace_review_defaults", md,
                        *_legacy("workspace_review_defaults"))
    md.create_all(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        conn.execute(policy.insert().values(
            repo_slug="acme/api", workspace_id="ws-1", enabled=True,
            prompt_template="rules", folder_rules=[], agent_prompt_overrides={},
            mcp_sources=[], disabled_agents=["cve"], summary_enabled=False,
            created_at=now, updated_at=now))
        conn.execute(defaults.insert().values(
            workspace_id="ws-1", disabled_agents=["structural"],
            summary_instructions="be brief", created_at=now, updated_at=now))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def _row(engine, table: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(sa.text(f"SELECT * FROM {table}")).mappings().one())


def test_it_has_the_id_the_rules_migration_chains_off():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "e4c8a1f7b2d9"


def test_every_column_arrives_nullable_on_both_tables(engine):
    _run(engine, "upgrade")
    for table, extra in (("repo_review_policies", MODEL_COLUMNS),
                         ("workspace_review_defaults", ())):
        columns = _columns(engine, table)
        for name in (*_AT_V23, *extra):
            assert name in columns, f"{table}.{name}"
            assert columns[name]["nullable"], f"{table}.{name}"
            assert columns[name]["default"] is None, f"{table}.{name}"
    assert "performance_model" not in _columns(engine, "workspace_review_defaults")


def test_existing_rows_behave_exactly_as_before(engine):
    _run(engine, "upgrade")
    policy = _row(engine, "repo_review_policies")
    defaults = _row(engine, "workspace_review_defaults")
    assert all(policy[n] is None for n in (*_AT_V23, *MODEL_COLUMNS))
    assert all(defaults[n] is None for n in _AT_V23)
    # What was there stays there.
    assert policy["prompt_template"] == "rules"
    assert defaults["summary_instructions"] == "be brief"
    # And NULL resolves to the built-in of every new key.
    values, sources = resolve(policy, {n: defaults[n] for n in _AT_V23},
                              install_defaults(ReviewSettings()))
    for name in _AT_V23:
        assert values[name] == BUILTIN_DEFAULTS[name], name
        assert sources[name] == "install", name


def test_it_is_idempotent_and_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE repo_review_policies SET run_on_drafts = 1, "
            "base_instruction = 'x', performance_model = 'm'"))
    _run(engine, "downgrade")
    for table in ("repo_review_policies", "workspace_review_defaults"):
        columns = _columns(engine, table)
        for name in (*_AT_V23, *MODEL_COLUMNS):
            assert name not in columns, f"{table}.{name}"
    assert _row(engine, "repo_review_policies")["prompt_template"] == "rules"
    assert _row(engine, "workspace_review_defaults")["summary_instructions"] == "be brief"
    _run(engine, "downgrade")              # again: nothing left to drop
    _run(engine, "upgrade")                # and back up
    assert "run_on_drafts" in _columns(engine, "repo_review_policies")
