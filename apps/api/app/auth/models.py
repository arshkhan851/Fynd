import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models import SavedCompetition


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    auth_tokens: Mapped[list["EmailAuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    saved_competitions: Mapped[list["SavedCompetition"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="SavedCompetition.user_id",
    )


class EmailAuthToken(Base):
    __tablename__ = "email_auth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="auth_tokens")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    
    session_token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="sessions")


class AuthRateLimitCounter(Base):
    __tablename__ = "auth_rate_limit_counters"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('email', 'ip')", name="ck_auth_rate_limit_counters_scope"
        ),
        CheckConstraint(
            "length(key_id) > 0", name="ck_auth_rate_limit_counters_key_id"
        ),
        CheckConstraint(
            "octet_length(identifier_digest) = 32",
            name="ck_auth_rate_limit_counters_digest_length",
        ),
        CheckConstraint(
            "request_count > 0", name="ck_auth_rate_limit_counters_positive_count"
        ),
        Index(
            "ix_auth_rate_limit_counters_window_start",
            "window_start",
        ),
    )

    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    identifier_digest: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
