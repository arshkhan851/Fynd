import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.models import EmailAuthToken, User, UserSession
from app.auth.service import (
    generate_magic_link_token,
    revoke_session,
    verify_magic_token_and_create_session,
)


def _session_factory(database_url: str = "sqlite+pysqlite://"):
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 15},
        poolclass=StaticPool if database_url.endswith("//") else None,
    )
    User.__table__.create(engine)
    EmailAuthToken.__table__.create(engine)
    UserSession.__table__.create(engine)
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def db_factory():
    engine, factory = _session_factory()
    try:
        yield factory
    finally:
        engine.dispose()


def _create_user(db: Session, email: str = "organizer@example.org") -> User:
    user = User(email=email)
    db.add(user)
    db.commit()
    return user


def test_magic_link_successful_first_use_and_second_use_rejection(db_factory):
    with db_factory() as db:
        _create_user(db)
        token = generate_magic_link_token(db, "organizer@example.org")

        first_session_token = verify_magic_token_and_create_session(db, token)
        second_session_token = verify_magic_token_and_create_session(db, token)

        assert first_session_token
        assert second_session_token is None
        stored_token = db.scalar(select(EmailAuthToken))
        assert stored_token is not None
        assert stored_token.used_at is not None
        assert db.scalar(select(func.count()).select_from(UserSession)) == 1


def test_expired_magic_link_is_rejected_without_session(db_factory):
    with db_factory() as db:
        user = _create_user(db)
        token = generate_magic_link_token(db, user.email)
        stored = db.scalar(select(EmailAuthToken))
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        assert verify_magic_token_and_create_session(db, token) is None
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0
        assert db.scalar(select(EmailAuthToken)).used_at is None


def test_new_magic_link_invalidates_older_active_token(db_factory):
    with db_factory() as db:
        _create_user(db)
        old_token = generate_magic_link_token(db, "organizer@example.org")
        new_token = generate_magic_link_token(db, "organizer@example.org")

        old_record = db.scalar(
            select(EmailAuthToken).where(EmailAuthToken.used_at.is_not(None))
        )
        assert old_record is not None
        assert old_record.used_at is not None
        assert verify_magic_token_and_create_session(db, old_token) is None
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0
        assert verify_magic_token_and_create_session(db, new_token)
        assert db.scalar(select(func.count()).select_from(UserSession)) == 1


def test_session_creation_failure_rolls_back_token_consumption(db_factory, monkeypatch):
    with db_factory() as db:
        _create_user(db)
        token = generate_magic_link_token(db, "organizer@example.org")
        original_add = db.add

        def fail_session_add(instance):
            if isinstance(instance, UserSession):
                raise RuntimeError("simulated session insert failure")
            original_add(instance)

        monkeypatch.setattr(db, "add", fail_session_add)
        with pytest.raises(RuntimeError, match="simulated session insert failure"):
            verify_magic_token_and_create_session(db, token)

        monkeypatch.setattr(db, "add", original_add)
        db.expire_all()
        assert db.scalar(select(EmailAuthToken)).used_at is None
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0


def test_concurrent_verification_creates_at_most_one_session(tmp_path):
    database_path = tmp_path / "auth-concurrency.sqlite"
    engine, factory = _session_factory(f"sqlite+pysqlite:///{database_path}")
    try:
        with factory() as db:
            _create_user(db)
            token = generate_magic_link_token(db, "organizer@example.org")

        barrier = Barrier(2)

        def verify_concurrently():
            with factory() as db:
                barrier.wait(timeout=5)
                return verify_magic_token_and_create_session(db, token)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: verify_concurrently(), range(2)))

        assert sum(result is not None for result in results) == 1
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(UserSession)) == 1
            assert db.scalar(select(EmailAuthToken)).used_at is not None
    finally:
        engine.dispose()


def test_session_revocation_is_idempotent_and_does_not_need_session_id(db_factory):
    raw_token = "session-to-revoke"
    with db_factory() as db:
        user = _create_user(db)
        session = UserSession(
            user_id=user.id,
            session_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(session)
        db.commit()

        revoke_session(db, raw_token)
        db.expire_all()
        assert db.get(UserSession, session.id).is_revoked is True

        revoke_session(db, raw_token)
        assert db.scalar(select(func.count()).select_from(UserSession)) == 1
        revoke_session(db, "unknown-token")
        assert db.scalar(select(func.count()).select_from(UserSession)) == 1
