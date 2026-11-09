"""require empty field keys for scalar review evidence

Revision ID: e3410fa8c27b
Revises: d7e82b64a109
Create Date: 2026-11-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3410fa8c27b"
down_revision: str | None = "d7e82b64a109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_field_evidence_scalar_empty_key"
_EXPRESSION = (
    "field_name NOT IN ('title', 'description', 'eligibility_summary', 'format', "
    "'registration_opens', 'registration_deadline', 'event_starts', 'event_ends', "
    "'event_timezone', 'official_url', 'registration_url') OR field_key = ''"
)


def upgrade() -> None:
    # PostgreSQL NOT VALID avoids scanning/rejecting historical rows during rollout,
    # while enforcing the check on new inserts and updates immediately.
    op.execute(
        f"ALTER TABLE field_evidence ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK ({_EXPRESSION}) NOT VALID"
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "field_evidence", type_="check")
