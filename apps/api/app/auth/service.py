import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.auth.email_address import normalize_email
from app.auth.models import EmailAuthToken, User, UserSession

TOKEN_LIFETIME_MINUTES = 15
SESSION_LIFETIME_DAYS = 30
MAX_ACTIVE_MAGIC_LINK_TOKENS = 5


class ActiveMagicLinkLimitExceeded(RuntimeError):
    """The user already has the maximum number of unexpired, unused links."""


def _hash_token(token: str) -> str:
    """Hashes high-entropy tokens for secure database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_magic_link_token(db: Session, email: str) -> str:
    """Create an independently valid token, up to five active links per user."""
    normalized_email = normalize_email(email)
    user_query = (
        select(User)
        .where(func.lower(User.email) == normalized_email)
        .order_by(User.id)
        .limit(2)
        .with_for_update()
    )
    matching_users = db.scalars(user_query).all()
    if len(matching_users) > 1:
        db.rollback()
        raise RuntimeError("Multiple users match the normalized email address")
    user = matching_users[0] if matching_users else None

    if user is None:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            insert_user = pg_insert(User)
        elif dialect == "sqlite":
            insert_user = sqlite_insert(User)
        else:
            raise RuntimeError("Magic-link user creation requires PostgreSQL or SQLite")
        db.execute(
            insert_user.values(email=normalized_email).on_conflict_do_nothing(
                index_elements=[User.email]
            )
        )
        # The unique email constraint arbitrates concurrent first inserts. Lock the
        # resulting row before counting or inserting tokens.
        matching_users = db.scalars(user_query).all()
        if len(matching_users) > 1:
            db.rollback()
            raise RuntimeError("Multiple users match the normalized email address")
        if not matching_users:
            db.rollback()
            raise RuntimeError("Could not create or lock the authentication user")
        user = matching_users[0]

    now = datetime.now(UTC)
    active_count = db.scalar(
        select(func.count())
        .select_from(EmailAuthToken)
        .where(
            EmailAuthToken.user_id == user.id,
            EmailAuthToken.used_at.is_(None),
            EmailAuthToken.expires_at > now,
        )
    ) or 0
    if active_count >= MAX_ACTIVE_MAGIC_LINK_TOKENS:
        db.rollback()
        raise ActiveMagicLinkLimitExceeded

    raw_token = secrets.token_urlsafe(32)
    auth_token = EmailAuthToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=now + timedelta(minutes=TOKEN_LIFETIME_MINUTES),
    )
    db.add(auth_token)
    db.commit()
    return raw_token

def verify_magic_token_and_create_session(db: Session, raw_token: str) -> str | None:
    """
    Verifies a magic link token. If valid, consumes it and generates a session token.
    Returns the raw session token to be set as a secure HttpOnly cookie, or None if invalid.
    """
    token_hash = _hash_token(raw_token)

    now = datetime.now(UTC)
    try:
        # A conditional UPDATE is the atomic claim. Under PostgreSQL's row locking,
        # concurrent verifiers serialize and only the first can match used_at IS NULL.
        claimed_user_id = db.execute(
            update(EmailAuthToken)
            .where(
                EmailAuthToken.token_hash == token_hash,
                EmailAuthToken.expires_at > now,
                EmailAuthToken.used_at.is_(None),
            )
            .values(used_at=now)
            .returning(EmailAuthToken.user_id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if claimed_user_id is None:
            db.rollback()
            return None

        raw_session_token = secrets.token_urlsafe(32)
        user_session = UserSession(
            user_id=claimed_user_id,
            session_token_hash=_hash_token(raw_session_token),
            expires_at=now + timedelta(days=SESSION_LIFETIME_DAYS),
        )
        db.add(user_session)
        db.commit()
        return raw_session_token
    except Exception:
        # Consumption and session creation are one transaction: a failed insert or
        # commit must leave the token available for a later valid attempt.
        db.rollback()
        raise


def revoke_session(db: Session, raw_session_token: str) -> None:
    """Revoke the session represented by a presented raw cookie token.

    The database stores only the token hash. Updating by that unique hash makes
    repeated revocation harmless and does not reveal whether a row matched.
    """
    try:
        db.execute(
            update(UserSession)
            .where(UserSession.session_token_hash == _hash_token(raw_session_token))
            .values(is_revoked=True)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
