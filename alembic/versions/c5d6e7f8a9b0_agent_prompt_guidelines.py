"""agent prompt guidelines: per-agent text ADDED to the prompt, apart from replacements

Until now a repository's per-agent prompt (`agent_prompt_overrides`) REPLACED
the agent's whole built-in system prompt. Teams pasted short lists of what to
look for into those boxes and silently lost the built-in prompt's severity
calibration, changed-lines-only scope, avoid-list and evidence demands.

1. `repo_review_policies.agent_prompt_guidelines` (JSON, NOT NULL, '{}') —
   {agent: text}, at most 2000 characters each, appended to the agent's
   prompt in a delimited "Team guidelines" block.
2. `repo_review_policies.agent_guidelines_extend` (JSON, NOT NULL, '[]') —
   the agents whose repository guidelines are added to the workspace's
   instead of replacing them (explicit opt-in; the default replaces, as in
   Kodus).
3. Every existing override is sorted, per agent, by `_classify` below:
     * GUIDELINES when it is at most 2000 characters, carries no output
       contract (no JSON, no "reasoning"/"severity"/"file" field, no "reply
       with …"/"output format") and does not open with a role statement
       ("You are …", "Act as …") — moved from `agent_prompt_overrides` to
       `agent_prompt_guidelines`;
     * REPLACEMENT otherwise — left where it is, behaving exactly as before.
   Each converted repository/agent is logged (slugs and agent names, never
   the text) — the audit trail of which prompts changed meaning.

The workspace layer (/review-settings → Global, stored encrypted in the
credential store, outside this database) is sorted by the same rule in
`src.api.routers.agents.migrate_workspace_prompt_overrides`, once per
installation at API start, and reverted with `python -m
src.review.prompt_guidelines_cli revert`.

Downgrade: every guideline goes back into `agent_prompt_overrides` for an
agent that has no override there — which restores each converted entry
exactly — and the two columns are dropped. A guideline written after the
upgrade for an agent that ALSO has a replacement cannot be represented by the
old schema and is dropped (logged).

The heuristic is a frozen copy of `src.review.prompt_guidelines.
classify_legacy_override` (a migration must not change meaning when the
application does); a test holds the two together.

Revision ID: c5d6e7f8a9b0
Revises: a7b8c9d0e1f2
"""

from __future__ import annotations

import json
import logging
import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c5d6e7f8a9b0"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

_TABLE = "repo_review_policies"
_GUIDELINES = "agent_prompt_guidelines"
_EXTEND = "agent_guidelines_extend"

logger = logging.getLogger("alembic.runtime.migration")

# ─── the frozen heuristic ────────────────────────────────────────────

_MAX_CHARS = 2000
_CONTRACT_MARKERS = re.compile(
    r'"reasoning"|\breasoning\s*:|"severity"|"file"\s*:|"line"\s*:'
    r"|\bjson\b|output format|\breply (?:exactly|with|only)\b"
    r"|\brespond (?:only )?with\b|\breturn (?:only )?(?:a|an|the)? ?(?:json|array)\b",
    re.IGNORECASE,
)
_ROLE_OPENING = re.compile(
    r"^\s*(?:#+\s*)?(?:you are|you're|act as|your (?:role|job|task) is)\b",
    re.IGNORECASE,
)


def _classify(text: object) -> str:
    if not isinstance(text, str) or not text.strip():
        return "empty"
    body = text.strip()
    if len(body) > _MAX_CHARS:
        return "replace"
    if _CONTRACT_MARKERS.search(body) or _ROLE_OPENING.search(body):
        return "replace"
    return "guidelines"


# ─── helpers ─────────────────────────────────────────────────────────


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _json_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _as_dict(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _policies(*columns: str) -> sa.TableClause:
    jt = _json_type()
    return sa.table(_TABLE, sa.column("repo_slug", sa.Text()),
                    *(sa.column(c, jt) for c in columns))


# ─── upgrade / downgrade ─────────────────────────────────────────────


def upgrade() -> None:
    # Spelled as literals, not the constants: the migration-chain test reads
    # every `op.add_column` to prove each model column has a migration.
    if not _has_column(_TABLE, _GUIDELINES):
        op.add_column("repo_review_policies", sa.Column(
            "agent_prompt_guidelines", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default="{}"))
    if not _has_column(_TABLE, _EXTEND):
        op.add_column("repo_review_policies", sa.Column(
            "agent_guidelines_extend", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default="[]"))

    bind = op.get_bind()
    policies = _policies("agent_prompt_overrides", _GUIDELINES)
    rows = bind.execute(sa.select(
        policies.c.repo_slug, policies.c.agent_prompt_overrides,
        policies.c[_GUIDELINES])).all()
    converted_total = 0
    for slug, raw_overrides, raw_guidelines in rows:
        overrides = _as_dict(raw_overrides)
        guidelines = _as_dict(raw_guidelines)
        moved: list[str] = []
        kept: list[str] = []
        for agent, text in list(overrides.items()):
            verdict = _classify(text)
            if verdict == "guidelines" and not str(guidelines.get(agent) or "").strip():
                guidelines[agent] = str(text).strip()
                del overrides[agent]
                moved.append(agent)
            elif verdict == "replace":
                kept.append(agent)
        if not moved:
            if kept:
                logger.info("agent_prompt_guidelines repo=%s kept_replacement=%s",
                            slug, ",".join(sorted(kept)))
            continue
        bind.execute(
            policies.update()
            .where(policies.c.repo_slug == slug)
            .values({"agent_prompt_overrides": overrides, _GUIDELINES: guidelines}))
        converted_total += len(moved)
        logger.warning(
            "agent_prompt_guidelines repo=%s converted_to_guidelines=%s "
            "kept_replacement=%s — these overrides read as guideline lists and "
            "are now ADDED to the built-in prompt",
            slug, ",".join(sorted(moved)), ",".join(sorted(kept)) or "-")
    logger.info("agent_prompt_guidelines converted=%d repositories=%d",
                converted_total, len(rows))


def downgrade() -> None:
    if _has_column(_TABLE, _GUIDELINES):
        bind = op.get_bind()
        policies = _policies("agent_prompt_overrides", _GUIDELINES)
        rows = bind.execute(sa.select(
            policies.c.repo_slug, policies.c.agent_prompt_overrides,
            policies.c[_GUIDELINES])).all()
        for slug, raw_overrides, raw_guidelines in rows:
            overrides = _as_dict(raw_overrides)
            guidelines = _as_dict(raw_guidelines)
            if not guidelines:
                continue
            dropped: list[str] = []
            for agent, text in guidelines.items():
                if not str(text or "").strip():
                    continue
                if str(overrides.get(agent) or "").strip():
                    dropped.append(agent)
                    continue
                overrides[agent] = str(text).strip()
            bind.execute(
                policies.update()
                .where(policies.c.repo_slug == slug)
                .values({"agent_prompt_overrides": overrides}))
            if dropped:
                logger.warning(
                    "agent_prompt_guidelines_downgrade repo=%s dropped=%s — "
                    "these agents also had a replacement prompt; the old "
                    "schema holds one text per agent", slug, ",".join(sorted(dropped)))

    present = [c for c in (_GUIDELINES, _EXTEND) if _has_column(_TABLE, c)]
    if present:
        # batch_alter_table so SQLite rebuilds the table; on Postgres it is a
        # plain ALTER TABLE … DROP COLUMN per column.
        with op.batch_alter_table(_TABLE) as batch:
            for column in present:
                batch.drop_column(column)
