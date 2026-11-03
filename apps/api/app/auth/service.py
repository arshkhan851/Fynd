import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

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
    # 1. Find or create the user
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email)
        db.add(user)
        db.flush()  # Flush to get the new user.id

    # 2. Invalidate existing outstanding tokens to prevent race conditions or abuse
    db.execute(
        update(EmailAuthToken)
        .where(EmailAuthToken.user_id == user.id)
        .values(expires_at=datetime.now(timezone.utc))
    )

    # 3. Generate a new cryptographically secure token
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_LIFETIME_MINUTES)

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

    # 1. Find a valid, unexpired token
    stmt = select(EmailAuthToken).where(
        EmailAuthToken.token_hash == token_hash,
        EmailAuthToken.expires_at > datetime.now(timezone.utc),
    )
    auth_token = db.scalar(stmt)

    if not auth_token:
        return None

    # 2. Invalidate the token so it cannot be used again
    auth_token.expires_at = datetime.now(timezone.utc)

    # 3. Create a new user session
    raw_session_token = secrets.token_urlsafe(32)
    session_hash = _hash_token(raw_session_token)

    user_session = UserSession(
        user_id=auth_token.user_id,
        session_token_hash=session_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS),
    )
    db.add(user_session)
    db.commit()

    return raw_session_token