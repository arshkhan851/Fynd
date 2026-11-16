"""add persistent email delivery reservation state

Revision ID: f82a6c1d903e
Revises: c4b71a9d2e63
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f82a6c1d903e"
down_revision: str | None = "c4b71a9d2e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_email_delivery_state",
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("identifier_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("last_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("reservation_state", sa.String(length=8), nullable=True),
        sa.Column(
            "reservation_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(key_id) > 0", name="ck_auth_email_delivery_state_key_id"
        ),
        sa.CheckConstraint(
            "octet_length(identifier_digest) = 32",
            name="ck_auth_email_delivery_state_digest_length",
        ),
        sa.CheckConstraint(
            "reservation_state IS NULL OR reservation_state IN ('reserved', 'sending')",
            name="ck_auth_email_delivery_state_reservation_state",
        ),
        sa.CheckConstraint(
            "(reservation_id IS NULL AND reservation_state IS NULL "
            "AND reservation_expires_at IS NULL) OR "
            "(reservation_id IS NOT NULL AND reservation_state IS NOT NULL "
            "AND reservation_state IN ('reserved', 'sending') "
            "AND reservation_expires_at IS NOT NULL)",
            name="ck_auth_email_delivery_state_reservation_fields",
        ),
        sa.PrimaryKeyConstraint("key_id", "identifier_digest"),
    )
    op.create_index(
        "ix_auth_email_delivery_state_cleanup",
        "auth_email_delivery_state",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_email_delivery_state_cleanup",
        table_name="auth_email_delivery_state",
    )
    op.drop_table("auth_email_delivery_state")
