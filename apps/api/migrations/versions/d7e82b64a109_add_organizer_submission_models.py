"""add organizer submission models

Revision ID: d7e82b64a109
Revises: c28f91a0e4d2
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e82b64a109"
down_revision: str | None = "c28f91a0e4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEE_STATUS_CHECK_EXPRESSION = (
    "(registration_price_status = 'paid' AND registration_fee_amount IS NOT NULL "
    "AND registration_fee_amount > 0 AND registration_fee_currency IS NOT NULL) OR "
    "(registration_price_status IN ('free', 'unknown') "
    "AND registration_fee_amount IS NULL AND registration_fee_currency IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "organizer_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claimed_name", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.String(length=255), nullable=False),
        sa.Column("contact_phone", sa.String(length=50), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column(
            "verification_status",
            sa.String(length=16),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("canonical_organizer_id", sa.BigInteger(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "verification_status IN ('unverified', 'pending', 'verified', 'rejected')",
            name="ck_organizer_profiles_verification_status",
        ),
        sa.CheckConstraint(
            "(verification_status = 'verified' AND canonical_organizer_id IS NOT NULL "
            "AND verified_at IS NOT NULL) OR "
            "(verification_status <> 'verified' AND verified_at IS NULL)",
            name="ck_organizer_profiles_verified_link",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_organizer_id"], ["organizers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_organizer_id"),
    )
    op.create_index(
        "ix_organizer_profiles_verification_status",
        "organizer_profiles",
        ["verification_status"],
    )

    op.create_table(
        "organizer_profile_members",
        sa.Column("organizer_profile_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('owner', 'manager')", name="ck_profile_members_role"),
        sa.ForeignKeyConstraint(
            ["organizer_profile_id"], ["organizer_profiles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organizer_profile_id", "user_id"),
    )
    op.create_index(
        "ix_organizer_profile_members_user_id", "organizer_profile_members", ["user_id"]
    )

    op.create_table(
        "organizer_submissions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("submitter_user_id", sa.Uuid(), nullable=False),
        sa.Column("organizer_profile_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("organizer_name", sa.Text(), nullable=False),
        sa.Column("organizer_email", sa.String(length=255), nullable=False),
        sa.Column("organizer_contact_name", sa.Text(), nullable=True),
        sa.Column("organizer_contact_email", sa.String(length=255), nullable=True),
        sa.Column("organizer_contact_phone", sa.String(length=50), nullable=True),
        sa.Column("official_url", sa.Text(), nullable=False),
        sa.Column("registration_url", sa.Text(), nullable=False),
        sa.Column("registration_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_deadline_on", sa.Date(), nullable=True),
        sa.Column("event_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_starts_on", sa.Date(), nullable=True),
        sa.Column("event_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_ends_on", sa.Date(), nullable=True),
        sa.Column("event_timezone", sa.String(length=64), nullable=True),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("location_name", sa.Text(), nullable=True),
        sa.Column("location_city", sa.Text(), nullable=True),
        sa.Column("location_region", sa.Text(), nullable=True),
        sa.Column("location_country_code", sa.String(length=2), nullable=True),
        sa.Column("registration_price_status", sa.String(length=8), nullable=False),
        sa.Column("registration_fee_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("registration_fee_currency", sa.String(length=3), nullable=True),
        sa.Column("approved_competition_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'under_review', 'approved', 'rejected', 'withdrawn')",
            name="ck_organizer_submissions_status",
        ),
        sa.CheckConstraint(
            "format IN ('online', 'offline', 'hybrid')",
            name="ck_organizer_submissions_format",
        ),
        sa.CheckConstraint(
            "NOT (registration_deadline_at IS NOT NULL AND registration_deadline_on IS NOT NULL)",
            name="ck_organizer_submissions_deadline_precision",
        ),
        sa.CheckConstraint(
            "NOT (event_starts_at IS NOT NULL AND event_starts_on IS NOT NULL)",
            name="ck_organizer_submissions_start_precision",
        ),
        sa.CheckConstraint(
            "NOT (event_ends_at IS NOT NULL AND event_ends_on IS NOT NULL)",
            name="ck_organizer_submissions_end_precision",
        ),
        sa.CheckConstraint(
            "registration_deadline_at IS NOT NULL OR registration_deadline_on IS NOT NULL",
            name="ck_organizer_submissions_deadline_required",
        ),
        sa.CheckConstraint(
            "event_starts_at IS NOT NULL OR event_starts_on IS NOT NULL",
            name="ck_organizer_submissions_start_required",
        ),
        sa.CheckConstraint(
            "event_ends_at IS NOT NULL OR event_ends_on IS NOT NULL",
            name="ck_organizer_submissions_end_required",
        ),
        sa.CheckConstraint(
            "event_starts_at IS NULL OR event_ends_at IS NULL OR event_ends_at >= event_starts_at",
            name="ck_organizer_submissions_timestamp_order",
        ),
        sa.CheckConstraint(
            "event_starts_on IS NULL OR event_ends_on IS NULL OR event_ends_on >= event_starts_on",
            name="ck_organizer_submissions_date_order",
        ),
        sa.CheckConstraint(
            "registration_price_status IN ('free', 'paid', 'unknown')",
            name="ck_organizer_submissions_price_status",
        ),
        sa.CheckConstraint(
            "registration_fee_amount IS NULL OR registration_fee_amount >= 0",
            name="ck_organizer_submissions_fee_nonnegative",
        ),
        sa.CheckConstraint(
            FEE_STATUS_CHECK_EXPRESSION,
            name="ck_organizer_submissions_fee_matches_status",
        ),
        sa.ForeignKeyConstraint(
            ["submitter_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organizer_profile_id"], ["organizer_profiles.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["approved_competition_id"], ["competitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organizer_submissions_submitter_created",
        "organizer_submissions",
        ["submitter_user_id", "created_at"],
    )
    op.create_index(
        "ix_organizer_submissions_status_created",
        "organizer_submissions",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_organizer_submissions_profile_id", "organizer_submissions", ["organizer_profile_id"]
    )

    op.create_table(
        "submission_consents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("submission_id", sa.BigInteger(), nullable=False),
        sa.Column("submitter_user_id", sa.Uuid(), nullable=False),
        sa.Column("submitter_email_snapshot", sa.String(length=255), nullable=False),
        sa.Column("declaration_version", sa.String(length=32), nullable=False),
        sa.Column("declaration_text", sa.Text(), nullable=False),
        sa.Column("declaration_sha256", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["organizer_submissions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submitter_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("submission_id"),
    )

    op.create_table(
        "organizer_submission_categories",
        sa.Column("submission_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["organizer_submissions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("submission_id", "category_id"),
    )
    op.create_index(
        "uq_organizer_submission_primary_category",
        "organizer_submission_categories",
        ["submission_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_index(
        "ix_organizer_submission_categories_category_id",
        "organizer_submission_categories",
        ["category_id"],
    )

    op.create_table(
        "submission_review_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("submission_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("previous_status", sa.String(length=16), nullable=False),
        sa.Column("new_status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "previous_status IN ('pending', 'under_review', 'approved', 'rejected', 'withdrawn')",
            name="ck_submission_review_events_previous_status",
        ),
        sa.CheckConstraint(
            "new_status IN ('pending', 'under_review', 'approved', 'rejected', 'withdrawn')",
            name="ck_submission_review_events_new_status",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["organizer_submissions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_submission_review_events_submission_created",
        "submission_review_events",
        ["submission_id", "created_at"],
    )

    op.add_column(
        "competition_sources",
        sa.Column("organizer_submission_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_competition_sources_organizer_submission_id",
        "competition_sources",
        "organizer_submissions",
        ["organizer_submission_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_competition_sources_organizer_submission_id",
        "competition_sources",
        ["organizer_submission_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_competition_sources_organizer_submission_id",
        table_name="competition_sources",
    )
    op.drop_constraint(
        "fk_competition_sources_organizer_submission_id",
        "competition_sources",
        type_="foreignkey",
    )
    op.drop_column("competition_sources", "organizer_submission_id")

    op.drop_index(
        "ix_submission_review_events_submission_created",
        table_name="submission_review_events",
    )
    op.drop_table("submission_review_events")
    op.drop_index(
        "ix_organizer_submission_categories_category_id",
        table_name="organizer_submission_categories",
    )
    op.drop_index(
        "uq_organizer_submission_primary_category",
        table_name="organizer_submission_categories",
    )
    op.drop_table("organizer_submission_categories")
    op.drop_table("submission_consents")
    op.drop_index(
        "ix_organizer_submissions_profile_id", table_name="organizer_submissions"
    )
    op.drop_index(
        "ix_organizer_submissions_status_created", table_name="organizer_submissions"
    )
    op.drop_index(
        "ix_organizer_submissions_submitter_created", table_name="organizer_submissions"
    )
    op.drop_table("organizer_submissions")
    op.drop_index(
        "ix_organizer_profile_members_user_id", table_name="organizer_profile_members"
    )
    op.drop_table("organizer_profile_members")
    op.drop_index(
        "ix_organizer_profiles_verification_status", table_name="organizer_profiles"
    )
    op.drop_table("organizer_profiles")
