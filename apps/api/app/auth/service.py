import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.email_address import normalize_email
from app.auth.models import EmailAuthToken, User, UserSession

TOKEN_LIFETIME_MINUTES = 15
SESSION_LIFETIME_DAYS = 30


def _hash_token(token: str) -> str:
    """Hashes high-entropy tokens for secure database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_magic_link_token(db: Session, email: str) -> str:
    """
    Finds or creates a user by email, invalidates any existing active magic links,
    and returns a raw cryptographic token to be sent via email.
    """
    normalized_email = normalize_email(email)
    # Match legacy mixed-case rows without changing their stored addresses.
    user = db.scalar(
        select(User).where(func.lower(User.email) == normalized_email).with_for_update()
    )
    if not user:
        user = User(email=normalized_email)
        db.add(user)
        db.flush()  # Flush to get the new user.id

    # 2. Invalidate existing outstanding tokens to prevent race conditions or abuse
    now = datetime.now(UTC)
    db.execute(
        update(EmailAuthToken)
        .where(
            EmailAuthToken.user_id == user.id,
            EmailAuthToken.used_at.is_(None),
            EmailAuthToken.expires_at > now,
        )
        .values(expires_at=now, used_at=now)
        .execution_options(synchronize_session=False)
    )

    # 3. Generate a new cryptographically secure token
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)

    expires_at = now + timedelta(minutes=TOKEN_LIFETIME_MINUTES)

    auth_token = EmailAuthToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
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
