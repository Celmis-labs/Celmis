"""issues backlog: what became of an issue once its PR merged, and its auto-resolve

Until now an issue lived and died with its pull request: a merge changed
nothing, so "found by the review, merged anyway" was a count and not a fate,
and nobody could say whether a suggestion had ever been implemented.

1. `review_issues` gains NULLABLE columns, no server defaults:
   `base_ref`, `merged_at` (together: "backlog" = open + merged_at set),
   `close_outcome` (implemented | unimplemented | dismissed | abandoned,
   frozen at merge or close), `dup_of` (the backlog issue another PR's row
   repeats), `snippet`, `last_checked_at`, `last_checked_sha`,
   `last_checked_blob`, `last_verified_blob`, `fixed_by_pr_number`,
   `fixed_by_pr_url`, `resolution_note`; and two indexes
   (`ix_review_issues_backlog`, `ix_review_issues_dup_of`).
2. `review_issue_recheck_state`: where the last recheck of one branch stopped.
3. Four policy columns on BOTH `repo_review_policies` and
   `workspace_review_defaults` (NULL = inherit): `issues_auto_resolve`,
   `issues_resolve_llm_verify`, `issues_resolve_max_llm`,
   `issues_announce_resolved`.
4. A guarded backfill: issues of PRs already recorded as merged get
   `merged_at` / `base_ref` and the outcome their status implies; issues a
   closed PR resolved become `abandoned`. A PR row without a base branch
   leaves `base_ref` NULL (the issue is then not backlog until the PR is
   reviewed again); an issue with no PR row stays untouched.

Idempotent both ways (every add / create / drop is guarded by an inspection)
and literal `op.add_column` calls over literal tuples only — the shape
tests/db/test_migration_chain.py can read. Downgrade drops everything this
added; the backfill is data derived from columns that stay, so it needs no
reverse.

Revision ID: b0e5a7c9d145
Revises: f8c3e5a7b923
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b0e5a7c9d145"
# This lane branched off before the other steps' migrations: the integrator
# re-chains it behind a9d4f6b8c034 (plan §1) once the lanes are merged.
down_revision = "f8c3e5a7b923"
branch_labels = None
depends_on = None

_ISSUES = "review_issues"
_STATE = "review_issue_recheck_state"
_POLICY = "repo_review_policies"
_DEFAULTS = "workspace_review_defaults"

_ISSUE_TEXT = (
    "base_ref", "close_outcome", "dup_of", "snippet", "last_checked_sha",
    "last_checked_blob", "last_verified_blob", "fixed_by_pr_url",
    "resolution_note",
)
_ISSUE_TIME = ("merged_at", "last_checked_at")
_ISSUE_INT = ("fixed_by_pr_number",)
_BOOL_COLUMNS = (
    "issues_auto_resolve", "issues_resolve_llm_verify", "issues_announce_resolved",
)
_INT_COLUMNS = ("issues_resolve_max_llm",)
_INDEXES = (
    ("ix_review_issues_backlog", ["workspace_id", "repo_slug", "base_ref", "status"]),
    ("ix_review_issues_dup_of", ["dup_of"]),
)


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return name in {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    # The tuples are spelled inline (not the module constants) so the
    # migration-chain test can bind each loop variable to its literal names.
    for text_column in (
        "base_ref", "close_outcome", "dup_of", "snippet", "last_checked_sha",
        "last_checked_blob", "last_verified_blob", "fixed_by_pr_url",
        "resolution_note",
    ):
        if not _has_column("review_issues", text_column):
            op.add_column("review_issues", sa.Column(text_column, sa.Text(), nullable=True))
    for time_column in ("merged_at", "last_checked_at"):
        if not _has_column("review_issues", time_column):
            op.add_column("review_issues", sa.Column(
                time_column, sa.DateTime(timezone=True), nullable=True))
    if not _has_column("review_issues", "fixed_by_pr_number"):
        op.add_column("review_issues", sa.Column(
            "fixed_by_pr_number", sa.Integer(), nullable=True))

    for name, columns in _INDEXES:
        if not _has_index(_ISSUES, name):
            op.create_index(name, _ISSUES, columns)

    if not _has_table(_STATE):
        op.create_table(
            "review_issue_recheck_state",
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("pr_provider", sa.Text(), nullable=False),
            sa.Column("pr_repo", sa.Text(), nullable=False),
            sa.Column("base_ref", sa.Text(), nullable=False),
            sa.Column("last_head_sha", sa.Text(), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_result", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("llm_calls_total", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.PrimaryKeyConstraint("workspace_id", "pr_provider", "pr_repo", "base_ref"),
        )

    for table in ("repo_review_policies", "workspace_review_defaults"):
        for bool_column in (
            "issues_auto_resolve", "issues_resolve_llm_verify", "issues_announce_resolved",
        ):
            if not _has_column(table, bool_column):
                op.add_column(table, sa.Column(bool_column, sa.Boolean(), nullable=True))
        if not _has_column(table, "issues_resolve_max_llm"):
            op.add_column(table, sa.Column(
                "issues_resolve_max_llm", sa.Integer(), nullable=True))

    _backfill()


def _backfill() -> None:
    """Fill the backlog columns from the PR rows, once.

    Only rows whose `merged_at` / `close_outcome` are still NULL are touched,
    so a second run (or a row the application already wrote) is left alone.
    Portable SQL: correlated sub-selects, no UPDATE ... FROM.
    """
    bind = op.get_bind()
    if not (_has_table("review_pull_requests") and _has_table(_ISSUES)):
        return
    same_pr = (
        "p.workspace_id = review_issues.workspace_id "
        "AND p.provider = review_issues.pr_provider "
        "AND p.repo = review_issues.pr_repo "
        "AND p.number = review_issues.pr_number"
    )
    bind.execute(sa.text(
        "UPDATE review_issues SET "
        f"merged_at = (SELECT p.closed_at FROM review_pull_requests p WHERE {same_pr}), "
        f"base_ref = (SELECT p.base_ref FROM review_pull_requests p WHERE {same_pr}) "
        "WHERE merged_at IS NULL AND EXISTS ("
        f"SELECT 1 FROM review_pull_requests p WHERE {same_pr} AND p.state = 'merged')"
    ))
    bind.execute(sa.text(
        "UPDATE review_issues SET close_outcome = CASE status "
        "WHEN 'fixed' THEN 'implemented' "
        "WHEN 'open' THEN 'unimplemented' "
        "WHEN 'dismissed' THEN 'dismissed' ELSE NULL END "
        "WHERE close_outcome IS NULL AND EXISTS ("
        f"SELECT 1 FROM review_pull_requests p WHERE {same_pr} AND p.state = 'merged')"
    ))
    bind.execute(sa.text(
        "UPDATE review_issues SET close_outcome = 'abandoned' "
        "WHERE close_outcome IS NULL AND status = 'resolved' "
        "AND resolution_source = 'pr_closed' AND EXISTS ("
        f"SELECT 1 FROM review_pull_requests p WHERE {same_pr} AND p.state = 'closed')"
    ))


def downgrade() -> None:
    for table in (_POLICY, _DEFAULTS):
        present = [
            c for c in (*_BOOL_COLUMNS, *_INT_COLUMNS) if _has_column(table, c)
        ]
        if present:
            with op.batch_alter_table(table) as batch:
                for column in present:
                    batch.drop_column(column)

    if _has_table(_STATE):
        op.drop_table(_STATE)

    for name, _columns in _INDEXES:
        if _has_index(_ISSUES, name):
            op.drop_index(name, table_name=_ISSUES)
    present = [
        c for c in (*_ISSUE_TEXT, *_ISSUE_TIME, *_ISSUE_INT) if _has_column(_ISSUES, c)
    ]
    if present:
        with op.batch_alter_table(_ISSUES) as batch:
            for column in present:
                batch.drop_column(column)
