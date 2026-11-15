from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.auth.email_delivery import (
    EMAIL_DELIVERY_ACCEPTED_COOLDOWN,
    EMAIL_DELIVERY_RESERVATION_LEASE,
    EMAIL_DELIVERY_UNCERTAINTY_HOLD,
    _rows_are_blocked,
    derive_email_delivery_aliases,
)
from app.auth.models import AuthEmailDeliveryState
from app.auth.rate_limit import HMACKey, HMACKeyring


def _keyring() -> HMACKeyring:
    return HMACKeyring(
        current=HMACKey("current", bytes(range(32))),
        previous=HMACKey("previous", bytes(range(1, 33))),
    )


def test_delivery_constants_match_approved_timing():
    assert EMAIL_DELIVERY_RESERVATION_LEASE == timedelta(seconds=60)
    assert EMAIL_DELIVERY_UNCERTAINTY_HOLD == timedelta(minutes=15)
    assert EMAIL_DELIVERY_ACCEPTED_COOLDOWN == timedelta(minutes=14)


def test_cooldown_is_expired_exactly_at_fourteen_minute_boundary():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    exactly_at_boundary = SimpleNamespace(
        last_accepted_at=now - EMAIL_DELIVERY_ACCEPTED_COOLDOWN,
        reservation_state=None,
        reservation_expires_at=None,
    )
    just_inside_boundary = SimpleNamespace(
        last_accepted_at=now - EMAIL_DELIVERY_ACCEPTED_COOLDOWN + timedelta(microseconds=1),
        reservation_state=None,
        reservation_expires_at=None,
    )

    assert _rows_are_blocked([exactly_at_boundary], now) is False
    assert _rows_are_blocked([just_inside_boundary], now) is True


def test_reservation_expires_exactly_at_fifteen_minute_boundary():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    exactly_at_boundary = SimpleNamespace(
        last_accepted_at=None,
        reservation_state="sending",
        reservation_expires_at=now,
    )
    still_live = SimpleNamespace(
        last_accepted_at=None,
        reservation_state="sending",
        reservation_expires_at=now + timedelta(microseconds=1),
    )

    assert _rows_are_blocked([exactly_at_boundary], now) is False
    assert _rows_are_blocked([still_live], now) is True


def test_email_aliases_reuse_normalization_rotation_and_hide_identifier():
    aliases = derive_email_delivery_aliases(" Alice@EXAMPLE.ORG ", _keyring())
    normalized = derive_email_delivery_aliases("alice@example.org", _keyring())

    assert aliases == normalized
    assert [alias.key_id for alias in aliases.values] == ["current", "previous"]
    assert all(len(alias.digest) == 32 for alias in aliases.values)
    assert "alice@example.org" not in repr(aliases)


def test_delivery_state_model_has_only_hashed_identifiers_and_expected_constraints():
    table = AuthEmailDeliveryState.__table__

    assert list(table.primary_key.columns.keys()) == ["key_id", "identifier_digest"]
    assert set(table.columns.keys()) == {
        "key_id",
        "identifier_digest",
        "last_accepted_at",
        "reservation_id",
        "reservation_state",
        "reservation_expires_at",
        "updated_at",
    }
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_auth_email_delivery_state_key_id",
        "ck_auth_email_delivery_state_digest_length",
        "ck_auth_email_delivery_state_reservation_state",
        "ck_auth_email_delivery_state_reservation_fields",
    }
    assert {index.name for index in table.indexes} == {
        "ix_auth_email_delivery_state_cleanup"
    }
    assert table.c.updated_at.server_default is not None
    assert table.c.last_accepted_at.type.timezone is True
    assert table.c.reservation_expires_at.type.timezone is True
    assert table.c.identifier_digest.type.length == 32
