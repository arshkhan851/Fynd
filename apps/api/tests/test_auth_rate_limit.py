import base64
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.auth.models import AuthRateLimitCounter
from app.auth.rate_limit import (
    HMACKey,
    HMACKeyring,
    RateLimitConfigurationError,
    RateLimitIdentifierError,
    _hmac_digest,
    derive_identifier_digests,
    fixed_window_start,
    load_hmac_keyring,
)


def _keyring() -> HMACKeyring:
    return HMACKeyring(HMACKey("current1", bytes(range(32))))


def test_counter_model_has_composite_key_constraints_and_cleanup_index():
    table = AuthRateLimitCounter.__table__

    assert list(table.primary_key.columns.keys()) == [
        "scope",
        "key_id",
        "identifier_digest",
        "window_start",
    ]
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_auth_rate_limit_counters_scope",
        "ck_auth_rate_limit_counters_key_id",
        "ck_auth_rate_limit_counters_digest_length",
        "ck_auth_rate_limit_counters_positive_count",
    }
    assert {index.name for index in table.indexes} == {
        "ix_auth_rate_limit_counters_window_start"
    }
    assert table.c.updated_at.server_default is not None


def test_hmac_is_deterministic_and_email_normalization_is_shared():
    keyring = _keyring()

    upper = derive_identifier_digests("email", " Alice@EXAMPLE.ORG ", keyring)
    normalized = derive_identifier_digests("email", "alice@example.org", keyring)

    assert upper == normalized
    assert len(upper[0].digest) == 32


def test_hmac_input_is_domain_separated_by_scope():
    key = _keyring().current

    assert _hmac_digest("email", "same-canonical-value", key) != _hmac_digest(
        "ip", "same-canonical-value", key
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("2001:0db8:0:0:0:0:0:1", "2001:db8::1"),
        ("192.0.2.1", "::ffff:192.0.2.1"),
        ("192.0.2.1", "::ffff:c000:201"),
    ],
)
def test_ip_addresses_are_canonicalized(first, second):
    keyring = _keyring()
    assert derive_identifier_digests("ip", first, keyring) == derive_identifier_digests(
        "ip", second, keyring
    )


def test_ipv4_address_is_supported():
    digests = derive_identifier_digests("ip", "192.0.2.1", _keyring())
    assert len(digests[0].digest) == 32


def test_missing_hmac_configuration_fails_closed():
    with pytest.raises(RateLimitConfigurationError, match="are required"):
        load_hmac_keyring({})


@pytest.mark.parametrize(
    ("encoded", "message"),
    [("not base64!", "valid base64"), (base64.b64encode(b"short").decode(), "32 bytes")],
)
def test_invalid_hmac_secret_fails_closed(encoded, message):
    with pytest.raises(RateLimitConfigurationError, match=message):
        load_hmac_keyring(
            {
                "AUTH_RATE_LIMIT_HMAC_KEY_ID": "current1",
                "AUTH_RATE_LIMIT_HMAC_KEY_B64": encoded,
            }
        )


def test_previous_hmac_key_must_be_complete_and_have_a_distinct_id():
    current = base64.b64encode(bytes(range(32))).decode()
    previous = base64.b64encode(bytes(range(1, 33))).decode()
    base = {
        "AUTH_RATE_LIMIT_HMAC_KEY_ID": "current1",
        "AUTH_RATE_LIMIT_HMAC_KEY_B64": current,
    }

    with pytest.raises(RateLimitConfigurationError, match="configured together"):
        load_hmac_keyring({**base, "AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_ID": "old1"})

    keyring = load_hmac_keyring(
        {
            **base,
            "AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_ID": "old1",
            "AUTH_RATE_LIMIT_PREVIOUS_HMAC_KEY_B64": previous,
        }
    )
    digests = derive_identifier_digests("email", "person@example.org", keyring)
    assert [digest.key_id for digest in digests] == ["current1", "old1"]
    assert digests[0].digest != digests[1].digest

    with pytest.raises(RateLimitConfigurationError, match="must differ"):
        HMACKeyring(
            current=HMACKey("same", bytes(range(32))),
            previous=HMACKey("same", bytes(range(32))),
        )


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
            datetime(
                2026, 11, 13, 15, 45, tzinfo=timezone(timedelta(hours=5, minutes=30))
            ),
            datetime(2026, 11, 13, 10, 15, tzinfo=UTC),
        ),
    ],
)
def test_fixed_window_start_uses_utc_epoch_aligned_15_minute_windows(instant, expected):
    assert fixed_window_start(instant) == expected


def test_fixed_window_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        fixed_window_start(datetime(2026, 11, 13, 10, 15))


def test_invalid_ip_is_rejected_without_echoing_value():
    with pytest.raises(RateLimitIdentifierError, match="Invalid IP") as exc_info:
        derive_identifier_digests("ip", "private-ip-input", _keyring())
    assert "private-ip-input" not in str(exc_info.value)
