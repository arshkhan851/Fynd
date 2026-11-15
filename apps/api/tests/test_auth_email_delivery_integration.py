"""Guarded PostgreSQL integration tests for email delivery reservations."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, inspect, select, text, tuple_
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

from app.auth.email_delivery import (
    EMAIL_DELIVERY_ACCEPTED_COOLDOWN,
    EMAIL_DELIVERY_RESERVATION_LEASE,
    EMAIL_DELIVERY_UNCERTAINTY_HOLD,
    EmailDeliveryStateStorageError,
    acquire_email_delivery_reservation,
    derive_email_delivery_aliases,
    finalize_accepted_email_delivery,
    preserve_ambiguous_email_delivery_hold,
    release_email_delivery_reservation,
    transition_email_delivery_to_sending,
)
from app.auth.models import AuthEmailDeliveryState
from app.auth.rate_limit import HMACKey, HMACKeyring
from app.database import engine

pytestmark = pytest.mark.integration
_EXPECTED_TARGET = ("127.0.0.1", 56000, "fynd_test", "fynd_test_user")
_TestSession = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _assert_isolated_target(database_url: str | None, engine_url: URL) -> None:
    if not database_url:
        raise RuntimeError(
            "Email delivery integration tests require isolated fynd_test DATABASE_URL"
        )
    try:
        configured = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError(
            "Email delivery integration tests require a valid isolated DATABASE_URL"
        ) from None
    fields = ("host", "port", "database", "username")
    if tuple(getattr(configured, field) for field in fields) != _EXPECTED_TARGET:
        raise RuntimeError(
            "Email delivery integration tests refuse a non-isolated database target"
        )
    if tuple(getattr(engine_url, field) for field in fields) != _EXPECTED_TARGET:
        raise RuntimeError(
            "Email delivery integration tests refuse a non-isolated engine target"
        )


def _assert_live_server_identity(connectable: Engine) -> None:
    """Verify the server behind the configured loopback port before test setup."""
    try:
        with connectable.connect() as connection:
            identity = connection.execute(
                text(
                    "SELECT current_database(), current_user, inet_server_port()"
                )
            ).one()
    except Exception:
        raise RuntimeError(
            "Email delivery integration tests cannot verify PostgreSQL identity"
        ) from None
    if tuple(identity) != ("fynd_test", "fynd_test_user", 5432):
        raise RuntimeError(
            "Email delivery integration tests refuse an unexpected PostgreSQL identity"
        )


# Both guards run during module import, before fixtures or test code can write.
_assert_isolated_target(os.environ.get("DATABASE_URL"), engine.url)
_assert_live_server_identity(engine)


@pytest.fixture(scope="module", autouse=True)
def ensure_delivery_state_table():
    existed = inspect(engine).has_table(AuthEmailDeliveryState.__tablename__)
    if not existed:
        AuthEmailDeliveryState.__table__.create(engine)
    try:
        yield
    finally:
        if not existed:
            AuthEmailDeliveryState.__table__.drop(engine, checkfirst=True)


@pytest.fixture
def delivery_case():
    email = f"delivery-{uuid4().hex}@example.org"
    keyring = HMACKeyring(
        current=HMACKey(f"dc_{uuid4().hex[:10]}", bytes(range(32))),
        previous=HMACKey(f"dp_{uuid4().hex[:10]}", bytes(range(1, 33))),
    )
    try:
        yield email, keyring
    finally:
        aliases = derive_email_delivery_aliases(email, keyring).values
        keys = tuple((alias.key_id, alias.digest) for alias in aliases)
        with _TestSession() as db:
            db.execute(
                delete(AuthEmailDeliveryState).where(
                    tuple_(
                        AuthEmailDeliveryState.key_id,
                        AuthEmailDeliveryState.identifier_digest,
                    ).in_(keys)
                )
            )
            db.commit()


def _rows(db: Session, email: str, keyring: HMACKeyring):
    aliases = derive_email_delivery_aliases(email, keyring).values
    keys = tuple((alias.key_id, alias.digest) for alias in aliases)
    return db.scalars(
        select(AuthEmailDeliveryState)
        .where(
            tuple_(
                AuthEmailDeliveryState.key_id,
                AuthEmailDeliveryState.identifier_digest,
            ).in_(keys)
        )
        .order_by(
            AuthEmailDeliveryState.key_id,
            AuthEmailDeliveryState.identifier_digest,
        )
    ).all()


def _acquire(email: str, keyring: HMACKeyring):
    with _TestSession() as db:
        return acquire_email_delivery_reservation(db, email=email, keyring=keyring)


def _transition(email: str, reservation_id, keyring: HMACKeyring):
    with _TestSession() as db:
        return transition_email_delivery_to_sending(
            db, email=email, reservation_id=reservation_id, keyring=keyring
        )


def _finalize(email: str, reservation_id, keyring: HMACKeyring):
    with _TestSession() as db:
        return finalize_accepted_email_delivery(
            db, email=email, reservation_id=reservation_id, keyring=keyring
        )


def _release(email: str, reservation_id, keyring: HMACKeyring):
    with _TestSession() as db:
        return release_email_delivery_reservation(
            db, email=email, reservation_id=reservation_id, keyring=keyring
        )


def test_new_reservation_acquires_every_alias_and_stores_no_raw_email(delivery_case):
    email, keyring = delivery_case
    reservation_id = _acquire(email, keyring)

    assert reservation_id is not None
    with _TestSession() as db:
        rows = _rows(db, email, keyring)
        assert len(rows) == 2
        assert all(row.reservation_id == reservation_id for row in rows)
        assert all(row.reservation_state == "reserved" for row in rows)
        assert all(row.reservation_expires_at is not None for row in rows)
        assert all(row.last_accepted_at is None for row in rows)
        assert all(len(row.identifier_digest) == 32 for row in rows)
        assert all(email.encode() not in row.identifier_digest for row in rows)
        assert "email" not in AuthEmailDeliveryState.__table__.columns


def test_live_reservation_suppresses_a_second_request(delivery_case):
    email, keyring = delivery_case
    first = _acquire(email, keyring)
    second = _acquire(email, keyring)

    assert first is not None
    assert second is None


def test_accepted_delivery_cooldown_suppresses_reacquisition(delivery_case):
    email, keyring = delivery_case
    reservation_id = _acquire(email, keyring)
    assert reservation_id is not None
    assert _transition(email, reservation_id, keyring)
    assert _finalize(email, reservation_id, keyring)

    assert _acquire(email, keyring) is None
    with _TestSession() as db:
        rows = _rows(db, email, keyring)
        assert len(rows) == 2
        assert all(row.last_accepted_at is not None for row in rows)
        assert all(row.reservation_id is None for row in rows)
        assert EMAIL_DELIVERY_ACCEPTED_COOLDOWN.total_seconds() == 14 * 60


def test_expired_reservation_can_be_replaced(delivery_case):
    email, keyring = delivery_case
    first = _acquire(email, keyring)
    assert first is not None
    with _TestSession() as db:
        db.execute(
            text(
                "UPDATE auth_email_delivery_state "
                "SET reservation_expires_at = transaction_timestamp() - INTERVAL '1 second' "
                "WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": first},
        )
        db.commit()

    second = _acquire(email, keyring)
    assert second is not None
    assert second != first
    with _TestSession() as db:
        assert all(row.reservation_id == second for row in _rows(db, email, keyring))
        assert EMAIL_DELIVERY_RESERVATION_LEASE.total_seconds() == 60


def test_expired_lease_cannot_transition_to_sending(delivery_case):
    email, keyring = delivery_case
    reservation_id = _acquire(email, keyring)
    assert reservation_id is not None
    with _TestSession() as db:
        db.execute(
            text(
                "UPDATE auth_email_delivery_state "
                "SET reservation_expires_at = clock_timestamp() - INTERVAL '1 second' "
                "WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": reservation_id},
        )
        db.commit()

    assert not _transition(email, reservation_id, keyring)


def test_transitions_require_ownership_and_ambiguous_hold_is_preserved(delivery_case):
    email, keyring = delivery_case
    reservation_id = _acquire(email, keyring)
    assert reservation_id is not None
    assert not _transition(email, uuid4(), keyring)
    assert _transition(email, reservation_id, keyring)
    with _TestSession() as db:
        before = [row.reservation_expires_at for row in _rows(db, email, keyring)]
        rows_before = _rows(db, email, keyring)
        assert all(
            row.reservation_expires_at - row.updated_at
            == EMAIL_DELIVERY_UNCERTAINTY_HOLD
            for row in rows_before
        )

    with _TestSession() as db:
        assert preserve_ambiguous_email_delivery_hold(
            db, email=email, reservation_id=reservation_id, keyring=keyring
        )
    assert not _release(email, uuid4(), keyring)
    assert not _finalize(email, uuid4(), keyring)
    with _TestSession() as db:
        rows = _rows(db, email, keyring)
        assert [row.reservation_expires_at for row in rows] == before
        assert all(row.reservation_state == "sending" for row in rows)
        assert all(row.reservation_expires_at is not None for row in rows)
        assert EMAIL_DELIVERY_UNCERTAINTY_HOLD.total_seconds() == 15 * 60

    assert _release(email, reservation_id, keyring)
    with _TestSession() as db:
        assert all(row.reservation_id is None for row in _rows(db, email, keyring))


def test_finalization_sets_database_time_cooldown_for_each_alias(delivery_case):
    email, keyring = delivery_case
    reservation_id = _acquire(email, keyring)
    assert reservation_id is not None
    assert _transition(email, reservation_id, keyring)
    assert _finalize(email, reservation_id, keyring)
    with _TestSession() as db:
        rows = _rows(db, email, keyring)
        assert len(rows) == 2
        assert all(row.last_accepted_at is not None for row in rows)
        assert all(row.reservation_state is None for row in rows)
        assert rows[0].last_accepted_at == rows[1].last_accepted_at


def test_old_current_key_overlaps_new_current_previous_alias(delivery_case):
    email, _ = delivery_case
    old_keyring = HMACKeyring(HMACKey("rotation_old", bytes(range(32))))
    rotating_keyring = HMACKeyring(
        current=HMACKey("rotation_new", bytes(range(1, 33))),
        previous=HMACKey("rotation_old", bytes(range(32))),
    )
    old_reservation = _acquire(email, old_keyring)

    assert old_reservation is not None
    assert _acquire(email, rotating_keyring) is None
    with _TestSession() as db:
        rows = _rows(db, email, rotating_keyring)
        assert len(rows) == 2
        old_alias = next(row for row in rows if row.key_id == "rotation_old")
        new_alias = next(row for row in rows if row.key_id == "rotation_new")
        assert old_alias.reservation_id == old_reservation
        assert new_alias.reservation_id is None


def test_concurrent_sessions_only_one_acquire_the_same_email(delivery_case):
    email, keyring = delivery_case
    worker_count = 10
    barrier = Barrier(worker_count)

    def acquire(_index: int):
        barrier.wait(timeout=15)
        with _TestSession() as db:
            return acquire_email_delivery_reservation(db, email=email, keyring=keyring)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(acquire, range(worker_count)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    with _TestSession() as db:
        rows = _rows(db, email, keyring)
        assert len(rows) == 2
        assert {row.reservation_id for row in rows} == {winners[0]}


def test_alias_update_failure_rolls_back_all_alias_inserts(delivery_case):
    email, _ = delivery_case
    keyring = HMACKeyring(
        current=HMACKey(f"trigger_new_{uuid4().hex[:8]}", bytes(range(32))),
        previous=HMACKey(f"trigger_old_{uuid4().hex[:8]}", bytes(range(1, 33))),
    )
    failing_key_id = keyring.current.key_id
    # Use a unique, SQL-identifier-safe trigger and function name for this test.
    suffix = uuid4().hex[:12]
    function_name = f"fail_delivery_update_{suffix}"
    trigger_name = f"fail_delivery_update_{suffix}"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE FUNCTION {function_name}() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    f"IF NEW.key_id = '{failing_key_id}' THEN "
                    "RAISE EXCEPTION 'simulated delivery-state failure'; "
                    "END IF; RETURN NEW; END; $$"
                )
            )
            connection.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE UPDATE "
                    "ON auth_email_delivery_state FOR EACH ROW "
                    f"EXECUTE FUNCTION {function_name}()"
                )
            )

        with _TestSession() as db:
            with pytest.raises(
                EmailDeliveryStateStorageError,
                match="temporarily unavailable",
            ):
                acquire_email_delivery_reservation(db, email=email, keyring=keyring)

        with _TestSession() as db:
            aliases = derive_email_delivery_aliases(email, keyring).values
            keys = tuple((alias.key_id, alias.digest) for alias in aliases)
            assert db.scalar(
                select(func.count())
                .select_from(AuthEmailDeliveryState)
                .where(
                    tuple_(
                        AuthEmailDeliveryState.key_id,
                        AuthEmailDeliveryState.identifier_digest,
                    ).in_(keys)
                )
            ) == 0
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"DROP TRIGGER IF EXISTS {trigger_name} "
                    "ON auth_email_delivery_state"
                )
            )
            connection.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))
