"""add evidence review events and trusted source identity

Revision ID: b6e2a4c91d73
Revises: f82a6c1d903e
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2a4c91d73"
down_revision: str | None = "f82a6c1d903e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competition_sources",
        sa.Column("registry_source_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_competition_sources_registry_source_id",
        "competition_sources",
        ["registry_source_id"],
        unique=False,
    )
    op.create_table(
        "evidence_review_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("previous_state", sa.String(length=16), nullable=False),
        sa.Column("new_state", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(action = 'accepted' AND previous_state = 'candidate' AND new_state = 'selected') "
            "OR (action = 'rejected' AND previous_state = 'candidate' AND new_state = 'rejected') "
            "OR (action = 'superseded' AND previous_state = 'selected' "
            "AND new_state = 'superseded')",
            name="ck_evidence_review_events_transition",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["field_evidence.id"],
            name="fk_evidence_review_events_evidence_id_field_evidence",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name="fk_evidence_review_events_reviewer_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_review_events"),
    )
    op.create_index(
        "ix_evidence_review_events_evidence_created",
        "evidence_review_events",
        ["evidence_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_review_events_reviewer_created",
        "evidence_review_events",
        ["reviewer_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_review_events_reviewer_created", table_name="evidence_review_events")
    op.drop_index("ix_evidence_review_events_evidence_created", table_name="evidence_review_events")
    op.drop_table("evidence_review_events")
    op.drop_index("ix_competition_sources_registry_source_id", table_name="competition_sources")
    op.drop_column("competition_sources", "registry_source_id")
