"""stage21_chats_workspace

Revision ID: e4a2c7b9f103
Revises: d1b7f38a4e51
Create Date: 2026-07-18 09:00:00.000000

Stage 21 — workspace isolation for chats (Q&A history).
`workspace_id` with server_default 'default' keeps existing rows valid.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4a2c7b9f103"
down_revision: str | Sequence[str] | None = "d1b7f38a4e51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column(
        "workspace_id", sa.Text(),
        nullable=False, server_default="default",
    ))
    op.create_index("ix_chats_workspace", "chats", ["workspace_id"])
    # compliance_checks missed the Stage 19 workspace_id sweep.
    op.add_column("compliance_checks", sa.Column(
        "workspace_id", sa.Text(),
        nullable=False, server_default="default",
    ))
    op.create_index("ix_compliance_workspace", "compliance_checks", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_compliance_workspace", table_name="compliance_checks")
    op.drop_column("compliance_checks", "workspace_id")
    op.drop_index("ix_chats_workspace", table_name="chats")
    op.drop_column("chats", "workspace_id")
