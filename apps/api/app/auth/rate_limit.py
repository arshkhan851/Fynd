"""Persistent, privacy-preserving counters for magic-link request limits."""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.email_address import normalize_email
from app.auth.models import AuthRateLimitCounter

RATE_LIMIT_WINDOW_SECONDS = 900
EMAIL_REQUEST_LIMIT = 5
IP_REQUEST_LIMIT = 10
COUNTER_RETENTION = timedelta(days=1)
MAX_CLEANUP_BATCH_SIZE = 500

RateLimitScope = Literal["email", "ip"]
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class RateLimitConfigurationError(RuntimeError):
    """The HMAC key configuration is missing or invalid."""


class RateLimitIdentifierError(ValueError):
    """A rate-limit identifier cannot be safely normalized."""


class RateLimitStorageError(RuntimeError):
    """A persistent rate-limit operation failed."""


@dataclass(frozen=True)
class HMACKey:
    key_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not _KEY_ID_RE.fullmatch(self.key_id):
            raise RateLimitConfigurationError("Rate-limit HMAC key ID is invalid")
        if not isinstance(self.secret, bytes):
            raise RateLimitConfigurationError("Rate-limit HMAC secret must be binary")
        if len(self.secret) < 32:
            raise RateLimitConfigurationError(
                "Rate-limit HMAC secrets must contain at least 32 bytes"
            )


@dataclass(frozen=True)
class HMACKeyring:
    current: HMACKey
    previous: HMACKey | None = None

    def __post_init__(self) -> None:
        if self.previous is not None and self.previous.key_id == self.current.key_id:
            raise RateLimitConfigurationError(
                "Current and previous rate-limit HMAC key IDs must differ"
            )

    @property
    def keys(self) -> tuple[HMACKey, ...]:
        if self.previous is None:
            return (self.current,)
        return (self.current, self.previous)


@dataclass(frozen=True)
class IdentifierDigest:
    key_id: str
    digest: bytes = field(repr=False)


@dataclass(frozen=True)
class RateLimitDecision:
    email_count: int
    ip_count: int

    @property
    def allowed(self) -> bool:
        return self.email_count <= EMAIL_REQUEST_LIMIT and self.ip_count <= IP_REQUEST_LIMIT


def _decode_secret(encoded: str, variable_name: str) -> bytes:
    try:
        secret = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise RateLimitConfigurationError(
            f"{variable_name} must be valid base64"
        ) from None
    if len(secret) < 32:
        raise RateLimitConfigurationError(
            f"{variable_name} must decode to at least 32 bytes"
        )
    return secret


def load_hmac_keyring(environ: Mapping[str, str] | None = None) -> HMACKeyring:
    """Load versioned HMAC keys from environment without exposing their values.

    During rotation, configure the old key as the previous key until its rate-limit
    windows expire. Replacing a key without configuring the previous key can reset
    effective rate-limit history.
    """
    values = os.environ if environ is None else environ
    current_id = values.get("AUTH_RATE_LIMIT_HMAC_KEY_ID", "").strip()
    current_encoded = values.get("AUTH_RATE_LIMIT_HMAC_KEY_B64", "").strip()
    if not current_id or not current_encoded:
        raise RateLimitConfigurationError(
            "AUTH_RATE_LIMIT_HMAC_KEY_ID and AUTH_RATE_LIMIT_HMAC_KEY_B64 are required"
        )
    current = HMACKey(current_id, _decode_secret(current_encoded, "AUTH_RATE_LIMIT_HMAC_KEY_B64"))

    previous_id = values.get("AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_ID", "").strip()
    previous_encoded = values.get("AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_B64", "").strip()
    if bool(previous_id) != bool(previous_encoded):
        raise RateLimitConfigurationError(
            "Previous HMAC key ID and secret must be configured together"
        )
    previous = None
    if previous_id and previous_encoded:
        previous = HMACKey(
            previous_id,
            _decode_secret(
                previous_encoded, "AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_B64"
            ),
        )
    return HMACKeyring(current=current, previous=previous)


def _hmac_digest(scope: RateLimitScope, canonical_identifier: str, key: HMACKey) -> bytes:
    message = scope.encode("ascii") + b"\x00" + canonical_identifier.encode("utf-8")
    return hmac.new(key.secret, message, hashlib.sha256).digest()


def derive_identifier_digests(
    scope: RateLimitScope,
    identifier: str,
    keyring: HMACKeyring,
) -> tuple[IdentifierDigest, ...]:
    """Normalize an identifier and derive current/previous domain-separated HMACs."""
    if scope == "email":
        try:
            canonical = normalize_email(identifier)
        except (TypeError, ValueError):
            raise RateLimitIdentifierError("Invalid email rate-limit identifier") from None
    elif scope == "ip":
        try:
            address = ipaddress.ip_address(identifier)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            canonical = address.compressed
        except (TypeError, ValueError):
            raise RateLimitIdentifierError("Invalid IP rate-limit identifier") from None
    else:
        raise RateLimitIdentifierError("Unsupported rate-limit scope")

    return tuple(
        IdentifierDigest(key_id=key.key_id, digest=_hmac_digest(scope, canonical, key))
        for key in keyring.keys
    )


def fixed_window_start(now: datetime) -> datetime:
    """Return the UTC epoch-aligned 15-minute window containing an aware datetime."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Rate-limit window calculation requires a timezone-aware datetime")
    epoch_seconds = math.floor(now.astimezone(UTC).timestamp())
    start_seconds = epoch_seconds - (epoch_seconds % RATE_LIMIT_WINDOW_SECONDS)
    return datetime.fromtimestamp(start_seconds, tz=UTC)


def _database_window_start(
    db: Session, timestamp: ColumnElement[datetime] | None = None
) -> datetime:
    instant = func.transaction_timestamp() if timestamp is None else timestamp
    statement = select(
        func.date_bin(
            text(f"INTERVAL '{RATE_LIMIT_WINDOW_SECONDS} seconds'"),
            instant,
            text("TIMESTAMPTZ 'epoch'"),
        )
    )
    window_start = db.scalar(statement)
    if not isinstance(window_start, datetime) or window_start.tzinfo is None:
        raise RateLimitStorageError("Could not determine the rate-limit window")
    return window_start.astimezone(UTC)


def _database_failure(db: Session, error: Exception) -> None:
    if isinstance(error, SQLAlchemyError):
        try:
            db.rollback()
        except SQLAlchemyError:
            pass
        raise RateLimitStorageError("Rate-limit storage is temporarily unavailable") from None
    db.rollback()
    raise error


def increment_request_counters(
    db: Session,
    *,
    email: str,
    client_ip: str,
    keyring: HMACKeyring | None = None,
) -> RateLimitDecision:
    """Atomically increment email and IP buckets, committing even over-limit counts.

    This function owns its transaction and commits both dimensions together. Call it
    with a clean Session before token generation or email delivery.
    """
    active_keyring = keyring if keyring is not None else load_hmac_keyring()
    email_digests = derive_identifier_digests("email", email, active_keyring)
    ip_digests = derive_identifier_digests("ip", client_ip, active_keyring)
    try:
        if db.get_bind().dialect.name != "postgresql":
            raise RateLimitStorageError(
                "Persistent rate limiting requires PostgreSQL"
            )
        window_start = _database_window_start(db)
        buckets = [
            ("email", item.key_id, item.digest) for item in email_digests
        ] + [("ip", item.key_id, item.digest) for item in ip_digests]
        counts: dict[tuple[str, str], int] = {}

        for scope, key_id, digest in sorted(buckets):
            insert = pg_insert(AuthRateLimitCounter).values(
                scope=scope,
                key_id=key_id,
                identifier_digest=digest,
                window_start=window_start,
                request_count=1,
            )
            statement = (
                insert.on_conflict_do_update(
                    index_elements=[
                        AuthRateLimitCounter.scope,
                        AuthRateLimitCounter.key_id,
                        AuthRateLimitCounter.identifier_digest,
                        AuthRateLimitCounter.window_start,
                    ],
                    set_={
                        "request_count": AuthRateLimitCounter.request_count + 1,
                        "updated_at": func.now(),
                    },
                )
                .returning(AuthRateLimitCounter.request_count)
            )
            counts[(scope, key_id)] = int(db.execute(statement).scalar_one())

        db.commit()
    except Exception as error:
        _database_failure(db, error)

    return RateLimitDecision(
        email_count=max(counts[("email", key.key_id)] for key in active_keyring.keys),
        ip_count=max(counts[("ip", key.key_id)] for key in active_keyring.keys),
    )


def cleanup_expired_rate_limit_counters(
    db: Session, *, batch_size: int = MAX_CLEANUP_BATCH_SIZE
) -> int:
    """Delete one bounded batch; cleanup is intentionally not on the request path.

    Operational scheduling remains pending, so callers must invoke this explicitly.
    """
    if not 1 <= batch_size <= MAX_CLEANUP_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_CLEANUP_BATCH_SIZE}")
    if db.get_bind().dialect.name != "postgresql":
        raise RateLimitStorageError("Persistent rate limiting requires PostgreSQL")

    try:
        now = db.scalar(select(func.transaction_timestamp()))
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RateLimitStorageError("Could not determine rate-limit cleanup cutoff")
        cutoff = now.astimezone(UTC) - COUNTER_RETENTION
        expired_rows = (
            select(
                AuthRateLimitCounter.scope.label("scope"),
                AuthRateLimitCounter.key_id.label("key_id"),
                AuthRateLimitCounter.identifier_digest.label("identifier_digest"),
                AuthRateLimitCounter.window_start.label("window_start"),
            )
            .where(AuthRateLimitCounter.window_start < cutoff)
            .order_by(AuthRateLimitCounter.window_start)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
            .cte("expired_rate_limit_rows")
        )
        statement = delete(AuthRateLimitCounter).where(
            tuple_(
                AuthRateLimitCounter.scope,
                AuthRateLimitCounter.key_id,
                AuthRateLimitCounter.identifier_digest,
                AuthRateLimitCounter.window_start,
            ).in_(
                select(
                    expired_rows.c.scope,
                    expired_rows.c.key_id,
                    expired_rows.c.identifier_digest,
                    expired_rows.c.window_start,
                )
            )
        )
        deleted = db.execute(statement).rowcount or 0
        db.commit()
        return deleted
    except Exception as error:
        _database_failure(db, error)
