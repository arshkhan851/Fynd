"""PostgreSQL integration tests for atomic auth rate-limit counters."""

import base64
import ipaddress
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, delete, func, inspect, literal, select, text, tuple_
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401 - register all ORM relationships
from app.auth.email import BaseEmailProvider, EmailDeliveryError
from app.auth.models import AuthRateLimitCounter, EmailAuthToken, User
from app.auth.rate_limit import (
    EMAIL_REQUEST_LIMIT,
    HMACKey,
    HMACKeyring,
    RateLimitStorageError,
    _database_window_start,
    cleanup_expired_rate_limit_counters,
    derive_identifier_digests,
    fixed_window_start,
    increment_request_counters,
)
from app.auth.router import get_email_provider_factory
from app.auth.router import router as auth_router
from app.database import engine, get_db

pytestmark = pytest.mark.integration
_EXPECTED_TARGET = ("127.0.0.1", 56000, "fynd_test", "fynd_test_user")
_TestSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
_TEST_KEYRING = HMACKeyring(HMACKey("integration1", bytes(range(32))))


class _ConcurrentCaptureProvider(BaseEmailProvider):
    def __init__(self):
        self._lock = Lock()
        self.sent: list[tuple[str, str]] = []

    def send_magic_link(self, email: str, raw_token: str) -> None:
        with self._lock:
            self.sent.append((email, raw_token))


class _RejectingCaptureProvider(BaseEmailProvider):
    def __init__(self):
        self._lock = Lock()
        self.attempts = 0

    def send_magic_link(self, email: str, raw_token: str) -> None:
        with self._lock:
            self.attempts += 1
        raise EmailDeliveryError("sanitized provider failure")


def _create_rate_limited_auth_app(
    keyring: HMACKeyring, provider: BaseEmailProvider, monkeypatch
):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")

    def override_db():
        with _TestSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_email_provider_factory] = lambda: lambda: provider
    monkeypatch.setenv("AUTH_RATE_LIMIT_HMAC_KEY_ID", keyring.current.key_id)
    monkeypatch.setenv(
        "AUTH_RATE_LIMIT_HMAC_KEY_B64",
        base64.b64encode(keyring.current.secret).decode("ascii"),
    )
    monkeypatch.delenv("AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_ID", raising=False)
    monkeypatch.delenv("AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_B64", raising=False)
    return app


def _cleanup_integrated_request_data(
    emails: list[str], client_ip: str, keyring: HMACKeyring
) -> None:
    identifiers = [
        (scope, item.key_id, item.digest)
        for scope, values in (
            ("email", emails),
            ("ip", [client_ip]),
        )
        for identifier in values
        for item in derive_identifier_digests(scope, identifier, keyring)
    ]
    with _TestSession() as db:
        db.execute(
            delete(AuthRateLimitCounter).where(
                tuple_(
                    AuthRateLimitCounter.scope,
                    AuthRateLimitCounter.key_id,
                    AuthRateLimitCounter.identifier_digest,
                ).in_(identifiers)
            )
        )
        db.execute(delete(User).where(User.email.in_(emails)))
        db.commit()


def _assert_isolated_target(database_url: str | None, engine_url: URL) -> None:
    if not database_url:
        raise RuntimeError("Rate-limit integration tests require isolated fynd_test DATABASE_URL")
    try:
        configured = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError(
            "Rate-limit integration tests require a valid isolated DATABASE_URL"
        ) from None
    fields = ("host", "port", "database", "username")
    if tuple(getattr(configured, field) for field in fields) != _EXPECTED_TARGET:
        raise RuntimeError("Rate-limit integration tests refuse a non-isolated database target")
    if tuple(getattr(engine_url, field) for field in fields) != _EXPECTED_TARGET:
        raise RuntimeError("Rate-limit integration tests refuse a non-isolated engine target")


_assert_isolated_target(os.environ.get("DATABASE_URL"), engine.url)


@pytest.fixture(scope="module", autouse=True)
def ensure_counter_table():
    existed = inspect(engine).has_table(AuthRateLimitCounter.__tablename__)
    if not existed:
        AuthRateLimitCounter.__table__.create(engine)
    try:
        yield
    finally:
        if not existed:
            AuthRateLimitCounter.__table__.drop(engine, checkfirst=True)


@pytest.fixture
def rate_limit_case():
    email = f"rate-limit-{uuid4().hex}@example.org"
    client_ip = ipaddress.IPv6Address(uuid4().int).compressed
    try:
        yield email, client_ip, _TEST_KEYRING
    finally:
        values = []
        for scope, identifier in (("email", email), ("ip", client_ip)):
            for item in derive_identifier_digests(scope, identifier, _TEST_KEYRING):
                values.append((scope, item.key_id, item.digest))
        with _TestSession() as db:
            db.execute(
                delete(AuthRateLimitCounter).where(
                    tuple_(
                        AuthRateLimitCounter.scope,
                        AuthRateLimitCounter.key_id,
                        AuthRateLimitCounter.identifier_digest,
                    ).in_(values)
                )
            )
            db.commit()


def _counter_rows(db: Session, *, scope: str, key_id: str, digest: bytes):
    return db.scalars(
        select(AuthRateLimitCounter).where(
            AuthRateLimitCounter.scope == scope,
            AuthRateLimitCounter.key_id == key_id,
            AuthRateLimitCounter.identifier_digest == digest,
        )
    ).all()


def test_atomic_increments_preserve_over_limit_attempts(rate_limit_case):
    email, client_ip, keyring = rate_limit_case
    decisions = []
    for _ in range(6):
        with _TestSession() as db:
            decisions.append(
                increment_request_counters(
                    db, email=email, client_ip=client_ip, keyring=keyring
                )
            )

    assert [decision.email_count for decision in decisions] == [1, 2, 3, 4, 5, 6]
    assert [decision.ip_count for decision in decisions] == [1, 2, 3, 4, 5, 6]
    assert [decision.allowed for decision in decisions] == [True] * 5 + [False]


def test_postgresql_second_dimension_failure_rolls_back_both_increments(
    rate_limit_case,
):
    email, client_ip, keyring = rate_limit_case
    email_digest = derive_identifier_digests("email", email, keyring)[0].digest
    ip_digest = derive_identifier_digests("ip", client_ip, keyring)[0].digest
    suffix = uuid4().hex
    function_name = f"fail_rate_limit_ip_{suffix}"
    trigger_name = f"fail_rate_limit_ip_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE FUNCTION {function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.scope = 'ip' THEN "
                "RAISE EXCEPTION 'simulated rate-limit write failure'; "
                "END IF; RETURN NEW; END; $$"
            )
        )
        connection.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT OR UPDATE "
                "ON auth_rate_limit_counters FOR EACH ROW "
                f"EXECUTE FUNCTION {function_name}()"
            )
        )

    try:
        with _TestSession() as db:
            with pytest.raises(RateLimitStorageError, match="temporarily unavailable"):
                increment_request_counters(
                    db, email=email, client_ip=client_ip, keyring=keyring
                )

        with _TestSession() as db:
            assert _counter_rows(
                db, scope="email", key_id=keyring.current.key_id, digest=email_digest
            ) == []
            assert _counter_rows(
                db, scope="ip", key_id=keyring.current.key_id, digest=ip_digest
            ) == []
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"DROP TRIGGER IF EXISTS {trigger_name} "
                    "ON auth_rate_limit_counters"
                )
            )
            connection.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))


def test_concurrent_email_requests_cannot_bypass_email_limit(rate_limit_case):
    email, client_ip, keyring = rate_limit_case
    workers = 12
    barrier = Barrier(workers)

    def increment():
        with _TestSession() as db:
            barrier.wait(timeout=20)
            return increment_request_counters(
                db, email=email, client_ip=client_ip, keyring=keyring
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        decisions = list(pool.map(lambda _: increment(), range(workers)))

    assert sum(decision.allowed for decision in decisions) == 5
    with _TestSession() as db:
        email_digest = derive_identifier_digests("email", email, keyring)[0].digest
        ip_digest = derive_identifier_digests("ip", client_ip, keyring)[0].digest
        email_rows = _counter_rows(
            db, scope="email", key_id=keyring.current.key_id, digest=email_digest
        )
        ip_rows = _counter_rows(
            db, scope="ip", key_id=keyring.current.key_id, digest=ip_digest
        )
        assert len(email_rows) == len(ip_rows) == 1
        assert email_rows[0].request_count == ip_rows[0].request_count == workers


def test_concurrent_requests_from_one_ip_cannot_bypass_ip_limit(rate_limit_case):
    _, client_ip, keyring = rate_limit_case
    workers = 12
    emails = [f"{uuid4().hex}@example.org" for _ in range(workers)]
    barrier = Barrier(workers)

    def increment(email):
        with _TestSession() as db:
            barrier.wait(timeout=20)
            return increment_request_counters(
                db, email=email, client_ip=client_ip, keyring=keyring
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        decisions = list(pool.map(increment, emails))

    assert sum(decision.allowed for decision in decisions) == 10
    ip_digest = derive_identifier_digests("ip", client_ip, keyring)[0].digest
    with _TestSession() as db:
        rows = _counter_rows(
            db, scope="ip", key_id=keyring.current.key_id, digest=ip_digest
        )
        assert len(rows) == 1
        assert rows[0].request_count == workers

        email_digests = [
            derive_identifier_digests("email", email, keyring)[0].digest
            for email in emails
        ]
        db.execute(
            delete(AuthRateLimitCounter).where(
                AuthRateLimitCounter.scope == "email",
                AuthRateLimitCounter.key_id == keyring.current.key_id,
                AuthRateLimitCounter.identifier_digest.in_(email_digests),
            )
        )
        db.commit()


def test_previous_key_rotation_increments_both_key_versions(rate_limit_case):
    email, client_ip, _ = rate_limit_case
    keyring = HMACKeyring(
        current=HMACKey("current2", bytes(range(32))),
        previous=HMACKey("previous2", bytes(range(1, 33))),
    )
    digests = [
        (scope, item.key_id, item.digest)
        for scope, identifier in (("email", email), ("ip", client_ip))
        for item in derive_identifier_digests(scope, identifier, keyring)
    ]
    try:
        with _TestSession() as db:
            decision = increment_request_counters(
                db, email=email, client_ip=client_ip, keyring=keyring
            )
            assert decision.email_count == decision.ip_count == 1

        with _TestSession() as db:
            for scope, key_id, digest in digests:
                rows = _counter_rows(db, scope=scope, key_id=key_id, digest=digest)
                assert len(rows) == 1
                assert rows[0].request_count == 1
    finally:
        with _TestSession() as db:
            db.execute(
                delete(AuthRateLimitCounter).where(
                    tuple_(
                        AuthRateLimitCounter.scope,
                        AuthRateLimitCounter.key_id,
                        AuthRateLimitCounter.identifier_digest,
                    ).in_(digests)
                )
            )
            db.commit()


def test_previous_key_at_limit_keeps_dual_key_request_rate_limited(rate_limit_case):
    email, client_ip, _ = rate_limit_case
    keyring = HMACKeyring(
        current=HMACKey("rotation_new", bytes(range(32))),
        previous=HMACKey("rotation_old", bytes(range(1, 33))),
    )
    email_digests = derive_identifier_digests("email", email, keyring)
    ip_digests = derive_identifier_digests("ip", client_ip, keyring)
    previous_email = next(item for item in email_digests if item.key_id == "rotation_old")
    window_start = fixed_window_start(datetime.now(UTC))
    adjacent_window_start = window_start + timedelta(seconds=900)
    cleanup_keys = [
        ("email", item.key_id, item.digest) for item in email_digests
    ] + [("ip", item.key_id, item.digest) for item in ip_digests]

    try:
        with _TestSession() as db:
            db.add_all(
                [
                    AuthRateLimitCounter(
                        scope="email",
                        key_id=previous_email.key_id,
                        identifier_digest=previous_email.digest,
                        window_start=seed_window,
                        request_count=EMAIL_REQUEST_LIMIT,
                    )
                    for seed_window in (window_start, adjacent_window_start)
                ]
            )
            db.commit()

        with _TestSession() as db:
            decision = increment_request_counters(
                db, email=email, client_ip=client_ip, keyring=keyring
            )
        assert decision.email_count == EMAIL_REQUEST_LIMIT + 1
        assert decision.allowed is False

        with _TestSession() as db:
            for scope, digests in (("email", email_digests), ("ip", ip_digests)):
                for item in digests:
                    rows = _counter_rows(
                        db,
                        scope=scope,
                        key_id=item.key_id,
                        digest=item.digest,
                    )
                    if scope == "email" and item.key_id == "rotation_old":
                        assert sorted(row.request_count for row in rows) == [
                            EMAIL_REQUEST_LIMIT,
                            EMAIL_REQUEST_LIMIT + 1,
                        ]
                    else:
                        assert len(rows) == 1
                        assert rows[0].request_count == 1
    finally:
        with _TestSession() as db:
            db.execute(
                delete(AuthRateLimitCounter).where(
                    tuple_(
                        AuthRateLimitCounter.scope,
                        AuthRateLimitCounter.key_id,
                        AuthRateLimitCounter.identifier_digest,
                    ).in_(cleanup_keys)
                )
            )
            db.commit()


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (
            datetime(2026, 11, 13, 10, 14, 59, 999999, tzinfo=UTC),
            datetime(2026, 11, 13, 10, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 13, 10, 15, tzinfo=UTC),
            datetime(2026, 11, 13, 10, 15, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 13, 10, 15, 0, 1, tzinfo=UTC),
            datetime(2026, 11, 13, 10, 15, tzinfo=UTC),
        ),
    ],
)
def test_postgresql_database_window_bucket_at_boundary(instant, expected):
    with _TestSession() as db:
        actual = _database_window_start(
            db, timestamp=literal(instant, type_=DateTime(timezone=True))
        )
    assert actual == expected


def test_concurrent_requests_through_auth_route_cannot_exceed_email_limit(
    rate_limit_case, monkeypatch
):
    email, client_ip, _ = rate_limit_case
    keyring = HMACKeyring(HMACKey(f"route_email_{uuid4().hex[:8]}", bytes(range(32))))
    provider = _ConcurrentCaptureProvider()
    app = _create_rate_limited_auth_app(keyring, provider, monkeypatch)
    workers = 14
    barrier = Barrier(workers)

    def request_link(_index: int) -> int:
        barrier.wait(timeout=20)
        with TestClient(app, client=(client_ip, 50000)) as client:
            return client.post(
                "/api/v1/auth/request-link", json={"email": email}
            ).status_code

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(request_link, range(workers)))

        assert statuses == [202] * workers
        assert len(provider.sent) == 5
        email_digest = derive_identifier_digests("email", email, keyring)[0].digest
        ip_digest = derive_identifier_digests("ip", client_ip, keyring)[0].digest
        with _TestSession() as db:
            email_rows = _counter_rows(
                db,
                scope="email",
                key_id=keyring.current.key_id,
                digest=email_digest,
            )
            ip_rows = _counter_rows(
                db,
                scope="ip",
                key_id=keyring.current.key_id,
                digest=ip_digest,
            )
            user = db.scalar(select(User).where(User.email == email))
            token_count = db.scalar(
                select(func.count())
                .select_from(EmailAuthToken)
                .where(EmailAuthToken.user_id == user.id)
            )
            assert len(email_rows) == len(ip_rows) == 1
            assert email_rows[0].request_count == ip_rows[0].request_count == workers
            assert token_count == 5
    finally:
        _cleanup_integrated_request_data([email], client_ip, keyring)


def test_concurrent_requests_through_auth_route_cannot_exceed_ip_limit(monkeypatch):
    workers = 13
    emails = [f"rate-route-{uuid4().hex}@example.org" for _ in range(workers)]
    client_ip = ipaddress.IPv6Address(uuid4().int).compressed
    keyring = HMACKeyring(HMACKey(f"route_ip_{uuid4().hex[:8]}", bytes(range(32))))
    provider = _ConcurrentCaptureProvider()
    app = _create_rate_limited_auth_app(keyring, provider, monkeypatch)
    barrier = Barrier(workers)

    def request_link(email: str) -> int:
        barrier.wait(timeout=20)
        with TestClient(app, client=(client_ip, 50000)) as client:
            return client.post(
                "/api/v1/auth/request-link", json={"email": email}
            ).status_code

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(request_link, emails))

        assert statuses == [202] * workers
        assert len(provider.sent) == 10
        ip_digest = derive_identifier_digests("ip", client_ip, keyring)[0].digest
        email_digests = [
            derive_identifier_digests("email", email, keyring)[0].digest
            for email in emails
        ]
        with _TestSession() as db:
            ip_rows = _counter_rows(
                db,
                scope="ip",
                key_id=keyring.current.key_id,
                digest=ip_digest,
            )
            email_rows = db.scalars(
                select(AuthRateLimitCounter).where(
                    AuthRateLimitCounter.scope == "email",
                    AuthRateLimitCounter.key_id == keyring.current.key_id,
                    AuthRateLimitCounter.identifier_digest.in_(email_digests),
                )
            ).all()
            assert len(ip_rows) == 1
            assert ip_rows[0].request_count == workers
            assert len(email_rows) == workers
            assert all(row.request_count == 1 for row in email_rows)
    finally:
        _cleanup_integrated_request_data(emails, client_ip, keyring)


def test_failed_delivery_attempts_consume_both_rate_limits(rate_limit_case, monkeypatch):
    email, client_ip, _ = rate_limit_case
    keyring = HMACKeyring(HMACKey(f"route_fail_{uuid4().hex[:8]}", bytes(range(32))))
    provider = _RejectingCaptureProvider()
    app = _create_rate_limited_auth_app(keyring, provider, monkeypatch)

    try:
        with TestClient(app, client=(client_ip, 50000)) as client:
            responses = [
                client.post(
                    "/api/v1/auth/request-link", json={"email": email}
                )
                for _ in range(6)
            ]

        assert [response.status_code for response in responses] == [503] * 5 + [202]
        assert responses[-1].json() == {
            "message": "If the email is valid, a login link has been sent."
        }
        assert provider.attempts == 5
        email_digest = derive_identifier_digests("email", email, keyring)[0].digest
        ip_digest = derive_identifier_digests("ip", client_ip, keyring)[0].digest
        with _TestSession() as db:
            email_rows = _counter_rows(
                db,
                scope="email",
                key_id=keyring.current.key_id,
                digest=email_digest,
            )
            ip_rows = _counter_rows(
                db,
                scope="ip",
                key_id=keyring.current.key_id,
                digest=ip_digest,
            )
            assert len(email_rows) == len(ip_rows) == 1
            assert email_rows[0].request_count == ip_rows[0].request_count == 6
    finally:
        _cleanup_integrated_request_data([email], client_ip, keyring)


def test_cleanup_deletes_only_one_bounded_batch_of_expired_rows():
    key_id = f"cleanup{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    stale_window = fixed_window_start(now - timedelta(days=2))
    current_window = fixed_window_start(now)
    stale_rows = [
        AuthRateLimitCounter(
            scope="email",
            key_id=key_id,
            identifier_digest=index.to_bytes(32, "big"),
            window_start=stale_window,
            request_count=1,
        )
        for index in range(501)
    ]
    current_row = AuthRateLimitCounter(
        scope="ip",
        key_id=key_id,
        identifier_digest=(501).to_bytes(32, "big"),
        window_start=current_window,
        request_count=1,
    )
    try:
        with _TestSession() as db:
            db.add_all([*stale_rows, current_row])
            db.commit()
            assert cleanup_expired_rate_limit_counters(db, batch_size=500) == 500
            remaining_stale = db.scalar(
                select(func.count())
                .select_from(AuthRateLimitCounter)
                .where(
                    AuthRateLimitCounter.key_id == key_id,
                    AuthRateLimitCounter.window_start == stale_window,
                )
            )
            current_count = db.scalar(
                select(func.count())
                .select_from(AuthRateLimitCounter)
                .where(
                    AuthRateLimitCounter.key_id == key_id,
                    AuthRateLimitCounter.window_start == current_window,
                )
            )
            assert remaining_stale == 1
            assert current_count == 1
    finally:
        with _TestSession() as db:
            db.execute(
                delete(AuthRateLimitCounter).where(
                    AuthRateLimitCounter.key_id == key_id
                )
            )
            db.commit()
