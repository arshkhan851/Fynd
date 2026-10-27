from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Competition(Base):
    __tablename__ = "competitions"
    __table_args__ = (
        CheckConstraint(
            "format IS NULL OR format IN ('online', 'offline', 'hybrid')",
            name="ck_competitions_format",
        ),
        CheckConstraint(
            "NOT (registration_opens_at IS NOT NULL AND registration_opens_on IS NOT NULL)",
            name="ck_competitions_registration_opens_precision",
        ),
        CheckConstraint(
            "NOT (registration_deadline_at IS NOT NULL AND registration_deadline_on IS NOT NULL)",
            name="ck_competitions_registration_deadline_precision",
        ),
        CheckConstraint(
            "NOT (event_starts_at IS NOT NULL AND event_starts_on IS NOT NULL)",
            name="ck_competitions_event_starts_precision",
        ),
        CheckConstraint(
            "NOT (event_ends_at IS NOT NULL AND event_ends_on IS NOT NULL)",
            name="ck_competitions_event_ends_precision",
        ),
        CheckConstraint(
            "event_starts_at IS NULL OR event_ends_at IS NULL "
            "OR event_ends_at >= event_starts_at",
            name="ck_competitions_event_timestamp_order",
        ),
        CheckConstraint(
            "event_starts_on IS NULL OR event_ends_on IS NULL "
            "OR event_ends_on >= event_starts_on",
            name="ck_competitions_event_date_order",
        ),
        Index("ix_competitions_event_starts_at", "event_starts_at"),
        Index("ix_competitions_event_starts_on", "event_starts_on"),
        Index("ix_competitions_event_ends_at", "event_ends_at"),
        Index("ix_competitions_event_ends_on", "event_ends_on"),
        Index("ix_competitions_registration_deadline_at", "registration_deadline_at"),
        Index("ix_competitions_registration_deadline_on", "registration_deadline_on"),
        Index(
            "ix_competitions_lower_title",
            func.lower(text("title")),
            postgresql_where=text("title IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    eligibility_summary: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(String(16))
    official_url: Mapped[str | None] = mapped_column(Text)
    registration_url: Mapped[str | None] = mapped_column(Text)

    registration_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_opens_on: Mapped[date | None] = mapped_column(Date)
    registration_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_deadline_on: Mapped[date | None] = mapped_column(Date)
    event_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_starts_on: Mapped[date | None] = mapped_column(Date)
    event_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_ends_on: Mapped[date | None] = mapped_column(Date)
    event_timezone: Mapped[str | None] = mapped_column(String(64))

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    organizer_links: Mapped[list[CompetitionOrganizer]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )
    category_links: Mapped[list[CompetitionCategory]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )
    topic_links: Mapped[list[CompetitionTopic]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )
    prizes: Mapped[list[CompetitionPrize]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )
    sources: Mapped[list[CompetitionSource]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )


class Organizer(Base):
    __tablename__ = "organizers"
    __table_args__ = (
        Index("ix_organizers_lower_name", func.lower(text("name"))),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    competition_links: Mapped[list[CompetitionOrganizer]] = relationship(
        back_populates="organizer"
    )


class CompetitionOrganizer(Base):
    __tablename__ = "competition_organizers"

    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    organizer_id: Mapped[int] = mapped_column(
        ForeignKey("organizers.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str | None] = mapped_column(String(64))

    competition: Mapped[Competition] = relationship(back_populates="organizer_links")
    organizer: Mapped[Organizer] = relationship(back_populates="competition_links")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    competition_links: Mapped[list[CompetitionCategory]] = relationship(
        back_populates="category"
    )


class CompetitionCategory(Base):
    __tablename__ = "competition_categories"
    __table_args__ = (
        Index(
            "uq_competition_primary_category",
            "competition_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index("ix_competition_categories_category_id", "category_id"),
    )

    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), primary_key=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    competition: Mapped[Competition] = relationship(back_populates="category_links")
    category: Mapped[Category] = relationship(back_populates="competition_links")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    competition_links: Mapped[list[CompetitionTopic]] = relationship(
        back_populates="topic"
    )


class CompetitionTopic(Base):
    __tablename__ = "competition_topics"
    __table_args__ = (Index("ix_competition_topics_topic_id", "topic_id"),)

    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="RESTRICT"), primary_key=True
    )

    competition: Mapped[Competition] = relationship(back_populates="topic_links")
    topic: Mapped[Topic] = relationship(back_populates="competition_links")


class CompetitionPrize(Base):
    __tablename__ = "competition_prizes"
    __table_args__ = (
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_prizes_nonnegative_amount"),
        CheckConstraint(
            "amount IS NULL OR currency IS NOT NULL", name="ck_prizes_amount_requires_currency"
        ),
        Index("ix_competition_prizes_competition_id", "competition_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False
    )
    award_name: Mapped[str | None] = mapped_column(Text)
    place: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    details: Mapped[str | None] = mapped_column(Text)

    competition: Mapped[Competition] = relationship(back_populates="prizes")


class SourcePublisher(Base):
    __tablename__ = "source_publishers"
    __table_args__ = (Index("ix_source_publishers_lower_name", func.lower(text("name"))),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(Text)

    source_links: Mapped[list[CompetitionSource]] = relationship(back_populates="publisher")


class CompetitionSource(Base):
    __tablename__ = "competition_sources"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('official', 'organizer', 'aggregator', 'social', 'manual', 'other')",
            name="ck_competition_sources_kind",
        ),
        CheckConstraint(
            "source_kind = 'manual' OR source_url IS NOT NULL",
            name="ck_competition_sources_url_required",
        ),
        CheckConstraint(
            "check_status IN ('unchecked', 'reachable', 'unavailable', 'changed', 'stale')",
            name="ck_competition_sources_check_status",
        ),
        UniqueConstraint("id", "competition_id", name="uq_competition_sources_id_competition"),
        Index(
            "uq_competition_source_normalized_url",
            "competition_id",
            "normalized_url",
            unique=True,
            postgresql_where=text("normalized_url IS NOT NULL"),
        ),
        Index("ix_competition_sources_competition_id", "competition_id"),
        Index("ix_competition_sources_last_checked_at", "last_checked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False
    )
    publisher_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_publishers.id", ondelete="SET NULL")
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    normalized_url: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="unchecked"
    )
    content_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    competition: Mapped[Competition] = relationship(back_populates="sources")
    publisher: Mapped[SourcePublisher | None] = relationship(back_populates="source_links")
    evidence: Mapped[list[FieldEvidence]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class FieldEvidence(Base):
    __tablename__ = "field_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "competition_id"],
            ["competition_sources.id", "competition_sources.competition_id"],
            ondelete="CASCADE",
            name="fk_field_evidence_source_competition",
        ),
        CheckConstraint(
            "field_name IN ('title', 'description', 'eligibility_summary', 'format', "
            "'registration_opens', 'registration_deadline', 'event_starts', 'event_ends', "
            "'event_timezone', 'official_url', 'registration_url', 'organizer', 'category', "
            "'topic', 'prize', 'registration_status')",
            name="ck_field_evidence_field_name",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_field_evidence_confidence_range",
        ),
        CheckConstraint(
            "review_state IN ('candidate', 'selected', 'rejected', 'superseded')",
            name="ck_field_evidence_review_state",
        ),
        Index(
            "uq_selected_field_evidence",
            "competition_id",
            "field_name",
            "field_key",
            unique=True,
            postgresql_where=text("review_state = 'selected'"),
        ),
        Index(
            "ix_field_evidence_competition_field_observed",
            "competition_id",
            "field_name",
            "observed_at",
        ),
        Index("ix_field_evidence_source_observed", "source_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    competition_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_name: Mapped[str] = mapped_column(String(32), nullable=False)
    field_key: Mapped[str] = mapped_column(String(160), nullable=False, server_default="")
    observed_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    review_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="candidate"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source: Mapped[CompetitionSource] = relationship(back_populates="evidence")


class DuplicateCandidate(Base):
    __tablename__ = "duplicate_candidates"
    __table_args__ = (
        CheckConstraint(
            "competition_a_id < competition_b_id", name="ck_duplicate_candidates_ordered_pair"
        ),
        CheckConstraint(
            "score >= 0 AND score <= 1", name="ck_duplicate_candidates_score_range"
        ),
        CheckConstraint(
            "review_state IN ('pending', 'confirmed_same', 'confirmed_distinct', 'deferred')",
            name="ck_duplicate_candidates_review_state",
        ),
        Index("ix_duplicate_candidates_state_score", "review_state", "score"),
    )

    competition_a_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    competition_b_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    matching_signals: Mapped[dict] = mapped_column(JSONB, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    review_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
