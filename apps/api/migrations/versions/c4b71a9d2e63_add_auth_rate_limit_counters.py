"""add persistent auth rate-limit counters

Revision ID: c4b71a9d2e63
Revises: f0a7c2d9416e
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4b71a9d2e63"
down_revision: str | None = "f0a7c2d9416e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limit_counters",
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("identifier_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('email', 'ip')", name="ck_auth_rate_limit_counters_scope"
        ),
        sa.CheckConstraint(
            "length(key_id) > 0", name="ck_auth_rate_limit_counters_key_id"
        ),
        sa.CheckConstraint(
            "octet_length(identifier_digest) = 32",
            name="ck_auth_rate_limit_counters_digest_length",
        ),
        sa.CheckConstraint(
            "request_count > 0", name="ck_auth_rate_limit_counters_positive_count"
        ),
        sa.PrimaryKeyConstraint(
            "scope", "key_id", "identifier_digest", "window_start"
        ),
    )
    op.create_index(
        "ix_auth_rate_limit_counters_window_start",
        "auth_rate_limit_counters",
        ["window_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_rate_limit_counters_window_start",
        table_name="auth_rate_limit_counters",
    )
    op.drop_table("auth_rate_limit_counters")
