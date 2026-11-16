"""PostgreSQL-backed, privacy-preserving email delivery reservations.

This module only manages reservation state. It is intentionally not integrated into
the authentication router or provider in this phase.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.models import AuthEmailDeliveryState
from app.auth.rate_limit import (
    HMACKeyring,
    IdentifierDigest,
    RateLimitIdentifierError,
    derive_identifier_digests,
    load_hmac_keyring,
)

EMAIL_DELIVERY_RESERVATION_LEASE = timedelta(seconds=60)
EMAIL_DELIVERY_UNCERTAINTY_HOLD = timedelta(minutes=15)
EMAIL_DELIVERY_ACCEPTED_COOLDOWN = timedelta(minutes=14)


class EmailDeliveryStateStorageError(RuntimeError):
    """A delivery-state database operation failed and the operation was rolled back."""


@dataclass(frozen=True)
class EmailDeliveryAliases:
    """Opaque aliases used to address all configured key versions."""

    values: tuple[IdentifierDigest, ...] = field(repr=False)


def derive_email_delivery_aliases(
    email: str, keyring: HMACKeyring | None = None
) -> EmailDeliveryAliases:
    """Derive sorted email HMAC aliases using the shared rate-limit key conventions."""
    active_keyring = keyring if keyring is not None else load_hmac_keyring()
    try:
        digests = derive_identifier_digests("email", email, active_keyring)
    except RateLimitIdentifierError:
        raise
    return EmailDeliveryAliases(
        tuple(sorted(digests, key=lambda item: (item.key_id, item.digest)))
    )


def _rollback_quietly(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        pass


def _storage_error(db: Session) -> None:
    _rollback_quietly(db)
    raise EmailDeliveryStateStorageError(
        "Email delivery state is temporarily unavailable"
    ) from None


def _require_postgresql(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise EmailDeliveryStateStorageError(
            "Email delivery reservations require PostgreSQL"
        )


def _alias_keys(aliases: EmailDeliveryAliases) -> tuple[tuple[str, bytes], ...]:
    return tuple((item.key_id, item.digest) for item in aliases.values)


def _lock_alias_rows(
    db: Session, aliases: EmailDeliveryAliases
) -> list[AuthEmailDeliveryState]:
    keys = _alias_keys(aliases)
    rows = db.scalars(
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
        .with_for_update()
    ).all()
    if tuple((row.key_id, row.identifier_digest) for row in rows) != keys:
        raise EmailDeliveryStateStorageError(
            "Email delivery state aliases are incomplete"
        )
    return rows


def _update_all_aliases(
    db: Session,
    aliases: EmailDeliveryAliases,
    *,
    values: dict[str, object],
    where: tuple[object, ...] = (),
) -> int:
    statement = update(AuthEmailDeliveryState).where(
        tuple_(
            AuthEmailDeliveryState.key_id,
            AuthEmailDeliveryState.identifier_digest,
        ).in_(_alias_keys(aliases))
    )
    for condition in where:
        statement = statement.where(condition)
    return db.execute(statement.values(**values)).rowcount or 0


def _get_database_time(db: Session) -> datetime:
    # Use wall-clock database time rather than transaction start time. Operations may
    # have waited on alias locks; a transaction timestamp could let an expired lease
    # appear live after a long lock wait.
    now = db.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise EmailDeliveryStateStorageError(
            "Could not determine email delivery state time"
        )
    return now


def _rows_are_blocked(rows: list[AuthEmailDeliveryState], now: datetime) -> bool:
    cooldown_cutoff = now - EMAIL_DELIVERY_ACCEPTED_COOLDOWN
    for row in rows:
        if row.last_accepted_at is not None and row.last_accepted_at > cooldown_cutoff:
            return True
        if row.reservation_state is not None:
            # Malformed legacy state fails closed. The database CHECK prevents new
            # malformed rows, but this also protects service behavior during repair.
            if row.reservation_expires_at is None:
                return True
            if row.reservation_expires_at > now:
                return True
    return False


def acquire_email_delivery_reservation(
    db: Session,
    *,
    email: str,
    keyring: HMACKeyring | None = None,
) -> UUID | None:
    """Acquire every active key alias atomically, or return None when suppressed.

    The function owns and commits its transaction. Database errors roll back all
    alias inserts and updates. Raw email addresses are never persisted.
    """
    aliases = derive_email_delivery_aliases(email, keyring)
    try:
        _require_postgresql(db)
        for alias in aliases.values:
            statement = (
                pg_insert(AuthEmailDeliveryState)
                .values(key_id=alias.key_id, identifier_digest=alias.digest)
                .on_conflict_do_nothing(
                    index_elements=[
                        AuthEmailDeliveryState.key_id,
                        AuthEmailDeliveryState.identifier_digest,
                    ]
                )
            )
            db.execute(statement)

        rows = _lock_alias_rows(db, aliases)
        now = _get_database_time(db)
        if _rows_are_blocked(rows, now):
            db.commit()
            return None

        reservation_id = uuid4()
        expires_at = now + EMAIL_DELIVERY_RESERVATION_LEASE
        updated = _update_all_aliases(
            db,
            aliases,
            values={
                "reservation_id": reservation_id,
                "reservation_state": "reserved",
                "reservation_expires_at": expires_at,
                "updated_at": now,
            },
        )
        if updated != len(aliases.values):
            raise EmailDeliveryStateStorageError(
                "Email delivery reservation was not acquired for every alias"
            )
        db.commit()
        return reservation_id
    except SQLAlchemyError:
        _storage_error(db)
    except EmailDeliveryStateStorageError:
        _rollback_quietly(db)
        raise
    except Exception:
        _rollback_quietly(db)
        raise


def transition_email_delivery_to_sending(
    db: Session,
    *,
    email: str,
    reservation_id: UUID,
    keyring: HMACKeyring | None = None,
) -> bool:
    """Conditionally claim the live lease and start the uncertainty hold."""
    aliases = derive_email_delivery_aliases(email, keyring)
    try:
        _require_postgresql(db)
        now = _get_database_time(db)
        rows = _lock_alias_rows(db, aliases)
        if any(
            row.reservation_id != reservation_id
            or row.reservation_state != "reserved"
            or row.reservation_expires_at is None
            or row.reservation_expires_at <= now
            for row in rows
        ):
            db.rollback()
            return False

        updated = _update_all_aliases(
            db,
            aliases,
            values={
                "reservation_state": "sending",
                "reservation_expires_at": now + EMAIL_DELIVERY_UNCERTAINTY_HOLD,
                "updated_at": now,
            },
            where=(
                AuthEmailDeliveryState.reservation_id == reservation_id,
                AuthEmailDeliveryState.reservation_state == "reserved",
                AuthEmailDeliveryState.reservation_expires_at > func.clock_timestamp(),
            ),
        )
        if updated != len(aliases.values):
            db.rollback()
            return False
        db.commit()
        return True
    except SQLAlchemyError:
        _storage_error(db)
    except EmailDeliveryStateStorageError:
        _rollback_quietly(db)
        raise
    except Exception:
        _rollback_quietly(db)
        raise


def release_email_delivery_reservation(
    db: Session,
    *,
    email: str,
    reservation_id: UUID,
    keyring: HMACKeyring | None = None,
) -> bool:
    """Release all aliases only when every alias still belongs to this reservation."""
    aliases = derive_email_delivery_aliases(email, keyring)
    try:
        _require_postgresql(db)
        rows = _lock_alias_rows(db, aliases)
        if any(row.reservation_id != reservation_id for row in rows):
            db.rollback()
            return False
        updated = _update_all_aliases(
            db,
            aliases,
            values={
                "reservation_id": None,
                "reservation_state": None,
                "reservation_expires_at": None,
                "updated_at": func.clock_timestamp(),
            },
            where=(AuthEmailDeliveryState.reservation_id == reservation_id,),
        )
        if updated != len(aliases.values):
            db.rollback()
            return False
        db.commit()
        return True
    except SQLAlchemyError:
        _storage_error(db)
    except EmailDeliveryStateStorageError:
        _rollback_quietly(db)
        raise
    except Exception:
        _rollback_quietly(db)
        raise


def finalize_accepted_email_delivery(
    db: Session,
    *,
    email: str,
    reservation_id: UUID,
    keyring: HMACKeyring | None = None,
) -> bool:
    """Record provider acceptance and clear all matching sending reservations."""
    aliases = derive_email_delivery_aliases(email, keyring)
    try:
        _require_postgresql(db)
        now = _get_database_time(db)
        rows = _lock_alias_rows(db, aliases)
        if any(
            row.reservation_id != reservation_id or row.reservation_state != "sending"
            for row in rows
        ):
            db.rollback()
            return False
        updated = _update_all_aliases(
            db,
            aliases,
            values={
                "last_accepted_at": now,
                "reservation_id": None,
                "reservation_state": None,
                "reservation_expires_at": None,
                "updated_at": now,
            },
            where=(
                AuthEmailDeliveryState.reservation_id == reservation_id,
                AuthEmailDeliveryState.reservation_state == "sending",
            ),
        )
        if updated != len(aliases.values):
            db.rollback()
            return False
        db.commit()
        return True
    except SQLAlchemyError:
        _storage_error(db)
    except EmailDeliveryStateStorageError:
        _rollback_quietly(db)
        raise
    except Exception:
        _rollback_quietly(db)
        raise


def preserve_ambiguous_email_delivery_hold(
    db: Session,
    *,
    email: str,
    reservation_id: UUID,
    keyring: HMACKeyring | None = None,
) -> bool:
    """Confirm an ambiguous sending reservation remains held without extending it.

    Callers should leave a still-live sending row untouched after timeout/ambiguous
    provider outcomes. Its existing 15-minute expiry bounds the uncertainty hold.
    """
    aliases = derive_email_delivery_aliases(email, keyring)
    try:
        _require_postgresql(db)
        now = _get_database_time(db)
        rows = _lock_alias_rows(db, aliases)
        preserved = all(
            row.reservation_id == reservation_id
            and row.reservation_state == "sending"
            and row.reservation_expires_at is not None
            and row.reservation_expires_at > now
            for row in rows
        )
        db.commit()
        return preserved
    except SQLAlchemyError:
        _storage_error(db)
    except EmailDeliveryStateStorageError:
        _rollback_quietly(db)
        raise
    except Exception:
        _rollback_quietly(db)
        raise
