"""PostgreSQL-only tests for concurrent magic-link issuance."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401 - register all ORM relationships
from app.auth.models import EmailAuthToken, User
from app.auth.service import (
    MAX_ACTIVE_MAGIC_LINK_TOKENS,
    ActiveMagicLinkLimitExceeded,
    generate_magic_link_token,
    verify_magic_token_and_create_session,
)
from app.database import engine

pytestmark = pytest.mark.integration
_EXPECTED_TARGET = ("127.0.0.1", 56000, "fynd_test", "fynd_test_user")


def _assert_isolated_target(database_url: str | None, engine_url: URL) -> None:
    if not database_url:
        raise RuntimeError("Auth concurrency tests require DATABASE_URL for isolated fynd_test")
    try:
        configured = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError("Auth concurrency tests require a valid isolated DATABASE_URL") from None
    fields = ("host", "port", "database", "username")
    if tuple(getattr(configured, field) for field in fields) != _EXPECTED_TARGET:
        raise RuntimeError("Auth concurrency tests refuse a non-isolated database target")
    if tuple(getattr(engine_url, field) for field in fields) != _EXPECTED_TARGET:
        raise RuntimeError("Auth concurrency tests refuse a non-isolated SQLAlchemy engine target")


_assert_isolated_target(os.environ.get("DATABASE_URL"), engine.url)
_TestSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def unique_email():
    email = f"auth-concurrency-{uuid4().hex}@example.org"
    try:
        yield email
    finally:
        with _TestSession() as db:
            user = db.scalar(select(User).where(func.lower(User.email) == email))
            if user is not None:
                db.query(EmailAuthToken).filter(EmailAuthToken.user_id == user.id).delete()
                db.delete(user)
                db.commit()


def test_concurrent_first_time_requests_create_one_user_and_two_tokens(unique_email):
    barrier = Barrier(2)

    def issue():
        with _TestSession() as db:
            barrier.wait(timeout=10)
            return generate_magic_link_token(db, unique_email.upper())

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: issue(), range(2)))

    with _TestSession() as db:
        users = db.scalars(select(User).where(func.lower(User.email) == unique_email)).all()
        assert len(users) == 1
        records = db.scalars(
            select(EmailAuthToken).where(EmailAuthToken.user_id == users[0].id)
        ).all()
        assert len(records) == 2
        assert all(token for token in tokens)
        assert all(record.used_at is None for record in records)


def test_concurrent_issuance_cannot_exceed_five_active_tokens(unique_email):
    with _TestSession() as db:
        user = User(email=unique_email)
        db.add(user)
        db.commit()
        for _ in range(MAX_ACTIVE_MAGIC_LINK_TOKENS - 1):
            generate_magic_link_token(db, unique_email)

    barrier = Barrier(3)

    def issue():
        with _TestSession() as db:
            barrier.wait(timeout=10)
            try:
                return ("issued", generate_magic_link_token(db, unique_email))
            except ActiveMagicLinkLimitExceeded:
                return ("capped", None)

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(lambda _: issue(), range(3)))

    with _TestSession() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == unique_email))
        active_count = db.scalar(
            select(func.count()).select_from(EmailAuthToken).where(
                EmailAuthToken.user_id == user.id,
                EmailAuthToken.used_at.is_(None),
                EmailAuthToken.expires_at > func.now(),
            )
        )
        assert active_count == MAX_ACTIVE_MAGIC_LINK_TOKENS
        assert sum(outcome[0] == "issued" for outcome in outcomes) == 1
        assert sum(outcome[0] == "capped" for outcome in outcomes) == 2


def test_cap_rejection_keeps_existing_tokens_valid_and_single_use(unique_email):
    with _TestSession() as db:
        db.add(User(email=unique_email))
        db.commit()

    def active_token_count():
        with _TestSession() as db:
            user = db.scalar(select(User).where(func.lower(User.email) == unique_email))
            assert user is not None
            return db.scalar(
                select(func.count())
                .select_from(EmailAuthToken)
                .where(
                    EmailAuthToken.user_id == user.id,
                    EmailAuthToken.used_at.is_(None),
                    EmailAuthToken.expires_at > func.now(),
                )
            )

    tokens = []
    for _ in range(MAX_ACTIVE_MAGIC_LINK_TOKENS):
        with _TestSession() as db:
            tokens.append(generate_magic_link_token(db, unique_email))
        assert active_token_count() <= MAX_ACTIVE_MAGIC_LINK_TOKENS

    with _TestSession() as db:
        with pytest.raises(ActiveMagicLinkLimitExceeded):
            generate_magic_link_token(db, unique_email)

    assert active_token_count() == MAX_ACTIVE_MAGIC_LINK_TOKENS

    for token in tokens:
        with _TestSession() as db:
            assert verify_magic_token_and_create_session(db, token) is not None
        assert active_token_count() <= MAX_ACTIVE_MAGIC_LINK_TOKENS

        with _TestSession() as db:
            assert verify_magic_token_and_create_session(db, token) is None
        assert active_token_count() <= MAX_ACTIVE_MAGIC_LINK_TOKENS
