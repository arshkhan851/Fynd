"""add explicit email auth token consumption timestamp

Revision ID: b2d7e4a18c90
Revises: 6c4bce01b363
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d7e4a18c90"
down_revision: str | None = "6c4bce01b363"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_auth_tokens",
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("email_auth_tokens", "used_at")
