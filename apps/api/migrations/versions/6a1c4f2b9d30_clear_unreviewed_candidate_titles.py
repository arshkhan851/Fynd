"""clear canonical titles copied from unreviewed candidate evidence

Revision ID: 6a1c4f2b9d30
Revises: 5893d90e9c46
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6a1c4f2b9d30"
down_revision: str | None = "5893d90e9c46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLEANUP_SQL = """
UPDATE competitions AS competition
SET title = NULL,
    updated_at = now()
WHERE competition.title IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM field_evidence AS evidence
      WHERE evidence.competition_id = competition.id
        AND evidence.field_name = 'title'
        AND evidence.review_state = 'candidate'
        AND evidence.observed_value ->> 'title' = competition.title
  )
  AND NOT EXISTS (
      SELECT 1
      FROM field_evidence AS selected_evidence
      WHERE selected_evidence.competition_id = competition.id
        AND selected_evidence.field_name = 'title'
        AND selected_evidence.review_state = 'selected'
  )
"""


def upgrade() -> None:
    op.execute(CLEANUP_SQL)


def downgrade() -> None:
    # The prior canonical title cannot be reconstructed reliably after cleanup.
    # Fail explicitly so Alembic cannot report a downgrade while leaving the data
    # cleanup in place and implying that the cleared values were restored.
    raise RuntimeError(
        "This data migration is irreversible: cleared candidate titles cannot be restored."
    )
