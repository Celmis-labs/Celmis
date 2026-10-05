"""Migration c5d6e7f8a9b0: per-agent prompts are sorted into guidelines and replacements.

Executed against SQLite (like its siblings). Before it, every per-repo agent
prompt REPLACED the built-in one — so the Kodus-style list a team pasted into
"Defect" silently threw the tuned prompt away. After it:

  * the two columns arrive with their empty defaults;
  * a short list with no output contract moves to `agent_prompt_guidelines`
    (now ADDED to the prompt) and leaves `agent_prompt_overrides`;
  * a real prompt (long, a JSON contract, "reasoning", a role) stays a
    replacement, untouched;
  * a guideline someone already wrote for the agent is never overwritten;
  * running it twice is harmless, and the downgrade puts every converted
    entry back where it was, byte for byte, then drops the columns;
  * the id chains off the review-rules migration, the head before it.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

REVISION = "c5d6e7f8a9b0"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_agent_prompt_guidelines.py")
NEW = ("agent_prompt_guidelines", "agent_guidelines_extend")

KODUS_BUG = """- Execution breaks: code that crashes or returns the wrong value
- Logic errors: inverted conditions, off-by-one, wrong operator
- Resource leaks on error paths"""
KODUS_SECURITY = """- Injection: SQL, command and template injection from request data
- Secrets committed in code or config
- Missing authorization checks on new endpoints"""
REAL_PROMPT = ("You are the security reviewer of this repository.\n"
               + "Read every changed line. " * 150
               + '\nOutput a JSON array; each finding starts with "reasoning".')
SHORT_CONTRACT = 'Find bugs. Reply with JSON: [{"reasoning": "...", "file": "..."}]'


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_table(md: sa.MetaData) -> sa.Table:
    """repo_review_policies as a7b8c9d0e1f2 left it: the model minus the new."""
    from src.db.models import RepoReviewPolicy

    return sa.Table("repo_review_policies", md, *[
        sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in RepoReviewPolicy.__table__.columns if c.name not in NEW
    ])


ROWS = {
    # the incident: Kodus-like lists in Defect and Security
    "acme/shop": {"defect": KODUS_BUG, "security": KODUS_SECURITY},
    # a deliberate full prompt, and a short one with an output contract
    "acme/api": {"security": REAL_PROMPT, "defect": SHORT_CONTRACT},
    # mixed
    "acme/web": {"contract": KODUS_BUG, "verifier": REAL_PROMPT},
    "acme/empty": {},
}


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    policy = _legacy_table(md)
    md.create_all(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        for slug, overrides in ROWS.items():
            conn.execute(policy.insert().values(
                repo_slug=slug, workspace_id="ws-1", enabled=True,
                prompt_template="", folder_rules=[], agent_prompt_overrides=overrides,
                mcp_sources=[], created_at=now, updated_at=now))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("repo_review_policies")}


def _rows(engine) -> dict[str, dict]:
    cols = [c for c in ("agent_prompt_overrides", *NEW) if c in _columns(engine)]
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            f"SELECT repo_slug, {', '.join(cols)} FROM repo_review_policies")).mappings().all()
    out = {}
    for r in rows:
        out[r["repo_slug"]] = {
            c: (json.loads(r[c]) if isinstance(r[c], str) else r[c]) for c in cols}
    return out


def test_it_chains_off_the_review_rules_migration():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "a7b8c9d0e1f2"


def test_the_columns_arrive_with_empty_defaults(engine):
    _run(engine, "upgrade")
    columns = _columns(engine)
    for name in NEW:
        assert name in columns
        assert not columns[name]["nullable"], name
    assert _rows(engine)["acme/empty"]["agent_guidelines_extend"] == []


def test_kodus_like_lists_become_guidelines(engine):
    _run(engine, "upgrade")
    row = _rows(engine)["acme/shop"]
    assert row["agent_prompt_overrides"] == {}
    assert row["agent_prompt_guidelines"] == {
        "defect": KODUS_BUG.strip(), "security": KODUS_SECURITY.strip()}


def test_real_prompts_stay_replacements(engine):
    _run(engine, "upgrade")
    row = _rows(engine)["acme/api"]
    assert row["agent_prompt_overrides"] == ROWS["acme/api"]
    assert row["agent_prompt_guidelines"] == {}


def test_a_mixed_row_is_sorted_per_agent(engine):
    _run(engine, "upgrade")
    row = _rows(engine)["acme/web"]
    assert row["agent_prompt_overrides"] == {"verifier": REAL_PROMPT}
    assert row["agent_prompt_guidelines"] == {"contract": KODUS_BUG.strip()}


def test_running_it_twice_changes_nothing(engine):
    _run(engine, "upgrade")
    once = _rows(engine)
    _run(engine, "upgrade")
    assert _rows(engine) == once


def test_the_downgrade_restores_every_override_exactly(engine):
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert not set(NEW) & set(_columns(engine))
    for slug, overrides in ROWS.items():
        assert _rows(engine)[slug]["agent_prompt_overrides"] == {
            k: v.strip() for k, v in overrides.items()}, slug


def test_the_downgrade_keeps_a_replacement_over_a_later_guideline(engine):
    """A guideline written after the upgrade for an agent that also has a
    replacement cannot live in the old schema: the replacement is kept."""
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE repo_review_policies SET agent_prompt_guidelines = :g "
            "WHERE repo_slug = 'acme/api'"),
            {"g": json.dumps({"security": "- later guideline", "contract": "- new"})})
    _run(engine, "downgrade")
    row = _rows(engine)["acme/api"]
    assert row["agent_prompt_overrides"]["security"] == REAL_PROMPT
    assert row["agent_prompt_overrides"]["contract"] == "- new"


def test_the_model_declares_the_columns_the_migration_adds():
    from src.db.models import RepoReviewPolicy

    for name in NEW:
        assert name in RepoReviewPolicy.__table__.columns
