"""add research workspaces (staging) and paper.staged

Revision ID: 7b3c9d1e5f20
Revises: 4e1a94ce0d93
Create Date: 2026-11-26 19:20:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b3c9d1e5f20"
down_revision: str | None = "4e1a94ce0d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper",
        sa.Column("staged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_paper_staged",
        "paper",
        ["staged"],
        postgresql_where=sa.text("staged"),
    )

    op.create_table(
        "research_workspace",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_research_workspace_name_active",
        "research_workspace",
        ["name"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "research_workspace_paper",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["research_workspace.id"]),
        sa.PrimaryKeyConstraint("workspace_id", "paper_id"),
    )
    op.create_index(
        "ix_research_workspace_paper_paper",
        "research_workspace_paper",
        ["paper_id"],
    )
    op.create_index(
        "ix_research_workspace_paper_position",
        "research_workspace_paper",
        ["workspace_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_workspace_paper_position", table_name="research_workspace_paper")
    op.drop_index("ix_research_workspace_paper_paper", table_name="research_workspace_paper")
    op.drop_table("research_workspace_paper")
    op.drop_index("uq_research_workspace_name_active", table_name="research_workspace")
    op.drop_table("research_workspace")
    op.drop_index("ix_paper_staged", table_name="paper")
    op.drop_column("paper", "staged")
