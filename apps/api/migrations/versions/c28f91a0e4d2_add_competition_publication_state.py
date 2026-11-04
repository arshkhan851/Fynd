"""add explicit competition publication state

Revision ID: c28f91a0e4d2
Revises: b2d7e4a18c90
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c28f91a0e4d2"
down_revision: str | None = "b2d7e4a18c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitions",
        sa.Column(
            "is_published",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    # Publish only records with a canonical title and explicit human-selected
    # title evidence. Titles without that provenance stay hidden for review.
    op.execute(
        sa.text(
            """
            UPDATE competitions AS competition
            SET is_published = true
            WHERE competition.title IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM field_evidence AS evidence
                  WHERE evidence.competition_id = competition.id
                    AND evidence.field_name = 'title'
                    AND evidence.review_state = 'selected'
                    AND evidence.observed_value ->> 'title' = competition.title
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("competitions", "is_published")
