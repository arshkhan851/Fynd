"""add saved competitions

Revision ID: f0a7c2d9416e
Revises: e3410fa8c27b
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a7c2d9416e"
down_revision: str | None = "e3410fa8c27b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_competitions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("competition_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["competition_id"], ["competitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "competition_id"),
    )
    op.create_index(
        "ix_saved_competitions_user_created_at",
        "saved_competitions",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_saved_competitions_user_created_at", table_name="saved_competitions"
    )
    op.drop_table("saved_competitions")
